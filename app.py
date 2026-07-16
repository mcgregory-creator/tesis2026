import os
import secrets
from datetime import date

# Carga opcional de variables desde un archivo .env (si python-dotenv está instalado).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (
    Flask, render_template, request, session, redirect, url_for,
    flash, abort, jsonify, Response
)
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash, check_password_hash

import reportes

# ==============================================================================
# 1. CONFIGURACIÓN INICIAL
# ------------------------------------------------------------------------------
# Toda la configuración sensible se lee de variables de entorno (.env). Si no se
# define, se usa un valor por defecto SOLO apto para desarrollo local.
# ==============================================================================
app = Flask(__name__)

# Clave secreta que firma las cookies de sesión. En producción DEBE venir del
# entorno; si no existe, se genera una aleatoria (las sesiones no sobreviven a
# un reinicio, lo cual es un recordatorio de que hay que configurarla).
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Endurecimiento de la cookie de sesión.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # No accesible por JavaScript
    SESSION_COOKIE_SAMESITE="Lax",  # Mitiga CSRF en navegación entre sitios
)

# En producción sobre HTTPS (p. ej. el hosting en la nube) conviene marcar la
# cookie de sesión como Secure para que el navegador solo la envíe por HTTPS.
# Se activa con SESSION_COOKIE_SECURE=1; se deja apagado por defecto para no
# romper el acceso por HTTP en la LAN local.
if os.environ.get("SESSION_COOKIE_SECURE", "0") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True

DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

# En una LAN cerrada no hace falta CORS abierto: el front y la API comparten
# origen. Se restringe a los orígenes indicados por entorno (por defecto ninguno
# externo). Se mantiene el soporte de credenciales para las cookies de sesión.
_cors_origins = os.environ.get("CORS_ORIGINS", "").strip()
if _cors_origins:
    CORS(
        app,
        resources={r"/api/*": {"origins": _cors_origins.split(",")}},
        supports_credentials=True,
    )

# ==============================================================================
# 2. CONEXIÓN A POSTGRESQL
# ------------------------------------------------------------------------------
# En un hosting en la nube (Render, Railway, Heroku...) la base de datos se
# entrega como una única cadena de conexión en la variable DATABASE_URL. Si esa
# variable existe se usa tal cual (normalizando el prefijo al driver psycopg2);
# si no, se arma la URL con las variables sueltas DB_* (modo local / LAN).
# ==============================================================================
def _construir_db_uri():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # SQLAlchemy 2.x no acepta el esquema "postgres://" (heredado de Heroku)
        # y conviene fijar explícitamente el driver psycopg2.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg2://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        return url

    DB_USER = os.environ.get("DB_USER", "postgres")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "postgres")
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = os.environ.get("DB_PORT", "5432")
    DB_NAME = os.environ.get("DB_NAME", "logistica_db")
    return f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


DB_URI = _construir_db_uri()

engine = create_engine(
    DB_URI,
    pool_size=10,
    max_overflow=5,
    pool_timeout=30,
    pool_pre_ping=True,   # Reconecta si la conexión quedó obsoleta
)

# Tipos de evento válidos que el móvil puede reportar.
EVENTOS_GASTO = {"Gasolina", "Peaje", "Novedad"}
EVENTOS_ESTADO = {"Salida", "Llegada"}
EVENTOS_VALIDOS = EVENTOS_GASTO | EVENTOS_ESTADO

# ==============================================================================
# 2.1 REGLAS DE NEGOCIO COMPARTIDAS
# ------------------------------------------------------------------------------
# Estados de envío sobre los que se permite editar/anular. Una vez "Entregado"
# la ruta queda fija como registro histórico para los reportes de productividad.
# ==============================================================================
ESTADOS_ENVIO_EDITABLES = {"Pendiente", "En Ruta"}


def _documento_vigente(fecha_vencimiento):
    """Un documento está vigente si tiene fecha registrada y no ha vencido."""
    return fecha_vencimiento is not None and fecha_vencimiento >= date.today()


def _validar_documentos_chofer(chofer):
    """Verifica licencia, certificado médico y cédula del chofer.

    Devuelve (True, None) si todo está vigente, o (False, mensaje) indicando
    qué documento falta o venció.
    """
    faltantes = []
    if not _documento_vigente(chofer["vencimiento_licencia"]):
        faltantes.append("licencia de conducir")
    if not _documento_vigente(chofer["vencimiento_certificado_medico"]):
        faltantes.append("certificado médico")
    if not _documento_vigente(chofer["vencimiento_cedula"]):
        faltantes.append("cédula")
    if faltantes:
        return False, f"El chofer tiene documentos vencidos o sin registrar: {', '.join(faltantes)}."
    return True, None


def _validar_documentos_vehiculo(vehiculo):
    """Verifica RCV e impuesto de alcaldía del vehículo."""
    faltantes = []
    if not _documento_vigente(vehiculo["vencimiento_rcv"]):
        faltantes.append("RCV")
    if not _documento_vigente(vehiculo["vencimiento_impuesto_alcaldia"]):
        faltantes.append("impuesto de alcaldía")
    if faltantes:
        return False, f"El vehículo tiene documentos vencidos o sin registrar: {', '.join(faltantes)}."
    return True, None


def _liberar_vehiculo(conn, id_vehiculo):
    """Marca un vehículo como Disponible (fin de ruta, edición o anulación)."""
    conn.execute(text(
        "UPDATE vehiculos SET estado = 'Disponible' WHERE id_vehiculo = :id"
    ), {"id": id_vehiculo})


def _chofer_con_ruta_activa(conn, id_chofer, excluir_id_envio=None):
    """True si el chofer ya tiene una ruta Pendiente o En Ruta asignada.

    El dashboard del chofer solo muestra una ruta activa a la vez, así que
    asignarle una segunda la dejaría invisible hasta entregar la primera.
    """
    consulta = (
        "SELECT 1 FROM envios "
        "WHERE id_chofer = :chofer AND estado_envio IN ('Pendiente', 'En Ruta')"
    )
    parametros = {"chofer": id_chofer}
    if excluir_id_envio is not None:
        consulta += " AND id_envio != :excluir"
        parametros["excluir"] = excluir_id_envio
    return conn.execute(text(consulta), parametros).fetchone() is not None


def _obtener_configuracion_financiera(conn):
    """Lee el precio del combustible y el margen de ganancia configurados
    desde la base de datos (tabla `configuracion_financiera`, fila única
    id=1 — ver migracion_v3.sql / schema_v2.sql). Editable desde
    /configuracion sin reiniciar el servidor.
    """
    fila = conn.execute(text(
        "SELECT precio_combustible, tipo_ganancia, valor_ganancia "
        "FROM configuracion_financiera WHERE id = 1"
    )).mappings().fetchone()
    if fila is None:
        # Red de seguridad por si la migración aún no se aplicó.
        return {"precio_combustible": 0.5, "tipo_ganancia": "porcentaje", "valor_ganancia": 0}
    return fila


def _calcular_costos_ruta(distancia, vehiculo, config_financiera):
    """Costo de combustible y mantenimiento de una ruta (lo que se guarda en
    `envios`). No incluye la ganancia: esa es solo para cotizar al cliente,
    se calcula aparte con `_calcular_ganancia` y no se persiste por ruta.
    """
    km_por_litro = float(vehiculo["km_por_litro"]) or 1.0
    costo_combustible = round(
        (distancia / km_por_litro) * float(config_financiera["precio_combustible"]), 2
    )
    costo_mantenimiento = round(distancia * float(vehiculo["costo_mantenimiento_km"]), 2)
    return costo_combustible, costo_mantenimiento


def _calcular_ganancia(costo_combustible, costo_mantenimiento, config_financiera):
    """Margen de ganancia sobre combustible + mantenimiento, según el modo
    configurado: porcentaje de la base, o un monto fijo."""
    if config_financiera["tipo_ganancia"] == "fijo":
        return round(float(config_financiera["valor_ganancia"]), 2)
    base = costo_combustible + costo_mantenimiento
    return round(base * (float(config_financiera["valor_ganancia"]) / 100), 2)


