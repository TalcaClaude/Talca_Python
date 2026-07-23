"""
Módulo Facturación Electrónica — emisión de Documentos Tributarios
Electrónicos (DTE) hacia clientes (factura afecta y nota de crédito, por
ahora), NO confundir con la pestaña "Facturas" de Finanzas, que son
facturas de COMPRA a proveedores (Cuentas por Pagar).

Diseño clave: el resto del sistema (endpoints, base de datos, frontend)
nunca habla directo con el proveedor de facturación electrónica (hoy
SimpleAPI) -- todos pasan por la interfaz EmisorDTE (ver más abajo). Si el
taller se cambia de proveedor el día de mañana, se escribe una clase nueva
que cumpla esa interfaz y se cambia UNA variable de entorno (PROVEEDOR_DTE)
-- nada del resto del código, ni la base de datos, ni el frontend, se
tocan. Es la misma razón por la que, por ejemplo, _aplicar_entrada_stock
vive en core.py en vez de repetirse en cada módulo.

Empieza en modo "simulado" (PROVEEDOR_DTE=simulado, el default): no manda
nada al SII, genera un folio falso, y sirve para construir y probar todo
el flujo (formulario, cálculo de IVA, guardado en base de datos, historial)
sin necesitar todavía una cuenta real. Cuando el taller tenga su cuenta en
el proveedor elegido y su API Key, se cambia PROVEEDOR_DTE en Railway y se
termina de conectar la clase real (ver EmisorSimpleAPI más abajo -- hoy
lanza un error explicando qué falta, a propósito, para no adivinar el
formato exacto de la API y mandar datos mal armados al SII).
"""

import json
import os
from datetime import datetime

from flask import Blueprint, jsonify, request

from core import (
    IVA_PORCENTAJE,
    coincide_busqueda,
    formatear_rut,
    get_db,
    now_iso,
    requiere_rol,
    usuario_actual,
)

bp = Blueprint("facturacion", __name__)

# Tipos de DTE que este módulo sabe emitir hoy. Boleta electrónica (39) y
# otros documentos se pueden agregar después siguiendo el mismo patrón --
# no es una limitación del diseño, es simplemente lo que el taller pidió
# para partir (ver conversación de la Migración 28).
TIPOS_DTE = {33: "Factura electrónica", 61: "Nota de crédito electrónica"}


# ---------------------------------------------------------------------------
# Capa de emisión -- ver comentario del módulo. Cualquier endpoint de acá
# abajo llama SOLO a get_emisor_dte(), nunca a una clase concreta.
# ---------------------------------------------------------------------------

class EmisorDTEError(Exception):
    """Cualquier problema al emitir: credenciales faltantes, el proveedor
    rechazó el documento, error de red, etc. Los endpoints la capturan y la
    devuelven como error 502 con el mensaje tal cual (son mensajes ya
    pensados para mostrarse al usuario)."""


class EmisorDTE:
    """Contrato que cualquier proveedor de facturación electrónica tiene
    que cumplir. `datos` es siempre un dict con: receptor (rut,
    razon_social, giro, direccion, comuna), items (lista de {nombre,
    cantidad, precio_unitario}), monto_neto, monto_iva, monto_total,
    observaciones. Debe devolver un dict con al menos {folio, pdf_url,
    respuesta_cruda} o levantar EmisorDTEError."""
    nombre = "base"

    def emitir_factura(self, datos):
        raise NotImplementedError

    def emitir_nota_credito(self, datos, documento_referencia):
        """documento_referencia es el dict ya guardado en
        documentos_tributarios de la factura que se está anulando/corrigiendo
        -- el proveedor necesita su folio y tipo para armar la referencia
        que exige el SII en toda nota de crédito."""
        raise NotImplementedError


class EmisorSimulado(EmisorDTE):
    """Proveedor 'de mentira': no manda nada al SII. Sirve para desarrollar
    y probar el resto del flujo (formulario, cálculo, guardado, historial,
    anulación) sin depender todavía de una cuenta real. Es el default
    (PROVEEDOR_DTE sin definir, o definida como 'simulado')."""
    nombre = "simulado"

    def _folio_falso(self):
        # No es un folio real del SII -- solo sirve para que el resto del
        # flujo (guardar, mostrar en el historial) tenga algo que mostrar.
        return int(datetime.now().timestamp()) % 100000

    def emitir_factura(self, datos):
        return {
            "ok": True,
            "folio": self._folio_falso(),
            "pdf_url": "",
            "respuesta_cruda": {"simulado": True, "datos": datos},
        }

    def emitir_nota_credito(self, datos, documento_referencia):
        return {
            "ok": True,
            "folio": self._folio_falso(),
            "pdf_url": "",
            "respuesta_cruda": {
                "simulado": True, "datos": datos,
                "anula_folio": documento_referencia.get("folio"),
            },
        }


