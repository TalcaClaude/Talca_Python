const state = {
  sesion: { username: null, nombre: null },
  opOrdenes: [],
  opClientes: [],
  opEquiposClienteActual: [],
  opOrdenActivaId: null,
  opRecepcionEquipoActual: null,
  opProductos: [],
  opGarantias: [],
  opListado: [], // filas livianas de OT + garantías para la pestaña "Listado" -- ver cargarListadoOperativo()
  catalogoVehiculos: null, // { marcas: [...], modelos: {marca: [...]} } — catálogo de referencia (no depende del historial de OTs)
  opMesTablero: null, // mes elegido para el tablero de OTs (null = mes en curso, "todos" = historial completo)
  opMesDashboard: null, // mismo criterio, para los gráficos del dashboard
};

const OP_MESES_NOMBRE = [
  "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
  "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
];

// Arma las <option> de un selector de mes: mes actual + N meses anteriores +
// "Todo el historial" -- usado tanto en el tablero de OTs como en el
// dashboard de Operativo (ver <select id="op-filtro-mes"> y
// id="op-dash-filtro-mes"> en operativo.html), siempre con el mismo criterio
// que pidió el taller: Operativo es la vista mes a mes, no el histórico
// completo (eso vive en Panel Principal).
function opcionesSelectorMes(meses = 24) {
  const hoy = new Date();
  let html = "";
  for (let i = 0; i < meses; i++) {
    const d = new Date(hoy.getFullYear(), hoy.getMonth() - i, 1);
    const valor = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    const etiqueta = i === 0 ? `${OP_MESES_NOMBRE[d.getMonth()]} ${d.getFullYear()} (actual)` : `${OP_MESES_NOMBRE[d.getMonth()]} ${d.getFullYear()}`;
    html += `<option value="${valor}">${etiqueta}</option>`;
  }
  html += `<option value="todos">Todo el historial</option>`;
  return html;
}

// Actualización en vivo del tablero de OT: mientras la pestaña "Órdenes de
// Trabajo" esté abierta, se refresca sola cada pocos segundos — para ver,
// por ejemplo, cuando el agente de WhatsApp mueve una tarjeta sin tener que
// recargar la página. opArrastrandoTarjeta evita que un refresco automático
// interrumpa un drag-and-drop en curso (el tablero se reconstruye entero en
// cada render, lo que cortaría el arrastre nativo del navegador a medias).
let opPollingIntervalId = null;
let opArrastrandoTarjeta = false;
const OP_POLLING_MS = 4000;

// ---------------------------------------------------------------------------
// Utilidades compartidas (duplicadas de app.js: cada módulo carga su propio JS)
// ---------------------------------------------------------------------------

function fmtMoney(n) {
  return "$" + Math.round(n).toLocaleString("es-CL");
}

function fmtFecha(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("es-CL", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtFechaSolo(isoFecha) {
  if (!isoFecha) return "—";
  const [y, m, d] = isoFecha.split("-");
  return `${d}-${m}-${y}`;
}

// ---------------------------------------------------------------------------
// Formato de entrada: mayúsculas automáticas, RUT y montos con puntos de
// miles. El backend igual normaliza todo esto al guardar (ver
// modulos/operativo.py), pero aquí se refleja de inmediato mientras se
// escribe para que el usuario vea el formato final sin tener que guardar.
// ---------------------------------------------------------------------------

// Campos que NO deben pasarse a mayúsculas automáticamente: RUT y montos
// tienen su propio formateador; teléfono/campos deshabilitados/buscadores no
// son datos que se guarden con este criterio.
const CAMPOS_SIN_MAYUSCULAS = new Set([
  "op-buscar", "cli-buscar", "opc-telefono", "opc-rut", "opon-rut-cliente",
  "opon-encargado", "opod-valor", "oppi-precio", "opoi-costo",
  "opag-monto", "gari-costo",
]);

function activarMayusculasGlobal() {
  document.querySelectorAll('input[type="text"], textarea').forEach(el => {
    if (CAMPOS_SIN_MAYUSCULAS.has(el.id)) return;
    el.addEventListener("input", (e) => {
      const inicio = e.target.selectionStart;
      const fin = e.target.selectionEnd;
      const enMayusculas = e.target.value.toUpperCase();
      if (enMayusculas === e.target.value) return; // ya estaba en mayúsculas: no tocar el cursor
      e.target.value = enMayusculas;
      e.target.setSelectionRange(inicio, fin);
    });
  });
}
activarMayusculasGlobal();

// RUT chileno: puntos de miles + guión antes del dígito verificador, en vivo
// mientras se escribe (mismo criterio que aplica el backend al guardar).
function formatearRutEnVivo(valor) {
  const limpio = valor.replace(/[^0-9kK]/g, "");
  if (!limpio) return "";
  const cuerpo = limpio.slice(0, -1);
  const dv = limpio.slice(-1).toUpperCase();
  if (!cuerpo) return dv;
  const cuerpoConPuntos = cuerpo.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `${cuerpoConPuntos}-${dv}`;
}

document.getElementById("opc-rut").addEventListener("input", (e) => {
  const cursorAlFinal = e.target.selectionEnd === e.target.value.length;
  e.target.value = formatearRutEnVivo(e.target.value);
  if (cursorAlFinal) e.target.setSelectionRange(e.target.value.length, e.target.value.length);
});

// Montos ($): puntos de miles en vivo mientras se escribe. Los campos con
// class="input-monto" son de solo dígitos (ver limpiarMonto para obtener el
// valor numérico real antes de mandarlo al backend).
function formatearMontoEnVivo(valor) {
  const limpio = (valor || "").replace(/[^0-9]/g, "");
  if (!limpio) return "";
  return limpio.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

function limpiarMonto(valor) {
  return (valor || "").toString().replace(/[^0-9]/g, "");
}

document.querySelectorAll(".input-monto").forEach(el => {
  el.addEventListener("input", (e) => {
    const cursorAlFinal = e.target.selectionEnd === e.target.value.length;
    e.target.value = formatearMontoEnVivo(e.target.value);
    if (cursorAlFinal) e.target.setSelectionRange(e.target.value.length, e.target.value.length);
  });
});

// Días que lleva (o llevó) el vehículo en el taller: desde la recepción
// hasta hoy, o hasta la entrega real si la orden ya se entregó (para que
// el contador se congele en vez de seguir subiendo).
// Cuenta días CALENDARIO (no horas exactas / 24) entre la recepción y hoy
// (o la entrega real, si ya se cerró) -- se compara solo la fecha, sin la
// hora, porque si no una OT recibida ayer a las 23:50 mostraba "0 días"
// hasta pasadas casi 24 horas reales, en vez de "1 día" apenas cruzaba la
// medianoche (que es como el taller cuenta los días: por fecha, no por
// horas transcurridas).
function diasEnTallerNumero(orden) {
  if (!orden.fecha_recepcion) return null;
  const inicio = new Date(orden.fecha_recepcion);
  if (isNaN(inicio)) return null;
  const finRaw = orden.fecha_entrega_real ? new Date(orden.fecha_entrega_real) : new Date();
  if (isNaN(finRaw)) return null;
  const soloFecha = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  return Math.max(0, Math.round((soloFecha(finRaw) - soloFecha(inicio)) / 86400000));
}

function diasEnTaller(orden) {
  const dias = diasEnTallerNumero(orden);
  if (dias === null) return "—";
  return `${dias} día${dias === 1 ? "" : "s"} en el taller`;
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

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".modal-overlay.abierto").forEach(overlay => {
    overlay.classList.remove("abierto");
  });
});

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Búsqueda sin tildes y multi-palabra (misma lógica que app.js): cada
// palabra escrita debe aparecer en alguno de los campos entregados, sin
// importar mayúsculas/tildes ni en cuál campo específico caiga cada una.
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
    throw err;
  }
  return data;
}

async function cargarSesion() {
  try {
    const s = await api("/api/sesion");
    state.sesion = s;
    document.getElementById("sesion-usuario").textContent = s.nombre ? `👤 ${s.nombre}` : "";
  } catch (e) {
    console.error(e);
  }
}

// ---------------------------------------------------------------------------
// Pestañas: Dashboard / Órdenes de Trabajo / Clientes / Garantías / Reportes
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-dashboard").style.display = vista === "dashboard" ? "block" : "none";
    document.getElementById("vista-ordenes").style.display = vista === "ordenes" ? "block" : "none";
    document.getElementById("vista-listado").style.display = vista === "listado" ? "block" : "none";
    document.getElementById("vista-clientes").style.display = vista === "clientes" ? "block" : "none";
    document.getElementById("vista-garantias").style.display = vista === "garantias" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      dashboard: "Operativo — Dashboard",
      ordenes: "Operativo — Órdenes de Trabajo",
      listado: "Operativo — Listado",
      clientes: "Operativo — Clientes",
      garantias: "Operativo — Garantías",
      reportes: "Operativo — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    detenerPollingOrdenes();
    if (vista === "dashboard") cargarDashboardOperativo();
    if (vista === "ordenes") {
      cargarOperativo();
      iniciarPollingOrdenes();
    }
    if (vista === "listado") cargarListadoOperativo();
    if (vista === "clientes") cargarClientesTab();
    if (vista === "garantias") cargarGarantiasTab();
    if (vista === "reportes") cargarResumenReportesOperativo();
  });
});

// El sidebar enlaza a /operativo#vista (ver _sidebar.html). Como los tabs
// se manejan 100% en JS, hay que leer el hash al cargar la página y también
// reaccionar si cambia estando ya en esta vista.
function activarPestanaDesdeHash() {
  const vista = location.hash.replace("#", "");
  if (!vista) return;
  const tabBtn = document.querySelector(`.tab[data-vista="${vista}"]`);
  if (tabBtn) tabBtn.click();
}
window.addEventListener("hashchange", activarPestanaDesdeHash);
activarPestanaDesdeHash();

// La campanita de notificaciones (ver _notificaciones.html) enlaza acá con
// "?abrir_ot=<id>&tab=<pestana>#ordenes" -- al llegar así, se abre directo
// el detalle de esa OT en la pestaña indicada (por defecto "diagnostico",
// donde vive el aviso de sugerencias; "qc" para la bitácora de pruebas de
// ruta), en vez de dejar que la persona la busque a mano en el tablero.
async function abrirOtDesdeQueryParam() {
  const params = new URLSearchParams(location.search);
  const otId = params.get("abrir_ot");
  if (!otId) return;
  const ordenId = parseInt(otId, 10);
  const pestana = params.get("tab") || "diagnostico";
  await abrirDetalleOrden(ordenId);
  const tabDestino = document.querySelector(`#modal-op-orden-detalle .op-tab[data-opod-tab="${pestana}"]`);
  if (tabDestino) tabDestino.click();
  // Se limpia el query param para que recargar la página no vuelva a abrir
  // el mismo modal solo, y para no dejar el ID de la OT visible en la URL.
  const url = new URL(location.href);
  url.searchParams.delete("abrir_ot");
  url.searchParams.delete("tab");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
  // Al llegar acá desde el link de un aviso (campanita/alerta flotante),
  // se marca como "vista" todo lo pendiente de ESTA orden -- así deja de
  // sumar en el contador/alerta roja aunque siga apareciendo en el panel
  // de la campanita hasta que alguien la descarte a mano (pedido del
  // taller: "una vez que se pincha y se abre, ya debería quedar marcada
  // como revisada... pero no es necesario que desaparezca"). Se hace
  // siempre que se llega con abrir_ot, no solo desde el aviso, porque no
  // hay forma de distinguir el origen del link y no tiene costo marcarla
  // igual si la persona abrió la OT por otro camino.
  try {
    await api(`/api/notificaciones/marcar-vista/${ordenId}`, { method: "POST" });
    if (typeof notifSondear === "function") await notifSondear();
  } catch (e) {
    // Silencioso -- no bloquea la apertura de la OT si esto falla.
  }
}
abrirOtDesdeQueryParam();

// ---------------------------------------------------------------------------
// Dashboard Operativo: KPIs
// ---------------------------------------------------------------------------

async function cargarDashboardOperativo() {
  try {
    const data = await api("/api/operativo/dashboard");
    document.getElementById("op-s-abiertas").textContent = data.ots_abiertas;
    document.getElementById("op-s-cerradas-mes").textContent = data.ots_cerradas_mes;
    document.getElementById("op-s-monto-cerradas-mes").textContent = fmtMoney(data.monto_cerradas_mes);
    document.getElementById("op-s-monto-por-cerrar").textContent = fmtMoney(data.monto_por_cerrar);
    document.getElementById("op-s-utilidad-mes").textContent = fmtMoney(data.utilidad_mes);
    document.getElementById("op-s-costo-ots-cerradas").textContent = fmtMoney(data.costo_ots_cerradas);
    document.getElementById("op-s-costo-mano-obra-cerradas").textContent = fmtMoney(data.costo_mano_obra_ots_cerradas || 0);
    document.getElementById("op-s-costo-convertidores-cerradas").textContent = fmtMoney(data.costo_convertidores_ots_cerradas || 0);
    document.getElementById("op-s-costo-caja-chica-cerradas").textContent = fmtMoney(data.costo_caja_chica_ots_cerradas || 0);
    document.getElementById("op-s-costo-garantias").textContent = fmtMoney(data.costo_garantias_mes);
    document.getElementById("op-s-garantias-abiertas").textContent = data.garantias_abiertas;
    document.getElementById("op-s-ticket-promedio").textContent = fmtMoney(data.ticket_promedio_mes);
    document.getElementById("op-s-tiempo-promedio").textContent =
      data.tiempo_promedio_taller_mes ? `${data.tiempo_promedio_taller_mes} días` : "—";
    document.getElementById("op-s-tasa-aprobacion").textContent =
      data.tasa_aprobacion_presupuestos != null ? `${data.tasa_aprobacion_presupuestos}%` : "Sin datos";
    document.getElementById("op-s-costo-garantias-pct").textContent =
      data.costo_garantias_pct_ingresos_mes != null ? `${data.costo_garantias_pct_ingresos_mes}%` : "—";
  } catch (e) {
    toast(e.message, true);
  }
  cargarGraficosDashboardOperativo();
}

async function cargarGraficosDashboardOperativo() {
  try {
    const mesQS = state.opMesDashboard ? `?mes=${encodeURIComponent(state.opMesDashboard)}` : "";
    const data = await api(`/api/operativo/dashboard/graficos${mesQS}`);

    // "OT ingresadas por mes" es una CANTIDAD (nº de OT), no plata -- el
    // tooltip por defecto de buildLineChart usa formato $ porque la mayoría
    // de los gráficos de línea del sistema son montos; acá se pisa con un
    // formateador propio para que no diga "$15" cuando son 15 órdenes.
    document.getElementById("op-chart-tendencia").innerHTML = buildLineChart(
      data.tendencia_mensual.map(d => ({ mes: d.mes, valor: d.cantidad })),
      { tooltipValueFmt: (v) => `${Math.round(v)} OT${Math.round(v) === 1 ? "" : "s"}` }
    );

    const estadosConDatos = data.distribucion_estado.filter(d => d.cantidad > 0);
    document.getElementById("op-chart-estados").innerHTML = buildBarChartV(
      estadosConDatos.map(d => ({ label: d.label, valor: d.cantidad }))
    );

    // Top marcas: el tooltip no repite la cantidad (ya se lee en la barra),
    // muestra en cambio los 2-3 modelos de esa marca que más se repiten
    // este mes -- un dato que hoy no se ve en ningún otro lado del dashboard.
    document.getElementById("op-chart-marcas").innerHTML = buildBarChartH(
      data.top_marcas.map(d => ({
        label: d.marca,
        valor: d.cantidad,
        tt: (d.modelos_top && d.modelos_top.length)
          ? d.modelos_top.map(m => ({ l: m.modelo, v: `${m.cantidad} OT` }))
          : [{ l: "Sin modelo registrado", v: "" }],
      }))
    );

    // Modelos más atendidos: flechita de tendencia (sube/baja/estable vs. el
    // mes anterior) junto al número, y el tooltip muestra el mes anterior y
    // el promedio de los últimos 12 meses -- para saber si es un modelo que
    // está en auge o de salida, no solo cuántos se hicieron este mes.
    document.getElementById("op-chart-modelos").innerHTML = buildBarChartH(
      data.top_modelos.map(d => ({
        label: d.modelo,
        valor: d.cantidad,
        tendencia: d.tendencia,
        tt: [
          { l: "Este mes", v: `${d.cantidad} OT` },
          { l: "Mes anterior", v: `${d.cantidad_mes_anterior} OT` },
          { l: "Promedio mensual (12m)", v: `${d.promedio_12m} OT` },
        ],
      }))
    );

    // Clientes por monto: el tooltip no repite el monto (ya se ve al lado de
    // la barra), muestra en cambio la cantidad de OTs y el ticket promedio.
    document.getElementById("op-chart-clientes").innerHTML = buildBarChartH(
      data.ranking_clientes.map(d => ({
        label: d.nombre,
        valor: d.monto,
        extra: fmtMoney(d.monto),
        tt: [
          { l: "Cantidad de OTs", v: String(d.cantidad) },
          { l: "Ticket promedio", v: fmtMoney(d.cantidad ? d.monto / d.cantidad : 0) },
        ],
      })),
      { padL: 140 }
    );

    document.getElementById("op-chart-garantias").innerHTML = buildLineChart(
      data.tasa_garantias_mensual.map(d => ({
        mes: d.mes,
        valor: d.pct,
        tt: [
          { l: "Tasa de garantías", v: `${d.pct}%` },
          { l: "Garantías generadas", v: String(d.garantias_creadas) },
          { l: "OT entregadas ese mes", v: String(d.entregadas) },
        ],
      })),
      { color: "var(--danger)" }
    );
  } catch (e) {
    // Los gráficos son un complemento -- si fallan, no tapamos los KPIs
    // de arriba (que sí son críticos) con un toast de error.
    console.error("Error cargando gráficos del dashboard:", e);
  }
}

// ---------------------------------------------------------------------------
// Módulo Operativo: órdenes de trabajo
// ---------------------------------------------------------------------------

const OP_ESTADOS_TABLERO = [
  "recibido", "diagnostico", "presupuesto", "esperando_aprobacion", "reparacion",
  "esperando_repuesto", "control_calidad", "listo", "entregado", "cancelado",
];

const OP_ESTADOS_LABEL = {
  recibido: "Recibido", diagnostico: "Diagnóstico", presupuesto: "Presupuesto",
  esperando_aprobacion: "Esperando aprobación", reparacion: "Reparación",
  esperando_repuesto: "Esperando repuesto", control_calidad: "Pruebas de ruta",
  listo: "Listo", entregado: "Entregado", cancelado: "Cancelado",
};

async function cargarOperativo(silencioso = false) {
  try {
    const mesQS = state.opMesTablero ? `?mes=${encodeURIComponent(state.opMesTablero)}` : "";
    const [dataOrdenes, dataClientes, dataProductos] = await Promise.all([
      api(`/api/operativo/ordenes${mesQS}`),
      api("/api/operativo/clientes"),
      api("/api/productos"),
    ]);
    state.opOrdenes = dataOrdenes.ordenes;
    state.opClientes = dataClientes.clientes;
    state.opProductos = dataProductos.productos;
    renderTableroOperativo();
    await cargarCatalogoVehiculos();
    poblarDatalistsOperativo();
  } catch (e) {
    // silencioso=true: se usa desde el refresco automático de fondo (ver
    // iniciarPollingOrdenes) — un error de red pasajero ahí no debería
    // interrumpir al usuario con un toast cada pocos segundos.
    if (!silencioso) toast(e.message, true);
  }
}

// ---- Actualización en vivo del tablero (ver declaración de
// opPollingIntervalId más arriba) ----

function iniciarPollingOrdenes() {
  detenerPollingOrdenes();
  opPollingIntervalId = setInterval(async () => {
    if (opArrastrandoTarjeta) return; // no interrumpir un drag-and-drop en curso
    if (state.opOrdenActivaId) {
      // Hay un modal de detalle de OT abierto: refrescarResumenHub ya deja
      // sincronizados tanto ese modal como el tablero de atrás (llama a
      // cargarOperativo internamente), así que no hace falta hacer las dos
      // cosas por separado.
      await refrescarResumenHub(state.opOrdenActivaId);
    } else {
      await cargarOperativo(true);
    }
  }, OP_POLLING_MS);
}

function detenerPollingOrdenes() {
  if (opPollingIntervalId) {
    clearInterval(opPollingIntervalId);
    opPollingIntervalId = null;
  }
}

// Catálogo de referencia de marcas y modelos (121 marcas / ~1700 modelos),
// provisto por el taller e incluido como archivo estático — no depende de
// ninguna API externa ni de lo que se haya escrito antes en el sistema.
// Se carga una sola vez y se reutiliza durante toda la sesión.
async function cargarCatalogoVehiculos() {
  if (state.catalogoVehiculos) return;
  try {
    const resp = await fetch("/static/data/catalogo_vehiculos.json");
    if (resp.ok) {
      state.catalogoVehiculos = await resp.json();
    }
  } catch (e) {
    // Si no está disponible por algún motivo, seguimos con el autocompletado
    // basado solo en el historial de OTs (comportamiento previo).
    console.warn("No se pudo cargar el catálogo de marcas/modelos:", e);
  }
}

// Lista base de trabajos propios del taller (especializado en cajas de
// cambio automáticas) — se ofrece siempre como punto de partida (igual que
// el catálogo de marcas), para que el campo "Trabajo a Realizar" no dependa
// únicamente de que ya existan órdenes anteriores con ese mismo texto (un
// taller recién empezado a usar el sistema, sin historial todavía, no tenía
// ninguna opción para elegir). Lista provista por el taller.
const TRABAJOS_COMUNES_BASE = [
  "OVERHAUL COMPLETO", "MANTENCIÓN TRANSMISIÓN", "OTROS TRABAJOS",
  "OVERHAUL CUERPO VALVULAS", "PÉRDIDA DE ACEITE", "CAMBIO DE ACEITE",
  "CAMBIO EMBRAGUE", "CAMBIO MECATRÓNICA", "CAMBIO CAJA TRANSFERENCIA",
  "REPARACIÓN CAJA MECANICA", "CAMPAÑA REVISIÓN", "DIAGNOSTICO Y PRESUPUESTO",
  "REPARACION CONVERTIDOR DE TORQUE", "CAMBIO DE TCM Y PROGRAMACION",
  "PROGRAMACION Y ADAPTACION", "NO APRUEBA PRESUPUESTO",
];

