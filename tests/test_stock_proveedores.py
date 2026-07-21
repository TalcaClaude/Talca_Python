"""Tests del desglose de stock por proveedor (stock_proveedores) y de las
dos correcciones pedidas junto con esa funcionalidad:
  1) Al registrar una SALIDA (venta / consumo interno / reserva / ajuste /
     devolución) desde "Registrar movimiento", ahora es obligatorio elegir
     de qué proveedor salen las unidades -- se descuenta ESE balde
     específico, no el costo promedio general del producto.
  2) Cuando la categoría es "Consumo interno" o "Reserva", el campo pasa a
     pedir un número de orden de trabajo (obligatorio para Consumo interno,
     opcional para Reserva) y el producto queda asociado a esa OT
     respetando la categoría real elegida (nunca forzada a "Consumo
     interno"), usando el costo del proveedor elegido para que el costeo de
     la OT sea el más real posible.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_stockprov_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class _BaseStockProveedores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_producto_http(self, nombre, stock_disponible=0, costo_compra=1000):
        resp = self.client.post(
            "/api/productos",
            json={"nombre": nombre, "stock_disponible": stock_disponible, "costo_compra": costo_compra},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_proveedor(self, nombre):
        resp = self.client.post("/api/proveedores", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_factura_aplicada(self, numero, producto_id, proveedor_id=None, cantidad=5, precio_unitario=1000):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "proveedor_id": proveedor_id,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_id": producto_id, "cantidad": cantidad, "precio_unitario": precio_unitario}],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        pendiente = resp.get_json()
        resp2 = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))
        return resp2.get_json()

    def _crear_cliente(self, nombre="Cliente stock proveedores"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _stock_proveedores(self, producto_id):
        resp = self.client.get(f"/api/productos/{producto_id}/stock-proveedores")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()


class TestBucketsSeLlenanSolosConLaEntrada(_BaseStockProveedores):
    def test_entrada_con_factura_de_proveedor_conocido_crea_balde_de_ese_proveedor(self):
        producto = self._crear_producto_http("Filtro de aceite - balde proveedor", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova")
        self._crear_factura_aplicada("FACT-SP-001", producto["id"], proveedor_id=casanova["id"], cantidad=4, precio_unitario=4000)

        data = self._stock_proveedores(producto["id"])
        self.assertEqual(len(data["proveedores"]), 1)
        balde = data["proveedores"][0]
        self.assertEqual(balde["proveedor_id"], casanova["id"])
        self.assertEqual(balde["cantidad"], 4)
        self.assertEqual(balde["costo_promedio"], 4000)

    def test_dos_entradas_del_mismo_proveedor_promedian_el_costo_de_ese_balde(self):
        producto = self._crear_producto_http("Filtro de aceite - balde promedio", stock_disponible=0)
        transpeed = self._crear_proveedor("Transpeed")
        self._crear_factura_aplicada("FACT-SP-002A", producto["id"], proveedor_id=transpeed["id"], cantidad=2, precio_unitario=1000)
        self._crear_factura_aplicada("FACT-SP-002B", producto["id"], proveedor_id=transpeed["id"], cantidad=2, precio_unitario=3000)

        data = self._stock_proveedores(producto["id"])
        balde = data["proveedores"][0]
        self.assertEqual(balde["cantidad"], 4)
        self.assertEqual(balde["costo_promedio"], 2000)  # (2*1000 + 2*3000) / 4

    def test_entrada_sin_factura_cae_en_balde_sin_proveedor_conocido(self):
        producto = self._crear_producto_http("Filtro de aceite - stock inicial", stock_disponible=10, costo_compra=500)

        data = self._stock_proveedores(producto["id"])
        self.assertTrue(data["permite_sin_proveedor"])
        self.assertEqual(len(data["proveedores"]), 1)
        self.assertIsNone(data["proveedores"][0]["proveedor_id"])
        self.assertEqual(data["proveedores"][0]["cantidad"], 10)

    def test_dos_proveedores_distintos_quedan_en_baldes_separados_sin_mezclar_costo(self):
        producto = self._crear_producto_http("Filtro de aceite - dos proveedores", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova SPA")
        chino = self._crear_proveedor("Proveedor Chino LTDA")
        self._crear_factura_aplicada("FACT-SP-003A", producto["id"], proveedor_id=casanova["id"], cantidad=3, precio_unitario=9000)
        self._crear_factura_aplicada("FACT-SP-003B", producto["id"], proveedor_id=chino["id"], cantidad=3, precio_unitario=2000)

        data = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b for b in data["proveedores"]}
        self.assertEqual(por_proveedor[casanova["id"]]["costo_promedio"], 9000)
        self.assertEqual(por_proveedor[chino["id"]]["costo_promedio"], 2000)
        # El CPP general del producto SÍ se mezcla (es el comportamiento de
        # siempre) -- esto confirma que no se tocó esa lógica existente.
        producto_actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(producto_actualizado["costo_compra"], 5500)  # (3*9000+3*2000)/6


class TestSalidaExigeElegirProveedor(_BaseStockProveedores):
    def test_salida_sin_proveedor_elegido_es_rechazada_si_hay_mas_de_una_opcion(self):
        producto = self._crear_producto_http("Bujía - salida sin proveedor", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Bujías")
        self._crear_factura_aplicada("FACT-SP-004", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=1000)

        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={"tipo": "salida", "categoria": "Venta", "cantidad": 1},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("proveedor", resp.get_json()["error"].lower())

    def test_salida_descuenta_solo_el_balde_del_proveedor_elegido(self):
        producto = self._crear_producto_http("Bujía - descuenta balde correcto", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Bujías 2")
        chino = self._crear_proveedor("Bujías Chinas")
        self._crear_factura_aplicada("FACT-SP-005A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=1000)
        self._crear_factura_aplicada("FACT-SP-005B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=300)

        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={"tipo": "salida", "categoria": "Venta", "cantidad": 3, "proveedor_id": casanova["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        data = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in data["proveedores"]}
        self.assertEqual(por_proveedor[casanova["id"]], 2)
        self.assertEqual(por_proveedor[chino["id"]], 5)

    def test_salida_mayor_al_balde_del_proveedor_elegido_es_rechazada_y_no_deja_nada_a_medias(self):
        producto = self._crear_producto_http("Bujía - salida excede balde", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Bujías 3")
        chino = self._crear_proveedor("Bujías Chinas 3")
        self._crear_factura_aplicada("FACT-SP-006A", producto["id"], proveedor_id=casanova["id"], cantidad=2, precio_unitario=1000)
        self._crear_factura_aplicada("FACT-SP-006B", producto["id"], proveedor_id=chino["id"], cantidad=10, precio_unitario=300)

        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={"tipo": "salida", "categoria": "Venta", "cantidad": 5, "proveedor_id": casanova["id"]},
        )
        self.assertEqual(resp.status_code, 400)

        producto_actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(producto_actualizado["stock_disponible"], 12)  # nada se descontó
        data = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in data["proveedores"]}
        self.assertEqual(por_proveedor[casanova["id"]], 2)  # el balde no se tocó


class TestSalidaConOrdenDeTrabajo(_BaseStockProveedores):
    def _preparar_producto_y_ot_con_proveedor(self, sufijo, costo=5000):
        producto = self._crear_producto_http(f"Repuesto OT {sufijo}", stock_disponible=0)
        proveedor = self._crear_proveedor(f"Proveedor OT {sufijo}")
        self._crear_factura_aplicada(f"FACT-OT-{sufijo}", producto["id"], proveedor_id=proveedor["id"], cantidad=10, precio_unitario=costo)
        cliente = self._crear_cliente(f"Cliente OT {sufijo}")
        orden = self._crear_orden(cliente["id"])
        return producto, proveedor, orden

    def test_consumo_interno_sin_numero_de_ot_es_rechazado(self):
        producto, proveedor, _orden = self._preparar_producto_y_ot_con_proveedor("A")
        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={"tipo": "salida", "categoria": "Consumo interno", "cantidad": 1, "proveedor_id": proveedor["id"]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("orden de trabajo", resp.get_json()["error"].lower())

    def test_consumo_interno_con_ot_queda_asociado_como_repuesto_de_la_orden(self):
        producto, proveedor, orden = self._preparar_producto_y_ot_con_proveedor("B", costo=7000)
        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={
                "tipo": "salida", "categoria": "Consumo interno", "cantidad": 2,
                "proveedor_id": proveedor["id"], "orden_id": orden["id"],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        orden_actualizada = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_repuesto = [i for i in orden_actualizada["items"] if i["tipo"] == "repuesto" and i["producto_id"] == producto["id"]]
        self.assertEqual(len(items_repuesto), 1)
        self.assertEqual(items_repuesto[0]["cantidad"], 2)
        # El costo que queda en la OT es el del proveedor elegido, no un CPP
        # ciego -- esto es justo lo que se pidió ("que la orden de trabajo
        # quede con el costo más real posible").
        self.assertEqual(items_repuesto[0]["costo_unitario"], 7000)

        # El movimiento de stock queda con la categoría REAL elegida, no
        # forzado a "Consumo interno" por casualidad de que coincida.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            mov = db.execute(
                "SELECT * FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1", (producto["id"],)
            ).fetchone()
        self.assertEqual(mov["categoria"], "Consumo interno")

    def test_reserva_con_ot_respeta_la_categoria_reserva_y_no_la_fuerza_a_consumo_interno(self):
        producto, proveedor, orden = self._preparar_producto_y_ot_con_proveedor("C")
        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={
                "tipo": "salida", "categoria": "Reserva", "cantidad": 1,
                "proveedor_id": proveedor["id"], "orden_id": orden["id"],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        with taller_app.app.app_context():
            db = taller_app.get_db()
            mov = db.execute(
                "SELECT * FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1", (producto["id"],)
            ).fetchone()
        self.assertEqual(mov["categoria"], "Reserva")

        orden_actualizada = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        items_repuesto = [i for i in orden_actualizada["items"] if i["tipo"] == "repuesto" and i["producto_id"] == producto["id"]]
        self.assertIn("Reserva", items_repuesto[0]["detalle"])  # distingue reserva de un consumo directo

    def test_reserva_sin_numero_de_ot_es_valida_porque_puede_ser_para_una_ot_futura(self):
        producto, proveedor, _orden = self._preparar_producto_y_ot_con_proveedor("D")
        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={"tipo": "salida", "categoria": "Reserva", "cantidad": 1, "proveedor_id": proveedor["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

    def test_ot_inexistente_devuelve_error_claro(self):
        producto, proveedor, _orden = self._preparar_producto_y_ot_con_proveedor("E")
        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={
                "tipo": "salida", "categoria": "Consumo interno", "cantidad": 1,
                "proveedor_id": proveedor["id"], "orden_id": 999999,
            },
        )
        self.assertEqual(resp.status_code, 404)

    def test_ot_cerrada_no_admite_mas_repuestos(self):
        producto, proveedor, orden = self._preparar_producto_y_ot_con_proveedor("F")
        resp_cancel = self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "cancelado"})
        self.assertEqual(resp_cancel.status_code, 200, resp_cancel.get_data(as_text=True))

        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={
                "tipo": "salida", "categoria": "Consumo interno", "cantidad": 1,
                "proveedor_id": proveedor["id"], "orden_id": orden["id"],
            },
        )
        self.assertEqual(resp.status_code, 409)


class TestPerdidaSigueDescontandoElBalde(_BaseStockProveedores):
    def test_perdida_descuenta_del_balde_del_proveedor_elegido_para_la_perdida(self):
        producto = self._crear_producto_http("Correa - perdida balde", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Correas")
        self._crear_factura_aplicada("FACT-SP-PERD-001", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=1000)

        resp = self.client.post(
            f"/api/productos/{producto['id']}/movimiento",
            json={"tipo": "perdida", "cantidad": 2, "proveedor_id": casanova["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        data = self._stock_proveedores(producto["id"])
        self.assertEqual(data["proveedores"][0]["cantidad"], 3)


if __name__ == "__main__":
    unittest.main()
