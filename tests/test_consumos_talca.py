"""
Tests de la fase 3: los repuestos usados en una OT de Talca Python viajan a la
OT de Talca PHP (POST /api/consumos) y, al quitarlos, se desvinculan
(POST /api/consumos/anular). Ver "Consumos" en modulos/push_talca.py.

Cubre: que un repuesto encola un consumo con los datos correctos, que la mano
de obra no viaja, que la entrada espera mientras la OT no tiene número de
Talca, que anular espera a que el consumo haya llegado, que quitar el ítem o
borrar la OT encolan la anulación, y que un fallo de red queda para reintento.

Cómo correrlos:
    python tests/correr_todo.py consumos
"""
import json
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_consumos_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402
import core  # noqa: E402
from modulos import push_talca  # noqa: E402

TS = "2026-10-14T10:00:00"


class TalcaFalso:
    """Hace de Talca PHP: registra lo que recibe y responde como el Api.php real."""

    def __init__(self):
        self.consumos = []
        self.falla = False
        self._siguiente = 9000

    def consumo(self, accion, body):
        if self.falla:
            raise push_talca.PushTalcaError("corte de red simulado")
        self.consumos.append((accion, dict(body)))
        if accion == "crear":
            return {"ok": True, "ids": list(range(1, body["cantidad"] + 1))}
        return {"ok": True, "desvinculadas": 1}

    def crear(self, entidad, campos, idem_key=""):
        self._siguiente += 1
        return {"ok": True, "id": self._siguiente, "updated_at": "2026-10-14 10:00:00"}

    def actualizar(self, entidad, talca_id, campos, talca_updated_at_conocido):
        return {"ok": True, "updated_at": "2026-10-14 10:00:00"}


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        self.talca = TalcaFalso()
        db = core._conectar_db()
        db.execute("DELETE FROM sync_talca_push_pendientes")
        self.cli = db.execute("INSERT INTO clientes (nombre, creado_en) VALUES ('CLIENTE CONSUMOS', ?)", (TS,)).lastrowid
        self.prod = db.execute(
            "INSERT INTO productos (codigo_interno, nombre, descripcion, categoria, marca, modelo, ubicacion, "
            "stock_disponible, stock_reservado, stock_merma, stock_minimo, costo_compra, creado_en, actualizado_en) "
            "VALUES ('', 'U660 - DISCOS', '', 'U660', '', 'DISCOS FRICCION', '', 0, 0, 0, 0, 0, ?, ?)", (TS, TS)).lastrowid
        fila = db.execute("SELECT * FROM productos WHERE id = ?", (self.prod,)).fetchone()
        core._aplicar_entrada_stock(db, fila, 10, 12345.6, motivo="STOCK DE PRUEBA", usuario="test",
                                    categoria="Ajuste de inventario")
        db.commit()
        db.close()

    def _orden(self, talca_id):
        db = core._conectar_db()
        oid = db.execute(
            "INSERT INTO ordenes_trabajo (cliente_id, estado, fecha_recepcion, creado_en, actualizado_en, talca_id) "
            "VALUES (?, 'recibido', ?, ?, ?, ?)", (self.cli, TS, TS, TS, talca_id)).lastrowid
        db.execute("UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE id = ?", (oid,))
        db.commit()
        db.close()
        return oid

    def _agregar_repuesto(self, orden_id, cantidad=2):
        r = self.client.post(f"/api/operativo/ordenes/{orden_id}/items",
                             json={"tipo": "repuesto", "producto_id": self.prod, "cantidad": cantidad})
        self.assertEqual(201, r.status_code, r.get_data(as_text=True))
        return r.get_json()["id"]

    def _cola(self, operacion=None):
        db = core._conectar_db()
        sql = "SELECT * FROM sync_talca_push_pendientes WHERE entidad = 'consumos'"
        params = []
        if operacion:
            sql += " AND operacion = ?"
            params.append(operacion)
        filas = [dict(f) for f in db.execute(sql + " ORDER BY id", params).fetchall()]
        db.close()
        return filas

    def _ejecutar(self, entrada_id):
        return push_talca._ejecutar_push_entrada(entrada_id, cliente_push=self.talca)


