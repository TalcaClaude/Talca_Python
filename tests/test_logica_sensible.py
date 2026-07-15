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

import io
import math
import os
import statistics
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

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


def _insertar_movimiento(db, producto_id, tipo, cantidad, fecha, stock_resultante, costo_unitario=None, categoria=""):
    db.execute(
        """
        INSERT INTO movimientos (
            producto_id, fecha, tipo, categoria, cantidad, motivo, usuario,
            costo_unitario, stock_resultante
        ) VALUES (?, ?, ?, ?, ?, '', 'test', ?, ?)
        """,
        (producto_id, fecha, tipo, categoria, cantidad, costo_unitario, stock_resultante),
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


class TestEliminarFacturaRevierteSinContaminarConsumo(unittest.TestCase):
    """Al eliminar una factura que ya aplicó stock, se revierte ese ingreso
    con un movimiento de salida. Ese movimiento es una CORRECCIÓN técnica
    (se está deshaciendo un error), no consumo real del taller — así que no
    debe contarse en rotación, planificación de compras ni en el cálculo de
    stock mínimo automático. Si esto se rompe, eliminar una factura mal
    cargada infla artificialmente esas métricas y puede llevar a comprar de
    más o a decisiones de reposición equivocadas."""

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

    def _crear_factura_aplicada(self, numero, producto_id, cantidad=5, precio_unitario=1000):
        # Desde la migración a "pendientes de recepción" ya no existe el
        # registro rápido (POST /api/facturas) — se crea el pendiente y se
        # aprueba de inmediato, que es el único camino que queda para que
        # una factura termine aplicando stock.
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
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

    def test_reversion_por_eliminar_factura_queda_marcada_como_correccion_de_inventario(self):
        producto = self._crear_producto_http("Producto - reversión marca categoría", stock_disponible=0)
        factura = self._crear_factura_aplicada("FACT-REV-CAT-001", producto["id"])

        resp = self.client.delete(f"/api/facturas/{factura['id']}?forzar=1")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        with taller_app.app.app_context():
            db = taller_app.get_db()
            mov = db.execute(
                "SELECT * FROM movimientos WHERE producto_id = ? AND tipo = 'salida' ORDER BY id DESC LIMIT 1",
                (producto["id"],),
            ).fetchone()
        self.assertIsNotNone(mov)
        self.assertEqual(mov["categoria"], "Corrección de inventario")

    def test_reversion_por_eliminar_factura_no_aparece_en_rotacion(self):
        producto = self._crear_producto_http("Producto - reversión no cuenta en rotación", stock_disponible=0)
        factura = self._crear_factura_aplicada("FACT-REV-ROT-001", producto["id"], cantidad=7)

        self.client.delete(f"/api/facturas/{factura['id']}?forzar=1")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            rotacion = taller_app._calcular_rotacion(dias=365)
        ids_en_mas_utilizados = {r["id"] for r in rotacion["mas_utilizados"]}
        self.assertNotIn(producto["id"], ids_en_mas_utilizados)

    def test_reversion_por_eliminar_factura_no_infla_el_consumo_diario_para_stock_minimo(self):
        producto = self._crear_producto_http("Producto - reversión no infla consumo diario", stock_disponible=0)
        factura = self._crear_factura_aplicada("FACT-REV-CONSUMO-001", producto["id"], cantidad=9)

        self.client.delete(f"/api/facturas/{factura['id']}?forzar=1")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            promedio, desviacion = taller_app._consumo_diario_stats(db, producto["id"])
        self.assertEqual(promedio, 0)
        self.assertEqual(desviacion, 0)

    def test_eliminar_factura_creada_via_whatsapp_no_falla_por_llave_foranea(self):
        # Reproduce el bug reportado: una factura que quedó vinculada a una
        # fila de facturas_whatsapp_pendientes (porque nació de una foto
        # aprobada desde esa bandeja) no se podía eliminar — SQLite
        # rechazaba el DELETE con "FOREIGN KEY constraint failed" porque esa
        # columna no tenía ON DELETE CASCADE/SET NULL.
        producto = self._crear_producto_http("Producto - factura de whatsapp aprobada", stock_disponible=0)
        factura = self._crear_factura_aplicada("FACT-REV-WAP-001", producto["id"])

        with taller_app.app.app_context():
            db = taller_app.get_db()
            ts = taller_app.now_iso()
            cur = db.execute(
                """
                INSERT INTO facturas_whatsapp_pendientes (
                    telefono, mecanico_nombre, foto_archivo, datos_json, estado,
                    factura_id, revisado_por, revisado_en, creado_en
                ) VALUES (?, ?, ?, ?, 'aprobada', ?, ?, ?, ?)
                """,
                ("+56911112222", "Mecánico Test", "foto-test.jpg", "{}", factura["id"], "admin", ts, ts),
            )
            db.commit()
            pendiente_id = cur.lastrowid

        resp = self.client.delete(f"/api/facturas/{factura['id']}?forzar=1")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        with taller_app.app.app_context():
            db = taller_app.get_db()
            pendiente = db.execute(
                "SELECT * FROM facturas_whatsapp_pendientes WHERE id = ?", (pendiente_id,)
            ).fetchone()
        self.assertIsNone(pendiente["factura_id"])
        self.assertEqual(pendiente["estado"], "aprobada")  # el historial de revisión se conserva


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

    def test_mano_de_obra_con_iva_y_detalle_de_trabajo_externo(self):
        # Trabajo subcontratado (ej. "programación electrónica por Lucas
        # Medina") que se cobra con IVA: el costo real que sale del taller
        # es el monto + 19%, y debe quedar reflejado tanto en el ítem como
        # en el total/mano_obra_cargada de la orden.
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={
                "tipo": "mano_obra", "descripcion": "Programación electrónica",
                "detalle": "Trabajo externo — Lucas Medina",
                "cantidad": 1, "costo_unitario": 50000, "aplica_iva": True,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()
        self.assertEqual(item["detalle"], "TRABAJO EXTERNO — LUCAS MEDINA")
        self.assertEqual(item["aplica_iva"], 1)
        self.assertAlmostEqual(item["subtotal"], 59500)  # 50.000 + 19%

        detalle = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertAlmostEqual(detalle["total"], 59500)
        self.assertAlmostEqual(detalle["mano_obra_cargada"], 59500)

    def test_mano_de_obra_sin_iva_no_agrega_recargo(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "Cambio de sellos", "cantidad": 1, "costo_unitario": 20000},
        )
        item = resp.get_json()
        self.assertEqual(item["aplica_iva"], 0)
        self.assertEqual(item["detalle"], "")
        self.assertEqual(item["subtotal"], 20000)

    def test_mano_de_obra_con_costo_resta_de_la_utilidad_estimada(self):
        # Trabajo externo pagado a un tercero: sí es plata real que sale del
        # taller, así que debe restar de la utilidad estimada (antes solo
        # restaban los repuestos, dejando afuera la mano de obra externa).
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 100000})
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={
                "tipo": "mano_obra", "descripcion": "Programación electrónica",
                "detalle": "Trabajo externo - Lucas Medina",
                "cantidad": 1, "costo_unitario": 25000, "aplica_iva": True,
            },
        )
        detalle = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertAlmostEqual(detalle["mano_obra_cargada"], 29750)  # 25.000 + 19%
        self.assertAlmostEqual(detalle["utilidad_estimada"], 100000 - 29750)

    def test_mano_de_obra_interna_sin_costo_no_afecta_la_utilidad(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 100000})
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "Diagnóstico interno", "cantidad": 1},
        )
        detalle = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertAlmostEqual(detalle["mano_obra_cargada"], 0)
        self.assertAlmostEqual(detalle["utilidad_estimada"], 100000)


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

    def test_presupuesto_enviado_transiciona_la_orden_a_esperando_aprobacion(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.assertEqual(orden["estado"], "recibido")

        resp = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "enviado"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        actualizada = resp.get_json()
        self.assertEqual(actualizada["estado"], "esperando_aprobacion")

        # Guardar el presupuesto de nuevo sin cambiar el estado (ej. solo se
        # ajustó el IVA) no debe re-disparar nada raro: la orden se queda
        # tranquila en esperando_aprobacion.
        resp2 = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"iva": True})
        self.assertEqual(resp2.get_json()["estado"], "esperando_aprobacion")

    def test_presupuesto_marcado_enviado_no_retrocede_una_orden_ya_avanzada(self):
        # Si la orden ya avanzó más allá de "esperando_aprobacion" (ej. ya
        # está en reparación porque el presupuesto fue aprobado), volver a
        # guardar el presupuesto con estado "enviado" no debe hacerla
        # retroceder — sería un bug confuso para el taller.
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "aprobado"})
        self.assertEqual(resp.get_json()["estado"], "reparacion")

        resp2 = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "enviado"})
        self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
        self.assertEqual(resp2.get_json()["estado"], "reparacion")

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

    def test_diagnostico_se_puede_revisar_y_ajustar_desde_el_presupuesto(self):
        # El mismo campo "diagnostico" (resumen para el cliente) se puede
        # editar desde la pestaña Presupuesto, para revisarlo/ajustarlo
        # antes de mandarlo — puede haber llegado por audio de WhatsApp.
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(
            f"/api/operativo/ordenes/{orden['id']}",
            json={"diagnostico": "Diagnóstico original, cargado por audio"},
        )

        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto",
            json={"diagnostico": "Diagnóstico revisado antes de enviar", "notas": "Precio válido por 15 días"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        actualizada = resp.get_json()
        self.assertEqual(actualizada["diagnostico"], "DIAGNÓSTICO REVISADO ANTES DE ENVIAR")
        self.assertEqual(actualizada["presupuesto_notas"], "PRECIO VÁLIDO POR 15 DÍAS")
        # Quien guardó el ajuste queda como responsable — misma sesión que
        # el resto de la app (usuario_actual()).
        self.assertEqual(actualizada["responsable_diagnostico"], "ADMINISTRADOR")

        # El nuevo texto también aparece en el PDF del presupuesto que se
        # le manda al cliente.
        from pypdf import PdfReader
        pdf_resp = self.client.get(f"/api/operativo/ordenes/{orden['id']}/presupuesto/pdf")
        texto = PdfReader(io.BytesIO(pdf_resp.data)).pages[0].extract_text()
        self.assertIn("DIAGNÓSTICO REVISADO ANTES DE ENVIAR", texto)

    def test_guardar_presupuesto_sin_tocar_diagnostico_no_lo_modifica(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"diagnostico": "Texto original"})

        resp = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"notas": "Solo notas"})
        actualizada = resp.get_json()
        self.assertEqual(actualizada["diagnostico"], "TEXTO ORIGINAL")

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

    def test_garantia_se_crea_como_entidad_propia_en_ingreso(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        resp = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Falla reaparecida"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        self.assertEqual(creada["estado"], "ingreso")
        self.assertIsNone(creada["fecha_cierre"])

        listado = self.client.get("/api/operativo/garantias").get_json()
        encontrada = next((g for g in listado["garantias"] if g["id"] == creada["id"]), None)
        self.assertIsNotNone(encontrada)
        self.assertEqual(encontrada["equipo"]["id"], equipo["id"])
        self.assertEqual(encontrada["cliente"]["id"], cliente["id"])

    def test_garantias_con_estados_legados_se_migran_al_proceso_nuevo(self):
        # El proceso de garantías pasó de 4 pasos (pendiente/en_reparacion/
        # resuelta/rechazada) a 8 (ingreso/diagnostico/reparacion/prueba/
        # revision/listo/entregado/rechazada). Una garantía real que haya
        # quedado guardada con un estado viejo no debería quedar "huérfana"
        # (invisible en los filtros, con una etiqueta en blanco) — al
        # reiniciar la app se traduce sola al estado nuevo equivalente.
        cliente = self._crear_cliente()
        db = taller_app._conectar_db()
        ts = taller_app.now_iso()
        cur = db.execute(
            """
            INSERT INTO garantias (cliente_id, motivo, estado, fecha_recepcion, usuario, creado_en, actualizado_en)
            VALUES (?, 'Motivo legado', 'pendiente', ?, 'test', ?, ?)
            """,
            (cliente["id"], ts, ts, ts),
        )
        garantia_id = cur.lastrowid
        db.commit()
        db.close()

        # Simula un reinicio de la app (es lo que corre init_db() en cada
        # arranque real) para disparar la migración.
        db2 = taller_app._conectar_db()
        taller_app.init_operativo_db(db2)
        migrada = db2.execute("SELECT estado FROM garantias WHERE id = ?", (garantia_id,)).fetchone()
        db2.close()
        self.assertEqual(migrada["estado"], "ingreso")

    def test_garantia_puede_asociarse_a_la_orden_original(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        # La garantía solo se puede abrir una vez que el auto ya salió del
        # taller (la OT original tiene que estar cerrada) — ver
        # test_no_se_puede_crear_garantia_de_un_equipo_con_ot_abierta.
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        resp = self.client.post(
            "/api/operativo/garantias",
            json={
                "cliente_id": cliente["id"], "equipo_id": equipo["id"],
                "orden_origen_id": orden["id"], "motivo": "Ruido tras la reparación",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["orden_origen_id"], orden["id"])

    def test_garantia_al_entregarse_o_rechazarse_registra_fecha_de_cierre(self):
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        garantia = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Motivo"},
        ).get_json()
        self.assertIsNone(garantia["fecha_cierre"])

        entregada = self.client.put(
            f"/api/operativo/garantias/{garantia['id']}", json={"estado": "entregado"}
        ).get_json()
        self.assertIsNotNone(entregada["fecha_cierre"])

        # Si se reabre (vuelve a un estado no cerrado), la fecha de cierre
        # debería limpiarse otra vez.
        reabierta = self.client.put(
            f"/api/operativo/garantias/{garantia['id']}", json={"estado": "reparacion"}
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

    def test_no_se_puede_crear_2_garantias_abiertas_para_el_mismo_equipo(self):
        # Mismo criterio que con las OT: el auto no puede tener dos
        # garantías abiertas al mismo tiempo — recién se puede abrir una
        # nueva cuando la anterior se entrega o se rechaza.
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        garantia_1 = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Falla A"},
        ).get_json()

        rechazada = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Falla B"},
        )
        self.assertEqual(rechazada.status_code, 400)

        self.client.put(f"/api/operativo/garantias/{garantia_1['id']}", json={"estado": "entregado"})
        permitida = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Falla B"},
        )
        self.assertEqual(permitida.status_code, 201, permitida.get_data(as_text=True))

    def test_no_se_puede_crear_garantia_de_un_equipo_con_ot_abierta(self):
        # Bug reportado por el taller: no tiene lógica dejar abrir una
        # garantía para un auto que TODAVÍA está en el taller con una OT
        # abierta (ni siquiera ha salido) — si aparece un problema mientras
        # la OT sigue abierta, se sigue trabajando ahí mismo, no como
        # garantía nueva.
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])  # nace "recibido" (abierta)

        bloqueada = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Ruido raro"},
        )
        self.assertEqual(bloqueada.status_code, 400)
        self.assertEqual(bloqueada.get_json().get("bloqueo_tipo"), "orden")
        self.assertEqual(bloqueada.get_json().get("bloqueo_id"), orden["id"])

        # Una vez que la OT se entrega (el auto ya salió del taller), sí se
        # puede abrir la garantía para ese mismo vehículo.
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        permitida = self.client.post(
            "/api/operativo/garantias",
            json={"cliente_id": cliente["id"], "equipo_id": equipo["id"], "motivo": "Ruido raro"},
        )
        self.assertEqual(permitida.status_code, 201, permitida.get_data(as_text=True))

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
        # No se puede crear una segunda OT abierta para el mismo equipo (ver
        # test_no_se_puede_crear_2_ot_abiertas_para_el_mismo_equipo) — para
        # que el equipo tenga dos OT en su historial, la primera se entrega
        # antes de abrir la segunda, como pasaría en la vida real.
        self.client.put(f"/api/operativo/ordenes/{orden_1['id']}", json={"estado": "entregado"})
        orden_2 = self._crear_orden(cliente["id"], equipo_id=equipo_a["id"])
        self._crear_orden(cliente["id"], equipo_id=equipo_b["id"])  # de otro equipo, no debe aparecer

        resp = self.client.get(f"/api/operativo/equipos/{equipo_a['id']}/historial")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        ids = {o["id"] for o in data["ordenes"]}
        self.assertEqual(ids, {orden_1["id"], orden_2["id"]})
        self.assertEqual(data["cliente"]["id"], cliente["id"])

    def test_no_se_puede_crear_2_ot_abiertas_para_el_mismo_equipo(self):
        # Un mismo auto no puede estar "recibiéndose" dos veces a la vez —
        # solo se puede abrir una OT nueva una vez que la anterior ya se
        # entregó (o se canceló).
        cliente = self._crear_cliente()
        equipo = self._crear_equipo(cliente["id"])
        orden_1 = self._crear_orden(cliente["id"], equipo_id=equipo["id"])

        rechazada = self.client.post(
            "/api/operativo/ordenes", json={"cliente_id": cliente["id"], "equipo_id": equipo["id"]}
        )
        self.assertEqual(rechazada.status_code, 400)

        self.client.put(f"/api/operativo/ordenes/{orden_1['id']}", json={"estado": "entregado"})
        permitida = self.client.post(
            "/api/operativo/ordenes", json={"cliente_id": cliente["id"], "equipo_id": equipo["id"]}
        )
        self.assertEqual(permitida.status_code, 201, permitida.get_data(as_text=True))

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
        # El backend normaliza los campos de texto libre a mayúsculas al guardar.
        self.assertEqual(actualizada["sintoma"], "RUIDO AL FRENAR")
        self.assertEqual(actualizada["falla_detectada"], "PASTILLAS GASTADAS")
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
        # El backend normaliza marca/modelo/color/VIN/etc. a mayúsculas al guardar.
        self.assertEqual(actualizado["vin"], "9BW1234567890")
        self.assertEqual(actualizado["color"], "ROJO")
        self.assertEqual(actualizado["anio"], 2018)
        self.assertEqual(actualizado["modelo_transmision"], "MANUAL")
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


