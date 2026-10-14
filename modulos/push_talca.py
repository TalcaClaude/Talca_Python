"""
modulos/push_talca.py — escribe hacia Talca.PHP los cambios que el taller
hace desde Python (la dirección contraria al sync: Python -> Talca).

Hoy: clientes y vehículos (que acá son `equipos`). Órdenes van después con el
mismo patrón.

Ver CLAUDE.md para el contexto completo de la integración. Las cuatro
decisiones que más condicionan este módulo:

1. **La atomicidad está en el flag, no en el orden de las llamadas.**
   `encolar_push` se llama DENTRO de la transacción del endpoint (antes del
   commit), junto con `SET push_pendiente = 1`. Las tres escrituras son
   atómicas. El pull sync comprueba push_pendiente antes de sobreescribir,
   así que no existe ventana donde el dato está en Python pero Talca todavía
   tiene la versión vieja y el pull puede pisarnos.

2. **El push es fire-and-forget: no bloquea al usuario.**
   Después del commit, `disparar_push` lanza un hilo ligero que intenta
   el POST o PUT de inmediato. Si falla, la entrada de cola ya existe y el
   job de reintentos (`procesar_pendientes`) la retoma. El endpoint devuelve
   200 sin esperar.

3. **Locking optimista: si Talca tiene un cambio más reciente, gana Talca.**
   En cada PUT se manda `talca_updated_at_conocido` (el timestamp de Talca
   al momento del último pull o push exitoso). Si Talca ya tiene un
   updated_at posterior, responde 409 y Python registra el conflicto sin
   sobreescribir. La fila queda en el estado de Talca y push_pendiente vuelve
   a 0; en la próxima vuelta del pull el dato de Talca llega normalmente.

4. **`dueno` (vehículos) se resuelve al pushear, nunca se guarda en la cola.**
   Es el talca_id del cliente, y el único lugar de donde sale es
   `clientes.talca_id`. Ver `_resolver_dueno` y la columna `requiere_dueno`.
   Si el cliente todavía no tiene talca_id, la entrada queda EN ESPERA en vez
   de fallar: no gasta reintentos ni marca error.

Sobre la conexión a la base: este módulo usa `_conectar_db` de core.py y
nunca importa app.py ni llama a get_db() (que necesita el contexto de Flask).
`encolar_push` recibe la conexión del endpoint que la llama; las funciones
de fondo abren su propia conexión con `_conectar_db`.
"""

import datetime
import json
import logging
import os
import threading
import uuid

from core import _conectar_db, now_iso

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

# Timeout para el push. Más corto que el pull (30 s) porque el push
# es síncrono desde el punto de vista del hilo: un host lento no debe
# dejar ese hilo colgado mucho tiempo.
PUSH_TIMEOUT = float(os.environ.get("TALCA_PUSH_TIMEOUT") or 8.0)

# Backoff exponencial (segundos) para los reintentos automáticos.
# Índice 0 = primer reintento (después del primer fallo), etc.
# Más allá del último: se usa el último valor siempre.
PUSH_BACKOFF = (60, 300, 900, 3600, 21600, 86400)  # 1m 5m 15m 1h 6h 1d

# Cuánto puede una entrada quedarse EN ESPERA antes de que deje de ser normal.
#
# Una espera corta es el sistema funcionando: se creó un cliente y su auto en
# el mismo minuto, y el auto espera a que el push del cliente confirme. Una
# hora es otra cosa. El umbral sale del backoff del cliente: su cuarto
# reintento cae a la hora, así que pasada esa marca el push del cliente ya no
# está "en camino", está trabado, y con él el vehículo.
#
# Debajo del umbral la espera se loguea a INFO; por encima, a WARNING.
PUSH_ESPERA_ALERTA_SEGUNDOS = float(
    os.environ.get("TALCA_PUSH_ESPERA_ALERTA") or 3600
)

# User-Agent igual que el pull: el mismo que supera el filtro de Bluehosting.
# (Ver CLAUDE.md: el hosting cierra el socket sin responder si el UA es
# "python-requests".)
from modulos.sync_talca import TALCA_USER_AGENT, url_entidad  # noqa: E402

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Campos que se sincronizan (local -> Talca)
# ---------------------------------------------------------------------------
#
# (columna_local, columna_talca, tipo)
#
# `tipo` decide cómo se COMPARAN dos valores para saber si el campo cambió.
# No decide cómo se manda: eso lo hace CENTINELAS_SALIDA, más abajo.
#
# Lo que NO está en estas listas no puede viajar. `notas` (clientes y equipos)
# y `motor_cc` (equipos) quedan fuera a propósito: son campos locales, el
# taller anota cosas ahí que no tienen por qué llegar a Talca. `motor_cc` ni
# siquiera existe del otro lado.

CAMPOS_CLIENTES = [
    ("nombre",    "nombre_cliente", "texto"),
    ("rut",       "rut",            "texto"),
    ("direccion", "direccion",      "texto"),
    ("comuna",    "comuna",         "texto"),
    ("telefono",  "telefono",       "texto"),
    ("email",     "email",          "texto"),
]

# El mapeo es el del pull al revés (ver _upsert_vehiculo en sync_talca.py):
# patente->identificador, ano->anio, chasis->vin. Acá se deshace.
#
# `dueno` NO está en la lista, y es deliberado: no se compara ni se difea como
# un campo más. Ver _resolver_dueno.
CAMPOS_VEHICULOS = [
    ("identificador",      "patente",            "texto"),
    ("marca",              "marca",              "texto"),
    ("modelo",             "modelo",             "texto"),
    ("color",              "color",              "texto"),
    ("anio",               "ano",                "entero"),
    ("vin",                "chasis",             "texto"),
    ("modelo_transmision", "modelo_transmision", "texto"),
    # `equipos.puertas` es TEXT (así nació el esquema) y en Talca es int.
    # Se compara como texto y se manda como número.
    ("puertas",            "puertas",            "texto"),
    ("kilometraje",        "kilometraje",        "entero"),
]

# Órdenes. De los 19 campos que el endpoint PHP acepta, acá viajan 14: los que
# el modelo de Python tiene columna donde guardar.
#
# Los otros cinco -- `glosa`, `glosa_obs`, `marca`, `modelo`, `color` -- viven
# hoy solo dentro de `datos_talca_json` y NO se mandan. El PHP los acepta
# igual, así que promoverlos a columnas es un paso posterior que no cambia el
# contrato. Ojo que marca/modelo/color se dejaron fuera del modelo a propósito
# en su momento (se leen del equipo); promoverlos es revertir esa decisión, no
# completar un olvido.
#
# `estado` NO está en la lista: lo que viaja es `estado_talca`, que es la
# columna de Talca espejada. Ver _estado_talca_para_push.
#
# `apro` tampoco: se deriva de presupuesto_estado. Ver _apro_para_push.
CAMPOS_ORDENES = [
    ("trabajo_a_realizar",   "requerimiento", "texto"),
    ("precio_referencial",   "precio",        "numero"),
    ("precio_iva",           "coniva",        "entero"),
    ("kilometraje",          "kilometraje",   "entero"),
    ("estanque_nivel",       "estanque",      "entero"),
    ("observaciones_talca",  "observaciones", "texto"),
    ("referencia",           "origen",        "texto"),
    # Los cinco denormalizados son hechos de ESTA orden, no del vehículo ni
    # del cliente: hay 14 OT con patente distinta a la del auto y 147 donde el
    # nombre difiere del cliente, y esas divergencias son intencionales.
    # Cambiarlos acá no cambia el vehículo ni el cliente.
    ("patente",              "patente",       "texto"),
    ("cliente_nombre_talca", "nombre",        "texto"),
    ("fecha_cierre_talca",   "fecha_cierre",  "texto"),
    ("fecha_entrega_real",   "fecha_salida",  "texto"),
    ("estado_talca",         "estado",        "texto"),
    ("tipo_talca",           "tipo",          "texto"),
    ("presupuesto_estado",   "apro",          "apro"),
]

