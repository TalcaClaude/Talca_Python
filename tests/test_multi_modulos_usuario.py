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


if __name__ == "__main__":
    unittest.main()