class TestPresupuestoDescargaEnvioYOrdenImprimible(unittest.TestCase):
    """Cubre las funcionalidades nuevas: (1) el PDF del presupuesto se
    descarga con el nombre "Presupuesto Talleres Calificados OT<n>
    <cliente>", (2) se puede mandar ese mismo PDF directo al correo
    registrado del cliente (con copia al taller), (3) el cliente ahora
    tiene un campo de dirección, y (4) existe una vista imprimible de la OT
    para el cliente que muestra valores pero NUNCA costos internos ni
    utilidad."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="José Alegría", email=""):
        resp = self.client.post(
            "/api/operativo/clientes",
            json={"nombre": nombre, "email": email, "direccion": "Av. Siempre Viva 742"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_direccion_del_cliente_se_guarda_y_se_puede_editar(self):
        cliente = self._crear_cliente(nombre="Cliente Con Dirección")
        self.assertEqual(cliente["direccion"], "AV. SIEMPRE VIVA 742")

        resp = self.client.put(
            f"/api/operativo/clientes/{cliente['id']}", json={"direccion": "Nueva Dirección 123"}
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["direccion"], "NUEVA DIRECCIÓN 123")

    def test_nombre_de_descarga_del_presupuesto_incluye_ot_y_cliente(self):
        cliente = self._crear_cliente(nombre="José Alegría")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.get(f"/api/operativo/ordenes/{orden['id']}/presupuesto/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/pdf")
        disposicion = resp.headers.get("Content-Disposition", "")
        self.assertIn(f"OT{orden['id']}", disposicion)
        self.assertIn("Presupuesto Talleres Calificados", disposicion)
        # El nombre se guarda en mayúsculas en la base; el archivo debe verse
        # con formato de nombre propio (title case), no todo en mayúsculas.
        # El navegador usa el filename*=UTF-8'' (RFC 6266) para el nombre
        # real con tildes — el filename= plano es solo el respaldo ASCII
        # para clientes HTTP viejos, así que ahí "José" se ve sin tilde
        # ("Jose") a propósito; eso no es un bug.
        self.assertIn("filename*=UTF-8''Presupuesto%20Talleres%20Calificados%20OT", disposicion)
        self.assertIn("Jos%C3%A9%20Alegr%C3%ADa.pdf", disposicion)

    def test_enviar_presupuesto_sin_correo_registrado_da_error_claro(self):
        cliente = self._crear_cliente(nombre="Cliente Sin Correo", email="")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/presupuesto/enviar")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("correo registrado", resp.get_json()["error"])

    def test_enviar_presupuesto_con_correo_llama_al_envio_con_copia_al_taller(self):
        cliente = self._crear_cliente(nombre="Cliente Con Correo", email="cliente@ejemplo.com")
        orden = self._crear_orden(cliente["id"])

        with patch.object(taller_app, "_enviar_correo", return_value=(True, "Enviado (simulado)")) as mock_envio, \
             patch.object(taller_app, "PRESUPUESTO_COPIA_TALLER", ["taller1@ejemplo.com", "taller2@ejemplo.com"]):
            resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/presupuesto/enviar")

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["destinatario"], "cliente@ejemplo.com")
        self.assertEqual(data["copia"], ["taller1@ejemplo.com", "taller2@ejemplo.com"])

        mock_envio.assert_called_once()
        args, kwargs = mock_envio.call_args
        self.assertEqual(args[0], "cliente@ejemplo.com")
        self.assertEqual(kwargs["cc"], ["taller1@ejemplo.com", "taller2@ejemplo.com"])
        self.assertTrue(kwargs["adjunto_bytes"])  # el PDF va adjunto de verdad, no solo el link
        self.assertIn(f"OT{orden['id']}", kwargs["adjunto_nombre"])

    def test_enviar_presupuesto_propaga_el_error_si_falla_el_transporte_de_correo(self):
        cliente = self._crear_cliente(nombre="Cliente Correo Falla", email="falla@ejemplo.com")
        orden = self._crear_orden(cliente["id"])

        with patch.object(taller_app, "_enviar_correo", return_value=(False, "SMTP caído")):
            resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/presupuesto/enviar")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("SMTP caído", resp.get_json()["error"])

    def test_vista_imprimible_de_la_ot_no_incluye_costos_ni_utilidad(self):
        from pypdf import PdfReader

        cliente = self._crear_cliente(nombre="Cliente Vista Imprimible")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 150000})
        # Un ítem de mano de obra con costo real (ej. trabajo externo): si
        # el documento imprimible mostrara costos, este monto (37500)
        # aparecería en el PDF — la prueba confirma que NO aparece.
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "mano_obra", "descripcion": "Trabajo externo", "cantidad": 1, "costo_unitario": 37500},
        )

        resp = self.client.get(f"/api/operativo/ordenes/{orden['id']}/orden-imprimible/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/pdf")
        disposicion = resp.headers.get("Content-Disposition", "")
        self.assertIn(f"OT{orden['id']}", disposicion)
        self.assertIn("Cliente Vista Imprimible", disposicion)

        texto = PdfReader(io.BytesIO(resp.data)).pages[0].extract_text()
        # El cuerpo del documento muestra el nombre tal cual se guarda en la
        # base (mayúsculas, igual que el resto de la app) — solo el nombre
        # del ARCHIVO se pasa por title() para que se vea prolijo al
        # descargarlo.
        self.assertIn("CLIENTE VISTA IMPRIMIBLE", texto)
        self.assertIn("Total OT", texto)
        self.assertIn("Total pagado", texto)
        self.assertIn("Total por pagar", texto)
        # Nada de costos internos ni utilidad — ese es justo el punto de
        # este documento (a diferencia de la pestaña "Costos", de uso
        # interno del taller).
        self.assertNotIn("37.500", texto)
        self.assertNotIn("Costo de repuestos", texto)
        self.assertNotIn("Costo mano de obra", texto)
        self.assertNotIn("Utilidad", texto)

    def test_presupuesto_pdf_incluye_el_diagnostico_junto_al_precio(self):
        # A pedido del taller: el presupuesto que recibe el cliente debe ir
        # acompañado del diagnóstico (resumen para el cliente), para que no
        # llegue un precio "pelado" sin explicación de por qué se cobra.
        from pypdf import PdfReader

        cliente = self._crear_cliente(nombre="Cliente Con Diagnostico")
        orden = self._crear_orden(cliente["id"])
        self.client.put(
            f"/api/operativo/ordenes/{orden['id']}",
            json={"diagnostico": "Se detectó el amortiguador delantero derecho roto, se recomienda cambiarlo."},
        )
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto/items",
            json={"tipo": "repuesto", "descripcion": "Amortiguador delantero", "cantidad": 1, "precio_unitario": 45000},
        )

        resp = self.client.get(f"/api/operativo/ordenes/{orden['id']}/presupuesto/pdf")
        self.assertEqual(resp.status_code, 200)
        texto = PdfReader(io.BytesIO(resp.data)).pages[0].extract_text()
        self.assertIn("Diagnóstico", texto)
        self.assertIn("AMORTIGUADOR DELANTERO DERECHO ROTO", texto)

    def test_presupuesto_pdf_sin_diagnostico_cargado_no_muestra_la_seccion(self):
        from pypdf import PdfReader

        cliente = self._crear_cliente(nombre="Cliente Sin Diagnostico")
        orden = self._crear_orden(cliente["id"])

        resp = self.client.get(f"/api/operativo/ordenes/{orden['id']}/presupuesto/pdf")
        self.assertEqual(resp.status_code, 200)
        texto = PdfReader(io.BytesIO(resp.data)).pages[0].extract_text()
        self.assertNotIn("Diagnóstico", texto)

    def test_notas_internas_del_presupuesto_nunca_aparecen_en_el_pdf(self):
        # Las "notas" del presupuesto son solo para uso interno del taller
        # (ej. "cliente pidió esperar hasta fin de mes") — nunca deben
        # llegar al documento que se descarga NI al que se manda por correo.
        from pypdf import PdfReader

        cliente = self._crear_cliente(nombre="Cliente Con Notas Internas", email="notas@ejemplo.com")
        orden = self._crear_orden(cliente["id"])
        self.client.put(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto",
            json={"notas": "Cliente pidió esperar hasta fin de mes, no llamar antes"},
        )

        resp = self.client.get(f"/api/operativo/ordenes/{orden['id']}/presupuesto/pdf")
        self.assertEqual(resp.status_code, 200)
        texto = PdfReader(io.BytesIO(resp.data)).pages[0].extract_text()
        self.assertNotIn("ESPERAR HASTA FIN DE MES", texto)
        self.assertNotIn("NO LLAMAR ANTES", texto)

        with patch.object(taller_app, "_enviar_correo", return_value=(True, "Enviado (simulado)")) as mock_envio, \
             patch.object(taller_app, "PRESUPUESTO_COPIA_TALLER", []):
            resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/presupuesto/enviar")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        pdf_adjunto = mock_envio.call_args.kwargs["adjunto_bytes"]
        texto_adjunto = PdfReader(io.BytesIO(pdf_adjunto)).pages[0].extract_text()
        self.assertNotIn("ESPERAR HASTA FIN DE MES", texto_adjunto)

    def test_enviar_presupuesto_por_correo_marca_enviado_y_pasa_a_esperando_aprobacion(self):
        cliente = self._crear_cliente(nombre="Cliente Auto Transicion", email="auto@ejemplo.com")
        orden = self._crear_orden(cliente["id"])
        self.assertEqual(orden["estado"], "recibido")

        with patch.object(taller_app, "_enviar_correo", return_value=(True, "Enviado (simulado)")), \
             patch.object(taller_app, "PRESUPUESTO_COPIA_TALLER", []):
            resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/presupuesto/enviar")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["orden"]["presupuesto_estado"], "enviado")
        self.assertEqual(data["orden"]["estado"], "esperando_aprobacion")

    def test_enviar_presupuesto_por_correo_no_retrocede_una_orden_ya_avanzada(self):
        # Si la orden ya está en reparación (ej. se reenvía el presupuesto
        # por correo después de que el cliente ya aprobó), mandar el correo
        # de nuevo NO debe hacerla retroceder a "esperando_aprobacion".
        cliente = self._crear_cliente(nombre="Cliente Ya En Reparacion", email="reparacion@ejemplo.com")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "aprobado"})

        with patch.object(taller_app, "_enviar_correo", return_value=(True, "Enviado (simulado)")), \
             patch.object(taller_app, "PRESUPUESTO_COPIA_TALLER", []):
            resp = self.client.post(f"/api/operativo/ordenes/{orden['id']}/presupuesto/enviar")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["orden"]["estado"], "reparacion")

    def test_responsable_del_diagnostico_se_registra_solo_con_la_sesion_activa(self):
        # El responsable NUNCA se toma de lo que mande el cliente en el PUT
        # — siempre es quien tiene la sesión iniciada al guardar (mismo
        # criterio que usuario_actual() en Caja Chica).
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}",
            json={"sintoma": "No enciende", "responsable_diagnostico": "Alguien Que No Guardó Esto"},
        )
        actualizada = resp.get_json()
        self.assertNotEqual(actualizada["responsable_diagnostico"], "ALGUIEN QUE NO GUARDÓ ESTO")
        self.assertEqual(actualizada["responsable_diagnostico"], "ADMINISTRADOR")

        # Si el PUT no toca ningún campo de diagnóstico (ej. guarda otra
        # pestaña), el responsable no se pisa.
        resp2 = self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"trabajo_a_realizar": "CAMBIO DE ACEITE"})
        self.assertEqual(resp2.get_json()["responsable_diagnostico"], "ADMINISTRADOR")

    def test_inspector_de_prueba_de_ruta_se_registra_solo_con_la_sesion_activa(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "control_calidad"})
        resp = self.client.put(
            f"/api/operativo/ordenes/{orden['id']}/qc",
            json={"observaciones": "Todo OK", "inspector": "Alguien Que No Guardó Esto"},
        )
        actualizada = resp.get_json()
        self.assertNotEqual(actualizada["qc_inspector"], "ALGUIEN QUE NO GUARDÓ ESTO")
        self.assertEqual(actualizada["qc_inspector"], "ADMINISTRADOR")


class TestBusquedaSinTildesMultipalabra(unittest.TestCase):
    """coincide_busqueda/normalizar_busqueda (core.py) son la base de TODOS
    los buscadores del sistema: ignoran tildes/mayúsculas y tratan cada
    palabra escrita como un filtro AND que puede calzar en cualquier campo
    — mientras más palabras se agregan, más se acota el resultado. Estos
    tests fijan ese comportamiento a nivel de función, y el de más abajo
    (test_buscador_de_productos_combina_dos_campos) lo verifica de punta a
    punta contra el endpoint /api/productos real."""

    def test_normaliza_tildes_y_mayusculas(self):
        self.assertEqual(taller_app.normalizar_busqueda("Bómba"), "bomba")
        self.assertEqual(taller_app.normalizar_busqueda("CAMIÓN"), "camion")

    def test_coincide_con_una_sola_palabra_sin_tilde(self):
        self.assertTrue(taller_app.coincide_busqueda("bomba", "BÓMBA DE AGUA"))
        self.assertTrue(taller_app.coincide_busqueda("camion", "Camión 3/4"))

    def test_varias_palabras_deben_calzar_todas_aunque_esten_en_campos_distintos(self):
        # "6f35 bomba" tiene que encontrar algo cuyo código es "6F35" Y cuya
        # descripción menciona "bomba" — cada palabra puede calzar en un
        # campo distinto, pero TODAS tienen que aparecer en algún lado.
        self.assertTrue(taller_app.coincide_busqueda("6f35 bomba", "6F35", "BOMBA DE AGUA"))
        self.assertFalse(taller_app.coincide_busqueda("6f35 bomba", "6F35", "FILTRO DE ACEITE"))
        self.assertFalse(taller_app.coincide_busqueda("6f35 bomba", "6F36", "BOMBA DE AGUA"))

    def test_consulta_vacia_no_filtra_nada(self):
        self.assertTrue(taller_app.coincide_busqueda("", "cualquier cosa"))
        self.assertTrue(taller_app.coincide_busqueda("   ", "cualquier cosa"))

    def test_buscador_de_productos_combina_dos_campos(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(db, "BOMBA DE AGUA TESTBUSQUEDA", stock_disponible=5)
            db.execute(
                "UPDATE productos SET codigo_interno = '6F35' WHERE nombre = 'BOMBA DE AGUA TESTBUSQUEDA'"
            )
            db.commit()
            _crear_producto(db, "FILTRO DE ACEITE TESTBUSQUEDA", stock_disponible=5)
            db.execute(
                "UPDATE productos SET codigo_interno = '9Z11' WHERE nombre = 'FILTRO DE ACEITE TESTBUSQUEDA'"
            )
            db.commit()

        cliente = taller_app.app.test_client()
        cliente.post("/login", data={"username": "admin", "password": "taller2026"})

        resp = cliente.get("/api/productos?q=6f35+bomba")
        data = resp.get_json()
        nombres = [p["nombre"] for p in data["productos"]]
        self.assertIn("BOMBA DE AGUA TESTBUSQUEDA", nombres)
        self.assertNotIn("FILTRO DE ACEITE TESTBUSQUEDA", nombres)


class TestBuscarProductoPorTexto(unittest.TestCase):
    """_buscar_producto_por_texto es el matcher difuso que intenta hacer
    calzar la descripción libre que la IA lee en una foto de factura con un
    producto ya existente. Si el umbral se relaja demasiado, se le puede
    asignar a una línea el producto equivocado sin que nadie lo note — así
    que estos tests fijan el comportamiento esperado en los tres casos que
    importan: calce exacto, calce parecido-pero-no-igual, y texto que no
    tiene nada que ver (debe devolver None, nunca "adivinar")."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    # Nota: la base de prueba viene con el catálogo real de ~1050 productos
    # (repuestos de caja de cambios) precargado desde init_db(), y cada
    # método de este test corre sobre la MISMA base (no se resetea entre
    # tests). Por eso cada producto de prueba se crea con un nombre único
    # (con el nombre del propio test method como marca), para no chocar ni
    # con el catálogo real ni con productos creados por otro test.

    def test_calce_exacto(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            nombre = "FILTRO DE AIRE BOSCH FA-220 TESTCALCEEXACTO"
            producto_id = _crear_producto(db, nombre, stock_disponible=10)
            producto = taller_app._buscar_producto_por_texto(db, nombre)
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], producto_id)

    def test_calce_parecido_por_encima_del_umbral(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            # Mismo producto, descrito de forma parecida a como lo pondría
            # un proveedor en su factura (sin la marca, con alguna palabra
            # de más) — debería seguir encontrando el mismo producto.
            producto_id = _crear_producto(db, "FILTRO AIRE TESTCALCEPARECIDO BOSCH FA-221", stock_disponible=10)
            producto = taller_app._buscar_producto_por_texto(db, "FILTRO AIRE BOSCH FA-221")
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], producto_id)

    def test_texto_no_relacionado_no_matchea(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            # Deliberadamente sin relación con nada del catálogo (ni con lo
            # que crean otros tests) — tiene que devolver None en vez de
            # "adivinar" el más parecido, aunque exista algún producto con
            # una similitud casual baja.
            producto = taller_app._buscar_producto_por_texto(db, "ZZZ-NOPE-9999 TEXTO INVENTADO SIN RELACION")
            self.assertIsNone(producto)

    def test_texto_vacio_no_matchea(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            self.assertIsNone(taller_app._buscar_producto_por_texto(db, ""))
            self.assertIsNone(taller_app._buscar_producto_por_texto(db, None))


class TestEscanearFotoFactura(unittest.TestCase):
    """El endpoint /api/facturas/escanear-foto no llama a la IA real en los
    tests (no hay ANTHROPIC_API_KEY en este entorno): se reemplaza
    _llamar_claude_vision_factura por una respuesta enlatada, igual a la
    que devolvería Claude Vision, y se verifica que todo el resto del
    camino (armar el "grupo", matchear producto, guardar en el cache de
    carga masiva, y poder confirmar la factura desde ahí) funcione tal
    cual funciona para la carga masiva por Excel — es el mismo motivo por
    el que ambas comparten código."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _subir_foto(self, filename="factura.jpg", contenido=b"contenido-falso-de-una-foto"):
        return self.client.post(
            "/api/facturas/escanear-foto",
            data={"foto": (io.BytesIO(contenido), filename)},
            content_type="multipart/form-data",
        )

    def test_extension_no_soportada_es_rechazada(self):
        resp = self._subir_foto(filename="factura.heic")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("HEIC", resp.get_json()["error"] + "no soportado")

    def test_sin_numero_de_factura_devuelve_422_con_mensaje_claro(self):
        respuesta_ia = {
            "numero_factura": "",
            "fecha": "2026-07-08",
            "proveedor_nombre": "Proveedor Foto Test",
            "proveedor_rut": "",
            "lineas": [],
        }
        with patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto()
        self.assertEqual(resp.status_code, 422)
        self.assertIn("número de factura", resp.get_json()["error"])

    def test_foto_reconocida_arma_preview_matcheando_producto_existente(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            producto_id = _crear_producto(db, "PASTILLAS DE FRENO DELANTERAS TRW TESTFOTO1", stock_disponible=5)
        respuesta_ia = {
            "numero_factura": "FOTO-001",
            "fecha": "2026-07-08",
            "proveedor_nombre": "Proveedor Foto Test",
            "proveedor_rut": "",
            "lineas": [
                {"descripcion": "PASTILLAS DE FRENO DELANTERAS TRW TESTFOTO1", "cantidad": 2, "precio_unitario": 15000},
                {"descripcion": "ZZZ-NOPE-9999 TEXTO INVENTADO SIN RELACION", "cantidad": 1, "precio_unitario": 5000},
            ],
        }
        with patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["origen"], "foto")
        self.assertTrue(data["token"])

        grupo = data["facturas"][0]
        self.assertEqual(grupo["numero"], "FOTO-001")
        self.assertTrue(grupo["proveedor_nuevo"])
        # Los proveedores "nuevos" (los que aún no existen en el sistema)
        # se guardan tal como los leyó la IA, sin forzar mayúsculas — solo
        # se normalizan a mayúsculas los proveedores YA existentes en la
        # base (ver _carga_masiva_resolver_proveedor).
        self.assertEqual(grupo["proveedor_nombre"], "Proveedor Foto Test")

        linea_matcheada, linea_sin_matchear = grupo["lineas"]
        self.assertEqual(linea_matcheada["producto_id"], producto_id)
        self.assertIsNone(linea_sin_matchear["producto_id"])
        # La línea sin match debe traer el texto original de la IA, para
        # mostrarlo como ayuda en la previsualización.
        self.assertEqual(linea_sin_matchear["texto_original"], "ZZZ-NOPE-9999 TEXTO INVENTADO SIN RELACION")

        # La factura completa no puede estar "valida" todavía: tiene una
        # línea sin producto asignado.
        self.assertFalse(grupo["valida"])

    def test_corregir_linea_y_proveedor_deja_la_factura_lista_para_confirmar(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            producto_1_id = _crear_producto(db, "PASTILLAS DE FRENO DELANTERAS TRW TESTFOTO2", stock_disponible=5)
            # Producto DISTINTO para la línea 2: si se corrigiera al mismo
            # producto que la línea 1, la validación de "producto duplicado
            # en la misma factura" (correcta y ya probada en carga masiva
            # por Excel) bloquearía la confirmación — acá se quiere probar
            # el camino feliz, así que cada línea va a un producto distinto.
            producto_2_id = _crear_producto(db, "FILTRO DE ACEITE TESTFOTO2B", stock_disponible=8)
            cur = db.execute(
                "INSERT INTO proveedores (nombre, rut, creado_en) VALUES (?, ?, ?)",
                ("Proveedor Foto Ya Existe", "", taller_app.now_iso()),
            )
            db.commit()
            proveedor_existente_id = cur.lastrowid
        respuesta_ia = {
            "numero_factura": "FOTO-002",
            "fecha": "2026-07-08",
            "proveedor_nombre": "Proveedor Foto Corrige",  # no calza con el existente -> llega como "nuevo"
            "proveedor_rut": "",
            "lineas": [
                {"descripcion": "PASTILLAS DE FRENO DELANTERAS TRW TESTFOTO2", "cantidad": 2, "precio_unitario": 15000},
                {"descripcion": "QQQ-XYZ-7777 OTRO TEXTO SIN RELACION ALGUNA", "cantidad": 1, "precio_unitario": 3000},
            ],
        }
        with patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto()
        data = resp.get_json()
        token = data["token"]
        self.assertTrue(data["facturas"][0]["proveedor_nuevo"])  # confirma la premisa del test

        # Corregir la línea sin match asignándole un producto existente
        # (simula lo que haría la persona a través del autocompletar).
        resp_linea = self.client.post(
            "/api/facturas/carga-masiva/corregir-linea",
            json={
                "token": token,
                "numero_factura": "FOTO-002",
                "fila": 2,
                "producto_id": producto_2_id,
                "cantidad": 1,
                "precio_unitario": 3000,
            },
        )
        self.assertEqual(resp_linea.status_code, 200, resp_linea.get_data(as_text=True))
        self.assertEqual(resp_linea.get_json()["factura"]["errores"], [])

        # Corregir el proveedor detectado (nuevo) por uno que ya existe en
        # el sistema — este es el endpoint agregado específicamente para la
        # previsualización por foto (POST .../corregir-proveedor).
        resp_prov = self.client.post(
            "/api/facturas/carga-masiva/corregir-proveedor",
            json={"token": token, "numero_factura": "FOTO-002", "proveedor_id": proveedor_existente_id},
        )
        self.assertEqual(resp_prov.status_code, 200, resp_prov.get_data(as_text=True))
        factura_corregida = resp_prov.get_json()["factura"]
        self.assertEqual(factura_corregida["proveedor_id"], proveedor_existente_id)
        self.assertEqual(factura_corregida["proveedor_nombre"], "Proveedor Foto Ya Existe")
        self.assertFalse(factura_corregida["proveedor_nuevo"])
        self.assertTrue(factura_corregida["valida"])

        # Desde la migración a "pendientes de recepción", confirmar la
        # carga masiva ya NO crea la factura real de inmediato — la deja
        # pendiente de que Bodega la apruebe (mismo camino unificado que
        # manual/Excel/WhatsApp, ver carga_masiva_confirmar).
        resp_confirmar = self.client.post(
            "/api/facturas/carga-masiva/confirmar", json={"token": token, "condicion_pago": "credito"}
        )
        self.assertEqual(resp_confirmar.status_code, 200, resp_confirmar.get_data(as_text=True))
        confirmadas = resp_confirmar.get_json()
        self.assertIn("FOTO-002", confirmadas.get("creadas", []))

        pendientes = self.client.get("/api/facturas/pendientes-recepcion?estado=pendiente").get_json()["pendientes"]
        pendiente_creado = next(p for p in pendientes if p["numero"] == "FOTO-002")
        self.assertEqual(pendiente_creado["origen"], "foto")
        # No debe haberse creado un proveedor nuevo — tiene que quedar
        # asociada al proveedor existente que se eligió en la corrección.
        self.assertEqual(pendiente_creado["proveedor_id"], proveedor_existente_id)


class TestAgenteWhatsApp(unittest.TestCase):
    """El agente de WhatsApp traduce un mensaje de texto libre de un
    mecánico en un cambio de estado de una Orden de Trabajo (OT). Como
    todavía no hay ninguna cuenta de WhatsApp conectada, estos tests usan
    /api/agente/simular-mensaje (mismo código que usaría un mensaje real,
    ver app.py: _agente_procesar_mensaje) y reemplazan la llamada a la IA
    por una respuesta enlatada. Lo más importante que cubren: que un
    teléfono no registrado NO puede mover nada, y que el agente respeta su
    propia lista de transiciones permitidas (más estricta que el PUT
    genérico que usa el tablero) aunque la IA le pida saltarse pasos."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Agente Test"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id, equipo_id=None):
        payload = {"cliente_id": cliente_id}
        if equipo_id is not None:
            payload["equipo_id"] = equipo_id
        resp = self.client.post("/api/operativo/ordenes", json=payload)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _forzar_estado(self, orden_id, estado):
        resp = self.client.put(f"/api/operativo/ordenes/{orden_id}", json={"estado": estado})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def _crear_equipo(self, cliente_id, identificador="ZZ0000", marca="Marca", modelo="Modelo"):
        resp = self.client.post(
            "/api/operativo/equipos",
            json={"cliente_id": cliente_id, "identificador": identificador, "marca": marca, "modelo": modelo},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _registrar_mecanico(self, nombre, telefono):
        resp = self.client.post("/api/agente/mecanicos", json={"nombre": nombre, "telefono": telefono})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _simular(self, telefono, texto):
        return self.client.post("/api/agente/simular-mensaje", json={"telefono": telefono, "texto": texto})

    def _ia(self, *acciones, resumen="ok"):
        """Arma una respuesta simulada de _llamar_claude_interpretar_mensaje_agente
        en el formato real (una lista de acciones — un mensaje puede pedir
        más de una cosa a la vez, ver app.py)."""
        return {"acciones": list(acciones), "resumen": resumen}

    def test_normalizar_telefono_uniforma_formatos_distintos(self):
        self.assertEqual(taller_app._normalizar_telefono("whatsapp:+569 1234 5678"), "+56912345678")
        self.assertEqual(taller_app._normalizar_telefono("+56 9 1234 5678"), "+56912345678")
        self.assertEqual(taller_app._normalizar_telefono("56912345678"), "56912345678")

    def test_telefono_no_registrado_no_puede_mover_nada(self):
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        # Ni siquiera se llega a llamar a la IA: el teléfono no está
        # registrado como mecánico, así que se corta antes.
        resp = self._simular("+56900000001", f"ot {orden['id']} lista para diagnostico")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["accion"], "no_autorizado")
        self.assertFalse(data["exito"])

        # La orden no debe haberse tocado.
        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "recibido")

    def test_mecanico_inactivo_no_puede_usar_el_agente(self):
        mecanico = self._registrar_mecanico("Juan Mecánico", "+56911111111")
        resp_desactivar = self.client.put(f"/api/agente/mecanicos/{mecanico['id']}", json={"activo": False})
        self.assertEqual(resp_desactivar.status_code, 200)

        resp = self._simular("+56911111111", "hola")
        self.assertEqual(resp.get_json()["accion"], "no_autorizado")

    def test_cambio_de_estado_valido_se_aplica_de_verdad(self):
        self._registrar_mecanico("Pedro Mecánico", "+56922222222")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])  # nace en "recibido"

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "diagnostico"},
            resumen="El mecánico avisa que ya revisó el equipo y pasa a diagnóstico",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56922222222", f"ot {orden['id']} ya la revise, pasa a diagnostico")

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["accion"], "cambio_estado_ot")
        self.assertEqual(data["estado_anterior"], "recibido")
        self.assertEqual(data["estado_nuevo"], "diagnostico")
        self.assertIn(str(orden["id"]), data["respuesta_texto"])

        # El cambio tiene que verse reflejado de verdad en la orden.
        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "diagnostico")

    def test_el_agente_permite_saltar_pasos_intermedios(self):
        # A pedido del taller: el agente ya NO exige seguir el flujo paso a
        # paso — un auto puede llegar ya aprobado y saltar directo de
        # "recibido" a "reparación", o de "pruebas de ruta" (control_calidad)
        # directo a "entregado", sin pasar por los estados intermedios.
        self._registrar_mecanico("Ana Mecánica", "+56933333333")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])  # nace en "recibido"

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "reparacion"},
            resumen="El auto llegó ya aprobado por el cliente, pasa directo a reparación",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56933333333", f"ot {orden['id']} llegó aprobada, directo a reparación")

        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["accion"], "cambio_estado_ot")
        self.assertEqual(data["estado_nuevo"], "reparacion")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "reparacion")

    def test_esperando_repuesto_puede_pasar_directo_a_control_calidad(self):
        # Antes solo se permitía volver a "reparación" desde "esperando
        # repuesto" — ahora puede ir directo a "pruebas de ruta" también.
        self._registrar_mecanico("Vicente Mecánico", "+56933344455")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "esperando_repuesto")

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "control_calidad"},
            resumen="Llegó el repuesto y quedó lista para pruebas de ruta",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56933344455", f"ot {orden['id']} lista para pruebas de ruta")

        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["estado_nuevo"], "control_calidad")

    def test_el_agente_no_puede_sacar_una_ot_de_entregado(self):
        # Esto sí se mantiene estricto: una OT "entregado" (o "cancelado")
        # es un estado cerrado y no se puede volver a mover por mensaje de
        # WhatsApp — evita descuadres en el cierre de mes o en el dashboard.
        # Sacarla de ahí exige un ajuste manual desde el sistema.
        self._registrar_mecanico("Bernardo Mecánico", "+56933355566")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "entregado")

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "reparacion"},
            resumen="El mecánico dice que hay que revisarla de nuevo",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56933355566", f"ot {orden['id']} hay que revisarla de nuevo")

        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "transicion_no_permitida")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "entregado")

    def test_mensaje_no_reconocido_no_toca_ninguna_orden(self):
        self._registrar_mecanico("Luis Mecánico", "+56944444444")
        respuesta_ia = self._ia({"tipo": "no_reconocida"}, resumen="Es solo un saludo, no pide nada")
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56944444444", "hola buenos días")

        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "no_reconocida")
        self.assertIsNone(data["ot_id"])

    def test_transicion_valida_desde_estado_intermedio(self):
        self._registrar_mecanico("Sofía Mecánica", "+56955555555")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "reparacion")

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "control_calidad"},
            resumen="El mecánico terminó la reparación y pide prueba de ruta",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular(
                "+56955555555", f"ot {orden['id']} trabajo de reparación listo, queda preparada para prueba de ruta"
            )

        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["estado_nuevo"], "control_calidad")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "control_calidad")

    def test_ot_inexistente_responde_error_sin_romper(self):
        self._registrar_mecanico("Tomás Mecánico", "+56966666666")
        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": 999999, "estado_destino": "diagnostico"},
            resumen="Pide mover una OT que no existe",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56966666666", "ot 999999 lista para diagnostico")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "error")

    def test_actualizar_diagnostico_por_agente_solo_toca_los_campos_mencionados(self):
        self._registrar_mecanico("Diego Mecánico", "+56911122233")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "diagnostico")
        # Deja un valor previo en un campo que el mensaje NO va a mencionar,
        # para comprobar que no se borra.
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"diagnostico_tecnico": "REVISIÓN PREVIA OK"})

        respuesta_ia = self._ia(
            {"tipo": "actualizar_diagnostico", "ot_id": orden["id"], "sintoma": "El auto no enciende en frío, batería descargada"},
            resumen="El mecánico reporta el síntoma encontrado",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular(
                "+56911122233", f"ot {orden['id']} revisé el auto, no enciende en frío, la batería está descargada"
            )

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "actualizar_diagnostico")
        self.assertEqual(data["estado_nuevo"], "")
        self.assertIn(str(orden["id"]), data["respuesta_texto"])

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["sintoma"], "EL AUTO NO ENCIENDE EN FRÍO, BATERÍA DESCARGADA")
        # El campo que ya tenía un valor y no fue mencionado sigue intacto.
        self.assertEqual(actual["diagnostico_tecnico"], "REVISIÓN PREVIA OK")
        # El responsable NUNCA sale de lo que interpretó la IA — siempre es
        # el mecánico registrado que mandó el mensaje de WhatsApp.
        self.assertEqual(actual["responsable_diagnostico"], "DIEGO MECÁNICO")
        # El estado de la orden no cambia por esta acción.
        self.assertEqual(actual["estado"], "diagnostico")

    def test_actualizar_diagnostico_por_agente_ignora_un_responsable_que_mande_la_ia(self):
        # Defensa en profundidad: aunque la IA (por error o por un prompt
        # manipulado) mandara un "responsable_diagnostico" en la
        # interpretación, la función ni siquiera lo mira — construye el
        # valor final ella misma a partir del mecánico autenticado.
        self._registrar_mecanico("Diego Mecánico", "+56911122266")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "diagnostico")

        respuesta_ia = self._ia(
            {
                "tipo": "actualizar_diagnostico", "ot_id": orden["id"], "sintoma": "Ruido en el motor",
                "responsable_diagnostico": "Otro Mecánico Cualquiera",
            },
            resumen="Intento de suplantar al responsable",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56911122266", f"ot {orden['id']} ruido en el motor")

        self.assertTrue(resp.get_json()["exito"])
        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["responsable_diagnostico"], "DIEGO MECÁNICO")

    def test_actualizar_prueba_ruta_por_agente_nunca_toca_qc_resultado_ni_estado(self):
        self._registrar_mecanico("Elena Mecánica", "+56911133344")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "control_calidad")

        respuesta_ia = self._ia(
            {
                "tipo": "actualizar_prueba_ruta", "ot_id": orden["id"],
                "qc_observaciones": "Frenos y dirección responden bien, sin ruidos extraños",
                # Aunque la IA mandara esto por error, la función solo debe
                # aplicar qc_observaciones (más el inspector, que ella misma
                # fija) — nunca qc_resultado.
                "qc_resultado": "aprobado",
            },
            resumen="El mecánico deja observaciones de la prueba de ruta",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56911133344", f"ot {orden['id']} hice la prueba de ruta, todo bien, sin ruidos")

        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        self.assertEqual(data["accion"], "actualizar_prueba_ruta")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        # El inspector NUNCA sale de lo que interpretó la IA — siempre es el
        # mecánico registrado que mandó el mensaje de WhatsApp.
        self.assertEqual(actual["qc_inspector"], "ELENA MECÁNICA")
        self.assertIn("FRENOS", actual["qc_observaciones"])
        # qc_resultado y el estado de la OT NUNCA se tocan por este medio:
        # eso sí mueve la OT de estado y se deja como acción manual.
        self.assertFalse(actual.get("qc_resultado"))
        self.assertEqual(actual["estado"], "control_calidad")

    def test_actualizar_diagnostico_sin_datos_concretos_no_rompe_nada(self):
        self._registrar_mecanico("Fabián Mecánico", "+56911144455")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "diagnostico")

        respuesta_ia = self._ia(
            {"tipo": "actualizar_diagnostico", "ot_id": orden["id"]},
            resumen="El mecánico solo saluda, no da ningún dato de diagnóstico",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56911144455", f"ot {orden['id']} hola, después te cuento")

        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "error")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "diagnostico")

    def test_actualizar_diagnostico_de_ot_entregada_es_rechazado(self):
        self._registrar_mecanico("Gabriela Mecánica", "+56911155566")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "entregado")

        respuesta_ia = self._ia(
            {"tipo": "actualizar_diagnostico", "ot_id": orden["id"], "sintoma": "Ruido en el motor"},
            resumen="Manda un dato de diagnóstico tarde, la OT ya está entregada",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56911155566", f"ot {orden['id']} el síntoma era ruido en el motor")

        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "error")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertNotEqual(actual["sintoma"], "RUIDO EN EL MOTOR")

    def test_un_solo_mensaje_puede_traer_dos_acciones_a_la_vez(self):
        # Ejemplo real pedido por el taller: "diagnostico haval de matias
        # patina en 5ta marcha y se recomienda reconstruccion completa y
        # pasar ot a reparacion" — un solo audio con DOS intenciones
        # (actualizar el diagnóstico Y cambiar el estado). El agente tiene
        # que reconocer y aplicar ambas, no solo una.
        self._registrar_mecanico("Hernán Mecánico", "+56911166677")
        cliente = self._crear_cliente("Matías Vega")
        equipo = self._crear_equipo(cliente["id"], identificador="HAV900", marca="HAVAL", modelo="H6")
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])  # nace en "recibido"
        self._forzar_estado(orden["id"], "diagnostico")

        respuesta_ia = self._ia(
            {
                "tipo": "actualizar_diagnostico", "ot_id": orden["id"],
                "sintoma": "Patina en 5ta marcha",
                "diagnostico_tecnico": "Se recomienda reconstrucción completa de la caja",
                "diagnostico": "La caja de cambios patina en 5ta marcha; se recomienda una reconstrucción completa.",
            },
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "reparacion"},
            resumen="Diagnóstico de la Haval de Matías (patina en 5ta) y pasa a reparación",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular(
                "+56911166677",
                "diagnostico haval de matias patina en 5ta marcha y se recomienda reconstruccion "
                "completa y pasar ot a reparacion",
            )

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["exito"], data)
        # Las dos acciones quedan reflejadas en el registro combinado.
        self.assertIn("actualizar_diagnostico", data["accion"])
        self.assertIn("cambio_estado_ot", data["accion"])
        self.assertEqual(data["estado_anterior"], "diagnostico")
        self.assertEqual(data["estado_nuevo"], "reparacion")
        self.assertIn(str(orden["id"]), data["respuesta_texto"])

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        # El diagnóstico se guardó...
        self.assertEqual(actual["sintoma"], "PATINA EN 5TA MARCHA")
        self.assertIn("RECONSTRUCCIÓN COMPLETA", actual["diagnostico_tecnico"])
        self.assertIn("RECONSTRUCCIÓN COMPLETA", actual["diagnostico"])
        self.assertEqual(actual["responsable_diagnostico"], "HERNÁN MECÁNICO")
        # ...Y el estado también cambió, las dos cosas del mismo mensaje.
        self.assertEqual(actual["estado"], "reparacion")

    def test_diagnostico_se_guarda_aunque_el_cambio_de_estado_del_mismo_mensaje_falle(self):
        # El diagnóstico se aplica ANTES que el cambio de estado (ver
        # _agente_procesar_mensaje) — así, si la parte del cambio de estado
        # no se puede aplicar, el diagnóstico ya alcanzó a quedar guardado
        # en vez de perderse. La OT queda en un estado abierto (no cerrado)
        # para que el fallo del cambio de estado sea por otra razón (un
        # 'estado_destino' inválido/inventado) y no se confunda con el caso
        # de OT cerrada, que ya bloquearía las dos acciones por igual.
        self._registrar_mecanico("Ismael Mecánico", "+56911177788")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        self._forzar_estado(orden["id"], "diagnostico")

        respuesta_ia = self._ia(
            {"tipo": "actualizar_diagnostico", "ot_id": orden["id"], "sintoma": "Ruido en suspensión"},
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "estado_que_no_existe"},
            resumen="Manda diagnóstico y un estado de destino inválido",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56911177788", f"ot {orden['id']} ruido en suspensión, pásala a un estado raro")

        data = resp.get_json()
        # Éxito general porque al menos una de las dos acciones sí se aplicó.
        self.assertTrue(data["exito"])
        self.assertIn("error", data["accion"])

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        # El diagnóstico quedó guardado a pesar de que el cambio de estado
        # falló — la OT sigue en el mismo estado, como corresponde.
        self.assertEqual(actual["sintoma"], "RUIDO EN SUSPENSIÓN")
        self.assertEqual(actual["estado"], "diagnostico")

    def test_mensaje_sin_lista_de_acciones_se_trata_como_no_reconocido(self):
        # Defensa por si la IA (bug, o cambio de modelo) devuelve un
        # formato inesperado sin 'acciones' — no debe romper, se trata
        # igual que un mensaje no reconocido.
        self._registrar_mecanico("Julia Mecánica", "+56911188899")
        respuesta_ia = {"resumen": "algo salió raro"}
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56911188899", "mensaje cualquiera")

        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "no_reconocida")

    def test_registrar_mecanico_valida_telefono_y_evita_duplicados(self):
        resp_sin_nombre = self.client.post("/api/agente/mecanicos", json={"telefono": "+56977777777"})
        self.assertEqual(resp_sin_nombre.status_code, 400)

        resp_telefono_invalido = self.client.post(
            "/api/agente/mecanicos", json={"nombre": "Alguien", "telefono": "123"}
        )
        self.assertEqual(resp_telefono_invalido.status_code, 400)

        self._registrar_mecanico("Duplicado Uno", "+56988888888")
        resp_duplicado = self.client.post(
            "/api/agente/mecanicos", json={"nombre": "Duplicado Dos", "telefono": "+56988888888"}
        )
        self.assertEqual(resp_duplicado.status_code, 400)

    def test_todos_los_mensajes_quedan_en_la_auditoria(self):
        self._registrar_mecanico("Carla Mecánica", "+56999999999")
        respuesta_ia = self._ia({"tipo": "no_reconocida"}, resumen="saludo")
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            self._simular("+56999999999", "hola")

        mensajes = self.client.get("/api/agente/mensajes").get_json()
        self.assertTrue(any(m["telefono"] == "+56999999999" for m in mensajes))

    def test_listar_ordenes_abiertas_excluye_entregadas_y_canceladas(self):
        cliente = self._crear_cliente("Matías Vega")
        equipo = self._crear_equipo(cliente["id"], identificador="HAV001", marca="HAVAL", modelo="H6")
        orden_abierta = self._crear_orden(cliente["id"], equipo_id=equipo["id"])

        cliente2 = self._crear_cliente("Otro Cliente Cerrado")
        orden_cerrada = self._crear_orden(cliente2["id"])
        self._forzar_estado(orden_cerrada["id"], "entregado")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            abiertas = taller_app._agente_listar_ordenes_abiertas(db)

        ids_abiertas = [o["ot_id"] for o in abiertas]
        self.assertIn(orden_abierta["id"], ids_abiertas)
        self.assertNotIn(orden_cerrada["id"], ids_abiertas)

        entrada = next(o for o in abiertas if o["ot_id"] == orden_abierta["id"])
        self.assertEqual(entrada["cliente_nombre"], "MATÍAS VEGA")
        self.assertEqual(entrada["marca"], "HAVAL")
        self.assertEqual(entrada["modelo"], "H6")

    def test_el_agente_le_pasa_a_la_ia_las_ordenes_abiertas_como_contexto(self):
        self._registrar_mecanico("Diego Mecánico", "+56900112233")
        cliente = self._crear_cliente("Matías Vega")
        equipo = self._crear_equipo(cliente["id"], identificador="HAV002", marca="HAVAL", modelo="H6")
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        self._forzar_estado(orden["id"], "esperando_repuesto")

        respuesta_ia = self._ia({"tipo": "no_reconocida"}, resumen="no importa para este test")
        with patch.object(
            taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia
        ) as mock_ia:
            self._simular("+56900112233", "la haval h6 de matias vega esta esperando repuestos")

        # La función de interpretación tiene que haber recibido, como
        # segundo argumento, el listado de OT abiertas — incluyendo esta
        # orden con su cliente y vehículo — para que la IA pueda resolver
        # la OT por descripción aunque el mensaje no traiga un número.
        mock_ia.assert_called_once()
        args = mock_ia.call_args.args
        self.assertEqual(args[0], "la haval h6 de matias vega esta esperando repuestos")
        ordenes_pasadas = args[1]
        entrada = next((o for o in ordenes_pasadas if o["ot_id"] == orden["id"]), None)
        self.assertIsNotNone(entrada)
        self.assertEqual(entrada["cliente_nombre"], "MATÍAS VEGA")
        self.assertEqual(entrada["marca"], "HAVAL")
        self.assertEqual(entrada["modelo"], "H6")

    def test_identificar_ot_por_vehiculo_sin_numero_mueve_el_estado_real(self):
        # Simula lo que la IA real haría al recibir el contexto de OT
        # abiertas: resolver el número de OT a partir de la descripción del
        # vehículo, sin que el mecánico lo haya escrito. Lo que le importa
        # a este test es que, una vez resuelto el ot_id, el resto del
        # camino (validar transición y aplicar el cambio) funciona igual
        # que cuando el número viene explícito en el mensaje.
        self._registrar_mecanico("Fernanda Mecánica", "+56900223344")
        cliente = self._crear_cliente("Rodrigo Soto")
        equipo = self._crear_equipo(cliente["id"], identificador="AUD001", marca="AUDI", modelo="A3")
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        self._forzar_estado(orden["id"], "reparacion")

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "control_calidad"},
            resumen="Identificó la OT por el vehículo (Audi A3 azul) sin número explícito",
        )
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56900223344", "audi a3 azul en pruebas de ruta")

        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["ot_id"], orden["id"])
        self.assertEqual(data["estado_nuevo"], "control_calidad")

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "control_calidad")

    # ---- Notas de voz: la API de Claude no acepta audio, así que un
    # mensaje de voz se transcribe primero con OpenAI (Whisper) y el texto
    # resultante sigue el mismo camino que un mensaje escrito. Estos tests
    # mockean tanto la transcripción como la IA de Claude (no hay
    # OPENAI_API_KEY/ANTHROPIC_API_KEY reales en este entorno).

    def test_mensaje_de_texto_normal_queda_registrado_con_origen_texto(self):
        self._registrar_mecanico("Marta Mecánica", "+56900334455")
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])

        respuesta_ia = self._ia({"tipo": "no_reconocida"}, resumen="no importa para este test")
        with patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._simular("+56900334455", f"ot {orden['id']} en diagnostico")

        data = resp.get_json()
        self.assertEqual(data["origen"], "texto")
        with taller_app.app.app_context():
            fila = taller_app.get_db().execute(
                "SELECT origen FROM agente_mensajes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(fila["origen"], "texto")

    def test_transcribir_audio_sin_openai_api_key_da_error_claro(self):
        with patch.object(taller_app, "OPENAI_API_KEY", ""):
            with self.assertRaises(RuntimeError) as ctx:
                taller_app._transcribir_audio_whatsapp(b"audio-falso", "audio/ogg")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_transcribir_audio_vacio_da_error_claro(self):
        with patch.object(taller_app, "OPENAI_API_KEY", "clave-de-prueba"):
            with self.assertRaises(RuntimeError) as ctx:
                taller_app._transcribir_audio_whatsapp(b"", "audio/ogg")
        self.assertIn("vacía", str(ctx.exception))

    def _subir_nota_de_voz(self, telefono, contenido=b"audio-falso-de-prueba", filename="nota.ogg"):
        return self.client.post(
            "/api/agente/simular-mensaje-voz",
            data={"telefono": telefono, "audio": (io.BytesIO(contenido), filename)},
            content_type="multipart/form-data",
        )

    def test_simular_mensaje_voz_sin_telefono_o_archivo_da_400(self):
        resp = self.client.post(
            "/api/agente/simular-mensaje-voz",
            data={"telefono": ""},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_simular_mensaje_voz_transcribe_y_mueve_la_ot(self):
        self._registrar_mecanico("Ignacio Mecánico", "+56900445566")
        cliente = self._crear_cliente("Cliente Nota De Voz")
        orden = self._crear_orden(cliente["id"])  # nace en "recibido"

        respuesta_ia = self._ia(
            {"tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "diagnostico"},
            resumen="El mecánico avisa por voz que ya revisó el equipo",
        )
        with patch.object(
            taller_app, "_transcribir_audio_whatsapp",
            return_value=f"ot {orden['id']} ya la revise, pasa a diagnostico",
        ) as mock_transcribir, patch.object(
            taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia
        ):
            resp = self._subir_nota_de_voz("+56900445566")

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        mock_transcribir.assert_called_once()
        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["origen"], "voz")
        self.assertIn(str(orden["id"]), data["transcripcion"])

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "diagnostico")

        with taller_app.app.app_context():
            fila = taller_app.get_db().execute(
                "SELECT origen FROM agente_mensajes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(fila["origen"], "voz")

    def test_simular_mensaje_voz_cuando_falla_la_transcripcion_devuelve_400_claro(self):
        self._registrar_mecanico("Pilar Mecánica", "+56900556677")
        with patch.object(
            taller_app, "_transcribir_audio_whatsapp",
            side_effect=RuntimeError("El servicio de transcripción respondió con error (415): formato no soportado."),
        ):
            resp = self._subir_nota_de_voz("+56900556677")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("formato no soportado", resp.get_json()["error"])

    # -- Foto de factura por WhatsApp: queda pendiente de revisión humana,
    # nunca se sube sola al inventario. Reutiliza la misma IA de visión que
    # /api/facturas/escanear-foto (ver TestEscanearFotoFactura), mockeada
    # acá también para no depender de ANTHROPIC_API_KEY en los tests. --

    def _imagen_jpeg_valida(self):
        """JPEG real y chico (a diferencia del contenido falso que usan los
        tests de factura, que nunca pasa por un decodificador de imágenes
        de verdad): las fotos de tipo 'vehiculo' SÍ se procesan con Pillow
        de verdad ahora (se archivan en fotos_vehiculo, ver
        _agente_procesar_mensaje / Fase 3), así que necesitan bytes
        decodificables."""
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (60, 40), (10, 20, 30)).save(buf, format="JPEG")
        return buf.getvalue()

    def _subir_foto_por_agente(self, telefono, filename="factura.jpg", contenido=b"contenido-falso-de-una-foto", texto=""):
        data = {"telefono": telefono, "foto": (io.BytesIO(contenido), filename)}
        if texto:
            data["texto"] = texto
        return self.client.post(
            "/api/agente/simular-mensaje-foto", data=data, content_type="multipart/form-data",
        )

    def _contar_pendientes(self):
        # Desde la migración, WhatsApp ya no escribe en su propia tabla —
        # todo queda en facturas_finanzas_pendientes con origen='whatsapp'
        # (ver _procesar_foto_factura_whatsapp).
        with taller_app.app.app_context():
            return taller_app.get_db().execute(
                "SELECT COUNT(*) AS n FROM facturas_finanzas_pendientes WHERE origen = 'whatsapp'"
            ).fetchone()["n"]

    def test_telefono_no_registrado_no_puede_mandar_facturas(self):
        antes = self._contar_pendientes()
        respuesta_ia = {"numero_factura": "AG-000", "fecha": "", "proveedor_nombre": "", "proveedor_rut": "", "lineas": []}
        with patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto_por_agente("+56900990099")
        data = resp.get_json()
        self.assertEqual(data["accion"], "no_autorizado")
        # Un teléfono no registrado no llega ni siquiera a llamar a la IA,
        # así que no debe haber quedado ningún pendiente nuevo.
        self.assertEqual(self._contar_pendientes(), antes)

    def _preparar_pendiente(self, sufijo):
        """Helper (NO es un test): registra un mecánico y un producto con
        datos únicos por 'sufijo' (para no chocar entre tests que comparten
        la misma base persistida durante toda la clase) y manda una foto de
        factura simulada por el agente. Devuelve (pendiente_id, producto_id,
        numero_factura)."""
        telefono = f"+5693{sufijo}"
        numero = f"AG-{sufijo}"
        nombre_producto = f"CORREA DE DISTRIBUCION TESTAGENTE{sufijo}"
        self._registrar_mecanico(f"Mecánico Facturas {sufijo}", telefono)
        with taller_app.app.app_context():
            db = taller_app.get_db()
            producto_id = _crear_producto(db, nombre_producto, stock_disponible=3)

        respuesta_ia = {
            "numero_factura": numero,
            "fecha": "2026-07-09",
            "proveedor_nombre": f"Repuestos Agente Test {sufijo}",
            "proveedor_rut": "",
            "lineas": [{"descripcion": nombre_producto, "cantidad": 4, "precio_unitario": 8000}],
        }
        # Toda foto que llega por el agente se clasifica primero (¿factura
        # o vehículo?) antes de intentar leerla como factura — ver
        # _llamar_claude_vision_clasificar_foto_whatsapp en app.py.
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp", return_value={"tipo": "factura", "patente": ""}), \
             patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto_por_agente(telefono)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["accion"], "factura_recibida")

        pendientes = self.client.get("/api/facturas/pendientes-recepcion?estado=pendiente").get_json()["pendientes"]
        pendiente = next(p for p in pendientes if p["numero"] == numero and p["origen"] == "whatsapp")
        return pendiente["id"], producto_id, numero

    # -- Foto de un vehículo por WhatsApp: en vez de una factura, el
    # mecánico manda la foto del auto (para mostrar la patente) junto con
    # un pie de foto que dice qué pasó (ej. "listo para probar"). La IA lee
    # la patente, la combina con el texto y se apoya en el mismo intérprete
    # de mensajes que ya sabe matchear una OT por patente (ver
    # ordenes_abiertas en _llamar_claude_interpretar_mensaje_agente). --

    def test_foto_de_vehiculo_con_patente_y_pie_de_foto_mueve_la_ot(self):
        self._registrar_mecanico("Mecánico Fotos Auto", "+56900771122")
        cliente = self._crear_cliente(nombre="Cliente Foto Vehiculo")
        equipo = self._crear_equipo(cliente["id"], identificador="FTV001", marca="Haval", modelo="H6")
        orden = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        self._forzar_estado(orden["id"], "reparacion")

        clasificacion = {"tipo": "vehiculo", "patente": "FTV001"}
        respuesta_ia = self._ia({
            "tipo": "cambiar_estado_ot", "ot_id": orden["id"], "estado_destino": "control_calidad",
        })
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp", return_value=clasificacion), \
             patch.object(taller_app, "_llamar_claude_interpretar_mensaje_agente", return_value=respuesta_ia):
            resp = self._subir_foto_por_agente(
                "+56900771122", filename="auto.jpg", contenido=self._imagen_jpeg_valida(), texto="listo para probar",
            )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["exito"])
        self.assertEqual(data["ot_id"], orden["id"])
        self.assertEqual(data["estado_nuevo"], "control_calidad")
        self.assertEqual(data["origen"], "foto_vehiculo")
        # Fase 3: además de mover la OT, la foto queda archivada -- ver los
        # tests de TestFotosPorWhatsAppFase3 más abajo para el detalle del
        # archivado en sí (categoría, origen='desarme_armado', responsable).
        self.assertIn("archivada", data["respuesta_texto"])

        actual = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertEqual(actual["estado"], "control_calidad")

    def test_foto_de_vehiculo_sin_patente_legible_pide_una_foto_mejor(self):
        self._registrar_mecanico("Mecánico Foto Sin Patente", "+56900771133")
        with patch.object(
            taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
            return_value={"tipo": "vehiculo", "patente": ""},
        ):
            resp = self._subir_foto_por_agente("+56900771133", filename="auto.jpg", texto="listo para probar")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "foto_vehiculo_sin_patente")
        self.assertIn("logré leer una patente", data["respuesta_texto"])

    def test_foto_de_vehiculo_sin_ot_abierta_para_esa_patente_avisa(self):
        # Fase 3: ya no existe "foto_vehiculo_sin_accion" -- una foto sin
        # texto también se intenta archivar (ver
        # test_foto_sin_texto_igual_se_archiva_si_hay_ot en
        # TestFotosPorWhatsAppFase3). Acá lo que falla es que la patente que
        # lee la IA no corresponde a ninguna OT abierta.
        self._registrar_mecanico("Mecánico Foto Sin Texto", "+56900771144")
        with patch.object(
            taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
            return_value={"tipo": "vehiculo", "patente": "SINTX01"},
        ):
            resp = self._subir_foto_por_agente("+56900771144", filename="auto.jpg")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "foto_vehiculo_ot_no_encontrada")
        self.assertIn("SINTX01", data["respuesta_texto"])

    def test_foto_clasificada_otro_sin_numero_de_ot_pide_el_numero(self):
        # Corregido a partir de un caso real: un mecánico mandó la foto de
        # una pieza suelta (un radiador de aceite arriba de un paño) con
        # "Ot 3103" en el pie de foto -- la IA la clasificó como 'otro'
        # (no se ve un auto completo), pero como el archivado YA NO exige
        # tipo_foto == 'vehiculo' (ver _agente_procesar_mensaje), esa foto
        # se archiva igual mientras el mensaje traiga el número de OT. Este
        # test cubre el caso sin número: ahí sí no hay cómo resolver la OT,
        # así que se pide explícitamente en vez de descartar la foto.
        self._registrar_mecanico("Mecánico Foto Rara", "+56900771155")
        with patch.object(
            taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp",
            return_value={"tipo": "otro", "patente": ""},
        ):
            resp = self._subir_foto_por_agente("+56900771155", filename="algo.jpg")
        data = resp.get_json()
        self.assertFalse(data["exito"])
        self.assertEqual(data["accion"], "foto_vehiculo_sin_patente")
        self.assertIn("número de OT", data["respuesta_texto"])

    def test_foto_de_factura_queda_pendiente_de_revision_y_no_toca_el_inventario(self):
        pendiente_id, producto_id, numero = self._preparar_pendiente("100001")

        # Todavía NO se creó ninguna factura real ni se movió stock: queda
        # pendiente de que alguien la revise y apruebe a mano.
        stock_actual = self.client.get(f"/api/productos/{producto_id}").get_json()["stock_disponible"]
        self.assertEqual(stock_actual, 3)
        self.assertEqual(self.client.get(f"/api/facturas/numero/{numero}").status_code, 404)

        pendiente = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente_id}").get_json()
        self.assertEqual(pendiente["estado"], "pendiente")
        self.assertEqual(pendiente["origen"], "whatsapp")
        self.assertEqual(pendiente["numero"], numero)
        self.assertEqual(pendiente["lineas"][0]["producto_id"], producto_id)

    def test_aprobar_pendiente_crea_la_factura_real_y_aplica_stock(self):
        pendiente_id, producto_id, numero = self._preparar_pendiente("100002")

        resp = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente_id}/aprobar", json={})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        data = resp.get_json()

        # El stock ahora sí se movió, y la factura real quedó creada.
        stock_actual = self.client.get(f"/api/productos/{producto_id}").get_json()["stock_disponible"]
        self.assertEqual(stock_actual, 3 + 4)
        self.assertEqual(self.client.get(f"/api/facturas/numero/{numero}").status_code, 200)

        pendiente_actualizado = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente_id}").get_json()
        self.assertEqual(pendiente_actualizado["estado"], "aprobada")
        self.assertEqual(pendiente_actualizado["factura_id"], data["id"])

        # No se puede volver a aprobar (ni rechazar) un pendiente ya procesado.
        resp_repetido = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente_id}/aprobar", json={})
        self.assertEqual(resp_repetido.status_code, 400)

    def test_rechazar_pendiente_no_crea_factura_ni_toca_stock(self):
        pendiente_id, producto_id, numero = self._preparar_pendiente("100003")

        resp = self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente_id}/rechazar",
            json={"nota": "Foto duplicada, ya se cargó antes a mano."},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        stock_actual = self.client.get(f"/api/productos/{producto_id}").get_json()["stock_disponible"]
        self.assertEqual(stock_actual, 3)
        self.assertEqual(self.client.get(f"/api/facturas/numero/{numero}").status_code, 404)

        pendiente_actualizado = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente_id}").get_json()
        self.assertEqual(pendiente_actualizado["estado"], "rechazada")
        self.assertEqual(pendiente_actualizado["nota_rechazo"], "Foto duplicada, ya se cargó antes a mano.")

    def test_ia_no_disponible_responde_con_error_explicable_y_no_crea_pendiente(self):
        antes = self._contar_pendientes()
        self._registrar_mecanico("Mecánico Sin IA", "+56944455566")
        with patch.object(
            taller_app, "_llamar_claude_vision_factura",
            side_effect=RuntimeError("El escaneo de facturas por foto no está configurado todavía."),
        ):
            resp = self._subir_foto_por_agente("+56944455566")
        data = resp.get_json()
        self.assertEqual(data["accion"], "error")
        self.assertFalse(data["exito"])
        self.assertEqual(self._contar_pendientes(), antes)