# Subconsulta reutilizable: gastos reales de ruta (gasolina adicional, peajes
# y fallas mecánicas) reportados por el chofer desde la bitácora, con el total
# y el desglose por categoría. No incluye "Salida"/"Llegada", que son eventos
# de estado, no gastos.
_SUBCONSULTA_GASTOS_RUTA = (
    "(SELECT id_envio, COALESCE(SUM(monto_valor), 0) AS gastos_ruta, "
    "        COALESCE(SUM(monto_valor) FILTER (WHERE tipo_evento = 'Gasolina'), 0) AS gastos_gasolina, "
    "        COALESCE(SUM(monto_valor) FILTER (WHERE tipo_evento = 'Peaje'), 0) AS gastos_peaje, "
    "        COALESCE(SUM(monto_valor) FILTER (WHERE tipo_evento = 'Novedad'), 0) AS gastos_falla "
    " FROM bitacora_rutas WHERE tipo_evento IN ('Gasolina', 'Peaje', 'Novedad') "
    " GROUP BY id_envio) AS gastos_bitacora"
)


def _gastos_ruta(conn, id_envio):
    """Total de gastos reales de una ruta puntual (ver un solo envío)."""
    return float(conn.execute(text(
        "SELECT COALESCE(SUM(monto_valor), 0) FROM bitacora_rutas "
        "WHERE id_envio = :id AND tipo_evento IN ('Gasolina', 'Peaje', 'Novedad')"
    ), {"id": id_envio}).scalar_one())


def _ganancia_neta(costo_flete, costo_combustible, costo_mantenimiento, gastos_ruta):
    """Ganancia neta real del flete:

    costo del flete − combustible estimado − mantenimiento estimado − gastos
    reales de la ruta (gasolina adicional, peajes, fallas mecánicas).
    """
    return round(
        float(costo_flete) - float(costo_combustible) - float(costo_mantenimiento)
        - float(gastos_ruta), 2,
    )

# ==============================================================================
# 3. PROTECCIÓN CSRF (sin dependencias externas)
# ------------------------------------------------------------------------------
# Se genera un token por sesión. Los formularios lo envían en un campo oculto y
# las peticiones fetch de la API lo mandan en la cabecera 'X-CSRFToken'.
# ==============================================================================
def _get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inyectar_csrf():
    """Expone csrf_token() a todas las plantillas Jinja2."""
    return {"csrf_token": _get_csrf_token}


def _validar_csrf():
    token_sesion = session.get("_csrf_token")
    enviado = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    return token_sesion is not None and enviado == token_sesion

# ==============================================================================
# 4. MIDDLEWARE DE SEGURIDAD (SESIÓN + CSRF)
# ==============================================================================
@app.before_request
def auditar_acceso_sesion():
    """Se ejecuta ANTES de cada ruta: garantiza token CSRF, valida CSRF en
    métodos que modifican estado y exige sesión iniciada."""
    # Asegurar que exista un token CSRF disponible para las plantillas.
    _get_csrf_token()

    # Validación CSRF para cualquier método que altere datos.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _validar_csrf():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Token CSRF inválido o ausente."}), 400
            abort(400)

    # El login y los archivos estáticos son de libre acceso.
    if request.endpoint == "login" or (
        request.endpoint and request.endpoint.startswith("static")
    ):
        return None

    # A partir de aquí se exige sesión válida.
    if "usuario_id" not in session:
        if request.path.startswith("/api/"):
            abort(401)
        flash("Por seguridad, debes iniciar sesión para acceder.", "warning")
        return redirect(url_for("login"))

    # Si el usuario tiene un cambio de contraseña pendiente (p. ej. el admin en
    # su primer ingreso), se le retiene en la pantalla de cambio obligatorio: no
    # puede usar el resto del sistema hasta definir una contraseña propia. Los
    # archivos estáticos y el login ya salieron antes; aquí solo se permiten la
    # propia pantalla de cambio y cerrar sesión.
    if session.get("debe_cambiar_clave") and request.endpoint not in (
        "cambio_obligatorio_password", "logout"
    ):
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "Debes cambiar tu contraseña antes de continuar."
            }), 403
        return redirect(url_for("cambio_obligatorio_password"))


def solo_admin():
    """Aborta con 403 si el usuario en sesión no es Administrador."""
    if session.get("rol") != "Administrador":
        abort(403)

# ==============================================================================
# 5. AUTENTICACIÓN
# ==============================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if "usuario_id" in session:
        return redirect(url_for("inicio"))

    if request.method == "POST":
        usuario_ingresado = request.form.get("usuario", "")
        password_ingresado = request.form.get("password", "")
        with engine.connect() as conn:
            consulta = text(
                "SELECT * FROM usuarios WHERE usuario = :user AND estado = 'Activo'"
            )
            user = conn.execute(
                consulta, {"user": usuario_ingresado}
            ).mappings().fetchone()

        if user and check_password_hash(user["password"], password_ingresado):
            # Regenerar el token CSRF al iniciar sesión (evita fijación de sesión).
            session.clear()
            session["_csrf_token"] = secrets.token_hex(32)
            session["usuario_id"] = user["id_usuario"]
            session["rol"] = user["rol"]
            session["nombre"] = user["nombre_completo"]
            session["debe_cambiar_clave"] = bool(user["debe_cambiar_clave"])
            if session["debe_cambiar_clave"]:
                return redirect(url_for("cambio_obligatorio_password"))
            return redirect(url_for("inicio"))
        else:
            flash("Credenciales inválidas o usuario inactivo.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Has cerrado sesión exitosamente.", "info")
    return redirect(url_for("login"))

# ==============================================================================
# 5.1 PERFIL DE USUARIO (autoservicio de contraseña)
# ==============================================================================
@app.route("/perfil/cambiar_password", methods=["POST"])
def cambiar_password():
    """Cualquier usuario logueado (Administrador o Chofer) cambia su propia
    contraseña, confirmando la actual."""
    password_actual = request.form.get("password_actual", "")
    password_nueva = request.form.get("password_nueva", "")
    confirmar = request.form.get("confirmar_password", "")
    destino = request.referrer or url_for("inicio")

    if len(password_nueva) < 8:
        flash("La nueva contraseña debe tener al menos 8 caracteres.", "danger")
        return redirect(destino)
    if password_nueva != confirmar:
        flash("La confirmación no coincide con la nueva contraseña.", "danger")
        return redirect(destino)

    with engine.begin() as conn:
        usuario = conn.execute(text(
            "SELECT password FROM usuarios WHERE id_usuario = :id"
        ), {"id": session["usuario_id"]}).mappings().fetchone()

        if not usuario or not check_password_hash(usuario["password"], password_actual):
            flash("La contraseña actual no es correcta.", "danger")
            return redirect(destino)

        conn.execute(text(
            "UPDATE usuarios SET password = :pwd WHERE id_usuario = :id"
        ), {"pwd": generate_password_hash(password_nueva), "id": session["usuario_id"]})

    flash("Contraseña actualizada correctamente.", "success")
    return redirect(destino)


@app.route("/perfil/cambio_obligatorio", methods=["GET", "POST"])
def cambio_obligatorio_password():
    """Retiene a un usuario con cambio de contraseña pendiente (el admin en su
    primer ingreso) hasta que define una contraseña propia. No pide la
    contraseña actual porque el usuario acaba de autenticarse con ella. El
    mínimo de 8 caracteres impide, además, dejar la clave inicial 'admin'."""
    if "usuario_id" not in session:
        return redirect(url_for("login"))
    if not session.get("debe_cambiar_clave"):
        return redirect(url_for("inicio"))

    if request.method == "POST":
        password_nueva = request.form.get("password_nueva", "")
        confirmar = request.form.get("confirmar_password", "")

        if len(password_nueva) < 8:
            flash("La nueva contraseña debe tener al menos 8 caracteres.", "danger")
            return redirect(url_for("cambio_obligatorio_password"))
        if password_nueva != confirmar:
            flash("La confirmación no coincide con la nueva contraseña.", "danger")
            return redirect(url_for("cambio_obligatorio_password"))

        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE usuarios SET password = :pwd, debe_cambiar_clave = false "
                "WHERE id_usuario = :id"
            ), {"pwd": generate_password_hash(password_nueva), "id": session["usuario_id"]})

        session["debe_cambiar_clave"] = False
        flash("Contraseña actualizada. Ya puedes usar el sistema.", "success")
        return redirect(url_for("inicio"))

    return render_template("cambio_obligatorio.html")

