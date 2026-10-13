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


class TestPerfilMecanicoRestringido(unittest.TestCase):
    """Perfil 'mecanico' (ver migración 16 / agente_mecanicos.tipo): puede
    mandar fotos igual que cualquier mecánico (se archivan igual), pero NO
    tiene permiso para cambiar diagnóstico/estado por WhatsApp -- su texto
    queda como sugerencia en sugerencias_diagnostico en vez de ejecutarse.
    El perfil 'completo' (default) sigue con el comportamiento de siempre."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Perfil"):
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

    def _registrar_mecanico(self, nombre, telefono, tipo="completo"):
        resp = self.client.post(
            "/api/agente/mecanicos", json={"nombre": nombre, "telefono": telefono, "tipo": tipo}
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _subir_foto(self, telefono, texto=""):
        data = {"telefono": telefono, "foto": (io.BytesIO(_imagen_jpeg_valida()), "foto.jpg")}
        if texto:
            data["texto"] = texto
        return self.client.post("/api/agente/simular-mensaje-foto", data=data, content_type="multipart/form-data")

    def _mandar_texto(self, telefono, texto):
        return self.client.post("/api/agente/simular-mensaje", json={"telefono": telefono, "texto": texto})

    def _sugerencias_de(self, orden_id):
        return self.client.get(f"/api/ordenes/{orden_id}/sugerencias-diagnostico").get_json()

    def test_mecanico_restringido_manda_foto_con_comentario_queda_como_sugerencia(self):
        self._registrar_mecanico("Mecánico Restringido Uno", "+56900900001", tipo="mecanico")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PERF001")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "otro", "patente": ""}):
            resp = self._subir_foto(
                "+56900900001", texto=f"ot {orden['id']} direccion hidraulica para reparar"
            )

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "foto_vehiculo_archivada_con_sugerencia")
        self.assertIn("archivada", data["respuesta_texto"])
        self.assertIn("sugerencia", data["respuesta_texto"])

        # La foto quedó archivada igual que a cualquier mecánico.
        fotos = self.client.get(f"/api/operativo/ordenes/{orden['id']}/fotos").get_json()["fotos"]
        self.assertEqual(len(fotos), 1)
        self.assertEqual(fotos[0]["origen"], "desarme_armado")

        # El diagnóstico real de la OT NO se tocó.
        orden_actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(orden_actual.get("diagnostico_tecnico") or "", "")

        # El comentario quedó como sugerencia pendiente, vinculada a la foto.
        sugerencias = self._sugerencias_de(orden["id"])
        self.assertEqual(len(sugerencias), 1)
        self.assertIn("direccion hidraulica", sugerencias[0]["texto"])
        self.assertEqual(sugerencias[0]["mecanico_nombre"], "MECÁNICO RESTRINGIDO UNO")
        self.assertIsNotNone(sugerencias[0]["foto_id"])
        self.assertEqual(sugerencias[0]["atendida"], 0)

    def test_mecanico_restringido_manda_solo_texto_con_ot_queda_como_sugerencia(self):
        self._registrar_mecanico("Mecánico Restringido Dos", "+56900900002", tipo="mecanico")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PERF002")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto("+56900900002", f"ot {orden['id']} el auto ya casi queda listo")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "sugerencia_registrada")
        self.assertEqual(data["ot_id"], orden["id"])

        sugerencias = self._sugerencias_de(orden["id"])
        self.assertEqual(len(sugerencias), 1)
        self.assertIsNone(sugerencias[0]["foto_id"])

    def test_mecanico_restringido_manda_texto_sin_ot_pide_el_numero(self):
        self._registrar_mecanico("Mecánico Restringido Tres", "+56900900003", tipo="mecanico")
        resp = self._mandar_texto("+56900900003", "el auto ya casi queda listo")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "sugerencia_sin_ot")
        self.assertIn("número de OT", data["respuesta_texto"])

    def test_mecanico_completo_sigue_pudiendo_cambiar_diagnostico(self):
        # Regresión: el perfil 'completo' (default) NO debe verse afectado
        # por este cambio -- sigue pudiendo escribir diagnóstico por texto.
        self._registrar_mecanico("Mecánico Completo Uno", "+56900900004", tipo="completo")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PERF004")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        respuesta_ia = {
            "acciones": [{"tipo": "actualizar_diagnostico", "ot_id": orden["id"],
                          "diagnostico_tecnico": "Correa de distribución rota"}],
            "resumen": "actualiza diagnóstico",
        }
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._mandar_texto("+56900900004", f"ot {orden['id']} correa de distribución rota")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        orden_actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertIn("CORREA DE DISTRIBUCIÓN ROTA", (orden_actual.get("diagnostico_tecnico") or "").upper())
        self.assertEqual(self._sugerencias_de(orden["id"]), [])

    def test_atender_sugerencia_la_saca_de_pendientes(self):
        self._registrar_mecanico("Mecánico Restringido Cinco", "+56900900005", tipo="mecanico")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PERF005")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        self._mandar_texto("+56900900005", f"ot {orden['id']} algo raro en el motor")
        sugerencias = self._sugerencias_de(orden["id"])
        self.assertEqual(len(sugerencias), 1)

        resp = self.client.post(f"/api/sugerencias-diagnostico/{sugerencias[0]['id']}/atender")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(self._sugerencias_de(orden["id"]), [])

    def test_crear_mecanico_con_tipo_invalido_falla(self):
        resp = self.client.post(
            "/api/agente/mecanicos",
            json={"nombre": "Mecánico Malo", "telefono": "+56900900006", "tipo": "lo-que-sea"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_notificaciones_resumen_y_lista_ven_la_sugerencia_de_cualquier_ot(self):
        # La campanita (ver /api/notificaciones/*) no está acotada a una OT
        # -- tiene que juntar las sugerencias de TODAS las órdenes abiertas.
        self._registrar_mecanico("Mecánico Restringido Siete", "+56900900007", tipo="mecanico")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PERF007")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        antes = self.client.get("/api/notificaciones/resumen").get_json()
        self._mandar_texto("+56900900007", f"ot {orden['id']} correa floja")
        despues = self.client.get("/api/notificaciones/resumen").get_json()
        self.assertEqual(despues["sugerencias_diagnostico"], antes["sugerencias_diagnostico"] + 1)

        lista = self.client.get("/api/notificaciones/sugerencias-diagnostico").get_json()
        entrada = next(s for s in lista if s["ot_id"] == orden["id"])
        self.assertEqual(entrada["cliente_nombre"], cliente["nombre"])
        self.assertEqual(entrada["patente"], "PERF007")
        self.assertIn("correa floja", entrada["texto"])

        # Al atenderla, desaparece tanto del resumen como de la lista global.
        self.client.post(f"/api/sugerencias-diagnostico/{entrada['id']}/atender")
        lista_despues = self.client.get("/api/notificaciones/sugerencias-diagnostico").get_json()
        self.assertFalse(any(s["id"] == entrada["id"] for s in lista_despues))
        resumen_final = self.client.get("/api/notificaciones/resumen").get_json()
        self.assertEqual(resumen_final["sugerencias_diagnostico"], antes["sugerencias_diagnostico"])


class TestPerfilProbadorDeAutos(unittest.TestCase):
    """Perfil 'probador_de_autos' (ver migración 20/21 / agente_mecanicos.tipo):
    igual que 'mecanico', puede mandar fotos (se archivan igual) y NO tiene
    permiso para cambiar diagnóstico/estado por WhatsApp -- pero a diferencia
    de 'mecanico', su texto NO queda como sugerencia de diagnóstico: se
    guarda directo como una entrada nueva en la bitácora de pruebas de ruta
    (tabla pruebas_ruta), sin pasar por revisión."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Probador"):
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

    def _registrar_mecanico(self, nombre, telefono, tipo="completo"):
        resp = self.client.post(
            "/api/agente/mecanicos", json={"nombre": nombre, "telefono": telefono, "tipo": tipo}
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _subir_foto(self, telefono, texto=""):
        data = {"telefono": telefono, "foto": (io.BytesIO(_imagen_jpeg_valida()), "foto.jpg")}
        if texto:
            data["texto"] = texto
        return self.client.post("/api/agente/simular-mensaje-foto", data=data, content_type="multipart/form-data")

    def _mandar_texto(self, telefono, texto):
        return self.client.post("/api/agente/simular-mensaje", json={"telefono": telefono, "texto": texto})

    def _pruebas_ruta_de(self, orden_id):
        resp = self.client.get(f"/api/operativo/ordenes/{orden_id}")
        return resp.get_json()["pruebas_ruta"]

    def test_registrar_mecanico_con_tipo_invalido_es_rechazado(self):
        resp = self.client.post(
            "/api/agente/mecanicos",
            json={"nombre": "Tipo Invalido", "telefono": "+56900900099", "tipo": "no_existe"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_probador_manda_solo_texto_con_ot_queda_en_bitacora_no_en_sugerencias(self):
        self._registrar_mecanico("Probador Uno", "+56900900010", tipo="probador_de_autos")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PROB001")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto(
            "+56900900010", f"ot {orden['id']} probé el auto, vibración leve a 80 km/h, dirección ok"
        )
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "prueba_ruta_registrada")
        self.assertEqual(data["ot_id"], orden["id"])

        pruebas = self._pruebas_ruta_de(orden["id"])
        self.assertEqual(len(pruebas), 1)
        self.assertIn("vibración leve", pruebas[0]["texto"])
        self.assertEqual(pruebas[0]["autor"], "PROBADOR UNO")
        self.assertEqual(pruebas[0]["origen"], "whatsapp")
        self.assertIsNone(pruebas[0]["foto_id"])

        # NO debe haber quedado como sugerencia de diagnóstico -- es un
        # perfil distinto, con un destino distinto.
        sugerencias = self.client.get(f"/api/ordenes/{orden['id']}/sugerencias-diagnostico").get_json()
        self.assertEqual(len(sugerencias), 0)

        # El diagnóstico real de la OT tampoco se tocó.
        orden_actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(orden_actual.get("diagnostico_tecnico") or "", "")

    def test_probador_manda_foto_con_comentario_queda_en_bitacora_vinculada_a_la_foto(self):
        self._registrar_mecanico("Probador Dos", "+56900900011", tipo="probador_de_autos")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PROB002")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "otro", "patente": ""}):
            resp = self._subir_foto(
                "+56900900011", texto=f"ot {orden['id']} frenos responden bien en la prueba"
            )

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "prueba_ruta_registrada")

        pruebas = self._pruebas_ruta_de(orden["id"])
        self.assertEqual(len(pruebas), 1)
        self.assertIn("frenos responden bien", pruebas[0]["texto"])
        self.assertIsNotNone(pruebas[0]["foto_id"])

        # La foto también quedó archivada igual que a cualquier mecánico.
        fotos = self.client.get(f"/api/operativo/ordenes/{orden['id']}/fotos").get_json()["fotos"]
        self.assertEqual(len(fotos), 1)

    def test_probador_manda_texto_sin_ot_pide_el_numero(self):
        self._registrar_mecanico("Probador Tres", "+56900900012", tipo="probador_de_autos")
        resp = self._mandar_texto("+56900900012", "el auto responde bien en la prueba de ruta")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "sugerencia_sin_ot")
        self.assertIn("prueba", data["respuesta_texto"].lower())

    def test_probador_manda_texto_con_patente_en_vez_de_numero_de_ot(self):
        # No siempre el mecánico/probador se sabe el número de OT de memoria
        # -- si escribe la patente del vehículo (tal cual está registrada en
        # el equipo), el sistema la resuelve sola contra las OT abiertas sin
        # necesitar el número (ver _agente_resolver_ot_por_patente_en_texto).
        self._registrar_mecanico("Probador Patente", "+56900900014", tipo="probador_de_autos")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "RPYZ62")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto("+56900900014", "RPYZ62 vuelta corta, funcionamiento de marchas ok")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "prueba_ruta_registrada")
        self.assertEqual(data["ot_id"], orden["id"])

        pruebas = self._pruebas_ruta_de(orden["id"])
        self.assertEqual(len(pruebas), 1)
        self.assertIn("vuelta corta", pruebas[0]["texto"])

    def test_probador_manda_texto_con_patente_con_guion_igual_matchea(self):
        # La patente puede venir con guión ('RP-YZ-62') o sin él -- ambas
        # formas deben resolver a la misma OT (ver _normalizar_patente).
        self._registrar_mecanico("Probador Patente Guion", "+56900900015", tipo="probador_de_autos")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "RPYZ63")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto("+56900900015", "RP-YZ-63 sin testigos de falla, carga combustible 1/4")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["ot_id"], orden["id"])

    def test_mecanico_restringido_manda_texto_con_patente_queda_como_sugerencia(self):
        self._registrar_mecanico("Restringido Patente", "+56900900016", tipo="mecanico")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "RPYZ64")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto("+56900900016", "RPYZ64 el auto ya casi queda listo")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "sugerencia_registrada")
        self.assertEqual(data["ot_id"], orden["id"])

    def test_texto_con_patente_que_no_corresponde_a_ninguna_ot_abierta_pide_el_numero(self):
        # Una palabra que "parece patente" pero no está registrada en
        # ninguna OT abierta no debe matchear cualquier cosa -- sigue
        # pidiendo el número/patente correctos.
        self._registrar_mecanico("Probador Patente Falsa", "+56900900017", tipo="probador_de_autos")
        resp = self._mandar_texto("+56900900017", "ZZZZ99 vuelta corta sin novedad")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "sugerencia_sin_ot")

    def test_varias_pruebas_de_ruta_de_la_misma_ot_se_acumulan_sin_pisarse(self):
        self._registrar_mecanico("Probador Cuatro", "+56900900013", tipo="probador_de_autos")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PROB004")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        self._mandar_texto("+56900900013", f"ot {orden['id']} primera prueba, ruido en el eje delantero")
        self._mandar_texto("+56900900013", f"ot {orden['id']} segunda prueba post reparación, sin ruido")

        pruebas = self._pruebas_ruta_de(orden["id"])
        self.assertEqual(len(pruebas), 2)
        textos = {p["texto"] for p in pruebas}
        self.assertTrue(any("primera prueba" in t for t in textos))
        self.assertTrue(any("segunda prueba" in t for t in textos))

    def test_prueba_de_ruta_se_puede_agregar_tambien_desde_la_web(self):
        # El mismo destino (tabla pruebas_ruta) se llena desde el
        # formulario de la pestaña "Pruebas de ruta" en Operativo, no solo
        # desde WhatsApp -- ver POST /api/operativo/ordenes/<id>/pruebas-ruta.
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PROB005")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/pruebas-ruta",
            json={"texto": "Prueba cargada manualmente desde el sistema"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        self.assertEqual(creada["origen"], "web")
        self.assertIsNone(creada["foto_id"])

        pruebas = self._pruebas_ruta_de(orden["id"])
        self.assertEqual(len(pruebas), 1)

        # Y se puede eliminar.
        resp_del = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/pruebas-ruta/{creada['id']}")
        self.assertEqual(resp_del.status_code, 200)
        self.assertEqual(len(self._pruebas_ruta_de(orden["id"])), 0)

    def test_prueba_de_ruta_sin_texto_es_rechazada(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PROB006")
        orden = self._crear_orden(cliente["id"], equipo["id"])
        resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/pruebas-ruta", json={"texto": "  "})
        self.assertEqual(resp.status_code, 400)


class TestPerfilEncargadoPreEntrega(unittest.TestCase):
    """Perfil 'encargado_pre_entrega' (ver migración 30 / agente_mecanicos.tipo):
    mismo comportamiento que 'probador_de_autos' (ver TestPerfilProbadorDeAutos
    arriba), pero lo que manda se guarda en su propia bitácora (tabla
    pre_entrega), pensada para dejar registrada la última revisión antes de
    entregarle el vehículo al cliente."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Pre Entrega"):
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

    def _registrar_mecanico(self, nombre, telefono, tipo="encargado_pre_entrega"):
        resp = self.client.post(
            "/api/agente/mecanicos", json={"nombre": nombre, "telefono": telefono, "tipo": tipo}
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _subir_foto(self, telefono, texto=""):
        data = {"telefono": telefono, "foto": (io.BytesIO(_imagen_jpeg_valida()), "foto.jpg")}
        if texto:
            data["texto"] = texto
        return self.client.post("/api/agente/simular-mensaje-foto", data=data, content_type="multipart/form-data")

    def _mandar_texto(self, telefono, texto):
        return self.client.post("/api/agente/simular-mensaje", json={"telefono": telefono, "texto": texto})

    def _pre_entrega_de(self, orden_id):
        resp = self.client.get(f"/api/operativo/ordenes/{orden_id}")
        return resp.get_json()["pre_entrega"]

    def test_encargado_manda_solo_texto_con_ot_queda_en_bitacora_de_pre_entrega(self):
        self._registrar_mecanico("Encargado Uno", "+56900900020")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT001")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto(
            "+56900900020", f"ot {orden['id']} lista para la entrega\nrevisada y lavada"
        )
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "pre_entrega_registrada")
        self.assertEqual(data["ot_id"], orden["id"])

        entradas = self._pre_entrega_de(orden["id"])
        self.assertEqual(len(entradas), 1)
        self.assertIn("revisada y lavada", entradas[0]["texto"])
        self.assertEqual(entradas[0]["autor"], "ENCARGADO UNO")
        self.assertEqual(entradas[0]["origen"], "whatsapp")
        self.assertIsNone(entradas[0]["foto_id"])

        # NO debe quedar en las otras bitácoras -- es un destino distinto.
        sugerencias = self.client.get(f"/api/ordenes/{orden['id']}/sugerencias-diagnostico").get_json()
        self.assertEqual(len(sugerencias), 0)
        orden_actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(len(orden_actual["pruebas_ruta"]), 0)
        self.assertEqual(orden_actual.get("diagnostico_tecnico") or "", "")

    def test_encargado_manda_texto_con_patente_en_vez_de_numero_de_ot(self):
        self._registrar_mecanico("Encargado Patente", "+56900900021")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "RPYZ70")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto("+56900900021", "RPYZ70 lista para la entrega, revisada y lavada")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "pre_entrega_registrada")
        self.assertEqual(data["ot_id"], orden["id"])

    def test_encargado_manda_foto_con_comentario_queda_en_bitacora_vinculada_a_la_foto(self):
        self._registrar_mecanico("Encargado Foto", "+56900900022")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT002")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "otro", "patente": ""}):
            resp = self._subir_foto(
                "+56900900022", texto=f"ot {orden['id']} auto lavado y revisado, listo"
            )

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "pre_entrega_registrada")

        entradas = self._pre_entrega_de(orden["id"])
        self.assertEqual(len(entradas), 1)
        self.assertIn("auto lavado y revisado", entradas[0]["texto"])
        self.assertIsNotNone(entradas[0]["foto_id"])

        fotos = self.client.get(f"/api/operativo/ordenes/{orden['id']}/fotos").get_json()["fotos"]
        self.assertEqual(len(fotos), 1)

    def test_encargado_manda_texto_sin_ot_pide_el_numero(self):
        self._registrar_mecanico("Encargado Sin OT", "+56900900023")
        resp = self._mandar_texto("+56900900023", "listo para entregar")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "sugerencia_sin_ot")
        self.assertIn("pre entrega", data["respuesta_texto"].lower())

    def test_varias_entradas_de_pre_entrega_de_la_misma_ot_se_acumulan_sin_pisarse(self):
        self._registrar_mecanico("Encargado Varias", "+56900900024")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT003")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        self._mandar_texto("+56900900024", f"ot {orden['id']} primera revisión, falta lavado")
        self._mandar_texto("+56900900024", f"ot {orden['id']} lista, lavada y revisada")

        entradas = self._pre_entrega_de(orden["id"])
        self.assertEqual(len(entradas), 2)
        textos = {p["texto"] for p in entradas}
        self.assertTrue(any("primera revisión" in t for t in textos))
        self.assertTrue(any("lavada y revisada" in t for t in textos))

    def test_pre_entrega_se_puede_agregar_tambien_desde_la_web(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT004")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/pre-entrega",
            json={"texto": "Revisión cargada manualmente desde el sistema"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        self.assertEqual(creada["origen"], "web")
        self.assertIsNone(creada["foto_id"])

        entradas = self._pre_entrega_de(orden["id"])
        self.assertEqual(len(entradas), 1)

        # Y se puede eliminar.
        resp_del = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/pre-entrega/{creada['id']}")
        self.assertEqual(resp_del.status_code, 200)
        self.assertEqual(len(self._pre_entrega_de(orden["id"])), 0)

    def test_pre_entrega_sin_texto_es_rechazada(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT005")
        orden = self._crear_orden(cliente["id"], equipo["id"])
        resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/pre-entrega", json={"texto": "  "})
        self.assertEqual(resp.status_code, 400)

    def _estado_de(self, orden_id):
        return self.client.get(f"/api/operativo/ordenes/{orden_id}").get_json()["estado"]

    def test_confirmacion_explicita_mueve_la_ot_a_listo(self):
        # Pedido del taller: "solo en el caso de que se diga explícitamente
        # que está listo para la entrega, la orden de trabajo se debe mover
        # a 'listo para la entrega'".
        self._registrar_mecanico("Encargado Confirma", "+56900900030")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT010")
        orden = self._crear_orden(cliente["id"], equipo["id"])
        self.assertEqual(self._estado_de(orden["id"]), "recibido")

        resp = self._mandar_texto("+56900900030", f"ot {orden['id']} listo para la entrega")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "listo")
        self.assertIn("Listo para la entrega", data["respuesta_texto"])
        self.assertEqual(self._estado_de(orden["id"]), "listo")

    def test_mensaje_ambiguo_no_mueve_el_estado_y_avisa(self):
        # "si hubiese alguna ambigüedad, debe preguntar y ante la duda
        # dejarla sin movimiento" -- un comentario normal de revisión, sin
        # la frase explícita, no debe mover nada, y la respuesta tiene que
        # explicar por qué no se movió.
        self._registrar_mecanico("Encargado Ambiguo", "+56900900031")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT011")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto("+56900900031", f"ot {orden['id']} revisada y lavada")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "")
        self.assertIn("No moví la OT", data["respuesta_texto"])
        self.assertEqual(self._estado_de(orden["id"]), "recibido")

    def test_frase_negada_no_mueve_el_estado(self):
        # "no está lista para la entrega" trae las palabras clave pero
        # negadas -- tiene que quedar exactamente igual que un mensaje
        # ambiguo (sin movimiento).
        self._registrar_mecanico("Encargado Negado", "+56900900032")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT012")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._mandar_texto(
            "+56900900032", f"ot {orden['id']} no esta lista para la entrega, falta el parachoques"
        )
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "")
        self.assertEqual(self._estado_de(orden["id"]), "recibido")

    def test_ot_ya_lista_se_mantiene_sin_error_ante_nueva_confirmacion(self):
        # Este perfil NUNCA puede sacar una OT de "listo" -- ni por
        # accidente ni porque llegue otra confirmación después. Se prueba
        # mandando dos mensajes de confirmación seguidos: el primero mueve
        # la OT, el segundo no debe romper nada ni "revertir" el estado.
        self._registrar_mecanico("Encargado Repite", "+56900900033")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT013")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        self._mandar_texto("+56900900033", f"ot {orden['id']} listo para la entrega")
        self.assertEqual(self._estado_de(orden["id"]), "listo")

        resp = self._mandar_texto("+56900900033", f"ot {orden['id']} lista para entregar de nuevo")
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "")
        self.assertIn("ya estaba marcada", data["respuesta_texto"])
        self.assertEqual(self._estado_de(orden["id"]), "listo")

    def test_confirmacion_por_foto_tambien_mueve_la_ot_a_listo(self):
        self._registrar_mecanico("Encargado Foto Listo", "+56900900034")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT014")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "otro", "patente": ""}):
            resp = self._subir_foto(
                "+56900900034", texto=f"ot {orden['id']} lista para la entrega"
            )
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "listo")
        self.assertEqual(self._estado_de(orden["id"]), "listo")

    def test_confirmacion_por_foto_ambigua_no_mueve_el_estado(self):
        self._registrar_mecanico("Encargado Foto Ambiguo", "+56900900035")
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "PENT015")
        orden = self._crear_orden(cliente["id"], equipo["id"])

        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
                           return_value={"tipo": "otro", "patente": ""}):
            resp = self._subir_foto(
                "+56900900035", texto=f"ot {orden['id']} auto lavado y revisado"
            )
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["estado_nuevo"], "")
        self.assertEqual(self._estado_de(orden["id"]), "recibido")


class TestDetectorTextoConfirmaListoParaEntrega(unittest.TestCase):
    """Pruebas directas sobre _agente_texto_confirma_listo_para_entrega --
    el detector determinístico (sin IA) que decide si un mensaje confirma
    EXPLÍCITAMENTE que el vehículo quedó listo para la entrega. Se prueba
    aparte de los tests de integración de arriba porque acá interesa cubrir
    muchas variantes de texto rápido, sin el costo de armar una OT por caso."""

    def test_frases_claras_confirman(self):
        frases = [
            "listo para la entrega",
            "Lista para la entrega",
            "listo para entregar",
            "lista para entregar",
            "ya se puede retirar, esta listo",
            "auto listo, puede pasar a retirarlo",
            "LISTO PARA LA ENTREGA",
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                self.assertTrue(taller_app._agente_texto_confirma_listo_para_entrega(frase))

    def test_frases_ambiguas_no_confirman(self):
        frases = [
            "revisada y lavada",
            "falta terminar el detallado",
            "esta listo",  # "listo" solo, sin mención de entrega/retiro
            "se entrego la cotizacion al cliente",
            "",
            "   ",
            None,
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                self.assertFalse(taller_app._agente_texto_confirma_listo_para_entrega(frase))

    def test_frases_negadas_no_confirman(self):
        frases = [
            "no esta lista para la entrega",
            "todavia no queda lista para la entrega",
            "falta lavado para que quede lista para la entrega",
            "sin lavar, no esta lista para la entrega",
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                self.assertFalse(taller_app._agente_texto_confirma_listo_para_entrega(frase))

    def test_negaciones_e_incertidumbre_menos_directas_no_confirman(self):
        # Casos encontrados en una auditoría posterior: negaciones con
        # "nunca"/"jamás"/"ni", incertidumbre con "dudo"/"duda"/"puede
        # que"/"capaz que"/"casi", preguntas (propias o reenviadas del
        # cliente) y condicionales con "cuando" -- ninguno de estos es una
        # confirmación explícita, así que el detector NO debe moverlos.
        frases = [
            "nunca va a estar listo para la entrega a este paso",
            "jamas va a estar listo para la entrega",
            "casi queda listo para la entrega, manana seguro",
            "ni de local queda listo para la entrega hoy",
            "dudo que este listo para la entrega hoy",
            "puede que quede listo para la entrega en la tarde",
            "capaz que quede listo para la entrega manana",
            "esta listo para la entrega?",
            "el cliente pregunto si esta listo para la entrega",
            "cuando este listo aviso para la entrega",
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                self.assertFalse(taller_app._agente_texto_confirma_listo_para_entrega(frase))

    def test_confirmaciones_con_puede_siguen_funcionando(self):
        # "puede" como palabra suelta (permiso/capacidad, no incertidumbre)
        # no debe bloquearse -- solo la frase literal "puede que"/"capaz
        # que" cuenta como incertidumbre (ver test anterior).
        frases = [
            "ya se puede retirar, esta listo",
            "auto listo, puede pasar a retirarlo",
        ]
        for frase in frases:
            with self.subTest(frase=frase):
                self.assertTrue(taller_app._agente_texto_confirma_listo_para_entrega(frase))


# ---------------------------------------------------------------------------
# Firma de Twilio detrás del proxy de Railway
# ---------------------------------------------------------------------------

import base64   # noqa: E402
import hashlib  # noqa: E402
import hmac     # noqa: E402


def _firma_twilio(url, params, token):
    """La firma tal como la calcula Twilio: HMAC-SHA1 del AUTH_TOKEN sobre la
    URL completa + cada par clave/valor del POST en orden alfabético, en
    base64. Se calcula acá a mano, sin usar el código de la app, para que el
    test compruebe el algoritmo y no se limite a repetirlo."""
    base = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")


class TestFirmaTwilio(unittest.TestCase):
    """El webhook está detrás del proxy de Railway: Flask recibe la petición
    como http:// y con el host interno, pero Twilio firmó la https:// pública.

    Si `request.url` no reflejara la URL original, el HMAC no coincidiría
    nunca y el webhook rechazaría TODO el tráfico legítimo con 403 — sin más
    síntoma que "no se pudo verificar". Lo que lo evita es el ProxyFix sobre
    app.wsgi_app; estos tests son los que fallan si alguien lo saca."""

    TOKEN = "token_de_prueba_para_firmar_12345"
    HOST = "taller.up.railway.app"
    RUTA = "/webhook/whatsapp"
    PARAMS = {
        "From": "whatsapp:+56911111111",
        "Body": "OT 8112 lista para entregar",
        "MessageSid": "SM0123456789abcdef",
    }

    @property
    def url_publica(self):
        return f"https://{self.HOST}{self.RUTA}"

    def setUp(self):
        taller_app.app.config["TESTING"] = True
        self.client = taller_app.app.test_client()
        self._token_original = taller_app.TWILIO_AUTH_TOKEN
        taller_app.TWILIO_AUTH_TOKEN = self.TOKEN
        self.addCleanup(
            lambda: setattr(taller_app, "TWILIO_AUTH_TOKEN", self._token_original))

    def _headers_proxy(self, extra=None):
        h = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": self.HOST}
        h.update(extra or {})
        return h

    # -- el caso que importa --------------------------------------------------

    def test_firma_de_la_url_https_valida_en_una_peticion_http(self):
        """El escenario real de Railway: Twilio firma la https pública y la
        petición llega a Flask como http."""
        firma = _firma_twilio(self.url_publica, self.PARAMS, self.TOKEN)
        resp = self.client.post(
            self.RUTA, data=self.PARAMS,
            headers=self._headers_proxy({"X-Twilio-Signature": firma}),
        )
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))

    def test_sin_los_headers_del_proxy_la_misma_firma_se_rechaza(self):
        """Es el bug que ProxyFix evita: sin los headers, `request.url` es
        http://localhost y el HMAC se calcula sobre otra cosa. Este test
        documenta el modo de falla; si algún día pasa, ProxyFix se fue."""
        firma = _firma_twilio(self.url_publica, self.PARAMS, self.TOKEN)
        resp = self.client.post(
            self.RUTA, data=self.PARAMS, headers={"X-Twilio-Signature": firma})
        self.assertEqual(403, resp.status_code)

    # -- los rechazos que tienen que seguir funcionando -----------------------

    def test_sin_firma_da_403(self):
        resp = self.client.post(self.RUTA, data=self.PARAMS,
                                headers=self._headers_proxy())
        self.assertEqual(403, resp.status_code)

    def test_firma_invalida_da_403(self):
        resp = self.client.post(
            self.RUTA, data=self.PARAMS,
            headers=self._headers_proxy({"X-Twilio-Signature": "firma-inventada"}),
        )
        self.assertEqual(403, resp.status_code)

    def test_un_parametro_alterado_invalida_la_firma(self):
        """La firma cubre los parámetros, no solo la URL: si alguien cambia el
        cuerpo, deja de validar."""
        firma = _firma_twilio(self.url_publica, self.PARAMS, self.TOKEN)
        alterado = dict(self.PARAMS, Body="OT 9999 lista para entregar")
        resp = self.client.post(
            self.RUTA, data=alterado,
            headers=self._headers_proxy({"X-Twilio-Signature": firma}),
        )
        self.assertEqual(403, resp.status_code)

    def test_una_firma_de_otro_host_no_sirve(self):
        """Evita que una firma capturada de otro despliegue valga acá."""
        firma = _firma_twilio(
            f"https://otro-host.example{self.RUTA}", self.PARAMS, self.TOKEN)
        resp = self.client.post(
            self.RUTA, data=self.PARAMS,
            headers=self._headers_proxy({"X-Twilio-Signature": firma}),
        )
        self.assertEqual(403, resp.status_code)

    # -- el log, que es lo que hacía invisible el problema --------------------

    def test_el_rechazo_registra_la_url_usada_para_el_hmac(self):
        firma = _firma_twilio(self.url_publica, self.PARAMS, self.TOKEN)
        with self.assertLogs(taller_app.logger, level="WARNING") as capturado:
            self.client.post(self.RUTA, data=self.PARAMS,
                             headers={"X-Twilio-Signature": firma})
        salida = "\n".join(capturado.output)
        self.assertIn("http://localhost/webhook/whatsapp", salida,
                      "el log tiene que decir con qué URL se calculó el HMAC")
        self.assertIn("(ausente)", salida,
                      "y que los headers del proxy no vinieron")

    def test_el_log_no_filtra_el_auth_token(self):
        with self.assertLogs(taller_app.logger, level="WARNING") as capturado:
            self.client.post(self.RUTA, data=self.PARAMS,
                             headers=self._headers_proxy(
                                 {"X-Twilio-Signature": "firma-inventada"}))
        self.assertNotIn(self.TOKEN, "\n".join(capturado.output))


class TestScriptSimuladorWhatsapp(unittest.TestCase):
    """`scripts/simular_whatsapp.py` es la única forma de ejercitar el webhook
    de punta a punta mientras Twilio bloquee el sandbox en cuentas trial.

    Si el script y el webhook se separan —otro conjunto de parámetros, otro
    orden al concatenar— el script empezaría a dar 403 y se leería como "la
    firma está mal en el servidor", que es el diagnóstico equivocado."""

    TOKEN = "token_de_prueba_para_el_script_98765"
    HOST = "taller.up.railway.app"

    def setUp(self):
        import importlib.util
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ruta = os.path.join(raiz, "scripts", "simular_whatsapp.py")
        spec = importlib.util.spec_from_file_location("simular_whatsapp", ruta)
        self.sim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sim)

        taller_app.app.config["TESTING"] = True
        self.client = taller_app.app.test_client()
        self._token_original = taller_app.TWILIO_AUTH_TOKEN
        taller_app.TWILIO_AUTH_TOKEN = self.TOKEN
        self.addCleanup(
            lambda: setattr(taller_app, "TWILIO_AUTH_TOKEN", self._token_original))

    def test_la_firma_del_script_la_acepta_el_webhook(self):
        url = f"https://{self.HOST}/webhook/whatsapp"
        params = self.sim.construir_params("OT 8112 lista", "+56911111111", "+14155238886")
        firma = self.sim.firmar(url, params, self.TOKEN)
        resp = self.client.post("/webhook/whatsapp", data=params, headers={
            "X-Twilio-Signature": firma,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": self.HOST,
        })
        self.assertEqual(200, resp.status_code, resp.get_data(as_text=True))

    def test_manda_los_campos_que_manda_twilio(self):
        params = self.sim.construir_params("hola", "+56911111111", "+14155238886")
        self.assertEqual(
            {"Body", "From", "To", "MessageSid", "AccountSid", "NumMedia"},
            set(params),
        )
        self.assertTrue(params["From"].startswith("whatsapp:"))
        self.assertTrue(params["MessageSid"].startswith("SM"))
        self.assertTrue(params["AccountSid"].startswith("AC"))

    def test_el_script_no_contiene_ni_imprime_credenciales(self):
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(raiz, "scripts", "simular_whatsapp.py"),
                  encoding="utf-8") as fh:
            fuente = fh.read()
        # El token solo se lee del entorno y se usa para firmar; nunca se
        # imprime. Lo único que se muestra de él es su largo.
        self.assertNotIn("print(token", fuente)
        self.assertNotIn('print(f"  Token        : {token', fuente)
        self.assertIn("no se imprime", fuente)


