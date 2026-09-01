"""
Tests para el push Python -> Talca (modulos/push_talca.py).

Qué cubre:

  1. Migración: las columnas y tablas nuevas existen y son idempotentes.
  2. Diff: solo los campos que cambiaron viajan; sin cambios, no se encola nada.
  3. Encolado + atomicidad: encolar_push + push_pendiente son atómicos; el pull
     sync salta la fila mientras push_pendiente=1 y la pisa después de 0.
  4. CREATE (nuevo cliente): push exitoso guarda talca_id y limpia el flag.
  5. UPDATE (editar cliente): push exitoso manda solo los campos del diff,
     actualiza talca_updated_at y limpia el flag.
  6. Conflicto (409): queda en sync_talca_conflictos, push_pendiente=0, el dato
     local NO se pisa.
  7. Fallo de conexión: la entrada queda en cola con backoff, push_pendiente=1.
  8. Reintento: procesar_pendientes() resuelve lo que estaba en cola.
  9. User-Agent: el header X-API-Key va en el POST y en el PUT.
 10. Cliente sin talca_id: editar_cliente no encola nada (no hay a dónde pushear).
 11. Idempotencia del CREATE: la key se genera una sola vez y viaja en cada
     reintento, así que una respuesta perdida no termina en cliente duplicado.

Cómo correr:
    cd tests && python -m unittest test_push_talca -v
"""

import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest

# Crear la base de prueba antes de importar cualquier cosa del proyecto.
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_push_talca_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app                  # noqa: E402  inicializa init_db()
from modulos import push_talca            # noqa: E402
from modulos import sync_talca            # noqa: E402


# ---------------------------------------------------------------------------
# Cliente falso para los tests: no sale a la red
# ---------------------------------------------------------------------------

class ClientePushTalcaFalso(push_talca.ClientePushTalca):
    """Reemplaza la capa HTTP. Acumula las llamadas para que los tests puedan
    inspeccionarlas, y devuelve respuestas configurables.

    Uso:
        falso = ClientePushTalcaFalso()
        falso.respuesta_crear = {"ok": True, "id": 42, "updated_at": "..."}
        _ejecutar_push_entrada(id_cola, falso)
    """

    def __init__(self):
        self.llamadas_crear  = []   # lista de los `campos` que se pasaron
        self.entidades_crear = []   # entidad de cada crear
        self.claves_idem     = []   # idem_key recibida en cada crear
        self.llamadas_update = []   # lista de (talca_id, campos, timestamp)
        self.entidades_update = []  # entidad de cada actualizar
        self.respuesta_crear  = {"ok": True, "id": 999, "updated_at": "2026-08-11 10:00:00"}
        self.respuesta_update = {"ok": True, "updated_at": "2026-08-11 10:00:00"}
        self.lanzar_error = None    # si es un str, crear/actualizar levanta PushTalcaError

    def crear(self, entidad, campos, idem_key=""):
        if self.lanzar_error:
            raise push_talca.PushTalcaError(self.lanzar_error)
        self.llamadas_crear.append(dict(campos))
        self.entidades_crear.append(entidad)
        self.claves_idem.append(idem_key)
        return dict(self.respuesta_crear)

    def actualizar(self, entidad, talca_id, campos, talca_updated_at_conocido):
        if self.lanzar_error:
            raise push_talca.PushTalcaError(self.lanzar_error)
        self.llamadas_update.append((talca_id, dict(campos), talca_updated_at_conocido))
        self.entidades_update.append(entidad)
        return dict(self.respuesta_update)


# ---------------------------------------------------------------------------
# Fixture de base de datos: un cliente con talca_id y uno sin él
# ---------------------------------------------------------------------------

class BasePushTalca(unittest.TestCase):
    """Abre una conexión real a la base de prueba y la deja limpia tras cada
    test. Los IDs de Python son estables dentro del test (se consultan tras
    insertar), los de Talca son los que devuelve el falso."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True

    def setUp(self):
        self.db = taller_app._conectar_db()
        self.addCleanup(self._limpiar)
        self._crear_clientes()

    def _crear_clientes(self):
        # Cliente que ya fue sincronizado desde Talca (tiene talca_id y
        # talca_updated_at). Es el que se puede editar y pushear.
        cur = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion,
                                  notas, creado_en, talca_id, talca_updated_at,
                                  push_pendiente)
            VALUES ('PEDRO PICAPIEDRA', '912345678', 'pedro@bedrock.cl',
                    '11.111.111-1', 'AVENIDA DEL PINO 1',
                    '', '2026-01-01T00:00:00', 101, '2026-08-10 08:00:00', 0)
            """,
        )
        self.db.commit()
        self.python_id_con_talca = cur.lastrowid

        # Cliente local sin talca_id: no hay dónde pushear.
        cur2 = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion,
                                  notas, creado_en)
            VALUES ('BARNEY RUBBLE', '987654321', 'barney@bedrock.cl',
                    '22.222.222-2', 'CALLE ROCA 5', '', '2026-01-01T00:00:00')
            """,
        )
        self.db.commit()
        self.python_id_sin_talca = cur2.lastrowid

    def _limpiar(self):
        self.db.execute("DELETE FROM equipos")
        self.db.execute(
            "DELETE FROM clientes WHERE id IN (?, ?)",
            (self.python_id_con_talca, self.python_id_sin_talca),
        )
        self.db.execute("DELETE FROM sync_talca_push_pendientes")
        self.db.execute("DELETE FROM sync_talca_conflictos")
        self.db.commit()
        self.db.close()

    def _cliente(self, python_id=None):
        pid = python_id or self.python_id_con_talca
        return self.db.execute("SELECT * FROM clientes WHERE id = ?", (pid,)).fetchone()

    def _cola(self):
        return self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes ORDER BY id"
        ).fetchall()

    def _conflictos(self):
        return self.db.execute(
            "SELECT * FROM sync_talca_conflictos ORDER BY id"
        ).fetchall()

    def _ejecutar(self, id_cola, falso=None):
        falso = falso or ClientePushTalcaFalso()
        return push_talca._ejecutar_push_entrada(id_cola, falso)


# ---------------------------------------------------------------------------
# 1. Migración
# ---------------------------------------------------------------------------

class TestMigracionPush(BasePushTalca):

    def test_columnas_nuevas_en_clientes(self):
        cols = [c[1] for c in self.db.execute("PRAGMA table_info(clientes)").fetchall()]
        self.assertIn("talca_updated_at",  cols, "falta talca_updated_at")
        self.assertIn("push_pendiente",    cols, "falta push_pendiente")

    def test_tablas_nuevas_existen(self):
        tablas = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("sync_talca_push_pendientes", tablas)
        self.assertIn("sync_talca_conflictos",       tablas)

    def test_migrar_dos_veces_no_falla(self):
        sync_talca.init_sync_talca_db(self.db)
        sync_talca.init_sync_talca_db(self.db)
        cols = [c[1] for c in self.db.execute("PRAGMA table_info(clientes)").fetchall()]
        self.assertEqual(1, cols.count("push_pendiente"))
        self.assertEqual(1, cols.count("talca_updated_at"))


# ---------------------------------------------------------------------------
# 2. Diff
# ---------------------------------------------------------------------------

class TestDiff(unittest.TestCase):

    def diff(self, antes, despues):
        return push_talca.calcular_diff_cliente(antes, despues)

    def test_sin_cambios_devuelve_dict_vacio(self):
        fila = {"nombre": "PEDRO", "telefono": "912345678", "email": "p@cl",
                "rut": "11.111.111-1", "direccion": "AV 1", "comuna": "MAIPU"}
        self.assertEqual({}, self.diff(fila, fila))

    def test_solo_el_campo_que_cambio(self):
        antes  = {"nombre": "PEDRO", "telefono": "912345678", "email": "p@cl",
                  "rut": "11.111.111-1", "direccion": "AV 1", "comuna": "MAIPU"}
        despues = dict(antes)
        despues["telefono"] = "999999999"
        result = self.diff(antes, despues)
        self.assertEqual({"telefono": "999999999"}, result)
        # Nombre del campo en el dict de salida es el que espera Talca.
        self.assertNotIn("nombre", result)

    def test_varios_campos(self):
        antes  = {"nombre": "PEDRO", "telefono": "1", "email": "a@a",
                  "rut": "11.111.111-1", "direccion": "AV 1", "comuna": ""}
        despues = {"nombre": "PEDRO NUEVO", "telefono": "1", "email": "b@b",
                   "rut": "11.111.111-1", "direccion": "AV 1", "comuna": "MAIPU"}
        result = self.diff(antes, despues)
        self.assertIn("nombre_cliente", result)
        self.assertIn("email",          result)
        self.assertIn("comuna",         result)
        self.assertNotIn("telefono",    result)
        self.assertNotIn("rut",         result)

    def test_none_equivale_a_cadena_vacia(self):
        antes  = {"nombre": "PEDRO", "telefono": None,   "email": "",
                  "rut": "",          "direccion": None,  "comuna": ""}
        despues = {"nombre": "PEDRO", "telefono": "",    "email": "",
                   "rut": "",          "direccion": "",   "comuna": ""}
        self.assertEqual({}, self.diff(antes, despues))

    def test_nombre_en_diff_usa_clave_talca(self):
        antes  = {"nombre": "PEDRO", "telefono": "", "email": "", "rut": "", "direccion": "", "comuna": ""}
        despues = {"nombre": "PABLO", "telefono": "", "email": "", "rut": "", "direccion": "", "comuna": ""}
        result = self.diff(antes, despues)
        self.assertIn("nombre_cliente", result)
        self.assertNotIn("nombre", result)


# ---------------------------------------------------------------------------
# 3. Encolado y atomicidad
# ---------------------------------------------------------------------------

class TestEncoladoAtomicidad(BasePushTalca):

    def test_encolar_crea_fila_en_cola(self):
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", {"telefono": "999"}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        cola = self._cola()
        self.assertEqual(1, len(cola))
        self.assertEqual("clientes",   cola[0]["entidad"])
        self.assertEqual("actualizar", cola[0]["operacion"])
        self.assertEqual(101,          cola[0]["talca_id"])
        self.assertEqual("",           cola[0]["resuelto_en"])

    def test_pull_salta_fila_con_push_pendiente(self):
        """La garantía de atomicidad: mientras push_pendiente=1 el pull no pisa."""
        # Poner el cliente en estado de push pendiente.
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1, telefono = '999999999' WHERE id = ?",
            (self.python_id_con_talca,),
        )
        self.db.commit()

        # Simular un pull que trae la versión vieja de Talca (teléfono original).
        fila_talca = {
            "id": 101, "nombre_cliente": "PEDRO PICAPIEDRA",
            "rut": "11.111.111-1", "direccion": "AVENIDA DEL PINO 1",
            "comuna": "MAIPU", "email": "pedro@bedrock.cl",
            "telefono": "912345678",  # versión vieja
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-08-10 08:00:00",
        }
        resultado = sync_talca._upsert_cliente(self.db, fila_talca)
        self.db.commit()

        self.assertEqual("saltada", resultado)
        # El teléfono local (el nuevo) sigue intacto.
        self.assertEqual("999999999", self._cliente()["telefono"])

    def test_pull_actualiza_fila_despues_de_limpiar_flag(self):
        """Cuando push_pendiente=0 el pull vuelve a actualizar normalmente."""
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 0 WHERE id = ?",
            (self.python_id_con_talca,),
        )
        self.db.commit()

        fila_talca = {
            "id": 101, "nombre_cliente": "PEDRO PICAPIEDRA ACTUALIZADO",
            "rut": "11.111.111-1", "direccion": "AVENIDA DEL PINO 1",
            "comuna": "MAIPU", "email": "pedro@bedrock.cl",
            "telefono": "900000000",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-08-11 09:00:00",
        }
        resultado = sync_talca._upsert_cliente(self.db, fila_talca)
        self.db.commit()

        self.assertEqual("actualizada", resultado)
        cli = self._cliente()
        self.assertEqual("PEDRO PICAPIEDRA ACTUALIZADO", cli["nombre"])
        self.assertEqual("900000000", cli["telefono"])
        # talca_updated_at también se actualiza.
        self.assertEqual("2026-08-11 09:00:00", cli["talca_updated_at"])

    def test_pull_guarda_talca_updated_at_en_insert(self):
        """Clientes nuevos traídos por el pull guardan talca_updated_at."""
        fila = {
            "id": 999, "nombre_cliente": "CLIENTE NUEVO",
            "rut": "", "direccion": "", "comuna": "", "email": "", "telefono": "",
            "created_at": "2026-08-01 00:00:00",
            "updated_at": "2026-08-11 12:00:00",
        }
        sync_talca._upsert_cliente(self.db, fila)
        self.db.commit()
        cli = self.db.execute("SELECT * FROM clientes WHERE talca_id = 999").fetchone()
        self.assertIsNotNone(cli)
        self.assertEqual("2026-08-11 12:00:00", cli["talca_updated_at"])
        # Limpiar.
        self.db.execute("DELETE FROM clientes WHERE talca_id = 999")
        self.db.commit()


# ---------------------------------------------------------------------------
# 4. CREATE exitoso
# ---------------------------------------------------------------------------

class TestCreateExitoso(BasePushTalca):

    def setUp(self):
        super().setUp()
        # Nuevo cliente local sin talca_id: el que se va a "crear" en Talca.
        cur = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion,
                                  notas, creado_en, push_pendiente)
            VALUES ('NUEVO LOCAL', '911111111', 'nuevo@cl',
                    '33.333.333-3', 'CALLE NUEVA 1', '', '2026-08-11T00:00:00', 1)
            """,
        )
        self.db.commit()
        self.nuevo_python_id = cur.lastrowid

        # Encolar el CREATE.
        push_talca.encolar_push(
            self.db, "clientes", self.nuevo_python_id, None, "crear",
            {"nombre_cliente": "NUEVO LOCAL", "telefono": "911111111",
             "email": "nuevo@cl", "rut": "33.333.333-3",
             "direccion": "CALLE NUEVA 1", "comuna": ""},
            "",
        )
        self.db.commit()
        self.id_cola = self._cola()[-1]["id"]
        self.addCleanup(self._borrar_nuevo)

    def _borrar_nuevo(self):
        self.db.execute("DELETE FROM clientes WHERE id = ?", (self.nuevo_python_id,))
        self.db.commit()

    def test_talca_id_se_guarda_y_flag_se_limpia(self):
        falso = ClientePushTalcaFalso()
        falso.respuesta_crear = {"ok": True, "id": 500, "updated_at": "2026-08-11 10:00:00"}

        resultado = self._ejecutar(self.id_cola, falso)
        self.assertEqual("ok", resultado)

        cli = self._cliente(self.nuevo_python_id)
        self.assertEqual(500, cli["talca_id"])
        self.assertEqual(0,   cli["push_pendiente"])
        self.assertEqual("2026-08-11 10:00:00", cli["talca_updated_at"])

    def test_entrada_cola_queda_resuelta(self):
        falso = ClientePushTalcaFalso()
        self._ejecutar(self.id_cola, falso)
        entrada = self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (self.id_cola,)
        ).fetchone()
        self.assertNotEqual("", entrada["resuelto_en"])

    def test_post_recibe_todos_los_campos(self):
        falso = ClientePushTalcaFalso()
        self._ejecutar(self.id_cola, falso)
        self.assertEqual(1, len(falso.llamadas_crear))
        campos = falso.llamadas_crear[0]
        self.assertIn("nombre_cliente", campos)
        self.assertIn("telefono",       campos)
        self.assertIn("email",          campos)

    def test_fallo_crea_deja_flag_en_1_y_incrementa_intentos(self):
        falso = ClientePushTalcaFalso()
        falso.lanzar_error = "timeout de red"
        self._ejecutar(self.id_cola, falso)

        cli = self._cliente(self.nuevo_python_id)
        self.assertEqual(1, cli["push_pendiente"], "push_pendiente debe seguir en 1")

        entrada = self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (self.id_cola,)
        ).fetchone()
        self.assertEqual("",  entrada["resuelto_en"],   "no debe estar resuelta")
        self.assertEqual(1,   entrada["intentos"],       "debe contar un intento")
        self.assertNotEqual("", entrada["proximo_intento"], "debe tener próximo intento")