CAMPOS_POR_ENTIDAD = {
    "clientes":  CAMPOS_CLIENTES,
    "vehiculos": CAMPOS_VEHICULOS,
    "ordenes":   CAMPOS_ORDENES,
}

# Nombre de la tabla local de cada entidad. En Talca son `clientes`,
# `vehiculos` y `orden_trabajo`; acá son `clientes`, `equipos` y
# `ordenes_trabajo`.
TABLA_LOCAL = {
    "clientes":  "clientes",
    "vehiculos": "equipos",
    "ordenes":   "ordenes_trabajo",
}

# Columnas que en Talca son NOT NULL y no aceptan cadena vacía: hay que
# mandarles un 0 donde acá el valor es NULL o ''. `vehiculos.ano`,
# `.puertas` y `.kilometraje` son `int(8) NOT NULL` (verificado en el dump).
#
# Ojo con el orden: el centinela se aplica DESPUÉS de comparar. Si se
# aplicara antes, un cambio de NULL a 0 se vería como "sin cambio" y no
# viajaría nunca.
CENTINELAS_SALIDA = {
    "vehiculos": {"ano": 0, "puertas": 0, "kilometraje": 0},
}


def _normalizar_comparacion(valor, tipo):
    """Valor local llevado a una forma comparable. NULL y '' NO se colapsan
    en los enteros: un kilometraje que pasa de desconocido (NULL) a 0 es un
    cambio real y tiene que viajar."""
    if tipo == "apro":
        # En Talca `apro` es una bandera de texto: 'SI' o vacío.
        return "SI" if (valor or "").strip().lower() == "aprobado" else ""
    if tipo == "numero":
        if valor is None or valor == "":
            return None
        try:
            return float(valor)
        except (TypeError, ValueError):
            return None
    if tipo == "entero":
        if valor is None or valor == "":
            return None
        try:
            return int(valor)
        except (TypeError, ValueError):
            return None
    return (valor or "").strip()


def _valor_para_talca(entidad, columna_talca, valor, tipo="texto"):
    """El valor tal como lo espera Talca, con los centinelas aplicados."""
    if tipo == "apro":
        return "SI" if (valor or "").strip().lower() == "aprobado" else ""
    centinela = CENTINELAS_SALIDA.get(entidad, {}).get(columna_talca)
    if centinela is not None:
        if valor is None or valor == "":
            return centinela
        try:
            return int(valor)
        except (TypeError, ValueError):
            return centinela
    if tipo in ("entero", "numero") and valor in (None, ""):
        # `precio`, `coniva`, `kilometraje` y `estanque` son NOT NULL en Talca.
        # Ojo: acá el 0 es DATO, no centinela -- 1.247 garantías cuestan $0 de
        # verdad y `estanque` en 0 es el estanque vacío. Lo que se traduce es
        # el "no sé", no el cero.
        return 0
    return valor if valor is not None else ""


# ---------------------------------------------------------------------------
# Diff: qué cambió
# ---------------------------------------------------------------------------

def calcular_diff(entidad, antes, despues):
    """Devuelve {columna_talca: valor_listo_para_talca} con los campos que
    cambiaron.

    `antes` es el dict de la fila ANTES de la escritura (se lee antes del
    UPDATE), `despues` son los valores nuevos tal como los escribió el
    endpoint. Ambos usan los nombres de columna locales.

    Si no hay ninguna diferencia, devuelve {} y el endpoint no encola nada.
    Comparación estricta: normalizar mayúsculas, strips, etc. ya lo hizo el
    endpoint antes de llamar a esta función."""
    diff = {}
    for local_col, talca_col, tipo in CAMPOS_POR_ENTIDAD[entidad]:
        if (_normalizar_comparacion(antes.get(local_col), tipo)
                != _normalizar_comparacion(despues.get(local_col), tipo)):
            diff[talca_col] = _valor_para_talca(
                entidad, talca_col, despues.get(local_col), tipo
            )
    return diff


def calcular_diff_cliente(antes, despues):
    """Envoltura histórica. Equivale a calcular_diff('clientes', ...)."""
    return calcular_diff("clientes", antes, despues)


def campos_completos(entidad, fila):
    """Todos los campos de la entidad listos para Talca, para un CREATE.
    `fila` usa los nombres de columna locales (sirve un sqlite3.Row)."""
    payload = {}
    for local_col, talca_col, tipo in CAMPOS_POR_ENTIDAD[entidad]:
        try:
            valor = fila[local_col]
        except (KeyError, IndexError):
            valor = None
        payload[talca_col] = _valor_para_talca(entidad, talca_col, valor, tipo)
    return payload


# ---------------------------------------------------------------------------
# Encolado (dentro de la transacción del endpoint)
# ---------------------------------------------------------------------------

