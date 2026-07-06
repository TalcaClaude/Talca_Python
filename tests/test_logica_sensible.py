"""
Tests automatizados para la lógica más sensible del sistema: la que si se
rompe silenciosamente puede llevar a decisiones de compra equivocadas o a
descuadres de plata, sin que nadie lo note hasta mucho después.

Cubre:
  1. Costo Promedio Ponderado (recalculado en cada entrada de stock).
  2. Punto de reorden / stock mínimo automático (consumo, lead time, stock
     de seguridad).
  3. Saldo de Caja Chica (aportes, gastos, pendiente de rendir, gasto del mes).
  4. Módulo Operativo: que un repuesto cargado a una orden de trabajo
     consuma stock real (y no un número que vive solo dentro de la orden,
     desconectado del inventario), y que eliminar ese ítem lo devuelva.

Cómo correrlos:
    cd tests && python3 -m unittest test_logica_sensible -v

No requiere pytest ni ninguna dependencia externa — solo la librería
estándar de Python (unittest) y lo que ya trae app.py (Flask, etc.).

Importante: la base de datos de prueba es un archivo temporal separado
(nunca la base de datos real del taller) que se crea antes de importar
app.py y se borra al terminar todos los tests del archivo.
"""

import math
import os
import statistics
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# ---- Antes de importar app.py hay que apuntar DB_PATH a un archivo
# temporal: app.py llama a init_db() apenas se importa el módulo, así que
# si no se hace esto ANTES del import, terminaría inicializando (o peor,
# tocando) la base de datos real del taller.
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)  # que init_db() la cree de cero, como en un despliegue nuevo
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402  (import después de fijar DB_PATH a propósito)


