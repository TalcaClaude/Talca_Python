"""
Sistema de Inventario - Taller Mecánico
Módulo 2: Administración de Inventario + Módulo 3: Organización Física de
Bodega + una primera pieza del Módulo 4: Facturas de compra.

Backend en Flask + SQLite. Sin dependencias raras: solo Flask,
para que sea fácil de instalar y correr en cualquier PC o servidor.
"""

import os
import sqlite3
import time
import threading
import smtplib
from email.message import EmailMessage
from datetime import datetime

from flask import Flask, g, jsonify, request, render_template, send_file, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "inventario.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion-usando-la-variable-SECRET_KEY")

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

# ---------------------------------------------------------------------------
# Protección básica contra fuerza bruta en el login
# ---------------------------------------------------------------------------

LOGIN_INTENTOS_FALLIDOS = {}  # ip -> [timestamps de intentos fallidos recientes]
LOGIN_MAX_INTENTOS = 5
LOGIN_VENTANA_SEGUNDOS = 300  # 5 minutos


def _intentos_recientes(ip):
    ahora = time.time()
    intentos = [t for t in LOGIN_INTENTOS_FALLIDOS.get(ip, []) if ahora - t < LOGIN_VENTANA_SEGUNDOS]
    LOGIN_INTENTOS_FALLIDOS[ip] = intentos
    return intentos


def _login_bloqueado(ip):
    return len(_intentos_recientes(ip)) >= LOGIN_MAX_INTENTOS


def _registrar_intento_fallido(ip):
    intentos = _intentos_recientes(ip)
    intentos.append(time.time())
    LOGIN_INTENTOS_FALLIDOS[ip] = intentos


def _limpiar_intentos(ip):
    LOGIN_INTENTOS_FALLIDOS.pop(ip, None)

USUARIO_INICIAL = "admin"
CLAVE_INICIAL = "taller2026"

# ---------------------------------------------------------------------------
# Respaldo automático por correo (Opción A: sin costo, sin infraestructura
# nueva). Se configura con variables de entorno; si no están, esta función
# simplemente no hace nada (el sistema sigue funcionando normal).
# ---------------------------------------------------------------------------

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_DESTINATARIO = os.environ.get("SMTP_DESTINATARIO", "") or SMTP_USER
RESPALDO_INTERVALO_HORAS = int(os.environ.get("RESPALDO_INTERVALO_HORAS", "24"))


def respaldo_automatico_configurado():
    return bool(SMTP_USER and SMTP_PASSWORD and SMTP_DESTINATARIO)


CATEGORIAS_INICIALES = ["Cuerpo de Válvulas", "Discos de Hierro", "Discos de Fricción", "Kit de Sellos"]
MODELOS_INICIALES = ["JF011", "JF015", "722.9", "722.6", "MPS6", "10R80"]
CATEGORIAS_MOVIMIENTO = [
    "Compra", "Venta", "Consumo interno", "Reserva",
    "Ajuste de inventario", "Devolución", "Traslado interno", "Merma o pérdida",
]


# ---------------------------------------------------------------------------
# Conexión a la base de datos
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)

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

        CREATE TABLE IF NOT EXISTS proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT UNIQUE NOT NULL,
            rut TEXT DEFAULT '',
            telefono TEXT DEFAULT '',
            email TEXT DEFAULT '',
            direccion TEXT DEFAULT '',
            creado_en TEXT NOT NULL
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
        """
    )
    db.commit()

    # ---- Migración 1: agrega productos.ubicacion_id si la base ya existía
    # de una versión sin Módulo 3 (sin perder los datos ya cargados).
    columnas = [c[1] for c in db.execute("PRAGMA table_info(productos)").fetchall()]
    if "ubicacion_id" not in columnas:
        db.execute("ALTER TABLE productos ADD COLUMN ubicacion_id INTEGER REFERENCES ubicaciones(id)")
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
                stock_minimo INTEGER NOT NULL DEFAULT 0,
                costo_compra REAL NOT NULL DEFAULT 0,
                creado_en TEXT NOT NULL,
                actualizado_en TEXT NOT NULL
            );

            INSERT INTO productos_nueva (
                id, codigo_interno, nombre, descripcion, categoria, marca, modelo,
                ubicacion, ubicacion_id, stock_disponible, stock_reservado,
                stock_minimo, costo_compra, creado_en, actualizado_en
            )
            SELECT
                id, codigo_interno, nombre, descripcion, categoria, marca, modelo,
                ubicacion, ubicacion_id, stock_disponible, stock_reservado,
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

    # ---- Migración 5: se elimina el campo "marca" del sistema (a pedido del
    # taller: la categoría + el modelo son suficientes para identificar el
    # producto). Se limpia en todos los productos ya cargados y se
    # recalcula el nombre automático por si dependía de la marca.
    db.execute("UPDATE productos SET marca = '' WHERE marca IS NOT NULL AND marca != ''")
    for r in db.execute("SELECT id, codigo_interno, categoria, modelo FROM productos").fetchall():
        nuevo_nombre = nombre_automatico(r[2], r[3], r[1], r[0])
        db.execute("UPDATE productos SET nombre = ? WHERE id = ?", (nuevo_nombre, r[0]))
    db.commit()

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

    db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def row_to_dict(row):
    d = dict(row)
    d["stock_total"] = d["stock_disponible"] + d["stock_reservado"]
    d["id_sistema"] = f"{d['id']:04d}"
    return d


def codigo_ubicacion(pasillo, estanteria, nivel):
    return f"P{pasillo}-E{estanteria}-N{nivel}"


def nombre_automatico(categoria, modelo, codigo_interno, id_num):
    """El producto ya no tiene un campo 'nombre' editable: se arma solo a
    partir de categoría + modelo (los datos que realmente lo identifican),
    con respaldos razonables si esos vienen vacíos."""
    partes = [p for p in [(categoria or "").strip(), (modelo or "").strip()] if p]
    if partes:
        return " - ".join(partes)
    if (codigo_interno or "").strip():
        return codigo_interno.strip()
    return f"Producto #{id_num:04d}"


PRODUCTO_SELECT = """
    SELECT p.*, u.codigo AS ubicacion_codigo, u.pasillo AS ubicacion_pasillo,
           u.estanteria AS ubicacion_estanteria, u.nivel AS ubicacion_nivel
    FROM productos p
    LEFT JOIN ubicaciones u ON p.ubicacion_id = u.id