# ---------------------------------------------------------------------------
# Cloud API de Meta: el segundo proveedor de WhatsApp
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402


class BaseMeta(unittest.TestCase):
    """Los dos proveedores conviven detrás de WHATSAPP_PROVEEDOR. Estos tests
    corren con 'meta' activo; TestFirmaTwilio cubre que el camino de Twilio
    siga intacto."""

    APP_SECRET = "app_secret_de_prueba_para_firmar_98765"
    VERIFY = "verify_token_de_prueba"
    TELEFONO = "+56946596850"

    def setUp(self):
        taller_app.app.config["TESTING"] = True
        self.client = taller_app.app.test_client()
        self._previo = {
            "proveedor": os.environ.get("WHATSAPP_PROVEEDOR"),
            "secret": taller_app.META_WA_APP_SECRET,
            "verify": taller_app.META_WA_VERIFY_TOKEN,
        }
        os.environ["WHATSAPP_PROVEEDOR"] = "meta"
        taller_app.META_WA_APP_SECRET = self.APP_SECRET
        taller_app.META_WA_VERIFY_TOKEN = self.VERIFY
        self.addCleanup(self._restaurar)

        self.db = taller_app._conectar_db()
        self.db.execute("DELETE FROM agente_mecanicos")
        self.db.commit()

    def _restaurar(self):
        if self._previo["proveedor"] is None:
            os.environ.pop("WHATSAPP_PROVEEDOR", None)
        else:
            os.environ["WHATSAPP_PROVEEDOR"] = self._previo["proveedor"]
        taller_app.META_WA_APP_SECRET = self._previo["secret"]
        taller_app.META_WA_VERIFY_TOKEN = self._previo["verify"]
        self.db.execute("DELETE FROM agente_mecanicos")
        self.db.commit()
        self.db.close()

    def payload_texto(self, cuerpo, de=None):
        de = (de or self.TELEFONO).lstrip("+")
        return {
            "object": "whatsapp_business_account",
            "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1", "phone_number_id": "2"},
                "contacts": [{"profile": {"name": "X"}, "wa_id": de}],
                "messages": [{"from": de, "id": "wamid.AAA", "timestamp": "1749416383",
                              "type": "text", "text": {"body": cuerpo}}],
            }}]}],
        }

    def enviar(self, payload, firmar=True):
        cuerpo = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if firmar:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
                self.APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
        return self.client.post("/webhook/whatsapp", data=cuerpo, headers=headers)


