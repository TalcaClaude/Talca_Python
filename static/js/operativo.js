const state = {
  sesion: { username: null, nombre: null },
  opOrdenes: [],
  opClientes: [],
  opEquiposClienteActual: [],
  opOrdenActivaId: null,
  opProductos: [],
  opGarantias: [],
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

// Días que lleva (o llevó) el vehículo en el taller: desde la recepción
// hasta hoy, o hasta la entrega real si la orden ya se entregó (para que
// el contador se congele en vez de seguir subiendo).
function diasEnTaller(orden) {
  const inicio = new Date(orden.fecha_recepcion);
  if (isNaN(inicio)) return "—";
  const fin = orden.fecha_entrega_real ? new Date(orden.fecha_entrega_real) : new Date();
  const dias = Math.max(0, Math.floor((fin - inicio) / 86400000));
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
    document.getElementById("vista-clientes").style.display = vista === "clientes" ? "block" : "none";
    document.getElementById("vista-garantias").style.display = vista === "garantias" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      dashboard: "Operativo — Dashboard",
      ordenes: "Operativo — Órdenes de Trabajo",
      clientes: "Operativo — Clientes",
      garantias: "Operativo — Garantías",
      reportes: "Operativo — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "dashboard") cargarDashboardOperativo();
    if (vista === "ordenes") cargarOperativo();
    if (vista === "clientes") cargarClientesTab();
    if (vista === "garantias") cargarGarantiasTab();
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
    document.getElementById("op-s-garantias-abiertas").textContent = data.garantias_abiertas;
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

const OP_ESTADOS_LABEL = {
  recibido: "Recibido", diagnostico: "Diagnóstico", presupuesto: "Presupuesto",
  esperando_aprobacion: "Esperando aprobación", reparacion: "Reparación",
  esperando_repuesto: "Esperando repuesto", control_calidad: "Pruebas de ruta",
  listo: "Listo", entregado: "Entregado", cancelado: "Cancelado",
};

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
    poblarDatalistsOperativo();
  } catch (e) {
    toast(e.message, true);
  }
}

