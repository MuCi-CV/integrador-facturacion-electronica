# Handoff sesión del 2026-08-28

> **Dos despliegues, los dos validados con ventas reales el mismo día.** Además se cerró la fuga
> de credencial del lado nuestro y quedó **todo listo para Django 5.2 menos el flip**, que se hace
> el lunes. Aparecieron tres hallazgos que corrigieron cosas que creíamos ciertas, incluida una
> afirmación de la spec que escribí unas horas antes.

## Estado al cierre

| | |
|---|---|
| **Producción** | `main` @ **`470e6ec`** — Python 3.7.17 + Django 3.2.25, sin cambios |
| Servicio | último arranque **14:50:29 UTC** |
| Tests | **179/179** en local (3.12 + **Django 5.2.17**) y en el venv nuevo del servidor (3.10.12 + 5.2.17) |
| Ramas sin mergear | `feature/django-52-lts` @ `35f9a31` (pusheada), `chore/verificar-stack-palancas` (tooling, sigue pendiente de ayer) |

---

# 📅 LO PRIMERO DEL LUNES

### El flip a Django 5.2

**Todo está hecho y probado menos el último paso.** Es la **Task 7** del plan
(`docs/superpowers/plans/2026-08-28-django-52-lts.md`), escrita paso por paso.

Lo que ya está listo:

- **Backup verificado de las 4 bases** — 225 MB, 10.837.450 líneas. El primero que tuvieron.
  Script reutilizable en `backup-bases.sh`.
- **Venv paralelo** `/root/venv-integrador-52` con Python 3.10.12 + Django 5.2.17: **179/179**.
- **`showmigrations --plan` contra la base viva: 0 sin aplicar, 26 aplicadas.** El `migrate` será
  no-op, así que **no queda estado de base adelantado y el rollback es completo.**
- El venv viejo (`integrador-ObaHlHmv`) **intacto**: es el rollback.

Dos cuidados que están en el plan y conviene no improvisar: **leer el unit completo antes de
editarlo** (la ruta del venv puede estar en `Environment=` o `ExecStartPre=`, no solo en
`ExecStart=`), y **respaldarlo como `.pre-django52`** — ese archivo es la vuelta atrás.

### Y lo que no depende de nosotros: enviar el reporte a BIMS

`docs/reportes/2026-08-27-reporte-a-bims.md`, **Parte 1 sola**. Sigue sin enviar, y es lo único
que puede lograr que **roten la credencial** — que es lo único que remedia lo que Sentry ya
ingirió.

Dato nuevo que refuerza el pedido: en 5,5 meses de logs hay **un solo valor distinto** de
credencial. **Nunca la rotaron**, así que la ventana de exposición es el período completo.

---

## 1. La fuga de credencial, cerrada de nuestro lado

Ayer se detectó que BIMS devuelve `Agency.tae_password` en texto plano. La auditoría de hoy mostró
que era más grande: **tres sumideros, no uno.**

| sumidero | estado |
|---|---|
| Log del servidor + rotación | **purgado** (8 ocurrencias) |
| **307 copias en la máquina local**, desde marzo | **purgadas** |
| **Sentry**, vía breadcrumbs de nivel INFO | **cortado de arriba** por el filtro |
| ~~`FailedOrder` en la base~~ | **descartado con evidencia directa**: 8632 filas, 0 con el campo |

**`propagate: False` NO protege de Sentry** — verificado en el fuente del SDK, no asumido:
`sentry_sdk/integrations/logging.py:160-195` parchea `logging.Logger.callHandlers`, que corre en el
logger de origen y solo filtra por nombre contra una lista de ignorados.

**El filtro** (`_redactar` / `_redactar_texto`, `470e6ec`) recorre dicts y listas y matchea la clave
por **subcadena**. El enmascarado viejo fallaba por tres razones independientes: comparaba nombres
exactos (`tae_password` ≠ `password`), era de un solo nivel, y no se aplicaba a las respuestas.
Devuelve **copia**, porque `login()` saca el session id del mismo dict que loguea.

**Validado contra los logs reales antes de desplegar:** de las 307 líneas, redacta 307 y deja 0.
**Y sobre tráfico vivo después:** las dos ventas posteriores al deploy generaron 4 entradas, todas
redactadas. Si el filtro estuviera roto, el conteo "con valor" sería 2 y es 0 — o sea que actuó el
filtro, no solo la purga.

**Cómo se purgó, que importa si hay que repetirlo:** los 3 workers de gunicorn tienen
`bims_api.log` abierto. Un `sed -i` crea un inodo nuevo y el proceso sigue escribiendo al viejo, ya
borrado → se pierde el log hasta el próximo reinicio. La forma correcta es abrir en `r+`,
reescribir desde 0 y truncar: mismo inodo (verificado `797722->797722`).

## 2. Correlación orden↔factura, desplegada y validada

`FailedOrder` ganó `bims_sale_id` y `bims_invoice_number` (migración 0008), `create_sale` dejó de
descartar el `invoice_number`, y la factura **también se anota como metadata en la orden de
WooCommerce**, que es donde trabaja la caja.

**Validación end-to-end con dos ventas reales**, con el corte exactamente en el deploy de las 14:15:

