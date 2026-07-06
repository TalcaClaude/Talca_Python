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
    _iniciar_transaccion_stock,
    get_db,
    now_iso,
    usuario_actual,
)

bp = Blueprint("operativo", __name__)

ESTADOS_ORDEN = [
    "recibido", "diagnostico", "reparacion", "esperando_repuesto", "listo", "entregado", "cancelado",
]
# Estados que cuentan como "la orden ya terminó" — no deberían poder seguir
# recibiendo repuestos o mano de obra nueva sin primero reabrirlas.
ESTADOS_CERRADOS = {"entregado", "cancelado"}

TIPOS_ITEM = ["repuesto", "mano_obra"]


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


@bp.route("/api/operativo/ordenes/<int:orden_id>", methods=["GET"])
def obtener_orden(orden_id):
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    return jsonify(_orden_dict(db, row))


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

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO ordenes_trabajo (
            cliente_id, equipo_id, estado, descripcion_problema, mecanico,
            fecha_recepcion, fecha_entrega_estimada, usuario, creado_en, actualizado_en
        ) VALUES (?, ?, 'recibido', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cliente_id, equipo_id,
            (data.get("descripcion_problema") or "").strip(),
            (data.get("mecanico") or "").strip(),
            ts,
            (data.get("fecha_entrega_estimada") or "").strip() or None,
            usuario_actual(), ts, ts,
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

    db.execute(
        """
        UPDATE ordenes_trabajo SET
            estado = ?, descripcion_problema = ?, diagnostico = ?, mecanico = ?,
            fecha_entrega_estimada = ?, fecha_entrega_real = ?, actualizado_en = ?
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
