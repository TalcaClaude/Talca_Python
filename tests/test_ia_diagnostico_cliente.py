"""
Tests para /api/operativo/ia/diagnostico-cliente -- el endpoint que
autocompleta con IA el campo "Diagnóstico (resumen para el cliente)" a
medida que se escribe el diagnóstico técnico, desde la pestaña Diagnóstico
de una OT (ver operativo.js, listener de input sobre
opdi-diagnostico-tecnico, y _llamar_claude_generar_diagnostico_cliente en
app.py).

Cómo correrlos:
    cd tests && python3 -m unittest test_ia_diagnostico_cliente -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class TestIaDiagnosticoCliente(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin = taller_app.app.test_client()
        cls.admin.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_usuario_con_rol(self, username, rol):
        resp = self.admin.post(
            "/api/admin/usuarios",
            json={"username": username, "password": "123456", "rol": rol},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cliente = taller_app.app.test_client()
        cliente.post("/login", data={"username": username, "password": "123456"})
        return cliente

    def test_devuelve_vacio_sin_diagnostico_tecnico(self):
        # No llama a la IA si no hay nada que resumir todavía.
        with patch.object(taller_app, "_llamar_claude_generar_diagnostico_cliente") as mock_ia:
            resp = self.admin.post(
                "/api/operativo/ia/diagnostico-cliente",
                json={"diagnostico_tecnico": "", "sintoma": ""},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"diagnostico_cliente": ""})
        mock_ia.assert_not_called()

    def test_genera_resumen_con_ia_mockeada(self):
        texto_generado = "Se detectó desgaste en la caja de cambios; se recomienda reparación completa."
        with patch.object(taller_app, "_llamar_claude_generar_diagnostico_cliente", return_value=texto_generado) as mock_ia:
            resp = self.admin.post(
                "/api/operativo/ia/diagnostico-cliente",
                json={
                    "diagnostico_tecnico": "PATINA EN 2DA MARCHA, GOLPEA EN 5TA",
                    "sintoma": "EL AUTO NO ACELERA BIEN",
                },
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"diagnostico_cliente": texto_generado})
        mock_ia.assert_called_once_with("EL AUTO NO ACELERA BIEN", "PATINA EN 2DA MARCHA, GOLPEA EN 5TA")

    def test_error_de_ia_devuelve_400_no_500(self):
        with patch.object(
            taller_app, "_llamar_claude_generar_diagnostico_cliente",
            side_effect=RuntimeError("No se pudo contactar al servicio de IA: timeout"),
        ):
            resp = self.admin.post(
                "/api/operativo/ia/diagnostico-cliente",
                json={"diagnostico_tecnico": "algo"},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_requiere_rol_operativo_o_admin(self):
        cliente_bodega = self._crear_usuario_con_rol("bodega_iadiag", "bodega")
        resp = cliente_bodega.post(
            "/api/operativo/ia/diagnostico-cliente",
            json={"diagnostico_tecnico": "algo"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_rol_operativo_puede_usarlo(self):
        cliente_operativo = self._crear_usuario_con_rol("op_iadiag", "operativo")
        with patch.object(taller_app, "_llamar_claude_generar_diagnostico_cliente", return_value="Resumen ok."):
            resp = cliente_operativo.post(
                "/api/operativo/ia/diagnostico-cliente",
                json={"diagnostico_tecnico": "algo"},
            )
        self.assertEqual(resp.status_code, 200)

    def test_sin_anthropic_api_key_da_error_controlado(self):
        # Sin mockear _llamar_claude_generar_diagnostico_cliente: en el
        # entorno de tests no hay ANTHROPIC_API_KEY configurada, así que
        # debe fallar con un mensaje claro (400), nunca un 500 sin manejar.
        self.assertEqual(taller_app.ANTHROPIC_API_KEY, "")
        resp = self.admin.post(
            "/api/operativo/ia/diagnostico-cliente",
            json={"diagnostico_tecnico": "algo"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ANTHROPIC_API_KEY", resp.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
