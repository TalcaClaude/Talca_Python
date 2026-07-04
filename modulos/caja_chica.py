"""
Módulo Caja Chica — primer módulo separado de app.py como piloto de la
nueva estructura modular del ERP (ver arquitectura_erp_taller.md).

Se eligió Caja Chica para partir porque es un dominio bastante
autocontenido: usa la tabla productos/movimientos solo en un caso puntual
(la "compra de emergencia" que ingresa stock junto con un gasto), y el
resto de su lógica (aportes, gastos, rendiciones, comprobantes) no
depende de nada más del sistema.

Comparte con el resto de la app: la conexión a la base de datos, el
usuario de la sesión actual, y las utilidades de generación de reportes
Excel/PDF — todo eso se importa desde core.py (nunca desde app.py: importar
desde app.py fue el intento original y causaba un import circular apenas
la app se corría como "python3 app.py" en vez de importarse como paquete,
porque app.py también termina importando este mismo módulo para registrar
su Blueprint. core.py no depende de app.py ni de ningún módulo, así que
cualquier módulo nuevo debe importar de ahí, igual que este).
"""

import os
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename

from core import (
    COMPROBANTES_DIR,
    EXTENSIONES_COMPROBANTE_PERMITIDAS,
    _aplicar_entrada_stock,
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

bp = Blueprint("caja_chica", __name__)


def _guardar_comprobante(archivo):
    """Guarda el archivo subido (boleta/comprobante) en COMPROBANTES_DIR con
    un nombre único, y devuelve el nombre guardado (o '' si no venía nada).
    Valida la extensión para no aceptar cualquier tipo de archivo."""
    if not archivo or not archivo.filename:
        return ""
    nombre_original = secure_filename(archivo.filename)
    ext = os.path.splitext(nombre_original)[1].lower()
    if ext not in EXTENSIONES_COMPROBANTE_PERMITIDAS:
        raise ValueError(f"Tipo de archivo no permitido para comprobantes ({ext or 'sin extensión'}). Usa foto (jpg/png/webp/heic) o PDF.")
    nombre_guardado = f"{uuid.uuid4().hex}{ext}"
    archivo.save(os.path.join(COMPROBANTES_DIR, nombre_guardado))
    return nombre_guardado


def _caja_bloqueo_motivo(row):
    """Explica por qué un gasto/aporte de Caja Chica NO se puede editar ni
    eliminar (para no descuadrar el saldo o la trazabilidad), o None si es
    seguro modificarlo libremente."""
    if row["rendicion_id"] is not None:
        return "Este gasto ya fue incluido en una rendición y no se puede modificar."
    if row["rendicion_origen_id"] is not None:
        return ("Este aporte lo generó automáticamente una rendición y no se puede modificar suelto "
                "(dejaría el saldo descuadrado respecto a los gastos que ya quedaron marcados como rendidos).")
    if row["movimiento_inventario_id"] is not None:
        return ("Este gasto ingresó stock al inventario (compra de emergencia). "
                "Ajusta el stock manualmente primero si corresponde, y luego edita o elimina el gasto.")
    return None


def _caja_movimiento_dict(row):
    d = dict(row)
    d["tiene_comprobante"] = bool(d.get("comprobante_archivo"))
    d["rendido"] = d.get("rendicion_id") is not None
    d["bloqueado_motivo"] = _caja_bloqueo_motivo(row)
    return d


@bp.route("/api/caja/resumen", methods=["GET"])
def caja_resumen():
    db = get_db()
    total_aportado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'aporte'"
    ).fetchone()[0]
    total_gastado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto'"
    ).fetchone()[0]
    pendiente_rendir = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND rendicion_id IS NULL"
    ).fetchone()[0]

    inicio_mes = datetime.now().strftime("%Y-%m") + "-01"
    gastado_mes = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND fecha >= ?",
        (inicio_mes,),
    ).fetchone()[0]

    return jsonify({
        "saldo_disponible": total_aportado - total_gastado,
        "total_aportado": total_aportado,
        "total_gastado": total_gastado,
        "pendiente_rendir": pendiente_rendir,
        "gastado_mes": gastado_mes,
    })


