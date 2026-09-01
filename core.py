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

from flask import g, jsonify, redirect, request, send_file, session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "inventario.db"))

COMPROBANTES_DIR = os.path.join(os.path.dirname(DB_PATH), "comprobantes")
os.makedirs(COMPROBANTES_DIR, exist_ok=True)
EXTENSIONES_COMPROBANTE_PERMITIDAS = {".jpg", ".jpeg", ".png", ".pdf", ".webp", ".heic"}

# IVA aplicado al valor cobrado al cliente por una OT (ver modulos/operativo.py
# y modulos/finanzas.py, ambos lo usan para calcular el total de una orden —
# vive acá para que los dos módulos calculen el mismo número sin uno
# importar del otro, que rompería la regla de "core.py no depende de nada").
IVA_PORCENTAJE = 0.19

# Porcentaje de anticipo que el taller pide al aprobar el presupuesto de una
# OT (ver modulos/finanzas.py: CxC "por abonos"). Es un criterio del
# negocio, no legal — el taller decidió cobrar 50% al aprobar el trabajo y
# el otro 50% al entregar el vehículo, con excepciones que se manejan
# registrando los pagos a mano sin que el sistema distinga el caso especial.
ANTICIPO_PORCENTAJE_DEFECTO = 50


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
    a punto de quedar obsoleto.

    Es un no-op si 'db' ya está en medio de una transacción (db.in_transaction):
    SQLite no permite un BEGIN anidado (lanzaría OperationalError "cannot
    start a transaction within a transaction"). Esto permite que funciones
    que ya toman este lock por su cuenta (ej. _aprobar_pendiente_recepcion
    en app.py) puedan llamarse tanto solas como desde un caller que YA
    tomó el lock más arriba (ej. aprobar_factura_pendiente_recepcion,
    que necesita el lock desde ANTES de leer el estado del pendiente para
    cerrar la carrera de un doble clic en "Aprobar" -- ver hallazgo #8/#9
    del informe de revisión) sin duplicar el BEGIN. El primer llamador que
    entra de verdad toma el lock; los anidados dentro de la misma
    transacción sin commit de por medio simplemente lo heredan."""
    if db.in_transaction:
        return
    db.execute("BEGIN IMMEDIATE")


def _iniciar_transaccion_correlativo(db):
    """Mismo mecanismo que _iniciar_transaccion_stock (BEGIN IMMEDIATE toma
    el lock de escritura al toque, y es no-op si ya hay una transacción
    abierta) pero usado para el correlativo compartido entre OT y
    garantías -- ver _asignar_id_correlativo en modulos/operativo.py. Es
    una función aparte (aunque hace lo mismo) para que quede claro en el
    código de OT/garantías por qué se está tomando el lock ahí -- no es
    por stock."""
    if db.in_transaction:
        return
    db.execute("BEGIN IMMEDIATE")


def _reconstruir_tabla_legada_si_hace_falta(db, tabla, sql_create_nueva, columnas_objetivo, mapeos_legado=None):
    """
    Sanea una tabla que puede venir de una versión mucho más vieja del
    proyecto, con columnas legadas que ya no usamos (a veces con NOT NULL
    y sin default, lo que hace fallar cualquier INSERT que no las llene).

    Vive en core.py (y no en modulos/operativo.py, donde se escribió
    originalmente) porque más de un módulo la necesita -- primero
    garantias/garantia_items en Operativo, después convertidores_torque/
    convertidor_items en su propio módulo -- y ningún módulo puede
    importar de otro sin arriesgar un ciclo (ver el comentario al inicio
    de este archivo).

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


def _reparar_fk_hacia_tabla_renombrada(db, tabla, sql_create_nueva, columnas_objetivo):
    """Repara una tabla cuya llave foránea quedó apuntando a
    '<algo>_legado' en vez de a la tabla activa real (ej. garantia_items
    apuntando a 'garantias_legado' en vez de 'garantias').

    Cómo pasa esto: cuando _reconstruir_tabla_legada_si_hace_falta
    renombra una tabla padre (ej. 'garantias' -> 'garantias_legado') para
    reconstruirla con el esquema correcto, SQLite reescribe
    automáticamente -como parte del comportamiento normal de ALTER TABLE
    RENAME- el texto de la llave foránea de CUALQUIER tabla hija que la
    referenciaba, dejándola apuntando al nombre nuevo ('garantias_legado')
    en vez de seguir apuntando al nombre 'garantias' (que un instante
    después vuelve a existir, ahora con el esquema correcto y los datos
    reales). El resultado: la tabla hija queda validando sus llaves
    foráneas contra la tabla vieja -que ya no recibe filas nuevas- en vez
    de la tabla activa. El síntoma es que CUALQUIER INSERT nuevo en la
    tabla hija falla con 'FOREIGN KEY constraint failed' aunque el id
    referenciado sí exista, porque SQLite está comparando contra la tabla
    equivocada. (Así se manifestó en producción para garantia_items ->
    garantias: fallaban el 100% de los registros, no uno puntual, porque
    el problema no era ningún dato -- era la definición de la tabla.)

    Se detecta revisando PRAGMA foreign_key_list: si alguna referencia
    apunta a un nombre de tabla que termina en '_legado' (o una variante
    con timestamp tipo '_legado_1234567890'), se reconstruye la tabla
    hija completa (mismo mecanismo que _reconstruir_tabla_legada_si_hace_falta:
    renombrar, recrear con el esquema correcto -que ya vuelve a
    referenciar el nombre correcto porque así está escrito en
    sql_create_nueva-, copiar los datos). Es seguro correr esto en cada
    arranque: si ninguna llave foránea está en ese estado, no hace nada."""
    info = db.execute(f"PRAGMA table_info({tabla})").fetchall()
    if not info:
        return False  # todavía no existe; no hay nada que reparar

    fks = db.execute(f"PRAGMA foreign_key_list({tabla})").fetchall()
    if not any(re.search(r"_legado(_\d+)?$", fk["table"] or "") for fk in fks):
        return False

    nombres_actuales = {c[1] for c in info}
    tabla_legado = f"{tabla}_legado_fk"
    if db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (tabla_legado,)
    ).fetchone():
        tabla_legado = f"{tabla}_legado_fk_{int(datetime.now().timestamp())}"

    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(f"ALTER TABLE {tabla} RENAME TO {tabla_legado}")
    db.executescript(sql_create_nueva)

    cols = [c for c in columnas_objetivo if c in nombres_actuales]
    if cols:
        db.execute(
            f"INSERT INTO {tabla} ({', '.join(cols)}) "
            f"SELECT {', '.join(cols)} FROM {tabla_legado}"
        )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")
    db.commit()
    return True


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