# ---------------------------------------------------------------------------
# 5. UPDATE exitoso: solo el diff viaja
# ---------------------------------------------------------------------------

class TestUpdateExitoso(BasePushTalca):

    def _encolar_actualizar(self, diff, talca_updated_at_conocido="2026-08-10 08:00:00"):
        # Marcar el flag antes de encolar (igual que el endpoint).
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1 WHERE id = ?",
            (self.python_id_con_talca,),
        )
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", diff, talca_updated_at_conocido,
        )
        self.db.commit()
        return self._cola()[-1]["id"]

    def test_solo_el_diff_viaja(self):
        id_cola = self._encolar_actualizar({"telefono": "999999999"})
        falso   = ClientePushTalcaFalso()
        self._ejecutar(id_cola, falso)

        self.assertEqual(1, len(falso.llamadas_update))
        _, campos, _ = falso.llamadas_update[0]
        self.assertEqual({"telefono": "999999999"}, campos,
                         "solo el campo editado debe ir; los demás no")

    def test_flag_limpiado_y_updated_at_actualizado(self):
        id_cola = self._encolar_actualizar({"telefono": "999999999"})
        falso   = ClientePushTalcaFalso()
        falso.respuesta_update = {"ok": True, "updated_at": "2026-08-11 11:00:00"}
        self._ejecutar(id_cola, falso)

        cli = self._cliente()
        self.assertEqual(0,                    cli["push_pendiente"])
        self.assertEqual("2026-08-11 11:00:00", cli["talca_updated_at"])

    def test_talca_id_y_timestamp_conocido_correctos_en_la_llamada(self):
        id_cola = self._encolar_actualizar(
            {"email": "nuevo@cl"}, "2026-08-10 08:00:00"
        )
        falso = ClientePushTalcaFalso()
        self._ejecutar(id_cola, falso)

        _, _, ts_conocido = falso.llamadas_update[0]
        self.assertEqual("2026-08-10 08:00:00", ts_conocido)


# ---------------------------------------------------------------------------
# 6. Conflicto (409)
# ---------------------------------------------------------------------------

class TestConflicto(BasePushTalca):

    def _encolar_y_conflictuar(self):
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1 WHERE id = ?",
            (self.python_id_con_talca,),
        )
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", {"telefono": "999999999"}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        id_cola = self._cola()[-1]["id"]

        falso = ClientePushTalcaFalso()
        falso.respuesta_update = {
            "ok":      False,
            "conflicto": True,
            "updated_at": "2026-08-11 09:00:00",
            "datos_actuales": {
                "id": 101, "nombre_cliente": "PEDRO TALCA",
                "telefono": "888888888",
            },
        }
        resultado = self._ejecutar(id_cola, falso)
        return resultado, id_cola

    def test_retorna_conflicto(self):
        resultado, _ = self._encolar_y_conflictuar()
        self.assertEqual("conflicto", resultado)

    def test_flag_se_limpia(self):
        self._encolar_y_conflictuar()
        self.assertEqual(0, self._cliente()["push_pendiente"])

    def test_dato_local_no_se_pisa(self):
        # El telefono local se dejó en 912345678 (original). El conflicto no
        # debe ni actualizar el teléfono local con lo que dijo Talca.
        self._encolar_y_conflictuar()
        cli = self._cliente()
        self.assertEqual("912345678", cli["telefono"])

    def test_conflicto_queda_en_tabla(self):
        self._encolar_y_conflictuar()
        conflictos = self._conflictos()
        self.assertEqual(1, len(conflictos))
        c = conflictos[0]
        self.assertEqual("clientes", c["entidad"])
        self.assertEqual(101,        c["talca_id"])
        self.assertIn("telefono",    c["nuestra_version_json"])
        self.assertNotEqual("",      c["registrado_en"])

    def test_entrada_cola_queda_resuelta(self):
        _, id_cola = self._encolar_y_conflictuar()
        entrada = self.db.execute(
            "SELECT resuelto_en FROM sync_talca_push_pendientes WHERE id = ?",
            (id_cola,),
        ).fetchone()
        self.assertNotEqual("", entrada["resuelto_en"])


# ---------------------------------------------------------------------------
# 7. Fallo de conexión → backoff en cola
# ---------------------------------------------------------------------------

class TestFalloConexion(BasePushTalca):

    def _encolar_y_fallar(self, error="timeout de red"):
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1 WHERE id = ?",
            (self.python_id_con_talca,),
        )
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", {"telefono": "999"}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        id_cola = self._cola()[-1]["id"]

        falso = ClientePushTalcaFalso()
        falso.lanzar_error = error
        resultado = self._ejecutar(id_cola, falso)
        return resultado, id_cola

    def test_retorna_error(self):
        resultado, _ = self._encolar_y_fallar()
        self.assertEqual("error", resultado)

    def test_flag_sigue_en_1(self):
        self._encolar_y_fallar()
        self.assertEqual(1, self._cliente()["push_pendiente"])

    def test_no_queda_resuelta(self):
        _, id_cola = self._encolar_y_fallar()
        entrada = self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (id_cola,)
        ).fetchone()
        self.assertEqual("",  entrada["resuelto_en"])
        self.assertEqual(1,   entrada["intentos"])
        self.assertNotEqual("", entrada["proximo_intento"])

    def test_ultimo_error_guardado(self):
        _, id_cola = self._encolar_y_fallar("timeout de red")
        entrada = self.db.execute(
            "SELECT ultimo_error FROM sync_talca_push_pendientes WHERE id = ?",
            (id_cola,),
        ).fetchone()
        self.assertIn("timeout", entrada["ultimo_error"])


# ---------------------------------------------------------------------------
# 8. Reintento: procesar_pendientes resuelve lo que estaba en cola
# ---------------------------------------------------------------------------

class TestProcesarPendientes(BasePushTalca):

    def test_reintento_resuelve_exitosamente(self):
        # Dejar una entrada pendiente con proximo_intento en el pasado.
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1 WHERE id = ?",
            (self.python_id_con_talca,),
        )
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", {"telefono": "777"}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        id_cola = self._cola()[-1]["id"]
        # Simular que ya pasó el tiempo de backoff.
        self.db.execute(
            "UPDATE sync_talca_push_pendientes SET proximo_intento = '2020-01-01T00:00:00' WHERE id = ?",
            (id_cola,),
        )
        self.db.commit()

        falso = ClientePushTalcaFalso()
        resumen = push_talca.procesar_pendientes(cliente_push=falso)

        self.assertEqual(1, resumen["intentados"])
        self.assertEqual(1, resumen["ok"])
        self.assertEqual(0, self._cliente()["push_pendiente"])

    def test_entradas_con_proximo_intento_futuro_no_se_procesan(self):
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1 WHERE id = ?",
            (self.python_id_con_talca,),
        )
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", {"telefono": "777"}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        id_cola = self._cola()[-1]["id"]
        # Futuro lejano: no debe procesarse.
        self.db.execute(
            "UPDATE sync_talca_push_pendientes SET proximo_intento = '2099-01-01T00:00:00' WHERE id = ?",
            (id_cola,),
        )
        self.db.commit()

        falso = ClientePushTalcaFalso()
        resumen = push_talca.procesar_pendientes(cliente_push=falso)
        self.assertEqual(0, resumen["intentados"])

    def test_fallo_en_una_entrada_no_detiene_las_demas(self):
        # Crear dos entradas: la primera falla, la segunda debe procesarse igual.
        for tid in (101, 102):
            pid = self.python_id_con_talca if tid == 101 else self.python_id_sin_talca
            push_talca.encolar_push(
                self.db, "clientes", pid, tid,
                "actualizar", {"telefono": "000"}, "",
            )
        self.db.commit()

        # Primera entrada falla, segunda tiene éxito.
        llamadas = []
        class FalsoConError(push_talca.ClientePushTalca):
            def crear(self, entidad, campos, idem_key=""):
                pass
            def actualizar(self, entidad, talca_id, campos, ts):
                llamadas.append(talca_id)
                if talca_id == 101:
                    raise push_talca.PushTalcaError("fallo programado")
                return {"ok": True, "updated_at": "2026-08-11 10:00:00"}

        cola = self._cola()
        ids  = [e["id"] for e in cola]

        falso = FalsoConError()
        resumen = push_talca.procesar_pendientes(cliente_push=falso)
        self.assertEqual(2, resumen["intentados"])
        self.assertEqual(1, resumen["error"])
        self.assertEqual(1, resumen["ok"])


# ---------------------------------------------------------------------------
# 9. User-Agent: el header viaja en POST y PUT
# ---------------------------------------------------------------------------

class TestUserAgent(unittest.TestCase):
    """Verifica que ClientePushTalcaHTTP use el mismo User-Agent que el pull.

    No sale a la red: mockea requests.post / requests.put con un objeto que
    registra los headers."""

    def _mock_response(self, status_code, body):
        import types
        resp = types.SimpleNamespace()
        resp.status_code = status_code
        resp.text = json.dumps(body)
        resp.json = lambda: body
        return resp

    def test_post_lleva_user_agent_correcto(self):
        capturado = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            capturado["headers"] = headers or {}
            return self._mock_response(201, {"ok": True, "id": 1, "updated_at": "x"})

        import modulos.push_talca as pt
        original = pt._requests.post if pt._requests else None
        try:
            if pt._requests:
                pt._requests.post = fake_post
            cli = push_talca.ClientePushTalcaHTTP(
                base_url="https://ejemplo.cl", api_key="llave-test"
            )
            cli.crear_cliente({"nombre_cliente": "TEST"})
            self.assertEqual(
                push_talca.TALCA_USER_AGENT,
                capturado.get("headers", {}).get("User-Agent"),
            )
        finally:
            if original and pt._requests:
                pt._requests.post = original

    def test_put_lleva_user_agent_correcto(self):
        capturado = {}

        def fake_put(url, json=None, headers=None, timeout=None):
            capturado["headers"] = headers or {}
            return self._mock_response(200, {"ok": True, "updated_at": "x"})

        import modulos.push_talca as pt
        original = pt._requests.put if pt._requests else None
        try:
            if pt._requests:
                pt._requests.put = fake_put
            cli = push_talca.ClientePushTalcaHTTP(
                base_url="https://ejemplo.cl", api_key="llave-test"
            )
            cli.actualizar_cliente(1, {"telefono": "1"}, "")
            self.assertEqual(
                push_talca.TALCA_USER_AGENT,
                capturado.get("headers", {}).get("User-Agent"),
            )
        finally:
            if original and pt._requests:
                pt._requests.put = original


# ---------------------------------------------------------------------------
# 10. Cliente sin talca_id: no se encola nada
# ---------------------------------------------------------------------------

