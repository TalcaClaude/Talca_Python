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


class TestCargaDeRepuestosEnOtExigeProveedorConMasDeUno(_BaseStockProveedores):
    """Auditoría de julio 2026: al agregar un repuesto desde la pestaña
    "Carga de repuestos" de una OT (agregar_item_orden en operativo.py), si
    el producto tenía stock de más de un proveedor a la vez, el sistema
    elegía uno solo "mejor esfuerzo" sin preguntar (ver
    _stockprov_descontar_automatico) y usaba el Costo Promedio Ponderado
    general del producto, no el costo real del proveedor efectivamente
    descontado. Ahora, con más de un proveedor, hay que indicar
    explícitamente de cuál -- con 0 o 1 proveedor, el comportamiento sigue
    siendo exactamente el de antes (no se pide nada)."""

    def test_con_un_solo_proveedor_no_pide_elegir_y_se_comporta_como_antes(self):
        producto = self._crear_producto_http("Rodamiento - un solo proveedor", stock_disponible=0)
        proveedor = self._crear_proveedor("Único Proveedor SpA")
        self._crear_factura_aplicada("FACT-CR-001", producto["id"], proveedor_id=proveedor["id"], cantidad=5, precio_unitario=3000)
        cliente = self._crear_cliente("Cliente carga repuestos A")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 2},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["costo_unitario"], 3000)

    def test_con_dos_proveedores_sin_elegir_uno_es_rechazado(self):
        producto = self._crear_producto_http("Rodamiento - dos proveedores OT", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Rodamientos")
        chino = self._crear_proveedor("Proveedor Chino Rodamientos")
        self._crear_factura_aplicada("FACT-CR-002A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-CR-002B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente carga repuestos B")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertTrue(data["requiere_proveedor"])
        self.assertEqual(len(data["proveedores"]), 2)

        # No se aplicó nada -- ni se agregó el ítem ni se tocó el stock.
        orden_actualizada = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(len(orden_actualizada["items"]), 0)
        stock_actual = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in stock_actual["proveedores"]}
        self.assertEqual(por_proveedor[casanova["id"]], 5)
        self.assertEqual(por_proveedor[chino["id"]], 5)

    def test_con_dos_proveedores_eligiendo_uno_usa_su_costo_y_descuenta_ese_balde(self):
        producto = self._crear_producto_http("Rodamiento - elige proveedor OT", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Rodamientos 2")
        chino = self._crear_proveedor("Proveedor Chino Rodamientos 2")
        self._crear_factura_aplicada("FACT-CR-003A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-CR-003B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente carga repuestos C")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 2, "proveedor_id": chino["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()
        # El costo que queda en la OT es el del proveedor Chino (1500), NO
        # el Costo Promedio Ponderado general del producto (que mezcla
        # ambos proveedores).
        self.assertEqual(item["costo_unitario"], 1500)

        stock_actual = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in stock_actual["proveedores"]}
        self.assertEqual(por_proveedor[chino["id"]], 3)  # 5 - 2
        self.assertEqual(por_proveedor[casanova["id"]], 5)  # intacto

    def test_con_proveedor_que_no_tiene_stock_de_este_producto_es_rechazado(self):
        producto = self._crear_producto_http("Rodamiento - proveedor invalido OT", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Rodamientos 3")
        chino = self._crear_proveedor("Proveedor Chino Rodamientos 3")
        otro = self._crear_proveedor("Proveedor Sin Stock De Este Producto")
        self._crear_factura_aplicada("FACT-CR-004A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-CR-004B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente carga repuestos D")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1, "proveedor_id": otro["id"]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json()["requiere_proveedor"])

    def test_costo_explicito_sigue_teniendo_prioridad_sobre_el_del_proveedor_elegido(self):
        producto = self._crear_producto_http("Rodamiento - costo explicito OT", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Rodamientos 4")
        chino = self._crear_proveedor("Proveedor Chino Rodamientos 4")
        self._crear_factura_aplicada("FACT-CR-005A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-CR-005B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente carga repuestos E")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={
                "tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1,
                "proveedor_id": chino["id"], "costo_unitario": 9999,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["costo_unitario"], 9999)
        # El balde igual se descontó del proveedor elegido -- el costo
        # explícito solo cambia lo que queda registrado en la OT, no de
        # dónde sale el stock.
        stock_actual = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in stock_actual["proveedores"]}
        self.assertEqual(por_proveedor[chino["id"]], 4)

    def test_con_stock_sin_proveedor_conocido_mas_un_proveedor_real_tambien_pide_elegir(self):
        # "Sin proveedor conocido" (stock inicial/sin factura) cuenta como
        # un balde más -- si además hay un proveedor real con stock, sigue
        # siendo "más de un proveedor a la vez" y hay que preguntar.
        producto = self._crear_producto_http("Rodamiento - sin proveedor + real", stock_disponible=3, costo_compra=1000)
        real = self._crear_proveedor("Proveedor Real Con Stock")
        self._crear_factura_aplicada("FACT-CR-006", producto["id"], proveedor_id=real["id"], cantidad=4, precio_unitario=2500)
        cliente = self._crear_cliente("Cliente carga repuestos F")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json()["requiere_proveedor"])

        # Eligiendo "sin proveedor conocido" explícitamente (null) también
        # funciona.
        resp2 = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1, "proveedor_id": None},
        )
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))
        self.assertEqual(resp2.get_json()["costo_unitario"], 1000)