// Datalists de "select con texto libre" para Color/Modelo de
// Transmisión/Referencia (Marca/Modelo dejaron de ser datalist de texto
// libre — ver poblarSelectMarca/poblarSelectModelo más arriba, ahora son
// <select> de verdad).
function poblarDatalistsOperativo() {
  const colores = new Set();
  const transmisiones = new Set(), referencias = new Set();
  state.opOrdenes.forEach(o => {
    if (o.equipo) {
      if (o.equipo.color) colores.add(o.equipo.color);
      if (o.equipo.modelo_transmision) transmisiones.add(o.equipo.modelo_transmision);
    }
    if (o.referencia) referencias.add(o.referencia);
  });
  const llenar = (id, valores) => {
    document.getElementById(id).innerHTML = [...valores].sort((a, b) => a.localeCompare(b, "es")).map(v => `<option value="${escapeHtml(v)}">`).join("");
  };
  llenar("dl-op-colores", colores);
  llenar("dl-op-transmisiones", transmisiones);
  llenar("dl-op-referencias", referencias);
}

// "Trabajo a Realizar" es un <select> bloqueado: solo se puede elegir un
// valor de la lista fija del taller (pedido explícito — no debe permitir
// escribir texto libre). "Otros Trabajos" ya está en la lista como opción
// para los casos que no encajan en ninguna categoría puntual.
function poblarSelectTrabajo(valorActual, selectId = "opon-trabajo") {
  const select = document.getElementById(selectId);
  const opciones = [...TRABAJOS_COMUNES_BASE].sort((a, b) => a.localeCompare(b, "es"));
  select.innerHTML = '<option value="">Selecciona un trabajo…</option>' +
    opciones.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  select.value = valorActual && opciones.includes(valorActual) ? valorActual : "";
}

// ---------------------------------------------------------------------------
// Marca / Modelo: selects en cascada, no campos de texto libre. El pedido
// fue explícito: deben ser seleccionables, sin espacio para tipear
// cualquier cosa. La única excepción es una opción "otra marca / otro
// modelo" al final de cada lista, para no dejar bloqueado un vehículo cuya
// marca no esté en el catálogo (121 marcas, no cubre todo lo que puede
// entrar a un taller) — es el mismo patrón de "+Nuevo" que ya se usa en
// otras partes de este módulo, no algo nuevo para el usuario.
// ---------------------------------------------------------------------------
const VALOR_OTRA_MARCA = "__otra_marca__";
const VALOR_OTRO_MODELO = "__otro_modelo__";

function _marcasConocidas() {
  const marcas = new Set();
  if (state.catalogoVehiculos) state.catalogoVehiculos.marcas.forEach(m => marcas.add(m));
  state.opOrdenes.forEach(o => { if (o.equipo && o.equipo.marca) marcas.add(o.equipo.marca); });
  return [...marcas].sort((a, b) => a.localeCompare(b, "es"));
}

function _modelosConocidos(marca) {
  const modelos = new Set();
  const marcaLimpia = (marca || "").trim();
  if (!marcaLimpia) return [];
  if (state.catalogoVehiculos) {
    const marcaCanonica = state.catalogoVehiculos.marcas.find(m => m.toLowerCase() === marcaLimpia.toLowerCase());
    if (marcaCanonica) (state.catalogoVehiculos.modelos[marcaCanonica] || []).forEach(m => modelos.add(m));
  }
  state.opOrdenes.forEach(o => {
    if (o.equipo && o.equipo.marca && o.equipo.marca.toLowerCase() === marcaLimpia.toLowerCase() && o.equipo.modelo) {
      modelos.add(o.equipo.modelo);
    }
  });
  return [...modelos].sort((a, b) => a.localeCompare(b, "es"));
}

// Llena el <select> de marca con el catálogo + historial, seleccionando
// 'valorActual' si calza con alguna opción (sin distinguir mayúsculas), o
// activando la opción "otra marca" con el texto libre precargado si no
// calza con ninguna (para no esconder ni perder un dato ya guardado).
function poblarSelectMarca(selectId, otraInputId, valorActual) {
  const select = document.getElementById(selectId);
  const otraInput = document.getElementById(otraInputId);
  const opciones = _marcasConocidas();
  const valor = (valorActual || "").trim();
  const coincide = opciones.find(m => m.toLowerCase() === valor.toLowerCase());

  select.innerHTML = '<option value="">Selecciona una marca…</option>' +
    opciones.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("") +
    `<option value="${VALOR_OTRA_MARCA}">Otra marca (no está en la lista)…</option>`;

  if (coincide) {
    select.value = coincide;
    otraInput.style.display = "none";
    otraInput.value = "";
  } else if (valor) {
    select.value = VALOR_OTRA_MARCA;
    otraInput.style.display = "";
    otraInput.value = valor;
  } else {
    select.value = "";
    otraInput.style.display = "none";
    otraInput.value = "";
  }
}

// Llena el <select> de modelo según la marca ya elegida (vacío si todavía
// no hay marca, o si la marca elegida fue "otra" — no hay catálogo de
// modelos posible para una marca que no conocemos).
function poblarSelectModelo(selectId, otroInputId, marca, valorActual) {
  const select = document.getElementById(selectId);
  const otroInput = document.getElementById(otroInputId);
  const marcaLimpia = (marca || "").trim();
  if (!marcaLimpia) {
    select.innerHTML = '<option value="">Selecciona una marca primero…</option>';
    otroInput.style.display = "none";
    otroInput.value = "";
    return;
  }

  const opciones = _modelosConocidos(marcaLimpia);
  const valor = (valorActual || "").trim();
  const coincide = opciones.find(m => m.toLowerCase() === valor.toLowerCase());

  select.innerHTML = '<option value="">Selecciona un modelo…</option>' +
    opciones.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("") +
    `<option value="${VALOR_OTRO_MODELO}">Otro modelo (no está en la lista)…</option>`;

  if (coincide) {
    select.value = coincide;
    otroInput.style.display = "none";
    otroInput.value = "";
  } else if (valor) {
    select.value = VALOR_OTRO_MODELO;
    otroInput.style.display = "";
    otroInput.value = valor;
  } else {
    select.value = "";
    otroInput.style.display = "none";
    otroInput.value = "";
  }
}

// Valor final a guardar: si el select quedó en la opción "otra/otro", se usa
// lo escrito en el campo de texto libre de al lado; si no, el valor del select.
function valorSeleccionUOtro(selectId, otroInputId, valorOtro) {
  const select = document.getElementById(selectId);
  if (select.value === valorOtro) return document.getElementById(otroInputId).value.trim();
  return select.value;
}

// Conecta un par marca/modelo: al cambiar la marca, se repuebla el select
// de modelo (vacío, salvo que la marca sea "otra", en cuyo caso se fuerza
// directo el campo de texto libre del modelo, porque no hay catálogo
// posible para una marca desconocida).
function conectarMarcaModelo(marcaSelectId, marcaOtraId, modeloSelectId, modeloOtroId) {
  document.getElementById(marcaSelectId).addEventListener("change", (e) => {
    const esOtra = e.target.value === VALOR_OTRA_MARCA;
    document.getElementById(marcaOtraId).style.display = esOtra ? "" : "none";
    if (!esOtra) document.getElementById(marcaOtraId).value = "";
    poblarSelectModelo(modeloSelectId, modeloOtroId, esOtra ? "" : e.target.value, "");
    if (esOtra) {
      const modeloSelect = document.getElementById(modeloSelectId);
      modeloSelect.innerHTML = `<option value="${VALOR_OTRO_MODELO}" selected>Escribe el modelo →</option>`;
      document.getElementById(modeloOtroId).style.display = "";
    }
  });
}
conectarMarcaModelo("ope-marca", "ope-marca-otra", "ope-modelo", "ope-modelo-otro");
conectarMarcaModelo("opon-marca", "opon-marca-otra", "opon-modelo", "opon-modelo-otro");

// Cubre el otro caso: el usuario elige "Otro modelo" directamente en el
// select de modelo (con una marca normal ya elegida, ej. HONDA, pero el
// modelo que busca no está en el catálogo) — antes esto no mostraba el
// campo de texto libre porque solo se revisaba al cambiar la MARCA.
function conectarModeloOtro(modeloSelectId, modeloOtroId) {
  document.getElementById(modeloSelectId).addEventListener("change", (e) => {
    const esOtro = e.target.value === VALOR_OTRO_MODELO;
    document.getElementById(modeloOtroId).style.display = esOtro ? "" : "none";
    if (!esOtro) document.getElementById(modeloOtroId).value = "";
  });
}
conectarModeloOtro("ope-modelo", "ope-modelo-otro");
conectarModeloOtro("opon-modelo", "opon-modelo-otro");

function renderTableroOperativo() {
  const filtroEstado = document.getElementById("op-filtro-estado").value;
  const busqueda = document.getElementById("op-buscar").value.trim().toLowerCase();

  document.getElementById("op-columna-cancelado").style.display = filtroEstado === "cancelado" ? "flex" : "none";
  document.querySelectorAll("#op-kanban .op-columna").forEach(col => {
    const est = col.dataset.estado;
    if (filtroEstado) {
      col.style.display = est === filtroEstado ? "flex" : "none";
    } else {
      col.style.display = est === "cancelado" ? "none" : "flex";
    }
  });

  const coincide = (orden) => coincideBusqueda(
    busqueda,
    String(orden.id),
    orden.cliente ? orden.cliente.nombre : "",
    orden.equipo ? `${orden.equipo.marca} ${orden.equipo.modelo} ${orden.equipo.identificador}` : "",
    orden.descripcion_problema || "",
  );

  OP_ESTADOS_TABLERO.forEach(estado => {
    const cont = document.getElementById(`op-cards-${estado}`);
    const contador = document.getElementById(`op-count-${estado}`);
    const ordenes = state.opOrdenes.filter(o => o.estado === estado && coincide(o));
    contador.textContent = ordenes.length;
    if (ordenes.length === 0) {
      cont.innerHTML = `<p class="vacio-inline">Sin órdenes.</p>`;
      return;
    }
    cont.innerHTML = ordenes.map(o => `
      <div class="op-card op-card--${o.prioridad || "media"}" data-id="${o.id}" draggable="true">
        <div class="op-card-top">
          <span class="op-card-ot">OT #${o.id}</span>
          ${o.prioridad && o.prioridad !== "media" ? `<span class="badge-prioridad badge-prioridad--${o.prioridad}">${escapeHtml(o.prioridad)}</span>` : ""}
        </div>
        <div class="op-card-cliente">${escapeHtml(o.cliente ? o.cliente.nombre : "Sin cliente")}</div>
        ${o.equipo ? (() => {
          const nombre = [o.equipo.marca, o.equipo.modelo].filter(Boolean).join(" ");
          // Patente/año/color van en su PROPIA línea, no recortados — se
          // pidió explícito que se alcancen a leer completos aunque la
          // tarjeta quede un poco más alta (nunca mucho: es una sola línea
          // más, sin envolver texto).
          const detalle = [o.equipo.identificador, o.equipo.anio, o.equipo.color].filter(Boolean).join(" · ");
          return `
            ${nombre ? `<div class="op-card-equipo-nombre">${escapeHtml(nombre)}</div>` : ""}
            ${detalle ? `<div class="op-card-equipo-detalle">${escapeHtml(detalle)}</div>` : ""}
          `;
        })() : ""}
        <div class="op-card-valor">${fmtMoney(o.precio_referencial || 0)}</div>
        <div class="op-card-meta">
          <span>Recepción: ${fmtFechaSolo((o.fecha_recepcion || "").slice(0, 10))}</span>
          <span>${diasEnTaller(o)}</span>
        </div>
      </div>
    `).join("");
  });

  document.querySelectorAll("#op-kanban .op-card").forEach(card => {
    card.addEventListener("click", () => abrirDetalleOrden(parseInt(card.dataset.id, 10)));
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id);
      card.classList.add("dragging");
      opArrastrandoTarjeta = true;
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      opArrastrandoTarjeta = false;
    });
  });

  updateKanbanFades();
}

// ---------------------------------------------------------------------------
// Vista "Listado": historial completo de OT + garantías (todos los meses,
// abiertas y cerradas), en líneas por orden correlativo (Nº más reciente
// arriba) en vez de agrupadas por etapa. A diferencia del tablero, NO usa
// state.opOrdenes (que por defecto acota al mes en curso para lo cerrado) --
// se alimenta de su propio endpoint liviano /api/operativo/listado (ver
// cargarListadoOperativo) para poder traer todo el historial sin repetir
// miles de queries por fila. Pensada para tener un vistazo general rápido y
// poder buscar cualquier OT o garantía que se haya ingresado alguna vez, con
// filtros propios, independientes de los del tablero.
// ---------------------------------------------------------------------------

// Por defecto NO se trae el historial completo (~3.000+ OT/garantías) cada
// vez que se abre la pestaña -- eso la hacía lenta para nada (a pedido del
// taller, jul. 2026). En cambio se manda un "desde" reciente (acá, 6 meses
// atrás) y el backend siempre agrega además TODO lo que sigue abierto sin
// importar su fecha (ver _listado_ordenes_liviano/_listado_garantias_liviano
// en modulos/operativo.py) -- así nunca se esconde trabajo activo, solo se
// acota el historial ya cerrado.
//
// OJO: este default se aplica DENTRO de cargarListadoOperativo() (con un
// flag para que sea una sola vez), y NO en un bloque suelto al cargar la
// página -- se probó así primero y falló: activarPestanaDesdeHash() (más
// arriba en este archivo) puede llamar a cargarListadoOperativo() ANTES de
// que el script llegue a esta sección si la URL ya viene con "#listado",
// así que un bloque de inicialización acá abajo corría demasiado tarde y
// la primera carga salía sin fecha (pedía el historial completo igual).
let opListadoFechaPorDefectoAplicada = false;

async function cargarListadoOperativo() {
  const tbody = document.getElementById("op-list-tbody");
  if (tbody) tbody.innerHTML = `<tr><td colspan="12" class="vacio">Cargando…</td></tr>`;
  try {
    const campoDesde = document.getElementById("op-list-fecha-desde");
    if (!opListadoFechaPorDefectoAplicada) {
      opListadoFechaPorDefectoAplicada = true;
      if (!campoDesde.value) {
        const hace6Meses = new Date();
        hace6Meses.setMonth(hace6Meses.getMonth() - 6);
        campoDesde.value = hace6Meses.toISOString().slice(0, 10);
      }
    }
    const desde = campoDesde.value;
    const hasta = document.getElementById("op-list-fecha-hasta").value;
    const qs = new URLSearchParams();
    if (desde) qs.set("desde", desde);
    if (hasta) qs.set("hasta", hasta);
    const query = qs.toString() ? `?${qs.toString()}` : "";
    const data = await api(`/api/operativo/listado${query}`);
    state.opListado = data.filas;
    renderListadoOperativo();
  } catch (e) {
    toast(e.message, true);
    if (tbody) tbody.innerHTML = `<tr><td colspan="12" class="vacio">No se pudo cargar el listado.</td></tr>`;
  }
}

function renderListadoOperativo() {
  const tbody = document.getElementById("op-list-tbody");
  const contador = document.getElementById("op-list-contador");
  if (!tbody) return; // la pestaña "Listado" puede no estar en el DOM todavía

  const filtroTipo = document.getElementById("op-list-filtro-tipo").value;
  const filtroEstado = document.getElementById("op-list-filtro-estado").value;
  const filtroPrioridad = document.getElementById("op-list-filtro-prioridad").value;
  const busqueda = document.getElementById("op-list-buscar").value.trim().toLowerCase();

  // OJO: acá NO se vuelve a filtrar por fecha -- eso ya lo hizo el backend
  // en cargarListadoOperativo() (mandó desde/hasta como query params). Si
  // se repitiera el filtro acá con las mismas fechas, se terminaría
  // escondiendo por accidente las OT/garantías que siguen ABIERTAS pero
  // ingresaron antes del rango elegido (el backend las trae a propósito,
  // sin importar su fecha, porque son trabajo activo -- ver
  // _listado_ordenes_liviano en modulos/operativo.py).

  // El filtro de prioridad solo aplica a OT normales (las garantías no
  // tienen prioridad) -- si está activo, se excluyen directamente las
  // garantías del resultado, no se las deja pasar sin filtrar.
  const coincide = (f) => coincideBusqueda(
    busqueda,
    String(f.id),
    f.cliente_nombre || "",
    `${f.marca || ""} ${f.modelo || ""} ${f.patente || ""}`,
  );

  const filas = (state.opListado || [])
    .filter(f => !filtroTipo || f.tipo === filtroTipo)
    .filter(f => filtroEstado ? f.estado === filtroEstado : f.estado !== "cancelado")
    .filter(f => !filtroPrioridad || f.tipo === "nuevo")
    .filter(coincide)
    .sort((a, b) => b.id - a.id);

  contador.textContent = `${filas.length} línea${filas.length === 1 ? "" : "s"}`;

  if (filas.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12" class="vacio">Sin resultados que coincidan con el filtro.</td></tr>`;
    return;
  }

  tbody.innerHTML = filas.map(f => {
    const dias = diasEnTallerNumero({ fecha_recepcion: f.fecha_recepcion, fecha_entrega_real: f.fecha_entrega_real });
    const esGarantia = f.tipo === "garantia";
    return `
      <tr data-id="${f.id}" data-tipo="${f.tipo}">
        <td class="mono">${esGarantia ? "G" : "OT"} #${f.id}</td>
        <td>${esGarantia ? "Garantía" : "Nuevo"}</td>
        <td>${dias === null ? "—" : dias}</td>
        <td>${escapeHtml(f.cliente_nombre || "—")}</td>
        <td>${escapeHtml(f.marca || "") || "—"}</td>
        <td>${escapeHtml(f.modelo || "") || "—"}</td>
        <td>${escapeHtml(f.color || "") || "—"}</td>
        <td class="mono">${escapeHtml(f.patente || "") || "—"}</td>
        <td>${escapeHtml(f.estado_label || f.estado)}</td>
        <td class="num">${f.valor_total === null ? "—" : fmtMoney(f.valor_total)}</td>
        <td class="num">${f.total_pagado === null ? "—" : fmtMoney(f.total_pagado)}</td>
        <td>${fmtFechaSolo((f.fecha_recepcion || "").slice(0, 10))}</td>
      </tr>`;
  }).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    const id = parseInt(tr.dataset.id, 10);
    tr.addEventListener("click", () => {
      if (tr.dataset.tipo === "garantia") abrirDetalleGarantia(id);
      else abrirDetalleOrden(id);
    });
  });
}

// Las etapas de OT y de garantías son procesos distintos (algunas se llaman
// igual -- "Diagnóstico", "Listo", "Entregado" -- pero varias son
// exclusivas de un tipo, ej. "Recibido" solo existe en OT e "Ingreso" solo
// en garantías). Al elegir "Solo garantías"/"Solo nuevos (OT)" en el filtro
// de Tipo, se oculta el <optgroup> del <select> de Etapa que no corresponde,
// para no dejar elegir una etapa que nunca va a traer resultados con ese
// tipo. Si la etapa elegida quedaba en el grupo que se acaba de ocultar, se
// vuelve a "Todas las etapas" en vez de dejar un filtro imposible puesto.
function actualizarOpcionesEstadoListado() {
  const tipo = document.getElementById("op-list-filtro-tipo").value;
  const selectEstado = document.getElementById("op-list-filtro-estado");
  const grupoOt = document.getElementById("op-list-optgroup-ot");
  const grupoGarantia = document.getElementById("op-list-optgroup-garantia");

  const ocultarOt = tipo === "garantia";
  const ocultarGarantia = tipo === "nuevo";
  grupoOt.hidden = ocultarOt;
  grupoOt.style.display = ocultarOt ? "none" : "";
  grupoGarantia.hidden = ocultarGarantia;
  grupoGarantia.style.display = ocultarGarantia ? "none" : "";

  const valorActual = selectEstado.value;
  if (!valorActual) return;
  // OJO: 'optgroup' no tiene propiedad .options (esa es solo de <select>) --
  // hay que buscar los <option> con querySelectorAll.
  const estadosOt = new Set(Array.from(grupoOt.querySelectorAll("option")).map(o => o.value));
  const estadosGarantia = new Set(Array.from(grupoGarantia.querySelectorAll("option")).map(o => o.value));
  const sigueValido = (!ocultarOt && estadosOt.has(valorActual)) || (!ocultarGarantia && estadosGarantia.has(valorActual));
  if (!sigueValido) selectEstado.value = "";
}

["op-list-buscar"].forEach(id => document.getElementById(id).addEventListener("input", renderListadoOperativo));
document.getElementById("op-list-filtro-tipo").addEventListener("change", () => {
  actualizarOpcionesEstadoListado();
  renderListadoOperativo();
});
["op-list-filtro-estado", "op-list-filtro-prioridad"].forEach(id => {
  document.getElementById(id).addEventListener("change", renderListadoOperativo);
});
// Los campos de fecha (a diferencia de los demás filtros) SÍ disparan un
// nuevo pedido al backend en vez de solo re-filtrar lo ya cargado -- son
// justamente los que controlan cuánto historial se trae (ver
// cargarListadoOperativo), así que cambiarlos tiene que ir a buscar datos
// nuevos, no filtrar los que ya están en memoria.
["op-list-fecha-desde", "op-list-fecha-hasta"].forEach(id => {
  document.getElementById(id).addEventListener("change", cargarListadoOperativo);
});
document.getElementById("op-list-btn-historial-completo").addEventListener("click", () => {
  document.getElementById("op-list-fecha-desde").value = "";
  document.getElementById("op-list-fecha-hasta").value = "";
  cargarListadoOperativo();
});
actualizarOpcionesEstadoListado();

