# Enriquecimiento de razón social por RUC — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Corregir automáticamente la razón social de un contacto consultando el RUC en una fuente externa (turuc), del lado del integrador, con caché de 30 días.

**Architecture:** Un módulo nuevo `core/ruc.py` independiente de `BimsApi` consulta turuc con su propio HTTP (timeout, manejo de error, fail-safe). Un modelo `RucCache` cachea RUC→razón social con TTL de 30 días. `resolve_contact_id` usa el valor autoritativo cuando hay RUC; si no, mantiene el dato de WooCommerce.

**Tech Stack:** Python 3.7, Django 3.2 (prod) / DRF, `requests`, `unittest.mock`.

## Global Constraints

- **Compatibilidad Python 3.7.17** (producción): usar `Optional[...]` / `Union[...]`, NO `X | Y`, NO `match`.
- **Formato Black** sobre todo el código nuevo.
- **Tests**: `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings` (sin `.env`, sqlite en memoria).
- **Mocks de HTTP**: nunca pegarle a turuc real en los tests; mockear `requests`.
- **No tocar** `BimsApi.find_razon_social_by_ruc` (fuera de alcance).
- **No tocar** WooCommerce/frontend.
- Commits frecuentes, uno por tarea.

---

### Task 1: Modelo `RucCache` + migración

**Files:**
- Modify: `core/models.py` (agregar clase `RucCache` al final)
- Create: `core/migrations/0005_ruccache.py` (generada por `makemigrations`)
- Test: `core/tests.py` (clase `RucCacheModelTest`)

**Interfaces:**
- Produces: modelo `RucCache` con campos `ruc: str` (unique), `razon_social: str`, `checked_at: datetime`, `created_at: datetime`.

- [ ] **Step 1: Escribir el test que falla**

En `core/tests.py`, agregar al final (las importaciones `TestCase`, `requests`, etc. ya existen arriba del archivo):

```python
from core.models import RucCache


class RucCacheModelTest(TestCase):
    def test_se_crea_y_es_unico_por_ruc(self):
        from django.utils.timezone import now
        from django.db import IntegrityError

        RucCache.objects.create(
            ruc="80012345-6", razon_social="EMPRESA SA", checked_at=now()
        )
        self.assertEqual(RucCache.objects.get(ruc="80012345-6").razon_social, "EMPRESA SA")
        with self.assertRaises(IntegrityError):
            RucCache.objects.create(
                ruc="80012345-6", razon_social="OTRA SA", checked_at=now()
            )
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.RucCacheModelTest --settings=muci-integrador.test_settings`
Expected: FAIL — `ImportError: cannot import name 'RucCache'` (el modelo no existe).

- [ ] **Step 3: Agregar el modelo**

En `core/models.py`, al final del archivo:

```python
class RucCache(models.Model):
    ruc = models.CharField(
        verbose_name="RUC", max_length=20, unique=True, db_index=True
    )  # con dígito verificador: "80012345-6"
    razon_social = models.CharField(verbose_name="Razón social", max_length=255)
    checked_at = models.DateTimeField(verbose_name="Última consulta exitosa a la API")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Caché de RUC"
        verbose_name_plural = "Cachés de RUC"

    def __str__(self):
        return f"{self.ruc} - {self.razon_social}"
```

- [ ] **Step 4: Generar la migración**

Run: `.venv/bin/python manage.py makemigrations core --settings=muci-integrador.test_settings`
Expected: crea `core/migrations/0005_ruccache.py` con `dependencies = [("core", "0004_add_index_failedorder_status")]` y `CreateModel(name="RucCache", ...)`.

- [ ] **Step 5: Correr el test para verificar que pasa**

Run: `.venv/bin/python manage.py test core.tests.RucCacheModelTest --settings=muci-integrador.test_settings`
Expected: PASS (2 aserciones, sin errores).

- [ ] **Step 6: Commit**

```bash
git add core/models.py core/migrations/0005_ruccache.py core/tests.py
git commit -m "feat: modelo RucCache para cachear razón social por RUC"
```

---

### Task 2: Setting `RUC_API_URL` + capa HTTP `_fetch_from_api`

**Files:**
- Modify: `muci-integrador/settings.py` (agregar `RUC_API_URL`)
- Modify: `muci-integrador/test_settings.py` (agregar `RUC_API_URL`)
- Create: `core/ruc.py`
- Test: `core/tests.py` (clase `FetchFromApiTest`)

**Interfaces:**
- Consumes: `settings.RUC_API_URL` (str | None).
- Produces: `core.ruc._fetch_from_api(ruc: str, timeout: int = 5) -> Optional[str]` — devuelve la razón social o `None`; nunca lanza.

