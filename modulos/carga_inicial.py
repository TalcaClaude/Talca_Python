"""
Carga inicial del inventario desde Talca PHP (Administración, solo admin).

Recibe el "plan de carga" que arma informes/_scripts/preparar_carga.py a partir
del stock de producción de Talca PHP, el Excel de Matías y la caja chica
clasificada, y lo aplica sobre una base con el catálogo VACÍO:

  1. proveedores (reusa los que ya existan con el mismo nombre);
  2. productos con su stock repartido por proveedor (una entrada "Ajuste de
     inventario" por proveedor, motivo CARGA INICIAL DESDE TALCA PHP);
  3. caja chica histórica: un retiro por documento (efectivo del cajero) y
     una boleta por línea; las líneas REPUESTO ingresan su stock como compra
     de emergencia; al final una sola rendición con su reposición automática,
     para que el saldo de caja no cambie y nada quede pendiente de rendir.

Usa core._aplicar_entrada_stock, así que el costo promedio, los baldes por
proveedor y el stock mínimo quedan igual que si se hubiera cargado a mano.

"Ensayar" hace TODO dentro de una transacción, verifica los totales contra el
plan y la deshace. "Aplicar" hace lo mismo, respalda la base y confirma. Se
niega si ya hay productos o si la carga histórica de caja ya existe: no es una
herramienta para repetir, es para el día del corte (y para el ensayo en test).
"""

import json
import os
import sqlite3
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

import core
from core import _aplicar_entrada_stock, _conectar_db, _iniciar_transaccion_stock, nombre_automatico, now_iso
from core import requiere_rol, usuario_actual

bp = Blueprint("carga_inicial", __name__)

MARCA_CAJA = "CARGA HISTÓRICA DESDE TALCA PHP"
MEDIO_PAGO = "efectivo_cajero"
CONFIRMACION = "CARGAR"
TAMANO_MAXIMO_PLAN = 20 * 1024 * 1024  # el plan real pesa ~2 MB


class CargaInicialError(Exception):
    """Algo impide la carga; el mensaje se muestra tal cual en la pantalla."""


# Lo que cuenta como USO REAL de un producto: si cualquiera de estas consultas
# encuentra algo, el catálogo existente no se puede reemplazar. Un catálogo que
# solo tiene entradas de carga/ajuste (lo que deja "Importar Excel", o crear un
# producto con stock inicial) es de prueba y sí se puede reemplazar.
USOS_DE_PRODUCTOS = [
    ("repuestos cargados a OT", "SELECT COUNT(*) FROM orden_items WHERE producto_id IS NOT NULL"),
    ("repuestos en garantías", "SELECT COUNT(*) FROM garantia_items WHERE producto_id IS NOT NULL"),
    ("repuestos en convertidores", "SELECT COUNT(*) FROM convertidor_items WHERE producto_id IS NOT NULL"),
    ("reservas", "SELECT COUNT(*) FROM reservas"),
    ("líneas de facturas de compra", "SELECT COUNT(*) FROM factura_lineas"),
    ("caja chica con producto", "SELECT COUNT(*) FROM caja_movimientos WHERE producto_id IS NOT NULL"),
    ("boletas de caja chica con producto", "SELECT COUNT(*) FROM caja_gasto_detalles WHERE producto_id IS NOT NULL"),
    ("mermas", "SELECT COUNT(*) FROM merma_proveedores"),
    ("movimientos que no son carga ni ajuste de entrada",
     "SELECT COUNT(*) FROM movimientos WHERE NOT (tipo = 'entrada' AND categoria = 'Ajuste de inventario')"),
]


def _vaciar_catalogo_de_prueba(db):
    """Borra el catálogo existente si nunca se usó (ver USOS_DE_PRODUCTOS). Va
    DENTRO de la transacción de la carga: en un ensayo se deshace con todo lo
    demás. Devuelve cuántos productos había."""
    usos = [(nombre, n) for nombre, sql in USOS_DE_PRODUCTOS for n in [db.execute(sql).fetchone()[0]] if n]
    if usos:
        detalle = "; ".join(f"{nombre}: {n}" for nombre, n in usos)
        raise CargaInicialError(f"El catálogo existente se usó ({detalle}): no se reemplaza. Hay que revisarlo a mano.")
    cantidad = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
    for tabla in ("movimientos", "stock_minimo_calculo", "stock_proveedores", "productos"):
        db.execute(f"DELETE FROM {tabla}")
    return cantidad


