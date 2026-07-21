"""Tests del aviso de "pruebas de ruta por revisar" para Operativo (ver
Migración 23 en app.py) -- mismo mecanismo que ya existía para sugerencias
de diagnóstico (sugerencias_diagnostico.atendida), aplicado a las entradas
de la bitácora de pruebas de ruta que llegan por WhatsApp del perfil
'probador_de_autos'. Cubre:
  1. /api/notificaciones/resumen ahora incluye 'pruebas_ruta' además de
     'sugerencias_diagnostico'.
  2. /api/notificaciones/pruebas-ruta solo lista entradas de origen
     'whatsapp' sin atender (las cargadas a mano desde la web no generan
     aviso).
  3. Marcar una entrada como atendida la saca del aviso pero NO la borra ni
     la oculta de la bitácora real de la OT.
  4. Ambos endpoints nuevos exigen rol operativo/admin.
"""
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_notifop_", suffix=".db")
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

    def _registrar_mecanico(self, nombre, telefono, tipo="probador_de_autos"):
        resp = self.client.post("/api/agente/mecanicos", json={"nombre": nombre, "telefono": telefono, "tipo": tipo})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _mandar_texto(self, telefono, texto):
        return self.client.post("/api/agente/simular-mensaje", json={"telefono": telefono, "texto": texto})

    def _agregar_prueba_ruta_web(self, orden_id, texto):
        resp = self.client.post(f"/api/operativo/ordenes/{orden_id}/pruebas-ruta", json={"texto": texto})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()


class TestResumenYListaPruebasRuta(_Base):
    def test_resumen_incluye_pruebas_ruta_junto_a_sugerencias(self):
        resumen = self.client.get("/api/notificaciones/resumen").get_json()
        self.assertIn("sugerencias_diagnostico", resumen)
        self.assertIn("pruebas_ruta", resumen)

    def test_entrada_por_whatsapp_aparece_en_la_lista_de_pendientes(self):
        self._registrar_mecanico("Probador Notif Uno", "+56900910001")
        cliente = self._crear_cliente("Cliente Notif Uno")
        orden = self._crear_orden(cliente["id"])

        antes = self.client.get("/api/notificaciones/resumen").get_json()["pruebas_ruta"]
        resp = self._mandar_texto("+56900910001", f"ot {orden['id']} prueba de ruta sin novedades, todo ok")
        self.assertTrue(resp.get_json()["exito"], resp.get_data(as_text=True))

        despues = self.client.get("/api/notificaciones/resumen").get_json()["pruebas_ruta"]
        self.assertEqual(despues, antes + 1)

        lista = self.client.get("/api/notificaciones/pruebas-ruta").get_json()
        encontrada = [p for p in lista if p["orden_id"] == orden["id"]]
        self.assertEqual(len(encontrada), 1)
        self.assertIn("sin novedades", encontrada[0]["texto"])
        self.assertEqual(encontrada[0]["cliente_nombre"], "CLIENTE NOTIF UNO")

    def test_entrada_cargada_a_mano_desde_la_web_no_genera_aviso(self):
        cliente = self._crear_cliente("Cliente Notif Web")
        orden = self._crear_orden(cliente["id"])

        antes = self.client.get("/api/notificaciones/resumen").get_json()["pruebas_ruta"]
        self._agregar_prueba_ruta_web(orden["id"], "Prueba anotada directo por el mecánico en el taller")
        despues = self.client.get("/api/notificaciones/resumen").get_json()["pruebas_ruta"]

        self.assertEqual(despues, antes)  # no sumó aviso
        lista = self.client.get("/api/notificaciones/pruebas-ruta").get_json()
        self.assertNotIn(orden["id"], [p["orden_id"] for p in lista])


class TestAtenderPruebaRuta(_Base):
    def test_marcar_atendida_la_saca_del_aviso_pero_no_de_la_bitacora(self):
        self._registrar_mecanico("Probador Notif Dos", "+56900910002")
        cliente = self._crear_cliente("Cliente Notif Dos")
        orden = self._crear_orden(cliente["id"])
        self._mandar_texto("+56900910002", f"ot {orden['id']} probé el auto, ruido en suspensión delantera")

        lista = self.client.get("/api/notificaciones/pruebas-ruta").get_json()
        prueba = [p for p in lista if p["orden_id"] == orden["id"]][0]

        resp = self.client.post(f"/api/operativo/pruebas-ruta/{prueba['id']}/atender", json={})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["atendida"], 1)

        lista_despues = self.client.get("/api/notificaciones/pruebas-ruta").get_json()
        self.assertNotIn(orden["id"], [p["orden_id"] for p in lista_despues])

        # Sigue en la bitácora real de la OT -- atender NO la borra.
        orden_detalle = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(len(orden_detalle["pruebas_ruta"]), 1)
        self.assertIn("ruido en suspensión", orden_detalle["pruebas_ruta"][0]["texto"])

    def test_atender_prueba_inexistente_devuelve_404(self):
        resp = self.client.post("/api/operativo/pruebas-ruta/999999/atender", json={})
        self.assertEqual(resp.status_code, 404)


class TestAccesoPorRolNotificacionesPruebasRuta(_Base):
    def _crear_usuario(self, username, rol):
        resp = self.client.post(
            "/api/admin/usuarios",
            json={"username": username, "password": "clave12345", "nombre": username, "modulos": [rol]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

    def _client_como(self, username):
        c = taller_app.app.test_client()
        c.post("/login", data={"username": username, "password": "clave12345"})
        return c

    def test_bodega_no_puede_ver_pruebas_de_ruta_pendientes(self):
        self._crear_usuario("bodega_notifop", "bodega")
        c = self._client_como("bodega_notifop")
        self.assertEqual(c.get("/api/notificaciones/pruebas-ruta").status_code, 403)
        self.assertEqual(c.get("/api/notificaciones/resumen").status_code, 403)

    def test_operativo_si_puede(self):
        self._crear_usuario("operativo_notifop", "operativo")
        c = self._client_como("operativo_notifop")
        self.assertEqual(c.get("/api/notificaciones/pruebas-ruta").status_code, 200)
        self.assertEqual(c.get("/api/notificaciones/resumen").status_code, 200)


if __name__ == "__main__":
    unittest.main()
