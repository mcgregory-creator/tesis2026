"""
Punto de entrada para servidores WSGI de producción (gunicorn, waitress...).

    gunicorn wsgi:app

A diferencia de `python app.py`, un servidor WSGI importa el objeto `app`
directamente y NO ejecuta el bloque `if __name__ == "__main__"`. Por eso aquí
llamamos explícitamente a inicializar_sistema(), de modo que el usuario 'admin'
por defecto se cree en el primer arranque también en producción.

Es idempotente: si el admin ya existe no hace nada, y si el esquema todavía no
se ha cargado en la base, inicializar_sistema() lo detecta y solo avisa por
consola sin impedir que la aplicación arranque.
"""
from app import app, inicializar_sistema

inicializar_sistema()