class TestMetaFirma(BaseMeta):

    def test_firma_valida_sobre_el_cuerpo_crudo(self):
        self.assertEqual(200, self.enviar(self.payload_texto("hola")).status_code)

    def test_sin_firma_da_403(self):
        self.assertEqual(403, self.enviar(self.payload_texto("hola"), firmar=False).status_code)

    def test_cuerpo_alterado_invalida_la_firma(self):
        """La firma cubre los bytes exactos: si alguien cambia el payload
        despues de firmarlo, deja de validar."""
        cuerpo = _json.dumps(self.payload_texto("hola")).encode()
        firma = "sha256=" + hmac.new(self.APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
        otro = _json.dumps(self.payload_texto("otra cosa")).encode()
        resp = self.client.post("/webhook/whatsapp", data=otro, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": firma})
        self.assertEqual(403, resp.status_code)

    def test_firma_sin_el_prefijo_sha256_se_rechaza(self):
        cuerpo = _json.dumps(self.payload_texto("hola")).encode()
        crudo = hmac.new(self.APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
        resp = self.client.post("/webhook/whatsapp", data=cuerpo, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": crudo})
        self.assertEqual(403, resp.status_code)

    def test_el_log_del_rechazo_no_filtra_el_app_secret(self):
        with self.assertLogs(taller_app.logger, level="WARNING") as cap:
            self.client.post("/webhook/whatsapp",
                             data=_json.dumps(self.payload_texto("hola")).encode(),
                             headers={"Content-Type": "application/json",
                                      "X-Hub-Signature-256": "sha256=malo"})
        salida = "\n".join(cap.output)
        self.assertNotIn(self.APP_SECRET, salida)
        self.assertIn("bytes del cuerpo", salida,
                      "el log debe decir cuanto cuerpo se firmo: si es 0, algo lo consumio antes")


class TestMetaVerificacion(BaseMeta):
    """El GET inicial que hace Meta al configurar el webhook."""

    def test_devuelve_el_challenge_con_el_token_correcto(self):
        resp = self.client.get("/webhook/whatsapp", query_string={
            "hub.mode": "subscribe", "hub.verify_token": self.VERIFY,
            "hub.challenge": "424242"})
        self.assertEqual(200, resp.status_code)
        self.assertEqual("424242", resp.get_data(as_text=True))

    def test_token_incorrecto_da_403(self):
        resp = self.client.get("/webhook/whatsapp", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "otro", "hub.challenge": "1"})
        self.assertEqual(403, resp.status_code)

    def test_con_twilio_activo_el_get_no_existe(self):
        os.environ["WHATSAPP_PROVEEDOR"] = "twilio"
        resp = self.client.get("/webhook/whatsapp", query_string={
            "hub.mode": "subscribe", "hub.verify_token": self.VERIFY, "hub.challenge": "1"})
        self.assertEqual(405, resp.status_code)


class TestMetaPayloadsQueNoSonMensajes(BaseMeta):
    """Meta manda muchos eventos que no son mensajes. Ignorarlos con 200 sin
    romperse es lo que evita que reintente en loop."""

    def test_evento_de_estado_se_ignora_con_200(self):
        payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [
            {"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "1", "phone_number_id": "2"},
                "statuses": [{"id": "wamid.X", "status": "delivered",
                              "timestamp": "1750263773", "recipient_id": "56946596850"}],
            }}]}]}
        self.assertEqual(200, self.enviar(payload).status_code)

    def test_payload_truncado_no_revienta(self):
        for payload in ({}, {"object": "x"}, {"entry": []}, {"entry": [{"changes": []}]},
                        {"entry": [{"changes": [{"value": {}}]}]}):
            with self.subTest(payload=payload):
                self.assertEqual(200, self.enviar(payload).status_code)

    def test_un_tipo_de_mensaje_desconocido_no_revienta(self):
        payload = self.payload_texto("x")
        msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        msg["type"] = "sticker"
        msg.pop("text")
        self.assertEqual(200, self.enviar(payload).status_code)


