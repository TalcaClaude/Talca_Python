"""
Módulo Finanzas — antes era "Caja Chica", el primer módulo separado de
app.py como piloto de la nueva estructura modular del ERP (ver
arquitectura_erp_taller.md). Caja Chica sigue viviendo acá adentro (mismas
tablas, mismos endpoints /api/caja/...), pero ahora el módulo también
expone un resumen financiero general (ingresos cobrados a clientes vs.
gastos de caja chica) para la pestaña "Resumen" del nuevo módulo Finanzas
en el frontend (antes esa pestaña no existía y Caja Chica vivía dentro de
Logística).

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
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename

from core import (
    ANTICIPO_PORCENTAJE_DEFECTO,
    COMPROBANTES_DIR,
    EXTENSIONES_COMPROBANTE_PERMITIDAS,
    IVA_PORCENTAJE,
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
    coincide_busqueda,
    fmt_money_pdf,
    get_db,
    now_iso,
    requiere_rol,
    usuario_actual,
)

# Estados de una OT en los que ya se aprobó el presupuesto y el trabajo está
# en curso, pero todavía no llega a 'listo'/'entregado' (ver
# modulos/operativo.py: ESTADOS_QUE_CIERRAN_CXC). Mientras una orden esté en
# uno de estos estados y no haya alcanzado el anticipo pactado
# (ANTICIPO_PORCENTAJE_DEFECTO, hoy 50%), aparece en la pestaña "CxC por
# abonos" de Finanzas — ver finanzas_listar_cxc_abonos más abajo.
ESTADOS_CXC_ABONO = {"reparacion", "esperando_repuesto", "control_calidad"}

bp = Blueprint("finanzas", __name__)


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


# ---------------------------------------------------------------------------
# Resumen financiero general (pestaña "Resumen" del módulo Finanzas) — cruza
# dos fuentes que hasta ahora vivían en módulos separados:
#   - Ingresos: lo efectivamente cobrado a los clientes por las OTs
#     (tabla pagos_orden, del módulo Operativo — plata que entró de verdad,
#     no el valor "de papel" de la orden si todavía no se ha pagado).
#   - Gastos: los gastos de Caja Chica (tabla caja_movimientos, tipo
#     'gasto'). Los "aportes" no se cuentan como gasto ni como ingreso: son
#     solo un traspaso interno de plata hacia la caja chica, no plata nueva
#     que entra al taller.
# El saldo de caja chica se muestra aparte porque es la plata física
# disponible AHORA (no depende del rango de fechas elegido), mientras que
# ingresos/gastos/resultado sí son del período seleccionado.
# ---------------------------------------------------------------------------

@bp.route("/api/finanzas/resumen", methods=["GET"])
def finanzas_resumen():
    desde, hasta = _rango_fechas()
    if not desde and not hasta:
        desde = datetime.now().strftime("%Y-%m") + "-01"
    resumen = _resumen_financiero(desde, hasta)
    return jsonify({"desde": desde, "hasta": hasta, **resumen})


@bp.route("/api/finanzas/resumen/graficos", methods=["GET"])
def finanzas_resumen_graficos():
    """Datos para los gráficos del dashboard de Finanzas:
      - tendencia_mensual: ingresos (pagos_orden) vs gastos (caja_movimientos
        tipo gasto), últimos 12 meses calendario.
      - gasto_por_proveedor: top proveedores por gasto acumulado en Caja
        Chica (campo de texto libre 'proveedor', no siempre coincide 1:1
        con la tabla proveedores porque históricamente se escribía a mano).
      - cxc_aging: cuentas por cobrar pendientes, agrupadas por antigüedad
        del vencimiento (al día / 1-30 / 31-60 / 61-90 / +90 días).
      - cxp_aging: lo mismo pero para cuentas por pagar (a proveedores) --
        mismos tramos, mismo criterio (saldo pendiente por antigüedad de
        vencimiento), usando cxp_pagos en vez de pagos_orden para calcular
        el saldo.
    """
    db = get_db()
    hoy = datetime.now()

    cursor_mes = hoy.replace(day=1)
    meses = []
    m = cursor_mes
    for _ in range(12):
        meses.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    meses.reverse()

    tendencia = []
    for m in meses:
        pref = f"{m.year:04d}-{m.month:02d}"
        ingresos = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE substr(fecha, 1, 7) = ?", (pref,)
        ).fetchone()[0]
        gastos = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND substr(fecha, 1, 7) = ?",
            (pref,),
        ).fetchone()[0]
        tendencia.append({"mes": pref, "ingresos": ingresos, "gastos": gastos})

    gasto_por_proveedor = [
        {"proveedor": r["proveedor"] or "(sin especificar)", "monto": r["monto"]}
        for r in db.execute(
            """
            SELECT COALESCE(NULLIF(proveedor, ''), '(sin especificar)') AS proveedor, SUM(monto) AS monto
            FROM caja_movimientos WHERE tipo = 'gasto'
            GROUP BY proveedor ORDER BY monto DESC LIMIT 8
            """
        ).fetchall()
    ]

    buckets = [
        {"label": "Al día", "min": None, "max": 0, "monto": 0.0},
        {"label": "1-30 días", "min": 1, "max": 30, "monto": 0.0},
        {"label": "31-60 días", "min": 31, "max": 60, "monto": 0.0},
        {"label": "61-90 días", "min": 61, "max": 90, "monto": 0.0},
        {"label": "+90 días", "min": 91, "max": None, "monto": 0.0},
    ]
    for f in db.execute(
        "SELECT id, orden_id, monto_total, fecha_vencimiento FROM cuentas_por_cobrar WHERE estado != 'pagada'"
    ).fetchall():
        pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = ?", (f["orden_id"],)
        ).fetchone()[0]
        saldo = f["monto_total"] - pagado
        if saldo <= 0:
            continue
        dias = 0
        if f["fecha_vencimiento"]:
            try:
                dias = (datetime.now() - datetime.fromisoformat(f["fecha_vencimiento"][:10])).days
            except ValueError:
                dias = 0
        for b in buckets:
            if (b["min"] is None or dias >= b["min"]) and (b["max"] is None or dias <= b["max"]):
                b["monto"] += saldo
                break

    for b in buckets:
        b["monto"] = round(b["monto"], 2)
        del b["min"]
        del b["max"]

    buckets_cxp = [
        {"label": "Al día", "min": None, "max": 0, "monto": 0.0},
        {"label": "1-30 días", "min": 1, "max": 30, "monto": 0.0},
        {"label": "31-60 días", "min": 31, "max": 60, "monto": 0.0},
        {"label": "61-90 días", "min": 61, "max": 90, "monto": 0.0},
        {"label": "+90 días", "min": 91, "max": None, "monto": 0.0},
    ]
    for f in db.execute(
        "SELECT id, monto_total, fecha_vencimiento FROM cuentas_por_pagar WHERE estado NOT IN ('pagada', 'anulada')"
    ).fetchall():
        pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (f["id"],)
        ).fetchone()[0]
        saldo = f["monto_total"] - pagado
        if saldo <= 0:
            continue
        dias = 0
        if f["fecha_vencimiento"]:
            try:
                dias = (datetime.now() - datetime.fromisoformat(f["fecha_vencimiento"][:10])).days
            except ValueError:
                dias = 0
        for b in buckets_cxp:
            if (b["min"] is None or dias >= b["min"]) and (b["max"] is None or dias <= b["max"]):
                b["monto"] += saldo
                break

    for b in buckets_cxp:
        b["monto"] = round(b["monto"], 2)
        del b["min"]
        del b["max"]

    return jsonify({
        "tendencia_mensual": tendencia,
        "gasto_por_proveedor": gasto_por_proveedor,
        "cxc_aging": buckets,
        "cxp_aging": buckets_cxp,
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

    # 'q' se filtra en Python con coincide_busqueda (ver core.py): admite
    # varias palabras sueltas, sin tildes, que pueden calzar en columnas
    # distintas (ej. "ferreteria bomba" encuentra un gasto de ese proveedor
    # Y esa descripción, aunque sean campos separados).
    sql += " ORDER BY fecha DESC, id DESC"
    rows = db.execute(sql, params).fetchall()
    movimientos = [_caja_movimiento_dict(r) for r in rows]

    if q:
        movimientos = [
            m for m in movimientos
            if coincide_busqueda(q, m.get("categoria"), m.get("descripcion"), m.get("proveedor"), m.get("usuario"))
        ]

    movimientos = movimientos[:500]

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
    # Por defecto True para no cambiar el comportamiento de siempre. Se pasa
    # False cuando los gastos que se están rindiendo ya se cubrieron antes
    # con un aporte manual — si igual repusiéramos acá, el aporte manual +
    # la reposición automática sumarían el doble de plata de la que
    # realmente entró a la caja.
    reponer_saldo = data.get("reponer_saldo", True)

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

    # Reposición automática (opcional, tildada por defecto): el saldo vuelve
    # a subir de inmediato por el total rendido, asumiendo que la plata
    # vuelve junto con la rendición. Se puede desactivar desde el checkbox
    # "Reponer el saldo automáticamente" del modal, para el caso en que esos
    # gastos ya se cubrieron con un aporte manual hecho antes — ahí rendir
    # solo cierra/reporta los gastos, sin agregar plata de más. Cuando sí se
    # crea, queda marcada con rendicion_origen_id para que no se pueda
    # borrar suelta y dejar el saldo descuadrado respecto a los gastos que
    # ya quedaron marcados como rendidos.
    if reponer_saldo:
        db.execute(
            """
            INSERT INTO caja_movimientos (tipo, fecha, monto, descripcion, usuario, rendicion_origen_id, creado_en)
            VALUES ('aporte', ?, ?, ?, ?, ?, ?)
            """,
            (ts, total, f"REPOSICIÓN AUTOMÁTICA - RENDICIÓN #{rendicion_id}", usuario_actual(), rendicion_id, ts),
        )

    db.commit()
    nueva = db.execute("SELECT * FROM caja_rendiciones WHERE id = ?", (rendicion_id,)).fetchone()
    d = dict(nueva)
    d["reposicion_automatica"] = bool(reponer_saldo)
    return jsonify(d), 201


def _rendicion_tuvo_reposicion(db, rendicion_id):
    """La reposición automática (si se generó) queda como un caja_movimientos
    con rendicion_origen_id = esta rendición — no hace falta una columna
    aparte, alcanza con mirar si existe ese movimiento."""
    fila = db.execute(
        "SELECT 1 FROM caja_movimientos WHERE rendicion_origen_id = ? LIMIT 1", (rendicion_id,)
    ).fetchone()
    return fila is not None


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
        d["reposicion_automatica"] = _rendicion_tuvo_reposicion(db, r["id"])
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
    d["reposicion_automatica"] = _rendicion_tuvo_reposicion(db, rendicion_id)
    return jsonify(d)

# ---------------------------------------------------------------------------
# Cuentas por Cobrar (CxC) y Cuentas por Pagar (CxP) — las tablas se generan
# solas (ver modulos/operativo.py: _generar_o_actualizar_cxc, y app.py:
# _generar_cxp_de_factura); acá solo viven los endpoints de lectura y el
# registro de pagos a proveedor. No hay endpoint para crear una CxC a mano
# (siempre nace de una OT lista o entregada) ni para editar su monto (queda
# "congelado" en ese momento, ver comentario en operativo.py).
#
# El taller cobra en dos momentos: un anticipo (por defecto 50%,
# ANTICIPO_PORCENTAJE_DEFECTO) apenas se aprueba el presupuesto y el auto
# entra a reparación, y el resto al entregar el vehículo. Por eso Finanzas
# separa la Cuentas por Cobrar en dos vistas:
#   - "CxC por abonos" (finanzas_listar_cxc_abonos): OTs en reparación o más
#     adelante (pero todavía no 'listo'/'entregado') que no han alcanzado el
#     anticipo pactado. Es una consulta EN VIVO contra ordenes_trabajo — a
#     diferencia de la CxC de abajo, todavía no existe una fila "congelada"
#     en cuentas_por_cobrar en esta etapa.
#   - "CxC OTs cerradas" (finanzas_listar_cxc, sin cambios de nombre en el
#     código): la tabla cuentas_por_cobrar de siempre, que ahora se genera
#     desde que la orden llega a 'listo' (no solo 'entregado') — ver
#     ESTADOS_QUE_CIERRAN_CXC en operativo.py.
# ---------------------------------------------------------------------------

def _cxc_total_pagado(db, orden_id):
    return db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = ?", (orden_id,)
    ).fetchone()[0]


def _orden_valor_total(orden_row):
    """Mismo cálculo que _orden_dict/_generar_o_actualizar_cxc en
    modulos/operativo.py (valor_neto + IVA si corresponde) — se repite acá
    en vez de importarlo porque core.py no puede depender de operativo.py
    y viceversa (ver comentario de imports arriba de este archivo)."""
    valor_neto = orden_row["precio_referencial"] or 0
    valor_iva_monto = round(valor_neto * IVA_PORCENTAJE, 2) if orden_row["precio_iva"] else 0.0
    return round(valor_neto + valor_iva_monto, 2)


@bp.route("/api/finanzas/cxc", methods=["GET"])
def finanzas_listar_cxc():
    """Lista las Cuentas por Cobrar "cerradas" (una por OT que llegó a
    'listo' o 'entregado' con precio > 0), con datos del cliente/equipo y
    el saldo pendiente calculado en vivo a partir de pagos_orden (ver
    _recalcular_estado_cxc en modulos/operativo.py, que ya mantiene
    'estado' al día — acá solo se agrega el número exacto de saldo)."""
    db = get_db()
    estado = request.args.get("estado", "").strip()
    q = request.args.get("q", "").strip()

    sql = """
        SELECT cxc.*, c.nombre AS cliente_nombre, c.telefono AS cliente_telefono,
               o.fecha_recepcion, o.fecha_entrega_real, o.mecanico, o.estado AS orden_estado
        FROM cuentas_por_cobrar cxc
        JOIN clientes c ON c.id = cxc.cliente_id
        JOIN ordenes_trabajo o ON o.id = cxc.orden_id
        WHERE 1=1
    """
    params = []
    if estado:
        sql += " AND cxc.estado = ?"
        params.append(estado)
    sql += " ORDER BY cxc.fecha_vencimiento ASC, cxc.id DESC"
    rows = db.execute(sql, params).fetchall()

    cuentas = []
    for r in rows:
        d = dict(r)
        total_pagado = _cxc_total_pagado(db, d["orden_id"])
        d["total_pagado"] = total_pagado
        d["saldo"] = round(d["monto_total"] - total_pagado, 2)
        cuentas.append(d)

    if q:
        cuentas = [c for c in cuentas if coincide_busqueda(q, c.get("cliente_nombre"), c.get("cliente_telefono"))]

    total_pendiente = round(sum(c["saldo"] for c in cuentas if c["estado"] != "pagada"), 2)
    return jsonify({"cuentas": cuentas, "total": len(cuentas), "total_pendiente": total_pendiente})


@bp.route("/api/finanzas/cxc/abonos", methods=["GET"])
def finanzas_listar_cxc_abonos():
    """Lista las OTs que ya tienen el presupuesto aprobado (están en
    reparación o más adelante, ver ESTADOS_CXC_ABONO) y todavía no alcanzan
    el anticipo pactado (ANTICIPO_PORCENTAJE_DEFECTO del valor total). En
    cuanto lo alcanzan, salen de esta vista — el saldo restante se sigue
    cobrando más adelante, cuando la orden llegue a 'listo'/'entregado' y
    aparezca en finanzas_listar_cxc ("OTs cerradas") por el saldo total
    real (que ya descuenta lo abonado acá, porque ambas vistas leen los
    mismos pagos_orden)."""
    db = get_db()
    q = request.args.get("q", "").strip()
    placeholders = ", ".join("?" for _ in ESTADOS_CXC_ABONO)
    rows = db.execute(
        f"""
        SELECT o.*, c.nombre AS cliente_nombre, c.telefono AS cliente_telefono,
               e.identificador AS equipo_identificador, e.marca AS equipo_marca, e.modelo AS equipo_modelo
        FROM ordenes_trabajo o
        JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN equipos e ON e.id = o.equipo_id
        WHERE o.estado IN ({placeholders})
        ORDER BY o.fecha_recepcion ASC
        """,
        list(ESTADOS_CXC_ABONO),
    ).fetchall()

    cuentas = []
    for r in rows:
        d = dict(r)
        valor_total = _orden_valor_total(d)
        if valor_total <= 0:
            continue
        anticipo_esperado = round(valor_total * ANTICIPO_PORCENTAJE_DEFECTO / 100, 2)
        total_pagado = _cxc_total_pagado(db, d["id"])
        if total_pagado >= anticipo_esperado:
            continue
        d["orden_id"] = d["id"]
        d["valor_total"] = valor_total
        d["anticipo_esperado"] = anticipo_esperado
        d["total_pagado"] = total_pagado
        d["saldo_anticipo"] = round(anticipo_esperado - total_pagado, 2)
        cuentas.append(d)

    if q:
        cuentas = [c for c in cuentas if coincide_busqueda(q, c.get("cliente_nombre"), c.get("cliente_telefono"))]

    total_pendiente = round(sum(c["saldo_anticipo"] for c in cuentas), 2)
    return jsonify({"cuentas": cuentas, "total": len(cuentas), "total_pendiente": total_pendiente})


@bp.route("/api/finanzas/cxp", methods=["POST"])
@requiere_rol("finanzas")
def finanzas_crear_cxp_manual():
    """Crea una Cuenta por Pagar 'a mano', sin pasar por una factura de
    compra de inventario — para gastos del taller que no son repuestos
    (arriendo, servicios, contador, etc.) pero igual hay que trackear como
    deuda a un tercero con su vencimiento, en vez de perderlos en Caja
    Chica (que es solo para gastos ya pagados en el momento). factura_id
    queda NULL: eso es justamente lo que distingue una CxP manual de una
    generada por _generar_cxp_de_factura (ver app.py), y lo que le permite
    a este mismo endpoint eventualmente anularse (ver
    finanzas_anular_cxp_manual) sin tocar ninguna factura ni stock."""
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    concepto = (data.get("concepto") or "").strip().upper()
    if not concepto:
        return jsonify({"error": "Describe el concepto de este gasto (ej. 'Arriendo julio', 'Contador')"}), 400

    try:
        monto_total = round(float(data.get("monto_total")), 2)
        if monto_total <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número mayor a 0"}), 400

    proveedor_id = None
    if data.get("proveedor_id") not in (None, ""):
        try:
            proveedor_id = int(data.get("proveedor_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Proveedor inválido"}), 400
        if db.execute("SELECT id FROM proveedores WHERE id = ?", (proveedor_id,)).fetchone() is None:
            return jsonify({"error": "El proveedor indicado no existe"}), 400

    fecha_emision = (data.get("fecha_emision") or "").strip() or now_iso()
    fecha_vencimiento = (data.get("fecha_vencimiento") or "").strip() or None

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO cuentas_por_pagar
            (proveedor_id, factura_id, concepto, monto_total, fecha_emision, fecha_vencimiento, estado, usuario, creado_en, actualizado_en)
        VALUES (?, NULL, ?, ?, ?, ?, 'pendiente', ?, ?, ?)
        """,
        (proveedor_id, concepto, monto_total, fecha_emision, fecha_vencimiento, usuario_actual(), ts, ts),
    )
    db.commit()

    nueva = db.execute("SELECT * FROM cuentas_por_pagar WHERE id = ?", (cur.lastrowid,)).fetchone()
    d = dict(nueva)
    d["total_pagado"] = 0
    d["saldo"] = d["monto_total"]
    return jsonify(d), 201


