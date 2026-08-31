"""Tests de "Agregar factura internacional" (Finanzas -> Facturas): reemplaza
el Excel que el taller usaba para prorratear flete, PayPal y agente de
aduana (DHL) entre los productos de una importación y convertir a pesos.

Cubre:
  1. _calcular_prorrateo_internacional() replica EXACTAMENTE, línea por
     línea, los resultados del Excel real que el taller compartió
     ("GUIAS DE IMPORTACION PARA AGREGAR A JULIO TRANSPEED.xlsx", hoja
     'TRANSPEED 9062026') -- las 69 líneas de producto de esa factura real
     (cantidad, costo unitario USD, columna S = costo final por unidad en
     pesos) quedaron hardcodeadas acá como fixture, extraídas directo del
     Excel con openpyxl (data_only=True) para no depender del archivo
     subido por el usuario en tiempo de test.
  2. Casos borde de la validación (tipo de cambio inválido, costos en 0,
     línea de gasto general rechazada).
  3. POST /api/facturas/internacional de punta a punta: crea productos,
     registra una factura internacional chica y confirma que el costo que
     terminó en el catálogo/stock es el prorrateado en pesos (no el
     ingresado en dólares), que se generó la Cuenta por Pagar, y que el
     desglose queda guardado para poder mostrarlo en "Ver factura".
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_facturaintl_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402

# (cantidad, costo_unitario_usd, costo_final_unit_clp_excel) -- una fila por
# cada producto de la factura real TRANSPEED 9062026, en el mismo orden en
# que aparecen en el Excel. costo_final_unit_clp_excel es la columna S
# (=H/G) SIN redondear, tal como la calculó Excel con flotantes de doble
# precisión -- el sistema redondea al peso más cercano, así que se compara
# contra round(valor_excel).
LINEAS_TRANSPEED = [
    (3, 140.5, 70365.30094734863), (3, 140.5, 70365.30094734863), (3, 140.5, 70365.30094734863),
    (10, 170, 25541.853012916585), (3, 1320, 661083.2544519586), (3, 176.25, 88269.63908875584),
    (3, 179.34, 89817.1748889502), (3, 108.21, 54193.802245641244), (3, 98, 49080.42343658482),
    (3, 98, 49080.42343658482), (3, 98, 49080.42343658482), (15, 576.25, 57719.57960271837),
    (15, 576.25, 57719.57960271837), (15, 576.25, 57719.57960271837), (15, 1230, 123201.87923877411),
    (15, 54.75, 5483.986088067384), (15, 54.75, 5483.986088067384), (3, 340.14, 170349.13497673426),
    (3, 309.21, 154858.7523553713), (3, 185.52, 92912.24648933893), (10, 35, 5258.616796776944),
    (15, 38.25, 3831.277951937488), (5, 221.85, 66664.23636371228), (5, 123.1, 36990.61301047096),
    (10, 35.7, 5363.789132712484), (10, 35.7, 5363.789132712484), (15, 35.4, 3545.810182969597),
    (8, 115.44, 21680.525822140375), (10, 37.1, 5574.13380458356), (20, 66.6, 5003.198266647778),
    (20, 82.4, 6190.143200777432), (12, 237.721, 29763.89627491933), (12, 237.721, 29763.89627491933),
    (12, 237.721, 29763.89627491933), (5, 166.15, 49926.81033054224), (2, 65.473, 49185.34536219669),
    (2, 65.473, 49185.34536219669), (2, 65.473, 49185.34536219669), (15, 37.5, 3756.154854840674),
    (10, 36.7, 5514.035326906111), (5, 193.9, 58265.474108288545), (10, 33.5, 5033.2475054865035),
    (3, 155, 77627.20033337393), (3, 155, 77627.20033337393), (3, 155, 77627.20033337393),
    (3, 356.67, 178627.7002768031), (15, 60, 6009.847767745078), (20, 40, 3004.9238838725396),
    (2, 476.92, 358277.07467412297), (2, 530.34, 398407.83314324066), (2, 342.24, 257101.28750413447),
    (10, 33.2, 4988.173647228416), (10, 39.8, 5979.798528906353), (2, 983.68, 738970.8815219349),
    (10, 56.3, 8458.860733101199), (7, 23.24, 4988.173647228416), (7, 24.5, 5258.616796776944),
    (3, 54, 27044.314954852856), (10, 35, 5258.616796776944), (10, 23.5, 3530.7855635502337),
    (5, 153.83, 46224.74410561128), (5, 153.83, 46224.74410561128), (5, 153.84, 46227.74902949515),
    (5, 16.25, 4883.001311292876), (5, 323.05, 97074.0660685024), (5, 207.5, 62352.17059035519),
    (5, 276.15, 82980.97305314017), (4, 480, 180295.43303235236), (5, 18.75, 5634.232282261011),
]
TRANSPEED_SHIPPING_USD = 4784
TRANSPEED_PAYPAL_USD = 635.39
TRANSPEED_DHL_CLP = 2751206
TRANSPEED_TIPO_CAMBIO = 944.48


class TestCalculoProrrateoInternacional(unittest.TestCase):
    """Prueba directa de la función pura, sin pasar por HTTP -- más rápida y
    más fácil de leer el fallo si algún día se toca la fórmula."""

    def test_replica_exactamente_la_factura_real_transpeed(self):
        lineas_in = [{"cantidad": c, "costo_unitario_usd": u} for c, u, _s in LINEAS_TRANSPEED]
        desglose, error = taller_app._calcular_prorrateo_internacional(
            lineas_in, TRANSPEED_TIPO_CAMBIO, TRANSPEED_SHIPPING_USD, TRANSPEED_PAYPAL_USD, TRANSPEED_DHL_CLP,
        )
        self.assertIsNone(error)
        self.assertEqual(len(desglose), len(LINEAS_TRANSPEED))
        diferencias = []
        for (cant, costo_usd, s_excel), calc in zip(LINEAS_TRANSPEED, desglose):
            esperado = round(s_excel)
            obtenido = calc["costo_unitario_final_clp"]
            if abs(obtenido - esperado) > 1:
                diferencias.append((cant, costo_usd, esperado, obtenido))
        self.assertEqual(diferencias, [], f"Líneas que no calzan con el Excel real: {diferencias}")

    def test_porcentajes_de_prorrateo_suman_1(self):
        lineas_in = [{"cantidad": c, "costo_unitario_usd": u} for c, u, _s in LINEAS_TRANSPEED]
        desglose, error = taller_app._calcular_prorrateo_internacional(
            lineas_in, TRANSPEED_TIPO_CAMBIO, TRANSPEED_SHIPPING_USD, TRANSPEED_PAYPAL_USD, TRANSPEED_DHL_CLP,
        )
        self.assertIsNone(error)
        self.assertAlmostEqual(sum(l["pct_flete"] for l in desglose), 1.0, places=6)
        self.assertAlmostEqual(sum(l["pct_dhl"] for l in desglose), 1.0, places=6)

    def test_ejemplo_simple_a_mano_dos_lineas(self):
        # Dos líneas con costo unitario USD 100 y 300 (25%/75% de prorrateo),
        # cantidad 1 cada una, para poder verificar el resultado a mano sin
        # depender del Excel real.
        desglose, error = taller_app._calcular_prorrateo_internacional(
            [{"cantidad": 1, "costo_unitario_usd": 100}, {"cantidad": 1, "costo_unitario_usd": 300}],
            tipo_cambio=1000, shipping_usd=40, paypal_usd=0, dhl_clp=0,
        )
        self.assertIsNone(error)
        # Línea 1: 25% de $40 = $10 de flete -> (100+10)*1000 = 110.000
        self.assertEqual(desglose[0]["costo_unitario_final_clp"], 110000)
        # Línea 2: 75% de $40 = $30 de flete -> (300+30)*1000 = 330.000
        self.assertEqual(desglose[1]["costo_unitario_final_clp"], 330000)

    def test_cantidad_mayor_a_uno_divide_el_costo_de_la_linea(self):
        # OJO: 'costo_unitario_usd' NO se multiplica por la cantidad en
        # ningún punto de la cadena (así es el Excel real del taller, y así
        # se confirmó explícitamente con el taller al encontrar este mismo
        # comportamiento probando con líneas de cantidad > 1 -- ver el
        # comentario largo en _calcular_prorrateo_internacional). O sea, con
        # cantidad 4 y sin flete/paypal/dhl, el costo final POR UNIDAD queda
        # dividido en 4 respecto del costo unitario convertido -- no es un
        # error de este código, es fiel al Excel que ya usaban.
        desglose, error = taller_app._calcular_prorrateo_internacional(
            [{"cantidad": 4, "costo_unitario_usd": 100}],
            tipo_cambio=1000, shipping_usd=0, paypal_usd=0, dhl_clp=0,
        )
        self.assertIsNone(error)
        self.assertEqual(desglose[0]["costo_unitario_final_clp"], 25000)

    def test_tipo_de_cambio_invalido(self):
        desglose, error = taller_app._calcular_prorrateo_internacional(
            [{"cantidad": 1, "costo_unitario_usd": 100}], tipo_cambio=0, shipping_usd=0, paypal_usd=0, dhl_clp=0,
        )
        self.assertIsNone(desglose)
        self.assertIn("tipo de cambio", error.lower())

    def test_todos_los_costos_unitarios_en_cero_es_error(self):
        desglose, error = taller_app._calcular_prorrateo_internacional(
            [{"cantidad": 1, "costo_unitario_usd": 0}, {"cantidad": 2, "costo_unitario_usd": 0}],
            tipo_cambio=900, shipping_usd=100, paypal_usd=0, dhl_clp=0,
        )
        self.assertIsNone(desglose)
        self.assertIsNotNone(error)

    def test_linea_de_gasto_general_no_es_valida_en_factura_internacional(self):
        desglose, error = taller_app._calcular_prorrateo_internacional(
            [{"es_gasto_general": True, "descripcion": "Bencina", "monto": 5000, "rubro_gav": "combustible"}],
            tipo_cambio=900, shipping_usd=0, paypal_usd=0, dhl_clp=0,
        )
        self.assertIsNone(desglose)
        self.assertIsNotNone(error)

    def test_sin_lineas_es_error(self):
        desglose, error = taller_app._calcular_prorrateo_internacional(
            [], tipo_cambio=900, shipping_usd=0, paypal_usd=0, dhl_clp=0,
        )
        self.assertIsNone(desglose)
        self.assertIsNotNone(error)


class TestEndpointFacturaInternacional(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_producto(self, nombre, costo_compra=0):
        resp = self.client.post("/api/productos", json={"nombre": nombre, "stock_disponible": 0, "costo_compra": costo_compra})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_factura_internacional_de_punta_a_punta_aplica_stock_al_costo_prorrateado(self):
        p1 = self._crear_producto("Kit embrague importado test FI-1")
        p2 = self._crear_producto("Filtro importado test FI-2")
        resp = self.client.post(
            "/api/facturas/internacional",
            json={
                "numero": f"FI-TEST-{p1['id']}",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "tipo_cambio": 1000,
                "shipping_usd": 40,
                "paypal_usd": 0,
                "dhl_clp": 0,
                "condicion_pago": "credito",
                "lineas": [
                    {"producto_id": p1["id"], "cantidad": 1, "costo_unitario_usd": 100},
                    {"producto_id": p2["id"], "cantidad": 1, "costo_unitario_usd": 300},
                ],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        # Con ambos productos ya en el catálogo, queda aplicada de inmediato
        # (mismo comportamiento que cualquier otra factura manual, ver
        # _crear_pendiente_y_aplicar_si_posible).
        self.assertEqual(creada["estado"], "aprobada")
        self.assertIsNotNone(creada["internacional"])
        self.assertEqual(creada["internacional"]["tipo_cambio"], 1000)

        factura = self.client.get(f"/api/facturas/{creada['factura_id']}").get_json()
        precios = {l["producto_id_num"]: l["precio_unitario"] for l in factura["lineas"]}
        # 25%/75% del flete de $40 -> (100+10)*1000=110.000 y (300+30)*1000=330.000,
        # NO 100.000/300.000 (el costo en dólares sin prorratear).
        self.assertEqual(precios[p1["id"]], 110000)
        self.assertEqual(precios[p2["id"]], 330000)

        # Stock actualizado y Cuenta por Pagar generada, igual que cualquier
        # otra factura de compra aplicada.
        prod1 = self.client.get(f"/api/productos/{p1['id']}").get_json()
        self.assertEqual(prod1["stock_disponible"], 1)
        cxp = self.client.get("/api/finanzas/cxp").get_json()
        self.assertTrue(any(c.get("factura_id") == creada["factura_id"] for c in cxp["cuentas"]))

    def test_costo_usd_negativo_es_rechazado(self):
        p1 = self._crear_producto("Producto FI costo negativo")
        resp = self.client.post(
            "/api/facturas/internacional",
            json={
                "numero": f"FI-NEG-{p1['id']}",
                "tipo_cambio": 900,
                "lineas": [{"producto_id": p1["id"], "cantidad": 1, "costo_unitario_usd": -5}],
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_producto_nuevo_no_creado_queda_pendiente_de_recepcion(self):
        resp = self.client.post(
            "/api/facturas/internacional",
            json={
                "numero": "FI-NUEVO-PROD",
                "tipo_cambio": 900,
                "shipping_usd": 0,
                "paypal_usd": 0,
                "dhl_clp": 0,
                "lineas": [{"producto_nuevo_nombre": "Repuesto importado nunca antes visto", "cantidad": 2, "costo_unitario_usd": 50}],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        self.assertEqual(creada["estado"], "pendiente")
        self.assertIsNone(creada["lineas"][0]["producto_id"])
        self.assertEqual(creada["lineas"][0]["precio_unitario"], 22500)  # (50*900) / cantidad 2, ver nota arriba
        self.assertIsNotNone(creada["internacional"])


if __name__ == "__main__":
    unittest.main()