@bp.route("/api/caja/movimientos", methods=["GET"])
def caja_listar_movimientos():
    db = get_db()

    tipo = request.args.get("tipo", "").strip()
    estado = request.args.get("estado", "").strip()  # 'pendiente' | 'rendido' (solo aplica a gasto)
    desde = request.args.get("desde", "").strip()
    hasta = request.args.get("hasta", "").strip()
    q = request.args.get("q", "").strip()

    sql = "SELECT * FROM caja_movimientos WHERE 1=1"
    params = []
    if tipo:
        sql += " AND tipo = ?"
        params.append(tipo)
    if estado == "pendiente":
        sql += " AND tipo = 'gasto' AND rendicion_id IS NULL"
    elif estado == "rendido":
        sql += " AND tipo = 'gasto' AND rendicion_id IS NOT NULL"
    if desde:
        sql += " AND fecha >= ?"
        params.append(desde)
    if hasta:
        sql += " AND fecha <= ?"
        params.append(hasta + "T23:59:59")
    if q:
        sql += " AND (categoria LIKE ? OR descripcion LIKE ? OR proveedor LIKE ? OR usuario LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like]

    sql += " ORDER BY fecha DESC, id DESC LIMIT 500"
    rows = db.execute(sql, params).fetchall()
    movimientos = [_caja_movimiento_dict(r) for r in rows]

    return jsonify({"movimientos": movimientos, "total": len(movimientos)})


@bp.route("/api/caja/gasto", methods=["POST"])
def caja_crear_gasto():
    db = get_db()
    _iniciar_transaccion_stock(db)  # puede incluir una "compra de emergencia" que ingresa stock
    data = request.form

    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número mayor a 0"}), 400

    fecha = (data.get("fecha") or "").strip()
    fecha = f"{fecha}T{datetime.now().strftime('%H:%M:%S')}" if fecha else now_iso()

    categoria = (data.get("categoria") or "").strip().upper()
    descripcion = (data.get("descripcion") or "").strip().upper()
    proveedor = (data.get("proveedor") or "").strip().upper()

    try:
        comprobante = _guardar_comprobante(request.files.get("comprobante"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # ---- Compra de emergencia asociada al inventario: si viene un producto
    # y una cantidad, además de registrar el gasto se ingresa el stock como
    # una entrada normal (igual que una factura), dejando ambos registros
    # enlazados para que se pueda seguir el rastro del dinero al producto.
    producto_id = (data.get("producto_id") or "").strip()
    movimiento_inventario_id = None
    if producto_id:
        try:
            producto_id = int(producto_id)
            cantidad = int(data.get("cantidad") or 0)
            if cantidad <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "Para asociar una compra de emergencia, indica un producto y una cantidad válida"}), 400

        producto_row = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if producto_row is None:
            return jsonify({"error": "El producto seleccionado no existe"}), 400

        costo_unitario = monto / cantidad
        nuevo_stock = _aplicar_entrada_stock(
            db, producto_row, cantidad, costo_unitario,
            motivo=f"COMPRA DE EMERGENCIA (CAJA CHICA){' - ' + descripcion if descripcion else ''}",
            usuario=usuario_actual(), factura=None, categoria="Compra",
        )
        movimiento_inventario_id = db.execute(
            "SELECT id FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1", (producto_id,)
        ).fetchone()[0]
    else:
        producto_id = None

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO caja_movimientos (
            tipo, fecha, monto, categoria, descripcion, proveedor, usuario,
            comprobante_archivo, producto_id, movimiento_inventario_id, creado_en
        ) VALUES ('gasto', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fecha, monto, categoria, descripcion, proveedor, usuario_actual(),
         comprobante, producto_id, movimiento_inventario_id, ts),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_caja_movimiento_dict(nuevo)), 201