def encolar_push(db, entidad, python_id, talca_id, operacion, campos,
                 talca_updated_at_conocido, requiere_dueno=0):
    """Escribe la entrada de cola en sync_talca_push_pendientes.

    DEBE llamarse con la transacción del endpoint todavía abierta, antes del
    commit. push_pendiente ya debe estar en 1 (el endpoint lo pone en la
    misma sentencia UPDATE).

    Para operacion='crear' genera un UUID como idempotency_key: viaja en cada
    intento del POST para que Talca devuelva el mismo id si ya procesó esta
    entrada (ver php_endpoints_push.php: crear_cliente / crear_vehiculo
    verifican la key antes de insertar). Sin esto, un reintento después de
    respuesta perdida crea un registro duplicado en Talca.

    `requiere_dueno` (solo vehículos) marca que el payload necesita el campo
    `dueno`. Ese valor NO se pasa acá: se resuelve al momento de pushear.

    No sale a la red. No lanza excepciones: si falla, el commit del endpoint
    propagará el error normalmente."""
    idem_key = str(uuid.uuid4()) if operacion == "crear" else ""
    db.execute(
        """
        INSERT INTO sync_talca_push_pendientes
            (entidad, python_id, talca_id, operacion, campos_json,
             talca_updated_at_conocido, idempotency_key, requiere_dueno, creado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entidad,
            python_id,
            talca_id,       # NULL para operacion='crear'
            operacion,
            json.dumps(campos, ensure_ascii=False),
            talca_updated_at_conocido or "",
            idem_key,
            1 if requiere_dueno else 0,
            now_iso(),
        ),
    )


# ---------------------------------------------------------------------------
# Cliente HTTP para el push
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Consumos (fase 3 de la integración del inventario)
#
# Mientras convivan los dos sistemas, el inventario se mueve solo en Talca
# Python, pero la OT de Talca PHP calcula su TOTAL COSTO y UTILIDAD sumando las
# filas de `inventario` con esa ot_relacionada (Nota_model::getOti). Por eso
# cada repuesto cargado a una OT acá viaja allá como filas de inventario (una
# por unidad, como trabaja Talca PHP), y al quitarlo se desvinculan.
#
# La llave entre los dos lados es la `referencia` "TALCA PYTHON ITEM <id>"
# (orden_items.id), que queda en inventario.obs. python_id de la cola es el
# id del ítem. Solo repuestos: mano de obra externa y convertidores tienen
# otras tablas en Talca PHP y quedan para después.
# ---------------------------------------------------------------------------

def referencia_consumo(item_id):
    return f"TALCA PYTHON ITEM {item_id}"


def encolar_consumo(db, item_id):
    """Encola el envío a Talca de un repuesto recién cargado a una OT. Va dentro
    de la transacción del endpoint. Si el ítem no es un repuesto con producto,
    no hace nada."""
    item = db.execute("SELECT * FROM orden_items WHERE id = ?", (item_id,)).fetchone()
    if item is None or item["tipo"] != "repuesto" or not item["producto_id"]:
        return
    producto = db.execute("SELECT categoria, modelo, nombre FROM productos WHERE id = ?",
                          (item["producto_id"],)).fetchone()
    campos = {
        "orden_id": item["orden_id"],
        "cantidad": int(round(item["cantidad"])),
        # Mismo mapeo que "Importar Excel": en Talca Python 'categoria' guarda
        # el MODELO de Talca PHP y 'modelo' la DESCRIPCION.
        "modelo": (producto["categoria"] if producto else "") or "",
        "descripcion": ((producto["modelo"] if producto else "") or item["descripcion"] or "")[:250],
        "costo_neto": int(round(item["costo_unitario"] or 0)),
        "referencia": referencia_consumo(item_id),
    }
    encolar_push(db, "consumos", item_id, None, "crear", campos, "")


def encolar_anulacion_consumo(db, item_id):
    """Encola la desvinculación en Talca de un repuesto que se quitó de la OT.
    Solo si ese ítem alguna vez se encoló para viajar (los anteriores a esta
    función nunca llegaron a Talca)."""
    hubo = db.execute(
        "SELECT 1 FROM sync_talca_push_pendientes WHERE entidad = 'consumos' AND python_id = ? "
        "AND operacion = 'crear' LIMIT 1", (item_id,)).fetchone()
    if hubo:
        encolar_push(db, "consumos", item_id, None, "anular",
                     {"referencia": referencia_consumo(item_id)}, "")


def _ejecutar_consumo(db, id_cola, entrada, campos, cliente_push):
    """La rama de la cola para 'consumos'. Devuelve 'ok' o 'espera' (o lanza
    PushTalcaError, que el llamador registra como fallo)."""
    python_id = entrada["python_id"]
    if entrada["operacion"] == "crear":
        orden = db.execute("SELECT talca_id FROM ordenes_trabajo WHERE id = ?",
                           (campos.get("orden_id"),)).fetchone()
        if orden is None:
            # La OT se borró antes de llegar a Talca: no hay nada que mandar.
            _resolver_entrada(db, id_cola)
            db.commit()
            return "ok"
        if orden["talca_id"] is None:
            _registrar_espera(db, id_cola, "consumos", python_id, entrada["creado_en"],
                              "la OT todavía no tiene número de Talca")
            db.commit()
            return "espera"
        body = {k: v for k, v in campos.items() if k != "orden_id"}
        body["ot"] = int(orden["talca_id"])
        if entrada["idempotency_key"]:
            body["idempotency_key"] = entrada["idempotency_key"]
        resp = cliente_push.consumo("crear", body)
        _resolver_entrada(db, id_cola)
        db.commit()
        _log.info("push consumos CREATE ok: item=%s OT Talca=%s filas=%s%s", python_id, body["ot"],
                  resp.get("ids"), " (idempotente)" if resp.get("idempotente") else "")
        return "ok"

    # anular: espera a que el 'crear' del mismo ítem se haya resuelto; si no,
    # podría desvincular antes de que existan las filas y ese costo quedaría
    # para siempre en la OT de Talca PHP.
    crear_pendiente = db.execute(
        "SELECT 1 FROM sync_talca_push_pendientes WHERE entidad = 'consumos' AND python_id = ? "
        "AND operacion = 'crear' AND resuelto_en = '' LIMIT 1", (python_id,)).fetchone()
    if crear_pendiente:
        _registrar_espera(db, id_cola, "consumos", python_id, entrada["creado_en"],
                          "el repuesto todavía no llegó a Talca")
        db.commit()
        return "espera"
    resp = cliente_push.consumo("anular", {"referencia": campos["referencia"]})
    _resolver_entrada(db, id_cola)
    db.commit()
    _log.info("push consumos ANULAR ok: item=%s filas desvinculadas=%s", python_id, resp.get("desvinculadas"))
    return "ok"


class PushTalcaError(Exception):
    """Error al escribir hacia Talca. El mensaje está pensado para mostrarse
    en logs; nunca incluye la API key."""


class ClientePushTalca:
    """Contrato de acceso para el push. La clase real hace HTTP; los tests
    usan ClientePushTalcaFalso."""

    def crear(self, entidad, campos, idem_key=""):
        """POST /api/<entidad>. Devuelve {"ok": True, "id": <int>, "updated_at": <str>}.

        `idem_key` es el UUID de idempotencia generado al encolar. Talca lo
        guarda en api_idempotency y lo usa para devolver el mismo id si ya
        procesó esta entrada (reintento después de respuesta perdida).

        Lanza PushTalcaError si no fue 2xx o si la respuesta no cumple el
        contrato."""
        raise NotImplementedError

    def actualizar(self, entidad, talca_id, campos, talca_updated_at_conocido):
        """PUT /api/<entidad>/<id>. Devuelve:
          {"ok": True,  "updated_at": <str>}  -- éxito
          {"ok": False, "conflicto": True, "updated_at": <str>,
           "datos_actuales": {...}}             -- 409: Talca tiene cambio más reciente
        Lanza PushTalcaError para cualquier otro problema."""
        raise NotImplementedError

    # -- Envolturas por entidad, para que los llamadores se lean solos ------

    def crear_cliente(self, campos, idem_key=""):
        return self.crear("clientes", campos, idem_key=idem_key)

    def actualizar_cliente(self, talca_id, campos, talca_updated_at_conocido):
        return self.actualizar("clientes", talca_id, campos, talca_updated_at_conocido)

    def crear_vehiculo(self, campos, idem_key=""):
        return self.crear("vehiculos", campos, idem_key=idem_key)

    def consumo(self, accion, body):
        """POST /api/consumos (accion='crear') o /api/consumos/anular
        (accion='anular'). Devuelve el JSON de Talca; lanza PushTalcaError si
        no fue 2xx o no trae ok."""
        raise NotImplementedError

    def actualizar_vehiculo(self, idd, campos, talca_updated_at_conocido):
        return self.actualizar("vehiculos", idd, campos, talca_updated_at_conocido)


class ClientePushTalcaHTTP(ClientePushTalca):
    """El cliente real.

    Mismas variables de entorno que el pull:
        TALCA_API_URL   raíz del sitio (el módulo agrega /api/<entidad>)
        TALCA_API_KEY   el header X-API-Key
    Y una propia:
        TALCA_PUSH_TIMEOUT  segundos (8 por defecto)
    """

    def __init__(self, base_url=None, api_key=None, timeout=None):
        self.base_url = (base_url if base_url is not None
                         else os.environ.get("TALCA_API_URL", "")).strip()
        self.api_key  = (api_key  if api_key  is not None
                         else os.environ.get("TALCA_API_KEY", "")).strip()
        self.timeout  = float(timeout if timeout is not None else PUSH_TIMEOUT)

    def _cabeceras(self):
        return {"X-API-Key": self.api_key, "User-Agent": TALCA_USER_AGENT}

    def _verificar_config(self):
        if not self.base_url:
            raise PushTalcaError(
                "Falta TALCA_API_URL (la raíz del sitio, ej. "
                "https://claude.tallerescalificados.cl)."
            )
        if not self.api_key:
            raise PushTalcaError("Falta TALCA_API_KEY.")
        if _requests is None:  # pragma: no cover
            raise PushTalcaError("Falta la dependencia 'requests'.")

    def crear(self, entidad, campos, idem_key=""):
        self._verificar_config()
        url = url_entidad(self.base_url, entidad)
        body = dict(campos)
        if idem_key:
            body["idempotency_key"] = idem_key
        try:
            resp = _requests.post(
                url, json=body,
                headers=self._cabeceras(), timeout=self.timeout,
            )
        except Exception as exc:
            raise PushTalcaError(f"No se pudo conectar con Talca ({url}): {exc}") from exc

        if resp.status_code not in (200, 201):
            raise PushTalcaError(
                f"Talca respondió HTTP {resp.status_code} al crear en {url}: "
                f"{(resp.text or '')[:200]!r}"
            )
        try:
            datos = resp.json()
        except ValueError as exc:
            raise PushTalcaError(
                f"Talca respondió algo que no es JSON ({url}): {(resp.text or '')[:200]!r}"
            ) from exc

        if not datos.get("ok") or "id" not in datos:
            raise PushTalcaError(
                f"Respuesta inesperada de Talca al crear en {entidad}: {datos!r}"
            )
        return datos  # {"ok": True, "id": <int>, "updated_at": <str>}

    def actualizar(self, entidad, talca_id, campos, talca_updated_at_conocido):
        self._verificar_config()
        url = f"{url_entidad(self.base_url, entidad)}/{talca_id}"
        body = dict(campos)
        body["talca_updated_at_conocido"] = talca_updated_at_conocido or ""
        try:
            resp = _requests.put(
                url, json=body,
                headers=self._cabeceras(), timeout=self.timeout,
            )
        except Exception as exc:
            raise PushTalcaError(f"No se pudo conectar con Talca ({url}): {exc}") from exc

        if resp.status_code == 409:
            try:
                return resp.json()  # {"ok": False, "conflicto": True, ...}
            except ValueError:
                raise PushTalcaError(
                    f"Talca respondió 409 sin JSON válido ({url}): {(resp.text or '')[:200]!r}"
                )

        if resp.status_code != 200:
            raise PushTalcaError(
                f"Talca respondió HTTP {resp.status_code} al actualizar {entidad} "
                f"{talca_id} en {url}: {(resp.text or '')[:200]!r}"
            )
        try:
            return resp.json()  # {"ok": True, "updated_at": <str>}
        except ValueError as exc:
            raise PushTalcaError(
                f"Talca respondió algo que no es JSON ({url}): {(resp.text or '')[:200]!r}"
            ) from exc

    def consumo(self, accion, body):
        self._verificar_config()
        url = url_entidad(self.base_url, "consumos") + ("/anular" if accion == "anular" else "")
        try:
            resp = _requests.post(url, json=body, headers=self._cabeceras(), timeout=self.timeout)
        except Exception as exc:
            raise PushTalcaError(f"No se pudo conectar con Talca ({url}): {exc}") from exc
        if resp.status_code not in (200, 201):
            raise PushTalcaError(
                f"Talca respondió HTTP {resp.status_code} en {url}: {(resp.text or '')[:200]!r}"
            )
        try:
            datos = resp.json()
        except ValueError as exc:
            raise PushTalcaError(
                f"Talca respondió algo que no es JSON ({url}): {(resp.text or '')[:200]!r}"
            ) from exc
        if not datos.get("ok"):
            raise PushTalcaError(f"Respuesta inesperada de Talca en {url}: {datos!r}")
        return datos


# ---------------------------------------------------------------------------
# Órdenes: el estado que ve Talca
# ---------------------------------------------------------------------------

# Los cuatro valores que Talca conoce. Se mandan SIEMPRE en mayúsculas
# exactas: las comparaciones de PHP son sensibles a mayúsculas, a diferencia
# de las de MySQL, así que un 'Cerrada' que MySQL daría por bueno el PHP lo
# trataría como un estado desconocido.
ESTADOS_TALCA_VALIDOS = ("ABIERTA", "CERRADA", "CERRADA BLOQUEADA",
                         "PRESUPUESTO NO ACEPTADO")
ESTADOS_TALCA_CERRADOS = {"CERRADA", "CERRADA BLOQUEADA", "PRESUPUESTO NO ACEPTADO"}

# Los estados de Python que significan "la orden terminó". Debe coincidir con
# ESTADOS_CERRADOS de operativo.py; se repite acá para no importar ese módulo
# (que importa este) y hay un test que exige que las dos listas sean iguales.
ESTADOS_PYTHON_CERRADOS = {"entregado", "cancelado"}


def _estado_talca_para_push(estado_python, estado_talca_actual):
    """El valor que le corresponde a `orden_trabajo.estado`.

    **Lo que viaja es estado_talca, no el estado interno de Python.** Python
    tiene diez estados de flujo y Talca cuatro; mandar `ESPERANDO_REPUESTO` a
    la lista de OT que mira el taller sería confuso, y traducir todo a
    'ABIERTA' perdería el matiz de las cerradas.

    **estado_talca se PRESERVA y solo se recalcula cuando cambia el carácter
    abierto/cerrado de la orden.** Eso es lo que hace que una OT que Talca
    bloqueó siga bloqueada aunque Python la toque: 'CERRADA BLOQUEADA' no
    tiene inversa -- el pull la manda a 'entregado' igual que 'CERRADA'-- así
    que recalcular a ciegas la degradaría a 'CERRADA' en la primera edición.

    'cancelado' tampoco tiene equivalente propio: NO es 'PRESUPUESTO NO
    ACEPTADO' (eso es un presupuesto rechazado, 1 fila en 7.116). Una orden
    cancelada acá se cierra en Talca como 'CERRADA', y el matiz queda en el
    estado de Python."""
    actual = (estado_talca_actual or "").strip().upper()
    cerrada_en_python = (estado_python or "") in ESTADOS_PYTHON_CERRADOS
    cerrada_en_talca = actual in ESTADOS_TALCA_CERRADOS

    if cerrada_en_python == cerrada_en_talca and actual in ESTADOS_TALCA_VALIDOS:
        return actual                      # mismo carácter: no se toca
    return "CERRADA" if cerrada_en_python else "ABIERTA"


# ---------------------------------------------------------------------------
# Resolución de `dueno` (vehículos) e identidad de la orden
# ---------------------------------------------------------------------------

def _resolver_dueno(db, equipo_id):
    """El talca_id del cliente actual del equipo, o None si todavía no tiene.

    Es el único lugar del módulo que produce el valor de `dueno`, y solo lo
    puede sacar de `clientes.talca_id`. Eso es lo que hace imposible mandar
    por error el cliente_id LOCAL: no hay ninguna otra ruta hacia ese campo.
    Un id local en `dueno` colgaría el vehículo de otro cliente sin ningún
    error de por medio, y con 4.700 clientes nadie lo notaría.

    Se resuelve acá y no al encolar para que refleje el dueño de AHORA: si el
    dueño cambió dos veces antes de que la cola drenara, las dos entradas
    pushean el dueño final. Convergente."""
    equipo = db.execute(
        "SELECT cliente_id FROM equipos WHERE id = ?", (equipo_id,)
    ).fetchone()
    if equipo is None or equipo["cliente_id"] is None:
        return None
    cliente = db.execute(
        "SELECT talca_id FROM clientes WHERE id = ?", (equipo["cliente_id"],)
    ).fetchone()
    if cliente is None or cliente["talca_id"] is None:
        return None
    return int(cliente["talca_id"])


def _resolver_identidad_orden(db, orden_id):
    """`(id_dueno, id_vehiculo)` para el CREATE de una orden, o `(None, falta)`
    si alguna de las dos no se puede resolver todavía.

    Son DOS dependencias, no una, y **falta cualquiera detiene el envío**:

    - Sin `id_dueno` la orden nacería colgada del cliente equivocado. Es el
      mismo daño silencioso que `dueno` en vehículos.
    - Sin `id_vehiculo` nacería sin auto, y además el CREATE del lado PHP
      deriva marca/modelo/color con getMarcaByPatente y compañía a partir de
      ese vehículo. En los datos reales hay 15 órdenes así y son basura
      histórica, no algo que valga la pena reproducir.

    Lo que NO se hace es mandar la orden con una resuelta y la otra en 0:
    sería crear en Talca una fila que después hay que corregir, y actualizar
    `id_dueno` / `id_vehiculo` está fuera del alcance del push a propósito
    (son la identidad de la orden).

    Devuelve `(dueno, vehiculo, None)` si las dos resolvieron, o
    `(None, None, motivo)` con el nombre de la que falta."""
    orden = db.execute(
        "SELECT cliente_id, equipo_id FROM ordenes_trabajo WHERE id = ?", (orden_id,)
    ).fetchone()
    if orden is None:
        return None, None, "la orden local no existe"

    dueno = None
    if orden["cliente_id"] is not None:
        fila = db.execute(
            "SELECT talca_id FROM clientes WHERE id = ?", (orden["cliente_id"],)
        ).fetchone()
        dueno = fila["talca_id"] if fila is not None else None

    vehiculo = None
    if orden["equipo_id"] is not None:
        fila = db.execute(
            "SELECT talca_id FROM equipos WHERE id = ?", (orden["equipo_id"],)
        ).fetchone()
        vehiculo = fila["talca_id"] if fila is not None else None

    faltan = []
    if dueno is None:
        faltan.append("el cliente")
    if vehiculo is None:
        faltan.append("el vehículo")
    if faltan:
        return None, None, " y ".join(faltan) + " todavía no tiene(n) talca_id"
    return int(dueno), int(vehiculo), None


def _segundos_esperando(creado_en):
    """Cuánto lleva la entrada esperando. `creado_en` sirve como marca de
    inicio porque la comprobación de espera ocurre ANTES de cualquier salida
    a la red: una entrada que necesita `dueno` o espera desde el primer
    intento, o no espera nunca."""
    try:
        inicio = datetime.datetime.fromisoformat(creado_en)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (datetime.datetime.now() - inicio).total_seconds())


# ---------------------------------------------------------------------------
# Ejecución de un push individual
# ---------------------------------------------------------------------------

def _get_cliente_push():
    """Instancia del cliente HTTP real. Misma idea que get_cliente_talca()
    en sync_talca.py: se construye en el momento de usarlo para leer las
    variables de entorno al momento de la llamada."""
    return ClientePushTalcaHTTP()


def _ejecutar_push_entrada(id_cola, cliente_push=None):
    """Intenta ejecutar la entrada de cola con el id dado.

    Abre su propia conexión a la base (se llama desde hilos sin contexto de
    Flask). No lanza excepciones: los errores quedan en la tabla y en el log.

    Devuelve 'ok', 'conflicto', 'espera' o 'error'."""
    cliente_push = cliente_push or _get_cliente_push()
    db = _conectar_db()
    try:
        entrada = db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (id_cola,)
        ).fetchone()
        if entrada is None or entrada["resuelto_en"]:
            return "ok"  # ya fue resuelta por otro hilo

        claves    = entrada.keys()
        entidad   = entrada["entidad"]
        python_id = entrada["python_id"]
        talca_id  = entrada["talca_id"]
        operacion = entrada["operacion"]
        campos    = json.loads(entrada["campos_json"] or "{}")
        talca_updated_at_conocido = entrada["talca_updated_at_conocido"] or ""
        idem_key  = entrada["idempotency_key"] if "idempotency_key" in claves else ""
        requiere_dueno = entrada["requiere_dueno"] if "requiere_dueno" in claves else 0
        tabla = TABLA_LOCAL.get(entidad, entidad)

        if entidad == "consumos":
            try:
                return _ejecutar_consumo(db, id_cola, entrada, campos, cliente_push)
            except PushTalcaError as exc:
                _registrar_fallo(db, id_cola, str(exc))
                db.commit()
                _log.warning("push consumos fallo (item=%s intento %s): %s",
                             python_id, entrada["intentos"] + 1, exc)
                return "error"

        # -- Espera: la identidad todavía no existe en Talca -----------------
        # Va ANTES de cualquier otra cosa: no tiene sentido gastar un intento
        # de red por algo que se sabe incompleto acá mismo.
        if requiere_dueno and entidad == "vehiculos":
            dueno = _resolver_dueno(db, python_id)
            if dueno is None:
                _registrar_espera(db, id_cola, entidad, python_id,
                                  entrada["creado_en"],
                                  "el cliente todavía no tiene talca_id")
                db.commit()
                return "espera"
            campos["dueno"] = dueno
        elif requiere_dueno and entidad == "ordenes":
            dueno, vehiculo, motivo = _resolver_identidad_orden(db, python_id)
            if motivo:
                _registrar_espera(db, id_cola, entidad, python_id,
                                  entrada["creado_en"], motivo)
                db.commit()
                return "espera"
            campos["id_dueno"] = dueno
            campos["id_vehiculo"] = vehiculo

        try:
            if operacion == "crear":
                # Guardia A: la fila local ya tiene talca_id.
                # Cubre el caso de concurrencia entre el hilo fire-and-forget y
                # procesar_pendientes: si el primero ya guardó el talca_id, el
                # segundo no debe reintentar aunque encuentre la entrada sin
                # resuelto_en.
                #
                # NO cubre el caso de la respuesta perdida (ahí el talca_id
                # local nunca se escribió): para eso está la idempotency_key.
                ya_tiene = db.execute(
                    f"SELECT talca_id FROM {tabla} WHERE id = ? AND talca_id IS NOT NULL",
                    (python_id,),
                ).fetchone()
                if ya_tiene:
                    db.execute(
                        f"UPDATE {tabla} SET push_pendiente = 0 WHERE id = ?", (python_id,)
                    )
                    _resolver_entrada(db, id_cola)
                    db.commit()
                    _log.info(
                        "push %s CREATE: talca_id=%s ya existe en local, "
                        "entrada resuelta sin reintentar (concurrencia)",
                        entidad, ya_tiene["talca_id"],
                    )
                    return "ok"

                # La idempotency_key viaja en cada intento del POST. Talca la
                # guarda en api_idempotency al crear la fila; si la recibe de
                # nuevo, devuelve el id que asignó la primera vez. Esto cierra
                # el caso de respuesta perdida en la red: Talca ya procesó el
                # INSERT, pero Python nunca recibió el id. Sin esta key, el
                # reintento crea un duplicado en Talca.
                resp = cliente_push.crear(entidad, campos, idem_key=idem_key)
                nuevo_talca_id   = int(resp["id"])
                nuevo_updated_at = resp.get("updated_at") or ""
                db.execute(
                    f"""
                    UPDATE {tabla}
                       SET talca_id = ?, talca_updated_at = ?, push_pendiente = 0
                     WHERE id = ?
                    """,
                    (nuevo_talca_id, nuevo_updated_at, python_id),
                )
                if entidad == "ordenes":
                    _refrescar_espejo_orden(db, python_id, campos)
                _resolver_entrada(db, id_cola)
                db.commit()
                _log.info(
                    "push %s CREATE ok: python_id=%s → talca_id=%s%s",
                    entidad, python_id, nuevo_talca_id,
                    " (idempotente: Talca ya lo tenía)" if resp.get("idempotente") else "",
                )
                # Una fila recién creada puede tener otras esperando su
                # talca_id: vehículos que esperan a su cliente, órdenes que
                # esperan a cualquiera de los dos. Sin esto habría que aguantar
                # hasta la próxima vuelta del hilo -- y en local, donde el hilo
                # recién existe, hasta que alguien corriera el comando.
                if entidad in ("clientes", "vehiculos"):
                    _despertar_dependientes(db, entidad, python_id)
                return "ok"

            else:  # actualizar
                resp = cliente_push.actualizar(
                    entidad, talca_id, campos, talca_updated_at_conocido
                )

                if resp.get("conflicto"):
                    # Talca tiene un cambio más reciente: gana Talca.
                    # Registrar para revisión y limpiar el flag; la próxima
                    # vuelta del pull traerá el estado de Talca.
                    db.execute(
                        """
                        INSERT INTO sync_talca_conflictos
                            (entidad, python_id, talca_id,
                             nuestra_version_json, version_talca_json,
                             talca_updated_at, registrado_en)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entidad, python_id, talca_id,
                            json.dumps(campos, ensure_ascii=False),
                            json.dumps(resp.get("datos_actuales") or {}, ensure_ascii=False),
                            resp.get("updated_at") or "",
                            now_iso(),
                        ),
                    )
                    db.execute(
                        f"UPDATE {tabla} SET push_pendiente = 0 WHERE id = ?",
                        (python_id,),
                    )
                    _resolver_entrada(db, id_cola)
                    db.commit()
                    _log.warning(
                        "push %s conflicto: python_id=%s talca_id=%s — "
                        "Talca tiene updated_at=%s, se registró para revisión",
                        entidad, python_id, talca_id, resp.get("updated_at"),
                    )
                    return "conflicto"

                # Éxito normal
                nuevo_updated_at = resp.get("updated_at") or ""
                db.execute(
                    f"""
                    UPDATE {tabla}
                       SET talca_updated_at = ?, push_pendiente = 0
                     WHERE id = ?
                    """,
                    (nuevo_updated_at, python_id),
                )
                if entidad == "ordenes":
                    _refrescar_espejo_orden(db, python_id, campos)
                _resolver_entrada(db, id_cola)
                db.commit()
                _log.info(
                    "push %s UPDATE ok: python_id=%s talca_id=%s campos=%s",
                    entidad, python_id, talca_id, list(campos.keys()),
                )
                return "ok"

        except PushTalcaError as exc:
            _registrar_fallo(db, id_cola, str(exc))
            db.commit()
            _log.warning(
                "push %s fallo (python_id=%s talca_id=%s intento %s): %s",
                entidad, python_id, talca_id, entrada["intentos"] + 1, exc,
            )
            return "error"

    finally:
        db.close()


