"""
Tests de los endpoints de migración histórica que el sync con Talca reemplazó
-- ver el bloque "REEMPLAZADOS POR EL SYNC CON TALCA" en app.py.

Por qué existe este archivo: los seis endpoints de acá abajo emparejaban
contra Talca por RUT, por patente o por fecha, y sobre los datos reales
ninguno de esos es una llave única (90 RUT duplicados en clientes, 285
patentes duplicadas en vehiculos). El sync empareja solo por el id numérico de
Talca. Dejar varios escritores vivos sobre ordenes_trabajo y garantias, cada
uno con su propio criterio de identidad, divergiría en silencio, así que los
endpoints quedaron bloqueados con HTTP 410. El código sigue en su lugar, abajo
del guard, como referencia -- que es exactamente el riesgo que estos tests
cubren: un refactor futuro que mueva o borre el `return` de una línea los
resucita sin que nada falle.

El criterio con el que se decidió cuáles caen: **si solo cuelgan hijos de una
orden que ya existe, buscándola por legacy_ot_no, se quedan; si crean o
identifican órdenes, 410.** Por eso pagos-ot-abiertas sigue vivo y
repuestos-ot-abiertas no: los dos solo agregan hijos, pero el primero resuelve
la orden por legacy_ot_no y el segundo por patente.

Cubre: que los seis endpoints bloqueados responden 410 con un mensaje que
nombra el reemplazo, que no escriben nada en la base **teniendo una entrada
válida que sí las escribiría** (por eso el fixture de más abajo), que el 410
viene del guard y no de un archivo de datos que falta, que la vista
/admin/migracion-datos también quedó retirada, y que
/api/admin/migracion/pagos-ot-abiertas sigue vivo (solo toca pagos_orden y
busca la orden por legacy_ot_no, la misma llave del sync).

Cómo correrlos:
    cd tests && python3 -m unittest test_migracion_bloqueada -v
"""

import io
import json
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

# Datos que el fixture deja en la base para que los endpoints bloqueados
# tengan de qué agarrarse. Sin esto, "no escriben nada" sería un test vacío:
# los que cuelgan hijos (repuestos, ítems de garantías) no encontrarían ninguna
# orden ni garantía que completar y no escribirían igual, guard o no guard.
RUT_FIXTURE = "11.111.111-K"
PATENTE_FIXTURE = "TESTAA11"
FECHA_GARANTIA_FIXTURE = "2026-01-02T03:04:05"

# Los seis endpoints bloqueados y con qué se les manda una entrada VÁLIDA.
#
#   - 'archivo_data': el JSON de data/ que el endpoint leía del filesystem.
#     Importa que exista: si faltara, el código original respondería 400 "no se
#     encontró data/..." y el 410 que vemos no probaría nada.
#   - 'upload': el multipart de los que recibían los archivos a mano. Cada
#     payload está armado para que, si el guard desapareciera, el endpoint
#     escribiera de verdad -- que es lo que hace concluyente a
#     test_no_escriben_nada.
#
# Uno u otro, nunca los dos.
ENDPOINTS_BLOQUEADOS = [
    {
        "ruta": "/api/admin/migracion/ordenes-historicas",
        "archivo_data": "migracion_historica_ordenes.json",
        "upload": None,
    },
    {
        "ruta": "/api/admin/migracion/backfill-legacy-no",
        "archivo_data": "migracion_legacy_no_abiertas.json",
        "upload": None,
    },
    {
        # Crea clientes, autos y OT por RUT y patente, y ninguna de esas filas
        # lleva talca_id ni legacy_ot_no: ni siquiera serían adoptables por el
        # sync después. El payload usa un RUT y una patente distintos a los del
        # fixture, así que desbloqueado crearía las tres cosas.
        "ruta": "/api/admin/migracion/importar",
        "archivo_data": None,
        "upload": {
            "clientes_json": [
                {"rut_clave": "22222222K", "rut_crudo": "22.222.222-K", "nombre": "CLIENTE QUE NO DEBE NACER"}
            ],
            "autos_json": [{"rut_clave": "22222222K", "patente": "TESTBB22", "marca": "X", "modelo": "Y"}],
            "ordenes_json": [{"no": 1, "patente": "TESTBB22", "requerimiento": "NO DEBE ENTRAR", "precio": 1000}],
        },
    },
    {
        # Solo cuelga orden_items, pero busca la orden por patente (con
        # setdefault, o sea una arbitraria entre las del mismo vehículo).
        "ruta": "/api/admin/migracion/repuestos-ot-abiertas",
        "archivo_data": None,
        "upload": {
            "repuestos_json": [
                {
                    "legacy_no": 9001,
                    "patente": PATENTE_FIXTURE,
                    "repuestos": [{"modelo": "M", "descripcion": "D", "costo_unitario": 1000}],
                    "mano_obra": [{"nombre_reparador": "R", "detalle": "D", "cantidad": 1, "precio": 500}],
                }
            ]
        },
    },
    {
        # No toca ordenes_trabajo y cae igual: garantias comparte correlativo
        # con las OT (_asignar_id_correlativo en modulos/operativo.py) e
        # inserta sin id explícito, o sea que reparte números de OT por fuera
        # del sync. La fecha es distinta a la del fixture para que no se salte
        # por "ya existía".
        "ruta": "/api/admin/migracion/garantias-abiertas",
        "archivo_data": None,
        "upload": {
            "garantias_json": [
                {
                    "legacy_no": 9002,
                    "cliente_rut": RUT_FIXTURE,
                    "patente": PATENTE_FIXTURE,
                    "motivo": "NO DEBE NACER",
                    "diagnostico": "D",
                    "fecha_recepcion": "2026-03-04 05:06:07",
                }
            ]
        },
    },
    {
        # Cuelga garantia_items emparejando por RUT + fecha_recepcion exacta:
        # la fecha calza con la garantía del fixture a propósito.
        "ruta": "/api/admin/migracion/garantias-items",
        "archivo_data": None,
        "upload": {
            "garantias_items_json": [
                {
                    "legacy_no": 9003,
                    "cliente_rut": RUT_FIXTURE,
                    "fecha_recepcion": FECHA_GARANTIA_FIXTURE.replace("T", " "),
                    "repuestos": [{"modelo": "M", "descripcion": "D", "costo_unitario": 1000}],
                    "mano_obra": [{"nombre_reparador": "R", "detalle": "D", "precio": 500}],
                }
            ]
        },
    },
]

