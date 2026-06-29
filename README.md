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

2. En la carpeta raíz, crea el archivo `.env` a partir de la plantilla:

    ```bash
    cp .env.example .env
    ```

    Luego completá los valores reales. La lista completa de variables (cuáles
    son obligatorias y cuáles opcionales) está documentada en `.env.example`.
    El `.env` está en `.gitignore`: nunca se commitea.

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

6. Ejecuta los tests (no requieren `.env`; usan settings mínimos con SQLite en memoria):

    ```bash
    pipenv run python manage.py test core/ --settings=muci-integrador.test_settings
    ```