def _refrescar_espejo_orden(db, orden_id, campos):
    """Anota en `datos_talca_json` los campos que Talca acaba de aceptar.

    El barrido calcula el diff contra ese JSON, así que sin esto la próxima
    edición volvería a mandar lo ya enviado (inofensivo, el PUT es
    idempotente, pero ensucia la cola y los logs).

    **Esto es una fusión, y el pull tiene la regla contraria** —ahí el JSON se
    regenera completo, porque si se fusionara un campo borrado en Talca
    sobreviviría para siempre—. Acá se fusiona a propósito y sin riesgo: son
    valores que Talca respondió 200 sobre ellos, o sea que ya son suyos, y la
    próxima vuelta del pull va a regenerar el JSON entero igual."""
    fila = db.execute(
        "SELECT datos_talca_json FROM ordenes_trabajo WHERE id = ?", (orden_id,)
    ).fetchone()
    if fila is None:
        return
    try:
        espejo = json.loads(fila["datos_talca_json"] or "{}")
    except ValueError:
        espejo = {}
    if not isinstance(espejo, dict):
        espejo = {}
    espejo.update({k: v for k, v in campos.items()
                   if k not in ("id_dueno", "id_vehiculo")})
    db.execute(
        "UPDATE ordenes_trabajo SET datos_talca_json = ? WHERE id = ?",
        (json.dumps(espejo, ensure_ascii=False, default=str), orden_id),
    )


