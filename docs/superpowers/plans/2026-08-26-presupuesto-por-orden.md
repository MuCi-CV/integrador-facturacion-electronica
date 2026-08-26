# Presupuesto por orden — Plan de implementación

> **Para trabajadores agénticos:** SUB-SKILL REQUERIDA: usar
> `superpowers:subagent-driven-development` (recomendada) o
> `superpowers:executing-plans` para implementar este plan tarea por tarea. Los
> pasos usan sintaxis de checkbox (`- [ ]`) para el seguimiento.

**Objetivo:** que ninguna orden pueda superar el `--timeout 120` de gunicorn, de modo que
un fallo por lentitud siempre termine en un `FailedOrder` grabado y reintentable, nunca en
una orden que desaparece sin rastro.

**Arquitectura:** un `contextvars.ContextVar` en un módulo nuevo `core/deadline.py` lleva el
límite de tiempo de la orden. `process_order` es el único que lo fija, con `try/finally`.
`bims.py` y `woocommerce.py` lo consultan y recortan sus timeouts al mínimo entre su propio
límite y el restante de la orden. Quien no fija presupuesto (el cron `sync_bims_contacts`)
recibe `None` y se comporta exactamente como hoy.

**Stack:** Python 3.7.17 + Django 3.2.25 (producción), `requests`, librería `woocommerce`.
Tests con `unittest.mock` y reloj falso, interceptando `HTTPAdapter.send`.

**Spec:** `docs/superpowers/specs/2026-08-25-presupuesto-por-orden-design.md`

## Restricciones globales

- **Python 3.7.17 es el piso.** Producción corre 3.7.17 + Django 3.2.25; local corre 3.12 +
  Django 6.0.3. Nada de walrus en comprehensions, `dict |` merge, `match`, ni tipos
  genéricos nativos (`list[int]`). Usar `typing.Optional`, `typing.List`.
  `contextvars` sí existe desde 3.7 — por eso el diseño es viable.
- **Type hints obligatorios** en toda función nueva (regla del `CLAUDE.md`).
- **Comentarios y docstrings en español**, siguiendo el estilo del archivo que se toca.
- **No tocar `core/management/commands/sync_bims_contacts.py`.** Que siga funcionando sin
  cambios es un criterio de éxito, no un efecto colateral.
- **No cambiar la lógica de facturación ni los payloads a BIMS.**
- **No subir el `--timeout` de gunicorn.**
- Valores exactos, copiados de la spec: `PRESUPUESTO_ORDEN = 90`,
  `PRESUPUESTO_REINTENTOS = 40` (se mantiene), `TIMEOUT_LECTURA = 30`,
  `TIMEOUT_CONEXION = 5`, `TIMEOUT_WOOCOMMERCE = 30`, gunicorn `--timeout 120`.
- **Comando de tests:**
  `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`
- Los tests nuevos van en `core/tests.py` (el proyecto tiene un solo archivo de tests).

## Estructura de archivos

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `core/deadline.py` | **Nuevo.** El reloj de la orden: `iniciar`, `restaurar`, `restante`, `PresupuestoOrdenAgotado`, `PRESUPUESTO_ORDEN`. Sin dependencias del proyecto. | 1 |
| `core/bims.py` | Consumidor. `_retry_loop` recorta al restante de la orden; `_retry_request` deja de conmutar de host con el presupuesto agotado. | 2 |
| `core/woocommerce.py` | Consumidor. `_timeout_efectivo()` ajusta `wcapi.timeout` por llamada. | 3 |
| `core/services.py` | Único productor: `process_order` fija el presupuesto. Y se cierra la brecha del `FailedOrder` en `build_sale_products`. | 4 |
| `core/tests.py` | Tests de las cuatro tareas. | 1–4 |

`core/deadline.py` va aparte y no dentro de `bims.py` porque `woocommerce.py` también lo
necesita, y que `woocommerce` importe de `bims` sería una dependencia al revés.

## Brecha encontrada al leer el código (2026-08-26)

La spec dice que `process_order` "ya tiene `except Exception as e:` que graba el
`FailedOrder`". **Es impreciso y la diferencia importa.** `process_order` tiene *cuatro*
`try/except` separados —`get_order` (479), `resolve_contact_id` (509), `create_sale` (552) y
el chequeo de `sale_id` (567)— y la llamada a `build_sale_products` (**`services.py:523-528`**)
**no está envuelta en ninguno**. Dentro de `build_sale_products` (`services.py:373`) está el
bucle `for item in line_items:` con `wc_api.get_product(search_id)`.

Consecuencia: un `PresupuestoOrdenAgotado` lanzado desde `get_product` **se escapa de
`process_order` sin grabar `FailedOrder`** — justo el tramo que la spec identifica como el más
riesgoso por escalar con la cantidad de ítems. La garantía central de la spec no se cumpliría
sin cerrar esto. Se cubre en la **Tarea 4, Pasos 6–10**.

(Nota: esto ya es un bug hoy, independiente de esta feature — una `ServerException` de
WooCommerce en el medio del bucle también se pierde sin registro.)

---

### Tarea 1: El módulo del reloj (`core/deadline.py`)

**Archivos:**
- Crear: `core/deadline.py`
- Test: `core/tests.py` (clase nueva `DeadlineOrdenTest`, al final del archivo)

**Interfaces:**
- Consume: nada del proyecto. Solo `contextvars`, `time`, `typing`.
- Produce (lo usan las tareas 2, 3 y 4):
  - `PRESUPUESTO_ORDEN: int = 90`
  - `iniciar(presupuesto: float = PRESUPUESTO_ORDEN) -> contextvars.Token`
  - `restaurar(token: contextvars.Token) -> None`
  - `restante() -> Optional[float]` — segundos restantes, o `None` si nadie fijó presupuesto
  - `class PresupuestoOrdenAgotado(Exception)` — **hereda de `Exception` directo**, NO de
    `BimsError` ni de `BimsTransientError`. Es lo que le permite pasar por encima del
    `except BimsTransientError` de `_retry_request` sin que el reintento se la coma.

