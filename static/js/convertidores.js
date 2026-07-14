// ---------------------------------------------------------------------------
// Convertidores de Torque — módulo top-level aparte (ver
// modulos/convertidores.py). Página independiente con su propio dashboard,
// tablero kanban y reportes. Sigue la misma convención de todo el proyecto:
// cada módulo duplica sus propias utilidades en vez de compartir un JS común
// (la única excepción real es charts.js).
// ---------------------------------------------------------------------------

const state = {
  sesion: { username: null, nombre: null },
  opConvertidores: [],
  opClientes: [],
  opOrdenes: [],
  opProductos: [],
};

let opArrastrandoTarjeta = false;

// ---------------------------------------------------------------------------
// Utilidades compartidas (duplicadas de operativo.js: cada módulo carga su
// propio JS)
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

const CAMPOS_SIN_MAYUSCULAS = new Set([
  "convn-numero-serie", "opc-telefono", "opc-rut",
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
// Pestañas: Dashboard / Convertidores / Reportes
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-dashboard").style.display = vista === "dashboard" ? "block" : "none";
    document.getElementById("vista-convertidores").style.display = vista === "convertidores" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      dashboard: "Convertidores de Torque — Dashboard",
      convertidores: "Convertidores de Torque — Tablero",
      reportes: "Convertidores de Torque — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "dashboard") cargarDashboardConvertidores();
    if (vista === "convertidores") cargarConvertidoresTab();
    if (vista === "reportes") cargarResumenReportesConvertidores();
  });
});

// El sidebar enlaza a /convertidores#vista (ver _sidebar.html). Como los
// tabs se manejan 100% en JS, hay que leer el hash al cargar la página y
// también reaccionar si cambia estando ya en esta vista.
function activarPestanaDesdeHash() {
  const vista = location.hash.replace("#", "");
  if (!vista) return;
  const tabBtn = document.querySelector(`.tab[data-vista="${vista}"]`);
  if (tabBtn) tabBtn.click();
}
window.addEventListener("hashchange", activarPestanaDesdeHash);
activarPestanaDesdeHash();

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