# ==============================================================================
# 6. DASHBOARD PRINCIPAL (RBAC)
# ==============================================================================
@app.route("/")
def inicio():
    with engine.connect() as conn:
        if session.get("rol") == "Administrador":
            rutas_activas = conn.execute(
                text("SELECT COUNT(*) AS total FROM envios WHERE estado_envio = 'En Ruta'")
            ).scalar_one()
            vehiculos_disp = conn.execute(
                text("SELECT COUNT(*) AS total FROM vehiculos WHERE estado = 'Disponible'")
            ).scalar_one()
            envios_finalizados = conn.execute(
                text("SELECT COUNT(*) AS total FROM envios WHERE estado_envio = 'Entregado'")
            ).scalar_one()

            # Ganancia neta del mes en curso: mismo criterio que el reporte
            # mensual en PDF (COALESCE(fecha_llegada, fecha_creacion) para
            # decidir el mes, se excluyen solo las rutas Anuladas), para que
            # esta tarjeta y el reporte exportable siempre coincidan.
            hoy = date.today()
            ganancia_mes = conn.execute(text(
                "SELECT COALESCE(SUM("
                "  e.costo_flete - e.costo_estimado_combustible - e.costo_estimado_mantenimiento "
                "  - COALESCE(gastos_bitacora.gastos_ruta, 0)"
                "), 0) "
                "FROM envios e "
                "LEFT JOIN " + _SUBCONSULTA_GASTOS_RUTA + " ON gastos_bitacora.id_envio = e.id_envio "
                "WHERE e.estado_envio != 'Anulado' "
                "  AND EXTRACT(YEAR FROM COALESCE(e.fecha_llegada, e.fecha_creacion)) = :anio "
                "  AND EXTRACT(MONTH FROM COALESCE(e.fecha_llegada, e.fecha_creacion)) = :mes"
            ), {"anio": hoy.year, "mes": hoy.month}).scalar_one()

            # Listas para poblar los desplegables de los modales.
            clientes = conn.execute(
                text("SELECT id_cliente, razon_social FROM clientes ORDER BY razon_social")
            ).mappings().fetchall()
            vehiculos = conn.execute(
                text("SELECT id_vehiculo, placa, modelo, km_por_litro, costo_mantenimiento_km "
                     "FROM vehiculos WHERE estado = 'Disponible' ORDER BY placa")
            ).mappings().fetchall()
            choferes = conn.execute(
                text("SELECT id_usuario, nombre_completo FROM usuarios "
                     "WHERE rol = 'Chofer' AND estado = 'Activo' ORDER BY nombre_completo")
            ).mappings().fetchall()
            config_financiera = _obtener_configuracion_financiera(conn)

            return render_template(
                "dashboard_admin.html",
                rutas=rutas_activas,
                vehiculos=vehiculos_disp,
                finalizados=envios_finalizados,
                ganancia_mes=float(ganancia_mes),
                lista_clientes=clientes,
                lista_vehiculos=vehiculos,
                lista_choferes=choferes,
                precio_combustible=float(config_financiera["precio_combustible"]),
                tipo_ganancia=config_financiera["tipo_ganancia"],
                valor_ganancia=float(config_financiera["valor_ganancia"]),
            )
        else:
            # Solo se muestra una ruta realmente activa (Pendiente o En Ruta).
            # Excluir explícitamente 'Entregado' y 'Anulado' — antes se usaba
            # "!= 'Entregado'", que dejaba pasar rutas Anuladas y, si el
            # chofer tenía una ruta vieja anulada, el ORDER BY + LIMIT 1
            # mostraba esa en vez de la nueva ruta activa.
            consulta_ruta = text(
                "SELECT * FROM envios WHERE id_chofer = :chofer_id "
                "AND estado_envio IN ('Pendiente', 'En Ruta') "
                "ORDER BY fecha_creacion DESC LIMIT 1"
            )
            ruta_actual = conn.execute(
                consulta_ruta, {"chofer_id": session["usuario_id"]}
            ).mappings().fetchone()
            return render_template("dashboard_chofer.html", ruta=ruta_actual)

# ==============================================================================
# 7. MÓDULOS DE ADMINISTRACIÓN (VISTAS)
# ==============================================================================
@app.route("/gestion")
def gestion():
    solo_admin()
    with engine.connect() as conn:
        choferes = conn.execute(text(
            "SELECT id_usuario, nombre_completo, usuario, vencimiento_licencia, "
            "       vencimiento_certificado_medico, vencimiento_cedula, estado "
            "FROM usuarios WHERE rol = 'Chofer' ORDER BY nombre_completo"
        )).mappings().fetchall()
        vehiculos = conn.execute(text(
            "SELECT id_vehiculo, placa, modelo, capacidad_carga, km_por_litro, "
            "       costo_mantenimiento_km, km_para_mantenimiento, estado, "
            "       vencimiento_rcv, vencimiento_impuesto_alcaldia "
            "FROM vehiculos ORDER BY placa"
        )).mappings().fetchall()
        envios = conn.execute(text(
            "SELECT e.id_envio, e.id_cliente, e.id_vehiculo, e.id_chofer, "
            "       c.razon_social AS cliente, v.placa AS vehiculo, "
            "       u.nombre_completo AS chofer, e.destino, e.distancia_km, e.costo_flete, "
            "       e.costo_estimado_combustible, e.costo_estimado_mantenimiento, "
            "       e.estado_envio, e.fecha_creacion, e.fecha_salida, e.fecha_llegada "
            "FROM envios e "
            "JOIN clientes c  ON c.id_cliente  = e.id_cliente "
            "JOIN vehiculos v ON v.id_vehiculo = e.id_vehiculo "
            "JOIN usuarios u  ON u.id_usuario  = e.id_chofer "
            "ORDER BY e.fecha_creacion DESC"
        )).mappings().fetchall()
        clientes = conn.execute(text(
            "SELECT id_cliente, razon_social FROM clientes ORDER BY razon_social"
        )).mappings().fetchall()
    return render_template(
        "gestion_tablas.html",
        choferes=choferes, vehiculos=vehiculos, envios=envios,
        clientes=clientes, estados_editables=ESTADOS_ENVIO_EDITABLES,
    )


@app.route("/gestion/envio/<int:id_envio>")
def ver_envio(id_envio):
    solo_admin()
    with engine.connect() as conn:
        envio = conn.execute(text(
            "SELECT e.*, c.razon_social AS cliente, v.placa AS vehiculo, "
            "       v.modelo AS vehiculo_modelo, u.nombre_completo AS chofer "
            "FROM envios e "
            "JOIN clientes c  ON c.id_cliente  = e.id_cliente "
            "JOIN vehiculos v ON v.id_vehiculo = e.id_vehiculo "
            "JOIN usuarios u  ON u.id_usuario  = e.id_chofer "
            "WHERE e.id_envio = :id"
        ), {"id": id_envio}).mappings().fetchone()

        if not envio:
            abort(404)

        bitacora = conn.execute(text(
            "SELECT tipo_evento, descripcion, cantidad_combustible, monto_valor, "
            "       fecha_hora_registro "
            "FROM bitacora_rutas WHERE id_envio = :id ORDER BY fecha_hora_registro"
        ), {"id": id_envio}).mappings().fetchall()

        gastos_ruta = _gastos_ruta(conn, id_envio)

    ganancia_neta = _ganancia_neta(
        envio["costo_flete"], envio["costo_estimado_combustible"],
        envio["costo_estimado_mantenimiento"], gastos_ruta,
    )
    return render_template(
        "detalle_envio.html", envio=envio, bitacora=bitacora,
        gastos_ruta=gastos_ruta, ganancia_neta=ganancia_neta,
    )