- [ ] **Paso 1: Escribir los tests que fallan**

Agregar al final de `core/tests.py`:

```python
class DeadlineOrdenTest(TestCase):
    """
    El reloj por orden. `restante()` devolviendo None es la pieza central del
    diseño: "sin presupuesto" es un estado explícito y legítimo, y es lo que
    hace que `sync_bims_contacts` siga funcionando sin tocar ese archivo.
    """

    def _reloj(self):
        """Reloj falso: devuelve (monotonic, avanzar)."""
        estado = {"t": 1000.0}

        def monotonic():
            return estado["t"]

        def avanzar(segundos):
            estado["t"] += segundos

        return monotonic, avanzar

    def test_sin_iniciar_no_hay_presupuesto(self):
        self.assertIsNone(deadline.restante())

    def test_iniciar_fija_el_presupuesto_completo(self):
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar()
            try:
                self.assertAlmostEqual(deadline.restante(), deadline.PRESUPUESTO_ORDEN)
            finally:
                deadline.restaurar(token)

    def test_el_restante_baja_con_el_reloj(self):
        monotonic, avanzar = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar()
            try:
                avanzar(30)
                self.assertAlmostEqual(deadline.restante(), deadline.PRESUPUESTO_ORDEN - 30)
            finally:
                deadline.restaurar(token)

    def test_el_restante_puede_ser_negativo(self):
        """No se recorta a 0: el consumidor distingue 'agotado' de 'sin presupuesto'."""
        monotonic, avanzar = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar()
            try:
                avanzar(deadline.PRESUPUESTO_ORDEN + 5)
                self.assertLess(deadline.restante(), 0)
            finally:
                deadline.restaurar(token)

    def test_restaurar_vuelve_a_sin_presupuesto(self):
        token = deadline.iniciar()
        deadline.restaurar(token)
        self.assertIsNone(deadline.restante())

    def test_acepta_un_presupuesto_explicito(self):
        """Los tests de las otras tareas fijan presupuestos chicos con esto."""
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(10)
            try:
                self.assertAlmostEqual(deadline.restante(), 10)
            finally:
                deadline.restaurar(token)

    def test_deja_margen_bajo_el_timeout_de_gunicorn(self):
        """30 s de margen para grabar el FailedOrder y responder."""
        self.assertLessEqual(deadline.PRESUPUESTO_ORDEN, 90)
        self.assertGreaterEqual(120 - deadline.PRESUPUESTO_ORDEN, 30)

    def test_la_excepcion_no_es_un_error_de_bims(self):
        """
        Si heredara de BimsTransientError, el `except` de `_retry_request` se la
        comería y volvería a intentar sin tiempo disponible.
        """
        self.assertTrue(issubclass(deadline.PresupuestoOrdenAgotado, Exception))
        self.assertFalse(issubclass(deadline.PresupuestoOrdenAgotado, BimsError))
```

Y agregar el import en la cabecera de `core/tests.py`, junto a los otros `from core...`
(después de la línea `from core.woocommerce import TIMEOUT_WOOCOMMERCE, WooCommerceAPI`):

```python
from core import deadline
```

Y **agregar `BimsError`** al bloque `from core.bims import (...)` de la línea 16 (verificado
el 2026-08-26: existe en `core/bims.py:83` pero **no** está importado en `tests.py`). El
bloque queda:

```python
    from core.bims import (
        BimsApi,
        BimsBusinessError,
        BimsError,
        BimsTransientError,
        PRESUPUESTO_REINTENTOS,
        TIMEOUT_CONEXION,
        TIMEOUT_LECTURA,
    )
```

Ese bloque vive **dentro** del `with patch("requests.post")` de la cabecera. El
`from core import deadline` va **afuera**, con los demás: `core/deadline.py` no tiene efectos
de import.

- [ ] **Paso 2: Correr los tests y verificar que fallan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.DeadlineOrdenTest --settings=muci-integrador.test_settings -v 2
```
Esperado: FALLA con `ImportError: cannot import name 'deadline'` (o
`ModuleNotFoundError: No module named 'core.deadline'`).

- [ ] **Paso 3: Crear `core/deadline.py`**

```python
"""
Presupuesto de tiempo por orden, transportado con un `contextvars.ContextVar`.

gunicorn corre con `--timeout 120` y mata al worker **por señal** al pasarse. Un
worker matado por señal no ejecuta el `except` que graba el `FailedOrder`, así
que la orden desaparece sin factura y sin registro. El presupuesto por llamada de
`bims.PRESUPUESTO_REINTENTOS` no alcanza: una orden hace 2-3 llamadas a BIMS más
un `get_product` por ítem, y nadie mira la suma.

Se usa un ContextVar y no un parámetro por firma porque un call site nuevo que se
olvide de pasarlo quedaría sin límite **en silencio** — el mismo tipo de falla que
esto viene a arreglar. Y no un atributo del singleton `bims` porque sería estado
mutable global sobre un objeto compartido, que se rompe sin aviso el día que algo
sea concurrente.
"""

import contextvars
import time
from typing import Optional

# 90 s deja 30 s de margen contra los --timeout 120 de gunicorn: suficiente para
# grabar el FailedOrder y responder. Una orden normal tarda 10-20 s, así que en
# operación sana esto no debería activarse nunca.
PRESUPUESTO_ORDEN = 90