async function cargarDashboardConvertidores() {
  try {
    const d = await api("/api/convertidores/dashboard");
    document.getElementById("conv-s-abiertos").textContent = d.abiertos;
    document.getElementById("conv-s-valor").textContent = fmtMoney(d.valor_en_reparacion);
    document.getElementById("conv-s-regionales").textContent = d.regionales_pendientes;
    document.getElementById("conv-s-tiempo").textContent = d.tiempo_promedio_cierre_mes_dias != null
      ? `${d.tiempo_promedio_cierre_mes_dias} día(s)` : "—";
    document.getElementById("conv-s-total").textContent = d.total;

    const tablaEstado = document.getElementById("conv-dash-tabla-estado");
    tablaEstado.innerHTML = Object.entries(d.por_estado).map(([estado, n]) => `
      <tr><td>${escapeHtml(d.por_estado_label[estado] || estado)}</td><td class="num">${n}</td></tr>
    `).join("");

    const tablaOrigen = document.getElementById("conv-dash-tabla-origen");
    tablaOrigen.innerHTML = Object.entries(d.por_origen).map(([origen, n]) => `
      <tr><td>${escapeHtml(d.por_origen_label[origen] || origen)}</td><td class="num">${n}</td></tr>
    `).join("");
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Convertidores de torque — tablero kanban + modal de creación + modal de
// detalle con ítems. "instalado" es un estado que solo asigna el sistema
// (al agregarlo como repuesto en la OT de un auto, ver
// modulos/operativo.py: agregar_item_orden).
// ---------------------------------------------------------------------------

const CONV_ESTADO_LABEL = {
  ingreso: "Ingreso", esperando_aprobacion: "Esperando aprobación", reparacion: "Reparación",
  esperando_repuesto: "Esperando repuesto", listo: "Listo para instalar",
  instalado: "Instalado", entregado: "Entregado", cancelado: "Cancelado",
};

const CONV_ESTADOS_TABLERO = [
  "ingreso", "esperando_aprobacion", "reparacion", "esperando_repuesto", "listo",
  "instalado", "entregado", "cancelado",
];

const CONV_ORIGEN_LABEL = {
  cliente_directo: "Cliente trae el convertidor",
  stock_taller: "Stock del taller",
  desde_ot_auto: "Desde la caja de un auto",
};

// Etiquetas cortas para el badge de la tarjeta del tablero (pedido del
// taller: distinguir de un vistazo si es stock, interno o externo).
const CONV_ORIGEN_BADGE = {
  cliente_directo: "Externo",
  stock_taller: "Stock",
  desde_ot_auto: "Interno",
};

async function cargarConvertidoresTab() {
  try {
    const data = await api("/api/convertidores");
    state.opConvertidores = data.convertidores;
    renderTableroConvertidores();
    if (!convVerAutoAbierto) {
      const params = new URLSearchParams(location.search);
      const verId = params.get("ver");
      if (verId) {
        convVerAutoAbierto = true;
        abrirDetalleConvertidor(parseInt(verId, 10));
      }
    }
  } catch (e) {
    toast(e.message, true);
  }
}

let convVerAutoAbierto = false;

function renderTableroConvertidores() {
  const filtroEstado = document.getElementById("conv-filtro-estado").value;
  const busqueda = document.getElementById("conv-buscar").value.trim().toLowerCase();

  document.getElementById("conv-columna-instalado").style.display = filtroEstado === "instalado" ? "flex" : "none";
  document.getElementById("conv-columna-cancelado").style.display = filtroEstado === "cancelado" ? "flex" : "none";
  document.querySelectorAll("#conv-kanban .op-columna").forEach(col => {
    const est = col.dataset.estado;
    if (filtroEstado) {
      col.style.display = est === filtroEstado ? "flex" : "none";
    } else {
      col.style.display = (est === "instalado" || est === "cancelado") ? "none" : "flex";
    }
  });

  const coincide = (c) => coincideBusqueda(
    busqueda,
    c.cliente ? c.cliente.nombre : "",
    c.marca || "", c.modelo || "", c.numero_serie || "", c.motivo || "",
  );

  CONV_ESTADOS_TABLERO.forEach(estado => {
    const cont = document.getElementById(`conv-cards-${estado}`);
    const contador = document.getElementById(`conv-count-${estado}`);
    const convertidores = (state.opConvertidores || []).filter(c => c.estado === estado && coincide(c));
    contador.textContent = convertidores.length;
    if (convertidores.length === 0) {
      cont.innerHTML = `<p class="vacio-inline">Sin convertidores.</p>`;
      return;
    }
    cont.innerHTML = convertidores.map(c => `
      <div class="op-card${c.es_regional ? " op-card--regional" : ""}" data-id="${c.id}" draggable="true">
        <div class="op-card-top">
          <span class="op-card-ot">Convertidor #${c.id}</span>
          <span class="op-card-origen-badge op-card-origen-badge--${c.origen_tipo}">${CONV_ORIGEN_BADGE[c.origen_tipo] || c.origen_tipo}</span>
        </div>
        <div class="op-card-cliente">${escapeHtml(c.cliente ? c.cliente.nombre : CONV_ORIGEN_LABEL[c.origen_tipo] || "Sin cliente")}</div>
        ${(c.marca || c.modelo) ? `<div class="op-card-equipo">${escapeHtml([c.marca, c.modelo].filter(Boolean).join(" · "))}${c.numero_serie ? ` — N/S ${escapeHtml(c.numero_serie)}` : ""}</div>` : ""}
        <div class="op-card-descripcion">${escapeHtml(c.motivo || "")}</div>
        ${c.costo_estimado ? `<div class="op-card-valor">${fmtMoney(c.costo_estimado)}</div>` : ""}
        ${c.es_regional ? `<div class="op-card-meta"><span class="badge-regional">De región — enviar al terminar</span></div>` : ""}
        <div class="op-card-meta">
          <span>Ingreso: ${fmtFechaSolo((c.fecha_recepcion || "").slice(0, 10))}</span>
          ${c.responsable ? `<span>${escapeHtml(c.responsable)}</span>` : ""}
        </div>
      </div>
    `).join("");
  });

  document.querySelectorAll("#conv-kanban .op-card").forEach(card => {
    card.addEventListener("click", () => abrirDetalleConvertidor(parseInt(card.dataset.id, 10)));
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

document.querySelectorAll("#conv-kanban .op-columna").forEach(columna => {
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
    const convertidorId = parseInt(e.dataTransfer.getData("text/plain"), 10);
    const nuevoEstado = columna.dataset.estado;
    if (!convertidorId || !nuevoEstado) return;
    const convertidor = (state.opConvertidores || []).find(c => c.id === convertidorId);
    if (convertidor && convertidor.estado === nuevoEstado) return;
    try {
      await api(`/api/convertidores/${convertidorId}`, { method: "PUT", body: JSON.stringify({ estado: nuevoEstado }) });
      toast(`Convertidor #${convertidorId} movido a "${CONV_ESTADO_LABEL[nuevoEstado] || nuevoEstado}".`);
      await cargarConvertidoresTab();
    } catch (err) {
      toast(err.message, true);
    }
  });
});

document.getElementById("conv-filtro-estado").addEventListener("change", renderTableroConvertidores);
document.getElementById("conv-buscar").addEventListener("input", renderTableroConvertidores);

// Auto-scroll del tablero mientras se arrastra una tarjeta: si el mouse pasa
// cerca del borde izquierdo/derecho del tablero, este se desplaza solo para
// ese lado -- mismo criterio que en operativo.js (#op-kanban / #gar-kanban),
// duplicado acá porque cada módulo carga su propio JS.
function activarAutoScrollKanban(kanbanId) {
  const kanban = document.getElementById(kanbanId);
  if (!kanban) return;
  const ZONA_PX = 70;
  const VELOCIDAD_PX = 14;
  let direccion = 0;
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
    if (!kanban.contains(e.relatedTarget)) detener();
  });
  kanban.addEventListener("drop", detener);
  document.addEventListener("dragend", detener);
}
activarAutoScrollKanban("conv-kanban");

// ---- Nuevo convertidor ----

function actualizarCamposOrigenConvertidor() {
  const origen = document.getElementById("convn-origen-tipo").value;
  document.getElementById("convn-campo-cliente").style.display = origen === "cliente_directo" ? "flex" : "none";
  document.getElementById("convn-campo-equipo").style.display = origen === "cliente_directo" ? "flex" : "none";
  document.getElementById("convn-campo-orden").style.display = origen === "desde_ot_auto" ? "flex" : "none";
  document.getElementById("convn-cliente-id").required = origen === "cliente_directo";
  document.getElementById("convn-orden-origen-id").required = origen === "desde_ot_auto";
  document.getElementById("convn-campo-regional").style.display = origen === "cliente_directo" ? "flex" : "none";
  if (origen !== "cliente_directo") document.getElementById("convn-es-regional").checked = false;
}

function renderSelectOrdenesConvertidor() {
  const select = document.getElementById("convn-orden-origen-id");
  const abiertas = (state.opOrdenes || []).filter(o => !["entregado", "cancelado"].includes(o.estado));
  select.innerHTML = '<option value="">Selecciona la OT…</option>' +
    abiertas.map(o => `<option value="${o.id}">OT #${o.id} — ${escapeHtml(o.cliente ? o.cliente.nombre : "")} — ${fmtFechaSolo((o.fecha_recepcion || "").slice(0, 10))}</option>`).join("");
}

async function abrirNuevoConvertidor() {
  document.getElementById("form-op-convertidor-nuevo").reset();
  if (state.opClientes.length === 0) {
    const dataClientes = await api("/api/operativo/clientes");
    state.opClientes = dataClientes.clientes;
  }
  renderSelectClientesOperativo("convn-cliente-id", "");
  document.getElementById("convn-equipo-id").innerHTML = '<option value="">Sin vehículo asociado</option>';
  // state.opOrdenes se carga bajo demanda: esta página no tiene una pestaña
  // de "Órdenes de Trabajo" que la deje cargada como efecto secundario (a
  // diferencia de cuando Convertidores vivía dentro de Operativo), así que
  // se trae acá mismo, justo antes de armar el selector.
  const dataOrdenes = await api("/api/operativo/ordenes?mes=todos");
  state.opOrdenes = dataOrdenes.ordenes;
  renderSelectOrdenesConvertidor();
  actualizarCamposOrigenConvertidor();
  abrirModal("modal-op-convertidor-nuevo");
}

document.getElementById("btn-conv-nuevo").addEventListener("click", abrirNuevoConvertidor);
document.getElementById("convn-origen-tipo").addEventListener("change", actualizarCamposOrigenConvertidor);

document.getElementById("convn-cliente-id").addEventListener("change", async (e) => {
  const clienteId = e.target.value;
  const select = document.getElementById("convn-equipo-id");
  if (!clienteId) {
    select.innerHTML = '<option value="">Sin vehículo asociado</option>';
    return;
  }
  const dataEquipos = await api(`/api/operativo/equipos?cliente_id=${clienteId}`);
  select.innerHTML = '<option value="">Sin vehículo asociado</option>' +
    dataEquipos.equipos.map(eq => `<option value="${eq.id}">${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</option>`).join("");
});

document.getElementById("convn-nuevo-cliente").addEventListener("click", () => {
  opClienteCallback = async (nuevo) => {
    const data = await api("/api/operativo/clientes");
    state.opClientes = data.clientes;
    renderSelectClientesOperativo("convn-cliente-id", nuevo.id);
    document.getElementById("convn-cliente-id").dispatchEvent(new Event("change"));
  };
  abrirNuevoClienteOperativo();
});

document.getElementById("form-op-convertidor-nuevo").addEventListener("submit", async (e) => {
  e.preventDefault();
  const origenTipo = document.getElementById("convn-origen-tipo").value;
  const payload = {
    origen_tipo: origenTipo,
    marca: document.getElementById("convn-marca").value.trim(),
    modelo: document.getElementById("convn-modelo").value.trim(),
    numero_serie: document.getElementById("convn-numero-serie").value.trim(),
    motivo: document.getElementById("convn-motivo").value.trim(),
  };
  if (origenTipo === "cliente_directo") {
    payload.cliente_id = document.getElementById("convn-cliente-id").value;
    payload.equipo_id = document.getElementById("convn-equipo-id").value || null;
    payload.es_regional = document.getElementById("convn-es-regional").checked;
    if (!payload.cliente_id) {
      toast("Selecciona el cliente que trae el convertidor.", true);
      return;
    }
  } else if (origenTipo === "desde_ot_auto") {
    payload.orden_origen_id = document.getElementById("convn-orden-origen-id").value;
    if (!payload.orden_origen_id) {
      toast("Selecciona la OT del auto.", true);
      return;
    }
  }
  try {
    await api("/api/convertidores", { method: "POST", body: JSON.stringify(payload) });
    toast("Convertidor creado.");
    cerrarModal("modal-op-convertidor-nuevo");
    await cargarConvertidoresTab();
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---- Detalle de convertidor ----

function renderItemsConvertidor(convertidor) {
  const tbody = document.getElementById("tbody-conv-items");
  if (convertidor.items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin ítems todavía.</td></tr>`;
  } else {
    const TIPO_LABEL = { repuesto: "Repuesto", mano_obra: "Mano de obra", convertidor: "Convertidor de stock" };
    tbody.innerHTML = convertidor.items.map(it => `
      <tr>
        <td>${TIPO_LABEL[it.tipo] || it.tipo}${it.aplica_iva ? ' <span style="font-size:11px; color:var(--accent-2); border:1px solid var(--accent-2); border-radius:4px; padding:1px 4px;">+IVA</span>' : ""}</td>
        <td>${escapeHtml(it.descripcion)}${it.detalle ? `<br><span class="ayuda-inline" style="margin:0">${escapeHtml(it.detalle)}</span>` : ""}${it.tipo === "convertidor" && it.convertidor_hijo ? `<br><a href="/convertidores?ver=${it.convertidor_hijo.id}" class="ayuda-inline">Ver convertidor #${it.convertidor_hijo.id}</a> — costo en vivo` : ""}</td>
        <td class="num">${it.tipo === "convertidor" ? "—" : it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.subtotal !== undefined ? it.subtotal : it.cantidad * it.costo_unitario)}</td>
        <td><button type="button" class="btn-link btn-link--peligro" data-eliminar-conv-item="${it.id}">Eliminar</button></td>
      </tr>
    `).join("");
  }
  document.getElementById("convd-total").innerHTML = `<strong>${fmtMoney(convertidor.total_reparacion)}</strong>`;
  tbody.querySelectorAll("[data-eliminar-conv-item]").forEach(btn => {
    btn.addEventListener("click", () => eliminarItemConvertidor(parseInt(btn.dataset.eliminarConvItem, 10)));
  });
}

async function abrirDetalleConvertidor(id) {
  try {
    const convertidor = await api(`/api/convertidores/${id}`);
    document.getElementById("convd-id").value = id;
    document.getElementById("convd-titulo").textContent = `Convertidor #${id}`;
    const origenTxt = CONV_ORIGEN_LABEL[convertidor.origen_tipo] || convertidor.origen_tipo;
    const clienteTxt = convertidor.cliente ? ` — ${convertidor.cliente.nombre}` : "";
    const equipoTxt = convertidor.equipo ? ` (${[convertidor.equipo.marca, convertidor.equipo.modelo].filter(Boolean).join(" ")})` : "";
    document.getElementById("convd-origen-info").textContent = `${origenTxt}${clienteTxt}${equipoTxt}`;
    if (convertidor.instalado_en_orden_id) {
      document.getElementById("convd-instalado-info").style.display = "";
      document.getElementById("convd-instalado-info").textContent = convertidor.origen_tipo === "desde_ot_auto"
        ? `Cargado automáticamente como costo en su OT de origen (OT #${convertidor.instalado_en_orden_id}).`
        : `Instalado en la OT #${convertidor.instalado_en_orden_id}.`;
    } else if (convertidor.instalado_en_convertidor_id) {
      document.getElementById("convd-instalado-info").style.display = "";
      document.getElementById("convd-instalado-info").innerHTML =
        `Cargado como repuesto del <a href="/convertidores?ver=${convertidor.instalado_en_convertidor_id}">convertidor #${convertidor.instalado_en_convertidor_id}</a>.`;
    } else {
      document.getElementById("convd-instalado-info").style.display = "none";
    }
    document.getElementById("convd-campo-regional").style.display = convertidor.origen_tipo === "cliente_directo" ? "flex" : "none";
    document.getElementById("convd-es-regional").checked = !!convertidor.es_regional;
    document.getElementById("convd-estado").value = convertidor.estado;
    document.getElementById("convd-responsable").value = convertidor.responsable || "";
    document.getElementById("convd-marca").value = convertidor.marca || "";
    document.getElementById("convd-modelo").value = convertidor.modelo || "";
    document.getElementById("convd-numero-serie").value = convertidor.numero_serie || "";
    document.getElementById("convd-motivo").value = convertidor.motivo || "";
    document.getElementById("convd-diagnostico").value = convertidor.diagnostico || "";
    document.getElementById("convd-precio-venta").value = convertidor.precio_venta != null ? fmtMoney(convertidor.precio_venta).replace(/[^0-9]/g, "") : "";
    renderItemsConvertidor(convertidor);

    if (state.opProductos.length === 0) {
      const dataProductos = await api("/api/productos");
      state.opProductos = dataProductos.productos;
    }
    document.getElementById("convi-producto-buscar").value = "";
    document.getElementById("convi-producto-id").value = "";
    document.getElementById("convi-producto-resultados").style.display = "none";
    document.getElementById("form-conv-item").reset();
    await renderSelectConvertidoresStockDisponibles(id);
    document.getElementById("convi-tipo").dispatchEvent(new Event("change"));

    abrirModal("modal-op-convertidor-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

// Unidades de STOCK "listas para instalar" disponibles para usar como
// repuesto de ESTE convertidor (tipo de ítem "convertidor") -- se excluye
// la propia unidad para que no se pueda ofrecer a sí misma.
async function renderSelectConvertidoresStockDisponibles(convertidorId) {
  const select = document.getElementById("convi-convertidor-hijo-id");
  try {
    const data = await api(`/api/convertidores/disponibles?origen=stock_taller&excluir_id=${convertidorId}`);
    select.innerHTML = '<option value="">Selecciona una unidad de stock…</option>' +
      data.convertidores.map(c => `<option value="${c.id}">Convertidor #${c.id} — ${escapeHtml([c.marca, c.modelo].filter(Boolean).join(" ") || "Sin marca/modelo")} — ${fmtMoney(c.costo_total)}</option>`).join("");
  } catch (e) {
    select.innerHTML = '<option value="">No se pudo cargar el stock disponible</option>';
  }
}

function renderResultadosConvi(texto) {
  const resultados = document.getElementById("convi-producto-resultados");
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
      if (p) agregarRepuestoConvertidorDesdeAutocomplete(p);
    });
  });
}

async function agregarRepuestoConvertidorDesdeAutocomplete(producto) {
  const convertidorId = document.getElementById("convd-id").value;
  const cantidad = document.getElementById("convi-cantidad").value || 1;
  const textoBusqueda = document.getElementById("convi-producto-buscar").value;
  try {
    await api(`/api/convertidores/${convertidorId}/items`, {
      method: "POST",
      body: JSON.stringify({ tipo: "repuesto", producto_id: producto.id, cantidad }),
    });
    toast(`Agregado: ${producto.nombre}`);
    const convertidor = await api(`/api/convertidores/${convertidorId}`);
    renderItemsConvertidor(convertidor);
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
    renderResultadosConvi(textoBusqueda);
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("convi-producto-buscar").addEventListener("input", (e) => {
  document.getElementById("convi-producto-id").value = "";
  renderResultadosConvi(e.target.value);
});
document.getElementById("convi-producto-buscar").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosConvi(e.target.value);
});
document.getElementById("convi-producto-buscar").addEventListener("blur", () => {
  setTimeout(() => {
    document.getElementById("convi-producto-resultados").style.display = "none";
  }, 150);
});

document.getElementById("btn-convd-eliminar").addEventListener("click", async () => {
  const id = document.getElementById("convd-id").value;
  if (!id) return;
  if (!confirm(`¿Eliminar el convertidor #${id} por completo? Esta acción no se puede deshacer. Se devolverá al inventario el stock de los repuestos que tenga cargados.`)) return;
  try {
    await api(`/api/convertidores/${id}`, { method: "DELETE" });
    toast("Convertidor eliminado.");
    cerrarModal("modal-op-convertidor-detalle");
    await cargarConvertidoresTab();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-convd-guardar").addEventListener("click", async () => {
  const id = document.getElementById("convd-id").value;
  const payload = {
    estado: document.getElementById("convd-estado").value,
    responsable: document.getElementById("convd-responsable").value.trim(),
    marca: document.getElementById("convd-marca").value.trim(),
    modelo: document.getElementById("convd-modelo").value.trim(),
    numero_serie: document.getElementById("convd-numero-serie").value.trim(),
    motivo: document.getElementById("convd-motivo").value.trim(),
    diagnostico: document.getElementById("convd-diagnostico").value.trim(),
    precio_venta: limpiarMonto(document.getElementById("convd-precio-venta").value) || null,
    es_regional: document.getElementById("convd-es-regional").checked,
  };
  try {
    await api(`/api/convertidores/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Convertidor actualizado.");
    cargarConvertidoresTab();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("convi-tipo").addEventListener("change", (e) => {
  const tipo = e.target.value;
  document.getElementById("convi-campo-producto").style.display = tipo === "repuesto" ? "flex" : "none";
  document.getElementById("convi-campo-descripcion").style.display = tipo === "mano_obra" ? "flex" : "none";
  document.getElementById("convi-campo-costo").style.display = tipo === "mano_obra" ? "flex" : "none";
  document.getElementById("convi-fila-mo-extra").style.display = tipo === "mano_obra" ? "flex" : "none";
  document.getElementById("convi-campo-convertidor-hijo").style.display = tipo === "convertidor" ? "flex" : "none";
  // Un convertidor no tiene "cantidad" -- siempre es 1, así que se oculta
  // el campo para esa opción (el backend igual la fuerza a 1).
  document.getElementById("convi-campo-cantidad").style.display = tipo === "convertidor" ? "none" : "flex";
});

document.getElementById("form-conv-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tipo = document.getElementById("convi-tipo").value;
  const convertidorId = document.getElementById("convd-id").value;
  const payload = { tipo };
  if (tipo === "repuesto") {
    payload.cantidad = document.getElementById("convi-cantidad").value;
    payload.producto_id = document.getElementById("convi-producto-id").value;
    if (!payload.producto_id) {
      toast("Busca el producto y haz clic en él para agregarlo.", true);
      return;
    }
  } else if (tipo === "mano_obra") {
    payload.cantidad = document.getElementById("convi-cantidad").value;
    payload.descripcion = document.getElementById("convi-descripcion").value.trim();
    payload.costo_unitario = limpiarMonto(document.getElementById("convi-costo").value) || 0;
    payload.detalle = document.getElementById("convi-mo-detalle").value.trim();
    payload.aplica_iva = document.getElementById("convi-mo-iva").checked;
    if (!payload.descripcion) {
      toast("Describe la mano de obra realizada.", true);
      return;
    }
  } else if (tipo === "convertidor") {
    payload.convertidor_hijo_id = document.getElementById("convi-convertidor-hijo-id").value;
    if (!payload.convertidor_hijo_id) {
      toast("Selecciona el convertidor de stock que vas a usar como repuesto.", true);
      return;
    }
  }
  try {
    await api(`/api/convertidores/${convertidorId}/items`, { method: "POST", body: JSON.stringify(payload) });
    toast("Ítem agregado.");
    const convertidor = await api(`/api/convertidores/${convertidorId}`);
    renderItemsConvertidor(convertidor);
    document.getElementById("form-conv-item").reset();
    document.getElementById("convi-producto-buscar").value = "";
    document.getElementById("convi-producto-id").value = "";
    document.getElementById("convi-tipo").dispatchEvent(new Event("change"));
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
    if (tipo === "convertidor") {
      // La unidad recién usada ya no debe ofrecerse de nuevo -- refresca
      // el selector para que desaparezca de la lista.
      await renderSelectConvertidoresStockDisponibles(convertidorId);
    }
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarItemConvertidor(itemId) {
  if (!confirm("¿Eliminar este ítem? Si era un repuesto, el stock consumido se devuelve al inventario.")) return;
  const convertidorId = document.getElementById("convd-id").value;
  try {
    await api(`/api/convertidores/${convertidorId}/items/${itemId}`, { method: "DELETE" });
    toast("Ítem eliminado.");
    const convertidor = await api(`/api/convertidores/${convertidorId}`);
    renderItemsConvertidor(convertidor);
    // Si el ítem eliminado era un convertidor de stock, esa unidad volvió
    // a 'listo' -- refresca el selector para que vuelva a aparecer.
    await renderSelectConvertidoresStockDisponibles(convertidorId);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Cliente (modal reutilizado por "+ Nuevo" dentro del formulario de creación)
// ---------------------------------------------------------------------------

function renderSelectClientesOperativo(selectId, valorActual) {
  const select = document.getElementById(selectId);
  select.innerHTML = '<option value="">Selecciona un cliente…</option>' +
    state.opClientes.map(c => `<option value="${c.id}">${escapeHtml(c.nombre)}</option>`).join("");
  select.value = valorActual || "";
}

let opClienteCallback = null; // se llama con el cliente recién creado, según de dónde se abrió el modal

function abrirNuevoClienteOperativo() {
  document.getElementById("form-op-cliente").reset();
  document.getElementById("opc-id").value = "";
  document.getElementById("modal-op-cliente-titulo").textContent = "Nuevo cliente";
  abrirModal("modal-op-cliente");
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

// ---------------------------------------------------------------------------
// Reportes
// ---------------------------------------------------------------------------

function rangoFechasConvertidores() {
  const desde = document.getElementById("rep-conv-desde").value;
  const hasta = document.getElementById("rep-conv-hasta").value;
  const params = new URLSearchParams();
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);
  return params.toString();
}

async function cargarResumenReportesConvertidores() {
  try {
    const r = await api(`/api/convertidores/reportes/resumen?${rangoFechasConvertidores()}`);
    document.getElementById("rep-contador-conv").textContent =
      `${r.total} convertidor(es) en el rango seleccionado`;
  } catch (e) {
    document.getElementById("rep-contador-conv").textContent = "No se pudo calcular el resumen.";
  }
}

document.getElementById("rep-conv-desde").addEventListener("change", cargarResumenReportesConvertidores);
document.getElementById("rep-conv-hasta").addEventListener("change", cargarResumenReportesConvertidores);
document.getElementById("btn-rep-conv-excel").addEventListener("click", () => {
  window.location.href = `/api/convertidores/reportes/excel?${rangoFechasConvertidores()}`;
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarDashboardConvertidores();
