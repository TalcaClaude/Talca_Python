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
import os
import re
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request, send_file

from core import (
    DB_PATH,
    IVA_PORCENTAJE,
    _aplicar_entrada_stock,
    _aplicar_salida_stock,
    _autoancho,
    _crear_libro_excel,
    _estilo_encabezado,
    _excel_response,
    _excel_resumen,
    _iniciar_transaccion_correlativo,
    _iniciar_transaccion_stock,
    _pdf_documento,
    _pdf_response,
    _pdf_resumen,
    _pdf_tabla,
    _rango_fechas,
    _reconstruir_tabla_legada_si_hace_falta,
    _reparar_fk_hacia_tabla_renombrada,
    coincide_busqueda,
    fmt_money_pdf,
    formatear_rut,
    get_db,
    now_iso,
    requiere_rol,
    sumar_dias_iso,
    usuario_actual,
)
import modulos.convertidores as convertidores

bp = Blueprint("operativo", __name__)

ESTADOS_ORDEN = [
    "recibido", "diagnostico", "presupuesto", "esperando_aprobacion", "reparacion",
    "esperando_repuesto", "control_calidad", "listo", "entregado", "cancelado",
]
# Estados que cuentan como "la orden ya terminó" — no deberían poder seguir
# recibiendo repuestos o mano de obra nueva sin primero reabrirlas.
ESTADOS_CERRADOS = {"entregado", "cancelado"}

# Estados que están MÁS ADELANTE que "esperando_aprobacion" en el flujo normal
# de una OT. Se usan para que la transición automática "presupuesto enviado
# -> esperando_aprobacion" no empuje HACIA ATRÁS una orden que ya avanzó (ej.
# si se reenvía el presupuesto por correo después de que el cliente ya
# aprobó y el auto ya está en reparación, o incluso ya entregado).
ESTADOS_POSTERIORES_A_APROBACION = {
    "reparacion", "esperando_repuesto", "control_calidad", "listo", "entregado", "cancelado",
}

# Secuencia "normal" de una OT para el stepper del seguimiento público — sin
# "cancelado", que no es un paso sino un estado terminal alternativo (se
# muestra aparte, con su propio mensaje).
PASOS_SEGUIMIENTO_PUBLICO = [e for e in ESTADOS_ORDEN if e != "cancelado"]

TIPOS_ITEM = ["repuesto", "mano_obra"]
# Solo válido para ítems de orden_items (OT de un auto), no para
# garantia_items ni convertidor_items -- ver modulos/convertidores.py:
# agregar_convertidor_a_orden().
TIPOS_ITEM_ORDEN = TIPOS_ITEM + ["convertidor_torque"]

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
# IVA_PORCENTAJE se importa de core.py (ver arriba) — compartido con
# modulos/finanzas.py para que ambos calculen el mismo total de una OT.

# Cuentas por Cobrar (CxC) — plazo por defecto para pagar una OT entregada,
# usado solo para calcular 'fecha_vencimiento' al generar la CxC (ver
# _generar_o_actualizar_cxc). No es un plazo legal ni contractual, es solo
# el criterio con el que el taller quiere que el sistema marque una deuda
# de cliente como "vencida" en los reportes — se puede ajustar acá si el
# taller define otro plazo estándar más adelante.
CXC_DIAS_VENCIMIENTO_DEFECTO = 30

# Estados que disparan la generación de la CxC "cerrada" (ver
# _generar_o_actualizar_cxc). Antes solo era 'entregado'; ahora también
# 'listo' — el taller pidió que la vista "CxC OTs cerradas" (Finanzas)
# muestre el auto desde que queda listo para retirar, no recién cuando ya
# se entregó físicamente. Es idempotente (ver _generar_o_actualizar_cxc),
# así que si una orden pasa primero por 'listo' y luego por 'entregado' no
# se genera ni se pisa una segunda vez.
ESTADOS_QUE_CIERRAN_CXC = {"listo", "entregado"}


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

# ---------------------------------------------------------------------------
# Fotos de recepción del vehículo (OTs y garantías) -- ver sección "Fotos de
# vehículos" más abajo. 7 categorías obligatorias al ingresar un auto
# (definidas a pedido del usuario); "libre" es para fotos adicionales
# (daños, rayones, lo que el personal estime) con un título escrito a mano.
# ---------------------------------------------------------------------------
FOTOS_CATEGORIAS_OBLIGATORIAS = [
    ("frente", "Frente"),
    ("costado_derecho", "Costado derecho"),
    ("costado_izquierdo", "Costado izquierdo"),
    ("atras", "Atrás"),
    ("parabrisas", "Parabrisas"),
    ("tablero", "Tablero"),
    ("vin", "VIN"),
]
FOTOS_CATEGORIAS_OBLIGATORIAS_SET = {c for c, _ in FOTOS_CATEGORIAS_OBLIGATORIAS}
FOTOS_CATEGORIA_LABEL = dict(FOTOS_CATEGORIAS_OBLIGATORIAS)
FOTOS_CATEGORIA_LABEL["libre"] = "Foto adicional"
FOTOS_ENTIDADES_VALIDAS = {"orden", "garantia"}

# Qué subconjunto de fotos mandar al cliente al presionar "Enviar registro
# fotográfico" (ver _finalizar_fotos_generico) -- por defecto se mandan
# ambas tandas (comportamiento histórico, antes de que existiera este
# filtro), pero el personal puede pedir solo una si, por ejemplo, ya se
# mandaron las de ingreso hace días y ahora solo quiere reenviar las de
# desarme/armado que llegaron por WhatsApp mientras se trabajaba el auto.
FILTROS_ENVIO_FOTOS = ["ingreso", "desarme_armado", "ambas"]

# Convertidores de torque ahora es su propio módulo (ver
# modulos/convertidores.py) -- lo único que queda acá es el enganche hacia
# orden_items (convertidor_id, ver init_operativo_db más abajo y
# agregar_item_orden), porque instalar un convertidor en la OT de un auto
# sigue siendo una acción sobre la OT del auto, no sobre el convertidor.

# Campos "simples" (sin efectos secundarios) que se pueden actualizar desde
# el PUT general de la orden — diagnóstico. Los que sí disparan una
# transición automática de estado (presupuesto_estado, qc_resultado) viven
# en sus propios endpoints más abajo, para que ese efecto secundario quede
# explícito y sea auditable.
#
# "responsable_diagnostico" NO está en esta lista a propósito: no se deja
# escribir con lo que mande el cliente (igual que qc_inspector, ver
# actualizar_qc() más abajo) — siempre queda igual a quien tiene la sesión
# iniciada al guardar el diagnóstico (ver editar_orden(), usuario_actual()),
# para que no se pueda registrar un diagnóstico a nombre de otra persona.
CAMPOS_SIMPLES_ORDEN = [
    "sintoma", "diagnostico_tecnico", "falla_detectada", "componentes_danados",
    "mediciones", "resultado_pruebas",
    "referencia", "trabajo_a_realizar",
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
        CREATE INDEX IF NOT EXISTS idx_orden_items_usuario_tipo ON orden_items (usuario, tipo);
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

    # Dirección del cliente (para la vista imprimible de la OT y el envío
    # de documentos oficiales) — no existía en el diseño original.
    columnas_cliente = [c[1] for c in db.execute("PRAGMA table_info(clientes)").fetchall()]
    if "direccion" not in columnas_cliente:
        db.execute("ALTER TABLE clientes ADD COLUMN direccion TEXT DEFAULT ''")
    db.commit()

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
        # Migración histórica: número de OT en la base legacy TalleresTC.
        # Sirve como clave de idempotencia (no volver a crear/duplicar la
        # misma OT si el endpoint de migración se corre más de una vez) y
        # como enlace permanente hacia el sistema antiguo -- ej. para que
        # una garantía o un pago histórico que menciona "OT #7891" pueda
        # encontrar la orden correspondiente ya migrada.
        "legacy_ot_no": "INTEGER",
    }
    for columna, definicion in columnas_nuevas_orden.items():
        if columna not in columnas_orden:
            db.execute(f"ALTER TABLE ordenes_trabajo ADD COLUMN {columna} {definicion}")
    db.commit()
    db.execute("CREATE INDEX IF NOT EXISTS idx_ordenes_legacy_ot_no ON ordenes_trabajo (legacy_ot_no)")
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

    # Repara garantia_items si su llave foránea garantia_id quedó
    # apuntando a 'garantias_legado' en vez de 'garantias' (ver
    # _reparar_fk_hacia_tabla_renombrada arriba para el detalle completo
    # de por qué pasa esto -- se confirmó en producción el 11/07/2026:
    # el 100% de los INSERT nuevos fallaban con 'FOREIGN KEY constraint
    # failed' porque la reconstrucción anterior de 'garantias' dejó a
    # garantia_items apuntando a la tabla vieja). Se corre en cada
    # arranque; si la llave ya está bien, no hace nada.
    _reparar_fk_hacia_tabla_renombrada(
        db,
        "garantia_items",
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
            creado_en TEXT NOT NULL,
            detalle TEXT DEFAULT '',
            aplica_iva INTEGER NOT NULL DEFAULT 0
        );
        """,
        [
            "id", "garantia_id", "tipo", "producto_id", "descripcion",
            "cantidad", "costo_unitario", "movimiento_id", "usuario",
            "creado_en", "detalle", "aplica_iva",
        ],
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_garantia_items_garantia_id ON garantia_items (garantia_id)")
    db.commit()

    # Convertidores de torque vive en su propio módulo (modulos/convertidores.py)
    # desde que se separó a un módulo top-level aparte, pero orden_items.convertidor_id
    # (más abajo) referencia convertidores_torque(id), así que esa tabla
    # tiene que existir ANTES de llegar a ese ALTER TABLE.
    convertidores.init_convertidores_db(db)

    # orden_items.convertidor_id: enganche opcional para cuando el ítem
    # agregado a la OT de un auto ES una unidad específica de convertidor
    # de torque ya reparada (tipo='convertidor_torque'), en vez de un
    # producto normal del inventario -- ver agregar_item_orden().
    columnas_orden_items = [c[1] for c in db.execute("PRAGMA table_info(orden_items)").fetchall()]
    if "convertidor_id" not in columnas_orden_items:
        db.execute(
            "ALTER TABLE orden_items ADD COLUMN convertidor_id INTEGER REFERENCES convertidores_torque(id)"
        )
        db.commit()
    db.execute("CREATE INDEX IF NOT EXISTS idx_orden_items_convertidor_id ON orden_items (convertidor_id)")
    db.commit()

    # Repara orden_items si su llave foránea convertidor_id quedó
    # apuntando a 'convertidores_torque_legado...' en vez de
    # 'convertidores_torque' (mismo mecanismo que ya se documentó arriba
    # para garantia_items -> garantias -- ver _reparar_fk_hacia_tabla_renombrada
    # en core.py). Se confirmó en producción el 14/07/2026: mover un
    # convertidor 'interno' a 'entregado' guardaba el cambio de estado
    # pero el auto-cargo a la OT de origen fallaba con 'FOREIGN KEY
    # constraint failed' al insertar en orden_items -- exactamente esta
    # causa. Se corre en cada arranque; si la llave ya está bien, no hace
    # nada.
    sql_orden_items_actual = """
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
            creado_en TEXT NOT NULL,
            detalle TEXT DEFAULT '',
            aplica_iva INTEGER NOT NULL DEFAULT 0,
            convertidor_id INTEGER REFERENCES convertidores_torque(id)
        );
    """
    _reparar_fk_hacia_tabla_renombrada(
        db,
        "orden_items",
        sql_orden_items_actual,
        [
            "id", "orden_id", "tipo", "producto_id", "descripcion", "cantidad",
            "costo_unitario", "movimiento_id", "usuario", "creado_en",
            "detalle", "aplica_iva", "convertidor_id",
        ],
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_orden_items_orden_id ON orden_items (orden_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_orden_items_producto_id ON orden_items (producto_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_orden_items_usuario_tipo ON orden_items (usuario, tipo)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_orden_items_convertidor_id ON orden_items (convertidor_id)")
    db.commit()

    # garantia_items.convertidor_id: mismo enganche que orden_items.convertidor_id
    # de arriba, pero para cuando el auto que trae el convertidor entró por
    # una garantía en vez de una OT normal (origen_tipo == 'desde_garantia',
    # ver modulos/convertidores.py:_cargar_convertidor_interno_en_garantia_origen).
    # Tiene que agregarse ACÁ, después de convertidores.init_convertidores_db(),
    # por la misma razón que orden_items.convertidor_id -- referencia
    # convertidores_torque(id).
    columnas_garantia_items = [c[1] for c in db.execute("PRAGMA table_info(garantia_items)").fetchall()]
    if "convertidor_id" not in columnas_garantia_items:
        db.execute(
            "ALTER TABLE garantia_items ADD COLUMN convertidor_id INTEGER REFERENCES convertidores_torque(id)"
        )
        db.commit()
    db.execute("CREATE INDEX IF NOT EXISTS idx_garantia_items_convertidor_id ON garantia_items (convertidor_id)")
    db.commit()

    # Repara garantia_items si su llave foránea convertidor_id quedó
    # apuntando a 'convertidores_torque_legado...' -- mismo mecanismo que
    # ya se documentó arriba para orden_items.convertidor_id. Se corre en
    # cada arranque; si la llave ya está bien, no hace nada.
    sql_garantia_items_actual = """
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
            creado_en TEXT NOT NULL,
            detalle TEXT DEFAULT '',
            aplica_iva INTEGER NOT NULL DEFAULT 0,
            convertidor_id INTEGER REFERENCES convertidores_torque(id)
        );
    """
    _reparar_fk_hacia_tabla_renombrada(
        db,
        "garantia_items",
        sql_garantia_items_actual,
        [
            "id", "garantia_id", "tipo", "producto_id", "descripcion", "cantidad",
            "costo_unitario", "movimiento_id", "usuario", "creado_en",
            "detalle", "aplica_iva", "convertidor_id",
        ],
    )
    db.commit()

    # Fotos de recepción/daños del vehículo (OTs y garantías) -- ver
    # sección "Fotos de vehículos" más abajo. 'entidad_tipo' + 'entidad_id'
    # en vez de dos columnas separadas (orden_id/garantia_id) porque una
    # foto siempre pertenece a UNA sola de las dos, nunca ambas, y así no
    # queda una columna siempre NULL según el caso (mismo criterio que
    # 'origen_tipo' en convertidores_torque).
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS fotos_vehiculo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entidad_tipo TEXT NOT NULL,
            entidad_id INTEGER NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'libre',
            titulo TEXT DEFAULT '',
            archivo TEXT NOT NULL,
            origen TEXT NOT NULL DEFAULT 'ingreso',
            usuario TEXT DEFAULT '',
            creado_en TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fotos_vehiculo_entidad
            ON fotos_vehiculo (entidad_tipo, entidad_id);
        """
    )
    db.commit()

    # Con la FK ya reparada, reintenta el auto-cargo de cualquier
    # convertidor 'interno' que haya quedado 'entregado' sin su ítem
    # correspondiente en la OT/garantía de origen (ver
    # convertidores.reparar_cargas_internas_pendientes -- idempotente).
    convertidores.reparar_cargas_internas_pendientes(db)

    # Corrige costos congelados/precio_venta residual de convertidores
    # internos que pudieron quedar mal calculados con una versión anterior
    # del código (ver convertidores.reparar_costos_congelados_convertidores_internos
    # -- idempotente).
    convertidores.reparar_costos_congelados_convertidores_internos(db)


