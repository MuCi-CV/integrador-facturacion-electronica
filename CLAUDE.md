# Guía para Claude - Integrador de Facturación Electrónica

Este proyecto sirve como un middleware (intermediario) entre WooCommerce y el sistema BIMS para la facturación electrónica continua y sincronización de órdenes/contactos.

## Comandos Útiles (Build & QA)
- **Entorno virtual y dependencias**: `pipenv install` (para instalar dependencias de `Pipfile`).
- **Activar entorno**: `pipenv shell`
- **Ejecutar servidor**: `python manage.py runserver`
- **Migraciones**: `python manage.py makemigrations core` y `python manage.py migrate`
- **Tests**: `python manage.py test core/` o `pytest` (si está configurado en el futuro).

## Arquitectura y Estructura del Proyecto
- `core/`: Contiene el dominio principal y lógica de la aplicación.
  - `bims.py`: Lógica principal de negocio, sincronización e interacción de APIs con el ecosistema BIMS (muy importante mantenerla limpia y modular).
  - `models.py`: Entidades de persistencia (ej. `ContactCache` para evitar múltiples peticiones a BIMS al crear usuarios; `FailedOrder` para registro de transacciones que requieran reintentos).
  - `views.py`: Endpoints y webhooks. Reciben la información (ej. de WooCommerce), validan el request y delegan el procesamiento a `bims.py` o equivalentes.
- `muci-integrador/`: Configuración global del proyecto Django (`settings.py`, `urls.py`).
- `runretryfaileds.sh`: Script utilizado para el reintento de sincronizaciones de órdenes caídas.
- **Logs locales**: Los archivos tipo `.log` (como `bims_api.log`, `bims_sync-2.log`) almacenan el histórico vital para el debugeo de respuestas y comunicación con BIMS.

## Reglas y Buenas Prácticas (Python & Django)

### 1. Patrones de Diseño Django
- **Fat Models, Skinny Views** / **Service Layer**: Evita alojar lógica de negocio compleja en `views.py`. Las vistas solo deben parsear el body, llamar a un servicio (dentro de `bims.py` u otra clase de infraestructura) y devolver una respuesta HTTP.
- **Consultas a BD (Querysets)**: Optimiza las consultas usando `select_related` y `prefetch_related` si ameritas cargar datos anidados. No iterar dentro de bucles si puedes resolverlo a nivel de Base de Datos.

### 2. Estilo de Código (Code Style PEP 8)
- **Formato Estándar**: Se recomienda altamente el uso de **Black** (`black .`) para un formateo uniforme en todo el código fuente.
- **Orden de Imports**: Librería global estándar -> Librerías de terceros (Django, DRF, requests) -> Módulos absolutos/locales de la aplicación.
- **Nomenclatura**: Variables y funciones en `snake_case`. Clases en `PascalCase`. Variables de entorno en `UPPER_SNAKE_CASE`.
- **Anotaciones de Tipo (Type Hints)**: Obligatorias para funciones nuevas. Favorecen el mantenimiento y entendimiento de las estructuras de datos complejas que van y vienen de WooCommerce o BIMS (`def sync_to_bims(order_data: dict) -> bool:`).

### 3. Integración con APIs de Terceros (BIMS / WooCommerce)
- **Manejo de Excepciones de Red**: Todo llamado a la API (`requests.post()`, `requests.get()`) debe estar envuelto en manejo de errores capturando `requests.exceptions.RequestException`. Deben poseer `timeout` explícitos.
- **Tratamiento de Errores Limpio**: Evitar usar `try... except Exception:` al aire salvo que sea extremadamente necesario. Especificar qué error controlar.
- **Idempotencia y Prevención de Bucles**: 
  - Chequear caché (`ContactCache`) antes de crear usuarios para optimizar uso de API.
  - Si una sincronización falla, registrar los motivos limpiamente en la clase `FailedOrder` previendo falsos positivos (no marcar como Success si realmentente no persistió en BIMS).
- **Logging Constante**: Utilizar preferiblemente la librería de `logging`. Guardar el `status_code` y la respuesta cruda `.text` es fundamental cuando trabajamos entre dos sistemas que sufren actualizaciones constantes.

### 4. Tests y QA
- Cada cambio en la creación de Payload para BIMS necesita pruebas manuales o test unitarios (preferibles).
- **Mocks**: Al testear interacciones HTTP usar dependencias como `responses` o el módulo `unittest.mock` para no realizar verdaderos HTTP Requests a BIMS y gastar su cuota/ensuciar el ambiente de pruebas.
