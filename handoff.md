# Handoff sesión del 2026-08-31

> **Producción migró a Python 3.10 + Django 5.2 LTS.** Aparecieron dos regresiones: una se cerró
> con una línea en nginx y la otra sigue abierta pero ya es visible. Además quedó diseñado y
> planificado el sub-proyecto A, con la Tarea 1 implementada.
>
> Tres correcciones a cosas que dábamos por ciertas, y **dos errores míos del lado del servidor**
> que conviene leer antes de repetirlos.

## Estado al cierre

| | |
|---|---|
| **Producción** | `main` @ **`43fd813`** — **Python 3.10.12 + Django 5.2.17** |
| Servicio | último arranque **13:57:39 UTC** |
| Tests | **186** en la rama de trabajo; 183 en `main` |
| Rama activa | `feature/hub-ingreso-cola` @ `0e4b3b4`, **sin pushear** |
| Rollback | `mucintegrador.service.pre-django52` + venv viejo intacto |

---

## 1. El flip a Django 5.2 — HECHO y validado

Producción pasó de **Python 3.7.17 + Django 3.2.25** a **3.10.12 + 5.2.17** a las 13:08:36 UTC.
Cierra más de dos años sin parches de seguridad del framework.

| verificación | resultado |
|---|---|
| Suite en el stack viejo | 179/179 → el rollback es seguro |
| Suite en el venv nuevo, contra el commit final | 179/179 |
| `corsheaders` / `drf-yasg` / `pymysql` | 4.9.0 / 1.21.15 / **1.2.0** |
| `migrate` | **`No migrations to apply.`** → rollback completo |
| Venta real | orden **204000 → BIMS 31385, factura 12040**, certificada ante la SET |

**El chequeo funcional del plan dio 404 con el deploy sano.** `curl http://localhost/swagger.json`
cae en un server block de nginx que no es el del integrador —el host real es
`integrador.muci.org`— así que las requests **nunca llegaron a gunicorn**, y el access log de
journald lo prueba. Por el socket daban 200 y 302 desde el principio.

**Lección, ya escrita en el plan:** un chequeo de verificación que nunca se corrió contra el estado
ANTERIOR no distingue una regresión de un chequeo mal escrito. Ante un rojo, probar el componente
**sin la capa de infraestructura de por medio** antes de disparar el rollback.

## 2. Regresión 1: CSRF 403 en el admin — CERRADA

El admin devolvía *"Origin checking failed"*. **La causa raíz no estaba en Django: estaba en nginx,
y llevaba años mal.**

El server block de `integrador.muci.org` no mandaba **`X-Forwarded-Proto`**. Sin ese header,
`SECURE_PROXY_SSL_HEADER` nunca dispara, `request.is_secure()` da **False**, y Django compara el
`Origin: https://…` del browser contra un `good_origin` armado con `http://`.

**Por qué apareció recién ahora, verificado en el fuente y no asumido:** `_origin_verified` aparece
**0 veces** en el `csrf.py` de Django 3.2 y **2 veces** en el de 5.2. La verificación del header
`Origin` **se agregó en Django 4.0**. Nginx estuvo mal todo este tiempo y 3.2 no miraba.

Arreglo: una línea (`proxy_set_header X-Forwarded-Proto $scheme;`). Backup en
`integrador.muci.org.pre-xfp` — ⚠️ **ese backup ya tiene el header**, no es el original.
Verificado sin ssh: `curl https://integrador.muci.org/swagger.json` reporta `"schemes": ["https"]`.

## 3. Regresión 2: las metas de BIMS no llegaron a Woo — INSTRUMENTADA, causa abierta

La orden 204000 facturó y **no** quedó con `_bims_sale_id` en WooCommerce. El corte parecía
limpio: cinco órdenes con metas antes del flip, la primera sin metas después.

**Afirmé que el flip lo había roto y estaba equivocado.** Se probó el camino completo sobre el venv
nuevo: sin redirect, el PUT sigue siendo PUT, 200, y las metas **quedan persistidas**. La orden se
reparó de paso. La hipótesis del redirect (un 302 convierte cualquier método en GET, verificado en
`SessionRedirectMixin.rebuild_method`) quedó **refutada**: el redirect de nginx para `muci.org` es
un **301**, que un PUT sobrevive.

**Sospecha principal: carrera de escritura perdida.** El PUT salió 24 s después de creada la orden,
con el plugin de Krayin escribiendo a los 9 s. Si otro proceso cargó la orden antes y la guardó
después, nos pisa la meta y Woo igual responde 200.

**Lección:** con `n=1` del lado nuevo, un corte temporal limpio **no** distingue una regresión de
una coincidencia.

**Lo que sí se hizo:** `update_order_meta` ahora verifica que las claves vuelvan en la respuesta del
PUT con el valor que mandamos, y lanza `ServerException` si no — que `services.py` ya captura y
loguea como `WARNING` sin romper la orden. **Cuatro tests, los cuatro vistos fallar antes.** Se
corrigió además un mock que devolvía `{"id": ...}` sin `meta_data`, cosa que Woo no hace nunca:
**ese mock irreal era la razón por la que el agujero podía existir con la suite en verde.**

