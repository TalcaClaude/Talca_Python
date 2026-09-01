"""
Tests para la sincronización de clientes desde Talca.PHP -- ver
modulos/sync_talca.py y la sección "Contrato del primer endpoint" de
CLAUDE.md.

Cubre lo que, si se rompe en silencio, deja las dos bases divergiendo sin que
nadie se entere:

  - la migración (clientes.talca_id + índice único, clientes.comuna, tablas de
    estado) y que correrla de nuevo no rompa nada;
  - que el match sea SIEMPRE por talca_id y NUNCA por RUT (los datos reales
    tienen 442 clientes sin RUT y 90 RUT repetidos entre 195 filas);
  - que los upserts sean idempotentes y no pisen los campos locales;
  - que la marca de agua sea el 'hasta' que devuelve Talca y no la hora local,
    y que Python no le haga ninguna aritmética (el margen de 2 minutos lo
    aplica Talca);
  - la paginación por (updated_at, id), incluido el caso en que TODAS las
    filas comparten el mismo updated_at -- que es exactamente lo que deja el
    ALTER TABLE de la carga inicial;
  - que una corrida fallida no avance la marca de agua;
  - el filtro de filas basura, incluida la trampa de GASTON BURGOS.

Cómo correrlos:
    cd tests && python -m unittest test_sync_talca -v
"""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_sync_talca_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402
from modulos import sync_talca  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers para armar respuestas de Talca sin escribir el dict entero cada vez
# ---------------------------------------------------------------------------

def cliente_talca(id_, nombre="JUAN PEREZ", rut="11.111.111-1", **extra):
    fila = {
        "id": id_,
        "nombre_cliente": nombre,
        "rut": rut,
        "direccion": "AVENIDA SIEMPRE VIVA 742",
        "comuna": "MAIPU",
        "email": "juan@example.cl",
        "telefono": "912345678",
        "created_at": "2020-12-28 12:41:02",
        "updated_at": "2026-08-06 15:00:00",
    }
    fila.update(extra)
    return fila


def pagina(clientes, hasta="2026-08-06 19:42:11", hay_mas=False,
           siguiente_desde=None, siguiente_desde_id=None):
    respuesta = {
        "clientes": clientes,
        "hasta": hasta,
        "hay_mas": hay_mas,
        "total": len(clientes),
    }
    if hay_mas:
        respuesta["siguiente_desde"] = siguiente_desde
        respuesta["siguiente_desde_id"] = siguiente_desde_id
    return respuesta


class BaseSyncTalca(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True

    def setUp(self):
        self.db = taller_app._conectar_db()
        self.addCleanup(self.db.close)
        # Las órdenes van primero: ordenes_trabajo.cliente_id NO tiene
        # ON DELETE CASCADE (a diferencia de equipos), así que una orden
        # colgando hace fallar el DELETE de los clientes con FOREIGN KEY
        # constraint failed. Se borran solo las que cuelgan de un cliente que
        # este mismo archivo creó.
        self.db.execute(
            """
            DELETE FROM ordenes_trabajo
             WHERE cliente_id IN (
                SELECT id FROM clientes WHERE talca_id IS NOT NULL OR marcador_sync != ''
             )
            """
        )
        # Solo se limpia lo que trajo el sync (talca_id no nulo). Los clientes
        # que hayan creado otros tests del mismo proceso se dejan en paz:
        # borrarlos rompería sus equipos y órdenes por llave foránea, y de
        # paso estos tests demuestran que el sync convive con clientes que no
        # vienen de Talca.
        self.db.execute("DELETE FROM clientes WHERE talca_id IS NOT NULL")
        self.db.execute("DELETE FROM sync_talca_estado")
        self.db.execute("DELETE FROM sync_talca_omitidos")
        self.db.commit()

    def sincronizar(self, paginas, **kwargs):
        falso = sync_talca.ClienteTalcaFalso(paginas)
        resultado = sync_talca.sincronizar_clientes(self.db, cliente_api=falso, **kwargs)
        return resultado, falso

    def clientes_guardados(self):
        return self.db.execute(
            "SELECT * FROM clientes WHERE talca_id IS NOT NULL ORDER BY talca_id, id"
        ).fetchall()

    def estado(self):
        return sync_talca.leer_estado(self.db, "clientes")


# ---------------------------------------------------------------------------
# 1. Migración
# ---------------------------------------------------------------------------

class TestMigracion(BaseSyncTalca):
    def test_columnas_y_tablas_creadas(self):
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(clientes)").fetchall()]
        self.assertIn("talca_id", columnas)
        self.assertIn("comuna", columnas)

        indices = {i[1]: i for i in self.db.execute("PRAGMA index_list(clientes)").fetchall()}
        self.assertIn("idx_clientes_talca_id", indices)
        self.assertEqual(1, indices["idx_clientes_talca_id"][2], "el índice de talca_id tiene que ser ÚNICO")

        for tabla in ("sync_talca_estado", "sync_talca_omitidos"):
            self.assertIsNotNone(
                self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (tabla,)
                ).fetchone(),
                f"falta la tabla {tabla}",
            )

    def test_correrla_de_nuevo_no_falla(self):
        # Es el caso normal: init_db() la llama en cada arranque.
        sync_talca.init_sync_talca_db(self.db)
        sync_talca.init_sync_talca_db(self.db)
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(clientes)").fetchall()]
        self.assertEqual(1, columnas.count("talca_id"))
        self.assertEqual(1, columnas.count("comuna"))

    def test_indice_unico_impide_dos_filas_con_el_mismo_talca_id(self):
        self.db.execute(
            "INSERT INTO clientes (talca_id, nombre, creado_en) VALUES (?, ?, ?)",
            (7, "CLIENTE A", "2026-08-06T10:00:00"),
        )
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO clientes (talca_id, nombre, creado_en) VALUES (?, ?, ?)",
                (7, "CLIENTE B", "2026-08-06T10:00:00"),
            )
        self.db.rollback()

    def test_varios_clientes_locales_sin_talca_id_conviven(self):
        # Los clientes creados en este sistema no tienen talca_id. En SQLite
        # cada NULL es distinto de los demás dentro de un índice único, así
        # que el índice no les estorba.
        nombres = ("LOCAL SYNC 1", "LOCAL SYNC 2", "LOCAL SYNC 3")
        for nombre in nombres:
            self.db.execute(
                "INSERT INTO clientes (nombre, creado_en) VALUES (?, ?)",
                (nombre, "2026-08-06T10:00:00"),
            )
        self.db.commit()
        self.addCleanup(self._borrar_clientes_locales, nombres)

        self.assertEqual(
            3,
            self.db.execute(
                "SELECT COUNT(*) FROM clientes WHERE talca_id IS NULL AND nombre LIKE 'LOCAL SYNC %'"
            ).fetchone()[0],
        )

    def _borrar_clientes_locales(self, nombres):
        self.db.executemany("DELETE FROM clientes WHERE nombre = ?", [(n,) for n in nombres])
        self.db.commit()


# ---------------------------------------------------------------------------
# 2. Alta, actualización e idempotencia
# ---------------------------------------------------------------------------

class TestUpsert(BaseSyncTalca):
    def test_carga_inicial_mapea_los_campos(self):
        resultado, _ = self.sincronizar([pagina([
            cliente_talca(1, nombre="GERARD QUERELLE", rut="13.442.786-8",
                          direccion="MONTORO 10736", comuna="LA PINTANA",
                          email="ge@example.cl", telefono="968356626",
                          created_at="2020-12-28 12:41:02"),
        ])])

        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual(1, resultado["creadas"])
        filas = self.clientes_guardados()
        self.assertEqual(1, len(filas))
        fila = filas[0]
        self.assertEqual(1, fila["talca_id"])
        self.assertEqual("GERARD QUERELLE", fila["nombre"])
        self.assertEqual("13.442.786-8", fila["rut"])
        self.assertEqual("MONTORO 10736", fila["direccion"])
        self.assertEqual("LA PINTANA", fila["comuna"])
        self.assertEqual("ge@example.cl", fila["email"])
        self.assertEqual("968356626", fila["telefono"])
        # created_at de Talca -> creado_en en el formato ISO del sistema
        self.assertEqual("2020-12-28T12:41:02", fila["creado_en"])

    def test_normaliza_los_espacios_sobrantes_de_los_nombres(self):
        # 319 filas del dump vienen con espacios de más: 'GERARD  QUERELLE',
        # 'MAURICIO ARISMENDI ' con espacio final. Sin normalizar, buscar el
        # nombre bien escrito no encuentra al cliente.
        self.sincronizar([pagina([
            cliente_talca(1, nombre="GERARD  QUERELLE"),
            cliente_talca(2, nombre="MAURICIO ARISMENDI "),
            cliente_talca(3, nombre="BERNARDO   JARA  OPAZO", direccion="  LOS OLMOS   123 ",
                          comuna=" MAIPU ", rut=" 13.442.786-8 "),
        ])])

        filas = self.clientes_guardados()
        self.assertEqual("GERARD QUERELLE", filas[0]["nombre"])
        self.assertEqual("MAURICIO ARISMENDI", filas[1]["nombre"])
        self.assertEqual("BERNARDO JARA OPAZO", filas[2]["nombre"])
        # La normalización vale para todos los campos de texto, no solo nombre
        self.assertEqual("LOS OLMOS 123", filas[2]["direccion"])
        self.assertEqual("MAIPU", filas[2]["comuna"])
        self.assertEqual("13.442.786-8", filas[2]["rut"])

    def test_cualquier_campo_de_texto_puede_venir_null(self):
        # Confirmado contra la API: no es solo el email. En el dump hay 935
        # emails, 87 comunas, 63 direcciones, 8 RUT y 8 teléfonos en NULL.
        self.sincronizar([pagina([
            cliente_talca(1, nombre="JUAN PEREZ", rut=None, direccion=None,
                          comuna=None, email=None, telefono=None),
        ])])

        fila = self.clientes_guardados()[0]
        for campo in ("rut", "direccion", "comuna", "email", "telefono"):
            self.assertEqual("", fila[campo], f"{campo} debería quedar como cadena vacía, no None")

    def test_fecha_imposible_de_mariadb_no_se_guarda(self):
        # En los datos reales hay filas con '0000-00-00 00:00:00'.
        self.sincronizar([pagina([cliente_talca(1, created_at="0000-00-00 00:00:00")])])
        creado_en = self.clientes_guardados()[0]["creado_en"]
        self.assertFalse(creado_en.startswith("0000-00-00"))
        self.assertTrue(creado_en.startswith("20"))

    def test_correr_dos_veces_lo_mismo_no_duplica(self):
        filas_talca = [cliente_talca(1), cliente_talca(2, nombre="MARIA SOTO", rut="9.999.999-9")]
        self.sincronizar([pagina(filas_talca)])
        id_local_antes = self.clientes_guardados()[0]["id"]

        resultado, _ = self.sincronizar([pagina(filas_talca)])

        self.assertEqual(2, len(self.clientes_guardados()))
        self.assertEqual(0, resultado["creadas"])
        self.assertEqual(2, resultado["actualizadas"])
        self.assertEqual(id_local_antes, self.clientes_guardados()[0]["id"],
                         "el id local no puede cambiar al re-sincronizar")

    def test_actualiza_por_talca_id_sin_tocar_campos_locales(self):
        self.sincronizar([pagina([cliente_talca(1, telefono="911111111")])])
        id_local = self.clientes_guardados()[0]["id"]
        # Una nota escrita por el taller en ESTE sistema, que Talca no conoce.
        self.db.execute("UPDATE clientes SET notas = ? WHERE id = ?", ("Cliente difícil, cobrar al contado", id_local))
        self.db.commit()

        self.sincronizar([pagina([cliente_talca(1, telefono="922222222")])])

        fila = self.clientes_guardados()[0]
        self.assertEqual(id_local, fila["id"])
        self.assertEqual("922222222", fila["telefono"])
        self.assertEqual("Cliente difícil, cobrar al contado", fila["notas"],
                         "el sync no debe pisar las notas locales")


# ---------------------------------------------------------------------------
# 3. El RUT no es llave (lo más importante del módulo)
# ---------------------------------------------------------------------------

class TestElRutNoEsLlave(BaseSyncTalca):
    def test_dos_clientes_distintos_con_el_mismo_rut_son_dos_filas(self):
        # Caso real: 90 RUT repetidos entre 195 filas, la misma persona
        # cargada varias veces con el nombre escrito distinto.
        resultado, _ = self.sincronizar([pagina([
            cliente_talca(101, nombre="LEONARDO RIO SECO", rut="12.345.678-5"),
            cliente_talca(102, nombre="LEONARDO RIOSECO CERDA", rut="12.345.678-5"),
        ])])
        self.assertEqual(2, resultado["creadas"])
        filas = self.clientes_guardados()
        self.assertEqual(2, len(filas))
        self.assertEqual([101, 102], [f["talca_id"] for f in filas])

    def test_clientes_sin_rut_se_importan_igual(self):
        resultado, _ = self.sincronizar([pagina([
            cliente_talca(201, nombre="CLIENTE SIN RUT", rut=""),
            cliente_talca(202, nombre="OTRO SIN RUT", rut=None),
        ])])
        self.assertEqual(2, resultado["creadas"])
        self.assertEqual(["", ""], [f["rut"] for f in self.clientes_guardados()])

    def test_si_corrigen_el_rut_en_talca_sigue_siendo_la_misma_fila(self):
        self.sincronizar([pagina([cliente_talca(301, rut="1-9")])])
        id_local = self.clientes_guardados()[0]["id"]

        self.sincronizar([pagina([cliente_talca(301, rut="16.284.987-1")])])

        filas = self.clientes_guardados()
        self.assertEqual(1, len(filas), "corregir el RUT no puede crear un cliente nuevo")
        self.assertEqual(id_local, filas[0]["id"])
        self.assertEqual("16.284.987-1", filas[0]["rut"])

    def test_nombre_y_rut_invertidos_se_importan_tal_cual(self):
        # 3 filas reales vienen así (ej. id 2316). Son clientes de verdad: se
        # copian sin "arreglarlos", para que la corrección hecha en Talca el
        # día de mañana sí se refleje acá.
        self.sincronizar([pagina([
            cliente_talca(2316, nombre="13530609-6", rut="FANNY MUÑOZ"),
        ])])
        fila = self.clientes_guardados()[0]
        self.assertEqual("13530609-6", fila["nombre"])
        self.assertEqual("FANNY MUÑOZ", fila["rut"])


# ---------------------------------------------------------------------------
# 4. Filas que no son datos reales
# ---------------------------------------------------------------------------

