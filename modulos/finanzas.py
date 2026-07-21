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
import modulos.operativo as operativo
import modulos.convertidores as convertidores

# Estados de una OT en los que ya se aprobó el presupuesto y el trabajo está
# en curso, pero todavía no llega a 'listo'/'entregado' (ver
# modulos/operativo.py: ESTADOS_QUE_CIERRAN_CXC). Mientras una orden esté en
# uno de estos estados y no haya alcanzado el anticipo pactado
# (ANTICIPO_PORCENTAJE_DEFECTO, hoy 50%), aparece en la pestaña "CxC por
# abonos" de Finanzas — ver finanzas_listar_cxc_abonos más abajo.
ESTADOS_CXC_ABONO = {"reparacion", "esperando_repuesto", "control_calidad"}

# ---------------------------------------------------------------------------
# Medios de pago de Caja Chica (migración 18 en app.py) — antes todo era un
# solo pozo de plata sin distinguir cómo salió; el taller la maneja en
# realidad por 3 caminos separados, cada uno con su propia dinámica:
#   - 'efectivo_cajero': retiro de cajero automático del taller, se contrasta
#     contra boletas y el vuelto que sobra queda disponible para el próximo
#     gasto. Es el default (y el único medio que existía antes de esto).
#   - 'tarjeta_empresa': la tarjeta genera un movimiento en el banco aparte
#     y se rinde con la boleta/factura física.
#   - 'cristian': el encargado de logística. Los trabajadores le pasan
#     boletas de compras propias, él (o idealmente el taller directo) les
#     transfiere para saldar rápido esa cuenta, y después el taller le rinde
#     cuentas a él: puede terminar debiéndole plata o al revés. Por eso es
#     el único medio cuyo saldo tiene sentido en negativo (ver
#     _caja_resumen_medio: negativo = el taller le debe a Cristian).
# El saldo (aportado - gastado) se calcula igual para los 3 -- lo único que
# cambia es cómo se interpreta en la UI (ver finanzas.js).
# ---------------------------------------------------------------------------
MEDIOS_PAGO_CAJA = ("efectivo_cajero", "tarjeta_empresa", "cristian")
MEDIO_PAGO_CAJA_DEFECTO = "efectivo_cajero"

# ---------------------------------------------------------------------------
# Rubros de Gastos de Administración y Ventas (GAV) — migración 19 en
# app.py. Es una clasificación FIJA y separada de 'categoria' (que sigue
# siendo texto libre, para que el taller la use como quiera). rubro_gav
# solo se llena desde este desplegable, y es lo único que el Estado de
# Resultados (ver finanzas_estado_resultados más abajo) suma para armar el
# total de GAV -- un gasto sin rubro_gav asignado queda afuera del total y
# se muestra aparte como "sin clasificar", nunca se adivina su categoría.
# ---------------------------------------------------------------------------
RUBROS_GAV = {
    "generales": "Gastos generales",
    "servicios_basicos": "Servicios básicos (agua, luz, gas, internet)",
    "combustible": "Combustible",
    "bancarios": "Gastos bancarios",
    "mantencion": "Mantención y equipos",
    "otros": "Otros gastos",
    # "Gastos por OT" (Migración 25) es distinto al resto: es un costo
    # atribuible a una orden de trabajo puntual (ej. un repuesto comprado
    # en efectivo para esa OT), no un gasto administrativo general -- por
    # eso, a diferencia de los otros 6 rubros, éste se EXCLUYE del total de
    # GAV en el Estado de Resultados (ver finanzas_estado_resultados más
    # abajo, mismo criterio que ya se usa con las compras de emergencia
    # ligadas a producto_id) y se reporta aparte. Sigue siendo un rubro
    # válido del desplegable para poder clasificarlo y filtrarlo.
    "gastos_por_ot": "Gastos por OT",
}

# Subconjunto de RUBROS_GAV que sí se suma al total de GAV del Estado de
# Resultados -- ver comentario de 'gastos_por_ot' arriba.
RUBROS_GAV_QUE_SUMAN_GAV = {k: v for k, v in RUBROS_GAV.items() if k != "gastos_por_ot"}

bp = Blueprint("finanzas", __name__)


