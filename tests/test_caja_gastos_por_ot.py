"""Tests de Caja Chica -- Migración 25/26: boletas parciales + rendición por boleta.

Antes, un "gasto" de Caja Chica era una sola cosa (el retiro Y su boleta a
la vez). El taller pidió separarlo en dos pasos:
  1. "Nuevo gasto": solo monto, medio de pago y fecha -- quien lo registra
     viendo el banco no tiene más información que esa a mano.
  2. Una o más "boletas" (caja_gasto_detalles) contra ese mismo gasto,
     cargadas después por quien sí tiene la boleta física en mano (puede
     ser otra persona, ej. Cristian contrastando contra el trabajador que
     compró). Cada boleta tiene su propio N° de documento, monto (no
     necesariamente el 100% del retiro -- sobre todo en efectivo), rubro
     GAV, detalle y, si el rubro es "Gastos por OT", la orden de trabajo
     relacionada.

Migración 26: la Rendición (POST /api/caja/rendicion) se genera por BOLETA
individual, no por gasto completo -- el taller reportó que rendir un retiro
de $180.000 con una sola boleta de $90.000 dejaba TODO el retiro marcado
"rendido" aunque solo se hubiera documentado la mitad. Ahora el monto
rendido es exactamente la suma de las boletas seleccionadas; el resto del
retiro (sin boleta todavía) sigue disponible para más boletas y otra
rendición después.

También cubre que "Gastos por OT" se excluye del total de GAV en el Estado
de Resultados (no es gasto administrativo, es costo de una OT puntual) pero
se reporta aparte.
"""
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_cajaot_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _crear_cliente(self, nombre):
        resp = self.client.post("/api/operativo/clientes", json={"nombre": nombre})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_orden(self, cliente_id):
        resp = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente_id})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()

    def _crear_gasto(self, **campos):
        base = {"monto": "1000", "medio_pago": "efectivo_cajero", "fecha": "2026-07-15"}
        base.update(campos)
        return self.client.post("/api/caja/gasto", data=base)

    def _agregar_boleta(self, gasto_id, **campos):
        base = {"monto": "1000"}
        base.update(campos)
        return self.client.post(f"/api/caja/gasto/{gasto_id}/boleta", data=base)


class TestNuevoGastoSimplificado(_Base):
    def test_crear_gasto_solo_pide_monto_medio_y_fecha(self):
        resp = self._crear_gasto()
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["monto"], 1000.0)
        self.assertEqual(g["medio_pago"], "efectivo_cajero")
        # Recién creado, sin boletas: no está listo para rendir.
        self.assertEqual(g["monto_justificado"], 0)
        self.assertEqual(g["monto_pendiente"], 1000.0)
        self.assertFalse(g["listo_para_rendir"])
        self.assertEqual(g["boletas"], [])

    def test_gasto_sin_monto_es_rechazado(self):
        resp = self.client.post(
            "/api/caja/gasto", data={"medio_pago": "efectivo_cajero", "fecha": "2026-07-15"}
        )
        self.assertEqual(resp.status_code, 400)