// El tablero (kanban) tiene más columnas de las que caben en una pantalla
// normal, así que hace scroll horizontal — estas dos franjas con flecha
// avisan de que hay más columnas para ese lado, para que no se sienta
// "cortado" sin explicación. Solo se muestran cuando efectivamente hay
// contenido oculto hacia ese lado (no son decorativas fijas).
function updateKanbanFades() {
  const kanban = document.getElementById("op-kanban");
  const fadeIzq = document.getElementById("op-kanban-fade-izq");
  const fadeDer = document.getElementById("op-kanban-fade-der");
  if (!kanban || !fadeIzq || !fadeDer) return;
  const rect = kanban.getBoundingClientRect();
  [fadeIzq, fadeDer].forEach(el => {
    el.style.top = `${rect.top}px`;
    el.style.height = `${rect.height}px`;
  });
  fadeIzq.style.left = `${rect.left}px`;
  fadeDer.style.left = `${rect.right - 56}px`;
  const haySobranteIzq = kanban.scrollLeft > 4;
  const haySobranteDer = kanban.scrollLeft + kanban.clientWidth < kanban.scrollWidth - 4;
  fadeIzq.classList.toggle("op-kanban-fade--visible", haySobranteIzq);
  fadeDer.classList.toggle("op-kanban-fade--visible", haySobranteDer);
}

(function conectarKanbanFades() {
  const kanban = document.getElementById("op-kanban");
  if (!kanban) return;
  kanban.addEventListener("scroll", updateKanbanFades);
  window.addEventListener("resize", updateKanbanFades);
  window.addEventListener("scroll", updateKanbanFades, true);
})();

// Auto-scroll del tablero mientras se arrastra una tarjeta: si el mouse pasa
// por encima de las flechas de los bordes (misma franja de ~70px que usa
// op-kanban-fade, ver arriba), el tablero se desplaza solo para ese lado,
// para no tener que soltar la tarjeta en una columna intermedia solo para
// poder seguir arrastrando hacia el otro extremo. Se conecta sobre el
// contenedor con scroll (el propio #kanban), no sobre las columnas, porque
// el dragover tiene que seguir disparándose sin importar en qué columna
// esté la tarjeta en ese instante.
function activarAutoScrollKanban(kanbanId) {
  const kanban = document.getElementById(kanbanId);
  if (!kanban) return;
  const ZONA_PX = 70; // ancho de la franja donde se activa el auto-scroll
  const VELOCIDAD_PX = 14; // px por tick (~60fps -> ~840px/s en el borde)
  let direccion = 0; // -1 izq, 0 quieto, 1 der
  let intervaloId = null;

  function tick() {
    if (direccion === 0) return;
    const max = kanban.scrollWidth - kanban.clientWidth;
    kanban.scrollLeft = Math.max(0, Math.min(max, kanban.scrollLeft + direccion * VELOCIDAD_PX));
  }

  function detener() {
    direccion = 0;
    if (intervaloId) {
      clearInterval(intervaloId);
      intervaloId = null;
    }
  }

  kanban.addEventListener("dragover", (e) => {
    const rect = kanban.getBoundingClientRect();
    const x = e.clientX;
    if (x < rect.left + ZONA_PX) direccion = -1;
    else if (x > rect.right - ZONA_PX) direccion = 1;
    else direccion = 0;

    if (direccion !== 0 && !intervaloId) {
      intervaloId = setInterval(tick, 16);
    } else if (direccion === 0 && intervaloId) {
      detener();
    }
  });
  kanban.addEventListener("dragleave", (e) => {
    // Solo detener si de verdad se salió del tablero, no al pasar entre
    // tarjetas/columnas hijas dentro del mismo contenedor.
    if (!kanban.contains(e.relatedTarget)) detener();
  });
  kanban.addEventListener("drop", detener);
  document.addEventListener("dragend", detener);
}
activarAutoScrollKanban("op-kanban");
activarAutoScrollKanban("gar-kanban");

// ---- Kanban interactivo: arrastrar una OT de una columna a otra cambia su
// estado. Los drop-zones son las columnas mismas (elementos fijos del HTML),
// así que los listeners se conectan una sola vez al cargar la página — solo
// el contenido interno de tarjetas se vuelve a pintar en cada render.
document.querySelectorAll("#op-kanban .op-columna").forEach(columna => {
  columna.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    columna.classList.add("drag-over");
  });
  columna.addEventListener("dragleave", () => {
    columna.classList.remove("drag-over");
  });
  columna.addEventListener("drop", async (e) => {
    e.preventDefault();
    columna.classList.remove("drag-over");
    const ordenId = parseInt(e.dataTransfer.getData("text/plain"), 10);
    const nuevoEstado = columna.dataset.estado;
    if (!ordenId || !nuevoEstado) return;
    const orden = state.opOrdenes.find(o => o.id === ordenId);
    if (orden && orden.estado === nuevoEstado) return; // soltada en su propia columna
    try {
      await api(`/api/operativo/ordenes/${ordenId}`, { method: "PUT", body: JSON.stringify({ estado: nuevoEstado }) });
      toast(`Orden #${ordenId} movida a "${OP_ESTADOS_LABEL[nuevoEstado] || nuevoEstado}".`);
      await cargarOperativo();
    } catch (err) {
      toast(err.message, true);
    }
  });
});

document.getElementById("op-buscar").addEventListener("input", renderTableroOperativo);
document.getElementById("op-filtro-estado").addEventListener("change", renderTableroOperativo);

// ---- Selector de mes (solo dashboard) ----
// El tablero de Órdenes de Trabajo (kanban) ya NO tiene selector de mes: las
// OT abiertas siempre se muestran todas sin importar el mes, y son la
// inmensa mayoría de lo que se mira en esa pestaña día a día -- tener un
// filtro de mes ahí solo generaba confusión ("¿por qué no veo esta OT?").
// state.opMesTablero se deja en null (mes en curso para las cerradas/
// entregadas, ver cargarOperativo) y no se puede cambiar desde la UI; si
// se necesita revisar el histórico de cerradas, está el dashboard o Reportes.
const selectMesDashboard = document.getElementById("op-dash-filtro-mes");
selectMesDashboard.innerHTML = opcionesSelectorMes();
selectMesDashboard.addEventListener("change", () => {
  state.opMesDashboard = selectMesDashboard.value;
  cargarGraficosDashboardOperativo();
});

// ---- Clientes ----

function renderSelectClientesOperativo(selectId, valorActual) {
  const select = document.getElementById(selectId);
  select.innerHTML = '<option value="">Selecciona un cliente…</option>' +
    state.opClientes.map(c => `<option value="${c.id}">${escapeHtml(c.nombre)}</option>`).join("");
  select.value = valorActual || "";
}

let opClienteCallback = null; // se llama con el cliente recién creado/editado, según de dónde se abrió el modal

function abrirNuevoClienteOperativo() {
  document.getElementById("form-op-cliente").reset();
  document.getElementById("opc-id").value = "";
  document.getElementById("modal-op-cliente-titulo").textContent = "Nuevo cliente";
  abrirModal("modal-op-cliente");
}

function abrirEdicionClienteOperativo(id) {
  const c = state.opClientes.find(x => x.id === id);
  if (!c) return;
  document.getElementById("opc-id").value = c.id;
  document.getElementById("opc-nombre").value = c.nombre;
  document.getElementById("opc-telefono").value = c.telefono || "";
  document.getElementById("opc-email").value = c.email || "";
  document.getElementById("opc-rut").value = c.rut || "";
  document.getElementById("opc-direccion").value = c.direccion || "";
  document.getElementById("opc-notas").value = c.notas || "";
  document.getElementById("modal-op-cliente-titulo").textContent = "Editar cliente";
  opClienteCallback = cargarClientesTab;
  abrirModal("modal-op-cliente");
}

document.getElementById("opon-nuevo-cliente").addEventListener("click", () => {
  opClienteCallback = async (nuevo) => {
    const data = await api("/api/operativo/clientes");
    state.opClientes = data.clientes;
    renderSelectClientesOperativo("opon-cliente-id", nuevo.id);
    document.getElementById("opon-cliente-id").dispatchEvent(new Event("change"));
  };
  abrirNuevoClienteOperativo();
});

// ---- Pestaña Clientes ----

