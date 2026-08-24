import os
import secrets
from datetime import date, datetime, timedelta

# carga variables de .env si python-dotenv esta instalado
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

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# detras de un proxy (render) la conexion real le llega al proxy, no a la app;
# sin esto request.remote_addr siempre daria la ip interna del proxy y el
# limite de intentos de login terminaria agrupando a todos los usuarios bajo
# una sola ip. x_for=1 confia en un solo salto de proxy (el de render)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# endurece la cookie de sesion
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # no accesible por js
    SESSION_COOKIE_SAMESITE="Lax",  # mitiga csrf entre sitios
)

# en produccion sobre https conviene poner la cookie como secure.
# se activa con SESSION_COOKIE_SECURE=1, apagado por defecto para no
# romper el acceso por http en la lan
if os.environ.get("SESSION_COOKIE_SECURE", "0") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True

DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

# en la lan no hace falta cors abierto, front y api comparten origen.
# se restringe a los origenes de CORS_ORIGINS (por defecto ninguno externo)
_cors_origins = os.environ.get("CORS_ORIGINS", "").strip()
if _cors_origins:
    CORS(
        app,
        resources={r"/api/*": {"origins": _cors_origins.split(",")}},
        supports_credentials=True,
    )

# conexion a postgres: si hay DATABASE_URL (hosting en la nube) se usa esa,
# si no se arma con las variables DB_* sueltas (modo local/lan)
def _construir_db_uri():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # sqlalchemy 2.x no acepta "postgres://" (heredado de heroku), toca
        # fijar el driver psycopg2 a mano
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
    pool_pre_ping=True,   # reconecta si la conexion quedo vieja
)

# tipos de evento que puede mandar el movil
EVENTOS_GASTO = {"Gasolina", "Peaje", "Novedad"}
EVENTOS_ESTADO = {"Salida", "Llegada"}
EVENTOS_VALIDOS = EVENTOS_GASTO | EVENTOS_ESTADO

# estados de envio donde se puede editar/anular. una vez "entregado" la ruta
# queda fija como historico para los reportes
ESTADOS_ENVIO_EDITABLES = {"Pendiente", "En Ruta"}


def _documento_vigente(fecha_vencimiento):
    """vigente si tiene fecha y no vencio"""
    return fecha_vencimiento is not None and fecha_vencimiento >= date.today()


def _validar_documentos_chofer(chofer):
    """chequea licencia, certificado medico y cedula del chofer.
    devuelve (True, None) si esta todo vigente, o (False, mensaje) si falta algo"""
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
    """chequea rcv e impuesto de alcaldia del vehiculo"""
    faltantes = []
    if not _documento_vigente(vehiculo["vencimiento_rcv"]):
        faltantes.append("RCV")
    if not _documento_vigente(vehiculo["vencimiento_impuesto_alcaldia"]):
        faltantes.append("impuesto de alcaldía")
    if faltantes:
        return False, f"El vehículo tiene documentos vencidos o sin registrar: {', '.join(faltantes)}."
    return True, None


# estados que se muestran en la tabla de gestion. son calculados, no se
# guardan: el estado manual (inactivo) manda sobre todo, despues si esta metido
# en una ruta activa, y despues si tiene algun documento vencido. "Documentos"
# es tecnicamente inactivo, porque los selects de asignacion de rutas ya filtran
# a los que tienen papeles vencidos
def _estado_visual_chofer(chofer, rutas_activas):
    if chofer["estado"] != "Activo":
        return "Inactivo", "danger"
    if rutas_activas:
        return "Asignado", "primary"
    if not _validar_documentos_chofer(chofer)[0]:
        return "Documentos", "warning"
    return "Activo", "success"


def _estado_visual_vehiculo(vehiculo, rutas_activas):
    if vehiculo["estado"] == "Fuera de Servicio":
        return "Inactivo", "danger"
    if vehiculo["estado"] == "Asignado" or rutas_activas:
        return "Asignado", "primary"
    if not _validar_documentos_vehiculo(vehiculo)[0]:
        return "Documentos", "warning"
    return "Activo", "success"


def _liberar_vehiculo(conn, id_vehiculo):
    """marca el vehiculo como disponible"""
    conn.execute(text(
        "UPDATE vehiculos SET estado = 'Disponible' WHERE id_vehiculo = :id"
    ), {"id": id_vehiculo})


def _chofer_con_ruta_activa(conn, id_chofer, excluir_id_envio=None):
    """true si el chofer ya tiene una ruta pendiente o en ruta.
    el dashboard del chofer solo muestra una a la vez, si le asignas otra
    queda invisible hasta que entregue la primera"""
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
    """lee precio de combustible y margen de ganancia de configuracion_financiera
    (fila unica id=1), editable desde /configuracion sin reiniciar nada"""
    fila = conn.execute(text(
        "SELECT precio_combustible, tipo_ganancia, valor_ganancia "
        "FROM configuracion_financiera WHERE id = 1"
    )).mappings().fetchone()
    if fila is None:
        # por si la migracion no se aplico todavia
        return {"precio_combustible": 0.5, "tipo_ganancia": "porcentaje", "valor_ganancia": 0}
    return fila


def _costo_mantenimiento_km(costo_ultimo_mantenimiento, km_para_mantenimiento):
    """tasa de mantenimiento por km, derivada de lo que costo el ultimo
    mantenimiento repartido entre el intervalo (en km) hasta el proximo.
    esta tasa es la que se guarda y se usa para cotizar rutas"""
    return round(costo_ultimo_mantenimiento / km_para_mantenimiento, 2)