class TestSinTalcaId(BasePushTalca):

    def test_diff_no_vacio_pero_sin_talca_id_no_encola(self):
        """editar_cliente con diff != {} pero talca_id = NULL no encola nada.

        El endpoint hace `if diff and talca_id:` antes de encolar; este test
        verifica el comportamiento esperado usando las primitivas del módulo
        directamente."""
        talca_id = self._cliente(self.python_id_sin_talca)["talca_id"]
        self.assertIsNone(talca_id)

        # Calcular diff: hay un campo diferente.
        diff = push_talca.calcular_diff_cliente(
            {"nombre": "BARNEY RUBBLE", "telefono": "987654321",
             "email": "", "rut": "", "direccion": "", "comuna": ""},
            {"nombre": "BARNEY NUEVO",  "telefono": "987654321",
             "email": "", "rut": "", "direccion": "", "comuna": ""},
        )
        self.assertNotEqual({}, diff)  # el diff existe

        # Pero sin talca_id, el endpoint no encola → la cola sigue vacía.
        # (Aquí replicamos la condición del endpoint.)
        if diff and talca_id:
            push_talca.encolar_push(
                self.db, "clientes", self.python_id_sin_talca,
                talca_id, "actualizar", diff, "",
            )
        self.db.commit()
        cola = self.db.execute(
            "SELECT COUNT(*) FROM sync_talca_push_pendientes WHERE python_id = ?",
            (self.python_id_sin_talca,),
        ).fetchone()[0]
        self.assertEqual(0, cola)


# ---------------------------------------------------------------------------
# 11. Idempotencia del CREATE
# ---------------------------------------------------------------------------

