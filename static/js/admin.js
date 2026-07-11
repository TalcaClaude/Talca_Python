const state = {
  usuarios: [],
  usuarioActivoId: null,
};

// ---------------------------------------------------------------------------
// Utilidades compartidas (duplicadas de app.js/operativo.js/finanzas.js:
// cada módulo carga su propio JS — ver comentario equivalente en esos
// archivos).
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
  document.querySelectorAll(".modal-overlay.abierto").forEach(overlay => overlay.classList.remove("abierto"));
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
  if (!res.ok) throw new Error(data.error || "Error inesperado");
  return data;
}

async function cargarSesion() {
  try {
    const s = await api("/api/sesion");
    document.getElementById("sesion-usuario").textContent = s.nombre ? `👤 ${s.nombre} (${s.rol})` : "";
    return s;
  } catch (e) {
    console.error(e);
    return {};
  }
}

// ---------------------------------------------------------------------------
// Listado de usuarios
// ---------------------------------------------------------------------------

const ROL_LABEL = { admin: "Admin", finanzas: "Finanzas", bodega: "Bodega", operativo: "Operativo" };

async function cargarUsuarios() {
  const tbody = document.getElementById("tbody-usuarios");
  try {
    const data = await api("/api/admin/usuarios");
    state.usuarios = data.usuarios;
    if (data.usuarios.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="vacio">No hay usuarios.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.usuarios.map(u => `
      <tr data-id="${u.id}" class="fila-clicable">
        <td class="mono">${escapeHtml(u.username)}</td>
        <td>${escapeHtml(u.nombre) || "—"}</td>
        <td>${ROL_LABEL[u.rol] || u.rol}</td>
        <td><span class="mov-tipo mov-tipo--${u.activo ? "entrada" : "perdida"}">${u.activo ? "Activa" : "Desactivada"}</span></td>
        <td class="mono">${fmtFecha(u.creado_en)}</td>
        <td><button type="button" class="btn btn-secundario" data-editar="${u.id}">Editar</button></td>
      </tr>`).join("");
    tbody.querySelectorAll("[data-editar]").forEach(btn => {
      btn.addEventListener("click", () => abrirEdicionUsuario(parseInt(btn.dataset.editar, 10)));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Error al cargar usuarios: ${e.message}</td></tr>`;
  }
}

// ---- Nuevo usuario ----

document.getElementById("btn-nuevo-usuario").addEventListener("click", () => {
  document.getElementById("form-nuevo-usuario").reset();
  abrirModal("modal-nuevo-usuario");
});

document.getElementById("form-nuevo-usuario").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/usuarios", {
      method: "POST",
      body: JSON.stringify({
        username: document.getElementById("nu-username").value,
        nombre: document.getElementById("nu-nombre").value,
        password: document.getElementById("nu-password").value,
        rol: document.getElementById("nu-rol").value,
      }),
    });
    toast("Usuario creado");
    cerrarModal("modal-nuevo-usuario");
    await cargarUsuarios();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---- Editar usuario ----

function abrirEdicionUsuario(id) {
  const u = state.usuarios.find(x => x.id === id);
  if (!u) return;
  state.usuarioActivoId = id;
  document.getElementById("eu-titulo").textContent = `Editar — ${u.username}`;
  document.getElementById("eu-id").value = u.id;
  document.getElementById("eu-nombre").value = u.nombre || "";
  document.getElementById("eu-rol").value = u.rol;
  document.getElementById("eu-activo").checked = !!u.activo;
  document.getElementById("eu-password-nueva").value = "";
  abrirModal("modal-editar-usuario");
}

document.getElementById("form-editar-usuario").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("eu-id").value;
  try {
    await api(`/api/admin/usuarios/${id}`, {
      method: "PUT",
      body: JSON.stringify({
        nombre: document.getElementById("eu-nombre").value,
        rol: document.getElementById("eu-rol").value,
        activo: document.getElementById("eu-activo").checked,
      }),
    });
    toast("Usuario actualizado");
    cerrarModal("modal-editar-usuario");
    await cargarUsuarios();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-resetear-password").addEventListener("click", async () => {
  const id = state.usuarioActivoId;
  const password = document.getElementById("eu-password-nueva").value;
  if (!id) return;
  if (password.length < 6) {
    toast("La contraseña debe tener al menos 6 caracteres", true);
    return;
  }
  try {
    await api(`/api/admin/usuarios/${id}/resetear-password`, {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    toast("Contraseña actualizada");
    document.getElementById("eu-password-nueva").value = "";
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Mantenimiento: limpiar CxC huérfanas (ver /api/admin/cxc/limpiar-huerfanas
// en modulos/operativo.py) — se puede correr las veces que sea, es un no-op
// si ya no queda nada por limpiar.
// ---------------------------------------------------------------------------

document.getElementById("btn-limpiar-cxc-huerfanas").addEventListener("click", async () => {
  const btn = document.getElementById("btn-limpiar-cxc-huerfanas");
  const resultado = document.getElementById("resultado-limpiar-cxc");
  btn.disabled = true;
  resultado.textContent = "Revisando…";
  try {
    const data = await api("/api/admin/cxc/limpiar-huerfanas", { method: "POST" });
    if (data.cantidad_eliminadas === 0) {
      resultado.textContent = "No había ninguna CxC huérfana para limpiar.";
    } else {
      const detalle = data.eliminadas
        .map(e => `OT #${e.orden_id} (${e.estado_actual}): $${Math.round(e.monto).toLocaleString("es-CL")}`)
        .join(" · ");
      resultado.textContent = `Se eliminaron ${data.cantidad_eliminadas} cuenta(s) por cobrar: ${detalle}`;
    }
    if (data.conservadas_con_pago && data.conservadas_con_pago.length) {
      resultado.textContent += ` — Ojo: ${data.conservadas_con_pago.length} más tenían un pago registrado y NO se tocaron (revisar a mano).`;
    }
    toast("Limpieza de CxC completada");
  } catch (e) {
    resultado.textContent = "";
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("btn-migracion-garantias-items").addEventListener("click", async () => {
  const btn = document.getElementById("btn-migracion-garantias-items");
  const resultado = document.getElementById("resultado-migracion-garantias-items");
  const input = document.getElementById("migracion-garantias-items-archivo");
  if (!input.files.length) {
    toast("Primero elige el archivo garantias_items_extraidos.json", true);
    return;
  }
  btn.disabled = true;
  resultado.textContent = "Migrando…";
  try {
    const formData = new FormData();
    formData.append("garantias_items_json", input.files[0]);
    const res = await fetch("/api/admin/migracion/garantias-items", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Error inesperado");
    resultado.textContent =
      `Garantías procesadas: ${data.garantias_procesadas} · ítems de repuesto: ${data.items_repuesto_creados} · ítems de mano de obra: ${data.items_mano_obra_creados} · ya migradas antes: ${data.garantias_ya_migradas}`
      + (data.garantias_sin_match.length ? ` · sin match: ${data.garantias_sin_match.length} (revisar RUT/fecha)` : "");
    toast("Migración de ítems de garantías completada");
  } catch (e) {
    resultado.textContent = "";
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarUsuarios();
