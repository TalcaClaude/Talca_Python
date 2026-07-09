"""
Módulo Operativo — órdenes de trabajo. Segundo módulo del ERP, construido
sobre el mismo patrón que Finanzas/Caja Chica (ver modulos/finanzas.py y
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

import json
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from core import (
    _aplicar_entrada_stock,
    _aplicar_salida_stock,
    _autoancho,
    _crear_libro_excel,
    _estilo_encabezado,
    _excel_response,
    _excel_resumen,
    _iniciar_transaccion_stock,
    _pdf_documento,
    _pdf_response,
    _pdf_resumen,
    _pdf_tabla,
    _rango_fechas,
    coincide_busqueda,
    fmt_money_pdf,
    formatear_rut,
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

# Secuencia "normal" de una OT para el stepper del seguimiento público — sin
# "cancelado", que no es un paso sino un estado terminal alternativo (se
# muestra aparte, con su propio mensaje).
PASOS_SEGUIMIENTO_PUBLICO = [e for e in ESTADOS_ORDEN if e != "cancelado"]

TIPOS_ITEM = ["repuesto", "mano_obra"]

# ---- Fase 3.5: constantes de los sub-flujos ampliados ----
PRIORIDADES_ORDEN = ["baja", "media", "alta", "urgente"]
TIPOS_PRESUPUESTO_ITEM = ["mano_obra", "repuesto", "insumo", "servicio_externo", "descuento"]
TIPOS_PRESUPUESTO_ITEM_LABEL = {
    "mano_obra": "Mano de obra", "repuesto": "Repuesto", "insumo": "Insumo",
    "servicio_externo": "Servicio externo", "descuento": "Descuento",
}
ESTADOS_PRESUPUESTO = ["pendiente", "enviado", "aprobado", "rechazado"]
RESULTADOS_QC_ITEM = ["ok", "falla", "na"]  # resultado de cada punto del checklist
RESULTADOS_QC = ["pendiente", "aprobado", "rechazado"]  # veredicto general de la orden
IVA_PORCENTAJE = 0.19


def _item_subtotal(item):
    """cantidad × costo_unitario de un ítem de orden_items/garantia_items,
    agregando el 19% si el ítem quedó marcado con aplica_iva (pensado para
    mano de obra subcontratada/externa que se cobra con IVA — ver migración
    de 'aplica_iva'/'detalle' en init_operativo_db). Repuestos normalmente
    no usan este flag (su costo ya es el Costo Promedio Ponderado interno),
    pero la función es genérica por si algún día se necesita ahí también."""
    multiplicador = (1 + IVA_PORCENTAJE) if item.get("aplica_iva") else 1
    return round(item["cantidad"] * item["costo_unitario"] * multiplicador, 2)

# Pagos de una OT: métodos aceptados.
METODOS_PAGO = ["efectivo", "tarjeta", "transferencia", "cheque", "otro"]

# Ítems fijos del checklist de "qué trae el vehículo" (recepción). Es una
# lista fija del taller (no un catálogo editable), así que se guarda como
# JSON en la orden en vez de una tabla aparte — más simple que el checklist
# de Control de Calidad, que sí necesita ítems libres por orden.
EQUIPAMIENTO_ITEMS = [
    "Encendedor", "Cenicero", "Gata", "Tapa Combustible", "Padron", "Tag",
    "Gancho Tiro", "Llave Rueda", "Radio", "Neumatico Repuesto", "Conos",
    "Extintor", "P. Circulacion", "Revision Tecnica", "S.O.A.P", "Certificado Gases",
]

# Garantías: proceso propio (definido por el taller), más simple que el de
# una OT normal (no pasa por presupuesto/aprobación/etc.). La secuencia
# esperada es ingreso -> diagnostico -> reparacion -> prueba -> revision ->
# listo -> entregado; "rechazada" es una salida alternativa disponible desde
# cualquier paso, para cuando el problema resulta no estar cubierto por la
# garantía.
ESTADOS_GARANTIA = [
    "ingreso", "diagnostico", "reparacion", "prueba", "revision", "listo",
    "entregado", "rechazada",
]
ESTADOS_GARANTIA_CERRADOS = {"entregado", "rechazada"}
ESTADOS_GARANTIA_LABEL = {
    "ingreso": "Ingreso", "diagnostico": "Diagnóstico", "reparacion": "Reparación",
    "prueba": "Prueba", "revision": "Revisión", "listo": "Listo",
    "entregado": "Entregado", "rechazada": "Rechazada",
}

# Campos "simples" (sin efectos secundarios) que se pueden actualizar desde
# el PUT general de la orden — diagnóstico. Los que sí disparan una
# transición automática de estado (presupuesto_estado, qc_resultado) viven
# en sus propios endpoints más abajo, para que ese efecto secundario quede
# explícito y sea auditable.
CAMPOS_SIMPLES_ORDEN = [
    "sintoma", "diagnostico_tecnico", "falla_detectada", "componentes_danados",
    "mediciones", "resultado_pruebas", "responsable_diagnostico",
    "referencia", "trabajo_a_realizar",
]


def _reconstruir_tabla_legada_si_hace_falta(db, tabla, sql_create_nueva, columnas_objetivo, mapeos_legado=None):
    """
    Sanea una tabla que puede venir de una versión mucho más vieja del
    proyecto, con columnas legadas que ya no usamos (a veces con NOT NULL
    y sin default, lo que hace fallar cualquier INSERT que no las llene).

    Ya nos pasó dos veces con 'garantias': primero le faltaba
    'cliente_id', después apareció una columna 'orden_id' NOT NULL de una
    versión anterior a que existiera 'orden_origen_id'. En vez de seguir
    agregando parches columna por columna cada vez que aparece una
    diferencia nueva, esta función reconstruye la tabla completa con el
    esquema actual, preservando todos los datos existentes:

      1. Si la tabla no existe todavía, no hace nada (el CREATE TABLE
         IF NOT EXISTS normal se encarga de crearla limpia).
      2. Si ya existe y coincide con el esquema objetivo (no le falta
         ninguna columna nuestra ni tiene columnas NOT NULL sin default
         que no conocemos), tampoco hace nada — evita reconstruir en
         cada arranque sin necesidad.
      3. Si detecta un desajuste, renombra la tabla vieja a
         '<tabla>_legado' (nunca se borra, por si hay que revisar o
         recuperar algo a mano), crea la tabla nueva con el esquema
         correcto, y copia los datos existentes columna por columna
         -usando 'mapeos_legado' para traducir nombres viejos a los
         actuales (ej. {"orden_id": "orden_origen_id"})-, incluyendo el
         'id' para no romper referencias de otras tablas.
    """
    info = db.execute(f"PRAGMA table_info({tabla})").fetchall()
    if not info:
        return False  # todavía no existe; la crea el CREATE TABLE normal

    nombres_actuales = {c[1] for c in info}
    columnas_problema = [
        c[1] for c in info
        if c[3] == 1 and c[4] is None and c[1] not in columnas_objetivo
    ]
    faltan_columnas = [c for c in columnas_objetivo if c not in nombres_actuales]
    if not columnas_problema and not faltan_columnas:
        return False

    mapeos_legado = mapeos_legado or {}
    tabla_legado = f"{tabla}_legado"
    if db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (tabla_legado,)
    ).fetchone():
        tabla_legado = f"{tabla}_legado_{int(datetime.now().timestamp())}"

    # Datos históricos pueden tener referencias que ya no apuntan a nada
    # (ej. un cliente/OT muy viejo que se insertó antes de que existiera
    # esta restricción, o de una época donde la app no aplicaba
    # "PRAGMA foreign_keys"). No queremos que el arranque de la app se
    # caiga -ni perder esas filas- solo porque una fila vieja no cumple
    # con una integridad referencial que nunca se le exigió al crearla.
    # Se desactiva la validación de llaves foráneas únicamente durante
    # esta copia puntual (hay que hacerlo fuera de una transacción activa,
    # por eso el commit() antes y después).
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")

    db.execute(f"ALTER TABLE {tabla} RENAME TO {tabla_legado}")
    db.executescript(sql_create_nueva)

    insert_cols, select_cols = [], []
    for col in columnas_objetivo:
        origen = None
        if col in nombres_actuales:
            origen = col
        else:
            for legado, destino in mapeos_legado.items():
                if destino == col and legado in nombres_actuales:
                    origen = legado
                    break
        if origen is not None:
            insert_cols.append(col)
            select_cols.append(origen)

    if insert_cols:
        db.execute(
            f"INSERT INTO {tabla} ({', '.join(insert_cols)}) "
            f"SELECT {', '.join(select_cols)} FROM {tabla_legado}"
        )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    db.commit()
    return True


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
    # control de calidad y garantías (como entidad propia). Todo aditivo
    # (ALTER TABLE ADD COLUMN sobre tablas ya existentes + CREATE TABLE IF
    # NOT EXISTS), siguiendo el mismo patrón de migración incremental que
    # usa app.py:init_db() — no rompe datos existentes.
    # -----------------------------------------------------------------

    # Datos del vehículo en sí (no cambian entre visitas) viven en equipos,
    # no en la orden — así el historial de vehículo no obliga a
    # re-escribirlos cada vez que el mismo auto vuelve al taller.
    columnas_equipo = [c[1] for c in db.execute("PRAGMA table_info(equipos)").fetchall()]
    columnas_nuevas_equipo = {
        "color": "TEXT DEFAULT ''",
        "anio": "INTEGER",
        "modelo_transmision": "TEXT DEFAULT ''",
        "puertas": "TEXT DEFAULT ''",
        "motor_cc": "TEXT DEFAULT ''",
        "vin": "TEXT DEFAULT ''",
    }
    for columna, definicion in columnas_nuevas_equipo.items():
        if columna not in columnas_equipo:
            db.execute(f"ALTER TABLE equipos ADD COLUMN {columna} {definicion}")
    db.commit()

    columnas_orden = [c[1] for c in db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
    columnas_nuevas_orden = {
        # Recepción (3.5.2) — solo lo que cambia entre visitas del mismo
        # vehículo; lo demás (marca/modelo/color/año/etc.) vive en equipos.
        "kilometraje": "INTEGER",
        "prioridad": "TEXT NOT NULL DEFAULT 'media'",
        "estanque_nivel": "INTEGER",
        "referencia": "TEXT DEFAULT ''",
        "trabajo_a_realizar": "TEXT DEFAULT ''",
        "precio_referencial": "REAL",
        "precio_iva": "INTEGER NOT NULL DEFAULT 0",
        "equipamiento_json": "TEXT DEFAULT ''",
        "recepcionado_por": "TEXT DEFAULT ''",
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
        # Control de calidad (3.5.6)
        "qc_resultado": "TEXT DEFAULT ''",
        "qc_inspector": "TEXT DEFAULT ''",
        "qc_observaciones": "TEXT DEFAULT ''",
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

        CREATE TABLE IF NOT EXISTS qc_checklist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
            item TEXT NOT NULL,
            resultado TEXT NOT NULL DEFAULT 'na',
            observacion TEXT DEFAULT '',
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_presupuesto_items_orden_id ON presupuesto_items (orden_id);
        CREATE INDEX IF NOT EXISTS idx_qc_checklist_items_orden_id ON qc_checklist_items (orden_id);
        """
    )
    db.commit()

    # Garantías: entidad propia (no un sub-registro de una orden) — se crea
    # a partir de los datos de un cliente/equipo, con su propio proceso más
    # simple que el de una OT normal, y puede opcionalmente referenciar la
    # OT original que tuvo el problema (solo para trazabilidad).
    #
    # Esta tabla viene teniendo un historial de incidentes en producción
    # porque arrastra un esquema mucho más viejo que el actual:
    #   1) Primero: 'garantia_items' nunca se creaba porque el CREATE TABLE
    #      estaba condicionado a que 'garantias' no existiera todavía.
    #   2) Después: al sacar esa condición, un CREATE INDEX sobre
    #      'cliente_id' fallaba porque la tabla real de producción no
    #      tenía esa columna (venía de antes de que existiera).
    #   3) Después: aun agregando 'cliente_id', apareció OTRA columna
    #      legada — 'orden_id' NOT NULL (de una versión anterior a que
    #      existiera 'orden_origen_id') — que hacía fallar cualquier
    #      INSERT nuevo.
    # En vez de seguir agregando parches cada vez que aparece una
    # diferencia nueva, ahora se usa _reconstruir_tabla_legada_si_hace_falta
    # (ver definición arriba de esta función): reconstruye la tabla
    # completa con el esquema actual si detecta cualquier desajuste,
    # preservando todos los datos existentes y sin necesidad de conocer de
    # antemano cada columna legada que pueda faltar o sobrar.
    sql_garantias_actual = """
        CREATE TABLE IF NOT EXISTS garantias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente_id INTEGER REFERENCES clientes(id),
            equipo_id INTEGER REFERENCES equipos(id),
            orden_origen_id INTEGER REFERENCES ordenes_trabajo(id),
            motivo TEXT DEFAULT '',
            diagnostico TEXT DEFAULT '',
            responsable TEXT DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'ingreso',
            fecha_recepcion TEXT,
            fecha_cierre TEXT,
            usuario TEXT DEFAULT '',
            creado_en TEXT,
            actualizado_en TEXT
        );
    """
    # Crea 'garantias' y 'garantia_items' limpias si no existían todavía
    # (CREATE TABLE IF NOT EXISTS no toca una tabla que ya existe).
    db.executescript(sql_garantias_actual)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS garantia_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            garantia_id INTEGER NOT NULL REFERENCES garantias(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,
            producto_id INTEGER REFERENCES productos(id),
            descripcion TEXT DEFAULT '',
            cantidad REAL NOT NULL DEFAULT 1,
            costo_unitario REAL NOT NULL DEFAULT 0,
            movimiento_id INTEGER REFERENCES movimientos(id),
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );
        """
    )
    db.commit()

    # Si la tabla ya existía de antes con un esquema legado (le falta
    # alguna columna nuestra, o tiene columnas NOT NULL sin default que ya
    # no usamos), se reconstruye preservando los datos.
    _reconstruir_tabla_legada_si_hace_falta(
        db,
        "garantias",
        sql_garantias_actual,
        [
            "id", "cliente_id", "equipo_id", "orden_origen_id", "motivo",
            "diagnostico", "responsable", "estado", "fecha_recepcion",
            "fecha_cierre", "usuario", "creado_en", "actualizado_en",
        ],
        mapeos_legado={"orden_id": "orden_origen_id"},
    )

    # Recién ahora es seguro crear los índices: todas las columnas que
    # referencian ya están garantizadas presentes.
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_garantias_cliente_id ON garantias (cliente_id);
        CREATE INDEX IF NOT EXISTS idx_garantias_equipo_id ON garantias (equipo_id);
        CREATE INDEX IF NOT EXISTS idx_garantias_estado ON garantias (estado);
        CREATE INDEX IF NOT EXISTS idx_garantia_items_garantia_id ON garantia_items (garantia_id);
        """
    )
    db.commit()

    # El proceso de garantías se amplió de 4 a 8 pasos (ver ESTADOS_GARANTIA
    # más arriba). Las garantías que ya existían con los nombres de estado
    # viejos se traducen una sola vez a los nuevos, para que no queden
    # "huérfanas" con un estado que ya no aparece en ningún filtro ni select.
    # Es seguro correr esto en cada arranque: una vez migradas, no queda
    # ninguna fila con los valores viejos y las consultas no hacen nada.
    MAPEO_ESTADOS_GARANTIA_LEGADO = {
        "pendiente": "ingreso",
        "en_reparacion": "reparacion",
        "resuelta": "entregado",
    }
    for estado_viejo, estado_nuevo in MAPEO_ESTADOS_GARANTIA_LEGADO.items():
        db.execute(
            "UPDATE garantias SET estado = ? WHERE estado = ?", (estado_nuevo, estado_viejo)
        )
    db.commit()

    # Pagos de una orden: un abono a la vez (efectivo, tarjeta, etc.), para
    # llevar el registro de cuánto se ha pagado y cuánto queda pendiente
    # sobre el valor de la OT (precio_referencial).
    columnas_pagos = [c[1] for c in db.execute("PRAGMA table_info(pagos_orden)").fetchall()]
    if not columnas_pagos:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pagos_orden (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id) ON DELETE CASCADE,
                metodo_pago TEXT NOT NULL DEFAULT 'efectivo',
                monto REAL NOT NULL DEFAULT 0,
                fecha TEXT NOT NULL,
                usuario TEXT DEFAULT '',
                creado_en TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pagos_orden_orden_id ON pagos_orden (orden_id);
            """
        )
        db.commit()

    # Ítems de mano de obra: se puede dejar un detalle de quién hizo el
    # trabajo (útil para trabajo externo/subcontratado, ej. "Programación
    # electrónica por Lucas Medina") y marcar si ese trabajo lleva IVA — la
    # mano de obra subcontratada normalmente sí, la interna normalmente no.
    # Cuando aplica_iva está marcado, el costo real de ese ítem (el que se
    # usa en "Costos"/utilidad) se calcula con el 19% agregado, ver
    # _item_subtotal más abajo.
    for tabla in ("orden_items", "garantia_items"):
        columnas_item = [c[1] for c in db.execute(f"PRAGMA table_info({tabla})").fetchall()]
        if "detalle" not in columnas_item:
            db.execute(f"ALTER TABLE {tabla} ADD COLUMN detalle TEXT DEFAULT ''")
            db.commit()
        if "aplica_iva" not in columnas_item:
            db.execute(f"ALTER TABLE {tabla} ADD COLUMN aplica_iva INTEGER NOT NULL DEFAULT 0")
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
    rows = db.execute("SELECT * FROM clientes ORDER BY nombre COLLATE NOCASE").fetchall()
    clientes = [_cliente_dict(db, r) for r in rows]
    if q:
        # Se filtra en Python con coincide_busqueda (ver core.py): admite
        # varias palabras sueltas, sin tildes, que pueden calzar en campos
        # distintos (ej. "perez 9912" encuentra por apellido Y por parte
        # del teléfono a la vez).
        clientes = [
            c for c in clientes
            if coincide_busqueda(q, c.get("nombre"), c.get("rut"), c.get("telefono"))
        ]
    return jsonify({"clientes": clientes, "total": len(clientes)})


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
    nombre = (data.get("nombre") or "").strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre del cliente es obligatorio"}), 400

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO clientes (nombre, telefono, email, rut, notas, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
        (
            nombre,
            (data.get("telefono") or "").strip(),
            (data.get("email") or "").strip(),
            formatear_rut((data.get("rut") or "").strip()),
            (data.get("notas") or "").strip().upper(),
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
    nombre = (data.get("nombre") or row["nombre"]).strip().upper()
    if not nombre:
        return jsonify({"error": "El nombre del cliente es obligatorio"}), 400

    db.execute(
        "UPDATE clientes SET nombre = ?, telefono = ?, email = ?, rut = ?, notas = ? WHERE id = ?",
        (
            nombre,
            (data.get("telefono", row["telefono"]) or "").strip(),
            (data.get("email", row["email"]) or "").strip(),
            formatear_rut((data.get("rut", row["rut"]) or "").strip()),
            (data.get("notas", row["notas"]) or "").strip().upper(),
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
    identificador = (request.args.get("identificador") or "").strip()
    sql = "SELECT * FROM equipos WHERE 1=1"
    params = []
    if cliente_id:
        sql += " AND cliente_id = ?"
        params.append(cliente_id)
    if identificador:
        # Coincidencia exacta (sin importar mayúsculas/espacios), para poder
        # autocompletar marca/modelo si esa patente ya está registrada en el
        # taller (aunque sea con otro cliente — ej. el auto cambió de dueño).
        sql += " AND UPPER(TRIM(identificador)) = UPPER(TRIM(?))"
        params.append(identificador)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    equipos = []
    for r in rows:
        d = dict(r)
        d["n_ordenes"] = db.execute(
            "SELECT COUNT(*) FROM ordenes_trabajo WHERE equipo_id = ?", (d["id"],)
        ).fetchone()[0]
        d["n_garantias"] = db.execute(
            "SELECT COUNT(*) FROM garantias WHERE equipo_id = ?", (d["id"],)
        ).fetchone()[0]
        equipos.append(d)
    return jsonify({"equipos": equipos, "total": len(equipos)})


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

    anio = None
    if data.get("anio") not in (None, ""):
        try:
            anio = int(data.get("anio"))
        except (TypeError, ValueError):
            return jsonify({"error": "El año debe ser un número entero"}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO equipos (
            cliente_id, identificador, marca, modelo, notas, creado_en,
            color, anio, modelo_transmision, puertas, motor_cc, vin
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cliente_id,
            (data.get("identificador") or "").strip().upper(),
            (data.get("marca") or "").strip().upper(),
            (data.get("modelo") or "").strip().upper(),
            (data.get("notas") or "").strip().upper(),
            ts,
            (data.get("color") or "").strip().upper(),
            anio,
            (data.get("modelo_transmision") or "").strip().upper(),
            (data.get("puertas") or "").strip(),
            (data.get("motor_cc") or "").strip().upper(),
            (data.get("vin") or "").strip().upper(),
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

    anio = row["anio"]
    if "anio" in data:
        if data.get("anio") in (None, ""):
            anio = None
        else:
            try:
                anio = int(data.get("anio"))
            except (TypeError, ValueError):
                return jsonify({"error": "El año debe ser un número entero"}), 400

    db.execute(
        """
        UPDATE equipos SET
            identificador = ?, marca = ?, modelo = ?, notas = ?,
            color = ?, anio = ?, modelo_transmision = ?, puertas = ?, motor_cc = ?, vin = ?
        WHERE id = ?
        """,
        (
            (data.get("identificador", row["identificador"]) or "").strip().upper(),
            (data.get("marca", row["marca"]) or "").strip().upper(),
            (data.get("modelo", row["modelo"]) or "").strip().upper(),
            (data.get("notas", row["notas"]) or "").strip().upper(),
            (data.get("color", row["color"]) or "").strip().upper(),
            anio,
            (data.get("modelo_transmision", row["modelo_transmision"]) or "").strip().upper(),
            (data.get("puertas", row["puertas"]) or "").strip(),
            (data.get("motor_cc", row["motor_cc"]) or "").strip().upper(),
            (data.get("vin", row["vin"]) or "").strip().upper(),
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

    n_garantias = db.execute("SELECT COUNT(*) FROM garantias WHERE equipo_id = ?", (equipo_id,)).fetchone()[0]
    if n_garantias:
        return jsonify({
            "error": f"Este equipo tiene {n_garantias} garantía(s) asociada(s) y no se puede eliminar."
        }), 409

    db.execute("DELETE FROM equipos WHERE id = ?", (equipo_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Órdenes de trabajo
# ---------------------------------------------------------------------------

def _orden_dict(db, row):
    d = dict(row)
    cliente = db.execute("SELECT id, nombre, telefono, rut, email FROM clientes WHERE id = ?", (d["cliente_id"],)).fetchone()
    d["cliente"] = dict(cliente) if cliente else None
    equipo = None
    if d["equipo_id"]:
        equipo = db.execute(
            "SELECT id, identificador, marca, modelo, color, anio, modelo_transmision, puertas, motor_cc, vin "
            "FROM equipos WHERE id = ?", (d["equipo_id"],)
        ).fetchone()
    d["equipo"] = dict(equipo) if equipo else None
    items = db.execute("SELECT * FROM orden_items WHERE orden_id = ? ORDER BY creado_en ASC", (d["id"],)).fetchall()
    d["items"] = [dict(i) for i in items]
    for it in d["items"]:
        it["subtotal"] = _item_subtotal(it)
    d["total"] = sum(i["subtotal"] for i in d["items"])
    pagos = db.execute(
        "SELECT * FROM pagos_orden WHERE orden_id = ? ORDER BY fecha ASC, creado_en ASC", (d["id"],)
    ).fetchall()
    d["pagos"] = [dict(p) for p in pagos]
    d["total_pagado"] = sum(p["monto"] for p in pagos)

    # Resumen de plata de la OT (Neto/IVA/Total/Pagado/Por pagar) — se
    # muestra siempre en la ventana principal de la orden. El Neto es el
    # valor cobrado al cliente (precio_referencial); el IVA solo se suma si
    # se marcó la casilla correspondiente.
    valor_neto = d["precio_referencial"] or 0
    valor_iva_monto = round(valor_neto * IVA_PORCENTAJE, 2) if d.get("precio_iva") else 0.0
    valor_total = round(valor_neto + valor_iva_monto, 2)
    d["valor_neto"] = valor_neto
    d["valor_iva_monto"] = valor_iva_monto
    d["valor_total"] = valor_total
    d["total_por_pagar"] = round(max(0, valor_total - d["total_pagado"]), 2)

    # Costo real de los repuestos cargados (a costo promedio ponderado, sin
    # margen) — para poder mostrar una utilidad estimada en la pestaña
    # "Costos". La mano de obra SÍ resta de la utilidad cuando tiene un
    # costo unitario cargado (ej. trabajo externo/subcontratado que se le
    # paga a un tercero, con o sin IVA) — es plata real que sale del taller.
    # La mano de obra interna normalmente se carga con costo $0 (ya está
    # cubierta por el sueldo fijo del personal), así que no afecta la
    # utilidad a menos que se le haya puesto un costo.
    costo_repuestos = sum(
        i["subtotal"] for i in d["items"] if i["tipo"] == "repuesto"
    )
    mano_obra_cargada = sum(
        i["subtotal"] for i in d["items"] if i["tipo"] == "mano_obra"
    )
    d["costo_repuestos"] = round(costo_repuestos, 2)
    d["mano_obra_cargada"] = round(mano_obra_cargada, 2)
    d["utilidad_estimada"] = round(valor_neto - costo_repuestos - mano_obra_cargada, 2)

    try:
        d["equipamiento"] = json.loads(d["equipamiento_json"]) if d.get("equipamiento_json") else {}
    except (TypeError, ValueError):
        d["equipamiento"] = {}
    return d


@bp.route("/api/operativo/dashboard", methods=["GET"])
def dashboard_operativo():
    """KPIs para el dashboard del módulo Operativo:
      - ots_abiertas: órdenes cuyo estado no es un estado cerrado (entregado/cancelado).
      - ots_cerradas_mes: órdenes entregadas cuya fecha_entrega_real cae en el mes calendario actual.
      - monto_cerradas_mes: valor cobrado (Neto + IVA si aplica) de esas órdenes cerradas este mes.
      - monto_por_cerrar: valor cobrado (Neto + IVA si aplica) de todas las órdenes abiertas.
      - costo_ots_cerradas: costo real de los repuestos usados en TODAS las órdenes entregadas
        (histórico, no solo el mes).
      - costo_mano_obra_ots_cerradas: costo real de la mano de obra CON costo unitario cargado
        (típicamente trabajo externo/subcontratado) en TODAS las órdenes entregadas (histórico) —
        la mano de obra interna sin costo cargado no aporta acá.
      - costo_garantias: costo real de los repuestos/mano de obra usados en TODAS las garantías
        (histórico) — es dinero que sale sin que se le cobre al cliente.
      - utilidad_mes: monto_cerradas_mes menos el costo de repuestos y de mano de obra (con costo
        cargado) de esas mismas órdenes.
      - ticket_promedio_mes: monto_cerradas_mes dividido por la cantidad de OTs cerradas este mes.
      - tiempo_promedio_taller_mes: días promedio entre recepción y entrega de las OTs cerradas
        este mes (qué tan rápido se está resolviendo, en promedio).
      - tasa_aprobacion_presupuestos: % de presupuestos aprobados sobre (aprobados + rechazados),
        histórico completo (no depende del mes porque una orden solo tiene un estado de
        presupuesto vigente, no un registro por mes).
      - costo_garantias_mes / costo_garantias_pct_ingresos_mes: costo de garantías de este mes y
        qué porcentaje representa sobre lo facturado este mes (indicador de calidad: si sube,
        se está reparando peor a la primera).
    """
    db = get_db()
    hoy = datetime.now()
    prefijo_mes = f"{hoy.year:04d}-{hoy.month:02d}"

    placeholders_cerrados = ", ".join("?" for _ in ESTADOS_CERRADOS)

    abiertas = db.execute(
        f"SELECT id, precio_referencial, precio_iva FROM ordenes_trabajo WHERE estado NOT IN ({placeholders_cerrados})",
        list(ESTADOS_CERRADOS),
    ).fetchall()
    ids_abiertas = [r["id"] for r in abiertas]

    cerradas_mes = db.execute(
        "SELECT id, precio_referencial, precio_iva, fecha_recepcion, fecha_entrega_real "
        "FROM ordenes_trabajo WHERE estado = 'entregado' AND substr(fecha_entrega_real, 1, 7) = ?",
        (prefijo_mes,),
    ).fetchall()
    ids_cerradas_mes = [r["id"] for r in cerradas_mes]

    ids_cerradas_todas = [
        r["id"] for r in db.execute("SELECT id FROM ordenes_trabajo WHERE estado = 'entregado'").fetchall()
    ]

    def _valor_total_orden(row):
        neto = row["precio_referencial"] or 0
        iva_monto = round(neto * IVA_PORCENTAJE, 2) if row["precio_iva"] else 0.0
        return round(neto + iva_monto, 2)

    # El multiplicador de IVA por ítem (CASE WHEN) refleja aplica_iva de la
    # misma forma que _item_subtotal en Python — no se puede reusar esa
    # función acá porque esto es un SUM agregado en SQL, no fila por fila.
    _iva_case = f"(CASE WHEN aplica_iva = 1 THEN 1 + {IVA_PORCENTAJE} ELSE 1 END)"

    def _suma_costo_repuestos(ids):
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        fila = db.execute(
            f"SELECT COALESCE(SUM(cantidad * costo_unitario), 0) AS total FROM orden_items "
            f"WHERE tipo = 'repuesto' AND orden_id IN ({placeholders})",
            ids,
        ).fetchone()
        return fila["total"] or 0

    # Costo de la mano de obra cargada con costo unitario (típicamente
    # trabajo externo/subcontratado pagado a un tercero) — plata real que
    # sale del taller y por lo tanto sí resta de la utilidad. La mano de
    # obra interna se suele cargar con costo $0, así que no aporta acá.
    def _suma_costo_mano_obra(ids):
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        fila = db.execute(
            f"SELECT COALESCE(SUM(cantidad * costo_unitario * {_iva_case}), 0) AS total FROM orden_items "
            f"WHERE tipo = 'mano_obra' AND orden_id IN ({placeholders})",
            ids,
        ).fetchone()
        return fila["total"] or 0

    monto_cerradas_mes = round(sum(_valor_total_orden(r) for r in cerradas_mes), 2)
    monto_por_cerrar = round(sum(_valor_total_orden(r) for r in abiertas), 2)
    costo_ots_cerradas = round(_suma_costo_repuestos(ids_cerradas_todas), 2)
    costo_mano_obra_ots_cerradas = round(_suma_costo_mano_obra(ids_cerradas_todas), 2)
    costo_repuestos_cerradas_mes = _suma_costo_repuestos(ids_cerradas_mes)
    costo_mano_obra_cerradas_mes = _suma_costo_mano_obra(ids_cerradas_mes)
    utilidad_mes = round(monto_cerradas_mes - costo_repuestos_cerradas_mes - costo_mano_obra_cerradas_mes, 2)

    costo_garantias = db.execute(
        f"SELECT COALESCE(SUM(cantidad * costo_unitario * {_iva_case}), 0) AS total FROM garantia_items"
    ).fetchone()["total"] or 0

    costo_garantias_mes = db.execute(
        f"SELECT COALESCE(SUM(cantidad * costo_unitario * {_iva_case}), 0) AS total FROM garantia_items "
        "WHERE substr(creado_en, 1, 7) = ?",
        (prefijo_mes,),
    ).fetchone()["total"] or 0

    placeholders_garantia_cerrados = ", ".join("?" for _ in ESTADOS_GARANTIA_CERRADOS)
    garantias_abiertas = db.execute(
        f"SELECT COUNT(*) AS n FROM garantias WHERE estado NOT IN ({placeholders_garantia_cerrados})",
        list(ESTADOS_GARANTIA_CERRADOS),
    ).fetchone()["n"]

    # Ticket promedio del mes: cuánto se está facturando en promedio por OT cerrada.
    ticket_promedio_mes = round(monto_cerradas_mes / len(ids_cerradas_mes), 2) if ids_cerradas_mes else 0

    # Tiempo promedio en el taller (recepción -> entrega) de las OTs cerradas este mes.
    def _dias(row):
        try:
            ini = datetime.fromisoformat(row["fecha_recepcion"])
            fin = datetime.fromisoformat(row["fecha_entrega_real"])
        except (TypeError, ValueError):
            return None
        return max(0, (fin - ini).total_seconds() / 86400)

    dias_validos = [d for d in (_dias(r) for r in cerradas_mes) if d is not None]
    tiempo_promedio_taller_mes = round(sum(dias_validos) / len(dias_validos), 1) if dias_validos else 0

    # Tasa de aprobación de presupuestos: histórica (una orden solo tiene un
    # estado de presupuesto vigente, no un registro por mes).
    conteo_presupuesto = dict(db.execute(
        "SELECT presupuesto_estado, COUNT(*) AS n FROM ordenes_trabajo "
        "WHERE presupuesto_estado IN ('aprobado', 'rechazado') GROUP BY presupuesto_estado"
    ).fetchall())
    n_aprobados = conteo_presupuesto.get("aprobado", 0)
    n_rechazados = conteo_presupuesto.get("rechazado", 0)
    total_decididos = n_aprobados + n_rechazados
    tasa_aprobacion_presupuestos = round(n_aprobados / total_decididos * 100, 1) if total_decididos else None

    costo_garantias_pct_ingresos_mes = (
        round(costo_garantias_mes / monto_cerradas_mes * 100, 1) if monto_cerradas_mes else None
    )

    return jsonify({
        "ots_abiertas": len(ids_abiertas),
        "ots_cerradas_mes": len(ids_cerradas_mes),
        "monto_cerradas_mes": monto_cerradas_mes,
        "monto_por_cerrar": monto_por_cerrar,
        "garantias_abiertas": garantias_abiertas,
        "costo_ots_cerradas": costo_ots_cerradas,
        "costo_mano_obra_ots_cerradas": costo_mano_obra_ots_cerradas,
        "costo_garantias": round(costo_garantias, 2),
        "utilidad_mes": utilidad_mes,
        "ticket_promedio_mes": ticket_promedio_mes,
        "tiempo_promedio_taller_mes": tiempo_promedio_taller_mes,
        "tasa_aprobacion_presupuestos": tasa_aprobacion_presupuestos,
        "costo_garantias_mes": round(costo_garantias_mes, 2),
        "costo_garantias_pct_ingresos_mes": costo_garantias_pct_ingresos_mes,
    })


# ---------------------------------------------------------------------------
# Seguimiento público (sin login) — el cliente ingresa solo la patente y ve
# en qué va su vehículo, sin tener que llamar al taller. No expone datos del
# cliente (nombre/teléfono/RUT/email) ni montos ni notas internas: solo lo
# necesario para saber el estado. Ver RUTAS_PUBLICAS en app.py, donde se
# habilitan estos dos endpoints sin sesión iniciada.
# ---------------------------------------------------------------------------

@bp.route("/seguimiento", methods=["GET"])
def seguimiento_publico():
    return render_template("seguimiento.html")


@bp.route("/api/seguimiento", methods=["GET"])
def api_seguimiento_publico():
    patente = (request.args.get("patente") or "").strip()
    if not patente:
        return jsonify({"error": "Ingresa una patente"}), 400

    db = get_db()
    equipo = db.execute(
        "SELECT id, marca, modelo FROM equipos WHERE UPPER(TRIM(identificador)) = UPPER(TRIM(?))",
        (patente,),
    ).fetchone()
    if equipo is None:
        return jsonify({"encontrado": False})

    orden = db.execute(
        "SELECT id, estado, fecha_recepcion, fecha_entrega_estimada, fecha_entrega_real "
        "FROM ordenes_trabajo WHERE equipo_id = ? ORDER BY creado_en DESC LIMIT 1",
        (equipo["id"],),
    ).fetchone()
    if orden is None:
        return jsonify({"encontrado": False})

    return jsonify({
        "encontrado": True,
        "patente": patente.strip().upper(),
        "marca": equipo["marca"] or "",
        "modelo": equipo["modelo"] or "",
        "orden_numero": orden["id"],
        "estado": orden["estado"],
        "estado_label": ESTADOS_LABEL.get(orden["estado"], orden["estado"]),
        "fecha_recepcion": orden["fecha_recepcion"],
        "fecha_entrega_estimada": orden["fecha_entrega_estimada"],
        "fecha_entrega_real": orden["fecha_entrega_real"],
        "pasos": PASOS_SEGUIMIENTO_PUBLICO,
        "pasos_label": {p: ESTADOS_LABEL.get(p, p) for p in PASOS_SEGUIMIENTO_PUBLICO},
    })


@bp.route("/api/operativo/ordenes", methods=["GET"])
def listar_ordenes():
    db = get_db()
    estado = (request.args.get("estado") or "").strip()
    cliente_id = request.args.get("cliente_id")
    equipo_id = request.args.get("equipo_id")
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
    if equipo_id:
        sql += " AND equipo_id = ?"
        params.append(equipo_id)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"ordenes": [_orden_dict(db, r) for r in rows], "total": len(rows)})


def _orden_dict_detalle(db, row):
    """Versión completa de la orden para la vista de detalle: agrega
    presupuesto y control de calidad. Deliberadamente NO se usa en
    listar_ordenes/dashboard (eso reintroduciría el problema N+1 que ya se
    corrigió antes en este proyecto) — solo en obtener_orden y en las
    acciones (presupuesto/qc) que devuelven la orden completa después de
    un cambio."""
    d = _orden_dict(db, row)
    d["presupuesto"] = _presupuesto_dict(db, row["id"], row["presupuesto_iva"])
    d["qc_checklist"] = [dict(i) for i in db.execute(
        "SELECT * FROM qc_checklist_items WHERE orden_id = ? ORDER BY creado_en ASC", (row["id"],)
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

        # Un mismo vehículo no puede tener dos OT abiertas al mismo tiempo
        # (físicamente no puede estar recibiéndose dos veces a la vez). Solo
        # se puede crear una OT nueva para este equipo una vez que la
        # anterior ya se entregó o se canceló — ahí se entiende que el auto
        # volvió a salir del taller.
        placeholders = ", ".join("?" for _ in ESTADOS_CERRADOS)
        ot_abierta = db.execute(
            f"SELECT id, estado FROM ordenes_trabajo WHERE equipo_id = ? AND estado NOT IN ({placeholders}) "
            "ORDER BY fecha_recepcion DESC LIMIT 1",
            (equipo_id, *ESTADOS_CERRADOS),
        ).fetchone()
        if ot_abierta is not None:
            return jsonify({
                "error": f"Este vehículo ya tiene una OT abierta (OT #{ot_abierta['id']}, estado: "
                         f"{ESTADOS_LABEL.get(ot_abierta['estado'], ot_abierta['estado'])}). "
                         f"Solo se puede crear una OT nueva para el mismo auto una vez que la anterior se entregue o se cancele.",
                "bloqueo_id": ot_abierta["id"],
                "bloqueo_tipo": "orden",
            }), 400

    prioridad = (data.get("prioridad") or "media").strip().lower()
    if prioridad not in PRIORIDADES_ORDEN:
        return jsonify({"error": f"Prioridad inválida. Usa una de: {', '.join(PRIORIDADES_ORDEN)}"}), 400

    kilometraje = None
    if data.get("kilometraje") not in (None, ""):
        try:
            kilometraje = int(data.get("kilometraje"))
        except (TypeError, ValueError):
            return jsonify({"error": "El kilometraje debe ser un número entero"}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO ordenes_trabajo (
            cliente_id, equipo_id, estado, descripcion_problema, mecanico,
            fecha_recepcion, fecha_entrega_estimada, usuario, creado_en, actualizado_en,
            kilometraje, prioridad, recepcionado_por
        ) VALUES (?, ?, 'recibido', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cliente_id, equipo_id,
            (data.get("descripcion_problema") or "").strip().upper(),
            (data.get("mecanico") or "").strip().upper(),
            ts,
            (data.get("fecha_entrega_estimada") or "").strip() or None,
            usuario_actual(), ts, ts,
            kilometraje,
            prioridad,
            usuario_actual(),
        ),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_orden_dict(db, nueva)), 201


def _actualizar_estado_orden_por_agente(db, orden_id, estado_nuevo):
    """Aplica un cambio de estado a una orden de trabajo — pensado para el
    agente de WhatsApp (ver app.py: _agente_procesar_mensaje). A diferencia
    de editar_orden() (el PUT genérico que usa el tablero de tarjetas y
    puede tocar ~15 campos a la vez), esta función SOLO toca
    estado/fecha_entrega_real: un mensaje de texto libre interpretado por
    IA no debería poder modificar diagnóstico, presupuesto, ítems, ni
    ningún otro campo, aunque el mensaje mencione algo de eso de pasada.

    Devuelve (orden_dict, None) si funcionó, o (None, mensaje_de_error) si
    no — nunca lanza una excepción, para que el llamador (que además tiene
    que dejar un registro de auditoría) controle el flujo sin try/except."""
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return None, f"No existe ninguna orden de trabajo con el número {orden_id}."
    if estado_nuevo not in ESTADOS_ORDEN:
        return None, f"Estado inválido: {estado_nuevo}"

    fecha_entrega_real = row["fecha_entrega_real"]
    if estado_nuevo == "entregado" and not fecha_entrega_real:
        # Mismo criterio que editar_orden(): se completa sola la primera
        # vez que la orden pasa a "entregado".
        fecha_entrega_real = now_iso()

    db.execute(
        "UPDATE ordenes_trabajo SET estado = ?, fecha_entrega_real = ?, actualizado_en = ? WHERE id = ?",
        (estado_nuevo, fecha_entrega_real, now_iso(), orden_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return _orden_dict(db, actualizada), None


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

    estanque_nivel = row["estanque_nivel"]
    if "estanque_nivel" in data:
        if data.get("estanque_nivel") in (None, ""):
            estanque_nivel = None
        else:
            try:
                estanque_nivel = int(data.get("estanque_nivel"))
            except (TypeError, ValueError):
                return jsonify({"error": "El nivel de estanque debe ser un número entero"}), 400
            if not (0 <= estanque_nivel <= 8):
                return jsonify({"error": "El nivel de estanque debe estar entre 0 y 8 (octavos)"}), 400

    precio_referencial = row["precio_referencial"]
    if "precio_referencial" in data:
        if data.get("precio_referencial") in (None, ""):
            precio_referencial = None
        else:
            try:
                precio_referencial = float(data.get("precio_referencial"))
            except (TypeError, ValueError):
                return jsonify({"error": "El precio referencial debe ser un número"}), 400

    precio_iva = row["precio_iva"]
    if "precio_iva" in data:
        precio_iva = 1 if data.get("precio_iva") else 0

    equipamiento_json = row["equipamiento_json"]
    if "equipamiento" in data:
        equipamiento = data.get("equipamiento") or {}
        if not isinstance(equipamiento, dict):
            return jsonify({"error": "El equipamiento debe ser un objeto de casillas marcadas"}), 400
        equipamiento_json = json.dumps({k: bool(v) for k, v in equipamiento.items() if k in EQUIPAMIENTO_ITEMS})

    # Campos de texto simples (diagnóstico, recepción de tipo texto): mismo
    # patrón de "usa lo nuevo si viene, si no deja lo que ya había".
    valores_simples = {
        campo: (data.get(campo, row[campo]) or "").strip().upper() for campo in CAMPOS_SIMPLES_ORDEN
    }

    db.execute(
        """
        UPDATE ordenes_trabajo SET
            estado = ?, descripcion_problema = ?, diagnostico = ?, mecanico = ?,
            fecha_entrega_estimada = ?, fecha_entrega_real = ?, actualizado_en = ?,
            prioridad = ?, kilometraje = ?, estanque_nivel = ?, precio_referencial = ?, precio_iva = ?,
            equipamiento_json = ?,
            sintoma = ?, diagnostico_tecnico = ?, falla_detectada = ?, componentes_danados = ?,
            mediciones = ?, resultado_pruebas = ?, responsable_diagnostico = ?,
            referencia = ?, trabajo_a_realizar = ?
        WHERE id = ?
        """,
        (
            estado,
            (data.get("descripcion_problema", row["descripcion_problema"]) or "").strip().upper(),
            (data.get("diagnostico", row["diagnostico"]) or "").strip().upper(),
            (data.get("mecanico", row["mecanico"]) or "").strip().upper(),
            (data.get("fecha_entrega_estimada", row["fecha_entrega_estimada"]) or "").strip() or None,
            fecha_entrega_real,
            now_iso(),
            prioridad, kilometraje, estanque_nivel, precio_referencial, precio_iva,
            equipamiento_json,
            valores_simples["sintoma"], valores_simples["diagnostico_tecnico"],
            valores_simples["falla_detectada"], valores_simples["componentes_danados"],
            valores_simples["mediciones"], valores_simples["resultado_pruebas"],
            valores_simples["responsable_diagnostico"],
            valores_simples["referencia"], valores_simples["trabajo_a_realizar"],
            orden_id,
        ),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return jsonify(_orden_dict(db, actualizada))


@bp.route("/api/operativo/ordenes/<int:orden_id>", methods=["DELETE"])
def eliminar_orden(orden_id):
    # No existía forma de borrar una orden completa (solo ítems sueltos
    # dentro de ella) — hacía falta para poder limpiar órdenes de prueba
    # sin dejar residuos. pagos_orden/orden_items/presupuesto_items/
    # qc_checklist_items tienen ON DELETE CASCADE hacia ordenes_trabajo, así
    # que se borran solos; lo único que hay que manejar a mano es devolver
    # el stock de los repuestos cargados (igual que al eliminar un ítem
    # suelto) y desvincular -sin borrar- cualquier garantía que referencie
    # esta orden solo para trazabilidad (orden_origen_id no tiene cascada
    # a propósito: una garantía es un registro propio, independiente de que
    # la OT original todavía exista).
    db = get_db()
    _iniciar_transaccion_stock(db)
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    items_repuesto = db.execute(
        "SELECT * FROM orden_items WHERE orden_id = ? AND tipo = 'repuesto' AND producto_id IS NOT NULL",
        (orden_id,),
    ).fetchall()
    for item in items_repuesto:
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (item["producto_id"],)).fetchone()
        if producto is not None:
            _aplicar_entrada_stock(
                db, producto, int(round(item["cantidad"])), item["costo_unitario"],
                motivo=f"DEVOLUCIÓN AL ELIMINAR ORDEN #{orden_id}",
                usuario=usuario_actual(), factura=None, categoria="Devolución",
            )

    db.execute("UPDATE garantias SET orden_origen_id = NULL WHERE orden_origen_id = ?", (orden_id,))
    db.execute("DELETE FROM ordenes_trabajo WHERE id = ?", (orden_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Pagos de la orden (abonos sobre el valor de la OT)
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/ordenes/<int:orden_id>/pagos", methods=["POST"])
def agregar_pago_orden(orden_id):
    db = get_db()
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    data = request.get_json(force=True, silent=True) or {}
    metodo_pago = (data.get("metodo_pago") or "").strip().lower()
    if metodo_pago not in METODOS_PAGO:
        return jsonify({"error": f"Método de pago inválido. Usa uno de: {', '.join(METODOS_PAGO)}"}), 400

    try:
        monto = float(data.get("monto"))
    except (TypeError, ValueError):
        return jsonify({"error": "El monto debe ser un número"}), 400
    if monto <= 0:
        return jsonify({"error": "El monto del pago debe ser mayor a 0"}), 400

    fecha = (data.get("fecha") or "").strip() or now_iso()

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO pagos_orden (orden_id, metodo_pago, monto, fecha, usuario, creado_en)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (orden_id, metodo_pago, monto, fecha, usuario_actual(), ts),
    )
    db.commit()
    nuevo = db.execute("SELECT * FROM pagos_orden WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nuevo)), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>/pagos/<int:pago_id>", methods=["DELETE"])
def eliminar_pago_orden(orden_id, pago_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM pagos_orden WHERE id = ? AND orden_id = ?", (pago_id, orden_id)
    ).fetchone()
    if row is None:
        return jsonify({"error": "Pago no encontrado"}), 404
    db.execute("DELETE FROM pagos_orden WHERE id = ?", (pago_id,))
    db.commit()
    return jsonify({"ok": True})


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
    descripcion = (data.get("descripcion") or "").strip().upper()

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
    detalle = ""
    aplica_iva = 0
    if tipo == "mano_obra":
        if not descripcion:
            return jsonify({"error": "Describe la mano de obra realizada"}), 400
        try:
            costo_unitario = float(data.get("costo_unitario") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "El costo unitario debe ser un número válido"}), 400
        # Detalle opcional (ej. quién hizo el trabajo si fue subcontratado:
        # "Programación electrónica por Lucas Medina") y si ese trabajo se
        # cobra con IVA (típico en mano de obra externa/subcontratada).
        detalle = (data.get("detalle") or "").strip().upper()
        aplica_iva = 1 if data.get("aplica_iva") else 0

    cur = db.execute(
        """
        INSERT INTO orden_items (
            orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario, movimiento_id, usuario, ts, detalle, aplica_iva),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))
    db.commit()

    nuevo_item = dict(db.execute("SELECT * FROM orden_items WHERE id = ?", (cur.lastrowid,)).fetchone())
    nuevo_item["subtotal"] = _item_subtotal(nuevo_item)
    return jsonify(nuevo_item), 201


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
    garantias = db.execute(
        "SELECT * FROM garantias WHERE equipo_id = ? ORDER BY creado_en DESC", (equipo_id,)
    ).fetchall()
    return jsonify({
        "equipo": dict(equipo),
        "cliente": dict(cliente) if cliente else None,
        "ordenes": [_orden_dict(db, r) for r in ordenes],
        "garantias": [_garantia_dict(db, g) for g in garantias],
    })