class TestFiltroDeBasura(BaseSyncTalca):
    def test_filtra_vacias_y_ficticios_y_deja_registro(self):
        resultado, _ = self.sincronizar([pagina([
            cliente_talca(51, nombre="", rut="", direccion="", comuna="", email="", telefono=""),
            cliente_talca(362, nombre="GENERICO", rut="25.967.868-4"),
            cliente_talca(2379, nombre="prueba", rut="18.333.333-7"),
            cliente_talca(3767, nombre="CLIENTE PRUEBA", rut="12.345.678-9"),
            cliente_talca(500, nombre="CLIENTE DE VERDAD", rut="7.364.617-0"),
        ])])

        self.assertEqual(1, resultado["creadas"])
        self.assertEqual(4, resultado["omitidas"])
        self.assertEqual(
            {"fila_vacia": 1, "cliente_ficticio": 3},
            resultado["omitidas_por_motivo"],
        )
        self.assertEqual([500], [f["talca_id"] for f in self.clientes_guardados()])

        omitidos = {
            f["talca_id"]: f["motivo"]
            for f in self.db.execute("SELECT talca_id, motivo FROM sync_talca_omitidos").fetchall()
        }
        self.assertEqual(
            {51: "fila_vacia", 362: "cliente_ficticio", 2379: "cliente_ficticio",
             3767: "cliente_ficticio"},
            omitidos,
        )

    def test_los_clientes_de_gastos_se_importan(self):
        # Se filtraban hasta que el taller aclaró que administración los usa
        # como registro, junto con sus 24 "vehículos" espejo. Este test es el
        # que fija esa decisión de negocio.
        resultado, _ = self.sincronizar([pagina([
            cliente_talca(928, nombre="GASTOS GENERALES ABRIL", rut="ABRIL 1"),
            cliente_talca(954, nombre="GASTOS ADMON  MAYO", rut="GASTOS ADMON MAYO"),
            cliente_talca(978, nombre="GASTO GENERAL JUNIO", rut="JUNIO1"),
            # Y las personas reales que un filtro por substring 'GASTO' se
            # habría llevado puestas cuando la regla existía.
            cliente_talca(925, nombre="GASTON BURGOS", rut="6.380.743-5"),
            cliente_talca(2109, nombre="GASTON AGUIRRE", rut="8.509.827-6"),
        ])])

        self.assertEqual(5, resultado["creadas"])
        self.assertEqual(0, resultado["omitidas"])
        self.assertEqual([925, 928, 954, 978, 2109],
                         [f["talca_id"] for f in self.clientes_guardados()])

    def test_un_cliente_que_deja_de_estar_omitido_sale_de_la_lista(self):
        # Al aflojar una regla de filtrado (como pasó con gasto_imputado), el
        # registro viejo de "omitido" quedaría contradiciendo a la tabla
        # clientes, y la lista que el taller consulta para revisar diría que
        # se saltaron filas que están perfectamente cargadas.
        self.db.execute(
            """
            INSERT INTO sync_talca_omitidos (entidad, talca_id, motivo, datos_json, visto_en)
            VALUES ('clientes', 928, 'gasto_imputado', '{}', '2026-08-06T10:00:00')
            """
        )
        self.db.commit()

        self.sincronizar([pagina([cliente_talca(928, nombre="GASTOS GENERALES ABRIL")])])

        self.assertEqual(
            0,
            self.db.execute(
                "SELECT COUNT(*) FROM sync_talca_omitidos WHERE entidad = 'clientes' AND talca_id = 928"
            ).fetchone()[0],
        )

    def test_las_filas_vacias_vienen_intercaladas_con_las_reales(self):
        # Confirmado contra la API: los ids vacíos (51, 53, 55, 57...) no
        # están en un bloque aparte, salen mezclados con clientes reales desde
        # la primera página. El filtro se ejercita en cada página, no una vez.
        paginas = [
            pagina([
                cliente_talca(51, nombre="", rut="", direccion="", comuna="", email="", telefono=""),
                cliente_talca(1, nombre="GERARD QUERELLE"),
                cliente_talca(53, nombre="", rut="", direccion="", comuna="", email="", telefono=""),
                cliente_talca(2, nombre="MARIA SOTO"),
            ], hay_mas=True, siguiente_desde="2026-08-06 12:00:00", siguiente_desde_id=2),
            pagina([
                cliente_talca(55, nombre="", rut="", direccion="", comuna="", email="", telefono=""),
                cliente_talca(3, nombre="PEDRO ROJAS"),
            ]),
        ]
        resultado, _ = self.sincronizar(paginas, limite=4)

        self.assertEqual(3, resultado["creadas"])
        self.assertEqual({"fila_vacia": 3}, resultado["omitidas_por_motivo"])
        self.assertEqual([1, 2, 3], [f["talca_id"] for f in self.clientes_guardados()])

    def test_una_fila_con_solo_telefono_no_es_vacia(self):
        # "vacía" es que no tenga NINGÚN dato. Con un teléfono ya hay algo
        # que un humano puede usar para identificar al cliente.
        resultado, _ = self.sincronizar([pagina([
            cliente_talca(9, nombre="", rut="", direccion="", comuna="", email="", telefono="987654321"),
        ])])
        self.assertEqual(1, resultado["creadas"])
        self.assertEqual(0, resultado["omitidas"])

    def test_volver_a_ver_una_fila_omitida_no_acumula_registros(self):
        basura = pagina([cliente_talca(362, nombre="GENERICO", rut="25.967.868-4")])
        self.sincronizar([basura])
        self.sincronizar([basura])
        self.assertEqual(
            1, self.db.execute("SELECT COUNT(*) FROM sync_talca_omitidos").fetchone()[0]
        )

    def test_fila_sin_id_se_omite_en_vez_de_reventar(self):
        fila = cliente_talca(1)
        del fila["id"]
        resultado, _ = self.sincronizar([pagina([fila, cliente_talca(2)])])
        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual(1, resultado["creadas"])
        self.assertEqual({"sin_id": 1}, resultado["omitidas_por_motivo"])


# ---------------------------------------------------------------------------
# 5. Marca de agua
# ---------------------------------------------------------------------------

class TestMarcaDeAgua(BaseSyncTalca):
    def test_guarda_el_hasta_de_talca_y_no_el_reloj_local(self):
        # Un 'hasta' imposible de confundir con la hora de esta máquina.
        self.sincronizar([pagina([cliente_talca(1)], hasta="2031-01-01 00:00:00")])
        self.assertEqual("2031-01-01 00:00:00", self.estado()["marca_agua"])

    def test_primera_corrida_pide_sin_desde(self):
        _, falso = self.sincronizar([pagina([cliente_talca(1)])])
        self.assertEqual([{"entidad": "clientes", "desde": None, "desde_id": None, "limite": 200}], falso.llamadas)

    def test_segunda_corrida_manda_la_marca_tal_cual_sin_desde_id(self):
        # El margen de 2 minutos lo aplica Talca, no Python: acá la marca sale
        # textualmente como llegó, sin restarle nada. Y sin desde_id, que es
        # como Talca sabe que es el inicio de una corrida y tiene que aplicar
        # el margen (ver CLAUDE.md, "Contrato del primer endpoint").
        self.sincronizar([pagina([cliente_talca(1)], hasta="2026-08-06 19:42:11")])

        _, falso = self.sincronizar([pagina([], hasta="2026-08-06 19:44:00")])

        self.assertEqual(
            [{"entidad": "clientes", "desde": "2026-08-06 19:42:11", "desde_id": None, "limite": 200}],
            falso.llamadas,
        )
        self.assertEqual("2026-08-06 19:44:00", self.estado()["marca_agua"])

    def test_desde_forzado_vacio_fuerza_carga_completa(self):
        self.sincronizar([pagina([cliente_talca(1)], hasta="2026-08-06 19:42:11")])
        _, falso = self.sincronizar([pagina([cliente_talca(1)])], desde_forzado="")
        self.assertIsNone(falso.llamadas[0]["desde"])

    def test_registra_el_resultado_de_la_corrida(self):
        self.sincronizar([pagina([cliente_talca(1), cliente_talca(2)])])
        estado = self.estado()
        self.assertEqual("ok", estado["ultimo_resultado"])
        self.assertEqual(2, estado["filas_recibidas"])
        self.assertEqual(2, estado["filas_creadas"])
        self.assertEqual(0, estado["filas_actualizadas"])
        self.assertEqual(0, estado["filas_omitidas"])
        self.assertTrue(estado["ultima_corrida_en"], "falta la hora local de la corrida")
        self.assertIn("creadas", estado["ultimo_detalle"])


# ---------------------------------------------------------------------------
# 6. Paginación
# ---------------------------------------------------------------------------

class TestPaginacion(BaseSyncTalca):
    def test_recorre_todas_las_paginas_con_el_mismo_updated_at(self):
        # El escenario del ALTER TABLE de la carga inicial: MariaDB deja TODAS
        # las filas con la misma hora. Sin el desempate por id, la segunda
        # vuelta traería otra vez las mismas filas y esto no terminaría nunca.
        T = "2026-08-06 12:00:00"
        paginas = [
            pagina([cliente_talca(1, updated_at=T), cliente_talca(2, updated_at=T)],
                   hay_mas=True, siguiente_desde=T, siguiente_desde_id=2),
            pagina([cliente_talca(3, updated_at=T), cliente_talca(4, updated_at=T)],
                   hay_mas=True, siguiente_desde=T, siguiente_desde_id=4),
            pagina([cliente_talca(5, updated_at=T)], hasta="2026-08-06 12:00:30"),
        ]
        resultado, falso = self.sincronizar(paginas, limite=2)

        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual(3, resultado["paginas"])
        self.assertEqual(5, resultado["creadas"])
        self.assertEqual([1, 2, 3, 4, 5], [f["talca_id"] for f in self.clientes_guardados()])
        self.assertEqual(
            [
                {"entidad": "clientes", "desde": None, "desde_id": None, "limite": 2},
                {"entidad": "clientes", "desde": T, "desde_id": 2, "limite": 2},
                {"entidad": "clientes", "desde": T, "desde_id": 4, "limite": 2},
            ],
            falso.llamadas,
        )
        self.assertEqual("2026-08-06 12:00:30", self.estado()["marca_agua"])

    def test_corta_si_una_pagina_repite_los_mismos_ids(self):
        # Cinturón de seguridad por si el endpoint ignorara desde_id.
        mismos = [cliente_talca(1), cliente_talca(2)]
        paginas = [
            pagina(list(mismos), hay_mas=True, siguiente_desde="2026-08-06 12:00:00", siguiente_desde_id=2),
            pagina(list(mismos), hay_mas=True, siguiente_desde="2026-08-06 12:00:00", siguiente_desde_id=4),
        ]
        resultado, _ = self.sincronizar(paginas)
        self.assertEqual("error", resultado["resultado"])
        self.assertIn("no está avanzando", resultado["detalle"])
        self.assertEqual("", self.estado()["marca_agua"])

    def test_corta_si_el_cursor_no_avanza(self):
        self.sincronizar([pagina([cliente_talca(1)], hasta="2026-01-01 00:00:00")])
        paginas = [
            pagina([cliente_talca(2)], hay_mas=True, siguiente_desde="2026-01-01 00:00:00", siguiente_desde_id=5),
            pagina([cliente_talca(3)], hay_mas=True, siguiente_desde="2026-01-01 00:00:00", siguiente_desde_id=5),
        ]
        resultado, _ = self.sincronizar(paginas)
        self.assertEqual("error", resultado["resultado"])
        self.assertIn("no avanzó", resultado["detalle"])

    def test_hay_mas_sin_filas_es_un_error(self):
        resultado, _ = self.sincronizar([
            pagina([], hay_mas=True, siguiente_desde="2026-08-06 12:00:00", siguiente_desde_id=9)
        ])
        self.assertEqual("error", resultado["resultado"])
        self.assertIn("sin clientes", resultado["detalle"])

    def test_ultima_pagina_sin_hasta_es_un_error(self):
        resultado, _ = self.sincronizar([pagina([cliente_talca(1)], hasta="")])
        self.assertEqual("error", resultado["resultado"])
        self.assertEqual("", self.estado()["marca_agua"])


# ---------------------------------------------------------------------------
# 7. Fallas a mitad de corrida
# ---------------------------------------------------------------------------

class TestFallas(BaseSyncTalca):
    def test_falla_en_la_segunda_pagina_no_avanza_la_marca_pero_conserva_lo_traido(self):
        paginas = [
            pagina([cliente_talca(1), cliente_talca(2)], hay_mas=True,
                   siguiente_desde="2026-08-06 12:00:00", siguiente_desde_id=2),
            sync_talca.ClienteTalcaError("se cayó el hosting"),
        ]
        resultado, _ = self.sincronizar(paginas)

        self.assertEqual("error", resultado["resultado"])
        self.assertIn("se cayó el hosting", resultado["detalle"])
        self.assertEqual("", self.estado()["marca_agua"],
                         "una corrida fallida no puede avanzar la marca de agua")
        self.assertEqual("error", self.estado()["ultimo_resultado"])
        self.assertEqual([1, 2], [f["talca_id"] for f in self.clientes_guardados()],
                         "lo confirmado en las páginas anteriores se conserva")

    def test_la_corrida_siguiente_reintenta_desde_el_ultimo_punto_bueno(self):
        self.sincronizar([pagina([cliente_talca(1)], hasta="2026-08-06 10:00:00")])
        self.sincronizar([sync_talca.ClienteTalcaError("timeout")])
        self.assertEqual("2026-08-06 10:00:00", self.estado()["marca_agua"])

        _, falso = self.sincronizar([pagina([cliente_talca(1)], hasta="2026-08-06 11:00:00")])

        self.assertEqual("2026-08-06 10:00:00", falso.llamadas[0]["desde"])
        self.assertEqual("2026-08-06 11:00:00", self.estado()["marca_agua"])

    def test_un_corte_de_conexion_a_mitad_de_corrida_no_pierde_ni_duplica_filas(self):
        # Una carga inicial son 24 páginas contra un hosting compartido. Si un
        # corte puntual en la página 2 abortara la corrida, habría que empezar
        # de nuevo cada vez. Acá se usa el cliente HTTP de verdad (con el
        # módulo requests reemplazado) para ejercitar el reintento completo.
        doble = _RequestsGuionado([
            _RespuestaFalsa(200, pagina([cliente_talca(1), cliente_talca(2)], hay_mas=True,
                                        siguiente_desde="2026-08-06 12:00:00", siguiente_desde_id=2)),
            OSError("connection aborted: RemoteDisconnected"),
            _RespuestaFalsa(200, pagina([cliente_talca(3), cliente_talca(4)],
                                        hasta="2026-08-06 12:00:30")),
        ])
        original = sync_talca.requests
        sync_talca.requests = doble
        self.addCleanup(lambda: setattr(sync_talca, "requests", original))

        esperas = []
        api = sync_talca.ClienteTalcaHTTP(
            base_url="https://elhosting", api_key="x", dormir=esperas.append
        )

        resultado = sync_talca.sincronizar_clientes(self.db, cliente_api=api, limite=2)

        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual(4, resultado["recibidas"], "el intento fallido no puede contar filas")
        self.assertEqual(4, resultado["creadas"])
        self.assertEqual(0, resultado["actualizadas"], "la página reintentada no puede procesarse dos veces")
        self.assertEqual([1, 2, 3, 4], [f["talca_id"] for f in self.clientes_guardados()])
        self.assertEqual([1], esperas)
        self.assertEqual("2026-08-06 12:00:30", self.estado()["marca_agua"])

    def test_dry_run_no_escribe_nada(self):
        resultado, _ = self.sincronizar(
            [pagina([cliente_talca(1), cliente_talca(362, nombre="GENERICO")])],
            dry_run=True,
        )
        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual(1, resultado["creadas"])
        self.assertEqual(1, resultado["omitidas"])
        self.assertEqual([], self.clientes_guardados())
        self.assertEqual("", self.estado()["marca_agua"])
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM sync_talca_omitidos").fetchone()[0])


# ---------------------------------------------------------------------------
# 8. El cliente HTTP (sin red: se le cambia el módulo requests por un doble)
# ---------------------------------------------------------------------------

class _RespuestaFalsa:
    def __init__(self, status_code=200, cuerpo=None, texto=""):
        self.status_code = status_code
        self._cuerpo = cuerpo
        self.text = texto

    def json(self):
        if self._cuerpo is None:
            raise ValueError("no es JSON")
        return self._cuerpo


class _RequestsFalso:
    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.llamadas = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.llamadas.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return self.respuesta