def nombre_automatico(categoria, modelo, codigo_interno, id_num, marca=""):
    """El producto ya no tiene un campo 'nombre' editable: se arma solo a
    partir de los campos que lo identifican (categoria + modelo en la
    base de datos -- de cara al taller son "Modelo" + "Detalle", ver
    relabeling en las rutas /api/categorias, /api/modelos y en el frontend;
    los nombres de columna internos no cambiaron), con respaldos
    razonables si esos vienen vacíos.

    'marca' es opcional (un mismo modelo/detalle puede existir en más de
    una marca -- ej. una versión china y una americana de la misma pieza,
    con distinta calidad y precio -- así que se agrega entre paréntesis al
    final para distinguirlas de un vistazo en cualquier lista o buscador
    que ya muestre 'nombre', sin tener que tocar cada pantalla por
    separado)."""
    partes = [p for p in [(categoria or "").strip(), (modelo or "").strip()] if p]
    base = " - ".join(partes) if partes else ((codigo_interno or "").strip() or f"Producto #{id_num:04d}")
    marca = (marca or "").strip()
    return f"{base} ({marca})" if marca else base

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
# Roles y módulos — usuarios.rol / usuarios.modulos_json (ver migraciones 14
# y 17 en app.py). 'admin' puede todo siempre, sin importar qué haya en
# modulos_json. Para cualquier otro usuario, el acceso real lo define el
# CONJUNTO de módulos en modulos_json (puede ser más de uno — ver migración
# 17, "un usuario tenga acceso a más de un módulo"), no un único rol como
# antes. 'rol' se sigue guardando para usuarios no-admin (queda con el
# primer módulo elegido) solo por compatibilidad/legado y para casos donde
# hace falta UN valor representativo (ej. mensajes de error) — pero NINGÚN
# chequeo de permisos debería comparar contra 'rol' directamente para un
# usuario no-admin; siempre se usa usuario_modulos(). Sesiones que ya
# estaban iniciadas ANTES de estos cambios no tienen 'rol'/'modulos'
# guardado en la sesión (se hizo login antes de que existieran) — se tratan
# como 'admin' mientras no vuelvan a iniciar sesión, para no dejar a nadie
# bloqueado de un día para otro por un despliegue; en el próximo login ya
# les queda su acceso real.
# ---------------------------------------------------------------------------

