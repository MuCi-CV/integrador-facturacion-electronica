# Spec — Presupuesto por orden para las llamadas externas

**Fecha:** 2026-08-25
**Rama:** `feature/presupuesto-por-orden`
**Estado:** ✅ **Aprobado para implementar** (2026-08-25). Decisiones cerradas: transporte por `contextvar`; alcance BIMS + WooCommerce; `PRESUPUESTO_ORDEN` = 90 s; `ContactLookupView` fuera de alcance.
**Origen:** hallazgo 1 de la revisión de `feature/timeouts-bims`, desplegada el 2026-08-25.

## Problema

`feature/timeouts-bims` puso timeout en las 13 llamadas a BIMS y un presupuesto de
reintentos de 40 s **por llamada**. Fue una mejora grande —antes 12 de 13 salían sin
ningún timeout y un BIMS colgado se llevaba un worker sin límite— pero **no acota el
tiempo total de una orden**, que es lo que gunicorn mide.

### El cálculo que lo demuestra

Una orden hace **2 o 3** llamadas a BIMS vía `_retry_request` (`find_contact`, quizá
`create_contact`, `create_sale`). El escenario que rompe no es BIMS caído, es **BIMS lento
pero vivo**: si cada llamada agota una lectura de 30 s y acierta en el reintento, son
~41 s por llamada.

```
3 llamadas × 41 s = 123 s   >   --timeout 120 de gunicorn
```

Y antes de BIMS está WooCommerce: `get_order` más **un `get_product` por ítem**, a 30 s
cada uno. Una orden de 5 ítems puede gastar 180 s ahí sola. Ese tramo **escala con la
cantidad de ítems** y hoy no tiene ningún techo agregado.

### Por qué importa tanto

Al pasarse de `--timeout 120`, gunicorn mata al worker **por señal**. Un worker matado por
señal **no ejecuta el `except` que graba el `FailedOrder`**. La orden desaparece: sin
factura, sin registro, sin nada que reintentar desde el admin.

Es exactamente la falla que `feature/timeouts-bims` venía a prevenir, sobreviviendo en un
camino más angosto. Y es la razón por la que **el reinicio automático cada 6 horas no se
puede sacar todavía**.

## Objetivo

Que **ninguna orden pueda superar el `--timeout` de gunicorn**, de modo que cualquier
fallo por lentitud termine en un `FailedOrder` grabado y reintentable, nunca en una orden
desaparecida.

## No objetivos

- **No** cambiar la lógica de facturación ni los payloads a BIMS.
- **No** tocar `sync_bims_contacts` — ver "Lo que queda deliberadamente sin límite".
- **No** subir el `--timeout` de gunicorn. Sería tratar el síntoma: un worker realmente
  colgado quedaría atado más tiempo.
- **No** resolver los hallazgos 3 y 5 de la revisión (ver "Fuera de alcance y por qué").

## Diseño

### El transporte: un `contextvars.ContextVar`

Se descartaron dos alternativas:

- **Parámetro explícito por firma.** Sin estado oculto, pero son ~8 firmas nuevas y un call
  site futuro que se olvide de pasarlo queda **sin límite en silencio** — el mismo tipo de
  falla que estamos arreglando.
- **Atributo en el singleton `bims`.** El que menos código toca, pero es estado mutable
  global sobre un objeto compartido: hoy anda porque los workers sync atienden de a una
  request, y se rompe sin aviso el día que algo sea concurrente.

Un `ContextVar` (disponible desde Python 3.7, que es lo que corre producción) da lo mejor
de ambas: casi no cambia firmas, **es imposible olvidarlo en un call site nuevo** porque
vive en el embudo, y deja fuera automáticamente a todo el que no lo fije.

**Módulo nuevo `core/deadline.py`**, no dentro de `bims.py`: lo van a usar tanto `bims.py`
como `woocommerce.py`, y que `woocommerce` importe de `bims` sería una dependencia al revés.

```python
# core/deadline.py
import contextvars
import time
from typing import Optional

# 90 s deja 30 s de margen contra los --timeout 120 de gunicorn: suficiente para
# grabar el FailedOrder y responder. Una orden normal tarda 10-20 s.
PRESUPUESTO_ORDEN = 90

_deadline = contextvars.ContextVar("deadline_orden", default=None)


class PresupuestoOrdenAgotado(Exception):
    """La orden superó su presupuesto total. Terminal para esta corrida, reintentable."""


def iniciar(presupuesto: float = PRESUPUESTO_ORDEN):
    """Arranca el reloj. Devuelve un token para restaurar en un finally."""
    return _deadline.set(time.monotonic() + presupuesto)


def restaurar(token) -> None:
    _deadline.reset(token)


def restante() -> Optional[float]:
    """Segundos que quedan, o None si no hay presupuesto fijado en este contexto."""
    limite = _deadline.get()
    return None if limite is None else limite - time.monotonic()
```

