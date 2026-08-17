const state = {
  sesion: { username: null, nombre: null },
  cajaPendientesSeleccionados: new Set(),
  cajaBoletasConocidas: new Set(),
  cajaPendientesLista: [],
  crGastoAbierto: null,
  cajaMovimientos: [],
  cajaMovimientoActivoId: null,
  cxcCuentas: [],
  cxcAbonosCuentas: [],
  cxcOtActiva: null, // { ordenId, contexto: "cerradas" | "abonos" }
  cmfToken: null,
  cmfFacturas: [],
  cmfProductos: [],
  cmfProveedores: [],
  facturasPendientes: [],
  dteConfig: null,
  dteItems: [],
  dteDocumentos: [],
  dteDocumentoActivoId: null,
};

const ESTADO_CXX_LABEL = { pendiente: "Pendiente", parcial: "Parcial", pagada: "Pagada", anulada: "Anulada" };

// 3 medios de pago de Caja Chica (ver MEDIOS_PAGO_CAJA en modulos/finanzas.py).
const MEDIO_PAGO_LABEL = {
  efectivo_cajero: "Efectivo (cajero)",
  tarjeta_empresa: "Tarjeta empresa",
  cristian: "Vía Cristian",
};

// ---------------------------------------------------------------------------
// Utilidades compartidas (duplicadas de app.js/operativo.js: cada módulo
// carga su propio JS — ver comentario equivalente en operativo.js).
// ---------------------------------------------------------------------------

function fmtMoney(n) {
  return "$" + Math.round(n).toLocaleString("es-CL");
}

function fmtFecha(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("es-CL", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function diasDesde(fechaIso) {
  if (!fechaIso) return "—";
  const d = new Date(fechaIso);
  if (isNaN(d)) return "—";
  const dias = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (dias < 0) return "0";
  return String(dias);
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.add("visible");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("visible"), 3200);
}

function abrirModal(id) {
  const el = document.getElementById(id);
  const abiertos = [...document.querySelectorAll(".modal-overlay.abierto")]
    .map(o => parseInt(getComputedStyle(o).zIndex, 10) || 50);
  el.style.zIndex = abiertos.length ? Math.max(...abiertos) + 1 : "";
  el.classList.add("abierto");
}
function cerrarModal(id) {
  const el = document.getElementById(id);
  el.classList.remove("abierto");
  el.style.zIndex = "";
}

document.querySelectorAll("[data-close]").forEach(btn => {
  btn.addEventListener("click", () => cerrarModal(btn.dataset.close));
});

// Los modales ya NO se cierran al hacer click afuera NI con ESC (para no
// perder datos escritos por accidente -- ej. una factura larga a medio
// llenar). Solo se cierran con la X o "Cancelar", que son clics explícitos.

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function normalizarBusqueda(texto) {
  if (texto === null || texto === undefined) return "";
  return String(texto).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function coincideBusqueda(consulta, ...campos) {
  const terminos = normalizarBusqueda(consulta).split(/\s+/).filter(Boolean);
  if (!terminos.length) return true;
  const texto = normalizarBusqueda(campos.filter(c => c !== null && c !== undefined && c !== "").join(" "));
  return terminos.every(t => texto.includes(t));
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || "Error inesperado");
    err.requiereForzar = !!data.requiere_forzar;
    err.data = data;
    throw err;
  }
  return data;
}

async function cargarSesion() {
  try {
    const s = await api("/api/sesion");
    state.sesion = s;
    document.getElementById("sesion-usuario").textContent = s.nombre ? `👤 ${s.nombre}` : "";
    // El backend siempre rechaza esto si no se tiene acceso al módulo
    // finanzas (ver @requiere_rol en app.py) — acá solo se evita mostrar
    // un botón que de todas formas iba a fallar. Se mira 'modulos' (no
    // 'rol') porque desde la migración 17 alguien puede tener finanzas
    // como módulo SECUNDARIO (ej. rol="bodega" con modulos=["bodega",
    // "finanzas"]) y aun así tener que ver este botón.
    const puedeFinanzas = s.rol === "admin" || (s.modulos || []).includes("finanzas");
    // "Registrar factura de compra" (ruta directa de ingreso, ver
    // crear_factura_pendiente_recepcion en app.py) también está abierta a
    // Bodega desde la nueva ruta de ingreso directo -- ella es quien
    // ahora ingresa las facturas al recibir la mercadería.
    const puedeFacturas = puedeFinanzas || (s.modulos || []).includes("bodega");
    const btnNuevaFacturaPendiente = document.getElementById("btn-nueva-factura-pendiente");
    if (btnNuevaFacturaPendiente) btnNuevaFacturaPendiente.style.display = puedeFacturas ? "" : "none";
    const btnNuevaCxpManual = document.getElementById("btn-nueva-cxp-manual");
    if (btnNuevaCxpManual) btnNuevaCxpManual.style.display = puedeFinanzas ? "" : "none";
    // Solo Finanzas/admin puede corregir la condición de pago de una
    // factura ya aplicada (ver PUT /api/facturas/<id>/condicion-pago) --
    // se guarda en el estado global para que abrirDetalleFacturaPendiente
    // decida si muestra ese botón sin tener que repetir este cálculo.
    state.puedeEditarCondicionPago = puedeFinanzas;
  } catch (e) {
    console.error(e);
  }
}

function rangoFechasParams(idDesde, idHasta) {
  const desde = document.getElementById(idDesde).value;
  const hasta = document.getElementById(idHasta).value;
  const params = new URLSearchParams();
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);
  return params.toString();
}

// ---------------------------------------------------------------------------
// Pestañas: Resumen / Caja Chica / Reportes
// ---------------------------------------------------------------------------

// OJO: excluye los botones de #cxc-subtabs (también tienen clase "tab") —
// si no, un click en "Por abonos"/"OTs cerradas" también dispara este
// listener (dataset.vista queda undefined, así que caía al "else" de cada
// línea y ocultaba TODAS las vistas, dejando la pantalla en blanco).
document.querySelectorAll(".tabs:not(.sub-tabs) > .tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tabs:not(.sub-tabs) > .tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-resumen").style.display = vista === "resumen" ? "block" : "none";
    document.getElementById("vista-caja").style.display = vista === "caja" ? "block" : "none";
    document.getElementById("vista-cxc").style.display = vista === "cxc" ? "block" : "none";
    document.getElementById("vista-cxp").style.display = vista === "cxp" ? "block" : "none";
    document.getElementById("vista-facturas").style.display = vista === "facturas" ? "block" : "none";
    document.getElementById("vista-gastos-generales").style.display = vista === "gastos-generales" ? "block" : "none";
    document.getElementById("vista-dte").style.display = vista === "dte" ? "block" : "none";
    document.getElementById("vista-estado-resultados").style.display = vista === "estado-resultados" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      resumen: "Finanzas — Resumen",
      caja: "Finanzas — Caja Chica",
      cxc: "Finanzas — Cuentas por Cobrar",
      cxp: "Finanzas — Cuentas por Pagar",
      facturas: "Finanzas — Facturas",
      "gastos-generales": "Finanzas — Gastos Generales",
      dte: "Finanzas — Facturación electrónica",
      "estado-resultados": "Finanzas — Estado de Resultados",
      reportes: "Finanzas — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "resumen") cargarResumenFinanzas();
    if (vista === "caja") cargarCaja();
    if (vista === "cxc") { cargarCxc(); cargarCxcAbonos(); }
    if (vista === "cxp") cargarCxp();
    if (vista === "facturas") {
      // Llegada desde el aviso rojo de facturas rechazadas (ver afrAbrirLista
      // en alerta_facturas_pendientes.js): /finanzas?ver_pendiente=123#facturas
      // -- abre directo el detalle de esa factura (con su nota de rechazo y
      // el botón "Corregir y reingresar" ya visible) apenas termina de cargar
      // la lista completa, de la que depende abrirDetalleFacturaPendiente.
      cargarFacturasPendientesRecepcion().then(() => {
        const verPendientePrefill = new URLSearchParams(location.search).get("ver_pendiente");
        if (verPendientePrefill) abrirDetalleFacturaPendiente(parseInt(verPendientePrefill, 10));
      });
    }
    if (vista === "gastos-generales") cargarGastosGenerales();
    if (vista === "dte") {
      cargarDte();
      // Llegada desde el botón "Ir a Facturación electrónica" de la
      // pestaña "Factura" de una OT (ver operativo.js): /finanzas?orden=123#dte
      // OJO: esto corre en la carga inicial de la página (activarPestanaDesdeHash()
      // se llama apenas se define, más arriba en este mismo archivo), pero el
      // listener "input" de #dtef-orden-id que hace el lookup recién se
      // registra más abajo en este archivo -- si se dispara el evento acá
      // mismo, ese listener todavía no existe y el prellenado se pierde
      // silenciosamente. Por eso se difiere con setTimeout(0): así corre
      // recién cuando el script completo (con todos sus listeners) ya
      // terminó de ejecutarse.
      const ordenPrefill = new URLSearchParams(location.search).get("orden");
      if (ordenPrefill) setTimeout(() => abrirModalNuevaFactura(ordenPrefill), 0);
    }
    if (vista === "estado-resultados") cargarEstadoResultados();
  });
});

// El sidebar enlaza a /finanzas#vista (ver _sidebar.html). Como los tabs
// se manejan 100% en JS, hay que leer el hash al cargar la página y también
// reaccionar si cambia estando ya en esta vista.
function activarPestanaDesdeHash() {
  const vista = location.hash.replace("#", "");
  if (!vista) return;
  const tabBtn = document.querySelector(`.tabs:not(.sub-tabs) > .tab[data-vista="${vista}"]`);
  if (tabBtn) tabBtn.click();
}
window.addEventListener("hashchange", activarPestanaDesdeHash);
activarPestanaDesdeHash();

// ---------------------------------------------------------------------------
// Sub-pestañas de Cuentas por Cobrar: "OTs cerradas" (tabla de siempre,
// cuentas_por_cobrar) y "Por abonos" (OTs en reparación que todavía no
// alcanzan el anticipo pactado — ver /api/finanzas/cxc/abonos).
// ---------------------------------------------------------------------------

document.querySelectorAll("#cxc-subtabs .tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll("#cxc-subtabs .tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const sub = tabBtn.dataset.subvista;
    document.getElementById("cxc-vista-cerradas").style.display = sub === "cerradas" ? "block" : "none";
    document.getElementById("cxc-vista-abonos").style.display = sub === "abonos" ? "block" : "none";
  });
});

// ---------------------------------------------------------------------------
// Resumen financiero general — cruza lo cobrado a clientes (Operativo) con
// los gastos de Caja Chica. Ver comentario en modulos/finanzas.py
// (finanzas_resumen) para el criterio exacto de qué cuenta como ingreso o
// gasto.
// ---------------------------------------------------------------------------

async function cargarResumenFinanzas() {
  try {
    const params = rangoFechasParams("fin-resumen-desde", "fin-resumen-hasta");
    const d = await api("/api/finanzas/resumen" + (params ? "?" + params : ""));
    // Si no se habían elegido fechas todavía, reflejamos el rango por
    // defecto (mes actual) que aplicó el backend, para que quede claro qué
    // período se está mostrando.
    if (!document.getElementById("fin-resumen-desde").value && !document.getElementById("fin-resumen-hasta").value && d.desde) {
      document.getElementById("fin-resumen-desde").value = d.desde;
    }
    document.getElementById("fin-s-ingresos").textContent = fmtMoney(d.ingresos);
    document.getElementById("fin-s-gastos").textContent = fmtMoney(d.gastos);
    document.getElementById("fin-s-resultado").textContent = fmtMoney(d.resultado);
    document.getElementById("fin-s-saldo-caja").textContent = fmtMoney(d.saldo_caja_chica);

    document.getElementById("fin-s-cxc-pendiente").textContent = fmtMoney(d.cxc_pendiente);
    const elCxcVencido = document.getElementById("fin-s-cxc-vencido");
    elCxcVencido.textContent = d.cxc_vencido > 0 ? `${fmtMoney(d.cxc_vencido)} vencida` : "sin vencidas";
    elCxcVencido.classList.toggle("summary-sub--danger", d.cxc_vencido > 0);

    document.getElementById("fin-s-cxp-pendiente").textContent = fmtMoney(d.cxp_pendiente);
    const elCxpVencido = document.getElementById("fin-s-cxp-vencido");
    elCxpVencido.textContent = d.cxp_vencido > 0 ? `${fmtMoney(d.cxp_vencido)} vencida` : "sin vencidas";
    elCxpVencido.classList.toggle("summary-sub--danger", d.cxp_vencido > 0);
  } catch (e) {
    toast(e.message, true);
  }
  cargarGraficosResumenFinanzas();
}

async function cargarGraficosResumenFinanzas() {
  try {
    const data = await api("/api/finanzas/resumen/graficos");

    document.getElementById("fin-chart-tendencia").innerHTML = buildLineChartDoble(
      data.tendencia_mensual.map(d => ({ mes: d.mes, a: d.ingresos, b: d.gastos })),
      "Ingresos", "Gastos",
      { colorA: "var(--success)", colorB: "var(--danger)" }
    );

    document.getElementById("fin-chart-aging").innerHTML = buildBarChartH(
      data.cxc_aging.map(d => ({ label: d.label, valor: d.monto, extra: fmtMoney(d.monto) })),
      { padL: 90, color: "var(--accent-2)" }
    );

    document.getElementById("fin-chart-aging-cxp").innerHTML = buildBarChartH(
      data.cxp_aging.map(d => ({ label: d.label, valor: d.monto, extra: fmtMoney(d.monto) })),
      { padL: 90, color: "var(--danger)" }
    );

    document.getElementById("fin-chart-proveedores").innerHTML = buildBarChartH(
      data.gasto_por_proveedor.map(d => ({ label: d.proveedor, valor: d.monto, extra: fmtMoney(d.monto) })),
      { padL: 150 }
    );
  } catch (e) {
    console.error("Error cargando gráficos de Finanzas:", e);
  }
}

document.getElementById("fin-resumen-desde").addEventListener("change", cargarResumenFinanzas);
document.getElementById("fin-resumen-hasta").addEventListener("change", cargarResumenFinanzas);
document.getElementById("fin-resumen-limpiar").addEventListener("click", () => {
  document.getElementById("fin-resumen-desde").value = "";
  document.getElementById("fin-resumen-hasta").value = "";
  cargarResumenFinanzas();
});

// ---------------------------------------------------------------------------
// Caja Chica — movido desde Logística (antes vivía en index.html / app.js).
// Misma lógica y mismos endpoints /api/caja/..., solo que ahora corre acá.
// ---------------------------------------------------------------------------

async function cargarCaja() {
  await Promise.all([cargarCajaResumen(), cargarCajaMovimientos()]);
}

async function cargarCajaResumen() {
  try {
    const d = await api("/api/caja/resumen");

    const ef = d.efectivo_cajero;
    document.getElementById("caja-ef-saldo").textContent = fmtMoney(ef.saldo_disponible);
    document.getElementById("caja-ef-pendiente").textContent = fmtMoney(ef.pendiente_rendir);
    document.getElementById("caja-ef-gastado-mes").textContent = fmtMoney(ef.gastado_mes);
    document.getElementById("caja-ef-total-rendido").textContent = fmtMoney(ef.total_rendido);

    const tj = d.tarjeta_empresa;
    document.getElementById("caja-tj-pendiente").textContent = fmtMoney(tj.pendiente_rendir);
    document.getElementById("caja-tj-gastado-mes").textContent = fmtMoney(tj.gastado_mes);
    document.getElementById("caja-tj-total-gastado").textContent = fmtMoney(tj.total_gastado);

    // El saldo de Cristian es una cuenta corriente, no una caja física: si
    // queda negativo significa que el taller le debe esa plata (anticipó
    // más de lo que se le ha transferido). Se lo dejamos explícito en vez
    // de mostrar un número negativo pelado.
    const cr = d.cristian;
    const saldoCr = document.getElementById("caja-cr-saldo");
    const labelCr = document.getElementById("caja-cr-saldo-label");
    const cardCr = document.getElementById("caja-cr-saldo-card");
    if (cr.saldo_disponible < -0.01) {
      labelCr.textContent = "El taller le debe a Cristian";
      saldoCr.textContent = fmtMoney(Math.abs(cr.saldo_disponible));
      cardCr.classList.add("summary-card--warn");
    } else if (cr.saldo_disponible > 0.01) {
      labelCr.textContent = "Cristian tiene esto sin rendir";
      saldoCr.textContent = fmtMoney(cr.saldo_disponible);
      cardCr.classList.remove("summary-card--warn");
    } else {
      labelCr.textContent = "Cuenta al día";
      saldoCr.textContent = fmtMoney(0);
      cardCr.classList.remove("summary-card--warn");
    }
    document.getElementById("caja-cr-pendiente").textContent = fmtMoney(cr.pendiente_rendir);
    document.getElementById("caja-cr-gastado-mes").textContent = fmtMoney(cr.gastado_mes);
    document.getElementById("caja-cr-total-aportado").textContent = fmtMoney(cr.total_aportado);
  } catch (e) {
    toast(e.message, true);
  }
}

async function cargarCajaMovimientos() {
  const tbody = document.getElementById("tbody-caja");
  const params = new URLSearchParams();
  const q = document.getElementById("caja-buscar").value.trim();
  const tipo = document.getElementById("caja-filtro-tipo").value;
  const medio = document.getElementById("caja-filtro-medio").value;
  const estado = document.getElementById("caja-filtro-estado").value;
  const desde = document.getElementById("caja-filtro-desde").value;
  const hasta = document.getElementById("caja-filtro-hasta").value;
  if (q) params.set("q", q);
  if (tipo) params.set("tipo", tipo);
  if (medio) params.set("medio_pago", medio);
  if (estado) params.set("estado", estado);
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);

  try {
    const data = await api("/api/caja/movimientos?" + params.toString());

    state.cajaMovimientos = data.movimientos;

    if (data.movimientos.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No se encontraron movimientos con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.movimientos.map(m => {
      const esGasto = m.tipo === "gasto";
      // Desde la Migración 25 un gasto (retiro) no tiene categoría/proveedor
      // propios -- esos datos viven en sus boletas, ver "Generar rendición".
      const catDesc = esGasto
        ? (m.boletas && m.boletas.length ? `${m.boletas.length} boleta(s)` : "(sin boletas)")
        : (m.descripcion || "—");
      const comprobante = m.tiene_comprobante
        ? `<a href="/api/caja/comprobante/${encodeURIComponent(m.comprobante_archivo)}" target="_blank" rel="noopener" data-sin-navegar="1">Ver</a>`
        : "—";
      const estadoTxt = esGasto ? (m.rendido ? `<span class="mov-tipo mov-tipo--entrada">rendido</span>` : `<span class="mov-tipo mov-tipo--perdida">pendiente</span>`) : "—";
      return `
      <tr data-id="${m.id}" data-tipo="${m.tipo}" class="fila-clicable">
        <td class="mono">${fmtFecha(m.fecha)}</td>
        <td><span class="mov-tipo mov-tipo--${esGasto ? "salida" : "entrada"}">${esGasto ? "gasto" : "aporte"}</span></td>
        <td>${MEDIO_PAGO_LABEL[m.medio_pago] || m.medio_pago}</td>
        <td>${escapeHtml(catDesc)}</td>
        <td class="num mono">${esGasto ? "-" : "+"}${fmtMoney(m.monto)}</td>
        <td>${comprobante}</td>
        <td>${estadoTxt}</td>
        <td>${escapeHtml(m.usuario) || "—"}</td>
        <td></td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("[data-sin-navegar]")) return; // no interceptar el link "Ver comprobante"
        const id = parseInt(tr.dataset.id, 10);
        if (tr.dataset.tipo === "gasto") abrirEdicionCajaGasto(id);
        else abrirEdicionCajaAporte(id);
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar Caja Chica: ${escapeHtml(e.message)}</td></tr>`;
  }
}

let cajaBuscarTimeout;
document.getElementById("caja-buscar").addEventListener("input", () => {
  clearTimeout(cajaBuscarTimeout);
  cajaBuscarTimeout = setTimeout(cargarCajaMovimientos, 250);
});
document.getElementById("caja-filtro-tipo").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-medio").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-estado").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-desde").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-hasta").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-limpiar-filtros").addEventListener("click", () => {
  document.getElementById("caja-buscar").value = "";
  document.getElementById("caja-filtro-tipo").value = "";
  document.getElementById("caja-filtro-medio").value = "";
  document.getElementById("caja-filtro-estado").value = "";
  document.getElementById("caja-filtro-desde").value = "";
  document.getElementById("caja-filtro-hasta").value = "";
  cargarCajaMovimientos();
});