class _RequestsGuionado:
    """Devuelve una cosa distinta en cada llamada, según un guion. Un
    elemento que sea una excepción se levanta: así se simula que el hosting
    corta la conexión (que es lo que hace Bluehosting con python-requests)."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.llamadas = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.llamadas.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if not self.guion:
            raise AssertionError("el guion se quedó sin respuestas preparadas")
        siguiente = self.guion.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente


class TestUrlDeLaEntidad(unittest.TestCase):
    """TALCA_API_URL es la raíz del SITIO; el módulo agrega /api/<entidad>.
    Las cuatro formas en que la variable se puede escribir tienen que dar la
    misma URL: pedirle a alguien que recuerde si va con /api y con barra o
    sin ellas es exactamente cómo apareció el 404 de /clientes."""

    def test_todas_las_formas_de_escribir_la_base_dan_la_misma_url(self):
        esperada = "https://claude.tallerescalificados.cl/api/clientes"
        for base in (
            "https://claude.tallerescalificados.cl",
            "https://claude.tallerescalificados.cl/",
            "https://claude.tallerescalificados.cl/api",
            "https://claude.tallerescalificados.cl/api/",
            "  https://claude.tallerescalificados.cl/api/  ",
        ):
            with self.subTest(base=base):
                self.assertEqual(esperada, sync_talca.url_entidad(base, "clientes"))

    def test_no_se_come_un_segmento_que_solo_empieza_con_api(self):
        self.assertEqual(
            "https://host/apiario/api/clientes",
            sync_talca.url_entidad("https://host/apiario", "clientes"),
        )

    def test_sirve_para_las_entidades_que_vienen_despues(self):
        self.assertEqual(
            "https://host/api/vehiculos",
            sync_talca.url_entidad("https://host", "vehiculos"),
        )


class TestClienteHTTP(unittest.TestCase):
    def _usar_requests_falso(self, respuesta):
        return self._reemplazar_requests(_RequestsFalso(respuesta))

    def _usar_requests_guionado(self, guion):
        return self._reemplazar_requests(_RequestsGuionado(guion))

    def _reemplazar_requests(self, doble):
        original = sync_talca.requests
        sync_talca.requests = doble
        self.addCleanup(lambda: setattr(sync_talca, "requests", original))
        return doble

    def test_arma_la_consulta_segun_el_contrato(self):
        falso = self._usar_requests_falso(_RespuestaFalsa(200, pagina([cliente_talca(1)])))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api/", api_key="secreta", timeout=5)

        api.obtener_clientes(desde="2026-08-06 15:00:00", desde_id=1834, limite=200)

        llamada = falso.llamadas[0]
        self.assertEqual("https://elhosting/api/clientes", llamada["url"])
        self.assertEqual({"limite": 200, "desde": "2026-08-06 15:00:00", "desde_id": 1834}, llamada["params"])
        self.assertEqual({"X-API-Key": "secreta", "User-Agent": "TalcaSync/1.0"}, llamada["headers"])
        self.assertEqual(5.0, llamada["timeout"])

    def test_el_user_agent_propio_viaja_en_todas_las_peticiones(self):
        # Bluehosting corta la conexión si el User-Agent dice
        # "python-requests": no responde 403, cierra el socket. Ver
        # TALCA_USER_AGENT en el módulo.
        falso = self._usar_requests_falso(_RespuestaFalsa(200, pagina([])))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting", api_key="secreta")

        api.obtener_clientes()
        api.obtener_clientes(desde="2026-08-06 15:00:00", desde_id=7)

        self.assertEqual(2, len(falso.llamadas))
        for llamada in falso.llamadas:
            enviado = llamada["headers"]["User-Agent"]
            self.assertEqual(sync_talca.TALCA_USER_AGENT, enviado)
            self.assertNotIn("python-requests", enviado.lower())

    def test_carga_inicial_no_manda_desde_ni_desde_id(self):
        falso = self._usar_requests_falso(_RespuestaFalsa(200, pagina([])))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api", api_key="secreta")

        api.obtener_clientes(limite=200)

        self.assertEqual({"limite": 200}, falso.llamadas[0]["params"])

    def test_desde_id_no_viaja_solo(self):
        # desde_id sin desde no significa nada en el contrato: sería una
        # continuación de página sin página de la cual continuar.
        falso = self._usar_requests_falso(_RespuestaFalsa(200, pagina([])))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api", api_key="secreta")

        api.obtener_clientes(desde=None, desde_id=99, limite=200)

        self.assertEqual({"limite": 200}, falso.llamadas[0]["params"])

    def test_falta_de_configuracion_avisa_que_variable_es(self):
        with self.assertRaises(sync_talca.ClienteTalcaError) as ctx:
            sync_talca.ClienteTalcaHTTP(base_url="", api_key="x").obtener_clientes()
        self.assertIn("TALCA_API_URL", str(ctx.exception))

        with self.assertRaises(sync_talca.ClienteTalcaError) as ctx:
            sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api", api_key="").obtener_clientes()
        self.assertIn("TALCA_API_KEY", str(ctx.exception))

    def test_error_http_no_filtra_la_api_key(self):
        self._usar_requests_falso(
            _RespuestaFalsa(401, {"error": "API key inválida", "codigo": "no_autorizado"})
        )
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api", api_key="ESTA-ES-LA-CLAVE")

        with self.assertRaises(sync_talca.ClienteTalcaError) as ctx:
            api.obtener_clientes()

        mensaje = str(ctx.exception)
        self.assertIn("401", mensaje)
        self.assertIn("no_autorizado", mensaje)
        self.assertNotIn("ESTA-ES-LA-CLAVE", mensaje)

    def test_respuesta_que_no_es_json(self):
        self._usar_requests_falso(_RespuestaFalsa(200, None, texto="<html>error de PHP</html>"))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api", api_key="x")
        with self.assertRaises(sync_talca.ClienteTalcaError) as ctx:
            api.obtener_clientes()
        self.assertIn("no es JSON", str(ctx.exception))

    def test_error_de_red_reintenta_y_al_final_avisa_con_la_url(self):
        guion = [OSError("connection aborted: RemoteDisconnected")] * 3
        falso = self._usar_requests_guionado(guion)
        esperas = []
        api = sync_talca.ClienteTalcaHTTP(
            base_url="https://elhosting", api_key="x", dormir=esperas.append
        )

        with self.assertRaises(sync_talca.ClienteTalcaError) as ctx:
            api.obtener_clientes()

        mensaje = str(ctx.exception)
        self.assertIn("No se pudo conectar", mensaje)
        self.assertIn("https://elhosting/api/clientes", mensaje,
                      "el mensaje tiene que mostrar la URL completa que se intentó")
        self.assertIn("3 intentos", mensaje)
        self.assertEqual(3, len(falso.llamadas))
        self.assertEqual([1, 2], esperas, "esperas crecientes entre reintentos")

    def test_un_corte_puntual_se_reintenta_y_sale_bien(self):
        falso = self._usar_requests_guionado([
            OSError("connection aborted: RemoteDisconnected"),
            _RespuestaFalsa(200, pagina([cliente_talca(1)])),
        ])
        esperas = []
        api = sync_talca.ClienteTalcaHTTP(
            base_url="https://elhosting", api_key="x", dormir=esperas.append
        )

        datos = api.obtener_clientes()

        self.assertEqual(1, len(datos["clientes"]))
        self.assertEqual(2, len(falso.llamadas))
        self.assertEqual([1], esperas)

    def test_los_errores_http_no_se_reintentan(self):
        # Un 401 o un 500 son determinísticos: la respuesta ya llegó y
        # repetirla solo demora el diagnóstico.
        for status in (400, 401, 500):
            with self.subTest(status=status):
                falso = self._usar_requests_guionado([
                    _RespuestaFalsa(status, {"error": "no", "codigo": "x"})
                ])
                api = sync_talca.ClienteTalcaHTTP(
                    base_url="https://elhosting", api_key="x", dormir=self._no_dormir
                )
                with self.assertRaises(sync_talca.ClienteTalcaError):
                    api.obtener_clientes()
                self.assertEqual(1, len(falso.llamadas), "un error HTTP no se reintenta")

    def _no_dormir(self, _segundos):
        raise AssertionError("no debería esperar para reintentar un error HTTP")


# ---------------------------------------------------------------------------
# 9. Vehículos (en Talca `vehiculos`, acá `equipos`)
# ---------------------------------------------------------------------------

def vehiculo_talca(idd, dueno=1, patente="ZN1510", **extra):
    """Una fila tal como la manda la API DE VERDAD: los int(8) de MariaDB
    llegan como STRING porque el driver mysqli de PHP serializa todo como
    texto. Por eso idd, dueno, ano, puertas y kilometraje van entre comillas
    -- si los tests usaran enteros, la traducción del centinela 0 pasaría los
    tests y fallaría contra el servidor real ("0" == 0 es False)."""
    fila = {
        "idd": str(idd),
        "dueno": str(dueno),
        "marca": "VOLVO",
        "modelo": "S60",
        "color": "AZUL",
        "ano": "2007",
        "chasis": "YV1RS595952467912",
        "modelo_transmision": None,
        "puertas": "4",
        "kilometraje": "155177",
        "patente": patente,
        "created_at": "2020-12-28 12:41:02",
        "updated_at": "2026-08-06 15:00:00",
    }
    fila.update(extra)
    return fila


def pagina_vehiculos(vehiculos, hasta="2026-08-06 19:42:11", hay_mas=False,
                     siguiente_desde=None, siguiente_desde_id=None):
    respuesta = {
        "vehiculos": vehiculos,
        "hasta": hasta,
        "hay_mas": hay_mas,
        "total": len(vehiculos),
    }
    if hay_mas:
        respuesta["siguiente_desde"] = siguiente_desde
        respuesta["siguiente_desde_id"] = siguiente_desde_id
    return respuesta


class BaseVehiculos(BaseSyncTalca):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM equipos")
        self.db.execute("DELETE FROM sync_talca_reasignados")
        self.db.execute("DELETE FROM clientes WHERE marcador_sync != ''")
        self.db.commit()

    def sembrar_clientes(self, talca_ids=(1,)):
        """Deja clientes sincronizados, que es la precondición para poder
        sincronizar vehículos."""
        self.sincronizar([pagina([cliente_talca(t) for t in talca_ids])])

    def sincronizar_vehiculos(self, paginas, **kwargs):
        falso = sync_talca.ClienteTalcaFalso(paginas)
        resultado = sync_talca.sincronizar_vehiculos(self.db, cliente_api=falso, **kwargs)
        return resultado, falso

    def equipos_guardados(self):
        return self.db.execute(
            "SELECT * FROM equipos WHERE talca_id IS NOT NULL ORDER BY talca_id"
        ).fetchall()

    def generico(self):
        return self.db.execute(
            "SELECT * FROM clientes WHERE marcador_sync = ?", (sync_talca.MARCADOR_SIN_DUENO,)
        ).fetchone()


class TestVehiculosMigracion(BaseVehiculos):
    def test_columnas_e_indices(self):
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(equipos)").fetchall()]
        for esperada in ("talca_id", "talca_dueno_id", "kilometraje"):
            self.assertIn(esperada, columnas)

        indices = {i[1]: i for i in self.db.execute("PRAGMA index_list(equipos)").fetchall()}
        self.assertIn("idx_equipos_talca_id", indices)
        self.assertEqual(1, indices["idx_equipos_talca_id"][2], "talca_id de equipos tiene que ser ÚNICO")

    def test_solo_puede_haber_un_cliente_marcado(self):
        # El índice único parcial convierte "no crear un genérico nuevo en
        # cada corrida" en un invariante de la base, no una convención.
        self.db.execute(
            "INSERT INTO clientes (nombre, creado_en, marcador_sync) VALUES ('A', '2026-01-01T00:00:00', 'sin_dueno_talca')"
        )
        self.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO clientes (nombre, creado_en, marcador_sync) VALUES ('B', '2026-01-01T00:00:00', 'sin_dueno_talca')"
            )
        self.db.rollback()

    def test_muchos_clientes_sin_marcador_conviven(self):
        # El índice es PARCIAL: los miles de clientes normales tienen
        # marcador_sync = '' y no se estorban entre ellos.
        self.sembrar_clientes((1, 2, 3))
        self.assertEqual(
            3,
            self.db.execute(
                "SELECT COUNT(*) FROM clientes WHERE marcador_sync = '' AND talca_id IS NOT NULL"
            ).fetchone()[0],
        )


class TestVehiculosOrden(BaseVehiculos):
    def test_sin_clientes_sincronizados_se_niega_a_correr(self):
        resultado, falso = self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1)])])

        self.assertEqual("error", resultado["resultado"])
        self.assertIn("sync_talca clientes", resultado["detalle"])
        self.assertEqual([], falso.llamadas, "no debe ni pedir la primera página")
        self.assertEqual([], self.equipos_guardados())

    def test_con_clientes_sincronizados_corre(self):
        self.sembrar_clientes((1,))
        resultado, falso = self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1, dueno=1)])])

        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual("vehiculos", falso.llamadas[0]["entidad"])


class TestVehiculosMapeo(BaseVehiculos):
    def setUp(self):
        super().setUp()
        self.sembrar_clientes((1,))
        self.cliente_id = self.clientes_guardados()[0]["id"]

    def test_mapea_los_campos_de_talca(self):
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(7, dueno=1, patente="ZN1510", marca="VOLVO", modelo="S60",
                           color="AZUL", ano="2007", chasis="YV1RS595952467912",
                           modelo_transmision="AW55-50", puertas="4", kilometraje="155177"),
        ])])

        equipo = self.equipos_guardados()[0]
        self.assertEqual(7, equipo["talca_id"])
        self.assertEqual(self.cliente_id, equipo["cliente_id"])
        self.assertEqual(1, equipo["talca_dueno_id"])
        self.assertEqual("ZN1510", equipo["identificador"])   # patente -> identificador
        self.assertEqual(2007, equipo["anio"])                # ano -> anio, ya como entero
        self.assertEqual("YV1RS595952467912", equipo["vin"])  # chasis -> vin
        self.assertEqual("VOLVO", equipo["marca"])
        self.assertEqual("S60", equipo["modelo"])
        self.assertEqual("AZUL", equipo["color"])
        self.assertEqual("AW55-50", equipo["modelo_transmision"])
        self.assertEqual("4", equipo["puertas"])
        self.assertEqual(155177, equipo["kilometraje"])

    def test_los_ceros_llegan_como_texto_y_se_traducen(self):
        # EL test del ajuste técnico. La API manda "0", no 0. Comparar contra
        # el entero dejaría 331 años, 624 puertas y 1.060 odómetros en cero
        # entrando tal cual, y el sistema mostraría "año 0, 0 puertas, 0 km".
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, ano="0", puertas="0", kilometraje="0"),
        ])])

        equipo = self.equipos_guardados()[0]
        self.assertIsNone(equipo["anio"], "ano '0' es ausencia de dato, no el año cero")
        self.assertEqual("", equipo["puertas"], "puertas es TEXT: el centinela va a cadena vacía")
        self.assertIsNone(equipo["kilometraje"], "0 km en un taller de transmisiones es 'no se anotó'")

    def test_los_valores_reales_no_se_tocan(self):
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, ano="2007", puertas="5", kilometraje="1"),
        ])])
        equipo = self.equipos_guardados()[0]
        self.assertEqual(2007, equipo["anio"])
        self.assertEqual("5", equipo["puertas"])
        self.assertEqual(1, equipo["kilometraje"], "1 km es un valor, no un centinela")

    def test_los_anos_absurdos_se_importan_tal_cual(self):
        # Un ano en 1 o en 20255 es error de tipeo del taller. Corregirlo
        # sería inventar: se copia como está y que lo arreglen en Talca.
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, ano="1"), vehiculo_talca(2, ano="20255"),
        ])])
        self.assertEqual([1, 20255], [e["anio"] for e in self.equipos_guardados()])

    def test_nulls_y_espacios(self):
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, modelo_transmision=None, color="", chasis="",
                           marca="LAND  ROVER ", patente="FGBB83 "),
        ])])
        equipo = self.equipos_guardados()[0]
        self.assertEqual("", equipo["modelo_transmision"])
        self.assertEqual("", equipo["color"])
        self.assertEqual("", equipo["vin"])
        self.assertEqual("LAND ROVER", equipo["marca"])
        self.assertEqual("FGBB83", equipo["identificador"])

    def test_created_at_imposible(self):
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, created_at="0000-00-00 00:00:00"),
        ])])
        self.assertFalse(self.equipos_guardados()[0]["creado_en"].startswith("0000"))

    def test_no_pisa_los_campos_locales(self):
        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1)])])
        self.db.execute(
            "UPDATE equipos SET notas = ?, motor_cc = ? WHERE talca_id = 1",
            ("Cliente pidió revisar el convertidor", "1998"),
        )
        self.db.commit()

        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1, kilometraje="160000")])])

        equipo = self.equipos_guardados()[0]
        self.assertEqual(160000, equipo["kilometraje"])
        self.assertEqual("Cliente pidió revisar el convertidor", equipo["notas"])
        self.assertEqual("1998", equipo["motor_cc"], "motor_cc no existe en Talca: es local")


class TestVehiculosLlave(BaseVehiculos):
    def setUp(self):
        super().setUp()
        self.sembrar_clientes((1,))

    def test_la_patente_no_es_llave(self):
        # CXMANO aparece 29 veces en el dump con dueños distintos: es el
        # comodín para trabajos de caja de cambios, no un auto.
        self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, patente="CXMANO"),
            vehiculo_talca(2, patente="CXMANO"),
            vehiculo_talca(3, patente="CXMANO"),
        ])])
        self.assertEqual(3, len(self.equipos_guardados()))

    def test_cambiar_la_patente_no_crea_un_equipo_nuevo(self):
        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1, patente="ZN1510")])])
        id_local = self.equipos_guardados()[0]["id"]

        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1, patente="BBCC11")])])

        equipos = self.equipos_guardados()
        self.assertEqual(1, len(equipos))
        self.assertEqual(id_local, equipos[0]["id"])
        self.assertEqual("BBCC11", equipos[0]["identificador"])

    def test_las_patentes_fuera_de_formato_se_importan(self):
        # No son basura como los clientes de gastos: están enganchadas a OT
        # reales, y omitirlas dejaría esas órdenes sin vehículo.
        resultado, _ = self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, patente="CXMANO"), vehiculo_talca(2, patente="VENTA"),
            vehiculo_talca(3, patente="GASTOSGEN"), vehiculo_talca(4, patente="SIN/AUTO"),
        ])])
        self.assertEqual(4, resultado["creadas"])
        self.assertEqual(0, resultado["omitidas"])

    def test_la_fila_vacia_se_omite(self):
        # Hoy no existe ninguna en las 5.103 filas del dump; la comprobación
        # queda como guardia para el día que aparezca.
        resultado, _ = self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, marca="", modelo="", color="", chasis="",
                           modelo_transmision=None, patente="", ano="0",
                           puertas="0", kilometraje="0"),
            vehiculo_talca(2),
        ])])
        self.assertEqual(1, resultado["creadas"])
        self.assertEqual({"fila_vacia": 1}, resultado["omitidas_por_motivo"])


class TestVehiculosSinDueno(BaseVehiculos):
    def setUp(self):
        super().setUp()
        self.sembrar_clientes((1,))

    def test_dueno_inexistente_va_al_generico_y_queda_registrado(self):
        resultado, _ = self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(1, dueno=1),
            vehiculo_talca(2, dueno=999),   # 999 no existe en clientes
        ])])

        self.assertEqual(2, resultado["creadas"])
        self.assertEqual({"dueno_inexistente": 1}, resultado["reasignados_por_motivo"])

        generico = self.generico()
        self.assertIsNotNone(generico, "el genérico se crea cuando hace falta")
        self.assertIsNone(generico["talca_id"], "el genérico no existe en Talca")

        huerfano = self.db.execute("SELECT * FROM equipos WHERE talca_id = 2").fetchone()
        self.assertEqual(generico["id"], huerfano["cliente_id"])
        self.assertEqual(999, huerfano["talca_dueno_id"], "se recuerda qué dice Talca")

        registro = self.db.execute(
            "SELECT * FROM sync_talca_reasignados WHERE entidad = 'vehiculos' AND talca_id = 2"
        ).fetchone()
        self.assertEqual("dueno_inexistente", registro["motivo"])
        self.assertEqual(999, registro["referencia_talca_id"])
        self.assertEqual("", registro["resuelto_en"], "sigue pendiente")

    def test_sin_huerfanos_no_aparece_ningun_cliente_fantasma(self):
        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(1, dueno=1)])])
        self.assertIsNone(self.generico(), "no se crea un genérico si nadie lo necesita")

    def test_el_generico_se_crea_una_sola_vez(self):
        for _ in range(3):
            self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(2, dueno=999)])])
        self.assertEqual(
            1,
            self.db.execute(
                "SELECT COUNT(*) FROM clientes WHERE marcador_sync = ?", (sync_talca.MARCADOR_SIN_DUENO,)
            ).fetchone()[0],
        )

    def test_el_generico_se_encuentra_aunque_lo_renombren(self):
        # El taller lo va a renombrar apenas lo vea en pantalla. Por eso se
        # busca por marcador_sync y no por nombre.
        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(2, dueno=999)])])
        self.db.execute(
            "UPDATE clientes SET nombre = 'AUTOS SIN DUEÑO (revisar)' WHERE marcador_sync = ?",
            (sync_talca.MARCADOR_SIN_DUENO,),
        )
        self.db.commit()

        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(3, dueno=998)])])

        self.assertEqual(
            1,
            self.db.execute(
                "SELECT COUNT(*) FROM clientes WHERE marcador_sync = ?", (sync_talca.MARCADOR_SIN_DUENO,)
            ).fetchone()[0],
        )

    def test_dueno_omitido_es_una_guardia_que_hoy_no_debe_dispararse(self):
        # El motivo existía cuando el filtro descartaba los clientes de
        # gastos. Ahora esos clientes se importan, así que en producción esto
        # tiene que dar cero -- si aparece uno, cambiaron las reglas de
        # filtrado y hay que mirarlo. Acá se fuerza el escenario a mano para
        # comprobar que el mecanismo sigue distinguiendo los dos casos.
        self.db.execute(
            """
            INSERT INTO sync_talca_omitidos (entidad, talca_id, motivo, datos_json, visto_en)
            VALUES ('clientes', 362, 'cliente_ficticio', '{}', '2026-08-06T10:00:00')
            """
        )
        self.db.commit()

        resultado, _ = self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(5, dueno=362),   # dueño omitido por el filtro
            vehiculo_talca(6, dueno=999),   # dueño que no existe en Talca
        ])])

        self.assertEqual(
            {"dueno_omitido": 1, "dueno_inexistente": 1},
            resultado["reasignados_por_motivo"],
        )

    def test_el_vehiculo_se_reengancha_solo_cuando_aparece_el_dueno(self):
        # El caso que justifica guardar talca_dueno_id: el que cambia es el
        # CLIENTE, así que el vehículo NO vuelve a llegar por el pull.
        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(2, dueno=999)])])
        generico = self.generico()
        self.assertEqual(generico["id"],
                         self.db.execute("SELECT cliente_id FROM equipos WHERE talca_id = 2").fetchone()[0])

        # Alguien recrea al cliente en Talca y llega en la corrida siguiente
        # de CLIENTES (el vehículo no se toca).
        resultado = self.sincronizar([pagina([cliente_talca(999, nombre="DUEÑO RECUPERADO")])])[0]

        self.assertEqual(1, resultado["reenganchados"])
        equipo = self.db.execute("SELECT * FROM equipos WHERE talca_id = 2").fetchone()
        cliente = self.db.execute("SELECT id FROM clientes WHERE talca_id = 999").fetchone()
        self.assertEqual(cliente["id"], equipo["cliente_id"])
        self.assertNotEqual(generico["id"], equipo["cliente_id"])

        registro = self.db.execute(
            "SELECT resuelto_en FROM sync_talca_reasignados WHERE talca_id = 2"
        ).fetchone()
        self.assertTrue(registro["resuelto_en"], "queda estampado cuándo se resolvió")

    def test_el_reenganche_tambien_corre_en_la_sincronizacion_de_vehiculos(self):
        self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(2, dueno=999)])])
        self.db.execute(
            "INSERT INTO clientes (talca_id, nombre, creado_en) VALUES (999, 'APARECIO', '2026-01-01T00:00:00')"
        )
        self.db.commit()

        resultado, _ = self.sincronizar_vehiculos([pagina_vehiculos([vehiculo_talca(9, dueno=1)])])

        self.assertEqual(1, resultado["reenganchados"])


# ---------------------------------------------------------------------------
# 10. Órdenes de trabajo (en Talca `orden_trabajo`, acá `ordenes_trabajo`)
# ---------------------------------------------------------------------------
# Lo que se cubre acá es lo que, si se rompe en silencio, deja al taller
# mirando datos falsos sin ningún error de por medio:
#
#   - que el estado de Talca no pise el avance del tablero cuando Talca no
#     tiene forma de expresarlo (su ABIERTA no es evidencia contra
#     'reparacion');
#   - que las tres cosas que `orden_trabajo` mezcla -- órdenes, garantías y OT
#     de conteo -- entren todas y queden distinguibles;
#   - que la patente salga de la rama correcta, porque hay dos épocas de datos
#     y 418 filas donde la columna `vehiculo` no es una placa;
#   - que las 88 órdenes sin cliente y las 15 sin vehículo entren igual y se
#     reenganchen solas;
#   - que una orden que ya existía se ADOPTE en vez de duplicarse, y que no se
#     adopte cuando el cliente no calza (legacy_ot_no lo escribió un backfill
#     por patente, y hay 285 patentes repetidas).

def orden_talca(id_, id_dueno=1, id_vehiculo=1, estado="ABIERTA", tipo="Nueva Orden", **extra):
    """Una fila de `orden_trabajo` como la manda la API de verdad: los enteros
    llegan como STRING (driver mysqli), igual que en vehículos."""
    fila = {
        "id": str(id_),
        "id_dueno": str(id_dueno),
        "id_vehiculo": str(id_vehiculo),
        "nombre": "JUAN PEREZ",
        "patente": "ZN1510",
        "vehiculo": "",
        "marca": "VOLVO",
        "modelo": "S60",
        "color": "AZUL",
        "estado": estado,
        "tipo": tipo,
        "tnr": "",
        "requerimiento": "CAMBIO DE ACEITE DE CAJA",
        "precio": "450000",
        "coniva": "1",
        "kilometraje": "155177",
        "estanque": "3",
        "origen": "GOOGLE",
        "obs_presupuesto": "",
        "apro": "",
        "observaciones": "",
        "glosa": "",
        "glosa_obs": "",
        "fecha_salida": "2026-08-05 17:30:00",
        "fecha_cierre": "2026-08-05 16:00:00",
        "isDeleted": "0",
        "createdDtm": "2026-08-01 09:15:00",
        "updatedDtm": "2026-08-06 15:00:00",
    }
    fila.update(extra)
    return fila


def pagina_ordenes(ordenes, hasta="2026-08-06 19:42:11", hay_mas=False,
                   siguiente_desde=None, siguiente_desde_id=None):
    respuesta = {
        "ordenes": ordenes,
        "hasta": hasta,
        "hay_mas": hay_mas,
        "total": len(ordenes),
    }
    if hay_mas:
        respuesta["siguiente_desde"] = siguiente_desde
        respuesta["siguiente_desde_id"] = siguiente_desde_id
    return respuesta


class BaseOrdenes(BaseVehiculos):
    def setUp(self):
        # Las órdenes se borran ANTES que los clientes y los equipos: sus
        # llaves foráneas no tienen ON DELETE CASCADE, así que la limpieza de
        # BaseSyncTalca fallaría con una orden colgando.
        conexion = taller_app._conectar_db()
        try:
            conexion.execute("DELETE FROM ordenes_trabajo")
            conexion.commit()
        finally:
            conexion.close()
        super().setUp()
        self.db.execute("DELETE FROM sync_talca_adopciones")
        self.db.commit()

    def sembrar(self, clientes=(1,), vehiculos=(1,)):
        """Deja clientes y vehículos sincronizados, que es la precondición
        para poder sincronizar órdenes."""
        self.sembrar_clientes(clientes)
        if vehiculos:
            self.sincronizar_vehiculos([pagina_vehiculos(
                [vehiculo_talca(v, dueno=clientes[0]) for v in vehiculos]
            )])

    def sincronizar_ordenes(self, paginas, **kwargs):
        falso = sync_talca.ClienteTalcaFalso(paginas)
        resultado = sync_talca.sincronizar_ordenes(self.db, cliente_api=falso, **kwargs)
        return resultado, falso

    def ordenes_guardadas(self):
        return self.db.execute(
            "SELECT * FROM ordenes_trabajo WHERE talca_id IS NOT NULL ORDER BY talca_id"
        ).fetchall()

    def orden(self, talca_id):
        return self.db.execute(
            "SELECT * FROM ordenes_trabajo WHERE talca_id = ?", (talca_id,)
        ).fetchone()


class TestOrdenesMigracion(BaseOrdenes):
    def test_columnas_e_indices(self):
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
        for esperada in ("talca_id", "talca_dueno_id", "talca_vehiculo_id", "patente",
                         "cliente_nombre_talca", "estado_talca", "tipo_talca",
                         "trabajo_no_realizado", "borrada_en_talca", "fecha_cierre_talca",
                         "observaciones_talca", "garantia_ot_origen_talca", "datos_talca_json"):
            self.assertIn(esperada, columnas)

        indices = {i[1]: i for i in self.db.execute("PRAGMA index_list(ordenes_trabajo)").fetchall()}
        self.assertIn("idx_ordenes_talca_id", indices)
        self.assertEqual(1, indices["idx_ordenes_talca_id"][2],
                         "talca_id de ordenes_trabajo tiene que ser ÚNICO")

    def test_tabla_de_adopciones(self):
        self.assertIsNotNone(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sync_talca_adopciones'"
            ).fetchone()
        )

    def test_correrla_de_nuevo_no_falla(self):
        sync_talca.init_sync_talca_db(self.db)
        sync_talca.init_sync_talca_db(self.db)
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
        self.assertEqual(1, columnas.count("talca_id"))

    def test_legacy_ot_no_sigue_existiendo(self):
        # Se conserva a propósito: significa "el número de OT en Talca" y por
        # ahí pasan los endpoints de pagos e historial, además de la adopción.
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
        self.assertIn("legacy_ot_no", columnas)


class TestOrdenesOrden(BaseOrdenes):
    def test_sin_clientes_sincronizados_se_niega(self):
        resultado, falso = self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])])
        self.assertEqual("error", resultado["resultado"])
        self.assertIn("sync_talca clientes", resultado["detalle"])
        self.assertEqual([], falso.llamadas, "no debe ni pedir la primera página")

    def test_sin_vehiculos_sincronizados_se_niega(self):
        self.sembrar_clientes((1,))
        resultado, falso = self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])])
        self.assertEqual("error", resultado["resultado"])
        self.assertIn("sync_talca vehiculos", resultado["detalle"])
        self.assertEqual([], falso.llamadas)

    def test_con_las_dos_corre(self):
        self.sembrar()
        resultado, falso = self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])])
        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual("ordenes", falso.llamadas[0]["entidad"])


class TestOrdenesMapeo(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()
        self.cliente_id = self.clientes_guardados()[0]["id"]
        self.equipo_id = self.equipos_guardados()[0]["id"]

    def test_mapea_los_campos(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(5710)])])

        fila = self.orden(5710)
        self.assertEqual(self.cliente_id, fila["cliente_id"])
        self.assertEqual(self.equipo_id, fila["equipo_id"])
        self.assertEqual(1, fila["talca_dueno_id"])
        self.assertEqual(1, fila["talca_vehiculo_id"])
        self.assertEqual("CAMBIO DE ACEITE DE CAJA", fila["trabajo_a_realizar"])
        self.assertEqual(450000, fila["precio_referencial"])
        self.assertEqual(1, fila["precio_iva"])
        self.assertEqual(155177, fila["kilometraje"])
        self.assertEqual(3, fila["estanque_nivel"])
        self.assertEqual("GOOGLE", fila["referencia"])
        self.assertEqual("ZN1510", fila["patente"])
        self.assertEqual("JUAN PEREZ", fila["cliente_nombre_talca"])
        self.assertEqual("ABIERTA", fila["estado_talca"])
        self.assertEqual("Nueva Orden", fila["tipo_talca"])
        self.assertEqual("2026-08-01T09:15:00", fila["fecha_recepcion"])
        self.assertEqual("2026-08-06T15:00:00", fila["actualizado_en"])
        self.assertEqual("2026-08-05T17:30:00", fila["fecha_entrega_real"])
        self.assertEqual("2026-08-05T16:00:00", fila["fecha_cierre_talca"])
        self.assertEqual(5710, fila["legacy_ot_no"], "legacy_ot_no es el número de OT en Talca")

    def test_el_estanque_no_se_valida_contra_1_a_5(self):
        # El dueño del sistema lo describió como 1 a 5, pero en los datos va
        # de 0 a 10: 322 OT fuera de ese rango.
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, estanque="10"), orden_talca(2, estanque="7"),
        ])])
        self.assertEqual([10, 7], [self.orden(t)["estanque_nivel"] for t in (1, 2)])

    def test_los_ceros_de_precio_y_estanque_son_dato(self):
        # Una garantía cuesta $0 de verdad (1.247 de las 1.440), y estanque 0
        # es el estanque vacío en el medidor (41 OT). Traducirlos a NULL
        # borraría dato real.
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, precio="0", estanque="0", kilometraje="0"),
        ])])
        fila = self.orden(1)
        self.assertEqual(0, fila["precio_referencial"])
        self.assertEqual(0, fila["estanque_nivel"])
        self.assertIsNone(fila["kilometraje"],
                          "el kilometraje SÍ es centinela: nadie llega con 0 km")

    def test_fechas_imposibles(self):
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, createdDtm="0000-00-00 00:00:00",
                        fecha_salida="0000-00-00 00:00:00", fecha_cierre=""),
        ])])
        fila = self.orden(1)
        self.assertFalse(fila["fecha_recepcion"].startswith("0000"))
        self.assertIsNone(fila["fecha_entrega_real"], "sin salida es un hecho, no una fecha inventada")
        self.assertIsNone(fila["fecha_cierre_talca"])

    def test_updated_dtm_en_cero_cae_a_created_dtm(self):
        # Las 15 órdenes que nacieron con updatedDtm = '0000-00-00 00:00:00'
        # porque addNewUser no escribía la columna. Ya está corregido con un
        # trigger del lado de Talca; el respaldo se queda porque acá
        # actualizado_en es NOT NULL.
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(2671, createdDtm="2021-09-14 11:02:00",
                        updatedDtm="0000-00-00 00:00:00"),
        ])])
        self.assertEqual("2021-09-14T11:02:00", self.orden(2671)["actualizado_en"])

    def test_el_json_crudo_guarda_los_campos_sin_columna(self):
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, glosa="CXMANO", glosa_obs="POLITICA DE ENTREGA", motor="2.000"),
        ])])
        crudo = json.loads(self.orden(1)["datos_talca_json"])
        self.assertEqual("CXMANO", crudo["glosa"])
        self.assertEqual("POLITICA DE ENTREGA", crudo["glosa_obs"])
        self.assertEqual("2.000", crudo["motor"])

    def test_el_json_se_regenera_completo_y_no_se_fusiona(self):
        # Si se fusionara, un campo borrado en Talca sobreviviría acá para
        # siempre y el JSON dejaría de ser un espejo fiel.
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, glosa="CXMANO")])])
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, glosa="")])])
        self.assertEqual("", json.loads(self.orden(1)["datos_talca_json"])["glosa"])

    def test_no_copia_marca_ni_modelo_a_la_orden(self):
        # Difieren del equipo en 8 y 6 casos, y varias de esas "marcas" son
        # números (109, 97, 80): son datos corruptos, no divergencias
        # intencionales como la patente. El equipo es la mejor fuente.
        columnas = [c[1] for c in self.db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
        for muerta in ("marca", "modelo", "color", "situacion",
                       "nombre_aprobacion", "fecha_aprobacion"):
            self.assertNotIn(muerta, columnas)

    def test_no_pisa_los_campos_locales(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])])
        self.db.execute(
            """
            UPDATE ordenes_trabajo
               SET diagnostico_tecnico = ?, qc_inspector = ?, mecanico = ?,
                   prioridad = ?, recepcionado_por = ?
             WHERE talca_id = 1
            """,
            ("Convertidor con juego axial", "PATRICIO", "LUIS", "alta", "CAROLINA"),
        )
        self.db.commit()

        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, precio="600000")])])

        fila = self.orden(1)
        self.assertEqual(600000, fila["precio_referencial"], "lo de Talca sí se actualiza")
        self.assertEqual("Convertidor con juego axial", fila["diagnostico_tecnico"])
        self.assertEqual("PATRICIO", fila["qc_inspector"])
        self.assertEqual("LUIS", fila["mecanico"])
        self.assertEqual("alta", fila["prioridad"])
        self.assertEqual("CAROLINA", fila["recepcionado_por"])


class TestOrdenesEstados(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()

    def _estado(self, **extra):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, **extra)])])
        return self.orden(1)

    def test_los_cuatro_estados_conocidos(self):
        self.assertEqual("recibido", self._estado(estado="ABIERTA")["estado"])
        self.assertEqual("entregado", self._estado(estado="CERRADA")["estado"])
        self.assertEqual("entregado", self._estado(estado="CERRADA BLOQUEADA")["estado"])
        self.assertEqual("cancelado", self._estado(estado="PRESUPUESTO NO ACEPTADO")["estado"])

    def test_un_estado_futuro_no_deja_la_orden_sin_estado(self):
        # `estados_ot` tiene 2 filas y editNota escribe otros dos a mano: la
        # tabla de estados no es la fuente de verdad, así que el mapeo
        # necesita rama por defecto.
        fila = self._estado(estado="EN ESPERA DE REPUESTO IMPORTADO")
        self.assertEqual("recibido", fila["estado"])
        self.assertEqual("EN ESPERA DE REPUESTO IMPORTADO", fila["estado_talca"],
                         "el literal crudo se guarda igual")

    def test_el_estado_crudo_se_conserva_siempre(self):
        self.assertEqual("CERRADA BLOQUEADA", self._estado(estado="CERRADA BLOQUEADA")["estado_talca"])

    def test_tnr_es_cancelado_y_no_entregado(self):
        # 49 órdenes cerradas SIN trabajo. Mandarlas a 'entregado' las haría
        # contar como trabajos realizados en las métricas.
        fila = self._estado(estado="CERRADA", tnr="TNR")
        self.assertEqual("cancelado", fila["estado"])
        self.assertEqual(1, fila["trabajo_no_realizado"])
        self.assertEqual("CERRADA", fila["estado_talca"])

    def test_sin_tnr_la_bandera_queda_en_cero(self):
        self.assertEqual(0, self._estado(estado="CERRADA")["trabajo_no_realizado"])
        self.assertEqual(0, self._estado(estado="CERRADA", tnr=None)["trabajo_no_realizado"])

    def test_las_borradas_se_importan_canceladas(self):
        # Se importan a propósito: si se filtraran, una orden borrada DESPUÉS
        # de haber sido importada quedaría viva acá para siempre, porque el
        # borrado lógico llega como un update normal.
        fila = self._estado(estado="ABIERTA", isDeleted="1")
        self.assertEqual("cancelado", fila["estado"])
        self.assertEqual(1, fila["borrada_en_talca"])

    def test_el_borrado_se_propaga_a_una_orden_ya_importada(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, estado="ABIERTA")])])
        self.assertEqual(0, self.orden(1)["borrada_en_talca"])

        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, estado="ABIERTA", isDeleted="1")])])

        fila = self.orden(1)
        self.assertEqual(1, fila["borrada_en_talca"])
        self.assertEqual("cancelado", fila["estado"])


class TestOrdenesNoDegradanElFlujo(BaseOrdenes):
    """La regla que evita que editar una OT en Talca borre el avance del
    tablero. Talca no modela el flujo interno del taller: no tiene forma de
    decir 'reparacion' ni 'listo', así que su ABIERTA no es evidencia en
    contra del estado que se puso acá."""

    def setUp(self):
        super().setUp()
        self.sembrar()

    def test_abierta_no_devuelve_una_orden_en_reparacion_a_recibido(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, estado="ABIERTA")])])
        self.db.execute("UPDATE ordenes_trabajo SET estado = 'reparacion' WHERE talca_id = 1")
        # El UPDATE de arriba dispara trg_ordenes_push_pendiente. Estos tests
        # prueban las reglas del PULL, no la interacción con el push, así que
        # se apaga el flag para no estar probando otra cosa.
        self.db.execute("UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE talca_id = 1")
        self.db.commit()

        # Alguien edita el kilometraje en Talca: la orden vuelve por el pull.
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, estado="ABIERTA", kilometraje="160000", precio="700000"),
        ])])

        fila = self.orden(1)
        self.assertEqual("reparacion", fila["estado"], "el avance del tablero no se pierde")
        self.assertEqual(160000, fila["kilometraje"], "todo lo demás sí se actualiza")
        self.assertEqual(700000, fila["precio_referencial"])

    def test_un_estado_futuro_tampoco_degrada(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])])
        self.db.execute("UPDATE ordenes_trabajo SET estado = 'control_calidad' WHERE talca_id = 1")
        # El UPDATE de arriba dispara trg_ordenes_push_pendiente. Estos tests
        # prueban las reglas del PULL, no la interacción con el push, así que
        # se apaga el flag para no estar probando otra cosa.
        self.db.execute("UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE talca_id = 1")
        self.db.commit()

        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, estado="VALOR NUEVO")])])
        self.assertEqual("control_calidad", self.orden(1)["estado"])

    def test_los_estados_terminales_de_talca_si_mandan(self):
        # CERRADA es un hecho sobre la orden, no una ausencia de vocabulario.
        for estado_talca, esperado in (("CERRADA", "entregado"),
                                       ("CERRADA BLOQUEADA", "entregado"),
                                       ("PRESUPUESTO NO ACEPTADO", "cancelado")):
            with self.subTest(estado_talca=estado_talca):
                self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, estado="ABIERTA")])])
                self.db.execute("UPDATE ordenes_trabajo SET estado = 'reparacion' WHERE talca_id = 1")
                # El UPDATE de arriba dispara trg_ordenes_push_pendiente. Estos tests
                # prueban las reglas del PULL, no la interacción con el push, así que
                # se apaga el flag para no estar probando otra cosa.
                self.db.execute("UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE talca_id = 1")
                self.db.commit()

                self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, estado=estado_talca)])])
                self.assertEqual(esperado, self.orden(1)["estado"])

    def test_apro_si_aprueba_el_presupuesto(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, apro="SI")])])
        self.assertEqual("aprobado", self.orden(1)["presupuesto_estado"])

    def test_apro_vacio_no_degrada_un_presupuesto_aprobado_aca(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, apro="")])])
        self.assertEqual("pendiente", self.orden(1)["presupuesto_estado"])

        self.db.execute("UPDATE ordenes_trabajo SET presupuesto_estado = 'aprobado' WHERE talca_id = 1")
        self.db.commit()

        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, apro="")])])
        self.assertEqual("aprobado", self.orden(1)["presupuesto_estado"],
                         "Talca no sabe expresar el flujo de presupuesto de este sistema")


class TestOrdenesPatente(BaseOrdenes):
    """`orden_trabajo` tiene dos épocas, con corte el 2024-09-25: las 4.710
    órdenes viejas traen la placa en la columna `vehiculo` (en minúsculas en
    4.337) y `patente` vacía. Pero en 418 de esas filas `vehiculo` no es una
    placa sino una etiqueta administrativa."""

    def setUp(self):
        super().setUp()
        self.sembrar()

    def _patente(self, **extra):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, **extra)])])
        return self.orden(1)["patente"]

    def test_rama_1_la_columna_patente_manda(self):
        self.assertEqual("BBCC11", self._patente(patente="BBCC11", vehiculo="zn1510"))

    def test_rama_2_el_campo_vehiculo_si_tiene_forma_de_patente(self):
        self.assertEqual("ZN1510", self._patente(patente="", vehiculo="zn1510"),
                         "4.337 OT viejas la guardaron en minúsculas")
        self.assertEqual("FGBB83", self._patente(patente="", vehiculo="fgbb83"))

    def test_rama_3_las_etiquetas_administrativas_no_son_patente(self):
        # 418 filas: venta, gastos gen, sept.21, admon marz, un punto suelto,
        # un chasis completo. Se cae al equipo en vez de inventar una placa.
        for etiqueta in ("sept.21", "gastos gen", "venta", "admon marz", ".",
                         "vf7rcxfuj5l505789"):
            with self.subTest(etiqueta=etiqueta):
                self.assertEqual("ZN1510", self._patente(patente="", vehiculo=etiqueta),
                                 "sale del equipo, no de la etiqueta")

    def test_rama_4_sin_nada_queda_vacia(self):
        self.assertEqual("", self._patente(patente="", vehiculo="sept.21", id_vehiculo="0"))

    def test_el_valor_crudo_nunca_se_pierde(self):
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, patente="", vehiculo="sept.21"),
        ])])
        self.assertEqual("sept.21", json.loads(self.orden(1)["datos_talca_json"])["vehiculo"])

    def test_el_reparto_por_rama_se_informa(self):
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, patente="BBCC11"),
            orden_talca(2, patente="", vehiculo="zn1510"),
            orden_talca(3, patente="", vehiculo="sept.21"),
            orden_talca(4, patente="", vehiculo="gastos gen", id_vehiculo="0"),
        ])])
        self.assertEqual(
            {"columna_patente": 1, "columna_vehiculo": 1, "equipo": 1, "sin_patente": 1},
            resultado["patentes_por_rama"],
        )

    def test_la_patente_de_la_orden_no_se_deriva_del_equipo(self):
        # 14 OT tienen una patente distinta a la de su vehículo, y esas
        # divergencias son INTENCIONALES: el dueño del sistema cambió el
        # modelo para poder corregir la patente de una OT sin tocar la del
        # auto.
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, patente="XXYY99")])])
        self.assertEqual("XXYY99", self.orden(1)["patente"])
        self.assertEqual("ZN1510", self.equipos_guardados()[0]["identificador"],
                         "el equipo conserva la suya")


class TestOrdenesTipos(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()

    def test_las_tres_cosas_entran_a_la_misma_tabla(self):
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, tipo="Nueva Orden"),
            orden_talca(2, tipo="Garantia"),
            orden_talca(3, tipo="Inventario"),
        ])])
        self.assertEqual(3, resultado["creadas"])
        self.assertEqual(0, resultado["omitidas"])
        self.assertEqual(["Nueva Orden", "Garantia", "Inventario"],
                         [o["tipo_talca"] for o in self.ordenes_guardadas()])

    def test_las_garantias_no_van_al_modulo_de_garantias(self):
        antes = self.db.execute("SELECT COUNT(*) FROM garantias").fetchone()[0]
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(2, tipo="Garantia")])])
        self.assertEqual(antes, self.db.execute("SELECT COUNT(*) FROM garantias").fetchone()[0])

    def test_la_ot_de_origen_de_una_garantia_se_rescata_del_texto_libre(self):
        # Solo 637 de las 1.440 la tienen, y está dentro de `observaciones`.
        # Se registra para que, si algún día se separan las garantías, no haya
        # que volver a parsear texto libre.
        self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, tipo="Garantia", observaciones="GARANTIA OT. 1160"),
            orden_talca(2, tipo="Garantia", observaciones="garantía ot 5709 revisar caja"),
            orden_talca(3, tipo="Garantia", observaciones="CLIENTE RECLAMA RUIDO"),
        ])])
        self.assertEqual(1160, self.orden(1)["garantia_ot_origen_talca"])
        self.assertEqual(5709, self.orden(2)["garantia_ot_origen_talca"])
        self.assertIsNone(self.orden(3)["garantia_ot_origen_talca"],
                          "el 56% no la tiene en ninguna parte")

    def test_las_observaciones_se_guardan_enteras(self):
        largo = "Talleres Calificados SpA se complace en notificar que hoy " * 3
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, observaciones=largo)])])
        self.assertEqual(largo.strip(), self.orden(1)["observaciones_talca"])


class TestOrdenesHuerfanas(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()

    def test_dueno_inexistente_va_al_generico(self):
        # 85 de las 7.116. El nombre real no se pierde.
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, id_dueno="999", nombre="MASAUTOS SPA"),
        ])])
        self.assertEqual({"dueno_inexistente": 1}, resultado["reasignados_por_motivo"])

        fila = self.orden(1)
        self.assertEqual(self.generico()["id"], fila["cliente_id"])
        self.assertEqual(999, fila["talca_dueno_id"])
        self.assertEqual("MASAUTOS SPA", fila["cliente_nombre_talca"],
                         "el único rastro de quién era")

        registro = self.db.execute(
            "SELECT * FROM sync_talca_reasignados WHERE entidad = 'ordenes' AND talca_id = 1"
        ).fetchone()
        self.assertEqual("dueno_inexistente", registro["motivo"])
        self.assertEqual("", registro["resuelto_en"])

    def test_id_dueno_en_cero_se_distingue(self):
        # 3 filas. Es un caso distinto de "apunta a alguien que ya no está".
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, id_dueno="0"),
        ])])
        self.assertEqual({"dueno_ausente": 1}, resultado["reasignados_por_motivo"])

    def test_vehiculo_inexistente_deja_equipo_en_null_y_no_inventa_uno(self):
        # 15 órdenes. equipo_id acepta NULL, así que no hay equipo genérico.
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, id_vehiculo="888"),
        ])])
        self.assertEqual({"vehiculo_inexistente": 1}, resultado["reasignados_por_motivo"])

        fila = self.orden(1)
        self.assertIsNone(fila["equipo_id"])
        self.assertEqual(888, fila["talca_vehiculo_id"])

        registro = self.db.execute(
            "SELECT * FROM sync_talca_reasignados WHERE entidad = 'ordenes_equipo' AND talca_id = 1"
        ).fetchone()
        self.assertEqual("vehiculo_inexistente", registro["motivo"])

    def test_huerfana_de_las_dos_cosas_registra_las_dos_causas(self):
        # sync_talca_reasignados tiene UNIQUE (entidad, talca_id): si las dos
        # causas usaran la misma entidad, una se perdería en silencio.
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, id_dueno="999", id_vehiculo="888"),
        ])])
        self.assertEqual({"dueno_inexistente": 1, "vehiculo_inexistente": 1},
                         resultado["reasignados_por_motivo"])
        self.assertEqual(
            2,
            self.db.execute(
                "SELECT COUNT(*) FROM sync_talca_reasignados WHERE talca_id = 1"
            ).fetchone()[0],
        )

    def test_la_orden_se_reengancha_cuando_aparece_el_cliente(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, id_dueno="999")])])
        self.assertEqual(self.generico()["id"], self.orden(1)["cliente_id"])

        resultado = self.sincronizar([pagina([cliente_talca(999, nombre="APARECIO")])])[0]

        self.assertEqual(1, resultado["reenganchadas_ordenes_cliente"])
        cliente = self.db.execute("SELECT id FROM clientes WHERE talca_id = 999").fetchone()
        self.assertEqual(cliente["id"], self.orden(1)["cliente_id"])
        self.assertTrue(
            self.db.execute(
                "SELECT resuelto_en FROM sync_talca_reasignados WHERE entidad = 'ordenes' AND talca_id = 1"
            ).fetchone()["resuelto_en"]
        )

    def test_la_orden_se_reengancha_cuando_aparece_el_vehiculo(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, id_vehiculo="888")])])
        self.assertIsNone(self.orden(1)["equipo_id"])

        resultado, _ = self.sincronizar_vehiculos([pagina_vehiculos([
            vehiculo_talca(888, dueno=1, patente="AABB11"),
        ])])

        self.assertEqual(1, resultado["reenganchadas_ordenes_equipo"])
        equipo = self.db.execute("SELECT id FROM equipos WHERE talca_id = 888").fetchone()
        self.assertEqual(equipo["id"], self.orden(1)["equipo_id"])
        self.assertTrue(
            self.db.execute(
                "SELECT resuelto_en FROM sync_talca_reasignados "
                "WHERE entidad = 'ordenes_equipo' AND talca_id = 1"
            ).fetchone()["resuelto_en"]
        )


class TestOrdenesLlave(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()

    def test_el_match_es_por_talca_id_y_es_idempotente(self):
        for _ in range(3):
            self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])])
        self.assertEqual(1, len(self.ordenes_guardadas()))

    def test_cambiar_la_patente_no_crea_una_orden_nueva(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, patente="ZN1510")])])
        id_local = self.orden(1)["id"]

        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1, patente="BBCC11")])])

        self.assertEqual(1, len(self.ordenes_guardadas()))
        self.assertEqual(id_local, self.orden(1)["id"])
        self.assertEqual("BBCC11", self.orden(1)["patente"])

    def test_dos_ordenes_del_mismo_auto_conviven(self):
        # Cuando entren las ~7.100 OT sobre 5.161 vehículos, casi toda patente
        # va a tener varias órdenes. Emparejar por patente sería el bug.
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            orden_talca(1, patente="ZN1510"),
            orden_talca(2, patente="ZN1510"),
            orden_talca(3, patente="ZN1510"),
        ])])
        self.assertEqual(3, resultado["creadas"])

    def test_el_id_de_python_no_se_fuerza_a_coincidir(self):
        # El número que ve el taller es talca_id; el id local es una llave
        # técnica. Forzarlos a coincidir chocaría con el correlativo
        # compartido y con las órdenes creadas localmente.
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(5710)])])
        fila = self.orden(5710)
        self.assertEqual(5710, fila["talca_id"])
        self.assertNotEqual(5710, fila["id"])

    def test_una_fila_sin_id_se_omite(self):
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([
            {"nombre": "SIN ID", "estado": "ABIERTA"},
            orden_talca(1),
        ])])
        self.assertEqual(1, resultado["creadas"])
        self.assertEqual({"sin_id": 1}, resultado["omitidas_por_motivo"])


class TestOrdenesAdopcion(BaseOrdenes):
    """El importador histórico (hoy bloqueado con 410) dejó órdenes con
    `legacy_ot_no` poblado y sin `talca_id`. El sync tiene que reclamarlas en
    vez de insertar una copia -- pero solo cuando puede comprobar que son la
    misma orden: `legacy_ot_no` lo escribió un backfill que emparejaba por
    PATENTE, y hay 285 patentes repetidas."""

    def setUp(self):
        super().setUp()
        self.sembrar()
        self.cliente_id = self.clientes_guardados()[0]["id"]

    def _orden_preexistente(self, legacy_no, cliente_id=None):
        cursor = self.db.execute(
            """
            INSERT INTO ordenes_trabajo (cliente_id, estado, fecha_recepcion, creado_en,
                                         actualizado_en, legacy_ot_no, diagnostico)
            VALUES (?, 'recibido', '2021-09-14T11:02:00', '2021-09-14T11:02:00',
                    '2021-09-14T11:02:00', ?, 'diagnóstico histórico')
            """,
            (cliente_id if cliente_id is not None else self.cliente_id, legacy_no),
        )
        self.db.commit()
        return cursor.lastrowid

    def test_adopta_la_fila_preexistente_en_vez_de_duplicarla(self):
        id_previo = self._orden_preexistente(2671)

        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])

        self.assertEqual(1, len(self.ordenes_guardadas()), "no se insertó una copia")
        self.assertEqual(id_previo, self.orden(2671)["id"], "es la MISMA fila")
        self.assertEqual({"adoptada": 1}, resultado["adopciones_por_resultado"])
        self.assertEqual(0, resultado["creadas"])
        self.assertEqual(1, resultado["actualizadas"])

    def test_toda_adopcion_queda_registrada_aunque_haya_coincidido(self):
        self._orden_preexistente(2671)
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])

        registro = self.db.execute(
            "SELECT * FROM sync_talca_adopciones WHERE entidad = 'ordenes' AND talca_id = 2671"
        ).fetchone()
        self.assertEqual("adoptada", registro["resultado"])

    def test_no_adopta_si_el_cliente_no_calza(self):
        otro = self.db.execute(
            "INSERT INTO clientes (nombre, creado_en) VALUES ('OTRO CLIENTE', '2026-01-01T00:00:00')"
        ).lastrowid
        self.db.commit()
        id_previo = self._orden_preexistente(2671, cliente_id=otro)

        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])

        self.assertEqual({"conflicto_cliente": 1}, resultado["adopciones_por_resultado"])
        self.assertEqual(1, resultado["creadas"], "se insertó una orden nueva")
        self.assertNotEqual(id_previo, self.orden(2671)["id"])
        self.assertIsNone(
            self.db.execute(
                "SELECT talca_id FROM ordenes_trabajo WHERE id = ?", (id_previo,)
            ).fetchone()["talca_id"],
            "la fila local queda intacta, para poder revisarla",
        )
        registro = self.db.execute(
            "SELECT * FROM sync_talca_adopciones WHERE talca_id = 2671"
        ).fetchone()
        self.assertEqual("conflicto_cliente", registro["resultado"])

    def test_no_adopta_si_hay_dos_candidatas(self):
        # legacy_ot_no no tiene índice único, así que puede pasar. Elegir una
        # "cualquiera" es exactamente el error que este sync existe para no
        # cometer.
        self._orden_preexistente(2671)
        self._orden_preexistente(2671)

        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])

        self.assertEqual({"conflicto_ambiguo": 1}, resultado["adopciones_por_resultado"])
        self.assertEqual(1, resultado["creadas"])

    def test_no_toca_una_orden_local_con_otro_numero(self):
        id_previo = self._orden_preexistente(9999)
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])
        self.assertIsNone(
            self.db.execute(
                "SELECT talca_id FROM ordenes_trabajo WHERE id = ?", (id_previo,)
            ).fetchone()["talca_id"]
        )

    def test_la_segunda_corrida_ya_empareja_por_talca_id(self):
        self._orden_preexistente(2671)
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([orden_talca(2671)])])

        self.assertEqual({}, resultado["adopciones_por_resultado"],
                         "adoptar es de una sola vez: después ya hay talca_id")
        self.assertEqual(1, len(self.ordenes_guardadas()))


class TestOrdenesMarcaDeAgua(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()

    def test_la_marca_de_agua_es_el_hasta_de_talca(self):
        # `orden_trabajo` usa DATETIME en hora de Santiago, no TIMESTAMP en
        # UTC como clientes y vehículos. Python no lo parsea ni le hace
        # aritmética: lo guarda y lo devuelve tal cual, así que el huso da
        # igual mientras la entidad sea consistente consigo misma.
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)], hasta="2026-08-06 15:42:11")])
        self.assertEqual("2026-08-06 15:42:11",
                         sync_talca.leer_estado(self.db, "ordenes")["marca_agua"])

    def test_la_corrida_siguiente_manda_desde_sin_desde_id(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)], hasta="2026-08-06 15:42:11")])
        _, falso = self.sincronizar_ordenes([pagina_ordenes([])])

        self.assertEqual("2026-08-06 15:42:11", falso.llamadas[0]["desde"])
        self.assertIsNone(falso.llamadas[0]["desde_id"],
                          "sin desde_id es como Talca sabe que debe aplicar el margen de 2 minutos")

    def test_una_corrida_fallida_no_avanza_la_marca_de_agua(self):
        self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)], hasta="2026-08-06 15:42:11")])
        resultado, _ = self.sincronizar_ordenes([
            sync_talca.ClienteTalcaError("se cayó el hosting"),
        ])
        self.assertEqual("error", resultado["resultado"])
        self.assertEqual("2026-08-06 15:42:11",
                         sync_talca.leer_estado(self.db, "ordenes")["marca_agua"])

    def test_dry_run_no_escribe_nada(self):
        resultado, _ = self.sincronizar_ordenes([pagina_ordenes([orden_talca(1)])], dry_run=True)
        self.assertEqual(1, resultado["creadas"])
        self.assertEqual([], self.ordenes_guardadas())
        self.assertEqual("", sync_talca.leer_estado(self.db, "ordenes")["marca_agua"])

    def test_pagina_por_llave_igual_que_las_otras_entidades(self):
        _, falso = self.sincronizar_ordenes([
            pagina_ordenes([orden_talca(1), orden_talca(2)], hay_mas=True,
                           siguiente_desde="2026-08-06 15:00:00", siguiente_desde_id=2),
            pagina_ordenes([orden_talca(3)], hasta="2026-08-06 19:42:11"),
        ])
        self.assertEqual(2, len(falso.llamadas))
        self.assertEqual("2026-08-06 15:00:00", falso.llamadas[1]["desde"])
        self.assertEqual(2, falso.llamadas[1]["desde_id"])
        self.assertEqual(3, len(self.ordenes_guardadas()))


class TestComandoEntidad(unittest.TestCase):
    """El comando pelado pasó a sincronizar 'todo' (clientes, vehículos y
    órdenes). Acá solo se comprueba el contrato de los argumentos, que es
    lo que puede romperse sin que ningún otro test se entere."""

    def _falla(self, argv):
        # argparse escribe el uso en stderr antes de abortar; se silencia para
        # que la salida de la suite siga siendo legible.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            sync_talca._main(argv)

    def test_desde_no_se_acepta_con_todo(self):
        self._falla(["--desde", "2026-08-06 12:00:00"])

    def test_desde_y_recarga_completa_no_se_mezclan(self):
        self._falla(["clientes", "--desde", "2026-08-06 12:00:00", "--recarga-completa"])

    def test_entidad_desconocida(self):
        self._falla(["inventario"])

    def test_las_tres_entidades_conocidas_se_aceptan(self):
        # 'ordenes' era el ejemplo de entidad desconocida de este test hasta
        # que se implementó el paso 5. Que las tres estén en `choices` es lo
        # que hace que un typo siga abortando en vez de correr algo distinto.
        self.assertEqual(
            {"clientes", "vehiculos", "ordenes"},
            set(sync_talca.ENTIDADES),
        )


# ---------------------------------------------------------------------------
# 10. Diagnóstico de conexión (sin red: mismos dobles de la sección 8)
# ---------------------------------------------------------------------------
# Lo que se prueba acá no es "el ping funciona" -- eso solo se sabe corriéndolo
# contra el hosting de verdad. Es que el informe DISTINGA los modos de falla:
# el endpoint existe justamente porque desde un equipo local no se puede saber
# si mod_security bloquea la IP del servidor, y un informe que diga "falló" sin
# decir cómo no sirve para nada.

class ReadTimeout(OSError):
    """Se llama EXACTAMENTE como la de requests, y hereda de OSError como ella
    (RequestException hereda de IOError): la clasificación mira el nombre de
    la clase y de sus ancestros en vez de hacer isinstance, para no tener que
    importar requests.exceptions -- el módulo se importa igual sin requests
    instalado. Si el doble se llamara distinto, el test no probaría nada."""


class ConnectTimeout(ReadTimeout, ConnectionError):
    """En requests, ConnectTimeout hereda de Timeout Y de ConnectionError. El
    diagnóstico útil es 'timeout', así que el orden de la clasificación
    importa -- este es el caso que lo fija."""


def ping_ok(base="converti1_talca_claude"):
    return {
        "url": "https://elhosting/api/ping",
        "status_code": 200,
        "json": {"ok": True, "base": base},
        "texto": "",
    }


def ping_http(status, cuerpo=None, texto=""):
    return {"url": "https://elhosting/api/ping", "status_code": status, "json": cuerpo, "texto": texto}


class TestDiagnosticoClasificaLaFalla(unittest.TestCase):
    def _informe(self, respuesta_ping):
        falso = sync_talca.ClienteTalcaFalso(pings=[respuesta_ping])
        return sync_talca.diagnostico_conexion(falso), falso

    def test_ping_bueno(self):
        informe, falso = self._informe(ping_ok())

        self.assertTrue(informe["ok"])
        self.assertIsNone(informe["error"])
        self.assertEqual(200, informe["http_status"])
        self.assertEqual({"ok": True, "base": "converti1_talca_claude"}, informe["respuesta"]["json"])
        self.assertEqual(1, falso.pings_pedidos, "una sola llamada, no un bucle")

    def test_informa_el_user_agent_y_la_configuracion(self):
        informe, _ = self._informe(ping_ok())

        self.assertEqual(sync_talca.TALCA_USER_AGENT, informe["user_agent"])
        self.assertTrue(informe["config"]["talca_api_url_definida"])
        self.assertTrue(informe["config"]["talca_api_key_definida"])
        self.assertEqual(sync_talca.SYNC_INTENTOS_POR_PAGINA, informe["config"]["intentos"])
        self.assertIsInstance(informe["duracion_ms"], int)

    def test_nunca_devuelve_el_valor_de_la_api_key(self):
        # Lo más importante de todo el endpoint: es admin, pero la clave no
        # tiene por qué salir del servidor ni aparecer en un log de navegador.
        falso = sync_talca.ClienteTalcaFalso(
            pings=[ping_http(401, {"error": "API key inválida", "codigo": "no_autorizado"})],
            api_key="ESTA-ES-LA-CLAVE",
        )
        informe = sync_talca.diagnostico_conexion(falso)

        self.assertNotIn("ESTA-ES-LA-CLAVE", json.dumps(informe))
        self.assertTrue(informe["config"]["talca_api_key_definida"],
                        "sí debe decir que está definida, solo no su valor")

    def test_401_es_credenciales_y_no_un_problema_de_red(self):
        informe, _ = self._informe(ping_http(401, {"error": "API key inválida"}))

        self.assertFalse(informe["ok"])
        self.assertEqual("credenciales", informe["error"]["clase"])
        self.assertIn("TALCA_API_KEY", informe["error"]["explicacion"])
        # Que haya llegado una respuesta ES el dato: descarta el bloqueo.
        self.assertIn("respondió", informe["error"]["explicacion"])

    def test_403_apunta_a_mod_security(self):
        informe, _ = self._informe(ping_http(403, None, texto="<html>Access Denied</html>"))

        self.assertEqual("prohibido", informe["error"]["clase"])
        self.assertIn("mod_security", informe["error"]["explicacion"])
        self.assertIn("Access Denied", informe["respuesta"]["texto"])

    def test_404_apunta_a_la_url_o_al_despliegue(self):
        informe, _ = self._informe(ping_http(404, None, texto="Not Found"))

        self.assertEqual("ruta_inexistente", informe["error"]["clase"])
        self.assertIn("Api.php", informe["error"]["explicacion"])

    def test_500_es_del_lado_de_talca(self):
        informe, _ = self._informe(ping_http(500, None, texto="Fatal error"))

        self.assertEqual("error_servidor", informe["error"]["clase"])

    def test_200_que_no_es_json_es_una_pagina_del_hosting(self):
        informe, _ = self._informe(ping_http(200, None, texto="<html>WAF</html>"))

        self.assertFalse(informe["ok"], "un 200 con HTML adentro no es un ping bueno")
        self.assertEqual("respuesta_no_json", informe["error"]["clase"])

    def test_corte_de_conexion_es_la_firma_del_bloqueo(self):
        # Bluehosting no responde 403 cuando bloquea: cierra el socket. Es el
        # mismo síntoma que se espera de un bloqueo por IP de datacenter, y es
        # lo que este endpoint existe para poder ver desde el servidor.
        corte = sync_talca.ClienteTalcaError("No se pudo conectar con Talca (...) en 3 intentos: x")
        corte.__cause__ = ConnectionError("connection aborted: RemoteDisconnected")
        informe, _ = self._informe(corte)

        self.assertFalse(informe["ok"])
        self.assertEqual("conexion", informe["error"]["clase"])
        self.assertEqual("ConnectionError", informe["error"]["tipo"])
        self.assertIn("mod_security", informe["error"]["explicacion"])
        self.assertIsNone(informe["http_status"], "no llegó ninguna respuesta HTTP")

    def test_timeout_no_se_confunde_con_un_corte(self):
        agotado = sync_talca.ClienteTalcaError("No se pudo conectar ...")
        agotado.__cause__ = ReadTimeout("read timed out")
        informe, _ = self._informe(agotado)

        self.assertEqual("timeout", informe["error"]["clase"])
        self.assertIn("no es la firma de un bloqueo", informe["error"]["explicacion"].lower())

    def test_connect_timeout_se_clasifica_como_timeout_y_no_como_conexion(self):
        agotado = sync_talca.ClienteTalcaError("No se pudo conectar ...")
        agotado.__cause__ = ConnectTimeout("connect timed out")
        informe, _ = self._informe(agotado)

        self.assertEqual("timeout", informe["error"]["clase"])

    def test_el_mensaje_del_cliente_viaja_entero(self):
        # El mensaje del sync lleva la URL completa que se intentó, que fue lo
        # que permitió ver el 404 de /clientes vs /api/clientes. No se recorta.
        corte = sync_talca.ClienteTalcaError(
            "No se pudo conectar con Talca (https://elhosting/api/ping) en 3 intentos: boom"
        )
        corte.__cause__ = ConnectionError("boom")
        informe, _ = self._informe(corte)

        self.assertIn("https://elhosting/api/ping", informe["error"]["mensaje"])


class TestDiagnosticoSinConfiguracion(unittest.TestCase):
    """Sin las variables puestas no se sale a la red: un error de red mandaría
    a diagnosticar el firewall cuando lo que falta es una variable."""

    def test_sin_url_ni_key_no_se_hace_ninguna_llamada(self):
        for base, key, esperada in (
            ("", "secreta", "TALCA_API_URL"),
            ("https://elhosting", "", "TALCA_API_KEY"),
        ):
            with self.subTest(falta=esperada):
                doble = _RequestsFalso(_RespuestaFalsa(200, {"ok": True}))
                original = sync_talca.requests
                sync_talca.requests = doble
                self.addCleanup(lambda o=original: setattr(sync_talca, "requests", o))

                api = sync_talca.ClienteTalcaHTTP(base_url=base, api_key=key)
                informe = sync_talca.diagnostico_conexion(api)

                self.assertFalse(informe["ok"])
                self.assertEqual("configuracion", informe["error"]["clase"])
                self.assertIn(esperada, informe["error"]["mensaje"])
                self.assertEqual([], doble.llamadas, "no debe salir a la red sin configuración")


class TestPingUsaElMismoClienteDelSync(unittest.TestCase):
    """El valor del diagnóstico depende de que la petición sea IDÉNTICA a la
    del sync. Si el ping mandara otro User-Agent, podría pasar mientras el
    sync sigue bloqueado -- y el informe diría que todo está bien."""

    def _reemplazar_requests(self, doble):
        original = sync_talca.requests
        sync_talca.requests = doble
        self.addCleanup(lambda: setattr(sync_talca, "requests", original))
        return doble

    def test_pega_en_api_ping_con_el_user_agent_y_el_timeout_del_sync(self):
        doble = self._reemplazar_requests(_RequestsFalso(_RespuestaFalsa(200, {"ok": True, "base": "x"})))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting/api/", api_key="secreta", timeout=7)

        informe = sync_talca.diagnostico_conexion(api)

        llamada = doble.llamadas[0]
        self.assertEqual("https://elhosting/api/ping", llamada["url"])
        self.assertEqual(sync_talca.TALCA_USER_AGENT, llamada["headers"]["User-Agent"])
        self.assertNotIn("python-requests", llamada["headers"]["User-Agent"].lower())
        self.assertEqual("secreta", llamada["headers"]["X-API-Key"])
        self.assertEqual(7.0, llamada["timeout"])
        self.assertTrue(informe["ok"])

    def test_reintenta_igual_que_una_pagina_del_sync(self):
        doble = self._reemplazar_requests(
            _RequestsGuionado([OSError("connection aborted: RemoteDisconnected")] * 3)
        )
        esperas = []
        api = sync_talca.ClienteTalcaHTTP(
            base_url="https://elhosting", api_key="x", dormir=esperas.append
        )

        informe = sync_talca.diagnostico_conexion(api)

        self.assertEqual(sync_talca.SYNC_INTENTOS_POR_PAGINA, len(doble.llamadas))
        self.assertEqual([1, 2], esperas)
        self.assertEqual("conexion", informe["error"]["clase"])

    def test_un_corte_puntual_se_recupera_y_el_informe_da_ok(self):
        self._reemplazar_requests(_RequestsGuionado([
            OSError("connection aborted"),
            _RespuestaFalsa(200, {"ok": True, "base": "converti1_talca_claude"}),
        ]))
        api = sync_talca.ClienteTalcaHTTP(base_url="https://elhosting", api_key="x", dormir=lambda _s: None)

        informe = sync_talca.diagnostico_conexion(api)

        self.assertTrue(informe["ok"])
        self.assertEqual("converti1_talca_claude", informe["respuesta"]["json"]["base"])

    def test_un_http_de_error_no_se_reintenta(self):
        doble = self._reemplazar_requests(_RequestsGuionado([_RespuestaFalsa(401, {"error": "no"})]))
        api = sync_talca.ClienteTalcaHTTP(
            base_url="https://elhosting", api_key="x",
            dormir=lambda _s: self.fail("un error HTTP no se reintenta"),
        )

        informe = sync_talca.diagnostico_conexion(api)

        self.assertEqual(1, len(doble.llamadas))
        self.assertEqual("credenciales", informe["error"]["clase"])


class TestEndpointDeDiagnostico(unittest.TestCase):
    """El endpoint entero, con la ruta real y el cliente HTTP real -- lo único
    reemplazado es el módulo requests."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def setUp(self):
        for var, valor in (("TALCA_API_URL", "https://elhosting"), ("TALCA_API_KEY", "SECRETA-DE-VERDAD")):
            anterior = os.environ.get(var)
            os.environ[var] = valor
            self.addCleanup(
                lambda v=var, a=anterior: os.environ.__setitem__(v, a) if a is not None
                else os.environ.pop(v, None)
            )

    def _reemplazar_requests(self, doble):
        original = sync_talca.requests
        sync_talca.requests = doble
        self.addCleanup(lambda: setattr(sync_talca, "requests", original))
        return doble

    def test_devuelve_el_informe(self):
        self._reemplazar_requests(
            _RequestsFalso(_RespuestaFalsa(200, {"ok": True, "base": "converti1_talca_claude"}))
        )

        resp = self.client.get("/api/admin/sync/diagnostico")

        self.assertEqual(200, resp.status_code)
        informe = resp.get_json()
        self.assertTrue(informe["ok"])
        self.assertEqual("https://elhosting/api/ping", informe["url"])
        self.assertEqual(sync_talca.TALCA_USER_AGENT, informe["user_agent"])

    def test_una_falla_devuelve_200_con_el_informe_adentro(self):
        # El endpoint responde 200 aunque el ping falle: el fallo es el
        # resultado que se fue a buscar, no un error de este servidor. Un 500
        # acá haría pensar que el roto es el sistema nuevo.
        self._reemplazar_requests(_RequestsGuionado([OSError("connection aborted")] * 3))

        resp = self.client.get("/api/admin/sync/diagnostico")

        self.assertEqual(200, resp.status_code)
        informe = resp.get_json()
        self.assertFalse(informe["ok"])
        self.assertEqual("conexion", informe["error"]["clase"])

    def test_no_filtra_la_api_key(self):
        self._reemplazar_requests(_RequestsFalso(_RespuestaFalsa(401, {"error": "no autorizado"})))

        resp = self.client.get("/api/admin/sync/diagnostico")

        self.assertNotIn("SECRETA-DE-VERDAD", resp.get_data(as_text=True))

    def test_exige_admin(self):
        anonimo = taller_app.app.test_client()
        resp = anonimo.get("/api/admin/sync/diagnostico")
        self.assertNotEqual(200, resp.status_code)

    def test_no_escribe_nada_en_la_base(self):
        # Es de diagnóstico: no debe tocar la marca de agua ni ninguna tabla.
        self._reemplazar_requests(_RequestsFalso(_RespuestaFalsa(200, {"ok": True})))
        db = taller_app._conectar_db()
        try:
            antes = db.execute("SELECT COUNT(*) FROM sync_talca_estado").fetchone()[0]
            marcas = db.execute("SELECT entidad, marca_agua FROM sync_talca_estado").fetchall()
        finally:
            db.close()

        self.client.get("/api/admin/sync/diagnostico")

        db = taller_app._conectar_db()
        try:
            self.assertEqual(antes, db.execute("SELECT COUNT(*) FROM sync_talca_estado").fetchone()[0])
            self.assertEqual(
                [tuple(f) for f in marcas],
                [tuple(f) for f in db.execute("SELECT entidad, marca_agua FROM sync_talca_estado").fetchall()],
            )
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 12. Hilo de fondo y endpoint de estado (app.py)
# ---------------------------------------------------------------------------
# Nada de acá sale a la red ni duerme de verdad: el bucle recibe un `dormir`
# de mentira y un tope de vueltas, y las páginas vienen de ClienteTalcaFalso.
#
# Lo que se cubre es lo que, si se rompe, apaga el sync en silencio: que el
# hilo no arranque sin configuración, que un fallo en una entidad no se lleve
# puestas a las otras ni mate el bucle, y que cada corrida quede escrita en
# sync_talca_estado.

