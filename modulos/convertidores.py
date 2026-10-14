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

Cuatro orígenes posibles para un convertidor, confirmados con el taller:
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
  - 'desde_garantia': igual que 'desde_ot_auto' pero cuando el auto entró
    por una garantía en vez de una OT normal -- garantia_origen_id queda
    apuntando a esa garantía. También es "interno": al llegar a
    'entregado' se carga solo como ítem de esa misma garantía (ver
    _cargar_convertidor_interno_en_garantia_origen), sin cobrarle nada al
    cliente -- es plata que el taller absorbe, igual que cualquier otro
    repuesto/mano de obra de garantía.

Flujo de estados: ingreso -> esperando_aprobacion -> reparacion ->
esperando_repuesto -> listo. Desde 'listo' hay dos salidas: se instala en
la OT de un auto o como repuesto de otro convertidor (pasa
automáticamente a 'instalado', ver agregar_convertidor_a_orden /
agregar_item_convertidor) o se entrega directo al cliente sin instalar
acá (pasa a 'entregado' manualmente). 'cancelado' está disponible como
salida manual desde cualquier paso. (Antes los pasos intermedios se
llamaban 'diagnostico' y 'control_calidad' -- se renombraron a
'esperando_aprobacion' y 'esperando_repuesto' porque describen mejor lo
que de verdad frena la reparación la mayoría de las veces; init_convertidores_db
migra los datos existentes al nombre nuevo.)

Un convertidor puede llevar como "repuesto" a OTRO convertidor de stock:
sirve para el caso de reparar un convertidor (el "madre", que puede ser
interno, externo o de stock) usando piezas de una unidad de stock ya
lista -- ver agregar_item_convertidor cuando tipo == 'convertidor'. La
unidad de stock usada así queda 'instalado' (igual que si se hubiese
instalado en la OT de un auto) y su costo se sigue en vivo dentro del
total de reparación del convertidor madre (no queda "congelado" al
momento de cargarla -- ver _convertidor_costo_estimado /
_convertidor_costo_total). Como el costo del madre incluye el de sus
convertidores-repuesto, y ese total es lo que se carga en la OT del auto
cuando el madre es 'desde_ot_auto' y llega a 'entregado', el costo del
repuesto interno termina reflejado solo, en cascada, en la orden de
trabajo del auto.

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
    requiere_rol,
    requiere_modulo,
    usuario_actual,
)

bp = Blueprint("convertidores", __name__)

# 'convertidor': otro convertidor de stock cargado como repuesto de este
# (ver agregar_item_convertidor) -- no descuenta stock de 'productos', usa
# convertidor_hijo_id en vez de producto_id, y su costo se calcula en vivo
# (no queda fijo al momento de cargarlo, ver _convertidor_costo_estimado).
TIPOS_ITEM = ["repuesto", "mano_obra", "convertidor"]

ORIGENES_CONVERTIDOR = ["cliente_directo", "stock_taller", "desde_ot_auto", "desde_garantia"]
ORIGENES_CONVERTIDOR_LABEL = {
    "cliente_directo": "Cliente trae el convertidor",
    "stock_taller": "Stock del taller",
    "desde_ot_auto": "Desde la caja de un auto (OT existente)",
    "desde_garantia": "Desde una garantía",
}

# 'desde_ot_auto' y 'desde_garantia' son "internos": el convertidor no se
# vende ni se le cobra a nadie por separado -- su costo se carga solo,
# como GASTO, en la OT/garantía a la que pertenece (ver
# _cargar_convertidor_interno_en_ot_origen / _cargar_convertidor_interno_en_garantia_origen).
# Por eso 'precio_venta' NO aplica acá: no generan ingreso directo, solo
# a través de la OT del auto (o de la garantía). Si se le dejara usar
# precio_venta igual que a los orígenes externos (cliente_directo/
# stock_taller), pasarían dos cosas
# malas: (1) ese precio_venta se contaría como si fuera ingreso en algún
# reporte cuando en realidad es 0 -- el único ingreso real es lo que se le
# cobra al cliente en la OT/garantía --, y (2) el costo cargado a la
# OT/garantía quedaría "congelado" en ese precio_venta y dejaría de
# reflejar ítems (ej. mano de obra) agregados después, porque
# _convertidor_costo_estimado preferiría precio_venta sobre el costo real.
ORIGENES_INTERNOS = {"desde_ot_auto", "desde_garantia"}

