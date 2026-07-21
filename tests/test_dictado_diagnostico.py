"""Tests del dictado de diagnóstico hablado directo desde la pestaña
Diagnóstico de una OT (ver /api/operativo/ia/dictado-diagnostico en
app.py) -- reusa _transcribir_audio_whatsapp (mismo camino que las notas
de voz de WhatsApp) y agrega _llamar_claude_extraer_diagnostico_dictado
para separar la transcripción en 'síntoma' y 'diagnóstico técnico'. Cubre:
  1. Camino feliz: transcribe y separa correctamente.
  2. Falla la transcripción (ej. sin OPENAI_API_KEY) -> error 400, nunca
     se llega a llamar a la separación de campos.
  3. Transcribe bien pero falla la separación en campos -> igual devuelve
     la transcripción cruda (para no perder el trabajo de haber grabado),
     con un 'aviso'.
  4. Falta el archivo de audio -> error 400.
  5. Exige rol operativo/admin.
"""
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_dictado_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


def _audio_falso():
    return io.BytesIO(b"contenido-de-audio-falso")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _dictar(self, contenido=None):
        data = {"audio": (contenido or _audio_falso(), "dictado.webm")}
        return self.client.post("/api/operativo/ia/dictado-diagnostico", data=data, content_type="multipart/form-data")


class TestDictadoDiagnostico(_Base):
    def test_camino_feliz_transcribe_y_separa_campos(self):
        campos = {"sintoma": "El auto tira hacia la derecha al frenar", "diagnostico_tecnico": "Pastillas de freno delanteras desgastadas de forma dispareja"}
        with patch.object(taller_app, "_transcribir_audio_whatsapp", return_value="el cliente dice que el auto tira a la derecha al frenar, revisé y las pastillas delanteras están desgastadas de forma dispareja"), \
             patch.object(taller_app, "_llamar_claude_extraer_diagnostico_dictado", return_value=campos):
            resp = self._dictar()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertIn("transcripcion", data)
        self.assertEqual(data["sintoma"], campos["sintoma"])
        self.assertEqual(data["diagnostico_tecnico"], campos["diagnostico_tecnico"])
        self.assertNotIn("aviso", data)

    def test_falla_transcripcion_devuelve_error_400(self):
        with patch.object(taller_app, "_transcribir_audio_whatsapp", side_effect=RuntimeError("No hay una clave de transcripción configurada")) as mock_transcribir, \
             patch.object(taller_app, "_llamar_claude_extraer_diagnostico_dictado") as mock_extraer:
            resp = self._dictar()
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("error", resp.get_json())
        mock_transcribir.assert_called_once()
        mock_extraer.assert_not_called()

    def test_falla_separacion_de_campos_igual_devuelve_transcripcion_con_aviso(self):
        with patch.object(taller_app, "_transcribir_audio_whatsapp", return_value="ruido raro en la suspensión delantera"), \
             patch.object(taller_app, "_llamar_claude_extraer_diagnostico_dictado", side_effect=RuntimeError("Falta la variable de entorno ANTHROPIC_API_KEY")):
            resp = self._dictar()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["transcripcion"], "ruido raro en la suspensión delantera")
        self.assertEqual(data["sintoma"], "")
        self.assertEqual(data["diagnostico_tecnico"], "")
        self.assertIn("aviso", data)

    def test_sin_archivo_de_audio_devuelve_error_400(self):
        resp = self.client.post("/api/operativo/ia/dictado-diagnostico", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)


class TestAccesoPorRolDictadoDiagnostico(_Base):
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

    def test_bodega_no_puede_usar_dictado(self):
        self._crear_usuario("bodega_dictado", "bodega")
        c = self._client_como("bodega_dictado")
        resp = c.post("/api/operativo/ia/dictado-diagnostico", data={"audio": (_audio_falso(), "d.webm")}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 403)

    def test_operativo_si_puede(self):
        self._crear_usuario("operativo_dictado", "operativo")
        c = self._client_como("operativo_dictado")
        with patch.object(taller_app, "_transcribir_audio_whatsapp", return_value="texto"), \
             patch.object(taller_app, "_llamar_claude_extraer_diagnostico_dictado", return_value={"sintoma": "", "diagnostico_tecnico": "texto"}):
            resp = c.post("/api/operativo/ia/dictado-diagnostico", data={"audio": (_audio_falso(), "d.webm")}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