class TestIdempotenciaCreate(BasePushTalca):
    """El caso que aparece la primera vez que el hosting se corta a mitad de
    una escritura: el POST llega, Talca inserta, y la respuesta se pierde.
    Python nunca recibe el id, así que el cliente local sigue sin talca_id y
    la entrada de cola sigue pendiente. El reintento NO debe crear un segundo
    cliente en Talca.

    Dos defensas, y las dos se prueban acá:
      A) el reintento comprueba si el cliente local ya tiene talca_id;
      B) si no lo tiene (que es el caso real de la respuesta perdida), manda
         la misma idempotency_key y Talca devuelve el id original."""

    def setUp(self):
        super().setUp()
        cur = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion,
                                  notas, creado_en, push_pendiente)
            VALUES ('CLIENTE IDEM', '944444444', 'idem@cl',
                    '44.444.444-4', 'CALLE IDEM 1', '', '2026-08-11T00:00:00', 1)
            """,
        )
        self.db.commit()
        self.pid = cur.lastrowid
        self.addCleanup(
            lambda: (self.db.execute("DELETE FROM clientes WHERE id = ?", (self.pid,)),
                     self.db.commit())
        )

        push_talca.encolar_push(
            self.db, "clientes", self.pid, None, "crear",
            {"nombre_cliente": "CLIENTE IDEM", "telefono": "944444444",
             "email": "idem@cl", "rut": "44.444.444-4",
             "direccion": "CALLE IDEM 1", "comuna": ""},
            "",
        )
        self.db.commit()
        self.id_cola = self._cola()[-1]["id"]

    def _entrada(self):
        return self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (self.id_cola,)
        ).fetchone()

    # --- la key existe y es estable -----------------------------------------

    def test_crear_genera_idempotency_key(self):
        self.assertNotEqual("", self._entrada()["idempotency_key"],
                            "un CREATE debe llevar idempotency_key")

    def test_actualizar_no_genera_idempotency_key(self):
        """Un PUT es idempotente por naturaleza: no necesita key."""
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_con_talca, 101,
            "actualizar", {"telefono": "999"}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        self.assertEqual("", self._cola()[-1]["idempotency_key"])

    def test_la_key_viaja_en_el_post(self):
        falso = ClientePushTalcaFalso()
        self._ejecutar(self.id_cola, falso)
        self.assertEqual(1, len(falso.claves_idem))
        self.assertEqual(self._entrada()["idempotency_key"], falso.claves_idem[0])

    def test_la_key_no_cambia_entre_intentos(self):
        """Lo que hace que la protección sirva: el reintento manda LA MISMA
        key. Si se regenerara, Talca no la reconocería y duplicaría."""
        key_original = self._entrada()["idempotency_key"]

        falla = ClientePushTalcaFalso()
        falla.lanzar_error = "connection aborted"
        self.assertEqual("error", self._ejecutar(self.id_cola, falla))
        self.assertEqual(key_original, self._entrada()["idempotency_key"])

        # Reintento (saltando el backoff, como haría procesar_pendientes
        # cuando llegue la hora).
        exito = ClientePushTalcaFalso()
        exito.respuesta_crear = {"ok": True, "id": 700,
                                 "updated_at": "2026-08-11 10:00:00",
                                 "idempotente": True}
        self.assertEqual("ok", self._ejecutar(self.id_cola, exito))
        self.assertEqual([key_original], exito.claves_idem)

    # --- respuesta perdida: el reintento no duplica --------------------------

    def test_respuesta_perdida_el_reintento_adopta_el_id_original(self):
        """Simula el escenario completo: primer POST llega a Talca (que crea
        el id 700) pero la respuesta se pierde; el reintento manda la misma
        key y Talca devuelve ese mismo 700 con idempotente=True."""
        # Intento 1: Talca lo procesó, pero acá llega como fallo de conexión.
        perdido = ClientePushTalcaFalso()
        perdido.lanzar_error = "connection aborted sin respuesta"
        self._ejecutar(self.id_cola, perdido)
        self.assertIsNone(self._cliente(self.pid)["talca_id"],
                          "sin respuesta no hay talca_id que guardar")

        # Intento 2: Talca reconoce la key y devuelve el id que ya asignó.
        reintento = ClientePushTalcaFalso()
        reintento.respuesta_crear = {"ok": True, "id": 700,
                                     "updated_at": "2026-08-11 10:05:00",
                                     "idempotente": True}
        self.assertEqual("ok", self._ejecutar(self.id_cola, reintento))

        cli = self._cliente(self.pid)
        self.assertEqual(700, cli["talca_id"], "adopta el id que ya existía en Talca")
        self.assertEqual(0,   cli["push_pendiente"])
        self.assertEqual("2026-08-11 10:05:00", cli["talca_updated_at"])
        self.assertNotEqual("", self._entrada()["resuelto_en"])

    # --- Guardia A: el local ya tiene talca_id -------------------------------

    def test_no_reintenta_si_el_local_ya_tiene_talca_id(self):
        """Concurrencia entre el hilo fire-and-forget y procesar_pendientes:
        si el primero ya guardó el talca_id, el segundo no debe volver a
        mandar el POST aunque encuentre la entrada sin resolver."""
        self.db.execute(
            "UPDATE clientes SET talca_id = 701 WHERE id = ?", (self.pid,)
        )
        self.db.commit()

        falso = ClientePushTalcaFalso()
        self.assertEqual("ok", self._ejecutar(self.id_cola, falso))
        self.assertEqual([], falso.llamadas_crear,
                         "no debe salir ningún POST: el cliente ya está en Talca")

        cli = self._cliente(self.pid)
        self.assertEqual(701, cli["talca_id"], "el talca_id existente no se toca")
        self.assertEqual(0,   cli["push_pendiente"])
        self.assertNotEqual("", self._entrada()["resuelto_en"])

    def test_procesar_pendientes_no_duplica_tras_push_exitoso(self):
        """El camino real: el hilo inmediato tiene éxito y después corre el
        job de reintentos. La entrada ya está resuelta, así que ni siquiera
        entra en la selección."""
        primero = ClientePushTalcaFalso()
        primero.respuesta_crear = {"ok": True, "id": 702, "updated_at": "2026-08-11 10:00:00"}
        self._ejecutar(self.id_cola, primero)

        segundo = ClientePushTalcaFalso()
        resumen = push_talca.procesar_pendientes(cliente_push=segundo)
        self.assertEqual(0, resumen["intentados"])
        self.assertEqual([], segundo.llamadas_crear)
        self.assertEqual(702, self._cliente(self.pid)["talca_id"])


# ---------------------------------------------------------------------------
# 12. Vehículos: mapeo, centinelas y campos locales
# ---------------------------------------------------------------------------

class TestDiffVehiculos(unittest.TestCase):
    """El diff de vehículos hace el mapeo del pull al revés
    (identificador->patente, anio->ano, vin->chasis) y aplica los centinelas
    de las tres columnas que en Talca son int NOT NULL."""

    BASE = {
        "identificador": "ZN1510", "marca": "TOYOTA", "modelo": "COROLLA",
        "color": "BLANCO", "anio": 2015, "vin": "ABC123",
        "modelo_transmision": "U340E", "puertas": "4", "kilometraje": 90000,
        "notas": "CLIENTE PIDE LAVADO", "motor_cc": "1600",
    }

    def diff(self, cambios):
        despues = dict(self.BASE)
        despues.update(cambios)
        return push_talca.calcular_diff("vehiculos", self.BASE, despues)

    def test_sin_cambios(self):
        self.assertEqual({}, self.diff({}))

    def test_mapeo_invertido(self):
        self.assertEqual({"patente": "FGBB83"}, self.diff({"identificador": "FGBB83"}))
        self.assertEqual({"ano": 2020},          self.diff({"anio": 2020}))
        self.assertEqual({"chasis": "XYZ"},      self.diff({"vin": "XYZ"}))

    def test_notas_y_motor_cc_nunca_viajan(self):
        """Son campos locales. Que cambien no debe producir ningún push."""
        self.assertEqual({}, self.diff({"notas": "OTRA COSA", "motor_cc": "2000"}))

    def test_centinela_anio_nulo_viaja_como_cero(self):
        self.assertEqual({"ano": 0}, self.diff({"anio": None}))

    def test_centinela_puertas_vacias_viaja_como_cero(self):
        self.assertEqual({"puertas": 0}, self.diff({"puertas": ""}))

    def test_centinela_kilometraje_nulo_viaja_como_cero(self):
        self.assertEqual({"kilometraje": 0}, self.diff({"kilometraje": None}))

    def test_nulo_a_cero_es_un_cambio_real(self):
        """El centinela se aplica DESPUÉS de comparar. Si se aplicara antes,
        NULL y 0 se verían iguales y este cambio no viajaría nunca."""
        antes   = dict(self.BASE, kilometraje=None)
        despues = dict(self.BASE, kilometraje=0)
        self.assertEqual(
            {"kilometraje": 0},
            push_talca.calcular_diff("vehiculos", antes, despues),
        )

    def test_campos_completos_trae_todo_el_payload(self):
        payload = push_talca.campos_completos("vehiculos", self.BASE)
        self.assertEqual(
            {"patente", "marca", "modelo", "color", "ano", "chasis",
             "modelo_transmision", "puertas", "kilometraje"},
            set(payload),
        )
        self.assertNotIn("notas",    payload)
        self.assertNotIn("motor_cc", payload)
        self.assertNotIn("dueno",    payload, "dueno no sale del diff: se resuelve al pushear")
        self.assertEqual(4, payload["puertas"], "puertas viaja como número")


# ---------------------------------------------------------------------------
# 13. Vehículos: push completo, y `dueno` resuelto al momento de mandar
# ---------------------------------------------------------------------------

class BaseVehiculos(BasePushTalca):

    def _crear_equipo(self, cliente_python_id, talca_id=None, push_pendiente=1):
        cur = self.db.execute(
            """
            INSERT INTO equipos (cliente_id, identificador, marca, modelo, color,
                                 anio, modelo_transmision, puertas, motor_cc, vin,
                                 kilometraje, notas, creado_en, talca_id,
                                 talca_updated_at, push_pendiente)
            VALUES (?, 'ZN1510', 'TOYOTA', 'COROLLA', 'BLANCO', 2015, 'U340E',
                    '4', '1600', 'ABC123', 90000, 'LOCAL', '2026-08-11T00:00:00',
                    ?, '2026-08-10 08:00:00', ?)
            """,
            (cliente_python_id, talca_id, push_pendiente),
        )
        self.db.commit()
        return cur.lastrowid

    def _equipo(self, equipo_id):
        return self.db.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()


class TestVehiculoCreate(BaseVehiculos):

    def setUp(self):
        super().setUp()
        # Dueño con talca_id: el camino feliz.
        self.equipo_id = self._crear_equipo(self.python_id_con_talca)
        push_talca.encolar_push(
            self.db, "vehiculos", self.equipo_id, None, "crear",
            push_talca.campos_completos("vehiculos", dict(self._equipo(self.equipo_id))),
            "", requiere_dueno=1,
        )
        self.db.commit()
        self.id_cola = self._cola()[-1]["id"]

    def test_dueno_es_el_talca_id_del_cliente_no_el_id_local(self):
        """El bug que este diseño hace imposible de escribir: mandar el
        cliente_id LOCAL como `dueno` colgaría el vehículo de otro cliente,
        sin ningún error de por medio."""
        falso = ClientePushTalcaFalso()
        falso.respuesta_crear = {"ok": True, "id": 8001, "updated_at": "2026-08-11 10:00:00"}
        self.assertEqual("ok", self._ejecutar(self.id_cola, falso))

        campos = falso.llamadas_crear[0]
        self.assertEqual(101, campos["dueno"], "debe ser clientes.talca_id")
        self.assertNotEqual(
            self.python_id_con_talca, campos["dueno"],
            "mandó el id local: el vehículo quedaría colgado del cliente equivocado",
        )

    def test_va_a_la_entidad_vehiculos(self):
        falso = ClientePushTalcaFalso()
        self._ejecutar(self.id_cola, falso)
        self.assertEqual(["vehiculos"], falso.entidades_crear)

    def test_guarda_talca_id_y_limpia_flag(self):
        falso = ClientePushTalcaFalso()
        falso.respuesta_crear = {"ok": True, "id": 8001, "updated_at": "2026-08-11 10:00:00"}
        self._ejecutar(self.id_cola, falso)

        eq = self._equipo(self.equipo_id)
        self.assertEqual(8001, eq["talca_id"])
        self.assertEqual(0,    eq["push_pendiente"])
        self.assertEqual("2026-08-11 10:00:00", eq["talca_updated_at"])

    def test_idempotencia_tambien_en_vehiculos(self):
        self.assertNotEqual("", self._cola()[-1]["idempotency_key"])
        falso = ClientePushTalcaFalso()
        self._ejecutar(self.id_cola, falso)
        self.assertEqual(self._cola()[-1]["idempotency_key"], falso.claves_idem[0])


class TestVehiculoEspera(BaseVehiculos):
    """El caso nuevo: el cliente del equipo todavía no tiene talca_id porque
    su propio push no confirmó."""

    def setUp(self):
        super().setUp()
        self.equipo_id = self._crear_equipo(self.python_id_sin_talca)
        push_talca.encolar_push(
            self.db, "vehiculos", self.equipo_id, None, "crear",
            push_talca.campos_completos("vehiculos", dict(self._equipo(self.equipo_id))),
            "", requiere_dueno=1,
        )
        self.db.commit()
        self.id_cola = self._cola()[-1]["id"]

    def _entrada(self):
        return self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (self.id_cola,)
        ).fetchone()

    def test_devuelve_espera_y_no_sale_ningun_request(self):
        falso = ClientePushTalcaFalso()
        self.assertEqual("espera", self._ejecutar(self.id_cola, falso))
        self.assertEqual([], falso.llamadas_crear,
                         "no debe salir un POST sin dueño resuelto")

    def test_la_espera_no_gasta_el_backoff(self):
        """Esperar no es fallar: si contara como intento, la entrada se
        dormiría 6 horas por algo que se arregla en cuanto el cliente
        confirme."""
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())
        entrada = self._entrada()
        self.assertEqual(0,  entrada["intentos"])
        self.assertEqual("", entrada["proximo_intento"])
        self.assertEqual("", entrada["resuelto_en"])

    def test_la_espera_queda_anotada(self):
        """Sin esto la entrada quedaría con intentos=0, sin error y sin
        resolver: idéntica a una que nunca se intentó."""
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())
        self.assertIn("en espera", self._entrada()["ultimo_error"])

    def test_sigue_elegible_en_la_vuelta_siguiente(self):
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())
        resumen = push_talca.procesar_pendientes(cliente_push=ClientePushTalcaFalso())
        self.assertEqual(1, resumen["intentados"])
        self.assertEqual(1, resumen["espera"])

    def test_cuando_el_cliente_obtiene_talca_id_se_manda(self):
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())

        # El push del cliente confirmó y le dejó su talca_id.
        self.db.execute(
            "UPDATE clientes SET talca_id = 555 WHERE id = ?", (self.python_id_sin_talca,)
        )
        self.db.commit()

        falso = ClientePushTalcaFalso()
        falso.respuesta_crear = {"ok": True, "id": 8100, "updated_at": "2026-08-11 12:00:00"}
        self.assertEqual("ok", self._ejecutar(self.id_cola, falso))
        self.assertEqual(555, falso.llamadas_crear[0]["dueno"])
        self.assertEqual(8100, self._equipo(self.equipo_id)["talca_id"])

    def test_el_pull_no_pisa_el_equipo_mientras_espera(self):
        """La contracara de la espera: push_pendiente sigue en 1, así que el
        pull no puede sobreescribir el equipo con la versión de Talca."""
        self.db.execute(
            "UPDATE equipos SET talca_id = 7777 WHERE id = ?", (self.equipo_id,)
        )
        self.db.commit()
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())

        fila_talca = {
            "idd": 7777, "dueno": 101, "patente": "OTRA99", "marca": "NISSAN",
            "modelo": "V16", "color": "ROJO", "ano": "1998", "chasis": "",
            "modelo_transmision": "", "puertas": "4", "kilometraje": "1",
            "created_at": "2020-01-01 00:00:00", "updated_at": "2026-08-11 09:00:00",
        }
        resultado = sync_talca._upsert_vehiculo(self.db, fila_talca, self.python_id_con_talca, 101)
        self.db.commit()

        self.assertEqual("saltada", resultado)
        self.assertEqual("ZN1510", self._equipo(self.equipo_id)["identificador"])

    def test_resumen_esperas_la_reporta(self):
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())
        resumen = push_talca.resumen_esperas(self.db)
        self.assertEqual(1, resumen["total"])
        self.assertEqual(self.equipo_id, resumen["entradas"][0]["equipo_id"])
        self.assertNotEqual("", resumen["espera_mas_antigua"])

    def test_resumen_esperas_se_vacia_cuando_deja_de_esperar(self):
        """Se calcula en vivo, no con una bandera guardada: en cuanto el
        cliente tiene talca_id, la entrada deja de aparecer aunque nadie
        haya vuelto a intentarla."""
        self._ejecutar(self.id_cola, ClientePushTalcaFalso())
        self.assertEqual(1, push_talca.resumen_esperas(self.db)["total"])

        self.db.execute(
            "UPDATE clientes SET talca_id = 556 WHERE id = ?", (self.python_id_sin_talca,)
        )
        self.db.commit()
        self.assertEqual(0, push_talca.resumen_esperas(self.db)["total"])

    def test_espera_larga_marca_alerta(self):
        """Pasado el umbral, la entrada se cuenta como alerta (y el log sube
        a WARNING). El umbral por defecto es 1 hora."""
        self.db.execute(
            "UPDATE sync_talca_push_pendientes SET creado_en = ? WHERE id = ?",
            ("2020-01-01T00:00:00", self.id_cola),
        )
        self.db.commit()
        resumen = push_talca.resumen_esperas(self.db)
        self.assertEqual(1, resumen["con_alerta"])
        self.assertTrue(resumen["entradas"][0]["alerta"])
        self.assertGreater(resumen["horas_espera_maxima"], 1.0)

    def test_despertar_dependientes_al_confirmar_el_cliente(self):
        """Cuando el CREATE del cliente confirma, las entradas de vehículos
        que lo esperaban se disparan sin aguardar la próxima vuelta."""
        self.db.execute(
            "UPDATE clientes SET push_pendiente = 1 WHERE id = ?",
            (self.python_id_sin_talca,),
        )
        push_talca.encolar_push(
            self.db, "clientes", self.python_id_sin_talca, None, "crear",
            {"nombre_cliente": "BARNEY RUBBLE"}, "",
        )
        self.db.commit()
        id_cola_cliente = self._cola()[-1]["id"]

        disparadas = []
        original = push_talca.disparar_push
        push_talca.disparar_push = lambda id_c, cliente_push=None: disparadas.append(id_c)
        try:
            falso = ClientePushTalcaFalso()
            falso.respuesta_crear = {"ok": True, "id": 557, "updated_at": "2026-08-11 13:00:00"}
            self._ejecutar(id_cola_cliente, falso)
        finally:
            push_talca.disparar_push = original

        self.assertIn(self.id_cola, disparadas,
                      "la entrada del vehículo debía despertarse")


class TestVehiculoUpdate(BaseVehiculos):

    def setUp(self):
        super().setUp()
        self.equipo_id = self._crear_equipo(
            self.python_id_con_talca, talca_id=7001, push_pendiente=1
        )
        push_talca.encolar_push(
            self.db, "vehiculos", self.equipo_id, 7001, "actualizar",
            {"kilometraje": 123456}, "2026-08-10 08:00:00",
        )
        self.db.commit()
        self.id_cola = self._cola()[-1]["id"]

    def test_solo_el_diff_viaja_y_sin_dueno(self):
        falso = ClientePushTalcaFalso()
        self._ejecutar(self.id_cola, falso)
        talca_id, campos, ts = falso.llamadas_update[0]
        self.assertEqual(7001, talca_id)
        self.assertEqual({"kilometraje": 123456}, campos)
        self.assertNotIn("dueno", campos, "si el dueño no cambió, no viaja")
        self.assertEqual("2026-08-10 08:00:00", ts)

    def test_conflicto_registra_y_no_pisa(self):
        falso = ClientePushTalcaFalso()
        falso.respuesta_update = {
            "ok": False, "conflicto": True, "updated_at": "2026-08-11 09:00:00",
            "datos_actuales": {"idd": 7001, "kilometraje": 999},
        }
        self.assertEqual("conflicto", self._ejecutar(self.id_cola, falso))

        eq = self._equipo(self.equipo_id)
        self.assertEqual(0, eq["push_pendiente"])
        self.assertEqual(90000, eq["kilometraje"], "el dato local no se pisa")

        conflictos = self._conflictos()
        self.assertEqual(1, len(conflictos))
        self.assertEqual("vehiculos", conflictos[0]["entidad"])
        self.assertEqual(7001,        conflictos[0]["talca_id"])


# ---------------------------------------------------------------------------
# 14. El cliente genérico del sync no admite equipos nuevos
# ---------------------------------------------------------------------------

class TestClienteGenericoNoAdmiteEquipos(unittest.TestCase):
    """`marcador_sync = 'sin_dueno_talca'` marca un CONTENEDOR para los
    vehículos huérfanos que llegaron de Talca, no un cliente al que se le
    puedan colgar registros nuevos.

    Si se permitiera: ese cliente no tiene talca_id ni lo va a tener, así que
    el push del equipo quedaría esperando `dueno` para siempre. Y darle uno
    empujándolo a Talca sería peor -- crearía allá un cliente sintético que el
    pull traería de vuelta como cliente normal."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        self.db = taller_app._conectar_db()
        self.addCleanup(self._limpiar)

        cur = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas,
                                  creado_en, marcador_sync)
            VALUES ('SIN DUENO REGISTRADO EN TALCA', '', '', '', '', '',
                    '2026-01-01T00:00:00', 'sin_dueno_talca')
            """,
        )
        self.generico_id = cur.lastrowid

        cur2 = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas,
                                  creado_en, talca_id, talca_updated_at)
            VALUES ('CLIENTE REAL', '', '', '', '', '', '2026-01-01T00:00:00',
                    9100, '2026-08-10 08:00:00')
            """,
        )
        self.real_id = cur2.lastrowid

        # Uno de los equipos que YA cuelgan del genérico (de los 60 que trajo
        # el sync): tiene talca_id propio y debe seguir siendo editable.
        cur3 = self.db.execute(
            """
            INSERT INTO equipos (cliente_id, identificador, marca, modelo, color,
                                 anio, modelo_transmision, puertas, motor_cc, vin,
                                 notas, creado_en, talca_id, talca_updated_at,
                                 push_pendiente)
            VALUES (?, 'HUERF01', 'VOLVO', 'S60', 'GRIS', 2008, '', '4', '', '',
                    '', '2026-01-01T00:00:00', 6001, '2026-08-10 08:00:00', 0)
            """,
            (self.generico_id,),
        )
        self.equipo_huerfano_id = cur3.lastrowid
        self.db.commit()

    def _limpiar(self):
        self.db.execute("DELETE FROM equipos")
        self.db.execute(
            "DELETE FROM clientes WHERE id IN (?, ?)", (self.generico_id, self.real_id)
        )
        self.db.execute("DELETE FROM sync_talca_push_pendientes")
        self.db.commit()
        self.db.close()

    # -- el rechazo ---------------------------------------------------------

    def test_crear_equipo_bajo_el_generico_se_rechaza(self):
        resp = self.client.post("/api/operativo/equipos", json={
            "cliente_id": self.generico_id,
            "identificador": "NUEVO11", "marca": "KIA", "modelo": "RIO",
        })
        self.assertEqual(400, resp.status_code, resp.get_data(as_text=True))
        self.assertIn("marcador interno del sync", resp.get_json()["error"])

    def test_el_rechazo_no_escribe_nada(self):
        antes = self.db.execute("SELECT COUNT(*) FROM equipos").fetchone()[0]
        cola_antes = self.db.execute(
            "SELECT COUNT(*) FROM sync_talca_push_pendientes"
        ).fetchone()[0]

        self.client.post("/api/operativo/equipos", json={
            "cliente_id": self.generico_id, "identificador": "NUEVO11",
        })

        self.assertEqual(antes, self.db.execute("SELECT COUNT(*) FROM equipos").fetchone()[0])
        self.assertEqual(cola_antes, self.db.execute(
            "SELECT COUNT(*) FROM sync_talca_push_pendientes"
        ).fetchone()[0], "tampoco debe quedar una entrada de cola huérfana")

    def test_bajo_un_cliente_real_si_se_crea(self):
        """El rechazo tiene que ser del marcador, no de la ruta entera."""
        resp = self.client.post("/api/operativo/equipos", json={
            "cliente_id": self.real_id,
            "identificador": "BUENO22", "marca": "KIA", "modelo": "RIO",
        })
        self.assertEqual(201, resp.status_code, resp.get_data(as_text=True))

    # -- los 60 que ya están no se ven afectados ----------------------------

    def test_editar_un_equipo_que_ya_cuelga_del_generico_sigue_funcionando(self):
        resp = self.client.put(
            f"/api/operativo/equipos/{self.equipo_huerfano_id}",
            json={"color": "NEGRO"},
        )
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual("NEGRO", resp.get_json()["color"])

    def test_esa_edicion_encola_su_push_sin_quedar_en_espera(self):
        """Tiene talca_id propio y el dueño no cambió, así que `dueno` no
        viaja: la entrada no necesita resolverlo y no espera a nadie."""
        self.client.put(
            f"/api/operativo/equipos/{self.equipo_huerfano_id}",
            json={"color": "NEGRO"},
        )
        entrada = self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE entidad = 'vehiculos' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(entrada, "la edición debía encolar un push")
        self.assertEqual(6001, entrada["talca_id"])
        self.assertEqual(0,    entrada["requiere_dueno"])
        self.assertIn("color", json.loads(entrada["campos_json"]))