# ---------------------------------------------------------------------------
# Fotos de vehículos (OTs y garantías)
# ---------------------------------------------------------------------------
#
# Al ingresar un auto se exigen 7 fotos fijas (FOTOS_CATEGORIAS_OBLIGATORIAS)
# más las libres que el personal quiera agregar (daños, rayones, etc., con
# título propio). Se guardan en disco (mismo volumen persistente que la
# base de datos, ver DEPLOY.md) comprimidas a WebP -- una foto de celular
# sin comprimir pesa 3-6 MB; así queda en ~100-250 KB sin pérdida visible
# para este uso, para que el disco alcance para años de fotos. La tabla usa
# 'entidad_tipo' + 'entidad_id' genérico (en vez de columnas separadas
# orden_id/garantia_id) porque una foto es de una OT O de una garantía,
# nunca ambas -- mismo criterio que 'origen_tipo' en convertidores_torque.

def _fotos_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "fotos_vehiculos")
    os.makedirs(d, exist_ok=True)
    return d


def _comprimir_foto(imagen_bytes, max_lado=1600, calidad=78):
    """Redimensiona (lado más largo a max_lado px) y comprime a WebP.
    Lanza ValueError si la imagen no se puede leer (formato no soportado,
    archivo corrupto, no es una imagen) -- el llamador lo traduce a un 400
    en vez de dejar pasar un 500."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ValueError(
            "Falta instalar la librería Pillow en el servidor para procesar fotos"
        ) from exc
    import io as _io
    try:
        img = Image.open(_io.BytesIO(imagen_bytes))
        # Respeta la orientación real de la foto: los celulares suelen
        # guardar la rotación en metadata EXIF en vez de rotar los píxeles.
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"No se pudo leer la imagen: {exc}")

    ancho, alto = img.size
    lado_mayor = max(ancho, alto)
    if lado_mayor > max_lado:
        factor = max_lado / lado_mayor
        img = img.resize(
            (max(1, round(ancho * factor)), max(1, round(alto * factor))), Image.LANCZOS
        )

    salida = _io.BytesIO()
    img.save(salida, format="WEBP", quality=calidad, method=4)
    return salida.getvalue()


def _guardar_foto_entidad(db, entidad_tipo, entidad_id, categoria, titulo, imagen_bytes, usuario, origen="ingreso"):
    if entidad_tipo not in FOTOS_ENTIDADES_VALIDAS:
        raise ValueError("Tipo de entidad inválido")
    comprimida = _comprimir_foto(imagen_bytes)
    ts = now_iso()
    nombre_archivo = f"{entidad_tipo}_{entidad_id}_{uuid.uuid4().hex}.webp"
    ruta_absoluta = os.path.join(_fotos_dir(), nombre_archivo)
    with open(ruta_absoluta, "wb") as f:
        f.write(comprimida)
    cur = db.execute(
        """
        INSERT INTO fotos_vehiculo
            (entidad_tipo, entidad_id, categoria, titulo, archivo, origen, usuario, creado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entidad_tipo, entidad_id, categoria or "libre", (titulo or "").strip().upper(),
            nombre_archivo, origen, usuario, ts,
        ),
    )
    db.commit()
    fila = db.execute("SELECT * FROM fotos_vehiculo WHERE id = ?", (cur.lastrowid,)).fetchone()
    d = dict(fila)
    d["categoria_label"] = FOTOS_CATEGORIA_LABEL.get(d["categoria"], d["categoria"])
    return d


def _listar_fotos_entidad(db, entidad_tipo, entidad_id):
    filas = db.execute(
        "SELECT * FROM fotos_vehiculo WHERE entidad_tipo = ? AND entidad_id = ? ORDER BY creado_en",
        (entidad_tipo, entidad_id),
    ).fetchall()
    resultado = []
    for f in filas:
        d = dict(f)
        d["categoria_label"] = FOTOS_CATEGORIA_LABEL.get(d["categoria"], d["categoria"])
        resultado.append(d)
    return resultado


def _fotos_obligatorias_faltantes(db, entidad_tipo, entidad_id):
    presentes = {
        r["categoria"] for r in db.execute(
            "SELECT DISTINCT categoria FROM fotos_vehiculo WHERE entidad_tipo = ? AND entidad_id = ?",
            (entidad_tipo, entidad_id),
        ).fetchall()
    }
    return [label for cat, label in FOTOS_CATEGORIAS_OBLIGATORIAS if cat not in presentes]


def _resolver_entidad_fotos(db, entidad_tipo, entidad_id):
    tabla = "ordenes_trabajo" if entidad_tipo == "orden" else "garantias"
    return db.execute(f"SELECT id FROM {tabla} WHERE id = ?", (entidad_id,)).fetchone()


def _subir_foto_generico(db, entidad_tipo, entidad_id):
    archivo = request.files.get("archivo")
    if not archivo or archivo.filename == "":
        return jsonify({"error": "Selecciona o toma una foto"}), 400
    categoria = (request.form.get("categoria") or "libre").strip().lower()
    if categoria != "libre" and categoria not in FOTOS_CATEGORIAS_OBLIGATORIAS_SET:
        return jsonify({"error": "Categoría de foto inválida"}), 400
    titulo = (request.form.get("titulo") or "").strip()
    if categoria == "libre" and not titulo:
        titulo = "Foto adicional"

    imagen_bytes = archivo.read()
    if not imagen_bytes:
        return jsonify({"error": "El archivo llegó vacío"}), 400
    if len(imagen_bytes) > 15 * 1024 * 1024:
        return jsonify({"error": "La foto es demasiado grande (máximo 15 MB antes de comprimir)"}), 400

    try:
        foto = _guardar_foto_entidad(db, entidad_tipo, entidad_id, categoria, titulo, imagen_bytes, usuario_actual())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(foto), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>/fotos", methods=["GET"])
@requiere_rol("operativo")
def listar_fotos_orden(orden_id):
    db = get_db()
    if _resolver_entidad_fotos(db, "orden", orden_id) is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    return jsonify({
        "fotos": _listar_fotos_entidad(db, "orden", orden_id),
        "faltan_obligatorias": _fotos_obligatorias_faltantes(db, "orden", orden_id),
    })


@bp.route("/api/operativo/ordenes/<int:orden_id>/fotos", methods=["POST"])
@requiere_rol("operativo")
def subir_foto_orden(orden_id):
    db = get_db()
    if _resolver_entidad_fotos(db, "orden", orden_id) is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    return _subir_foto_generico(db, "orden", orden_id)


@bp.route("/api/operativo/garantias/<int:garantia_id>/fotos", methods=["GET"])
@requiere_rol("operativo")
def listar_fotos_garantia(garantia_id):
    db = get_db()
    if _resolver_entidad_fotos(db, "garantia", garantia_id) is None:
        return jsonify({"error": "Garantía no encontrada"}), 404
    return jsonify({
        "fotos": _listar_fotos_entidad(db, "garantia", garantia_id),
        "faltan_obligatorias": _fotos_obligatorias_faltantes(db, "garantia", garantia_id),
    })


@bp.route("/api/operativo/garantias/<int:garantia_id>/fotos", methods=["POST"])
@requiere_rol("operativo")
def subir_foto_garantia(garantia_id):
    db = get_db()
    if _resolver_entidad_fotos(db, "garantia", garantia_id) is None:
        return jsonify({"error": "Garantía no encontrada"}), 404
    return _subir_foto_generico(db, "garantia", garantia_id)


@bp.route("/api/operativo/fotos/<int:foto_id>/archivo", methods=["GET"])
@requiere_rol("operativo")
def servir_foto(foto_id):
    db = get_db()
    foto = db.execute("SELECT * FROM fotos_vehiculo WHERE id = ?", (foto_id,)).fetchone()
    if foto is None:
        return jsonify({"error": "Foto no encontrada"}), 404
    ruta = os.path.join(_fotos_dir(), foto["archivo"])
    if not os.path.exists(ruta):
        return jsonify({"error": "El archivo de la foto ya no existe en el disco"}), 404
    return send_file(ruta, mimetype="image/webp")


@bp.route("/api/operativo/fotos/<int:foto_id>", methods=["DELETE"])
@requiere_rol("operativo")
def eliminar_foto(foto_id):
    db = get_db()
    foto = db.execute("SELECT * FROM fotos_vehiculo WHERE id = ?", (foto_id,)).fetchone()
    if foto is None:
        return jsonify({"error": "Foto no encontrada"}), 404
    ruta = os.path.join(_fotos_dir(), foto["archivo"])
    db.execute("DELETE FROM fotos_vehiculo WHERE id = ?", (foto_id,))
    db.commit()
    try:
        if os.path.exists(ruta):
            os.remove(ruta)
    except OSError:
        pass
    return jsonify({"ok": True})


def _armar_pdf_fotos_ingreso(db, entidad_tipo, entidad_id, equipo, cliente, fotos):
    """Arma (sin construir todavía -- eso lo hace el llamador con
    doc.build) el PDF de registro fotográfico que se manda al cliente al
    finalizar la recepción del vehículo. Mismo patrón que
    _armar_presupuesto_pdf (reportlab, SimpleDocTemplate).

    'entidad_tipo' es 'orden' o 'garantia' -- ambos flujos (recepción de una
    OT nueva, y recepción del vehículo cuando entra por garantía) usan el
    mismo armado de PDF, solo cambia la etiqueta (OT vs Garantía) y de dónde
    sale el cliente/equipo."""
    import io as _io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage

    equipo = equipo or {}
    etiqueta = "OT" if entidad_tipo == "orden" else "Garantía"

    buffer = _io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    estilos = getSampleStyleSheet()
    estilos.add(ParagraphStyle(name="TituloFotosIng", fontSize=18, leading=22, spaceAfter=2, fontName="Helvetica-Bold"))
    estilos.add(ParagraphStyle(name="SubtituloFotosIng", fontSize=10, textColor="#666666", spaceAfter=16))
    estilos.add(ParagraphStyle(name="SeccionFotosIng", fontSize=11, textColor="#1F2430", spaceBefore=14, spaceAfter=6, fontName="Helvetica-Bold"))
    estilos.add(ParagraphStyle(name="PieFotoIng", fontSize=8.5, textColor="#444444", alignment=1, spaceBefore=2, spaceAfter=10))

    elementos = [
        Paragraph("Talleres Calificados", estilos["TituloFotosIng"]),
        Paragraph(
            f"Registro fotográfico de recepción — {etiqueta}{entidad_id} · {datetime.now().strftime('%d-%m-%Y %H:%M')}",
            estilos["SubtituloFotosIng"],
        ),
    ]

    elementos.append(Paragraph("Datos del vehículo", estilos["SeccionFotosIng"]))
    elementos.append(Paragraph(
        f"<b>Cliente:</b> {cliente.get('nombre') or '—'} &nbsp;&nbsp; "
        f"<b>Patente:</b> {equipo.get('identificador') or '—'} &nbsp;&nbsp; "
        f"<b>Marca/Modelo:</b> {equipo.get('marca') or '—'} {equipo.get('modelo') or ''}",
        estilos["Normal"],
    ))
    elementos.append(Spacer(1, 10))

    ancho_foto = 8.0 * cm
    fila_imgs, fila_labels = [], []

    def _cerrar_fila():
        while len(fila_imgs) < 2:
            fila_imgs.append("")
            fila_labels.append("")
        t = Table([list(fila_imgs), list(fila_labels)], colWidths=[ancho_foto + 0.4 * cm] * 2)
        t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        elementos.append(t)

    for foto in fotos:
        ruta = os.path.join(_fotos_dir(), foto["archivo"])
        if not os.path.exists(ruta):
            continue
        try:
            img = RLImage(ruta, width=ancho_foto, height=ancho_foto * 0.75)
        except Exception:
            continue
        etiqueta = Paragraph(foto.get("titulo") or foto.get("categoria_label") or "", estilos["PieFotoIng"])
        fila_imgs.append(img)
        fila_labels.append(etiqueta)
        if len(fila_imgs) == 2:
            _cerrar_fila()
            fila_imgs, fila_labels = [], []
    if fila_imgs:
        _cerrar_fila()

    nombre_cliente = (cliente.get("nombre") or "").title()
    nombre = _nombre_archivo_seguro(f"Fotos Recepcion Talleres Calificados {etiqueta}{entidad_id} {nombre_cliente}").strip()
    nombre = f"{nombre}.pdf"
    return buffer, doc, elementos, nombre