def _rubro_gav_valido(valor):
    """Normaliza y valida un rubro_gav recibido del cliente. Devuelve '' si
    viene vacío (gasto sin clasificar todavía, permitido) o el valor
    normalizado si es uno de los rubros fijos. Lanza ValueError si viene
    algo que no está en la lista -- a diferencia de 'categoria' (texto
    libre de siempre), acá si hay valor tiene que ser uno de los fijos."""
    v = (valor or "").strip().lower()
    if not v:
        return ""
    if v not in RUBROS_GAV:
        raise ValueError(f"Rubro GAV inválido. Debe ser uno de: {', '.join(RUBROS_GAV)}")
    return v


def _orden_id_valido(db, valor):
    """Normaliza y valida el 'orden_id' opcional de un gasto de Caja Chica
    (Migración 25, rubro 'gastos_por_ot'). Devuelve None si viene vacío
    (permitido -- quien crea el gasto puede no saber todavía a qué OT
    corresponde) o el id normalizado si la orden existe. Lanza ValueError
    si viene un valor que no es un número o no corresponde a ninguna OT."""
    v = (valor or "").strip() if isinstance(valor, str) else valor
    if v in (None, ""):
        return None
    try:
        orden_id = int(v)
    except (TypeError, ValueError):
        raise ValueError("La orden de trabajo relacionada debe ser un número de OT válido.")
    existe = db.execute("SELECT 1 FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if not existe:
        raise ValueError(f"No existe una orden de trabajo con el número {orden_id}.")
    return orden_id


def _medio_pago_valido(valor, permitir_vacio=False):
    """Normaliza y valida un medio_pago recibido del cliente. Devuelve el
    valor normalizado, o None si viene vacío y permitir_vacio=True (para
    filtros opcionales). Lanza ValueError con mensaje en español si no es
    uno de los 3 medios reconocidos."""
    v = (valor or "").strip()
    if not v:
        if permitir_vacio:
            return None
        return MEDIO_PAGO_CAJA_DEFECTO
    if v not in MEDIOS_PAGO_CAJA:
        raise ValueError(f"Medio de pago inválido. Debe ser uno de: {', '.join(MEDIOS_PAGO_CAJA)}")
    return v


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


def _caja_resumen_medio(db, medio_pago):
    """Mismo cálculo que antes (aportado/gastado/pendiente/mes), pero
    acotado a un solo medio_pago -- ver comentario de MEDIOS_PAGO_CAJA más
    arriba. saldo_disponible = aportado - gastado; para 'cristian' este
    número puede quedar negativo con sentido (el taller le debe esa
    diferencia), para los otros 2 medios un negativo indicaría que se
    gastó más de lo aportado/retirado, lo cual no debería pasar en uso
    normal pero no se bloquea a nivel de datos (cada gasto se valida solo,
    no contra el saldo -- igual que el comportamiento de siempre)."""
    total_aportado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'aporte' AND medio_pago = ?",
        (medio_pago,),
    ).fetchone()[0]
    total_gastado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND medio_pago = ?",
        (medio_pago,),
    ).fetchone()[0]
    pendiente_rendir = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND rendicion_id IS NULL AND medio_pago = ?",
        (medio_pago,),
    ).fetchone()[0]

    inicio_mes = datetime.now().strftime("%Y-%m") + "-01"
    gastado_mes = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM caja_movimientos WHERE tipo = 'gasto' AND fecha >= ? AND medio_pago = ?",
        (inicio_mes, medio_pago),
    ).fetchone()[0]

    return {
        "saldo_disponible": round(total_aportado - total_gastado, 2),
        "total_aportado": total_aportado,
        "total_gastado": total_gastado,
        "pendiente_rendir": pendiente_rendir,
        "gastado_mes": gastado_mes,
    }


