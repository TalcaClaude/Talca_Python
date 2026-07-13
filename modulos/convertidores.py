"""
Módulo Convertidores de Torque — tercer módulo top-level del ERP (mismo
patrón que Operativo/Finanzas, ver core.py). Antes vivía como una pestaña
más dentro de Operativo; se separó a su propio módulo (con su propia
página, su propio dashboard y sus propios reportes) para que quedara más
ordenado -- un convertidor de torque tiene su propio flujo de reparación
completo (con ítems/repuestos propios y un tablero kanban de estados) que
no tiene que ver con las órdenes de trabajo de un auto en sí, salvo por un
enganche puntual: una unidad ya reparada se puede vincular como ítem
dentro de la OT de un auto (ver orden_items.convertidor_id, que sigue
viviendo en modulos/operativo.py porque orden_items es una tabla de ese
módulo).

Tres orígenes posibles para un convertidor, confirmados con el taller:
  - 'cliente_directo': el cliente trae solo el convertidor a reparar (sin
    traer el auto completo) -- este es el único caso "externo" en el que
    aplica el flag es_regional (ver crear_convertidor).
  - 'stock_taller': el taller repara convertidores por su cuenta para
    tener stock listo, sin cliente asociado todavía.
  - 'desde_ot_auto': surge de la reparación de la caja automática de un
    auto que ya tiene su propia OT (el convertidor es una pieza de esa
    caja) — orden_origen_id queda apuntando a esa OT. Estos son
    "internos": al llegar a 'entregado' se cargan solos como costo en esa
    misma OT (ver _cargar_convertidor_interno_en_ot_origen).

Flujo de estados: ingreso -> diagnostico -> reparacion -> control_calidad
-> listo. Desde 'listo' hay dos salidas: se instala en la OT de un auto
(pasa automáticamente a 'instalado', ver agregar_convertidor_a_orden) o se
entrega directo al cliente sin instalar acá (pasa a 'entregado'
manualmente). 'cancelado' está disponible como salida manual desde
cualquier paso.

Este módulo solo importa piezas compartidas desde core.py, nunca desde
app.py ni desde otros modulos/ -- operativo.py es el que importa de acá
(para el enganche hacia orden_items), nunca al revés, así que no hay
riesgo de import circular.
"""

from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from core import (
    IVA_PORCENTAJE,
    _aplicar_entrada_stock,
    _aplicar_salida_stock,
    _autoancho,
    _crear_libro_excel,
    _estilo_encabezado,
    _excel_response,
    _excel_resumen,
    _iniciar_transaccion_stock,
    _rango_fechas,
    _reconstruir_tabla_legada_si_hace_falta,
    _reparar_fk_hacia_tabla_renombrada,
    get_db,
    now_iso,
    usuario_actual,
)

bp = Blueprint("convertidores", __name__)

TIPOS_ITEM = ["repuesto", "mano_obra"]

ORIGENES_CONVERTIDOR = ["cliente_directo", "stock_taller", "desde_ot_auto"]
ORIGENES_CONVERTIDOR_LABEL = {
    "cliente_directo": "Cliente trae el convertidor",
    "stock_taller": "Stock del taller",
    "desde_ot_auto": "Desde la caja de un auto (OT existente)",
}

ESTADOS_CONVERTIDOR = [
    "ingreso", "diagnostico", "reparacion", "control_calidad", "listo",
    "instalado", "entregado", "cancelado",
]
ESTADOS_CONVERTIDOR_CERRADOS = {"instalado", "entregado", "cancelado"}
# Único estado desde el que un convertidor se puede vincular como repuesto
# a la OT de un auto -- ver agregar_convertidor_a_orden(). Evita instalar
# una unidad cuya propia reparación todavía no terminó.
ESTADO_CONVERTIDOR_DISPONIBLE = "listo"
ESTADOS_CONVERTIDOR_LABEL = {
    "ingreso": "Ingreso", "diagnostico": "Diagnóstico", "reparacion": "Reparación",
    "control_calidad": "Control de calidad", "listo": "Listo para instalar",
    "instalado": "Instalado en un auto", "entregado": "Entregado", "cancelado": "Cancelado",
}


def _item_subtotal(item):
    """cantidad × costo_unitario de un ítem de convertidor_items, agregando
    el 19% si quedó marcado con aplica_iva -- mismo criterio que
    modulos/operativo.py:_item_subtotal (duplicado acá a propósito, para
    que este módulo no dependa de operativo.py; ver el comentario del
    encabezado sobre por qué no hay imports entre módulos hermanos)."""
    multiplicador = (1 + IVA_PORCENTAJE) if item.get("aplica_iva") else 1
    return round(item["cantidad"] * item["costo_unitario"] * multiplicador, 2)


