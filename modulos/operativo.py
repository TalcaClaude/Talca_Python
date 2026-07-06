"""
Módulo Operativo — órdenes de trabajo. Segundo módulo del ERP, construido
sobre el mismo patrón que Caja Chica (ver modulos/caja_chica.py y
core.py): un Flask Blueprint que solo importa piezas compartidas desde
core.py, nunca desde app.py, para no repetir el import circular que se
encontró al separar Caja Chica por primera vez.

Entidades:
  - clientes: quién trae el equipo/vehículo al taller.
  - equipos: lo que se repara, ligado a un cliente (puede tener varios
    equipos con el taller a lo largo del tiempo).
  - ordenes_trabajo: el ticket de reparación en sí, con un estado que va
    avanzando (recibido -> diagnostico -> reparacion -> esperando_repuesto
    -> listo -> entregado; o cancelado en cualquier punto).
  - orden_items: lo que se le cargó a una orden — repuestos (que consumen
    stock del inventario, igual que ya hace Caja Chica con su "compra de
    emergencia") y mano de obra (que no toca inventario).

La tabla de migración de este módulo (init_operativo_db) se llama desde
app.py:init_db(), después de que existan las tablas productos y
movimientos (los repuestos de una orden las referencian).
"""

from datetime import datetime

from flask import Blueprint, jsonify, request

from core import (
    _aplicar_entrada_stock,
    _aplicar_salida_stock,
    _autoancho,
    _crear_libro_excel,
    _estilo_encabezado,
    _excel_response,
    _iniciar_transaccion_stock,
    _pdf_documento,
    _pdf_response,
    _pdf_tabla,
    _rango_fechas,
    fmt_money_pdf,
    get_db,
    now_iso,
    usuario_actual,
)

bp = Blueprint("operativo", __name__)

ESTADOS_ORDEN = [
    "recibido", "diagnostico", "presupuesto", "esperando_aprobacion", "reparacion",
    "esperando_repuesto", "control_calidad", "listo", "entregado", "cancelado",
]
# Estados que cuentan como "la orden ya terminó" — no deberían poder seguir
# recibiendo repuestos o mano de obra nueva sin primero reabrirlas.
ESTADOS_CERRADOS = {"entregado", "cancelado"}

TIPOS_ITEM = ["repuesto", "mano_obra"]

# ---- Fase 3.5: constantes de los sub-flujos ampliados ----
PRIORIDADES_ORDEN = ["baja", "media", "alta", "urgente"]
TIPOS_PRESUPUESTO_ITEM = ["mano_obra", "repuesto", "insumo", "servicio_externo", "descuento"]
ESTADOS_PRESUPUESTO = ["pendiente", "enviado", "aprobado", "rechazado"]
EVENTOS_TIEMPO = ["inicio", "pausa", "reanudar", "termino"]
RESULTADOS_QC_ITEM = ["ok", "falla", "na"]  # resultado de cada punto del checklist
RESULTADOS_QC = ["pendiente", "aprobado", "rechazado"]  # veredicto general de la orden
ESTADOS_GARANTIA = ["abierta", "cerrada"]
IVA_PORCENTAJE = 0.19

# Campos "simples" (sin efectos secundarios) que se pueden actualizar desde
# el PUT general de la orden — recepción, diagnóstico, planificación y
# entrega. Los que sí disparan una transición automática de estado
# (presupuesto_estado, qc_resultado) viven en sus propios endpoints más
# abajo, para que ese efecto secundario quede explícito y sea auditable.
CAMPOS_SIMPLES_ORDEN = [
    "vin", "caja_automatica", "accesorios", "estado_visual",
    "sintoma", "diagnostico_tecnico", "falla_detectada", "componentes_danados",
    "mediciones", "resultado_pruebas", "responsable_diagnostico",
    "entrega_recibido_por", "entrega_documento", "entrega_observaciones",
]


def init_operativo_db(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            telefono TEXT DEFAULT '',
            email TEXT DEFAULT '',
            rut TEXT DEFAULT '',
            notas TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS equipos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
            identificador TEXT DEFAULT '',
            marca TEXT DEFAULT '',
            modelo TEXT DEFAULT '',
            notas TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ordenes_trabajo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER NOT NULL REFERENCES clientes(id),
            equipo_id INTEGER REFERENCES equipos(id),
            estado TEXT NOT NULL DEFAULT 'recibido',
            descripcion_problema TEXT DEFAULT '',
            diagnostico TEXT DEFAULT '',
            mecanico TEXT DEFAULT '',
            fecha_recepcion TEXT NOT NULL,
            fecha_entrega_estimada TEXT,
            fecha_entrega_real TEXT,
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            actualizado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orden_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            producto_id INTEGER REFERENCES productos(id),
            descripcion TEXT DEFAULT '',
            cantidad REAL NOT NULL DEFAULT 1,
            costo_unitario REAL NOT NULL DEFAULT 0,
            movimiento_id INTEGER REFERENCES movimientos(id),
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_equipos_cliente_id ON equipos (cliente_id);
        CREATE INDEX IF NOT EXISTS idx_ordenes_cliente_id ON ordenes_trabajo (cliente_id);
        CREATE INDEX IF NOT EXISTS idx_ordenes_equipo_id ON ordenes_trabajo (equipo_id);
        CREATE INDEX IF NOT EXISTS idx_ordenes_estado ON ordenes_trabajo (estado);
        CREATE INDEX IF NOT EXISTS idx_orden_items_orden_id ON orden_items (orden_id);
        CREATE INDEX IF NOT EXISTS idx_orden_items_producto_id ON orden_items (producto_id);
        """
    )
    db.commit()

    # -----------------------------------------------------------------
    # Migración Fase 3.5: recepción enriquecida, diagnóstico, presupuesto,
    # planificación/tiempos, control de calidad, garantía y entrega.
    # Todo aditivo (ALTER TABLE ADD COLUMN sobre la tabla ya existente +
    # CREATE TABLE IF NOT EXISTS), siguiendo el mismo patrón de migración
    # incremental que usa app.py:init_db() — no rompe datos existentes.
    # -----------------------------------------------------------------
    columnas_orden = [c[1] for c in db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
    columnas_nuevas_orden = {
        # Recepción (3.5.2)
        "vin": "TEXT DEFAULT ''",
        "kilometraje": "INTEGER",
        "anio": "INTEGER",
        "caja_automatica": "TEXT DEFAULT ''",
        "prioridad": "TEXT NOT NULL DEFAULT 'media'",
        "accesorios": "TEXT DEFAULT ''",
        "estado_visual": "TEXT DEFAULT ''",
        # Diagnóstico (3.5.4)
        "sintoma": "TEXT DEFAULT ''",
        "diagnostico_tecnico": "TEXT DEFAULT ''",
        "falla_detectada": "TEXT DEFAULT ''",
        "componentes_danados": "TEXT DEFAULT ''",
        "mediciones": "TEXT DEFAULT ''",
        "resultado_pruebas": "TEXT DEFAULT ''",
        "responsable_diagnostico": "TEXT DEFAULT ''",
        # Presupuesto (3.5.3)
        "presupuesto_estado": "TEXT NOT NULL DEFAULT 'pendiente'",
        "presupuesto_iva": "INTEGER NOT NULL DEFAULT 0",
        "presupuesto_notas": "TEXT DEFAULT ''",
        # Planificación / tiempos (3.5.5)
        "horas_presupuestadas": "REAL",
        # Control de calidad (3.5.6)
        "qc_resultado": "TEXT DEFAULT ''",
        "qc_inspector": "TEXT DEFAULT ''",
        "qc_observaciones": "TEXT DEFAULT ''",
        # Entrega (3.5.8)
        "entrega_recibido_por": "TEXT DEFAULT ''",
        "entrega_documento": "TEXT DEFAULT ''",
        "entrega_observaciones": "TEXT DEFAULT ''",
    }
    for columna, definicion in columnas_nuevas_orden.items():
        if columna not in columnas_orden:
            db.execute(f"ALTER TABLE ordenes_trabajo ADD COLUMN {columna} {definicion}")
    db.commit()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS presupuesto_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            cantidad REAL NOT NULL DEFAULT 1,
            precio_unitario REAL NOT NULL DEFAULT 0,
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orden_tiempos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            evento TEXT NOT NULL,
            marca_tiempo TEXT NOT NULL,
            usuario TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS qc_checklist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            item TEXT NOT NULL,
            resultado TEXT NOT NULL DEFAULT 'na',
            observacion TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS garantias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id),
            motivo TEXT DEFAULT '',
            diagnostico TEXT DEFAULT '',
            costo_asumido REAL NOT NULL DEFAULT 0,
            responsable TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'abierta',
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            cerrado_en TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_presupuesto_items_orden_id ON presupuesto_items (orden_id);
        CREATE INDEX IF NOT EXISTS idx_orden_tiempos_orden_id ON orden_tiempos (orden_id);
        CREATE INDEX IF NOT EXISTS idx_qc_checklist_items_orden_id ON qc_checklist_items (orden_id);
        CREATE INDEX IF NOT EXISTS idx_garantias_orden_id ON garantias (orden_id);
        """
    )
    db.commit()


# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------

def _cliente_dict(db, row):
    d = dict(row)
    d["n_equipos"] = db.execute(
        "SELECT COUNT(*) FROM equipos WHERE cliente_id = ?", (row["id"],)
    ).fetchone()[0]
    return d


@bp.route("/api/operativo/clientes", methods=["GET"])
def listar_clientes():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    sql = "SELECT * FROM clientes"
    params = []
    if q:
        sql += " WHERE nombre LIKE ? OR rut LIKE ? OR telefono LIKE ?"
        comodin = f"%{q}%"
        params = [comodin, comodin, comodin]
    sql += " ORDER BY nombre COLLATE NOCASE"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"clientes": [_cliente_dict(db, r) for r in rows], "total": len(rows)})


@bp.route("/api/operativo/clientes/<int:cliente_id>", methods=["GET"])
def obtener_cliente(cliente_id):
    db = get_db()
    row = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Cliente no encontrado"}), 404
    d = _cliente_dict(db, row)
    d["equipos"] = [dict(e) for e in db.execute(
        "SELECT * FROM equipos WHERE cliente_id = ? ORDER BY creado_en DESC", (cliente_id,)
    ).fetchall()]
    return jsonify(d)


@bp.route("/api/operativo/clientes", methods=["POST"])
def crear_cliente():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"error": "El nombre del cliente es obligatorio"}), 400

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO clientes (nombre, telefono, email, rut, notas, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (
            nombre,
            (data.get("telefono") or "").strip(),
            (data.get("email") or "").strip(),
            (data.get("rut") or "").strip(),
            (data.get("notas") or "").strip(),
            ts,
        ),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM clientes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_cliente_dict(db, nuevo)), 201


@bp.route("/api/operativo/clientes/<int:cliente_id>", methods=["PUT"])
def editar_cliente(cliente_id):
    db = get_db()
    row = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Cliente no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or row["nombre"]).strip()
    if not nombre:
        return jsonify({"error": "El nombre del cliente es obligatorio"}), 400

    db.execute(
        "UPDATE clientes SET nombre = ?, telefono = ?, email = ?, rut = ?, notas = ? WHERE id = ?",
        (
            nombre,
            (data.get("telefono", row["telefono"]) or "").strip(),
            (data.get("email", row["email"]) or "").strip(),
            (data.get("rut", row["rut"]) or "").strip(),
            (data.get("notas", row["notas"]) or "").strip(),
            cliente_id,
        ),
    )
    db.commit()
    actualizado = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    return jsonify(_cliente_dict(db, actualizado))


@bp.route("/api/operativo/clientes/<int:cliente_id>", methods=["DELETE"])
def eliminar_cliente(cliente_id):
    db = get_db()
    row = db.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Cliente no encontrado"}), 404

    n_ordenes = db.execute("SELECT COUNT(*) FROM ordenes_trabajo WHERE cliente_id = ?", (cliente_id,)).fetchone()[0]
    if n_ordenes:
        return jsonify({
            "error": f"Este cliente tiene {n_ordenes} orden(es) de trabajo asociada(s) y no se puede eliminar. "
                     f"Elimina o reasigna esas órdenes primero."
        }), 409

    db.execute("DELETE FROM clientes WHERE id = ?", (cliente_id,))  # el ON DELETE CASCADE se lleva sus equipos
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Equipos
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/equipos", methods=["GET"])
def listar_equipos():
    db = get_db()
    cliente_id = request.args.get("cliente_id")
    sql = "SELECT * FROM equipos"
    params = []
    if cliente_id:
        sql += " WHERE cliente_id = ?"
        params.append(cliente_id)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"equipos": [dict(r) for r in rows], "total": len(rows)})