# Renombrado desde ["ingreso", "diagnostico", "reparacion", "control_calidad",
# "listo", "instalado", "entregado", "cancelado"] -- init_convertidores_db
# migra los datos existentes (diagnostico -> esperando_aprobacion,
# control_calidad -> esperando_repuesto) la primera vez que corre.
ESTADOS_CONVERTIDOR = [
    "ingreso", "esperando_aprobacion", "reparacion", "esperando_repuesto", "listo",
    "instalado", "entregado", "cancelado",
]
ESTADOS_CONVERTIDOR_CERRADOS = {"instalado", "entregado", "cancelado"}
# Único estado desde el que un convertidor se puede vincular como repuesto
# a la OT de un auto o al reparar OTRO convertidor -- ver
# agregar_convertidor_a_orden() / agregar_item_convertidor(). Evita
# instalar una unidad cuya propia reparación todavía no terminó.
ESTADO_CONVERTIDOR_DISPONIBLE = "listo"
ESTADOS_CONVERTIDOR_LABEL = {
    "ingreso": "Ingreso", "esperando_aprobacion": "Esperando aprobación", "reparacion": "Reparación",
    "esperando_repuesto": "Esperando repuesto", "listo": "Listo para instalar",
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
            garantia_origen_id INTEGER REFERENCES garantias(id),
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
            aplica_iva INTEGER NOT NULL DEFAULT 0,
            convertidor_hijo_id INTEGER REFERENCES convertidores_torque(id)
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
            "garantia_origen_id", "marca", "modelo", "numero_serie", "motivo",
            "diagnostico", "responsable", "estado", "precio_venta", "es_regional",
            "fecha_recepcion", "fecha_cierre", "usuario", "creado_en", "actualizado_en",
        ],
    )
    # 'es_regional' se agregó después del primer despliegue del módulo --
    # si la tabla ya existía sin esta columna, se agrega acá en caliente.
    columnas_convertidor = [c[1] for c in db.execute("PRAGMA table_info(convertidores_torque)").fetchall()]
    if "es_regional" not in columnas_convertidor:
        db.execute("ALTER TABLE convertidores_torque ADD COLUMN es_regional INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # Migración de nombres de estados (una sola vez, idempotente): los pasos
    # intermedios se llamaban 'diagnostico' y 'control_calidad' -- se
    # renombraron a 'esperando_aprobacion' y 'esperando_repuesto'. Los
    # convertidores que ya estaban en esos pasos quedan en su equivalente
    # nuevo, sin perder su lugar en el flujo.
    db.execute("UPDATE convertidores_torque SET estado = 'esperando_aprobacion' WHERE estado = 'diagnostico'")
    db.execute("UPDATE convertidores_torque SET estado = 'esperando_repuesto' WHERE estado = 'control_calidad'")
    db.commit()

    # 'convertidor_hijo_id' se agregó para poder cargar OTRO convertidor de
    # stock como repuesto de este (ver agregar_item_convertidor) -- si la
    # tabla ya existía sin esta columna, se agrega acá en caliente.
    columnas_item = [c[1] for c in db.execute("PRAGMA table_info(convertidor_items)").fetchall()]
    if "convertidor_hijo_id" not in columnas_item:
        db.execute("ALTER TABLE convertidor_items ADD COLUMN convertidor_hijo_id INTEGER REFERENCES convertidores_torque(id)")
        db.commit()

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_convertidores_cliente_id ON convertidores_torque (cliente_id);
        CREATE INDEX IF NOT EXISTS idx_convertidores_equipo_id ON convertidores_torque (equipo_id);
        CREATE INDEX IF NOT EXISTS idx_convertidores_estado ON convertidores_torque (estado);
        CREATE INDEX IF NOT EXISTS idx_convertidores_garantia_origen_id ON convertidores_torque (garantia_origen_id);
        CREATE INDEX IF NOT EXISTS idx_convertidor_items_convertidor_id ON convertidor_items (convertidor_id);
        CREATE INDEX IF NOT EXISTS idx_convertidor_items_convertidor_hijo_id ON convertidor_items (convertidor_hijo_id);
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
            aplica_iva INTEGER NOT NULL DEFAULT 0,
            convertidor_hijo_id INTEGER REFERENCES convertidores_torque(id)
        );
        """,
        [
            "id", "convertidor_id", "tipo", "producto_id", "descripcion",
            "cantidad", "costo_unitario", "movimiento_id", "usuario",
            "creado_en", "detalle", "aplica_iva", "convertidor_hijo_id",
        ],
    )
    db.commit()

    # Repara convertidores_torque si su llave foránea garantia_origen_id
    # quedó apuntando a 'garantias_legado...' -- mismo mecanismo que ya
    # mordió a orden_items.convertidor_id (ver el comentario largo en
    # modulos/operativo.py:init_operativo_db). 'garantias' SÍ se
    # reconstruye ocasionalmente (ver _reconstruir_tabla_legada_si_hace_falta
    # más arriba en este archivo), así que esta tabla corre el mismo riesgo
    # apenas exista al menos un convertidor 'desde_garantia'. Se corre en
    # cada arranque; si la llave ya está bien, no hace nada.
    _reparar_fk_hacia_tabla_renombrada(
        db,
        "convertidores_torque",
        sql_convertidores_actual,
        [
            "id", "origen_tipo", "cliente_id", "equipo_id", "orden_origen_id",
            "garantia_origen_id", "marca", "modelo", "numero_serie", "motivo",
            "diagnostico", "responsable", "estado", "precio_venta", "es_regional",
            "fecha_recepcion", "fecha_cierre", "usuario", "creado_en", "actualizado_en",
        ],
    )
    db.commit()

    # Pagos de convertidor, con el mismo método que Talca PHP (pago_ot con
    # ot_relacionada_conver): monto, tipo de pago y fecha contra el
    # convertidor; "pagado" es la suma; quitar un pago NO lo borra, lo
    # desvincula (Talca PHP deja ot_relacionada_conver en NULL). Va en su
    # propia tabla y no en pagos_orden porque ahí orden_id es NOT NULL y
    # reconstruirla tocaría todos los pagos de OT.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS pagos_convertidor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            convertidor_id INTEGER NOT NULL REFERENCES convertidores_torque(id),
            metodo_pago TEXT NOT NULL DEFAULT 'efectivo',
            monto REAL NOT NULL,
            fecha TEXT NOT NULL,
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL,
            desvinculado_en TEXT,
            desvinculado_por TEXT DEFAULT ''
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_pagos_convertidor_conv ON pagos_convertidor (convertidor_id)")
    db.commit()


# ---------------------------------------------------------------------------
# Página del módulo
# ---------------------------------------------------------------------------

@bp.route("/convertidores")
@requiere_modulo("convertidores")
def vista_convertidores():
    return render_template("convertidores.html", modulo_activo="convertidores")


# ---------------------------------------------------------------------------
# Enganche hacia orden_items (OT de un auto) -- llamado desde
# modulos/operativo.py, nunca al revés.
# ---------------------------------------------------------------------------

def _convertidor_costo_total(db, convertidor_id, _visitados=None):
    """Suma de los ítems propios del convertidor (lo que costó repararlo):
    repuestos + mano de obra, más el costo ACTUAL de cualquier convertidor
    de stock cargado como repuesto interno (ítem tipo 'convertidor', ver
    agregar_item_convertidor). Ese costo se sigue en vivo -- no queda fijo
    al momento de cargarlo -- así que si el convertidor de stock cambia
    después (se le agregan más ítems, se le ajusta el precio_venta), el
    total de este convertidor "madre" (y en cascada, el de la OT del auto
    si corresponde) refleja el valor más reciente la próxima vez que se
    consulte.

    Es el costo que se usa por defecto al instalarlo como repuesto en la
    OT de un auto, salvo que el convertidor tenga un 'precio_venta'
    explícito seteado (ver _convertidor_costo_estimado).

    _visitados es una defensa contra un ciclo corrupto en los datos (no
    debería poder darse: un convertidor solo se puede cargar como repuesto
    de otro si está 'listo', y una vez cargado queda 'instalado', por lo
    que no puede volver a cargarse en ningún otro lado -- pero es barato
    prevenir un loop infinito igual)."""
    if _visitados is None:
        _visitados = set()
    if convertidor_id in _visitados:
        return 0
    _visitados = _visitados | {convertidor_id}

    fila = db.execute(
        f"""SELECT COALESCE(SUM(
                cantidad * costo_unitario *
                (CASE WHEN aplica_iva = 1 THEN 1 + {IVA_PORCENTAJE} ELSE 1 END)
            ), 0) AS total
            FROM convertidor_items WHERE convertidor_id = ? AND tipo != 'convertidor'""",
        (convertidor_id,),
    ).fetchone()
    total = fila["total"]

    hijos = db.execute(
        "SELECT convertidor_hijo_id FROM convertidor_items "
        "WHERE convertidor_id = ? AND tipo = 'convertidor' AND convertidor_hijo_id IS NOT NULL",
        (convertidor_id,),
    ).fetchall()
    for h in hijos:
        total += _convertidor_costo_estimado(db, h["convertidor_hijo_id"], _visitados)

    return round(total, 2)


def _convertidor_costo_estimado(db, convertidor_id, _visitados=None):
    """Costo "de venta/instalación" de un convertidor puntual: su
    precio_venta si se cargó uno explícito, si no la suma de sus propios
    ítems (_convertidor_costo_total, que a su vez sigue en vivo el costo
    de cualquier convertidor-repuesto que tenga adentro). Mismo criterio
    en los tres lugares que necesitan "cuánto vale este convertidor ahora
    mismo": instalarlo en la OT de un auto, cargarlo automáticamente en su
    OT/garantía de origen al llegar a 'entregado', y cargarlo como
    repuesto de OTRO convertidor.

    EXCEPCIÓN -- orígenes internos (ver ORIGENES_INTERNOS): acá
    precio_venta NUNCA se usa, aunque tenga un valor cargado (ej. residual
    de un edit viejo) -- siempre se devuelve el costo real en vivo
    (_convertidor_costo_total). Así el monto que se carga a la OT/garantía
    de origen siempre refleja los ítems reales (incluida mano de obra
    agregada después), en vez de quedar "congelado" en un precio de venta
    que no debería existir para un convertidor que no se vende por
    separado."""
    fila = db.execute(
        "SELECT precio_venta, origen_tipo FROM convertidores_torque WHERE id = ?", (convertidor_id,)
    ).fetchone()
    if fila is None:
        return 0
    if fila["origen_tipo"] not in ORIGENES_INTERNOS and fila["precio_venta"] not in (None, ""):
        return fila["precio_venta"]
    return _convertidor_costo_total(db, convertidor_id, _visitados)


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

    costo_unitario = _convertidor_costo_estimado(db, convertidor_id)

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

    # usuario_actual() necesita sesión (contexto de request) -- normal
    # cuando esto se dispara desde editar_convertidor (un PUT real), pero
    # reparar_cargas_internas_pendientes() lo llama al arrancar el
    # servidor, fuera de cualquier request, así que ahí usuario_actual()
    # explota con "Working outside of request context". Con try/except
    # queda cubierto para los dos casos sin tener que cambiar la firma.
    try:
        usuario = usuario_actual()
    except RuntimeError:
        usuario = "Sistema"
    ts = now_iso()
    partes = ["CONVERTIDOR DE TORQUE (REPARACIÓN INTERNA)"]
    if convertidor["marca"]:
        partes.append(convertidor["marca"])
    if convertidor["modelo"]:
        partes.append(convertidor["modelo"])
    if convertidor["numero_serie"]:
        partes.append(f"N/S {convertidor['numero_serie']}")
    descripcion = " ".join(partes).upper()

    costo_unitario = _convertidor_costo_estimado(db, convertidor_id)

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


def _cargar_convertidor_interno_en_garantia_origen(db, convertidor_id):
    """Mismo mecanismo que _cargar_convertidor_interno_en_ot_origen pero
    para convertidores 'desde_garantia' (el auto entró por una garantía en
    vez de una OT normal). Apenas termina la propia reparación del
    convertidor (queda 'entregado'), se carga automáticamente como ítem de
    esa misma garantía (garantia_origen_id) con tipo 'convertidor_torque'
    -- ese costo NO se le cobra al cliente, es plata que el taller absorbe
    como cualquier otro repuesto/mano de obra de garantía (ver
    garantia_items en modulos/operativo.py, que ya lo suma sin distinción
    de tipo dentro de "Costo garantías (mes)").

    Idempotente: si ya hay un garantia_items apuntando a este convertidor,
    no lo vuelve a agregar."""
    convertidor = db.execute(
        "SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)
    ).fetchone()
    if convertidor is None or not convertidor["garantia_origen_id"]:
        return

    ya_cargado = db.execute(
        "SELECT id FROM garantia_items WHERE convertidor_id = ?", (convertidor_id,)
    ).fetchone()
    if ya_cargado:
        return

    garantia_id = convertidor["garantia_origen_id"]
    garantia = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if garantia is None:
        return

    # Mismo motivo que en _cargar_convertidor_interno_en_ot_origen:
    # usuario_actual() necesita sesión, pero reparar_cargas_internas_pendientes()
    # se llama al arrancar el servidor, fuera de cualquier request.
    try:
        usuario = usuario_actual()
    except RuntimeError:
        usuario = "Sistema"
    ts = now_iso()
    partes = ["CONVERTIDOR DE TORQUE (REPARACIÓN INTERNA - GARANTÍA)"]
    if convertidor["marca"]:
        partes.append(convertidor["marca"])
    if convertidor["modelo"]:
        partes.append(convertidor["modelo"])
    if convertidor["numero_serie"]:
        partes.append(f"N/S {convertidor['numero_serie']}")
    descripcion = " ".join(partes).upper()

    costo_unitario = _convertidor_costo_estimado(db, convertidor_id)

    db.execute(
        """
        INSERT INTO garantia_items (
            garantia_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva, convertidor_id
        ) VALUES (?, 'convertidor_torque', NULL, ?, 1, ?, NULL, ?, ?, '', 0, ?)
        """,
        (garantia_id, descripcion, costo_unitario, usuario, ts, convertidor_id),
    )
    db.execute("UPDATE garantias SET actualizado_en = ? WHERE id = ?", (ts, garantia_id))
    db.commit()


def _retirar_carga_interna(db, convertidor_id):
    """Contrapartida de _cargar_convertidor_interno_en_ot_origen /
    _cargar_convertidor_interno_en_garantia_origen: si un convertidor
    'interno' RETROCEDE desde 'entregado' a cualquier otro estado (ej. se
    detectó que la reparación no quedó bien y hay que reabrirlo), el ítem
    que se había auto-cargado en la OT/garantía de origen ya no
    corresponde -- el convertidor todavía no está listo, así que no debería
    seguir sumando como costo cerrado en esa OT/garantía. Se elimina acá
    (en vez de dejarlo con un costo "pegado" del momento en que se cargó);
    cuando el convertidor vuelva a 'entregado' se vuelve a cargar solo, con
    el costo actualizado a ESE momento (ver disparar_carga_interna_ot/
    disparar_carga_interna_garantia en editar_convertidor). Esto es lo que
    evita que un ítem de repuesto/mano de obra agregado después de un
    primer 'entregado' -> retroceso -> 'entregado' se pierda del costo
    final: al retroceder se borra el cargo viejo, y al re-entregar se
    recalcula desde cero con TODOS los ítems que tenga en ese momento."""
    item_ot = db.execute(
        "SELECT id, orden_id FROM orden_items WHERE convertidor_id = ?", (convertidor_id,)
    ).fetchone()
    if item_ot is not None:
        db.execute("DELETE FROM orden_items WHERE id = ?", (item_ot["id"],))
        db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (now_iso(), item_ot["orden_id"]))

    item_garantia = db.execute(
        "SELECT id, garantia_id FROM garantia_items WHERE convertidor_id = ?", (convertidor_id,)
    ).fetchone()
    if item_garantia is not None:
        db.execute("DELETE FROM garantia_items WHERE id = ?", (item_garantia["id"],))
        db.execute("UPDATE garantias SET actualizado_en = ? WHERE id = ?", (now_iso(), item_garantia["garantia_id"]))

    db.commit()


def reparar_cargas_internas_pendientes(db):
    """Reintenta el auto-cargo a la OT/garantía de origen para
    convertidores internos ('desde_ot_auto' o 'desde_garantia') que ya
    llegaron a 'entregado' pero se quedaron sin su ítem correspondiente en
    orden_items/garantia_items -- típicamente porque el intento original
    falló con 'FOREIGN KEY constraint failed' (ver
    modulos/operativo.py:init_operativo_db(), donde se repara la FK de
    orden_items.convertidor_id hacia convertidores_torque justo antes de
    llamar a esta función). El cambio de estado a 'entregado' sí se
    guarda aunque la carga falle -- por eso una unidad puede quedar
    "entregada" en el tablero pero sin su costo reflejado en la OT/garantía
    de origen, y como el auto-cargo solo se dispara en la TRANSICIÓN a
    'entregado' (ver editar_convertidor), una unidad que ya está
    'entregado' no lo vuelve a intentar sola.

    Se llama desde modulos/operativo.py:init_operativo_db(), en cada
    arranque, DESPUÉS de reparar esa FK -- es idempotente (usa el mismo
    chequeo "ya_cargado" que la carga normal), así que no hace nada si ya
    no queda ninguna unidad pendiente."""
    pendientes_ot = db.execute(
        "SELECT id FROM convertidores_torque "
        "WHERE origen_tipo = 'desde_ot_auto' AND estado = 'entregado' AND orden_origen_id IS NOT NULL"
    ).fetchall()
    for fila in pendientes_ot:
        _cargar_convertidor_interno_en_ot_origen(db, fila["id"])

    pendientes_garantia = db.execute(
        "SELECT id FROM convertidores_torque "
        "WHERE origen_tipo = 'desde_garantia' AND estado = 'entregado' AND garantia_origen_id IS NOT NULL"
    ).fetchall()
    for fila in pendientes_garantia:
        _cargar_convertidor_interno_en_garantia_origen(db, fila["id"])


def reparar_costos_congelados_convertidores_internos(db):
    """Corrige tres problemas de datos heredados de una versión anterior
    del código, donde un convertidor 'interno' (desde_ot_auto/
    desde_garantia) SÍ podía usar precio_venta en vez de su costo real de
    ítems, y retroceder desde 'entregado' no retiraba el cargo ya hecho
    (ver _convertidor_costo_estimado y _retirar_carga_interna, ya
    corregidos para que esto no vuelva a pasar):

      1. precio_venta residual: si a una unidad interna le quedó un
         precio_venta cargado (de antes de esta corrección), se limpia acá
         -- ya no se usa para nada, pero dejarlo puesto es confuso (parece
         que el convertidor "se vendió" cuando en realidad su costo real
         está reflejado como gasto en su OT/garantía de origen).

      2. costo_unitario desactualizado en unidades TODAVÍA 'entregado':
         el ítem que se auto-cargó en orden_items/garantia_items guarda un
         costo_unitario FIJO al momento de cargarlo (no se recalcula solo
         en cada lectura). Si ese monto se calculó con el precio_venta
         viejo (bug) en vez del costo real de los ítems, quedó incorrecto.
         Acá se recalcula el costo real actual (_convertidor_costo_total,
         que nunca usa precio_venta) y se actualiza el ítem si no coincide.

      3. cargo huérfano en unidades que YA NO están 'entregado': antes de
         que existiera _retirar_carga_interna, retroceder un convertidor
         desde 'entregado' a otro estado no quitaba el ítem ya cargado en
         la OT/garantía -- quedaba con un costo "pegado" de una reparación
         que en realidad se reabrió. Acá se detecta y se retira (ver
         _retirar_carga_interna); si el convertidor vuelve a 'entregado'
         más adelante, se vuelve a cargar solo con el costo correcto de
         ese momento.

    Se llama desde modulos/operativo.py:init_operativo_db(), en cada
    arranque, después de reparar_cargas_internas_pendientes() -- es
    idempotente (si no hay nada que corregir, no hace nada)."""
    internos = db.execute(
        "SELECT id, estado, origen_tipo, precio_venta FROM convertidores_torque WHERE origen_tipo IN ('desde_ot_auto', 'desde_garantia')"
    ).fetchall()
    ts = now_iso()
    for c in internos:
        if c["precio_venta"] not in (None, ""):
            db.execute(
                "UPDATE convertidores_torque SET precio_venta = NULL, actualizado_en = ? WHERE id = ?",
                (ts, c["id"]),
            )

        if c["estado"] != "entregado":
            # Ya no está entregado -- cualquier cargo que le haya quedado
            # de una entrega anterior ya no corresponde.
            _retirar_carga_interna(db, c["id"])
            continue

        costo_correcto = _convertidor_costo_total(db, c["id"])

        if c["origen_tipo"] == "desde_ot_auto":
            item = db.execute(
                "SELECT id, costo_unitario, orden_id FROM orden_items WHERE convertidor_id = ? ORDER BY id DESC LIMIT 1",
                (c["id"],),
            ).fetchone()
            if item is not None and round(item["costo_unitario"] or 0, 2) != round(costo_correcto, 2):
                db.execute("UPDATE orden_items SET costo_unitario = ? WHERE id = ?", (costo_correcto, item["id"]))
                db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, item["orden_id"]))
        elif c["origen_tipo"] == "desde_garantia":
            item = db.execute(
                "SELECT id, costo_unitario, garantia_id FROM garantia_items WHERE convertidor_id = ? ORDER BY id DESC LIMIT 1",
                (c["id"],),
            ).fetchone()
            if item is not None and round(item["costo_unitario"] or 0, 2) != round(costo_correcto, 2):
                db.execute("UPDATE garantia_items SET costo_unitario = ? WHERE id = ?", (costo_correcto, item["id"]))
                db.execute("UPDATE garantias SET actualizado_en = ? WHERE id = ?", (ts, item["garantia_id"]))
    db.commit()


def revertir_convertidor_al_quitar_item(db, convertidor_id):
    """Libera una unidad de convertidor que había quedado 'instalado' al
    usarla como repuesto -- de un auto o de OTRO convertidor. Se llama
    desde dos lugares: modulos/operativo.py:eliminar_item_orden() (ítem
    tipo 'convertidor_torque' de una OT) y eliminar_item_convertidor() acá
    mismo (ítem tipo 'convertidor' de otro convertidor). No hay stock que
    devolver (el convertidor no vive en 'productos'), pero sí hay que
    liberar la unidad: vuelve a 'listo' para que se pueda volver a usar en
    cualquiera de los dos casos. Solo aplica si estaba 'instalado' (el
    caso 'interno' que se auto-cargó al llegar a 'entregado' se queda en
    'entregado' -- no tiene sentido "liberarlo" porque nunca fue
    instalable en otra OT distinta a la suya propia)."""
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
        if it["tipo"] == "convertidor" and it.get("convertidor_hijo_id"):
            # El costo de un convertidor cargado como repuesto de este se
            # sigue EN VIVO (no queda fijo al momento de cargarlo, ver
            # _convertidor_costo_estimado) -- se recalcula cada vez que se
            # consulta el convertidor madre, así que costo_unitario/subtotal
            # siempre reflejan el valor actual del convertidor-repuesto.
            costo_vivo = _convertidor_costo_estimado(db, it["convertidor_hijo_id"])
            it["costo_unitario"] = costo_vivo
            it["subtotal"] = costo_vivo
            hijo = db.execute(
                "SELECT id, marca, modelo, numero_serie, estado FROM convertidores_torque WHERE id = ?",
                (it["convertidor_hijo_id"],),
            ).fetchone()
            it["convertidor_hijo"] = dict(hijo) if hijo else None
        else:
            it["subtotal"] = _item_subtotal(it)
    d["total_reparacion"] = sum(i["subtotal"] for i in d["items"])
    # Precio a mostrar en la tarjeta del tablero: el precio de venta
    # explícito si se cargó uno, si no, la suma de los ítems propios
    # (mismo criterio que agregar_convertidor_a_orden al instalarlo) --
    # salvo un origen interno (ver ORIGENES_INTERNOS), donde precio_venta
    # nunca aplica y siempre se muestra el costo real de los ítems, mismo
    # criterio que _convertidor_costo_estimado.
    if d["origen_tipo"] in ORIGENES_INTERNOS:
        d["costo_estimado"] = d["total_reparacion"]
    else:
        d["costo_estimado"] = d["precio_venta"] if d["precio_venta"] not in (None, "") else d["total_reparacion"]
    # Ítem de orden_items (si ya está instalado en la OT de un auto) donde
    # quedó registrado — útil para que el detalle del convertidor muestre
    # "instalado en OT #X".
    instalacion = db.execute(
        "SELECT orden_id FROM orden_items WHERE convertidor_id = ? ORDER BY id DESC LIMIT 1", (d["id"],)
    ).fetchone()
    d["instalado_en_orden_id"] = instalacion["orden_id"] if instalacion else None
    # Mismo criterio pero para un convertidor 'desde_garantia' -- el ítem
    # de garantia_items (si ya se auto-cargó) donde quedó registrado.
    carga_garantia = db.execute(
        "SELECT garantia_id FROM garantia_items WHERE convertidor_id = ? ORDER BY id DESC LIMIT 1", (d["id"],)
    ).fetchone()
    d["instalado_en_garantia_id"] = carga_garantia["garantia_id"] if carga_garantia else None
    pagos = db.execute(
        "SELECT * FROM pagos_convertidor WHERE convertidor_id = ? AND desvinculado_en IS NULL "
        "ORDER BY fecha ASC, id ASC", (d["id"],)
    ).fetchall()
    d["pagos"] = [dict(p) for p in pagos]
    d["total_pagado"] = round(sum(p["monto"] for p in pagos), 2)
    # Mismo criterio pero para el otro camino posible: que esta unidad haya
    # quedado cargada como repuesto DENTRO de otro convertidor (ver
    # agregar_item_convertidor). Los dos campos son mutuamente excluyentes
    # en la práctica (una unidad 'instalado' solo pudo consumirse por uno
    # de los dos caminos).
    uso_como_repuesto = db.execute(
        "SELECT convertidor_id FROM convertidor_items WHERE convertidor_hijo_id = ? ORDER BY id DESC LIMIT 1", (d["id"],)
    ).fetchone()
    d["instalado_en_convertidor_id"] = uso_como_repuesto["convertidor_id"] if uso_como_repuesto else None
    return d


@bp.route("/api/convertidores", methods=["POST"])
@requiere_rol("convertidores")
def crear_convertidor():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    origen_tipo = (data.get("origen_tipo") or "").strip().lower()
    if origen_tipo not in ORIGENES_CONVERTIDOR:
        return jsonify({"error": f"Origen inválido. Usa uno de: {', '.join(ORIGENES_CONVERTIDOR)}"}), 400

    cliente_id = None
    equipo_id = None
    orden_origen_id = None
    garantia_origen_id = None

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

    elif origen_tipo == "desde_garantia":
        # Igual que 'desde_ot_auto' pero el auto entró por una garantía --
        # se completan cliente_id/equipo_id automáticamente desde esa
        # garantía, y al llegar a 'entregado' el costo se carga solo como
        # ítem de esa misma garantía (no se le cobra al cliente).
        try:
            garantia_origen_id = int(data.get("garantia_origen_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Debes indicar la garantía de la que sale este convertidor"}), 400
        garantia = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_origen_id,)).fetchone()
        if garantia is None:
            return jsonify({"error": "La garantía indicada no existe"}), 400
        cliente_id = garantia["cliente_id"]
        equipo_id = garantia["equipo_id"]

    # 'stock_taller': sin cliente ni equipo, se repara por cuenta del taller.

    # "De región" solo tiene sentido cuando el cliente trae el convertidor
    # directo (origen externo) -- para stock_taller/desde_ot_auto no hay
    # ningún envío de vuelta involucrado, así que se ignora si viene en el
    # payload para esos casos (evita datos inconsistentes en la tarjeta).
    es_regional = 1 if (origen_tipo == "cliente_directo" and data.get("es_regional")) else 0

    # precio_venta NUNCA aplica a un origen interno (ver ORIGENES_INTERNOS
    # arriba) -- se ignora lo que venga en el payload para esos casos, para
    # que no quede un valor residual que _convertidor_costo_estimado tenga
    # que descartar más tarde.
    precio_venta = None
    if origen_tipo not in ORIGENES_INTERNOS and data.get("precio_venta") not in (None, ""):
        try:
            precio_venta = float(data["precio_venta"])
            # A diferencia de todos los montos de Finanzas (gasto, aporte,
            # pago de CxP, CxP manual), este no exigía > 0 -- se podía
            # cargar un precio de venta en $0 o negativo sin ningún aviso.
            if precio_venta <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "precio_venta debe ser un número mayor a 0"}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO convertidores_torque (
            origen_tipo, cliente_id, equipo_id, orden_origen_id, garantia_origen_id, marca, modelo,
            numero_serie, motivo, diagnostico, responsable, estado, precio_venta,
            es_regional, fecha_recepcion, usuario, creado_en, actualizado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingreso', ?, ?, ?, ?, ?, ?)
        """,
        (
            origen_tipo, cliente_id, equipo_id, orden_origen_id, garantia_origen_id,
            (data.get("marca") or "").strip().upper(),
            (data.get("modelo") or "").strip().upper(),
            (data.get("numero_serie") or "").strip().upper(),
            (data.get("motivo") or "").strip().upper(),
            (data.get("diagnostico") or "").strip().upper(),
            (data.get("responsable") or "").strip().upper(),
            precio_venta,
            es_regional, ts, usuario_actual(), ts, ts,
        ),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_convertidor_dict(db, nuevo)), 201