def init_convertidores_db(db):
    """Se llama desde modulos/operativo.py:init_operativo_db(), ANTES del
    ALTER TABLE que agrega orden_items.convertidor_id (esa columna
    referencia convertidores_torque(id), así que esta tabla tiene que
    existir primero). Es un poco inusual que un módulo dependa de que
    otro llame su init en cierto orden, pero es preferible a que
    convertidores.py importe de operativo.py (eso sí generaría un ciclo,
    ya que operativo.py necesita importar convertidores.py para el
    enganche hacia orden_items)."""
    sql_convertidores_actual = """
        CREATE TABLE IF NOT EXISTS convertidores_torque (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origen_tipo TEXT NOT NULL DEFAULT 'stock_taller',
            cliente_id INTEGER REFERENCES clientes(id),
            equipo_id INTEGER REFERENCES equipos(id),
            orden_origen_id INTEGER REFERENCES ordenes_trabajo(id),
            marca TEXT DEFAULT '',
            modelo TEXT DEFAULT '',
            numero_serie TEXT DEFAULT '',
            motivo TEXT DEFAULT '',
            diagnostico TEXT DEFAULT '',
            responsable TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'ingreso',
            precio_venta REAL,
            es_regional INTEGER NOT NULL DEFAULT 0,
            fecha_recepcion TEXT,
            fecha_cierre TEXT,
            usuario TEXT DEFAULT '',
            creado_en TEXT,
            actualizado_en TEXT
        );
    """
    db.executescript(sql_convertidores_actual)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS convertidor_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            convertidor_id INTEGER NOT NULL REFERENCES convertidores_torque(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            producto_id INTEGER REFERENCES productos(id),
            descripcion TEXT DEFAULT '',
            cantidad REAL NOT NULL DEFAULT 1,
            costo_unitario REAL NOT NULL DEFAULT 0,
            movimiento_id INTEGER REFERENCES movimientos(id),
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            detalle TEXT DEFAULT '',
            aplica_iva INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    db.commit()

    # Si esta tabla alguna vez queda con un esquema legado (mismo mecanismo
    # preventivo que ya se usa para garantias/garantia_items en Operativo),
    # se reconstruye preservando los datos.
    _reconstruir_tabla_legada_si_hace_falta(
        db,
        "convertidores_torque",
        sql_convertidores_actual,
        [
            "id", "origen_tipo", "cliente_id", "equipo_id", "orden_origen_id",
            "marca", "modelo", "numero_serie", "motivo", "diagnostico",
            "responsable", "estado", "precio_venta", "es_regional", "fecha_recepcion",
            "fecha_cierre", "usuario", "creado_en", "actualizado_en",
        ],
    )
    # 'es_regional' se agregó después del primer despliegue del módulo --
    # si la tabla ya existía sin esta columna, se agrega acá en caliente.
    columnas_convertidor = [c[1] for c in db.execute("PRAGMA table_info(convertidores_torque)").fetchall()]
    if "es_regional" not in columnas_convertidor:
        db.execute("ALTER TABLE convertidores_torque ADD COLUMN es_regional INTEGER NOT NULL DEFAULT 0")
        db.commit()
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_convertidores_cliente_id ON convertidores_torque (cliente_id);
        CREATE INDEX IF NOT EXISTS idx_convertidores_equipo_id ON convertidores_torque (equipo_id);
        CREATE INDEX IF NOT EXISTS idx_convertidores_estado ON convertidores_torque (estado);
        CREATE INDEX IF NOT EXISTS idx_convertidor_items_convertidor_id ON convertidor_items (convertidor_id);
        """
    )
    db.commit()

    # Repara convertidor_items si su llave foránea quedó apuntando a una
    # tabla '_legado' (ver _reparar_fk_hacia_tabla_renombrada en core.py).
    _reparar_fk_hacia_tabla_renombrada(
        db,
        "convertidor_items",
        """
        CREATE TABLE IF NOT EXISTS convertidor_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            convertidor_id INTEGER NOT NULL REFERENCES convertidores_torque(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            producto_id INTEGER REFERENCES productos(id),
            descripcion TEXT DEFAULT '',
            cantidad REAL NOT NULL DEFAULT 1,
            costo_unitario REAL NOT NULL DEFAULT 0,
            movimiento_id INTEGER REFERENCES movimientos(id),
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            detalle TEXT DEFAULT '',
            aplica_iva INTEGER NOT NULL DEFAULT 0
        );
        """,
        [
            "id", "convertidor_id", "tipo", "producto_id", "descripcion",
            "cantidad", "costo_unitario", "movimiento_id", "usuario",
            "creado_en", "detalle", "aplica_iva",
        ],
    )
    db.commit()


# ---------------------------------------------------------------------------
# Página del módulo
# ---------------------------------------------------------------------------

@bp.route("/convertidores")
def vista_convertidores():
    return render_template("convertidores.html", modulo_activo="convertidores")


# ---------------------------------------------------------------------------
# Enganche hacia orden_items (OT de un auto) -- llamado desde
# modulos/operativo.py, nunca al revés.
# ---------------------------------------------------------------------------

def _convertidor_costo_total(db, convertidor_id):
    """Suma de los ítems propios del convertidor (lo que costó repararlo).
    Es el costo que se usa por defecto al instalarlo como repuesto en la
    OT de un auto, salvo que el convertidor tenga un 'precio_venta'
    explícito seteado."""
    fila = db.execute(
        f"""SELECT COALESCE(SUM(
                cantidad * costo_unitario *
                (CASE WHEN aplica_iva = 1 THEN 1 + {IVA_PORCENTAJE} ELSE 1 END)
            ), 0) AS total
            FROM convertidor_items WHERE convertidor_id = ?""",
        (convertidor_id,),
    ).fetchone()
    return round(fila["total"], 2)


def agregar_convertidor_a_orden(db, orden, orden_id, data):
    """Vincula una unidad específica de convertidor de torque (ya
    reparada, estado 'listo') como repuesto dentro de la OT de un auto.
    Llamada desde modulos/operativo.py:agregar_item_orden() cuando
    tipo == 'convertidor_torque'. A diferencia de un repuesto normal, NO
    descuenta stock de 'productos' -- el convertidor no vive en el
    inventario genérico, es su propia entidad rastreada en
    convertidores_torque. Lo que sí hace es marcar esa unidad como
    'instalado' para que no se pueda volver a usar en otra OT."""
    try:
        convertidor_id = int(data.get("convertidor_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Debes indicar qué convertidor de torque instalar"}), 400

    convertidor = db.execute(
        "SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)
    ).fetchone()
    if convertidor is None:
        return jsonify({"error": "El convertidor indicado no existe"}), 400
    if convertidor["estado"] != ESTADO_CONVERTIDOR_DISPONIBLE:
        return jsonify({
            "error": f"Este convertidor está en estado '{ESTADOS_CONVERTIDOR_LABEL.get(convertidor['estado'], convertidor['estado'])}', "
                     f"solo se puede instalar uno que esté '{ESTADOS_CONVERTIDOR_LABEL[ESTADO_CONVERTIDOR_DISPONIBLE]}'."
        }), 409

    usuario = usuario_actual()
    ts = now_iso()
    descripcion = (data.get("descripcion") or "").strip().upper()
    if not descripcion:
        partes = ["CONVERTIDOR DE TORQUE"]
        if convertidor["marca"]:
            partes.append(convertidor["marca"])
        if convertidor["modelo"]:
            partes.append(convertidor["modelo"])
        if convertidor["numero_serie"]:
            partes.append(f"N/S {convertidor['numero_serie']}")
        descripcion = " ".join(partes).upper()

    costo_unitario = convertidor["precio_venta"]
    if costo_unitario in (None, ""):
        costo_unitario = _convertidor_costo_total(db, convertidor_id)

    cur = db.execute(
        """
        INSERT INTO orden_items (
            orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva, convertidor_id
        ) VALUES (?, 'convertidor_torque', NULL, ?, 1, ?, NULL, ?, ?, '', 0, ?)
        """,
        (orden_id, descripcion, costo_unitario, usuario, ts, convertidor_id),
    )

    db.execute(
        """UPDATE convertidores_torque
           SET estado = 'instalado', actualizado_en = ?,
               orden_origen_id = COALESCE(orden_origen_id, ?)
           WHERE id = ?""",
        (ts, orden_id, convertidor_id),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))
    db.commit()

    nuevo_item = dict(db.execute("SELECT * FROM orden_items WHERE id = ?", (cur.lastrowid,)).fetchone())
    nuevo_item["subtotal"] = _item_subtotal(nuevo_item)
    return jsonify(nuevo_item), 201


def _cargar_convertidor_interno_en_ot_origen(db, convertidor_id):
    """Un convertidor 'interno' (origen_tipo == 'desde_ot_auto', salió de
    la caja de un auto que ya tenía su propia OT) no se instala a mano
    con el selector de repuestos como los de stock/cliente_directo -- ya
    pertenece a esa OT desde que se creó. Por eso, apenas termina SU
    PROPIA reparación (queda 'entregado'), se carga automáticamente como
    ítem de esa misma OT (orden_origen_id) con el costo de la reparación,
    para que sume como costo real de esa orden sin que alguien tenga que
    ir a agregarlo a mano.

    Idempotente: si ya hay un orden_items apuntando a este convertidor
    (ej. por un doble PUT a 'entregado'), no lo vuelve a agregar."""
    convertidor = db.execute(
        "SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)
    ).fetchone()
    if convertidor is None or not convertidor["orden_origen_id"]:
        return

    ya_cargado = db.execute(
        "SELECT id FROM orden_items WHERE convertidor_id = ?", (convertidor_id,)
    ).fetchone()
    if ya_cargado:
        return

    orden_id = convertidor["orden_origen_id"]
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return

    usuario = usuario_actual()
    ts = now_iso()
    partes = ["CONVERTIDOR DE TORQUE (REPARACIÓN INTERNA)"]
    if convertidor["marca"]:
        partes.append(convertidor["marca"])
    if convertidor["modelo"]:
        partes.append(convertidor["modelo"])
    if convertidor["numero_serie"]:
        partes.append(f"N/S {convertidor['numero_serie']}")
    descripcion = " ".join(partes).upper()

    costo_unitario = convertidor["precio_venta"]
    if costo_unitario in (None, ""):
        costo_unitario = _convertidor_costo_total(db, convertidor_id)

    db.execute(
        """
        INSERT INTO orden_items (
            orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva, convertidor_id
        ) VALUES (?, 'convertidor_torque', NULL, ?, 1, ?, NULL, ?, ?, '', 0, ?)
        """,
        (orden_id, descripcion, costo_unitario, usuario, ts, convertidor_id),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))
    db.commit()


def revertir_convertidor_al_quitar_item(db, convertidor_id):
    """Llamada desde modulos/operativo.py:eliminar_item_orden() cuando se
    quita de la OT un ítem tipo 'convertidor_torque'. No hay stock que
    devolver (el convertidor no vive en 'productos'), pero sí hay que
    liberar la unidad: vuelve a 'listo' para que se pueda instalar en
    otra OT si esto fue un error de carga. Solo aplica si estaba
    'instalado' (el caso 'interno' que se auto-cargó al llegar a
    'entregado' se queda en 'entregado' -- no tiene sentido "liberarlo"
    porque nunca fue instalable en otra OT distinta a la suya propia)."""
    db.execute(
        "UPDATE convertidores_torque SET estado = ?, actualizado_en = ? WHERE id = ? AND estado = 'instalado'",
        (ESTADO_CONVERTIDOR_DISPONIBLE, now_iso(), convertidor_id),
    )


# ---------------------------------------------------------------------------
# CRUD de convertidores
# ---------------------------------------------------------------------------

def _convertidor_dict(db, row):
    d = dict(row)
    cliente = None
    if d["cliente_id"]:
        cliente = db.execute("SELECT id, nombre, telefono FROM clientes WHERE id = ?", (d["cliente_id"],)).fetchone()
    d["cliente"] = dict(cliente) if cliente else None
    equipo = None
    if d["equipo_id"]:
        equipo = db.execute(
            "SELECT id, identificador, marca, modelo FROM equipos WHERE id = ?", (d["equipo_id"],)
        ).fetchone()
    d["equipo"] = dict(equipo) if equipo else None
    items = db.execute(
        "SELECT * FROM convertidor_items WHERE convertidor_id = ? ORDER BY creado_en ASC", (d["id"],)
    ).fetchall()
    d["items"] = [dict(i) for i in items]
    for it in d["items"]:
        it["subtotal"] = _item_subtotal(it)
    d["total_reparacion"] = sum(i["subtotal"] for i in d["items"])
    # Precio a mostrar en la tarjeta del tablero: el precio de venta
    # explícito si se cargó uno, si no, la suma de los ítems propios
    # (mismo criterio que agregar_convertidor_a_orden al instalarlo).
    d["costo_estimado"] = d["precio_venta"] if d["precio_venta"] not in (None, "") else d["total_reparacion"]
    # Ítem de orden_items (si ya está instalado) donde quedó registrado —
    # útil para que el detalle del convertidor muestre "instalado en OT #X".
    instalacion = db.execute(
        "SELECT orden_id FROM orden_items WHERE convertidor_id = ? ORDER BY id DESC LIMIT 1", (d["id"],)
    ).fetchone()
    d["instalado_en_orden_id"] = instalacion["orden_id"] if instalacion else None
    return d


@bp.route("/api/convertidores", methods=["POST"])
def crear_convertidor():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    origen_tipo = (data.get("origen_tipo") or "").strip().lower()
    if origen_tipo not in ORIGENES_CONVERTIDOR:
        return jsonify({"error": f"Origen inválido. Usa uno de: {', '.join(ORIGENES_CONVERTIDOR)}"}), 400

    cliente_id = None
    equipo_id = None
    orden_origen_id = None

    if origen_tipo == "cliente_directo":
        # El cliente trae solo la pieza -- exige cliente, el equipo es
        # opcional (puede que ni siquiera se sepa a qué auto pertenecía).
        try:
            cliente_id = int(data.get("cliente_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Debes indicar el cliente que trae el convertidor"}), 400
        if db.execute("SELECT id FROM clientes WHERE id = ?", (cliente_id,)).fetchone() is None:
            return jsonify({"error": "El cliente indicado no existe"}), 400
        if data.get("equipo_id") not in (None, ""):
            try:
                equipo_id = int(data.get("equipo_id"))
            except (TypeError, ValueError):
                return jsonify({"error": "Equipo inválido"}), 400
            if db.execute("SELECT id FROM equipos WHERE id = ? AND cliente_id = ?", (equipo_id, cliente_id)).fetchone() is None:
                return jsonify({"error": "Ese equipo no existe o no pertenece a este cliente"}), 400

    elif origen_tipo == "desde_ot_auto":
        # Surge de la caja de un auto que ya tiene su propia OT -- se
        # completan cliente_id/equipo_id automáticamente desde esa OT, no
        # hace falta que el usuario los repita a mano.
        try:
            orden_origen_id = int(data.get("orden_origen_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Debes indicar la OT del auto de la que sale este convertidor"}), 400
        orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_origen_id,)).fetchone()
        if orden is None:
            return jsonify({"error": "La OT indicada no existe"}), 400
        cliente_id = orden["cliente_id"]
        equipo_id = orden["equipo_id"]

    # 'stock_taller': sin cliente ni equipo, se repara por cuenta del taller.

    # "De región" solo tiene sentido cuando el cliente trae el convertidor
    # directo (origen externo) -- para stock_taller/desde_ot_auto no hay
    # ningún envío de vuelta involucrado, así que se ignora si viene en el
    # payload para esos casos (evita datos inconsistentes en la tarjeta).
    es_regional = 1 if (origen_tipo == "cliente_directo" and data.get("es_regional")) else 0

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO convertidores_torque (
            origen_tipo, cliente_id, equipo_id, orden_origen_id, marca, modelo,
            numero_serie, motivo, diagnostico, responsable, estado, precio_venta,
            es_regional, fecha_recepcion, usuario, creado_en, actualizado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingreso', ?, ?, ?, ?, ?, ?)
        """,
        (
            origen_tipo, cliente_id, equipo_id, orden_origen_id,
            (data.get("marca") or "").strip().upper(),
            (data.get("modelo") or "").strip().upper(),
            (data.get("numero_serie") or "").strip().upper(),
            (data.get("motivo") or "").strip().upper(),
            (data.get("diagnostico") or "").strip().upper(),
            (data.get("responsable") or "").strip().upper(),
            float(data["precio_venta"]) if data.get("precio_venta") not in (None, "") else None,
            es_regional, ts, usuario_actual(), ts, ts,
        ),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_convertidor_dict(db, nuevo)), 201