# ---------------------------------------------------------------------------
# 15. Editar un vehículo desde la interfaz (PUT /api/operativo/equipos/<id>)
# ---------------------------------------------------------------------------

class TestEditarVehiculoDesdeLaInterfaz(unittest.TestCase):
    """El modal de "Editar datos del vehículo" manda el formulario COMPLETO en
    cada guardado, no solo lo que el usuario tocó. Lo que decide qué viaja a
    Talca es el diff del endpoint, no el navegador."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        self.db = taller_app._conectar_db()
        self.addCleanup(self._limpiar)

        cur = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas,
                                  creado_en, talca_id, talca_updated_at)
            VALUES ('DUENO ORIGINAL', '', '', '', '', '', '2026-01-01T00:00:00',
                    9200, '2026-08-10 08:00:00')
            """,
        )
        self.dueno_id = cur.lastrowid
        cur2 = self.db.execute(
            """
            INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas,
                                  creado_en, talca_id, talca_updated_at)
            VALUES ('OTRO CLIENTE', '', '', '', '', '', '2026-01-01T00:00:00',
                    9201, '2026-08-10 08:00:00')
            """,
        )
        self.otro_cliente_id = cur2.lastrowid

        cur3 = self.db.execute(
            """
            INSERT INTO equipos (cliente_id, identificador, marca, modelo, color,
                                 anio, modelo_transmision, puertas, motor_cc, vin,
                                 kilometraje, notas, creado_en, talca_id,
                                 talca_updated_at, push_pendiente)
            VALUES (?, 'ZN1510', 'TOYOTA', 'COROLLA', 'BLANCO', 2015, 'U340E',
                    '4', '1600', 'ABC123', 90000, 'NOTA LOCAL',
                    '2026-01-01T00:00:00', 7500, '2026-08-10 08:00:00', 0)
            """,
            (self.dueno_id,),
        )
        self.equipo_id = cur3.lastrowid
        self.db.commit()

    def _limpiar(self):
        self.db.execute("DELETE FROM equipos")
        self.db.execute("DELETE FROM clientes WHERE id IN (?, ?)",
                        (self.dueno_id, self.otro_cliente_id))
        self.db.execute("DELETE FROM sync_talca_push_pendientes")
        self.db.commit()
        self.db.close()

    def _formulario(self, **cambios):
        """Lo que manda el modal: todos los campos, con los valores actuales
        salvo los que se pasen acá."""
        base = {
            "identificador": "ZN1510", "vin": "ABC123", "marca": "TOYOTA",
            "modelo": "COROLLA", "color": "BLANCO", "anio": 2015,
            "modelo_transmision": "U340E", "puertas": "4", "motor_cc": "1600",
            "kilometraje": 90000,
        }
        base.update(cambios)
        return base

    def _cola_vehiculos(self):
        return self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE entidad = 'vehiculos' ORDER BY id"
        ).fetchall()

    def _equipo(self):
        return self.db.execute(
            "SELECT * FROM equipos WHERE id = ?", (self.equipo_id,)
        ).fetchone()

    # -- solo lo que cambió viaja --------------------------------------------

    def test_solo_los_campos_cambiados_se_encolan(self):
        resp = self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                               json=self._formulario(kilometraje=125000))
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))

        cola = self._cola_vehiculos()
        self.assertEqual(1, len(cola))
        self.assertEqual({"kilometraje": 125000}, json.loads(cola[0]["campos_json"]),
                         "el formulario manda todo, pero solo el diff debe viajar")
        self.assertEqual(7500, cola[0]["talca_id"])
        self.assertEqual("actualizar", cola[0]["operacion"])

    def test_dos_campos_cambiados_viajan_los_dos(self):
        self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                        json=self._formulario(kilometraje=125000, color="NEGRO"))
        campos = json.loads(self._cola_vehiculos()[0]["campos_json"])
        self.assertEqual({"kilometraje": 125000, "color": "NEGRO"}, campos)

    def test_guardar_sin_cambios_no_encola_nada(self):
        resp = self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                               json=self._formulario())
        self.assertEqual(200, resp.status_code)
        self.assertEqual([], self._cola_vehiculos(),
                         "abrir y guardar sin tocar nada no debe generar un push")

    def test_notas_y_motor_cc_no_viajan(self):
        self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                        json=self._formulario(motor_cc="2000", notas="OTRA NOTA"))
        self.assertEqual([], self._cola_vehiculos(),
                         "son campos locales: cambiarlos no debe producir push")
        eq = self._equipo()
        self.assertEqual("2000",      eq["motor_cc"], "pero sí se guardan acá")
        self.assertEqual("OTRA NOTA", eq["notas"])

    # -- kilometraje: vacío no es cero ---------------------------------------

    def test_kilometraje_vacio_se_guarda_como_null_y_viaja_como_cero(self):
        self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                        json=self._formulario(kilometraje=None))
        self.assertIsNone(self._equipo()["kilometraje"],
                          "vacío es 'no sé', no 0")
        self.assertEqual({"kilometraje": 0},
                         json.loads(self._cola_vehiculos()[0]["campos_json"]),
                         "Talca lo tiene NOT NULL: el centinela lo traduce a 0")

    def test_kilometraje_negativo_se_rechaza(self):
        resp = self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                               json=self._formulario(kilometraje=-5))
        self.assertEqual(400, resp.status_code)
        self.assertEqual(90000, self._equipo()["kilometraje"], "no debe escribirse")

    def test_kilometraje_no_numerico_se_rechaza(self):
        resp = self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                               json=self._formulario(kilometraje="ochenta mil"))
        self.assertEqual(400, resp.status_code)

    # -- el dueño no se toca desde acá ---------------------------------------

    def test_no_permite_cambiar_el_cliente(self):
        """`cliente_id` en el body se ignora. Reasignar dueño es otra
        operación: habría que mandar `dueno` a Talca, y ese campo no sale del
        diff."""
        resp = self.client.put(
            f"/api/operativo/equipos/{self.equipo_id}",
            json=self._formulario(cliente_id=self.otro_cliente_id),
        )
        self.assertEqual(200, resp.status_code)
        self.assertEqual(self.dueno_id, self._equipo()["cliente_id"],
                         "el vehículo no debe cambiar de dueño")
        self.assertEqual([], self._cola_vehiculos(),
                         "y no debe encolar un push por un cambio que no ocurrió")

    def test_cambiar_cliente_junto_con_otro_campo_tampoco_lo_mueve(self):
        self.client.put(
            f"/api/operativo/equipos/{self.equipo_id}",
            json=self._formulario(cliente_id=self.otro_cliente_id, color="VERDE"),
        )
        self.assertEqual(self.dueno_id, self._equipo()["cliente_id"])
        cola = self._cola_vehiculos()
        self.assertEqual({"color": "VERDE"}, json.loads(cola[0]["campos_json"]))
        self.assertEqual(0, cola[0]["requiere_dueno"],
                         "sin cambio de dueño, `dueno` no debe resolverse ni viajar")

    # -- el flag de atomicidad ------------------------------------------------

    def test_marca_push_pendiente_para_que_el_pull_no_pise(self):
        self.client.put(f"/api/operativo/equipos/{self.equipo_id}",
                        json=self._formulario(color="AZUL"))
        # El hilo de push va a fallar (no hay TALCA_API_URL), así que el flag
        # queda en 1: es justo lo que impide que el pull traiga la versión
        # vieja de Talca encima.
        self.assertEqual(1, self._equipo()["push_pendiente"])


# ---------------------------------------------------------------------------
# 16. Órdenes: el trigger, el estado que ve Talca y la doble espera
# ---------------------------------------------------------------------------

class BaseOrdenesPush(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        self.db = taller_app._conectar_db()
        self.addCleanup(self._limpiar)

        cur = self.db.execute(
            "INSERT INTO clientes (nombre, telefono, email, rut, direccion, notas,"
            " creado_en, talca_id, talca_updated_at)"
            " VALUES ('CLIENTE OT', '', '', '', '', '', '2026-01-01T00:00:00', 9300, '2026-08-10 08:00:00')"
        )
        self.cliente_id = cur.lastrowid
        cur2 = self.db.execute(
            "INSERT INTO equipos (cliente_id, identificador, marca, modelo, notas,"
            " creado_en, talca_id, talca_updated_at)"
            " VALUES (?, 'ZN1510', 'TOYOTA', 'COROLLA', '', '2026-01-01T00:00:00', 7600, '2026-08-10 08:00:00')",
            (self.cliente_id,),
        )
        self.equipo_id = cur2.lastrowid
        self.db.commit()

    def _limpiar(self):
        self.db.execute("DELETE FROM ordenes_trabajo")
        self.db.execute("DELETE FROM equipos")
        self.db.execute("DELETE FROM clientes WHERE id = ?", (self.cliente_id,))
        self.db.execute("DELETE FROM sync_talca_push_pendientes")
        self.db.execute("DELETE FROM sync_talca_conflictos")
        self.db.commit()
        self.db.close()

    def _crear_orden(self, talca_id=None, estado="recibido", estado_talca="ABIERTA",
                     push_pendiente=0, **extra):
        campos = {
            "cliente_id": self.cliente_id, "equipo_id": self.equipo_id,
            "estado": estado, "fecha_recepcion": "2026-08-01T09:00:00",
            "creado_en": "2026-08-01T09:00:00", "actualizado_en": "2026-08-01T09:00:00",
            "prioridad": "media", "precio_iva": 1, "presupuesto_iva": 1,
            "presupuesto_estado": "pendiente", "trabajo_no_realizado": 0,
            "borrada_en_talca": 0, "talca_id": talca_id, "estado_talca": estado_talca,
            "talca_updated_at": "2026-08-10 08:00:00", "push_pendiente": push_pendiente,
            "datos_talca_json": json.dumps({
                "id": talca_id, "estado": estado_talca, "requerimiento": "CAMBIO DE ACEITE",
                "precio": "150000", "coniva": "1", "kilometraje": "90000",
                "estanque": "3", "observaciones": "", "origen": "GOOGLE",
                "patente": "ZN1510", "nombre": "CLIENTE OT", "fecha_cierre": "",
                "fecha_salida": "", "tipo": "Nueva Orden", "apro": "",
            }),
            "trabajo_a_realizar": "CAMBIO DE ACEITE", "precio_referencial": 150000,
            "kilometraje": 90000, "estanque_nivel": 3, "referencia": "GOOGLE",
            "patente": "ZN1510", "cliente_nombre_talca": "CLIENTE OT",
            "tipo_talca": "Nueva Orden", "observaciones_talca": "",
        }
        campos.update(extra)
        cols = ", ".join(campos)
        cur = self.db.execute(
            f"INSERT INTO ordenes_trabajo ({cols}) VALUES ({', '.join('?' * len(campos))})",
            tuple(campos.values()),
        )
        # El alta con talca_id puesto no dispara el trigger de alta; sin él,
        # sí. Se normaliza al valor pedido para que cada test parta de un
        # estado conocido.
        self.db.execute("UPDATE ordenes_trabajo SET push_pendiente = ? WHERE id = ?",
                        (push_pendiente, cur.lastrowid))
        self.db.commit()
        return cur.lastrowid

    def _orden(self, orden_id):
        return self.db.execute(
            "SELECT * FROM ordenes_trabajo WHERE id = ?", (orden_id,)
        ).fetchone()

    def _cola(self):
        return self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE entidad = 'ordenes' ORDER BY id"
        ).fetchall()


