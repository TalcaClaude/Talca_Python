"""
Tests de la Carga inicial desde Talca PHP (modulos/carga_inicial.py):
POST /api/admin/carga-inicial con accion=ensayar|aplicar.

Cubre: que ensayar verifica los totales y no escribe nada; que aplicar exige
escribir CARGAR, respalda la base y escribe productos, stock por proveedor y
caja chica histórica rendida con el saldo intacto; que no se puede repetir;
que un plan con totales que no cuadran no escribe nada; que un archivo que no
es un plan se rechaza; y que solo un admin puede usarlo.

Cómo correrlos:
    python tests/correr_todo.py carga_inicial
"""
import io
import json
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_carga_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402
import core  # noqa: E402


def plan_de_prueba(**cambios_totales):
    plan = {
        "origen": {"extraido_en": "2026-10-14 10:00:00"},
        "proveedores": [{"nombre": "PROV A", "rut": "1-9", "telefono": "", "email": "", "direccion": ""},
                        {"nombre": "PROV B", "rut": "2-7", "telefono": "", "email": "", "direccion": ""}],
        "productos": [
            {"modelo": "U660", "descripcion": "DISCOS", "ubicacion": "354",
             "baldes": [{"proveedor": "PROV A", "cantidad": 3, "costo_neto": 1000.0},
                        {"proveedor": None, "cantidad": 1, "costo_neto": 2000.0}]},
            {"modelo": "", "descripcion": "ACEITE 1L", "ubicacion": "ESTANTE",
             "baldes": [{"proveedor": "PROV B", "cantidad": 2, "costo_neto": 500.0}]},
        ],
        "caja_chica": [
            {"fecha": "2025-01-10", "numero_documento": "B-1", "proveedor": "",
             "lineas": [{"clase": "GASTO", "descripcion": "BENCINA", "monto": 20000, "rubro_gav": "combustible"}]},
            {"fecha": "2025-02-11", "numero_documento": "F-9", "proveedor": "PROV A",
             "lineas": [{"clase": "REPUESTO", "descripcion": "ACEITE 1L", "modelo": "", "monto": 595, "cantidad": 1,
                         "costo_neto": 500, "proveedor_stock": "PROV A"},
                        {"clase": "REPUESTO", "descripcion": "RETEN 40X62", "modelo": "", "monto": 3000,
                         "cantidad": 1, "costo_neto": 3000, "proveedor_stock": None}]},
        ],
        "totales": {"productos": 2, "unidades_inventario": 6, "valor_neto_inventario": 6000.0,
                    "documentos_caja": 2, "monto_caja": 23595, "unidades_repuestos_caja": 2},
    }
    plan["totales"].update(cambios_totales)
    return plan


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        r = cls.client.post("/login", data={"username": "admin", "password": "taller2026"})
        assert r.status_code in (200, 302)

    def setUp(self):
        # Cada test parte con el catálogo y la caja vacíos.
        db = core._conectar_db()
        db.execute("PRAGMA foreign_keys = OFF")
        for t in ("caja_gasto_detalles", "caja_movimientos", "caja_rendiciones", "stock_proveedores", "movimientos",
                  "stock_minimo_calculo", "productos", "proveedores"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()

    def _post(self, plan, accion="ensayar", confirmar=None, cliente=None):
        datos = {"accion": accion,
                 "archivo": (io.BytesIO(json.dumps(plan).encode("utf-8")), "plan_carga_prueba.json")}
        if confirmar is not None:
            datos["confirmar_texto"] = confirmar
        return (cliente or self.client).post("/api/admin/carga-inicial", data=datos,
                                             content_type="multipart/form-data")

    def _contar(self, tabla):
        db = core._conectar_db()
        n = db.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
        db.close()
        return n


class TestCargaInicial(_Base):

    def test_ensayar_verifica_y_no_escribe(self):
        r = self._post(plan_de_prueba())
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        d = r.get_json()
        self.assertTrue(all(c["ok"] for c in d["chequeos"]), d["chequeos"])
        self.assertFalse(d["aplicado"])
        self.assertEqual(0, self._contar("productos"))
        self.assertEqual(0, self._contar("caja_movimientos"))

    def test_aplicar_exige_escribir_cargar(self):
        r = self._post(plan_de_prueba(), accion="aplicar", confirmar="si")
        self.assertEqual(400, r.status_code)
        self.assertEqual(0, self._contar("productos"))

    def test_aplicar_escribe_todo_y_respalda(self):
        r = self._post(plan_de_prueba(), accion="aplicar", confirmar="cargar")
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        d = r.get_json()
        self.assertTrue(d["aplicado"])
        respaldo = os.path.join(os.path.dirname(_TMP_DB_PATH), d["respaldo"])
        self.assertTrue(os.path.isfile(respaldo))
        os.remove(respaldo)

        db = core._conectar_db()
        # 2 del plan + el RETEN de caja chica (el ACEITE de caja suma al producto que ya existía).
        self.assertEqual(3, db.execute("SELECT COUNT(*) FROM productos").fetchone()[0])
        discos = db.execute("SELECT * FROM productos WHERE modelo = 'DISCOS'").fetchone()
        self.assertEqual((4, 1250.0, "U660", "354"),
                         (discos["stock_disponible"], round(discos["costo_compra"], 2), discos["categoria"], discos["ubicacion"]))
        baldes = {r["nombre"]: r["cantidad"] for r in db.execute(
            "SELECT COALESCE(p.nombre, '(sin)') nombre, s.cantidad FROM stock_proveedores s "
            "LEFT JOIN proveedores p ON p.id = s.proveedor_id WHERE s.producto_id = ?", (discos["id"],))}
        self.assertEqual({"PROV A": 3, "(sin)": 1}, baldes)
        aceite = db.execute("SELECT stock_disponible FROM productos WHERE modelo = 'ACEITE 1L'").fetchone()
        self.assertEqual(3, aceite["stock_disponible"])  # 2 del plan + 1 de caja chica
        # Caja: rendida entera y con el saldo intacto.
        self.assertEqual(0, db.execute("SELECT COUNT(*) FROM caja_gasto_detalles WHERE rendicion_id IS NULL").fetchone()[0])
        saldo = db.execute("SELECT COALESCE(SUM(CASE WHEN tipo='aporte' THEN monto ELSE -monto END),0) "
                           "FROM caja_movimientos WHERE medio_pago='efectivo_cajero'").fetchone()[0]
        self.assertEqual(0, saldo)
        db.close()

    def test_no_se_puede_repetir(self):
        r = self._post(plan_de_prueba(), accion="aplicar", confirmar="CARGAR")
        self.assertEqual(200, r.status_code)
        os.remove(os.path.join(os.path.dirname(_TMP_DB_PATH), r.get_json()["respaldo"]))
        r = self._post(plan_de_prueba())
        self.assertEqual(409, r.status_code)
        self.assertIn("ya tiene productos", r.get_json()["error"])

    def test_totales_que_no_cuadran_no_escriben_nada(self):
        r = self._post(plan_de_prueba(unidades_inventario=7), accion="aplicar", confirmar="CARGAR")
        self.assertEqual(409, r.status_code)
        self.assertIn("unidades en stock", r.get_json()["error"])
        self.assertEqual(0, self._contar("productos"))
        self.assertEqual(0, self._contar("proveedores"))
        self.assertEqual(0, self._contar("caja_movimientos"))

    def test_un_archivo_que_no_es_un_plan_se_rechaza(self):
        r = self._post({"hola": 1})
        self.assertEqual(409, r.status_code)
        self.assertIn("plan", r.get_json()["error"])
        r = self.client.post("/api/admin/carga-inicial", content_type="multipart/form-data",
                             data={"accion": "ensayar", "archivo": (io.BytesIO(b"no es json"), "x.json")})
        self.assertEqual(400, r.status_code)

    def test_solo_admin(self):
        db = core._conectar_db()
        hash_ = taller_app.generate_password_hash("clave-bodega-1")
        db.execute("DELETE FROM usuarios WHERE username = 'bodega_ci'")
        db.execute("INSERT INTO usuarios (username, nombre, password_hash, rol, creado_en) "
                   "VALUES ('bodega_ci', 'Bodega', ?, 'bodega', ?)", (hash_, core.now_iso()))
        db.commit()
        db.close()
        otro = taller_app.app.test_client()
        otro.post("/login", data={"username": "bodega_ci", "password": "clave-bodega-1"})
        r = self._post(plan_de_prueba(), cliente=otro)
        self.assertIn(r.status_code, (302, 401, 403))
        self.assertEqual(0, self._contar("productos"))

    def test_la_pantalla_responde(self):
        r = self.client.get("/admin/carga-inicial")
        self.assertEqual(200, r.status_code)
        self.assertIn("Carga inicial", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