@bp.route("/api/caja/aporte", methods=["POST"])
def caja_crear_aporte():
    db = get_db()
    data = request.form

    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número mayor a 0"}), 400

    fecha = (data.get("fecha") or "").strip()
    fecha = f"{fecha}T{datetime.now().strftime('%H:%M:%S')}" if fecha else now_iso()
    descripcion = (data.get("descripcion") or "").strip().upper()

    try:
        comprobante = _guardar_comprobante(request.files.get("comprobante"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO caja_movimientos (tipo, fecha, monto, descripcion, usuario, comprobante_archivo, creado_en)
        VALUES ('aporte', ?, ?, ?, ?, ?, ?)
        """,
        (fecha, monto, descripcion, usuario_actual(), comprobante, ts),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_caja_movimiento_dict(nuevo)), 201


@bp.route("/api/caja/movimiento/<int:mov_id>", methods=["DELETE"])
def caja_eliminar_movimiento(mov_id):
    db = get_db()
    row = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (mov_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Movimiento no encontrado"}), 404

    bloqueo = _caja_bloqueo_motivo(row)
    if bloqueo:
        return jsonify({"error": bloqueo}), 409

    if row["comprobante_archivo"]:
        ruta = os.path.join(COMPROBANTES_DIR, row["comprobante_archivo"])
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass

    db.execute("DELETE FROM caja_movimientos WHERE id = ?", (mov_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/caja/movimiento/<int:mov_id>", methods=["PUT"])
def caja_editar_movimiento(mov_id):
    """Editar un gasto o aporte (fecha, monto, categoría, descripción,
    proveedor, y opcionalmente reemplazar el comprobante). Usa el mismo
    candado que Eliminar: si ya está rendido, es una reposición automática,
    o generó un ingreso de stock, no se puede tocar — para no descuadrar el
    saldo ni la trazabilidad."""
    db = get_db()
    row = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (mov_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Movimiento no encontrado"}), 404

    bloqueo = _caja_bloqueo_motivo(row)
    if bloqueo:
        return jsonify({"error": bloqueo}), 409

    data = request.form

    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número mayor a 0"}), 400

    fecha_nueva = (data.get("fecha") or "").strip()
    fecha_final = f"{fecha_nueva}T{datetime.now().strftime('%H:%M:%S')}" if fecha_nueva else row["fecha"]
    descripcion = (data.get("descripcion") or "").strip().upper()

    campos = {"monto": monto, "fecha": fecha_final, "descripcion": descripcion}
    if row["tipo"] == "gasto":
        campos["categoria"] = (data.get("categoria") or "").strip().upper()
        campos["proveedor"] = (data.get("proveedor") or "").strip().upper()

    if "comprobante" in request.files and request.files["comprobante"].filename:
        try:
            nuevo_comprobante = _guardar_comprobante(request.files.get("comprobante"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if row["comprobante_archivo"]:
            ruta_vieja = os.path.join(COMPROBANTES_DIR, row["comprobante_archivo"])
            if os.path.exists(ruta_vieja):
                try:
                    os.remove(ruta_vieja)
                except OSError:
                    pass
        campos["comprobante_archivo"] = nuevo_comprobante

    set_clause = ", ".join(f"{k} = ?" for k in campos)
    db.execute(f"UPDATE caja_movimientos SET {set_clause} WHERE id = ?", (*campos.values(), mov_id))
    db.commit()

    actualizado = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (mov_id,)).fetchone()
    return jsonify(_caja_movimiento_dict(actualizado))


@bp.route("/api/caja/comprobante/<path:nombre_archivo>", methods=["GET"])
def caja_ver_comprobante(nombre_archivo):
    nombre_seguro = secure_filename(nombre_archivo)
    ruta = os.path.join(COMPROBANTES_DIR, nombre_seguro)
    if not os.path.isfile(ruta):
        return jsonify({"error": "Comprobante no encontrado"}), 404
    return send_file(ruta)


@bp.route("/api/caja/pendientes", methods=["GET"])
def caja_gastos_pendientes():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM caja_movimientos WHERE tipo = 'gasto' AND rendicion_id IS NULL ORDER BY fecha ASC"
    ).fetchall()
    return jsonify({"gastos": [_caja_movimiento_dict(r) for r in rows]})


@bp.route("/api/caja/rendicion", methods=["POST"])
def caja_crear_rendicion():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    gasto_ids = data.get("gasto_ids") or []
    nota = (data.get("nota") or "").strip().upper()

    if not gasto_ids:
        return jsonify({"error": "Selecciona al menos un gasto para rendir"}), 400

    placeholders = ",".join("?" for _ in gasto_ids)
    gastos = db.execute(
        f"SELECT * FROM caja_movimientos WHERE id IN ({placeholders}) AND tipo = 'gasto'",
        gasto_ids,
    ).fetchall()
    if len(gastos) != len(gasto_ids):
        return jsonify({"error": "Alguno de los gastos seleccionados no existe"}), 400
    if any(g["rendicion_id"] is not None for g in gastos):
        return jsonify({"error": "Alguno de los gastos seleccionados ya fue rendido antes"}), 400

    total = sum(g["monto"] for g in gastos)
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO caja_rendiciones (fecha, usuario, total, nota, creado_en) VALUES (?, ?, ?, ?, ?)",
        (ts, usuario_actual(), total, nota, ts),
    )
    rendicion_id = cur.lastrowid

    db.execute(
        f"UPDATE caja_movimientos SET rendicion_id = ? WHERE id IN ({placeholders})",
        [rendicion_id] + gasto_ids,
    )

    # Reposición automática: el saldo vuelve a subir de inmediato por el
    # total rendido (a pedido del taller — se asume que la plata vuelve
    # junto con la rendición). Queda marcada con rendicion_origen_id para
    # que no se pueda borrar suelta y dejar el saldo descuadrado respecto a
    # los gastos que ya quedaron marcados como rendidos.
    db.execute(
        """
        INSERT INTO caja_movimientos (tipo, fecha, monto, descripcion, usuario, rendicion_origen_id, creado_en)
        VALUES ('aporte', ?, ?, ?, ?, ?, ?)
        """,
        (ts, total, f"REPOSICIÓN AUTOMÁTICA - RENDICIÓN #{rendicion_id}", usuario_actual(), rendicion_id, ts),
    )

    db.commit()
    nueva = db.execute("SELECT * FROM caja_rendiciones WHERE id = ?", (rendicion_id,)).fetchone()
    return jsonify(dict(nueva)), 201


@bp.route("/api/caja/rendiciones", methods=["GET"])
def caja_listar_rendiciones():
    db = get_db()
    rows = db.execute("SELECT * FROM caja_rendiciones ORDER BY fecha DESC, id DESC").fetchall()
    rendiciones = []
    for r in rows:
        d = dict(r)
        d["n_gastos"] = db.execute(
            "SELECT COUNT(*) FROM caja_movimientos WHERE rendicion_id = ?", (r["id"],)
        ).fetchone()[0]
        rendiciones.append(d)
    return jsonify({"rendiciones": rendiciones})


@bp.route("/api/caja/rendicion/<int:rendicion_id>", methods=["GET"])
def caja_detalle_rendicion(rendicion_id):
    db = get_db()
    rendicion = db.execute("SELECT * FROM caja_rendiciones WHERE id = ?", (rendicion_id,)).fetchone()
    if rendicion is None:
        return jsonify({"error": "Rendición no encontrada"}), 404
    gastos = db.execute(
        "SELECT * FROM caja_movimientos WHERE rendicion_id = ? ORDER BY fecha ASC", (rendicion_id,)
    ).fetchall()
    d = dict(rendicion)
    d["gastos"] = [_caja_movimiento_dict(g) for g in gastos]
    return jsonify(d)

def _gastos_caja_para_reporte(desde, hasta):
    db = get_db()
    sql = "SELECT * FROM caja_movimientos WHERE tipo = 'gasto'"
    params = []
    if desde:
        sql += " AND fecha >= ?"
        params.append(desde)
    if hasta:
        sql += " AND fecha <= ?"
        params.append(hasta + "T23:59:59")
    sql += " ORDER BY fecha DESC, id DESC"
    return [_caja_movimiento_dict(r) for r in db.execute(sql, params).fetchall()]


@bp.route("/api/reportes/caja-gastos", methods=["GET"])
def reporte_caja_gastos():
    desde, hasta = _rango_fechas()
    gastos = _gastos_caja_para_reporte(desde, hasta)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Gastos Caja Chica"
    ws.append(["Fecha", "Categoría", "Descripción", "Proveedor", "Monto", "Estado", "Responsable"])
    total = 0
    for g in gastos:
        total += g["monto"]
        ws.append([
            g["fecha"], g["categoria"] or "", g["descripcion"] or "", g["proveedor"] or "",
            round(g["monto"], 2), "Rendido" if g["rendido"] else "Pendiente", g["usuario"] or "",
        ])
    ws.append([])
    ws.append(["", "", "", "", round(total, 2), "", ""])
    _estilo_encabezado(ws)
    _autoancho(ws)

    ws2 = wb.create_sheet("Por categoría")
    ws2.append(["Categoría", "N° de gastos", "Total"])
    por_categoria = {}
    for g in gastos:
        cat = g["categoria"] or "(sin categoría)"
        acc = por_categoria.setdefault(cat, {"n": 0, "total": 0})
        acc["n"] += 1
        acc["total"] += g["monto"]
    for cat, acc in sorted(por_categoria.items(), key=lambda kv: kv[1]["total"], reverse=True):
        ws2.append([cat, acc["n"], round(acc["total"], 2)])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"gastos_caja_chica{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@bp.route("/api/reportes/caja-gastos/pdf", methods=["GET"])
def reporte_caja_gastos_pdf():
    desde, hasta = _rango_fechas()
    gastos = _gastos_caja_para_reporte(desde, hasta)

    rango_txt = f" ({desde or '…'} a {hasta or '…'})" if (desde or hasta) else ""
    buffer, doc, elementos, estilos = _pdf_documento(f"Gastos de Caja Chica{rango_txt}")

    total = sum(g["monto"] for g in gastos)
    filas = [
        [g["fecha"][:10], g["categoria"] or "—", g["descripcion"] or "—", fmt_money_pdf(g["monto"]),
         "Rendido" if g["rendido"] else "Pendiente", g["usuario"] or "—"]
        for g in gastos
    ]
    _pdf_tabla(
        elementos, estilos, f"Total gastado: {fmt_money_pdf(total)} — {len(gastos)} gasto(s)",
        ["Fecha", "Categoría", "Descripción", "Monto", "Estado", "Responsable"],
        filas,
    )

    sufijo = f"_{desde}_a_{hasta}" if (desde or hasta) else ""
    nombre = f"gastos_caja_chica{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)