class TestMetaTelefonoSinMas(BaseMeta):
    """El caso que rompia el reconocimiento del mecanico.

    Meta manda el numero SIN '+' ('56946596850'); el taller lo registra CON
    '+'. Antes _normalizar_telefono conservaba el signo, asi que no calzaban y
    el agente respondia "este numero no esta registrado" -- un error que
    parece del agente cuando es del formato."""

    def registrar(self, telefono_guardado):
        self.db.execute(
            "INSERT INTO agente_mecanicos (nombre, telefono, activo, tipo, creado_en)"
            " VALUES ('FRANCO', ?, 1, 'completo', '2026-01-01T00:00:00')",
            (telefono_guardado,))
        self.db.commit()

    def buscar(self, crudo_entrante):
        norm = taller_app._normalizar_telefono(crudo_entrante)
        return self.db.execute(
            "SELECT id FROM agente_mecanicos WHERE telefono = ? AND activo = 1",
            (norm,)).fetchone()

    def test_registrado_con_mas_lo_encuentra_lo_que_manda_meta(self):
        # Asi lo deja el endpoint de registro, que normaliza al guardar.
        self.registrar(taller_app._normalizar_telefono("+56946596850"))
        self.assertIsNotNone(self.buscar("56946596850"),
                             "Meta manda sin '+' y tiene que calzar igual")

    def test_las_tres_formas_de_escribirlo_dan_lo_mismo(self):
        formas = ["+56946596850", "56946596850", "whatsapp:+56946596850",
                  "+56 9 4659 6850"]
        normalizadas = {taller_app._normalizar_telefono(f) for f in formas}
        self.assertEqual({"56946596850"}, normalizadas)

    def test_una_fila_vieja_con_mas_la_arregla_la_migracion(self):
        """Las filas guardadas antes del cambio tienen el '+' adentro. Sin la
        migracion de init_db dejarian de encontrarse."""
        self.registrar("+56946596850")
        self.assertIsNone(self.buscar("56946596850"), "antes de migrar no calza")
        self.db.execute(
            "UPDATE agente_mecanicos SET telefono = REPLACE(telefono, '+', '')"
            " WHERE telefono LIKE '%+%'")
        self.db.commit()
        self.assertIsNotNone(self.buscar("56946596850"), "despues de migrar si")