def validar_plan(plan):
    if not isinstance(plan, dict):
        raise CargaInicialError("El archivo no es un plan de carga (se esperaba un objeto JSON).")
    for clave in ("proveedores", "productos", "caja_chica", "totales"):
        if clave not in plan:
            raise CargaInicialError(f"Al plan le falta '{clave}': ¿es el archivo plan_carga_*.json?")
    for clave in ("productos", "unidades_inventario", "valor_neto_inventario", "documentos_caja",
                  "monto_caja", "unidades_repuestos_caja"):
        if clave not in plan["totales"]:
            raise CargaInicialError(f"A los totales del plan les falta '{clave}'.")
    for doc in plan["caja_chica"]:
        for ln in doc.get("lineas", []):
            if ln.get("clase") not in ("REPUESTO", "GASTO"):
                raise CargaInicialError(f"Línea de caja chica sin clasificar: {ln.get('descripcion')!r}.")


def ejecutar_carga(plan, usuario, aplicar=False, db_path=None, reemplazar=False):
    """Aplica el plan. Devuelve un dict con los chequeos y, si aplicar, la ruta
    del respaldo previo. Lanza CargaInicialError (con la base intacta) si algo
    impide la carga o si algún total no cuadra.

    reemplazar=True permite partir de un catálogo con productos de PRUEBA (por
    ejemplo, un Excel subido con "Importar Excel" para probar): se borran dentro
    de la misma transacción, solo si nunca se usaron."""
    validar_plan(plan)
    db_path = db_path or core.DB_PATH
    db = _conectar_db(db_path)
    try:
        productos_previos = db.execute("SELECT COUNT(*) FROM productos").fetchone()[0]
        if productos_previos and not reemplazar:
            raise CargaInicialError(
                f"La base ya tiene {productos_previos} productos: la carga inicial es sobre un catálogo vacío. "
                "Si son de prueba y nunca se usaron, marca \"Reemplazar el catálogo existente\".")
        if db.execute("SELECT COUNT(*) FROM caja_movimientos WHERE descripcion LIKE ?", (MARCA_CAJA + "%",)).fetchone()[0]:
            raise CargaInicialError("La carga histórica de caja chica ya existe en esta base.")

        def saldo_caja():
            apo = db.execute("SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE tipo='aporte' AND medio_pago=?",
                             (MEDIO_PAGO,)).fetchone()[0]
            gas = db.execute("SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE tipo='gasto' AND medio_pago=?",
                             (MEDIO_PAGO,)).fetchone()[0]
            return round(apo - gas, 2)

        saldo_antes = saldo_caja()
        ts = now_iso()
        _iniciar_transaccion_stock(db)
        try:
            reemplazados = _vaciar_catalogo_de_prueba(db) if productos_previos else 0

            # 1. proveedores
            prov_id = {}
            creados_prov = 0
            for p in plan["proveedores"]:
                fila = db.execute("SELECT id FROM proveedores WHERE nombre = ?", (p["nombre"],)).fetchone()
                if fila is None:
                    cur = db.execute(
                        "INSERT INTO proveedores (nombre, rut, telefono, email, direccion, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
                        (p["nombre"], p.get("rut", ""), p.get("telefono", ""), p.get("email", ""), p.get("direccion", ""), ts))
                    prov_id[p["nombre"]] = cur.lastrowid
                    creados_prov += 1
                else:
                    prov_id[p["nombre"]] = fila[0]

            # 2. productos
            producto_id = {}

            def crear_producto(modelo, descripcion, ubicacion):
                # Mismo mapeo que "Importar Excel": MODELO externo -> categoria, DESCRIPCION -> modelo.
                cur = db.execute(
                    """INSERT INTO productos (codigo_interno, nombre, descripcion, categoria, marca, modelo, ubicacion,
                           ubicacion_id, stock_disponible, stock_reservado, stock_merma, stock_minimo, costo_compra,
                           creado_en, actualizado_en)
                       VALUES ('', '', '', ?, '', ?, ?, NULL, 0, 0, 0, 0, 0, ?, ?)""",
                    (modelo, descripcion, ubicacion, ts, ts))
                pid = cur.lastrowid
                db.execute("UPDATE productos SET nombre = ? WHERE id = ?",
                           (nombre_automatico(modelo, descripcion, "", pid), pid))
                for tabla, valor in (("categorias", modelo), ("modelos", descripcion)):
                    if valor:
                        db.execute(f"INSERT OR IGNORE INTO {tabla} (nombre) VALUES (?)", (valor,))
                producto_id[(modelo, descripcion)] = pid
                return pid

            def entrada(pid, cantidad, costo, motivo, categoria, proveedor):
                fila = db.execute("SELECT * FROM productos WHERE id = ?", (pid,)).fetchone()
                _aplicar_entrada_stock(db, fila, cantidad, costo, motivo=motivo, usuario=usuario, factura=None,
                                       categoria=categoria, proveedor_id=prov_id.get(proveedor) if proveedor else None)
                return db.execute("SELECT id FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT 1",
                                  (pid,)).fetchone()[0]

            for p in plan["productos"]:
                pid = crear_producto(p["modelo"], p["descripcion"], p["ubicacion"])
                for b in p["baldes"]:
                    entrada(pid, b["cantidad"], b["costo_neto"], "CARGA INICIAL DESDE TALCA PHP",
                            "Ajuste de inventario", b["proveedor"])

            # 3. caja chica
            detalles = []
            nuevos_por_caja = 0
            for doc in plan["caja_chica"]:
                total = sum(ln["monto"] for ln in doc["lineas"])
                cur = db.execute(
                    """INSERT INTO caja_movimientos (tipo, medio_pago, fecha, monto, descripcion, usuario, creado_en)
                       VALUES ('gasto', ?, ?, ?, ?, ?, ?)""",
                    (MEDIO_PAGO, f"{doc['fecha']}T12:00:00", total, f"{MARCA_CAJA} ({doc['numero_documento']})", usuario, ts))
                gasto_id = cur.lastrowid
                for ln in doc["lineas"]:
                    pid = mov = None
                    if ln["clase"] == "REPUESTO":
                        pid = producto_id.get((ln["modelo"], ln["descripcion"]))
                        if pid is None:
                            pid = crear_producto(ln["modelo"], ln["descripcion"], "CAJA CHICA")
                            nuevos_por_caja += 1
                        mov = entrada(pid, ln["cantidad"], ln["costo_neto"],
                                      "COMPRA DE EMERGENCIA (CAJA CHICA) - HISTÓRICO TALCA PHP", "Compra", ln["proveedor_stock"])
                    cur = db.execute(
                        """INSERT INTO caja_gasto_detalles (gasto_id, numero_documento, monto, rubro_gav, categoria, proveedor,
                               descripcion, orden_id, comprobante_archivo, producto_id, movimiento_inventario_id, usuario, creado_en)
                           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?, ?, ?)""",
                        (gasto_id, doc["numero_documento"].upper(), ln["monto"], ln.get("rubro_gav", ""), MARCA_CAJA,
                         doc["proveedor"], ln["descripcion"], pid, mov, usuario, ts))
                    detalles.append((cur.lastrowid, ln["monto"]))

            total_caja = sum(m for _, m in detalles)
            rendicion_id = None
            if detalles:
                cur = db.execute(
                    "INSERT INTO caja_rendiciones (fecha, usuario, total, nota, medio_pago, creado_en) VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, usuario, total_caja, MARCA_CAJA, MEDIO_PAGO, ts))
                rendicion_id = cur.lastrowid
                db.executemany("UPDATE caja_gasto_detalles SET rendicion_id = ? WHERE id = ?",
                               [(rendicion_id, i) for i, _ in detalles])
                db.execute(
                    """INSERT INTO caja_movimientos (tipo, medio_pago, fecha, monto, descripcion, usuario, rendicion_origen_id, creado_en)
                       VALUES ('aporte', ?, ?, ?, ?, ?, ?, ?)""",
                    (MEDIO_PAGO, ts, total_caja,
                     f"REPOSICIÓN AUTOMÁTICA - RENDICIÓN #{rendicion_id} ({MARCA_CAJA}, {len(plan['caja_chica'])} DOCUMENTOS)",
                     usuario, rendicion_id, ts))

            # Verificación contra el plan
            t = plan["totales"]
            unidades_esperadas = t["unidades_inventario"] + t["unidades_repuestos_caja"]
            valor = db.execute("SELECT COALESCE(SUM(stock_disponible * costo_compra),0) FROM productos").fetchone()[0]
            valor_esperado = t["valor_neto_inventario"] + sum(
                ln["costo_neto"] * ln["cantidad"] for doc in plan["caja_chica"] for ln in doc["lineas"] if ln["clase"] == "REPUESTO")
            chequeos = []

            def chequeo(nombre, real, esperado, tolerancia=0):
                chequeos.append({"nombre": nombre, "real": real, "esperado": esperado,
                                 "ok": abs(real - esperado) <= tolerancia})

            chequeo("productos", db.execute("SELECT COUNT(*) FROM productos").fetchone()[0], t["productos"] + nuevos_por_caja)
            chequeo("unidades en stock", db.execute("SELECT COALESCE(SUM(stock_disponible),0) FROM productos").fetchone()[0],
                    unidades_esperadas)
            chequeo("unidades en baldes por proveedor",
                    db.execute("SELECT COALESCE(SUM(cantidad),0) FROM stock_proveedores").fetchone()[0], unidades_esperadas)
            # Solo el valor en pesos admite $1 de redondeo; lo demás tiene que calzar exacto.
            chequeo("valor neto del stock", round(valor), round(valor_esperado), tolerancia=1)
            chequeo("documentos de caja",
                    db.execute("SELECT COUNT(*) FROM caja_movimientos WHERE tipo='gasto' AND descripcion LIKE ?",
                               (MARCA_CAJA + "%",)).fetchone()[0], t["documentos_caja"])
            chequeo("monto de caja", round(total_caja), round(t["monto_caja"]))
            chequeo("boletas históricas pendientes de rendir",
                    db.execute("SELECT COUNT(*) FROM caja_gasto_detalles WHERE categoria = ? AND rendicion_id IS NULL",
                               (MARCA_CAJA,)).fetchone()[0], 0)
            chequeo("saldo de caja efectivo del cajero", saldo_caja(), saldo_antes)

            resultado = {
                "chequeos": chequeos,
                "proveedores_creados": creados_prov,
                "proveedores_reusados": len(prov_id) - creados_prov,
                "productos_nuevos_por_caja": nuevos_por_caja,
                "productos_de_prueba_reemplazados": reemplazados,
                "aplicado": False,
                "respaldo": None,
            }
            fallas = [c["nombre"] for c in chequeos if not c["ok"]]
            if fallas:
                raise CargaInicialError(f"No cuadran {len(fallas)} chequeo(s): {', '.join(fallas)}. No se escribió nada.")
        except Exception:
            db.rollback()
            raise

        if not aplicar:
            db.rollback()
            return resultado

        # Respaldo con la API de SQLite (consistente aunque haya WAL) antes de confirmar:
        # otra conexión ve el estado confirmado, o sea la base de antes de la carga.
        respaldo = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                                f"antes-carga-inicial-{datetime.now():%Y%m%d-%H%M%S}.db")
        origen = sqlite3.connect(db_path)
        destino = sqlite3.connect(respaldo)
        try:
            origen.backup(destino)
        finally:
            destino.close()
            origen.close()
        db.commit()
        resultado.update(aplicado=True, respaldo=respaldo)
        return resultado
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pantalla y endpoint
# ---------------------------------------------------------------------------

