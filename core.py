"""
core.py — piezas compartidas del sistema (conexión a la base de datos,
cálculo de stock mínimo, utilidades de formato, generación de reportes
Excel/PDF) que tanto app.py como los módulos separados (modulos/) necesitan.

Este archivo NO importa nada de app.py ni de modulos/ — es intencional: es
la base de la que todo lo demás cuelga, así que si dependiera hacia arriba
se armaría un import circular apenas hubiera más de un módulo (pasó
exactamente eso al separar Caja Chica la primera vez: modulos/caja_chica.py
hacía "from app import ...", y en cuanto se corría "python3 app.py"
directamente en vez de importarlo como paquete, Python terminaba
reimportando app.py desde cero bajo un nombre de módulo distinto y entraba
en un loop de import circular). Con esta capa aparte, tanto app.py como
cualquier módulo nuevo (Operativo, RRHH, etc.) importan de acá y nunca uno
del otro.
"""

import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from functools import wraps
import statistics
import math

from flask import g, jsonify, request, send_file, session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "inventario.db"))

COMPROBANTES_DIR = os.path.join(os.path.dirname(DB_PATH), "comprobantes")
os.makedirs(COMPROBANTES_DIR, exist_ok=True)
EXTENSIONES_COMPROBANTE_PERMITIDAS = {".jpg", ".jpeg", ".png", ".pdf", ".webp", ".heic"}


# ---------------------------------------------------------------------------
# Cálculo automático del stock mínimo (punto de reorden) — constantes
# ---------------------------------------------------------------------------
STOCK_MINIMO_VENTANA_DIAS = int(os.environ.get("STOCK_MINIMO_VENTANA_DIAS", "90"))
STOCK_MINIMO_Z_SERVICIO = float(os.environ.get("STOCK_MINIMO_Z_SERVICIO", "1.65"))  # ≈95% de nivel de servicio
STOCK_MINIMO_LEAD_TIME_DEFECTO_DIAS = float(os.environ.get("STOCK_MINIMO_LEAD_TIME_DEFECTO_DIAS", "7"))
STOCK_MINIMO_LEAD_TIME_MIN_DIAS = 1.0
STOCK_MINIMO_LEAD_TIME_MAX_DIAS = 60.0


# ---------------------------------------------------------------------------
# Conexión a la base de datos (WAL + busy_timeout, ver docstring de
# _iniciar_transaccion_stock para el porqué de BEGIN IMMEDIATE)
# ---------------------------------------------------------------------------

DB_BUSY_TIMEOUT_MS = 5000