class EmisorSimpleAPI(EmisorDTE):
    """Integración real con SimpleAPI (simpleapi.cl) -- proveedor elegido
    para partir: tiene plan gratis (500 documentos/mes durante 12 meses) y
    los planes pagados usan la MISMA API/misma API Key, solo cambia el
    límite mensual contratado -- así que subir de plan el día de mañana no
    requiere tocar nada de este código.

    OJO -- pendiente de terminar: el endpoint exacto y el formato del JSON
    de acá abajo todavía no están confirmados contra la documentación real
    de SimpleAPI (queda accesible recién con una cuenta/API Key propia, vía
    Postman). A propósito esta clase lanza EmisorDTEError en vez de mandar
    una request "adivinada" que podría fallar silenciosamente contra el
    SII. Una vez que el taller tenga su cuenta y comparta el API Key (como
    variable de entorno en Railway, nunca en el código ni en el chat) y la
    documentación específica, se completa emitir_factura/emitir_nota_credito
    acá -- el resto del sistema no necesita ningún otro cambio."""
    nombre = "simpleapi"
    BASE_URL = "https://api.simpleapi.cl"  # TODO: confirmar contra la doc real (Postman)

    def __init__(self):
        self.token = os.environ.get("SIMPLEAPI_TOKEN", "")
        self.ambiente = os.environ.get("SIMPLEAPI_AMBIENTE", "certificacion")
        if not self.token:
            raise EmisorDTEError(
                "Falta configurar SIMPLEAPI_TOKEN en las variables de entorno de Railway. "
                "Crea tu cuenta gratis en simpleapi.cl, genera tu API Key ahí y agrégala como "
                "variable de entorno (nunca la pegues en el chat ni la subas a GitHub)."
            )

    def emitir_factura(self, datos):
        raise EmisorDTEError(
            "La integración con SimpleAPI todavía no está terminada de conectar -- falta confirmar "
            "el endpoint y el formato exacto del JSON contra su documentación (Postman), una vez que "
            "tengas tu cuenta y API Key. Mientras tanto usa PROVEEDOR_DTE=simulado para seguir "
            "probando el resto del flujo (formulario, historial, anulación)."
        )

    def emitir_nota_credito(self, datos, documento_referencia):
        raise EmisorDTEError(
            "La integración con SimpleAPI todavía no está terminada de conectar -- ver "
            "emitir_factura para el detalle de qué falta."
        )


def get_emisor_dte():
    """Único punto del sistema que decide QUÉ proveedor se usa -- todo lo
    demás (endpoints de este módulo) solo conoce la interfaz EmisorDTE.
    Cambiar de proveedor es cambiar PROVEEDOR_DTE en Railway y agregar la
    clase nueva acá arriba; no hay que tocar endpoints, base de datos ni
    frontend."""
    proveedor = (os.environ.get("PROVEEDOR_DTE") or "simulado").strip().lower()
    if proveedor == "simpleapi":
        return EmisorSimpleAPI()
    return EmisorSimulado()


# ---------------------------------------------------------------------------
# Helpers de cálculo / validación
# ---------------------------------------------------------------------------

def _calcular_montos(items):
    """IVA_PORCENTAJE (0.19) ya se usa igual en el resto del sistema (ver
    core.py) -- una factura afecta siempre desglosa neto + IVA. El neto de
    cada línea se redondea antes de sumar (igual que hace el SII), para que
    el total coincida centavo a centavo con lo que después va a timbrar el
    proveedor."""
    neto = 0
    items_normalizados = []
    for item in items:
        nombre = (item.get("nombre") or "").strip()
        if not nombre:
            raise ValueError("Cada ítem necesita un nombre/descripción")
        try:
            cantidad = float(item.get("cantidad"))
            precio_unitario = float(item.get("precio_unitario"))
            if cantidad <= 0 or precio_unitario < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"Cantidad o precio unitario inválido en el ítem \"{nombre}\"")
        subtotal = round(cantidad * precio_unitario, 2)
        neto += subtotal
        items_normalizados.append({
            "nombre": nombre, "cantidad": cantidad,
            "precio_unitario": precio_unitario, "subtotal": subtotal,
        })
    neto = round(neto, 2)
    iva = round(neto * IVA_PORCENTAJE, 2)
    total = round(neto + iva, 2)
    return items_normalizados, neto, iva, total