@app.route("/empleados")
def empleados():
    solo_admin()
    with engine.connect() as conn:
        lista = conn.execute(text(
            "SELECT id_usuario, nombre_completo, usuario, rol, estado "
            "FROM usuarios WHERE rol != 'Chofer' ORDER BY nombre_completo"
        )).mappings().fetchall()
    return render_template("lista_usuarios.html", titulo="Empleados", usuarios=lista)


@app.route("/choferes")
def choferes():
    solo_admin()
    return redirect(url_for("gestion"))


@app.route("/configuracion", methods=["POST"])
def configuracion():
    """Guarda el precio del combustible y el margen de ganancia. Se edita
    desde el modal "Configuración Financiera" en el panel principal — ya no
    es una página propia."""
    solo_admin()
    try:
        precio_combustible = float(request.form["precio_combustible"])
        tipo_ganancia = request.form.get("tipo_ganancia", "")
        valor_ganancia = float(request.form["valor_ganancia"])
        if precio_combustible <= 0:
            raise ValueError("El precio del combustible debe ser mayor que cero.")
        if tipo_ganancia not in ("porcentaje", "fijo"):
            raise ValueError("Selecciona un tipo de ganancia válido.")
        if valor_ganancia < 0:
            raise ValueError("El valor de ganancia no puede ser negativo.")
    except (KeyError, ValueError):
        flash("Datos de configuración inválidos. Revisa los campos.", "danger")
        return redirect(url_for("inicio"))

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE configuracion_financiera SET precio_combustible = :pc, "
            "tipo_ganancia = :tg, valor_ganancia = :vg WHERE id = 1"
        ), {"pc": precio_combustible, "tg": tipo_ganancia, "vg": valor_ganancia})
    flash("Configuración financiera actualizada.", "success")
    return redirect(url_for("inicio"))

# ==============================================================================
# 8. ENDPOINTS DE ALTA (INSERT REALES)
# ==============================================================================
@app.route("/agregar_cliente", methods=["POST"])
def agregar_cliente():
    solo_admin()
    try:
        razon_social = request.form["razon_social"].strip()
        direccion_principal = request.form["direccion_principal"].strip()
        telefono = request.form.get("telefono", "").strip() or None
        if not razon_social or not direccion_principal:
            raise ValueError("Razón social y dirección son obligatorias.")
    except (KeyError, ValueError):
        flash("Datos del cliente inválidos. Revisa los campos.", "danger")
        return redirect(url_for("inicio"))

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO clientes (razon_social, direccion_principal, telefono) "
                "VALUES (:razon, :dir, :tel)"
            ), {"razon": razon_social, "dir": direccion_principal, "tel": telefono})
        flash(f"Cliente {razon_social} registrado.", "success")
    except (IntegrityError, SQLAlchemyError):
        flash("No se pudo registrar el cliente (datos inválidos o duplicados).", "danger")
    return redirect(url_for("inicio"))


@app.route("/agregar_chofer", methods=["POST"])
def agregar_chofer():
    solo_admin()
    try:
        nombre_completo = request.form["nombre_completo"].strip()
        usuario_login = request.form["usuario"].strip()
        password = request.form["password"]
        if not nombre_completo or not usuario_login or not password:
            raise ValueError("Nombre, usuario y contraseña son obligatorios.")
        password_hash = generate_password_hash(password)
    except (KeyError, ValueError):
        flash("Datos del chofer inválidos. Revisa los campos.", "danger")
        return redirect(url_for("inicio"))

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO usuarios "
                "(nombre_completo, usuario, password, rol, estado, "
                " vencimiento_licencia, vencimiento_certificado_medico, vencimiento_cedula) "
                "VALUES (:nombre, :usuario, :pwd, 'Chofer', 'Activo', "
                " :lic, :med, :ced)"
            ), {
                "nombre": nombre_completo,
                "usuario": usuario_login,
                "pwd": password_hash,
                "lic": request.form.get("vencimiento_licencia") or None,
                "med": request.form.get("vencimiento_certificado_medico") or None,
                "ced": request.form.get("vencimiento_cedula") or None,
            })
        flash(f"Chofer {nombre_completo} registrado exitosamente.", "success")
    except IntegrityError:
        flash("El nombre de usuario ya existe. Elige otro.", "danger")
    except SQLAlchemyError:
        flash("No se pudo registrar el chofer (datos inválidos).", "danger")
    return redirect(url_for("inicio"))


@app.route("/agregar_vehiculo", methods=["POST"])
def agregar_vehiculo():
    solo_admin()
    try:
        placa = request.form["placa"].strip().upper()
        modelo = request.form["modelo"].strip()
        km_por_litro = float(request.form["km_por_litro"])
        costo_mantenimiento_km = float(request.form["costo_mantenimiento_km"])
        km_para_mantenimiento = int(request.form["km_para_mantenimiento"])
        capacidad_raw = request.form.get("capacidad_carga", "").strip()
        capacidad_carga = float(capacidad_raw) if capacidad_raw else None

        if not placa or not modelo:
            raise ValueError("Placa y modelo son obligatorios.")
        if km_por_litro <= 0:
            raise ValueError("El rendimiento (Km/L) debe ser mayor que cero.")
        if costo_mantenimiento_km < 0 or km_para_mantenimiento <= 0:
            raise ValueError("Los datos de mantenimiento deben ser válidos.")
        if capacidad_carga is not None and capacidad_carga < 0:
            raise ValueError("La capacidad de carga no puede ser negativa.")
    except (KeyError, ValueError):
        flash("Datos del vehículo inválidos. Revisa los campos numéricos.", "danger")
        return redirect(url_for("inicio"))

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO vehiculos "
                "(placa, modelo, capacidad_carga, km_por_litro, costo_mantenimiento_km, "
                " km_para_mantenimiento, estado, vencimiento_rcv, vencimiento_impuesto_alcaldia) "
                "VALUES (:placa, :modelo, :cap, :kml, :cmk, :kmm, 'Disponible', :rcv, :imp)"
            ), {
                "placa": placa,
                "modelo": modelo,
                "cap": capacidad_carga,
                "kml": km_por_litro,
                "cmk": costo_mantenimiento_km,
                "kmm": km_para_mantenimiento,
                "rcv": request.form.get("vencimiento_rcv") or None,
                "imp": request.form.get("vencimiento_impuesto_alcaldia") or None,
            })
        flash(f"Vehículo placa {placa} registrado exitosamente.", "success")
    except IntegrityError:
        flash("La placa ya está registrada.", "danger")
    except SQLAlchemyError:
        flash("No se pudo registrar el vehículo (datos inválidos).", "danger")
    return redirect(url_for("inicio"))


