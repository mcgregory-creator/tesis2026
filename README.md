# Sistema de Gestión de Envíos

Aplicación web (Flask + PostgreSQL) para gestión de despachos, choferes, rutas y
bitácora de eventos. Pensada para funcionar en una LAN: el servidor corre en una
laptop y los choferes se conectan desde el navegador de sus móviles.

## Requisitos

- **Con Docker (recomendado):** solo Docker Desktop. No necesitas instalar
  Python ni PostgreSQL en tu máquina.
- **Sin Docker (nativo):** Python 3.10+ y PostgreSQL 14+.

## Ejecución con Docker (recomendado)

Levanta la base de datos y la app juntas, con un solo comando. La base carga el
esquema sola la primera vez y la app crea el usuario `admin` al arrancar.

1. Copia `.env.example` a `.env` y define al menos `FLASK_SECRET_KEY` (genérala
   con `python -c "import secrets; print(secrets.token_hex(32))"`). Ajusta
   `ADMIN_PASSWORD` y `WEB_PORT` si quieres. Para Docker **no** hace falta tocar
   `DB_HOST`/`DB_PORT`: el compose conecta la app al contenedor de la base.

2. Construye y levanta todo:

   ```bash
   docker compose up -d --build
   ```

3. Abre `http://localhost:8080` (o el `WEB_PORT` que hayas puesto). Entra con
   `admin` / `admin`; el sistema te obligará a cambiar la clave en ese primer
   ingreso.

Comandos útiles:

```bash
docker compose logs -f web     # ver los logs de la app
docker compose down            # detener (los datos se conservan en el volumen)
docker compose up -d           # volver a levantar
docker compose down -v         # detener y BORRAR la base (empieza de cero)
```

> **Notas de diseño.** El esquema se **hornea** dentro de la imagen de Postgres
> (`Dockerfile.db`) en vez de montarse como archivo, porque los bind-mounts de
> archivos sueltos no funcionan de forma fiable cuando el proyecto vive en
> Google Drive / unidades virtuales de Windows. Los datos persisten en el
> volumen `logistica_pgdata` entre reinicios. Si editas `schema.sql`, reconstruye
> con `docker compose up -d --build` y, si quieres recargarlo desde cero,
> `docker compose down -v` primero (esto borra los datos).

### Llevarlo a un servidor en la red

En el servidor: instala Docker, clona el repositorio de GitHub, crea el `.env`
con secretos fuertes (`FLASK_SECRET_KEY` propio, `ADMIN_PASSWORD` propio,
`SESSION_COOKIE_SECURE=1` si sirves por HTTPS) y ejecuta el mismo
`docker compose up -d --build`. Publica el puerto (`WEB_PORT`) o pon un proxy
inverso (Nginx/Caddy) delante para HTTPS.

Para mover el sistema a otra PC y editar el código allí con VS Code, sigue
`MIGRACION.md` paso a paso.

## Puesta en marcha (sin Docker)

1. **Crear y activar el entorno virtual e instalar dependencias**

   ```bash
   python -m venv venv
   venv\Scripts\activate        # Windows
   pip install -r requirements.txt
   ```

2. **Crear la base de datos** con `schema.sql` (SQL plano, exportado directamente con
   `pg_dump --schema-only` desde la base de datos ya al día — incluye las 5 tablas
   originales más `fecha_salida`/`fecha_llegada` en `envios` y la tabla
   `configuracion_financiera`):

   ```bash
   createdb -U postgres logistica_db
   psql -U postgres -d logistica_db -f schema.sql
   ```

   Si ya tienes una base `logistica_db` de una instalación anterior y quieres
   recrearla desde cero con este esquema (perderás los datos que tenga):

   ```bash
   pg_dump  -U postgres -d logistica_db -f respaldo_antes_de_recrear.sql  # opcional
   dropdb   -U postgres logistica_db
   createdb -U postgres logistica_db
   psql -U postgres -d logistica_db -f schema.sql
   ```

   > Las versiones anteriores de este archivo (el respaldo binario original de
   > `pg_restore`, y los pasos intermedios `schema_v2.sql`/`migracion_v2.sql`/
   > `migracion_v3.sql`) ya no forman parte del proyecto: este `schema.sql` es ahora
   > la única fuente de verdad del esquema, verificada aplicándola sobre una base
   > vacía antes de reemplazar los archivos anteriores.

3. **Configurar variables de entorno**: copia `.env.example` a `.env` y ajusta los
   valores (credenciales de la BD, `FLASK_SECRET_KEY`, `PRECIO_COMBUSTIBLE`, etc.).

   Genera una clave segura con:

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

4. **Descargar los assets locales (una sola vez, con internet)** para que el sistema
   funcione sin conexión en la LAN. Esto trae Bootstrap y Chart.js (usado en el
   gráfico de productividad del panel de administrador):

   ```bash
   python descargar_assets.py
   ```