@bp.route("/api/convertidores", methods=["GET"])
def listar_convertidores():
    db = get_db()
    estado = (request.args.get("estado") or "").strip()
    sql = "SELECT * FROM convertidores_torque WHERE 1=1"
    params = []
    if estado:
        sql += " AND estado = ?"
        params.append(estado)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"convertidores": [_convertidor_dict(db, r) for r in rows], "total": len(rows)})


@bp.route("/api/convertidores/disponibles", methods=["GET"])
def convertidores_disponibles():
    """Para el selector de 'agregar repuesto' en la OT de un auto: solo
    las unidades cuya propia reparación ya terminó (estado 'listo')."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM convertidores_torque WHERE estado = ? ORDER BY actualizado_en DESC",
        (ESTADO_CONVERTIDOR_DISPONIBLE,),
    ).fetchall()
    resultado = []
    for r in rows:
        d = dict(r)
        d["costo_total"] = d["precio_venta"] if d["precio_venta"] not in (None, "") else _convertidor_costo_total(db, d["id"])
        resultado.append(d)
    return jsonify({"convertidores": resultado})


@bp.route("/api/convertidores/<int:convertidor_id>", methods=["GET"])
def obtener_convertidor(convertidor_id):
    db = get_db()
    row = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    return jsonify(_convertidor_dict(db, row))


@bp.route("/api/convertidores/<int:convertidor_id>", methods=["PUT"])
def editar_convertidor(convertidor_id):
    db = get_db()
    row = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    data = request.get_json(force=True, silent=True) or {}

    estado = row["estado"]
    if "estado" in data:
        estado = (data.get("estado") or "").strip().lower()
        if estado not in ESTADOS_CONVERTIDOR:
            return jsonify({"error": f"Estado inválido. Usa uno de: {', '.join(ESTADOS_CONVERTIDOR)}"}), 400
        # 'instalado' solo lo puede setear el sistema (al vincularlo como
        # repuesto en la OT de un auto, ver agregar_convertidor_a_orden)
        # -- no tiene sentido que alguien lo marque a mano sin que quede
        # vinculado a ningún ítem real.
        if estado == "instalado" and row["estado"] != "instalado":
            return jsonify({
                "error": "El estado 'instalado' se asigna automáticamente al agregar este convertidor "
                         "como repuesto en la OT de un auto -- no se puede setear a mano."
            }), 400

    precio_venta = row["precio_venta"]
    if "precio_venta" in data:
        precio_venta = float(data["precio_venta"]) if data.get("precio_venta") not in (None, "") else None

    # "De región" solo aplica al origen 'cliente_directo' (ver crear_convertidor)
    # -- el origen no se puede cambiar después de creado, así que si esta
    # unidad no es de ese origen, el flag se mantiene en 0 pase lo que
    # mande el payload.
    es_regional = row["es_regional"]
    if "es_regional" in data and row["origen_tipo"] == "cliente_directo":
        es_regional = 1 if data.get("es_regional") else 0

    fecha_cierre = row["fecha_cierre"]
    if estado in ESTADOS_CONVERTIDOR_CERRADOS and not fecha_cierre:
        fecha_cierre = now_iso()
    elif estado not in ESTADOS_CONVERTIDOR_CERRADOS:
        fecha_cierre = None

    # Un convertidor 'interno' (viene de la caja de un auto que ya tiene
    # su propia OT) se carga solo, como costo, en esa misma OT apenas
    # termina su propia reparación -- ver _cargar_convertidor_interno_en_ot_origen.
    # Se detecta ANTES del UPDATE (comparando contra el estado previo) para
    # no disparar el efecto secundario en cada guardado repetido, solo la
    # primera vez que entra a 'entregado'.
    disparar_carga_interna = (
        estado == "entregado"
        and row["estado"] != "entregado"
        and row["origen_tipo"] == "desde_ot_auto"
        and row["orden_origen_id"]
    )

    db.execute(
        """
        UPDATE convertidores_torque SET marca = ?, modelo = ?, numero_serie = ?,
            motivo = ?, diagnostico = ?, responsable = ?, estado = ?,
            precio_venta = ?, es_regional = ?, fecha_cierre = ?, actualizado_en = ?
        WHERE id = ?
        """,
        (
            (data.get("marca", row["marca"]) or "").strip().upper(),
            (data.get("modelo", row["modelo"]) or "").strip().upper(),
            (data.get("numero_serie", row["numero_serie"]) or "").strip().upper(),
            (data.get("motivo", row["motivo"]) or "").strip().upper(),
            (data.get("diagnostico", row["diagnostico"]) or "").strip().upper(),
            (data.get("responsable", row["responsable"]) or "").strip().upper(),
            estado, precio_venta, es_regional, fecha_cierre, now_iso(), convertidor_id,
        ),
    )
    db.commit()

    if disparar_carga_interna:
        _cargar_convertidor_interno_en_ot_origen(db, convertidor_id)

    actualizado = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    return jsonify(_convertidor_dict(db, actualizado))


@bp.route("/api/convertidores/<int:convertidor_id>", methods=["DELETE"])
def eliminar_convertidor(convertidor_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    convertidor = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    if convertidor is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    ya_en_ot = db.execute(
        "SELECT orden_id FROM orden_items WHERE convertidor_id = ? LIMIT 1", (convertidor_id,)
    ).fetchone()
    if convertidor["estado"] == "instalado" or ya_en_ot is not None:
        return jsonify({
            "error": "Este convertidor ya está cargado como ítem en la OT de un auto -- elimina primero ese "
                     "ítem desde la orden correspondiente si de verdad quieres deshacerlo."
        }), 409

    items_repuesto = db.execute(
        "SELECT * FROM convertidor_items WHERE convertidor_id = ? AND tipo = 'repuesto' AND producto_id IS NOT NULL",
        (convertidor_id,),
    ).fetchall()
    for item in items_repuesto:
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (item["producto_id"],)).fetchone()
        if producto is not None:
            _aplicar_entrada_stock(
                db, producto, int(round(item["cantidad"])), item["costo_unitario"],
                motivo=f"DEVOLUCIÓN AL ELIMINAR CONVERTIDOR #{convertidor_id}",
                usuario=usuario_actual(), factura=None, categoria="Devolución",
            )

    db.execute("DELETE FROM convertidores_torque WHERE id = ?", (convertidor_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/convertidores/<int:convertidor_id>/items", methods=["POST"])
def agregar_item_convertidor(convertidor_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    convertidor = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    if convertidor is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    if convertidor["estado"] in ESTADOS_CONVERTIDOR_CERRADOS:
        return jsonify({"error": f"El convertidor está '{convertidor['estado']}' y no admite más ítems."}), 409

    data = request.get_json(force=True, silent=True) or {}
    tipo = (data.get("tipo") or "").strip().lower()
    if tipo not in TIPOS_ITEM:
        return jsonify({"error": f"Tipo de ítem inválido. Usa uno de: {', '.join(TIPOS_ITEM)}"}), 400
    try:
        cantidad = float(data.get("cantidad"))
        if cantidad <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número mayor a 0"}), 400

    usuario = usuario_actual()
    ts = now_iso()
    producto_id = None
    movimiento_id = None
    descripcion = (data.get("descripcion") or "").strip().upper()

    if tipo == "repuesto":
        try:
            producto_id = int(data.get("producto_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Debes indicar el repuesto (producto) a usar"}), 400
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if producto is None:
            return jsonify({"error": "El producto indicado no existe"}), 400
        costo_unitario = producto["costo_compra"]
        if data.get("costo_unitario") not in (None, ""):
            try:
                costo_unitario = float(data.get("costo_unitario"))
            except (TypeError, ValueError):
                return jsonify({"error": "El costo unitario debe ser un número válido"}), 400
        cantidad_stock = int(round(cantidad))
        try:
            _aplicar_salida_stock(
                db, producto, cantidad_stock,
                motivo=f"CONVERTIDOR DE TORQUE #{convertidor_id}" + (f" - {descripcion}" if descripcion else ""),
                usuario=usuario, categoria="Consumo interno",
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        movimiento_id = db.execute(
            "SELECT id FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1", (producto_id,)
        ).fetchone()[0]
        if not descripcion:
            descripcion = producto["nombre"]
    detalle = ""
    aplica_iva = 0
    if tipo == "mano_obra":
        if not descripcion:
            return jsonify({"error": "Describe la mano de obra realizada"}), 400
        try:
            costo_unitario = float(data.get("costo_unitario") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "El costo unitario debe ser un número válido"}), 400
        detalle = (data.get("detalle") or "").strip().upper()
        aplica_iva = 1 if data.get("aplica_iva") else 0

    cur = db.execute(
        """
        INSERT INTO convertidor_items (
            convertidor_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (convertidor_id, tipo, producto_id, descripcion, cantidad, costo_unitario, movimiento_id, usuario, ts, detalle, aplica_iva),
    )
    db.execute("UPDATE convertidores_torque SET actualizado_en = ? WHERE id = ?", (ts, convertidor_id))
    db.commit()
    nuevo_item = dict(db.execute("SELECT * FROM convertidor_items WHERE id = ?", (cur.lastrowid,)).fetchone())
    nuevo_item["subtotal"] = _item_subtotal(nuevo_item)
    return jsonify(nuevo_item), 201


