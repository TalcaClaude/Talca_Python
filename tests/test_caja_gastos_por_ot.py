"""Tests de Caja Chica -- Migración 25: número de boleta/factura
(numero_documento) y orden de trabajo relacionada (orden_id) en
caja_movimientos, más el nuevo rubro_gav "gastos_por_ot".

Cubre el flujo pedido por el taller: quien registra el "Nuevo gasto" solo
tiene el retiro del banco (monto), sin boleta ni detalle a mano -- esos
campos quedan opcionales al crear y se completan después, al editar el
gasto durante "Generar rendición" (PUT /api/caja/movimiento/<id>).

También cubre que "Gastos por OT" (costo asociado a una OT puntual) se
excluye del total de GAV en el Estado de Resultados -- no es gasto
administrativo general -- pero se reporta aparte (gastos_por_ot_total).
"""
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_cajaot_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_gasto(self, **campos):
        base = {"monto": "1000", "medio_pago": "efectivo_cajero", "fecha": "2026-07-15"}
        base.update(campos)
        return self.client.post("/api/caja/gasto", data=base)

    def _editar_movimiento(self, mov_id, **campos):
        base = {"monto": "1000"}
        base.update(campos)
        return self.client.put(f"/api/caja/movimiento/{mov_id}", data=base)


class TestNumeroDocumentoYOrdenId(_Base):
    def test_crear_gasto_sin_numero_documento_ni_orden_ni_rubro(self):
        """Al crear, todos estos campos son opcionales -- el escenario
        típico de quien solo saca la info del banco."""
        resp = self._crear_gasto()
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["numero_documento"], "")
        self.assertIsNone(g["orden_id"])
        self.assertEqual(g["rubro_gav"], "")

    def test_crear_gasto_con_numero_documento(self):
        resp = self._crear_gasto(numero_documento="  boleta-4521  ")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["numero_documento"], "BOLETA-4521")

    def test_crear_gasto_orden_id_invalido_no_numerico(self):
        resp = self._crear_gasto(rubro_gav="gastos_por_ot", orden_id="abc")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("orden", resp.get_json()["error"].lower())

    def test_crear_gasto_orden_id_inexistente(self):
        resp = self._crear_gasto(rubro_gav="gastos_por_ot", orden_id="999999")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("no existe", resp.get_json()["error"].lower())

    def test_crear_gasto_orden_id_valido_con_rubro_gastos_por_ot(self):
        cliente = self._crear_cliente("CLIENTE OT CAJA")
        orden = self._crear_orden(cliente["id"])
        resp = self._crear_gasto(rubro_gav="gastos_por_ot", orden_id=str(orden["id"]))
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["rubro_gav"], "gastos_por_ot")
        self.assertEqual(g["orden_id"], orden["id"])

    def test_completar_datos_via_put_despues_de_creacion_liviana(self):
        """El flujo principal pedido: se crea "liviano" (solo banco) y se
        completa después desde "Generar rendición"."""
        cliente = self._crear_cliente("CLIENTE COMPLETAR")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_gasto().get_json()

        resp = self._editar_movimiento(
            creado["id"],
            numero_documento="F-1002",
            rubro_gav="gastos_por_ot",
            orden_id=str(orden["id"]),
            descripcion="Repuesto urgente",
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["numero_documento"], "F-1002")
        self.assertEqual(g["rubro_gav"], "gastos_por_ot")
        self.assertEqual(g["orden_id"], orden["id"])

    def test_editar_sin_orden_id_preserva_valor_existente(self):
        cliente = self._crear_cliente("CLIENTE PRESERVA")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_gasto(rubro_gav="gastos_por_ot", orden_id=str(orden["id"])).get_json()

        resp = self._editar_movimiento(creado["id"], numero_documento="F-9")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["orden_id"], orden["id"])

    def test_editar_con_orden_id_vacio_limpia_el_valor(self):
        cliente = self._crear_cliente("CLIENTE LIMPIA")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_gasto(rubro_gav="gastos_por_ot", orden_id=str(orden["id"])).get_json()

        resp = self._editar_movimiento(creado["id"], orden_id="", rubro_gav="otros")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertIsNone(g["orden_id"])

    def test_pendientes_incluye_numero_documento_y_orden_id(self):
        cliente = self._crear_cliente("CLIENTE PENDIENTES")
        orden = self._crear_orden(cliente["id"])
        self._crear_gasto(
            medio_pago="tarjeta_empresa", numero_documento="F-77",
            rubro_gav="gastos_por_ot", orden_id=str(orden["id"]),
        )
        resp = self.client.get("/api/caja/pendientes?medio_pago=tarjeta_empresa")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        gastos = resp.get_json()["gastos"]
        encontrado = next(g for g in gastos if g["numero_documento"] == "F-77")
        self.assertEqual(encontrado["orden_id"], orden["id"])
        self.assertEqual(encontrado["rubro_gav"], "gastos_por_ot")


class TestGastosPorOtExcluidoDelGav(_Base):
    def test_gastos_por_ot_no_suma_en_gav_total_pero_se_reporta_aparte(self):
        cliente = self._crear_cliente("CLIENTE ESTADO RESULTADOS")
        orden = self._crear_orden(cliente["id"])
        # Mes distinto al resto de los tests de esta clase (que usan la
        # fecha por defecto 2026-07-15) para que este total no se contamine
        # con gastos "gastos_por_ot" creados en otros tests.
        mes = "2031-07"

        self._crear_gasto(
            monto="5000", fecha=f"{mes}-10", rubro_gav="otros", numero_documento="F-1",
        )
        self._crear_gasto(
            monto="7000", fecha=f"{mes}-11", rubro_gav="gastos_por_ot",
            orden_id=str(orden["id"]), numero_documento="F-2",
        )

        resp = self.client.get(f"/api/finanzas/estado-resultados?mes={mes}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        gav = resp.get_json()["gav"]

        self.assertNotIn("gastos_por_ot", gav["por_rubro"])
        self.assertEqual(gav["gastos_por_ot_total"], 7000.0)
        # El total de GAV no debe incluir los 7000 del gasto por OT.
        self.assertNotIn("gastos_por_ot", gav["rubros_label"])

    def test_rubros_label_no_incluye_gastos_por_ot(self):
        resp = self.client.get("/api/finanzas/estado-resultados?mes=2026-07")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rubros_label = resp.get_json()["gav"]["rubros_label"]
        self.assertNotIn("gastos_por_ot", rubros_label)
        self.assertIn("otros", rubros_label)


if __name__ == "__main__":
    unittest.main()
