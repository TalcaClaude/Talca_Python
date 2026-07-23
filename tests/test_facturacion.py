"""Tests de Facturación Electrónica (Migración 28, modulos/facturacion.py).

Cubre el flujo con el proveedor 'simulado' (default de PROVEEDOR_DTE) --
el mismo que se prueba acá es el que va a usar el taller para probar el
formulario, el cálculo de IVA y el historial ANTES de tener una cuenta real
con un proveedor de facturación electrónica. No prueba contra un proveedor
real (SimpleAPI u otro) porque esa integración todavía no está conectada a
propósito -- ver el comentario de EmisorSimpleAPI en facturacion.py.
"""
import os
import sys
import tempfile
import unittest

_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(prefix="test_taller_facturacion_", suffix=".db")
os.close(_TMP_DB_FD)
os.remove(_TMP_DB_PATH)
os.environ["DB_PATH"] = _TMP_DB_PATH
os.environ.setdefault("SECRET_KEY", "clave-de-pruebas")
os.environ.pop("PROVEEDOR_DTE", None)  # asegura modo 'simulado' (el default)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as taller_app  # noqa: E402


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        taller_app.app.config["TESTING"] = True
        cls.client = taller_app.app.test_client()
        cls.client.post("/login", data={"username": "admin", "password": "taller2026"})

    def _emitir_factura(self, **campos):
        base = {
            "receptor": {"rut": "12345678-9", "razon_social": "Cliente Test SpA", "giro": "Comercio"},
            "items": [{"nombre": "Reparación", "cantidad": 1, "precio_unitario": 10000}],
        }
        base.update(campos)
        return self.client.post("/api/facturacion/documentos", json=base)