@bp.route("/api/convertidores", methods=["GET"])
@requiere_rol("convertidores")
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
@requiere_rol("convertidores")
def convertidores_disponibles():
    """Para los dos selectores de 'agregar repuesto' que consumen esto:
    la OT de un auto (modulos/operativo.py) y el ítem 'convertidor' de
    OTRO convertidor (ver agregar_item_convertidor acá mismo). Siempre
    solo las unidades cuya propia reparación ya terminó (estado 'listo').

    ?origen=stock_taller filtra a solo unidades de stock -- lo usa el
    selector de "cargar convertidor de stock como repuesto" de otro
    convertidor, porque ahí no tiene sentido ofrecer una unidad que ya
    tiene cliente/auto propio (cliente_directo/desde_ot_auto).
    ?excluir_id=X saca esa unidad puntual de la lista -- para que un
    convertidor no se pueda ofrecer a sí mismo como su propio repuesto."""
    db = get_db()
    origen = (request.args.get("origen") or "").strip()
    excluir_id = request.args.get("excluir_id")
    sql = "SELECT * FROM convertidores_torque WHERE estado = ?"
    params = [ESTADO_CONVERTIDOR_DISPONIBLE]
    if origen:
        sql += " AND origen_tipo = ?"
        params.append(origen)
    if excluir_id:
        sql += " AND id != ?"
        params.append(excluir_id)
    sql += " ORDER BY actualizado_en DESC"
    rows = db.execute(sql, params).fetchall()
    resultado = []
    for r in rows:
        d = dict(r)
        d["costo_total"] = _convertidor_costo_estimado(db, d["id"])
        resultado.append(d)
    return jsonify({"convertidores": resultado})