class TestCuentasPorCobrar(unittest.TestCase):
    """La CxC de una OT se genera sola al entregarla (nunca a mano), queda
    'congelada' en ese monto, no se duplica si la orden vuelve a pasar por
    'entregado', y su estado (pendiente/parcial/pagada) sigue en vivo lo
    que pasa con pagos_orden. Si esto se rompe, el taller puede terminar
    cobrándole dos veces al mismo cliente por la misma OT, o mostrando una
    cuenta "pendiente" que en realidad ya se pagó completa."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente CxC"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _cxc_de_orden(self, orden_id):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            row = db.execute("SELECT * FROM cuentas_por_cobrar WHERE orden_id = ?", (orden_id,)).fetchone()
            return dict(row) if row else None

    def test_entregar_una_orden_con_precio_genera_su_cxc(self):
        cliente = self._crear_cliente("Cliente CxC - genera")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 100000})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})

        cxc = self._cxc_de_orden(orden["id"])
        self.assertIsNotNone(cxc)
        self.assertEqual(cxc["monto_total"], 100000)
        self.assertEqual(cxc["estado"], "pendiente")
        self.assertEqual(cxc["cliente_id"], cliente["id"])

    def test_entregar_una_orden_sin_precio_no_genera_cxc(self):
        cliente = self._crear_cliente("Cliente CxC - sin precio")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})

        self.assertIsNone(self._cxc_de_orden(orden["id"]))

    def test_reentregar_una_orden_no_duplica_la_cxc_en_el_camino_normal(self):
        cliente = self._crear_cliente("Cliente CxC - normal")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 50000})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "listo"})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})

        # El camino normal (listo -> entregado) es ambos estados "que
        # cierran CxC" -- no debe generar una segunda fila.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            total = db.execute(
                "SELECT COUNT(*) FROM cuentas_por_cobrar WHERE orden_id = ?", (orden["id"],)
            ).fetchone()[0]
        self.assertEqual(total, 1)
        cxc = self._cxc_de_orden(orden["id"])
        self.assertEqual(cxc["monto_total"], 50000)

    def test_reabrir_una_orden_sin_pagos_elimina_la_cxc_y_se_regenera_con_el_monto_nuevo(self):
        # El taller pidió explícitamente este comportamiento: si una OT que
        # ya estaba "lista para cobrar" se devuelve a reparación (ej. se
        # encontró una falla y hay que reabrirla) la CxC no debería seguir
        # apareciendo como pendiente de cobro -- el auto en realidad no está
        # listo. Debe reaparecer, con el monto recalculado, cuando la OT
        # vuelva a llegar a 'listo'/'entregado'.
        cliente = self._crear_cliente("Cliente CxC - reabierta sin pagos")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 50000})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        self.assertIsNotNone(self._cxc_de_orden(orden["id"]))

        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "reparacion"})
        with taller_app.app.app_context():
            db = taller_app.get_db()
            total = db.execute(
                "SELECT COUNT(*) FROM cuentas_por_cobrar WHERE orden_id = ?", (orden["id"],)
            ).fetchone()[0]
        self.assertEqual(total, 0, "la CxC debería eliminarse al salir de listo/entregado sin pagos")

        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 999999})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        cxc = self._cxc_de_orden(orden["id"])
        self.assertIsNotNone(cxc)
        self.assertEqual(cxc["monto_total"], 999999, "al reingresar debe recalcularse con el precio vigente")

    def test_reabrir_una_orden_con_pagos_ya_registrados_conserva_la_cxc(self):
        # Si el cliente ya alcanzó a abonar mientras la OT estaba
        # lista/entregada, reabrirla no debe hacer desaparecer ese registro
        # de cobro -- se perdería el rastro de la plata ya recibida.
        cliente = self._crear_cliente("Cliente CxC - reabierta con pagos")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 50000})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/pagos",
            json={"metodo_pago": "efectivo", "monto": 20000},
        )

        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "reparacion"})
        cxc = self._cxc_de_orden(orden["id"])
        self.assertIsNotNone(cxc, "con un pago ya registrado, la CxC debe conservarse al reabrir")
        self.assertEqual(cxc["monto_total"], 50000)

    def test_estado_de_la_cxc_pasa_a_parcial_y_luego_a_pagada_con_los_abonos(self):
        cliente = self._crear_cliente("Cliente CxC - abonos")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 100000})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})

        pago1 = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/pagos",
            json={"metodo_pago": "efectivo", "monto": 40000},
        )
        self.assertEqual(pago1.status_code, 201, pago1.get_data(as_text=True))
        self.assertEqual(self._cxc_de_orden(orden["id"])["estado"], "parcial")

        pago2_resp = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/pagos",
            json={"metodo_pago": "efectivo", "monto": 60000},
        )
        self.assertEqual(self._cxc_de_orden(orden["id"])["estado"], "pagada")

        # Al eliminar un abono, el estado vuelve a recalcularse (no queda
        # "pagada" congelada si en realidad ya no está toda la plata).
        pago2_id = pago2_resp.get_json()["id"]
        self.client.delete(f"/api/operativo/ordenes/{orden['id']}/pagos/{pago2_id}")
        self.assertEqual(self._cxc_de_orden(orden["id"])["estado"], "parcial")

    def test_endpoint_de_listado_de_cxc_calcula_el_saldo_pendiente(self):
        cliente = self._crear_cliente("Cliente CxC - listado")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"precio_referencial": 80000})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/pagos",
            json={"metodo_pago": "efectivo", "monto": 30000},
        )

        resp = self.client.get("/api/finanzas/cxc")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        fila = next(c for c in data["cuentas"] if c["orden_id"] == orden["id"])
        self.assertEqual(fila["saldo"], 50000)
        self.assertEqual(fila["estado"], "parcial")


class TestCuentasPorPagar(unittest.TestCase):
    """La CxP se genera sola al registrar una factura de compra (por
    cualquiera de sus caminos: acá se prueba el manual, que es el único
    directamente expuesto por HTTP en este archivo), y un pago registrado
    contra ella recalcula su estado y — si es 'desde_caja_chica' — además
    genera un gasto real en Caja Chica enlazado. Si esto se rompe, el
    taller puede pagarle dos veces a un proveedor por la misma factura, o
    perder el rastro de plata que salió de caja chica para pagar deuda."""

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

    def _crear_factura(self, numero, producto_id, cantidad=5, precio_unitario=1000):
        # Mismo camino que _crear_factura_aplicada en
        # TestEliminarFacturaRevierteSinContaminarConsumo: ya no existe el
        # registro rápido, se pasa por pendiente-de-recepción + aprobar.
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
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

    def _cxp_de_factura(self, factura_id):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            row = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura_id,)).fetchone()
            return dict(row) if row else None

    def test_registrar_una_factura_genera_su_cxp_por_el_total(self):
        producto = self._crear_producto_http("Producto - CxP genera", stock_disponible=0)
        factura = self._crear_factura("FACT-CXP-001", producto["id"], cantidad=5, precio_unitario=2000)

        cxp = self._cxp_de_factura(factura["id"])
        self.assertIsNotNone(cxp)
        self.assertEqual(cxp["monto_total"], 10000)
        self.assertEqual(cxp["estado"], "pendiente")

    def test_pagar_cxp_sin_caja_chica_no_toca_caja_movimientos(self):
        producto = self._crear_producto_http("Producto - CxP pago sin caja", stock_disponible=0)
        factura = self._crear_factura("FACT-CXP-002", producto["id"], cantidad=2, precio_unitario=5000)
        cxp = self._cxp_de_factura(factura["id"])

        antes = self.client.get("/api/caja/resumen").get_json()
        resp = self.client.post(
            f"/api/finanzas/cxp/{cxp['id']}/pagos",
            json={"monto": 4000, "metodo_pago": "transferencia"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        despues = self.client.get("/api/caja/resumen").get_json()
        self.assertEqual(antes["total_gastado"], despues["total_gastado"])

        actualizada = resp.get_json()
        self.assertEqual(actualizada["estado"], "parcial")
        self.assertEqual(actualizada["saldo"], 6000)

    def test_pagar_cxp_desde_caja_chica_genera_gasto_enlazado(self):
        producto = self._crear_producto_http("Producto - CxP pago con caja", stock_disponible=0)
        factura = self._crear_factura("FACT-CXP-003", producto["id"], cantidad=1, precio_unitario=8000)
        cxp = self._cxp_de_factura(factura["id"])

        antes = self.client.get("/api/caja/resumen").get_json()
        resp = self.client.post(
            f"/api/finanzas/cxp/{cxp['id']}/pagos",
            json={"monto": 8000, "metodo_pago": "efectivo", "desde_caja_chica": True},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        despues = self.client.get("/api/caja/resumen").get_json()
        self.assertEqual(despues["total_gastado"], antes["total_gastado"] + 8000)

        actualizada = resp.get_json()
        self.assertEqual(actualizada["estado"], "pagada")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            pago = db.execute(
                "SELECT * FROM cxp_pagos WHERE cxp_id = ? ORDER BY id DESC LIMIT 1", (cxp["id"],)
            ).fetchone()
            self.assertIsNotNone(pago["caja_movimiento_id"])
            mov = db.execute(
                "SELECT * FROM caja_movimientos WHERE id = ?", (pago["caja_movimiento_id"],)
            ).fetchone()
            self.assertEqual(mov["categoria"], "Pago a proveedor")
            self.assertEqual(mov["monto"], 8000)

    def test_no_se_puede_pagar_mas_del_saldo_pendiente(self):
        producto = self._crear_producto_http("Producto - CxP excede saldo", stock_disponible=0)
        factura = self._crear_factura("FACT-CXP-004", producto["id"], cantidad=1, precio_unitario=3000)
        cxp = self._cxp_de_factura(factura["id"])

        resp = self.client.post(
            f"/api/finanzas/cxp/{cxp['id']}/pagos",
            json={"monto": 999999, "metodo_pago": "efectivo"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_endpoint_de_listado_de_cxp_calcula_el_saldo_pendiente(self):
        producto = self._crear_producto_http("Producto - CxP listado", stock_disponible=0)
        factura = self._crear_factura("FACT-CXP-005", producto["id"], cantidad=1, precio_unitario=7000)
        cxp = self._cxp_de_factura(factura["id"])
        self.client.post(f"/api/finanzas/cxp/{cxp['id']}/pagos", json={"monto": 2000, "metodo_pago": "efectivo"})

        resp = self.client.get("/api/finanzas/cxp")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        fila = next(c for c in data["cuentas"] if c["id"] == cxp["id"])
        self.assertEqual(fila["saldo"], 5000)
        self.assertEqual(fila["estado"], "parcial")


class TestCuentasPorPagarManual(unittest.TestCase):
    """Cierre de la Fase 2 de Finanzas: además de las CxP que se generan
    solas desde una factura de compra, Finanzas puede registrar a mano una
    deuda que no viene de una factura de repuestos (arriendo, servicios,
    contador). Se prueba que quede con factura_id NULL, que solo se pueda
    anular si no tiene abonos y no viene de una factura, y que el bloqueo
    por rol sea real (solo finanzas/admin)."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin_client = taller_app.app.test_client()
        cls.admin_client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_usuario(self, username, password, rol):
        resp = self.admin_client.post(
            "/api/admin/usuarios",
            json={"username": username, "password": password, "nombre": username, "rol": rol},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _client_como(self, username, password):
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    def test_crear_cxp_manual_queda_pendiente_sin_factura(self):
        resp = self.admin_client.post(
            "/api/finanzas/cxp",
            json={"concepto": "Arriendo julio", "monto_total": 350000},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertIsNone(data["factura_id"])
        self.assertEqual(data["estado"], "pendiente")
        self.assertEqual(data["saldo"], 350000)

        listado = self.admin_client.get("/api/finanzas/cxp").get_json()
        fila = next(c for c in listado["cuentas"] if c["id"] == data["id"])
        self.assertEqual(fila["concepto"], "ARRIENDO JULIO")

    def test_crear_cxp_manual_sin_concepto_falla(self):
        resp = self.admin_client.post("/api/finanzas/cxp", json={"monto_total": 1000})
        self.assertEqual(resp.status_code, 400)

    def test_crear_cxp_manual_sin_monto_valido_falla(self):
        resp = self.admin_client.post("/api/finanzas/cxp", json={"concepto": "Test", "monto_total": 0})
        self.assertEqual(resp.status_code, 400)

    def test_anular_cxp_manual_sin_abonos_funciona(self):
        creada = self.admin_client.post(
            "/api/finanzas/cxp", json={"concepto": "Gasto a anular", "monto_total": 5000},
        ).get_json()
        resp = self.admin_client.post(f"/api/finanzas/cxp/{creada['id']}/anular")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["estado"], "anulada")

    def test_anular_cxp_manual_con_abonos_falla(self):
        creada = self.admin_client.post(
            "/api/finanzas/cxp", json={"concepto": "Gasto con abono", "monto_total": 8000},
        ).get_json()
        self.admin_client.post(f"/api/finanzas/cxp/{creada['id']}/pagos", json={"monto": 2000, "metodo_pago": "efectivo"})
        resp = self.admin_client.post(f"/api/finanzas/cxp/{creada['id']}/anular")
        self.assertEqual(resp.status_code, 400)

    def test_anular_cxp_de_factura_falla(self):
        producto = self.admin_client.post(
            "/api/productos", json={"nombre": "Producto - CxP manual anular factura", "stock_disponible": 0, "costo_compra": 1000},
        ).get_json()
        pendiente = self.admin_client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-CXPM-001",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 1000}],
                "condicion_pago": "credito",
            },
        ).get_json()
        factura = self.admin_client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={}
        ).get_json()
        with taller_app.app.app_context():
            db = taller_app.get_db()
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
        resp = self.admin_client.post(f"/api/finanzas/cxp/{cxp['id']}/anular")
        self.assertEqual(resp.status_code, 400)

    def test_solo_finanzas_o_admin_puede_crear_cxp_manual(self):
        self._crear_usuario("operativo_cxpm", "clave123", "operativo")
        client_operativo = self._client_como("operativo_cxpm", "clave123")
        resp = client_operativo.post("/api/finanzas/cxp", json={"concepto": "No debería poder", "monto_total": 1000})
        self.assertEqual(resp.status_code, 403)

        self._crear_usuario("finanzas_cxpm", "clave123", "finanzas")
        client_finanzas = self._client_como("finanzas_cxpm", "clave123")
        resp = client_finanzas.post("/api/finanzas/cxp", json={"concepto": "Sí debería poder", "monto_total": 1000})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

    def test_resumen_financiero_incluye_cxc_y_cxp_pendientes(self):
        self.admin_client.post("/api/finanzas/cxp", json={"concepto": "Gasto para resumen", "monto_total": 12345})
        resp = self.admin_client.get("/api/finanzas/resumen")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for campo in ("cxc_pendiente", "cxc_vencido", "cxp_pendiente", "cxp_vencido"):
            self.assertIn(campo, data)
        self.assertGreaterEqual(data["cxp_pendiente"], 12345)


