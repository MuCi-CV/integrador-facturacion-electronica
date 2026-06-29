# Spec — Enriquecimiento de razón social a partir del RUC

**Fecha:** 2026-06-29
**Rama:** `feature/refactor-service-layer`
**Estado:** Aprobado para implementación (con caché dedicado `RucCache`, TTL 30 días)

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

Se separa en dos piezas: `_fetch_from_api` (solo HTTP, sin saber de caché) y
`get_razon_social` (orquesta caché + API).

```python
import logging
from datetime import timedelta
from typing import Optional

import requests
from django.conf import settings
from django.utils.timezone import now

from core.models import RucCache

logger = logging.getLogger("ruc_api")

CACHE_TTL_DAYS = 30


def _fetch_from_api(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Pega a la fuente externa. Devuelve la razón social si responde positivo;
    None ante fuente no configurada, error de red/timeout, JSON inválido, sin
    match o razón social vacía. Nunca lanza hacia el caller.
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


def get_razon_social(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Resuelve la razón social de un RUC usando RucCache (TTL 30 días) y, si hace
    falta, la fuente externa. Ver reglas de caché/fallback en la sección RucCache.
    """
    if not ruc:
        return None

    cached = RucCache.objects.filter(ruc=ruc).first()

    # 1. Caché fresco (<30 días) → usar sin pegarle a la API.
    if cached and (now() - cached.checked_at) < timedelta(days=CACHE_TTL_DAYS):
        return cached.razon_social

    # 2. No hay caché o está vencido → consultar la API.
    fetched = _fetch_from_api(ruc, timeout)
    if fetched:
        # Éxito: refrescar el caché y SÍ renovar checked_at.
        RucCache.objects.update_or_create(
            ruc=ruc, defaults={"razon_social": fetched, "checked_at": now()}
        )
        return fetched

    # 3. La API falló o no devolvió match:
    #    - hay valor viejo en caché → usarlo SIN renovar checked_at (caso B1),
    #      para que la próxima orden vuelva a intentar la API.
    if cached:
        return cached.razon_social
    #    - no hay nada cacheado → caer a WooCommerce (caso A).
    return None
```

### Modelo nuevo: `RucCache` (`core/models.py`)

Caché dedicado para el padrón de RUC. **Separado** de `ContactCache` porque: (a) se
puebla en la consulta misma, no depende de que el contacto exista en BIMS (que necesita
`bims_id`); (b) la llave es el **RUC completo con dígito verificador**, no el número base
sin DV que guarda `ContactCache.document_id`; (c) es otra responsabilidad (cachear una
fuente externa, no mapear contactos a BIMS).

```python
class RucCache(models.Model):
    ruc = models.CharField(verbose_name="RUC", max_length=20, unique=True, db_index=True)  # con DV: "80012345-6"
    razon_social = models.CharField(verbose_name="Razón social", max_length=255)
    checked_at = models.DateTimeField(verbose_name="Última consulta exitosa a la API")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Caché de RUC"
        verbose_name_plural = "Cachés de RUC"
```

- `checked_at` se setea **explícitamente** a `now()` (no `auto_now`) y solo se actualiza
  cuando la API responde positivo. Representa la última consulta **exitosa**, no la última
  lectura.
- Migración nueva (`0005_ruccache`).

### Reglas de caché y fallback

| Situación | Acción | ¿Se actualiza `checked_at`? |
|---|---|---|
| Caché fresco (`checked_at` < 30 días) | Usar razón social cacheada, **sin** llamar a la API | No |
| Caché vencido/ausente + API positiva | Usar valor de la API y `update_or_create` | **Sí** (a `now()`) |
| Caché vencido + API falla/sin match (B1) | Usar el valor **viejo** del caché | **No** (queda vencido → se reintenta en la próxima orden) |
| Sin caché + API falla/sin match (A) | Caer a los datos de WooCommerce (`None`) | — |

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

Agrega un HTTP extra por orden con RUC, en un webhook síncrono. Mitigaciones: `timeout`
corto (5 s), fallo silencioso a WooCommerce, y el **caché `RucCache` (TTL 30 días)** que
evita la llamada para RUCs ya consultados recientemente (los clientes recurrentes no
pegan a la API). La razón social casi nunca cambia, así que el TTL de 30 días refresca lo
suficiente sin penalizar cada orden.

## Lo que NO se toca

- `BimsApi.find_razon_social_by_ruc` queda **intacta** (parece parte de otra tarea
  inconclusa que habla con BIMS; fuera de alcance).
- Sin cambios en WooCommerce / frontend.

## Tests (`core/tests.py`, con mock de `requests`)

Clase nueva, con mock de `requests` (sin pegarle a turuc real). `RucCache` usa la BD de
test en memoria.

**`_fetch_from_api` (capa HTTP):**
1. **Positivo**: la fuente devuelve `razonSocial` → retorna ese valor.
2. **Sin match → None**: `data` ausente o sin `razonSocial`, o `razonSocial` vacía.
3. **Error de red/timeout → None**: `requests.RequestException` capturada.
4. **No configurado → None**: `RUC_API_URL` ausente → `None` sin hacer request.
5. **JSON malformado → None**: respuesta no-JSON (`ValueError`).

**`get_razon_social` (caché + API):**
6. **Caché fresco**: existe `RucCache` con `checked_at` reciente → devuelve el valor
   cacheado y **no** llama a `_fetch_from_api`.
7. **Caché vencido + API ok**: `checked_at` > 30 días → consulta la API, devuelve el
   nuevo valor y actualiza `checked_at` a ahora.
8. **Caché vencido + API falla (B1)**: devuelve el valor **viejo** y `checked_at`
   **no** cambia (sigue vencido).
9. **Sin caché + API ok**: crea la fila en `RucCache` y devuelve el valor.
10. **Sin caché + API falla (A)**: devuelve `None`.

**Integración en `resolve_contact_id`:**
11. Con `document_type == "ruc"` y fuente positiva, el contacto se arma con la razón
    social autoritativa; con `document_type == "ci"` no se consulta la fuente.

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