def _resolver_entrada(db, id_cola):
    """Marca la entrada como resuelta (push exitoso o conflicto: en los dos
    casos no hay nada más que reintentar)."""
    db.execute(
        "UPDATE sync_talca_push_pendientes SET resuelto_en = ? WHERE id = ?",
        (now_iso(), id_cola),
    )


def _registrar_espera(db, id_cola, entidad, python_id, creado_en, motivo):
    """La entrada no se puede pushear todavía porque algo de lo que depende no
    existe en Talca. **No incrementa intentos ni toca proximo_intento**:
    esperar no es fallar, y gastar el backoff acá dejaría la entrada durmiendo
    6 horas por algo que se resuelve en cuanto la dependencia confirme.

    El motivo sí se escribe en ultimo_error. Es el único uso de esa columna
    que no es un error, y está para que una entrada trabada se vea: sin esto
    quedaría con intentos=0, sin error y sin resolver, o sea idéntica a una
    que nunca se intentó."""
    segundos = _segundos_esperando(creado_en)
    detalle = (
        f"en espera: {motivo} (fila local {python_id}, "
        f"esperando hace {int(segundos // 60)} min)"
    )
    db.execute(
        "UPDATE sync_talca_push_pendientes SET ultimo_error = ? WHERE id = ?",
        (detalle[:500], id_cola),
    )
    if segundos >= PUSH_ESPERA_ALERTA_SEGUNDOS:
        _log.warning(
            "push %s EN ESPERA hace %.1f h: python_id=%s. El push del cliente "
            "no está avanzando; mientras tanto el pull no actualiza este equipo.",
            entidad, segundos / 3600.0, python_id,
        )
    else:
        _log.info("push %s en espera: python_id=%s (%s)", entidad, python_id, detalle)