@app.route("/crear_envio", methods=["POST"])
def crear_envio():
    solo_admin()
    try:
        id_cliente = int(request.form["id_cliente"])
        id_vehiculo = int(request.form["id_vehiculo"])
        id_chofer = int(request.form["id_chofer"])
        distancia = float(request.form["distancia_km"])
        costo_flete = float(request.form["costo_flete"])
        if distancia <= 0:
            raise ValueError("La distancia debe ser positiva.")
        if costo_flete <= 0:
            raise ValueError("El costo del flete (cobrado al cliente) debe ser mayor que cero.")
    except (KeyError, ValueError):
        flash("Datos del envío inválidos. Revisa los campos.", "danger")
        return redirect(url_for("inicio"))

    try:
        with engine.begin() as conn:
            # FOR UPDATE bloquea la fila hasta el commit: evita que dos
            # administradores asignen el mismo vehículo en solicitudes simultáneas.
            vehiculo = conn.execute(text(
                "SELECT km_por_litro, costo_mantenimiento_km, estado, "
                "       vencimiento_rcv, vencimiento_impuesto_alcaldia "
                "FROM vehiculos WHERE id_vehiculo = :id FOR UPDATE"
            ), {"id": id_vehiculo}).mappings().fetchone()

            if not vehiculo:
                flash("El vehículo seleccionado no existe.", "danger")
                return redirect(url_for("inicio"))
            if vehiculo["estado"] != "Disponible":
                flash("El vehículo seleccionado no está disponible.", "warning")
                return redirect(url_for("inicio"))

            ok, error_doc = _validar_documentos_vehiculo(vehiculo)
            if not ok:
                flash(error_doc, "danger")
                return redirect(url_for("inicio"))

            chofer = conn.execute(text(
                "SELECT estado, vencimiento_licencia, vencimiento_certificado_medico, "
                "       vencimiento_cedula "
                "FROM usuarios WHERE id_usuario = :id AND rol = 'Chofer'"
            ), {"id": id_chofer}).mappings().fetchone()

            if not chofer:
                flash("El chofer seleccionado no existe.", "danger")
                return redirect(url_for("inicio"))
            if chofer["estado"] != "Activo":
                flash("El chofer seleccionado no está activo.", "warning")
                return redirect(url_for("inicio"))

            ok, error_doc = _validar_documentos_chofer(chofer)
            if not ok:
                flash(error_doc, "danger")
                return redirect(url_for("inicio"))

            if _chofer_con_ruta_activa(conn, id_chofer):
                flash("El chofer ya tiene una ruta activa asignada.", "warning")
                return redirect(url_for("inicio"))

            # Cálculo de costos estimados (columnas NOT NULL en el esquema).
            config_financiera = _obtener_configuracion_financiera(conn)
            costo_combustible, costo_mantenimiento = _calcular_costos_ruta(
                distancia, vehiculo, config_financiera
            )

            conn.execute(text(
                "INSERT INTO envios "
                "(id_cliente, id_vehiculo, id_chofer, destino, distancia_km, costo_flete, "
                " costo_estimado_combustible, costo_estimado_mantenimiento, estado_envio) "
                "VALUES (:cli, :veh, :cho, :dest, :dist, :flete, :ccomb, :cmant, 'Pendiente')"
            ), {
                "cli": id_cliente, "veh": id_vehiculo, "cho": id_chofer,
                "dest": request.form["destino"].strip(), "dist": distancia,
                "flete": costo_flete,
                "ccomb": costo_combustible, "cmant": costo_mantenimiento,
            })
            # El vehículo queda asignado hasta que la ruta se entregue.
            conn.execute(text(
                "UPDATE vehiculos SET estado = 'Asignado' WHERE id_vehiculo = :id"
            ), {"id": id_vehiculo})

        flash(f"Ruta hacia {request.form['destino']} creada y asignada.", "success")
    except IntegrityError:
        flash("Error de integridad: cliente, vehículo o chofer inválido.", "danger")
    except SQLAlchemyError:
        flash("No se pudo crear el envío.", "danger")
    return redirect(url_for("inicio"))

# ==============================================================================
# 8.1 EDICIÓN Y BAJA ADMINISTRATIVA
# ------------------------------------------------------------------------------
# Endpoints para corregir registros ya existentes desde "Gestión de Tablas":
# editar choferes/vehículos, resetear contraseñas, corregir o anular rutas.
# ==============================================================================
@app.route("/gestion/chofer/<int:id_usuario>/editar", methods=["POST"])
def editar_chofer(id_usuario):
    solo_admin()
    try:
        nombre_completo = request.form["nombre_completo"].strip()
        usuario_login = request.form["usuario"].strip()
        estado = request.form.get("estado", "Activo")
        nueva_password = request.form.get("password", "").strip()
        if not nombre_completo or not usuario_login:
            raise ValueError("Nombre y usuario son obligatorios.")
        if estado not in ("Activo", "Inactivo"):
            raise ValueError("Estado inválido.")
        if nueva_password and len(nueva_password) < 8:
            raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    except (KeyError, ValueError) as error:
        flash(f"Datos del chofer inválidos: {error}", "danger")
        return redirect(url_for("gestion"))

    campos = {
        "id": id_usuario,
        "nombre": nombre_completo,
        "usuario": usuario_login,
        "estado": estado,
        "lic": request.form.get("vencimiento_licencia") or None,
        "med": request.form.get("vencimiento_certificado_medico") or None,
        "ced": request.form.get("vencimiento_cedula") or None,
    }
    set_password_sql = ""
    if nueva_password:
        campos["pwd"] = generate_password_hash(nueva_password)
        set_password_sql = ", password = :pwd"

    try:
        with engine.begin() as conn:
            resultado = conn.execute(text(
                "UPDATE usuarios SET nombre_completo = :nombre, usuario = :usuario, "
                "estado = :estado, vencimiento_licencia = :lic, "
                "vencimiento_certificado_medico = :med, vencimiento_cedula = :ced"
                + set_password_sql +
                " WHERE id_usuario = :id AND rol = 'Chofer'"
            ), campos)
            if resultado.rowcount == 0:
                flash("El chofer indicado no existe.", "danger")
                return redirect(url_for("gestion"))
        flash(f"Chofer {nombre_completo} actualizado.", "success")
    except IntegrityError:
        flash("El nombre de usuario ya existe. Elige otro.", "danger")
    except SQLAlchemyError:
        flash("No se pudo actualizar el chofer.", "danger")
    return redirect(url_for("gestion"))


@app.route("/gestion/vehiculo/<int:id_vehiculo>/editar", methods=["POST"])
def editar_vehiculo(id_vehiculo):
    solo_admin()
    try:
        placa = request.form["placa"].strip().upper()
        modelo = request.form["modelo"].strip()
        km_por_litro = float(request.form["km_por_litro"])
        costo_mantenimiento_km = float(request.form["costo_mantenimiento_km"])
        km_para_mantenimiento = int(request.form["km_para_mantenimiento"])
        capacidad_raw = request.form.get("capacidad_carga", "").strip()
        capacidad_carga = float(capacidad_raw) if capacidad_raw else None
        estado_solicitado = request.form.get("estado", "Disponible")

        if not placa or not modelo:
            raise ValueError("Placa y modelo son obligatorios.")
        if km_por_litro <= 0:
            raise ValueError("El rendimiento (Km/L) debe ser mayor que cero.")
        if costo_mantenimiento_km < 0 or km_para_mantenimiento <= 0:
            raise ValueError("Los datos de mantenimiento deben ser válidos.")
        if capacidad_carga is not None and capacidad_carga < 0:
            raise ValueError("La capacidad de carga no puede ser negativa.")
        if estado_solicitado not in ("Disponible", "Fuera de Servicio"):
            raise ValueError("Estado inválido.")
    except (KeyError, ValueError):
        flash("Datos del vehículo inválidos. Revisa los campos numéricos.", "danger")
        return redirect(url_for("gestion"))

    try:
        with engine.begin() as conn:
            actual = conn.execute(text(
                "SELECT estado FROM vehiculos WHERE id_vehiculo = :id FOR UPDATE"
            ), {"id": id_vehiculo}).mappings().fetchone()

            if not actual:
                flash("El vehículo indicado no existe.", "danger")
                return redirect(url_for("gestion"))

            # El estado "Asignado" lo controla el sistema (ruta activa); el
            # admin solo puede alternar Disponible <-> Fuera de Servicio
            # cuando el vehículo no está en ruta.
            estado_final = actual["estado"]
            if actual["estado"] == "Asignado":
                if estado_solicitado == "Fuera de Servicio":
                    flash(
                        "No se puede poner Fuera de Servicio un vehículo "
                        "actualmente asignado a una ruta activa.", "warning"
                    )
            else:
                estado_final = estado_solicitado

            conn.execute(text(
                "UPDATE vehiculos SET placa = :placa, modelo = :modelo, "
                "capacidad_carga = :cap, km_por_litro = :kml, "
                "costo_mantenimiento_km = :cmk, km_para_mantenimiento = :kmm, "
                "vencimiento_rcv = :rcv, vencimiento_impuesto_alcaldia = :imp, "
                "estado = :estado "
                "WHERE id_vehiculo = :id"
            ), {
                "placa": placa, "modelo": modelo, "cap": capacidad_carga,
                "kml": km_por_litro, "cmk": costo_mantenimiento_km,
                "kmm": km_para_mantenimiento,
                "rcv": request.form.get("vencimiento_rcv") or None,
                "imp": request.form.get("vencimiento_impuesto_alcaldia") or None,
                "estado": estado_final, "id": id_vehiculo,
            })
        flash(f"Vehículo {placa} actualizado.", "success")
    except IntegrityError:
        flash("La placa ya está registrada por otro vehículo.", "danger")
    except SQLAlchemyError:
        flash("No se pudo actualizar el vehículo.", "danger")
    return redirect(url_for("gestion"))