def _conectar_db(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return db


def get_db():
    if "db" not in g:
        g.db = _conectar_db()
    return g.db


def _iniciar_transaccion_stock(db):
    """Se llama al principio de cualquier endpoint que vaya a LEER el stock
    actual de un producto y después ESCRIBIR un nuevo valor calculado a
    partir de esa lectura (entradas, salidas, pérdidas, reversiones).

    El patrón "leo el stock, calculo el nuevo valor en Python, lo guardo" es
    vulnerable a una condición de carrera clásica si dos requests llegan casi
    al mismo tiempo: ambas leen el mismo valor viejo, ambas calculan a partir
    de ese valor, y la segunda escritura pisa a la primera sin que ninguna
    de las dos se entere — el stock queda mal sin que nadie vea ningún error.
    Se comprobó que esto pasa incluso con WAL activado, porque WAL evita que
    una escritura BLOQUEE a otra, pero no evita que dos lecturas-y-escrituras
    se entrelacen si no se agrupan en una sola transacción.

    BEGIN IMMEDIATE toma el lock de escritura al toque (no al primer INSERT/
    UPDATE, sino desde esta línea), así que si dos requests llegan casi
    juntas, la segunda simplemente espera a que la primera termine y
    confirme sus cambios antes de leer — ya no puede leer un valor que está
    a punto de quedar obsoleto."""
    db.execute("BEGIN IMMEDIATE")


def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# ---------------------------------------------------------------------------
# Utilidades de formato
# ---------------------------------------------------------------------------

def row_to_dict(row):
    d = dict(row)
    d["stock_total"] = d["stock_disponible"] + d["stock_reservado"]
    d["id_sistema"] = f"{d['id']:04d}"
    if "foto_archivo" in d:
        d["tiene_foto"] = bool(d.get("foto_archivo"))
    return d

def codigo_ubicacion(pasillo, estanteria, nivel):
    return f"P{pasillo}-E{estanteria}-N{nivel}"


# ---------------------------------------------------------------------------
# Búsqueda "de varias palabras, sin tildes": todos los buscadores del sistema
# (productos, movimientos, clientes, caja chica, etc.) usan estas dos
# funciones para que buscar sea más tolerante y más preciso a la vez:
#   - Sin tildes/mayúsculas: "bomba" encuentra "BOMBA" y "bómba" por igual.
#   - Varias palabras = AND, no importa en qué campo caiga cada una: buscar
#     "6f35 bomba" encuentra algo cuyo código es "6F35" Y cuya descripción
#     menciona "bomba", aunque sean campos distintos — mientras más palabras
#     se agreguen, más se acota el resultado (en vez de tener que acertarle
#     a una frase exacta en un solo campo).
# ---------------------------------------------------------------------------

def normalizar_busqueda(texto):
    """minúsculas + sin tildes/diacríticos, para comparar sin que la tilde
    (o su ausencia) cambie el resultado."""
    if texto is None:
        return ""
    texto = unicodedata.normalize("NFD", str(texto))
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower()


def coincide_busqueda(consulta, *campos):
    """True si CADA palabra de 'consulta' aparece en al menos uno de
    'campos' (concatenados), sin importar tildes/mayúsculas ni en cuál
    campo específico caiga cada palabra. Con 'consulta' vacía, siempre
    coincide (no filtra nada)."""
    terminos = normalizar_busqueda(consulta).split()
    if not terminos:
        return True
    texto = normalizar_busqueda(" ".join(str(c) for c in campos if c))
    return all(t in texto for t in terminos)


def nombre_automatico(categoria, modelo, codigo_interno, id_num):
    """El producto ya no tiene un campo 'nombre' editable: se arma solo a
    partir de categoría + modelo (los datos que realmente lo identifican),
    con respaldos razonables si esos vienen vacíos."""
    partes = [p for p in [(categoria or "").strip(), (modelo or "").strip()] if p]
    if partes:
        return " - ".join(partes)
    if (codigo_interno or "").strip():
        return codigo_interno.strip()
    return f"Producto #{id_num:04d}"

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def sumar_dias_iso(fecha_iso, dias):
    """'2026-07-09T10:00:00' + 30 días -> '2026-08-08T10:00:00'. Tolera que
    la fecha venga sin la parte de hora (ej. solo '2026-07-09'), o vacía —
    en ese caso usa el momento actual como base. Compartida entre módulos
    (Operativo la usa para vencimiento de CxC, Facturas/CxP para
    vencimiento de CxP) para no repetir la misma cuenta dos veces."""
    try:
        base = datetime.fromisoformat(fecha_iso)
    except (TypeError, ValueError):
        base = datetime.now()
    return (base + timedelta(days=dias)).isoformat(timespec="seconds")


def fmt_money_pdf(valor):
    """Formatea un monto en pesos chilenos para los reportes PDF (con
    separador de miles), igual que se ve en la interfaz."""
    try:
        return f"${round(valor):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "$0"

def usuario_actual():
    """El responsable de un movimiento de Caja Chica siempre es quien tiene
    la sesión iniciada (nunca un valor mandado por el cliente), para que no
    se pueda registrar un gasto o aporte a nombre de otra persona."""
    return session.get("nombre") or session.get("username") or "Sistema"


# ---------------------------------------------------------------------------
# Roles — usuarios.rol (ver migración en app.py). 'admin' puede todo
# siempre; los demás roles solo lo que su rol tenga permitido en cada
# endpoint (ver requiere_rol). Sesiones que ya estaban iniciadas ANTES de
# este cambio no tienen 'rol' guardado en la sesión (se hizo login antes de
# que existiera la columna) — se tratan como 'admin' mientras no vuelvan a
# iniciar sesión, para no dejar a nadie bloqueado de un día para otro por
# un despliegue; en el próximo login ya les queda su rol real.
# ---------------------------------------------------------------------------

ROLES_VALIDOS = ["admin", "finanzas", "bodega", "operativo"]


def usuario_rol():
    return session.get("rol") or "admin"


def requiere_rol(*roles_permitidos):
    """Decorador para endpoints sensibles: exige que la sesión activa tenga
    uno de los roles indicados (o sea 'admin', que siempre puede todo).
    Devuelve 403 con un mensaje claro si no corresponde — a diferencia de
    solo ocultar el botón en la interfaz, esto se cumple aunque alguien
    llame al endpoint directamente sin pasar por la pantalla. Sin
    argumentos (requiere_rol()) significa "solo admin"."""
    def decorador(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            rol = usuario_rol()
            if rol != "admin" and rol not in roles_permitidos:
                permitidos = ", ".join(roles_permitidos) or "admin"
                return jsonify({"error": f"Esta acción es solo para el rol {permitidos}. Tu rol actual es '{rol}'."}), 403
            return vista(*args, **kwargs)
        return envoltura
    return decorador


def formatear_rut(valor):
    """Normaliza un RUT chileno agregando puntos de miles y el guión antes
    del dígito verificador, sin importar cómo lo haya escrito el usuario.
    Ej: "770893703" -> "77.089.370-3", "7.708.937-03-0" (con basura) se
    limpia primero. Si no viene nada o no hay al menos un dígito, retorna
    el texto original (limpio de espacios) tal cual, para no inventar datos."""
    if not valor:
        return ""
    limpio = re.sub(r"[^0-9kK]", "", valor)
    if not limpio:
        return valor.strip().upper()
    cuerpo, dv = limpio[:-1], limpio[-1].upper()
    if not cuerpo:
        return dv
    cuerpo_con_puntos = ""
    for i, digito in enumerate(reversed(cuerpo)):
        if i and i % 3 == 0:
            cuerpo_con_puntos = "." + cuerpo_con_puntos
        cuerpo_con_puntos = digito + cuerpo_con_puntos
    return f"{cuerpo_con_puntos}-{dv}"

# ---------------------------------------------------------------------------
# Punto de reorden (stock mínimo automático) — ver Migración 10 en
# app.py:init_db() para la tabla stock_minimo_calculo donde se guarda el
# detalle de cada cálculo.
# ---------------------------------------------------------------------------

def _estimar_lead_time_dias(db, producto_id):
    """Estima cuántos días suele tardar en 'reponerse' este producto,
    midiendo el intervalo entre entradas de stock sucesivas (compras)
    históricas — no se registra un tiempo de entrega explícito en ninguna
    parte del sistema, así que esto es lo más cercano que hay a ese dato.

    Devuelve (dias, es_estimado_por_defecto). Con menos de dos entradas
    históricas no hay intervalo que medir, así que se usa un valor por
    defecto razonable (configurable) y se marca como tal, para que la
    interfaz pueda avisar que es una estimación poco confiable todavía."""
    fechas = db.execute(
        "SELECT fecha FROM movimientos WHERE producto_id = ? AND tipo = 'entrada' ORDER BY fecha ASC",
        (producto_id,),
    ).fetchall()

    if len(fechas) < 2:
        return STOCK_MINIMO_LEAD_TIME_DEFECTO_DIAS, True

    intervalos = []
    anterior = None
    for f in fechas:
        try:
            actual = datetime.fromisoformat(f["fecha"])
        except (TypeError, ValueError):
            continue
        if anterior is not None:
            dias = (actual - anterior).total_seconds() / 86400
            if dias > 0:
                intervalos.append(dias)
        anterior = actual

    if not intervalos:
        return STOCK_MINIMO_LEAD_TIME_DEFECTO_DIAS, True

    # La mediana es más robusta que el promedio: una sola compra atrasada o
    # adelantada (ej. stock inicial cargado el mismo día que la primera
    # factura) no debería distorsionar la estimación completa.
    estimado = statistics.median(intervalos)
    estimado = max(STOCK_MINIMO_LEAD_TIME_MIN_DIAS, min(STOCK_MINIMO_LEAD_TIME_MAX_DIAS, estimado))
    return estimado, False


def _consumo_diario_stats(db, producto_id, dias=STOCK_MINIMO_VENTANA_DIAS):
    """Consumo diario promedio y su desviación estándar dentro de la
    ventana de días indicada, contando los días sin salidas como 0 (para
    que el promedio refleje bien la frecuencia real de uso, no solo los
    días que sí tuvieron movimiento).

    Excluye la categoría 'Corrección de inventario' (reversión técnica de un
    ingreso hecho por error, ej. al eliminar una factura ya aplicada — ver
    CATEGORIA_CORRECCION_INVENTARIO en app.py): esa salida nunca fue consumo
    real del taller, y contarla infla el promedio y el stock mínimo sugerido."""
    desde_dt = datetime.now() - timedelta(days=dias)
    desde = desde_dt.isoformat(timespec="seconds")

    filas = db.execute(
        """
        SELECT date(fecha) AS dia, SUM(cantidad) AS total
        FROM movimientos
        WHERE producto_id = ? AND tipo = 'salida' AND categoria != 'Corrección de inventario' AND fecha >= ?
        GROUP BY dia
        """,
        (producto_id, desde),
    ).fetchall()
    mapa = {r["dia"]: r["total"] for r in filas}

    serie = []
    cursor = desde_dt
    hoy = datetime.now()
    while cursor <= hoy:
        serie.append(mapa.get(cursor.strftime("%Y-%m-%d"), 0))
        cursor += timedelta(days=1)

    if not serie:
        return 0.0, 0.0

    promedio = statistics.mean(serie)
    desviacion = statistics.pstdev(serie) if len(serie) > 1 else 0.0
    return promedio, desviacion


def _calcular_stock_minimo_producto(db, producto_id):
    """Punto de reorden: cuánto stock disponible debería quedar cuando toca
    volver a comprar, calculado solo con datos reales del producto (sin
    necesidad de ningún modelo de IA — es aritmética simple de inventario):

        mínimo = consumo_diario_promedio × días_de_entrega + stock_de_seguridad

    El stock de seguridad cubre que el consumo real varíe semana a semana
    (usa la desviación estándar del consumo diario y un nivel de servicio
    configurable — por defecto ≈95%, es decir, deja margen para no quedar
    en cero en la gran mayoría de los casos, sin sobre-comprar tampoco)."""
    consumo_diario, desviacion_diaria = _consumo_diario_stats(db, producto_id)
    lead_time_dias, lead_time_es_defecto = _estimar_lead_time_dias(db, producto_id)

    if consumo_diario <= 0:
        # Sin consumo reciente: no hay base para justificar un colchón de
        # stock — mantenerlo en 0 evita que productos obsoletos o nuevos
        # sin historial queden marcados como "críticos" sin motivo real.
        stock_seguridad = 0.0
        punto_reorden = 0.0
    else:
        stock_seguridad = STOCK_MINIMO_Z_SERVICIO * desviacion_diaria * math.sqrt(lead_time_dias)
        punto_reorden = consumo_diario * lead_time_dias + stock_seguridad

    return {
        "consumo_diario_promedio": round(consumo_diario, 4),
        "desviacion_diaria": round(desviacion_diaria, 4),
        "lead_time_dias": round(lead_time_dias, 2),
        "lead_time_es_estimado_por_defecto": lead_time_es_defecto,
        "stock_seguridad": round(stock_seguridad, 2),
        "stock_minimo_calculado": max(0, round(punto_reorden)),
    }


def _recalcular_stock_minimo(db, producto_id):
    """Recalcula y GUARDA el stock mínimo del producto (sobrescribe lo que
    hubiera antes, sea manual o automático) — se llama automáticamente
    después de cada entrada, salida o pérdida de stock, para que el mínimo
    quede siempre al día con el consumo real más reciente. No hace commit:
    queda dentro de la misma transacción del movimiento que lo disparó."""
    calculo = _calcular_stock_minimo_producto(db, producto_id)
    ts = now_iso()
    db.execute(
        "UPDATE productos SET stock_minimo = ? WHERE id = ?",
        (calculo["stock_minimo_calculado"], producto_id),
    )
    db.execute(
        """
        INSERT INTO stock_minimo_calculo (
            producto_id, consumo_diario_promedio, desviacion_diaria, lead_time_dias,
            lead_time_es_estimado_por_defecto, stock_seguridad, stock_minimo_calculado, calculado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(producto_id) DO UPDATE SET
            consumo_diario_promedio = excluded.consumo_diario_promedio,
            desviacion_diaria = excluded.desviacion_diaria,
            lead_time_dias = excluded.lead_time_dias,
            lead_time_es_estimado_por_defecto = excluded.lead_time_es_estimado_por_defecto,
            stock_seguridad = excluded.stock_seguridad,
            stock_minimo_calculado = excluded.stock_minimo_calculado,
            calculado_en = excluded.calculado_en
        """,
        (
            producto_id, calculo["consumo_diario_promedio"], calculo["desviacion_diaria"],
            calculo["lead_time_dias"], 1 if calculo["lead_time_es_estimado_por_defecto"] else 0,
            calculo["stock_seguridad"], calculo["stock_minimo_calculado"], ts,
        ),
    )
    return calculo


def _aplicar_entrada_stock(db, producto_row, cantidad, costo_unitario, motivo, usuario, factura=None, categoria="Compra"):
    """Aplica una entrada de stock a un producto, recalculando el Costo
    Promedio Ponderado si se informa costo_unitario, y deja el movimiento
    registrado con la referencia a la factura si corresponde.
    Devuelve el nuevo stock disponible.
    """
    stock_actual = producto_row["stock_disponible"]
    costo_actual = producto_row["costo_compra"]
    nuevo_stock = stock_actual + cantidad

    nuevo_costo_promedio = costo_actual
    if costo_unitario is not None:
        valor_actual = stock_actual * costo_actual
        valor_nuevo = cantidad * costo_unitario
        nuevo_costo_promedio = (valor_actual + valor_nuevo) / nuevo_stock if nuevo_stock > 0 else costo_unitario

    ts = now_iso()
    db.execute(
        "UPDATE productos SET stock_disponible = ?, costo_compra = ?, actualizado_en = ? WHERE id = ?",
        (nuevo_stock, nuevo_costo_promedio, ts, producto_row["id"]),
    )
    db.execute(
        """
        INSERT INTO movimientos (
            producto_id, fecha, tipo, categoria, cantidad, motivo, usuario,
            costo_unitario, factura_id, factura_numero, stock_resultante
        )
        VALUES (?, ?, 'entrada', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            producto_row["id"], ts, categoria, cantidad, motivo, usuario, costo_unitario,
            factura["id"] if factura else None,
            factura["numero"] if factura else None,
            nuevo_stock,
        ),
    )
    _recalcular_stock_minimo(db, producto_row["id"])
    return nuevo_stock


def _aplicar_salida_stock(db, producto_row, cantidad, motivo, usuario, categoria="Consumo interno"):
    """Aplica una salida de stock a un producto (sin tocar el Costo Promedio
    Ponderado — una salida no cambia lo que costó lo que queda en bodega).
    Deja el movimiento registrado y recalcula el stock mínimo, porque una
    salida sí cambia el consumo real que alimenta esa proyección.

    Lanza ValueError si no hay stock disponible suficiente, con el mismo
    mensaje que usa el resto del sistema para este caso — el que llama debe
    capturarlo y devolver un 400 (ver registrar_movimiento en app.py o
    _consumir_repuesto_orden en modulos/operativo.py para el patrón).
    Devuelve el nuevo stock disponible."""
    stock_actual = producto_row["stock_disponible"]
    if cantidad > stock_actual:
        raise ValueError(f"No hay stock suficiente (disponible: {stock_actual})")
    nuevo_stock = stock_actual - cantidad

    ts = now_iso()
    db.execute(
        "UPDATE productos SET stock_disponible = ?, actualizado_en = ? WHERE id = ?",
        (nuevo_stock, ts, producto_row["id"]),
    )
    db.execute(
        "INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, stock_resultante) "
        "VALUES (?, ?, 'salida', ?, ?, ?, ?, ?)",
        (producto_row["id"], ts, categoria, cantidad, motivo, usuario, nuevo_stock),
    )
    _recalcular_stock_minimo(db, producto_row["id"])
    return nuevo_stock

# ---------------------------------------------------------------------------
# Reportes: helpers compartidos para armar planillas Excel y documentos PDF
# ---------------------------------------------------------------------------

def _crear_libro_excel():
    from openpyxl import Workbook
    return Workbook()


def _estilo_encabezado(ws, fila=1):
    from openpyxl.styles import Font, PatternFill, Alignment
    for celda in ws[fila]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill(start_color="1F2430", end_color="1F2430", fill_type="solid")
        celda.alignment = Alignment(vertical="center")


def _autoancho(ws):
    for columna in ws.columns:
        letra = columna[0].column_letter
        largo = max((len(str(c.value)) if c.value is not None else 0) for c in columna)
        ws.column_dimensions[letra].width = min(48, max(10, largo + 2))


def _excel_response(wb, nombre_archivo):
    import io
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nombre_archivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

def _excel_resumen(ws, titulo, lineas):
    """Escribe un resumen corto (2-5 datos clave) arriba de la hoja, antes de
    la tabla de detalle — para que al abrir el archivo se entienda de
    inmediato qué trae, sin tener que contar filas a mano. Devuelve la fila
    en la que debería empezar el encabezado de la tabla de datos."""
    from openpyxl.styles import Font
    ws.append([titulo])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=13)
    for label, valor in lineas:
        ws.append([f"{label}: {valor}"])
    ws.append([])  # fila en blanco antes de la tabla
    return ws.max_row + 1


def _pdf_resumen(elementos, estilos, lineas):
    """Agrega un resumen corto (2-5 datos clave) justo debajo del título del
    PDF, antes de las tablas de detalle."""
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.lib.units import cm
    texto = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(f"<b>{label}:</b> {valor}" for label, valor in lineas)
    elementos.append(Paragraph(texto, estilos["Normal"]))
    elementos.append(Spacer(1, 0.4 * cm))


def _pdf_documento(titulo):
    """Arma un documento PDF apaisado (más ancho que alto, para que quepan
    tablas con varias columnas) con un título arriba y devuelve
    (buffer, doc, elementos, estilos) listos para ir agregando contenido."""
    import io
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=1.4 * cm, bottomMargin=1.4 * cm, leftMargin=1.4 * cm, rightMargin=1.4 * cm,
    )
    estilos = getSampleStyleSheet()
    estilos.add(ParagraphStyle(name="TituloReporte", fontSize=16, leading=20, spaceAfter=4, fontName="Helvetica-Bold"))
    estilos.add(ParagraphStyle(name="SubtituloReporte", fontSize=10, textColor="#666666", spaceAfter=14))
    estilos.add(ParagraphStyle(name="TituloTabla", fontSize=12, spaceBefore=14, spaceAfter=6, fontName="Helvetica-Bold"))

    elementos = [
        Paragraph(titulo, estilos["TituloReporte"]),
        Paragraph(f"Taller Mecánico — generado el {datetime.now().strftime('%d-%m-%Y %H:%M')}", estilos["SubtituloReporte"]),
    ]
    return buffer, doc, elementos, estilos


def _pdf_tabla(elementos, estilos, titulo_tabla, encabezados, filas, notas=""):
    """Agrega un título de sección + una tabla con estilo consistente."""
    from reportlab.platypus import Table, TableStyle, Paragraph, Spacer
    from reportlab.lib import colors

    if titulo_tabla:
        elementos.append(Paragraph(titulo_tabla, estilos["TituloTabla"]))
    if not filas:
        elementos.append(Paragraph("Sin datos para mostrar.", estilos["Normal"]))
        return
    datos = [encabezados] + [[str(c) if c is not None else "" for c in fila] for fila in filas]
    tabla = Table(datos, repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2430")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F5F7")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DCE3")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    elementos.append(tabla)
    if notas:
        elementos.append(Paragraph(notas, estilos["Normal"]))


def _pdf_response(buffer, doc, elementos, nombre_archivo):
    doc.build(elementos)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=nombre_archivo, mimetype="application/pdf")


def _rango_fechas():
    desde = (request.args.get("desde") or "").strip()
    hasta = (request.args.get("hasta") or "").strip()
    return desde, hasta
