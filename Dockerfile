# ==========================================================================
# Imagen de la aplicación web (Flask servido con gunicorn).
# La base de datos NO va aquí: corre en su propio contenedor (ver
# docker-compose.yml). Esta imagen solo contiene la app.
# ==========================================================================
FROM python:3.13-slim

# No escribir .pyc y no bufferizar la salida (para ver logs en vivo en Docker).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias primero, para aprovechar la caché de capas de Docker.
# psycopg2-binary y reportlab traen wheels precompilados: no hacen falta
# compiladores del sistema.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código de la aplicación.
COPY . .

# Descarga Bootstrap y Chart.js a static/vendor (esa carpeta está en
# .gitignore y se regenera aquí, durante el build, que sí tiene internet).
RUN python descargar_assets.py

EXPOSE 8080

# Servidor de producción. wsgi:app importa la app y crea el usuario admin en
# el primer arranque (ver wsgi.py). Los logs van a stdout/stderr para que se
# vean con `docker compose logs`.
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