class TestFacturasPendientesDeRecepcion(unittest.TestCase):
    """Una factura que Finanzas registra con condiciones de pago (contado /
    crédito / mixto) no debe tocar el inventario ni generar Cuentas por
    Pagar hasta que alguien contraste lo recibido físicamente y la
    apruebe (o la rechace). Si esto se rompe, se podría inflar el stock o
    generar una deuda a proveedor por mercadería que en realidad nunca
    llegó al taller, o que llegó en menor cantidad de la pactada."""

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

    def _crear_pendiente(self, numero, producto_id, cantidad=5, precio_unitario=1000, **extra):
        body = {
            "numero": numero,
            "fecha": datetime.now().strftime("%Y-%m-%d"),
            "lineas": [{"producto_id": producto_id, "cantidad": cantidad, "precio_unitario": precio_unitario}],
            "condicion_pago": "credito",
        }
        body.update(extra)
        resp = self.client.post("/api/facturas/pendientes-recepcion", json=body)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_crear_pendiente_no_toca_stock_ni_genera_cxp(self):
        producto = self._crear_producto_http("Producto - pendiente no toca stock", stock_disponible=0)
        self._crear_pendiente("FACT-PEND-001", producto["id"], cantidad=10, precio_unitario=1000)

        sin_cambios = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(sin_cambios["stock_disponible"], 0)

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT id FROM facturas WHERE numero = ?", ("FACT-PEND-001",)).fetchone()
            cxp = db.execute(
                "SELECT COUNT(*) FROM cuentas_por_pagar cxp JOIN facturas f ON f.id = cxp.factura_id WHERE f.numero = ?",
                ("FACT-PEND-001",),
            ).fetchone()[0]
        self.assertIsNone(factura)
        self.assertEqual(cxp, 0)

    def test_no_se_puede_crear_dos_pendientes_con_el_mismo_numero(self):
        producto = self._crear_producto_http("Producto - pendiente numero duplicado", stock_disponible=0)
        self._crear_pendiente("FACT-PEND-DUP-001", producto["id"])
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-DUP-001",
                "lineas": [{"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 1000}],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_aprobar_tal_cual_facturado_aplica_stock_y_genera_cxp_a_credito(self):
        producto = self._crear_producto_http("Producto - aprobar credito", stock_disponible=0)
        pendiente = self._crear_pendiente(
            "FACT-PEND-CRED-001", producto["id"], cantidad=4, precio_unitario=2500,
            condicion_pago="credito", plazo_dias_credito=60,
        )
        resp = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 4)

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-CRED-001",)).fetchone()
            self.assertEqual(factura["condicion_pago"], "credito")
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
            self.assertEqual(cxp["monto_total"], 10000)
            self.assertEqual(cxp["estado"], "pendiente")
            dias = (datetime.fromisoformat(cxp["fecha_vencimiento"]) - datetime.fromisoformat(cxp["fecha_emision"])).days
            self.assertEqual(dias, 60)

        pend_actualizado = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        self.assertEqual(pend_actualizado["estado"], "aprobada")

    def test_aprobar_al_contado_genera_cxp_ya_pagada(self):
        producto = self._crear_producto_http("Producto - aprobar contado", stock_disponible=0)
        pendiente = self._crear_pendiente(
            "FACT-PEND-CONT-001", producto["id"], cantidad=2, precio_unitario=5000, condicion_pago="contado",
        )
        resp = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-CONT-001",)).fetchone()
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
            self.assertEqual(cxp["estado"], "pagada")
            self.assertEqual(cxp["monto_total"], 10000)
            pago = db.execute("SELECT * FROM cxp_pagos WHERE cxp_id = ?", (cxp["id"],)).fetchone()
            self.assertEqual(pago["monto"], 10000)
            self.assertEqual(pago["metodo_pago"], "contado")

    def test_aprobar_mixto_genera_dos_cxp_una_pagada_y_una_pendiente(self):
        producto = self._crear_producto_http("Producto - aprobar mixto", stock_disponible=0)
        pendiente = self._crear_pendiente(
            "FACT-PEND-MIX-001", producto["id"], cantidad=10, precio_unitario=1000,
            condicion_pago="mixto", porcentaje_contado=50, plazo_dias_credito=30,
        )
        resp = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-MIX-001",)).fetchone()
            cxps = db.execute(
                "SELECT * FROM cuentas_por_pagar WHERE factura_id = ? ORDER BY id", (factura["id"],)
            ).fetchall()
        self.assertEqual(len(cxps), 2)
        montos_por_estado = {c["estado"]: c["monto_total"] for c in cxps}
        self.assertEqual(montos_por_estado["pagada"], 5000)
        self.assertEqual(montos_por_estado["pendiente"], 5000)

    def test_mixto_sin_porcentaje_valido_es_rechazado_al_crear(self):
        producto = self._crear_producto_http("Producto - mixto invalido", stock_disponible=0)
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-MIX-INVALIDO",
                "lineas": [{"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 1000}],
                "condicion_pago": "mixto",
                "porcentaje_contado": 0,
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_aprobar_con_cantidad_recibida_menor_ajusta_stock_y_monto_de_cxp(self):
        # Se facturaron 10 unidades pero solo llegaron físicamente 6 — el
        # stock que se aplica y lo que se le debe al proveedor tienen que
        # reflejar lo que de verdad llegó, no lo que decía la factura.
        producto = self._crear_producto_http("Producto - recepcion parcial", stock_disponible=0)
        pendiente = self._crear_pendiente(
            "FACT-PEND-PARCIAL-001", producto["id"], cantidad=10, precio_unitario=1000, condicion_pago="credito",
        )
        resp = self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar",
            json={"lineas_recibidas": [{"producto_id": producto["id"], "cantidad_recibida": 6}]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 6)

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-PARCIAL-001",)).fetchone()
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
        self.assertEqual(cxp["monto_total"], 6000)

    def test_aprobar_con_cantidad_recibida_cero_no_falla_por_falta_de_lineas_validas(self):
        producto = self._crear_producto_http("Producto - recepcion nada llego", stock_disponible=0)
        pendiente = self._crear_pendiente("FACT-PEND-CERO-001", producto["id"], cantidad=5, precio_unitario=1000)
        resp = self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar",
            json={"lineas_recibidas": [{"producto_id": producto["id"], "cantidad_recibida": 0}]},
        )
        self.assertEqual(resp.status_code, 400)

    def test_rechazar_no_aplica_stock_ni_crea_factura(self):
        producto = self._crear_producto_http("Producto - pendiente rechazada", stock_disponible=0)
        pendiente = self._crear_pendiente("FACT-PEND-RECH-001", producto["id"], cantidad=5, precio_unitario=1000)

        resp = self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/rechazar",
            json={"nota": "Llegó dañada, se devolvió completa al proveedor."},
        )
        self.assertEqual(resp.status_code, 200)

        sin_cambios = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(sin_cambios["stock_disponible"], 0)
        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT id FROM facturas WHERE numero = ?", ("FACT-PEND-RECH-001",)).fetchone()
        self.assertIsNone(factura)

        pend_actualizado = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        self.assertEqual(pend_actualizado["estado"], "rechazada")
        self.assertEqual(pend_actualizado["nota_rechazo"], "Llegó dañada, se devolvió completa al proveedor.")

    def test_no_se_puede_aprobar_ni_rechazar_dos_veces(self):
        producto = self._crear_producto_http("Producto - doble procesamiento", stock_disponible=0)
        pendiente = self._crear_pendiente("FACT-PEND-DOBLE-001", producto["id"])
        self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})

        resp_aprobar_de_nuevo = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp_aprobar_de_nuevo.status_code, 400)
        resp_rechazar_despues = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/rechazar", json={})
        self.assertEqual(resp_rechazar_despues.status_code, 400)

    def test_contador_de_pendientes_refleja_solo_las_en_estado_pendiente(self):
        producto = self._crear_producto_http("Producto - contador pendientes", stock_disponible=0)
        antes = self.client.get("/api/facturas/pendientes-recepcion/contador").get_json()["pendientes"]

        p1 = self._crear_pendiente("FACT-PEND-CONTADOR-001", producto["id"])
        self._crear_pendiente("FACT-PEND-CONTADOR-002", producto["id"])
        self.client.post(f"/api/facturas/pendientes-recepcion/{p1['id']}/aprobar", json={})

        despues = self.client.get("/api/facturas/pendientes-recepcion/contador").get_json()["pendientes"]
        # Se crearon 2, se aprobó 1 -> debe quedar 1 más que "antes".
        self.assertEqual(despues, antes + 1)


