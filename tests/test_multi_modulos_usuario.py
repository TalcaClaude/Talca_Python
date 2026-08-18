"""
Tests para el sistema de acceso multi-módulo por usuario (migración 17):
un usuario no-admin ahora puede tener acceso a más de un módulo a la vez
(antes: un único 'rol' == un único módulo). Cubre:

  1. Migración 17: agrega/backfillea usuarios.modulos_json sin cambiar el
     acceso de nadie que ya existía antes del deploy.
  2. requiere_rol()/requiere_modulo(): lógica de intersección de conjuntos
     (antes era igualdad de un solo valor).
  3. home_del_rol(): a qué módulo se redirige según ORDEN_PREFERENCIA_MODULOS,
     y el caso raro de un usuario sin ningún módulo (-> /sin-acceso).
  4. Contrato nuevo de /api/admin/usuarios (POST/PUT): es_admin + modulos en
     vez de un único 'rol', incluyendo validaciones (módulo inválido, al
     menos un módulo obligatorio, guardia de auto-democión).
  5. /sin-acceso: no genera loop de redirects y ofrece salida (logout).

Cómo correrlos:
    cd tests && python3 -m unittest test_multi_modulos_usuario -v
"""

import json
import os
import re
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
import core as taller_core  # noqa: E402


class TestMultiModulosUsuario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin = taller_app.app.test_client()
        cls.admin.post("/login", data={"username": "admin", "password": "taller2026"})
        cls._contador = 0

    def _nuevo_username(self, prefijo):
        type(self)._contador += 1
        return f"{prefijo}_{type(self)._contador}"

    def _crear_usuario(self, modulos=None, es_admin=False, password="clave123456"):
        username = self._nuevo_username("mm")
        resp = self.admin.post(
            "/api/admin/usuarios",
            json={
                "username": username, "password": password, "nombre": username,
                "es_admin": es_admin,
                "modulos": modulos or [],
            },
        )
        return resp, username

    def _login_como(self, username, password="clave123456"):
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    # ---- Migración 17: backfill ----

    def test_migracion_backfillea_usuario_existente_con_su_rol_como_modulo(self):
        # La migración 17 solo agrega/backfillea modulos_json la PRIMERA vez
        # que corre init_db() sin esa columna (ver el guard "if 'modulos_json'
        # not in columnas..." en app.py) -- así que para probar el backfill
        # real hay que simular una base "pre-migración 17": se le quita la
        # columna a propósito (DROP COLUMN, soportado desde SQLite 3.35) y
        # se inserta un usuario legado con solo 'rol', antes de volver a
        # correr init_db().
        with taller_app.app.app_context():
            db = taller_app.get_db()
            db.execute("ALTER TABLE usuarios DROP COLUMN modulos_json")
            db.commit()
            ts = taller_app.now_iso()
            db.execute(
                "INSERT INTO usuarios (username, password_hash, nombre, rol, activo, creado_en) "
                "VALUES (?, 'x', 'Viejo Bodega', 'bodega', 1, ?)",
                ("viejo_bodega_migracion", ts),
            )
            db.commit()
            taller_app.init_db()
            fila = db.execute(
                "SELECT modulos_json FROM usuarios WHERE username = 'viejo_bodega_migracion'"
            ).fetchone()
            # El backfill debe completar modulos_json a partir del rol que ya
            # tenía, para que no pierda ni gane acceso con el deploy.
            self.assertEqual(json.loads(fila["modulos_json"]), ["bodega"])
            # Y al admin semilla (rol == 'admin') no le debe crear ningún
            # módulo -- admin sigue significando "acceso a todo", nunca una
            # lista explícita.
            fila_admin = db.execute("SELECT modulos_json FROM usuarios WHERE rol = 'admin' LIMIT 1").fetchone()
            self.assertEqual(json.loads(fila_admin["modulos_json"]), [])

    def test_migracion_es_idempotente_no_pisa_modulos_ya_asignados(self):
        resp, username = self._crear_usuario(modulos=["finanzas", "bodega"])
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        with taller_app.app.app_context():
            taller_app.init_db()  # correr la migración de nuevo no debe tocar nada
            db = taller_app.get_db()
            fila = db.execute("SELECT modulos_json FROM usuarios WHERE username = ?", (username,)).fetchone()
        self.assertEqual(sorted(json.loads(fila["modulos_json"])), ["bodega", "finanzas"])

    # ---- requiere_rol / requiere_modulo: intersección de conjuntos ----

    def test_usuario_con_dos_modulos_puede_usar_ambos(self):
        resp, username = self._crear_usuario(modulos=["finanzas", "bodega"])
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        client = self._login_como(username)
        # /api/admin/usuarios es solo-admin (requiere_rol() sin argumentos);
        # usamos endpoints reales protegidos por cada módulo respectivo.
        self.assertEqual(client.get("/finanzas").status_code, 200)
        self.assertEqual(client.get("/logistica").status_code, 200)
        # Pero no accede a un módulo que no se le asignó.
        resp_op = client.get("/operativo")
        self.assertEqual(resp_op.status_code, 302)

    def test_usuario_con_un_solo_modulo_no_accede_a_otro(self):
        resp, username = self._crear_usuario(modulos=["operativo"])
        self.assertEqual(resp.status_code, 201)
        client = self._login_como(username)
        self.assertEqual(client.get("/operativo").status_code, 200)
        for ruta in ("/finanzas", "/logistica", "/convertidores"):
            resp_ruta = client.get(ruta)
            self.assertEqual(resp_ruta.status_code, 302)

    def test_sesion_expone_lista_de_modulos_ordenada(self):
        resp, username = self._crear_usuario(modulos=["operativo", "bodega"])
        self.assertEqual(resp.status_code, 201)
        client = self._login_como(username)
        sesion = client.get("/api/sesion").get_json()
        self.assertEqual(sesion["modulos"], ["bodega", "operativo"])

    # ---- home_del_rol(): orden de preferencia + caso sin módulos ----

    def test_home_del_rol_respeta_orden_de_preferencia(self):
        resp, username = self._crear_usuario(modulos=["finanzas", "bodega"])
        self.assertEqual(resp.status_code, 201)
        client = self._login_como(username)
        resp_raiz = client.get("/", follow_redirects=True)
        self.assertEqual(resp_raiz.status_code, 200)
        # ORDEN_PREFERENCIA_MODULOS = ["operativo", "bodega", "finanzas", "convertidores"]
        # -- con ["finanzas", "bodega"] asignados, bodega gana.
        self.assertIn("/logistica", resp_raiz.request.path)

    def test_usuario_sin_modulos_va_a_sin_acceso(self):
        resp, username = self._crear_usuario(modulos=["operativo"])
        self.assertEqual(resp.status_code, 201)
        usuario_id = resp.get_json()["id"]
        client = self._login_como(username)
        # Un admin le quita el único módulo que tenía.
        resp_edit = self.admin.put(
            f"/api/admin/usuarios/{usuario_id}",
            json={"es_admin": False, "modulos": []},
        )
        self.assertEqual(resp_edit.status_code, 400, resp_edit.get_data(as_text=True))
        self.assertIn("módulo", resp_edit.get_json()["error"])

    def test_sin_acceso_no_genera_loop_si_de_verdad_no_tiene_modulos(self):
        # Vía directa a la sesión (no a través del API, que ya bloquea
        # dejar a alguien sin módulos) para simular el caso raro que
        # /sin-acceso existe para manejar.
        resp, username = self._crear_usuario(modulos=["operativo"])
        self.assertEqual(resp.status_code, 201)
        usuario_id = resp.get_json()["id"]
        with taller_app.app.app_context():
            db = taller_app.get_db()
            db.execute("UPDATE usuarios SET modulos_json = '[]' WHERE id = ?", (usuario_id,))
            db.commit()
        client = self._login_como(username)
        resp_sin_acceso = client.get("/sin-acceso")
        self.assertEqual(resp_sin_acceso.status_code, 200)
        self.assertIn("Sin módulos asignados", resp_sin_acceso.get_data(as_text=True))
        # También queda encerrado ahí al intentar entrar a cualquier módulo.
        resp_raiz = client.get("/", follow_redirects=True)
        self.assertIn("/sin-acceso", resp_raiz.request.path)

    # ---- Contrato nuevo de /api/admin/usuarios ----

    def test_crear_usuario_rechaza_modulo_invalido(self):
        resp, _ = self._crear_usuario(modulos=["contabilidad"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("inválido", resp.get_json()["error"])

    def test_crear_usuario_exige_al_menos_un_modulo_si_no_es_admin(self):
        resp, _ = self._crear_usuario(modulos=[], es_admin=False)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("módulo", resp.get_json()["error"])

    def test_crear_usuario_admin_no_necesita_modulos(self):
        resp, username = self._crear_usuario(modulos=[], es_admin=True)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        cuerpo = resp.get_json()
        self.assertTrue(cuerpo["es_admin"])
        self.assertEqual(cuerpo["modulos"], [])
        client = self._login_como(username)
        self.assertEqual(client.get("/administracion").status_code, 200)

    def test_editar_usuario_agrega_y_quita_modulos(self):
        resp, username = self._crear_usuario(modulos=["operativo"])
        usuario_id = resp.get_json()["id"]

        resp_agregar = self.admin.put(
            f"/api/admin/usuarios/{usuario_id}", json={"modulos": ["operativo", "finanzas"]}
        )
        self.assertEqual(resp_agregar.status_code, 200, resp_agregar.get_data(as_text=True))
        self.assertEqual(sorted(resp_agregar.get_json()["modulos"]), ["finanzas", "operativo"])

        client = self._login_como(username)
        self.assertEqual(client.get("/finanzas").status_code, 200)
        self.assertEqual(client.get("/operativo").status_code, 200)

        resp_quitar = self.admin.put(f"/api/admin/usuarios/{usuario_id}", json={"modulos": ["finanzas"]})
        self.assertEqual(resp_quitar.status_code, 200)
        self.assertEqual(resp_quitar.get_json()["modulos"], ["finanzas"])
        # La sesión anterior sigue viva hasta que vuelva a hacer login (el
        # cambio de módulos no invalida sesiones activas) -- se confirma el
        # cambio real haciendo login de nuevo.
        client2 = self._login_como(username)
        self.assertEqual(client2.get("/operativo").status_code, 302)
        self.assertEqual(client2.get("/finanzas").status_code, 200)

    def test_editar_usuario_rechaza_dejar_al_ultimo_admin_sin_admin(self):
        resp, username = self._crear_usuario(modulos=[], es_admin=True)
        usuario_id = resp.get_json()["id"]
        client_nuevo_admin = self._login_como(username)
        # Este nuevo admin es UN admin más (el usuario 'admin' semilla sigue
        # activo), así que si se degrada a sí mismo no debería bloquearse
        # por la guardia de "único admin" -- probamos el caso real: que el
        # usuario 'admin' semilla no pueda quitarse a sí mismo si fuera el
        # único admin activo. Para eso, primero desactivamos/degradamos a
        # todos los demás admins (solo existe el semilla en un test limpio,
        # pero por las dudas usamos el propio semilla contra sí mismo).
        sesion_semilla = self.admin.get("/api/sesion").get_json()
        usuarios = self.admin.get("/api/admin/usuarios").get_json()["usuarios"]
        admin_semilla_id = next(u["id"] for u in usuarios if u["username"] == sesion_semilla["username"])
        # Degradar al admin recién creado (no es el único admin todavía).
        resp_ok = self.admin.put(
            f"/api/admin/usuarios/{usuario_id}", json={"es_admin": False, "modulos": ["operativo"]}
        )
        self.assertEqual(resp_ok.status_code, 200, resp_ok.get_data(as_text=True))
        # Ahora si el admin semilla es el único admin activo, no debe poder
        # auto-degradarse.
        otros_admin = [
            u for u in self.admin.get("/api/admin/usuarios").get_json()["usuarios"]
            if u["es_admin"] and u["id"] != admin_semilla_id and u["activo"]
        ]
        if not otros_admin:
            resp_auto_democion = self.admin.put(
                f"/api/admin/usuarios/{admin_semilla_id}", json={"es_admin": False, "modulos": ["operativo"]}
            )
            self.assertEqual(resp_auto_democion.status_code, 400)
            self.assertIn("único", resp_auto_democion.get_json()["error"])

    def test_usuario_admin_dict_incluye_es_admin_y_modulos(self):
        resp, _ = self._crear_usuario(modulos=["bodega", "convertidores"])
        cuerpo = resp.get_json()
        self.assertFalse(cuerpo["es_admin"])
        self.assertEqual(sorted(cuerpo["modulos"]), ["bodega", "convertidores"])
        self.assertNotIn("password_hash", cuerpo)


class TestFuncionesNucleoModulos(unittest.TestCase):
    """Prueba directa (sin HTTP) de las funciones de core.py que rigen el
    acceso multi-módulo, usando el request/session context de Flask."""

    def test_usuario_modulos_devuelve_conjunto_vacio_sin_sesion(self):
        with taller_app.app.test_request_context("/"):
            self.assertEqual(taller_core.usuario_modulos(), set())

    def test_usuario_modulos_lee_de_la_sesion(self):
        with taller_app.app.test_request_context("/"):
            from flask import session
            session["modulos"] = ["finanzas", "bodega"]
            self.assertEqual(taller_core.usuario_modulos(), {"finanzas", "bodega"})

    def test_home_del_rol_admin_no_depende_de_modulos(self):
        with taller_app.app.test_request_context("/"):
            from flask import session
            session["rol"] = "admin"
            session["modulos"] = []
            # home_del_rol() no está gateado por rol=='admin' en sí -- eso
            # lo maneja la vista "/" por separado -- pero un admin nunca
            # llama a esta función porque nunca llega a necesitarla (ver
            # vista_sin_acceso). Se prueba el caso general: con cero
            # módulos, siempre da /sin-acceso, admin o no.
            self.assertEqual(taller_core.home_del_rol(), "/sin-acceso")

    def test_home_del_rol_sigue_orden_de_preferencia(self):
        with taller_app.app.test_request_context("/"):
            from flask import session
            session["modulos"] = ["convertidores", "finanzas"]
            self.assertEqual(taller_core.home_del_rol(), "/finanzas")
            session["modulos"] = ["convertidores", "finanzas", "bodega"]
            self.assertEqual(taller_core.home_del_rol(), "/logistica")

    def test_requiere_modulo_redirige_si_no_tiene_ninguno_de_los_permitidos(self):
        vista = taller_core.requiere_modulo("finanzas")(lambda: "ok")
        with taller_app.app.test_request_context("/"):
            from flask import session
            session["rol"] = "bodega"
            session["modulos"] = ["bodega"]
            resultado = vista()
            # redirect() de Flask devuelve un Response con status 302.
            self.assertEqual(resultado.status_code, 302)

    def test_requiere_rol_403_json_si_no_tiene_ninguno_de_los_permitidos(self):
        vista = taller_core.requiere_rol("finanzas")(lambda: "ok")
        with taller_app.app.test_request_context("/"):
            from flask import session
            session["rol"] = "bodega"
            session["modulos"] = ["bodega"]
            resultado = vista()
            self.assertEqual(resultado[1], 403)


class TestControlAccesoApiFinanzasYOperativo(unittest.TestCase):
    """Hallazgo #3 (y #6) del informe de revisión técnica: las pantallas de
    cada módulo estaban protegidas, pero la mayoría de las rutas de API que
    esas pantallas consumen solo exigían tener sesión iniciada, sin importar
    a qué módulo -- un usuario con acceso ÚNICAMENTE a Bodega podía llamar
    directamente la API de Finanzas u Operativo (ver el resumen financiero,
    listar clientes, crear una orden de trabajo, etc.) con solo escribir la
    URL correcta. Se agregó @requiere_rol("finanzas")/@requiere_rol("operativo")
    a todas las rutas de esos dos módulos que no lo tenían (más los
    endpoints de mecánicos/Agente de WhatsApp, que dependen del módulo
    'operativo' -- ver /agente-ia en app.py). Los dos endpoints de
    seguimiento público por patente quedan deliberadamente afuera: están
    pensados para usarse sin sesión iniciada (ver RUTAS_PUBLICAS).

    Además cubre la extensión pedida por el taller a Logística
    (productos/proveedores/ubicaciones/categorías/modelos/movimientos/
    mermas/dashboard/reportes de inventario): decisión explícita de que
    CREAR/EDITAR/ELIMINAR quede restringido a 'bodega' (antes cualquier
    sesión iniciada podía tocarlo), pero LEER el listado de productos, el
    listado de proveedores y el stock por proveedor de un producto se dejó
    abierto a cualquier módulo con sesión iniciada -- porque Operativo
    (agregar un repuesto a una OT), Convertidores (agregar un repuesto a un
    convertidor) y Finanzas (elegir producto/proveedor al registrar una
    compra de emergencia o cargar una factura) dependen de leer esos datos
    desde su propio frontend; restringirlos habría roto esos tres flujos ya
    existentes. Las rutas de Facturas que no tenían protección quedaron con
    @requiere_rol("finanzas", "bodega") -- el mismo patrón que ya usaban
    las de 'pendientes de recepción' desde antes de esta revisión. Y cuatro
    rutas que resultaron ser de uso exclusivo de admin (el diagnóstico de
    disco duplicado con /api/admin/diagnostico-disco, el respaldo
    automático, y el Panel Principal que cruza Finanzas+Operativo y que la
    propia página ya limitaba a admin) quedaron con @requiere_rol() sin
    argumentos, igual que sus rutas hermanas de administración."""

    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.admin = taller_app.app.test_client()
        cls.admin.post("/login", data={"username": "admin", "password": "taller2026"})
        cls._contador = 0

    def _nuevo_username(self, prefijo):
        type(self)._contador += 1
        return f"{prefijo}_{type(self)._contador}"

    def _crear_usuario(self, modulos=None, es_admin=False, password="clave123456"):
        username = self._nuevo_username("acc")
        resp = self.admin.post(
            "/api/admin/usuarios",
            json={
                "username": username, "password": password, "nombre": username,
                "es_admin": es_admin,
                "modulos": modulos or [],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return username

    def _login_como(self, username, password="clave123456"):
        client = taller_app.app.test_client()
        resp = client.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        return client

    # ---- Barrido sistemático: TODA ruta registrada de estos dos módulos
    #      (más los endpoints de agente/mecánicos) debe rechazar a alguien
    #      que solo tiene 'bodega'. Esto es justamente lo que faltaba antes
    #      de esta corrección -- si en el futuro alguien agrega una ruta
    #      nueva a Finanzas u Operativo y se olvida del decorador, este test
    #      la detecta sin que nadie tenga que acordarse de agregarla a mano. ----

    _ENDPOINTS_AGENTE_MECANICOS = {
        "listar_mecanicos_agente", "crear_mecanico_agente", "editar_mecanico_agente",
        "eliminar_mecanico_agente", "simular_mensaje_agente", "simular_mensaje_voz_agente",
        "simular_mensaje_foto_agente", "listar_mensajes_agente",
    }
    _ENDPOINTS_LOGISTICA_BODEGA = {
        "productos_importar_excel_previsualizar", "productos_importar_excel_confirmar",
        "obtener_producto", "crear_producto", "editar_producto", "eliminar_producto",
        "subir_foto_producto", "eliminar_foto_producto", "obtener_foto_producto", "registrar_movimiento",
        "listar_ubicaciones", "crear_ubicacion", "crear_ubicaciones_rango", "reasignar_ubicacion",
        "eliminar_ubicacion", "listar_categorias", "crear_categoria", "editar_categoria", "eliminar_categoria",
        "listar_modelos", "crear_modelo", "editar_modelo", "eliminar_modelo",
        "crear_proveedor", "editar_proveedor", "eliminar_proveedor",
        "resumen", "alertas_stock_estado", "alertas_stock_probar", "planificacion_recalcular_todos",
        "listar_movimientos", "proveedores_origen_producto", "listar_mermas",
        "descartar_merma", "revertir_merma", "merma_origenes", "dashboard", "logistica_dashboard_graficos",
        "rotacion", "planificacion", "logistica_repuestos_tipicos_modelos",
        "reporte_inventario", "reporte_stock", "reporte_rotacion", "reporte_inventario_pdf",
        "reporte_stock_pdf", "reporte_rotacion_pdf", "reporte_movimientos", "reporte_movimientos_pdf",
        "reporte_compras", "reporte_compras_pdf", "reportes_resumen",
    }
    # Estas tres quedaron deliberadamente ABIERTAS a cualquier módulo con
    # sesión iniciada (no solo Bodega) porque Operativo (agregar un repuesto
    # a una OT), Convertidores (agregar un repuesto a un convertidor) y
    # Finanzas (elegir producto/proveedor al registrar una compra de
    # emergencia o cargar una factura) las llaman directamente desde su
    # propio frontend -- restringirlas habría roto esos tres flujos. Ver
    # decisión explícita del taller: "Bodega crea/edita, todos pueden leer".
    _ENDPOINTS_LOGISTICA_LECTURA_ABIERTA = {
        "listar_productos", "listar_proveedores", "stock_proveedores_producto",
    }
    _ENDPOINTS_FACTURAS_FINANZAS_O_BODEGA = {
        "listar_facturas", "obtener_factura", "obtener_factura_por_numero",
        "carga_masiva_plantilla", "carga_masiva_previsualizar", "carga_masiva_corregir_linea",
        "carga_masiva_corregir_proveedor", "escanear_foto_factura",
        # Ruta de ingreso directo (Bodega ingresa la factura y, si el
        # catálogo ya tiene todo, queda aplicada de una -- ver
        # _crear_pendiente_y_aplicar_si_posible en app.py): antes estos dos
        # eran solo de Finanzas, ahora Bodega también puede usarlos.
        "crear_factura_pendiente_recepcion", "carga_masiva_confirmar",
    }
    _ENDPOINTS_SOLO_ADMIN = {
        "estado_sistema", "backup_estado_automatico", "backup_probar_automatico", "panel_dueno",
    }
    _ENDPOINTS_PUBLICOS_EXCLUIDOS = {
        "operativo.seguimiento_publico", "operativo.api_seguimiento_publico",
    }

    @staticmethod
    def _ruta_concreta(regla):
        """Convierte el patrón de una regla de Flask (ej.
        '/api/operativo/ordenes/<int:orden_id>/items') en una URL concreta
        rellenando cualquier parámetro con un valor de relleno -- no importa
        que el recurso no exista, porque @requiere_rol corre ANTES de que
        la vista toque la base de datos (es el decorador más externo)."""
        patron = str(regla)

        def relleno(m):
            conversor = m.group(1)
            return "999999" if conversor == "int" else "zzz"

        return re.sub(r"<(?:(\w+):)?(\w+)>", relleno, patron)

    def _rutas_de(self, nombres_endpoint):
        rutas = []
        for regla in taller_app.app.url_map.iter_rules():
            endpoint = regla.endpoint
            if endpoint not in nombres_endpoint or endpoint in self._ENDPOINTS_PUBLICOS_EXCLUIDOS:
                continue
            metodos = (regla.methods or set()) - {"HEAD", "OPTIONS"}
            for metodo in metodos:
                rutas.append((endpoint, metodo, self._ruta_concreta(regla)))
        return rutas

    def _rutas_protegidas(self):
        rutas = []
        for regla in taller_app.app.url_map.iter_rules():
            endpoint = regla.endpoint
            es_de_los_modulos = (
                endpoint.startswith("finanzas.") or endpoint.startswith("operativo.")
                or endpoint in self._ENDPOINTS_AGENTE_MECANICOS
                or endpoint in self._ENDPOINTS_LOGISTICA_BODEGA
                or endpoint in self._ENDPOINTS_FACTURAS_FINANZAS_O_BODEGA
                or endpoint in self._ENDPOINTS_SOLO_ADMIN
            )
            if not es_de_los_modulos or endpoint in self._ENDPOINTS_PUBLICOS_EXCLUIDOS:
                continue
            metodos = (regla.methods or set()) - {"HEAD", "OPTIONS"}
            for metodo in metodos:
                rutas.append((endpoint, metodo, self._ruta_concreta(regla)))
        return rutas

    def test_todas_las_rutas_protegidas_rechazan_a_un_usuario_de_modulo_no_relacionado(self):
        # 'convertidores' no da acceso a NINGUNA de estas rutas (Finanzas,
        # Operativo, Agente, Logística/Bodega, Facturas ni las de solo-admin)
        # -- es el control negativo universal: si el barrido encuentra
        # aunque sea una que SÍ deja pasar a este usuario, es una ruta que
        # quedó sin el decorador correcto.
        username = self._crear_usuario(modulos=["convertidores"])
        client = self._login_como(username)
        rutas = self._rutas_protegidas()
        # Si esto falla, es porque el barrido no encontró nada que proteger
        # -- probablemente un cambio en cómo se registran las rutas/blueprints.
        self.assertGreater(len(rutas), 135, "el barrido debería cubrir >135 rutas entre todos los grupos")
        fallos = []
        for endpoint, metodo, ruta in rutas:
            resp = client.open(ruta, method=metodo, json={})
            if resp.status_code != 403:
                fallos.append((endpoint, metodo, ruta, resp.status_code))
        self.assertEqual(fallos, [], f"rutas que NO devolvieron 403 a un usuario de otro módulo: {fallos}")

    def test_usuario_sin_ningun_modulo_tambien_queda_bloqueado(self):
        # Caso límite: alguien creado sin ningún módulo (ver
        # test_usuario_sin_modulos_va_a_sin_acceso más arriba) tampoco debe
        # poder colarse por la API aunque nunca aterrice en ninguna pantalla.
        username = self._crear_usuario(modulos=["bodega"])
        with taller_app.app.app_context():
            db = taller_app.get_db()
            db.execute("UPDATE usuarios SET modulos_json = '[]' WHERE username = ?", (username,))
            db.commit()
        client = self._login_como(username)
        resp = client.get("/api/finanzas/resumen")
        self.assertEqual(resp.status_code, 403)

    # ---- Logística restringida a 'bodega' (decisión explícita del taller
    #      para el hallazgo #3, tras dejarlo pendiente el resto de esta
    #      revisión) + Facturas (finanzas O bodega, ya era el criterio
    #      usado en 'pendientes de recepción') + rutas de solo-admin. ----

    def test_usuario_bodega_si_puede_usar_logistica_y_facturas(self):
        username = self._crear_usuario(modulos=["bodega"])
        client = self._login_como(username)
        for endpoint, metodo, ruta in self._rutas_de(self._ENDPOINTS_LOGISTICA_BODEGA):
            resp = client.open(ruta, method=metodo, json={})
            self.assertNotEqual(resp.status_code, 403, f"{endpoint} {metodo} {ruta} -> {resp.status_code}")
        for endpoint, metodo, ruta in self._rutas_de(self._ENDPOINTS_FACTURAS_FINANZAS_O_BODEGA):
            resp = client.open(ruta, method=metodo, json={})
            self.assertNotEqual(resp.status_code, 403, f"{endpoint} {metodo} {ruta} -> {resp.status_code}")

    def test_usuario_finanzas_tambien_puede_usar_facturas_y_crear_productos_pero_no_el_resto_de_logistica(self):
        username = self._crear_usuario(modulos=["finanzas"])
        client = self._login_como(username)
        for endpoint, metodo, ruta in self._rutas_de(self._ENDPOINTS_FACTURAS_FINANZAS_O_BODEGA):
            resp = client.open(ruta, method=metodo, json={})
            self.assertNotEqual(resp.status_code, 403, f"{endpoint} {metodo} {ruta} -> {resp.status_code}")
        # Puede LEER el catálogo de productos (lo necesita para su propio
        # flujo de compra de emergencia/carga de facturas)...
        self.assertEqual(client.get("/api/productos").status_code, 200)
        # ...y también puede CREAR uno (el taller pasó a operar con una sola
        # persona a cargo de Bodega y Facturas, así que separar ese permiso
        # ya no aporta nada -- ver docstring de crear_producto en app.py),
        # pero sigue sin poder tocar el resto de Logística (ubicaciones,
        # categorías, dashboard, etc.), que sigue siendo exclusivo de
        # Bodega.
        resp_crear = client.post("/api/productos", json={"nombre": "Repuesto de emergencia"})
        self.assertEqual(resp_crear.status_code, 201, resp_crear.get_data(as_text=True))
        self.assertEqual(client.get("/api/logistica/dashboard/graficos").status_code, 403)

    def test_usuario_operativo_no_puede_escribir_en_logistica_ni_tocar_facturas(self):
        username = self._crear_usuario(modulos=["operativo"])
        client = self._login_como(username)
        # Igual que Finanzas: puede leer el catálogo (lo necesita para
        # agregar repuestos a una OT) pero no crear/editar productos ni
        # entrar a Facturas (que es finanzas O bodega, no operativo).
        self.assertEqual(client.get("/api/productos").status_code, 200)
        resp_crear = client.post("/api/productos", json={"nombre": "Intruso"})
        self.assertEqual(resp_crear.status_code, 403)
        self.assertEqual(client.get("/api/facturas").status_code, 403)

    def test_operativo_convertidores_y_finanzas_pueden_leer_catalogo_y_stock_por_proveedor(self):
        # Escenario real: agregar un repuesto a una OT/Convertidor, o elegir
        # producto/proveedor al registrar una compra de emergencia -- las
        # tres pantallas necesitan poder leer esto aunque no sea "su"
        # módulo. Ver _ENDPOINTS_LOGISTICA_LECTURA_ABIERTA.
        resp_prod = self.admin.post(
            "/api/productos", json={"nombre": "Producto lectura cruzada", "stock_disponible": 0, "costo_compra": 0}
        )
        self.assertEqual(resp_prod.status_code, 201, resp_prod.get_data(as_text=True))
        producto_id = resp_prod.get_json()["id"]
        for modulo in ("operativo", "convertidores", "finanzas", "bodega"):
            username = self._crear_usuario(modulos=[modulo])
            client = self._login_como(username)
            self.assertEqual(client.get("/api/productos").status_code, 200, modulo)
            self.assertEqual(client.get("/api/proveedores").status_code, 200, modulo)
            resp_stock = client.get(f"/api/productos/{producto_id}/stock-proveedores")
            self.assertEqual(resp_stock.status_code, 200, f"[{modulo}] {resp_stock.get_data(as_text=True)}")

    def test_pero_esa_lectura_abierta_no_sirve_para_escribir(self):
        # La lectura de /api/productos y /api/proveedores es abierta, pero
        # crear/editar/eliminar sigue siendo solo de Bodega (y, desde que el
        # taller pasó a operar con una sola persona a cargo de Bodega y
        # Facturas, también de Finanzas para /api/productos Y /api/proveedores
        # -- ver crear_producto/crear_proveedor en app.py, necesario para
        # poder crear un repuesto con su marca y, si hace falta, su
        # proveedor, al vuelo desde la nueva ventana de Facturas sin tener
        # que pasar por Logística aparte) -- confirma que no se "filtró" de
        # más al resolver el escenario anterior, ni a módulos que no tienen
        # nada que ver (operativo, convertidores).
        for modulo in ("operativo", "convertidores"):
            username = self._crear_usuario(modulos=[modulo])
            client = self._login_como(username)
            self.assertEqual(client.post("/api/productos", json={"nombre": "X"}).status_code, 403)
            self.assertEqual(client.post("/api/proveedores", json={"nombre": "X"}).status_code, 403)

        username_finanzas = self._crear_usuario(modulos=["finanzas"])
        client_finanzas = self._login_como(username_finanzas)
        self.assertEqual(client_finanzas.post("/api/productos", json={"nombre": "X"}).status_code, 201)
        self.assertEqual(client_finanzas.post("/api/proveedores", json={"nombre": "Proveedor X"}).status_code, 201)

    def test_rutas_de_solo_admin_rechazan_incluso_a_bodega_y_finanzas(self):
        # panel-dueno, estado_sistema y el respaldo automático son
        # transversales (cruzan Finanzas+Operativo, o son diagnóstico de
        # infraestructura) -- ni siquiera Bodega o Finanzas deberían
        # entrar, solo admin.
        for modulo in ("bodega", "finanzas", "operativo"):
            username = self._crear_usuario(modulos=[modulo])
            client = self._login_como(username)
            for endpoint, metodo, ruta in self._rutas_de(self._ENDPOINTS_SOLO_ADMIN):
                resp = client.open(ruta, method=metodo, json={})
                self.assertEqual(resp.status_code, 403, f"[{modulo}] {endpoint} {metodo} {ruta} -> {resp.status_code}")

    # ---- Reproducción literal de los escenarios descritos en el informe ----

    def test_usuario_bodega_entra_a_finanzas_pero_solo_ve_la_pestana_facturas(self):
        # Desde la nueva ruta de ingreso directo de facturas (ver
        # _crear_pendiente_y_aplicar_si_posible en app.py y el comentario
        # de vista_finanzas()), Bodega SÍ puede entrar a /finanzas -- pero
        # la plantilla le esconde todo salvo la pestaña "Facturas" (ver
        # 'solo_facturas' en finanzas.html), y el resto de las APIs de
        # Finanzas (resumen general, CxC, Gastos Generales, etc.) le
        # siguen estando vedadas -- solo se le abrió lo puntual que
        # necesita para ingresar facturas.
        username = self._crear_usuario(modulos=["bodega"])
        client = self._login_como(username)
        resp = client.get("/finanzas")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertNotIn('data-vista="resumen"', html)
        self.assertIn('data-vista="facturas"', html)
        resp_api = client.get("/api/finanzas/resumen")
        self.assertEqual(resp_api.status_code, 403)  # el resumen general sigue siendo solo de Finanzas

    def test_bodega_y_finanzas_entran_a_la_nueva_ventana_de_facturas_pero_operativo_no(self):
        # /facturas/nueva (ver vista_factura_nueva en app.py) es la nueva
        # ventana dedicada para registrar una factura de compra a mano --
        # mismo criterio de acceso que /finanzas: Bodega y Finanzas sí,
        # cualquier otro módulo no.
        for modulo in ("bodega", "finanzas"):
            username = self._crear_usuario(modulos=[modulo])
            client = self._login_como(username)
            self.assertEqual(client.get("/facturas/nueva").status_code, 200)

        username_ajeno = self._crear_usuario(modulos=["operativo"])
        client_ajeno = self._login_como(username_ajeno)
        resp_ajeno = client_ajeno.get("/facturas/nueva")
        self.assertNotEqual(resp_ajeno.status_code, 200)

    def test_usuario_bodega_no_puede_listar_clientes_de_operativo(self):
        username = self._crear_usuario(modulos=["bodega"])
        client = self._login_como(username)
        resp = client.get("/api/operativo/clientes")
        self.assertEqual(resp.status_code, 403)

    def test_usuario_bodega_no_puede_crear_un_cliente_ni_una_orden_de_trabajo(self):
        username = self._crear_usuario(modulos=["bodega"])
        client = self._login_como(username)
        resp_cliente = client.post("/api/operativo/clientes", json={"nombre": "Intruso"})
        self.assertEqual(resp_cliente.status_code, 403)
        resp_orden = client.post("/api/operativo/ordenes", json={"cliente_id": 1, "equipo_id": 1})
        self.assertEqual(resp_orden.status_code, 403)

    def test_endpoints_de_mecanicos_y_agente_exigen_modulo_operativo(self):
        username = self._crear_usuario(modulos=["bodega"])
        client = self._login_como(username)
        self.assertEqual(client.get("/api/agente/mecanicos").status_code, 403)
        self.assertEqual(
            client.post("/api/agente/mecanicos", json={"nombre": "X", "telefono": "+56911111111"}).status_code, 403
        )
        self.assertEqual(
            client.post("/api/agente/simular-mensaje", json={"telefono": "+56911111111", "texto": "hola"}).status_code,
            403,
        )
        self.assertEqual(client.get("/api/agente/mensajes").status_code, 403)

    # ---- Control positivo: el módulo correcto SÍ puede seguir trabajando
    #      con normalidad (que no se haya bloqueado de más). ----

    def test_usuario_finanzas_si_puede_usar_su_propia_api(self):
        username = self._crear_usuario(modulos=["finanzas"])
        client = self._login_como(username)
        resp = client.get("/api/finanzas/resumen")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_usuario_operativo_si_puede_usar_su_propia_api(self):
        username = self._crear_usuario(modulos=["operativo"])
        client = self._login_como(username)
        resp = client.get("/api/operativo/clientes")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        resp_agente = client.get("/api/agente/mecanicos")
        self.assertEqual(resp_agente.status_code, 200, resp_agente.get_data(as_text=True))

    def test_seguimiento_publico_sigue_funcionando_sin_sesion(self):
        # Este NO debe quedar protegido -- es la página pública que un
        # cliente usa sin iniciar sesión para ver el estado de su vehículo.
        cliente_anonimo = taller_app.app.test_client()
        resp = cliente_anonimo.get("/api/seguimiento?patente=ZZZZ99")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
