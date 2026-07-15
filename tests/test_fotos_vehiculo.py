"""
Tests para las fotos de recepción del vehículo (modulos/operativo.py,
sección "Fotos de vehículos"): las 7 categorías obligatorias al ingresar un
auto, las fotos libres adicionales, la compresión a WebP, el endpoint de
"finalizar" (que arma el PDF y lo manda por correo al cliente sin bloquear
si no hay correo registrado), el rol exigido, y que no se pueda subir a una
OT/garantía que no existe.

Cómo correrlos:
    cd tests && python3 -m unittest test_fotos_vehiculo -v
"""

import io
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_fotos_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402
from modulos.operativo import FOTOS_CATEGORIAS_OBLIGATORIAS  # noqa: E402


def _imagen_de_prueba(color=(200, 50, 50), size=(60, 40)):
    """Genera bytes JPEG válidos y chicos, sin depender de ningún archivo
    externo -- Pillow ya es una dependencia real del proyecto (requirements.txt)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    buf.seek(0)
    return buf


class _BaseFotos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_usuario_con_rol(self, username, rol):
        resp = self.client.post(
            "/api/admin/usuarios",
            json={
                "username": username, "password": "123456",
                "es_admin": rol == "admin",
                "modulos": [] if rol == "admin" else [rol],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cliente = taller_app.app.test_client()
        cliente.post("/login", data={"username": username, "password": "123456"})
        return cliente

    def _crear_cliente(self, nombre="Cliente Fotos", email=""):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre, "email": email})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_equipo(self, cliente_id, identificador="FT0000"):
        resp = self.client.post(
            "/api/operativo/equipos",
            json={"cliente_id": cliente_id, "identificador": identificador, "marca": "Marca", "modelo": "Modelo"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id, equipo_id):
        resp = self.client.post(
            "/api/operativo/ordenes", json={"cliente_id": cliente_id, "equipo_id": equipo_id}
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_garantia(self, cliente_id, equipo_id):
        resp = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente_id, "equipo_id": equipo_id, "motivo": "Prueba"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _subir_foto(self, cliente, orden_id, categoria, titulo=None, entidad="ordenes"):
        data = {"archivo": (_imagen_de_prueba(), "foto.jpg"), "categoria": categoria}
        if titulo:
            data["titulo"] = titulo
        return cliente.post(
            f"/api/operativo/{entidad}/{orden_id}/fotos",
            data=data,
            content_type="multipart/form-data",
        )

    def _subir_las_7_obligatorias(self, cliente, orden_id, entidad="ordenes"):
        for cat, _ in FOTOS_CATEGORIAS_OBLIGATORIAS:
            resp = self._subir_foto(cliente, orden_id, cat, entidad=entidad)
            self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))


class TestFotosSubidaYListado(_BaseFotos):
    def test_subir_foto_obligatoria_y_listarla(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo["id"])

        resp = self._subir_foto(self.client, orden["id"], "frente")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        foto = resp.get_json()
        self.assertEqual(foto["categoria"], "frente")
        self.assertEqual(foto["categoria_label"], "Frente")

        listado = self.client.get(f"/api/operativo/ordenes/{orden['id']}/fotos").get_json()
        self.assertEqual(len(listado["fotos"]), 1)
        self.assertIn("Costado derecho", listado["faltan_obligatorias"])
        self.assertNotIn("Frente", listado["faltan_obligatorias"])

    def test_categoria_invalida_da_400(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo["id"])
        resp = self._subir_foto(self.client, orden["id"], "categoria_que_no_existe")
        self.assertEqual(resp.status_code, 400)

    def test_foto_libre_exige_titulo_o_usa_uno_por_defecto(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo["id"])
        resp = self._subir_foto(self.client, orden["id"], "libre", titulo="Rayón puerta")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["titulo"], "RAYÓN PUERTA")

    def test_subir_a_orden_inexistente_da_404(self):
        resp = self._subir_foto(self.client, 999999, "frente")
        self.assertEqual(resp.status_code, 404)

    def test_archivo_vacio_da_400(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo["id"])
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/fotos",
            data={"archivo": (io.BytesIO(b""), "vacio.jpg"), "categoria": "frente"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_archivo_no_es_imagen_da_400_no_500(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo["id"])
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/fotos",
            data={"archivo": (io.BytesIO(b"esto no es una imagen"), "falso.jpg"), "categoria": "frente"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_servir_archivo_y_eliminar_foto(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo["id"])
        foto = self._subir_foto(self.client, orden["id"], "frente").get_json()

        resp_archivo = self.client.get(f"/api/operativo/fotos/{foto['id']}/archivo")
        self.assertEqual(resp_archivo.status_code, 200)
        self.assertEqual(resp_archivo.mimetype, "image/webp")

        resp_del = self.client.delete(f"/api/operativo/fotos/{foto['id']}")
        self.assertEqual(resp_del.status_code, 200)
        self.assertEqual(self.client.get(f"/api/operativo/fotos/{foto['id']}/archivo").status_code, 404)


class TestFotosGarantia(_BaseFotos):
    def test_fotos_de_garantia_usan_el_mismo_flujo_generico(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "FT0001")
        garantia = self._crear_garantia(cliente["id"], equipo["id"])
        resp = self._subir_foto(self.client, garantia["id"], "vin", entidad="garantias")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        listado = self.client.get(f"/api/operativo/garantias/{garantia['id']}/fotos").get_json()
        self.assertEqual(len(listado["fotos"]), 1)


class TestFotosRoles(_BaseFotos):
    def test_endpoints_exigen_rol_operativo_o_admin(self):
        cliente_finanzas = self._crear_usuario_con_rol("fin_fotos", "finanzas")
        cli = self._crear_cliente()
        equipo = self._crear_equipo(cli["id"])
        orden = self._crear_orden(cli["id"], equipo["id"])

        resp = self._subir_foto(cliente_finanzas, orden["id"], "frente")
        self.assertEqual(resp.status_code, 403)
        resp_get = cliente_finanzas.get(f"/api/operativo/ordenes/{orden['id']}/fotos")
        self.assertEqual(resp_get.status_code, 403)

    def test_rol_operativo_si_puede(self):
        cliente_operativo = self._crear_usuario_con_rol("op_fotos", "operativo")
        cli = self._crear_cliente()
        equipo = self._crear_equipo(cli["id"], "FT0002")
        orden = self._crear_orden(cli["id"], equipo["id"])
        resp = self._subir_foto(cliente_operativo, orden["id"], "frente")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))


class TestFinalizarFotosIngreso(_BaseFotos):
    def test_finalizar_sin_las_7_obligatorias_da_400_con_el_detalle(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "FT0003")
        orden = self._crear_orden(cliente["id"], equipo["id"])
        self._subir_foto(self.client, orden["id"], "frente")

        resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/fotos/finalizar")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("Costado derecho", data["faltan_obligatorias"])

    def test_finalizar_sin_correo_de_cliente_no_bloquea_pero_avisa(self):
        cliente = self._crear_cliente(nombre="Cliente Sin Correo", email="")
        equipo = self._crear_equipo(cliente["id"], "FT0004")
        orden = self._crear_orden(cliente["id"], equipo["id"])
        self._subir_las_7_obligatorias(self.client, orden["id"])

        resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/fotos/finalizar")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["correo_enviado"])
        self.assertIsNone(data["destinatario"])

    def test_finalizar_orden_inexistente_da_404(self):
        resp = self.client.post("/api/operativo/ordenes/999999/fotos/finalizar")
        self.assertEqual(resp.status_code, 404)

    def test_finalizar_exige_rol_operativo(self):
        cliente_bodega = self._crear_usuario_con_rol("bod_fotos", "bodega")
        cli = self._crear_cliente()
        equipo = self._crear_equipo(cli["id"], "FT0005")
        orden = self._crear_orden(cli["id"], equipo["id"])
        self._subir_las_7_obligatorias(self.client, orden["id"])

        resp = cliente_bodega.post(f"/api/operativo/ordenes/{orden['id']}/fotos/finalizar")
        self.assertEqual(resp.status_code, 403)


class TestFinalizarFotosGarantia(_BaseFotos):
    """Fase 2: el mismo flujo de fotos obligatorias + PDF + correo, pero
    para cuando el vehículo entra por garantía (la garantía tiene su propio
    proceso, separado de la OT -- ver _finalizar_fotos_generico)."""

    def test_finalizar_sin_las_7_obligatorias_da_400_con_el_detalle(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"], "FT0006")
        garantia = self._crear_garantia(cliente["id"], equipo["id"])
        self._subir_foto(self.client, garantia["id"], "vin", entidad="garantias")

        resp = self.client.post(f"/api/operativo/garantias/{garantia['id']}/fotos/finalizar")
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("Frente", data["faltan_obligatorias"])

    def test_finalizar_con_correo_de_cliente_intenta_enviar(self):
        cliente = self._crear_cliente(nombre="Cliente Con Correo", email="cliente@ejemplo.cl")
        equipo = self._crear_equipo(cliente["id"], "FT0007")
        garantia = self._crear_garantia(cliente["id"], equipo["id"])
        self._subir_las_7_obligatorias(self.client, garantia["id"], entidad="garantias")

        resp = self.client.post(f"/api/operativo/garantias/{garantia['id']}/fotos/finalizar")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["destinatario"], "cliente@ejemplo.cl")
        # Sin RESEND_API_KEY/SMTP configurado en el entorno de pruebas, el
        # envío real falla -- lo que importa acá es que SÍ se intentó
        # (armó el PDF, encontró el destinatario) y no bloqueó la respuesta.
        self.assertFalse(data["correo_enviado"])

    def test_finalizar_garantia_inexistente_da_404(self):
        resp = self.client.post("/api/operativo/garantias/999999/fotos/finalizar")
        self.assertEqual(resp.status_code, 404)

    def test_finalizar_exige_rol_operativo(self):
        cliente_bodega = self._crear_usuario_con_rol("bod_fotos_gar", "bodega")
        cli = self._crear_cliente()
        equipo = self._crear_equipo(cli["id"], "FT0008")
        garantia = self._crear_garantia(cli["id"], equipo["id"])
        self._subir_las_7_obligatorias(self.client, garantia["id"], entidad="garantias")

        resp = cliente_bodega.post(f"/api/operativo/garantias/{garantia['id']}/fotos/finalizar")
        self.assertEqual(resp.status_code, 403)

    def test_fotos_de_orden_y_garantia_no_se_mezclan(self):
        """Ambas usan la misma tabla fotos_vehiculo con entidad_tipo/entidad_id
        -- este test confirma que subir fotos a una garantía no las cuenta
        como obligatorias de una OT con el mismo id numérico, y viceversa.
        Se usan dos vehículos distintos porque no se puede abrir una
        garantía para un auto que todavía tiene una OT abierta (regla de
        negocio ya existente, ver crear_garantia)."""
        cliente = self._crear_cliente()
        equipo_ot = self._crear_equipo(cliente["id"], "FT0009")
        equipo_gar = self._crear_equipo(cliente["id"], "FT0010")
        orden = self._crear_orden(cliente["id"], equipo_ot["id"])
        garantia = self._crear_garantia(cliente["id"], equipo_gar["id"])

        self._subir_las_7_obligatorias(self.client, orden["id"], entidad="ordenes")

        # La garantía (que puede compartir el mismo id numérico que otra
        # entidad en otra tabla) no debería heredar esas fotos.
        listado_garantia = self.client.get(f"/api/operativo/garantias/{garantia['id']}/fotos").get_json()
        self.assertEqual(len(listado_garantia["fotos"]), 0)
        self.assertEqual(len(listado_garantia["faltan_obligatorias"]), 7)


if __name__ == "__main__":
    unittest.main()