"""


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _aplicar_entrada_stock(db, producto_row, cantidad, costo_unitario, motivo, usuario, factura=None, categoria="Compra"):
    """Aplica una entrada de stock a un producto, recalculando el Costo
    Promedio Ponderado si se informa costo_unitario, y deja el movimiento
    registrado con la referencia a la factura si corresponde.
    Devuelve el nuevo stock disponible.
    """
    stock_actual = producto_row["stock_disponible"]
    costo_actual = producto_row["costo_compra"]
    nuevo_stock = stock_actual + cantidad

    nuevo_costo_promedio = costo_actual
    if costo_unitario is not None:
        valor_actual = stock_actual * costo_actual
        valor_nuevo = cantidad * costo_unitario
        nuevo_costo_promedio = (valor_actual + valor_nuevo) / nuevo_stock if nuevo_stock > 0 else costo_unitario

    ts = now_iso()
    db.execute(
        "UPDATE productos SET stock_disponible = ?, costo_compra = ?, actualizado_en = ? WHERE id = ?",
        (nuevo_stock, nuevo_costo_promedio, ts, producto_row["id"]),
    )
    db.execute(
        """
        INSERT INTO movimientos (
            producto_id, fecha, tipo, categoria, cantidad, motivo, usuario,
            costo_unitario, factura_id, factura_numero, stock_resultante
        )
        VALUES (?, ?, 'entrada', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            producto_row["id"], ts, categoria, cantidad, motivo, usuario, costo_unitario,
            factura["id"] if factura else None,
            factura["numero"] if factura else None,
            nuevo_stock,
        ),
    )
    return nuevo_stock


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

RUTAS_PUBLICAS = {"login", "static"}


