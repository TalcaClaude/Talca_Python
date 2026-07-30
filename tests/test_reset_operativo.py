"""
Tests para el reseteo de datos operativos (para la migración desde el
sistema PHP antiguo) -- ver la sección "Reseteo de datos operativos" al
final de app.py: RESET_OPERATIVO_TABLAS, /api/admin/reset-operativo/vista-previa
y /api/admin/reset-operativo/ejecutar.

Cubre: que ambos endpoints exigen admin, que "vista-previa" no borra nada,
que "ejecutar" exige la frase de confirmación exacta, que crea un respaldo
local antes de tocar cualquier dato, que efectivamente vacía TODAS las
tablas de la lista, que preserva clientes/equipos/usuarios/catálogos
intactos, y que la base queda con las llaves foráneas reactivadas y sin
ninguna referencia rota al terminar.

Cómo correrlos:
    cd tests && python3 -m unittest test_reset_operativo -v
"""

import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class TestResetOperativo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        # Limpia la carpeta de respaldos locales antes de cada test para
        # poder verificar con certeza que "ejecutar" creó uno nuevo.
        if os.path.isdir(taller_app.RESPALDO_LOCAL_DIR):
            for f in os.listdir(taller_app.RESPALDO_LOCAL_DIR):
                os.remove(os.path.join(taller_app.RESPALDO_LOCAL_DIR, f))
        # Deja todas las tablas del reseteo (y las preservadas) en $0 antes
        # de cada test, para que los conteos de cada test sean predecibles
        # sin depender del orden de ejecución.
        db = taller_app._conectar_db()
        try:
            db.execute("PRAGMA foreign_keys = OFF")
            for tabla in taller_app.RESET_OPERATIVO_TABLAS:
                db.execute(f"DELETE FROM {tabla}")
            db.execute("DELETE FROM equipos")
            db.execute("DELETE FROM clientes")
            # Tablas preservadas que estos tests también siembran a mano
            # (con nombres/códigos fijos) -- se limpian acá para que cada
            # test parta de cero sin chocar con UNIQUE de un test anterior.
            db.execute("DELETE FROM categorias")
            db.execute("DELETE FROM marcas")
            db.execute("DELETE FROM modelos")
            db.execute("DELETE FROM ubicaciones")
            db.execute("PRAGMA foreign_keys = ON")
            db.commit()
        finally:
            db.close()

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

    def _sembrar_datos_operativos(self):
        """Inserta directamente por SQL una fila mínima válida en un buen
        subconjunto representativo de las tablas que el reseteo debe vaciar
        (una por cada grupo: finanzas de compra, caja chica, garantías,
        convertidores, OT, inventario, agente WhatsApp), más clientes/
        equipos/usuarios/catálogos que deben sobrevivir intactos. Usa SQL
        directo (no los endpoints) para no depender de la lógica de negocio
        de cada módulo -- lo que se está probando acá es el reseteo, no la
        creación de cada entidad."""
        ts = "2026-07-01T10:00:00"
        db = taller_app._conectar_db()
        try:
            self._insertar_datos_operativos(db, ts)
            db.commit()
        finally:
            db.close()

    def _insertar_datos_operativos(self, db, ts):
        db.execute(
            "INSERT INTO clientes (id, nombre, telefono, email, rut, notas, creado_en) "
            "VALUES (1, 'CLIENTE UNO', '', '', '', '', ?)", (ts,)
        )
        db.execute(
            "INSERT INTO equipos (id, cliente_id, identificador, marca, modelo, notas, creado_en) "
            "VALUES (1, 1, 'ABCD12', 'TOYOTA', 'HILUX', '', ?)", (ts,)
        )
        db.execute(
            "INSERT INTO proveedores (id, nombre, rut, telefono, email, direccion, creado_en) "
            "VALUES (1, 'PROVEEDOR UNO', '', '', '', '', ?)", (ts,)
        )
        db.execute(
            "INSERT INTO productos (id, codigo_interno, nombre, stock_disponible, stock_minimo, "
            "costo_compra, creado_en, actualizado_en) VALUES (1, 'P-1', 'FILTRO ACEITE', 10, 2, 1000, ?, ?)",
            (ts, ts),
        )
        db.execute(
            "INSERT INTO movimientos (producto_id, fecha, tipo, categoria, cantidad, motivo, usuario, "
            "stock_resultante) VALUES (1, ?, 'entrada', 'compra', 10, 'Carga inicial', 'admin', 10)", (ts,)
        )
        db.execute(
            "INSERT INTO facturas (id, numero, proveedor_id, fecha, creado_en) "
            "VALUES (1, 'F-1', 1, ?, ?)", (ts, ts)
        )
        db.execute(
            "INSERT INTO factura_lineas (factura_id, producto_id, cantidad, precio_unitario, aplicada) "
            "VALUES (1, 1, 10, 1000, 1)"
        )
        db.execute(
            "INSERT INTO cuentas_por_pagar (id, proveedor_id, factura_id, concepto, monto_total, "
            "fecha_emision, creado_en, actualizado_en) "
            "VALUES (1, 1, 1, 'Compra inicial', 10000, ?, ?, ?)", (ts, ts, ts)
        )
        db.execute(
            "INSERT INTO ordenes_trabajo (id, cliente_id, equipo_id, estado, descripcion_problema, "
            "fecha_recepcion, usuario, creado_en, actualizado_en) "
            "VALUES (1, 1, 1, 'ingreso', 'RUIDO EN FRENOS', ?, 'admin', ?, ?)", (ts, ts, ts)
        )
        db.execute(
            "INSERT INTO orden_items (orden_id, tipo, producto_id, descripcion, cantidad, costo_unitario, "
            "usuario, creado_en) VALUES (1, 'repuesto', 1, 'FILTRO ACEITE', 1, 1000, 'admin', ?)", (ts,)
        )
        db.execute(
            "INSERT INTO garantias (id, cliente_id, equipo_id, orden_origen_id, estado, usuario, creado_en) "
            "VALUES (1, 1, 1, 1, 'ingreso', 'admin', ?)", (ts,)
        )
        db.execute(
            "INSERT INTO garantia_items (garantia_id, tipo, producto_id, descripcion, cantidad, "
            "costo_unitario, usuario, creado_en) VALUES (1, 'repuesto', 1, 'FILTRO ACEITE', 1, 1000, 'admin', ?)",
            (ts,)
        )
        db.execute(
            "INSERT INTO convertidores_torque (id, origen_tipo, cliente_id, equipo_id, estado, usuario, creado_en) "
            "VALUES (1, 'stock_taller', 1, 1, 'ingreso', 'admin', ?)", (ts,)
        )
        db.execute(
            "INSERT INTO convertidor_items (convertidor_id, tipo, producto_id, descripcion, cantidad, "
            "costo_unitario, usuario, creado_en) VALUES (1, 'repuesto', 1, 'FILTRO ACEITE', 1, 1000, 'admin', ?)",
            (ts,)
        )
        db.execute(
            "INSERT INTO caja_rendiciones (id, fecha, usuario, total, creado_en) "
            "VALUES (1, ?, 'admin', 0, ?)", (ts, ts)
        )
        db.execute(
            "INSERT INTO caja_movimientos (id, tipo, monto, descripcion, usuario, fecha, creado_en) "
            "VALUES (1, 'retiro', 5000, 'Gasto de prueba', 'admin', ?, ?)", (ts, ts)
        )
        db.execute(
            "INSERT INTO agente_mecanicos (id, nombre, telefono, activo, creado_en) "
            "VALUES (1, 'MECANICO UNO', '+56911111111', 1, ?)", (ts,)
        )
        db.execute(
            "INSERT INTO agente_mensajes (telefono, mecanico_nombre, mensaje_texto, exito, creado_en) "
            "VALUES ('+56911111111', 'MECANICO UNO', 'listo', 1, ?)", (ts,)
        )
        # Catálogos y config que deben sobrevivir intactos.
        db.execute("INSERT INTO categorias (nombre) VALUES ('FRENOS')")
        db.execute("INSERT INTO marcas (nombre) VALUES ('TOYOTA')")
        db.execute("INSERT INTO modelos (nombre) VALUES ('HILUX')")
        db.execute(
            "INSERT INTO ubicaciones (pasillo, estanteria, nivel, codigo, creado_en) "
            "VALUES (1, 1, 1, 'A-01-01', ?)", (ts,)
        )

    def test_vista_previa_requiere_admin(self):
        cliente_bodega = self._crear_usuario_con_rol("bodega_reset", "bodega")
        resp = cliente_bodega.get("/api/admin/reset-operativo/vista-previa")
        self.assertEqual(resp.status_code, 403)

    def test_ejecutar_requiere_admin(self):
        cliente_operativo = self._crear_usuario_con_rol("operativo_reset", "operativo")
        resp = cliente_operativo.post(
            "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "RESETEAR TALLER"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_vista_previa_no_borra_nada_y_cuenta_bien(self):
        self._sembrar_datos_operativos()
        resp = self.client.get("/api/admin/reset-operativo/vista-previa")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["conteos"]["productos"], 1)
        self.assertEqual(data["conteos"]["ordenes_trabajo"], 1)
        self.assertEqual(data["conteos"]["garantias"], 1)
        self.assertEqual(data["conteos"]["convertidores_torque"], 1)
        self.assertGreater(data["total_filas"], 10)
        self.assertIn("clientes", data["tablas_preservadas"])
        self.assertIn("equipos", data["tablas_preservadas"])
        self.assertNotIn("clientes", data["conteos"])
        self.assertNotIn("productos", data["tablas_preservadas"])

        # No debe haber tocado ningún dato -- los productos siguen ahí.
        productos = self.client.get("/api/productos").get_json()["productos"]
        self.assertEqual(len(productos), 1)

    def test_ejecutar_sin_frase_correcta_no_borra_nada(self):
        self._sembrar_datos_operativos()
        resp = self.client.post(
            "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "resetear taller"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

        resp_vacio = self.client.post("/api/admin/reset-operativo/ejecutar", json={})
        self.assertEqual(resp_vacio.status_code, 400)

        productos = self.client.get("/api/productos").get_json()["productos"]
        self.assertEqual(len(productos), 1, "no debería haber borrado nada sin la frase correcta")

    def test_ejecutar_crea_respaldo_antes_de_borrar(self):
        self._sembrar_datos_operativos()
        self.assertEqual(len(taller_app._listar_respaldos_locales()), 0)

        resp = self.client.post(
            "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "RESETEAR TALLER"}
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["respaldo_local_creado"].endswith(".db"))

        copias = taller_app._listar_respaldos_locales()
        self.assertEqual(len(copias), 1)
        self.assertEqual(copias[0]["nombre"], data["respaldo_local_creado"])

    def test_ejecutar_vacia_todas_las_tablas_del_reseteo_y_preserva_el_resto(self):
        self._sembrar_datos_operativos()

        resp = self.client.post(
            "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "RESETEAR TALLER"}
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertGreater(data["total_eliminadas"], 10)

        db = taller_app._conectar_db()
        try:
            for tabla in taller_app.RESET_OPERATIVO_TABLAS:
                n = db.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
                self.assertEqual(n, 0, f"la tabla '{tabla}' debería haber quedado vacía")

            # Preservadas: siguen con sus datos.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM clientes").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM equipos").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM categorias").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM marcas").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM modelos").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ubicaciones").fetchone()[0], 1)
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 1)

            # Las llaves foráneas quedan reactivadas y sin ninguna violación
            # pendiente al terminar.
            fk_on = db.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk_on, 1)
            violaciones = db.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(list(violaciones), [])

            # La reactivación de llaves foráneas realmente se aplica: un
            # INSERT con una referencia inexistente debe seguir fallando.
            with self.assertRaises(taller_app.sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO equipos (cliente_id, identificador, creado_en) VALUES (99999, 'ZZ', ?)",
                    ("2026-07-01T10:00:00",),
                )
        finally:
            db.close()

        # También verificable vía API: el cliente sigue existiendo, pero ya
        # no tiene ninguna OT asociada.
        clientes = self.client.get("/api/operativo/clientes").get_json()["clientes"]
        self.assertEqual(len(clientes), 1)
        db2 = taller_app._conectar_db()
        try:
            self.assertEqual(db2.execute("SELECT COUNT(*) FROM ordenes_trabajo").fetchone()[0], 0)
        finally:
            db2.close()

    def test_ejecutar_sin_datos_no_falla(self):
        # Correr el reseteo con todo ya vacío (setUp ya deja todo en $0) no
        # debería fallar -- debe poder correrse más de una vez sin problema.
        resp = self.client.post(
            "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "RESETEAR TALLER"}
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["total_eliminadas"], 0)

    def test_diagnostico_no_encuentra_nada_en_una_base_recien_creada(self):
        # Sanity check estructural: en una base recién creada por init_db()
        # (sin ningún incidente histórico de esquema) el diagnóstico no
        # debería encontrar ni tablas sin clasificar ni riesgos -- si esto
        # falla, es que RESET_OPERATIVO_TABLAS/RESET_OPERATIVO_TABLAS_PRESERVADAS
        # quedaron desincronizadas del esquema real definido en init_db().
        resp = self.client.get("/api/admin/reset-operativo/diagnostico")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["tablas_no_clasificadas"], [])
        self.assertEqual(data["riesgos"], [])
        self.assertTrue(data["listo_para_ejecutar"])

    def test_diagnostico_requiere_admin(self):
        cliente_finanzas = self._crear_usuario_con_rol("fin_reset_diag", "finanzas")
        resp = cliente_finanzas.get("/api/admin/reset-operativo/diagnostico")
        self.assertEqual(resp.status_code, 403)

    def test_detecta_y_vacia_automaticamente_una_tabla_legado_huerfana(self):
        # Reproduce el escenario real que abortó el reseteo en producción:
        # una tabla '<algo>_legado...' que quedó de una reconstrucción
        # histórica de esquema (ver _reconstruir_tabla_legada_si_hace_falta
        # en core.py -- el incidente documentado ahí es justo con
        # 'garantias', que en su momento tuvo una columna 'orden_id' NOT
        # NULL apuntando a ordenes_trabajo, antes de existir
        # 'orden_origen_id'). Esa tabla vieja no aparece en RESET_OPERATIVO_TABLAS
        # (no existe ningún CREATE TABLE para ella en el código fuente -- se
        # crea en caliente con ALTER TABLE ... RENAME TO), así que sin la
        # detección dinámica por nombre, borrar 'ordenes_trabajo' la dejaría
        # con una referencia rota y abortaría el reseteo, exactamente lo que
        # le pasó al taller.
        self._sembrar_datos_operativos()
        db = taller_app._conectar_db()
        try:
            db.execute(
                """
                CREATE TABLE garantias_legado (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    orden_id INTEGER NOT NULL REFERENCES ordenes_trabajo(id),
                    motivo TEXT DEFAULT ''
                )
                """
            )
            db.execute("INSERT INTO garantias_legado (orden_id, motivo) VALUES (1, 'Reclamo antiguo')")
            db.commit()
        finally:
            db.close()

        # El diagnóstico ya no debería marcar nada -- la tabla legado se
        # detecta y se suma sola a lo que se va a vaciar.
        resp_diag = self.client.get("/api/admin/reset-operativo/diagnostico")
        self.assertEqual(resp_diag.status_code, 200, resp_diag.get_data(as_text=True))
        data_diag = resp_diag.get_json()
        self.assertEqual(data_diag["tablas_no_clasificadas"], [])
        self.assertEqual(data_diag["riesgos"], [])
        self.assertTrue(data_diag["listo_para_ejecutar"])

        # Y "ejecutar" tiene que completarse sin abortar, vaciando también
        # garantias_legado.
        resp = self.client.post(
            "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "RESETEAR TALLER"}
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["eliminadas"].get("garantias_legado"), 1)

        db2 = taller_app._conectar_db()
        try:
            self.assertEqual(db2.execute("SELECT COUNT(*) FROM garantias_legado").fetchone()[0], 0)
            self.assertEqual(list(db2.execute("PRAGMA foreign_key_check").fetchall()), [])
        finally:
            db2.execute("DROP TABLE garantias_legado")
            db2.commit()
            db2.close()

    def test_diagnostico_detecta_tabla_desconocida_con_referencia_de_riesgo(self):
        # Caso más general que el de arriba: una tabla que NO sigue el
        # patrón '_legado' (o sea, ni siquiera se puede adivinar por
        # nombre) pero igual tiene una llave foránea propia hacia una
        # tabla que el reseteo sí va a vaciar. El diagnóstico tiene que
        # encontrarla iguel -- listar cada llave foránea real de la base es
        # justamente lo que permite no depender de que alguien se acuerde
        # de catalogar cada tabla nueva a mano.
        self._sembrar_datos_operativos()
        db = taller_app._conectar_db()
        try:
            db.execute(
                """
                CREATE TABLE cosa_no_catalogada (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    producto_id INTEGER REFERENCES productos(id)
                )
                """
            )
            db.execute("INSERT INTO cosa_no_catalogada (producto_id) VALUES (1)")
            db.commit()
        finally:
            db.close()

        try:
            resp = self.client.get("/api/admin/reset-operativo/diagnostico")
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            data = resp.get_json()
            self.assertIn("cosa_no_catalogada", data["tablas_no_clasificadas"])
            self.assertFalse(data["listo_para_ejecutar"])
            riesgo = next(r for r in data["riesgos"] if r["tabla"] == "cosa_no_catalogada")
            self.assertEqual(riesgo["columna"], "producto_id")
            self.assertEqual(riesgo["tabla_referenciada"], "productos")
            self.assertEqual(riesgo["filas_con_referencia"], 1)
            self.assertFalse(riesgo["tabla_preservada_a_proposito"])

            # El esquema de la tabla sin clasificar viene incluido, para
            # poder decidir qué es sin necesitar acceso directo a la base.
            esquema = data["esquema_tablas_no_clasificadas"]["cosa_no_catalogada"]
            self.assertEqual(esquema["filas"], 1)
            nombres_columnas = {c["nombre"] for c in esquema["columnas"]}
            self.assertEqual(nombres_columnas, {"id", "producto_id"})
            self.assertEqual(esquema["llaves_foraneas"], [{"columna": "producto_id", "tabla_referenciada": "productos"}])

            # Y "ejecutar" tiene que abortar sin tocar nada -- justo el
            # comportamiento que evitó el problema en producción.
            resp_ejecutar = self.client.post(
                "/api/admin/reset-operativo/ejecutar", json={"confirmacion": "RESETEAR TALLER"}
            )
            self.assertEqual(resp_ejecutar.status_code, 500)
            data_ejecutar = resp_ejecutar.get_json()
            self.assertIn("violaciones", data_ejecutar)
            self.assertTrue(len(data_ejecutar["violaciones"]) > 0)

            productos = self.client.get("/api/productos").get_json()["productos"]
            self.assertEqual(len(productos), 1, "el abort no debería haber tocado ningún dato")
        finally:
            db2 = taller_app._conectar_db()
            db2.execute("DROP TABLE cosa_no_catalogada")
            db2.commit()
            db2.close()


if __name__ == "__main__":
    unittest.main()