class BaseHilo(BaseOrdenes):
    def setUp(self):
        super().setUp()
        self.sembrar()
        self._intervalo(None)

    def _intervalo(self, valor):
        """Deja SYNC_INTERVALO_SEGUNDOS en `valor` (None = sin definir) y la
        restaura al terminar el test."""
        anterior = os.environ.get("SYNC_INTERVALO_SEGUNDOS")
        if valor is None:
            os.environ.pop("SYNC_INTERVALO_SEGUNDOS", None)
        else:
            os.environ["SYNC_INTERVALO_SEGUNDOS"] = str(valor)
        self.addCleanup(
            lambda: os.environ.__setitem__("SYNC_INTERVALO_SEGUNDOS", anterior)
            if anterior is not None else os.environ.pop("SYNC_INTERVALO_SEGUNDOS", None)
        )

    def paginas_de_una_vuelta(self, ordenes=(), clientes=(), vehiculos=()):
        """Las tres respuestas que consume una vuelta completa, en orden."""
        return [
            pagina(list(clientes), hasta="2026-08-10 12:00:00"),
            pagina_vehiculos(list(vehiculos), hasta="2026-08-10 12:00:01"),
            pagina_ordenes(list(ordenes), hasta="2026-08-10 12:00:02"),
        ]


class TestIntervaloConfigurable(BaseHilo):
    def test_sin_variable_el_hilo_no_arranca(self):
        self._intervalo(None)
        self.assertEqual(0, taller_app.sync_intervalo_segundos())
        self.assertFalse(taller_app.sync_automatico_configurado())

    def test_en_cero_el_hilo_no_arranca(self):
        # Es el caso de local: se sigue usando el comando manual.
        self._intervalo(0)
        self.assertEqual(0, taller_app.sync_intervalo_segundos())
        self.assertFalse(taller_app.sync_automatico_configurado())

    def test_un_valor_valido_manda(self):
        self._intervalo(120)
        self.assertEqual(120, taller_app.sync_intervalo_segundos())
        self.assertTrue(taller_app.sync_automatico_configurado())

    def test_un_valor_negativo_se_trata_como_apagado(self):
        self._intervalo(-5)
        self.assertEqual(0, taller_app.sync_intervalo_segundos())

    def test_un_valor_mal_escrito_cae_al_defecto_y_no_apaga_el_sync(self):
        # Si alguien se tomó el trabajo de definir la variable, la intención
        # era que corriera. Apagarlo en silencio por un typo sería peor.
        self._intervalo("cada 5 minutos")
        self.assertEqual(taller_app.SYNC_INTERVALO_DEFECTO, taller_app.sync_intervalo_segundos())

    def test_el_defecto_es_300(self):
        self.assertEqual(300, taller_app.SYNC_INTERVALO_DEFECTO)

    def test_iniciar_no_crea_hilo_sin_configuracion(self):
        self._intervalo(0)
        antes = threading.active_count()
        taller_app.iniciar_hilo_sync_talca()
        self.assertEqual(antes, threading.active_count())
        self.assertFalse(taller_app._hilo_sync_talca_iniciado)