_deadline = contextvars.ContextVar("deadline_orden", default=None)


class PresupuestoOrdenAgotado(Exception):
    """
    La orden superó su presupuesto total. Terminal para esta corrida, reintentable
    desde el admin.

    Hereda de `Exception` a propósito, NO de `BimsTransientError`: tiene que pasar
    por encima del `except BimsTransientError` de `_retry_request`, o el reintento
    se la comería justo cuando ya no queda tiempo para reintentar.
    """


def iniciar(presupuesto: float = PRESUPUESTO_ORDEN) -> contextvars.Token:
    """Arranca el reloj. Devuelve un token para restaurar en un `finally`."""
    return _deadline.set(time.monotonic() + presupuesto)


def restaurar(token: contextvars.Token) -> None:
    """Deshace un `iniciar()`. Va siempre en un `finally`."""
    _deadline.reset(token)


def restante() -> Optional[float]:
    """
    Segundos que quedan, o `None` si no hay presupuesto fijado en este contexto.

    El `None` es deliberado y es lo que protege al cron `sync_bims_contacts`, que
    hace 38 llamadas secuenciales y nunca debe tener deadline de orden. No se
    recorta a 0: el consumidor necesita distinguir "agotado" (negativo) de "sin
    presupuesto" (None).
    """
    limite = _deadline.get()
    return None if limite is None else limite - time.monotonic()
```

- [ ] **Paso 4: Correr los tests y verificar que pasan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.DeadlineOrdenTest --settings=muci-integrador.test_settings -v 2
```
Esperado: PASA, 8 tests.

- [ ] **Paso 5: Correr la suite completa (nada debe romperse)**

Ejecutar:
```bash
.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings
```
Esperado: 130 tests OK (122 previos + 8 nuevos).

- [ ] **Paso 6: Commit**

```bash
git add core/deadline.py core/tests.py
git commit -m "feat(deadline): modulo del presupuesto por orden con ContextVar"
```

---

### Tarea 2: Integrar el presupuesto en `bims.py`

**Archivos:**
- Modificar: `core/bims.py` — imports (cabecera), `_retry_request` (313-331), `_retry_loop` (333-344)
- Test: `core/tests.py` (clase nueva `PresupuestoOrdenBimsTest`)

**Interfaces:**
- Consume de la Tarea 1: `deadline.restante()`, `deadline.PresupuestoOrdenAgotado`,
  `deadline.PRESUPUESTO_ORDEN`, `deadline.iniciar()`, `deadline.restaurar()`.
- Produce: `_retry_loop` pasa a lanzar `deadline.PresupuestoOrdenAgotado` cuando el que se
  agota es el presupuesto **de la orden** (y `BimsTransientError` cuando es el de la
  llamada, como hoy). La firma de `_retry_loop` y `_retry_request` **no cambia**.

**Ojo con el reloj en los tests:** `bims.py` usa `core.bims.time.monotonic` y `deadline.py`
usa `core.deadline.time.monotonic`. Los tests tienen que parchear **los dos** con el mismo
reloj falso, o el presupuesto de la orden no se moverá.

- [ ] **Paso 1: Escribir los tests que fallan**

Agregar a `core/tests.py`, después de `TimeoutsBimsTest`:

```python
class PresupuestoOrdenBimsTest(TestCase):
    """
    El presupuesto de la orden se impone por encima del de cada llamada.

    Hoy `PRESUPUESTO_REINTENTOS` es por LLAMADA: una orden hace 2-3 llamadas y
    3 x 41 s = 123 s > los 120 s de gunicorn. Al pasarse, gunicorn mata al worker
    por señal y el `FailedOrder` no se graba.
    """

    _CONTACTO_OK = {"status": "ok", "count": 1, "data": [{"Contact": {"id": "7"}}]}

    def _api(self):
        with patch.object(BimsApi, "login", return_value="sid"):
            return BimsApi()

    def _reloj_y_send(self, capturados, paso=15.0, responder_ok=False):
        """
        Reloj falso compartido por core.bims y core.deadline.

        Devuelve (monotonic, dormir, fake_send). Cada envío consume `paso`
        segundos; si `responder_ok`, responde 200 en vez de fallar por red.
        """
        reloj = {"t": 1000.0}

        def monotonic():
            return reloj["t"]

        def dormir(segundos):
            reloj["t"] += segundos

        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            reloj["t"] += paso
            if responder_ok:
                respuesta = requests.Response()
                respuesta.status_code = 200
                respuesta._content = json.dumps(self._CONTACTO_OK).encode("utf-8")
                respuesta.request = request
                return respuesta
            raise requests.ConnectionError("BIMS no responde")

        return monotonic, dormir, fake_send

    # ── Sin presupuesto: nada cambia ────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_sin_presupuesto_de_orden_el_timeout_es_el_de_siempre(self):
        """
        Protege a `sync_bims_contacts`: 38 llamadas secuenciales sin deadline.
        """
        api = self._api()
        capturados = []
        with patch.object(requests.adapters.HTTPAdapter, "send", self._send_ok_simple(capturados)):
            api.list_contacts("123456", "CI")
        self.assertEqual(capturados, [(TIMEOUT_CONEXION, TIMEOUT_LECTURA)])

    def _send_ok_simple(self, capturados):
        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            respuesta = requests.Response()
            respuesta.status_code = 200
            respuesta._content = json.dumps(self._CONTACTO_OK).encode("utf-8")
            respuesta.request = request
            return respuesta

        return fake_send

    # ── Con presupuesto: se recorta ─────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_timeout_se_recorta_al_restante_de_la_orden(self):
        """
        Con 8 s de orden restantes, la lectura no puede ser de 30 s aunque el
        presupuesto de la llamada tenga 40 s enteros.
        """
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados, responder_ok=True)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(8)
            try:
                api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        self.assertEqual(capturados, [(5, 8)])

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_timeout_de_conexion_tambien_se_recorta(self):
        """
        Hallazgo 2 de la revisión: hoy solo se recorta la lectura, así que un
        intento que arranca al límite puede excederse hasta 5 s por la conexión.
        Con el presupuesto de orden en juego ese exceso deja de ser cosmético.
        """
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados, responder_ok=True)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(3)
            try:
                api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        self.assertEqual(capturados, [(3, 3)])

    # ── Agotado: corta seco ─────────────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_presupuesto_agotado_lanza_la_excepcion_propia(self):
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(10)
            try:
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        # Un solo intento: consume los 15 s del envío más 2 de espera, así que el
        # segundo arranca con el presupuesto de orden (10 s) ya en negativo. El de
        # la llamada todavía tendría 23 s — la orden es la que corta.
        self.assertEqual(len(capturados), 1)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_presupuesto_agotado_no_conmuta_de_host(self):
        """No queda tiempo para probar el otro host."""
        api = self._api()
        api.fallback_url = "http://otro.bims.test.local"
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(10)
            try:
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        # Con fallback configurada seguiría habiendo un solo envío: agotado el
        # presupuesto de la orden no se prueba el otro host.
        self.assertEqual(len(capturados), 1)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_error_conserva_la_causa_real(self):
        """
        Hallazgo 4: al agotarse el presupuesto de la LLAMADA con fallback
        configurada, se reentraba al loop con el mismo límite y el segundo loop
        fallaba de inmediato con `Last error: None`, perdiendo la causa.
        """
        api = self._api()
        api.fallback_url = "http://otro.bims.test.local"
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            with self.assertRaises(BimsTransientError) as ctx:
                api.list_contacts("123456", "CI")
        self.assertNotIn("Last error: None", str(ctx.exception))
        self.assertIn("BIMS no responde", str(ctx.exception))

    # ── La garantía agregada ────────────────────────────────────────────────

    def test_tres_llamadas_no_superan_el_presupuesto_de_la_orden(self):
        """
        El cálculo que motiva la spec: 3 x 41 s = 123 s > 120 de gunicorn. Con el
        presupuesto de orden, el techo agregado es PRESUPUESTO_ORDEN, no 3 x 41.
        """
        self.assertLess(deadline.PRESUPUESTO_ORDEN, 120)
        self.assertGreater(PRESUPUESTO_REINTENTOS * 3, deadline.PRESUPUESTO_ORDEN)
```

- [ ] **Paso 2: Correr los tests y verificar que fallan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenBimsTest --settings=muci-integrador.test_settings -v 2
```
Esperado: FALLAN los que dependen del recorte y de la excepción nueva
(`test_el_timeout_se_recorta_al_restante_de_la_orden`,
`test_el_timeout_de_conexion_tambien_se_recorta`,
`test_presupuesto_agotado_lanza_la_excepcion_propia`,
`test_presupuesto_agotado_no_conmuta_de_host`,
`test_el_error_conserva_la_causa_real`). Los otros dos ya pasan — son los que
garantizan que no rompimos lo que había.

- [ ] **Paso 3: Agregar el import en `core/bims.py`**

En la cabecera, junto a los demás imports locales del proyecto:

```python
from core import deadline
```

`core/deadline.py` no importa nada de `core`, así que no hay ciclo.

- [ ] **Paso 4: Reescribir el arranque de `_retry_loop`**

En `core/bims.py`, reemplazar las líneas 336-344 (desde `restante = limite - time.monotonic()`
hasta el `kwargs["timeout"] = ...`) por:

```python
            restante = limite - time.monotonic()
            # El presupuesto de la ORDEN manda por encima del de esta llamada: es
            # el que gunicorn realmente mide. Se chequea primero porque agotarlo
            # es terminal — no se reintenta ni se conmuta de host.
            restante_orden = deadline.restante()
            if restante_orden is not None and restante_orden <= 0:
                raise deadline.PresupuestoOrdenAgotado(
                    f"Presupuesto de orden de {deadline.PRESUPUESTO_ORDEN}s agotado "
                    f"para {url} tras {attempt - 1} intentos. "
                    f"Last error: {last_error_details}"
                )
            if restante <= 0:
                raise BimsTransientError(
                    f"Presupuesto de {PRESUPUESTO_REINTENTOS}s agotado para {url} "
                    f"tras {attempt - 1} intentos. Last error: {last_error_details}"
                )
            if restante_orden is not None:
                restante = min(restante, restante_orden)
            # Recortar al restante: sin esto el presupuesto sería decorativo,
            # porque un intento que arranca al límite correría entero. Se recorta
            # también la CONEXIÓN (hallazgo 2): con el presupuesto de orden en
            # juego, los hasta 5 s de exceso dejan de ser cosméticos.
            kwargs["timeout"] = (
                min(TIMEOUT_CONEXION, restante),
                min(TIMEOUT_LECTURA, restante),
            )
```

- [ ] **Paso 5: Evitar la conmutación de host sin tiempo (hallazgo 4)**

En `core/bims.py`, en `_retry_request`, reemplazar las líneas 319-322:

```python
        except BimsTransientError:
            alternate_url = self._alternate_base_url(url)
            if alternate_url is None:
                raise
```

por:

```python
        except BimsTransientError:
            alternate_url = self._alternate_base_url(url)
            # Si el presupuesto ya se agotó, reentrar al loop con el mismo límite
            # hacía que el segundo loop fallara de inmediato con
            # `Last error: None`, tapando la causa real (hallazgo 4). Y no tiene
            # sentido: no queda tiempo para probar el otro host.
            if alternate_url is None or time.monotonic() >= limite:
                raise