@app.before_request
def exigir_login():
    if request.endpoint in RUTAS_PUBLICAS or request.endpoint is None:
        return None
    if session.get("user_id"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Debes iniciar sesión"}), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user_id"):
            return redirect(url_for("index"))
        return render_template("login.html")

    ip = request.remote_addr or "desconocida"
    if _login_bloqueado(ip):
        return render_template(
            "login.html",
            error=f"Demasiados intentos fallidos. Espera {LOGIN_VENTANA_SEGUNDOS // 60} minutos antes de volver a intentar.",
        ), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    db = get_db()
    usuario = db.execute("SELECT * FROM usuarios WHERE username = ?", (username,)).fetchone()

    if usuario is None or not check_password_hash(usuario["password_hash"], password):
        _registrar_intento_fallido(ip)
        return render_template("login.html", error="Usuario o contraseña incorrectos."), 401

    _limpiar_intentos(ip)
    session.clear()
    session["user_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["nombre"] = usuario["nombre"] or usuario["username"]
    session.permanent = True
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sesion", methods=["GET"])
def sesion_actual():
    return jsonify({"username": session.get("username"), "nombre": session.get("nombre")})


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

    sql = PRODUCTO_SELECT + " WHERE 1=1"
    params = []

    if q:
        sql += """ AND (
            p.codigo_interno LIKE ? OR p.nombre LIKE ?
            OR p.modelo LIKE ? OR u.codigo LIKE ? OR CAST(p.id AS TEXT) LIKE ?
        )"""
        like = f"%{q}%"
        params += [like, like, like, like, like]

    if categoria:
        sql += " AND p.categoria = ?"
        params.append(categoria)

    if solo_criticos:
        sql += " AND p.stock_disponible <= p.stock_minimo"

    if ubicacion_id:
        sql += " AND p.ubicacion_id = ?"
        params.append(ubicacion_id)

    sql += " ORDER BY p.nombre COLLATE NOCASE ASC"

    rows = db.execute(sql, params).fetchall()
    productos = [row_to_dict(r) for r in rows]

    categorias = [r["categoria"] for r in db.execute(
        "SELECT DISTINCT categoria FROM productos WHERE categoria != '' ORDER BY categoria"
    ).fetchall()]

    return jsonify({
        "productos": productos,
        "categorias": categorias,
        "total": len(productos),
    })


@app.route("/api/productos/<int:producto_id>", methods=["GET"])
def obtener_producto(producto_id):
    db = get_db()
    row = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    movimientos = db.execute(
        "SELECT * FROM movimientos WHERE producto_id = ? ORDER BY fecha DESC, id DESC",
        (producto_id,),
    ).fetchall()

    producto = row_to_dict(row)
    producto["movimientos"] = [dict(m) for m in movimientos]
    return jsonify(producto)


@app.route("/api/productos", methods=["POST"])
def crear_producto():
    data = request.get_json(force=True, silent=True) or {}

    codigo_interno = (data.get("codigo_interno") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    modelo = (data.get("modelo") or "").strip()

    db = get_db()

    if codigo_interno:
        existe = db.execute(
            "SELECT id FROM productos WHERE codigo_interno = ?", (codigo_interno,)
        ).fetchone()
        if existe:
            return jsonify({"error": f"Ya existe un producto con código '{codigo_interno}'"}), 400

    if categoria or modelo:
        duplicado = db.execute(
            "SELECT id, id AS pid FROM productos WHERE lower(trim(categoria)) = lower(trim(?)) AND lower(trim(modelo)) = lower(trim(?))",
            (categoria, modelo),
        ).fetchone()
        if duplicado:
            return jsonify({
                "error": f"Ya existe un producto con esa categoría y modelo (#{duplicado[0]:04d}). "
                         f"Si necesitas más stock, ingrésalo con una factura en vez de crear un producto nuevo."
            }), 400

    ts = now_iso()
    try:
        stock_disponible = int(data.get("stock_disponible") or 0)
        stock_reservado = int(data.get("stock_reservado") or 0)
        stock_minimo = int(data.get("stock_minimo") or 0)
        costo_compra = float(data.get("costo_compra") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Stock y costo deben ser números"}), 400

    ubicacion_id = data.get("ubicacion_id") or None
    if ubicacion_id:
        try:
            ubicacion_id = int(ubicacion_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Ubicación inválida"}), 400
        existe_ubi = db.execute("SELECT id FROM ubicaciones WHERE id = ?", (ubicacion_id,)).fetchone()
        if not existe_ubi:
            return jsonify({"error": "La ubicación seleccionada no existe"}), 400
        # Nota: una ubicación puede tener más de un producto (es normal en
        # una bodega real), así que aquí no se valida "ocupación".

    # El nombre se autogenera (no existe como campo del formulario); se
    # calcula una vez que sabemos el id definitivo del producto.
    cur = db.execute(
        """
        INSERT INTO productos (
            codigo_interno, nombre, descripcion, categoria, marca, modelo,
            ubicacion_id, stock_disponible, stock_reservado, stock_minimo,
            costo_compra, creado_en, actualizado_en
        ) VALUES (?, '', ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            codigo_interno,
            (data.get("descripcion") or "").strip(),
            categoria,
            modelo,
            ubicacion_id,
            stock_disponible,
            stock_reservado,
            stock_minimo,
            costo_compra,
            ts,
            ts,
        ),
    )
    nuevo_id = cur.lastrowid
    nombre = nombre_automatico(categoria, modelo, codigo_interno, nuevo_id)
    db.execute("UPDATE productos SET nombre = ? WHERE id = ?", (nombre, nuevo_id))

    # Si se crea con stock inicial, se deja registrado como movimiento de entrada
    if stock_disponible > 0:
        db.execute(
            """
            INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, costo_unitario, stock_resultante)
            VALUES (?, ?, 'entrada', 'Ajuste de inventario', ?, 'Stock inicial al crear producto', ?, ?, ?)
            """,
            (nuevo_id, ts, stock_disponible, (data.get("usuario") or "Sistema"), costo_compra, stock_disponible),
        )

    db.commit()

    _asegurar_categoria(db, categoria)
    _asegurar_modelo(db, modelo)
    db.commit()

    nuevo = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (nuevo_id,)).fetchone()
    return jsonify(row_to_dict(nuevo)), 201


@app.route("/api/productos/<int:producto_id>", methods=["PUT"])
def editar_producto(producto_id):
    db = get_db()
    row = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}

    codigo_interno = data.get("codigo_interno", row["codigo_interno"])
    codigo_interno = (codigo_interno or "").strip()

    if codigo_interno:
        duplicado = db.execute(
            "SELECT id FROM productos WHERE codigo_interno = ? AND id != ?",
            (codigo_interno, producto_id),
        ).fetchone()
        if duplicado:
            return jsonify({"error": f"Ya existe otro producto con código '{codigo_interno}'"}), 400

    try:
        stock_minimo = int(data.get("stock_minimo", row["stock_minimo"]))
        costo_compra = float(data.get("costo_compra", row["costo_compra"]))
    except (TypeError, ValueError):
        return jsonify({"error": "Stock mínimo y costo deben ser números"}), 400

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

    categoria = (data.get("categoria") if data.get("categoria") is not None else row["categoria"]).strip()
    modelo = (data.get("modelo") if data.get("modelo") is not None else row["modelo"]).strip()

    if categoria or modelo:
        duplicado_pm = db.execute(
            "SELECT id FROM productos WHERE lower(trim(categoria)) = lower(trim(?)) AND lower(trim(modelo)) = lower(trim(?)) AND id != ?",
            (categoria, modelo, producto_id),
        ).fetchone()
        if duplicado_pm:
            return jsonify({
                "error": f"Ya existe otro producto con esa categoría y modelo (#{duplicado_pm[0]:04d})."
            }), 400

    nombre = nombre_automatico(categoria, modelo, codigo_interno, producto_id)

    # Nota: el stock disponible/reservado NO se edita aquí directamente.
    # Se ajusta siempre a través de /movimiento para dejar trazabilidad.
    db.execute(
        """
        UPDATE productos SET
            codigo_interno = ?, nombre = ?, descripcion = ?, categoria = ?,
            marca = '', modelo = ?, ubicacion_id = ?,
            stock_minimo = ?, costo_compra = ?, actualizado_en = ?
        WHERE id = ?
        """,
        (
            codigo_interno,
            nombre,
            (data.get("descripcion") if data.get("descripcion") is not None else row["descripcion"]).strip(),
            categoria,
            modelo,
            ubicacion_id,
            stock_minimo,
            costo_compra,
            now_iso(),
            producto_id,
        ),
    )
    db.commit()

    _asegurar_categoria(db, categoria)
    _asegurar_modelo(db, modelo)
    db.commit()

    actualizado = db.execute(PRODUCTO_SELECT + " WHERE p.id = ?", (producto_id,)).fetchone()
    return jsonify(row_to_dict(actualizado))


@app.route("/api/productos/<int:producto_id>", methods=["DELETE"])
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
        # Los movimientos se eliminan solos por ON DELETE CASCADE.

    db.execute("DELETE FROM productos WHERE id = ?", (producto_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Movimientos (ingreso/salida/ajuste de stock, con trazabilidad)
# ---------------------------------------------------------------------------

@app.route("/api/productos/<int:producto_id>/movimiento", methods=["POST"])
def registrar_movimiento(producto_id):
    db = get_db()
    row = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    tipo = (data.get("tipo") or "").strip().lower()
    motivo = (data.get("motivo") or "").strip()
    usuario = (data.get("usuario") or "").strip() or "Sin especificar"
    factura_numero = (data.get("factura_numero") or "").strip()

    if tipo not in ("entrada", "salida", "ajuste"):
        return jsonify({"error": "Tipo de movimiento inválido (usa entrada, salida o ajuste)"}), 400

    categoria_por_defecto = {"entrada": "Compra", "salida": "Consumo interno", "ajuste": "Ajuste de inventario"}
    categoria = (data.get("categoria") or "").strip() or categoria_por_defecto[tipo]
    if categoria not in CATEGORIAS_MOVIMIENTO:
        return jsonify({"error": f"Categoría inválida. Usa una de: {', '.join(CATEGORIAS_MOVIMIENTO)}"}), 400

    if factura_numero and tipo != "entrada":
        return jsonify({"error": "El número de factura solo aplica a movimientos de entrada"}), 400

    try:
        cantidad = int(data.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número entero"}), 400

    if tipo in ("entrada", "salida") and cantidad <= 0:
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
        if cantidad > stock_actual:
            return jsonify({"error": f"No hay stock suficiente (disponible: {stock_actual})"}), 400
        nuevo_stock = stock_actual - cantidad
        ts = now_iso()
        db.execute(
            "UPDATE productos SET stock_disponible = ?, actualizado_en = ? WHERE id = ?",
            (nuevo_stock, ts, producto_id),
        )
        db.execute(
            "INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, stock_resultante) "
            "VALUES (?, ?, 'salida', ?, ?, ?, ?, ?)",
            (producto_id, ts, categoria, cantidad, motivo, usuario, nuevo_stock),
        )
    else:  # ajuste -> cantidad es el nuevo stock disponible absoluto
        if cantidad < 0:
            return jsonify({"error": "El stock ajustado no puede ser negativo"}), 400
        nuevo_stock = cantidad
        cantidad = nuevo_stock - stock_actual  # delta real para el registro
        ts = now_iso()
        db.execute(
            "UPDATE productos SET stock_disponible = ?, actualizado_en = ? WHERE id = ?",
            (nuevo_stock, ts, producto_id),
        )
        db.execute(
            "INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, stock_resultante) "
            "VALUES (?, ?, 'ajuste', ?, ?, ?, ?, ?)",
            (producto_id, ts, categoria, cantidad, motivo, usuario, nuevo_stock),
        )

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
def listar_ubicaciones():
    db = get_db()
    ubis = db.execute(
        "SELECT * FROM ubicaciones ORDER BY pasillo, estanteria, nivel"
    ).fetchall()

    ubicaciones = []
    for u in ubis:
        productos = db.execute(
            "SELECT id, codigo_interno, nombre FROM productos WHERE ubicacion_id = ? ORDER BY nombre COLLATE NOCASE",
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


@app.route("/api/ubicaciones/<int:ubicacion_id>", methods=["DELETE"])
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
    nombre = (nombre or "").strip()
    if nombre:
        db.execute("INSERT OR IGNORE INTO categorias (nombre) VALUES (?)", (nombre,))


def _asegurar_modelo(db, nombre):
    nombre = (nombre or "").strip()
    if nombre:
        db.execute("INSERT OR IGNORE INTO modelos (nombre) VALUES (?)", (nombre,))


@app.route("/api/categorias", methods=["GET"])
def listar_categorias():
    db = get_db()
    rows = db.execute("SELECT nombre FROM categorias ORDER BY nombre COLLATE NOCASE").fetchall()
    return jsonify({"categorias": [r["nombre"] for r in rows]})


@app.route("/api/categorias", methods=["POST"])
def crear_categoria():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"error": "El nombre de la categoría no puede estar vacío"}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO categorias (nombre) VALUES (?)", (nombre,))
    db.commit()
    return jsonify({"nombre": nombre}), 201


@app.route("/api/modelos", methods=["GET"])
def listar_modelos():
    db = get_db()
    rows = db.execute("SELECT nombre FROM modelos ORDER BY nombre COLLATE NOCASE").fetchall()
    return jsonify({"modelos": [r["nombre"] for r in rows]})


@app.route("/api/modelos", methods=["POST"])
def crear_modelo():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"error": "El nombre del modelo no puede estar vacío"}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO modelos (nombre) VALUES (?)", (nombre,))
    db.commit()
    return jsonify({"nombre": nombre}), 201


# ---------------------------------------------------------------------------
# API: Proveedores (Módulo 4 - Ingreso de Compras)
# ---------------------------------------------------------------------------

@app.route("/api/proveedores", methods=["GET"])
def listar_proveedores():
    db = get_db()
    rows = db.execute(
        """
        SELECT pr.*, COUNT(f.id) AS cantidad_facturas
        FROM proveedores pr
        LEFT JOIN facturas f ON f.proveedor_id = pr.id
        GROUP BY pr.id
        ORDER BY pr.nombre COLLATE NOCASE
        """
    ).fetchall()
    return jsonify({"proveedores": [dict(r) for r in rows]})


@app.route("/api/proveedores", methods=["POST"])
def crear_proveedor():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"error": "El nombre del proveedor es obligatorio"}), 400

    db = get_db()
    existe = db.execute("SELECT id FROM proveedores WHERE nombre = ?", (nombre,)).fetchone()
    if existe:
        return jsonify({"error": f"Ya existe un proveedor con el nombre '{nombre}'"}), 400

    cur = db.execute(
        "INSERT INTO proveedores (nombre, rut, telefono, email, direccion, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (
            nombre,
            (data.get("rut") or "").strip(),
            (data.get("telefono") or "").strip(),
            (data.get("email") or "").strip(),
            (data.get("direccion") or "").strip(),
            now_iso(),
        ),
    )
    db.commit()
    nuevo = db.execute("SELECT *, 0 AS cantidad_facturas FROM proveedores WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@app.route("/api/proveedores/<int:proveedor_id>", methods=["PUT"])
def editar_proveedor(proveedor_id):
    db = get_db()
    row = db.execute("SELECT * FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or row["nombre"]).strip()
    if not nombre:
        return jsonify({"error": "El nombre del proveedor es obligatorio"}), 400

    duplicado = db.execute(
        "SELECT id FROM proveedores WHERE nombre = ? AND id != ?", (nombre, proveedor_id)
    ).fetchone()
    if duplicado:
        return jsonify({"error": f"Ya existe otro proveedor con el nombre '{nombre}'"}), 400

    db.execute(
        "UPDATE proveedores SET nombre = ?, rut = ?, telefono = ?, email = ?, direccion = ? WHERE id = ?",
        (
            nombre,
            (data.get("rut") if data.get("rut") is not None else row["rut"]).strip(),
            (data.get("telefono") if data.get("telefono") is not None else row["telefono"]).strip(),
            (data.get("email") if data.get("email") is not None else row["email"]).strip(),
            (data.get("direccion") if data.get("direccion") is not None else row["direccion"]).strip(),
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
def eliminar_proveedor(proveedor_id):
    db = get_db()
    row = db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Proveedor no encontrado"}), 404

    forzar = request.args.get("forzar") == "1"
    cant_facturas = db.execute(
        "SELECT COUNT(*) FROM facturas WHERE proveedor_id = ?", (proveedor_id,)
    ).fetchone()[0]

    if cant_facturas > 0 and not forzar:
        return jsonify({
            "error": f"Este proveedor tiene {cant_facturas} factura(s) asociada(s). "
                     f"Puedes eliminarlo de todas formas: las facturas se conservan, solo quedan sin proveedor asignado.",
            "requiere_forzar": True,
        }), 409

    if forzar:
        db.execute("UPDATE facturas SET proveedor_id = NULL WHERE proveedor_id = ?", (proveedor_id,))

    db.execute("DELETE FROM proveedores WHERE id = ?", (proveedor_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: Usuarios
# ---------------------------------------------------------------------------

@app.route("/api/usuarios", methods=["GET"])
def listar_usuarios():
    db = get_db()
    rows = db.execute("SELECT id, username, nombre, creado_en FROM usuarios ORDER BY username COLLATE NOCASE").fetchall()
    return jsonify({"usuarios": [dict(r) for r in rows]})


@app.route("/api/usuarios", methods=["POST"])
def crear_usuario():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    nombre = (data.get("nombre") or "").strip()

    if not username or not password:
        return jsonify({"error": "Usuario y contraseña son obligatorios"}), 400
    if len(password) < 4:
        return jsonify({"error": "La contraseña debe tener al menos 4 caracteres"}), 400

    db = get_db()
    existe = db.execute("SELECT id FROM usuarios WHERE username = ?", (username,)).fetchone()
    if existe:
        return jsonify({"error": f"Ya existe un usuario '{username}'"}), 400

    cur = db.execute(
        "INSERT INTO usuarios (username, password_hash, nombre, creado_en) VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), nombre, now_iso()),
    )
    db.commit()
    nuevo = db.execute("SELECT id, username, nombre, creado_en FROM usuarios WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@app.route("/api/usuarios/<int:usuario_id>", methods=["PUT"])
def editar_usuario(usuario_id):
    db = get_db()
    row = db.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Usuario no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nombre = data.get("nombre") if data.get("nombre") is not None else row["nombre"]
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
    return d


@app.route("/api/facturas", methods=["GET"])
def listar_facturas():
    db = get_db()
    rows = db.execute(FACTURA_SELECT + " ORDER BY f.creado_en DESC, f.id DESC").fetchall()
    return jsonify({"facturas": [_factura_resumen(db, r) for r in rows]})


@app.route("/api/facturas/<int:factura_id>", methods=["GET"])
def obtener_factura(factura_id):
    db = get_db()
    row = db.execute(FACTURA_SELECT + " WHERE f.id = ?", (factura_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Factura no encontrada"}), 404
    return jsonify(_factura_detalle(db, row))


@app.route("/api/facturas/numero/<path:numero>", methods=["GET"])
def obtener_factura_por_numero(numero):
    db = get_db()
    row = db.execute(FACTURA_SELECT + " WHERE f.numero = ?", (numero,)).fetchone()
    if row is None:
        return jsonify({"error": "No existe una factura con ese número"}), 404
    return jsonify(_factura_detalle(db, row))


@app.route("/api/facturas", methods=["POST"])
def crear_factura():
    data = request.get_json(force=True, silent=True) or {}
    numero = (data.get("numero") or "").strip()
    proveedor_id = data.get("proveedor_id") or None
    fecha = (data.get("fecha") or "").strip()
    lineas_in = data.get("lineas") or []
    aplicar_stock = data.get("aplicar_stock", True)
    usuario = (data.get("usuario") or "").strip() or "Sistema"

    if not numero:
        return jsonify({"error": "El número de factura es obligatorio"}), 400
    if not lineas_in:
        return jsonify({"error": "Agrega al menos un producto a la factura"}), 400

    db = get_db()

    if proveedor_id:
        try:
            proveedor_id = int(proveedor_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Proveedor inválido"}), 400
        existe_prov = db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone()
        if not existe_prov:
            return jsonify({"error": "El proveedor seleccionado no existe"}), 400

    existe = db.execute("SELECT id FROM facturas WHERE numero = ?", (numero,)).fetchone()
    if existe:
        return jsonify({"error": f"Ya existe una factura registrada con el número '{numero}'"}), 400

    lineas_validadas = []
    productos_vistos = set()
    for i, linea in enumerate(lineas_in, start=1):
        try:
            producto_id = int(linea.get("producto_id"))
            cantidad = int(linea.get("cantidad"))
            precio_unitario = float(linea.get("precio_unitario"))
        except (TypeError, ValueError):
            return jsonify({"error": f"Línea {i}: producto, cantidad y precio deben ser válidos"}), 400

        if cantidad <= 0:
            return jsonify({"error": f"Línea {i}: la cantidad debe ser mayor a 0"}), 400
        if precio_unitario < 0:
            return jsonify({"error": f"Línea {i}: el precio no puede ser negativo"}), 400

        producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if not producto:
            return jsonify({"error": f"Línea {i}: el producto seleccionado no existe"}), 400

        if producto_id in productos_vistos:
            return jsonify({"error": f"Línea {i}: ese producto ya está agregado en otra línea de esta misma factura"}), 400
        productos_vistos.add(producto_id)

        lineas_validadas.append((producto, cantidad, precio_unitario))

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO facturas (numero, proveedor_id, fecha, creado_en) VALUES (?, ?, ?, ?)",
        (numero, proveedor_id, fecha, ts),
    )
    factura_id = cur.lastrowid
    factura_row = {"id": factura_id, "numero": numero}

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
            )
            db.execute(
                "UPDATE factura_lineas SET aplicada = 1, aplicada_en = ? WHERE id = ?",
                (now_iso(), cur_linea.lastrowid),
            )

    db.commit()

    nueva = db.execute(FACTURA_SELECT + " WHERE f.id = ?", (factura_id,)).fetchone()
    return jsonify(_factura_detalle(db, nueva)), 201


@app.route("/api/facturas/<int:factura_id>", methods=["DELETE"])
def eliminar_factura(factura_id):
    db = get_db()
    factura = db.execute("SELECT * FROM facturas WHERE id = ?", (factura_id,)).fetchone()
    if factura is None:
        return jsonify({"error": "Factura no encontrada"}), 404

    forzar = request.args.get("forzar") == "1"
    data = request.get_json(force=True, silent=True) or {}
    usuario = (data.get("usuario") or "").strip() or "Sistema"

    lineas_aplicadas = db.execute(
        "SELECT * FROM factura_lineas WHERE factura_id = ? AND aplicada = 1", (factura_id,)
    ).fetchall()

    if lineas_aplicadas and not forzar:
        return jsonify({
            "error": f"{len(lineas_aplicadas)} producto(s) de esta factura ya fueron ingresados a stock. "
                     f"Si la eliminas de todas formas, se intentará revertir automáticamente ese stock.",
            "requiere_forzar": True,
        }), 409

    if forzar and lineas_aplicadas:
        # Antes de tocar nada: confirmar que TODAS las líneas aplicadas se
        # pueden revertir (que quede suficiente stock disponible). Si alguna
        # no se puede, se aborta completa para no dejar la reversión a medias.
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
            pendientes_de_revertir.append((producto, linea))

        ts = now_iso()
        for producto, linea in pendientes_de_revertir:
            nuevo_stock = producto["stock_disponible"] - linea["cantidad"]
            db.execute(
                "UPDATE productos SET stock_disponible = ?, actualizado_en = ? WHERE id = ?",
                (nuevo_stock, ts, producto["id"]),
            )
            db.execute(
                """
                INSERT INTO movimientos (
                    producto_id, fecha, tipo, cantidad, motivo, usuario,
                    costo_unitario, factura_id, factura_numero, stock_resultante
                ) VALUES (?, ?, 'salida', ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    producto["id"], ts, linea["cantidad"],
                    f"Reversión automática por eliminación de la factura '{factura['numero']}' (corrección de error)",
                    usuario, factura["numero"], nuevo_stock,
                ),
            )

    db.execute("DELETE FROM facturas WHERE id = ?", (factura_id,))
    db.commit()
    return jsonify({"ok": True, "revertido": bool(lineas_aplicadas)})


# ---------------------------------------------------------------------------
# API: Resumen (base para el futuro Dashboard)
# ---------------------------------------------------------------------------

@app.route("/api/resumen", methods=["GET"])
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


@app.route("/api/backup/descargar", methods=["GET"])
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
# Respaldo automático diario por correo
# ---------------------------------------------------------------------------

def _enviar_respaldo_por_correo():
    """Manda el archivo de base de datos como adjunto por correo.
    Devuelve (exito: bool, detalle: str)."""
    if not respaldo_automatico_configurado():
        return False, "El respaldo automático no está configurado (faltan las variables SMTP_USER / SMTP_PASSWORD)."
    if not os.path.exists(DB_PATH):
        return False, "Aún no existe base de datos que respaldar."

    try:
        fecha = datetime.now().strftime("%Y-%m-%d_%H%M")
        msg = EmailMessage()
        msg["Subject"] = f"Respaldo Inventario Taller — {fecha}"
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_DESTINATARIO
        msg.set_content(
            "Respaldo automático diario del sistema de inventario del taller.\n\n"
            f"Fecha: {fecha}\n"
            "Este correo se genera solo, no es necesario responderlo.\n"
            "Si algún día necesitas restaurar estos datos, guarda el archivo adjunto "
            "y súbelo desde el botón 'Respaldo' → 'Restaurar' dentro del sistema."
        )
        with open(DB_PATH, "rb") as f:
            contenido = f.read()
        msg.add_attachment(
            contenido, maintype="application", subtype="octet-stream",
            filename=f"inventario_backup_{fecha}.db",
        )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)

        return True, f"Enviado a {SMTP_DESTINATARIO}"
    except Exception as e:
        return False, f"Error al enviar: {e}"


def _registrar_respaldo_log(exito, detalle):
    try:
        db = sqlite3.connect(DB_PATH)
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
                db = sqlite3.connect(DB_PATH)
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
def backup_probar_automatico():
    exito, detalle = _enviar_respaldo_por_correo()
    _registrar_respaldo_log(exito, detalle)
    if not exito:
        return jsonify({"error": detalle}), 400
    return jsonify({"ok": True, "detalle": detalle})


# ---------------------------------------------------------------------------
# API: Reservas (Módulo 6 - Reserva de Productos)
# ---------------------------------------------------------------------------

RESERVA_SELECT = """
    SELECT r.*, p.nombre AS producto_nombre, p.codigo_interno AS producto_codigo_interno
    FROM reservas r
    JOIN productos p ON p.id = r.producto_id
"""


def _reserva_a_dict(row):
    d = dict(row)
    d["producto_id_sistema"] = f"{d['producto_id']:04d}"
    return d


@app.route("/api/reservas", methods=["GET"])
def listar_reservas():
    db = get_db()
    estado = request.args.get("estado", "").strip()
    producto_id = request.args.get("producto_id", "").strip()

    sql = RESERVA_SELECT + " WHERE 1=1"
    params = []
    if estado:
        sql += " AND r.estado = ?"
        params.append(estado)
    if producto_id:
        sql += " AND r.producto_id = ?"
        params.append(producto_id)
    sql += " ORDER BY r.creado_en DESC"

    rows = db.execute(sql, params).fetchall()
    return jsonify({"reservas": [_reserva_a_dict(r) for r in rows]})


@app.route("/api/productos/<int:producto_id>/reservar", methods=["POST"])
def reservar_stock(producto_id):
    db = get_db()
    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    try:
        cantidad = int(data.get("cantidad"))
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número entero"}), 400
    if cantidad <= 0:
        return jsonify({"error": "La cantidad debe ser mayor a 0"}), 400
    if cantidad > producto["stock_disponible"]:
        return jsonify({"error": f"No hay stock disponible suficiente (disponible: {producto['stock_disponible']})"}), 400

    orden_trabajo = (data.get("orden_trabajo") or "").strip()
    motivo = (data.get("motivo") or "").strip()
    usuario = (data.get("usuario") or "").strip() or "Sin especificar"
    ts = now_iso()

    db.execute(
        "UPDATE productos SET stock_disponible = stock_disponible - ?, stock_reservado = stock_reservado + ?, actualizado_en = ? WHERE id = ?",
        (cantidad, cantidad, ts, producto_id),
    )
    cur = db.execute(
        """
        INSERT INTO reservas (producto_id, cantidad, orden_trabajo, motivo, usuario, estado, creado_en, actualizado_en)
        VALUES (?, ?, ?, ?, ?, 'activa', ?, ?)
        """,
        (producto_id, cantidad, orden_trabajo, motivo, usuario, ts, ts),
    )
    db.commit()

    nueva = db.execute(RESERVA_SELECT + " WHERE r.id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_reserva_a_dict(nueva)), 201


@app.route("/api/reservas/<int:reserva_id>/liberar", methods=["POST"])
def liberar_reserva(reserva_id):
    db = get_db()
    reserva = db.execute("SELECT * FROM reservas WHERE id = ?", (reserva_id,)).fetchone()
    if reserva is None:
        return jsonify({"error": "Reserva no encontrada"}), 404
    if reserva["estado"] != "activa":
        return jsonify({"error": f"Esta reserva ya está '{reserva['estado']}', no se puede liberar de nuevo"}), 400

    ts = now_iso()
    db.execute(
        "UPDATE productos SET stock_disponible = stock_disponible + ?, stock_reservado = stock_reservado - ?, actualizado_en = ? WHERE id = ?",
        (reserva["cantidad"], reserva["cantidad"], ts, reserva["producto_id"]),
    )
    db.execute(
        "UPDATE reservas SET estado = 'liberada', actualizado_en = ? WHERE id = ?",
        (ts, reserva_id),
    )
    db.commit()

    actualizada = db.execute(RESERVA_SELECT + " WHERE r.id = ?", (reserva_id,)).fetchone()
    return jsonify(_reserva_a_dict(actualizada))


@app.route("/api/reservas/<int:reserva_id>/consumir", methods=["POST"])
def consumir_reserva(reserva_id):
    db = get_db()
    reserva = db.execute("SELECT * FROM reservas WHERE id = ?", (reserva_id,)).fetchone()
    if reserva is None:
        return jsonify({"error": "Reserva no encontrada"}), 404
    if reserva["estado"] != "activa":
        return jsonify({"error": f"Esta reserva ya está '{reserva['estado']}', no se puede consumir de nuevo"}), 400

    data = request.get_json(force=True, silent=True) or {}
    usuario = (data.get("usuario") or reserva["usuario"] or "Sin especificar").strip()

    producto = db.execute("SELECT * FROM productos WHERE id = ?", (reserva["producto_id"],)).fetchone()
    ts = now_iso()
    nuevo_reservado = producto["stock_reservado"] - reserva["cantidad"]

    db.execute(
        "UPDATE productos SET stock_reservado = ?, actualizado_en = ? WHERE id = ?",
        (nuevo_reservado, ts, producto["id"]),
    )
    motivo = f"Consumo de reserva" + (f" — OT {reserva['orden_trabajo']}" if reserva["orden_trabajo"] else "")
    nuevo_total = producto["stock_disponible"] + nuevo_reservado
    db.execute(
        """
        INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, stock_resultante)
        VALUES (?, ?, 'salida', 'Reserva', ?, ?, ?, ?)
        """,
        (producto["id"], ts, reserva["cantidad"], motivo, usuario, nuevo_total),
    )
    db.execute(
        "UPDATE reservas SET estado = 'consumida', actualizado_en = ? WHERE id = ?",
        (ts, reserva_id),
    )
    db.commit()

    actualizada = db.execute(RESERVA_SELECT + " WHERE r.id = ?", (reserva_id,)).fetchone()
    return jsonify(_reserva_a_dict(actualizada))


# ---------------------------------------------------------------------------
# API: Movimientos - bitácora completa (Módulo 5)
# ---------------------------------------------------------------------------

@app.route("/api/movimientos", methods=["GET"])
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
    if q:
        sql += " AND (m.motivo LIKE ? OR m.usuario LIKE ? OR p.nombre LIKE ? OR m.factura_numero LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like]

    sql += " ORDER BY m.fecha DESC, m.id DESC LIMIT 500"

    rows = db.execute(sql, params).fetchall()
    movimientos = []
    for r in rows:
        d = dict(r)
        d["producto_id_sistema"] = f"{r['producto_id']:04d}" if r["producto_id"] is not None else "----"
        d["producto_nombre"] = d["producto_nombre"] or "(producto eliminado)"
        movimientos.append(d)

    return jsonify({"movimientos": movimientos, "total": len(movimientos), "categorias": CATEGORIAS_MOVIMIENTO})


# ---------------------------------------------------------------------------
# API: Dashboard (Módulo 1)
# ---------------------------------------------------------------------------

@app.route("/api/dashboard", methods=["GET"])
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
        WHERE m.tipo = 'salida'
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

    return jsonify({
        "resumen": resumen_row,
        "stock_critico": stock_critico,
        "sin_movimiento": sin_movimiento,
        "mas_movidos": mas_movidos,
        "ultimos_movimientos": ultimos_movimientos,
        "compras_recientes": compras_recientes,
        "bodega": {
            "total_ubicaciones": total_ubicaciones,
            "ubicaciones_ocupadas": ubicaciones_ocupadas,
            "total_proveedores": total_proveedores,
            "facturas_pendientes": facturas_pendientes,
        },
    })


init_db()
iniciar_hilo_respaldos()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
