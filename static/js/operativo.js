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
// Pestañas: Dashboard / Órdenes de Trabajo
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-dashboard").style.display = vista === "dashboard" ? "block" : "none";
    document.getElementById("vista-ordenes").style.display = vista === "ordenes" ? "block" : "none";
    const titulos = {
      dashboard: "Operativo — Dashboard",
      ordenes: "Operativo — Órdenes de Trabajo",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "dashboard") cargarDashboardOperativo();
    if (vista === "ordenes") cargarOperativo();
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

const OP_ESTADOS_TABLERO = ["recibido", "diagnostico", "reparacion", "esperando_repuesto", "listo", "entregado", "cancelado"];

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
        <div class="op-card-cliente">${escapeHtml(o.cliente ? o.cliente.nombre : "Sin cliente")}</div>
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

document.getElementById("btn-op-clientes").addEventListener("click", async () => {
  await cargarListaClientesOperativo();
  abrirModal("modal-op-clientes-lista");
});

async function cargarListaClientesOperativo() {
  try {
    const busqueda = document.getElementById("opcl-buscar").value.trim();
    const data = await api(`/api/operativo/clientes${busqueda ? "?q=" + encodeURIComponent(busqueda) : ""}`);
    state.opClientes = data.clientes;
    const tbody = document.getElementById("tbody-op-clientes");
    if (data.clientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="vacio">Sin clientes todavía.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.clientes.map(c => `
      <tr>
        <td>${escapeHtml(c.nombre)}</td>
        <td>${escapeHtml(c.telefono || "—")}</td>
        <td>${escapeHtml(c.email || "—")}</td>
        <td class="num">${c.n_equipos}</td>
        <td><button type="button" class="btn-link" data-editar-cliente="${c.id}">Editar</button></td>
      </tr>
    `).join("");
    tbody.querySelectorAll("[data-editar-cliente]").forEach(btn => {
      btn.addEventListener("click", () => abrirEdicionClienteOperativo(parseInt(btn.dataset.editarCliente, 10)));
    });
  } catch (e) {
    toast(e.message, true);
  }
}

let opClienteCallback = null; // si viene desde "+ Nuevo" del formulario de orden, para preseleccionar al guardar

document.getElementById("opcl-buscar").addEventListener("input", () => {
  clearTimeout(document.getElementById("opcl-buscar")._t);
  document.getElementById("opcl-buscar")._t = setTimeout(cargarListaClientesOperativo, 250);
});

document.getElementById("btn-opcl-nuevo").addEventListener("click", () => {
  opClienteCallback = cargarListaClientesOperativo;
  abrirNuevoClienteOperativo();
});

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
  opClienteCallback = cargarListaClientesOperativo;
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
    return;
  }
  const data = await api(`/api/operativo/equipos?cliente_id=${clienteId}`);
  state.opEquiposClienteActual = data.equipos;
  select.innerHTML = '<option value="">Sin equipo específico</option>' +
    data.equipos.map(eq => `<option value="${eq.id}">${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</option>`).join("");
  select.value = valorActual || "";
}

document.getElementById("opon-cliente-id").addEventListener("change", (e) => {
  renderSelectEquiposOperativo(e.target.value || null);
});

document.getElementById("opon-nuevo-equipo").addEventListener("click", () => {
  const clienteId = document.getElementById("opon-cliente-id").value;
  if (!clienteId) {
    toast("Primero selecciona (o crea) un cliente.", true);
    return;
  }
  document.getElementById("form-op-equipo").reset();
  document.getElementById("ope-id").value = "";
  document.getElementById("ope-cliente-id").value = clienteId;
  document.getElementById("modal-op-equipo-titulo").textContent = "Nuevo equipo";
  abrirModal("modal-op-equipo");
});

document.getElementById("form-op-equipo").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("ope-id").value;
  const payload = {
    cliente_id: document.getElementById("ope-cliente-id").value,
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
    await renderSelectEquiposOperativo(payload.cliente_id, nuevo.id);
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---- Nueva orden ----

document.getElementById("btn-op-nueva-orden").addEventListener("click", () => {
  document.getElementById("form-op-orden-nueva").reset();
  renderSelectClientesOperativo("opon-cliente-id", "");
  renderSelectEquiposOperativo(null);
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
    document.getElementById("opod-fecha-estimada").value = (orden.fecha_entrega_estimada || "").slice(0, 10);
    document.getElementById("opod-fecha-real").value = orden.fecha_entrega_real ? fmtFecha(orden.fecha_entrega_real) : "Todavía no";
    document.getElementById("opod-descripcion").value = orden.descripcion_problema || "";
    document.getElementById("opod-diagnostico").value = orden.diagnostico || "";

    renderItemsOrden(orden);

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
    fecha_entrega_estimada: document.getElementById("opod-fecha-estimada").value || null,
    descripcion_problema: document.getElementById("opod-descripcion").value.trim(),
    diagnostico: document.getElementById("opod-diagnostico").value.trim(),
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
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarDashboardOperativo();