```

`PresupuestoOrdenAgotado` no hereda de `BimsTransientError`, así que atraviesa este `except`
sin tocarlo: agotado el presupuesto de la orden, nunca se conmuta.

- [ ] **Paso 6: Correr los tests y verificar que pasan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenBimsTest --settings=muci-integrador.test_settings -v 2
```
Esperado: PASA, 7 tests.

- [ ] **Paso 7: Correr `TimeoutsBimsTest` — no debe haber regresiones**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.TimeoutsBimsTest --settings=muci-integrador.test_settings -v 2
```
Esperado: PASA, 6 tests. En particular
`test_el_timeout_de_lectura_se_recorta_al_presupuesto_restante` espera
`[(5, 30), (5, 23), (5, 6)]` y debe **seguir dando eso**: sin presupuesto de orden,
`min(TIMEOUT_CONEXION, restante)` da 5 en las tres vueltas porque el restante nunca baja de 5.

Si ese test falla en la última tupla, revisar: significa que el recorte de conexión se aplicó
donde no correspondía.

- [ ] **Paso 8: Correr la suite completa**

Ejecutar:
```bash
.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings
```
Esperado: 137 tests OK.

- [ ] **Paso 9: Commit**

```bash
git add core/bims.py core/tests.py
git commit -m "feat(bims): respetar el presupuesto de la orden y recortar el connect timeout"
```

---

### Tarea 3: Integrar el presupuesto en `woocommerce.py`

**Archivos:**
- Modificar: `core/woocommerce.py` — imports, método nuevo `_timeout_efectivo`, y los cinco
  métodos que se usan dentro de una orden
- Test: `core/tests.py` (clase nueva `PresupuestoOrdenWooCommerceTest`)

**Interfaces:**
- Consume de la Tarea 1: `deadline.restante()`, `deadline.PresupuestoOrdenAgotado`.
- Produce: `WooCommerceAPI._timeout_efectivo() -> float`. Los métodos `get_order`,
  `get_product`, `get_customer`, `find_customer_by_email` y `refund_order` ajustan
  `self.wcapi.timeout` antes de cada request. Las firmas públicas **no cambian**.

**Por qué se puede ajustar por llamada:** la librería `woocommerce.API` guarda `self.timeout`
como atributo de instancia y lo lee en cada request (verificado el 2026-08-25), así que
asignarlo antes de cada llamada surte efecto.

**Sobre `refund_order`:** la spec lo marca como riesgo ("si se corta por presupuesto a mitad,
verificar que no quede un reembolso a medias"). Verificado el 2026-08-26: `refund_order` se
llama **solo desde `core/views.py:60`**, fuera de `process_order`. Como el deadline se fija
únicamente en `process_order`, ahí `restante()` devuelve `None` y el timeout es el de siempre.
El riesgo es inerte hoy. Se le aplica igual por consistencia: si algún día se llama dentro de
una orden, debe respetar el presupuesto.

- [ ] **Paso 1: Escribir los tests que fallan**

Agregar a `core/tests.py`, después de `TimeoutWooCommerceTest`:

```python
class PresupuestoOrdenWooCommerceTest(TestCase):
    """
    WooCommerce también consume presupuesto de la orden.

    `get_product` se llama una vez por ítem, así que este tramo escala con la
    cantidad de ítems y hoy no tiene ningún techo agregado: una orden de 5 ítems
    puede gastar 5 x 30 = 150 s antes de tocar BIMS.
    """

    def _reloj(self):
        estado = {"t": 1000.0}

        def monotonic():
            return estado["t"]

        def avanzar(segundos):
            estado["t"] += segundos

        return monotonic, avanzar

    def test_sin_presupuesto_usa_el_timeout_completo(self):
        """Protege a cualquier uso fuera de una orden."""
        api = WooCommerceAPI()
        self.assertEqual(api._timeout_efectivo(), TIMEOUT_WOOCOMMERCE)

    def test_con_presupuesto_amplio_usa_el_timeout_completo(self):
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(90)
            try:
                self.assertEqual(api._timeout_efectivo(), TIMEOUT_WOOCOMMERCE)
            finally:
                deadline.restaurar(token)

    def test_con_presupuesto_corto_se_recorta(self):
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(7)
            try:
                self.assertEqual(api._timeout_efectivo(), 7)
            finally:
                deadline.restaurar(token)

    def test_con_presupuesto_agotado_lanza_sin_pegarle_a_woocommerce(self):
        api = WooCommerceAPI()
        monotonic, avanzar = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(5)
            try:
                avanzar(6)
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    api._timeout_efectivo()
            finally:
                deadline.restaurar(token)

    def test_get_product_aplica_el_timeout_recortado(self):
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        respuesta = MagicMock()
        respuesta.status_code = 200
        respuesta.json.return_value = {"id": 1}
        with patch("core.deadline.time.monotonic", monotonic), patch.object(
            api.wcapi, "get", return_value=respuesta
        ):
            token = deadline.iniciar(9)
            try:
                api.get_product(1)
            finally:
                deadline.restaurar(token)
            self.assertEqual(api.wcapi.timeout, 9)

    def test_una_orden_de_muchos_items_no_supera_el_presupuesto(self):
        """
        El caso que hoy escala sin techo: con el presupuesto puesto, el ítem que
        cae fuera del tiempo corta con PresupuestoOrdenAgotado en vez de seguir
        sumando 30 s por ítem hasta pasarse de gunicorn.
        """
        api = WooCommerceAPI()
        monotonic, avanzar = self._reloj()
        respuesta = MagicMock()
        respuesta.status_code = 200
        respuesta.json.return_value = {"id": 1}

        def get_lento(*args, **kwargs):
            avanzar(TIMEOUT_WOOCOMMERCE)
            return respuesta

        atendidos = 0
        with patch("core.deadline.time.monotonic", monotonic), patch.object(
            api.wcapi, "get", side_effect=get_lento
        ):
            token = deadline.iniciar(deadline.PRESUPUESTO_ORDEN)
            try:
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    for _ in range(10):
                        api.get_product(1)
                        atendidos += 1
            finally:
                deadline.restaurar(token)
        # 90 s de presupuesto a 30 s por ítem: entran 3, el cuarto corta.
        self.assertEqual(atendidos, 3)