class TestMetaEntregaAsincrona(BaseMeta):
    """Con Meta la respuesta NO viaja en el HTTP del webhook: se manda aparte
    por la Graph API."""

    def test_el_webhook_responde_200_con_cuerpo_vacio(self):
        resp = self.enviar(self.payload_texto("hola"))
        self.assertEqual(200, resp.status_code)
        self.assertEqual("", resp.get_data(as_text=True))

    def test_manda_la_respuesta_a_la_graph_api(self):
        llamadas = []

        class RespFalsa:
            status_code = 200
            text = "{}"

        def fake_post(url, **kw):
            llamadas.append((url, kw))
            return RespFalsa()

        taller_app.META_WA_ACCESS_TOKEN = "token-de-prueba"
        taller_app.META_WA_PHONE_NUMBER_ID = "123456"
        original = taller_app.requests.post
        taller_app.requests.post = fake_post
        try:
            self.enviar(self.payload_texto("hola"))
        finally:
            taller_app.requests.post = original
            taller_app.META_WA_ACCESS_TOKEN = ""
            taller_app.META_WA_PHONE_NUMBER_ID = ""

        self.assertEqual(1, len(llamadas), "debia salir una llamada a la Graph API")
        url, kw = llamadas[0]
        self.assertIn("/123456/messages", url)
        self.assertIn(taller_app.META_WA_API_VERSION, url)
        cuerpo = kw["json"]
        self.assertEqual("whatsapp", cuerpo["messaging_product"])
        self.assertEqual("text", cuerpo["type"])
        self.assertEqual("56946596850", cuerpo["to"], "el destinatario sin '+'")
        self.assertTrue(cuerpo["text"]["body"])
        self.assertIn("Bearer token-de-prueba", kw["headers"]["Authorization"])

    def test_si_falla_la_graph_api_igual_responde_200(self):
        """Un no-200 haria que Meta reintente el webhook en loop, y el trabajo
        pedido ya se aplico antes de intentar responder."""
        def fake_post(url, **kw):
            raise taller_app.requests.RequestException("sin red")

        taller_app.META_WA_ACCESS_TOKEN = "token-de-prueba"
        taller_app.META_WA_PHONE_NUMBER_ID = "123456"
        original = taller_app.requests.post
        taller_app.requests.post = fake_post
        try:
            resp = self.enviar(self.payload_texto("hola"))
        finally:
            taller_app.requests.post = original
            taller_app.META_WA_ACCESS_TOKEN = ""
            taller_app.META_WA_PHONE_NUMBER_ID = ""
        self.assertEqual(200, resp.status_code)