def _finalizar_fotos_generico(db, entidad_tipo, entidad_id, equipo, cliente, incluir_origen="ambas"):
    """Lógica compartida entre 'finalizar fotos de una OT' y 'finalizar
    fotos de una garantía': arma el PDF con las fotos elegidas y lo manda
    por correo al cliente, con copia al taller (PRESUPUESTO_COPIA_TALLER --
    mismo listado de correos administrativos que ya se usaba para el envío
    de presupuestos, para no duplicar esa configuración). A propósito NO
    bloquea el ingreso del auto si el cliente no tiene correo registrado o
    el envío falla -- solo se informa en la respuesta, para que el personal
    pueda reenviarlo después a mano en vez de trabar la recepción física
    del vehículo.

    'incluir_origen' filtra qué tanda de fotos va en el PDF (ver
    FILTROS_ENVIO_FOTOS y 'origen' en fotos_vehiculo):
      - "ingreso": las obligatorias + libres tomadas al recibir el auto
        (todo lo que NO sea origen == 'desarme_armado').
      - "desarme_armado": solo las que llegaron por WhatsApp mientras se
        trabajaba el vehículo.
      - "ambas" (default): todas, igual que el comportamiento histórico
        antes de que existiera este filtro."""
    fotos = _listar_fotos_entidad(db, entidad_tipo, entidad_id)
    if incluir_origen == "ingreso":
        fotos = [f for f in fotos if f.get("origen") != "desarme_armado"]
    elif incluir_origen == "desarme_armado":
        fotos = [f for f in fotos if f.get("origen") == "desarme_armado"]

    if not fotos:
        return {
            "ok": False,
            "correo_enviado": False,
            "correo_detalle": "No hay fotos de ese tipo todavía -- no se mandó nada.",
            "destinatario": None,
        }

    from app import _enviar_correo, PRESUPUESTO_COPIA_TALLER

    etiqueta = "OT" if entidad_tipo == "orden" else "Garantía"
    destinatario = (cliente.get("email") or "").strip()
    correo_enviado = False
    correo_detalle = "Este cliente no tiene un correo registrado."
    if destinatario:
        pdf_bytes = None
        try:
            buffer, doc, elementos, nombre_pdf = _armar_pdf_fotos_ingreso(db, entidad_tipo, entidad_id, equipo, cliente, fotos)
            doc.build(elementos)
            buffer.seek(0)
            pdf_bytes = buffer.getvalue()
        except Exception as exc:
            correo_detalle = f"No se pudo generar el PDF: {exc}"
        if pdf_bytes:
            nombre_cliente = (cliente.get("nombre") or "").title() or "cliente"
            asunto = f"Recepción de tu vehículo — {etiqueta}{entidad_id} — Talleres Calificados"
            cuerpo = (
                f"Hola {nombre_cliente},\n\n"
                f"Te confirmamos la recepción de tu vehículo ({etiqueta}{entidad_id}). Adjuntamos el registro "
                f"fotográfico tomado al momento del ingreso.\n\n"
                f"Ante cualquier consulta, contáctanos.\n\n"
                f"Talleres Calificados"
            )
            correo_enviado, correo_detalle = _enviar_correo(
                destinatario, asunto, cuerpo,
                adjunto_bytes=pdf_bytes, adjunto_nombre=nombre_pdf, cc=PRESUPUESTO_COPIA_TALLER,
            )

    return {
        "ok": True,
        "correo_enviado": correo_enviado,
        "correo_detalle": correo_detalle,
        "destinatario": destinatario or None,
    }


@bp.route("/api/operativo/ordenes/<int:orden_id>/fotos/finalizar", methods=["POST"])
@requiere_rol("operativo")
def finalizar_fotos_orden(orden_id):
    """Cierra la etapa de fotos de recepción de una OT: exige que estén las
    7 fotos obligatorias antes de armar y mandar el registro fotográfico
    (ver _finalizar_fotos_generico). '?incluir=' elige qué tanda mandar
    (ver FILTROS_ENVIO_FOTOS) -- las 7 obligatorias son siempre fotos de
    ingreso, así que ese chequeo no aplica si se pidió mandar solo
    desarme/armado (no tendría sentido bloquear el envío de esas por fotos
    de ingreso que ni se van a mandar)."""
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    incluir = (request.args.get("incluir") or "ambas").strip().lower()
    if incluir not in FILTROS_ENVIO_FOTOS:
        return jsonify({"error": f"Filtro inválido. Usa uno de: {', '.join(FILTROS_ENVIO_FOTOS)}"}), 400

    if incluir != "desarme_armado":
        faltantes = _fotos_obligatorias_faltantes(db, "orden", orden_id)
        if faltantes:
            return jsonify({
                "error": "Faltan fotos obligatorias: " + ", ".join(faltantes),
                "faltan_obligatorias": faltantes,
            }), 400

    orden = _orden_dict_detalle(db, row)
    cliente = orden.get("cliente") or {}
    equipo = orden.get("equipo") or {}
    return jsonify(_finalizar_fotos_generico(db, "orden", orden_id, equipo, cliente, incluir))


@bp.route("/api/operativo/garantias/<int:garantia_id>/fotos/finalizar", methods=["POST"])
@requiere_rol("operativo")
def finalizar_fotos_garantia(garantia_id):
    """Mismo flujo que finalizar_fotos_orden, pero para cuando el vehículo
    entra por garantía -- la garantía tiene su propia OT/proceso (ver
    docstring de 'Garantías como entidad propia' más abajo), así que este
    registro fotográfico de recepción también se hace por separado, con las
    mismas 7 fotos obligatorias. '?incluir=' -- ver finalizar_fotos_orden."""
    db = get_db()
    row = db.execute("SELECT * FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Garantía no encontrada"}), 404

    incluir = (request.args.get("incluir") or "ambas").strip().lower()
    if incluir not in FILTROS_ENVIO_FOTOS:
        return jsonify({"error": f"Filtro inválido. Usa uno de: {', '.join(FILTROS_ENVIO_FOTOS)}"}), 400

    if incluir != "desarme_armado":
        faltantes = _fotos_obligatorias_faltantes(db, "garantia", garantia_id)
        if faltantes:
            return jsonify({
                "error": "Faltan fotos obligatorias: " + ", ".join(faltantes),
                "faltan_obligatorias": faltantes,
            }), 400

    garantia = _garantia_dict(db, row)
    cliente = garantia.get("cliente") or {}
    equipo = garantia.get("equipo") or {}
    return jsonify(_finalizar_fotos_generico(db, "garantia", garantia_id, equipo, cliente, incluir))


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
        "INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas, creado_en) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            nombre,
            (data.get("telefono") or "").strip(),
            (data.get("email") or "").strip(),
            formatear_rut((data.get("rut") or "").strip()),
            (data.get("direccion") or "").strip().upper(),
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
        "UPDATE clientes SET nombre = ?, telefono = ?, email = ?, rut = ?, direccion = ?, notas = ? WHERE id = ?",
        (
            nombre,
            (data.get("telefono", row["telefono"]) or "").strip(),
            (data.get("email", row["email"]) or "").strip(),
            formatear_rut((data.get("rut", row["rut"]) or "").strip()),
            (data.get("direccion", row["direccion"]) or "").strip().upper(),
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
    cliente = db.execute(
        "SELECT id, nombre, telefono, rut, email, direccion FROM clientes WHERE id = ?", (d["cliente_id"],)
    ).fetchone()
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
    # Costo de convertidores de torque internos cargados como repuesto de
    # esta OT (ver modulos/convertidores.py, tipo == 'convertidor_torque').
    # Es plata real que salió del taller (repuestos/mano de obra usados para
    # reparar el convertidor), así que sí debe restar de la utilidad.
    costo_convertidores = sum(
        i["subtotal"] for i in d["items"] if i["tipo"] == "convertidor_torque"
    )
    d["costo_repuestos"] = round(costo_repuestos, 2)
    d["mano_obra_cargada"] = round(mano_obra_cargada, 2)
    d["costo_convertidores"] = round(costo_convertidores, 2)

    # Gastos de Caja Chica con rubro "Gastos por OT" (Migración 25) ligados
    # a esta orden -- boletas cargadas contra un retiro de caja chica que en
    # realidad son un costo de esta OT puntual (ej. un repuesto comprado en
    # efectivo). Se muestran en la pestaña "Costos" junto a repuestos/mano
    # de obra, con la boleta a la que corresponden, y sí restan de la
    # utilidad estimada -- por eso se excluyen del GAV en el Estado de
    # Resultados (quedarían duplicadas si sumaran en los dos lados).
    boletas_caja_chica = db.execute(
        "SELECT d.id, d.numero_documento, d.monto, d.descripcion, d.gasto_id, g.fecha AS fecha_retiro "
        "FROM caja_gasto_detalles d JOIN caja_movimientos g ON g.id = d.gasto_id "
        "WHERE d.orden_id = ? AND d.rubro_gav = 'gastos_por_ot' ORDER BY g.fecha ASC",
        (d["id"],),
    ).fetchall()
    d["costos_caja_chica"] = [dict(b) for b in boletas_caja_chica]
    costo_caja_chica = sum(b["monto"] for b in boletas_caja_chica)
    d["costo_caja_chica"] = round(costo_caja_chica, 2)

    d["utilidad_estimada"] = round(
        valor_neto - costo_repuestos - mano_obra_cargada - costo_convertidores - costo_caja_chica, 2
    )

    try:
        d["equipamiento"] = json.loads(d["equipamiento_json"]) if d.get("equipamiento_json") else {}
    except (TypeError, ValueError):
        d["equipamiento"] = {}
    return d


# ---------------------------------------------------------------------------
# Cuentas por Cobrar (CxC) — se generan solas al entregar una OT, nunca a
# mano (ver editar_orden y _actualizar_estado_orden_por_agente, los dos
# únicos lugares donde una orden puede pasar a 'entregado'). Viven en la
# tabla cuentas_por_cobrar (ver init_db en app.py); los abonos siguen
# viviendo en pagos_orden, que ya existía — no se duplica esa tabla, solo
# se usa para recalcular 'estado' acá.
# ---------------------------------------------------------------------------

def _generar_o_actualizar_cxc(db, orden_row):
    """Genera la Cuenta por Cobrar de una OT la PRIMERA vez que pasa por
    alguno de ESTADOS_QUE_CIERRAN_CXC ('listo' o 'entregado') — el monto
    queda 'congelado' con el valor_neto + IVA de ese momento (una vez
    lista, la OT ya no debería seguir sumando ítems). Es idempotente: si
    la orden pasa por 'listo' y después por 'entregado' (el camino
    normal), o vuelve a pasar por cualquiera de los dos más de una vez
    (ej. se revirtió el estado a mano), NO genera una segunda CxC ni pisa
    el monto ya emitido — eso evitaría que un vaivén de estados infle por
    accidente la deuda del cliente. Una OT sin precio cargado (valor_total
    = 0) no genera CxC: no tiene sentido una cuenta por cobrar de $0."""
    orden_id = orden_row["id"]
    ya_existe = db.execute("SELECT id FROM cuentas_por_cobrar WHERE orden_id = ?", (orden_id,)).fetchone()
    if ya_existe:
        return

    valor_neto = orden_row["precio_referencial"] or 0
    valor_iva_monto = round(valor_neto * IVA_PORCENTAJE, 2) if orden_row["precio_iva"] else 0.0
    valor_total = round(valor_neto + valor_iva_monto, 2)
    if valor_total <= 0:
        return

    fecha_emision = orden_row["fecha_entrega_real"] or now_iso()
    fecha_vencimiento = sumar_dias_iso(fecha_emision, CXC_DIAS_VENCIMIENTO_DEFECTO)
    ts = now_iso()
    db.execute(
        """
        INSERT INTO cuentas_por_cobrar
            (orden_id, cliente_id, monto_total, fecha_emision, fecha_vencimiento, estado, creado_en, actualizado_en)
        VALUES (?, ?, ?, ?, ?, 'pendiente', ?, ?)
        """,
        (orden_id, orden_row["cliente_id"], valor_total, fecha_emision, fecha_vencimiento, ts, ts),
    )
    _recalcular_estado_cxc(db, orden_id)


def _recalcular_estado_cxc(db, orden_id):
    """Recalcula 'estado' (pendiente/parcial/pagada) de la CxC de una orden
    según lo que de verdad se ha abonado en pagos_orden — se llama después
    de agregar o eliminar un pago (ver agregar_pago_orden/
    eliminar_pago_orden) y al generar la CxC. No hace nada si esa orden
    todavía no tiene una CxC generada (ej. no se ha entregado, o se entregó
    sin precio cargado)."""
    cxc = db.execute("SELECT * FROM cuentas_por_cobrar WHERE orden_id = ?", (orden_id,)).fetchone()
    if cxc is None:
        return
    total_pagado = db.execute(
        "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = ?", (orden_id,)
    ).fetchone()[0]
    if total_pagado <= 0:
        nuevo_estado = "pendiente"
    elif total_pagado >= cxc["monto_total"]:
        nuevo_estado = "pagada"
    else:
        nuevo_estado = "parcial"
    if nuevo_estado != cxc["estado"]:
        db.execute(
            "UPDATE cuentas_por_cobrar SET estado = ?, actualizado_en = ? WHERE id = ?",
            (nuevo_estado, now_iso(), cxc["id"]),
        )


def _manejar_transicion_cxc(db, estado_antes, orden_row_despues):
    """Se llama en cada cambio de estado de una OT para mantener la CxC
    sincronizada con si la orden está de verdad lista para cobrar:
      - Si ENTRA a 'listo'/'entregado': genera la CxC (ver
        _generar_o_actualizar_cxc, ya es idempotente si ya existía).
      - Si SALE de 'listo'/'entregado' hacia atrás (ej. un auto que se
        había marcado listo pero hay que reabrirlo a reparación por un
        defecto encontrado después) Y esa CxC todavía no tiene ningún pago
        registrado: se elimina. No tenía sentido dejar "pendiente de
        cobro" un auto que en la práctica todavía no está listo para
        retirar -- se vuelve a generar sola, con el monto recalculado a
        ese momento (por si cambiaron los ítems durante el reproceso),
        cuando la orden vuelva a llegar a 'listo' o 'entregado'.
      - Si ya tiene algún pago encima (el cliente alcanzó a abonar antes
        de que se reabriera la OT), NO se borra -- se conserva para no
        perder el rastro de esa plata ya cobrada; solo se generará una
        CxC nueva si no quedaba ninguna al reingresar a 'listo'/'entregado'
        (_generar_o_actualizar_cxc ya es un no-op si sigue existiendo)."""
    estado_despues = orden_row_despues["estado"]
    if estado_despues in ESTADOS_QUE_CIERRAN_CXC:
        _generar_o_actualizar_cxc(db, orden_row_despues)
        return
    if estado_antes in ESTADOS_QUE_CIERRAN_CXC and estado_despues not in ESTADOS_QUE_CIERRAN_CXC:
        cxc = db.execute(
            "SELECT id FROM cuentas_por_cobrar WHERE orden_id = ?", (orden_row_despues["id"],)
        ).fetchone()
        if cxc is None:
            return
        total_pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = ?", (orden_row_despues["id"],)
        ).fetchone()[0]
        if total_pagado <= 0:
            db.execute("DELETE FROM cuentas_por_cobrar WHERE id = ?", (cxc["id"],))