class TestConfiguracionEmisor(_Base):
    def test_configuracion_por_defecto_viene_en_modo_simulado(self):
        resp = self.client.get("/api/facturacion/configuracion")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["proveedor_dte"], "simulado")

    def test_guardar_y_leer_configuracion(self):
        resp = self.client.put("/api/facturacion/configuracion", json={
            "razon_social": "Taller Mecánico Ejemplo SpA",
            "rut_emisor": "770893703",
            "giro": "Mantención y reparación de vehículos",
            "direccion": "Av. Siempre Viva 123",
            "comuna": "Santiago",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["rut_emisor"], "77.089.370-3")  # queda formateado

        resp2 = self.client.get("/api/facturacion/configuracion")
        config = resp2.get_json()
        self.assertEqual(config["razon_social"], "TALLER MECÁNICO EJEMPLO SPA")
        self.assertEqual(config["comuna"], "SANTIAGO")

    def test_guardar_configuracion_sin_razon_social_es_rechazado(self):
        resp = self.client.put("/api/facturacion/configuracion", json={"rut_emisor": "770893703"})
        self.assertEqual(resp.status_code, 400)


class TestEmisionFactura(_Base):
    def test_emitir_factura_calcula_iva_y_queda_emitida(self):
        resp = self._emitir_factura(items=[{"nombre": "Cambio de aceite", "cantidad": 2, "precio_unitario": 5000}])
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        d = resp.get_json()
        self.assertEqual(d["tipo_dte"], 33)
        self.assertEqual(d["estado"], "emitido")
        self.assertIsNotNone(d["folio"])
        self.assertEqual(d["proveedor"], "simulado")
        self.assertAlmostEqual(d["monto_neto"], 10000, delta=0.01)
        self.assertAlmostEqual(d["monto_iva"], 1900, delta=0.01)  # 19% de 10000
        self.assertAlmostEqual(d["monto_total"], 11900, delta=0.01)

    def test_emitir_factura_sin_rut_receptor_es_rechazado(self):
        resp = self._emitir_factura(receptor={"razon_social": "Sin RUT SpA"})
        self.assertEqual(resp.status_code, 400)

    def test_emitir_factura_sin_items_es_rechazado(self):
        resp = self._emitir_factura(items=[])
        self.assertEqual(resp.status_code, 400)

    def test_emitir_factura_con_item_sin_nombre_es_rechazado(self):
        resp = self._emitir_factura(items=[{"nombre": "", "cantidad": 1, "precio_unitario": 1000}])
        self.assertEqual(resp.status_code, 400)

    def test_factura_emitida_aparece_en_el_listado(self):
        resp = self._emitir_factura(receptor={"rut": "9-8", "razon_social": "Buscar Este Cliente"})
        doc_id = resp.get_json()["id"]
        listado = self.client.get("/api/facturacion/documentos?q=Buscar Este Cliente").get_json()
        ids = [d["id"] for d in listado["documentos"]]
        self.assertIn(doc_id, ids)

    def test_ver_documento_individual(self):
        resp = self._emitir_factura()
        doc_id = resp.get_json()["id"]
        detalle = self.client.get(f"/api/facturacion/documentos/{doc_id}")
        self.assertEqual(detalle.status_code, 200)
        self.assertEqual(detalle.get_json()["id"], doc_id)

    def test_documento_inexistente_da_404(self):
        resp = self.client.get("/api/facturacion/documentos/999999")
        self.assertEqual(resp.status_code, 404)


class TestNotaDeCredito(_Base):
    def test_anular_factura_con_nota_de_credito(self):
        factura = self._emitir_factura().get_json()

        resp_nc = self.client.post(
            f"/api/facturacion/documentos/{factura['id']}/nota-credito",
            json={"motivo": "Anula por error en el monto"},
        )
        self.assertEqual(resp_nc.status_code, 201, resp_nc.get_data(as_text=True))
        nc = resp_nc.get_json()
        self.assertEqual(nc["tipo_dte"], 61)
        self.assertEqual(nc["documento_referencia_id"], factura["id"])
        self.assertEqual(nc["estado"], "emitido")

        # La factura original queda anulada.
        factura_actualizada = self.client.get(f"/api/facturacion/documentos/{factura['id']}").get_json()
        self.assertEqual(factura_actualizada["estado"], "anulado")

    def test_nota_de_credito_sin_motivo_es_rechazada(self):
        factura = self._emitir_factura().get_json()
        resp = self.client.post(f"/api/facturacion/documentos/{factura['id']}/nota-credito", json={})
        self.assertEqual(resp.status_code, 400)

    def test_no_se_puede_anular_dos_veces_la_misma_factura(self):
        factura = self._emitir_factura().get_json()
        self.client.post(f"/api/facturacion/documentos/{factura['id']}/nota-credito", json={"motivo": "primera"})
        resp2 = self.client.post(f"/api/facturacion/documentos/{factura['id']}/nota-credito", json={"motivo": "segunda"})
        self.assertEqual(resp2.status_code, 400)  # ya no está 'emitido', quedó 'anulado'

    def test_no_se_puede_generar_nota_de_credito_sobre_otra_nota_de_credito(self):
        factura = self._emitir_factura().get_json()
        nc = self.client.post(
            f"/api/facturacion/documentos/{factura['id']}/nota-credito", json={"motivo": "primera"}
        ).get_json()
        resp = self.client.post(f"/api/facturacion/documentos/{nc['id']}/nota-credito", json={"motivo": "otra"})
        self.assertEqual(resp.status_code, 400)


class TestPdfYEnvio(_Base):
    def test_descarga_pdf_de_documento_emitido(self):
        factura = self._emitir_factura().get_json()
        resp = self.client.get(f"/api/facturacion/documentos/{factura['id']}/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/pdf")
        self.assertTrue(resp.data.startswith(b"%PDF"))

    def test_pdf_de_documento_inexistente_da_404(self):
        resp = self.client.get("/api/facturacion/documentos/999999/pdf")
        self.assertEqual(resp.status_code, 404)

    def test_pdf_no_se_rompe_con_un_tag_sin_cerrar_en_observaciones(self):
        # Auditoría de julio 2026: un "<" suelto sin cerrar en
        # 'observaciones' (ej. "revisar <valor bomba de agua") hacía que
        # ReportLab reventara al generar el PDF (interpreta un mini-lenguaje
        # tipo XML dentro de un Paragraph), dejando ese documento tributario
        # -- inmutable una vez emitido -- sin poder verse/descargarse/
        # enviarse nunca más. Se corrigió escapando el texto libre antes de
        # interpolarlo.
        factura = self._emitir_factura(
            observaciones="Revisar <valor bomba de agua en proxima visita"
        ).get_json()
        resp = self.client.get(f"/api/facturacion/documentos/{factura['id']}/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b"%PDF"))

    def test_pdf_de_nota_de_credito_no_se_rompe_con_tag_sin_cerrar_en_motivo(self):
        factura = self._emitir_factura().get_json()
        nc = self.client.post(
            f"/api/facturacion/documentos/{factura['id']}/nota-credito",
            json={"motivo": "Cliente pidio <garantia extendida revisar en 6 meses"},
        ).get_json()
        resp = self.client.get(f"/api/facturacion/documentos/{nc['id']}/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b"%PDF"))

    def test_pdf_por_defecto_es_inline_no_se_descarga_solo(self):
        # Ver PDF en pestaña nueva / la vista previa en el <iframe> no deben
        # forzar una descarga -- solo el botón "Descargar PDF" (que agrega
        # ?descargar=1) debe hacerlo.
        factura = self._emitir_factura().get_json()
        resp = self.client.get(f"/api/facturacion/documentos/{factura['id']}/pdf")
        disposicion = resp.headers.get("Content-Disposition", "")
        self.assertNotIn("attachment", disposicion.lower())

    def test_pdf_con_descargar_fuerza_attachment(self):
        factura = self._emitir_factura().get_json()
        resp = self.client.get(f"/api/facturacion/documentos/{factura['id']}/pdf?descargar=1")
        disposicion = resp.headers.get("Content-Disposition", "")
        self.assertIn("attachment", disposicion.lower())

    def test_pdf_permite_ser_embebido_en_iframe_del_propio_sitio(self):
        # Las cabeceras de seguridad globales (X-Frame-Options: DENY,
        # frame-ancestors 'none', ver app.py) bloquean CUALQUIER framing,
        # incluso del propio sitio -- eso dejaba la vista previa en <iframe>
        # de finanzas.js/operativo.js siempre en blanco. Se relajan SOLO
        # para esta ruta, a 'self'/SAMEORIGIN.
        factura = self._emitir_factura().get_json()
        resp = self.client.get(f"/api/facturacion/documentos/{factura['id']}/pdf")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", resp.headers.get("Content-Security-Policy", ""))

    def test_resto_del_sitio_mantiene_x_frame_options_deny(self):
        resp = self.client.get("/api/facturacion/configuracion")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", resp.headers.get("Content-Security-Policy", ""))

    def test_enviar_sin_correo_registrado_ni_destinatario_da_error_claro(self):
        factura = self._emitir_factura(receptor={
            "rut": "12345678-9", "razon_social": "Sin Correo SpA", "giro": "Comercio",
        }).get_json()
        resp = self.client.post(f"/api/facturacion/documentos/{factura['id']}/enviar", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("correo", resp.get_json()["error"].lower())

    def test_enviar_falla_con_mensaje_claro_si_no_hay_transporte_de_correo_configurado(self):
        # En el entorno de pruebas no hay RESEND_API_KEY ni SMTP configurado
        # -- debe fallar de forma controlada (502 con detalle), no reventar.
        factura = self._emitir_factura(receptor={
            "rut": "12345678-9", "razon_social": "Con Correo SpA", "giro": "Comercio",
            "email": "cliente@correo.cl",
        }).get_json()
        resp = self.client.post(f"/api/facturacion/documentos/{factura['id']}/enviar", json={})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("correo", resp.get_json()["error"].lower())

    def test_enviar_permite_destinatario_distinto_al_registrado(self):
        factura = self._emitir_factura(receptor={
            "rut": "12345678-9", "razon_social": "Con Correo SpA", "giro": "Comercio",
            "email": "cliente@correo.cl",
        }).get_json()
        resp = self.client.post(
            f"/api/facturacion/documentos/{factura['id']}/enviar",
            json={"destinatario": "otro@correo.cl"},
        )
        # Sin transporte configurado también falla, pero lo importante acá
        # es que no se rechaza por validación -- si fallara por falta de
        # destinatario sería 400, acá debe ser 502 (falla en el envío).
        self.assertEqual(resp.status_code, 502)


class TestFiltroPorOrden(_Base):
    def test_listado_filtra_por_orden_id(self):
        self._emitir_factura(orden_id=None, receptor={"rut": "1-9", "razon_social": "Sin OT"})
        # No hay una OT real en esta base de pruebas (tabla vacía), así que
        # se prueba que el filtro no rompe y devuelve vacío para un orden_id
        # que no tiene ninguna factura asociada.
        resp = self.client.get("/api/facturacion/documentos?orden_id=999999")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["documentos"], [])

    def test_orden_id_invalido_no_rompe_el_listado(self):
        resp = self.client.get("/api/facturacion/documentos?orden_id=no-es-un-numero")
        self.assertEqual(resp.status_code, 200)

    def test_busqueda_por_texto_tambien_encuentra_por_numero_de_ot(self):
        # La tabla "Facturación electrónica" y su buscador (ver finanzas.js)
        # ahora muestran/filtran también por N° de OT, no solo por razón
        # social/RUT/folio -- para eso hace falta una OT real (FK).
        cliente = self.client.post("/api/operativo/clientes", json={"nombre": "Cliente Con OT"}).get_json()
        orden = self.client.post("/api/operativo/ordenes", json={"cliente_id": cliente["id"]}).get_json()
        factura = self._emitir_factura(orden_id=orden["id"], receptor={"rut": "1-9", "razon_social": "Cliente Con OT"}).get_json()

        resp = self.client.get(f"/api/facturacion/documentos?q={orden['id']}")
        ids = [d["id"] for d in resp.get_json()["documentos"]]
        self.assertIn(factura["id"], ids)


if __name__ == "__main__":
    unittest.main()