def _calcular_costos_ruta(distancia, vehiculo, config_financiera):
    """costo de combustible y mantenimiento de la ruta (lo que se guarda en
    envios). no incluye la ganancia, esa es solo para cotizar y se calcula aparte"""
    km_por_litro = float(vehiculo["km_por_litro"]) or 1.0
    costo_combustible = round(
        (distancia / km_por_litro) * float(config_financiera["precio_combustible"]), 2
    )
    costo_mantenimiento = round(distancia * float(vehiculo["costo_mantenimiento_km"]), 2)
    return costo_combustible, costo_mantenimiento


def _calcular_ganancia(costo_combustible, costo_mantenimiento, config_financiera):
    """margen de ganancia sobre combustible + mantenimiento, segun el modo
    configurado: porcentaje o monto fijo"""
    if config_financiera["tipo_ganancia"] == "fijo":
        return round(float(config_financiera["valor_ganancia"]), 2)
    base = costo_combustible + costo_mantenimiento
    return round(base * (float(config_financiera["valor_ganancia"]) / 100), 2)


# subconsulta con los gastos reales de la ruta (gasolina, peajes, fallas) que
# reporta el chofer desde la bitacora, total y por categoria. salida/llegada
# no cuentan porque son eventos de estado, no gastos
_SUBCONSULTA_GASTOS_RUTA = (
    "(SELECT id_envio, COALESCE(SUM(monto_valor), 0) AS gastos_ruta, "
    "        COALESCE(SUM(monto_valor) FILTER (WHERE tipo_evento = 'Gasolina'), 0) AS gastos_gasolina, "
    "        COALESCE(SUM(monto_valor) FILTER (WHERE tipo_evento = 'Peaje'), 0) AS gastos_peaje, "
    "        COALESCE(SUM(monto_valor) FILTER (WHERE tipo_evento = 'Novedad'), 0) AS gastos_falla "
    " FROM bitacora_rutas WHERE tipo_evento IN ('Gasolina', 'Peaje', 'Novedad') "
    " GROUP BY id_envio) AS gastos_bitacora"
)


def _gastos_ruta(conn, id_envio):
    """total de gastos reales de una ruta puntual"""
    return float(conn.execute(text(
        "SELECT COALESCE(SUM(monto_valor), 0) FROM bitacora_rutas "
        "WHERE id_envio = :id AND tipo_evento IN ('Gasolina', 'Peaje', 'Novedad')"
    ), {"id": id_envio}).scalar_one())


def _ganancia_neta(costo_flete, costo_combustible, costo_mantenimiento, gastos_ruta):
    """ganancia neta real: flete - combustible estimado - mantenimiento estimado
    - gastos reales de la ruta (gasolina, peajes, fallas)"""
    return round(
        float(costo_flete) - float(costo_combustible) - float(costo_mantenimiento)
        - float(gastos_ruta), 2,
    )


# proteccion csrf sin dependencias externas: se genera un token por sesion,
# los formularios lo mandan en un campo oculto y el fetch de la api en el
# header X-CSRFToken
def _get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inyectar_csrf():
    """expone csrf_token() a los templates"""
    return {"csrf_token": _get_csrf_token}


def _validar_csrf():
    token_sesion = session.get("_csrf_token")
    enviado = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    return token_sesion is not None and enviado == token_sesion


# middleware de seguridad: sesion + csrf
@app.before_request
def auditar_acceso_sesion():
    """corre antes de cada request: asegura el token csrf, lo valida en los
    metodos que cambian estado y exige sesion iniciada"""
    # que siempre haya un token csrf para los templates
    _get_csrf_token()

    # valida csrf en los metodos que cambian datos. si falla, lo normal es que
    # la sesion ya haya expirado (pestaña vieja abierta por horas) mas que un
    # ataque real, asi que en vez de la pagina fea de "bad request" se manda
    # a iniciar sesion de nuevo
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _validar_csrf():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Token CSRF inválido o ausente."}), 400
            flash(
                "Tu sesión expiró o la página quedó desactualizada. "
                "Inicia sesión de nuevo e intenta otra vez.", "warning",
            )
            return redirect(url_for("login"))

    # login y estaticos son libres
    if request.endpoint == "login" or (
        request.endpoint and request.endpoint.startswith("static")
    ):
        return None

    # de aca en adelante se exige sesion
    if "usuario_id" not in session:
        if request.path.startswith("/api/"):
            abort(401)
        flash("Por seguridad, debes iniciar sesión para acceder.", "warning")
        return redirect(url_for("login"))

    # si el usuario tiene cambio de clave pendiente (el admin en su primer
    # ingreso) se lo retiene en esa pantalla hasta que defina su propia clave.
    # solo se permite esa pagina y el logout
    if session.get("debe_cambiar_clave") and request.endpoint not in (
        "cambio_obligatorio_password", "logout"
    ):
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "Debes cambiar tu contraseña antes de continuar."
            }), 403
        return redirect(url_for("cambio_obligatorio_password"))


def solo_admin():
    """403 si el usuario en sesion no es administrador"""
    if session.get("rol") != "Administrador":
        abort(403)


# limite de intentos de login, contado por la pareja usuario+ip (asi nadie
# puede bloquear al usuario real fallando a proposito desde otra ip)
LIMITE_INTENTOS_LOGIN = 5
BLOQUEO_LOGIN_MINUTOS = 15


def _ip_cliente():
    return request.remote_addr or "desconocida"


