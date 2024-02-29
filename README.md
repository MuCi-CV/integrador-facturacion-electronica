# Muci - Integrador Facturación Electrónica

Sisteque que integra woocommerce y BIMS

## Requerimientos

-   Python >= 3.7
-   Pipenv
    -   MySQL >= 5.6

## Entorno de desarrollo

1. Crea una base de datos para el proyecto:

    ```bash
    mysql --execute="CREATE DATABASE muci-integrador;"
    mysql --execute="CREATE USER 'muci-integrador'@'localhost' identified by 'superstrongpassword';"
    mysql --execute="GRANT ALL PRIVILEGES ON muci-integrador.* to 'muci-integrador'@'localhost';"
    ```

2. En la carpeta raíz, crea un archivo `.env` con el siguiente contenido:

    ```
        DEBUG = True
        SECRET_KEY = supersecretkey
        ALLOWED_HOSTS = *
        DB_USER = muci-integrador
        DB_PASSWORD = superstrongpassword
        DB_NAME = muci-integrador
        DB_HOST = 127.0.0.1
        DB_PORT = 3306
        EMAIL_HOST = emaillhost
        EMAIL_FROM = emailfrom
        EMAIL_PORT = emailport
        EMAIL_USER = emailuser
        EMAIL_PASSWORD = emailpassword
        FRONTEND_URL = frontenturl
        EMAIL_USE_SSL = true
        EMAIL_USE_TLS = false

    ```

3. Instala las dependencias del proyecto:

    ```bash
    pipenv install
    ```

4. Ejecuta las migraciones y crea un superusuario:

    ```bash
    pipenv run python manage.py migrate
    pipenv run python manage.py createsuperuser
    ```

5. Ejecuta el servidor de desarrollo:

    ```bash
    pipenv run python manage.py runserver
    ```