@bp.route("/api/operativo/equipos/<int:equipo_id>/bloqueo", methods=["GET"])
def bloqueo_equipo(equipo_id):
    """Chequeo rápido para avisar ANTES de intentar crear una OT o una
    garantía si el vehículo ya tiene una abierta — se llama apenas se
    selecciona el vehículo en el formulario, para que el error aparezca de
    inmediato en vez de recién al enviar. Devuelve la misma información
    (id + estado) que usan crear_orden()/crear_garantia() para bloquear."""
    db = get_db()
    equipo = db.execute("SELECT id FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    if equipo is None:
        return jsonify({"error": "Equipo no encontrado"}), 404

    placeholders_ot = ", ".join("?" for _ in ESTADOS_CERRADOS)
    ot_abierta = db.execute(
        f"SELECT id, estado FROM ordenes_trabajo WHERE equipo_id = ? AND estado NOT IN ({placeholders_ot}) "
        "ORDER BY fecha_recepcion DESC LIMIT 1",
        (equipo_id, *ESTADOS_CERRADOS),
    ).fetchone()

    placeholders_gar = ", ".join("?" for _ in ESTADOS_GARANTIA_CERRADOS)
    garantia_abierta = db.execute(
        f"SELECT id, estado FROM garantias WHERE equipo_id = ? AND estado NOT IN ({placeholders_gar}) "
        "ORDER BY fecha_recepcion DESC LIMIT 1",
        (equipo_id, *ESTADOS_GARANTIA_CERRADOS),
    ).fetchone()

    return jsonify({
        "ot_abierta": {
            "id": ot_abierta["id"],
            "estado": ot_abierta["estado"],
            "estado_label": ESTADOS_LABEL.get(ot_abierta["estado"], ot_abierta["estado"]),
        } if ot_abierta else None,
        "garantia_abierta": {
            "id": garantia_abierta["id"],
            "estado": garantia_abierta["estado"],
            "estado_label": ESTADOS_GARANTIA_LABEL.get(garantia_abierta["estado"], garantia_abierta["estado"]),
        } if garantia_abierta else None,
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
    descripcion = (data.get("descripcion") or "").strip().upper()
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

    notas = (data.get("notas", row["presupuesto_notas"]) or "").strip().upper()

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


@bp.route("/api/operativo/ordenes/<int:orden_id>/presupuesto/pdf", methods=["GET"])
def presupuesto_pdf(orden_id):
    """Genera el presupuesto en PDF, listo para descargar y enviárselo al
    cliente (por correo, WhatsApp, etc.). Usa los mismos datos que ya se
    ven en el modal de Presupuesto — cliente, vehículo, ítems y totales."""
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    orden = _orden_dict_detalle(db, row)
    cliente = orden.get("cliente") or {}
    equipo = orden.get("equipo") or {}
    presupuesto = orden["presupuesto"]

    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    estilos = getSampleStyleSheet()
    estilos.add(ParagraphStyle(name="TituloPresupuesto", fontSize=18, leading=22, spaceAfter=2, fontName="Helvetica-Bold"))
    estilos.add(ParagraphStyle(name="SubtituloPresupuesto", fontSize=10, textColor="#666666", spaceAfter=16))
    estilos.add(ParagraphStyle(name="EtiquetaPresupuesto", fontSize=9, textColor="#666666", spaceBefore=10, spaceAfter=2))

    elementos = [
        Paragraph("Taller Mecánico", estilos["TituloPresupuesto"]),
        Paragraph(f"Presupuesto — Orden de trabajo #{orden_id} · generado el {datetime.now().strftime('%d-%m-%Y %H:%M')}", estilos["SubtituloPresupuesto"]),
    ]

    vehiculo_txt = " ".join(filter(None, [equipo.get("marca"), equipo.get("modelo")])) or "—"
    datos_cliente = [
        ["Cliente:", cliente.get("nombre") or "—", "RUT:", cliente.get("rut") or "—"],
        ["Teléfono:", cliente.get("telefono") or "—", "Vehículo:", vehiculo_txt],
        ["Patente:", equipo.get("identificador") or "—", "Año:", equipo.get("anio") or "—"],
    ]
    tabla_cliente = Table(datos_cliente, colWidths=[2.4 * cm, 6.3 * cm, 2.2 * cm, 6.3 * cm])
    tabla_cliente.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elementos.append(tabla_cliente)
    elementos.append(Spacer(1, 14))

    encabezados = ["Tipo", "Descripción", "Cantidad", "Precio unit.", "Subtotal"]
    filas = [encabezados]
    for it in presupuesto["items"]:
        filas.append([
            TIPOS_PRESUPUESTO_ITEM_LABEL.get(it["tipo"], it["tipo"]),
            it["descripcion"] or "—",
            f"{it['cantidad']:g}",
            fmt_money_pdf(it["precio_unitario"]),
            fmt_money_pdf(it["subtotal"]),
        ])
    if len(filas) == 1:
        filas.append(["—", "Sin ítems todavía", "", "", ""])

    tabla_items = Table(filas, colWidths=[2.8 * cm, 7.2 * cm, 2.2 * cm, 2.9 * cm, 2.9 * cm])
    tabla_items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2430")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elementos.append(tabla_items)
    elementos.append(Spacer(1, 12))

    resumen = [["Subtotal", fmt_money_pdf(presupuesto["subtotal"])]]
    if orden.get("presupuesto_iva"):
        resumen.append(["IVA (19%)", fmt_money_pdf(presupuesto["iva_monto"])])
    resumen.append(["TOTAL", fmt_money_pdf(presupuesto["total"])])
    tabla_resumen = Table(resumen, colWidths=[15.2 * cm, 2.8 * cm])
    tabla_resumen.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#333333")),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]))
    elementos.append(tabla_resumen)

    if orden.get("presupuesto_notas"):
        elementos.append(Paragraph("Notas", estilos["EtiquetaPresupuesto"]))
        elementos.append(Paragraph(orden["presupuesto_notas"], estilos["Normal"]))

    nombre = f"presupuesto_ot{orden_id}_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    return _pdf_response(buffer, doc, elementos, nombre)


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
    item = (data.get("item") or "").strip().upper()
    if not item:
        return jsonify({"error": "Describe el punto a revisar"}), 400
    resultado = (data.get("resultado") or "na").strip().lower()
    if resultado not in RESULTADOS_QC_ITEM:
        return jsonify({"error": f"Resultado inválido. Usa uno de: {', '.join(RESULTADOS_QC_ITEM)}"}), 400
    observacion = (data.get("observacion") or "").strip().upper()
    ts = now_iso()
    cur = db.execute(
        "INSERT INTO qc_checklist_items (orden_id, item, resultado, observacion, usuario, creado_en) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (orden_id, item, resultado, observacion, usuario_actual(), ts),
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
    observacion = (data.get("observacion", item["observacion"]) or "").strip().upper()
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
    inspector = (data.get("inspector", row["qc_inspector"]) or "").strip().upper()
    observaciones = (data.get("observaciones", row["qc_observaciones"]) or "").strip().upper()

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
# Fase 3.5.7 (rediseñada) — Garantías como entidad propia
#
# Una garantía ya NO es un sub-registro de una orden: se crea "a partir de
# los datos del auto" (cliente + equipo), con su propio proceso (ver
# ESTADOS_GARANTIA arriba), y puede opcionalmente referenciar la OT
# original solo para trazabilidad.
# Vive en su propia vista dentro del módulo, separada del tablero de OTs.
# ---------------------------------------------------------------------------

def _garantia_dict(db, row):
    d = dict(row)
    cliente = db.execute("SELECT id, nombre, telefono FROM clientes WHERE id = ?", (d["cliente_id"],)).fetchone()
    d["cliente"] = dict(cliente) if cliente else None
    equipo = None
    if d["equipo_id"]:
        equipo = db.execute(
            "SELECT id, identificador, marca, modelo FROM equipos WHERE id = ?", (d["equipo_id"],)
        ).fetchone()
    d["equipo"] = dict(equipo) if equipo else None
    items = db.execute(
        "SELECT * FROM garantia_items WHERE garantia_id = ? ORDER BY creado_en ASC", (d["id"],)
    ).fetchall()
    d["items"] = [dict(i) for i in items]
    for it in d["items"]:
        it["subtotal"] = _item_subtotal(it)
    d["total"] = sum(i["subtotal"] for i in d["items"])
    return d


@bp.route("/api/operativo/garantias", methods=["POST"])
def crear_garantia():
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

        # Una garantía es "el auto ya se entregó y volvió por un problema" —
        # no tiene sentido abrir una garantía para un vehículo que TODAVÍA
        # está en el taller con una OT abierta (ni siquiera ha salido). Si
        # el problema apareció mientras la OT sigue abierta, corresponde
        # seguir trabajándolo dentro de esa misma OT, no como garantía.
        placeholders_ot = ", ".join("?" for _ in ESTADOS_CERRADOS)
        ot_abierta = db.execute(
            f"SELECT id, estado FROM ordenes_trabajo WHERE equipo_id = ? AND estado NOT IN ({placeholders_ot}) "
            "ORDER BY fecha_recepcion DESC LIMIT 1",
            (equipo_id, *ESTADOS_CERRADOS),
        ).fetchone()
        if ot_abierta is not None:
            return jsonify({
                "error": f"Este vehículo ya tiene una Orden de Trabajo abierta en el taller (OT #{ot_abierta['id']}, "
                         f"estado: {ESTADOS_LABEL.get(ot_abierta['estado'], ot_abierta['estado'])}). No se puede abrir "
                         f"una garantía para un vehículo que todavía no ha salido del taller — sigue trabajándolo "
                         f"dentro de esa misma OT.",
                "bloqueo_id": ot_abierta["id"],
                "bloqueo_tipo": "orden",
            }), 400

        # Mismo criterio, pero entre garantías: un vehículo no puede tener
        # dos garantías abiertas al mismo tiempo. Recién se puede abrir una
        # garantía nueva para el mismo auto cuando la anterior ya quedó
        # entregada o rechazada (el auto ya salió del taller).
        placeholders = ", ".join("?" for _ in ESTADOS_GARANTIA_CERRADOS)
        garantia_abierta = db.execute(
            f"SELECT id, estado FROM garantias WHERE equipo_id = ? AND estado NOT IN ({placeholders}) "
            "ORDER BY fecha_recepcion DESC LIMIT 1",
            (equipo_id, *ESTADOS_GARANTIA_CERRADOS),
        ).fetchone()
        if garantia_abierta is not None:
            return jsonify({
                "error": f"Este vehículo ya tiene una garantía abierta (Garantía #{garantia_abierta['id']}, estado: "
                         f"{ESTADOS_GARANTIA_LABEL.get(garantia_abierta['estado'], garantia_abierta['estado'])}). "
                         f"Solo se puede abrir una garantía nueva para el mismo auto una vez que la anterior se entregue o se rechace.",
                "bloqueo_id": garantia_abierta["id"],
                "bloqueo_tipo": "garantia",
            }), 400

    orden_origen_id = None
    if data.get("orden_origen_id") not in (None, ""):
        try:
            orden_origen_id = int(data.get("orden_origen_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "OT original inválida"}), 400
        if db.execute("SELECT id FROM ordenes_trabajo WHERE id = ?", (orden_origen_id,)).fetchone() is None:
            return jsonify({"error": "La OT original indicada no existe"}), 400

    motivo = (data.get("motivo") or "").strip().upper()
    if not motivo:
        return jsonify({"error": "Describe el motivo de la garantía"}), 400

    ts = now_iso()
    cur = db.execute(
        """
        INSERT INTO garantias (
            cliente_id, equipo_id, orden_origen_id, motivo, diagnostico, responsable,
            estado, fecha_recepcion, usuario, creado_en, actualizado_en
        ) VALUES (?, ?, ?, ?, ?, ?, 'ingreso', ?, ?, ?, ?)
        """,
        (
            cliente_id, equipo_id, orden_origen_id, motivo,
            (data.get("diagnostico") or "").strip().upper(),
            (data.get("responsable") or "").strip().upper(),
            ts, usuario_actual(), ts, ts,
        ),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM garantias WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_garantia_dict(db, nueva)), 201


@bp.route("/api/operativo/garantias", methods=["GET"])
def listar_garantias():
    db = get_db()
    estado = (request.args.get("estado") or "").strip()
    sql = "SELECT * FROM garantias WHERE 1=1"
    params = []
    if estado:
        sql += " AND estado = ?"
        params.append(estado)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"garantias": [_garantia_dict(db, r) for r in rows], "total": len(rows)})


@bp.route("/api/operativo/garantias/<int:garantia_id>", methods=["GET"])
def obtener_garantia(garantia_id):
    db = get_db()
    row = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Garantía no encontrada"}), 404
    return jsonify(_garantia_dict(db, row))


@bp.route("/api/operativo/garantias/<int:garantia_id>", methods=["PUT"])
def editar_garantia(garantia_id):
    db = get_db()
    row = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Garantía no encontrada"}), 404
    data = request.get_json(force=True, silent=True) or {}

    estado = row["estado"]
    if "estado" in data:
        estado = (data.get("estado") or "").strip().lower()
        if estado not in ESTADOS_GARANTIA:
            return jsonify({"error": f"Estado inválido. Usa uno de: {', '.join(ESTADOS_GARANTIA)}"}), 400

    fecha_cierre = row["fecha_cierre"]
    if estado in ESTADOS_GARANTIA_CERRADOS and not fecha_cierre:
        fecha_cierre = now_iso()
    elif estado not in ESTADOS_GARANTIA_CERRADOS:
        fecha_cierre = None

    db.execute(
        """
        UPDATE garantias SET motivo = ?, diagnostico = ?, responsable = ?,
            estado = ?, fecha_cierre = ?, actualizado_en = ?
        WHERE id = ?
        """,
        (
            (data.get("motivo", row["motivo"]) or "").strip().upper(),
            (data.get("diagnostico", row["diagnostico"]) or "").strip().upper(),
            (data.get("responsable", row["responsable"]) or "").strip().upper(),
            estado, fecha_cierre, now_iso(), garantia_id,
        ),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    return jsonify(_garantia_dict(db, actualizada))


@bp.route("/api/operativo/garantias/<int:garantia_id>", methods=["DELETE"])
def eliminar_garantia(garantia_id):
    # No existía forma de borrar una garantía completa (solo ítems sueltos
    # dentro de ella) — hace falta para limpiar datos de prueba. Antes de
    # borrar hay que devolver el stock de los ítems de tipo repuesto (igual
    # que al eliminar una orden); garantia_items tiene ON DELETE CASCADE
    # hacia garantias, así que esos se borran solos una vez que se hace el
    # DELETE de la garantía.
    db = get_db()
    _iniciar_transaccion_stock(db)
    garantia = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if garantia is None:
        return jsonify({"error": "Garantía no encontrada"}), 404

    items_repuesto = db.execute(
        "SELECT * FROM garantia_items WHERE garantia_id = ? AND tipo = 'repuesto' AND producto_id IS NOT NULL",
        (garantia_id,),
    ).fetchall()
    for item in items_repuesto:
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (item["producto_id"],)).fetchone()
        if producto is not None:
            _aplicar_entrada_stock(
                db, producto, int(round(item["cantidad"])), item["costo_unitario"],
                motivo=f"DEVOLUCIÓN AL ELIMINAR GARANTÍA #{garantia_id}",
                usuario=usuario_actual(), factura=None, categoria="Devolución",
            )

    db.execute("DELETE FROM garantias WHERE id = ?", (garantia_id,))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/operativo/garantias/<int:garantia_id>/items", methods=["POST"])
def agregar_item_garantia(garantia_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    garantia = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if garantia is None:
        return jsonify({"error": "Garantía no encontrada"}), 404
    if garantia["estado"] in ESTADOS_GARANTIA_CERRADOS:
        return jsonify({"error": f"La garantía está '{garantia['estado']}' y no admite más ítems."}), 409

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
                motivo=f"GARANTÍA #{garantia_id}" + (f" - {descripcion}" if descripcion else ""),
                usuario=usuario, categoria="Garantía",
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
        INSERT INTO garantia_items (
            garantia_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (garantia_id, tipo, producto_id, descripcion, cantidad, costo_unitario, movimiento_id, usuario, ts, detalle, aplica_iva),
    )
    db.execute("UPDATE garantias SET actualizado_en = ? WHERE id = ?", (ts, garantia_id))
    db.commit()
    nuevo_item = dict(db.execute("SELECT * FROM garantia_items WHERE id = ?", (cur.lastrowid,)).fetchone())
    nuevo_item["subtotal"] = _item_subtotal(nuevo_item)
    return jsonify(nuevo_item), 201


@bp.route("/api/operativo/garantias/<int:garantia_id>/items/<int:item_id>", methods=["DELETE"])
def eliminar_item_garantia(garantia_id, item_id):
    db = get_db()
    _iniciar_transaccion_stock(db)
    item = db.execute(
        "SELECT * FROM garantia_items WHERE id = ? AND garantia_id = ?", (item_id, garantia_id)
    ).fetchone()
    if item is None:
        return jsonify({"error": "Ítem no encontrado en esta garantía"}), 404

    if item["tipo"] == "repuesto" and item["producto_id"]:
        producto = db.execute("SELECT * FROM productos WHERE id = ?", (item["producto_id"],)).fetchone()
        if producto is not None:
            _aplicar_entrada_stock(
                db, producto, int(round(item["cantidad"])), item["costo_unitario"],
                motivo=f"DEVOLUCIÓN AL ELIMINAR ÍTEM DE GARANTÍA #{garantia_id}",
                usuario=usuario_actual(), factura=None, categoria="Devolución",
            )

    db.execute("DELETE FROM garantia_items WHERE id = ?", (item_id,))
    db.execute("UPDATE garantias SET actualizado_en = ? WHERE id = ?", (now_iso(), garantia_id))
    db.commit()
    return jsonify({"ok": True})


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
    "control_calidad": "Pruebas de ruta",
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
    total_previo = sum(o["total"] for o in ordenes)
    rango_txt = f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"
    _excel_resumen(ws, "Órdenes de trabajo", [
        ("Rango", rango_txt),
        ("Órdenes", len(ordenes)),
        ("Total facturado", f"${total_previo:,.0f}".replace(",", ".")),
    ])
    fila_encabezado = ws.max_row + 1
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
    _estilo_encabezado(ws, fila=fila_encabezado)
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
    _pdf_resumen(elementos, estilos, [
        ("Órdenes", len(ordenes)),
        ("Total facturado", fmt_money_pdf(total_general)),
    ])
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
    (por responsable y por equipo) — para detectar qué técnico, modelo o
    proveedor genera más reclamos de garantía."""
    desde, hasta = _rango_fechas()
    db = get_db()
    sql = "SELECT * FROM garantias WHERE 1=1"
    params = []
    if desde:
        sql += " AND substr(creado_en, 1, 10) >= ?"
        params.append(desde)
    if hasta:
        sql += " AND substr(creado_en, 1, 10) <= ?"
        params.append(hasta)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()

    detalle = []
    conteo_responsable = {}
    conteo_equipo = {}
    costo_total_general = 0
    for r in rows:
        g = _garantia_dict(db, r)
        equipo_txt = "—"
        if g["equipo"]:
            equipo_txt = " ".join(filter(None, [g["equipo"].get("marca"), g["equipo"].get("modelo"), g["equipo"].get("identificador")])) or "—"
        responsable = g["responsable"] or "Sin asignar"
        conteo_responsable[responsable] = conteo_responsable.get(responsable, 0) + 1
        conteo_equipo[equipo_txt] = conteo_equipo.get(equipo_txt, 0) + 1
        costo_total_general += g["total"]
        detalle.append([
            (g["creado_en"] or "")[:10],
            g["cliente"]["nombre"] if g["cliente"] else "—",
            equipo_txt,
            responsable,
            g["motivo"],
            g["estado"].replace("_", " ").capitalize(),
            round(g["total"], 2),
        ])

    wb = _crear_libro_excel()
    ws = wb.active
    ws.title = "Garantías"
    rango_txt = f"{desde or '(sin límite)'} a {hasta or '(sin límite)'}"
    _excel_resumen(ws, "Garantías", [
        ("Rango", rango_txt),
        ("Garantías", len(detalle)),
        ("Costo total", f"${costo_total_general:,.0f}".replace(",", ".")),
    ])
    fila_encabezado = ws.max_row + 1
    ws.append(["Fecha", "Cliente", "Equipo", "Responsable", "Motivo", "Estado", "Costo (repuestos/mano de obra)"])
    for fila in detalle:
        ws.append(fila)
    ws.append([])
    ws.append(["", "", "", "", "", "TOTAL", round(costo_total_general, 2)])
    _estilo_encabezado(ws, fila=fila_encabezado)
    _autoancho(ws)

    ws2 = wb.create_sheet("Resumen por responsable")
    ws2.append(["Responsable", "Cantidad de garantías"])
    for responsable, cantidad in sorted(conteo_responsable.items(), key=lambda x: -x[1]):
        ws2.append([responsable, cantidad])
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


@bp.route("/api/operativo/reportes/resumen", methods=["GET"])
def reportes_operativo_resumen():
    """Contadores rápidos de órdenes y garantías, para mostrarlos en la
    pestaña Reportes del módulo Operativo ANTES de descargar el archivo."""
    db = get_db()

    ordenes_desde = (request.args.get("ordenes_desde") or "").strip()
    ordenes_hasta = (request.args.get("ordenes_hasta") or "").strip()
    ordenes = _ordenes_para_reporte(ordenes_desde, ordenes_hasta)

    garantias_desde = (request.args.get("garantias_desde") or "").strip()
    garantias_hasta = (request.args.get("garantias_hasta") or "").strip()
    sql = "SELECT COUNT(*) FROM garantias WHERE 1=1"
    params = []
    if garantias_desde:
        sql += " AND substr(creado_en, 1, 10) >= ?"
        params.append(garantias_desde)
    if garantias_hasta:
        sql += " AND substr(creado_en, 1, 10) <= ?"
        params.append(garantias_hasta)
    total_garantias = db.execute(sql, params).fetchone()[0]

    return jsonify({
        "ordenes": {"total": len(ordenes), "total_facturado": round(sum(o["total"] for o in ordenes), 2)},
        "garantias": {"total": total_garantias},
    })