def _login_bloqueado_hasta(conn, usuario, ip):
    """devuelve el datetime hasta el que sigue bloqueado, o None si puede intentar"""
    fila = conn.execute(text(
        "SELECT bloqueado_hasta FROM intentos_login WHERE usuario = :u AND ip = :i"
    ), {"u": usuario, "i": ip}).mappings().fetchone()
    if fila and fila["bloqueado_hasta"] and fila["bloqueado_hasta"] > datetime.utcnow():
        return fila["bloqueado_hasta"]
    return None


def _registrar_intento_fallido(conn, usuario, ip):
    """suma un intento fallido y bloquea la pareja usuario+ip si llega al limite"""
    fila = conn.execute(text(
        "SELECT intentos, bloqueado_hasta FROM intentos_login "
        "WHERE usuario = :u AND ip = :i FOR UPDATE"
    ), {"u": usuario, "i": ip}).mappings().fetchone()

    ahora = datetime.utcnow()
    # si no hay registro previo, o el bloqueo anterior ya vencio, se arranca de nuevo
    if fila is None or (fila["bloqueado_hasta"] and fila["bloqueado_hasta"] <= ahora):
        intentos = 1
    else:
        intentos = fila["intentos"] + 1

    bloqueado_hasta = (
        ahora + timedelta(minutes=BLOQUEO_LOGIN_MINUTOS)
        if intentos >= LIMITE_INTENTOS_LOGIN else None
    )
    conn.execute(text(
        "INSERT INTO intentos_login (usuario, ip, intentos, ultimo_intento, bloqueado_hasta) "
        "VALUES (:u, :i, :n, :ahora, :bh) "
        "ON CONFLICT (usuario, ip) DO UPDATE SET "
        "intentos = :n, ultimo_intento = :ahora, bloqueado_hasta = :bh"
    ), {"u": usuario, "i": ip, "n": intentos, "ahora": ahora, "bh": bloqueado_hasta})
    return intentos, bloqueado_hasta


def _limpiar_intentos_login(conn, usuario, ip):
    conn.execute(text(
        "DELETE FROM intentos_login WHERE usuario = :u AND ip = :i"
    ), {"u": usuario, "i": ip})