```

Agregar `MagicMock` al import de `unittest.mock` en la cabecera si no estuviera (ya está en
la línea 6 de `core/tests.py`).

- [ ] **Paso 2: Correr los tests y verificar que fallan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenWooCommerceTest --settings=muci-integrador.test_settings -v 2
```
Esperado: FALLA con `AttributeError: 'WooCommerceAPI' object has no attribute '_timeout_efectivo'`.

- [ ] **Paso 3: Agregar el import y el método en `core/woocommerce.py`**

En la cabecera, después de `from typing import Union, List, Optional`:

```python
from core import deadline
```

Y agregar el método dentro de `WooCommerceAPI`, justo después de `__init__`:

```python
    def _timeout_efectivo(self) -> float:
        """
        Timeout de esta llamada: el mínimo entre el propio y lo que queda de la orden.

        `woocommerce.API` guarda `self.timeout` como atributo de instancia y lo lee
        en cada request, así que ajustarlo antes de llamar surte efecto.

        Sin presupuesto de orden devuelve el timeout de siempre: eso mantiene
        intacto todo uso fuera de `process_order`.
        """
        restante = deadline.restante()
        if restante is None:
            return TIMEOUT_WOOCOMMERCE
        if restante <= 0:
            raise deadline.PresupuestoOrdenAgotado(
                "Presupuesto de orden agotado antes de llamar a WooCommerce."
            )
        return min(TIMEOUT_WOOCOMMERCE, restante)
```

- [ ] **Paso 4: Aplicarlo en los cinco métodos que corren dentro de una orden**

En `core/woocommerce.py`, agregar `self.wcapi.timeout = self._timeout_efectivo()` como
primera línea del cuerpo de cada uno de estos métodos:

```python
    def get_product(self, id, **kwargs):
        self.wcapi.timeout = self._timeout_efectivo()
        res = self.wcapi.get(f"products/{id}", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def get_order(self, id, **kwargs):
        self.wcapi.timeout = self._timeout_efectivo()
        res = self.wcapi.get(f"orders/{id}", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)
```

Igual en `get_customer`, `find_customer_by_email` y `refund_order` — la misma línea como
primera del cuerpo, después del docstring donde lo haya.

**No tocar `get_products`** (el plural, listado): no se usa dentro de una orden.