@bp.route("/api/finanzas/cxp/<int:cxp_id>/anular", methods=["POST"])
@requiere_rol("finanzas")
def finanzas_anular_cxp_manual(cxp_id):
    """Anula una CxP creada a mano (factura_id NULL) que todavía no tiene
    ningún abono registrado — para poder corregir un gasto cargado por
    error sin dejar un número de deuda fantasma dando vueltas en los
    reportes. Deliberadamente NO permite anular una CxP generada desde una
    factura de compra (esa nace ligada al stock que ya entró a bodega;
    corregirla es un problema de inventario, no de Finanzas) ni una que ya
    tiene abonos (anularla ahí perdería el rastro de esa plata que ya
    salió — hay que revertir el pago primero, no hay endpoint para eso
    todavía)."""
    db = get_db()
    cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE id = ?", (cxp_id,)).fetchone()
    if cxp is None:
        return jsonify({"error": "Cuenta por pagar no encontrada"}), 404
    if cxp["factura_id"] is not None:
        return jsonify({"error": "Esta cuenta por pagar viene de una factura de compra y no se puede anular desde acá."}), 400
    if cxp["estado"] == "anulada":
        return jsonify({"error": "Esta cuenta por pagar ya está anulada"}), 400
    total_pagado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (cxp_id,)
    ).fetchone()[0]
    if total_pagado > 0:
        return jsonify({"error": "Esta cuenta por pagar ya tiene abonos registrados y no se puede anular."}), 400

    db.execute(
        "UPDATE cuentas_por_pagar SET estado = 'anulada', actualizado_en = ? WHERE id = ?",
        (now_iso(), cxp_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM cuentas_por_pagar WHERE id = ?", (cxp_id,)).fetchone()
    return jsonify(dict(actualizada))


@bp.route("/api/finanzas/cxp", methods=["GET"])
def finanzas_listar_cxp():
    """Lista las Cuentas por Pagar (una por factura de compra registrada),
    con datos del proveedor/factura y el saldo pendiente calculado a
    partir de los abonos en cxp_pagos."""
    db = get_db()
    estado = request.args.get("estado", "").strip()
    q = request.args.get("q", "").strip()

    sql = """
        SELECT cxp.*, p.nombre AS proveedor_nombre, f.numero AS factura_numero
        FROM cuentas_por_pagar cxp
        LEFT JOIN proveedores p ON p.id = cxp.proveedor_id
        LEFT JOIN facturas f ON f.id = cxp.factura_id
        WHERE 1=1
    """
    params = []
    if estado:
        sql += " AND cxp.estado = ?"
        params.append(estado)
    sql += " ORDER BY cxp.fecha_vencimiento ASC, cxp.id DESC"
    rows = db.execute(sql, params).fetchall()

    cuentas = []
    for r in rows:
        d = dict(r)
        total_pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (d["id"],)
        ).fetchone()[0]
        d["total_pagado"] = total_pagado
        d["saldo"] = round(d["monto_total"] - total_pagado, 2)
        cuentas.append(d)

    if q:
        cuentas = [c for c in cuentas if coincide_busqueda(q, c.get("proveedor_nombre"), c.get("factura_numero"), c.get("concepto"))]

    total_pendiente = round(sum(c["saldo"] for c in cuentas if c["estado"] != "anulada" and c["estado"] != "pagada"), 2)
    return jsonify({"cuentas": cuentas, "total": len(cuentas), "total_pendiente": total_pendiente})


def _recalcular_estado_cxp(db, cxp_id):
    """Mismo criterio que _recalcular_estado_cxc en modulos/operativo.py
    (pendiente/parcial/pagada), pero a partir de cxp_pagos. No toca una
    CxP que ya esté 'anulada' (esa es una decisión manual, no derivada de
    los pagos)."""
    cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE id = ?", (cxp_id,)).fetchone()
    if cxp is None or cxp["estado"] == "anulada":
        return
    total_pagado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (cxp_id,)
    ).fetchone()[0]
    if total_pagado <= 0:
        nuevo_estado = "pendiente"
    elif total_pagado >= cxp["monto_total"]:
        nuevo_estado = "pagada"
    else:
        nuevo_estado = "parcial"
    if nuevo_estado != cxp["estado"]:
        db.execute(
            "UPDATE cuentas_por_pagar SET estado = ?, actualizado_en = ? WHERE id = ?",
            (nuevo_estado, now_iso(), cxp_id),
        )