class TestBoletasParciales(_Base):
    def test_una_boleta_por_el_monto_completo_deja_el_gasto_listo(self):
        gasto = self._crear_gasto(monto="15000").get_json()
        resp = self._agregar_boleta(gasto["id"], monto="15000", numero_documento="F-1", rubro_gav="otros")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["monto_justificado"], 15000.0)
        self.assertEqual(g["monto_pendiente"], 0)
        self.assertTrue(g["listo_para_rendir"])
        self.assertEqual(len(g["boletas"]), 1)

    def test_varias_boletas_parciales_van_reduciendo_el_pendiente(self):
        """El caso central pedido por el taller: un retiro de $200.000 en
        efectivo que se justifica con boletas parciales, sin perder la
        información entre una y otra."""
        gasto = self._crear_gasto(monto="200000").get_json()

        r1 = self._agregar_boleta(gasto["id"], monto="60000", numero_documento="F-1", rubro_gav="otros")
        self.assertEqual(r1.status_code, 201, r1.get_data(as_text=True))
        self.assertEqual(r1.get_json()["monto_pendiente"], 140000.0)
        self.assertFalse(r1.get_json()["listo_para_rendir"])

        r2 = self._agregar_boleta(gasto["id"], monto="90000", numero_documento="F-2", rubro_gav="mantencion")
        self.assertEqual(r2.get_json()["monto_pendiente"], 50000.0)

        r3 = self._agregar_boleta(gasto["id"], monto="50000", numero_documento="F-3", rubro_gav="generales")
        self.assertEqual(r3.get_json()["monto_pendiente"], 0)
        self.assertTrue(r3.get_json()["listo_para_rendir"])
        self.assertEqual(len(r3.get_json()["boletas"]), 3)

    def test_boleta_rubro_gastos_por_ot_exige_orden_de_trabajo(self):
        gasto = self._crear_gasto(monto="7000").get_json()
        resp = self._agregar_boleta(gasto["id"], monto="7000", rubro_gav="gastos_por_ot")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("orden de trabajo", resp.get_json()["error"].lower())

    def test_boleta_gastos_por_ot_con_orden_valida_queda_ligada_a_la_ot(self):
        cliente = self._crear_cliente("CLIENTE BOLETA OT")
        orden = self._crear_orden(cliente["id"])
        gasto = self._crear_gasto(monto="7000").get_json()

        resp = self._agregar_boleta(
            gasto["id"], monto="7000", rubro_gav="gastos_por_ot", orden_id=str(orden["id"]), numero_documento="F-9"
        )
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        boleta = resp.get_json()["boletas"][0]
        self.assertEqual(boleta["orden_id"], orden["id"])
        self.assertEqual(boleta["rubro_gav"], "gastos_por_ot")

    def test_boleta_con_orden_id_inexistente_es_rechazada(self):
        gasto = self._crear_gasto(monto="7000").get_json()
        resp = self._agregar_boleta(gasto["id"], monto="7000", rubro_gav="gastos_por_ot", orden_id="999999")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
        self.assertIn("no existe", resp.get_json()["error"].lower())

    def test_se_puede_agregar_otra_boleta_aunque_una_anterior_ya_este_rendida(self):
        """Migración 26: rendir una boleta ya NO bloquea el resto del gasto
        -- se puede seguir agregando boletas nuevas contra el mismo retiro
        (para documentar lo que falta) y rendirlas después en otro lote."""
        gasto = self._crear_gasto(monto="2000").get_json()
        r1 = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1")
        boleta1_id = r1.get_json()["boletas"][0]["id"]
        rend = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta1_id]})
        self.assertEqual(rend.status_code, 201, rend.get_data(as_text=True))

        resp = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-2")
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(len(g["boletas"]), 2)
        self.assertEqual(g["monto_pendiente"], 0)
        self.assertEqual(g["monto_rendido"], 1000.0)
        self.assertEqual(g["monto_por_rendir"], 1000.0)

    def test_editar_o_eliminar_una_boleta_ya_rendida_es_rechazado(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        r1 = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1")
        boleta_id = r1.get_json()["boletas"][0]["id"]
        rend = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_id]})
        self.assertEqual(rend.status_code, 201, rend.get_data(as_text=True))

        resp_edit = self.client.put(f"/api/caja/boleta/{boleta_id}", data={"monto": "500"})
        self.assertEqual(resp_edit.status_code, 409, resp_edit.get_data(as_text=True))

        resp_del = self.client.delete(f"/api/caja/boleta/{boleta_id}")
        self.assertEqual(resp_del.status_code, 409, resp_del.get_data(as_text=True))

    def test_editar_boleta(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        boleta = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1", rubro_gav="otros")
        boleta_id = boleta.get_json()["boletas"][0]["id"]

        resp = self.client.put(f"/api/caja/boleta/{boleta_id}", data={
            "monto": "1000", "numero_documento": "F-1-CORREGIDA", "rubro_gav": "otros",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        boletas = resp.get_json()["boletas"]
        self.assertEqual(boletas[0]["numero_documento"], "F-1-CORREGIDA")

    def test_eliminar_boleta_reabre_el_gasto(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        boleta = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1", rubro_gav="otros")
        boleta_id = boleta.get_json()["boletas"][0]["id"]
        self.assertTrue(boleta.get_json()["listo_para_rendir"])

        resp = self.client.delete(f"/api/caja/boleta/{boleta_id}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["monto_pendiente"], 1000.0)
        self.assertFalse(resp.get_json()["listo_para_rendir"])


class TestCierreManual(_Base):
    def test_cerrar_manualmente_deja_listo_para_rendir_aunque_falte_monto(self):
        gasto = self._crear_gasto(monto="20000").get_json()
        self._agregar_boleta(gasto["id"], monto="18000", numero_documento="F-1", rubro_gav="otros")

        resp = self.client.put(f"/api/caja/gasto/{gasto['id']}/cerrar", json={"cerrado": True})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        g = resp.get_json()
        self.assertEqual(g["monto_pendiente"], 2000.0)  # el pendiente real no cambia...
        self.assertTrue(g["listo_para_rendir"])          # ...pero ya puede rendirse
        self.assertTrue(g["cerrado_manualmente"])

    def test_agregar_otra_boleta_reabre_el_cierre_manual(self):
        gasto = self._crear_gasto(monto="20000").get_json()
        self._agregar_boleta(gasto["id"], monto="18000", numero_documento="F-1", rubro_gav="otros")
        self.client.put(f"/api/caja/gasto/{gasto['id']}/cerrar", json={"cerrado": True})

        resp = self._agregar_boleta(gasto["id"], monto="2000", numero_documento="F-2", rubro_gav="otros")
        g = resp.get_json()
        self.assertFalse(g["cerrado_manualmente"])
        self.assertEqual(g["monto_pendiente"], 0)
        self.assertTrue(g["listo_para_rendir"])


class TestRendicionPorBoleta(_Base):
    """Migración 26: la Rendición se genera seleccionando BOLETAS
    individuales (ya cargadas con caja_agregar_boleta), no gastos/retiros
    completos. El monto rendido es exactamente lo que se está documentando
    en ese momento -- el resto del retiro, si queda algo sin boleta, sigue
    disponible para cargarle más boletas y rendirlas en otro lote después.
    Ya no hace falta "cerrar manualmente" ni justificar el 100% de nada
    para poder rendir: cada boleta ya es, por definición, algo documentado
    y listo para presentarse."""

    def test_no_se_puede_rendir_sin_seleccionar_ninguna_boleta(self):
        resp = self.client.post("/api/caja/rendicion", json={"detalle_ids": []})
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))

    def test_rendir_una_boleta_parcial_solo_rinde_ese_monto_y_el_resto_sigue_pendiente(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        r1 = self._agregar_boleta(gasto["id"], monto="400", numero_documento="F-1")
        boleta_id = r1.get_json()["boletas"][0]["id"]

        resp = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_id]})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        # El total rendido es el de la BOLETA seleccionada, no el del retiro
        # completo -- a diferencia del modelo viejo, acá los $600 restantes
        # siguen sin documentar Y sin rendir, disponibles para una boleta futura.
        self.assertEqual(resp.get_json()["total"], 400.0)

        pend = self.client.get("/api/caja/pendientes").get_json()["gastos"]
        gasto_pendiente = next(g for g in pend if g["id"] == gasto["id"])
        self.assertEqual(gasto_pendiente["monto_pendiente"], 600.0)
        self.assertEqual(gasto_pendiente["monto_rendido"], 400.0)
        self.assertEqual(len(gasto_pendiente["boletas"]), 1)
        self.assertTrue(gasto_pendiente["boletas"][0]["rendido"])

    def test_gasto_desaparece_de_pendientes_cuando_todo_esta_documentado_y_rendido(self):
        gasto = self._crear_gasto(monto="1000", medio_pago="tarjeta_empresa").get_json()
        r1 = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1", rubro_gav="otros")
        boleta_id = r1.get_json()["boletas"][0]["id"]

        resp = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_id]})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))

        pend = self.client.get("/api/caja/pendientes?medio_pago=tarjeta_empresa").get_json()["gastos"]
        self.assertFalse(any(g["id"] == gasto["id"] for g in pend))

        det = self.client.get(f"/api/caja/rendicion/{resp.get_json()['id']}").get_json()
        self.assertEqual(len(det["boletas"]), 1)
        self.assertEqual(det["boletas"][0]["numero_documento"], "F-1")

    def test_se_pueden_agregar_mas_boletas_y_rendirlas_en_otro_lote(self):
        """El caso concreto que reportó el taller: un retiro de $180.000 se
        rinde primero con una boleta de $90.000 -- los $90.000 restantes
        quedan disponibles para una segunda boleta y una segunda rendición,
        sin que la primera rendición se los haya "llevado" sin documentarlos."""
        gasto = self._crear_gasto(monto="180000").get_json()
        r1 = self._agregar_boleta(gasto["id"], monto="90000", numero_documento="F-1")
        boleta1_id = r1.get_json()["boletas"][0]["id"]
        rend1 = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta1_id]})
        self.assertEqual(rend1.status_code, 201, rend1.get_data(as_text=True))
        self.assertEqual(rend1.get_json()["total"], 90000.0)

        r2 = self._agregar_boleta(gasto["id"], monto="90000", numero_documento="F-2")
        self.assertEqual(r2.status_code, 201, r2.get_data(as_text=True))
        self.assertEqual(r2.get_json()["monto_pendiente"], 0)
        boleta2_id = next(b["id"] for b in r2.get_json()["boletas"] if b["numero_documento"] == "F-2")

        rend2 = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta2_id]})
        self.assertEqual(rend2.status_code, 201, rend2.get_data(as_text=True))
        self.assertEqual(rend2.get_json()["total"], 90000.0)

    def test_no_se_puede_rendir_dos_veces_la_misma_boleta(self):
        gasto = self._crear_gasto(monto="1000").get_json()
        r1 = self._agregar_boleta(gasto["id"], monto="1000", numero_documento="F-1")
        boleta_id = r1.get_json()["boletas"][0]["id"]
        rend1 = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_id]})
        self.assertEqual(rend1.status_code, 201, rend1.get_data(as_text=True))

        rend2 = self.client.post("/api/caja/rendicion", json={"detalle_ids": [boleta_id]})
        self.assertEqual(rend2.status_code, 400, rend2.get_data(as_text=True))


