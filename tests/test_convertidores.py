"""
Tests automatizados para modulos/convertidores.py (módulo "Convertidores de
Torque"), priorizando los casos de seguridad de plata: que 'precio_venta'
nunca se cuele en un origen interno, que la carga/retiro automático de
costo en la OT/garantía de origen sea siempre consistente, que eliminar un
convertidor no se pueda hacer por debajo de una pieza que ya está en uso, y
que los endpoints respeten el rol de quien llama.

Sigue el mismo patrón que tests/test_logica_sensible.py: base de datos de
prueba en un archivo temporal (nunca la real), fijada en DB_PATH ANTES de
importar app.py, mismo login admin/taller2026 vía test_client(), mismos
helpers _crear_cliente / _crear_producto_http, etc.

Cómo correrlos:
    cd tests && python3 -m unittest test_convertidores -v
"""

import io
import os
import sys
import tempfile
import unittest
from datetime import datetime

# ---- Igual que test_logica_sensible.py: hay que apuntar DB_PATH a un
# archivo temporal ANTES de importar app.py (que llama a init_db() apenas
# se importa), para no tocar nunca la base de datos real del taller.
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_conv_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class _BaseConvertidores(unittest.TestCase):
    """Base común: sesión admin lista, helpers de creación de clientes/
    equipos/OT/garantías/productos vía HTTP (nunca tocando la DB
    directamente salvo que el test lo necesite explícitamente)."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Convertidores"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_equipo(self, cliente_id, identificador="CV0000"):
        resp = self.client.post(
            "/api/operativo/equipos",
            json={"cliente_id": cliente_id, "identificador": identificador, "marca": "Marca", "modelo": "Modelo"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id, equipo_id=None, **extra):
        payload = {"cliente_id": cliente_id}
        if equipo_id is not None:
            payload["equipo_id"] = equipo_id
        payload.update(extra)
        resp = self.client.post("/api/operativo/ordenes", json=payload)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_garantia(self, cliente_id, equipo_id=None, motivo="Motivo de prueba"):
        payload = {"cliente_id": cliente_id, "motivo": motivo}
        if equipo_id is not None:
            payload["equipo_id"] = equipo_id
        resp = self.client.post("/api/operativo/garantias", json=payload)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_producto_http(self, nombre, stock_disponible=10, costo_compra=1000):
        resp = self.client.post(
            "/api/productos",
            json={"nombre": nombre, "stock_disponible": stock_disponible, "costo_compra": costo_compra},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_convertidor(self, **payload):
        resp = self.client.post("/api/convertidores", json=payload)
        return resp

    def _crear_convertidor_ok(self, **payload):
        resp = self._crear_convertidor(**payload)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _agregar_item(self, convertidor_id, **payload):
        return self.client.post(f"/api/convertidores/{convertidor_id}/items", json=payload)

    def _editar_convertidor(self, convertidor_id, **payload):
        return self.client.put(f"/api/convertidores/{convertidor_id}", json=payload)


# ---------------------------------------------------------------------------
# CRUD básico
# ---------------------------------------------------------------------------

class TestConvertidoresCRUDBasico(_BaseConvertidores):
    def test_crear_cliente_directo_y_listarlo(self):
        cliente = self._crear_cliente("Cliente CRUD 1")
        creado = self._crear_convertidor_ok(
            origen_tipo="cliente_directo", cliente_id=cliente["id"], marca="valeo", modelo="123",
        )
        self.assertEqual(creado["origen_tipo"], "cliente_directo")
        self.assertEqual(creado["estado"], "ingreso")
        self.assertEqual(creado["marca"], "VALEO")  # se guarda en mayúsculas
        self.assertEqual(creado["cliente"]["id"], cliente["id"])

        listado = self.client.get("/api/convertidores").get_json()
        ids = {c["id"] for c in listado["convertidores"]}
        self.assertIn(creado["id"], ids)

    def test_crear_stock_taller_sin_cliente_ni_equipo(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller", marca="aisin")
        self.assertIsNone(creado["cliente_id"])
        self.assertIsNone(creado["equipo_id"])

    def test_ver_detalle(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self.client.get(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp.status_code, 200)
        detalle = resp.get_json()
        self.assertEqual(detalle["id"], creado["id"])
        self.assertEqual(detalle["items"], [])
        self.assertEqual(detalle["total_reparacion"], 0)

    def test_editar_campos_basicos(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._editar_convertidor(creado["id"], marca="nueva marca", diagnostico="revisado")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        actualizado = resp.get_json()
        self.assertEqual(actualizado["marca"], "NUEVA MARCA")
        self.assertEqual(actualizado["diagnostico"], "REVISADO")

    def test_eliminar_convertidor_sin_uso_es_permitido(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self.client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        resp_get = self.client.get(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp_get.status_code, 404)


# ---------------------------------------------------------------------------
# Validaciones de entrada
# ---------------------------------------------------------------------------

class TestConvertidoresValidaciones(_BaseConvertidores):
    def test_origen_tipo_invalido(self):
        resp = self._crear_convertidor(origen_tipo="algo_raro")
        self.assertEqual(resp.status_code, 400)

    def test_json_vacio_da_400_no_500(self):
        resp = self.client.post("/api/convertidores", json={})
        self.assertEqual(resp.status_code, 400)

    def test_body_no_json_da_400_no_500(self):
        resp = self.client.post("/api/convertidores", data="esto no es json", content_type="text/plain")
        self.assertEqual(resp.status_code, 400)

    def test_cliente_directo_sin_cliente_id(self):
        resp = self._crear_convertidor(origen_tipo="cliente_directo")
        self.assertEqual(resp.status_code, 400)

    def test_cliente_directo_con_cliente_inexistente(self):
        resp = self._crear_convertidor(origen_tipo="cliente_directo", cliente_id=999999)
        self.assertEqual(resp.status_code, 400)

    def test_cliente_directo_con_cliente_id_tipo_incorrecto(self):
        resp = self._crear_convertidor(origen_tipo="cliente_directo", cliente_id="no-es-numero")
        self.assertEqual(resp.status_code, 400)

    def test_desde_ot_auto_sin_orden_origen_id(self):
        resp = self._crear_convertidor(origen_tipo="desde_ot_auto")
        self.assertEqual(resp.status_code, 400)

    def test_desde_ot_auto_con_orden_inexistente(self):
        resp = self._crear_convertidor(origen_tipo="desde_ot_auto", orden_origen_id=999999)
        self.assertEqual(resp.status_code, 400)

    def test_desde_garantia_sin_garantia_origen_id(self):
        resp = self._crear_convertidor(origen_tipo="desde_garantia")
        self.assertEqual(resp.status_code, 400)

    def test_desde_garantia_con_garantia_inexistente(self):
        resp = self._crear_convertidor(origen_tipo="desde_garantia", garantia_origen_id=999999)
        self.assertEqual(resp.status_code, 400)

    def test_obtener_convertidor_inexistente_da_404(self):
        resp = self.client.get("/api/convertidores/999999")
        self.assertEqual(resp.status_code, 404)

    def test_editar_convertidor_inexistente_da_404(self):
        resp = self._editar_convertidor(999999, marca="x")
        self.assertEqual(resp.status_code, 404)

    def test_eliminar_convertidor_inexistente_da_404(self):
        resp = self.client.delete("/api/convertidores/999999")
        self.assertEqual(resp.status_code, 404)

    def test_estado_invalido_al_editar(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._editar_convertidor(creado["id"], estado="estado_que_no_existe")
        self.assertEqual(resp.status_code, 400)

    def test_no_se_puede_setear_instalado_a_mano(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._editar_convertidor(creado["id"], estado="instalado")
        self.assertEqual(resp.status_code, 400)

    def test_agregar_item_tipo_invalido(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="algo_raro")
        self.assertEqual(resp.status_code, 400)

    def test_agregar_item_a_convertidor_inexistente_da_404(self):
        resp = self._agregar_item(999999, tipo="mano_obra", descripcion="x", cantidad=1, costo_unitario=100)
        self.assertEqual(resp.status_code, 404)

    def test_agregar_item_repuesto_sin_producto_id(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="repuesto", cantidad=1)
        self.assertEqual(resp.status_code, 400)

    def test_agregar_item_cantidad_invalida(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="mano_obra", descripcion="algo", cantidad=-1, costo_unitario=10)
        self.assertEqual(resp.status_code, 400)

    def test_agregar_item_mano_obra_sin_descripcion(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="mano_obra", cantidad=1, costo_unitario=10)
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# Origen interno: desde_ot_auto
# ---------------------------------------------------------------------------

class TestConvertidoresDesdeOtAuto(_BaseConvertidores):
    def test_precio_venta_queda_none_al_crear_pese_a_mandarlo(self):
        cliente = self._crear_cliente("Cliente OT interno 1")
        equipo = self._crear_equipo(cliente["id"], "OTX-001")
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])

        creado = self._crear_convertidor_ok(
            origen_tipo="desde_ot_auto", orden_origen_id=orden["id"], precio_venta=999999,
        )
        self.assertIsNone(creado["precio_venta"])
        self.assertEqual(creado["cliente_id"], cliente["id"])
        self.assertEqual(creado["equipo_id"], equipo["id"])

    def test_precio_venta_queda_none_al_editar_pese_a_mandarlo(self):
        cliente = self._crear_cliente("Cliente OT interno 2")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])

        resp = self._editar_convertidor(creado["id"], precio_venta=55555)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertIsNone(resp.get_json()["precio_venta"])

    def test_entrega_carga_costo_real_en_la_ot_y_descuenta_utilidad(self):
        cliente = self._crear_cliente("Cliente OT interno 3")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 200000})

        creado = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])
        # Costo real de la reparación: 30.000 de mano de obra.
        item_resp = self._agregar_item(
            creado["id"], tipo="mano_obra", descripcion="Reparación interna", cantidad=1, costo_unitario=30000,
        )
        self.assertEqual(item_resp.status_code, 201, item_resp.get_data(as_text=True))

        resp = self._editar_convertidor(creado["id"], estado="entregado")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        detalle_orden = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_convertidor = [i for i in detalle_orden["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_convertidor), 1)
        self.assertAlmostEqual(items_convertidor[0]["costo_unitario"], 30000)
        self.assertAlmostEqual(detalle_orden["costo_convertidores"], 30000)
        self.assertAlmostEqual(detalle_orden["utilidad_estimada"], 200000 - 30000)

    def test_entrega_es_idempotente_no_duplica_el_item(self):
        cliente = self._crear_cliente("Cliente OT interno 4")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])
        self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Trabajo", cantidad=1, costo_unitario=5000)

        self._editar_convertidor(creado["id"], estado="entregado")
        # Un segundo PUT a 'entregado' (sin cambiar de estado real) no debe
        # duplicar el ítem cargado en la OT.
        self._editar_convertidor(creado["id"], estado="entregado")

        detalle_orden = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_convertidor = [i for i in detalle_orden["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_convertidor), 1)


# ---------------------------------------------------------------------------
# Origen interno: desde_garantia
# ---------------------------------------------------------------------------

class TestConvertidoresDesdeGarantia(_BaseConvertidores):
    def test_precio_venta_queda_none_y_autocompleta_cliente_equipo(self):
        cliente = self._crear_cliente("Cliente Garantía interno 1")
        equipo = self._crear_equipo(cliente["id"], "GTX-001")
        garantia = self._crear_garantia(cliente["id"], equipo_id=equipo["id"])

        creado = self._crear_convertidor_ok(
            origen_tipo="desde_garantia", garantia_origen_id=garantia["id"], precio_venta=123456,
        )
        self.assertIsNone(creado["precio_venta"])
        self.assertEqual(creado["cliente_id"], cliente["id"])
        self.assertEqual(creado["equipo_id"], equipo["id"])

    def test_entrega_carga_costo_real_como_item_de_garantia(self):
        cliente = self._crear_cliente("Cliente Garantía interno 2")
        garantia = self._crear_garantia(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_garantia", garantia_origen_id=garantia["id"])

        self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Reparación", cantidad=1, costo_unitario=15000)

        resp = self._editar_convertidor(creado["id"], estado="entregado")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        detalle_garantia = self.client.get(f"/api/operativo/garantias/{garantia['id']}").get_json()
        items_convertidor = [i for i in detalle_garantia["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_convertidor), 1)
        self.assertAlmostEqual(items_convertidor[0]["costo_unitario"], 15000)


# ---------------------------------------------------------------------------
# Retroceso desde 'entregado' y recarga con costo actualizado
# ---------------------------------------------------------------------------

class TestConvertidoresRetroceso(_BaseConvertidores):
    def test_retroceso_desde_entregado_retira_el_cargo_y_re_entrega_con_costo_actualizado(self):
        cliente = self._crear_cliente("Cliente Retroceso 1")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])
        self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Primera pasada", cantidad=1, costo_unitario=10000)

        self._editar_convertidor(creado["id"], estado="entregado")
        detalle_orden_1 = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_1 = [i for i in detalle_orden_1["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_1), 1)
        self.assertAlmostEqual(items_1[0]["costo_unitario"], 10000)

        # Retrocede: el cargo en la OT debe eliminarse.
        resp_retroceso = self._editar_convertidor(creado["id"], estado="reparacion")
        self.assertEqual(resp_retroceso.status_code, 200, resp_retroceso.get_data(as_text=True))
        detalle_orden_2 = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_2 = [i for i in detalle_orden_2["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_2), 0)

        # Agrega más costo ANTES de re-entregar (para confirmar que el
        # recargo usa el costo actualizado, no el viejo de 10.000).
        self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Segunda pasada", cantidad=1, costo_unitario=7000)

        self._editar_convertidor(creado["id"], estado="entregado")
        detalle_orden_3 = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_3 = [i for i in detalle_orden_3["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_3), 1)
        self.assertAlmostEqual(items_3[0]["costo_unitario"], 17000)

    def test_retroceso_desde_entregado_en_garantia_retira_y_recarga(self):
        cliente = self._crear_cliente("Cliente Retroceso Garantía 1")
        garantia = self._crear_garantia(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_garantia", garantia_origen_id=garantia["id"])
        self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Primera pasada", cantidad=1, costo_unitario=4000)

        self._editar_convertidor(creado["id"], estado="entregado")
        self._editar_convertidor(creado["id"], estado="esperando_repuesto")

        detalle_1 = self.client.get(f"/api/operativo/garantias/{garantia['id']}").get_json()
        self.assertEqual(len([i for i in detalle_1["items"] if i["tipo"] == "convertidor_torque"]), 0)

        self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Repuesto extra", cantidad=1, costo_unitario=1000)
        self._editar_convertidor(creado["id"], estado="entregado")

        detalle_2 = self.client.get(f"/api/operativo/garantias/{garantia['id']}").get_json()
        items_2 = [i for i in detalle_2["items"] if i["tipo"] == "convertidor_torque"]
        self.assertEqual(len(items_2), 1)
        self.assertAlmostEqual(items_2[0]["costo_unitario"], 5000)


# ---------------------------------------------------------------------------
# eliminar_convertidor: bloqueos
# ---------------------------------------------------------------------------

class TestConvertidoresEliminar(_BaseConvertidores):
    def test_bloqueado_si_instalado_como_repuesto_de_otro_convertidor(self):
        hijo = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(hijo["id"], estado="listo")
        madre = self._crear_convertidor_ok(origen_tipo="stock_taller")

        resp_item = self._agregar_item(madre["id"], tipo="convertidor", convertidor_hijo_id=hijo["id"])
        self.assertEqual(resp_item.status_code, 201, resp_item.get_data(as_text=True))

        resp_delete = self.client.delete(f"/api/convertidores/{hijo['id']}")
        self.assertEqual(resp_delete.status_code, 409)

    def test_bloqueado_si_ya_cargado_en_ot(self):
        cliente = self._crear_cliente("Cliente Eliminar OT 1")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])
        self._editar_convertidor(creado["id"], estado="entregado")

        resp = self.client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp.status_code, 409)

    def test_bloqueado_si_ya_cargado_en_garantia(self):
        cliente = self._crear_cliente("Cliente Eliminar Garantía 1")
        garantia = self._crear_garantia(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="desde_garantia", garantia_origen_id=garantia["id"])
        self._editar_convertidor(creado["id"], estado="entregado")

        resp = self.client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp.status_code, 409)

    def test_bloqueado_si_instalado_en_una_ot_como_repuesto_directo(self):
        cliente = self._crear_cliente("Cliente Eliminar Instalado 1")
        orden = self._crear_orden(cliente["id"])
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(creado["id"], estado="listo")

        resp_instalar = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "convertidor_torque", "convertidor_id": creado["id"]},
        )
        self.assertEqual(resp_instalar.status_code, 201, resp_instalar.get_data(as_text=True))

        resp_delete = self.client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp_delete.status_code, 409)

    def test_permitido_si_no_esta_en_uso(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self.client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_eliminar_devuelve_stock_de_items_repuesto(self):
        producto = self._crear_producto_http("Repuesto Convertidor Eliminar", stock_disponible=5, costo_compra=2000)
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._agregar_item(creado["id"], tipo="repuesto", producto_id=producto["id"], cantidad=2)

        tras_consumo = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_consumo["stock_disponible"], 3)

        resp = self.client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        tras_eliminar = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_eliminar["stock_disponible"], 5)


# ---------------------------------------------------------------------------
# Ítems del convertidor
# ---------------------------------------------------------------------------

class TestConvertidoresItems(_BaseConvertidores):
    def test_item_repuesto_consume_stock_real(self):
        producto = self._crear_producto_http("Repuesto Convertidor Items 1", stock_disponible=10, costo_compra=1500)
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="repuesto", producto_id=producto["id"], cantidad=3)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 7)

    def test_eliminar_item_repuesto_devuelve_stock(self):
        producto = self._crear_producto_http("Repuesto Convertidor Items 2", stock_disponible=10, costo_compra=1500)
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        item = self._agregar_item(creado["id"], tipo="repuesto", producto_id=producto["id"], cantidad=4).get_json()

        tras_consumo = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_consumo["stock_disponible"], 6)

        resp = self.client.delete(f"/api/convertidores/{creado['id']}/items/{item['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        tras_devolucion = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_devolucion["stock_disponible"], 10)

    def test_item_mano_obra_no_toca_stock(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="mano_obra", descripcion="Diagnóstico", cantidad=1, costo_unitario=5000)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        detalle = self.client.get(f"/api/convertidores/{creado['id']}").get_json()
        self.assertEqual(detalle["total_reparacion"], 5000)
        self.assertIsNone(detalle["items"][0]["producto_id"])

    def test_item_convertidor_repuesto_solo_acepta_stock_taller_listo(self):
        cliente = self._crear_cliente("Cliente Item Convertidor 1")
        madre = self._crear_convertidor_ok(origen_tipo="stock_taller")

        # No 'listo' todavía -> rechazado.
        hijo_no_listo = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp_no_listo = self._agregar_item(madre["id"], tipo="convertidor", convertidor_hijo_id=hijo_no_listo["id"])
        self.assertEqual(resp_no_listo.status_code, 409)

        # Origen distinto de stock_taller -> rechazado aunque esté 'listo'.
        hijo_cliente = self._crear_convertidor_ok(origen_tipo="cliente_directo", cliente_id=cliente["id"])
        self._editar_convertidor(hijo_cliente["id"], estado="listo")
        resp_no_stock = self._agregar_item(madre["id"], tipo="convertidor", convertidor_hijo_id=hijo_cliente["id"])
        self.assertEqual(resp_no_stock.status_code, 400)

        # Válido: stock_taller y 'listo'.
        hijo_ok = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(hijo_ok["id"], estado="listo")
        resp_ok = self._agregar_item(madre["id"], tipo="convertidor", convertidor_hijo_id=hijo_ok["id"])
        self.assertEqual(resp_ok.status_code, 201, resp_ok.get_data(as_text=True))

        hijo_actualizado = self.client.get(f"/api/convertidores/{hijo_ok['id']}").get_json()
        self.assertEqual(hijo_actualizado["estado"], "instalado")

    def test_costo_de_convertidor_repuesto_se_sigue_en_vivo(self):
        madre = self._crear_convertidor_ok(origen_tipo="stock_taller")
        hijo = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(hijo["id"], estado="listo")
        self._agregar_item(hijo["id"], tipo="mano_obra", descripcion="Costo inicial", cantidad=1, costo_unitario=1000)
        self._editar_convertidor(hijo["id"], estado="listo")  # se queda 'listo' tras agregar el ítem

        item = self._agregar_item(madre["id"], tipo="convertidor", convertidor_hijo_id=hijo["id"]).get_json()
        self.assertAlmostEqual(item["costo_unitario"], 1000)

        # El hijo ya quedó 'instalado' -- no se le pueden agregar más ítems
        # (estado cerrado), así que para probar que el costo se sigue en
        # vivo hay que mirar el total del madre después de una consulta,
        # que recalcula sobre la marcha (ver _convertidor_dict).
        detalle_madre = self.client.get(f"/api/convertidores/{madre['id']}").get_json()
        self.assertAlmostEqual(detalle_madre["total_reparacion"], 1000)

    def test_eliminar_item_convertidor_libera_al_hijo(self):
        madre = self._crear_convertidor_ok(origen_tipo="stock_taller")
        hijo = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(hijo["id"], estado="listo")
        item = self._agregar_item(madre["id"], tipo="convertidor", convertidor_hijo_id=hijo["id"]).get_json()

        hijo_instalado = self.client.get(f"/api/convertidores/{hijo['id']}").get_json()
        self.assertEqual(hijo_instalado["estado"], "instalado")

        resp = self.client.delete(f"/api/convertidores/{madre['id']}/items/{item['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        hijo_liberado = self.client.get(f"/api/convertidores/{hijo['id']}").get_json()
        self.assertEqual(hijo_liberado["estado"], "listo")

    def test_no_se_puede_usarse_a_si_mismo_como_repuesto(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(creado["id"], estado="listo")
        resp = self._agregar_item(creado["id"], tipo="convertidor", convertidor_hijo_id=creado["id"])
        self.assertEqual(resp.status_code, 400)

    def test_no_se_pueden_agregar_items_a_convertidor_cerrado(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        self._editar_convertidor(creado["id"], estado="cancelado")
        resp = self._agregar_item(creado["id"], tipo="mano_obra", descripcion="algo", cantidad=1, costo_unitario=100)
        self.assertEqual(resp.status_code, 409)

    def test_eliminar_item_inexistente_da_404(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self.client.delete(f"/api/convertidores/{creado['id']}/items/999999")
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class TestConvertidoresDashboard(_BaseConvertidores):
    def test_valor_cerrado_mes_excluye_internos_y_gasto_externos_solo_cuenta_cliente_directo(self):
        antes = self.client.get("/api/convertidores/dashboard").get_json()

        # Externo (cliente_directo) cerrado este mes, con precio_venta.
        cliente = self._crear_cliente("Cliente Dashboard 1")
        externo = self._crear_convertidor_ok(origen_tipo="cliente_directo", cliente_id=cliente["id"], precio_venta=50000)
        self._agregar_item(externo["id"], tipo="mano_obra", descripcion="Reparación externa", cantidad=1, costo_unitario=20000)
        self._editar_convertidor(externo["id"], estado="entregado")

        # Interno (desde_ot_auto) cerrado este mes.
        orden = self._crear_orden(cliente["id"])
        interno = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])
        self._agregar_item(interno["id"], tipo="mano_obra", descripcion="Reparación interna", cantidad=1, costo_unitario=8000)
        self._editar_convertidor(interno["id"], estado="entregado")

        despues = self.client.get("/api/convertidores/dashboard").get_json()

        # valor_cerrado_mes solo debe subir por el externo (50.000, su
        # precio_venta), nunca por el interno.
        self.assertAlmostEqual(despues["valor_cerrado_mes"] - antes["valor_cerrado_mes"], 50000)
        # valor_cerrado_interno_mes sí debe reflejar el interno (su costo
        # real de ítems: 8.000).
        self.assertAlmostEqual(despues["valor_cerrado_interno_mes"] - antes["valor_cerrado_interno_mes"], 8000)
        # gasto_externos_mes solo cuenta cliente_directo (costo real: 20.000,
        # no el precio_venta de 50.000, y nunca el interno).
        self.assertAlmostEqual(despues["gasto_externos_mes"] - antes["gasto_externos_mes"], 20000)

    def test_por_estado_y_por_origen_reflejan_lo_creado(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        dashboard = self.client.get("/api/convertidores/dashboard").get_json()
        self.assertGreaterEqual(dashboard["por_estado"]["ingreso"], 1)
        self.assertGreaterEqual(dashboard["por_origen"]["stock_taller"], 1)
        self.assertGreaterEqual(dashboard["total"], 1)


# ---------------------------------------------------------------------------
# Reporte Excel
# ---------------------------------------------------------------------------

class TestConvertidoresReporteExcel(_BaseConvertidores):
    def test_reporte_no_falla_y_devuelve_un_excel(self):
        self._crear_convertidor_ok(origen_tipo="stock_taller", marca="reporte-basico")
        resp = self.client.get("/api/convertidores/reportes/excel")
        # OJO: el cuerpo es binario (.xlsx) -- no se puede pasar
        # resp.get_data(as_text=True) como mensaje de assertEqual porque se
        # evalúa siempre (incluso cuando la aserción pasa) y falla al
        # intentar decodificarlo como UTF-8.
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheet", resp.headers.get("Content-Type", ""))

    def _leer_totales_reporte(self):
        """Lee las líneas de resumen del reporte Excel (formato "label:
        $monto" en una sola celda, ver core.py:_excel_resumen) y devuelve
        (valor_total, valor_ya_cargado) como floats."""
        from openpyxl import load_workbook

        resp = self.client.get("/api/convertidores/reportes/excel")
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.data))
        ws = wb["Convertidores"]

        valor_total = valor_ya_cargado = None
        for row in ws.iter_rows(values_only=True):
            celda = row[0] if row else None
            if not isinstance(celda, str):
                continue
            if celda.startswith("Valor total"):
                valor_total = float(celda.rsplit("$", 1)[1].replace(".", ""))
            elif celda.startswith("Ya cargado en OT/garantía"):
                valor_ya_cargado = float(celda.rsplit("$", 1)[1].replace(".", ""))
        self.assertIsNotNone(valor_total)
        self.assertIsNotNone(valor_ya_cargado)
        return valor_total, valor_ya_cargado

    def test_reporte_excluye_convertidores_ya_cargados_en_ot_del_total(self):
        # El reporte escanea TODOS los convertidores (sin filtrar por lo
        # creado en este test) y la base de datos de prueba es compartida
        # entre clases dentro del mismo proceso -- por eso se compara
        # "antes" vs "después" (delta), en vez de un valor absoluto, para
        # no depender de lo que hayan dejado otros tests.
        total_antes, ya_cargado_antes = self._leer_totales_reporte()

        cliente = self._crear_cliente("Cliente Reporte Excel 1")
        orden = self._crear_orden(cliente["id"])
        interno = self._crear_convertidor_ok(origen_tipo="desde_ot_auto", orden_origen_id=orden["id"])
        self._agregar_item(interno["id"], tipo="mano_obra", descripcion="Costo", cantidad=1, costo_unitario=12345)
        self._editar_convertidor(interno["id"], estado="entregado")

        total_despues, ya_cargado_despues = self._leer_totales_reporte()

        # El costo del convertidor interno ya cargado en la OT debe subir
        # el acumulado de "ya cargado" (12.345) y NO tocar el total general.
        self.assertAlmostEqual(ya_cargado_despues - ya_cargado_antes, 12345, delta=1)
        self.assertAlmostEqual(total_despues - total_antes, 0, delta=1)


# ---------------------------------------------------------------------------
# Seguridad: roles y payloads maliciosos
# ---------------------------------------------------------------------------

class TestConvertidoresSeguridad(_BaseConvertidores):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._contador_usuarios = 0

    def _nuevo_username(self, prefijo):
        type(self)._contador_usuarios += 1
        return f"{prefijo}_{type(self)._contador_usuarios}"

    def _crear_usuario(self, rol):
        username = self._nuevo_username(f"conv_rol_{rol}")
        resp = self.client.post(
            "/api/admin/usuarios",
            json={
                "username": username, "password": "clave123456", "nombre": username,
                "es_admin": rol == "admin",
                "modulos": [] if rol == "admin" else [rol],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return username

    def _client_con_rol(self, rol):
        username = self._crear_usuario(rol)
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": "clave123456"})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    def test_finanzas_no_puede_listar_ni_crear_convertidores(self):
        client = self._client_con_rol("finanzas")
        resp_listar = client.get("/api/convertidores")
        self.assertEqual(resp_listar.status_code, 403)
        resp_crear = client.post("/api/convertidores", json={"origen_tipo": "stock_taller"})
        self.assertEqual(resp_crear.status_code, 403)

    def test_bodega_no_puede_acceder_a_convertidores(self):
        client = self._client_con_rol("bodega")
        resp = client.get("/api/convertidores/dashboard")
        self.assertEqual(resp.status_code, 403)

    def test_rol_convertidores_puede_operar_el_modulo_completo(self):
        client = self._client_con_rol("convertidores")
        resp_crear = client.post("/api/convertidores", json={"origen_tipo": "stock_taller", "marca": "rol test"})
        self.assertEqual(resp_crear.status_code, 201, resp_crear.get_data(as_text=True))
        creado = resp_crear.get_json()

        resp_listar = client.get("/api/convertidores")
        self.assertEqual(resp_listar.status_code, 200)

        resp_detalle = client.get(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp_detalle.status_code, 200)

        resp_item = client.post(
            f"/api/convertidores/{creado['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "algo", "cantidad": 1, "costo_unitario": 100},
        )
        self.assertEqual(resp_item.status_code, 201, resp_item.get_data(as_text=True))

        resp_editar = client.put(f"/api/convertidores/{creado['id']}", json={"marca": "editado"})
        self.assertEqual(resp_editar.status_code, 200)

        resp_dashboard = client.get("/api/convertidores/dashboard")
        self.assertEqual(resp_dashboard.status_code, 200)

        resp_excel = client.get("/api/convertidores/reportes/excel")
        self.assertEqual(resp_excel.status_code, 200)

        resp_eliminar = client.delete(f"/api/convertidores/{creado['id']}")
        self.assertEqual(resp_eliminar.status_code, 200, resp_eliminar.get_data(as_text=True))

    def test_rol_operativo_no_puede_operar_convertidores(self):
        # Convertidores y Operativo son departamentos separados con
        # personas distintas -- 'operativo' NO tiene acceso al módulo de
        # Convertidores (solo 'convertidores' y 'admin' lo tienen).
        client = self._client_con_rol("operativo")
        resp_crear = client.post("/api/convertidores", json={"origen_tipo": "stock_taller"})
        self.assertEqual(resp_crear.status_code, 403, resp_crear.get_data(as_text=True))
        resp_listar = client.get("/api/convertidores")
        self.assertEqual(resp_listar.status_code, 403)

    def test_convertidores_no_puede_administrar_usuarios(self):
        # Chequeo cruzado: 'convertidores' puede usar su propio módulo
        # (arriba) pero sigue sin poder administrar usuarios -- confirma que
        # requiere_rol("convertidores") no se filtró a otros endpoints por
        # error.
        client = self._client_con_rol("convertidores")
        resp = client.get("/api/admin/usuarios")
        self.assertEqual(resp.status_code, 403)

    def test_admin_puede_todo_sin_rol_asignado_explicito(self):
        resp = self.client.get("/api/convertidores/dashboard")
        self.assertEqual(resp.status_code, 200)

    def test_get_convertidor_inexistente_da_404_no_500(self):
        resp = self.client.get("/api/convertidores/999999999")
        self.assertEqual(resp.status_code, 404)

    def test_payload_con_tipos_incorrectos_da_400_no_500(self):
        # equipo_id no numérico en un origen cliente_directo válido.
        cliente = self._crear_cliente("Cliente Seguridad Payload 1")
        resp = self.client.post(
            "/api/convertidores",
            json={"origen_tipo": "cliente_directo", "cliente_id": cliente["id"], "equipo_id": "no-numero"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_precio_venta_no_numerico_da_400_no_500(self):
        # Bug real detectado al escribir esta suite (float(data["precio_venta"])
        # sin try/except en crear_convertidor/editar_convertidor, a diferencia
        # de todos los demás campos numéricos del módulo) -- ya corregido en
        # modulos/convertidores.py: ahora captura (TypeError, ValueError) y
        # responde 400 igual que el resto de los campos numéricos.
        cliente = self._crear_cliente("Cliente Seguridad Payload 2")
        resp = self.client.post(
            "/api/convertidores",
            json={"origen_tipo": "cliente_directo", "cliente_id": cliente["id"], "precio_venta": "no-numero"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_agregar_item_convertidor_hijo_id_no_numerico_da_400(self):
        creado = self._crear_convertidor_ok(origen_tipo="stock_taller")
        resp = self._agregar_item(creado["id"], tipo="convertidor", convertidor_hijo_id="no-numero")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