- [ ] **Paso 5: Correr los tests y verificar que pasan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenWooCommerceTest --settings=muci-integrador.test_settings -v 2
```
Esperado: PASA, 6 tests.

- [ ] **Paso 6: Correr la suite completa**

Ejecutar:
```bash
.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings
```
Esperado: 143 tests OK.

- [ ] **Paso 7: Commit**

```bash
git add core/woocommerce.py core/tests.py
git commit -m "feat(woocommerce): recortar el timeout al restante de la orden"
```

---

### Tarea 4: Fijar el presupuesto en `process_order` y cerrar la brecha del `FailedOrder`

**Archivos:**
- Modificar: `core/services.py` — imports, `process_order` (471-591), la llamada a
  `build_sale_products` (523-528)
- Test: `core/tests.py` (clase nueva `PresupuestoOrdenProcessOrderTest`)

**Interfaces:**
- Consume de la Tarea 1: `deadline.iniciar()`, `deadline.restaurar()`,
  `deadline.PresupuestoOrdenAgotado`.
- Produce: `process_order(order_id: int) -> dict` conserva su firma y su contrato. El cuerpo
  actual pasa a llamarse `_process_order(order_id: int) -> dict` (privado, no lo llama nadie
  más).

**Por qué renombrar en vez de indentar:** envolver el cuerpo actual en un `try/finally`
obligaría a reindentar ~120 líneas y el diff quedaría ilegible. Extraer el cuerpo a
`_process_order` deja el diff en dos bloques chicos.

- [ ] **Paso 1: Escribir los tests que fallan**

Agregar a `core/tests.py`:

```python
class PresupuestoOrdenProcessOrderTest(TestCase):
    """
    `process_order` es el único que fija el presupuesto, y la garantía central es
    que agotarlo termine en un `FailedOrder` grabado — nunca en una orden que
    desaparece.
    """

    def _orden(self, items=1):
        return {
            "total": "40000.00",
            "discount_total": "0.00",
            "meta_data": [],
            "billing": {"email": "cliente@example.com", "first_name": "Ana", "last_name": "Diaz"},
            "shipping": {},
            "line_items": [
                {"product_id": 100 + i, "quantity": 1, "total": "40000.00", "name": f"Item {i}"}
                for i in range(items)
            ],
            "fee_lines": [],
            "payment_method_title": "Efectivo",
        }

    def test_process_order_fija_el_presupuesto(self):
        """Durante la orden hay deadline; el valor arranca en PRESUPUESTO_ORDEN."""
        visto = {}

        def espiar(order_id):
            visto["restante"] = deadline.restante()
            return {"status": "ok"}

        with patch("core.services._process_order", side_effect=espiar):
            process_order(1)
        self.assertIsNotNone(visto["restante"])
        self.assertLessEqual(visto["restante"], deadline.PRESUPUESTO_ORDEN)
        self.assertGreater(visto["restante"], deadline.PRESUPUESTO_ORDEN - 5)

    def test_el_finally_restaura_el_contexto_aunque_la_orden_falle(self):
        """
        Sin esto, el presupuesto de una orden se filtraría a la siguiente request
        del mismo worker y la mataría antes de empezar.
        """
        with patch("core.services._process_order", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                process_order(1)
        self.assertIsNone(deadline.restante())

    def test_el_finally_restaura_el_contexto_en_el_camino_feliz(self):
        with patch("core.services._process_order", return_value={"status": "ok"}):
            process_order(1)
        self.assertIsNone(deadline.restante())

    def test_presupuesto_agotado_en_bims_graba_failed_order(self):
        """La garantía central de la spec."""
        with patch("core.services.wc_api.get_order", return_value=self._orden()), patch(
            "core.services.resolve_pos_and_payments", return_value=(6, [])
        ), patch(
            "core.services.resolve_contact_id",
            side_effect=deadline.PresupuestoOrdenAgotado("sin tiempo"),
        ):
            with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                process_order(555)
        fallida = FailedOrder.objects.get(order_id=555)
        self.assertIn("contacto", fallida.message.lower())

    def test_presupuesto_agotado_leyendo_productos_graba_failed_order(self):
        """
        La brecha encontrada el 2026-08-26: `build_sale_products` (que hace un
        `get_product` por ítem) NO estaba envuelta en try/except, así que una
        excepción ahí se escapaba de `process_order` sin registro. Es justo el
        tramo que escala con la cantidad de ítems.
        """
        with patch("core.services.wc_api.get_order", return_value=self._orden(items=3)), patch(
            "core.services.resolve_pos_and_payments", return_value=(6, [])
        ), patch(
            "core.services.resolve_contact_id", return_value=(7, ["cliente@example.com"])
        ), patch(
            "core.services.build_sale_products",
            side_effect=deadline.PresupuestoOrdenAgotado("sin tiempo en el item 2"),
        ):
            with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                process_order(556)
        fallida = FailedOrder.objects.get(order_id=556)
        self.assertEqual(fallida.status, FailedOrder.FAILED)
        self.assertIn("productos", fallida.message.lower())

    def test_un_error_cualquiera_leyendo_productos_tambien_se_registra(self):
        """El agujero era genérico, no solo del presupuesto."""
        with patch("core.services.wc_api.get_order", return_value=self._orden()), patch(
            "core.services.resolve_pos_and_payments", return_value=(6, [])
        ), patch(
            "core.services.resolve_contact_id", return_value=(7, ["cliente@example.com"])
        ), patch(
            "core.services.build_sale_products",
            side_effect=RuntimeError("WooCommerce se cayó"),
        ):
            with self.assertRaises(RuntimeError):
                process_order(557)
        self.assertTrue(FailedOrder.objects.filter(order_id=557).exists())
```

Agregar `process_order` al import de `core.services` en la cabecera de `core/tests.py`
(línea 15), que hoy dice:

```python
    from core.services import _parse_pos_payments, build_sale_products, resolve_pos_and_payments
```

y pasa a:

```python
    from core.services import (
        _parse_pos_payments,
        build_sale_products,
        process_order,
        resolve_pos_and_payments,
    )
```

- [ ] **Paso 2: Correr los tests y verificar que fallan**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenProcessOrderTest --settings=muci-integrador.test_settings -v 2
```
Esperado: FALLA. Los tres primeros con
`AttributeError: <module 'core.services'> does not have the attribute '_process_order'`;
`test_presupuesto_agotado_leyendo_productos_graba_failed_order` y
`test_un_error_cualquiera_leyendo_productos_tambien_se_registra` con
`FailedOrder.DoesNotExist` — que es la brecha.

- [ ] **Paso 3: Agregar el import en `core/services.py`**

En la cabecera, junto a los otros `from core...`:

```python
from core import deadline
```

- [ ] **Paso 4: Renombrar el cuerpo actual a `_process_order`**

En `core/services.py:471`, cambiar la firma:

```python
def process_order(order_id: int) -> dict:
```

por:

```python
def _process_order(order_id: int) -> dict:
```

y dejar el resto del cuerpo (472-591) **exactamente como está**, incluido el docstring.

- [ ] **Paso 5: Escribir el envoltorio `process_order`**

Insertar justo **antes** de `def _process_order`:

```python
def process_order(order_id: int) -> dict:
    """
    Orquesta el procesamiento completo de una orden de WooCommerce hacia BIMS.

    Es el único punto donde se fija el presupuesto de la orden. gunicorn corre con
    `--timeout 120` y mata al worker **por señal** al pasarse; un worker matado por
    señal no ejecuta el `except` que graba el `FailedOrder`, así que la orden
    desaparecería sin factura y sin registro. Con el presupuesto, cualquier fallo
    por lentitud termina en un `FailedOrder` reintentable.

    El `finally` no es decorativo: sin él, el presupuesto de esta orden se
    filtraría a la próxima request que atienda el mismo worker.
    """
    token = deadline.iniciar()
    try:
        return _process_order(order_id)
    finally:
        deadline.restaurar(token)
```

- [ ] **Paso 6: Correr los tres primeros tests**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenProcessOrderTest.test_process_order_fija_el_presupuesto core.tests.PresupuestoOrdenProcessOrderTest.test_el_finally_restaura_el_contexto_aunque_la_orden_falle core.tests.PresupuestoOrdenProcessOrderTest.test_el_finally_restaura_el_contexto_en_el_camino_feliz --settings=muci-integrador.test_settings -v 2
```
Esperado: PASA, 3 tests.

- [ ] **Paso 7: Cerrar la brecha de `build_sale_products`**

En `core/services.py`, dentro de `_process_order`, reemplazar las líneas 523-528:

```python
    sale_products, skipped_messages = build_sale_products(
        order_id=order_id,
        line_items=order.get("line_items", []),
        fee_lines=order.get("fee_lines", []),
        discount=discount,
    )
```

por:

```python
    # Envuelto a propósito: acá adentro hay un `get_product` por ítem, así que es
    # el tramo que más escala con el tamaño de la orden. Sin este try/except una
    # excepción se escapaba de `process_order` SIN grabar `FailedOrder` y la orden
    # desaparecía — exactamente la falla que el presupuesto viene a evitar.
    try:
        sale_products, skipped_messages = build_sale_products(
            order_id=order_id,
            line_items=order.get("line_items", []),
            fee_lines=order.get("fee_lines", []),
            discount=discount,
        )
    except Exception as e:
        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={
                "status": FailedOrder.FAILED,
                "message": f"Error al leer los productos en WooCommerce. {e}",
            },
        )
        raise