// ---- Nuevo gasto ----
// Migración 25: acá solo se registra el RETIRO (monto, medio de pago,
// fecha) -- quien lo carga viendo el banco no tiene más información que
// esa. El N° de boleta/factura, el rubro, el detalle, si es una compra de
// emergencia (Migración 27) y la OT relacionada se cargan después, boleta
// por boleta, desde "Generar rendición" (ver más abajo,
// cargarPendientesRendicion / agregarBoletaAGasto).

document.getElementById("btn-nuevo-gasto").addEventListener("click", async () => {
  document.getElementById("form-caja-gasto").reset();
  document.getElementById("cg-id").value = "";
  document.getElementById("modal-caja-gasto-titulo").textContent = "Nuevo gasto";
  document.getElementById("cg-bloqueado-aviso").style.display = "none";
  document.getElementById("cg-acciones-editar").style.display = "none";
  habilitarFormularioCaja("cg", true);
  abrirModal("modal-caja-gasto");
});

function habilitarFormularioCaja(prefijo, habilitado) {
  document.querySelectorAll(`#form-caja-${prefijo === "cg" ? "gasto" : "aporte"} input, #form-caja-${prefijo === "cg" ? "gasto" : "aporte"} select`)
    .forEach(el => { if (el.id !== `${prefijo}-id`) el.disabled = !habilitado; });
  document.getElementById(`${prefijo}-btn-guardar`).style.display = habilitado ? "" : "none";
}

function abrirEdicionCajaGasto(id) {
  const m = (state.cajaMovimientos || []).find(x => x.id === id);
  if (!m) return;
  document.getElementById("form-caja-gasto").reset();
  document.getElementById("cg-id").value = m.id;
  document.getElementById("cg-monto").value = m.monto;
  document.getElementById("cg-fecha").value = (m.fecha || "").slice(0, 10);
  document.getElementById("cg-medio-pago").value = m.medio_pago || "efectivo_cajero";
  document.getElementById("modal-caja-gasto-titulo").textContent = "Editar gasto";

  const bloqueo = m.bloqueado_motivo;
  const aviso = document.getElementById("cg-bloqueado-aviso");
  if (bloqueo) {
    aviso.textContent = bloqueo;
    aviso.className = "ayuda-inline ayuda-inline--bloqueado";
    aviso.style.display = "block";
  } else {
    aviso.style.display = "none";
  }
  habilitarFormularioCaja("cg", !bloqueo);
  document.getElementById("cg-acciones-editar").style.display = bloqueo ? "none" : "flex";
  state.cajaMovimientoActivoId = m.id;
  abrirModal("modal-caja-gasto");
}