class TestUnaVueltaDelHilo(BaseHilo):
    def test_corre_las_tres_entidades_en_orden(self):
        falso = sync_talca.ClienteTalcaFalso(self.paginas_de_una_vuelta(
            clientes=[cliente_talca(50)],
            vehiculos=[vehiculo_talca(50, dueno=50)],
            ordenes=[orden_talca(5710, id_dueno="50", id_vehiculo="50")],
        ))

        resultados = taller_app._correr_sync_una_vuelta(cliente_api=falso)

        self.assertEqual(["clientes", "vehiculos", "ordenes"],
                         [r["entidad"] for r in resultados])
        self.assertEqual(["clientes", "vehiculos", "ordenes"],
                         [ll["entidad"] for ll in falso.llamadas])
        self.assertTrue(all(r["resultado"] == "ok" for r in resultados))

    def test_lo_que_trae_queda_guardado(self):
        falso = sync_talca.ClienteTalcaFalso(self.paginas_de_una_vuelta(
            ordenes=[orden_talca(5710)],
        ))
        taller_app._correr_sync_una_vuelta(cliente_api=falso)
        self.assertEqual(1, len(self.ordenes_guardadas()))

    def test_cada_corrida_queda_registrada_en_sync_talca_estado(self):
        falso = sync_talca.ClienteTalcaFalso(self.paginas_de_una_vuelta(
            ordenes=[orden_talca(5710)],
        ))
        taller_app._correr_sync_una_vuelta(cliente_api=falso)

        for entidad, marca in (("clientes", "2026-08-10 12:00:00"),
                               ("vehiculos", "2026-08-10 12:00:01"),
                               ("ordenes", "2026-08-10 12:00:02")):
            with self.subTest(entidad=entidad):
                estado = sync_talca.leer_estado(self.db, entidad)
                self.assertEqual(marca, estado["marca_agua"])
                self.assertEqual("ok", estado["ultimo_resultado"])
                self.assertTrue(estado["ultima_corrida_en"])

    def test_un_fallo_en_clientes_no_impide_que_corran_las_otras(self):
        # La diferencia deliberada con el comando manual, que se detiene. En
        # un hilo cada 5 minutos, cortar las tres porque una tuvo un timeout
        # dejaría las órdenes sin sincronizar hasta que alguien lo note.
        falso = sync_talca.ClienteTalcaFalso([
            sync_talca.ClienteTalcaError("se cayó el hosting"),
            pagina_vehiculos([], hasta="2026-08-10 12:00:01"),
            pagina_ordenes([orden_talca(5710)], hasta="2026-08-10 12:00:02"),
        ])

        resultados = taller_app._correr_sync_una_vuelta(cliente_api=falso)

        self.assertEqual(["error", "ok", "ok"], [r["resultado"] for r in resultados])
        self.assertEqual(1, len(self.ordenes_guardadas()), "las órdenes sí entraron")

    def test_la_marca_de_agua_de_la_que_fallo_no_avanza(self):
        # Primero una vuelta buena, para tener una marca de agua conocida.
        taller_app._correr_sync_una_vuelta(cliente_api=sync_talca.ClienteTalcaFalso(
            self.paginas_de_una_vuelta()
        ))
        marca_previa = sync_talca.leer_estado(self.db, "clientes")["marca_agua"]

        taller_app._correr_sync_una_vuelta(cliente_api=sync_talca.ClienteTalcaFalso([
            sync_talca.ClienteTalcaError("se cayó el hosting"),
            pagina_vehiculos([], hasta="2026-08-10 13:00:01"),
            pagina_ordenes([], hasta="2026-08-10 13:00:02"),
        ]))

        estado = sync_talca.leer_estado(self.db, "clientes")
        self.assertEqual(marca_previa, estado["marca_agua"], "no avanzó")
        self.assertEqual("error", estado["ultimo_resultado"])
        self.assertEqual("2026-08-10 13:00:02",
                         sync_talca.leer_estado(self.db, "ordenes")["marca_agua"],
                         "la que sí anduvo avanzó igual")

    def test_una_excepcion_inesperada_no_corta_la_vuelta(self):
        # _sincronizar ya atrapa lo suyo; esto es la red por si algo se
        # escapa (un sqlite3.Error al guardar el estado, por ejemplo).
        original = sync_talca.sincronizar_entidad

        def explota(db, nombre, **kwargs):
            if nombre == "vehiculos":
                raise RuntimeError("algo muy inesperado")
            return original(db, nombre, **kwargs)

        taller_app.sync_entidad = explota
        self.addCleanup(lambda: setattr(taller_app, "sync_entidad", original))

        resultados = taller_app._correr_sync_una_vuelta(
            cliente_api=sync_talca.ClienteTalcaFalso([
                pagina([], hasta="2026-08-10 12:00:00"),
                pagina_ordenes([orden_talca(5710)], hasta="2026-08-10 12:00:02"),
            ])
        )

        self.assertEqual(["ok", "error", "ok"], [r["resultado"] for r in resultados])
        self.assertIn("RuntimeError", resultados[1]["detalle"])
        self.assertEqual(1, len(self.ordenes_guardadas()))

    def test_las_tres_pueden_fallar_sin_que_se_levante_nada(self):
        # El hilo llama a esto sin red de contención propia por vuelta: si
        # este método levantara, la vuelta entera se perdería.
        resultados = taller_app._correr_sync_una_vuelta(
            cliente_api=sync_talca.ClienteTalcaFalso([
                sync_talca.ClienteTalcaError("se cayó el hosting"),
                sync_talca.ClienteTalcaError("se cayó el hosting"),
                sync_talca.ClienteTalcaError("se cayó el hosting"),
            ])
        )
        self.assertEqual(["error", "error", "error"], [r["resultado"] for r in resultados])