5. **Arrancar el servidor**

   ```bash
   python app.py
   ```

   Quedará disponible en `http://<IP-de-la-laptop>:5000`. Los choferes acceden desde
   sus móviles usando esa IP dentro de la misma red WiFi.

## Acceso inicial

La primera vez se crea automáticamente el usuario administrador:

- Usuario: `admin`
- Contraseña: la de `ADMIN_PASSWORD` en `.env` (por defecto `admin` — **cámbiala**).

## Roles

- **Administrador**: registra clientes, choferes, vehículos, crea rutas, edita o anula
  rutas en curso, gestiona altas/bajas y contraseñas, y consulta el panel de
  productividad (KPIs, gráfico anual, rankings) con exportación de reportes en PDF.
- **Chofer**: al tener una ruta asignada ve 3 acciones: **Iniciar** (marca la salida),
  **Actualizar** (reporta Gasolina, Peaje o Falla mecánica desde un modal guiado) y
  **Finalizar** (marca la llegada y libera el vehículo).

Cualquier usuario (Administrador o Chofer) puede cambiar su propia contraseña desde el
botón "Mi Cuenta" en la barra de navegación.

## Reportes y validaciones

- **Panel de productividad** (dashboard del administrador): rutas en curso/finalizadas,
  gráfico de viajes entregados por mes, top 5 destinos y top 5 choferes.
- **Exportación en PDF** desde `/reportes`: por ruta puntual, por chofer (todas sus
  rutas) o por mes calendario (todos los choferes).
- **Documentos vigentes**: al crear o editar una ruta, el sistema valida que el chofer
  (licencia, certificado médico, cédula) y el vehículo (RCV, impuesto de alcaldía)
  tengan esos documentos vigentes; si no, bloquea la operación indicando cuál venció.
  Esas fechas se actualizan desde "Gestión de Tablas" → Editar.

## Seguridad para el despliegue real

- Deja `FLASK_DEBUG=0` en `.env`.
- Cambia `ADMIN_PASSWORD` y define una `FLASK_SECRET_KEY` propia.
- Considera servir con `waitress` (`pip install waitress`) en la laptop:
  `waitress-serve --host=0.0.0.0 --port=5000 app:app`.

## Despliegue en la web (Render, uso temporal)

El proyecto ya trae los archivos para publicarlo en un hosting en la nube:

- `wsgi.py` — punto de entrada para el servidor de producción (crea el admin al
  arrancar, cosa que `python app.py` hacía solo en modo local).
- `Procfile` — comando de arranque con `gunicorn`.
- `requirements.txt` — incluye `gunicorn`.
- `.python-version` — fija la versión de Python del host (3.13).
- `render.yaml` — describe la base PostgreSQL + el servicio web para Render.
- Soporte de `DATABASE_URL`: si el host entrega la conexión en esa variable, la
  app la usa (normalizando el esquema); si no, sigue usando `DB_*` como en local.

**Pasos en [Render](https://render.com) usando el blueprint:**

1. Sube el proyecto a un repositorio de GitHub (**privado**, por ser de cliente).
2. En Render: `New +` → `Blueprint` → conecta el repositorio. Render leerá
   `render.yaml` y creará la base de datos y el servicio web.
3. Cuando lo pida, escribe el valor de `ADMIN_PASSWORD` (no está en el repo).
4. Carga el esquema en la base recién creada: copia la *External Database URL*
   que muestra Render y ejecuta, desde una máquina con `psql`:

   ```bash
   psql "postgresql://usuario:clave@host/basedatos" -f schema.sql
   ```

5. Haz un *Manual Deploy* del servicio web (o espera al siguiente arranque): al
   existir ya las tablas, se creará el usuario `admin`. La app quedará en
   `https://logistica-envios.onrender.com` (el nombre puede variar).

> El plan gratuito de PostgreSQL de Render **expira a los 30 días**, ideal para
> un uso temporal de 1 mes. El servicio web gratuito se duerme tras ~15 min sin
> tráfico y despierta solo en la siguiente visita (tarda unos segundos).

Si en vez del blueprint creas el *Web Service* a mano, usa como **Build Command**
`pip install -r requirements.txt && python descargar_assets.py` y como
**Start Command** `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`,
y define a mano las variables `DATABASE_URL`, `FLASK_SECRET_KEY`, `FLASK_DEBUG=0`,
`SESSION_COOKIE_SECURE=1` y `ADMIN_PASSWORD`.

Ver `CORRECCIONES.md` para el detalle de los errores corregidos y `MEJORAS.md` para el
detalle de las funcionalidades añadidas (panel del chofer, reportes, edición
administrativa, etc.).