# autenticacion
@app.route("/login", methods=["GET", "POST"])
def login():
    if "usuario_id" in session:
        return redirect(url_for("inicio"))

    if request.method == "POST":
        usuario_ingresado = request.form.get("usuario", "")
        password_ingresado = request.form.get("password", "")
        ip = _ip_cliente()

        with engine.begin() as conn:
            bloqueado_hasta = _login_bloqueado_hasta(conn, usuario_ingresado, ip)
            if bloqueado_hasta:
                minutos_restantes = max(1, int((bloqueado_hasta - datetime.utcnow()).total_seconds() // 60) + 1)
                flash(
                    f"Demasiados intentos fallidos. Vuelve a intentar en "
                    f"{minutos_restantes} minuto(s).", "danger",
                )
                return render_template("login.html")

            consulta = text(
                "SELECT * FROM usuarios WHERE usuario = :user AND estado = 'Activo'"
            )
            user = conn.execute(
                consulta, {"user": usuario_ingresado}
            ).mappings().fetchone()

            if user and check_password_hash(user["password"], password_ingresado):
                _limpiar_intentos_login(conn, usuario_ingresado, ip)
            else:
                _registrar_intento_fallido(conn, usuario_ingresado, ip)
                user = None

        if user:
            # regenera el token csrf al loguear (evita fijacion de sesion)
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


# perfil de usuario: cambio de clave por el propio usuario
@app.route("/perfil/cambiar_password", methods=["POST"])
def cambiar_password():
    """cualquier usuario logueado cambia su propia clave, confirmando la actual"""
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
    """retiene a un usuario con cambio de clave pendiente hasta que defina una
    propia. no pide la clave actual porque se acaba de autenticar con ella"""
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


# dashboard principal segun el rol
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

            # ganancia neta del mes: mismo criterio que el pdf mensual (fecha
            # llegada o creacion, solo se excluyen las anuladas) para que
            # coincida con el reporte
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

            # listas para los selects de los modales
            clientes = conn.execute(
                text("SELECT id_cliente, razon_social FROM clientes ORDER BY razon_social")
            ).mappings().fetchall()
            # solo entran a los selects los que realmente pueden salir a ruta:
            # activos y con los documentos vigentes. una fecha nula tambien
            # queda fuera (NULL >= CURRENT_DATE no es true), igual que en
            # _documento_vigente
            vehiculos = conn.execute(
                text("SELECT id_vehiculo, placa, modelo, km_por_litro, costo_mantenimiento_km "
                     "FROM vehiculos WHERE estado = 'Disponible' "
                     "  AND vencimiento_rcv >= CURRENT_DATE "
                     "  AND vencimiento_impuesto_alcaldia >= CURRENT_DATE "
                     "ORDER BY placa")
            ).mappings().fetchall()
            choferes = conn.execute(
                text("SELECT id_usuario, nombre_completo FROM usuarios "
                     "WHERE rol = 'Chofer' AND estado = 'Activo' "
                     "  AND vencimiento_licencia >= CURRENT_DATE "
                     "  AND vencimiento_certificado_medico >= CURRENT_DATE "
                     "  AND vencimiento_cedula >= CURRENT_DATE "
                     "ORDER BY nombre_completo")
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
            # solo se muestra la ruta activa (pendiente o en ruta), sin anuladas
            consulta_ruta = text(
                "SELECT * FROM envios WHERE id_chofer = :chofer_id "
                "AND estado_envio IN ('Pendiente', 'En Ruta') "
                "ORDER BY fecha_creacion DESC LIMIT 1"
            )
            ruta_actual = conn.execute(
                consulta_ruta, {"chofer_id": session["usuario_id"]}
            ).mappings().fetchone()
            return render_template("dashboard_chofer.html", ruta=ruta_actual)


# vistas de administracion
def _agrupar_historial(conn, tipo_recurso):
    """historial de activaciones/inactivaciones agrupado por id de recurso"""
    filas = conn.execute(text(
        "SELECT id_recurso, accion, motivo, fecha_registro "
        "FROM historial_estado WHERE tipo_recurso = :tipo "
        "ORDER BY fecha_registro DESC, id_historial DESC"
    ), {"tipo": tipo_recurso}).mappings().fetchall()
    agrupado = {}
    for fila in filas:
        agrupado.setdefault(fila["id_recurso"], []).append(fila)
    return agrupado


def _conteo_por_id(conn, sql):
    """{id: total} a partir de una consulta que devuelve dos columnas"""
    return {fila[0]: fila[1] for fila in conn.execute(text(sql)).fetchall()}


def _datos_inactivacion(historial, esta_inactivo):
    """fecha y motivo de la ultima inactivacion. si el recurso ya se reactivo,
    la casilla queda vacia (solo aplica mientras esta inactivo)"""
    if not esta_inactivo:
        return None, None
    for evento in historial:
        if evento["accion"] == "Inactivacion":
            return evento["fecha_registro"], evento["motivo"]
    return None, None


@app.route("/gestion")
def gestion():
    solo_admin()
    with engine.connect() as conn:
        filas_choferes = conn.execute(text(
            "SELECT id_usuario, nombre_completo, usuario, vencimiento_licencia, "
            "       vencimiento_certificado_medico, vencimiento_cedula, estado, "
            "       fecha_registro "
            "FROM usuarios WHERE rol = 'Chofer' ORDER BY nombre_completo"
        )).mappings().fetchall()
        filas_vehiculos = conn.execute(text(
            "SELECT id_vehiculo, placa, modelo, capacidad_carga, km_por_litro, "
            "       costo_ultimo_mantenimiento, costo_mantenimiento_km, "
            "       km_para_mantenimiento, estado, "
            "       vencimiento_rcv, vencimiento_impuesto_alcaldia, fecha_registro "
            "FROM vehiculos ORDER BY placa"
        )).mappings().fetchall()

        # rutas activas (bloquean la inactivacion) y total historico de rutas
        activas_chofer = _conteo_por_id(conn,
            "SELECT id_chofer, COUNT(*) FROM envios "
            "WHERE estado_envio IN ('Pendiente', 'En Ruta') GROUP BY id_chofer")
        activas_vehiculo = _conteo_por_id(conn,
            "SELECT id_vehiculo, COUNT(*) FROM envios "
            "WHERE estado_envio IN ('Pendiente', 'En Ruta') GROUP BY id_vehiculo")
        rutas_chofer = _conteo_por_id(conn,
            "SELECT id_chofer, COUNT(*) FROM envios "
            "WHERE estado_envio != 'Anulado' GROUP BY id_chofer")
        rutas_vehiculo = _conteo_por_id(conn,
            "SELECT id_vehiculo, COUNT(*) FROM envios "
            "WHERE estado_envio != 'Anulado' GROUP BY id_vehiculo")
        hist_choferes = _agrupar_historial(conn, "Chofer")
        hist_vehiculos = _agrupar_historial(conn, "Vehiculo")

        choferes = []
        for fila in filas_choferes:
            ch = dict(fila)
            rutas_activas = activas_chofer.get(ch["id_usuario"], 0)
            ch["estado_visual"], ch["estado_color"] = _estado_visual_chofer(ch, rutas_activas)
            ch["rutas_activas"] = rutas_activas
            ch["total_rutas"] = rutas_chofer.get(ch["id_usuario"], 0)
            ch["historial"] = hist_choferes.get(ch["id_usuario"], [])
            ch["fecha_inactivacion"], ch["motivo_inactivacion"] = _datos_inactivacion(
                ch["historial"], ch["estado"] != "Activo")
            # para pintar en rojo el input del documento vencido en Detalles
            ch["lic_vencida"] = not _documento_vigente(ch["vencimiento_licencia"])
            ch["med_vencida"] = not _documento_vigente(ch["vencimiento_certificado_medico"])
            ch["ced_vencida"] = not _documento_vigente(ch["vencimiento_cedula"])
            choferes.append(ch)

        vehiculos = []
        for fila in filas_vehiculos:
            v = dict(fila)
            rutas_activas = activas_vehiculo.get(v["id_vehiculo"], 0)
            v["estado_visual"], v["estado_color"] = _estado_visual_vehiculo(v, rutas_activas)
            v["rutas_activas"] = rutas_activas
            v["total_rutas"] = rutas_vehiculo.get(v["id_vehiculo"], 0)
            v["historial"] = hist_vehiculos.get(v["id_vehiculo"], [])
            v["fecha_inactivacion"], v["motivo_inactivacion"] = _datos_inactivacion(
                v["historial"], v["estado"] == "Fuera de Servicio")
            v["rcv_vencido"] = not _documento_vigente(v["vencimiento_rcv"])
            v["imp_vencido"] = not _documento_vigente(v["vencimiento_impuesto_alcaldia"])
            vehiculos.append(v)
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


@app.route("/agregar_empleado", methods=["POST"])
def agregar_empleado():
    """registra un usuario administrativo (rol Administrador). a diferencia del
    chofer no lleva documentos legales, solo nombre, usuario y clave"""
    solo_admin()
    try:
        nombre_completo = request.form["nombre_completo"].strip()
        usuario_login = request.form["usuario"].strip()
        password = request.form["password"]
        if not nombre_completo or not usuario_login or not password:
            raise ValueError("Nombre, usuario y contraseña son obligatorios.")
        if len(password) < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres.")
        password_hash = generate_password_hash(password)
    except (KeyError, ValueError) as error:
        flash(f"Datos del empleado inválidos: {error}", "danger")
        return redirect(url_for("empleados"))

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO usuarios (nombre_completo, usuario, password, rol, estado) "
                "VALUES (:nombre, :usuario, :pwd, 'Administrador', 'Activo')"
            ), {"nombre": nombre_completo, "usuario": usuario_login, "pwd": password_hash})
        flash(f"Empleado {nombre_completo} registrado exitosamente.", "success")
    except IntegrityError:
        flash("El nombre de usuario ya existe. Elige otro.", "danger")
    except SQLAlchemyError:
        flash("No se pudo registrar el empleado.", "danger")
    return redirect(url_for("empleados"))


@app.route("/choferes")
def choferes():
    solo_admin()
    return redirect(url_for("gestion"))


@app.route("/configuracion", methods=["POST"])
def configuracion():
    """guarda precio de combustible y margen de ganancia, desde el modal
    "configuracion financiera" del panel principal"""
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


# endpoints de alta (insert reales)
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
        costo_ultimo_mantenimiento = float(request.form["costo_ultimo_mantenimiento"])
        km_para_mantenimiento = int(request.form["km_para_mantenimiento"])
        capacidad_raw = request.form.get("capacidad_carga", "").strip()
        capacidad_carga = float(capacidad_raw) if capacidad_raw else None

        if not placa or not modelo:
            raise ValueError("Placa y modelo son obligatorios.")
        if km_por_litro <= 0:
            raise ValueError("El rendimiento (Km/L) debe ser mayor que cero.")
        if costo_ultimo_mantenimiento < 0 or km_para_mantenimiento <= 0:
            raise ValueError("Los datos de mantenimiento deben ser válidos.")
        if capacidad_carga is not None and capacidad_carga < 0:
            raise ValueError("La capacidad de carga no puede ser negativa.")
    except (KeyError, ValueError):
        flash("Datos del vehículo inválidos. Revisa los campos numéricos.", "danger")
        return redirect(url_for("inicio"))

    costo_mantenimiento_km = _costo_mantenimiento_km(costo_ultimo_mantenimiento, km_para_mantenimiento)

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO vehiculos "
                "(placa, modelo, capacidad_carga, km_por_litro, costo_ultimo_mantenimiento, "
                " costo_mantenimiento_km, km_para_mantenimiento, estado, vencimiento_rcv, "
                " vencimiento_impuesto_alcaldia) "
                "VALUES (:placa, :modelo, :cap, :kml, :cum, :cmk, :kmm, 'Disponible', :rcv, :imp)"
            ), {
                "placa": placa,
                "modelo": modelo,
                "cap": capacidad_carga,
                "kml": km_por_litro,
                "cum": costo_ultimo_mantenimiento,
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
        destino = request.form["destino"].strip()
        if distancia <= 0 or not destino:
            raise ValueError("Datos inválidos.")
        if costo_flete <= 0:
            raise ValueError("El costo del flete (cobrado al cliente) debe ser mayor que cero.")
    except (KeyError, ValueError):
        flash("Datos del envío inválidos. Revisa los campos.", "danger")
        return redirect(url_for("inicio"))

    try:
        with engine.begin() as conn:
            # for update bloquea la fila hasta el commit, evita que dos admins
            # asignen el mismo vehiculo a la vez
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

            # calcula los costos estimados (son NOT NULL en el esquema)
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
                "dest": destino, "dist": distancia,
                "flete": costo_flete,
                "ccomb": costo_combustible, "cmant": costo_mantenimiento,
            })
            # el vehiculo queda asignado hasta que se entregue
            conn.execute(text(
                "UPDATE vehiculos SET estado = 'Asignado' WHERE id_vehiculo = :id"
            ), {"id": id_vehiculo})

        flash(f"Ruta hacia {destino} creada y asignada.", "success")
    except IntegrityError:
        flash("Error de integridad: cliente, vehículo o chofer inválido.", "danger")
    except SQLAlchemyError:
        flash("No se pudo crear el envío.", "danger")
    return redirect(url_for("inicio"))