@bp.route("/api/convertidores/<int:convertidor_id>/items/<int:item_id>", methods=["DELETE"])
def eliminar_item_convertidor(convertidor_id, item_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    item = db.execute(
        "SELECT * FROM convertidor_items WHERE id = ? AND convertidor_id = ?", (item_id, convertidor_id)
    ).fetchone()
    if item is None:
        return jsonify({"error": "Ítem no encontrado en este convertidor"}), 404

    if item["tipo"] == "repuesto" and item["producto_id"]:
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (item["producto_id"],)).fetchone()
        if producto is not None:
            _aplicar_entrada_stock(
                db, producto, int(round(item["cantidad"])), item["costo_unitario"],
                motivo=f"DEVOLUCIÓN AL ELIMINAR ÍTEM DE CONVERTIDOR #{convertidor_id}",
                usuario=usuario_actual(), factura=None, categoria="Devolución",
            )

    db.execute("DELETE FROM convertidor_items WHERE id = ?", (item_id,))
    db.execute("UPDATE convertidores_torque SET actualizado_en = ? WHERE id = ?", (now_iso(), convertidor_id))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@bp.route("/api/convertidores/dashboard", methods=["GET"])
def dashboard_convertidores():
    """Resumen para la pestaña Dashboard del módulo: cuántos hay por
    estado, cuánto valor en reparación hay abierto, cuántos "de región"
    siguen pendientes de despachar de vuelta, y el tiempo promedio de
    reparación de los que se cerraron el mes en curso (mismo criterio de
    "mes en curso" que usa Operativo en su propio dashboard)."""
    db = get_db()
    rows = db.execute("SELECT * FROM convertidores_torque").fetchall()

    por_estado = {estado: 0 for estado in ESTADOS_CONVERTIDOR}
    por_origen = {origen: 0 for origen in ORIGENES_CONVERTIDOR}
    abiertos = 0
    valor_en_reparacion = 0.0
    regionales_pendientes = 0
    tiempos_cierre_mes = []
    mes_actual = datetime.now().strftime("%Y-%m")

    for r in rows:
        d = dict(r)
        por_estado[d["estado"]] = por_estado.get(d["estado"], 0) + 1
        por_origen[d["origen_tipo"]] = por_origen.get(d["origen_tipo"], 0) + 1
        if d["estado"] not in ESTADOS_CONVERTIDOR_CERRADOS:
            abiertos += 1
            costo = d["precio_venta"] if d["precio_venta"] not in (None, "") else _convertidor_costo_total(db, d["id"])
            valor_en_reparacion += costo or 0
        if d["es_regional"] and d["estado"] not in ("entregado", "cancelado"):
            regionales_pendientes += 1
        if d["estado"] in ESTADOS_CONVERTIDOR_CERRADOS and (d["fecha_cierre"] or "").startswith(mes_actual):
            if d["fecha_recepcion"] and d["fecha_cierre"]:
                try:
                    dt_ini = datetime.fromisoformat(d["fecha_recepcion"][:19])
                    dt_fin = datetime.fromisoformat(d["fecha_cierre"][:19])
                    tiempos_cierre_mes.append((dt_fin - dt_ini).total_seconds() / 86400)
                except ValueError:
                    pass

    tiempo_promedio_dias = round(sum(tiempos_cierre_mes) / len(tiempos_cierre_mes), 1) if tiempos_cierre_mes else None

    return jsonify({
        "total": len(rows),
        "abiertos": abiertos,
        "valor_en_reparacion": round(valor_en_reparacion, 2),
        "regionales_pendientes": regionales_pendientes,
        "tiempo_promedio_cierre_mes_dias": tiempo_promedio_dias,
        "por_estado": por_estado,
        "por_estado_label": ESTADOS_CONVERTIDOR_LABEL,
        "por_origen": por_origen,
        "por_origen_label": ORIGENES_CONVERTIDOR_LABEL,
    })


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------