// Datalists de "select con texto libre" para Marca/Modelo/Color/Modelo de
// Transmisión/Referencia/Trabajo a Realizar: se arman a partir del historial
// ya cargado (sin necesitar una pantalla de catálogo aparte).
function poblarDatalistsOperativo() {
  const marcas = new Set(), modelos = new Set(), colores = new Set();
  const transmisiones = new Set(), referencias = new Set(), trabajos = new Set();
  state.opOrdenes.forEach(o => {
    if (o.equipo) {
      if (o.equipo.marca) marcas.add(o.equipo.marca);
      if (o.equipo.modelo) modelos.add(o.equipo.modelo);
      if (o.equipo.color) colores.add(o.equipo.color);
      if (o.equipo.modelo_transmision) transmisiones.add(o.equipo.modelo_transmision);
    }
    if (o.referencia) referencias.add(o.referencia);
    if (o.trabajo_a_realizar) trabajos.add(o.trabajo_a_realizar);
  });
  const llenar = (id, valores) => {
    document.getElementById(id).innerHTML = [...valores].map(v => `<option value="${escapeHtml(v)}">`).join("");
  };
  llenar("dl-op-marcas", marcas);
  llenar("dl-op-modelos", modelos);
  llenar("dl-op-colores", colores);
  llenar("dl-op-transmisiones", transmisiones);
  llenar("dl-op-referencias", referencias);
  llenar("dl-op-trabajos", trabajos);
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
      <div class="op-card op-card--${o.prioridad || "media"}" data-id="${o.id}" draggable="true">
        <div class="op-card-top">
          <span class="op-card-ot">OT #${o.id}</span>
          ${o.prioridad && o.prioridad !== "media" ? `<span class="badge-prioridad badge-prioridad--${o.prioridad}">${escapeHtml(o.prioridad)}</span>` : ""}
        </div>
        <div class="op-card-cliente">${escapeHtml(o.cliente ? o.cliente.nombre : "Sin cliente")}</div>
        ${o.equipo ? `<div class="op-card-equipo">${escapeHtml([o.equipo.marca, o.equipo.modelo].filter(Boolean).join(" · ") || o.equipo.identificador || "")}</div>` : ""}
        <div class="op-card-valor">${fmtMoney(o.precio_referencial || 0)}</div>
        <div class="op-card-meta">
          <span>Recepción: ${fmtFechaSolo((o.fecha_recepcion || "").slice(0, 10))}</span>
          <span>${diasEnTaller(o)}</span>
        </div>
      </div>
    `).join("");
  });

  document.querySelectorAll(".op-card").forEach(card => {
    card.addEventListener("click", () => abrirDetalleOrden(parseInt(card.dataset.id, 10)));
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id);
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
    });
  });
}

// ---- Kanban interactivo: arrastrar una OT de una columna a otra cambia su
// estado. Los drop-zones son las columnas mismas (elementos fijos del HTML),
// así que los listeners se conectan una sola vez al cargar la página — solo
// el contenido interno de tarjetas se vuelve a pintar en cada render.
document.querySelectorAll(".op-columna").forEach(columna => {
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
    select.innerHTML = '<option value="">Selecciona un vehículo…</option>';
    document.getElementById("opon-ver-historial").style.display = "none";
    return;
  }
  const data = await api(`/api/operativo/equipos?cliente_id=${clienteId}`);
  state.opEquiposClienteActual = data.equipos;
  select.innerHTML = '<option value="">Selecciona un vehículo…</option>' +
    data.equipos.map(eq => `<option value="${eq.id}">${escapeHtml([eq.marca, eq.modelo, eq.identificador].filter(Boolean).join(" · ") || "Equipo #" + eq.id)}</option>`).join("");
  select.value = valorActual || "";
  // select.value = ... no dispara "change", así que el link de historial se
  // actualiza acá también (cubre el caso de equipo recién creado inline).
  document.getElementById("opon-ver-historial").style.display = valorActual ? "" : "none";
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
  document.getElementById("opon-marca").value = eq ? (eq.marca || "") : "";
  document.getElementById("opon-modelo").value = eq ? (eq.modelo || "") : "";
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
  renderSelectClienteEquipoModal(clienteId);
  document.getElementById("modal-op-equipo-titulo").textContent = "Nuevo equipo";
  opEquipoCallback = async (nuevo, payload) => {
    await renderSelectEquiposOperativo(payload.cliente_id, nuevo.id);
    poblarCamposVehiculoNuevaOrden(nuevo.id);
  };
  abrirModal("modal-op-equipo");
});

// No existe ninguna base de datos (ni gratis ni confiable) para autocompletar
// marca/modelo a partir de la patente de un auto en Chile — se evaluó y no
// se integró ningún servicio externo de pago. Lo que sí podemos hacer con
// nuestros propios datos: si esa patente ya está registrada en el taller
// (aunque haya sido con otro cliente, ej. el auto cambió de dueño),
// autocompletamos marca/modelo/color/año/etc. desde ese registro anterior.
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
    const marcaEl = document.getElementById("ope-marca");
    const modeloEl = document.getElementById("ope-modelo");
    if (!marcaEl.value.trim()) marcaEl.value = encontrado.marca || "";
    if (!modeloEl.value.trim()) modeloEl.value = encontrado.modelo || "";
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
    const dataOrdenes = await api("/api/operativo/ordenes");
    state.opOrdenes = dataOrdenes.ordenes;
  }
  poblarDatalistsOperativo();
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
        marca: document.getElementById("opon-marca").value.trim(),
        modelo: document.getElementById("opon-modelo").value.trim(),
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
        precio_referencial: document.getElementById("opon-precio").value || null,
        equipamiento,
      }),
    });
    cerrarModal("modal-op-orden-nueva");
    toast("Orden creada.");
    await cargarOperativo();
    abrirDetalleOrden(nueva.id);
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---------------------------------------------------------------------------
// Detalle de orden: SOLO resumen de cliente/auto + botones de acceso.
// ---------------------------------------------------------------------------

// Pestaña "OT": toda la info del cliente y del vehículo (sin el checklist
// de equipamiento, que solo se ve en "Recepción").
function renderResumenClienteAuto(orden) {
  const cliente = orden.cliente;
  const equipo = orden.equipo;
  const filasCliente = cliente ? [
    ["Cliente", cliente.nombre],
    ["Teléfono", cliente.telefono || "—"],
    ["RUT", cliente.rut || "—"],
    ["Email", cliente.email || "—"],
  ] : [["Cliente", "Sin cliente"]];
  const filasAuto = equipo ? [
    ["Marca", equipo.marca || "—"],
    ["Modelo", equipo.modelo || "—"],
    ["Patente", equipo.identificador || "—"],
    ["Color", equipo.color || "—"],
    ["Año", equipo.anio || "—"],
    ["Transmisión", equipo.modelo_transmision || "—"],
    ["Puertas", equipo.puertas || "—"],
    ["Motor (cc)", equipo.motor_cc || "—"],
    ["VIN", equipo.vin || "—"],
  ] : [["Vehículo", "Sin vehículo asociado"]];

  const filas = (arr) => arr.map(([k, v]) => `<div class="op-resumen-fila"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join("");

  document.getElementById("opod-resumen-cliente").innerHTML = filas(filasCliente);
  document.getElementById("opod-resumen-vehiculo").innerHTML = filas(filasAuto);
}