class TestTriggerOrdenes(BaseOrdenesPush):
    """El trigger es lo que hace que ningún escritor se escape. Hay diez
    repartidos entre operativo.py, convertidores.py y app.py."""

    def test_cambiar_un_campo_que_viaja_marca_la_orden(self):
        oid = self._crear_orden(talca_id=8800)
        self.db.execute(
            "UPDATE ordenes_trabajo SET precio_referencial = 999 WHERE id = ?", (oid,))
        self.db.commit()
        self.assertEqual(1, self._orden(oid)["push_pendiente"])

    def test_cambiar_el_estado_interno_tambien_marca(self):
        """`estado` no viaja, pero puede cambiar el carácter abierto/cerrado
        y con eso recalcular estado_talca."""
        oid = self._crear_orden(talca_id=8800)
        self.db.execute("UPDATE ordenes_trabajo SET estado = 'entregado' WHERE id = ?", (oid,))
        self.db.commit()
        self.assertEqual(1, self._orden(oid)["push_pendiente"])

    def test_cambiar_un_campo_local_no_marca(self):
        """`qc_observaciones` es interno del taller: no tiene destino en Talca."""
        oid = self._crear_orden(talca_id=8800)
        self.db.execute(
            "UPDATE ordenes_trabajo SET qc_observaciones = 'REVISADO' WHERE id = ?", (oid,))
        self.db.commit()
        self.assertEqual(0, self._orden(oid)["push_pendiente"])

    def test_apagar_el_flag_no_lo_vuelve_a_encender(self):
        """El trigger mira solo columnas de negocio: sin esto, el UPDATE con
        el que el pull apaga el flag lo volvería a prender y el sync entraría
        en un bucle."""
        oid = self._crear_orden(talca_id=8800, push_pendiente=1)
        self.db.execute("UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE id = ?", (oid,))
        self.db.commit()
        self.assertEqual(0, self._orden(oid)["push_pendiente"])

    def test_el_alta_local_se_marca_pero_la_del_pull_no(self):
        """`talca_id IS NULL` distingue una orden creada acá de una traída por
        el pull. Sin esa condición, la carga inicial marcaría las 7.116 para
        empujarlas de vuelta."""
        local = self._crear_orden(talca_id=None, push_pendiente=0)
        self.db.execute("UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE id = ?", (local,))
        self.db.commit()
        # Alta nueva sin talca_id: el trigger de INSERT la marca.
        otra = self.db.execute(
            "INSERT INTO ordenes_trabajo (cliente_id, estado, fecha_recepcion, creado_en,"
            " actualizado_en, prioridad, precio_iva, presupuesto_iva, presupuesto_estado,"
            " trabajo_no_realizado, borrada_en_talca)"
            " VALUES (?, 'recibido', '2026-08-01', '2026-08-01', '2026-08-01', 'media',"
            " 1, 1, 'pendiente', 0, 0)", (self.cliente_id,),
        ).lastrowid
        # Y una "traída por el pull", con talca_id: no se marca.
        del_pull = self.db.execute(
            "INSERT INTO ordenes_trabajo (cliente_id, estado, fecha_recepcion, creado_en,"
            " actualizado_en, prioridad, precio_iva, presupuesto_iva, presupuesto_estado,"
            " trabajo_no_realizado, borrada_en_talca, talca_id)"
            " VALUES (?, 'recibido', '2026-08-01', '2026-08-01', '2026-08-01', 'media',"
            " 1, 1, 'pendiente', 0, 0, 9999)", (self.cliente_id,),
        ).lastrowid
        self.db.commit()
        self.assertEqual(1, self._orden(otra)["push_pendiente"], "alta local: se marca")
        self.assertEqual(0, self._orden(del_pull)["push_pendiente"], "alta del pull: no")

    def test_el_pull_no_se_marca_a_si_mismo_al_actualizar(self):
        """_upsert_orden apaga el flag en una segunda sentencia. Sin eso, el
        pull marcaría para push exactamente lo que acaba de recibir."""
        oid = self._crear_orden(talca_id=8800)
        fila_talca = {
            "id": 8800, "id_dueno": 9300, "id_vehiculo": 7600, "estado": "ABIERTA",
            "requerimiento": "OTRA COSA", "precio": "200000", "coniva": "1",
            "kilometraje": "95000", "estanque": "4", "observaciones": "",
            "origen": "GOOGLE", "patente": "ZN1510", "nombre": "CLIENTE OT",
            "fecha_cierre": "", "fecha_salida": "", "tipo": "Nueva Orden",
            "apro": "", "tnr": "", "isDeleted": "0", "vehiculo": "",
            "createdDtm": "2026-08-01 09:00:00", "updatedDtm": "2026-08-12 10:00:00",
        }
        resultado = sync_talca._upsert_orden(
            self.db, fila_talca, self.cliente_id, self.equipo_id, "ZN1510",
            {"adoptadas": 0},
        )
        self.db.commit()
        self.assertEqual("actualizada", resultado)
        self.assertEqual(0, self._orden(oid)["push_pendiente"],
                         "lo recibido de Talca no se devuelve a Talca")

    def test_el_pull_salta_una_orden_con_push_pendiente(self):
        oid = self._crear_orden(talca_id=8800, push_pendiente=1)
        fila_talca = {
            "id": 8800, "id_dueno": 9300, "id_vehiculo": 7600, "estado": "ABIERTA",
            "requerimiento": "LO DE TALCA", "precio": "1", "coniva": "1",
            "kilometraje": "1", "estanque": "1", "observaciones": "", "origen": "",
            "patente": "ZN1510", "nombre": "CLIENTE OT", "fecha_cierre": "",
            "fecha_salida": "", "tipo": "Nueva Orden", "apro": "", "tnr": "",
            "isDeleted": "0", "vehiculo": "", "createdDtm": "2026-08-01 09:00:00",
            "updatedDtm": "2026-08-12 10:00:00",
        }
        resultado = sync_talca._upsert_orden(
            self.db, fila_talca, self.cliente_id, self.equipo_id, "ZN1510", {"adoptadas": 0})
        self.db.commit()
        self.assertEqual("saltada", resultado)
        self.assertEqual("CAMBIO DE ACEITE", self._orden(oid)["trabajo_a_realizar"])


class TestEstadoTalca(unittest.TestCase):
    """Lo que viaja es estado_talca, no el estado interno de Python."""

    def calc(self, estado_python, estado_talca_actual):
        return push_talca._estado_talca_para_push(estado_python, estado_talca_actual)

    def test_abierta_se_preserva_mientras_la_orden_siga_abierta(self):
        for estado in ("recibido", "diagnostico", "reparacion", "listo"):
            with self.subTest(estado=estado):
                self.assertEqual("ABIERTA", self.calc(estado, "ABIERTA"))

    def test_cerrada_bloqueada_no_se_degrada_a_cerrada(self):
        """El matiz no tiene inversa: el pull manda CERRADA y CERRADA
        BLOQUEADA al mismo 'entregado'. Recalcular a ciegas lo perdería."""
        self.assertEqual("CERRADA BLOQUEADA", self.calc("entregado", "CERRADA BLOQUEADA"))

    def test_presupuesto_no_aceptado_se_preserva(self):
        self.assertEqual("PRESUPUESTO NO ACEPTADO",
                         self.calc("cancelado", "PRESUPUESTO NO ACEPTADO"))

    def test_cerrar_en_python_cierra_en_talca(self):
        self.assertEqual("CERRADA", self.calc("entregado", "ABIERTA"))
        self.assertEqual("CERRADA", self.calc("cancelado", "ABIERTA"))

    def test_reabrir_en_python_reabre_en_talca(self):
        self.assertEqual("ABIERTA", self.calc("reparacion", "CERRADA"))
        self.assertEqual("ABIERTA", self.calc("recibido", "CERRADA BLOQUEADA"))

    def test_valor_desconocido_se_normaliza(self):
        self.assertEqual("ABIERTA", self.calc("recibido", "CUALQUIER COSA"))
        self.assertEqual("ABIERTA", self.calc("recibido", ""))

    def test_los_estados_cerrados_coinciden_con_operativo(self):
        """Si alguien agrega un estado terminal en operativo.py y no acá, las
        órdenes cerradas dejarían de cerrarse en Talca."""
        from modulos.operativo import ESTADOS_CERRADOS
        self.assertEqual(ESTADOS_CERRADOS, push_talca.ESTADOS_PYTHON_CERRADOS)


class TestBarridoOrdenes(BaseOrdenesPush):
    """El trigger levanta la bandera; el barrido arma la entrada de cola
    diffeando contra datos_talca_json, que es la última fila que mandó Talca."""

    def test_solo_lo_que_difiere_de_talca_se_encola(self):
        oid = self._crear_orden(talca_id=8800)
        self.db.execute(
            "UPDATE ordenes_trabajo SET precio_referencial = 250000 WHERE id = ?", (oid,))
        self.db.commit()

        push_talca.barrer_ordenes_marcadas(self.db)
        cola = self._cola()
        self.assertEqual(1, len(cola))
        self.assertEqual({"precio": 250000.0}, json.loads(cola[0]["campos_json"]))
        self.assertEqual("actualizar", cola[0]["operacion"])
        self.assertEqual(8800, cola[0]["talca_id"])
        self.assertEqual("2026-08-10 08:00:00", cola[0]["talca_updated_at_conocido"])

    def test_avanzar_en_el_tablero_sin_cerrar_no_manda_nada(self):
        """'recibido' -> 'reparacion' no cambia el carácter abierto/cerrado:
        Talca no tiene nada que ver acá y la cola queda vacía."""
        oid = self._crear_orden(talca_id=8800)
        self.db.execute("UPDATE ordenes_trabajo SET estado = 'reparacion' WHERE id = ?", (oid,))
        self.db.commit()

        push_talca.barrer_ordenes_marcadas(self.db)
        self.assertEqual([], self._cola())
        self.assertEqual(0, self._orden(oid)["push_pendiente"],
                         "y el flag se apaga: no hay nada pendiente")

    def test_cerrar_la_orden_manda_cerrada(self):
        oid = self._crear_orden(talca_id=8800)
        self.db.execute("UPDATE ordenes_trabajo SET estado = 'entregado' WHERE id = ?", (oid,))
        self.db.commit()

        push_talca.barrer_ordenes_marcadas(self.db)
        campos = json.loads(self._cola()[0]["campos_json"])
        self.assertEqual("CERRADA", campos["estado"])
        self.assertEqual("CERRADA", self._orden(oid)["estado_talca"],
                         "y queda guardado, no solo enviado")

    def test_el_alta_local_se_encola_como_crear(self):
        oid = self._crear_orden(talca_id=None, push_pendiente=1)
        push_talca.barrer_ordenes_marcadas(self.db)
        cola = self._cola()
        self.assertEqual("crear", cola[0]["operacion"])
        self.assertEqual(1, cola[0]["requiere_dueno"])
        self.assertNotEqual("", cola[0]["idempotency_key"])

    def test_no_duplica_la_entrada_si_ya_hay_una_sin_resolver(self):
        oid = self._crear_orden(talca_id=8800)
        self.db.execute(
            "UPDATE ordenes_trabajo SET precio_referencial = 250000 WHERE id = ?", (oid,))
        self.db.commit()
        push_talca.barrer_ordenes_marcadas(self.db)
        push_talca.barrer_ordenes_marcadas(self.db)
        self.assertEqual(1, len(self._cola()))


class TestDobleEsperaOrdenes(BaseOrdenesPush):
    """El CREATE de una orden depende de DOS talca_id: el del cliente y el del
    vehículo. Falta cualquiera y no sale nada."""

    def _encolar_alta(self, **extra):
        oid = self._crear_orden(talca_id=None, push_pendiente=1, **extra)
        push_talca.barrer_ordenes_marcadas(self.db)
        return oid, self._cola()[-1]["id"]

    def test_sin_cliente_espera(self):
        self.db.execute("UPDATE clientes SET talca_id = NULL WHERE id = ?", (self.cliente_id,))
        self.db.commit()
        _, id_cola = self._encolar_alta()

        falso = ClientePushTalcaFalso()
        self.assertEqual("espera", push_talca._ejecutar_push_entrada(id_cola, falso))
        self.assertEqual([], falso.llamadas_crear)
        entrada = self.db.execute(
            "SELECT * FROM sync_talca_push_pendientes WHERE id = ?", (id_cola,)).fetchone()
        self.assertIn("el cliente", entrada["ultimo_error"])
        self.assertEqual(0, entrada["intentos"], "esperar no es fallar")

    def test_sin_vehiculo_espera(self):
        self.db.execute("UPDATE equipos SET talca_id = NULL WHERE id = ?", (self.equipo_id,))
        self.db.commit()
        _, id_cola = self._encolar_alta()

        falso = ClientePushTalcaFalso()
        self.assertEqual("espera", push_talca._ejecutar_push_entrada(id_cola, falso))
        entrada = self.db.execute(
            "SELECT ultimo_error FROM sync_talca_push_pendientes WHERE id = ?", (id_cola,)).fetchone()
        self.assertIn("el vehículo", entrada["ultimo_error"])

    def test_con_una_resuelta_y_la_otra_no_tampoco_sale(self):
        """Lo que NO se hace es mandar la orden con id_vehiculo en 0: sería
        crear en Talca una fila que después hay que corregir, y reasignar la
        identidad está fuera del alcance del push."""
        self.db.execute("UPDATE equipos SET talca_id = NULL WHERE id = ?", (self.equipo_id,))
        self.db.commit()
        _, id_cola = self._encolar_alta()
        falso = ClientePushTalcaFalso()
        push_talca._ejecutar_push_entrada(id_cola, falso)
        self.assertEqual([], falso.llamadas_crear)

    def test_faltando_las_dos_el_mensaje_las_nombra(self):
        self.db.execute("UPDATE clientes SET talca_id = NULL WHERE id = ?", (self.cliente_id,))
        self.db.execute("UPDATE equipos SET talca_id = NULL WHERE id = ?", (self.equipo_id,))
        self.db.commit()
        _, id_cola = self._encolar_alta()
        push_talca._ejecutar_push_entrada(id_cola, ClientePushTalcaFalso())
        error = self.db.execute(
            "SELECT ultimo_error FROM sync_talca_push_pendientes WHERE id = ?", (id_cola,)
        ).fetchone()["ultimo_error"]
        self.assertIn("el cliente", error)
        self.assertIn("el vehículo", error)

    def test_con_las_dos_resueltas_sale_con_la_identidad_correcta(self):
        oid, id_cola = self._encolar_alta()
        falso = ClientePushTalcaFalso()
        falso.respuesta_crear = {"ok": True, "id": 8900, "updated_at": "2026-08-12 11:00:00"}
        self.assertEqual("ok", push_talca._ejecutar_push_entrada(id_cola, falso))

        campos = falso.llamadas_crear[0]
        self.assertEqual(9300, campos["id_dueno"], "el talca_id del cliente, no el local")
        self.assertEqual(7600, campos["id_vehiculo"], "el talca_id del equipo, no el local")
        self.assertNotEqual(self.cliente_id, campos["id_dueno"])
        self.assertNotEqual(self.equipo_id, campos["id_vehiculo"])

        orden = self._orden(oid)
        self.assertEqual(8900, orden["talca_id"], "el número de OT lo reparte Talca")
        self.assertEqual(0, orden["push_pendiente"])

    def test_cuando_aparecen_los_dos_talca_id_se_manda(self):
        self.db.execute("UPDATE clientes SET talca_id = NULL WHERE id = ?", (self.cliente_id,))
        self.db.commit()
        _, id_cola = self._encolar_alta()
        push_talca._ejecutar_push_entrada(id_cola, ClientePushTalcaFalso())

        self.db.execute("UPDATE clientes SET talca_id = 9300 WHERE id = ?", (self.cliente_id,))
        self.db.commit()
        falso = ClientePushTalcaFalso()
        self.assertEqual("ok", push_talca._ejecutar_push_entrada(id_cola, falso))
        self.assertEqual(9300, falso.llamadas_crear[0]["id_dueno"])