@app.route("/gestion/usuario/<int:id_usuario>/resetear_password", methods=["POST"])
def resetear_password(id_usuario):
    """El administrador fija una nueva contraseña para cualquier usuario
    (chofer o empleado/administrador) sin necesidad de conocer la anterior."""
    solo_admin()
    destino = request.referrer or url_for("gestion")
    nueva_password = request.form.get("password", "")
    if len(nueva_password) < 8:
        flash("La nueva contraseña debe tener al menos 8 caracteres.", "danger")
        return redirect(destino)

    with engine.begin() as conn:
        resultado = conn.execute(text(
            "UPDATE usuarios SET password = :pwd WHERE id_usuario = :id"
        ), {"pwd": generate_password_hash(nueva_password), "id": id_usuario})

    if resultado.rowcount == 0:
        flash("El usuario indicado no existe.", "danger")
    else:
        flash("Contraseña actualizada correctamente.", "success")
    return redirect(destino)


@app.route("/gestion/usuario/<int:id_usuario>/estado", methods=["POST"])
def cambiar_estado_usuario(id_usuario):
    """Activa/desactiva un usuario (empleados y administradores; los choferes
    tienen su propio toggle dentro de 'editar_chofer')."""
    solo_admin()
    destino = request.referrer or url_for("empleados")
    estado_nuevo = request.form.get("estado")
    if estado_nuevo not in ("Activo", "Inactivo"):
        flash("Estado inválido.", "danger")
        return redirect(destino)

    with engine.begin() as conn:
        resultado = conn.execute(text(
            "UPDATE usuarios SET estado = :estado WHERE id_usuario = :id"
        ), {"estado": estado_nuevo, "id": id_usuario})

    if resultado.rowcount == 0:
        flash("El usuario indicado no existe.", "danger")
    else:
        flash(f"Estado actualizado a {estado_nuevo}.", "success")
    return redirect(destino)


@app.route("/gestion/envio/<int:id_envio>/editar", methods=["POST"])
def editar_envio(id_envio):
    """Corrige una ruta Pendiente o En Ruta: destino, distancia, cliente,
    vehículo y/o chofer, revalidando disponibilidad y documentos vigentes."""
    solo_admin()
    try:
        id_cliente = int(request.form["id_cliente"])
        id_vehiculo_nuevo = int(request.form["id_vehiculo"])
        id_chofer_nuevo = int(request.form["id_chofer"])
        distancia = float(request.form["distancia_km"])
        costo_flete = float(request.form["costo_flete"])
        destino = request.form["destino"].strip()
        if distancia <= 0 or not destino:
            raise ValueError("Datos inválidos.")
        if costo_flete <= 0:
            raise ValueError("El costo del flete (cobrado al cliente) debe ser mayor que cero.")
    except (KeyError, ValueError):
        flash("Datos del envío inválidos. Revisa los campos.", "danger")
        return redirect(url_for("gestion"))

    try:
        with engine.begin() as conn:
            envio = conn.execute(text(
                "SELECT id_vehiculo, estado_envio FROM envios "
                "WHERE id_envio = :id FOR UPDATE"
            ), {"id": id_envio}).mappings().fetchone()

            if not envio:
                flash("El envío indicado no existe.", "danger")
                return redirect(url_for("gestion"))
            if envio["estado_envio"] not in ESTADOS_ENVIO_EDITABLES:
                flash("Solo se pueden editar rutas Pendientes o En Ruta.", "warning")
                return redirect(url_for("gestion"))

            id_vehiculo_anterior = envio["id_vehiculo"]
            cambia_vehiculo = id_vehiculo_nuevo != id_vehiculo_anterior

            # Se bloquea la fila del vehículo (nuevo o el mismo) para evitar
            # que otra petición lo asigne al mismo tiempo.
            vehiculo = conn.execute(text(
                "SELECT km_por_litro, costo_mantenimiento_km, estado, "
                "       vencimiento_rcv, vencimiento_impuesto_alcaldia "
                "FROM vehiculos WHERE id_vehiculo = :id FOR UPDATE"
            ), {"id": id_vehiculo_nuevo}).mappings().fetchone()

            if not vehiculo:
                flash("El vehículo seleccionado no existe.", "danger")
                return redirect(url_for("gestion"))
            if cambia_vehiculo and vehiculo["estado"] != "Disponible":
                flash("El vehículo seleccionado no está disponible.", "warning")
                return redirect(url_for("gestion"))

            ok, error_doc = _validar_documentos_vehiculo(vehiculo)
            if not ok:
                flash(error_doc, "danger")
                return redirect(url_for("gestion"))

            chofer = conn.execute(text(
                "SELECT estado, vencimiento_licencia, vencimiento_certificado_medico, "
                "       vencimiento_cedula "
                "FROM usuarios WHERE id_usuario = :id AND rol = 'Chofer'"
            ), {"id": id_chofer_nuevo}).mappings().fetchone()

            if not chofer:
                flash("El chofer seleccionado no existe.", "danger")
                return redirect(url_for("gestion"))
            if chofer["estado"] != "Activo":
                flash("El chofer seleccionado no está activo.", "warning")
                return redirect(url_for("gestion"))

            ok, error_doc = _validar_documentos_chofer(chofer)
            if not ok:
                flash(error_doc, "danger")
                return redirect(url_for("gestion"))

            if _chofer_con_ruta_activa(conn, id_chofer_nuevo, excluir_id_envio=id_envio):
                flash("El chofer ya tiene otra ruta activa asignada.", "warning")
                return redirect(url_for("gestion"))

            config_financiera = _obtener_configuracion_financiera(conn)
            costo_combustible, costo_mantenimiento = _calcular_costos_ruta(
                distancia, vehiculo, config_financiera
            )

            conn.execute(text(
                "UPDATE envios SET id_cliente = :cli, id_vehiculo = :veh, "
                "id_chofer = :cho, destino = :dest, distancia_km = :dist, "
                "costo_flete = :flete, "
                "costo_estimado_combustible = :ccomb, "
                "costo_estimado_mantenimiento = :cmant "
                "WHERE id_envio = :id"
            ), {
                "cli": id_cliente, "veh": id_vehiculo_nuevo, "cho": id_chofer_nuevo,
                "dest": destino, "dist": distancia,
                "flete": costo_flete,
                "ccomb": costo_combustible, "cmant": costo_mantenimiento,
                "id": id_envio,
            })

            if cambia_vehiculo:
                _liberar_vehiculo(conn, id_vehiculo_anterior)
                conn.execute(text(
                    "UPDATE vehiculos SET estado = 'Asignado' WHERE id_vehiculo = :id"
                ), {"id": id_vehiculo_nuevo})

        flash("Ruta actualizada correctamente.", "success")
    except IntegrityError:
        flash("Error de integridad: cliente, vehículo o chofer inválido.", "danger")
    except SQLAlchemyError:
        flash("No se pudo actualizar el envío.", "danger")
    return redirect(url_for("gestion"))


