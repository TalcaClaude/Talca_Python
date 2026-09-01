"""Tests del "tipo de cambio definitivo" de facturas internacionales (pedido
del taller, septiembre 2026): el dólar que se tipea al ingresar una factura
internacional es siempre TENTATIVO -- el real recién se sabe a fin de mes,
al pagar la tarjeta en dólares. Estos tests cubren:

  1. Cada factura internacional queda en su propio balde de stock_proveedores
     (Migración 38), separado de las compras nacionales del mismo proveedor
     -- las nacionales se siguen mezclando exactamente igual que siempre.
  2. Al agregar un repuesto a una OT con más de un balde vivo (nacional +
     internacional, o dos facturas internacionales distintas), el sistema
     pregunta cuál -- y el balde elegido queda registrado en el ítem
     (orden_items.factura_internacional_id).
  3. Confirmar el tipo de cambio definitivo (POST
     /api/facturas/<id>/tipo-cambio-definitivo) recalcula el costo de cada
     línea y lo propaga a: factura_lineas.precio_unitario, el balde de
     stock_proveedores todavía vivo, y el costo_unitario de CUALQUIER ítem
     de OT que haya consumido ese balde -- incluso si la OT ya está
     cerrada, porque el Estado de Resultados lee el costo desde ahí.
  4. Bloqueado si la Cuenta por Pagar de la factura ya tiene abonos.
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_fitcd_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class TestTipoCambioDefinitivo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_producto(self, nombre):
        resp = self.client.post("/api/productos", json={"nombre": nombre, "stock_disponible": 0, "costo_compra": 0})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_proveedor(self, nombre):
        resp = self.client.post("/api/proveedores", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_cliente(self):
        return self.client.post("/api/operativo/clientes", json={"nombre": "Cliente FI-TCD"}).get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_factura_internacional(self, numero, proveedor_id, producto_id, cantidad, costo_usd, tipo_cambio=1000, shipping_usd=0):
        resp = self.client.post(
            "/api/facturas/internacional",
            json={
                "numero": numero,
                "proveedor_id": proveedor_id,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "tipo_cambio": tipo_cambio,
                "shipping_usd": shipping_usd,
                "paypal_usd": 0,
                "dhl_clp": 0,
                "condicion_pago": "credito",
                "lineas": [{"producto_id": producto_id, "cantidad": cantidad, "costo_unitario_usd": costo_usd}],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _comprar_nacional(self, numero, proveedor_id, producto_id, cantidad, precio_unitario):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "proveedor_id": proveedor_id,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "condicion_pago": "credito",
                "lineas": [{"producto_id": producto_id, "cantidad": cantidad, "precio_unitario": precio_unitario}],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_factura_internacional_no_se_mezcla_con_compra_nacional_del_mismo_proveedor(self):
        producto = self._crear_producto("Repuesto FI-TCD balde separado")
        proveedor = self._crear_proveedor("Proveedor FI-TCD balde separado")

        self._comprar_nacional(f"NAC-{producto['id']}", proveedor["id"], producto["id"], 5, 10000)
        self._crear_factura_internacional(f"FI-{producto['id']}", proveedor["id"], producto["id"], 5, 100, tipo_cambio=900)

        baldes = self.client.get(f"/api/productos/{producto['id']}/stock-proveedores").get_json()["proveedores"]
        # Dos baldes vivos del MISMO proveedor: el nacional (factura_id None)
        # y el internacional (factura_id puesto) -- nunca se mezclan.
        self.assertEqual(len(baldes), 2)
        con_factura = [b for b in baldes if b["factura_id"]]
        sin_factura = [b for b in baldes if not b["factura_id"]]
        self.assertEqual(len(con_factura), 1)
        self.assertEqual(len(sin_factura), 1)
        self.assertEqual(sin_factura[0]["costo_promedio"], 10000)
        self.assertEqual(con_factura[0]["cantidad"], 5)

    def test_agregar_a_ot_con_baldes_ambiguos_pide_elegir_y_registra_la_factura_elegida(self):
        producto = self._crear_producto("Repuesto FI-TCD ambiguo")
        proveedor = self._crear_proveedor("Proveedor FI-TCD ambiguo")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])

        self._comprar_nacional(f"NAC-AMB-{producto['id']}", proveedor["id"], producto["id"], 5, 10000)
        creada = self._crear_factura_internacional(f"FI-AMB-{producto['id']}", proveedor["id"], producto["id"], 5, 100, tipo_cambio=900)

        # Sin elegir: el backend pide el balde, y entre las opciones debe
        # venir la internacional con su factura_id/numero.
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        )
        self.assertEqual(resp.status_code, 400)
        cuerpo = resp.get_json()
        self.assertTrue(cuerpo["requiere_proveedor"])
        balde_intl = next(b for b in cuerpo["proveedores"] if b["factura_id"])
        self.assertEqual(balde_intl["factura_id"], creada["factura_id"])

        # Eligiendo el balde internacional (proveedor_id + factura_id): se
        # agrega y queda registrado en el ítem.
        resp2 = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1,
                  "proveedor_id": proveedor["id"], "factura_id": creada["factura_id"]},
        )
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))
        item = resp2.get_json()
        self.assertEqual(item["factura_internacional_id"], creada["factura_id"])

    def test_confirmar_tipo_cambio_definitivo_corrige_factura_stock_y_ot_cerrada(self):
        producto = self._crear_producto("Repuesto FI-TCD definitivo")
        proveedor = self._crear_proveedor("Proveedor FI-TCD definitivo")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])

        # Factura internacional de 4 unidades, costo_unitario_usd=400 (el
        # precio de TODA la línea en dólares, no por unidad individual -- ver
        # el "OJO" en _calcular_prorrateo_internacional), dólar provisorio
        # 900 (sin flete/paypal/dhl para simplificar la cuenta): costo final
        # por unidad = (400*900)/4 = 90.000.
        creada = self._crear_factura_internacional(
            f"FI-DEF-{producto['id']}", proveedor["id"], producto["id"], 4, 400, tipo_cambio=900,
        )
        factura_id = creada["factura_id"]
        costo_provisorio = self.client.get(f"/api/facturas/{factura_id}").get_json()["lineas"][0]["precio_unitario"]
        self.assertEqual(costo_provisorio, 90000)  # 100 usd * 900

        # Se consume 1 unidad en una OT que después se cierra -- el costo
        # queda "congelado" en orden_items.costo_unitario al momento de
        # consumir.
        item = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 1},
        ).get_json()
        self.assertEqual(item["costo_unitario"], 90000)
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        orden_cerrada = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(orden_cerrada["estado"], "entregado")

        # Ahora se sabe el dólar real: 950 en vez de 900.
        resp = self.client.post(
            f"/api/facturas/{factura_id}/tipo-cambio-definitivo",
            json={"tipo_cambio_definitivo": 950},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        resultado = resp.get_json()
        self.assertEqual(resultado["items_ot_garantia_corregidos"], 1)

        # factura_lineas: (400*950)/4 = 95.000.
        factura_actualizada = self.client.get(f"/api/facturas/{factura_id}").get_json()
        self.assertEqual(factura_actualizada["lineas"][0]["precio_unitario"], 95000)

        # El balde de stock todavía vivo (quedan 3 unidades sin consumir)
        # se corrige al costo nuevo.
        baldes = self.client.get(f"/api/productos/{producto['id']}/stock-proveedores").get_json()["proveedores"]
        balde_intl = next(b for b in baldes if b["factura_id"] == factura_id)
        self.assertEqual(balde_intl["costo_promedio"], 95000)
        self.assertEqual(balde_intl["cantidad"], 3)

        # El ítem de la OT YA CERRADA queda con el costo real, no el
        # provisorio -- es lo que pidió el taller explícitamente ("es
        # fundamental que el costo sea el real... con eso se calcula el
        # costo final del mes y de cada OT").
        orden_final = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(orden_final["items"][0]["costo_unitario"], 95000)

        # El flag queda marcado como definitivo, con el valor confirmado.
        pendientes = self.client.get("/api/facturas/pendientes-recepcion").get_json()["pendientes"]
        propia = next(p for p in pendientes if p.get("factura_id") == factura_id)
        self.assertTrue(propia["internacional"]["definitivo"])
        self.assertEqual(propia["internacional"]["tipo_cambio_definitivo"], 950)

    def test_confirmar_tipo_cambio_definitivo_actualiza_el_monto_en_la_lista_de_facturas(self):
        """La lista de Facturas (y el pendiente antes de abrir el detalle)
        calculan su monto EN VIVO desde facturas_finanzas_pendientes.lineas_json
        (ver _factura_pendiente_recepcion_dict) -- si esta corrección no
        actualizara también esa copia, la lista seguiría mostrando el monto
        viejo aunque la factura real ya tuviera el correcto."""
        producto = self._crear_producto("Repuesto FI-TCD monto lista")
        proveedor = self._crear_proveedor("Proveedor FI-TCD monto lista")
        creada = self._crear_factura_internacional(
            f"FI-LISTA-{producto['id']}", proveedor["id"], producto["id"], 4, 400, tipo_cambio=900,
        )
        factura_id = creada["factura_id"]

        pendientes_antes = self.client.get("/api/facturas/pendientes-recepcion").get_json()["pendientes"]
        propia_antes = next(p for p in pendientes_antes if p.get("factura_id") == factura_id)
        monto_antes = propia_antes["monto_total"]

        self.client.post(f"/api/facturas/{factura_id}/tipo-cambio-definitivo", json={"tipo_cambio_definitivo": 950})

        pendientes_despues = self.client.get("/api/facturas/pendientes-recepcion").get_json()["pendientes"]
        propia_despues = next(p for p in pendientes_despues if p.get("factura_id") == factura_id)
        factura_real = self.client.get(f"/api/facturas/{factura_id}").get_json()

        self.assertNotEqual(propia_despues["monto_total"], monto_antes)
        self.assertEqual(propia_despues["monto_total"], factura_real["monto_total"])
        self.assertEqual(propia_despues["lineas"][0]["precio_unitario"], 95000)  # (400*950)/4

    def test_confirmar_tipo_cambio_definitivo_bloqueado_si_ya_hay_abonos(self):
        producto = self._crear_producto("Repuesto FI-TCD con abono")
        proveedor = self._crear_proveedor("Proveedor FI-TCD con abono")
        creada = self._crear_factura_internacional(
            f"FI-ABONO-{producto['id']}", proveedor["id"], producto["id"], 2, 100, tipo_cambio=900,
        )
        factura_id = creada["factura_id"]

        cxp = self.client.get("/api/finanzas/cxp").get_json()["cuentas"]
        cxp_propia = next(c for c in cxp if c.get("factura_id") == factura_id)
        resp_abono = self.client.post(
            f"/api/finanzas/cxp/{cxp_propia['id']}/pagos",
            json={"monto": 1000, "fecha": datetime.now().strftime("%Y-%m-%d"), "metodo_pago": "transferencia"},
        )
        self.assertEqual(resp_abono.status_code, 201, resp_abono.get_data(as_text=True))

        resp = self.client.post(
            f"/api/facturas/{factura_id}/tipo-cambio-definitivo",
            json={"tipo_cambio_definitivo": 950},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("abono", resp.get_json()["error"].lower())

    def test_endpoint_rechaza_factura_que_no_es_internacional(self):
        producto = self._crear_producto("Repuesto FI-TCD no internacional")
        proveedor = self._crear_proveedor("Proveedor FI-TCD no internacional")
        nacional = self._comprar_nacional(f"NAC-SOLO-{producto['id']}", proveedor["id"], producto["id"], 1, 5000)
        factura_id = nacional["factura_id"]
        self.assertIsNotNone(factura_id)

        resp = self.client.post(
            f"/api/facturas/{factura_id}/tipo-cambio-definitivo",
            json={"tipo_cambio_definitivo": 950},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("internacional", resp.get_json()["error"].lower())


if __name__ == "__main__":
    unittest.main()