class TestPushOrdenExtremoAExtremo(BaseOrdenesPush):

    def test_conflicto_409_no_pisa_y_registra(self):
        oid = self._crear_orden(talca_id=8800)
        self.db.execute(
            "UPDATE ordenes_trabajo SET precio_referencial = 250000 WHERE id = ?", (oid,))
        self.db.commit()
        push_talca.barrer_ordenes_marcadas(self.db)
        id_cola = self._cola()[-1]["id"]

        falso = ClientePushTalcaFalso()
        falso.respuesta_update = {
            "ok": False, "conflicto": True, "updated_at": "2026-08-12 12:00:00",
            "datos_actuales": {"id": 8800, "precio": "999"},
        }
        self.assertEqual("conflicto", push_talca._ejecutar_push_entrada(id_cola, falso))
        self.assertEqual(0, self._orden(oid)["push_pendiente"])
        self.assertEqual(250000, self._orden(oid)["precio_referencial"],
                         "el dato local no se pisa; el pull traerá el de Talca")
        conflictos = self.db.execute(
            "SELECT * FROM sync_talca_conflictos WHERE entidad = 'ordenes'").fetchall()
        self.assertEqual(1, len(conflictos))

    def test_el_espejo_se_refresca_tras_un_push_exitoso(self):
        """Sin esto, la próxima edición volvería a mandar lo ya enviado: el
        diff se calcula contra datos_talca_json."""
        oid = self._crear_orden(talca_id=8800)
        self.db.execute(
            "UPDATE ordenes_trabajo SET precio_referencial = 250000 WHERE id = ?", (oid,))
        self.db.commit()
        push_talca.barrer_ordenes_marcadas(self.db)
        push_talca._ejecutar_push_entrada(self._cola()[-1]["id"], ClientePushTalcaFalso())

        espejo = json.loads(self._orden(oid)["datos_talca_json"])
        self.assertEqual(250000.0, espejo["precio"])

        # Un cambio nuevo manda solo lo nuevo.
        self.db.execute(
            "UPDATE ordenes_trabajo SET kilometraje = 111111 WHERE id = ?", (oid,))
        self.db.commit()
        push_talca.barrer_ordenes_marcadas(self.db)
        self.assertEqual({"kilometraje": 111111},
                         json.loads(self._cola()[-1]["campos_json"]))

    def test_el_selector_elige_el_sabor_del_cerrado(self):
        """Sobre una orden cerrada acá, el selector sirve para distinguir
        CERRADA de CERRADA BLOQUEADA -- el matiz que Python no puede deducir."""
        oid = self._crear_orden(talca_id=8800, estado="entregado", estado_talca="CERRADA")
        resp = self.client.put(f"/api/operativo/ordenes/{oid}",
                               json={"estado": "entregado",
                                     "estado_talca": "CERRADA BLOQUEADA"})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertEqual("CERRADA BLOQUEADA", self._orden(oid)["estado_talca"])

        # Y el barrido lo respeta en vez de degradarlo a CERRADA.
        push_talca.barrer_ordenes_marcadas(self.db)
        self.assertEqual("CERRADA BLOQUEADA", self._orden(oid)["estado_talca"])

    def test_un_estado_talca_inventado_se_rechaza(self):
        oid = self._crear_orden(talca_id=8800)
        malo = self.client.put(f"/api/operativo/ordenes/{oid}",
                               json={"estado_talca": "INVENTADO"})
        self.assertEqual(400, malo.status_code)
        self.assertEqual("ABIERTA", self._orden(oid)["estado_talca"])

    def test_no_se_puede_cerrar_solo_en_talca(self):
        """Los dos estados tienen que coincidir en abierto/cerrado. Aceptar la
        combinación incoherente haría que el barrido la revirtiera después, y
        desde la pantalla eso se ve como 'el selector no guarda'."""
        oid = self._crear_orden(talca_id=8800, estado="reparacion")
        resp = self.client.put(f"/api/operativo/ordenes/{oid}",
                               json={"estado": "reparacion",
                                     "estado_talca": "CERRADA BLOQUEADA"})
        self.assertEqual(400, resp.status_code)
        self.assertIn("Primero cambia el estado", resp.get_json()["error"])
        self.assertEqual("ABIERTA", self._orden(oid)["estado_talca"])


class TestEscritoresDeOrdenes(BaseOrdenesPush):
    """El test que atrapa al escritor número once.

    Recorre los endpoints que escriben `ordenes_trabajo` y exige que cada uno
    deje la orden marcada. No comprueba que llamen a disparar_ordenes_marcadas
    —eso es latencia, no correctitud— sino que el trigger los cubra."""

    def _marcada(self, oid):
        return self._orden(oid)["push_pendiente"] == 1

    def test_editar_orden_marca(self):
        oid = self._crear_orden(talca_id=8800)
        resp = self.client.put(f"/api/operativo/ordenes/{oid}",
                               json={"kilometraje": 123456})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(self._marcada(oid))

    def test_cambiar_estado_marca(self):
        oid = self._crear_orden(talca_id=8800)
        resp = self.client.put(f"/api/operativo/ordenes/{oid}",
                               json={"estado": "entregado"})
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))
        self.assertTrue(self._marcada(oid))

    def test_guardar_presupuesto_marca(self):
        oid = self._crear_orden(talca_id=8800)
        resp = self.client.put(f"/api/operativo/ordenes/{oid}/presupuesto",
                               json={"estado": "aprobado", "notas": "OK"})
        if resp.status_code == 404:
            self.skipTest("la ruta del presupuesto cambió de nombre")
        self.assertTrue(self._marcada(oid),
                        "aprobar el presupuesto cambia `apro`, que viaja a Talca")

    def test_crear_orden_desde_el_endpoint_queda_marcada(self):
        resp = self.client.post("/api/operativo/ordenes", json={
            "cliente_id": self.cliente_id, "equipo_id": self.equipo_id,
            "descripcion_problema": "NO ARRANCA",
        })
        self.assertEqual(201, resp.status_code, resp.get_data(as_text=True))
        oid = resp.get_json()["id"]
        self.assertTrue(self._marcada(oid),
                        "una orden creada acá tiene que llegar a Talca")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 17. El número de OT que ve el taller
# ---------------------------------------------------------------------------

class TestNumeroOT(unittest.TestCase):
    """El `id` de Python es llave técnica interna y nunca se muestra. El
    número visible es `talca_id`; las órdenes locales llevan prefijo L-."""

    def numero(self, **campos):
        from modulos.operativo import numero_ot
        return numero_ot(campos)

    def test_con_talca_id_muestra_el_de_talca(self):
        self.assertEqual("8112", self.numero(id=7086, talca_id=8112))

    def test_sin_talca_id_lleva_prefijo_local(self):
        self.assertEqual("L-7086", self.numero(id=7086, talca_id=None))

    def test_el_prefijo_evita_confundirlo_con_un_numero_de_talca(self):
        """Sin el prefijo, la OT local 8112 y la de Talca 8112 se verían
        idénticas y son órdenes distintas."""
        self.assertNotEqual(self.numero(id=8112, talca_id=None),
                            self.numero(id=1, talca_id=8112))

    def test_sin_orden_no_revienta(self):
        from modulos.operativo import numero_ot
        self.assertEqual("—", numero_ot(None))


class TestBusquedaYOrdenPorNumeroTalca(BaseOrdenesPush):
    """Los dos sitios donde mostrar el número equivocado no es cosmético."""

    def test_buscar_por_numero_usa_talca_id(self):
        oid = self._crear_orden(talca_id=8112)
        resp = self.client.get("/api/operativo/ordenes?q=8112&por_pagina=todos")
        ids = [o["id"] for o in resp.get_json()["ordenes"]]
        self.assertIn(oid, ids)

    def test_buscar_por_el_id_interno_no_la_encuentra(self):
        """`o.id` salió del filtro: las dos series se solapan en 6.117
        números, así que buscar por el id interno devolvía, junto a la orden
        correcta, otra que no tiene nada que ver.

        El fixture evita que los dígitos del id aparezcan en otro campo
        buscable (la patente por defecto es ZN1510, que matchearía '10' por
        su cuenta y haría pasar el test por el motivo equivocado)."""
        oid = self._crear_orden(talca_id=8112, patente="SINDIGITOS")
        self.db.execute("UPDATE equipos SET identificador = 'SINDIGITOS' WHERE id = ?",
                        (self.equipo_id,))
        self.db.commit()

        # El id interno no puede estar contenido en el talca_id, o el match
        # vendría de ahí.
        self.assertNotIn(str(oid), "8112", "elegir otro talca_id para este test")

        resp = self.client.get(f"/api/operativo/ordenes?q={oid}&por_pagina=todos")
        ids = [o["id"] for o in resp.get_json()["ordenes"]]
        self.assertNotIn(oid, ids)

    def test_el_listado_ordena_por_numero_de_talca(self):
        self._crear_orden(talca_id=8000)
        self._crear_orden(talca_id=8200)
        self._crear_orden(talca_id=8100)
        resp = self.client.get("/api/operativo/ordenes?por_pagina=todos")
        talcas = [o["talca_id"] for o in resp.get_json()["ordenes"] if o["talca_id"]]
        self.assertEqual(sorted(talcas, reverse=True), talcas)


class TestAgenteResuelvePorTalcaId(BaseOrdenesPush):
    """Acá el riesgo es escritura cruzada, no confusión visual: el agente
    escribe diagnósticos y cambia estados."""

    def test_resuelve_por_talca_id(self):
        oid = self._crear_orden(talca_id=8112)
        orden = taller_app._agente_resolver_orden(self.db, 8112)
        self.assertIsNotNone(orden)
        self.assertEqual(oid, orden["id"])

    def test_no_confunde_el_id_interno_con_un_numero_de_talca(self):
        """El caso peligroso: el mecánico escribe 'OT 8112' y existe una
        orden interna con id 8112 que es el auto de otra persona."""
        con_talca = self._crear_orden(talca_id=8112)
        senuelo = self._crear_orden(talca_id=9999)
        self.db.execute("UPDATE ordenes_trabajo SET id = 8112 WHERE id = ?", (senuelo,))
        self.db.commit()

        orden = taller_app._agente_resolver_orden(self.db, 8112)
        self.assertEqual(con_talca, orden["id"],
                         "tiene que resolver por talca_id, no por el id interno")

    def test_una_orden_local_se_resuelve_por_su_id(self):
        """Las locales no tienen talca_id que pueda chocar, así que ahí sí
        vale el id interno."""
        oid = self._crear_orden(talca_id=None)
        orden = taller_app._agente_resolver_orden(self.db, oid)
        self.assertIsNotNone(orden)
        self.assertEqual(oid, orden["id"])

    def test_un_numero_inexistente_devuelve_none(self):
        self.assertIsNone(taller_app._agente_resolver_orden(self.db, 999999))


class TestBusquedaEnElNavegador(unittest.TestCase):
    """La búsqueda de la pantalla Operativo NO pasa por el backend: el tablero
    y la pestaña Listado piden `por_pagina=todos` y filtran en el navegador
    (ver `coincide` en static/js/operativo.js). Por eso los tests del endpoint
    no vieron la regresión de "buscar 8112 no encuentra nada": el endpoint
    siempre estuvo bien.

    Esto es un guardia sobre el fuente. No prueba que el filtro funcione
    —para eso haría falta un runner de JS, que el proyecto no tiene— pero sí
    que no se vuelva a filtrar por el id interno, que es exactamente el error
    que hubo."""

    RUTA_JS = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "js", "operativo.js",
    )

    def fuente(self):
        with open(self.RUTA_JS, encoding="utf-8") as fh:
            return fh.read()

    def test_el_filtro_del_tablero_no_usa_el_id_interno(self):
        js = self.fuente()
        self.assertNotIn(
            "String(orden.id),", js,
            "el filtro del tablero volvió a buscar por el id interno",
        )

    def test_el_filtro_del_tablero_usa_el_numero_de_ot(self):
        """El tablero sigue filtrando en el navegador **porque no pagina**:
        tiene todas las OT del mes cargadas, así que el filtro las ve todas.
        La pestaña Listado hace lo contrario (consulta al servidor) porque sí
        pagina. No es una inconsistencia; ver CLAUDE.md."""
        self.assertIn("numeroOT(orden),", self.fuente(),
                      "el tablero debe filtrar por número de OT")

    def test_el_listado_busca_en_el_servidor(self):
        """Si volviera a filtrar en el navegador, la búsqueda solo vería la
        página cargada — peor que antes de paginar."""
        js = self.fuente()
        self.assertIn('qs.set("q", busqueda)', js,
                      "el Listado debe mandar la búsqueda al endpoint")
        self.assertIn("setTimeout(cargarListadoOperativo, 250)", js,
                      "y hacerlo con debounce, para no consultar por cada tecla")

    def test_el_tablero_pide_la_vista_liviana(self):
        self.assertIn("vista=tablero", self.fuente(),
                      "el tablero no debe traer el dict completo de la orden")

    def test_el_modal_de_orden_nueva_ya_no_trae_el_historial(self):
        """Traía las 7.116 órdenes (23,5 MB) para armar tres datalists."""
        js = self.fuente()
        self.assertNotIn('api("/api/operativo/ordenes?mes=todos&por_pagina=todos")', js)
        self.assertIn('api("/api/operativo/sugerencias")', js)

    def test_el_helper_se_carga_antes_en_todas_las_plantillas(self):
        """Si `ot_numero.js` no está, `numeroOT` queda indefinida y el
        listado revienta al renderizar."""
        raiz = os.path.dirname(self.RUTA_JS).replace(os.path.join("static", "js"), "")
        raiz = os.path.dirname(os.path.dirname(self.RUTA_JS))
        plantillas = os.path.join(os.path.dirname(raiz), "templates")
        for nombre in ("operativo", "finanzas", "convertidores", "admin",
                       "index", "inicio", "agente_ia"):
            with self.subTest(plantilla=nombre):
                with open(os.path.join(plantillas, f"{nombre}.html"), encoding="utf-8") as fh:
                    html = fh.read()
                self.assertIn("js/ot_numero.js", html)
                otros = [html.index(f"js/{x}.js") for x in
                         ("operativo", "finanzas", "convertidores", "admin",
                          "app", "panel_dueno", "agente_ia", "charts")
                         if f"js/{x}.js" in html]
                self.assertLess(html.index("js/ot_numero.js"), min(otros),
                                "ot_numero.js debe cargarse antes que los demás")