// Pinta el recuadro de valor/pagado-saldo y la tabla de pagos de la OT
// abierta en el hub. Se llama al abrir la orden y después de cada
// acción que pueda cambiar el valor o los pagos (guardar cambios,
// registrar/eliminar un pago, refrescos automáticos de estado).
function renderValorYPagosOrden(orden) {
  document.getElementById("opod-valor").value = orden.valor_neto != null ? orden.valor_neto : 0;
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
const OPOI_TIPO_LABEL = { repuesto: "Repuesto", mano_obra: "Mano de obra" };

function renderCostosOrden(orden) {
  const tbody = document.getElementById("tbody-opod-costos-items");
  const items = orden.items || [];
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Sin ítems cargados.</td></tr>`;
  } else {
    tbody.innerHTML = items.map(it => `
      <tr>
        <td>${escapeHtml(OPOI_TIPO_LABEL[it.tipo] || it.tipo)}</td>
        <td>${escapeHtml(it.descripcion)}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.cantidad * it.costo_unitario)}</td>
      </tr>
    `).join("");
  }
  document.getElementById("opod-c-costo-repuestos").textContent = fmtMoney(orden.costo_repuestos || 0);
  document.getElementById("opod-c-mano-obra").textContent = fmtMoney(orden.mano_obra_cargada || 0);
  document.getElementById("opod-c-neto").textContent = fmtMoney(orden.valor_neto || 0);
  document.getElementById("opod-c-total").textContent = fmtMoney(orden.valor_total || 0);
  document.getElementById("opod-c-pagado").textContent = fmtMoney(orden.total_pagado || 0);
  document.getElementById("opod-c-por-pagar").textContent = fmtMoney(orden.total_por_pagar || 0);
  document.getElementById("opod-c-utilidad").textContent = fmtMoney(orden.utilidad_estimada || 0);
}

// Pestañas "OT" / "Costos" dentro de la ventana de la orden.
document.querySelectorAll("#modal-op-orden-detalle .op-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll("#modal-op-orden-detalle .op-tab").forEach(t => t.classList.remove("activo"));
    tab.classList.add("activo");
    document.getElementById("opod-panel-ot").style.display = tab.dataset.opodTab === "ot" ? "" : "none";
    document.getElementById("opod-panel-costos").style.display = tab.dataset.opodTab === "costos" ? "" : "none";
  });
});

function activarTabOtPorDefecto() {
  document.querySelectorAll("#modal-op-orden-detalle .op-tab").forEach(t => t.classList.remove("activo"));
  document.querySelector('#modal-op-orden-detalle .op-tab[data-opod-tab="ot"]').classList.add("activo");
  document.getElementById("opod-panel-ot").style.display = "";
  document.getElementById("opod-panel-costos").style.display = "none";
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
    descripcion_problema: document.getElementById("opod-descripcion").value.trim(),
    precio_referencial: document.getElementById("opod-valor").value || 0,
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
    monto: document.getElementById("opag-monto").value,
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

document.getElementById("btn-abrir-recepcion").addEventListener("click", () => abrirRecepcionLectura());

async function abrirRecepcionLectura() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    const equipo = orden.equipo || {};
    const cliente = orden.cliente || {};
    const estanque = orden.estanque_nivel;

    const filasVehiculo = [
      ["VIN", equipo.vin || "—"],
      ["Patente", equipo.identificador || "—"],
      ["Marca", equipo.marca || "—"],
      ["Modelo", equipo.modelo || "—"],
      ["Color", equipo.color || "—"],
      ["Año", equipo.anio || "—"],
      ["Modelo de Transmisión", equipo.modelo_transmision || "—"],
      ["Puertas", equipo.puertas || "—"],
      ["Motor (cc)", equipo.motor_cc || "—"],
    ];
    const filasRecepcion = [
      ["RUT Cliente", cliente.rut || "—"],
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

    abrirModal("modal-op-recepcion");
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Diagnóstico
// ---------------------------------------------------------------------------

document.getElementById("btn-abrir-diagnostico").addEventListener("click", () => abrirDiagnostico());

async function abrirDiagnostico() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    document.getElementById("opdi-sintoma").value = orden.sintoma || "";
    document.getElementById("opdi-diagnostico-tecnico").value = orden.diagnostico_tecnico || "";
    document.getElementById("opdi-falla-detectada").value = orden.falla_detectada || "";
    document.getElementById("opdi-componentes-danados").value = orden.componentes_danados || "";
    document.getElementById("opdi-mediciones").value = orden.mediciones || "";
    document.getElementById("opdi-resultado-pruebas").value = orden.resultado_pruebas || "";
    document.getElementById("opdi-responsable-diagnostico").value = orden.responsable_diagnostico || "";
    document.getElementById("opdi-diagnostico").value = orden.diagnostico || "";
    abrirModal("modal-op-diagnostico");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-opdi-guardar").addEventListener("click", async () => {
  const ordenId = document.getElementById("opod-id").value;
  const payload = {
    sintoma: document.getElementById("opdi-sintoma").value.trim(),
    diagnostico_tecnico: document.getElementById("opdi-diagnostico-tecnico").value.trim(),
    falla_detectada: document.getElementById("opdi-falla-detectada").value.trim(),
    componentes_danados: document.getElementById("opdi-componentes-danados").value.trim(),
    mediciones: document.getElementById("opdi-mediciones").value.trim(),
    resultado_pruebas: document.getElementById("opdi-resultado-pruebas").value.trim(),
    responsable_diagnostico: document.getElementById("opdi-responsable-diagnostico").value.trim(),
    diagnostico: document.getElementById("opdi-diagnostico").value.trim(),
  };
  try {
    await api(`/api/operativo/ordenes/${ordenId}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Diagnóstico guardado.");
    await refrescarResumenHub(ordenId);
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