⚠️ Esto **no arregla** la pérdida, la hace visible. La Tarea 7 del plan de A la vuelve
auto-reparable.

## 4. Sub-proyecto A — spec, plan y Tarea 1

Objetivo de Carlos: que el CRM sepa si una donación se facturó de verdad, porque **fundraising va a
cargar donaciones desde Krayin** y esas nunca pasan por WooCommerce.

**Corrección importante de la sesión:** afirmé que "el CRM como origen no está contemplado en
ninguna parte". **Falso.** El documento publicado el 26/08 ya tiene el diagrama de **dos orígenes**
con Krayin entrando al integrador, rotulado "entrada nueva". El `.md` de arquitectura en disco es de
**julio** y quedó superado. La memoria de Carlos era mejor que la mía.

- **Spec:** `docs/superpowers/specs/2026-08-31-hub-ingreso-cola-estado-design.md`
- **Plan:** `docs/superpowers/plans/2026-08-31-hub-ingreso-cola-estado.md` — 9 tareas, 2 despliegues
- **Progreso:** `progreso.md`

**Tarea 1 hecha** (`51f2798`): estados `PENDING`/`PROCESSING`/`PAUSED`/`NOT_APPLICABLE` y campos
`origin`, `bims_attempts`, `bims_next_attempt`, `woo_meta_ok`, `claimed_at`. Migración `0009`
aditiva: **ningún dato se toca**. 183 → **186 tests**.

**Se abandonó el `RenameField`** por pedido de Carlos, y con razón: en MySQL/MariaDB el DDL hace
commit implícito, así que la atomicidad que Django le da a una migración **no cubre el esquema**, y
no hay dump a mano para ensayarla. Ahora se **expande y contrae**: `order_id` queda intacto y
borrarlo es una tarea diferida.

---

## Cabos sueltos y hallazgos

- ⚠️ **CUATRO consumidores se rompen con el 202, no dos.** Además de `retryfaileds.py:28` y
  `sync_bims_contacts.py:78`, el admin tiene **dos más**: `retry_failed_orders_button`
  (`admin.py:111`) y `retry_selected_orders` (`admin.py:152`). Los cuatro hacen `POST` a `/sales/`
  y chequean `status_code == 200`, que con el 202 **nunca vuelve a ser cierto**. El plan solo lista
  dos: **hay que actualizarlo antes de la Tarea 5.**
- **`makemigrations` no funciona con `test_settings`.** `core/bims.py:723` tiene `bims = BimsApi()`
  a nivel de módulo y el `__init__` hace login, así que el comando intenta conectarse a un host
  inventado y crashea. Usar `--settings=muci-integrador.dev_settings`.
- **`black` no está instalado ni en el `Pipfile`, y el código no está formateado con él** (hay
  líneas de 108 caracteres; su default es 88). `CLAUDE.md` lo recomienda pero nunca se aplicó.
  Correrlo ahora reformatearía medio proyecto: conviene como commit aislado, después del
  Despliegue 2.
- **`DEBUG=True` en producción.** Confirmado por dos caminos: la página de error 403 mostró la
  sección de ayuda, y el SentryUptimeBot recibe **302 en `/`**, redirect que `urls.py` solo agrega
  `if settings.DEBUG`. **Previo al flip.** Sale del `.env`.
- **`admin.py` tiene un botón que corre `call_command("migrate")` desde la UI** (`:84`). Preexistente.
- **`admin.py:53-62` registra la misma URL dos veces.** Preexistente e inofensivo.
- **El reporte a BIMS sigue sin enviar** (`docs/reportes/2026-08-27-reporte-a-bims.md`). Es lo único
  que puede lograr que roten la credencial. Carlos decidió postergarlo.
- Del backlog viejo: **201 órdenes en FAILED**, los **67 parches de terceros**, y
  `traces_sample_rate`/`profiles_sample_rate` en 1.0.

## ⚠️ Dos errores míos del lado del servidor, para no repetirlos

1. **Un `sed` no idempotente duplicó una directiva de nginx.** Se corrió dos veces y quedaron dos
   `proxy_set_header X-Forwarded-Proto`. Con el header duplicado nginx lo manda dos veces y Django
   puede leer `https,https`, que tampoco matchea: el CSRF habría seguido roto por otra razón.
   **Todo comando de parcheo sobre el servidor debe chequear antes de escribir.**
2. **Un `git add -A` mandó tres archivos de dev a producción** (`dev-sucursales.sqlite3`,
   `dev_settings.py`, `dev_urls.py`). Sin credenciales y inertes allá, pero no iban. **Agregar por
   nombre, siempre.** Quedaron gitignorados, y `dev_settings.py` resultó ser una herramienta útil.

**Y una regla nueva de Carlos:** los `ssh` del asistente van **siempre** como
`anthropic_readonly@muci.org`, nunca como root, ni para leer. Todo lo que modifica el servidor se lo
pasa a Carlos para que lo corra él.