@bp.route("/api/convertidores/<int:convertidor_id>", methods=["GET"])
@requiere_rol("convertidores")
def obtener_convertidor(convertidor_id):
    db = get_db()
    row = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    return jsonify(_convertidor_dict(db, row))


@bp.route("/api/convertidores/<int:convertidor_id>", methods=["PUT"])
@requiere_rol("convertidores")
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

    # precio_venta NUNCA aplica a un origen interno (ver ORIGENES_INTERNOS)
    # -- el origen no se puede cambiar después de creado, así que si esta
    # unidad es interna, precio_venta se fuerza a None pase lo que mande
    # el payload (protege contra un valor residual de una versión anterior
    # o un payload manual mal armado).
    if row["origen_tipo"] in ORIGENES_INTERNOS:
        precio_venta = None
    else:
        precio_venta = row["precio_venta"]
        if "precio_venta" in data:
            if data.get("precio_venta") not in (None, ""):
                try:
                    precio_venta = float(data["precio_venta"])
                    if precio_venta <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    return jsonify({"error": "precio_venta debe ser un número mayor a 0"}), 400
            else:
                precio_venta = None

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
    # su propia OT, o de una garantía) se carga solo, como costo, en esa
    # misma OT/garantía apenas termina su propia reparación -- ver
    # _cargar_convertidor_interno_en_ot_origen /
    # _cargar_convertidor_interno_en_garantia_origen. Se detecta ANTES del
    # UPDATE (comparando contra el estado previo) para no disparar el
    # efecto secundario en cada guardado repetido, solo la primera vez que
    # entra a 'entregado'.
    disparar_carga_interna_ot = (
        estado == "entregado"
        and row["estado"] != "entregado"
        and row["origen_tipo"] == "desde_ot_auto"
        and row["orden_origen_id"]
    )
    disparar_carga_interna_garantia = (
        estado == "entregado"
        and row["estado"] != "entregado"
        and row["origen_tipo"] == "desde_garantia"
        and row["garantia_origen_id"]
    )
    # Caso contrario -- RETROCESO: el convertidor estaba 'entregado' (ya
    # cargado como costo en su OT/garantía de origen) y se mueve a
    # cualquier otro estado (se reabre la reparación). El cargo viejo ya no
    # corresponde -- se retira acá (ver _retirar_carga_interna) para que no
    # quede "pegado" con un costo desactualizado; al volver a 'entregado'
    # se recarga solo con el costo real de ese momento.
    retrocede_desde_entregado = (
        estado != "entregado"
        and row["estado"] == "entregado"
        and row["origen_tipo"] in ORIGENES_INTERNOS
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

    if disparar_carga_interna_ot:
        _cargar_convertidor_interno_en_ot_origen(db, convertidor_id)
    if disparar_carga_interna_garantia:
        _cargar_convertidor_interno_en_garantia_origen(db, convertidor_id)
    if retrocede_desde_entregado:
        _retirar_carga_interna(db, convertidor_id)

    actualizado = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    return jsonify(_convertidor_dict(db, actualizado))


@bp.route("/api/convertidores/<int:convertidor_id>", methods=["DELETE"])
@requiere_rol("convertidores")
def eliminar_convertidor(convertidor_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    convertidor = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    if convertidor is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    ya_en_ot = db.execute(
        "SELECT orden_id FROM orden_items WHERE convertidor_id = ? LIMIT 1", (convertidor_id,)
    ).fetchone()
    ya_en_garantia = db.execute(
        "SELECT garantia_id FROM garantia_items WHERE convertidor_id = ? LIMIT 1", (convertidor_id,)
    ).fetchone()
    # Una unidad 'instalado' quedó consumida por uno de tres caminos
    # posibles -- la OT de un auto, una garantía, o el ítem de OTRO
    # convertidor -- ver cuál para dar un mensaje de error que apunte al
    # lugar correcto.
    ya_en_convertidor = db.execute(
        "SELECT convertidor_id FROM convertidor_items WHERE convertidor_hijo_id = ? LIMIT 1", (convertidor_id,)
    ).fetchone()
    if convertidor["estado"] == "instalado" or ya_en_ot is not None or ya_en_garantia is not None or ya_en_convertidor is not None:
        if ya_en_convertidor is not None:
            mensaje = (
                f"Este convertidor ya está cargado como repuesto del convertidor #{ya_en_convertidor['convertidor_id']} "
                "-- elimina primero ese ítem si de verdad quieres deshacerlo."
            )
        elif ya_en_garantia is not None:
            mensaje = (
                f"Este convertidor ya está cargado como ítem en la garantía #{ya_en_garantia['garantia_id']} "
                "-- elimina primero ese ítem desde la garantía correspondiente si de verdad quieres deshacerlo."
            )
        else:
            mensaje = (
                "Este convertidor ya está cargado como ítem en la OT de un auto -- elimina primero ese "
                "ítem desde la orden correspondiente si de verdad quieres deshacerlo."
            )
        return jsonify({"error": mensaje}), 409

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
                recalcular_costo_promedio=False,
            )

    # Si este convertidor tenía a su vez OTRO convertidor de stock cargado
    # como su propio repuesto, hay que liberar esa unidad también -- si no,
    # quedaría 'instalado' para siempre, apuntando a un convertidor que ya
    # no existe.
    items_convertidor = db.execute(
        "SELECT * FROM convertidor_items WHERE convertidor_id = ? AND tipo = 'convertidor' AND convertidor_hijo_id IS NOT NULL",
        (convertidor_id,),
    ).fetchall()
    for item in items_convertidor:
        revertir_convertidor_al_quitar_item(db, item["convertidor_hijo_id"])

    db.execute("DELETE FROM convertidores_torque WHERE id = ?", (convertidor_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/convertidores/<int:convertidor_id>/items", methods=["POST"])
@requiere_rol("convertidores")
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

    if tipo == "convertidor":
        # Una unidad de convertidor no tiene "cantidad" -- siempre es 1.
        cantidad = 1.0
    else:
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
    convertidor_hijo_id = None
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
                if costo_unitario < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return jsonify({"error": "El costo unitario debe ser un número válido y no puede ser negativo"}), 400
        # Mismo criterio que agregar_item_orden() en modulos/operativo.py:
        # el stock de un repuesto se lleva en unidades enteras, así que se
        # rechaza una cantidad fraccionaria en vez de cobrarla exacta y
        # redondear el descuento de stock en silencio (hallazgo #11 del
        # informe de revisión).
        if cantidad != int(cantidad):
            return jsonify({"error": "La cantidad de un repuesto debe ser un número entero (el stock no se descuenta en fracciones de unidad)"}), 400
        cantidad_stock = int(cantidad)
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
            if costo_unitario < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "El costo unitario debe ser un número válido y no puede ser negativo"}), 400
        detalle = (data.get("detalle") or "").strip().upper()
        aplica_iva = 1 if data.get("aplica_iva") else 0

    if tipo == "convertidor":
        # Repara ESTE convertidor usando otro de STOCK como repuesto (ej.
        # el auto trae un convertidor "madre" interno/externo que no
        # sirve y se repara con uno de stock ya listo). El de stock queda
        # 'instalado' -- igual que si se hubiese instalado en la OT de un
        # auto -- y ya no se puede volver a ofrecer en ningún otro lado
        # hasta que se elimine este ítem (ver eliminar_item_convertidor).
        # Su costo NO se congela acá: se sigue en vivo cada vez que se
        # consulta el convertidor madre (ver _convertidor_dict /
        # _convertidor_costo_total), según lo que se definió con el taller.
        try:
            convertidor_hijo_id = int(data.get("convertidor_hijo_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Debes indicar qué convertidor de stock usar como repuesto"}), 400
        if convertidor_hijo_id == convertidor_id:
            return jsonify({"error": "Un convertidor no puede usarse a sí mismo como repuesto"}), 400
        hijo = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_hijo_id,)).fetchone()
        if hijo is None:
            return jsonify({"error": "El convertidor de stock indicado no existe"}), 400
        if hijo["origen_tipo"] != "stock_taller":
            return jsonify({"error": "Solo se puede cargar como repuesto un convertidor de STOCK del taller."}), 400
        if hijo["estado"] != ESTADO_CONVERTIDOR_DISPONIBLE:
            return jsonify({
                "error": f"Ese convertidor de stock está '{ESTADOS_CONVERTIDOR_LABEL.get(hijo['estado'], hijo['estado'])}', "
                         f"solo se puede usar uno que esté '{ESTADOS_CONVERTIDOR_LABEL[ESTADO_CONVERTIDOR_DISPONIBLE]}'."
            }), 409
        costo_unitario = 0  # ignorado al leer -- el costo real se calcula en vivo, ver _convertidor_dict
        if not descripcion:
            partes = ["CONVERTIDOR DE STOCK COMO REPUESTO"]
            if hijo["marca"]:
                partes.append(hijo["marca"])
            if hijo["modelo"]:
                partes.append(hijo["modelo"])
            if hijo["numero_serie"]:
                partes.append(f"N/S {hijo['numero_serie']}")
            descripcion = " ".join(partes).upper()
        db.execute(
            "UPDATE convertidores_torque SET estado = 'instalado', actualizado_en = ? WHERE id = ?",
            (ts, convertidor_hijo_id),
        )

    cur = db.execute(
        """
        INSERT INTO convertidor_items (
            convertidor_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva, convertidor_hijo_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            convertidor_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, ts, detalle, aplica_iva, convertidor_hijo_id,
        ),
    )
    db.execute("UPDATE convertidores_torque SET actualizado_en = ? WHERE id = ?", (ts, convertidor_id))
    db.commit()
    nuevo_item = dict(db.execute("SELECT * FROM convertidor_items WHERE id = ?", (cur.lastrowid,)).fetchone())
    if tipo == "convertidor" and convertidor_hijo_id:
        costo_vivo = _convertidor_costo_estimado(db, convertidor_hijo_id)
        nuevo_item["costo_unitario"] = costo_vivo
        nuevo_item["subtotal"] = costo_vivo
    else:
        nuevo_item["subtotal"] = _item_subtotal(nuevo_item)
    return jsonify(nuevo_item), 201


@bp.route("/api/convertidores/<int:convertidor_id>/items/<int:item_id>", methods=["DELETE"])
@requiere_rol("convertidores")
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
                recalcular_costo_promedio=False,
            )
    elif item["tipo"] == "convertidor" and item["convertidor_hijo_id"]:
        # Libera el convertidor de stock que estaba cargado como repuesto
        # de este -- vuelve a 'listo' para poder usarse de nuevo.
        revertir_convertidor_al_quitar_item(db, item["convertidor_hijo_id"])

    db.execute("DELETE FROM convertidor_items WHERE id = ?", (item_id,))
    db.execute("UPDATE convertidores_torque SET actualizado_en = ? WHERE id = ?", (now_iso(), convertidor_id))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Pagos de convertidor (método de Talca PHP: agrega_pago_f_Conver /
# borrapago_conver_real). Mismos tipos de pago que los de una OT.
# ---------------------------------------------------------------------------

METODOS_PAGO_CONVERTIDOR = ["efectivo", "tarjeta", "transferencia", "cheque", "otro"]  # = operativo.METODOS_PAGO


@bp.route("/api/convertidores/<int:convertidor_id>/pagos", methods=["POST"])
@requiere_rol("convertidores")
def agregar_pago_convertidor(convertidor_id):
    db = get_db()
    if db.execute("SELECT id FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone() is None:
        return jsonify({"error": "Convertidor no encontrado"}), 404
    data = request.get_json(force=True, silent=True) or {}
    try:
        monto = float(data.get("monto"))
        if monto <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número mayor a 0"}), 400
    metodo = (data.get("metodo_pago") or "efectivo").strip().lower()
    if metodo not in METODOS_PAGO_CONVERTIDOR:
        return jsonify({"error": f"Tipo de pago inválido. Debe ser uno de: {', '.join(METODOS_PAGO_CONVERTIDOR)}"}), 400
    fecha = (data.get("fecha") or "").strip() or now_iso()[:10]
    try:
        datetime.fromisoformat(fecha[:10])
    except ValueError:
        return jsonify({"error": "La fecha del pago no es válida (AAAA-MM-DD)"}), 400
    db.execute(
        "INSERT INTO pagos_convertidor (convertidor_id, metodo_pago, monto, fecha, usuario, creado_en) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (convertidor_id, metodo, round(monto, 2), fecha[:10], usuario_actual(), now_iso()),
    )
    db.execute("UPDATE convertidores_torque SET actualizado_en = ? WHERE id = ?", (now_iso(), convertidor_id))
    db.commit()
    row = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (convertidor_id,)).fetchone()
    return jsonify(_convertidor_dict(db, row)), 201


@bp.route("/api/convertidores/<int:convertidor_id>/pagos/<int:pago_id>", methods=["DELETE"])
@requiere_rol("convertidores")
def quitar_pago_convertidor(convertidor_id, pago_id):
    """Como borrapago_conver_real de Talca PHP: el pago no se borra, se
    desvincula del convertidor y deja de sumar en "pagado"."""
    db = get_db()
    pago = db.execute(
        "SELECT id FROM pagos_convertidor WHERE id = ? AND convertidor_id = ? AND desvinculado_en IS NULL",
        (pago_id, convertidor_id),
    ).fetchone()
    if pago is None:
        return jsonify({"error": "Pago no encontrado en este convertidor"}), 404
    db.execute("UPDATE pagos_convertidor SET desvinculado_en = ?, desvinculado_por = ? WHERE id = ?",
               (now_iso(), usuario_actual(), pago_id))
    db.execute("UPDATE convertidores_torque SET actualizado_en = ? WHERE id = ?", (now_iso(), convertidor_id))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def ventas_y_costo_ct_mes(db, prefijo_mes):
    """Ventas y costo de ventas de convertidores 'cliente_directo' (el único
    origen que genera venta propia, ver docstring del módulo más arriba)
    para UN mes cualquiera ('YYYY-MM') -- versión parametrizada del mismo
    cálculo que dashboard_convertidores() hace para "el mes en curso"
    (valor_cerrado_mes / gasto_externos_mes). La usa el Estado de Resultados
    de Finanzas para poder revisar cualquier mes, no solo el actual.

    Los convertidores internos (desde_ot_auto/desde_garantia) NO se incluyen
    acá a propósito -- ver la nota larga en dashboard_convertidores(): su
    costo ya se cuenta como gasto dentro de Operativo/Garantías, incluirlo
    también acá sería contar la misma plata dos veces."""
    rows = db.execute(
        "SELECT id FROM convertidores_torque WHERE origen_tipo = 'cliente_directo' "
        "AND estado IN ({}) AND substr(fecha_cierre, 1, 7) = ?".format(
            ", ".join("?" for _ in ESTADOS_CONVERTIDOR_CERRADOS)
        ),
        list(ESTADOS_CONVERTIDOR_CERRADOS) + [prefijo_mes],
    ).fetchall()

    ventas_ct = 0.0
    costo_ct = 0.0
    for r in rows:
        conv = db.execute("SELECT * FROM convertidores_torque WHERE id = ?", (r["id"],)).fetchone()
        costo_real = _convertidor_costo_total(db, r["id"]) or 0
        precio_venta = conv["precio_venta"] if conv["precio_venta"] not in (None, "") else costo_real
        ventas_ct += precio_venta or 0
        costo_ct += costo_real

    return {"ventas_ct": round(ventas_ct, 2), "costo_ct": round(costo_ct, 2)}


@bp.route("/api/convertidores/dashboard", methods=["GET"])
@requiere_rol("convertidores")
def dashboard_convertidores():
    """Resumen para la pestaña Dashboard del módulo: cuántos hay por
    estado, cuánto valor en reparación hay abierto, cuánto se cerró este
    mes, cuánto costó realmente reparar unidades externas (cliente_directo)
    este mes, cuántos convertidores han pasado por una garantía, cuántos
    "de región" siguen pendientes de despachar de vuelta, y el tiempo
    promedio de reparación de los que se cerraron el mes en curso (mismo
    criterio de "mes en curso" que usa Operativo en su propio dashboard).

    IMPORTANTE -- precio_venta NUNCA se usa para un origen interno
    (ORIGENES_INTERNOS: 'desde_ot_auto'/'desde_garantia'), ni acá ni en
    ningún otro lado del módulo (ver _convertidor_costo_estimado /
    _convertidor_dict): esas unidades no se venden por separado, no
    generan ingreso directo -- su costo real es siempre la suma de sus
    ítems, y ese costo ya se refleja como GASTO en la OT/garantía a la que
    pertenecen. Si se dejara usar precio_venta ahí, se contaría plata que
    nunca existió como ingreso, y el monto cargado a la OT/garantía
    quedaría "congelado" sin reflejar ítems agregados después.

    IMPORTANTE #2 -- valor_cerrado_mes NO incluye convertidores internos
    (desde_ot_auto/desde_garantia) que llegaron a 'entregado' este mes: ese
    costo ya se contabiliza como GASTO en Operativo (costo_convertidores_ots_cerradas)
    o en Garantías (costo_garantias_mes) apenas el convertidor se entrega
    -- son el mismo peso mirado desde el otro lado (gasto ahí, no "valor
    generado" acá). Si se sumara también en esta tarjeta, el mismo monto
    aparecería como algo positivo en Convertidores Y como un costo en
    Operativo/Garantías al mismo tiempo, confundiendo al dueño al comparar
    dashboards (mismo tipo de duplicación que ya se corrigió en
    reporte_excel -- ver ahí el comentario largo). valor_cerrado_interno_mes
    se reporta aparte, solo informativo."""
    db = get_db()
    rows = db.execute("SELECT * FROM convertidores_torque").fetchall()

    por_estado = {estado: 0 for estado in ESTADOS_CONVERTIDOR}
    por_origen = {origen: 0 for origen in ORIGENES_CONVERTIDOR}
    abiertos = 0
    valor_en_reparacion = 0.0
    valor_cerrado_mes = 0.0
    valor_cerrado_interno_mes = 0.0
    gasto_externos_mes = 0.0
    convertidores_en_garantia = 0
    regionales_pendientes = 0
    tiempos_cierre_mes = []
    mes_actual = datetime.now().strftime("%Y-%m")

    for r in rows:
        d = dict(r)
        por_estado[d["estado"]] = por_estado.get(d["estado"], 0) + 1
        por_origen[d["origen_tipo"]] = por_origen.get(d["origen_tipo"], 0) + 1
        if d["origen_tipo"] == "desde_garantia" or d["garantia_origen_id"]:
            convertidores_en_garantia += 1
        es_interno = d["origen_tipo"] in ORIGENES_INTERNOS
        cerrado_este_mes = d["estado"] in ESTADOS_CONVERTIDOR_CERRADOS and (d["fecha_cierre"] or "").startswith(mes_actual)
        if d["estado"] not in ESTADOS_CONVERTIDOR_CERRADOS:
            abiertos += 1
            costo = _convertidor_costo_total(db, d["id"]) if es_interno else (
                d["precio_venta"] if d["precio_venta"] not in (None, "") else _convertidor_costo_total(db, d["id"])
            )
            valor_en_reparacion += costo or 0
        if d["es_regional"] and d["estado"] not in ("entregado", "cancelado"):
            regionales_pendientes += 1
        if cerrado_este_mes:
            costo_cierre = _convertidor_costo_total(db, d["id"]) if es_interno else (
                d["precio_venta"] if d["precio_venta"] not in (None, "") else _convertidor_costo_total(db, d["id"])
            )
            if es_interno:
                # No se suma a valor_cerrado_mes -- ya quedó contabilizado
                # como gasto en Operativo/Garantías (ver docstring). Se
                # reporta aparte, solo para transparencia.
                valor_cerrado_interno_mes += costo_cierre or 0
            else:
                valor_cerrado_mes += costo_cierre or 0
            if d["fecha_recepcion"] and d["fecha_cierre"]:
                try:
                    dt_ini = datetime.fromisoformat(d["fecha_recepcion"][:19])
                    dt_fin = datetime.fromisoformat(d["fecha_cierre"][:19])
                    tiempos_cierre_mes.append((dt_fin - dt_ini).total_seconds() / 86400)
                except ValueError:
                    pass
            if d["origen_tipo"] == "cliente_directo":
                # Cuánto le costó REALMENTE al taller reparar esta unidad
                # externa (repuestos + mano de obra reales, nunca
                # precio_venta) -- para comparar contra lo cobrado y ver el
                # margen real de este servicio, no solo lo cobrado.
                gasto_externos_mes += _convertidor_costo_total(db, d["id"]) or 0

    tiempo_promedio_dias = round(sum(tiempos_cierre_mes) / len(tiempos_cierre_mes), 1) if tiempos_cierre_mes else None

    return jsonify({
        "total": len(rows),
        "abiertos": abiertos,
        "valor_en_reparacion": round(valor_en_reparacion, 2),
        "valor_cerrado_mes": round(valor_cerrado_mes, 2),
        "valor_cerrado_interno_mes": round(valor_cerrado_interno_mes, 2),
        "gasto_externos_mes": round(gasto_externos_mes, 2),
        "convertidores_en_garantia": convertidores_en_garantia,
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
@requiere_rol("convertidores")
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


# ---------------------------------------------------------------------------
# Reporte de OT de convertidores: el de createXLS_ot_convertidor de Talca PHP.
# ABIERTAS se filtran por fecha de recepción; CERRADAS por fecha de cierre;
# "todas" por recepción. Segunda hoja: costos por mes de cierre (como
# "Costos Convertidores" de Talca PHP).
#
# El costo es lo que costó la reparación (total_reparacion), NO costo_estimado:
# para los externos con precio de venta, costo_estimado devuelve ese precio y
# la utilidad saldría siempre 0. "Pagado" es la suma de pagos_convertidor no
# desvinculados (método de Talca PHP).
# ---------------------------------------------------------------------------

ESTADOS_REPORTE_CONVERTIDORES = ("todas", "abiertas", "cerradas")


def _numero_ot_origen(db, orden_id):
    # Mismo criterio que numero_ot de operativo (sin importarlo: no hay imports
    # entre módulos hermanos): el N° de Talca, o L-<id> si la OT es local.
    if not orden_id:
        return ""
    fila = db.execute("SELECT id, talca_id FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if fila is None:
        return f"L-{orden_id}"
    return str(fila["talca_id"]) if fila["talca_id"] is not None else f"L-{fila['id']}"


def _convertidores_para_reporte(desde, hasta, estado):
    db = get_db()
    cerrados = tuple(sorted(ESTADOS_CONVERTIDOR_CERRADOS))
    marcas = ", ".join("?" for _ in cerrados)
    if estado == "cerradas":
        campo = "fecha_cierre"
        sql = f"SELECT * FROM convertidores_torque WHERE estado IN ({marcas})"
        params = list(cerrados)
    elif estado == "abiertas":
        campo = "fecha_recepcion"
        sql = f"SELECT * FROM convertidores_torque WHERE estado NOT IN ({marcas})"
        params = list(cerrados)
    else:
        campo = "fecha_recepcion"
        sql = "SELECT * FROM convertidores_torque WHERE 1=1"
        params = []
    if desde:
        sql += f" AND substr({campo}, 1, 10) >= ?"
        params.append(desde)
    if hasta:
        sql += f" AND substr({campo}, 1, 10) <= ?"
        params.append(hasta)
    sql += f" ORDER BY {campo} DESC, id DESC"
    filas = []
    for r in db.execute(sql, params).fetchall():
        c = _convertidor_dict(db, r)
        interno = c["origen_tipo"] in ORIGENES_INTERNOS
        precio = None if interno or c["precio_venta"] in (None, "") else round(c["precio_venta"], 2)
        costo = round(c["total_reparacion"], 2)
        rut = ""
        if c["cliente_id"]:
            fila_rut = db.execute("SELECT rut FROM clientes WHERE id = ?", (c["cliente_id"],)).fetchone()
            rut = (fila_rut["rut"] if fila_rut else "") or ""
        filas.append({
            "numero": c["id"],
            "cliente": c["cliente"]["nombre"] if c["cliente"] else "",
            "rut": rut,
            "convertidor": " ".join(filter(None, [c["marca"], c["modelo"], c["numero_serie"]])),
            "origen": ORIGENES_CONVERTIDOR_LABEL.get(c["origen_tipo"], c["origen_tipo"]),
            "precio": precio,
            "costo": costo,
            "utilidad": round(precio - costo, 2) if precio is not None else None,
            "pagado": c["total_pagado"],
            "estado": ESTADOS_CONVERTIDOR_LABEL.get(c["estado"], c["estado"]),
            "ot_origen": _numero_ot_origen(db, c["orden_origen_id"]),
            "creacion": (c["fecha_recepcion"] or "")[:10],
            "cierre": (c["fecha_cierre"] or "")[:10],
            "regional": "Sí" if c["es_regional"] else "No",
        })
    return filas


COLUMNAS_REPORTE_CONVERTIDORES = [
    ("numero", "N°"), ("cliente", "Cliente"), ("rut", "RUT"), ("convertidor", "Convertidor"),
    ("origen", "Origen"), ("precio", "Precio de venta"), ("pagado", "Pagado"), ("costo", "Costo reparación"),
    ("utilidad", "Utilidad"), ("estado", "Estado"), ("ot_origen", "OT de origen"),
    ("creacion", "F. recepción"), ("cierre", "F. cierre"), ("regional", "Regional"),
]


@bp.route("/api/convertidores/reportes/ot", methods=["GET"])
@requiere_rol("convertidores")
def reporte_ot_convertidores():
    desde, hasta = _rango_fechas()
    estado = (request.args.get("estado") or "todas").strip().lower()
    if estado not in ESTADOS_REPORTE_CONVERTIDORES:
        estado = "todas"
    filas = _convertidores_para_reporte(desde, hasta, estado)

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "OT de convertidores"
    suma = lambda k: round(sum(f[k] or 0 for f in filas), 2)  # noqa: E731
    _excel_resumen(ws, "Reporte de OT de convertidores", [
        ("Estado", {"todas": "Todas", "abiertas": "Abiertas (por fecha de recepción)",
                    "cerradas": "Cerradas (por fecha de cierre)"}[estado]),
        ("Rango", f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"),
        ("Convertidores", len(filas)),
        ("Precio de venta", f"${suma('precio'):,.0f}".replace(",", ".")),
        ("Costo reparación", f"${suma('costo'):,.0f}".replace(",", ".")),
    ])
    fila_encabezado = ws.max_row + 1
    claves = [c for c, _ in COLUMNAS_REPORTE_CONVERTIDORES]
    ws.append([t for _, t in COLUMNAS_REPORTE_CONVERTIDORES])
    for f in filas:
        ws.append([f[c] if f[c] is not None else "" for c in claves])
    ws.append([])
    total_fila = [""] * len(claves)
    total_fila[claves.index("origen")] = "TOTAL"
    for k in ("precio", "pagado", "costo", "utilidad"):
        total_fila[claves.index(k)] = suma(k)
    ws.append(total_fila)
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    # Costos por mes de cierre (solo cerrados), por origen.
    ws2 = wb.create_sheet("Costos por mes")
    ws2.append(["Mes de cierre", "Origen", "Convertidores", "Costo reparación", "Precio de venta"])
    por_mes = {}
    for f in (filas if estado == "cerradas" else _convertidores_para_reporte(desde, hasta, "cerradas")):
        clave = ((f["cierre"] or "")[:7] or "(sin fecha)", f["origen"])
        acc = por_mes.setdefault(clave, [0, 0.0, 0.0])
        acc[0] += 1
        acc[1] += f["costo"] or 0
        acc[2] += f["precio"] or 0
    for (mes, origen), (cant, costo, precio) in sorted(por_mes.items(), reverse=True):
        ws2.append([mes, origen, cant, round(costo, 2), round(precio, 2)])
    _estilo_encabezado(ws2)
    _autoancho(ws2)

    sufijo = f"_{estado}" + (f"_{desde}_a_{hasta}" if (desde or hasta) else "")
    return _excel_response(wb, f"reporte_ot_convertidores{sufijo}_{datetime.now().strftime('%Y-%m-%d')}.xlsx")


@bp.route("/api/convertidores/reportes/excel", methods=["GET"])
@requiere_rol("convertidores")
def reporte_excel():
    """Excel con el detalle de convertidores del período y un resumen por
    origen -- para ver de un vistazo cuántas unidades vienen de stock,
    de clientes externos o de cajas de autos ya en el taller.

    IMPORTANTE -- valor_total NO incluye convertidores internos ya cargados
    en la OT/garantía de origen (origen_tipo en ORIGENES_INTERNOS, con
    instalado_en_orden_id o instalado_en_garantia_id != None): ese costo ya
    quedó registrado como gasto real en esa OT/garantía (ver
    _orden_dict_detalle / dashboard_operativo / "Costo garantías" en
    modulos/operativo.py), así que sumarlo también acá lo duplicaría. El
    detalle por fila SÍ sigue mostrando su costo_estimado individual (para
    trazabilidad/auditoría), marcado con la columna "Ya cargado en OT/garantía"
    -- solo se excluye del TOTAL agregado. Ver decisión del usuario: "excluir
    de reportes, no borrar datos"."""
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
    valor_ya_cargado = 0
    for r in rows:
        c = _convertidor_dict(db, r)
        origen_txt = ORIGENES_CONVERTIDOR_LABEL.get(c["origen_tipo"], c["origen_tipo"])
        conteo_origen[origen_txt] = conteo_origen.get(origen_txt, 0) + 1
        costo = c["costo_estimado"] or 0
        ya_cargado_en_ot = bool(c["origen_tipo"] == "desde_ot_auto" and c.get("instalado_en_orden_id"))
        ya_cargado_en_garantia = bool(c["origen_tipo"] == "desde_garantia" and c.get("instalado_en_garantia_id"))
        if ya_cargado_en_ot or ya_cargado_en_garantia:
            valor_ya_cargado += costo
        else:
            valor_total += costo
        if ya_cargado_en_ot:
            marca_cargado = f"Sí, OT #{c['instalado_en_orden_id']}"
        elif ya_cargado_en_garantia:
            marca_cargado = f"Sí, garantía #{c['instalado_en_garantia_id']}"
        else:
            marca_cargado = "No"
        detalle.append([
            (c["fecha_recepcion"] or "")[:10],
            c["cliente"]["nombre"] if c["cliente"] else "—",
            origen_txt,
            " ".join(filter(None, [c["marca"], c["modelo"]])) or "—",
            c["numero_serie"] or "—",
            ESTADOS_CONVERTIDOR_LABEL.get(c["estado"], c["estado"]),
            "Sí" if c["es_regional"] else "No",
            round(costo, 2),
            marca_cargado,
        ])

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Convertidores"
    rango_txt = f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"
    _excel_resumen(ws, "Convertidores de torque", [
        ("Rango", rango_txt),
        ("Convertidores", len(detalle)),
        ("Valor total (estimado)", f"${valor_total:,.0f}".replace(",", ".")),
        ("Ya cargado en OT/garantía (no sumado arriba)", f"${valor_ya_cargado:,.0f}".replace(",", ".")),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["Fecha", "Cliente", "Origen", "Marca/Modelo", "N° de serie", "Estado", "De región", "Costo estimado", "Ya cargado en OT/garantía"])
    for fila in detalle:
        ws.append(fila)
    ws.append([])
    ws.append(["", "", "", "", "", "", "TOTAL (sin lo ya cargado en OT/garantía)", round(valor_total, 2), ""])
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
