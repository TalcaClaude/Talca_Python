"""
Tests para la Fase 3 de fotos de vehículos: un mecánico que solo tiene
acceso al WhatsApp (no al sistema) manda una foto de un daño encontrado
durante el desarme/armado con la OT o la patente en el pie de foto, y el
agente la archiva automáticamente en fotos_vehiculo de esa OT --
origen='desarme_armado' (para distinguirla de las fotos obligatorias de
ingreso), con 'usuario' = el nombre del mecánico registrado (siempre debe
quedar quién es el responsable de la foto).

Ver app.py: _agente_procesar_mensaje (rama 'vehiculo'), _normalizar_patente.

Cómo correrlos:
    cd tests && python3 -m unittest test_agente_fotos_whatsapp -v
"""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_agfotos_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


def _imagen_jpeg_valida():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (80, 90, 100)).save(buf, format="JPEG")
    return buf.getvalue()


class TestFotosPorWhatsAppFase3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Fotos WSP"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_equipo(self, cliente_id, identificador):
        resp = self.client.post(
            "/api/operativo/equipos",
            json={"cliente_id": cliente_id, "identificador": identificador, "marca": "Marca", "modelo": "Modelo"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id, equipo_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id, "equipo_id": equipo_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _registrar_mecanico(self, nombre, telefono):
        resp = self.client.post("/api/agente/mecanicos", json={"nombre": nombre, "telefono": telefono})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _subir_foto(self, telefono, texto="", contenido=None):
        data = {"telefono": telefono, "foto": (io.BytesIO(contenido or _imagen_jpeg_valida()), "foto.jpg")}
        if texto:
            data["texto"] = texto
        return self.client.post("/api/agente/simular-mensaje-foto", data=data, content_type="multipart/form-data")

    def _fotos_de(self, orden_id):
        return self.client.get(f"/api/operativo/ordenes/{orden_id}/fotos").get_json()["fotos"]

    def test_foto_con_numero_de_ot_en_el_texto_se_archiva(self):
        self._registrar_mecanico("Mecánico Uno", "+56900800001")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF001")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        respuesta_ia = {"acciones": [], "resumen": "solo describe un daño, no es una acción"}
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": ""}), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto("+56900800001", texto=f"ot {orden['id']} cable cortado en el motor")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "foto_vehiculo_archivada")
        self.assertEqual(data["ot_id"], orden["id"])
        self.assertIn(str(orden["id"]), data["respuesta_texto"])

        fotos = self._fotos_de(orden["id"])
        self.assertEqual(len(fotos), 1)
        self.assertEqual(fotos[0]["origen"], "desarme_armado")
        self.assertEqual(fotos[0]["categoria"], "libre")
        self.assertEqual(fotos[0]["usuario"], "MECÁNICO UNO")  # responsable siempre queda registrado
        self.assertIn("cable cortado", fotos[0]["titulo"].lower())

    def test_foto_de_una_pieza_suelta_clasificada_otro_igual_se_archiva_con_numero_de_ot(self):
        # Caso real reportado por el usuario: mandó la foto de una carcasa/
        # radiador de aceite (una pieza suelta, sin verse el auto completo)
        # con "Ot 3103" en el pie de foto. La IA la clasificó como 'otro'
        # (no 'vehiculo'), y con la lógica vieja eso hacía que se rechazara
        # como "no reconocida" aunque el número de OT estaba clarísimo en
        # el texto. Ahora el archivado no exige tipo_foto == 'vehiculo'.
        self._registrar_mecanico("Mecánico Diez", "+56900800010")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF010")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "otro", "patente": ""}):
            resp = self._subir_foto("+56900800010", texto=f"Ot {orden['id']}")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "foto_vehiculo_archivada")
        self.assertEqual(data["ot_id"], orden["id"])

        fotos = self._fotos_de(orden["id"])
        self.assertEqual(len(fotos), 1)
        self.assertEqual(fotos[0]["origen"], "desarme_armado")
        self.assertEqual(fotos[0]["usuario"], "MECÁNICO DIEZ")

    def test_foto_con_patente_detectada_se_archiva_en_la_ot_correcta(self):
        self._registrar_mecanico("Mecánico Dos", "+56900800002")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF002")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        respuesta_ia = {"acciones": [], "resumen": "solo describe un daño, no es una acción"}
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": "AGF002"}), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto("+56900800002", texto="rayón en la puerta")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["ot_id"], orden["id"])

        fotos = self._fotos_de(orden["id"])
        self.assertEqual(len(fotos), 1)
        self.assertEqual(fotos[0]["usuario"], "MECÁNICO DOS")

    def test_patente_con_guiones_o_minusculas_igual_matchea(self):
        self._registrar_mecanico("Mecánico Tres", "+56900800003")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AG-F003")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        respuesta_ia = {"acciones": [], "resumen": "solo describe un daño, no es una acción"}
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": "agf003"}), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto("+56900800003", texto="algo se soltó")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["ot_id"], orden["id"])

    def test_foto_sin_texto_igual_se_archiva_si_hay_ot_por_patente(self):
        # Fase 3: a diferencia del comportamiento anterior (que exigía pie
        # de foto obligatorio), ahora una foto sin ningún texto igual se
        # archiva si la patente matchea una OT abierta -- el mecánico no
        # tiene por qué escribir nada, puede mandar solo la foto.
        self._registrar_mecanico("Mecánico Cuatro", "+56900800004")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF004")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": "AGF004"}):
            resp = self._subir_foto("+56900800004")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "foto_vehiculo_archivada")
        fotos = self._fotos_de(orden["id"])
        self.assertEqual(len(fotos), 1)
        self.assertEqual(fotos[0]["titulo"], "FOTO ENVIADA POR WHATSAPP")  # título por defecto sin caption

    def test_ot_numero_explicito_pero_no_existe_avisa_sin_archivar(self):
        self._registrar_mecanico("Mecánico Cinco", "+56900800005")
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": ""}):
            resp = self._subir_foto("+56900800005", texto="ot 999999 problema raro")

        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "foto_vehiculo_ot_no_encontrada")
        self.assertIn("999999", data["respuesta_texto"])

    def test_ot_numero_explicito_tiene_prioridad_sobre_la_patente(self):
        self._registrar_mecanico("Mecánico Seis", "+56900800006")
        cliente = self._crear_cliente()
        equipo_a = self._crear_equipo(cliente["id"], "AGF006A")
        equipo_b = self._crear_equipo(cliente["id"], "AGF006B")
        orden_a = self._crear_orden(cliente["id"], equipo_a["id"])
        orden_b = self._crear_orden(cliente["id"], equipo_b["id"])

        # La IA "lee" la patente del vehículo A en la foto, pero el mecánico
        # escribió el número de la OT B en el texto -- gana lo explícito.
        respuesta_ia = {"acciones": [], "resumen": "solo describe un daño, no es una acción"}
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": "AGF006A"}), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto("+56900800006", texto=f"ot {orden_b['id']} correa suelta")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["ot_id"], orden_b["id"])
        self.assertEqual(len(self._fotos_de(orden_a["id"])), 0)
        self.assertEqual(len(self._fotos_de(orden_b["id"])), 1)

    def test_foto_con_accion_de_texto_ademas_archiva_y_mueve_la_ot(self):
        self._registrar_mecanico("Mecánico Siete", "+56900800007")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF007")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        respuesta_ia = {
            "acciones": [{"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "reparacion"}],
            "resumen": "ok",
        }
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": ""}), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto("+56900800007", texto=f"ot {orden['id']} pasa a reparación")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "reparacion")
        self.assertIn("archivada", data["respuesta_texto"])
        self.assertEqual(len(self._fotos_de(orden["id"])), 1)

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "reparacion")

    def test_archivado_reporta_exito_aunque_el_texto_no_traiga_accion_reconocible(self):
        # Si el texto que acompaña la foto (además de la OT) no se traduce
        # en ninguna acción concreta, la foto igual quedó guardada -- eso NO
        # debe reportarse como error al mecánico.
        self._registrar_mecanico("Mecánico Ocho", "+56900800008")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF008")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        respuesta_ia = {"acciones": [], "resumen": "no se entendió nada más"}
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": ""}), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto("+56900800008", texto=f"ot {orden['id']} mmm no sé qué más decir")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertIn("archivada", data["respuesta_texto"])
        self.assertEqual(len(self._fotos_de(orden["id"])), 1)

    def test_fotos_de_whatsapp_no_cuentan_como_obligatorias_de_ingreso(self):
        # Las 7 categorías obligatorias de ingreso son un requisito aparte
        # (Fase 1) -- una foto libre archivada por WhatsApp durante el
        # desarme/armado no debe hacer que 'faltan_obligatorias' se dé por
        # cumplido.
        self._registrar_mecanico("Mecánico Nueve", "+56900800009")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "AGF009")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "vehiculo", "patente": "AGF009"}):
            self._subir_foto("+56900800009", texto="motor con fuga de aceite")

        listado = self.client.get(f"/api/operativo/ordenes/{orden['id']}/fotos").get_json()
        self.assertEqual(len(listado["faltan_obligatorias"]), 7)


if __name__ == "__main__":
    unittest.main()
