"""
Sistema de Inventario - Taller Mecánico
Módulo 2: Administración de Inventario + Módulo 3: Organización Física de
Bodega + una primera pieza del Módulo 4: Facturas de compra.

Backend en Flask + SQLite. Sin dependencias raras: solo Flask,
para que sea fácil de instalar y correr en cualquier PC o servidor.
"""

import os
import re
import json
import sqlite3
import shutil
import time
import threading
import smtplib
import uuid
import statistics
import math
import logging
import traceback
import base64
import difflib
import hmac
import hashlib
import html
import secrets
from email.message import EmailMessage
from datetime import datetime, timedelta

try:
    import requests
except ImportError:  # pragma: no cover - solo pasa si el hosting no instaló la dependencia
    requests = None
from flask import Flask, g, jsonify, request, render_template, send_file, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

# BASE_DIR/DB_PATH y la conexión a la base de datos, el cálculo de stock
# mínimo, las utilidades de formato y los helpers de reportes Excel/PDF ya
# no viven acá: se movieron a core.py, un módulo sin ninguna dependencia
# hacia app.py ni hacia modulos/, justamente para que ni app.py ni un
# módulo nuevo (Caja Chica, y a futuro Operativo, RRHH, etc.) tengan que
# importarse entre sí — eso es lo que causaba un import circular cuando
# Caja Chica se separó por primera vez. Ver el docstring de core.py.
from core import (  # noqa: E402
    BASE_DIR,
    DB_PATH,
    COMPROBANTES_DIR,
    EXTENSIONES_COMPROBANTE_PERMITIDAS,
    DB_BUSY_TIMEOUT_MS,
    STOCK_MINIMO_VENTANA_DIAS,
    STOCK_MINIMO_Z_SERVICIO,
    STOCK_MINIMO_LEAD_TIME_DEFECTO_DIAS,
    STOCK_MINIMO_LEAD_TIME_MIN_DIAS,
    STOCK_MINIMO_LEAD_TIME_MAX_DIAS,
    _conectar_db,
    get_db,
    close_db,
    _iniciar_transaccion_stock,
    row_to_dict,
    codigo_ubicacion,
    nombre_automatico,
    normalizar_busqueda,
    coincide_busqueda,
    now_iso,
    sumar_dias_iso,
    fmt_money_pdf,
    usuario_actual,
    usuario_rol,
    usuario_modulos,
    requiere_rol,
    requiere_modulo,
    home_del_rol,
    ROLES_VALIDOS,
    MODULOS_VALIDOS,
    formatear_rut,
    _estimar_lead_time_dias,
    _consumo_diario_stats,
    _calcular_stock_minimo_producto,
    _recalcular_stock_minimo,
    _aplicar_entrada_stock,
    _aplicar_salida_stock,
    _stockprov_bucket_entrada,
    _stockprov_bucket_get,
    _stockprov_descontar_preferido,
    _crear_libro_excel,
    _estilo_encabezado,
    _autoancho,
    _excel_response,
    _excel_resumen,
    _pdf_documento,
    _pdf_tabla,
    _pdf_resumen,
    _pdf_response,
    _rango_fechas,
)

# Fotografías de producto (opcional) — mismo criterio que los comprobantes:
# se guardan junto a la base de datos para no perderse con cada actualización.
FOTOS_DIR = os.path.join(os.path.dirname(DB_PATH), "fotos_productos")
os.makedirs(FOTOS_DIR, exist_ok=True)
EXTENSIONES_FOTO_PERMITIDAS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# Fotos de facturas que llegan por WhatsApp (ver facturas_whatsapp_pendientes
# y _procesar_foto_factura_whatsapp) — mismo criterio de guardarlas junto a
# la base de datos.
FACTURAS_WHATSAPP_DIR = os.path.join(os.path.dirname(DB_PATH), "facturas_whatsapp")
os.makedirs(FACTURAS_WHATSAPP_DIR, exist_ok=True)

app = Flask(__name__)
_SECRET_KEY = os.environ.get("SECRET_KEY")
if not _SECRET_KEY:
    # Antes, si faltaba esta variable, el sistema arrancaba igual usando un
    # valor de respaldo escrito directamente acá en el código -- visible
    # para cualquiera que viera el repositorio. Con esa clave conocida se
    # pueden falsificar cookies de sesión (incluida una de administrador)
    # sin necesitar contraseña. Se confirmó que SECRET_KEY SÍ está
    # configurada en Railway hoy (este riesgo no se llegó a explotar en
    # producción), pero el código seguía dejando la puerta abierta para el
    # día en que alguien despliegue un ambiente nuevo (u otro hosting) y se
    # le olvide copiar esta variable. Ahora el sistema se niega a arrancar
    # en vez de seguir con un valor de respaldo inseguro (hallazgo #14 del
    # informe de revisión).
    raise RuntimeError(
        "Falta configurar la variable de entorno SECRET_KEY -- es obligatoria, ya no existe un valor de "
        "respaldo (por seguridad: un valor fijo en el código permitiría falsificar sesiones, incluida una "
        "de administrador). Genera una clave aleatoria larga (ej. con `python3 -c \"import secrets; "
        "print(secrets.token_hex(32))\"`) y agrégala como variable de entorno SECRET_KEY antes de arrancar "
        "(en Railway: Variables del servicio)."
    )
app.secret_key = _SECRET_KEY

# "Cache busting" de CSS/JS: el navegador guarda los archivos estáticos en
# caché por horas (Flask los sirve con Cache-Control de varias horas por
# defecto), así que después de cada despliegue alguien podía seguir viendo
# la versión vieja de la página sin darse cuenta (ej. un botón nuevo que
# "no aparece" aunque el despliegue haya salido bien) hasta que el caché
# expirara solo. ASSET_VERSION se calcula una vez CUANDO ARRANCA EL
# PROCESO (o sea, en cada despliegue nuevo cambia solo) y se agrega como
# "?v=..." a cada <script>/<link> en las plantillas (ver ASSET_VERSION más
# abajo) — eso hace que la URL del archivo cambie en cada despliegue, así
# que el navegador SIEMPRE pide la versión nueva en vez de usar la cacheada.
ASSET_VERSION = str(int(datetime.now().timestamp()))


@app.context_processor
def _inyectar_asset_version():
    return {"ASSET_VERSION": ASSET_VERSION}


@app.context_processor
def _inyectar_rol_usuario():
    # Las plantillas venían chequeando session.get("rol") == "admin"
    # directo, pero core.usuario_rol() (la misma función que protege
    # /administracion del lado del servidor) en realidad devuelve
    # session.get("rol") or "admin" — o sea, trata como admin a
    # cualquier sesión que no tenga "rol" guardado (sesiones viejas de
    # antes de que existiera el sistema de roles). Esa diferencia hacía
    # que /administracion SÍ dejara entrar a esas cuentas pero el botón
    # para llegar ahí nunca apareciera en ninguna pantalla — quedaba
    # "escondido" aunque el usuario sí tuviera permiso. Se expone acá la
    # misma función para que las plantillas usen exactamente la misma
    # regla que ya se aplica en el servidor.
    return {"rol_usuario": usuario_rol() if session.get("user_id") else None}


@app.context_processor
def _inyectar_modulos_usuario():
    # Mismo criterio que _inyectar_rol_usuario de arriba: expone
    # usuario_modulos() (ver migración 17 / core.py) para que la barra
    # lateral pueda mostrar cada módulo según el CONJUNTO real de módulos
    # de la sesión, no un único rol -- un usuario admin no necesita esto
    # (rol_usuario == "admin" ya destrapa todo en las plantillas), así que
    # para admin se manda un set vacío en vez de forzar a cada plantilla a
    # acordarse de chequear rol_usuario primero.
    return {"modulos_usuario": usuario_modulos() if session.get("user_id") else set()}


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taller")

# ---------------------------------------------------------------------------
# Monitoreo de errores (Sentry) -- OPCIONAL. Sin SENTRY_DSN configurada, esto
# no hace nada distinto a antes: los errores solo quedan en el log de
# Railway (visible, pero nadie se entera hasta que entra a mirarlo). Con la
# variable configurada, cada excepción no controlada además se reporta a
# Sentry (que sí puede avisar por correo/Slack apenas pasa). Para activarlo:
# crear una cuenta gratis en https://sentry.io, un proyecto tipo "Flask", y
# copiar el DSN que te da a la variable de entorno SENTRY_DSN en Railway --
# no requiere ningún cambio de código adicional.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
sentry_sdk = None
if SENTRY_DSN:
    try:
        import sentry_sdk as _sentry_sdk
        _sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.environ.get("RAILWAY_ENVIRONMENT_NAME", "production"),
            # Solo errores, sin tracing de performance -- lo único que se
            # pidió es enterarse cuando algo se rompe, no medir latencias
            # (eso además consume la cuota gratis de Sentry más rápido).
            traces_sample_rate=0.0,
        )
        sentry_sdk = _sentry_sdk
        logger.info("Sentry activado (SENTRY_DSN configurada) -- los errores no controlados también se reportan ahí.")
    except Exception:
        logger.exception("SENTRY_DSN está configurada pero Sentry no pudo inicializarse -- sigue funcionando solo con el log de Railway.")
else:
    logger.info("Sentry no configurado (falta SENTRY_DSN) -- los errores no controlados solo quedan en el log de Railway.")


@app.errorhandler(Exception)
def _manejar_error_inesperado(e):
    """Cualquier excepción no controlada por una ruta específica cae acá.
    Antes esto lo mostraba el servidor de desarrollo como una página HTML
    (que el frontend no puede parsear como JSON y termina mostrando el
    genérico "Error inesperado"), y en un deploy de producción se veía
    todavía peor. Ahora: se registra el traceback completo en el log del
    servidor (para poder diagnosticarlo), se reporta a Sentry si está
    configurado (ver SENTRY_DSN arriba), y se le devuelve al navegador un
    JSON con el mensaje real del error, para saber altiro qué pasó."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    logger.error("Error no controlado en %s %s:\n%s", request.method, request.path, traceback.format_exc())
    if sentry_sdk is not None:
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            logger.exception("No se pudo reportar el error a Sentry (no afecta la respuesta al usuario).")
    if request.path.startswith("/api/"):
        return jsonify({"error": f"Error interno del servidor: {e}"}), 500
    raise e

# Railway (y la mayoría de los hosts) sirven la app detrás de un proxy: esto
# le permite a Flask reconocer correctamente el HTTPS real del navegador.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Cookies de sesión más seguras. "Secure" solo se activa cuando corre con una
# SECRET_KEY configurada (asumimos que eso significa que está en producción,
# detrás de HTTPS); en local (sin esa variable) se deja apagado para que el
# login funcione igual por http://localhost.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = "SECRET_KEY" in os.environ

# close_db vive en core.py (sin decorador, porque core.py no conoce el
# objeto `app`); se registra acá como teardown de cada contexto de request.
app.teardown_appcontext(close_db)

# ---------------------------------------------------------------------------
# Protección básica contra fuerza bruta en el login
#
# Hallazgo #18 de la auditoría: hasta acá este contador vivía solo en un
# dict en memoria del proceso (LOGIN_INTENTOS_FALLIDOS) -- funcionaba bien
# con el despliegue actual (un solo proceso worker), pero se reiniciaba
# solo con reiniciar el servidor, y dejaría de ser efectivo el día que el
# servicio se escale a más de un proceso worker (cada worker tendría su
# propio dict, sin compartir el conteo de intentos fallidos entre sí).
# Se mueve a la tabla login_intentos_fallidos (ver Migración 34 en
# init_db) para que el conteo sea compartido y persistente, igual que
# cualquier otro estado del sistema -- sin cambiar el comportamiento ni la
# ventana de bloqueo que ya existían.
# ---------------------------------------------------------------------------

LOGIN_MAX_INTENTOS = 5
LOGIN_VENTANA_SEGUNDOS = 300  # 5 minutos


def _intentos_recientes(db, ip):
    ahora = time.time()
    limite = ahora - LOGIN_VENTANA_SEGUNDOS
    db.execute("DELETE FROM login_intentos_fallidos WHERE ip = ? AND creado_en < ?", (ip, limite))
    db.commit()
    return db.execute(
        "SELECT COUNT(*) FROM login_intentos_fallidos WHERE ip = ?", (ip,)
    ).fetchone()[0]


def _login_bloqueado(db, ip):
    return _intentos_recientes(db, ip) >= LOGIN_MAX_INTENTOS


def _registrar_intento_fallido(db, ip):
    db.execute("INSERT INTO login_intentos_fallidos (ip, creado_en) VALUES (?, ?)", (ip, time.time()))
    db.commit()


def _limpiar_intentos(db, ip):
    db.execute("DELETE FROM login_intentos_fallidos WHERE ip = ?", (ip,))
    db.commit()

USUARIO_INICIAL = "admin"
CLAVE_INICIAL = "taller2026"

# ---------------------------------------------------------------------------
# Respaldo automático por correo (Opción A: sin costo, sin infraestructura
# nueva). Se configura con variables de entorno; si no están, esta función
# simplemente no hace nada (el sistema sigue funcionando normal).
#
# Hay dos formas de mandar el correo, y se elige sola según qué variables
# estén configuradas:
#
#   1) Resend (recomendado): manda el correo por una API HTTPS normal, no
#      por el protocolo SMTP. Muchos hostings (Railway incluido, en sus
#      planes gratis/Trial/Hobby) BLOQUEAN el tráfico saliente por SMTP para
#      evitar que se use para spam, aunque las credenciales estén bien —
#      eso es justo lo que causa el error "Network is unreachable". Como
#      Resend usa HTTPS (el mismo protocolo que usa el navegador para
#      cargar la página), no choca con ese bloqueo.
#   2) SMTP tradicional (Gmail): funciona si el hosting no bloquea el
#      puerto SMTP (por ejemplo, corriendo en tu propio PC, o en un plan de
#      hosting pago que sí lo permita).
#
# Si están configuradas las dos, se usa Resend primero.
# ---------------------------------------------------------------------------

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "") or "Inventario Taller <onboarding@resend.dev>"

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_DESTINATARIO = os.environ.get("SMTP_DESTINATARIO", "") or SMTP_USER
RESPALDO_INTERVALO_HORAS = int(os.environ.get("RESPALDO_INTERVALO_HORAS", "24"))
ALERTA_STOCK_INTERVALO_HORAS = int(os.environ.get("ALERTA_STOCK_INTERVALO_HORAS", "24"))
ALERTA_STOCK_DESTINATARIO = os.environ.get("ALERTA_STOCK_DESTINATARIO", "") or SMTP_DESTINATARIO

# Correos del taller que van en copia (CC) cuando se le manda un presupuesto
# al cliente por correo — separados por coma. Quedan vacíos por defecto
# hasta que el usuario confirme cuáles direcciones usar; mientras tanto el
# envío sigue funcionando igual, solo que sin copia a nadie del taller.
# Configúralo agregando la variable de entorno PRESUPUESTO_COPIA_TALLER en
# Railway (ej: "ventas@talleres.cl,jefe@talleres.cl").
PRESUPUESTO_COPIA_TALLER = [
    correo.strip() for correo in os.environ.get("PRESUPUESTO_COPIA_TALLER", "").split(",") if correo.strip()
]

# ---------------------------------------------------------------------------
# IA (Claude, Anthropic): usada para leer fotos de facturas y, a futuro,
# para el agente de WhatsApp que traduce mensajes en acciones sobre el
# sistema. Se llama por HTTPS directo con 'requests' (igual que Resend más
# arriba) en vez de instalar el SDK oficial de Anthropic, para no sumar una
# dependencia nueva al build. Sin ANTHROPIC_API_KEY configurada, estas
# funciones fallan con un mensaje claro en vez de romper el resto del
# sistema — todo lo demás sigue funcionando igual sin esta variable.
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# ---------------------------------------------------------------------------
# Agente de WhatsApp: un mecánico le escribe a un número de WhatsApp y el
# mensaje termina moviendo el estado de una Orden de Trabajo (OT) en el
# sistema. TWILIO_AUTH_TOKEN es OPCIONAL — Twilio es el intermediario más
# simple para conectar WhatsApp Business sin pasar por la verificación
# directa de Meta, y firma cada webhook que nos manda; si se configura este
# token, /webhook/whatsapp valida esa firma y rechaza cualquier request que
# no venga realmente de Twilio. Sin el token, el webhook igual funciona
# (para poder probarlo antes de tener la cuenta 100% configurada) pero sin
# esa verificación extra — conviene setearlo apenas se tenga la cuenta.
# No hace falta ninguna otra credencial de Twilio en el servidor: como
# Twilio nos llama a nosotros (webhook) y nuestra respuesta (TwiML) YA es
# el mensaje de vuelta, no hace falta llamar a ninguna API de Twilio para
# enviar mensajes.
# ---------------------------------------------------------------------------

TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

# TWILIO_ACCOUNT_SID sí es obligatorio para las notas de VOZ (no para
# texto): para descargar el audio de un mensaje de voz hay que pedírselo a
# la URL que manda Twilio (MediaUrl0), y esa descarga requiere autenticarse
# con Basic Auth usando el Account SID como usuario y el Auth Token como
# contraseña — es la única llamada que este sistema le hace a la API de
# Twilio (para todo lo demás, incluido responder mensajes, alcanza con el
# TwiML que devuelve el webhook). Sin este SID, un mensaje de voz que
# llegue simplemente no se puede transcribir y el agente responde
# explicando eso — el resto del sistema sigue funcionando igual.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")

# ---------------------------------------------------------------------------
# Proveedor de WhatsApp: Twilio o la Cloud API de Meta
# ---------------------------------------------------------------------------
# Los dos caminos conviven y se eligen con WHATSAPP_PROVEEDOR. **El defecto
# es 'twilio'**: mientras no se defina la variable no cambia nada, y volver
# atrás es cambiarla en Railway -- se lee en cada request, no al importar, así
# que no hace falta redesplegar.
#
# Por qué se agregó Meta: Twilio bloquea la descarga de media en cuentas trial
# (error 20003) y cobra comisión de plataforma sobre las tarifas de Meta.
# Contra Meta directo, las conversaciones de SERVICIO no se cobran desde el
# 1-nov-2024 (no hay cuota mensual: simplemente son gratis), y desde el
# 1-jul-2025 solo se cobran las plantillas de marketing, utilidad y
# autenticación.
#
# Lo único que cambia entre proveedores es el TRANSPORTE. El agente que
# interpreta, la resolución de OT por talca_id, el registro de mecánicos, las
# acciones y el push hacia Talca no se enteran de cuál está activo.
#
# **Límite conocido: la ventana de 24 horas** (Meta solo deja responder dentro de las
# 24 h siguientes al último mensaje del usuario; iniciar una conversación exige plantillas aprobadas).
WHATSAPP_PROVEEDOR_DEFECTO = "twilio"


def whatsapp_proveedor():
    """El proveedor activo, leído en cada llamada. Un valor desconocido cae al
    defecto en vez de romper: quedarse sin webhook por un typo en una variable
    de entorno sería peor que seguir con Twilio."""
    valor = (os.environ.get("WHATSAPP_PROVEEDOR") or "").strip().lower()
    return valor if valor in ("twilio", "meta") else WHATSAPP_PROVEEDOR_DEFECTO


# -- Credenciales de la Cloud API de Meta -----------------------------------
# De dónde sale cada una (App Dashboard de Meta for Developers):
#
#   META_WA_ACCESS_TOKEN    WhatsApp > API Setup da uno TEMPORAL (24 h), útil
#                           para probar. Para producción hay que generar uno
#                           permanente en Business Settings > System Users.
#   META_WA_PHONE_NUMBER_ID WhatsApp > API Setup, campo "Phone number ID".
#                           **No es el número de teléfono**, es un id numérico.
#   META_WA_APP_SECRET      Settings > Basic > App Secret. Firma los webhooks.
#   META_WA_VERIFY_TOKEN    La inventa uno. Tiene que quedar IDÉNTICA acá y en
#                           la configuración del webhook del App Dashboard, o
#                           la verificación inicial falla.
#   META_WA_API_VERSION     Opcional. Existe para que subir de versión sea
#                           cambiar una variable y no tocar código.
META_WA_ACCESS_TOKEN = os.environ.get("META_WA_ACCESS_TOKEN", "")
META_WA_PHONE_NUMBER_ID = os.environ.get("META_WA_PHONE_NUMBER_ID", "")
META_WA_APP_SECRET = os.environ.get("META_WA_APP_SECRET", "")
META_WA_VERIFY_TOKEN = os.environ.get("META_WA_VERIFY_TOKEN", "")
# v25.0 es la versión vigente al implementar esto (verificada contra la
# documentación de Meta, agosto 2026).
META_WA_API_VERSION = os.environ.get("META_WA_API_VERSION", "v25.0")
META_WA_GRAPH = "https://graph.facebook.com"

# ---------------------------------------------------------------------------
# Transcripción de notas de voz de WhatsApp: la API de Claude no acepta
# audio como entrada (solo texto/imágenes), así que las notas de voz se
# transcriben primero con la API de OpenAI (Whisper) y el TEXTO resultante
# se manda al mismo traductor de Claude que ya procesa los mensajes
# escritos (_llamar_claude_interpretar_mensaje_agente) — no hace falta
# duplicar ninguna lógica de interpretación. whisper-1 es el modelo con
# mayor tolerancia de formatos de los que ofrece OpenAI; WhatsApp manda las
# notas de voz en ogg/opus, que no aparece en la lista de formatos
# "oficialmente" documentados, pero en la práctica la API igual las acepta
# — si algún día dejara de aceptarlas, el error se atrapa y el mecánico
# recibe un mensaje claro en vez de que el sistema se caiga.
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_AUDIO_MAX_BYTES = 25 * 1024 * 1024  # límite documentado de OpenAI


def _correo_transporte_configurado():
    """True si hay algún método de envío de correo configurado (Resend o
    SMTP). No verifica el destinatario — eso lo revisa cada función que
    llama a esta."""
    return bool(RESEND_API_KEY and requests is not None) or bool(SMTP_USER and SMTP_PASSWORD)

# ---------------------------------------------------------------------------
# Cálculo automático del stock mínimo (punto de reorden)
# ---------------------------------------------------------------------------
#
# El stock mínimo de cada producto ya no se escribe a mano: se recalcula
# solo cada vez que entra o sale stock, usando la fórmula estándar de
# "punto de reorden":
#
#     mínimo = (consumo diario promedio × días de entrega) + stock de seguridad
#
# El "stock de seguridad" es el colchón para cubrir que algunas semanas se
# consume más de lo normal: se calcula a partir de qué tan variable ha sido
# el consumo (desviación estándar) y de qué tan seguro se quiere estar de no
# quedar en cero (nivel de servicio). No se necesita ningún modelo de IA
# para esto — es la fórmula que usa cualquier sistema de inventario.

def respaldo_automatico_configurado():
    return _correo_transporte_configurado() and bool(SMTP_DESTINATARIO)


def alerta_stock_configurada():
    # Reutiliza las mismas credenciales que ya se configuran para el
    # respaldo automático (no hace falta configurar dos veces lo mismo).
    return _correo_transporte_configurado() and bool(ALERTA_STOCK_DESTINATARIO)


# CATEGORIAS_INICIALES precarga la tabla 'categorias' (campo 'categoria',
# mostrado en la UI como "Modelo") y MODELOS_INICIALES precarga la tabla
# 'modelos' (campo 'modelo', mostrado como "Detalle"). Por eso acá van
# nombres de modelo de transmisión y no descripciones de pieza -- antes del
# cambio de etiquetas estaban al revés, lo que confundía igual que el
# problema que motivó el cambio.
CATEGORIAS_INICIALES = ["JF011", "JF015", "722.9", "722.6", "MPS6", "10R80"]
MODELOS_INICIALES = ["Cuerpo de Válvulas", "Discos de Hierro", "Discos de Fricción", "Kit de Sellos"]

# "Merma o pérdida" ya no es una categoría a elegir: es su propio tipo de
# movimiento ("perdida"). "Traslado interno" se quitó por no usarse.
CATEGORIAS_ENTRADA = ["Compra", "Ajuste de inventario", "Devolución"]
CATEGORIAS_SALIDA = ["Venta", "Consumo interno", "Reserva", "Ajuste de inventario", "Devolución", "Corrección de inventario"]
CATEGORIAS_MOVIMIENTO = sorted(set(CATEGORIAS_ENTRADA) | set(CATEGORIAS_SALIDA))

# "Corrección de inventario": movimientos técnicos que revierten un ingreso
# que se hizo por error (ej. al eliminar una factura que ya había aplicado
# stock) — quedan en el historial de Movimientos para no perder trazabilidad,
# pero NUNCA representaron un uso real del taller, así que toda consulta que
# mida consumo/rotación (ver _calcular_rotacion, _calcular_planificacion, el
# "más movidos" del dashboard, y _consumo_diario_stats en core.py) filtra
# "AND categoria != 'Corrección de inventario'" junto con "tipo = 'salida'",
# para no ensuciar esos números con algo que en la práctica nunca pasó por
# bodega.
CATEGORIA_CORRECCION_INVENTARIO = "Corrección de inventario"


# ---------------------------------------------------------------------------
# Inicialización de la base de datos: creación de tablas y migraciones.
# La conexión en sí (WAL, busy_timeout, BEGIN IMMEDIATE) vive en core.py.
# ---------------------------------------------------------------------------


def init_db():
    db = _conectar_db()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY,
            codigo_interno TEXT DEFAULT '',
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            categoria TEXT DEFAULT '',
            marca TEXT DEFAULT '',
            modelo TEXT DEFAULT '',
            ubicacion TEXT DEFAULT '',
            ubicacion_id INTEGER REFERENCES ubicaciones(id),
            stock_disponible INTEGER NOT NULL DEFAULT 0,
            stock_reservado INTEGER NOT NULL DEFAULT 0,
            stock_merma INTEGER NOT NULL DEFAULT 0,
            stock_minimo INTEGER NOT NULL DEFAULT 0,
            costo_compra REAL NOT NULL DEFAULT 0,
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movimientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL,
            fecha TEXT NOT NULL,
            tipo TEXT NOT NULL,          -- entrada | salida | ajuste
            categoria TEXT DEFAULT '',   -- Compra, Venta, Consumo interno, Reserva, Ajuste de inventario, Devolución, Traslado interno, Merma o pérdida
            cantidad INTEGER NOT NULL,
            motivo TEXT DEFAULT '',
            usuario TEXT DEFAULT '',
            costo_unitario REAL,
            factura_id INTEGER,
            factura_numero TEXT,
            stock_resultante INTEGER NOT NULL,
            FOREIGN KEY (producto_id) REFERENCES productos (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ubicaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pasillo INTEGER NOT NULL,
            estanteria INTEGER NOT NULL,
            nivel INTEGER NOT NULL,
            codigo TEXT UNIQUE NOT NULL,
            creado_en TEXT NOT NULL,
            UNIQUE (pasillo, estanteria, nivel)
        );

        CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS marcas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS modelos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS facturas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT UNIQUE NOT NULL,
            proveedor TEXT DEFAULT '',
            proveedor_id INTEGER REFERENCES proveedores(id),
            fecha TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        -- Líneas de una factura que NO son repuestos y no deben tocar el
        -- inventario (ej. combustible para el generador, una herramienta
        -- para el taller) -- a pedido del taller, para mantener el
        -- catálogo de productos estricto (solo repuestos reales). Una
        -- misma factura puede tener líneas normales en factura_lineas
        -- (van a inventario) Y líneas acá al mismo tiempo (ej. un filtro
        -- que va a una OT + la herramienta para sacarlo, en la misma
        -- factura) -- por eso es un catálogo de líneas aparte y no una
        -- marca sobre producto ni sobre la factura completa. rubro_gav usa
        -- la misma clasificación fija que ya usan las boletas de Caja
        -- Chica (ver RUBROS_GAV en modulos/finanzas.py) para que el total
        -- de GAV del Estado de Resultados sume ambos orígenes sin
        -- duplicar ni inventar una segunda clasificación paralela.
        CREATE TABLE IF NOT EXISTS factura_gastos_generales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factura_id INTEGER NOT NULL REFERENCES facturas(id) ON DELETE CASCADE,
            descripcion TEXT NOT NULL,
            monto REAL NOT NULL,
            rubro_gav TEXT NOT NULL,
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE NOT NULL,
            rut TEXT DEFAULT '',
            telefono TEXT DEFAULT '',
            email TEXT DEFAULT '',
            direccion TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        -- Agente de WhatsApp: qué mecánicos pueden escribirle al agente
        -- (mapea el teléfono desde el que escriben a un nombre) y el
        -- historial de cada mensaje que llegó, para poder auditar qué
        -- movió el agente y por qué (o por qué no hizo nada).
        CREATE TABLE IF NOT EXISTS agente_mecanicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            telefono TEXT UNIQUE NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agente_mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefono TEXT NOT NULL,
            mecanico_nombre TEXT DEFAULT '',
            mensaje_texto TEXT NOT NULL,
            accion TEXT DEFAULT '',
            ot_id INTEGER,
            estado_anterior TEXT DEFAULT '',
            estado_nuevo TEXT DEFAULT '',
            exito INTEGER NOT NULL DEFAULT 0,
            respuesta_texto TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        -- Facturas de compra que llegaron como foto por WhatsApp: la IA las
        -- lee y las deja acá en estado 'pendiente' (con la foto guardada y
        -- los datos que alcanzó a extraer, ya en el mismo formato que usa
        -- la previsualización de carga masiva/foto desde el navegador) para
        -- que alguien con sesión iniciada las revise en "Facturas →
        -- Pendientes de WhatsApp" y decida aprobarlas (crea la factura real
        -- y aplica el stock) o rechazarlas. Nunca se suben solas al
        -- inventario sin ese paso manual.
        CREATE TABLE IF NOT EXISTS facturas_whatsapp_pendientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telefono TEXT NOT NULL,
            mecanico_nombre TEXT DEFAULT '',
            foto_archivo TEXT NOT NULL,
            datos_json TEXT NOT NULL,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            factura_id INTEGER REFERENCES facturas(id),
            revisado_por TEXT DEFAULT '',
            revisado_en TEXT DEFAULT '',
            nota_rechazo TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        -- Facturas de compra que registra Finanzas ANTES de que lleguen
        -- físicamente al taller (a diferencia de crear_factura(), que asume
        -- que la mercadería ya está en bodega y aplica el stock al
        -- instante). Acá se guardan las condiciones de pago pactadas con el
        -- proveedor (contado / crédito a N días / mixto) y quedan en
        -- 'pendiente' hasta que alguien contraste lo que dice esta factura
        -- contra lo que llegó de verdad y la apruebe (puede ajustar
        -- cantidades por línea si algo no calzó) o la rechace. Solo al
        -- aprobarla se crea la factura real, se aplica el stock y se
        -- generan sus Cuentas por Pagar — ver aprobar_factura_pendiente_recepcion.
        -- Ver también el aviso global de facturas por revisar (alerta_facturas_pendientes.js),
        -- pensado para que esto no pase piola como antes pasaba con
        -- "Pendientes de WhatsApp" (que solo se veía si entrabas a Facturas).
        CREATE TABLE IF NOT EXISTS facturas_finanzas_pendientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL,
            proveedor_id INTEGER REFERENCES proveedores(id),
            fecha TEXT DEFAULT '',
            lineas_json TEXT NOT NULL,
            condicion_pago TEXT NOT NULL DEFAULT 'credito',
            porcentaje_contado REAL NOT NULL DEFAULT 0,
            plazo_dias_credito INTEGER NOT NULL DEFAULT 30,
            estado TEXT NOT NULL DEFAULT 'pendiente',
            factura_id INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
            lineas_recibidas_json TEXT,
            creado_por TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            revisado_por TEXT DEFAULT '',
            revisado_en TEXT DEFAULT '',
            nota_rechazo TEXT DEFAULT '',
            origen TEXT NOT NULL DEFAULT 'manual',
            foto_archivo TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL,
            cantidad INTEGER NOT NULL,
            orden_trabajo TEXT DEFAULT '',
            motivo TEXT DEFAULT '',
            usuario TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activa',
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL,
            FOREIGN KEY (producto_id) REFERENCES productos (id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS respaldos_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            exito INTEGER NOT NULL,
            destino TEXT DEFAULT '',
            detalle TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS alertas_stock_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            exito INTEGER NOT NULL,
            destino TEXT DEFAULT '',
            detalle TEXT DEFAULT '',
            n_productos INTEGER NOT NULL DEFAULT 0,
            correo_enviado INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            nombre TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS factura_lineas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factura_id INTEGER NOT NULL,
            producto_id INTEGER NOT NULL,
            cantidad INTEGER NOT NULL,
            precio_unitario REAL NOT NULL,
            aplicada INTEGER NOT NULL DEFAULT 0,
            aplicada_en TEXT,
            FOREIGN KEY (factura_id) REFERENCES facturas (id) ON DELETE CASCADE,
            FOREIGN KEY (producto_id) REFERENCES productos (id)
        );

        CREATE TABLE IF NOT EXISTS caja_rendiciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            usuario TEXT DEFAULT '',
            total REAL NOT NULL DEFAULT 0,
            nota TEXT DEFAULT '',
            medio_pago TEXT NOT NULL DEFAULT 'efectivo_cajero',
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS caja_movimientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,               -- 'aporte' | 'gasto'
            medio_pago TEXT NOT NULL DEFAULT 'efectivo_cajero',  -- 'efectivo_cajero' | 'tarjeta_empresa' | 'cristian' -- ver comentario de migración 18 en init_db()
            fecha TEXT NOT NULL,
            monto REAL NOT NULL,
            categoria TEXT DEFAULT '',        -- solo gasto; texto libre (a pedido del taller)
            descripcion TEXT DEFAULT '',
            proveedor TEXT DEFAULT '',        -- lugar o proveedor, opcional
            usuario TEXT DEFAULT '',
            comprobante_archivo TEXT DEFAULT '',
            rendicion_id INTEGER REFERENCES caja_rendiciones(id),
            rendicion_origen_id INTEGER REFERENCES caja_rendiciones(id),
            producto_id INTEGER REFERENCES productos(id),
            movimiento_inventario_id INTEGER REFERENCES movimientos(id),
            creado_en TEXT NOT NULL
        );

        -- Cuentas por Cobrar (CxC): lo que los clientes le deben al taller.
        -- Se genera SOLA (nunca a mano) cuando una orden de trabajo pasa a
        -- "entregado" (ver modulos/operativo.py: _generar_o_actualizar_cxc)
        -- — antes de eso la plata "de papel" de la OT puede seguir
        -- cambiando (más ítems, ajustes de presupuesto), así que recién al
        -- entregar el vehículo se "congela" el monto que quedó a deber. Los
        -- abonos siguen viviendo en pagos_orden (no se duplica esa tabla);
        -- 'estado' se recalcula solo cada vez que cambian los pagos de esa
        -- orden (pendiente/parcial/pagada), nunca se edita a mano.
        CREATE TABLE IF NOT EXISTS cuentas_por_cobrar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER UNIQUE NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            cliente_id INTEGER NOT NULL REFERENCES clientes(id),
            monto_total REAL NOT NULL DEFAULT 0,
            fecha_emision TEXT NOT NULL,
            fecha_vencimiento TEXT,
            estado TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente | parcial | pagada
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        );

        -- Cuentas por Pagar (CxP): lo que el taller le debe a sus
        -- proveedores. Se genera SOLA al registrar una factura de compra
        -- (ver app.py: _registrar_factura) por el total de esa factura, o
        -- se puede crear a mano para gastos que no vienen de una factura de
        -- inventario (ej. arriendo, servicios). factura_id queda NULL en
        -- ese segundo caso.
        CREATE TABLE IF NOT EXISTS cuentas_por_pagar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proveedor_id INTEGER REFERENCES proveedores(id),
            factura_id INTEGER REFERENCES facturas(id) ON DELETE CASCADE,
            concepto TEXT DEFAULT '',
            monto_total REAL NOT NULL DEFAULT 0,
            fecha_emision TEXT NOT NULL,
            fecha_vencimiento TEXT,
            estado TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente | parcial | pagada | anulada
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        );

        -- Abonos/pagos contra una Cuenta por Pagar. A diferencia de CxC (que
        -- reutiliza pagos_orden, ya existente), acá no había ninguna tabla
        -- de "pagos a proveedor" — se crea de cero. Si se paga
        -- 'desde_caja_chica', el pago además genera un gasto real en
        -- caja_movimientos (ver app.py: _registrar_pago_cxp) para que la
        -- salida de plata quede reflejada en el saldo de Caja Chica —
        -- automatización pedida en el plan del módulo financiero ("Pago
        -- proveedor → disminuir CxP y registrar egreso").
        CREATE TABLE IF NOT EXISTS cxp_pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cxp_id INTEGER NOT NULL REFERENCES cuentas_por_pagar(id) ON DELETE CASCADE,
            monto REAL NOT NULL,
            fecha TEXT NOT NULL,
            metodo_pago TEXT NOT NULL DEFAULT 'transferencia',
            desde_caja_chica INTEGER NOT NULL DEFAULT 0,
            caja_movimiento_id INTEGER REFERENCES caja_movimientos(id),
            nota TEXT DEFAULT '',
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );
        """
    )
    db.commit()

    # Índices para CxC/CxP — quedaron sin crear cuando se agregaron estas
    # tablas (solo orden_id en cuentas_por_cobrar tenía índice automático
    # por ser UNIQUE); a medida que crece el historial de facturas/pagos,
    # los joins y filtros por cliente/proveedor/estado que usan el
    # dashboard, el gráfico de antigüedad y los listados de Finanzas
    # empiezan a hacer table scan completo sin esto.
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_cxc_cliente_id ON cuentas_por_cobrar (cliente_id);
        CREATE INDEX IF NOT EXISTS idx_cxc_estado ON cuentas_por_cobrar (estado);
        CREATE INDEX IF NOT EXISTS idx_cxp_proveedor_id ON cuentas_por_pagar (proveedor_id);
        CREATE INDEX IF NOT EXISTS idx_cxp_factura_id ON cuentas_por_pagar (factura_id);
        CREATE INDEX IF NOT EXISTS idx_cxp_estado ON cuentas_por_pagar (estado);
        CREATE INDEX IF NOT EXISTS idx_cxp_pagos_cxp_id ON cxp_pagos (cxp_id);
        """
    )
    db.commit()

    # ---- Migración 1: agrega productos.ubicacion_id si la base ya existía
    # de una versión sin Módulo 3 (sin perder los datos ya cargados).
    columnas = [c[1] for c in db.execute("PRAGMA table_info(productos)").fetchall()]
    if "ubicacion_id" not in columnas:
        db.execute("ALTER TABLE productos ADD COLUMN ubicacion_id INTEGER REFERENCES ubicaciones(id)")
        db.commit()
    if "stock_merma" not in columnas:
        db.execute("ALTER TABLE productos ADD COLUMN stock_merma INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # ---- Migración 2: columnas nuevas en movimientos (costo_unitario, factura)
    columnas_mov = [c[1] for c in db.execute("PRAGMA table_info(movimientos)").fetchall()]
    if "costo_unitario" not in columnas_mov:
        db.execute("ALTER TABLE movimientos ADD COLUMN costo_unitario REAL")
        db.commit()
    if "factura_id" not in columnas_mov:
        db.execute("ALTER TABLE movimientos ADD COLUMN factura_id INTEGER")
        db.commit()
    if "factura_numero" not in columnas_mov:
        db.execute("ALTER TABLE movimientos ADD COLUMN factura_numero TEXT")
        db.commit()
    if "categoria" not in columnas_mov:
        db.execute("ALTER TABLE movimientos ADD COLUMN categoria TEXT DEFAULT ''")
        db.commit()

    # ---- Migración 3: el código interno deja de ser obligatorio/único a nivel
    # de columna (ahora es opcional). Y el id deja de usar AUTOINCREMENT: así,
    # si borras el último producto creado, el siguiente que crees reutiliza
    # ese número en vez de saltárselo para siempre. Si la tabla viene de una
    # versión anterior con alguna de esas dos cosas, se reconstruye
    # preservando todos los datos.
    info_codigo = next(
        (c for c in db.execute("PRAGMA table_info(productos)").fetchall() if c[1] == "codigo_interno"),
        None,
    )
    sql_tabla = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'productos'"
    ).fetchone()
    tiene_autoincrement = sql_tabla is not None and "AUTOINCREMENT" in sql_tabla[0].upper()
    necesita_reconstruir = (info_codigo is not None and info_codigo[3] == 1) or tiene_autoincrement  # notnull == 1

    if necesita_reconstruir:
        db.executescript(
            """
            CREATE TABLE productos_nueva (
                id INTEGER PRIMARY KEY,
                codigo_interno TEXT DEFAULT '',
                nombre TEXT NOT NULL,
                descripcion TEXT DEFAULT '',
                categoria TEXT DEFAULT '',
                marca TEXT DEFAULT '',
                modelo TEXT DEFAULT '',
                ubicacion TEXT DEFAULT '',
                ubicacion_id INTEGER REFERENCES ubicaciones(id),
                stock_disponible INTEGER NOT NULL DEFAULT 0,
                stock_reservado INTEGER NOT NULL DEFAULT 0,
                stock_merma INTEGER NOT NULL DEFAULT 0,
                stock_minimo INTEGER NOT NULL DEFAULT 0,
                costo_compra REAL NOT NULL DEFAULT 0,
                creado_en TEXT NOT NULL,
                actualizado_en TEXT NOT NULL
            );

            INSERT INTO productos_nueva (
                id, codigo_interno, nombre, descripcion, categoria, marca, modelo,
                ubicacion, ubicacion_id, stock_disponible, stock_reservado, stock_merma,
                stock_minimo, costo_compra, creado_en, actualizado_en
            )
            SELECT
                id, codigo_interno, nombre, descripcion, categoria, marca, modelo,
                ubicacion, ubicacion_id, stock_disponible, stock_reservado, stock_merma,
                stock_minimo, costo_compra, creado_en, actualizado_en
            FROM productos;

            DROP TABLE productos;
            ALTER TABLE productos_nueva RENAME TO productos;
            DELETE FROM sqlite_sequence WHERE name = 'productos';
            """
        )
        db.commit()

    # Índice único parcial: el código interno solo debe ser único cuando se
    # informa (puede quedar vacío porque no todas las facturas lo traen).
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_codigo_interno_unico
        ON productos (codigo_interno)
        WHERE codigo_interno IS NOT NULL AND codigo_interno != ''
        """
    )
    db.commit()

    # ---- Migración 4: corrige una carga anterior en la que los modelos de
    # caja (JF011, 722.9, etc.) habían quedado precargados como "marca" en
    # vez de "modelo". Se mueven de la tabla marcas a la tabla modelos, y se
    # corrigen los productos que ya los tenían asignados como marca.
    for valor in MODELOS_INICIALES:
        estaba_en_marcas = db.execute("SELECT id FROM marcas WHERE nombre = ?", (valor,)).fetchone()
        if estaba_en_marcas:
            db.execute("DELETE FROM marcas WHERE nombre = ?", (valor,))
            db.execute("INSERT OR IGNORE INTO modelos (nombre) VALUES (?)", (valor,))
            db.execute(
                "UPDATE productos SET modelo = ?, marca = '' WHERE marca = ? AND (modelo IS NULL OR modelo = '')",
                (valor, valor),
            )
            db.execute(
                "UPDATE productos SET marca = '' WHERE marca = ?",
                (valor,),
            )
    db.commit()

    # ---- Semillas: categorías y modelos iniciales (solo si la tabla está vacía)
    if db.execute("SELECT COUNT(*) FROM categorias").fetchone()[0] == 0:
        db.executemany("INSERT OR IGNORE INTO categorias (nombre) VALUES (?)", [(c,) for c in CATEGORIAS_INICIALES])
    if db.execute("SELECT COUNT(*) FROM modelos").fetchone()[0] == 0:
        db.executemany("INSERT OR IGNORE INTO modelos (nombre) VALUES (?)", [(m,) for m in MODELOS_INICIALES])
    db.commit()

    # ---- Semilla: usuario administrador inicial (solo si no hay ningún usuario)
    if db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO usuarios (username, password_hash, nombre, creado_en) VALUES (?, ?, ?, ?)",
            (USUARIO_INICIAL, generate_password_hash(CLAVE_INICIAL), "Administrador", now_iso()),
        )
        db.commit()

    # ---- Migración 5 (histórica, revertida): en su momento se eliminó el
    # campo "marca" del sistema (categoría + modelo alcanzaban para
    # identificar el producto). Ahora se reintroduce a pedido del taller: un
    # mismo modelo/detalle puede venir de distintas marcas (ej. una versión
    # china y una americana de la misma pieza), con calidad y precio
    # distintos, y el taller necesita elegir de cuál está descontando o
    # cargando stock -- por eso "marca" vuelve a ser parte de lo que
    # identifica al producto (ver constraint de duplicados en
    # _crear_producto_desde_datos/editar_producto) en vez de solo un dato
    # informativo. Esta migración ya NO limpia el campo -- se deja tal cual
    # quedó registrado.

    # ---- Migración 6: agrega facturas.proveedor_id y crea registros de
    # proveedor a partir del texto libre que ya existía, sin perder nada.
    columnas_fac = [c[1] for c in db.execute("PRAGMA table_info(facturas)").fetchall()]
    if "proveedor_id" not in columnas_fac:
        db.execute("ALTER TABLE facturas ADD COLUMN proveedor_id INTEGER REFERENCES proveedores(id)")
        db.commit()

    nombres_proveedor = [
        r[0] for r in db.execute(
            "SELECT DISTINCT proveedor FROM facturas WHERE proveedor IS NOT NULL AND proveedor != '' AND (proveedor_id IS NULL)"
        ).fetchall()
    ]
    for nombre in nombres_proveedor:
        db.execute("INSERT OR IGNORE INTO proveedores (nombre, creado_en) VALUES (?, ?)", (nombre, now_iso()))
        db.execute(
            "UPDATE facturas SET proveedor_id = (SELECT id FROM proveedores WHERE nombre = ?) "
            "WHERE proveedor = ? AND proveedor_id IS NULL",
            (nombre, nombre),
        )
    db.commit()

    # ---- Migración 7: se retira el apartado separado de "Reservas" (ahora
    # una reserva es simplemente un movimiento de salida con categoría
    # "Reserva"). Cualquier reserva que haya quedado activa de la versión
    # anterior se libera automáticamente para no dejar stock atrapado.
    tablas = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
    if "reservas" in tablas:
        activas = db.execute("SELECT * FROM reservas WHERE estado = 'activa'").fetchall()
        for r in activas:
            db.execute(
                "UPDATE productos SET stock_disponible = stock_disponible + ?, stock_reservado = stock_reservado - ? WHERE id = ?",
                (r["cantidad"], r["cantidad"], r["producto_id"]),
            )
        db.execute("UPDATE reservas SET estado = 'liberada' WHERE estado = 'activa'")
        db.commit()

    # ---- Migración 8: la merma ahora se rastrea por proveedor (antes solo
    # se guardaba un total por producto, y la pantalla "de dónde viene"
    # asumía erróneamente que era del último ingreso — lo cual era falso si
    # un producto se compró a más de un proveedor). Se agrega
    # movimientos.proveedor_id (para que cada pérdida quede con su proveedor
    # elegido a mano) y la tabla merma_proveedores, que lleva el saldo de
    # merma pendiente desglosado por proveedor.
    if "proveedor_id" not in columnas_mov:
        db.execute("ALTER TABLE movimientos ADD COLUMN proveedor_id INTEGER REFERENCES proveedores(id)")
        db.commit()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS merma_proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
            proveedor_id INTEGER REFERENCES proveedores(id),
            cantidad INTEGER NOT NULL DEFAULT 0,
            actualizado_en TEXT NOT NULL
        )
        """
    )
    db.commit()

    # Si ya existía stock_merma de antes de esta migración (pérdidas
    # registradas sin proveedor elegido), se preserva el total en un bucket
    # "sin proveedor conocido" para no perder el dato — no se puede adivinar
    # retroactivamente cuál fue el proveedor real de esas unidades.
    ya_migrado = db.execute("SELECT COUNT(*) FROM merma_proveedores").fetchone()[0]
    if ya_migrado == 0:
        con_merma = db.execute("SELECT id, stock_merma FROM productos WHERE stock_merma > 0").fetchall()
        for p in con_merma:
            db.execute(
                "INSERT INTO merma_proveedores (producto_id, proveedor_id, cantidad, actualizado_en) VALUES (?, NULL, ?, ?)",
                (p["id"], p["stock_merma"], now_iso()),
            )
        db.commit()

    # ---- Migración 9: fotografía opcional por producto — solo guarda el
    # nombre del archivo (igual que los comprobantes de Caja Chica); el
    # archivo en sí se guarda aparte en FOTOS_DIR.
    columnas_actuales = [c[1] for c in db.execute("PRAGMA table_info(productos)").fetchall()]
    if "foto_archivo" not in columnas_actuales:
        db.execute("ALTER TABLE productos ADD COLUMN foto_archivo TEXT DEFAULT ''")
        db.commit()

    # ---- Migración 10: el stock mínimo (punto de reorden) deja de ser un
    # número que se escribe a mano y pasa a calcularse solo, a partir del
    # consumo histórico real de cada producto y de un tiempo de entrega
    # estimado (medido de cada cuánto se le ha estado comprando a los
    # proveedores). Esta tabla guarda el detalle del último cálculo de cada
    # producto, para poder mostrar "por qué" quedó ese mínimo.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_minimo_calculo (
            producto_id INTEGER PRIMARY KEY REFERENCES productos(id) ON DELETE CASCADE,
            consumo_diario_promedio REAL NOT NULL DEFAULT 0,
            desviacion_diaria REAL NOT NULL DEFAULT 0,
            lead_time_dias REAL NOT NULL DEFAULT 0,
            lead_time_es_estimado_por_defecto INTEGER NOT NULL DEFAULT 1,
            stock_seguridad REAL NOT NULL DEFAULT 0,
            stock_minimo_calculado INTEGER NOT NULL DEFAULT 0,
            calculado_en TEXT NOT NULL
        )
        """
    )
    db.commit()

    # ---- (Migración 11 retirada) Antes existía acá una siembra automática
    # del "levantamiento de bodega junio 2026" (data/importacion_inventario_
    # junio2026.json) que se reinyectaba en cada arranque si no encontraba
    # el producto guía (id 60001). El problema: en cuanto se usó "Importar
    # Excel (reemplazar)" para cargar el inventario real y actualizado, esa
    # importación borra TODOS los productos (incluido el 60001), así que en
    # el siguiente reinicio del servidor (cada deploy de Railway reinicia el
    # proceso) esta siembra volvía a dispararse y agregaba de nuevo el
    # inventario viejo de junio ENCIMA del recién cargado — eso era el
    # "inventario duplicado" (dos inventarios distintos conviviendo). Se
    # retira por completo: la carga de productos ahora vive 100% en
    # Productos → "Importar Excel", que sí reemplaza todo de una sola vez y
    # no se repite sola en cada arranque.

    # ---- Migración 18: reestructuración de Caja Chica en 3 "medios de pago"
    # separados (pedido del taller — antes todo era un solo pozo de plata sin
    # distinguir cómo salió): 'efectivo_cajero' (retiro de cajero automático,
    # se contrasta contra boletas + el vuelto que sobra queda disponible para
    # el próximo gasto — comportamiento de siempre, por eso es el default),
    # 'tarjeta_empresa' (la tarjeta genera un movimiento en el banco aparte y
    # se rinde con la boleta/factura física) y 'cristian' (el encargado de
    # logística: los trabajadores le pasan boletas, él las paga de su bolsillo
    # o transferencia para saldar rápido, y después el taller le rinde cuentas
    # a él — puede terminar debiéndole plata o al revés, por eso 'cristian' es
    # el único medio cuyo saldo puede quedar negativo con sentido: negativo =
    # el taller le debe a Cristian, ver _caja_resumen_medio en
    # modulos/finanzas.py). Todo lo que ya existía en la base queda en
    # 'efectivo_cajero' (el default de la columna), que es literalmente lo que
    # era antes de esta migración -- no hace falta backfill manual. Va ANTES
    # del bloque de índices de más abajo (que crea un índice sobre esta misma
    # columna): si quedara después, una base existente sin la columna todavía
    # rompería el CREATE INDEX.
    columnas_caja_mov_v18 = [c[1] for c in db.execute("PRAGMA table_info(caja_movimientos)").fetchall()]
    if "medio_pago" not in columnas_caja_mov_v18:
        db.execute("ALTER TABLE caja_movimientos ADD COLUMN medio_pago TEXT NOT NULL DEFAULT 'efectivo_cajero'")
        db.commit()
    columnas_caja_rend_v18 = [c[1] for c in db.execute("PRAGMA table_info(caja_rendiciones)").fetchall()]
    if "medio_pago" not in columnas_caja_rend_v18:
        db.execute("ALTER TABLE caja_rendiciones ADD COLUMN medio_pago TEXT NOT NULL DEFAULT 'efectivo_cajero'")
        db.commit()

    # ---- Migración 19: Estado de Resultados mensual (Finanzas). Se agrega
    # 'rubro_gav' a caja_movimientos -- una clasificación FIJA y separada de
    # la 'categoria' de texto libre que ya existía (esa la sigue usando el
    # taller como quiera, ej. "Aseo", "Ferretería"). rubro_gav solo se llena
    # desde el desplegable nuevo del formulario de gasto (ver RUBROS_GAV en
    # modulos/finanzas.py) y es lo que el Estado de Resultados suma para
    # armar Gastos de Administración y Ventas -- queda vacío ('') para todo
    # gasto ya cargado antes de esta migración o creado por otro camino
    # (pago a proveedor desde caja chica, compra de emergencia), a propósito:
    # el reporte los deja afuera del total y los muestra aparte como
    # "sin clasificar" en vez de adivinar dónde van, para no inventar cifras.
    columnas_caja_mov_v19 = [c[1] for c in db.execute("PRAGMA table_info(caja_movimientos)").fetchall()]
    if "rubro_gav" not in columnas_caja_mov_v19:
        db.execute("ALTER TABLE caja_movimientos ADD COLUMN rubro_gav TEXT NOT NULL DEFAULT ''")
        db.commit()

    # ---- Migración 25: Caja Chica -- boletas parciales de rendición
    # (caja_gasto_detalles) + cierre manual de un gasto (cerrado_manualmente).
    # Pedido del taller: la persona que registra un "gasto" (retiro de
    # banco/tarjeta/Cristian) solo ve el monto y la fecha -- no tiene la
    # boleta física a mano. Quien SÍ rinde cuentas contra la boleta física
    # (a veces otra persona, contrastando contra Cristian) va agregando una
    # o más "boletas" (caja_gasto_detalles) contra ese mismo gasto, cada una
    # con su propio N° de documento, monto (no necesariamente el 100% del
    # retiro, sobre todo en efectivo), rubro GAV, detalle y, si corresponde,
    # la OT relacionada (rubro "gastos_por_ot"). El gasto queda "listo para
    # rendir" (ver _gasto_estado_boletas en modulos/finanzas.py) cuando la
    # suma de sus boletas alcanza el monto del retiro, o cuando se cierra
    # manualmente (cerrado_manualmente=1, para el caso de vuelto que queda
    # en caja o una boleta chica que se perdió). Recién ahí puede
    # incluirse en una Rendición (caja_rendiciones, sin cambios ahí: sigue
    # siendo un lote de gastos ya listos, ver caja_crear_rendicion).
    # 'orden_id' en caja_gasto_detalles NO se usa para nada automático sobre
    # la OT (no se suma a su costo ni aparece en su ficha) -- es solo
    # trazabilidad de caja chica, a propósito, para no mezclar sistemas sin
    # que el taller lo haya pedido explícitamente.
    columnas_caja_mov_v25 = [c[1] for c in db.execute("PRAGMA table_info(caja_movimientos)").fetchall()]
    if "cerrado_manualmente" not in columnas_caja_mov_v25:
        db.execute("ALTER TABLE caja_movimientos ADD COLUMN cerrado_manualmente INTEGER NOT NULL DEFAULT 0")
        db.commit()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS caja_gasto_detalles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gasto_id INTEGER NOT NULL REFERENCES caja_movimientos(id),
            numero_documento TEXT NOT NULL DEFAULT '',
            monto REAL NOT NULL,
            rubro_gav TEXT NOT NULL DEFAULT '',
            categoria TEXT NOT NULL DEFAULT '',
            proveedor TEXT NOT NULL DEFAULT '',
            descripcion TEXT NOT NULL DEFAULT '',
            orden_id INTEGER REFERENCES ordenes_trabajo(id),
            comprobante_archivo TEXT NOT NULL DEFAULT '',
            usuario TEXT NOT NULL DEFAULT '',
            creado_en TEXT NOT NULL
        );
        """
    )
    db.commit()

    # ---- Migración 26: la Rendición ahora se genera por BOLETA
    # (caja_gasto_detalles), no por gasto/retiro completo. Antes, rendir un
    # gasto de $180.000 con una sola boleta de $90.000 marcaba TODO el
    # retiro como "rendido" y desaparecía de la lista de pendientes, aunque
    # solo se hubiera justificado la mitad -- confuso, y encima obligaba a
    # exigir "listo para rendir" antes de poder generar la rendición (el
    # taller pidió sacar esa traba, ver Migración 25). Ahora cada boleta se
    # rinde por su propio monto: 'caja_gasto_detalles.rendicion_id' queda
    # NULL hasta que esa boleta puntual se incluye en una rendición: el
    # resto del retiro (lo que no tiene boleta todavía) sigue disponible
    # para cargarle más boletas después y rendirlas en otro lote. La columna
    # 'caja_movimientos.rendicion_id' (Migración 18) deja de usarse para
    # gastos nuevos -- se mantiene en el esquema solo por compatibilidad,
    # nunca se vuelve a escribir.
    columnas_caja_detalle_v26 = [c[1] for c in db.execute("PRAGMA table_info(caja_gasto_detalles)").fetchall()]
    if "rendicion_id" not in columnas_caja_detalle_v26:
        db.execute("ALTER TABLE caja_gasto_detalles ADD COLUMN rendicion_id INTEGER REFERENCES caja_rendiciones(id)")
        db.commit()

    # ---- Migración 27: "compra de emergencia" (ingreso de stock al
    # inventario) se mueve del GASTO/retiro a la BOLETA individual. Quien
    # registra el retiro solo sabe monto y fecha (ver Migración 25) -- recién
    # al cargar la boleta se sabe qué se compró de verdad, así que es ahí
    # donde tiene sentido preguntar si corresponde ingresar stock. Las
    # columnas 'caja_movimientos.producto_id' / 'movimiento_inventario_id'
    # (de antes de la Migración 25) dejan de usarse para gastos nuevos -- se
    # mantienen en el esquema solo por compatibilidad con compras de
    # emergencia ya registradas, nunca se vuelven a escribir.
    columnas_caja_detalle_v27 = [c[1] for c in db.execute("PRAGMA table_info(caja_gasto_detalles)").fetchall()]
    if "producto_id" not in columnas_caja_detalle_v27:
        db.execute("ALTER TABLE caja_gasto_detalles ADD COLUMN producto_id INTEGER REFERENCES productos(id)")
        db.commit()
    if "movimiento_inventario_id" not in columnas_caja_detalle_v27:
        db.execute("ALTER TABLE caja_gasto_detalles ADD COLUMN movimiento_inventario_id INTEGER REFERENCES movimientos(id)")
        db.commit()

    # ---- Migración 28: Facturación electrónica (ver modulos/facturacion.py)
    # -- emisión de DTE (factura afecta y nota de crédito, por ahora) hacia
    # clientes, vía un proveedor externo ya certificado ante el SII (no
    # confundir con la tabla de "Facturas" de compra a proveedores). Parte
    # en modo 'simulado' (ver PROVEEDOR_DTE en facturacion.py) hasta que el
    # taller tenga su cuenta y API Key con el proveedor real.
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS documentos_tributarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo_dte INTEGER NOT NULL,
            estado TEXT NOT NULL DEFAULT 'borrador',
            folio INTEGER,
            orden_id INTEGER REFERENCES ordenes_trabajo(id),
            documento_referencia_id INTEGER REFERENCES documentos_tributarios(id),
            motivo_nc TEXT DEFAULT '',
            receptor_rut TEXT NOT NULL DEFAULT '',
            receptor_razon_social TEXT NOT NULL DEFAULT '',
            receptor_giro TEXT DEFAULT '',
            receptor_direccion TEXT DEFAULT '',
            receptor_comuna TEXT DEFAULT '',
            items_json TEXT NOT NULL DEFAULT '[]',
            monto_neto REAL NOT NULL DEFAULT 0,
            monto_iva REAL NOT NULL DEFAULT 0,
            monto_total REAL NOT NULL DEFAULT 0,
            observaciones TEXT DEFAULT '',
            proveedor TEXT DEFAULT '',
            respuesta_proveedor TEXT DEFAULT '',
            pdf_url TEXT DEFAULT '',
            error_mensaje TEXT DEFAULT '',
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            emitido_en TEXT
        );

        CREATE TABLE IF NOT EXISTS configuracion_facturacion (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            razon_social TEXT DEFAULT '',
            rut_emisor TEXT DEFAULT '',
            giro TEXT DEFAULT '',
            direccion TEXT DEFAULT '',
            comuna TEXT DEFAULT '',
            actividad_economica TEXT DEFAULT '',
            actualizado_en TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_documentos_tributarios_estado ON documentos_tributarios (estado);
        CREATE INDEX IF NOT EXISTS idx_documentos_tributarios_orden_id ON documentos_tributarios (orden_id);
        CREATE INDEX IF NOT EXISTS idx_documentos_tributarios_referencia ON documentos_tributarios (documento_referencia_id);
        """
    )
    db.commit()

    # ---- Migración 29: correo del receptor + registro de envío en
    # documentos_tributarios -- para poder mandar la factura por correo al
    # cliente (por defecto al que ya tiene registrado, pudiendo cambiarlo a
    # mano al enviar) y dejar trazabilidad de a qué correo se mandó y
    # cuándo, sin depender de leer los logs del servidor.
    columnas_dte_v29 = [c[1] for c in db.execute("PRAGMA table_info(documentos_tributarios)").fetchall()]
    if "receptor_email" not in columnas_dte_v29:
        db.execute("ALTER TABLE documentos_tributarios ADD COLUMN receptor_email TEXT DEFAULT ''")
        db.commit()
    if "enviado_en" not in columnas_dte_v29:
        db.execute("ALTER TABLE documentos_tributarios ADD COLUMN enviado_en TEXT")
        db.commit()
    if "enviado_a" not in columnas_dte_v29:
        db.execute("ALTER TABLE documentos_tributarios ADD COLUMN enviado_a TEXT DEFAULT ''")
        db.commit()

    # ---- Migración 30: bitácora de "Pre entrega" por OT + perfil
    # "encargado_pre_entrega" para el Agente de WhatsApp -- misma idea que
    # Migración 20/21 (pruebas_ruta / "probador_de_autos"), pero para la
    # última revisión antes de entregar el vehículo al cliente (auto
    # revisado, lavado, etc.). Se guarda en su propia tabla (no reutiliza
    # pruebas_ruta) porque conceptualmente es un paso distinto del proceso
    # (control de calidad en ruta vs. revisión final antes de entrega) y el
    # taller pidió que aparezca en su propia pestaña, no mezclada con
    # Pruebas de ruta. Se llena desde el formulario de la pestaña "Pre
    # entrega" (web) o automáticamente cuando un mecánico de perfil
    # "encargado_pre_entrega" manda un mensaje por WhatsApp (con o sin
    # foto, resuelto por N° de OT o por patente -- ver
    # _agente_guardar_pre_entrega en app.py, mismo mecanismo de resolución
    # de OT que ya usan "mecanico"/"probador_de_autos").
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pre_entrega (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            texto TEXT NOT NULL,
            autor TEXT DEFAULT '',
            origen TEXT NOT NULL DEFAULT 'web',
            foto_id INTEGER REFERENCES fotos_vehiculo(id),
            creado_en TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pre_entrega_orden_id ON pre_entrega (orden_id);
        """
    )
    db.commit()

    # ---- Migración 31: aviso de "Pre entrega por revisar" para la
    # campanita de notificaciones (mismo mecanismo que Migración 23/24 para
    # pruebas_ruta/sugerencias_diagnostico) -- se agrega ACÁ, como columnas
    # nuevas sobre la tabla creada en la Migración 30, en vez de meterlas
    # directo en el CREATE TABLE de arriba, porque ese CREATE TABLE ya se
    # entregó antes y en un despliegue que ya lo corrió el ALTER TABLE es lo
    # único que agrega las columnas sin perder datos (igual que las demás
    # migraciones de este archivo). También se aprovecha para que el
    # "encargado_pre_entrega" pueda mover la OT a "Listo" cuando el mensaje
    # confirma EXPLÍCITAMENTE que quedó lista para la entrega (ver
    # _agente_texto_confirma_listo_para_entrega) -- eso no necesita columnas
    # nuevas, se apoya en el estado ya existente de la OT.
    columnas_pre_entrega = [c[1] for c in db.execute("PRAGMA table_info(pre_entrega)").fetchall()]
    if "atendida" not in columnas_pre_entrega:
        db.execute("ALTER TABLE pre_entrega ADD COLUMN atendida INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE pre_entrega ADD COLUMN atendida_por TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE pre_entrega ADD COLUMN atendida_en TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE pre_entrega ADD COLUMN vista INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # Datos manuales del Estado de Resultados por mes (rubros que no se
    # registran en ningún otro lugar del sistema porque vienen de
    # contabilidad/remuneraciones externas: sueldos, Previred, arriendo,
    # seguros, gastos financieros, impuesto, dividendos). Un registro por
    # mes ('YYYY-MM'), se sobrescribe completo cada vez que se guarda desde
    # Finanzas -> Estado de Resultados -> Editar datos del mes.
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS estado_resultado_manual (
            mes TEXT PRIMARY KEY,
            otros_ingresos REAL NOT NULL DEFAULT 0,
            remuneraciones REAL NOT NULL DEFAULT 0,
            bono_remuneraciones REAL NOT NULL DEFAULT 0,
            otras_remuneraciones REAL NOT NULL DEFAULT 0,
            previred REAL NOT NULL DEFAULT 0,
            arriendo REAL NOT NULL DEFAULT 0,
            seguros REAL NOT NULL DEFAULT 0,
            gastos_financieros_intereses REAL NOT NULL DEFAULT 0,
            gastos_financieros_factoring REAL NOT NULL DEFAULT 0,
            gastos_financieros_comisiones REAL NOT NULL DEFAULT 0,
            impuesto_renta REAL NOT NULL DEFAULT 0,
            dividendos REAL NOT NULL DEFAULT 0,
            actualizado_en TEXT NOT NULL
        );
        """
    )
    db.commit()

    # ---- Migración 20: bitácora de pruebas de ruta por OT. Antes, la
    # pestaña "Pruebas de ruta" solo tenía un campo de texto
    # (qc_observaciones) que se SOBRESCRIBÍA cada vez que se guardaba el
    # control de calidad -- se perdía cualquier entrada anterior. Esta tabla
    # es un historial de verdad: cada prueba de ruta queda como una entrada
    # nueva con fecha y autor, sin pisar las anteriores. Se llena desde el
    # formulario de la pestaña o automáticamente cuando un mecánico de
    # perfil "probador_de_autos" manda el resultado por WhatsApp (ver
    # Migración 21 / agente_mecanicos.tipo).
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pruebas_ruta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            texto TEXT NOT NULL,
            autor TEXT DEFAULT '',
            origen TEXT NOT NULL DEFAULT 'web',
            foto_id INTEGER REFERENCES fotos_vehiculo(id),
            creado_en TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pruebas_ruta_orden_id ON pruebas_ruta (orden_id);
        """
    )
    db.commit()

    # ---- Migración 21: perfil "probador_de_autos" para el Agente de
    # WhatsApp (ver migración 16, columna 'tipo' de agente_mecanicos). Este
    # perfil, igual que 'mecanico', NO puede cambiar estado ni diagnóstico
    # de una OT -- pero a diferencia de 'mecanico' (cuyos mensajes quedan
    # como sugerencia de diagnóstico para que operativo la revise), lo que
    # manda este perfil se guarda directo como una entrada nueva en la
    # bitácora de pruebas de ruta (tabla pruebas_ruta de arriba), con
    # origen='whatsapp'. No requiere columna nueva -- 'tipo' ya es TEXT
    # libre, solo se agrega este tercer valor válido (ver
    # TIPOS_MECANICO_VALIDOS en core.py).

    # ---- Migración 22: stock por proveedor (pedido del taller: un mismo
    # repuesto puede venir de proveedores de calidad/costo muy distintos, y
    # hasta ahora todo se mezclaba bajo un solo costo promedio sin poder
    # saber de qué proveedor viene lo que hay disponible). Mismo patrón que
    # merma_proveedores (Migración 8): un balde por producto+proveedor, en
    # vez de duplicar el producto. Se llena solo desde ahora en adelante:
    # - En cada ENTRADA con factura, el balde del proveedor de esa factura
    #   se actualiza automáticamente (ver _aplicar_entrada_stock en core.py).
    # - En cada SALIDA desde el formulario de "Registrar movimiento", la
    #   persona elige a mano de qué proveedor están saliendo las unidades
    #   (ver _aplicar_salida_stock con proveedor_manual=True) -- se pidió
    #   así (no automático) para que el costo que quede en la orden de
    #   trabajo sea el real de ese proveedor, útil por ejemplo si un auto
    #   vuelve por garantía y hay que saber qué repuesto específico se usó.
    # - Otras salidas automáticas que todavía no tienen selector de
    #   proveedor (consumo de repuesto desde una OT por la pantalla de
    #   Operativo, Convertidores de Torque) descuentan del balde disponible
    #   sin preguntar (ver _stockprov_descontar_automatico en core.py) --
    #   no bloquean su flujo por esto, es información complementaria.
    # No se puede reconstruir con certeza el desglose histórico (no se sabe
    # de qué proveedor salió cada unidad ya consumida antes de esta
    # migración), así que el stock ya disponible al momento de migrar queda
    # en un balde "sin proveedor conocido" -- mismo criterio que se usó para
    # la merma vieja en la Migración 8.
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
            proveedor_id INTEGER REFERENCES proveedores(id),
            cantidad INTEGER NOT NULL DEFAULT 0,
            costo_promedio REAL NOT NULL DEFAULT 0,
            actualizado_en TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stock_proveedores_producto_id ON stock_proveedores (producto_id);
        """
    )
    db.commit()

    ya_migrado_stockprov = db.execute("SELECT COUNT(*) FROM stock_proveedores").fetchone()[0]
    if ya_migrado_stockprov == 0:
        con_stock = db.execute("SELECT id, stock_disponible, costo_compra FROM productos WHERE stock_disponible > 0").fetchall()
        for p in con_stock:
            db.execute(
                "INSERT INTO stock_proveedores (producto_id, proveedor_id, cantidad, costo_promedio, actualizado_en) VALUES (?, NULL, ?, ?, ?)",
                (p["id"], p["stock_disponible"], p["costo_compra"], now_iso()),
            )
        db.commit()

    # ---- Migración 23: aviso de "pruebas de ruta por revisar" para
    # Operativo -- mismo mecanismo que ya existe para las sugerencias de
    # diagnóstico (sugerencias_diagnostico.atendida), aplicado a las
    # entradas de la bitácora de pruebas de ruta que llegan por WhatsApp
    # (perfil 'probador_de_autos', ver Migración 20/21). Marcar una entrada
    # como atendida SOLO saca el aviso pendiente -- la entrada sigue
    # apareciendo para siempre en la bitácora de la OT, igual que atender
    # una sugerencia no borra nada del diagnóstico. Las entradas cargadas
    # desde la web (origen='web') no generan aviso -- si alguien del taller
    # la escribió a mano ahí mismo, no hace falta notificarle a él mismo.
    columnas_pruebas_ruta = [c[1] for c in db.execute("PRAGMA table_info(pruebas_ruta)").fetchall()]
    if "atendida" not in columnas_pruebas_ruta:
        db.execute("ALTER TABLE pruebas_ruta ADD COLUMN atendida INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE pruebas_ruta ADD COLUMN atendida_por TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE pruebas_ruta ADD COLUMN atendida_en TEXT NOT NULL DEFAULT ''")
        db.commit()

    # ---- Índices de rendimiento: movimientos es la tabla que más va a
    # crecer con los años (cada entrada, salida y pérdida queda ahí para
    # siempre), y prácticamente todas las pantallas de análisis (Rotación,
    # Planificación, historial de un producto, reportes) filtran por
    # producto_id, tipo y/o fecha. Sin estos índices, cada una de esas
    # consultas recorre la tabla completa — hoy no se nota con pocos meses
    # de datos, pero sí se va a notar con miles de movimientos acumulados.
    # Van al final de init_db() porque referencian tablas (merma_proveedores,
    # stock_minimo_calculo) que recién se crean en migraciones anteriores;
    # así funcionan tanto en una base nueva como en una ya existente.
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_movimientos_producto_id ON movimientos (producto_id);
        CREATE INDEX IF NOT EXISTS idx_movimientos_tipo_fecha ON movimientos (tipo, fecha);
        CREATE INDEX IF NOT EXISTS idx_movimientos_producto_tipo_fecha ON movimientos (producto_id, tipo, fecha);
        CREATE INDEX IF NOT EXISTS idx_movimientos_factura_numero ON movimientos (factura_numero);

        CREATE INDEX IF NOT EXISTS idx_factura_lineas_factura_id ON factura_lineas (factura_id);
        CREATE INDEX IF NOT EXISTS idx_factura_lineas_producto_id ON factura_lineas (producto_id);

        CREATE INDEX IF NOT EXISTS idx_caja_movimientos_fecha ON caja_movimientos (fecha);
        CREATE INDEX IF NOT EXISTS idx_caja_movimientos_tipo ON caja_movimientos (tipo);
        CREATE INDEX IF NOT EXISTS idx_caja_movimientos_rendicion_id ON caja_movimientos (rendicion_id);
        CREATE INDEX IF NOT EXISTS idx_caja_movimientos_medio_pago ON caja_movimientos (medio_pago);
        CREATE INDEX IF NOT EXISTS idx_caja_movimientos_rubro_gav ON caja_movimientos (rubro_gav);

        CREATE INDEX IF NOT EXISTS idx_merma_proveedores_producto_id ON merma_proveedores (producto_id);

        CREATE INDEX IF NOT EXISTS idx_productos_stock_minimo ON productos (stock_disponible, stock_minimo);
        """
    )
    db.commit()

    # ---- Migración 12: el agente de WhatsApp ahora también acepta notas de
    # voz (además de texto). Se guarda de qué tipo era cada mensaje para
    # poder mostrarlo en el historial/auditoría del agente.
    columnas_agente_mensajes = [c[1] for c in db.execute("PRAGMA table_info(agente_mensajes)").fetchall()]
    if "origen" not in columnas_agente_mensajes:
        db.execute("ALTER TABLE agente_mensajes ADD COLUMN origen TEXT NOT NULL DEFAULT 'texto'")
        db.commit()

    # ---- Migración 13: condiciones de pago pactadas con el proveedor
    # (contado / crédito a N días / mixto), guardadas en la factura ya
    # aprobada como dato informativo — ver facturas_finanzas_pendientes
    # (de donde salen estos valores al aprobar) y _generar_cxp_de_factura
    # (que las usa para generar la o las Cuentas por Pagar correspondientes).
    # Las facturas creadas por los caminos antiguos (manual directo, Excel,
    # WhatsApp) siguen sin pasar por este flujo — quedan con estos campos en
    # NULL y su CxP se sigue generando 100% a crédito a 30 días, como
    # siempre (ver valores por defecto de _generar_cxp_de_factura).
    columnas_facturas = [c[1] for c in db.execute("PRAGMA table_info(facturas)").fetchall()]
    if "condicion_pago" not in columnas_facturas:
        db.execute("ALTER TABLE facturas ADD COLUMN condicion_pago TEXT")
        db.execute("ALTER TABLE facturas ADD COLUMN porcentaje_contado REAL")
        db.execute("ALTER TABLE facturas ADD COLUMN plazo_dias_credito INTEGER")
        db.commit()

    # ---- Migración 14: roles de usuario (admin / finanzas / bodega /
    # operativo — ver ROLES_VALIDOS en core.py) y la posibilidad de
    # desactivar un usuario sin borrarlo (para no perder el rastro de qué
    # hizo mientras tuvo la cuenta activa). Todo usuario que ya existiera
    # ANTES de esta migración queda con rol 'admin' (el DEFAULT de la
    # columna se aplica a las filas existentes) — es la opción más segura:
    # nadie pierde acceso a nada de un día para otro solo por este
    # despliegue. El propio rol 'admin' es quien después reasigna el rol
    # que corresponda a cada quien desde el panel de Administración.
    columnas_usuarios = [c[1] for c in db.execute("PRAGMA table_info(usuarios)").fetchall()]
    if "rol" not in columnas_usuarios:
        db.execute("ALTER TABLE usuarios ADD COLUMN rol TEXT NOT NULL DEFAULT 'admin'")
        db.commit()
    if "activo" not in columnas_usuarios:
        db.execute("ALTER TABLE usuarios ADD COLUMN activo INTEGER NOT NULL DEFAULT 1")
        db.commit()

    # ---- Migración 15: se unifican TODOS los caminos de entrada de una
    # factura de compra (manual, carga masiva por Excel, foto escaneada, y
    # foto recibida por WhatsApp) en esta misma tabla
    # facturas_finanzas_pendientes — antes cada camino aplicaba stock de
    # inmediato y generaba su CxP sin pasar por la aprobación de Bodega
    # (salvo WhatsApp, que tenía su propia tabla facturas_whatsapp_pendientes
    # con su propio flujo de aprobación, separado de este). 'origen' deja
    # registro de por dónde entró cada una (manual/excel/foto/whatsapp) y
    # 'foto_archivo' guarda la foto original cuando corresponde (WhatsApp y
    # escaneo por foto) — ver _crear_pendiente_recepcion.
    columnas_ffp = [c[1] for c in db.execute("PRAGMA table_info(facturas_finanzas_pendientes)").fetchall()]
    if "origen" not in columnas_ffp:
        db.execute("ALTER TABLE facturas_finanzas_pendientes ADD COLUMN origen TEXT NOT NULL DEFAULT 'manual'")
        db.commit()
    if "foto_archivo" not in columnas_ffp:
        db.execute("ALTER TABLE facturas_finanzas_pendientes ADD COLUMN foto_archivo TEXT DEFAULT ''")
        db.commit()

    # ---- Migración 32: historial permanente por factura pendiente de
    # recepción (creada / rechazada+nota / corregida y reingresada /
    # aprobada). Antes, rechazar una factura la dejaba trabada: no había
    # forma de corregirla y volver a mandarla sin recrearla desde cero
    # (perdiendo el número, proveedor, líneas ya cargadas), y una vez que
    # volvía a aprobarse no quedaba ningún rastro de que alguna vez fue
    # rechazada (revisado_por/revisado_en/nota_rechazo se sobrescriben en
    # cada paso). historial_json es un registro que solo crece, nunca se
    # sobrescribe ni se borra -- ver _registrar_historial_pendiente.
    if "historial_json" not in columnas_ffp:
        db.execute("ALTER TABLE facturas_finanzas_pendientes ADD COLUMN historial_json TEXT NOT NULL DEFAULT '[]'")
        db.commit()

    # ---- Migración 33: Neto/IVA automático en facturas de compra -- a
    # pedido del taller (roadmap ítem #2). Hasta acá, el precio que se
    # escribía por línea se sumaba tal cual como "el total" de la factura,
    # sin sumarle el 19% de IVA -- lo que dejaba la Cuenta por Pagar
    # generada (ver _generar_cxp_de_factura) subestimada en un 19% respecto
    # a lo que realmente se le debe al proveedor, cada vez que el precio
    # ingresado era el neto (como efectivamente lo es: ver ayuda-inline del
    # formulario "Registrar factura de compra"). Desde esta migración, el
    # precio de cada línea se sigue tratando como NETO, pero el IVA (19%) y
    # el total se calculan solos y son los que se usan para la CxP -- así
    # queda reflejado lo que de verdad hay que pagarle al proveedor.
    # 'exento_iva' (tanto en el pendiente como en la factura final) es el
    # escape para las facturas de proveedores que no cargan IVA, para no
    # forzarles un 19% de más.
    #
    # OJO: esto NO recalcula ni corrige ninguna Cuenta por Pagar ya
    # generada antes de esta migración -- solo aplica a facturas
    # registradas de acá en adelante. Tocar retroactivamente CxP con pagos
    # ya hechos sería peligroso (los montos abonados quedarían descuadrados
    # contra un total que cambió por debajo). facturas.monto_neto/monto_iva
    # quedan en 0 para las filas históricas (no aplica, no "es cero").
    if "exento_iva" not in columnas_ffp:
        db.execute("ALTER TABLE facturas_finanzas_pendientes ADD COLUMN exento_iva INTEGER NOT NULL DEFAULT 0")
        db.commit()
    columnas_facturas_v33 = [c[1] for c in db.execute("PRAGMA table_info(facturas)").fetchall()]
    if "exento_iva" not in columnas_facturas_v33:
        db.execute("ALTER TABLE facturas ADD COLUMN exento_iva INTEGER NOT NULL DEFAULT 0")
        db.commit()
    if "monto_neto" not in columnas_facturas_v33:
        db.execute("ALTER TABLE facturas ADD COLUMN monto_neto REAL NOT NULL DEFAULT 0")
        db.commit()
    if "monto_iva" not in columnas_facturas_v33:
        db.execute("ALTER TABLE facturas ADD COLUMN monto_iva REAL NOT NULL DEFAULT 0")
        db.commit()
    if "monto_total" not in columnas_facturas_v33:
        db.execute("ALTER TABLE facturas ADD COLUMN monto_total REAL NOT NULL DEFAULT 0")
        db.commit()

    # ---- Migración 16: perfil restringido de "mecánico" para el agente de
    # WhatsApp. Un mecánico registrado en agente_mecanicos puede ser de tipo
    # 'completo' (comportamiento de siempre: puede mandar fotos Y cambiar
    # diagnóstico/estado de la OT por texto) o 'mecanico' (solo puede mandar
    # fotos, que se archivan igual que siempre; cualquier texto que mande —
    # con o sin foto — NO se escribe en diagnóstico/estado, se guarda como
    # sugerencia en sugerencias_diagnostico para que la parte operativa la
    # revise y decida). Todo mecánico que ya existiera ANTES de esta
    # migración queda en 'completo' (el DEFAULT de la columna se aplica a
    # las filas existentes) — nadie pierde permisos de un día para otro solo
    # por este despliegue; el perfil 'mecanico' se asigna a mano desde la
    # administración de mecánicos para quien corresponda.
    columnas_agente_mecanicos = [c[1] for c in db.execute("PRAGMA table_info(agente_mecanicos)").fetchall()]
    if "tipo" not in columnas_agente_mecanicos:
        db.execute("ALTER TABLE agente_mecanicos ADD COLUMN tipo TEXT NOT NULL DEFAULT 'completo'")
        db.commit()

    # Teléfonos de agente_mecanicos sin '+': _normalizar_telefono se queda solo
    # con los dígitos porque la Cloud API de Meta manda el número sin '+'. Las
    # filas guardadas antes con '+' se dejan en la misma forma o dejarían de
    # encontrarse. telefono es UNIQUE: si el mismo número ya existe sin '+'
    # (registrado dos veces), esa fila NO se toca -- un UPDATE a ciegas chocaría
    # contra la llave y la app no arrancaría. Queda duplicada a la vista en la
    # administración de mecánicos para que alguien la borre.
    db.execute(
        "UPDATE agente_mecanicos SET telefono = REPLACE(telefono, '+', '')"
        " WHERE telefono LIKE '%+%'"
        " AND REPLACE(telefono, '+', '') NOT IN (SELECT telefono FROM agente_mecanicos)"
    )
    db.commit()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS sugerencias_diagnostico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ot_id INTEGER NOT NULL,
            foto_id INTEGER,
            mecanico_nombre TEXT NOT NULL,
            texto TEXT NOT NULL,
            creado_en TEXT NOT NULL,
            atendida INTEGER NOT NULL DEFAULT 0,
            atendida_por TEXT DEFAULT '',
            atendida_en TEXT,
            vista INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.commit()

    # ---- Migración 24: distinguir "vista" de "atendida" en los avisos de
    # Operativo (sugerencias de diagnóstico y pruebas de ruta) -- pedido
    # del taller: al abrir la OT desde el link del aviso, ya no debería
    # seguir apareciendo en la alerta roja de abajo a la derecha ni en el
    # contador de la campanita, PERO tampoco hace falta que desaparezca de
    # la lista del panel (eso sigue siendo lo que hace "Marcar como
    # revisada"/"Descartar sugerencia", que marca 'atendida'). 'vista' es
    # más liviana: se marca sola al abrir la OT desde el aviso (ver
    # /api/notificaciones/marcar-vista/<orden_id> y
    # abrirOtDesdeQueryParam() en operativo.js), no requiere una acción
    # explícita de la persona, y el conteo/alerta de pendientes ahora
    # cuenta solo lo que sigue sin 'atender' Y sin 'ver' (atendida=0 AND
    # vista=0) -- el panel de la campanita, en cambio, sigue mostrando todo
    # lo no atendido sin importar si ya se vio o no. Va acá porque ambas
    # tablas (sugerencias_diagnostico y pruebas_ruta) ya existen a esta
    # altura de init_db().
    columnas_sugerencias_diagnostico = [c[1] for c in db.execute("PRAGMA table_info(sugerencias_diagnostico)").fetchall()]
    if "vista" not in columnas_sugerencias_diagnostico:
        db.execute("ALTER TABLE sugerencias_diagnostico ADD COLUMN vista INTEGER NOT NULL DEFAULT 0")
        db.commit()
    columnas_pruebas_ruta_v24 = [c[1] for c in db.execute("PRAGMA table_info(pruebas_ruta)").fetchall()]
    if "vista" not in columnas_pruebas_ruta_v24:
        db.execute("ALTER TABLE pruebas_ruta ADD COLUMN vista INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # ---- Migración 17: acceso a más de un módulo por usuario. Hasta acá
    # cada usuario no-admin quedaba encerrado en el único módulo que su
    # 'rol' representaba (ver migración 14 y requiere_modulo) -- pedido
    # explícito del taller para cuando una persona necesita trabajar en más
    # de un módulo (ej. alguien que hace Logística Y Finanzas a la vez).
    # 'modulos_json' guarda la lista de módulos permitidos como JSON (ej.
    # '["bodega","finanzas"]'). 'rol' se sigue guardando tal cual --
    # 'admin' sigue significando acceso total (modulos_json no se usa para
    # esa cuenta), y para cualquier usuario no-admin queda con el PRIMER
    # módulo elegido, solo por compatibilidad/legado (ver comentario en
    # core.py: ningún chequeo de permisos debería mirar 'rol' directo para
    # un usuario no-admin, siempre usuario_modulos()).
    #
    # Backfill: todo usuario no-admin que ya existiera ANTES de esta
    # migración queda con modulos_json = el único módulo que su 'rol'
    # anterior representaba -- nadie pierde ni gana acceso de un día para
    # otro solo por este despliegue.
    columnas_usuarios_v17 = [c[1] for c in db.execute("PRAGMA table_info(usuarios)").fetchall()]
    if "modulos_json" not in columnas_usuarios_v17:
        db.execute("ALTER TABLE usuarios ADD COLUMN modulos_json TEXT NOT NULL DEFAULT '[]'")
        db.commit()
        for fila_usuario in db.execute("SELECT id, rol FROM usuarios").fetchall():
            if fila_usuario["rol"] and fila_usuario["rol"] != "admin":
                db.execute(
                    "UPDATE usuarios SET modulos_json = ? WHERE id = ?",
                    (json.dumps([fila_usuario["rol"]]), fila_usuario["id"]),
                )
        db.commit()

    # ---- Migración 34: contador de intentos fallidos de login persistido
    # en la base (hallazgo #18 de la auditoría) en vez de vivir solo en un
    # dict en memoria del proceso -- ver el comentario junto a
    # LOGIN_MAX_INTENTOS más arriba en este archivo para el porqué. Cada
    # fila es UN intento fallido de UNA ip; se van borrando solos los que
    # ya salieron de la ventana de bloqueo (ver _intentos_recientes) y los
    # de una ip completa al loguearse bien (ver _limpiar_intentos), así que
    # la tabla se mantiene chica en la práctica.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS login_intentos_fallidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            creado_en REAL NOT NULL
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_login_intentos_ip ON login_intentos_fallidos (ip)")
    db.commit()

    # ---- Migración 35: "boleta de compra" como tipo de documento
    # alternativo a "factura" (roadmap del taller). Se agrega
    # 'tipo_documento' tanto a facturas como a facturas_finanzas_pendientes
    # -- por defecto 'factura' (todo lo registrado hasta ahora sigue siendo
    # eso), con 'boleta' como el único otro valor válido (ver
    # TIPOS_DOCUMENTO_VALIDOS). A pedido del taller, una boleta SIEMPRE es
    # al contado: no genera Cuenta por Pagar pendiente ni pide condición de
    # pago/plazo -- se fuerza condicion_pago='contado' sin importar lo que
    # mande quien llama (ver _crear_pendiente_recepcion), igual que ya
    # existía para facturas al contado (_generar_cxp_de_factura genera su
    # CxP ya pagada, para no perder el registro de la compra).
    #
    # El número de documento sigue siendo único a nivel GLOBAL (no se
    # separa por tipo): varios lugares del sistema hoy identifican una
    # factura por su número como llave natural (ver movimientos.factura_numero
    # y sus JOIN contra facturas.numero) -- separar la unicidad por tipo
    # obligaría a reescribir esos caminos para usar factura_id en vez de
    # numero. En la práctica una boleta y una factura jamás comparten
    # folio (son series de documentos distintas), así que esta restricción
    # no debería molestar.
    columnas_facturas_v35 = [c[1] for c in db.execute("PRAGMA table_info(facturas)").fetchall()]
    if "tipo_documento" not in columnas_facturas_v35:
        db.execute("ALTER TABLE facturas ADD COLUMN tipo_documento TEXT NOT NULL DEFAULT 'factura'")
        db.commit()
    columnas_ffp_v35 = [c[1] for c in db.execute("PRAGMA table_info(facturas_finanzas_pendientes)").fetchall()]
    if "tipo_documento" not in columnas_ffp_v35:
        db.execute("ALTER TABLE facturas_finanzas_pendientes ADD COLUMN tipo_documento TEXT NOT NULL DEFAULT 'factura'")
        db.commit()

    # ---- Migración 36: "Módulo de Aceites" (roadmap del taller) -- los
    # aceites son productos más de la tabla 'productos' (mismo stock, mismos
    # movimientos, mismo descuento automático en una OT vía orden_items), y
    # se distinguen de un repuesto común solo por esta columna nueva. Por
    # defecto 'repuesto' -- todo lo cargado hasta ahora sigue siendo eso; el
    # único otro valor válido es 'aceite' (ver TIPOS_PRODUCTO_VALIDOS). Ver
    # el comentario junto a PRODUCTO_SELECT para cómo se reutilizan
    # 'categoria' (tipo de aceite) y 'modelo' (nombre del aceite) en vez de
    # agregar más columnas.
    columnas_productos_v36 = [c[1] for c in db.execute("PRAGMA table_info(productos)").fetchall()]
    if "tipo_producto" not in columnas_productos_v36:
        db.execute("ALTER TABLE productos ADD COLUMN tipo_producto TEXT NOT NULL DEFAULT 'repuesto'")
        db.commit()

    # ---- Migración 37: "Factura internacional" (roadmap del taller) -- un
    # botón nuevo en Facturas para registrar una importación (flete, PayPal
    # y agente de aduana/DHL se prorratean entre las líneas y se convierten
    # a pesos con un tipo de cambio) reemplazando el Excel manual que se
    # usaba para esto. La factura resultante es una facturas_finanzas_pendientes
    # común y corriente -- mismo camino de aprobación/stock/CxP que
    # cualquier otra (ver crear_factura_internacional / _calcular_prorrateo_internacional)
    # -- esta columna solo guarda el desglose (tipo de cambio, flete,
    # PayPal, DHL y el % de prorrateo que le tocó a cada línea) para poder
    # mostrarlo después en "Ver factura" y que quede trazable de dónde salió
    # el costo unitario final en pesos de cada producto. NULL en cualquier
    # factura que no se haya creado por este camino.
    columnas_ffp_v37 = [c[1] for c in db.execute("PRAGMA table_info(facturas_finanzas_pendientes)").fetchall()]
    if "internacional_json" not in columnas_ffp_v37:
        db.execute("ALTER TABLE facturas_finanzas_pendientes ADD COLUMN internacional_json TEXT")
        db.commit()

    # ---- Migración 38: "Tipo de cambio definitivo" de facturas internacionales
    # (pedido del taller, septiembre 2026) -- el dólar que se tipea al
    # ingresar una factura internacional es siempre TENTATIVO (el real recién
    # se sabe a fin de mes, cuando se paga la tarjeta en dólares), así que
    # hace falta poder corregirlo después y que la factura "cambie sus
    # precios en base a eso" -- ver /api/facturas/<id>/tipo-cambio-definitivo.
    # Para poder corregir con precisión el costo del stock que vino de ESA
    # factura puntual (y no mezclarlo con el resto de las compras del mismo
    # proveedor), stock_proveedores gana una dimensión extra opcional:
    # factura_id. NULL (default, todas las filas existentes y toda compra
    # NACIONAL de ahora en adelante) es el balde "de siempre", mezclado por
    # proveedor -- sin cambios de comportamiento para lo nacional. Un id real
    # solo lo usan las facturas internacionales (ver _registrar_factura,
    # parámetro es_internacional), donde cada factura queda en su propio
    # balde exclusivo. Ver _stockprov_bucket_get/_stockprov_bucket_entrada en
    # core.py para el detalle de cómo se arma la clave del balde.
    columnas_sp_v38 = [c[1] for c in db.execute("PRAGMA table_info(stock_proveedores)").fetchall()]
    if "factura_id" not in columnas_sp_v38:
        db.execute("ALTER TABLE stock_proveedores ADD COLUMN factura_id INTEGER REFERENCES facturas(id)")
        db.commit()

    # ---- Módulo Operativo (órdenes de trabajo): cada módulo separado
    # (ver modulos/) es dueño de su propia migración, para que agregar un
    # módulo nuevo no implique tocar el resto de init_db(). Va al final
    # porque orden_items referencia productos y movimientos.
    init_operativo_db(db)

    # ---- Sincronización con Talca.PHP (ver modulos/sync_talca.py y CLAUDE.md):
    # agrega clientes.talca_id + su índice único, clientes.comuna, y las tablas
    # de estado del sync. Va DESPUÉS de init_operativo_db() porque la tabla
    # `clientes` la crea ese módulo.
    init_sync_talca_db(db)

    db.close()


# ---------------------------------------------------------------------------
# Helpers propios de Logística (row_to_dict, now_iso, el cálculo de stock
# mínimo, etc. se movieron a core.py — ver el import al principio del archivo)
# ---------------------------------------------------------------------------

PRODUCTO_SELECT = """
    SELECT p.*, u.codigo AS ubicacion_codigo, u.pasillo AS ubicacion_pasillo,
           u.estanteria AS ubicacion_estanteria, u.nivel AS ubicacion_nivel
    FROM productos p
    LEFT JOIN ubicaciones u ON p.ubicacion_id = u.id
"""

# "Módulo de Aceites" (roadmap del taller, ver Migración 36): un aceite es,
# por debajo, un producto más de 'productos' -- mismo stock, mismos
# movimientos, mismo descuento automático al usarse en una orden de trabajo
# (orden_items.producto_id ya apunta a esta misma tabla) -- así se reutiliza
# TODO el motor de inventario ya probado en vez de construir un sistema
# paralelo. Lo único que distingue a un aceite de un repuesto común es la
# columna 'tipo_producto', que solo sirve para que la pestaña "Aceites" (en
# Logística) pueda filtrar y mostrar solo los suyos -- en todo lo demás
# (búsqueda de repuestos en una OT, en una factura de compra, control de
# stock, etc.) un aceite se comporta exactamente igual que cualquier otro
# producto, sin ningún código especial.
#
# El campo 'categoria' de un aceite guarda su "tipo" (a pedido del taller,
# una lista cerrada -- no una categoría libre como en los repuestos, así que
# NO pasa por el catálogo editable de categorías) y el campo 'modelo' guarda
# su "nombre" (texto libre, ej. "DEXRON VI SINTÉTICO") -- se reutilizan esas
# dos columnas ya existentes (en vez de agregar columnas nuevas) porque el
# nombre automático del producto (ver nombre_automatico en core.py) ya las
# combina como "TIPO - NOMBRE (MARCA)", que es justo lo que se necesita para
# encontrar un aceite por nombre en el buscador de repuestos de una OT.
TIPOS_PRODUCTO_VALIDOS = {"repuesto", "aceite"}
TIPOS_ACEITE_VALIDOS = ["CVT", "DCT", "ATF", "ATF Mineral", "75W90", "80W90"]


def _proveedores_origen_producto(db, producto_id):
    """Proveedores que efectivamente le han vendido este producto al taller
    (según las entradas registradas con factura), más si corresponde la
    opción "sin proveedor" cuando hay entradas sin factura/proveedor (ej. el
    stock inicial al crear el producto). Se usa para que, al marcar una
    pérdida, solo se puedan elegir proveedores reales de ESE producto."""
    proveedores = db.execute(
        """
        SELECT DISTINCT pr.id, pr.nombre
        FROM movimientos m
        JOIN facturas f ON f.numero = m.factura_numero
        JOIN proveedores pr ON pr.id = f.proveedor_id
        WHERE m.producto_id = ? AND m.tipo = 'entrada' AND f.proveedor_id IS NOT NULL
        ORDER BY pr.nombre COLLATE NOCASE
        """,
        (producto_id,),
    ).fetchall()

    tiene_sin_proveedor = db.execute(
        """
        SELECT COUNT(*) FROM movimientos m
        WHERE m.producto_id = ? AND m.tipo = 'entrada'
          AND (
            m.factura_numero IS NULL OR m.factura_numero = ''
            OR NOT EXISTS (
                SELECT 1 FROM facturas f
                WHERE f.numero = m.factura_numero AND f.proveedor_id IS NOT NULL
            )
          )
        """,
        (producto_id,),
    ).fetchone()[0] > 0

    return [dict(p) for p in proveedores], tiene_sin_proveedor


def _merma_bucket_get(db, producto_id, proveedor_id):
    if proveedor_id is None:
        return db.execute(
            "SELECT * FROM merma_proveedores WHERE producto_id = ? AND proveedor_id IS NULL",
            (producto_id,),
        ).fetchone()
    return db.execute(
        "SELECT * FROM merma_proveedores WHERE producto_id = ? AND proveedor_id = ?",
        (producto_id, proveedor_id),
    ).fetchone()


def _merma_bucket_sumar(db, producto_id, proveedor_id, delta):
    """Suma (o resta, con delta negativo) unidades al saldo de merma pendiente
    de un producto para un proveedor específico (o NULL = sin proveedor
    conocido). Crea el bucket si no existía; lo elimina si queda en 0."""
    bucket = _merma_bucket_get(db, producto_id, proveedor_id)
    ts = now_iso()
    if bucket is None:
        if delta <= 0:
            return  # nada que restar de un bucket que no existe
        db.execute(
            "INSERT INTO merma_proveedores (producto_id, proveedor_id, cantidad, actualizado_en) VALUES (?, ?, ?, ?)",
            (producto_id, proveedor_id, delta, ts),
        )
        return
    nuevo = bucket["cantidad"] + delta
    if nuevo <= 0:
        db.execute("DELETE FROM merma_proveedores WHERE id = ?", (bucket["id"],))
    else:
        db.execute(
            "UPDATE merma_proveedores SET cantidad = ?, actualizado_en = ? WHERE id = ?",
            (nuevo, ts, bucket["id"]),
        )


def _stockprov_buckets_producto(db, producto_id):
    """Desglose ACTUAL (solo baldes con cantidad > 0) del stock disponible
    de un producto por proveedor, para mostrarlo en la ficha del producto y
    para poblar el selector de "de qué proveedor sale esto" al registrar una
    salida. A diferencia de _proveedores_origen_producto (que mira TODO el
    historial de compras, sirve para la pérdida), esto solo muestra
    proveedores con stock vivo ahora mismo.

    Desde Migración 38, un mismo proveedor puede tener MÁS de un balde vivo
    a la vez: el balde mezclado de siempre (factura_id NULL, toda compra
    nacional) y, además, uno POR CADA factura internacional suya que
    todavía tenga stock sin consumir (ver _stockprov_bucket_get en core.py).
    Cada balde de factura internacional viene con 'factura_id'/
    'factura_numero' propios para poder distinguirlo en el selector (ver
    poblarOpcionesProveedorRepuesto en operativo.js); los baldes nacionales
    de siempre traen ambos en None, sin cambios de comportamiento."""
    filas = db.execute(
        """
        SELECT sp.proveedor_id, pr.nombre AS proveedor_nombre, sp.cantidad, sp.costo_promedio,
               sp.actualizado_en, sp.factura_id, f.numero AS factura_numero
        FROM stock_proveedores sp
        LEFT JOIN proveedores pr ON pr.id = sp.proveedor_id
        LEFT JOIN facturas f ON f.id = sp.factura_id
        WHERE sp.producto_id = ? AND sp.cantidad > 0
        ORDER BY (sp.proveedor_id IS NULL), pr.nombre COLLATE NOCASE, (sp.factura_id IS NULL), f.numero
        """,
        (producto_id,),
    ).fetchall()
    baldes = []
    permite_sin_proveedor = False
    for f in filas:
        if f["proveedor_id"] is None and f["factura_id"] is None:
            # El balde "sin proveedor conocido, sin factura" de siempre --
            # es el único caso donde una salida sin elegir nada (ej. desde
            # Logística) tiene sentido, por eso es el único que prende
            # permite_sin_proveedor.
            permite_sin_proveedor = True
            baldes.append({
                "proveedor_id": None,
                "proveedor_nombre": "Sin proveedor conocido (stock inicial / sin factura)",
                "cantidad": f["cantidad"],
                "costo_promedio": f["costo_promedio"],
                "factura_id": None,
                "factura_numero": None,
            })
        elif f["factura_id"] is not None:
            nombre_base = f["proveedor_nombre"] if f["proveedor_id"] is not None else "Sin proveedor"
            baldes.append({
                "proveedor_id": f["proveedor_id"],
                "proveedor_nombre": f"{nombre_base} — Factura internacional {f['factura_numero'] or f['factura_id']}",
                "cantidad": f["cantidad"],
                "costo_promedio": f["costo_promedio"],
                "factura_id": f["factura_id"],
                "factura_numero": f["factura_numero"],
            })
        else:
            baldes.append({
                "proveedor_id": f["proveedor_id"],
                "proveedor_nombre": f["proveedor_nombre"],
                "cantidad": f["cantidad"],
                "costo_promedio": f["costo_promedio"],
                "factura_id": None,
                "factura_numero": None,
            })
    return baldes, permite_sin_proveedor


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

RUTAS_PUBLICAS = {
    "login", "static",
    # Seguimiento público de una OT por patente (sin login) — ver modulos/operativo.py.
    "operativo.seguimiento_publico", "operativo.api_seguimiento_publico",
    # Webhook del agente de WhatsApp (Twilio nos llama sin sesión de este
    # sistema — la única verificación posible es la firma de Twilio, ver
    # _validar_firma_twilio).
    "webhook_whatsapp",
    # Chequeo de salud para Railway (y cualquier monitor externo tipo
    # UptimeRobot) -- sin login a propósito, porque quien pregunta "¿está
    # viva la app?" no tiene (ni debería necesitar) una sesión de usuario.
    "salud",
}


@app.before_request
def exigir_login():
    if request.endpoint in RUTAS_PUBLICAS or request.endpoint is None:
        return None
    if session.get("user_id"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Debes iniciar sesión"}), 401
    return redirect(url_for("login"))


@app.route("/health")
def salud():
    """Chequeo de salud trivial para el healthcheck de Railway (o cualquier
    monitor externo tipo UptimeRobot/BetterStack) -- a propósito NO consulta
    la base de datos ni nada más: solo confirma que el proceso de la app
    está vivo y respondiendo. Un healthcheck que dependiera de la base de
    datos podría marcar la app como "caída" durante, por ejemplo, una
    restauración de respaldo (que sí toma unos segundos con la base
    bloqueada), lo que sería un falso positivo. Si algún día se necesita
    distinguir "el proceso vive" de "la base responde", lo más simple es
    agregar un ?db=1 opcional acá en vez de complicar este endpoint por
    defecto."""
    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------------------
# Protección CSRF (Cross-Site Request Forgery)
# ---------------------------------------------------------------------------
# Cada sesión tiene un token secreto propio (guardado en la cookie de
# sesión, que ya es HttpOnly + SameSite=Lax). Se lo exponemos a las
# plantillas como <meta name="csrf-token"> y templates/_csrf.html se
# encarga de mandarlo en cada request que cambia datos (POST/PUT/PATCH/
# DELETE), agregándolo como cabecera X-CSRFToken. Acá lo validamos: si
# falta o no coincide con el que quedó guardado en la sesión, se rechaza
# la request. Esto es una capa adicional a SameSite=Lax (que ya bloquea
# la mayoría de los ataques CSRF típicos, pero no todos los casos).
CSRF_HEADER_NOMBRE = "X-CSRFToken"
CSRF_METODOS_PROTEGIDOS = {"POST", "PUT", "PATCH", "DELETE"}


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def _inyectar_csrf_token():
    return {"csrf_token": _csrf_token}


@app.before_request
def _validar_csrf():
    if request.method not in CSRF_METODOS_PROTEGIDOS:
        return None
    if request.endpoint in RUTAS_PUBLICAS or request.endpoint is None:
        return None
    # En la suite de tests (app.config["TESTING"] = True) el cliente de
    # pruebas de Flask no pasa por el navegador ni por templates/_csrf.html,
    # así que no tiene forma de mandar la cabecera. Es el mismo criterio que
    # ya usa Flask/Werkzeug para otras protecciones en modo test.
    if app.testing:
        return None
    if not session.get("user_id"):
        # exigir_login ya va a rechazar esta request por falta de sesión;
        # no hace falta duplicar el error acá.
        return None
    token_sesion = session.get("csrf_token")
    token_enviado = request.headers.get(CSRF_HEADER_NOMBRE, "")
    if not token_sesion or not hmac.compare_digest(token_sesion, token_enviado):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Token de seguridad inválido o expirado. Recarga la página e inténtalo de nuevo."}), 403
        return redirect(url_for("login"))
    return None


# ---------------------------------------------------------------------------
# Cabeceras de seguridad
# ---------------------------------------------------------------------------
# Se agregan en cada respuesta.
#
# Hallazgo #17 de la auditoría: el CSP permitía 'unsafe-inline' tanto en
# script-src como en style-src, lo que anula esa cabecera como segunda
# barrera si algún día apareciera una falla de XSS (un <script> inyectado se
# ejecutaría igual, porque 'unsafe-inline' lo permite todo). No había
# ninguna falla activa -- es refuerzo a futuro.
#
# style-src sigue con 'unsafe-inline': las plantillas usan cientos de
# style="" inline (más de 200 en templates/*.html hoy), así que sacarlo
# implicaría un refactor grande y riesgoso solo para bloquear inyección de
# CSS, que es un riesgo bastante menor que ejecución de JS arbitrario.
#
# script-src, en cambio, SÍ se pudo endurecer con poco riesgo: todo el
# sistema tiene solo 3 <script> inline (sin src=""), ninguno con variables
# de Jinja adentro (_csrf.html, _sidebar.html, migracion_datos.html), así
# que se identifican por su hash SHA-256 exacto (ver
# _calcular_hashes_scripts_inline más abajo) en vez de con 'unsafe-inline'
# -- cualquier OTRO script inline que aparezca en la página (ej. inyectado
# por una futura falla de XSS) no calza con ninguno de esos 3 hashes exactos
# y el navegador lo bloquea solo, que es justo el refuerzo que pedía este
# hallazgo.
#
# Aparte de esto, el CSP ya bloqueaba lo más peligroso: que este sitio se
# cargue dentro de un <iframe> ajeno (clickjacking) y que se carguen
# scripts/recursos desde dominios externos no autorizados.


def _calcular_hashes_scripts_inline():
    """Calcula los hashes 'sha256-<base64>' de los <script> inline estáticos
    (_csrf.html, _sidebar.html, _tema.html, migracion_datos.html -- los únicos <script>
    sin src="" de todo el sistema, y ninguno tiene variables de Jinja
    adentro, así que su contenido final es siempre el mismo que en el
    archivo fuente). Se calculan acá a partir de la plantilla real, no se
    escriben a mano como texto fijo -- si algún día se edita el contenido de
    cualquiera de los 3, el hash se recalcula solo en el próximo arranque,
    sin que a nadie se le olvide actualizarlo (olvidarlo dejaría ESE script
    bloqueado por el propio CSP -- rompería esa función puntual, no un
    problema de seguridad, pero igual conviene evitarlo)."""
    hashes = []
    directorio_templates = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    for nombre_archivo in ("_csrf.html", "_sidebar.html", "_tema.html", "migracion_datos.html"):
        with open(os.path.join(directorio_templates, nombre_archivo), encoding="utf-8") as f:
            contenido = f.read()
        for bloque in re.findall(r"<script>(.*?)</script>", contenido, re.DOTALL):
            digest = hashlib.sha256(bloque.encode("utf-8")).digest()
            hashes.append("'sha256-" + base64.b64encode(digest).decode() + "'")
    return " ".join(hashes)


_CSP_SCRIPT_SRC = "'self' " + _calcular_hashes_scripts_inline()


@app.after_request
def _agregar_cabeceras_seguridad(resp):
    # Caso especial: la vista previa de facturas/notas de crédito (finanzas.js
    # / operativo.js) muestra el PDF adentro de un <iframe> DE ESTE MISMO
    # sitio -- "frame-ancestors 'none'" + "X-Frame-Options: DENY" bloquean
    # CUALQUIER framing, incluso del propio origen, así que esa vista previa
    # quedaba siempre en blanco (aunque el mismo PDF sí abría bien en una
    # pestaña nueva, porque ahí no hay framing de por medio). Se relaja SOLO
    # para esta ruta puntual, a 'self' en vez de sacar la protección
    # anti-clickjacking del resto del sitio.
    es_pdf_dte = request.path.startswith("/api/facturacion/documentos/") and request.path.endswith("/pdf")
    resp.headers["X-Frame-Options"] = "SAMEORIGIN" if es_pdf_dte else "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    frame_ancestors = "frame-ancestors 'self'; " if es_pdf_dte else "frame-ancestors 'none'; "
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src {_CSP_SCRIPT_SRC}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        + frame_ancestors +
        "base-uri 'self'; "
        "form-action 'self'"
    )
    # HSTS: le dice al navegador que en adelante SIEMPRE use HTTPS con este
    # dominio. Es inofensivo mandarlo siempre — si la conexión es HTTP puro
    # (ej. en local) el navegador simplemente lo ignora.
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user_id"):
            return redirect(url_for("inicio"))
        return render_template("login.html")

    ip = request.remote_addr or "desconocida"
    db = get_db()
    if _login_bloqueado(db, ip):
        return render_template(
            "login.html",
            error=f"Demasiados intentos fallidos. Espera {LOGIN_VENTANA_SEGUNDOS // 60} minutos antes de volver a intentar.",
        ), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    usuario = db.execute("SELECT * FROM usuarios WHERE username = ?", (username,)).fetchone()

    if usuario is None or not check_password_hash(usuario["password_hash"], password):
        _registrar_intento_fallido(db, ip)
        return render_template("login.html", error="Usuario o contraseña incorrectos."), 401

    if not usuario["activo"]:
        _registrar_intento_fallido(db, ip)
        return render_template("login.html", error="Esta cuenta está desactivada. Habla con un administrador."), 403

    _limpiar_intentos(db, ip)
    session.clear()
    session["user_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["nombre"] = usuario["nombre"] or usuario["username"]
    session["rol"] = usuario["rol"] or "admin"
    # 'admin' no necesita módulos (ya puede todo); para cualquier otro
    # usuario se recalcula acá, en cada login, a partir de la columna real
    # -- así un cambio de módulos hecho desde Administración se refleja
    # recién en el próximo inicio de sesión de esa persona, nunca a mitad
    # de una sesión ya abierta (mismo criterio que 'rol' ya tenía).
    try:
        session["modulos"] = [] if session["rol"] == "admin" else json.loads(usuario["modulos_json"] or "[]")
    except (TypeError, ValueError):
        session["modulos"] = []
    session.permanent = True
    return redirect(url_for("inicio"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Administración de usuarios y roles — solo 'admin' (ver vista_administracion
# y el panel en templates/admin.html). No hay endpoint para borrar un
# usuario de verdad: se desactiva (usuarios.activo = 0), que le bloquea el
# login sin perder el rastro de qué hizo mientras la cuenta estuvo activa
# (nada más en el sistema referencia usuarios.id por llave foránea, así que
# un borrado real sería seguro en ese sentido, pero preferimos no perder
# historial por accidente).
# ---------------------------------------------------------------------------

def _usuario_admin_dict(row):
    d = dict(row)
    d.pop("password_hash", None)
    d["es_admin"] = d.get("rol") == "admin"
    try:
        d["modulos"] = [] if d["es_admin"] else json.loads(d.get("modulos_json") or "[]")
    except (TypeError, ValueError):
        d["modulos"] = []
    return d


def _validar_modulos_usuario(modulos):
    """Valida la lista de módulos que llega del formulario de usuario (ver
    crear_usuario_admin/editar_usuario_admin) -- debe ser una lista de
    strings, cada uno un módulo real (MODULOS_VALIDOS, ver core.py).
    Devuelve (modulos_limpios, error_o_None)."""
    if not isinstance(modulos, list) or any(not isinstance(m, str) for m in modulos):
        return None, f"Los módulos deben ser una lista. Usa alguno de: {', '.join(MODULOS_VALIDOS)}"
    limpios = []
    for m in modulos:
        m = m.strip().lower()
        if m not in MODULOS_VALIDOS:
            return None, f"Módulo inválido '{m}'. Usa alguno de: {', '.join(MODULOS_VALIDOS)}"
        if m not in limpios:
            limpios.append(m)
    return limpios, None


@app.route("/api/admin/usuarios", methods=["GET"])
@requiere_rol()
def listar_usuarios_admin():
    db = get_db()
    filas = db.execute("SELECT * FROM usuarios ORDER BY username COLLATE NOCASE").fetchall()
    return jsonify({"usuarios": [_usuario_admin_dict(f) for f in filas]})


@app.route("/api/admin/usuarios", methods=["POST"])
@requiere_rol()
def crear_usuario_admin():
    # A partir de la migración 17 (acceso a más de un módulo por usuario),
    # ya no se elige un único 'rol' desde el formulario -- se elige
    # es_admin (acceso total) o una lista de 'modulos' (uno o más). 'rol'
    # se sigue guardando en la fila por compatibilidad/legado (ver core.py)
    # pero se calcula acá mismo a partir de esos dos campos, nunca se
    # recibe directo del cliente.
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    nombre = (data.get("nombre") or "").strip()
    es_admin = bool(data.get("es_admin"))
    modulos, error = _validar_modulos_usuario(data.get("modulos") or [])
    if error:
        return jsonify({"error": error}), 400

    if not username:
        return jsonify({"error": "El nombre de usuario es obligatorio"}), 400
    if len(password) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400
    if not es_admin and not modulos:
        return jsonify({"error": "Selecciona al menos un módulo para este usuario, o márcalo como administrador."}), 400

    db = get_db()
    if db.execute("SELECT id FROM usuarios WHERE username = ?", (username,)).fetchone():
        return jsonify({"error": f"Ya existe un usuario con el nombre '{username}'"}), 400

    rol = "admin" if es_admin else modulos[0]
    modulos_json = "[]" if es_admin else json.dumps(modulos)
    cur = db.execute(
        "INSERT INTO usuarios (username, password_hash, nombre, rol, modulos_json, activo, creado_en) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (username, generate_password_hash(password), nombre or username, rol, modulos_json, now_iso()),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_usuario_admin_dict(nuevo)), 201


@app.route("/api/admin/usuarios/<int:usuario_id>", methods=["PUT"])
@requiere_rol()
def editar_usuario_admin(usuario_id):
    db = get_db()
    fila = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nombre = fila["nombre"]
    rol = fila["rol"]
    modulos_json = fila["modulos_json"] or "[]"
    activo = fila["activo"]

    if "nombre" in data:
        nombre = (data.get("nombre") or "").strip() or fila["username"]

    # es_admin y modulos se editan juntos -- si viene cualquiera de los
    # dos, se recalculan los dos (si solo viniera 'modulos' sin tocar
    # es_admin, se usa el es_admin actual de la fila; y viceversa).
    if "es_admin" in data or "modulos" in data:
        es_admin_nuevo = bool(data.get("es_admin")) if "es_admin" in data else (fila["rol"] == "admin")
        modulos_nuevo, error = _validar_modulos_usuario(data.get("modulos") if "modulos" in data else json.loads(modulos_json))
        if error:
            return jsonify({"error": error}), 400
        if not es_admin_nuevo and not modulos_nuevo:
            return jsonify({"error": "Selecciona al menos un módulo, o márcalo como administrador."}), 400
        if not es_admin_nuevo and fila["rol"] == "admin" and fila["id"] == session.get("user_id"):
            # No te puedes sacar a ti mismo el acceso admin — si lo hicieras
            # y fueras el único admin, nadie más podría volver a entrar al
            # panel de Administración a revertirlo.
            otros_admin = db.execute(
                "SELECT COUNT(*) FROM usuarios WHERE rol = 'admin' AND activo = 1 AND id != ?", (usuario_id,)
            ).fetchone()[0]
            if otros_admin == 0:
                return jsonify({"error": "No puedes quitarte el acceso admin: eres el único usuario admin activo."}), 400
        rol = "admin" if es_admin_nuevo else modulos_nuevo[0]
        modulos_json = "[]" if es_admin_nuevo else json.dumps(modulos_nuevo)

    if "activo" in data:
        activo = 1 if data.get("activo") else 0
        if not activo and fila["id"] == session.get("user_id"):
            return jsonify({"error": "No puedes desactivar tu propia cuenta."}), 400

    db.execute(
        "UPDATE usuarios SET nombre = ?, rol = ?, modulos_json = ?, activo = ? WHERE id = ?",
        (nombre, rol, modulos_json, activo, usuario_id),
    )
    db.commit()
    actualizado = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    return jsonify(_usuario_admin_dict(actualizado))


@app.route("/api/admin/usuarios/<int:usuario_id>/resetear-password", methods=["POST"])
@requiere_rol()
def resetear_password_usuario_admin(usuario_id):
    db = get_db()
    fila = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    password = data.get("password") or ""
    if len(password) < 6:
        return jsonify({"error": "La contraseña debe tener al menos 6 caracteres"}), 400

    db.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?", (generate_password_hash(password), usuario_id))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------
#
# La página de inicio ("/") es el selector de módulos: se ve justo después
# de iniciar sesión, y desde ahí se entra a cada módulo (Logística,
# Operativo, Finanzas, Agente IA, y los que se agreguen — ej. RRHH). Cada
# módulo tiene su propia plantilla y su propio archivo JS (ver static/js/),
# pero todos comparten el mismo login y la misma sesión — no hay que
# autenticarse de nuevo al cambiar de módulo.

@app.route("/")
def inicio():
    # El Panel Principal cruza datos de Operativo y Finanzas (ingresos,
    # CxC/CxP, utilidad) -- no es "de nadie en particular", así que solo
    # admin lo ve. Cualquier otro rol tiene un único módulo permitido (ver
    # ROL_MODULO_HOME en core.py) y va derecho ahí, en vez de aterrizar en
    # una pantalla que igual no le sirve de nada.
    if usuario_rol() != "admin":
        return redirect(home_del_rol())
    return render_template("inicio.html", modulo_activo="panel")


@app.route("/sin-acceso")
def vista_sin_acceso():
    # Caso raro (ver home_del_rol en core.py): un usuario no-admin al que
    # le quitaron todos los módulos asignados. En vez de dejarlo en un
    # loop de redirects hacia "/", se le explica la situación y se le da
    # la salida de cerrar sesión mientras un admin le vuelve a asignar
    # algún módulo desde Administración.
    if usuario_rol() == "admin" or usuario_modulos():
        return redirect(url_for("inicio"))
    return render_template("sin_acceso.html")


@app.route("/logistica")
@requiere_modulo("bodega")
def vista_logistica():
    return render_template("index.html", modulo_activo="logistica")


@app.route("/operativo")
@requiere_modulo("operativo")
def vista_operativo():
    return render_template("operativo.html", modulo_activo="operativo")


@app.route("/finanzas")
@requiere_modulo("finanzas", "bodega")
def vista_finanzas():
    # Bodega entra acá desde la nueva ruta de ingreso directo de facturas
    # (ver _crear_pendiente_y_aplicar_si_posible) -- pero solo debe ver la
    # pestaña "Facturas", no el resto de Finanzas (Caja Chica, CxC, CxP,
    # Gastos Generales, DTE, Estado de Resultados, Reportes). 'solo_facturas'
    # controla eso en la plantilla (nav de pestañas + qué vista se muestra
    # por defecto al cargar) -- ver templates/finanzas.html.
    solo_facturas = usuario_rol() != "admin" and "finanzas" not in usuario_modulos()
    return render_template("finanzas.html", modulo_activo="finanzas", solo_facturas=solo_facturas)


@app.route("/facturas/nueva")
@requiere_modulo("finanzas", "bodega")
def vista_factura_nueva():
    """Ventana dedicada (página aparte, no modal) para registrar una
    factura de compra paso a paso -- numero/proveedor/fecha, luego
    productos (con buscador, creación de producto nuevo con marca al
    vuelo, y la opción de descargar un repuesto directo a una OT), y por
    último la condición de pago. Reemplaza al modal "Registrar factura de
    compra" de Finanzas como punto de entrada para cargar una factura
    manualmente, ahora que el taller opera con una sola persona a cargo de
    Bodega y Facturas y ya no tiene sentido repartir esto en varias
    pantallas -- ver static/js/factura_nueva.js. El modal (y su flujo de
    revisar/editar/aprobar/rechazar) sigue existiendo para las facturas que
    llegan por WhatsApp, Excel o foto escaneada, que si necesitan ese paso
    de revisión aparte."""
    return render_template("factura_nueva.html", modulo_activo="finanzas")


@app.route("/facturas/nueva-internacional")
@requiere_modulo("finanzas", "bodega")
def vista_factura_internacional():
    """Ventana dedicada (página aparte, no modal -- mismo criterio que
    /facturas/nueva) para registrar una factura de compra internacional
    paso a paso: datos de la factura + de la importación (tipo de cambio,
    flete, PayPal, DHL/agente de aduana), luego productos con su costo
    unitario en dólares (con vista previa en vivo del prorrateo), y por
    último la condición de pago -- ver static/js/factura_internacional.js
    y POST /api/facturas/internacional (_calcular_prorrateo_internacional)
    para el cálculo real.

    A diferencia de /facturas/nueva, esta ventana NO tiene "guardar para
    continuar más adelante": recién se crea el registro real al terminar
    (paso 3), porque hasta ese punto no hay con qué calcular el costo final
    de cada línea (falta el tipo de cambio y/o los gastos de la
    importación) -- no tiene sentido guardar un pendiente a medio calcular."""
    return render_template("factura_internacional.html", modulo_activo="finanzas")


@app.route("/agente-ia")
@requiere_modulo("operativo")
def vista_agente_ia():
    return render_template("agente_ia.html", modulo_activo="agente")


@app.route("/administracion")
def vista_administracion():
    # No es un rol como los otros (finanzas/bodega/operativo no deberían
    # ni ver esta pantalla) — si alguien sin ser admin entra a la URL a
    # mano, se le manda de vuelta al selector de módulos en vez de mostrar
    # una pantalla vacía o un error feo. Los endpoints de abajo
    # (/api/admin/usuarios) igual están protegidos aparte con
    # @requiere_rol(), así que esto es solo para la página en sí.
    if usuario_rol() != "admin":
        return redirect(url_for("inicio"))
    return render_template("admin.html", modulo_activo="administracion")


@app.route("/api/sesion", methods=["GET"])
def sesion_actual():
    # 'rol' se mantiene tal cual por compatibilidad (ver core.py: 'admin'
    # sigue significando acceso total; para un usuario no-admin queda con
    # el primer módulo elegido, solo representativo). 'modulos' es la
    # lista real de módulos a los que tiene acceso (ver migración 17) --
    # el frontend debería usar SIEMPRE 'modulos' (o 'rol' == "admin") para
    # decidir qué mostrar, nunca comparar 'rol' contra un módulo puntual.
    return jsonify({
        "username": session.get("username"),
        "nombre": session.get("nombre"),
        "rol": usuario_rol(),
        "modulos": sorted(usuario_modulos()),
    })


# ---------------------------------------------------------------------------
# API: Productos
# ---------------------------------------------------------------------------

@app.route("/api/productos", methods=["GET"])
def listar_productos():
    db = get_db()
    q = request.args.get("q", "").strip()
    categoria = request.args.get("categoria", "").strip()
    solo_criticos = request.args.get("criticos") == "1"
    ubicacion_id = request.args.get("ubicacion_id", "").strip()
    tipo_producto = request.args.get("tipo_producto", "").strip().lower()

    sql = PRODUCTO_SELECT + " WHERE 1=1"
    params = []

    # Filtro opcional por tipo_producto ('repuesto' | 'aceite', ver
    # Migración 36) -- lo usa la pestaña "Aceites" de Logística para pedir
    # solo los suyos; el resto de pantallas (OT, facturas, Inventario) no
    # lo manda y siguen viendo todo mezclado, como siempre.
    if tipo_producto in TIPOS_PRODUCTO_VALIDOS:
        sql += " AND p.tipo_producto = ?"
        params.append(tipo_producto)

    # 'q' se filtra en Python (ver coincide_busqueda en core.py), no con SQL
    # LIKE: así la búsqueda ignora tildes/mayúsculas y admite varias palabras
    # sueltas que pueden calzar en campos distintos (ej. "6f35 bomba"
    # encuentra un producto cuyo código es "6F35" Y cuya descripción
    # menciona "bomba"), en vez de tener que acertarle a una frase exacta
    # dentro de un solo campo.

    if categoria:
        sql += " AND p.categoria = ?"
        params.append(categoria)

    if solo_criticos:
        sql += " AND p.stock_disponible <= p.stock_minimo"

    if ubicacion_id:
        sql += " AND p.ubicacion_id = ?"
        params.append(ubicacion_id)

    if request.args.get("sin_ubicacion") == "1":
        sql += " AND p.ubicacion_id IS NULL"

    # Cuántos productos NO tienen ubicación asignada en total (no depende de
    # los filtros de arriba) — se muestra siempre como contador junto al
    # filtro, para que se note el problema aunque no se esté usando el filtro.
    total_sin_ubicacion = db.execute(
        "SELECT COUNT(*) FROM productos WHERE ubicacion_id IS NULL"
    ).fetchone()[0]

    # La paginación es opcional (solo se activa si viene "pagina" o
    # "por_pagina" en la URL) porque este mismo endpoint lo usan varias
    # pantallas (Facturas, Operativo, Caja Chica, el selector de
    # movimientos) para llenar selectores con la lista COMPLETA de
    # productos — paginar siempre habría cortado esos selectores a la
    # primera página sin que nadie lo notara.
    paginar = request.args.get("pagina") is not None or request.args.get("por_pagina") is not None

    # Los tipos de aceite (ver Migración 36) no entran a este listado -- son
    # una lista cerrada aparte (TIPOS_ACEITE_VALIDOS), no "modelos" de
    # repuesto, así que no corresponde que aparezcan en este filtro.
    categorias = [r["categoria"] for r in db.execute(
        "SELECT DISTINCT categoria FROM productos WHERE categoria != '' AND tipo_producto != 'aceite' ORDER BY categoria"
    ).fetchall()]

    sql += " ORDER BY p.nombre COLLATE NOCASE ASC"
    rows = db.execute(sql, params).fetchall()
    productos = [row_to_dict(r) for r in rows]

    if q:
        productos = [
            p for p in productos
            if coincide_busqueda(
                q, p.get("codigo_interno"), p.get("nombre"), p.get("descripcion"),
                p.get("modelo"), p.get("ubicacion"), p.get("ubicacion_codigo"), p.get("id"),
            )
        ]

    if not paginar:
        return jsonify({
            "productos": productos,
            "categorias": categorias,
            "total": len(productos),
            "total_sin_ubicacion": total_sin_ubicacion,
        })

    total = len(productos)

    try:
        pagina = max(1, int(request.args.get("pagina", 1)))
    except (TypeError, ValueError):
        pagina = 1
    try:
        por_pagina = int(request.args.get("por_pagina", 50))
    except (TypeError, ValueError):
        por_pagina = 50
    por_pagina = min(max(por_pagina, 1), 500)
    total_paginas = max(1, math.ceil(total / por_pagina))
    pagina = min(pagina, total_paginas)

    inicio = (pagina - 1) * por_pagina
    productos = productos[inicio:inicio + por_pagina]

    return jsonify({
        "productos": productos,
        "categorias": categorias,
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total_paginas": total_paginas,
        "total_sin_ubicacion": total_sin_ubicacion,
    })


def _leer_excel_productos_masivo(archivo):
    """Lee un Excel de inventario en formato 'ID / MODELO / DESCRIPCION /
    CANTIDAD / CODIGO DE UBICACIÓN / PRECIO PROMEDIO' (una fila por
    producto, ver /api/productos/importar-excel/*) y devuelve
    (filas_validas, errores). 'filas_validas' es una lista de dicts ya
    listos para insertarse en 'productos'; 'errores' son advertencias en
    español sobre filas puntuales que no se pudieron leer bien — una fila
    mala NUNCA hace fallar el archivo completo, solo se excluye (o se
    corrige con un valor por defecto, dejando la advertencia)."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(archivo, data_only=True)
    except Exception as e:
        raise RuntimeError(f"No se pudo leer el archivo como Excel (.xlsx): {e}")

    ws = wb.worksheets[0]
    filas_validas = []
    errores = []
    vistos = {}  # (modelo, descripcion) normalizados -> primera fila donde aparecieron, para avisar de duplicados

    for i, fila in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if fila is None or all(v is None or str(v).strip() == "" for v in fila):
            continue  # fila completamente vacía, se ignora sin avisar

        valores = (list(fila) + [None] * 6)[:6]
        _id, modelo, descripcion, cantidad, ubicacion, precio = valores

        modelo = (str(modelo).strip() if modelo is not None else "")
        descripcion = (str(descripcion).strip() if descripcion is not None else "")
        if not descripcion:
            errores.append(f"Fila {i}: no tiene descripción, se omitió.")
            continue

        # Filas de resumen al final de la planilla (ej. "TOTAL:" con la
        # suma de cantidades/valores) no son productos — se detectan porque
        # no traen modelo y la descripción es justo esa palabra, y se
        # omiten sin avisar (no es un error, es esperable en este formato).
        if not modelo and descripcion.strip(":").strip().upper() in ("TOTAL", "TOTALES", "SUMA", "SUBTOTAL"):
            continue

        try:
            cantidad_num = int(float(cantidad)) if cantidad not in (None, "") else 0
        except (TypeError, ValueError):
            errores.append(f"Fila {i} ({descripcion}): la cantidad '{cantidad}' no es un número, se usó 0.")
            cantidad_num = 0

        try:
            precio_num = float(precio) if precio not in (None, "") else 0.0
        except (TypeError, ValueError):
            errores.append(f"Fila {i} ({descripcion}): el precio '{precio}' no es un número, se usó 0.")
            precio_num = 0.0

        ubicacion_txt = (str(ubicacion).strip() if ubicacion is not None else "")

        clave = (modelo.upper(), descripcion.upper())
        if clave in vistos:
            errores.append(
                f"Fila {i} ({modelo} - {descripcion}): mismo modelo y descripción que la fila "
                f"{vistos[clave]} — se importan ambas igual, pero revisa si es un duplicado real."
            )
        else:
            vistos[clave] = i

        filas_validas.append({
            "categoria": modelo.upper(),        # la columna externa MODELO va en 'categoria' (mostrado
            "modelo": descripcion.upper(),       # en la UI como "Modelo"), y DESCRIPCION va en 'modelo'
            "ubicacion": ubicacion_txt.upper(),  # (mostrado como "Detalle"), para que el nombre
                                                  # automático quede "MODELO - DESCRIPCION" como se pidió.
            "cantidad": max(0, cantidad_num),
            "costo_compra": max(0.0, precio_num),
        })

    return filas_validas, errores


@app.route("/api/productos/importar-excel/previsualizar", methods=["POST"])
@requiere_rol("bodega")
def productos_importar_excel_previsualizar():
    """Primer paso de 'Importar Excel' (reemplazo completo del catálogo):
    lee el archivo y devuelve un resumen (cuántas filas se importarían,
    cuántas tuvieron algún problema, valor total estimado) SIN TOCAR LA
    BASE DE DATOS TODAVÍA — el reemplazo real ocurre recién en
    /confirmar, y solo si el usuario lo confirma explícitamente desde la
    interfaz (ver 'confirmar_texto')."""
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Debes adjuntar un archivo Excel (.xlsx)"}), 400

    try:
        filas, errores = _leer_excel_productos_masivo(archivo)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    if not filas:
        return jsonify({"error": "El archivo no trae ninguna fila con al menos una descripción — no hay nada que importar."}), 400

    db = get_db()
    total_actuales = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    valor_total_estimado = sum(f["cantidad"] * f["costo_compra"] for f in filas)

    return jsonify({
        "total_filas_validas": len(filas),
        "total_advertencias": len(errores),
        "advertencias": errores[:30],
        "advertencias_recortadas": max(0, len(errores) - 30),
        "valor_total_estimado": round(valor_total_estimado, 2),
        "productos_actuales_a_reemplazar": total_actuales,
        "muestra": [
            {"nombre_resultante": f"{f['categoria']} - {f['modelo']}".strip(" -"),
             "ubicacion": f["ubicacion"], "cantidad": f["cantidad"], "costo_compra": f["costo_compra"]}
            for f in filas[:8]
        ],
    })


@app.route("/api/productos/importar-excel/confirmar", methods=["POST"])
@requiere_rol("bodega")
def productos_importar_excel_confirmar():
    """Reemplaza TODO el catálogo de productos por el contenido del Excel
    — acción destructiva e irreversible (no hay 'deshacer'), por eso exige
    'confirmar_texto' == 'REEMPLAZAR' además del archivo, para que no se
    pueda disparar por accidente con un solo clic ni con un doble-submit
    del formulario.

    Los productos actuales se BORRAN (no se intenta hacer merge/actualizar
    por nombre — a pedido explícito del taller). Cualquier movimiento,
    ítem de OT/garantía o línea de factura que apuntara a un producto
    viejo queda con una referencia que ya no existe ("huérfana"): el
    historial en sí no se borra, pero deja de poder mostrar el producto
    asociado. Por eso se desactiva 'PRAGMA foreign_keys' justo durante el
    reemplazo (igual que en las migraciones de esquema, ver
    modulos/operativo.py) — si no, SQLite directamente RECHAZARÍA borrar
    un producto todavía referenciado en vez de dejarlo huérfano.

    La ubicación de bodega del Excel (ej. '334') es un código antiguo del
    taller que no calza con el formato estructurado de este sistema
    (pasillo/estantería/nivel, ej. 'P3-E3-N4') — se guarda tal cual en el
    campo de ubicación libre ('pendiente de asignar' en la interfaz), para
    no inventar una conversión que podría quedar mal. Se puede asignar la
    ubicación estructurada real después, producto por producto, desde
    Productos → editar."""
    archivo = request.files.get("archivo")
    confirmar_texto = (request.form.get("confirmar_texto") or "").strip().upper()
    if confirmar_texto != "REEMPLAZAR":
        return jsonify({"error": "Falta confirmar la acción: escribe exactamente REEMPLAZAR para continuar."}), 400
    if not archivo or not archivo.filename:
        return jsonify({"error": "Debes adjuntar un archivo Excel (.xlsx)"}), 400

    try:
        filas, errores = _leer_excel_productos_masivo(archivo)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    if not filas:
        return jsonify({"error": "El archivo no trae ninguna fila con al menos una descripción — no hay nada que importar."}), 400

    db = get_db()
    ts = now_iso()
    total_actuales = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]

    # Fuera de una transacción activa a propósito (PRAGMA foreign_keys no
    # se puede cambiar en medio de una transacción) — mismo patrón que
    # modulos/operativo.py usa para sus migraciones de esquema.
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute("DELETE FROM productos")
        db.execute("DELETE FROM stock_minimo_calculo")
        db.execute("DELETE FROM stock_proveedores")
        for f in filas:
            cur = db.execute(
                """
                INSERT INTO productos (
                    codigo_interno, nombre, descripcion, categoria, marca, modelo,
                    ubicacion, ubicacion_id, stock_disponible, stock_reservado, stock_merma,
                    stock_minimo, costo_compra, creado_en, actualizado_en
                ) VALUES ('', '', '', ?, '', ?, ?, NULL, ?, 0, 0, 0, ?, ?, ?)
                """,
                (f["categoria"], f["modelo"], f["ubicacion"], f["cantidad"], f["costo_compra"], ts, ts),
            )
            nuevo_id = cur.lastrowid
            nombre = nombre_automatico(f["categoria"], f["modelo"], "", nuevo_id)
            db.execute("UPDATE productos SET nombre = ? WHERE id = ?", (nombre, nuevo_id))
            if f["cantidad"] > 0:
                db.execute(
                    """
                    INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, costo_unitario, stock_resultante)
                    VALUES (?, ?, 'entrada', 'Ajuste de inventario', ?, 'Carga inicial por importación de Excel', ?, ?, ?)
                    """,
                    (nuevo_id, ts, f["cantidad"], usuario_actual(), f["costo_compra"], f["cantidad"]),
                )
                # El Excel no trae proveedor por línea -- igual que el stock
                # inicial al crear un producto a mano, cae en el balde "sin
                # proveedor conocido" (ver Migración 22).
                db.execute(
                    "INSERT INTO stock_proveedores (producto_id, proveedor_id, cantidad, costo_promedio, actualizado_en) VALUES (?, NULL, ?, ?, ?)",
                    (nuevo_id, f["cantidad"], f["costo_compra"], ts),
                )
            _asegurar_categoria(db, f["categoria"])
            _asegurar_modelo(db, f["modelo"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

    for pid in [r["id"] for r in db.execute("SELECT id FROM productos").fetchall()]:
        _recalcular_stock_minimo(db, pid)
    db.commit()

    total_nuevos = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    return jsonify({
        "ok": True,
        "productos_eliminados": total_actuales,
        "productos_creados": total_nuevos,
        "filas_con_advertencia": len(errores),
    })


@app.route("/api/productos/<int:producto_id>", methods=["GET"])
@requiere_rol("bodega")
def obtener_producto(producto_id):
    db = get_db()
    row = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    movimientos = db.execute(
        """
        SELECT m.*, pr.nombre AS proveedor_nombre
        FROM movimientos m
        LEFT JOIN proveedores pr ON pr.id = m.proveedor_id
        WHERE m.producto_id = ?
        ORDER BY m.fecha DESC, m.id DESC
        """,
        (producto_id,),
    ).fetchall()

    producto = row_to_dict(row)
    producto["movimientos"] = [dict(m) for m in movimientos]
    producto["stock_minimo_detalle"] = _stock_minimo_detalle(db, producto_id)
    return jsonify(producto)


def _stock_minimo_detalle(db, producto_id):
    """Detalle de cómo se llegó al stock mínimo actual (para mostrar en la
    interfaz el 'por qué' del número, no solo el número)."""
    fila = db.execute("SELECT * FROM stock_minimo_calculo WHERE producto_id = ?", (producto_id,)).fetchone()
    return dict(fila) if fila else None


@app.route("/api/productos", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def crear_producto():
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    nuevo, error, status = _crear_producto_desde_datos(db, data)
    if error:
        return jsonify({"error": error}), status
    return jsonify(nuevo), 201


def _crear_producto_desde_datos(db, data):
    """Núcleo de crear_producto() -- factorizado para poder reusarlo desde
    crear_y_vincular_producto_pendiente() (Bodega crea un producto nuevo
    directo desde una línea de factura sin resolver), sin duplicar toda la
    validación. Devuelve (producto_dict, None, 201) si se creó, o
    (None, mensaje_error, status_code) si no."""
    codigo_interno = (data.get("codigo_interno") or "").strip().upper()
    categoria = (data.get("categoria") or "").strip().upper()
    modelo = (data.get("modelo") or "").strip().upper()
    marca = (data.get("marca") or "").strip().upper()

    # tipo_producto ('repuesto' | 'aceite') -- ver comentario junto a
    # PRODUCTO_SELECT / Migración 36. Por defecto 'repuesto' (comportamiento
    # de siempre); un aceite reutiliza 'categoria' como su "tipo" (lista
    # cerrada, ver TIPOS_ACEITE_VALIDOS) y 'modelo' como su "nombre" libre.
    tipo_producto = (data.get("tipo_producto") or "repuesto").strip().lower()
    if tipo_producto not in TIPOS_PRODUCTO_VALIDOS:
        return None, f"Tipo de producto inválido: '{tipo_producto}'", 400
    es_aceite = tipo_producto == "aceite"

    if codigo_interno:
        existe = db.execute(
            "SELECT id FROM productos WHERE codigo_interno = ?", (codigo_interno,)
        ).fetchone()
        if existe:
            return None, f"Ya existe un producto con código '{codigo_interno}'", 400

    if es_aceite:
        if categoria not in {t.upper() for t in TIPOS_ACEITE_VALIDOS}:
            return None, (
                f"Tipo de aceite inválido: '{categoria}'. Debe ser uno de: {', '.join(TIPOS_ACEITE_VALIDOS)}."
            ), 400
        if not modelo:
            return None, "El nombre del aceite es obligatorio.", 400

    if categoria or modelo:
        # La marca ahora es obligatoria en cuanto el producto tiene una
        # identidad real (modelo y/o detalle) -- antes era opcional, y eso
        # dejaba crear repuestos "sin marca" que después nunca se volvían a
        # preguntar: cualquier factura futura de ese mismo repuesto, sin
        # importar la marca real que trajera, terminaba mezclada bajo esa
        # misma fila sin marca, con un solo precio y sin poder rastrear cuál
        # unidad era de qué marca para la garantía. Si el repuesto de verdad
        # no tiene marca reconocible (ej. un perno genérico), se pide
        # escribir algo igual (ej. "GENÉRICA") en vez de dejarlo vacío. Para
        # un aceite aplica el mismo criterio (la marca del aceite).
        if not marca:
            etiqueta = "un aceite" if es_aceite else "un repuesto"
            return None, (
                f"La marca es obligatoria para crear {etiqueta} (ej. BOSCH, o \"GENÉRICA\" si no tiene una "
                "marca real) -- así se puede llevar el precio y el seguimiento de garantía por marca."
            ), 400
        # La marca entra al constraint de duplicados a propósito: un mismo
        # modelo/detalle puede existir en más de una marca (ej. una versión
        # china y una americana de la misma pieza), cada una con su propio
        # stock y costo -- por eso dos productos con igual categoria+modelo
        # pero distinta marca NO se consideran duplicados entre sí. Para un
        # aceite, esto equivale a "mismo tipo + mismo nombre + misma marca".
        duplicado = db.execute(
            "SELECT id, id AS pid FROM productos WHERE lower(trim(categoria)) = lower(trim(?)) AND lower(trim(modelo)) = lower(trim(?)) AND lower(trim(marca)) = lower(trim(?))",
            (categoria, modelo, marca),
        ).fetchone()
        if duplicado:
            msg = (
                f"Ya existe un aceite con ese tipo, nombre y marca (#{duplicado[0]:04d})."
                if es_aceite else
                f"Ya existe un producto con ese modelo, detalle y marca (#{duplicado[0]:04d})."
            )
            return None, (
                f"{msg} Si necesitas más stock, ingrésalo con una factura en vez de crear un producto nuevo."
            ), 400

    ts = now_iso()
    try:
        stock_disponible = int(data.get("stock_disponible") or 0)
        stock_reservado = int(data.get("stock_reservado") or 0)
        stock_minimo = int(data.get("stock_minimo") or 0)
        costo_compra = float(data.get("costo_compra") or 0)
    except (TypeError, ValueError):
        return None, "Stock y costo deben ser números", 400
    # Sin este chequeo se podía crear un producto con stock o costo
    # negativos (ej. stock_disponible=-5) sin ningún aviso -- quedaba
    # aceptado tal cual (hallazgo #12 del informe de revisión), y de ahí en
    # adelante cualquier cálculo que sume/reste sobre ese stock (reportes de
    # valorización, proyección de stock mínimo, la propia ficha del
    # producto) arrastraba un número que no tiene sentido físico.
    if stock_disponible < 0 or stock_reservado < 0 or stock_minimo < 0:
        return None, "El stock no puede ser negativo", 400
    if costo_compra < 0:
        return None, "El costo de compra no puede ser negativo", 400

    ubicacion_id = data.get("ubicacion_id") or None
    if ubicacion_id:
        try:
            ubicacion_id = int(ubicacion_id)
        except (TypeError, ValueError):
            return None, "Ubicación inválida", 400
        existe_ubi = db.execute("SELECT id FROM ubicaciones WHERE id = ?", (ubicacion_id,)).fetchone()
        if not existe_ubi:
            return None, "La ubicación seleccionada no existe", 400
        # Nota: una ubicación puede tener más de un producto (es normal en
        # una bodega real), así que aquí no se valida "ocupación".

    # El nombre se autogenera (no existe como campo del formulario); se
    # calcula una vez que sabemos el id definitivo del producto.
    cur = db.execute(
        """
        INSERT INTO productos (
            codigo_interno, nombre, descripcion, categoria, marca, modelo,
            ubicacion_id, stock_disponible, stock_reservado, stock_minimo,
            costo_compra, creado_en, actualizado_en, tipo_producto
        ) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            codigo_interno,
            # A diferencia de código/categoría/modelo/marca (que son casi
            # como "etiquetas" cortas y se ven mejor en mayúscula), la
            # descripción es texto libre más larga — forzarla a mayúscula
            # sostenida la hacía más difícil de leer, así que solo se
            # recorta espacios.
            (data.get("descripcion") or "").strip(),
            categoria,
            marca,
            modelo,
            ubicacion_id,
            stock_disponible,
            stock_reservado,
            stock_minimo,
            costo_compra,
            ts,
            ts,
            tipo_producto,
        ),
    )
    nuevo_id = cur.lastrowid
    nombre = nombre_automatico(categoria, modelo, codigo_interno, nuevo_id, marca)
    db.execute("UPDATE productos SET nombre = ? WHERE id = ?", (nombre, nuevo_id))

    # Si se crea con stock inicial, se deja registrado como movimiento de
    # entrada. No pasa por _aplicar_entrada_stock (el producto recién se
    # está creando, no existe fila previa que actualizar), así que el balde
    # de "stock por proveedor" correspondiente se arma acá a mano: al no
    # venir de una factura, siempre cae en el balde "sin proveedor conocido"
    # -- igual que un stock inicial cargado antes de que existiera este
    # desglose (ver Migración 22).
    if stock_disponible > 0:
        db.execute(
            """
            INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, costo_unitario, stock_resultante)
            VALUES (?, ?, 'entrada', 'Ajuste de inventario', ?, 'Stock inicial al crear producto', ?, ?, ?)
            """,
            (nuevo_id, ts, stock_disponible, usuario_actual(), costo_compra, stock_disponible),
        )
        _stockprov_bucket_entrada(db, nuevo_id, None, stock_disponible, costo_compra)

    # El stock mínimo se calcula solo desde el primer momento (con cero
    # historial da 0 — recién se establece un mínimo real una vez que el
    # producto acumule salidas), para que ningún producto quede con el
    # número que alguien haya tipeado a mano en el formulario.
    _recalcular_stock_minimo(db, nuevo_id)
    db.commit()

    # Un aceite NO agrega su tipo/nombre a los catálogos compartidos de
    # "modelos"/"detalles" de repuestos -- 'categoria' es una lista cerrada
    # (TIPOS_ACEITE_VALIDOS) y 'modelo' es texto libre propio de cada
    # aceite, ninguno de los dos corresponde al catálogo pensado para
    # repuestos (evita ensuciar el panel "Gestionar modelos/detalles" y el
    # filtro de la pestaña Productos con tipos de aceite). La marca sí se
    # comparte a propósito -- es el mismo catálogo de marcas de siempre.
    if not es_aceite:
        _asegurar_categoria(db, categoria)
        _asegurar_modelo(db, modelo)
    _asegurar_marca(db, marca)
    db.commit()

    nuevo = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (nuevo_id,)).fetchone()
    return row_to_dict(nuevo), None, 201


@app.route("/api/productos/<int:producto_id>", methods=["PUT"])
@requiere_rol("bodega")
def editar_producto(producto_id):
    db = get_db()
    row = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}

    codigo_interno = data.get("codigo_interno", row["codigo_interno"])
    codigo_interno = (codigo_interno or "").strip().upper()

    if codigo_interno:
        duplicado = db.execute(
            "SELECT id FROM productos WHERE codigo_interno = ? AND id != ?",
            (codigo_interno, producto_id),
        ).fetchone()
        if duplicado:
            return jsonify({"error": f"Ya existe otro producto con código '{codigo_interno}'"}), 400

    # El stock mínimo ya NO se edita a mano: se recalcula solo con cada
    # movimiento de stock (ver _recalcular_stock_minimo). Cualquier valor
    # que llegue en el body para este campo se ignora a propósito.
    stock_minimo = row["stock_minimo"]
    # El costo promedio TAMPOCO se edita a mano desde aquí: se recalcula
    # solo como promedio ponderado con cada entrada de stock (igual criterio
    # que el stock, para no perder trazabilidad — antes se podía sobreescribir
    # libremente el costo desde "Editar datos" sin dejar ningún movimiento
    # registrado, lo que podía descuadrar el valor de inventario en
    # silencio). Cualquier valor que llegue en el body para este campo
    # también se ignora a propósito; el único costo que se acepta por acá es
    # el "costo inicial" al CREAR el producto (ver crear_producto), nunca al
    # editarlo.
    costo_compra = row["costo_compra"]

    if "ubicacion_id" in data:
        ubicacion_id = data.get("ubicacion_id") or None
        if ubicacion_id:
            try:
                ubicacion_id = int(ubicacion_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Ubicación inválida"}), 400
            existe_ubi = db.execute("SELECT id FROM ubicaciones WHERE id = ?", (ubicacion_id,)).fetchone()
            if not existe_ubi:
                return jsonify({"error": "La ubicación seleccionada no existe"}), 400
            # Una ubicación puede tener más de un producto; no se valida "ocupación".
    else:
        ubicacion_id = row["ubicacion_id"]

    categoria = (data.get("categoria") if data.get("categoria") is not None else row["categoria"]).strip().upper()
    modelo = (data.get("modelo") if data.get("modelo") is not None else row["modelo"]).strip().upper()
    marca = (data.get("marca") if data.get("marca") is not None else row["marca"]).strip().upper()

    # tipo_producto NO se edita desde acá (ver Migración 36) -- un aceite se
    # queda como aceite para siempre; solo cambia si es 'aceite' para poder
    # aplicar las mismas reglas de "tipo cerrado" y "nombre obligatorio" que
    # en la creación.
    es_aceite = row["tipo_producto"] == "aceite"
    if es_aceite:
        if categoria not in {t.upper() for t in TIPOS_ACEITE_VALIDOS}:
            return jsonify({
                "error": f"Tipo de aceite inválido: '{categoria}'. Debe ser uno de: {', '.join(TIPOS_ACEITE_VALIDOS)}."
            }), 400
        if not modelo:
            return jsonify({"error": "El nombre del aceite es obligatorio."}), 400

    if categoria or modelo:
        # Misma lógica que en _crear_producto_desde_datos: la marca es
        # obligatoria en cuanto el producto tiene modelo y/o detalle (evita
        # que una edición le borre la marca a un producto que ya la tenía),
        # y entra al constraint de duplicados, así que dos productos con
        # igual categoria+modelo pero distinta marca (ej. versión china vs.
        # americana de la misma pieza) no chocan entre sí.
        if not marca:
            etiqueta = "este aceite" if es_aceite else "este repuesto"
            return jsonify({
                "error": (
                    f"La marca es obligatoria para {etiqueta} (ej. BOSCH, o \"GENÉRICA\" si no tiene una "
                    "marca real) -- así se puede llevar el precio y el seguimiento de garantía por marca."
                )
            }), 400
        duplicado_pm = db.execute(
            "SELECT id FROM productos WHERE lower(trim(categoria)) = lower(trim(?)) AND lower(trim(modelo)) = lower(trim(?)) AND lower(trim(marca)) = lower(trim(?)) AND id != ?",
            (categoria, modelo, marca, producto_id),
        ).fetchone()
        if duplicado_pm:
            msg = (
                f"Ya existe otro aceite con ese tipo, nombre y marca (#{duplicado_pm[0]:04d})."
                if es_aceite else
                f"Ya existe otro producto con ese modelo, detalle y marca (#{duplicado_pm[0]:04d})."
            )
            return jsonify({"error": msg}), 400

    nombre = nombre_automatico(categoria, modelo, codigo_interno, producto_id, marca)

    # Nota: el stock disponible/reservado NO se edita aquí directamente.
    # Se ajusta siempre a través de /movimiento para dejar trazabilidad.
    db.execute(
        """
        UPDATE productos SET
            codigo_interno = ?, nombre = ?, descripcion = ?, categoria = ?,
            marca = ?, modelo = ?, ubicacion_id = ?,
            stock_minimo = ?, costo_compra = ?, actualizado_en = ?
        WHERE id = ?
        """,
        (
            codigo_interno,
            nombre,
            (data.get("descripcion") if data.get("descripcion") is not None else row["descripcion"]).strip(),
            categoria,
            marca,
            modelo,
            ubicacion_id,
            stock_minimo,
            costo_compra,
            now_iso(),
            producto_id,
        ),
    )
    db.commit()

    # Ver el mismo criterio en _crear_producto_desde_datos: un aceite no
    # ensucia los catálogos compartidos de modelos/detalles de repuestos.
    if not es_aceite:
        _asegurar_categoria(db, categoria)
        _asegurar_modelo(db, modelo)
    _asegurar_marca(db, marca)
    db.commit()

    actualizado = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    return jsonify(row_to_dict(actualizado))


@app.route("/api/productos/<int:producto_id>", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_producto(producto_id):
    db = get_db()
    row = db.execute("SELECT id FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    forzar = request.args.get("forzar") == "1"

    cant_movimientos = db.execute(
        "SELECT COUNT(*) FROM movimientos WHERE producto_id = ?", (producto_id,)
    ).fetchone()[0]
    cant_lineas_factura = db.execute(
        "SELECT COUNT(*) FROM factura_lineas WHERE producto_id = ?", (producto_id,)
    ).fetchone()[0]

    if (cant_movimientos > 0 or cant_lineas_factura > 0) and not forzar:
        partes = []
        if cant_movimientos > 0:
            partes.append(f"{cant_movimientos} movimiento(s) de stock")
        if cant_lineas_factura > 0:
            partes.append(f"{cant_lineas_factura} línea(s) de factura")
        detalle = " y ".join(partes)
        return jsonify({
            "error": f"Este producto tiene {detalle} asociados. Si lo eliminas, se pierde ese historial de forma permanente.",
            "requiere_forzar": True,
        }), 409

    if forzar:
        # El producto deja de existir, así que su historial y su presencia
        # en facturas ya no tienen sentido: se eliminan junto con él.
        db.execute("DELETE FROM factura_lineas WHERE producto_id = ?", (producto_id,))
        # movimientos/reservas/merma_proveedores/stock_minimo_calculo/
        # stock_proveedores se eliminan solos por ON DELETE CASCADE.
        #
        # Estas otras cinco tablas SÍ referencian producto_id pero SIN
        # cascada, y cada una guarda su propia descripción/cantidad/monto
        # de forma independiente (no dependen de un join en vivo contra
        # productos para tener sentido) -- así que en vez de borrar esos
        # registros (perdiendo historial real de una OT, garantía,
        # convertidor o gasto de caja chica) se desvincula el producto ya
        # eliminado, dejando el registro intacto. Sin este paso, borrar un
        # producto que ya se hubiera usado en cualquiera de estos lugares
        # fallaba con "FOREIGN KEY constraint failed" (hallazgo #2 de la
        # auditoría de agosto 2026, confirmado en vivo con un producto
        # cargado en una OT).
        db.execute("UPDATE orden_items SET producto_id = NULL WHERE producto_id = ?", (producto_id,))
        db.execute("UPDATE garantia_items SET producto_id = NULL WHERE producto_id = ?", (producto_id,))
        db.execute("UPDATE convertidor_items SET producto_id = NULL WHERE producto_id = ?", (producto_id,))
        db.execute("UPDATE caja_movimientos SET producto_id = NULL WHERE producto_id = ?", (producto_id,))
        db.execute("UPDATE caja_gasto_detalles SET producto_id = NULL WHERE producto_id = ?", (producto_id,))

        # Los movimientos de este producto se van a borrar solos (CASCADE),
        # pero otras cinco tablas guardan una referencia SIN cascada hacia
        # ESE movimiento específico (de qué movimiento salió este ítem de
        # OT/garantía/convertidor, o a qué movimiento quedó ligado este
        # gasto de caja chica) -- si no se desvinculan primero, SQLite
        # bloquea igual el CASCADE de movimientos con el mismo error
        # "FOREIGN KEY constraint failed", solo que un paso más adelante.
        movimientos_del_producto = [
            r["id"] for r in db.execute("SELECT id FROM movimientos WHERE producto_id = ?", (producto_id,)).fetchall()
        ]
        if movimientos_del_producto:
            placeholders = ", ".join("?" for _ in movimientos_del_producto)
            for tabla, columna in [
                ("orden_items", "movimiento_id"),
                ("garantia_items", "movimiento_id"),
                ("convertidor_items", "movimiento_id"),
                ("caja_movimientos", "movimiento_inventario_id"),
                ("caja_gasto_detalles", "movimiento_inventario_id"),
            ]:
                db.execute(
                    f"UPDATE {tabla} SET {columna} = NULL WHERE {columna} IN ({placeholders})",
                    movimientos_del_producto,
                )

    fila_foto = db.execute("SELECT foto_archivo FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if fila_foto and fila_foto["foto_archivo"]:
        _borrar_foto_producto(fila_foto["foto_archivo"])

    db.execute("DELETE FROM productos WHERE id = ?", (producto_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Fotografía de producto (opcional)
# ---------------------------------------------------------------------------

def _borrar_foto_producto(nombre_archivo):
    if not nombre_archivo:
        return
    try:
        ruta = os.path.join(FOTOS_DIR, secure_filename(nombre_archivo))
        if os.path.exists(ruta):
            os.remove(ruta)
    except OSError:
        pass


@app.route("/api/productos/<int:producto_id>/foto", methods=["POST"])
@requiere_rol("bodega")
def subir_foto_producto(producto_id):
    db = get_db()
    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    archivo = request.files.get("foto")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Debes adjuntar una imagen"}), 400

    nombre_original = secure_filename(archivo.filename)
    ext = os.path.splitext(nombre_original)[1].lower()
    if ext not in EXTENSIONES_FOTO_PERMITIDAS:
        return jsonify({"error": f"Tipo de archivo no permitido ({ext or 'sin extensión'}). Usa una foto (jpg/png/webp/heic)."}), 400

    nombre_guardado = f"{uuid.uuid4().hex}{ext}"
    archivo.save(os.path.join(FOTOS_DIR, nombre_guardado))

    # Si ya tenía una foto anterior, se reemplaza (se borra la vieja del disco).
    if producto["foto_archivo"]:
        _borrar_foto_producto(producto["foto_archivo"])

    db.execute(
        "UPDATE productos SET foto_archivo = ?, actualizado_en = ? WHERE id = ?",
        (nombre_guardado, now_iso(), producto_id),
    )
    db.commit()
    return jsonify({"ok": True, "foto_archivo": nombre_guardado})


@app.route("/api/productos/<int:producto_id>/foto", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_foto_producto(producto_id):
    db = get_db()
    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    if producto["foto_archivo"]:
        _borrar_foto_producto(producto["foto_archivo"])
        db.execute(
            "UPDATE productos SET foto_archivo = '', actualizado_en = ? WHERE id = ?",
            (now_iso(), producto_id),
        )
        db.commit()

    return jsonify({"ok": True})


@app.route("/api/productos/foto/<path:nombre_archivo>", methods=["GET"])
@requiere_rol("bodega")
def obtener_foto_producto(nombre_archivo):
    nombre_seguro = secure_filename(nombre_archivo)
    ruta = os.path.join(FOTOS_DIR, nombre_seguro)
    if not os.path.exists(ruta):
        return jsonify({"error": "Foto no encontrada"}), 404
    return send_file(ruta)


# ---------------------------------------------------------------------------
# API: Movimientos (ingreso/salida/ajuste de stock, con trazabilidad)
# ---------------------------------------------------------------------------

@app.route("/api/productos/<int:producto_id>/movimiento", methods=["POST"])
@requiere_rol("bodega")
def registrar_movimiento(producto_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    row = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    tipo = (data.get("tipo") or "").strip().lower()
    motivo = (data.get("motivo") or "").strip().upper()
    usuario = usuario_actual()  # siempre quien tiene la sesión iniciada, nunca lo que mande el cliente
    factura_numero = (data.get("factura_numero") or "").strip().upper()

    if tipo not in ("entrada", "salida", "perdida"):
        return jsonify({"error": "Tipo de movimiento inválido (usa entrada, salida o perdida)"}), 400

    proveedor_id_perdida = None
    proveedor_id_salida = None
    orden_id_asociada = None
    if tipo == "perdida":
        categoria = "Merma o pérdida"
        # El proveedor de una pérdida SIEMPRE debe ser uno que efectivamente
        # le haya vendido este producto al taller (nunca se infiere del
        # último ingreso, porque un producto puede tener varios
        # proveedores). Es obligatorio elegir uno de la lista real.
        proveedores_posibles, permite_sin_proveedor = _proveedores_origen_producto(db, producto_id)
        ids_posibles = {p["id"] for p in proveedores_posibles}
        proveedor_id_crudo = data.get("proveedor_id")
        if proveedor_id_crudo in (None, ""):
            if not permite_sin_proveedor:
                return jsonify({"error": "Selecciona el proveedor del cual viene este producto para registrar la pérdida"}), 400
            proveedor_id_perdida = None
        else:
            try:
                proveedor_id_perdida = int(proveedor_id_crudo)
            except (TypeError, ValueError):
                return jsonify({"error": "Proveedor inválido"}), 400
            if proveedor_id_perdida not in ids_posibles:
                return jsonify({"error": "Ese proveedor no tiene ingresos registrados de este producto. Elige uno de la lista."}), 400
    else:
        categorias_permitidas = CATEGORIAS_ENTRADA if tipo == "entrada" else CATEGORIAS_SALIDA
        categoria_por_defecto = "Compra" if tipo == "entrada" else "Consumo interno"
        categoria = (data.get("categoria") or "").strip() or categoria_por_defecto
        if categoria not in categorias_permitidas:
            return jsonify({
                "error": f"Para {tipo}, la categoría debe ser una de: {', '.join(categorias_permitidas)}"
            }), 400

        if tipo == "salida":
            # De qué proveedor están saliendo estas unidades: se elige a
            # mano (no se reparte/infiera solo), para que el costo que
            # quede registrado -- y el que se le carga a una orden de
            # trabajo si corresponde -- sea el real de ESE proveedor.
            baldes_posibles, permite_sin_proveedor_salida = _stockprov_buckets_producto(db, producto_id)
            # Solo cuenta baldes NACIONALES (factura_id None) -- "Registrar
            # movimiento" (Logística) no tiene selector de factura
            # internacional, así que un proveedor cuyo ÚNICO stock vivo esté
            # en un balde de factura internacional no debe aparecer como
            # "disponible" acá (ver _stockprov_buckets_producto, Migración
            # 38); si se necesita descargar justo ese stock, se hace desde
            # una OT (agregar_item_orden), que sí sabe elegir entre baldes.
            ids_posibles_salida = {
                b["proveedor_id"] for b in baldes_posibles
                if b["proveedor_id"] is not None and b.get("factura_id") is None
            }
            proveedor_id_crudo_salida = data.get("proveedor_id")
            if proveedor_id_crudo_salida in (None, ""):
                if not permite_sin_proveedor_salida:
                    return jsonify({"error": "Selecciona de qué proveedor están saliendo estas unidades"}), 400
                proveedor_id_salida = None
            else:
                try:
                    proveedor_id_salida = int(proveedor_id_crudo_salida)
                except (TypeError, ValueError):
                    return jsonify({"error": "Proveedor inválido"}), 400
                if proveedor_id_salida not in ids_posibles_salida:
                    return jsonify({"error": "Ese proveedor no tiene stock disponible de este producto. Elige uno de la lista."}), 400

            # Orden de trabajo asociada: obligatoria si la categoría es
            # "Consumo interno" (siempre debe saberse a qué OT se le está
            # descargando el repuesto); opcional si es "Reserva" (se puede
            # reservar para una OT que todavía no existe en el sistema).
            if categoria in ("Consumo interno", "Reserva"):
                orden_id_crudo = (data.get("orden_id") or "")
                orden_id_crudo = str(orden_id_crudo).strip()
                if not orden_id_crudo:
                    if categoria == "Consumo interno":
                        return jsonify({"error": "Indica el número de la orden de trabajo a la que se le está descargando este repuesto"}), 400
                else:
                    try:
                        orden_id_asociada = int(orden_id_crudo)
                    except (TypeError, ValueError):
                        return jsonify({"error": "Número de orden de trabajo inválido"}), 400

    if factura_numero and tipo != "entrada":
        return jsonify({"error": "El número de factura solo aplica a movimientos de entrada"}), 400

    try:
        cantidad = int(data.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número entero"}), 400
    if cantidad <= 0:
        return jsonify({"error": "La cantidad debe ser mayor a 0"}), 400

    costo_unitario = None
    if tipo == "entrada" and data.get("costo_unitario") not in (None, ""):
        try:
            costo_unitario = float(data.get("costo_unitario"))
            if costo_unitario < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "El costo unitario debe ser un número válido"}), 400

    # ---- Si viene un número de factura, esta manda: la cantidad y el costo
    # se toman EXACTOS de la línea de esa factura para ese producto (no de
    # lo que haya tipeado el usuario), y esa línea queda marcada como usada.
    factura = None
    linea = None
    if factura_numero:
        factura = db.execute("SELECT * FROM facturas WHERE numero = ?", (factura_numero,)).fetchone()
        if not factura:
            return jsonify({"error": f"No existe una factura registrada con el número '{factura_numero}'"}), 400
        linea = db.execute(
            "SELECT * FROM factura_lineas WHERE factura_id = ? AND producto_id = ?",
            (factura["id"], producto_id),
        ).fetchone()
        if not linea:
            return jsonify({"error": f"Este producto no aparece en la factura '{factura_numero}'"}), 400
        if linea["aplicada"]:
            return jsonify({"error": f"La línea de este producto en la factura '{factura_numero}' ya fue ingresada a stock antes"}), 400
        cantidad = linea["cantidad"]
        costo_unitario = linea["precio_unitario"]

    stock_actual = row["stock_disponible"]

    if tipo == "entrada":
        nuevo_stock = _aplicar_entrada_stock(db, row, cantidad, costo_unitario, motivo, usuario, factura, categoria)
    elif tipo == "salida":
        # Una salida es la única que cambia el consumo real (una pérdida no
        # es "demanda", así que no se usa para proyectar el mínimo) — eso
        # ya lo maneja _aplicar_salida_stock (core.py), que también usa
        # el módulo Operativo cuando una orden de trabajo consume un
        # repuesto, para que ambos caminos queden siempre consistentes.
        if orden_id_asociada is not None:
            # Se pidió el número de OT: en vez de un movimiento genérico, el
            # producto queda formalmente asociado a esa orden (mismo efecto
            # que agregarlo desde la pestaña de repuestos de la OT), con la
            # categoría real elegida (Consumo interno o Reserva).
            _nuevo_item, _error = registrar_salida_directa_a_orden(
                db, orden_id_asociada, producto_id, cantidad,
                categoria=categoria, usuario=usuario, motivo=motivo,
                proveedor_id=proveedor_id_salida,
            )
            if _error:
                return jsonify({"error": _error[0]}), _error[1]
        else:
            try:
                nuevo_stock = _aplicar_salida_stock(
                    db, row, cantidad, motivo, usuario, categoria,
                    proveedor_id=proveedor_id_salida, proveedor_manual=True,
                )
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
    else:  # perdida -> el stock sale de "disponible" pero no desaparece: pasa a la cuenta de merma
        if cantidad > stock_actual:
            return jsonify({"error": f"No hay stock disponible suficiente para marcar como pérdida (disponible: {stock_actual})"}), 400
        nuevo_stock = stock_actual - cantidad
        nuevo_merma = row["stock_merma"] + cantidad
        ts = now_iso()
        db.execute(
            "UPDATE productos SET stock_disponible = ?, stock_merma = ?, actualizado_en = ? WHERE id = ?",
            (nuevo_stock, nuevo_merma, ts, producto_id),
        )
        db.execute(
            "INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, stock_resultante, proveedor_id) "
            "VALUES (?, ?, 'perdida', ?, ?, ?, ?, ?, ?)",
            (producto_id, ts, categoria, cantidad, motivo, usuario, nuevo_stock, proveedor_id_perdida),
        )
        _merma_bucket_sumar(db, producto_id, proveedor_id_perdida, cantidad)
        _stockprov_descontar_preferido(db, producto_id, proveedor_id_perdida, cantidad)

    if linea:
        db.execute(
            "UPDATE factura_lineas SET aplicada = 1, aplicada_en = ? WHERE id = ?",
            (now_iso(), linea["id"]),
        )

    db.commit()

    actualizado = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    return jsonify(row_to_dict(actualizado)), 201


# ---------------------------------------------------------------------------
# API: Ubicaciones (Módulo 3 - Organización Física de Bodega)
# ---------------------------------------------------------------------------

@app.route("/api/ubicaciones", methods=["GET"])
@requiere_rol("bodega")
def listar_ubicaciones():
    db = get_db()
    ubis = db.execute(
        "SELECT * FROM ubicaciones ORDER BY pasillo, estanteria, nivel"
    ).fetchall()

    ubicaciones = []
    for u in ubis:
        productos = db.execute(
            "SELECT id, codigo_interno, nombre, categoria, modelo FROM productos WHERE ubicacion_id = ? ORDER BY nombre COLLATE NOCASE",
            (u["id"],),
        ).fetchall()
        ubicaciones.append({
            "id": u["id"],
            "pasillo": u["pasillo"],
            "estanteria": u["estanteria"],
            "nivel": u["nivel"],
            "codigo": u["codigo"],
            "ocupada": len(productos) > 0,
            "productos": [dict(p) for p in productos],
        })

    return jsonify({"ubicaciones": ubicaciones, "total": len(ubicaciones)})


@app.route("/api/ubicaciones", methods=["POST"])
@requiere_rol("bodega")
def crear_ubicacion():
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()

    try:
        pasillo = int(data.get("pasillo"))
        estanteria = int(data.get("estanteria"))
        nivel = int(data.get("nivel"))
    except (TypeError, ValueError):
        return jsonify({"error": "Pasillo, estantería y nivel deben ser números enteros"}), 400

    if pasillo <= 0 or estanteria <= 0 or nivel <= 0:
        return jsonify({"error": "Pasillo, estantería y nivel deben ser mayores a 0"}), 400

    existe = db.execute(
        "SELECT id FROM ubicaciones WHERE pasillo = ? AND estanteria = ? AND nivel = ?",
        (pasillo, estanteria, nivel),
    ).fetchone()
    if existe:
        return jsonify({"error": "Esa ubicación (pasillo/estantería/nivel) ya existe"}), 400

    codigo = codigo_ubicacion(pasillo, estanteria, nivel)
    cur = db.execute(
        "INSERT INTO ubicaciones (pasillo, estanteria, nivel, codigo, creado_en) VALUES (?, ?, ?, ?, ?)",
        (pasillo, estanteria, nivel, codigo, now_iso()),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM ubicaciones WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nueva)), 201


@app.route("/api/ubicaciones/rango", methods=["POST"])
@requiere_rol("bodega")
def crear_ubicaciones_rango():
    """Crea de una sola vez todas las ubicaciones de un rango (ej. pasillo
    1 a 3, estantería 1 a 10, nivel 1 a 5 = hasta 150 ubicaciones), en vez
    de tener que crear una por una a mano. Las combinaciones que ya
    existieran se dejan tal cual (no se duplican ni se consideran error) —
    así el mismo rango se puede volver a enviar más adelante para "rellenar"
    ubicaciones nuevas sin tener que acordarse cuáles faltan."""
    data = request.get_json(force=True, silent=True) or {}

    try:
        pasillo_desde = int(data.get("pasillo_desde"))
        pasillo_hasta = int(data.get("pasillo_hasta"))
        estanteria_desde = int(data.get("estanteria_desde"))
        estanteria_hasta = int(data.get("estanteria_hasta"))
        nivel_desde = int(data.get("nivel_desde"))
        nivel_hasta = int(data.get("nivel_hasta"))
    except (TypeError, ValueError):
        return jsonify({"error": "Los seis valores del rango deben ser números enteros"}), 400

    if min(pasillo_desde, estanteria_desde, nivel_desde) <= 0:
        return jsonify({"error": "Los valores 'desde' deben ser mayores a 0"}), 400
    if pasillo_hasta < pasillo_desde or estanteria_hasta < estanteria_desde or nivel_hasta < nivel_desde:
        return jsonify({"error": "El valor 'hasta' de cada rango debe ser mayor o igual al 'desde'"}), 400

    total_solicitado = (
        (pasillo_hasta - pasillo_desde + 1)
        * (estanteria_hasta - estanteria_desde + 1)
        * (nivel_hasta - nivel_desde + 1)
    )
    LIMITE_LOTE = 500
    if total_solicitado > LIMITE_LOTE:
        return jsonify({
            "error": f"Ese rango generaría {total_solicitado} ubicaciones de una sola vez; el máximo por lote es "
                     f"{LIMITE_LOTE}. Achica el rango o créalas en varias tandas."
        }), 400

    db = get_db()
    existentes = {
        (r["pasillo"], r["estanteria"], r["nivel"])
        for r in db.execute("SELECT pasillo, estanteria, nivel FROM ubicaciones").fetchall()
    }

    ts = now_iso()
    creadas = 0
    for p in range(pasillo_desde, pasillo_hasta + 1):
        for e in range(estanteria_desde, estanteria_hasta + 1):
            for n in range(nivel_desde, nivel_hasta + 1):
                if (p, e, n) in existentes:
                    continue
                codigo = codigo_ubicacion(p, e, n)
                db.execute(
                    "INSERT INTO ubicaciones (pasillo, estanteria, nivel, codigo, creado_en) VALUES (?, ?, ?, ?, ?)",
                    (p, e, n, codigo, ts),
                )
                creadas += 1
    db.commit()

    return jsonify({
        "creadas": creadas,
        "omitidas": total_solicitado - creadas,
        "total_solicitado": total_solicitado,
    }), 201


@app.route("/api/ubicaciones/<int:ubicacion_id>/reasignar", methods=["PUT"])
@requiere_rol("bodega")
def reasignar_ubicacion(ubicacion_id):
    """Mueve de una sola vez TODOS los productos que hay en `ubicacion_id`
    hacia otra ubicación — para reorganizar la bodega sin editar producto
    por producto (ej. vaciar un pasillo completo hacia otro)."""
    db = get_db()
    origen = db.execute("SELECT id, codigo FROM ubicaciones WHERE id = ?", (ubicacion_id,)).fetchone()
    if origen is None:
        return jsonify({"error": "Ubicación de origen no encontrada"}), 404

    data = request.get_json(force=True, silent=True) or {}
    try:
        destino_id = int(data.get("destino_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Selecciona una ubicación de destino válida"}), 400

    if destino_id == ubicacion_id:
        return jsonify({"error": "La ubicación de destino debe ser distinta de la de origen"}), 400

    destino = db.execute("SELECT id FROM ubicaciones WHERE id = ?", (destino_id,)).fetchone()
    if destino is None:
        return jsonify({"error": "Ubicación de destino no encontrada"}), 400

    ts = now_iso()
    cur = db.execute(
        "UPDATE productos SET ubicacion_id = ?, actualizado_en = ? WHERE ubicacion_id = ?",
        (destino_id, ts, ubicacion_id),
    )
    db.commit()
    return jsonify({"movidos": cur.rowcount})


@app.route("/api/ubicaciones/<int:ubicacion_id>", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_ubicacion(ubicacion_id):
    db = get_db()
    row = db.execute("SELECT id FROM ubicaciones WHERE id = ?", (ubicacion_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Ubicación no encontrada"}), 404

    forzar = request.args.get("forzar") == "1"
    cant_productos = db.execute(
        "SELECT COUNT(*) FROM productos WHERE ubicacion_id = ?", (ubicacion_id,)
    ).fetchone()[0]

    if cant_productos > 0 and not forzar:
        return jsonify({
            "error": f"Esta ubicación tiene {cant_productos} producto(s) guardado(s). "
                     f"Puedes eliminarla de todas formas: esos productos quedan sin ubicación asignada.",
            "requiere_forzar": True,
        }), 409

    if forzar:
        db.execute("UPDATE productos SET ubicacion_id = NULL WHERE ubicacion_id = ?", (ubicacion_id,))

    db.execute("DELETE FROM ubicaciones WHERE id = ?", (ubicacion_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Categorías, Marcas y Modelos (listas precargadas, ampliables desde la interfaz)
# ---------------------------------------------------------------------------

def _asegurar_categoria(db, nombre):
    nombre = (nombre or "").strip().upper()
    if nombre:
        db.execute("INSERT OR IGNORE INTO categorias (nombre) VALUES (?)", (nombre,))


def _asegurar_modelo(db, nombre):
    nombre = (nombre or "").strip().upper()
    if nombre:
        db.execute("INSERT OR IGNORE INTO modelos (nombre) VALUES (?)", (nombre,))


def _asegurar_marca(db, nombre):
    nombre = (nombre or "").strip().upper()
    if nombre:
        db.execute("INSERT OR IGNORE INTO marcas (nombre) VALUES (?)", (nombre,))


def _recalcular_nombres_afectados(db, ids_productos):
    for pid in ids_productos:
        r = db.execute("SELECT id, codigo_interno, categoria, modelo, marca FROM productos WHERE id = ?", (pid,)).fetchone()
        if r:
            nuevo_nombre = nombre_automatico(r["categoria"], r["modelo"], r["codigo_interno"], r["id"], r["marca"])
            db.execute("UPDATE productos SET nombre = ? WHERE id = ?", (nuevo_nombre, r["id"]))


@app.route("/api/categorias", methods=["GET"])
@requiere_rol("bodega")
def listar_categorias():
    db = get_db()
    rows = db.execute(
        """
        SELECT c.id, c.nombre, COUNT(p.id) AS n_productos
        FROM categorias c
        LEFT JOIN productos p ON UPPER(TRIM(p.categoria)) = c.nombre
        GROUP BY c.id
        ORDER BY c.nombre COLLATE NOCASE
        """
    ).fetchall()
    return jsonify({"categorias": [dict(r) for r in rows]})


@app.route("/api/categorias", methods=["POST"])
@requiere_rol("bodega")
def crear_categoria():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre del modelo no puede estar vacío"}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO categorias (nombre) VALUES (?)", (nombre,))
    db.commit()
    return jsonify({"nombre": nombre}), 201


@app.route("/api/categorias/<int:categoria_id>", methods=["PUT"])
@requiere_rol("bodega")
def editar_categoria(categoria_id):
    """Renombra un 'modelo' (el campo interno sigue llamándose 'categoria'
    en la base de datos y en esta ruta -- ver nota de relabeling más abajo
    en core.py/nombre_automatico -- pero de cara al taller esto es "Modelo",
    ej. JF011, 722.6) — y actualiza en cascada todos los productos que ya
    lo tenían asignado, para corregir errores de tipeo sin tener que editar
    producto por producto."""
    db = get_db()
    actual = db.execute("SELECT * FROM categorias WHERE id = ?", (categoria_id,)).fetchone()
    if actual is None:
        return jsonify({"error": "Modelo no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nuevo_nombre = (data.get("nombre") or "").strip().upper()
    if not nuevo_nombre:
        return jsonify({"error": "El nombre del modelo no puede estar vacío"}), 400

    if nuevo_nombre != actual["nombre"]:
        choque = db.execute("SELECT id FROM categorias WHERE nombre = ? AND id != ?", (nuevo_nombre, categoria_id)).fetchone()
        if choque:
            return jsonify({"error": f"Ya existe un modelo llamado '{nuevo_nombre}'. Elimina uno de los dos para evitar duplicados."}), 400

    afectados = [r["id"] for r in db.execute("SELECT id FROM productos WHERE UPPER(TRIM(categoria)) = ?", (actual["nombre"],)).fetchall()]

    db.execute("UPDATE categorias SET nombre = ? WHERE id = ?", (nuevo_nombre, categoria_id))
    db.execute("UPDATE productos SET categoria = ? WHERE UPPER(TRIM(categoria)) = ?", (nuevo_nombre, actual["nombre"]))
    _recalcular_nombres_afectados(db, afectados)
    db.commit()
    return jsonify({"id": categoria_id, "nombre": nuevo_nombre, "productos_actualizados": len(afectados)})


@app.route("/api/categorias/<int:categoria_id>", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_categoria(categoria_id):
    """Quita el modelo de la lista precargada. Como se guarda como texto
    directo en cada producto (no es una llave foránea), esto no le cambia
    el dato a los productos que ya lo tenían — solo deja de sugerirse para
    productos nuevos."""
    db = get_db()
    actual = db.execute("SELECT * FROM categorias WHERE id = ?", (categoria_id,)).fetchone()
    if actual is None:
        return jsonify({"error": "Modelo no encontrado"}), 404
    n_productos = db.execute(
        "SELECT COUNT(*) FROM productos WHERE UPPER(TRIM(categoria)) = ?", (actual["nombre"],)
    ).fetchone()[0]
    db.execute("DELETE FROM categorias WHERE id = ?", (categoria_id,))
    db.commit()
    return jsonify({"ok": True, "productos_con_esta_categoria": n_productos})


@app.route("/api/modelos", methods=["GET"])
@requiere_rol("bodega")
def listar_modelos():
    db = get_db()
    rows = db.execute(
        """
        SELECT m.id, m.nombre, COUNT(p.id) AS n_productos
        FROM modelos m
        LEFT JOIN productos p ON UPPER(TRIM(p.modelo)) = m.nombre
        GROUP BY m.id
        ORDER BY m.nombre COLLATE NOCASE
        """
    ).fetchall()
    return jsonify({"modelos": [dict(r) for r in rows]})


@app.route("/api/modelos", methods=["POST"])
@requiere_rol("bodega")
def crear_modelo():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre del detalle no puede estar vacío"}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO modelos (nombre) VALUES (?)", (nombre,))
    db.commit()
    return jsonify({"nombre": nombre}), 201


@app.route("/api/modelos/<int:modelo_id>", methods=["PUT"])
@requiere_rol("bodega")
def editar_modelo(modelo_id):
    db = get_db()
    actual = db.execute("SELECT * FROM modelos WHERE id = ?", (modelo_id,)).fetchone()
    if actual is None:
        return jsonify({"error": "Detalle no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nuevo_nombre = (data.get("nombre") or "").strip().upper()
    if not nuevo_nombre:
        return jsonify({"error": "El nombre del detalle no puede estar vacío"}), 400

    if nuevo_nombre != actual["nombre"]:
        choque = db.execute("SELECT id FROM modelos WHERE nombre = ? AND id != ?", (nuevo_nombre, modelo_id)).fetchone()
        if choque:
            return jsonify({"error": f"Ya existe un detalle llamado '{nuevo_nombre}'. Elimina uno de los dos para evitar duplicados."}), 400

    afectados = [r["id"] for r in db.execute("SELECT id FROM productos WHERE UPPER(TRIM(modelo)) = ?", (actual["nombre"],)).fetchall()]

    db.execute("UPDATE modelos SET nombre = ? WHERE id = ?", (nuevo_nombre, modelo_id))
    db.execute("UPDATE productos SET modelo = ? WHERE UPPER(TRIM(modelo)) = ?", (nuevo_nombre, actual["nombre"]))
    _recalcular_nombres_afectados(db, afectados)
    db.commit()
    return jsonify({"id": modelo_id, "nombre": nuevo_nombre, "productos_actualizados": len(afectados)})


@app.route("/api/modelos/<int:modelo_id>", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_modelo(modelo_id):
    db = get_db()
    actual = db.execute("SELECT * FROM modelos WHERE id = ?", (modelo_id,)).fetchone()
    if actual is None:
        return jsonify({"error": "Detalle no encontrado"}), 404
    n_productos = db.execute(
        "SELECT COUNT(*) FROM productos WHERE UPPER(TRIM(modelo)) = ?", (actual["nombre"],)
    ).fetchone()[0]
    db.execute("DELETE FROM modelos WHERE id = ?", (modelo_id,))
    db.commit()
    return jsonify({"ok": True, "productos_con_este_modelo": n_productos})


@app.route("/api/marcas", methods=["GET"])
@requiere_rol("bodega")
def listar_marcas():
    db = get_db()
    rows = db.execute(
        """
        SELECT m.id, m.nombre, COUNT(p.id) AS n_productos
        FROM marcas m
        LEFT JOIN productos p ON UPPER(TRIM(p.marca)) = m.nombre
        GROUP BY m.id
        ORDER BY m.nombre COLLATE NOCASE
        """
    ).fetchall()
    return jsonify({"marcas": [dict(r) for r in rows]})


@app.route("/api/marcas", methods=["POST"])
@requiere_rol("bodega")
def crear_marca():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre de la marca no puede estar vacío"}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO marcas (nombre) VALUES (?)", (nombre,))
    db.commit()
    return jsonify({"nombre": nombre}), 201


@app.route("/api/marcas/<int:marca_id>", methods=["PUT"])
@requiere_rol("bodega")
def editar_marca(marca_id):
    db = get_db()
    actual = db.execute("SELECT * FROM marcas WHERE id = ?", (marca_id,)).fetchone()
    if actual is None:
        return jsonify({"error": "Marca no encontrada"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nuevo_nombre = (data.get("nombre") or "").strip().upper()
    if not nuevo_nombre:
        return jsonify({"error": "El nombre de la marca no puede estar vacío"}), 400

    if nuevo_nombre != actual["nombre"]:
        choque = db.execute("SELECT id FROM marcas WHERE nombre = ? AND id != ?", (nuevo_nombre, marca_id)).fetchone()
        if choque:
            return jsonify({"error": f"Ya existe una marca llamada '{nuevo_nombre}'. Elimina una de las dos para evitar duplicados."}), 400

    afectados = [r["id"] for r in db.execute("SELECT id FROM productos WHERE UPPER(TRIM(marca)) = ?", (actual["nombre"],)).fetchall()]

    db.execute("UPDATE marcas SET nombre = ? WHERE id = ?", (nuevo_nombre, marca_id))
    db.execute("UPDATE productos SET marca = ? WHERE UPPER(TRIM(marca)) = ?", (nuevo_nombre, actual["nombre"]))
    _recalcular_nombres_afectados(db, afectados)
    db.commit()
    return jsonify({"id": marca_id, "nombre": nuevo_nombre, "productos_actualizados": len(afectados)})


@app.route("/api/marcas/<int:marca_id>", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_marca(marca_id):
    db = get_db()
    actual = db.execute("SELECT * FROM marcas WHERE id = ?", (marca_id,)).fetchone()
    if actual is None:
        return jsonify({"error": "Marca no encontrada"}), 404
    n_productos = db.execute(
        "SELECT COUNT(*) FROM productos WHERE UPPER(TRIM(marca)) = ?", (actual["nombre"],)
    ).fetchone()[0]
    db.execute("DELETE FROM marcas WHERE id = ?", (marca_id,))
    db.commit()
    return jsonify({"ok": True, "productos_con_esta_marca": n_productos})


# ---------------------------------------------------------------------------
# API: Proveedores (Módulo 4 - Ingreso de Compras)
# ---------------------------------------------------------------------------

@app.route("/api/proveedores", methods=["GET"])
def listar_proveedores():
    db = get_db()
    rows = db.execute(
        """
        SELECT pr.*, COUNT(f.id) AS cantidad_facturas, MAX(f.fecha) AS ultima_compra
        FROM proveedores pr
        LEFT JOIN facturas f ON f.proveedor_id = pr.id
        GROUP BY pr.id
        ORDER BY pr.nombre COLLATE NOCASE
        """
    ).fetchall()
    return jsonify({"proveedores": [dict(r) for r in rows]})


@app.route("/api/proveedores", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def crear_proveedor():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre del proveedor es obligatorio"}), 400

    db = get_db()
    existe = db.execute("SELECT id FROM proveedores WHERE nombre = ?", (nombre,)).fetchone()
    if existe:
        return jsonify({"error": f"Ya existe un proveedor con el nombre '{nombre}'"}), 400

    rut_normalizado = formatear_rut((data.get("rut") or "").strip())
    if rut_normalizado:
        existe_rut = db.execute("SELECT id, nombre FROM proveedores WHERE rut = ? AND rut != ''", (rut_normalizado,)).fetchone()
        if existe_rut:
            return jsonify({"error": f"Ya existe un proveedor con ese RUT: '{existe_rut['nombre']}'"}), 400

    cur = db.execute(
        "INSERT INTO proveedores (nombre, rut, telefono, email, direccion, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (
            nombre,
            rut_normalizado,
            (data.get("telefono") or "").strip(),
            (data.get("email") or "").strip(),
            (data.get("direccion") or "").strip().upper(),
            now_iso(),
        ),
    )
    db.commit()
    nuevo = db.execute("SELECT *, 0 AS cantidad_facturas FROM proveedores WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@app.route("/api/proveedores/<int:proveedor_id>", methods=["PUT"])
@requiere_rol("bodega")
def editar_proveedor(proveedor_id):
    db = get_db()
    row = db.execute("SELECT * FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or row["nombre"]).strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre del proveedor es obligatorio"}), 400

    duplicado = db.execute(
        "SELECT id FROM proveedores WHERE nombre = ? AND id != ?", (nombre, proveedor_id)
    ).fetchone()
    if duplicado:
        return jsonify({"error": f"Ya existe otro proveedor con el nombre '{nombre}'"}), 400

    rut_nuevo = (data.get("rut") if data.get("rut") is not None else row["rut"]).strip()
    rut_normalizado = formatear_rut(rut_nuevo)
    if rut_normalizado:
        duplicado_rut = db.execute(
            "SELECT id, nombre FROM proveedores WHERE rut = ? AND rut != '' AND id != ?", (rut_normalizado, proveedor_id)
        ).fetchone()
        if duplicado_rut:
            return jsonify({"error": f"Ya existe otro proveedor con ese RUT: '{duplicado_rut['nombre']}'"}), 400

    db.execute(
        "UPDATE proveedores SET nombre = ?, rut = ?, telefono = ?, email = ?, direccion = ? WHERE id = ?",
        (
            nombre,
            rut_normalizado,
            (data.get("telefono") if data.get("telefono") is not None else row["telefono"]).strip(),
            (data.get("email") if data.get("email") is not None else row["email"]).strip(),
            (data.get("direccion") if data.get("direccion") is not None else row["direccion"]).strip().upper(),
            proveedor_id,
        ),
    )
    db.commit()
    actualizado = db.execute(
        """
        SELECT pr.*, COUNT(f.id) AS cantidad_facturas
        FROM proveedores pr LEFT JOIN facturas f ON f.proveedor_id = pr.id
        WHERE pr.id = ? GROUP BY pr.id
        """,
        (proveedor_id,),
    ).fetchone()
    return jsonify(dict(actualizado))


@app.route("/api/proveedores/<int:proveedor_id>", methods=["DELETE"])
@requiere_rol("bodega")
def eliminar_proveedor(proveedor_id):
    db = get_db()
    row = db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    forzar = request.args.get("forzar") == "1"
    # Todas las tablas que referencian proveedor_id sin cascada -- antes
    # solo se contaban/desvinculaban las facturas, pero movimientos,
    # cuentas por pagar, facturas pendientes de recepción, stock/merma por
    # proveedor, y los ítems de OT/garantía que registran de qué
    # proveedor salió un repuesto TAMBIÉN bloquean el DELETE si quedan
    # apuntando a un proveedor que ya no existe (hallazgo #2 de la
    # auditoría de agosto 2026). Cada una de estas tiene su propio dato
    # (monto, concepto, descripción) independiente del proveedor, así que
    # se desvincula (proveedor_id = NULL) en vez de borrar el registro.
    tablas_proveedor = [
        ("facturas", "factura(s)"),
        ("facturas_finanzas_pendientes", "factura(s) pendiente(s) de recepción"),
        ("cuentas_por_pagar", "cuenta(s) por pagar"),
        ("movimientos", "movimiento(s) de stock"),
        ("merma_proveedores", "registro(s) de merma"),
        ("stock_proveedores", "balde(s) de stock"),
        ("orden_items", "ítem(s) de OT"),
        ("garantia_items", "ítem(s) de garantía"),
    ]
    conteos = {
        tabla: db.execute(f"SELECT COUNT(*) FROM {tabla} WHERE proveedor_id = ?", (proveedor_id,)).fetchone()[0]
        for tabla, _ in tablas_proveedor
    }
    total_referencias = sum(conteos.values())

    if total_referencias > 0 and not forzar:
        detalle = " y ".join(f"{conteos[tabla]} {etiqueta}" for tabla, etiqueta in tablas_proveedor if conteos[tabla] > 0)
        return jsonify({
            "error": f"Este proveedor tiene {detalle} asociado(s). "
                     f"Puedes eliminarlo de todas formas: esos registros se conservan, solo quedan sin proveedor asignado.",
            "requiere_forzar": True,
        }), 409

    if forzar:
        # stock_proveedores/merma_proveedores son "baldes" que se agregan por
        # producto+proveedor (ver _stockprov_buckets_producto) -- si el
        # producto ya tenía un balde "sin proveedor conocido" (proveedor_id
        # NULL) y además el balde de ESTE proveedor, un simple SET NULL
        # dejaría dos baldes NULL duplicados para el mismo producto en vez de
        # fusionarse en uno. Se suma la cantidad al balde NULL existente (si
        # hay) y se borra el balde del proveedor eliminado; si no había balde
        # NULL todavía, el balde del proveedor simplemente pasa a serlo.
        for tabla in ("stock_proveedores", "merma_proveedores"):
            baldes_proveedor = db.execute(
                f"SELECT id, producto_id, cantidad FROM {tabla} WHERE proveedor_id = ?", (proveedor_id,)
            ).fetchall()
            for balde in baldes_proveedor:
                balde_null = db.execute(
                    f"SELECT id, cantidad FROM {tabla} WHERE producto_id = ? AND proveedor_id IS NULL",
                    (balde["producto_id"],),
                ).fetchone()
                if balde_null is None:
                    db.execute(f"UPDATE {tabla} SET proveedor_id = NULL WHERE id = ?", (balde["id"],))
                else:
                    db.execute(
                        f"UPDATE {tabla} SET cantidad = cantidad + ? WHERE id = ?",
                        (balde["cantidad"], balde_null["id"]),
                    )
                    db.execute(f"DELETE FROM {tabla} WHERE id = ?", (balde["id"],))

        for tabla, _ in tablas_proveedor:
            if tabla in ("stock_proveedores", "merma_proveedores"):
                continue  # ya resuelto arriba con la fusión de baldes
            db.execute(f"UPDATE {tabla} SET proveedor_id = NULL WHERE proveedor_id = ?", (proveedor_id,))

    db.execute("DELETE FROM proveedores WHERE id = ?", (proveedor_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Usuarios
# ---------------------------------------------------------------------------

@app.route("/api/usuarios", methods=["GET"])
@requiere_rol()
def listar_usuarios():
    db = get_db()
    rows = db.execute("SELECT id, username, nombre, creado_en FROM usuarios ORDER BY username COLLATE NOCASE").fetchall()
    return jsonify({"usuarios": [dict(r) for r in rows]})


@app.route("/api/usuarios", methods=["POST"])
@requiere_rol()
def crear_usuario():
    # Panel histórico de Logística (previo a que existieran los roles —
    # ver /administracion para el panel completo, con selector de rol).
    # Se restringió a solo-admin apenas se agregaron roles: antes cualquier
    # sesión iniciada podía crear/editar/borrar cualquier cuenta, lo que ya
    # no tiene sentido una vez que hay roles con distintos permisos. Los
    # usuarios creados por acá quedan con rol 'operativo' (el de menos
    # privilegios) por defecto — quien los cree puede ir a Administración
    # después a cambiarles el rol si corresponde.
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    nombre = (data.get("nombre") or "").strip().upper()

    if not username or not password:
        return jsonify({"error": "Usuario y contraseña son obligatorios"}), 400
    if len(password) < 4:
        return jsonify({"error": "La contraseña debe tener al menos 4 caracteres"}), 400

    db = get_db()
    existe = db.execute("SELECT id FROM usuarios WHERE username = ?", (username,)).fetchone()
    if existe:
        return jsonify({"error": f"Ya existe un usuario '{username}'"}), 400

    cur = db.execute(
        "INSERT INTO usuarios (username, password_hash, nombre, rol, modulos_json, activo, creado_en) VALUES (?, ?, ?, 'operativo', '[\"operativo\"]', 1, ?)",
        (username, generate_password_hash(password), nombre, now_iso()),
    )
    db.commit()
    nuevo = db.execute("SELECT id, username, nombre, creado_en FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@app.route("/api/usuarios/<int:usuario_id>", methods=["PUT"])
@requiere_rol()
def editar_usuario(usuario_id):
    db = get_db()
    row = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") if data.get("nombre") is not None else row["nombre"]) or ""
    nombre = nombre.strip().upper()
    password = data.get("password") or ""

    if password and len(password) < 4:
        return jsonify({"error": "La contraseña debe tener al menos 4 caracteres"}), 400

    if password:
        db.execute(
            "UPDATE usuarios SET nombre = ?, password_hash = ? WHERE id = ?",
            (nombre, generate_password_hash(password), usuario_id),
        )
    else:
        db.execute("UPDATE usuarios SET nombre = ? WHERE id = ?", (nombre, usuario_id))
    db.commit()

    if usuario_id == session.get("user_id"):
        session["nombre"] = nombre or session.get("username")

    actualizado = db.execute("SELECT id, username, nombre, creado_en FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    return jsonify(dict(actualizado))


@app.route("/api/usuarios/<int:usuario_id>", methods=["DELETE"])
@requiere_rol()
def eliminar_usuario(usuario_id):
    db = get_db()
    row = db.execute("SELECT id FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    total_usuarios = db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    if total_usuarios <= 1:
        return jsonify({"error": "No puedes eliminar el único usuario que queda"}), 400

    if usuario_id == session.get("user_id"):
        return jsonify({"error": "No puedes eliminar el usuario con el que iniciaste sesión"}), 400

    db.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Facturas (primera pieza del Módulo 4 - Ingreso de Compras)
# ---------------------------------------------------------------------------

FACTURA_SELECT = """
    SELECT f.*, pr.nombre AS proveedor_nombre
    FROM facturas f
    LEFT JOIN proveedores pr ON f.proveedor_id = pr.id
"""


def _factura_resumen(db, factura_row):
    lineas = db.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(aplicada), 0) AS aplicadas FROM factura_lineas WHERE factura_id = ?",
        (factura_row["id"],),
    ).fetchone()
    d = dict(factura_row)
    d["proveedor_display"] = d.get("proveedor_nombre") or d.get("proveedor") or ""
    d["total_lineas"] = lineas["total"]
    d["lineas_aplicadas"] = lineas["aplicadas"]
    return d


def _factura_detalle(db, factura_row):
    d = dict(factura_row)
    d["proveedor_display"] = d.get("proveedor_nombre") or d.get("proveedor") or ""
    lineas = db.execute(
        """
        SELECT fl.*, p.nombre AS producto_nombre, p.id AS producto_id_num,
               p.codigo_interno AS producto_codigo_interno
        FROM factura_lineas fl
        JOIN productos p ON p.id = fl.producto_id
        WHERE fl.factura_id = ?
        ORDER BY fl.id
        """,
        (factura_row["id"],),
    ).fetchall()
    d["lineas"] = []
    for l in lineas:
        ld = dict(l)
        ld["producto_id_sistema"] = f"{ld['producto_id_num']:04d}"
        d["lineas"].append(ld)

    # Ítems de gasto general de esta misma factura (ver
    # factura_gastos_generales) -- una factura puede traer ambos tipos de
    # línea a la vez (ej. un filtro que va a inventario + la herramienta
    # para sacarlo, que no), así que se muestran juntos al ver el detalle.
    gastos = db.execute(
        "SELECT id, descripcion, monto, rubro_gav, creado_en FROM factura_gastos_generales "
        "WHERE factura_id = ? ORDER BY id",
        (factura_row["id"],),
    ).fetchall()
    d["gastos_generales"] = [{
        **dict(g), "rubro_gav_label": RUBROS_GAV_QUE_SUMAN_GAV.get(g["rubro_gav"], g["rubro_gav"]),
    } for g in gastos]
    return d


@app.route("/api/facturas", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def listar_facturas():
    db = get_db()
    proveedor_id = request.args.get("proveedor_id", "").strip()
    estado = request.args.get("estado", "").strip()  # "pendiente" | "parcial" | "completa"

    sql = FACTURA_SELECT
    params = []
    if proveedor_id:
        try:
            sql += " WHERE f.proveedor_id = ?"
            params.append(int(proveedor_id))
        except ValueError:
            return jsonify({"error": "Proveedor inválido"}), 400
    sql += " ORDER BY f.creado_en DESC, f.id DESC"

    rows = db.execute(sql, params).fetchall()
    facturas = [_factura_resumen(db, r) for r in rows]

    if estado in ("pendiente", "parcial", "completa"):
        def _estado_factura(f):
            if f["total_lineas"] == 0:
                return "pendiente"
            if f["lineas_aplicadas"] == 0:
                return "pendiente"
            if f["lineas_aplicadas"] == f["total_lineas"]:
                return "completa"
            return "parcial"
        facturas = [f for f in facturas if _estado_factura(f) == estado]

    return jsonify({"facturas": facturas})


@app.route("/api/facturas/<int:factura_id>", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def obtener_factura(factura_id):
    db = get_db()
    row = db.execute(FACTURA_SELECT + " WHERE f.id = ?", (factura_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Factura no encontrada"}), 404
    return jsonify(_factura_detalle(db, row))


@app.route("/api/facturas/numero/<path:numero>", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def obtener_factura_por_numero(numero):
    db = get_db()
    row = db.execute(FACTURA_SELECT + " WHERE f.numero = ?", (numero,)).fetchone()
    if row is None:
        return jsonify({"error": "No existe una factura con ese número"}), 404
    return jsonify(_factura_detalle(db, row))


@app.route("/api/facturas/verificar-numero/<path:numero>", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def verificar_numero_factura(numero):
    """Chequeo liviano (solo lectura, sin lock) de duplicados por número,
    pensado para avisar apenas se ingresa el número en el paso 1 del
    asistente de facturas nuevas (/facturas/nueva) -- antes esto solo se
    detectaba al final, al intentar crear/aplicar la factura con los tres
    pasos ya llenos, lo que obligaba a rehacer todo el trabajo si el
    número resultaba repetido. Mismo criterio de duplicado que
    _crear_pendiente_recepcion (factura ya aplicada, o pendiente sin
    resolver con el mismo número) -- la validación real y con lock sigue
    ocurriendo ahí, esto es solo para avisar temprano.

    'excluir_pendiente_id' se usa al editar un borrador ya guardado, para
    que no se marque a sí mismo como duplicado de sí mismo.
    """
    db = get_db()
    numero_norm = (numero or "").strip().upper()
    excluir_pendiente_id = request.args.get("excluir_pendiente_id", type=int)
    if not numero_norm:
        return jsonify({"existe": False})
    existe = db.execute("SELECT id FROM facturas WHERE numero = ?", (numero_norm,)).fetchone()
    if existe:
        return jsonify({
            "existe": True,
            "mensaje": f"Ya existe un documento registrado con el número '{numero_norm}'",
        })
    query = "SELECT id FROM facturas_finanzas_pendientes WHERE numero = ? AND estado = 'pendiente'"
    params = [numero_norm]
    if excluir_pendiente_id:
        query += " AND id != ?"
        params.append(excluir_pendiente_id)
    ya_pendiente = db.execute(query, params).fetchone()
    if ya_pendiente:
        return jsonify({
            "existe": True,
            "mensaje": f"Ya hay un documento pendiente de recepción con el número '{numero_norm}'",
        })
    return jsonify({"existe": False})


# NOTA: el registro manual "rápido" de una factura (POST /api/facturas, con
# aplicación de stock inmediata y sin pasar por aprobación de Bodega) se
# eliminó — el taller pidió unificar todo bajo "pendientes de recepción"
# (ver _crear_pendiente_recepcion / aprobar_factura_pendiente_recepcion más
# abajo), que es el único camino de entrada ahora, para manual, Excel, foto
# escaneada y WhatsApp por igual.

# Cuentas por Pagar (CxP) — plazo por defecto para pagarle a un proveedor,
# usado solo para calcular 'fecha_vencimiento' al generar la CxP de una
# factura de compra (ver _generar_cxp_de_factura). Mismo criterio que
# CXC_DIAS_VENCIMIENTO_DEFECTO en modulos/operativo.py: no es un plazo
# legal, es el que el taller quiere usar para marcar una deuda a
# proveedor como "vencida" en los reportes.
CXP_DIAS_VENCIMIENTO_DEFECTO = 30


def _generar_cxp_de_factura(db, factura_id, proveedor_id, fecha, lineas_validadas,
                              condicion_pago="credito", porcentaje_contado=0, plazo_dias_credito=None,
                              lineas_gasto_general=None, exento_iva=False):
    """Genera la(s) Cuenta(s) por Pagar de una factura de compra recién
    registrada — el monto total es la suma de cantidad×precio_unitario de
    todas sus líneas de producto MÁS el monto de sus líneas de gasto
    general (ver lineas_gasto_general) MÁS el IVA (19%, salvo que
    exento_iva) sobre ese neto (ver Migración 33) -- porque al proveedor se
    le debe la factura completa, con IVA incluido, sin importar cómo se
    clasificó cada línea puertas adentro. Se llama SIEMPRE desde
    _registrar_factura (nunca a mano), así que cualquier factura que entre
    por cualquiera de sus caminos (registro manual, carga masiva por Excel,
    foto de WhatsApp aprobada, o aprobación de recepción desde Finanzas)
    genera su CxP sola, sin que se pueda olvidar. No se genera nada si el
    total es $0 (ej. factura de solo devoluciones).

    condicion_pago/porcentaje_contado/plazo_dias_credito solo los manda
    aprobar_factura_pendiente_recepcion (ver más abajo) — las facturas de
    los otros tres caminos no pasan por la revisión de condiciones de pago
    y siguen generando su CxP igual que siempre: 100% a crédito, a
    CXP_DIAS_VENCIMIENTO_DEFECTO días.
      - 'contado': se paga íntegro en el momento. Se genera la CxP igual
        (para no perder el registro de cuánto se le compró a ese
        proveedor) pero ya con su abono registrado y 'estado' en
        'pagada' — no queda nada pendiente.
      - 'credito': se genera pendiente, con vencimiento a plazo_dias_credito.
      - 'mixto': se genera UNA CxP por el tramo al contado (ya pagada) y
        OTRA por el tramo a crédito (pendiente, con su propio
        vencimiento) — ambas apuntando a la misma factura_id."""
    monto_neto = round(sum(cantidad * precio_unitario for _p, cantidad, precio_unitario in lineas_validadas), 2)
    monto_neto += round(sum(l["monto"] for l in (lineas_gasto_general or [])), 2)
    monto_neto = round(monto_neto, 2)
    monto_iva = 0 if exento_iva else round(monto_neto * 0.19, 2)
    monto_total = round(monto_neto + monto_iva, 2)
    if monto_total <= 0:
        return

    if plazo_dias_credito is None:
        plazo_dias_credito = CXP_DIAS_VENCIMIENTO_DEFECTO
    porcentaje_contado = max(0.0, min(100.0, porcentaje_contado or 0))
    if condicion_pago == "contado":
        porcentaje_contado = 100.0
    elif condicion_pago == "credito":
        porcentaje_contado = 0.0

    monto_contado = round(monto_total * porcentaje_contado / 100, 2)
    monto_credito = round(monto_total - monto_contado, 2)

    fecha_emision = fecha or now_iso()
    ts = now_iso()

    if monto_contado > 0:
        cur = db.execute(
            """
            INSERT INTO cuentas_por_pagar
                (proveedor_id, factura_id, concepto, monto_total, fecha_emision, fecha_vencimiento, estado, usuario, creado_en, actualizado_en)
            VALUES (?, ?, 'Tramo al contado', ?, ?, ?, 'pagada', ?, ?, ?)
            """,
            (proveedor_id, factura_id, monto_contado, fecha_emision, fecha_emision, usuario_actual(), ts, ts),
        )
        db.execute(
            """
            INSERT INTO cxp_pagos (cxp_id, monto, fecha, metodo_pago, desde_caja_chica, nota, usuario, creado_en)
            VALUES (?, ?, ?, 'contado', 0, 'Pago al contado registrado junto con la aprobación de recepción de la factura', ?, ?)
            """,
            (cur.lastrowid, monto_contado, fecha_emision, usuario_actual(), ts),
        )

    if monto_credito > 0:
        fecha_vencimiento = sumar_dias_iso(fecha_emision, plazo_dias_credito)
        db.execute(
            """
            INSERT INTO cuentas_por_pagar
                (proveedor_id, factura_id, concepto, monto_total, fecha_emision, fecha_vencimiento, estado, usuario, creado_en, actualizado_en)
            VALUES (?, ?, ?, ?, ?, ?, 'pendiente', ?, ?, ?)
            """,
            (proveedor_id, factura_id, "Tramo a crédito" if monto_contado > 0 else "",
             monto_credito, fecha_emision, fecha_vencimiento, usuario_actual(), ts, ts),
        )


def _registrar_factura(db, numero, proveedor_id, fecha, lineas_validadas, usuario, aplicar_stock=True,
                        condicion_pago="credito", porcentaje_contado=0, plazo_dias_credito=None,
                        lineas_gasto_general=None, exento_iva=False, lineas_ot_destino=None,
                        tipo_documento="factura", es_internacional=False):
    """Inserta la factura y sus líneas, aplicando el ingreso de stock si
    corresponde, y generando su(s) Cuenta(s) por Pagar (ver
    _generar_cxp_de_factura). Único llamador hoy:
    aprobar_factura_pendiente_recepcion — manual, Excel, foto escaneada y
    WhatsApp entran TODOS por 'pendientes de recepción' primero (ver
    _crear_pendiente_recepcion) y esta función recién se ejecuta al
    aprobar, sin importar el origen.

    lineas_gasto_general son ítems que llegaron en la misma factura pero
    NO son repuestos (ej. combustible, una herramienta) -- van a la tabla
    aparte factura_gastos_generales, nunca a factura_lineas ni a stock, a
    pedido del taller de mantener el catálogo de productos estricto (ver
    tabla en init_db). Sí se suman al total de la CxP igual que cualquier
    otra línea (ver _generar_cxp_de_factura).

    monto_neto/monto_iva/monto_total quedan guardados en la propia fila de
    'facturas' (ver Migración 33) -- el precio de cada línea es el NETO, y
    de acá sale exactamente el mismo cálculo que usa _generar_cxp_de_factura
    para la Cuenta por Pagar, así el detalle de la factura y lo que se le
    debe al proveedor siempre calzan.

    lineas_ot_destino es un dict {producto_id: {'orden_id': N}} para
    líneas marcadas "directo a una orden de trabajo" (ver
    _validar_destino_ot_linea / la nueva ventana de Facturas en
    /facturas/nueva): además de ingresar el stock como cualquier línea, se
    descarga esa misma cantidad de inmediato hacia la OT indicada, en la
    misma operación -- reutiliza registrar_salida_directa_a_orden, el
    mismo camino que ya usa Operativo para cargar un repuesto a una OT
    (ver modulos/operativo.py), así el costeo/historial queda idéntico.

    es_internacional (Migración 38, ver crear_factura_internacional): si es
    True, el stock de cada línea entra a un balde de stock_proveedores
    EXCLUSIVO de esta factura (factura_id = el id recién creado) en vez de
    mezclarse con el balde de siempre de ese proveedor -- ver
    _aplicar_entrada_stock/factura_id_bucket. Necesario para poder corregir
    después el costo exacto de ESA importación cuando se confirme el "tipo
    de cambio definitivo" (ver /api/facturas/<id>/tipo-cambio-definitivo),
    sin afectar el resto de las compras (nacionales) del mismo proveedor."""
    ts = now_iso()
    monto_neto = round(sum(cantidad * precio_unitario for _p, cantidad, precio_unitario in lineas_validadas), 2)
    monto_neto += round(sum(l["monto"] for l in (lineas_gasto_general or [])), 2)
    monto_neto = round(monto_neto, 2)
    monto_iva = 0 if exento_iva else round(monto_neto * 0.19, 2)
    monto_total = round(monto_neto + monto_iva, 2)
    cur = db.execute(
        """
        INSERT INTO facturas
            (numero, proveedor_id, fecha, creado_en, condicion_pago, porcentaje_contado, plazo_dias_credito,
             exento_iva, monto_neto, monto_iva, monto_total, tipo_documento)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (numero, proveedor_id, fecha, ts, condicion_pago, porcentaje_contado, plazo_dias_credito,
         1 if exento_iva else 0, monto_neto, monto_iva, monto_total, (tipo_documento or "factura").strip().lower()),
    )
    factura_id = cur.lastrowid
    factura_row = {"id": factura_id, "numero": numero, "proveedor_id": proveedor_id}

    for producto, cantidad, precio_unitario in lineas_validadas:
        cur_linea = db.execute(
            """
            INSERT INTO factura_lineas (factura_id, producto_id, cantidad, precio_unitario)
            VALUES (?, ?, ?, ?)
            """,
            (factura_id, producto["id"], cantidad, precio_unitario),
        )
        if aplicar_stock:
            # Ingresa el stock de inmediato, para no tener que repetir cada
            # producto a mano después: la factura ya trae cantidad y precio.
            _aplicar_entrada_stock(
                db, producto, cantidad, precio_unitario,
                f"Ingreso automático al registrar la factura '{numero}'",
                usuario, factura_row,
                factura_id_bucket=(factura_id if es_internacional else None),
            )
            db.execute(
                "UPDATE factura_lineas SET aplicada = 1, aplicada_en = ? WHERE id = ?",
                (now_iso(), cur_linea.lastrowid),
            )
            destino_ot = (lineas_ot_destino or {}).get(producto["id"])
            if destino_ot:
                # "Directo a una OT" (ver docstring arriba): el repuesto ya
                # quedó en stock justo arriba -- ahora se descarga esa misma
                # cantidad hacia la OT, en la misma operación, con el mismo
                # criterio de costeo que usa Operativo (costo promedio del
                # proveedor de esta factura, ver
                # registrar_salida_directa_a_orden).
                _item_ot, error_ot = registrar_salida_directa_a_orden(
                    db, destino_ot["orden_id"], producto["id"], cantidad,
                    categoria="Consumo interno", usuario=usuario,
                    motivo=f"Compra directa — factura '{numero}'", proveedor_id=proveedor_id,
                )
                if error_ot:
                    # No debería pasar (ya se revalidó en
                    # _aprobar_pendiente_recepcion justo antes de llamar
                    # acá), pero si pasa igual no queremos dejar la
                    # transacción a medias -- se propaga como excepción para
                    # que el caller (dentro de _iniciar_transaccion_stock)
                    # la revierta entera.
                    raise ValueError(error_ot[0])

    for l in (lineas_gasto_general or []):
        db.execute(
            """
            INSERT INTO factura_gastos_generales (factura_id, descripcion, monto, rubro_gav, creado_en)
            VALUES (?, ?, ?, ?, ?)
            """,
            (factura_id, l["descripcion"], l["monto"], l["rubro_gav"], ts),
        )

    _generar_cxp_de_factura(
        db, factura_id, proveedor_id, fecha, lineas_validadas,
        condicion_pago=condicion_pago, porcentaje_contado=porcentaje_contado, plazo_dias_credito=plazo_dias_credito,
        lineas_gasto_general=lineas_gasto_general, exento_iva=exento_iva,
    )

    return factura_id


# NOTA: "aplicar-masivo" (POST /api/facturas/aplicar-masivo, ingresar a
# stock después líneas que se habían registrado sin aplicar,
# aplicar_stock=False) se eliminó junto con el registro manual rápido — ya
# no existe ningún camino que deje una factura registrada con líneas sin
# aplicar (aprobar_factura_pendiente_recepcion siempre aplica lo
# efectivamente recibido en el momento de aprobar).


@app.route("/api/facturas/<int:factura_id>", methods=["DELETE"])
@requiere_rol("finanzas")
def eliminar_factura(factura_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    factura = db.execute("SELECT * FROM facturas WHERE id = ?", (factura_id,)).fetchone()
    if factura is None:
        return jsonify({"error": "Factura no encontrada"}), 404

    # cuentas_por_pagar.factura_id tiene ON DELETE CASCADE (y cxp_pagos en
    # cascada desde ahí) -- si esta factura ya generó una CxP y esa CxP ya
    # tiene abonos (plata real que salió de Caja Chica o se transfirió a un
    # proveedor), borrar la factura borraría en silencio el historial de esos
    # pagos, dejando el gasto de Caja Chica huérfano sin explicación. Se
    # bloquea sin excepción (ni siquiera con forzar=1, que solo estaba
    # pensado para la reversión de stock) -- hay que resolver la CxP primero.
    cxp_con_abonos = db.execute(
        """
        SELECT cxp.id, cxp.concepto, COALESCE(SUM(p.monto), 0) AS total_abonado
        FROM cuentas_por_pagar cxp
        JOIN cxp_pagos p ON p.cxp_id = cxp.id
        WHERE cxp.factura_id = ?
        GROUP BY cxp.id
        HAVING total_abonado > 0
        """,
        (factura_id,),
    ).fetchall()
    if cxp_con_abonos:
        total = round(sum(r["total_abonado"] for r in cxp_con_abonos), 2)
        return jsonify({
            "error": f"Esta factura ya tiene ${round(total):,}".replace(",", ".")
                     + " en abonos pagados registrados en su Cuenta por Pagar. No se puede eliminar así "
                       "porque se perdería el rastro de esos pagos reales -- resuelve o anula esos abonos "
                       "primero desde Finanzas antes de eliminar la factura.",
        }), 409

    forzar = request.args.get("forzar") == "1"
    usuario = usuario_actual()  # siempre quien tiene la sesión iniciada, nunca lo que mande el cliente

    lineas_aplicadas = db.execute(
        "SELECT * FROM factura_lineas WHERE factura_id = ? AND aplicada = 1", (factura_id,)
    ).fetchall()

    if lineas_aplicadas and not forzar:
        # Detalle producto por producto de lo que se va a revertir, para que
        # la interfaz pueda mostrarlo ANTES de que la persona confirme (en
        # vez de solo avisar "se va a revertir stock" sin decir de qué ni
        # cuánto).
        detalle_reversion = []
        for linea in lineas_aplicadas:
            producto = db.execute("SELECT * FROM productos WHERE id = ?", (linea["producto_id"],)).fetchone()
            detalle_reversion.append({
                "producto_id": linea["producto_id"],
                "producto_nombre": producto["nombre"] if producto else "(producto eliminado)",
                "codigo_interno": producto["codigo_interno"] if producto else "",
                "cantidad": linea["cantidad"],
            })
        return jsonify({
            "error": f"{len(lineas_aplicadas)} producto(s) de esta factura ya fueron ingresados a stock. "
                     f"Si la eliminas de todas formas, se intentará revertir automáticamente ese stock.",
            "requiere_forzar": True,
            "lineas_a_revertir": detalle_reversion,
        }), 409

    if forzar and lineas_aplicadas:
        # Antes de tocar nada: confirmar que TODAS las líneas aplicadas se
        # pueden revertir (que quede suficiente stock disponible). Si alguna
        # no se puede, se aborta completa para no dejar la reversión a medias.
        #
        # OJO: se valida tanto el stock TOTAL del producto como el balde
        # específico de stock_proveedores del proveedor de esta factura --
        # antes solo se chequeaba (y descontaba) el total agregado con un
        # UPDATE crudo a productos.stock_disponible, sin tocar
        # stock_proveedores para nada. Eso dejaba el desglose por proveedor
        # mintiendo tras revertir una factura (mostraba stock de un
        # proveedor que en la práctica ya no está, o de menos si ese
        # producto se compró después a otro proveedor y el total agregado
        # alcanzaba a "cubrir" la reversión desde el balde equivocado)
        # (hallazgo #7 del informe de revisión). _aplicar_salida_stock con
        # proveedor_manual=True (mismo mecanismo que usa el resto del
        # sistema para salidas con proveedor elegido a mano) descuenta del
        # balde correcto y mantiene stock_proveedores sincronizado con
        # productos.stock_disponible.
        pendientes_de_revertir = []
        for linea in lineas_aplicadas:
            producto = db.execute("SELECT * FROM productos WHERE id = ?", (linea["producto_id"],)).fetchone()
            if producto is None:
                continue  # el producto ya no existe: nada que revertir
            if producto["stock_disponible"] < linea["cantidad"]:
                return jsonify({
                    "error": f"No se puede eliminar: el producto '{producto['nombre']}' ya no tiene "
                             f"suficiente stock disponible para revertir esta factura (probablemente parte "
                             f"de ese stock ya se consumió). Ajusta el stock manualmente antes de eliminarla."
                }), 409
            bucket = _stockprov_bucket_get(db, producto["id"], factura["proveedor_id"])
            disponible_balde = bucket["cantidad"] if bucket else 0
            if disponible_balde < linea["cantidad"]:
                return jsonify({
                    "error": f"No se puede eliminar: el producto '{producto['nombre']}' ya no tiene "
                             f"suficiente stock del balde de este proveedor específico para revertir esta "
                             f"factura (probablemente parte de ese stock ya se consumió, o se mezcló con "
                             f"stock de otro proveedor). Ajusta el stock manualmente antes de eliminarla."
                }), 409
            pendientes_de_revertir.append((producto, linea))

        for producto, linea in pendientes_de_revertir:
            # Se relee 'producto' al toque (en vez de reusar la fila de la
            # validación de arriba) por si esta factura tiene más de una
            # línea del MISMO producto: la fila de la validación quedaría
            # con el stock de ANTES de revertir la línea anterior, y aplicar
            # la salida sobre esa foto vieja pisaría el descuento ya hecho
            # en vez de sumarse a él.
            producto_actual = db.execute("SELECT * FROM productos WHERE id = ?", (producto["id"],)).fetchone()
            _aplicar_salida_stock(
                db, producto_actual, linea["cantidad"],
                motivo=(
                    f"Reversión automática por eliminación de la factura '{factura['numero']}' (corrección de error, "
                    f"no cuenta como consumo real)"
                ),
                usuario=usuario,
                categoria=CATEGORIA_CORRECCION_INVENTARIO,
                proveedor_id=factura["proveedor_id"],
                proveedor_manual=True,
            )

    # Si esta factura fue creada a partir de una foto mandada por WhatsApp
    # (facturas_whatsapp_pendientes.factura_id apunta a ella una vez
    # aprobada), hay que soltar esa referencia ANTES de borrar la factura:
    # esa columna no tiene ON DELETE CASCADE/SET NULL (no queremos borrar el
    # historial de la revisión solo porque la factura se eliminó después), así
    # que sin este paso el DELETE de más abajo choca con la restricción de
    # llave foránea. El registro de la pendiente queda igual (mismo estado
    # 'aprobada', mismo audit trail), solo se limpia el vínculo roto.
    db.execute(
        "UPDATE facturas_whatsapp_pendientes SET factura_id = NULL WHERE factura_id = ?",
        (factura_id,),
    )
    # La factura "real" que se está borrando casi siempre nació de un
    # registro en facturas_finanzas_pendientes (estado 'aprobada', ver
    # aprobar_factura_pendiente_recepcion) — esa columna sí tiene
    # ON DELETE SET NULL, así que al borrar la factura el vínculo se
    # rompe solo, PERO el registro pendiente se queda con estado
    # 'aprobada' y sus propios datos (monto, líneas) copiados desde antes
    # de aprobar, por lo que seguía apareciendo en la pantalla de
    # Facturas como si nada hubiera pasado (factura "fantasma"). Se marca
    # 'eliminada' explícitamente para que la lista refleje que ya no
    # existe, sin perder el historial de quién la registró/aprobó.
    db.execute(
        "UPDATE facturas_finanzas_pendientes SET estado = 'eliminada' WHERE factura_id = ? AND estado = 'aprobada'",
        (factura_id,),
    )
    db.execute("DELETE FROM facturas WHERE id = ?", (factura_id,))
    db.commit()
    return jsonify({"ok": True, "revertido": bool(lineas_aplicadas)})


# ---------------------------------------------------------------------------
# API: Carga masiva de facturas vía Excel
# ---------------------------------------------------------------------------
#
# Flujo en dos pasos para evitar cargar datos con errores sin que el usuario
# los vea antes: primero se sube el Excel y se valida todo (sin tocar la
# base de datos), devolviendo una previsualización agrupada por factura con
# los errores que tenga cada una. Recién cuando el usuario confirma esa
# previsualización se registran las facturas válidas (las que tengan algún
# error quedan afuera, no bloquean a las demás).
#
# La previsualización se guarda un rato en memoria (CARGA_MASIVA_CACHE) para
# que "confirmar" no tenga que volver a mandar el Excel completo: solo manda
# el token que le devolvió la previsualización.

CARGA_MASIVA_CACHE = {}
CARGA_MASIVA_TTL_SEGUNDOS = 30 * 60
CARGA_MASIVA_ENCABEZADOS = [
    "N° Factura", "Fecha (AAAA-MM-DD)", "Proveedor", "RUT Proveedor",
    "ID Producto", "Modelo", "Detalle", "Cantidad", "Precio Unitario",
]


def _carga_masiva_limpiar_cache():
    ahora = time.time()
    vencidos = [t for t, v in CARGA_MASIVA_CACHE.items() if ahora - v["creado_en"] > CARGA_MASIVA_TTL_SEGUNDOS]
    for t in vencidos:
        CARGA_MASIVA_CACHE.pop(t, None)


# ---------------------------------------------------------------------------
# Escaneo de facturas por foto (IA): extrae número, fecha, proveedor y
# líneas de una foto, y las deja en la MISMA previsualización con
# corrección inline que usa la carga masiva por Excel (ver más abajo) — así
# se reutiliza toda esa UI ya construida y probada, en vez de duplicarla.
# ---------------------------------------------------------------------------

def _llamar_claude_vision_factura(imagen_bytes, mime_type):
    """Manda la foto de una factura a la API de Claude (Anthropic) y pide
    que devuelva los datos estructurados vía tool use (mucho más confiable
    que pedirle texto libre y tratar de parsearlo). Devuelve un dict con
    numero_factura/fecha/proveedor_nombre/proveedor_rut/lineas, o lanza
    RuntimeError con un mensaje explicable para mostrarle al usuario."""
    if requests is None:
        raise RuntimeError(
            "Falta instalar la librería 'requests' en el servidor para poder usar el escaneo de fotos."
        )
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "El escaneo de facturas por foto no está configurado todavía: falta la variable de entorno "
            "ANTHROPIC_API_KEY en el servidor. Mientras tanto puedes seguir registrando la factura a mano."
        )

    imagen_b64 = base64.b64encode(imagen_bytes).decode("ascii")

    herramienta = {
        "name": "extraer_factura",
        "description": "Registra los datos leídos de la foto de una factura de compra de un taller mecánico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_factura": {"type": "string", "description": "Número o folio de la factura, tal como aparece impreso. Vacío si no se alcanza a leer."},
                "fecha": {"type": "string", "description": "Fecha de la factura en formato AAAA-MM-DD. Vacío si no se ve con claridad."},
                "proveedor_nombre": {"type": "string", "description": "Nombre o razón social del proveedor/emisor de la factura. Vacío si no se ve."},
                "proveedor_rut": {"type": "string", "description": "RUT del proveedor si aparece impreso. Vacío si no."},
                "lineas": {
                    "type": "array",
                    "description": "Cada producto/ítem que aparece en el detalle de la factura.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "modelo": {
                                "type": "string",
                                "description": (
                                    "El código de MODELO/FAMILIA de transmisión o caja de cambios al que "
                                    "corresponde este repuesto -- ej. 'JF010', 'U140', '0AM', 'A6LF', '0BH'. "
                                    "En este taller, CADA repuesto pertenece a un modelo de transmisión "
                                    "específico, y ese código casi siempre viene como PRIMERA palabra de la "
                                    "descripción del ítem en la factura (ej. en 'JF011 PULLEY SET', el modelo "
                                    "es 'JF011'). Es un campo distinto de 'codigo' (que es el N° de parte/SKU "
                                    "del fabricante, no el modelo de transmisión) -- una misma factura puede "
                                    "traer ambos. CRÍTICO: transcribe el modelo EXACTAMENTE como está impreso, "
                                    "letra por letra y dígito por dígito -- NUNCA lo 'corrijas' o redondees a "
                                    "un modelo parecido que te suene más común o más conocido (ej. si dice "
                                    "'JF011', jamás pongas 'JF010' aunque JF010 te parezca más habitual -- son "
                                    "modelos distintos y confundirlos hace que el repuesto quede mal "
                                    "identificado en el inventario). Vacío si esa línea no trae un modelo de "
                                    "transmisión identificable."
                                ),
                            },
                            "codigo": {
                                "type": "string",
                                "description": (
                                    "Código o N° de parte (part number) del FABRICANTE del repuesto, tal como "
                                    "aparece impreso en la factura -- suele ser un código largo, tipo SKU "
                                    "(ej. '33710-NKC-7-FX', 'LUK0920006990', '073740'). Es DISTINTO del "
                                    "'modelo' de transmisión (ver ese campo) -- este es el código propio del "
                                    "fabricante de la pieza, no el modelo del vehículo/transmisión. Vacío si "
                                    "esa línea no trae ningún código de fabricante."
                                ),
                            },
                            "descripcion": {
                                "type": "string",
                                "description": (
                                    "Descripción del producto tal como aparece impresa en la factura, "
                                    "COMPLETA -- si el modelo de transmisión o el código vienen pegados al "
                                    "nombre (ej. 'JF011 PULLEY SET'), inclúyelos también acá tal cual, aunque "
                                    "ya los hayas puesto por separado en los campos 'modelo'/'codigo'. Nunca "
                                    "resumas ni dejes solo una parte del texto (ej. no reduzcas 'JF011 PULLEY "
                                    "SET' a solamente 'PULLEY SET')."
                                ),
                            },
                            "cantidad": {"type": "number"},
                            "precio_unitario": {"type": "number", "description": "Precio unitario neto (sin IVA) si se puede distinguir; si no, el precio de la línea tal cual aparece."},
                        },
                        "required": ["descripcion", "cantidad", "precio_unitario"],
                    },
                },
            },
            "required": ["numero_factura", "lineas"],
        },
    }

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2048,
        "tools": [herramienta],
        "tool_choice": {"type": "tool", "name": "extraer_factura"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": imagen_b64}},
                {
                    "type": "text",
                    "text": (
                        "Esta es una foto de una factura de compra de un taller mecánico especializado en "
                        "transmisiones/cajas de cambio. Extrae los datos usando la herramienta extraer_factura. "
                        "Presta ESPECIAL atención al campo 'modelo' (el modelo de transmisión, ej. JF010, U140, "
                        "0AM, A6LF, 0BH) de cada línea -- es el dato MÁS importante para identificar el "
                        "producto correcto en el inventario del taller, y hay que transcribirlo EXACTAMENTE "
                        "como está impreso, sin redondearlo a un modelo parecido que te resulte más familiar "
                        "(dos modelos que se ven casi iguales, ej. JF010 y JF011, son productos completamente "
                        "distintos para este taller). También presta atención al campo 'codigo' (N° de parte "
                        "del fabricante), que es un dato distinto del modelo. Si algún dato no se alcanza a "
                        "leer con confianza (letra borrosa, foto cortada, etc.), déjalo vacío en vez de "
                        "inventarlo o adivinarlo — alguien va a revisar y corregir a mano antes de guardar."
                    ),
                },
            ],
        }],
    }

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo contactar al servicio de IA: {e}")

    if resp.status_code >= 400:
        detalle = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(f"El servicio de IA respondió con error ({resp.status_code}): {detalle}")

    data = resp.json()
    for bloque in data.get("content", []):
        if bloque.get("type") == "tool_use" and bloque.get("name") == "extraer_factura":
            return bloque.get("input") or {}

    raise RuntimeError("La IA no devolvió los datos en el formato esperado. Intenta con una foto más nítida.")


# ---------------------------------------------------------------------------
# Clasificación de fotos que llegan por el agente de WhatsApp: antes de
# tratar una foto como factura de compra (ver _llamar_claude_vision_factura
# / _procesar_foto_factura_whatsapp), primero hay que saber DE QUÉ es la
# foto — un mecánico también puede mandar una foto del vehículo (ej. para
# avisar "listo para probar" mostrando la patente) en vez de una factura.
# Se hace en una llamada de IA aparte, más liviana, en vez de meter la
# clasificación dentro de _llamar_claude_vision_factura, para no tocar esa
# función (la usa también /api/facturas/escanear-foto, que siempre asume
# que la foto es una factura porque el usuario la subió desde ahí a
# propósito).
# ---------------------------------------------------------------------------

def _llamar_claude_vision_clasificar_foto_whatsapp(imagen_bytes, mime_type):
    """Le pregunta a Claude (vision) de qué es una foto mandada por
    WhatsApp: 'factura' (factura de compra), 'vehiculo' (foto de un auto/
    moto, típicamente para mostrar la patente) u 'otro' (no calza con
    ninguna de las dos, ej. una foto borrosa o de otra cosa). Si es
    'vehiculo', también intenta leer la patente. Devuelve un dict
    {"tipo": ..., "patente": ...} ('patente' vacía si no se detectó tipo
    'vehiculo' o no se alcanzó a leer con confianza), o lanza RuntimeError
    con un mensaje explicable si la IA no está disponible/configurada."""
    if requests is None:
        raise RuntimeError("Falta instalar la librería 'requests' en el servidor para poder leer fotos.")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "El análisis de fotos por WhatsApp no está configurado todavía: falta la variable de entorno "
            "ANTHROPIC_API_KEY en el servidor."
        )

    imagen_b64 = base64.b64encode(imagen_bytes).decode("ascii")

    herramienta = {
        "name": "clasificar_foto_taller",
        "description": "Clasifica una foto mandada por WhatsApp a un taller mecánico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["factura", "vehiculo", "otro"],
                    "description": (
                        "'factura' si la foto muestra una factura, boleta o guía de despacho de compra de "
                        "repuestos/insumos. 'vehiculo' si la foto muestra un auto, camioneta o moto (entero o "
                        "en parte, ej. solo la patente o el motor). 'otro' si no calza con ninguna de las dos "
                        "(foto borrosa, de otra cosa, o no se entiende qué es)."
                    ),
                },
                "patente": {
                    "type": "string",
                    "description": (
                        "Solo si tipo es 'vehiculo': la patente/placa del vehículo tal como se lee en la foto, "
                        "sin espacios ni guiones (ej. 'AB1234' o 'ABCD12'). Vacía si no se alcanza a leer con "
                        "confianza (patente no visible, foto borrosa/cortada, ángulo malo)."
                    ),
                },
            },
            "required": ["tipo"],
        },
    }

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 256,
        "tools": [herramienta],
        "tool_choice": {"type": "tool", "name": "clasificar_foto_taller"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": imagen_b64}},
                {
                    "type": "text",
                    "text": (
                        "Un mecánico de un taller mandó esta foto por WhatsApp. Clasifícala usando la "
                        "herramienta clasificar_foto_taller. NO es una factura de compra a menos que se vea "
                        "claramente un documento con ese formato (encabezado, líneas de detalle, totales)."
                    ),
                },
            ],
        }],
    }

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo contactar al servicio de IA: {e}")

    if resp.status_code >= 400:
        detalle = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(f"El servicio de IA respondió con error ({resp.status_code}): {detalle}")

    data = resp.json()
    for bloque in data.get("content", []):
        if bloque.get("type") == "tool_use" and bloque.get("name") == "clasificar_foto_taller":
            resultado = bloque.get("input") or {}
            return {
                "tipo": resultado.get("tipo") or "otro",
                "patente": (resultado.get("patente") or "").strip().upper(),
            }

    raise RuntimeError("La IA no devolvió una respuesta en el formato esperado. Intenta con una foto más nítida.")


UMBRAL_RECALL_PALABRAS = 0.6


def _palabras(texto):
    """Separa un texto en el conjunto de sus palabras/números (letras y
    dígitos, sin puntuación) en mayúscula -- unidad básica del comparador
    por palabras de más abajo."""
    return set(re.findall(r"[A-Z0-9]+", (texto or "").upper()))


def _mejor_match_por_texto(texto_norm, candidatos, campo="nombre"):
    """Núcleo del comparador difuso: entre 'candidatos' (filas de
    productos), devuelve el que mejor calce contra 'texto_norm', o None si
    ninguno es lo bastante bueno. 'campo' es qué columna del producto
    comparar (mezclada con codigo_interno para no perder esa señal) --
    separado de _buscar_producto_por_texto para poder reusarlo tanto
    contra el catálogo completo como contra un subconjunto ya acotado por
    modelo de transmisión (ver más abajo).

    Compara por PALABRAS COMPLETAS, no por caracteres -- un comparador de
    caracteres (difflib.SequenceMatcher puro, como se usaba antes) le da
    demasiado parecido a textos cortos que no tienen nada que ver: en un
    caso real de este taller, "PULLEY SET" vs "OIL FILTER E" (productos
    completamente distintos) puntuaba 0.455 de 1.0 con ese método, por
    encima del umbral que había -- y "VALVE BODY" vs "BRAKE BAND" (0.5)
    también. Exigir que un porcentaje real de las PALABRAS leídas
    aparezcan tal cual en el candidato (UMBRAL_RECALL_PALABRAS) filtra
    esos falsos positivos de raíz, porque ninguna palabra de "pulley set"
    aparece en "oil filter e". El parecido de caracteres (difflib) se usa
    solo como desempate ENTRE candidatos que ya pasaron el filtro de
    palabras, nunca como criterio único. Si dos productos distintos
    quedan empatados en el primer lugar, se considera ambiguo y no se
    matchea ninguno (mejor preguntarle a la persona que adivinar)."""
    texto_tokens = _palabras(texto_norm)
    if not texto_tokens:
        return None

    candidatos_calificados = []
    for p in candidatos:
        candidato = f"{p[campo] or ''} {p['codigo_interno'] or ''}".strip().upper()
        candidato_tokens = _palabras(candidato)
        if not candidato_tokens:
            continue
        recall = len(texto_tokens & candidato_tokens) / len(texto_tokens)
        if recall < UMBRAL_RECALL_PALABRAS:
            continue
        parecido_caracteres = difflib.SequenceMatcher(None, texto_norm, candidato).ratio()
        candidatos_calificados.append((recall, parecido_caracteres, p))

    if not candidatos_calificados:
        return None

    candidatos_calificados.sort(key=lambda t: (t[0], t[1]), reverse=True)
    mejor = candidatos_calificados[0]
    if len(candidatos_calificados) > 1:
        segundo = candidatos_calificados[1]
        if (mejor[0], mejor[1]) == (segundo[0], segundo[1]) and mejor[2]["id"] != segundo[2]["id"]:
            # Dos productos distintos empatados en el primer lugar -- no
            # hay forma de saber cuál es el correcto, así que no se
            # adivina ninguno.
            return None
    return mejor[2]


def _buscar_producto_por_texto(db, texto, codigo=None, modelo=None):
    """Intenta encontrar, entre los productos existentes, el que mejor
    calza con una descripción de texto libre (típicamente sacada de una
    foto de factura escaneada por IA — ahí no hay ID ni modelo/detalle
    exactos como en la carga masiva por Excel, solo el nombre que puso el
    proveedor). Se intenta, en este orden, la señal más confiable primero:

    1. 'codigo' (el N° de parte/SKU del fabricante que la IA haya leído
       por separado): match EXACTO contra productos.codigo_interno (única
       en la BD, ver idx_codigo_interno_unico). Si calza, se devuelve ese
       producto de inmediato -- es la señal más fuerte posible.

    2. 'modelo' (el modelo de transmisión, ej. 'JF010', que la IA también
       lee por separado -- ver _llamar_claude_vision_factura): en este
       taller cada producto pertenece a un modelo de transmisión exacto
       (columna productos.categoria, ver nombre_automatico en core.py), y
       dos modelos que se ESCRIBEN parecido (ej. JF010 vs JF011) pueden
       ser productos totalmente distintos -- por eso acá NUNCA se hace
       fuzzy: si 'modelo' viene y NO calza EXACTO con ningún
       productos.categoria existente, la línea queda sin matchear de
       inmediato, sin caer al comparador de texto general de más abajo
       (antes SÍ cateaba: 'JF011 pulley set' terminaba emparejado con
       'JF010 - PULLEY SET' del catálogo solo porque el texto se parecía
       mucho, un error grave -- confundir el modelo de transmisión es
       peor que no matchear nada). Si 'modelo' SÍ calza exacto, se acota
       la búsqueda a solo los productos de ese modelo y se compara el
       texto contra el detalle (columna 'modelo' en la BD, la que el
       taller ve como "Detalle") de ESE subconjunto nada más -- comparar
       ya sin el modelo de transmisión de por medio (que ambos lados
       comparten y sólo infla el puntaje de parecido) es lo que permite
       distinguir, por ejemplo, "0BH clutch" de "0BH - TCU": aunque
       compartan el modelo 0BH, "clutch" y "TCU" no se parecen en nada
       como texto, así que correctamente no matchea.

    3. Si no hay 'modelo' (la IA no distinguió ninguno en esa línea), cae
       al comparador de texto simple contra TODO el catálogo, como
       siempre se hizo -- es la señal menos confiable, así que solo se
       usa cuando no hay nada mejor.

    Umbral conservador a propósito en cada paso de texto: es mejor dejar
    la línea sin matchear (y que la persona la busque a mano en el
    autocompletar, viendo siempre lo que se leyó -- ver 'texto_original'/
    'codigo_leido'/'modelo_leido' en las pantallas de revisión) que
    asignarle el producto equivocado sin que se note."""
    codigo_norm = (codigo or "").strip().upper()
    if codigo_norm:
        por_codigo = db.execute(
            "SELECT * FROM productos WHERE codigo_interno = ?", (codigo_norm,)
        ).fetchone()
        if por_codigo is not None:
            return por_codigo

    texto_norm = (texto or "").strip().upper()

    modelo_norm = (modelo or "").strip().upper()
    if modelo_norm:
        familia = db.execute(
            "SELECT * FROM productos WHERE upper(trim(categoria)) = ?", (modelo_norm,)
        ).fetchall()
        if not familia:
            # El modelo que leyó la IA no existe en el catálogo -- podría
            # ser un modelo nuevo que el taller nunca ha cargado, o un
            # dígito mal leído (JF011 vs JF010). En cualquiera de los dos
            # casos, adivinar es peligroso: se deja sin matchear a
            # propósito, NO se cae al comparador de texto general.
            return None
        if not texto_norm:
            return None
        return _mejor_match_por_texto(texto_norm, familia, campo="modelo")

    if not texto_norm:
        return None

    productos = db.execute("SELECT * FROM productos").fetchall()
    return _mejor_match_por_texto(texto_norm, productos, campo="nombre")


@app.route("/api/facturas/carga-masiva/plantilla", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def carga_masiva_plantilla():
    """Descarga un Excel de ejemplo con las columnas esperadas, más una hoja
    de referencia con los productos actuales (su ID y nombre) para que sea
    fácil llenar la planilla sin adivinar los códigos."""
    from openpyxl import Workbook

    db = get_db()
    wb = Workbook()

    ws = wb.active
    ws.title = "Facturas a cargar"
    ws.append(CARGA_MASIVA_ENCABEZADOS)
    _estilo_encabezado(ws)
    ws.append(["F-1001", "2026-07-01", "Repuestos Central", "76.123.456-7", 1, "", "", 4, 25000])
    ws.append(["F-1001", "2026-07-01", "Repuestos Central", "76.123.456-7", "", "FILTROS", "AIRE-01", 2, 18000])
    ws.append(["F-1002", "2026-07-02", "Baterías Sur", "", 3, "", "", 1, 90000])
    _autoancho(ws)

    ws_ref = wb.create_sheet("Productos (referencia)")
    ws_ref.append(["ID Producto", "Modelo", "Detalle", "Nombre", "Stock actual"])
    _estilo_encabezado(ws_ref)
    for p in db.execute(PRODUCTO_SELECT + " ORDER BY p.categoria, p.modelo").fetchall():
        d = row_to_dict(p)
        ws_ref.append([d["id"], d["categoria"], d["modelo"], d["nombre"], d["stock_disponible"]])
    _autoancho(ws_ref)

    ws_ayuda = wb.create_sheet("Instrucciones")
    ws_ayuda.append(["Cómo llenar esta planilla"])
    instrucciones = [
        "Una fila por cada producto de cada factura. Si una factura trae varios productos, repite el N° de Factura en varias filas.",
        "N° Factura: el número o folio de la factura. No puede repetir uno que ya esté registrado en el sistema.",
        "Fecha: formato AAAA-MM-DD (ej: 2026-07-01). Si se deja vacía, se guarda sin fecha.",
        "Proveedor: nombre del proveedor. Si no existe todavía en el sistema, se crea automáticamente.",
        "RUT Proveedor: opcional. Solo se usa si hay que crear el proveedor (si ya existe, se respeta el RUT que ya tiene guardado).",
        "ID Producto: el número de producto tal como aparece en el sistema (columna 'ID Producto' de la hoja Productos (referencia), sin el '#'). Es la forma más segura de identificar el producto.",
        "Modelo / Detalle: alternativa si no se tiene el ID Producto — deben coincidir exactamente con un producto existente (no distingue mayúsculas/minúsculas).",
        "Cantidad: número entero mayor a 0.",
        "Precio Unitario: número, puede tener decimales. No puede ser negativo.",
        "El stock se ingresa automáticamente al confirmar la carga, igual que al crear una factura manual.",
    ]
    for i, texto in enumerate(instrucciones, start=2):
        ws_ayuda.cell(row=i, column=1, value=f"• {texto}")
    ws_ayuda.column_dimensions["A"].width = 120

    return _excel_response(wb, "plantilla_carga_facturas.xlsx")


def _carga_masiva_leer_excel(archivo):
    """Lee el Excel subido y devuelve una lista de filas crudas (dict), sin
    validar todavía. Usa los nombres de columna del encabezado (no la
    posición), para tolerar que el usuario reordene columnas."""
    from openpyxl import load_workbook

    wb = load_workbook(archivo, data_only=True)
    ws = wb.worksheets[0]

    filas_iter = ws.iter_rows(values_only=True)
    try:
        encabezado = next(filas_iter)
    except StopIteration:
        return []

    def _norm(txt):
        return re.sub(r"\s+", " ", str(txt or "")).strip().lower()

    mapa_columnas = {
        "n° factura": "numero", "n numero factura": "numero", "numero factura": "numero", "factura": "numero",
        "fecha (aaaa-mm-dd)": "fecha", "fecha": "fecha",
        "proveedor": "proveedor",
        "rut proveedor": "rut_proveedor",
        "id producto": "producto_id",
        "cantidad": "cantidad",
        "precio unitario": "precio_unitario",
    }

    # Las columnas de "Categoría"/"Modelo" se renombraron a "Modelo"/"Detalle"
    # (mismos campos internos 'categoria'/'modelo' de siempre, solo cambió la
    # etiqueta). Para que las plantillas viejas ya descargadas (con columnas
    # "Categoría" y "Modelo") se sigan leyendo igual que antes, se detecta
    # cuál juego de encabezados trae el archivo: si aparece "Categoría" en el
    # encabezado, es una plantilla vieja y "Modelo" ahí significa el campo
    # 'modelo'; si no aparece, es una plantilla nueva y "Modelo" significa el
    # campo 'categoria' (con "Detalle" para el campo 'modelo').
    encabezado_norm = {_norm(col) for col in encabezado}
    es_plantilla_vieja = "categoría" in encabezado_norm or "categoria" in encabezado_norm
    if es_plantilla_vieja:
        mapa_columnas["categoría"] = "categoria"
        mapa_columnas["categoria"] = "categoria"
        mapa_columnas["modelo"] = "modelo"
    else:
        mapa_columnas["modelo"] = "categoria"
        mapa_columnas["detalle"] = "modelo"

    indices = {}
    for i, col in enumerate(encabezado):
        clave = mapa_columnas.get(_norm(col))
        if clave:
            indices[clave] = i

    filas = []
    for n_fila, fila in enumerate(filas_iter, start=2):
        if fila is None or all(v is None or str(v).strip() == "" for v in fila):
            continue  # fila vacía: se ignora en silencio
        def val(clave):
            idx = indices.get(clave)
            if idx is None or idx >= len(fila):
                return None
            v = fila[idx]
            return v.strip() if isinstance(v, str) else v
        filas.append({
            "fila": n_fila,
            "numero": val("numero"),
            "fecha": val("fecha"),
            "proveedor": val("proveedor"),
            "rut_proveedor": val("rut_proveedor"),
            "producto_id": val("producto_id"),
            "categoria": val("categoria"),
            "modelo": val("modelo"),
            "cantidad": val("cantidad"),
            "precio_unitario": val("precio_unitario"),
        })
    return filas


def _carga_masiva_resolver_producto(db, fila):
    """Devuelve (producto, error, texto_original). 'error' solo se llena
    cuando la fila ni siquiera trae con qué IDENTIFICAR un producto (ni ID
    ni modelo/detalle) o cuando lo que trae es basura (un ID no
    numérico) -- eso sigue bloqueando la factura, porque no hay ni un
    nombre que mostrarle a Bodega. Si en cambio el ID/modelo/detalle
    están bien formados pero no calzan con ningún producto del catálogo,
    NO es un error: se arma un texto_original descriptivo y la línea queda
    'producto nuevo, pendiente de crear' (ver _carga_masiva_revalidar_grupo
    y aprobar_factura_pendiente_recepcion) en vez de descartar la factura
    completa por un solo repuesto que todavía no existe."""
    pid = fila.get("producto_id")
    if pid is not None and str(pid).strip() != "":
        try:
            pid_int = int(float(pid))
        except (TypeError, ValueError):
            return None, f"Fila {fila['fila']}: 'ID Producto' inválido ({pid!r})", None
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (pid_int,)).fetchone()
        if not producto:
            return None, None, f"ID Producto {pid_int} (no existe todavía en el catálogo)"
        return producto, None, None

    categoria = (fila.get("categoria") or "").strip()
    modelo = (fila.get("modelo") or "").strip()
    if not categoria and not modelo:
        return None, f"Fila {fila['fila']}: falta 'ID Producto' o 'Modelo'/'Detalle' para identificar el producto", None
    producto = db.execute(
        "SELECT * FROM productos WHERE UPPER(categoria) = UPPER(?) AND UPPER(modelo) = UPPER(?)",
        (categoria, modelo),
    ).fetchone()
    if not producto:
        descripcion = " / ".join(p for p in (categoria, modelo) if p)
        return None, None, descripcion or None
    return producto, None, None


def _buscar_proveedor_existente(db, nombre, rut):
    """Punto ÚNICO para decidir si un proveedor 'nombre'/'rut' que viene de
    una fuente externa (Excel, foto escaneada, WhatsApp) ya existe en el
    sistema. El RUT es el identificador confiable — dos facturas del mismo
    proveedor pueden traer el nombre escrito distinto ("Repuestos ABC" vs
    "REPUESTOS ABC SPA", typos de la IA leyendo la foto, etc.) pero el RUT
    normalizado (ver formatear_rut) no cambia, así que se prueba primero.
    Si no hay RUT o no matchea, se cae a nombre exacto (case-insensitive)
    como antes. Se usa en TODOS los caminos que pueden crear un proveedor
    "al vuelo" (carga masiva, escaneo de foto, WhatsApp) para no duplicar
    proveedores — ver también crear_proveedor(), que aplica la misma regla
    cuando alguien lo crea a mano."""
    rut_norm = formatear_rut((rut or "").strip())
    if rut_norm:
        fila = db.execute("SELECT * FROM proveedores WHERE rut = ? AND rut != ''", (rut_norm,)).fetchone()
        if fila:
            return fila
    nombre = (nombre or "").strip()
    if nombre:
        fila = db.execute("SELECT * FROM proveedores WHERE UPPER(nombre) = UPPER(?)", (nombre,)).fetchone()
        if fila:
            return fila
    return None


def _carga_masiva_resolver_proveedor(db, nombre, rut, proveedores_nuevos_en_este_lote):
    nombre = (nombre or "").strip()
    if not nombre:
        return None, None, False
    existente = _buscar_proveedor_existente(db, nombre, rut)
    if existente:
        return existente["id"], existente["nombre"], False
    if nombre.upper() in proveedores_nuevos_en_este_lote:
        return proveedores_nuevos_en_este_lote[nombre.upper()], nombre, True
    return "NUEVO", nombre, True  # marcador: se crea recién al confirmar


@app.route("/api/facturas/carga-masiva/previsualizar", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def carga_masiva_previsualizar():
    _carga_masiva_limpiar_cache()
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Debes adjuntar un archivo Excel (.xlsx)"}), 400
    if not archivo.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "El archivo debe ser un Excel (.xlsx)"}), 400

    try:
        filas = _carga_masiva_leer_excel(archivo)
    except Exception:
        return jsonify({"error": "No se pudo leer el archivo. Verifica que sea un Excel válido y siga el formato de la plantilla."}), 400

    if not filas:
        return jsonify({"error": "El archivo no tiene filas con datos (o está vacío)."}), 400

    db = get_db()
    numeros_existentes = {r["numero"] for r in db.execute("SELECT numero FROM facturas").fetchall()}

    por_factura = {}
    orden_facturas = []
    proveedores_nuevos_vistos = {}

    for fila in filas:
        numero = str(fila.get("numero") or "").strip().upper()
        errores_fila = []

        if not numero:
            errores_fila.append(f"Fila {fila['fila']}: falta el N° de Factura")
            numero = f"(sin número — fila {fila['fila']})"

        if numero not in por_factura:
            por_factura[numero] = {"numero": numero, "fecha": fila.get("fecha") or "", "proveedor_nombre": None,
                                    "proveedor_id": None, "proveedor_nuevo": False, "lineas": [], "errores": []}
            orden_facturas.append(numero)
        grupo = por_factura[numero]

        if numero in numeros_existentes:
            grupo["errores"].append(f"Ya existe una factura registrada con el número '{numero}'")

        if fila.get("proveedor") and grupo["proveedor_nombre"] is None:
            prov_id, prov_nombre, es_nuevo = _carga_masiva_resolver_proveedor(
                db, fila.get("proveedor"), fila.get("rut_proveedor"), proveedores_nuevos_vistos
            )
            grupo["proveedor_nombre"] = prov_nombre
            grupo["proveedor_rut"] = (fila.get("rut_proveedor") or "").strip()
            grupo["proveedor_nuevo"] = es_nuevo
            grupo["proveedor_id"] = None if prov_id == "NUEVO" else prov_id
            if es_nuevo and prov_nombre:
                proveedores_nuevos_vistos[prov_nombre.upper()] = "NUEVO"

        producto, error_producto, texto_original_producto = _carga_masiva_resolver_producto(db, fila)
        if error_producto:
            errores_fila.append(error_producto)

        cantidad_ok, precio_ok = None, None
        try:
            cantidad_ok = int(fila.get("cantidad"))
            if cantidad_ok <= 0:
                errores_fila.append(f"Fila {fila['fila']}: la cantidad debe ser mayor a 0")
        except (TypeError, ValueError):
            errores_fila.append(f"Fila {fila['fila']}: 'Cantidad' inválida ({fila.get('cantidad')!r})")

        try:
            precio_ok = float(fila.get("precio_unitario"))
            if precio_ok < 0:
                errores_fila.append(f"Fila {fila['fila']}: el precio no puede ser negativo")
        except (TypeError, ValueError):
            errores_fila.append(f"Fila {fila['fila']}: 'Precio Unitario' inválido ({fila.get('precio_unitario')!r})")

        grupo["lineas"].append({
            "fila": fila["fila"],
            "producto_id": producto["id"] if producto else None,
            "producto_nombre": row_to_dict(producto)["nombre"] if producto else None,
            "cantidad": cantidad_ok,
            "precio_unitario": precio_ok,
            "errores": errores_fila,
            "texto_original": texto_original_producto,
        })
        if errores_fila:
            grupo["errores"].extend(errores_fila)

    # Un producto no puede aparecer dos veces en la misma factura.
    for grupo in por_factura.values():
        vistos = {}
        for linea in grupo["lineas"]:
            if linea["producto_id"] is None:
                continue
            if linea["producto_id"] in vistos:
                msg = f"El producto '{linea['producto_nombre']}' aparece más de una vez en la factura '{grupo['numero']}'"
                if msg not in grupo["errores"]:
                    grupo["errores"].append(msg)
            vistos[linea["producto_id"]] = True

    facturas_preview = [por_factura[n] for n in orden_facturas]
    for g in facturas_preview:
        g["valida"] = len(g["errores"]) == 0

    token = uuid.uuid4().hex
    CARGA_MASIVA_CACHE[token] = {"creado_en": time.time(), "usuario": usuario_actual(), "facturas": facturas_preview, "origen": "excel"}

    return jsonify({
        "token": token,
        "facturas": facturas_preview,
        "resumen": _carga_masiva_resumen(facturas_preview),
    })


def _carga_masiva_resumen(facturas_preview):
    return {
        "total_facturas": len(facturas_preview),
        "validas": sum(1 for g in facturas_preview if g["valida"]),
        "con_errores": sum(1 for g in facturas_preview if not g["valida"]),
    }


def _carga_masiva_revalidar_grupo(db, grupo, numeros_existentes):
    """Recalcula errores/valida de UN grupo (factura) de la previsualización,
    después de que el usuario corrigió algo a mano (producto/cantidad/precio
    de una línea). No vuelve a leer el Excel: solo re-chequea con los valores
    que hay ahora mismo en el grupo cacheado.

    Una línea sin producto_id NO bloquea la factura (antes sí, con "falta
    identificar el producto") -- se deja pasar como "producto nuevo,
    pendiente de que Bodega lo cree" (ver texto_original), igual que ya
    hacía WhatsApp. Bloquear la factura completa por un solo repuesto que
    todavía no existe en el catálogo obligaba a Finanzas a descartarla
    entera; ahora queda igual como pendiente de recepción, con esa línea
    sin resolver, y Bodega la completa antes de poder aprobarla (ver
    aprobar_factura_pendiente_recepcion)."""
    errores = []

    if grupo["numero"] in numeros_existentes:
        errores.append(f"Ya existe una factura registrada con el número '{grupo['numero']}'")

    conteo_productos = {}
    for linea in grupo["lineas"]:
        errores_linea = []

        if linea["producto_id"] is None:
            pass  # producto nuevo, pendiente de crear -- no bloquea (ver docstring)
        else:
            producto = db.execute("SELECT * FROM productos WHERE id = ?", (linea["producto_id"],)).fetchone()
            if not producto:
                errores_linea.append(f"Fila {linea['fila']}: no existe ningún producto con ID {linea['producto_id']}")
                linea["producto_nombre"] = None
            else:
                linea["producto_nombre"] = row_to_dict(producto)["nombre"]
                conteo_productos[linea["producto_id"]] = conteo_productos.get(linea["producto_id"], 0) + 1

        if linea["cantidad"] is None:
            errores_linea.append(f"Fila {linea['fila']}: falta la cantidad")
        elif linea["cantidad"] <= 0:
            errores_linea.append(f"Fila {linea['fila']}: la cantidad debe ser mayor a 0")

        if linea["precio_unitario"] is None:
            errores_linea.append(f"Fila {linea['fila']}: falta el precio unitario")
        elif linea["precio_unitario"] < 0:
            errores_linea.append(f"Fila {linea['fila']}: el precio no puede ser negativo")

        linea["errores"] = errores_linea
        errores.extend(errores_linea)

    for producto_id, veces in conteo_productos.items():
        if veces > 1:
            nombre = next(l["producto_nombre"] for l in grupo["lineas"] if l["producto_id"] == producto_id)
            errores.append(f"El producto '{nombre}' aparece más de una vez en la factura '{grupo['numero']}'")

    grupo["errores"] = errores
    grupo["valida"] = len(errores) == 0
    return grupo


@app.route("/api/facturas/carga-masiva/corregir-linea", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def carga_masiva_corregir_linea():
    """Aplica una corrección manual (producto/cantidad/precio) a UNA línea de
    la previsualización de carga masiva, sin tener que volver a subir el
    Excel completo — para errores puntuales (un producto que no calzó, una
    cantidad mal tipeada) no vale la pena rehacer todo el archivo."""
    _carga_masiva_limpiar_cache()
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token")
    numero_factura = (data.get("numero_factura") or "").strip().upper()
    fila = data.get("fila")

    entrada = CARGA_MASIVA_CACHE.get(token)
    if not entrada:
        return jsonify({"error": "La previsualización expiró o no es válida. Vuelve a subir el archivo."}), 400

    grupo = next((g for g in entrada["facturas"] if g["numero"] == numero_factura), None)
    if grupo is None:
        return jsonify({"error": "No se encontró esa factura en la previsualización"}), 404

    linea = next((l for l in grupo["lineas"] if l["fila"] == fila), None)
    if linea is None:
        return jsonify({"error": "No se encontró esa línea en la previsualización"}), 404

    db = get_db()

    if "producto_id" in data:
        producto_id = data.get("producto_id")
        try:
            linea["producto_id"] = int(producto_id) if producto_id not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"error": "Producto inválido"}), 400
    if "cantidad" in data:
        cantidad = data.get("cantidad")
        try:
            linea["cantidad"] = int(cantidad) if cantidad not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"error": "Cantidad inválida"}), 400
    if "precio_unitario" in data:
        precio = data.get("precio_unitario")
        try:
            linea["precio_unitario"] = float(precio) if precio not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"error": "Precio inválido"}), 400

    numeros_existentes = {r["numero"] for r in db.execute("SELECT numero FROM facturas").fetchall()}
    _carga_masiva_revalidar_grupo(db, grupo, numeros_existentes)

    return jsonify({
        "factura": grupo,
        "resumen": _carga_masiva_resumen(entrada["facturas"]),
    })


@app.route("/api/facturas/carga-masiva/corregir-proveedor", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def carga_masiva_corregir_proveedor():
    """Corrige el proveedor de UNA factura de la previsualización — a
    diferencia de las líneas (producto/cantidad/precio), el proveedor no se
    editaba inline todavía. Se necesita sobre todo para el escaneo por
    foto, donde la IA puede leer mal el nombre del proveedor y no hay forma
    de elegir el correcto de la lista antes de confirmar."""
    _carga_masiva_limpiar_cache()
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token")
    numero_factura = (data.get("numero_factura") or "").strip().upper()
    proveedor_id = data.get("proveedor_id")  # None/"" = sin proveedor

    entrada = CARGA_MASIVA_CACHE.get(token)
    if not entrada:
        return jsonify({"error": "La previsualización expiró o no es válida. Vuelve a subir el archivo o la foto."}), 400

    grupo = next((g for g in entrada["facturas"] if g["numero"] == numero_factura), None)
    if grupo is None:
        return jsonify({"error": "No se encontró esa factura en la previsualización"}), 404

    db = get_db()
    if proveedor_id in (None, ""):
        grupo["proveedor_id"] = None
        grupo["proveedor_nombre"] = None
        grupo["proveedor_nuevo"] = False
    else:
        try:
            proveedor_id = int(proveedor_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Proveedor inválido"}), 400
        proveedor = db.execute("SELECT * FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
        if not proveedor:
            return jsonify({"error": "El proveedor seleccionado no existe"}), 400
        grupo["proveedor_id"] = proveedor["id"]
        grupo["proveedor_nombre"] = proveedor["nombre"]
        grupo["proveedor_nuevo"] = False

    numeros_existentes = {r["numero"] for r in db.execute("SELECT numero FROM facturas").fetchall()}
    _carga_masiva_revalidar_grupo(db, grupo, numeros_existentes)

    return jsonify({
        "factura": grupo,
        "resumen": _carga_masiva_resumen(entrada["facturas"]),
    })


@app.route("/api/facturas/escanear-foto", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def escanear_foto_factura():
    """Sube una foto de una factura de compra y usa IA (Claude, con
    visión) para leer proveedor, fecha, número y líneas — para no tener que
    tipearla a mano. El resultado se muestra en la MISMA previsualización
    con corrección inline que usa la carga masiva por Excel, así que todo
    lo que se pueda corregir ahí (producto, cantidad, precio, proveedor) se
    corrige exactamente igual antes de confirmar."""
    _carga_masiva_limpiar_cache()
    archivo = request.files.get("foto")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Debes adjuntar una foto de la factura"}), 400

    extension = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
    mime_por_extension = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
    mime_type = mime_por_extension.get(extension)
    if not mime_type:
        return jsonify({
            "error": "Formato de foto no soportado. Usa JPG, PNG, WEBP o GIF. Si tu teléfono guarda las fotos "
                     "en HEIC, toma la foto directo desde este botón (con la cámara) en vez de elegirla de tu galería."
        }), 400

    imagen_bytes = archivo.read()
    if not imagen_bytes:
        return jsonify({"error": "La foto llegó vacía. Intenta subirla de nuevo."}), 400
    if len(imagen_bytes) > 10 * 1024 * 1024:
        return jsonify({"error": "La foto es muy pesada (máximo 10 MB). Intenta con menos resolución."}), 400

    try:
        extraido = _llamar_claude_vision_factura(imagen_bytes, mime_type)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    db = get_db()
    numero = (extraido.get("numero_factura") or "").strip().upper()
    if not numero:
        # Sin número no hay cómo identificar la factura ni evitar duplicados,
        # y esta previsualización (a diferencia de las líneas) todavía no
        # permite corregir el número a mano — así que en vez de inventar un
        # marcador y dejar la persona atrapada, se corta acá con un mensaje
        # claro: mejor completarla con "Nueva factura" usando lo que sí se
        # alcanzó a leer.
        return jsonify({
            "error": "No se pudo leer el número de factura en la foto. Prueba con una foto más nítida, "
                     "o usa \"+ Nueva factura\" y complétala a mano con los datos que sí se alcanzan a ver.",
        }), 422

    numeros_existentes = {r["numero"] for r in db.execute("SELECT numero FROM facturas").fetchall()}

    proveedores_nuevos_vistos = {}
    prov_id, prov_nombre, es_nuevo = _carga_masiva_resolver_proveedor(
        db, extraido.get("proveedor_nombre"), extraido.get("proveedor_rut"), proveedores_nuevos_vistos
    )

    grupo = {
        "numero": numero,
        "fecha": (extraido.get("fecha") or "").strip(),
        "proveedor_nombre": prov_nombre,
        "proveedor_rut": (extraido.get("proveedor_rut") or "").strip(),
        "proveedor_nuevo": es_nuevo,
        "proveedor_id": None if prov_id in (None, "NUEVO") else prov_id,
        "lineas": [],
        "errores": [],
    }

    for i, linea in enumerate(extraido.get("lineas") or [], start=1):
        descripcion = (linea.get("descripcion") or "").strip()
        codigo_leido = (linea.get("codigo") or "").strip()
        modelo_leido = (linea.get("modelo") or "").strip()
        producto = _buscar_producto_por_texto(db, descripcion, codigo_leido, modelo_leido)
        try:
            cantidad = int(linea.get("cantidad"))
        except (TypeError, ValueError):
            cantidad = None
        try:
            precio = float(linea.get("precio_unitario"))
        except (TypeError, ValueError):
            precio = None
        grupo["lineas"].append({
            "fila": i,
            "producto_id": producto["id"] if producto else None,
            "producto_nombre": row_to_dict(producto)["nombre"] if producto else None,
            "cantidad": cantidad,
            "precio_unitario": precio,
            "errores": [],
            # Texto (código y modelo) tal como lo leyó la IA en la foto —
            # se muestra SIEMPRE en la previsualización, matcheó o no, para
            # que la persona pueda comparar contra el producto asignado
            # antes de confirmar (un match automático puede estar mal).
            "texto_original": descripcion,
            "codigo_leido": codigo_leido,
            "modelo_leido": modelo_leido,
        })

    _carga_masiva_revalidar_grupo(db, grupo, numeros_existentes)

    token = uuid.uuid4().hex
    CARGA_MASIVA_CACHE[token] = {"creado_en": time.time(), "usuario": usuario_actual(), "facturas": [grupo], "origen": "foto"}

    return jsonify({
        "token": token,
        "facturas": [grupo],
        "resumen": _carga_masiva_resumen([grupo]),
        "origen": "foto",
    })


# ---------------------------------------------------------------------------
# Facturas recibidas como foto por WhatsApp: un mecánico/encargado le manda
# al agente la foto de una factura de compra, la IA la lee (misma función
# _llamar_claude_vision_factura de arriba) y queda guardada como pendiente
# de recepción — mismo camino unificado que Excel, foto escaneada desde el
# navegador y carga manual (ver _crear_pendiente_recepcion) — NUNCA se sube
# sola al inventario. Alguien con sesión iniciada la revisa en Finanzas →
# Cuentas por Pagar, corrige lo que haga falta (PUT
# /api/facturas/pendientes-recepcion/<id>, ver editar_factura_pendiente_recepcion)
# y recién ahí la aprueba o la rechaza. Antes de esta migración, WhatsApp
# tenía su propia tabla y flujo de aprobación separado
# (facturas_whatsapp_pendientes) — se dejó de escribir ahí; la tabla sigue
# existiendo solo por los registros históricos que ya tenía.
# ---------------------------------------------------------------------------

def _procesar_foto_factura_whatsapp(db, telefono, mecanico_nombre, imagen_bytes, mime_type):
    """Lee la foto con IA, resuelve el proveedor y matchea las líneas contra
    el catálogo, y deja todo guardado como pendiente de recepción (misma
    tabla facturas_finanzas_pendientes que usan Excel/foto/manual) con
    condición de pago 'credito' por defecto (Finanzas la puede corregir al
    revisarla). Devuelve un dict con el mensaje de confirmación para
    responderle al mecánico por WhatsApp y el id del pendiente creado.
    Lanza RuntimeError (con mensaje explicable) si la IA no está disponible
    o no pudo leer nada útil — el llamador se encarga de responder eso.

    A diferencia de Excel/foto escaneada desde el navegador (que descartan
    la factura completa si una línea no matchea), acá se guarda igual con
    'producto_id': None en las líneas que no se pudieron resolver — una
    foto de WhatsApp, sacada rápido, es más propensa a traer datos
    incompletos, y preferimos guardar el resto en vez de perder la factura
    entera (alguien la completa después con el PUT de edición antes de
    aprobarla; ver aprobar_factura_pendiente_recepcion, que exige
    producto_id en todas las líneas para aprobar)."""
    extraido = _llamar_claude_vision_factura(imagen_bytes, mime_type)

    numero = (extraido.get("numero_factura") or "").strip().upper()
    proveedores_nuevos_vistos = {}
    prov_id, prov_nombre, es_nuevo = _carga_masiva_resolver_proveedor(
        db, extraido.get("proveedor_nombre"), extraido.get("proveedor_rut"), proveedores_nuevos_vistos
    )

    proveedor_id = None if prov_id in (None, "NUEVO") else prov_id
    if es_nuevo and prov_nombre:
        # Doble chequeo (mismo criterio de _buscar_proveedor_existente, RUT
        # primero) por si entre el resolver de arriba y acá alguien más creó
        # el proveedor — evita el duplicado que se generaba antes al confiar
        # solo en el resultado de _carga_masiva_resolver_proveedor.
        existente = _buscar_proveedor_existente(db, prov_nombre, extraido.get("proveedor_rut"))
        if existente:
            proveedor_id = existente["id"]
        else:
            cur = db.execute(
                "INSERT INTO proveedores (nombre, rut, creado_en) VALUES (?, ?, ?)",
                (prov_nombre, formatear_rut((extraido.get("proveedor_rut") or "").strip()), now_iso()),
            )
            proveedor_id = cur.lastrowid
            db.commit()

    lineas_validadas = []
    for linea in extraido.get("lineas") or []:
        descripcion = (linea.get("descripcion") or "").strip()
        codigo_leido = (linea.get("codigo") or "").strip()
        modelo_leido = (linea.get("modelo") or "").strip()
        producto = _buscar_producto_por_texto(db, descripcion, codigo_leido, modelo_leido)
        try:
            cantidad = int(linea.get("cantidad"))
        except (TypeError, ValueError):
            cantidad = 0
        try:
            precio = float(linea.get("precio_unitario"))
        except (TypeError, ValueError):
            precio = 0.0
        lineas_validadas.append({
            "producto_id": producto["id"] if producto else None,
            "producto_nombre": row_to_dict(producto)["nombre"] if producto else None,
            "cantidad": cantidad,
            "precio_unitario": precio,
            # Texto (código y modelo) tal como lo leyó la IA -- se guarda
            # SIEMPRE, matcheó o no, para que quien revise el pendiente vea
            # con qué se comparó antes de confiar en el producto asignado
            # (ver pfd-lineas en finanzas.js / afp-tbody-lineas en
            # alerta_facturas_pendientes.js).
            "texto_original": descripcion,
            "codigo_leido": codigo_leido,
            "modelo_leido": modelo_leido,
        })

    if not lineas_validadas:
        raise RuntimeError("no logré leer ninguna línea de producto en la foto")

    nombre_archivo = f"{uuid.uuid4().hex}.jpg" if mime_type == "image/jpeg" else f"{uuid.uuid4().hex}.{mime_type.split('/')[-1]}"
    with open(os.path.join(FACTURAS_WHATSAPP_DIR, nombre_archivo), "wb") as f:
        f.write(imagen_bytes)

    nueva, error = _crear_pendiente_recepcion(
        db, numero, proveedor_id, (extraido.get("fecha") or "").strip(), lineas_validadas,
        condicion_pago="credito", porcentaje_contado=0, plazo_dias_credito=CXP_DIAS_VENCIMIENTO_DEFECTO,
        creado_por=f"WhatsApp: {mecanico_nombre}", origen="whatsapp", foto_archivo=nombre_archivo,
        exigir_numero=False,
    )
    if error:
        # Con exigir_numero=False esto solo puede pasar por número duplicado
        # (factura ya existente o ya pendiente con el mismo folio) — se lo
        # avisamos al mecánico en vez de perder la foto en un error genérico.
        raise RuntimeError(error)

    pendiente_id = nueva["id"]
    sin_resolver = sum(1 for l in lineas_validadas if l["producto_id"] is None)

    partes = ["Recibí la foto de una factura"]
    if numero:
        partes.append(f"folio {numero}")
    if prov_nombre:
        partes.append(f"de {prov_nombre}")
    resumen = " ".join(partes) + f", con {len(lineas_validadas)} línea(s) detectada(s)."
    if not numero:
        resumen += " No alcancé a leer el número de folio — hay que completarlo a mano al revisarla."
    if sin_resolver:
        resumen += f" {sin_resolver} línea(s) no matchearon con ningún producto del inventario."
    mensaje = (
        f"{resumen} Quedó guardada como pendiente de recepción #{pendiente_id} en Finanzas → Cuentas por Pagar — "
        f"alguien del taller tiene que revisarla y aprobarla ahí antes de que se sume al inventario."
    )
    return {"mensaje": mensaje, "pendiente_id": pendiente_id}


# ---------------------------------------------------------------------------
# Facturas pendientes de recepción — a pedido del taller: hay facturas que
# se pagan al contado, otras a crédito (30/60/90 días), y otras mixtas
# (ej. 50% contado / 50% a crédito). Finanzas registra la factura con esas
# condiciones de pago apenas se pacta con el proveedor, ANTES de que la
# mercadería llegue físicamente — a diferencia de crear_factura(), acá NO
# se aplica el stock ni se genera la CxP todavía. Alguien tiene que
# contrastar esta factura contra lo que llegó de verdad al taller y
# aprobarla o rechazarla; solo al aprobar se crea la factura real
# (_registrar_factura, con las condiciones de pago ya definidas) y recién
# ahí se aplica el stock y se generan las Cuentas por Pagar. No hay un rol
# "Bodega" separado en este sistema — cualquier sesión iniciada puede
# aprobar o rechazar, igual que ya pasa con las facturas de WhatsApp — pero
# para que esto no quede escondido como pasaba con "Pendientes de
# WhatsApp" (solo se veía si entrabas a Facturas), estas quedan avisadas
# con un aviso global bloqueante en cualquier pantalla (ver
# alerta_facturas_pendientes.js).
#
# IMPORTANTE (corregido a pedido del taller — antes se podía "ajustar" la
# cantidad recibida por línea al momento de aprobar, para casos en que
# llegó menos de lo pactado): quien APRUEBA una recepción nunca modifica
# los datos de la factura. Si algo no calza contra lo que llegó
# físicamente (cantidad, precio, lo que sea), el conducto es SIEMPRE
# rechazarla con una nota explicando el motivo (ver
# rechazar_factura_pendiente_recepcion) para que quien la subió la
# corrija (PUT .../pendientes-recepcion/<id>, editar_factura_pendiente_recepcion)
# y la reingrese — nunca que la misma persona que revisa la reescriba de
# paso al aprobarla. Aprobar aplica la factura EXACTAMENTE como está
# cargada, sin excepción.
# ---------------------------------------------------------------------------

CONDICIONES_PAGO_VALIDAS = {"contado", "credito", "mixto"}

# "Boleta de compra" como alternativa a "factura" (ver Migración 35): una
# boleta siempre es al contado -- se fuerza en _crear_pendiente_recepcion,
# sin importar lo que mande quien llama.
TIPOS_DOCUMENTO_VALIDOS = {"factura", "boleta"}


def _factura_pendiente_recepcion_dict(db, row):
    d = dict(row)
    d["lineas"] = json.loads(d.pop("lineas_json") or "[]")
    # rubro_gav_label para que cualquier pantalla que muestre estas líneas
    # (revisión de Bodega en Finanzas, o el aviso flotante global -- ver
    # alerta_facturas_pendientes.js, que a propósito no comparte utilidades
    # con finanzas.js) pueda mostrar la etiqueta linda ("Combustible") sin
    # tener que duplicar el mapeo de rubros en cada script.
    for l in d["lineas"]:
        if l.get("es_gasto_general"):
            l["rubro_gav_label"] = RUBROS_GAV_QUE_SUMAN_GAV.get(l.get("rubro_gav"), l.get("rubro_gav"))
    recibidas = d.pop("lineas_recibidas_json", None)
    d["lineas_recibidas"] = json.loads(recibidas) if recibidas else None
    d["historial"] = json.loads(d.pop("historial_json", None) or "[]")
    internacional = d.pop("internacional_json", None)
    d["internacional"] = json.loads(internacional) if internacional else None
    # Las líneas de gasto general (ver es_gasto_general en
    # _validar_lineas_pendiente) no tienen cantidad/precio_unitario, van
    # con un 'monto' único -- el neto de la factura suma ambos tipos (ver
    # Migración 33: el precio de cada línea es NETO, y de ahí se calculan
    # IVA y total -- mismo criterio con el que luego se genera la CxP en
    # _generar_cxp_de_factura, para que lo que se muestra acá antes de
    # aprobar calce con lo que se termina debiendo al proveedor).
    d["monto_neto"] = round(sum(
        l["monto"] if l.get("es_gasto_general") else l["cantidad"] * l["precio_unitario"]
        for l in d["lineas"]
    ), 2)
    d["monto_iva"] = 0 if d.get("exento_iva") else round(d["monto_neto"] * 0.19, 2)
    d["monto_total"] = round(d["monto_neto"] + d["monto_iva"], 2)
    if d["proveedor_id"]:
        prov = db.execute("SELECT nombre FROM proveedores WHERE id = ?", (d["proveedor_id"],)).fetchone()
        d["proveedor_nombre"] = prov["nombre"] if prov else ""
    else:
        d["proveedor_nombre"] = ""
    return d


def _registrar_historial_pendiente(db, pendiente_id, evento, nota=None):
    """Agrega un evento al historial de una factura pendiente de recepción
    (historial_json) -- a diferencia de 'estado' (que solo refleja la
    situación ACTUAL) esto es un registro que nunca se sobrescribe ni se
    borra: permite ver, en una misma factura, que fue rechazada una vez,
    corregida, y aprobada después, sin perder ese rastro (a pedido del
    taller). No hace commit -- queda a cargo de quien llama, que ya tiene
    su propio commit al final de la misma operación."""
    fila = db.execute(
        "SELECT historial_json FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)
    ).fetchone()
    historial = json.loads((fila["historial_json"] if fila else None) or "[]")
    entrada = {"evento": evento, "usuario": usuario_actual(), "fecha": now_iso()}
    if nota:
        entrada["nota"] = nota
    historial.append(entrada)
    db.execute(
        "UPDATE facturas_finanzas_pendientes SET historial_json = ? WHERE id = ?",
        (json.dumps(historial), pendiente_id),
    )


@app.route("/api/facturas/pendientes-recepcion", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def listar_facturas_pendientes_recepcion():
    db = get_db()
    estado = request.args.get("estado", "").strip()
    if estado:
        filas = db.execute(
            "SELECT * FROM facturas_finanzas_pendientes WHERE estado = ? ORDER BY creado_en DESC", (estado,)
        ).fetchall()
    else:
        filas = db.execute("SELECT * FROM facturas_finanzas_pendientes ORDER BY creado_en DESC").fetchall()
    return jsonify({"pendientes": [_factura_pendiente_recepcion_dict(db, f) for f in filas]})


@app.route("/api/facturas/pendientes-recepcion/contador", methods=["GET"])
@requiere_rol("bodega")
def contador_facturas_pendientes_recepcion():
    """Endpoint liviano para el aviso global flotante (se consulta seguido,
    desde cualquier pantalla) — solo el número, no los datos completos.
    Restringido a 'bodega' (o admin) a propósito: quien recibe físicamente
    la mercadería y aprueba la recepción es Bodega, así que el aviso
    flotante ('N facturas por revisar') es solo para ese perfil. Finanzas
    sigue viendo sus propias facturas registradas desde su propia pantalla
    en el módulo Finanzas (ver listar_facturas_pendientes_recepcion), eso
    no es "notificación", es su propio trabajo pendiente."""
    db = get_db()
    n = db.execute(
        "SELECT COUNT(*) FROM facturas_finanzas_pendientes WHERE estado = 'pendiente'"
    ).fetchone()[0]
    return jsonify({"pendientes": n})


@app.route("/api/facturas/pendientes-recepcion/rechazadas/contador", methods=["GET"])
@requiere_rol("finanzas")
def contador_facturas_rechazadas():
    """Aviso global flotante simétrico al de arriba, pero para Finanzas: a
    diferencia de 'cuántas tengo pendientes de aprobar' (que es simplemente
    su propio trabajo de todos los días, visible en su pantalla sin
    necesidad de avisos), que Bodega haya rechazado una factura SÍ es una
    excepción que amerita un aviso activo -- si nadie se entera, la factura
    queda trabada esperando una corrección que nunca llega. Deja de contar
    en cuanto se corrige y reingresa (vuelve a 'pendiente', ver
    editar_factura_pendiente_recepcion) — no hace falta un flag de 'vista'
    aparte para esto."""
    db = get_db()
    n = db.execute(
        "SELECT COUNT(*) FROM facturas_finanzas_pendientes WHERE estado = 'rechazada'"
    ).fetchone()[0]
    return jsonify({"rechazadas": n})


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def detalle_factura_pendiente_recepcion(pendiente_id):
    db = get_db()
    fila = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    return jsonify(_factura_pendiente_recepcion_dict(db, fila))


def _crear_pendiente_recepcion(db, numero, proveedor_id, fecha, lineas_validadas, condicion_pago,
                                 porcentaje_contado, plazo_dias_credito, creado_por, origen="manual",
                                 foto_archivo="", exigir_numero=True, exento_iva=False,
                                 tipo_documento="factura", internacional_json=None):
    """Inserta una fila en facturas_finanzas_pendientes — punto de entrada
    ÚNICO para las cuatro formas de registrar una factura de compra (manual,
    carga masiva por Excel, foto escaneada, foto de WhatsApp), sin importar
    el origen. Antes de esta migración cada camino aplicaba stock y
    generaba su CxP de inmediato (o, en el caso de WhatsApp, pasaba por su
    propia tabla/flujo de aprobación separado) — ahora TODAS pasan por acá
    y quedan 'pendiente' hasta que Bodega las apruebe (ver
    aprobar_factura_pendiente_recepcion), sin excepción.

    'exigir_numero'=False es solo para el origen 'whatsapp': una foto sacada
    rápido puede no traer el folio legible, y preferimos guardar igual el
    resto de los datos ya leídos (para que alguien lo complete a mano al
    revisarla, ver PUT .../pendientes-recepcion/<id>) antes que perder la
    factura completa por un dato faltante. Con numero vacío, tampoco tiene
    sentido chequear duplicados por número.

    Devuelve (fila_dict, None) si se creó, o (None, mensaje_de_error) si
    algo no es válido — nunca lanza, así lo puede llamar tanto un endpoint
    HTTP (que responde con el mensaje) como el pipeline del agente de
    WhatsApp (que lo manda de vuelta como texto).

    'internacional_json' (ver crear_factura_internacional) se guarda ACÁ,
    en el INSERT inicial, en vez de con un UPDATE posterior -- a propósito:
    cuando esta fila entra por la ruta de aplicación automática (ver
    _crear_pendiente_y_aplicar_si_posible con auto_aplicar=True, el camino
    normal para una factura internacional cuyas líneas ya calzan con el
    catálogo), _aprobar_pendiente_recepcion corre DENTRO de esa misma
    llamada y necesita saber ahí mismo si es internacional para decidir en
    qué balde de stock_proveedores entra el stock (ver
    _registrar_factura/es_internacional) -- si se guardara con un UPDATE
    después, como se hacía antes, ese chequeo llegaría siempre tarde."""
    numero = (numero or "").strip().upper()
    tipo_documento = (tipo_documento or "factura").strip().lower()
    if tipo_documento not in TIPOS_DOCUMENTO_VALIDOS:
        return None, f"Tipo de documento inválido. Usa uno de: {', '.join(TIPOS_DOCUMENTO_VALIDOS)}"
    etiqueta_doc = "boleta" if tipo_documento == "boleta" else "factura"
    if tipo_documento == "boleta":
        # Una boleta siempre es al contado -- se fuerza acá, en el único
        # punto de entrada, sin importar lo que haya mandado quien llama
        # (defensa en profundidad además de lo que ya valida el asistente
        # en el navegador, ver factura_nueva.js).
        condicion_pago = "contado"
        porcentaje_contado = 100
    if exigir_numero and not numero:
        return None, f"El número de {etiqueta_doc} es obligatorio"
    if not lineas_validadas:
        return None, f"Agrega al menos un producto a la {etiqueta_doc}"
    if condicion_pago not in CONDICIONES_PAGO_VALIDAS:
        return None, f"Condición de pago inválida. Usa una de: {', '.join(CONDICIONES_PAGO_VALIDAS)}"
    if condicion_pago == "mixto" and not (0 < porcentaje_contado < 100):
        return None, "Para una condición mixta, el porcentaje al contado debe estar entre 0 y 100 (sin incluirlos)"
    if plazo_dias_credito <= 0 and condicion_pago in ("credito", "mixto"):
        return None, "El plazo de crédito debe ser mayor a 0 días"

    # Lock ANTES de chequear duplicados por número, no después -- sin esto,
    # dos registros casi simultáneos con el mismo folio (ej. la misma
    # factura escaneada dos veces por apuro, o Excel + manual mandando el
    # mismo número casi a la vez) podían leer "no existe" los dos antes de
    # que cualquiera alcanzara a insertar, y terminar con dos facturas (o
    # dos pendientes) con el mismo folio -- folio duplicado, hallazgo #8/#9
    # del informe de revisión. Es un no-op si el caller ya venía con el
    # lock tomado (ver _iniciar_transaccion_stock en core.py), así que es
    # seguro para los cuatro caminos de entrada (manual/Excel/foto/WhatsApp)
    # que comparten esta función.
    _iniciar_transaccion_stock(db)
    if numero:
        existe = db.execute("SELECT id FROM facturas WHERE numero = ?", (numero,)).fetchone()
        if existe:
            return None, f"Ya existe un documento registrado con el número '{numero}'"
        ya_pendiente = db.execute(
            "SELECT id FROM facturas_finanzas_pendientes WHERE numero = ? AND estado = 'pendiente'", (numero,)
        ).fetchone()
        if ya_pendiente:
            return None, f"Ya hay un documento pendiente de recepción con el número '{numero}'"

    ts = now_iso()
    # El primer evento del historial usa 'creado_por' (el parámetro, no
    # usuario_actual()) porque este mismo punto de entrada también lo llama
    # el pipeline del agente de WhatsApp fuera de una sesión con login (ver
    # docstring más arriba) -- ahí 'creado_por' ya trae algo como "Agente
    # WhatsApp", que es más preciso que "Sistema".
    historial_inicial = json.dumps([{"evento": "creada", "usuario": creado_por, "fecha": ts}])
    cur = db.execute(
        """
        INSERT INTO facturas_finanzas_pendientes
            (numero, proveedor_id, fecha, lineas_json, condicion_pago, porcentaje_contado,
             plazo_dias_credito, estado, creado_por, creado_en, origen, foto_archivo, historial_json,
             exento_iva, tipo_documento, internacional_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pendiente', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (numero, proveedor_id, fecha, json.dumps(lineas_validadas), condicion_pago, porcentaje_contado,
         plazo_dias_credito, creado_por, ts, origen, foto_archivo, historial_inicial, 1 if exento_iva else 0,
         tipo_documento, internacional_json),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return nueva, None


def _crear_pendiente_y_aplicar_si_posible(db, numero, proveedor_id, fecha, lineas_validadas, condicion_pago,
                                            porcentaje_contado, plazo_dias_credito, creado_por, origen,
                                            foto_archivo="", exigir_numero=True, auto_aplicar=True,
                                            exento_iva=False, tipo_documento="factura", internacional_json=None):
    """Como _crear_pendiente_recepcion, pero además -- si auto_aplicar es
    True y la factura recién creada queda con TODAS sus líneas ya
    resueltas contra el catálogo -- la aplica de inmediato (stock + CxP)
    en la misma operación, sin dejarla esperando un segundo paso de
    aprobación aparte. Es el corazón de la nueva ruta de ingreso directo
    (Bodega ingresa la factura -- manual, Excel o foto -- y queda lista de
    una, sin tener que pasársela a nadie más para que la apruebe después;
    ver _aprobar_pendiente_recepcion, que hace la aplicación real).

    Si queda alguna línea sin producto asociado (un repuesto nuevo que
    todavía no existe en el catálogo), NO se puede aplicar todavía -- no
    hay con qué mover stock. En ese caso la factura queda 'pendiente'
    exactamente como se comportaba el sistema antes de esta ruta directa:
    visible en la cola de siempre (ahora también accesible para Bodega),
    hasta que alguien cree/vincule el producto que falta y la apruebe a
    mano con el botón "Aprobar" (que sigue existiendo justo para este
    caso, y para las facturas de WhatsApp -- ver auto_aplicar=False más
    abajo).

    auto_aplicar=False es para WhatsApp (ver _procesar_foto_factura_whatsapp):
    esa vía no tiene un paso de revisión en vivo como las otras tres (nadie
    mira/corrige la lectura de la foto antes de que esto se guarde), así
    que ahí se prefiere mantener SIEMPRE el paso de aprobación humana
    aparte, sin excepción, como red de seguridad mínima.

    Devuelve (fila_dict, error, se_aplico). 'se_aplico' es True solo si la
    factura quedó aplicada de inmediato -- así quien llama arma el mensaje
    correcto ("factura aplicada: stock y CxP listos" vs "factura pendiente
    de recepción, falta crear un producto")."""
    nueva, error = _crear_pendiente_recepcion(
        db, numero, proveedor_id, fecha, lineas_validadas, condicion_pago,
        porcentaje_contado, plazo_dias_credito, creado_por, origen=origen,
        foto_archivo=foto_archivo, exigir_numero=exigir_numero, exento_iva=exento_iva,
        tipo_documento=tipo_documento, internacional_json=internacional_json,
    )
    if error:
        return None, error, False
    if not auto_aplicar:
        return nueva, None, False

    factura_id, lineas_recibidas_registro, error_aplicar = _aprobar_pendiente_recepcion(db, nueva)
    if error_aplicar:
        # No se pudo aplicar de inmediato (típicamente: una línea con
        # producto nuevo sin crear todavía) -- no es un error real, queda
        # 'pendiente' tal cual se guardó arriba, a la espera de que se
        # complete y se apruebe a mano.
        return nueva, None, False

    ts = now_iso()
    db.execute(
        """
        UPDATE facturas_finanzas_pendientes
        SET estado = 'aprobada', factura_id = ?, lineas_recibidas_json = ?, revisado_por = ?, revisado_en = ?
        WHERE id = ?
        """,
        (factura_id, json.dumps(lineas_recibidas_registro), creado_por, ts, nueva["id"]),
    )
    _registrar_historial_pendiente(db, nueva["id"], "aplicada_directamente")
    db.commit()
    aplicada = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (nueva["id"],)).fetchone()
    return aplicada, None, True


def _validar_lineas_pendiente(db, lineas_in):
    """Valida y normaliza las líneas que manda el cliente para crear un
    pendiente de recepción manual — separado de _crear_pendiente_recepcion
    porque esa validación (producto existe, cantidad/precio válidos, sin
    productos repetidos) solo tiene sentido cuando el usuario las tipeó a
    mano; Excel/foto/WhatsApp arman sus 'lineas_validadas' con su propia
    lógica de matching contra el catálogo (ver _carga_masiva_revalidar_grupo)
    y pueden traer líneas sin producto_id todavía por resolver.

    Una línea manual también puede venir SIN producto_id si en cambio trae
    'producto_nuevo_nombre' no vacío -- es el caso de una factura que trae
    un repuesto que todavía no existe en el catálogo (ver hallazgo
    planteado por el taller: Finanzas no crea productos, pero necesita
    poder dejar la factura registrada igual). Esa línea queda con
    producto_id=None y 'texto_original' con lo que describió Finanzas,
    exactamente en el mismo formato que ya usan Excel/foto/WhatsApp para
    líneas sin resolver -- Bodega la completa después desde la pantalla de
    editar el pendiente (ver crear_y_vincular_producto_pendiente), y
    aprobar_factura_pendiente_recepcion sigue exigiendo que toda línea
    tenga un producto_id real antes de poder aprobar la factura.

    Una línea también puede venir marcada 'es_gasto_general': true -- ej.
    una herramienta o combustible que llegó en la misma factura que
    repuestos reales (una factura puede traer un filtro que sí va a
    inventario Y la herramienta para sacarlo, que no), a pedido del taller
    para no ensuciar el catálogo de productos con ítems que no son
    repuestos. Esas líneas no pasan por el matching de producto_id en
    absoluto: van con 'descripcion' (texto libre) y 'monto' (un solo
    monto total, sin desglosar cantidad/precio unitario) y un 'rubro_gav'
    de la misma clasificación fija que ya usan las boletas de Caja Chica.
    Nunca requieren que Bodega les cree un producto ni bloquean la
    aprobación de la factura."""
    lineas_validadas = []
    productos_vistos = set()
    for i, linea in enumerate(lineas_in, start=1):
        if linea.get("es_gasto_general"):
            descripcion = (linea.get("descripcion") or "").strip()
            if not descripcion:
                return None, f"Línea {i}: describe el gasto general (ej. 'Bencina para el generador')"
            try:
                monto = float(linea.get("monto"))
            except (TypeError, ValueError):
                return None, f"Línea {i}: el monto debe ser un número válido"
            if monto <= 0:
                return None, f"Línea {i}: el monto debe ser mayor a 0"
            try:
                rubro_gav = _rubro_gav_valido(linea.get("rubro_gav"))
            except ValueError as e:
                return None, f"Línea {i}: {e}"
            if rubro_gav not in RUBROS_GAV_QUE_SUMAN_GAV:
                return None, f"Línea {i}: elige un rubro para el gasto general"
            lineas_validadas.append({
                "es_gasto_general": True, "producto_id": None, "producto_nombre": None,
                "descripcion": descripcion, "monto": round(monto, 2), "rubro_gav": rubro_gav,
            })
            continue

        try:
            cantidad = int(linea.get("cantidad"))
            precio_unitario = float(linea.get("precio_unitario"))
        except (TypeError, ValueError):
            return None, f"Línea {i}: la cantidad y el precio deben ser válidos"
        if cantidad <= 0:
            return None, f"Línea {i}: la cantidad debe ser mayor a 0"
        if precio_unitario < 0:
            return None, f"Línea {i}: el precio no puede ser negativo"

        extra_destino, error = _validar_destino_ot_linea(db, linea, i)
        if error:
            return None, error

        producto_id_raw = linea.get("producto_id")
        producto_nuevo_nombre = (linea.get("producto_nuevo_nombre") or "").strip()

        if producto_id_raw in (None, "", 0, "0"):
            if not producto_nuevo_nombre:
                return None, (
                    f"Línea {i}: elige un producto del catálogo, o describe el producto nuevo "
                    f"para que Bodega lo cree antes de aprobar la factura"
                )
            lineas_validadas.append({
                "producto_id": None, "producto_nombre": None,
                "cantidad": cantidad, "precio_unitario": precio_unitario,
                "texto_original": producto_nuevo_nombre,
                **extra_destino,
            })
            continue

        try:
            producto_id = int(producto_id_raw)
        except (TypeError, ValueError):
            return None, f"Línea {i}: producto inválido"
        producto = db.execute("SELECT id, nombre FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if not producto:
            return None, f"Línea {i}: el producto seleccionado no existe"
        if producto_id in productos_vistos:
            return None, f"Línea {i}: ese producto ya está agregado en otra línea de esta misma factura"
        productos_vistos.add(producto_id)
        lineas_validadas.append({"producto_id": producto_id, "producto_nombre": producto["nombre"],
                                  "cantidad": cantidad, "precio_unitario": precio_unitario, **extra_destino})
    return lineas_validadas, None


def _validar_destino_ot_linea(db, linea, i):
    """Valida el destino de una línea de producto de una factura pendiente
    de recepción -- por defecto ('stock') no cambia nada del comportamiento
    de siempre, pero la nueva ventana de Facturas (ver /facturas/nueva)
    permite marcar una línea como 'directo a una orden de trabajo': el
    repuesto igual ingresa a stock (mismo costo promedio, mismo historial
    que cualquier compra) pero se descarga altiro hacia esa OT al aprobar
    la factura, en la misma operación -- pensado para un repuesto que se
    compra sabiendo de antemano que va a una OT puntual (ej. "ya sabemos
    que va a la OT 4521"), sin tener que ir después a Operativo a
    descargarlo a mano. Ver _registrar_factura (lineas_ot_destino) y
    registrar_salida_directa_a_orden (modulos/operativo.py) para la
    aplicación real.

    Devuelve (extra, error): 'extra' es un dict para mezclar en la línea
    validada -- {} si va a stock normal, o {'destino': 'ot', 'orden_id': N}
    si corresponde. La OT debe existir y seguir abierta; se vuelve a
    chequear esto mismo justo antes de aplicar la factura (ver
    _aprobar_pendiente_recepcion), por si se cerró entremedio."""
    destino = (linea.get("destino") or "stock").strip().lower()
    if destino not in ("stock", "ot"):
        return None, f"Línea {i}: destino inválido"
    if destino == "stock":
        return {}, None
    try:
        orden_id = int(linea.get("orden_id"))
    except (TypeError, ValueError):
        return None, f"Línea {i}: indica el número de OT a la que va directo este repuesto"
    orden = db.execute("SELECT estado FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return None, f"Línea {i}: no existe una orden de trabajo #{orden_id}"
    if orden["estado"] in ESTADOS_CERRADOS:
        return None, f"Línea {i}: la OT #{orden_id} está '{orden['estado']}' y no admite más ítems"
    return {"destino": "ot", "orden_id": orden_id}, None


def _calcular_prorrateo_internacional(lineas_in, tipo_cambio, shipping_usd, paypal_usd, dhl_clp):
    """Reproduce, línea por línea, el Excel de prorrateo de importaciones
    que ya usaba el taller ("GUIAS DE IMPORTACION...xlsx") para repartir el
    flete, PayPal y el agente de aduana (DHL) entre los productos de una
    factura internacional y sacar el costo final en pesos de cada uno --
    ver /api/facturas/internacional, que es el único que llama a esto.

    La lógica (confirmada con el taller, se replica tal cual el Excel, sin
    "corregirla" aunque a primera vista mezcle precio unitario con montos
    de línea -- así es como el taller la viene usando):

      1) El % de prorrateo de flete/PayPal de cada línea es su COSTO
         UNITARIO en dólares (no el valor extendido cantidad×precio) sobre
         la suma de los costos unitarios de TODAS las líneas. O sea, un
         repuesto caro pero de poca cantidad puede llevarse más prorrateo
         que uno barato pedido en volumen -- así calcula el Excel y así se
         pidió mantenerlo.
      2) Esa misma proporción se usa para repartir tanto el flete como
         PayPal (ambos en dólares) -- se suman al costo unitario para
         llegar a un "total dólares" por línea, que se convierte a pesos
         con el tipo de cambio de la factura.
      3) El agente de aduana / DHL (que en el Excel llega directo en
         PESOS, no en dólares) se prorratea en una SEGUNDA pasada aparte,
         esta vez según el peso en pesos de cada línea sobre el total en
         pesos de todas (ya con flete y PayPal sumados).
      4) El resultado de sumar ambos prorrateos es el costo total en pesos
         "de la línea"; dividido por la cantidad de esa línea da el costo
         final POR UNIDAD (columna S del Excel) -- ese es el número que
         termina como precio_unitario de la factura, en pesos, IVA no
         incluido, listo para entrar al mismo camino que cualquier otra
         factura de compra.

    OJO -- esto puede parecer un error a primera vista, y por eso se
    verificó dos veces con el taller antes de dejarlo así: el costo unitario
    en dólares (I) NUNCA se multiplica por la cantidad en ningún paso de la
    cadena (I→K→L→M→N→O→P→Q) -- se suma tal cual, como si la línea fuera de
    una sola unidad -- y recién al final se divide por la cantidad real para
    sacar el "costo final por unidad". Con una línea de varias unidades esto
    da un número bastante menor al costo de fábrica en dólares convertido a
    pesos (para 15 unidades a US$576 cada una, por ejemplo, el resultado
    queda por debajo del valor de UNA sola unidad al tipo de cambio). Se le
    mostró este caso concreto al taller (con esos mismos números) y
    confirmaron que así es como calculan hoy el "precio unitario real" que
    tipean a mano en el sistema -- así que se replica exactamente así,
    tal como venía del Excel, sin "corregir" la fórmula.

    Devuelve (desglose, error): 'desglose' es una lista alineada 1 a 1 con
    lineas_in, cada una con el detalle completo del cálculo (para poder
    mostrarlo después en "Ver factura", ver internacional_json) además de
    'costo_unitario_final_clp', que es lo que crear_factura_internacional
    usa como precio_unitario al armar las líneas para _validar_lineas_pendiente."""
    if tipo_cambio <= 0:
        return None, "El tipo de cambio debe ser mayor a 0"
    if shipping_usd < 0 or paypal_usd < 0 or dhl_clp < 0:
        return None, "Shipping, PayPal y DHL/agente de aduana no pueden ser negativos"

    previas = []
    total_usd_unitario = 0.0
    for i, linea in enumerate(lineas_in, start=1):
        if linea.get("es_gasto_general"):
            return None, f"Línea {i}: una factura internacional no admite líneas de gasto general"
        try:
            cantidad = int(linea.get("cantidad"))
            costo_unitario_usd = float(linea.get("costo_unitario_usd"))
        except (TypeError, ValueError):
            return None, f"Línea {i}: la cantidad y el costo unitario en dólares deben ser válidos"
        if cantidad <= 0:
            return None, f"Línea {i}: la cantidad debe ser mayor a 0"
        if costo_unitario_usd < 0:
            return None, f"Línea {i}: el costo unitario en dólares no puede ser negativo"
        previas.append({"cantidad": cantidad, "costo_unitario_usd": costo_unitario_usd})
        total_usd_unitario += costo_unitario_usd

    if not previas:
        return None, "Agrega al menos un producto a la factura internacional"
    if total_usd_unitario <= 0:
        return None, "Agrega costos unitarios en dólares mayores a 0 (el prorrateo se reparte según ese valor)"

    # Primera pasada: flete + PayPal, prorrateados por costo unitario USD,
    # convertidos a pesos con el tipo de cambio de la factura.
    total_clp_pre_dhl = 0.0
    for p in previas:
        pct_flete = p["costo_unitario_usd"] / total_usd_unitario
        shipping_prorrateado_usd = shipping_usd * pct_flete
        paypal_prorrateado_usd = paypal_usd * pct_flete
        total_usd_linea = p["costo_unitario_usd"] + shipping_prorrateado_usd + paypal_prorrateado_usd
        clp_pre_dhl_linea = total_usd_linea * tipo_cambio
        p["pct_flete"] = pct_flete
        p["shipping_prorrateado_usd"] = round(shipping_prorrateado_usd, 4)
        p["paypal_prorrateado_usd"] = round(paypal_prorrateado_usd, 4)
        p["total_usd_linea"] = round(total_usd_linea, 4)
        p["clp_pre_dhl_linea"] = clp_pre_dhl_linea
        total_clp_pre_dhl += clp_pre_dhl_linea

    if total_clp_pre_dhl <= 0:
        return None, "El total en pesos antes de DHL/agente de aduana quedó en 0 -- revisa los costos unitarios"

    # Segunda pasada: DHL/agente de aduana, prorrateado por el peso en
    # pesos de cada línea (ya con flete+PayPal sumados) sobre ese total.
    desglose = []
    for p in previas:
        pct_dhl = p["clp_pre_dhl_linea"] / total_clp_pre_dhl
        dhl_prorrateado_clp = dhl_clp * pct_dhl
        total_clp_linea = p["clp_pre_dhl_linea"] + dhl_prorrateado_clp
        costo_unitario_final_clp = round(total_clp_linea / p["cantidad"])
        desglose.append({
            "cantidad": p["cantidad"],
            "costo_unitario_usd": p["costo_unitario_usd"],
            "pct_flete": round(p["pct_flete"], 6),
            "shipping_prorrateado_usd": p["shipping_prorrateado_usd"],
            "paypal_prorrateado_usd": p["paypal_prorrateado_usd"],
            "total_usd_linea": p["total_usd_linea"],
            "clp_pre_dhl_linea": round(p["clp_pre_dhl_linea"], 2),
            "pct_dhl": round(pct_dhl, 6),
            "dhl_prorrateado_clp": round(dhl_prorrateado_clp, 2),
            "total_clp_linea": round(total_clp_linea, 2),
            "costo_unitario_final_clp": costo_unitario_final_clp,
        })
    return desglose, None


@app.route("/api/facturas/internacional", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def crear_factura_internacional():
    """Registra una factura de compra internacional: a diferencia de
    crear_factura_pendiente_recepcion (que recibe el precio unitario en
    pesos ya calculado), acá cada línea viene con su costo unitario en
    DÓLARES tal como aparece en la factura del proveedor, más los datos de
    la importación completa (tipo de cambio, flete y PayPal en dólares,
    DHL/agente de aduana en pesos) -- ver _calcular_prorrateo_internacional
    para la lógica exacta, que replica el Excel que usaba el taller para
    esto ("GUIAS DE IMPORTACION...xlsx").

    Una vez calculado el costo final en pesos de cada línea, el resto es
    EXACTAMENTE el mismo camino que cualquier otra factura de compra
    manual (_validar_lineas_pendiente + _crear_pendiente_y_aplicar_si_posible):
    misma cola de aprobación de Bodega si trae un producto nuevo, mismo
    stock y misma Cuenta por Pagar si todos los productos ya existen en el
    catálogo. El desglose del prorrateo se guarda aparte (internacional_json)
    solo para poder mostrarlo después en "Ver factura", nunca se usa para
    nada más."""
    data = request.get_json(force=True, silent=True) or {}
    numero = (data.get("numero") or "").strip().upper()
    proveedor_id = data.get("proveedor_id") or None
    fecha = (data.get("fecha") or "").strip()
    lineas_in = data.get("lineas") or []
    condicion_pago = (data.get("condicion_pago") or "credito").strip().lower()
    exento_iva = bool(data.get("exento_iva"))
    try:
        porcentaje_contado = float(data.get("porcentaje_contado") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El porcentaje al contado debe ser un número"}), 400
    try:
        plazo_dias_credito = int(data.get("plazo_dias_credito") or CXP_DIAS_VENCIMIENTO_DEFECTO)
    except (TypeError, ValueError):
        return jsonify({"error": "El plazo de crédito debe ser un número de días"}), 400
    try:
        tipo_cambio = float(data.get("tipo_cambio"))
    except (TypeError, ValueError):
        return jsonify({"error": "El tipo de cambio debe ser un número"}), 400
    try:
        shipping_usd = float(data.get("shipping_usd") or 0)
        paypal_usd = float(data.get("paypal_usd") or 0)
        dhl_clp = float(data.get("dhl_clp") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Shipping, PayPal y DHL/agente de aduana deben ser números"}), 400

    if not lineas_in:
        return jsonify({"error": "Agrega al menos un producto a la factura internacional"}), 400

    desglose, error = _calcular_prorrateo_internacional(lineas_in, tipo_cambio, shipping_usd, paypal_usd, dhl_clp)
    if error:
        return jsonify({"error": error}), 400

    db = get_db()
    if proveedor_id:
        try:
            proveedor_id = int(proveedor_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Proveedor inválido"}), 400
        if not db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone():
            return jsonify({"error": "El proveedor seleccionado no existe"}), 400

    # Arma las líneas en el mismo formato que espera _validar_lineas_pendiente
    # (producto_id o producto_nuevo_nombre + cantidad + precio_unitario),
    # con precio_unitario = el costo final en pesos que recién calculó
    # _calcular_prorrateo_internacional -- de acá en adelante es una factura
    # de compra común y corriente, sin ningún trato especial.
    lineas_para_validar = []
    for linea_in, calc in zip(lineas_in, desglose):
        nueva_linea = {"cantidad": calc["cantidad"], "precio_unitario": calc["costo_unitario_final_clp"]}
        if (linea_in.get("producto_nuevo_nombre") or "").strip():
            nueva_linea["producto_nuevo_nombre"] = linea_in["producto_nuevo_nombre"].strip()
        else:
            nueva_linea["producto_id"] = linea_in.get("producto_id")
        lineas_para_validar.append(nueva_linea)

    lineas_validadas, error = _validar_lineas_pendiente(db, lineas_para_validar)
    if error:
        return jsonify({"error": error}), 400

    # El desglose (tipo de cambio, flete/PayPal/DHL y el detalle por línea,
    # incluido cantidad/costo_unitario_usd de cada una -- la base que
    # /api/facturas/<id>/tipo-cambio-definitivo vuelve a usar más adelante
    # para recalcular con el dólar real) se arma ACÁ, antes de crear el
    # pendiente, para poder guardarlo en el INSERT inicial en vez de con un
    # UPDATE después (ver docstring de _crear_pendiente_recepcion sobre por
    # qué eso importa para la ruta de aplicación automática). "definitivo"
    # empieza en False: el dólar recién tipeado es siempre tentativo, tal
    # como pidió el taller -- se pasa a True solo al confirmar el tipo de
    # cambio definitivo.
    internacional_meta = {
        "tipo_cambio": tipo_cambio,
        "shipping_usd": shipping_usd,
        "paypal_usd": paypal_usd,
        "dhl_clp": dhl_clp,
        "lineas": desglose,
        "definitivo": False,
        "tipo_cambio_definitivo": None,
    }

    nueva, error, _se_aplico = _crear_pendiente_y_aplicar_si_posible(
        db, numero, proveedor_id, fecha, lineas_validadas, condicion_pago,
        porcentaje_contado, plazo_dias_credito, usuario_actual(), origen="manual",
        exento_iva=exento_iva, tipo_documento="factura",
        internacional_json=json.dumps(internacional_meta),
    )
    if error:
        return jsonify({"error": error}), 400

    return jsonify(_factura_pendiente_recepcion_dict(db, nueva)), 201


@app.route("/api/facturas/<int:factura_id>/tipo-cambio-definitivo", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def confirmar_tipo_cambio_definitivo(factura_id):
    """Confirma (o corrige) el "tipo de cambio definitivo" de una factura
    internacional YA APLICADA -- a pedido del taller: el dólar que se tipea
    al ingresar una factura internacional es siempre TENTATIVO (el real
    recién se sabe a fin de mes, cuando se paga la tarjeta en dólares), así
    que hace falta poder corregirlo después y que "la factura cambie sus
    precios en base a eso definitivamente". Se puede llamar más de una vez
    (ej. para corregir un error de tipeo) -- cada llamada vuelve a calcular
    todo desde cero con los mismos cantidad/costo_unitario_usd originales
    de cada línea (ver internacional_json), así que es seguro repetirla.

    Recalcula el prorrateo completo (_calcular_prorrateo_internacional) con
    el nuevo tipo de cambio -- shipping/PayPal/DHL no cambian, solo el
    dólar -- y propaga el costo nuevo de cada línea a TODO lo que depende
    de él:
      - factura_lineas.precio_unitario (y los totales de 'facturas').
      - El balde de stock_proveedores exclusivo de esta factura (ver
        Migración 38), si todavía le queda stock sin consumir -- se
        corrige directo (SET, no promedio: todo ese balde vino de esta
        misma factura al mismo costo de origen).
      - El movimiento de 'entrada' original de cada línea, para que quede
        auditable con el costo real.
      - TODOS los orden_items/garantia_items que consumieron ese balde,
        aunque la orden/garantía ya esté cerrada -- a pedido explícito del
        taller ("es fundamental que el costo sea el real... con eso se
        calcula el costo final del mes y de cada OT"), porque el Estado de
        Resultados lee el costo directamente desde ahí (ver
        modulos/finanzas.py), nunca desde el promedio general del producto.
      - La(s) Cuenta(s) por Pagar de la factura: se reemplazan por unas
        nuevas con el monto correcto (mismo patrón "anular y regenerar" que
        cambiar_condicion_pago_factura), bloqueado si ya tiene abonos
        registrados -- no hay forma de revertir un pago que ya "salió".

    'productos.costo_compra' (el Costo Promedio Ponderado GENERAL, que
    mezcla TODOS los proveedores/compras históricas) se ajusta también,
    pero solo de forma aproximada -- no hay manera de saber con exactitud
    cuánto de ese número mezclado vino de esta factura puntual (ver
    comentario largo sobre stock_proveedores en core.py). No es grave: ese
    número ya es "último recurso" en todo el sistema, nunca la fuente real
    de un costo (ver agregar_item_orden)."""
    db = get_db()
    _iniciar_transaccion_stock(db)
    factura = db.execute("SELECT * FROM facturas WHERE id = ?", (factura_id,)).fetchone()
    if factura is None:
        return jsonify({"error": "Factura no encontrada"}), 404

    pendiente = db.execute(
        "SELECT * FROM facturas_finanzas_pendientes WHERE factura_id = ?", (factura_id,)
    ).fetchone()
    if pendiente is None or not pendiente["internacional_json"]:
        return jsonify({"error": "Esta factura no es una factura internacional -- no tiene tipo de cambio que confirmar."}), 400

    meta = json.loads(pendiente["internacional_json"])

    data = request.get_json(force=True, silent=True) or {}
    try:
        tipo_cambio_definitivo = float(data.get("tipo_cambio_definitivo"))
    except (TypeError, ValueError):
        return jsonify({"error": "El tipo de cambio definitivo debe ser un número"}), 400

    # Bloqueado si la CxP ya tiene abonos -- mismo criterio de seguridad
    # que cambiar_condicion_pago_factura: no hay forma de revertir un pago
    # que ya "salió", así que no se puede reescribir el monto por debajo
    # de un pago ya hecho.
    cxp_existentes = db.execute(
        "SELECT * FROM cuentas_por_pagar WHERE factura_id = ? AND estado != 'anulada'", (factura_id,)
    ).fetchall()
    for cxp in cxp_existentes:
        total_pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (cxp["id"],)
        ).fetchone()[0]
        if total_pagado > 0:
            return jsonify({
                "error": "Esta factura ya tiene abonos registrados en su Cuenta por Pagar y no se puede "
                         "confirmar el tipo de cambio definitivo sin revertirlos primero.",
            }), 400

    # Recalcula el prorrateo completo con el dólar nuevo -- 'meta["lineas"]'
    # ya trae 'cantidad'/'costo_unitario_usd' de cada línea (lo que pide
    # _calcular_prorrateo_internacional como 'lineas_in'), sin tocar, así
    # que esto es seguro de repetir tantas veces como haga falta.
    desglose_nuevo, error = _calcular_prorrateo_internacional(
        meta["lineas"], tipo_cambio_definitivo, meta["shipping_usd"], meta["paypal_usd"], meta["dhl_clp"],
    )
    if error:
        return jsonify({"error": error}), 400

    lineas_factura = db.execute(
        "SELECT * FROM factura_lineas WHERE factura_id = ? ORDER BY id", (factura_id,)
    ).fetchall()
    if len(lineas_factura) != len(desglose_nuevo):
        # No debería pasar nunca -- factura_lineas se llenó 1 a 1 desde
        # este mismo desglose al aprobar la factura (ver crear_factura_internacional
        # / _registrar_factura) y nadie más agrega/quita líneas de una
        # factura internacional después. Se corta acá en vez de aplicar
        # una corrección desalineada.
        return jsonify({"error": "El detalle de esta factura no calza con su desglose internacional original -- contacta soporte antes de continuar."}), 400

    # 'lineas_json' del pendiente (que sigue existiendo aunque la factura ya
    # esté aprobada) también se actualiza -- _factura_pendiente_recepcion_dict
    # calcula monto_neto/monto_iva/monto_total EN VIVO desde acá (no desde
    # 'facturas'), y es lo que usa la lista de Facturas para mostrar el monto
    # facturado. Sin esto, la lista seguiría mostrando el total viejo aunque
    # el detalle (que sí trae la factura real) ya mostrara el corregido.
    lineas_json_pendiente = json.loads(pendiente["lineas_json"])

    ts = now_iso()
    items_corregidos = 0
    for idx, (linea, nuevo) in enumerate(zip(lineas_factura, desglose_nuevo)):
        costo_nuevo = nuevo["costo_unitario_final_clp"]
        db.execute("UPDATE factura_lineas SET precio_unitario = ? WHERE id = ?", (costo_nuevo, linea["id"]))
        lineas_json_pendiente[idx]["precio_unitario"] = costo_nuevo

        producto_id = linea["producto_id"]
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if producto is not None:
            balde = db.execute(
                "SELECT * FROM stock_proveedores WHERE producto_id = ? AND factura_id = ?",
                (producto_id, factura_id),
            ).fetchone()
            if balde is not None:
                # Todo ese balde vino de ESTA factura al mismo costo de
                # origen -- se corrige con un SET directo, no un promedio
                # ponderado (no hay nada más con qué ponderar).
                db.execute(
                    "UPDATE stock_proveedores SET costo_promedio = ?, actualizado_en = ? WHERE id = ?",
                    (costo_nuevo, ts, balde["id"]),
                )
                # Ajuste aproximado del CPP general del producto (ver
                # docstring de la función): solo la porción del stock
                # actual que sigue viva en ESTE balde se corrige acá.
                if producto["stock_disponible"] and producto["stock_disponible"] > 0:
                    delta = costo_nuevo - linea["precio_unitario"]
                    ajuste = (balde["cantidad"] * delta) / producto["stock_disponible"]
                    db.execute(
                        "UPDATE productos SET costo_compra = costo_compra + ?, actualizado_en = ? WHERE id = ?",
                        (ajuste, ts, producto_id),
                    )

        # El movimiento de 'entrada' original de esta línea (ver
        # _aplicar_entrada_stock, que lo deja con factura_id/factura_numero
        # puestos) -- se corrige para que quede auditable con el costo real.
        db.execute(
            "UPDATE movimientos SET costo_unitario = ? WHERE producto_id = ? AND factura_id = ? AND tipo = 'entrada'",
            (costo_nuevo, producto_id, factura_id),
        )

        # Todo ítem de OT/garantía que consumió el balde de esta factura --
        # ABIERTA O CERRADA, no importa -- queda con el costo real (a
        # pedido explícito del taller, ver docstring de la función).
        items_corregidos += db.execute(
            "UPDATE orden_items SET costo_unitario = ? WHERE factura_internacional_id = ? AND producto_id = ?",
            (costo_nuevo, factura_id, producto_id),
        ).rowcount
        items_corregidos += db.execute(
            "UPDATE garantia_items SET costo_unitario = ? WHERE factura_internacional_id = ? AND producto_id = ?",
            (costo_nuevo, factura_id, producto_id),
        ).rowcount

    # Totales de la factura (mismo cálculo que _registrar_factura la
    # primera vez) -- las líneas de gasto general de una factura
    # internacional no existen (_calcular_prorrateo_internacional las
    # rechaza), así que no hace falta sumarlas acá.
    monto_neto = round(sum(l["cantidad"] * d["costo_unitario_final_clp"] for l, d in zip(lineas_factura, desglose_nuevo)), 2)
    monto_iva = 0 if factura["exento_iva"] else round(monto_neto * 0.19, 2)
    monto_total = round(monto_neto + monto_iva, 2)
    db.execute(
        "UPDATE facturas SET monto_neto = ?, monto_iva = ?, monto_total = ? WHERE id = ?",
        (monto_neto, monto_iva, monto_total, factura_id),
    )

    # Reemplaza la(s) CxP activa(s) por unas nuevas con el monto correcto
    # -- mismo patrón "anular y regenerar" que cambiar_condicion_pago_factura
    # (las viejas se anulan, nunca se borran, para no perder el rastro),
    # con la MISMA condición de pago que ya tenía la factura (esto no la
    # cambia, solo corrige el monto).
    lineas_validadas_cxp = [(None, l["cantidad"], d["costo_unitario_final_clp"]) for l, d in zip(lineas_factura, desglose_nuevo)]
    for cxp in cxp_existentes:
        db.execute("UPDATE cuentas_por_pagar SET estado = 'anulada', actualizado_en = ? WHERE id = ?", (ts, cxp["id"]))
    _generar_cxp_de_factura(
        db, factura_id, factura["proveedor_id"], factura["fecha"], lineas_validadas_cxp,
        condicion_pago=factura["condicion_pago"], porcentaje_contado=factura["porcentaje_contado"],
        plazo_dias_credito=factura["plazo_dias_credito"], exento_iva=bool(factura["exento_iva"]),
    )

    meta["tipo_cambio_definitivo"] = tipo_cambio_definitivo
    meta["definitivo"] = True
    meta["fecha_definitivo"] = ts
    meta["lineas"] = desglose_nuevo
    db.execute(
        "UPDATE facturas_finanzas_pendientes SET internacional_json = ?, lineas_json = ? WHERE id = ?",
        (json.dumps(meta), json.dumps(lineas_json_pendiente), pendiente["id"]),
    )
    db.commit()

    nueva = db.execute(FACTURA_SELECT + " WHERE f.id = ?", (factura_id,)).fetchone()
    detalle = _factura_detalle(db, nueva)
    detalle["items_ot_garantia_corregidos"] = items_corregidos
    return jsonify(detalle)


@app.route("/api/facturas/pendientes-recepcion", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def crear_factura_pendiente_recepcion():
    data = request.get_json(force=True, silent=True) or {}
    numero = (data.get("numero") or "").strip().upper()
    proveedor_id = data.get("proveedor_id") or None
    fecha = (data.get("fecha") or "").strip()
    lineas_in = data.get("lineas") or []
    tipo_documento = (data.get("tipo_documento") or "factura").strip().lower()
    if tipo_documento not in TIPOS_DOCUMENTO_VALIDOS:
        return jsonify({"error": f"Tipo de documento inválido. Usa uno de: {', '.join(TIPOS_DOCUMENTO_VALIDOS)}"}), 400
    condicion_pago = (data.get("condicion_pago") or "credito").strip().lower()
    exento_iva = bool(data.get("exento_iva"))
    try:
        porcentaje_contado = float(data.get("porcentaje_contado") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El porcentaje al contado debe ser un número"}), 400
    try:
        plazo_dias_credito = int(data.get("plazo_dias_credito") or CXP_DIAS_VENCIMIENTO_DEFECTO)
    except (TypeError, ValueError):
        return jsonify({"error": "El plazo de crédito debe ser un número de días"}), 400

    if not lineas_in:
        return jsonify({"error": f"Agrega al menos un producto a la {'boleta' if tipo_documento == 'boleta' else 'factura'}"}), 400

    db = get_db()
    if proveedor_id:
        try:
            proveedor_id = int(proveedor_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Proveedor inválido"}), 400
        if not db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone():
            return jsonify({"error": "El proveedor seleccionado no existe"}), 400

    lineas_validadas, error = _validar_lineas_pendiente(db, lineas_in)
    if error:
        return jsonify({"error": error}), 400

    nueva, error, _se_aplico = _crear_pendiente_y_aplicar_si_posible(
        db, numero, proveedor_id, fecha, lineas_validadas, condicion_pago,
        porcentaje_contado, plazo_dias_credito, usuario_actual(), origen="manual",
        exento_iva=exento_iva, tipo_documento=tipo_documento,
    )
    if error:
        return jsonify({"error": error}), 400
    return jsonify(_factura_pendiente_recepcion_dict(db, nueva)), 201


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>", methods=["PUT"])
@requiere_rol("finanzas", "bodega")
def editar_factura_pendiente_recepcion(pendiente_id):
    """Corrige una factura pendiente de recepción ANTES de aprobarla, o
    DESPUÉS de que Bodega la rechazó — pensado tanto para las que llegan
    por WhatsApp con datos incompletos (folio no leído, producto que la IA
    no pudo matchear contra el catálogo) como para el camino normal de
    rechazo: Bodega rechaza con una nota (ver
    rechazar_factura_pendiente_recepcion) y quien la subió corrige acá lo
    que estaba mal SIN tener que volver a escribir toda la factura de
    cero, y al guardar vuelve sola a la cola de revisión ('pendiente') —
    la nota de rechazo y quién/cuándo rechazó quedan para siempre en
    historial_json (ver _registrar_historial_pendiente), no se pierden solo
    porque se corrigió y se volvió a aprobar después."""
    db = get_db()
    fila = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    if fila["estado"] not in ("pendiente", "rechazada"):
        return jsonify({"error": f"Esta factura ya fue {fila['estado']}, no se puede editar."}), 400
    estaba_rechazada = fila["estado"] == "rechazada"

    data = request.get_json(force=True, silent=True) or {}
    numero = (data.get("numero") if "numero" in data else fila["numero"]) or ""
    numero = numero.strip().upper()
    tipo_documento = (data.get("tipo_documento") if "tipo_documento" in data else fila["tipo_documento"]) or "factura"
    tipo_documento = tipo_documento.strip().lower()
    if tipo_documento not in TIPOS_DOCUMENTO_VALIDOS:
        return jsonify({"error": f"Tipo de documento inválido. Usa uno de: {', '.join(TIPOS_DOCUMENTO_VALIDOS)}"}), 400
    etiqueta_doc = "boleta" if tipo_documento == "boleta" else "factura"
    if not numero:
        return jsonify({"error": f"El número de {etiqueta_doc} es obligatorio"}), 400

    proveedor_id = fila["proveedor_id"]
    if "proveedor_id" in data:
        proveedor_id = data.get("proveedor_id") or None
        if proveedor_id:
            try:
                proveedor_id = int(proveedor_id)
            except (TypeError, ValueError):
                return jsonify({"error": "Proveedor inválido"}), 400
            if not db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone():
                return jsonify({"error": "El proveedor seleccionado no existe"}), 400

    fecha = data.get("fecha", fila["fecha"]) or ""

    lineas_in = data.get("lineas")
    if lineas_in is not None:
        lineas_validadas, error = _validar_lineas_pendiente(db, lineas_in)
        if error:
            return jsonify({"error": error}), 400
    else:
        lineas_validadas = json.loads(fila["lineas_json"])

    exento_iva = bool(data.get("exento_iva")) if "exento_iva" in data else bool(fila["exento_iva"])

    # Una boleta siempre es al contado (ver Migración 35 / _crear_pendiente_recepcion)
    # -- si al editar el borrador se cambia el interruptor de factura a
    # boleta, la condición de pago que hubiera quedado guardada de antes
    # (ej. crédito) se pisa acá para no dejar una boleta "a crédito" a
    # medio camino.
    condicion_pago = fila["condicion_pago"]
    porcentaje_contado = fila["porcentaje_contado"]
    if tipo_documento == "boleta":
        condicion_pago = "contado"
        porcentaje_contado = 100

    if numero != fila["numero"]:
        existe = db.execute("SELECT id FROM facturas WHERE numero = ?", (numero,)).fetchone()
        if existe:
            return jsonify({"error": f"Ya existe un documento registrado con el número '{numero}'"}), 400
        ya_pendiente = db.execute(
            "SELECT id FROM facturas_finanzas_pendientes WHERE numero = ? AND estado = 'pendiente' AND id != ?",
            (numero, pendiente_id),
        ).fetchone()
        if ya_pendiente:
            return jsonify({"error": f"Ya hay un documento pendiente de recepción con el número '{numero}'"}), 400

    if estaba_rechazada:
        db.execute(
            """
            UPDATE facturas_finanzas_pendientes
            SET numero = ?, proveedor_id = ?, fecha = ?, lineas_json = ?, estado = 'pendiente', exento_iva = ?,
                tipo_documento = ?, condicion_pago = ?, porcentaje_contado = ?
            WHERE id = ?
            """,
            (numero, proveedor_id, fecha, json.dumps(lineas_validadas), 1 if exento_iva else 0,
             tipo_documento, condicion_pago, porcentaje_contado, pendiente_id),
        )
        _registrar_historial_pendiente(db, pendiente_id, "corregida_reingresada")
    else:
        db.execute(
            """
            UPDATE facturas_finanzas_pendientes
            SET numero = ?, proveedor_id = ?, fecha = ?, lineas_json = ?, exento_iva = ?,
                tipo_documento = ?, condicion_pago = ?, porcentaje_contado = ?
            WHERE id = ?
            """,
            (numero, proveedor_id, fecha, json.dumps(lineas_validadas), 1 if exento_iva else 0,
             tipo_documento, condicion_pago, porcentaje_contado, pendiente_id),
        )
    db.commit()
    actualizada = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()

    if data.get("aplicar"):
        # "Terminar factura" desde la nueva ventana de Facturas (ver
        # /facturas/nueva): a diferencia de un simple "Guardar para
        # continuar más adelante" (que solo actualiza el borrador y lo deja
        # 'pendiente'), este paso intenta aplicarla de inmediato -- mismo
        # camino que ya usa el ingreso directo al crear (ver
        # _crear_pendiente_y_aplicar_si_posible), pero disparado a mano
        # sobre un pendiente que ya existía. A diferencia de ese camino, acá
        # SÍ se responde con error si no se pudo aplicar -- quien completó
        # el asistente espera que la factura quede lista, no que quede
        # pendiente en silencio sin explicación.
        _iniciar_transaccion_stock(db)
        factura_id, lineas_recibidas_registro, error_aplicar = _aprobar_pendiente_recepcion(db, actualizada)
        if error_aplicar:
            return jsonify({"error": error_aplicar}), 400
        ts = now_iso()
        db.execute(
            """
            UPDATE facturas_finanzas_pendientes
            SET estado = 'aprobada', factura_id = ?, lineas_recibidas_json = ?, revisado_por = ?, revisado_en = ?
            WHERE id = ?
            """,
            (factura_id, json.dumps(lineas_recibidas_registro), usuario_actual(), ts, pendiente_id),
        )
        _registrar_historial_pendiente(db, pendiente_id, "aplicada_directamente")
        db.commit()
        actualizada = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()

    return jsonify(_factura_pendiente_recepcion_dict(db, actualizada))


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>", methods=["DELETE"])
@requiere_rol("finanzas", "bodega")
def eliminar_factura_pendiente_recepcion(pendiente_id):
    """Descarta un borrador de factura pendiente que todavía no se aprobó
    -- el "Descartar" del doble check al cerrar a medio camino la nueva
    ventana de Facturas (ver /facturas/nueva). Solo borra la fila de
    facturas_finanzas_pendientes en sí: como nunca se llegó a aprobar, no
    hay stock ni Cuenta por Pagar que revertir (eso solo se aplica recién
    en _aprobar_pendiente_recepcion, ver también aprobar_factura_pendiente_recepcion).
    No se puede descartar una que ya fue aprobada (para esa no existe
    "deshacer" acá -- ver DELETE /api/facturas/<id>, que si hace falta
    revierte el stock de una factura real ya aplicada) ni una rechazada
    (esa sigue disponible para corregir y reingresar, ver
    editar_factura_pendiente_recepcion)."""
    db = get_db()
    fila = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    if fila["estado"] != "pendiente":
        return jsonify({"error": f"Esta factura ya fue {fila['estado']}, no se puede descartar."}), 400
    db.execute("DELETE FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>/lineas/<int:indice>/crear-producto", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def crear_y_vincular_producto_pendiente(pendiente_id, indice):
    """Crea un producto nuevo y lo vincula de inmediato a UNA línea sin
    resolver de una factura pendiente de recepción -- el punto de
    resolución para el hueco que reportó el taller: una factura puede
    traer un repuesto que todavía no existe en el catálogo (Excel, foto
    escaneada, WhatsApp o el formulario manual ya la dejan pasar igual con
    esa línea en 'producto_id: None', ver _validar_lineas_pendiente /
    _carga_masiva_revalidar_grupo). Quien esté revisando el pendiente antes
    de aprobarlo crea el producto que falta con sus propios datos (modelo,
    detalle, marca) y esta línea queda vinculada en el mismo paso, sin
    tener que ir a Logística aparte y después volver a editar la factura a
    mano.

    Antes esto quedaba reservado solo a Bodega (ver hallazgo #3 de la
    revisión técnica), pero el taller pasó a operar con una sola persona a
    cargo tanto de Bodega como de Facturas -- separar el permiso ya no
    aporta nada y solo agregaba un paso extra, así que ahora también
    'finanzas' puede crear el producto directamente acá. 'indice' es la
    posición de la línea dentro de lineas_json (0-based, en el mismo orden
    que ya devuelve el pendiente al listarlo/verlo) -- no el número de fila
    del Excel, que puede repetirse o no venir."""
    db = get_db()
    fila = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    if fila["estado"] != "pendiente":
        return jsonify({"error": f"Esta factura ya fue {fila['estado']}, no se puede editar."}), 400

    lineas = json.loads(fila["lineas_json"])
    if indice < 0 or indice >= len(lineas):
        return jsonify({"error": "Esa línea no existe en esta factura"}), 404
    linea = lineas[indice]
    if linea.get("producto_id") is not None:
        return jsonify({"error": "Esta línea ya está vinculada a un producto -- no hace falta crear uno nuevo"}), 400

    data = request.get_json(force=True, silent=True) or {}
    # El nuevo producto nace SIN stock ni costo (ambos en 0): la cantidad y
    # el precio que trae la factura se aplican recién al aprobarla, igual
    # que a cualquier otro producto que ya existía -- si se precargara acá
    # el stock/costo de la línea, se contaría dos veces (una al "crear", otra
    # al aprobar), el mismo error de fondo que ya se corrigió esta sesión
    # para las devoluciones de stock.
    datos_producto = {k: v for k, v in data.items() if k not in ("stock_disponible", "costo_compra")}
    nuevo, error, status = _crear_producto_desde_datos(db, datos_producto)
    if error:
        return jsonify({"error": error}), status

    linea["producto_id"] = nuevo["id"]
    linea["producto_nombre"] = nuevo["nombre"]
    lineas[indice] = linea
    db.execute(
        "UPDATE facturas_finanzas_pendientes SET lineas_json = ? WHERE id = ?",
        (json.dumps(lineas), pendiente_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    return jsonify({"producto": nuevo, "pendiente": _factura_pendiente_recepcion_dict(db, actualizada)}), 201


def _aprobar_pendiente_recepcion(db, fila):
    """Lógica compartida para aplicar un pendiente de recepción 'fila' (ya
    debe estar en estado 'pendiente'): valida sus líneas, registra la
    factura real (stock + CxP) EXACTAMENTE como quedó cargada (mismas
    cantidades, mismos precios, sin poder "ajustarla" de paso -- a pedido
    del taller, ver historial de este mismo comentario en versiones
    anteriores de esta función). Reutilizada tanto por el botón "Aprobar"
    de Bodega (aprobar_factura_pendiente_recepcion, el camino manual de
    siempre) como por el ingreso directo nuevo (ver
    _crear_pendiente_y_aplicar_si_posible), que intenta aplicar la factura
    de inmediato apenas se registra, sin pasar por una revisión aparte.

    Devuelve (factura_id, lineas_recibidas_registro, error). Si 'error'
    viene con texto, no se aplicó nada (ni transacción de stock ni CxP) y
    los otros dos valores son None -- así quien llama decide qué hacer con
    la fila de pendientes en sí (dejarla 'pendiente' tal cual para
    reintentar después, o responder ese error directo a quien la estaba
    aprobando a mano)."""
    tipo_documento = (fila["tipo_documento"] or "factura").strip().lower()
    etiqueta_doc = "boleta" if tipo_documento == "boleta" else "factura"
    if not (fila["numero"] or "").strip():
        return None, None, f"Esta {etiqueta_doc} todavía no tiene número de folio — complétalo (editar) antes de aprobarla."

    ya_existe = db.execute("SELECT id FROM facturas WHERE numero = ?", (fila["numero"],)).fetchone()
    if ya_existe:
        return None, None, f"Ya existe un documento registrado con el número '{fila['numero']}'"

    lineas_facturadas = json.loads(fila["lineas_json"])

    _iniciar_transaccion_stock(db)

    lineas_validadas = []
    lineas_gasto_general = []
    lineas_recibidas_registro = []
    lineas_ot_destino = {}
    for l in lineas_facturadas:
        if l.get("es_gasto_general"):
            # Sin concepto de "cantidad recibida" -- es un gasto, no un
            # repuesto que se cuenta al llegar. Siempre se incluye tal
            # cual quedó cargada; si Finanzas/Bodega no lo quiere en la
            # factura, se saca la línea editando el pendiente antes de
            # aprobar (mismo camino que para sacar una línea de producto).
            lineas_gasto_general.append(l)
            continue
        cantidad_recibida = l["cantidad"]
        lineas_recibidas_registro.append({"producto_id": l["producto_id"], "cantidad_recibida": cantidad_recibida})
        if cantidad_recibida <= 0:
            continue
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (l["producto_id"],)).fetchone() if l["producto_id"] is not None else None
        if not producto:
            nombre = l.get("producto_nombre") or l.get("texto_original") or "(sin identificar)"
            if l["producto_id"] is None:
                return None, None, f"La línea '{nombre}' todavía no está asociada a un producto del inventario — corrígela (editar) antes de aprobar."
            return None, None, f"El producto '{nombre}' ya no existe en el inventario"
        if l.get("destino") == "ot":
            # Se revalida acá (no solo al cargar la línea, ver
            # _validar_destino_ot_linea) por si la OT se cerró entre que se
            # armó la factura y que se aprueba -- no queremos dejar la
            # factura a medio aplicar (stock sí, descarga a la OT no).
            orden_id = l.get("orden_id")
            orden = db.execute("SELECT estado FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
            if orden is None:
                return None, None, f"La OT #{orden_id} de la línea '{producto['nombre']}' ya no existe -- corrígela (editar) antes de aprobar."
            if orden["estado"] in ESTADOS_CERRADOS:
                return None, None, f"La OT #{orden_id} de la línea '{producto['nombre']}' está '{orden['estado']}' y no admite más ítems -- corrígela (editar) antes de aprobar."
            lineas_ot_destino[producto["id"]] = {"orden_id": orden_id}
        lineas_validadas.append((producto, cantidad_recibida, l["precio_unitario"]))

    if not lineas_validadas and not lineas_gasto_general:
        return None, None, "No se puede aprobar una recepción sin ningún producto recibido — si nada llegó, rechaza la factura."

    # Defensa en profundidad (ver Migración 35): una boleta siempre es al
    # contado, sin importar lo que haya quedado guardado en condicion_pago
    # -- normalmente ya viene forzado desde que se creó/editó el pendiente
    # (ver _crear_pendiente_recepcion / editar_factura_pendiente_recepcion),
    # pero se revalida acá mismo, justo antes de aplicar, para no depender
    # de que todos los caminos anteriores lo hayan hecho bien.
    condicion_pago = fila["condicion_pago"]
    porcentaje_contado = fila["porcentaje_contado"]
    if tipo_documento == "boleta":
        condicion_pago = "contado"
        porcentaje_contado = 100

    # 'es_internacional' se lee de internacional_json (no de un flag aparte)
    # -- ver el docstring de _crear_pendiente_recepcion sobre por qué esa
    # columna se guarda en el INSERT inicial y no con un UPDATE después:
    # este chequeo tiene que ver el valor correcto incluso cuando 'fila'
    # viene de la ruta de aplicación automática (auto_aplicar=True), que
    # corre en la misma operación en que se crea el pendiente.
    factura_id = _registrar_factura(
        db, fila["numero"], fila["proveedor_id"], fila["fecha"], lineas_validadas, usuario_actual(),
        aplicar_stock=True, condicion_pago=condicion_pago,
        porcentaje_contado=porcentaje_contado, plazo_dias_credito=fila["plazo_dias_credito"],
        lineas_gasto_general=lineas_gasto_general, exento_iva=bool(fila["exento_iva"]),
        lineas_ot_destino=lineas_ot_destino, tipo_documento=tipo_documento,
        es_internacional=fila["internacional_json"] is not None,
    )
    return factura_id, lineas_recibidas_registro, None


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>/aprobar", methods=["POST"])
@requiere_rol("bodega")
def aprobar_factura_pendiente_recepcion(pendiente_id):
    """Aprueba la recepción a mano -- camino que ahora queda como respaldo
    para lo que la nueva ruta de ingreso directo no pudo dejar aplicado
    sola (ej. una línea con un producto nuevo que todavía falta crear en
    el catálogo, ver _crear_pendiente_y_aplicar_si_posible) y para las
    facturas que llegan por WhatsApp (que a propósito siguen pasando
    siempre por acá, ver _procesar_foto_factura_whatsapp). Quien aprueba
    no puede modificar ningún dato en este paso -- si lo que llegó
    físicamente no calza con lo facturado, el camino correcto es rechazar
    la factura (con una nota explicando el motivo) para que quien la
    subió la corrija y la reingrese."""
    db = get_db()
    # El lock se toma ANTES de leer 'fila' (no solo dentro de
    # _aprobar_pendiente_recepcion, que lo toma más adentro) -- si dos
    # clics en "Aprobar" llegan casi juntos, sin este lock ambos podían leer
    # estado='pendiente' antes de que cualquiera de los dos alcanzara a
    # marcarla 'aprobada', y los dos terminaban aplicando stock y CxP por
    # duplicado para la misma factura (hallazgo #8/#9 del informe de
    # revisión). Con el lock desde acá, el segundo clic espera a que el
    # primero confirme su cambio de estado antes de leer, y ve la factura
    # ya aprobada.
    _iniciar_transaccion_stock(db)
    fila = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    if fila["estado"] != "pendiente":
        return jsonify({"error": f"Esta factura ya fue {fila['estado']}, no se puede volver a procesar."}), 400

    factura_id, lineas_recibidas_registro, error = _aprobar_pendiente_recepcion(db, fila)
    if error:
        return jsonify({"error": error}), 400

    ts = now_iso()
    db.execute(
        """
        UPDATE facturas_finanzas_pendientes
        SET estado = 'aprobada', factura_id = ?, lineas_recibidas_json = ?, revisado_por = ?, revisado_en = ?
        WHERE id = ?
        """,
        (factura_id, json.dumps(lineas_recibidas_registro), usuario_actual(), ts, pendiente_id),
    )
    _registrar_historial_pendiente(db, pendiente_id, "aprobada")
    db.commit()

    nueva = db.execute(FACTURA_SELECT + " WHERE f.id = ?", (factura_id,)).fetchone()
    return jsonify(_factura_detalle(db, nueva)), 201


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>/rechazar", methods=["POST"])
@requiere_rol("bodega")
def rechazar_factura_pendiente_recepcion(pendiente_id):
    db = get_db()
    fila = db.execute("SELECT * FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    if fila["estado"] != "pendiente":
        return jsonify({"error": f"Esta factura ya fue {fila['estado']}, no se puede volver a procesar."}), 400

    data = request.get_json(force=True, silent=True) or {}
    nota = (data.get("nota") or "").strip()
    ts = now_iso()
    db.execute(
        """
        UPDATE facturas_finanzas_pendientes
        SET estado = 'rechazada', revisado_por = ?, revisado_en = ?, nota_rechazo = ?
        WHERE id = ?
        """,
        (usuario_actual(), ts, nota, pendiente_id),
    )
    _registrar_historial_pendiente(db, pendiente_id, "rechazada", nota=nota)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/facturas/pendientes-recepcion/<int:pendiente_id>/foto", methods=["GET"])
@requiere_rol("finanzas", "bodega")
def obtener_foto_factura_pendiente_recepcion(pendiente_id):
    """Sirve la foto original de un pendiente de recepción — hoy solo la
    tienen los que llegaron por WhatsApp (ver _procesar_foto_factura_whatsapp,
    origen='whatsapp'); manual/Excel/foto-escaneada no guardan foto propia
    porque esos datos ya se tipearon/matchearon en el momento."""
    db = get_db()
    fila = db.execute(
        "SELECT foto_archivo FROM facturas_finanzas_pendientes WHERE id = ?", (pendiente_id,)
    ).fetchone()
    if fila is None:
        return jsonify({"error": "No se encontró ese pendiente"}), 404
    if not fila["foto_archivo"]:
        return jsonify({"error": "Este pendiente no tiene una foto asociada"}), 404
    ruta = os.path.join(FACTURAS_WHATSAPP_DIR, secure_filename(fila["foto_archivo"]))
    if not os.path.exists(ruta):
        return jsonify({"error": "La foto ya no está disponible en el servidor"}), 404
    return send_file(ruta)


@app.route("/api/facturas/carga-masiva/confirmar", methods=["POST"])
@requiere_rol("finanzas", "bodega")
def carga_masiva_confirmar():
    """Confirma la previsualización (Excel o foto escaneada, mismo caché —
    ver 'origen' guardado al crearlo). Cada factura válida se intenta
    aplicar de inmediato (stock + CxP, ver
    _crear_pendiente_y_aplicar_si_posible) -- solo queda como PENDIENTE DE
    RECEPCIÓN, a la espera de aprobación manual, si le falta crear algún
    producto nuevo del catálogo. La condición de pago que se haya elegido
    en esta confirmación se aplica a todas las facturas del lote (si el
    Excel/foto trae varias facturas de proveedores distintos en una sola
    carga, todas quedan con la misma condición; para condiciones distintas
    por factura, se registran en cargas separadas)."""
    _carga_masiva_limpiar_cache()
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token")
    entrada = CARGA_MASIVA_CACHE.get(token)
    if not entrada:
        return jsonify({"error": "La previsualización expiró o no es válida. Vuelve a subir el archivo."}), 400

    condicion_pago = (data.get("condicion_pago") or "credito").strip().lower()
    try:
        porcentaje_contado = float(data.get("porcentaje_contado") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El porcentaje al contado debe ser un número"}), 400
    try:
        plazo_dias_credito = int(data.get("plazo_dias_credito") or CXP_DIAS_VENCIMIENTO_DEFECTO)
    except (TypeError, ValueError):
        return jsonify({"error": "El plazo de crédito debe ser un número de días"}), 400
    if condicion_pago not in CONDICIONES_PAGO_VALIDAS:
        return jsonify({"error": f"Condición de pago inválida. Usa una de: {', '.join(CONDICIONES_PAGO_VALIDAS)}"}), 400
    if condicion_pago == "mixto" and not (0 < porcentaje_contado < 100):
        return jsonify({"error": "Para una condición mixta, el porcentaje al contado debe estar entre 0 y 100 (sin incluirlos)"}), 400
    if plazo_dias_credito <= 0 and condicion_pago in ("credito", "mixto"):
        return jsonify({"error": "El plazo de crédito debe ser mayor a 0 días"}), 400

    db = get_db()
    origen = entrada.get("origen") or "excel"
    creadas, omitidas, aplicadas = [], [], []

    for grupo in entrada["facturas"]:
        if not grupo["valida"]:
            omitidas.append(grupo["numero"])
            continue

        # Revalidación rápida por si algo cambió desde la previsualización
        # (otra factura con el mismo número, un producto eliminado, etc.).
        ya_existe = db.execute("SELECT id FROM facturas WHERE numero = ?", (grupo["numero"],)).fetchone()
        ya_pendiente = db.execute(
            "SELECT id FROM facturas_finanzas_pendientes WHERE numero = ? AND estado = 'pendiente'", (grupo["numero"],)
        ).fetchone()
        if ya_existe or ya_pendiente:
            omitidas.append(grupo["numero"])
            continue

        proveedor_id = grupo.get("proveedor_id")
        if grupo.get("proveedor_nuevo") and grupo.get("proveedor_nombre"):
            # Mismo criterio de _buscar_proveedor_existente (RUT primero,
            # nombre como respaldo) — revalidación por si se creó el
            # proveedor recién, entre la previsualización y este confirmar.
            existente = _buscar_proveedor_existente(db, grupo["proveedor_nombre"], grupo.get("proveedor_rut"))
            if existente:
                proveedor_id = existente["id"]
            else:
                cur = db.execute(
                    "INSERT INTO proveedores (nombre, rut, creado_en) VALUES (?, ?, ?)",
                    (grupo["proveedor_nombre"], formatear_rut(grupo.get("proveedor_rut") or ""), now_iso()),
                )
                proveedor_id = cur.lastrowid

        lineas_validadas = []
        lineas_ok = True
        for linea in grupo["lineas"]:
            if linea["cantidad"] is None or linea["precio_unitario"] is None:
                lineas_ok = False
                break
            if linea["producto_id"] is None:
                # Producto que todavía no existe en el catálogo -- se deja
                # pasar igual (queda pendiente de que Bodega lo cree, ver
                # aprobar_factura_pendiente_recepcion) en vez de descartar
                # la factura completa por un solo repuesto sin matchear.
                lineas_validadas.append({
                    "producto_id": None, "producto_nombre": None,
                    "cantidad": linea["cantidad"], "precio_unitario": linea["precio_unitario"],
                    "texto_original": linea.get("texto_original"),
                    "codigo_leido": linea.get("codigo_leido"),
                    "modelo_leido": linea.get("modelo_leido"),
                })
                continue
            producto = db.execute("SELECT id, nombre FROM productos WHERE id = ?", (linea["producto_id"],)).fetchone()
            if not producto:
                lineas_ok = False
                break
            lineas_validadas.append({
                "producto_id": producto["id"], "producto_nombre": producto["nombre"],
                "cantidad": linea["cantidad"], "precio_unitario": linea["precio_unitario"],
                # Aunque esta línea SÍ matcheó un producto, se guarda igual
                # lo que leyó la IA (si vino de una foto) -- para que la
                # pantalla de revisión de Bodega pueda mostrar "Leído: ..."
                # junto al producto asignado en CUALQUIER línea, no solo en
                # las que quedaron sin resolver (ver pfd-lineas en
                # finanzas.js / afp-tbody-lineas en
                # alerta_facturas_pendientes.js) -- así un match automático
                # equivocado sigue siendo detectable después de confirmar.
                "texto_original": linea.get("texto_original"),
                "codigo_leido": linea.get("codigo_leido"),
                "modelo_leido": linea.get("modelo_leido"),
            })

        if not lineas_ok or not lineas_validadas:
            omitidas.append(grupo["numero"])
            continue

        _nueva, error, se_aplico = _crear_pendiente_y_aplicar_si_posible(
            db, grupo["numero"], proveedor_id, grupo.get("fecha") or "", lineas_validadas,
            condicion_pago, porcentaje_contado, plazo_dias_credito, usuario_actual(), origen=origen,
        )
        if error:
            omitidas.append(grupo["numero"])
            continue
        creadas.append(grupo["numero"])
        if se_aplico:
            aplicadas.append(grupo["numero"])

    CARGA_MASIVA_CACHE.pop(token, None)

    return jsonify({
        "creadas": creadas, "omitidas": omitidas, "total_creadas": len(creadas),
        "aplicadas": aplicadas, "total_aplicadas": len(aplicadas),
    })


@app.route("/api/facturas/<int:factura_id>/condicion-pago", methods=["PUT"])
@requiere_rol("finanzas")
def cambiar_condicion_pago_factura(factura_id):
    """Corrige la condición de pago (al contado / a crédito a N días /
    mixta) de una factura de compra YA REGISTRADA -- pensado para la nueva
    ruta de ingreso directo (ver _crear_pendiente_y_aplicar_si_posible):
    Bodega ingresa la factura y aplica el stock de inmediato, pero no
    siempre tiene la última palabra sobre las condiciones pactadas con el
    proveedor (eso lo termina de confirmar Finanzas) -- acá se corrige
    DESPUÉS sin tocar nada más de la factura (ni sus líneas ni el stock ya
    aplicado, que quedan intactos).

    Reemplaza TODAS las Cuentas por Pagar activas de esta factura por
    una(s) nueva(s) con la condición corregida (mismo cálculo que
    _generar_cxp_de_factura usó la primera vez) -- las viejas se anulan
    (nunca se borran, para no perder el rastro) y se generan de cero.
    Bloqueado si alguna ya tiene abonos registrados (incluido el abono
    automático que se crea junto con un tramo 'al contado') -- mismo
    criterio de seguridad que finanzas_anular_cxp_manual: no hay todavía
    forma de revertir un pago que ya "salió", así que no se puede
    reescribir la condición por debajo de un pago ya hecho."""
    db = get_db()
    factura = db.execute("SELECT * FROM facturas WHERE id = ?", (factura_id,)).fetchone()
    if factura is None:
        return jsonify({"error": "Factura no encontrada"}), 404
    if (factura["tipo_documento"] or "factura") == "boleta":
        return jsonify({"error": "Una boleta siempre es al contado -- no tiene condición de pago que corregir."}), 400

    data = request.get_json(force=True, silent=True) or {}
    condicion_pago = (data.get("condicion_pago") or "").strip().lower()
    if condicion_pago not in CONDICIONES_PAGO_VALIDAS:
        return jsonify({"error": f"Condición de pago inválida. Usa una de: {', '.join(CONDICIONES_PAGO_VALIDAS)}"}), 400
    try:
        porcentaje_contado = float(data.get("porcentaje_contado") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El porcentaje al contado debe ser un número"}), 400
    try:
        plazo_dias_credito = int(data.get("plazo_dias_credito") or CXP_DIAS_VENCIMIENTO_DEFECTO)
    except (TypeError, ValueError):
        return jsonify({"error": "El plazo de crédito debe ser un número de días"}), 400
    if condicion_pago == "mixto" and not (0 < porcentaje_contado < 100):
        return jsonify({"error": "Para una condición mixta, el porcentaje al contado debe estar entre 0 y 100 (sin incluirlos)"}), 400
    if plazo_dias_credito <= 0 and condicion_pago in ("credito", "mixto"):
        return jsonify({"error": "El plazo de crédito debe ser mayor a 0 días"}), 400

    cxp_existentes = db.execute(
        "SELECT * FROM cuentas_por_pagar WHERE factura_id = ? AND estado != 'anulada'", (factura_id,)
    ).fetchall()
    for cxp in cxp_existentes:
        total_pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (cxp["id"],)
        ).fetchone()[0]
        if total_pagado > 0:
            return jsonify({
                "error": "Esta factura ya tiene abonos registrados en su Cuenta por Pagar y no se puede "
                         "cambiar la condición de pago sin revertirlos primero.",
            }), 400

    # Mismo cálculo de monto que usó _generar_cxp_de_factura la primera
    # vez -- junta las líneas de producto (cantidad×precio_unitario) y las
    # de gasto general de esta factura, para que la CxP nueva quede por el
    # mismo total. El producto en sí no importa acá (_generar_cxp_de_factura
    # solo usa cantidad×precio_unitario de cada tupla), así que se manda
    # None en su lugar.
    lineas_producto = db.execute(
        "SELECT cantidad, precio_unitario FROM factura_lineas WHERE factura_id = ?", (factura_id,)
    ).fetchall()
    lineas_validadas = [(None, l["cantidad"], l["precio_unitario"]) for l in lineas_producto]
    gastos_generales = db.execute(
        "SELECT monto FROM factura_gastos_generales WHERE factura_id = ?", (factura_id,)
    ).fetchall()
    lineas_gasto_general = [{"monto": g["monto"]} for g in gastos_generales]

    ts = now_iso()
    for cxp in cxp_existentes:
        db.execute(
            "UPDATE cuentas_por_pagar SET estado = 'anulada', actualizado_en = ? WHERE id = ?",
            (ts, cxp["id"]),
        )

    _generar_cxp_de_factura(
        db, factura_id, factura["proveedor_id"], factura["fecha"], lineas_validadas,
        condicion_pago=condicion_pago, porcentaje_contado=porcentaje_contado,
        plazo_dias_credito=plazo_dias_credito, lineas_gasto_general=lineas_gasto_general,
        exento_iva=bool(factura["exento_iva"]),
    )

    # El pendiente de recepción del que nació esta factura (siempre existe
    # uno, sin importar si fue por el camino viejo de aprobación o el
    # nuevo de ingreso directo) guarda su propia copia de
    # condicion_pago/porcentaje_contado/plazo_dias_credito -- se actualiza
    # también acá para que la pantalla de detalle (pfd-info en
    # finanzas.js) no quede mostrando la condición vieja.
    pendiente = db.execute(
        "SELECT id FROM facturas_finanzas_pendientes WHERE factura_id = ?", (factura_id,)
    ).fetchone()
    if pendiente:
        db.execute(
            """
            UPDATE facturas_finanzas_pendientes
            SET condicion_pago = ?, porcentaje_contado = ?, plazo_dias_credito = ?
            WHERE id = ?
            """,
            (condicion_pago, porcentaje_contado, plazo_dias_credito, pendiente["id"]),
        )
        _registrar_historial_pendiente(db, pendiente["id"], "condicion_pago_editada")

    db.commit()

    nueva = db.execute(FACTURA_SELECT + " WHERE f.id = ?", (factura_id,)).fetchone()
    return jsonify(_factura_detalle(db, nueva))


# ---------------------------------------------------------------------------
# Agente de WhatsApp: un mecánico le escribe un mensaje de texto libre (ej.
# "ot 4012 trabajo de reparación listo, queda preparada para prueba de
# ruta") y el agente lo traduce en un cambio de estado de esa Orden de
# Trabajo. Hoy la ÚNICA acción que el agente puede ejecutar es cambiar el
# estado de una OT, a propósito: dejar que un mensaje de texto libre
# dispare acciones más sensibles (borrar algo, tocar presupuestos o
# precios) es un riesgo que no vale la pena todavía.
#
# Todavía no hay ninguna cuenta de WhatsApp Business conectada (eso lo
# hace el dueño del taller por su cuenta, ver instrucciones de entrega) —
# así que todo esto se probó con /api/agente/simular-mensaje (un mensaje
# "de prueba" desde el navegador, sin pasar por WhatsApp de verdad) y con
# la IA de Claude mockeada en los tests automatizados. El webhook real
# (/webhook/whatsapp, pensado para Twilio) usa exactamente la misma
# función de procesamiento por debajo, así que en cuanto se conecte una
# cuenta real no hay que tocar nada de esta lógica.
# ---------------------------------------------------------------------------
# TRANSICIONES_AGENTE (qué cambios de estado puede aplicar el agente) se
# define más abajo, después de importar ESTADOS_ORDEN/ESTADOS_CERRADOS desde
# modulos.operativo (ver el import al final del archivo) — acá solo queda la
# función que la usa.


def _normalizar_telefono(valor):
    """Deja un teléfono en un formato comparable sin importar de dónde venga:
    Twilio lo manda como 'whatsapp:+56912345678', **Meta lo manda SIN el más**
    ('56912345678'), y la persona que registra al mecánico lo escribe como se
    le ocurra ('+56 9 1234 5678', '56912345678').

    **Se queda con los dígitos y NADA más — el '+' también se descarta.**

    Antes se conservaba el '+' cuando venía, y eso hacía que un mecánico
    registrado como '+56946596850' no calzara con el '56946596850' que manda
    la Cloud API de Meta: el agente respondía "este número no está registrado"
    y parecía un problema del agente cuando era del formato. Con Twilio no se
    notaba porque él siempre manda el '+'.

    Los teléfonos ya guardados se normalizan por migración (ver init_db,
    agente_mecanicos), así que las dos puntas quedan en la misma forma."""
    valor = (valor or "").strip()
    if valor.lower().startswith("whatsapp:"):
        valor = valor[len("whatsapp:"):]
    return re.sub(r"\D", "", valor)


def _normalizar_patente(valor):
    """Deja solo letras/números en mayúscula, para poder comparar patentes
    aunque vengan con guiones o espacios distintos (ej. 'AB-12-CD' vs
    'ab12cd') -- usado por el archivado automático de fotos de WhatsApp
    (ver _agente_procesar_mensaje, rama 'vehiculo') para matchear la
    patente que lee la IA de la foto contra 'identificador' en equipos."""
    return re.sub(r"[^A-Z0-9]", "", (valor or "").upper())


def _agente_resolver_ot_por_patente_en_texto(texto, ordenes_abiertas):
    """Busca, dentro de un mensaje de puro texto (sin foto -- ver rama
    'es_mecanico_restringido' de _agente_procesar_mensaje), alguna palabra
    que calce EXACTO con la patente de una OT abierta. No es un regex de
    'forma de patente chilena' (eso sería frágil: formato viejo AA1234,
    formato nuevo BBBB12, con o sin guión) -- en cambio se normaliza cada
    palabra del mensaje (_normalizar_patente, deja solo A-Z0-9) y se compara
    contra las patentes YA existentes entre las OT abiertas. Así solo
    matchea patentes reales que el taller ya tiene registradas, sin
    falsos positivos por confundir un número cualquiera con una patente."""
    palabras = re.findall(r"[A-Za-z0-9-]{4,8}", texto or "")
    if not palabras:
        return None
    candidatas = {_normalizar_patente(p) for p in palabras if _normalizar_patente(p)}
    if not candidatas:
        return None
    for orden in ordenes_abiertas:
        patente_orden = _normalizar_patente(orden.get("patente") or "")
        if patente_orden and patente_orden in candidatas:
            return orden
    return None


def _agente_listar_ordenes_abiertas(db):
    """Lista liviana (sin ítems/pagos/costos, a diferencia de _orden_dict)
    de las OT que todavía admiten cambios de estado — cliente + datos del
    vehículo incluidos — para dárselas de contexto a la IA y que pueda
    identificar una OT por descripción ('la Haval H6 de Matías Vega')
    cuando el mecánico no escribe el número. Como el sistema ya impide que
    un mismo vehículo tenga dos OT abiertas a la vez (ver
    test_no_se_puede_crear_2_ot_abiertas_para_el_mismo_equipo), identificar
    por vehículo es tan confiable como por número mientras no haya
    ambigüedad entre clientes/vehículos distintos con nombres parecidos."""
    placeholders = ", ".join("?" for _ in ESTADOS_CERRADOS)
    filas = db.execute(
        f"""
        SELECT o.id AS ot_id, o.estado,
               c.nombre AS cliente_nombre,
               e.marca, e.modelo, e.color, e.identificador AS patente
        FROM ordenes_trabajo o
        JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN equipos e ON e.id = o.equipo_id
        WHERE o.estado NOT IN ({placeholders})
        ORDER BY o.id
        """,
        list(ESTADOS_CERRADOS),
    ).fetchall()
    return [dict(f) for f in filas]


# Campos de "Diagnóstico" y "Pruebas de ruta" que el agente de WhatsApp
# puede actualizar directo (sin revisión humana): son notas de texto que no
# disparan ningún efecto secundario sobre la OT (a diferencia de, por
# ejemplo, marcar el control de calidad como "aprobado"/"rechazado", que sí
# mueve la orden de estado — por eso qc_resultado queda afuera de esta
# lista y sigue siendo solo manual, ver actualizar_qc en modulos/operativo.py).
#
# Deliberadamente reducidos a lo mínimo — lo que un mecánico realmente
# alcanza a contar en un audio corto — a pedido del taller ("lo fundamental
# es lo que se dice por audio"). "responsable del diagnóstico" e
# "inspector" NO están en estas listas a propósito: nunca se completan con
# lo que interprete la IA del mensaje, siempre quedan igual al mecánico
# registrado que mandó el mensaje (ver _agente_actualizar_diagnostico_o_
# prueba_ruta más abajo), para que ese dato no se pueda falsear ni quede
# vacío por un mensaje ambiguo.
CAMPOS_DIAGNOSTICO_AGENTE = {
    "sintoma": "síntoma reportado por el cliente",
    "diagnostico_tecnico": "diagnóstico técnico (qué encontró y qué falla)",
    "diagnostico": "diagnóstico (resumen para el cliente)",
}
CAMPOS_PRUEBA_RUTA_AGENTE = {
    "qc_observaciones": "observaciones generales de la prueba de ruta",
}

# Guía extra SOLO para el campo 'diagnostico' del schema que se le manda a
# la IA (nunca se usa como etiqueta corta en las respuestas al mecánico —
# para eso está CAMPOS_DIAGNOSTICO_AGENTE de arriba). Va aparte porque este
# campo es el único que se le muestra al cliente junto al presupuesto, así
# que conviene pedirle a la IA un párrafo más desarrollado que el resto de
# los campos (que son solo notas internas del taller).
_DESCRIPCION_CAMPO_DIAGNOSTICO_CLIENTE = (
    "Texto para el campo 'diagnóstico (resumen para el cliente)': va impreso junto al presupuesto que "
    "recibe el cliente, así que tiene que ser un párrafo corto pero completo (no una sola frase suelta) "
    "que mencione la falla encontrada, la causa técnica y el trabajo que se recomienda, en un lenguaje "
    "simple que un cliente sin conocimientos mecánicos entienda — lo justo para que entienda POR QUÉ se "
    "le está cobrando cada ítem, sin volverse un texto largo. Omitir si el mensaje no da esa información."
)

# Tope de acciones distintas que se procesan de un solo mensaje (ver
# _agente_ejecutar_una_accion / _agente_procesar_mensaje). Un mensaje de
# audio normalmente trae 1, a veces 2 (ej. "diagnóstico X, pásala a
# reparación"); 5 es un margen generoso para no cortar casos reales, pero
# acotado a propósito — así un mensaje raro/mal interpretado no puede hacer
# que el agente encadene una cantidad de cambios indefinida de una sola
# vez. También se refuerza en el propio schema de la IA (maxItems) para
# que Claude ni siquiera intente devolver más.
MAX_ACCIONES_POR_MENSAJE = 5


def _llamar_claude_generar_diagnostico_cliente(sintoma, diagnostico_tecnico):
    """Genera el texto del campo 'diagnóstico (resumen para el cliente)' a
    partir del diagnóstico técnico que se esté escribiendo -- pensado para
    autocompletar ese campo en tiempo real desde la pestaña Diagnóstico de
    una OT (ver /api/operativo/ia/diagnostico-cliente más abajo), con la
    MISMA redacción/guía que ya usa el agente de WhatsApp cuando el
    mecánico manda un audio y la IA completa 'diagnostico' como parte de
    _llamar_claude_interpretar_mensaje_agente (ver
    _DESCRIPCION_CAMPO_DIAGNOSTICO_CLIENTE arriba) -- así el resumen que ve
    el cliente es consistente sea cual sea el camino por el que se generó.
    Devuelve un string (puede ser vacío) o lanza RuntimeError con un
    mensaje explicable para mostrar en la interfaz."""
    if requests is None:
        raise RuntimeError("Falta la librería 'requests' en el servidor.")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Falta la variable de entorno ANTHROPIC_API_KEY en el servidor.")

    diagnostico_tecnico = (diagnostico_tecnico or "").strip()
    if not diagnostico_tecnico:
        return ""

    herramienta = {
        "name": "generar_resumen_cliente",
        "description": "Registra el resumen de diagnóstico en lenguaje simple para el cliente.",
        "input_schema": {
            "type": "object",
            "properties": {"texto": {"type": "string", "description": _DESCRIPCION_CAMPO_DIAGNOSTICO_CLIENTE}},
            "required": ["texto"],
        },
    }
    contexto_sintoma = f"Síntoma reportado por el cliente: {(sintoma or '').strip()}\n" if (sintoma or "").strip() else ""
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "tools": [herramienta],
        "tool_choice": {"type": "tool", "name": "generar_resumen_cliente"},
        "messages": [{
            "role": "user",
            "content": (
                f"{contexto_sintoma}Diagnóstico técnico interno del taller (notas del mecánico, puede estar "
                f"incompleto todavía): {diagnostico_tecnico}\n\n"
                "Redacta el resumen de diagnóstico para el cliente usando la herramienta generar_resumen_cliente, "
                "a partir de lo que ya está escrito arriba aunque no esté terminado."
            ),
        }],
    }
    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo contactar al servicio de IA: {e}")

    if resp.status_code >= 400:
        detalle = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(f"El servicio de IA respondió con error ({resp.status_code}): {detalle}")

    data = resp.json()
    for bloque in data.get("content", []):
        if bloque.get("type") == "tool_use" and bloque.get("name") == "generar_resumen_cliente":
            return (bloque.get("input") or {}).get("texto", "") or ""

    raise RuntimeError("La IA no devolvió el resumen en el formato esperado.")


def _llamar_claude_extraer_diagnostico_dictado(texto_transcrito):
    """Separa una transcripción de audio dictado directo en la pestaña
    Diagnóstico de una OT (ver /api/operativo/ia/dictado-diagnostico más
    abajo) en los campos 'síntoma' y 'diagnóstico técnico' -- mismo tipo de
    llamada puntual y acotada que _llamar_claude_generar_diagnostico_cliente
    (una sola herramienta forzada, no el intérprete grande de WhatsApp
    _llamar_claude_interpretar_mensaje_agente, que además identifica la OT y
    ejecuta acciones -- acá la OT ya está abierta en pantalla, así que solo
    hace falta separar el texto en los dos campos). Reusa las mismas
    descripciones de campo que ya usa el agente de WhatsApp
    (CAMPOS_DIAGNOSTICO_AGENTE) para que la redacción sea consistente sea
    cual sea el camino. Devuelve un dict {"sintoma": ..., "diagnostico_tecnico": ...}
    (strings vacíos para lo que el dictado no haya mencionado) o lanza
    RuntimeError con un mensaje explicable para mostrar en la interfaz."""
    if requests is None:
        raise RuntimeError("Falta la librería 'requests' en el servidor.")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Falta la variable de entorno ANTHROPIC_API_KEY en el servidor.")

    texto_transcrito = (texto_transcrito or "").strip()
    if not texto_transcrito:
        return {"sintoma": "", "diagnostico_tecnico": ""}

    herramienta = {
        "name": "completar_diagnostico",
        "description": "Registra el síntoma y el diagnóstico técnico separados a partir de un dictado hablado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sintoma": {"type": "string", "description": CAMPOS_DIAGNOSTICO_AGENTE["sintoma"]},
                "diagnostico_tecnico": {"type": "string", "description": CAMPOS_DIAGNOSTICO_AGENTE["diagnostico_tecnico"]},
            },
            "required": ["sintoma", "diagnostico_tecnico"],
        },
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "tools": [herramienta],
        "tool_choice": {"type": "tool", "name": "completar_diagnostico"},
        "messages": [{
            "role": "user",
            "content": (
                f"Transcripción de un audio dictado por un mecánico directo en el sistema, describiendo el "
                f"diagnóstico de una orden de trabajo: \"{texto_transcrito}\"\n\n"
                "Usa la herramienta completar_diagnostico para separar esta transcripción en 'sintoma' (lo que "
                "el cliente reportó que le pasaba al vehículo, si el dictado lo menciona) y "
                "'diagnostico_tecnico' (lo que el mecánico encontró: falla, componentes dañados, mediciones, "
                "resultado de pruebas). Deja un campo como texto vacío \"\" si el dictado no menciona esa parte "
                "-- no inventes contenido que no esté en la transcripción. No repitas la transcripción completa "
                "en los dos campos si ya se puede distinguir cuál parte es cuál."
            ),
        }],
    }
    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo contactar al servicio de IA: {e}")

    if resp.status_code >= 400:
        detalle = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(f"El servicio de IA respondió con error ({resp.status_code}): {detalle}")

    data = resp.json()
    for bloque in data.get("content", []):
        if bloque.get("type") == "tool_use" and bloque.get("name") == "completar_diagnostico":
            entrada = bloque.get("input") or {}
            return {
                "sintoma": entrada.get("sintoma", "") or "",
                "diagnostico_tecnico": entrada.get("diagnostico_tecnico", "") or "",
            }

    raise RuntimeError("La IA no devolvió los campos en el formato esperado.")


def _llamar_claude_interpretar_mensaje_agente(texto_mensaje, ordenes_abiertas):
    """Usa Claude (misma integración por HTTPS directo que
    _llamar_claude_vision_factura, más arriba) para traducir un mensaje de
    WhatsApp de un mecánico en una o MÁS acciones concretas — un mismo
    mensaje/audio puede pedir varias cosas a la vez (ej. "diagnóstico X,
    pásala a reparación" son dos acciones: 'actualizar_diagnostico' Y
    'cambiar_estado_ot'). Devuelve siempre un diccionario con la clave
    'acciones' (una lista, nunca vacía) — nunca texto libre, gracias a
    tool_choice forzado.

    'ordenes_abiertas' (ver _agente_listar_ordenes_abiertas) se le pasa a
    la IA como contexto para que pueda identificar la OT por descripción
    del vehículo o del cliente (ej. "la Haval H6 de Matías Vega quedó
    esperando repuestos") y no solo cuando el mecánico escribe el número
    explícito — así el mensaje puede ser bastante más natural."""
    if requests is None:
        raise RuntimeError("Falta la librería 'requests' en el servidor — no se puede contactar a la IA.")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("No hay una clave de IA configurada (ANTHROPIC_API_KEY) en el servidor.")

    estados_descripcion = "\n".join(f'- {clave}: "{ESTADOS_LABEL[clave]}"' for clave in ESTADOS_ORDEN)

    if ordenes_abiertas:
        lineas_ordenes = []
        for o in ordenes_abiertas:
            vehiculo = " ".join(
                p for p in [o.get("marca"), o.get("modelo"), o.get("color")] if p
            ) or "(sin vehículo registrado)"
            patente = f", patente {o['patente']}" if o.get("patente") else ""
            lineas_ordenes.append(
                f"- OT {o['ot_id']}: cliente \"{o['cliente_nombre']}\", vehículo {vehiculo}{patente}, "
                f"estado actual: \"{ESTADOS_LABEL.get(o['estado'], o['estado'])}\""
            )
        ordenes_descripcion = "\n".join(lineas_ordenes)
    else:
        ordenes_descripcion = "(no hay ninguna OT abierta en este momento)"

    campos_diagnostico_schema = {
        campo: {
            "type": "string",
            "description": (
                _DESCRIPCION_CAMPO_DIAGNOSTICO_CLIENTE if campo == "diagnostico"
                else f"Texto para el campo '{etiqueta}' del diagnóstico. Omitir si el mensaje no da esa información."
            ),
        }
        for campo, etiqueta in CAMPOS_DIAGNOSTICO_AGENTE.items()
    }
    campos_prueba_ruta_schema = {
        campo: {
            "type": "string",
            "description": f"Texto para el campo '{etiqueta}' de la prueba de ruta. Omitir si el mensaje no da esa información.",
        }
        for campo, etiqueta in CAMPOS_PRUEBA_RUTA_AGENTE.items()
    }

    accion_item_schema = {
        "type": "object",
        "properties": {
            "tipo": {
                "type": "string",
                "enum": [
                    "cambiar_estado_ot", "actualizar_diagnostico", "actualizar_prueba_ruta", "no_reconocida",
                ],
                "description": (
                    "'cambiar_estado_ot' si el mensaje pide explícitamente cambiar el estado de una Orden de "
                    "Trabajo Y menciona un número de OT claro (o un vehículo/cliente que identifica una sola "
                    "OT sin ambigüedad). "
                    "'actualizar_diagnostico' si el mensaje describe lo que el mecánico encontró o hizo "
                    "durante el diagnóstico de una OT (síntoma, causa, pieza dañada, mediciones, etc.). "
                    "'actualizar_prueba_ruta' si el mensaje describe el resultado de una prueba de ruta o "
                    "inspección final de una OT (observaciones generales). "
                    "'no_reconocida' si el mensaje completo (o una parte que no calza con ninguna de las "
                    "anteriores) es un saludo, pregunta, o algo ambiguo/sin OT identificable."
                ),
            },
            "ot_id": {
                "type": "integer",
                "description": "El número de OT a la que aplica ESTA acción (ej. 'OT 4012' -> 4012).",
            },
            "estado_destino": {
                "type": "string",
                "enum": ESTADOS_ORDEN,
                "description": "Solo para tipo 'cambiar_estado_ot': el estado interno al que el mecánico quiere pasar la OT.",
            },
            **campos_diagnostico_schema,
            **campos_prueba_ruta_schema,
        },
        "required": ["tipo"],
    }

    herramienta = {
        "name": "interpretar_mensaje_taller",
        "description": "Registra la o las acciones que pide un mensaje de WhatsApp de un mecánico de un taller mecánico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "acciones": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_ACCIONES_POR_MENSAJE,
                    "items": accion_item_schema,
                    "description": (
                        "Una entrada por cada intención DISTINTA que traiga el mensaje. La mayoría de los "
                        "mensajes traen una sola, pero es común que traigan dos (ej. contar el diagnóstico Y "
                        "pedir que se cambie el estado en el mismo audio) — en ese caso van dos objetos "
                        "separados en esta lista, uno por intención, cada uno con su 'tipo' correspondiente. "
                        "Si el mensaje completo no se entiende, esta lista igual debe traer un objeto con "
                        "tipo 'no_reconocida'."
                    ),
                },
                "resumen": {
                    "type": "string",
                    "description": "Una frase corta en español que resuma qué entendiste del mensaje completo (todas las acciones).",
                },
            },
            "required": ["acciones", "resumen"],
        },
    }

    campos_diagnostico_descripcion = "\n".join(f"- {c}: {e}" for c, e in CAMPOS_DIAGNOSTICO_AGENTE.items())
    campos_prueba_ruta_descripcion = "\n".join(f"- {c}: {e}" for c, e in CAMPOS_PRUEBA_RUTA_AGENTE.items())

    prompt = (
        "Eres el traductor de mensajes de WhatsApp de mecánicos de un taller mecánico hacia el sistema de "
        "gestión del taller. Los mecánicos escriben mensajes informales (a veces transcritos de un audio) "
        "para avisar que avanzaron con una Orden de Trabajo (OT). Un mismo mensaje puede pedir VARIAS cosas "
        "a la vez — tu trabajo es descomponerlo en una lista de acciones ('acciones'), una por cada "
        "intención distinta que traiga, en vez de forzarlo a una sola. Las intenciones posibles son:\n"
        "1. Cambiar el estado de la OT, ej: \"ot 4012 trabajo de reparación listo, queda preparada para "
        "prueba de ruta\", o sin decir el número: \"la haval h6 de matias vega esta esperando repuestos\" "
        "— esto es 'cambiar_estado_ot'.\n"
        "2. Contar lo que encontraron en el DIAGNÓSTICO de una OT, ej: \"ot 4012 el sintoma era ruido en la "
        "suspension delantera, se detecto el amortiguador derecho roto\" — esto es 'actualizar_diagnostico'.\n"
        "3. Contar el resultado de una PRUEBA DE RUTA / inspección final de una OT, ej: \"ot 4012 prueba de "
        "ruta ok, frenos y dirección responden bien, sin ruidos\" — esto es 'actualizar_prueba_ruta'. Quién "
        "hizo la prueba NUNCA se le pide a la IA — el sistema ya sabe quién es porque es el mismo mecánico "
        "que mandó el mensaje.\n\n"
        "Ejemplo de mensaje con DOS intenciones (esto es lo más importante de entender): \"diagnostico haval "
        "de matias patina en 5ta marcha y se recomienda reconstruccion completa y pasar ot a reparacion\" "
        "tiene que devolver DOS objetos en 'acciones': uno con tipo 'actualizar_diagnostico' (con el síntoma "
        "\"patina en 5ta marcha\" y la recomendación de reconstrucción completa) Y otro con tipo "
        "'cambiar_estado_ot' con estado_destino 'reparacion' — ambos con el mismo ot_id, el de esa OT. NO "
        "elijas solo una de las dos: si el mensaje pide las dos cosas, van las dos.\n\n"
        "Los estados posibles de una OT son (usa EXACTAMENTE estas claves internas en 'estado_destino'):\n"
        f"{estados_descripcion}\n\n"
        "Campos disponibles para 'actualizar_diagnostico' (usa EXACTAMENTE estos nombres de campo):\n"
        f"{campos_diagnostico_descripcion}\n\n"
        "Campos disponibles para 'actualizar_prueba_ruta' (usa EXACTAMENTE estos nombres de campo):\n"
        f"{campos_prueba_ruta_descripcion}\n\n"
        "Estas son TODAS las OT actualmente abiertas en el sistema (la única fuente válida de números de "
        "OT, clientes y vehículos — no existe ninguna otra):\n"
        f"{ordenes_descripcion}\n\n"
        "Reglas para identificar DE CUÁL OT habla cada acción:\n"
        "1. Si el mensaje menciona un número de OT explícito (ej. 'OT 4012', 'orden 4012', '#4012'), usa ese "
        "número — tiene que aparecer tal cual en la lista de arriba.\n"
        "2. Si el mensaje NO menciona un número, pero describe un vehículo y/o un cliente (marca, modelo, "
        "color, patente, nombre) que calza CLARAMENTE con UNA SOLA de las OT de la lista, usa el número de "
        "esa OT — no hace falta que el mecánico diga el número si la descripción ya identifica una sola OT "
        "sin ambigüedad. Los apellidos/nombres pueden venir con errores de tipeo o sin tildes.\n"
        "3. Si la descripción del vehículo/cliente podría calzar con MÁS DE UNA OT de la lista, o no calza "
        "con ninguna, usa 'no_reconocida' para esa intención y en 'resumen' explica la ambigüedad o que no "
        "encontraste esa OT — nunca elijas una al azar entre varias posibles.\n"
        "4. Si el mensaje completo es ambiguo, una pregunta, un saludo, o no tiene que ver con ninguna de "
        "las tres intenciones de arriba, devuelve una sola acción con tipo 'no_reconocida'. Ante la duda, "
        "prefiere 'no_reconocida' antes que adivinar y modificar la OT equivocada.\n"
        "5. Nunca inventes un número de OT que no esté en la lista de arriba.\n"
        "6. Para 'actualizar_diagnostico'/'actualizar_prueba_ruta': solo incluye los campos sobre los que el "
        "mensaje realmente da información — NUNCA inventes ni completes con texto vacío un campo que el "
        "mensaje no menciona, mejor omitirlo por completo (así no se borra información que ya estaba "
        "cargada en el sistema). El campo 'diagnostico' (resumen para el cliente) sí conviene redactarlo "
        "un poco más desarrollado que el resto — un párrafo corto que junte síntoma + causa + recomendación "
        "en lenguaje simple, porque ese texto se le muestra al cliente junto al presupuesto.\n"
        "7. Máximo una acción de tipo 'cambiar_estado_ot' y una de cada tipo de actualización por mensaje — "
        "si el mensaje repite la misma intención dos veces para la misma OT, combínala en una sola acción "
        "en vez de duplicarla.\n\n"
        f'Mensaje del mecánico:\n"{texto_mensaje}"'
    )

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "tools": [herramienta],
        "tool_choice": {"type": "tool", "name": "interpretar_mensaje_taller"},
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=60)
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo contactar al servicio de IA: {e}")

    if resp.status_code >= 400:
        detalle = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(f"El servicio de IA respondió con error ({resp.status_code}): {detalle}")

    data = resp.json()
    for bloque in data.get("content", []):
        if bloque.get("type") == "tool_use" and bloque.get("name") == "interpretar_mensaje_taller":
            return bloque.get("input") or {}

    raise RuntimeError("La IA no devolvió una respuesta en el formato esperado.")


def _transcribir_audio_whatsapp(audio_bytes, mime_type):
    """Transcribe una nota de voz a texto usando la API de OpenAI (ver
    bloque de configuración más arriba). Devuelve el texto transcrito, o
    lanza RuntimeError con un mensaje en español listo para mostrarle al
    mecánico si algo falla (sin OPENAI_API_KEY, audio muy pesado, formato
    rechazado, error de red, etc.) — nunca deja pasar una excepción cruda."""
    if requests is None:
        raise RuntimeError("Falta la librería 'requests' en el servidor — no se puede transcribir audio.")
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "No hay una clave de transcripción configurada (OPENAI_API_KEY) en el servidor — todavía no "
            "puedo entender notas de voz, escribe el mensaje en texto mientras tanto."
        )
    if not audio_bytes:
        raise RuntimeError("La nota de voz llegó vacía.")
    if len(audio_bytes) > OPENAI_AUDIO_MAX_BYTES:
        raise RuntimeError("La nota de voz es demasiado pesada (máximo 25 MB).")

    extension = "ogg"
    if mime_type:
        subtipo = mime_type.split("/")[-1].split(";")[0].strip().lower()
        if subtipo:
            extension = subtipo
    nombre_archivo = f"nota_voz.{extension}"

    try:
        resp = requests.post(
            OPENAI_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (nombre_archivo, audio_bytes, mime_type or "audio/ogg")},
            data={"model": OPENAI_TRANSCRIBE_MODEL},
            timeout=90,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo contactar al servicio de transcripción: {e}")

    if resp.status_code >= 400:
        detalle = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        raise RuntimeError(
            f"El servicio de transcripción respondió con error ({resp.status_code}): {detalle}. Puede que el "
            "formato de la nota de voz no sea compatible — intenta escribir el mensaje en texto."
        )

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("El servicio de transcripción devolvió una respuesta inesperada.")

    texto = (data.get("text") or "").strip()
    if not texto:
        raise RuntimeError("No se pudo entender nada en la nota de voz — intenta de nuevo o escribe el mensaje.")
    return texto


def _agente_guardar_sugerencia_diagnostico(db, ot_id, foto_id, mecanico_nombre, texto):
    """Guarda un comentario de un mecánico de perfil restringido (ver
    migración 16) como sugerencia de diagnóstico, en vez de escribirlo
    directo en diagnostico_tecnico/diagnostico de la OT. 'foto_id' queda en
    NULL cuando la sugerencia vino de un mensaje de puro texto (sin foto
    adjunta). Lo que hace visible estas sugerencias a la parte operativa es
    _agente_listar_sugerencias_pendientes(), usado desde el detalle de la
    OT en el módulo Operativo."""
    db.execute(
        """
        INSERT INTO sugerencias_diagnostico
            (ot_id, foto_id, mecanico_nombre, texto, creado_en, atendida)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (ot_id, foto_id, mecanico_nombre, texto, now_iso()),
    )
    db.commit()


def _agente_guardar_prueba_ruta(db, ot_id, foto_id, autor_nombre, texto):
    """Guarda un comentario de un mecánico de perfil 'probador_de_autos'
    (ver migración 21) como una entrada nueva en la bitácora de pruebas de
    ruta de la OT (tabla pruebas_ruta, migración 20) -- NUNCA como
    sugerencia de diagnóstico ni como acción de estado. A diferencia de
    _agente_guardar_sugerencia_diagnostico, cada llamada agrega una fila
    nueva (historial), no sobrescribe nada."""
    db.execute(
        """
        INSERT INTO pruebas_ruta (orden_id, texto, autor, origen, foto_id, creado_en)
        VALUES (?, ?, ?, 'whatsapp', ?, ?)
        """,
        (ot_id, texto, autor_nombre, foto_id, now_iso()),
    )
    db.commit()


def _agente_guardar_pre_entrega(db, ot_id, foto_id, autor_nombre, texto):
    """Guarda un comentario de un mecánico de perfil 'encargado_pre_entrega'
    (ver migración 30) como una entrada nueva en la bitácora de Pre entrega
    de la OT (tabla pre_entrega) -- mismo patrón que
    _agente_guardar_prueba_ruta, pensado para dejar registrada la última
    revisión antes de entregarle el vehículo al cliente."""
    db.execute(
        """
        INSERT INTO pre_entrega (orden_id, texto, autor, origen, foto_id, creado_en)
        VALUES (?, ?, ?, 'whatsapp', ?, ?)
        """,
        (ot_id, texto, autor_nombre, foto_id, now_iso()),
    )
    db.commit()


# Patrón usado por _agente_texto_confirma_listo_para_entrega: "list[oa]"
# (listo/lista) cerca de "entrega/entregar/retiro/retirar", en cualquier
# orden, dentro de la misma oración -- se define una sola vez a nivel de
# módulo para no recompilarlo en cada mensaje que llega por WhatsApp.
_PATRON_LISTO_PARA_ENTREGA = re.compile(
    r"\blist[oa]\b[^.\n]{0,25}\b(?:entrega|entregar|retiro|retirar)\w*"
    r"|\b(?:entrega|entregar|retiro|retirar)\w*[^.\n]{0,25}\blist[oa]\b"
)
# Negaciones/incertidumbre que, si aparecen en la MISMA oración que el
# patrón de arriba, invalidan la confirmación (ej. "no está lista para la
# entrega, falta el parachoques", "dudo que esté lista para la entrega",
# "el cliente preguntó si está lista para la entrega", "nunca va a estar
# lista para la entrega a este paso") -- a propósito la lista es corta y
# literal (no un intento de NLP de negación general), pero se amplió tras
# una auditoría que encontró casos reales donde el patrón de arriba
# matcheaba pese a que el mensaje NO era una confirmación (negaciones con
# "nunca"/"jamás", incertidumbre con "dudo"/"duda", una pregunta
# reenviada con "pregunt-", o una condición futura con "cuando"/"casi").
# Cualquier otro caso ambiguo ya queda descartado porque
# _PATRON_LISTO_PARA_ENTREGA simplemente no matchea.
_PATRON_NEGACION_LISTO = re.compile(
    r"\b(no|ni|sin|falta|faltan|todavia|nunca|jamas|dudo|duda|pregunt\w*|cuando|casi)\b"
)
# Frases de dos palabras que también expresan incertidumbre ("puede que",
# "capaz que") -- se buscan como texto literal, NO como palabra suelta
# "puede"/"capaz", porque "puede" sola SÍ aparece en confirmaciones válidas
# (ej. "ya se puede retirar, está listo").
_FRASES_NEGACION_LISTO = ("puede que", "capaz que")


def _agente_texto_confirma_listo_para_entrega(texto):
    """Detecta, de forma determinística y conservadora, si un mensaje del
    perfil 'encargado_pre_entrega' (ver Migración 30/31) confirma
    EXPLÍCITAMENTE que el vehículo quedó listo para la entrega -- a
    propósito NO se reutiliza acá el intérprete de IA
    (_llamar_claude_interpretar_mensaje_agente) que usa el perfil
    'completo' para cambiar de estado: el pedido del taller fue "la
    información tiene que ser muy clara... ante la duda dejarla sin
    movimiento", así que se prefiere un chequeo de palabras clave 100%
    predecible y auditable (sin depender de una llamada a un servicio
    externo, ni de que ANTHROPIC_API_KEY esté configurada, ni del riesgo de
    que una IA interprete de más) antes que dejarle la decisión a un
    modelo.

    Devuelve True SOLO si alguna oración del mensaje trae "listo"/"lista"
    cerca de "entrega"/"entregar"/"retiro"/"retirar" (en cualquier orden),
    esa misma oración no trae una negación/incertidumbre cercana (ver
    _PATRON_NEGACION_LISTO), y la oración no es una pregunta (no trae
    "?" -- una pregunta, propia o reenviada del cliente, no es una
    confirmación). Cualquier otro caso -- texto ambiguo, sin esas
    palabras, negado, dudoso, o formulado como pregunta -- devuelve
    False, que es justamente lo que hace que la OT NO se mueva de estado
    (ver _agente_procesar_mensaje, rama 'es_encargado_pre_entrega')."""
    texto_norm = normalizar_busqueda(texto or "")
    if not texto_norm:
        return False
    for oracion in re.split(r"[.\n]+", texto_norm):
        if "?" in oracion:
            continue
        if any(frase in oracion for frase in _FRASES_NEGACION_LISTO):
            continue
        if _PATRON_LISTO_PARA_ENTREGA.search(oracion) and not _PATRON_NEGACION_LISTO.search(oracion):
            return True
    return False


def _agente_pre_entrega_intentar_marcar_listo(db, ot, texto):
    """Si 'texto' confirma explícitamente que el vehículo está listo para
    la entrega (ver _agente_texto_confirma_listo_para_entrega), mueve la OT
    a 'listo' -- y SOLO a 'listo': este perfil no tiene permiso para
    ejecutar ningún otro cambio de estado, ni para sacar una OT de 'listo'
    una vez que llegó ahí (pedido del taller: "solo es posible moverla a
    esa casilla y solo esa. tampoco tiene permiso para sacar una ot de
    ahi"). Reutiliza _actualizar_estado_orden_por_agente -- la misma
    función que usa el perfil 'completo' para cambiar de estado -- para que
    los efectos secundarios de un cambio de estado (ver
    _manejar_transicion_cxc) se apliquen igual acá.

    Devuelve el texto que se agrega a la respuesta de WhatsApp, explicando
    SIEMPRE qué pasó (se movió, ya estaba en "listo", o no se encontró una
    confirmación explícita) -- nunca se queda callado sobre el estado,
    justo lo que pidió el taller ("debe preguntar" ante la ambigüedad)."""
    if not _agente_texto_confirma_listo_para_entrega(texto):
        return (
            "\n\n❓ No moví la OT a \"Listo para la entrega\" porque no encontré una confirmación "
            "explícita en tu mensaje. Si el vehículo ya está listo, escribe algo claro como "
            "\"listo para la entrega\" o \"lista para entregar\"."
        )
    if ot["estado"] == "listo":
        return "\n\n✅ La OT ya estaba marcada como \"Listo para la entrega\" -- no hice ningún cambio."
    _, error = _actualizar_estado_orden_por_agente(db, ot["ot_id"], "listo")
    if error:
        return f"\n\n⚠️ No pude mover la OT a \"Listo para la entrega\": {error}"
    return "\n\n🚚 Moví la OT a \"Listo para la entrega\"."


def _agente_listar_sugerencias_pendientes(db, ot_id):
    """Sugerencias de diagnóstico (ver _agente_guardar_sugerencia_diagnostico)
    aún no revisadas para una OT, más recientes primero."""
    filas = db.execute(
        "SELECT * FROM sugerencias_diagnostico WHERE ot_id = ? AND atendida = 0 ORDER BY creado_en DESC",
        (ot_id,),
    ).fetchall()
    return [dict(f) for f in filas]


def _agente_listar_todas_sugerencias_pendientes(db):
    """Igual que _agente_listar_sugerencias_pendientes pero sin acotar a
    una OT -- para la campanita de notificaciones (ver
    /api/notificaciones/*), que muestra TODAS las sugerencias sin revisar
    de cualquier OT, para que la parte operativa las vea sin tener que
    entrar una por una. Se suma info de la OT (cliente, patente) para que
    la lista sea legible sin otra llamada."""
    filas = db.execute(
        """
        SELECT
            s.*,
            o.estado AS ot_estado,
            c.nombre AS cliente_nombre,
            e.identificador AS patente
        FROM sugerencias_diagnostico s
        JOIN ordenes_trabajo o ON o.id = s.ot_id
        LEFT JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN equipos e ON e.id = o.equipo_id
        WHERE s.atendida = 0
        ORDER BY s.creado_en DESC
        """
    ).fetchall()
    return [dict(f) for f in filas]


def _operativo_listar_todas_pruebas_ruta_pendientes(db):
    """Igual que _agente_listar_todas_sugerencias_pendientes, pero para la
    bitácora de pruebas de ruta (tabla pruebas_ruta, Migración 20/23):
    todas las entradas llegadas por WhatsApp (perfil 'probador_de_autos')
    que todavía no se marcaron como revisadas, con la info de la OT para
    que la lista sea legible sin otra llamada."""
    filas = db.execute(
        """
        SELECT
            pr.*,
            o.estado AS ot_estado,
            c.nombre AS cliente_nombre,
            e.identificador AS patente
        FROM pruebas_ruta pr
        JOIN ordenes_trabajo o ON o.id = pr.orden_id
        LEFT JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN equipos e ON e.id = o.equipo_id
        WHERE pr.atendida = 0 AND pr.origen = 'whatsapp'
        ORDER BY pr.creado_en DESC
        """
    ).fetchall()
    return [dict(f) for f in filas]


def _operativo_listar_todas_pre_entrega_pendientes(db):
    """Igual que _operativo_listar_todas_pruebas_ruta_pendientes, pero para
    la bitácora de Pre entrega (tabla pre_entrega, Migración 30/31): todas
    las entradas llegadas por WhatsApp (perfil 'encargado_pre_entrega') que
    todavía no se marcaron como revisadas, con la info de la OT para que la
    lista sea legible sin otra llamada."""
    filas = db.execute(
        """
        SELECT
            pe.*,
            o.estado AS ot_estado,
            c.nombre AS cliente_nombre,
            e.identificador AS patente
        FROM pre_entrega pe
        JOIN ordenes_trabajo o ON o.id = pe.orden_id
        LEFT JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN equipos e ON e.id = o.equipo_id
        WHERE pe.atendida = 0 AND pe.origen = 'whatsapp'
        ORDER BY pe.creado_en DESC
        """
    ).fetchall()
    return [dict(f) for f in filas]


def _agente_ejecutar_una_accion(db, mecanico, accion):
    """Ejecuta UNA acción ya interpretada (un elemento de la lista
    'acciones' que devuelve _llamar_claude_interpretar_mensaje_agente) y
    devuelve un resultado uniforme, sin tocar la auditoría — quien llama
    (_agente_procesar_mensaje) junta los resultados de todas las acciones
    de un mismo mensaje y hace un solo registro de auditoría al final.

    Devuelve siempre:
        {"tipo", "codigo", "ot_id", "estado_anterior", "estado_nuevo", "exito", "mensaje"}
    ('codigo' es la misma etiqueta corta que antes se guardaba en la
    columna 'accion' de agente_mensajes para una sola acción — ej.
    'cambio_estado_ot' en éxito, 'transicion_no_permitida' o 'error' en
    fallo — se mantiene igual para no perder esa distinción ahora que
    varias acciones se consolidan en un solo registro de auditoría.)

    Cubre las 4 acciones posibles: 'cambiar_estado_ot',
    'actualizar_diagnostico', 'actualizar_prueba_ruta' y 'no_reconocida'.
    El responsable del diagnóstico / inspector de la prueba de ruta NUNCA
    sale de lo que la IA interpretó del mensaje — siempre es el mecánico
    que efectivamente mandó el mensaje de WhatsApp (ya autenticado en
    _agente_procesar_mensaje por su teléfono registrado)."""
    tipo = accion.get("tipo")

    def resultado(ot_id, exito, mensaje, estado_anterior="", estado_nuevo="", codigo=None):
        return {
            "tipo": tipo, "codigo": codigo or (tipo if exito else "error"), "ot_id": ot_id,
            "estado_anterior": estado_anterior, "estado_nuevo": estado_nuevo,
            "exito": exito, "mensaje": mensaje,
        }

    if tipo in ("actualizar_diagnostico", "actualizar_prueba_ruta"):
        campos_agente = CAMPOS_DIAGNOSTICO_AGENTE if tipo == "actualizar_diagnostico" else CAMPOS_PRUEBA_RUTA_AGENTE
        etiqueta_accion = "diagnóstico" if tipo == "actualizar_diagnostico" else "prueba de ruta"
        campo_responsable = "responsable_diagnostico" if tipo == "actualizar_diagnostico" else "qc_inspector"

        ot_id = accion.get("ot_id")
        if not isinstance(ot_id, int):
            return resultado(None, False, f"No pude identificar con claridad el número de OT en tu mensaje sobre el {etiqueta_accion}.")

        # El número que manda el mecánico es el de Talca, no la llave interna.
        orden = _agente_resolver_orden(db, ot_id)
        if orden is None:
            return resultado(ot_id, False, f"No encontré ninguna OT con el número {ot_id}.")

        if orden["estado"] in ESTADOS_CERRADOS:
            return resultado(
                ot_id, False,
                f'No pude actualizar el {etiqueta_accion}: la OT #{ot_id} ya está en estado '
                f'"{ESTADOS_LABEL[orden["estado"]]}", que no admite cambios por este medio.',
                estado_anterior=orden["estado"],
            )

        campos = {
            columna: accion.get(columna)
            for columna in campos_agente
            if (accion.get(columna) or "").strip()
        }
        if not campos:
            return resultado(ot_id, False, f"No encontré ningún dato concreto de {etiqueta_accion} en tu mensaje para la OT #{ot_id}.")

        # Se agrega DESPUÉS de comprobar que hay datos reales que guardar
        # (así un mensaje sin información de diagnóstico sigue
        # rechazándose), pero siempre que se aplique algo, el responsable
        # queda registrado — nunca se guarda un diagnóstico "huérfano" sin
        # saber quién lo hizo.
        campos[campo_responsable] = mecanico["nombre"]

        actualizada, error = _actualizar_campos_orden_por_agente(db, ot_id, campos)
        if error:
            return resultado(ot_id, False, error)

        campos_legibles = ", ".join(campos_agente[c] for c in campos if c in campos_agente)
        return resultado(ot_id, True, f"Listo — actualicé el {etiqueta_accion} de la OT #{ot_id} ({campos_legibles}). ✅")

    if tipo == "cambiar_estado_ot":
        ot_id = accion.get("ot_id")
        estado_destino = accion.get("estado_destino")
        if not isinstance(ot_id, int) or estado_destino not in ESTADOS_ORDEN:
            return resultado(
                ot_id if isinstance(ot_id, int) else None, False,
                "No pude identificar con claridad el número de OT o el estado de destino en tu mensaje.",
            )

        # El número que manda el mecánico es el de Talca, no la llave interna.
        orden = _agente_resolver_orden(db, ot_id)
        if orden is None:
            return resultado(ot_id, False, f"No encontré ninguna OT con el número {ot_id}.")

        estado_actual = orden["estado"]
        permitidos = TRANSICIONES_AGENTE.get(estado_actual, set())
        if estado_destino not in permitidos:
            if not permitidos:
                detalle = f'la OT #{ot_id} está en estado "{ESTADOS_LABEL[estado_actual]}", que ya no admite cambios por este medio.'
            else:
                opciones = ", ".join(f'"{ESTADOS_LABEL[e]}"' for e in sorted(permitidos))
                detalle = (
                    f'la OT #{ot_id} está en estado "{ESTADOS_LABEL[estado_actual]}" — desde ahí solo se puede '
                    f"pasar a: {opciones}."
                )
            return resultado(
                ot_id, False,
                f"No pude aplicar ese cambio: {detalle} Si de verdad corresponde, hazlo manualmente desde el sistema.",
                estado_anterior=estado_actual, codigo="transicion_no_permitida",
            )

        actualizada, error = _actualizar_estado_orden_por_agente(db, ot_id, estado_destino)
        if error:
            return resultado(ot_id, False, error, estado_anterior=estado_actual)

        return resultado(
            ot_id, True,
            f'Listo — la OT #{ot_id} pasó de "{ESTADOS_LABEL[estado_actual]}" a "{ESTADOS_LABEL[estado_destino]}". ✅',
            estado_anterior=estado_actual, estado_nuevo=estado_destino, codigo="cambio_estado_ot",
        )

    # tipo == "no_reconocida" (o cualquier valor inesperado — se trata igual)
    return resultado(
        accion.get("ot_id") if isinstance(accion.get("ot_id"), int) else None, False,
        "No pude identificar una acción concreta en esa parte de tu mensaje.", codigo="no_reconocida",
    )


def _agente_resolver_orden(db, numero):
    """La orden a la que se refiere el número que escribió el mecánico.

    **Acá el riesgo no es visual, es de escritura cruzada.** El mecánico
    escribe el número que tiene a la vista, que es el de Talca. Si esto
    buscara por `ordenes_trabajo.id`, un "OT 8112" podría caer sobre la orden
    interna 8112, que es el auto de otra persona — y el agente escribe
    diagnósticos y cambia estados. De las 7.116 órdenes importadas hay 6.117
    números que existen en las dos series apuntando a órdenes distintas.

    Por eso la búsqueda es **estricta por `talca_id`**. El único caso en que
    se mira el id interno es el de las órdenes creadas acá que todavía no
    tienen número de Talca: se muestran como `L-<id>` y no hay ambigüedad
    posible, porque ninguna de ellas tiene talca_id que pueda chocar.

    Devuelve la fila o None."""
    if not isinstance(numero, int):
        return None
    orden = db.execute(
        "SELECT * FROM ordenes_trabajo WHERE talca_id = ?", (numero,)
    ).fetchone()
    if orden is not None:
        return orden
    return db.execute(
        "SELECT * FROM ordenes_trabajo WHERE id = ? AND talca_id IS NULL", (numero,)
    ).fetchone()


def _agente_procesar_mensaje(db, telefono_crudo, texto_mensaje, origen="texto", imagen_bytes=None, imagen_mime=None):
    """Punto único de entrada para CUALQUIER mensaje que le llegue al
    agente, venga de /api/agente/simular-mensaje o /api/agente/simular-
    mensaje-voz (pruebas manuales desde el navegador) o de
    /webhook/whatsapp (WhatsApp real, vía Twilio). Siempre deja un
    registro en agente_mensajes — incluso cuando no se pudo hacer nada —
    para poder auditar después qué pasó con cada mensaje.

    Un mismo mensaje puede traer VARIAS acciones (ver
    _llamar_claude_interpretar_mensaje_agente) — ej. "diagnóstico X, pásala
    a reparación" trae 'actualizar_diagnostico' y 'cambiar_estado_ot' a la
    vez. Cada una se ejecuta con _agente_ejecutar_una_accion() y se
    consolida en UN solo registro de auditoría por mensaje, con la
    respuesta de todas juntas.

    'origen' es 'texto', 'voz', 'foto_factura' o 'foto_vehiculo', solo para
    dejar registro de cómo llegó el mensaje (si vino de una nota de voz,
    'texto_mensaje' ya es la transcripción — el resto de la lógica es
    idéntica sea cual sea el origen). Si 'imagen_bytes' viene con datos,
    primero se clasifica la foto (ver
    _llamar_claude_vision_clasificar_foto_whatsapp): si es una factura, se
    procesa como tal (ver _procesar_foto_factura_whatsapp) y ahí termina.
    Si es un vehículo, se resuelve la OT (por número explícito en el
    mensaje, ej. "OT 3321", o si no por la patente que lee la IA de la
    foto) y la foto se ARCHIVA de una vez en la carpeta de fotos de esa OT
    (fotos_vehiculo, origen='desarme_armado' -- así se distingue en la
    pestaña "Fotos" de las fotos obligatorias que se toman al ingresar el
    auto). Esto es lo que permite que un mecánico, que solo tiene acceso al
    WhatsApp y no al sistema, mande una foto de un daño encontrado durante
    el desarme/armado (ej. un cable cortado) con la OT o patente en el pie
    de foto, y quede guardada automáticamente sin que nadie tenga que
    entrar al sistema a subirla a mano. Si además el mensaje trae una
    acción de texto (ej. "OT 3321 listo para probar"), esa parte se sigue
    procesando con el intérprete de texto de más abajo, igual que antes."""
    telefono = _normalizar_telefono(telefono_crudo)
    texto_mensaje = (texto_mensaje or "").strip()
    ts = now_iso()
    nota_foto_archivada = None
    ot_id_foto_archivada = None

    def _registrar(mecanico_nombre, accion, ot_id, estado_anterior, estado_nuevo, exito, respuesta_texto):
        db.execute(
            """
            INSERT INTO agente_mensajes
                (telefono, mecanico_nombre, mensaje_texto, accion, ot_id,
                 estado_anterior, estado_nuevo, exito, respuesta_texto, origen, creado_en)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telefono, mecanico_nombre, texto_mensaje, accion, ot_id,
             estado_anterior, estado_nuevo, 1 if exito else 0, respuesta_texto, origen, ts),
        )
        db.commit()
        return {
            "telefono": telefono,
            "mecanico_nombre": mecanico_nombre,
            "accion": accion,
            "ot_id": ot_id,
            "estado_anterior": estado_anterior,
            "estado_nuevo": estado_nuevo,
            "exito": exito,
            "respuesta_texto": respuesta_texto,
            "origen": origen,
        }

    mecanico = db.execute(
        "SELECT * FROM agente_mecanicos WHERE telefono = ? AND activo = 1", (telefono,)
    ).fetchone()
    if not mecanico:
        return _registrar(
            "", "no_autorizado", None, "", "", False,
            "Este número no está registrado como mecánico en el sistema. Pide que te agreguen desde "
            "\"Agente IA\" en Logística.",
        )

    # Perfil restringido (ver migración 16/21/30, columna 'tipo' de
    # agente_mecanicos): un mecánico 'mecanico', 'probador_de_autos' o
    # 'encargado_pre_entrega' puede mandar fotos, que se archivan
    # exactamente igual que a cualquier otro mecánico, pero NO tiene
    # permiso para cambiar diagnóstico/estado de la OT por texto. Lo que
    # cambia entre los tres perfiles restringidos es DÓNDE queda el
    # comentario: 'mecanico' -> sugerencia de diagnóstico (para que
    # operativo la revise y decida); 'probador_de_autos' -> una entrada
    # nueva en la bitácora de pruebas de ruta; 'encargado_pre_entrega' ->
    # una entrada nueva en la bitácora de Pre entrega (los dos últimos
    # quedan directo, sin revisión, porque no son un diagnóstico, son un
    # registro de lo que se hizo). El permiso de escribir
    # diagnóstico/estado sigue existiendo tal cual para un mecánico de
    # tipo 'completo'.
    tipo_mecanico = mecanico["tipo"] if "tipo" in mecanico.keys() else "completo"
    es_probador_de_autos = tipo_mecanico == "probador_de_autos"
    es_encargado_pre_entrega = tipo_mecanico == "encargado_pre_entrega"
    es_mecanico_restringido = tipo_mecanico in ("mecanico", "probador_de_autos", "encargado_pre_entrega")

    if imagen_bytes is not None:
        try:
            clasificacion = _llamar_claude_vision_clasificar_foto_whatsapp(imagen_bytes, imagen_mime)
        except RuntimeError as e:
            return _registrar(mecanico["nombre"], "error", None, "", "", False, f"No pude leer la foto: {e}")

        tipo_foto = clasificacion.get("tipo")

        if tipo_foto == "factura":
            try:
                resultado_factura = _procesar_foto_factura_whatsapp(db, telefono, mecanico["nombre"], imagen_bytes, imagen_mime)
            except RuntimeError as e:
                return _registrar(mecanico["nombre"], "error", None, "", "", False, f"No pude leer la factura: {e}")
            return _registrar(mecanico["nombre"], "factura_recibida", None, "", "", True, resultado_factura["mensaje"])

        if tipo_foto != "factura":
            # A propósito NO se exige tipo_foto == "vehiculo": un mecánico
            # también manda fotos de una pieza suelta (ej. un radiador de
            # aceite arriba de un paño, un cable cortado) que la IA
            # clasifica como 'otro' porque no se ve un auto completo -- eso
            # es exactamente el caso de uso principal de este archivado
            # (ver spec del usuario: "un mecánico manda una foto de un daño
            # y pone 'ot 3321'"). Mientras el mensaje traiga un número de OT
            # explícito, se archiva igual sea cual sea la clasificación;
            # 'vehiculo' solo aporta la patente como alternativa para
            # cuando no se escribió el número.
            origen = "foto_vehiculo"
            patente = clasificacion.get("patente") or ""

            ordenes_abiertas_foto = _agente_listar_ordenes_abiertas(db)
            match_ot_numero = re.search(r"\bot\s*#?\s*(\d+)\b", texto_mensaje, re.IGNORECASE)
            ot_resuelta = None
            if match_ot_numero:
                ot_id_deseado = int(match_ot_numero.group(1))
                ot_resuelta = next((o for o in ordenes_abiertas_foto if o["ot_id"] == ot_id_deseado), None)
                if ot_resuelta is None:
                    return _registrar(
                        mecanico["nombre"], "foto_vehiculo_ot_no_encontrada", None, "", "", False,
                        f"Recibí la foto, pero no encontré ninguna OT abierta con el número {ot_id_deseado}. "
                        f"Revisa el número, o manda la foto de nuevo para que lea la patente.",
                    )
            elif patente:
                patente_norm = _normalizar_patente(patente)
                ot_resuelta = next(
                    (o for o in ordenes_abiertas_foto
                     if o.get("patente") and _normalizar_patente(o["patente"]) == patente_norm),
                    None,
                )

            if ot_resuelta is None:
                if not patente:
                    return _registrar(
                        mecanico["nombre"], "foto_vehiculo_sin_patente", None, "", "", False,
                        "Recibí la foto, pero no encontré ningún número de OT en el mensaje ni logré leer una "
                        "patente en la foto. Escribe el número de OT (ej. \"OT 3321\") junto con la foto.",
                    )
                return _registrar(
                    mecanico["nombre"], "foto_vehiculo_ot_no_encontrada", None, "", "", False,
                    f"Recibí la foto (patente {patente}), pero no encontré ninguna OT abierta para ese "
                    f"vehículo. Escribe el número de OT junto con la foto.",
                )

            # Se archiva de una vez, tenga o no el mensaje una acción de
            # texto además -- así el mecánico no tiene que mandar nada más
            # que la foto con la OT/patente para que quede guardada.
            # 'origen="desarme_armado"' es lo que separa estas fotos de las
            # obligatorias de ingreso en la pestaña "Fotos" de la OT (ver
            # modulos/operativo.py, _renderFotosGrid en operativo.js).
            titulo_foto = texto_mensaje.strip() or "Foto enviada por WhatsApp"
            try:
                foto_archivada = _guardar_foto_entidad(
                    db, "orden", ot_resuelta["ot_id"], "libre", titulo_foto, imagen_bytes,
                    mecanico["nombre"], origen="desarme_armado",
                )
                nota_foto_archivada = (
                    f"📷 Foto archivada en la OT #{ot_resuelta['ot_id']} "
                    f"({ot_resuelta['cliente_nombre']} — {ot_resuelta.get('patente') or 'sin patente'})."
                )
                ot_id_foto_archivada = ot_resuelta["ot_id"]
            except ValueError as e:
                return _registrar(
                    mecanico["nombre"], "error", ot_resuelta["ot_id"], "", "", False,
                    f"Encontré la OT #{ot_resuelta['ot_id']}, pero no pude guardar la foto: {e}",
                )

            # El resto del mensaje (sacando la referencia al número de OT,
            # si la traía) puede tener además una acción normal de texto
            # (ej. "OT 3321 listo para probar") -- si no queda nada más que
            # decir, se corta acá con la confirmación del archivado; si
            # queda algo, sigue el camino normal de interpretación de texto
            # de más abajo, reutilizando la misma lógica que ya sabe
            # matchear por patente/descripción (ver 'ordenes_abiertas').
            texto_resto = texto_mensaje
            if match_ot_numero:
                texto_resto = (texto_resto[:match_ot_numero.start()] + texto_resto[match_ot_numero.end():]).strip()
            if not texto_resto:
                return _registrar(
                    mecanico["nombre"], "foto_vehiculo_archivada", ot_resuelta["ot_id"], "", "", True,
                    nota_foto_archivada,
                )

            if es_probador_de_autos:
                # Perfil "probador de autos": la foto ya quedó archivada
                # arriba (eso NO cambia para nadie), y el comentario que la
                # acompaña se guarda directo como una entrada nueva en la
                # bitácora de pruebas de ruta, vinculada a la OT y a la foto
                # recién archivada.
                _agente_guardar_prueba_ruta(
                    db, ot_resuelta["ot_id"], foto_archivada["id"], mecanico["nombre"], texto_resto,
                )
                return _registrar(
                    mecanico["nombre"], "prueba_ruta_registrada", ot_resuelta["ot_id"], "", "", True,
                    f"{nota_foto_archivada}\n🚗 Dejé tu prueba de ruta (\"{texto_resto}\") registrada en la "
                    f"bitácora de la OT #{ot_resuelta['ot_id']}.",
                )

            if es_encargado_pre_entrega:
                # Perfil "encargado pre entrega": la foto ya quedó archivada
                # arriba (eso NO cambia para nadie), y el comentario que la
                # acompaña se guarda directo como una entrada nueva en la
                # bitácora de Pre entrega, vinculada a la OT y a la foto
                # recién archivada. Además, si el texto confirma
                # EXPLÍCITAMENTE que el vehículo quedó listo para la
                # entrega, la OT se mueve a "listo" -- y SOLO a ese estado
                # (ver _agente_pre_entrega_intentar_marcar_listo).
                _agente_guardar_pre_entrega(
                    db, ot_resuelta["ot_id"], foto_archivada["id"], mecanico["nombre"], texto_resto,
                )
                estado_previo = ot_resuelta["estado"]
                mensaje_estado = _agente_pre_entrega_intentar_marcar_listo(db, ot_resuelta, texto_resto)
                estado_quedo = "listo" if (estado_previo != "listo" and "🚚" in mensaje_estado) else ""
                return _registrar(
                    mecanico["nombre"], "pre_entrega_registrada", ot_resuelta["ot_id"],
                    estado_previo, estado_quedo, True,
                    f"{nota_foto_archivada}\n✅ Dejé tu registro de pre entrega (\"{texto_resto}\") en la "
                    f"bitácora de la OT #{ot_resuelta['ot_id']}.{mensaje_estado}",
                )

            if es_mecanico_restringido:
                # Perfil restringido: la foto ya quedó archivada arriba (eso
                # NO cambia para nadie), pero el comentario que la acompaña
                # no se interpreta como una acción de diagnóstico/estado --
                # este perfil no tiene ese permiso. Queda como sugerencia,
                # vinculada tanto a la OT como a la foto recién archivada.
                _agente_guardar_sugerencia_diagnostico(
                    db, ot_resuelta["ot_id"], foto_archivada["id"], mecanico["nombre"], texto_resto,
                )
                return _registrar(
                    mecanico["nombre"], "foto_vehiculo_archivada_con_sugerencia", ot_resuelta["ot_id"], "", "", True,
                    f"{nota_foto_archivada}\n💡 Dejé tu comentario (\"{texto_resto}\") como sugerencia de "
                    f"diagnóstico en la OT #{ot_resuelta['ot_id']} para que la parte operativa lo revise.",
                )

            texto_mensaje = f"{texto_resto} (OT identificada por la foto adjunta: OT #{ot_resuelta['ot_id']})"
        # No queda ningún 'else' acá: con tipo_foto != "factura" ya cubierto
        # arriba (y "factura" resuelto en el primer if, que siempre
        # retorna), toda foto que llega por WhatsApp pasa por la lógica de
        # archivado -- no existe ya un tercer camino "no reconocida" para
        # fotos (si acaso no se puede resolver la OT, ver los 'return'
        # dentro de la rama de arriba).

    if not texto_mensaje:
        return _registrar("", "error", None, "", "", False, "El mensaje llegó vacío.")

    if es_mecanico_restringido:
        # Mensaje de puro texto (sin foto) de un mecánico restringido: no
        # tiene permiso para ejecutar acciones de diagnóstico/estado, así
        # que en vez de interpretarlo como una acción se intenta guardar
        # como sugerencia o como prueba de ruta (según el perfil) -- para
        # eso sí o sí necesita traer un número de OT O una patente que ya
        # esté registrada en alguna OT abierta (ver
        # _agente_resolver_ot_por_patente_en_texto -- sin foto no hay OCR,
        # pero si el mecánico escribe la patente tal cual, igual se puede
        # resolver sola sin pedirle el número de memoria).
        ordenes_abiertas_texto = _agente_listar_ordenes_abiertas(db)
        match_ot_numero_texto = re.search(r"\bot\s*#?\s*(\d+)\b", texto_mensaje, re.IGNORECASE)
        ot_para_sugerencia = None
        if match_ot_numero_texto:
            ot_id_deseado_texto = int(match_ot_numero_texto.group(1))
            ot_para_sugerencia = next(
                (o for o in ordenes_abiertas_texto if o["ot_id"] == ot_id_deseado_texto), None
            )
        if ot_para_sugerencia is None:
            ot_para_sugerencia = _agente_resolver_ot_por_patente_en_texto(texto_mensaje, ordenes_abiertas_texto)
        if ot_para_sugerencia is None:
            if es_probador_de_autos:
                mensaje_perfil = "Tu perfil solo puede registrar pruebas de ruta, no cambiar el diagnóstico directamente."
            elif es_encargado_pre_entrega:
                mensaje_perfil = "Tu perfil solo puede registrar la pre entrega, no cambiar el diagnóstico directamente."
            else:
                mensaje_perfil = (
                    "Tu perfil solo puede mandar fotos y comentarios para la OT, no cambiar el diagnóstico "
                    "directamente."
                )
            return _registrar(
                mecanico["nombre"], "sugerencia_sin_ot", None, "", "", False,
                f"{mensaje_perfil} Indica el número de OT (ej. \"OT 3321\") o la patente del vehículo junto "
                f"con tu comentario para que quede registrado.",
            )
        if es_probador_de_autos:
            _agente_guardar_prueba_ruta(
                db, ot_para_sugerencia["ot_id"], None, mecanico["nombre"], texto_mensaje,
            )
            return _registrar(
                mecanico["nombre"], "prueba_ruta_registrada", ot_para_sugerencia["ot_id"], "", "", True,
                f"🚗 Dejé tu prueba de ruta registrada en la bitácora de la OT #{ot_para_sugerencia['ot_id']}.",
            )
        if es_encargado_pre_entrega:
            _agente_guardar_pre_entrega(
                db, ot_para_sugerencia["ot_id"], None, mecanico["nombre"], texto_mensaje,
            )
            estado_previo = ot_para_sugerencia["estado"]
            mensaje_estado = _agente_pre_entrega_intentar_marcar_listo(db, ot_para_sugerencia, texto_mensaje)
            estado_quedo = "listo" if (estado_previo != "listo" and "🚚" in mensaje_estado) else ""
            return _registrar(
                mecanico["nombre"], "pre_entrega_registrada", ot_para_sugerencia["ot_id"],
                estado_previo, estado_quedo, True,
                f"✅ Dejé tu registro de pre entrega en la bitácora de la OT #{ot_para_sugerencia['ot_id']}."
                f"{mensaje_estado}",
            )
        _agente_guardar_sugerencia_diagnostico(
            db, ot_para_sugerencia["ot_id"], None, mecanico["nombre"], texto_mensaje,
        )
        return _registrar(
            mecanico["nombre"], "sugerencia_registrada", ot_para_sugerencia["ot_id"], "", "", True,
            f"💡 Dejé tu comentario como sugerencia de diagnóstico en la OT #{ot_para_sugerencia['ot_id']} "
            f"para que la parte operativa lo revise.",
        )

    try:
        ordenes_abiertas = _agente_listar_ordenes_abiertas(db)
        interpretado = _llamar_claude_interpretar_mensaje_agente(texto_mensaje, ordenes_abiertas)
    except RuntimeError as e:
        return _registrar(mecanico["nombre"], "error", None, "", "", False, f"No pude leer tu mensaje: {e}")

    acciones = interpretado.get("acciones")
    if not isinstance(acciones, list) or not acciones:
        acciones = [{"tipo": "no_reconocida"}]
    # Tope defensivo — además de estar acotado en el propio schema
    # (maxItems) que se le manda a la IA, se corta acá también por si
    # algún día cambia el modelo/schema y llegara a devolver más.
    acciones = acciones[:MAX_ACCIONES_POR_MENSAJE]

    # Las actualizaciones de diagnóstico/prueba de ruta se aplican ANTES
    # que el cambio de estado: así, si el mensaje además mueve la OT a un
    # estado cerrado (entregado/cancelado), el diagnóstico ya alcanzó a
    # quedar guardado en vez de perderse por el orden de ejecución.
    orden_prioridad = {"actualizar_diagnostico": 0, "actualizar_prueba_ruta": 0, "cambiar_estado_ot": 1, "no_reconocida": 2}
    acciones_ordenadas = sorted(acciones, key=lambda a: orden_prioridad.get(a.get("tipo"), 1))

    if all(a.get("tipo") == "no_reconocida" for a in acciones_ordenadas):
        motivo = interpretado.get("resumen") or "no reconocí una acción concreta en tu mensaje"
        mensaje_no_reconocida = (
            f"No pude identificar una acción clara ({motivo}). Si querías mover una OT o contar un "
            f"diagnóstico, indica el número (ej. \"OT 4012\") y qué pasó."
        )
        if nota_foto_archivada:
            # La foto sí se archivó (ver rama 'vehiculo' más arriba) aunque
            # el texto que la acompañaba no se haya reconocido como una
            # acción -- esto NO es un error para el mecánico, así que se
            # reporta como éxito con las dos noticias juntas.
            return _registrar(
                mecanico["nombre"], "foto_vehiculo_archivada", ot_id_foto_archivada, "", "", True,
                f"{nota_foto_archivada}\n{mensaje_no_reconocida}",
            )
        return _registrar(
            mecanico["nombre"], "no_reconocida", None, "", "", False,
            mensaje_no_reconocida,
        )

    resultados = [_agente_ejecutar_una_accion(db, mecanico, a) for a in acciones_ordenadas]

    # Consolidación en UN registro de auditoría por mensaje (no uno por
    # acción): más fácil de leer en el historial, y el mecánico recibe una
    # sola respuesta de WhatsApp con todo lo que pasó, en el mismo orden en
    # que se ejecutó.
    exito_general = any(r["exito"] for r in resultados)
    respuesta_texto = "\n".join(r["mensaje"] for r in resultados)
    if nota_foto_archivada:
        respuesta_texto = f"{nota_foto_archivada}\n{respuesta_texto}"
        exito_general = True  # la foto ya quedó archivada aunque la acción de texto fallara
    accion_registro = "+".join(dict.fromkeys(r["codigo"] for r in resultados))
    if nota_foto_archivada:
        accion_registro = f"foto_vehiculo_archivada+{accion_registro}"
    ot_id_registro = next((r["ot_id"] for r in resultados if r["ot_id"] is not None), None) or ot_id_foto_archivada
    cambio_estado = next((r for r in resultados if r["tipo"] == "cambiar_estado_ot" and r["exito"]), None)
    estado_anterior = cambio_estado["estado_anterior"] if cambio_estado else ""
    estado_nuevo = cambio_estado["estado_nuevo"] if cambio_estado else ""

    return _registrar(
        mecanico["nombre"], accion_registro, ot_id_registro, estado_anterior, estado_nuevo,
        exito_general, respuesta_texto,
    )


# ---------------------------------------------------------------------------
# Adaptadores de WhatsApp: lo único que cambia entre Twilio y Meta es el
# transporte (firma, lectura del mensaje, descarga de media y entrega).
# ---------------------------------------------------------------------------

def _wsp_media_tipo(mime):
    if (mime or "").startswith("image/"):
        return "imagen"
    if (mime or "").startswith("audio/"):
        return "audio"
    return "otro" if mime else ""


# -- Twilio -----------------------------------------------------------------
# Todo esto es el código que ya funcionaba, movido detrás de la interfaz. No
# cambió su comportamiento: con WHATSAPP_PROVEEDOR sin definir (o en 'twilio')
# el webhook se comporta exactamente como antes.

def _twilio_extraer_mensaje(req):
    num_media = int(req.form.get("NumMedia", "0") or "0")
    media = None
    mime = ""
    if num_media > 0:
        mime = req.form.get("MediaContentType0", "")
        url = req.form.get("MediaUrl0", "")
        if url:
            media = {"url": url, "mime": mime}
    return {
        "telefono": req.form.get("From", ""),
        "texto": req.form.get("Body", ""),
        "media": media,
        "media_tipo": _wsp_media_tipo(mime),
    }


def _twilio_descargar_media(ref):
    """Basic Auth con el par SID/token. Distinto de Meta, que usa Bearer y
    necesita dos pasos."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError(
            "el servidor todavía no tiene configuradas las credenciales de Twilio "
            "(TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN) para poder descargarla"
        )
    try:
        resp = requests.get(ref["url"], auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc
    return resp.content, ref["mime"]


def _twilio_entregar(telefono, texto):
    """En Twilio la respuesta HTTP ES la entrega: el TwiML que se devuelve es
    lo que el mecánico recibe por WhatsApp. Por eso `telefono` no se usa."""
    cuerpo = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{html.escape(texto)}</Message></Response>"
    )
    return cuerpo, 200, {"Content-Type": "text/xml; charset=utf-8"}


# -- Meta (Cloud API) --------------------------------------------------------

def _meta_validar_firma(req):
    """HMAC-SHA256 del App Secret sobre el CUERPO CRUDO, comparado contra
    X-Hub-Signature-256 (que viene con prefijo 'sha256=').

    Nada que ver con el esquema de Twilio, que firma la URL más los
    parámetros. Acá la URL no participa, así que ProxyFix no influye.

    El cuerpo se toma con get_data(cache=True) **antes** de cualquier parseo:
    Flask lo cachea, así que get_json() más adelante sigue funcionando."""
    cabecera = req.headers.get("X-Hub-Signature-256", "")
    cuerpo = req.get_data(cache=True)
    if not cabecera.startswith("sha256="):
        logger.warning(
            "Webhook de WhatsApp (Meta) rechazado: falta X-Hub-Signature-256 "
            "o viene sin el prefijo 'sha256=' (recibido=%r, bytes=%d)",
            cabecera[:20], len(cuerpo),
        )
        return False
    esperada = hmac.new(
        META_WA_APP_SECRET.encode("utf-8"), cuerpo, hashlib.sha256
    ).hexdigest()
    if hmac.compare_digest(esperada, cabecera[len("sha256="):]):
        return True
    logger.warning(
        "Webhook de WhatsApp (Meta) rechazado: la firma no coincide.\n"
        "  bytes del cuerpo   : %d\n"
        "  firma recibida     : %s…\n"
        "  firma calculada    : %s…\n"
        "  Se firma el CUERPO CRUDO con META_WA_APP_SECRET (no la URL, no el "
        "access token). Si el largo de arriba es 0, algo consumió el body antes.",
        len(cuerpo), cabecera[7:19], esperada[:12],
    )
    return False


def _meta_extraer_mensaje(req):
    """Navega entry[0].changes[0].value.messages[0].

    Devuelve None —o sea "ignorar, responder 200"— para todo lo que no sea un
    mensaje de un usuario: los eventos de estado de entrega traen `statuses`
    en vez de `messages`, y Meta va a seguir agregando tipos. Cada nivel se
    lee defensivamente: un payload con otra forma no puede reventar el
    webhook."""
    datos = req.get_json(silent=True) or {}
    try:
        valor = datos["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None
    mensajes = valor.get("messages")
    if not mensajes:
        return None                      # statuses, o un evento sin mensajes
    msg = mensajes[0]

    tipo = msg.get("type", "")
    texto = ""
    media = None
    mime = ""
    if tipo == "text":
        texto = (msg.get("text") or {}).get("body", "")
    elif tipo in ("image", "audio", "video", "document", "voice"):
        bloque = msg.get(tipo) or {}
        mime = bloque.get("mime_type", "")
        if bloque.get("id"):
            media = {"id": bloque["id"], "mime": mime}
        # Una imagen puede venir con pie de foto; se usa como el texto.
        texto = bloque.get("caption", "") or ""

    return {
        # Meta manda el número SIN '+' ('56946596850'). _normalizar_telefono
        # se queda solo con los dígitos, así que calza igual que el de Twilio.
        "telefono": msg.get("from", ""),
        "texto": texto,
        "media": media,
        "media_tipo": _wsp_media_tipo(mime),
    }


def _meta_descargar_media(ref):
    """Dos pasos, como exige la Cloud API:

      1. GET /{version}/{media-id}  -> devuelve `url` y `mime_type`
      2. GET esa url                -> los bytes

    **Las dos llamadas necesitan el Bearer**; sin él la segunda falla, que es
    la diferencia más fácil de pasar por alto respecto del Basic Auth de
    Twilio. Y la url del paso 1 **vence a los 5 minutos**, por eso se pide y
    se usa en el mismo request en vez de guardarla."""
    if not META_WA_ACCESS_TOKEN:
        raise RuntimeError(
            "el servidor todavía no tiene configurado META_WA_ACCESS_TOKEN para "
            "poder descargarla"
        )
    cabeceras = {"Authorization": f"Bearer {META_WA_ACCESS_TOKEN}"}
    try:
        meta_resp = requests.get(
            f"{META_WA_GRAPH}/{META_WA_API_VERSION}/{ref['id']}",
            headers=cabeceras, timeout=30,
        )
        meta_resp.raise_for_status()
        datos = meta_resp.json()
        url = datos.get("url")
        if not url:
            raise RuntimeError(f"la Graph API no devolvió url para el media {ref['id']}")
        binario = requests.get(url, headers=cabeceras, timeout=60)
        binario.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc
    return binario.content, datos.get("mime_type") or ref.get("mime") or ""


def _meta_entregar(telefono, texto):
    """En Meta la respuesta al webhook NO llega al usuario: hay que devolver
    200 de inmediato y mandar el mensaje por separado a la Graph API.

    El envío es **best-effort**: si falla, se loguea y se devuelve 200 igual.
    Un no-200 haría que Meta reintente el webhook en loop, que es peor que
    perder una respuesta -- el trabajo pedido por el mecánico ya se aplicó
    antes de llegar acá."""
    if not META_WA_ACCESS_TOKEN or not META_WA_PHONE_NUMBER_ID:
        logger.error(
            "No se pudo responder por WhatsApp: faltan META_WA_ACCESS_TOKEN o "
            "META_WA_PHONE_NUMBER_ID. El mensaje SÍ se procesó."
        )
        return "", 200
    try:
        resp = requests.post(
            f"{META_WA_GRAPH}/{META_WA_API_VERSION}/{META_WA_PHONE_NUMBER_ID}/messages",
            headers={
                "Authorization": f"Bearer {META_WA_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": _normalizar_telefono(telefono),
                "type": "text",
                "text": {"preview_url": False, "body": texto},
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.error(
                "La Graph API rechazó la respuesta a %s: HTTP %s %s",
                _normalizar_telefono(telefono), resp.status_code, (resp.text or "")[:300],
            )
    except requests.RequestException as exc:
        logger.error("No se pudo entregar la respuesta por la Graph API: %s", exc)
    return "", 200


WHATSAPP_ADAPTADORES = {
    # 'secreto' es lo que permite verificar que el request viene de la
    # plataforma. SIN él el webhook NO procesa nada (responde 503): sin firma
    # cualquiera podría mandar un POST haciéndose pasar por un mecánico
    # registrado y el agente actuaría sobre eso (hallazgo #15 del informe de
    # revisión). La versión anterior aceptaba el tráfico sin validar cuando
    # faltaba el secreto; eso NO se trajo de vuelta.
    "twilio": {
        "secreto": lambda: TWILIO_AUTH_TOKEN,
        "falta_secreto": "TWILIO_AUTH_TOKEN",
        "validar_firma": lambda req: _validar_firma_twilio(req),
        "extraer_mensaje": _twilio_extraer_mensaje,
        "descargar_media": _twilio_descargar_media,
        "entregar": _twilio_entregar,
    },
    "meta": {
        "secreto": lambda: META_WA_APP_SECRET,
        "falta_secreto": "META_WA_APP_SECRET",
        "validar_firma": _meta_validar_firma,
        "extraer_mensaje": _meta_extraer_mensaje,
        "descargar_media": _meta_descargar_media,
        "entregar": _meta_entregar,
    },
}


def _validar_firma_twilio(req):
    """Valida que un request al webhook realmente venga de Twilio, según
    su algoritmo documentado: HMAC-SHA1 del AUTH_TOKEN sobre la URL
    completa + cada parámetro del POST (ordenado alfabéticamente),
    codificado en base64, comparado contra el header X-Twilio-Signature.
    Solo se llama si TWILIO_AUTH_TOKEN está configurado (ver más arriba).

    **`req.url` tiene que ser la URL que vio Twilio, no la que ve Flask.**
    Detrás del proxy de Railway, Flask recibe la petición como `http://` y
    con el host interno; Twilio firmó la `https://` pública. Si los dos
    strings difieren, el HMAC no coincide nunca y el webhook rechaza TODO el
    tráfico legítimo con 403.

    Lo que lo hace funcionar es el `ProxyFix` que se aplica sobre
    `app.wsgi_app` (ver más arriba, cerca del manejador de errores): lee
    `X-Forwarded-Proto` y `X-Forwarded-Host` y reconstruye `request.url` con
    el esquema y host originales. **No es opcional para este endpoint**: si
    alguien lo saca, esta función deja de validar nada correcto.

    `TestFirmaTwilio` cubre justamente eso, con una firma calculada a mano
    sobre la URL https y mandada en una petición http con los headers del
    proxy."""
    firma_recibida = req.headers.get("X-Twilio-Signature", "")
    if not firma_recibida:
        logger.warning(
            "Webhook de WhatsApp rechazado: no vino el header X-Twilio-Signature "
            "(url=%s, origen=%s)", req.url, req.remote_addr,
        )
        return False
    base = req.url
    for clave in sorted(req.form.keys()):
        base += clave + req.form[clave]
    firma_calculada = base64.b64encode(
        hmac.new(TWILIO_AUTH_TOKEN.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    if hmac.compare_digest(firma_calculada, firma_recibida):
        return True

    # Sin esto el fallo es invisible: la respuesta dice "no se pudo verificar"
    # y en el log no queda POR QUÉ. La URL es el dato que importa —si aparece
    # http:// o un host interno, el problema es el proxy, no la credencial— y
    # por eso se registra junto con los headers que la determinan.
    #
    # Se loguean prefijos de las firmas, no completas: no son secretas, pero
    # tampoco hay motivo para dejarlas enteras en un log. El AUTH_TOKEN nunca
    # se registra.
    logger.warning(
        "Webhook de WhatsApp rechazado: la firma no coincide.\n"
        "  URL usada para el HMAC : %s\n"
        "  X-Forwarded-Proto      : %s\n"
        "  X-Forwarded-Host       : %s\n"
        "  parámetros firmados    : %s\n"
        "  firma recibida         : %s…\n"
        "  firma calculada        : %s…\n"
        "  Si la URL de arriba no es la https pública que Twilio tiene "
        "configurada, el problema es el proxy (ver ProxyFix), no el token.",
        req.url,
        req.headers.get("X-Forwarded-Proto", "(ausente)"),
        req.headers.get("X-Forwarded-Host", "(ausente)"),
        sorted(req.form.keys()),
        firma_recibida[:12], firma_calculada[:12],
    )
    return False


# Perfiles válidos para un mecánico registrado en el Agente de WhatsApp:
#   - 'completo': puede cambiar estado/diagnóstico de la OT por WhatsApp
#     (ver _agente_ejecutar_una_accion).
#   - 'mecanico': perfil restringido -- lo que manda queda como SUGERENCIA
#     de diagnóstico para que operativo la revise (migración 16).
#   - 'probador_de_autos': perfil restringido -- tampoco puede cambiar
#     estado/diagnóstico, pero a diferencia de 'mecanico' lo que manda se
#     guarda directo como una entrada nueva en la bitácora de pruebas de
#     ruta de la OT (tabla pruebas_ruta, migración 20/21), no como
#     sugerencia de diagnóstico.
#   - 'encargado_pre_entrega': perfil restringido -- misma idea que
#     'probador_de_autos', pero lo que manda se guarda en la bitácora de
#     Pre entrega de la OT (tabla pre_entrega, migración 30), pensada para
#     dejar registrada la última revisión antes de entregarle el vehículo
#     al cliente.
TIPOS_MECANICO_VALIDOS = ("completo", "mecanico", "probador_de_autos", "encargado_pre_entrega")


@app.route("/api/agente/mecanicos", methods=["GET"])
@requiere_rol("operativo")
def listar_mecanicos_agente():
    db = get_db()
    filas = db.execute("SELECT * FROM agente_mecanicos ORDER BY nombre").fetchall()
    return jsonify([dict(r) for r in filas])


@app.route("/api/agente/mecanicos", methods=["POST"])
@requiere_rol("operativo")
def crear_mecanico_agente():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    telefono = _normalizar_telefono(data.get("telefono"))
    if not nombre:
        return jsonify({"error": "Falta el nombre del mecánico"}), 400
    if len(telefono.lstrip("+")) < 8:
        return jsonify({"error": "El teléfono no parece válido. Inclúyelo con código de país, ej: +56912345678"}), 400
    existente = db.execute("SELECT id FROM agente_mecanicos WHERE telefono = ?", (telefono,)).fetchone()
    if existente:
        return jsonify({"error": "Ya hay un mecánico registrado con ese teléfono"}), 400
    tipo = (data.get("tipo") or "completo").strip()
    if tipo not in TIPOS_MECANICO_VALIDOS:
        return jsonify({"error": f"El tipo de mecánico debe ser uno de: {', '.join(TIPOS_MECANICO_VALIDOS)}"}), 400
    cur = db.execute(
        "INSERT INTO agente_mecanicos (nombre, telefono, activo, tipo, creado_en) VALUES (?, ?, 1, ?, ?)",
        (nombre.upper(), telefono, tipo, now_iso()),
    )
    db.commit()
    fila = db.execute("SELECT * FROM agente_mecanicos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(fila)), 201


@app.route("/api/agente/mecanicos/<int:mecanico_id>", methods=["PUT"])
@requiere_rol("operativo")
def editar_mecanico_agente(mecanico_id):
    db = get_db()
    fila = db.execute("SELECT * FROM agente_mecanicos WHERE id = ?", (mecanico_id,)).fetchone()
    if not fila:
        return jsonify({"error": "No existe ese mecánico"}), 404
    data = request.get_json(force=True, silent=True) or {}
    activo = fila["activo"]
    if "activo" in data:
        activo = 1 if data.get("activo") else 0
    tipo = fila["tipo"] if "tipo" in fila.keys() else "completo"
    if "tipo" in data:
        tipo_nuevo = (data.get("tipo") or "completo").strip()
        if tipo_nuevo not in TIPOS_MECANICO_VALIDOS:
            return jsonify({"error": f"El tipo de mecánico debe ser uno de: {', '.join(TIPOS_MECANICO_VALIDOS)}"}), 400
        tipo = tipo_nuevo
    db.execute("UPDATE agente_mecanicos SET activo = ?, tipo = ? WHERE id = ?", (activo, tipo, mecanico_id))
    db.commit()
    actualizado = db.execute("SELECT * FROM agente_mecanicos WHERE id = ?", (mecanico_id,)).fetchone()
    return jsonify(dict(actualizado))


@app.route("/api/agente/mecanicos/<int:mecanico_id>", methods=["DELETE"])
@requiere_rol("operativo")
def eliminar_mecanico_agente(mecanico_id):
    db = get_db()
    db.execute("DELETE FROM agente_mecanicos WHERE id = ?", (mecanico_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/ordenes/<int:ot_id>/sugerencias-diagnostico", methods=["GET"])
@requiere_rol("operativo")
def listar_sugerencias_diagnostico_ot(ot_id):
    """Sugerencias de diagnóstico pendientes de revisar para una OT (ver
    _agente_guardar_sugerencia_diagnostico) -- dejadas por un mecánico de
    perfil restringido vía WhatsApp. Se usa desde el detalle de la OT en
    Operativo para mostrar el aviso a la parte operativa. Restringido a
    'operativo' (o admin) -- antes cualquier sesión iniciada podía leerlas
    aunque la campanita ya las ocultaba en la interfaz para otros roles
    (ver notifPuedeVer() en notificaciones.js); esto lo hace cumplir
    también del lado del servidor."""
    db = get_db()
    return jsonify(_agente_listar_sugerencias_pendientes(db, ot_id))


@app.route("/api/sugerencias-diagnostico/<int:sugerencia_id>/atender", methods=["POST"])
@requiere_rol("operativo")
def atender_sugerencia_diagnostico(sugerencia_id):
    """Marca una sugerencia como revisada -- no cambia nada del diagnóstico
    real de la OT, solo saca el aviso pendiente. La decisión de si el
    hallazgo del mecánico se traduce o no en un cambio real de diagnóstico
    la toma a mano la parte operativa, editando la OT como siempre."""
    db = get_db()
    fila = db.execute("SELECT * FROM sugerencias_diagnostico WHERE id = ?", (sugerencia_id,)).fetchone()
    if not fila:
        return jsonify({"error": "No existe esa sugerencia"}), 404
    db.execute(
        "UPDATE sugerencias_diagnostico SET atendida = 1, atendida_por = ?, atendida_en = ? WHERE id = ?",
        (usuario_actual(), now_iso(), sugerencia_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM sugerencias_diagnostico WHERE id = ?", (sugerencia_id,)).fetchone()
    return jsonify(dict(actualizada))


# ---------------------------------------------------------------------------
# Notificaciones globales — campanita en el topbar (ver
# templates/_notificaciones.html y static/js/notificaciones.js). Hoy solo
# cubre las sugerencias de diagnóstico de mecánicos restringidos (ver
# migración 16), pensada desde el principio para sumar más categorías más
# adelante (una por rol, ej. bodega con facturas, finanzas con CxC/CxP
# próximas, logística con stock crítico) sin tener que rehacer el mecanismo
# -- por eso 'resumen' ya devuelve un dict con una clave por categoría en
# vez de un solo número suelto.
# ---------------------------------------------------------------------------

@app.route("/api/notificaciones/resumen", methods=["GET"])
@requiere_rol("operativo")
def notificaciones_resumen():
    # 'sugerencias_diagnostico' y 'pruebas_ruta' son ambas del mismo rol
    # (operativo), así que basta con exigirlo una vez acá -- si el día de
    # mañana se suma una categoría de otro módulo (ver comentario de la
    # sección de arriba), este endpoint va a necesitar volver a ser público
    # y filtrar por categoría según el rol, en vez de un solo candado para
    # todo.
    # El contador (usado por el badge de la campanita y la alerta roja de
    # abajo a la derecha) exige además "vista = 0" (Migración 24) -- una
    # vez que la persona abrió la OT desde el link del aviso, deja de
    # sumar acá aunque la sugerencia/prueba siga sin "atender" y siga
    # apareciendo en el panel de la campanita (ver
    # _agente_listar_todas_sugerencias_pendientes /
    # _operativo_listar_todas_pruebas_ruta_pendientes más abajo, que NO
    # filtran por vista).
    db = get_db()
    sugerencias = db.execute("SELECT COUNT(*) AS n FROM sugerencias_diagnostico WHERE atendida = 0 AND vista = 0").fetchone()["n"]
    pruebas_ruta = db.execute(
        "SELECT COUNT(*) AS n FROM pruebas_ruta WHERE atendida = 0 AND vista = 0 AND origen = 'whatsapp'"
    ).fetchone()["n"]
    pre_entrega = db.execute(
        "SELECT COUNT(*) AS n FROM pre_entrega WHERE atendida = 0 AND vista = 0 AND origen = 'whatsapp'"
    ).fetchone()["n"]
    return jsonify({
        "sugerencias_diagnostico": sugerencias,
        "pruebas_ruta": pruebas_ruta,
        "pre_entrega": pre_entrega,
    })


@app.route("/api/notificaciones/sugerencias-diagnostico", methods=["GET"])
@requiere_rol("operativo")
def notificaciones_sugerencias_diagnostico():
    db = get_db()
    return jsonify(_agente_listar_todas_sugerencias_pendientes(db))


@app.route("/api/notificaciones/pruebas-ruta", methods=["GET"])
@requiere_rol("operativo")
def notificaciones_pruebas_ruta():
    db = get_db()
    return jsonify(_operativo_listar_todas_pruebas_ruta_pendientes(db))


@app.route("/api/notificaciones/pre-entrega", methods=["GET"])
@requiere_rol("operativo")
def notificaciones_pre_entrega():
    db = get_db()
    return jsonify(_operativo_listar_todas_pre_entrega_pendientes(db))


@app.route("/api/notificaciones/marcar-vista/<int:orden_id>", methods=["POST"])
@requiere_rol("operativo")
def notificaciones_marcar_vista(orden_id):
    """Marca como 'vista' (Migración 24) todas las sugerencias de
    diagnóstico, pruebas de ruta y entradas de Pre entrega pendientes de
    una OT en particular -- se llama sola al abrir una OT desde el link de
    un aviso (ver abrirOtDesdeQueryParam() en operativo.js), no requiere
    que la persona haga clic en nada aparte. A propósito NO marca
    'atendida': la sugerencia/prueba/pre-entrega sigue apareciendo en el
    panel de la campanita hasta que alguien la descarte a mano con "Marcar
    como revisada"/"Descartar sugerencia" -- ver
    /api/sugerencias-diagnostico/<id>/atender,
    /api/operativo/pruebas-ruta/<id>/atender y
    /api/operativo/pre-entrega/<id>/atender -- esto solo saca el aviso
    urgente (badge/alerta roja) de encima."""
    db = get_db()
    db.execute("UPDATE sugerencias_diagnostico SET vista = 1 WHERE ot_id = ? AND atendida = 0 AND vista = 0", (orden_id,))
    db.execute(
        "UPDATE pruebas_ruta SET vista = 1 WHERE orden_id = ? AND atendida = 0 AND vista = 0 AND origen = 'whatsapp'",
        (orden_id,),
    )
    db.execute(
        "UPDATE pre_entrega SET vista = 1 WHERE orden_id = ? AND atendida = 0 AND vista = 0 AND origen = 'whatsapp'",
        (orden_id,),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/operativo/pruebas-ruta/<int:prueba_id>/atender", methods=["POST"])
@requiere_rol("operativo")
def atender_prueba_ruta(prueba_id):
    """Marca una entrada de la bitácora de pruebas de ruta como revisada --
    igual que atender una sugerencia de diagnóstico, esto NO borra ni
    modifica la entrada en sí (sigue apareciendo para siempre en la
    bitácora de la OT), solo saca el aviso pendiente de la campanita."""
    db = get_db()
    fila = db.execute("SELECT * FROM pruebas_ruta WHERE id = ?", (prueba_id,)).fetchone()
    if not fila:
        return jsonify({"error": "No existe esa entrada de la bitácora"}), 404
    db.execute(
        "UPDATE pruebas_ruta SET atendida = 1, atendida_por = ?, atendida_en = ? WHERE id = ?",
        (usuario_actual(), now_iso(), prueba_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM pruebas_ruta WHERE id = ?", (prueba_id,)).fetchone()
    return jsonify(dict(actualizada))


@app.route("/api/operativo/pre-entrega/<int:entrada_id>/atender", methods=["POST"])
@requiere_rol("operativo")
def atender_pre_entrega_notificacion(entrada_id):
    """Marca una entrada de la bitácora de Pre entrega como revisada --
    mismo mecanismo que atender_prueba_ruta: NO borra ni modifica la
    entrada en sí (sigue apareciendo para siempre en la bitácora "Pre
    entrega" de la OT), solo saca el aviso pendiente de la campanita.
    Nombre distinto a la ruta de eliminar (eliminar_pre_entrega, en
    modulos/operativo.py) para no pisarse -- esa borra la entrada, esta
    solo la marca como revisada."""
    db = get_db()
    fila = db.execute("SELECT * FROM pre_entrega WHERE id = ?", (entrada_id,)).fetchone()
    if not fila:
        return jsonify({"error": "No existe esa entrada de la bitácora"}), 404
    db.execute(
        "UPDATE pre_entrega SET atendida = 1, atendida_por = ?, atendida_en = ? WHERE id = ?",
        (usuario_actual(), now_iso(), entrada_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM pre_entrega WHERE id = ?", (entrada_id,)).fetchone()
    return jsonify(dict(actualizada))


@app.route("/api/operativo/ia/diagnostico-cliente", methods=["POST"])
@requiere_rol("operativo")
def ia_diagnostico_cliente():
    """Autocompleta (sin guardar) el campo 'Diagnóstico (resumen para el
    cliente)' de la pestaña Diagnóstico de una OT, a partir de lo que se
    vaya escribiendo en 'Diagnóstico técnico' -- ver
    _llamar_claude_generar_diagnostico_cliente arriba y el listener de
    input en operativo.js que llama a este endpoint con un debounce
    mientras la persona escribe. No recibe ni necesita el ID de la orden:
    es una generación de texto sin efecto secundario sobre ningún dato
    guardado, la propia pantalla decide si usa o descarta el resultado."""
    data = request.get_json(force=True, silent=True) or {}
    diagnostico_tecnico = (data.get("diagnostico_tecnico") or "").strip()
    sintoma = (data.get("sintoma") or "").strip()
    if not diagnostico_tecnico:
        return jsonify({"diagnostico_cliente": ""})
    try:
        texto = _llamar_claude_generar_diagnostico_cliente(sintoma, diagnostico_tecnico)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"diagnostico_cliente": texto})


@app.route("/api/operativo/ia/dictado-diagnostico", methods=["POST"])
@requiere_rol("operativo")
def ia_dictado_diagnostico():
    """Transcribe un audio dictado directo desde la pestaña Diagnóstico de
    una OT (multipart/form-data, campo 'audio') y separa la transcripción
    en 'síntoma' y 'diagnóstico técnico' -- el mismo tipo de dictado que ya
    se puede hacer por WhatsApp, pero ahora también manual, sin necesitar
    el celular del mecánico (ver _transcribir_audio_whatsapp y
    _llamar_claude_extraer_diagnostico_dictado arriba). Nunca guarda nada
    solo: la pantalla recibe el texto y la persona decide si lo usa,
    revisa y guarda con el botón "Guardar diagnóstico" de siempre -- mismo
    criterio de autocompletar-sin-pisar que el resumen para el cliente.

    Si la transcripción funciona pero falla la separación en campos (ej. la
    IA de texto no responde), no se pierde el trabajo de grabar: se
    devuelve igual la transcripción cruda en 'transcripcion' junto con un
    'aviso', para que la persona pueda copiarla a mano."""
    archivo = request.files.get("audio")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Falta el archivo de audio"}), 400

    audio_bytes = archivo.read()
    try:
        texto_transcrito = _transcribir_audio_whatsapp(audio_bytes, archivo.mimetype)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    try:
        campos = _llamar_claude_extraer_diagnostico_dictado(texto_transcrito)
    except RuntimeError as e:
        return jsonify({
            "transcripcion": texto_transcrito,
            "sintoma": "",
            "diagnostico_tecnico": "",
            "aviso": f"Se transcribió el audio pero no se pudo separar en campos automáticamente: {e}",
        })

    return jsonify({
        "transcripcion": texto_transcrito,
        "sintoma": campos["sintoma"],
        "diagnostico_tecnico": campos["diagnostico_tecnico"],
    })


@app.route("/api/agente/simular-mensaje", methods=["POST"])
@requiere_rol("operativo")
def simular_mensaje_agente():
    """Deja probar el agente completo (IA + validación de transición +
    cambio de estado real) desde el navegador, sin necesitar una cuenta de
    WhatsApp conectada — útil tanto para probar hoy como para hacer una
    demo. Ejecuta EXACTAMENTE la misma lógica que usaría un mensaje real
    de WhatsApp (_agente_procesar_mensaje), así que un mensaje simulado
    puede mover una OT de verdad — se avisa en la interfaz."""
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    telefono = (data.get("telefono") or "").strip()
    texto = (data.get("texto") or "").strip()
    if not telefono or not texto:
        return jsonify({"error": "Faltan el teléfono y/o el mensaje"}), 400
    resultado = _agente_procesar_mensaje(db, telefono, texto)
    return jsonify(resultado)


@app.route("/api/agente/simular-mensaje-voz", methods=["POST"])
@requiere_rol("operativo")
def simular_mensaje_voz_agente():
    """Igual que /api/agente/simular-mensaje, pero para probar notas de
    voz sin necesitar una cuenta de WhatsApp real: recibe un archivo de
    audio (multipart/form-data), lo transcribe con
    _transcribir_audio_whatsapp y de ahí en adelante sigue exactamente el
    mismo camino que un mensaje escrito."""
    db = get_db()
    telefono = (request.form.get("telefono") or "").strip()
    archivo = request.files.get("audio")
    if not telefono or not archivo or not archivo.filename:
        return jsonify({"error": "Faltan el teléfono y/o el archivo de audio"}), 400

    audio_bytes = archivo.read()
    try:
        texto = _transcribir_audio_whatsapp(audio_bytes, archivo.mimetype)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    resultado = _agente_procesar_mensaje(db, telefono, texto, origen="voz")
    resultado["transcripcion"] = texto
    return jsonify(resultado)


@app.route("/api/agente/simular-mensaje-foto", methods=["POST"])
@requiere_rol("operativo")
def simular_mensaje_foto_agente():
    """Igual que /api/agente/simular-mensaje, pero para probar el análisis
    de fotos sin necesitar una cuenta de WhatsApp real: recibe una imagen
    (multipart/form-data) y sigue exactamente el mismo camino que una foto
    real mandada por WhatsApp — la IA primero clasifica si es una factura
    de compra (queda pendiente de revisión en Facturas → "Pendientes de
    WhatsApp", nunca se sube sola) o la foto de un vehículo (se lee la
    patente y se usa junto al 'texto' opcional, el pie de foto, para
    identificar la OT y aplicar la acción — ver
    _llamar_claude_vision_clasificar_foto_whatsapp)."""
    db = get_db()
    telefono = (request.form.get("telefono") or "").strip()
    texto = (request.form.get("texto") or "").strip()
    archivo = request.files.get("foto")
    if not telefono or not archivo or not archivo.filename:
        return jsonify({"error": "Faltan el teléfono y/o la foto"}), 400

    mime_por_extension = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
    extension = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
    mime_type = mime_por_extension.get(extension) or archivo.mimetype or "image/jpeg"

    imagen_bytes = archivo.read()
    resultado = _agente_procesar_mensaje(db, telefono, texto, origen="foto_factura", imagen_bytes=imagen_bytes, imagen_mime=mime_type)
    return jsonify(resultado)


@app.route("/api/agente/mensajes", methods=["GET"])
@requiere_rol("operativo")
def listar_mensajes_agente():
    db = get_db()
    filas = db.execute("SELECT * FROM agente_mensajes ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in filas])


@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def webhook_whatsapp():
    """Punto de entrada para mensajes REALES de WhatsApp.

    **Es agnóstico del proveedor.** Twilio y la Cloud API de Meta conviven
    detrás de WHATSAPP_ADAPTADORES; acá solo se orquesta el flujo. Lo que
    cambia entre uno y otro es el transporte: el agente que interpreta, la
    resolución de OT por talca_id, el registro de mecánicos, las acciones y el
    push hacia Talca no se enteran de cuál está activo.

    Ruta pública (ver RUTAS_PUBLICAS): ninguno de los dos manda una sesión de
    este sistema, así que la única verificación posible es la firma -- y por
    eso sin el secreto del proveedor activo no se procesa nada (503).

    **Nunca devuelve 500.** Cualquier excepción se atrapa y se responde 200
    con un mensaje de disculpa: un no-200 hace que la plataforma reintente en
    loop, y con Meta eso además duplicaría acciones ya aplicadas.

    El GET solo existe para Meta: es la verificación inicial del webhook."""
    proveedor = whatsapp_proveedor()
    adaptador = WHATSAPP_ADAPTADORES[proveedor]

    # -- Verificación inicial del webhook (solo Meta) ------------------------
    # Meta llama una vez con hub.mode=subscribe y espera que se le devuelva
    # hub.challenge tal cual, si hub.verify_token coincide con el configurado.
    if request.method == "GET":
        if proveedor != "meta":
            return "", 405
        if (request.args.get("hub.mode") == "subscribe"
                and META_WA_VERIFY_TOKEN
                and request.args.get("hub.verify_token") == META_WA_VERIFY_TOKEN):
            logger.info("Webhook de WhatsApp (Meta): verificación superada.")
            return request.args.get("hub.challenge", ""), 200
        logger.warning(
            "Webhook de WhatsApp (Meta): verificación RECHAZADA "
            "(mode=%r, token coincide=%s, META_WA_VERIFY_TOKEN definida=%s)",
            request.args.get("hub.mode"),
            request.args.get("hub.verify_token") == META_WA_VERIFY_TOKEN,
            bool(META_WA_VERIFY_TOKEN),
        )
        return "", 403

    def _error(texto):
        """Un fallo del que hay que avisarle al mecánico. Siempre 200: ver el
        docstring."""
        try:
            return adaptador["entregar"](telefono_crudo, texto)
        except Exception:
            logger.exception("No se pudo entregar el mensaje de error")
            return "", 200

    telefono_crudo = ""
    try:
        if not adaptador["secreto"]():
            logger.error(
                "Webhook de WhatsApp (%s) sin %s: el mensaje NO se procesó.",
                proveedor, adaptador["falta_secreto"],
            )
            if proveedor == "twilio":
                cuerpo, _, cabeceras = _twilio_entregar(
                    "", f"El agente de WhatsApp todavía no está configurado del todo (falta "
                        f"{adaptador['falta_secreto']}) -- este mensaje no se procesó.")
                return cuerpo, 503, cabeceras
            return "", 503
        if not adaptador["validar_firma"](request):
            # 403 sin cuerpo: si la firma no coincide, no es quien dice ser y
            # no hay a quién responderle.
            return "", 403

        mensaje = adaptador["extraer_mensaje"](request)
        if mensaje is None:
            # No es un mensaje de un usuario (con Meta: eventos de estado de
            # entrega). Se acusa recibo y listo.
            return "", 200

        telefono_crudo = mensaje["telefono"]
        texto = mensaje["texto"]
        db = get_db()

        # Adjuntos. Una foto se manda a clasificar por IA (puede ser una
        # factura de compra o la foto de un vehículo, en cuyo caso el texto
        # que venga con ella se usa como pie de foto con la acción a
        # realizar). Un audio se transcribe y se trata como el texto del
        # mensaje. Solo se mira el primer adjunto.
        origen = "texto"
        imagen_bytes = None
        imagen_mime = None
        if mensaje["media"]:
            if mensaje["media_tipo"] == "imagen":
                try:
                    imagen_bytes, imagen_mime = adaptador["descargar_media"](mensaje["media"])
                    origen = "foto_factura"
                except RuntimeError as exc:
                    return _error(f"No pude descargar tu foto: {exc}")
            elif not texto.strip():
                if mensaje["media_tipo"] == "audio":
                    try:
                        audio, mime_audio = adaptador["descargar_media"](mensaje["media"])
                        texto = _transcribir_audio_whatsapp(audio, mime_audio)
                        origen = "voz"
                    except RuntimeError as exc:
                        return _error(f"No pude procesar tu nota de voz: {exc}")
                else:
                    return _error(
                        "Recibí un adjunto que no pude procesar (solo entiendo texto, "
                        "fotos de facturas o notas de voz)."
                    )

        resultado = _agente_procesar_mensaje(
            db, telefono_crudo, texto, origen=origen,
            imagen_bytes=imagen_bytes, imagen_mime=imagen_mime,
        )
        return adaptador["entregar"](
            telefono_crudo, resultado.get("respuesta_texto") or "No pude procesar tu mensaje."
        )
    except Exception:
        logger.exception("Error en /webhook/whatsapp (proveedor=%s)", proveedor)
        return _error("Hubo un error interno procesando tu mensaje. Avisa en el taller.")


# ---------------------------------------------------------------------------
# API: Resumen (base para el futuro Dashboard)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API: Resumen (base para el futuro Dashboard)
# ---------------------------------------------------------------------------

@app.route("/api/resumen", methods=["GET"])
@requiere_rol("bodega")
def resumen():
    db = get_db()
    row = db.execute(
        """
        SELECT
            COUNT(*) AS total_productos,
            COALESCE(SUM(stock_disponible + stock_reservado), 0) AS total_unidades,
            COALESCE(SUM((stock_disponible + stock_reservado) * costo_compra), 0) AS valor_total,
            COALESCE(SUM(CASE WHEN stock_disponible <= stock_minimo THEN 1 ELSE 0 END), 0) AS stock_critico
        FROM productos
        """
    ).fetchone()
    return jsonify(dict(row))


# ---------------------------------------------------------------------------
# API: Diagnóstico y respaldo (para verificar que el disco persistente de
# Railway realmente está guardando los datos, y como red de seguridad)
# ---------------------------------------------------------------------------

@app.route("/api/estado", methods=["GET"])
@requiere_rol()
def estado_sistema():
    db = get_db()
    existe_archivo = os.path.exists(DB_PATH)
    tamano_kb = round(os.path.getsize(DB_PATH) / 1024, 1) if existe_archivo else 0
    productos = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    facturas = db.execute("SELECT COUNT(*) FROM facturas").fetchone()[0]
    movimientos = db.execute("SELECT COUNT(*) FROM movimientos").fetchone()[0]
    return jsonify({
        "db_path": DB_PATH,
        "parece_disco_persistente": DB_PATH.startswith("/data"),
        "archivo_existe": existe_archivo,
        "tamano_kb": tamano_kb,
        "cantidad_productos": productos,
        "cantidad_facturas": facturas,
        "cantidad_movimientos": movimientos,
    })


@app.route("/api/admin/limpiar-inventario-duplicado", methods=["POST"])
@requiere_rol()
def limpiar_inventario_duplicado():
    """Herramienta de un solo uso para corregir el inventario duplicado
    causado por la extinta 'Migración 11' (ver init_db): un lote de
    productos con id >= 60001 pudo haberse reinyectado solo en algún
    reinicio del servidor, encima del inventario real cargado a mano por
    'Importar Excel'. Borra ese lote específico (y en cascada sus
    movimientos/reservas/cálculo de stock mínimo asociados, por las
    FOREIGN KEY ON DELETE CASCADE de esas tablas) dejando intacto todo lo
    demás. Exige 'confirmar_texto' == 'LIMPIAR'. Es seguro correrla más de
    una vez: si ya no quedan productos con id >= 60001, no borra nada."""
    confirmar_texto = (request.get_json(silent=True) or {}).get("confirmar_texto", "").strip().upper()
    if confirmar_texto != "LIMPIAR":
        return jsonify({"error": "Falta confirmar la acción: envía confirmar_texto = 'LIMPIAR'."}), 400

    db = get_db()
    afectados = db.execute("SELECT COUNT(*) FROM productos WHERE id >= 60001").fetchone()[0]
    if afectados == 0:
        return jsonify({"mensaje": "No hay nada que limpiar: no quedan productos con id >= 60001.", "productos_eliminados": 0})

    db.execute("DELETE FROM factura_lineas WHERE producto_id >= 60001")
    db.execute("DELETE FROM productos WHERE id >= 60001")
    db.commit()

    productos_restantes = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    return jsonify({
        "mensaje": f"Se eliminó el lote duplicado ({afectados} productos, ids 60001+) y sus movimientos/reservas asociados.",
        "productos_eliminados": afectados,
        "productos_restantes": productos_restantes,
    })


# ---------------------------------------------------------------------------
# Respaldos automáticos locales (cada pocos minutos, en el mismo disco)
# ---------------------------------------------------------------------------
# Complementa al respaldo diario por correo: cada RESPALDO_LOCAL_INTERVALO_MINUTOS
# minutos se crea una copia consistente (VACUUM INTO) de la base de datos en
# una carpeta aparte del mismo volumen, rotando para no acumular copias
# indefinidamente. Así, ante un problema (borrado accidental, dato corrupto)
# se puede volver a un estado de "hace pocos minutos" sin depender del correo
# diario ni de un proveedor externo nuevo (se evaluó Litestream + Cloudflare
# R2 y se descartó para no sumar otro proveedor más — ver DEPLOY.md).


# Cambiado de 15 a 60 minutos (julio 2026, tras el incidente del volumen al
# 95%): con MAX_COPIAS sin tocar, esto pasa de cubrir 24 horas de
# historial a cubrir muchas más horas con la misma cantidad de copias --
# ambos casos terminan ocupando el MISMO tope de disco en régimen
# (MAX_COPIAS × tamaño de la base de datos en ese momento), la rotación en
# _rotar_respaldos_locales() ya se encarga de eso solo; lo único que cambia
# con el intervalo es cuánto tarda en llegar a ese tope y cuánto historial
# cubre. El respiro real vino de subir el volumen de Railway de 500 MB a
# 5 GB -- si el disco vuelve a quedar ajustado, bajar RESPALDO_LOCAL_MAX_
# COPIAS (no solo el intervalo) es lo que de verdad baja el tope de disco.
#
# Bajado de 96 a 48 (julio 2026, mismo incidente): el disco se seguía
# llenando más rápido de lo esperado incluso después de subir el volumen a
# 5 GB, así que se acota también el techo real (48 copias × tamaño de la
# base en ese momento) hasta confirmar que el consumo se estabiliza.
RESPALDO_LOCAL_INTERVALO_MINUTOS = int(os.environ.get("RESPALDO_LOCAL_INTERVALO_MINUTOS", "60"))
RESPALDO_LOCAL_MAX_COPIAS = int(os.environ.get("RESPALDO_LOCAL_MAX_COPIAS", "48"))
RESPALDO_LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "respaldos_locales")
RESPALDO_LOCAL_PATRON = re.compile(r"^auto_\d{8}_\d{6}\.db$")


def _rotar_respaldos_locales():
    copias = _listar_respaldos_locales()
    if len(copias) <= RESPALDO_LOCAL_MAX_COPIAS:
        return
    for copia in copias[RESPALDO_LOCAL_MAX_COPIAS:]:
        try:
            os.remove(os.path.join(RESPALDO_LOCAL_DIR, copia["nombre"]))
        except OSError:
            pass


RESPALDO_LOCAL_MIN_MB_LIBRES = int(os.environ.get("RESPALDO_LOCAL_MIN_MB_LIBRES", "100"))


def _crear_respaldo_local():
    """Crea una copia consistente de la base de datos con VACUUM INTO.
    Devuelve (True, nombre_del_archivo) o (False, mensaje_de_error).

    Antes de escribir nada, revisa el espacio libre del disco: si queda
    menos de RESPALDO_LOCAL_MIN_MB_LIBRES, se salta el respaldo en vez de
    intentarlo -- un disco casi lleno (ver incidente de julio 2026, volumen
    de Railway al 95%) es exactamente el escenario donde un respaldo más NO
    ayuda (es una copia completa de la base de datos) y puede terminar de
    llenarlo, lo que rompe TODO el sistema (cualquier escritura a SQLite
    empieza a tirar 'disk I/O error', no solo los respaldos)."""
    if not os.path.exists(DB_PATH):
        return False, "Aún no existe base de datos que respaldar"
    try:
        libres_mb = shutil.disk_usage(os.path.dirname(os.path.abspath(DB_PATH)) or ".").free / (1024 * 1024)
        if libres_mb < RESPALDO_LOCAL_MIN_MB_LIBRES:
            return False, (
                f"Se salta el respaldo: quedan solo {libres_mb:.0f} MB libres en el disco "
                f"(mínimo configurado: {RESPALDO_LOCAL_MIN_MB_LIBRES} MB). Libera espacio antes de que "
                f"esto afecte al resto del sistema."
            )
    except OSError:
        pass  # si no se puede medir, se sigue intentando el respaldo como antes
    os.makedirs(RESPALDO_LOCAL_DIR, exist_ok=True)
    nombre = f"auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    destino = os.path.join(RESPALDO_LOCAL_DIR, nombre)
    try:
        # Conexión propia (no g.db): esta función también se llama desde el
        # hilo en segundo plano, fuera de cualquier request/app_context.
        db = _conectar_db()
        try:
            db.execute("VACUUM INTO ?", (destino,))
        finally:
            db.close()
    except sqlite3.Error as exc:
        return False, str(exc)
    _rotar_respaldos_locales()
    return True, nombre


def _listar_respaldos_locales():
    if not os.path.isdir(RESPALDO_LOCAL_DIR):
        return []
    copias = []
    for nombre in os.listdir(RESPALDO_LOCAL_DIR):
        if not RESPALDO_LOCAL_PATRON.match(nombre):
            continue
        ruta = os.path.join(RESPALDO_LOCAL_DIR, nombre)
        try:
            stat = os.stat(ruta)
        except OSError:
            continue
        copias.append({
            "nombre": nombre,
            "tamano_kb": round(stat.st_size / 1024, 1),
            "creado_en": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    copias.sort(key=lambda c: c["nombre"], reverse=True)
    return copias


def _hilo_respaldos_locales():
    time.sleep(60)  # deja que el servidor termine de arrancar antes del primer respaldo
    while True:
        try:
            ok, detalle = _crear_respaldo_local()
            if not ok:
                logging.warning("Respaldo automático local no se creó: %s", detalle)
        except Exception:
            logging.exception("Error creando respaldo automático local")
        time.sleep(RESPALDO_LOCAL_INTERVALO_MINUTOS * 60)


def iniciar_hilo_respaldos_locales():
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        hilo = threading.Thread(target=_hilo_respaldos_locales, daemon=True)
        hilo.start()


@app.route("/api/backup/locales", methods=["GET"])
@requiere_rol()
def backup_locales_listar():
    return jsonify({
        "copias": _listar_respaldos_locales(),
        "intervalo_minutos": RESPALDO_LOCAL_INTERVALO_MINUTOS,
        "max_copias": RESPALDO_LOCAL_MAX_COPIAS,
    })


def _tamano_carpeta_mb(ruta):
    """Suma recursiva del tamaño de todos los archivos bajo 'ruta', en MB.
    0 si la carpeta todavía no existe (nunca se guardó nada ahí)."""
    if not os.path.isdir(ruta):
        return 0.0, 0
    total_bytes = 0
    total_archivos = 0
    for dirpath, _dirnames, nombres in os.walk(ruta):
        for nombre in nombres:
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, nombre))
                total_archivos += 1
            except OSError:
                continue
    return round(total_bytes / (1024 * 1024), 1), total_archivos


@app.route("/api/admin/diagnostico-disco", methods=["GET"])
@requiere_rol()
def diagnostico_disco():
    """Desglosa qué está ocupando el disco del servidor -- la base de datos
    en sí, los respaldos automáticos locales, y cada carpeta de fotos/
    comprobantes -- para poder identificar qué crece más rápido de lo
    esperado sin tener acceso directo al volumen (ver incidente de julio
    2026: el volumen de Railway llegó al 95% mucho antes de lo previsto)."""
    base_dir = os.path.dirname(os.path.abspath(DB_PATH)) or "."
    uso_disco = shutil.disk_usage(base_dir)

    db_mb = round(os.path.getsize(DB_PATH) / (1024 * 1024), 1) if os.path.exists(DB_PATH) else 0.0
    respaldos_mb, respaldos_n = _tamano_carpeta_mb(RESPALDO_LOCAL_DIR)
    fotos_vehiculos_mb, fotos_vehiculos_n = _tamano_carpeta_mb(os.path.join(base_dir, "fotos_vehiculos"))
    fotos_productos_mb, fotos_productos_n = _tamano_carpeta_mb(FOTOS_DIR)
    facturas_whatsapp_mb, facturas_whatsapp_n = _tamano_carpeta_mb(FACTURAS_WHATSAPP_DIR)
    comprobantes_mb, comprobantes_n = _tamano_carpeta_mb(COMPROBANTES_DIR)

    return jsonify({
        "disco": {
            "total_mb": round(uso_disco.total / (1024 * 1024), 1),
            "usado_mb": round(uso_disco.used / (1024 * 1024), 1),
            "libre_mb": round(uso_disco.free / (1024 * 1024), 1),
            "porcentaje_usado": round(100 * uso_disco.used / uso_disco.total, 1) if uso_disco.total else 0,
        },
        "desglose": [
            {"carpeta": "Base de datos (inventario.db)", "mb": db_mb, "archivos": 1},
            {"carpeta": "Respaldos automáticos locales", "mb": respaldos_mb, "archivos": respaldos_n},
            {"carpeta": "Fotos de órdenes/garantías", "mb": fotos_vehiculos_mb, "archivos": fotos_vehiculos_n},
            {"carpeta": "Fotos de productos", "mb": fotos_productos_mb, "archivos": fotos_productos_n},
            {"carpeta": "Facturas recibidas por WhatsApp", "mb": facturas_whatsapp_mb, "archivos": facturas_whatsapp_n},
            {"carpeta": "Comprobantes de pago", "mb": comprobantes_mb, "archivos": comprobantes_n},
        ],
    })


@app.route("/api/backup/locales/crear", methods=["POST"])
@requiere_rol()
def backup_locales_crear():
    exito, resultado = _crear_respaldo_local()
    if not exito:
        return jsonify({"error": resultado}), 400
    return jsonify({"ok": True, "nombre": resultado})


@app.route("/api/backup/locales/limpiar", methods=["POST"])
@requiere_rol()
def backup_locales_limpiar():
    """Borra respaldos automáticos locales YA, sin esperar a la próxima
    rotación (que solo pasa cuando se crea uno nuevo) -- pensado para
    liberar espacio de urgencia si el disco se está por llenar (ver
    incidente de julio 2026). Por defecto deja los 3 más recientes; se
    puede pedir dejar más/menos con 'conservar' en el body."""
    conservar = (request.get_json(silent=True) or {}).get("conservar", 3)
    try:
        conservar = max(0, int(conservar))
    except (TypeError, ValueError):
        conservar = 3
    copias = _listar_respaldos_locales()
    a_borrar = copias[conservar:]
    borrados = 0
    mb_liberados = 0.0
    for copia in a_borrar:
        try:
            os.remove(os.path.join(RESPALDO_LOCAL_DIR, copia["nombre"]))
            borrados += 1
            mb_liberados += copia["tamano_kb"] / 1024
        except OSError:
            continue
    return jsonify({
        "ok": True,
        "borrados": borrados,
        "mb_liberados": round(mb_liberados, 1),
        "copias_restantes": len(copias) - borrados,
    })


@app.route("/api/backup/locales/restaurar", methods=["POST"])
@requiere_rol()
def backup_locales_restaurar():
    nombre = (request.get_json(silent=True) or {}).get("nombre", "").strip()
    if not RESPALDO_LOCAL_PATRON.match(nombre):
        return jsonify({"error": "Nombre de respaldo inválido"}), 400
    origen = os.path.join(RESPALDO_LOCAL_DIR, nombre)
    if not os.path.exists(origen):
        return jsonify({"error": "No se encontró ese respaldo"}), 404

    db = g.pop("db", None)
    if db is not None:
        db.close()

    shutil.copyfile(origen, DB_PATH)
    init_db()
    return jsonify({"ok": True})


@app.route("/api/backup/descargar", methods=["GET"])
@requiere_rol()
def backup_descargar():
    if not os.path.exists(DB_PATH):
        return jsonify({"error": "Aún no existe base de datos que respaldar"}), 404
    db = get_db()
    db.commit()  # asegura que todo lo pendiente quede escrito en el archivo
    fecha = datetime.now().strftime("%Y-%m-%d_%H%M")
    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name=f"inventario_backup_{fecha}.db",
        mimetype="application/octet-stream",
    )


@app.route("/api/backup/restaurar", methods=["POST"])
@requiere_rol()
def backup_restaurar():
    archivo = request.files.get("archivo")
    if not archivo or archivo.filename == "":
        return jsonify({"error": "Selecciona un archivo de respaldo (.db)"}), 400

    tmp_path = DB_PATH + ".subida_tmp"
    archivo.save(tmp_path)

    # Validar que sea una base de datos SQLite válida con la tabla esperada,
    # antes de reemplazar nada.
    try:
        prueba = sqlite3.connect(tmp_path)
        prueba.execute("SELECT COUNT(*) FROM productos")
        prueba.close()
    except sqlite3.Error:
        os.remove(tmp_path)
        return jsonify({"error": "El archivo no parece ser un respaldo válido de este sistema"}), 400

    # Cierra la conexión de esta request y respalda el archivo actual antes de reemplazarlo.
    db = g.pop("db", None)
    if db is not None:
        db.close()

    if os.path.exists(DB_PATH):
        respaldo_previo = DB_PATH + ".antes_de_restaurar"
        try:
            os.replace(DB_PATH, respaldo_previo)
        except OSError:
            pass

    os.replace(tmp_path, DB_PATH)

    # Se corren las migraciones por si el respaldo es de una versión anterior.
    init_db()

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Envío de correo: Resend (HTTPS) primero, SMTP como respaldo
# ---------------------------------------------------------------------------

def _enviar_correo_resend(destinatario, asunto, cuerpo_texto, adjunto_bytes=None, adjunto_nombre=None, cc=None):
    """Manda un correo usando la API HTTPS de Resend (https://resend.com).
    No usa el protocolo SMTP, así que no lo afectan los bloqueos de puerto
    SMTP que aplican varios hostings (incluido Railway en sus planes
    gratis/Trial/Hobby). 'cc' es una lista opcional de correos en copia.
    Devuelve (exito: bool, detalle: str)."""
    if requests is None:
        return False, (
            "No se pudo enviar por Resend: falta instalar la librería 'requests' en el "
            "servidor (revisa que requirements.txt la incluya y que el último deploy se haya "
            "construido de nuevo, no solo reiniciado)."
        )
    try:
        payload = {
            "from": RESEND_FROM,
            "to": [destinatario],
            "subject": asunto,
            "text": cuerpo_texto,
        }
        if cc:
            payload["cc"] = list(cc)
        if adjunto_bytes is not None:
            payload["attachments"] = [{
                "filename": adjunto_nombre or "adjunto",
                "content": base64.b64encode(adjunto_bytes).decode("ascii"),
            }]
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code >= 400:
            detalle_error = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
            return False, f"Resend respondió con error ({resp.status_code}): {detalle_error}"
        return True, f"Enviado a {destinatario} (vía Resend)"
    except requests.RequestException as e:
        return False, f"Error al enviar por Resend: {e}"


def _enviar_correo_smtp(destinatario, asunto, cuerpo_texto, adjunto_bytes=None, adjunto_nombre=None, cc=None):
    """Manda un correo por SMTP tradicional (ej: Gmail). Solo funciona si
    el hosting no bloquea el puerto SMTP saliente. 'cc' es una lista
    opcional de correos en copia. Devuelve (exito: bool, detalle: str)."""
    try:
        msg = EmailMessage()
        msg["Subject"] = asunto
        msg["From"] = SMTP_USER
        msg["To"] = destinatario
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.set_content(cuerpo_texto)
        if adjunto_bytes is not None:
            msg.add_attachment(
                adjunto_bytes, maintype="application", subtype="octet-stream",
                filename=adjunto_nombre or "adjunto",
            )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            # Los destinatarios reales del envío SMTP hay que pasarlos
            # explícitos (send_message solo lee To/Cc/Bcc del mensaje si no
            # se le indica to_addrs, pero se es explícito acá para que quede
            # claro que el CC sí se está mandando y no solo mostrando).
            destinatarios = [destinatario] + list(cc or [])
            smtp.send_message(msg, to_addrs=destinatarios)

        return True, f"Enviado a {destinatario}" + (f" (copia a {', '.join(cc)})" if cc else "") + " (vía SMTP)"
    except Exception as e:
        return False, f"Error al enviar por SMTP: {e}"


def _enviar_correo(destinatario, asunto, cuerpo_texto, adjunto_bytes=None, adjunto_nombre=None, cc=None):
    """Envía un correo por el transporte disponible: Resend si hay
    RESEND_API_KEY configurada (se prueba primero — funciona incluso si el
    hosting bloquea SMTP), si no, SMTP tradicional. 'cc' es una lista
    opcional de correos en copia (se aplica en ambos transportes). Devuelve
    (exito: bool, detalle: str)."""
    if RESEND_API_KEY and requests is not None:
        return _enviar_correo_resend(destinatario, asunto, cuerpo_texto, adjunto_bytes, adjunto_nombre, cc)
    if SMTP_USER and SMTP_PASSWORD:
        return _enviar_correo_smtp(destinatario, asunto, cuerpo_texto, adjunto_bytes, adjunto_nombre, cc)
    if RESEND_API_KEY and requests is None:
        return False, (
            "Hay RESEND_API_KEY configurada pero falta instalar la librería 'requests' en el "
            "servidor — revisa requirements.txt y fuerza un redeploy completo en Railway."
        )
    return False, "No hay ningún método de envío de correo configurado (falta RESEND_API_KEY, o SMTP_USER / SMTP_PASSWORD)."


# ---------------------------------------------------------------------------
# Respaldo automático diario por correo
# ---------------------------------------------------------------------------

def _enviar_respaldo_por_correo():
    """Manda el archivo de base de datos como adjunto por correo.
    Devuelve (exito: bool, detalle: str)."""
    if not respaldo_automatico_configurado():
        return False, "El respaldo automático no está configurado (falta RESEND_API_KEY, o SMTP_USER / SMTP_PASSWORD, y en cualquier caso SMTP_DESTINATARIO)."
    if not os.path.exists(DB_PATH):
        return False, "Aún no existe base de datos que respaldar."

    fecha = datetime.now().strftime("%Y-%m-%d_%H%M")
    cuerpo = (
        "Respaldo automático diario del sistema de inventario del taller.\n\n"
        f"Fecha: {fecha}\n"
        "Este correo se genera solo, no es necesario responderlo.\n"
        "Si algún día necesitas restaurar estos datos, guarda el archivo adjunto "
        "y súbelo desde el botón 'Respaldo' → 'Restaurar' dentro del sistema."
    )
    with open(DB_PATH, "rb") as f:
        contenido = f.read()

    return _enviar_correo(
        SMTP_DESTINATARIO,
        f"Respaldo Inventario Taller — {fecha}",
        cuerpo,
        adjunto_bytes=contenido,
        adjunto_nombre=f"inventario_backup_{fecha}.db",
    )


def _registrar_respaldo_log(exito, detalle):
    try:
        db = _conectar_db()
        db.execute(
            "INSERT INTO respaldos_log (fecha, exito, destino, detalle) VALUES (?, ?, ?, ?)",
            (now_iso(), 1 if exito else 0, SMTP_DESTINATARIO, detalle),
        )
        db.commit()
        db.close()
    except sqlite3.Error:
        pass


def _hilo_respaldos_automaticos():
    while True:
        try:
            if respaldo_automatico_configurado():
                db = _conectar_db()
                ultimo = db.execute(
                    "SELECT fecha FROM respaldos_log WHERE exito = 1 ORDER BY fecha DESC LIMIT 1"
                ).fetchone()
                db.close()

                debe_enviar = True
                if ultimo:
                    horas_desde_ultimo = (datetime.now() - datetime.fromisoformat(ultimo[0])).total_seconds() / 3600
                    debe_enviar = horas_desde_ultimo >= RESPALDO_INTERVALO_HORAS

                if debe_enviar:
                    exito, detalle = _enviar_respaldo_por_correo()
                    _registrar_respaldo_log(exito, detalle)
        except Exception:
            pass
        time.sleep(3600)  # revisa una vez por hora si ya toca enviar


_hilo_iniciado = False


def iniciar_hilo_respaldos():
    global _hilo_iniciado
    if not _hilo_iniciado:
        _hilo_iniciado = True
        hilo = threading.Thread(target=_hilo_respaldos_automaticos, daemon=True)
        hilo.start()


@app.route("/api/backup/estado-automatico", methods=["GET"])
@requiere_rol()
def backup_estado_automatico():
    db = get_db()
    historial = db.execute(
        "SELECT * FROM respaldos_log ORDER BY fecha DESC LIMIT 10"
    ).fetchall()
    return jsonify({
        "configurado": respaldo_automatico_configurado(),
        "destinatario": SMTP_DESTINATARIO if respaldo_automatico_configurado() else None,
        "intervalo_horas": RESPALDO_INTERVALO_HORAS,
        "historial": [dict(r) for r in historial],
    })


@app.route("/api/backup/probar-automatico", methods=["POST"])
@requiere_rol()
def backup_probar_automatico():
    exito, detalle = _enviar_respaldo_por_correo()
    _registrar_respaldo_log(exito, detalle)
    if not exito:
        return jsonify({"error": detalle}), 400
    return jsonify({"ok": True, "detalle": detalle})


# ---------------------------------------------------------------------------
# Alertas automáticas de stock crítico por correo
# ---------------------------------------------------------------------------
#
# Reutiliza las mismas credenciales SMTP que el respaldo automático diario:
# una vez por día (configurable) revisa si hay productos en o bajo su stock
# mínimo y, si hay alguno, manda un correo con el detalle. Si no hay
# productos críticos ese día, no se manda nada (para no generar ruido).

def _productos_stock_critico_para_alerta(db):
    """Mismo criterio que se usa en el resto del sistema para 'stock
    crítico' (Dashboard, Reportes): stock disponible en o bajo el mínimo."""
    rows = db.execute(
        PRODUCTO_SELECT + " WHERE p.stock_disponible <= p.stock_minimo AND p.stock_minimo > 0"
        " ORDER BY (p.stock_disponible - p.stock_minimo) ASC"
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _enviar_alerta_stock_critico(forzar_aunque_vacio=False):
    """Revisa el stock crítico actual y, si hay productos, manda un correo
    con el detalle. Devuelve (exito, detalle, n_productos, correo_enviado)."""
    if not alerta_stock_configurada():
        return False, "Las alertas de stock no están configuradas (falta RESEND_API_KEY, o SMTP_USER / SMTP_PASSWORD).", 0, False

    db = _conectar_db()
    try:
        productos = _productos_stock_critico_para_alerta(db)
    finally:
        db.close()

    if not productos and not forzar_aunque_vacio:
        return True, "Sin productos en estado crítico — no se envió correo.", 0, False

    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    if productos:
        asunto = f"⚠ {len(productos)} producto(s) con stock crítico — Inventario Taller"
        lineas = [
            f"  - #{p['id_sistema']} {p['nombre']}: {p['stock_disponible']} disponible(s) (mínimo {p['stock_minimo']})"
            for p in productos
        ]
        cuerpo = (
            f"Se detectaron {len(productos)} producto(s) en o bajo su stock mínimo.\n\n"
            + "\n".join(lineas)
            + f"\n\nFecha: {fecha}\n"
            "Revisa la pestaña 'Planificación' en el sistema para ver cuánto se sugiere comprar "
            "y a qué proveedor, o la pestaña 'Productos' para ajustar el mínimo si ya no aplica.\n"
            "Este correo se genera solo, no es necesario responderlo."
        )
    else:
        asunto = "Inventario Taller — sin productos con stock crítico"
        cuerpo = f"No hay productos con stock crítico al momento de este chequeo manual.\n\nFecha: {fecha}"

    exito, detalle = _enviar_correo(ALERTA_STOCK_DESTINATARIO, asunto, cuerpo)
    if exito:
        return True, f"{detalle} ({len(productos)} producto(s))", len(productos), True
    return False, detalle, len(productos), False


def _registrar_alerta_stock_log(exito, detalle, n_productos, correo_enviado):
    try:
        db = _conectar_db()
        db.execute(
            """
            INSERT INTO alertas_stock_log (fecha, exito, destino, detalle, n_productos, correo_enviado)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now_iso(), 1 if exito else 0, ALERTA_STOCK_DESTINATARIO, detalle, n_productos, 1 if correo_enviado else 0),
        )
        db.commit()
        db.close()
    except sqlite3.Error:
        pass


def _hilo_alertas_stock():
    while True:
        try:
            if alerta_stock_configurada():
                db = _conectar_db()
                ultimo = db.execute(
                    "SELECT fecha FROM alertas_stock_log ORDER BY fecha DESC LIMIT 1"
                ).fetchone()
                db.close()

                debe_revisar = True
                if ultimo:
                    horas_desde_ultimo = (datetime.now() - datetime.fromisoformat(ultimo[0])).total_seconds() / 3600
                    debe_revisar = horas_desde_ultimo >= ALERTA_STOCK_INTERVALO_HORAS

                if debe_revisar:
                    exito, detalle, n_productos, correo_enviado = _enviar_alerta_stock_critico()
                    _registrar_alerta_stock_log(exito, detalle, n_productos, correo_enviado)
        except Exception:
            pass
        time.sleep(3600)  # revisa una vez por hora si ya toca el chequeo diario


_hilo_alertas_iniciado = False


def iniciar_hilo_alertas_stock():
    global _hilo_alertas_iniciado
    if not _hilo_alertas_iniciado:
        _hilo_alertas_iniciado = True
        hilo = threading.Thread(target=_hilo_alertas_stock, daemon=True)
        hilo.start()


@app.route("/api/alertas-stock/estado", methods=["GET"])
@requiere_rol("bodega")
def alertas_stock_estado():
    db = get_db()
    historial = db.execute(
        "SELECT * FROM alertas_stock_log ORDER BY fecha DESC LIMIT 10"
    ).fetchall()
    productos_criticos_ahora = _productos_stock_critico_para_alerta(db)
    return jsonify({
        "configurado": alerta_stock_configurada(),
        "destinatario": ALERTA_STOCK_DESTINATARIO if alerta_stock_configurada() else None,
        "intervalo_horas": ALERTA_STOCK_INTERVALO_HORAS,
        "n_productos_criticos_ahora": len(productos_criticos_ahora),
        "historial": [dict(r) for r in historial],
    })


@app.route("/api/alertas-stock/probar", methods=["POST"])
@requiere_rol("bodega")
def alertas_stock_probar():
    exito, detalle, n_productos, correo_enviado = _enviar_alerta_stock_critico(forzar_aunque_vacio=True)
    _registrar_alerta_stock_log(exito, detalle, n_productos, correo_enviado)
    if not exito:
        return jsonify({"error": detalle}), 400
    return jsonify({"ok": True, "detalle": detalle, "n_productos": n_productos})


# ---------------------------------------------------------------------------
# Recalculo periódico del stock mínimo (además del recálculo instantáneo que
# se dispara con cada movimiento de stock — esto es un respaldo diario para
# que productos que llevan tiempo sin moverse también reflejen el consumo
# más reciente, ya que la ventana de cálculo va "envejeciendo" datos aunque
# no haya un movimiento nuevo que dispare el recálculo puntual).
# ---------------------------------------------------------------------------

def _recalcular_stock_minimo_todos(db):
    ids = [r["id"] for r in db.execute("SELECT id FROM productos").fetchall()]
    for pid in ids:
        _recalcular_stock_minimo(db, pid)
    db.commit()
    return len(ids)


def _hilo_recalculo_stock_minimo():
    while True:
        try:
            db = _conectar_db()
            _recalcular_stock_minimo_todos(db)
            db.close()
        except Exception:
            pass
        time.sleep(24 * 3600)  # una vez al día alcanza: el recálculo instantáneo cubre lo urgente


_hilo_recalculo_iniciado = False


def iniciar_hilo_recalculo_stock_minimo():
    global _hilo_recalculo_iniciado
    if not _hilo_recalculo_iniciado:
        _hilo_recalculo_iniciado = True
        hilo = threading.Thread(target=_hilo_recalculo_stock_minimo, daemon=True)
        hilo.start()


@app.route("/api/planificacion/recalcular-todos", methods=["POST"])
@requiere_rol("bodega")
def planificacion_recalcular_todos():
    db = get_db()
    n = _recalcular_stock_minimo_todos(db)
    return jsonify({"ok": True, "n_productos": n})


# ---------------------------------------------------------------------------
# API: Movimientos - bitácora completa (Módulo 5)
# ---------------------------------------------------------------------------

@app.route("/api/movimientos", methods=["GET"])
@requiere_rol("bodega")
def listar_movimientos():
    db = get_db()

    tipo = request.args.get("tipo", "").strip()
    categoria = request.args.get("categoria", "").strip()
    producto_id = request.args.get("producto_id", "").strip()
    factura_numero = request.args.get("factura_numero", "").strip()
    desde = request.args.get("desde", "").strip()
    hasta = request.args.get("hasta", "").strip()
    q = request.args.get("q", "").strip()

    sql = """
        SELECT m.*, p.nombre AS producto_nombre, p.codigo_interno AS producto_codigo_interno
        FROM movimientos m
        LEFT JOIN productos p ON p.id = m.producto_id
        WHERE 1=1
    """
    params = []

    if tipo:
        sql += " AND m.tipo = ?"
        params.append(tipo)
    if categoria:
        sql += " AND m.categoria = ?"
        params.append(categoria)
    if producto_id:
        sql += " AND m.producto_id = ?"
        params.append(producto_id)
    if factura_numero:
        sql += " AND m.factura_numero = ?"
        params.append(factura_numero)
    if desde:
        sql += " AND m.fecha >= ?"
        params.append(desde)
    if hasta:
        sql += " AND m.fecha <= ?"
        params.append(hasta + "T23:59:59")

    # 'q' se filtra en Python con coincide_busqueda (ver core.py), no con SQL
    # LIKE — así admite varias palabras sueltas sin tildes que pueden calzar
    # en campos distintos (ej. "factura 1023 juan" encuentra un movimiento
    # con esa factura Y ese responsable, aunque estén en columnas separadas).

    sql += " ORDER BY m.fecha DESC, m.id DESC"
    rows = db.execute(sql, params).fetchall()
    movimientos = []
    for r in rows:
        d = dict(r)
        d["producto_id_sistema"] = f"{r['producto_id']:04d}" if r["producto_id"] is not None else "----"
        d["producto_nombre"] = d["producto_nombre"] or "(producto eliminado)"
        movimientos.append(d)

    if q:
        movimientos = [
            m for m in movimientos
            if coincide_busqueda(q, m.get("motivo"), m.get("usuario"), m.get("producto_nombre"), m.get("factura_numero"))
        ]

    # Cuántos movimientos calzan con el filtro EN TOTAL, antes del límite de
    # cuántas filas se muestran — se necesita para avisar si se está
    # mostrando solo una parte (el límite existe para no mandar, por
    # ejemplo, 40.000 filas de una sola vez).
    LIMITE_FILAS = 500
    total_sin_limite = len(movimientos)
    movimientos = movimientos[:LIMITE_FILAS]

    return jsonify({
        "movimientos": movimientos,
        "total": len(movimientos),
        "total_sin_limite": total_sin_limite,
        "limite_filas": LIMITE_FILAS,
        "truncado": total_sin_limite > LIMITE_FILAS,
        "categorias": sorted(set(CATEGORIAS_MOVIMIENTO) | {"Merma o pérdida"}),
        # Mismo criterio que usa el formulario de "Registrar movimiento": una
        # entrada no puede tener una categoría que es solo de salida (y
        # viceversa). El filtro de esta pantalla usa las mismas listas.
        "categorias_por_tipo": {
            "entrada": CATEGORIAS_ENTRADA,
            "salida": CATEGORIAS_SALIDA,
            "perdida": ["Merma o pérdida"],
        },
    })


# ---------------------------------------------------------------------------
# API: Mermas (cuenta separada de pérdidas)
# ---------------------------------------------------------------------------

@app.route("/api/productos/<int:producto_id>/proveedores-origen", methods=["GET"])
@requiere_rol("bodega")
def proveedores_origen_producto(producto_id):
    """Para el formulario de 'Registrar movimiento' cuando el tipo es
    Pérdida: solo deja elegir un proveedor que efectivamente le haya
    vendido este producto al taller (según las facturas de ingreso ya
    registradas), evitando así que alguien marque cualquier proveedor al
    azar. Si además hay entradas sin proveedor (ej. el stock inicial al
    crear el producto), se habilita la opción "sin proveedor conocido"."""
    db = get_db()
    producto = db.execute("SELECT id FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404
    proveedores, permite_sin_proveedor = _proveedores_origen_producto(db, producto_id)
    return jsonify({"proveedores": proveedores, "permite_sin_proveedor": permite_sin_proveedor})


@app.route("/api/productos/<int:producto_id>/stock-proveedores", methods=["GET"])
def stock_proveedores_producto(producto_id):
    """Desglose del stock DISPONIBLE ahora mismo por proveedor (tabla
    stock_proveedores) -- se usa para: (1) mostrarlo en la ficha del
    producto, y (2) para el selector obligatorio de "de qué proveedor salen
    estas unidades" al registrar una salida (venta, consumo interno,
    reserva, ajuste, devolución). A diferencia de /proveedores-origen (que
    mira todo el historial de compras, para la pérdida), esto solo lista
    proveedores con stock vivo."""
    db = get_db()
    producto = db.execute("SELECT id FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404
    baldes, permite_sin_proveedor = _stockprov_buckets_producto(db, producto_id)
    return jsonify({"proveedores": baldes, "permite_sin_proveedor": permite_sin_proveedor})


@app.route("/api/mermas", methods=["GET"])
@requiere_rol("bodega")
def listar_mermas():
    """Módulo 7: Mermas.

    Sin filtro (modo "actual"): muestra el saldo pendiente de revisión hoy,
    desglosado por proveedor (tabla merma_proveedores) — cada fila es un
    producto + el proveedor elegido al marcar esa pérdida, porque un mismo
    producto puede tener unidades en merma de más de un proveedor a la vez.

    Con ?mes=YYYY-MM (modo "mes"): muestra el historial real de pérdidas
    registradas ese mes (movimientos tipo 'perdida'), también desglosado por
    proveedor, aunque ya se hayan descartado después. El valor se calcula
    con el costo promedio actual del producto, porque no se guarda el costo
    exacto al momento de la pérdida. Las pérdidas registradas ANTES de que
    existiera esta función quedan como "proveedor no registrado" (no hay
    forma de reconstruir ese dato retroactivamente).
    """
    db = get_db()

    meses_disponibles = [
        r[0] for r in db.execute(
            "SELECT DISTINCT substr(fecha, 1, 7) AS mes FROM movimientos "
            "WHERE tipo = 'perdida' ORDER BY mes DESC"
        ).fetchall()
    ]

    mes = (request.args.get("mes") or "").strip()

    if mes:
        filas = db.execute(
            """
            SELECT producto_id, proveedor_id, SUM(cantidad) AS cantidad_mes, COUNT(*) AS n_movimientos
            FROM movimientos
            WHERE tipo = 'perdida' AND substr(fecha, 1, 7) = ?
            GROUP BY producto_id, proveedor_id
            """,
            (mes,),
        ).fetchall()

        productos = []
        for r in filas:
            prod = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (r["producto_id"],)).fetchone()
            if prod is None:
                continue
            proveedor_nombre = None
            if r["proveedor_id"] is not None:
                pr = db.execute("SELECT nombre FROM proveedores WHERE id = ?", (r["proveedor_id"],)).fetchone()
                proveedor_nombre = pr["nombre"] if pr else None
            d = row_to_dict(prod)
            d["proveedor_id"] = r["proveedor_id"]
            d["proveedor_nombre"] = proveedor_nombre or "Proveedor no registrado"
            d["cantidad_mes"] = r["cantidad_mes"]
            d["n_movimientos"] = r["n_movimientos"]
            d["valor_mes"] = r["cantidad_mes"] * d["costo_compra"]
            productos.append(d)
        productos.sort(key=lambda d: d["cantidad_mes"], reverse=True)

        total_unidades = sum(p["cantidad_mes"] for p in productos)
        total_valor = sum(p["valor_mes"] for p in productos)
        return jsonify({
            "modo": "mes",
            "mes": mes,
            "meses_disponibles": meses_disponibles,
            "productos": productos,
            "total_unidades": total_unidades,
            "total_valor": total_valor,
        })

    buckets = db.execute(
        """
        SELECT mp.producto_id, mp.proveedor_id, mp.cantidad, pr.nombre AS proveedor_nombre
        FROM merma_proveedores mp
        LEFT JOIN proveedores pr ON pr.id = mp.proveedor_id
        WHERE mp.cantidad > 0
        """
    ).fetchall()

    productos = []
    for b in buckets:
        prod = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (b["producto_id"],)).fetchone()
        if prod is None:
            continue
        d = row_to_dict(prod)
        d["proveedor_id"] = b["proveedor_id"]
        d["proveedor_nombre"] = b["proveedor_nombre"] or "Sin proveedor conocido"
        d["cantidad_bucket"] = b["cantidad"]
        d["valor_bucket"] = b["cantidad"] * d["costo_compra"]
        productos.append(d)
    productos.sort(key=lambda d: d["nombre"].lower())

    total_unidades = sum(p["cantidad_bucket"] for p in productos)
    total_valor = sum(p["valor_bucket"] for p in productos)
    return jsonify({
        "modo": "actual",
        "mes": "",
        "meses_disponibles": meses_disponibles,
        "productos": productos,
        "total_unidades": total_unidades,
        "total_valor": total_valor,
    })


@app.route("/api/productos/<int:producto_id>/merma/descartar", methods=["POST"])
@requiere_rol("bodega")
def descartar_merma(producto_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    try:
        proveedor_id = int(data["proveedor_id"]) if data.get("proveedor_id") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Proveedor inválido"}), 400
    try:
        cantidad = int(data.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número entero"}), 400
    if cantidad <= 0:
        return jsonify({"error": "La cantidad debe ser mayor a 0"}), 400

    bucket = _merma_bucket_get(db, producto_id, proveedor_id)
    disponible_bucket = bucket["cantidad"] if bucket else 0
    if cantidad > disponible_bucket:
        return jsonify({"error": f"Solo hay {disponible_bucket} unidad(es) en merma de este proveedor para este producto"}), 400

    nuevo_merma = producto["stock_merma"] - cantidad
    db.execute(
        "UPDATE productos SET stock_merma = ?, actualizado_en = ? WHERE id = ?",
        (nuevo_merma, now_iso(), producto_id),
    )
    _merma_bucket_sumar(db, producto_id, proveedor_id, -cantidad)
    db.commit()

    actualizado = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    return jsonify(row_to_dict(actualizado))


@app.route("/api/productos/<int:producto_id>/merma/revertir", methods=["POST"])
@requiere_rol("bodega")
def revertir_merma(producto_id):
    """Deshace una pérdida: devuelve unidades de stock_merma a
    stock_disponible (a diferencia de 'Descartar', que las elimina para
    siempre). Solo aplica sobre el saldo pendiente actual, igual que
    'Descartar' — no corrige historial de meses anteriores. Se indica de
    qué proveedor son las unidades a revertir (el mismo bucket elegido al
    marcar la pérdida), para no mezclar saldos de distintos proveedores."""
    db = get_db()
    _iniciar_transaccion_stock(db)
    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    try:
        proveedor_id = int(data["proveedor_id"]) if data.get("proveedor_id") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Proveedor inválido"}), 400
    try:
        cantidad = int(data.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número entero"}), 400
    if cantidad <= 0:
        return jsonify({"error": "La cantidad debe ser mayor a 0"}), 400

    bucket = _merma_bucket_get(db, producto_id, proveedor_id)
    disponible_bucket = bucket["cantidad"] if bucket else 0
    if cantidad > disponible_bucket:
        return jsonify({"error": f"Solo hay {disponible_bucket} unidad(es) en merma de este proveedor para este producto"}), 400

    ts = now_iso()
    nuevo_merma = producto["stock_merma"] - cantidad
    nuevo_disponible = producto["stock_disponible"] + cantidad
    db.execute(
        "UPDATE productos SET stock_merma = ?, stock_disponible = ?, actualizado_en = ? WHERE id = ?",
        (nuevo_merma, nuevo_disponible, ts, producto_id),
    )
    _merma_bucket_sumar(db, producto_id, proveedor_id, -cantidad)
    db.execute(
        """
        INSERT INTO movimientos (
            producto_id, fecha, tipo, categoria, cantidad, motivo, usuario,
            costo_unitario, stock_resultante, proveedor_id
        ) VALUES (?, ?, 'entrada', 'Ajuste de inventario', ?, ?, ?, ?, ?, ?)
        """,
        (
            producto_id, ts, cantidad,
            "Reversión de merma: se restituye a stock disponible",
            usuario_actual(), producto["costo_compra"], nuevo_disponible, proveedor_id,
        ),
    )
    _recalcular_stock_minimo(db, producto_id)
    db.commit()

    actualizado = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    return jsonify(row_to_dict(actualizado))


@app.route("/api/productos/<int:producto_id>/merma/origenes", methods=["GET"])
@requiere_rol("bodega")
def merma_origenes(producto_id):
    """Historial completo de compras (ingresos con factura) de este
    producto, para dar contexto — por ejemplo, para decidir a qué
    proveedor asignar una pérdida nueva. OJO: esto es solo informativo, no
    dice de qué proveedor son las unidades actualmente en merma (eso lo
    muestra la pestaña Mermas directamente, ya desglosado por proveedor)."""
    db = get_db()
    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    filas = db.execute(
        """
        SELECT m.id AS movimiento_id, m.fecha, m.cantidad, m.costo_unitario,
               m.factura_numero, f.id AS factura_id, f.fecha AS factura_fecha,
               pr.id AS proveedor_id, pr.nombre AS proveedor_nombre, pr.rut AS proveedor_rut
        FROM movimientos m
        LEFT JOIN facturas f ON f.numero = m.factura_numero
        LEFT JOIN proveedores pr ON pr.id = f.proveedor_id
        WHERE m.producto_id = ? AND m.tipo = 'entrada'
        ORDER BY m.fecha DESC
        """,
        (producto_id,),
    ).fetchall()

    origenes = [dict(r) for r in filas]
    proveedores_distintos = {
        o["proveedor_id"] for o in origenes if o["proveedor_id"] is not None
    }
    return jsonify({
        "producto_id": producto_id,
        "origenes": origenes,
        "n_proveedores_distintos": len(proveedores_distintos),
    })


# ---------------------------------------------------------------------------
# API: Caja Chica (Módulo 10)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# API: Dashboard (Módulo 1)
# ---------------------------------------------------------------------------

@app.route("/api/dashboard", methods=["GET"])
@requiere_rol("bodega")
def dashboard():
    db = get_db()

    resumen_row = dict(db.execute(
        """
        SELECT
            COUNT(*) AS total_productos,
            COALESCE(SUM(stock_disponible + stock_reservado), 0) AS total_unidades,
            COALESCE(SUM((stock_disponible + stock_reservado) * costo_compra), 0) AS valor_total,
            COALESCE(SUM(CASE WHEN stock_disponible <= stock_minimo THEN 1 ELSE 0 END), 0) AS stock_critico
        FROM productos
        """
    ).fetchone())

    stock_critico = [row_to_dict(r) for r in db.execute(
        PRODUCTO_SELECT + " WHERE p.stock_disponible <= p.stock_minimo ORDER BY (p.stock_disponible - p.stock_minimo) ASC LIMIT 8"
    ).fetchall()]

    sin_movimiento = [row_to_dict(r) for r in db.execute(
        PRODUCTO_SELECT + """
        WHERE p.id NOT IN (SELECT DISTINCT producto_id FROM movimientos)
        ORDER BY p.creado_en DESC LIMIT 8
        """
    ).fetchall()]

    mas_movidos_rows = db.execute(
        """
        SELECT m.producto_id, SUM(m.cantidad) AS total_salida
        FROM movimientos m
        WHERE m.tipo = 'salida' AND m.categoria != 'Corrección de inventario'
        GROUP BY m.producto_id
        ORDER BY total_salida DESC
        LIMIT 5
        """
    ).fetchall()
    mas_movidos = []
    for r in mas_movidos_rows:
        prod = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (r["producto_id"],)).fetchone()
        if prod:
            d = row_to_dict(prod)
            d["total_salida"] = r["total_salida"]
            mas_movidos.append(d)

    ultimos_movimientos = []
    for m in db.execute(
        "SELECT * FROM movimientos ORDER BY fecha DESC, id DESC LIMIT 10"
    ).fetchall():
        md = dict(m)
        prod = db.execute("SELECT id, nombre FROM productos WHERE id = ?", (md["producto_id"],)).fetchone()
        md["producto_nombre"] = prod["nombre"] if prod else "(producto eliminado)"
        md["producto_id_sistema"] = f"{prod['id']:04d}" if prod else "----"
        ultimos_movimientos.append(md)

    compras_recientes = db.execute(
        FACTURA_SELECT + " ORDER BY f.creado_en DESC LIMIT 5"
    ).fetchall()
    compras_recientes = [_factura_resumen(db, r) for r in compras_recientes]

    total_ubicaciones = db.execute("SELECT COUNT(*) FROM ubicaciones").fetchone()[0]
    ubicaciones_ocupadas = db.execute(
        "SELECT COUNT(DISTINCT ubicacion_id) FROM productos WHERE ubicacion_id IS NOT NULL"
    ).fetchone()[0]
    total_proveedores = db.execute("SELECT COUNT(*) FROM proveedores").fetchone()[0]
    facturas_pendientes = db.execute(
        """
        SELECT COUNT(DISTINCT f.id) FROM facturas f
        JOIN factura_lineas fl ON fl.factura_id = f.id
        WHERE fl.aplicada = 0
        """
    ).fetchone()[0]

    merma_row = db.execute(
        """
        SELECT
            COALESCE(SUM(stock_merma), 0) AS unidades,
            COALESCE(SUM(stock_merma * costo_compra), 0) AS valor
        FROM productos WHERE stock_merma > 0
        """
    ).fetchone()

    return jsonify({
        "resumen": resumen_row,
        "stock_critico": stock_critico,
        "sin_movimiento": sin_movimiento,
        "mas_movidos": mas_movidos,
        "ultimos_movimientos": ultimos_movimientos,
        "compras_recientes": compras_recientes,
        "merma": {"unidades": merma_row["unidades"], "valor": merma_row["valor"]},
        "bodega": {
            "total_ubicaciones": total_ubicaciones,
            "ubicaciones_ocupadas": ubicaciones_ocupadas,
            "total_proveedores": total_proveedores,
            "facturas_pendientes": facturas_pendientes,
        },
    })


@app.route("/api/logistica/dashboard/graficos", methods=["GET"])
@requiere_rol("bodega")
def logistica_dashboard_graficos():
    """Gráficos del dashboard de Logística: movimientos mensuales
    (entradas vs salidas, últimos 12 meses) y valor de inventario por
    modelo (top 8) — mismo patrón que /api/operativo/dashboard/graficos
    y /api/finanzas/resumen/graficos."""
    db = get_db()
    hoy = datetime.now()

    meses = []
    m = hoy.replace(day=1)
    for _ in range(12):
        meses.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    meses.reverse()

    movimientos_mensual = []
    for mes in meses:
        pref = f"{mes.year:04d}-{mes.month:02d}"
        entradas = db.execute(
            "SELECT COALESCE(SUM(cantidad), 0) FROM movimientos WHERE tipo = 'entrada' AND substr(fecha, 1, 7) = ?",
            (pref,),
        ).fetchone()[0]
        salidas = db.execute(
            "SELECT COALESCE(SUM(cantidad), 0) FROM movimientos WHERE tipo = 'salida' AND substr(fecha, 1, 7) = ?",
            (pref,),
        ).fetchone()[0]
        movimientos_mensual.append({"mes": pref, "entradas": entradas, "salidas": abs(salidas)})

    valor_por_categoria = [
        {"categoria": r["categoria"] or "(sin modelo)", "valor": r["valor"]}
        for r in db.execute(
            """
            SELECT categoria, SUM((stock_disponible + stock_reservado) * costo_compra) AS valor
            FROM productos
            GROUP BY categoria ORDER BY valor DESC LIMIT 8
            """
        ).fetchall()
    ]

    return jsonify({
        "movimientos_mensual": movimientos_mensual,
        "valor_por_categoria": valor_por_categoria,
    })


# ---------------------------------------------------------------------------
# API: Rotación (Módulo 8 - Control de Rotación)
# ---------------------------------------------------------------------------

def _calcular_rotacion(dias):
    """Módulo 8: Control de Rotación.

    En este taller casi no hay venta directa (todo es consumo interno), así
    que no se distingue "más vendidos" de "más utilizados": se mide toda
    salida de stock por igual (tipo = 'salida'), sin importar la categoría,
    dentro de la ventana de días que pida el usuario.

    Se separó del endpoint /api/rotacion para poder reutilizar el mismo
    cálculo también en el reporte exportable a Excel (Módulo 11).
    """
    db = get_db()
    desde = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")

    # ---- Más utilizados: mayor cantidad de salidas dentro del período.
    top_rows = db.execute(
        """
        SELECT producto_id, SUM(cantidad) AS total_salida_periodo, COUNT(*) AS n_movimientos
        FROM movimientos
        WHERE tipo = 'salida' AND categoria != 'Corrección de inventario' AND fecha >= ?
        GROUP BY producto_id
        ORDER BY total_salida_periodo DESC
        LIMIT 15
        """,
        (desde,),
    ).fetchall()

    # ---- Baja rotación: sí tuvieron salida en el período, pero poca — las
    # que aparecen al final del ranking general (no las que no tuvieron
    # ninguna, esas son "inmovilizados").
    todas_salidas = db.execute(
        """
        SELECT producto_id, SUM(cantidad) AS total_salida_periodo
        FROM movimientos
        WHERE tipo = 'salida' AND categoria != 'Corrección de inventario' AND fecha >= ?
        GROUP BY producto_id
        """,
        (desde,),
    ).fetchall()

    # ---- Se traen de una sola vez todos los productos que aparecen en
    # top_rows o en todas_salidas (en vez de una consulta por producto
    # dentro de cada loop), para no golpear la base con N consultas cuando
    # hay cientos de productos con movimiento en el período.
    ids_relevantes = {r["producto_id"] for r in top_rows} | {r["producto_id"] for r in todas_salidas}
    productos_por_id = {}
    if ids_relevantes:
        placeholders = ",".join("?" for _ in ids_relevantes)
        for prod in db.execute(
            PRODUCTO_SELECT + f" WHERE p.id IN ({placeholders})", tuple(ids_relevantes)
        ).fetchall():
            productos_por_id[prod["id"]] = prod

    mas_utilizados = []
    ids_con_salida = set()
    for r in top_rows:
        prod = productos_por_id.get(r["producto_id"])
        if prod:
            d = row_to_dict(prod)
            d["total_salida_periodo"] = r["total_salida_periodo"]
            d["n_movimientos"] = r["n_movimientos"]
            mas_utilizados.append(d)
            ids_con_salida.add(prod["id"])

    baja_rotacion = []
    for r in todas_salidas:
        prod = productos_por_id.get(r["producto_id"])
        if prod:
            d = row_to_dict(prod)
            d["total_salida_periodo"] = r["total_salida_periodo"]
            baja_rotacion.append(d)
    baja_rotacion.sort(key=lambda d: d["total_salida_periodo"])
    baja_rotacion = baja_rotacion[:15]

    # ---- Inmovilizados: ninguna salida en el período (puede que nunca
    # hayan tenido movimiento, o que su última salida sea más antigua).
    inmovilizados_rows = db.execute(
        f"""
        {PRODUCTO_SELECT}
        WHERE p.id NOT IN (
            SELECT DISTINCT producto_id FROM movimientos
            WHERE tipo = 'salida' AND categoria != 'Corrección de inventario' AND fecha >= ?
        )
        AND (p.stock_disponible + p.stock_reservado) > 0
        ORDER BY p.actualizado_en ASC
        LIMIT 20
        """,
        (desde,),
    ).fetchall()
    inmovilizados = [row_to_dict(r) for r in inmovilizados_rows]

    # ---- Tendencia: salidas agrupadas por semana dentro del período, para
    # graficar. Se arma primero la lista completa de semanas del período
    # (aunque no hayan tenido salidas, quedan en 0) para que el total y cada
    # producto compartan el mismo eje horizontal y las líneas se vean
    # continuas en el gráfico.
    semanas = []
    vistas = set()
    cursor = datetime.now() - timedelta(days=dias)
    fin = datetime.now()
    while cursor <= fin:
        clave = cursor.strftime("%Y-%W")
        if clave not in vistas:
            vistas.add(clave)
            semanas.append(clave)
        cursor += timedelta(days=7)
    clave_fin = fin.strftime("%Y-%W")
    if clave_fin not in vistas:
        semanas.append(clave_fin)

    tendencia_rows = db.execute(
        """
        SELECT strftime('%Y-%W', fecha) AS semana, SUM(cantidad) AS total
        FROM movimientos
        WHERE tipo = 'salida' AND categoria != 'Corrección de inventario' AND fecha >= ?
        GROUP BY semana
        """,
        (desde,),
    ).fetchall()
    totales_por_semana = {r["semana"]: r["total"] for r in tendencia_rows}

    def _valores_semanales(producto_id):
        filas = db.execute(
            """
            SELECT strftime('%Y-%W', fecha) AS semana, SUM(cantidad) AS total
            FROM movimientos
            WHERE tipo = 'salida' AND categoria != 'Corrección de inventario' AND producto_id = ? AND fecha >= ?
            GROUP BY semana
            """,
            (producto_id, desde),
        ).fetchall()
        mapa = {r["semana"]: r["total"] for r in filas}
        return [mapa.get(s, 0) for s in semanas]

    def _tendencia_desde_valores(valores):
        """Compara las últimas semanas con movimiento contra el bloque
        anterior de igual tamaño (una ventana reciente, no la mitad completa
        del período): si el período elegido es largo pero el producto solo
        tuvo salidas hace poco, comparar por mitades diluye esa señal con
        semanas vacías del principio. Con la ventana reciente se detecta
        igual tanto lo que viene subiendo como lo que ha ido dejando de
        salir últimamente."""
        n = len(valores)
        if n < 2:
            return "estable"
        ventana = max(1, min(4, n // 2))
        reciente = sum(valores[-ventana:])
        anterior = sum(valores[-2 * ventana:-ventana])
        if anterior == 0 and reciente == 0:
            return "estable"
        if reciente == 0 and anterior > 0:
            return "bajando"
        if anterior == 0 and reciente > 0:
            return "subiendo"
        if reciente >= anterior * 1.15:
            return "subiendo"
        if reciente <= anterior * 0.85:
            return "bajando"
        return "estable"

    # Se calcula la serie semanal de cada producto "más utilizado" una sola
    # vez: sirve tanto para el badge de tendencia como para las líneas del
    # gráfico (los 5 primeros).
    for d in mas_utilizados:
        valores = _valores_semanales(d["id"])
        d["tendencia"] = _tendencia_desde_valores(valores)
        d["_valores_semanales"] = valores

    tendencia_productos = [
        {
            "producto_id": p["id"],
            "id_sistema": p["id_sistema"],
            "nombre": p["nombre"],
            "categoria": p["categoria"],
            "modelo": p["modelo"],
            "valores": p["_valores_semanales"],
            "total_periodo": sum(p["_valores_semanales"]),
        }
        for p in mas_utilizados[:5]
    ]
    for p in mas_utilizados:
        p.pop("_valores_semanales", None)

    tendencia = {
        "semanas": semanas,
        "total": [totales_por_semana.get(s, 0) for s in semanas],
        "productos": tendencia_productos,
    }

    return {
        "dias": dias,
        "mas_utilizados": mas_utilizados,
        "baja_rotacion": baja_rotacion,
        "inmovilizados": inmovilizados,
        "tendencia": tendencia,
    }


@app.route("/api/rotacion", methods=["GET"])
@requiere_rol("bodega")
def rotacion():
    try:
        dias = int(request.args.get("dias", 90))
    except (TypeError, ValueError):
        dias = 90
    dias = max(7, min(dias, 730))
    return jsonify(_calcular_rotacion(dias))


# ---------------------------------------------------------------------------
# API: Planificación de compras asistida (Módulo 9)
# ---------------------------------------------------------------------------

def _calcular_planificacion(dias=60):
    """Para cada producto con stock mínimo definido, proyecta cuándo se va a
    quedar bajo el mínimo usando el promedio de salidas semanales recientes
    (mismo cálculo de ventana que Rotación), y sugiere cuánto comprar.

    También arma la lista inversa: productos con mucho stock y poco o ningún
    consumo reciente (plata inmovilizada en bodega).
    """
    db = get_db()
    desde = (datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds")

    productos = [row_to_dict(r) for r in db.execute(PRODUCTO_SELECT).fetchall()]

    salidas_rows = db.execute(
        """
        SELECT producto_id, SUM(cantidad) AS total, COUNT(DISTINCT strftime('%Y-%W', fecha)) AS n_semanas
        FROM movimientos
        WHERE tipo = 'salida' AND categoria != 'Corrección de inventario' AND fecha >= ?
        GROUP BY producto_id
        """,
        (desde,),
    ).fetchall()
    salidas_por_producto = {r["producto_id"]: r for r in salidas_rows}
    semanas_periodo = max(1, dias / 7.0)

    calculos = {r["producto_id"]: dict(r) for r in db.execute("SELECT * FROM stock_minimo_calculo").fetchall()}

    reponer = []
    exceso = []

    for p in productos:
        stock_minimo = p["stock_minimo"] or 0
        stock_actual = p["stock_disponible"]
        salida = salidas_por_producto.get(p["id"])
        total_salida = salida["total"] if salida else 0
        promedio_semanal = total_salida / semanas_periodo if total_salida else 0.0
        calculo_minimo = calculos.get(p["id"])

        if stock_minimo <= 0:
            # Sin mínimo definido: no se puede proyectar cuándo se va a
            # quedar bajo el mínimo, pero igual sirve para detectar exceso.
            if stock_actual > 0 and promedio_semanal == 0:
                d = dict(p)
                d["promedio_semanal"] = round(promedio_semanal, 2)
                d["semanas_sin_consumo"] = dias // 7
                d["stock_minimo_detalle"] = calculo_minimo
                exceso.append(d)
            continue

        d = dict(p)
        d["promedio_semanal"] = round(promedio_semanal, 2)
        d["stock_minimo_detalle"] = calculo_minimo

        if promedio_semanal > 0:
            semanas_para_minimo = (stock_actual - stock_minimo) / promedio_semanal
            dias_para_minimo = round(semanas_para_minimo * 7)
            d["dias_para_minimo"] = dias_para_minimo
            fecha_estim = datetime.now() + timedelta(days=dias_para_minimo)
            d["fecha_estimada_minimo"] = fecha_estim.strftime("%Y-%m-%d")
            # Sugerencia: cubrir 4 semanas de consumo por sobre el mínimo,
            # descontando lo que ya hay disponible.
            objetivo = stock_minimo + promedio_semanal * 4
            sugerido = max(0, round(objetivo - stock_actual))
            d["cantidad_sugerida"] = sugerido
            if stock_actual <= stock_minimo or dias_para_minimo <= 30:
                proveedores, permite_sin = _proveedores_origen_producto(db, p["id"])
                d["proveedores"] = proveedores
                d["urgencia"] = "critico" if stock_actual <= stock_minimo else "proximo"
                reponer.append(d)
        else:
            # Sin consumo en el período: no se puede proyectar una fecha,
            # pero si ya está bajo el mínimo igual amerita revisión manual.
            if stock_actual <= stock_minimo:
                proveedores, permite_sin = _proveedores_origen_producto(db, p["id"])
                d["proveedores"] = proveedores
                d["dias_para_minimo"] = None
                d["fecha_estimada_minimo"] = None
                d["cantidad_sugerida"] = max(0, stock_minimo * 2 - stock_actual)
                d["urgencia"] = "critico"
                reponer.append(d)
            elif stock_actual > stock_minimo * 2:
                d["semanas_sin_consumo"] = dias // 7
                exceso.append(d)

    reponer.sort(key=lambda d: (d["urgencia"] != "critico", d.get("dias_para_minimo") if d.get("dias_para_minimo") is not None else -1))
    exceso.sort(key=lambda d: d["stock_disponible"] * (d["costo_compra"] or 0), reverse=True)

    return {
        "dias": dias,
        "reponer": reponer,
        "exceso": exceso[:20],
    }


@app.route("/api/planificacion", methods=["GET"])
@requiere_rol("bodega")
def planificacion():
    try:
        dias = int(request.args.get("dias", 60))
    except (TypeError, ValueError):
        dias = 60
    dias = max(14, min(dias, 365))
    return jsonify(_calcular_planificacion(dias))


# ---------------------------------------------------------------------------
# API: Repuestos típicos por modelo, para las OTs abiertas ahora mismo
# ---------------------------------------------------------------------------
#
# Reemplaza al reporte anterior (que listaba, plano, todos los repuestos
# migrados de las OTs abiertas). Este es más confiable porque en vez de
# mostrar "lo que quedó cargado" nomás, cruza el modelo del auto de cada OT
# abierta con el patrón histórico de consumo de OTs YA CERRADAS de ese
# mismo modelo — como haría un mecánico con experiencia: "a las Explorer
# les suele tocar tal cosa, revisemos si a esta también".
#
# Sigue siendo puramente informativo (nunca escribe nada, nunca toca stock
# ni genera movimientos): el patrón histórico se arma en cada request desde
# orden_items/ordenes_trabajo/equipos, no queda guardado en ninguna tabla
# nueva.
#
# Tres filtros clave para que la sugerencia sea confiable y no ruido:
#   1) Solo se consideran los 10 modelos con más volumen histórico — con
#      pocos casos cualquier patrón es puro ruido estadístico.
#   2) Se descartan los repuestos "universales" que aparecen parejo en casi
#      cualquier modelo — si no se filtran, la lista de "repuestos
#      típicos" se vería igual para cualquier auto y no aportaría nada
#      específico. Se mide qué tan repartido está un repuesto entre TODOS
#      los modelos con historial: si aparece en más de UMBRAL_UNIVERSALIDAD
#      de ellos, se considera genérico y se excluye.
#   3) Se descartan además, por texto, los gastos operacionales que en el
#      sistema legado quedaron cargados como "repuesto" sin serlo de
#      verdad (bencina, diésel, peajes, estacionamiento, lavado) — no son
#      autopartes ligadas a un modelo, son costos de logística/traslado que
#      pueden aparecer parejo o no según la muestra, así que el filtro
#      estadístico del punto 2 no los pesca siempre (con poca data, un
#      gasto así puede parecer "típico" de un modelo por pura casualidad).
#      Se identifican por prefijo porque en la data real ese patrón separa
#      bien la compra de combustible/peaje (arranca con la palabra) de una
#      reparación real que la menciona de pasada (ej. "REPARACIÓN DE BOMBA
#      DE BENCINA", que si es un repuesto/trabajo real y no se quiere
#      perder).
PREFIJOS_GASTO_OPERACIONAL = (
    "BENCINA", "DIESEL", "PETROLEO", "GASOLINA", "COMBUSTIBLE",
    "PEAJE", "ESTACIONAMIENTO", "LAVADO",
)
UMBRAL_UNIVERSALIDAD_REPUESTO = 0.35


def _es_gasto_operacional(descripcion):
    return descripcion.startswith(PREFIJOS_GASTO_OPERACIONAL)


def _normalizar_modelo_auto(modelo):
    """'X-TRAIL' y 'XTRAIL' son el mismo auto escrito distinto a mano — se
    normaliza sacando todo lo que no sea letra/número para agruparlos."""
    return re.sub(r"[^A-Z0-9]", "", (modelo or "").upper())


def _top_modelos_por_volumen(db, n=10):
    filas = db.execute("SELECT id, marca, modelo FROM equipos WHERE modelo != ''").fetchall()
    grupos = {}
    for f in filas:
        modelo_norm = _normalizar_modelo_auto(f["modelo"])
        if not modelo_norm:
            continue
        clave = ((f["marca"] or "").strip().upper(), modelo_norm)
        g = grupos.setdefault(clave, {"marca": clave[0], "modelo_norm": clave[1], "equipo_ids": [], "conteo_display": {}})
        g["equipo_ids"].append(f["id"])
        disp = (f["modelo"] or "").strip().upper()
        g["conteo_display"][disp] = g["conteo_display"].get(disp, 0) + 1

    lista = list(grupos.values())
    for g in lista:
        g["modelo_display"] = max(g["conteo_display"], key=g["conteo_display"].get)
        g["total_autos"] = len(g["equipo_ids"])
        del g["conteo_display"]
    lista.sort(key=lambda g: g["total_autos"], reverse=True)
    return lista[:n]


def _universalidad_repuestos(db):
    """Para cada descripción de repuesto, en cuántos modelos DISTINTOS
    aparece (sobre el total de modelos con historial) — la base para
    detectar y excluir los genéricos."""
    filas = db.execute(
        """
        SELECT oi.descripcion, e.marca, e.modelo
        FROM orden_items oi
        JOIN ordenes_trabajo o ON o.id = oi.orden_id
        JOIN equipos e ON e.id = o.equipo_id
        WHERE oi.tipo = 'repuesto' AND e.modelo != ''
        """
    ).fetchall()
    modelos_por_descripcion = {}
    todos_modelos = set()
    for f in filas:
        modelo_norm = _normalizar_modelo_auto(f["modelo"])
        if not modelo_norm:
            continue
        clave_modelo = ((f["marca"] or "").strip().upper(), modelo_norm)
        todos_modelos.add(clave_modelo)
        desc = (f["descripcion"] or "").strip().upper()
        if not desc:
            continue
        modelos_por_descripcion.setdefault(desc, set()).add(clave_modelo)
    return modelos_por_descripcion, (len(todos_modelos) or 1)


def _sugerir_producto_por_descripcion(descripcion, productos_idx):
    for p in productos_idx:
        if p["codigo_interno"] and len(p["codigo_interno"]) >= 3 and p["codigo_interno"] in descripcion:
            return p["id"]
    for p in productos_idx:
        if p["nombre"] and len(p["nombre"]) >= 4 and p["nombre"] in descripcion:
            return p["id"]
    return None


def _repuestos_tipicos_modelo(db, equipo_ids, modelos_por_descripcion, total_modelos, top_n=6):
    """Repuestos que más se repiten en OTs YA CERRADAS de este grupo de
    autos (mismo modelo normalizado), sacando los genéricos."""
    if not equipo_ids:
        return []
    placeholders_equipos = ", ".join("?" for _ in equipo_ids)
    placeholders_cerrados = ", ".join("?" for _ in ESTADOS_CERRADOS)
    filas = db.execute(
        f"""
        SELECT oi.descripcion, oi.costo_unitario, o.id AS orden_id
        FROM orden_items oi
        JOIN ordenes_trabajo o ON o.id = oi.orden_id
        WHERE oi.tipo = 'repuesto' AND o.equipo_id IN ({placeholders_equipos})
          AND o.estado IN ({placeholders_cerrados})
        """,
        list(equipo_ids) + list(ESTADOS_CERRADOS),
    ).fetchall()

    agrupado = {}
    for f in filas:
        desc = (f["descripcion"] or "").strip().upper()
        if not desc or _es_gasto_operacional(desc):
            continue
        acc = agrupado.setdefault(desc, {"descripcion": desc, "ordenes": set(), "costo_total": 0.0, "n": 0})
        acc["ordenes"].add(f["orden_id"])
        acc["costo_total"] += f["costo_unitario"] or 0
        acc["n"] += 1

    resultado = []
    for desc, acc in agrupado.items():
        n_modelos = len(modelos_por_descripcion.get(desc, ()))
        if (n_modelos / total_modelos) > UMBRAL_UNIVERSALIDAD_REPUESTO:
            continue  # genérico (aparece parejo en muchos modelos), no distingue a este
        resultado.append({
            "descripcion": desc,
            "en_ots_cerradas": len(acc["ordenes"]),
            "costo_promedio": round(acc["costo_total"] / acc["n"]) if acc["n"] else 0,
        })
    resultado.sort(key=lambda r: r["en_ots_cerradas"], reverse=True)
    return resultado[:top_n]


@app.route("/api/logistica/repuestos-tipicos-modelos", methods=["GET"])
@requiere_rol("bodega")
def logistica_repuestos_tipicos_modelos():
    db = get_db()
    placeholders_cerrados = ", ".join("?" for _ in ESTADOS_CERRADOS)

    top_modelos = _top_modelos_por_volumen(db, n=10)
    modelos_por_descripcion, total_modelos = _universalidad_repuestos(db)
    productos_idx = [
        {"id": p["id"], "nombre": (p["nombre"] or "").upper(), "codigo_interno": (p["codigo_interno"] or "").upper()}
        for p in db.execute("SELECT id, nombre, codigo_interno FROM productos").fetchall()
    ]

    resultado = []
    for g in top_modelos:
        equipo_ids = g["equipo_ids"]
        placeholders_equipos = ", ".join("?" for _ in equipo_ids)
        ots_abiertas = [dict(o) for o in db.execute(
            f"""
            SELECT o.id AS orden_id, c.nombre AS cliente_nombre
            FROM ordenes_trabajo o
            LEFT JOIN clientes c ON c.id = o.cliente_id
            WHERE o.equipo_id IN ({placeholders_equipos}) AND o.estado NOT IN ({placeholders_cerrados})
            """,
            equipo_ids + list(ESTADOS_CERRADOS),
        ).fetchall()]
        if not ots_abiertas:
            # Solo interesan los modelos que tienen algún auto en el taller
            # AHORA MISMO — un modelo con harto historial pero sin ninguna
            # OT abierta hoy no aporta nada accionable en este reporte.
            continue

        repuestos = _repuestos_tipicos_modelo(db, equipo_ids, modelos_por_descripcion, total_modelos)
        ids_sugeridos = set()
        for r in repuestos:
            r["producto_id_sugerido"] = _sugerir_producto_por_descripcion(r["descripcion"], productos_idx)
            if r["producto_id_sugerido"]:
                ids_sugeridos.add(r["producto_id_sugerido"])

        productos_detalle = {}
        if ids_sugeridos:
            marcas = ",".join("?" * len(ids_sugeridos))
            for p in db.execute(
                f"SELECT id, nombre, codigo_interno, stock_disponible, stock_minimo FROM productos WHERE id IN ({marcas})",
                tuple(ids_sugeridos),
            ).fetchall():
                productos_detalle[p["id"]] = dict(p)
        for r in repuestos:
            r["producto_sugerido"] = productos_detalle.get(r.pop("producto_id_sugerido"))

        resultado.append({
            "marca": g["marca"],
            "modelo": g["modelo_display"],
            "total_autos_historico": g["total_autos"],
            "ots_abiertas": ots_abiertas,
            "repuestos_tipicos": repuestos,
        })

    resultado.sort(key=lambda m: len(m["ots_abiertas"]), reverse=True)
    return jsonify({"modelos": resultado})


# ---------------------------------------------------------------------------
# API: Reportes y Exportaciones (Módulo 11)
# ---------------------------------------------------------------------------


@app.route("/api/reportes/inventario", methods=["GET"])
@requiere_rol("bodega")
def reporte_inventario():
    """Módulo 11: Inventario completo y valorizado, exportado a Excel."""
    db = get_db()
    rows = db.execute(PRODUCTO_SELECT + " ORDER BY p.nombre COLLATE NOCASE").fetchall()
    productos = [row_to_dict(r) for r in rows]

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Inventario valorizado"

    valor_general_previo = sum(p["stock_total"] * p["costo_compra"] for p in productos)
    _excel_resumen(ws, "Inventario completo y valorizado", [
        ("Productos", len(productos)),
        ("Valor total del inventario", f"${valor_general_previo:,.0f}".replace(",", ".")),
    ])

    encabezados = [
        "N° sistema", "Código interno", "Modelo", "Detalle", "Ubicación",
        "Stock disponible", "Stock reservado", "Stock en merma", "Stock total",
        "Stock mínimo", "Costo promedio", "Valor total",
    ]
    fila_encabezado = ws.max_row + 1
    ws.append(encabezados)

    valor_general = 0
    for p in productos:
        valor = p["stock_total"] * p["costo_compra"]
        valor_general += valor
        ws.append([
            p["id_sistema"], p["codigo_interno"], p["categoria"], p["modelo"],
            p["ubicacion_codigo"] or "", p["stock_disponible"], p["stock_reservado"],
            p["stock_merma"], p["stock_total"], p["stock_minimo"],
            round(p["costo_compra"], 2), round(valor, 2),
        ])

    ws.append([])
    ws.append(["", "", "", "", "", "", "", "", "", "", "TOTAL", round(valor_general, 2)])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    # Segunda hoja: valorizado agrupado por modelo, para ver dónde está
    # concentrado el dinero invertido en inventario.
    ws2 = wb.create_sheet("Por modelo")
    ws2.append(["Modelo", "N° de productos", "Unidades totales", "Valor total"])
    por_categoria = {}
    for p in productos:
        cat = p["categoria"] or "(sin modelo)"
        acc = por_categoria.setdefault(cat, {"n": 0, "unidades": 0, "valor": 0})
        acc["n"] += 1
        acc["unidades"] += p["stock_total"]
        acc["valor"] += p["stock_total"] * p["costo_compra"]
    for cat, acc in sorted(por_categoria.items(), key=lambda kv: kv[1]["valor"], reverse=True):
        ws2.append([cat, acc["n"], acc["unidades"], round(acc["valor"], 2)])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    nombre = f"inventario_valorizado_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@app.route("/api/reportes/stock", methods=["GET"])
@requiere_rol("bodega")
def reporte_stock():
    """Módulo 11: Stock crítico y productos sin movimiento, a Excel."""
    db = get_db()

    criticos = [row_to_dict(r) for r in db.execute(
        PRODUCTO_SELECT + " WHERE p.stock_disponible <= p.stock_minimo ORDER BY (p.stock_disponible - p.stock_minimo) ASC"
    ).fetchall()]

    sin_movimiento = [row_to_dict(r) for r in db.execute(
        PRODUCTO_SELECT + """
        WHERE p.id NOT IN (SELECT DISTINCT producto_id FROM movimientos)
        ORDER BY p.creado_en DESC
        """
    ).fetchall()]

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Stock crítico"
    _excel_resumen(ws, "Stock crítico y sin movimiento", [
        ("Productos con stock crítico", len(criticos)),
        ("Productos sin ningún movimiento", len(sin_movimiento)),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["N° sistema", "Producto", "Modelo", "Stock disponible", "Stock mínimo", "Diferencia", "Ubicación"])
    for p in criticos:
        ws.append([
            p["id_sistema"], p["nombre"], p["categoria"], p["stock_disponible"],
            p["stock_minimo"], p["stock_disponible"] - p["stock_minimo"], p["ubicacion_codigo"] or "",
        ])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    ws2 = wb.create_sheet("Sin movimiento")
    ws2.append(["N° sistema", "Producto", "Modelo", "Stock total", "Valor", "Creado el"])
    for p in sin_movimiento:
        ws2.append([
            p["id_sistema"], p["nombre"], p["categoria"], p["stock_total"],
            round(p["stock_total"] * p["costo_compra"], 2), p["creado_en"],
        ])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    nombre = f"stock_critico_sin_movimiento_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@app.route("/api/reportes/rotacion", methods=["GET"])
@requiere_rol("bodega")
def reporte_rotacion():
    """Módulo 11: Rotación y consumo por período, a Excel (Módulo 8 + 11)."""
    try:
        dias = int(request.args.get("dias", 90))
    except (TypeError, ValueError):
        dias = 90
    dias = max(7, min(dias, 730))
    datos = _calcular_rotacion(dias)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Más utilizados"
    _excel_resumen(ws, f"Rotación y consumo — últimos {dias} días", [
        ("Más utilizados", len(datos["mas_utilizados"])),
        ("Baja rotación", len(datos["baja_rotacion"])),
        ("Inmovilizados", len(datos["inmovilizados"])),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["N° sistema", "Producto", "Modelo", "Salidas en el período", "N° movimientos", "Tendencia"])
    for p in datos["mas_utilizados"]:
        ws.append([p["id_sistema"], p["nombre"], p["categoria"], p["total_salida_periodo"], p["n_movimientos"], p["tendencia"]])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    ws2 = wb.create_sheet("Baja rotación")
    ws2.append(["N° sistema", "Producto", "Modelo", "Salidas en el período"])
    for p in datos["baja_rotacion"]:
        ws2.append([p["id_sistema"], p["nombre"], p["categoria"], p["total_salida_periodo"]])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    ws3 = wb.create_sheet("Inmovilizados")
    ws3.append(["N° sistema", "Producto", "Modelo", "Stock total", "Valor"])
    for p in datos["inmovilizados"]:
        ws3.append([p["id_sistema"], p["nombre"], p["categoria"], p["stock_total"], round(p["stock_total"] * p["costo_compra"], 2)])
    _estilo_encabezado(ws3)
    _autoancho(ws3)

    ws4 = wb.create_sheet("Tendencia semanal")
    encabezado4 = ["Semana", "Total general"] + [f"#{p['id_sistema']} {p['nombre']}" for p in datos["tendencia"]["productos"]]
    ws4.append(encabezado4)
    semanas = datos["tendencia"]["semanas"]
    total = datos["tendencia"]["total"]
    for i, semana in enumerate(semanas):
        fila = [semana, total[i]] + [p["valores"][i] for p in datos["tendencia"]["productos"]]
        ws4.append(fila)
    _estilo_encabezado(ws4)
    _autoancho(ws4)

    nombre = f"rotacion_{dias}dias_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


# ---------------------------------------------------------------------------
# API: Reportes — versión PDF (mismos 3 reportes que Excel, para imprimir o
# mostrarle a alguien sin necesidad de abrir una planilla).
# ---------------------------------------------------------------------------



@app.route("/api/reportes/inventario/pdf", methods=["GET"])
@requiere_rol("bodega")
def reporte_inventario_pdf():
    db = get_db()
    rows = db.execute(PRODUCTO_SELECT + " ORDER BY p.nombre COLLATE NOCASE").fetchall()
    productos = [row_to_dict(r) for r in rows]

    buffer, doc, elementos, estilos = _pdf_documento("Inventario completo y valorizado")

    valor_general = sum(p["stock_total"] * p["costo_compra"] for p in productos)
    _pdf_resumen(elementos, estilos, [
        ("Productos", len(productos)),
        ("Valor total del inventario", fmt_money_pdf(valor_general)),
    ])
    filas = [
        [p["id_sistema"], p["codigo_interno"], p["categoria"], p["modelo"],
         p["ubicacion_codigo"] or "—", p["stock_disponible"], p["stock_reservado"],
         p["stock_merma"], p["stock_total"], fmt_money_pdf(p["costo_compra"]), fmt_money_pdf(p["stock_total"] * p["costo_compra"])]
        for p in productos
    ]
    _pdf_tabla(
        elementos, estilos, f"Detalle por producto — valor total: {fmt_money_pdf(valor_general)}",
        ["N°", "Código", "Modelo", "Detalle", "Ubicación", "Disp.", "Reserv.", "Merma", "Total", "Costo prom.", "Valor"],
        filas,
    )

    por_categoria = {}
    for p in productos:
        cat = p["categoria"] or "(sin modelo)"
        acc = por_categoria.setdefault(cat, {"n": 0, "unidades": 0, "valor": 0})
        acc["n"] += 1
        acc["unidades"] += p["stock_total"]
        acc["valor"] += p["stock_total"] * p["costo_compra"]
    filas_cat = [
        [cat, acc["n"], acc["unidades"], fmt_money_pdf(acc["valor"])]
        for cat, acc in sorted(por_categoria.items(), key=lambda kv: kv[1]["valor"], reverse=True)
    ]
    _pdf_tabla(elementos, estilos, "Valorizado por modelo", ["Modelo", "N° productos", "Unidades", "Valor"], filas_cat)

    nombre = f"inventario_valorizado_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


@app.route("/api/reportes/stock/pdf", methods=["GET"])
@requiere_rol("bodega")
def reporte_stock_pdf():
    db = get_db()
    criticos = [row_to_dict(r) for r in db.execute(
        PRODUCTO_SELECT + " WHERE p.stock_disponible <= p.stock_minimo ORDER BY (p.stock_disponible - p.stock_minimo) ASC"
    ).fetchall()]
    sin_movimiento = [row_to_dict(r) for r in db.execute(
        PRODUCTO_SELECT + """
        WHERE p.id NOT IN (SELECT DISTINCT producto_id FROM movimientos)
        ORDER BY p.creado_en DESC
        """
    ).fetchall()]

    buffer, doc, elementos, estilos = _pdf_documento("Stock crítico y productos sin movimiento")
    _pdf_resumen(elementos, estilos, [
        ("Productos con stock crítico", len(criticos)),
        ("Productos sin ningún movimiento", len(sin_movimiento)),
    ])

    filas_criticos = [
        [p["id_sistema"], p["nombre"], p["categoria"], p["stock_disponible"], p["stock_minimo"],
         p["stock_disponible"] - p["stock_minimo"], p["ubicacion_codigo"] or "—"]
        for p in criticos
    ]
    _pdf_tabla(
        elementos, estilos, f"Stock crítico ({len(criticos)} producto(s))",
        ["N°", "Producto", "Modelo", "Disponible", "Mínimo", "Diferencia", "Ubicación"],
        filas_criticos,
    )

    filas_sin_mov = [
        [p["id_sistema"], p["nombre"], p["categoria"], p["stock_total"], fmt_money_pdf(p["stock_total"] * p["costo_compra"]), p["creado_en"][:10]]
        for p in sin_movimiento
    ]
    _pdf_tabla(
        elementos, estilos, f"Sin ningún movimiento registrado ({len(sin_movimiento)} producto(s))",
        ["N°", "Producto", "Modelo", "Stock total", "Valor", "Creado el"],
        filas_sin_mov,
    )

    nombre = f"stock_critico_sin_movimiento_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


@app.route("/api/reportes/rotacion/pdf", methods=["GET"])
@requiere_rol("bodega")
def reporte_rotacion_pdf():
    try:
        dias = int(request.args.get("dias", 90))
    except (TypeError, ValueError):
        dias = 90
    dias = max(7, min(dias, 730))
    datos = _calcular_rotacion(dias)

    buffer, doc, elementos, estilos = _pdf_documento(f"Rotación y consumo — últimos {dias} días")
    _pdf_resumen(elementos, estilos, [
        ("Más utilizados", len(datos["mas_utilizados"])),
        ("Baja rotación", len(datos["baja_rotacion"])),
        ("Inmovilizados", len(datos["inmovilizados"])),
    ])

    _pdf_tabla(
        elementos, estilos, "Más utilizados",
        ["N°", "Producto", "Modelo", "Salidas", "N° mov.", "Tendencia"],
        [[p["id_sistema"], p["nombre"], p["categoria"], p["total_salida_periodo"], p["n_movimientos"], p["tendencia"]]
         for p in datos["mas_utilizados"]],
    )
    _pdf_tabla(
        elementos, estilos, "Baja rotación",
        ["N°", "Producto", "Modelo", "Salidas en el período"],
        [[p["id_sistema"], p["nombre"], p["categoria"], p["total_salida_periodo"]] for p in datos["baja_rotacion"]],
    )
    _pdf_tabla(
        elementos, estilos, "Inmovilizados (sin salidas en el período)",
        ["N°", "Producto", "Modelo", "Stock total", "Valor"],
        [[p["id_sistema"], p["nombre"], p["categoria"], p["stock_total"], fmt_money_pdf(p["stock_total"] * p["costo_compra"])]
         for p in datos["inmovilizados"]],
    )

    nombre = f"rotacion_{dias}dias_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


# ---------------------------------------------------------------------------
# API: Reportes adicionales — Movimientos, Compras y Gastos de Caja Chica
# (los otros reportes que pide la planilla original, además de los 3 ya
# construidos). Todos aceptan ?desde=YYYY-MM-DD&hasta=YYYY-MM-DD opcional.
# ---------------------------------------------------------------------------



def _movimientos_para_reporte(desde, hasta):
    db = get_db()
    sql = """
        SELECT m.*, p.nombre AS producto_nombre, p.codigo_interno AS producto_codigo_interno
        FROM movimientos m
        LEFT JOIN productos p ON p.id = m.producto_id
        WHERE 1=1
    """
    params = []
    if desde:
        sql += " AND m.fecha >= ?"
        params.append(desde)
    if hasta:
        sql += " AND m.fecha <= ?"
        params.append(hasta + "T23:59:59")
    sql += " ORDER BY m.fecha DESC, m.id DESC"
    return db.execute(sql, params).fetchall()


@app.route("/api/reportes/movimientos", methods=["GET"])
@requiere_rol("bodega")
def reporte_movimientos():
    desde, hasta = _rango_fechas()
    filas_db = _movimientos_para_reporte(desde, hasta)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Movimientos"
    rango_txt = f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"
    _excel_resumen(ws, "Movimientos de inventario", [
        ("Rango", rango_txt),
        ("Total de movimientos", len(filas_db)),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["Fecha", "Tipo", "Categoría", "Producto", "Cantidad", "Costo unit.", "Motivo", "Usuario", "Factura", "Stock resultante"])
    for m in filas_db:
        ws.append([
            m["fecha"], m["tipo"], m["categoria"] or "", m["producto_nombre"] or "(producto eliminado)",
            m["cantidad"], m["costo_unitario"], m["motivo"] or "", m["usuario"] or "",
            m["factura_numero"] or "", m["stock_resultante"],
        ])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"movimientos{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@app.route("/api/reportes/movimientos/pdf", methods=["GET"])
@requiere_rol("bodega")
def reporte_movimientos_pdf():
    desde, hasta = _rango_fechas()
    filas_db = _movimientos_para_reporte(desde, hasta)

    rango_txt = f" ({desde or '…'} a {hasta or '…'})" if (desde or hasta) else ""
    buffer, doc, elementos, estilos = _pdf_documento(f"Movimientos de inventario{rango_txt}")
    _pdf_resumen(elementos, estilos, [("Total de movimientos", len(filas_db))])

    filas = [
        [m["fecha"][:16].replace("T", " "), m["tipo"], m["categoria"] or "—",
         m["producto_nombre"] or "(producto eliminado)", m["cantidad"], m["motivo"] or "—", m["usuario"] or "—"]
        for m in filas_db
    ]
    _pdf_tabla(
        elementos, estilos, f"Total: {len(filas)} movimiento(s)",
        ["Fecha", "Tipo", "Categoría", "Producto", "Cant.", "Motivo", "Usuario"],
        filas,
    )

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"movimientos{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


def _facturas_para_reporte(desde, hasta):
    db = get_db()
    sql = FACTURA_SELECT + " WHERE 1=1"
    params = []
    if desde:
        sql += " AND f.fecha >= ?"
        params.append(desde)
    if hasta:
        sql += " AND f.fecha <= ?"
        params.append(hasta)
    sql += " ORDER BY f.fecha DESC, f.id DESC"
    facturas = db.execute(sql, params).fetchall()
    resultado = []
    for f in facturas:
        resumen = _factura_resumen(db, f)
        total = db.execute(
            "SELECT COALESCE(SUM(cantidad * precio_unitario), 0) FROM factura_lineas WHERE factura_id = ?",
            (f["id"],),
        ).fetchone()[0]
        resumen["total"] = total
        resultado.append(resumen)
    return resultado


@app.route("/api/reportes/compras", methods=["GET"])
@requiere_rol("bodega")
def reporte_compras():
    desde, hasta = _rango_fechas()
    facturas = _facturas_para_reporte(desde, hasta)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Compras"
    monto_previo = sum(f["total"] for f in facturas)
    rango_txt = f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"
    _excel_resumen(ws, "Compras realizadas", [
        ("Rango", rango_txt),
        ("Facturas", len(facturas)),
        ("Monto total", f"${monto_previo:,.0f}".replace(",", ".")),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["Fecha", "N° factura", "Proveedor", "N° líneas", "Líneas aplicadas", "Total"])
    total_general = 0
    for f in facturas:
        total_general += f["total"]
        ws.append([f["fecha"], f["numero"], f["proveedor_display"], f["total_lineas"], f["lineas_aplicadas"], round(f["total"], 2)])
    ws.append([])
    ws.append(["", "", "", "", "TOTAL", round(total_general, 2)])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"compras{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@app.route("/api/reportes/compras/pdf", methods=["GET"])
@requiere_rol("bodega")
def reporte_compras_pdf():
    desde, hasta = _rango_fechas()
    facturas = _facturas_para_reporte(desde, hasta)

    rango_txt = f" ({desde or '…'} a {hasta or '…'})" if (desde or hasta) else ""
    buffer, doc, elementos, estilos = _pdf_documento(f"Compras realizadas{rango_txt}")

    total_general = sum(f["total"] for f in facturas)
    _pdf_resumen(elementos, estilos, [
        ("Facturas", len(facturas)),
        ("Monto total", fmt_money_pdf(total_general)),
    ])
    filas = [
        [f["fecha"], f["numero"], f["proveedor_display"] or "—", f["total_lineas"], fmt_money_pdf(f["total"])]
        for f in facturas
    ]
    _pdf_tabla(
        elementos, estilos, f"Total facturado: {fmt_money_pdf(total_general)} — {len(facturas)} factura(s)",
        ["Fecha", "N° factura", "Proveedor", "N° líneas", "Total"],
        filas,
    )

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"compras{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


@app.route("/api/reportes/resumen", methods=["GET"])
@requiere_rol("bodega")
def reportes_resumen():
    """Contadores rápidos de qué trae cada reporte (Módulo 11), para
    mostrarlos en la pestaña Reportes ANTES de descargar el archivo — así se
    sabe de antemano si va a traer 3 filas o 3.000, sin tener que abrirlo."""
    db = get_db()

    total_productos = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    valor_inventario = db.execute(
        "SELECT COALESCE(SUM((stock_disponible + stock_reservado) * costo_compra), 0) FROM productos"
    ).fetchone()[0]

    criticos = db.execute("SELECT COUNT(*) FROM productos WHERE stock_disponible <= stock_minimo").fetchone()[0]
    sin_movimiento = db.execute(
        "SELECT COUNT(*) FROM productos WHERE id NOT IN (SELECT DISTINCT producto_id FROM movimientos)"
    ).fetchone()[0]

    try:
        dias = int(request.args.get("dias", 90))
    except (TypeError, ValueError):
        dias = 90
    dias = max(7, min(dias, 730))
    datos_rotacion = _calcular_rotacion(dias)

    mov_desde = (request.args.get("mov_desde") or "").strip()
    mov_hasta = (request.args.get("mov_hasta") or "").strip()
    total_movimientos = len(_movimientos_para_reporte(mov_desde, mov_hasta))

    compras_desde = (request.args.get("compras_desde") or "").strip()
    compras_hasta = (request.args.get("compras_hasta") or "").strip()
    facturas = _facturas_para_reporte(compras_desde, compras_hasta)

    return jsonify({
        "inventario": {"productos": total_productos, "valor_total": round(valor_inventario, 2)},
        "stock": {"criticos": criticos, "sin_movimiento": sin_movimiento},
        "rotacion": {
            "dias": dias,
            "mas_utilizados": len(datos_rotacion["mas_utilizados"]),
            "baja_rotacion": len(datos_rotacion["baja_rotacion"]),
            "inmovilizados": len(datos_rotacion["inmovilizados"]),
        },
        "movimientos": {"total": total_movimientos},
        "compras": {"facturas": len(facturas), "monto_total": round(sum(f["total"] for f in facturas), 2)},
    })


# ---------------------------------------------------------------------------
# Módulos separados (ver modulos/) — primer paso hacia la arquitectura de
# ERP: Finanzas (que incluye Caja Chica) ya vive en su propio archivo como
# Blueprint de Flask, en vez de estar mezclado acá. Se importa recién al
# final del archivo, después de que todas las funciones que ese módulo
# necesita (get_db, _aplicar_entrada_stock, los helpers de reportes, etc.)
# ya están definidas arriba — así se evita un import circular.
# ---------------------------------------------------------------------------
from modulos.finanzas import (  # noqa: E402
    bp as bp_finanzas,
    _cxc_pendiente_y_vencido,
    _cxp_pendiente_y_vencido,
    RUBROS_GAV_QUE_SUMAN_GAV,
    _rubro_gav_valido,
)
app.register_blueprint(bp_finanzas)

from modulos.operativo import (  # noqa: E402
    bp as bp_operativo,
    init_operativo_db,
    ESTADOS_ORDEN,
    ESTADOS_LABEL,
    ESTADOS_CERRADOS,
    ESTADOS_QUE_CIERRAN_CXC,
    _actualizar_estado_orden_por_agente,
    _actualizar_campos_orden_por_agente,
    resumen_utilidad_mes,
    _guardar_foto_entidad,
    registrar_salida_directa_a_orden,
)
app.register_blueprint(bp_operativo)

# Convertidores de Torque: módulo top-level aparte (ver
# modulos/convertidores.py) -- init_convertidores_db() se llama desde
# adentro de init_operativo_db() (necesita correr antes del ALTER TABLE
# que agrega orden_items.convertidor_id), así que acá solo hace falta
# registrar el blueprint, no llamar su init por separado.
from modulos.convertidores import bp as bp_convertidores  # noqa: E402
app.register_blueprint(bp_convertidores)

# Facturación electrónica (DTE a clientes) -- ver modulos/facturacion.py
# para el diseño de la capa de proveedor (EmisorDTE) que hace posible
# cambiar de proveedor sin tocar el resto del sistema.
from modulos.facturacion import bp as bp_facturacion  # noqa: E402
app.register_blueprint(bp_facturacion)

# Sincronización con Talca.PHP -- ver modulos/sync_talca.py. No registra
# blueprint. Lo que aporta acá son tres cosas: su migración, que init_db()
# llama al final (ver init_sync_talca_db más arriba), el hilo de fondo que
# corre el pull cada tanto, y los dos endpoints de abajo (diagnóstico de
# conexión y estado del sync).
#
# El comando manual "python -m modulos.sync_talca" sigue funcionando igual y
# no depende de nada de acá: importa de core.py y abre su propia conexión.
from modulos.sync_talca import (  # noqa: E402
    diagnostico_conexion,
    init_sync_talca_db,
    leer_estado as sync_leer_estado,
    sincronizar_entidad as sync_entidad,
)

# El orden NO es cosmético: los vehículos se cuelgan de `clientes.talca_id` y
# las órdenes de las dos cosas. Es el mismo orden que corre el comando manual
# con 'todo'.
SYNC_ORDEN_ENTIDADES = ["clientes", "vehiculos", "ordenes"]

# Cada cuánto corre el hilo de fondo, en segundos. **0 o sin definir = el hilo
# NO arranca**, que es como queda en local: ahí el sync se sigue corriendo a
# mano, porque depurar un job que se dispara solo cada tantos segundos es
# innecesariamente doloroso.
#
# 300 (5 minutos) es el valor por defecto cuando la variable está definida
# pero vacía o mal escrita. El plan original hablaba de 30 segundos; contra un
# hosting compartido, y con el pull siendo idempotente, 5 minutos es mucho más
# barato y no cambia nada de lo que el taller ve.
SYNC_INTERVALO_DEFECTO = 300


def sync_intervalo_segundos():
    """Los segundos entre corridas, o 0 si el hilo no debe arrancar.

    Se lee en cada llamada y no al importar, igual que el resto de la
    configuración de esta integración (ver ClienteTalcaHTTP), así que el
    endpoint de estado informa lo que está puesto AHORA."""
    crudo = (os.environ.get("SYNC_INTERVALO_SEGUNDOS") or "").strip()
    if not crudo:
        return 0
    try:
        intervalo = int(crudo)
    except ValueError:
        # Un valor mal escrito no apaga el sync en silencio: si alguien se
        # tomó el trabajo de definir la variable, la intención era que
        # corriera. Se usa el defecto y el endpoint de estado muestra el
        # crudo para que se vea el error de tipeo.
        return SYNC_INTERVALO_DEFECTO
    return intervalo if intervalo > 0 else 0


def sync_automatico_configurado():
    return sync_intervalo_segundos() > 0


def _correr_sync_una_vuelta(cliente_api=None):
    """Una pasada por las tres entidades, en orden. Devuelve la lista de
    resultados, uno por entidad.

    **Un fallo en una entidad no impide que corran las siguientes**, a
    diferencia del comando manual, que se detiene. La diferencia es
    deliberada y es segura por dos motivos:

      - `precondiciones` sigue mandando. Si clientes nunca se sincronizó,
        vehículos y órdenes se niegan a correr igual, así que el escenario
        peligroso -- colgar media base del cliente genérico en una carga
        inicial a medias -- no puede ocurrir por esta vía.
      - Si clientes ya corrió alguna vez y hoy falla, lo peor que pasa es que
        un vehículo cuyo dueño es nuevo caiga en el genérico. Eso se repara
        solo: `reconciliar_duenos_pendientes` lo reengancha en cuanto
        clientes vuelva a andar.

    En un hilo que corre cada 5 minutos, cortar las tres entidades porque una
    tuvo un timeout significaría que un corte de red de un minuto deja las
    órdenes sin sincronizar hasta que alguien lo note. La marca de agua de la
    que falló no avanza -- eso lo hace `_sincronizar`, no este código -- así
    que la vuelta siguiente reintenta desde el último punto bueno."""
    resultados = []
    db = _conectar_db()
    try:
        for nombre in SYNC_ORDEN_ENTIDADES:
            try:
                resultados.append(sync_entidad(db, nombre, cliente_api=cliente_api))
            except Exception as exc:
                # _sincronizar ya atrapa lo suyo y devuelve un resultado de
                # error sin levantar. Esto es la red por si algo se escapa
                # (un sqlite3.Error al guardar el estado, por ejemplo): el
                # hilo tiene que seguir vivo pase lo que pase.
                resultados.append({
                    "entidad": nombre,
                    "resultado": "error",
                    "detalle": f"{type(exc).__name__}: {exc}",
                })
    finally:
        db.close()

    # Reintentos de push Python -> Talca: al FINAL de la vuelta, cuando los
    # upserts del pull ya procesaron (o saltaron) sus filas. Si hubiera
    # corrido al principio, podría liberar push_pendiente de una fila que el
    # pull del mismo ciclo haría upsert encima.
    #
    # No se pasa el cliente_api del pull: ese es un ClienteTalca (GET) y
    # procesar_pendientes espera un ClientePushTalca (POST/PUT). Pasando None
    # la función construye su propio ClientePushTalcaHTTP.
    try:
        from modulos.push_talca import procesar_pendientes
        procesar_pendientes()
    except Exception as exc:
        logger.error("procesar_pendientes push: %s", exc)

    return resultados


def _hilo_sync_talca(veces=None, dormir=None):
    """El bucle. `veces` y `dormir` existen solo para los tests: en producción
    se llama sin argumentos y gira para siempre, durmiendo de verdad.

    El intervalo se relee en cada vuelta, así que cambiarlo en Railway toma
    efecto sin reiniciar el proceso. Si se pone en 0 con el hilo ya andando,
    el hilo se queda dormido con el intervalo por defecto en vez de girar a
    toda velocidad -- apagar el sync de verdad exige reiniciar, que es lo que
    Railway hace al cambiar una variable de todos modos."""
    dormir = dormir or time.sleep
    vuelta = 0
    while veces is None or vuelta < veces:
        vuelta += 1
        try:
            _correr_sync_una_vuelta()
        except Exception:
            # Nada puede matar este hilo: si muere, el sync se apaga en
            # silencio y las dos bases divergen sin que nadie se entere.
            pass
        dormir(sync_intervalo_segundos() or SYNC_INTERVALO_DEFECTO)


_hilo_sync_talca_iniciado = False


def iniciar_hilo_sync_talca():
    global _hilo_sync_talca_iniciado
    if not sync_automatico_configurado():
        return
    if not _hilo_sync_talca_iniciado:
        _hilo_sync_talca_iniciado = True
        hilo = threading.Thread(target=_hilo_sync_talca, daemon=True)
        hilo.start()


@app.route("/api/admin/sync/estado", methods=["GET"])
@requiere_rol()
def sync_estado():
    """Cómo le fue a cada entidad en su última corrida, para poder mirarlo
    desde Railway sin entrar a la base.

    Es de solo lectura: no dispara ninguna sincronización ni toca la marca de
    agua. Todo lo que devuelve sale de `sync_talca_estado`, que es la misma
    tabla que escriben tanto el hilo como el comando manual -- así que una
    corrida hecha a mano desde un equipo local también se ve acá.

    No incluye ningún dato de conexión: para saber si el hosting responde
    está /api/admin/sync/diagnostico, que sí sale a la red."""
    db = get_db()
    entidades = []
    for nombre in SYNC_ORDEN_ENTIDADES:
        estado = sync_leer_estado(db, nombre)
        entidades.append({
            "entidad": nombre,
            "marca_agua": estado["marca_agua"],
            "ultima_corrida_en": estado["ultima_corrida_en"],
            "ultimo_resultado": estado["ultimo_resultado"],
            "ultimo_detalle": estado["ultimo_detalle"],
            "filas_recibidas": estado["filas_recibidas"],
            "filas_creadas": estado["filas_creadas"],
            "filas_actualizadas": estado["filas_actualizadas"],
            "filas_omitidas": estado["filas_omitidas"],
            # Marca de agua vacía = esta entidad nunca terminó una corrida
            # completa. Es la diferencia entre "nunca corrió" y "corrió y no
            # trajo nada", que desde afuera se ven igual.
            "sincronizada_alguna_vez": bool(estado["marca_agua"]),
        })
    # Cola del push Python -> Talca. Va acá y no en un endpoint aparte porque
    # es la otra mitad de la misma pregunta: "¿están las dos bases al día?".
    #
    # `esperas` es el hueco que este informe viene a tapar: una entrada que
    # espera el talca_id de su cliente no incrementa intentos ni marca error
    # (esperar no es fallar), así que sin esto quedaría trabada sin escalar a
    # nada visible -- y con ella el pull dejaría de actualizar ese equipo.
    try:
        from modulos.push_talca import resumen_esperas
        cola = db.execute(
            """
            SELECT entidad,
                   COUNT(*) AS pendientes,
                   SUM(CASE WHEN intentos > 0 THEN 1 ELSE 0 END) AS con_fallos
              FROM sync_talca_push_pendientes
             WHERE resuelto_en = ''
             GROUP BY entidad
            """
        ).fetchall()
        push = {
            "pendientes_por_entidad": {r["entidad"]: r["pendientes"] for r in cola},
            "con_fallos_por_entidad": {r["entidad"]: (r["con_fallos"] or 0) for r in cola},
            "esperas": resumen_esperas(db),
        }
    except Exception as exc:      # la cola puede no existir en una base vieja
        push = {"error": str(exc)}

    return jsonify({
        "automatico_configurado": sync_automatico_configurado(),
        "intervalo_segundos": sync_intervalo_segundos(),
        "intervalo_configurado_crudo": os.environ.get("SYNC_INTERVALO_SEGUNDOS"),
        "hilo_iniciado": _hilo_sync_talca_iniciado,
        "orden": SYNC_ORDEN_ENTIDADES,
        "entidades": entidades,
        "push": push,
    })


@app.route("/api/admin/sync/diagnostico", methods=["GET"])
@requiere_rol()
def sync_diagnostico():
    """¿Se alcanza el hosting de Talca DESDE ACÁ? Hace una sola llamada a
    /api/ping con el mismo cliente HTTP del sync -- mismo User-Agent, mismos
    reintentos, mismo timeout -- e informa qué pasó.

    Existe porque hay fallas que no se pueden reproducir desde un equipo
    local: Bluehosting ya cortó la conexión por User-Agent (ver
    TALCA_USER_AGENT), y mod_security suele bloquear rangos de datacenter
    como los de Railway. Desde una casa la llamada funciona y desde el
    servidor no, sin que cambie una línea de código.

    Es de solo lectura: no toca la base, ni la marca de agua, ni el sync.
    Nunca devuelve el valor de TALCA_API_KEY, solo si está definida."""
    return jsonify(diagnostico_conexion())

# Transiciones de estado que el agente de WhatsApp tiene permitido aplicar
# (ver _agente_procesar_mensaje más arriba). A pedido del taller, el agente
# puede mover una OT desde CUALQUIER estado abierto hacia CUALQUIER otro
# estado (no solo el "siguiente paso" del flujo normal) — en la práctica un
# auto puede llegar ya aprobado (recibido -> reparación directo) o pasar de
# pruebas de ruta directo a entregado, sin pasar por cada paso intermedio.
# Lo único que se mantiene estricto es que un estado "cerrado" (ver
# ESTADOS_CERRADOS: entregado y cancelado) NO se puede volver a mover por
# este medio — sacar una OT de ahí exige un ajuste manual desde el sistema,
# precisamente para que un mensaje de WhatsApp mal interpretado no pueda
# generar un descuadre en el cierre de mes o en el dashboard. Se calcula a
# partir de ESTADOS_ORDEN/ESTADOS_CERRADOS (en vez de escribirlo a mano) para
# que un estado nuevo que se agregue a futuro quede incluido acá
# automáticamente sin tener que acordarse de tocar esta lista.
TRANSICIONES_AGENTE = {
    estado: (set(ESTADOS_ORDEN) - {estado} if estado not in ESTADOS_CERRADOS else set())
    for estado in ESTADOS_ORDEN
}


# ---------------------------------------------------------------------------
# Panel del dueño: KPIs y gráficos cruzados entre Operativo y Finanzas para
# la pantalla de inicio. No pertenece a ningún blueprint porque cruza datos
# de ambos módulos (a diferencia de los dashboards propios de cada uno, que
# sí viven en su blueprint).
# ---------------------------------------------------------------------------

def _ingresos_mes(db, prefijo_mes):
    fila = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE substr(fecha, 1, 7) = ?",
        (prefijo_mes,),
    ).fetchone()
    return fila[0] or 0


def _ingresos_mes_hasta_dia(db, prefijo_mes, dia):
    """Igual que _ingresos_mes, pero solo suma los pagos hasta cierto día del
    mes (inclusive). Sirve para comparar el mes en curso (que siempre está
    parcial, salvo el último día) contra el mismo tramo de días del mes
    anterior -- comparar contra el mes anterior COMPLETO exagera artificialmente
    la baja (ej. "-65,8%" el día 5 del mes, que no es una baja real, es
    sencillamente que llevamos pocos días)."""
    fila = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden "
        "WHERE substr(fecha, 1, 7) = ? AND CAST(substr(fecha, 9, 2) AS INTEGER) <= ?",
        (prefijo_mes, dia),
    ).fetchone()
    return fila[0] or 0


def _gastos_mes(db, prefijo_mes):
    fila = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND substr(fecha, 1, 7) = ?",
        (prefijo_mes,),
    ).fetchone()
    return fila[0] or 0


@app.route("/api/panel-dueno", methods=["GET"])
@requiere_rol()
def panel_dueno():
    db = get_db()
    hoy = datetime.now()

    def _prefijo(dt):
        return f"{dt.year:04d}-{dt.month:02d}"

    prefijo_actual = _prefijo(hoy)
    # mes anterior, sin depender de que el mes actual tenga 28-31 días
    anterior = (hoy.replace(day=1) - timedelta(days=1))
    prefijo_anterior = _prefijo(anterior)

    ingresos_mes = _ingresos_mes(db, prefijo_actual)
    ingresos_mes_anterior = _ingresos_mes(db, prefijo_anterior)

    # Proyección simple de fin de mes: regla de tres sobre los días
    # transcurridos del mes actual. No es un modelo estadístico -- es
    # exactamente lo que un dueño calcularía a mano mirando el ritmo actual.
    dias_transcurridos = hoy.day
    ultimo_dia_mes = (hoy.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    dias_totales_mes = ultimo_dia_mes.day
    proyeccion_fin_mes = round(ingresos_mes / dias_transcurridos * dias_totales_mes, 2) if dias_transcurridos else 0

    # El mes en curso está parcial (salvo que hoy sea el último día del mes),
    # así que comparar ingresos_mes contra ingresos_mes_anterior COMPLETO
    # exagera cualquier baja de forma artificial. En vez de eso, comparamos
    # contra el mismo tramo de días del mes anterior ("a esta altura del mes
    # anterior llevábamos X"). mes_en_curso se manda al frontend para que
    # el rótulo aclare que es una comparación parcial.
    mes_en_curso = dias_transcurridos < dias_totales_mes
    ultimo_dia_mes_anterior = (hoy.replace(day=1) - timedelta(days=1)).day
    dia_comparable_anterior = min(dias_transcurridos, ultimo_dia_mes_anterior)
    ingresos_mes_anterior_comparable = (
        _ingresos_mes_hasta_dia(db, prefijo_anterior, dia_comparable_anterior)
        if mes_en_curso else ingresos_mes_anterior
    )
    var_pct = (
        ((ingresos_mes / ingresos_mes_anterior_comparable) - 1) * 100
        if ingresos_mes_anterior_comparable else None
    )

    # "Margen bruto" (ingresos - gastos de caja) se retiró de Panel Principal
    # a pedido del usuario: mezclaba cobros de caja con gastos de caja de
    # naturaleza distinta y no reflejaba la operación real. Las métricas que
    # importan son venta_total_mes (ventas devengadas del mes, mismo criterio
    # que Operativo) y utilidad_estimada_mes (esa venta menos repuestos, mano
    # de obra con costo y convertidores internos -- ver resumen_utilidad_mes).
    _resumen_utilidad = resumen_utilidad_mes(db)
    venta_total_mes = _resumen_utilidad["monto_cerradas_mes"]
    utilidad_estimada_mes = _resumen_utilidad["utilidad_mes"]

    # "Abiertas" acá usa el mismo criterio que el dashboard propio de
    # Operativo (ver dashboard_operativo() -- ESTADOS_QUE_CIERRAN_CXC en vez
    # de ESTADOS_CERRADOS): una OT "lista" ya no cuenta como abierta, porque
    # su plata ya se cobró y contó (ver Migración 37). Así el número de acá
    # coincide con el que muestra la propia pestaña de Operativo, en vez de
    # mostrar dos cifras distintas para lo mismo.
    placeholders_abiertas = ", ".join("?" for _ in ESTADOS_QUE_CIERRAN_CXC)
    ot_abiertas = db.execute(
        f"SELECT COUNT(*) FROM ordenes_trabajo WHERE estado != 'cancelado' AND estado NOT IN ({placeholders_abiertas})",
        list(ESTADOS_QUE_CIERRAN_CXC),
    ).fetchone()[0]

    # Capacidad de referencia: promedio de OT entregadas por semana en las
    # últimas 8 semanas -- sirve para comparar contra las OT abiertas ahora
    # y ver si se está acumulando atraso frente al ritmo habitual de cierre.
    hace_8_semanas = (hoy - timedelta(weeks=8)).strftime("%Y-%m-%d")
    cerradas_8sem = db.execute(
        "SELECT COUNT(*) FROM ordenes_trabajo WHERE estado = 'entregado' AND fecha_entrega_real >= ?",
        (hace_8_semanas,),
    ).fetchone()[0]
    capacidad_semanal = round(cerradas_8sem / 8, 1)

    # Tendencia mensual de ingresos (pagos recibidos) -- ventana elegible con
    # ?rango=12|24|36|historico (por defecto 12), a diferencia de Operativo
    # (que es mes a mes) Panel Principal es la vista de largo plazo para
    # decisiones estratégicas, así que el rango es configurable. 'historico'
    # arranca desde el primer pago que exista en el sistema (migrado o no),
    # incluyendo meses sin pagos en 0 para no distorsionar la escala.
    rango_param = (request.args.get("rango") or "12").strip()
    if rango_param == "historico":
        primer_pago = db.execute("SELECT MIN(fecha) FROM pagos_orden WHERE fecha != ''").fetchone()[0]
        if primer_pago:
            anio_ini, mes_ini = int(primer_pago[0:4]), int(primer_pago[5:7])
            n_meses = (hoy.year - anio_ini) * 12 + (hoy.month - mes_ini) + 1
            n_meses = max(1, min(n_meses, 240))  # tope de cordura: 20 años
        else:
            n_meses = 12
    else:
        try:
            n_meses = int(rango_param)
        except (TypeError, ValueError):
            n_meses = 12
        if n_meses not in (12, 24, 36):
            n_meses = 12

    tendencia = []
    meses_cursor = []
    m = hoy.replace(day=1)
    for _ in range(n_meses):
        meses_cursor.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    meses_cursor.reverse()
    for m in meses_cursor:
        pref = _prefijo(m)
        tendencia.append({"mes": pref, "ingresos": _ingresos_mes(db, pref)})

    # Proyección simple de los próximos 3 meses: regresión lineal (mínimos
    # cuadrados) sobre los 12 meses de tendencia real de arriba. No pretende
    # ser un modelo estadístico sofisticado -- es la misma idea que
    # proyeccion_fin_mes (una recta que sigue el ritmo reciente), pero
    # mirando varios meses hacia atrás en vez de solo los días del mes
    # actual, para no sobrerreaccionar a un mes puntualmente bueno o malo.
    # El mes en curso (todavía incompleto) se excluye del ajuste para no
    # sesgar la pendiente hacia abajo.
    meses_completos = tendencia[:-1] if len(tendencia) > 1 else tendencia
    proyeccion_mensual = []
    if len(meses_completos) >= 3:
        xs = list(range(len(meses_completos)))
        ys = [p["ingresos"] for p in meses_completos]
        n_pts = len(xs)
        media_x = sum(xs) / n_pts
        media_y = sum(ys) / n_pts
        num = sum((xs[i] - media_x) * (ys[i] - media_y) for i in range(n_pts))
        den = sum((xs[i] - media_x) ** 2 for i in range(n_pts))
        pendiente = (num / den) if den else 0
        intercepto = media_y - pendiente * media_x
        m_futuro = hoy.replace(day=1)
        for paso in range(1, 4):
            m_futuro = (m_futuro.replace(day=28) + timedelta(days=4)).replace(day=1)
            x_futuro = n_pts - 1 + paso
            valor = max(0, round(pendiente * x_futuro + intercepto, 2))
            proyeccion_mensual.append({"mes": _prefijo(m_futuro), "ingresos_proyectados": valor})

    cxc_pendiente, cxc_vencido = _cxc_pendiente_y_vencido(db)
    cxp_pendiente, cxp_vencido = _cxp_pendiente_y_vencido(db)

    # Ranking de clientes por monto -- acotado al MISMO rango que el gráfico
    # de tendencia de arriba (?rango=12|24|36|historico), a pedido del
    # taller: antes era siempre "todo el tiempo" sin importar el selector de
    # meses, lo que no calzaba con el resto del panel. Se filtra por
    # o.fecha_recepcion (mismo campo que usa la versión mensual de este
    # ranking en Operativo, ver ranking_clientes en
    # /api/operativo/dashboard/graficos) desde el primer mes de la ventana
    # ya calculada arriba (meses_cursor[0]) -- "historico" reusa el mismo
    # n_meses ya resuelto (desde el primer pago), así que no hace falta
    # duplicar esa lógica acá.
    prefijo_desde_ranking = _prefijo(meses_cursor[0]) if meses_cursor else prefijo_actual
    ranking_clientes_historico = [
        {"nombre": r["nombre"], "cantidad": r["cantidad"], "monto": r["monto"]}
        for r in db.execute(
            """
            SELECT c.nombre AS nombre, COUNT(*) AS cantidad,
                   COALESCE(SUM(o.precio_referencial), 0) AS monto
            FROM ordenes_trabajo o JOIN clientes c ON c.id = o.cliente_id
            WHERE substr(o.fecha_recepcion, 1, 7) >= ?
            GROUP BY c.id ORDER BY monto DESC LIMIT 8
            """,
            (prefijo_desde_ranking,),
        ).fetchall()
    ]

    return jsonify({
        "ingresos_mes": round(ingresos_mes, 2),
        "ingresos_mes_anterior": round(ingresos_mes_anterior, 2),
        "var_pct_mes": round(var_pct, 1) if var_pct is not None else None,
        "mes_en_curso": mes_en_curso,
        "proyeccion_fin_mes": proyeccion_fin_mes,
        "venta_total_mes": venta_total_mes,
        "utilidad_estimada_mes": utilidad_estimada_mes,
        "ot_abiertas": ot_abiertas,
        "capacidad_semanal_promedio": capacidad_semanal,
        "cxc_pendiente": cxc_pendiente,
        "cxp_pendiente": cxp_pendiente,
        "tendencia_mensual": tendencia,
        "proyeccion_mensual": proyeccion_mensual,
        "rango_aplicado": rango_param if rango_param in ("historico",) or n_meses in (12, 24, 36) else "12",
        "ranking_clientes_historico": ranking_clientes_historico,
    })


# ---------------------------------------------------------------------------
# Migración de datos inicial (clientes / autos / OTs abiertas del taller)
# ---------------------------------------------------------------------------
# Endpoint de uso único (admin) para cargar los 3 archivos que salen del
# script de preparación de datos (clientes_a_crear.json, autos_a_crear.json,
# ordenes_a_crear.json). Reimplementa a propósito los mismos INSERT que
# crear_cliente/crear_equipo/crear_orden en modulos/operativo.py — se evaluó
# llamar esos endpoints directamente pero requieren un request de Flask real,
# así que se replican sus reglas 1:1 (mismos campos, mismo default de estado
# "recibido") en vez de eso. Si mañana cambian esas reglas, revisar también
# acá si se vuelve a usar esta migración.
def _migracion_inferir_estado(requerimiento):
    t = (requerimiento or "").upper()
    t = re.sub(r"<[^>]+>", " ", t)
    if "NO APRUEBA" in t or ("PRESUPUESTO" in t and "DIAGNOSTIC" not in t):
        return "esperando_aprobacion"
    if "DIAGNOSTIC" in t:
        return "diagnostico"
    if "INGRESO" in t or not t.strip():
        return "recibido"
    return "reparacion"


@app.route("/admin/migracion-datos", methods=["GET"])
@requiere_rol()
def vista_migracion_datos():
    """Página retirada: era la única interfaz de /api/admin/migracion/importar,
    que ahora responde 410. Se conserva la ruta -- y no se borra -- porque a
    esta herramienta se llegaba tipeando la URL a mano (ver el comentario en
    templates/admin.html), así que un 404 mandaría a buscar el link "perdido"
    en vez de explicar por qué ya no se usa. Devuelve 410 igual que su API."""
    return render_template("migracion_datos.html"), 410


@app.route("/api/admin/migracion/importar", methods=["POST"])
@requiere_rol()
def migracion_importar():
    """REEMPLAZADO POR EL SYNC — responde 410. Ver el bloque "REEMPLAZADOS POR
    EL SYNC CON TALCA" más abajo.

    Creaba clientes, autos y OT de una, desde tres JSON subidos a mano:
    emparejaba los clientes por RUT normalizado y los equipos por patente —
    exactamente el criterio de identidad que el sync tiene prohibido.

    Dos cosas lo hacen peor que los otros dos bloqueados:

    1. **No escribe legacy_ot_no.** Las órdenes que creaba quedaban sin
       ninguna marca de identidad de Talca, así que el sync de órdenes (que
       adopta por legacy_ot_no = talca_id) no tendría por dónde reclamarlas:
       entrarían de nuevo, duplicadas.
    2. **Ya no compite solo por órdenes.** Sus INSERT de clientes y equipos
       tampoco escriben talca_id, y el sync ya cargó 4.212 clientes y 5.161
       equipos. Su dedup de clientes mira `WHERE rut != ''` (ciega a los 442
       sin RUT, arbitraria entre los 90 RUT repetidos) y **los autos no
       tienen dedup en absoluto**: arma el índice por patente pero inserta
       sin consultarlo. Correrlo hoy duplica la flota entera.

    El código de abajo queda como referencia de cómo entraron los datos
    históricos que ya están en producción. No se ejecuta."""
    return _respuesta_reemplazado_por_sync(
        "/api/admin/migracion/importar",
        "Este además creaba clientes y equipos sin talca_id, sobre los 4.212 "
        "clientes y 5.161 equipos que el sync ya cargó, y sus órdenes quedaban "
        "sin legacy_ot_no: ni siquiera serían adoptables después.",
    )

    db = get_db()

    def leer_json(campo):
        archivo = request.files.get(campo)
        if not archivo or archivo.filename == "":
            return []
        try:
            return json.loads(archivo.read().decode("utf-8"))
        except Exception:
            return None

    clientes_in = leer_json("clientes_json")
    autos_in = leer_json("autos_json")
    ordenes_in = leer_json("ordenes_json")
    if clientes_in is None or autos_in is None or ordenes_in is None:
        return jsonify({"error": "Uno de los 3 archivos no es un JSON válido"}), 400

    resumen = {
        "clientes_creados": 0, "clientes_reutilizados_existian": 0,
        "autos_creados": 0, "autos_sin_cliente": [],
        "ordenes_creadas": 0, "ordenes_sin_equipo": [],
    }
    ts = now_iso()

    # ---- clientes: no duplicar contra los que YA existen en producción ----
    rut_a_cliente_id = {}
    for fila in db.execute("SELECT id, rut FROM clientes WHERE rut != ''"):
        clave = re.sub(r"[.\-]", "", fila["rut"] or "").upper()
        if clave:
            rut_a_cliente_id[clave] = fila["id"]

    for c in clientes_in:
        rut_clave = (c.get("rut_clave") or "").strip()
        if rut_clave and rut_clave in rut_a_cliente_id:
            resumen["clientes_reutilizados_existian"] += 1
            continue
        direccion = c.get("direccion") or ""
        if c.get("comuna"):
            direccion = f"{direccion}, {c['comuna']}" if direccion else c["comuna"]
        cur = db.execute(
            "INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas, creado_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (c.get("nombre") or "").strip().upper(),
                (c.get("telefono") or "").strip(),
                (c.get("email") or "").strip(),
                formatear_rut((c.get("rut_crudo") or "").strip()),
                direccion.strip().upper(),
                "",
                ts,
            ),
        )
        db.commit()
        resumen["clientes_creados"] += 1
        if rut_clave:
            rut_a_cliente_id[rut_clave] = cur.lastrowid

    # ---- autos ----
    patente_a_equipo = {}
    for fila in db.execute("SELECT id, cliente_id, identificador FROM equipos"):
        if fila["identificador"]:
            patente_a_equipo.setdefault(
                fila["identificador"].strip().upper(),
                {"equipo_id": fila["id"], "cliente_id": fila["cliente_id"]},
            )

    for a in autos_in:
        patente = (a.get("patente") or "").strip().upper()
        cliente_id = rut_a_cliente_id.get((a.get("rut_clave") or "").strip())
        if not cliente_id:
            resumen["autos_sin_cliente"].append(patente)
            continue
        anio_val = None
        try:
            av = str(a.get("anio") or "").strip()
            if av:
                anio_int = int(float(av))
                if 1950 <= anio_int <= 2100:
                    anio_val = anio_int
        except (TypeError, ValueError):
            anio_val = None
        cur = db.execute(
            """INSERT INTO equipos (
                cliente_id, identificador, marca, modelo, notas, creado_en,
                color, anio, modelo_transmision, puertas, motor_cc, vin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cliente_id, patente,
                (a.get("marca") or "").strip().upper(),
                (a.get("modelo") or "").strip().upper(),
                "", ts,
                (a.get("color") or "").strip().upper(),
                anio_val,
                (a.get("transmision") or "").strip().upper(),
                (a.get("puertas") or "").strip(),
                "",
                (a.get("chasis") or "").strip().upper(),
            ),
        )
        db.commit()
        resumen["autos_creados"] += 1
        patente_a_equipo[patente] = {"equipo_id": cur.lastrowid, "cliente_id": cliente_id}

    # ---- órdenes (ya vienen filtradas a solo ABIERTA desde el script) ----
    for o in ordenes_in:
        patente = (o.get("patente") or "").strip().upper()
        info = patente_a_equipo.get(patente)
        equipo_id = info["equipo_id"] if info else None
        cliente_id = info["cliente_id"] if info else None
        if cliente_id is None:
            # Caso borde: la orden no corresponde a un vehículo real (ej. un
            # préstamo de repuesto) — se enlaza solo por cliente, usando la
            # clave de RUT que el script de preparación dejó explícita para
            # estos casos, en vez de descartar la orden.
            rut_clave_fallback = (o.get("cliente_rut_clave") or "").strip()
            cliente_id = rut_a_cliente_id.get(rut_clave_fallback)
        if cliente_id is None:
            resumen["ordenes_sin_equipo"].append({"no": o.get("no"), "patente": patente})
            continue
        requerimiento = o.get("requerimiento") or ""
        cur = db.execute(
            """INSERT INTO ordenes_trabajo (
                cliente_id, equipo_id, estado, descripcion_problema, mecanico,
                fecha_recepcion, fecha_entrega_estimada, usuario, creado_en, actualizado_en,
                kilometraje, prioridad, recepcionado_por
            ) VALUES (?, ?, 'recibido', ?, '', ?, NULL, ?, ?, ?, NULL, 'media', ?)""",
            (
                cliente_id, equipo_id,
                requerimiento.strip().upper(),
                ts, usuario_actual(), ts, ts, usuario_actual(),
            ),
        )
        orden_id = cur.lastrowid
        precio = o.get("precio")
        try:
            precio_val = float(precio) if precio not in (None, "", 0) else None
        except (TypeError, ValueError):
            precio_val = None
        db.execute(
            "UPDATE ordenes_trabajo SET estado = ?, precio_referencial = ?, actualizado_en = ? WHERE id = ?",
            (_migracion_inferir_estado(requerimiento), precio_val, ts, orden_id),
        )
        db.commit()
        resumen["ordenes_creadas"] += 1

    return jsonify(resumen)


@app.route("/api/admin/migracion/repuestos-ot-abiertas", methods=["POST"])
@requiere_rol()
def migracion_repuestos_ot_abiertas():
    """Carga los repuestos y mano de obra externa históricos (de la base
    legacy TalleresTC) hacia las OT ABIERTAS ya migradas — a diferencia de
    /api/admin/migracion/importar (que crea clientes/autos/OT), esto NO
    crea órdenes nuevas: solo agrega orden_items a órdenes que ya existen
    en el sistema, emparejándolas por patente del vehículo.

    Espera un archivo 'repuestos_json' con una lista de objetos:
    [{legacy_no, patente, repuestos: [{modelo, descripcion, costo_unitario, ...}],
      mano_obra: [{nombre_reparador, detalle, cantidad, precio, ...}]}, ...]

    Es seguro correrla más de una vez sobre la misma orden: si esa orden
    ya tiene ítems cargados por esta migración (usuario = 'Migración
    histórica'), se salta para no duplicar.

    REEMPLAZADO POR EL SYNC — responde 410. Ver el bloque "REEMPLAZADOS POR
    EL SYNC CON TALCA" más abajo.

    No crea órdenes, pero **no las busca por legacy_ot_no**: las resuelve por
    patente, y con `setdefault` sin ORDER BY, o sea que entre las varias
    órdenes de un mismo vehículo gana una arbitraria. Hoy no matchea nada
    (ordenes_trabajo está en 0); cuando el sync traiga las ~7.100 OT sobre
    5.161 vehículos, casi toda patente va a tener varias y estos ítems —
    que alimentan TOTAL COSTO y UTILIDAD del documento impreso — se
    colgarían de la orden equivocada, sin ningún error.

    Tiene arreglo, no solo bloqueo: su JSON de entrada ya trae `legacy_no`
    (hoy solo se usa para reportar). Emparejando por legacy_ot_no queda en
    la misma categoría que pagos-ot-abiertas y puede volver a vivir."""
    return _respuesta_reemplazado_por_sync(
        "/api/admin/migracion/repuestos-ot-abiertas",
        "Este no crea órdenes, pero las busca por patente y con setdefault: "
        "entre las varias órdenes de un mismo vehículo gana una arbitraria. "
        "Su JSON de entrada ya trae legacy_no; emparejando por legacy_ot_no "
        "podría volver a habilitarse.",
    )

    archivo = request.files.get("repuestos_json")
    if not archivo or archivo.filename == "":
        return jsonify({"error": "Debes adjuntar el archivo repuestos_json"}), 400
    try:
        datos = json.loads(archivo.read().decode("utf-8"))
    except Exception:
        return jsonify({"error": "El archivo no es un JSON válido"}), 400

    db = get_db()
    ts = now_iso()
    usuario_migracion = "Migración histórica"

    patente_a_orden = {}
    for fila in db.execute(
        """
        SELECT o.id AS orden_id, e.identificador AS patente
        FROM ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id
        WHERE e.identificador != ''
        """
    ):
        patente_a_orden.setdefault(fila["patente"].strip().upper(), fila["orden_id"])

    resumen = {
        "ordenes_procesadas": 0, "ordenes_sin_match": [], "ordenes_ya_migradas": 0,
        "items_repuesto_creados": 0, "items_mano_obra_creados": 0,
    }

    for entrada in datos:
        patente = (entrada.get("patente") or "").strip().upper()
        orden_id = patente_a_orden.get(patente) if patente else None
        if not orden_id:
            if entrada.get("repuestos") or entrada.get("mano_obra"):
                resumen["ordenes_sin_match"].append({"legacy_no": entrada.get("legacy_no"), "patente": patente})
            continue

        ya_migrada = db.execute(
            "SELECT 1 FROM orden_items WHERE orden_id = ? AND usuario = ?",
            (orden_id, usuario_migracion),
        ).fetchone()
        if ya_migrada:
            resumen["ordenes_ya_migradas"] += 1
            continue

        resumen["ordenes_procesadas"] += 1

        for r in entrada.get("repuestos") or []:
            descripcion = " - ".join(
                x.strip() for x in [(r.get("modelo") or ""), (r.get("descripcion") or "")] if x.strip()
            ) or "(sin descripción)"
            costo = 0.0
            try:
                costo = float(r.get("costo_unitario") or 0)
            except (TypeError, ValueError):
                costo = 0.0
            db.execute(
                """INSERT INTO orden_items (orden_id, tipo, descripcion, cantidad, costo_unitario, usuario, creado_en)
                   VALUES (?, 'repuesto', ?, 1, ?, ?, ?)""",
                (orden_id, descripcion.upper(), costo, usuario_migracion, ts),
            )
            resumen["items_repuesto_creados"] += 1

        for m in entrada.get("mano_obra") or []:
            descripcion = " - ".join(
                x.strip() for x in [(m.get("nombre_reparador") or ""), (m.get("detalle") or "")] if x.strip()
            ) or "(mano de obra externa)"
            try:
                cantidad = float(m.get("cantidad") or 1) or 1
            except (TypeError, ValueError):
                cantidad = 1
            try:
                precio = float(m.get("precio") or 0)
            except (TypeError, ValueError):
                precio = 0.0
            db.execute(
                """INSERT INTO orden_items (orden_id, tipo, descripcion, cantidad, costo_unitario, usuario, creado_en, detalle)
                   VALUES (?, 'mano_obra', ?, ?, ?, ?, ?, 'Reparación externa (migración histórica)')""",
                (orden_id, descripcion.upper(), cantidad, precio, usuario_migracion, ts),
            )
            resumen["items_mano_obra_creados"] += 1

        db.commit()

    return jsonify(resumen)


@app.route("/api/admin/migracion/garantias-abiertas", methods=["POST"])
@requiere_rol()
def migracion_garantias_abiertas():
    """Crea garantías (entidad 'garantias') a partir de las OT tipo
    'Garantía' que en la base legacy siguen ABIERTAS. Se enlazan por RUT
    del cliente (ya migrado) y, cuando hay patente, por el vehículo. Si el
    texto original menciona la OT de origen (ej. 'GARANTÍA OT.2623') pero
    esa orden todavía no está en este sistema (porque solo se migraron las
    OT abiertas, no el historial cerrado completo), el número de OT legado
    queda igual anotado en el diagnóstico para no perder el dato, aunque
    'orden_origen_id' quede vacío.

    Espera un archivo 'garantias_json': lista de objetos con legacy_no,
    patente, cliente_rut, motivo, diagnostico, fecha_recepcion,
    origen_legacy_no. Idempotente: si ya existe una garantía con el mismo
    cliente + fecha_recepcion, se salta.

    REEMPLAZADO POR EL SYNC — responde 410. Ver el bloque "REEMPLAZADOS POR
    EL SYNC CON TALCA" más abajo.

    No toca ordenes_trabajo, y aun así se bloquea, por dos motivos:

    1. **Consume números del correlativo compartido.** En este sistema las
       garantías comparten correlativo con las OT (_asignar_id_correlativo
       en modulos/operativo.py hace max(MAX(id) ordenes_trabajo, MAX(id)
       garantias) + 1), y este endpoint inserta en garantias sin id
       explícito, o sea que se lleva números por rowid de SQLite. Durante el
       mes de convivencia está decidido que Talca es el único que reparte
       esos números: repartirlos por fuera del sync es exactamente lo que no
       puede pasar.
    2. **Crea identidad con RUT y patente**, que no son llave, y encima
       adivina: si la patente no calza pero el cliente tiene un solo
       vehículo, le cuelga ese. La tabla garantias no guarda ningún id de
       Talca, así que nada de lo que cree es adoptable después — y Talca
       tiene 1.424 filas tipo='Garantia' destinadas a esta misma tabla
       cuando llegue el paso 5.

    El código de abajo queda como referencia. No se ejecuta."""
    return _respuesta_reemplazado_por_sync(
        "/api/admin/migracion/garantias-abiertas",
        "Este no toca ordenes_trabajo, pero crea filas en garantias, que "
        "comparte correlativo con las OT (_asignar_id_correlativo en "
        "modulos/operativo.py): reparte números de OT por fuera del sync, y "
        "durante la convivencia solo Talca puede repartirlos.",
    )

    archivo = request.files.get("garantias_json")
    if not archivo or archivo.filename == "":
        return jsonify({"error": "Debes adjuntar el archivo garantias_json"}), 400
    try:
        datos = json.loads(archivo.read().decode("utf-8"))
    except Exception:
        return jsonify({"error": "El archivo no es un JSON válido"}), 400

    db = get_db()
    ts = now_iso()

    rut_a_cliente = {}
    for fila in db.execute("SELECT id, rut FROM clientes WHERE rut != ''"):
        clave = re.sub(r"[.\-]", "", fila["rut"] or "").upper()
        if clave:
            rut_a_cliente[clave] = fila["id"]

    resumen = {"creadas": 0, "ya_existian": 0, "sin_cliente": [], "sin_equipo": []}

    for g in datos:
        rut_clave = re.sub(r"[.\-]", "", g.get("cliente_rut") or "").upper()
        cliente_id = rut_a_cliente.get(rut_clave)
        if not cliente_id:
            resumen["sin_cliente"].append({"legacy_no": g.get("legacy_no"), "cliente_rut": g.get("cliente_rut")})
            continue

        patente = (g.get("patente") or "").strip().upper()
        equipo_id = None
        if patente:
            fila_eq = db.execute(
                "SELECT id FROM equipos WHERE cliente_id = ? AND identificador = ?", (cliente_id, patente)
            ).fetchone()
            equipo_id = fila_eq["id"] if fila_eq else None
        if not equipo_id:
            equipos_cliente = db.execute(
                "SELECT id FROM equipos WHERE cliente_id = ?", (cliente_id,)
            ).fetchall()
            if len(equipos_cliente) == 1:
                equipo_id = equipos_cliente[0]["id"]
            elif not patente:
                resumen["sin_equipo"].append({"legacy_no": g.get("legacy_no"), "cliente_rut": g.get("cliente_rut")})

        fecha_raw = (g.get("fecha_recepcion") or "").strip()
        fecha_recepcion = fecha_raw.replace(" ", "T") if fecha_raw else ts

        ya_existe = db.execute(
            "SELECT 1 FROM garantias WHERE cliente_id = ? AND fecha_recepcion = ?",
            (cliente_id, fecha_recepcion),
        ).fetchone()
        if ya_existe:
            resumen["ya_existian"] += 1
            continue

        diagnostico = (g.get("diagnostico") or "").strip()
        origen_legacy_no = g.get("origen_legacy_no")
        if origen_legacy_no:
            diagnostico = (diagnostico + f" (OT original legado #{origen_legacy_no} — aún no migrada al sistema nuevo)").strip()

        db.execute(
            """INSERT INTO garantias (
                cliente_id, equipo_id, orden_origen_id, motivo, diagnostico, estado,
                fecha_recepcion, usuario, creado_en, actualizado_en
            ) VALUES (?, ?, NULL, ?, ?, 'ingreso', ?, ?, ?, ?)""",
            (
                cliente_id, equipo_id,
                (g.get("motivo") or "GARANTÍA").strip().upper(),
                diagnostico.upper(),
                fecha_recepcion, "Migración histórica", ts, ts,
            ),
        )
        resumen["creadas"] += 1
        db.commit()

    return jsonify(resumen)


@app.route("/api/admin/migracion/garantias-items", methods=["POST"])
@requiere_rol()
def migracion_garantias_items():
    """Carga los repuestos y mano de obra externa históricos (de la base
    legacy TalleresTC) hacia garantías YA MIGRADAS (ver
    migracion_garantias_abiertas) — no crea garantías nuevas, solo agrega
    garantia_items a las que ya existen en el sistema.

    Como 'garantias' no guarda el legacy_no (se creó matcheando por
    cliente + fecha_recepcion, ver migracion_garantias_abiertas), este
    endpoint usa el mismo criterio para encontrar la garantía correcta:
    RUT del cliente + fecha_recepcion exacta.

    Espera un archivo 'garantias_items_json' con una lista de objetos:
    [{legacy_no, cliente_rut, fecha_recepcion,
      repuestos: [{modelo, descripcion, costo_unitario}],
      mano_obra: [{nombre_reparador, detalle, precio}]}, ...]

    Igual que la migración de repuestos de OTs abiertas: los ítems quedan
    con producto_id = NULL a propósito (no se tocan ni catálogo ni stock).
    Es seguro correrla más de una vez: si la garantía ya tiene ítems
    cargados por esta migración (usuario = 'Migración histórica'), se
    salta para no duplicar.

    REEMPLAZADO POR EL SYNC — responde 410. Ver el bloque "REEMPLAZADOS POR
    EL SYNC CON TALCA" más abajo.

    Va junto con migracion_garantias_abiertas, que es de donde salían las
    garantías que este endpoint completa: bloqueado aquel, este no tendría
    de qué colgarse (todo caería en garantias_sin_match). Tampoco cumple el
    criterio por sí solo — empareja por RUT + fecha_recepcion exacta, no por
    ninguna llave de Talca."""
    return _respuesta_reemplazado_por_sync(
        "/api/admin/migracion/garantias-items",
        "Este solo cuelga ítems, pero empareja por RUT + fecha_recepcion, no "
        "por una llave de Talca, y las garantías de las que colgaba las creaba "
        "garantias-abiertas, que también quedó bloqueado.",
    )

    archivo = request.files.get("garantias_items_json")
    if not archivo or archivo.filename == "":
        return jsonify({"error": "Debes adjuntar el archivo garantias_items_json"}), 400
    try:
        datos = json.loads(archivo.read().decode("utf-8"))
    except Exception:
        return jsonify({"error": "El archivo no es un JSON válido"}), 400

    db = get_db()
    ts = now_iso()
    usuario_migracion = "Migración histórica"

    rut_a_cliente = {}
    for fila in db.execute("SELECT id, rut FROM clientes WHERE rut != ''"):
        clave = re.sub(r"[.\-]", "", fila["rut"] or "").upper()
        if clave:
            rut_a_cliente[clave] = fila["id"]

    resumen = {
        "garantias_procesadas": 0, "garantias_sin_match": [], "garantias_ya_migradas": 0,
        "items_repuesto_creados": 0, "items_mano_obra_creados": 0, "garantias_con_error": [],
    }

    for entrada in datos:
        rut_clave = re.sub(r"[.\-]", "", entrada.get("cliente_rut") or "").upper()
        cliente_id = rut_a_cliente.get(rut_clave)
        fecha_raw = (entrada.get("fecha_recepcion") or "").strip()
        fecha_recepcion = fecha_raw.replace(" ", "T") if fecha_raw else ""

        garantia = None
        if cliente_id and fecha_recepcion:
            garantia = db.execute(
                "SELECT id FROM garantias WHERE cliente_id = ? AND fecha_recepcion = ?",
                (cliente_id, fecha_recepcion),
            ).fetchone()
        if not garantia:
            resumen["garantias_sin_match"].append({
                "legacy_no": entrada.get("legacy_no"), "cliente_rut": entrada.get("cliente_rut"),
            })
            continue
        garantia_id = garantia["id"]

        ya_migrada = db.execute(
            "SELECT 1 FROM garantia_items WHERE garantia_id = ? AND usuario = ?",
            (garantia_id, usuario_migracion),
        ).fetchone()
        if ya_migrada:
            resumen["garantias_ya_migradas"] += 1
            continue

        # Cada garantía se procesa en su propio "intento": si algo puntual
        # de ESTA garantía falla (ej. un dato corrupto que dispara una
        # violación de integridad referencial), no se quiere perder el
        # resto del archivo completo con un error 500 genérico. Se
        # deshace solo lo de esta garantía (las anteriores ya quedaron
        # confirmadas con su propio commit) y se registra el detalle
        # exacto del error para poder diagnosticarlo sin necesidad de
        # acceder a la base de producción directamente.
        try:
            items_repuesto_entrada = 0
            items_mano_obra_entrada = 0

            for r in entrada.get("repuestos") or []:
                descripcion = " - ".join(
                    x.strip() for x in [(r.get("modelo") or ""), (r.get("descripcion") or "")] if x.strip()
                ) or "(sin descripción)"
                try:
                    costo = float(r.get("costo_unitario") or 0)
                except (TypeError, ValueError):
                    costo = 0.0
                db.execute(
                    """INSERT INTO garantia_items (garantia_id, tipo, descripcion, cantidad, costo_unitario, usuario, creado_en)
                       VALUES (?, 'repuesto', ?, 1, ?, ?, ?)""",
                    (garantia_id, descripcion.upper(), costo, usuario_migracion, ts),
                )
                items_repuesto_entrada += 1

            for m in entrada.get("mano_obra") or []:
                descripcion = " - ".join(
                    x.strip() for x in [(m.get("nombre_reparador") or ""), (m.get("detalle") or "")] if x.strip()
                ) or "(mano de obra externa)"
                try:
                    precio = float(m.get("precio") or 0)
                except (TypeError, ValueError):
                    precio = 0.0
                db.execute(
                    """INSERT INTO garantia_items (garantia_id, tipo, descripcion, cantidad, costo_unitario, usuario, creado_en, detalle)
                       VALUES (?, 'mano_obra', ?, 1, ?, ?, ?, 'Reparación externa (migración histórica)')""",
                    (garantia_id, descripcion.upper(), precio, usuario_migracion, ts),
                )
                items_mano_obra_entrada += 1

            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(
                "Migración garantías: error en legacy_no=%s cliente_rut=%s garantia_id=%s: %s",
                entrada.get("legacy_no"), entrada.get("cliente_rut"), garantia_id, e,
            )
            resumen["garantias_con_error"].append({
                "legacy_no": entrada.get("legacy_no"),
                "cliente_rut": entrada.get("cliente_rut"),
                "garantia_id": garantia_id,
                "error": str(e),
            })
            continue

        resumen["garantias_procesadas"] += 1
        resumen["items_repuesto_creados"] += items_repuesto_entrada
        resumen["items_mano_obra_creados"] += items_mano_obra_entrada

    return jsonify(resumen)


@app.route("/api/admin/migracion/diagnostico-garantias", methods=["GET"])
@requiere_rol()
def migracion_diagnostico_garantias():
    """Diagnóstico de un solo uso: la migración de ítems de garantías
    (migracion_garantias_items) empezó a fallar en producción con
    'FOREIGN KEY constraint failed' para las 9 garantías, todas a la vez
    -- no un dato puntual corrupto, sino algo sistémico en cómo está la
    relación garantias/garantia_items en esa base específica. Como no
    hay acceso directo a la base de producción, este endpoint expone lo
    necesario para diagnosticarlo desde el navegador: el esquema real de
    ambas tablas, la definición de la llave foránea tal como SQLite la
    ve, y un chequeo explícito (PRAGMA foreign_key_check) que lista
    cualquier fila que efectivamente esté violando la integridad
    referencial ahora mismo."""
    db = get_db()
    info = {
        "garantias_table_info": [dict(r) for r in db.execute("PRAGMA table_info(garantias)")],
        "garantia_items_table_info": [dict(r) for r in db.execute("PRAGMA table_info(garantia_items)")],
        "garantia_items_foreign_key_list": [dict(r) for r in db.execute("PRAGMA foreign_key_list(garantia_items)")],
        "foreign_key_check_garantia_items": [dict(r) for r in db.execute("PRAGMA foreign_key_check(garantia_items)")],
        "total_garantias": db.execute("SELECT COUNT(*) AS n FROM garantias").fetchone()["n"],
        "total_garantia_items": db.execute("SELECT COUNT(*) AS n FROM garantia_items").fetchone()["n"],
        "tablas_legado": [
            r["name"] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_legado%'"
            )
        ],
    }
    # Muestra puntual: las mismas 9 garantías que reportó el error, para
    # ver si su 'id' efectivamente existe y con qué tipo de dato.
    muestra = []
    for fila in db.execute("SELECT id, cliente_id, fecha_recepcion FROM garantias LIMIT 15"):
        muestra.append(dict(fila))
    info["muestra_garantias"] = muestra
    return jsonify(info)


# ---------------------------------------------------------------------------
# Migración histórica completa (OTs cerradas, últimos 3 años) — a diferencia
# de las migraciones anteriores (que reciben el JSON por upload desde el
# navegador porque son chiquitas), acá el volumen es demasiado grande para
# transportarlo por fetch()+FormData sin toparse con timeouts del lado del
# navegador. Los archivos ya vienen incluidos en el deploy (carpeta data/,
# generados una sola vez desde la base legacy TalleresTC) y estos endpoints
# simplemente los leen del disco y los procesan. Se disparan a mano desde el
# navegador (autenticado, rol admin), nunca automáticamente en el arranque.
# ---------------------------------------------------------------------------

def _cargar_json_data(nombre_archivo):
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", nombre_archivo)
    with open(ruta, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# REEMPLAZADOS POR EL SYNC CON TALCA (modulos/sync_talca.py)
# ---------------------------------------------------------------------------
#
# Seis endpoints de migración quedaron BLOQUEADOS con HTTP 410 Gone. Dos están
# acá abajo; los otros cuatro, bastante más arriba en este mismo archivo, donde
# vive el resto de la migración histórica:
#
#   - /api/admin/migracion/ordenes-historicas   (acá abajo)
#   - /api/admin/migracion/backfill-legacy-no   (acá abajo)
#   - /api/admin/migracion/importar             (+ la vista /admin/migracion-datos)
#   - /api/admin/migracion/repuestos-ot-abiertas
#   - /api/admin/migracion/garantias-abiertas
#   - /api/admin/migracion/garantias-items
#
# El criterio para decidir cuáles caen: **si solo cuelgan hijos de una orden
# que ya existe, buscándola por legacy_ot_no, se quedan; si crean o
# identifican órdenes, 410.** Por eso pagos-ot-abiertas sigue vivo y
# repuestos-ot-abiertas no: los dos solo agregan hijos, pero el primero
# resuelve la orden por legacy_ot_no y el segundo por patente.
#
# Los cuatro nuevos, en una línea cada uno (el detalle está en su docstring):
#
#   - importar: crea clientes, autos y OT por RUT y patente. Sus órdenes no
#     llevan legacy_ot_no, así que ni siquiera serían adoptables; y sus
#     clientes y equipos tampoco llevan talca_id, sobre los 4.212 clientes y
#     5.161 equipos que el sync ya cargó (los autos no tienen dedup alguno).
#   - repuestos-ot-abiertas: no crea órdenes, pero las busca por patente con
#     setdefault, o sea que gana una arbitraria entre las varias del mismo
#     vehículo. Reparable: su JSON ya trae legacy_no.
#   - garantias-abiertas: no toca ordenes_trabajo y cae igual, porque
#     garantias comparte correlativo con las OT (_asignar_id_correlativo en
#     modulos/operativo.py) e inserta sin id explícito: reparte números de OT
#     por fuera del sync, y durante la convivencia solo Talca los reparte.
#   - garantias-items: cuelga de las garantías que creaba el anterior,
#     emparejando por RUT + fecha_recepcion.
#
# El motivo es la identidad de las órdenes, no el volumen ni el rendimiento.
# Ambos endpoints emparejan contra Talca por RUT (el cliente) y por patente
# (el equipo), y sobre los datos reales ninguno de los dos es una llave:
#
#   - clientes: 442 filas sin RUT, 90 RUT duplicados (195 filas), 127 con
#     dígito verificador inválido.
#   - vehiculos: 285 patentes duplicadas (641 filas), 417 fuera de formato
#     chileno (CXMANO, VENTA, GASTOSGEN, SIN/AUTO...).
#   - data/migracion_legacy_no_abiertas.json, que es justamente el insumo del
#     backfill, trae valores que no son patentes: la primera entrada es
#     {"patente": "SEPT.21", "legacy_no": 2671}.
#   - data/migracion_historica_ordenes.json tiene 16 patentes que aparecen con
#     más de un RUT, afectando a 65 de sus 2.983 órdenes.
#
# El sync usa el 'id' numérico de Talca (guardado como talca_id) y nada más.
# Dejar los dos escritores vivos sobre ordenes_trabajo, cada uno con su propio
# criterio de identidad, es la receta para divergencias silenciosas: la misma
# OT entrando dos veces, o colgada del cliente equivocado, sin ningún error.
#
# El código NO se borra: queda abajo del guard, intacto, como referencia de
# qué se cargó y cómo (los datos históricos que ya están en producción
# entraron por acá). Si alguna vez hay que releerlo para entender una fila
# vieja, está; lo que no puede es volver a ejecutarse.
#
# /api/admin/migracion/pagos-ot-abiertas NO se bloquea: solo inserta en
# pagos_orden y busca la orden por legacy_ot_no — que es el número de OT de
# Talca, la misma llave que usa el sync. No escribe identidad de órdenes ni
# crea clientes o equipos. Ver su docstring.
# ---------------------------------------------------------------------------

MENSAJE_REEMPLAZADO_POR_SYNC = (
    "Endpoint retirado: la sincronización con Talca lo reemplazó. "
    "Emparejaba las órdenes por RUT y por patente, y sobre los datos reales "
    "ninguno de los dos es una llave única (90 RUT duplicados en clientes, "
    "285 patentes duplicadas en vehiculos), así que correrlo junto al sync "
    "duplica órdenes o las cuelga del cliente equivocado en silencio. "
    "El sync empareja solo por el id numérico de Talca (talca_id). "
    "Reemplazo: 'python -m modulos.sync_talca ordenes' "
    "(el sync cubre clientes, vehículos y órdenes). Si hace falta recargar todo: "
    "'python -m modulos.sync_talca <entidad> --recarga-completa'."
)


def _respuesta_reemplazado_por_sync(endpoint, detalle=""):
    """Respuesta 410 Gone de los endpoints de migración que el sync reemplazó.

    410 y no 404: la ruta existió y se retiró a propósito. Un 404 haría
    pensar que está mal escrita la URL y llevaría a buscar el endpoint que
    'falta' en vez de leer por qué no debe correr.

    'detalle' se agrega al final del mensaje común, para los endpoints que
    caen por algo propio además del emparejamiento por RUT y patente (el
    correlativo compartido de garantias-abiertas, la falta de legacy_ot_no
    de importar). El motivo tiene que viajar en la respuesta: quien reciba
    el 410 no siempre va a poder leer el código."""
    return (
        jsonify(
            {
                "error": MENSAJE_REEMPLAZADO_POR_SYNC + (f" {detalle}" if detalle else ""),
                "codigo": "reemplazado_por_sync_talca",
                "endpoint": endpoint,
                "reemplazo": "python -m modulos.sync_talca ordenes",
            }
        ),
        410,
    )


@app.route("/api/admin/migracion/diagnostico-archivos", methods=["GET"])
@requiere_rol()
def migracion_diagnostico_archivos():
    """Diagnóstico de un solo uso: confirma si los archivos de la migración
    histórica (data/migracion_*.json) efectivamente llegaron al filesystem
    del contenedor desplegado -- se agregó porque el primer intento de
    correr la migración dio 'archivo no encontrado' pese a que el commit sí
    los incluía, para descartar si es un problema del build (ej. un
    Dockerfile que copia archivos explícitos y se olvidó de data/) en vez de
    tener que adivinarlo a ciegas."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "data")
    info = {"base_dir": base_dir, "base_dir_listado": sorted(os.listdir(base_dir))}
    if os.path.isdir(data_dir):
        info["data_dir_existe"] = True
        info["data_dir_listado"] = []
        for nombre in sorted(os.listdir(data_dir)):
            ruta = os.path.join(data_dir, nombre)
            info["data_dir_listado"].append({"nombre": nombre, "tamano_kb": round(os.path.getsize(ruta) / 1024, 1)})
    else:
        info["data_dir_existe"] = False
    return jsonify(info)


@app.route("/api/admin/migracion/backfill-legacy-no", methods=["POST"])
@requiere_rol()
def migracion_backfill_legacy_no():
    """REEMPLAZADO POR EL SYNC — responde 410. Ver el bloque "REEMPLAZADOS POR
    EL SYNC CON TALCA" más arriba.

    Rellenaba ordenes_trabajo.legacy_ot_no en las 101 OT abiertas ya migradas
    **emparejando por patente**, usando data/migracion_legacy_no_abiertas.json.
    Eso es exactamente lo que el sync tiene prohibido: hay 285 patentes
    duplicadas en Talca, y el propio JSON de entrada trae valores que no son
    patentes ({"patente": "SEPT.21", "legacy_no": 2671} es la primera fila).
    El sync trae legacy_ot_no/talca_id directo del id de Talca, sin adivinar.

    El código de abajo queda como referencia de cómo se cargaron los datos
    históricos que ya están en producción. No se ejecuta."""
    return _respuesta_reemplazado_por_sync("/api/admin/migracion/backfill-legacy-no")

    try:
        mapa = _cargar_json_data("migracion_legacy_no_abiertas.json")
    except FileNotFoundError:
        return jsonify({"error": "No se encontró data/migracion_legacy_no_abiertas.json en el deploy"}), 400

    db = get_db()
    resumen = {"actualizadas": 0, "ya_tenian": 0, "sin_match": []}
    for fila in mapa:
        patente = (fila.get("patente") or "").strip().upper()
        legacy_no = fila.get("legacy_no")
        if not patente or legacy_no is None:
            continue
        orden = db.execute(
            """SELECT o.id, o.legacy_ot_no FROM ordenes_trabajo o
               JOIN equipos e ON e.id = o.equipo_id
               WHERE e.identificador = ? LIMIT 1""",
            (patente,),
        ).fetchone()
        if not orden:
            resumen["sin_match"].append({"patente": patente, "legacy_no": legacy_no})
            continue
        if orden["legacy_ot_no"] is not None:
            resumen["ya_tenian"] += 1
            continue
        db.execute("UPDATE ordenes_trabajo SET legacy_ot_no = ? WHERE id = ?", (legacy_no, orden["id"]))
        resumen["actualizadas"] += 1
    db.commit()
    return jsonify(resumen)


@app.route("/api/admin/migracion/pagos-ot-abiertas", methods=["POST"])
@requiere_rol()
def migracion_pagos_ot_abiertas():
    """Carga los abonos/pagos históricos (tabla legacy pago_ot) de las 101 OT
    abiertas ya migradas, usando data/migracion_pagos_ot_abiertas.json.
    Idempotente por orden (usuario = 'Migración histórica' en pagos_orden).

    NO se bloqueó junto a los otros dos, y la razón es la llave: busca la
    orden por legacy_ot_no, que es el número de OT en Talca — la misma
    identidad que usa el sync. No empareja por RUT ni por patente, no crea
    clientes ni equipos, y no escribe una sola columna de ordenes_trabajo:
    solo inserta filas en pagos_orden. No compite con el sync.

    Sí dependía de backfill-legacy-no (ahora 410) para que esas 101 órdenes
    tuvieran legacy_ot_no. Eso no lo rompe: cuando el sync de órdenes entre
    (paso 5 del plan en CLAUDE.md), legacy_ot_no lo va a poblar el sync desde
    el id de Talca, que es lo correcto. Las órdenes que no tengan el número
    aparecen en 'ordenes_sin_match' en vez de escribirse en el lugar
    equivocado.

    Pendiente cuando llegue el sync de órdenes: si alguna vez llega a
    importar pago_ot desde Talca, este endpoint pasa a ser un segundo
    escritor de pagos_orden y hay que bloquearlo también. Hoy el sync no
    toca pagos."""
    try:
        datos = _cargar_json_data("migracion_pagos_ot_abiertas.json")
    except FileNotFoundError:
        return jsonify({"error": "No se encontró data/migracion_pagos_ot_abiertas.json en el deploy"}), 400

    db = get_db()
    ts = now_iso()
    usuario_migracion = "Migración histórica"
    resumen = {"ordenes_procesadas": 0, "ordenes_sin_match": [], "ordenes_ya_migradas": 0, "pagos_creados": 0}

    for entrada in datos:
        legacy_no = entrada.get("legacy_no")
        orden = db.execute(
            "SELECT id FROM ordenes_trabajo WHERE legacy_ot_no = ?", (legacy_no,)
        ).fetchone()
        if not orden:
            resumen["ordenes_sin_match"].append({"legacy_no": legacy_no, "patente": entrada.get("patente")})
            continue
        orden_id = orden["id"]
        ya_migrada = db.execute(
            "SELECT 1 FROM pagos_orden WHERE orden_id = ? AND usuario = ?",
            (orden_id, usuario_migracion),
        ).fetchone()
        if ya_migrada:
            resumen["ordenes_ya_migradas"] += 1
            continue
        resumen["ordenes_procesadas"] += 1
        for p in entrada.get("pagos") or []:
            db.execute(
                """INSERT INTO pagos_orden (orden_id, metodo_pago, monto, fecha, usuario, creado_en)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (orden_id, p.get("metodo_pago") or "otro", float(p.get("monto") or 0), p.get("fecha"), usuario_migracion, ts),
            )
            resumen["pagos_creados"] += 1
        db.commit()

    return jsonify(resumen)


@app.route("/api/admin/migracion/ordenes-historicas", methods=["POST"])
@requiere_rol()
def migracion_ordenes_historicas():
    """REEMPLAZADO POR EL SYNC — responde 410. Ver el bloque "REEMPLAZADOS POR
    EL SYNC CON TALCA" más arriba.

    Emparejaba el cliente por RUT y el equipo por patente, creando ambos si no
    los encontraba, e insertaba en ordenes_trabajo con legacy_ot_no como única
    marca de identidad. Dos escritores sobre la misma tabla con criterios de
    identidad distintos (RUT/patente contra talca_id) divergen en silencio: en
    data/migracion_historica_ordenes.json hay 16 patentes que aparecen con más
    de un RUT, afectando a 65 de sus 2.983 órdenes.

    El código de abajo queda como referencia de cómo se cargaron los datos
    históricos que ya están en producción. No se ejecuta.

    --- Documentación original ---
    Migra las OT CERRADAS de los últimos 3 años (base legacy TalleresTC)
    como historial completo: cliente, equipo, orden (estado 'entregado'),
    repuestos, mano de obra externa y pagos — todo en un solo paso, replicando
    lo que ya existe en el sistema viejo. Usa data/migracion_historica_ordenes.json
    (generado una sola vez desde la base legacy).

    Por el volumen (~3.000 órdenes, ~48.000 filas entre ítems y pagos) se
    procesa en lotes vía los parámetros de query ?offset=&limite= para no
    arriesgar un timeout del proxy en una sola request; se llama repetidas
    veces avanzando el offset hasta que 'siguiente_offset' sea null.
    Idempotente por legacy_ot_no: si la orden ya existe, se salta completa."""
    return _respuesta_reemplazado_por_sync("/api/admin/migracion/ordenes-historicas")

    try:
        datos = _cargar_json_data("migracion_historica_ordenes.json")
    except FileNotFoundError:
        return jsonify({"error": "No se encontró data/migracion_historica_ordenes.json en el deploy"}), 400

    try:
        offset = int(request.args.get("offset", 0))
        limite = int(request.args.get("limite", 300))
    except (TypeError, ValueError):
        return jsonify({"error": "offset/limite inválidos"}), 400

    lote = datos[offset:offset + limite]
    db = get_db()
    ts = now_iso()
    usuario_migracion = "Migración histórica"

    rut_a_cliente_id = {}
    for fila in db.execute("SELECT id, rut FROM clientes WHERE rut != ''"):
        clave = re.sub(r"[.\-]", "", fila["rut"] or "").upper()
        if clave:
            rut_a_cliente_id[clave] = fila["id"]

    patente_a_equipo = {}
    for fila in db.execute("SELECT id, cliente_id, identificador FROM equipos WHERE identificador != ''"):
        patente_a_equipo.setdefault(fila["identificador"].strip().upper(), {"equipo_id": fila["id"], "cliente_id": fila["cliente_id"]})

    resumen = {
        "rango": {"offset": offset, "limite": limite, "total_archivo": len(datos)},
        "ordenes_creadas": 0, "ordenes_ya_existian": 0, "ordenes_sin_rut": 0,
        "items_repuesto_creados": 0, "items_mano_obra_creados": 0, "pagos_creados": 0,
        "clientes_creados": 0, "equipos_creados": 0,
    }

    for o in lote:
        legacy_no = o.get("legacy_no")
        ya_existe = db.execute("SELECT 1 FROM ordenes_trabajo WHERE legacy_ot_no = ?", (legacy_no,)).fetchone()
        if ya_existe:
            resumen["ordenes_ya_existian"] += 1
            continue

        rut_clave = re.sub(r"[.\-]", "", o.get("cliente_rut") or "").upper()
        if not rut_clave:
            resumen["ordenes_sin_rut"] += 1
            continue

        cliente_id = rut_a_cliente_id.get(rut_clave)
        if cliente_id is None:
            cur = db.execute(
                "INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas, creado_en) VALUES (?, ?, '', ?, '', '', ?)",
                (
                    (o.get("cliente_nombre") or "").strip().upper() or "SIN NOMBRE",
                    (o.get("cliente_telefono") or "").strip(),
                    formatear_rut(o.get("cliente_rut") or ""),
                    ts,
                ),
            )
            cliente_id = cur.lastrowid
            rut_a_cliente_id[rut_clave] = cliente_id
            resumen["clientes_creados"] += 1

        patente = (o.get("patente") or "").strip().upper()
        equipo_id = None
        if patente:
            info = patente_a_equipo.get(patente)
            if info and info["cliente_id"] == cliente_id:
                equipo_id = info["equipo_id"]
            elif not info:
                cur = db.execute(
                    "INSERT INTO equipos (cliente_id, identificador, marca, modelo, notas, creado_en) VALUES (?, ?, ?, ?, '', ?)",
                    (cliente_id, patente, (o.get("marca") or "").strip().upper(), (o.get("modelo") or "").strip().upper(), ts),
                )
                equipo_id = cur.lastrowid
                patente_a_equipo[patente] = {"equipo_id": equipo_id, "cliente_id": cliente_id}
                resumen["equipos_creados"] += 1
            # si la patente ya existe pero de OTRO cliente (auto vendido/traspasado
            # entre clientes del taller a lo largo de los años), se deja sin
            # equipo_id antes que enlazarlo al dueño incorrecto -- el dato de
            # patente/marca/modelo igual queda visible en descripcion_problema.

        precio_val = o.get("precio")
        try:
            precio_val = float(precio_val) if precio_val not in (None, "") else None
        except (TypeError, ValueError):
            precio_val = None

        fecha_recepcion = (o.get("fecha_recepcion") or "").strip().replace(" ", "T") or (o.get("fecha_cierre") or ts)
        fecha_cierre = (o.get("fecha_cierre") or "").strip() or None
        descripcion = (o.get("requerimiento") or "").strip().upper()
        if patente and not equipo_id:
            descripcion = f"[VEHÍCULO {patente} {o.get('marca', '')} {o.get('modelo', '')}] {descripcion}".strip()

        cur = db.execute(
            """INSERT INTO ordenes_trabajo (
                cliente_id, equipo_id, estado, descripcion_problema, mecanico,
                fecha_recepcion, fecha_entrega_real, usuario, creado_en, actualizado_en,
                precio_referencial, precio_iva, legacy_ot_no
            ) VALUES (?, ?, 'entregado', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cliente_id, equipo_id, descripcion, (o.get("mecanico") or "").strip(),
                fecha_recepcion, fecha_cierre, usuario_migracion, ts, ts,
                precio_val, 1 if o.get("coniva") else 0, legacy_no,
            ),
        )
        orden_id = cur.lastrowid
        resumen["ordenes_creadas"] += 1

        for r in o.get("repuestos") or []:
            descripcion_item = " - ".join(
                x.strip() for x in [(r.get("modelo") or ""), (r.get("descripcion") or "")] if x and x.strip()
            ) or "(sin descripción)"
            try:
                costo = float(r.get("costo_unitario") or 0)
            except (TypeError, ValueError):
                costo = 0.0
            db.execute(
                """INSERT INTO orden_items (orden_id, tipo, descripcion, cantidad, costo_unitario, usuario, creado_en)
                   VALUES (?, 'repuesto', ?, 1, ?, ?, ?)""",
                (orden_id, descripcion_item.upper(), costo, usuario_migracion, ts),
            )
            resumen["items_repuesto_creados"] += 1

        for m in o.get("mano_obra") or []:
            descripcion_item = " - ".join(
                x.strip() for x in [(m.get("nombre_reparador") or ""), (m.get("detalle") or "")] if x and x.strip()
            ) or "(mano de obra externa)"
            try:
                cantidad = float(m.get("cantidad") or 1) or 1
            except (TypeError, ValueError):
                cantidad = 1
            try:
                precio_item = float(m.get("precio") or 0)
            except (TypeError, ValueError):
                precio_item = 0.0
            db.execute(
                """INSERT INTO orden_items (orden_id, tipo, descripcion, cantidad, costo_unitario, usuario, creado_en, detalle)
                   VALUES (?, 'mano_obra', ?, ?, ?, ?, ?, 'Reparación externa (migración histórica)')""",
                (orden_id, descripcion_item.upper(), cantidad, precio_item, usuario_migracion, ts),
            )
            resumen["items_mano_obra_creados"] += 1

        for p in o.get("pagos") or []:
            db.execute(
                """INSERT INTO pagos_orden (orden_id, metodo_pago, monto, fecha, usuario, creado_en)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (orden_id, p.get("metodo_pago") or "otro", float(p.get("monto") or 0), p.get("fecha"), usuario_migracion, ts),
            )
            resumen["pagos_creados"] += 1

    db.commit()
    resumen["siguiente_offset"] = offset + limite if offset + limite < len(datos) else None
    return jsonify(resumen)


# ---------------------------------------------------------------------------
# API: Reseteo de datos operativos (para la migración desde el sistema PHP)
# ---------------------------------------------------------------------------
#
# Antes de cargar la base de datos migrada desde el sistema PHP antiguo, el
# taller pidió poder dejar en blanco todo lo operativo (órdenes de trabajo,
# productos de bodega y todo lo que se genera a partir de ellos: movimientos,
# facturas de compra, cuentas por pagar/cobrar, garantías, convertidores de
# torque, caja chica, documentos tributarios, etc.) SIN perder clientes ni
# sus vehículos (equipos) asociados -- justamente el dato que más cuesta
# reconstruir a mano y que no depende en nada del sistema PHP.
#
# RESET_OPERATIVO_TABLAS lista, en orden hijo -> padre, todas las tablas que
# se vacían. El orden es solo por prolijidad al leer/auditar la lista --
# durante el borrado real se desactiva "PRAGMA foreign_keys" (mismo
# mecanismo que ya usa _reconstruir_tabla_legada_si_hace_falta en core.py) y
# se verifica integridad al final con "PRAGMA foreign_key_check" antes de
# confirmar, así que un orden distinto no rompería nada.
#
# OJO -- esta lista NO es necesariamente todo lo que se borra en la
# práctica: ver _reset_operativo_tablas_a_vaciar() más abajo, que le suma
# cualquier tabla '<algo>_legado...' huérfana que haya quedado de una
# reconstrucción histórica de esquema (ver _reconstruir_tabla_legada_si_hace_falta
# y _reparar_fk_hacia_tabla_renombrada en core.py -- garantias/garantia_items
# y convertidores_torque/convertidor_items ya tuvieron incidentes así en
# producción). Esas tablas '_legado' no aparecen en ningún CREATE TABLE del
# código fuente (se crean en caliente con ALTER TABLE ... RENAME TO), así
# que no pueden listarse a mano acá -- si una de ellas conserva una llave
# foránea propia hacia alguna tabla de esta lista (ej. la columna vieja
# 'orden_id' que tenía garantias antes de existir 'orden_origen_id'),
# quedaría con una referencia rota después del borrado si no se detecta.
#
# Tablas que este reseteo NUNCA toca (quedan intactas):
#   - clientes, equipos              (lo que el taller pidió preservar)
#   - usuarios                        (cuentas del sistema, no son datos del taller)
#   - categorias, marcas, modelos, ubicaciones   (catálogos/listas de apoyo)
#   - configuracion_facturacion       (configuración de la empresa emisora de DTE)
#   - estado_resultado_manual         (datos manuales de contabilidad por mes,
#                                       no se generan a partir de OT/productos)
#   - respaldos_log                   (bitácora de respaldos -- incluye el que
#                                       este mismo reseteo crea automáticamente
#                                       antes de borrar nada; borrarla sería
#                                       contraproducente justo cuando más se
#                                       necesita ese historial)
#   - login_intentos_fallidos         (contador de intentos fallidos de login,
#                                       seguridad de las cuentas del sistema,
#                                       no datos del taller)
RESET_OPERATIVO_TABLAS = [
    # -- Finanzas ligadas a facturas de compra / OT --
    "cxp_pagos",
    "cuentas_por_pagar",
    "cuentas_por_cobrar",
    "factura_gastos_generales",
    "factura_lineas",
    "facturas_whatsapp_pendientes",
    "facturas_finanzas_pendientes",
    "facturas",
    # -- Caja Chica --
    "caja_gasto_detalles",
    "caja_movimientos",
    "caja_rendiciones",
    # -- Avisos/bitácoras ligados a una OT --
    "pre_entrega",
    "pruebas_ruta",
    "sugerencias_diagnostico",
    "fotos_vehiculo",
    "pagos_orden",
    "qc_checklist_items",
    "presupuesto_items",
    # -- Garantías --
    "garantia_items",
    "garantias",
    # -- Convertidores de torque --
    "convertidor_items",
    "convertidores_torque",
    # -- Órdenes de trabajo --
    "orden_items",
    "ordenes_trabajo",
    # -- Registro de las órdenes preexistentes que el sync de Talca reclamó en
    #    vez de duplicar (ver _adoptar_orden en modulos/sync_talca.py). Va con
    #    'ordenes_trabajo' y NO con las tablas preservadas del sync: cada fila
    #    apunta a una orden por su id, así que vaciada esa tabla la lista
    #    quedaría describiendo filas que ya no existen. Es lo contrario de
    #    'sync_talca_omitidos' y 'sync_talca_reasignados', que siguen siendo
    #    ciertas porque el reseteo preserva clientes y equipos. --
    "sync_talca_adopciones",
    # -- Facturación electrónica (DTE) emitida a clientes --
    "documentos_tributarios",
    # -- Inventario / bodega --
    "reservas",
    "merma_proveedores",
    "stock_minimo_calculo",
    "stock_proveedores",
    "movimientos",
    "productos",
    # -- Proveedores (a pedido del taller: se borra el catálogo completo,
    #    no solo sus facturas/CxP) --
    "proveedores",
    # -- Agente de WhatsApp --
    "agente_mensajes",
    "agente_mecanicos",
    # -- Log de alertas de stock (quedaría referenciando productos ya borrados) --
    "alertas_stock_log",
    # -- Bitácora de eventos por OT (columnas: orden_id NOT NULL REFERENCES
    #    ordenes_trabajo, evento, marca_tiempo, usuario). Encontrada por
    #    /api/admin/reset-operativo/diagnostico -- no aparece en ningún
    #    CREATE TABLE del código fuente actual (ni se lee ni se escribe
    #    desde ninguna pantalla hoy), así que es de una función que ya se
    #    sacó del sistema. Estaba vacía (0 filas) al encontrarla. --
    "orden_tiempos",
]

RESET_OPERATIVO_FRASE_CONFIRMACION = "RESETEAR TALLER"

# Tablas que el reseteo deja intactas a propósito (ver comentario largo más
# arriba de RESET_OPERATIVO_TABLAS). Se separa en su propia constante -- y no
# se deja como un literal metido dentro de vista_previa() -- porque
# reset_operativo_diagnostico() también la necesita para saber qué tablas de
# la base están "clasificadas" (con una decisión tomada, borrar o preservar)
# y cuáles quedaron fuera de ambas listas sin que nadie se diera cuenta.
RESET_OPERATIVO_TABLAS_PRESERVADAS = [
    "clientes", "equipos", "usuarios", "categorias", "marcas", "modelos",
    "ubicaciones", "configuracion_facturacion", "estado_resultado_manual",
    "respaldos_log",
    # -- Sincronización con Talca.PHP (ver modulos/sync_talca.py). Se
    #    preservan por la misma razón que 'clientes': el reseteo deja los
    #    clientes en pie, y con ellos su talca_id, así que borrar la marca de
    #    agua obligaría a repetir la carga completa desde Talca sin ninguna
    #    ganancia. 'sync_talca_omitidos' además es el registro auditable de
    #    qué filas del sistema viejo se decidió no importar. --
    "sync_talca_estado", "sync_talca_omitidos", "sync_talca_reasignados",
    # -- Push Python -> Talca (ver modulos/push_talca.py). 'sync_talca_push_pendientes'
    #    NO se puede vaciar: cada entrada es un cambio que Talca todavía no tiene,
    #    y la fila local sigue con push_pendiente=1. Borrar la cola dejaría esas
    #    filas marcadas para siempre, y el pull deja de actualizar lo que tiene ese
    #    flag. 'sync_talca_conflictos' es registro histórico de revisión manual. --
    "sync_talca_push_pendientes", "sync_talca_conflictos",
    # login_intentos_fallidos: contador de intentos fallidos de login (ver
    # Migración 34) -- es seguridad de las cuentas del sistema, igual que
    # 'usuarios' más arriba, no datos operativos del taller.
    "login_intentos_fallidos",
]


def _reset_operativo_tablas_reales(db):
    """Nombres de TODAS las tablas que existen hoy en la base (sin las
    internas 'sqlite_%'), consultado directo contra sqlite_master -- no
    contra ninguna lista escrita a mano. Es la fuente de verdad que usan
    tanto _reset_operativo_tablas_legado (para encontrar tablas '_legado'
    huérfanas) como _reset_operativo_tablas_a_vaciar (para nunca intentar
    un DELETE contra una tabla de RESET_OPERATIVO_TABLAS que en este
    ambiente en particular no llegó a crearse -- ej. 'orden_tiempos', que
    quedó en la lista por una función ya retirada del código y que por lo
    tanto ni init_db() la crea en una base nueva)."""
    return {
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _reset_operativo_tablas_legado(db, todas_las_tablas=None):
    """Tablas '<tabla>_legado', '<tabla>_legado_fk', o esas mismas variantes
    con un timestamp pegado al final (ver core.py: _reconstruir_tabla_legada_si_hace_falta
    y _reparar_fk_hacia_tabla_renombrada, que arman el nombre así), para
    CUALQUIER tabla de RESET_OPERATIVO_TABLAS -- se detectan por nombre
    directamente contra la base real (no hay como saberlo desde el código
    fuente, ver comentario arriba de RESET_OPERATIVO_TABLAS)."""
    todas_las_tablas = _reset_operativo_tablas_reales(db) if todas_las_tablas is None else todas_las_tablas
    encontradas = []
    for base in RESET_OPERATIVO_TABLAS:
        patron = re.compile(rf"^{re.escape(base)}_legado(_fk)?(_\d+)?$")
        encontradas.extend(sorted(t for t in todas_las_tablas if patron.match(t)))
    return encontradas


def _reset_operativo_tablas_a_vaciar(db):
    """La lista real de tablas a vaciar: las de RESET_OPERATIVO_TABLAS que
    de verdad existen en esta base (ver _reset_operativo_tablas_reales --
    protege contra una entrada de la lista, como 'orden_tiempos', que en
    algún ambiente nunca se llegó a crear) más cualquier tabla '_legado'
    huérfana que se le haya pegado (ver _reset_operativo_tablas_legado). Se
    recalcula en cada request -- no es cara (una sola consulta a
    sqlite_master) y así 'vista-previa', 'diagnostico' y 'ejecutar' siempre
    están mirando exactamente lo mismo."""
    todas_las_tablas = _reset_operativo_tablas_reales(db)
    tablas_conocidas_que_existen = [t for t in RESET_OPERATIVO_TABLAS if t in todas_las_tablas]
    return tablas_conocidas_que_existen + _reset_operativo_tablas_legado(db, todas_las_tablas)


@app.route("/api/admin/reset-operativo/vista-previa", methods=["GET"])
@requiere_rol()
def reset_operativo_vista_previa():
    """Solo lectura: cuenta cuántas filas hay HOY en cada tabla que el
    reseteo borraría, para mostrarlo ANTES de ejecutar nada (mismo espíritu
    que el 'requiere_forzar' de eliminar_factura, pero acá se necesita el
    detalle completo -- son ~30 tablas -- así que se separó en su propio
    endpoint de solo lectura en vez de meterlo en la respuesta de error del
    endpoint que ejecuta)."""
    db = get_db()
    tablas_a_vaciar = _reset_operativo_tablas_a_vaciar(db)
    conteos = {tabla: db.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0] for tabla in tablas_a_vaciar}
    return jsonify({
        "conteos": conteos,
        "total_filas": sum(conteos.values()),
        "tablas_preservadas": RESET_OPERATIVO_TABLAS_PRESERVADAS,
        "frase_confirmacion_requerida": RESET_OPERATIVO_FRASE_CONFIRMACION,
    })


@app.route("/api/admin/reset-operativo/diagnostico", methods=["GET"])
@requiere_rol()
def reset_operativo_diagnostico():
    """Solo lectura, no borra ni modifica nada: revisa la base REAL (no la
    lista de tablas escrita a mano en el código) para encontrar por qué
    'ejecutar' se abortaría con 'quedaron referencias rotas después de
    borrar'. Pensado para correrlo cuantas veces haga falta, sin riesgo,
    cuando 'ejecutar' avisa ese error -- así no hay que intentar el reseteo
    a ciegas otra vez para ver qué tabla es.

    Tres cosas puede encontrar:
      1. 'tablas_no_clasificadas': tablas que existen HOY en la base pero
         no están ni en RESET_OPERATIVO_TABLAS (se borran) ni en
         RESET_OPERATIVO_TABLAS_PRESERVADAS (se conservan a propósito) --
         ej. una tabla nueva que se agregó después de escribir esta lista,
         una tabla '<algo>_legado...' que quedó de una reconstrucción vieja
         (ver _reconstruir_tabla_legada_si_hace_falta en core.py), o una
         tabla huérfana de una función que ya se sacó del código (nadie la
         lee ni la escribe hoy, pero nunca se hizo DROP TABLE).
      2. 'esquema_tablas_no_clasificadas': para cada una de esas tablas sin
         clasificar, sus columnas (PRAGMA table_info) y llaves foráneas
         declaradas (PRAGMA foreign_key_list) -- para poder decidir de qué
         se trata y si hay que sumarla al borrado o a las preservadas, sin
         necesidad de acceso directo a la base.
      3. 'riesgos': para cada tabla que el reseteo NO va a vaciar (esté
         clasificada como preservada, o ni siquiera clasificada), revisa
         sus llaves foráneas (PRAGMA foreign_key_list) buscando alguna que
         apunte a una tabla que SÍ se va a vaciar -- y cuenta cuántas filas
         tienen ese valor lleno hoy. Cualquier fila ahí es justamente la
         que después de borrar quedaría con una referencia rota."""
    db = get_db()
    tablas_a_vaciar = _reset_operativo_tablas_a_vaciar(db)
    todas_las_tablas = sorted(_reset_operativo_tablas_reales(db))
    tablas_clasificadas = set(tablas_a_vaciar) | set(RESET_OPERATIVO_TABLAS_PRESERVADAS)
    tablas_no_clasificadas = sorted(t for t in todas_las_tablas if t not in tablas_clasificadas)

    esquema_tablas_no_clasificadas = {}
    for tabla in tablas_no_clasificadas:
        columnas = [
            {"nombre": c["name"], "tipo": c["type"], "not_null": bool(c["notnull"])}
            for c in db.execute(f"PRAGMA table_info({tabla})").fetchall()
        ]
        llaves_foraneas = [
            {"columna": fk["from"], "tabla_referenciada": fk["table"]}
            for fk in db.execute(f"PRAGMA foreign_key_list({tabla})").fetchall()
        ]
        filas = db.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
        esquema_tablas_no_clasificadas[tabla] = {
            "columnas": columnas, "llaves_foraneas": llaves_foraneas, "filas": filas,
        }

    riesgos = []
    for tabla in todas_las_tablas:
        if tabla in tablas_a_vaciar:
            continue  # esta tabla también se vacía completa -- no puede quedar con nada colgando de sí misma
        for fk in db.execute(f"PRAGMA foreign_key_list({tabla})").fetchall():
            tabla_referenciada = fk["table"]
            if tabla_referenciada not in tablas_a_vaciar:
                continue
            columna = fk["from"]
            filas_con_referencia = db.execute(
                f"SELECT COUNT(*) FROM {tabla} WHERE {columna} IS NOT NULL"
            ).fetchone()[0]
            if filas_con_referencia > 0:
                riesgos.append({
                    "tabla": tabla,
                    "columna": columna,
                    "tabla_referenciada": tabla_referenciada,
                    "filas_con_referencia": filas_con_referencia,
                    "tabla_preservada_a_proposito": tabla in RESET_OPERATIVO_TABLAS_PRESERVADAS,
                })

    return jsonify({
        "total_tablas_en_la_base": len(todas_las_tablas),
        "tablas_no_clasificadas": tablas_no_clasificadas,
        "esquema_tablas_no_clasificadas": esquema_tablas_no_clasificadas,
        "riesgos": riesgos,
        "listo_para_ejecutar": not tablas_no_clasificadas and not riesgos,
    })


@app.route("/api/admin/reset-operativo/ejecutar", methods=["POST"])
@requiere_rol()
def reset_operativo_ejecutar():
    """Vacía TODAS las tablas de _reset_operativo_tablas_a_vaciar() (la lista
    fija RESET_OPERATIVO_TABLAS más cualquier tabla '_legado' huérfana que
    se le haya detectado), preservando clientes/equipos/usuarios/catálogos
    (ver comentario arriba de la lista). Pensado como paso previo a cargar
    la base migrada desde el sistema PHP.

    Salvaguardas, en orden:
      1. Exige la frase exacta RESETEAR TALLER en el body -- es una
         operación mucho más amplia que 'eliminar factura', así que un
         simple forzar=1 se sentía insuficiente.
      2. Crea un respaldo automático de TODA la base ANTES de borrar nada
         (_crear_respaldo_local, mismo mecanismo que el respaldo local por
         hora). Si el respaldo falla por cualquier motivo (ej. disco casi
         lleno), se aborta sin tocar un solo dato.
      3. Borra todo dentro de una única transacción con las llaves foráneas
         desactivadas (mismo mecanismo que _reconstruir_tabla_legada_si_hace_falta
         en core.py), y antes de confirmar corre 'PRAGMA foreign_key_check'
         -- si detecta cualquier referencia que haya quedado rota (no
         debería pasar, dado que se listan TODAS las tablas dependientes),
         revierte todo y no cambia nada en vez de dejar la base a medias."""
    body = request.get_json(silent=True) or {}
    confirmacion = (body.get("confirmacion") or "").strip()
    if confirmacion != RESET_OPERATIVO_FRASE_CONFIRMACION:
        return jsonify({
            "error": f"Para ejecutar el reseteo hay que mandar el campo 'confirmacion' con el texto exacto "
                     f"'{RESET_OPERATIVO_FRASE_CONFIRMACION}'.",
        }), 400

    respaldo_ok, respaldo_detalle = _crear_respaldo_local()
    if not respaldo_ok:
        return jsonify({
            "error": "Se abortó el reseteo antes de borrar nada: no se pudo crear el respaldo automático previo "
                     f"({respaldo_detalle}). Libera espacio o revisa el error e inténtalo de nuevo.",
        }), 500

    db = get_db()
    tablas_a_vaciar = _reset_operativo_tablas_a_vaciar(db)
    db.commit()  # cierra cualquier transacción implícita pendiente antes de tocar el pragma
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        eliminadas = {}
        for tabla in tablas_a_vaciar:
            cur = db.execute(f"DELETE FROM {tabla}")
            eliminadas[tabla] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        violaciones = db.execute("PRAGMA foreign_key_check").fetchall()
        if violaciones:
            db.execute("ROLLBACK")
            return jsonify({
                "error": "Se abortó el reseteo: quedaron referencias rotas después de borrar (no debería pasar). "
                         "No se modificó ningún dato -- revisa el detalle y avisa antes de reintentar.",
                "violaciones": [dict(v) for v in violaciones][:10],
            }), 500

        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
        db.commit()

    return jsonify({
        "ok": True,
        "respaldo_local_creado": respaldo_detalle,
        "eliminadas": eliminadas,
        "total_eliminadas": sum(eliminadas.values()),
    })


init_db()
iniciar_hilo_respaldos()
iniciar_hilo_respaldos_locales()
iniciar_hilo_alertas_stock()
iniciar_hilo_recalculo_stock_minimo()
# Solo arranca si SYNC_INTERVALO_SEGUNDOS está definida y es > 0. En local no
# lo está, así que el sync se sigue corriendo a mano.
iniciar_hilo_sync_talca()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