def _receptor_valido(data):
    receptor = data.get("receptor") or {}
    rut = formatear_rut((receptor.get("rut") or "").strip())
    razon_social = (receptor.get("razon_social") or "").strip().upper()
    if not rut or not razon_social:
        raise ValueError("El receptor necesita al menos RUT y razón social")
    return {
        "rut": rut,
        "razon_social": razon_social,
        "giro": (receptor.get("giro") or "").strip().upper(),
        "direccion": (receptor.get("direccion") or "").strip().upper(),
        "comuna": (receptor.get("comuna") or "").strip().upper(),
    }


def _documento_dict(row):
    d = dict(row)
    d["tipo_dte_label"] = TIPOS_DTE.get(d["tipo_dte"], str(d["tipo_dte"]))
    return d


# ---------------------------------------------------------------------------
# Configuración del emisor (datos legales del taller para el encabezado de
# cada DTE) -- son datos públicos del taller, no credenciales, así que
# viven en la base de datos (a diferencia del API Key del proveedor, que
# siempre va como variable de entorno, nunca en la base de datos ni en el
# código). Solo admin puede editarlos: son los datos que van impresos en
# cada factura.
# ---------------------------------------------------------------------------

@bp.route("/api/facturacion/configuracion", methods=["GET"])
@requiere_rol("finanzas")
def facturacion_ver_configuracion():
    db = get_db()
    fila = db.execute("SELECT * FROM configuracion_facturacion WHERE id = 1").fetchone()
    config = dict(fila) if fila else {
        "razon_social": "", "rut_emisor": "", "giro": "",
        "direccion": "", "comuna": "", "actividad_economica": "",
    }
    config["proveedor_dte"] = (os.environ.get("PROVEEDOR_DTE") or "simulado").strip().lower()
    return jsonify(config)


@bp.route("/api/facturacion/configuracion", methods=["PUT"])
@requiere_rol()  # admin -- son los datos legales que van impresos en cada factura
def facturacion_guardar_configuracion():
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    campos = {
        "razon_social": (data.get("razon_social") or "").strip().upper(),
        "rut_emisor": formatear_rut(data.get("rut_emisor") or ""),
        "giro": (data.get("giro") or "").strip().upper(),
        "direccion": (data.get("direccion") or "").strip().upper(),
        "comuna": (data.get("comuna") or "").strip().upper(),
        "actividad_economica": (data.get("actividad_economica") or "").strip().upper(),
    }
    if not campos["razon_social"] or not campos["rut_emisor"]:
        return jsonify({"error": "Razón social y RUT del emisor son obligatorios"}), 400
    ts = now_iso()
    db.execute(
        """
        INSERT INTO configuracion_facturacion
            (id, razon_social, rut_emisor, giro, direccion, comuna, actividad_economica, actualizado_en)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            razon_social = excluded.razon_social, rut_emisor = excluded.rut_emisor,
            giro = excluded.giro, direccion = excluded.direccion, comuna = excluded.comuna,
            actividad_economica = excluded.actividad_economica, actualizado_en = excluded.actualizado_en
        """,
        (*campos.values(), ts),
    )
    db.commit()
    return jsonify({"ok": True, **campos})


# ---------------------------------------------------------------------------
# Emisión y listado de documentos
# ---------------------------------------------------------------------------

@bp.route("/api/facturacion/documentos", methods=["GET"])
@requiere_rol("finanzas")
def facturacion_listar_documentos():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    tipo = request.args.get("tipo", "").strip()
    estado = request.args.get("estado", "").strip()

    sql = "SELECT * FROM documentos_tributarios WHERE 1=1"
    params = []
    if tipo:
        sql += " AND tipo_dte = ?"
        params.append(tipo)
    if estado:
        sql += " AND estado = ?"
        params.append(estado)
    sql += " ORDER BY creado_en DESC"
    rows = db.execute(sql, params).fetchall()
    documentos = [_documento_dict(r) for r in rows]

    if q:
        documentos = [
            d for d in documentos
            if coincide_busqueda(q, d.get("receptor_razon_social"), d.get("receptor_rut"), str(d.get("folio") or ""))
        ]

    return jsonify({"documentos": documentos, "total": len(documentos)})


