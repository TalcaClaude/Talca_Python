const state = {
  sesion: { username: null, nombre: null },
  opOrdenes: [],
  opClientes: [],
  opEquiposClienteActual: [],
  opOrdenActivaId: null,
  opProductos: [],
};

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
// Pestañas: Dashboard / Órdenes de Trabajo / Clientes / Reportes
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-dashboard").style.display = vista === "dashboard" ? "block" : "none";
    document.getElementById("vista-ordenes").style.display = vista === "ordenes" ? "block" : "none";
    document.getElementById("vista-clientes").style.display = vista === "clientes" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      dashboard: "Operativo — Dashboard",
      ordenes: "Operativo — Órdenes de Trabajo",
      clientes: "Operativo — Clientes",
      reportes: "Operativo — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "dashboard") cargarDashboardOperativo();
    if (vista === "ordenes") cargarOperativo();
    if (vista === "clientes") cargarClientesTab();
  });
});

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
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Módulo Operativo: órdenes de trabajo
// ---------------------------------------------------------------------------

const OP_ESTADOS_TABLERO = [
  "recibido", "diagnostico", "presupuesto", "esperando_aprobacion", "reparacion",
  "esperando_repuesto", "control_calidad", "listo", "entregado", "cancelado",
];

async function cargarOperativo() {
  try {
    const [dataOrdenes, dataClientes, dataProductos] = await Promise.all([
      api("/api/operativo/ordenes"),
      api("/api/operativo/clientes"),
      api("/api/productos"),
    ]);
    state.opOrdenes = dataOrdenes.ordenes;
    state.opClientes = dataClientes.clientes;
    state.opProductos = dataProductos.productos;
    renderTableroOperativo();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderTableroOperativo() {
  const filtroEstado = document.getElementById("op-filtro-estado").value;
  const busqueda = document.getElementById("op-buscar").value.trim().toLowerCase();

  document.getElementById("op-columna-cancelado").style.display = filtroEstado === "cancelado" ? "flex" : "none";
  document.querySelectorAll(".op-columna").forEach(col => {
    const est = col.dataset.estado;
    if (filtroEstado) {
      col.style.display = est === filtroEstado ? "flex" : "none";
    } else {
      col.style.display = est === "cancelado" ? "none" : "flex";
    }
  });

  const coincide = (orden) => {
    if (!busqueda) return true;
    const partes = [
      orden.cliente ? orden.cliente.nombre : "",
      orden.equipo ? `${orden.equipo.marca} ${orden.equipo.modelo} ${orden.equipo.identificador}` : "",
      orden.mecanico || "",
      orden.descripcion_problema || "",
    ].join(" ").toLowerCase();
    return partes.includes(busqueda);
  };

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
      <div class="op-card" data-id="${o.id}">
        <div class="op-card-cliente">${escapeHtml(o.cliente ? o.cliente.nombre : "Sin cliente")}
          ${o.prioridad && o.prioridad !== "media" ? `<span class="badge-prioridad badge-prioridad--${o.prioridad}">${escapeHtml(o.prioridad)}</span>` : ""}
        </div>
        ${o.equipo ? `<div class="op-card-equipo">${escapeHtml([o.equipo.marca, o.equipo.modelo, o.equipo.identificador].filter(Boolean).join(" · "))}</div>` : ""}
        ${o.descripcion_problema ? `<div class="op-card-descripcion">${escapeHtml(o.descripcion_problema)}</div>` : ""}
        <div class="op-card-meta">
          <span>${escapeHtml(o.mecanico || "Sin asignar")}</span>
          <span>${fmtFechaSolo((o.fecha_recepcion || "").slice(0, 10))}</span>
        </div>
      </div>
    `).join("");
  });

  document.querySelectorAll(".op-card").forEach(card => {
    card.addEventListener("click", () => abrirDetalleOrden(parseInt(card.dataset.id, 10)));
  });
}

document.getElementById("op-buscar").addEventListener("input", renderTableroOperativo);
document.getElementById("op-filtro-estado").addEventListener("change", renderTableroOperativo);

// ---- Clientes ----

function renderSelectClientesOperativo(selectId, valorActual) {
  const select = document.getElementById(selectId);
  select.innerHTML = '<option value="">Selecciona un cliente…</option>' +
    state.opClientes.map(c => `<option value="${c.id}">${escapeHtml(c.nombre)}</option>`).join("");
  select.value = valorActual || "";
}

let opClienteCallback = null; // si viene desde "+ Nuevo" del formulario de orden, para preseleccionar al guardar

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

async function renderSelectEquiposOperativo(clienteId, valorActual) {
  const select = document.getElementById("opon-equipo-id");
  if (!clienteId) {
    select.innerHTML = '<option value="">Sin equipo específico</option>';
    document.getElementById("opon-ver-historial").style.display = "none";
    return;
  }
  const data = await api(`/api/operativo/equipos?cliente_id=${clienteId}`);
  state.opEquiposClienteActual = data.equipos;
  select.innerHTML = '<option value="">Sin equipo específico</option>' +
    data.equipos.map(eq => `<option value="${eq.id}">${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</option>`).join("");
  select.value = valorActual || "";
  // select.value = ... no dispara "change", así que el link de historial se
  // actualiza acá también (cubre el caso de equipo recién creado inline).
  document.getElementById("opon-ver-historial").style.display = valorActual ? "" : "none";
}

document.getElementById("opon-cliente-id").addEventListener("change", (e) => {
  renderSelectEquiposOperativo(e.target.value || null);
});

document.getElementById("opon-equipo-id").addEventListener("change", (e) => {
  document.getElementById("opon-ver-historial").style.display = e.target.value ? "" : "none";
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
  document.getElementById("ope-id").value = "";
  renderSelectClienteEquipoModal(clienteId);
  document.getElementById("modal-op-equipo-titulo").textContent = "Nuevo equipo";
  opEquipoCallback = async (nuevo, payload) => {
    await renderSelectEquiposOperativo(payload.cliente_id, nuevo.id);
  };
  abrirModal("modal-op-equipo");
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
    marca: document.getElementById("ope-marca").value.trim(),
    modelo: document.getElementById("ope-modelo").value.trim(),
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

// ---- Nueva orden ----

document.getElementById("btn-op-nueva-orden").addEventListener("click", () => {
  document.getElementById("form-op-orden-nueva").reset();
  renderSelectClientesOperativo("opon-cliente-id", "");
  renderSelectEquiposOperativo(null);
  document.getElementById("opon-ver-historial").style.display = "none";
  abrirModal("modal-op-orden-nueva");
});

document.getElementById("form-op-orden-nueva").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    cliente_id: document.getElementById("opon-cliente-id").value,
    equipo_id: document.getElementById("opon-equipo-id").value || null,
    descripcion_problema: document.getElementById("opon-descripcion").value.trim(),
    mecanico: document.getElementById("opon-mecanico").value.trim(),
    fecha_entrega_estimada: document.getElementById("opon-fecha-estimada").value || null,
    prioridad: document.getElementById("opon-prioridad").value,
    vin: document.getElementById("opon-vin").value.trim(),
    kilometraje: document.getElementById("opon-kilometraje").value || null,
    anio: document.getElementById("opon-anio").value || null,
    caja_automatica: document.getElementById("opon-caja-automatica").value.trim(),
    accesorios: document.getElementById("opon-accesorios").value.trim(),
    estado_visual: document.getElementById("opon-estado-visual").value.trim(),
  };
  if (!payload.cliente_id) {
    toast("Selecciona un cliente.", true);
    return;
  }
  try {
    const nueva = await api("/api/operativo/ordenes", { method: "POST", body: JSON.stringify(payload) });
    cerrarModal("modal-op-orden-nueva");
    toast("Orden creada.");
    await cargarOperativo();
    abrirDetalleOrden(nueva.id);
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---- Detalle de orden ----

async function abrirDetalleOrden(id) {
  try {
    const orden = await api(`/api/operativo/ordenes/${id}`);
    state.opOrdenActivaId = id;
    document.getElementById("opod-id").value = id;
    document.getElementById("opod-titulo").textContent = `Orden #${id}`;
    document.getElementById("opod-cliente-equipo").textContent =
      `${orden.cliente ? orden.cliente.nombre : "Sin cliente"}` +
      (orden.equipo ? ` — ${[orden.equipo.marca, orden.equipo.modelo, orden.equipo.identificador].filter(Boolean).join(" ")}` : "");
    document.getElementById("opod-estado").value = orden.estado;
    document.getElementById("opod-mecanico").value = orden.mecanico || "";
    document.getElementById("opod-prioridad").value = orden.prioridad || "media";
    document.getElementById("opod-fecha-estimada").value = (orden.fecha_entrega_estimada || "").slice(0, 10);
    document.getElementById("opod-fecha-real").value = orden.fecha_entrega_real ? fmtFecha(orden.fecha_entrega_real) : "Todavía no";
    document.getElementById("opod-descripcion").value = orden.descripcion_problema || "";
    document.getElementById("opod-diagnostico").value = orden.diagnostico || "";

    // Recepción
    document.getElementById("opod-vin").value = orden.vin || "";
    document.getElementById("opod-kilometraje").value = orden.kilometraje ?? "";
    document.getElementById("opod-anio").value = orden.anio ?? "";
    document.getElementById("opod-caja-automatica").value = orden.caja_automatica || "";
    document.getElementById("opod-accesorios").value = orden.accesorios || "";
    document.getElementById("opod-estado-visual").value = orden.estado_visual || "";

    // Diagnóstico
    document.getElementById("opod-sintoma").value = orden.sintoma || "";
    document.getElementById("opod-diagnostico-tecnico").value = orden.diagnostico_tecnico || "";
    document.getElementById("opod-falla-detectada").value = orden.falla_detectada || "";
    document.getElementById("opod-componentes-danados").value = orden.componentes_danados || "";
    document.getElementById("opod-mediciones").value = orden.mediciones || "";
    document.getElementById("opod-resultado-pruebas").value = orden.resultado_pruebas || "";
    document.getElementById("opod-responsable-diagnostico").value = orden.responsable_diagnostico || "";

    // Ver historial del vehículo
    const btnHistorial = document.getElementById("btn-opod-ver-historial");
    if (orden.equipo) {
      btnHistorial.style.display = "";
      btnHistorial.onclick = () => abrirHistorialEquipo(orden.equipo.id);
    } else {
      btnHistorial.style.display = "none";
    }

    renderItemsOrden(orden);
    renderPresupuesto(orden);
    renderTiempos(orden);
    renderQcChecklist(orden);
    renderGarantias(orden);

    // Planificación / tiempos
    document.getElementById("opod-horas-presupuestadas").value = orden.horas_presupuestadas ?? "";

    // Entrega
    document.getElementById("ope-recibido-por").value = orden.entrega_recibido_por || "";
    document.getElementById("ope-documento").value = orden.entrega_documento || "";
    document.getElementById("ope-observaciones").value = orden.entrega_observaciones || "";

    if (state.opProductos.length === 0) {
      const dataProductos = await api("/api/productos");
      state.opProductos = dataProductos.productos;
    }
    const selectProducto = document.getElementById("opoi-producto-id");
    selectProducto.innerHTML = '<option value="">Selecciona un producto…</option>' +
      state.opProductos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)} (stock: ${p.stock_disponible})</option>`).join("");
    document.getElementById("form-op-item").reset();
    document.getElementById("opoi-tipo").dispatchEvent(new Event("change"));

    abrirModal("modal-op-orden-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

function renderItemsOrden(orden) {
  const tbody = document.getElementById("tbody-op-items");
  if (orden.items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin ítems todavía.</td></tr>`;
  } else {
    tbody.innerHTML = orden.items.map(it => `
      <tr>
        <td>${it.tipo === "repuesto" ? "Repuesto" : "Mano de obra"}</td>
        <td>${escapeHtml(it.descripcion)}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.cantidad * it.costo_unitario)}</td>
        <td><button type="button" class="btn-link btn-link--peligro" data-eliminar-item="${it.id}">Eliminar</button></td>
      </tr>
    `).join("");
  }
  document.getElementById("opod-total").innerHTML = `<strong>${fmtMoney(orden.total)}</strong>`;
  tbody.querySelectorAll("[data-eliminar-item]").forEach(btn => {
    btn.addEventListener("click", () => eliminarItemOrden(parseInt(btn.dataset.eliminarItem, 10)));
  });
}