class TestConvivenciaProveedores(unittest.TestCase):
    """El requisito central: poder volver a Twilio cambiando una variable."""

    def test_el_defecto_es_twilio(self):
        previo = os.environ.pop("WHATSAPP_PROVEEDOR", None)
        try:
            self.assertEqual("twilio", taller_app.whatsapp_proveedor())
        finally:
            if previo is not None:
                os.environ["WHATSAPP_PROVEEDOR"] = previo

    def test_un_valor_desconocido_cae_a_twilio(self):
        """Quedarse sin webhook por un typo seria peor que seguir con Twilio."""
        previo = os.environ.get("WHATSAPP_PROVEEDOR")
        os.environ["WHATSAPP_PROVEEDOR"] = "metaa"
        try:
            self.assertEqual("twilio", taller_app.whatsapp_proveedor())
        finally:
            if previo is None:
                os.environ.pop("WHATSAPP_PROVEEDOR", None)
            else:
                os.environ["WHATSAPP_PROVEEDOR"] = previo

    def test_los_dos_adaptadores_tienen_las_mismas_funciones(self):
        # Las cuatro del transporte más 'secreto' y 'falta_secreto': sin el
        # secreto del proveedor activo el webhook no procesa nada (503).
        esperadas = {"validar_firma", "extraer_mensaje", "descargar_media", "entregar",
                     "secreto", "falta_secreto"}
        for nombre, adaptador in taller_app.WHATSAPP_ADAPTADORES.items():
            with self.subTest(proveedor=nombre):
                self.assertEqual(esperadas, set(adaptador))

    def test_el_codigo_de_twilio_sigue_estando(self):
        """Requisito explicito: no borrar el camino de Twilio."""
        for fn in ("_validar_firma_twilio", "_twilio_extraer_mensaje",
                   "_twilio_descargar_media", "_twilio_entregar"):
            self.assertTrue(hasattr(taller_app, fn), f"falta {fn}")