def _crear_producto(db, nombre, stock_disponible=0, costo_compra=0.0, stock_minimo=0):
    ts = taller_app.now_iso()
    cur = db.execute(
        """
        INSERT INTO productos (
            nombre, stock_disponible, costo_compra, stock_minimo, creado_en, actualizado_en
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (nombre, stock_disponible, costo_compra, stock_minimo, ts, ts),
    )
    db.commit()
    return cur.lastrowid


def _insertar_movimiento(db, producto_id, tipo, cantidad, fecha, stock_resultante, costo_unitario=None):
    db.execute(
        """
        INSERT INTO movimientos (
            producto_id, fecha, tipo, categoria, cantidad, motivo, usuario,
            costo_unitario, stock_resultante
        ) VALUES (?, ?, ?, '', ?, '', 'test', ?, ?)
        """,
        (producto_id, fecha, tipo, cantidad, costo_unitario, stock_resultante),
    )
    db.commit()


class TestCostoPromedioPonderado(unittest.TestCase):
    """_aplicar_entrada_stock recalcula el Costo Promedio Ponderado (CPP)
    cada vez que entra stock con un costo unitario informado. Si esta
    cuenta se rompe, el costo de cada producto queda mal para siempre
    (afecta valorización de inventario y reportes de pérdida)."""

    def setUp(self):
        self.db = taller_app._conectar_db()

    def tearDown(self):
        self.db.close()

    def test_primera_entrada_sin_stock_previo_usa_el_costo_informado(self):
        pid = _crear_producto(self.db, "CPP - primera entrada", stock_disponible=0, costo_compra=0)
        row = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()

        nuevo_stock = taller_app._aplicar_entrada_stock(
            self.db, row, cantidad=10, costo_unitario=100.0, motivo="", usuario="test"
        )

        actualizado = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(nuevo_stock, 10)
        self.assertAlmostEqual(actualizado["costo_compra"], 100.0)

    def test_segunda_entrada_calcula_promedio_ponderado_por_cantidad(self):
        # 10 unidades a 100 + 10 unidades a 200 => promedio ponderado 150,
        # NO el promedio simple de (100+200)/2 = 150 en este caso puntual
        # (mismas cantidades), así que se usa un segundo caso con
        # cantidades distintas para que el test realmente distinga
        # "ponderado por cantidad" de "promedio simple".
        pid = _crear_producto(self.db, "CPP - ponderado", stock_disponible=0, costo_compra=0)
        row = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()
        taller_app._aplicar_entrada_stock(self.db, row, cantidad=10, costo_unitario=100.0, motivo="", usuario="t")

        row = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()
        taller_app._aplicar_entrada_stock(self.db, row, cantidad=30, costo_unitario=200.0, motivo="", usuario="t")

        # (10*100 + 30*200) / 40 = (1000 + 6000) / 40 = 175
        actualizado = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()
        self.assertAlmostEqual(actualizado["costo_compra"], 175.0)
        self.assertEqual(actualizado["stock_disponible"], 40)

    def test_entrada_sin_costo_informado_no_modifica_el_costo_promedio(self):
        # Ej: una devolución de stock donde no se vuelve a informar precio
        # de compra — el costo promedio ponderado no debería cambiar.
        pid = _crear_producto(self.db, "CPP - sin costo", stock_disponible=5, costo_compra=80.0)
        row = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()

        taller_app._aplicar_entrada_stock(self.db, row, cantidad=5, costo_unitario=None, motivo="", usuario="t")

        actualizado = self.db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()
        self.assertAlmostEqual(actualizado["costo_compra"], 80.0)
        self.assertEqual(actualizado["stock_disponible"], 10)


class TestPuntoDeReorden(unittest.TestCase):
    """_calcular_stock_minimo_producto es la fórmula completa de stock
    mínimo automático: consumo diario promedio × lead time estimado +
    stock de seguridad. Si se rompe, el sistema puede sugerir comprar de
    más (plata inmovilizada) o de menos (quiebre de stock)."""

    def setUp(self):
        self.db = taller_app._conectar_db()

    def tearDown(self):
        self.db.close()

    def test_sin_consumo_reciente_el_minimo_es_cero(self):
        pid = _crear_producto(self.db, "Reorden - sin consumo")
        calculo = taller_app._calcular_stock_minimo_producto(self.db, pid)
        self.assertEqual(calculo["stock_minimo_calculado"], 0)
        self.assertEqual(calculo["stock_seguridad"], 0.0)

    def test_lead_time_por_defecto_con_menos_de_dos_entradas_historicas(self):
        pid = _crear_producto(self.db, "Reorden - sin historial de compras")
        dias, es_defecto = taller_app._estimar_lead_time_dias(self.db, pid)
        self.assertTrue(es_defecto)
        self.assertEqual(dias, taller_app.STOCK_MINIMO_LEAD_TIME_DEFECTO_DIAS)

    def test_lead_time_estimado_usa_la_mediana_de_intervalos_reales(self):
        # Tres entradas espaciadas 10 y 20 días -> mediana de [10, 20] = 15.
        pid = _crear_producto(self.db, "Reorden - con historial de compras")
        base = datetime.now() - timedelta(days=40)
        _insertar_movimiento(self.db, pid, "entrada", 5, (base).isoformat(timespec="seconds"), 5)
        _insertar_movimiento(self.db, pid, "entrada", 5, (base + timedelta(days=10)).isoformat(timespec="seconds"), 10)
        _insertar_movimiento(self.db, pid, "entrada", 5, (base + timedelta(days=30)).isoformat(timespec="seconds"), 15)

        dias, es_defecto = taller_app._estimar_lead_time_dias(self.db, pid)
        self.assertFalse(es_defecto)
        self.assertAlmostEqual(dias, 15.0, delta=0.01)

    def test_lead_time_se_acota_al_rango_configurado(self):
        # Dos entradas separadas por 200 días deberían recortarse al
        # máximo configurado (60 días), no proyectar un lead time absurdo.
        pid = _crear_producto(self.db, "Reorden - intervalo extremo")
        base = datetime.now() - timedelta(days=250)
        _insertar_movimiento(self.db, pid, "entrada", 5, base.isoformat(timespec="seconds"), 5)
        _insertar_movimiento(self.db, pid, "entrada", 5, (base + timedelta(days=200)).isoformat(timespec="seconds"), 10)

        dias, _ = taller_app._estimar_lead_time_dias(self.db, pid)
        self.assertEqual(dias, taller_app.STOCK_MINIMO_LEAD_TIME_MAX_DIAS)

    def test_consumo_diario_promedio_cuenta_los_dias_sin_salida_como_cero(self):
        # 1 salida de 30 unidades hace 5 días, dentro de una ventana de 10
        # días -> promedio = 30 / 11 días (incluye el día de hoy), NO 30/1.
        pid = _crear_producto(self.db, "Reorden - consumo esporádico")
        fecha_salida = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        _insertar_movimiento(self.db, pid, "salida", 30, fecha_salida, 0)

        promedio, _ = taller_app._consumo_diario_stats(self.db, pid, dias=10)
        self.assertAlmostEqual(promedio, 30 / 11, delta=0.05)

    def test_formula_completa_del_punto_de_reorden(self):
        # Consumo constante y conocido, para poder verificar la fórmula
        # exacta: mínimo = consumo_diario × lead_time + Z × desviación × sqrt(lead_time).
        # Con consumo perfectamente constante, la desviación estándar es 0,
        # así que el stock de seguridad también debería ser 0 y el mínimo
        # debería ser exactamente consumo_diario × lead_time.
        pid = _crear_producto(self.db, "Reorden - consumo constante")
        # Cubre TODA la ventana que usa _consumo_diario_stats por defecto
        # (STOCK_MINIMO_VENTANA_DIAS), no solo una parte — si quedan días
        # sin salida dentro de la ventana, esos se cuentan como consumo 0
        # y meten varianza artificial que rompe la comparación con 0.
        for dias_atras in range(0, taller_app.STOCK_MINIMO_VENTANA_DIAS + 1):
            fecha = (datetime.now() - timedelta(days=dias_atras)).isoformat(timespec="seconds")
            _insertar_movimiento(self.db, pid, "salida", 4, fecha, 0)

        calculo = taller_app._calcular_stock_minimo_producto(self.db, pid)
        self.assertAlmostEqual(calculo["desviacion_diaria"], 0.0, delta=0.5)
        self.assertAlmostEqual(calculo["stock_seguridad"], 0.0, delta=1.0)
        esperado = calculo["consumo_diario_promedio"] * calculo["lead_time_dias"]
        self.assertAlmostEqual(calculo["stock_minimo_calculado"], round(esperado), delta=1)

    def test_mayor_variabilidad_de_consumo_exige_mas_stock_de_seguridad(self):
        # Dos productos con el mismo consumo promedio pero uno con consumo
        # parejo y otro con picos irregulares: el segundo debería terminar
        # con un stock mínimo calculado mayor (más colchón de seguridad).
        base = datetime.now() - timedelta(days=1)

        pid_parejo = _crear_producto(self.db, "Reorden - consumo parejo")
        pid_irregular = _crear_producto(self.db, "Reorden - consumo irregular")
        for i in range(20):
            fecha = (base - timedelta(days=i)).isoformat(timespec="seconds")
            _insertar_movimiento(self.db, pid_parejo, "salida", 5, fecha, 0)
            # Alterna entre 0 y 10 -> mismo promedio (5), mucha más varianza.
            cantidad_irregular = 10 if i % 2 == 0 else 0
            if cantidad_irregular > 0:
                _insertar_movimiento(self.db, pid_irregular, "salida", cantidad_irregular, fecha, 0)

        calculo_parejo = taller_app._calcular_stock_minimo_producto(self.db, pid_parejo)
        calculo_irregular = taller_app._calcular_stock_minimo_producto(self.db, pid_irregular)

        self.assertGreater(calculo_irregular["desviacion_diaria"], calculo_parejo["desviacion_diaria"])
        self.assertGreaterEqual(calculo_irregular["stock_minimo_calculado"], calculo_parejo["stock_minimo_calculado"])

    def test_recalcular_stock_minimo_sobrescribe_el_campo_del_producto(self):
        pid = _crear_producto(self.db, "Reorden - recalculo guarda", stock_minimo=999)
        for dias_atras in range(1, 11):
            fecha = (datetime.now() - timedelta(days=dias_atras)).isoformat(timespec="seconds")
            _insertar_movimiento(self.db, pid, "salida", 2, fecha, 0)

        calculo = taller_app._recalcular_stock_minimo(self.db, pid)
        self.db.commit()

        actualizado = self.db.execute("SELECT stock_minimo FROM productos WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(actualizado["stock_minimo"], calculo["stock_minimo_calculado"])
        self.assertNotEqual(actualizado["stock_minimo"], 999)

        guardado = self.db.execute(
            "SELECT * FROM stock_minimo_calculo WHERE producto_id = ?", (pid,)
        ).fetchone()
        self.assertIsNotNone(guardado)
        self.assertEqual(guardado["stock_minimo_calculado"], calculo["stock_minimo_calculado"])


class TestCajaChica(unittest.TestCase):
    """El saldo de Caja Chica (/api/caja/resumen) es dinero real: si el
    cálculo de saldo disponible o pendiente de rendir se descuadra, alguien
    puede terminar gastando plata que en realidad no está disponible."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        # Crea (o reutiliza, si ya corrió otra clase de test antes en el
        # mismo proceso) el usuario administrador y hace login.
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_aporte(self, monto):
        resp = self.client.post(
            "/api/caja/aporte",
            data={"fecha": datetime.now().strftime("%Y-%m-%d"), "monto": str(monto), "descripcion": "test"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_gasto(self, monto, categoria="Otro"):
        resp = self.client.post(
            "/api/caja/gasto",
            data={
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "monto": str(monto),
                "categoria": categoria,
                "descripcion": "test",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_saldo_disponible_es_aportado_menos_gastado(self):
        antes = self.client.get("/api/caja/resumen").get_json()

        self._crear_aporte(10000)
        self._crear_gasto(3000)

        despues = self.client.get("/api/caja/resumen").get_json()
        self.assertAlmostEqual(
            despues["saldo_disponible"] - antes["saldo_disponible"], 10000 - 3000, delta=0.01
        )
        self.assertAlmostEqual(despues["total_aportado"] - antes["total_aportado"], 10000, delta=0.01)
        self.assertAlmostEqual(despues["total_gastado"] - antes["total_gastado"], 3000, delta=0.01)

    def test_pendiente_de_rendir_solo_cuenta_gastos_sin_rendicion(self):
        antes = self.client.get("/api/caja/resumen").get_json()
        gasto = self._crear_gasto(1500)
        despues = self.client.get("/api/caja/resumen").get_json()

        self.assertAlmostEqual(
            despues["pendiente_rendir"] - antes["pendiente_rendir"], 1500, delta=0.01
        )

        # Al rendirlo, debería salir de "pendiente de rendir" sin afectar
        # el total gastado (la plata ya se gastó, solo cambia su estado
        # administrativo).
        resp = self.client.post("/api/caja/rendicion", json={"gasto_ids": [gasto["id"]]})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        final = self.client.get("/api/caja/resumen").get_json()
        self.assertAlmostEqual(final["pendiente_rendir"], despues["pendiente_rendir"] - 1500, delta=0.01)
        self.assertAlmostEqual(final["total_gastado"], despues["total_gastado"], delta=0.01)


class TestOperativo(unittest.TestCase):
    """Un repuesto cargado a una orden de trabajo tiene que consumir stock
    real del inventario (no un número que vive solo dentro de la orden) —
    es el mismo tipo de integración que ya existe entre Caja Chica y
    Logística ("compra de emergencia"). Si esto se rompe, el inventario
    queda desincronizado con lo que realmente se usó en el taller sin que
    nadie lo note hasta el próximo conteo físico."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_producto_http(self, nombre, stock_disponible, costo_compra):
        resp = self.client.post(
            "/api/productos",
            json={"nombre": nombre, "stock_disponible": stock_disponible, "costo_compra": costo_compra},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_cliente(self):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": "Cliente de prueba"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_agregar_repuesto_a_una_orden_consume_stock_del_inventario(self):
        producto = self._crear_producto_http("Repuesto para orden - consumo", 20, 5000)
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 3},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 17)

    def test_eliminar_item_de_repuesto_devuelve_el_stock(self):
        producto = self._crear_producto_http("Repuesto para orden - devolucion", 10, 5000)
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])

        item = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 4},
        ).get_json()

        tras_consumo = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_consumo["stock_disponible"], 6)

        resp = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/items/{item['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        tras_devolucion = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_devolucion["stock_disponible"], 10)

    def test_no_se_puede_cargar_mas_repuesto_del_que_hay_en_stock(self):
        producto = self._crear_producto_http("Repuesto para orden - sin stock", 2, 5000)
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 5},
        )
        self.assertEqual(resp.status_code, 400)

        # El stock no debe haberse tocado si la operación se rechazó.
        sin_cambios = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(sin_cambios["stock_disponible"], 2)

    def test_mano_de_obra_no_toca_el_stock_de_ningun_producto(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "Diagnóstico", "cantidad": 1, "costo_unitario": 20000},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        detalle = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(detalle["total"], 20000)
        self.assertIsNone(detalle["items"][0]["producto_id"])

    def test_orden_entregada_no_admite_nuevos_items(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})

        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "algo", "cantidad": 1, "costo_unitario": 1000},
        )
        self.assertEqual(resp.status_code, 409)


class TestOperativoFase35(unittest.TestCase):
    """Sub-flujos ampliados del módulo Operativo (Fase 3.5): presupuesto con
    aprobación, control de calidad, planificación/tiempos, garantía, entrega
    e historial de vehículo. Estas transiciones automáticas de estado son la
    lógica más fácil de romper sin darse cuenta (ej. aprobar un presupuesto
    y que la orden se quede pegada en 'esperando_aprobacion')."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Fase 3.5"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_equipo(self, cliente_id, identificador="XX0000"):
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

    # ---- Presupuesto ----

    def test_presupuesto_aprobado_transiciona_la_orden_a_reparacion(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto/items",
            json={"tipo": "mano_obra", "descripcion": "Desarme", "cantidad": 1, "precio_unitario": 100000},
        )
        resp = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "aprobado"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        actualizada = resp.get_json()
        self.assertEqual(actualizada["estado"], "reparacion")
        self.assertEqual(actualizada["presupuesto"]["total"], 100000)

    def test_presupuesto_con_descuento_e_iva_calcula_el_total_correcto(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto/items",
            json={"tipo": "mano_obra", "descripcion": "Mano de obra", "cantidad": 1, "precio_unitario": 100000},
        )
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto/items",
            json={"tipo": "descuento", "descripcion": "Descuento", "cantidad": 1, "precio_unitario": 10000},
        )
        resp = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"iva": True})
        presupuesto = resp.get_json()["presupuesto"]
        self.assertEqual(presupuesto["subtotal"], 90000)
        self.assertAlmostEqual(presupuesto["iva_monto"], 17100)
        self.assertAlmostEqual(presupuesto["total"], 107100)

    def test_no_se_puede_modificar_presupuesto_de_una_orden_cerrada(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto/items",
            json={"tipo": "mano_obra", "descripcion": "Algo", "cantidad": 1, "precio_unitario": 1000},
        )
        self.assertEqual(resp.status_code, 409)

    # ---- Control de calidad ----

    def test_qc_rechazado_devuelve_la_orden_a_reparacion(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "control_calidad"})
        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}/qc",
            json={"resultado": "rechazado", "inspector": "Ana", "observaciones": "Ruido"},
        )
        self.assertEqual(resp.get_json()["estado"], "reparacion")

    def test_qc_aprobado_pasa_la_orden_a_listo(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "control_calidad"})
        resp = self.client.put(f"/api/operativo/ordenes/{orden['id']}/qc", json={"resultado": "aprobado"})
        self.assertEqual(resp.get_json()["estado"], "listo")

    def test_checklist_qc_usa_vocabulario_distinto_al_veredicto_general(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        # El ítem del checklist usa ok/falla/na...
        ok = self.client.post(f"/api/operativo/ordenes/{orden['id']}/qc/items", json={"item": "Prueba de ruta", "resultado": "ok"})
        self.assertEqual(ok.status_code, 201, ok.get_data(as_text=True))
        # ...y "aprobado" (vocabulario del veredicto general) no es válido ahí.
        invalido = self.client.post(f"/api/operativo/ordenes/{orden['id']}/qc/items", json={"item": "Otro punto", "resultado": "aprobado"})
        self.assertEqual(invalido.status_code, 400)
        # A la inversa, "ok" no es un veredicto general válido.
        veredicto_invalido = self.client.put(f"/api/operativo/ordenes/{orden['id']}/qc", json={"resultado": "ok"})
        self.assertEqual(veredicto_invalido.status_code, 400)

    # ---- Garantía (entidad propia, ya no vive dentro de la OT) ----

    def test_garantia_se_crea_como_entidad_propia_pendiente(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        resp = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Falla reaparecida"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        self.assertEqual(creada["estado"], "pendiente")
        self.assertIsNone(creada["fecha_cierre"])

        listado = self.client.get("/api/operativo/garantias").get_json()
        encontrada = next((g for g in listado["garantias"] if g["id"] == creada["id"]), None)
        self.assertIsNotNone(encontrada)
        self.assertEqual(encontrada["equipo"]["id"], equipo["id"])
        self.assertEqual(encontrada["cliente"]["id"], cliente["id"])

    def test_garantia_puede_asociarse_a_la_orden_original(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        resp = self.client.post(
            "/api/operativo/garantias",
            json={
                "cliente_id": cliente["id"], "equipo_id": equipo["id"],
                "orden_origen_id": orden["id"], "motivo": "Ruido tras la reparación",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["orden_origen_id"], orden["id"])

    def test_garantia_al_resolverse_o_rechazarse_registra_fecha_de_cierre(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        garantia = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Motivo"},
        ).get_json()
        self.assertIsNone(garantia["fecha_cierre"])

        resuelta = self.client.put(
            f"/api/operativo/garantias/{garantia['id']}", json={"estado": "resuelta"}
        ).get_json()
        self.assertIsNotNone(resuelta["fecha_cierre"])

        # Si se reabre (vuelve a un estado no cerrado), la fecha de cierre
        # debería limpiarse otra vez.
        reabierta = self.client.put(
            f"/api/operativo/garantias/{garantia['id']}", json={"estado": "en_reparacion"}
        ).get_json()
        self.assertIsNone(reabierta["fecha_cierre"])

    def test_item_de_garantia_consume_stock_real_y_al_eliminarlo_se_devuelve(self):
        producto = self.client.post(
            "/api/productos",
            json={"nombre": "Repuesto para garantía", "stock_disponible": 10, "costo_compra": 3000},
        ).get_json()
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        garantia = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Motivo"},
        ).get_json()

        item = self.client.post(
            f"/api/operativo/garantias/{garantia['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 2},
        )
        self.assertEqual(item.status_code, 201, item.get_data(as_text=True))
        item = item.get_json()

        tras_consumo = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_consumo["stock_disponible"], 8)

        resp = self.client.delete(f"/api/operativo/garantias/{garantia['id']}/items/{item['id']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        tras_devolucion = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(tras_devolucion["stock_disponible"], 10)

    def test_no_se_pueden_agregar_items_a_una_garantia_cerrada(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        garantia = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Motivo"},
        ).get_json()
        self.client.put(f"/api/operativo/garantias/{garantia['id']}", json={"estado": "rechazada"})

        resp = self.client.post(
            f"/api/operativo/garantias/{garantia['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "algo", "cantidad": 1, "costo_unitario": 1000},
        )
        self.assertEqual(resp.status_code, 409)

    # ---- Dashboard: KPI nuevo de garantías abiertas ----

    def test_dashboard_incluye_garantias_abiertas(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        antes = self.client.get("/api/operativo/dashboard").get_json()["garantias_abiertas"]

        self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Motivo"},
        )
        despues = self.client.get("/api/operativo/dashboard").get_json()["garantias_abiertas"]
        self.assertEqual(despues, antes + 1)

    # ---- Historial de vehículo (3.5.1) ----

    def test_historial_de_equipo_lista_solo_las_ordenes_de_ese_equipo(self):
        cliente = self._crear_cliente()
        equipo_a = self._crear_equipo(cliente["id"], identificador="AAA111")
        equipo_b = self._crear_equipo(cliente["id"], identificador="BBB222")
        orden_1 = self._crear_orden(cliente["id"], equipo_id=equipo_a["id"])
        orden_2 = self._crear_orden(cliente["id"], equipo_id=equipo_a["id"])
        self._crear_orden(cliente["id"], equipo_id=equipo_b["id"])  # de otro equipo, no debe aparecer

        resp = self.client.get(f"/api/operativo/equipos/{equipo_a['id']}/historial")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        ids = {o["id"] for o in data["ordenes"]}
        self.assertEqual(ids, {orden_1["id"], orden_2["id"]})
        self.assertEqual(data["cliente"]["id"], cliente["id"])

    # ---- Recepción ampliada (3.5.2) ----

    def test_prioridad_invalida_es_rechazada_al_crear_la_orden(self):
        cliente = self._crear_cliente()
        resp = self.client.post(
            "/api/operativo/ordenes", json={"cliente_id": cliente["id"], "prioridad": "super-urgente"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_campos_de_recepcion_y_diagnostico_se_guardan(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"], kilometraje=50000, prioridad="urgente")
        self.assertEqual(orden["kilometraje"], 50000)
        self.assertEqual(orden["prioridad"], "urgente")

        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}",
            json={"sintoma": "Ruido al frenar", "falla_detectada": "Pastillas gastadas"},
        )
        actualizada = resp.get_json()
        self.assertEqual(actualizada["sintoma"], "Ruido al frenar")
        self.assertEqual(actualizada["falla_detectada"], "Pastillas gastadas")
        # Los campos de recepción ya guardados no deberían borrarse por
        # actualizar solo el diagnóstico.
        self.assertEqual(actualizada["kilometraje"], 50000)

    def test_estanque_fuera_de_rango_es_rechazado(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}", json={"estanque_nivel": 9}
        )
        self.assertEqual(resp.status_code, 400)

    def test_equipamiento_solo_guarda_items_de_la_lista_permitida(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}",
            json={"equipamiento": {"Gata": True, "Radio": False, "item_inventado": True}},
        )
        actualizada = resp.get_json()
        self.assertEqual(actualizada["equipamiento"], {"Gata": True, "Radio": False})

    # ---- Datos de vehículo ahora viven en el equipo, no en la orden (3.5.2 rediseño) ----

    def test_datos_de_vehiculo_se_guardan_en_el_equipo_y_persisten_entre_ordenes(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        resp = self.client.put(
            f"/api/operativo/equipos/{equipo['id']}",
            json={
                "vin": "9BW1234567890", "color": "Rojo", "anio": 2018,
                "modelo_transmision": "Manual", "puertas": "4", "motor_cc": "1600",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        actualizado = resp.get_json()
        self.assertEqual(actualizado["vin"], "9BW1234567890")
        self.assertEqual(actualizado["color"], "Rojo")
        self.assertEqual(actualizado["anio"], 2018)
        self.assertEqual(actualizado["modelo_transmision"], "Manual")
        self.assertEqual(actualizado["puertas"], "4")
        self.assertEqual(actualizado["motor_cc"], "1600")

        # Esos datos deben verse reflejados en cualquier orden de ese equipo,
        # sin tener que volver a escribirlos por cada visita.
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        self.assertEqual(orden["equipo"]["vin"], "9BW1234567890")
        self.assertEqual(orden["equipo"]["anio"], 2018)

    # ---- Control de calidad: cada punto queda con fecha y persona (3.5.6 rediseño) ----

    def test_cada_punto_de_qc_registra_fecha_y_usuario(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/qc/items",
            json={"item": "Prueba de ruta", "resultado": "ok"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()
        self.assertTrue(item["usuario"])
        self.assertIsNotNone(item["creado_en"])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        # Limpieza: no dejar el archivo de base de datos temporal (ni sus
        # acompañantes -wal/-shm de modo WAL) tirados en /tmp.
        for sufijo in ("", "-wal", "-shm"):
            try:
                os.remove(_TMP_DB_PATH + sufijo)
            except OSError:
                pass