class TestRolesYPermisos(unittest.TestCase):
    """Los roles (admin/finanzas/bodega/operativo — ver ROLES_VALIDOS en
    core.py) no son solo cosméticos: los endpoints sensibles rechazan la
    acción en el backend si la sesión activa no tiene el rol
    correspondiente (ver requiere_rol), aunque se llame directo a la API
    sin pasar por ningún botón. Si esto se rompe, cualquier usuario podría
    aprobar recepciones de mercadería o crear facturas de compra sin que
    le correspondiera, o administrar cuentas de otras personas."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin_client = taller_app.app.test_client()
        cls.admin_client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_usuario(self, username, password, rol):
        resp = self.admin_client.post(
            "/api/admin/usuarios",
            json={"username": username, "password": password, "nombre": username, "rol": rol},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _client_como(self, username, password):
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    def _crear_producto_http(self, client, nombre, stock_disponible=0, costo_compra=1000):
        resp = client.post(
            "/api/productos",
            json={"nombre": nombre, "stock_disponible": stock_disponible, "costo_compra": costo_compra},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_pendiente_recepcion(self, client, producto_id, numero):
        return client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "lineas": [{"producto_id": producto_id, "cantidad": 1, "precio_unitario": 1000}],
                "condicion_pago": "credito",
            },
        )

    def test_usuario_nuevo_hereda_el_rol_asignado_al_hacer_login(self):
        self._crear_usuario("rol_test_finanzas1", "clave123456", "finanzas")
        client = self._client_como("rol_test_finanzas1", "clave123456")
        sesion = client.get("/api/sesion").get_json()
        self.assertEqual(sesion["rol"], "finanzas")

    def test_bodega_no_puede_crear_factura_pendiente_de_recepcion(self):
        self._crear_usuario("rol_test_bodega1", "clave123456", "bodega")
        client = self._client_como("rol_test_bodega1", "clave123456")
        producto = self._crear_producto_http(self.admin_client, "Producto - rol bodega crea factura")
        resp = self._crear_pendiente_recepcion(client, producto["id"], "FACT-ROL-001")
        self.assertEqual(resp.status_code, 403)

    def test_finanzas_puede_crear_factura_pendiente_de_recepcion(self):
        self._crear_usuario("rol_test_finanzas2", "clave123456", "finanzas")
        client = self._client_como("rol_test_finanzas2", "clave123456")
        producto = self._crear_producto_http(self.admin_client, "Producto - rol finanzas crea factura")
        resp = self._crear_pendiente_recepcion(client, producto["id"], "FACT-ROL-002")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

    def test_finanzas_no_puede_aprobar_recepcion(self):
        self._crear_usuario("rol_test_finanzas3", "clave123456", "finanzas")
        client = self._client_como("rol_test_finanzas3", "clave123456")
        producto = self._crear_producto_http(self.admin_client, "Producto - rol finanzas no aprueba")
        pendiente = self._crear_pendiente_recepcion(client, producto["id"], "FACT-ROL-003").get_json()
        resp = client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 403)

    def test_bodega_puede_aprobar_recepcion_creada_por_finanzas(self):
        self._crear_usuario("rol_test_bodega2", "clave123456", "bodega")
        bodega_client = self._client_como("rol_test_bodega2", "clave123456")
        self._crear_usuario("rol_test_finanzas4", "clave123456", "finanzas")
        finanzas_client = self._client_como("rol_test_finanzas4", "clave123456")
        producto = self._crear_producto_http(self.admin_client, "Producto - rol bodega aprueba")
        pendiente = self._crear_pendiente_recepcion(finanzas_client, producto["id"], "FACT-ROL-004").get_json()
        resp = bodega_client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

    def test_operativo_no_puede_crear_ni_aprobar_facturas(self):
        self._crear_usuario("rol_test_operativo1", "clave123456", "operativo")
        client = self._client_como("rol_test_operativo1", "clave123456")
        producto = self._crear_producto_http(self.admin_client, "Producto - rol operativo bloqueado")
        resp = self._crear_pendiente_recepcion(client, producto["id"], "FACT-ROL-005")
        self.assertEqual(resp.status_code, 403)

    def test_usuario_desactivado_no_puede_iniciar_sesion(self):
        creado = self._crear_usuario("rol_test_desactivado1", "clave123456", "operativo")
        resp_edit = self.admin_client.put(f"/api/admin/usuarios/{creado['id']}", json={"activo": False})
        self.assertEqual(resp_edit.status_code, 200, resp_edit.get_data(as_text=True))
        resp_login = self.admin_client.post("/login", data={"username": "rol_test_desactivado1", "password": "clave123456"})
        self.assertEqual(resp_login.status_code, 403)

    def test_admin_no_puede_desactivar_su_propia_cuenta(self):
        sesion = self.admin_client.get("/api/sesion").get_json()
        usuarios = self.admin_client.get("/api/admin/usuarios").get_json()["usuarios"]
        admin_id = next(u["id"] for u in usuarios if u["username"] == sesion["username"])
        resp = self.admin_client.put(f"/api/admin/usuarios/{admin_id}", json={"activo": False})
        self.assertEqual(resp.status_code, 400)

    def test_solo_admin_puede_administrar_usuarios(self):
        self._crear_usuario("rol_test_finanzas5", "clave123456", "finanzas")
        client = self._client_como("rol_test_finanzas5", "clave123456")
        resp = client.get("/api/admin/usuarios")
        self.assertEqual(resp.status_code, 403)

    def test_resetear_password_permite_iniciar_sesion_con_la_nueva(self):
        creado = self._crear_usuario("rol_test_reset1", "clave123456", "operativo")
        resp = self.admin_client.post(
            f"/api/admin/usuarios/{creado['id']}/resetear-password", json={"password": "nuevaclave789"}
        )
        self.assertEqual(resp.status_code, 200)
        client = taller_app.app.test_client()
        resp_login_vieja = client.post("/login", data={"username": "rol_test_reset1", "password": "clave123456"})
        self.assertEqual(resp_login_vieja.status_code, 401)
        resp_login_nueva = client.post("/login", data={"username": "rol_test_reset1", "password": "nuevaclave789"})
        self.assertEqual(resp_login_nueva.status_code, 302)

    def test_usuario_viejo_creado_por_el_panel_historico_de_logistica_queda_como_operativo(self):
        # /api/usuarios es el panel de creación de cuentas que ya existía
        # en Logística antes de que hubiera roles — ahora solo lo puede
        # usar admin, y como no tiene selector de rol, el usuario creado
        # por ahí debe quedar con el rol de menos privilegios (operativo),
        # nunca heredar 'admin' del valor por defecto de la columna.
        resp = self.admin_client.post(
            "/api/usuarios",
            json={"username": "rol_test_legado1", "password": "clave1234", "nombre": "Legado"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        client = self._client_como("rol_test_legado1", "clave1234")
        sesion = client.get("/api/sesion").get_json()
        self.assertEqual(sesion["rol"], "operativo")


class TestProveedoresDeduplicacion(unittest.TestCase):
    """El RUT es el identificador confiable de un proveedor — un nombre
    escrito distinto (typo de la IA leyendo una foto, mayúsculas/espacios
    distintos, razón social abreviada) no debería generar un proveedor
    duplicado si el RUT ya existe. Cubre tanto la creación manual
    (POST /api/proveedores) como los caminos que crean un proveedor "al
    vuelo" (WhatsApp, Excel, foto) reutilizando _buscar_proveedor_existente."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def test_crear_proveedor_con_rut_duplicado_falla_aunque_el_nombre_sea_distinto(self):
        r1 = self.client.post("/api/proveedores", json={"nombre": "Repuestos Dedup Uno", "rut": "76543210-5"})
        self.assertEqual(r1.status_code, 201, r1.get_data(as_text=True))

        r2 = self.client.post("/api/proveedores", json={"nombre": "Repuestos Dedup Uno SPA (otro nombre)", "rut": "76.543.210-5"})
        self.assertEqual(r2.status_code, 400, r2.get_data(as_text=True))
        self.assertIn("RUT", r2.get_json()["error"])

    def test_editar_proveedor_con_rut_de_otro_proveedor_falla(self):
        a = self.client.post("/api/proveedores", json={"nombre": "Proveedor Dedup A", "rut": "11111111-1"}).get_json()
        b = self.client.post("/api/proveedores", json={"nombre": "Proveedor Dedup B", "rut": "22222222-2"}).get_json()

        resp = self.client.put(f"/api/proveedores/{b['id']}", json={"rut": "11.111.111-1"})
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("RUT", resp.get_json()["error"])

    def test_whatsapp_no_duplica_proveedor_existente_con_nombre_distinto_pero_mismo_rut(self):
        # Reproduce el bug reportado: una factura recibida por WhatsApp de
        # un proveedor que ya existe (pero con el nombre escrito distinto)
        # no debe crear un proveedor nuevo si el RUT calza.
        existente = self.client.post(
            "/api/proveedores", json={"nombre": "Repuestos Original SPA", "rut": "89012345-1"}
        ).get_json()

        telefono = "+56945566001"
        self.client.post("/api/agente/mecanicos", json={"nombre": "Mecánico Dedup WhatsApp", "telefono": telefono})
        with taller_app.app.app_context():
            db = taller_app.get_db()
            producto_id = _crear_producto(db, "PRODUCTO DEDUP WHATSAPP", stock_disponible=1)

        respuesta_ia = {
            "numero_factura": "DEDUP-WSP-001",
            "fecha": "2026-07-10",
            "proveedor_nombre": "repuestos original",  # mismo proveedor, nombre distinto
            "proveedor_rut": "89.012.345-1",
            "lineas": [{"descripcion": "PRODUCTO DEDUP WHATSAPP", "cantidad": 1, "precio_unitario": 5000}],
        }
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp", return_value={"tipo": "factura", "patente": ""}), \
             patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self.client.post(
                "/api/agente/simular-mensaje-foto",
                data={"telefono": telefono, "foto": (io.BytesIO(b"foto-falsa"), "factura.jpg")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        pendientes = self.client.get("/api/facturas/pendientes-recepcion?estado=pendiente").get_json()["pendientes"]
        pendiente = next(p for p in pendientes if p["numero"] == "DEDUP-WSP-001")
        self.assertEqual(pendiente["proveedor_id"], existente["id"])

        with taller_app.app.app_context():
            db = taller_app.get_db()
            total_proveedores_con_ese_rut = db.execute(
                "SELECT COUNT(*) AS n FROM proveedores WHERE rut = ?", ("89.012.345-1",)
            ).fetchone()["n"]
        self.assertEqual(total_proveedores_con_ese_rut, 1)


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