# edicion y baja: editar choferes/vehiculos, resetear claves, corregir o
# anular rutas desde gestion de tablas
@app.route("/gestion/chofer/<int:id_usuario>/editar", methods=["POST"])
def editar_chofer(id_usuario):
    solo_admin()
    try:
        nombre_completo = request.form["nombre_completo"].strip()
        usuario_login = request.form["usuario"].strip()
        nueva_password = request.form.get("password", "").strip()
        if not nombre_completo or not usuario_login:
            raise ValueError("Nombre y usuario son obligatorios.")
        if nueva_password and len(nueva_password) < 8:
            raise ValueError("La nueva contraseña debe tener al menos 8 caracteres.")
    except (KeyError, ValueError) as error:
        flash(f"Datos del chofer inválidos: {error}", "danger")
        return redirect(url_for("gestion"))

    campos = {
        "id": id_usuario,
        "nombre": nombre_completo,
        "usuario": usuario_login,
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
                # el estado no se cambia aca: va por el boton Activar/Inactivar
                # de la tabla, que exige un motivo y lo deja en el historial
                "UPDATE usuarios SET nombre_completo = :nombre, usuario = :usuario, "
                "vencimiento_licencia = :lic, "
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
        costo_ultimo_mantenimiento = float(request.form["costo_ultimo_mantenimiento"])
        km_para_mantenimiento = int(request.form["km_para_mantenimiento"])
        capacidad_raw = request.form.get("capacidad_carga", "").strip()
        capacidad_carga = float(capacidad_raw) if capacidad_raw else None

        if not placa or not modelo:
            raise ValueError("Placa y modelo son obligatorios.")
        if km_por_litro <= 0:
            raise ValueError("El rendimiento (Km/L) debe ser mayor que cero.")
        if costo_ultimo_mantenimiento < 0 or km_para_mantenimiento <= 0:
            raise ValueError("Los datos de mantenimiento deben ser válidos.")
        if capacidad_carga is not None and capacidad_carga < 0:
            raise ValueError("La capacidad de carga no puede ser negativa.")
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

            # el estado no se cambia aca: va por el boton Activar/Inactivar de
            # la tabla, que exige un motivo y lo deja en el historial
            costo_mantenimiento_km = _costo_mantenimiento_km(costo_ultimo_mantenimiento, km_para_mantenimiento)

            conn.execute(text(
                "UPDATE vehiculos SET placa = :placa, modelo = :modelo, "
                "capacidad_carga = :cap, km_por_litro = :kml, "
                "costo_ultimo_mantenimiento = :cum, costo_mantenimiento_km = :cmk, "
                "km_para_mantenimiento = :kmm, "
                "vencimiento_rcv = :rcv, vencimiento_impuesto_alcaldia = :imp "
                "WHERE id_vehiculo = :id"
            ), {
                "placa": placa, "modelo": modelo, "cap": capacidad_carga,
                "kml": km_por_litro, "cum": costo_ultimo_mantenimiento,
                "cmk": costo_mantenimiento_km,
                "kmm": km_para_mantenimiento,
                "rcv": request.form.get("vencimiento_rcv") or None,
                "imp": request.form.get("vencimiento_impuesto_alcaldia") or None,
                "id": id_vehiculo,
            })
        flash(f"Vehículo {placa} actualizado.", "success")
    except IntegrityError:
        flash("La placa ya está registrada por otro vehículo.", "danger")
    except SQLAlchemyError:
        flash("No se pudo actualizar el vehículo.", "danger")
    return redirect(url_for("gestion"))


@app.route("/gestion/usuario/<int:id_usuario>/resetear_password", methods=["POST"])
def resetear_password(id_usuario):
    """el admin fija una nueva clave para cualquier usuario sin conocer la anterior"""
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
    """activa/desactiva un usuario (los choferes tienen su propio toggle en editar_chofer)"""
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


def _procesar_cambio_estado(tipo_recurso, id_recurso):
    """activa/inactiva un chofer o un vehiculo. exige un motivo y lo deja
    guardado en historial_estado. no deja inactivar si el recurso tiene una
    ruta pendiente o en curso"""
    accion = request.form.get("accion")
    motivo = (request.form.get("motivo") or "").strip()
    destino = request.referrer or url_for("gestion")

    if accion not in ("inactivar", "activar"):
        flash("Acción inválida.", "danger")
        return redirect(destino)
    if not motivo:
        flash("Debes indicar el motivo para cambiar el estado.", "danger")
        return redirect(destino)
    motivo = motivo[:300]

    if tipo_recurso == "Chofer":
        tabla, col_id, col_ruta = "usuarios", "id_usuario", "id_chofer"
        estado_activo, estado_inactivo = "Activo", "Inactivo"
        etiqueta, filtro_extra = "El chofer", " AND rol = 'Chofer'"
    else:
        tabla, col_id, col_ruta = "vehiculos", "id_vehiculo", "id_vehiculo"
        estado_activo, estado_inactivo = "Disponible", "Fuera de Servicio"
        etiqueta, filtro_extra = "El vehículo", ""

    inactivando = accion == "inactivar"
    nuevo_estado = estado_inactivo if inactivando else estado_activo

    try:
        with engine.begin() as conn:
            # se bloquea la fila para que nadie asigne una ruta justo mientras
            # se esta inactivando
            actual = conn.execute(text(
                f"SELECT estado FROM {tabla} WHERE {col_id} = :id{filtro_extra} FOR UPDATE"
            ), {"id": id_recurso}).mappings().fetchone()
            if not actual:
                flash(f"{etiqueta} indicado no existe.", "danger")
                return redirect(destino)

            if inactivando:
                rutas_activas = conn.execute(text(
                    f"SELECT COUNT(*) FROM envios WHERE {col_ruta} = :id "
                    "AND estado_envio IN ('Pendiente', 'En Ruta')"
                ), {"id": id_recurso}).scalar_one()
                if rutas_activas:
                    flash(
                        f"No se puede desactivar: {etiqueta.lower()} tiene una ruta "
                        "asignada o en curso. Finaliza o anula esa ruta primero.",
                        "warning",
                    )
                    return redirect(destino)

            conn.execute(text(
                f"UPDATE {tabla} SET estado = :estado WHERE {col_id} = :id"
            ), {"estado": nuevo_estado, "id": id_recurso})
            conn.execute(text(
                "INSERT INTO historial_estado (tipo_recurso, id_recurso, accion, motivo) "
                "VALUES (:tipo, :id, :accion, :motivo)"
            ), {
                "tipo": tipo_recurso, "id": id_recurso,
                "accion": "Inactivacion" if inactivando else "Reactivacion",
                "motivo": motivo,
            })
    except SQLAlchemyError:
        flash("No se pudo cambiar el estado.", "danger")
        return redirect(destino)

    if inactivando:
        flash(f"{etiqueta} quedó inactivo. Motivo: {motivo}", "success")
    else:
        flash(f"{etiqueta} fue reincorporado. Motivo: {motivo}", "success")
    return redirect(destino)


@app.route("/gestion/chofer/<int:id_usuario>/estado", methods=["POST"])
def cambiar_estado_chofer(id_usuario):
    solo_admin()
    return _procesar_cambio_estado("Chofer", id_usuario)


@app.route("/gestion/vehiculo/<int:id_vehiculo>/estado", methods=["POST"])
def cambiar_estado_vehiculo(id_vehiculo):
    solo_admin()
    return _procesar_cambio_estado("Vehiculo", id_vehiculo)


@app.route("/gestion/envio/<int:id_envio>/editar", methods=["POST"])
def editar_envio(id_envio):
    """corrige una ruta pendiente o en ruta: destino, distancia, cliente,
    vehiculo y/o chofer, revalidando todo de nuevo"""
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

            # bloquea la fila del vehiculo para que nadie mas lo asigne a la vez
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
    """anula una ruta pendiente o en ruta y libera el vehiculo"""
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


# api rest para el movil (bitacora)
@app.route("/api/incidencia", methods=["POST"])
def registrar_incidencia():
    """recibe eventos json del movil del chofer"""
    if session.get("rol") != "Chofer":
        return jsonify({"error": "Acceso denegado, rol insuficiente"}), 403

    datos = request.get_json(silent=True) or {}
    id_envio = datos.get("id_envio")
    tipo_evento = datos.get("tipo_evento")

    # valida lo que llega
    if tipo_evento not in EVENTOS_VALIDOS:
        return jsonify({"error": "Tipo de evento no reconocido."}), 400
    try:
        id_envio = int(id_envio)
    except (TypeError, ValueError):
        return jsonify({"error": "Identificador de envío inválido."}), 400

    # litros y monto solo aplican a eventos de gasto
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

    # peaje y novedad llevan texto libre del chofer (nombre del peaje o la
    # falla), el resto usa una descripcion fija
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
            # el envio debe existir, ser de este chofer y no estar cerrado
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

            # cambia el estado del envio
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
                # libera el vehiculo al terminar
                _liberar_vehiculo(conn, envio["id_vehiculo"])

        return jsonify({"status": "success", "mensaje": f"Evento {tipo_evento} procesado en DB."}), 200

    except IntegrityError:
        return jsonify({"error": "Error de integridad en PostgreSQL (llave foránea inválida)."}), 500
    except SQLAlchemyError:
        return jsonify({"error": "Error al registrar el evento en la base de datos."}), 500


# estadisticas del dashboard (kpis, grafico, rankings). todo sobre envios
# "entregado", no cuenta lo pendiente/en ruta
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

        # ganancia neta por mes (mismo criterio que el pdf mensual y el kpi
        # del home: se excluyen las anuladas y manda la fecha de llegada)
        filas_ganancia = conn.execute(text(
            "SELECT EXTRACT(MONTH FROM COALESCE(e.fecha_llegada, e.fecha_creacion))::int AS mes, "
            "       COALESCE(SUM("
            "         e.costo_flete - e.costo_estimado_combustible "
            "         - e.costo_estimado_mantenimiento "
            "         - COALESCE(gastos_bitacora.gastos_ruta, 0)"
            "       ), 0) AS total "
            "FROM envios e "
            "LEFT JOIN " + _SUBCONSULTA_GASTOS_RUTA + " ON gastos_bitacora.id_envio = e.id_envio "
            "WHERE e.estado_envio != 'Anulado' "
            "  AND EXTRACT(YEAR FROM COALESCE(e.fecha_llegada, e.fecha_creacion)) "
            "      = EXTRACT(YEAR FROM CURRENT_DATE) "
            "GROUP BY mes"
        )).mappings().fetchall()
        ganancia_por_mes = [0.0] * 12
        for fila in filas_ganancia:
            ganancia_por_mes[int(fila["mes"]) - 1] = round(float(fila["total"]), 2)

        # kilometros recorridos por mes (solo rutas entregadas)
        filas_km = conn.execute(text(
            "SELECT EXTRACT(MONTH FROM COALESCE(fecha_llegada, fecha_creacion))::int AS mes, "
            "       COALESCE(SUM(distancia_km), 0) AS total "
            "FROM envios WHERE estado_envio = 'Entregado' "
            "  AND EXTRACT(YEAR FROM COALESCE(fecha_llegada, fecha_creacion)) "
            "      = EXTRACT(YEAR FROM CURRENT_DATE) "
            "GROUP BY mes"
        )).mappings().fetchall()
        km_por_mes = [0.0] * 12
        for fila in filas_km:
            km_por_mes[int(fila["mes"]) - 1] = round(float(fila["total"]), 2)

        # en que se va la plata en ruta, segun la bitacora de los choferes
        filas_gastos = conn.execute(text(
            "SELECT tipo_evento, COALESCE(SUM(monto_valor), 0) AS total "
            "FROM bitacora_rutas WHERE tipo_evento IN ('Gasolina', 'Peaje', 'Novedad') "
            "GROUP BY tipo_evento"
        )).mappings().fetchall()
        gastos_por_tipo = {"Gasolina": 0.0, "Peaje": 0.0, "Novedad": 0.0}
        for fila in filas_gastos:
            gastos_por_tipo[fila["tipo_evento"]] = round(float(fila["total"]), 2)

        # composicion de la flota con los mismos estados que la tabla de gestion
        filas_veh = conn.execute(text(
            "SELECT id_vehiculo, estado, vencimiento_rcv, vencimiento_impuesto_alcaldia "
            "FROM vehiculos"
        )).mappings().fetchall()
        activas_vehiculo = _conteo_por_id(conn,
            "SELECT id_vehiculo, COUNT(*) FROM envios "
            "WHERE estado_envio IN ('Pendiente', 'En Ruta') GROUP BY id_vehiculo")
        estado_flota = {"Activo": 0, "Asignado": 0, "Documentos": 0, "Inactivo": 0}
        for fila in filas_veh:
            etiqueta, _ = _estado_visual_vehiculo(
                fila, activas_vehiculo.get(fila["id_vehiculo"], 0))
            estado_flota[etiqueta] += 1

    return jsonify({
        "viajes_por_mes": viajes_por_mes,
        "top_destinos": [dict(fila) for fila in top_destinos],
        "top_choferes": [dict(fila) for fila in top_choferes],
        "ganancia_por_mes": ganancia_por_mes,
        "km_por_mes": km_por_mes,
        "gastos_por_tipo": gastos_por_tipo,
        "estado_flota": estado_flota,
    })


@app.route("/graficos")
def graficos():
    """pagina aparte con todos los graficos de productividad"""
    solo_admin()
    return render_template("graficos.html")


@app.route("/manual")
def manual_usuario():
    """sirve el manual de usuario en pdf. Content-Disposition inline para que
    el navegador lo abra en pestaña propia en vez de descargarlo cada vez"""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Manual_de_Usuario.pdf")
    if not os.path.exists(ruta):
        abort(404)
    with open(ruta, "rb") as f:
        contenido = f.read()
    return Response(
        contenido,
        mimetype="application/pdf",
        headers={"Content-Disposition": "inline; filename=Manual_de_Usuario.pdf"},
    )


# reportes en pdf: por ruta, por chofer, por mes
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
        # el mes de la ruta se decide por fecha de llegada (o creacion si no
        # se ha entregado), igual que /api/estadisticas para que no quede
        # descuadrado con el grafico
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


# arranque: carga el esquema si hace falta y crea el admin por defecto
# migraciones idempotentes: ponen al dia una base que YA existe (la de docker o
# la de render) sin recargar el esquema ni tocar los datos. se corren en cada
# arranque, son baratas y no hacen nada si ya estan aplicadas
_MIGRACIONES = [
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS fecha_registro "
    "timestamp without time zone DEFAULT CURRENT_TIMESTAMP",

    "ALTER TABLE vehiculos ADD COLUMN IF NOT EXISTS fecha_registro "
    "timestamp without time zone DEFAULT CURRENT_TIMESTAMP",

    "CREATE TABLE IF NOT EXISTS historial_estado ("
    " id_historial integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
    " tipo_recurso character varying(10) NOT NULL,"
    " id_recurso integer NOT NULL,"
    " accion character varying(20) NOT NULL,"
    " motivo text NOT NULL,"
    " fecha_registro timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,"
    " CONSTRAINT historial_estado_tipo_check"
    "   CHECK (tipo_recurso IN ('Chofer', 'Vehiculo')),"
    " CONSTRAINT historial_estado_accion_check"
    "   CHECK (accion IN ('Inactivacion', 'Reactivacion')))",

    "CREATE INDEX IF NOT EXISTS idx_historial_estado_recurso "
    "ON historial_estado (tipo_recurso, id_recurso)",

    # limite de intentos de login: se cuenta por la pareja usuario+ip, asi
    # nadie puede bloquear al usuario real inundando intentos desde otra ip
    "CREATE TABLE IF NOT EXISTS intentos_login ("
    " usuario character varying(50) NOT NULL,"
    " ip character varying(45) NOT NULL,"
    " intentos integer NOT NULL DEFAULT 1,"
    " ultimo_intento timestamp without time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,"
    " bloqueado_hasta timestamp without time zone,"
    " PRIMARY KEY (usuario, ip))",
]


def _asegurar_esquema():
    # si la base esta vacia (no existe la tabla usuarios) carga schema.sql.
    # asi una base nueva (ej. render) queda lista sin correr psql a mano.
    # despues corre las migraciones para las bases que ya existian.
    with engine.begin() as conn:
        # lock a nivel de transaccion: si arrancan varios workers de gunicorn a
        # la vez (render usa 2), solo uno toca el esquema; el resto espera y
        # al entrar ya encuentra todo hecho.
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(727274)")
        existe = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'usuarios'"
        )).fetchone()
        if not existe:
            ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
            with open(ruta, "r", encoding="utf-8") as f:
                conn.exec_driver_sql(f.read())
            # el header de schema.sql (formato pg_dump) deja el search_path
            # vacio; lo restauramos para no romper las consultas que siguen ni
            # dejar la conexion sucia al volver al pool.
            conn.exec_driver_sql("RESET search_path")
            print("Esquema cargado en la base de datos (schema.sql).")

        for sentencia in _MIGRACIONES:
            conn.exec_driver_sql(sentencia)


def inicializar_sistema():
    """crea el admin por defecto si no existe. reintenta varias veces por si
    la base de datos todavia esta arrancando (tipico con docker compose)"""
    import time
    from sqlalchemy.exc import OperationalError

    for intento in range(1, 16):
        try:
            with app.app_context():
                _asegurar_esquema()
                with engine.begin() as conn:
                    admin = conn.execute(
                        text("SELECT 1 FROM usuarios WHERE usuario = 'admin'")
                    ).fetchone()
                    if not admin:
                        password_hash = generate_password_hash(
                            os.environ.get("ADMIN_PASSWORD", "admin")
                        )
                        # se crea con debe_cambiar_clave=true, la clave inicial
                        # es conocida asi que se le obliga a cambiarla al entrar.
                        # ON CONFLICT evita duplicados si varios workers de
                        # gunicorn arrancan a la vez
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
