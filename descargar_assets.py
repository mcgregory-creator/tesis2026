"""
Descarga Bootstrap (CSS y JS) y Chart.js a static/vendor para que el sistema
funcione SIN internet en la LAN.

Ejecútalo UNA sola vez desde una máquina con conexión a internet:

    python descargar_assets.py

Luego copia toda la carpeta del proyecto (incluida static/vendor) al servidor.
"""
import os
import sys
import urllib.request

BOOTSTRAP_VERSION = "5.3.0"
CHARTJS_VERSION = "4.4.4"

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

ARCHIVOS = {
    f"https://cdn.jsdelivr.net/npm/bootstrap@{BOOTSTRAP_VERSION}/dist/css/bootstrap.min.css":
        "vendor/bootstrap/bootstrap.min.css",
    f"https://cdn.jsdelivr.net/npm/bootstrap@{BOOTSTRAP_VERSION}/dist/js/bootstrap.bundle.min.js":
        "vendor/bootstrap/bootstrap.bundle.min.js",
    f"https://cdn.jsdelivr.net/npm/chart.js@{CHARTJS_VERSION}/dist/chart.umd.min.js":
        "vendor/chartjs/chart.umd.min.js",
}


def descargar():
    for url, destino_rel in ARCHIVOS.items():
        destino = os.path.join(STATIC_DIR, destino_rel)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        print(f"Descargando {url} ...")
        try:
            urllib.request.urlretrieve(url, destino)
            print(f"  -> guardado en {destino}")
        except Exception as e:
            print(f"  ERROR al descargar {url}: {e}")
            sys.exit(1)
    print("\nListo. Bootstrap y Chart.js quedaron disponibles en static/vendor.")


if __name__ == "__main__":
    descargar()