document.getElementById("btn-eliminar-caja-gasto").addEventListener("click", async () => {
  const id = state.cajaMovimientoActivoId;
  if (!id) return;
  if (!confirm("¿Eliminar este gasto? No se puede deshacer.")) return;
  try {
    await api(`/api/caja/movimiento/${id}`, { method: "DELETE" });
    toast("Gasto eliminado");
    cerrarModal("modal-caja-gasto");
    await cargarCaja();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("form-caja-gasto").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("cg-id").value;

  const formData = new FormData();
  formData.append("monto", document.getElementById("cg-monto").value);
  formData.append("fecha", document.getElementById("cg-fecha").value);
  formData.append("medio_pago", document.getElementById("cg-medio-pago").value);

  try {
    const url = id ? `/api/caja/movimiento/${id}` : "/api/caja/gasto";
    const res = await fetch(url, { method: id ? "PUT" : "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Error al guardar el gasto");
    toast(id ? "Gasto actualizado" : "Gasto registrado");
    cerrarModal("modal-caja-gasto");
    await cargarCaja();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---- Aporte ----
// Se sacó el botón "+ Aporte" (a pedido del taller): un aporte manual
// suelto ya no se puede crear desde acá -- los aportes ahora solo se
// generan solos al "Reponer el saldo" en una rendición (ver
// btn-confirmar-rendicion más abajo), para no tener dos caminos de
// entrada distintos para la misma plata. Editar/eliminar un aporte ya
// existente (clicando su fila en la tabla de movimientos) sigue
// funcionando igual que siempre.

function abrirEdicionCajaAporte(id) {
  const m = (state.cajaMovimientos || []).find(x => x.id === id);
  if (!m) return;
  document.getElementById("form-caja-aporte").reset();
  document.getElementById("ca-id").value = m.id;
  document.getElementById("ca-monto").value = m.monto;
  document.getElementById("ca-fecha").value = (m.fecha || "").slice(0, 10);
  document.getElementById("ca-medio-pago").value = m.medio_pago || "efectivo_cajero";
  document.getElementById("ca-descripcion").value = m.descripcion || "";
  document.getElementById("ca-comprobante-actual").style.display = m.tiene_comprobante ? "block" : "none";
  document.getElementById("ca-comprobante-actual").innerHTML = m.tiene_comprobante
    ? `Ya tiene un comprobante cargado. <a href="/api/caja/comprobante/${encodeURIComponent(m.comprobante_archivo)}" target="_blank" rel="noopener">Verlo</a> — sube uno nuevo para reemplazarlo.`
    : "";
  document.getElementById("modal-caja-aporte-titulo").textContent = "Editar aporte";

  const bloqueo = m.bloqueado_motivo;
  const aviso = document.getElementById("ca-bloqueado-aviso");
  if (bloqueo) {
    aviso.textContent = bloqueo;
    aviso.className = "ayuda-inline ayuda-inline--bloqueado";
    aviso.style.display = "block";
  } else {
    aviso.style.display = "none";
  }
  habilitarFormularioCaja("ca", !bloqueo);
  document.getElementById("ca-acciones-editar").style.display = bloqueo ? "none" : "flex";
  state.cajaMovimientoActivoId = m.id;
  abrirModal("modal-caja-aporte");
}

document.getElementById("btn-eliminar-caja-aporte").addEventListener("click", async () => {
  const id = state.cajaMovimientoActivoId;
  if (!id) return;
  if (!confirm("¿Eliminar este aporte? No se puede deshacer.")) return;
  try {
    await api(`/api/caja/movimiento/${id}`, { method: "DELETE" });
    toast("Aporte eliminado");
    cerrarModal("modal-caja-aporte");
    await cargarCaja();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("form-caja-aporte").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("ca-id").value;
  const formData = new FormData();
  formData.append("monto", document.getElementById("ca-monto").value);
  formData.append("fecha", document.getElementById("ca-fecha").value);
  formData.append("medio_pago", document.getElementById("ca-medio-pago").value);
  formData.append("descripcion", document.getElementById("ca-descripcion").value);
  const archivo = document.getElementById("ca-comprobante").files[0];
  if (archivo) formData.append("comprobante", archivo);

  try {
    const url = id ? `/api/caja/movimiento/${id}` : "/api/caja/aporte";
    const res = await fetch(url, { method: id ? "PUT" : "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Error al guardar el aporte");
    toast(id ? "Aporte actualizado" : "Aporte registrado");
    cerrarModal("modal-caja-aporte");
    await cargarCaja();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---- Generar rendición ----
// Migración 25: acá cada retiro pendiente se pincha para desplegarlo y
// agregarle una o más boletas (N° de documento, monto, rubro, detalle y,
// si corresponde, OT relacionada). Cada boleta se guarda al tiro contra el
// servidor -- no se pierde si se cierra el navegador -- y el retiro queda
// "listo para rendir" cuando sus boletas suman el monto completo (o se
// cierra manualmente). Solo los retiros "listos" se pueden marcar para
// incluir en la rendición final (el botón grande de abajo).

function actualizarTotalSeleccionadoRendicion() {
  // Desde la Migración 26 se selecciona por BOLETA (no por gasto completo)
  // -- el total de la rendición es exactamente la suma de las boletas
  // marcadas, nunca el monto del retiro completo.
  const total = [...state.cajaPendientesSeleccionados].reduce((suma, id) => {
    const chk = document.querySelector(`[data-boleta-seleccionable="${id}"]`);
    return suma + (chk ? Number(chk.dataset.monto) : 0);
  }, 0);
  document.getElementById("cr-total-seleccionado").textContent = fmtMoney(total);
}

function optionsRubroGav(valorActual) {
  return Object.entries(RUBRO_GAV_LABEL).map(([valor, label]) =>
    `<option value="${valor}" ${valor === (valorActual || "") ? "selected" : ""}>${label}</option>`
  ).join("");
}

// Mismos rubros que optionsRubroGav, pero sin "Gastos por OT" -- ese es
// exclusivo de Caja Chica (liga un gasto a una orden puntual, ver
// RUBROS_GAV_QUE_SUMAN_GAV en modulos/finanzas.py), no tiene sentido para
// un ítem de "gasto general" de una factura de proveedor.
function optionsRubroGavFactura(valorActual) {
  return Object.entries(RUBRO_GAV_LABEL)
    .filter(([valor]) => valor !== "gastos_por_ot")
    .map(([valor, label]) => `<option value="${valor}" ${valor === (valorActual || "") ? "selected" : ""}>${label}</option>`)
    .join("");
}

function renderBoletaItem(b) {
  const rubroTxt = b.rubro_gav ? (RUBRO_GAV_LABEL[b.rubro_gav] || b.rubro_gav) : "sin clasificar";
  const otTxt = b.rubro_gav === "gastos_por_ot" && b.orden_id ? ` · OT #${b.orden_id}` : "";
  const info = `
      <span class="cr-boleta-doc">${escapeHtml(b.numero_documento) || "(sin N°)"}</span>
      <span class="cr-boleta-rubro">${rubroTxt}${otTxt}${b.descripcion ? " — " + escapeHtml(b.descripcion) : ""}</span>
      <span class="cr-boleta-monto mono">${fmtMoney(b.monto)}</span>`;
  // Una boleta ya rendida (Migración 26: la rendición se genera por boleta,
  // no por gasto completo) queda de solo lectura acá -- no se puede volver
  // a seleccionar, editar ni eliminar (ver caja_editar_boleta/caja_eliminar_boleta).
  // Migración 27: si esta boleta ingresó stock al inventario (compra de
  // emergencia), no se puede editar ni eliminar sin descuadrar el
  // inventario -- ver caja_editar_boleta/caja_eliminar_boleta. Se avisa acá
  // igual que con una boleta ya rendida.
  const stockTxt = b.ingreso_stock ? ` <span class="cr-boleta-estado">📦 ingresó stock</span>` : "";
  if (b.rendido) {
    return `
    <div class="cr-boleta-item cr-boleta-item--rendida" data-boleta-id="${b.id}">
      ${info}
      <span class="cr-boleta-estado">✓ ya rendida</span>${stockTxt}
    </div>`;
  }
  // Preseleccionada por defecto (ver renderPendientesRendicion): el caso más
  // común es rendir todo lo que ya está documentado, así que alcanza con
  // desmarcar lo que no corresponda en vez de tener que marcar boleta por
  // boleta.
  const checked = state.cajaPendientesSeleccionados.has(b.id) ? "checked" : "";
  const quitarBtn = b.ingreso_stock ? "" : `<button type="button" class="btn btn-secundario" data-eliminar-boleta="${b.id}">Quitar</button>`;
  return `
    <div class="cr-boleta-item" data-boleta-id="${b.id}">
      <input type="checkbox" data-boleta-seleccionable="${b.id}" data-monto="${b.monto}" ${checked}>
      ${info}${stockTxt}
      ${quitarBtn}
    </div>`;
}

function renderFormBoleta(gastoId, montoSugerido) {
  return `
    <form class="cr-form-boleta" data-form-boleta="${gastoId}">
      <div class="grid-2">
        <div class="campo">
          <label>N° de boleta o factura</label>
          <input type="text" data-campo="numero_documento" placeholder="Ej: 4521">
        </div>
        <div class="campo">
          <label>Monto de esta boleta ($ CLP) *</label>
          <input type="number" data-campo="monto" required min="1" step="1" value="${montoSugerido > 0 ? montoSugerido : ""}">
        </div>
      </div>
      <div class="campo">
        <label>Rubro (para el Estado de Resultados)</label>
        <select data-campo="rubro_gav">
          <option value="">Sin clasificar</option>
          ${optionsRubroGav("")}
        </select>
      </div>
      <div class="campo" data-orden-wrap style="display:none">
        <label>Orden de trabajo relacionada *</label>
        <input type="text" data-campo-orden-id placeholder="Ej: 123" inputmode="numeric">
        <p class="ayuda-inline" data-orden-preview style="display:none"></p>
      </div>
      <div class="campo">
        <label>Detalle (opcional)</label>
        <input type="text" data-campo="descripcion" placeholder="Ej: Pilas para el multímetro">
      </div>
      <div class="campo" data-emergencia-wrap>
        <label class="toggle-critico">
          <input type="checkbox" data-campo-emergencia>
          Esta boleta es de una compra de emergencia — ingresar el stock a un producto del inventario
        </label>
        <div data-emergencia-campos style="display:none">
          <div class="grid-2">
            <div class="campo">
              <label>Producto</label>
              <select data-campo-producto-id>
                <option value="">Selecciona un producto…</option>
              </select>
            </div>
            <div class="campo">
              <label>Cantidad</label>
              <input type="number" data-campo-cantidad min="1" value="1">
            </div>
          </div>
          <p class="ayuda-inline">El costo unitario se calcula solo, dividiendo el monto de esta boleta entre la cantidad.</p>
        </div>
      </div>
      <div class="modal-actions">
        <button type="submit" class="btn btn-primary">Agregar boleta</button>
      </div>
    </form>`;
}

let productosParaEmergenciaCache = null;
async function poblarProductosEmergencia(select) {
  try {
    if (!productosParaEmergenciaCache) {
      const data = await api("/api/productos");
      productosParaEmergenciaCache = data.productos;
    }
    select.innerHTML = '<option value="">Selecciona un producto…</option>' +
      productosParaEmergenciaCache.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("");
  } catch (e) {
    toast(e.message, true);
  }
}

function renderGastoCard(g) {
  const abierto = state.crGastoAbierto === g.id;
  // Ya no hay un único estado "listo/no listo" a nivel de retiro (Migración
  // 26): puede tener a la vez plata sin boleta Y boletas ya listas para
  // rendir. Se muestran ambos datos, cada uno solo si aplica.
  const badges = [];
  if (g.monto_pendiente > 0) {
    badges.push(`<span class="cr-gasto-estado cr-gasto-estado--pendiente">faltan ${fmtMoney(g.monto_pendiente)} sin boleta${g.cerrado_manualmente ? " (cerrado manual)" : ""}</span>`);
  }
  if (g.monto_por_rendir > 0) {
    badges.push(`<span class="cr-gasto-estado cr-gasto-estado--listo">${fmtMoney(g.monto_por_rendir)} listo para rendir</span>`);
  }
  return `
    <div class="cr-gasto ${abierto ? "cr-gasto--abierto" : ""}" data-gasto-id="${g.id}">
      <div class="cr-gasto-resumen" data-toggle-gasto="${g.id}">
        <span class="cr-gasto-fecha">${fmtFecha(g.fecha)}</span>
        <span>${MEDIO_PAGO_LABEL[g.medio_pago] || g.medio_pago}</span>
        <span class="cr-gasto-monto mono">${fmtMoney(g.monto)}</span>
        ${badges.join("")}
        <span class="cr-gasto-chevron">▾</span>
      </div>
      <div class="cr-gasto-detalle" style="display:${abierto ? "flex" : "none"}">
        <div class="cr-boletas-lista">
          ${(g.boletas || []).map(renderBoletaItem).join("") || `<p class="ayuda-inline">Sin boletas todavía.</p>`}
        </div>
        ${renderFormBoleta(g.id, g.monto_pendiente)}
        <div class="cr-gasto-cerrar">
          <span>${g.monto_pendiente > 0 ? `Quedan ${fmtMoney(g.monto_pendiente)} sin boleta — se puede rendir el resto igual.` : "Monto completamente justificado."}</span>
          <button type="button" class="btn btn-secundario" data-toggle-cierre="${g.id}" data-cerrado="${g.cerrado_manualmente ? "1" : "0"}">
            ${g.cerrado_manualmente ? "Reabrir" : "Marcar cerrado (sin más boletas)"}
          </button>
        </div>
      </div>
    </div>`;
}

function wireGastoCard(cont, g) {
  const card = cont.querySelector(`.cr-gasto[data-gasto-id="${g.id}"]`);
  if (!card) return;

  // Una casilla por BOLETA (Migración 26) -- vienen preseleccionadas desde
  // renderPendientesRendicion, acá solo se sincroniza el Set cuando el
  // usuario las toca.
  card.querySelectorAll("[data-boleta-seleccionable]").forEach(chk => {
    const boletaId = parseInt(chk.dataset.boletaSeleccionable, 10);
    chk.addEventListener("change", () => {
      if (chk.checked) state.cajaPendientesSeleccionados.add(boletaId);
      else state.cajaPendientesSeleccionados.delete(boletaId);
      actualizarTotalSeleccionadoRendicion();
    });
  });

  card.querySelector("[data-toggle-gasto]").addEventListener("click", (e) => {
    if (e.target.closest("input,button")) return; // no interceptar el checkbox
    state.crGastoAbierto = state.crGastoAbierto === g.id ? null : g.id;
    renderPendientesRendicion(cont);
  });

  card.querySelectorAll("[data-eliminar-boleta]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm("¿Quitar esta boleta?")) return;
      try {
        const actualizado = await api(`/api/caja/boleta/${btn.dataset.eliminarBoleta}`, { method: "DELETE" });
        actualizarGastoEnLista(actualizado);
        renderPendientesRendicion(cont);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  const toggleCierre = card.querySelector("[data-toggle-cierre]");
  if (toggleCierre) {
    toggleCierre.addEventListener("click", async () => {
      const cerrarAhora = toggleCierre.dataset.cerrado !== "1";
      try {
        const actualizado = await api(`/api/caja/gasto/${g.id}/cerrar`, {
          method: "PUT", body: JSON.stringify({ cerrado: cerrarAhora }),
        });
        actualizarGastoEnLista(actualizado);
        renderPendientesRendicion(cont);
      } catch (e) {
        toast(e.message, true);
      }
    });
  }

  const formBoleta = card.querySelector(`[data-form-boleta="${g.id}"]`);
  if (formBoleta) {
    const selectRubro = formBoleta.querySelector('[data-campo="rubro_gav"]');
    const ordenWrap = formBoleta.querySelector("[data-orden-wrap]");
    const ordenInput = formBoleta.querySelector("[data-campo-orden-id]");
    const ordenPreview = formBoleta.querySelector("[data-orden-preview]");
    let ordenEncontrada = null;
    let ordenLookupTimeout;

    async function buscarOrden() {
      const numero = ordenInput.value.trim();
      ordenEncontrada = null;
      if (!numero) { ordenPreview.style.display = "none"; return; }
      try {
        const orden = await api(`/api/operativo/ordenes/${encodeURIComponent(numero)}`);
        const cliente = orden.cliente ? orden.cliente.nombre : "";
        ordenPreview.textContent = `✓ OT #${orden.id}${cliente ? " — " + cliente : ""} · estado: ${orden.estado}`;
        ordenPreview.style.display = "block";
        ordenEncontrada = orden;
      } catch (e) {
        ordenPreview.textContent = e.message || `No existe una orden de trabajo con el número "${numero}".`;
        ordenPreview.style.display = "block";
      }
    }

    // Migración 27: "compra de emergencia" (ingresa stock al inventario) se
    // pregunta acá, boleta por boleta -- recién al cargar la boleta se sabe
    // qué se compró de verdad (ver caja_agregar_boleta).
    const chkEmergencia = formBoleta.querySelector("[data-campo-emergencia]");
    const camposEmergencia = formBoleta.querySelector("[data-emergencia-campos]");
    const selectProducto = formBoleta.querySelector("[data-campo-producto-id]");

    // Una boleta no puede ser "compra de emergencia" (ingresa el repuesto al
    // inventario) y "Gastos por OT" (carga ese mismo monto como costo de la
    // orden) al mismo tiempo -- el repuesto ya se cobraría dos veces (ver
    // hallazgo #10 del informe de revisión, y la misma validación del lado
    // del servidor en caja_agregar_boleta). Se mantienen mutuamente
    // excluyentes en la interfaz para que el error nunca llegue a mostrarse.
    selectRubro.addEventListener("change", () => {
      const esOt = selectRubro.value === "gastos_por_ot";
      ordenWrap.style.display = esOt ? "flex" : "none";
      if (!esOt) { ordenInput.value = ""; ordenPreview.style.display = "none"; ordenEncontrada = null; }
      if (esOt && chkEmergencia.checked) {
        chkEmergencia.checked = false;
        camposEmergencia.style.display = "none";
        selectProducto.value = "";
        toast("Se desmarcó \"compra de emergencia\": una boleta no puede ser las dos cosas a la vez", true);
      }
    });
    ordenInput.addEventListener("input", () => {
      clearTimeout(ordenLookupTimeout);
      ordenLookupTimeout = setTimeout(buscarOrden, 400);
    });

    chkEmergencia.addEventListener("change", async () => {
      camposEmergencia.style.display = chkEmergencia.checked ? "block" : "none";
      if (chkEmergencia.checked && selectProducto.options.length <= 1) {
        await poblarProductosEmergencia(selectProducto);
      }
      if (chkEmergencia.checked && selectRubro.value === "gastos_por_ot") {
        selectRubro.value = "";
        ordenWrap.style.display = "none";
        ordenInput.value = "";
        ordenPreview.style.display = "none";
        ordenEncontrada = null;
        toast("Se cambió el rubro a \"Sin clasificar\": una boleta no puede ser \"Gastos por OT\" y \"compra de emergencia\" a la vez", true);
      }
    });

    formBoleta.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (selectRubro.value === "gastos_por_ot" && !ordenEncontrada) {
        toast("Indica una orden de trabajo válida para \"Gastos por OT\"", true);
        return;
      }
      if (chkEmergencia.checked && selectRubro.value === "gastos_por_ot") {
        toast("Una boleta no puede ser \"compra de emergencia\" y \"Gastos por OT\" al mismo tiempo", true);
        return;
      }
      const fd = new FormData();
      fd.append("numero_documento", formBoleta.querySelector('[data-campo="numero_documento"]').value);
      fd.append("monto", formBoleta.querySelector('[data-campo="monto"]').value);
      fd.append("rubro_gav", selectRubro.value);
      fd.append("descripcion", formBoleta.querySelector('[data-campo="descripcion"]').value);
      fd.append("orden_id", selectRubro.value === "gastos_por_ot" && ordenEncontrada ? ordenEncontrada.id : "");
      if (chkEmergencia.checked) {
        fd.append("producto_id", selectProducto.value);
        fd.append("cantidad", formBoleta.querySelector("[data-campo-cantidad]").value);
      }
      try {
        const res = await fetch(`/api/caja/gasto/${g.id}/boleta`, { method: "POST", body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || "Error al agregar la boleta");
        toast("Boleta agregada");
        state.crGastoAbierto = g.id; // se mantiene abierto para agregar otra
        actualizarGastoEnLista(data);
        renderPendientesRendicion(cont);
      } catch (e) {
        toast(e.message, true);
      }
    });
  }
}

function actualizarGastoEnLista(gastoActualizado) {
  state.cajaPendientesLista = (state.cajaPendientesLista || []).map(g =>
    g.id === gastoActualizado.id ? gastoActualizado : g
  );
}

function renderPendientesRendicion(cont) {
  const gastos = state.cajaPendientesLista || [];
  const medio = document.getElementById("cr-medio-pago").value;
  if (gastos.length === 0) {
    cont.innerHTML = `<p class="vacio">No hay boletas ni retiros pendientes en ${MEDIO_PAGO_LABEL[medio] || medio}. 👍</p>`;
    actualizarTotalSeleccionadoRendicion();
    return;
  }
  // Toda boleta sin rendir que aparece por primera vez (recién cargada, o
  // recién vista en este modal) queda preseleccionada sola -- el caso más
  // común es rendir todo lo que ya está documentado, así que alcanza con
  // desmarcar lo que no corresponda en vez de tener que marcar boleta por
  // boleta (ver renderBoletaItem).
  gastos.forEach(g => (g.boletas || []).forEach(b => {
    if (!b.rendido && !state.cajaBoletasConocidas.has(b.id)) {
      state.cajaBoletasConocidas.add(b.id);
      state.cajaPendientesSeleccionados.add(b.id);
    }
  }));
  cont.innerHTML = gastos.map(renderGastoCard).join("");
  gastos.forEach(g => wireGastoCard(cont, g));
  actualizarTotalSeleccionadoRendicion();
}

async function cargarPendientesRendicion() {
  state.cajaPendientesSeleccionados = new Set();
  state.cajaBoletasConocidas = new Set();
  state.crGastoAbierto = null;
  const cont = document.getElementById("cr-lista-pendientes");
  cont.innerHTML = `<p class="vacio">Cargando…</p>`;
  const medio = document.getElementById("cr-medio-pago").value;

  try {
    const data = await api("/api/caja/pendientes?medio_pago=" + encodeURIComponent(medio));
    state.cajaPendientesLista = data.gastos;
    renderPendientesRendicion(cont);
  } catch (e) {
    cont.innerHTML = `<p class="vacio">Error al cargar retiros pendientes: ${escapeHtml(e.message)}</p>`;
  }
}

document.getElementById("btn-generar-rendicion").addEventListener("click", async () => {
  document.getElementById("cr-nota").value = "";
  document.getElementById("cr-reponer-saldo").checked = true;
  document.getElementById("cr-medio-pago").value = "efectivo_cajero";
  abrirModal("modal-caja-rendicion");
  await cargarPendientesRendicion();
});

document.getElementById("cr-medio-pago").addEventListener("change", () => cargarPendientesRendicion());

document.getElementById("btn-confirmar-rendicion").addEventListener("click", async () => {
  // Desde la Migración 26 se rinde por BOLETA -- el total de la rendición es
  // exactamente la suma de lo seleccionado, nunca el monto completo de un
  // retiro que todavía no esté 100% documentado.
  const detalleIds = [...state.cajaPendientesSeleccionados];
  if (detalleIds.length === 0) {
    toast("Selecciona al menos una boleta para rendir", true);
    return;
  }
  const reponerSaldo = document.getElementById("cr-reponer-saldo").checked;
  try {
    await api("/api/caja/rendicion", {
      method: "POST",
      body: JSON.stringify({
        detalle_ids: detalleIds,
        nota: document.getElementById("cr-nota").value,
        reponer_saldo: reponerSaldo,
      }),
    });
    toast(reponerSaldo ? "Rendición generada — el saldo ya se repuso" : "Rendición generada — no se agregó ningún aporte nuevo");
    cerrarModal("modal-caja-rendicion");
    await cargarCaja();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---- Historial de rendiciones ----

document.getElementById("btn-ver-rendiciones").addEventListener("click", async () => {
  const tbody = document.getElementById("tbody-caja-rendiciones");
  tbody.innerHTML = `<tr><td colspan="7" class="vacio">Cargando…</td></tr>`;
  abrirModal("modal-caja-rendiciones-historial");
  try {
    const data = await api("/api/caja/rendiciones");
    if (data.rendiciones.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="vacio">Todavía no se ha generado ninguna rendición.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.rendiciones.map(r => `
      <tr data-id="${r.id}">
        <td class="mono">${fmtFecha(r.fecha)}</td>
        <td>${MEDIO_PAGO_LABEL[r.medio_pago] || r.medio_pago || "—"}</td>
        <td class="num mono">${r.n_boletas}</td>
        <td class="num mono">${fmtMoney(r.total)}</td>
        <td>${r.reposicion_automatica ? "Sí" : "No"}</td>
        <td>${escapeHtml(r.nota) || "—"}</td>
        <td>${escapeHtml(r.usuario) || "—"}</td>
      </tr>
    `).join("");
    tbody.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.addEventListener("click", () => verDetalleRendicion(parseInt(tr.dataset.id, 10)));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Error al cargar el historial: ${escapeHtml(e.message)}</td></tr>`;
  }
});

async function verDetalleRendicion(id) {
  document.getElementById("modal-caja-rendicion-detalle-titulo").textContent = `Rendición #${id}`;
  const ficha = document.getElementById("crd-ficha");
  const tbody = document.getElementById("tbody-caja-rendicion-detalle");
  ficha.innerHTML = "";
  tbody.innerHTML = `<tr><td colspan="5" class="vacio">Cargando…</td></tr>`;
  abrirModal("modal-caja-rendicion-detalle");
  try {
    const r = await api(`/api/caja/rendicion/${id}`);
    ficha.innerHTML = `
      <dt>Fecha</dt><dd>${fmtFecha(r.fecha)}</dd>
      <dt>Medio de pago</dt><dd>${MEDIO_PAGO_LABEL[r.medio_pago] || r.medio_pago || "—"}</dd>
      <dt>Total</dt><dd class="mono">${fmtMoney(r.total)}</dd>
      <dt>N° de boletas</dt><dd class="mono">${r.boletas.length}</dd>
      <dt>¿Repuso el saldo?</dt><dd>${r.reposicion_automatica ? "Sí — se creó un aporte automático por el mismo total" : "No — estos gastos ya estaban cubiertos"}</dd>
      <dt>Nota</dt><dd>${escapeHtml(r.nota) || "—"}</dd>
      <dt>Responsable</dt><dd>${escapeHtml(r.usuario) || "—"}</dd>
    `;
    // Desde la Migración 26 una rendición junta BOLETAS individuales
    // directamente (ya no gastos completos con boletas adentro).
    tbody.innerHTML = r.boletas.length
      ? r.boletas.map(b => `
        <tr>
          <td class="mono">${fmtFecha(b.fecha_retiro)}</td>
          <td>${escapeHtml(b.numero_documento) || "—"}</td>
          <td>${b.rubro_gav ? (RUBRO_GAV_LABEL[b.rubro_gav] || b.rubro_gav) + (b.rubro_gav === "gastos_por_ot" && b.orden_id ? ` · OT #${b.orden_id}` : "") : "—"}</td>
          <td>${escapeHtml(b.descripcion) || "—"}</td>
          <td class="num mono">${fmtMoney(b.monto)}</td>
        </tr>`).join("")
      : `<tr><td colspan="5" class="vacio">Sin boletas asociadas.</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---------------------------------------------------------------------------
// Cuentas por Cobrar (CxC) — solo lectura: se generan solas al entregar una
// OT (ver modulos/operativo.py: _generar_o_actualizar_cxc). Los abonos se
// registran desde la ficha de la OT en el módulo Operativo, no acá.
// ---------------------------------------------------------------------------

async function cargarCxc() {
  const tbody = document.getElementById("tbody-cxc");
  const params = new URLSearchParams();
  const q = document.getElementById("cxc-buscar").value.trim();
  const estado = document.getElementById("cxc-filtro-estado").value;
  if (q) params.set("q", q);
  if (estado) params.set("estado", estado);

  try {
    const data = await api("/api/finanzas/cxc?" + params.toString());
    state.cxcCuentas = data.cuentas;
    document.getElementById("cxc-total-pendiente").textContent = fmtMoney(data.total_pendiente);

    if (data.cuentas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="vacio">No se encontraron cuentas por cobrar con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.cuentas.map(c => `
      <tr data-orden-id="${c.orden_id}" class="fila-clicable">
        <td>${escapeHtml(c.cliente_nombre)}</td>
        <td class="mono">#${c.orden_id}</td>
        <td class="mono">${c.fecha_entrega_real ? fmtFecha(c.fecha_entrega_real) : "—"}</td>
        <td class="num mono">${fmtMoney(c.monto_total)}</td>
        <td class="num mono">${fmtMoney(c.total_pagado)}</td>
        <td class="num mono">${fmtMoney(c.saldo)}</td>
        <td><span class="mov-tipo mov-tipo--${c.estado === "pagada" ? "entrada" : c.estado === "parcial" ? "salida" : "perdida"}">${ESTADO_CXX_LABEL[c.estado] || c.estado}</span></td>
        <td class="num mono">${diasDesde(c.fecha_emision)}</td>
      </tr>`).join("");
    tbody.querySelectorAll("tr[data-orden-id]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleOtCxc(parseInt(tr.dataset.ordenId, 10), "cerradas"));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="vacio">Error al cargar Cuentas por Cobrar: ${escapeHtml(e.message)}</td></tr>`;
  }
}

let cxcBuscarTimeout;
document.getElementById("cxc-buscar").addEventListener("input", () => {
  clearTimeout(cxcBuscarTimeout);
  cxcBuscarTimeout = setTimeout(cargarCxc, 250);
});
document.getElementById("cxc-filtro-estado").addEventListener("change", cargarCxc);

// ---------------------------------------------------------------------------
// Cuentas por Cobrar "por abonos" — OTs que ya entraron a reparación y
// todavía no alcanzan el anticipo pactado (ver /api/finanzas/cxc/abonos en
// modulos/finanzas.py). Es una consulta en vivo, no una tabla congelada
// como cuentas_por_cobrar: por eso no tiene filtro de estado ni columna de
// estado, solo "cuánto falta para llegar al anticipo".
// ---------------------------------------------------------------------------

async function cargarCxcAbonos() {
  const tbody = document.getElementById("tbody-cxc-abonos");
  const params = new URLSearchParams();
  const q = document.getElementById("cxc-abonos-buscar").value.trim();
  if (q) params.set("q", q);

  try {
    const data = await api("/api/finanzas/cxc/abonos?" + params.toString());
    state.cxcAbonosCuentas = data.cuentas;
    document.getElementById("cxc-abonos-total-pendiente").textContent = fmtMoney(data.total_pendiente);

    if (data.cuentas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No hay OTs en reparación que estén bajo el anticipo pactado. 👍</td></tr>`;
      return;
    }

    tbody.innerHTML = data.cuentas.map(c => {
      const equipo = [c.equipo_marca, c.equipo_modelo, c.equipo_identificador ? `(${c.equipo_identificador})` : ""]
        .filter(Boolean).join(" ") || "—";
      return `
      <tr data-orden-id="${c.orden_id}" class="fila-clicable">
        <td>${escapeHtml(c.cliente_nombre)}</td>
        <td class="mono">#${c.orden_id}</td>
        <td>${escapeHtml(equipo)}</td>
        <td class="mono">${c.fecha_recepcion ? fmtFecha(c.fecha_recepcion) : "—"}</td>
        <td class="num mono">${fmtMoney(c.valor_total)}</td>
        <td class="num mono">${fmtMoney(c.anticipo_esperado)}</td>
        <td class="num mono">${fmtMoney(c.total_pagado)}</td>
        <td class="num mono">${fmtMoney(c.saldo_anticipo)}</td>
        <td class="num mono">${diasDesde(c.fecha_recepcion)}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll("tr[data-orden-id]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleOtCxc(parseInt(tr.dataset.ordenId, 10), "abonos"));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar CxC por abonos: ${escapeHtml(e.message)}</td></tr>`;
  }
}

let cxcAbonosBuscarTimeout;
document.getElementById("cxc-abonos-buscar").addEventListener("input", () => {
  clearTimeout(cxcAbonosBuscarTimeout);
  cxcAbonosBuscarTimeout = setTimeout(cargarCxcAbonos, 250);
});

// ---------------------------------------------------------------------------
// Detalle de una OT desde Cuentas por Cobrar (cualquiera de las dos
// sub-pestañas) — datos de cliente/equipo, plata (total/pagado/saldo, y el
// anticipo esperado si viene del contexto "abonos"), y un mini-formulario
// para registrar un pago sin tener que salir a Operativo. Reutiliza
// GET /api/operativo/ordenes/<id> (mismo endpoint que la ficha completa de
// la OT) y POST .../pagos.
// ---------------------------------------------------------------------------

async function abrirDetalleOtCxc(ordenId, contexto) {
  state.cxcOtActiva = { ordenId, contexto };
  document.getElementById("cxcd-titulo").textContent = `OT #${ordenId}`;
  document.getElementById("cxcd-orden-id").value = ordenId;
  document.getElementById("cxcd-ficha").innerHTML = "<dt>Cargando…</dt><dd></dd>";
  document.getElementById("form-cxcd-pago").reset();
  abrirModal("modal-cxc-ot-detalle");
  try {
    const o = await api(`/api/operativo/ordenes/${ordenId}`);
    const equipo = o.equipo
      ? [o.equipo.marca, o.equipo.modelo, o.equipo.identificador ? `(${o.equipo.identificador})` : ""].filter(Boolean).join(" ")
      : "—";
    let filas = `
      <dt>Cliente</dt><dd>${escapeHtml(o.cliente ? o.cliente.nombre : "—")}</dd>
      <dt>Teléfono</dt><dd>${escapeHtml(o.cliente ? o.cliente.telefono : "") || "—"}</dd>
      <dt>Equipo</dt><dd>${escapeHtml(equipo)}</dd>
      <dt>Estado de la OT</dt><dd>${escapeHtml(o.estado)}</dd>
      <dt>Valor total</dt><dd class="mono">${fmtMoney(o.valor_total)}</dd>
      <dt>Pagado</dt><dd class="mono">${fmtMoney(o.total_pagado)}</dd>
      <dt>Saldo pendiente</dt><dd class="mono">${fmtMoney(o.total_por_pagar)}</dd>
    `;
    if (contexto === "abonos") {
      const c = state.cxcAbonosCuentas.find(x => x.orden_id === ordenId);
      if (c) {
        filas += `
          <dt>Anticipo esperado (50%)</dt><dd class="mono">${fmtMoney(c.anticipo_esperado)}</dd>
          <dt>Falta para el anticipo</dt><dd class="mono">${fmtMoney(c.saldo_anticipo)}</dd>
        `;
      }
    }
    document.getElementById("cxcd-ficha").innerHTML = filas;
    document.getElementById("cxcd-monto").value = o.total_por_pagar > 0 ? o.total_por_pagar : "";
  } catch (e) {
    document.getElementById("cxcd-ficha").innerHTML = `<dt>Error</dt><dd>${escapeHtml(e.message)}</dd>`;
  }
}

document.getElementById("form-cxcd-pago").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("cxcd-orden-id").value;
  // Ver comentario en form-op-orden-nueva (operativo.js): sin bloquear el
  // botón, un doble clic registraba dos abonos idénticos a la misma OT
  // (hallazgo #4).
  const btnPago = e.target.querySelector('button[type="submit"]');
  btnPago.disabled = true;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/pagos`, {
      method: "POST",
      body: JSON.stringify({
        monto: Number(document.getElementById("cxcd-monto").value),
        metodo_pago: document.getElementById("cxcd-metodo").value,
      }),
    });
    toast("Pago registrado");
    cerrarModal("modal-cxc-ot-detalle");
    // No hace falta lógica especial acá: se vuelve a pedir el listado que
    // estaba abierto y, si el pago hizo que la fila ya no califique (llegó
    // al anticipo, o se saldó del todo), el backend simplemente deja de
    // devolverla.
    const contexto = state.cxcOtActiva ? state.cxcOtActiva.contexto : "cerradas";
    if (contexto === "abonos") await cargarCxcAbonos();
    else await cargarCxc();
    await cargarResumenFinanzas();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btnPago.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Cuentas por Pagar (CxP) — se generan solas al registrar una factura de
// compra (ver app.py: _generar_cxp_de_factura). Acá se listan y se
// registran los abonos a proveedor.
// ---------------------------------------------------------------------------

let cxpCuentas = [];

async function cargarCxp() {
  const tbody = document.getElementById("tbody-cxp");
  const params = new URLSearchParams();
  const q = document.getElementById("cxp-buscar").value.trim();
  const estado = document.getElementById("cxp-filtro-estado").value;
  if (q) params.set("q", q);
  if (estado) params.set("estado", estado);

  try {
    const data = await api("/api/finanzas/cxp?" + params.toString());
    cxpCuentas = data.cuentas;
    document.getElementById("cxp-total-pendiente").textContent = fmtMoney(data.total_pendiente);

    if (data.cuentas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No se encontraron cuentas por pagar con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.cuentas.map(c => {
      const puedePagar = c.estado !== "pagada" && c.estado !== "anulada";
      const puedeFinanzas = state.sesion && (state.sesion.rol === "admin" || (state.sesion.modulos || []).includes("finanzas"));
      const puedeAnular = puedeFinanzas && !c.factura_id && c.estado === "pendiente" && Number(c.total_pagado) === 0;
      const acciones = [
        puedePagar ? `<button type="button" class="btn btn-secundario" data-pagar-cxp="${c.id}">Registrar pago</button>` : "",
        puedeAnular ? `<button type="button" class="btn btn-secundario" data-anular-cxp="${c.id}">Anular</button>` : "",
      ].filter(Boolean).join(" ");
      return `
      <tr>
        <td>${escapeHtml(c.proveedor_nombre) || "—"}</td>
        <td>${escapeHtml(c.factura_numero ? `Factura ${c.factura_numero}` : c.concepto) || "—"}</td>
        <td class="mono">${fmtFecha(c.fecha_emision)}</td>
        <td class="mono">${c.fecha_vencimiento ? fmtFecha(c.fecha_vencimiento) : "—"}</td>
        <td class="num mono">${fmtMoney(c.monto_total)}</td>
        <td class="num mono">${fmtMoney(c.total_pagado)}</td>
        <td class="num mono">${fmtMoney(c.saldo)}</td>
        <td><span class="mov-tipo mov-tipo--${c.estado === "pagada" ? "entrada" : c.estado === "parcial" ? "salida" : "perdida"}">${ESTADO_CXX_LABEL[c.estado] || c.estado}</span></td>
        <td>${acciones}</td>
      </tr>`;
    }).join("");

    tbody.querySelectorAll("[data-pagar-cxp]").forEach(btn => {
      btn.addEventListener("click", () => abrirPagoCxp(parseInt(btn.dataset.pagarCxp, 10)));
    });
    tbody.querySelectorAll("[data-anular-cxp]").forEach(btn => {
      btn.addEventListener("click", () => anularCxpManual(parseInt(btn.dataset.anularCxp, 10)));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar Cuentas por Pagar: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function anularCxpManual(id) {
  if (!confirm("¿Anular esta cuenta por pagar? No se puede deshacer.")) return;
  try {
    await api(`/api/finanzas/cxp/${id}/anular`, { method: "POST" });
    toast("Cuenta por pagar anulada");
    await cargarCxp();
    await cargarResumenFinanzas();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Registrar Cuenta por Pagar manual (gasto sin factura de repuestos) —
// para deudas del taller que no vienen de una factura de compra registrada
// en Logística (arriendo, servicios, contador, etc.). Ver
// finanzas_crear_cxp_manual en modulos/finanzas.py.
// ---------------------------------------------------------------------------

let proveedoresCacheCxpManual = null;

async function abrirModalCxpManual() {
  document.getElementById("form-cxp-manual").reset();
  document.getElementById("cxpm-fecha-emision").value = new Date().toISOString().slice(0, 10);
  try {
    if (!proveedoresCacheCxpManual) {
      const data = await api("/api/proveedores");
      proveedoresCacheCxpManual = data.proveedores;
    }
    const select = document.getElementById("cxpm-proveedor");
    select.innerHTML = '<option value="">— Sin proveedor asociado —</option>' +
      proveedoresCacheCxpManual.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
  } catch (e) {
    toast(e.message, true);
  }
  abrirModal("modal-cxp-manual");
}

document.getElementById("btn-nueva-cxp-manual").addEventListener("click", abrirModalCxpManual);

document.getElementById("form-cxp-manual").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/finanzas/cxp", {
      method: "POST",
      body: JSON.stringify({
        concepto: document.getElementById("cxpm-concepto").value,
        proveedor_id: document.getElementById("cxpm-proveedor").value || null,
        monto_total: Number(document.getElementById("cxpm-monto").value),
        fecha_emision: document.getElementById("cxpm-fecha-emision").value,
        fecha_vencimiento: document.getElementById("cxpm-fecha-vencimiento").value,
      }),
    });
    toast("Cuenta por pagar registrada");
    cerrarModal("modal-cxp-manual");
    await cargarCxp();
    await cargarResumenFinanzas();
  } catch (e) {
    toast(e.message, true);
  }
});

let cxpBuscarTimeout;
document.getElementById("cxp-buscar").addEventListener("input", () => {
  clearTimeout(cxpBuscarTimeout);
  cxpBuscarTimeout = setTimeout(cargarCxp, 250);
});
document.getElementById("cxp-filtro-estado").addEventListener("change", cargarCxp);

function abrirPagoCxp(id) {
  const c = cxpCuentas.find(x => x.id === id);
  if (!c) return;
  document.getElementById("form-cxp-pago").reset();
  document.getElementById("cxpp-cxp-id").value = c.id;
  document.getElementById("cxpp-monto").value = c.saldo;
  document.getElementById("cxpp-monto").max = c.saldo;
  document.getElementById("modal-cxp-pago-titulo").textContent =
    `Registrar pago — ${c.proveedor_nombre || (c.factura_numero ? `Factura ${c.factura_numero}` : "CxP #" + c.id)}`;
  document.getElementById("cxp-pago-saldo").textContent = `Saldo pendiente: ${fmtMoney(c.saldo)}`;
  abrirModal("modal-cxp-pago");
}

document.getElementById("form-cxp-pago").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cxpId = document.getElementById("cxpp-cxp-id").value;
  // Ver comentario en form-op-orden-nueva (operativo.js): sin bloquear el
  // botón, un doble clic registraba dos abonos idénticos a la misma CxP —
  // y si "desde_caja_chica" estaba marcado, duplicaba también el egreso en
  // Caja Chica (hallazgo #4).
  const btnPago = e.target.querySelector('button[type="submit"]');
  btnPago.disabled = true;
  try {
    await api(`/api/finanzas/cxp/${cxpId}/pagos`, {
      method: "POST",
      body: JSON.stringify({
        monto: Number(document.getElementById("cxpp-monto").value),
        metodo_pago: document.getElementById("cxpp-metodo").value,
        nota: document.getElementById("cxpp-nota").value,
        desde_caja_chica: document.getElementById("cxpp-desde-caja").checked,
      }),
    });
    toast("Pago registrado");
    cerrarModal("modal-cxp-pago");
    await cargarCxp();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btnPago.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Facturas de compra pendientes de recepción — Finanzas las registra acá
// con sus condiciones de pago (contado / crédito / mixto). No aplican
// stock ni generan CxP hasta que se aprueben (ver aviso global 📦 en
// alerta_facturas_pendientes.js, que es donde de verdad se revisan y
// aprueban/rechazan — acá solo se crean y se les hace seguimiento de
// estado).
// ---------------------------------------------------------------------------

const ESTADO_PFR_LABEL = { pendiente: "Pendiente de revisión", aprobada: "Aprobada", rechazada: "Rechazada", eliminada: "Eliminada" };
const EVENTO_HISTORIAL_PFR_LABEL = {
  creada: "Registrada",
  rechazada: "Rechazada por Bodega",
  corregida_reingresada: "Corregida y reingresada",
  aprobada: "Aprobada",
  aplicada_directamente: "Aplicada directamente al ingresarla (sin paso de aprobación aparte)",
  condicion_pago_editada: "Condición de pago corregida por Finanzas",
};
const ORIGEN_PFR_LABEL = { manual: "Manual", excel: "Excel", foto: "Foto", whatsapp: "WhatsApp" };

async function cargarFacturasPendientesRecepcion() {
  const tbody = document.getElementById("tbody-facturas-pendientes");
  try {
    const data = await api("/api/facturas/pendientes-recepcion");
    state.facturasPendientes = data.pendientes;
    if (data.pendientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="vacio">Todavía no se ha registrado ninguna factura por este camino.</td></tr>`;
      return;
    }
    const etiquetaCondicion = { contado: "Contado", credito: "Crédito", mixto: "Mixto" };
    tbody.innerHTML = data.pendientes.map(p => {
      const acciones = [
        // "rechazada" también puede editarse -- es justo el camino para
        // corregirla y que vuelva sola a la cola de revisión (ver
        // editar_factura_pendiente_recepcion en app.py), sin tener que
        // volver a escribirla de cero.
        (p.estado === "pendiente" || p.estado === "rechazada") ? `<button type="button" class="btn btn-secundario btn-chico" data-pfr-editar="${p.id}">${p.estado === "rechazada" ? "Corregir y reingresar" : "Editar"}</button>` : "",
        p.origen === "whatsapp" ? `<a class="btn btn-secundario btn-chico" href="/api/facturas/pendientes-recepcion/${p.id}/foto" target="_blank" rel="noopener">Ver foto</a>` : "",
      ].filter(Boolean).join(" ");
      return `
      <tr class="fila-clicable" data-id="${p.id}">
        <td>${escapeHtml(p.numero) || "—"}</td>
        <td>${escapeHtml(p.proveedor_nombre) || "—"}</td>
        <td>${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}${p.condicion_pago === "credito" ? ` (${p.plazo_dias_credito}d)` : ""}${p.condicion_pago === "mixto" ? ` (${p.porcentaje_contado}% contado)` : ""}</td>
        <td class="num mono">${fmtMoney(p.monto_total)}</td>
        <td><span class="mov-tipo mov-tipo--${p.estado === "aprobada" ? "entrada" : (p.estado === "rechazada" || p.estado === "eliminada") ? "perdida" : "salida"}">${ESTADO_PFR_LABEL[p.estado] || p.estado}</span></td>
        <td><span class="badge-origen">${ORIGEN_PFR_LABEL[p.origen] || p.origen || "—"}</span></td>
        <td>${escapeHtml(p.creado_por) || "—"}</td>
        <td>${acciones}</td>
      </tr>`;
    }).join("");
    // La fila entera es clickeable para revisar (ver detalle, o eliminar
    // si ya fue aprobada) — los botones de "Editar"/"Ver foto" están
    // dentro de la misma fila, así que se detiene la propagación del
    // click para que no abran el detalle Y la acción del botón a la vez.
    tbody.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleFacturaPendiente(parseInt(tr.dataset.id, 10)));
    });
    tbody.querySelectorAll("[data-pfr-editar]").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        abrirEdicionFacturaPendiente(parseInt(btn.dataset.pfrEditar, 10));
      });
    });
    tbody.querySelectorAll("a[href]").forEach(a => a.addEventListener("click", (e) => e.stopPropagation()));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="vacio">Error al cargar: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---------------------------------------------------------------------------
// Pestaña "Gastos Generales": ítems de facturas marcados como gasto general
// (ver es_gasto_general / factura_gastos_generales) -- combustible,
// herramientas y similares que no van a inventario. Se trae la lista
// completa una sola vez y los filtros de mes/rubro se aplican en el
// navegador, sin volver a pedirle nada al servidor.
// ---------------------------------------------------------------------------

let ggItemsCache = [];
let ggRubrosLabel = {};

function ggNombreMes(mes) {
  return new Date(`${mes}-01T00:00:00`).toLocaleDateString("es-CL", { month: "long", year: "numeric" });
}

function ggRenderTabla() {
  const mesFiltro = document.getElementById("gg-filtro-mes").value;
  const rubroFiltro = document.getElementById("gg-filtro-rubro").value;
  const items = ggItemsCache.filter(i =>
    (!mesFiltro || i.mes === mesFiltro) && (!rubroFiltro || i.rubro_gav === rubroFiltro)
  );

  const tbody = document.getElementById("tbody-gastos-generales");
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">No hay gastos generales registrados${mesFiltro || rubroFiltro ? " con este filtro" : ""}.</td></tr>`;
    document.getElementById("gg-total").textContent = fmtMoney(0);
    return;
  }

  // Ya vienen ordenados por fecha DESC desde el servidor -- se agrupan acá
  // por mes (encabezado tipo "Julio 2026") sin perder ese orden, tal como
  // pidió el taller ("separados por mes").
  let filasHtml = "";
  let mesAnterior = null;
  for (const i of items) {
    if (i.mes !== mesAnterior) {
      filasHtml += `<tr class="fila-grupo-mes"><td colspan="6">${escapeHtml(ggNombreMes(i.mes))}</td></tr>`;
      mesAnterior = i.mes;
    }
    filasHtml += `
      <tr>
        <td>${escapeHtml(i.descripcion)}</td>
        <td><span class="badge-origen">${escapeHtml(i.rubro_gav_label || i.rubro_gav)}</span></td>
        <td>${escapeHtml(fmtFecha(i.fecha))}</td>
        <td>${escapeHtml(i.numero_factura) || "—"}</td>
        <td>${escapeHtml(i.proveedor) || "—"}</td>
        <td class="num mono">${fmtMoney(i.monto)}</td>
      </tr>`;
  }
  tbody.innerHTML = filasHtml;

  const total = items.reduce((acc, i) => acc + i.monto, 0);
  document.getElementById("gg-total").textContent = fmtMoney(total);
}

async function cargarGastosGenerales() {
  const tbody = document.getElementById("tbody-gastos-generales");
  try {
    const data = await api("/api/finanzas/gastos-generales");
    ggItemsCache = data.items;
    ggRubrosLabel = data.rubros_label || {};

    const selectRubro = document.getElementById("gg-filtro-rubro");
    const rubroActual = selectRubro.value;
    selectRubro.innerHTML = '<option value="">Todos los rubros</option>' +
      Object.entries(ggRubrosLabel).map(([valor, label]) => `<option value="${valor}">${label}</option>`).join("");
    selectRubro.value = rubroActual;

    const mesesDisponibles = [...new Set(ggItemsCache.map(i => i.mes))].sort().reverse();
    const selectMes = document.getElementById("gg-filtro-mes");
    const mesActual = selectMes.value;
    selectMes.innerHTML = '<option value="">Todos los meses</option>' +
      mesesDisponibles.map(m => `<option value="${m}">${ggNombreMes(m)}</option>`).join("");
    selectMes.value = mesActual;

    ggRenderTabla();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Error al cargar: ${escapeHtml(e.message)}</td></tr>`;
  }
}

document.getElementById("gg-filtro-mes").addEventListener("change", ggRenderTabla);
document.getElementById("gg-filtro-rubro").addEventListener("change", ggRenderTabla);

// ---------------------------------------------------------------------------
// Detalle de una factura (pendiente, aprobada o rechazada) al hacer click en
// su fila — para las ya aprobadas, muestra las líneas tal como quedaron en
// la factura real (con si se aplicaron o no a stock) y permite eliminarla
// (mismo endpoint DELETE /api/facturas/<id> de siempre, que revierte el
// stock que haya aplicado — pide confirmación en dos pasos igual que el
// viejo modal de Logística: primero avisa qué se va a revertir, y solo al
// confirmar de nuevo ejecuta el borrado con forzar=1).
// ---------------------------------------------------------------------------

let pfdPendienteActivo = null;
let pfdFacturaIdReal = null;

async function abrirDetalleFacturaPendiente(id) {
  const p = state.facturasPendientes.find(x => x.id === id);
  if (!p) return;
  pfdPendienteActivo = p;
  pfdFacturaIdReal = null;

  document.getElementById("pfd-titulo").textContent = `Factura ${p.numero || "(sin folio)"}`;
  document.getElementById("pfd-confirmar-eliminar").style.display = "none";
  document.getElementById("pfd-btn-eliminar-confirmar").style.display = "none";
  document.getElementById("pfd-btn-eliminar").style.display = "none";
  document.getElementById("pfd-btn-editar").style.display = "none";
  document.getElementById("pfd-condicion-pago").style.display = "none";
  document.getElementById("pfd-btn-condicion-pago").style.display = "none";
  document.getElementById("pfd-acciones-principales").style.display = "";

  const etiquetaCondicion = {
    contado: "Al contado",
    credito: `A crédito, ${p.plazo_dias_credito} días`,
    mixto: `Mixto — ${p.porcentaje_contado}% contado / ${100 - p.porcentaje_contado}% a crédito (${p.plazo_dias_credito} días)`,
  };

  let lineasParaMostrar = p.lineas || [];
  let gastosGeneralesParaMostrar = (p.lineas || []).filter(l => l.es_gasto_general);
  lineasParaMostrar = lineasParaMostrar.filter(l => !l.es_gasto_general);
  let facturaReal = null;
  if (p.estado === "aprobada" && p.factura_id) {
    try {
      facturaReal = await api(`/api/facturas/${p.factura_id}`);
      lineasParaMostrar = facturaReal.lineas;
      gastosGeneralesParaMostrar = facturaReal.gastos_generales || [];
      pfdFacturaIdReal = p.factura_id;
    } catch (e) {
      // Si no se pudo traer la factura real (ej. se eliminó desde otro
      // lado), se sigue mostrando lo que había quedado guardado en el
      // pendiente, solo que sin poder ofrecer "Eliminar".
    }
  }

  // Neto/IVA/Total: mientras está pendiente se usa el cálculo en vivo del
  // pendiente (p.monto_neto/monto_iva/monto_total, ver
  // _factura_pendiente_recepcion_dict); una vez aprobada, se usa lo que
  // efectivamente quedó guardado en la factura real (facturaReal.*), que es
  // lo que también generó su Cuenta por Pagar (ver _registrar_factura).
  const montoNeto = facturaReal ? facturaReal.monto_neto : p.monto_neto;
  const montoIva = facturaReal ? facturaReal.monto_iva : p.monto_iva;
  const montoTotalConIva = facturaReal ? facturaReal.monto_total : p.monto_total;
  const exentoIva = facturaReal ? facturaReal.exento_iva : p.exento_iva;

  document.getElementById("pfd-info").innerHTML = `
    <p><strong>Proveedor:</strong> ${escapeHtml(p.proveedor_nombre) || "—"}</p>
    <p><strong>Fecha:</strong> ${escapeHtml(p.fecha) || "—"}</p>
    <p><strong>Condición de pago:</strong> ${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}</p>
    <p><strong>Estado:</strong> ${ESTADO_PFR_LABEL[p.estado] || p.estado}</p>
    <p><strong>Neto:</strong> ${fmtMoney(montoNeto)} — <strong>IVA${exentoIva ? " (exenta)" : " (19%)"}:</strong> ${fmtMoney(montoIva)} — <strong>Total:</strong> ${fmtMoney(montoTotalConIva)}</p>
    <p><strong>Origen:</strong> ${ORIGEN_PFR_LABEL[p.origen] || p.origen || "—"}</p>
    <p><strong>Registrada por:</strong> ${escapeHtml(p.creado_por) || "—"}</p>
    ${p.estado === "rechazada" && p.nota_rechazo ? `<p><strong>Motivo de rechazo:</strong> ${escapeHtml(p.nota_rechazo)}</p>` : ""}
    ${(p.historial && p.historial.length) ? `
      <h3 class="subtitulo">Historial</h3>
      <ul class="pfd-historial">
        ${p.historial.map(h => `<li>${escapeHtml(fmtFecha(h.fecha))} — ${EVENTO_HISTORIAL_PFR_LABEL[h.evento] || h.evento} (${escapeHtml(h.usuario) || "—"})${h.nota ? `: "${escapeHtml(h.nota)}"` : ""}</li>`).join("")}
      </ul>` : ""}
  `;

  document.getElementById("pfd-lineas").innerHTML = lineasParaMostrar.map(l => {
    const sinResolver = l.producto_id == null;
    // "Leído: ..." se muestra SIEMPRE que haya texto_original (viene de
    // una foto leída por IA -- WhatsApp o "Escanear foto"; las líneas
    // tipeadas a mano/Excel no traen este campo), matcheó producto o no.
    // Antes solo se mostraba cuando no matcheaba nada, así que un match
    // automático equivocado (ej. el código del producto se perdió al leer
    // la foto y matcheó por texto contra el producto incorrecto) pasaba
    // desapercibido -- ahora siempre queda a la vista para comparar contra
    // el producto asignado antes de aprobar.
    const leido = l.texto_original
      ? `<div class="pfd-linea-leido">Leído: "${escapeHtml(l.texto_original)}"${l.modelo_leido ? ` · modelo: "${escapeHtml(l.modelo_leido)}"` : ""}${l.codigo_leido ? ` · código: "${escapeHtml(l.codigo_leido)}"` : ""}</div>`
      : "";
    const nombreCelda = sinResolver
      ? `⚠️ Producto nuevo, falta crear${leido}`
      : `${escapeHtml(l.producto_nombre) || `Producto #${l.producto_id}`}${leido}`;
    return `
    <tr>
      <td>${nombreCelda}</td>
      <td class="num mono">${l.cantidad}</td>
      <td class="num mono">${fmtMoney(l.precio_unitario)}</td>
      <td>${facturaReal ? (l.aplicada ? "Ingresada a stock" : "No aplicada") : "—"}</td>
    </tr>`;
  }).join("") + gastosGeneralesParaMostrar.map(g => `
    <tr>
      <td>💸 ${escapeHtml(g.descripcion)} <span class="badge-origen">${escapeHtml(g.rubro_gav_label || g.rubro_gav || "")}</span></td>
      <td class="num mono">—</td>
      <td class="num mono">${fmtMoney(g.monto)}</td>
      <td>Gasto general — no va a inventario</td>
    </tr>`).join("");

  const hayLineasSinResolver = p.estado === "pendiente" && lineasParaMostrar.some(l => l.producto_id == null);
  if (hayLineasSinResolver) {
    document.getElementById("pfd-info").innerHTML += `
      <p class="ayuda-inline ayuda-inline--bloqueado">
        Esta factura tiene productos que todavía no existen en el catálogo — Bodega tiene que crearlos
        (botón "Editar" → "Crear este producto en Logística" en cada línea) antes de poder aprobarla.
      </p>`;
  }

  if (p.estado === "pendiente" || p.estado === "rechazada") {
    document.getElementById("pfd-btn-editar").textContent = p.estado === "rechazada" ? "Corregir y reingresar" : "Editar";
    document.getElementById("pfd-btn-editar").style.display = "";
  } else {
    document.getElementById("pfd-btn-editar").textContent = "Editar";
  }
  if (pfdFacturaIdReal) {
    document.getElementById("pfd-btn-eliminar").style.display = "";
  }
  // Cambiar condición de pago: solo tiene sentido sobre una factura YA
  // aplicada de verdad (con factura_id real, ver arriba) y solo para
  // Finanzas/admin (ver puedeEditarCondicionPago en cargarSesion) -- Bodega
  // puede ver esta pantalla (ruta directa de ingreso) pero no puede tocar
  // la condición pactada con el proveedor, solo Finanzas la termina de
  // confirmar.
  if (pfdFacturaIdReal && state.puedeEditarCondicionPago) {
    document.getElementById("pfd-btn-condicion-pago").style.display = "";
    // Prellenado con la condición actual (guardada en el pendiente, que
    // PUT /api/facturas/<id>/condicion-pago mantiene al día -- ver
    // cambiar_condicion_pago_factura en app.py).
    const radioCondicionActual = document.querySelector(`input[name="pfd-condicion"][value="${p.condicion_pago}"]`);
    if (radioCondicionActual) radioCondicionActual.checked = true;
    document.getElementById("pfd-porcentaje-contado").value = p.porcentaje_contado || 50;
    document.getElementById("pfd-plazo-dias").value = p.plazo_dias_credito || 30;
    document.getElementById("pfd-campos-mixto").style.display = p.condicion_pago === "mixto" ? "grid" : "none";
    document.getElementById("pfd-campos-credito").style.display = p.condicion_pago === "contado" ? "none" : "grid";
  }

  abrirModal("modal-factura-pendiente-detalle");
}

document.getElementById("pfd-btn-editar").addEventListener("click", () => {
  cerrarModal("modal-factura-pendiente-detalle");
  if (pfdPendienteActivo) abrirEdicionFacturaPendiente(pfdPendienteActivo.id);
});

document.getElementById("pfd-btn-eliminar").addEventListener("click", async () => {
  if (!pfdFacturaIdReal) return;
  try {
    await api(`/api/facturas/${pfdFacturaIdReal}`, { method: "DELETE" });
    toast("Factura eliminada");
    cerrarModal("modal-factura-pendiente-detalle");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    if (e.requiereForzar && e.data && e.data.lineas_a_revertir) {
      document.getElementById("pfd-eliminar-resumen").textContent = e.data.error;
      document.getElementById("pfd-eliminar-tbody").innerHTML = e.data.lineas_a_revertir.map(l => `
        <tr>
          <td>${escapeHtml(l.producto_nombre)}${l.codigo_interno ? ` (#${escapeHtml(l.codigo_interno)})` : ""}</td>
          <td class="num mono">${l.cantidad}</td>
        </tr>`).join("");
      document.getElementById("pfd-confirmar-eliminar").style.display = "block";
      document.getElementById("pfd-btn-eliminar").style.display = "none";
      document.getElementById("pfd-btn-eliminar-confirmar").style.display = "";
    } else {
      toast(e.message, true);
    }
  }
});

document.getElementById("pfd-btn-eliminar-confirmar").addEventListener("click", async () => {
  if (!pfdFacturaIdReal) return;
  try {
    await api(`/api/facturas/${pfdFacturaIdReal}?forzar=1`, { method: "DELETE" });
    toast("Factura eliminada y stock revertido");
    cerrarModal("modal-factura-pendiente-detalle");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Cambiar condición de pago de una factura ya aplicada (Finanzas/admin,
// ver PUT /api/facturas/<id>/condicion-pago) -- no toca líneas ni stock,
// solo reemplaza la(s) Cuenta(s) por Pagar por una(s) nueva(s) con la
// condición corregida.
// ---------------------------------------------------------------------------

document.getElementById("pfd-btn-condicion-pago").addEventListener("click", () => {
  document.getElementById("pfd-acciones-principales").style.display = "none";
  document.getElementById("pfd-condicion-pago").style.display = "block";
});

document.getElementById("pfd-cond-cancelar").addEventListener("click", () => {
  document.getElementById("pfd-condicion-pago").style.display = "none";
  document.getElementById("pfd-acciones-principales").style.display = "";
});

document.querySelectorAll('input[name="pfd-condicion"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const condicion = document.querySelector('input[name="pfd-condicion"]:checked').value;
    document.getElementById("pfd-campos-mixto").style.display = condicion === "mixto" ? "grid" : "none";
    document.getElementById("pfd-campos-credito").style.display = condicion === "contado" ? "none" : "grid";
  });
});

document.getElementById("pfd-cond-guardar").addEventListener("click", async () => {
  if (!pfdFacturaIdReal) return;
  const condicion = document.querySelector('input[name="pfd-condicion"]:checked').value;
  try {
    await api(`/api/facturas/${pfdFacturaIdReal}/condicion-pago`, {
      method: "PUT",
      body: JSON.stringify({
        condicion_pago: condicion,
        porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("pfd-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
        plazo_dias_credito: Number(document.getElementById("pfd-plazo-dias").value || 30),
      }),
    });
    toast("Condición de pago corregida");
    cerrarModal("modal-factura-pendiente-detalle");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    toast(e.message, true);
  }
});

// Neto/IVA/Total en vivo, mientras se arma la factura -- mismo criterio que
// ya usa la emisión de DTE de venta (ver actualizarTotalDte): el precio que
// se escribe por línea es el NETO (sin IVA, tal como aparece en la factura
// del proveedor), y de ahí se calculan IVA (19%) y el total. El checkbox
// "Exenta de IVA" (pfr-exento-iva) deja el IVA en $0 para las facturas de
// proveedores que no cargan IVA -- sin él, TODA factura de compra quedaría
// forzada a pagar 19% de más en su Cuenta por Pagar, lo que sería un error
// real para esos casos.
function actualizarTotalFacturaPendiente() {
  const filas = Array.from(document.querySelectorAll("#pfr-lineas .fac-linea-row"));
  const neto = filas.reduce((acc, fila) => {
    const esGG = fila.querySelector(".pfr-linea-modo-gg").checked;
    if (esGG) {
      const monto = parseFloat(fila.querySelector(".pfr-linea-monto-gg").value) || 0;
      return acc + monto;
    }
    const cantidad = parseFloat(fila.querySelector(".pfr-linea-cantidad").value) || 0;
    const precio = parseFloat(fila.querySelector(".pfr-linea-precio").value) || 0;
    return acc + cantidad * precio;
  }, 0);
  const exento = document.getElementById("pfr-exento-iva").checked;
  const iva = exento ? 0 : Math.round(neto * 0.19 * 100) / 100;
  const total = Math.round((neto + iva) * 100) / 100;
  document.getElementById("pfr-total-neto").textContent = fmtMoney(neto);
  document.getElementById("pfr-total-iva").textContent = fmtMoney(iva);
  document.getElementById("pfr-total-vivo").textContent = fmtMoney(total);
}
document.getElementById("pfr-exento-iva").addEventListener("change", actualizarTotalFacturaPendiente);

// ctx = { pendienteId, indice } -- solo se pasan cuando la línea pertenece
// a un pendiente YA GUARDADO (pantalla de "Editar"), porque el botón
// "Crear este producto" llama a un endpoint que necesita un pendiente_id +
// índice de línea reales. En "Registrar factura" (todavía no existe el
// pendiente) esos dos vienen null y el botón no se muestra -- ahí Finanzas
// solo puede DESCRIBIR el producto nuevo, no crearlo (ver hallazgo: crear
// productos es exclusivo de Bodega).
let pfrLineaContador = 0;

function filaLineaFacturaPendiente(productos, linea, ctx) {
  ctx = ctx || {};
  const div = document.createElement("div");
  div.className = "fac-linea-row";
  const sinResolverAlAbrir = !!(linea && linea.producto_id == null && (linea.texto_original || linea.cantidad != null));
  const puedeCrearProducto = ctx.pendienteId != null && ctx.indice != null &&
    state.sesion && (state.sesion.rol === "admin" || (state.sesion.modulos || []).includes("bodega"));
  const esGastoGeneralAlAbrir = !!(linea && linea.es_gasto_general);
  const grupoModo = `pfr-modo-${++pfrLineaContador}`;
  div.innerHTML = `
    <div class="pfr-linea-modo-campo" style="grid-column:1/-1;margin-bottom:6px">
      <label class="toggle-critico">
        <input type="radio" name="${grupoModo}" class="pfr-linea-modo-producto" value="producto" ${esGastoGeneralAlAbrir ? "" : "checked"}>
        Repuesto (va a inventario)
      </label>
      <label class="toggle-critico">
        <input type="radio" name="${grupoModo}" class="pfr-linea-modo-gg" value="gasto_general" ${esGastoGeneralAlAbrir ? "checked" : ""}>
        Gasto general — no va a inventario (ej. combustible, herramienta)
      </label>
    </div>
    <div class="pfr-modo-producto" style="display:${esGastoGeneralAlAbrir ? "none" : "contents"}">
      <div class="campo">
        <label>Producto</label>
        <select class="pfr-linea-producto-id">
          <option value="">Selecciona un producto…</option>
          <option value="__nuevo__">✏️ No está en el catálogo — describir para Bodega</option>
          ${productos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("")}
        </select>
        <input type="text" class="pfr-linea-nombre-nuevo" placeholder="¿Qué producto es? (ej. Kit de embrague ABC 2020)"
               style="display:none;margin-top:6px"
               value="${linea && linea.texto_original ? escapeHtml(linea.texto_original) : ""}">
        ${puedeCrearProducto ? `
          <button type="button" class="btn btn-secundario btn-chico pfr-linea-crear-producto" style="display:none;margin-top:6px">
            Crear este producto en Logística
          </button>
          <div class="pfr-linea-crear-form" style="display:none;margin-top:6px">
            <input type="text" class="pfr-crear-categoria" placeholder="Modelo (ej. JF010)"
                   value="${linea && linea.modelo_leido ? escapeHtml(linea.modelo_leido) : ""}" style="margin-bottom:4px">
            <input type="text" class="pfr-crear-modelo" placeholder="Detalle (ej. Pulley Set)" style="margin-bottom:4px">
            <input type="text" class="pfr-crear-marca" placeholder="Marca (ej. BOSCH — obligatoria; si no tiene, escribe GENÉRICA)"
                   value="${document.getElementById("pfr-marca-todas") ? escapeHtml(document.getElementById("pfr-marca-todas").value.trim()) : ""}" style="margin-bottom:4px">
            <p class="ayuda-inline pfr-crear-marca-hint" style="display:none;margin:0 0 6px"></p>
            <input type="text" class="pfr-crear-codigo-interno" placeholder="Código interno / N° de parte del fabricante (ej. 33710-NKC-7-FX)"
                   value="${linea && linea.codigo_leido ? escapeHtml(linea.codigo_leido) : ""}" style="margin-bottom:4px">
            <button type="button" class="btn btn-primario btn-chico pfr-crear-confirmar">Crear y vincular</button>
          </div>
        ` : ""}
      </div>
      <div class="campo">
        <label>Cantidad</label>
        <input type="number" class="pfr-linea-cantidad" min="1" value="${linea && linea.cantidad != null ? linea.cantidad : 1}">
      </div>
      <div class="campo">
        <label>Precio unit. ($ CLP)</label>
        <input type="number" class="pfr-linea-precio" min="0" step="1" value="${linea && linea.precio_unitario != null ? linea.precio_unitario : 0}">
      </div>
    </div>
    <div class="pfr-modo-gasto-general" style="display:${esGastoGeneralAlAbrir ? "contents" : "none"}">
      <div class="campo">
        <label>Descripción</label>
        <input type="text" class="pfr-linea-descripcion-gg" placeholder="Ej: Bencina para el generador"
               value="${linea && linea.descripcion ? escapeHtml(linea.descripcion) : ""}">
      </div>
      <div class="campo">
        <label>Rubro</label>
        <select class="pfr-linea-rubro-gg">
          <option value="">Selecciona…</option>
          ${optionsRubroGavFactura(linea ? linea.rubro_gav : "")}
        </select>
      </div>
      <div class="campo">
        <label>Monto ($ CLP)</label>
        <input type="number" class="pfr-linea-monto-gg" min="0" step="1" value="${linea && linea.monto != null ? linea.monto : 0}">
      </div>
    </div>
    <button type="button" class="fac-linea-quitar">Quitar</button>
  `;

  const select = div.querySelector(".pfr-linea-producto-id");
  const inputNombreNuevo = div.querySelector(".pfr-linea-nombre-nuevo");
  const btnCrear = div.querySelector(".pfr-linea-crear-producto");
  const formCrear = div.querySelector(".pfr-linea-crear-form");
  const radioModoProducto = div.querySelector(".pfr-linea-modo-producto");
  const radioModoGG = div.querySelector(".pfr-linea-modo-gg");
  const bloqueProducto = div.querySelector(".pfr-modo-producto");
  const bloqueGG = div.querySelector(".pfr-modo-gasto-general");

  function sincronizarVisibilidad() {
    const esNuevo = select.value === "__nuevo__";
    inputNombreNuevo.style.display = esNuevo ? "block" : "none";
    if (btnCrear) btnCrear.style.display = esNuevo ? "inline-block" : "none";
    if (formCrear && !esNuevo) formCrear.style.display = "none";
  }

  function sincronizarModo() {
    const esGG = radioModoGG.checked;
    bloqueProducto.style.display = esGG ? "none" : "contents";
    bloqueGG.style.display = esGG ? "contents" : "none";
    actualizarTotalFacturaPendiente();
  }

  if (linea && linea.producto_id) {
    select.value = linea.producto_id;
  } else if (sinResolverAlAbrir) {
    select.value = "__nuevo__";
  }
  sincronizarVisibilidad();

  select.addEventListener("change", sincronizarVisibilidad);
  radioModoProducto.addEventListener("change", sincronizarModo);
  radioModoGG.addEventListener("change", sincronizarModo);
  div.querySelector(".fac-linea-quitar").addEventListener("click", () => {
    div.remove();
    actualizarTotalFacturaPendiente();
  });
  div.querySelector(".pfr-linea-cantidad").addEventListener("input", actualizarTotalFacturaPendiente);
  div.querySelector(".pfr-linea-precio").addEventListener("input", actualizarTotalFacturaPendiente);
  div.querySelector(".pfr-linea-monto-gg").addEventListener("input", actualizarTotalFacturaPendiente);

  if (btnCrear) {
    // Aviso en vivo: si el Modelo+Detalle que se está escribiendo ya
    // existe en el catálogo con otra(s) marca(s), se muestra de inmediato
    // -- ayuda a decidir si es una marca nueva de verdad o si conviene
    // avisarle a quien carga la factura que revise antes de duplicar por
    // un typo (ej. "BOSCH " con espacio de más).
    const inputCategoria = div.querySelector(".pfr-crear-categoria");
    const inputModelo = div.querySelector(".pfr-crear-modelo");
    const hintMarca = div.querySelector(".pfr-crear-marca-hint");
    function actualizarHintVariantes() {
      if (!hintMarca) return;
      const categoria = inputCategoria.value.trim().toLowerCase();
      const modelo = inputModelo.value.trim().toLowerCase();
      if (!categoria && !modelo) {
        hintMarca.style.display = "none";
        return;
      }
      const variantes = productos.filter(p =>
        (p.categoria || "").trim().toLowerCase() === categoria &&
        (p.modelo || "").trim().toLowerCase() === modelo
      );
      if (!variantes.length) {
        hintMarca.style.display = "none";
        return;
      }
      const detalle = variantes
        .map(p => `${p.marca ? escapeHtml(p.marca) : "sin marca (repuesto antiguo)"} — ${fmtMoney(p.costo_compra)}`)
        .join(" · ");
      hintMarca.innerHTML = `Ya existe este modelo/detalle en: ${detalle}. Si es la misma marca, no crees uno nuevo -- selecciónalo arriba de la lista de productos.`;
      hintMarca.style.display = "block";
    }
    inputCategoria.addEventListener("input", actualizarHintVariantes);
    inputModelo.addEventListener("input", actualizarHintVariantes);

    btnCrear.addEventListener("click", () => {
      formCrear.style.display = formCrear.style.display === "none" ? "block" : "none";
      // Ayuda: si Finanzas ya tipeó (o la IA leyó) una descripción libre y
      // el campo Detalle todavía está vacío, se usa como punto de partida
      // -- Bodega igual puede corregirla. Independiente de si "Modelo" ya
      // viene prellenado con lo que leyó la IA (ver modelo_leido más
      // arriba) o no.
      if (formCrear.style.display === "block" && !div.querySelector(".pfr-crear-modelo").value) {
        div.querySelector(".pfr-crear-modelo").value = inputNombreNuevo.value || "";
      }
      if (formCrear.style.display === "block") actualizarHintVariantes();
    });
    div.querySelector(".pfr-crear-confirmar").addEventListener("click", async () => {
      const codigoInterno = div.querySelector(".pfr-crear-codigo-interno").value.trim();
      const categoria = div.querySelector(".pfr-crear-categoria").value.trim();
      const modelo = div.querySelector(".pfr-crear-modelo").value.trim();
      const marca = div.querySelector(".pfr-crear-marca").value.trim();
      if (!categoria && !modelo) {
        toast("Ingresa al menos modelo o detalle para crear el producto", true);
        return;
      }
      // Mismo criterio que valida el backend (ver
      // crear_y_vincular_producto_pendiente -> _crear_producto_desde_datos
      // en app.py): con modelo/detalle, la marca ya no es opcional -- este
      // aviso evita el viaje al servidor solo para enterarse.
      if (!marca) {
        toast('Escribe una marca para este repuesto (ej. BOSCH, o "GENÉRICA" si no tiene una marca real).', true);
        return;
      }
      try {
        const data = await api(
          `/api/facturas/pendientes-recepcion/${ctx.pendienteId}/lineas/${ctx.indice}/crear-producto`,
          { method: "POST", body: JSON.stringify({ codigo_interno: codigoInterno, categoria, modelo, marca }) },
        );
        toast(`Producto "${data.producto.nombre}" creado y vinculado a esta línea`);
        pfrProductosCache = [...pfrProductosCache, data.producto];
        // Re-renderiza la línea ya resuelta, sin perder cantidad/precio que
        // el usuario haya alcanzado a corregir en esta misma edición.
        const cantidadActual = div.querySelector(".pfr-linea-cantidad").value;
        const precioActual = div.querySelector(".pfr-linea-precio").value;
        const nuevaFila = filaLineaFacturaPendiente(
          pfrProductosCache,
          { producto_id: data.producto.id, cantidad: cantidadActual, precio_unitario: precioActual },
          ctx,
        );
        div.replaceWith(nuevaFila);
        actualizarTotalFacturaPendiente();
      } catch (err) {
        toast(err.message, true);
      }
    });
  }

  return div;
}

let pfrProductosCache = [];
let pfrProveedoresCache = [];

async function pfrCargarCatalogos() {
  const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
  pfrProductosCache = dataProductos.productos;
  pfrProveedoresCache = dataProveedores.proveedores;
  const select = document.getElementById("pfr-proveedor-id");
  select.innerHTML = '<option value="">Sin proveedor</option>' +
    pfrProveedoresCache.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
}

document.getElementById("btn-nueva-factura-pendiente").addEventListener("click", async () => {
  document.getElementById("form-factura-pendiente").reset();
  document.getElementById("pfr-id").value = "";
  document.getElementById("pfr-titulo").textContent = "Registrar factura de compra";
  document.getElementById("pfr-btn-guardar").textContent = "Registrar factura";
  document.getElementById("pfr-lineas").innerHTML = "";
  document.getElementById("pfr-campos-mixto").style.display = "none";
  document.getElementById("pfr-campos-credito").style.display = "block";
  document.getElementById("pfr-seccion-condicion").style.display = "block";
  document.getElementById("pfr-aviso-condicion-fija").style.display = "none";
  document.getElementById("pfr-aviso-origen").style.display = "none";
  document.getElementById("pfr-aviso-rechazo").style.display = "none";
  document.getElementById("pfr-foto-whatsapp").style.display = "none";
  document.getElementById("pfr-producto-resultados").style.display = "none";
  try {
    await pfrCargarCatalogos();
  } catch (e) {
    toast(e.message, true);
    return;
  }
  document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache));
  actualizarTotalFacturaPendiente();
  abrirModal("modal-factura-pendiente");
});

// ---- Editar un pendiente antes de aprobar/rechazar (clave para completar
// folio/productos de los que llegaron por WhatsApp con datos incompletos) ----

async function abrirEdicionFacturaPendiente(id) {
  try {
    const [p] = await Promise.all([api(`/api/facturas/pendientes-recepcion/${id}`), pfrCargarCatalogos()]);
    document.getElementById("form-factura-pendiente").reset();
    document.getElementById("pfr-id").value = p.id;
    document.getElementById("pfr-numero").value = p.numero || "";
    document.getElementById("pfr-proveedor-id").value = p.proveedor_id || "";
    document.getElementById("pfr-fecha").value = (p.fecha || "").slice(0, 10);
    document.getElementById("pfr-exento-iva").checked = !!p.exento_iva;

    const avisoRechazo = document.getElementById("pfr-aviso-rechazo");
    if (p.estado === "rechazada") {
      document.getElementById("pfr-titulo").textContent = `Corregir y reingresar factura #${p.id}`;
      document.getElementById("pfr-btn-guardar").textContent = "Corregir y reingresar";
      avisoRechazo.textContent = `Bodega rechazó esta factura${p.nota_rechazo ? `: "${p.nota_rechazo}"` : ""}. Corrige lo que corresponda y guarda -- vuelve sola a la cola de revisión, sin perder el registro de que fue rechazada.`;
      avisoRechazo.style.display = "block";
    } else {
      document.getElementById("pfr-titulo").textContent = `Editar factura pendiente #${p.id}`;
      document.getElementById("pfr-btn-guardar").textContent = "Guardar cambios";
      avisoRechazo.style.display = "none";
    }

    const etiquetaCondicion = { contado: "Al contado", credito: `A crédito, ${p.plazo_dias_credito} días`, mixto: `Mixto — ${p.porcentaje_contado}% contado` };
    document.getElementById("pfr-seccion-condicion").style.display = "none";
    const avisoCondicion = document.getElementById("pfr-aviso-condicion-fija");
    avisoCondicion.textContent = `Condición de pago ya pactada: ${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}. La condición de pago ya se fijó al registrar esta factura y no se puede editar acá — solo el número, proveedor, fecha y productos.`;
    avisoCondicion.style.display = "block";

    const avisoOrigen = document.getElementById("pfr-aviso-origen");
    const fotoImg = document.getElementById("pfr-foto-whatsapp");
    if (p.origen === "whatsapp") {
      avisoOrigen.textContent = "Esta factura llegó por WhatsApp — revisa que el folio y cada producto queden bien identificados antes de aprobarla.";
      avisoOrigen.style.display = "block";
      fotoImg.src = `/api/facturas/pendientes-recepcion/${p.id}/foto`;
      fotoImg.style.display = "block";
    } else {
      avisoOrigen.style.display = "none";
      fotoImg.style.display = "none";
    }

    const cont = document.getElementById("pfr-lineas");
    cont.innerHTML = "";
    (p.lineas && p.lineas.length ? p.lineas : [{}]).forEach((linea, idx) => {
      // ctx con pendienteId+indice reales (idx calza 1:1 con lineas_json en
      // el backend, ver _factura_pendiente_recepcion_dict) -- habilita el
      // botón "Crear este producto" para líneas sin resolver.
      cont.appendChild(filaLineaFacturaPendiente(pfrProductosCache, linea, { pendienteId: p.id, indice: idx }));
    });
    actualizarTotalFacturaPendiente();
    document.getElementById("pfr-producto-resultados").style.display = "none";
    abrirModal("modal-factura-pendiente");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("pfr-agregar-linea").addEventListener("click", () => {
  document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache));
  actualizarTotalFacturaPendiente();
});

// "Aplicar a todos los productos nuevos": copia la marca escrita arriba a
// cada campo de marca de los formularios "Crear este producto" que ya estén
// en pantalla en este momento -- pensado para el caso típico de una factura
// que trae varios repuestos nuevos, todos de la misma marca (ej. china o
// americana), para no tener que repetir la marca línea por línea. Los
// formularios de líneas que se agreguen DESPUÉS de aplicar ya nacen con el
// valor actual del campo de marca (ver filaLineaFacturaPendiente más
// arriba), así que en general basta con escribir la marca una vez al
// empezar a cargar la factura.
document.getElementById("pfr-btn-aplicar-marca-todas").addEventListener("click", () => {
  const marca = document.getElementById("pfr-marca-todas").value.trim();
  const campos = document.querySelectorAll("#pfr-lineas .pfr-crear-marca");
  campos.forEach(input => { input.value = marca; });
  toast(marca
    ? `Marca "${marca}" aplicada a ${campos.length} producto(s) nuevo(s) en pantalla`
    : "Se limpió la marca de los productos nuevos en pantalla");
});

// ---------------------------------------------------------------------------
// Buscador de producto para agregar líneas a una factura de compra: al
// hacer clic en un resultado se agrega la línea al tiro (cantidad 1, precio
// sugerido = costo de compra del producto), SIN cerrar la lista de
// resultados — así se puede seguir escribiendo/clickeando otros productos
// sin perder el foco ni tener que reabrir nada (mismo patrón que
// renderResultadosOpoi en operativo.js, para "Carga de repuestos" en una OT).
// ---------------------------------------------------------------------------

function renderResultadosPfr(texto) {
  const resultados = document.getElementById("pfr-producto-resultados");
  const q = texto.trim();
  if (!q) {
    resultados.style.display = "none";
    return;
  }
  const coincidencias = pfrProductosCache.filter(p =>
    coincideBusqueda(q, p.nombre, p.descripcion, p.codigo_interno, p.id_sistema)
  ).slice(0, 8);
  if (coincidencias.length === 0) {
    resultados.innerHTML = '<div class="autocomplete-sin-resultados">Sin productos que coincidan</div>';
    resultados.style.display = "block";
    return;
  }
  resultados.innerHTML = coincidencias.map(p => `
    <div class="autocomplete-item" data-id="${p.id}">
      #${escapeHtml(p.id_sistema)} — ${escapeHtml(p.nombre)}
      <span class="autocomplete-item-meta">Stock: ${p.stock_disponible}</span>
    </div>
  `).join("");
  resultados.style.display = "block";
  resultados.querySelectorAll(".autocomplete-item").forEach(item => {
    // mousedown (no click) para que se dispare antes del blur del input,
    // que si no cerraría la lista antes de alcanzar a procesar el click.
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const p = pfrProductosCache.find(x => x.id === parseInt(item.dataset.id, 10));
      if (p) agregarLineaFacturaDesdeAutocomplete(p);
    });
  });
}

function agregarLineaFacturaDesdeAutocomplete(producto) {
  // Si el producto ya tiene una línea en la factura, en vez de duplicarla
  // se le suma 1 a la cantidad existente (mismo criterio de "no permitir
  // producto repetido en la misma factura" que ya validaba el registro
  // manual — ver _validar_lineas_pendiente en app.py).
  const filaExistente = Array.from(document.querySelectorAll("#pfr-lineas .fac-linea-row"))
    .find(fila => parseInt(fila.querySelector(".pfr-linea-producto-id").value, 10) === producto.id);
  if (filaExistente) {
    const inputCantidad = filaExistente.querySelector(".pfr-linea-cantidad");
    inputCantidad.value = (parseFloat(inputCantidad.value) || 0) + 1;
  } else {
    document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache, {
      producto_id: producto.id,
      cantidad: 1,
      precio_unitario: producto.costo_compra || 0,
    }));
  }
  actualizarTotalFacturaPendiente();
  toast(`Agregado: ${producto.nombre}`);
}

document.getElementById("pfr-buscar-producto").addEventListener("input", (e) => {
  renderResultadosPfr(e.target.value);
});
document.getElementById("pfr-buscar-producto").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosPfr(e.target.value);
});
document.getElementById("pfr-buscar-producto").addEventListener("blur", () => {
  // Pequeño delay para que el mousedown del resultado alcance a dispararse
  // antes de que el blur oculte la lista.
  setTimeout(() => { document.getElementById("pfr-producto-resultados").style.display = "none"; }, 150);
});

document.querySelectorAll('input[name="pfr-condicion"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const condicion = document.querySelector('input[name="pfr-condicion"]:checked').value;
    document.getElementById("pfr-campos-mixto").style.display = condicion === "mixto" ? "grid" : "none";
    document.getElementById("pfr-campos-credito").style.display = condicion === "contado" ? "none" : "grid";
  });
});

document.querySelectorAll(".pfr-plazos-rapidos [data-plazo]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.getElementById("pfr-plazo-dias").value = btn.dataset.plazo;
  });
});

document.getElementById("form-factura-pendiente").addEventListener("submit", async (e) => {
  e.preventDefault();
  const filas = Array.from(document.querySelectorAll("#pfr-lineas .fac-linea-row"));
  const lineas = [];
  for (const fila of filas) {
    if (fila.querySelector(".pfr-linea-modo-gg").checked) {
      // Gasto general (combustible, herramienta, etc.) -- no pasa por el
      // matching de producto, va con descripción libre + rubro + un solo
      // monto (ver es_gasto_general en _validar_lineas_pendiente).
      const descripcion = fila.querySelector(".pfr-linea-descripcion-gg").value.trim();
      const rubro_gav = fila.querySelector(".pfr-linea-rubro-gg").value;
      const monto = fila.querySelector(".pfr-linea-monto-gg").value;
      if (!descripcion) {
        toast("Describe cada línea marcada como \"Gasto general\" (ej. \"Bencina para el generador\")", true);
        return;
      }
      if (!rubro_gav) {
        toast("Elige un rubro para cada línea marcada como \"Gasto general\"", true);
        return;
      }
      lineas.push({ es_gasto_general: true, descripcion, rubro_gav, monto });
      continue;
    }
    const valorSelect = fila.querySelector(".pfr-linea-producto-id").value;
    const cantidad = fila.querySelector(".pfr-linea-cantidad").value;
    const precio_unitario = fila.querySelector(".pfr-linea-precio").value;
    if (valorSelect === "__nuevo__") {
      // Producto que todavía no existe en el catálogo: Finanzas solo lo
      // DESCRIBE (no lo crea, eso es exclusivo de Bodega -- ver hallazgo
      // #3). La línea queda sin producto_id y la factura igual se guarda
      // como pendiente; Bodega la completa después desde "Editar" (botón
      // "Crear este producto en Logística").
      const nombreNuevo = fila.querySelector(".pfr-linea-nombre-nuevo").value.trim();
      if (!nombreNuevo) {
        toast("Describe el producto en cada línea marcada como \"No está en el catálogo\" (o quítala)", true);
        return;
      }
      lineas.push({ producto_nuevo_nombre: nombreNuevo, cantidad, precio_unitario });
    } else if (!valorSelect) {
      toast("Selecciona un producto en cada línea (o quítala)", true);
      return;
    } else {
      lineas.push({ producto_id: valorSelect, cantidad, precio_unitario });
    }
  }

  const id = document.getElementById("pfr-id").value;
  const eraCorreccionDeRechazo = document.getElementById("pfr-aviso-rechazo").style.display !== "none";

  // Ver comentario en form-op-orden-nueva (operativo.js): sin bloquear el
  // botón, un doble clic al registrar una factura de compra nueva la
  // duplicaba (dos facturas, y si venían con productos ya en catálogo, dos
  // aplicaciones de stock/CxP por la misma compra real) (hallazgo #4).
  const btnGuardarFactura = e.target.querySelector('button[type="submit"]');
  btnGuardarFactura.disabled = true;
  try {
    if (id) {
      await api(`/api/facturas/pendientes-recepcion/${id}`, {
        method: "PUT",
        body: JSON.stringify({
          numero: document.getElementById("pfr-numero").value,
          proveedor_id: document.getElementById("pfr-proveedor-id").value || null,
          fecha: document.getElementById("pfr-fecha").value,
          lineas,
          exento_iva: document.getElementById("pfr-exento-iva").checked,
        }),
      });
      toast(eraCorreccionDeRechazo ? "Factura corregida y reingresada a la cola de revisión de Bodega" : "Factura pendiente actualizada");
      // Si se corrigió una rechazada, el aviso rojo ⚠️ (afr-flotante, ver
      // alerta_facturas_pendientes.js) debe apagarse de inmediato en vez de
      // esperar hasta 25s al próximo sondeo automático -- typeof por si esta
      // página no cargó ese script.
      if (eraCorreccionDeRechazo && typeof afrSondear === "function") afrSondear();
    } else {
      const condicion = document.querySelector('input[name="pfr-condicion"]:checked').value;
      const creada = await api("/api/facturas/pendientes-recepcion", {
        method: "POST",
        body: JSON.stringify({
          numero: document.getElementById("pfr-numero").value,
          proveedor_id: document.getElementById("pfr-proveedor-id").value || null,
          fecha: document.getElementById("pfr-fecha").value,
          lineas,
          condicion_pago: condicion,
          porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("pfr-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
          plazo_dias_credito: Number(document.getElementById("pfr-plazo-dias").value || 30),
          exento_iva: document.getElementById("pfr-exento-iva").checked,
        }),
      });
      // Con todos los productos ya en el catálogo, la factura queda
      // aplicada de una (ver _crear_pendiente_y_aplicar_si_posible en
      // app.py) -- solo sigue "pendiente" si le falta crear algún
      // producto nuevo, que es cuando de verdad hace falta el paso extra
      // de aprobar a mano.
      toast(creada.estado === "aprobada"
        ? "Factura aplicada: stock actualizado y Cuenta por Pagar generada"
        : "Factura registrada — queda pendiente de recepción (falta vincular un producto nuevo, ver el aviso 📦)");
    }
    cerrarModal("modal-factura-pendiente");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btnGuardarFactura.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Carga masiva de facturas de compra (Excel o foto escaneada) — deja cada
// factura válida como pendiente de recepción, con la misma condición de
// pago elegida para todo el lote (ver POST /api/facturas/carga-masiva/*
// en app.py). Reutiliza el mismo token de previsualización para Excel y
// para foto: la única diferencia es qué endpoint arma la previsualización.
// ---------------------------------------------------------------------------

function cmfMostrarPaso(paso) {
  document.getElementById("cmf-paso-subir").style.display = paso === "subir" ? "block" : "none";
  document.getElementById("cmf-paso-preview").style.display = paso === "preview" ? "block" : "none";
}

async function cmfCargarCatalogos() {
  if (state.cmfProductos.length && state.cmfProveedores.length) return;
  const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
  state.cmfProductos = dataProductos.productos;
  state.cmfProveedores = dataProveedores.proveedores;
}

function cmfAbrirPreview(data) {
  state.cmfToken = data.token;
  state.cmfFacturas = data.facturas;
  cmfMostrarPaso("preview");
  cmfRenderPreview(data.resumen);
}

function cmfRenderPreview(resumen) {
  document.getElementById("cmf-resumen").textContent =
    `${resumen.total_facturas} factura(s) leída(s) — ${resumen.validas} lista(s) para confirmar, ${resumen.con_errores} con errores por corregir.`;

  document.getElementById("cmf-lista-facturas").innerHTML = state.cmfFacturas.map(f => {
    const proveedorOpciones = '<option value="">— Sin proveedor —</option>' +
      state.cmfProveedores.map(p => `<option value="${p.id}" ${p.id === f.proveedor_id ? "selected" : ""}>${escapeHtml(p.nombre)}</option>`).join("");
    const erroresGenerales = (f.errores || []).filter(err => !(f.lineas || []).some(l => (l.errores || []).includes(err)));
    return `
      <div class="dash-panel" style="margin-bottom:16px" data-cmf-factura="${escapeHtml(f.numero)}">
        <h3 class="dash-titulo">${escapeHtml(f.numero)} ${f.valida ? "✅" : "⚠️"}</h3>
        ${erroresGenerales.length ? `<p class="ayuda-inline ayuda-inline--bloqueado">${erroresGenerales.map(escapeHtml).join(" · ")}</p>` : ""}
        <div class="campo">
          <label>Proveedor${f.proveedor_nuevo ? " (nuevo — se creará al confirmar)" : ""}</label>
          <select class="cmf-factura-proveedor-id" data-cmf-factura="${escapeHtml(f.numero)}">${proveedorOpciones}</select>
        </div>
        <div class="tabla-wrap">
          <table class="tabla">
            <thead><tr><th>Producto</th><th class="num">Cantidad</th><th class="num">Precio unit.</th><th></th></tr></thead>
            <tbody>
              ${(f.lineas || []).map(l => `
                <tr data-cmf-linea="${escapeHtml(f.numero)}|${l.fila}">
                  <td>
                    <select class="cmf-linea-producto-id">
                      <option value="">Selecciona un producto…</option>
                      ${state.cmfProductos.map(p => `<option value="${p.id}" ${p.id === l.producto_id ? "selected" : ""}>#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("")}
                    </select>
                    ${l.texto_original ? `<p class="ayuda-inline">Leído: "${escapeHtml(l.texto_original)}"${l.modelo_leido ? ` · modelo: "${escapeHtml(l.modelo_leido)}"` : ""}${l.codigo_leido ? ` · código: "${escapeHtml(l.codigo_leido)}"` : ""}</p>` : ""}
                  </td>
                  <td class="num"><input type="number" class="cmf-linea-cantidad" min="1" value="${l.cantidad ?? ""}" style="width:70px"></td>
                  <td class="num"><input type="number" class="cmf-linea-precio" min="0" step="1" value="${l.precio_unitario ?? ""}" style="width:90px"></td>
                  <td>
                    ${(l.errores || []).length ? `<p class="ayuda-inline ayuda-inline--bloqueado">${l.errores.map(escapeHtml).join(" · ")}</p>` : ""}
                    <button type="button" class="btn btn-secundario btn-chico cmf-linea-guardar">Guardar corrección</button>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }).join("");

  // La corrección de una línea se manda al servidor SOLA (sin esperar a
  // que la persona aprete "Guardar corrección" a propósito): antes esto
  // dependía por completo de ese botón chico, y era fácil elegir otro
  // producto en el desplegable, dar por hecho que ya quedó guardado, y
  // apretar directo "Confirmar carga" -- la corrección se perdía en
  // silencio y lo que le llegaba a Bodega era la factura tal como se
  // había leído originalmente. "change" alcanza (se dispara al elegir del
  // desplegable, o al salir de un campo de texto/número después de
  // editarlo), no hace falta guardar en cada tecla.
  document.querySelectorAll("#cmf-lista-facturas .cmf-linea-producto-id, #cmf-lista-facturas .cmf-linea-cantidad, #cmf-lista-facturas .cmf-linea-precio").forEach(el => {
    el.addEventListener("change", () => cmfGuardarCorreccionLinea(el.closest("tr[data-cmf-linea]")));
  });
  // El botón "Guardar corrección" se deja igual (por si alguien prefiere
  // guardar explícitamente, o para repetir el guardado tras un error) --
  // ya no es la ÚNICA forma de que la corrección se guarde.
  document.querySelectorAll("#cmf-lista-facturas .cmf-linea-guardar").forEach(btn => {
    btn.addEventListener("click", () => cmfGuardarCorreccionLinea(btn.closest("tr[data-cmf-linea]")));
  });
  document.querySelectorAll("#cmf-lista-facturas .cmf-factura-proveedor-id").forEach(select => {
    select.addEventListener("change", () => cmfGuardarCorreccionProveedor(select));
  });
}

// Manda al servidor la corrección de UNA línea con los valores que hay
// AHORA MISMO en su fila (sin toast ni repintar la tabla completa) --
// separado de cmfGuardarCorreccionLinea para poder reusarlo también desde
// "Confirmar carga" (ver más abajo), que hace un último barrido de todas
// las filas visibles antes de confirmar, como red de seguridad extra por
// si algún cambio no alcanzó a dispararse por su cuenta (ej. autocompletado
// del navegador).
async function cmfEnviarCorreccionLinea(tr) {
  const [numeroFactura, filaStr] = tr.dataset.cmfLinea.split("|");
  const productoId = tr.querySelector(".cmf-linea-producto-id").value;
  const cantidad = tr.querySelector(".cmf-linea-cantidad").value;
  const precio = tr.querySelector(".cmf-linea-precio").value;
  const data = await api("/api/facturas/carga-masiva/corregir-linea", {
    method: "POST",
    body: JSON.stringify({
      token: state.cmfToken,
      numero_factura: numeroFactura,
      fila: parseInt(filaStr, 10),
      producto_id: productoId || null,
      cantidad: cantidad || null,
      precio_unitario: precio || null,
    }),
  });
  const idx = state.cmfFacturas.findIndex(f => f.numero === numeroFactura);
  if (idx >= 0) state.cmfFacturas[idx] = data.factura;
  return data;
}

async function cmfGuardarCorreccionLinea(tr) {
  try {
    const data = await cmfEnviarCorreccionLinea(tr);
    cmfRenderPreview(data.resumen);
    toast("Corrección guardada");
  } catch (e) {
    toast(e.message, true);
  }
}

async function cmfGuardarCorreccionProveedor(select) {
  const numeroFactura = select.dataset.cmfFactura;
  try {
    const data = await api("/api/facturas/carga-masiva/corregir-proveedor", {
      method: "POST",
      body: JSON.stringify({ token: state.cmfToken, numero_factura: numeroFactura, proveedor_id: select.value || null }),
    });
    const idx = state.cmfFacturas.findIndex(f => f.numero === numeroFactura);
    if (idx >= 0) state.cmfFacturas[idx] = data.factura;
    cmfRenderPreview(data.resumen);
    toast("Proveedor corregido");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-cxp-carga-masiva").addEventListener("click", async () => {
  document.getElementById("cmf-archivo").value = "";
  document.getElementById("cmf-titulo").textContent = "Carga masiva de facturas (Excel)";
  state.cmfToken = null;
  state.cmfFacturas = [];
  cmfMostrarPaso("subir");
  abrirModal("modal-cxp-carga-masiva");
});

document.getElementById("btn-cmf-volver").addEventListener("click", () => {
  document.getElementById("cmf-archivo").value = "";
  document.getElementById("cmf-titulo").textContent = "Carga masiva de facturas (Excel)";
  cmfMostrarPaso("subir");
});

document.getElementById("btn-cmf-previsualizar").addEventListener("click", async () => {
  const archivo = document.getElementById("cmf-archivo").files[0];
  if (!archivo) {
    toast("Selecciona un archivo Excel primero", true);
    return;
  }
  const formData = new FormData();
  formData.append("archivo", archivo);
  try {
    await cmfCargarCatalogos();
    const res = await fetch("/api/facturas/carga-masiva/previsualizar", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo previsualizar el archivo");
    cmfAbrirPreview(data);
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-cxp-escanear-foto").addEventListener("click", () => {
  document.getElementById("input-cxp-escanear-foto").click();
});

document.getElementById("input-cxp-escanear-foto").addEventListener("change", async (e) => {
  const archivo = e.target.files[0];
  if (!archivo) return;
  const formData = new FormData();
  formData.append("foto", archivo);
  document.getElementById("cmf-archivo").value = "";
  state.cmfToken = null;
  state.cmfFacturas = [];
  cmfMostrarPaso("subir");
  document.getElementById("cmf-titulo").textContent = "Factura escaneada — revisa antes de confirmar";
  abrirModal("modal-cxp-carga-masiva");
  try {
    await cmfCargarCatalogos();
    const res = await fetch("/api/facturas/escanear-foto", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo leer la foto");
    document.getElementById("btn-cmf-volver").textContent = "Escanear otra foto";
    cmfAbrirPreview(data);
  } catch (e) {
    toast(e.message, true);
    cerrarModal("modal-cxp-carga-masiva");
  }
  e.target.value = "";
});

document.querySelectorAll('input[name="cmf-condicion"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const condicion = document.querySelector('input[name="cmf-condicion"]:checked').value;
    document.getElementById("cmf-campos-mixto").style.display = condicion === "mixto" ? "grid" : "none";
    document.getElementById("cmf-campos-credito").style.display = condicion === "contado" ? "none" : "grid";
  });
});

document.querySelectorAll("[data-plazo-cmf]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.getElementById("cmf-plazo-dias").value = btn.dataset.plazoCmf;
  });
});

document.getElementById("btn-cmf-confirmar").addEventListener("click", async () => {
  if (!state.cmfToken) return;
  const condicion = document.querySelector('input[name="cmf-condicion"]:checked').value;
  try {
    // Red de seguridad: manda una última corrección de CADA línea visible
    // con el valor que hay ahora mismo en pantalla, antes de confirmar --
    // así "Confirmar carga" nunca puede dejar afuera un cambio que la
    // persona ya hizo, incluso si por algún motivo no se disparó solo
    // (ver cmfEnviarCorreccionLinea más arriba).
    for (const tr of document.querySelectorAll("#cmf-lista-facturas tr[data-cmf-linea]")) {
      await cmfEnviarCorreccionLinea(tr);
    }
    const data = await api("/api/facturas/carga-masiva/confirmar", {
      method: "POST",
      body: JSON.stringify({
        token: state.cmfToken,
        condicion_pago: condicion,
        porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("cmf-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
        plazo_dias_credito: Number(document.getElementById("cmf-plazo-dias").value || 30),
      }),
    });
    const pendientesSinAplicar = data.total_creadas - data.total_aplicadas;
    toast(
      (data.total_aplicadas ? `${data.total_aplicadas} factura(s) aplicadas (stock + Cuenta por Pagar)` : "") +
      (data.total_aplicadas && pendientesSinAplicar ? " — " : "") +
      (pendientesSinAplicar ? `${pendientesSinAplicar} quedaron pendientes de recepción (falta vincular un producto nuevo)` : "") +
      (data.omitidas.length ? ` — se omitieron ${data.omitidas.length} con errores (${data.omitidas.join(", ")})` : "")
    );
    cerrarModal("modal-cxp-carga-masiva");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Reportes
// ---------------------------------------------------------------------------

document.getElementById("btn-reporte-finanzas").addEventListener("click", () => {
  window.location.href = `/api/reportes/finanzas-resumen?${rangoFechasParams("reportes-finanzas-desde", "reportes-finanzas-hasta")}`;
});
document.getElementById("btn-reporte-finanzas-pdf").addEventListener("click", () => {
  window.location.href = `/api/reportes/finanzas-resumen/pdf?${rangoFechasParams("reportes-finanzas-desde", "reportes-finanzas-hasta")}`;
});

document.getElementById("btn-reporte-caja").addEventListener("click", () => {
  window.location.href = `/api/reportes/caja-gastos?${rangoFechasParams("reportes-caja-desde", "reportes-caja-hasta")}`;
});
document.getElementById("btn-reporte-caja-pdf").addEventListener("click", () => {
  window.location.href = `/api/reportes/caja-gastos/pdf?${rangoFechasParams("reportes-caja-desde", "reportes-caja-hasta")}`;
});

// ---------------------------------------------------------------------------
// Estado de Resultados mensual
// ---------------------------------------------------------------------------

const RUBRO_GAV_LABEL = {
  generales: "Gastos generales",
  servicios_basicos: "Servicios básicos",
  combustible: "Combustible",
  bancarios: "Gastos bancarios",
  mantencion: "Mantención y equipos",
  otros: "Otros gastos",
  gastos_por_ot: "Gastos por OT",
};

state.erUltimoReporte = null;

function mesActualInput() {
  const hoy = new Date();
  return `${hoy.getFullYear()}-${String(hoy.getMonth() + 1).padStart(2, "0")}`;
}

// fmtMoney (compartido) antepone "$" incluso a números negativos (queda
// "$-123") -- acá el signo importa mucho más (utilidades negativas son
// esperables en un Estado de Resultados), así que se arma aparte con el
// signo antes del "$", como se acostumbra a leer un balance.
function erMoney(n) {
  const negativo = n < 0;
  return `${negativo ? "-" : ""}$${Math.round(Math.abs(n)).toLocaleString("es-CL")}`;
}

// Cada línea "hoja" (ventas, costo de ventas, GAV por rubro, y los 12
// campos manuales) es clicable y abre el detalle vía /api/finanzas/estado-
// resultados/detalle -- ver LINEAS_DETALLE_LISTA/MANUAL en
// modulos/finanzas.py, las claves de acá tienen que coincidir con esas.
// Las líneas de subtotal/total no pegan al servidor: se explican con la
// aritmética que arma erExplicarFormula() a partir del mismo reporte que
// ya está en memoria (state.erUltimoReporte).
function erFila(label, valor, { cls = "", linea = null, seccionTitulo = null } = {}) {
  const negClase = valor < 0 ? " er-negativo" : "";
  const clicable = linea ? ` data-clicable="1" data-linea="${escapeHtml(linea)}" tabindex="0" role="button"` : "";
  return `<div class="er-fila ${cls}"${clicable}>
    <span class="er-label">${escapeHtml(label)}</span>
    <span class="er-monto${negClase}">${erMoney(valor)}</span>
  </div>`;
}

function erFilaPct(label, valor, pct, opts = {}) {
  const negClase = valor < 0 ? " er-negativo" : "";
  const clicable = opts.linea ? ` data-clicable="1" data-linea="${escapeHtml(opts.linea)}" tabindex="0" role="button"` : "";
  const pctTxt = pct === null || pct === undefined ? "" : `<span class="er-pct">(${pct}%)</span>`;
  return `<div class="er-fila ${opts.cls || ""}"${clicable}>
    <span class="er-label">${escapeHtml(label)}</span>
    <span class="er-monto${negClase}">${erMoney(valor)}${pctTxt}</span>
  </div>`;
}

function erSeccion(titulo) {
  return `<div class="er-seccion-titulo">${escapeHtml(titulo)}</div>`;
}

function renderEstadoResultados(d) {
  const filasGav = Object.entries(d.gav.por_rubro).map(
    ([rubro, monto]) => erFila(RUBRO_GAV_LABEL[rubro] || rubro, monto, { linea: `gav_${rubro}` })
  ).join("");

  const avisoSinClasificar = d.gav.sin_clasificar > 0
    ? `<div class="er-aviso-sin-clasificar">Hay ${fmtMoney(d.gav.sin_clasificar)} en gastos de Caja Chica de este mes sin rubro asignado — no están incluidos en el total. <a href="#" data-clicable="1" data-linea="gav_sin_clasificar" style="color:inherit">Ver cuáles son</a>, o edítalos desde la pestaña "Caja Chica" y asígnales un rubro.</div>`
    : "";

  const [mesNum, anioNum] = [d.mes.slice(5, 7), d.mes.slice(0, 4)];
  const nombreMes = new Date(`${d.mes}-01T00:00:00`).toLocaleDateString("es-CL", { month: "long", year: "numeric" });

  const html = `
    <div class="er-papel-wrap">
      <div class="er-papel">
        <div class="er-papel-encabezado">
          <h2>Estado de Resultados</h2>
          <p>${escapeHtml(nombreMes.charAt(0).toUpperCase() + nombreMes.slice(1))}</p>
        </div>

        ${erSeccion("Ventas")}
        ${erFila("Ventas Operativo (OTs entregadas)", d.ventas.ventas_ot, { linea: "ventas_ot" })}
        ${erFila("Ventas Convertidores (directas)", d.ventas.ventas_ct, { linea: "ventas_ct" })}
        ${erFila("Otros ingresos", d.ventas.otros_ingresos, { linea: "otros_ingresos" })}
        ${erFila("Total Ventas", d.ventas.total, { cls: "er-subtotal" })}

        ${erSeccion("Costo de Ventas")}
        ${erFila("Repuestos", d.costo_ventas.costo_repuestos, { linea: "costo_repuestos" })}
        ${erFila("Mano de obra (con costo cargado)", d.costo_ventas.costo_mano_obra, { linea: "costo_mano_obra" })}
        ${erFila("Convertidores de torque internos", d.costo_ventas.costo_convertidores_internos, { linea: "costo_convertidores" })}
        ${erFila("Costo de convertidores vendidos directo", d.costo_ventas.costo_ct_total, { linea: "costo_ct" })}
        ${erFila("Caja chica asociada a OT", d.costo_ventas.costo_caja_chica_ot)}
        ${erFila("Total Costo de Ventas", d.costo_ventas.total, { cls: "er-subtotal" })}

        ${erFilaPct("Utilidad Bruta", d.utilidad_bruta, d.margen_bruto_pct, { cls: "er-total", linea: "_formula:utilidad_bruta" })}

        ${erSeccion("Gastos de Administración y Ventas (GAV)")}
        ${filasGav || `<div class="er-fila er-vacio"><span class="er-label">Sin gastos de Caja Chica clasificados este mes</span><span class="er-monto">—</span></div>`}
        ${erFila("Remuneraciones", d.gav.remuneraciones, { linea: "remuneraciones" })}
        ${erFila("Bono remuneraciones", d.gav.bono_remuneraciones, { linea: "bono_remuneraciones" })}
        ${erFila("Otras remuneraciones", d.gav.otras_remuneraciones, { linea: "otras_remuneraciones" })}
        ${erFila("Previred", d.gav.previred, { linea: "previred" })}
        ${erFila("Arriendo", d.gav.arriendo, { linea: "arriendo" })}
        ${erFila("Seguros", d.gav.seguros, { linea: "seguros" })}
        ${erFila("Total GAV", d.gav.total, { cls: "er-subtotal" })}
        ${avisoSinClasificar}

        ${erFila("Utilidad de Operación", d.utilidad_operacional, { cls: "er-total", linea: "_formula:utilidad_operacional" })}

        ${erSeccion("Gastos Financieros")}
        ${erFila("Intereses (préstamos)", d.gastos_financieros.intereses, { linea: "gf_intereses" })}
        ${erFila("Factoring", d.gastos_financieros.factoring, { linea: "gf_factoring" })}
        ${erFila("Comisiones bancarias", d.gastos_financieros.comisiones, { linea: "gf_comisiones" })}
        ${erFila("Total Gastos Financieros", d.gastos_financieros.total, { cls: "er-subtotal" })}

        ${erFila("Utilidad antes de Impuesto", d.utilidad_antes_impuesto, { cls: "er-total", linea: "_formula:utilidad_antes_impuesto" })}
        ${erFila("Impuesto a la renta", d.impuesto_renta, { linea: "impuesto_renta" })}
        ${erFilaPct("Utilidad Neta", d.utilidad_neta, d.margen_neto_pct, { cls: "er-total", linea: "_formula:utilidad_neta" })}
        ${erFila("Asignación de dividendos", d.dividendos, { linea: "dividendos" })}
        ${erFila("Utilidad Retenida", d.utilidad_retenida, { cls: "er-total", linea: "_formula:utilidad_retenida" })}
      </div>
    </div>
  `;
  document.getElementById("er-reporte").innerHTML = html;
}

// ---------------------------------------------------------------------------
// Detalle de una línea ("¿cómo se llegó a este número?")
// ---------------------------------------------------------------------------

// Fórmulas de las líneas de subtotal/total -- se explican solas con los
// componentes que YA están en el reporte cargado, sin pedir nada al
// servidor (a diferencia de las líneas "hoja", que si necesitan ir a
// buscar el detalle transaccional -- ver abrirDetalleEr).
const ER_FORMULAS = {
  utilidad_bruta: (d) => ({
    titulo: "Utilidad Bruta",
    pasos: [["Total Ventas", d.ventas.total], ["Total Costo de Ventas", -d.costo_ventas.total]],
    resultado: d.utilidad_bruta,
  }),
  utilidad_operacional: (d) => ({
    titulo: "Utilidad de Operación",
    pasos: [["Utilidad Bruta", d.utilidad_bruta], ["Total GAV", -d.gav.total]],
    resultado: d.utilidad_operacional,
  }),
  utilidad_antes_impuesto: (d) => ({
    titulo: "Utilidad antes de Impuesto",
    pasos: [["Utilidad de Operación", d.utilidad_operacional], ["Total Gastos Financieros", -d.gastos_financieros.total]],
    resultado: d.utilidad_antes_impuesto,
  }),
  utilidad_neta: (d) => ({
    titulo: "Utilidad Neta",
    pasos: [["Utilidad antes de Impuesto", d.utilidad_antes_impuesto], ["Impuesto a la renta", -d.impuesto_renta]],
    resultado: d.utilidad_neta,
  }),
  utilidad_retenida: (d) => ({
    titulo: "Utilidad Retenida",
    pasos: [["Utilidad Neta", d.utilidad_neta], ["Asignación de dividendos", -d.dividendos]],
    resultado: d.utilidad_retenida,
  }),
};

function erRenderFormula(formula) {
  const filas = formula.pasos.map(([label, valor]) =>
    `<div class="erd-items" style="max-height:none"><li style="border:none;padding:3px 0">
      <span>${escapeHtml(label)}</span><span>${erMoney(valor)}</span>
    </li></div>`
  ).join("");
  return `
    <p class="ayuda-inline">Este número sale de sumar/restar otras líneas de este mismo reporte — no hay transacciones nuevas que listar acá, es aritmética directa.</p>
    <div class="erd-formula">
      ${formula.pasos.map(([label, valor]) => `${escapeHtml(label)}: ${erMoney(valor)}`).join("<br>")}
    </div>
    <div class="erd-total-fila"><span>${escapeHtml(formula.titulo)}</span><span>${erMoney(formula.resultado)}</span></div>
  `;
}

async function abrirDetalleEr(linea) {
  document.getElementById("erd-titulo").textContent = "Detalle";
  document.getElementById("erd-contenido").innerHTML = "Cargando…";
  abrirModal("modal-er-detalle");

  if (linea.startsWith("_formula:")) {
    const key = linea.slice("_formula:".length);
    const formula = ER_FORMULAS[key] ? ER_FORMULAS[key](state.erUltimoReporte) : null;
    document.getElementById("erd-titulo").textContent = formula ? formula.titulo : "Detalle";
    document.getElementById("erd-contenido").innerHTML = formula
      ? erRenderFormula(formula)
      : `<p class="ayuda-inline">No se pudo calcular el detalle.</p>`;
    return;
  }

  try {
    const mes = document.getElementById("er-mes").value;
    const data = await api(`/api/finanzas/estado-resultados/detalle?mes=${encodeURIComponent(mes)}&linea=${encodeURIComponent(linea)}`);
    if (data.tipo === "manual") {
      document.getElementById("erd-titulo").textContent = "Dato ingresado a mano";
      document.getElementById("erd-contenido").innerHTML = `
        <p class="ayuda-inline">${escapeHtml(data.mensaje)}</p>
        <button type="button" class="btn btn-secundario" id="erd-btn-editar-desde-detalle">Editar datos del mes</button>
      `;
      document.getElementById("erd-btn-editar-desde-detalle").addEventListener("click", () => {
        cerrarModal("modal-er-detalle");
        document.getElementById("btn-er-editar-manual").click();
      });
      return;
    }
    document.getElementById("erd-titulo").textContent = `Detalle (${data.items.length} movimiento${data.items.length === 1 ? "" : "s"})`;
    if (!data.items.length) {
      document.getElementById("erd-contenido").innerHTML = `<p class="ayuda-inline">No hay movimientos de este mes que compongan esta línea.</p>`;
      return;
    }
    const items = data.items.map(it => `
      <li>
        <span>${escapeHtml(it.etiqueta)}${it.fecha ? `<br><span class="erd-item-fecha">${fmtFecha(it.fecha)}</span>` : ""}</span>
        <span>${erMoney(it.monto)}</span>
      </li>
    `).join("");
    document.getElementById("erd-contenido").innerHTML = `
      <ul class="erd-items">${items}</ul>
      <div class="erd-total-fila"><span>Total</span><span>${erMoney(data.total)}</span></div>
    `;
  } catch (e) {
    document.getElementById("erd-contenido").innerHTML = `<p class="ayuda-inline ayuda-inline--bloqueado">${escapeHtml(e.message)}</p>`;
  }
}

document.getElementById("er-reporte").addEventListener("click", (e) => {
  const fila = e.target.closest("[data-linea]");
  if (!fila) return;
  e.preventDefault();
  abrirDetalleEr(fila.dataset.linea);
});
document.getElementById("er-reporte").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const fila = e.target.closest("[data-linea]");
  if (!fila) return;
  e.preventDefault();
  abrirDetalleEr(fila.dataset.linea);
});

async function cargarEstadoResultados() {
  const inputMes = document.getElementById("er-mes");
  if (!inputMes.value) inputMes.value = mesActualInput();
  const mes = inputMes.value;
  document.getElementById("er-reporte").innerHTML = "Cargando…";
  try {
    const d = await api(`/api/finanzas/estado-resultados?mes=${encodeURIComponent(mes)}`);
    state.erUltimoReporte = d;
    renderEstadoResultados(d);
  } catch (e) {
    document.getElementById("er-reporte").innerHTML = `<p class="ayuda-inline ayuda-inline--bloqueado">${escapeHtml(e.message)}</p>`;
    toast(e.message, true);
  }
}

document.getElementById("er-mes").addEventListener("change", cargarEstadoResultados);

document.getElementById("btn-er-imprimir").addEventListener("click", () => window.print());

document.getElementById("btn-er-editar-manual").addEventListener("click", () => {
  const mes = document.getElementById("er-mes").value || mesActualInput();
  const manual = (state.erUltimoReporte && state.erUltimoReporte.mes === mes) ? state.erUltimoReporte.manual : null;
  document.getElementById("erm-mes-titulo").textContent = mes;
  document.getElementById("form-er-manual").dataset.mes = mes;
  document.getElementById("erm-otros-ingresos").value = manual ? manual.otros_ingresos : 0;
  document.getElementById("erm-remuneraciones").value = manual ? manual.remuneraciones : 0;
  document.getElementById("erm-bono-remuneraciones").value = manual ? manual.bono_remuneraciones : 0;
  document.getElementById("erm-otras-remuneraciones").value = manual ? manual.otras_remuneraciones : 0;
  document.getElementById("erm-previred").value = manual ? manual.previred : 0;
  document.getElementById("erm-arriendo").value = manual ? manual.arriendo : 0;
  document.getElementById("erm-seguros").value = manual ? manual.seguros : 0;
  document.getElementById("erm-gf-intereses").value = manual ? manual.gastos_financieros_intereses : 0;
  document.getElementById("erm-gf-factoring").value = manual ? manual.gastos_financieros_factoring : 0;
  document.getElementById("erm-gf-comisiones").value = manual ? manual.gastos_financieros_comisiones : 0;
  document.getElementById("erm-impuesto-renta").value = manual ? manual.impuesto_renta : 0;
  document.getElementById("erm-dividendos").value = manual ? manual.dividendos : 0;
  abrirModal("modal-er-manual");
});

document.getElementById("form-er-manual").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mes = e.target.dataset.mes;
  const payload = {
    mes,
    otros_ingresos: Number(document.getElementById("erm-otros-ingresos").value || 0),
    remuneraciones: Number(document.getElementById("erm-remuneraciones").value || 0),
    bono_remuneraciones: Number(document.getElementById("erm-bono-remuneraciones").value || 0),
    otras_remuneraciones: Number(document.getElementById("erm-otras-remuneraciones").value || 0),
    previred: Number(document.getElementById("erm-previred").value || 0),
    arriendo: Number(document.getElementById("erm-arriendo").value || 0),
    seguros: Number(document.getElementById("erm-seguros").value || 0),
    gastos_financieros_intereses: Number(document.getElementById("erm-gf-intereses").value || 0),
    gastos_financieros_factoring: Number(document.getElementById("erm-gf-factoring").value || 0),
    gastos_financieros_comisiones: Number(document.getElementById("erm-gf-comisiones").value || 0),
    impuesto_renta: Number(document.getElementById("erm-impuesto-renta").value || 0),
    dividendos: Number(document.getElementById("erm-dividendos").value || 0),
  };
  try {
    await api("/api/finanzas/estado-resultados/manual", { method: "POST", body: JSON.stringify(payload) });
    toast("Datos del mes guardados");
    cerrarModal("modal-er-manual");
    await cargarEstadoResultados();
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---------------------------------------------------------------------------
// Facturación electrónica (DTE a clientes) -- Migración 28. No confundir
// con la pestaña "Facturas" (arriba), que son facturas de COMPRA a
// proveedores. El proveedor real (SimpleAPI u otro) se elige en el backend
// (ver modulos/facturacion.py, PROVEEDOR_DTE) -- acá el frontend solo
// habla con /api/facturacion/..., nunca directo con el proveedor.
// ---------------------------------------------------------------------------

const TIPO_DTE_LABEL = { 33: "Factura", 61: "Nota de crédito" };
const ESTADO_DTE_LABEL = { borrador: "Borrador", emitido: "Emitido", anulado: "Anulado", error: "Con error" };

async function cargarDte() {
  await cargarDteConfiguracion();
  await cargarDocumentosDte();
}

async function cargarDteConfiguracion() {
  try {
    const config = await api("/api/facturacion/configuracion");
    state.dteConfig = config;
    const aviso = document.getElementById("dte-aviso-simulado");
    if (config.proveedor_dte === "simulado") {
      aviso.textContent = "⚠️ Estás en modo de prueba (proveedor 'simulado'): las facturas que emitas acá NO se mandan al SII, solo sirven para probar el flujo. Cuando tengas tu cuenta con el proveedor real, se activa desde Railway.";
      aviso.className = "ayuda-inline ayuda-inline--bloqueado";
      aviso.style.display = "block";
    } else {
      aviso.style.display = "none";
    }
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-dte-configuracion").addEventListener("click", () => {
  const c = state.dteConfig || {};
  document.getElementById("dte-config-proveedor").textContent = c.proveedor_dte || "simulado";
  document.getElementById("dtec-razon-social").value = c.razon_social || "";
  document.getElementById("dtec-rut-emisor").value = c.rut_emisor || "";
  document.getElementById("dtec-giro").value = c.giro || "";
  document.getElementById("dtec-direccion").value = c.direccion || "";
  document.getElementById("dtec-comuna").value = c.comuna || "";
  document.getElementById("dtec-actividad-economica").value = c.actividad_economica || "";
  abrirModal("modal-dte-configuracion");
});

document.getElementById("form-dte-configuracion").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/facturacion/configuracion", {
      method: "PUT",
      body: JSON.stringify({
        razon_social: document.getElementById("dtec-razon-social").value,
        rut_emisor: document.getElementById("dtec-rut-emisor").value,
        giro: document.getElementById("dtec-giro").value,
        direccion: document.getElementById("dtec-direccion").value,
        comuna: document.getElementById("dtec-comuna").value,
        actividad_economica: document.getElementById("dtec-actividad-economica").value,
      }),
    });
    toast("Datos del emisor guardados");
    cerrarModal("modal-dte-configuracion");
    await cargarDteConfiguracion();
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function cargarDocumentosDte() {
  const tbody = document.getElementById("tbody-dte");
  tbody.innerHTML = `<tr><td colspan="9" class="vacio">Cargando…</td></tr>`;
  const params = new URLSearchParams();
  const q = document.getElementById("dte-buscar").value.trim();
  const tipo = document.getElementById("dte-filtro-tipo").value;
  const estado = document.getElementById("dte-filtro-estado").value;
  if (q) params.set("q", q);
  if (tipo) params.set("tipo", tipo);
  if (estado) params.set("estado", estado);
  try {
    const data = await api("/api/facturacion/documentos?" + params.toString());
    state.dteDocumentos = data.documentos;
    if (data.documentos.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No hay documentos emitidos todavía.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.documentos.map(d => `
      <tr data-abrir-dte="${d.id}" style="cursor:pointer">
        <td>${fmtFecha(d.creado_en)}</td>
        <td>${TIPO_DTE_LABEL[d.tipo_dte] || d.tipo_dte}</td>
        <td class="mono">${d.folio ?? "—"}</td>
        <td>${escapeHtml(d.receptor_razon_social)} <span class="ayuda-inline">(${escapeHtml(d.receptor_rut)})</span></td>
        <td class="mono">${d.orden_id ? `OT #${d.orden_id}` : "—"}</td>
        <td class="num mono">${fmtMoney(d.monto_total)}</td>
        <td>${ESTADO_DTE_LABEL[d.estado] || d.estado}</td>
        <td>${escapeHtml(d.usuario || "")}</td>
        <td></td>
      </tr>`).join("");
    tbody.querySelectorAll("[data-abrir-dte]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleDte(Number(tr.dataset.abrirDte)));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar: ${escapeHtml(e.message)}</td></tr>`;
  }
}

let dteBuscarTimeout;
document.getElementById("dte-buscar").addEventListener("input", () => {
  clearTimeout(dteBuscarTimeout);
  dteBuscarTimeout = setTimeout(cargarDocumentosDte, 250);
});
document.getElementById("dte-filtro-tipo").addEventListener("change", cargarDocumentosDte);
document.getElementById("dte-filtro-estado").addEventListener("change", cargarDocumentosDte);

// ---- Emitir factura ----

function renderItemsDteFactura() {
  const cont = document.getElementById("dtef-items-lista");
  cont.innerHTML = (state.dteItems || []).map((item, i) => `
    <div class="grid-2" data-item-dte="${i}">
      <div class="campo">
        <label>Descripción</label>
        <input type="text" data-campo-item="nombre" value="${escapeHtml(item.nombre)}">
      </div>
      <div class="campo">
        <label>Cantidad</label>
        <input type="number" data-campo-item="cantidad" min="0.01" step="0.01" value="${item.cantidad}">
      </div>
      <div class="campo">
        <label>Precio unitario ($ CLP)</label>
        <input type="number" data-campo-item="precio_unitario" min="0" step="1" value="${item.precio_unitario}">
      </div>
      <div class="campo" style="align-self:flex-end">
        <button type="button" class="btn btn-secundario" data-quitar-item-dte="${i}">Quitar ítem</button>
      </div>
    </div>`).join("");
  cont.querySelectorAll("[data-item-dte]").forEach(fila => {
    const i = Number(fila.dataset.itemDte);
    fila.querySelectorAll("[data-campo-item]").forEach(input => {
      input.addEventListener("input", () => {
        const campo = input.dataset.campoItem;
        state.dteItems[i][campo] = campo === "nombre" ? input.value : Number(input.value || 0);
        recalcularTotalesDte();
      });
    });
  });
  cont.querySelectorAll("[data-quitar-item-dte]").forEach(btn => {
    btn.addEventListener("click", () => {
      state.dteItems.splice(Number(btn.dataset.quitarItemDte), 1);
      renderItemsDteFactura();
      recalcularTotalesDte();
    });
  });
}

function recalcularTotalesDte() {
  const neto = round2((state.dteItems || []).reduce((s, it) => s + (Number(it.cantidad) || 0) * (Number(it.precio_unitario) || 0), 0));
  const iva = round2(neto * 0.19);
  const total = round2(neto + iva);
  document.getElementById("dtef-total-neto").textContent = fmtMoney(neto);
  document.getElementById("dtef-total-iva").textContent = fmtMoney(iva);
  document.getElementById("dtef-total-total").textContent = fmtMoney(total);
}

function round2(n) {
  return Math.round(n * 100) / 100;
}

function abrirModalNuevaFactura(ordenIdPrefill) {
  document.getElementById("dtef-orden-id").value = "";
  document.getElementById("dtef-orden-preview").style.display = "none";
  document.getElementById("dtef-receptor-rut").value = "";
  document.getElementById("dtef-receptor-razon-social").value = "";
  document.getElementById("dtef-receptor-giro").value = "";
  document.getElementById("dtef-receptor-direccion").value = "";
  document.getElementById("dtef-receptor-comuna").value = "";
  document.getElementById("dtef-receptor-email").value = "";
  document.getElementById("dtef-observaciones").value = "";
  state.dteItems = [{ nombre: "", cantidad: 1, precio_unitario: 0 }];
  renderItemsDteFactura();
  recalcularTotalesDte();
  const aviso = document.getElementById("dtef-aviso-simulado");
  if ((state.dteConfig || {}).proveedor_dte === "simulado") {
    aviso.textContent = "Modo de prueba: esta factura no se va a mandar al SII de verdad.";
    aviso.style.display = "block";
  } else {
    aviso.style.display = "none";
  }
  abrirModal("modal-dte-factura");
  // Si venimos desde el botón "Ir a Facturación electrónica" de la pestaña
  // "Factura" de una OT (ver operativo.js), se prellena el N° de OT y se
  // dispara el mismo lookup que si el usuario lo hubiera escrito a mano.
  if (ordenIdPrefill) {
    const input = document.getElementById("dtef-orden-id");
    input.value = ordenIdPrefill;
    input.dispatchEvent(new Event("input"));
  }
}

document.getElementById("btn-dte-nueva-factura").addEventListener("click", () => abrirModalNuevaFactura());

document.getElementById("dtef-btn-agregar-item").addEventListener("click", () => {
  state.dteItems.push({ nombre: "", cantidad: 1, precio_unitario: 0 });
  renderItemsDteFactura();
});

let dteOrdenLookupTimeout;
document.getElementById("dtef-orden-id").addEventListener("input", () => {
  clearTimeout(dteOrdenLookupTimeout);
  dteOrdenLookupTimeout = setTimeout(async () => {
    const numero = document.getElementById("dtef-orden-id").value.trim();
    const preview = document.getElementById("dtef-orden-preview");
    if (!numero) { preview.style.display = "none"; return; }
    try {
      const orden = await api(`/api/operativo/ordenes/${encodeURIComponent(numero)}`);
      const cliente = orden.cliente || {};
      preview.textContent = `✓ OT #${orden.id} — ${cliente.nombre || ""} · valor cobrado: ${fmtMoney(orden.valor_neto || 0)}`;
      preview.style.display = "block";
      document.getElementById("dtef-receptor-razon-social").value = cliente.nombre || "";
      document.getElementById("dtef-receptor-rut").value = cliente.rut || "";
      document.getElementById("dtef-receptor-direccion").value = cliente.direccion || "";
      document.getElementById("dtef-receptor-email").value = cliente.email || "";
      // OJO: orden.items son los REPUESTOS/mano de obra a COSTO interno, no
      // lo que se le cobra al cliente -- lo que se cobra es un solo monto
      // por OT (orden.valor_neto, el "precio referencial" que se pactó con
      // el cliente), así que se prellena un único ítem con ese valor en vez
      // de copiar los ítems internos de la OT.
      if (orden.valor_neto) {
        state.dteItems = [{ nombre: `Reparación OT #${orden.id}`, cantidad: 1, precio_unitario: orden.valor_neto }];
        renderItemsDteFactura();
        recalcularTotalesDte();
      }
    } catch (e) {
      preview.textContent = e.message || `No existe una orden de trabajo con el número "${numero}".`;
      preview.style.display = "block";
    }
  }, 400);
});

document.getElementById("dtef-btn-emitir").addEventListener("click", async (e) => {
  const payload = {
    orden_id: document.getElementById("dtef-orden-id").value.trim() || null,
    receptor: {
      rut: document.getElementById("dtef-receptor-rut").value,
      razon_social: document.getElementById("dtef-receptor-razon-social").value,
      giro: document.getElementById("dtef-receptor-giro").value,
      direccion: document.getElementById("dtef-receptor-direccion").value,
      comuna: document.getElementById("dtef-receptor-comuna").value,
      email: document.getElementById("dtef-receptor-email").value,
    },
    items: state.dteItems || [],
    observaciones: document.getElementById("dtef-observaciones").value,
  };
  // El más delicado de los formularios con este problema (hallazgo #4): un
  // doble clic acá emite DOS documentos tributarios electrónicos reales
  // ante el SII por la misma OT, no solo un registro duplicado en la BD.
  const btnEmitir = e.currentTarget;
  btnEmitir.disabled = true;
  try {
    await api("/api/facturacion/documentos", { method: "POST", body: JSON.stringify(payload) });
    toast("Factura emitida");
    cerrarModal("modal-dte-factura");
    await cargarDocumentosDte();
  } catch (e2) {
    toast(e2.message, true);
  } finally {
    btnEmitir.disabled = false;
  }
});

// ---- Detalle / anular con nota de crédito ----

async function abrirDetalleDte(id) {
  try {
    const d = await api(`/api/facturacion/documentos/${id}`);
    state.dteDocumentoActivoId = id;
    document.getElementById("dted-titulo").textContent = `${TIPO_DTE_LABEL[d.tipo_dte] || d.tipo_dte} #${d.folio ?? "—"}`;
    const items = (() => { try { return JSON.parse(d.items_json || "[]"); } catch (e) { return []; } })();
    const filasItems = items.map(it => `<tr><td>${escapeHtml(it.nombre)}</td><td class="num">${it.cantidad}</td><td class="num mono">${fmtMoney(it.precio_unitario)}</td><td class="num mono">${fmtMoney(it.subtotal)}</td></tr>`).join("");
    document.getElementById("dted-contenido").innerHTML = `
      <p><strong>Receptor:</strong> ${escapeHtml(d.receptor_razon_social)} (${escapeHtml(d.receptor_rut)})</p>
      ${d.receptor_giro ? `<p><strong>Giro:</strong> ${escapeHtml(d.receptor_giro)}</p>` : ""}
      ${d.receptor_direccion ? `<p><strong>Dirección:</strong> ${escapeHtml(d.receptor_direccion)} ${escapeHtml(d.receptor_comuna || "")}</p>` : ""}
      <p><strong>Estado:</strong> ${ESTADO_DTE_LABEL[d.estado] || d.estado}</p>
      ${d.error_mensaje ? `<p class="ayuda-inline ayuda-inline--bloqueado">${escapeHtml(d.error_mensaje)}</p>` : ""}
      ${items.length ? `<table class="tabla"><thead><tr><th>Ítem</th><th class="num">Cant.</th><th class="num">Precio unit.</th><th class="num">Subtotal</th></tr></thead><tbody>${filasItems}</tbody></table>` : ""}
      <p class="fac-total-linea">Neto: <strong>${fmtMoney(d.monto_neto)}</strong> — IVA: <strong>${fmtMoney(d.monto_iva)}</strong> — Total: <strong>${fmtMoney(d.monto_total)}</strong></p>
      ${d.observaciones ? `<p><strong>Observaciones:</strong> ${escapeHtml(d.observaciones)}</p>` : ""}
      ${d.orden_id ? `<p class="ayuda-inline">Emitida desde la OT #${d.orden_id}</p>` : ""}
    `;
    const puedeAnular = d.tipo_dte === 33 && d.estado === "emitido";
    document.getElementById("dted-btn-nota-credito").style.display = puedeAnular ? "inline-block" : "none";
    document.getElementById("dted-btn-nota-credito-confirmar").style.display = "none";
    document.getElementById("dted-nc-wrap").style.display = "none";
    document.getElementById("dted-nc-motivo").value = "";

    const puedeVerPdf = d.estado === "emitido" || d.estado === "anulado";
    const pdfUrlVer = `/api/facturacion/documentos/${id}/pdf`;
    const pdfUrlDescargar = `/api/facturacion/documentos/${id}/pdf?descargar=1`;
    document.getElementById("dted-btn-descargar-pdf").href = pdfUrlDescargar;
    document.getElementById("dted-pdf-preview").src = puedeVerPdf ? pdfUrlVer : "about:blank";
    document.getElementById("dted-pdf-preview-ampliar").href = pdfUrlVer;
    document.getElementById("dted-pdf-preview-ampliar").style.display = puedeVerPdf ? "block" : "none";
    document.getElementById("dted-pdf-preview").style.display = puedeVerPdf ? "block" : "none";
    document.querySelectorAll("#modal-dte-detalle .toolbar, #modal-dte-detalle h3.subtitulo, #modal-dte-detalle .grid-2, #dted-envio-historial")
      .forEach(el => { el.style.display = puedeVerPdf ? "" : "none"; });
    document.getElementById("dted-envio-destinatario").value = d.receptor_email || "";
    document.getElementById("dted-envio-historial").textContent = d.enviado_en
      ? `Ya se envió el ${fmtFecha(d.enviado_en)} a ${d.enviado_a}.`
      : "";
    abrirModal("modal-dte-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("dted-btn-enviar").addEventListener("click", async () => {
  const destinatario = document.getElementById("dted-envio-destinatario").value.trim();
  const btn = document.getElementById("dted-btn-enviar");
  btn.disabled = true;
  try {
    const data = await api(`/api/facturacion/documentos/${state.dteDocumentoActivoId}/enviar`, {
      method: "POST", body: JSON.stringify(destinatario ? { destinatario } : {}),
    });
    toast(`Enviado a ${data.destinatario}`);
    document.getElementById("dted-envio-historial").textContent = `Ya se envió el ${fmtFecha(data.documento.enviado_en)} a ${data.documento.enviado_a}.`;
    await cargarDocumentosDte();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("dted-btn-nota-credito").addEventListener("click", () => {
  document.getElementById("dted-nc-wrap").style.display = "block";
  document.getElementById("dted-btn-nota-credito").style.display = "none";
  document.getElementById("dted-btn-nota-credito-confirmar").style.display = "inline-block";
});

document.getElementById("dted-btn-nota-credito-confirmar").addEventListener("click", async () => {
  const motivo = document.getElementById("dted-nc-motivo").value.trim();
  if (!motivo) { toast("Indica el motivo de la nota de crédito", true); return; }
  if (!confirm("¿Emitir la nota de crédito y anular esta factura? No se puede deshacer.")) return;
  try {
    await api(`/api/facturacion/documentos/${state.dteDocumentoActivoId}/nota-credito`, {
      method: "POST", body: JSON.stringify({ motivo }),
    });
    toast("Nota de crédito emitida — la factura quedó anulada");
    cerrarModal("modal-dte-detalle");
    await cargarDocumentosDte();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
// Bodega con acceso restringido llega acá sin el botón "Resumen" en el DOM
// (ver templates/finanzas.html, 'solo_facturas') -- cargar el resumen
// general fallaría con 403 (/api/finanzas/resumen es solo para Finanzas) y
// ensuciaría la pantalla con un toast de error apenas carga. En ese caso se
// carga directo la única pestaña a la que tiene acceso.
if (document.querySelector('.tabs:not(.sub-tabs) > .tab[data-vista="resumen"]')) {
  cargarResumenFinanzas();
} else {
  document.getElementById("vista-facturas").style.display = "block";
  cargarFacturasPendientesRecepcion();
}