async function cargarClientesTab() {
  try {
    const busqueda = document.getElementById("cli-buscar").value.trim();
    const data = await api(`/api/operativo/clientes${busqueda ? "?q=" + encodeURIComponent(busqueda) : ""}`);
    state.opClientes = data.clientes;
    const tbody = document.getElementById("tbody-clientes");
    if (data.clientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin clientes todavía.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.clientes.map(c => `
      <tr>
        <td>${escapeHtml(c.nombre)}</td>
        <td>${escapeHtml(c.telefono || "—")}</td>
        <td>${escapeHtml(c.email || "—")}</td>
        <td>${escapeHtml(c.rut || "—")}</td>
        <td class="num">${c.n_equipos}</td>
        <td>
          <button type="button" class="btn-link" data-ver-equipos-cliente="${c.id}">Ver equipos</button>
          <button type="button" class="btn-link" data-editar-cliente-tab="${c.id}">Editar</button>
          <button type="button" class="btn-link btn-link--peligro" data-eliminar-cliente="${c.id}">Eliminar</button>
        </td>
      </tr>
    `).join("");
    tbody.querySelectorAll("[data-ver-equipos-cliente]").forEach(btn => {
      btn.addEventListener("click", () => abrirEquiposDeCliente(parseInt(btn.dataset.verEquiposCliente, 10)));
    });
    tbody.querySelectorAll("[data-editar-cliente-tab]").forEach(btn => {
      btn.addEventListener("click", () => abrirEdicionClienteOperativo(parseInt(btn.dataset.editarClienteTab, 10)));
    });
    tbody.querySelectorAll("[data-eliminar-cliente]").forEach(btn => {
      btn.addEventListener("click", () => eliminarClienteTab(parseInt(btn.dataset.eliminarCliente, 10)));
    });
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("cli-buscar").addEventListener("input", () => {
  clearTimeout(document.getElementById("cli-buscar")._t);
  document.getElementById("cli-buscar")._t = setTimeout(cargarClientesTab, 250);
});

document.getElementById("btn-cli-nuevo").addEventListener("click", () => {
  opClienteCallback = cargarClientesTab;
  abrirNuevoClienteOperativo();
});

// Un equipo está "fuera del catálogo" si su marca no está en la lista de
// referencia, o si su modelo no está entre los modelos conocidos de esa
// marca — típicamente porque se escribió a mano con "Otra marca…" / "Otro
// modelo…" al probar el sistema. Si el catálogo todavía no cargó, no se
// marca nada (mejor no mostrar falsos positivos que perder datos reales).
function _equipoFueraDeCatalogo(eq) {
  if (!state.catalogoVehiculos) return false;
  const marca = (eq.marca || "").trim();
  const modelo = (eq.modelo || "").trim();
  if (!marca) return false;
  const marcaCanonica = state.catalogoVehiculos.marcas.find(m => m.toLowerCase() === marca.toLowerCase());
  if (!marcaCanonica) return true;
  if (!modelo) return false;
  const modelosDeMarca = state.catalogoVehiculos.modelos[marcaCanonica] || [];
  return !modelosDeMarca.some(m => m.toLowerCase() === modelo.toLowerCase());
}

document.getElementById("btn-revisar-vehiculos").addEventListener("click", abrirRevisarVehiculos);

async function abrirRevisarVehiculos() {
  const tbody = document.getElementById("tbody-revisar-vehiculos");
  tbody.innerHTML = `<tr><td colspan="7" class="vacio">Cargando…</td></tr>`;
  abrirModal("modal-op-revisar-vehiculos");
  try {
    await cargarCatalogoVehiculos();
    const dataEquipos = await api("/api/operativo/equipos");
    if (!state.opClientes || state.opClientes.length === 0) {
      const dataClientes = await api("/api/operativo/clientes");
      state.opClientes = dataClientes.clientes;
    }
    const clientesPorId = new Map(state.opClientes.map(c => [c.id, c.nombre]));

    const fueraDeCatalogo = dataEquipos.equipos.filter(_equipoFueraDeCatalogo);

    if (fueraDeCatalogo.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="vacio">No se encontró ningún vehículo fuera del catálogo. Todo limpio.</td></tr>`;
      return;
    }

    tbody.innerHTML = fueraDeCatalogo.map(eq => `
      <tr>
        <td>${escapeHtml(eq.identificador || "—")}</td>
        <td>${escapeHtml(eq.marca || "—")}</td>
        <td>${escapeHtml(eq.modelo || "—")}</td>
        <td>${escapeHtml(clientesPorId.get(eq.cliente_id) || "—")}</td>
        <td class="num">${eq.n_ordenes}</td>
        <td class="num">${eq.n_garantias}</td>
        <td><button type="button" class="btn-link" style="color: var(--danger)" data-eliminar-vehiculo-fuera="${eq.id}">Eliminar</button></td>
      </tr>
    `).join("");

    tbody.querySelectorAll("[data-eliminar-vehiculo-fuera]").forEach(btn => {
      btn.addEventListener("click", () => eliminarVehiculoFueraDeCatalogo(parseInt(btn.dataset.eliminarVehiculoFuera, 10)));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Error al cargar: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function eliminarVehiculoFueraDeCatalogo(equipoId) {
  try {
    const data = await api(`/api/operativo/equipos/${equipoId}/historial`);
    const nOrdenes = data.ordenes.length;
    const nGarantias = data.garantias.length;
    let mensaje = "¿Eliminar este vehículo?";
    if (nOrdenes || nGarantias) {
      const partes = [];
      if (nOrdenes) partes.push(`${nOrdenes} orden(es) de trabajo`);
      if (nGarantias) partes.push(`${nGarantias} garantía(s)`);
      mensaje = `Este vehículo tiene ${partes.join(" y ")} asociada(s). Para eliminarlo hay que eliminar todo eso también, y no se puede deshacer. ¿Continuar?`;
    }
    if (!confirm(mensaje)) return;

    for (const orden of data.ordenes) {
      await api(`/api/operativo/ordenes/${orden.id}`, { method: "DELETE" });
    }
    for (const garantia of data.garantias) {
      await api(`/api/operativo/garantias/${garantia.id}`, { method: "DELETE" });
    }
    await api(`/api/operativo/equipos/${equipoId}`, { method: "DELETE" });
    toast("Vehículo eliminado.");
    abrirRevisarVehiculos();
  } catch (e) {
    toast(e.message, true);
  }
}

async function eliminarClienteTab(id) {
  if (!confirm("¿Eliminar este cliente? Solo se puede si no tiene órdenes de trabajo asociadas.")) return;
  try {
    await api(`/api/operativo/clientes/${id}`, { method: "DELETE" });
    toast("Cliente eliminado.");
    cargarClientesTab();
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("form-op-cliente").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("opc-id").value;
  const payload = {
    nombre: document.getElementById("opc-nombre").value.trim(),
    telefono: document.getElementById("opc-telefono").value.trim(),
    email: document.getElementById("opc-email").value.trim(),
    rut: document.getElementById("opc-rut").value.trim(),
    direccion: document.getElementById("opc-direccion").value.trim(),
    notas: document.getElementById("opc-notas").value.trim(),
  };
  try {
    const nuevo = id
      ? await api(`/api/operativo/clientes/${id}`, { method: "PUT", body: JSON.stringify(payload) })
      : await api("/api/operativo/clientes", { method: "POST", body: JSON.stringify(payload) });
    cerrarModal("modal-op-cliente");
    toast("Cliente guardado.");
    if (opClienteCallback) await opClienteCallback(nuevo);
    opClienteCallback = null;
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---- Equipos ----

// Chequeo temprano de bloqueo por vehículo duplicado: se llama apenas se
// selecciona un vehículo (no hay que esperar a enviar el formulario) para
// avisar de inmediato si ya tiene una OT o una garantía abierta, con un
// link para saltar directo a esa OT/garantía. `tipo` es "orden" o
// "garantia" y determina cuál de los dos posibles bloqueos se muestra
// (el mismo criterio que usa el backend para bloquear la creación).
async function chequearBloqueoEquipo(equipoId, tipo, contenedorId) {
  const cont = document.getElementById(contenedorId);
  if (!equipoId) {
    cont.style.display = "none";
    cont.innerHTML = "";
    return;
  }
  let data;
  try {
    data = await api(`/api/operativo/equipos/${equipoId}/bloqueo`);
  } catch (e) {
    // Si falla el chequeo (ej. problema de red) no bloqueamos la UI: el
    // backend igual valida al enviar el formulario.
    cont.style.display = "none";
    cont.innerHTML = "";
    return;
  }
  const bloqueo = tipo === "orden" ? data.ot_abierta : data.garantia_abierta;
  if (!bloqueo) {
    cont.style.display = "none";
    cont.innerHTML = "";
    return;
  }
  const etiqueta = tipo === "orden" ? "OT" : "Garantía";
  const sujeto = tipo === "orden" ? "una OT abierta" : "una garantía abierta";
  const cierre = tipo === "orden" ? "se entregue o se cancele" : "se entregue o se rechace";
  cont.innerHTML =
    `⚠️ Este vehículo ya tiene ${sujeto} (${etiqueta} #${bloqueo.id}, estado: ${escapeHtml(bloqueo.estado_label)}). ` +
    `No se puede crear otra hasta que ${cierre}. ` +
    `<button type="button" class="btn-link" id="${contenedorId}-btn">Ver ${etiqueta} #${bloqueo.id} →</button>`;
  cont.style.display = "";
  document.getElementById(`${contenedorId}-btn`).addEventListener("click", () => {
    if (tipo === "orden") {
      cerrarModal("modal-op-orden-nueva");
      abrirDetalleOrden(bloqueo.id);
    } else {
      cerrarModal("modal-op-garantia-nueva");
      abrirDetalleGarantia(bloqueo.id);
    }
  });
}

async function renderSelectEquiposOperativo(clienteId, valorActual) {
  const select = document.getElementById("opon-equipo-id");
  if (!clienteId) {
    select.innerHTML = '<option value="">Selecciona un vehículo…</option>';
    document.getElementById("opon-ver-historial").style.display = "none";
    chequearBloqueoEquipo(null, "orden", "opon-equipo-bloqueo");
    return;
  }
  const data = await api(`/api/operativo/equipos?cliente_id=${clienteId}`);
  state.opEquiposClienteActual = data.equipos;
  select.innerHTML = '<option value="">Selecciona un vehículo…</option>' +
    data.equipos.map(eq => `<option value="${eq.id}">${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</option>`).join("");
  select.value = valorActual || "";
  // select.value = ... no dispara "change", así que el link de historial y el
  // aviso de bloqueo se actualizan acá también (cubre el caso de equipo
  // recién creado inline).
  document.getElementById("opon-ver-historial").style.display = valorActual ? "" : "none";
  chequearBloqueoEquipo(valorActual || null, "orden", "opon-equipo-bloqueo");
}

document.getElementById("opon-cliente-id").addEventListener("change", (e) => {
  renderSelectEquiposOperativo(e.target.value || null);
  const cliente = state.opClientes.find(c => c.id === parseInt(e.target.value, 10));
  document.getElementById("opon-rut-cliente").value = (cliente && cliente.rut) || "";
});

// Al elegir (o crear) un vehículo, se precargan sus datos persistentes
// (VIN/color/año/transmisión/puertas/motor) para revisarlos o completarlos
// — si el vehículo ya existía, aquí se ven los datos que ya tenía.
function poblarCamposVehiculoNuevaOrden(equipoId) {
  const eq = state.opEquiposClienteActual.find(x => x.id === parseInt(equipoId, 10));
  poblarSelectMarca("opon-marca", "opon-marca-otra", eq ? eq.marca : "");
  poblarSelectModelo("opon-modelo", "opon-modelo-otro", eq ? eq.marca : "", eq ? eq.modelo : "");
  document.getElementById("opon-vin").value = eq ? (eq.vin || "") : "";
  document.getElementById("opon-color").value = eq ? (eq.color || "") : "";
  document.getElementById("opon-anio").value = eq && eq.anio != null ? eq.anio : "";
  document.getElementById("opon-transmision").value = eq ? (eq.modelo_transmision || "") : "";
  document.getElementById("opon-puertas").value = eq ? (eq.puertas || "") : "";
  document.getElementById("opon-motor-cc").value = eq ? (eq.motor_cc || "") : "";
}

document.getElementById("opon-equipo-id").addEventListener("change", (e) => {
  document.getElementById("opon-ver-historial").style.display = e.target.value ? "" : "none";
  poblarCamposVehiculoNuevaOrden(e.target.value);
  chequearBloqueoEquipo(e.target.value || null, "orden", "opon-equipo-bloqueo");
});

document.getElementById("opon-ver-historial").addEventListener("click", () => {
  const equipoId = document.getElementById("opon-equipo-id").value;
  if (equipoId) abrirHistorialEquipo(parseInt(equipoId, 10));
});

let opEquipoCallback = null; // se llama con (nuevoEquipo, payload) tras guardar, según de dónde se abrió el modal

function renderSelectClienteEquipoModal(valorActual) {
  const select = document.getElementById("ope-cliente-id");
  select.innerHTML = '<option value="">Selecciona un cliente…</option>' +
    state.opClientes.map(c => `<option value="${c.id}">${escapeHtml(c.nombre)}</option>`).join("");
  select.value = valorActual || "";
}

document.getElementById("opon-nuevo-equipo").addEventListener("click", () => {
  const clienteId = document.getElementById("opon-cliente-id").value;
  if (!clienteId) {
    toast("Primero selecciona (o crea) un cliente.", true);
    return;
  }
  document.getElementById("form-op-equipo").reset();
  document.getElementById("ope-patente-encontrada").style.display = "none";
  document.getElementById("ope-id").value = "";
  poblarSelectMarca("ope-marca", "ope-marca-otra", "");
  poblarSelectModelo("ope-modelo", "ope-modelo-otro", "", "");
  renderSelectClienteEquipoModal(clienteId);
  document.getElementById("modal-op-equipo-titulo").textContent = "Nuevo equipo";
  opEquipoCallback = async (nuevo, payload) => {
    await renderSelectEquiposOperativo(payload.cliente_id, nuevo.id);
    poblarCamposVehiculoNuevaOrden(nuevo.id);
  };
  abrirModal("modal-op-equipo");
});

// No existe ninguna base de datos (ni gratis ni confiable) que relacione una
// patente con la marca/modelo del vehículo en Chile — se evaluó y no se
// integró ningún servicio externo de pago para eso. Lo que sí tenemos ahora
// es un catálogo confiable de marcas y modelos (ver cargarCatalogoVehiculos)
// que precarga las opciones de los campos Marca/Modelo. Además, con nuestros
// propios datos: si esa patente ya está registrada en el taller (aunque haya
// sido con otro cliente, ej. el auto cambió de dueño), autocompletamos
// marca/modelo/color/año/etc. desde ese registro anterior.
document.getElementById("ope-identificador").addEventListener("blur", async (e) => {
  if (document.getElementById("ope-id").value) return; // editando un equipo existente: no tocar
  const patente = e.target.value.trim();
  const aviso = document.getElementById("ope-patente-encontrada");
  if (!patente) {
    aviso.style.display = "none";
    return;
  }
  try {
    const data = await api(`/api/operativo/equipos?identificador=${encodeURIComponent(patente)}`);
    if (data.equipos.length === 0) {
      aviso.style.display = "none";
      return;
    }
    const encontrado = data.equipos[0];
    const marcaActual = valorSeleccionUOtro("ope-marca", "ope-marca-otra", VALOR_OTRA_MARCA);
    if (!marcaActual) {
      poblarSelectMarca("ope-marca", "ope-marca-otra", encontrado.marca || "");
      poblarSelectModelo("ope-modelo", "ope-modelo-otro", encontrado.marca || "", encontrado.modelo || "");
    }
    aviso.textContent = `Esta patente ya está registrada (${[encontrado.marca, encontrado.modelo].filter(Boolean).join(" ") || "sin marca/modelo"}) — se completaron marca y modelo.`;
    aviso.style.display = "";
  } catch (e2) {
    // silencioso: esto es solo una ayuda, no debe bloquear el guardado del equipo
  }
});

document.getElementById("form-op-equipo").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("ope-id").value;
  const clienteId = document.getElementById("ope-cliente-id").value;
  if (!clienteId) {
    toast("Selecciona un cliente.", true);
    return;
  }
  const payload = {
    cliente_id: clienteId,
    identificador: document.getElementById("ope-identificador").value.trim(),
    marca: valorSeleccionUOtro("ope-marca", "ope-marca-otra", VALOR_OTRA_MARCA),
    modelo: valorSeleccionUOtro("ope-modelo", "ope-modelo-otro", VALOR_OTRO_MODELO),
    notas: document.getElementById("ope-notas").value.trim(),
  };
  try {
    const nuevo = id
      ? await api(`/api/operativo/equipos/${id}`, { method: "PUT", body: JSON.stringify(payload) })
      : await api("/api/operativo/equipos", { method: "POST", body: JSON.stringify(payload) });
    cerrarModal("modal-op-equipo");
    toast("Equipo guardado.");
    if (opEquipoCallback) await opEquipoCallback(nuevo, payload);
    opEquipoCallback = null;
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---- Pestaña Reportes ----

function rangoFechasParamsOperativo() {
  const desde = document.getElementById("rep-ordenes-desde").value;
  const hasta = document.getElementById("rep-ordenes-hasta").value;
  const params = new URLSearchParams();
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);
  return params.toString();
}

document.getElementById("btn-rep-ordenes-excel").addEventListener("click", () => {
  window.location.href = `/api/operativo/reportes/ordenes?${rangoFechasParamsOperativo()}`;
});
document.getElementById("btn-rep-ordenes-pdf").addEventListener("click", () => {
  window.location.href = `/api/operativo/reportes/ordenes/pdf?${rangoFechasParamsOperativo()}`;
});

// Preview con contadores: se muestra cuántas órdenes/garantías trae cada
// reporte ANTES de descargarlo, igual que en Logística — se recalcula al
// entrar a la pestaña y cada vez que cambia un rango de fecha.
async function cargarResumenReportesOperativo() {
  const params = new URLSearchParams();
  const ordenesDesde = document.getElementById("rep-ordenes-desde").value;
  const ordenesHasta = document.getElementById("rep-ordenes-hasta").value;
  if (ordenesDesde) params.set("ordenes_desde", ordenesDesde);
  if (ordenesHasta) params.set("ordenes_hasta", ordenesHasta);
  const garantiasDesde = document.getElementById("rep-garantias-desde").value;
  const garantiasHasta = document.getElementById("rep-garantias-hasta").value;
  if (garantiasDesde) params.set("garantias_desde", garantiasDesde);
  if (garantiasHasta) params.set("garantias_hasta", garantiasHasta);

  try {
    const r = await api(`/api/operativo/reportes/resumen?${params.toString()}`);
    document.getElementById("rep-contador-ordenes").textContent =
      `${r.ordenes.total} orden(es) · total facturado ${fmtMoney(r.ordenes.total_facturado)}`;
    document.getElementById("rep-contador-garantias").textContent =
      `${r.garantias.total} garantía(s) en el rango seleccionado`;
  } catch (e) {
    document.getElementById("rep-contador-ordenes").textContent = "No se pudo calcular el resumen.";
    document.getElementById("rep-contador-garantias").textContent = "No se pudo calcular el resumen.";
  }
}

document.getElementById("rep-ordenes-desde").addEventListener("change", cargarResumenReportesOperativo);
document.getElementById("rep-ordenes-hasta").addEventListener("change", cargarResumenReportesOperativo);
document.getElementById("rep-garantias-desde").addEventListener("change", cargarResumenReportesOperativo);
document.getElementById("rep-garantias-hasta").addEventListener("change", cargarResumenReportesOperativo);

// ---- Nueva orden — el ingreso completo del auto (recepción + checklist de
// equipamiento) se hace acá, al momento de crear la orden, tal como se hace
// una sola vez cuando el vehículo entra al taller. ----

document.getElementById("btn-op-nueva-orden").addEventListener("click", async () => {
  document.getElementById("form-op-orden-nueva").reset();
  renderSelectClientesOperativo("opon-cliente-id", "");
  renderSelectEquiposOperativo(null);
  document.getElementById("opon-ver-historial").style.display = "none";
  document.getElementById("opon-rut-cliente").value = "";
  poblarCamposVehiculoNuevaOrden(null);
  document.getElementById("opon-encargado").value = state.sesion.nombre || "";
  document.getElementById("opon-estanque").value = 0;
  document.getElementById("opon-estanque-valor").textContent = "0 / 8";
  if (state.opOrdenes.length === 0) {
    // acá se usa para poblar autocompletados (ej. buscar cliente por
    // historial), no para pintar el tablero -- por eso se pide el
    // historial completo y no se acota al mes en curso.
    const dataOrdenes = await api("/api/operativo/ordenes?mes=todos");
    state.opOrdenes = dataOrdenes.ordenes;
  }
  poblarDatalistsOperativo();
  poblarSelectTrabajo("");
  renderEquipamientoChecklist("op-equipamiento-grid-nueva", null, { lectura: false });
  abrirModal("modal-op-orden-nueva");
});

document.getElementById("opon-estanque").addEventListener("input", (e) => {
  document.getElementById("opon-estanque-valor").textContent = `${e.target.value} / 8`;
});

document.getElementById("form-op-orden-nueva").addEventListener("submit", async (e) => {
  e.preventDefault();
  const clienteId = document.getElementById("opon-cliente-id").value;
  const equipoId = document.getElementById("opon-equipo-id").value || null;
  if (!clienteId) {
    toast("Selecciona un cliente.", true);
    return;
  }
  if (!equipoId) {
    toast("Selecciona o crea el vehículo que ingresa al taller.", true);
    return;
  }

  const equipamiento = {};
  document.querySelectorAll('#op-equipamiento-grid-nueva [data-equipamiento-item]').forEach(chk => {
    equipamiento[chk.dataset.equipamientoItem] = chk.checked;
  });

  try {
    await api(`/api/operativo/equipos/${equipoId}`, {
      method: "PUT",
      body: JSON.stringify({
        marca: valorSeleccionUOtro("opon-marca", "opon-marca-otra", VALOR_OTRA_MARCA),
        modelo: valorSeleccionUOtro("opon-modelo", "opon-modelo-otro", VALOR_OTRO_MODELO),
        vin: document.getElementById("opon-vin").value.trim(),
        color: document.getElementById("opon-color").value.trim(),
        anio: document.getElementById("opon-anio").value || null,
        modelo_transmision: document.getElementById("opon-transmision").value.trim(),
        puertas: document.getElementById("opon-puertas").value,
        motor_cc: document.getElementById("opon-motor-cc").value.trim(),
      }),
    });
    const nueva = await api("/api/operativo/ordenes", {
      method: "POST",
      body: JSON.stringify({
        cliente_id: clienteId,
        equipo_id: equipoId,
        prioridad: document.getElementById("opon-prioridad").value,
        descripcion_problema: document.getElementById("opon-descripcion").value.trim(),
        kilometraje: document.getElementById("opon-kilometraje").value || null,
      }),
    });
    await api(`/api/operativo/ordenes/${nueva.id}`, {
      method: "PUT",
      body: JSON.stringify({
        estanque_nivel: document.getElementById("opon-estanque").value,
        referencia: document.getElementById("opon-referencia").value.trim(),
        trabajo_a_realizar: document.getElementById("opon-trabajo").value.trim(),
        // El precio ya no se pide al crear la orden — se define después,
        // desde la pestaña de la OT (campo "Valor"), cuando ya se sabe
        // cuánto se le va a cobrar al cliente.
        equipamiento,
      }),
    });
    cerrarModal("modal-op-orden-nueva");
    toast("Orden creada.");
    await cargarOperativo();
    // Por obligación, al ingresar el auto se piden 7 fotos fijas antes de
    // enviarle al cliente el registro fotográfico -- ver spec del usuario.
    // Se puede cerrar y retomar después desde la pestaña "Fotos" de la OT.
    abrirFotosIngreso("orden", nueva.id);
  } catch (e2) {
    toast(e2.message, true);
    // Puede pasar que el bloqueo haya aparecido justo entre que se
    // seleccionó el vehículo y se envió el formulario (ej. otra persona
    // creó una OT para el mismo auto en ese momento) — se refresca el
    // aviso para que quede visible con su link, no solo el toast.
    chequearBloqueoEquipo(equipoId, "orden", "opon-equipo-bloqueo");
  }
});

// ---------------------------------------------------------------------------
// Detalle de orden: SOLO resumen de cliente/auto + botones de acceso.
// ---------------------------------------------------------------------------

// La pestaña "OT" ya no repite los datos de cliente/vehículo (viven
// completos en "Recepción", con botón de editar) — acá solo se arma el
// título compacto de la ventana ("Orden #7956 — José Alegría · AB1234")
// para poder identificar de un vistazo qué orden se está viendo, y se deja
// listo el botón de "copiar link de seguimiento" (que sí necesita la
// patente aunque no se muestre el bloque completo del vehículo).
function renderResumenClienteAuto(orden) {
  const cliente = orden.cliente;
  const equipo = orden.equipo;

  const partesTitulo = [`Orden #${orden.id}`];
  const identificacion = [cliente ? cliente.nombre : "", equipo ? equipo.identificador : ""]
    .filter(Boolean).join(" · ");
  if (identificacion) partesTitulo.push(identificacion);
  document.getElementById("opod-titulo").textContent = partesTitulo.join(" — ");

  const btnSeguimiento = document.getElementById("btn-opod-copiar-seguimiento");
  const patente = equipo ? equipo.identificador : "";
  btnSeguimiento.disabled = !patente;
  btnSeguimiento.dataset.patente = patente || "";
}

// El botón "Copiar link de seguimiento" antes dependía por completo de
// navigator.clipboard.writeText(): si el navegador no daba permiso (algo
// bastante común fuera de un contexto de prueba — móviles, políticas de
// privacidad, sitios sin el permiso "clipboard-write" otorgado), el único
// respaldo era mostrar el link en un toast que desaparece solo a los 3
// segundos, sin forma real de seleccionarlo ni copiarlo a tiempo — para el
// usuario, el botón simplemente "no servía". Ahora se abre un modal que
// se queda abierto con el link en un campo de texto seleccionable, y de
// paso se intenta copiar solo al portapapeles (si el navegador lo permite,
// mejor: ya queda copiado al abrir el modal).
async function mostrarLinkSeguimiento(patente) {
  const link = `${window.location.origin}/seguimiento?patente=${encodeURIComponent(patente)}`;
  const input = document.getElementById("op-link-seguimiento-valor");
  const estado = document.getElementById("op-link-seguimiento-estado");
  input.value = link;
  estado.textContent = "";
  abrirModal("modal-op-link-seguimiento");
  input.focus();
  input.select();
  try {
    await navigator.clipboard.writeText(link);
    estado.textContent = "Ya se copió automáticamente al portapapeles.";
  } catch (err) {
    estado.textContent = "Selecciona el link de arriba y cópialo con Ctrl+C (o mantén presionado y elige \"Copiar\" en el celular).";
  }
}

document.getElementById("btn-opod-copiar-seguimiento").addEventListener("click", (e) => {
  const patente = e.currentTarget.dataset.patente;
  if (!patente) {
    toast("Esta orden no tiene una patente asociada.", true);
    return;
  }
  mostrarLinkSeguimiento(patente);
});

// Vista imprimible: documento oficial que se le entrega al cliente (datos
// de recepción + valores, SIN costos internos ni utilidad — ver
// orden_imprimible_pdf en modulos/operativo.py).
document.getElementById("btn-opod-orden-imprimible").addEventListener("click", () => {
  const ordenId = document.getElementById("opod-id").value;
  window.open(`/api/operativo/ordenes/${ordenId}/orden-imprimible/pdf`, "_blank");
});

document.getElementById("btn-op-link-seguimiento-copiar").addEventListener("click", async () => {
  const input = document.getElementById("op-link-seguimiento-valor");
  const estado = document.getElementById("op-link-seguimiento-estado");
  input.focus();
  input.select();
  try {
    await navigator.clipboard.writeText(input.value);
    estado.textContent = "Copiado al portapapeles.";
  } catch (err) {
    estado.textContent = "No se pudo copiar automáticamente — el texto ya está seleccionado, usa Ctrl+C.";
  }
});

// Elimina la orden completa (no solo un ítem suelto). Antes no existía
// forma de hacer esto desde la interfaz — hacía falta para poder limpiar
// órdenes de prueba. El backend devuelve el stock de los repuestos
// cargados y desvincula (sin borrar) cualquier garantía que la
// referenciara solo para trazabilidad.
document.getElementById("btn-opod-eliminar-orden").addEventListener("click", async () => {
  const id = document.getElementById("opod-id").value;
  if (!id) return;
  if (!confirm(`¿Eliminar la orden #${id} por completo? Esta acción no se puede deshacer. Se devolverá al inventario el stock de los repuestos que tenga cargados.`)) return;
  try {
    await api(`/api/operativo/ordenes/${id}`, { method: "DELETE" });
    toast("Orden eliminada.");
    cerrarModal("modal-op-orden-detalle");
    await cargarOperativo();
  } catch (e) {
    toast(e.message, true);
  }
});

// Pinta el recuadro de valor/pagado-saldo y la tabla de pagos de la OT
// abierta en el hub. Se llama al abrir la orden y después de cada
// acción que pueda cambiar el valor o los pagos (guardar cambios,
// registrar/eliminar un pago, refrescos automáticos de estado).
function renderValorYPagosOrden(orden) {
  document.getElementById("opod-valor").value = formatearMontoEnVivo(String(orden.valor_neto != null ? orden.valor_neto : 0));
  document.getElementById("opod-iva-check").checked = !!orden.precio_iva;
  document.getElementById("opod-r-neto").textContent = fmtMoney(orden.valor_neto || 0);
  document.getElementById("opod-r-iva").textContent = fmtMoney(orden.valor_iva_monto || 0);
  document.getElementById("opod-r-total").textContent = fmtMoney(orden.valor_total || 0);
  document.getElementById("opod-r-pagado").textContent = fmtMoney(orden.total_pagado || 0);
  document.getElementById("opod-r-por-pagar").textContent = fmtMoney(orden.total_por_pagar || 0);

  const tbody = document.getElementById("tbody-opod-pagos");
  const pagos = orden.pagos || [];
  if (pagos.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Sin pagos registrados.</td></tr>`;
  } else {
    tbody.innerHTML = pagos.map((p, idx) => `
      <tr>
        <td>${idx + 1}</td>
        <td>${fmtFechaSolo((p.fecha || "").slice(0, 10))}</td>
        <td>${escapeHtml(METODOS_PAGO_LABEL[p.metodo_pago] || p.metodo_pago)}</td>
        <td class="num">${fmtMoney(p.monto)}</td>
        <td><button type="button" class="btn-link btn-link--peligro" data-eliminar-pago="${p.id}">Eliminar</button></td>
      </tr>
    `).join("");
    tbody.querySelectorAll("[data-eliminar-pago]").forEach(btn => {
      btn.addEventListener("click", () => eliminarPagoOrden(parseInt(btn.dataset.eliminarPago, 10)));
    });
  }
}

// Pestaña "Costos": todos los ítems (repuestos y mano de obra) cargados a
// la orden, con el resumen de plata y la utilidad estimada.
const OPOI_TIPO_LABEL = { repuesto: "Repuesto", mano_obra: "Mano de obra", convertidor_torque: "Convertidor de torque" };

function renderCostosOrden(orden) {
  const tbody = document.getElementById("tbody-opod-costos-items");
  const items = orden.items || [];
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Sin ítems cargados.</td></tr>`;
  } else {
    tbody.innerHTML = items.map(it => `
      <tr>
        <td>${escapeHtml(OPOI_TIPO_LABEL[it.tipo] || it.tipo)}${it.aplica_iva ? ' <span style="font-size:11px; color:var(--accent-2); border:1px solid var(--accent-2); border-radius:4px; padding:1px 4px;">+IVA</span>' : ""}</td>
        <td>${escapeHtml(it.descripcion)}${it.detalle ? `<br><span class="ayuda-inline" style="margin:0">${escapeHtml(it.detalle)}</span>` : ""}${it.tipo === "convertidor_torque" && it.convertidor_id ? `<br><a href="/convertidores?ver=${it.convertidor_id}#convertidores" class="ayuda-inline">Ver convertidor #${it.convertidor_id}</a>` : ""}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.subtotal !== undefined ? it.subtotal : it.cantidad * it.costo_unitario)}</td>
      </tr>
    `).join("");
  }
  const tbodyCC = document.getElementById("tbody-opod-costos-caja-chica");
  const boletasCC = orden.costos_caja_chica || [];
  if (boletasCC.length === 0) {
    tbodyCC.innerHTML = `<tr><td colspan="4" class="vacio">Sin gastos de caja chica asociados.</td></tr>`;
  } else {
    tbodyCC.innerHTML = boletasCC.map(b => `
      <tr>
        <td>${fmtFechaSolo((b.fecha_retiro || "").slice(0, 10))}</td>
        <td>${escapeHtml(b.numero_documento || "—")}</td>
        <td>${escapeHtml(b.descripcion || "—")}</td>
        <td class="num">${fmtMoney(b.monto)}</td>
      </tr>
    `).join("");
  }

  document.getElementById("opod-c-costo-repuestos").textContent = fmtMoney(orden.costo_repuestos || 0);
  document.getElementById("opod-c-mano-obra").textContent = fmtMoney(orden.mano_obra_cargada || 0);
  document.getElementById("opod-c-costo-convertidores").textContent = fmtMoney(orden.costo_convertidores || 0);
  document.getElementById("opod-c-costo-caja-chica").textContent = fmtMoney(orden.costo_caja_chica || 0);
  document.getElementById("opod-c-neto").textContent = fmtMoney(orden.valor_neto || 0);
  document.getElementById("opod-c-total").textContent = fmtMoney(orden.valor_total || 0);
  document.getElementById("opod-c-pagado").textContent = fmtMoney(orden.total_pagado || 0);
  document.getElementById("opod-c-por-pagar").textContent = fmtMoney(orden.total_por_pagar || 0);
  document.getElementById("opod-c-utilidad").textContent = fmtMoney(orden.utilidad_estimada || 0);
}

// Pestañas dentro de la ventana de la orden: "OT", "Costos", y lo que antes
// eran botones que abrían un modal aparte (Recepción, Diagnóstico,
// Presupuesto, Carga de repuestos, Pruebas de ruta) — ahora todo vive en la
// misma ventana. Cada pestaña "carga" (re-fetch + render) su contenido recién
// al hacer clic, igual que antes hacían los botones al abrir su modal.
const OPOD_TAB_LOADERS = {
  recepcion: () => abrirRecepcionLectura(),
  fotos: () => cargarFotosOrdenTab(),
  diagnostico: () => abrirDiagnostico(),
  presupuesto: () => abrirPresupuestoModal(),
  repuestos: () => abrirRepuestosModal(),
  qc: () => abrirQcModal(),
  factura: () => cargarFacturaTab(),
};

// Pestaña "Factura" dentro del detalle de la OT: si la orden ya tiene una
// factura electrónica emitida (buscada por orden_id en el módulo de
// Facturación), se muestra la vista previa con opciones de ver/descargar;
// si no, un botón directo a Facturación electrónica con la OT prellenada
// (ver manejo de ?orden= en finanzas.js).
async function cargarFacturaTab() {
  const ordenId = document.getElementById("opod-id").value;
  const bloqueSinEmitir = document.getElementById("opod-factura-sin-emitir");
  const bloqueEmitida = document.getElementById("opod-factura-emitida");
  bloqueSinEmitir.style.display = "none";
  bloqueEmitida.style.display = "none";
  try {
    const data = await api(`/api/facturacion/documentos?orden_id=${encodeURIComponent(ordenId)}`);
    const documento = (data.documentos || []).find(d => d.estado === "emitido" || d.estado === "anulado");
    if (!documento) {
      document.getElementById("opod-factura-btn-ir").href = `/finanzas?orden=${encodeURIComponent(ordenId)}#dte`;
      bloqueSinEmitir.style.display = "block";
      return;
    }
    const pdfUrlVer = `/api/facturacion/documentos/${documento.id}/pdf`;
    const pdfUrlDescargar = `/api/facturacion/documentos/${documento.id}/pdf?descargar=1`;
    document.getElementById("opod-factura-resumen").textContent =
      `${documento.tipo_dte_label} N° ${documento.folio ?? "—"} — ${documento.receptor_razon_social} — ${fmtMoney(documento.monto_total)}` +
      (documento.estado === "anulado" ? " (anulada por nota de crédito)" : "");
    document.getElementById("opod-factura-btn-ver-pdf").href = pdfUrlVer;
    document.getElementById("opod-factura-btn-descargar-pdf").href = pdfUrlDescargar;
    document.getElementById("opod-factura-pdf-preview").src = pdfUrlVer;
    document.getElementById("opod-factura-pdf-preview-ampliar").href = pdfUrlVer;
    bloqueEmitida.style.display = "block";
  } catch (e) {
    document.getElementById("opod-factura-btn-ir").href = `/finanzas?orden=${encodeURIComponent(ordenId)}#dte`;
    bloqueSinEmitir.style.display = "block";
  }
}

function activarTabOpod(nombre) {
  document.querySelectorAll("#modal-op-orden-detalle .op-tab").forEach(t => {
    t.classList.toggle("activo", t.dataset.opodTab === nombre);
  });
  document.querySelectorAll("#modal-op-orden-detalle .op-tab-panel").forEach(panel => {
    panel.style.display = panel.id === `opod-panel-${nombre}` ? "" : "none";
  });
}

document.querySelectorAll("#modal-op-orden-detalle .op-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const nombre = tab.dataset.opodTab;
    activarTabOpod(nombre);
    if (OPOD_TAB_LOADERS[nombre]) OPOD_TAB_LOADERS[nombre]();
  });
});

function activarTabOtPorDefecto() {
  activarTabOpod("ot");
}

async function abrirDetalleOrden(id) {
  try {
    const orden = await api(`/api/operativo/ordenes/${id}`);
    state.opOrdenActivaId = id;
    document.getElementById("opod-id").value = id;
    document.getElementById("opod-titulo").textContent = `Orden #${id}`;
    activarTabOtPorDefecto();
    renderResumenClienteAuto(orden);

    document.getElementById("opod-estado").value = orden.estado;
    document.getElementById("opod-prioridad").value = orden.prioridad || "media";
    poblarSelectTrabajo(orden.trabajo_a_realizar || "", "opod-trabajo");
    document.getElementById("opod-descripcion").value = orden.descripcion_problema || "";
    renderValorYPagosOrden(orden);
    renderCostosOrden(orden);

    const btnHistorial = document.getElementById("btn-opod-ver-historial");
    if (orden.equipo) {
      btnHistorial.style.display = "";
      btnHistorial.onclick = () => abrirHistorialEquipo(orden.equipo.id);
    } else {
      btnHistorial.style.display = "none";
    }

    abrirModal("modal-op-orden-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

// Refresca solo el resumen/estado de la OT ya abierta (sin volver a abrir el
// modal), para reflejar transiciones automáticas de estado que ocurren desde
// un sub-modal (ej. aprobar presupuesto -> Reparación; QC aprobado -> Listo).
async function refrescarResumenHub(ordenId) {
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderResumenClienteAuto(orden);
    document.getElementById("opod-estado").value = orden.estado;
    renderValorYPagosOrden(orden);
    renderCostosOrden(orden);
    cargarOperativo();
  } catch (e) {
    // silencioso: esto es un refresco de fondo, no una acción explícita del usuario
  }
}

document.getElementById("btn-opod-guardar").addEventListener("click", async () => {
  const id = document.getElementById("opod-id").value;
  const payload = {
    estado: document.getElementById("opod-estado").value,
    prioridad: document.getElementById("opod-prioridad").value,
    trabajo_a_realizar: document.getElementById("opod-trabajo").value,
    descripcion_problema: document.getElementById("opod-descripcion").value.trim(),
    precio_referencial: limpiarMonto(document.getElementById("opod-valor").value) || 0,
    precio_iva: document.getElementById("opod-iva-check").checked,
  };
  try {
    const actualizada = await api(`/api/operativo/ordenes/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Orden actualizada.");
    renderResumenClienteAuto(actualizada);
    renderValorYPagosOrden(actualizada);
    renderCostosOrden(actualizada);
    cargarOperativo();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---- Pagos de la OT ----

const METODOS_PAGO_LABEL = {
  efectivo: "Efectivo", tarjeta: "Tarjeta", transferencia: "Transferencia",
  cheque: "Cheque", otro: "Otro",
};

document.getElementById("btn-opod-nuevo-pago").addEventListener("click", () => {
  const ordenId = document.getElementById("opod-id").value;
  document.getElementById("form-op-pago-nuevo").reset();
  document.getElementById("opag-orden-id").value = ordenId;
  document.getElementById("opag-fecha").value = new Date().toISOString().slice(0, 10);
  abrirModal("modal-op-pago-nuevo");
});

document.getElementById("form-op-pago-nuevo").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("opag-orden-id").value;
  const payload = {
    metodo_pago: document.getElementById("opag-metodo").value,
    monto: limpiarMonto(document.getElementById("opag-monto").value),
    fecha: document.getElementById("opag-fecha").value,
  };
  try {
    await api(`/api/operativo/ordenes/${ordenId}/pagos`, { method: "POST", body: JSON.stringify(payload) });
    cerrarModal("modal-op-pago-nuevo");
    toast("Pago registrado.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderValorYPagosOrden(orden);
    renderCostosOrden(orden);
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarPagoOrden(pagoId) {
  const ordenId = document.getElementById("opod-id").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/pagos/${pagoId}`, { method: "DELETE" });
    toast("Pago eliminado.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderValorYPagosOrden(orden);
    renderCostosOrden(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Recepción — datos del vehículo + checklist de equipamiento
// ---------------------------------------------------------------------------

const EQUIPAMIENTO_ITEMS = [
  "Encendedor", "Cenicero", "Gata", "Tapa Combustible", "Padron", "Tag",
  "Gancho Tiro", "Llave Rueda", "Radio", "Neumatico Repuesto", "Conos",
  "Extintor", "P. Circulacion", "Revision Tecnica", "S.O.A.P", "Certificado Gases",
];

// containerId: id del contenedor donde se pinta el checklist.
// opts.lectura=true -> solo muestra marcado/no marcado, sin checkboxes (para
// el visor de Recepción). opts.lectura=false -> checkboxes editables (para
// Nueva Orden, que es donde se completa una única vez al ingresar el auto).
function renderEquipamientoChecklist(containerId, equipamiento, opts) {
  const eq = equipamiento || {};
  const lectura = !!(opts && opts.lectura);
  const cont = document.getElementById(containerId);
  if (lectura) {
    cont.innerHTML = EQUIPAMIENTO_ITEMS.map(item => `
      <div class="op-equipamiento-item op-equipamiento-item--lectura ${eq[item] ? "marcado" : ""}">
        <span>${eq[item] ? "✓" : "—"}</span> ${escapeHtml(item)}
      </div>
    `).join("");
  } else {
    cont.innerHTML = EQUIPAMIENTO_ITEMS.map(item => `
      <label class="op-equipamiento-item">
        <input type="checkbox" data-equipamiento-item="${escapeHtml(item)}" ${eq[item] ? "checked" : ""}>
        <span>${escapeHtml(item)}</span>
      </label>
    `).join("");
  }
}

// ---- Recepción: SOLO LECTURA — se completó una vez, al crear la orden ----

async function abrirRecepcionLectura() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    const equipo = orden.equipo || {};
    const cliente = orden.cliente || {};
    const estanque = orden.estanque_nivel;

    // Se guarda el equipo tal cual llegó de la API (con su id) para poder
    // prellenar el modal de "Editar datos del vehículo" sin tener que
    // volver a pedirlo — el botón de editar vive en esta misma pestaña.
    state.opRecepcionEquipoActual = equipo.id ? equipo : null;

    const filasVehiculo = [
      ["VIN", equipo.vin || "—"],
      ["Patente", equipo.identificador || "—"],
      ["Marca", equipo.marca || "—"],
      ["Modelo", equipo.modelo || "—"],
      ["Color", equipo.color || "—"],
      ["Año", equipo.anio || "—"],
      ["Modelo de Transmisión", equipo.modelo_transmision || "—"],
      ["Motor (cc)", equipo.motor_cc || "—"],
    ];
    const filasRecepcion = [
      ["RUT Cliente", cliente.rut || "—"],
      ["Dirección Cliente", cliente.direccion || "—"],
      ["Encargado", orden.recepcionado_por || "—"],
      ["Kilometraje", orden.kilometraje != null ? orden.kilometraje : "—"],
      ["Estanque", estanque != null ? `${estanque} / 8` : "—"],
      ["Referencia", orden.referencia || "—"],
      ["Precio", orden.precio_referencial != null ? fmtMoney(orden.precio_referencial) : "—"],
      ["Trabajo a Realizar", orden.trabajo_a_realizar || "—"],
      ["Comentarios", orden.descripcion_problema || "—"],
    ];

    const bloque = (titulo, filas) => `
      <div class="op-resumen-bloque">
        <h4>${titulo}</h4>
        ${filas.map(([k, v]) => `<div class="op-resumen-fila"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join("")}
      </div>`;
    document.getElementById("op-recepcion-lectura").innerHTML = bloque("Vehículo", filasVehiculo) + bloque("Recepción", filasRecepcion);

    renderEquipamientoChecklist("op-equipamiento-grid-lectura", orden.equipamiento, { lectura: true });
  } catch (e) {
    toast(e.message, true);
  }
}

// Corrección de datos del vehículo ya ingresado (ej. el color o el VIN se
// escribieron mal en la recepción). Se abre desde el botón "✏ Editar datos
// del vehículo" dentro de la pestaña Recepción, y usa el mismo PUT
// /api/operativo/equipos/<id> que ya existe (el que llena estos datos al
// crear la orden) — acá solo se reutiliza para poder corregirlos después.
document.getElementById("btn-oprl-editar-vehiculo").addEventListener("click", () => {
  const equipo = state.opRecepcionEquipoActual;
  if (!equipo) {
    toast("Esta orden no tiene un vehículo asociado.", true);
    return;
  }
  document.getElementById("opee-id").value = equipo.id;
  document.getElementById("opee-identificador").value = equipo.identificador || "";
  document.getElementById("opee-vin").value = equipo.vin || "";
  document.getElementById("opee-marca").value = equipo.marca || "";
  document.getElementById("opee-modelo").value = equipo.modelo || "";
  document.getElementById("opee-color").value = equipo.color || "";
  document.getElementById("opee-anio").value = equipo.anio != null ? equipo.anio : "";
  document.getElementById("opee-transmision").value = equipo.modelo_transmision || "";
  document.getElementById("opee-puertas").value = equipo.puertas || "";
  document.getElementById("opee-motor-cc").value = equipo.motor_cc || "";
  abrirModal("modal-op-equipo-editar");
});

document.getElementById("form-op-equipo-editar").addEventListener("submit", async (e) => {
  e.preventDefault();
  const equipoId = document.getElementById("opee-id").value;
  try {
    await api(`/api/operativo/equipos/${equipoId}`, {
      method: "PUT",
      body: JSON.stringify({
        identificador: document.getElementById("opee-identificador").value.trim(),
        vin: document.getElementById("opee-vin").value.trim(),
        marca: document.getElementById("opee-marca").value.trim(),
        modelo: document.getElementById("opee-modelo").value.trim(),
        color: document.getElementById("opee-color").value.trim(),
        anio: document.getElementById("opee-anio").value || null,
        modelo_transmision: document.getElementById("opee-transmision").value.trim(),
        puertas: document.getElementById("opee-puertas").value,
        motor_cc: document.getElementById("opee-motor-cc").value.trim(),
      }),
    });
    cerrarModal("modal-op-equipo-editar");
    toast("Datos del vehículo actualizados.");
    await abrirRecepcionLectura();
    // El kanban y la tarjeta muestran año/color/patente — hay que refrescar
    // para que la corrección se vea también ahí, no solo en Recepción.
    await cargarOperativo();
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---------------------------------------------------------------------------
// Fotos de recepción del vehículo
// ---------------------------------------------------------------------------
// Al crear una orden se abre automáticamente modal-op-fotos-ingreso pidiendo
// las 7 fotos obligatorias (FOTOS_CATEGORIAS_OBLIGATORIAS). Se puede cerrar y
// retomar después desde la pestaña "Fotos" de la OT (misma lógica de
// render/subida, reutilizada en los dos lugares).

const FOTOS_CATEGORIAS_OBLIGATORIAS = [
  ["frente", "Frente"],
  ["costado_derecho", "Costado derecho"],
  ["costado_izquierdo", "Costado izquierdo"],
  ["atras", "Atrás"],
  ["parabrisas", "Parabrisas"],
  ["tablero", "Tablero"],
  ["vin", "VIN"],
];

// categoría pendiente de subir con el próximo archivo elegido -- se setea
// justo antes de disparar el click() del <input type=file> oculto
// correspondiente, porque un solo input se reutiliza para todas las
// categorías (evita crear un <input> nuevo por cada tile).
let _opFotosCategoriaPendiente = null;
let _opFotosTituloPendiente = "";

function _renderFotosTile(categoria, label, foto) {
  if (foto) {
    // El responsable (quién la tomó/mandó) va siempre visible -- si llega
    // por WhatsApp es el mecánico que la mandó; si se sube desde el
    // sistema es quien tenía la sesión iniciada (ver 'usuario' en
    // fotos_vehiculo, modulos/operativo.py).
    return `
      <div class="op-foto-tile op-foto-tile--lista" data-foto-id="${foto.id}" data-foto-url="/api/operativo/fotos/${foto.id}/archivo" data-foto-titulo="${escapeHtml(label)}">
        <div class="op-foto-tile-preview"><img src="/api/operativo/fotos/${foto.id}/archivo" alt="${escapeHtml(label)}" loading="lazy"></div>
        <div class="op-foto-tile-titulo op-foto-tile-titulo--lista">${escapeHtml(label)}</div>
        <div class="op-foto-tile-responsable">${escapeHtml(foto.usuario || "—")}</div>
        <button type="button" class="op-foto-tile-borrar" data-borrar-foto="${foto.id}">Eliminar</button>
      </div>`;
  }
  return `
    <div class="op-foto-tile" data-categoria-vacia="${categoria}" data-categoria-label="${escapeHtml(label)}">
      <div class="op-foto-tile-preview">Sin foto</div>
      <div class="op-foto-tile-titulo op-foto-tile-titulo--pendiente">${escapeHtml(label)}</div>
      <button type="button" class="btn btn-secundario btn-chico" data-tomar-foto="${categoria}" data-tomar-foto-label="${escapeHtml(label)}">Tomar/Subir foto</button>
    </div>`;
}

// Las fotos se dividen en dos secciones: "de ingreso" (las obligatorias +
// las libres agregadas desde el sistema al recibir el auto o después) y
// "de desarme/armado" (las que llegan por WhatsApp mientras se trabaja el
// vehículo -- ver 'origen' en fotos_vehiculo y la rama 'vehiculo' de
// _agente_procesar_mensaje en app.py, que es lo único que hoy genera
// origen='desarme_armado'). Se separan porque son momentos distintos del
// proceso y quién las toma también suele ser distinto (recepción vs.
// mecánico en terreno).
function _renderFotosGrid(gridEl, fotos) {
  const todas = fotos || [];
  const fotosDesarme = todas.filter(f => f.origen === "desarme_armado");
  const fotosIngreso = todas.filter(f => f.origen !== "desarme_armado");

  const porCategoria = {};
  fotosIngreso.forEach(f => { if (f.categoria !== "libre") porCategoria[f.categoria] = f; });
  const libresIngreso = fotosIngreso.filter(f => f.categoria === "libre");

  let html = `<h4 class="op-fotos-seccion-titulo">Fotos de ingreso</h4><div class="op-fotos-grid">`;
  html += FOTOS_CATEGORIAS_OBLIGATORIAS.map(([cat, label]) => _renderFotosTile(cat, label, porCategoria[cat])).join("");
  html += libresIngreso.map(f => _renderFotosTile("libre", f.titulo || "Foto adicional", f)).join("");
  html += `</div>`;

  html += `<h4 class="op-fotos-seccion-titulo">Fotos de desarme / armado</h4>`;
  if (fotosDesarme.length === 0) {
    html += `<p class="ayuda-inline">Todavía no hay fotos de desarme/armado. Llegan automáticamente cuando un mecánico manda una foto por WhatsApp indicando la OT o patente.</p>`;
  } else {
    html += `<div class="op-fotos-grid">` + fotosDesarme.map(f => _renderFotosTile("libre", f.titulo || "Foto adicional", f)).join("") + `</div>`;
  }

  gridEl.innerHTML = html;
}

function _wireFotosGrid(gridEl, inputArchivoEl, onCambio) {
  gridEl.querySelectorAll("[data-tomar-foto]").forEach(btn => {
    btn.addEventListener("click", () => {
      _opFotosCategoriaPendiente = btn.dataset.tomarFoto;
      _opFotosTituloPendiente = btn.dataset.tomarFotoLabel || "";
      inputArchivoEl.click();
    });
  });
  gridEl.querySelectorAll("[data-borrar-foto]").forEach(btn => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm("¿Eliminar esta foto?")) return;
      try {
        await api(`/api/operativo/fotos/${btn.dataset.borrarFoto}`, { method: "DELETE" });
        toast("Foto eliminada.");
        await onCambio();
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
  gridEl.querySelectorAll(".op-foto-tile--lista").forEach(tile => {
    tile.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-borrar-foto]")) return;
      abrirFotoZoom(tile.dataset.fotoUrl, tile.dataset.fotoTitulo);
    });
  });
}

async function _subirFotoEntidad(entidadTipo, entidadId, categoria, titulo, archivo) {
  const formData = new FormData();
  formData.append("archivo", archivo);
  formData.append("categoria", categoria);
  if (titulo) formData.append("titulo", titulo);
  const res = await fetch(_urlFotosEntidad(entidadTipo, entidadId), { method: "POST", body: formData });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "No se pudo subir la foto");
  return data;
}

function abrirFotoZoom(url, titulo) {
  document.getElementById("opfz-titulo").textContent = titulo || "Foto";
  const img = document.getElementById("opfz-img");
  img.src = url;
  img.classList.remove("op-foto-zoom-activo");
  abrirModal("modal-op-foto-zoom");
}

document.getElementById("opfz-img").addEventListener("click", (e) => {
  e.target.classList.toggle("op-foto-zoom-activo");
});

// ---- Flujo obligatorio al crear la orden o la garantía ----
// El mismo modal (modal-op-fotos-ingreso) sirve para las dos entidades --
// "entidadTipo" ('orden' o 'garantia') decide qué endpoint usar y a qué
// detalle volver al cerrar. Ver también _urlFotosEntidad más abajo, que
// arma la URL base según el tipo (las OT usan "ordenes", las garantías
// "garantias" -- mismo criterio que el resto de la API).

function _urlFotosEntidad(entidadTipo, entidadId) {
  const segmento = entidadTipo === "garantia" ? "garantias" : "ordenes";
  return `/api/operativo/${segmento}/${entidadId}/fotos`;
}

async function abrirFotosIngreso(entidadTipo, entidadId) {
  document.getElementById("opfi-entidad-tipo").value = entidadTipo;
  document.getElementById("opfi-orden-id").value = entidadId;
  await cargarFotosIngreso();
  abrirModal("modal-op-fotos-ingreso");
}

// Cerrar (X o "Completar después") también deja a la persona en el detalle
// de la OT/garantía recién creada, en vez de devolverla al tablero sin más
// -- la idea es que "cerrar" sea "lo dejo para después", no "perder el hilo".
document.querySelectorAll('#modal-op-fotos-ingreso [data-close="modal-op-fotos-ingreso"]').forEach(btn => {
  btn.addEventListener("click", () => {
    const entidadTipo = document.getElementById("opfi-entidad-tipo").value;
    const entidadId = document.getElementById("opfi-orden-id").value;
    if (!entidadId) return;
    if (entidadTipo === "garantia") abrirDetalleGarantia(parseInt(entidadId, 10));
    else abrirDetalleOrden(parseInt(entidadId, 10));
  });
});

async function cargarFotosIngreso() {
  const entidadTipo = document.getElementById("opfi-entidad-tipo").value;
  const entidadId = document.getElementById("opfi-orden-id").value;
  try {
    const data = await api(_urlFotosEntidad(entidadTipo, entidadId));
    const grid = document.getElementById("opfi-fotos-grid");
    _renderFotosGrid(grid, data.fotos);
    _wireFotosGrid(grid, document.getElementById("opfi-archivo-libre"), cargarFotosIngreso);
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-opfi-agregar-libre").addEventListener("click", () => {
  const titulo = (prompt("Título de la foto (ej: Rayón puerta trasera derecha)") || "").trim();
  if (!titulo) return;
  _opFotosCategoriaPendiente = "libre";
  _opFotosTituloPendiente = titulo;
  document.getElementById("opfi-archivo-libre").click();
});

document.getElementById("opfi-archivo-libre").addEventListener("change", async (e) => {
  const archivo = e.target.files[0];
  const entidadTipo = document.getElementById("opfi-entidad-tipo").value;
  const entidadId = document.getElementById("opfi-orden-id").value;
  if (!archivo || !_opFotosCategoriaPendiente) { e.target.value = ""; return; }
  try {
    await _subirFotoEntidad(entidadTipo, entidadId, _opFotosCategoriaPendiente, _opFotosTituloPendiente, archivo);
    toast("Foto guardada.");
    await cargarFotosIngreso();
  } catch (err) {
    toast(err.message, true);
  } finally {
    e.target.value = "";
    _opFotosCategoriaPendiente = null;
    _opFotosTituloPendiente = "";
  }
});

document.getElementById("btn-opfi-finalizar").addEventListener("click", async () => {
  const entidadTipo = document.getElementById("opfi-entidad-tipo").value;
  const entidadId = document.getElementById("opfi-orden-id").value;
  const incluir = document.getElementById("opfi-fotos-filtro-envio").value;
  const btn = document.getElementById("btn-opfi-finalizar");
  btn.disabled = true;
  btn.textContent = "Enviando…";
  try {
    const data = await api(`${_urlFotosEntidad(entidadTipo, entidadId)}/finalizar?incluir=${incluir}`, { method: "POST" });
    if (data.correo_enviado) {
      toast("Registro fotográfico enviado al cliente.");
    } else {
      toast("Fotos guardadas, pero no se pudo enviar el correo: " + (data.correo_detalle || "revisa el correo del cliente."), true);
    }
    cerrarModal("modal-op-fotos-ingreso");
    if (entidadTipo === "garantia") abrirDetalleGarantia(parseInt(entidadId, 10));
    else abrirDetalleOrden(parseInt(entidadId, 10));
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Enviar registro fotográfico al cliente";
  }
});

// ---- Pestaña "Fotos" dentro del detalle de la OT (para completar/revisar
// después, si en la recepción se cerró el modal sin terminar) ----

async function cargarFotosOrdenTab() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const data = await api(`/api/operativo/ordenes/${ordenId}/fotos`);
    const grid = document.getElementById("opod-fotos-grid");
    _renderFotosGrid(grid, data.fotos);
    _wireFotosGrid(grid, document.getElementById("opod-fotos-archivo-libre"), cargarFotosOrdenTab);

    const aviso = document.getElementById("opod-fotos-faltan");
    if (data.faltan_obligatorias.length) {
      aviso.style.display = "";
      aviso.textContent = `Faltan fotos obligatorias: ${data.faltan_obligatorias.join(", ")}.`;
    } else {
      aviso.style.display = "none";
    }
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-opod-fotos-agregar").addEventListener("click", () => {
  const titulo = (prompt("Título de la foto (ej: Rayón puerta trasera derecha)") || "").trim();
  if (!titulo) return;
  _opFotosCategoriaPendiente = "libre";
  _opFotosTituloPendiente = titulo;
  document.getElementById("opod-fotos-archivo-libre").click();
});

document.getElementById("opod-fotos-archivo-libre").addEventListener("change", async (e) => {
  const archivo = e.target.files[0];
  const ordenId = document.getElementById("opod-id").value;
  if (!archivo || !_opFotosCategoriaPendiente) { e.target.value = ""; return; }
  try {
    await _subirFotoEntidad("orden", ordenId, _opFotosCategoriaPendiente, _opFotosTituloPendiente, archivo);
    toast("Foto guardada.");
    await cargarFotosOrdenTab();
  } catch (err) {
    toast(err.message, true);
  } finally {
    e.target.value = "";
    _opFotosCategoriaPendiente = null;
    _opFotosTituloPendiente = "";
  }
});

document.getElementById("btn-opod-fotos-finalizar").addEventListener("click", async () => {
  const ordenId = document.getElementById("opod-id").value;
  const incluir = document.getElementById("opod-fotos-filtro-envio").value;
  const btn = document.getElementById("btn-opod-fotos-finalizar");
  btn.disabled = true;
  btn.textContent = "Enviando…";
  try {
    const data = await api(`/api/operativo/ordenes/${ordenId}/fotos/finalizar?incluir=${incluir}`, { method: "POST" });
    if (data.correo_enviado) {
      toast("Registro fotográfico enviado al cliente.");
    } else {
      toast("Fotos guardadas, pero no se pudo enviar el correo: " + (data.correo_detalle || "revisa el correo del cliente."), true);
    }
    await cargarFotosOrdenTab();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Enviar registro fotográfico al cliente";
  }
});

// ---- Sección "Fotos" dentro del detalle de la garantía -- mismo criterio
// que la pestaña "Fotos" de la OT (ver arriba), solo que acá vive como una
// sección más del modal en vez de una pestaña, porque el detalle de
// garantía no está dividido en pestañas. ----

async function cargarFotosGarantiaDetalle() {
  const garantiaId = document.getElementById("gard-id").value;
  try {
    const data = await api(`/api/operativo/garantias/${garantiaId}/fotos`);
    const grid = document.getElementById("gard-fotos-grid");
    _renderFotosGrid(grid, data.fotos);
    _wireFotosGrid(grid, document.getElementById("gard-fotos-archivo-libre"), cargarFotosGarantiaDetalle);

    const aviso = document.getElementById("gard-fotos-faltan");
    if (data.faltan_obligatorias.length) {
      aviso.style.display = "";
      aviso.textContent = `Faltan fotos obligatorias: ${data.faltan_obligatorias.join(", ")}.`;
    } else {
      aviso.style.display = "none";
    }
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-gard-fotos-agregar").addEventListener("click", () => {
  const titulo = (prompt("Título de la foto (ej: Rayón puerta trasera derecha)") || "").trim();
  if (!titulo) return;
  _opFotosCategoriaPendiente = "libre";
  _opFotosTituloPendiente = titulo;
  document.getElementById("gard-fotos-archivo-libre").click();
});

document.getElementById("gard-fotos-archivo-libre").addEventListener("change", async (e) => {
  const archivo = e.target.files[0];
  const garantiaId = document.getElementById("gard-id").value;
  if (!archivo || !_opFotosCategoriaPendiente) { e.target.value = ""; return; }
  try {
    await _subirFotoEntidad("garantia", garantiaId, _opFotosCategoriaPendiente, _opFotosTituloPendiente, archivo);
    toast("Foto guardada.");
    await cargarFotosGarantiaDetalle();
  } catch (err) {
    toast(err.message, true);
  } finally {
    e.target.value = "";
    _opFotosCategoriaPendiente = null;
    _opFotosTituloPendiente = "";
  }
});

document.getElementById("btn-gard-fotos-finalizar").addEventListener("click", async () => {
  const garantiaId = document.getElementById("gard-id").value;
  const incluir = document.getElementById("gard-fotos-filtro-envio").value;
  const btn = document.getElementById("btn-gard-fotos-finalizar");
  btn.disabled = true;
  btn.textContent = "Enviando…";
  try {
    const data = await api(`/api/operativo/garantias/${garantiaId}/fotos/finalizar?incluir=${incluir}`, { method: "POST" });
    if (data.correo_enviado) {
      toast("Registro fotográfico enviado al cliente.");
    } else {
      toast("Fotos guardadas, pero no se pudo enviar el correo: " + (data.correo_detalle || "revisa el correo del cliente."), true);
    }
    await cargarFotosGarantiaDetalle();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Enviar registro fotográfico al cliente";
  }
});

// ---------------------------------------------------------------------------
// Diagnóstico
// ---------------------------------------------------------------------------

async function abrirDiagnostico() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    document.getElementById("opdi-sintoma").value = orden.sintoma || "";
    document.getElementById("opdi-diagnostico-tecnico").value = orden.diagnostico_tecnico || "";
    // Campo de solo lectura: se rellena para mostrar quién lo guardó la
    // última vez, pero el servidor jamás toma este valor del cliente (ver
    // editar_orden() en modulos/operativo.py) — siempre lo pisa con quien
    // tiene la sesión iniciada al guardar.
    document.getElementById("opdi-responsable-diagnostico").value = orden.responsable_diagnostico || "";
    document.getElementById("opdi-diagnostico").value = orden.diagnostico || "";
    // Se resetea el rastreo de "último texto generado por IA" cada vez que
    // se abre una OT -- si no, al pasar de una OT a otra el autocompletado
    // podría comparar contra el texto de la orden anterior y pisar un
    // diagnóstico de cliente ya guardado que nadie pidió tocar.
    _opdiUltimoGeneradoIA = orden.diagnostico || "";
    _opdiMostrarEstadoIA("");
    _opdiDetenerGrabacionSiEstaActiva();
    _opdiMostrarEstadoDictado("");
    await cargarSugerenciasDiagnostico(ordenId);
  } catch (e) {
    toast(e.message, true);
  }
}

// Sugerencias de diagnóstico dejadas por mecánicos de perfil restringido
// (ver migración 16 / agente_mecanicos.tipo) — el mecánico no puede
// escribir directo en diagnóstico_tecnico/diagnóstico, así que su
// comentario queda acá para que la parte operativa decida si lo usa.
async function cargarSugerenciasDiagnostico(ordenId) {
  const wrap = document.getElementById("opdi-sugerencias-wrap");
  const lista = document.getElementById("opdi-sugerencias-lista");
  try {
    const sugerencias = await api(`/api/ordenes/${ordenId}/sugerencias-diagnostico`);
    if (!sugerencias.length) {
      wrap.style.display = "none";
      lista.innerHTML = "";
      return;
    }
    wrap.style.display = "block";
    lista.innerHTML = sugerencias.map(s => `
      <div class="op-sugerencia-diagnostico" data-id="${s.id}">
        ${s.foto_id ? `
        <div class="op-sugerencia-foto" data-foto-url="/api/operativo/fotos/${s.foto_id}/archivo" data-foto-titulo="Foto de ${escapeHtml(s.mecanico_nombre)}">
          <img src="/api/operativo/fotos/${s.foto_id}/archivo" alt="Foto adjunta" loading="lazy">
          <span class="op-sugerencia-foto-zoom">🔍 Ampliar</span>
        </div>` : ""}
        <p class="op-sugerencia-texto">${escapeHtml(s.texto)}</p>
        <p class="ayuda-inline" style="margin:0 0 8px 0">Por: ${escapeHtml(s.mecanico_nombre)} — ${fmtFecha(s.creado_en)}</p>
        <div class="modal-actions modal-actions--start">
          <button type="button" class="btn btn-primary btn-chico" data-agregar-sugerencia="${s.id}">Agregar al diagnóstico</button>
          <button type="button" class="btn btn-secundario btn-chico" data-atender-sugerencia="${s.id}">Descartar sin agregar</button>
        </div>
      </div>
    `).join("");
    lista.querySelectorAll(".op-sugerencia-foto").forEach(el => {
      el.addEventListener("click", () => abrirFotoZoom(el.dataset.fotoUrl, el.dataset.fotoTitulo));
    });
    lista.querySelectorAll("[data-agregar-sugerencia]").forEach(btn => {
      const s = sugerencias.find(x => x.id === parseInt(btn.dataset.agregarSugerencia, 10));
      btn.addEventListener("click", () => agregarSugerenciaAlDiagnostico(s, ordenId));
    });
    lista.querySelectorAll("[data-atender-sugerencia]").forEach(btn => {
      btn.addEventListener("click", () => atenderSugerenciaDiagnostico(parseInt(btn.dataset.atenderSugerencia, 10), ordenId));
    });
  } catch (e) {
    wrap.style.display = "none";
  }
}

// "Agregar al diagnóstico" NO guarda solo -- deja el texto de la sugerencia
// cargado en el campo de Diagnóstico técnico (agregándolo al final si ya
// había algo escrito, para no perder lo que hubiera) y hace foco ahí, para
// que la parte operativa lo revise/ajuste y recién guarde con el botón
// "Guardar diagnóstico" de siempre -- así el diagnóstico oficial SIEMPRE
// queda con el responsable real (quien tiene la sesión iniciada), nunca se
// escribe solo con la sugerencia sin que nadie la haya mirado. Ya no se
// agrega "(sugerido por ...)" al texto -- el responsable de la sugerencia ya
// se ve en la tarjeta de arriba, repetirlo acá era redundante (pedido del
// taller).
async function agregarSugerenciaAlDiagnostico(sugerencia, ordenId) {
  if (!sugerencia) return;
  const campo = document.getElementById("opdi-diagnostico-tecnico");
  const previo = campo.value.trim();
  const aporte = sugerencia.texto;
  campo.value = previo ? `${previo}\n${aporte}` : aporte;
  // campo.value = ... (asignación por script) NO dispara el evento "input"
  // del que depende el autocompletado con IA del resumen para el cliente
  // (ver _opdiGenerarDiagnosticoCliente más abajo) -- por eso antes, al
  // "pegar" una sugerencia acá, la IA nunca se enteraba de que el campo
  // había cambiado. Se dispara a mano para que el flujo sea idéntico a
  // cuando alguien tipea directo en el campo.
  campo.dispatchEvent(new Event("input", { bubbles: true }));
  campo.focus();
  campo.scrollIntoView({ behavior: "smooth", block: "center" });
  toast("Sugerencia agregada al diagnóstico técnico — revisa y guarda cuando quede lista.");
  await atenderSugerenciaDiagnostico(sugerencia.id, ordenId);
}

async function atenderSugerenciaDiagnostico(sugerenciaId, ordenId) {
  try {
    await api(`/api/sugerencias-diagnostico/${sugerenciaId}/atender`, { method: "POST" });
    await cargarSugerenciasDiagnostico(ordenId);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---- Autocompletar "Diagnóstico (resumen para el cliente)" con IA ----
// A medida que se escribe el "Diagnóstico técnico", se genera
// automáticamente (con un pequeño retraso mientras se sigue escribiendo)
// un resumen en lenguaje simple para el campo que ve el cliente junto al
// presupuesto -- misma redacción que ya usa el agente de WhatsApp cuando
// el mecánico manda un audio (ver /api/operativo/ia/diagnostico-cliente y
// _DESCRIPCION_CAMPO_DIAGNOSTICO_CLIENTE en app.py), pero ahora también al
// escribir manualmente desde el sistema. Solo se autocompleta mientras el
// campo de cliente esté vacío o siga teniendo el último texto que generó
// la IA -- si alguien lo edita a mano, se deja de pisar hasta que la
// persona vuelva a tocar el diagnóstico técnico (ver abrirDiagnostico, que
// resetea _opdiUltimoGeneradoIA al abrir cada OT).
let _opdiDebounceTimer = null;
let _opdiUltimoGeneradoIA = "";
let _opdiPeticionEnCurso = 0;

function _opdiMostrarEstadoIA(texto) {
  const el = document.getElementById("opdi-diagnostico-ia-estado");
  if (!texto) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "block";
  el.textContent = texto;
}

async function _opdiGenerarDiagnosticoCliente() {
  const campoTecnico = document.getElementById("opdi-diagnostico-tecnico");
  const campoCliente = document.getElementById("opdi-diagnostico");
  const tecnico = campoTecnico.value.trim();
  if (!tecnico) {
    _opdiMostrarEstadoIA("");
    return;
  }
  // Si el campo de cliente tiene algo distinto de lo último que generó la
  // IA, asumimos que alguien lo editó a mano y no lo pisamos.
  if (campoCliente.value.trim() && campoCliente.value.trim() !== _opdiUltimoGeneradoIA.trim()) {
    return;
  }

  const miPeticion = ++_opdiPeticionEnCurso;
  _opdiMostrarEstadoIA("✨ Generando resumen para el cliente con IA…");
  try {
    const data = await api("/api/operativo/ia/diagnostico-cliente", {
      method: "POST",
      body: JSON.stringify({
        diagnostico_tecnico: tecnico,
        sintoma: document.getElementById("opdi-sintoma").value.trim(),
      }),
    });
    if (miPeticion !== _opdiPeticionEnCurso) return; // llegó tarde, ya hay una petición más nueva en curso
    if (data.diagnostico_cliente) {
      campoCliente.value = data.diagnostico_cliente;
      _opdiUltimoGeneradoIA = data.diagnostico_cliente;
      _opdiMostrarEstadoIA("✓ Resumen generado con IA — revísalo antes de guardar.");
    } else {
      _opdiMostrarEstadoIA("");
    }
  } catch (e) {
    if (miPeticion !== _opdiPeticionEnCurso) return;
    _opdiMostrarEstadoIA("No se pudo generar el resumen con IA: " + e.message);
  }
}

document.getElementById("opdi-diagnostico-tecnico").addEventListener("input", () => {
  clearTimeout(_opdiDebounceTimer);
  _opdiDebounceTimer = setTimeout(_opdiGenerarDiagnosticoCliente, 1200);
});

// ---- Dictar diagnóstico (audio grabado directo en el navegador) ----
// Mismo camino que ya existe para las notas de voz de WhatsApp
// (_transcribir_audio_whatsapp) más una separación automática en
// "síntoma" / "diagnóstico técnico" (ver /api/operativo/ia/dictado-diagnostico
// y _llamar_claude_extraer_diagnostico_dictado en app.py) -- pensado para
// que el mecánico pueda contar el diagnóstico hablado directo desde una OT
// abierta en el sistema, sin necesitar mandarlo por WhatsApp. Nunca guarda
// solo: rellena los campos y la persona revisa y guarda con el botón
// "Guardar diagnóstico" de siempre, igual que el autocompletado del
// resumen para el cliente de más arriba.
let _opdiMediaRecorder = null;
let _opdiChunksAudio = [];
let _opdiGrabando = false;

function _opdiMostrarEstadoDictado(texto, esError = false) {
  const el = document.getElementById("opdi-dictado-estado");
  if (!texto) {
    el.style.display = "none";
    el.textContent = "";
    el.classList.remove("ayuda-inline--error");
    return;
  }
  el.style.display = "block";
  el.textContent = texto;
  el.classList.toggle("ayuda-inline--error", esError);
}

function _opdiDetenerGrabacionSiEstaActiva() {
  if (_opdiGrabando && _opdiMediaRecorder && _opdiMediaRecorder.state !== "inactive") {
    // stop() dispara igual el evento "stop" y por lo tanto el envío al
    // servidor -- para cortar de verdad sin transcribir (ej. al cambiar de
    // OT a mitad de una grabación) hay que sacar los listeners antes.
    _opdiMediaRecorder.onstop = null;
    _opdiMediaRecorder.stream.getTracks().forEach(t => t.stop());
    _opdiMediaRecorder.stop();
  }
  _opdiGrabando = false;
  _opdiActualizarBotonDictado();
}

function _opdiActualizarBotonDictado() {
  const btn = document.getElementById("btn-opdi-dictar");
  if (_opdiGrabando) {
    btn.textContent = "⏹ Detener grabación";
    btn.classList.add("btn-danger");
  } else {
    btn.textContent = "🎤 Dictar diagnóstico";
    btn.classList.remove("btn-danger");
  }
}

async function _opdiIniciarGrabacion() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    _opdiMostrarEstadoDictado("Este navegador no permite grabar audio -- prueba desde Chrome o el WhatsApp del taller.", true);
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    _opdiChunksAudio = [];
    _opdiMediaRecorder = new MediaRecorder(stream);
    _opdiMediaRecorder.addEventListener("dataavailable", (ev) => {
      if (ev.data && ev.data.size > 0) _opdiChunksAudio.push(ev.data);
    });
    _opdiMediaRecorder.addEventListener("stop", () => {
      stream.getTracks().forEach(t => t.stop());
      _opdiSubirDictado(new Blob(_opdiChunksAudio, { type: _opdiMediaRecorder.mimeType || "audio/webm" }));
    });
    _opdiMediaRecorder.start();
    _opdiGrabando = true;
    _opdiActualizarBotonDictado();
    _opdiMostrarEstadoDictado("🔴 Grabando… presiona \"Detener grabación\" cuando termines de contar el diagnóstico.");
  } catch (e) {
    _opdiMostrarEstadoDictado("No se pudo acceder al micrófono: " + e.message, true);
  }
}

async function _opdiSubirDictado(blob) {
  _opdiMostrarEstadoDictado("✨ Transcribiendo y separando el dictado…");
  const formData = new FormData();
  formData.append("audio", blob, "dictado-diagnostico.webm");
  try {
    const res = await fetch("/api/operativo/ia/dictado-diagnostico", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo procesar el audio");

    // Nunca se pisa lo que ya esté escrito -- se agrega al final, igual
    // que "agregarSugerenciaAlDiagnostico" más arriba, para no perder algo
    // que la persona ya haya tipeado a mano antes de dictar.
    const campoSintoma = document.getElementById("opdi-sintoma");
    const campoTecnico = document.getElementById("opdi-diagnostico-tecnico");
    if (data.sintoma) {
      campoSintoma.value = campoSintoma.value.trim() ? `${campoSintoma.value.trim()}\n${data.sintoma}` : data.sintoma;
    }
    if (data.diagnostico_tecnico) {
      campoTecnico.value = campoTecnico.value.trim() ? `${campoTecnico.value.trim()}\n${data.diagnostico_tecnico}` : data.diagnostico_tecnico;
      // Dispara "input" a mano para que el autocompletado del resumen para
      // el cliente se entere del cambio (ver comentario en
      // agregarSugerenciaAlDiagnostico más arriba -- misma razón).
      campoTecnico.dispatchEvent(new Event("input", { bubbles: true }));
    }

    if (data.aviso) {
      _opdiMostrarEstadoDictado(data.aviso, true);
    } else if (!data.sintoma && !data.diagnostico_tecnico) {
      _opdiMostrarEstadoDictado("Se transcribió el audio pero no se detectó contenido para completar los campos.", true);
    } else {
      _opdiMostrarEstadoDictado("✓ Dictado agregado -- revisa el texto antes de guardar.");
    }
  } catch (e) {
    _opdiMostrarEstadoDictado("No se pudo procesar el dictado: " + e.message, true);
  } finally {
    _opdiGrabando = false;
    _opdiActualizarBotonDictado();
  }
}

document.getElementById("btn-opdi-dictar").addEventListener("click", () => {
  if (_opdiGrabando) {
    _opdiMediaRecorder.stop();
  } else {
    _opdiIniciarGrabacion();
  }
});

document.getElementById("btn-opdi-guardar").addEventListener("click", async () => {
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    sintoma: document.getElementById("opdi-sintoma").value.trim(),
    diagnostico_tecnico: document.getElementById("opdi-diagnostico-tecnico").value.trim(),
    diagnostico: document.getElementById("opdi-diagnostico").value.trim(),
  };
  try {
    await api(`/api/operativo/ordenes/${ordenId}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Diagnóstico guardado.");
    await refrescarResumenHub(ordenId);
    await abrirDiagnostico(); // refresca el responsable (lo acaba de fijar el servidor)
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Presupuesto
// ---------------------------------------------------------------------------

const OPP_TIPO_LABEL = {
  mano_obra: "Mano de obra", repuesto: "Repuesto", insumo: "Insumo",
  servicio_externo: "Servicio externo", descuento: "Descuento",
};

async function abrirPresupuestoModal() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPresupuesto(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

function renderPresupuesto(orden) {
  const tbody = document.getElementById("tbody-op-presupuesto");
  const items = orden.presupuesto.items;
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin ítems todavía.</td></tr>`;
  } else {
    tbody.innerHTML = items.map(it => `
      <tr>
        <td>${escapeHtml(OPP_TIPO_LABEL[it.tipo] || it.tipo)}</td>
        <td>${escapeHtml(it.descripcion)}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.precio_unitario)}</td>
        <td class="num">${fmtMoney(it.subtotal)}</td>
        <td><button type="button" class="btn-link btn-link--peligro" data-eliminar-presupuesto-item="${it.id}">Eliminar</button></td>
      </tr>
    `).join("");
  }
  document.getElementById("opp-subtotal").textContent = fmtMoney(orden.presupuesto.subtotal);
  document.getElementById("opp-iva").textContent = fmtMoney(orden.presupuesto.iva_monto);
  document.getElementById("opp-total").textContent = fmtMoney(orden.presupuesto.total);
  document.getElementById("opp-estado").value = orden.presupuesto_estado || "pendiente";
  document.getElementById("opp-iva-check").checked = !!orden.presupuesto_iva;
  // Mismo campo "diagnostico" que la pestaña Diagnóstico (y que puede
  // haber llegado por audio de WhatsApp) — se repite acá para poder
  // revisarlo/ajustarlo justo antes de mandar el presupuesto.
  document.getElementById("opp-diagnostico").value = orden.diagnostico || "";
  document.getElementById("opp-notas").value = orden.presupuesto_notas || "";

  tbody.querySelectorAll("[data-eliminar-presupuesto-item]").forEach(btn => {
    btn.addEventListener("click", () => eliminarItemPresupuesto(parseInt(btn.dataset.eliminarPresupuestoItem, 10)));
  });
}

document.getElementById("form-op-presupuesto-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    tipo: document.getElementById("oppi-tipo").value,
    descripcion: document.getElementById("oppi-descripcion").value.trim(),
    cantidad: document.getElementById("oppi-cantidad").value,
    precio_unitario: limpiarMonto(document.getElementById("oppi-precio").value) || 0,
  };
  if (!payload.descripcion) {
    toast("Describe el ítem del presupuesto.", true);
    return;
  }
  try {
    await api(`/api/operativo/ordenes/${ordenId}/presupuesto/items`, { method: "POST", body: JSON.stringify(payload) });
    toast("Ítem de presupuesto agregado.");
    document.getElementById("form-op-presupuesto-item").reset();
    document.getElementById("oppi-cantidad").value = 1;
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPresupuesto(orden);
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarItemPresupuesto(itemId) {
  if (!confirm("¿Eliminar este ítem del presupuesto?")) return;
  const ordenId = document.getElementById("opod-id").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/presupuesto/items/${itemId}`, { method: "DELETE" });
    toast("Ítem eliminado.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPresupuesto(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-opp-guardar").addEventListener("click", async () => {
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    estado: document.getElementById("opp-estado").value,
    iva: document.getElementById("opp-iva-check").checked,
    diagnostico: document.getElementById("opp-diagnostico").value.trim(),
    notas: document.getElementById("opp-notas").value.trim(),
  };
  try {
    const actualizada = await api(`/api/operativo/ordenes/${ordenId}/presupuesto`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Presupuesto actualizado.");
    renderPresupuesto(actualizada);
    await refrescarResumenHub(ordenId);
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-opp-descargar-pdf").addEventListener("click", () => {
  const ordenId = document.getElementById("opod-id").value;
  window.open(`/api/operativo/ordenes/${ordenId}/presupuesto/pdf`, "_blank");
});

document.getElementById("btn-opp-enviar-correo").addEventListener("click", async (e) => {
  const ordenId = document.getElementById("opod-id").value;
  const btn = e.currentTarget;
  if (!confirm("¿Enviar el presupuesto al correo registrado del cliente?")) return;
  btn.disabled = true;
  const textoOriginal = btn.textContent;
  btn.textContent = "Enviando…";
  try {
    const resp = await api(`/api/operativo/ordenes/${ordenId}/presupuesto/enviar`, { method: "POST" });
    const copia = resp.copia && resp.copia.length ? ` (con copia a ${resp.copia.join(", ")})` : "";
    toast(`Presupuesto enviado a ${resp.destinatario}${copia}.`);
    if (resp.orden) {
      renderPresupuesto(resp.orden);
      await refrescarResumenHub(ordenId);
    }
  } catch (e2) {
    toast(e2.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = textoOriginal;
  }
});

// ---------------------------------------------------------------------------
// Carga de repuestos (antes "Repuestos y mano de obra")
// ---------------------------------------------------------------------------

async function renderSelectConvertidoresDisponibles() {
  const select = document.getElementById("opoi-convertidor-id");
  const aviso = document.getElementById("opoi-convertidor-sin-stock");
  try {
    const data = await api("/api/convertidores/disponibles");
    const disponibles = data.convertidores || [];
    select.innerHTML = '<option value="">Selecciona un convertidor…</option>' +
      disponibles.map(c => {
        const detalle = [c.marca, c.modelo].filter(Boolean).join(" ");
        const serie = c.numero_serie ? ` — N/S ${c.numero_serie}` : "";
        return `<option value="${c.id}">Convertidor #${c.id}${detalle ? " — " + escapeHtml(detalle) : ""}${escapeHtml(serie)} — ${fmtMoney(c.costo_total)}</option>`;
      }).join("");
    aviso.style.display = disponibles.length === 0 ? "" : "none";
  } catch (e) {
    aviso.style.display = "";
  }
}

async function abrirRepuestosModal() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    if (state.opProductos.length === 0) {
      const dataProductos = await api("/api/productos");
      state.opProductos = dataProductos.productos;
    }
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderItemsOrden(orden);
    document.getElementById("opoi-producto-buscar").value = "";
    document.getElementById("opoi-producto-id").value = "";
    document.getElementById("opoi-producto-resultados").style.display = "none";
    document.getElementById("form-op-item").reset();
    document.getElementById("opoi-tipo").dispatchEvent(new Event("change"));
    await renderSelectConvertidoresDisponibles();
  } catch (e) {
    toast(e.message, true);
  }
}

// Búsqueda de producto para "Carga de repuestos": al hacer clic en un
// resultado se agrega el ítem al tiro (misma cantidad del campo "Cantidad"),
// SIN cerrar la lista de resultados — así se puede seguir clickeando otros
// productos que calcen con el mismo filtro sin retipear la búsqueda. El botón
// "+ Agregar ítem" se mantiene para mano de obra y como respaldo manual.
function renderResultadosOpoi(texto) {
  const resultados = document.getElementById("opoi-producto-resultados");
  const q = texto.trim();
  if (!q) {
    resultados.style.display = "none";
    return;
  }
  const coincidencias = state.opProductos.filter(p =>
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
    // mousedown (no click) para que se dispare antes del blur del input.
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const p = state.opProductos.find(x => x.id === parseInt(item.dataset.id, 10));
      if (p) agregarRepuestoDesdeAutocomplete(p);
    });
  });
}

async function agregarRepuestoDesdeAutocomplete(producto) {
  const ordenId = document.getElementById("opod-id").value;
  const cantidad = document.getElementById("opoi-cantidad").value || 1;
  const textoBusqueda = document.getElementById("opoi-producto-buscar").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/items`, {
      method: "POST",
      body: JSON.stringify({ tipo: "repuesto", producto_id: producto.id, cantidad }),
    });
    toast(`Agregado: ${producto.nombre}`);
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderItemsOrden(orden);
    renderCostosOrden(orden);
    // Refresca el stock y vuelve a mostrar los resultados con el mismo
    // filtro, para poder seguir agregando productos parecidos sin cerrar
    // la lista ni retipear la búsqueda.
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
    renderResultadosOpoi(textoBusqueda);
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("opoi-producto-buscar").addEventListener("input", (e) => {
  document.getElementById("opoi-producto-id").value = "";
  renderResultadosOpoi(e.target.value);
});
document.getElementById("opoi-producto-buscar").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosOpoi(e.target.value);
});
document.getElementById("opoi-producto-buscar").addEventListener("blur", () => {
  setTimeout(() => {
    document.getElementById("opoi-producto-resultados").style.display = "none";
  }, 150);
});

function renderItemsOrden(orden) {
  const tbody = document.getElementById("tbody-op-items");
  if (orden.items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin ítems todavía.</td></tr>`;
  } else {
    const TIPO_ITEM_LABEL_OT = { repuesto: "Repuesto", mano_obra: "Mano de obra", convertidor_torque: "Convertidor de torque" };
    tbody.innerHTML = orden.items.map(it => `
      <tr>
        <td>${TIPO_ITEM_LABEL_OT[it.tipo] || it.tipo}${it.aplica_iva ? ' <span style="font-size:11px; color:var(--accent-2); border:1px solid var(--accent-2); border-radius:4px; padding:1px 4px;">+IVA</span>' : ""}</td>
        <td>${escapeHtml(it.descripcion)}${it.detalle ? `<br><span class="ayuda-inline" style="margin:0">${escapeHtml(it.detalle)}</span>` : ""}${it.convertidor_id ? `<br><a href="/convertidores?ver=${it.convertidor_id}#convertidores" class="ayuda-inline">Ver convertidor #${it.convertidor_id}</a>` : ""}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.subtotal !== undefined ? it.subtotal : it.cantidad * it.costo_unitario)}</td>
        <td><button type="button" class="btn-link btn-link--peligro" data-eliminar-item="${it.id}">Eliminar</button></td>
      </tr>
    `).join("");
  }
  document.getElementById("opod-total").innerHTML = `<strong>${fmtMoney(orden.total)}</strong>`;
  tbody.querySelectorAll("[data-eliminar-item]").forEach(btn => {
    btn.addEventListener("click", () => eliminarItemOrden(parseInt(btn.dataset.eliminarItem, 10)));
  });
}

document.getElementById("opoi-tipo").addEventListener("change", (e) => {
  const esRepuesto = e.target.value === "repuesto";
  const esConvertidor = e.target.value === "convertidor_torque";
  document.getElementById("opoi-campo-producto").style.display = esRepuesto ? "flex" : "none";
  document.getElementById("opoi-campo-descripcion").style.display = (!esRepuesto && !esConvertidor) ? "flex" : "none";
  document.getElementById("opoi-campo-costo").style.display = (!esRepuesto && !esConvertidor) ? "flex" : "none";
  document.getElementById("opoi-fila-mo-extra").style.display = (!esRepuesto && !esConvertidor) ? "flex" : "none";
  document.getElementById("opoi-campo-convertidor").style.display = esConvertidor ? "flex" : "none";
  if (esConvertidor) renderSelectConvertidoresDisponibles();
});

document.getElementById("form-op-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tipo = document.getElementById("opoi-tipo").value;
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    tipo,
    cantidad: document.getElementById("opoi-cantidad").value,
  };
  if (tipo === "repuesto") {
    payload.producto_id = document.getElementById("opoi-producto-id").value;
    if (!payload.producto_id) {
      toast("Busca el producto y haz clic en él para agregarlo.", true);
      return;
    }
  } else if (tipo === "convertidor_torque") {
    payload.convertidor_id = document.getElementById("opoi-convertidor-id").value;
    payload.cantidad = 1;
    if (!payload.convertidor_id) {
      toast("Selecciona el convertidor de torque a instalar.", true);
      return;
    }
  } else {
    payload.descripcion = document.getElementById("opoi-descripcion").value.trim();
    payload.costo_unitario = limpiarMonto(document.getElementById("opoi-costo").value) || 0;
    payload.detalle = document.getElementById("opoi-mo-detalle").value.trim();
    payload.aplica_iva = document.getElementById("opoi-mo-iva").checked;
    if (!payload.descripcion) {
      toast("Describe la mano de obra realizada.", true);
      return;
    }
  }
  try {
    await api(`/api/operativo/ordenes/${ordenId}/items`, { method: "POST", body: JSON.stringify(payload) });
    toast("Ítem agregado.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderItemsOrden(orden);
    renderCostosOrden(orden);
    document.getElementById("form-op-item").reset();
    document.getElementById("opoi-producto-buscar").value = "";
    document.getElementById("opoi-producto-id").value = "";
    document.getElementById("opoi-tipo").dispatchEvent(new Event("change"));
    // Refresca el stock que se muestra en los resultados de búsqueda.
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarItemOrden(itemId) {
  if (!confirm("¿Eliminar este ítem? Si era un repuesto, el stock consumido se devuelve al inventario.")) return;
  const ordenId = document.getElementById("opod-id").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/items/${itemId}`, { method: "DELETE" });
    toast("Ítem eliminado.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderItemsOrden(orden);
    renderCostosOrden(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

async function abrirQcModal() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPruebasRuta(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Bitácora de pruebas de ruta — historial de entradas (fecha + autor +
// texto + foto opcional), nunca se sobrescribe una entrada anterior. Se
// llena desde el formulario de acá abajo o, automáticamente, cuando un
// mecánico de perfil "probador_de_autos" manda el resultado por WhatsApp
// (ver _agente_guardar_prueba_ruta en app.py) — en ese caso "origen" llega
// como 'whatsapp' en vez de 'web', y se muestra distinto para que se note
// de dónde vino.
// ---------------------------------------------------------------------------

function renderPruebasRuta(orden) {
  const lista = document.getElementById("op-pruebas-ruta-lista");
  const pruebas = orden.pruebas_ruta || [];
  if (!pruebas.length) {
    lista.innerHTML = `<p class="vacio-inline">Todavía no hay pruebas de ruta registradas para esta OT.</p>`;
  } else {
    lista.innerHTML = pruebas.map(p => `
      <div class="op-prueba-ruta-item" data-id="${p.id}">
        ${p.foto_id ? `
        <div class="op-sugerencia-foto" data-foto-url="/api/operativo/fotos/${p.foto_id}/archivo" data-foto-titulo="Foto de prueba de ruta">
          <img src="/api/operativo/fotos/${p.foto_id}/archivo" alt="Foto de la prueba de ruta" loading="lazy">
          <span class="op-sugerencia-foto-zoom">🔍 Ampliar</span>
        </div>` : ""}
        <p class="op-sugerencia-texto">${escapeHtml(p.texto)}</p>
        <p class="ayuda-inline" style="margin:0 0 8px 0">
          ${p.origen === "whatsapp" ? "📱 Por WhatsApp — " : ""}${escapeHtml(p.autor || "—")} — ${fmtFecha(p.creado_en)}
        </p>
        <div class="modal-actions modal-actions--start">
          <button type="button" class="btn-link btn-link--peligro" data-eliminar-prueba-ruta="${p.id}">Eliminar</button>
        </div>
      </div>
    `).join("");
    lista.querySelectorAll(".op-sugerencia-foto").forEach(el => {
      el.addEventListener("click", () => abrirFotoZoom(el.dataset.fotoUrl, el.dataset.fotoTitulo));
    });
    lista.querySelectorAll("[data-eliminar-prueba-ruta]").forEach(btn => {
      btn.addEventListener("click", () => eliminarPruebaRuta(parseInt(btn.dataset.eliminarPruebaRuta, 10)));
    });
  }
}

document.getElementById("form-op-prueba-ruta").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("opod-id").value;
  const texto = document.getElementById("oppr-texto").value.trim();
  if (!texto) {
    toast("Escribe el resultado de la prueba de ruta.", true);
    return;
  }
  const archivoInput = document.getElementById("oppr-foto");
  const archivo = archivoInput.files[0];
  try {
    let fotoId = null;
    if (archivo) {
      const foto = await _subirFotoEntidad("orden", ordenId, "libre", "Prueba de ruta", archivo);
      fotoId = foto.id;
    }
    await api(`/api/operativo/ordenes/${ordenId}/pruebas-ruta`, {
      method: "POST", body: JSON.stringify({ texto, foto_id: fotoId }),
    });
    toast("Prueba de ruta agregada a la bitácora.");
    document.getElementById("form-op-prueba-ruta").reset();
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPruebasRuta(orden);
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarPruebaRuta(pruebaId) {
  if (!confirm("¿Eliminar esta entrada de la bitácora de pruebas de ruta?")) return;
  const ordenId = document.getElementById("opod-id").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/pruebas-ruta/${pruebaId}`, { method: "DELETE" });
    toast("Entrada eliminada.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPruebasRuta(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Historial de vehículo
// ---------------------------------------------------------------------------

let opHistorialEquipoActivo = null; // { equipo, cliente, ordenes } — para "Crear garantía"

async function abrirHistorialEquipo(equipoId) {
  try {
    const data = await api(`/api/operativo/equipos/${equipoId}/historial`);
    opHistorialEquipoActivo = data;
    document.getElementById("modal-op-equipo-historial-titulo").textContent =
      `Historial — ${[data.equipo.marca, data.equipo.modelo, data.equipo.identificador].filter(Boolean).join(" ") || "Equipo #" + data.equipo.id}`;
    document.getElementById("opeh-cliente").textContent = data.cliente ? `Cliente: ${data.cliente.nombre}` : "";
    const tbody = document.getElementById("tbody-equipo-historial");
    if (data.ordenes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="vacio">Este vehículo no tiene órdenes registradas todavía.</td></tr>`;
    } else {
      tbody.innerHTML = data.ordenes.map(o => `
        <tr data-ver-orden="${o.id}">
          <td>${fmtFechaSolo((o.fecha_recepcion || "").slice(0, 10))}</td>
          <td>${escapeHtml(OP_ESTADOS_LABEL[o.estado] || o.estado)}</td>
          <td>${escapeHtml(o.diagnostico || o.descripcion_problema || "—")}</td>
          <td class="num">${fmtMoney(o.total)}</td>
        </tr>
      `).join("");
      tbody.querySelectorAll("[data-ver-orden]").forEach(tr => {
        tr.addEventListener("click", () => {
          cerrarModal("modal-op-equipo-historial");
          cerrarModal("modal-op-orden-nueva");
          abrirDetalleOrden(parseInt(tr.dataset.verOrden, 10));
        });
      });
    }
    const contGarantias = document.getElementById("opeh-garantias");
    if (!data.garantias || data.garantias.length === 0) {
      contGarantias.innerHTML = `<p class="vacio-inline">Sin garantías registradas para este vehículo.</p>`;
    } else {
      contGarantias.innerHTML = data.garantias.map(g => `
        <div class="op-garantia-card">
          <div>${escapeHtml(g.motivo)}</div>
          <div class="op-garantia-meta">
            Estado: ${escapeHtml(GAR_ESTADO_LABEL[g.estado] || g.estado)}
            ${g.responsable ? " · " + escapeHtml(g.responsable) : ""} · ${fmtFecha(g.creado_en)}
          </div>
        </div>
      `).join("");
    }
    abrirModal("modal-op-equipo-historial");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-opeh-crear-garantia").addEventListener("click", () => {
  if (!opHistorialEquipoActivo) return;
  abrirNuevaGarantia({
    clienteId: opHistorialEquipoActivo.cliente ? opHistorialEquipoActivo.cliente.id : null,
    equipoId: opHistorialEquipoActivo.equipo.id,
    ordenes: opHistorialEquipoActivo.ordenes,
  });
});

// ---- Pestaña Clientes: ver equipos / historial ----

async function abrirEquiposDeCliente(clienteId) {
  try {
    const data = await api(`/api/operativo/clientes/${clienteId}`);
    document.getElementById("modal-op-cliente-equipos-titulo").textContent = `Equipos de ${data.nombre}`;
    const tbody = document.getElementById("tbody-cliente-equipos");
    if (data.equipos.length === 0) {
      tbody.innerHTML = `<tr><td colspan="2" class="vacio">Este cliente no tiene equipos registrados todavía.</td></tr>`;
    } else {
      tbody.innerHTML = data.equipos.map(eq => `
        <tr>
          <td>${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</td>
          <td>
            <button type="button" class="btn-link" data-ver-historial-equipo="${eq.id}">Ver historial</button>
            <button type="button" class="btn-link" style="color: var(--danger)" data-eliminar-equipo="${eq.id}">Eliminar</button>
          </td>
        </tr>
      `).join("");
      tbody.querySelectorAll("[data-ver-historial-equipo]").forEach(btn => {
        btn.addEventListener("click", () => abrirHistorialEquipo(parseInt(btn.dataset.verHistorialEquipo, 10)));
      });
      // Solo se puede eliminar un equipo si no tiene órdenes asociadas
      // (el backend lo valida igual, esto es solo el mensaje de confirmación).
      tbody.querySelectorAll("[data-eliminar-equipo]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const equipoId = parseInt(btn.dataset.eliminarEquipo, 10);
          if (!confirm("¿Eliminar este equipo? Solo se puede si no tiene órdenes de trabajo asociadas.")) return;
          try {
            await api(`/api/operativo/equipos/${equipoId}`, { method: "DELETE" });
            toast("Equipo eliminado.");
            abrirEquiposDeCliente(clienteId);
          } catch (e2) {
            toast(e2.message, true);
          }
        });
      });
    }
    abrirModal("modal-op-cliente-equipos");
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Garantías — entidad propia, con su propia vista y su propio proceso
// (no viven dentro del tablero de OTs).
// ---------------------------------------------------------------------------

const GAR_ESTADO_LABEL = {
  ingreso: "Ingreso", diagnostico: "Diagnóstico", reparacion: "Reparación",
  prueba: "Prueba", revision: "Revisión", listo: "Listo",
  entregado: "Entregado", rechazada: "Rechazada",
};

const GAR_ESTADOS_TABLERO = [
  "ingreso", "diagnostico", "reparacion", "prueba", "revision", "listo",
  "entregado", "rechazada",
];

// Tablero kanban de garantías — mismo patrón visual y de arrastrar-y-soltar
// que el tablero de Órdenes de Trabajo (ver renderTableroOperativo), pero
// con su propio proceso y su propio contenedor (#gar-kanban), para que no
// se mezclen entre sí los selectores ".op-columna"/".op-card" que usan la
// misma clase CSS en los dos tableros.
async function cargarGarantiasTab() {
  try {
    const data = await api("/api/operativo/garantias");
    state.opGarantias = data.garantias;
    renderTableroGarantias();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderTableroGarantias() {
  const filtroEstado = document.getElementById("gar-filtro-estado").value;
  const busqueda = document.getElementById("gar-buscar").value.trim().toLowerCase();

  document.getElementById("gar-columna-rechazada").style.display = filtroEstado === "rechazada" ? "flex" : "none";
  document.querySelectorAll("#gar-kanban .op-columna").forEach(col => {
    const est = col.dataset.estado;
    if (filtroEstado) {
      col.style.display = est === filtroEstado ? "flex" : "none";
    } else {
      col.style.display = est === "rechazada" ? "none" : "flex";
    }
  });

  const coincide = (g) => coincideBusqueda(
    busqueda,
    g.cliente ? g.cliente.nombre : "",
    g.equipo ? `${g.equipo.marca} ${g.equipo.modelo} ${g.equipo.identificador || ""}` : "",
    g.motivo || "",
  );

  GAR_ESTADOS_TABLERO.forEach(estado => {
    const cont = document.getElementById(`gar-cards-${estado}`);
    const contador = document.getElementById(`gar-count-${estado}`);
    const garantias = (state.opGarantias || []).filter(g => g.estado === estado && coincide(g));
    contador.textContent = garantias.length;
    if (garantias.length === 0) {
      cont.innerHTML = `<p class="vacio-inline">Sin garantías.</p>`;
      return;
    }
    cont.innerHTML = garantias.map(g => `
      <div class="op-card" data-id="${g.id}" draggable="true">
        <div class="op-card-top">
          <span class="op-card-ot">Garantía #${g.id}</span>
        </div>
        <div class="op-card-cliente">${escapeHtml(g.cliente ? g.cliente.nombre : "Sin cliente")}</div>
        ${g.equipo ? `<div class="op-card-equipo">${escapeHtml([g.equipo.marca, g.equipo.modelo].filter(Boolean).join(" · ") || g.equipo.identificador || "")}</div>` : ""}
        <div class="op-card-descripcion">${escapeHtml(g.motivo)}</div>
        <div class="op-card-meta">
          <span>Ingreso: ${fmtFechaSolo((g.fecha_recepcion || "").slice(0, 10))}</span>
          ${g.responsable ? `<span>${escapeHtml(g.responsable)}</span>` : ""}
        </div>
      </div>
    `).join("");
  });

  document.querySelectorAll("#gar-kanban .op-card").forEach(card => {
    card.addEventListener("click", () => abrirDetalleGarantia(parseInt(card.dataset.id, 10)));
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id);
      card.classList.add("dragging");
      opArrastrandoTarjeta = true;
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      opArrastrandoTarjeta = false;
    });
  });
}

// Igual que en el tablero de OT: los drop-zones son las columnas fijas del
// HTML, así que los listeners se conectan una sola vez.
document.querySelectorAll("#gar-kanban .op-columna").forEach(columna => {
  columna.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    columna.classList.add("drag-over");
  });
  columna.addEventListener("dragleave", () => {
    columna.classList.remove("drag-over");
  });
  columna.addEventListener("drop", async (e) => {
    e.preventDefault();
    columna.classList.remove("drag-over");
    const garantiaId = parseInt(e.dataTransfer.getData("text/plain"), 10);
    const nuevoEstado = columna.dataset.estado;
    if (!garantiaId || !nuevoEstado) return;
    const garantia = (state.opGarantias || []).find(g => g.id === garantiaId);
    if (garantia && garantia.estado === nuevoEstado) return; // soltada en su propia columna
    try {
      await api(`/api/operativo/garantias/${garantiaId}`, { method: "PUT", body: JSON.stringify({ estado: nuevoEstado }) });
      toast(`Garantía #${garantiaId} movida a "${GAR_ESTADO_LABEL[nuevoEstado] || nuevoEstado}".`);
      await cargarGarantiasTab();
    } catch (err) {
      toast(err.message, true);
    }
  });
});

document.getElementById("gar-filtro-estado").addEventListener("change", renderTableroGarantias);
document.getElementById("gar-buscar").addEventListener("input", renderTableroGarantias);

// ---- Nueva garantía ----

// Conceptos frecuentes de motivo de garantía: al hacer clic se agregan al
// textarea, para no tener que escribirlos siempre desde cero.
const MOTIVOS_GARANTIA_SUGERIDOS = [
  "Ruido", "No se mueve", "Patina entre marchas", "Golpe al aplicar",
  "Golpe al pasar marchas", "Pierde fluido", "Falla eléctrica", "Vuelve a fallar",
];

function renderMotivoChipsGarantia() {
  const cont = document.getElementById("garn-motivo-chips");
  cont.innerHTML = MOTIVOS_GARANTIA_SUGERIDOS.map(m => `<button type="button" class="chip" data-motivo-chip="${escapeHtml(m)}">${escapeHtml(m)}</button>`).join("");
  cont.querySelectorAll("[data-motivo-chip]").forEach(chip => {
    chip.addEventListener("click", () => {
      const textarea = document.getElementById("garn-motivo");
      const texto = chip.dataset.motivoChip;
      textarea.value = textarea.value.trim() ? `${textarea.value.trim()}, ${texto}` : texto;
      textarea.focus();
    });
  });
}

function renderSelectEquiposGarantia(equipos, valorActual) {
  const select = document.getElementById("garn-equipo-id");
  select.innerHTML = '<option value="">Selecciona un vehículo…</option>' +
    equipos.map(eq => `<option value="${eq.id}">${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</option>`).join("");
  select.value = valorActual || "";
  // select.value = ... no dispara "change", así que el link de historial y el
  // aviso de bloqueo se actualizan acá también (cubre equipo recién creado).
  document.getElementById("garn-ver-historial").style.display = valorActual ? "" : "none";
  chequearBloqueoEquipo(valorActual || null, "garantia", "garn-equipo-bloqueo");
}

function renderSelectOrdenesGarantia(ordenes, valorActual) {
  const select = document.getElementById("garn-orden-origen-id");
  select.innerHTML = '<option value="">Sin OT asociada</option>' +
    (ordenes || []).map(o => `<option value="${o.id}">OT #${o.id} — ${fmtFechaSolo((o.fecha_recepcion || "").slice(0, 10))}</option>`).join("");
  select.value = valorActual || "";
}

async function cargarOrdenesGarantiaParaEquipo(equipoId, valorActual) {
  const ayuda = document.getElementById("garn-orden-origen-ayuda");
  if (!equipoId) {
    renderSelectOrdenesGarantia([], "");
    ayuda.style.display = "";
    return;
  }
  ayuda.style.display = "none";
  const dataOrdenes = await api(`/api/operativo/ordenes?equipo_id=${equipoId}`);
  renderSelectOrdenesGarantia(dataOrdenes.ordenes, valorActual || "");
}

async function abrirNuevaGarantia(prefill) {
  document.getElementById("form-op-garantia-nueva").reset();
  renderMotivoChipsGarantia();
  if (state.opClientes.length === 0) {
    const dataClientes = await api("/api/operativo/clientes");
    state.opClientes = dataClientes.clientes;
  }
  renderSelectClientesOperativo("garn-cliente-id", prefill ? prefill.clienteId : "");
  if (prefill && prefill.clienteId) {
    const dataEquipos = await api(`/api/operativo/equipos?cliente_id=${prefill.clienteId}`);
    renderSelectEquiposGarantia(dataEquipos.equipos, prefill.equipoId);
    await cargarOrdenesGarantiaParaEquipo(prefill.equipoId, "");
  } else {
    renderSelectEquiposGarantia([], "");
    await cargarOrdenesGarantiaParaEquipo(null, "");
  }
  abrirModal("modal-op-garantia-nueva");
}

document.getElementById("btn-gar-nueva").addEventListener("click", () => abrirNuevaGarantia(null));

document.getElementById("garn-cliente-id").addEventListener("change", async (e) => {
  const clienteId = e.target.value;
  if (!clienteId) {
    renderSelectEquiposGarantia([], "");
    await cargarOrdenesGarantiaParaEquipo(null, "");
    return;
  }
  const dataEquipos = await api(`/api/operativo/equipos?cliente_id=${clienteId}`);
  renderSelectEquiposGarantia(dataEquipos.equipos, "");
  await cargarOrdenesGarantiaParaEquipo(null, "");
});

document.getElementById("garn-equipo-id").addEventListener("change", (e) => {
  document.getElementById("garn-ver-historial").style.display = e.target.value ? "" : "none";
  chequearBloqueoEquipo(e.target.value || null, "garantia", "garn-equipo-bloqueo");
  cargarOrdenesGarantiaParaEquipo(e.target.value || null, "");
});

document.getElementById("garn-ver-historial").addEventListener("click", () => {
  const equipoId = document.getElementById("garn-equipo-id").value;
  if (equipoId) abrirHistorialEquipo(parseInt(equipoId, 10));
});

document.getElementById("garn-nuevo-cliente").addEventListener("click", () => {
  opClienteCallback = async (nuevo) => {
    const data = await api("/api/operativo/clientes");
    state.opClientes = data.clientes;
    renderSelectClientesOperativo("garn-cliente-id", nuevo.id);
    document.getElementById("garn-cliente-id").dispatchEvent(new Event("change"));
  };
  abrirNuevoClienteOperativo();
});

document.getElementById("garn-nuevo-equipo").addEventListener("click", () => {
  const clienteId = document.getElementById("garn-cliente-id").value;
  if (!clienteId) {
    toast("Primero selecciona (o crea) un cliente.", true);
    return;
  }
  document.getElementById("form-op-equipo").reset();
  document.getElementById("ope-patente-encontrada").style.display = "none";
  document.getElementById("ope-id").value = "";
  poblarSelectMarca("ope-marca", "ope-marca-otra", "");
  poblarSelectModelo("ope-modelo", "ope-modelo-otro", "", "");
  renderSelectClienteEquipoModal(clienteId);
  document.getElementById("modal-op-equipo-titulo").textContent = "Nuevo equipo";
  opEquipoCallback = async (nuevo, payload) => {
    const dataEquipos = await api(`/api/operativo/equipos?cliente_id=${payload.cliente_id}`);
    renderSelectEquiposGarantia(dataEquipos.equipos, nuevo.id);
    await cargarOrdenesGarantiaParaEquipo(nuevo.id, "");
  };
  abrirModal("modal-op-equipo");
});

document.getElementById("form-op-garantia-nueva").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    cliente_id: document.getElementById("garn-cliente-id").value,
    equipo_id: document.getElementById("garn-equipo-id").value || null,
    orden_origen_id: document.getElementById("garn-orden-origen-id").value || null,
    motivo: document.getElementById("garn-motivo").value.trim(),
  };
  if (!payload.cliente_id) {
    toast("Selecciona un cliente.", true);
    return;
  }
  if (!payload.equipo_id) {
    toast("Selecciona o crea el vehículo de la garantía.", true);
    return;
  }
  if (!payload.motivo) {
    toast("Describe el motivo de la garantía.", true);
    return;
  }
  try {
    const nueva = await api("/api/operativo/garantias", { method: "POST", body: JSON.stringify(payload) });
    cerrarModal("modal-op-garantia-nueva");
    toast("Garantía creada.");
    await cargarGarantiasTab();
    cargarDashboardOperativo();
    // Igual que al crear una OT: por obligación se piden las 7 fotos fijas
    // del vehículo también cuando entra por garantía (spec explícita del
    // usuario -- "este flujo también tiene que hacerse en las garantías").
    abrirFotosIngreso("garantia", nueva.id);
  } catch (e2) {
    toast(e2.message, true);
    // Igual que en "Nueva orden": si el bloqueo apareció justo entre la
    // selección del vehículo y el envío, se refresca el aviso con su link.
    chequearBloqueoEquipo(payload.equipo_id, "garantia", "garn-equipo-bloqueo");
  }
});

// ---- Detalle de garantía ----

const GAR_TIPO_ITEM_LABEL = { repuesto: "Repuesto", mano_obra: "Mano de obra", convertidor_torque: "Convertidor de torque" };

function renderItemsGarantia(garantia) {
  const tbody = document.getElementById("tbody-gar-items");
  if (garantia.items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin ítems todavía.</td></tr>`;
  } else {
    tbody.innerHTML = garantia.items.map(it => `
      <tr>
        <td>${GAR_TIPO_ITEM_LABEL[it.tipo] || it.tipo}${it.aplica_iva ? ' <span style="font-size:11px; color:var(--accent-2); border:1px solid var(--accent-2); border-radius:4px; padding:1px 4px;">+IVA</span>' : ""}</td>
        <td>${escapeHtml(it.descripcion)}${it.detalle ? `<br><span class="ayuda-inline" style="margin:0">${escapeHtml(it.detalle)}</span>` : ""}${it.tipo === "convertidor_torque" && it.convertidor_id ? `<br><a href="/convertidores?ver=${it.convertidor_id}#convertidores" class="ayuda-inline">Ver convertidor #${it.convertidor_id}</a>` : ""}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.subtotal !== undefined ? it.subtotal : it.cantidad * it.costo_unitario)}</td>
        <td><button type="button" class="btn-link btn-link--peligro" data-eliminar-gar-item="${it.id}">Eliminar</button></td>
      </tr>
    `).join("");
  }
  document.getElementById("gard-total").innerHTML = `<strong>${fmtMoney(garantia.total)}</strong>`;
  tbody.querySelectorAll("[data-eliminar-gar-item]").forEach(btn => {
    btn.addEventListener("click", () => eliminarItemGarantia(parseInt(btn.dataset.eliminarGarItem, 10)));
  });
}

async function abrirDetalleGarantia(id) {
  try {
    const garantia = await api(`/api/operativo/garantias/${id}`);
    document.getElementById("gard-id").value = id;
    document.getElementById("gard-titulo").textContent = `Garantía #${id}`;
    document.getElementById("gard-cliente-equipo").textContent =
      `${garantia.cliente ? garantia.cliente.nombre : "Sin cliente"}` +
      (garantia.equipo ? ` — ${[garantia.equipo.marca, garantia.equipo.modelo, garantia.equipo.identificador].filter(Boolean).join(" ")}` : "");
    document.getElementById("gard-estado").value = garantia.estado;
    document.getElementById("gard-responsable").value = garantia.responsable || "";
    document.getElementById("gard-motivo").value = garantia.motivo || "";
    document.getElementById("gard-diagnostico").value = garantia.diagnostico || "";
    renderItemsGarantia(garantia);

    if (state.opProductos.length === 0) {
      const dataProductos = await api("/api/productos");
      state.opProductos = dataProductos.productos;
    }
    document.getElementById("gari-producto-buscar").value = "";
    document.getElementById("gari-producto-id").value = "";
    document.getElementById("gari-producto-resultados").style.display = "none";
    document.getElementById("form-gar-item").reset();
    document.getElementById("gari-tipo").dispatchEvent(new Event("change"));

    await cargarFotosGarantiaDetalle();

    abrirModal("modal-op-garantia-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

// Igual patrón que "Carga de repuestos" de la OT: clic en un resultado
// agrega el ítem al tiro sin cerrar la lista.
function renderResultadosGari(texto) {
  const resultados = document.getElementById("gari-producto-resultados");
  const q = texto.trim();
  if (!q) {
    resultados.style.display = "none";
    return;
  }
  const coincidencias = state.opProductos.filter(p =>
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
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const p = state.opProductos.find(x => x.id === parseInt(item.dataset.id, 10));
      if (p) agregarRepuestoGarantiaDesdeAutocomplete(p);
    });
  });
}

