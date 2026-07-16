# Migrar el sistema a otra PC y editar el código allí

Esta guía lleva el sistema de esta PC a otra usando **GitHub** como puente (es
la forma recomendada y reproducible). El código viaja por GitHub; los datos
NO se copian: en la otra PC la base arranca limpia con el usuario `admin`.

---

## Parte A — En ESTA PC (una sola vez): subir el proyecto a GitHub

El `.env` con secretos **no** se sube (está en `.gitignore`); en la otra PC se
crea de nuevo desde `.env.example`.

1. Inicializa el repositorio y haz el primer commit:

git config --global user.name "Mcgregory"
git config --global user.email "mcgregorymacias12@gmail.com"

   ```bash
   cd "G:\My Drive\Trabajo\Clientes\McGregor\Proyecto"
   git init
   git add .
   git commit -m "Sistema de gestion de envios"
   ```
   ```bash
   cd "C:\Users\Mcgregory\Documents\Proyecto"
   git init
   git add .
   git commit -m "Sistema gestion de envios"
   ```
2. En https://github.com crea un repositorio **privado** (es de un cliente), sin
   inicializarlo con README. Copia la URL que te da.

3. Conéctalo y súbelo:

   ```bash
   git branch -M main
   git remote add origin https://github.com/mcgregory-creator/tesis.git
   git push -u origin main
   ```

   Te pedirá autenticarte con tu usuario y un **token de acceso personal**
   (GitHub → Settings → Developer settings → Personal access tokens).

---

## Parte B — En la OTRA PC: poner el sistema a correr

1. **Instala Docker Desktop** (https://www.docker.com/products/docker-desktop) y
   **Git** (https://git-scm.com). Deja Docker Desktop abierto y corriendo.

2. **Clona el repositorio en una carpeta local NORMAL** — NO dentro de Google
   Drive, OneDrive ni unidades de red (Docker rinde mejor y evita problemas de
   montaje):

   ```bash
   cd C:\proyectos
   git clone https://github.com/mcgregory-creator/tesis.git logistica
   cd logistica
   ```

3. **Crea el archivo `.env`** copiando la plantilla y ajústalo:

   ```bash
   copy .env.example .env
   ```

   Abre `.env` y define al menos una `FLASK_SECRET_KEY` (obligatoria). Genérala
   con:

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   Si esa PC no tiene Python, sirve cualquier cadena larga y aleatoria. Ajusta
   también `ADMIN_PASSWORD` y `WEB_PORT` si quieres.

4. **Levanta todo:**

   ```bash
   docker compose up -d --build
   ```

5. Abre **http://localhost:8080** (o el `WEB_PORT` que pusiste). Entra con
   `admin` / `admin`; el sistema te pedirá cambiar la clave en el primer ingreso.

Con eso el sistema ya corre en la otra PC, idéntico a esta.

### Comandos de operación (en la otra PC)

```bash
docker compose logs -f web     # ver los logs de la app
docker compose down            # detener (los datos se conservan)
docker compose up -d           # volver a levantar
docker compose down -v         # detener y BORRAR la base (empezar de cero)
```

---

## Parte C — Revisar y editar el código con Visual Studio Code

Para este proyecto (Python/Flask) usa **Visual Studio Code** (VS Code), no el
Visual Studio grande. Es gratis y liviano: https://code.visualstudio.com

1. Instala VS Code y ábrelo. `File → Open Folder…` → elige la carpeta clonada
   (`C:\proyectos\logistica`).

2. Instala estas extensiones (icono de extensiones, en la barra izquierda):
   - **Python** (Microsoft) — resaltado y ayuda para el código `.py`.
   - **Docker** (Microsoft) — ver y manejar los contenedores desde VS Code.

3. Edita los archivos que quieras (por ejemplo `app.py`, las plantillas en
   `templates/`, los estilos en `static/css/styles.css`).

4. **Para que tus cambios se vean en la app**, el código está dentro de la
   imagen, así que hay que reconstruir:

   ```bash
   docker compose up -d --build
   ```

   (En VS Code puedes abrir una terminal integrada con `Ctrl+ñ` / `Terminal →
   New Terminal` y correr ahí ese comando.)

   Con la extensión **Docker** también puedes hacer clic derecho sobre el
   contenedor `logistica-web-1` para ver logs o reiniciarlo.

5. **Para guardar tus cambios en GitHub** (y que esta PC o el servidor los
   reciban):

   ```bash
   git add .
   git commit -m "Describe tu cambio"
   git push
   ```

---

## Parte D — Mantener las PCs sincronizadas

- En cualquier PC, para traer los últimos cambios de GitHub y aplicarlos:

  ```bash
  git pull
  docker compose up -d --build
  ```

- Regla de oro: el código se comparte por **GitHub** (`push` / `pull`); la base
  de datos de cada PC es independiente (cada una tiene su propio volumen Docker).

---

## Alternativa sin GitHub (offline / sin internet)

Si la otra PC no tiene internet, puedes mover imágenes y carpeta a mano:

1. En esta PC, exporta las imágenes ya construidas:

   ```bash
   docker save logistica-web logistica-db -o imagenes-logistica.tar
   ```

2. Copia a la otra PC (USB/red): el archivo `imagenes-logistica.tar` **y** la
   carpeta del proyecto (sin `venv/`).

3. En la otra PC:

   ```bash
   docker load -i imagenes-logistica.tar
   copy .env.example .env      # y edita FLASK_SECRET_KEY
   docker compose up -d        # sin --build, ya tiene las imágenes
   ```

Es más frágil que GitHub y no ayuda a editar el código, así que usa GitHub
siempre que puedas.
