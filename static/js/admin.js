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

const MODULO_LABEL = { finanzas: "Finanzas", bodega: "Bodega", operativo: "Operativo", convertidores: "Convertidores" };

function etiquetaAccesoUsuario(u) {
  if (u.es_admin) return "Admin (todo)";
  const modulos = u.modulos || [];
  if (!modulos.length) return "Sin módulos";
  return modulos.map(m => MODULO_LABEL[m] || m).join(", ");
}

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
        <td>${escapeHtml(etiquetaAccesoUsuario(u))}</td>
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

// ---- Checkboxes de módulos (compartido entre nuevo/editar) ----

function leerModulosMarcados(claseCheckbox) {
  return [...document.querySelectorAll(`.${claseCheckbox}:checked`)].map(c => c.value);
}

function marcarModulos(claseCheckbox, modulos) {
  document.querySelectorAll(`.${claseCheckbox}`).forEach(c => {
    c.checked = (modulos || []).includes(c.value);
  });
}

function actualizarVisibilidadModulos(campoId, esAdminCheckboxId) {
  const campo = document.getElementById(campoId);
  const esAdmin = document.getElementById(esAdminCheckboxId).checked;
  campo.style.display = esAdmin ? "none" : "";
}

document.getElementById("nu-es-admin").addEventListener("change", () => {
  actualizarVisibilidadModulos("nu-modulos-campo", "nu-es-admin");
});
document.getElementById("eu-es-admin").addEventListener("change", () => {
  actualizarVisibilidadModulos("eu-modulos-campo", "eu-es-admin");
});

// ---- Nuevo usuario ----