# ---------------------------------------------------------------------------
# Sin el secreto del proveedor activo, el webhook NO procesa nada (503).
# La versión anterior aceptaba el tráfico sin validar la firma cuando faltaba
# el secreto; eso no se trajo de vuelta (hallazgo #15).
# ---------------------------------------------------------------------------

class TestSinSecretoNoSeProcesa(BaseMeta):

    def test_meta_sin_app_secret_da_503_y_no_llama_al_agente(self):
        taller_app.META_WA_APP_SECRET = ""
        with patch.object(taller_app, "_agente_procesar_mensaje") as agente:
            resp = self.enviar(self.payload_texto("OT 8112 lista"), firmar=False)
        self.assertEqual(503, resp.status_code)
        agente.assert_not_called()

    def test_meta_con_app_secret_y_sin_firma_da_403(self):
        with patch.object(taller_app, "_agente_procesar_mensaje") as agente:
            resp = self.enviar(self.payload_texto("OT 8112 lista"), firmar=False)
        self.assertEqual(403, resp.status_code)
        agente.assert_not_called()

    def test_twilio_sin_auth_token_da_503_y_no_llama_al_agente(self):
        os.environ["WHATSAPP_PROVEEDOR"] = "twilio"
        previo = taller_app.TWILIO_AUTH_TOKEN
        taller_app.TWILIO_AUTH_TOKEN = ""
        self.addCleanup(lambda: setattr(taller_app, "TWILIO_AUTH_TOKEN", previo))
        with patch.object(taller_app, "_agente_procesar_mensaje") as agente:
            resp = self.client.post("/webhook/whatsapp", data={"From": "whatsapp:+56946596850", "Body": "hola"})
        self.assertEqual(503, resp.status_code)
        self.assertIn("TWILIO_AUTH_TOKEN", resp.get_data(as_text=True))
        agente.assert_not_called()

    def test_la_verificacion_get_no_existe_con_twilio(self):
        os.environ["WHATSAPP_PROVEEDOR"] = "twilio"
        resp = self.client.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=x&hub.challenge=1")
        self.assertEqual(405, resp.status_code)


