const state = {
  sesion: { username: null, nombre: null },
  agenteMecanicos: [],
  agenteMensajes: [],
};

// ---------------------------------------------------------------------------
// Utilidades compartidas (duplicadas de app.js: cada módulo carga su propio JS)
// ---------------------------------------------------------------------------

function fmtFecha(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("es-CL", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
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

// Mayúsculas automáticas en campos de texto libre (nombre del mecánico) —
// mismo criterio que el resto de los módulos.
document.querySelectorAll('input[type="text"], textarea').forEach(el => {
  el.addEventListener("input", (e) => {
    const inicio = e.target.selectionStart;
    const fin = e.target.selectionEnd;
    const enMayusculas = e.target.value.toUpperCase();
    if (enMayusculas === e.target.value) return; // ya estaba en mayúsculas: no tocar el cursor
    e.target.value = enMayusculas;
    e.target.setSelectionRange(inicio, fin);
  });
});

// ---------------------------------------------------------------------------
// Agente IA (WhatsApp): mecánicos registrados, simulador de mensajes (texto
// y voz) y auditoría de lo que el agente hizo (o no pudo hacer) con cada
// mensaje. Antes vivía como una pestaña más dentro de Logística — ahora es
// su propio módulo (ver "/agente-ia" en app.py e inicio.html), para que
// quede más ordenado a medida que el agente va sumando funciones.
// ---------------------------------------------------------------------------

async function cargarAgenteIA() {
  await Promise.all([cargarMecanicosAgente(), cargarMensajesAgente()]);
}

async function cargarMecanicosAgente() {
  const tbody = document.getElementById("tbody-mecanicos-agente");
  try {
    state.agenteMecanicos = await api("/api/agente/mecanicos");
    renderTablaMecanicosAgente();
    renderSelectMecanicosAgente();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="vacio">Error al cargar mecánicos: ${e.message}</td></tr>`;
  }
}

function renderTablaMecanicosAgente() {
  const tbody = document.getElementById("tbody-mecanicos-agente");
  if (!state.agenteMecanicos.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="vacio">Todavía no hay mecánicos registrados.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.agenteMecanicos.map(m => `
    <tr data-id="${m.id}">
      <td>${escapeHtml(m.nombre)}</td>
      <td class="mono">${escapeHtml(m.telefono)}</td>
      <td><span class="badge-urgencia ${m.activo ? "badge-urgencia--proximo" : "badge-urgencia--critico"}">${m.activo ? "Activo" : "Inactivo"}</span></td>
      <td>
        <button type="button" class="btn btn-secundario btn-chico" data-toggle-mecanico="${m.id}">${m.activo ? "Desactivar" : "Activar"}</button>
        <button type="button" class="btn btn-danger btn-chico" data-eliminar-mecanico="${m.id}">Eliminar</button>
      </td>
    </tr>
  `).join("");

  tbody.querySelectorAll("[data-toggle-mecanico]").forEach(btn => {
    btn.addEventListener("click", () => alternarActivoMecanicoAgente(parseInt(btn.dataset.toggleMecanico, 10)));
  });
  tbody.querySelectorAll("[data-eliminar-mecanico]").forEach(btn => {
    btn.addEventListener("click", () => eliminarMecanicoAgente(parseInt(btn.dataset.eliminarMecanico, 10)));
  });
}

function renderSelectMecanicosAgente() {
  const opciones = `<option value="">Selecciona un mecánico…</option>` + state.agenteMecanicos
    .filter(m => m.activo)
    .map(m => `<option value="${escapeHtml(m.telefono)}">${escapeHtml(m.nombre)} (${escapeHtml(m.telefono)})</option>`)
    .join("");
  ["sim-agente-telefono", "sim-agente-voz-telefono"].forEach(id => {
    const select = document.getElementById(id);
    if (!select) return;
    const valorPrevio = select.value;
    select.innerHTML = opciones;
    select.value = valorPrevio;
  });
}

async function alternarActivoMecanicoAgente(id) {
  const m = state.agenteMecanicos.find(x => x.id === id);
  if (!m) return;
  try {
    await api(`/api/agente/mecanicos/${id}`, { method: "PUT", body: JSON.stringify({ activo: !m.activo }) });
    toast(m.activo ? "Mecánico desactivado" : "Mecánico activado");
    await cargarMecanicosAgente();
  } catch (e) {
    toast(e.message, true);
  }
}

async function eliminarMecanicoAgente(id) {
  const m = state.agenteMecanicos.find(x => x.id === id);
  if (!m) return;
  if (!confirm(`¿Eliminar al mecánico "${m.nombre}"? Ya no va a poder usar el agente.`)) return;
  try {
    await api(`/api/agente/mecanicos/${id}`, { method: "DELETE" });
    toast("Mecánico eliminado");
    await cargarMecanicosAgente();
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-nuevo-mecanico-agente").addEventListener("click", () => {
  document.getElementById("form-mecanico-agente").reset();
  abrirModal("modal-mecanico-agente");
});

document.getElementById("form-mecanico-agente").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    nombre: document.getElementById("mec-nombre").value,
    telefono: document.getElementById("mec-telefono").value,
  };
  try {
    await api("/api/agente/mecanicos", { method: "POST", body: JSON.stringify(payload) });
    toast("Mecánico creado");
    cerrarModal("modal-mecanico-agente");
    await cargarMecanicosAgente();
  } catch (e2) {
    toast(e2.message, true);
  }
});

async function cargarMensajesAgente() {
  const tbody = document.getElementById("tbody-mensajes-agente");
  try {
    state.agenteMensajes = await api("/api/agente/mensajes");
    renderTablaMensajesAgente();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Error al cargar el historial: ${e.message}</td></tr>`;
  }
}

function renderTablaMensajesAgente() {
  const tbody = document.getElementById("tbody-mensajes-agente");
  if (!state.agenteMensajes.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Todavía no ha llegado ningún mensaje.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.agenteMensajes.map(m => `
    <tr>
      <td class="mono">${fmtFecha(m.creado_en)}</td>
      <td>${escapeHtml(m.mecanico_nombre) || "—"}</td>
      <td>${m.origen === "voz" ? "🎤 Voz" : "💬 Texto"}</td>
      <td>${escapeHtml(m.mensaje_texto)}</td>
      <td><span class="badge-urgencia ${m.exito ? "badge-urgencia--proximo" : "badge-urgencia--critico"}">${escapeHtml(m.respuesta_texto)}</span></td>
    </tr>
  `).join("");
}

document.getElementById("form-simular-agente").addEventListener("submit", async (e) => {
  e.preventDefault();
  const telefono = document.getElementById("sim-agente-telefono").value;
  const texto = document.getElementById("sim-agente-texto").value;
  const resultadoDiv = document.getElementById("sim-agente-resultado");
  resultadoDiv.style.display = "block";
  resultadoDiv.innerHTML = `<p class="ayuda-inline">Enviando…</p>`;
  try {
    const data = await api("/api/agente/simular-mensaje", {
      method: "POST",
      body: JSON.stringify({ telefono, texto }),
    });
    const clase = data.exito ? "ayuda-inline" : "ayuda-inline ayuda-inline--bloqueado";
    resultadoDiv.innerHTML = `<p class="${clase}">${escapeHtml(data.respuesta_texto)}</p>`;
    document.getElementById("sim-agente-texto").value = "";
    await cargarMensajesAgente();
  } catch (e2) {
    resultadoDiv.innerHTML = `<p class="ayuda-inline ayuda-inline--bloqueado">${escapeHtml(e2.message)}</p>`;
  }
});

document.getElementById("form-simular-agente-voz").addEventListener("submit", async (e) => {
  e.preventDefault();
  const telefono = document.getElementById("sim-agente-voz-telefono").value;
  const archivoInput = document.getElementById("sim-agente-voz-archivo");
  const archivo = archivoInput.files[0];
  const resultadoDiv = document.getElementById("sim-agente-voz-resultado");
  resultadoDiv.style.display = "block";
  if (!archivo) {
    resultadoDiv.innerHTML = `<p class="ayuda-inline ayuda-inline--bloqueado">Selecciona un archivo de audio.</p>`;
    return;
  }
  resultadoDiv.innerHTML = `<p class="ayuda-inline">Transcribiendo y procesando…</p>`;
  try {
    const formData = new FormData();
    formData.append("telefono", telefono);
    formData.append("audio", archivo);
    const resp = await fetch("/api/agente/simular-mensaje-voz", { method: "POST", body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || "Error al procesar la nota de voz");
    const clase = data.exito ? "ayuda-inline" : "ayuda-inline ayuda-inline--bloqueado";
    resultadoDiv.innerHTML = `
      <p class="ayuda-inline"><strong>Transcripción:</strong> ${escapeHtml(data.transcripcion || "")}</p>
      <p class="${clase}">${escapeHtml(data.respuesta_texto)}</p>
    `;
    archivoInput.value = "";
    await cargarMensajesAgente();
  } catch (e2) {
    resultadoDiv.innerHTML = `<p class="ayuda-inline ayuda-inline--bloqueado">${escapeHtml(e2.message)}</p>`;
  }
});

cargarSesion();
cargarAgenteIA();