`restante()` devolviendo `None` es la pieza clave del diseño: **"sin presupuesto" es un
estado explícito y legítimo**, y es lo que hace que el cron de contactos siga funcionando
sin cambios.

### Dónde se inicia

En **`process_order`**, que es el orquestador, envuelto en `try/finally`:

```python
token = deadline.iniciar()
try:
    ...  # el cuerpo actual, sin cambios
finally:
    deadline.restaurar(token)
```

Cubre también el botón de reintento del admin, que hace `POST` a `/sales/` y termina en
`SalesView` → `process_order`.

**Es el único lugar donde se fija el deadline.** Las otras entradas HTTP quedan afuera; ver
"Fuera de alcance".

### Integración en `bims.py`

En `_retry_loop`, el restante efectivo pasa a ser el **mínimo** entre el presupuesto por
llamada y el que queda de la orden:

```python
restante = limite - time.monotonic()
restante_orden = deadline.restante()
if restante_orden is not None:
    restante = min(restante, restante_orden)
if restante <= 0:
    raise ...  # transitorio si fue el de la llamada; PresupuestoOrdenAgotado si fue el de la orden
kwargs["timeout"] = (min(TIMEOUT_CONEXION, restante), min(TIMEOUT_LECTURA, restante))
```

Notar el `min(TIMEOUT_CONEXION, restante)`: **eso cierra de paso el hallazgo 2**. Hoy la
lectura se recorta al restante pero la conexión no, así que un intento que arranca al
límite puede excederse hasta 5 s. Con el presupuesto por orden en juego, ese exceso deja de
ser cosmético.

Cuando quien se agota es el presupuesto **de la orden**, se lanza `PresupuestoOrdenAgotado`
y **no** se reintenta ni se conmuta de host: no queda tiempo para nada. Debe propagar por
encima del `except BimsTransientError` de `_retry_loop`, o el reintento se la comería.

### Integración en `woocommerce.py`

La librería `woocommerce.API` guarda `self.timeout` como atributo de instancia y lo usa en
cada request (verificado el 2026-08-25), así que se puede ajustar por llamada:

```python
def _timeout_efectivo(self) -> float:
    restante = deadline.restante()
    if restante is None:
        return TIMEOUT_WOOCOMMERCE
    if restante <= 0:
        raise deadline.PresupuestoOrdenAgotado(...)
    return min(TIMEOUT_WOOCOMMERCE, restante)
```

Se aplica en los métodos que se usan dentro de una orden: `get_order`, `get_product`,
`get_customer`, `find_customer_by_email`, `refund_order`. **`get_product` es el que más
importa** porque se llama una vez por ítem.

### Cómo termina siendo un `FailedOrder`

Sin tocar el manejo actual: `process_order` ya tiene `except Exception as e:` que hace
`FailedOrder.objects.update_or_create(...)` y re-lanza. `PresupuestoOrdenAgotado` cae ahí
como cualquier otra excepción.

El resultado es el cambio de fondo: **una orden lenta pasa de desaparecer sin rastro a
quedar registrada como fallida y reintentable desde el admin.**

## Los números y por qué

| Constante | Valor | Razón |
|---|---|---|
| `--timeout` de gunicorn | 120 s | dato existente, no se toca |
| `PRESUPUESTO_ORDEN` | **90 s** | deja 30 s de margen para grabar el `FailedOrder` y responder |
| `PRESUPUESTO_REINTENTOS` | 40 s | se mantiene como tope secundario por llamada |
| `TIMEOUT_LECTURA` / `TIMEOUT_CONEXION` | 30 / 5 s | se mantienen |

El peor caso real queda en ~95 s (90 más un connect que arranque justo en el límite), bien
por debajo de 120. Una orden normal tarda 10-20 s, así que el presupuesto **no debería
activarse nunca** en operación sana. Si empieza a activarse, es señal de que BIMS se
degradó — y eso es información útil, no ruido.

## Lo que queda deliberadamente sin límite