class TestGastosPorOtExcluidoDelGav(_Base):
    def _marcar_entregada(self, orden_id, fecha_entrega_real):
        db = taller_app._conectar_db()
        db.execute(
            "UPDATE ordenes_trabajo SET estado = 'entregado', fecha_entrega_real = ? WHERE id = ?",
            (fecha_entrega_real, orden_id),
        )
        db.commit()
        db.close()

    def test_gastos_por_ot_no_suma_en_gav_pero_se_cuenta_como_costo_de_la_ot(self):
        cliente = self._crear_cliente("CLIENTE ESTADO RESULTADOS")
        orden = self._crear_orden(cliente["id"])
        # Mes propio para no mezclarse con otros tests de este archivo. El
        # retiro/boleta puede quedar registrado en cualquier fecha -- lo que
        # importa para la atribución del costo a la OT es el mes en que la
        # OT se ENTREGÓ (fecha_entrega_real), igual criterio que
        # ventas_y_costo_ot_mes usa para repuestos/mano de obra.
        mes = "2031-08"
        self._marcar_entregada(orden["id"], f"{mes}-20T10:00:00")

        g1 = self._crear_gasto(monto="5000", fecha=f"{mes}-10").get_json()
        self._agregar_boleta(g1["id"], monto="5000", rubro_gav="otros", numero_documento="F-1")

        g2 = self._crear_gasto(monto="7000", fecha=f"{mes}-11").get_json()
        self._agregar_boleta(
            g2["id"], monto="7000", rubro_gav="gastos_por_ot",
            orden_id=str(orden["id"]), numero_documento="F-2",
        )

        resp = self.client.get(f"/api/finanzas/estado-resultados?mes={mes}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        gav = data["gav"]

        # El rubro "otros" sí queda en el GAV; "gastos_por_ot" nunca aparece
        # ahí -- se cuenta como costo de ventas de la OT a la que quedó
        # ligado, no como gasto de administración y ventas.
        self.assertIn("otros", gav["por_rubro"])
        self.assertNotIn("gastos_por_ot", gav["por_rubro"])
        self.assertNotIn("gastos_por_ot", gav["rubros_label"])

        self.assertEqual(data["costo_ventas"]["costo_caja_chica_ot"], 7000.0)

    def test_rubros_label_no_incluye_gastos_por_ot(self):
        resp = self.client.get("/api/finanzas/estado-resultados?mes=2031-08")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rubros_label = resp.get_json()["gav"]["rubros_label"]
        self.assertNotIn("gastos_por_ot", rubros_label)
        self.assertIn("otros", rubros_label)


class TestGastosPorOtQuedaComoCostoDeLaOt(_Base):
    """Cubre el pedido del taller: 'ese gasto por ot debería haber quedado
    asociado a la orden de trabajo ... y debería quedar en un nuevo costo
    como caja chica en tipo dentro de la pestaña costos con su costo
    asociado y la factura a la cual esta asociado' -- ver GET de una orden
    (_orden_dict) y su campo costos_caja_chica / costo_caja_chica."""

    def test_boleta_gastos_por_ot_aparece_en_el_detalle_de_la_orden(self):
        cliente = self._crear_cliente("CLIENTE COSTO OT")
        orden = self._crear_orden(cliente["id"])

        gasto = self._crear_gasto(monto="150000", fecha="2026-07-05").get_json()
        resp_boleta = self._agregar_boleta(
            gasto["id"], monto="150000", rubro_gav="gastos_por_ot",
            orden_id=str(orden["id"]), numero_documento="FACT-2001",
            descripcion="Repuesto comprado en efectivo",
        )
        self.assertEqual(resp_boleta.status_code, 201, resp_boleta.get_data(as_text=True))

        resp_orden = self.client.get(f"/api/operativo/ordenes/{orden['id']}")
        self.assertEqual(resp_orden.status_code, 200, resp_orden.get_data(as_text=True))
        d = resp_orden.get_json()

        self.assertEqual(d["costo_caja_chica"], 150000.0)
        self.assertEqual(len(d["costos_caja_chica"]), 1)
        boleta = d["costos_caja_chica"][0]
        self.assertEqual(boleta["numero_documento"], "FACT-2001")
        self.assertEqual(boleta["monto"], 150000.0)
        self.assertEqual(boleta["descripcion"], "REPUESTO COMPRADO EN EFECTIVO")

        # La utilidad estimada de la orden resta el costo de caja chica igual
        # que resta repuestos/mano de obra/convertidores.
        self.assertEqual(
            d["utilidad_estimada"],
            round(d["valor_neto"] - d["costo_repuestos"] - d["mano_obra_cargada"]
                  - d["costo_convertidores"] - d["costo_caja_chica"], 2),
        )

    def test_boleta_sin_orden_no_aparece_en_costos_de_otra_ot(self):
        cliente = self._crear_cliente("CLIENTE COSTO OT 2")
        orden_a = self._crear_orden(cliente["id"])
        orden_b = self._crear_orden(cliente["id"])

        gasto = self._crear_gasto(monto="30000", fecha="2026-07-06").get_json()
        self._agregar_boleta(
            gasto["id"], monto="30000", rubro_gav="gastos_por_ot",
            orden_id=str(orden_a["id"]), numero_documento="FACT-2002",
        )

        resp_b = self.client.get(f"/api/operativo/ordenes/{orden_b['id']}")
        self.assertEqual(resp_b.status_code, 200, resp_b.get_data(as_text=True))
        d_b = resp_b.get_json()
        self.assertEqual(d_b["costo_caja_chica"], 0.0)
        self.assertEqual(d_b["costos_caja_chica"], [])


class TestCajaVaciarSoloAdmin(_Base):
    """POST /api/admin/caja/vaciar borra TODO Caja Chica (gastos, boletas,
    rendiciones) de una sola vez -- pensado solo para volver a un estado
    limpio mientras se prueba el módulo, nunca para uso normal. No tenía
    ningún test que confirmara ni (a) que de verdad borra todo, ni (b) que
    un usuario sin rol admin no puede llamarlo directo a la API aunque el
    botón esté escondido en la interfaz (ver @requiere_rol() sin argumentos
    en modulos/finanzas.py -- "sin argumentos" significa "solo admin")."""

    def _crear_usuario_no_admin(self, modulos=None, password="clave123456"):
        username = "caja_vaciar_no_admin"
        resp = self.client.post(
            "/api/admin/usuarios",
            json={
                "username": username, "password": password, "nombre": username,
                "es_admin": False, "modulos": modulos or ["finanzas"],
            },
        )
        self.assertIn(resp.status_code, (200, 201), resp.get_data(as_text=True))
        cliente_no_admin = taller_app.app.test_client()
        resp_login = cliente_no_admin.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp_login.status_code, 302, resp_login.get_data(as_text=True))
        return cliente_no_admin

    def test_usuario_con_modulo_finanzas_pero_sin_ser_admin_no_puede_vaciar_caja(self):
        gasto = self._crear_gasto(monto="5000", fecha="2026-07-01").get_json()
        self._agregar_boleta(gasto["id"], monto="5000", numero_documento="F-NOBORRAR", rubro_gav="otros")

        cliente_finanzas = self._crear_usuario_no_admin(modulos=["finanzas"])
        resp = cliente_finanzas.post("/api/admin/caja/vaciar")
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))

        # No basta con que devuelva 403 -- hay que confirmar que el efecto
        # (borrar todo) tampoco ocurrió: el gasto sigue existiendo.
        resp_pendientes = self.client.get("/api/caja/pendientes")
        self.assertEqual(resp_pendientes.status_code, 200, resp_pendientes.get_data(as_text=True))
        ids_pendientes = [g["id"] for g in resp_pendientes.get_json()["gastos"]]
        self.assertIn(gasto["id"], ids_pendientes)

    def test_admin_vacia_caja_por_completo_y_devuelve_los_conteos(self):
        # Ojo: esta clase comparte la base de datos con TODAS las demás
        # clases de este archivo (un solo setUpClass a nivel de módulo,
        # sin reset entre tests) -- así que a esta altura ya puede haber
        # gastos/boletas de tests anteriores. Por eso los conteos que
        # devuelve /vaciar se comparan "al menos los 2 que agregamos acá",
        # no con un número exacto -- lo exacto y confiable de verificar es
        # que, DESPUÉS de vaciar, no quede ningún pendiente.
        gasto_1 = self._crear_gasto(monto="10000", fecha="2026-07-01").get_json()
        self._agregar_boleta(gasto_1["id"], monto="10000", numero_documento="F-VAC-1", rubro_gav="otros")
        gasto_2 = self._crear_gasto(monto="20000", fecha="2026-07-02").get_json()
        self._agregar_boleta(gasto_2["id"], monto="20000", numero_documento="F-VAC-2", rubro_gav="otros")

        resp_vaciar = self.client.post("/api/admin/caja/vaciar")
        self.assertEqual(resp_vaciar.status_code, 200, resp_vaciar.get_data(as_text=True))
        d = resp_vaciar.get_json()
        self.assertTrue(d["ok"])
        self.assertGreaterEqual(d["boletas_eliminadas"], 2)
        self.assertGreaterEqual(d["movimientos_eliminados"], 2)

        resp_pendientes = self.client.get("/api/caja/pendientes")
        self.assertEqual(resp_pendientes.status_code, 200, resp_pendientes.get_data(as_text=True))
        self.assertEqual(resp_pendientes.get_json()["gastos"], [])


if __name__ == "__main__":
    unittest.main()