def _despertar_dependientes(db, entidad, python_id):
    """Dispara las entradas que estaban esperando el talca_id de esta fila.

    Se llama justo después de que un CREATE confirma. Dos cadenas:
      - un cliente despierta sus vehículos y sus órdenes;
      - un vehículo despierta sus órdenes.

    Los ids se leen acá y el disparo va por hilos aparte, cada uno con su
    propia conexión: esta función corre dentro de _ejecutar_push_entrada, que
    ya tiene la suya."""
    consultas = []
    if entidad == "clientes":
        consultas.append((
            """
            SELECT p.id FROM sync_talca_push_pendientes p
              JOIN equipos e ON e.id = p.python_id
             WHERE p.resuelto_en = '' AND p.entidad = 'vehiculos'
               AND p.requiere_dueno = 1 AND e.cliente_id = ?
            """, (python_id,)))
        consultas.append((
            """
            SELECT p.id FROM sync_talca_push_pendientes p
              JOIN ordenes_trabajo o ON o.id = p.python_id
             WHERE p.resuelto_en = '' AND p.entidad = 'ordenes'
               AND p.requiere_dueno = 1 AND o.cliente_id = ?
            """, (python_id,)))
    elif entidad == "vehiculos":
        consultas.append((
            """
            SELECT p.id FROM sync_talca_push_pendientes p
              JOIN ordenes_trabajo o ON o.id = p.python_id
             WHERE p.resuelto_en = '' AND p.entidad = 'ordenes'
               AND p.requiere_dueno = 1 AND o.equipo_id = ?
            """, (python_id,)))

    for sql, params in consultas:
        try:
            filas = db.execute(sql, params).fetchall()
        except Exception as exc:      # tabla ausente en una base vieja
            _log.error("despertar dependientes (%s): %s", entidad, exc)
            continue
        for fila in filas:
            _log.info("push %s CREATE ok: despertando entrada %s",
                      entidad, fila["id"])
            disparar_push(fila["id"])