**`sync_bims_contacts`.** Es un comando de cron, no corre bajo gunicorn y hace **38
llamadas secuenciales**. Cualquier presupuesto por orden lo mataría a mitad de camino.
Nunca llama a `deadline.iniciar()`, así que `restante()` le devuelve `None` y se comporta
exactamente como hoy. No requiere ningún cambio en ese archivo — es la propiedad que hace
elegante al `ContextVar`.

## Fuera de alcance y por qué

- **`ContactLookupView` y las demás entradas HTTP.** Se evaluó darles su propio presupuesto
  —también corren bajo gunicorn y también pueden colgar un worker— y **se decidió dejarlas
  afuera** (Carlos, 2026-08-25). La diferencia con la facturación es cualitativa: si a una
  consulta la mata gunicorn **no se pierde nada**, porque no escribe. El daño que motiva
  esta spec es la orden que desaparece sin `FailedOrder`, y eso solo pasa en
  `process_order`. Mantener el alcance en un solo punto de entrada hace el cambio más chico
  y más fácil de verificar. Si más adelante se quiere extender, el mecanismo ya está: es
  llamar a `deadline.iniciar()` en la vista que corresponda.
- **Hallazgo 3** (el `login()` del relogin usa 30 s fuera del presupuesto): solo ocurre en
  modo sesión, y producción corre en modo API Key desde el 2026-08-24. Se arregla cuando y
  si se vuelve a modo sesión.
- **Hallazgo 5** (la conmutación de host solo sirve ante fallas rápidas): es consecuencia
  deliberada de compartir el presupuesto. Con el presupuesto por orden encima, la conmutación
  queda todavía más acotada. Es el trade-off correcto: proteger a gunicorn vale más que un
  fallback que en la práctica ya está desactivado (`BIMS_FALLBACK_URL` comentada).
- **Hallazgo 4** (el error pierde la causa real cuando se agota el presupuesto y hay
  fallback configurada) sí se arregla acá, porque es dos líneas en el mismo camino.

## Tests

Siguiendo lo que ya hace bien `TimeoutsBimsTest`: **interceptar `HTTPAdapter.send`**, no
`Session.send`, y usar reloj falso (`monotonic` parcheado más un `sleep` que adelanta el
tiempo) para no esperar de verdad.

1. Sin `deadline.iniciar()`, el comportamiento es idéntico al actual (protege a `sync_bims_contacts`).
2. Con presupuesto fijado, el `timeout` que llega al adaptador se recorta al restante de la orden.
3. **`TIMEOUT_CONEXION` también se recorta** — el hallazgo 2.
4. Agotado el presupuesto de la orden, se lanza `PresupuestoOrdenAgotado` y **no** hay más intentos ni conmutación de host.
5. Tres llamadas a BIMS con el peor caso **no superan** `PRESUPUESTO_ORDEN`.
6. `get_product` sobre una orden de N ítems no supera el presupuesto (el caso que hoy escala sin techo).
7. `PresupuestoOrdenAgotado` dentro de `process_order` **graba un `FailedOrder`** — la garantía central.
8. El `finally` restaura el contexto aunque la orden falle.

**Y correr la suite sobre el stack real de producción** (Python 3.7.17 + Django 3.2.25) con
un worktree en el servidor, antes de aprobar el despliegue. Los tests locales corren sobre
Django 6 / Python 3.12 y **no prueban compatibilidad**.

## Criterios de éxito

1. Suite en verde en local **y** en el stack de producción.
2. Con presupuesto agotado, siempre hay `FailedOrder`; nunca una orden sin rastro.
3. `sync_bims_contacts` completa sus 38 páginas sin cambios.
4. Ninguna orden normal activa el presupuesto (no debería verse en el log en operación sana).

## Riesgos

- **Estado implícito.** Es el costo del `ContextVar`. Se acota con el `try/finally` y con el
  test 8.
- **Un presupuesto demasiado ajustado convierte lentitud en fallas.** 90 s contra órdenes
  que tardan 10-20 s da mucho margen, pero si empiezan a aparecer `PresupuestoOrdenAgotado`
  en el log hay que revisar el número antes que la lógica.
- **`refund_order` escribe en WooCommerce.** Si se corta por presupuesto a mitad, hay que
  verificar que no quede un reembolso a medias. Revisar durante la implementación.

## Qué habilita

Cerrar esto es la condición para **discutir sacar el reinicio automático cada 6 horas**,
que hoy corta una facturación por la mitad cuatro veces al día. No para sacarlo de
inmediato: primero hay que ver esto corriendo estable unos días.
