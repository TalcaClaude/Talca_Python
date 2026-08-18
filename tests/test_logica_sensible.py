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


def _crear_producto(db, nombre, stock_disponible=0, costo_compra=0.0, stock_minimo=0, codigo_interno="",
                     categoria="", modelo=""):
    ts = taller_app.now_iso()
    cur = db.execute(
        """
        INSERT INTO productos (
            nombre, codigo_interno, categoria, modelo, stock_disponible, costo_compra, stock_minimo,
            creado_en, actualizado_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (nombre, codigo_interno, categoria, modelo, stock_disponible, costo_compra, stock_minimo, ts, ts),
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
        # registro rápido (POST /api/facturas) — se crea el pendiente, que
        # con el producto ya en el catálogo queda aplicado de inmediato con
        # la ruta de ingreso directo (ver
        # _crear_pendiente_y_aplicar_si_posible en app.py); si por algún
        # motivo no se aplicara solo, se aprueba a mano como respaldo.
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
        if pendiente["estado"] == "aprobada":
            resp2 = self.client.get(f"/api/facturas/{pendiente['factura_id']}")
            self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
            return resp2.get_json()
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
        # _crear_aporte/_crear_gasto no mandan medio_pago -> caen en el
        # default 'efectivo_cajero' (ver MEDIO_PAGO_CAJA_DEFECTO), que es el
        # único bloque que hay que mirar en el resumen desglosado por medio.
        antes = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]

        self._crear_aporte(10000)
        self._crear_gasto(3000)

        despues = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        self.assertAlmostEqual(
            despues["saldo_disponible"] - antes["saldo_disponible"], 10000 - 3000, delta=0.01
        )
        self.assertAlmostEqual(despues["total_aportado"] - antes["total_aportado"], 10000, delta=0.01)
        self.assertAlmostEqual(despues["total_gastado"] - antes["total_gastado"], 3000, delta=0.01)

    def test_pendiente_de_rendir_cuenta_todo_lo_gastado_que_no_ha_vuelto_por_rendicion(self):
        """"Pendiente de rendir" = total retirado (gastado) menos lo que ya
        volvió a través de una rendición -- sin importar si el retiro tiene
        boleta cargada o no. Ojo: la versión anterior de este cálculo solo
        contaba boletas ya documentadas, así que un retiro recién creado sin
        boleta no aportaba nada ahí -- eso confundía al taller (mostraba $0
        pendiente aunque hubiera plata recién retirada sin ninguna boleta
        todavía). Ahora un retiro nuevo aporta a "pendiente" de inmediato,
        tenga boleta o no, y solo baja cuando se rinde."""
        antes = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        gasto = self._crear_gasto(1500)

        # Un retiro recién creado, SIN boleta todavía, ya cuenta como
        # pendiente -- es plata que salió de la caja y todavía no volvió.
        sin_boleta = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        self.assertAlmostEqual(sin_boleta["pendiente_rendir"] - antes["pendiente_rendir"], 1500, delta=0.01)

        resp_boleta = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta", data={"monto": "1500", "numero_documento": "B-1"}
        )
        self.assertEqual(resp_boleta.status_code, 201, resp_boleta.get_data(as_text=True))
        boleta_id = resp_boleta.get_json()["boletas"][0]["id"]

        # Agregar la boleta no cambia el pendiente (documentar no es lo
        # mismo que rendir).
        con_boleta = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        self.assertAlmostEqual(con_boleta["pendiente_rendir"], sin_boleta["pendiente_rendir"], delta=0.01)

        # Al rendir esa boleta, debería salir de "pendiente de rendir" sin
        # afectar el total gastado (la plata ya se gastó, solo cambia su
        # estado administrativo).
        resp = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_id]})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        final = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        self.assertAlmostEqual(final["pendiente_rendir"], antes["pendiente_rendir"], delta=0.01)
        self.assertAlmostEqual(final["total_gastado"], con_boleta["total_gastado"], delta=0.01)

    def test_resumen_desglosa_saldo_por_medio_de_pago_y_cristian_puede_quedar_negativo(self):
        """El pedido explícito del taller: 3 medios con saldo separado
        (efectivo cajero, tarjeta empresa, cristian), y el saldo de
        'cristian' tiene que poder quedar negativo quedar (el taller le
        debe esa plata) sin que el sistema lo bloquee."""
        antes = self.client.get("/api/caja/resumen").get_json()
        for medio in ("efectivo_cajero", "tarjeta_empresa", "cristian"):
            self.assertIn(medio, antes)

        # Cristian gasta (anticipa a un trabajador) 5000 antes de que el
        # taller le haya transferido nada -> saldo negativo, el taller le
        # debe esos 5000.
        resp = self.client.post(
            "/api/caja/gasto",
            data={
                "fecha": datetime.now().strftime("%Y-%m-%d"), "monto": "5000",
                "categoria": "Repuesto", "descripcion": "boleta de un trabajador",
                "medio_pago": "cristian",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        despues = self.client.get("/api/caja/resumen").get_json()
        self.assertAlmostEqual(
            despues["cristian"]["saldo_disponible"] - antes["cristian"]["saldo_disponible"], -5000, delta=0.01
        )
        # Los otros medios no se tocaron.
        self.assertAlmostEqual(
            despues["efectivo_cajero"]["saldo_disponible"], antes["efectivo_cajero"]["saldo_disponible"], delta=0.01
        )
        self.assertAlmostEqual(
            despues["tarjeta_empresa"]["saldo_disponible"], antes["tarjeta_empresa"]["saldo_disponible"], delta=0.01
        )

    def test_medio_pago_invalido_se_rechaza(self):
        resp = self.client.post(
            "/api/caja/gasto",
            data={
                "fecha": datetime.now().strftime("%Y-%m-%d"), "monto": "1000",
                "descripcion": "test", "medio_pago": "billetera_bitcoin",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_rendicion_no_puede_mezclar_medios_de_pago(self):
        gasto_efectivo = self._crear_gasto(2000)
        boleta_efectivo = self.client.post(
            f"/api/caja/gasto/{gasto_efectivo['id']}/boleta", data={"monto": "2000"}
        ).get_json()["boletas"][0]

        resp_cristian = self.client.post(
            "/api/caja/gasto",
            data={
                "fecha": datetime.now().strftime("%Y-%m-%d"), "monto": "3000",
                "descripcion": "boleta de cristian", "medio_pago": "cristian",
            },
        )
        gasto_cristian = resp_cristian.get_json()
        boleta_cristian = self.client.post(
            f"/api/caja/gasto/{gasto_cristian['id']}/boleta", data={"monto": "3000"}
        ).get_json()["boletas"][0]

        resp = self.client.post(
            "/api/caja/rendicion",
            json={"detalle_ids": [boleta_efectivo["id"], boleta_cristian["id"]]},
        )
        self.assertEqual(resp.status_code, 400)

    def test_reposicion_automatica_de_rendicion_hereda_el_medio_de_pago(self):
        resp_cristian = self.client.post(
            "/api/caja/gasto",
            data={
                "fecha": datetime.now().strftime("%Y-%m-%d"), "monto": "7000",
                "descripcion": "boleta de cristian", "medio_pago": "cristian",
            },
        )
        gasto_cristian = resp_cristian.get_json()
        boleta_cristian = self.client.post(
            f"/api/caja/gasto/{gasto_cristian['id']}/boleta", data={"monto": "7000"}
        ).get_json()["boletas"][0]

        antes = self.client.get("/api/caja/resumen").get_json()["cristian"]
        resp = self.client.post(
            "/api/caja/rendicion",
            json={"detalle_ids": [boleta_cristian["id"]], "reponer_saldo": True},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["medio_pago"], "cristian")

        despues = self.client.get("/api/caja/resumen").get_json()["cristian"]
        # La reposición automática (un aporte 'cristian' por 7000) sube el
        # saldo de vuelta -- si hubiera caído en 'efectivo_cajero' por
        # defecto, este saldo no se habría movido.
        self.assertAlmostEqual(despues["saldo_disponible"] - antes["saldo_disponible"], 7000, delta=0.01)

    def test_rendicion_solo_vincula_las_boletas_realmente_incluidas_en_su_total(self):
        """Hallazgo #9 del informe de revisión (Julio 2026): al generar una
        rendición, el total se calcula descartando boletas que en el camino
        ya quedaron rendidas por otra rendición (ej. una pestaña con el
        listado desactualizado) -- pero antes se guardaba qué boletas
        quedan vinculadas usando la lista cruda enviada por el cliente, sin
        aplicar ese mismo descarte. Eso podía "robarle" una boleta a una
        rendición anterior sin que su total cambiara, dejando ambas
        rendiciones descuadradas respecto a lo que realmente contienen."""
        gasto_a = self._crear_gasto(4000)
        boleta_a = self.client.post(
            f"/api/caja/gasto/{gasto_a['id']}/boleta", data={"monto": "4000"}
        ).get_json()["boletas"][0]
        gasto_b = self._crear_gasto(6000)
        boleta_b = self.client.post(
            f"/api/caja/gasto/{gasto_b['id']}/boleta", data={"monto": "6000"}
        ).get_json()["boletas"][0]

        resp_a = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_a["id"]]})
        self.assertEqual(resp_a.status_code, 201, resp_a.get_data(as_text=True))
        rendicion_a_id = resp_a.get_json()["id"]

        # Rendición B se genera con boleta_a (ya rendida en A, por error o
        # listado desactualizado) Y boleta_b -- el sistema debe descartar
        # boleta_a silenciosamente y rendir solo boleta_b.
        resp_b = self.client.post(
            "/api/caja/rendicion", json={"detalle_ids": [boleta_a["id"], boleta_b["id"]]}
        )
        self.assertEqual(resp_b.status_code, 201, resp_b.get_data(as_text=True))
        rendicion_b = resp_b.get_json()
        self.assertAlmostEqual(rendicion_b["total"], 6000, delta=0.01)

        detalle_a = self.client.get(f"/api/caja/rendicion/{rendicion_a_id}").get_json()
        detalle_b = self.client.get(f"/api/caja/rendicion/{rendicion_b['id']}").get_json()

        # boleta_a debe seguir perteneciendo a la rendición A (su total de
        # $4.000 sigue respaldado por esa misma boleta), y la rendición B
        # solo debe contener boleta_b -- nunca las dos con boleta_a.
        self.assertEqual([b["id"] for b in detalle_a["boletas"]], [boleta_a["id"]])
        self.assertEqual([b["id"] for b in detalle_b["boletas"]], [boleta_b["id"]])

    def test_boleta_no_puede_ser_compra_de_emergencia_y_gasto_por_ot_a_la_vez(self):
        """Hallazgo #10 del informe de revisión: nada impedía marcar una
        misma boleta como "compra de emergencia" (ingresa el repuesto al
        inventario) Y como "gastos por OT" (carga ese mismo monto como
        costo de la orden) -- duplicando el gasto cuando ese repuesto
        efectivamente se usa en la orden."""
        producto = self.client.post(
            "/api/productos", json={"nombre": "Producto - boleta emergencia+OT", "stock_disponible": 0}
        ).get_json()
        cliente = self.client.post("/api/operativo/clientes", json={"nombre": "Cliente boleta doble"}).get_json()
        orden = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente["id"]}).get_json()

        gasto = self._crear_gasto(50000)
        resp = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta",
            data={
                "monto": "50000", "rubro_gav": "gastos_por_ot", "orden_id": str(orden["id"]),
                "producto_id": str(producto["id"]), "cantidad": "1",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("compra de emergencia", resp.get_json()["error"].lower())

        # La misma boleta, pero SOLO como compra de emergencia (sin rubro
        # "gastos_por_ot"), debe seguir funcionando normalmente.
        resp_ok = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta",
            data={"monto": "50000", "producto_id": str(producto["id"]), "cantidad": "1"},
        )
        self.assertEqual(resp_ok.status_code, 201, resp_ok.get_data(as_text=True))

    def test_boletas_no_pueden_exceder_el_monto_del_retiro(self):
        """Hallazgo #11 del informe de revisión: ni al agregar ni al editar
        una boleta se validaba que la suma de boletas de un retiro no
        superara el monto efectivamente retirado -- el exceso se absorbía
        en silencio (quedaba en $0 "pendiente"), y si se rendía con
        reposición automática, el saldo de Caja Chica terminaba inflado con
        dinero que nunca existió."""
        gasto = self._crear_gasto(10000)

        # Primera boleta dentro del monto retirado: ok.
        resp1 = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta", data={"monto": "7000", "numero_documento": "B-1"}
        )
        self.assertEqual(resp1.status_code, 201, resp1.get_data(as_text=True))
        boleta1 = resp1.get_json()["boletas"][0]

        # Segunda boleta que deja el total justificado ($15.000) por sobre
        # lo retirado ($10.000): debe rechazarse, no absorberse en silencio.
        resp2 = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta", data={"monto": "8000", "numero_documento": "B-2"}
        )
        self.assertEqual(resp2.status_code, 400, resp2.get_data(as_text=True))

        # Una boleta que SÍ cabe en lo que queda disponible (10.000 - 7.000
        # = 3.000) se sigue aceptando normalmente.
        resp3 = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta", data={"monto": "3000", "numero_documento": "B-3"}
        )
        self.assertEqual(resp3.status_code, 201, resp3.get_data(as_text=True))

        # Editar la primera boleta para subirla de forma que también exceda
        # el monto retirado debe rechazarse igual (7000 -> 9000 dejaría
        # 9000 + 3000 = 12000 > 10000 retirado).
        resp_edit = self.client.put(
            f"/api/caja/boleta/{boleta1['id']}", data={"monto": "9000", "numero_documento": "B-1"}
        )
        self.assertEqual(resp_edit.status_code, 400, resp_edit.get_data(as_text=True))

        # Pero editarla a un monto que sigue cabiendo (7000 -> 6000, dejando
        # 6000 + 3000 = 9000 <= 10000) debe aceptarse.
        resp_edit_ok = self.client.put(
            f"/api/caja/boleta/{boleta1['id']}", data={"monto": "6000", "numero_documento": "B-1"}
        )
        self.assertEqual(resp_edit_ok.status_code, 200, resp_edit_ok.get_data(as_text=True))


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

    def test_orden_entregada_no_admite_eliminar_items(self):
        """Hallazgo #16 del informe de revisión: a diferencia de agregar un
        ítem (bloqueado si la orden está cerrada, ver test de arriba),
        eliminar un ítem no tenía esa misma protección -- se podía borrar
        un repuesto de una orden ya entregada y el stock volvía como si la
        pieza jamás se hubiera usado, aunque siguiera instalada en el
        vehículo ya entregado."""
        producto = self._crear_producto_http("Repuesto para orden - no eliminar si cerrada", 10, 5000)
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        resp_item = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto["id"], "cantidad": 3},
        )
        self.assertEqual(resp_item.status_code, 201, resp_item.get_data(as_text=True))
        item_id = resp_item.get_json()["id"]
        stock_antes = self.client.get(f"/api/productos/{producto['id']}").get_json()["stock_disponible"]

        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})

        resp_del = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/items/{item_id}")
        self.assertEqual(resp_del.status_code, 409, resp_del.get_data(as_text=True))

        # El ítem sigue existiendo y el stock NO volvió -- el repuesto sigue
        # instalado en el vehículo ya entregado.
        detalle = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertTrue(any(it["id"] == item_id for it in detalle["items"]))
        stock_despues = self.client.get(f"/api/productos/{producto['id']}").get_json()["stock_disponible"]
        self.assertAlmostEqual(stock_despues, stock_antes, delta=0.01)

        # Reabriendo la orden, eliminar SÍ debe volver a funcionar (mismo
        # criterio que agregar ítems).
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "reparacion"})
        resp_del_ok = self.client.delete(f"/api/operativo/ordenes/{orden['id']}/items/{item_id}")
        self.assertEqual(resp_del_ok.status_code, 200, resp_del_ok.get_data(as_text=True))
        stock_final = self.client.get(f"/api/productos/{producto['id']}").get_json()["stock_disponible"]
        self.assertAlmostEqual(stock_final - stock_antes, 3, delta=0.01)

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

    def test_presupuesto_marcado_aprobado_no_retrocede_una_orden_ya_avanzada(self):
        """Hallazgo #19 del informe de revisión: el sistema ya evitaba que
        marcar el presupuesto "enviado" retrocediera una orden que había
        avanzado más allá de "esperando_aprobacion" (ver el test de
        arriba), pero esa misma protección le faltaba a "aprobado" -- una
        orden que ya estaba en 'listo' (control de calidad hecho) podía
        saltar de vuelta a 'reparación' solo porque se aprobó un ítem
        adicional del presupuesto, deshaciendo visualmente el trabajo ya
        avanzado."""
        cliente = self._crear_cliente()
        orden = self._crear_orden(cliente["id"])
        # Llega hasta 'listo' por el camino normal.
        self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "aprobado"})
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "control_calidad"})
        resp_listo = self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "listo"})
        self.assertEqual(resp_listo.get_json()["estado"], "listo")

        # Se agrega un ítem nuevo al presupuesto y se vuelve a marcar
        # "aprobado" (ej. el cliente aceptó un adicional) -- la orden NO
        # debería retroceder a 'reparación', sigue en 'listo'.
        self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/presupuesto/items",
            json={"tipo": "mano_obra", "descripcion": "Adicional", "cantidad": 1, "precio_unitario": 5000},
        )
        resp2 = self.client.put(f"/api/operativo/ordenes/{orden['id']}/presupuesto", json={"estado": "aprobado"})
        self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
        self.assertEqual(resp2.get_json()["estado"], "listo")

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


class TestCorrelativoYListadoOperativo(unittest.TestCase):
    """OT y garantías comparten un mismo correlativo desde jul. 2026 (ver
    _asignar_id_correlativo en modulos/operativo.py) para que un número de
    OT nunca corresponda a dos vehículos distintos -- salvo convertidores
    de torque, que quedan aparte a propósito. Además cubre el endpoint
    /api/operativo/listado, que a diferencia de /api/operativo/ordenes trae
    SIEMPRE el historial completo (todos los meses) y mezcla OT + garantías
    con un filtro de tipo."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre="Cliente Correlativo"):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_garantia(self, cliente_id, motivo="Motivo de prueba"):
        resp = self.client.post(
            "/api/operativo/garantias", json={"cliente_id": cliente_id, "motivo": motivo}
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def test_ot_y_garantia_nunca_comparten_numero_y_quedan_correlativas(self):
        cliente = self._crear_cliente()
        orden_1 = self._crear_orden(cliente["id"])
        garantia_1 = self._crear_garantia(cliente["id"])
        orden_2 = self._crear_orden(cliente["id"])
        garantia_2 = self._crear_garantia(cliente["id"])

        ids = [orden_1["id"], garantia_1["id"], orden_2["id"], garantia_2["id"]]
        self.assertEqual(len(ids), len(set(ids)), f"Los IDs no deberían repetirse nunca: {ids}")
        # Cada alta sucesiva (sea OT o garantía) debe sacar un número más
        # alto que todas las anteriores, sin importar de qué tabla venga.
        self.assertTrue(ids[0] < ids[1] < ids[2] < ids[3], ids)

    def test_listado_incluye_historial_completo_no_solo_mes_en_curso(self):
        cliente = self._crear_cliente("Cliente Listado Histórico")
        orden = self._crear_orden(cliente["id"])
        # Se cierra la orden con fecha de entrega de un mes bien viejo, para
        # simular una OT del historial (no del mes en curso).
        db = taller_app._conectar_db()
        db.execute(
            "UPDATE ordenes_trabajo SET estado = 'entregado', fecha_entrega_real = '2020-01-15T10:00:00' WHERE id = ?",
            (orden["id"],),
        )
        db.commit()
        db.close()

        # /api/operativo/ordenes (el tablero) por defecto NO la trae -- es
        # de un mes viejo y ya está cerrada.
        resp_tablero = self.client.get("/api/operativo/ordenes")
        ids_tablero = [o["id"] for o in resp_tablero.get_json()["ordenes"]]
        self.assertNotIn(orden["id"], ids_tablero)

        # /api/operativo/listado sí, porque siempre trae todo el historial.
        resp_listado = self.client.get("/api/operativo/listado")
        self.assertEqual(resp_listado.status_code, 200)
        filas = resp_listado.get_json()["filas"]
        fila = next((f for f in filas if f["id"] == orden["id"] and f["tipo"] == "nuevo"), None)
        self.assertIsNotNone(fila, "La OT vieja cerrada debería aparecer en el listado histórico")
        self.assertEqual(fila["estado_label"], "Entregado")

    def test_listado_incluye_garantias_y_filtra_por_tipo(self):
        cliente = self._crear_cliente("Cliente Listado Garantía")
        garantia = self._crear_garantia(cliente["id"], motivo="Falla eléctrica")

        resp_todos = self.client.get("/api/operativo/listado")
        filas_todos = resp_todos.get_json()["filas"]
        fila = next((f for f in filas_todos if f["id"] == garantia["id"] and f["tipo"] == "garantia"), None)
        self.assertIsNotNone(fila)
        self.assertIsNone(fila["valor_total"])
        self.assertIsNone(fila["total_pagado"])

        resp_solo_nuevos = self.client.get("/api/operativo/listado?tipo=nuevo")
        filas_solo_nuevos = resp_solo_nuevos.get_json()["filas"]
        self.assertTrue(all(f["tipo"] == "nuevo" for f in filas_solo_nuevos))
        self.assertFalse(any(f["id"] == garantia["id"] for f in filas_solo_nuevos))

        resp_solo_garantias = self.client.get("/api/operativo/listado?tipo=garantia")
        filas_solo_garantias = resp_solo_garantias.get_json()["filas"]
        self.assertTrue(all(f["tipo"] == "garantia" for f in filas_solo_garantias))
        self.assertTrue(any(f["id"] == garantia["id"] for f in filas_solo_garantias))

    def test_listado_tipo_invalido_da_400(self):
        resp = self.client.get("/api/operativo/listado?tipo=lo-que-sea")
        self.assertEqual(resp.status_code, 400)

    def test_listado_con_desde_acota_lo_cerrado_pero_no_lo_abierto(self):
        # Objetivo del filtro desde/hasta: no traer los ~3.000+ registros
        # del historial completo cada vez que se abre la pestaña, PERO sin
        # esconder nunca trabajo que sigue abierto, sin importar su fecha
        # de ingreso (ver _listado_ordenes_liviano en modulos/operativo.py).
        cliente = self._crear_cliente("Cliente Filtro Fecha")
        orden_vieja_cerrada = self._crear_orden(cliente["id"])
        orden_vieja_abierta = self._crear_orden(cliente["id"])

        db = taller_app._conectar_db()
        db.execute(
            "UPDATE ordenes_trabajo SET fecha_recepcion = '2019-05-01T09:00:00', "
            "estado = 'entregado', fecha_entrega_real = '2019-05-03T09:00:00' WHERE id = ?",
            (orden_vieja_cerrada["id"],),
        )
        db.execute(
            "UPDATE ordenes_trabajo SET fecha_recepcion = '2019-05-01T09:00:00' WHERE id = ?",
            (orden_vieja_abierta["id"],),
        )
        db.commit()
        db.close()

        # Con un "desde" reciente (hoy), la cerrada de 2019 debería quedar
        # afuera -- pero la abierta de 2019 debería seguir apareciendo,
        # porque es trabajo activo sin importar cuándo ingresó.
        hoy = taller_app.now_iso()[:10]
        resp = self.client.get(f"/api/operativo/listado?desde={hoy}")
        ids = [f["id"] for f in resp.get_json()["filas"]]
        self.assertNotIn(orden_vieja_cerrada["id"], ids, "La OT vieja y cerrada no debería aparecer con un 'desde' reciente")
        self.assertIn(orden_vieja_abierta["id"], ids, "La OT vieja pero todavía abierta debería aparecer igual")

        # Sin desde/hasta (botón "Ver historial completo"), aparecen ambas.
        resp_completo = self.client.get("/api/operativo/listado")
        ids_completo = [f["id"] for f in resp_completo.get_json()["filas"]]
        self.assertIn(orden_vieja_cerrada["id"], ids_completo)
        self.assertIn(orden_vieja_abierta["id"], ids_completo)


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

    def test_codigo_exacto_prioriza_por_encima_del_texto(self):
        # Caso reportado por el taller: la IA lee "JF011 PULLEY SET" pero al
        # describir la línea se queda solo con "PULLEY SET" (pierde el
        # código) -- si el catálogo tiene DOS "pulley set" con nombres
        # parecidos, el texto libre solo no alcanza para distinguirlos, pero
        # el código sí. El código, cuando viene y matchea exacto contra
        # codigo_interno (columna única en la BD), tiene que ganarle a
        # cualquier parecido de texto, aunque el texto se parezca más a otro
        # producto.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            correcto_id = _crear_producto(db, "PULLEY SET TESTCODIGO", codigo_interno="JF011TC", stock_disponible=5)
            _crear_producto(db, "PULLEY SET TESTCODIGO EXTRA PARECIDO", codigo_interno="XX999TC", stock_disponible=5)
            producto = taller_app._buscar_producto_por_texto(db, "PULLEY SET TESTCODIGO EXTRA PARECIDO", "JF011TC")
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], correcto_id)

    def test_codigo_que_no_matchea_ningun_producto_cae_al_texto(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            producto_id = _crear_producto(db, "FILTRO DE AIRE BOSCH FA-222 TESTCODIGOFALLBACK", stock_disponible=10)
            producto = taller_app._buscar_producto_por_texto(
                db, "FILTRO DE AIRE BOSCH FA-222 TESTCODIGOFALLBACK", "CODIGO-QUE-NO-EXISTE"
            )
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], producto_id)

    def test_codigo_vacio_no_afecta_el_comportamiento_de_siempre(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            nombre = "AMORTIGUADOR TRASERO TESTCODIGOVACIO"
            producto_id = _crear_producto(db, nombre, stock_disponible=3)
            self.assertEqual(taller_app._buscar_producto_por_texto(db, nombre, "")["id"], producto_id)
            self.assertEqual(taller_app._buscar_producto_por_texto(db, nombre, None)["id"], producto_id)

    # -- Matching por "modelo" (modelo de transmisión, columna 'categoria' en
    # la BD -- ver nombre_automatico en core.py) -- caso real reportado por
    # el taller: la IA lee "JF011 pulley set" pero el catálogo solo tiene
    # "JF010 - PULLEY SET" (un modelo DISTINTO, un dígito de diferencia). El
    # comparador de texto plano de antes matcheaba igual (altísima
    # similitud entre "JF011" y "JF010"), asignando el repuesto al modelo
    # de transmisión equivocado -- un error grave para un taller de
    # transmisiones. Estos tests fijan el comportamiento correcto: el
    # modelo nunca se compara de forma difusa, solo exacto o nada. --

    def test_modelo_exacto_acota_la_busqueda_y_matchea_el_detalle_correcto_de_esa_familia(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            correcto_id = _crear_producto(
                db, "JF010 - PULLEY SET TESTMODELOFAMILIA", categoria="JF010TMF", modelo="PULLEY SET TESTMODELOFAMILIA",
                stock_disponible=5,
            )
            # Mismo modelo de transmisión, pieza distinta -- no debe
            # confundirse aunque comparta el prefijo "JF010TMF".
            _crear_producto(
                db, "JF010 - VALVE BODY TESTMODELOFAMILIA", categoria="JF010TMF", modelo="VALVE BODY TESTMODELOFAMILIA",
                stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(
                db, "JF010TMF PULLEY SET TESTMODELOFAMILIA", modelo="JF010TMF"
            )
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], correcto_id)

    def test_modelo_que_no_existe_en_el_catalogo_no_matchea_aunque_el_texto_se_parezca_mucho(self):
        # El caso exacto reportado: "JF011" (typo/modelo distinto) no
        # existe en el catálogo, solo "JF010" -- por muy parecido que sea
        # el texto, NO se puede caer al comparador de texto general y
        # adivinar "JF010": eso es justo el bug que se está corrigiendo.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(
                db, "JF010 - PULLEY SET TESTMODELOINEXISTENTE", categoria="JF010TMI", modelo="PULLEY SET TESTMODELOINEXISTENTE",
                stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(
                db, "JF011TMI PULLEY SET TESTMODELOINEXISTENTE", modelo="JF011TMI"
            )
            self.assertIsNone(producto)

    def test_modelo_exacto_pero_detalle_sin_relacion_dentro_de_la_familia_no_matchea(self):
        # Caso real reportado: "0BH clutch" terminó matcheado a "0BH -
        # TCU" (un repuesto totalmente distinto: embrague vs. unidad de
        # control electrónica) solo porque compartían el prefijo del
        # modelo. Acotar por modelo exacto y comparar el detalle SIN ese
        # prefijo (que ya no debería inflar el puntaje) tiene que rechazar
        # este match, no forzarlo. (Ojo: a propósito NO se repite ningún
        # sufijo de texto entre el producto y la búsqueda -- compartir un
        # marcador largo "para no chocar entre tests" infla el parecido de
        # texto y falsea el resultado; la unicidad acá la da el código de
        # modelo, que ya es exclusivo de este test.)
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(
                db, "0BHZTDR - TCU", categoria="0BHZTDR", modelo="TCU",
                stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(
                db, "0BHZTDR CLUTCH", modelo="0BHZTDR"
            )
            self.assertIsNone(producto)

    def test_modelo_vacio_cae_al_comparador_de_texto_general_de_siempre(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            nombre = "FILTRO DE ACEITE TESTMODELOVACIO"
            producto_id = _crear_producto(db, nombre, stock_disponible=4)
            self.assertEqual(taller_app._buscar_producto_por_texto(db, nombre, modelo="")["id"], producto_id)
            self.assertEqual(taller_app._buscar_producto_por_texto(db, nombre, modelo=None)["id"], producto_id)

    def test_codigo_exacto_sigue_ganando_por_encima_del_modelo(self):
        # El código de fabricante (match exacto contra codigo_interno) es
        # la señal más fuerte de todas -- tiene que ganarle incluso a un
        # modelo que apunta a una familia distinta (ej. porque la IA leyó
        # mal el modelo pero el código sí lo tenía cargado el taller).
        with taller_app.app.app_context():
            db = taller_app.get_db()
            correcto_id = _crear_producto(
                db, "PULLEY SET TESTPRIORIDADCODIGO", codigo_interno="SKU-PRIORIDAD-1",
                categoria="MODELOA-PRIORIDAD", modelo="PULLEY SET TESTPRIORIDADCODIGO", stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(
                db, "PULLEY SET TESTPRIORIDADCODIGO", codigo="SKU-PRIORIDAD-1", modelo="MODELOB-PRIORIDAD-INEXISTENTE"
            )
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], correcto_id)

    # -- Caso real reportado por el taller (factura de Guangzhou Transpeed):
    # dentro de una familia de modelo ya acotada exacto (ej. "JF011"),
    # comparar por CARACTERES (como se hacía hasta la corrección anterior)
    # le daba demasiado parecido a textos cortos sin relación real --
    # "pulley set" vs "oil filter e" puntuaba 0.455 sobre un umbral de
    # 0.45, y "valve body" vs "brake band" puntuaba 0.5 -- ambos productos
    # completamente distintos, matcheados igual. Estos tests fijan que el
    # comparador por PALABRAS los rechace. --

    def test_dentro_de_la_familia_palabras_sin_relacion_no_matchean_aunque_el_texto_se_parezca(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(
                db, "JF011PSR - OIL FILTER E", categoria="JF011PSR", modelo="OIL FILTER E", stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(db, "PULLEY SET", modelo="JF011PSR")
            self.assertIsNone(producto)

    def test_dentro_de_la_familia_palabras_sin_relacion_no_matchean_caso_valve_body(self):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(
                db, "RE4R01APSR - BRAKE BAND", categoria="RE4R01APSR", modelo="BRAKE BAND", stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(db, "VALVE BODY", modelo="RE4R01APSR")
            self.assertIsNone(producto)

    def test_dentro_de_la_familia_con_palabras_en_comun_si_matchea(self):
        # Contraparte de los dos tests anteriores: "clutch" SÍ debe
        # encontrar "clutch late" dentro de la misma familia, porque
        # comparten la palabra completa (a diferencia de "pulley set"
        # dentro de "oil filter e", que no comparten ninguna).
        with taller_app.app.app_context():
            db = taller_app.get_db()
            correcto_id = _crear_producto(
                db, "0AMPSR - CLUTCH LATE", categoria="0AMPSR", modelo="CLUTCH LATE", stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(db, "CLUTCH", modelo="0AMPSR")
            self.assertIsNotNone(producto)
            self.assertEqual(producto["id"], correcto_id)

    def test_dos_productos_empatados_en_palabras_no_matchean_ninguno(self):
        # Si dentro de la misma familia hay dos productos con exactamente
        # el mismo puntaje (ej. dos ubicaciones/lotes cargados por error
        # con el mismo detalle), no hay forma de saber cuál es el
        # correcto -- mejor dejarlo sin matchear que elegir uno al azar.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(
                db, "U140PSR - CLUTCH SET A", categoria="U140PSR", modelo="CLUTCH SET", stock_disponible=5,
            )
            _crear_producto(
                db, "U140PSR - CLUTCH SET B", categoria="U140PSR", modelo="CLUTCH SET", stock_disponible=5,
            )
            producto = taller_app._buscar_producto_por_texto(db, "CLUTCH SET", modelo="U140PSR")
            self.assertIsNone(producto)


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
        self.assertEqual(linea_sin_matchear["errores"], [])

        # Una línea sin producto asignado YA NO bloquea la factura completa
        # (antes sí) -- puede quedar como pendiente de recepción igual, con
        # esa línea sin resolver; Bodega la completa después (creando el
        # producto que falta, ver crear_y_vincular_producto_pendiente) antes
        # de poder aprobarla (aprobar_factura_pendiente_recepcion sigue
        # exigiendo producto_id en todas las líneas para APROBAR).
        self.assertTrue(grupo["valida"])
        self.assertEqual(grupo["errores"], [])

    def test_foto_con_codigo_leido_prioriza_ese_match_por_encima_del_texto(self):
        # Mismo caso que en TestFotosPorWhatsAppFase3 pero para el camino de
        # "Escanear foto" desde el navegador (ambos comparten
        # _buscar_producto_por_texto): si la IA devuelve un código junto a
        # la descripción, ese código tiene que ganarle a cualquier parecido
        # de texto contra otro producto del catálogo.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            correcto_id = _crear_producto(db, "PULLEY SET TESTFOTOCODIGO", codigo_interno="JF011FT", stock_disponible=5)
            _crear_producto(db, "PULLEY SET TESTFOTOCODIGO OTRO", codigo_interno="XX999FT", stock_disponible=5)
        respuesta_ia = {
            "numero_factura": "FOTO-CODIGO-001",
            "fecha": "2026-07-08",
            "proveedor_nombre": "Proveedor Foto Codigo Test",
            "proveedor_rut": "",
            "lineas": [{
                "codigo": "JF011FT",
                "descripcion": "PULLEY SET TESTFOTOCODIGO OTRO",
                "cantidad": 1,
                "precio_unitario": 12000,
            }],
        }
        with patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        linea = resp.get_json()["facturas"][0]["lineas"][0]
        self.assertEqual(linea["producto_id"], correcto_id)
        self.assertEqual(linea["codigo_leido"], "JF011FT")

    def test_foto_con_modelo_inexistente_no_asigna_el_modelo_parecido_del_catalogo(self):
        # Reproduce el caso real reportado por el taller de punta a punta:
        # una factura de importación trae "JF011 pulley set" (código de
        # fabricante largo, sin relación con el catálogo del taller) --
        # el catálogo solo tiene "JF010", un modelo de transmisión
        # DISTINTO aunque el texto se parezca mucho. La línea tiene que
        # quedar sin matchear, nunca asignada a JF010 por error.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            _crear_producto(
                db, "JF010 - PULLEY SET TESTFOTOMODELOE2E", categoria="JF010FME2E", modelo="PULLEY SET TESTFOTOMODELOE2E",
                stock_disponible=5,
            )
        respuesta_ia = {
            "numero_factura": "FOTO-MODELO-001",
            "fecha": "2026-07-08",
            "proveedor_nombre": "Proveedor Foto Modelo Test",
            "proveedor_rut": "",
            "lineas": [{
                "modelo": "JF011FME2E",
                "codigo": "33710-NKC-7-FX",
                "descripcion": "JF011FME2E PULLEY SET TESTFOTOMODELOE2E",
                "cantidad": 1,
                "precio_unitario": 12000,
            }],
        }
        with patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        linea = resp.get_json()["facturas"][0]["lineas"][0]
        self.assertIsNone(linea["producto_id"])
        self.assertEqual(linea["modelo_leido"], "JF011FME2E")
        # Aunque no matcheó, el texto/modelo/código leídos quedan
        # guardados para mostrarlos en la previsualización -- así Bodega
        # ve exactamente qué leyó la IA y puede decidir a mano.
        self.assertEqual(linea["texto_original"], "JF011FME2E PULLEY SET TESTFOTOMODELOE2E")
        self.assertEqual(linea["codigo_leido"], "33710-NKC-7-FX")

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

        # Ruta de ingreso directo (ver _crear_pendiente_y_aplicar_si_posible
        # en app.py): con las dos líneas ya resueltas contra el catálogo,
        # confirmar la carga masiva aplica la factura de inmediato (stock +
        # CxP) en vez de dejarla esperando que alguien la apruebe aparte.
        resp_confirmar = self.client.post(
            "/api/facturas/carga-masiva/confirmar", json={"token": token, "condicion_pago": "credito"}
        )
        self.assertEqual(resp_confirmar.status_code, 200, resp_confirmar.get_data(as_text=True))
        confirmadas = resp_confirmar.get_json()
        self.assertIn("FOTO-002", confirmadas.get("creadas", []))
        self.assertIn("FOTO-002", confirmadas.get("aplicadas", []))

        pendientes = self.client.get("/api/facturas/pendientes-recepcion").get_json()["pendientes"]
        pendiente_creado = next(p for p in pendientes if p["numero"] == "FOTO-002")
        self.assertEqual(pendiente_creado["estado"], "aprobada")
        self.assertEqual(pendiente_creado["origen"], "foto")
        # No debe haberse creado un proveedor nuevo — tiene que quedar
        # asociada al proveedor existente que se eligió en la corrección.
        self.assertEqual(pendiente_creado["proveedor_id"], proveedor_existente_id)
        # La corrección de la línea 2 (que originalmente no matcheó ningún
        # producto) tiene que haber sobrevivido hasta el pendiente final,
        # no quedarse solo en la previsualización -- este es el bug
        # reportado por el taller: una corrección hecha en la vista previa
        # de carga masiva se perdía y lo que le llegaba a Bodega era la
        # factura tal como se había leído originalmente.
        linea_1, linea_2 = pendiente_creado["lineas"]
        self.assertEqual(linea_1["producto_id"], producto_1_id)
        self.assertEqual(linea_2["producto_id"], producto_2_id)
        # Y el texto que leyó la IA se conserva igual para AMBAS líneas
        # (matcheadas o no), para que Bodega pueda comparar "Leído: ..."
        # contra el producto asignado al revisar y aprobar.
        self.assertEqual(linea_1["texto_original"], "PASTILLAS DE FRENO DELANTERAS TRW TESTFOTO2")
        self.assertEqual(linea_2["texto_original"], "QQQ-XYZ-7777 OTRO TEXTO SIN RELACION ALGUNA")


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

    def test_foto_de_factura_usa_el_codigo_leido_para_no_matchear_el_producto_equivocado(self):
        # Caso real reportado por el taller: la foto trae "JF011 PULLEY
        # SET", pero la IA arma la descripción quedándose solo con "PULLEY
        # SET" (pierde el código) -- si el catálogo tiene dos productos de
        # nombre parecido, el texto solo no alcanza para distinguirlos. Acá
        # se simula justo eso: la IA sí devuelve el código en su propio
        # campo ('codigo'), aparte de una descripción que por sí sola sería
        # ambigua entre dos productos -- el pendiente tiene que quedar
        # ligado al producto CORRECTO (el del código), no al que más se
        # parece por texto.
        telefono = "+56931234567"
        numero = "AG-JF011"
        self._registrar_mecanico("Mecánico Código Test", telefono)
        with taller_app.app.app_context():
            db = taller_app.get_db()
            correcto_id = _crear_producto(db, "PULLEY SET TESTWHATSAPPCODIGO", codigo_interno="JF011WA", stock_disponible=2)
            _crear_producto(db, "PULLEY SET TESTWHATSAPPCODIGO OTRO MODELO", codigo_interno="XX999WA", stock_disponible=2)

        respuesta_ia = {
            "numero_factura": numero,
            "fecha": "2026-07-09",
            "proveedor_nombre": "Repuestos Código Test",
            "proveedor_rut": "",
            "lineas": [{
                "codigo": "JF011WA",
                "descripcion": "PULLEY SET TESTWHATSAPPCODIGO OTRO MODELO",
                "cantidad": 1,
                "precio_unitario": 12000,
            }],
        }
        with patch.object(taller_app, "_llamar_claude_vision_clasificar_foto_whatsapp", return_value={"tipo": "factura", "patente": ""}), \
             patch.object(taller_app, "_llamar_claude_vision_factura", return_value=respuesta_ia):
            resp = self._subir_foto_por_agente(telefono)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        pendientes = self.client.get("/api/facturas/pendientes-recepcion?estado=pendiente").get_json()["pendientes"]
        pendiente = next(p for p in pendientes if p["numero"] == numero and p["origen"] == "whatsapp")
        detalle = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        linea = detalle["lineas"][0]
        self.assertEqual(linea["producto_id"], correcto_id)
        # El código leído queda guardado en la línea para que la pantalla de
        # revisión lo muestre siempre junto al producto asignado (ver
        # pfd-lineas en finanzas.js / afp-tbody-lineas en
        # alerta_facturas_pendientes.js) -- así un match automático
        # equivocado salta a la vista antes de aprobar.
        self.assertEqual(linea["codigo_leido"], "JF011WA")

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

    def _crear_orden(self, cliente_id, equipo_id=None):
        payload = {"cliente_id": cliente_id}
        if equipo_id is not None:
            payload["equipo_id"] = equipo_id
        resp = self.client.post("/api/operativo/ordenes", json=payload)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_equipo(self, cliente_id, identificador="XX0000"):
        resp = self.client.post(
            "/api/operativo/equipos",
            json={"cliente_id": cliente_id, "identificador": identificador, "marca": "Marca", "modelo": "Modelo"},
        )
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

    def test_reabrir_una_orden_entregada_limpia_la_fecha_de_entrega_anterior(self):
        """Hallazgo #17 del informe de revisión: reabrir una orden ya
        entregada no borraba la fecha de la entrega anterior -- si esa OT
        se volvía a entregar más adelante, el sistema seguía usando la
        fecha de la PRIMERA entrega (para la CxC y los reportes de
        capacidad semanal), pudiendo nacer una cuenta "vencida" el mismo
        día en que se reentrega el auto."""
        cliente = self._crear_cliente("Cliente fecha entrega - reabrir")
        orden = self._crear_orden(cliente["id"])
        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        entregada = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertIsNotNone(entregada["fecha_entrega_real"])

        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "reparacion"})
        reabierta = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertIsNone(reabierta["fecha_entrega_real"], "la fecha de la primera entrega debería limpiarse al reabrir")

        self.client.put(f"/api/operativo/ordenes/{orden['id']}", json={"estado": "entregado"})
        reentregada = self.client.get(f"/api/operativo/ordenes/{orden['id']}").get_json()
        self.assertIsNotNone(reentregada["fecha_entrega_real"])

    def test_reabrir_orden_entregada_respeta_un_solo_trabajo_abierto_por_vehiculo(self):
        """Hallazgo #18 del informe de revisión: la regla de "un vehículo
        no puede tener dos OT abiertas a la vez" se aplicaba al CREAR una
        orden nueva (ver crear_orden), pero no al REABRIR una que ya
        estaba cerrada -- se podía terminar con dos OT abiertas para el
        mismo auto (una nueva y otra reabierta), confundiendo en cuál
        cargar repuestos."""
        cliente = self._crear_cliente("Cliente un solo trabajo - reabrir")
        equipo = self._crear_equipo(cliente["id"], identificador="REABRIR-01")

        orden_vieja = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        self.client.put(f"/api/operativo/ordenes/{orden_vieja['id']}", json={"estado": "entregado"})

        # El auto vuelve al taller por otro motivo: se crea una OT nueva
        # (permitido, la vieja ya está entregada).
        orden_nueva = self._crear_orden(cliente["id"], equipo_id=equipo["id"])
        self.assertNotEqual(orden_nueva["id"], orden_vieja["id"])

        # Reabrir la OT vieja ahora debe bloquearse: el mismo vehículo ya
        # tiene la OT nueva abierta.
        resp = self.client.put(f"/api/operativo/ordenes/{orden_vieja['id']}", json={"estado": "reparacion"})
        self.assertEqual(resp.status_code, 409, resp.get_data(as_text=True))
        self.assertIn(str(orden_nueva["id"]), resp.get_json()["error"])

        # Si se cancela la OT nueva, reabrir la vieja sí debe funcionar.
        self.client.put(f"/api/operativo/ordenes/{orden_nueva['id']}", json={"estado": "cancelado"})
        resp_ok = self.client.put(f"/api/operativo/ordenes/{orden_vieja['id']}", json={"estado": "reparacion"})
        self.assertEqual(resp_ok.status_code, 200, resp_ok.get_data(as_text=True))

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
        # registro rápido, se pasa por pendiente-de-recepción + aprobar --
        # salvo que, con el producto ya en el catálogo, la ruta de ingreso
        # directo (ver _crear_pendiente_y_aplicar_si_posible en app.py) la
        # deja aplicada de una sola vez, sin necesitar el segundo paso.
        #
        # exento_iva=True a propósito: esta clase prueba la mecánica de CxP
        # (generación, pagos, saldo) con montos redondos y fáciles de leer,
        # no el cálculo de Neto/IVA en sí -- ese tiene su propia cobertura
        # en TestNetoIvaAutomaticoEnFacturas más abajo.
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_id": producto_id, "cantidad": cantidad, "precio_unitario": precio_unitario}],
                "condicion_pago": "credito",
                "exento_iva": True,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        pendiente = resp.get_json()
        if pendiente["estado"] == "aprobada":
            resp2 = self.client.get(f"/api/facturas/{pendiente['factura_id']}")
            self.assertEqual(resp2.status_code, 200, resp2.get_data(as_text=True))
            return resp2.get_json()
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

        antes = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        resp = self.client.post(
            f"/api/finanzas/cxp/{cxp['id']}/pagos",
            json={"monto": 4000, "metodo_pago": "transferencia"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        despues = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        self.assertEqual(antes["total_gastado"], despues["total_gastado"])

        actualizada = resp.get_json()
        self.assertEqual(actualizada["estado"], "parcial")
        self.assertEqual(actualizada["saldo"], 6000)

    def test_pagar_cxp_desde_caja_chica_genera_gasto_enlazado(self):
        producto = self._crear_producto_http("Producto - CxP pago con caja", stock_disponible=0)
        factura = self._crear_factura("FACT-CXP-003", producto["id"], cantidad=1, precio_unitario=8000)
        cxp = self._cxp_de_factura(factura["id"])

        antes = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
        resp = self.client.post(
            f"/api/finanzas/cxp/{cxp['id']}/pagos",
            json={"monto": 8000, "metodo_pago": "efectivo", "desde_caja_chica": True},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        despues = self.client.get("/api/caja/resumen").get_json()["efectivo_cajero"]
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


class TestCambiarCondicionPagoFactura(unittest.TestCase):
    """PUT /api/facturas/<id>/condicion-pago: la corrección que Finanzas
    puede hacerle a una factura YA aplicada (típicamente por la nueva ruta
    de ingreso directo de Bodega, ver _crear_pendiente_y_aplicar_si_posible
    en app.py) cuando la condición de pago pactada con el proveedor
    termina siendo distinta a la que Bodega puso al ingresarla. Reemplaza
    la(s) CxP existente(s) por una(s) nueva(s) -- nunca toca stock ni las
    líneas de la factura."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin_client = taller_app.app.test_client()
        cls.admin_client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_usuario(self, username, rol):
        resp = self.admin_client.post(
            "/api/admin/usuarios",
            json={"username": username, "password": "clave123456", "nombre": username,
                  "es_admin": False, "modulos": [rol]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

    def _client_como(self, username):
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": "clave123456"})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    def _crear_factura_credito(self, numero, cantidad=1, precio_unitario=10000, plazo_dias_credito=30):
        # exento_iva=True: esta clase prueba qué pasa con las CxP al cambiar
        # la condición de pago, no el cálculo de Neto/IVA (ver
        # TestNetoIvaAutomaticoEnFacturas para esa cobertura) -- así los
        # montos esperados coinciden directo con precio_unitario * cantidad.
        producto = self.admin_client.post(
            "/api/productos", json={"nombre": f"Producto - {numero}", "stock_disponible": 0, "costo_compra": 0},
        ).get_json()
        resp = self.admin_client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_id": producto["id"], "cantidad": cantidad, "precio_unitario": precio_unitario}],
                "condicion_pago": "credito", "plazo_dias_credito": plazo_dias_credito,
                "exento_iva": True,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        pendiente = resp.get_json()
        # Con el producto recién creado (sin nombre repetido), la ruta de
        # ingreso directo la aplica de una -- debe quedar 'aprobada'.
        self.assertEqual(pendiente["estado"], "aprobada")
        return pendiente

    def _cxps_activas(self, factura_id):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            return [dict(r) for r in db.execute(
                "SELECT * FROM cuentas_por_pagar WHERE factura_id = ? AND estado != 'anulada' ORDER BY id",
                (factura_id,),
            ).fetchall()]

    def test_cambiar_a_contado_anula_la_cxp_de_credito_y_genera_una_pagada(self):
        pendiente = self._crear_factura_credito("FACT-CONDPAGO-001", precio_unitario=10000)
        factura_id = pendiente["factura_id"]
        cxp_original = self._cxps_activas(factura_id)
        self.assertEqual(len(cxp_original), 1)
        self.assertEqual(cxp_original[0]["estado"], "pendiente")

        resp = self.admin_client.put(
            f"/api/facturas/{factura_id}/condicion-pago",
            json={"condicion_pago": "contado"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        with taller_app.app.app_context():
            db = taller_app.get_db()
            anulada = db.execute(
                "SELECT * FROM cuentas_por_pagar WHERE id = ?", (cxp_original[0]["id"],)
            ).fetchone()
        self.assertEqual(anulada["estado"], "anulada")

        cxp_nueva = self._cxps_activas(factura_id)
        self.assertEqual(len(cxp_nueva), 1)
        self.assertEqual(cxp_nueva[0]["estado"], "pagada")
        self.assertEqual(cxp_nueva[0]["monto_total"], 10000)

    def test_cambiar_a_mixto_genera_dos_cxp(self):
        pendiente = self._crear_factura_credito("FACT-CONDPAGO-002", precio_unitario=10000)
        factura_id = pendiente["factura_id"]

        resp = self.admin_client.put(
            f"/api/facturas/{factura_id}/condicion-pago",
            json={"condicion_pago": "mixto", "porcentaje_contado": 40, "plazo_dias_credito": 45},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        cxps = self._cxps_activas(factura_id)
        self.assertEqual(len(cxps), 2)
        montos_por_estado = {c["estado"]: c["monto_total"] for c in cxps}
        self.assertEqual(montos_por_estado["pagada"], 4000)
        self.assertEqual(montos_por_estado["pendiente"], 6000)

    def test_cambiar_condicion_actualiza_el_pendiente_asociado(self):
        pendiente = self._crear_factura_credito("FACT-CONDPAGO-003", plazo_dias_credito=30)
        factura_id = pendiente["factura_id"]

        resp = self.admin_client.put(
            f"/api/facturas/{factura_id}/condicion-pago",
            json={"condicion_pago": "credito", "plazo_dias_credito": 90},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        actualizado = self.admin_client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        self.assertEqual(actualizado["plazo_dias_credito"], 90)
        eventos = [h["evento"] for h in actualizado["historial"]]
        self.assertIn("condicion_pago_editada", eventos)

    def test_cambiar_condicion_bloqueada_si_ya_tiene_abonos(self):
        pendiente = self._crear_factura_credito("FACT-CONDPAGO-004", precio_unitario=8000)
        factura_id = pendiente["factura_id"]
        cxp = self._cxps_activas(factura_id)[0]

        resp_pago = self.admin_client.post(
            f"/api/finanzas/cxp/{cxp['id']}/pagos", json={"monto": 2000, "metodo_pago": "efectivo"},
        )
        self.assertEqual(resp_pago.status_code, 201, resp_pago.get_data(as_text=True))

        resp = self.admin_client.put(
            f"/api/facturas/{factura_id}/condicion-pago",
            json={"condicion_pago": "contado"},
        )
        self.assertEqual(resp.status_code, 400)

        # No debe haber tocado nada: la CxP original sigue activa, tal cual.
        cxps = self._cxps_activas(factura_id)
        self.assertEqual(len(cxps), 1)
        self.assertEqual(cxps[0]["id"], cxp["id"])

    def test_cambiar_condicion_pago_es_solo_para_finanzas_o_admin(self):
        pendiente = self._crear_factura_credito("FACT-CONDPAGO-005")
        factura_id = pendiente["factura_id"]

        self._crear_usuario("condpago_bodega1", "bodega")
        bodega_client = self._client_como("condpago_bodega1")
        resp_bodega = bodega_client.put(
            f"/api/facturas/{factura_id}/condicion-pago", json={"condicion_pago": "contado"},
        )
        self.assertEqual(resp_bodega.status_code, 403)

        self._crear_usuario("condpago_finanzas1", "finanzas")
        finanzas_client = self._client_como("condpago_finanzas1")
        resp_finanzas = finanzas_client.put(
            f"/api/facturas/{factura_id}/condicion-pago", json={"condicion_pago": "contado"},
        )
        self.assertEqual(resp_finanzas.status_code, 200, resp_finanzas.get_data(as_text=True))

    def test_cambiar_condicion_pago_con_valor_invalido_es_rechazada(self):
        pendiente = self._crear_factura_credito("FACT-CONDPAGO-006")
        factura_id = pendiente["factura_id"]
        resp = self.admin_client.put(
            f"/api/facturas/{factura_id}/condicion-pago", json={"condicion_pago": "no_existe"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_cambiar_condicion_pago_de_factura_inexistente_da_404(self):
        resp = self.admin_client.put(
            "/api/facturas/9999999/condicion-pago", json={"condicion_pago": "contado"},
        )
        self.assertEqual(resp.status_code, 404)


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
            json={
                "username": username, "password": password, "nombre": username,
                "es_admin": rol == "admin",
                "modulos": [] if rol == "admin" else [rol],
            },
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
        # Con el producto ya en el catálogo, la ruta de ingreso directo
        # (ver _crear_pendiente_y_aplicar_si_posible en app.py) ya la deja
        # aplicada al crearla -- no hace falta (ni se puede) aprobarla de
        # nuevo a mano.
        if pendiente["estado"] == "aprobada":
            factura = self.admin_client.get(f"/api/facturas/{pendiente['factura_id']}").get_json()
        else:
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

    def _crear_pendiente_forzado_lineas(self, numero, lineas_validadas, condicion_pago="credito",
                                          porcentaje_contado=0, plazo_dias_credito=30, exento_iva=True):
        """Como _crear_pendiente, pero llama directo a la función de más
        bajo nivel (_crear_pendiente_recepcion) en vez de pasar por el
        endpoint HTTP -- así se salta la ruta de ingreso directo nueva
        (ver _crear_pendiente_y_aplicar_si_posible en app.py), que con
        todas las líneas ya resueltas contra el catálogo aplicaría la
        factura de inmediato. Para los tests de esta clase que quieren
        probar el mecanismo de aprobar/rechazar EN SÍ (no la nueva capa de
        auto-aplicación), hace falta poder dejar la factura genuinamente
        en estado 'pendiente'. Recibe las líneas ya armadas (mismo formato
        que espera _crear_pendiente_recepcion) para poder cubrir tanto
        líneas de producto como de gasto general.

        exento_iva=True por defecto: esta clase prueba el mecanismo de
        aprobar/rechazar/CxP, no el cálculo de Neto/IVA en sí (ver
        TestNetoIvaAutomaticoEnFacturas para esa cobertura) -- así los
        montos esperados coinciden directo con precio_unitario * cantidad."""
        with taller_app.app.app_context():
            db = taller_app.get_db()
            nueva, error = taller_app._crear_pendiente_recepcion(
                db, numero, None, datetime.now().strftime("%Y-%m-%d"),
                lineas_validadas, condicion_pago, porcentaje_contado, plazo_dias_credito, "test", origen="manual",
                exento_iva=exento_iva,
            )
            self.assertIsNone(error, error)
            return taller_app._factura_pendiente_recepcion_dict(db, nueva)

    def _crear_pendiente_forzado(self, numero, producto_id, cantidad=5, precio_unitario=1000, **extra):
        return self._crear_pendiente_forzado_lineas(
            numero,
            [{"producto_id": producto_id, "producto_nombre": None, "cantidad": cantidad, "precio_unitario": precio_unitario}],
            **extra,
        )

    def test_crear_pendiente_con_producto_existente_se_aplica_de_inmediato(self):
        # Ruta de ingreso directo (ver _crear_pendiente_y_aplicar_si_posible
        # en app.py): con el producto ya en el catálogo, no hace falta un
        # segundo paso de aprobación aparte -- la factura queda aplicada
        # (stock + CxP) en la misma llamada que la crea.
        producto = self._crear_producto_http("Producto - pendiente se aplica directo", stock_disponible=0)
        creada = self._crear_pendiente("FACT-PEND-001", producto["id"], cantidad=10, precio_unitario=1000)
        self.assertEqual(creada["estado"], "aprobada")
        self.assertIsNotNone(creada["factura_id"])

        aplicado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(aplicado["stock_disponible"], 10)

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT id FROM facturas WHERE numero = ?", ("FACT-PEND-001",)).fetchone()
            cxp = db.execute(
                "SELECT COUNT(*) FROM cuentas_por_pagar cxp JOIN facturas f ON f.id = cxp.factura_id WHERE f.numero = ?",
                ("FACT-PEND-001",),
            ).fetchone()[0]
        self.assertIsNotNone(factura)
        self.assertEqual(cxp, 1)

    def test_crear_pendiente_con_producto_nuevo_no_toca_stock_ni_genera_cxp_hasta_resolverlo(self):
        # Si trae una línea sin resolver (repuesto que todavía no existe en
        # el catálogo), NO se puede aplicar todavía -- queda 'pendiente'
        # exactamente como el sistema se comportaba antes de la ruta de
        # ingreso directo, a la espera de que Bodega cree el producto y
        # apruebe a mano (ver TestProductoNuevoDesdeFacturaPendiente).
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-001B",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_nuevo_nombre": "Repuesto sin crear todavía", "cantidad": 10, "precio_unitario": 1000}],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        creada = resp.get_json()
        self.assertEqual(creada["estado"], "pendiente")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT id FROM facturas WHERE numero = ?", ("FACT-PEND-001B",)).fetchone()
            cxp = db.execute(
                "SELECT COUNT(*) FROM cuentas_por_pagar cxp JOIN facturas f ON f.id = cxp.factura_id WHERE f.numero = ?",
                ("FACT-PEND-001B",),
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
        pendiente = self._crear_pendiente_forzado(
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
        pendiente = self._crear_pendiente_forzado(
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
        pendiente = self._crear_pendiente_forzado(
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

    def test_aprobar_ignora_lineas_recibidas_y_siempre_aplica_lo_facturado(self):
        # Corregido a pedido del taller: quien aprueba NO puede modificar
        # la cantidad recibida -- si antes se mandaba un 'lineas_recibidas'
        # con una cantidad distinta a la facturada (ej. para reflejar que
        # llegó menos de lo pactado), el sistema simplemente lo ignora y
        # aplica la factura tal cual quedó cargada. Si algo no calza, el
        # camino es rechazar la factura, no "ajustarla" al aprobar.
        producto = self._crear_producto_http("Producto - aprobar ignora ajuste", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado(
            "FACT-PEND-IGNORA-001", producto["id"], cantidad=10, precio_unitario=1000, condicion_pago="credito",
        )
        resp = self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar",
            json={"lineas_recibidas": [{"producto_id": producto["id"], "cantidad_recibida": 6}]},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 10)

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-IGNORA-001",)).fetchone()
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
        self.assertEqual(cxp["monto_total"], 10000)

    def test_aprobar_sin_cuerpo_aplica_todas_las_lineas_facturadas(self):
        producto = self._crear_producto_http("Producto - aprobar sin cuerpo", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado("FACT-PEND-SINCUERPO-001", producto["id"], cantidad=5, precio_unitario=1000)
        resp = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 5)

    def test_rechazar_no_aplica_stock_ni_crea_factura(self):
        producto = self._crear_producto_http("Producto - pendiente rechazada", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado("FACT-PEND-RECH-001", producto["id"], cantidad=5, precio_unitario=1000)

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

        p1 = self._crear_pendiente_forzado("FACT-PEND-CONTADOR-001", producto["id"])
        self._crear_pendiente_forzado("FACT-PEND-CONTADOR-002", producto["id"])
        self.client.post(f"/api/facturas/pendientes-recepcion/{p1['id']}/aprobar", json={})

        despues = self.client.get("/api/facturas/pendientes-recepcion/contador").get_json()["pendientes"]
        # Se crearon 2, se aprobó 1 -> debe quedar 1 más que "antes".
        self.assertEqual(despues, antes + 1)

    # ---- Corregir y reingresar una factura rechazada (a pedido del taller:
    # antes, una vez rechazada, quedaba trabada -- no había forma de
    # corregirla sin recrearla de cero; y ni Finanzas se enteraba de que
    # había sido rechazada más que entrando a mirar la lista a mano) ----

    _contador_usuarios_fpr = 0

    def _crear_usuario_y_login(self, modulos):
        type(self)._contador_usuarios_fpr += 1
        username = f"fpr_{type(self)._contador_usuarios_fpr}"
        resp = self.client.post(
            "/api/admin/usuarios",
            json={"username": username, "password": "clave123456", "nombre": username,
                  "es_admin": False, "modulos": modulos},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cliente = taller_app.app.test_client()
        resp_login = cliente.post("/login", data={"username": username, "password": "clave123456"})
        self.assertEqual(resp_login.status_code, 302)
        return cliente

    def test_no_se_podia_editar_una_rechazada_ahora_si_y_vuelve_a_pendiente(self):
        producto = self._crear_producto_http("Producto - corregir rechazada", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado("FACT-PEND-CORRIGE-001", producto["id"], cantidad=2, precio_unitario=5000)
        self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/rechazar",
            json={"nota": "El precio no calza con lo pactado"},
        )
        rechazada = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        self.assertEqual(rechazada["estado"], "rechazada")

        # La corrección: mismo número, misma factura, solo se ajusta el
        # precio -- no hace falta volver a escribir todo de cero.
        resp_editar = self.client.put(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}",
            json={"lineas": [{"producto_id": producto["id"], "cantidad": 2, "precio_unitario": 4500}]},
        )
        self.assertEqual(resp_editar.status_code, 200, resp_editar.get_data(as_text=True))
        corregida = resp_editar.get_json()
        self.assertEqual(corregida["estado"], "pendiente")
        self.assertEqual(corregida["lineas"][0]["precio_unitario"], 4500)

        # Y ahora que volvió a 'pendiente', se puede aprobar normalmente.
        resp_aprobar = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp_aprobar.status_code, 201, resp_aprobar.get_data(as_text=True))
        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 2)
        self.assertEqual(actualizado["costo_compra"], 4500)

    def test_una_factura_aprobada_sigue_sin_poder_editarse(self):
        # Regresión: el fix de "editar una rechazada" no debe abrir la
        # puerta a editar una que YA fue aprobada -- eso sigue prohibido
        # (aprobar aplica stock/CxP de inmediato; "corregirla" después
        # dejaría esos montos ya aplicados desincronizados de la factura).
        producto = self._crear_producto_http("Producto - no editar aprobada", stock_disponible=0)
        pendiente = self._crear_pendiente("FACT-PEND-NOEDITAR-001", producto["id"])
        self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        resp = self.client.put(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}",
            json={"lineas": [{"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 1}]},
        )
        self.assertEqual(resp.status_code, 400)

    def test_historial_registra_todo_el_ciclo_sin_perder_el_rechazo_previo(self):
        producto = self._crear_producto_http("Producto - historial completo", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado("FACT-PEND-HIST-001", producto["id"], cantidad=1, precio_unitario=1000)

        creada = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        self.assertEqual([h["evento"] for h in creada["historial"]], ["creada"])

        self.client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/rechazar",
            json={"nota": "Cantidad no calza"},
        )
        self.client.put(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}",
            json={"lineas": [{"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 1000}]},
        )
        self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})

        final = self.client.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        eventos = [h["evento"] for h in final["historial"]]
        self.assertEqual(eventos, ["creada", "rechazada", "corregida_reingresada", "aprobada"])
        # La nota del rechazo original sigue disponible en el historial,
        # aunque el estado actual ya sea 'aprobada' y nota_rechazo (el
        # campo suelto, no el historial) ya no se muestre en el detalle.
        evento_rechazo = next(h for h in final["historial"] if h["evento"] == "rechazada")
        self.assertEqual(evento_rechazo["nota"], "Cantidad no calza")

    def test_contador_de_rechazadas_es_solo_para_finanzas_no_para_bodega(self):
        cliente_bodega = self._crear_usuario_y_login(["bodega"])
        resp_bodega = cliente_bodega.get("/api/facturas/pendientes-recepcion/rechazadas/contador")
        self.assertEqual(resp_bodega.status_code, 403)

        cliente_finanzas = self._crear_usuario_y_login(["finanzas"])
        resp_finanzas = cliente_finanzas.get("/api/facturas/pendientes-recepcion/rechazadas/contador")
        self.assertEqual(resp_finanzas.status_code, 200)

    def test_contador_de_rechazadas_baja_al_corregir_y_reingresar(self):
        producto = self._crear_producto_http("Producto - contador rechazadas", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado("FACT-PEND-CONTADOR-RECH-001", producto["id"])
        antes = self.client.get("/api/facturas/pendientes-recepcion/rechazadas/contador").get_json()["rechazadas"]

        self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/rechazar", json={"nota": "x"})
        despues_rechazo = self.client.get("/api/facturas/pendientes-recepcion/rechazadas/contador").get_json()["rechazadas"]
        self.assertEqual(despues_rechazo, antes + 1)

        self.client.put(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}",
            json={"lineas": [{"producto_id": producto["id"], "cantidad": 5, "precio_unitario": 1000}]},
        )
        despues_correccion = self.client.get("/api/facturas/pendientes-recepcion/rechazadas/contador").get_json()["rechazadas"]
        self.assertEqual(despues_correccion, antes)

    # ---- Líneas de "gasto general" (ítems de una factura que NO son
    # repuestos -- combustible, herramientas -- y a propósito no deben
    # tocar el inventario ni el catálogo de productos, a pedido del
    # taller). Ver factura_gastos_generales / es_gasto_general.

    def _linea_gasto_general(self, descripcion="Bencina generador", monto=15000, rubro_gav="combustible"):
        return {
            "es_gasto_general": True, "descripcion": descripcion, "monto": monto, "rubro_gav": rubro_gav,
        }

    def test_linea_gasto_general_no_crea_producto_ni_afecta_stock(self):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-GG-001",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [self._linea_gasto_general()],
                "condicion_pago": "credito",
                "exento_iva": True,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        pendiente = resp.get_json()

        antes = self.client.get("/api/productos").get_json()["productos"]
        # Una factura de puro gasto general (sin ninguna línea de producto)
        # no tiene nada que le impida aplicarse de inmediato con la nueva
        # ruta de ingreso directo (ver _crear_pendiente_y_aplicar_si_posible
        # en app.py) -- ya queda 'aprobada' desde la creación.
        self.assertEqual(pendiente["estado"], "aprobada")
        despues = self.client.get("/api/productos").get_json()["productos"]
        self.assertEqual(len(antes), len(despues), "no debe crearse ningún producto nuevo")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-GG-001",)).fetchone()
            self.assertIsNotNone(factura)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM factura_lineas WHERE factura_id = ?", (factura["id"],)).fetchone()[0],
                0,
            )
            gasto = db.execute(
                "SELECT * FROM factura_gastos_generales WHERE factura_id = ?", (factura["id"],)
            ).fetchone()
            self.assertEqual(gasto["descripcion"], "Bencina generador")
            self.assertEqual(gasto["monto"], 15000)
            self.assertEqual(gasto["rubro_gav"], "combustible")
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
            self.assertEqual(cxp["monto_total"], 15000, "la CxP debe incluir el gasto general aunque no sea repuesto")

    def test_pendiente_con_gasto_general_trae_rubro_gav_label_para_el_aviso_flotante(self):
        """El aviso flotante global (alerta_facturas_pendientes.js) decide
        si bloquea 'Aprobar' mirando si alguna línea tiene producto_id nulo
        -- una línea de gasto general también lo tiene a propósito, así
        que ese script necesita poder distinguirla por 'es_gasto_general'
        (no debe tratarla como "producto sin resolver"). También necesita
        'rubro_gav_label' ya resuelto en la respuesta, porque ese script
        deliberadamente no comparte el mapeo de rubros con finanzas.js."""
        # Se fuerza a quedar 'pendiente' (ver _crear_pendiente_forzado_lineas)
        # porque, con solo un gasto general, la ruta de ingreso directo la
        # aplicaría de inmediato (ver _crear_pendiente_y_aplicar_si_posible
        # en app.py) y ya no aparecería en el listado "?estado=pendiente"
        # que este test necesita revisar.
        pendiente_id = self._crear_pendiente_forzado_lineas(
            "FACT-PEND-GG-AVISO-001",
            [{
                "es_gasto_general": True, "producto_id": None, "producto_nombre": None,
                "descripcion": "Aceite para el compresor", "monto": 15000, "rubro_gav": "mantencion",
            }],
        )["id"]

        listado = self.client.get("/api/facturas/pendientes-recepcion?estado=pendiente").get_json()
        pendiente = next(p for p in listado["pendientes"] if p["id"] == pendiente_id)
        linea = pendiente["lineas"][0]
        self.assertTrue(linea["es_gasto_general"])
        self.assertIsNone(linea["producto_id"])
        self.assertEqual(linea["rubro_gav_label"], "Mantención y equipos")

    def test_factura_hibrida_repuesto_y_gasto_general_juntos(self):
        """Ej. del taller: un filtro (va a inventario/OT) y la herramienta
        para sacarlo (no va a inventario) llegan en la MISMA factura."""
        producto = self._crear_producto_http("Filtro de aceite - factura híbrida", stock_disponible=0)
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-HIBRIDA-001",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [
                    {"producto_id": producto["id"], "cantidad": 3, "precio_unitario": 4000},
                    self._linea_gasto_general(descripcion="Extractor de filtros", monto=8000, rubro_gav="mantencion"),
                ],
                "condicion_pago": "credito",
                "exento_iva": True,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        pendiente = resp.get_json()

        # El producto de la única línea de repuesto ya existe en el
        # catálogo, así que la ruta de ingreso directo la aplica de
        # inmediato (ver _crear_pendiente_y_aplicar_si_posible en app.py) --
        # no hace falta (ni se puede) aprobarla aparte.
        self.assertEqual(pendiente["estado"], "aprobada")

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 3, "el repuesto sí debe ingresar a stock")

        with taller_app.app.app_context():
            db = taller_app.get_db()
            factura = db.execute("SELECT * FROM facturas WHERE numero = ?", ("FACT-PEND-HIBRIDA-001",)).fetchone()
            n_lineas_producto = db.execute(
                "SELECT COUNT(*) FROM factura_lineas WHERE factura_id = ?", (factura["id"],)
            ).fetchone()[0]
            n_gastos = db.execute(
                "SELECT COUNT(*) FROM factura_gastos_generales WHERE factura_id = ?", (factura["id"],)
            ).fetchone()[0]
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
        self.assertEqual(n_lineas_producto, 1)
        self.assertEqual(n_gastos, 1)
        self.assertEqual(cxp["monto_total"], 12000 + 8000)  # 3 x 4000 + 8000

        detalle = self.client.get(f"/api/facturas/{factura['id']}").get_json()
        self.assertEqual(len(detalle["lineas"]), 1)
        self.assertEqual(len(detalle["gastos_generales"]), 1)
        self.assertEqual(detalle["gastos_generales"][0]["descripcion"], "Extractor de filtros")

    def test_linea_gasto_general_sin_descripcion_es_rechazada(self):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-GG-SIN-DESC",
                "lineas": [self._linea_gasto_general(descripcion="")],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_linea_gasto_general_con_rubro_invalido_es_rechazada(self):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-GG-RUBRO-MALO",
                "lineas": [self._linea_gasto_general(rubro_gav="no_existe")],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_linea_gasto_general_sin_rubro_es_rechazada(self):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-GG-SIN-RUBRO",
                "lineas": [self._linea_gasto_general(rubro_gav="")],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_linea_gasto_general_con_gastos_por_ot_es_rechazada(self):
        # 'gastos_por_ot' es un rubro válido del desplegable, pero es
        # exclusivo de Caja Chica (liga un gasto a una orden puntual) --
        # no tiene sentido para un ítem de factura sin OT asociada.
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PEND-GG-OT",
                "lineas": [self._linea_gasto_general(rubro_gav="gastos_por_ot")],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 400)

    def test_bodega_puede_convertir_una_linea_de_producto_en_gasto_general_al_editar(self):
        """Flujo pedido por el taller: Finanzas marca (o no) el gasto
        general al cargar la factura, y Bodega confirma o desconfirma esa
        marca al revisar el pendiente antes de aprobarlo -- ambos roles
        pueden editar el mismo pendiente (PUT), así que Bodega puede
        corregir la clasificación sin tener que rechazar toda la factura."""
        producto = self._crear_producto_http("Producto - bodega reclasifica", stock_disponible=0)
        pendiente = self._crear_pendiente_forzado("FACT-PEND-GG-RECLASIFICA", producto["id"], cantidad=1, precio_unitario=9000)

        cliente_bodega = self._crear_usuario_y_login(["bodega"])
        resp = cliente_bodega.put(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}",
            json={"lineas": [self._linea_gasto_general(descripcion="En realidad es una herramienta", monto=9000, rubro_gav="mantencion")]},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        aprobar = cliente_bodega.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(aprobar.status_code, 201, aprobar.get_data(as_text=True))

        actualizado = self.client.get(f"/api/productos/{producto['id']}").get_json()
        self.assertEqual(actualizado["stock_disponible"], 0, "ya no debe aplicarse como stock, se reclasificó")


class TestNetoIvaAutomaticoEnFacturas(unittest.TestCase):
    """Cobertura dedicada de la funcionalidad "Neto/IVA automático" (el
    precio de cada línea de una factura de compra es el NETO; el sistema
    calcula el IVA (19%) y el total solo, salvo que la factura se marque
    'exenta'). Esta funcionalidad se entregó sin pruebas propias -- lo que
    dejó pasar, sin que nadie lo notara, que 12 pruebas más antiguas de CxP
    quedaran calculando mal su monto esperado (no consideraban el IVA que
    ahora se agrega solo). Esta clase existe para que el cálculo en sí
    quede probado de una vez, en el mismo lugar donde vive la lógica."""

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

    def _factura_de(self, numero):
        with taller_app.app.app_context():
            db = taller_app.get_db()
            return dict(db.execute("SELECT * FROM facturas WHERE numero = ?", (numero,)).fetchone())

    def test_factura_normal_calcula_iva_19_por_ciento_sobre_el_neto(self):
        producto = self._crear_producto_http("Producto - IVA normal", stock_disponible=0)
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-IVA-001",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_id": producto["id"], "cantidad": 2, "precio_unitario": 10000}],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        factura = self._factura_de("FACT-IVA-001")
        self.assertEqual(factura["monto_neto"], 20000)
        self.assertEqual(factura["monto_iva"], 3800)  # 20000 * 0.19
        self.assertEqual(factura["monto_total"], 23800)
        self.assertEqual(factura["exento_iva"], 0)

        # La CxP que se genera junto con la factura debe coincidir exacto
        # con ese mismo monto_total -- lo que de verdad se le debe al
        # proveedor incluye el IVA, no solo el neto.
        with taller_app.app.app_context():
            db = taller_app.get_db()
            cxp = db.execute("SELECT * FROM cuentas_por_pagar WHERE factura_id = ?", (factura["id"],)).fetchone()
        self.assertEqual(cxp["monto_total"], 23800)

    def test_factura_exenta_no_agrega_iva(self):
        producto = self._crear_producto_http("Producto - IVA exento", stock_disponible=0)
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-IVA-002",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_id": producto["id"], "cantidad": 3, "precio_unitario": 5000}],
                "condicion_pago": "credito",
                "exento_iva": True,
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        factura = self._factura_de("FACT-IVA-002")
        self.assertEqual(factura["monto_neto"], 15000)
        self.assertEqual(factura["monto_iva"], 0)
        self.assertEqual(factura["monto_total"], 15000)
        self.assertEqual(factura["exento_iva"], 1)

    def test_gasto_general_tambien_entra_al_calculo_de_iva(self):
        # El monto de una línea de gasto general (ej. combustible) se suma
        # al neto igual que cualquier repuesto, así que también le calcula
        # su parte del IVA -- no queda "afuera" del cálculo solo por no ir
        # a inventario.
        producto = self._crear_producto_http("Producto - IVA hibrida", stock_disponible=0)
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-IVA-003",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [
                    {"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 6000},
                    {"es_gasto_general": True, "descripcion": "Bencina", "monto": 4000, "rubro_gav": "combustible"},
                ],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        factura = self._factura_de("FACT-IVA-003")
        self.assertEqual(factura["monto_neto"], 10000)
        self.assertEqual(factura["monto_iva"], 1900)
        self.assertEqual(factura["monto_total"], 11900)


class TestProductoNuevoDesdeFacturaPendiente(unittest.TestCase):
    """Hallazgo planteado por el taller: una factura puede traer un
    repuesto que todavía no existe en el catálogo. Antes, los cuatro
    caminos para registrar una factura (manual, Excel, foto, WhatsApp)
    exigían que el producto ya existiera -- si no, la línea (o la factura
    completa, según el camino) quedaba bloqueada, sin ninguna forma de
    avanzar. Ahora todos dejan pasar la línea igual, sin resolver, y la
    factura queda como pendiente de recepción -- pero como Finanzas no
    puede crear productos (hallazgo #3 de la revisión técnica), alguien de
    Bodega tiene que crear el producto que falta y vincularlo a esa línea
    (ver crear_y_vincular_producto_pendiente) ANTES de poder aprobar la
    factura -- aprobar_factura_pendiente_recepcion sigue exigiendo
    producto_id real en todas las líneas, eso no cambió."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin = taller_app.app.test_client()
        cls.admin.post("/login", data={"username": "admin", "password": "taller2026"})
        cls._contador = 0

    def _nuevo_username(self, prefijo):
        type(self)._contador += 1
        return f"{prefijo}_{type(self)._contador}"

    def _crear_usuario_y_login(self, modulos):
        username = self._nuevo_username("pnf")
        resp = self.admin.post(
            "/api/admin/usuarios",
            json={"username": username, "password": "clave123456", "nombre": username,
                  "es_admin": False, "modulos": modulos},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        client = taller_app.app.test_client()
        resp_login = client.post("/login", data={"username": username, "password": "clave123456"})
        self.assertEqual(resp_login.status_code, 302)
        return client

    def _crear_pendiente_con_producto_nuevo(self, numero, nombre_nuevo="Kit de embrague ABC 2020",
                                              cantidad=3, precio_unitario=15000):
        resp = self.admin.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"producto_nuevo_nombre": nombre_nuevo, "cantidad": cantidad, "precio_unitario": precio_unitario}],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    # ---- Entrada manual (Finanzas) puede describir un producto nuevo ----

    def test_finanzas_puede_registrar_factura_con_producto_que_no_existe_todavia(self):
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-001")
        linea = pendiente["lineas"][0]
        self.assertIsNone(linea["producto_id"])
        self.assertEqual(linea["texto_original"], "Kit de embrague ABC 2020")
        self.assertEqual(linea["cantidad"], 3)
        self.assertEqual(pendiente["estado"], "pendiente")

    def test_linea_manual_sin_producto_id_ni_nombre_nuevo_se_rechaza(self):
        resp = self.admin.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PROD-NUEVO-002",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [{"cantidad": 1, "precio_unitario": 1000}],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("describe el producto nuevo", resp.get_json()["error"])

    # ---- No se puede aprobar hasta que Bodega resuelva la línea ----

    def test_no_se_puede_aprobar_con_una_linea_sin_producto_vinculado(self):
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-003")
        resp = self.admin.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("todavía no está asociada a un producto", resp.get_json()["error"])

    # ---- Crear y vincular: quién puede, y qué pasa cuando ----

    def test_finanzas_y_bodega_pueden_crear_y_vincular_el_producto_pero_no_un_modulo_ajeno(self):
        # El taller pasó a operar con una sola persona a cargo de Bodega y
        # Facturas -- separar el permiso entre esos dos módulos ya no
        # aporta nada, así que ambos pueden crear el producto acá (ver
        # docstring de crear_y_vincular_producto_pendiente en app.py). Sigue
        # bloqueado para alguien sin ninguno de los dos módulos.
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-PERM-01")
        client_finanzas = self._crear_usuario_y_login(["finanzas"])
        resp = client_finanzas.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/0/crear-producto",
            json={"categoria": "EMBRAGUE", "modelo": "ABC 2020", "marca": "GENERICA"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        pendiente2 = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-PERM-02")
        client_bodega = self._crear_usuario_y_login(["bodega"])
        resp2 = client_bodega.post(
            f"/api/facturas/pendientes-recepcion/{pendiente2['id']}/lineas/0/crear-producto",
            json={"categoria": "EMBRAGUE", "modelo": "ABC 2020 B", "marca": "GENERICA"},
        )
        self.assertEqual(resp2.status_code, 201, resp2.get_data(as_text=True))

        pendiente3 = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-PERM-03")
        client_ajeno = self._crear_usuario_y_login(["operativo"])
        resp3 = client_ajeno.post(
            f"/api/facturas/pendientes-recepcion/{pendiente3['id']}/lineas/0/crear-producto",
            json={"categoria": "EMBRAGUE", "modelo": "ABC 2020 C", "marca": "GENERICA"},
        )
        self.assertEqual(resp3.status_code, 403)

    def test_crear_y_vincular_producto_deja_la_linea_resuelta_y_permite_aprobar(self):
        pendiente = self._crear_pendiente_con_producto_nuevo(
            "FACT-PROD-NUEVO-005", nombre_nuevo="Bomba de agua XYZ", cantidad=2, precio_unitario=45000,
        )
        resp = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/0/crear-producto",
            json={"categoria": "BOMBA DE AGUA", "modelo": "XYZ", "marca": "BOSCH"},
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cuerpo = resp.get_json()
        producto_nuevo = cuerpo["producto"]
        pendiente_actualizado = cuerpo["pendiente"]

        # El producto nace SIN stock ni costo -- eso se aplica recién al
        # aprobar la factura, para no contar la compra dos veces (mismo
        # principio ya corregido esta sesión para las devoluciones de stock).
        self.assertEqual(producto_nuevo["stock_disponible"], 0)
        self.assertEqual(producto_nuevo["costo_compra"], 0)

        linea_resuelta = pendiente_actualizado["lineas"][0]
        self.assertEqual(linea_resuelta["producto_id"], producto_nuevo["id"])
        self.assertEqual(linea_resuelta["producto_nombre"], producto_nuevo["nombre"])

        # Ahora sí se puede aprobar, y el stock/costo se aplican tal como
        # venían en la factura (ni más, ni menos -- ver test siguiente para
        # la verificación numérica exacta).
        resp_aprobar = self.admin.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(resp_aprobar.status_code, 201, resp_aprobar.get_data(as_text=True))

        producto_final = self.admin.get(f"/api/productos/{producto_nuevo['id']}").get_json()
        self.assertEqual(producto_final["stock_disponible"], 2)
        self.assertEqual(producto_final["costo_compra"], 45000)

    def test_crear_y_vincular_rechaza_sin_marca_si_hay_categoria_o_modelo(self):
        # La marca solía ser opcional acá: si Bodega la dejaba en blanco al
        # crear el producto desde una factura, ese repuesto quedaba para
        # siempre como una entrada "genérica sin marca" -- cualquier factura
        # futura de ese mismo repuesto, sin importar su marca real, se
        # vinculaba a esa misma fila sin volver a preguntar, mezclando
        # precios y perdiendo el seguimiento de garantía por marca (reporte
        # del taller). Ahora se exige la marca en cuanto se define un modelo
        # y/o detalle real.
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-004B")
        resp = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/0/crear-producto",
            json={"categoria": "EMBRAGUE", "modelo": "ABC 2020"},
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("marca", resp.get_json()["error"].lower())

        # La línea sigue sin vincular -- no se creó ningún producto a medias.
        pendiente_actual = self.admin.get(f"/api/facturas/pendientes-recepcion/{pendiente['id']}").get_json()
        self.assertIsNone(pendiente_actual["lineas"][0]["producto_id"])

    def test_dos_marcas_del_mismo_modelo_y_detalle_quedan_como_productos_separados(self):
        # Núcleo de la funcionalidad "marca por repuesto": la misma pieza en
        # dos marcas (ej. una versión genérica y una BOSCH) debe quedar como
        # dos productos con su propio stock/costo, y una tercera factura con
        # exactamente la misma combinación categoría+modelo+marca debe
        # rechazarse como duplicado exacto.
        pendiente_1 = self._crear_pendiente_con_producto_nuevo("FACT-PROD-MARCA-001")
        resp_1 = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente_1['id']}/lineas/0/crear-producto",
            json={"categoria": "FILTRO", "modelo": "ABC-100", "marca": "GENERICA"},
        )
        self.assertEqual(resp_1.status_code, 201, resp_1.get_data(as_text=True))
        producto_generico = resp_1.get_json()["producto"]

        pendiente_2 = self._crear_pendiente_con_producto_nuevo("FACT-PROD-MARCA-002")
        resp_2 = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente_2['id']}/lineas/0/crear-producto",
            json={"categoria": "FILTRO", "modelo": "ABC-100", "marca": "BOSCH"},
        )
        self.assertEqual(resp_2.status_code, 201, resp_2.get_data(as_text=True))
        producto_bosch = resp_2.get_json()["producto"]

        self.assertNotEqual(producto_generico["id"], producto_bosch["id"])
        self.assertIn("GENERICA", producto_generico["nombre"])
        self.assertIn("BOSCH", producto_bosch["nombre"])

        # Repetir exactamente la misma marca para el mismo modelo/detalle sí
        # se rechaza como duplicado.
        pendiente_3 = self._crear_pendiente_con_producto_nuevo("FACT-PROD-MARCA-003")
        resp_3 = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente_3['id']}/lineas/0/crear-producto",
            json={"categoria": "FILTRO", "modelo": "ABC-100", "marca": "BOSCH"},
        )
        self.assertEqual(resp_3.status_code, 400, resp_3.get_data(as_text=True))
        self.assertIn("ya existe", resp_3.get_json()["error"].lower())

    def test_editar_producto_no_permite_dejar_la_marca_en_blanco(self):
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-MARCA-004")
        resp = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/0/crear-producto",
            json={"categoria": "TURBO", "modelo": "T200", "marca": "GARRETT"},
        )
        producto = resp.get_json()["producto"]
        resp_editar = self.admin.put(f"/api/productos/{producto['id']}", json={"marca": ""})
        self.assertEqual(resp_editar.status_code, 400, resp_editar.get_data(as_text=True))
        self.assertIn("marca", resp_editar.get_json()["error"].lower())

    def test_crear_producto_directo_en_logistica_tambien_exige_marca(self):
        # Misma regla, camino directo (Logística "Nuevo producto", no desde
        # una factura): _crear_producto_desde_datos es compartida por
        # ambos, así que no debería poder crearse un producto con modelo
        # y/o detalle pero sin marca por ninguna de las dos puertas.
        resp = self.admin.post(
            "/api/productos", json={"categoria": "AMORTIGUADOR", "modelo": "M900"},
        )
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("marca", resp.get_json()["error"].lower())

        resp_ok = self.admin.post(
            "/api/productos", json={"categoria": "AMORTIGUADOR", "modelo": "M900", "marca": "MONROE"},
        )
        self.assertEqual(resp_ok.status_code, 201, resp_ok.get_data(as_text=True))

    def test_crear_y_vincular_rechaza_indice_invalido(self):
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-006")
        resp = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/5/crear-producto",
            json={"categoria": "X", "modelo": "Y"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_crear_y_vincular_rechaza_si_la_linea_ya_tiene_producto(self):
        producto = self.admin.post(
            "/api/productos", json={"nombre": "Producto ya vinculado", "stock_disponible": 0, "costo_compra": 500},
        ).get_json()
        resp_pend = self.admin.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-PROD-NUEVO-007",
                "fecha": datetime.now().strftime("%Y-%m-%d"),
                "lineas": [
                    {"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 500},
                    # Segunda línea sin resolver, a propósito, para que la
                    # factura NO se aplique de inmediato (ver
                    # _crear_pendiente_y_aplicar_si_posible en app.py) y se
                    # pueda probar el rechazo de "línea ya vinculada" sobre
                    # una factura todavía en estado 'pendiente'.
                    {"producto_nuevo_nombre": "Otro repuesto sin resolver", "cantidad": 1, "precio_unitario": 100},
                ],
                "condicion_pago": "credito",
            },
        )
        pendiente = resp_pend.get_json()
        self.assertEqual(pendiente["estado"], "pendiente")
        resp = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/0/crear-producto",
            json={"categoria": "X", "modelo": "Y"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ya está vinculada", resp.get_json()["error"])

    def test_crear_y_vincular_rechaza_si_la_factura_ya_fue_procesada(self):
        producto = self.admin.post(
            "/api/productos", json={"nombre": "Producto para rechazo procesada", "stock_disponible": 0, "costo_compra": 500},
        ).get_json()
        pendiente = self._crear_pendiente_con_producto_nuevo("FACT-PROD-NUEVO-008")
        # Se rechaza la factura completa -- ya no debería poder tocarse.
        self.admin.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/rechazar", json={})
        resp = self.admin.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/0/crear-producto",
            json={"categoria": "X", "modelo": "Y"},
        )
        self.assertEqual(resp.status_code, 400)

    # ---- Excel: una fila sin match ya no bloquea toda la factura ----

    def test_excel_con_producto_sin_match_queda_pendiente_en_vez_de_omitirse(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["N° Factura", "Fecha", "Proveedor", "RUT Proveedor", "ID Producto",
                   "Categoría", "Modelo", "Cantidad", "Precio Unitario"])
        ws.append(["FACT-EXCEL-NUEVO-001", "2026-07-08", "Proveedor Excel Nuevo", "",
                   "", "AMORTIGUADOR", "TEST-EXCEL-NUEVO", 4, 20000])
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        resp_preview = self.admin.post(
            "/api/facturas/carga-masiva/previsualizar",
            data={"archivo": (buffer, "carga.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp_preview.status_code, 200, resp_preview.get_data(as_text=True))
        data = resp_preview.get_json()
        grupo = data["facturas"][0]
        # Ya NO bloquea: la fila con categoría/modelo que no matchea queda
        # como "producto nuevo, pendiente de crear", no como error.
        self.assertTrue(grupo["valida"])
        self.assertEqual(grupo["errores"], [])
        self.assertIsNone(grupo["lineas"][0]["producto_id"])
        self.assertIn("AMORTIGUADOR", grupo["lineas"][0]["texto_original"])

        resp_confirmar = self.admin.post(
            "/api/facturas/carga-masiva/confirmar", json={"token": data["token"], "condicion_pago": "credito"},
        )
        self.assertEqual(resp_confirmar.status_code, 200, resp_confirmar.get_data(as_text=True))
        confirmadas = resp_confirmar.get_json()
        self.assertIn("FACT-EXCEL-NUEVO-001", confirmadas["creadas"])

        pendientes = self.admin.get("/api/facturas/pendientes-recepcion?estado=pendiente").get_json()["pendientes"]
        pendiente_creado = next(p for p in pendientes if p["numero"] == "FACT-EXCEL-NUEVO-001")
        self.assertIsNone(pendiente_creado["lineas"][0]["producto_id"])


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
            json={
                "username": username, "password": password, "nombre": username,
                "es_admin": rol == "admin",
                "modulos": [] if rol == "admin" else [rol],
            },
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

    def test_bodega_puede_crear_factura_pendiente_de_recepcion(self):
        # Desde la nueva ruta de ingreso directo (ver
        # _crear_pendiente_y_aplicar_si_posible en app.py): Bodega es
        # ahora quien ingresa las facturas al recibir la mercadería, así
        # que este endpoint ya no es exclusivo de Finanzas.
        self._crear_usuario("rol_test_bodega1", "clave123456", "bodega")
        client = self._client_como("rol_test_bodega1", "clave123456")
        producto = self._crear_producto_http(self.admin_client, "Producto - rol bodega crea factura")
        resp = self._crear_pendiente_recepcion(client, producto["id"], "FACT-ROL-001")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

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
        # Se agrega una segunda línea SIN resolver a propósito, para que la
        # factura no se aplique de inmediato al crearla (ver
        # _crear_pendiente_y_aplicar_si_posible en app.py) y se pueda
        # seguir probando que Bodega (y no Finanzas) es quien aprueba la
        # recepción a mano, con la factura todavía en estado 'pendiente'.
        resp_crear = finanzas_client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": "FACT-ROL-004",
                "lineas": [
                    {"producto_id": producto["id"], "cantidad": 1, "precio_unitario": 1000},
                    {"producto_nuevo_nombre": "Repuesto sin resolver", "cantidad": 1, "precio_unitario": 100},
                ],
                "condicion_pago": "credito",
            },
        )
        pendiente = resp_crear.get_json()
        self.assertEqual(pendiente["estado"], "pendiente")
        resp_vincular = bodega_client.post(
            f"/api/facturas/pendientes-recepcion/{pendiente['id']}/lineas/1/crear-producto",
            json={"categoria": "REPUESTO SIN RESOLVER ROL", "modelo": "X", "marca": "GENERICA"},
        )
        self.assertEqual(resp_vincular.status_code, 201, resp_vincular.get_data(as_text=True))
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

    def test_bodega_no_puede_entrar_a_otros_modulos_ni_al_panel_principal(self):
        # Bug reportado: un usuario 'bodega' podía navegar a cualquier
        # módulo con solo escribir la URL (las páginas no tenían ningún
        # control de rol, a diferencia de los endpoints de API sensibles
        # que ya estaban protegidos). Ahora cada rol no-admin queda
        # encerrado en su único módulo permitido (ver requiere_modulo() /
        # home_del_rol() en core.py).
        self._crear_usuario("rol_test_bodega_paginas", "clave123456", "bodega")
        client = self._client_como("rol_test_bodega_paginas", "clave123456")
        for ruta in ("/operativo", "/convertidores", "/agente-ia"):
            resp = client.get(ruta)
            self.assertEqual(resp.status_code, 302, f"{ruta} debería redirigir, dio {resp.status_code}")
            self.assertIn("/logistica", resp.headers["Location"], f"{ruta} no redirigió a /logistica")
        # /finanzas es la excepción, a propósito, desde la nueva ruta de
        # ingreso directo de facturas (ver vista_finanzas() en app.py):
        # Bodega SÍ entra, pero la plantilla le esconde todo salvo la
        # pestaña "Facturas" (ver 'solo_facturas' en finanzas.html, y
        # test_usuario_bodega_entra_a_finanzas_pero_solo_ve_la_pestana_facturas
        # en test_multi_modulos_usuario.py para el detalle completo).
        resp_finanzas = client.get("/finanzas")
        self.assertEqual(resp_finanzas.status_code, 200)
        # /administracion ya redirigía siempre a "/" (comportamiento previo,
        # sin cambios) -- y "/" a su vez redirige a /logistica para este rol.
        resp_admin = client.get("/administracion")
        self.assertEqual(resp_admin.status_code, 302)
        self.assertTrue(resp_admin.headers["Location"].endswith("/"))
        resp_raiz = client.get("/", follow_redirects=True)
        self.assertEqual(resp_raiz.status_code, 200)
        self.assertIn("/logistica", resp_raiz.request.path)
        resp_propio = client.get("/logistica")
        self.assertEqual(resp_propio.status_code, 200)

    def test_operativo_no_puede_entrar_a_logistica_ni_a_finanzas(self):
        self._crear_usuario("rol_test_operativo_paginas", "clave123456", "operativo")
        client = self._client_como("rol_test_operativo_paginas", "clave123456")
        for ruta in ("/logistica", "/finanzas", "/convertidores"):
            resp = client.get(ruta)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/operativo", resp.headers["Location"])
        # Sí tiene acceso a su propio módulo y a Agente IA (van de la mano).
        self.assertEqual(client.get("/operativo").status_code, 200)
        self.assertEqual(client.get("/agente-ia").status_code, 200)

    def test_admin_puede_entrar_a_todos_los_modulos(self):
        for ruta in ("/", "/logistica", "/operativo", "/finanzas", "/convertidores", "/agente-ia", "/administracion"):
            resp = self.admin_client.get(ruta)
            self.assertEqual(resp.status_code, 200, f"{ruta} debería dar 200 para admin, dio {resp.status_code}")

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


class TestEstadoResultados(unittest.TestCase):
    """Estado de Resultados mensual de Finanzas: rubro_gav (clasificación
    fija de gastos de Caja Chica), los datos manuales por mes, y que no se
    duplique plata entre Costo de Ventas y GAV. Usa un mes bien alejado de
    "hoy" (2031-01) para no mezclarse con gastos de fecha por defecto que
    crean otros tests de esta misma clase o de otras clases del archivo."""

    MES = "2031-01"

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_gasto(self, monto, rubro_gav=None, dia="10", **extra):
        """Migración 25: el rubro_gav ya no se pone al crear el gasto (retiro
        de banco), sino en una boleta contra ese gasto -- acá se hacen los
        dos pasos seguidos para no tener que repetirlos en cada test. Se
        crea siempre una boleta por el monto completo (rubro_gav='' si no
        se pasa nada, igual que el comportamiento anterior de un gasto "sin
        clasificar"). Migración 27: "compra de emergencia" (producto_id /
        cantidad) también se pasa a la boleta, no al gasto/retiro -- recién
        ahí se sabe qué se compró de verdad."""
        data_gasto = {"fecha": f"{self.MES}-{dia}", "monto": str(monto)}
        resp = self.client.post("/api/caja/gasto", data=data_gasto)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        gasto = resp.get_json()

        data_boleta = {"monto": str(monto), "rubro_gav": rubro_gav or "", "descripcion": "test estado resultados"}
        data_boleta.update(extra)
        resp_boleta = self.client.post(f"/api/caja/gasto/{gasto['id']}/boleta", data=data_boleta)
        self.assertEqual(resp_boleta.status_code, 201, resp_boleta.get_data(as_text=True))
        return resp_boleta.get_json()

    def _reporte(self):
        resp = self.client.get(f"/api/finanzas/estado-resultados?mes={self.MES}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def test_rubro_gav_invalido_es_rechazado(self):
        gasto = self.client.post(
            "/api/caja/gasto", data={"fecha": f"{self.MES}-05", "monto": "1000"}
        ).get_json()
        resp = self.client.post(
            f"/api/caja/gasto/{gasto['id']}/boleta",
            data={"monto": "1000", "rubro_gav": "no_existe_este_rubro"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_mes_con_formato_invalido_es_rechazado(self):
        resp = self.client.get("/api/finanzas/estado-resultados?mes=enero-2031")
        self.assertEqual(resp.status_code, 400)

    def test_gasto_clasificado_suma_en_su_rubro_y_sin_clasificar_queda_fuera_del_total(self):
        antes = self._reporte()
        total_antes = antes["gav"]["total"]
        sin_clasificar_antes = antes["gav"]["sin_clasificar"]

        self._crear_gasto(20000, rubro_gav="combustible", dia="12")
        self._crear_gasto(7000, dia="13")  # sin rubro -> sin clasificar

        despues = self._reporte()
        self.assertAlmostEqual(
            despues["gav"]["por_rubro"].get("combustible", 0)
            - antes["gav"]["por_rubro"].get("combustible", 0),
            20000, delta=0.01,
        )
        self.assertAlmostEqual(despues["gav"]["sin_clasificar"] - sin_clasificar_antes, 7000, delta=0.01)
        # El gasto sin clasificar sumó a "sin_clasificar" pero NO al total
        # de GAV -- solo el clasificado (20000) debe reflejarse en el total.
        self.assertAlmostEqual(despues["gav"]["total"] - total_antes, 20000, delta=0.01)

    def test_compra_de_emergencia_no_se_cuenta_como_gav_aunque_lleve_rubro(self):
        """Una compra de emergencia (con producto_id) ingresa stock: su costo
        ya se va a contar como costo de ventas el día que ese repuesto se
        use en una OT, así que NO debe sumar también como gasto GAV -- eso
        sería contar la misma plata dos veces."""
        producto = self.client.post(
            "/api/productos", json={"nombre": "Repuesto ER test", "stock_disponible": 0, "costo_compra": 0}
        ).get_json()

        antes = self._reporte()
        total_gav_antes = antes["gav"]["total"]
        sin_clasificar_antes = antes["gav"]["sin_clasificar"]

        self._crear_gasto(
            50000, rubro_gav="generales", dia="14",
            producto_id=str(producto["id"]), cantidad="5",
        )

        despues = self._reporte()
        self.assertAlmostEqual(despues["gav"]["total"], total_gav_antes, delta=0.01)
        self.assertAlmostEqual(despues["gav"]["sin_clasificar"], sin_clasificar_antes, delta=0.01)

    def test_datos_manuales_se_guardan_por_mes_y_entran_en_los_subtotales(self):
        resp = self.client.post(
            "/api/finanzas/estado-resultados/manual",
            json={
                "mes": self.MES, "otros_ingresos": 100000, "remuneraciones": 900000,
                "arriendo": 200000, "gastos_financieros_intereses": 50000,
                "impuesto_renta": 10000, "dividendos": 30000,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        data = self._reporte()
        self.assertAlmostEqual(data["ventas"]["otros_ingresos"], 100000, delta=0.01)
        self.assertAlmostEqual(data["gav"]["remuneraciones"], 900000, delta=0.01)
        self.assertAlmostEqual(data["gav"]["arriendo"], 200000, delta=0.01)
        self.assertAlmostEqual(data["gastos_financieros"]["intereses"], 50000, delta=0.01)
        self.assertAlmostEqual(data["impuesto_renta"], 10000, delta=0.01)
        self.assertAlmostEqual(data["dividendos"], 30000, delta=0.01)

        # Verifica que los subtotales realmente encadenen entre sí (no solo
        # que cada campo manual se haya guardado suelto).
        esperado_utilidad_bruta = data["ventas"]["total"] - data["costo_ventas"]["total"]
        self.assertAlmostEqual(data["utilidad_bruta"], esperado_utilidad_bruta, delta=0.01)
        esperado_operacional = data["utilidad_bruta"] - data["gav"]["total"]
        self.assertAlmostEqual(data["utilidad_operacional"], esperado_operacional, delta=0.01)
        esperado_antes_impuesto = data["utilidad_operacional"] - data["gastos_financieros"]["total"]
        self.assertAlmostEqual(data["utilidad_antes_impuesto"], esperado_antes_impuesto, delta=0.01)
        esperado_neta = data["utilidad_antes_impuesto"] - data["impuesto_renta"]
        self.assertAlmostEqual(data["utilidad_neta"], esperado_neta, delta=0.01)
        esperado_retenida = data["utilidad_neta"] - data["dividendos"]
        self.assertAlmostEqual(data["utilidad_retenida"], esperado_retenida, delta=0.01)

    def test_guardar_manual_de_nuevo_reemplaza_los_valores_del_mismo_mes(self):
        self.client.post(
            "/api/finanzas/estado-resultados/manual",
            json={"mes": self.MES, "seguros": 15000},
        )
        primero = self._reporte()
        self.assertAlmostEqual(primero["gav"]["seguros"], 15000, delta=0.01)

        # Volver a guardar el mismo mes con seguros=0 debe REEMPLAZAR el
        # valor anterior, no sumarse ni dejarlo como estaba.
        self.client.post(
            "/api/finanzas/estado-resultados/manual",
            json={"mes": self.MES, "seguros": 0},
        )
        segundo = self._reporte()
        self.assertAlmostEqual(segundo["gav"]["seguros"], 0, delta=0.01)

    def test_manual_con_monto_negativo_es_rechazado(self):
        resp = self.client.post(
            "/api/finanzas/estado-resultados/manual",
            json={"mes": self.MES, "arriendo": -1000},
        )
        self.assertEqual(resp.status_code, 400)

    def test_detalle_linea_invalida_da_400(self):
        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=no_existe")
        self.assertEqual(resp.status_code, 400)

    def test_detalle_de_linea_manual_devuelve_mensaje_no_lista(self):
        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=arriendo")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["tipo"], "manual")
        self.assertTrue(data["mensaje"])

    def test_detalle_de_rubro_gav_lista_los_gastos_que_lo_componen(self):
        gasto = self._crear_gasto(12345, rubro_gav="bancarios", dia="20")
        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=gav_bancarios")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["tipo"], "lista")
        montos = [i["monto"] for i in data["items"]]
        self.assertIn(12345.0, montos)

    def test_detalle_de_ventas_ot_lista_la_ot_que_la_compone(self):
        """Extremo a extremo: crea un cliente + OT, la marca 'entregado' con
        fecha de este mes de prueba (vía DB directa, como ya hace
        TestCorrelativoYListadoOperativo para simular una OT cerrada de un
        mes específico) y confirma que aparece en el detalle de ventas_ot
        con el monto correcto (Neto, sin IVA en este caso)."""
        cliente = self.client.post("/api/operativo/clientes", json={"nombre": "Cliente Detalle ER"}).get_json()
        orden = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente["id"]}).get_json()

        db = taller_app._conectar_db()
        db.execute(
            "UPDATE ordenes_trabajo SET estado = 'entregado', fecha_entrega_real = ?, "
            "precio_referencial = 77000, precio_iva = 0 WHERE id = ?",
            (f"{self.MES}-22T10:00:00", orden["id"]),
        )
        db.commit()
        db.close()

        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=ventas_ot")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        coincidencias = [i for i in data["items"] if i["monto"] == 77000.0 and str(orden["id"]) in i["etiqueta"]]
        self.assertEqual(len(coincidencias), 1, data["items"])

    def test_detalle_de_costo_repuestos_muestra_la_ot_y_el_nombre_del_repuesto(self):
        """El detalle de costo de repuestos debe listarse a nivel de ítem
        (OT + nombre del repuesto), no agrupado por OT con solo el cliente
        -- si no, no se entiende de qué repuesto se trata (pedido explícito
        del dueño al ver el detalle en pantalla)."""
        # /api/productos autogenera el nombre desde categoría+modelo, no
        # acepta un "nombre" arbitrario por form -- para fijar un nombre
        # elegido a mano (y así poder buscarlo en el detalle) se inserta
        # directo en la DB, como ya hace _crear_producto() en otros tests.
        db_setup = taller_app._conectar_db()
        producto_id = _crear_producto(db_setup, "Filtro de Aceite ER", stock_disponible=10, costo_compra=5000)
        db_setup.close()
        cliente = self.client.post("/api/operativo/clientes", json={"nombre": "Cliente Detalle Repuesto"}).get_json()
        orden = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente["id"]}).get_json()
        resp_item = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "repuesto", "producto_id": producto_id, "cantidad": 2},
        )
        self.assertEqual(resp_item.status_code, 201, resp_item.get_data(as_text=True))

        db = taller_app._conectar_db()
        db.execute(
            "UPDATE ordenes_trabajo SET estado = 'entregado', fecha_entrega_real = ? WHERE id = ?",
            (f"{self.MES}-15T10:00:00", orden["id"]),
        )
        db.commit()
        db.close()

        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=costo_repuestos")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        coincidencias = [
            i for i in data["items"]
            if str(orden["id"]) in i["etiqueta"] and "Filtro de Aceite ER" in i["etiqueta"]
        ]
        self.assertEqual(len(coincidencias), 1, data["items"])
        self.assertAlmostEqual(coincidencias[0]["monto"], 10000.0, delta=0.01)  # 2 x 5000

    def test_detalle_de_costo_mano_obra_muestra_la_ot_y_la_descripcion_del_trabajo(self):
        cliente = self.client.post("/api/operativo/clientes", json={"nombre": "Cliente Detalle Mano de Obra"}).get_json()
        orden = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente["id"]}).get_json()
        resp_item = self.client.post(
            f"/api/operativo/ordenes/{orden['id']}/items",
            json={"tipo": "mano_obra", "cantidad": 1, "costo_unitario": 25000, "descripcion": "Cambio de embrague"},
        )
        self.assertEqual(resp_item.status_code, 201, resp_item.get_data(as_text=True))

        db = taller_app._conectar_db()
        db.execute(
            "UPDATE ordenes_trabajo SET estado = 'entregado', fecha_entrega_real = ? WHERE id = ?",
            (f"{self.MES}-16T10:00:00", orden["id"]),
        )
        db.commit()
        db.close()

        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=costo_mano_obra")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        coincidencias = [
            i for i in data["items"]
            if str(orden["id"]) in i["etiqueta"] and "CAMBIO DE EMBRAGUE" in i["etiqueta"]
        ]
        self.assertEqual(len(coincidencias), 1, data["items"])
        self.assertAlmostEqual(coincidencias[0]["monto"], 25000.0, delta=0.01)

    def _crear_y_aprobar_factura_con_gasto_general(self, numero, monto=15000, rubro_gav="combustible", dia="10"):
        resp = self.client.post(
            "/api/facturas/pendientes-recepcion",
            json={
                "numero": numero,
                "fecha": f"{self.MES}-{dia}",
                "lineas": [{
                    "es_gasto_general": True, "descripcion": "Bencina generador",
                    "monto": monto, "rubro_gav": rubro_gav,
                }],
                "condicion_pago": "credito",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        pendiente = resp.get_json()
        # Una factura de puro gasto general (sin ninguna línea de producto
        # sin resolver) también queda aplicada de inmediato con la nueva
        # ruta de ingreso directo (ver _crear_pendiente_y_aplicar_si_posible
        # en app.py) -- ya no hace falta (ni se puede) aprobarla aparte.
        if pendiente["estado"] == "aprobada":
            return self.client.get(f"/api/facturas/{pendiente['factura_id']}").get_json()
        aprobar = self.client.post(f"/api/facturas/pendientes-recepcion/{pendiente['id']}/aprobar", json={})
        self.assertEqual(aprobar.status_code, 201, aprobar.get_data(as_text=True))
        return aprobar.get_json()

    def test_gasto_general_de_factura_suma_al_mismo_rubro_que_caja_chica(self):
        """El taller pidió una pestaña de Gastos Generales para ítems de
        factura (combustible, herramientas) que no van a inventario -- la
        clasificación reutiliza los mismos rubros de Caja Chica para que
        el Estado de Resultados quede completo sin importar por cuál de
        los dos caminos entró el gasto operacional."""
        antes = self._reporte()
        combustible_antes = antes["gav"]["por_rubro"].get("combustible", 0)
        total_antes = antes["gav"]["total"]
        facturas_total_antes = antes["gav"]["facturas_total"]

        self._crear_gasto(20000, rubro_gav="combustible", dia="12")  # vía Caja Chica
        self._crear_y_aprobar_factura_con_gasto_general(
            "FACT-GG-ESTADO-001", monto=15000, rubro_gav="combustible", dia="14",
        )  # vía factura de proveedor -- mismo rubro

        despues = self._reporte()
        self.assertAlmostEqual(despues["gav"]["por_rubro"]["combustible"], combustible_antes + 20000 + 15000, delta=0.01)
        self.assertAlmostEqual(despues["gav"]["total"], total_antes + 20000 + 15000, delta=0.01)
        self.assertAlmostEqual(despues["gav"]["facturas_total"], facturas_total_antes + 15000, delta=0.01)

    def test_detalle_de_gav_por_rubro_incluye_items_de_factura_y_de_caja_chica(self):
        self._crear_gasto(9000, rubro_gav="mantencion", dia="11")
        self._crear_y_aprobar_factura_con_gasto_general(
            "FACT-GG-DETALLE-001", monto=8000, rubro_gav="mantencion", dia="15",
        )

        resp = self.client.get(f"/api/finanzas/estado-resultados/detalle?mes={self.MES}&linea=gav_mantencion")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        montos = sorted(i["monto"] for i in data["items"])
        self.assertIn(9000.0, montos)
        self.assertIn(8000.0, montos)

    def test_endpoint_gastos_generales_lista_items_del_mes_con_su_factura(self):
        aprobada = self._crear_y_aprobar_factura_con_gasto_general(
            "FACT-GG-TAB-001", monto=12000, rubro_gav="combustible", dia="09",
        )
        resp = self.client.get(f"/api/finanzas/gastos-generales?mes={self.MES}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        item = next(i for i in data["items"] if i["numero_factura"] == "FACT-GG-TAB-001")
        self.assertEqual(item["monto"], 12000)
        self.assertEqual(item["rubro_gav"], "combustible")
        self.assertEqual(item["descripcion"], "Bencina generador")
        self.assertEqual(item["factura_id"], aprobada["id"])


class TestAccesoPorRolNotificaciones(unittest.TestCase):
    """Las sugerencias de diagnóstico y las facturas pendientes de
    recepción ya se ocultaban en la interfaz según el rol (la campanita y
    el aviso flotante), pero los endpoints que las sirven no tenían
    ningún candado del lado del servidor -- cualquier sesión iniciada
    podía leerlas igual llamando la URL directo. Esto prueba que ahora sí
    está cerrado: solo 'operativo' (sugerencias de mecánicos) y solo
    'bodega' (aviso de facturas pendientes) reciben esas notificaciones;
    admin siempre puede todo."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin_client = taller_app.app.test_client()
        cls.admin_client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_usuario(self, username, rol):
        resp = self.admin_client.post(
            "/api/admin/usuarios",
            json={
                "username": username, "password": "clave12345", "nombre": username,
                "es_admin": False, "modulos": [rol],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _client_como(self, username):
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": "clave12345"})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    def _crear_orden_para_sugerencia(self):
        cliente = self.admin_client.post(
            "/api/operativo/clientes", json={"nombre": "Cliente Acceso Rol"}
        ).get_json()
        orden = self.admin_client.post(
            "/api/operativo/ordenes", json={"cliente_id": cliente["id"]}
        ).get_json()
        return orden

    def test_sugerencias_de_una_ot_solo_las_ve_operativo_o_admin(self):
        orden = self._crear_orden_para_sugerencia()
        self._crear_usuario("finanzas_acc1", "finanzas")
        self._crear_usuario("operativo_acc1", "operativo")

        resp_finanzas = self._client_como("finanzas_acc1").get(
            f"/api/ordenes/{orden['id']}/sugerencias-diagnostico"
        )
        self.assertEqual(resp_finanzas.status_code, 403)

        resp_operativo = self._client_como("operativo_acc1").get(
            f"/api/ordenes/{orden['id']}/sugerencias-diagnostico"
        )
        self.assertEqual(resp_operativo.status_code, 200)

        resp_admin = self.admin_client.get(f"/api/ordenes/{orden['id']}/sugerencias-diagnostico")
        self.assertEqual(resp_admin.status_code, 200)

    def test_resumen_y_lista_de_notificaciones_solo_operativo_o_admin(self):
        self._crear_usuario("bodega_acc1", "bodega")
        self._crear_usuario("operativo_acc2", "operativo")

        cliente_bodega = self._client_como("bodega_acc1")
        self.assertEqual(cliente_bodega.get("/api/notificaciones/resumen").status_code, 403)
        self.assertEqual(cliente_bodega.get("/api/notificaciones/sugerencias-diagnostico").status_code, 403)

        cliente_operativo = self._client_como("operativo_acc2")
        self.assertEqual(cliente_operativo.get("/api/notificaciones/resumen").status_code, 200)
        self.assertEqual(cliente_operativo.get("/api/notificaciones/sugerencias-diagnostico").status_code, 200)

    def test_contador_de_facturas_pendientes_solo_bodega_o_admin(self):
        self._crear_usuario("finanzas_acc2", "finanzas")
        self._crear_usuario("bodega_acc2", "bodega")

        resp_finanzas = self._client_como("finanzas_acc2").get("/api/facturas/pendientes-recepcion/contador")
        self.assertEqual(resp_finanzas.status_code, 403)

        resp_bodega = self._client_como("bodega_acc2").get("/api/facturas/pendientes-recepcion/contador")
        self.assertEqual(resp_bodega.status_code, 200)

        resp_admin = self.admin_client.get("/api/facturas/pendientes-recepcion/contador")
        self.assertEqual(resp_admin.status_code, 200)

    def test_listado_y_detalle_de_facturas_pendientes_finanzas_y_bodega_si_operativo_no(self):
        # El listado/detalle SÍ lo necesita Finanzas (su propia pantalla de
        # facturas que registró) además de Bodega (quien las aprueba) --
        # a diferencia del contador/aviso flotante, que es solo Bodega.
        self._crear_usuario("operativo_acc3", "operativo")
        self._crear_usuario("finanzas_acc3", "finanzas")
        self._crear_usuario("bodega_acc3", "bodega")

        resp_operativo = self._client_como("operativo_acc3").get("/api/facturas/pendientes-recepcion")
        self.assertEqual(resp_operativo.status_code, 403)

        resp_finanzas = self._client_como("finanzas_acc3").get("/api/facturas/pendientes-recepcion")
        self.assertEqual(resp_finanzas.status_code, 200)

        resp_bodega = self._client_como("bodega_acc3").get("/api/facturas/pendientes-recepcion")
        self.assertEqual(resp_bodega.status_code, 200)

    def test_aprobar_factura_pendiente_sigue_siendo_solo_bodega(self):
        # Regresión: esto ya estaba restringido antes de este cambio, no
        # debería haberse tocado.
        self._crear_usuario("finanzas_acc4", "finanzas")
        resp = self._client_como("finanzas_acc4").post(
            "/api/facturas/pendientes-recepcion/999999/aprobar", json={}
        )
        self.assertEqual(resp.status_code, 403)


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