class TestBucleDelHilo(BaseHilo):
    """El bucle en sí, con un `dormir` de mentira: lo que importa es que gire
    y que nada lo mate."""

    def test_duerme_el_intervalo_configurado_entre_vueltas(self):
        self._intervalo(120)
        esperas = []
        falso = sync_talca.ClienteTalcaFalso(
            self.paginas_de_una_vuelta() + self.paginas_de_una_vuelta()
        )
        original = taller_app._correr_sync_una_vuelta
        taller_app._correr_sync_una_vuelta = lambda cliente_api=None: original(cliente_api=falso)
        self.addCleanup(lambda: setattr(taller_app, "_correr_sync_una_vuelta", original))

        taller_app._hilo_sync_talca(veces=2, dormir=esperas.append)

        self.assertEqual([120, 120], esperas)

    def test_una_vuelta_que_explota_no_mata_el_bucle(self):
        # Si el hilo muere, el sync se apaga en silencio y las dos bases
        # divergen sin que nadie se entere. Es el peor final posible.
        self._intervalo(60)
        vueltas = []

        def explota(cliente_api=None):
            vueltas.append(1)
            raise RuntimeError("todo mal")

        original = taller_app._correr_sync_una_vuelta
        taller_app._correr_sync_una_vuelta = explota
        self.addCleanup(lambda: setattr(taller_app, "_correr_sync_una_vuelta", original))

        taller_app._hilo_sync_talca(veces=3, dormir=lambda _s: None)

        self.assertEqual(3, len(vueltas), "siguió girando después de cada explosión")

    def test_relee_el_intervalo_en_cada_vuelta(self):
        # Cambiar la variable en Railway toma efecto sin reiniciar.
        self._intervalo(60)
        esperas = []
        original = taller_app._correr_sync_una_vuelta
        taller_app._correr_sync_una_vuelta = lambda cliente_api=None: os.environ.__setitem__(
            "SYNC_INTERVALO_SEGUNDOS", "600"
        )
        self.addCleanup(lambda: setattr(taller_app, "_correr_sync_una_vuelta", original))

        taller_app._hilo_sync_talca(veces=2, dormir=esperas.append)

        self.assertEqual([600, 600], esperas)

    def test_con_el_intervalo_apagado_no_gira_a_toda_velocidad(self):
        # Si alguien pone 0 con el hilo ya andando, se queda dormido con el
        # defecto en vez de consumir CPU girando sin pausa.
        self._intervalo(0)
        esperas = []
        original = taller_app._correr_sync_una_vuelta
        taller_app._correr_sync_una_vuelta = lambda cliente_api=None: None
        self.addCleanup(lambda: setattr(taller_app, "_correr_sync_una_vuelta", original))

        taller_app._hilo_sync_talca(veces=1, dormir=esperas.append)

        self.assertEqual([taller_app.SYNC_INTERVALO_DEFECTO], esperas)