class TestCargaDeRepuestosEnGarantiaExigeProveedorConMasDeUno(_BaseStockProveedores):
    """Mismo criterio que TestCargaDeRepuestosEnOtExigeProveedorConMasDeUno,
    pero para los ítems de una Garantía (agregar_item_garantia) -- ahí
    importa todavía más saber con certeza de qué proveedor salió el
    repuesto, porque es justo el caso de "el auto volvió por un problema"."""

    def _crear_garantia(self, cliente_id):
        resp = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente_id, "motivo": "Reclamo de repuesto en garantia"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_con_dos_proveedores_sin_elegir_uno_es_rechazado(self):
        producto = self._crear_producto_http("Sello - dos proveedores garantia", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Sellos")
        chino = self._crear_proveedor("Proveedor Chino Sellos")
        self._crear_factura_aplicada("FACT-GAR-001A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-GAR-001B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente garantia A")
        garantia = self._crear_garantia(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/garantias/{garantia['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json()["requiere_proveedor"])

    def test_con_dos_proveedores_eligiendo_uno_usa_su_costo_y_descuenta_ese_balde(self):
        producto = self._crear_producto_http("Sello - elige proveedor garantia", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova Sellos 2")
        chino = self._crear_proveedor("Proveedor Chino Sellos 2")
        self._crear_factura_aplicada("FACT-GAR-002A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-GAR-002B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente garantia B")
        garantia = self._crear_garantia(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/garantias/{garantia['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1, "proveedor_id": casanova["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["costo_unitario"], 4000)

        stock_actual = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in stock_actual["proveedores"]}
        self.assertEqual(por_proveedor[casanova["id"]], 4)
        self.assertEqual(por_proveedor[chino["id"]], 5)


class TestCostoUnitarioUsaBaldeEspecificoNoPromedioGlobal(_BaseStockProveedores):
    """Reportado por el taller (julio 2026) sobre el desglose por
    proveedor: al ingresar un lote caro de un repuesto que ya tenía stock
    barato de otro proveedor, el Costo Promedio Ponderado GENERAL del
    producto (productos.costo_compra) sube como corresponde -- pero ese
    número es una media móvil de TODA la historia de compras, así que
    puede quedar "pegado" a un valor que ya no corresponde a ninguna
    unidad realmente disponible (ej. si el balde caro que lo hizo subir ya
    se agotó, el único balde que queda vivo es el barato, pero el CPP
    general sigue reflejando la mezcla histórica). Con exactamente UN
    balde vivo no debe haber ambigüedad: el costo tiene que ser el de ESE
    balde, nunca el CPP general."""

    def test_con_un_solo_balde_vivo_usa_su_costo_aunque_el_promedio_general_este_mas_alto(self):
        producto = self._crear_producto_http("Friction Kit 01J", stock_disponible=0)
        barato = self._crear_proveedor("Proveedor Barato FK")
        caro = self._crear_proveedor("Proveedor Caro FK")
        # 5 unidades a 1.000 (barato) + 5 a 5.000 (caro) -> CPP general
        # queda en 3.000, muy por sobre el costo real del balde barato.
        self._crear_factura_aplicada("FACT-CPP-001A", producto["id"], proveedor_id=barato["id"], cantidad=5, precio_unitario=1000)
        self._crear_factura_aplicada("FACT-CPP-001B", producto["id"], proveedor_id=caro["id"], cantidad=5, precio_unitario=5000)

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["costo_compra"], 3000)

        cliente = self._crear_cliente("Cliente CPP A")
        orden = self._crear_orden(cliente["id"])

        # Se agota por completo el balde caro (eligiéndolo explícitamente,
        # como exige el sistema con más de un proveedor con stock vivo) --
        # después de esto, el ÚNICO balde que queda vivo es el barato.
        resp_drena = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 5, "proveedor_id": caro["id"]},
        )
        self.assertEqual(resp_drena.status_code, 201, resp_drena.get_data(as_text=True))
        self.assertEqual(resp_drena.get_json()["costo_unitario"], 5000)

        # El CPP general del producto NUNCA baja con una salida -- sigue en
        # 3.000, un número que ya no corresponde a ninguna unidad viva.
        actualizado2 = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado2["costo_compra"], 3000)

        # Ahora se agrega OTRO ítem del mismo repuesto -- con un solo
        # balde vivo (el barato) no debería preguntar nada, y el costo que
        # quede en la OT tiene que ser el REAL del balde barato (1.000),
        # nunca los 3.000 del CPP general que ya no reflejan nada vivo.
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["costo_unitario"], 1000)

        stock_actual = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in stock_actual["proveedores"]}
        self.assertEqual(por_proveedor[barato["id"]], 4)
        self.assertNotIn(caro["id"], por_proveedor)


class TestEliminarItemDevuelveStockAlBaldeDeProveedorCorrecto(_BaseStockProveedores):
    """Reportado por el taller (julio 2026): al eliminar un ítem de
    repuesto de una OT/garantía, el stock se devolvía SIEMPRE al balde
    "sin proveedor conocido" sin importar de cuál proveedor había salido
    en realidad -- el producto "volvía" al sistema con proveedor genérico
    y costo promediado, perdiendo la trazabilidad real. Ahora el ítem
    guarda de qué balde salió (orden_items.proveedor_id /
    garantia_items.proveedor_id) y la devolución va exactamente de vuelta
    a ese mismo balde."""

    def test_eliminar_item_de_ot_devuelve_al_balde_del_proveedor_elegido_no_al_generico(self):
        producto = self._crear_producto_http("Oil Filter 01J", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova OF")
        chino = self._crear_proveedor("Proveedor Chino OF")
        self._crear_factura_aplicada("FACT-ELIM-001A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-ELIM-001B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente eliminar item A")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 2, "proveedor_id": chino["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()
        self.assertEqual(item["proveedor_id"], chino["id"])

        antes = self._stock_proveedores(producto["id"])
        por_proveedor_antes = {b["proveedor_id"]: b["cantidad"] for b in antes["proveedores"]}
        self.assertEqual(por_proveedor_antes[chino["id"]], 3)
        self.assertEqual(por_proveedor_antes[casanova["id"]], 5)

        resp_del = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/items/{item['id']}")
        self.assertEqual(resp_del.status_code, 200, resp_del.get_data(as_text=True))

        despues = self._stock_proveedores(producto["id"])
        por_proveedor_despues = {b["proveedor_id"]: b["cantidad"] for b in despues["proveedores"]}
        # Vuelve exactamente al balde de Chino (de donde había salido), no
        # al balde "sin proveedor conocido" ni al de Casanova.
        self.assertEqual(por_proveedor_despues[chino["id"]], 5)
        self.assertEqual(por_proveedor_despues[casanova["id"]], 5)
        self.assertNotIn(None, por_proveedor_despues)

    def test_agregar_eliminar_y_volver_a_agregar_mantiene_la_trazabilidad_por_proveedor(self):
        # Reproduce el caso puntual descrito por el taller: cargar un
        # repuesto en la OT, eliminarlo, y volver a cargarlo -- el sistema
        # tiene que seguir sabiendo cuántas unidades y a qué costo tiene
        # cada proveedor, sin que aparezca un balde "genérico" fantasma.
        producto = self._crear_producto_http("Steel Kit 01J", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova SK")
        chino = self._crear_proveedor("Proveedor Chino SK")
        self._crear_factura_aplicada("FACT-ELIM-002A", producto["id"], proveedor_id=casanova["id"], cantidad=3, precio_unitario=800000)
        self._crear_factura_aplicada("FACT-ELIM-002B", producto["id"], proveedor_id=chino["id"], cantidad=3, precio_unitario=200000)
        cliente = self._crear_cliente("Cliente eliminar item B")
        orden = self._crear_orden(cliente["id"])

        resp1 = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1, "proveedor_id": casanova["id"]},
        )
        self.assertEqual(resp1.status_code, 201, resp1.get_data(as_text=True))
        item1 = resp1.get_json()

        resp_del = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/items/{item1['id']}")
        self.assertEqual(resp_del.status_code, 200)

        # Vuelve a estar íntegro: 3 y 3, ningún balde "sin proveedor".
        stock_restaurado = self._stock_proveedores(producto["id"])
        por_proveedor = {b["proveedor_id"]: b["cantidad"] for b in stock_restaurado["proveedores"]}
        self.assertEqual(por_proveedor[casanova["id"]], 3)
        self.assertEqual(por_proveedor[chino["id"]], 3)
        self.assertNotIn(None, por_proveedor)

        # Se vuelve a cargar -- sigue habiendo 2 proveedores con stock
        # vivo, así que el sistema tiene que volver a pedir cuál (no
        # "adivinar" ni caer en un balde genérico).
        resp2_sin_elegir = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        )
        self.assertEqual(resp2_sin_elegir.status_code, 400)
        self.assertTrue(resp2_sin_elegir.get_json()["requiere_proveedor"])

        resp2 = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1, "proveedor_id": chino["id"]},
        )
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))
        self.assertEqual(resp2.get_json()["costo_unitario"], 200000)

    def test_eliminar_item_de_garantia_devuelve_al_balde_del_proveedor_elegido(self):
        producto = self._crear_producto_http("Brake Pad 01J", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova BP")
        chino = self._crear_proveedor("Proveedor Chino BP")
        self._crear_factura_aplicada("FACT-ELIM-003A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-ELIM-003B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente eliminar item garantia")
        resp_gar = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "motivo": "Prueba eliminar item garantia"},
        )
        self.assertEqual(resp_gar.status_code, 201, resp_gar.get_data(as_text=True))
        garantia = resp_gar.get_json()

        resp = self.client.post(
            f"/api/operativo/garantias/{garantia['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 2, "proveedor_id": chino["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()
        self.assertEqual(item["proveedor_id"], chino["id"])

        resp_del = self.client.delete(f"/api/operativo/garantias/{garantia['id']}/items/{item['id']}")
        self.assertEqual(resp_del.status_code, 200, resp_del.get_data(as_text=True))

        despues = self._stock_proveedores(producto["id"])
        por_proveedor_despues = {b["proveedor_id"]: b["cantidad"] for b in despues["proveedores"]}
        self.assertEqual(por_proveedor_despues[chino["id"]], 5)
        self.assertEqual(por_proveedor_despues[casanova["id"]], 5)
        self.assertNotIn(None, por_proveedor_despues)

    def test_eliminar_orden_completa_devuelve_stock_al_balde_correcto(self):
        producto = self._crear_producto_http("Gasket 01J", stock_disponible=0)
        casanova = self._crear_proveedor("Casanova GK")
        chino = self._crear_proveedor("Proveedor Chino GK")
        self._crear_factura_aplicada("FACT-ELIM-004A", producto["id"], proveedor_id=casanova["id"], cantidad=5, precio_unitario=4000)
        self._crear_factura_aplicada("FACT-ELIM-004B", producto["id"], proveedor_id=chino["id"], cantidad=5, precio_unitario=1500)
        cliente = self._crear_cliente("Cliente eliminar orden completa")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 2, "proveedor_id": chino["id"]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        resp_del = self.client.delete(f"/api/operativo/ordenes/{orden['id']}")
        self.assertEqual(resp_del.status_code, 200, resp_del.get_data(as_text=True))

        despues = self._stock_proveedores(producto["id"])
        por_proveedor_despues = {b["proveedor_id"]: b["cantidad"] for b in despues["proveedores"]}
        self.assertEqual(por_proveedor_despues[chino["id"]], 5)
        self.assertEqual(por_proveedor_despues[casanova["id"]], 5)
        self.assertNotIn(None, por_proveedor_despues)


if __name__ == "__main__":
    unittest.main()