@bp.route("/api/admin/cxc/limpiar-huerfanas", methods=["POST"])
@requiere_rol()
def limpiar_cxc_huerfanas():
    """Limpieza de una sola vez (idempotente, se puede correr las veces que
    sea) para las CxC que quedaron "pegadas" de ANTES de que existiera
    _manejar_transicion_cxc: esa función solo actúa en el momento en que una
    OT cambia de estado, así que una orden que ya había salido de
    listo/entregado hacia atrás mientras corría el código viejo se quedó con
    una CxC vigente aunque el auto ya no esté listo para retirar. Este
    endpoint aplica retroactivamente el mismo criterio: borra la CxC de
    cualquier orden que HOY no esté en listo/entregado, siempre que no tenga
    ningún pago registrado en su contra (mismo resguardo que
    _manejar_transicion_cxc: si ya se cobró algo, no se toca)."""
    db = get_db()
    huerfanas = db.execute(
        """
        SELECT cxc.id AS cxc_id, cxc.orden_id AS orden_id, cxc.monto_total AS monto,
               o.estado AS estado_actual
        FROM cuentas_por_cobrar cxc
        JOIN ordenes_trabajo o ON o.id = cxc.orden_id
        WHERE o.estado NOT IN ('listo', 'entregado')
        """
    ).fetchall()

    eliminadas = []
    conservadas_con_pago = []
    for fila in huerfanas:
        total_pagado = db.execute(
            "SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = ?", (fila["orden_id"],)
        ).fetchone()[0]
        if total_pagado > 0:
            conservadas_con_pago.append({"orden_id": fila["orden_id"], "monto": fila["monto"], "total_pagado": total_pagado})
            continue
        db.execute("DELETE FROM cuentas_por_cobrar WHERE id = ?", (fila["cxc_id"],))
        eliminadas.append({"orden_id": fila["orden_id"], "monto": fila["monto"], "estado_actual": fila["estado_actual"]})
    db.commit()

    return jsonify({
        "eliminadas": eliminadas,
        "cantidad_eliminadas": len(eliminadas),
        "conservadas_con_pago": conservadas_con_pago,
    })


def ventas_y_costo_ot_mes(db, prefijo_mes):
    """Ventas y costo de ventas de Operativo (órdenes de trabajo) para UN mes
    cualquiera ('YYYY-MM'), no solo el mes en curso -- es la versión
    parametrizada de lo que antes calculaba resumen_utilidad_mes() solo para
    "hoy". La usan tanto resumen_utilidad_mes() (mes actual, para Panel
    Principal) como el Estado de Resultados de Finanzas (cualquier mes que
    el dueño elija revisar).

    Devuelve monto_cerradas_mes (ventas, Neto + IVA si aplica), y el costo de
    ventas desglosado en sus 3 componentes (repuestos, mano de obra con costo
    cargado, convertidores de torque internos) además del total, para que el
    Estado de Resultados pueda mostrarlos por separado si quiere.
    """
    cerradas_mes = db.execute(
        "SELECT id, precio_referencial, precio_iva FROM ordenes_trabajo "
        "WHERE estado = 'entregado' AND substr(fecha_entrega_real, 1, 7) = ?",
        (prefijo_mes,),
    ).fetchall()
    ids_cerradas_mes = [r["id"] for r in cerradas_mes]

    def _valor_total_orden(row):
        neto = row["precio_referencial"] or 0
        iva_monto = round(neto * IVA_PORCENTAJE, 2) if row["precio_iva"] else 0.0
        return round(neto + iva_monto, 2)

    monto_cerradas_mes = round(sum(_valor_total_orden(r) for r in cerradas_mes), 2)

    if not ids_cerradas_mes:
        return {
            "monto_cerradas_mes": monto_cerradas_mes,
            "costo_repuestos": 0.0, "costo_mano_obra": 0.0, "costo_convertidores": 0.0,
            "costo_caja_chica": 0.0, "costo_total": 0.0, "utilidad_mes": 0.0,
        }

    _iva_case = f"(CASE WHEN aplica_iva = 1 THEN 1 + {IVA_PORCENTAJE} ELSE 1 END)"
    placeholders = ", ".join("?" for _ in ids_cerradas_mes)

    costo_repuestos = db.execute(
        f"SELECT COALESCE(SUM(cantidad * costo_unitario), 0) AS total FROM orden_items "
        f"WHERE tipo = 'repuesto' AND orden_id IN ({placeholders})",
        ids_cerradas_mes,
    ).fetchone()["total"] or 0
    costo_mano_obra = db.execute(
        f"SELECT COALESCE(SUM(cantidad * costo_unitario * {_iva_case}), 0) AS total FROM orden_items "
        f"WHERE tipo = 'mano_obra' AND orden_id IN ({placeholders})",
        ids_cerradas_mes,
    ).fetchone()["total"] or 0
    costo_convertidores = db.execute(
        f"SELECT COALESCE(SUM(cantidad * costo_unitario), 0) AS total FROM orden_items "
        f"WHERE tipo = 'convertidor_torque' AND orden_id IN ({placeholders})",
        ids_cerradas_mes,
    ).fetchone()["total"] or 0
    # Gastos de Caja Chica con rubro "Gastos por OT" (Migración 25) ligados
    # a una de estas órdenes -- boletas cargadas contra un retiro de caja
    # chica que en realidad son un costo de ESTA OT puntual, no gasto
    # administrativo. Por eso se excluyen del GAV en el Estado de
    # Resultados (ver finanzas_estado_resultados) y en cambio se suman acá,
    # junto a repuestos/mano de obra/convertidores.
    costo_caja_chica = db.execute(
        f"SELECT COALESCE(SUM(monto), 0) AS total FROM caja_gasto_detalles "
        f"WHERE rubro_gav = 'gastos_por_ot' AND orden_id IN ({placeholders})",
        ids_cerradas_mes,
    ).fetchone()["total"] or 0

    costo_total = round(costo_repuestos + costo_mano_obra + costo_convertidores + costo_caja_chica, 2)
    utilidad_mes = round(monto_cerradas_mes - costo_total, 2)
    return {
        "monto_cerradas_mes": monto_cerradas_mes,
        "costo_repuestos": round(costo_repuestos, 2),
        "costo_mano_obra": round(costo_mano_obra, 2),
        "costo_convertidores": round(costo_convertidores, 2),
        "costo_caja_chica": round(costo_caja_chica, 2),
        "costo_total": costo_total,
        "utilidad_mes": utilidad_mes,
    }


def resumen_utilidad_mes(db):
    """Versión liviana del cálculo de monto_cerradas_mes / utilidad_mes de
    dashboard_operativo(), pensada para que Panel Principal (app.py) pueda
    mostrar "venta total" y "utilidad estimada" del mes sin duplicar la
    lógica de negocio (repuestos + mano de obra con costo + convertidores
    internos se restan del monto cobrado). Debe mantenerse en sincronía con
    dashboard_operativo() -- mismo criterio "mes en curso" para todo.
    """
    hoy = datetime.now()
    prefijo_mes = f"{hoy.year:04d}-{hoy.month:02d}"
    r = ventas_y_costo_ot_mes(db, prefijo_mes)
    return {"monto_cerradas_mes": r["monto_cerradas_mes"], "utilidad_mes": r["utilidad_mes"]}