@app.route("/gestion/envio/<int:id_envio>/anular", methods=["POST"])
def anular_envio(id_envio):
    """Anula una ruta Pendiente o En Ruta y libera el vehículo asignado."""
    solo_admin()
    try:
        with engine.begin() as conn:
            envio = conn.execute(text(
                "SELECT id_vehiculo, estado_envio FROM envios "
                "WHERE id_envio = :id FOR UPDATE"
            ), {"id": id_envio}).mappings().fetchone()

            if not envio:
                flash("El envío indicado no existe.", "danger")
                return redirect(url_for("gestion"))
            if envio["estado_envio"] not in ESTADOS_ENVIO_EDITABLES:
                flash("Solo se pueden anular rutas Pendientes o En Ruta.", "warning")
                return redirect(url_for("gestion"))

            conn.execute(text(
                "UPDATE envios SET estado_envio = 'Anulado' WHERE id_envio = :id"
            ), {"id": id_envio})
            _liberar_vehiculo(conn, envio["id_vehiculo"])

        flash("Ruta anulada. El vehículo quedó disponible nuevamente.", "success")
    except SQLAlchemyError:
        flash("No se pudo anular la ruta.", "danger")
    return redirect(url_for("gestion"))

# ==============================================================================
# 9. API REST PARA EL MÓVIL (BITÁCORA TRANSACCIONAL)
# ==============================================================================
@app.route("/api/incidencia", methods=["POST"])
def registrar_incidencia():
    """Recibe eventos JSON desde el móvil del chofer en la LAN."""
    if session.get("rol") != "Chofer":
        return jsonify({"error": "Acceso denegado, rol insuficiente"}), 403

    datos = request.get_json(silent=True) or {}
    id_envio = datos.get("id_envio")
    tipo_evento = datos.get("tipo_evento")

    # --- Validación de entrada ---
    if tipo_evento not in EVENTOS_VALIDOS:
        return jsonify({"error": "Tipo de evento no reconocido."}), 400
    try:
        id_envio = int(id_envio)
    except (TypeError, ValueError):
        return jsonify({"error": "Identificador de envío inválido."}), 400

    # Cantidad (litros) y monto solo aplican a eventos de gasto.
    def _num(valor):
        try:
            n = float(valor)
            return n if n >= 0 else None
        except (TypeError, ValueError):
            return None

    cantidad = _num(datos.get("cantidad", 0)) or 0
    monto = _num(datos.get("precio", 0)) or 0

    if tipo_evento in EVENTOS_GASTO and monto <= 0:
        return jsonify({"error": "El monto del gasto debe ser mayor que cero."}), 400

    # "Peaje" y "Novedad" llevan un texto libre que ingresa el chofer (nombre
    # del peaje / descripción de la falla mecánica); los demás eventos usan
    # una descripción fija.
    if tipo_evento in ("Peaje", "Novedad"):
        descripcion = (datos.get("descripcion") or "").strip()
        if not descripcion:
            etiqueta = "el nombre del peaje" if tipo_evento == "Peaje" else "la descripción de la falla"
            return jsonify({"error": f"Debes indicar {etiqueta}."}), 400
    else:
        descripcion = {
            "Salida": "Inicio de ruta",
            "Llegada": "Fin de ruta - entrega realizada",
            "Gasolina": "Carga de combustible",
        }.get(tipo_evento)

    try:
        with engine.begin() as conn:
            # El envío debe existir, pertenecer a este chofer y no estar cerrado.
            envio = conn.execute(text(
                "SELECT estado_envio, id_vehiculo FROM envios "
                "WHERE id_envio = :id AND id_chofer = :chofer"
            ), {"id": id_envio, "chofer": session["usuario_id"]}).mappings().fetchone()

            if not envio:
                return jsonify({"error": "Ruta no encontrada o no asignada a este chofer."}), 404
            if envio["estado_envio"] == "Entregado":
                return jsonify({
                    "error": "La ruta ya está finalizada. Prohibida la inserción de nuevos registros."
                }), 400

            conn.execute(text(
                "INSERT INTO bitacora_rutas "
                "(id_envio, tipo_evento, descripcion, cantidad_combustible, monto_valor) "
                "VALUES (:envio, :evento, :desc, :cant, :monto)"
            ), {
                "envio": id_envio, "evento": tipo_evento, "desc": descripcion,
                "cant": cantidad if tipo_evento == "Gasolina" else None,
                "monto": monto if tipo_evento in EVENTOS_GASTO else None,
            })

            # Transiciones de estado del envío.
            if tipo_evento == "Salida" and envio["estado_envio"] == "Pendiente":
                conn.execute(text(
                    "UPDATE envios SET estado_envio = 'En Ruta', "
                    "fecha_salida = CURRENT_TIMESTAMP WHERE id_envio = :envio"
                ), {"envio": id_envio})
            elif tipo_evento == "Llegada":
                conn.execute(text(
                    "UPDATE envios SET estado_envio = 'Entregado', "
                    "fecha_llegada = CURRENT_TIMESTAMP WHERE id_envio = :envio"
                ), {"envio": id_envio})
                # Liberar el vehículo al finalizar la ruta.
                _liberar_vehiculo(conn, envio["id_vehiculo"])

        return jsonify({"status": "success", "mensaje": f"Evento {tipo_evento} procesado en DB."}), 200

    except IntegrityError:
        return jsonify({"error": "Error de integridad en PostgreSQL (llave foránea inválida)."}), 500
    except SQLAlchemyError:
        return jsonify({"error": "Error al registrar el evento en la base de datos."}), 500

# ==============================================================================
# 9.1 ESTADÍSTICAS PARA EL DASHBOARD (KPIs, gráfico anual, rankings)
# ------------------------------------------------------------------------------
# Todo se calcula sobre envíos "Entregado": el objetivo es medir productividad
# real, no planificación futura (rutas Pendientes/En Ruta no cuentan aquí).
# ==============================================================================
@app.route("/api/estadisticas")
def api_estadisticas():
    solo_admin()
    with engine.connect() as conn:
        filas_mes = conn.execute(text(
            "SELECT EXTRACT(MONTH FROM COALESCE(fecha_llegada, fecha_creacion))::int AS mes, "
            "       COUNT(*) AS total "
            "FROM envios "
            "WHERE estado_envio = 'Entregado' "
            "  AND EXTRACT(YEAR FROM COALESCE(fecha_llegada, fecha_creacion)) "
            "      = EXTRACT(YEAR FROM CURRENT_DATE) "
            "GROUP BY mes"
        )).mappings().fetchall()
        viajes_por_mes = [0] * 12
        for fila in filas_mes:
            viajes_por_mes[int(fila["mes"]) - 1] = fila["total"]

        top_destinos = conn.execute(text(
            "SELECT destino, COUNT(*) AS total FROM envios "
            "WHERE estado_envio = 'Entregado' "
            "GROUP BY destino ORDER BY total DESC LIMIT 5"
        )).mappings().fetchall()

        top_choferes = conn.execute(text(
            "SELECT u.nombre_completo, COUNT(*) AS total "
            "FROM envios e JOIN usuarios u ON u.id_usuario = e.id_chofer "
            "WHERE e.estado_envio = 'Entregado' "
            "GROUP BY u.nombre_completo ORDER BY total DESC LIMIT 5"
        )).mappings().fetchall()

    return jsonify({
        "viajes_por_mes": viajes_por_mes,
        "top_destinos": [dict(fila) for fila in top_destinos],
        "top_choferes": [dict(fila) for fila in top_choferes],
    })

# ==============================================================================
# 9.2 REPORTES EN PDF (por ruta, por chofer, por mes)
# ==============================================================================
MESES_ES = [
    (1, "Enero"), (2, "Febrero"), (3, "Marzo"), (4, "Abril"),
    (5, "Mayo"), (6, "Junio"), (7, "Julio"), (8, "Agosto"),
    (9, "Septiembre"), (10, "Octubre"), (11, "Noviembre"), (12, "Diciembre"),
]