async function agregarRepuestoGarantiaDesdeAutocomplete(producto) {
  const garantiaId = document.getElementById("gard-id").value;
  const cantidad = document.getElementById("gari-cantidad").value || 1;
  const textoBusqueda = document.getElementById("gari-producto-buscar").value;
  try {
    await api(`/api/operativo/garantias/${garantiaId}/items`, {
      method: "POST",
      body: JSON.stringify({ tipo: "repuesto", producto_id: producto.id, cantidad }),
    });
    toast(`Agregado: ${producto.nombre}`);
    const garantia = await api(`/api/operativo/garantias/${garantiaId}`);
    renderItemsGarantia(garantia);
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
    renderResultadosGari(textoBusqueda);
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("gari-producto-buscar").addEventListener("input", (e) => {
  document.getElementById("gari-producto-id").value = "";
  renderResultadosGari(e.target.value);
});
document.getElementById("gari-producto-buscar").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosGari(e.target.value);
});
document.getElementById("gari-producto-buscar").addEventListener("blur", () => {
  setTimeout(() => {
    document.getElementById("gari-producto-resultados").style.display = "none";
  }, 150);
});

// Elimina la garantía completa (no solo un ítem suelto) — igual que
// "Eliminar orden" en las OT. El backend devuelve al inventario el stock
// de los repuestos cargados como ítems de la garantía.
document.getElementById("btn-gard-eliminar-garantia").addEventListener("click", async () => {
  const id = document.getElementById("gard-id").value;
  if (!id) return;
  if (!confirm(`¿Eliminar la garantía #${id} por completo? Esta acción no se puede deshacer. Se devolverá al inventario el stock de los repuestos que tenga cargados.`)) return;
  try {
    await api(`/api/operativo/garantias/${id}`, { method: "DELETE" });
    toast("Garantía eliminada.");
    cerrarModal("modal-op-garantia-detalle");
    await cargarGarantiasTab();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-gard-guardar").addEventListener("click", async () => {
  const id = document.getElementById("gard-id").value;
  const payload = {
    estado: document.getElementById("gard-estado").value,
    responsable: document.getElementById("gard-responsable").value.trim(),
    motivo: document.getElementById("gard-motivo").value.trim(),
    diagnostico: document.getElementById("gard-diagnostico").value.trim(),
  };
  try {
    await api(`/api/operativo/garantias/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Garantía actualizada.");
    cargarGarantiasTab();
    cargarDashboardOperativo();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("gari-tipo").addEventListener("change", (e) => {
  const esRepuesto = e.target.value === "repuesto";
  document.getElementById("gari-campo-producto").style.display = esRepuesto ? "flex" : "none";
  document.getElementById("gari-campo-descripcion").style.display = esRepuesto ? "none" : "flex";
  document.getElementById("gari-campo-costo").style.display = esRepuesto ? "none" : "flex";
  document.getElementById("gari-fila-mo-extra").style.display = esRepuesto ? "none" : "flex";
});

document.getElementById("form-gar-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tipo = document.getElementById("gari-tipo").value;
  const garantiaId = document.getElementById("gard-id").value;
  const payload = {
    tipo,
    cantidad: document.getElementById("gari-cantidad").value,
  };
  if (tipo === "repuesto") {
    payload.producto_id = document.getElementById("gari-producto-id").value;
    if (!payload.producto_id) {
      toast("Busca el producto y haz clic en él para agregarlo.", true);
      return;
    }
  } else {
    payload.descripcion = document.getElementById("gari-descripcion").value.trim();
    payload.costo_unitario = limpiarMonto(document.getElementById("gari-costo").value) || 0;
    payload.detalle = document.getElementById("gari-mo-detalle").value.trim();
    payload.aplica_iva = document.getElementById("gari-mo-iva").checked;
    if (!payload.descripcion) {
      toast("Describe la mano de obra realizada.", true);
      return;
    }
  }
  try {
    await api(`/api/operativo/garantias/${garantiaId}/items`, { method: "POST", body: JSON.stringify(payload) });
    toast("Ítem agregado.");
    const garantia = await api(`/api/operativo/garantias/${garantiaId}`);
    renderItemsGarantia(garantia);
    document.getElementById("form-gar-item").reset();
    document.getElementById("gari-producto-buscar").value = "";
    document.getElementById("gari-producto-id").value = "";
    document.getElementById("gari-tipo").dispatchEvent(new Event("change"));
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarItemGarantia(itemId) {
  if (!confirm("¿Eliminar este ítem? Si era un repuesto, el stock consumido se devuelve al inventario.")) return;
  const garantiaId = document.getElementById("gard-id").value;
  try {
    await api(`/api/operativo/garantias/${garantiaId}/items/${itemId}`, { method: "DELETE" });
    toast("Ítem eliminado.");
    const garantia = await api(`/api/operativo/garantias/${garantiaId}`);
    renderItemsGarantia(garantia);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---- Reportes: garantías ----

function rangoFechasGarantiasOperativo() {
  const desde = document.getElementById("rep-garantias-desde").value;
  const hasta = document.getElementById("rep-garantias-hasta").value;
  const params = new URLSearchParams();
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);
  return params.toString();
}

document.getElementById("btn-rep-garantias-excel").addEventListener("click", () => {
  window.location.href = `/api/operativo/reportes/garantias?${rangoFechasGarantiasOperativo()}`;
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarDashboardOperativo();