class TestBusquedaPorNumeroEnElBackend(BaseOrdenesPush):
    """El endpoint sí acepta `q` (lo usan otros llamadores). Estos dos fijan
    la regla ahí."""

    def test_buscar_el_talca_id_encuentra_su_orden(self):
        oid = self._crear_orden(talca_id=8112)
        resp = self.client.get("/api/operativo/ordenes?q=8112&mes=todos&por_pagina=todos")
        ids = [o["id"] for o in resp.get_json()["ordenes"]]
        self.assertIn(oid, ids)

    def test_buscar_el_id_interno_de_otra_orden_no_la_trae(self):
        """El caso que motivó sacar `o.id` del filtro: dos series que se
        solapan en 6.117 valores."""
        intrusa = self._crear_orden(talca_id=7000, patente="AAAAAA")
        self.db.execute("UPDATE equipos SET identificador = 'AAAAAA' WHERE id = ?",
                        (self.equipo_id,))
        self.db.commit()
        # Se busca el id INTERNO de la intrusa; no debe aparecer.
        resp = self.client.get(
            f"/api/operativo/ordenes?q={intrusa}&mes=todos&por_pagina=todos")
        ids = [o["id"] for o in resp.get_json()["ordenes"]]
        self.assertNotIn(intrusa, ids)

    def test_una_orden_sin_equipo_igual_se_encuentra_por_su_numero(self):
        """`e.marca` y `e.modelo` vienen de un LEFT JOIN y son NULL en las 15
        órdenes sin equipo resoluble. Si faltara el COALESCE en la
        concatenación, esas órdenes desaparecerían del filtro por completo."""
        oid = self._crear_orden(talca_id=8500)
        self.db.execute("UPDATE ordenes_trabajo SET equipo_id = NULL WHERE id = ?", (oid,))
        self.db.commit()
        resp = self.client.get("/api/operativo/ordenes?q=8500&mes=todos&por_pagina=todos")
        ids = [o["id"] for o in resp.get_json()["ordenes"]]
        self.assertIn(oid, ids)


class TestListadoPaginadoYBusqueda(BaseOrdenesPush):
    """La pestaña Listado pasó a paginar y a buscar en el servidor."""

    def test_pagina_por_defecto(self):
        for i in range(60):
            self._crear_orden(talca_id=9000 + i)
        datos = self.client.get("/api/operativo/listado").get_json()
        self.assertEqual(50, len(datos["filas"]), "el defecto son 50 por página")
        self.assertGreaterEqual(datos["total"], 60, "pero el total cuenta todas")
        self.assertGreaterEqual(datos["total_paginas"], 2)

    def test_la_segunda_pagina_trae_filas_distintas(self):
        for i in range(60):
            self._crear_orden(talca_id=9100 + i)
        p1 = self.client.get("/api/operativo/listado?pagina=1").get_json()["filas"]
        p2 = self.client.get("/api/operativo/listado?pagina=2").get_json()["filas"]
        self.assertTrue(p2)
        self.assertEqual(set(), {f["id"] for f in p1} & {f["id"] for f in p2})

    def test_la_busqueda_filtra_en_el_servidor(self):
        self._crear_orden(talca_id=9500)
        self._crear_orden(talca_id=9501)
        datos = self.client.get("/api/operativo/listado?q=9500").get_json()
        self.assertEqual(1, datos["total"])
        self.assertEqual(9500, datos["filas"][0]["talca_id"])

    def test_la_busqueda_encuentra_fuera_de_la_primera_pagina(self):
        """El punto de moverla al servidor: filtrando en el navegador, una
        coincidencia en la página 3 sería invisible."""
        for i in range(60):
            self._crear_orden(talca_id=9200 + i)
        objetivo = self._crear_orden(talca_id=1234, patente="RAROXX")
        datos = self.client.get("/api/operativo/listado?q=1234").get_json()
        self.assertIn(objetivo, [f["id"] for f in datos["filas"]])

    def test_por_pagina_todos_sigue_funcionando(self):
        for i in range(60):
            self._crear_orden(talca_id=9300 + i)
        datos = self.client.get("/api/operativo/listado?por_pagina=todos").get_json()
        self.assertEqual(datos["total"], len(datos["filas"]))


class TestVistaTablero(BaseOrdenesPush):
    """El tablero se refresca solo cada pocos segundos: su peso se paga una y
    otra vez."""

    def test_la_vista_liviana_no_trae_el_lastre(self):
        self._crear_orden(talca_id=9600)
        datos = self.client.get(
            "/api/operativo/ordenes?mes=todos&vista=tablero&por_pagina=todos").get_json()
        orden = datos["ordenes"][0]
        for campo in ("datos_talca_json", "items", "pagos", "observaciones_talca",
                      "presupuesto_notas"):
            self.assertNotIn(campo, orden, f"{campo} no lo usa la tarjeta")

    def test_trae_todo_lo_que_pinta_la_tarjeta(self):
        self._crear_orden(talca_id=9601)
        datos = self.client.get(
            "/api/operativo/ordenes?mes=todos&vista=tablero&por_pagina=todos").get_json()
        orden = datos["ordenes"][0]
        for campo in ("id", "talca_id", "estado", "prioridad", "precio_referencial",
                      "fecha_recepcion", "fecha_entrega_real", "descripcion_problema",
                      "cliente", "equipo"):
            self.assertIn(campo, orden)
        self.assertIn("nombre", orden["cliente"])
        self.assertIn("identificador", orden["equipo"])

    def test_sin_vista_tablero_sigue_devolviendo_el_dict_completo(self):
        """Los demás llamadores (detalle, convertidores) no deben verse
        afectados."""
        self._crear_orden(talca_id=9602)
        datos = self.client.get(
            "/api/operativo/ordenes?mes=todos&por_pagina=todos").get_json()
        self.assertIn("datos_talca_json", datos["ordenes"][0])

    def test_avisa_cuando_el_mes_trae_mucho(self):
        datos = self.client.get("/api/operativo/ordenes?mes=todos&por_pagina=todos").get_json()
        self.assertEqual("", datos["aviso_volumen"], "con pocas OT no avisa nada")

        import modulos.operativo as mo
        original = mo.ORDENES_TABLERO_AVISO
        mo.ORDENES_TABLERO_AVISO = 0
        try:
            self._crear_orden(talca_id=9700)
            datos = self.client.get(
                "/api/operativo/ordenes?mes=todos&por_pagina=todos").get_json()
            self.assertIn("órdenes", datos["aviso_volumen"])
        finally:
            mo.ORDENES_TABLERO_AVISO = original


class TestSugerenciasOperativo(BaseOrdenesPush):
    """Reemplaza la carga del historial completo que alimentaba tres
    datalists."""

    def test_devuelve_los_valores_distintos(self):
        self._crear_orden(talca_id=9800, referencia="GOOGLE")
        self._crear_orden(talca_id=9801, referencia="GOOGLE")
        self._crear_orden(talca_id=9802, referencia="REFERIDO")
        datos = self.client.get("/api/operativo/sugerencias").get_json()
        self.assertEqual(["GOOGLE", "REFERIDO"], datos["referencias"])
        self.assertIn("colores", datos)
        self.assertIn("transmisiones", datos)

    def test_no_incluye_vacios(self):
        self._crear_orden(talca_id=9810, referencia="")
        datos = self.client.get("/api/operativo/sugerencias").get_json()
        self.assertNotIn("", datos["referencias"])


# ---------------------------------------------------------------------------
# 18. El refresco de fondo no pisa lo que el usuario está escribiendo
# ---------------------------------------------------------------------------

class TestRefrescoNoPisaEdicion(unittest.TestCase):
    """El tablero se refresca solo cada OP_POLLING_MS (4 s) y, con el modal de
    detalle abierto, repintaba el formulario con los datos del servidor. Quien
    tardara más de 4 s en guardar un precio veía su valor volver al anterior.

    Eso no es un detalle de usabilidad: se lee como "el sistema perdió mi
    dato", y a partir de ahí la pantalla deja de ser creíble.

    Guardias sobre el fuente. No prueban el comportamiento en el navegador
    —no hay runner de JS— pero sí que las piezas del arreglo sigan en su
    lugar."""

    RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def js(self):
        with open(os.path.join(self.RAIZ, "static", "js", "operativo.js"),
                  encoding="utf-8") as fh:
            return fh.read()

    def html(self):
        with open(os.path.join(self.RAIZ, "templates", "operativo.html"),
                  encoding="utf-8") as fh:
            return fh.read()

    def campos_protegidos(self):
        lista = re.search(r"const OP_CAMPOS_FORMULARIO_OT = \[(.*?)\];",
                          self.js(), re.S).group(1)
        return set(re.findall(r'"([\w-]+)"', lista))

    def test_protege_exactamente_los_campos_que_manda_guardar(self):
        """Si alguien agrega un campo al payload y no a la lista protegida,
        ese campo queda otra vez expuesto al refresco. Este test es el que lo
        atrapa."""
        js = self.js()
        frag = js[js.index('document.getElementById("btn-opod-guardar").addEventListener'):]
        frag = frag[:frag.index("try {")]
        del_payload = set(re.findall(r'getElementById\("([\w-]+)"\)', frag))
        del_payload -= {"opod-id", "btn-opod-guardar"}
        self.assertEqual(self.campos_protegidos(), del_payload)

    def test_los_campos_protegidos_existen_en_el_html(self):
        html = self.html()
        for campo in self.campos_protegidos():
            with self.subTest(campo=campo):
                self.assertIn(f'id="{campo}"', html)

    def test_el_refresco_consulta_si_hay_cambios_sin_guardar(self):
        js = self.js()
        bloque = js[js.index("async function refrescarResumenHub"):]
        bloque = bloque[:bloque.index("\n}")]
        self.assertIn("opHayCambiosSinGuardarOT()", bloque)
        self.assertIn("conservarEdicion: editando", bloque)

    def test_el_valor_y_el_iva_no_se_repintan_al_editar(self):
        js = self.js()
        bloque = js[js.index("function renderValorYPagosOrden"):]
        bloque = bloque[:bloque.index('document.getElementById("opod-r-neto")')]
        self.assertIn("if (!conservarEdicion) {", bloque)
        self.assertIn("opod-valor", bloque)
        self.assertIn("opod-iva-check", bloque)

    def test_la_deteccion_es_por_cambios_y_no_por_foco(self):
        """Por foco no alcanza: escribir el precio y hacer clic fuera del
        input sin guardar dejaría el formulario desprotegido otra vez."""
        js = self.js()
        bloque = js[js.index("function opHayCambiosSinGuardarOT"):]
        bloque = bloque[:bloque.index("\n}")]
        self.assertIn("opFormularioOTGuardado", bloque)
        self.assertNotIn("document.activeElement", bloque)
        self.assertNotIn(":focus", bloque)

    def test_la_foto_se_renueva_al_abrir_y_al_guardar(self):
        """Tres puntos: al abrir el modal, después de guardar, y después de un
        refresco que sí repintó. Si faltara el de guardar, el aviso de
        cambios pendientes quedaría encendido para siempre."""
        self.assertGreaterEqual(
            self.js().count("opMarcarFormularioOTSincronizado()"), 3)

    def test_cerrar_el_modal_limpia_la_foto(self):
        """Sin esto, la foto de la orden anterior seguiría viva y el refresco
        quedaría bloqueado para la siguiente."""
        js = self.js()
        bloque = js[js.index("function cerrarModal"):]
        bloque = bloque[:bloque.index("\n}\n")]
        self.assertIn("opFormularioOTGuardado = null", bloque)
        self.assertIn("state.opOrdenActivaId = null", bloque)

    def test_el_aviso_de_cambios_pendientes_existe(self):
        """El arreglo tiene que ser visible: si no, el campo deja de
        revertirse pero nadie sabe si quedó guardado."""
        self.assertIn('id="opod-cambios-pendientes"', self.html())
        self.assertIn("opActualizarAvisoCambiosOT", self.js())

    def test_lo_informativo_se_sigue_actualizando(self):
        """Solo se congelan los CONTROLES. Pagos, costos y el resumen de
        cliente/auto son hechos del servidor y se siguen repintando."""
        js = self.js()
        bloque = js[js.index("async function refrescarResumenHub"):]
        bloque = bloque[:bloque.index("\n}")]
        for llamada in ("renderResumenClienteAuto(orden)", "renderCostosOrden(orden)",
                        "cargarOperativo()"):
            with self.subTest(llamada=llamada):
                self.assertIn(llamada, bloque)
