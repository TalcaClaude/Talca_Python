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


def _caja_bloqueo_motivo(db, row):
    """Explica por qué un gasto/aporte de Caja Chica NO se puede editar ni
    eliminar (para no descuadrar el saldo o la trazabilidad), o None si es
    seguro modificarlo libremente.

    Desde la Migración 26, un gasto (retiro) se bloquea si ALGUNA de sus
    boletas ya fue incluida en una rendición (no todo el retiro tiene que
    estar rendido) -- si se dejara editar el monto o el medio de pago
    después de eso, se descuadraría la rendición ya generada. Boletas
    individuales todavía sin rendir se pueden seguir agregando/editando
    normalmente (ver caja_agregar_boleta/caja_editar_boleta, que revisan la
    boleta puntual, no el gasto completo)."""
    if row["tipo"] == "gasto":
        tiene_boleta_rendida = db.execute(
            "SELECT 1 FROM caja_gasto_detalles WHERE gasto_id = ? AND rendicion_id IS NOT NULL LIMIT 1",
            (row["id"],),
        ).fetchone()
        if tiene_boleta_rendida:
            return "Este retiro tiene boletas ya incluidas en una rendición y no se puede modificar ni eliminar."
    if row["rendicion_origen_id"] is not None:
        return ("Este aporte lo generó automáticamente una rendición y no se puede modificar suelto "
                "(dejaría el saldo descuadrado respecto a las boletas que ya quedaron marcadas como rendidas).")
    if row["movimiento_inventario_id"] is not None:
        return ("Este gasto ingresó stock al inventario (compra de emergencia). "
                "Ajusta el stock manualmente primero si corresponde, y luego edita o elimina el gasto.")
    return None


def _gasto_estado_boletas(db, gasto_id, monto_gasto, cerrado_manualmente):
    """Boletas cargadas contra un gasto (Migración 25) + cuánto de su monto
    ya quedó justificado y cuánto de eso ya fue rendido. Desde la Migración
    26 la Rendición se genera por boleta individual (ver caja_crear_rendicion),
    así que un mismo gasto puede tener boletas ya rendidas y boletas
    todavía pendientes de rendir al mismo tiempo -- por eso ya no existe un
    estado binario "rendido" a nivel de gasto, solo a nivel de boleta
    (boleta['rendido'])."""
    boletas = [dict(r) for r in db.execute(
        "SELECT * FROM caja_gasto_detalles WHERE gasto_id = ? ORDER BY creado_en ASC, id ASC", (gasto_id,)
    ).fetchall()]
    for b in boletas:
        b["tiene_comprobante"] = bool(b.get("comprobante_archivo"))
        b["rendido"] = b.get("rendicion_id") is not None
    monto_justificado = round(sum(b["monto"] for b in boletas), 2)
    monto_pendiente = round(max(monto_gasto - monto_justificado, 0), 2)
    monto_rendido = round(sum(b["monto"] for b in boletas if b["rendido"]), 2)
    monto_por_rendir = round(max(monto_justificado - monto_rendido, 0), 2)
    listo_para_rendir = bool(cerrado_manualmente) or monto_pendiente <= 0.009
    return boletas, monto_justificado, monto_pendiente, monto_rendido, monto_por_rendir, listo_para_rendir