TABLAS_VIGILADAS = (
    "ordenes_trabajo", "orden_items", "pagos_orden", "clientes", "equipos",
    "garantias", "garantia_items",
)


def _ruta_data(nombre):
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", nombre)


def _multipart(upload):
    """Arma el multipart de un endpoint. Se rearma en cada llamada: los
    BytesIO se consumen al leerlos y reusarlos mandaría archivos vacíos, que
    es justo el caso que estos tests NO quieren probar."""
    return {
        campo: (io.BytesIO(json.dumps(contenido).encode("utf-8")), f"{campo}.json")
        for campo, contenido in upload.items()
    }


class TestMigracionReemplazadaPorSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})
        cls._crear_fixture()

    @classmethod
    def _crear_fixture(cls):
        """Un cliente, un equipo, una orden y una garantía para que los
        endpoints bloqueados tengan a qué engancharse si el guard fallara."""
        db = taller_app._conectar_db()
        try:
            ts = "2026-01-01T00:00:00"
            cur = db.execute(
                "INSERT INTO clientes (nombre, rut, creado_en) VALUES (?, ?, ?)",
                ("CLIENTE FIXTURE", RUT_FIXTURE, ts),
            )
            cls.cliente_id = cur.lastrowid
            cur = db.execute(
                "INSERT INTO equipos (cliente_id, identificador, creado_en) VALUES (?, ?, ?)",
                (cls.cliente_id, PATENTE_FIXTURE, ts),
            )
            cls.equipo_id = cur.lastrowid
            cur = db.execute(
                """INSERT INTO ordenes_trabajo (cliente_id, equipo_id, fecha_recepcion, creado_en, actualizado_en)
                   VALUES (?, ?, ?, ?, ?)""",
                (cls.cliente_id, cls.equipo_id, ts, ts, ts),
            )
            cls.orden_id = cur.lastrowid
            cur = db.execute(
                """INSERT INTO garantias (cliente_id, equipo_id, fecha_recepcion, creado_en, actualizado_en)
                   VALUES (?, ?, ?, ?, ?)""",
                (cls.cliente_id, cls.equipo_id, FECHA_GARANTIA_FIXTURE, ts, ts),
            )
            cls.garantia_id = cur.lastrowid
            db.commit()
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        """Deshace el fixture. No es prolijidad: la base de pruebas es una sola
        para todos los módulos de tests, y test_sync_talca hace
        `DELETE FROM equipos` en su setUp. Si el equipo del fixture queda
        colgando con una orden y una garantía apuntándole, ese DELETE muere con
        FOREIGN KEY constraint failed y revienta 23 tests de otro archivo.
        Se borra en orden de dependencia: primero lo que referencia."""
        db = taller_app._conectar_db()
        try:
            db.execute("DELETE FROM garantias WHERE id = ?", (cls.garantia_id,))
            db.execute("DELETE FROM ordenes_trabajo WHERE id = ?", (cls.orden_id,))
            db.execute("DELETE FROM equipos WHERE id = ?", (cls.equipo_id,))
            db.execute("DELETE FROM clientes WHERE id = ?", (cls.cliente_id,))
            db.commit()
        finally:
            db.close()

    def _conteos(self):
        """Foto de las tablas que los endpoints bloqueados escribían."""
        db = taller_app._conectar_db()
        try:
            return {
                tabla: db.execute(f"SELECT COUNT(*) AS n FROM {tabla}").fetchone()["n"]
                for tabla in TABLAS_VIGILADAS
            }
        finally:
            db.close()

    def _post(self, caso):
        """POST con entrada válida: multipart para los que reciben archivos,
        pelado para los que leían de data/."""
        if caso["upload"]:
            return self.client.post(
                caso["ruta"], data=_multipart(caso["upload"]), content_type="multipart/form-data"
            )
        return self.client.post(caso["ruta"])

    def test_responden_410(self):
        for caso in ENDPOINTS_BLOQUEADOS:
            with self.subTest(ruta=caso["ruta"]):
                resp = self._post(caso)
                self.assertEqual(
                    resp.status_code,
                    410,
                    f"{caso['ruta']} debería estar bloqueado con 410 Gone; devolvió {resp.status_code}",
                )

    def test_el_mensaje_nombra_el_reemplazo(self):
        # Sin esto el 410 es un callejón sin salida: quien lo reciba tiene que
        # poder saber con qué se reemplazó sin ir a leer el código.
        for caso in ENDPOINTS_BLOQUEADOS:
            with self.subTest(ruta=caso["ruta"]):
                datos = self._post(caso).get_json()
                self.assertEqual(datos.get("codigo"), "reemplazado_por_sync_talca")
                self.assertEqual(datos.get("endpoint"), caso["ruta"])
                self.assertIn("modulos.sync_talca", datos.get("reemplazo", ""))
                self.assertIn("modulos.sync_talca", datos.get("error", ""))
                self.assertIn("talca_id", datos.get("error", ""))

    def test_no_escriben_nada(self):
        # El guard va como primera sentencia de la función: ni siquiera se
        # abre el JSON. Si alguien lo mueve más abajo, esto lo caza -- las
        # entradas de ENDPOINTS_BLOQUEADOS están armadas para que un endpoint
        # desbloqueado escriba de verdad contra el fixture.
        antes = self._conteos()
        for caso in ENDPOINTS_BLOQUEADOS:
            self._post(caso)
        self.assertEqual(antes, self._conteos())

    def test_el_410_no_es_por_una_entrada_que_falta(self):
        # Si el JSON de entrada no estuviera, el código original respondería
        # 400 ("No se encontró data/..." o "Debes adjuntar el archivo...") y
        # este archivo de tests pasaría por el motivo equivocado. Para los de
        # data/, el archivo sigue ahí; para los de upload, el 410 de
        # test_responden_410 ya se obtuvo mandando el archivo, así que acá solo
        # se comprueba que un POST pelado NO devuelve el 400 de "falta el
        # archivo": el guard gana antes.
        for caso in ENDPOINTS_BLOQUEADOS:
            with self.subTest(ruta=caso["ruta"]):
                if caso["archivo_data"]:
                    self.assertTrue(
                        os.path.isfile(_ruta_data(caso["archivo_data"])),
                        f"falta data/{caso['archivo_data']}: el 410 de {caso['ruta']} dejaría de ser concluyente",
                    )
                else:
                    resp = self.client.post(caso["ruta"])
                    self.assertEqual(
                        resp.status_code,
                        410,
                        f"{caso['ruta']} sin archivo devolvió {resp.status_code}: el guard debería ir antes "
                        "de la validación de la entrada",
                    )

    def test_siguen_exigiendo_admin(self):
        # El bloqueo va DESPUÉS de @requiere_rol(), así que una sesión sin
        # permisos no debe llegar al 410 ni enterarse de nada.
        anonimo = taller_app.app.test_client()
        for caso in ENDPOINTS_BLOQUEADOS:
            with self.subTest(ruta=caso["ruta"]):
                resp = anonimo.post(caso["ruta"])
                self.assertNotEqual(resp.status_code, 410)
                self.assertNotEqual(resp.status_code, 200)

    def test_la_vista_de_migracion_tambien_esta_retirada(self):
        # /admin/migracion-datos era la única interfaz de /migracion/importar.
        # La ruta se conserva (se llegaba tipeando la URL a mano), pero como
        # aviso: si alguien devuelve el formulario, esto lo caza.
        resp = self.client.get("/admin/migracion-datos")
        self.assertEqual(resp.status_code, 410)
        html = resp.get_data(as_text=True)
        self.assertNotIn("/api/admin/migracion/importar", html)
        self.assertIn("sync_talca", html)

    def test_pagos_ot_abiertas_sigue_vivo(self):
        # Decisión deliberada: busca la orden por legacy_ot_no (el número de OT
        # en Talca, la misma llave del sync), solo inserta en pagos_orden, y no
        # escribe identidad de órdenes ni crea clientes o equipos. Si alguien
        # lo bloquea de arrastre junto a los otros, este test lo avisa.
        antes = self._conteos()
        resp = self.client.post("/api/admin/migracion/pagos-ot-abiertas")
        self.assertEqual(resp.status_code, 200)
        datos = resp.get_json()
        self.assertIn("pagos_creados", datos)
        # La única orden de esta base es la del fixture, y no tiene
        # legacy_ot_no: ninguna calza. Lo que importa es que resuelve por
        # legacy_ot_no y no inventa nada.
        self.assertEqual(datos["pagos_creados"], 0)
        self.assertEqual(datos["ordenes_procesadas"], 0)
        self.assertEqual(antes, self._conteos())


if __name__ == "__main__":
    unittest.main()