class TestEndpointDeEstado(BaseHilo):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def test_muestra_las_tres_entidades_en_orden(self):
        resp = self.client.get("/api/admin/sync/estado")

        self.assertEqual(200, resp.status_code)
        datos = resp.get_json()
        self.assertEqual(["clientes", "vehiculos", "ordenes"],
                         [e["entidad"] for e in datos["entidades"]])

    def test_informa_los_contadores_de_la_ultima_corrida(self):
        falso = sync_talca.ClienteTalcaFalso(self.paginas_de_una_vuelta(
            ordenes=[orden_talca(5710), orden_talca(5711)],
        ))
        taller_app._correr_sync_una_vuelta(cliente_api=falso)

        datos = self.client.get("/api/admin/sync/estado").get_json()
        ordenes = next(e for e in datos["entidades"] if e["entidad"] == "ordenes")

        self.assertEqual("ok", ordenes["ultimo_resultado"])
        self.assertEqual("2026-08-10 12:00:02", ordenes["marca_agua"])
        self.assertEqual(2, ordenes["filas_recibidas"])
        self.assertEqual(2, ordenes["filas_creadas"])
        self.assertEqual(0, ordenes["filas_omitidas"])
        self.assertTrue(ordenes["ultima_corrida_en"])
        self.assertTrue(ordenes["sincronizada_alguna_vez"])

    def test_distingue_nunca_corrio_de_corrio_sin_traer_nada(self):
        # Desde afuera las dos se ven igual: cero filas. La marca de agua es
        # lo que las separa.
        self.db.execute("DELETE FROM sync_talca_estado WHERE entidad = 'ordenes'")
        self.db.commit()

        datos = self.client.get("/api/admin/sync/estado").get_json()
        ordenes = next(e for e in datos["entidades"] if e["entidad"] == "ordenes")
        self.assertFalse(ordenes["sincronizada_alguna_vez"])
        self.assertEqual("", ordenes["marca_agua"])

    def test_muestra_el_fallo_de_la_ultima_corrida(self):
        taller_app._correr_sync_una_vuelta(cliente_api=sync_talca.ClienteTalcaFalso([
            sync_talca.ClienteTalcaError("se cayó el hosting"),
            pagina_vehiculos([], hasta="2026-08-10 12:00:01"),
            pagina_ordenes([], hasta="2026-08-10 12:00:02"),
        ]))

        datos = self.client.get("/api/admin/sync/estado").get_json()
        clientes = next(e for e in datos["entidades"] if e["entidad"] == "clientes")

        self.assertEqual("error", clientes["ultimo_resultado"])
        self.assertIn("se cayó el hosting", clientes["ultimo_detalle"])

    def test_informa_la_configuracion_del_hilo(self):
        self._intervalo(180)
        datos = self.client.get("/api/admin/sync/estado").get_json()
        self.assertTrue(datos["automatico_configurado"])
        self.assertEqual(180, datos["intervalo_segundos"])
        self.assertEqual("180", datos["intervalo_configurado_crudo"])

    def test_informa_cuando_el_automatico_esta_apagado(self):
        self._intervalo(None)
        datos = self.client.get("/api/admin/sync/estado").get_json()
        self.assertFalse(datos["automatico_configurado"])
        self.assertEqual(0, datos["intervalo_segundos"])
        self.assertIsNone(datos["intervalo_configurado_crudo"])

    def test_no_dispara_ninguna_sincronizacion(self):
        # Es de solo lectura. Si saliera a la red, mirarlo desde Railway
        # podría disparar una carga completa sin querer.
        marcas_antes = [tuple(f) for f in self.db.execute(
            "SELECT entidad, marca_agua, ultima_corrida_en FROM sync_talca_estado"
        ).fetchall()]

        self.client.get("/api/admin/sync/estado")

        self.assertEqual(marcas_antes, [tuple(f) for f in self.db.execute(
            "SELECT entidad, marca_agua, ultima_corrida_en FROM sync_talca_estado"
        ).fetchall()])

    def test_no_filtra_la_api_key(self):
        anterior = os.environ.get("TALCA_API_KEY")
        os.environ["TALCA_API_KEY"] = "SECRETA-DE-VERDAD"
        self.addCleanup(
            lambda: os.environ.__setitem__("TALCA_API_KEY", anterior) if anterior is not None
            else os.environ.pop("TALCA_API_KEY", None)
        )
        resp = self.client.get("/api/admin/sync/estado")
        self.assertNotIn("SECRETA-DE-VERDAD", resp.get_data(as_text=True))

    def test_exige_admin(self):
        anonimo = taller_app.app.test_client()
        self.assertNotEqual(200, anonimo.get("/api/admin/sync/estado").status_code)


class TestElComandoManualSigueIgual(BaseHilo):
    """El hilo no puede haber cambiado la forma de correr el sync a mano: es
    lo único que hay en local."""

    def test_sincronizar_entidad_despacha_por_nombre(self):
        falso = sync_talca.ClienteTalcaFalso([pagina_ordenes([orden_talca(5710)])])
        resultado = sync_talca.sincronizar_entidad(self.db, "ordenes", cliente_api=falso)
        self.assertEqual("ok", resultado["resultado"])
        self.assertEqual("ordenes", resultado["entidad"])

    def test_un_nombre_inexistente_es_un_error_de_programacion(self):
        with self.assertRaises(KeyError):
            sync_talca.sincronizar_entidad(self.db, "inventario")

    def test_las_funciones_por_entidad_siguen_existiendo(self):
        for nombre in ("sincronizar_clientes", "sincronizar_vehiculos", "sincronizar_ordenes"):
            self.assertTrue(callable(getattr(sync_talca, nombre)), nombre)


if __name__ == "__main__":
    unittest.main(verbosity=2)