@bp.route("/api/operativo/dashboard", methods=["GET"])
def dashboard_operativo():
    """KPIs para el dashboard del módulo Operativo:
      - ots_abiertas: órdenes cuyo estado no es un estado cerrado (entregado/cancelado).
      - ots_cerradas_mes: órdenes entregadas cuya fecha_entrega_real cae en el mes calendario actual.
      - monto_cerradas_mes: valor cobrado (Neto + IVA si aplica) de esas órdenes cerradas este mes.
      - monto_por_cerrar: valor cobrado (Neto + IVA si aplica) de todas las órdenes abiertas.
      - costo_ots_cerradas: costo real de los repuestos usados en las órdenes entregadas del mes
        actual (mismo criterio "mes en curso" que el resto del dashboard de Operativo).
      - costo_mano_obra_ots_cerradas: costo real de la mano de obra CON costo unitario cargado
        (típicamente trabajo externo/subcontratado) en las órdenes entregadas del mes actual —
        la mano de obra interna sin costo cargado no aporta acá.
      - costo_convertidores_ots_cerradas: costo real de los convertidores de torque internos
        cargados como repuesto en las órdenes entregadas del mes actual (ver
        modulos/convertidores.py) — plata real que salió del taller para repararlos.
      - costo_garantias: costo real de los repuestos/mano de obra usados en TODAS las garantías
        (histórico) — es dinero que sale sin que se le cobre al cliente.
      - utilidad_mes: monto_cerradas_mes menos el costo de repuestos, de mano de obra (con costo
        cargado) y de convertidores de torque internos de esas mismas órdenes.
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

    # Costo de convertidores de torque internos cargados como repuesto de la
    # OT (ver modulos/convertidores.py) -- plata real que salió del taller
    # para reparar el convertidor propio, así que también resta de la
    # utilidad, igual que repuestos y mano de obra con costo.
    def _suma_costo_convertidores(ids):
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        fila = db.execute(
            f"SELECT COALESCE(SUM(cantidad * costo_unitario), 0) AS total FROM orden_items "
            f"WHERE tipo = 'convertidor_torque' AND orden_id IN ({placeholders})",
            ids,
        ).fetchone()
        return fila["total"] or 0

    # Costo de boletas de Caja Chica rendidas con rubro "Gastos por OT" y
    # ligadas a alguna de las OTs cerradas este mes (ver Migración 25 /
    # caja_gasto_detalles) -- plata real que salió del taller para esa OT,
    # igual criterio que ventas_y_costo_ot_mes.
    def _suma_costo_caja_chica(ids):
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        fila = db.execute(
            f"SELECT COALESCE(SUM(monto), 0) AS total FROM caja_gasto_detalles "
            f"WHERE rubro_gav = 'gastos_por_ot' AND orden_id IN ({placeholders})",
            ids,
        ).fetchone()
        return fila["total"] or 0

    monto_cerradas_mes = round(sum(_valor_total_orden(r) for r in cerradas_mes), 2)
    monto_por_cerrar = round(sum(_valor_total_orden(r) for r in abiertas), 2)
    costo_repuestos_cerradas_mes = _suma_costo_repuestos(ids_cerradas_mes)
    costo_mano_obra_cerradas_mes = _suma_costo_mano_obra(ids_cerradas_mes)
    costo_convertidores_cerradas_mes = _suma_costo_convertidores(ids_cerradas_mes)
    costo_caja_chica_cerradas_mes = _suma_costo_caja_chica(ids_cerradas_mes)
    # Antes eran históricas (todas las OTs entregadas alguna vez); se acotaron
    # al mes en curso para que sigan el mismo criterio "día a día" que el
    # resto de las tarjetas de este dashboard (ver nota del usuario: todo lo
    # de Operativo debe ser mensual salvo CxC/CxP).
    costo_ots_cerradas = round(costo_repuestos_cerradas_mes, 2)
    costo_mano_obra_ots_cerradas = round(costo_mano_obra_cerradas_mes, 2)
    costo_convertidores_ots_cerradas = round(costo_convertidores_cerradas_mes, 2)
    costo_caja_chica_ots_cerradas = round(costo_caja_chica_cerradas_mes, 2)
    utilidad_mes = round(
        monto_cerradas_mes
        - costo_repuestos_cerradas_mes
        - costo_mano_obra_cerradas_mes
        - costo_convertidores_cerradas_mes
        - costo_caja_chica_cerradas_mes,
        2,
    )

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
        "costo_convertidores_ots_cerradas": costo_convertidores_ots_cerradas,
        "costo_caja_chica_ots_cerradas": costo_caja_chica_ots_cerradas,
        "costo_garantias": round(costo_garantias, 2),
        "utilidad_mes": utilidad_mes,
        "ticket_promedio_mes": ticket_promedio_mes,
        "tiempo_promedio_taller_mes": tiempo_promedio_taller_mes,
        "tasa_aprobacion_presupuestos": tasa_aprobacion_presupuestos,
        "costo_garantias_mes": round(costo_garantias_mes, 2),
        "costo_garantias_pct_ingresos_mes": costo_garantias_pct_ingresos_mes,
    })


@bp.route("/api/operativo/dashboard/graficos", methods=["GET"])
def dashboard_operativo_graficos():
    """Datos para los gráficos del dashboard de Operativo (separado del
    endpoint de KPIs de arriba porque son consultas más pesadas y no todas
    las pantallas que usan los KPIs necesitan también los gráficos):
      - tendencia_mensual: OT recibidas y su valor referencial, últimos 12
        meses calendario, por fecha_recepcion (siempre rolling 12 meses,
        no se ve afectado por ?mes -- para eso está Panel Principal). Es una
        CANTIDAD (nº de OT), no un monto en pesos -- ver tooltipValueFmt en
        el frontend (cargarGraficosDashboardOperativo).
      - distribucion_estado: cuántas OT hay en cada estado del flujo, con el
        mismo criterio "mes en curso" que el resto de Operativo -- los
        estados abiertos (todavía no entregado/cancelado) muestran SIEMPRE
        el total completo sin importar el mes (una OT abierta no tiene
        "mes", está abierta ahora); los estados cerrados (entregado,
        cancelado) se acotan al mes elegido con ?mes=. Coherente con
        listar_ordenes() más abajo, que usa exactamente este mismo criterio.
      - top_marcas / top_modelos / ranking_clientes: acotados al mes
        elegido con ?mes=YYYY-MM (por defecto el mes en curso) -- Operativo
        es la vista del día a día; el ranking histórico de clientes vive en
        Panel Principal. Se puede pedir ?mes=todos para ver el histórico acá
        también si hace falta. Cada marca de top_marcas trae además sus 3
        modelos más frecuentes (modelos_top) y cada modelo de top_modelos
        trae la cantidad del mes anterior y el promedio de los últimos 12
        meses, para poder mostrar una tendencia (sube/baja/estable) sin
        duplicar en el tooltip el mismo dato que ya se ve en la barra.
      - tasa_garantias_mensual: garantías creadas / OT entregadas, por mes
        (rolling 12 meses, igual que tendencia_mensual).
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
        fila = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(precio_referencial), 0) FROM ordenes_trabajo "
            "WHERE substr(fecha_recepcion, 1, 7) = ?",
            (pref,),
        ).fetchone()
        tendencia.append({"mes": pref, "cantidad": fila[0], "monto": fila[1]})

    mes_filtro = (request.args.get("mes") or "").strip() or f"{hoy.year:04d}-{hoy.month:02d}"
    filtro_mes_sql = ""
    filtro_mes_params = []
    if mes_filtro != "todos":
        filtro_mes_sql = " AND substr(o.fecha_recepcion, 1, 7) = ?"
        filtro_mes_params = [mes_filtro]

    if mes_filtro == "todos":
        distribucion_estado = [
            {"estado": e, "label": ESTADOS_LABEL.get(e, e), "cantidad": db.execute(
                "SELECT COUNT(*) FROM ordenes_trabajo WHERE estado = ?", (e,)
            ).fetchone()[0]}
            for e in ESTADOS_ORDEN
        ]
    else:
        distribucion_estado = []
        for e in ESTADOS_ORDEN:
            if e in ESTADOS_CERRADOS:
                cantidad = db.execute(
                    "SELECT COUNT(*) FROM ordenes_trabajo WHERE estado = ? AND substr(fecha_entrega_real, 1, 7) = ?",
                    (e, mes_filtro),
                ).fetchone()[0]
            else:
                cantidad = db.execute(
                    "SELECT COUNT(*) FROM ordenes_trabajo WHERE estado = ?", (e,)
                ).fetchone()[0]
            distribucion_estado.append({"estado": e, "label": ESTADOS_LABEL.get(e, e), "cantidad": cantidad})

    top_marcas_rows = db.execute(
        f"""
        SELECT e.marca AS marca, COUNT(*) AS cantidad
        FROM ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id
        WHERE e.marca != ''{filtro_mes_sql}
        GROUP BY e.marca ORDER BY cantidad DESC LIMIT 8
        """,
        filtro_mes_params,
    ).fetchall()

    def _modelos_top_de_marca(marca):
        filas = db.execute(
            f"""
            SELECT e.modelo AS modelo, COUNT(*) AS cantidad
            FROM ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id
            WHERE e.marca = ? AND e.modelo != ''{filtro_mes_sql}
            GROUP BY e.modelo ORDER BY cantidad DESC LIMIT 3
            """,
            [marca] + filtro_mes_params,
        ).fetchall()
        return [{"modelo": f["modelo"], "cantidad": f["cantidad"]} for f in filas]

    top_marcas = [
        {"marca": r["marca"], "cantidad": r["cantidad"], "modelos_top": _modelos_top_de_marca(r["marca"])}
        for r in top_marcas_rows
    ]

    # Modelos de auto que más se reparan (marca + modelo juntos, ej. "FORD
    # RANGER"), a diferencia de top_marcas que solo agrupa por marca.
    top_modelos_rows = db.execute(
        f"""
        SELECT e.marca AS marca, e.modelo AS modelo, COUNT(*) AS cantidad
        FROM ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id
        WHERE e.modelo != ''{filtro_mes_sql}
        GROUP BY e.marca, e.modelo ORDER BY cantidad DESC LIMIT 8
        """,
        filtro_mes_params,
    ).fetchall()

    mes_anterior_dt = (cursor_mes - timedelta(days=1)).replace(day=1)
    pref_mes_anterior = f"{mes_anterior_dt.year:04d}-{mes_anterior_dt.month:02d}"
    hace_12_meses = meses[0]  # ya viene ordenado ascendente arriba

    def _tendencia_modelo(marca, modelo):
        cantidad_mes_anterior = db.execute(
            """
            SELECT COUNT(*) FROM ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id
            WHERE e.marca = ? AND e.modelo = ? AND substr(o.fecha_recepcion, 1, 7) = ?
            """,
            (marca, modelo, pref_mes_anterior),
        ).fetchone()[0]
        total_12m = db.execute(
            """
            SELECT COUNT(*) FROM ordenes_trabajo o JOIN equipos e ON e.id = o.equipo_id
            WHERE e.marca = ? AND e.modelo = ? AND o.fecha_recepcion >= ?
            """,
            (marca, modelo, hace_12_meses.strftime("%Y-%m-%d")),
        ).fetchone()[0]
        promedio_12m = round(total_12m / 12, 1)
        return cantidad_mes_anterior, promedio_12m

    top_modelos = []
    for r in top_modelos_rows:
        cantidad_mes_anterior, promedio_12m = _tendencia_modelo(r["marca"], r["modelo"])
        if r["cantidad"] > cantidad_mes_anterior:
            tendencia_dir = "sube"
        elif r["cantidad"] < cantidad_mes_anterior:
            tendencia_dir = "baja"
        else:
            tendencia_dir = "estable"
        top_modelos.append({
            "modelo": f"{r['marca']} {r['modelo']}".strip(),
            "cantidad": r["cantidad"],
            "cantidad_mes_anterior": cantidad_mes_anterior,
            "promedio_12m": promedio_12m,
            "tendencia": tendencia_dir,
        })

    ranking_clientes = [
        {"nombre": r["nombre"], "cantidad": r["cantidad"], "monto": r["monto"]}
        for r in db.execute(
            f"""
            SELECT c.nombre AS nombre, COUNT(*) AS cantidad,
                   COALESCE(SUM(o.precio_referencial), 0) AS monto
            FROM ordenes_trabajo o JOIN clientes c ON c.id = o.cliente_id
            WHERE 1=1{filtro_mes_sql}
            GROUP BY c.id ORDER BY monto DESC LIMIT 8
            """,
            filtro_mes_params,
        ).fetchall()
    ]

    tasa_garantias = []
    for m in meses:
        pref = f"{m.year:04d}-{m.month:02d}"
        entregadas = db.execute(
            "SELECT COUNT(*) FROM ordenes_trabajo WHERE estado = 'entregado' AND substr(fecha_entrega_real, 1, 7) = ?",
            (pref,),
        ).fetchone()[0]
        garantias_creadas = db.execute(
            "SELECT COUNT(*) FROM garantias WHERE substr(creado_en, 1, 7) = ?",
            (pref,),
        ).fetchone()[0]
        pct = round(garantias_creadas / entregadas * 100, 1) if entregadas else 0
        tasa_garantias.append({"mes": pref, "pct": pct, "garantias_creadas": garantias_creadas, "entregadas": entregadas})

    return jsonify({
        "tendencia_mensual": tendencia,
        "distribucion_estado": distribucion_estado,
        "top_marcas": top_marcas,
        "top_modelos": top_modelos,
        "ranking_clientes": ranking_clientes,
        "tasa_garantias_mensual": tasa_garantias,
        "mes_filtrado": mes_filtro,
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
    """Con la migración histórica (~3.000 OT cerradas) el listado sin filtro
    dejaría de ser útil para el día a día -- Operativo es para el mes a mes,
    el historial completo vive en Panel Principal (gráficos agregados) y se
    puede consultar acá igual filtrando por cliente/equipo o pidiendo
    explícitamente ?mes=todos. Reglas:
      - Las OT ABIERTAS (no en ESTADOS_CERRADOS) siempre se listan todas,
        sin importar el mes -- son trabajo activo, no historial.
      - Las OT CERRADAS se filtran por el mes de fecha_entrega_real. Por
        defecto es el mes en curso; se puede pedir otro con ?mes=YYYY-MM, o
        ?mes=todos para traer el historial completo cerrado (ej. al buscar
        por cliente_id/equipo_id, donde tiene sentido ver todo su historial).
      - Si viene cliente_id o equipo_id explícito, el filtro de mes NO se
        aplica por defecto (se asume que quieres el historial de ESE cliente
        o vehículo, no solo el mes en curso) -- pero sigue pudiéndose forzar
        con ?mes= si se quiere acotar igual.
    """
    db = get_db()
    estado = (request.args.get("estado") or "").strip()
    cliente_id = request.args.get("cliente_id")
    equipo_id = request.args.get("equipo_id")
    mes = (request.args.get("mes") or "").strip()

    if estado and estado not in ESTADOS_ORDEN:
        return jsonify({"error": f"Estado inválido. Usa uno de: {', '.join(ESTADOS_ORDEN)}"}), 400

    if not mes and not cliente_id and not equipo_id:
        mes = f"{datetime.now().year:04d}-{datetime.now().month:02d}"

    sql = "SELECT * FROM ordenes_trabajo WHERE 1=1"
    params = []
    if estado:
        sql += " AND estado = ?"
        params.append(estado)
    if cliente_id:
        sql += " AND cliente_id = ?"
        params.append(cliente_id)
    if equipo_id:
        sql += " AND equipo_id = ?"
        params.append(equipo_id)
    if mes and mes != "todos":
        placeholders_cerrados = ", ".join("?" for _ in ESTADOS_CERRADOS)
        sql += f" AND (estado NOT IN ({placeholders_cerrados}) OR substr(fecha_entrega_real, 1, 7) = ?)"
        params.extend(ESTADOS_CERRADOS)
        params.append(mes)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify({"ordenes": [_orden_dict(db, r) for r in rows], "total": len(rows), "mes_filtrado": mes or "todos"})


def _listado_ordenes_liviano(db, desde=None, hasta=None):
    """Una fila liviana por OT para la pestaña 'Listado' -- a propósito NO
    usa _orden_dict (que trae items/pagos completos con varias queries por
    fila): con el historial completo (~3.000+ cerradas, ver comentario de
    listar_ordenes) haría falta miles de queries si se reusara _orden_dict
    fila por fila. En cambio se resuelve todo con una sola query (con
    subquery de pagos_orden, que ya tiene índice por orden_id).

    'desde'/'hasta' (YYYY-MM-DD, ambos opcionales) acotan por fecha de
    ingreso -- pero SOLO a las OT ya CERRADAS. Las abiertas se traen
    siempre, sin importar la fecha, porque son trabajo activo (mismo
    criterio que usa listar_ordenes con el mes). Así, por defecto el
    frontend manda un 'desde' reciente para no traer los ~3.000+ registros
    del historial completo cada vez que se abre la pestaña, sin por eso
    esconder ninguna OT que siga abierta. Si no viene ni 'desde' ni
    'hasta', no hay filtro de fecha -- se trae todo el historial (lo que
    usa el botón "Ver historial completo" del frontend)."""
    sql = """
        SELECT
            o.id, o.estado, o.fecha_recepcion, o.fecha_entrega_real,
            o.precio_referencial, o.precio_iva,
            c.nombre AS cliente_nombre,
            e.marca, e.modelo, e.color, e.identificador AS patente,
            (SELECT COALESCE(SUM(monto), 0) FROM pagos_orden WHERE orden_id = o.id) AS total_pagado
        FROM ordenes_trabajo o
        LEFT JOIN clientes c ON c.id = o.cliente_id
        LEFT JOIN equipos e ON e.id = o.equipo_id
        WHERE 1=1
    """
    params = []
    if desde or hasta:
        condiciones_fecha = []
        params_fecha = []
        if desde:
            condiciones_fecha.append("substr(o.fecha_recepcion, 1, 10) >= ?")
            params_fecha.append(desde)
        if hasta:
            condiciones_fecha.append("substr(o.fecha_recepcion, 1, 10) <= ?")
            params_fecha.append(hasta)
        placeholders_cerrados = ", ".join("?" for _ in ESTADOS_CERRADOS)
        sql += f" AND (o.estado NOT IN ({placeholders_cerrados}) OR ({' AND '.join(condiciones_fecha)}))"
        params = list(ESTADOS_CERRADOS) + params_fecha
    sql += " ORDER BY o.id DESC"
    rows = db.execute(sql, params).fetchall()
    filas = []
    for r in rows:
        valor_neto = r["precio_referencial"] or 0
        valor_iva_monto = round(valor_neto * IVA_PORCENTAJE, 2) if r["precio_iva"] else 0.0
        valor_total = round(valor_neto + valor_iva_monto, 2)
        filas.append({
            "id": r["id"],
            "tipo": "nuevo",
            "estado": r["estado"],
            "estado_label": ESTADOS_LABEL.get(r["estado"], r["estado"]),
            "cliente_nombre": r["cliente_nombre"],
            "marca": r["marca"], "modelo": r["modelo"], "color": r["color"], "patente": r["patente"],
            "fecha_recepcion": r["fecha_recepcion"],
            # Se reusa el nombre 'fecha_entrega_real' (en vez de un genérico
            # 'fecha_cierre') a propósito, para que el frontend pueda
            # calcular "días en el taller" con la misma función que ya usa
            # el tablero (diasEnTallerNumero), sin tener que tocarla.
            "fecha_entrega_real": r["fecha_entrega_real"],
            "valor_total": valor_total,
            "total_pagado": round(r["total_pagado"] or 0, 2),
        })
    return filas


def _listado_garantias_liviano(db, desde=None, hasta=None):
    """Misma idea que _listado_ordenes_liviano pero para garantías -- estas
    no tienen precio/pagos propios (una garantía no se le cobra al
    cliente), así que 'valor_total'/'total_pagado' quedan en None (el
    frontend los muestra como "—"). 'desde'/'hasta' siguen el mismo
    criterio (solo acotan las cerradas; las abiertas siempre se traen)."""
    sql = """
        SELECT
            g.id, g.estado, g.fecha_recepcion, g.fecha_cierre,
            c.nombre AS cliente_nombre,
            e.marca, e.modelo, e.color, e.identificador AS patente
        FROM garantias g
        LEFT JOIN clientes c ON c.id = g.cliente_id
        LEFT JOIN equipos e ON e.id = g.equipo_id
        WHERE 1=1
    """
    params = []
    if desde or hasta:
        condiciones_fecha = []
        params_fecha = []
        if desde:
            condiciones_fecha.append("substr(g.fecha_recepcion, 1, 10) >= ?")
            params_fecha.append(desde)
        if hasta:
            condiciones_fecha.append("substr(g.fecha_recepcion, 1, 10) <= ?")
            params_fecha.append(hasta)
        placeholders_cerrados = ", ".join("?" for _ in ESTADOS_GARANTIA_CERRADOS)
        sql += f" AND (g.estado NOT IN ({placeholders_cerrados}) OR ({' AND '.join(condiciones_fecha)}))"
        params = list(ESTADOS_GARANTIA_CERRADOS) + params_fecha
    sql += " ORDER BY g.id DESC"
    rows = db.execute(sql, params).fetchall()
    filas = []
    for r in rows:
        filas.append({
            "id": r["id"],
            "tipo": "garantia",
            "estado": r["estado"],
            "estado_label": ESTADOS_GARANTIA_LABEL.get(r["estado"], r["estado"]),
            "cliente_nombre": r["cliente_nombre"],
            "marca": r["marca"], "modelo": r["modelo"], "color": r["color"], "patente": r["patente"],
            "fecha_recepcion": r["fecha_recepcion"],
            "fecha_entrega_real": r["fecha_cierre"],
            "valor_total": None,
            "total_pagado": None,
        })
    return filas


@bp.route("/api/operativo/listado", methods=["GET"])
def listado_operativo():
    """Alimenta la pestaña 'Listado': a diferencia de listar_ordenes (que
    por defecto acota a las OT abiertas + las cerradas del mes en curso,
    pensado para el tablero del día a día), acá se puede traer el
    historial completo -- todas las OT y garantías de siempre, abiertas o
    cerradas, de cualquier mes -- porque la idea de esta vista es poder
    buscar cualquier OT o garantía que se haya ingresado alguna vez (a
    pedido del taller, jul. 2026).

    ?tipo=nuevo      -> solo OT normales (tabla ordenes_trabajo)
    ?tipo=garantia   -> solo garantías (tabla garantias)
    sin ?tipo, o vacío -> ambas mezcladas, más recientes primero

    ?desde=YYYY-MM-DD / ?hasta=YYYY-MM-DD (opcionales) -- acotan por fecha
    de ingreso, pero SOLO a lo ya CERRADO (lo abierto siempre se trae, ver
    _listado_ordenes_liviano/_listado_garantias_liviano). El frontend manda
    un 'desde' reciente por defecto para no traer los ~3.000+ registros del
    historial completo en cada apertura de la pestaña; el botón "Ver
    historial completo" del frontend simplemente no manda ninguno de los
    dos, y acá eso trae todo sin filtrar."""
    db = get_db()
    tipo = (request.args.get("tipo") or "").strip().lower()
    if tipo and tipo not in ("nuevo", "garantia"):
        return jsonify({"error": "Tipo inválido. Usa 'nuevo' o 'garantia'."}), 400
    desde = (request.args.get("desde") or "").strip() or None
    hasta = (request.args.get("hasta") or "").strip() or None

    filas = []
    if tipo != "garantia":
        filas.extend(_listado_ordenes_liviano(db, desde, hasta))
    if tipo != "nuevo":
        filas.extend(_listado_garantias_liviano(db, desde, hasta))
    filas.sort(key=lambda f: f["fecha_recepcion"] or "", reverse=True)
    return jsonify({"filas": filas, "total": len(filas)})


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
    # Bitácora de pruebas de ruta (migración 20) -- historial de entradas,
    # más reciente primero, cada una con fecha/autor propios. A diferencia
    # de qc_checklist, ninguna entrada se sobrescribe ni se resume.
    d["pruebas_ruta"] = [dict(i) for i in db.execute(
        "SELECT * FROM pruebas_ruta WHERE orden_id = ? ORDER BY creado_en DESC", (row["id"],)
    ).fetchall()]
    return d


@bp.route("/api/operativo/ordenes/<int:orden_id>", methods=["GET"])
def obtener_orden(orden_id):
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    return jsonify(_orden_dict_detalle(db, row))


def _asignar_id_correlativo(db):
    """Calcula el próximo id a usar para una OT o una garantía nueva, de
    forma que 'ordenes_trabajo' y 'garantias' compartan un mismo correlativo
    y un número de OT nunca se repita entre una orden normal y una
    garantía -- así el número siempre identifica a un único vehículo, sea
    la OT original o una garantía posterior sobre ese mismo trabajo (a
    pedido del taller, jul. 2026). Los convertidores de torque quedan
    fuera a propósito -- son casi un negocio aparte y ahí sí puede
    repetirse un número con una OT/garantía.

    Debe llamarse DESPUÉS de _iniciar_transaccion_correlativo (BEGIN
    IMMEDIATE) y el INSERT explícito que use este id debe ir en la misma
    transacción -- si no, dos altas casi simultáneas podrían calcular el
    mismo próximo número (ver _iniciar_transaccion_stock en core.py para
    el porqué del lock).

    Las OT y garantías creadas ANTES de este cambio conservan su id
    original tal cual (ya se comunicaron a clientes en papeletas/PDFs/
    WhatsApp), así que sí puede coincidir un número de OT vieja con uno de
    garantía vieja -- la unificación rige solo hacia adelante, no
    reordena ni renumera el historial."""
    max_ot = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ordenes_trabajo").fetchone()["m"]
    max_gar = db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM garantias").fetchone()["m"]
    return max(max_ot, max_gar) + 1


@bp.route("/api/operativo/ordenes", methods=["POST"])
def crear_orden():
    db = get_db()
    _iniciar_transaccion_correlativo(db)
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
    nuevo_id = _asignar_id_correlativo(db)
    db.execute(
        """
        INSERT INTO ordenes_trabajo (
            id, cliente_id, equipo_id, estado, descripcion_problema, mecanico,
            fecha_recepcion, fecha_entrega_estimada, usuario, creado_en, actualizado_en,
            kilometraje, prioridad, recepcionado_por
        ) VALUES (?, ?, ?, 'recibido', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nuevo_id, cliente_id, equipo_id,
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
    nueva = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (nuevo_id,)).fetchone()
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
    _manejar_transicion_cxc(db, row["estado"], actualizada)
    db.commit()
    return _orden_dict(db, actualizada), None


# Columnas que _actualizar_campos_orden_por_agente() tiene permitido tocar,
# agrupadas por las dos acciones del agente de WhatsApp que las usan (ver
# app.py: CAMPOS_DIAGNOSTICO_AGENTE / CAMPOS_PRUEBA_RUTA_AGENTE y
# _agente_procesar_mensaje). Es una lista blanca fija (no se arma a partir
# de lo que mande la IA) precisamente para que un mensaje de WhatsApp jamás
# pueda terminar escribiendo en una columna arbitraria de la tabla:
# qc_resultado queda deliberadamente afuera de _CAMPOS_QC porque cambiarlo
# sí tiene un efecto secundario (mueve el estado de la OT — ver
# actualizar_qc() más abajo), a diferencia del resto que son solo texto
# libre sin ningún efecto sobre el resto del sistema. responsable_diagnostico
# y qc_inspector SÍ están en la lista blanca porque la función igual los
# escribe (ver _agente_ejecutar_una_accion en app.py) —
# pero solo con el nombre del mecánico autenticado que mandó el mensaje,
# nunca con lo que la IA haya interpretado del texto libre.
_CAMPOS_DIAGNOSTICO_AGENTE = {"sintoma", "diagnostico_tecnico", "diagnostico", "responsable_diagnostico"}
_CAMPOS_QC_AGENTE = {"qc_inspector", "qc_observaciones"}


def _actualizar_campos_orden_por_agente(db, orden_id, campos):
    """Aplica una actualización parcial de texto libre (diagnóstico o
    prueba de ruta) a una orden de trabajo — pensado para el agente de
    WhatsApp (ver app.py: _agente_procesar_mensaje), igual que
    _actualizar_estado_orden_por_agente() de arriba pero para las dos
    acciones 'actualizar_diagnostico' / 'actualizar_prueba_ruta'.

    'campos' es un dict {columna: valor} que SOLO puede contener columnas
    presentes en _CAMPOS_DIAGNOSTICO_AGENTE ∪ _CAMPOS_QC_AGENTE — cualquier
    otra clave se ignora en vez de aplicarse, como defensa en profundidad
    por si algún día se agrega una acción nueva y se pasa el dict
    equivocado. Los valores se normalizan igual que el resto del sistema
    (.strip().upper()) y solo se escriben los campos que el mensaje
    realmente mencionó — un campo omitido deja el valor existente intacto,
    nunca lo vacía.

    Devuelve (orden_dict, None) si funcionó, (None, mensaje_de_error) si
    no — nunca lanza una excepción, mismo criterio que
    _actualizar_estado_orden_por_agente()."""
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return None, f"No existe ninguna orden de trabajo con el número {orden_id}."

    columnas_permitidas = _CAMPOS_DIAGNOSTICO_AGENTE | _CAMPOS_QC_AGENTE
    a_aplicar = {
        columna: (valor or "").strip().upper()
        for columna, valor in (campos or {}).items()
        if columna in columnas_permitidas and (valor or "").strip()
    }
    if not a_aplicar:
        return None, "No encontré ningún dato concreto para guardar en ese mensaje."

    set_clause = ", ".join(f"{columna} = ?" for columna in a_aplicar)
    valores = list(a_aplicar.values()) + [now_iso(), orden_id]
    db.execute(
        f"UPDATE ordenes_trabajo SET {set_clause}, actualizado_en = ? WHERE id = ?",
        valores,
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

    # El responsable del diagnóstico NUNCA se toma de lo que mande el
    # cliente (a diferencia del resto de los campos de arriba) — siempre es
    # quien tiene la sesión iniciada al momento de guardar, igual que
    # usuario_actual() en Caja Chica. Si el guardado no tocó ningún campo
    # de diagnóstico (ej. viene de otra pestaña) se deja el valor que ya
    # había, para no "robarle" la autoría a quien lo hizo antes.
    campos_diagnostico = {"sintoma", "diagnostico_tecnico", "falla_detectada", "componentes_danados",
                           "mediciones", "resultado_pruebas", "diagnostico"}
    if campos_diagnostico & data.keys():
        responsable_diagnostico = usuario_actual().strip().upper()
    else:
        responsable_diagnostico = row["responsable_diagnostico"]

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
            responsable_diagnostico,
            valores_simples["referencia"], valores_simples["trabajo_a_realizar"],
            orden_id,
        ),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    _manejar_transicion_cxc(db, row["estado"], actualizada)
    db.commit()
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
    _recalcular_estado_cxc(db, orden_id)
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
    _recalcular_estado_cxc(db, orden_id)
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Ítems de la orden (repuestos y mano de obra)
# ---------------------------------------------------------------------------

def registrar_salida_directa_a_orden(db, orden_id, producto_id, cantidad, categoria, usuario, motivo, proveedor_id=None):
    """Descarga un producto directamente a una orden de trabajo desde el
    formulario "Registrar movimiento" de Logística (categoría "Consumo
    interno" o "Reserva"), en vez del /movimiento genérico. Queda asociado a
    la OT igual que si se hubiera agregado desde la pestaña de repuestos de
    la orden (aparece en orden_items, afecta el costeo) pero -- a diferencia
    de agregar_item_orden(), que siempre fuerza categoría "Consumo interno"
    -- respeta la categoría real elegida, y usa el costo promedio del
    proveedor elegido a mano (no el CPP general del producto) para que el
    costo que quede en la OT sea el más real posible: útil, por ejemplo, si
    el auto vuelve por garantía y hay que saber exactamente qué repuesto (de
    qué proveedor) se usó. Si la categoría es "Reserva", el ítem queda
    marcado como tal en su detalle, para distinguirlo de un consumo directo.

    Devuelve (item_dict, None) si salió bien, o (None, (mensaje, status)) si
    hubo un error -- quien llama debe propagarlo tal cual (ver
    registrar_movimiento en app.py)."""
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return None, ("No existe una orden de trabajo con ese número", 404)
    if orden["estado"] in ESTADOS_CERRADOS:
        return None, (f"La orden #{orden_id} está '{orden['estado']}' y no admite más ítems. Reábrela primero cambiando su estado.", 409)

    producto = db.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if producto is None:
        return None, ("Producto no encontrado", 400)

    costo_unitario = producto["costo_compra"]
    if proveedor_id is None:
        balde = db.execute(
            "SELECT costo_promedio FROM stock_proveedores WHERE producto_id = ? AND proveedor_id IS NULL",
            (producto_id,),
        ).fetchone()
    else:
        balde = db.execute(
            "SELECT costo_promedio FROM stock_proveedores WHERE producto_id = ? AND proveedor_id = ?",
            (producto_id, proveedor_id),
        ).fetchone()
    if balde is not None:
        costo_unitario = balde["costo_promedio"]

    cantidad_stock = int(round(cantidad))
    motivo_completo = motivo or f"ORDEN DE TRABAJO #{orden_id}"
    try:
        _aplicar_salida_stock(
            db, producto, cantidad_stock, motivo=motivo_completo, usuario=usuario, categoria=categoria,
            proveedor_id=proveedor_id, proveedor_manual=True,
        )
    except ValueError as e:
        return None, (str(e), 400)

    movimiento_id = db.execute(
        "SELECT id FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1", (producto_id,)
    ).fetchone()[0]

    ts = now_iso()
    detalle_item = "Reservado desde Logística (aún sin descargar como consumo)" if categoria == "Reserva" else ""
    cur = db.execute(
        """
        INSERT INTO orden_items (
            orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario,
            movimiento_id, usuario, creado_en, detalle, aplica_iva
        ) VALUES (?, 'repuesto', ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (orden_id, producto_id, producto["nombre"], cantidad, costo_unitario, movimiento_id, usuario, ts, detalle_item),
    )
    db.execute("UPDATE ordenes_trabajo SET actualizado_en = ? WHERE id = ?", (ts, orden_id))

    nuevo_item = dict(db.execute("SELECT * FROM orden_items WHERE id = ?", (cur.lastrowid,)).fetchone())
    nuevo_item["subtotal"] = _item_subtotal(nuevo_item)
    return nuevo_item, None


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
    # 'convertidor_torque' solo es válido acá (ítems de la OT de un auto),
    # no en garantía_items -- ver TIPOS_ITEM_ORDEN más arriba.
    if tipo not in TIPOS_ITEM_ORDEN:
        return jsonify({"error": f"Tipo de ítem inválido. Usa uno de: {', '.join(TIPOS_ITEM_ORDEN)}"}), 400

    if tipo == "convertidor_torque":
        return convertidores.agregar_convertidor_a_orden(db, orden, orden_id, data)

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

    if item["tipo"] == "convertidor_torque" and item["convertidor_id"]:
        # No hay stock que devolver (el convertidor no vive en 'productos'),
        # pero sí hay que liberar la unidad -- ver convertidores.py.
        convertidores.revertir_convertidor_al_quitar_item(db, item["convertidor_id"])

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
    el IVA, el diagnóstico (resumen para el cliente) y las notas. Al quedar
    'aprobado' la orden pasa sola a 'reparacion' — así nadie tiene que
    acordarse de mover el tablero a mano después de que el cliente dio el
    visto bueno."""
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

    # El diagnóstico (resumen para el cliente) es EL MISMO campo que se ve
    # en la pestaña Diagnóstico — se repite acá para poder revisarlo/
    # ajustarlo justo antes de mandar el presupuesto (a pedido del taller:
    # puede haber llegado por audio de WhatsApp y conviene revisarlo antes
    # de que le llegue al cliente). Igual que en editar_orden(), quien
    # queda como responsable del diagnóstico es quien tiene la sesión
    # iniciada al guardar — nunca un valor que mande el cliente.
    diagnostico = row["diagnostico"]
    responsable_diagnostico = row["responsable_diagnostico"]
    if "diagnostico" in data:
        diagnostico = (data.get("diagnostico") or "").strip().upper()
        responsable_diagnostico = usuario_actual().strip().upper()

    nuevo_estado_orden = row["estado"]
    if estado_presupuesto == "aprobado" and row["presupuesto_estado"] != "aprobado":
        nuevo_estado_orden = "reparacion"
    elif (
        estado_presupuesto == "enviado"
        and row["presupuesto_estado"] != "enviado"
        and row["estado"] not in ESTADOS_POSTERIORES_A_APROBACION
    ):
        # Igual que con "aprobado" -> reparación: apenas el presupuesto queda
        # marcado como enviado, la orden pasa sola a "esperando_aprobacion"
        # para que quede claro que ahora se está a la espera de que el
        # cliente responda. No se aplica si la orden ya iba más adelante
        # (ver ESTADOS_POSTERIORES_A_APROBACION) para no hacerla retroceder.
        nuevo_estado_orden = "esperando_aprobacion"

    ts = now_iso()
    db.execute(
        "UPDATE ordenes_trabajo SET presupuesto_estado = ?, presupuesto_iva = ?, presupuesto_notas = ?, "
        "diagnostico = ?, responsable_diagnostico = ?, estado = ?, actualizado_en = ? WHERE id = ?",
        (estado_presupuesto, iva, notas, diagnostico, responsable_diagnostico, nuevo_estado_orden, ts, orden_id),
    )
    db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    return jsonify(_orden_dict_detalle(db, actualizada))


def _nombre_archivo_seguro(texto):
    """Deja un texto listo para usarse como nombre de archivo: saca los
    caracteres que Windows/macOS no permiten en nombres de archivo, pero
    conserva espacios, tildes y mayúsculas/minúsculas tal como están (para
    que se vea igual de prolijo que el pedido original, ej. "Presupuesto
    Talleres Calificados OT7956 José Alegría")."""
    invalidos = '<>:"/\\|?*'
    limpio = "".join(c for c in (texto or "") if c not in invalidos)
    return " ".join(limpio.split())  # colapsa espacios repetidos


def _armar_presupuesto_pdf(db, orden_id):
    """Arma (pero no construye) el PDF del presupuesto: devuelve
    (buffer, doc, elementos, nombre_archivo, orden), o (None, None, None,
    None, None) si la orden no existe. Se separó de la ruta de descarga
    para poder reutilizar exactamente el mismo armado también al mandarlo
    por correo (ver /presupuesto/enviar) — cada quien decide si construye
    el PDF para descargarlo (_pdf_response) o para adjuntarlo a un correo
    (doc.build + buffer.getvalue())."""
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return None, None, None, None, None
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
        Paragraph("Talleres Calificados", estilos["TituloPresupuesto"]),
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

    # El diagnóstico (resumen para el cliente, pestaña "Diagnóstico") se
    # incluye acá para que el presupuesto que recibe el cliente venga
    # acompañado de POR QUÉ se cobra cada ítem — a pedido del taller, para
    # que no llegue un presupuesto "pelado" sin explicación. Se omite por
    # completo si todavía no se ha cargado un diagnóstico (ej. presupuesto
    # armado antes de terminar de revisar el vehículo).
    if orden.get("diagnostico"):
        elementos.append(Paragraph("Diagnóstico", estilos["EtiquetaPresupuesto"]))
        elementos.append(Paragraph(orden["diagnostico"], estilos["Normal"]))
        elementos.append(Spacer(1, 10))

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

    # OJO: "presupuesto_notas" NO se incluye acá a propósito — son notas
    # internas del taller (ej. "cliente pidió esperar hasta fin de mes",
    # "revisar con el proveedor tal"), nunca deben llegar al documento que
    # se descarga o se manda por correo al cliente. Si el taller necesita
    # comunicarle algo AL CLIENTE, eso va en el campo "Diagnóstico (resumen
    # para el cliente)", que sí se imprime más arriba.

    # Nombre con el número real de OT y el nombre del cliente registrado en
    # la orden, tal como se pidió (ej. "Presupuesto Talleres Calificados
    # OT7956 José Alegría"). El nombre del cliente se guarda en mayúsculas
    # en la base (ver crear_cliente), así que se pasa por .title() para que
    # el archivo se vea con formato de nombre propio y no todo en mayúsculas.
    nombre_cliente = (cliente.get("nombre") or "").title()
    nombre = _nombre_archivo_seguro(f"Presupuesto Talleres Calificados OT{orden_id} {nombre_cliente}").strip()
    nombre = f"{nombre}.pdf"
    return buffer, doc, elementos, nombre, orden


@bp.route("/api/operativo/ordenes/<int:orden_id>/presupuesto/pdf", methods=["GET"])
def presupuesto_pdf(orden_id):
    """Genera el presupuesto en PDF, listo para descargar y enviárselo al
    cliente (por correo, WhatsApp, etc.). Usa los mismos datos que ya se
    ven en el modal de Presupuesto — cliente, vehículo, ítems y totales."""
    db = get_db()
    buffer, doc, elementos, nombre, orden = _armar_presupuesto_pdf(db, orden_id)
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    return _pdf_response(buffer, doc, elementos, nombre)


@bp.route("/api/operativo/ordenes/<int:orden_id>/presupuesto/enviar", methods=["POST"])
def presupuesto_enviar(orden_id):
    """Manda el mismo PDF del presupuesto directo al correo registrado del
    cliente, con copia a los correos del taller configurados en
    PRESUPUESTO_COPIA_TALLER (variable de entorno, separada por coma —
    queda vacía hasta que se confirmen esas direcciones). Si el correo se
    manda con éxito, esto además marca el presupuesto como "enviado" y —
    salvo que la orden ya vaya más adelante en el flujo (ver
    ESTADOS_POSTERIORES_A_APROBACION) — la pasa sola a
    "esperando_aprobacion", igual que si se hubiera guardado ese estado a
    mano desde el desplegable. Se hace un import diferido de app.py (y no
    uno arriba del archivo) para evitar un import circular: app.py es quien
    importa este blueprint, así que operativo.py no puede depender de
    app.py al cargarse el módulo, solo al momento de atender la request."""
    from app import _enviar_correo, PRESUPUESTO_COPIA_TALLER

    db = get_db()
    buffer, doc, elementos, nombre, orden = _armar_presupuesto_pdf(db, orden_id)
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    cliente = orden.get("cliente") or {}
    destinatario = (cliente.get("email") or "").strip()
    if not destinatario:
        return jsonify({
            "error": "Este cliente no tiene un correo registrado. Agrégalo en la ficha del cliente "
                     "(pestaña Clientes) e intenta de nuevo."
        }), 400

    doc.build(elementos)
    buffer.seek(0)
    pdf_bytes = buffer.getvalue()

    nombre_cliente = (cliente.get("nombre") or "").title() or "cliente"
    asunto = f"Presupuesto Talleres Calificados — OT{orden_id}"
    cuerpo = (
        f"Hola {nombre_cliente},\n\n"
        f"Te adjuntamos el presupuesto de la orden de trabajo OT{orden_id}.\n\n"
        f"Ante cualquier consulta, contáctanos.\n\n"
        f"Talleres Calificados"
    )
    exito, detalle = _enviar_correo(
        destinatario, asunto, cuerpo,
        adjunto_bytes=pdf_bytes, adjunto_nombre=nombre, cc=PRESUPUESTO_COPIA_TALLER,
    )
    if not exito:
        return jsonify({"error": f"No se pudo enviar el correo: {detalle}"}), 502

    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    nuevo_estado_orden = row["estado"]
    if (
        row["presupuesto_estado"] != "enviado"
        and row["estado"] not in ESTADOS_POSTERIORES_A_APROBACION
    ):
        nuevo_estado_orden = "esperando_aprobacion"
    if row["presupuesto_estado"] != "enviado" or nuevo_estado_orden != row["estado"]:
        db.execute(
            "UPDATE ordenes_trabajo SET presupuesto_estado = ?, estado = ?, actualizado_en = ? WHERE id = ?",
            ("enviado", nuevo_estado_orden, now_iso(), orden_id),
        )
        db.commit()
    actualizada = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()

    return jsonify({
        "ok": True, "detalle": detalle, "destinatario": destinatario, "copia": PRESUPUESTO_COPIA_TALLER,
        "orden": _orden_dict_detalle(db, actualizada),
    })


@bp.route("/api/operativo/ordenes/<int:orden_id>/orden-imprimible/pdf", methods=["GET"])
def orden_imprimible_pdf(orden_id):
    """Documento imprimible que se le entrega al cliente como comprobante
    oficial de la orden de trabajo: todos los datos de la recepción (cliente
    y vehículo) más los valores (total, pagado, por pagar) — a propósito
    SIN costos internos (repuestos, mano de obra) ni utilidad, porque ese es
    un documento para el cliente, no para uso interno del taller (para eso
    ya existe la pestaña "Costos" dentro de la orden)."""
    db = get_db()
    row = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Orden no encontrada"}), 404
    orden = _orden_dict_detalle(db, row)
    cliente = orden.get("cliente") or {}
    equipo = orden.get("equipo") or {}
    try:
        equipamiento = json.loads(orden.get("equipamiento_json") or "{}")
    except (TypeError, ValueError):
        equipamiento = {}

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
    estilos.add(ParagraphStyle(name="TituloOrdenImp", fontSize=18, leading=22, spaceAfter=2, fontName="Helvetica-Bold"))
    estilos.add(ParagraphStyle(name="SubtituloOrdenImp", fontSize=10, textColor="#666666", spaceAfter=16))
    estilos.add(ParagraphStyle(name="SeccionOrdenImp", fontSize=11, textColor="#1F2430", spaceBefore=14, spaceAfter=6, fontName="Helvetica-Bold"))

    elementos = [
        Paragraph("Talleres Calificados", estilos["TituloOrdenImp"]),
        Paragraph(
            f"Orden de trabajo OT{orden_id} · generado el {datetime.now().strftime('%d-%m-%Y %H:%M')}",
            estilos["SubtituloOrdenImp"],
        ),
    ]

    def _tabla_datos(filas):
        t = Table(filas, colWidths=[3.4 * cm, 5.3 * cm, 3.4 * cm, 5.3 * cm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    elementos.append(Paragraph("Datos del cliente", estilos["SeccionOrdenImp"]))
    elementos.append(_tabla_datos([
        ["Cliente:", cliente.get("nombre") or "—", "RUT:", cliente.get("rut") or "—"],
        ["Teléfono:", cliente.get("telefono") or "—", "Email:", cliente.get("email") or "—"],
        ["Dirección:", cliente.get("direccion") or "—", "", ""],
    ]))

    elementos.append(Paragraph("Datos del vehículo", estilos["SeccionOrdenImp"]))
    elementos.append(_tabla_datos([
        ["Patente:", equipo.get("identificador") or "—", "VIN:", equipo.get("vin") or "—"],
        ["Marca:", equipo.get("marca") or "—", "Modelo:", equipo.get("modelo") or "—"],
        ["Color:", equipo.get("color") or "—", "Año:", equipo.get("anio") or "—"],
        ["Transmisión:", equipo.get("modelo_transmision") or "—", "Motor (cc):", equipo.get("motor_cc") or "—"],
    ]))

    estanque = orden.get("estanque_nivel")
    elementos.append(Paragraph("Datos de la recepción", estilos["SeccionOrdenImp"]))
    elementos.append(_tabla_datos([
        ["Fecha recepción:", (orden.get("fecha_recepcion") or "—")[:10], "Encargado:", orden.get("recepcionado_por") or "—"],
        ["Kilometraje:", str(orden.get("kilometraje")) if orden.get("kilometraje") is not None else "—",
         "Estanque:", f"{estanque} / 8" if estanque is not None else "—"],
        ["Referencia:", orden.get("referencia") or "—", "", ""],
    ]))
    elementos.append(Spacer(1, 4))
    elementos.append(Paragraph(f"<b>Trabajo a realizar:</b> {orden.get('trabajo_a_realizar') or '—'}", estilos["Normal"]))
    if orden.get("descripcion_problema"):
        elementos.append(Spacer(1, 4))
        elementos.append(Paragraph(f"<b>Comentarios:</b> {orden['descripcion_problema']}", estilos["Normal"]))

    elementos.append(Paragraph("Equipamiento con el que ingresó el vehículo", estilos["SeccionOrdenImp"]))
    marcados = [item for item in EQUIPAMIENTO_ITEMS if equipamiento.get(item)]
    elementos.append(Paragraph(", ".join(marcados) if marcados else "Sin equipamiento adicional registrado.", estilos["Normal"]))

    # Valores: a propósito solo lo que le corresponde ver al cliente (lo
    # que se le cobra, lo pagado y lo pendiente) — nada de costos internos
    # de repuestos/mano de obra ni de utilidad, eso es información interna
    # del taller y no debe salir en un documento que se lleva el cliente.
    elementos.append(Paragraph("Valores", estilos["SeccionOrdenImp"]))
    resumen = [["Total OT", fmt_money_pdf(orden.get("valor_total") or 0)]]
    resumen.append(["Total pagado", fmt_money_pdf(orden.get("total_pagado") or 0)])
    resumen.append(["Total por pagar", fmt_money_pdf(orden.get("total_por_pagar") or 0)])
    tabla_valores = Table(resumen, colWidths=[15.2 * cm, 2.8 * cm])
    tabla_valores.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#333333")),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]))
    elementos.append(tabla_valores)

    nombre_cliente = (cliente.get("nombre") or "").title()
    nombre = _nombre_archivo_seguro(f"Orden de Trabajo Talleres Calificados OT{orden_id} {nombre_cliente}").strip()
    nombre = f"{nombre}.pdf"
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
    # El inspector, igual que el responsable del diagnóstico (ver
    # editar_orden()), NUNCA se toma de lo que mande el cliente — siempre
    # es quien tiene la sesión iniciada al momento de guardar. Si este PUT
    # no tocó ni el resultado ni las observaciones, se deja el inspector
    # que ya había (ej. una llamada que solo actualiza el checklist).
    if "resultado" in data or "observaciones" in data:
        inspector = usuario_actual().strip().upper()
    else:
        inspector = row["qc_inspector"] or ""
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
# Bitácora de pruebas de ruta (migración 20) -- historial de entradas por
# OT, cada una con fecha y autor propios, sin sobrescribir a las anteriores.
# Se llena desde acá (formulario web) o automáticamente cuando un mecánico
# de perfil "probador_de_autos" manda el resultado por WhatsApp (ver
# _agente_guardar_prueba_ruta en app.py). Deliberadamente separada del
# control de calidad (qc_*): esto es solo un registro interno, no un
# veredicto que mueva el estado de la OT.
# ---------------------------------------------------------------------------

@bp.route("/api/operativo/ordenes/<int:orden_id>/pruebas-ruta", methods=["POST"])
def agregar_prueba_ruta(orden_id):
    db = get_db()
    orden = db.execute("SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)).fetchone()
    if orden is None:
        return jsonify({"error": "Orden no encontrada"}), 404

    data = request.get_json(force=True, silent=True) or {}
    texto = (data.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "Escribe el resultado de la prueba de ruta"}), 400

    foto_id = data.get("foto_id") or None
    if foto_id:
        try:
            foto_id = int(foto_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Foto inválida"}), 400
        foto = db.execute(
            "SELECT id FROM fotos_vehiculo WHERE id = ? AND entidad_tipo = 'orden' AND entidad_id = ?",
            (foto_id, orden_id),
        ).fetchone()
        if foto is None:
            return jsonify({"error": "La foto indicada no corresponde a esta orden"}), 400

    ts = now_iso()
    cur = db.execute(
        "INSERT INTO pruebas_ruta (orden_id, texto, autor, origen, foto_id, creado_en) "
        "VALUES (?, ?, ?, 'web', ?, ?)",
        (orden_id, texto, usuario_actual(), foto_id, ts),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM pruebas_ruta WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(nueva)), 201


@bp.route("/api/operativo/ordenes/<int:orden_id>/pruebas-ruta/<int:prueba_id>", methods=["DELETE"])
def eliminar_prueba_ruta(orden_id, prueba_id):
    db = get_db()
    fila = db.execute(
        "SELECT * FROM pruebas_ruta WHERE id = ? AND orden_id = ?", (prueba_id, orden_id)
    ).fetchone()
    if fila is None:
        return jsonify({"error": "Entrada no encontrada"}), 404
    db.execute("DELETE FROM pruebas_ruta WHERE id = ?", (prueba_id,))
    db.commit()
    return jsonify({"ok": True})


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
    # Se agrega 'email' (no se seleccionaba antes) porque ahora también lo
    # necesita finalizar_fotos_garantia() para saber a dónde mandar el
    # registro fotográfico -- ver _finalizar_fotos_generico.
    cliente = db.execute(
        "SELECT id, nombre, telefono, email FROM clientes WHERE id = ?", (d["cliente_id"],)
    ).fetchone()
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
    _iniciar_transaccion_correlativo(db)  # ver _asignar_id_correlativo más abajo
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
    nuevo_id = _asignar_id_correlativo(db)
    db.execute(
        """
        INSERT INTO garantias (
            id, cliente_id, equipo_id, orden_origen_id, motivo, diagnostico, responsable,
            estado, fecha_recepcion, usuario, creado_en, actualizado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ingreso', ?, ?, ?, ?)
        """,
        (
            nuevo_id, cliente_id, equipo_id, orden_origen_id, motivo,
            (data.get("diagnostico") or "").strip().upper(),
            (data.get("responsable") or "").strip().upper(),
            ts, usuario_actual(), ts, ts,
        ),
    )
    db.commit()
    nueva = db.execute("SELECT * FROM garantias WHERE id = ?", (nuevo_id,)).fetchone()
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

    if item["tipo"] == "convertidor_torque" and item["convertidor_id"]:
        # Mismo caso que eliminar_item_orden(): no hay stock que devolver
        # (el convertidor no vive en 'productos'), pero se intenta liberar
        # la unidad por si acaso -- en la práctica es un no-op porque un
        # convertidor 'desde_garantia' queda 'entregado', no 'instalado'
        # (ver revertir_convertidor_al_quitar_item en convertidores.py).
        convertidores.revertir_convertidor_al_quitar_item(db, item["convertidor_id"])

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
