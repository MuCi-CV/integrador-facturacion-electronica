# Spec — Enriquecimiento de razón social a partir del RUC

**Fecha:** 2026-06-29
**Rama:** `feature/refactor-service-layer`
**Estado:** Aprobado para implementación (sin caché)

## Problema

En el checkout de WooCommerce el cliente carga su RUC y su razón social a mano.
Es común que el RUC esté bien pero la razón social esté mal escrita, incompleta o
no corresponda. Eso genera contactos/facturas en BIMS con la razón social incorrecta.

Queremos corregir la razón social automáticamente consultando una fuente externa
por RUC, **del lado del integrador** (sin tocar WooCommerce).

## Regla de comportamiento

- Aplica **solo cuando hay un RUC real** (`document_type == "ruc"`, es decir el valor
  tiene guión verificador).
- Si la fuente externa responde **positivo** (devuelve una razón social) → **siempre**
  se sobrescribe el `name` del contacto con ese valor autoritativo, sin importar lo que
  haya cargado el cliente.
- Si la fuente responde **negativo** o falla (red, timeout, no configurada, JSON
  inválido, sin match) → se mantiene el valor que llegó de WooCommerce. La consulta
  **nunca** debe romper el procesamiento de la orden.

## Contexto legal / privacidad

El RUC y su razón social son **datos públicos** del padrón de la DNIT/SET de Paraguay
(consultables por cualquiera). Además, para una factura legal la razón social debe
corresponder al RUC. Corregirla es un requisito fiscal, no un dato privado descubierto.
Como la funcionalidad vive en el backend y solo se usa al facturar, no hay exposición
del dato en el frontend.

## Fuente de datos

**Elegida: `turuc.com.py`** (API REST pública).

- Gratuita, **sin API key, sin registro, sin pago**, datos provenientes de la DNIT.
- Responde a clientes servidor (verificado: HTTP 200 a una petición no-navegador;
  a diferencia de `rucparaguay.info`, que bloquea con 403).
- Endpoint: `GET https://turuc.com.py/api/contribuyente/{ruc}` donde `{ruc}` incluye el
  dígito verificador (ej. `80012345-6`).
- Respuesta JSON real (verificada 2026-06-29):

  ```json
  {
    "data": {
      "doc": 80012345,
      "razonSocial": "COMERCIO Y FINANZAS SA",
      "dv": 0,
      "ruc": "80012345-0",
      "estado": "BLOQUEADO",
      "esPersonaJuridica": true,
      "esEntidadPublica": false
    },
    "message": "OK"
  }
  ```

- Campo de interés: `data.razonSocial`. Se usa siempre que esté presente y no vacío,
  independientemente de `estado` (el estado es incumbencia de BIMS, no de la corrección
  del nombre).

**Alternativas documentadas (no se implementan ahora):**
- Servicio oficial SET/DNIT (`servicios.set.gov.py/EsetApiWS/ApiWS/consultaRUC?apiKey=...`):
  autoritativo pero requiere apiKey/registro y separar ruc+dv.
- Dataset self-hosted (proyectos open-source que bajan el padrón de la DNIT a una base
  local): sin dependencia externa ni rate limits, pero con costo de mantenimiento/staleness.

El módulo se diseña con una interfaz mínima para poder cambiar de fuente sin tocar el
flujo de órdenes.

## Diseño técnico

### Módulo nuevo: `core/ruc.py`

Independiente de `BimsApi`. **No** reutiliza `_retry_request` / `_request_with_relogin`
(esa maquinaria es de BIMS: relogin con `sid`, `raise_for_status`, formato BIMS — nada de
eso aplica a un endpoint de terceros).

```python
import logging
import requests
from typing import Optional

from django.conf import settings

logger = logging.getLogger("ruc_api")


def get_razon_social(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Consulta la razón social de un RUC (con dígito verificador) en la fuente externa.

    Devuelve la razón social si la fuente responde positivo; devuelve None ante
    cualquier otra situación (fuente no configurada, error de red, timeout, respuesta
    no-JSON, sin match, o razón social vacía). Nunca lanza excepción hacia el caller.
    """
    base_url = getattr(settings, "RUC_API_URL", None)
    if not base_url or not ruc:
        return None
    try:
        res = requests.get(f"{base_url}/api/contribuyente/{ruc}", timeout=timeout)
        res.raise_for_status()
        payload = res.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Consulta RUC {ruc} falló: {e}")
        return None

    razon_social = (payload.get("data") or {}).get("razonSocial")
    if razon_social and razon_social.strip():
        return razon_social.strip()
    return None
```

### Configuración

- Nuevo setting `RUC_API_URL` en `muci-integrador/settings.py`, leído del `.env`
  (`config.get("RUC_API_URL")`), con default `https://turuc.com.py`.
- **Separado** del `RUC_URL` existente para no chocar con `find_razon_social_by_ruc`,
  que se deja intacta.
- Agregar `RUC_API_URL` a `muci-integrador/test_settings.py` con un valor de prueba.

### Punto de integración: `resolve_contact_id` en `core/services.py`

En la rama donde se arma el `name` del contacto a partir de los datos de WooCommerce
(no-POS, con `_billing_ruc` / `_billing_razon_social`), después de determinar
`document_type` y `name`:

- Si `document_type == "ruc"`: llamar a `core.ruc.get_razon_social(document_id)`.
  - Si devuelve un valor → asignar `name = valor` (sobrescribe siempre).
  - Si devuelve `None` → dejar `name` como está (valor de WooCommerce).
- Loguear cuándo se corrige (valor viejo vs. nuevo) para trazabilidad.

Se usa `document_id` con su guión verificador (el código ya preserva `ruc_value` completo).

### Latencia

Agrega un HTTP extra por orden con RUC, en un webhook síncrono. Mitigación: `timeout`
corto (5 s) y fallo silencioso a los datos de WooCommerce. **Sin caché** en esta
iteración (decisión explícita). Si la latencia molesta, se podrá agregar después un
cache RUC→razón social sin cambiar la interfaz del módulo.

## Lo que NO se toca

- `BimsApi.find_razon_social_by_ruc` queda **intacta** (parece parte de otra tarea
  inconclusa que habla con BIMS; fuera de alcance).
- Sin cambios en WooCommerce / frontend.

## Tests (`core/tests.py`, con mock de `requests`)

Clase nueva, sin pegarle a turuc real:

1. **Positivo sobrescribe**: la fuente devuelve `razonSocial` → `get_razon_social`
   retorna ese valor (y en services, `name` se sobrescribe).
2. **Negativo usa WooCommerce**: respuesta sin match (`data` ausente o sin
   `razonSocial`, o `razonSocial` vacía) → `None`.
3. **Error de red/timeout → None**: `requests.RequestException` capturada → `None`.
4. **No configurado → None**: `RUC_API_URL` ausente → `None` sin hacer request.
5. **JSON malformado → None**: respuesta no-JSON (`ValueError`) → `None`.
6. **Integración en `resolve_contact_id`**: con `document_type == "ruc"` y fuente
   positiva, el contacto se arma con la razón social autoritativa; con `document_type
   == "ci"` no se consulta la fuente.

## Compatibilidad

Mantener compatibilidad con Python 3.7.17 (producción): usar `Optional[...]`, sin
`X | Y` ni `match`.

## Criterio de aceptación

- Orden con RUC válido y razón social mal cargada → el contacto en BIMS queda con la
  razón social del padrón.
- Fuente caída/no configurada → la orden se procesa igual con los datos de WooCommerce.
- Toda la suite de tests en verde.
</content>
</invoke>