@bp.route("/api/caja/resumen", methods=["GET"])
def caja_resumen():
    """Devuelve el resumen desglosado por medio_pago (antes era un solo
    bloque para toda Caja Chica). El frontend arma 3 tarjetas a partir de
    esto -- ver cargarCajaResumen en finanzas.js."""
    db = get_db()
    return jsonify({medio: _caja_resumen_medio(db, medio) for medio in MEDIOS_PAGO_CAJA})


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
    try:
        medio_pago = _medio_pago_valido(request.args.get("medio_pago", ""), permitir_vacio=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    sql = "SELECT * FROM caja_movimientos WHERE 1=1"
    params = []
    if tipo:
        sql += " AND tipo = ?"
        params.append(tipo)
    if medio_pago:
        sql += " AND medio_pago = ?"
        params.append(medio_pago)
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

    try:
        medio_pago = _medio_pago_valido(data.get("medio_pago"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        rubro_gav = _rubro_gav_valido(data.get("rubro_gav"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # numero_documento y orden_id (Migración 25) quedan OPCIONALES al crear
    # el gasto a propósito: quien lo registra muchas veces solo tiene el
    # retiro del banco (el monto), sin la boleta ni el detalle a mano -- se
    # completan después, al editar el gasto durante la rendición (ver
    # caja_editar_movimiento más abajo).
    numero_documento = (data.get("numero_documento") or "").strip().upper()
    try:
        orden_id = _orden_id_valido(db, data.get("orden_id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

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
            tipo, medio_pago, fecha, monto, categoria, rubro_gav, descripcion, proveedor, usuario,
            comprobante_archivo, producto_id, movimiento_inventario_id, numero_documento, orden_id, creado_en
        ) VALUES ('gasto', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (medio_pago, fecha, monto, categoria, rubro_gav, descripcion, proveedor, usuario_actual(),
         comprobante, producto_id, movimiento_inventario_id, numero_documento, orden_id, ts),
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

    try:
        medio_pago = _medio_pago_valido(data.get("medio_pago"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

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
        INSERT INTO caja_movimientos (tipo, medio_pago, fecha, monto, descripcion, usuario, comprobante_archivo, creado_en)
        VALUES ('aporte', ?, ?, ?, ?, ?, ?, ?)
        """,
        (medio_pago, fecha, monto, descripcion, usuario_actual(), comprobante, ts),
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

    # Si no viene medio_pago en el request, se mantiene el que ya tenía la
    # fila -- a diferencia de crear (donde vacío = 'efectivo_cajero' por
    # defecto), acá vacío NO debe pisar silenciosamente el medio original.
    try:
        medio_pago = _medio_pago_valido(data.get("medio_pago"), permitir_vacio=True) or row["medio_pago"]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    fecha_nueva = (data.get("fecha") or "").strip()
    fecha_final = f"{fecha_nueva}T{datetime.now().strftime('%H:%M:%S')}" if fecha_nueva else row["fecha"]
    descripcion = (data.get("descripcion") or "").strip().upper()

    campos = {"monto": monto, "medio_pago": medio_pago, "fecha": fecha_final, "descripcion": descripcion}
    if row["tipo"] == "gasto":
        campos["categoria"] = (data.get("categoria") or "").strip().upper()
        campos["proveedor"] = (data.get("proveedor") or "").strip().upper()
        # Si no viene rubro_gav en el request, se mantiene el que ya tenía
        # la fila -- mismo criterio que medio_pago un poco más arriba, para
        # no perder la clasificación al editar solo otro campo.
        try:
            rubro_recibido = data.get("rubro_gav")
            campos["rubro_gav"] = _rubro_gav_valido(rubro_recibido) if rubro_recibido is not None else row["rubro_gav"]
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # numero_documento y orden_id (Migración 25): este es el lugar
        # donde normalmente se completan -- quien generó el gasto solo tenía
        # el monto del banco, y quien rinde ya tiene la boleta a la vista.
        # Mismo criterio que rubro_gav arriba: si no viene en el request, se
        # mantiene lo que ya tenía la fila.
        if "numero_documento" in data:
            campos["numero_documento"] = (data.get("numero_documento") or "").strip().upper()
        try:
            orden_recibida = data.get("orden_id")
            campos["orden_id"] = _orden_id_valido(db, orden_recibida) if orden_recibida is not None else row["orden_id"]
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

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
    """Lista gastos sin rendir. Acepta ?medio_pago= para acotar a un solo
    medio -- una rendición solo puede juntar gastos de un mismo medio (ver
    caja_crear_rendicion), así que el modal "Generar rendición" siempre
    manda este filtro."""
    db = get_db()
    try:
        medio_pago = _medio_pago_valido(request.args.get("medio_pago", ""), permitir_vacio=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    sql = "SELECT * FROM caja_movimientos WHERE tipo = 'gasto' AND rendicion_id IS NULL"
    params = []
    if medio_pago:
        sql += " AND medio_pago = ?"
        params.append(medio_pago)
    sql += " ORDER BY fecha ASC"
    rows = db.execute(sql, params).fetchall()
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

    # Todos los gastos de una rendición tienen que ser del mismo medio_pago:
    # una rendición es literalmente "estoy presentando este lote de
    # boletas" para UN medio (ej. "las boletas de la tarjeta empresa de
    # julio", o "lo que rindió Cristian esta semana") -- mezclar medios acá
    # dejaría sin sentido la reposición automática de abajo, que solo puede
    # inyectar el aporte a un saldo a la vez.
    medios_en_seleccion = {g["medio_pago"] for g in gastos}
    if len(medios_en_seleccion) > 1:
        return jsonify({"error": "Todos los gastos de una rendición deben ser del mismo medio de pago"}), 400
    medio_pago = medios_en_seleccion.pop()

    total = sum(g["monto"] for g in gastos)
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO caja_rendiciones (fecha, usuario, total, nota, medio_pago, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, usuario_actual(), total, nota, medio_pago, ts),
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
    # ya quedaron marcados como rendidos. Hereda el medio_pago de la
    # rendición para que el saldo que repone sea el mismo que se gastó.
    if reponer_saldo:
        db.execute(
            """
            INSERT INTO caja_movimientos (tipo, medio_pago, fecha, monto, descripcion, usuario, rendicion_origen_id, creado_en)
            VALUES ('aporte', ?, ?, ?, ?, ?, ?, ?)
            """,
            (medio_pago, ts, total, f"REPOSICIÓN AUTOMÁTICA - RENDICIÓN #{rendicion_id}", usuario_actual(), rendicion_id, ts),
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
@requiere_rol("finanzas")
def finanzas_pagar_cxp(cxp_id):
    """Registra un abono a una Cuenta por Pagar. Si viene 'desde_caja_chica'
    en true, además genera un gasto real en caja_movimientos (categoría
    'Pago a proveedor') para que la salida de plata quede reflejada en el
    saldo de Caja Chica — automatización pedida en el plan del módulo
    financiero ('Pago proveedor → disminuir CxP y registrar egreso').
    cxp_pagos.caja_movimiento_id queda enlazado a ese gasto para poder
    seguir el rastro en ambos sentidos.

    Envuelto en BEGIN IMMEDIATE (_iniciar_transaccion_stock, mismo helper que
    usan aprobar_factura_pendiente_recepcion/eliminar_factura) para cerrar
    una condición de carrera: sin esto, dos abonos casi simultáneos podían
    leer el mismo saldo_actual y ambos pasar la validación, sobrepagando la
    CxP (y duplicando el gasto en Caja Chica si ambos usaban
    desde_caja_chica)."""
    db = get_db()
    _iniciar_transaccion_stock(db)
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


# ---------------------------------------------------------------------------
# Estado de Resultados mensual (migración 19 en app.py)
# ---------------------------------------------------------------------------
# Junta lo que el sistema SÍ sabe con certeza para el mes elegido (ventas de
# Operativo y de Convertidores "cliente_directo", su costo de ventas real, y
# los gastos de Caja Chica ya clasificados por rubro_gav) con los rubros que
# vienen de contabilidad/remuneraciones externas y que nadie más registra acá
# adentro (sueldos, Previred, arriendo, seguros, gastos financieros,
# impuesto, dividendos) -- esos se guardan a mano por mes en
# estado_resultado_manual y se editan desde Finanzas -> Estado de
# Resultados -> "Editar datos del mes". A propósito, nunca se inventa un
# número: lo que no se puede calcular con certeza se deja en 0 hasta que el
# dueño lo cargue, y los gastos de caja chica sin rubro_gav asignado se
# muestran aparte como "sin clasificar" en vez de sumarse a ciegas.

MANUAL_CAMPOS_ESTADO_RESULTADO = (
    "otros_ingresos", "remuneraciones", "bono_remuneraciones", "otras_remuneraciones",
    "previred", "arriendo", "seguros", "gastos_financieros_intereses",
    "gastos_financieros_factoring", "gastos_financieros_comisiones",
    "impuesto_renta", "dividendos",
)


def _mes_valido(valor):
    """Valida el formato 'YYYY-MM'. Devuelve el string tal cual si es
    válido, o lanza ValueError con mensaje en español si no."""
    v = (valor or "").strip()
    try:
        datetime.strptime(v, "%Y-%m")
    except ValueError:
        raise ValueError("El mes debe venir en formato YYYY-MM, ej: 2026-07")
    return v


def _datos_manuales_mes(db, mes):
    """Devuelve los datos manuales guardados para ese mes, o todos en 0 si
    todavía no se ha guardado nada (primera vez que se revisa ese mes)."""
    fila = db.execute("SELECT * FROM estado_resultado_manual WHERE mes = ?", (mes,)).fetchone()
    if fila is None:
        return {campo: 0.0 for campo in MANUAL_CAMPOS_ESTADO_RESULTADO}
    return {campo: fila[campo] or 0.0 for campo in MANUAL_CAMPOS_ESTADO_RESULTADO}


@bp.route("/api/finanzas/estado-resultados", methods=["GET"])
@requiere_rol("finanzas")
def finanzas_estado_resultados():
    db = get_db()
    try:
        mes = _mes_valido(request.args.get("mes") or datetime.now().strftime("%Y-%m"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ot = operativo.ventas_y_costo_ot_mes(db, mes)
    ct = convertidores.ventas_y_costo_ct_mes(db, mes)
    manual = _datos_manuales_mes(db, mes)

    # ---- Ventas
    ventas_ot = ot["monto_cerradas_mes"]
    ventas_ct = ct["ventas_ct"]
    ventas_total = round(ventas_ot + ventas_ct + manual["otros_ingresos"], 2)

    # ---- Costo de ventas
    costo_ot_total = ot["costo_total"]
    costo_ct_total = ct["costo_ct"]
    costo_ventas_total = round(costo_ot_total + costo_ct_total, 2)

    utilidad_bruta = round(ventas_total - costo_ventas_total, 2)
    margen_bruto_pct = round((utilidad_bruta / ventas_total) * 100, 1) if ventas_total else None

    # ---- GAV: gastos de Caja Chica ya clasificados por rubro_gav (nunca
    # incluye producto_id IS NOT NULL -- esas son compras de inventario que
    # ya se cuentan como costo de ventas cuando el repuesto se usa en una
    # OT, ver ventas_y_costo_ot_mes; contarlas también acá sería duplicar
    # el gasto). El rubro "gastos_por_ot" (Migración 25) se excluye del
    # mismo modo y por la misma razón: es un costo de una OT puntual, no un
    # gasto administrativo -- se reporta aparte más abajo
    # ('gastos_por_ot_total'), sin sumarse a gav_total, para no inflar GAV
    # con algo que en realidad es costo asociado a una orden de trabajo.
    filas_gav = db.execute(
        "SELECT rubro_gav, COALESCE(SUM(monto), 0) AS total FROM caja_movimientos "
        "WHERE tipo = 'gasto' AND producto_id IS NULL AND substr(fecha, 1, 7) = ? "
        "AND rubro_gav != '' AND rubro_gav != 'gastos_por_ot' GROUP BY rubro_gav",
        (mes,),
    ).fetchall()
    gav_por_rubro = {fila["rubro_gav"]: round(fila["total"], 2) for fila in filas_gav}
    gav_caja_chica_total = round(sum(gav_por_rubro.values()), 2)

    gastos_por_ot_total = db.execute(
        "SELECT COALESCE(SUM(monto), 0) AS total FROM caja_movimientos "
        "WHERE tipo = 'gasto' AND producto_id IS NULL AND substr(fecha, 1, 7) = ? AND rubro_gav = 'gastos_por_ot'",
        (mes,),
    ).fetchone()["total"] or 0

    gav_sin_clasificar = db.execute(
        "SELECT COALESCE(SUM(monto), 0) AS total FROM caja_movimientos "
        "WHERE tipo = 'gasto' AND producto_id IS NULL AND substr(fecha, 1, 7) = ? AND rubro_gav = ''",
        (mes,),
    ).fetchone()["total"] or 0

    total_remuneraciones = round(
        manual["remuneraciones"] + manual["bono_remuneraciones"]
        + manual["otras_remuneraciones"] + manual["previred"], 2
    )
    gav_total = round(gav_caja_chica_total + total_remuneraciones + manual["arriendo"] + manual["seguros"], 2)

    utilidad_operacional = round(utilidad_bruta - gav_total, 2)

    # ---- Gastos financieros (manual)
    total_gastos_financieros = round(
        manual["gastos_financieros_intereses"] + manual["gastos_financieros_factoring"]
        + manual["gastos_financieros_comisiones"], 2
    )
    utilidad_antes_impuesto = round(utilidad_operacional - total_gastos_financieros, 2)

    utilidad_neta = round(utilidad_antes_impuesto - manual["impuesto_renta"], 2)
    margen_neto_pct = round((utilidad_neta / ventas_total) * 100, 1) if ventas_total else None

    utilidad_retenida = round(utilidad_neta - manual["dividendos"], 2)

    return jsonify({
        "mes": mes,
        "ventas": {
            "ventas_ot": round(ventas_ot, 2),
            "ventas_ct": round(ventas_ct, 2),
            "otros_ingresos": round(manual["otros_ingresos"], 2),
            "total": ventas_total,
        },
        "costo_ventas": {
            "costo_repuestos": ot["costo_repuestos"],
            "costo_mano_obra": ot["costo_mano_obra"],
            "costo_convertidores_internos": ot["costo_convertidores"],
            "costo_ot_total": costo_ot_total,
            "costo_ct_total": costo_ct_total,
            "total": costo_ventas_total,
        },
        "utilidad_bruta": utilidad_bruta,
        "margen_bruto_pct": margen_bruto_pct,
        "gav": {
            "por_rubro": gav_por_rubro,
            "rubros_label": RUBROS_GAV_QUE_SUMAN_GAV,
            "sin_clasificar": round(gav_sin_clasificar, 2),
            "caja_chica_total": gav_caja_chica_total,
            "gastos_por_ot_total": round(gastos_por_ot_total, 2),
            "remuneraciones": manual["remuneraciones"],
            "bono_remuneraciones": manual["bono_remuneraciones"],
            "otras_remuneraciones": manual["otras_remuneraciones"],
            "previred": manual["previred"],
            "total_remuneraciones": total_remuneraciones,
            "arriendo": manual["arriendo"],
            "seguros": manual["seguros"],
            "total": gav_total,
        },
        "utilidad_operacional": utilidad_operacional,
        "gastos_financieros": {
            "intereses": manual["gastos_financieros_intereses"],
            "factoring": manual["gastos_financieros_factoring"],
            "comisiones": manual["gastos_financieros_comisiones"],
            "total": total_gastos_financieros,
        },
        "utilidad_antes_impuesto": utilidad_antes_impuesto,
        "impuesto_renta": manual["impuesto_renta"],
        "utilidad_neta": utilidad_neta,
        "margen_neto_pct": margen_neto_pct,
        "dividendos": manual["dividendos"],
        "utilidad_retenida": utilidad_retenida,
        "manual": manual,
    })


@bp.route("/api/finanzas/estado-resultados/manual", methods=["POST"])
@requiere_rol("finanzas")
def finanzas_estado_resultados_guardar_manual():
    """Guarda (crea o reemplaza) los datos manuales de un mes -- sueldos,
    Previred, arriendo, seguros, gastos financieros, impuesto y dividendos.
    Se sobrescribe completo cada vez (no hay historial de versiones; el
    dueño edita el mismo mes las veces que necesite hasta dejarlo bien)."""
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        mes = _mes_valido(data.get("mes"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    valores = {}
    for campo in MANUAL_CAMPOS_ESTADO_RESULTADO:
        crudo = data.get(campo, 0)
        try:
            monto = float(crudo or 0)
            if monto < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": f"El campo '{campo}' debe ser un número mayor o igual a 0"}), 400
        valores[campo] = monto

    ts = now_iso()
    columnas = ["mes"] + list(MANUAL_CAMPOS_ESTADO_RESULTADO) + ["actualizado_en"]
    placeholders = ", ".join("?" for _ in columnas)
    actualizaciones = ", ".join(f"{c} = excluded.{c}" for c in columnas if c != "mes")
    db.execute(
        f"INSERT INTO estado_resultado_manual ({', '.join(columnas)}) VALUES ({placeholders}) "
        f"ON CONFLICT(mes) DO UPDATE SET {actualizaciones}",
        [mes] + [valores[c] for c in MANUAL_CAMPOS_ESTADO_RESULTADO] + [ts],
    )
    db.commit()
    return jsonify({"ok": True, "mes": mes})


# Líneas "hoja" del Estado de Resultados que se pueden desglosar en el
# detalle transaccional que las compone (a diferencia de las líneas de
# subtotal/total -- Utilidad Bruta, Total GAV, etc. -- que se explican solo
# con aritmética sobre números que el frontend ya tiene, sin pedir nada
# nuevo al servidor).
LINEAS_DETALLE_GAV = {f"gav_{rubro}" for rubro in RUBROS_GAV}
LINEAS_DETALLE_GAV.add("gav_sin_clasificar")
LINEAS_DETALLE_LISTA = {"ventas_ot", "ventas_ct", "costo_repuestos", "costo_mano_obra", "costo_convertidores", "costo_ct"} | LINEAS_DETALLE_GAV
LINEAS_DETALLE_MANUAL = {
    "otros_ingresos": "Ingreso del mes que no viene de una OT ni de un convertidor vendido directo (ej. venta de un servicio puntual, chatarra, etc.) — se ingresa a mano cada mes.",
    "remuneraciones": "Sueldos líquidos pagados este mes — se ingresa a mano, no se registra en ningún otro lugar del sistema.",
    "bono_remuneraciones": "Bonos pagados a trabajadores este mes, aparte del sueldo base — se ingresa a mano.",
    "otras_remuneraciones": "Otros pagos asociados a remuneraciones (ej. finiquitos, horas extra facturadas aparte) — se ingresa a mano.",
    "previred": "Cotizaciones previsionales (AFP, salud, seguro de cesantía) pagadas este mes — se ingresa a mano.",
    "arriendo": "Arriendo del local/taller de este mes — se ingresa a mano.",
    "seguros": "Pólizas de seguro pagadas o devengadas este mes — se ingresa a mano.",
    "gf_intereses": "Intereses de créditos o préstamos de este mes — se ingresa a mano.",
    "gf_factoring": "Costo de factoring (venta de facturas a un tercero) de este mes — se ingresa a mano.",
    "gf_comisiones": "Comisiones bancarias asociadas a financiamiento (no confundir con las comisiones de Caja Chica, que van en GAV) — se ingresa a mano.",
    "impuesto_renta": "Estimación de impuesto a la renta del período — se ingresa a mano (tu contador es la fuente real).",
    "dividendos": "Monto asignado a repartir a los socios/dueños este mes — se ingresa a mano.",
}


@bp.route("/api/finanzas/estado-resultados/detalle", methods=["GET"])
@requiere_rol("finanzas")
def finanzas_estado_resultados_detalle():
    """Desglose transaccional de UNA línea del Estado de Resultados, para
    el panel "¿cómo se llegó a este número?" que se abre al hacer click en
    una fila del reporte. Cada línea "hoja" (ventas, costo de ventas, GAV
    por rubro) devuelve la lista de movimientos que la componen; las líneas
    manuales devuelven solo una explicación (no hay transacciones que
    listar, es un número que el dueño cargó a mano)."""
    db = get_db()
    try:
        mes = _mes_valido(request.args.get("mes"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    linea = (request.args.get("linea") or "").strip()

    if linea in LINEAS_DETALLE_MANUAL:
        return jsonify({"linea": linea, "tipo": "manual", "mensaje": LINEAS_DETALLE_MANUAL[linea]})

    if linea not in LINEAS_DETALLE_LISTA:
        return jsonify({"error": "Línea de detalle no reconocida"}), 400

    items = []

    if linea == "ventas_ot":
        filas = db.execute(
            "SELECT o.id, o.precio_referencial, o.precio_iva, o.fecha_entrega_real, c.nombre AS cliente "
            "FROM ordenes_trabajo o JOIN clientes c ON c.id = o.cliente_id "
            "WHERE o.estado = 'entregado' AND substr(o.fecha_entrega_real, 1, 7) = ? "
            "ORDER BY o.fecha_entrega_real",
            (mes,),
        ).fetchall()
        for f in filas:
            neto = f["precio_referencial"] or 0
            iva_monto = round(neto * IVA_PORCENTAJE, 2) if f["precio_iva"] else 0.0
            items.append({
                "etiqueta": f"OT #{f['id']} — {f['cliente']}",
                "fecha": f["fecha_entrega_real"],
                "monto": round(neto + iva_monto, 2),
            })

    elif linea in ("costo_repuestos", "costo_mano_obra", "costo_convertidores"):
        tipo_item = {"costo_repuestos": "repuesto", "costo_mano_obra": "mano_obra", "costo_convertidores": "convertidor_torque"}[linea]
        _iva_case = f"(CASE WHEN oi.aplica_iva = 1 THEN 1 + {IVA_PORCENTAJE} ELSE 1 END)" if tipo_item == "mano_obra" else "1"
        # Detalle a nivel de ítem (no agrupado por OT), para que se entienda
        # de qué repuesto o trabajo se trata, no solo en qué OT y cliente.
        filas = db.execute(
            f"SELECT o.id AS ot_id, o.fecha_entrega_real, oi.descripcion AS descripcion, "
            f"p.nombre AS producto_nombre, oi.cantidad, "
            f"(oi.cantidad * oi.costo_unitario * {_iva_case}) AS monto "
            f"FROM ordenes_trabajo o "
            f"JOIN orden_items oi ON oi.orden_id = o.id AND oi.tipo = ? "
            f"LEFT JOIN productos p ON p.id = oi.producto_id "
            f"WHERE o.estado = 'entregado' AND substr(o.fecha_entrega_real, 1, 7) = ? "
            f"ORDER BY o.fecha_entrega_real, o.id, oi.id",
            (tipo_item, mes),
        ).fetchall()
        for f in filas:
            nombre_item = (f["producto_nombre"] or f["descripcion"] or "").strip() or "(sin descripción)"
            etiqueta = f"OT #{f['ot_id']} — {nombre_item}"
            cantidad = f["cantidad"] or 0
            if cantidad and cantidad != 1:
                cantidad_txt = f"{cantidad:g}"
                etiqueta += f" (x{cantidad_txt})"
            items.append({"etiqueta": etiqueta, "fecha": f["fecha_entrega_real"], "monto": round(f["monto"], 2)})

    elif linea in ("ventas_ct", "costo_ct"):
        filas = db.execute(
            "SELECT ct.id, ct.marca, ct.modelo, ct.precio_venta, ct.fecha_cierre, c.nombre AS cliente "
            "FROM convertidores_torque ct LEFT JOIN clientes c ON c.id = ct.cliente_id "
            "WHERE ct.origen_tipo = 'cliente_directo' AND ct.estado IN ({}) "
            "AND substr(ct.fecha_cierre, 1, 7) = ? ORDER BY ct.fecha_cierre".format(
                ", ".join("?" for _ in convertidores.ESTADOS_CONVERTIDOR_CERRADOS)
            ),
            list(convertidores.ESTADOS_CONVERTIDOR_CERRADOS) + [mes],
        ).fetchall()
        for f in filas:
            costo_real = convertidores._convertidor_costo_total(db, f["id"]) or 0
            monto = f["precio_venta"] if f["precio_venta"] not in (None, "") else costo_real
            etiqueta = f"Convertidor #{f['id']} — {(f['marca'] or '').strip()} {(f['modelo'] or '').strip()}".strip()
            if f["cliente"]:
                etiqueta += f" ({f['cliente']})"
            items.append({
                "etiqueta": etiqueta, "fecha": f["fecha_cierre"],
                "monto": round((monto if linea == "ventas_ct" else costo_real), 2),
            })

    elif linea in LINEAS_DETALLE_GAV:
        if linea == "gav_sin_clasificar":
            filtro_rubro = "AND rubro_gav = ''"
            params = [mes]
        else:
            filtro_rubro = "AND rubro_gav = ?"
            params = [mes, linea[len("gav_"):]]
        filas = db.execute(
            f"SELECT fecha, categoria, descripcion, proveedor, monto FROM caja_movimientos "
            f"WHERE tipo = 'gasto' AND producto_id IS NULL AND substr(fecha, 1, 7) = ? {filtro_rubro} "
            f"ORDER BY fecha",
            params,
        ).fetchall()
        for f in filas:
            etiqueta = f["descripcion"] or f["categoria"] or "Gasto de Caja Chica"
            if f["proveedor"]:
                etiqueta += f" — {f['proveedor']}"
            items.append({"etiqueta": etiqueta, "fecha": f["fecha"], "monto": round(f["monto"], 2)})

    total = round(sum(i["monto"] for i in items), 2)
    return jsonify({"linea": linea, "tipo": "lista", "items": items, "total": total})