- [ ] **Step 1: Agregar el setting (prod y test)**

En `muci-integrador/settings.py`, justo debajo de la línea `RUC_URL = config.get("RUC_URL")` (línea ~238):

```python
RUC_API_URL = config.get("RUC_API_URL") or "https://turuc.com.py"
```

En `muci-integrador/test_settings.py`, debajo de `RUC_URL = None`:

```python
RUC_API_URL = "http://turuc.test.local"
```

- [ ] **Step 2: Escribir los tests que fallan**

En `core/tests.py`, agregar (usa `from unittest.mock import patch` y `requests`, ya importados arriba):

```python
from django.test import override_settings


class FetchFromApiTest(TestCase):
    def _resp(self, json_data, status=200):
        m = MagicMock(status_code=status)
        m.json.return_value = json_data
        m.raise_for_status.return_value = None
        return m

    @patch("core.ruc.requests.get")
    def test_positivo_devuelve_razon_social(self, mock_get):
        from core.ruc import _fetch_from_api

        mock_get.return_value = self._resp(
            {"data": {"razonSocial": "COMERCIO Y FINANZAS SA"}, "message": "OK"}
        )
        self.assertEqual(_fetch_from_api("80012345-6"), "COMERCIO Y FINANZAS SA")

    @patch("core.ruc.requests.get")
    def test_sin_match_devuelve_none(self, mock_get):
        from core.ruc import _fetch_from_api

        mock_get.return_value = self._resp({"data": {}, "message": "OK"})
        self.assertIsNone(_fetch_from_api("80012345-6"))

    @patch("core.ruc.requests.get")
    def test_error_de_red_devuelve_none(self, mock_get):
        from core.ruc import _fetch_from_api

        mock_get.side_effect = requests.RequestException("timeout")
        self.assertIsNone(_fetch_from_api("80012345-6"))

    @override_settings(RUC_API_URL=None)
    @patch("core.ruc.requests.get")
    def test_no_configurado_no_hace_request(self, mock_get):
        from core.ruc import _fetch_from_api

        self.assertIsNone(_fetch_from_api("80012345-6"))
        mock_get.assert_not_called()

    @patch("core.ruc.requests.get")
    def test_json_malformado_devuelve_none(self, mock_get):
        from core.ruc import _fetch_from_api

        m = MagicMock(status_code=200)
        m.json.side_effect = ValueError("no JSON")
        m.raise_for_status.return_value = None
        mock_get.return_value = m
        self.assertIsNone(_fetch_from_api("80012345-6"))
```

- [ ] **Step 3: Correr los tests para verificar que fallan**

Run: `.venv/bin/python manage.py test core.tests.FetchFromApiTest --settings=muci-integrador.test_settings`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ruc'`.

- [ ] **Step 4: Crear el módulo con la capa HTTP**

Create `core/ruc.py`:

```python
import logging
from typing import Optional

import requests
from django.conf import settings

logger = logging.getLogger("ruc_api")

CACHE_TTL_DAYS = 30


def _fetch_from_api(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Pega a la fuente externa (turuc). Devuelve la razón social si responde
    positivo; None ante fuente no configurada, error de red/timeout, JSON
    inválido, sin match o razón social vacía. Nunca lanza hacia el caller.
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

- [ ] **Step 5: Correr los tests para verificar que pasan**

Run: `.venv/bin/python manage.py test core.tests.FetchFromApiTest --settings=muci-integrador.test_settings`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add muci-integrador/settings.py muci-integrador/test_settings.py core/ruc.py core/tests.py
git commit -m "feat: capa HTTP _fetch_from_api para consulta de RUC en turuc"
```

---

### Task 3: Lógica de caché `get_razon_social`

**Files:**
- Modify: `core/ruc.py` (agregar `get_razon_social`)
- Test: `core/tests.py` (clase `GetRazonSocialTest`)

**Interfaces:**
- Consumes: `core.ruc._fetch_from_api`, `core.models.RucCache`, `core.ruc.CACHE_TTL_DAYS`.
- Produces: `core.ruc.get_razon_social(ruc: str, timeout: int = 5) -> Optional[str]`.

- [ ] **Step 1: Escribir los tests que fallan**

En `core/tests.py`, agregar:

```python
class GetRazonSocialTest(TestCase):
    def _make_cache(self, ruc, razon, dias_atras):
        from django.utils.timezone import now
        from datetime import timedelta

        return RucCache.objects.create(
            ruc=ruc, razon_social=razon, checked_at=now() - timedelta(days=dias_atras)
        )

    @patch("core.ruc._fetch_from_api")
    def test_cache_fresco_no_llama_api(self, mock_fetch):
        from core.ruc import get_razon_social

        self._make_cache("80012345-6", "EMPRESA FRESCA SA", dias_atras=5)
        self.assertEqual(get_razon_social("80012345-6"), "EMPRESA FRESCA SA")
        mock_fetch.assert_not_called()

    @patch("core.ruc._fetch_from_api")
    def test_cache_vencido_api_ok_actualiza(self, mock_fetch):
        from core.ruc import get_razon_social
        from django.utils.timezone import now
        from datetime import timedelta

        self._make_cache("80012345-6", "NOMBRE VIEJO SA", dias_atras=40)
        mock_fetch.return_value = "NOMBRE NUEVO SA"

        self.assertEqual(get_razon_social("80012345-6"), "NOMBRE NUEVO SA")
        row = RucCache.objects.get(ruc="80012345-6")
        self.assertEqual(row.razon_social, "NOMBRE NUEVO SA")
        self.assertGreater(row.checked_at, now() - timedelta(minutes=1))

    @patch("core.ruc._fetch_from_api")
    def test_cache_vencido_api_falla_usa_viejo_sin_renovar(self, mock_fetch):
        from core.ruc import get_razon_social

        row = self._make_cache("80012345-6", "NOMBRE VIEJO SA", dias_atras=40)
        viejo_checked_at = row.checked_at
        mock_fetch.return_value = None

        self.assertEqual(get_razon_social("80012345-6"), "NOMBRE VIEJO SA")
        self.assertEqual(RucCache.objects.get(ruc="80012345-6").checked_at, viejo_checked_at)

    @patch("core.ruc._fetch_from_api")
    def test_sin_cache_api_ok_crea_fila(self, mock_fetch):
        from core.ruc import get_razon_social

        mock_fetch.return_value = "EMPRESA NUEVA SA"
        self.assertEqual(get_razon_social("80012345-6"), "EMPRESA NUEVA SA")
        self.assertTrue(RucCache.objects.filter(ruc="80012345-6").exists())

    @patch("core.ruc._fetch_from_api")
    def test_sin_cache_api_falla_devuelve_none(self, mock_fetch):
        from core.ruc import get_razon_social

        mock_fetch.return_value = None
        self.assertIsNone(get_razon_social("80012345-6"))
        self.assertFalse(RucCache.objects.filter(ruc="80012345-6").exists())
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `.venv/bin/python manage.py test core.tests.GetRazonSocialTest --settings=muci-integrador.test_settings`
Expected: FAIL — `ImportError: cannot import name 'get_razon_social'`.

- [ ] **Step 3: Implementar `get_razon_social`**

En `core/ruc.py`, agregar las importaciones necesarias arriba (`from datetime import timedelta`, `from django.utils.timezone import now`, `from core.models import RucCache`) y la función:

```python
def get_razon_social(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Resuelve la razón social de un RUC usando RucCache (TTL 30 días) y, si hace
    falta, la fuente externa.

    - Caché fresco (<30 días): devuelve el valor cacheado, sin llamar a la API.
    - Caché vencido/ausente + API ok: devuelve el valor nuevo y refresca checked_at.
    - Caché vencido + API falla: devuelve el valor viejo SIN renovar checked_at.
    - Sin caché + API falla: devuelve None (el caller cae a WooCommerce).
    """
    if not ruc:
        return None

    cached = RucCache.objects.filter(ruc=ruc).first()

    if cached and (now() - cached.checked_at) < timedelta(days=CACHE_TTL_DAYS):
        return cached.razon_social

    fetched = _fetch_from_api(ruc, timeout)
    if fetched:
        RucCache.objects.update_or_create(
            ruc=ruc, defaults={"razon_social": fetched, "checked_at": now()}
        )
        return fetched

    if cached:
        return cached.razon_social
    return None
```

Mover el bloque de `import` al tope del archivo (junto a los existentes), respetando el orden: stdlib (`logging`, `datetime`) → terceros (`requests`, `django`) → locales (`core.models`).

- [ ] **Step 4: Correr los tests para verificar que pasan**

Run: `.venv/bin/python manage.py test core.tests.GetRazonSocialTest --settings=muci-integrador.test_settings`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add core/ruc.py core/tests.py
git commit -m "feat: get_razon_social con caché RucCache (TTL 30d) y fallback"
```

---

### Task 4: Integración en `resolve_contact_id`

**Files:**
- Modify: `core/services.py` (import + bloque después de la línea 196)
- Test: `core/tests.py` (clase `ResolveContactRucEnrichmentTest`)

**Interfaces:**
- Consumes: `core.ruc.get_razon_social`.
- Produces: efecto observable — cuando `document_type == "ruc"` y la fuente responde positivo, `resolve_contact_id` arma el contacto con la razón social autoritativa.

- [ ] **Step 1: Escribir el test que falla**

En `core/tests.py`, agregar. El test verifica que `resolve_contact_id` consulta el RUC y usa el nombre autoritativo. Se mockea `bims` (para no pegarle a BIMS) y `core.ruc.get_razon_social`.

```python
class ResolveContactRucEnrichmentTest(TestCase):
    def _meta(self, ruc, razon_social):
        return [
            {"key": "_billing_ruc", "value": ruc},
            {"key": "_billing_razon_social", "value": razon_social},
        ]

    @patch("core.services.bims")
    @patch("core.services.get_razon_social")
    def test_ruc_con_fuente_positiva_usa_nombre_autoritativo(self, mock_ruc, mock_bims):
        from core.services import resolve_contact_id

        mock_ruc.return_value = "RAZON SOCIAL OFICIAL SA"
        mock_bims.find_contact.return_value = None
        mock_bims.create_contact.return_value = 999

        resolve_contact_id(
            order_id=1,
            meta_data=self._meta("80012345-6", "nombre mal escrito"),
            billing={"first_name": "Juan", "last_name": "Pérez", "email": "j@x.com"},
            shipping={},
            is_pos=False,
        )

        mock_ruc.assert_called_once_with("80012345-6")
        # el name autoritativo llega a create_contact
        _, kwargs = mock_bims.create_contact.call_args
        self.assertEqual(kwargs["name"], "RAZON SOCIAL OFICIAL SA")

    @patch("core.services.bims")
    @patch("core.services.get_razon_social")
    def test_ci_no_consulta_la_fuente(self, mock_ruc, mock_bims):
        from core.services import resolve_contact_id

        mock_bims.find_contact.return_value = None
        mock_bims.create_contact.return_value = 999

        resolve_contact_id(
            order_id=2,
            meta_data=[{"key": "_billing_documento", "value": "1234567"}],
            billing={"first_name": "Ana", "last_name": "López", "email": "a@x.com"},
            shipping={},
            is_pos=False,
        )

        mock_ruc.assert_not_called()
```

> **Nota para el implementador:** Si la firma real de `create_contact` pasa `name` como argumento posicional, ajustá la aserción para leerlo de `args` en vez de `kwargs`. Verificá la firma en `core/bims.py` antes de correr (método `create_contact`). El objetivo del test es: el nombre que llega a BIMS es el autoritativo.

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.ResolveContactRucEnrichmentTest --settings=muci-integrador.test_settings`
Expected: FAIL — `ImportError: cannot import name 'get_razon_social'` desde `core.services` (todavía no se importó) o el `name` no coincide.

- [ ] **Step 3: Cablear la consulta en `services.py`**

En `core/services.py`, agregar el import (junto a la línea 9, `from core.bims import bims, BimsBusinessError`):

```python
from core.ruc import get_razon_social
```

Y en la rama no-POS de `resolve_contact_id`, justo después de la línea que arma `name` (línea ~196, `name = _camel_to_spaces(...) if social_reason_value else ...`), agregar:

```python
        # Corrección autoritativa: si hay RUC, la razón social del padrón manda.
        if document_type == "ruc":
            authoritative = get_razon_social(document_id)
            if authoritative:
                logger.info(
                    f"Order {order_id}: Razón social corregida por RUC {document_id}: "
                    f"'{name}' -> '{authoritative}'"
                )
                name = authoritative
```

(Respetar la indentación: este bloque va dentro del `else` no-POS, al mismo nivel que la asignación de `name`.)

- [ ] **Step 4: Correr el test para verificar que pasa**

Run: `.venv/bin/python manage.py test core.tests.ResolveContactRucEnrichmentTest --settings=muci-integrador.test_settings`
Expected: PASS (2 tests).

- [ ] **Step 5: Correr TODA la suite**

Run: `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`
Expected: PASS — 22 (previos) + 1 (modelo) + 5 (_fetch) + 5 (get_razon_social) + 2 (integración) = **35 tests**, salida limpia.

- [ ] **Step 6: Commit**

```bash
git add core/services.py core/tests.py
git commit -m "feat: usar razón social autoritativa por RUC en resolve_contact_id"
```

---

## Notas de verificación final

- Confirmar que no se introdujo sintaxis incompatible con Python 3.7 (sin `X | Y`, sin `match`).
- `find_razon_social_by_ruc` en `core/bims.py` debe quedar sin cambios.
- Opcional: probar el endpoint real con `! curl https://turuc.com.py/api/contribuyente/<un-ruc-real>` antes de subir a producción, y configurar `RUC_API_URL` en el `.env` del servidor (o dejar el default `https://turuc.com.py`).
</content>