def _caja_movimiento_dict(db, row):
    d = dict(row)
    d["tiene_comprobante"] = bool(d.get("comprobante_archivo"))
    d["bloqueado_motivo"] = _caja_bloqueo_motivo(db, row)
    if d["tipo"] == "gasto":
        boletas, justificado, pendiente, rendido_monto, por_rendir, listo = _gasto_estado_boletas(
            db, d["id"], d["monto"], d.get("cerrado_manualmente")
        )
        d["boletas"] = boletas
        d["monto_justificado"] = justificado
        d["monto_pendiente"] = pendiente
        d["monto_rendido"] = rendido_monto
        d["monto_por_rendir"] = por_rendir
        d["listo_para_rendir"] = listo
        d["cerrado_manualmente"] = bool(d.get("cerrado_manualmente"))
        # "Rendido" a nivel de gasto (para el filtro de la lista de
        # movimientos y los reportes exportables) significa que YA NO
        # QUEDA NADA por hacer con este retiro: todo su monto está
        # justificado con boleta y todas esas boletas ya fueron incluidas
        # en alguna rendición.
        d["rendido"] = pendiente <= 0.009 and por_rendir <= 0.009
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
    # Desde la Migración 26 "pendiente de rendir" es la suma de las BOLETAS
    # ya cargadas (documentadas) que todavía no se incluyeron en ninguna
    # rendición -- es justo lo que aparece seleccionable en el modal
    # "Generar rendición" para este medio de pago. El monto de un retiro que
    # todavía no tiene boleta (monto_pendiente en _gasto_estado_boletas) es
    # un concepto distinto: falta documentarlo, no falta rendirlo.
    pendiente_rendir = db.execute(
        "SELECT COALESCE(SUM(d.monto), 0) FROM caja_gasto_detalles d "
        "JOIN caja_movimientos g ON g.id = d.gasto_id "
        "WHERE d.rendicion_id IS NULL AND g.medio_pago = ?",
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

    # Desde la Migración 25 el proveedor se registra en la boleta
    # (caja_gasto_detalles), no en el gasto/retiro -- por eso este ranking
    # sale de ahí en vez de caja_movimientos.
    gasto_por_proveedor = [
        {"proveedor": r["proveedor"] or "(sin especificar)", "monto": r["monto"]}
        for r in db.execute(
            """
            SELECT COALESCE(NULLIF(proveedor, ''), '(sin especificar)') AS proveedor, SUM(monto) AS monto
            FROM caja_gasto_detalles
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
    movimientos = [_caja_movimiento_dict(db, r) for r in rows]

    # El filtro 'estado' se aplica en Python (no en SQL) porque desde la
    # Migración 26 "rendido" es un cálculo que combina varias boletas de
    # caja_gasto_detalles, no una columna directa de caja_movimientos (ver
    # _caja_movimiento_dict / _gasto_estado_boletas).
    if estado == "pendiente":
        movimientos = [m for m in movimientos if m["tipo"] == "gasto" and not m["rendido"]]
    elif estado == "rendido":
        movimientos = [m for m in movimientos if m["tipo"] == "gasto" and m["rendido"]]

    if q:
        movimientos = [
            m for m in movimientos
            if coincide_busqueda(q, m.get("categoria"), m.get("descripcion"), m.get("proveedor"), m.get("usuario"))
        ]

    movimientos = movimientos[:500]

    return jsonify({"movimientos": movimientos, "total": len(movimientos)})


@bp.route("/api/caja/gasto", methods=["POST"])
def caja_crear_gasto():
    """Crea un "gasto" de Caja Chica -- ojo, esto es apenas el RETIRO (banco,
    tarjeta o vía Cristian), no la rendición: a propósito solo pide monto,
    medio de pago y fecha, porque quien lo registra viendo el banco no tiene
    más información que esa a mano. El número de boleta/factura, el rubro,
    el detalle y la OT relacionada se cargan después, boleta por boleta,
    contra este mismo gasto (ver caja_agregar_boleta más abajo) -- puede
    haber más de una boleta por gasto si el retiro cubrió varias compras."""
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

    fecha = (data.get("fecha") or "").strip()
    fecha = f"{fecha}T{datetime.now().strftime('%H:%M:%S')}" if fecha else now_iso()

    # ---- Compra de emergencia asociada al inventario: caso especial, no es
    # un simple retiro -- quien la registra SÍ sabe en el momento qué compró,
    # así que además de registrar el gasto se ingresa el stock como una
    # entrada normal (igual que una factura), dejando ambos registros
    # enlazados para que se pueda seguir el rastro del dinero al producto.
    # De todas formas, igual que cualquier otro gasto, sigue necesitando su(s)
    # boleta(s) para poder rendirse.
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
        _aplicar_entrada_stock(
            db, producto_row, cantidad, costo_unitario,
            motivo="COMPRA DE EMERGENCIA (CAJA CHICA)",
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
            tipo, medio_pago, fecha, monto, usuario, producto_id, movimiento_inventario_id, creado_en
        ) VALUES ('gasto', ?, ?, ?, ?, ?, ?, ?)
        """,
        (medio_pago, fecha, monto, usuario_actual(), producto_id, movimiento_inventario_id, ts),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_caja_movimiento_dict(db, nuevo)), 201


@bp.route("/api/caja/gasto/<int:gasto_id>/boleta", methods=["POST"])
def caja_agregar_boleta(gasto_id):
    """Agrega una boleta/factura (rendición parcial) contra un gasto ya
    registrado. Puede llamarse varias veces sobre el mismo gasto -- ej. un
    retiro de $200.000 en efectivo que se justifica con 3 boletas de $60.000,
    $90.000 y $50.000. Cada boleta se guarda de inmediato (no se pierde si
    se cierra el navegador). Desde la Migración 26 la Rendición se genera por
    boleta individual, así que siempre se puede seguir agregando boletas
    nuevas a un gasto aunque otras boletas suyas ya se hayan rendido antes
    -- lo único que no se puede tocar es una boleta que YA fue rendida (ver
    caja_editar_boleta/caja_eliminar_boleta)."""
    db = get_db()
    gasto = db.execute("SELECT * FROM caja_movimientos WHERE id = ? AND tipo = 'gasto'", (gasto_id,)).fetchone()
    if gasto is None:
        return jsonify({"error": "Gasto no encontrado"}), 404

    data = request.form
    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto de la boleta debe ser un número mayor a 0"}), 400

    try:
        rubro_gav = _rubro_gav_valido(data.get("rubro_gav"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        orden_id = _orden_id_valido(db, data.get("orden_id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if rubro_gav == "gastos_por_ot" and orden_id is None:
        return jsonify({"error": "Para el rubro \"Gastos por OT\" indica la orden de trabajo relacionada."}), 400

    numero_documento = (data.get("numero_documento") or "").strip().upper()
    categoria = (data.get("categoria") or "").strip().upper()
    proveedor = (data.get("proveedor") or "").strip().upper()
    descripcion = (data.get("descripcion") or "").strip().upper()

    try:
        comprobante = _guardar_comprobante(request.files.get("comprobante"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ts = now_iso()
    db.execute(
        """
        INSERT INTO caja_gasto_detalles (
            gasto_id, numero_documento, monto, rubro_gav, categoria, proveedor, descripcion,
            orden_id, comprobante_archivo, usuario, creado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (gasto_id, numero_documento, monto, rubro_gav, categoria, proveedor, descripcion,
         orden_id, comprobante, usuario_actual(), ts),
    )
    # Si el gasto se había cerrado manualmente (vuelto en caja, boleta chica
    # perdida) y recién ahora aparece otra boleta, se reabre solo -- ya no
    # tiene sentido seguir marcado como "cerrado sin justificar" si se está
    # justificando más.
    db.execute("UPDATE caja_movimientos SET cerrado_manualmente = 0 WHERE id = ?", (gasto_id,))
    db.commit()

    actualizado = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (gasto_id,)).fetchone()
    return jsonify(_caja_movimiento_dict(db, actualizado)), 201


@bp.route("/api/caja/boleta/<int:detalle_id>", methods=["PUT"])
def caja_editar_boleta(detalle_id):
    db = get_db()
    detalle = db.execute("SELECT * FROM caja_gasto_detalles WHERE id = ?", (detalle_id,)).fetchone()
    if detalle is None:
        return jsonify({"error": "Boleta no encontrada"}), 404
    if detalle["rendicion_id"] is not None:
        return jsonify({"error": "Esta boleta ya fue incluida en una rendición y no se puede modificar."}), 409
    gasto = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (detalle["gasto_id"],)).fetchone()

    data = request.form
    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto de la boleta debe ser un número mayor a 0"}), 400

    try:
        rubro_gav = _rubro_gav_valido(data.get("rubro_gav"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        orden_id = _orden_id_valido(db, data.get("orden_id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if rubro_gav == "gastos_por_ot" and orden_id is None:
        return jsonify({"error": "Para el rubro \"Gastos por OT\" indica la orden de trabajo relacionada."}), 400

    campos = {
        "monto": monto,
        "rubro_gav": rubro_gav,
        "orden_id": orden_id,
        "numero_documento": (data.get("numero_documento") or "").strip().upper(),
        "categoria": (data.get("categoria") or "").strip().upper(),
        "proveedor": (data.get("proveedor") or "").strip().upper(),
        "descripcion": (data.get("descripcion") or "").strip().upper(),
    }

    if "comprobante" in request.files and request.files["comprobante"].filename:
        try:
            nuevo_comprobante = _guardar_comprobante(request.files.get("comprobante"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if detalle["comprobante_archivo"]:
            ruta_vieja = os.path.join(COMPROBANTES_DIR, detalle["comprobante_archivo"])
            if os.path.exists(ruta_vieja):
                try:
                    os.remove(ruta_vieja)
                except OSError:
                    pass
        campos["comprobante_archivo"] = nuevo_comprobante

    set_clause = ", ".join(f"{k} = ?" for k in campos)
    db.execute(f"UPDATE caja_gasto_detalles SET {set_clause} WHERE id = ?", (*campos.values(), detalle_id))
    db.commit()

    gasto_actualizado = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (gasto["id"],)).fetchone()
    return jsonify(_caja_movimiento_dict(db, gasto_actualizado))


@bp.route("/api/caja/boleta/<int:detalle_id>", methods=["DELETE"])
def caja_eliminar_boleta(detalle_id):
    db = get_db()
    detalle = db.execute("SELECT * FROM caja_gasto_detalles WHERE id = ?", (detalle_id,)).fetchone()
    if detalle is None:
        return jsonify({"error": "Boleta no encontrada"}), 404
    if detalle["rendicion_id"] is not None:
        return jsonify({"error": "Esta boleta ya fue incluida en una rendición y no se puede eliminar."}), 409
    gasto = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (detalle["gasto_id"],)).fetchone()

    if detalle["comprobante_archivo"]:
        ruta = os.path.join(COMPROBANTES_DIR, detalle["comprobante_archivo"])
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass
    db.execute("DELETE FROM caja_gasto_detalles WHERE id = ?", (detalle_id,))
    db.commit()

    gasto_actualizado = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (gasto["id"],)).fetchone()
    return jsonify(_caja_movimiento_dict(db, gasto_actualizado))


@bp.route("/api/caja/gasto/<int:gasto_id>/cerrar", methods=["PUT"])
def caja_cerrar_gasto(gasto_id):
    """Cierra (o reabre) un gasto manualmente aunque sus boletas no
    justifiquen el 100% del monto -- para el vuelto que queda disponible en
    caja o una boleta chica que se perdió. Es puramente informativo (marca
    'no busco más boletas para este retiro'): no bloquea ni condiciona la
    Rendición, que desde la Migración 26 se genera boleta por boleta."""
    db = get_db()
    gasto = db.execute("SELECT * FROM caja_movimientos WHERE id = ? AND tipo = 'gasto'", (gasto_id,)).fetchone()
    if gasto is None:
        return jsonify({"error": "Gasto no encontrado"}), 404

    data = request.get_json(force=True, silent=True) or {}
    cerrado = bool(data.get("cerrado", True))
    db.execute("UPDATE caja_movimientos SET cerrado_manualmente = ? WHERE id = ?", (1 if cerrado else 0, gasto_id))
    db.commit()

    actualizado = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (gasto_id,)).fetchone()
    return jsonify(_caja_movimiento_dict(db, actualizado))


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
    return jsonify(_caja_movimiento_dict(db, nuevo)), 201


@bp.route("/api/caja/movimiento/<int:mov_id>", methods=["DELETE"])
def caja_eliminar_movimiento(mov_id):
    db = get_db()
    row = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (mov_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Movimiento no encontrado"}), 404

    bloqueo = _caja_bloqueo_motivo(db, row)
    if bloqueo:
        return jsonify({"error": bloqueo}), 409

    if row["comprobante_archivo"]:
        ruta = os.path.join(COMPROBANTES_DIR, row["comprobante_archivo"])
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass

    # Un gasto sin rendicion_id (garantizado por el bloqueo de arriba) puede
    # tener boletas cargadas -- se borran junto con él para no dejar boletas
    # huérfanas apuntando a un gasto inexistente (sus comprobantes también).
    if row["tipo"] == "gasto":
        for d in db.execute("SELECT * FROM caja_gasto_detalles WHERE gasto_id = ?", (mov_id,)).fetchall():
            if d["comprobante_archivo"]:
                ruta = os.path.join(COMPROBANTES_DIR, d["comprobante_archivo"])
                if os.path.exists(ruta):
                    try:
                        os.remove(ruta)
                    except OSError:
                        pass
        db.execute("DELETE FROM caja_gasto_detalles WHERE gasto_id = ?", (mov_id,))

    db.execute("DELETE FROM caja_movimientos WHERE id = ?", (mov_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/caja/movimiento/<int:mov_id>", methods=["PUT"])
def caja_editar_movimiento(mov_id):
    """Editar un gasto o aporte. Desde la Migración 25, un "gasto" (retiro)
    solo tiene monto/medio de pago/fecha propios -- categoría, proveedor,
    rubro, N° de documento, detalle y OT relacionada ahora viven en sus
    boletas (ver caja_editar_boleta), no acá. Usa el mismo candado que
    Eliminar: si ya está rendido, es una reposición automática, o generó un
    ingreso de stock, no se puede tocar — para no descuadrar el saldo ni la
    trazabilidad."""
    db = get_db()
    row = db.execute("SELECT * FROM caja_movimientos WHERE id = ?", (mov_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Movimiento no encontrado"}), 404

    bloqueo = _caja_bloqueo_motivo(db, row)
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
    return jsonify(_caja_movimiento_dict(db, actualizado))


@bp.route("/api/caja/comprobante/<path:nombre_archivo>", methods=["GET"])
def caja_ver_comprobante(nombre_archivo):
    nombre_seguro = secure_filename(nombre_archivo)
    ruta = os.path.join(COMPROBANTES_DIR, nombre_seguro)
    if not os.path.isfile(ruta):
        return jsonify({"error": "Comprobante no encontrado"}), 404
    return send_file(ruta)


@bp.route("/api/caja/pendientes", methods=["GET"])
def caja_gastos_pendientes():
    """Lista los gastos (retiros) que todavía tienen algo por hacer -- les
    falta documentar parte de su monto con una boleta, o ya tienen alguna
    boleta cargada que todavía no se incluyó en ninguna rendición. Acepta
    ?medio_pago= para acotar a un solo medio -- una rendición solo puede
    juntar boletas de un mismo medio (ver caja_crear_rendicion), así que el
    modal "Generar rendición" siempre manda este filtro.

    Desde la Migración 26 un gasto YA NO desaparece de esta lista apenas se
    genera una rendición con alguna de sus boletas: sigue apareciendo
    mientras le quede monto sin boleta (para poder seguir documentándolo) o
    boletas cargadas y aún no rendidas."""
    db = get_db()
    try:
        medio_pago = _medio_pago_valido(request.args.get("medio_pago", ""), permitir_vacio=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    sql = """
        SELECT g.* FROM caja_movimientos g WHERE g.tipo = 'gasto' AND (
            g.monto - COALESCE((SELECT SUM(d.monto) FROM caja_gasto_detalles d WHERE d.gasto_id = g.id), 0) > 0.009
            OR EXISTS (
                SELECT 1 FROM caja_gasto_detalles d
                WHERE d.gasto_id = g.id AND d.rendicion_id IS NULL
            )
        )
    """
    params = []
    if medio_pago:
        sql += " AND g.medio_pago = ?"
        params.append(medio_pago)
    sql += " ORDER BY g.fecha ASC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"gastos": [_caja_movimiento_dict(db, r) for r in rows]})


@bp.route("/api/caja/rendicion", methods=["POST"])
def caja_crear_rendicion():
    """Genera una Rendición a partir de BOLETAS individuales (Migración 26,
    ya cargadas contra algún gasto con caja_agregar_boleta) -- no de gastos
    completos como antes. Así el monto rendido es exactamente lo que se está
    documentando en ese momento: si un retiro de $200.000 solo tiene una
    boleta de $90.000, la rendición queda en $90.000 y los $110.000
    restantes del retiro siguen disponibles para cargarles boleta(s) después
    y rendirlas en otro lote -- no quedan "perdidos" dentro de una rendición
    que en realidad no los documentó."""
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    detalle_ids = data.get("detalle_ids") or []
    nota = (data.get("nota") or "").strip().upper()
    # Por defecto True para no cambiar el comportamiento de siempre. Se pasa
    # False cuando los gastos que se están rindiendo ya se cubrieron antes
    # con un aporte manual — si igual repusiéramos acá, el aporte manual +
    # la reposición automática sumarían el doble de plata de la que
    # realmente entró a la caja.
    reponer_saldo = data.get("reponer_saldo", True)

    if not detalle_ids:
        return jsonify({"error": "Selecciona al menos una boleta para rendir"}), 400

    placeholders = ",".join("?" for _ in detalle_ids)
    boletas = db.execute(
        f"SELECT * FROM caja_gasto_detalles WHERE id IN ({placeholders})",
        detalle_ids,
    ).fetchall()
    if len(boletas) != len(detalle_ids):
        return jsonify({"error": "Alguna de las boletas seleccionadas no existe"}), 400
    if any(b["rendicion_id"] is not None for b in boletas):
        return jsonify({"error": "Alguna de las boletas seleccionadas ya fue rendida antes"}), 400

    gastos_ids = list({b["gasto_id"] for b in boletas})
    gastos_placeholders = ",".join("?" for _ in gastos_ids)
    gastos = {
        g["id"]: g for g in db.execute(
            f"SELECT * FROM caja_movimientos WHERE id IN ({gastos_placeholders})", gastos_ids
        ).fetchall()
    }

    # Todas las boletas de una rendición tienen que ser del mismo medio_pago
    # (heredado del gasto/retiro al que pertenecen): una rendición es
    # literalmente "estoy presentando este lote de boletas" para UN medio
    # (ej. "las boletas de la tarjeta empresa de julio", o "lo que rindió
    # Cristian esta semana") -- mezclar medios acá dejaría sin sentido la
    # reposición automática de abajo, que solo puede inyectar el aporte a un
    # saldo a la vez.
    medios_en_seleccion = {gastos[b["gasto_id"]]["medio_pago"] for b in boletas}
    if len(medios_en_seleccion) > 1:
        return jsonify({"error": "Todas las boletas de una rendición deben ser del mismo medio de pago"}), 400
    medio_pago = medios_en_seleccion.pop()

    total = sum(b["monto"] for b in boletas)
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO caja_rendiciones (fecha, usuario, total, nota, medio_pago, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, usuario_actual(), total, nota, medio_pago, ts),
    )
    rendicion_id = cur.lastrowid

    db.execute(
        f"UPDATE caja_gasto_detalles SET rendicion_id = ? WHERE id IN ({placeholders})",
        [rendicion_id] + detalle_ids,
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
        # Desde la Migración 26 una rendición junta BOLETAS, no gastos
        # completos -- ver caja_crear_rendicion.
        d["n_boletas"] = db.execute(
            "SELECT COUNT(*) FROM caja_gasto_detalles WHERE rendicion_id = ?", (r["id"],)
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
    # Boletas incluidas en esta rendición, con la fecha del retiro (gasto) al
    # que pertenece cada una -- Migración 26, ver caja_crear_rendicion.
    boletas = db.execute(
        "SELECT d.*, g.fecha AS fecha_retiro FROM caja_gasto_detalles d "
        "JOIN caja_movimientos g ON g.id = d.gasto_id "
        "WHERE d.rendicion_id = ? ORDER BY g.fecha ASC, d.id ASC",
        (rendicion_id,),
    ).fetchall()
    d = dict(rendicion)
    boletas_dict = []
    for b in boletas:
        bd = dict(b)
        bd["tiene_comprobante"] = bool(bd.get("comprobante_archivo"))
        boletas_dict.append(bd)
    d["boletas"] = boletas_dict
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
    return [_caja_movimiento_dict(db, r) for r in db.execute(sql, params).fetchall()]


def _boletas_texto(g):
    """Texto compacto con las boletas de un gasto (Migración 25 -- ya no
    tiene categoría/descripción/proveedor propios, esos datos ahora viven
    en sus boletas) para mostrar en los reportes exportables."""
    boletas = g.get("boletas") or []
    if not boletas:
        return "(sin boletas)", ""
    docs = "; ".join(b["numero_documento"] or "(sin N°)" for b in boletas)
    rubros = "; ".join(
        (RUBROS_GAV.get(b["rubro_gav"], b["rubro_gav"]) if b["rubro_gav"] else "Sin clasificar")
        for b in boletas
    )
    return docs, rubros


@bp.route("/api/reportes/caja-gastos", methods=["GET"])
def reporte_caja_gastos():
    desde, hasta = _rango_fechas()
    gastos = _gastos_caja_para_reporte(desde, hasta)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Gastos Caja Chica"
    ws.append(["Fecha", "N° de boleta(s)", "Rubro(s)", "Monto", "Estado", "Responsable"])
    total = 0
    for g in gastos:
        total += g["monto"]
        docs, rubros = _boletas_texto(g)
        ws.append([
            g["fecha"], docs, rubros,
            round(g["monto"], 2), "Rendido" if g["rendido"] else "Pendiente", g["usuario"] or "",
        ])
    ws.append([])
    ws.append(["", "", "", round(total, 2), "", ""])
    _estilo_encabezado(ws)
    _autoancho(ws)

    ws2 = wb.create_sheet("Por rubro GAV")
    ws2.append(["Rubro", "N° de boletas", "Total"])
    por_rubro = {}
    for g in gastos:
        for b in g.get("boletas") or []:
            rubro = RUBROS_GAV.get(b["rubro_gav"], "(sin clasificar)") if b["rubro_gav"] else "(sin clasificar)"
            acc = por_rubro.setdefault(rubro, {"n": 0, "total": 0})
            acc["n"] += 1
            acc["total"] += b["monto"]
    for rubro, acc in sorted(por_rubro.items(), key=lambda kv: kv[1]["total"], reverse=True):
        ws2.append([rubro, acc["n"], round(acc["total"], 2)])
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
    filas = []
    for g in gastos:
        docs, rubros = _boletas_texto(g)
        filas.append([
            g["fecha"][:10], docs or "—", rubros or "—", fmt_money_pdf(g["monto"]),
            "Rendido" if g["rendido"] else "Pendiente", g["usuario"] or "—",
        ])
    _pdf_tabla(
        elementos, estilos, f"Total gastado: {fmt_money_pdf(total)} — {len(gastos)} gasto(s)",
        ["Fecha", "N° de boleta(s)", "Rubro(s)", "Monto", "Estado", "Responsable"],
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
    ws3.append(["Fecha", "N° de boleta(s)", "Rubro(s)", "Monto", "Estado", "Responsable"])
    for g in gastos:
        docs, rubros = _boletas_texto(g)
        ws3.append([
            g["fecha"], docs, rubros,
            round(g["monto"], 2), "Rendido" if g["rendido"] else "Pendiente", g["usuario"] or "",
        ])
    ws3.append([])
    ws3.append(["", "", "", "Total", round(sum(g["monto"] for g in gastos), 2), ""])
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

    filas_gastos = []
    for g in gastos:
        docs, rubros = _boletas_texto(g)
        filas_gastos.append([
            g["fecha"][:10], docs or "—", rubros or "—", fmt_money_pdf(g["monto"]),
            "Rendido" if g["rendido"] else "Pendiente",
        ])
    _pdf_tabla(
        elementos, estilos, f"Gastos de Caja Chica — {fmt_money_pdf(sum(g['monto'] for g in gastos))}",
        ["Fecha", "N° de boleta(s)", "Rubro(s)", "Monto", "Estado"],
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

    # ---- GAV: boletas de Caja Chica ya clasificadas por rubro_gav (Migración
    # 25 -- el rubro vive en la boleta, no en el gasto/retiro). Nunca incluye
    # producto_id IS NOT NULL en el gasto -- esas son compras de inventario
    # que ya se cuentan como costo de ventas cuando el repuesto se usa en una
    # OT, ver ventas_y_costo_ot_mes; contarlas también acá sería duplicar el
    # gasto. El rubro "gastos_por_ot" se excluye del mismo modo y por la
    # misma razón, pero además YA SE CONTÓ arriba: ventas_y_costo_ot_mes
    # (ot["costo_caja_chica"], ver "costo_ventas" en el jsonify de abajo) lo
    # suma como costo de la orden de trabajo a la que quedó ligada esa
    # boleta -- sumarlo también acá lo duplicaría.
    filas_gav = db.execute(
        "SELECT d.rubro_gav AS rubro_gav, COALESCE(SUM(d.monto), 0) AS total "
        "FROM caja_gasto_detalles d JOIN caja_movimientos g ON g.id = d.gasto_id "
        "WHERE g.producto_id IS NULL AND substr(g.fecha, 1, 7) = ? "
        "AND d.rubro_gav != '' AND d.rubro_gav != 'gastos_por_ot' GROUP BY d.rubro_gav",
        (mes,),
    ).fetchall()
    gav_por_rubro = {fila["rubro_gav"]: round(fila["total"], 2) for fila in filas_gav}
    gav_caja_chica_total = round(sum(gav_por_rubro.values()), 2)

    gav_sin_clasificar = db.execute(
        "SELECT COALESCE(SUM(d.monto), 0) AS total FROM caja_gasto_detalles d "
        "JOIN caja_movimientos g ON g.id = d.gasto_id "
        "WHERE g.producto_id IS NULL AND substr(g.fecha, 1, 7) = ? AND d.rubro_gav = ''",
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
            "costo_caja_chica_ot": ot["costo_caja_chica"],
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
        # Migración 25: el rubro/descripción/proveedor viven en la boleta
        # (caja_gasto_detalles), no en el gasto/retiro -- de ahí el JOIN.
        if linea == "gav_sin_clasificar":
            filtro_rubro = "AND d.rubro_gav = ''"
            params = [mes]
        else:
            filtro_rubro = "AND d.rubro_gav = ?"
            params = [mes, linea[len("gav_"):]]
        filas = db.execute(
            f"SELECT g.fecha AS fecha, d.categoria AS categoria, d.descripcion AS descripcion, "
            f"d.proveedor AS proveedor, d.numero_documento AS numero_documento, d.monto AS monto "
            f"FROM caja_gasto_detalles d JOIN caja_movimientos g ON g.id = d.gasto_id "
            f"WHERE g.producto_id IS NULL AND substr(g.fecha, 1, 7) = ? {filtro_rubro} "
            f"ORDER BY g.fecha",
            params,
        ).fetchall()
        for f in filas:
            etiqueta = f["descripcion"] or f["categoria"] or (f"Boleta {f['numero_documento']}" if f["numero_documento"] else "Gasto de Caja Chica")
            if f["proveedor"]:
                etiqueta += f" — {f['proveedor']}"
            items.append({"etiqueta": etiqueta, "fecha": f["fecha"], "monto": round(f["monto"], 2)})

    total = round(sum(i["monto"] for i in items), 2)
    return jsonify({"linea": linea, "tipo": "lista", "items": items, "total": total})