def _registrar_fallo(db, id_cola, mensaje_error):
    """Incrementa intentos, calcula proximo_intento (backoff) y guarda el
    último error. La entrada sigue en cola (resuelto_en sigue vacío)."""
    entrada = db.execute(
        "SELECT intentos FROM sync_talca_push_pendientes WHERE id = ?", (id_cola,)
    ).fetchone()
    intentos = (entrada["intentos"] if entrada else 0) + 1
    idx      = min(intentos - 1, len(PUSH_BACKOFF) - 1)
    demora   = PUSH_BACKOFF[idx]
    # proximo_intento se compara con now_iso(), que es hora LOCAL. Calcularlo
    # en UTC le sumaría el desfase del huso al backoff -- en Chile, 4 horas
    # extra a cada reintento, sin ningún síntoma más que la lentitud.
    proximo = (
        datetime.datetime.now() + datetime.timedelta(seconds=demora)
    ).isoformat(timespec="seconds")
    db.execute(
        """
        UPDATE sync_talca_push_pendientes
           SET intentos = ?, proximo_intento = ?, ultimo_error = ?
         WHERE id = ?
        """,
        (intentos, proximo, mensaje_error[:500], id_cola),
    )


# ---------------------------------------------------------------------------
# Disparo inmediato (fire-and-forget desde el endpoint)
# ---------------------------------------------------------------------------

def disparar_push(id_cola, cliente_push=None):
    """Lanza un hilo daemon para intentar el push de inmediato.

    Se llama DESPUÉS del commit del endpoint. Si el hilo falla o el proceso
    muere antes de que termine, la entrada de cola ya está escrita y
    procesar_pendientes la retomará en la próxima vuelta del sync.

    No bloquea al endpoint: el usuario ya tiene su 200/201."""
    def _correr():
        _ejecutar_push_entrada(id_cola, cliente_push)

    t = threading.Thread(target=_correr, daemon=True, name=f"push-talca-{id_cola}")
    t.start()


# ---------------------------------------------------------------------------
# Job de reintentos (al final de cada vuelta del pull)
# ---------------------------------------------------------------------------