document.getElementById("btn-opod-guardar").addEventListener("click", async () => {
  const id = document.getElementById("opod-id").value;
  const payload = {
    estado: document.getElementById("opod-estado").value,
    mecanico: document.getElementById("opod-mecanico").value.trim(),
    prioridad: document.getElementById("opod-prioridad").value,
    fecha_entrega_estimada: document.getElementById("opod-fecha-estimada").value || null,
    descripcion_problema: document.getElementById("opod-descripcion").value.trim(),
    diagnostico: document.getElementById("opod-diagnostico").value.trim(),
    vin: document.getElementById("opod-vin").value.trim(),
    kilometraje: document.getElementById("opod-kilometraje").value || null,
    anio: document.getElementById("opod-anio").value || null,
    caja_automatica: document.getElementById("opod-caja-automatica").value.trim(),
    accesorios: document.getElementById("opod-accesorios").value.trim(),
    estado_visual: document.getElementById("opod-estado-visual").value.trim(),
    sintoma: document.getElementById("opod-sintoma").value.trim(),
    diagnostico_tecnico: document.getElementById("opod-diagnostico-tecnico").value.trim(),
    falla_detectada: document.getElementById("opod-falla-detectada").value.trim(),
    componentes_danados: document.getElementById("opod-componentes-danados").value.trim(),
    mediciones: document.getElementById("opod-mediciones").value.trim(),
    resultado_pruebas: document.getElementById("opod-resultado-pruebas").value.trim(),
    responsable_diagnostico: document.getElementById("opod-responsable-diagnostico").value.trim(),
    horas_presupuestadas: document.getElementById("opod-horas-presupuestadas").value || null,
  };
  try {
    await api(`/api/operativo/ordenes/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Orden actualizada.");
    await cargarOperativo();
    abrirDetalleOrden(parseInt(id, 10));
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("opoi-tipo").addEventListener("change", (e) => {
  const esRepuesto = e.target.value === "repuesto";
  document.getElementById("opoi-campo-producto").style.display = esRepuesto ? "flex" : "none";
  document.getElementById("opoi-campo-descripcion").style.display = esRepuesto ? "none" : "flex";
  document.getElementById("opoi-campo-costo").style.display = esRepuesto ? "none" : "flex";
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
      toast("Selecciona un producto.", true);
      return;
    }
  } else {
    payload.descripcion = document.getElementById("opoi-descripcion").value.trim();
    payload.costo_unitario = document.getElementById("opoi-costo").value || 0;
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
    document.getElementById("form-op-item").reset();
    document.getElementById("opoi-tipo").dispatchEvent(new Event("change"));
    // Refresca el stock mostrado en el selector de productos si era un repuesto.
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
    const selectProducto = document.getElementById("opoi-producto-id");
    const valorActual = selectProducto.value;
    selectProducto.innerHTML = '<option value="">Selecciona un producto…</option>' +
      state.opProductos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)} (stock: ${p.stock_disponible})</option>`).join("");
    selectProducto.value = valorActual;
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
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Fase 3.5.3 — Presupuesto
// ---------------------------------------------------------------------------

const OPP_TIPO_LABEL = {
  mano_obra: "Mano de obra", repuesto: "Repuesto", insumo: "Insumo",
  servicio_externo: "Servicio externo", descuento: "Descuento",
};

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
  document.getElementById("opp-notas").value = orden.presupuesto_notas || "";

  tbody.querySelectorAll("[data-eliminar-presupuesto-item]").forEach(btn => {
    btn.addEventListener("click", () => eliminarItemPresupuesto(parseInt(btn.dataset.eliminarPresupuestoItem, 10)));
  });
}

document.getElementById("oppi-tipo").addEventListener("change", () => {});

document.getElementById("form-op-presupuesto-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    tipo: document.getElementById("oppi-tipo").value,
    descripcion: document.getElementById("oppi-descripcion").value.trim(),
    cantidad: document.getElementById("oppi-cantidad").value,
    precio_unitario: document.getElementById("oppi-precio").value || 0,
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
    notas: document.getElementById("opp-notas").value.trim(),
  };
  try {
    await api(`/api/operativo/ordenes/${ordenId}/presupuesto`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Presupuesto actualizado.");
    await cargarOperativo();
    abrirDetalleOrden(parseInt(ordenId, 10));
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Fase 3.5.5 — Planificación y tiempos
// ---------------------------------------------------------------------------

const OP_TIEMPO_EVENTO_LABEL = { inicio: "Inicio", pausa: "Pausa", reanudar: "Reanudó", termino: "Término" };

function renderTiempos(orden) {
  document.getElementById("opod-horas-reales").value = `${orden.horas_reales} h`;
  const lista = document.getElementById("op-tiempos-lista");
  if (!orden.tiempos || orden.tiempos.length === 0) {
    lista.innerHTML = `<li>Sin registros de tiempo todavía.</li>`;
    return;
  }
  lista.innerHTML = orden.tiempos.map(t => `<li>${OP_TIEMPO_EVENTO_LABEL[t.evento] || t.evento} — ${fmtFecha(t.marca_tiempo)}${t.usuario ? " · " + escapeHtml(t.usuario) : ""}</li>`).join("");
}

document.querySelectorAll("[data-tiempo-evento]").forEach(btn => {
  btn.addEventListener("click", async () => {
    const ordenId = document.getElementById("opod-id").value;
    try {
      await api(`/api/operativo/ordenes/${ordenId}/tiempos`, {
        method: "POST", body: JSON.stringify({ evento: btn.dataset.tiempoEvento }),
      });
      toast("Registro de tiempo agregado.");
      const orden = await api(`/api/operativo/ordenes/${ordenId}`);
      renderTiempos(orden);
    } catch (e) {
      toast(e.message, true);
    }
  });
});

// ---------------------------------------------------------------------------
// Fase 3.5.6 — Control de Calidad
// ---------------------------------------------------------------------------

const OPQC_RESULTADO_LABEL = { ok: "OK", falla: "Falla", na: "N/A" };

function renderQcChecklist(orden) {
  const cont = document.getElementById("op-qc-checklist");
  if (!orden.qc_checklist || orden.qc_checklist.length === 0) {
    cont.innerHTML = `<p class="vacio-inline">Sin puntos de control todavía.</p>`;
  } else {
    cont.innerHTML = orden.qc_checklist.map(it => `
      <div class="op-checklist-row">
        <span class="op-checklist-item">${escapeHtml(it.item)}${it.observacion ? " — " + escapeHtml(it.observacion) : ""}</span>
        <span class="badge-urgencia ${it.resultado === "falla" ? "badge-urgencia--critico" : ""}">${OPQC_RESULTADO_LABEL[it.resultado] || it.resultado}</span>
        <button type="button" class="btn-link btn-link--peligro" data-eliminar-qc-item="${it.id}">Eliminar</button>
      </div>
    `).join("");
    cont.querySelectorAll("[data-eliminar-qc-item]").forEach(btn => {
      btn.addEventListener("click", () => eliminarItemQc(parseInt(btn.dataset.eliminarQcItem, 10)));
    });
  }
  document.getElementById("opqc-veredicto").value = orden.qc_resultado || "pendiente";
  document.getElementById("opqc-inspector").value = orden.qc_inspector || "";
  document.getElementById("opqc-observaciones-generales").value = orden.qc_observaciones || "";
}

document.getElementById("form-op-qc-item").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    item: document.getElementById("opqc-item").value.trim(),
    resultado: document.getElementById("opqc-resultado").value,
    observacion: document.getElementById("opqc-observacion").value.trim(),
  };
  if (!payload.item) {
    toast("Describe el punto a revisar.", true);
    return;
  }
  try {
    await api(`/api/operativo/ordenes/${ordenId}/qc/items`, { method: "POST", body: JSON.stringify(payload) });
    toast("Punto de control agregado.");
    document.getElementById("form-op-qc-item").reset();
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderQcChecklist(orden);
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function eliminarItemQc(itemId) {
  if (!confirm("¿Eliminar este punto de control?")) return;
  const ordenId = document.getElementById("opod-id").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/qc/items/${itemId}`, { method: "DELETE" });
    toast("Punto de control eliminado.");
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderQcChecklist(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-opqc-guardar").addEventListener("click", async () => {
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    resultado: document.getElementById("opqc-veredicto").value,
    inspector: document.getElementById("opqc-inspector").value.trim(),
    observaciones: document.getElementById("opqc-observaciones-generales").value.trim(),
  };
  try {
    await api(`/api/operativo/ordenes/${ordenId}/qc`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Control de calidad actualizado.");
    await cargarOperativo();
    abrirDetalleOrden(parseInt(ordenId, 10));
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Fase 3.5.7 — Garantía
// ---------------------------------------------------------------------------

function renderGarantias(orden) {
  const cont = document.getElementById("op-garantias-lista");
  if (!orden.garantias || orden.garantias.length === 0) {
    cont.innerHTML = `<p class="vacio-inline">Sin garantías registradas para esta orden.</p>`;
    return;
  }
  cont.innerHTML = orden.garantias.map(g => `
    <div class="op-garantia-card">
      <div>${escapeHtml(g.motivo)}</div>
      <div class="op-garantia-meta">
        Estado: ${escapeHtml(g.estado)} · Costo asumido: ${fmtMoney(g.costo_asumido)}
        ${g.responsable ? " · " + escapeHtml(g.responsable) : ""} · ${fmtFecha(g.creado_en)}
      </div>
    </div>
  `).join("");
}

document.getElementById("form-op-garantia").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    motivo: document.getElementById("opg-motivo").value.trim(),
    diagnostico: document.getElementById("opg-diagnostico").value.trim(),
    responsable: document.getElementById("opg-responsable").value.trim(),
    costo_asumido: document.getElementById("opg-costo").value || 0,
  };
  if (!payload.motivo) {
    toast("Describe el motivo de la garantía.", true);
    return;
  }
  try {
    await api(`/api/operativo/ordenes/${ordenId}/garantias`, { method: "POST", body: JSON.stringify(payload) });
    toast("Garantía registrada.");
    document.getElementById("form-op-garantia").reset();
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderGarantias(orden);
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---------------------------------------------------------------------------
// Fase 3.5.8 — Entrega
// ---------------------------------------------------------------------------

document.getElementById("btn-ope-entregar").addEventListener("click", async () => {
  if (!confirm("¿Marcar esta orden como entregada?")) return;
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    recibido_por: document.getElementById("ope-recibido-por").value.trim(),
    documento: document.getElementById("ope-documento").value,
    observaciones: document.getElementById("ope-observaciones").value.trim(),
  };
  if (!payload.recibido_por) {
    toast("Indica quién retira el vehículo.", true);
    return;
  }
  try {
    await api(`/api/operativo/ordenes/${ordenId}/entregar`, { method: "POST", body: JSON.stringify(payload) });
    toast("Orden marcada como entregada.");
    await cargarOperativo();
    abrirDetalleOrden(parseInt(ordenId, 10));
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Fase 3.5.1 — Historial de vehículo
// ---------------------------------------------------------------------------

async function abrirHistorialEquipo(equipoId) {
  try {
    const data = await api(`/api/operativo/equipos/${equipoId}/historial`);
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
    abrirModal("modal-op-equipo-historial");
  } catch (e) {
    toast(e.message, true);
  }
}

const OP_ESTADOS_LABEL = {
  recibido: "Recibido", diagnostico: "Diagnóstico", presupuesto: "Presupuesto",
  esperando_aprobacion: "Esperando aprobación", reparacion: "Reparación",
  esperando_repuesto: "Esperando repuesto", control_calidad: "Control de calidad",
  listo: "Listo", entregado: "Entregado", cancelado: "Cancelado",
};

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
          <td><button type="button" class="btn-link" data-ver-historial-equipo="${eq.id}">Ver historial</button></td>
        </tr>
      `).join("");
      tbody.querySelectorAll("[data-ver-historial-equipo]").forEach(btn => {
        btn.addEventListener("click", () => abrirHistorialEquipo(parseInt(btn.dataset.verHistorialEquipo, 10)));
      });
    }
    abrirModal("modal-op-cliente-equipos");
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