@bp.route("/admin/carga-inicial", methods=["GET"])
@requiere_rol()
def vista_carga_inicial():
    return render_template("carga_inicial.html")


@bp.route("/api/admin/carga-inicial", methods=["POST"])
@requiere_rol()
def api_carga_inicial():
    accion = (request.form.get("accion") or "ensayar").strip().lower()
    if accion not in ("ensayar", "aplicar"):
        return jsonify({"error": "Acción inválida: usa 'ensayar' o 'aplicar'."}), 400
    if accion == "aplicar" and (request.form.get("confirmar_texto") or "").strip().upper() != CONFIRMACION:
        return jsonify({"error": f"Para aplicar, escribe exactamente {CONFIRMACION}."}), 400
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Adjunta el archivo plan_carga_*.json."}), 400
    contenido = archivo.read(TAMANO_MAXIMO_PLAN + 1)
    if len(contenido) > TAMANO_MAXIMO_PLAN:
        return jsonify({"error": "El archivo es demasiado grande para ser un plan de carga."}), 400
    try:
        plan = json.loads(contenido.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return jsonify({"error": "El archivo no es un JSON válido."}), 400
    try:
        resultado = ejecutar_carga(plan, usuario_actual() or "CARGA TALCA PHP", aplicar=(accion == "aplicar"),
                                   reemplazar=(request.form.get("reemplazar") == "1"))
    except CargaInicialError as exc:
        return jsonify({"error": str(exc)}), 409
    resultado["origen_plan"] = plan.get("origen", {})
    resultado["accion"] = accion
    if resultado.get("respaldo"):
        resultado["respaldo"] = os.path.basename(resultado["respaldo"])
    return jsonify(resultado)