def barrer_ordenes_marcadas(db):
    """Arma las entradas de cola de las órdenes que el trigger marcó.

    **Por qué las órdenes se encolan acá y no en el endpoint**, a diferencia
    de clientes y vehículos: `ordenes_trabajo` tiene diez escritores, y el
    trigger `trg_ordenes_push_pendiente` es lo que garantiza que ninguno se
    escape (ver _crear_trigger_push_ordenes). El trigger solo puede levantar
    una bandera; armar la entrada es trabajo de este barrido.

    El diff NO se calcula contra un snapshot "antes" —no hay dónde tomarlo con
    diez escritores— sino contra `datos_talca_json`, que guarda la última fila
    cruda que mandó Talca. O sea que la pregunta que se responde es la
    correcta: *¿en qué difiere esta orden de lo que Talca tiene?*

    Devuelve la lista de ids de cola creados, para poder dispararlos."""
    filas = db.execute(
        """
        SELECT o.* FROM ordenes_trabajo o
         WHERE o.push_pendiente = 1
           AND NOT EXISTS (
               SELECT 1 FROM sync_talca_push_pendientes p
                WHERE p.entidad = 'ordenes' AND p.python_id = o.id
                  AND p.resuelto_en = ''
           )
         ORDER BY o.id
        """
    ).fetchall()

    creados = []
    for orden in filas:
        talca_id = orden["talca_id"]
        # estado_talca se recalcula acá, no en los endpoints: así vale para
        # los diez escritores sin que ninguno tenga que saber de esto.
        estado_talca = _estado_talca_para_push(orden["estado"], orden["estado_talca"])
        actual = dict(orden)
        actual["estado_talca"] = estado_talca
        if estado_talca != (orden["estado_talca"] or ""):
            db.execute(
                "UPDATE ordenes_trabajo SET estado_talca = ? WHERE id = ?",
                (estado_talca, orden["id"]),
            )

        if talca_id is None:
            encolar_push(db, "ordenes", orden["id"], None, "crear",
                         campos_completos("ordenes", actual), "",
                         requiere_dueno=1)
        else:
            conocido = json.loads(orden["datos_talca_json"] or "{}")
            # datos_talca_json usa los nombres de Talca; el diff necesita los
            # locales, así que se traduce al vuelo.
            antes = {
                local: conocido.get(remoto)
                for local, remoto, _t in CAMPOS_ORDENES
            }
            # `apro` viene de Talca como 'SI'/''; del lado local se compara
            # contra presupuesto_estado, así que se lleva a esa escala.
            antes["presupuesto_estado"] = (
                "aprobado" if (conocido.get("apro") or "").strip().upper() == "SI"
                else "pendiente"
            )
            diff = calcular_diff("ordenes", antes, actual)
            if not diff:
                # El trigger marcó algo que no cambia nada del lado de Talca
                # (ej. avanzar de 'diagnostico' a 'reparacion': el carácter
                # abierto/cerrado no cambió). No hay nada que mandar.
                db.execute(
                    "UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE id = ?",
                    (orden["id"],),
                )
                continue
            encolar_push(db, "ordenes", orden["id"], talca_id, "actualizar",
                         diff, orden["talca_updated_at"] or "")

        fila_cola = db.execute(
            "SELECT id FROM sync_talca_push_pendientes"
            " WHERE entidad = 'ordenes' AND python_id = ? AND resuelto_en = ''"
            " ORDER BY id DESC LIMIT 1",
            (orden["id"],),
        ).fetchone()
        if fila_cola:
            creados.append(fila_cola["id"])

    if filas:
        db.commit()
    return creados


def disparar_ordenes_marcadas():
    """Barre las órdenes que el trigger marcó y dispara su push de inmediato.

    Se llama DESPUÉS del commit, desde los escritores conocidos de
    `ordenes_trabajo`. El trigger ya garantizó que el cambio no se pierda; esto
    solo adelanta el envío para que no haya que esperar la vuelta del hilo (60 s
    en local, 300 s en Railway).

    Si alguien agrega un escritor nuevo y se olvida de llamar acá, el cambio
    viaja igual en la vuelta siguiente. Correctitud por defecto, latencia donde
    se puede.

    No lanza: es un adelanto, no un requisito. Abre su propia conexión porque
    el llamador ya cerró su transacción."""
    try:
        db = _conectar_db()
        try:
            ids = barrer_ordenes_marcadas(db)
        finally:
            db.close()
        for id_cola in ids:
            disparar_push(id_cola)
    except Exception as exc:
        _log.error("disparar_ordenes_marcadas: %s", exc)


def procesar_pendientes(cliente_push=None):
    """Procesa las entradas de sync_talca_push_pendientes con resuelto_en=''
    y proximo_intento <= now (o vacío).

    Abre su propia conexión. Se llama al FINAL de cada vuelta del pull, para
    no competir con el upsert que saltará las filas con push_pendiente=1
    (si procesa primero y limpia el flag, el pull del mismo ciclo podría
    sobreescribir sin verlo).

    Un fallo en una entrada no detiene a las demás. Devuelve un resumen
    {'intentados': N, 'ok': N, 'conflicto': N, 'espera': N, 'error': N}."""
    ahora = now_iso()  # ISO local, comparable con proximo_intento
    db = _conectar_db()
    resumen = {"intentados": 0, "ok": 0, "conflicto": 0, "espera": 0, "error": 0}
    try:
        # Las órdenes que marcó el trigger todavía no tienen entrada de cola:
        # se arman acá, antes de seleccionar pendientes, para que entren en
        # esta misma vuelta.
        try:
            barrer_ordenes_marcadas(db)
        except Exception as exc:      # una base vieja puede no tener la tabla
            _log.error("barrido de ordenes marcadas: %s", exc)

        pendientes = db.execute(
            """
            SELECT id FROM sync_talca_push_pendientes
             WHERE resuelto_en = ''
               AND (proximo_intento = '' OR proximo_intento <= ?)
             ORDER BY creado_en
            """,
            (ahora,),
        ).fetchall()
        ids = [r["id"] for r in pendientes]
    finally:
        db.close()

    for id_cola in ids:
        resumen["intentados"] += 1
        resultado = _ejecutar_push_entrada(id_cola, cliente_push)
        resumen[resultado] = resumen.get(resultado, 0) + 1

    if resumen["intentados"]:
        _log.info("push pendientes: %s", resumen)
    return resumen


# ---------------------------------------------------------------------------
# Informe para /api/admin/sync/estado
# ---------------------------------------------------------------------------

def resumen_esperas(db):
    """Entradas que hoy no pueden pushearse porque su cliente todavía no
    tiene talca_id.

    Se calcula en vivo (se vuelve a resolver el dueño) en vez de leer una
    bandera guardada: una bandera se quedaría vieja en cuanto el cliente
    confirme, y este informe existe justamente para que nadie tenga que
    adivinar si lo que muestra sigue siendo cierto.

    Devuelve {'total': N, 'con_alerta': N, 'espera_mas_antigua': <iso|''>,
    'horas_espera_maxima': <float>, 'entradas': [...]} con como mucho 20
    entradas en el detalle."""
    filas = db.execute(
        """
        SELECT p.id, p.python_id, p.operacion, p.creado_en, e.cliente_id
          FROM sync_talca_push_pendientes p
          JOIN equipos e ON e.id = p.python_id
     LEFT JOIN clientes c ON c.id = e.cliente_id
         WHERE p.resuelto_en = ''
           AND p.entidad = 'vehiculos'
           AND p.requiere_dueno = 1
           AND (c.id IS NULL OR c.talca_id IS NULL)
         ORDER BY p.creado_en
        """
    ).fetchall()

    entradas, con_alerta, maxima = [], 0, 0.0
    for fila in filas:
        segundos = _segundos_esperando(fila["creado_en"])
        maxima = max(maxima, segundos)
        if segundos >= PUSH_ESPERA_ALERTA_SEGUNDOS:
            con_alerta += 1
        if len(entradas) < 20:
            entradas.append({
                "id_cola":        fila["id"],
                "equipo_id":      fila["python_id"],
                "cliente_id":     fila["cliente_id"],
                "operacion":      fila["operacion"],
                "esperando_desde": fila["creado_en"],
                "horas_esperando": round(segundos / 3600.0, 2),
                "alerta":          segundos >= PUSH_ESPERA_ALERTA_SEGUNDOS,
            })

    return {
        "total":                len(filas),
        "con_alerta":           con_alerta,
        "umbral_alerta_horas":  round(PUSH_ESPERA_ALERTA_SEGUNDOS / 3600.0, 2),
        "espera_mas_antigua":   filas[0]["creado_en"] if filas else "",
        "horas_espera_maxima":  round(maxima / 3600.0, 2),
        "entradas":             entradas,
    }