| orden | UTC | sale_id | factura |
|---|---|---|---|
| 203769 → 203775 | 12:56–13:15 | `None` | `None` |
| **203784** | 17:26 | **31325** | **14567** |
| **203787** | 18:10 | **31326** | **14568** |

Y en `wpzv_wc_orders_meta` las dos tienen `_bims_sale_id` y `_bims_invoice_number`, **con las tres
metas de Krayin intactas** — el upsert por clave del `PUT` confirmado con tráfico real, no solo por
lectura del código.

**La escritura a Woo es best-effort a propósito:** ahí la venta ya está facturada y certificada
ante la SET, y el `FailedOrder` ya quedó COMPLETED. Propagar el error daría un 503 que además
mentiría, y 5 seguidos apagan el webhook `Venta Entrada`. Hay un test que lo fija.

⚠️ **203784 y 203787 son las primeras órdenes donde nuestro `PUT` agrega un tercer
`order.updated`**, que dispara el bot de WhatsApp. No debería duplicar entradas (el job chequea
`status = 'sent'` y corre con `numprocs=1`), y no hay señal de problema. Pero si aparece un reclamo
de entrada duplicada, son esas dos las primeras a mirar.

## 3. Django 5.2: tres hallazgos que cambiaron el plan

- **El servidor ya tenía `/usr/bin/python3.10`** (Ubuntu 22.04.5). El requisito duro de 5.2 estaba
  satisfecho desde el principio.
- **No hay ni una migración interna nueva entre 3.2.25 y 5.2.17** (`admin` 3/3, `auth` 12/12,
  `contenttypes` 2/2, `sessions` 1/1). Eso convierte el rollback por venv en **completo** y bajó el
  riesgo de todo el proyecto.
- **El "muro de PyMySQL" para Django 6 no existe.** El spoof de `version_info` cambió entre
  versiones: 1.1.x reporta `(1,4,6)` y no pasa el umbral `≥2.2.1` de Django 6, pero **1.2.0 reporta
  `(2,2,8)` y pasa los dos**. **Corregí la spec**, que lo daba como el motivo técnico para no ir a
  6. El motivo válido es el otro: **5.2 es el único LTS vigente.** De ahí el piso
  `pymysql = ">=1.2"` en el `Pipfile`.

## 4. Lo que se descubrió al cerrar el hueco de validación

`test_settings` cargaba **4 apps** y esquivaba `drf_yasg`, `corsheaders` y el admin. O sea que
"176 en verde sobre Django 6" no decía nada sobre lo que más puede romper. Ahora carga **las 10 de
producción**, con `ROOT_URLCONF` real, y hay tres tests nuevos (176 → **179**).

El test que carga el `settings.py` real encontró **dos cosas en su primera corrida**:

1. **`corsheaders` no estaba instalado en el venv local**, aunque el settings real lo lista.
2. **Con Django 6.0.3 + PyMySQL 1.1.2 el settings real NO LEVANTA:** `ImproperlyConfigured:
   mysqlclient 2.2.1 or newer is required; you have 1.4.6`. **El entorno local no podía arrancar la
   app y nadie lo sabía.**

Y de paso: **`manage.py check` dispara un login REAL contra BIMS.** `bims.py` instancia `BimsApi()`
al importarse y el `check` carga el admin, que arrastra esa cadena. **Todo comando de `manage.py`
en producción hace un login a BIMS al importar**, incluidos `migrate` y `showmigrations`.

---

## Cabos sueltos

- **`Pipfile.lock` quedó desactualizado** y está trackeado: sigue fijando Django 3.2.25, DRF
  3.15.1, `mysqlclient`, `simplejwt` y PyMySQL 1.1.1. Un `pipenv install` honra el lock e
  instalaría **el stack viejo en silencio**. Decisión pendiente: regenerarlo con Python 3.10, o
  sacarlo del repo dado que el venv nuevo ya no se construye con pipenv.
- **El `.env` de producción es legible por todos** (`-rw-r-xr--`): cualquier usuario del servidor
  lee la password de MariaDB y las credenciales de BIMS. Se arregla con `chmod 600`. No lo toqué.
- **`chore/verificar-stack-palancas` sigue sin mergear.** Mientras viva solo en rama, el script
  **ignora la variable `PYTHON=` en silencio**.
- **Corrección al registro:** la memoria decía que los PAT de `carlvallory` no podían pushear a
  este repo. **Hoy el push funcionó** tres veces.
- Tres archivos de dev sin trackear (`dev-sucursales.sqlite3`, `dev_settings.py`, `dev_urls.py`),
  restos de la rama de sucursales.
- **Recomendación para el repo del bot** (no es de este proyecto): la idempotencia depende de
  `numprocs=1` en un `.conf` de Supervisor, y `bot/README.md:350` documenta `numprocs=2`. El
  arreglo que el propio doc propone es `ShouldBeUnique` con `uniqueId()` devolviendo el `orderId`.
- Del backlog viejo, sin tocar: **201 órdenes en FAILED** sin reproceso automático, los **67
  parches de terceros**, `traces_sample_rate`/`profiles_sample_rate` en **1.0** con el DSN
  hardcodeado, y la rotación de `bims_api.log` que pierde ~12 h por día.