document.getElementById("btn-abrir-presupuesto").addEventListener("click", () => abrirPresupuestoModal());

async function abrirPresupuestoModal() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderPresupuesto(orden);
    abrirModal("modal-op-presupuesto");
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

// ---------------------------------------------------------------------------
// Carga de repuestos (antes "Repuestos y mano de obra")
// ---------------------------------------------------------------------------

document.getElementById("btn-abrir-repuestos").addEventListener("click", () => abrirRepuestosModal());

async function abrirRepuestosModal() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    if (state.opProductos.length === 0) {
      const dataProductos = await api("/api/productos");
      state.opProductos = dataProductos.productos;
    }
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderItemsOrden(orden);
    const selectProducto = document.getElementById("opoi-producto-id");
    selectProducto.innerHTML = '<option value="">Selecciona un producto…</option>' +
      state.opProductos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)} (stock: ${p.stock_disponible})</option>`).join("");
    document.getElementById("form-op-item").reset();
    document.getElementById("opoi-tipo").dispatchEvent(new Event("change"));
    abrirModal("modal-op-repuestos");
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
    renderCostosOrden(orden);
    document.getElementById("form-op-item").reset();
    document.getElementById("opoi-tipo").dispatchEvent(new Event("change"));
    // Refresca el stock mostrado en el selector de productos.
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
    renderCostosOrden(orden);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Control de Calidad — cada punto queda con fecha y persona
// ---------------------------------------------------------------------------

const OPQC_RESULTADO_LABEL = { ok: "OK", falla: "Falla", na: "N/A" };

document.getElementById("btn-abrir-qc").addEventListener("click", () => abrirQcModal());

async function abrirQcModal() {
  const ordenId = document.getElementById("opod-id").value;
  try {
    const orden = await api(`/api/operativo/ordenes/${ordenId}`);
    renderQcChecklist(orden);
    abrirModal("modal-op-qc");
  } catch (e) {
    toast(e.message, true);
  }
}

function renderQcChecklist(orden) {
  const cont = document.getElementById("op-qc-checklist");
  if (!orden.qc_checklist || orden.qc_checklist.length === 0) {
    cont.innerHTML = `<p class="vacio-inline">Sin puntos de control todavía.</p>`;
  } else {
    cont.innerHTML = orden.qc_checklist.map(it => `
      <div class="op-checklist-row">
        <span class="op-checklist-item">${escapeHtml(it.item)}${it.observacion ? " — " + escapeHtml(it.observacion) : ""}</span>
        <span class="op-checklist-meta">${it.usuario ? escapeHtml(it.usuario) + " · " : ""}${fmtFecha(it.creado_en)}</span>
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
    const actualizada = await api(`/api/operativo/ordenes/${ordenId}/qc`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Control de calidad actualizado.");
    renderQcChecklist(actualizada);
    await refrescarResumenHub(ordenId);
  } catch (e) {
    toast(e.message, true);
  }
});

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