@app.route("/reportes")
def reportes_vista():
    solo_admin()
    with engine.connect() as conn:
        choferes = conn.execute(text(
            "SELECT id_usuario, nombre_completo FROM usuarios "
            "WHERE rol = 'Chofer' ORDER BY nombre_completo"
        )).mappings().fetchall()
    return render_template(
        "reportes.html", lista_choferes=choferes,
        anio_actual=date.today().year, meses=MESES_ES,
    )


def _pdf_response(buffer, nombre_archivo):
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"},
    )


@app.route("/reportes/ruta/<int:id_envio>.pdf")
def reporte_pdf_ruta(id_envio):
    solo_admin()
    with engine.connect() as conn:
        envio = conn.execute(text(
            "SELECT e.*, c.razon_social AS cliente, v.placa AS vehiculo, "
            "       v.modelo AS vehiculo_modelo, u.nombre_completo AS chofer "
            "FROM envios e "
            "JOIN clientes c  ON c.id_cliente  = e.id_cliente "
            "JOIN vehiculos v ON v.id_vehiculo = e.id_vehiculo "
            "JOIN usuarios u  ON u.id_usuario  = e.id_chofer "
            "WHERE e.id_envio = :id"
        ), {"id": id_envio}).mappings().fetchone()
        if not envio:
            abort(404)
        bitacora = conn.execute(text(
            "SELECT tipo_evento, descripcion, cantidad_combustible, monto_valor, "
            "       fecha_hora_registro "
            "FROM bitacora_rutas WHERE id_envio = :id ORDER BY fecha_hora_registro"
        ), {"id": id_envio}).mappings().fetchall()
        gastos_ruta = _gastos_ruta(conn, id_envio)

    buffer = reportes.pdf_ruta(envio, bitacora, gastos_ruta)
    return _pdf_response(buffer, f"ruta_{id_envio}.pdf")


@app.route("/reportes/chofer/<int:id_chofer>.pdf")
def reporte_pdf_chofer(id_chofer):
    solo_admin()
    with engine.connect() as conn:
        chofer = conn.execute(text(
            "SELECT nombre_completo FROM usuarios "
            "WHERE id_usuario = :id AND rol = 'Chofer'"
        ), {"id": id_chofer}).mappings().fetchone()
        if not chofer:
            abort(404)
        envios = conn.execute(text(
            "SELECT e.id_envio, c.razon_social AS cliente, e.destino, e.distancia_km, "
            "       e.costo_flete, e.costo_estimado_combustible, e.costo_estimado_mantenimiento, "
            "       COALESCE(gastos_bitacora.gastos_ruta, 0) AS gastos_ruta, "
            "       COALESCE(gastos_bitacora.gastos_gasolina, 0) AS gastos_gasolina, "
            "       COALESCE(gastos_bitacora.gastos_peaje, 0) AS gastos_peaje, "
            "       COALESCE(gastos_bitacora.gastos_falla, 0) AS gastos_falla, "
            "       e.estado_envio, e.fecha_creacion "
            "FROM envios e JOIN clientes c ON c.id_cliente = e.id_cliente "
            "LEFT JOIN " + _SUBCONSULTA_GASTOS_RUTA + " ON gastos_bitacora.id_envio = e.id_envio "
            "WHERE e.id_chofer = :id ORDER BY e.fecha_creacion"
        ), {"id": id_chofer}).mappings().fetchall()

    buffer = reportes.pdf_chofer(chofer["nombre_completo"], envios)
    return _pdf_response(buffer, f"chofer_{id_chofer}.pdf")


@app.route("/reportes/mes.pdf")
def reporte_pdf_mes():
    solo_admin()
    try:
        anio = int(request.args["anio"])
        mes = int(request.args["mes"])
        if not (1 <= mes <= 12):
            raise ValueError("Mes fuera de rango.")
    except (KeyError, ValueError):
        abort(400)

    with engine.connect() as conn:
        # El mes de una ruta se decide por su fecha de llegada (cuando ya
        # generó el gasto/ingreso real); si aún no fue entregada, se usa la
        # fecha de creación. Mismo criterio que /api/estadisticas, para que
        # el reporte mensual y el gráfico de productividad no queden
        # inconsistentes sobre a qué mes pertenece una ruta.
        envios = conn.execute(text(
            "SELECT e.id_envio, c.razon_social AS cliente, u.nombre_completo AS chofer, "
            "       v.placa AS vehiculo, e.destino, e.distancia_km, e.estado_envio, "
            "       e.costo_flete, e.costo_estimado_combustible, e.costo_estimado_mantenimiento, "
            "       COALESCE(gastos_bitacora.gastos_ruta, 0) AS gastos_ruta, "
            "       COALESCE(gastos_bitacora.gastos_gasolina, 0) AS gastos_gasolina, "
            "       COALESCE(gastos_bitacora.gastos_peaje, 0) AS gastos_peaje, "
            "       COALESCE(gastos_bitacora.gastos_falla, 0) AS gastos_falla "
            "FROM envios e "
            "JOIN clientes c  ON c.id_cliente  = e.id_cliente "
            "JOIN usuarios u  ON u.id_usuario  = e.id_chofer "
            "JOIN vehiculos v ON v.id_vehiculo = e.id_vehiculo "
            "LEFT JOIN " + _SUBCONSULTA_GASTOS_RUTA + " ON gastos_bitacora.id_envio = e.id_envio "
            "WHERE EXTRACT(YEAR FROM COALESCE(e.fecha_llegada, e.fecha_creacion)) = :anio "
            "  AND EXTRACT(MONTH FROM COALESCE(e.fecha_llegada, e.fecha_creacion)) = :mes "
            "ORDER BY e.fecha_creacion"
        ), {"anio": anio, "mes": mes}).mappings().fetchall()

    buffer = reportes.pdf_mes(anio, mes, envios)
    return _pdf_response(buffer, f"reporte_{anio}_{mes:02d}.pdf")

# ==============================================================================
# 10. ARRANQUE Y CREACIÓN DE ADMIN POR DEFECTO
# ==============================================================================
def inicializar_sistema():
    """Crea un usuario admin por defecto si aún no existe.

    Reintenta la conexión varias veces para tolerar que la base de datos aún
    esté arrancando: es lo habitual al levantar todo junto con Docker Compose,
    donde el contenedor de la app puede iniciar unos segundos antes que el de
    PostgreSQL termine de aceptar conexiones."""
    import time
    from sqlalchemy.exc import OperationalError

    for intento in range(1, 16):
        try:
            with app.app_context():
                with engine.begin() as conn:
                    admin = conn.execute(
                        text("SELECT 1 FROM usuarios WHERE usuario = 'admin'")
                    ).fetchone()
                    if not admin:
                        password_hash = generate_password_hash(
                            os.environ.get("ADMIN_PASSWORD", "admin")
                        )
                        # Se crea con debe_cambiar_clave = true: en su primer
                        # ingreso el sistema le obligará a definir una contraseña
                        # propia, ya que la inicial ('admin') es conocida.
                        # ON CONFLICT evita una condición de carrera cuando varios
                        # workers de gunicorn arrancan a la vez: solo uno lo inserta
                        # y el resto no falla ni duplica.
                        result = conn.execute(text(
                            "INSERT INTO usuarios "
                            "(nombre_completo, usuario, password, rol, estado, debe_cambiar_clave) "
                            "VALUES ('Administrador Principal', 'admin', :pwd, 'Administrador', "
                            "'Activo', true) "
                            "ON CONFLICT (usuario) DO NOTHING"
                        ), {"pwd": password_hash})
                        if result.rowcount:
                            print("Usuario 'admin' creado (clave inicial 'admin'; "
                                  "deberá cambiarse en el primer ingreso).")
            return
        except OperationalError:
            print(f"Base de datos no disponible aún (intento {intento}/15); "
                  "reintentando en 2 s...")
            time.sleep(2)
        except SQLAlchemyError:
            print("Aún no existen las tablas; ejecuta el esquema antes de crear el admin.")
            return
    print("No se pudo conectar a la base de datos tras varios intentos.")


if __name__ == "__main__":
    inicializar_sistema()
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