@bp.route("/api/operativo/equipos", methods=["POST"])
def crear_equipo():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    try:
        cliente_id = int(data.get("cliente_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Debes indicar un cliente válido"}), 400
    cliente = db.execute("SELECT id FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if cliente is None:
        return jsonify({"error": "El cliente indicado no existe"}), 400

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO equipos (cliente_id, identificador, marca, modelo, notas, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (
            cliente_id,
            (data.get("identificador") or "").strip(),
            (data.get("marca") or "").strip(),
            (data.get("modelo") or "").strip(),
            (data.get("notas") or "").strip(),
            ts,
        ),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM equipos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@bp.route("/api/operativo/equipos/<int:equipo_id>", methods=["PUT"])
def editar_equipo(equipo_id):
    db = get_db()
    row = db.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Equipo no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    db.execute(
        "UPDATE equipos SET identificador = ?, marca = ?, modelo = ?, notas = ? WHERE id = ?",
        (
            (data.get("identificador", row["identificador"]) or "").strip(),
            (data.get("marca", row["marca"]) or "").strip(),
            (data.get("modelo", row["modelo"]) or "").strip(),
            (data.get("notas", row["notas"]) or "").strip(),
            equipo_id,
        ),
    )
    db.commit()
    actualizado = db.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    return jsonify(dict(actualizado))


@bp.route("/api/operativo/equipos/<int:equipo_id>", methods=["DELETE"])
def eliminar_equipo(equipo_id):
    db = get_db()
    row = db.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Equipo no encontrado"}), 404

    n_ordenes = db.execute("SELECT COUNT(*) FROM ordenes_trabajo WHERE equipo_id = ?", (equipo_id,)).fetchone()[0]
    if n_ordenes:
        return jsonify({
            "error": f"Este equipo tiene {n_ordenes} orden(es) de trabajo asociada(s) y no se puede eliminar."
        }), 409

    db.execute("DELETE FROM equipos WHERE id = ?", (equipo_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Órdenes de trabajo
# ---------------------------------------------------------------------------

def _orden_dict(db, row):
    d = dict(row)
    cliente = db.execute("SELECT id, nombre, telefono FROM clientes WHERE id = ?", (d["cliente_id"],)).fetchone()
    d["cliente"] = dict(cliente) if cliente else None
    equipo = None
    if d["equipo_id"]:
        equipo = db.execute("SELECT id, identificador, marca, modelo FROM equipos WHERE id = ?", (d["equipo_id"],)).fetchone()
    d["equipo"] = dict(equipo) if equipo else None
    items = db.execute("SELECT * FROM orden_items WHERE orden_id = ? ORDER BY creado_en ASC", (d["id"],)).fetchall()
    d["items"] = [dict(i) for i in items]
    d["total"] = sum(i["cantidad"] * i["costo_unitario"] for i in items)
    return d


@bp.route("/api/operativo/dashboard", methods=["GET"])
def dashboard_operativo():
    """KPIs para el dashboard del módulo Operativo:
      - ots_abiertas: órdenes cuyo estado no es un estado cerrado (entregado/cancelado).
      - ots_cerradas_mes: órdenes entregadas cuya fecha_entrega_real cae en el mes calendario actual.
      - monto_cerradas_mes: suma de (cantidad * costo_unitario) de los items de esas órdenes cerradas este mes.
      - monto_por_cerrar: suma de (cantidad * costo_unitario) de los items de todas las órdenes abiertas.
    """
    db = get_db()
    hoy = datetime.now()
    prefijo_mes = f"{hoy.year:04d}-{hoy.month:02d}"

    placeholders_cerrados = ", ".join("?" for _ in ESTADOS_CERRADOS)

    abiertas = db.execute(
        f"SELECT id FROM ordenes_trabajo WHERE estado NOT IN ({placeholders_cerrados})",
        list(ESTADOS_CERRADOS),
    ).fetchall()
    ids_abiertas = [r["id"] for r in abiertas]

    cerradas_mes = db.execute(
        "SELECT id FROM ordenes_trabajo WHERE estado = 'entregado' AND substr(fecha_entrega_real, 1, 7) = ?",
        (prefijo_mes,),
    ).fetchall()
    ids_cerradas_mes = [r["id"] for r in cerradas_mes]

    def _suma_items(ids):
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        fila = db.execute(
            f"SELECT COALESCE(SUM(cantidad * costo_unitario), 0) AS total FROM orden_items WHERE orden_id IN ({placeholders})",
            ids,
        ).fetchone()
        return fila["total"] or 0

    return jsonify({
        "ots_abiertas": len(ids_abiertas),
        "ots_cerradas_mes": len(ids_cerradas_mes),
        "monto_cerradas_mes": _suma_items(ids_cerradas_mes),
        "monto_por_cerrar": _suma_items(ids_abiertas),
    })


@bp.route("/api/operativo/ordenes", methods=["GET"])
def listar_ordenes():
    db = get_db()
    estado = (request.args.get("estado") or "").strip()
    cliente_id = request.args.get("cliente_id")
    sql = "SELECT * FROM ordenes_trabajo WHERE 1=1"
    params = []
    if estado:
        if estado not in ESTADOS_ORDEN:
            return jsonify({"error": f"Estado inválido. Usa uno de: {', '.join(ESTADOS_ORDEN)}"}), 400
        sql += " AND estado = ?"
        params.append(estado)
    if cliente_id:
        sql += " AND cliente_id = ?"
        params.append(cliente_id)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"ordenes": [_orden_dict(db, r) for r in rows], "total": len(rows)})


def _orden_dict_detalle(db, row):
    """Versión completa de la orden para la vista de detalle: agrega
    presupuesto, tiempos, control de calidad y garantías. Deliberadamente
    NO se usa en listar_ordenes/dashboard (eso reintroduciría el problema
    N+1 que ya se corrigió antes en este proyecto) — solo en obtener_orden
    y en las acciones (presupuesto/qc/entregar) que devuelven la orden
    completa después de un cambio."""
    d = _orden_dict(db, row)
    d["presupuesto"] = _presupuesto_dict(db, row["id"], row["presupuesto_iva"])
    tiempos = db.execute(
        "SELECT * FROM orden_tiempos WHERE orden_id = ? ORDER BY marca_tiempo ASC", (row["id"],)
    ).fetchall()
    d["tiempos"] = [dict(t) for t in tiempos]
    d["horas_reales"] = _calcular_horas_reales(tiempos)
    d["qc_checklist"] = [dict(i) for i in db.execute(
        "SELECT * FROM qc_checklist_items WHERE orden_id = ? ORDER BY creado_en ASC", (row["id"],)
    ).fetchall()]
    d["garantias"] = [dict(g) for g in db.execute(
        "SELECT * FROM garantias WHERE orden_id = ? ORDER BY creado_en DESC", (row["id"],)
    ).fetchall()]
    return d


@bp.route("/api/operativo/ordenes/<int:orden_id>", methods=["GET"])
def obtener_orden(orden_id):
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    return jsonify(_orden_dict_detalle(db, row))


@bp.route("/api/operativo/ordenes", methods=["POST"])
def crear_orden():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    try:
        cliente_id = int(data.get("cliente_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Debes indicar un cliente válido"}), 400
    cliente = db.execute("SELECT id FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
    if cliente is None:
        return jsonify({"error": "El cliente indicado no existe"}), 400

    equipo_id = None
    if data.get("equipo_id") not in (None, ""):
        try:
            equipo_id = int(data.get("equipo_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Equipo inválido"}), 400
        equipo = db.execute("SELECT id FROM equipos WHERE id = ? AND cliente_id = ?", (equipo_id, cliente_id)).fetchone()
        if equipo is None:
            return jsonify({"error": "Ese equipo no existe o no pertenece a este cliente"}), 400

    prioridad = (data.get("prioridad") or "media").strip().lower()
    if prioridad not in PRIORIDADES_ORDEN:
        return jsonify({"error": f"Prioridad inválida. Usa una de: {', '.join(PRIORIDADES_ORDEN)}"}), 400

    kilometraje = None
    if data.get("kilometraje") not in (None, ""):
        try:
            kilometraje = int(data.get("kilometraje"))
        except (TypeError, ValueError):
            return jsonify({"error": "El kilometraje debe ser un número entero"}), 400

    anio = None
    if data.get("anio") not in (None, ""):
        try:
            anio = int(data.get("anio"))
        except (TypeError, ValueError):
            return jsonify({"error": "El año debe ser un número entero"}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO ordenes_trabajo (
            cliente_id, equipo_id, estado, descripcion_problema, mecanico,
            fecha_recepcion, fecha_entrega_estimada, usuario, creado_en, actualizado_en,
            vin, kilometraje, anio, caja_automatica, prioridad, accesorios, estado_visual
        ) VALUES (?, ?, 'recibido', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cliente_id, equipo_id,
            (data.get("descripcion_problema") or "").strip(),
            (data.get("mecanico") or "").strip(),
            ts,
            (data.get("fecha_entrega_estimada") or "").strip() or None,
            usuario_actual(), ts, ts,
            (data.get("vin") or "").strip(),
            kilometraje,
            anio,
            (data.get("caja_automatica") or "").strip(),
            prioridad,
            (data.get("accesorios") or "").strip(),
            (data.get("estado_visual") or "").strip(),
        ),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_orden_dict(db, nueva)), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>", methods=["PUT"])
def editar_orden(orden_id):
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    data = request.get_json(force=True, silent=True) or {}

    estado = row["estado"]
    if "estado" in data:
        estado = (data.get("estado") or "").strip()
        if estado not in ESTADOS_ORDEN:
            return jsonify({"error": f"Estado inválido. Usa uno de: {', '.join(ESTADOS_ORDEN)}"}), 400

    fecha_entrega_real = row["fecha_entrega_real"]
    if estado == "entregado" and not fecha_entrega_real:
        # Se completa sola al pasar a "entregado" por primera vez, salvo que
        # el cliente mande una fecha explícita (ej. una entrega retroactiva).
        fecha_entrega_real = (data.get("fecha_entrega_real") or "").strip() or now_iso()
    elif "fecha_entrega_real" in data:
        fecha_entrega_real = (data.get("fecha_entrega_real") or "").strip() or None

    prioridad = row["prioridad"]
    if "prioridad" in data:
        prioridad = (data.get("prioridad") or "media").strip().lower()
        if prioridad not in PRIORIDADES_ORDEN:
            return jsonify({"error": f"Prioridad inválida. Usa una de: {', '.join(PRIORIDADES_ORDEN)}"}), 400

    kilometraje = row["kilometraje"]
    if "kilometraje" in data:
        if data.get("kilometraje") in (None, ""):
            kilometraje = None
        else:
            try:
                kilometraje = int(data.get("kilometraje"))
            except (TypeError, ValueError):
                return jsonify({"error": "El kilometraje debe ser un número entero"}), 400

    anio = row["anio"]
    if "anio" in data:
        if data.get("anio") in (None, ""):
            anio = None
        else:
            try:
                anio = int(data.get("anio"))
            except (TypeError, ValueError):
                return jsonify({"error": "El año debe ser un número entero"}), 400

    horas_presupuestadas = row["horas_presupuestadas"]
    if "horas_presupuestadas" in data:
        if data.get("horas_presupuestadas") in (None, ""):
            horas_presupuestadas = None
        else:
            try:
                horas_presupuestadas = float(data.get("horas_presupuestadas"))
            except (TypeError, ValueError):
                return jsonify({"error": "Las horas presupuestadas deben ser un número"}), 400

    # Campos de texto simples (recepción, diagnóstico, entrega): mismo
    # patrón de "usa lo nuevo si viene, si no deja lo que ya había".
    valores_simples = {
        campo: (data.get(campo, row[campo]) or "").strip() for campo in CAMPOS_SIMPLES_ORDEN
    }

    db.execute(
        """
        UPDATE ordenes_trabajo SET
            estado = ?, descripcion_problema = ?, diagnostico = ?, mecanico = ?,
            fecha_entrega_estimada = ?, fecha_entrega_real = ?, actualizado_en = ?,
            prioridad = ?, kilometraje = ?, anio = ?, horas_presupuestadas = ?,
            vin = ?, caja_automatica = ?, accesorios = ?, estado_visual = ?,
            sintoma = ?, diagnostico_tecnico = ?, falla_detectada = ?, componentes_danados = ?,
            mediciones = ?, resultado_pruebas = ?, responsable_diagnostico = ?,
            entrega_recibido_por = ?, entrega_documento = ?, entrega_observaciones = ?
        WHERE id = ?
        """,
        (
            estado,
            (data.get("descripcion_problema", row["descripcion_problema"]) or "").strip(),
            (data.get("diagnostico", row["diagnostico"]) or "").strip(),
            (data.get("mecanico", row["mecanico"]) or "").strip(),
            (data.get("fecha_entrega_estimada", row["fecha_entrega_estimada"]) or "").strip() or None,
            fecha_entrega_real,
            now_iso(),
            prioridad, kilometraje, anio, horas_presupuestadas,
            valores_simples["vin"], valores_simples["caja_automatica"],
            valores_simples["accesorios"], valores_simples["estado_visual"],
            valores_simples["sintoma"], valores_simples["diagnostico_tecnico"],
            valores_simples["falla_detectada"], valores_simples["componentes_danados"],
            valores_simples["mediciones"], valores_simples["resultado_pruebas"],
            valores_simples["responsable_diagnostico"],
            valores_simples["entrega_recibido_por"], valores_simples["entrega_documento"],
            valores_simples["entrega_observaciones"],
            orden_id,
        ),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return jsonify(_orden_dict(db, actualizada))


# ---------------------------------------------------------------------------
# Ítems de la orden (repuestos y mano de obra)
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/ordenes/<int:orden_id>/items", methods=["POST"])
def agregar_item_orden(orden_id):
    db = get_db()
    _iniciar_transaccion_stock(db)  # puede consumir stock si el ítem es un repuesto
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    if orden["estado"] in ESTADOS_CERRADOS:
        return jsonify({"error": f"La orden está '{orden['estado']}' y no admite más ítems. Reábrela primero cambiando su estado."}), 409

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
    descripcion = (data.get("descripcion") or "").strip()

    if tipo == "repuesto":
        try:
            producto_id = int(data.get("producto_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Debes indicar el repuesto (producto) a usar"}), 400
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if producto is None:
            return jsonify({"error": "El producto indicado no existe"}), 400

        # El costo del repuesto es el Costo Promedio Ponderado actual del
        # producto (lo que efectivamente cuesta reponerlo), salvo que se
        # informe uno distinto explícitamente.
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
                motivo=f"ORDEN DE TRABAJO #{orden_id}" + (f" - {descripcion}" if descripcion else ""),
                usuario=usuario, categoria="Consumo interno",
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        movimiento_id = db.execute(
            "SELECT id FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1", (producto_id,)
        ).fetchone()[0]
        if not descripcion:
            descripcion = producto["nombre"]
    else:  # mano_obra
        if not descripcion:
            return jsonify({"error": "Describe la mano de obra realizada"}), 400
        try:
            costo_unitario = float(data.get("costo_unitario") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "El costo unitario debe ser un número válido"}), 400

    cur = db.execute(
        """
        INSERT INTO orden_items (
            orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario, movimiento_id, usuario, ts),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))
    db.commit()

    nuevo_item = db.execute("SELECT * FROM orden_items WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo_item)), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>/items/<int:item_id>", methods=["DELETE"])
def eliminar_item_orden(orden_id, item_id):
    db = get_db()
    _iniciar_transaccion_stock(db)  # puede devolver stock si el ítem era un repuesto
    item = db.execute("SELECT * FROM orden_items WHERE id = ? AND orden_id = ?", (item_id, orden_id)).fetchone()
    if item is None:
        return jsonify({"error": "Ítem no encontrado en esta orden"}), 404

    if item["tipo"] == "repuesto" and item["producto_id"]:
        # Se devuelve el stock consumido, igual que una devolución normal
        # de inventario — usa _aplicar_entrada_stock para que el Costo
        # Promedio Ponderado se recalcule de forma consistente con
        # cualquier otra entrada del sistema.
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (item["producto_id"],)).fetchone()
        if producto is not None:
            _aplicar_entrada_stock(
                db, producto, int(round(item["cantidad"])), item["costo_unitario"],
                motivo=f"DEVOLUCIÓN AL ELIMINAR ÍTEM DE ORDEN #{orden_id}",
                usuario=usuario_actual(), factura=None, categoria="Devolución",
            )

    db.execute("DELETE FROM orden_items WHERE id = ?", (item_id,))
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (now_iso(), orden_id))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Fase 3.5.1 — Historial de vehículo
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/equipos/<int:equipo_id>/historial", methods=["GET"])
def historial_equipo(equipo_id):
    db = get_db()
    equipo = db.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    if equipo is None:
        return jsonify({"error": "Equipo no encontrado"}), 404
    cliente = db.execute("SELECT id, nombre FROM clientes WHERE id = ?", (equipo["cliente_id"],)).fetchone()
    ordenes = db.execute(
        "SELECT * FROM ordenes_trabajo WHERE equipo_id = ? ORDER BY fecha_recepcion DESC", (equipo_id,)
    ).fetchall()
    return jsonify({
        "equipo": dict(equipo),
        "cliente": dict(cliente) if cliente else None,
        "ordenes": [_orden_dict(db, r) for r in ordenes],
    })


# ---------------------------------------------------------------------------
# Fase 3.5.3 — Presupuesto con aprobación
# ---------------------------------------------------------------------------

def _presupuesto_dict(db, orden_id, presupuesto_iva):
    items = db.execute(
        "SELECT * FROM presupuesto_items WHERE orden_id = ? ORDER BY creado_en ASC", (orden_id,)
    ).fetchall()
    items_dict = []
    subtotal = 0.0
    for i in items:
        it = dict(i)
        linea = it["cantidad"] * it["precio_unitario"]
        if it["tipo"] == "descuento":
            linea = -abs(linea)
        it["subtotal"] = round(linea, 2)
        subtotal += linea
        items_dict.append(it)
    iva_monto = round(subtotal * IVA_PORCENTAJE, 2) if presupuesto_iva else 0.0
    return {
        "items": items_dict,
        "subtotal": round(subtotal, 2),
        "iva_monto": iva_monto,
        "total": round(subtotal + iva_monto, 2),
    }


@bp.route("/api/operativo/ordenes/<int:orden_id>/presupuesto/items", methods=["POST"])
def agregar_item_presupuesto(orden_id):
    db = get_db()
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    if orden["estado"] in ESTADOS_CERRADOS:
        return jsonify({"error": f"La orden está '{orden['estado']}' y no admite cambios al presupuesto."}), 409

    data = request.get_json(force=True, silent=True) or {}
    tipo = (data.get("tipo") or "").strip().lower()
    if tipo not in TIPOS_PRESUPUESTO_ITEM:
        return jsonify({"error": f"Tipo inválido. Usa uno de: {', '.join(TIPOS_PRESUPUESTO_ITEM)}"}), 400
    descripcion = (data.get("descripcion") or "").strip()
    if not descripcion:
        return jsonify({"error": "Describe el ítem del presupuesto"}), 400
    try:
        cantidad = float(data.get("cantidad") or 1)
        if cantidad <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "La cantidad debe ser un número mayor a 0"}), 400
    try:
        precio_unitario = float(data.get("precio_unitario") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El precio unitario debe ser un número válido"}), 400

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO presupuesto_items (orden_id, tipo, descripcion, cantidad, precio_unitario, usuario, creado_en) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (orden_id, tipo, descripcion, cantidad, precio_unitario, usuario_actual(), ts),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))
    db.commit()
    nuevo = db.execute("SELECT * FROM presupuesto_items WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>/presupuesto/items/<int:item_id>", methods=["DELETE"])
def eliminar_item_presupuesto(orden_id, item_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM presupuesto_items WHERE id = ? AND orden_id = ?", (item_id, orden_id)
    ).fetchone()
    if item is None:
        return jsonify({"error": "Ítem de presupuesto no encontrado"}), 404
    db.execute("DELETE FROM presupuesto_items WHERE id = ?", (item_id,))
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (now_iso(), orden_id))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/operativo/ordenes/<int:orden_id>/presupuesto", methods=["PUT"])
def actualizar_presupuesto(orden_id):
    """Cambia el estado del presupuesto (pendiente/enviado/aprobado/rechazado),
    el IVA y las notas. Al quedar 'aprobado' la orden pasa sola a
    'reparacion' — así nadie tiene que acordarse de mover el tablero a mano
    después de que el cliente dio el visto bueno."""
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    if row["estado"] in ESTADOS_CERRADOS:
        return jsonify({"error": f"La orden está '{row['estado']}' y no admite cambios al presupuesto."}), 409

    data = request.get_json(force=True, silent=True) or {}
    estado_presupuesto = row["presupuesto_estado"]
    if "estado" in data:
        estado_presupuesto = (data.get("estado") or "").strip().lower()
        if estado_presupuesto not in ESTADOS_PRESUPUESTO:
            return jsonify({"error": f"Estado de presupuesto inválido. Usa uno de: {', '.join(ESTADOS_PRESUPUESTO)}"}), 400

    iva = row["presupuesto_iva"]
    if "iva" in data:
        iva = 1 if data.get("iva") else 0

    notas = (data.get("notas", row["presupuesto_notas"]) or "").strip()

    nuevo_estado_orden = row["estado"]
    if estado_presupuesto == "aprobado" and row["presupuesto_estado"] != "aprobado":
        nuevo_estado_orden = "reparacion"

    ts = now_iso()
    db.execute(
        "UPDATE ordenes_trabajo SET presupuesto_estado = ?, presupuesto_iva = ?, presupuesto_notas = ?, "
        "estado = ?, actualizado_en = ? WHERE id = ?",
        (estado_presupuesto, iva, notas, nuevo_estado_orden, ts, orden_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return jsonify(_orden_dict_detalle(db, actualizada))


# ---------------------------------------------------------------------------
# Fase 3.5.5 — Planificación y control de tiempos
# ---------------------------------------------------------------------------

def _calcular_horas_reales(eventos):
    """Suma el tiempo efectivamente trabajado a partir de una lista de
    eventos (dict/Row con 'evento' y 'marca_tiempo', ISO, ordenados
    ascendentemente por marca_tiempo). 'inicio'/'reanudar' abren un
    intervalo; 'pausa'/'termino' lo cierran y lo suman. Eventos fuera de
    secuencia o con timestamp inválido simplemente no se cuentan, en vez de
    reventar — esto es un cálculo informativo, no debe tumbar la vista de
    la orden si el historial de eventos quedó imperfecto."""
    total_segundos = 0.0
    inicio_actual = None
    for ev in eventos:
        tipo = ev["evento"]
        try:
            momento = datetime.fromisoformat(ev["marca_tiempo"])
        except (TypeError, ValueError):
            continue
        if tipo in ("inicio", "reanudar"):
            if inicio_actual is None:
                inicio_actual = momento
        elif tipo in ("pausa", "termino"):
            if inicio_actual is not None:
                total_segundos += (momento - inicio_actual).total_seconds()
                inicio_actual = None
    return round(total_segundos / 3600, 2)


@bp.route("/api/operativo/ordenes/<int:orden_id>/tiempos", methods=["POST"])
def registrar_tiempo_orden(orden_id):
    db = get_db()
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    data = request.get_json(force=True, silent=True) or {}
    evento = (data.get("evento") or "").strip().lower()
    if evento not in EVENTOS_TIEMPO:
        return jsonify({"error": f"Evento inválido. Usa uno de: {', '.join(EVENTOS_TIEMPO)}"}), 400

    ultima_fila = db.execute(
        "SELECT evento FROM orden_tiempos WHERE orden_id = ? ORDER BY id DESC LIMIT 1", (orden_id,)
    ).fetchone()
    ultimo = ultima_fila["evento"] if ultima_fila else None

    # Validación de secuencia: evita registros que no tendrían sentido
    # (pausar sin haber iniciado, iniciar dos veces seguidas, etc.) — el
    # cálculo de horas reales asume que la secuencia es coherente.
    if evento == "inicio" and ultimo is not None:
        return jsonify({"error": "Esta orden ya tiene un registro de tiempo iniciado."}), 409
    if evento == "reanudar" and ultimo != "pausa":
        return jsonify({"error": "Solo se puede reanudar un trabajo que está pausado."}), 409
    if evento in ("pausa", "termino") and ultimo not in ("inicio", "reanudar"):
        return jsonify({"error": "No hay un trabajo en curso para pausar o terminar."}), 409

    ts = now_iso()
    db.execute(
        "INSERT INTO orden_tiempos (orden_id, evento, marca_tiempo, usuario) VALUES (?, ?, ?, ?)",
        (orden_id, evento, ts, usuario_actual()),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))
    db.commit()

    eventos = db.execute(
        "SELECT evento, marca_tiempo FROM orden_tiempos WHERE orden_id = ? ORDER BY marca_tiempo ASC", (orden_id,)
    ).fetchall()
    return jsonify({"ok": True, "evento": evento, "marca_tiempo": ts, "horas_reales": _calcular_horas_reales(eventos)})


# ---------------------------------------------------------------------------
# Fase 3.5.6 — Control de Calidad
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/ordenes/<int:orden_id>/qc/items", methods=["POST"])
def agregar_item_qc(orden_id):
    db = get_db()
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    data = request.get_json(force=True, silent=True) or {}
    item = (data.get("item") or "").strip()
    if not item:
        return jsonify({"error": "Describe el punto a revisar"}), 400
    resultado = (data.get("resultado") or "na").strip().lower()
    if resultado not in RESULTADOS_QC_ITEM:
        return jsonify({"error": f"Resultado inválido. Usa uno de: {', '.join(RESULTADOS_QC_ITEM)}"}), 400
    observacion = (data.get("observacion") or "").strip()
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO qc_checklist_items (orden_id, item, resultado, observacion, creado_en) VALUES (?, ?, ?, ?, ?)",
        (orden_id, item, resultado, observacion, ts),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM qc_checklist_items WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>/qc/items/<int:item_id>", methods=["PUT"])
def editar_item_qc(orden_id, item_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM qc_checklist_items WHERE id = ? AND orden_id = ?", (item_id, orden_id)
    ).fetchone()
    if item is None:
        return jsonify({"error": "Punto de control no encontrado"}), 404
    data = request.get_json(force=True, silent=True) or {}
    resultado = (data.get("resultado", item["resultado"]) or "na").strip().lower()
    if resultado not in RESULTADOS_QC_ITEM:
        return jsonify({"error": f"Resultado inválido. Usa uno de: {', '.join(RESULTADOS_QC_ITEM)}"}), 400
    observacion = (data.get("observacion", item["observacion"]) or "").strip()
    db.execute(
        "UPDATE qc_checklist_items SET resultado = ?, observacion = ? WHERE id = ?",
        (resultado, observacion, item_id),
    )
    db.commit()
    actualizado = db.execute("SELECT * FROM qc_checklist_items WHERE id = ?", (item_id,)).fetchone()
    return jsonify(dict(actualizado))


@bp.route("/api/operativo/ordenes/<int:orden_id>/qc/items/<int:item_id>", methods=["DELETE"])
def eliminar_item_qc(orden_id, item_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM qc_checklist_items WHERE id = ? AND orden_id = ?", (item_id, orden_id)
    ).fetchone()
    if item is None:
        return jsonify({"error": "Punto de control no encontrado"}), 404
    db.execute("DELETE FROM qc_checklist_items WHERE id = ?", (item_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/operativo/ordenes/<int:orden_id>/qc", methods=["PUT"])
def actualizar_qc(orden_id):
    """Resultado general del control de calidad. 'rechazado' devuelve la
    orden a 'reparacion' automáticamente (algo hay que corregir antes de
    poder entregar); 'aprobado' la pasa a 'listo'."""
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    if row["estado"] in ESTADOS_CERRADOS:
        return jsonify({"error": f"La orden está '{row['estado']}' y no admite cambios de control de calidad."}), 409

    data = request.get_json(force=True, silent=True) or {}
    resultado = row["qc_resultado"] or "pendiente"
    if "resultado" in data:
        resultado = (data.get("resultado") or "").strip().lower()
        if resultado not in RESULTADOS_QC:
            return jsonify({"error": f"Resultado inválido. Usa uno de: {', '.join(RESULTADOS_QC)}"}), 400
    inspector = (data.get("inspector", row["qc_inspector"]) or "").strip()
    observaciones = (data.get("observaciones", row["qc_observaciones"]) or "").strip()

    nuevo_estado_orden = row["estado"]
    if resultado == "rechazado":
        nuevo_estado_orden = "reparacion"
    elif resultado == "aprobado":
        nuevo_estado_orden = "listo"

    ts = now_iso()
    db.execute(
        "UPDATE ordenes_trabajo SET qc_resultado = ?, qc_inspector = ?, qc_observaciones = ?, "
        "estado = ?, actualizado_en = ? WHERE id = ?",
        (resultado, inspector, observaciones, nuevo_estado_orden, ts, orden_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return jsonify(_orden_dict_detalle(db, actualizada))


# ---------------------------------------------------------------------------
# Fase 3.5.7 — Garantía
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/ordenes/<int:orden_id>/garantias", methods=["POST"])
def crear_garantia(orden_id):
    db = get_db()
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    data = request.get_json(force=True, silent=True) or {}
    motivo = (data.get("motivo") or "").strip()
    if not motivo:
        return jsonify({"error": "Describe el motivo de la garantía"}), 400
    try:
        costo_asumido = float(data.get("costo_asumido") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El costo asumido debe ser un número válido"}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO garantias (orden_id, motivo, diagnostico, costo_asumido, responsable, estado, usuario, creado_en)
        VALUES (?, ?, ?, ?, ?, 'abierta', ?, ?)
        """,
        (
            orden_id, motivo,
            (data.get("diagnostico") or "").strip(),
            costo_asumido,
            (data.get("responsable") or "").strip(),
            usuario_actual(), ts,
        ),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM garantias WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nueva)), 201


@bp.route("/api/operativo/garantias", methods=["GET"])
def listar_garantias():
    db = get_db()
    estado = (request.args.get("estado") or "").strip()
    sql = (
        "SELECT g.*, o.cliente_id, o.equipo_id, o.mecanico AS mecanico_orden "
        "FROM garantias g JOIN ordenes_trabajo o ON o.id = g.orden_id WHERE 1=1"
    )
    params = []
    if estado:
        sql += " AND g.estado = ?"
        params.append(estado)
    sql += " ORDER BY g.creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    garantias = []
    for r in rows:
        d = dict(r)
        cliente = db.execute("SELECT nombre FROM clientes WHERE id = ?", (d["cliente_id"],)).fetchone()
        equipo = None
        if d["equipo_id"]:
            equipo = db.execute(
                "SELECT marca, modelo, identificador FROM equipos WHERE id = ?", (d["equipo_id"],)
            ).fetchone()
        d["cliente_nombre"] = cliente["nombre"] if cliente else None
        d["equipo_texto"] = (
            " ".join(filter(None, [equipo["marca"], equipo["modelo"], equipo["identificador"]])) if equipo else None
        )
        garantias.append(d)
    return jsonify({"garantias": garantias, "total": len(garantias)})


@bp.route("/api/operativo/garantias/<int:garantia_id>", methods=["PUT"])
def editar_garantia(garantia_id):
    db = get_db()
    row = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Garantía no encontrada"}), 404
    data = request.get_json(force=True, silent=True) or {}
    estado = (data.get("estado", row["estado"]) or "").strip().lower()
    if estado not in ESTADOS_GARANTIA:
        return jsonify({"error": f"Estado inválido. Usa uno de: {', '.join(ESTADOS_GARANTIA)}"}), 400
    cerrado_en = row["cerrado_en"]
    if estado == "cerrada" and not cerrado_en:
        cerrado_en = now_iso()
    elif estado == "abierta":
        cerrado_en = None
    try:
        costo_asumido = float(data.get("costo_asumido", row["costo_asumido"]) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "El costo asumido debe ser un número válido"}), 400
    db.execute(
        "UPDATE garantias SET motivo = ?, diagnostico = ?, costo_asumido = ?, responsable = ?, "
        "estado = ?, cerrado_en = ? WHERE id = ?",
        (
            (data.get("motivo", row["motivo"]) or "").strip(),
            (data.get("diagnostico", row["diagnostico"]) or "").strip(),
            costo_asumido,
            (data.get("responsable", row["responsable"]) or "").strip(),
            estado, cerrado_en, garantia_id,
        ),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    return jsonify(dict(actualizada))


# ---------------------------------------------------------------------------
# Fase 3.5.8 — Entrega
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/ordenes/<int:orden_id>/entregar", methods=["POST"])
def entregar_orden(orden_id):
    """Acción explícita de entrega: exige saber quién retira el vehículo
    antes de poder cerrar la orden como 'entregado' (evita que quede
    marcada como entregada sin ese dato mínimo). Fotos y firma digital
    quedan pendientes de la decisión de almacenamiento externo (bloqueo
    3.1 del plan de Fase 3.5 — Railway no garantiza disco persistente)."""
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    if row["estado"] == "cancelado":
        return jsonify({"error": "Esta orden está cancelada y no se puede entregar."}), 409

    data = request.get_json(force=True, silent=True) or {}
    recibido_por = (data.get("recibido_por") or "").strip()
    if not recibido_por:
        return jsonify({"error": "Indica quién retira el vehículo"}), 400
    documento = (data.get("documento") or "").strip()
    observaciones = (data.get("observaciones") or "").strip()

    ts = now_iso()
    fecha_entrega_real = row["fecha_entrega_real"] or ts
    db.execute(
        """
        UPDATE ordenes_trabajo SET
            estado = 'entregado', fecha_entrega_real = ?, entrega_recibido_por = ?,
            entrega_documento = ?, entrega_observaciones = ?, actualizado_en = ?
        WHERE id = ?
        """,
        (fecha_entrega_real, recibido_por, documento, observaciones, ts, orden_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return jsonify(_orden_dict_detalle(db, actualizada))


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------

ESTADOS_LABEL = {
    "recibido": "Recibido",
    "diagnostico": "Diagnóstico",
    "presupuesto": "Presupuesto",
    "esperando_aprobacion": "Esperando aprobación",
    "reparacion": "Reparación",
    "esperando_repuesto": "Esperando repuesto",
    "control_calidad": "Control de calidad",
    "listo": "Listo",
    "entregado": "Entregado",
    "cancelado": "Cancelado",
}


def _ordenes_para_reporte(desde, hasta):
    db = get_db()
    sql = "SELECT * FROM ordenes_trabajo WHERE 1=1"
    params = []
    if desde:
        sql += " AND substr(fecha_recepcion, 1, 10) >= ?"
        params.append(desde)
    if hasta:
        sql += " AND substr(fecha_recepcion, 1, 10) <= ?"
        params.append(hasta)
    sql += " ORDER BY fecha_recepcion DESC"
    rows = db.execute(sql, params).fetchall()
    return [_orden_dict(db, r) for r in rows]


@bp.route("/api/operativo/reportes/ordenes", methods=["GET"])
def reporte_ordenes():
    desde, hasta = _rango_fechas()
    ordenes = _ordenes_para_reporte(desde, hasta)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Órdenes de trabajo"
    ws.append(["Fecha recepción", "Cliente", "Equipo", "Estado", "Mecánico", "Entrega estimada", "Entrega real", "Total"])
    total_general = 0
    for o in ordenes:
        total_general += o["total"]
        equipo_txt = ""
        if o["equipo"]:
            equipo_txt = " ".join(filter(None, [o["equipo"].get("marca"), o["equipo"].get("modelo"), o["equipo"].get("identificador")]))
        ws.append([
            (o["fecha_recepcion"] or "")[:10],
            o["cliente"]["nombre"] if o["cliente"] else "—",
            equipo_txt or "—",
            ESTADOS_LABEL.get(o["estado"], o["estado"]),
            o["mecanico"] or "—",
            (o["fecha_entrega_estimada"] or "")[:10],
            (o["fecha_entrega_real"] or "")[:10],
            round(o["total"], 2),
        ])
    ws.append([])
    ws.append(["", "", "", "", "", "", "TOTAL", round(total_general, 2)])
    _estilo_encabezado(ws)
    _autoancho(ws)

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"ordenes_trabajo{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@bp.route("/api/operativo/reportes/ordenes/pdf", methods=["GET"])
def reporte_ordenes_pdf():
    desde, hasta = _rango_fechas()
    ordenes = _ordenes_para_reporte(desde, hasta)

    rango_txt = f" ({desde or '…'} a {hasta or '…'})" if (desde or hasta) else ""
    buffer, doc, elementos, estilos = _pdf_documento(f"Órdenes de trabajo{rango_txt}")

    total_general = sum(o["total"] for o in ordenes)
    filas = []
    for o in ordenes:
        equipo_txt = ""
        if o["equipo"]:
            equipo_txt = " ".join(filter(None, [o["equipo"].get("marca"), o["equipo"].get("modelo"), o["equipo"].get("identificador")]))
        filas.append([
            (o["fecha_recepcion"] or "")[:10],
            o["cliente"]["nombre"] if o["cliente"] else "—",
            equipo_txt or "—",
            ESTADOS_LABEL.get(o["estado"], o["estado"]),
            o["mecanico"] or "—",
            fmt_money_pdf(o["total"]),
        ])
    _pdf_tabla(
        elementos, estilos, f"Total: {fmt_money_pdf(total_general)} — {len(ordenes)} orden(es)",
        ["Fecha", "Cliente", "Equipo", "Estado", "Mecánico", "Total"],
        filas,
    )

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"ordenes_trabajo{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


@bp.route("/api/operativo/reportes/garantias", methods=["GET"])
def reporte_garantias():
    """Excel con el detalle de garantías del período y dos resúmenes
    (por técnico y por equipo) — el análisis que el bosquejo original
    pedía para detectar qué mecánico, modelo o proveedor genera más
    reclamos de garantía."""
    desde, hasta = _rango_fechas()
    db = get_db()
    sql = (
        "SELECT g.*, o.cliente_id, o.equipo_id, o.mecanico AS mecanico_orden "
        "FROM garantias g JOIN ordenes_trabajo o ON o.id = g.orden_id WHERE 1=1"
    )
    params = []
    if desde:
        sql += " AND substr(g.creado_en, 1, 10) >= ?"
        params.append(desde)
    if hasta:
        sql += " AND substr(g.creado_en, 1, 10) <= ?"
        params.append(hasta)
    sql += " ORDER BY g.creado_en DESC"
    rows = db.execute(sql, params).fetchall()

    detalle = []
    conteo_tecnico = {}
    conteo_equipo = {}
    for r in rows:
        d = dict(r)
        cliente = db.execute("SELECT nombre FROM clientes WHERE id = ?", (d["cliente_id"],)).fetchone()
        equipo_txt = "—"
        if d["equipo_id"]:
            eq = db.execute(
                "SELECT marca, modelo, identificador FROM equipos WHERE id = ?", (d["equipo_id"],)
            ).fetchone()
            if eq:
                equipo_txt = " ".join(filter(None, [eq["marca"], eq["modelo"], eq["identificador"]])) or "—"
        tecnico = d["mecanico_orden"] or "Sin asignar"
        conteo_tecnico[tecnico] = conteo_tecnico.get(tecnico, 0) + 1
        conteo_equipo[equipo_txt] = conteo_equipo.get(equipo_txt, 0) + 1
        detalle.append([
            (d["creado_en"] or "")[:10],
            f"OT #{d['orden_id']}",
            cliente["nombre"] if cliente else "—",
            equipo_txt,
            tecnico,
            d["motivo"],
            d["estado"].capitalize(),
            round(d["costo_asumido"], 2),
        ])

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Garantías"
    ws.append(["Fecha", "Orden", "Cliente", "Equipo", "Mecánico", "Motivo", "Estado", "Costo asumido"])
    for fila in detalle:
        ws.append(fila)
    _estilo_encabezado(ws)
    _autoancho(ws)

    ws2 = wb.create_sheet("Resumen por técnico")
    ws2.append(["Mecánico", "Cantidad de garantías"])
    for tecnico, cantidad in sorted(conteo_tecnico.items(), key=lambda x: -x[1]):
        ws2.append([tecnico, cantidad])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    ws3 = wb.create_sheet("Resumen por equipo")
    ws3.append(["Equipo", "Cantidad de garantías"])
    for equipo_txt, cantidad in sorted(conteo_equipo.items(), key=lambda x: -x[1]):
        ws3.append([equipo_txt, cantidad])
    _estilo_encabezado(ws3)
    _autoancho(ws3)

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"garantias{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)