class TestConsumoViaja(_Base):

    def test_un_repuesto_encola_un_consumo_con_sus_datos(self):
        oid = self._orden(talca_id=8500)
        item = self._agregar_repuesto(oid, cantidad=2)
        cola = self._cola("crear")
        self.assertEqual(1, len(cola))
        campos = json.loads(cola[0]["campos_json"])
        self.assertEqual({"orden_id": oid, "cantidad": 2, "modelo": "U660", "descripcion": "DISCOS FRICCION",
                          "costo_neto": 12346, "referencia": f"TALCA PYTHON ITEM {item}"}, campos)
        self.assertTrue(cola[0]["idempotency_key"])

        self.assertEqual("ok", self._ejecutar(cola[0]["id"]))
        accion, body = self.talca.consumos[0]
        self.assertEqual("crear", accion)
        self.assertEqual(8500, body["ot"])
        self.assertNotIn("orden_id", body)
        self.assertEqual(cola[0]["idempotency_key"], body["idempotency_key"])
        self.assertTrue(self._cola("crear")[0]["resuelto_en"])

    def test_la_mano_de_obra_no_viaja(self):
        oid = self._orden(talca_id=8501)
        r = self.client.post(f"/api/operativo/ordenes/{oid}/items",
                             json={"tipo": "mano_obra", "descripcion": "DESARME", "cantidad": 1, "costo_unitario": 0})
        self.assertEqual(201, r.status_code, r.get_data(as_text=True))
        self.assertEqual([], self._cola())

    def test_espera_mientras_la_ot_no_tiene_numero_de_talca(self):
        oid = self._orden(talca_id=None)
        self._agregar_repuesto(oid)
        entrada = self._cola("crear")[0]
        self.assertEqual("espera", self._ejecutar(entrada["id"]))
        self.assertEqual([], self.talca.consumos)
        self.assertIn("no tiene número de Talca", self._cola("crear")[0]["ultimo_error"])
        self.assertEqual(0, self._cola("crear")[0]["intentos"])  # esperar no es fallar

        db = core._conectar_db()
        db.execute("UPDATE ordenes_trabajo SET talca_id = 8502 WHERE id = ?", (oid,))
        db.commit()
        db.close()
        self.assertEqual("ok", self._ejecutar(entrada["id"]))
        self.assertEqual(8502, self.talca.consumos[0][1]["ot"])

    def test_un_fallo_de_red_queda_para_reintento(self):
        oid = self._orden(talca_id=8503)
        self._agregar_repuesto(oid)
        entrada = self._cola("crear")[0]
        self.talca.falla = True
        self.assertEqual("error", self._ejecutar(entrada["id"]))
        fila = self._cola("crear")[0]
        self.assertEqual(1, fila["intentos"])
        self.assertFalse(fila["resuelto_en"])
        self.assertIn("corte de red", fila["ultimo_error"])


class TestConsumoSeAnula(_Base):

    def test_quitar_el_item_encola_la_anulacion(self):
        oid = self._orden(talca_id=8510)
        item = self._agregar_repuesto(oid)
        self._ejecutar(self._cola("crear")[0]["id"])
        r = self.client.delete(f"/api/operativo/ordenes/{oid}/items/{item}")
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        anular = self._cola("anular")
        self.assertEqual(1, len(anular))
        self.assertEqual("ok", self._ejecutar(anular[0]["id"]))
        self.assertEqual(("anular", {"referencia": f"TALCA PYTHON ITEM {item}"}), self.talca.consumos[-1])

    def test_anular_espera_a_que_el_consumo_haya_llegado(self):
        oid = self._orden(talca_id=None)  # el crear queda en espera
        item = self._agregar_repuesto(oid)
        self.client.delete(f"/api/operativo/ordenes/{oid}/items/{item}")
        anular = self._cola("anular")[0]
        self.assertEqual("espera", self._ejecutar(anular["id"]))
        self.assertEqual([], self.talca.consumos)
        self.assertIn("todavía no llegó a Talca", self._cola("anular")[0]["ultimo_error"])

    def test_borrar_la_ot_anula_sus_repuestos(self):
        oid = self._orden(talca_id=8511)
        i1 = self._agregar_repuesto(oid, cantidad=1)
        i2 = self._agregar_repuesto(oid, cantidad=1)
        r = self.client.delete(f"/api/operativo/ordenes/{oid}")
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        self.assertEqual(sorted([i1, i2]), sorted(f["python_id"] for f in self._cola("anular")))

    def test_un_item_anterior_a_la_fase_3_no_encola_anulacion(self):
        oid = self._orden(talca_id=8512)
        item = self._agregar_repuesto(oid)
        db = core._conectar_db()
        db.execute("DELETE FROM sync_talca_push_pendientes")  # como si se hubiera cargado antes de esta función
        db.commit()
        db.close()
        self.client.delete(f"/api/operativo/ordenes/{oid}/items/{item}")
        self.assertEqual([], self._cola("anular"))

    def test_si_la_ot_se_borro_antes_de_llegar_el_consumo_se_descarta(self):
        oid = self._orden(talca_id=8513)
        self._agregar_repuesto(oid)
        entrada = self._cola("crear")[0]
        db = core._conectar_db()
        db.execute("DELETE FROM orden_items WHERE orden_id = ?", (oid,))
        db.execute("DELETE FROM ordenes_trabajo WHERE id = ?", (oid,))
        db.commit()
        db.close()
        self.assertEqual("ok", self._ejecutar(entrada["id"]))
        self.assertEqual([], self.talca.consumos)


if __name__ == "__main__":
    unittest.main()