ROLES_VALIDOS = ["admin", "finanzas", "bodega", "operativo", "convertidores"]

# Módulos asignables a un usuario no-admin (todo ROLES_VALIDOS menos
# 'admin', que no es un módulo sino "acceso a todo"). Usado para validar
# 'modulos' al crear/editar un usuario (ver app.py) y para el orden fijo de
# preferencia en home_del_rol() más abajo.
MODULOS_VALIDOS = [r for r in ROLES_VALIDOS if r != "admin"]


def usuario_rol():
    return session.get("rol") or "admin"


def usuario_modulos():
    """Conjunto de módulos a los que la sesión activa tiene acceso (aparte
    de 'admin', que siempre puede todo y no necesita aparecer acá). Se
    completa en login() a partir de usuarios.modulos_json de esa fila —
    ver migración 17. Una sesión admin no necesita consultar esto (los
    decoradores de abajo dejan pasar a 'admin' antes de mirar módulos)."""
    return set(session.get("modulos") or [])


def requiere_rol(*roles_permitidos):
    """Decorador para endpoints sensibles: exige que la sesión activa sea
    'admin' (que siempre puede todo) o tenga acceso a AL MENOS UNO de los
    módulos indicados (ver usuario_modulos() — un usuario puede tener más
    de un módulo asignado desde la migración 17). Devuelve 403 con un
    mensaje claro si no corresponde — a diferencia de solo ocultar el botón
    en la interfaz, esto se cumple aunque alguien llame al endpoint
    directamente sin pasar por la pantalla. Sin argumentos
    (requiere_rol()) significa "solo admin"."""
    def decorador(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            if usuario_rol() == "admin":
                return vista(*args, **kwargs)
            if not (set(roles_permitidos) & usuario_modulos()):
                permitidos = ", ".join(roles_permitidos) or "admin"
                return jsonify({"error": f"Esta acción es solo para los módulos: {permitidos}. No tienes acceso a ninguno de ellos."}), 403
            return vista(*args, **kwargs)
        return envoltura
    return decorador


# Hasta la migración 14, requiere_rol() solo protegía acciones puntuales
# (guardar, aprobar, eliminar), pero las PÁGINAS en sí (/logistica,
# /operativo, /finanzas, /convertidores, /agente-ia) no tenían ningún
# control: cualquier usuario con sesión iniciada podía entrar a cualquier
# módulo con solo escribir la URL, y la barra lateral se los mostraba
# todos. Se detectó porque un usuario 'bodega' podía ver el resto de los
# módulos igual, cuando la idea de ese rol siempre fue que solo tuviera
# acceso a Logística.
ROL_MODULO_HOME = {
    "finanzas": "/finanzas",
    "bodega": "/logistica",
    "operativo": "/operativo",
    "convertidores": "/convertidores",
}

# Orden fijo de preferencia para decidir a qué módulo aterriza alguien con
# más de uno asignado (ver migración 17) al pedir "/" o una página a la que
# no tiene acceso -- Operativo primero por ser, en general, el módulo de
# uso más diario en un taller; después Logística, Finanzas y Convertidores.
# La barra lateral igual lo deja moverse a cualquiera de sus otros módulos
# desde ahí, esto solo decide DÓNDE aterriza primero.
ORDEN_PREFERENCIA_MODULOS = ["operativo", "bodega", "finanzas", "convertidores"]


def home_del_rol():
    """A dónde mandar a alguien que pidió una página a la que no tiene
    acceso (o la home '/' si no es admin): al primer módulo, en el orden
    de ORDEN_PREFERENCIA_MODULOS, que tenga asignado. Si no tiene NINGÚN
    módulo asignado (caso raro -- un admin se los quitó todos y la persona
    tampoco es admin), va a '/sin-acceso' en vez de esconder el problema
    con un redirect en loop hacia '/'."""
    modulos = usuario_modulos()
    for modulo in ORDEN_PREFERENCIA_MODULOS:
        if modulo in modulos:
            return ROL_MODULO_HOME[modulo]
    return "/sin-acceso"


def requiere_modulo(*roles_permitidos):
    """Decorador para las páginas de cada módulo (ej. /operativo,
    /logistica): si la sesión activa no es 'admin' ni tiene acceso a
    NINGUNO de los módulos indicados (ver usuario_modulos()), la redirige
    a home_del_rol() en vez de mostrar la página. A diferencia de
    requiere_rol() (pensado para endpoints de API, que devuelve 403 en
    JSON), esto es para vistas HTML completas, así que redirige en vez de
    devolver un error."""
    def decorador(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            if usuario_rol() == "admin":
                return vista(*args, **kwargs)
            if not (set(roles_permitidos) & usuario_modulos()):
                return redirect(home_del_rol())
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


# ---------------------------------------------------------------------------
# Stock por proveedor (stock_proveedores): esta tabla lleva un balde de
# cantidad + costo promedio propio POR proveedor, para poder saber de qué
# proveedor viene el stock disponible y no mezclar calidades/costos
# distintos bajo un mismo número. Mismo patrón que merma_proveedores
# (app.py), pero para stock vivo en vez de merma.
#
# productos.costo_compra (el Costo Promedio Ponderado GENERAL, mezclando
# todos los proveedores históricos) sigue existiendo y sigue siendo el que
# se usa en reportes/valorización agregada de inventario (ej. "valor total
# del inventario") -- ahí sí tiene sentido como una foto general. Pero
# desde que existe este desglose por proveedor, dejó de ser confiable para
# costear una salida/consumo real puntual: al ser una media móvil de TODA
# la historia de compras del producto, puede arrastrar para siempre el
# costo de un proveedor que ya ni siquiera tiene stock vivo, mostrando un
# costo que no corresponde a ninguna unidad físicamente disponible ahora
# mismo. Por eso, en todo lugar donde se cobra/costea una unidad que
# efectivamente sale de bodega (ítems de OT/garantía, ver
# agregar_item_orden/agregar_item_garantia en modulos/operativo.py) el
# costo se resuelve SIEMPRE contra el balde de stock_proveedores real (el
# que corresponde al proveedor elegido, o el único balde vivo si no hay
# ambigüedad) y costo_compra queda solo como último recurso para el caso
# límite de que no exista ningún balde vivo (dato legado/inconsistencia).
# ---------------------------------------------------------------------------

def _stockprov_bucket_get(db, producto_id, proveedor_id, factura_id=None):
    """Busca el balde de stock_proveedores para (producto_id, proveedor_id,
    factura_id). 'factura_id' es una dimensión EXTRA opcional (ver Migración
    38): por defecto (None) apunta al balde "de siempre" -- mezclado por
    proveedor, sin importar de qué compra vino cada unidad -- que es como
    se comportan TODAS las compras nacionales, sin excepción. Solo las
    facturas INTERNACIONALES (ver _registrar_factura/es_internacional) usan
    un factura_id real acá, para que cada factura internacional quede en su
    propio balde en vez de mezclarse con el resto de las compras de ese
    proveedor -- la razón es el "tipo de cambio definitivo" (ver
    /api/facturas/<id>/tipo-cambio-definitivo): cuando se sabe el dólar
    real, hay que poder encontrar EXACTAMENTE el stock que vino de esa
    factura para corregirle el costo, sin tocar el resto."""
    if proveedor_id is None:
        if factura_id is None:
            return db.execute(
                "SELECT * FROM stock_proveedores WHERE producto_id = ? AND proveedor_id IS NULL AND factura_id IS NULL",
                (producto_id,),
            ).fetchone()
        return db.execute(
            "SELECT * FROM stock_proveedores WHERE producto_id = ? AND proveedor_id IS NULL AND factura_id = ?",
            (producto_id, factura_id),
        ).fetchone()
    if factura_id is None:
        return db.execute(
            "SELECT * FROM stock_proveedores WHERE producto_id = ? AND proveedor_id = ? AND factura_id IS NULL",
            (producto_id, proveedor_id),
        ).fetchone()
    return db.execute(
        "SELECT * FROM stock_proveedores WHERE producto_id = ? AND proveedor_id = ? AND factura_id = ?",
        (producto_id, proveedor_id, factura_id),
    ).fetchone()


def _stockprov_bucket_entrada(db, producto_id, proveedor_id, cantidad, costo_unitario, recalcular_costo=True, factura_id=None):
    """Suma unidades al balde de stock de un proveedor específico (o NULL =
    sin proveedor conocido, ej. stock inicial o entrada sin factura) y, por
    defecto, recalcula el costo promedio de ESE balde en particular
    (independiente del costo promedio general del producto). Crea el
    balde si no existía.

    'factura_id' (ver _stockprov_bucket_get): None para toda compra
    nacional (el balde sigue siendo "uno por proveedor", como siempre). Un
    id real solo para facturas internacionales -- ahí el balde queda
    exclusivo de esa factura, así que 'recalcular_costo' en la práctica no
    pondera con nada más (el balde nace vacío la primera vez, y una factura
    internacional nunca vuelve a aportarle unidades a un balde ya
    existente salvo que sea la misma factura entrando dos veces, lo que no
    pasa hoy).

    recalcular_costo=False se usa para DEVOLUCIONES de stock (ver
    _aplicar_entrada_stock más abajo, que es quien decide cuándo pasar
    esto en False): esas unidades ya habían sido contempladas en el costo
    promedio de este balde la primera vez que entraron (por la factura
    original) -- la salida NUNCA tocó ese promedio (ver
    _stockprov_bucket_salida), así que su reverso (la devolución) tampoco
    debe tocarlo, para no "contar la compra dos veces". Si el balde ya no
    existía (se vació del todo antes de la devolución, lo que borra la
    fila -- ver _stockprov_bucket_salida), igual se recrea usando
    'costo_unitario' tal cual (es el costo real conocido de esas
    unidades, no una compra nueva que haya que ponderar con nada más)."""
    bucket = _stockprov_bucket_get(db, producto_id, proveedor_id, factura_id)
    ts = now_iso()
    if bucket is None:
        db.execute(
            "INSERT INTO stock_proveedores (producto_id, proveedor_id, factura_id, cantidad, costo_promedio, actualizado_en) VALUES (?, ?, ?, ?, ?, ?)",
            (producto_id, proveedor_id, factura_id, cantidad, costo_unitario if costo_unitario is not None else 0, ts),
        )
        return
    cantidad_actual = bucket["cantidad"]
    nueva_cantidad = cantidad_actual + cantidad
    nuevo_costo = bucket["costo_promedio"]
    if recalcular_costo and costo_unitario is not None:
        valor_actual = cantidad_actual * bucket["costo_promedio"]
        valor_nuevo = cantidad * costo_unitario
        nuevo_costo = (valor_actual + valor_nuevo) / nueva_cantidad if nueva_cantidad > 0 else costo_unitario
    db.execute(
        "UPDATE stock_proveedores SET cantidad = ?, costo_promedio = ?, actualizado_en = ? WHERE id = ?",
        (nueva_cantidad, nuevo_costo, ts, bucket["id"]),
    )


def _stockprov_bucket_salida(db, producto_id, proveedor_id, cantidad, factura_id=None):
    """Descuenta unidades del balde de un proveedor específico (no toca su
    costo promedio, igual que una salida no toca el CPP general). Se usa
    cuando la persona elige a mano de qué proveedor está saliendo el
    producto (formulario de "Registrar movimiento", o el selector de OT/
    garantía cuando hay más de un balde vivo -- ver agregar_item_orden).
    'factura_id' identifica un balde de factura internacional puntual (ver
    _stockprov_bucket_get); None descuenta del balde mezclado de siempre.
    Lanza ValueError si ese balde no tiene stock suficiente -- a propósito,
    para que quien eligió mal el balde vea el error de inmediato en vez de
    dejarlo en negativo."""
    bucket = _stockprov_bucket_get(db, producto_id, proveedor_id, factura_id)
    disponible = bucket["cantidad"] if bucket else 0
    if cantidad > disponible:
        raise ValueError(f"Ese proveedor solo tiene {disponible} unidad(es) disponibles de este producto")
    ts = now_iso()
    nuevo = disponible - cantidad
    if nuevo <= 0:
        db.execute("DELETE FROM stock_proveedores WHERE id = ?", (bucket["id"],))
    else:
        db.execute(
            "UPDATE stock_proveedores SET cantidad = ?, actualizado_en = ? WHERE id = ?",
            (nuevo, ts, bucket["id"]),
        )


def _stockprov_descontar_automatico(db, producto_id, cantidad):
    """Descuenta unidades de los baldes de proveedor de un producto SIN que
    nadie haya elegido a mano de cuál (ej. un repuesto que se consume desde
    una orden de trabajo por la pantalla de Operativo, o una salida de
    Convertidores de Torque -- pantallas que hoy no tienen selector de
    proveedor). Es "mejor esfuerzo": prioriza el balde "sin proveedor
    conocido" y de ahí los baldes actualizados hace más tiempo (aproximación
    FIFO), y si los baldes registrados no alcanzan a cubrir la cantidad
    (puede pasar si arrastran una inconsistencia histórica) simplemente los
    deja en 0 en vez de bloquear la salida real de stock -- este desglose es
    información complementaria, nunca debe ser la causa de que una OT o una
    fabricación de convertidor no pueda completarse."""
    baldes = db.execute(
        "SELECT * FROM stock_proveedores WHERE producto_id = ? ORDER BY (proveedor_id IS NULL) DESC, actualizado_en ASC",
        (producto_id,),
    ).fetchall()
    restante = cantidad
    ts = now_iso()
    for balde in baldes:
        if restante <= 0:
            break
        tomar = min(balde["cantidad"], restante)
        if tomar <= 0:
            continue
        restante -= tomar
        nuevo = balde["cantidad"] - tomar
        if nuevo <= 0:
            db.execute("DELETE FROM stock_proveedores WHERE id = ?", (balde["id"],))
        else:
            db.execute(
                "UPDATE stock_proveedores SET cantidad = ?, actualizado_en = ? WHERE id = ?",
                (nuevo, ts, balde["id"]),
            )


def _stockprov_descontar_preferido(db, producto_id, proveedor_id_preferido, cantidad):
    """Igual que _stockprov_descontar_automatico, pero primero intenta
    descontar del balde de un proveedor preferido (ej. el proveedor que la
    persona ya eligió al registrar una pérdida/merma) y solo si ese balde no
    alcanza, cubre el resto con el resto de los baldes disponibles. Nunca
    lanza ValueError -- mismo criterio "mejor esfuerzo" de arriba."""
    bucket = _stockprov_bucket_get(db, producto_id, proveedor_id_preferido)
    disponible_preferido = bucket["cantidad"] if bucket else 0
    tomar_preferido = min(disponible_preferido, cantidad)
    if tomar_preferido > 0:
        _stockprov_bucket_salida(db, producto_id, proveedor_id_preferido, tomar_preferido)
    restante = cantidad - tomar_preferido
    if restante > 0:
        _stockprov_descontar_automatico(db, producto_id, restante)


def _aplicar_entrada_stock(db, producto_row, cantidad, costo_unitario, motivo, usuario, factura=None, categoria="Compra", proveedor_id=None, recalcular_costo_promedio=True, factura_id_bucket=None):
    """Aplica una entrada de stock a un producto, recalculando el Costo
    Promedio Ponderado si se informa costo_unitario, y deja el movimiento
    registrado con la referencia a la factura si corresponde.
    Devuelve el nuevo stock disponible.

    El balde de stock_proveedores que recibe esta entrada (ver
    _stockprov_bucket_entrada más abajo) se resuelve así: si hay factura,
    del proveedor de esa factura (como ya hacía, sin cambios); si no hay
    factura pero se informó 'proveedor_id' a mano, se usa ese -- pensado
    para devoluciones de stock (ej. al eliminar un ítem de una OT/garantía)
    que necesitan volver EXACTAMENTE al mismo balde de donde habían
    salido, en vez de caer siempre en "sin proveedor conocido" perdiendo
    esa trazabilidad (ver eliminar_item_orden/eliminar_item_garantia en
    modulos/operativo.py). Si no hay ninguno de los dos, cae en "sin
    proveedor conocido", igual que siempre.

    factura_id_bucket (ver _registrar_factura/es_internacional, septiembre
    2026): None para toda entrada normal (el balde de stock_proveedores
    sigue siendo "uno por proveedor", mezclado, como siempre). Solo se
    manda un id real cuando la entrada viene de una factura INTERNACIONAL
    -- ahí el balde queda exclusivo de esa factura en vez de mezclarse con
    el resto de las compras de ese proveedor, para poder encontrar después
    exactamente qué stock vino de esa factura (ver
    /api/facturas/<id>/tipo-cambio-definitivo, que corrige el costo de ese
    balde puntual cuando se sabe el dólar real).

    recalcular_costo_promedio=False (reportado por el taller, julio 2026):
    se usa para DEVOLUCIONES de stock -- al eliminar un ítem que ya había
    salido antes, esas unidades ya habían sido contempladas en el Costo
    Promedio Ponderado (general Y del balde de su proveedor) la primera
    vez que entraron de verdad, por la factura original. Una salida NUNCA
    toca ese promedio (ver _aplicar_salida_stock, a propósito), así que su
    reverso tampoco debe tocarlo -- si se recalculara como si fuera una
    compra nueva sería "contar la compra dos veces", y además se rompía
    en el caso límite de que el stock hubiera llegado a 0 antes de la
    devolución: ahí la fórmula ponderada normal pierde toda la historia
    previa (el "valor actual" en stock era 0) y el promedio quedaba pegado
    al costo de esa única devolución, ignorando por completo el histórico
    real del producto. Con este parámetro en False, la cantidad se suma
    igual pero el costo (general y del balde) queda exactamente como
    estaba antes de la devolución."""
    stock_actual = producto_row["stock_disponible"]
    costo_actual = producto_row["costo_compra"]
    nuevo_stock = stock_actual + cantidad

    nuevo_costo_promedio = costo_actual
    if recalcular_costo_promedio and costo_unitario is not None:
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
    # El proveedor de esta entrada se toma de la factura si la hay (y tiene
    # proveedor asignado); si no hay factura pero se informó 'proveedor_id'
    # a mano (ver docstring de arriba), se usa ese; si no hay ninguno de
    # los dos, queda en el balde "sin proveedor conocido".
    if factura is not None:
        try:
            proveedor_id_entrada = factura["proveedor_id"]
        except (IndexError, KeyError):
            proveedor_id_entrada = None
    else:
        proveedor_id_entrada = proveedor_id
    _stockprov_bucket_entrada(
        db, producto_row["id"], proveedor_id_entrada, cantidad, costo_unitario,
        recalcular_costo=recalcular_costo_promedio, factura_id=factura_id_bucket,
    )
    _recalcular_stock_minimo(db, producto_row["id"])
    return nuevo_stock


def _aplicar_salida_stock(db, producto_row, cantidad, motivo, usuario, categoria="Consumo interno",
                           proveedor_id=None, proveedor_manual=False, factura_id=None):
    """Aplica una salida de stock a un producto (sin tocar el Costo Promedio
    Ponderado — una salida no cambia lo que costó lo que queda en bodega).
    Deja el movimiento registrado y recalcula el stock mínimo, porque una
    salida sí cambia el consumo real que alimenta esa proyección.

    proveedor_id / proveedor_manual controlan el descuento del desglose por
    proveedor (stock_proveedores): si proveedor_manual=True, se descuenta
    ESTRICTAMENTE del balde de proveedor_id indicado (puede lanzar
    ValueError si ese balde no alcanza -- pensado para cuando una persona
    elige el proveedor a mano en el formulario). Si proveedor_manual=False
    (default, para no romper los llamados existentes que todavía no tienen
    ese selector), se descuenta automáticamente del/los balde(s) disponibles
    sin bloquear la operación.

    'factura_id' (solo tiene efecto junto con proveedor_manual=True) apunta
    a un balde de factura internacional puntual en vez del balde mezclado
    de ese proveedor -- ver agregar_item_orden en modulos/operativo.py, que
    lo manda cuando el balde elegido (único o seleccionado a mano entre
    varios) resultó ser de una factura internacional.

    Lanza ValueError si no hay stock disponible suficiente, con el mismo
    mensaje que usa el resto del sistema para este caso — el que llama debe
    capturarlo y devolver un 400 (ver registrar_movimiento en app.py o
    _consumir_repuesto_orden en modulos/operativo.py para el patrón).
    Devuelve el nuevo stock disponible."""
    stock_actual = producto_row["stock_disponible"]
    if cantidad > stock_actual:
        raise ValueError(f"No hay stock suficiente (disponible: {stock_actual})")
    if proveedor_manual:
        # Se valida ANTES de escribir nada, para no dejar el movimiento
        # general aplicado si después falla el descuento del balde de
        # proveedor (mismo criterio que la validación de stock_actual de
        # arriba, ambas fallan "limpio" sin tocar la base).
        bucket = _stockprov_bucket_get(db, producto_row["id"], proveedor_id, factura_id)
        disponible_proveedor = bucket["cantidad"] if bucket else 0
        if cantidad > disponible_proveedor:
            raise ValueError(f"Ese proveedor solo tiene {disponible_proveedor} unidad(es) disponibles de este producto")
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
    if proveedor_manual:
        _stockprov_bucket_salida(db, producto_row["id"], proveedor_id, cantidad, factura_id=factura_id)
    else:
        _stockprov_descontar_automatico(db, producto_row["id"], cantidad)
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


def _pdf_response(buffer, doc, elementos, nombre_archivo, as_attachment=True):
    """as_attachment=True (default, para no cambiar el comportamiento de
    quien ya llamaba a esta función) fuerza la descarga inmediata --
    as_attachment=False manda el PDF 'inline', para que un <iframe> o
    'abrir en pestaña nueva' lo muestren en vez de descargarlos solos."""
    doc.build(elementos)
    buffer.seek(0)
    return send_file(buffer, as_attachment=as_attachment, download_name=nombre_archivo, mimetype="application/pdf")


def _rango_fechas():
    desde = (request.args.get("desde") or "").strip()
    hasta = (request.args.get("hasta") or "").strip()
    return desde, hasta