class TestMigracionTelefonosSinMas(unittest.TestCase):
    """init_db deja los teléfonos de agente_mecanicos sin '+', pero telefono es
    UNIQUE: si el mismo número ya está sin '+', esa fila no se toca. Un UPDATE
    a ciegas chocaría contra la llave y la app no arrancaría."""

    def setUp(self):
        self.db = taller_app._conectar_db()
        self.db.execute("DELETE FROM agente_mecanicos")
        self.db.commit()
        self.addCleanup(self._limpiar)

    def _limpiar(self):
        self.db.execute("DELETE FROM agente_mecanicos")
        self.db.commit()
        self.db.close()

    def _insertar(self, nombre, telefono):
        self.db.execute(
            "INSERT INTO agente_mecanicos (nombre, telefono, activo, creado_en) VALUES (?, ?, 1, '2026-10-13T00:00:00')",
            (nombre, telefono))
        self.db.commit()

    def _telefonos(self):
        return sorted(r[0] for r in self.db.execute("SELECT telefono FROM agente_mecanicos"))

    def test_quita_el_mas_y_no_se_cae_con_un_duplicado(self):
        self._insertar("UNO", "+56911111111")
        self._insertar("DOS", "+56922222222")
        self._insertar("DOS OTRA VEZ", "56922222222")
        taller_app.init_db()  # no debe lanzar IntegrityError
        self.assertEqual(["+56922222222", "56911111111", "56922222222"], self._telefonos())


if __name__ == "__main__":
    unittest.main()