document.getElementById("btn-nuevo-usuario").addEventListener("click", () => {
  document.getElementById("form-nuevo-usuario").reset();
  marcarModulos("nu-modulo", ["operativo"]);
  actualizarVisibilidadModulos("nu-modulos-campo", "nu-es-admin");
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
        es_admin: document.getElementById("nu-es-admin").checked,
        modulos: leerModulosMarcados("nu-modulo"),
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
  document.getElementById("eu-es-admin").checked = !!u.es_admin;
  marcarModulos("eu-modulo", u.modulos);
  actualizarVisibilidadModulos("eu-modulos-campo", "eu-es-admin");
  document.getElementById("eu-activo").checked = !!u.activo;
  document.getElementById("eu-password-nueva").value = "";
  abrirModal("modal-editar-usuario");
}

document.getElementById("form-editar-usuario").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("eu-id").value;
  const nuevoActivo = document.getElementById("eu-activo").checked;
  const nuevoEsAdmin = document.getElementById("eu-es-admin").checked;

  // Solo se confirma cuando el cambio es sensible (le corta el acceso a la
  // persona, o le da/saca permiso de Administrador) -- comparado contra los
  // valores que tenía ANTES de abrir el modal (state.usuarios sigue con la
  // lista vieja hasta que se recarga después de guardar). Editar solo el
  // nombre o los módulos, por ejemplo, sigue guardando directo sin
  // interrumpir. Antes ningún cambio acá pedía confirmación, a diferencia
  // de "Resetear contraseña" en el mismo modal (ver más abajo), que si bien
  // tampoco la tenía, es igual de sensible -- se agregó a las dos.
  const original = state.usuarios.find(u => String(u.id) === String(id));
  if (original) {
    if (original.activo && !nuevoActivo && !confirm(`¿Desactivar la cuenta de "${original.username}"? No va a poder iniciar sesión hasta que la reactives.`)) return;
    if (!original.es_admin && nuevoEsAdmin && !confirm(`¿Dar acceso de Administrador a "${original.username}"? Va a poder ver y modificar todos los módulos.`)) return;
    if (original.es_admin && !nuevoEsAdmin && !confirm(`¿Quitarle el acceso de Administrador a "${original.username}"?`)) return;
  }

  try {
    await api(`/api/admin/usuarios/${id}`, {
      method: "PUT",
      body: JSON.stringify({
        nombre: document.getElementById("eu-nombre").value,
        es_admin: nuevoEsAdmin,
        modulos: leerModulosMarcados("eu-modulo"),
        activo: nuevoActivo,
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
  const original = state.usuarios.find(u => String(u.id) === String(id));
  if (!confirm(`¿Resetear la contraseña de "${original ? original.username : "este usuario"}"? La contraseña anterior deja de funcionar de inmediato.`)) return;
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
  if (!confirm("¿Eliminar las Cuentas por Cobrar huérfanas? Se borran de forma permanente las que no tengan ningún pago registrado.")) return;
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
      + (data.garantias_sin_match.length ? ` · sin match: ${data.garantias_sin_match.length} (revisar RUT/fecha)` : "")
      + (data.garantias_con_error && data.garantias_con_error.length
          ? ` · con error: ${data.garantias_con_error.length} (${data.garantias_con_error.map(e => `legacy_no ${e.legacy_no}: ${e.error}`).join(" | ")})`
          : "");
    toast(
      data.garantias_con_error && data.garantias_con_error.length
        ? "Migración terminada con algunos errores — revisa el detalle abajo"
        : "Migración de ítems de garantías completada",
      !!(data.garantias_con_error && data.garantias_con_error.length),
    );
  } catch (e) {
    resultado.textContent = "";
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("btn-diagnostico-garantias").addEventListener("click", async () => {
  const btn = document.getElementById("btn-diagnostico-garantias");
  const resultado = document.getElementById("resultado-diagnostico-garantias");
  btn.disabled = true;
  resultado.textContent = "Consultando…";
  try {
    const data = await api("/api/admin/migracion/diagnostico-garantias");
    resultado.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    resultado.textContent = "";
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Respaldo de datos
// ---------------------------------------------------------------------------
// Movido acá desde Logística (donde vivía como un botón "Respaldo" suelto,
// visible incluso para bodega aunque los endpoints de abajo son admin-only
// -- ver @requiere_rol() en app.py) para juntar todo lo de administración
// del sistema en un solo lugar. A diferencia del modal de antes, acá se
// carga directo al entrar a la página (ver "Init" más abajo), sin botón
// disparador.

async function cargarDiagnosticoGeneral() {
  const cont = document.getElementById("respaldo-diagnostico");
  cont.textContent = "Cargando diagnóstico…";
  try {
    const info = await api("/api/estado");
    const disco = info.parece_disco_persistente ? "✓ usando disco persistente (/data)" : "⚠ NO parece estar usando un disco persistente — revisa la variable DB_PATH en Railway";
    cont.innerHTML = `
      Ruta de la base de datos: <span class="mono">${escapeHtml(info.db_path)}</span><br>
      ${disco}<br>
      Productos: ${info.cantidad_productos} · Facturas: ${info.cantidad_facturas} · Movimientos: ${info.cantidad_movimientos}
      (${info.tamano_kb} KB)
    `;
  } catch (e) {
    cont.textContent = "No se pudo cargar el diagnóstico: " + e.message;
  }
}

async function cargarDiagnosticoDisco() {
  const cont = document.getElementById("disco-diagnostico");
  cont.textContent = "Cargando…";
  try {
    const info = await api("/api/admin/diagnostico-disco");
    const d = info.disco;
    const clasePct = d.porcentaje_usado >= 90 ? "ayuda-inline--bloqueado" : "";
    const filas = info.desglose
      .slice()
      .sort((a, b) => b.mb - a.mb)
      .map(f => `
        <div class="disco-fila">
          <span>${escapeHtml(f.carpeta)}</span>
          <span class="mono">${f.mb} MB${f.archivos ? ` (${f.archivos} archivo${f.archivos === 1 ? "" : "s"})` : ""}</span>
        </div>
      `).join("");
    cont.innerHTML = `
      <p class="${clasePct}" style="font-weight:600">
        Disco: ${d.usado_mb} MB usados de ${d.total_mb} MB (${d.porcentaje_usado}%) — ${d.libre_mb} MB libres
      </p>
      <div class="disco-desglose">${filas}</div>
    `;
  } catch (e) {
    cont.textContent = "No se pudo cargar: " + e.message;
  }
}

document.getElementById("btn-refrescar-disco").addEventListener("click", cargarDiagnosticoDisco);

document.getElementById("btn-limpiar-respaldos-urgencia").addEventListener("click", async () => {
  if (!confirm("Esto borra TODOS los respaldos automáticos locales excepto los 3 más recientes, para liberar espacio ya. ¿Continuar?")) return;
  const btn = document.getElementById("btn-limpiar-respaldos-urgencia");
  btn.disabled = true;
  btn.textContent = "Liberando espacio…";
  try {
    const resultado = await api("/api/backup/locales/limpiar", { method: "POST", body: JSON.stringify({ conservar: 3 }) });
    toast(`Se borraron ${resultado.borrados} respaldos — ${resultado.mb_liberados} MB liberados.`);
    await cargarRespaldosLocales();
    await cargarDiagnosticoDisco();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Liberar espacio ahora (borra respaldos viejos, deja los 3 más recientes)";
  }
});

async function cargarRespaldosLocales() {
  const cont = document.getElementById("respaldo-locales-lista");
  cont.textContent = "Cargando…";
  try {
    const info = await api("/api/backup/locales");
    if (!info.copias.length) {
      cont.innerHTML = `Todavía no hay copias automáticas (se crea la primera al minuto de arrancar el servidor, cada ${info.intervalo_minutos} minutos).`;
      return;
    }
    cont.innerHTML = info.copias.map(c => `
      <div class="respaldo-local-fila">
        <span>${escapeHtml(c.creado_en)} · ${c.tamano_kb} KB</span>
        <button type="button" class="btn btn-secundario btn-chico" data-restaurar-local="${escapeHtml(c.nombre)}" data-fecha-local="${escapeHtml(c.creado_en)}">Restaurar esta copia</button>
      </div>
    `).join("");
    cont.querySelectorAll("[data-restaurar-local]").forEach(btn => {
      btn.addEventListener("click", () => restaurarRespaldoLocal(btn.dataset.restaurarLocal, btn.dataset.fechaLocal));
    });
  } catch (e) {
    cont.textContent = "No se pudo cargar: " + e.message;
  }
}

document.getElementById("btn-crear-respaldo-local").addEventListener("click", async () => {
  const btn = document.getElementById("btn-crear-respaldo-local");
  btn.disabled = true;
  btn.textContent = "Creando…";
  try {
    await api("/api/backup/locales/crear", { method: "POST" });
    toast("Respaldo local creado.");
    await cargarRespaldosLocales();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Crear un respaldo ahora";
  }
});

// A diferencia de cuando esto vivía en Logística (donde se refrescaban
// dashboard/productos/resumen en el propio módulo tras restaurar), acá lo
// más simple y confiable es recargar la página entera -- una restauración
// reemplaza TODOS los datos, así que cualquier cosa en pantalla (usuarios
// incluidos) puede haber cambiado.
async function restaurarRespaldoLocal(nombre, fecha) {
  if (!confirm(`Esto reemplaza TODOS los datos actuales por los del respaldo de ${fecha}. ¿Continuar?`)) return;
  try {
    await api("/api/backup/locales/restaurar", { method: "POST", body: JSON.stringify({ nombre }) });
    toast("Datos restaurados correctamente");
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    toast(e.message, true);
  }
}

async function cargarEstadoRespaldoAutomatico() {
  const cont = document.getElementById("respaldo-auto-estado");
  cont.textContent = "Cargando…";
  try {
    const info = await api("/api/backup/estado-automatico");
    if (!info.configurado) {
      cont.innerHTML = `⚠ No está configurado todavía. Se necesita agregar la variable <span class="mono">RESEND_API_KEY</span> en Railway (recomendado), o las variables <span class="mono">SMTP_USER</span> y <span class="mono">SMTP_PASSWORD</span> (ver guía DEPLOY.md).`;
      return;
    }
    const ultimo = info.historial.find(h => h.exito);
    const ultimoTexto = ultimo ? `Último envío exitoso: ${fmtFecha(ultimo.fecha)}` : "Todavía no se ha enviado ningún respaldo exitoso.";
    cont.innerHTML = `
      ✓ Configurado — se envía a <span class="mono">${escapeHtml(info.destinatario)}</span> cada ${info.intervalo_horas}h.<br>
      ${ultimoTexto}
    `;
  } catch (e) {
    cont.textContent = "No se pudo cargar: " + e.message;
  }
}

document.getElementById("btn-probar-respaldo-auto").addEventListener("click", async () => {
  const btn = document.getElementById("btn-probar-respaldo-auto");
  btn.disabled = true;
  btn.textContent = "Enviando…";
  try {
    await api("/api/backup/probar-automatico", { method: "POST" });
    toast("Respaldo de prueba enviado. Revisa tu correo.");
    await cargarEstadoRespaldoAutomatico();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Enviar un respaldo de prueba ahora";
  }
});

async function cargarEstadoAlertasStock() {
  const cont = document.getElementById("alertas-stock-estado");
  cont.textContent = "Cargando…";
  try {
    const info = await api("/api/alertas-stock/estado");
    if (!info.configurado) {
      cont.innerHTML = `⚠ No está configurado todavía. Se necesita agregar la variable <span class="mono">RESEND_API_KEY</span> en Railway (recomendado), o las variables <span class="mono">SMTP_USER</span> y <span class="mono">SMTP_PASSWORD</span> (ver guía DEPLOY.md) — las mismas que usa el respaldo automático.`;
      return;
    }
    const ultimoEnviado = info.historial.find(h => h.correo_enviado);
    const ultimoTexto = ultimoEnviado
      ? `Último correo de alerta enviado: ${fmtFecha(ultimoEnviado.fecha)} (${ultimoEnviado.n_productos} producto(s))`
      : "Todavía no se ha enviado ningún correo de alerta (no ha habido productos críticos, o aún no se hace el primer chequeo).";
    const criticosAhora = info.n_productos_criticos_ahora > 0
      ? `<strong>${info.n_productos_criticos_ahora} producto(s) con stock crítico ahora mismo.</strong>`
      : "No hay productos con stock crítico en este momento.";
    cont.innerHTML = `
      ✓ Configurado — se revisa y avisa a <span class="mono">${escapeHtml(info.destinatario)}</span> cada ${info.intervalo_horas}h si corresponde.<br>
      ${criticosAhora}<br>
      ${ultimoTexto}
    `;
  } catch (e) {
    cont.textContent = "No se pudo cargar: " + e.message;
  }
}

document.getElementById("btn-probar-alertas-stock").addEventListener("click", async () => {
  const btn = document.getElementById("btn-probar-alertas-stock");
  btn.disabled = true;
  btn.textContent = "Revisando…";
  try {
    const data = await api("/api/alertas-stock/probar", { method: "POST" });
    toast(data.n_productos > 0
      ? `Alerta enviada: ${data.n_productos} producto(s) con stock crítico.`
      : "Revisado: no hay productos con stock crítico por ahora.");
    await cargarEstadoAlertasStock();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Revisar y enviar ahora si corresponde";
  }
});

document.getElementById("form-restaurar").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("restaurar-archivo");
  if (!input.files.length) {
    toast("Selecciona un archivo primero", true);
    return;
  }
  if (!confirm("Esto reemplaza TODOS los datos actuales por los del archivo. ¿Continuar?")) return;

  const formData = new FormData();
  formData.append("archivo", input.files[0]);
  try {
    const res = await fetch("/api/backup/restaurar", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Error al restaurar");
    toast("Datos restaurados correctamente");
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarUsuarios();
cargarDiagnosticoGeneral();
cargarDiagnosticoDisco();
cargarEstadoRespaldoAutomatico();
cargarEstadoAlertasStock();
cargarRespaldosLocales();