@bp.route("/api/facturacion/documentos/<int:documento_id>", methods=["GET"])
@requiere_rol("finanzas")
def facturacion_ver_documento(documento_id):
    db = get_db()
    row = db.execute("SELECT * FROM documentos_tributarios WHERE id = ?", (documento_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Documento no encontrado"}), 404
    return jsonify(_documento_dict(row))


@bp.route("/api/facturacion/documentos", methods=["POST"])
@requiere_rol("finanzas")
def facturacion_emitir_factura():
    """Emite una factura electrónica (DTE 33). orden_id es opcional --
    solo queda guardado como referencia para poder ver después "esta OT ya
    tiene su factura", el sistema no vuelve a leer la OT acá (el frontend
    es quien trae los datos de la OT para prellenar el formulario, ver
    finanzas.js) -- así este módulo no depende del módulo Operativo."""
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}

    try:
        receptor = _receptor_valido(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    items = data.get("items") or []
    if not items:
        return jsonify({"error": "Agrega al menos un ítem a facturar"}), 400
    try:
        items_normalizados, neto, iva, total = _calcular_montos(items)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    orden_id = data.get("orden_id")
    try:
        orden_id = int(orden_id) if orden_id not in (None, "") else None
    except (TypeError, ValueError):
        orden_id = None
    observaciones = (data.get("observaciones") or "").strip()

    datos_emision = {
        "receptor": receptor, "items": items_normalizados,
        "monto_neto": neto, "monto_iva": iva, "monto_total": total,
        "observaciones": observaciones,
    }

    ts = now_iso()
    try:
        emisor = get_emisor_dte()
        resultado = emisor.emitir_factura(datos_emision)
    except EmisorDTEError as e:
        # Se guarda igual, como registro en estado 'error' -- así queda
        # trazabilidad de los intentos fallidos (útil mientras se termina
        # de conectar el proveedor real) en vez de perderse silenciosamente.
        cur = db.execute(
            _INSERT_DOCUMENTO_SQL,
            _valores_documento(
                tipo_dte=33, estado="error", folio=None, orden_id=orden_id,
                documento_referencia_id=None, motivo_nc="", receptor=receptor,
                items_normalizados=items_normalizados, neto=neto, iva=iva, total=total,
                observaciones=observaciones, proveedor=get_emisor_dte_nombre_seguro(),
                respuesta_proveedor="", pdf_url="", error_mensaje=str(e), ts=ts, emitido_en=None,
            ),
        )
        db.commit()
        return jsonify({"error": str(e), "documento_id": cur.lastrowid}), 502

    cur = db.execute(
        _INSERT_DOCUMENTO_SQL,
        _valores_documento(
            tipo_dte=33, estado="emitido", folio=resultado.get("folio"), orden_id=orden_id,
            documento_referencia_id=None, motivo_nc="", receptor=receptor,
            items_normalizados=items_normalizados, neto=neto, iva=iva, total=total,
            observaciones=observaciones, proveedor=emisor.nombre,
            respuesta_proveedor=str(resultado.get("respuesta_cruda") or ""),
            pdf_url=resultado.get("pdf_url") or "", error_mensaje="", ts=ts, emitido_en=ts,
        ),
    )
    db.commit()
    documento = db.execute("SELECT * FROM documentos_tributarios WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_documento_dict(documento)), 201


@bp.route("/api/facturacion/documentos/<int:documento_id>/nota-credito", methods=["POST"])
@requiere_rol("finanzas")
def facturacion_emitir_nota_credito(documento_id):
    """Anula (o corrige) una factura ya emitida con una nota de crédito
    (DTE 61) que la referencia -- es la única forma válida de "deshacer"
    un documento tributario ya tributado ante el SII, nunca se puede
    simplemente borrar."""
    db = get_db()
    documento = db.execute("SELECT * FROM documentos_tributarios WHERE id = ?", (documento_id,)).fetchone()
    if documento is None:
        return jsonify({"error": "Documento no encontrado"}), 404
    if documento["tipo_dte"] != 33:
        return jsonify({"error": "Solo se puede generar una nota de crédito sobre una factura"}), 400
    if documento["estado"] != "emitido":
        return jsonify({"error": "Esta factura no está en estado 'emitido' -- no hay nada que anular"}), 400
    ya_anulada = db.execute(
        "SELECT id FROM documentos_tributarios WHERE documento_referencia_id = ? AND estado = 'emitido'",
        (documento_id,),
    ).fetchone()
    if ya_anulada:
        return jsonify({"error": "Esta factura ya tiene una nota de crédito emitida"}), 409

    data = request.get_json(force=True, silent=True) or {}
    motivo = (data.get("motivo") or "").strip()
    if not motivo:
        return jsonify({"error": "Indica el motivo de la nota de crédito"}), 400

    receptor = {
        "rut": documento["receptor_rut"], "razon_social": documento["receptor_razon_social"],
        "giro": documento["receptor_giro"], "direccion": documento["receptor_direccion"],
        "comuna": documento["receptor_comuna"],
    }
    datos_emision = {
        "receptor": receptor, "items": [], "monto_neto": documento["monto_neto"],
        "monto_iva": documento["monto_iva"], "monto_total": documento["monto_total"],
        "observaciones": motivo,
    }

    ts = now_iso()
    try:
        emisor = get_emisor_dte()
        resultado = emisor.emitir_nota_credito(datos_emision, dict(documento))
    except EmisorDTEError as e:
        return jsonify({"error": str(e)}), 502

    cur = db.execute(
        _INSERT_DOCUMENTO_SQL,
        _valores_documento(
            tipo_dte=61, estado="emitido", folio=resultado.get("folio"), orden_id=documento["orden_id"],
            documento_referencia_id=documento_id, motivo_nc=motivo, receptor=receptor,
            items_normalizados=[], neto=documento["monto_neto"], iva=documento["monto_iva"],
            total=documento["monto_total"], observaciones=motivo, proveedor=emisor.nombre,
            respuesta_proveedor=str(resultado.get("respuesta_cruda") or ""),
            pdf_url=resultado.get("pdf_url") or "", error_mensaje="", ts=ts, emitido_en=ts,
        ),
    )
    db.execute("UPDATE documentos_tributarios SET estado = 'anulado' WHERE id = ?", (documento_id,))
    db.commit()
    nota_credito = db.execute("SELECT * FROM documentos_tributarios WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_documento_dict(nota_credito)), 201


def get_emisor_dte_nombre_seguro():
    """Como get_emisor_dte() puede lanzar EmisorDTEError (ej. falta el API
    Key), este helper la vuelve a intentar solo para dejar registrado QUÉ
    proveedor se intentó usar en el documento que quedó en estado 'error'
    -- nunca debería fallar dos veces seguidas por lo mismo, pero si pasa,
    se guarda el nombre del proveedor configurado igual."""
    try:
        return get_emisor_dte().nombre
    except EmisorDTEError:
        return (os.environ.get("PROVEEDOR_DTE") or "simulado").strip().lower()


_INSERT_DOCUMENTO_SQL = """
    INSERT INTO documentos_tributarios (
        tipo_dte, estado, folio, orden_id, documento_referencia_id, motivo_nc,
        receptor_rut, receptor_razon_social, receptor_giro, receptor_direccion, receptor_comuna,
        items_json, monto_neto, monto_iva, monto_total, observaciones,
        proveedor, respuesta_proveedor, pdf_url, error_mensaje, usuario, creado_en, emitido_en
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _valores_documento(tipo_dte, estado, folio, orden_id, documento_referencia_id, motivo_nc,
                        receptor, items_normalizados, neto, iva, total, observaciones,
                        proveedor, respuesta_proveedor, pdf_url, error_mensaje, ts, emitido_en):
    return (
        tipo_dte, estado, folio, orden_id, documento_referencia_id, motivo_nc,
        receptor["rut"], receptor["razon_social"], receptor["giro"], receptor["direccion"], receptor["comuna"],
        json.dumps(items_normalizados, ensure_ascii=False), neto, iva, total, observaciones,
        proveedor, respuesta_proveedor, pdf_url, error_mensaje, usuario_actual(), ts, emitido_en,
    )