@bp.route("/api/convertidores/reportes/resumen", methods=["GET"])
def reporte_resumen():
    """Contador rápido (mismo patrón que Operativo: preview antes de
    descargar) de cuántos convertidores caen en el rango de fechas
    elegido, filtrando por fecha de recepción."""
    desde, hasta = _rango_fechas()
    db = get_db()
    sql = "SELECT COUNT(*) AS n FROM convertidores_torque WHERE 1=1"
    params = []
    if desde:
        sql += " AND substr(fecha_recepcion, 1, 10) >= ?"
        params.append(desde)
    if hasta:
        sql += " AND substr(fecha_recepcion, 1, 10) <= ?"
        params.append(hasta)
    total = db.execute(sql, params).fetchone()["n"]
    return jsonify({"total": total})


@bp.route("/api/convertidores/reportes/excel", methods=["GET"])
def reporte_excel():
    """Excel con el detalle de convertidores del período y un resumen por
    origen -- para ver de un vistazo cuántas unidades vienen de stock,
    de clientes externos o de cajas de autos ya en el taller."""
    desde, hasta = _rango_fechas()
    db = get_db()
    sql = "SELECT * FROM convertidores_torque WHERE 1=1"
    params = []
    if desde:
        sql += " AND substr(fecha_recepcion, 1, 10) >= ?"
        params.append(desde)
    if hasta:
        sql += " AND substr(fecha_recepcion, 1, 10) <= ?"
        params.append(hasta)
    sql += " ORDER BY fecha_recepcion DESC"
    rows = db.execute(sql, params).fetchall()

    detalle = []
    conteo_origen = {}
    valor_total = 0
    for r in rows:
        c = _convertidor_dict(db, r)
        origen_txt = ORIGENES_CONVERTIDOR_LABEL.get(c["origen_tipo"], c["origen_tipo"])
        conteo_origen[origen_txt] = conteo_origen.get(origen_txt, 0) + 1
        valor_total += c["costo_estimado"] or 0
        detalle.append([
            (c["fecha_recepcion"] or "")[:10],
            c["cliente"]["nombre"] if c["cliente"] else "—",
            origen_txt,
            " ".join(filter(None, [c["marca"], c["modelo"]])) or "—",
            c["numero_serie"] or "—",
            ESTADOS_CONVERTIDOR_LABEL.get(c["estado"], c["estado"]),
            "Sí" if c["es_regional"] else "No",
            round(c["costo_estimado"] or 0, 2),
        ])

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Convertidores"
    rango_txt = f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"
    _excel_resumen(ws, "Convertidores de torque", [
        ("Rango", rango_txt),
        ("Convertidores", len(detalle)),
        ("Valor total (estimado)", f"${valor_total:,.0f}".replace(",", ".")),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["Fecha", "Cliente", "Origen", "Marca/Modelo", "N° de serie", "Estado", "De región", "Costo estimado"])
    for fila in detalle:
        ws.append(fila)
    ws.append([])
    ws.append(["", "", "", "", "", "", "TOTAL", round(valor_total, 2)])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    ws2 = wb.create_sheet("Resumen por origen")
    ws2.append(["Origen", "Cantidad"])
    for origen_txt, cantidad in sorted(conteo_origen.items(), key=lambda x: -x[1]):
        ws2.append([origen_txt, cantidad])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"convertidores{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)