@bp.route("/api/finanzas/cxp/<int:cxp_id>/pagos", methods=["POST"])
def finanzas_pagar_cxp(cxp_id):
    """Registra un abono a una Cuenta por Pagar. Si viene 'desde_caja_chica'
    en true, además genera un gasto real en caja_movimientos (categoría
    'Pago a proveedor') para que la salida de plata quede reflejada en el
    saldo de Caja Chica — automatización pedida en el plan del módulo
    financiero ('Pago proveedor → disminuir CxP y registrar egreso').
    cxp_pagos.caja_movimiento_id queda enlazado a ese gasto para poder
    seguir el rastro en ambos sentidos."""
    db = get_db()
    cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE id = ?", (cxp_id,)).fetchone()
    if cxp is None:
        return jsonify({"error": "Cuenta por pagar no encontrada"}), 404
    if cxp["estado"] == "anulada":
        return jsonify({"error": "Esta cuenta por pagar está anulada"}), 400

    data = request.get_json(silent=True) or {}
    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número mayor a 0"}), 400

    saldo_actual = cxp["monto_total"] - db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (cxp_id,)
    ).fetchone()[0]
    if monto > saldo_actual + 0.01:
        return jsonify({"error": f"El monto supera el saldo pendiente (${round(saldo_actual):,})".replace(",", ".")}), 400

    metodo_pago = (data.get("metodo_pago") or "transferencia").strip()
    nota = (data.get("nota") or "").strip().upper()
    desde_caja_chica = bool(data.get("desde_caja_chica"))
    fecha = now_iso()
    ts = fecha

    caja_movimiento_id = None
    if desde_caja_chica:
        proveedor_nombre = ""
        if cxp["proveedor_id"]:
            fila_prov = db.execute("SELECT nombre FROM proveedores WHERE id = ?", (cxp["proveedor_id"],)).fetchone()
            proveedor_nombre = fila_prov["nombre"] if fila_prov else ""
        cur_mov = db.execute(
            """
            INSERT INTO caja_movimientos (
                tipo, fecha, monto, categoria, descripcion, proveedor, usuario, creado_en
            ) VALUES ('gasto', ?, ?, 'Pago a proveedor', ?, ?, ?, ?)
            """,
            (fecha, monto, nota or f"PAGO CXP #{cxp_id}", proveedor_nombre, usuario_actual(), ts),
        )
        caja_movimiento_id = cur_mov.lastrowid

    db.execute(
        """
        INSERT INTO cxp_pagos (cxp_id, monto, fecha, metodo_pago, desde_caja_chica, caja_movimiento_id, nota, usuario, creado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cxp_id, monto, fecha, metodo_pago, 1 if desde_caja_chica else 0, caja_movimiento_id, nota, usuario_actual(), ts),
    )
    _recalcular_estado_cxp(db, cxp_id)
    db.commit()

    actualizada = db.execute("SELECT * FROM cuentas_por_pagar WHERE id = ?", (cxp_id,)).fetchone()
    d = dict(actualizada)
    total_pagado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (cxp_id,)
    ).fetchone()[0]
    d["total_pagado"] = total_pagado
    d["saldo"] = round(d["monto_total"] - total_pagado, 2)
    return jsonify(d), 201


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


# ---------------------------------------------------------------------------
# Reporte "Resumen financiero" — la versión exportable de la pestaña
# Resumen: mismo criterio de ingresos/gastos que /api/finanzas/resumen (ver
# comentario ahí), más el detalle línea por línea de cada pago cobrado y
# cada gasto de caja chica del período, para poder revisar o entregarle el
# número a alguien más sin tener que abrir el sistema.
# ---------------------------------------------------------------------------

def _ingresos_para_reporte(desde, hasta):
    db = get_db()
    sql = """
        SELECT p.*, o.id AS orden_numero, c.nombre AS cliente_nombre
        FROM pagos_orden p
        JOIN ordenes_trabajo o ON o.id = p.orden_id
        JOIN clientes c ON c.id = o.cliente_id
        WHERE 1 = 1
    """
    params = []
    if desde:
        sql += " AND p.fecha >= ?"
        params.append(desde)
    if hasta:
        sql += " AND p.fecha <= ?"
        params.append(hasta + "T23:59:59")
    sql += " ORDER BY p.fecha DESC, p.id DESC"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def _cxc_pendiente_y_vencido(db):
    """Total pendiente de cobro (saldo real, no el 'de papel' de la OT) y
    cuánto de eso ya está vencido a hoy — mismo criterio de saldo que usa
    finanzas_listar_cxc, pero agregado en un solo número para el resumen.
    'Pendiente' acá es 'no pagada por completo' (incluye parcial), no solo
    estado == 'pendiente'."""
    hoy = now_iso()
    filas = db.execute(
        "SELECT id, orden_id, monto_total, fecha_vencimiento FROM cuentas_por_cobrar WHERE estado != 'pagada'"
    ).fetchall()
    pendiente = 0.0
    vencido = 0.0
    for f in filas:
        pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = ?", (f["orden_id"],)
        ).fetchone()[0]
        saldo = f["monto_total"] - pagado
        pendiente += saldo
        if f["fecha_vencimiento"] and f["fecha_vencimiento"] < hoy:
            vencido += saldo
    return round(pendiente, 2), round(vencido, 2)


def _cxp_pendiente_y_vencido(db):
    """Mismo cálculo que _cxc_pendiente_y_vencido, para lo que el taller le
    debe a proveedores y otros terceros (incluye las CxP manuales sin
    factura). Excluye 'anulada' además de 'pagada' — una CxP anulada nunca
    fue una deuda real."""
    hoy = now_iso()
    filas = db.execute(
        "SELECT id, monto_total, fecha_vencimiento FROM cuentas_por_pagar WHERE estado NOT IN ('pagada', 'anulada')"
    ).fetchall()
    pendiente = 0.0
    vencido = 0.0
    for f in filas:
        pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM cxp_pagos WHERE cxp_id = ?", (f["id"],)
        ).fetchone()[0]
        saldo = f["monto_total"] - pagado
        pendiente += saldo
        if f["fecha_vencimiento"] and f["fecha_vencimiento"] < hoy:
            vencido += saldo
    return round(pendiente, 2), round(vencido, 2)


def _resumen_financiero(desde, hasta):
    """Mismo cálculo que finanzas_resumen(), separado en una función aparte
    para que tanto el endpoint JSON como los reportes Excel/PDF usen
    exactamente la misma lógica y nunca puedan mostrar números distintos.

    ingresos/gastos/resultado sí dependen del rango de fechas elegido (son
    flujo de un período). saldo_caja_chica, cxc_pendiente/cxc_vencido y
    cxp_pendiente/cxp_vencido son fotos del momento actual — igual que el
    saldo de caja chica, no tendría sentido un "pendiente de cobro al 30 de
    junio" cuando lo que importa es cuánto falta por cobrar/pagar HOY."""
    db = get_db()

    def _suma_en_rango(sql_base):
        sql = sql_base
        params = []
        if desde:
            sql += " AND fecha >= ?"
            params.append(desde)
        if hasta:
            sql += " AND fecha <= ?"
            params.append(hasta + "T23:59:59")
        return db.execute(sql, params).fetchone()[0]

    ingresos = _suma_en_rango("SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE 1 = 1")
    gastos = _suma_en_rango("SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto'")
    total_aportado = db.execute("SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'aporte'").fetchone()[0]
    total_gastado = db.execute("SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto'").fetchone()[0]

    cxc_pendiente, cxc_vencido = _cxc_pendiente_y_vencido(db)
    cxp_pendiente, cxp_vencido = _cxp_pendiente_y_vencido(db)

    return {
        "ingresos": round(ingresos, 2),
        "gastos": round(gastos, 2),
        "resultado": round(ingresos - gastos, 2),
        "saldo_caja_chica": round(total_aportado - total_gastado, 2),
        "cxc_pendiente": cxc_pendiente,
        "cxc_vencido": cxc_vencido,
        "cxp_pendiente": cxp_pendiente,
        "cxp_vencido": cxp_vencido,
    }


@bp.route("/api/reportes/finanzas-resumen", methods=["GET"])
def reporte_finanzas_resumen():
    desde, hasta = _rango_fechas()
    if not desde and not hasta:
        desde = datetime.now().strftime("%Y-%m") + "-01"
    resumen = _resumen_financiero(desde, hasta)
    ingresos = _ingresos_para_reporte(desde, hasta)
    gastos = _gastos_caja_para_reporte(desde, hasta)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Resumen"
    ws.append(["Período", f"{desde or '(desde el inicio)'} a {hasta or '(hoy)'}"])
    ws.append(["Ingresos cobrados a clientes", round(resumen["ingresos"], 2)])
    ws.append(["Gastos de Caja Chica", round(resumen["gastos"], 2)])
    ws.append(["Resultado del período", round(resumen["resultado"], 2)])
    ws.append(["Saldo actual de Caja Chica", round(resumen["saldo_caja_chica"], 2)])
    ws.append(["Cuentas por Cobrar pendientes (hoy)", round(resumen["cxc_pendiente"], 2)])
    ws.append(["  de las cuales vencidas", round(resumen["cxc_vencido"], 2)])
    ws.append(["Cuentas por Pagar pendientes (hoy)", round(resumen["cxp_pendiente"], 2)])
    ws.append(["  de las cuales vencidas", round(resumen["cxp_vencido"], 2)])
    _autoancho(ws)

    ws2 = wb.create_sheet("Ingresos (pagos)")
    ws2.append(["Fecha", "OT", "Cliente", "Método de pago", "Monto"])
    for p in ingresos:
        ws2.append([p["fecha"], f"#{p['orden_numero']}", p["cliente_nombre"], p["metodo_pago"], round(p["monto"], 2)])
    ws2.append([])
    ws2.append(["", "", "", "Total", round(sum(p["monto"] for p in ingresos), 2)])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    ws3 = wb.create_sheet("Gastos (Caja Chica)")
    ws3.append(["Fecha", "Categoría", "Descripción", "Proveedor", "Monto", "Estado", "Responsable"])
    for g in gastos:
        ws3.append([
            g["fecha"], g["categoria"] or "", g["descripcion"] or "", g["proveedor"] or "",
            round(g["monto"], 2), "Rendido" if g["rendido"] else "Pendiente", g["usuario"] or "",
        ])
    ws3.append([])
    ws3.append(["", "", "", "Total", round(sum(g["monto"] for g in gastos), 2), "", ""])
    _estilo_encabezado(ws3)
    _autoancho(ws3)

    sufijo = f"_{desde or 'inicio'}_a_{hasta or 'hoy'}"
    nombre = f"resumen_financiero{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return _excel_response(wb, nombre)


@bp.route("/api/reportes/finanzas-resumen/pdf", methods=["GET"])
def reporte_finanzas_resumen_pdf():
    desde, hasta = _rango_fechas()
    if not desde and not hasta:
        desde = datetime.now().strftime("%Y-%m") + "-01"
    resumen = _resumen_financiero(desde, hasta)
    ingresos = _ingresos_para_reporte(desde, hasta)
    gastos = _gastos_caja_para_reporte(desde, hasta)

    rango_txt = f" ({desde or '…'} a {hasta or '…'})"
    buffer, doc, elementos, estilos = _pdf_documento(f"Resumen Financiero{rango_txt}")

    _pdf_tabla(
        elementos, estilos, "Resumen",
        ["Ingresos cobrados", "Gastos de Caja Chica", "Resultado del período", "Saldo actual de Caja Chica"],
        [[
            fmt_money_pdf(resumen["ingresos"]), fmt_money_pdf(resumen["gastos"]),
            fmt_money_pdf(resumen["resultado"]), fmt_money_pdf(resumen["saldo_caja_chica"]),
        ]],
    )

    _pdf_tabla(
        elementos, estilos, "Cuentas por Cobrar y por Pagar (a hoy)",
        ["CxC pendiente", "CxC vencida", "CxP pendiente", "CxP vencida"],
        [[
            fmt_money_pdf(resumen["cxc_pendiente"]), fmt_money_pdf(resumen["cxc_vencido"]),
            fmt_money_pdf(resumen["cxp_pendiente"]), fmt_money_pdf(resumen["cxp_vencido"]),
        ]],
    )

    filas_ingresos = [
        [p["fecha"][:10], f"#{p['orden_numero']}", p["cliente_nombre"], p["metodo_pago"], fmt_money_pdf(p["monto"])]
        for p in ingresos
    ]
    _pdf_tabla(
        elementos, estilos, f"Ingresos cobrados a clientes — {fmt_money_pdf(sum(p['monto'] for p in ingresos))}",
        ["Fecha", "OT", "Cliente", "Método de pago", "Monto"],
        filas_ingresos,
    )

    filas_gastos = [
        [g["fecha"][:10], g["categoria"] or "—", g["descripcion"] or "—", fmt_money_pdf(g["monto"]),
         "Rendido" if g["rendido"] else "Pendiente"]
        for g in gastos
    ]
    _pdf_tabla(
        elementos, estilos, f"Gastos de Caja Chica — {fmt_money_pdf(sum(g['monto'] for g in gastos))}",
        ["Fecha", "Categoría", "Descripción", "Monto", "Estado"],
        filas_gastos,
    )

    sufijo = f"_{desde or 'inicio'}_a_{hasta or 'hoy'}"
    nombre = f"resumen_financiero{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)
