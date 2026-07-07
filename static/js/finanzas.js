const state = {
  sesion: { username: null, nombre: null },
  cajaCategoriasVistas: [],
  cajaPendientesSeleccionados: new Set(),
  cajaMovimientos: [],
  cajaMovimientoActivoId: null,
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

document.querySelectorAll(".tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-resumen").style.display = vista === "resumen" ? "block" : "none";
    document.getElementById("vista-caja").style.display = vista === "caja" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      resumen: "Finanzas — Resumen",
      caja: "Finanzas — Caja Chica",
      reportes: "Finanzas — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "resumen") cargarResumenFinanzas();
    if (vista === "caja") cargarCaja();
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
  } catch (e) {
    toast(e.message, true);
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
    document.getElementById("caja-saldo").textContent = fmtMoney(d.saldo_disponible);
    document.getElementById("caja-pendiente").textContent = fmtMoney(d.pendiente_rendir);
    document.getElementById("caja-gastado-mes").textContent = fmtMoney(d.gastado_mes);
    document.getElementById("caja-total-aportado").textContent = fmtMoney(d.total_aportado);
  } catch (e) {
    toast(e.message, true);
  }
}

async function cargarCajaMovimientos() {
  const tbody = document.getElementById("tbody-caja");
  const params = new URLSearchParams();
  const q = document.getElementById("caja-buscar").value.trim();
  const tipo = document.getElementById("caja-filtro-tipo").value;
  const estado = document.getElementById("caja-filtro-estado").value;
  const desde = document.getElementById("caja-filtro-desde").value;
  const hasta = document.getElementById("caja-filtro-hasta").value;
  if (q) params.set("q", q);
  if (tipo) params.set("tipo", tipo);
  if (estado) params.set("estado", estado);
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);

  try {
    const data = await api("/api/caja/movimientos?" + params.toString());

    state.cajaCategoriasVistas = [...new Set(
      data.movimientos.filter(m => m.tipo === "gasto" && m.categoria).map(m => m.categoria)
    )];
    document.getElementById("cg-categorias-usadas").innerHTML =
      state.cajaCategoriasVistas.map(c => `<option value="${escapeHtml(c)}"></option>`).join("");

    state.cajaMovimientos = data.movimientos;

    if (data.movimientos.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No se encontraron movimientos con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.movimientos.map(m => {
      const esGasto = m.tipo === "gasto";
      const catDesc = [m.categoria, m.descripcion].filter(Boolean).join(" — ") || "—";
      const comprobante = m.tiene_comprobante
        ? `<a href="/api/caja/comprobante/${encodeURIComponent(m.comprobante_archivo)}" target="_blank" rel="noopener" data-sin-navegar="1">Ver</a>`
        : "—";
      const estadoTxt = esGasto ? (m.rendido ? `<span class="mov-tipo mov-tipo--entrada">rendido</span>` : `<span class="mov-tipo mov-tipo--perdida">pendiente</span>`) : "—";
      return `
      <tr data-id="${m.id}" data-tipo="${m.tipo}" class="fila-clicable">
        <td class="mono">${fmtFecha(m.fecha)}</td>
        <td><span class="mov-tipo mov-tipo--${esGasto ? "salida" : "entrada"}">${esGasto ? "gasto" : "aporte"}</span></td>
        <td>${escapeHtml(catDesc)}</td>
        <td>${escapeHtml(m.proveedor) || "—"}</td>
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
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar Caja Chica: ${e.message}</td></tr>`;
  }
}

let cajaBuscarTimeout;
document.getElementById("caja-buscar").addEventListener("input", () => {
  clearTimeout(cajaBuscarTimeout);
  cajaBuscarTimeout = setTimeout(cargarCajaMovimientos, 250);
});
document.getElementById("caja-filtro-tipo").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-estado").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-desde").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-filtro-hasta").addEventListener("change", cargarCajaMovimientos);
document.getElementById("caja-limpiar-filtros").addEventListener("click", () => {
  document.getElementById("caja-buscar").value = "";
  document.getElementById("caja-filtro-tipo").value = "";
  document.getElementById("caja-filtro-estado").value = "";
  document.getElementById("caja-filtro-desde").value = "";
  document.getElementById("caja-filtro-hasta").value = "";
  cargarCajaMovimientos();
});

// ---- Nuevo gasto ----

document.getElementById("btn-nuevo-gasto").addEventListener("click", async () => {
  document.getElementById("form-caja-gasto").reset();
  document.getElementById("cg-id").value = "";
  document.getElementById("cg-emergencia-campos").style.display = "none";
  document.getElementById("modal-caja-gasto-titulo").textContent = "Nuevo gasto";
  document.getElementById("cg-bloqueado-aviso").style.display = "none";
  document.getElementById("cg-comprobante-actual").style.display = "none";
  document.getElementById("cg-emergencia-wrap").style.display = "block";
  document.getElementById("cg-acciones-editar").style.display = "none";
  habilitarFormularioCaja("cg", true);
  try {
    const data = await api("/api/productos");
    const select = document.getElementById("cg-producto-id");
    select.innerHTML = '<option value="">Selecciona un producto…</option>' +
      data.productos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("");
  } catch (e) {
    toast(e.message, true);
  }
  abrirModal("modal-caja-gasto");
});

document.getElementById("cg-emergencia").addEventListener("change", (e) => {
  document.getElementById("cg-emergencia-campos").style.display = e.target.checked ? "block" : "none";
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
  document.getElementById("cg-categoria").value = m.categoria || "";
  document.getElementById("cg-proveedor").value = m.proveedor || "";
  document.getElementById("cg-descripcion").value = m.descripcion || "";
  document.getElementById("cg-comprobante-actual").style.display = m.tiene_comprobante ? "block" : "none";
  document.getElementById("cg-comprobante-actual").innerHTML = m.tiene_comprobante
    ? `Ya tiene un comprobante cargado. <a href="/api/caja/comprobante/${encodeURIComponent(m.comprobante_archivo)}" target="_blank" rel="noopener">Verlo</a> — sube uno nuevo para reemplazarlo.`
    : "";
  // La compra de emergencia queda fija al crear el gasto (ya generó su
  // propio movimiento de inventario); no tiene sentido editarla después.
  document.getElementById("cg-emergencia-wrap").style.display = m.movimiento_inventario_id ? "none" : "block";
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
  if (!confirm("¿Eliminar este gasto?")) return;
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
  formData.append("categoria", document.getElementById("cg-categoria").value);
  formData.append("proveedor", document.getElementById("cg-proveedor").value);
  formData.append("descripcion", document.getElementById("cg-descripcion").value);
  const archivo = document.getElementById("cg-comprobante").files[0];
  if (archivo) formData.append("comprobante", archivo);
  if (!id && document.getElementById("cg-emergencia").checked) {
    formData.append("producto_id", document.getElementById("cg-producto-id").value);
    formData.append("cantidad", document.getElementById("cg-cantidad").value);
  }

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

// ---- Nuevo aporte ----

document.getElementById("btn-nuevo-aporte").addEventListener("click", () => {
  document.getElementById("form-caja-aporte").reset();
  document.getElementById("ca-id").value = "";
  document.getElementById("modal-caja-aporte-titulo").textContent = "Nuevo aporte / reposición";
  document.getElementById("ca-bloqueado-aviso").style.display = "none";
  document.getElementById("ca-comprobante-actual").style.display = "none";
  document.getElementById("ca-acciones-editar").style.display = "none";
  habilitarFormularioCaja("ca", true);
  abrirModal("modal-caja-aporte");
});

function abrirEdicionCajaAporte(id) {
  const m = (state.cajaMovimientos || []).find(x => x.id === id);
  if (!m) return;
  document.getElementById("form-caja-aporte").reset();
  document.getElementById("ca-id").value = m.id;
  document.getElementById("ca-monto").value = m.monto;
  document.getElementById("ca-fecha").value = (m.fecha || "").slice(0, 10);
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
  if (!confirm("¿Eliminar este aporte?")) return;
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

function actualizarTotalSeleccionadoRendicion() {
  const total = [...state.cajaPendientesSeleccionados].reduce((suma, id) => {
    const chk = document.querySelector(`[data-pendiente-id="${id}"]`);
    return suma + (chk ? Number(chk.dataset.monto) : 0);
  }, 0);
  document.getElementById("cr-total-seleccionado").textContent = fmtMoney(total);
}

document.getElementById("btn-generar-rendicion").addEventListener("click", async () => {
  state.cajaPendientesSeleccionados = new Set();
  document.getElementById("cr-nota").value = "";
  document.getElementById("cr-reponer-saldo").checked = true;
  const cont = document.getElementById("cr-lista-pendientes");
  cont.innerHTML = `<p class="vacio">Cargando…</p>`;
  abrirModal("modal-caja-rendicion");

  try {
    const data = await api("/api/caja/pendientes");
    if (data.gastos.length === 0) {
      cont.innerHTML = `<p class="vacio">No hay gastos pendientes de rendir. 👍</p>`;
      actualizarTotalSeleccionadoRendicion();
      return;
    }
    cont.innerHTML = `
      <table class="tabla">
        <thead>
          <tr><th></th><th>Fecha</th><th>Categoría / Descripción</th><th class="num">Monto</th></tr>
        </thead>
        <tbody>
          ${data.gastos.map(g => `
            <tr>
              <td><input type="checkbox" data-pendiente-id="${g.id}" data-monto="${g.monto}"></td>
              <td class="mono">${fmtFecha(g.fecha)}</td>
              <td>${escapeHtml([g.categoria, g.descripcion].filter(Boolean).join(" — ") || "—")}</td>
              <td class="num mono">${fmtMoney(g.monto)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    `;
    cont.querySelectorAll("[data-pendiente-id]").forEach(chk => {
      chk.addEventListener("change", () => {
        const id = Number(chk.dataset.pendienteId);
        if (chk.checked) state.cajaPendientesSeleccionados.add(id);
        else state.cajaPendientesSeleccionados.delete(id);
        actualizarTotalSeleccionadoRendicion();
      });
    });
    actualizarTotalSeleccionadoRendicion();
  } catch (e) {
    cont.innerHTML = `<p class="vacio">Error al cargar gastos pendientes: ${e.message}</p>`;
  }
});

document.getElementById("btn-confirmar-rendicion").addEventListener("click", async () => {
  const gastoIds = [...state.cajaPendientesSeleccionados];
  if (gastoIds.length === 0) {
    toast("Selecciona al menos un gasto", true);
    return;
  }
  const reponerSaldo = document.getElementById("cr-reponer-saldo").checked;
  try {
    await api("/api/caja/rendicion", {
      method: "POST",
      body: JSON.stringify({
        gasto_ids: gastoIds,
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
  tbody.innerHTML = `<tr><td colspan="6" class="vacio">Cargando…</td></tr>`;
  abrirModal("modal-caja-rendiciones-historial");
  try {
    const data = await api("/api/caja/rendiciones");
    if (data.rendiciones.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="vacio">Todavía no se ha generado ninguna rendición.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.rendiciones.map(r => `
      <tr data-id="${r.id}">
        <td class="mono">${fmtFecha(r.fecha)}</td>
        <td class="num mono">${r.n_gastos}</td>
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
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Error al cargar el historial: ${e.message}</td></tr>`;
  }
});

async function verDetalleRendicion(id) {
  document.getElementById("modal-caja-rendicion-detalle-titulo").textContent = `Rendición #${id}`;
  const ficha = document.getElementById("crd-ficha");
  const tbody = document.getElementById("tbody-caja-rendicion-detalle");
  ficha.innerHTML = "";
  tbody.innerHTML = `<tr><td colspan="3" class="vacio">Cargando…</td></tr>`;
  abrirModal("modal-caja-rendicion-detalle");
  try {
    const r = await api(`/api/caja/rendicion/${id}`);
    ficha.innerHTML = `
      <dt>Fecha</dt><dd>${fmtFecha(r.fecha)}</dd>
      <dt>Total</dt><dd class="mono">${fmtMoney(r.total)}</dd>
      <dt>N° de gastos</dt><dd class="mono">${r.gastos.length}</dd>
      <dt>¿Repuso el saldo?</dt><dd>${r.reposicion_automatica ? "Sí — se creó un aporte automático por el mismo total" : "No — estos gastos ya estaban cubiertos"}</dd>
      <dt>Nota</dt><dd>${escapeHtml(r.nota) || "—"}</dd>
      <dt>Responsable</dt><dd>${escapeHtml(r.usuario) || "—"}</dd>
    `;
    tbody.innerHTML = r.gastos.length
      ? r.gastos.map(g => `
        <tr>
          <td class="mono">${fmtFecha(g.fecha)}</td>
          <td>${escapeHtml([g.categoria, g.descripcion].filter(Boolean).join(" — ") || "—")}</td>
          <td class="num mono">${fmtMoney(g.monto)}</td>
        </tr>`).join("")
      : `<tr><td colspan="3" class="vacio">Sin gastos asociados.</td></tr>`;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="vacio">Error: ${e.message}</td></tr>`;
  }
}

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
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarResumenFinanzas();
