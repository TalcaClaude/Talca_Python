"""
Tests para los respaldos automáticos locales (cada pocos minutos, en el
mismo disco) agregados a app.py como complemento del respaldo diario por
correo -- ver la sección "Respaldos automáticos locales" en app.py.

Cubre: creación de una copia consistente (VACUUM INTO), rotación cuando se
supera el máximo de copias, restauración exitosa (los datos vuelven a como
estaban en la copia), rechazo de nombres con path traversal, y que los
endpoints exigen rol admin.

Cómo correrlos:
    cd tests && python3 -m unittest test_respaldos_locales -v
"""

import os
import sys
import tempfile
import time
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class TestRespaldosLocales(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        # Deja la carpeta de respaldos locales limpia antes de cada test
        # para que la rotación y los conteos sean predecibles.
        if os.path.isdir(taller_app.RESPALDO_LOCAL_DIR):
            for f in os.listdir(taller_app.RESPALDO_LOCAL_DIR):
                os.remove(os.path.join(taller_app.RESPALDO_LOCAL_DIR, f))
        self._max_copias_original = taller_app.RESPALDO_LOCAL_MAX_COPIAS

    def tearDown(self):
        taller_app.RESPALDO_LOCAL_MAX_COPIAS = self._max_copias_original

    def _crear_usuario_con_rol(self, username, rol):
        resp = self.client.post(
            "/api/admin/usuarios",
            json={"username": username, "password": "123456", "rol": rol},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cliente = taller_app.app.test_client()
        cliente.post("/login", data={"username": username, "password": "123456"})
        return cliente

    def test_crear_respaldo_local_genera_archivo_valido(self):
        exito, nombre = taller_app._crear_respaldo_local()
        self.assertTrue(exito, nombre)
        ruta = os.path.join(taller_app.RESPALDO_LOCAL_DIR, nombre)
        self.assertTrue(os.path.exists(ruta))
        self.assertTrue(taller_app.RESPALDO_LOCAL_PATRON.match(nombre))

    def test_listar_respaldos_locales_via_endpoint(self):
        taller_app._crear_respaldo_local()
        resp = self.client.get("/api/backup/locales")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreaterEqual(len(data["copias"]), 1)
        self.assertEqual(data["intervalo_minutos"], taller_app.RESPALDO_LOCAL_INTERVALO_MINUTOS)

    def test_rotacion_respeta_el_maximo_de_copias(self):
        taller_app.RESPALDO_LOCAL_MAX_COPIAS = 2
        for _ in range(4):
            time.sleep(1.05)  # el nombre incluye segundos -- evita colisiones
            ok, _ = taller_app._crear_respaldo_local()
            self.assertTrue(ok)
        copias = taller_app._listar_respaldos_locales()
        self.assertEqual(len(copias), 2)

    def test_crear_respaldo_via_endpoint_requiere_admin(self):
        cliente_bodega = self._crear_usuario_con_rol("bodega_resp", "bodega")
        resp = cliente_bodega.post("/api/backup/locales/crear")
        self.assertEqual(resp.status_code, 403)

        resp_admin = self.client.post("/api/backup/locales/crear")
        self.assertEqual(resp_admin.status_code, 200)

    def test_restaurar_devuelve_los_datos_al_estado_del_respaldo(self):
        creado = self.client.post(
            "/api/operativo/clientes", json={"nombre": "Cliente Respaldo Local"}
        ).get_json()
        cliente_id = creado["id"]

        nombre = self.client.post("/api/backup/locales/crear").get_json()["nombre"]

        # Modifica el cliente DESPUÉS del respaldo.
        resp_put = self.client.put(f"/api/operativo/clientes/{cliente_id}", json={"nombre": "Nombre cambiado despues"})
        self.assertEqual(resp_put.status_code, 200, resp_put.get_data(as_text=True))
        actualizado = self.client.get(f"/api/operativo/clientes/{cliente_id}").get_json()
        self.assertEqual(actualizado["nombre"], "NOMBRE CAMBIADO DESPUES")

        resp = self.client.post("/api/backup/locales/restaurar", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        restaurado = self.client.get(f"/api/operativo/clientes/{cliente_id}").get_json()
        self.assertEqual(restaurado["nombre"], "CLIENTE RESPALDO LOCAL")

    def test_restaurar_rechaza_nombre_con_path_traversal(self):
        resp = self.client.post(
            "/api/backup/locales/restaurar", json={"nombre": "../../etc/passwd"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_restaurar_nombre_inexistente_da_404(self):
        resp = self.client.post(
            "/api/backup/locales/restaurar", json={"nombre": "auto_20200101_000000.db"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_restaurar_via_endpoint_requiere_admin(self):
        nombre = self.client.post("/api/backup/locales/crear").get_json()["nombre"]
        cliente_operativo = self._crear_usuario_con_rol("operativo_resp", "operativo")
        resp = cliente_operativo.post("/api/backup/locales/restaurar", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 403)

    def test_listar_via_endpoint_requiere_admin(self):
        cliente_convertidores = self._crear_usuario_con_rol("conv_resp", "convertidores")
        resp = cliente_convertidores.get("/api/backup/locales")
        self.assertEqual(resp.status_code, 403)

    def test_descargar_y_restaurar_manual_ahora_exigen_admin(self):
        # Gap de seguridad real encontrado y corregido junto con esta
        # función: /api/backup/descargar y /api/backup/restaurar no tenían
        # @requiere_rol() antes de este cambio -- cualquier sesión podía
        # descargar TODA la base de datos, o subir un archivo y reemplazarla
        # entera.
        cliente_finanzas = self._crear_usuario_con_rol("fin_resp", "finanzas")
        resp_descarga = cliente_finanzas.get("/api/backup/descargar")
        self.assertEqual(resp_descarga.status_code, 403)

        resp_restaurar = cliente_finanzas.post("/api/backup/restaurar", data={})
        self.assertEqual(resp_restaurar.status_code, 403)


if __name__ == "__main__":
    unittest.main()
