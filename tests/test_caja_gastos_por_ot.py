"""Tests de Caja Chica -- Migración 25: rendición parcial por boletas.

Antes, un "gasto" de Caja Chica era una sola cosa (el retiro Y su boleta a
la vez). El taller pidió separarlo en dos pasos:
  1. "Nuevo gasto": solo monto, medio de pago y fecha -- quien lo registra
     viendo el banco no tiene más información que esa a mano.
  2. Una o más "boletas" (caja_gasto_detalles) contra ese mismo gasto,
     cargadas después por quien sí tiene la boleta física en mano (puede
     ser otra persona, ej. Cristian contrastando contra el trabajador que
     compró). Cada boleta tiene su propio N° de documento, monto (no
     necesariamente el 100% del retiro -- sobre todo en efectivo), rubro
     GAV, detalle y, si el rubro es "Gastos por OT", la orden de trabajo
     relacionada.
El gasto queda "listo para rendir" cuando la suma de sus boletas alcanza el
monto del retiro, o cuando se cierra manualmente (vuelto en caja, boleta
chica perdida). Solo ahí puede incluirse en una Rendición
(POST /api/caja/rendicion, sin cambios en su forma -- sigue siendo un lote
de gastos ya listos).

También cubre que "Gastos por OT" se excluye del total de GAV en el Estado
de Resultados (no es gasto administrativo, es costo de una OT puntual) pero
se reporta aparte.
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

    def _agregar_boleta(self, gasto_id, **campos):
        base = {"monto": "1000"}
        base.update(campos)
        return self.client.post(f"/api/caja/gasto/{gasto_id}/boleta", data=base)


class TestNuevoGastoSimplificado(_Base):
    def test_crear_gasto_solo_pide_monto_medio_y_fecha(self):
        resp = self._crear_gasto()
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["monto"], 1000.0)
        self.assertEqual(g["medio_pago"], "efectivo_cajero")
        # Recién creado, sin boletas: no está listo para rendir.
        self.assertEqual(g["monto_justificado"], 0)
        self.assertEqual(g["monto_pendiente"], 1000.0)
        self.assertFalse(g["listo_para_rendir"])
        self.assertEqual(g["boletas"], [])

    def test_gasto_sin_monto_es_rechazado(self):
        resp = self.client.post(
            "/api/caja/gasto", data={"medio_pago": "efectivo_cajero", "fecha": "2026-07-15"}
        )
        self.assertEqual(resp.status_code, 400)


class TestBoletasParciales(_Base):
    def test_una_boleta_por_el_monto_completo_deja_el_gasto_listo(self):
        gasto = self._crear_gasto(monto="15000").get_json()
        resp = self._agregar_boleta(gasto["id"], monto="15000", numero_documento="F-1", rubro_gav="otros")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["monto_justificado"], 15000.0)
        self.assertEqual(g["monto_pendiente"], 0)
        self.assertTrue(g["listo_para_rendir"])
        self.assertEqual(len(g["boletas"]), 1)

    def test_varias_boletas_parciales_van_reduciendo_el_pendiente(self):
        """El caso central pedido por el taller: un retiro de $200.000 en
        efectivo que se justifica con boletas parciales, sin perder la
        información entre una y otra."""
        gasto = self._crear_gasto(monto="200000").get_json()

        r1 = self._agregar_boleta(gasto["id"], monto="60000", numero_documento="F-1", rubro_gav="otros")
        self.assertEqual(r1.status_code, 201, r1.get_data(as_text=True))
        self.assertEqual(r1.get_json()["monto_pendiente"], 140000.0)
        self.assertFalse(r1.get_json()["listo_para_rendir"])

        r2 = self._agregar_boleta(gasto["id"], monto="90000", numero_documento="F-2", rubro_gav="mantencion")
        self.assertEqual(r2.get_json()["monto_pendiente"], 50000.0)

        r3 = self._agregar_boleta(gasto["id"], monto="50000", numero_documento="F-3", rubro_gav="generales")
        self.assertEqual(r3.get_json()["monto_pendiente"], 0)
        self.assertTrue(r3.get_json()["listo_para_rendir"])
        self.assertEqual(len(r3.get_json()["boletas"]), 3)

    def test_boleta_rubro_gastos_por_ot_exige_orden_de_trabajo(self):
        gasto = self._crear_gasto(monto="7000").get_json()
        resp = self._agregar_boleta(gasto["id"], monto="7000", rubro_gav="gastos_por_ot")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("orden de trabajo", resp.get_json()["error"].lower())

    def test_boleta_gastos_por_ot_con_orden_valida_queda_ligada_a_la_ot(self):
        cliente = self._crear_cliente("CLIENTE BOLETA OT")
        orden = self._crear_orden(cliente["id"])
        gasto = self._crear_gasto(monto="7000").get_json()

        resp = self._agregar_boleta(
            gasto["id"], monto="7000", rubro_gav="gastos_por_ot", orden_id=str(orden["id"]), numero_documento="F-9"
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        boleta = resp.get_json()["boletas"][0]
        self.assertEqual(boleta["orden_id"], orden["id"])
        self.assertEqual(boleta["rubro_gav"], "gastos_por_ot")

    def test_boleta_con_orden_id_inexistente_es_rechazada(self):
        gasto = self._crear_gasto(monto="7000").get_json()
        resp = self._agregar_boleta(gasto["id"], monto="7000", rubro_gav="gastos_por_ot", orden_id="999999")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("no existe", resp.get_json()["error"].lower())

    def test_agregar_boleta_a_gasto_ya_rendido_es_rechazado(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1")
        rend = self.client.post("/api/caja/rendicion", json={"gasto_ids": [gasto["id"]]})
        self.assertEqual(rend.status_code, 201, rend.get_data(as_text=True))

        resp = self._agregar_boleta(gasto["id"], monto="500", numero_documento="F-2")
        self.assertEqual(resp.status_code, 409, resp.get_data(as_text=True))

    def test_editar_boleta(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        boleta = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1", rubro_gav="otros")
        boleta_id = boleta.get_json()["boletas"][0]["id"]

        resp = self.client.put(f"/api/caja/boleta/{boleta_id}", data={
            "monto": "1000", "numero_documento": "F-1-CORREGIDA", "rubro_gav": "otros",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        boletas = resp.get_json()["boletas"]
        self.assertEqual(boletas[0]["numero_documento"], "F-1-CORREGIDA")

    def test_eliminar_boleta_reabre_el_gasto(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        boleta = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1", rubro_gav="otros")
        boleta_id = boleta.get_json()["boletas"][0]["id"]
        self.assertTrue(boleta.get_json()["listo_para_rendir"])

        resp = self.client.delete(f"/api/caja/boleta/{boleta_id}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["monto_pendiente"], 1000.0)
        self.assertFalse(resp.get_json()["listo_para_rendir"])


class TestCierreManual(_Base):
    def test_cerrar_manualmente_deja_listo_para_rendir_aunque_falte_monto(self):
        gasto = self._crear_gasto(monto="20000").get_json()
        self._agregar_boleta(gasto["id"], monto="18000", numero_documento="F-1", rubro_gav="otros")

        resp = self.client.put(f"/api/caja/gasto/{gasto['id']}/cerrar", json={"cerrado": True})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["monto_pendiente"], 2000.0)  # el pendiente real no cambia...
        self.assertTrue(g["listo_para_rendir"])          # ...pero ya puede rendirse
        self.assertTrue(g["cerrado_manualmente"])

    def test_agregar_otra_boleta_reabre_el_cierre_manual(self):
        gasto = self._crear_gasto(monto="20000").get_json()
        self._agregar_boleta(gasto["id"], monto="18000", numero_documento="F-1", rubro_gav="otros")
        self.client.put(f"/api/caja/gasto/{gasto['id']}/cerrar", json={"cerrado": True})

        resp = self._agregar_boleta(gasto["id"], monto="2000", numero_documento="F-2", rubro_gav="otros")
        g = resp.get_json()
        self.assertFalse(g["cerrado_manualmente"])
        self.assertEqual(g["monto_pendiente"], 0)
        self.assertTrue(g["listo_para_rendir"])


class TestRendicionRequiereGastoListo(_Base):
    def test_rendir_gasto_sin_boletas_es_rechazado(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        resp = self.client.post("/api/caja/rendicion", json={"gasto_ids": [gasto["id"]]})
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("listos para rendir", resp.get_json()["error"])

    def test_rendir_gasto_parcialmente_justificado_es_rechazado(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        self._agregar_boleta(gasto["id"], monto="400", numero_documento="F-1")
        resp = self.client.post("/api/caja/rendicion", json={"gasto_ids": [gasto["id"]]})
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))

    def test_rendir_gasto_listo_funciona_y_queda_fuera_de_pendientes(self):
        gasto = self._crear_gasto(monto="1000", medio_pago="tarjeta_empresa").get_json()
        self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1", rubro_gav="otros")

        resp = self.client.post("/api/caja/rendicion", json={"gasto_ids": [gasto["id"]], "medio_pago": "tarjeta_empresa"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        pend = self.client.get("/api/caja/pendientes?medio_pago=tarjeta_empresa").get_json()["gastos"]
        self.assertFalse(any(g["id"] == gasto["id"] for g in pend))

        det = self.client.get(f"/api/caja/rendicion/{resp.get_json()['id']}").get_json()
        self.assertEqual(len(det["gastos"]), 1)
        self.assertEqual(det["gastos"][0]["boletas"][0]["numero_documento"], "F-1")


class TestGastosPorOtExcluidoDelGav(_Base):
    def test_gastos_por_ot_no_suma_en_gav_total_pero_se_reporta_aparte(self):
        cliente = self._crear_cliente("CLIENTE ESTADO RESULTADOS")
        orden = self._crear_orden(cliente["id"])
        # Mes propio para no mezclarse con otros tests de este archivo.
        mes = "2031-08"

        g1 = self._crear_gasto(monto="5000", fecha=f"{mes}-10").get_json()
        self._agregar_boleta(g1["id"], monto="5000", rubro_gav="otros", numero_documento="F-1")

        g2 = self._crear_gasto(monto="7000", fecha=f"{mes}-11").get_json()
        self._agregar_boleta(
            g2["id"], monto="7000", rubro_gav="gastos_por_ot",
            orden_id=str(orden["id"]), numero_documento="F-2",
        )

        resp = self.client.get(f"/api/finanzas/estado-resultados?mes={mes}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        gav = resp.get_json()["gav"]

        self.assertNotIn("gastos_por_ot", gav["por_rubro"])
        self.assertEqual(gav["gastos_por_ot_total"], 7000.0)
        self.assertNotIn("gastos_por_ot", gav["rubros_label"])

    def test_rubros_label_no_incluye_gastos_por_ot(self):
        resp = self.client.get("/api/finanzas/estado-resultados?mes=2031-08")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rubros_label = resp.get_json()["gav"]["rubros_label"]
        self.assertNotIn("gastos_por_ot", rubros_label)
        self.assertIn("otros", rubros_label)


if __name__ == "__main__":
    unittest.main()