```

- [ ] **Paso 8: Correr la clase completa**

Ejecutar:
```bash
.venv/bin/python manage.py test core.tests.PresupuestoOrdenProcessOrderTest --settings=muci-integrador.test_settings -v 2
```
Esperado: PASA, 6 tests.

- [ ] **Paso 9: Correr la suite completa**

Ejecutar:
```bash
.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings
```
Esperado: 149 tests OK.

- [ ] **Paso 10: Verificar a mano que `sync_bims_contacts` sigue sin presupuesto**

Ejecutar:
```bash
grep -n "deadline" core/management/commands/sync_bims_contacts.py
```
Esperado: **sin resultados**. Ese archivo no se toca; `restante()` le devuelve `None` y se
comporta igual que hoy. Es el criterio de éxito 3 de la spec.

- [ ] **Paso 11: Commit**

```bash
git add core/services.py core/tests.py
git commit -m "feat(services): presupuesto por orden y FailedOrder al leer productos"
```

---

### Tarea 5: Verificar sobre el stack real de producción

**Archivos:** ninguno. Es verificación previa al despliegue.

**Por qué existe esta tarea:** local corre Python 3.12 + Django 6.0.3 y producción corre
3.7.17 + Django 3.2.25. Un "150/150 en verde" local **no prueba compatibilidad** — es la
lección documentada del 2026-08-25. `contextvars` existe desde 3.7, pero eso hay que
verificarlo corriendo, no asumiéndolo.

- [ ] **Paso 1: Pushear la rama**

```bash
git push origin feature/presupuesto-por-orden
```

- [ ] **Paso 2: Crear un worktree en el servidor y correr la suite**

Producción **no cambia de rama**: el worktree es un directorio aparte, así que el servicio
sigue facturando con `main` todo el tiempo.

```bash
ssh -i ~/.ssh/muci anthropic_readonly@muci.org
git -C /var/www/integrador fetch origin feature/presupuesto-por-orden
git -C /var/www/integrador worktree add /root/wt-presupuesto origin/feature/presupuesto-por-orden
cd /root/wt-presupuesto && /root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python \
    manage.py test core/ --settings=muci-integrador.test_settings
```

Invocar el `bin/python` del venv **por ruta absoluta**, no `pipenv run`: desde otro directorio
pipenv busca otro venv por hash y no lo encuentra.

Esperado: **149 tests OK sobre Python 3.7.17 + Django 3.2.25**.

Si `anthropic_readonly` no puede escribir en `/root`, usar `/tmp/wt-presupuesto`.

- [ ] **Paso 3: Verificar que los archivos nuevos compilan en 3.7**

```bash
cd /root/wt-presupuesto && /usr/bin/python3.7 -m py_compile \
    core/deadline.py core/bims.py core/woocommerce.py core/services.py
```
Esperado: sin salida (éxito).

- [ ] **Paso 4: Limpiar el worktree**

```bash
git -C /var/www/integrador worktree remove --force /root/wt-presupuesto
```

- [ ] **Paso 5: Registrar el resultado en el handoff**

Actualizar `handoff.md` con: resultado de la suite en ambos stacks, qué quedó implementado,
y que el hallazgo 1 queda cerrado. **No** afirmar que el reinicio cada 6 h se puede sacar:
la spec pide verlo estable unos días primero.

```bash
git add handoff.md
git commit -m "docs: resultado de la verificacion del presupuesto por orden"
```

---

## Criterios de éxito (de la spec)

1. Suite en verde en local **y** en el stack de producción (Tarea 5).
2. Con presupuesto agotado, siempre hay `FailedOrder`; nunca una orden sin rastro
   (Tarea 4, pasos 1 y 7 — incluida la brecha de `build_sale_products`).
3. `sync_bims_contacts` completa sus 38 páginas sin cambios (Tarea 4, paso 10).
4. Ninguna orden normal activa el presupuesto: en operación sana
   `PresupuestoOrdenAgotado` no debería aparecer en el log.

## Después del despliegue

Vigilar en `bims_sync.log` y `bims_api.log` la aparición de `PresupuestoOrdenAgotado`. Si
aparece en órdenes normales, **el número está mal antes que la lógica**: revisar
`PRESUPUESTO_ORDEN` antes de tocar el diseño. Una orden normal tarda 10-20 s contra 90 de
presupuesto.

Recién con esto estable unos días se puede **discutir** sacar el reinicio automático cada 6
horas del crontab de root, que hoy corta una facturación por la mitad cuatro veces al día.