// ---------------------------------------------------------------------------
// Garantías — entidad propia, con su propia vista y su propio proceso
// (no viven dentro del tablero de OTs).
// ---------------------------------------------------------------------------

const GAR_ESTADO_LABEL = {
  pendiente: "Pendiente", en_reparacion: "En reparación",
  resuelta: "Resuelta", rechazada: "Rechazada",
};

async function cargarGarantiasTab() {
  try {
    const estado = document.getElementById("gar-filtro-estado").value;
    const data = await api(`/api/operativo/garantias${estado ? "?estado=" + encodeURIComponent(estado) : ""}`);
    state.opGarantias = data.garantias;
    const tbody = document.getElementById("tbody-garantias");
    if (data.garantias.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="vacio">Sin garantías registradas todavía.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.garantias.map(g => `
      <tr data-ver-garantia="${g.id}">
        <td>${fmtFechaSolo((g.fecha_recepcion || "").slice(0, 10))}</td>
        <td>${escapeHtml(g.cliente ? g.cliente.nombre : "—")}</td>
        <td>${escapeHtml(g.equipo ? [g.equipo.marca, g.equipo.modelo, g.equipo.identificador].filter(Boolean).join(" · ") : "—")}</td>
        <td>${escapeHtml(g.motivo)}</td>
        <td>${escapeHtml(GAR_ESTADO_LABEL[g.estado] || g.estado)}</td>
        <td>${escapeHtml(g.responsable || "—")}</td>
        <td class="num">${fmtMoney(g.total)}</td>
      </tr>
    `).join("");
    tbody.querySelectorAll("[data-ver-garantia]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleGarantia(parseInt(tr.dataset.verGarantia, 10)));
    });
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("gar-filtro-estado").addEventListener("change", cargarGarantiasTab);

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
  cargarOrdenesGarantiaParaEquipo(e.target.value || null, "");
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
    await api("/api/operativo/garantias", { method: "POST", body: JSON.stringify(payload) });
    cerrarModal("modal-op-garantia-nueva");
    toast("Garantía creada.");
    cargarGarantiasTab();
    cargarDashboardOperativo();
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---- Detalle de garantía ----

function renderItemsGarantia(garantia) {
  const tbody = document.getElementById("tbody-gar-items");
  if (garantia.items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Sin ítems todavía.</td></tr>`;
  } else {
    tbody.innerHTML = garantia.items.map(it => `
      <tr>
        <td>${it.tipo === "repuesto" ? "Repuesto" : "Mano de obra"}</td>
        <td>${escapeHtml(it.descripcion)}</td>
        <td class="num">${it.cantidad}</td>
        <td class="num">${fmtMoney(it.costo_unitario)}</td>
        <td class="num">${fmtMoney(it.cantidad * it.costo_unitario)}</td>
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
    const selectProducto = document.getElementById("gari-producto-id");
    selectProducto.innerHTML = '<option value="">Selecciona un producto…</option>' +
      state.opProductos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)} (stock: ${p.stock_disponible})</option>`).join("");
    document.getElementById("form-gar-item").reset();
    document.getElementById("gari-tipo").dispatchEvent(new Event("change"));

    abrirModal("modal-op-garantia-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

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
      toast("Selecciona un producto.", true);
      return;
    }
  } else {
    payload.descripcion = document.getElementById("gari-descripcion").value.trim();
    payload.costo_unitario = document.getElementById("gari-costo").value || 0;
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
    document.getElementById("gari-tipo").dispatchEvent(new Event("change"));
    const dataProductos = await api("/api/productos");
    state.opProductos = dataProductos.productos;
    const selectProducto = document.getElementById("gari-producto-id");
    const valorActual = selectProducto.value;
    selectProducto.innerHTML = '<option value="">Selecciona un producto…</option>' +
      state.opProductos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)} (stock: ${p.stock_disponible})</option>`).join("");
    selectProducto.value = valorActual;
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
