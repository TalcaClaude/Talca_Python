const state = {
  sesion: { username: null, nombre: null },
  cajaCategoriasVistas: [],
  cajaPendientesSeleccionados: new Set(),
  cajaMovimientos: [],
  cajaMovimientoActivoId: null,
};

const ESTADO_CXX_LABEL = { pendiente: "Pendiente", parcial: "Parcial", pagada: "Pagada", anulada: "Anulada" };

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
    // El backend siempre rechaza esto si el rol no es finanzas/admin (ver
    // @requiere_rol en app.py) — acá solo se evita mostrar un botón que de
    // todas formas iba a fallar.
    const puedeFinanzas = s.rol === "admin" || s.rol === "finanzas";
    const btnNuevaFacturaPendiente = document.getElementById("btn-nueva-factura-pendiente");
    if (btnNuevaFacturaPendiente) btnNuevaFacturaPendiente.style.display = puedeFinanzas ? "" : "none";
    const btnNuevaCxpManual = document.getElementById("btn-nueva-cxp-manual");
    if (btnNuevaCxpManual) btnNuevaCxpManual.style.display = puedeFinanzas ? "" : "none";
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
    document.getElementById("vista-cxc").style.display = vista === "cxc" ? "block" : "none";
    document.getElementById("vista-cxp").style.display = vista === "cxp" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      resumen: "Finanzas — Resumen",
      caja: "Finanzas — Caja Chica",
      cxc: "Finanzas — Cuentas por Cobrar",
      cxp: "Finanzas — Cuentas por Pagar",
      reportes: "Finanzas — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "resumen") cargarResumenFinanzas();
    if (vista === "caja") cargarCaja();
    if (vista === "cxc") cargarCxc();
    if (vista === "cxp") { cargarCxp(); cargarFacturasPendientesRecepcion(); }
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
    document.getElementById("cxc-total-pendiente").textContent = fmtMoney(data.total_pendiente);

    if (data.cuentas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="vacio">No se encontraron cuentas por cobrar con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.cuentas.map(c => `
      <tr>
        <td>${escapeHtml(c.cliente_nombre)}</td>
        <td class="mono">#${c.orden_id}</td>
        <td class="mono">${c.fecha_entrega_real ? fmtFecha(c.fecha_entrega_real) : "—"}</td>
        <td class="mono">${c.fecha_vencimiento ? fmtFecha(c.fecha_vencimiento) : "—"}</td>
        <td class="num mono">${fmtMoney(c.monto_total)}</td>
        <td class="num mono">${fmtMoney(c.total_pagado)}</td>
        <td class="num mono">${fmtMoney(c.saldo)}</td>
        <td><span class="mov-tipo mov-tipo--${c.estado === "pagada" ? "entrada" : c.estado === "parcial" ? "salida" : "perdida"}">${ESTADO_CXX_LABEL[c.estado] || c.estado}</span></td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="vacio">Error al cargar Cuentas por Cobrar: ${e.message}</td></tr>`;
  }
}

let cxcBuscarTimeout;
document.getElementById("cxc-buscar").addEventListener("input", () => {
  clearTimeout(cxcBuscarTimeout);
  cxcBuscarTimeout = setTimeout(cargarCxc, 250);
});
document.getElementById("cxc-filtro-estado").addEventListener("change", cargarCxc);

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
      const puedeFinanzas = state.sesion && (state.sesion.rol === "admin" || state.sesion.rol === "finanzas");
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
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar Cuentas por Pagar: ${e.message}</td></tr>`;
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

const ESTADO_PFR_LABEL = { pendiente: "Pendiente de revisión", aprobada: "Aprobada", rechazada: "Rechazada" };

async function cargarFacturasPendientesRecepcion() {
  const tbody = document.getElementById("tbody-facturas-pendientes");
  try {
    const data = await api("/api/facturas/pendientes-recepcion");
    if (data.pendientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="vacio">Todavía no se ha registrado ninguna factura por este camino.</td></tr>`;
      return;
    }
    const etiquetaCondicion = { contado: "Contado", credito: "Crédito", mixto: "Mixto" };
    tbody.innerHTML = data.pendientes.map(p => `
      <tr>
        <td>${escapeHtml(p.numero)}</td>
        <td>${escapeHtml(p.proveedor_nombre) || "—"}</td>
        <td>${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}${p.condicion_pago === "credito" ? ` (${p.plazo_dias_credito}d)` : ""}${p.condicion_pago === "mixto" ? ` (${p.porcentaje_contado}% contado)` : ""}</td>
        <td class="num mono">${fmtMoney(p.monto_total)}</td>
        <td><span class="mov-tipo mov-tipo--${p.estado === "aprobada" ? "entrada" : p.estado === "rechazada" ? "perdida" : "salida"}">${ESTADO_PFR_LABEL[p.estado] || p.estado}</span></td>
        <td>${escapeHtml(p.creado_por) || "—"}</td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Error al cargar: ${e.message}</td></tr>`;
  }
}

function actualizarTotalFacturaPendiente() {
  const filas = Array.from(document.querySelectorAll("#pfr-lineas .fac-linea-row"));
  const total = filas.reduce((acc, fila) => {
    const cantidad = parseFloat(fila.querySelector(".pfr-linea-cantidad").value) || 0;
    const precio = parseFloat(fila.querySelector(".pfr-linea-precio").value) || 0;
    return acc + cantidad * precio;
  }, 0);
  document.getElementById("pfr-total-vivo").textContent = fmtMoney(total);
}

function filaLineaFacturaPendiente(productos) {
  const div = document.createElement("div");
  div.className = "fac-linea-row";
  div.innerHTML = `
    <div class="campo">
      <label>Producto</label>
      <select class="pfr-linea-producto-id">
        <option value="">Selecciona un producto…</option>
        ${productos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("")}
      </select>
    </div>
    <div class="campo">
      <label>Cantidad</label>
      <input type="number" class="pfr-linea-cantidad" min="1" value="1">
    </div>
    <div class="campo">
      <label>Precio unit. ($ CLP)</label>
      <input type="number" class="pfr-linea-precio" min="0" step="1" value="0">
    </div>
    <button type="button" class="fac-linea-quitar">Quitar</button>
  `;
  div.querySelector(".fac-linea-quitar").addEventListener("click", () => {
    div.remove();
    actualizarTotalFacturaPendiente();
  });
  div.querySelector(".pfr-linea-cantidad").addEventListener("input", actualizarTotalFacturaPendiente);
  div.querySelector(".pfr-linea-precio").addEventListener("input", actualizarTotalFacturaPendiente);
  return div;
}

let pfrProductosCache = [];

document.getElementById("btn-nueva-factura-pendiente").addEventListener("click", async () => {
  document.getElementById("form-factura-pendiente").reset();
  document.getElementById("pfr-lineas").innerHTML = "";
  document.getElementById("pfr-campos-mixto").style.display = "none";
  document.getElementById("pfr-campos-credito").style.display = "block";
  try {
    const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
    pfrProductosCache = dataProductos.productos;
    const select = document.getElementById("pfr-proveedor-id");
    select.innerHTML = '<option value="">Sin proveedor</option>' +
      dataProveedores.proveedores.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
  } catch (e) {
    toast(e.message, true);
    return;
  }
  document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache));
  actualizarTotalFacturaPendiente();
  abrirModal("modal-factura-pendiente");
});

document.getElementById("pfr-agregar-linea").addEventListener("click", () => {
  document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache));
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
  const lineas = filas.map(fila => ({
    producto_id: fila.querySelector(".pfr-linea-producto-id").value,
    cantidad: fila.querySelector(".pfr-linea-cantidad").value,
    precio_unitario: fila.querySelector(".pfr-linea-precio").value,
  }));
  if (lineas.some(l => !l.producto_id)) {
    toast("Selecciona un producto en cada línea (o quítala)", true);
    return;
  }
  const condicion = document.querySelector('input[name="pfr-condicion"]:checked').value;

  try {
    await api("/api/facturas/pendientes-recepcion", {
      method: "POST",
      body: JSON.stringify({
        numero: document.getElementById("pfr-numero").value,
        proveedor_id: document.getElementById("pfr-proveedor-id").value || null,
        fecha: document.getElementById("pfr-fecha").value,
        lineas,
        condicion_pago: condicion,
        porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("pfr-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
        plazo_dias_credito: Number(document.getElementById("pfr-plazo-dias").value || 30),
      }),
    });
    toast("Factura registrada — queda pendiente de aprobar la recepción");
    cerrarModal("modal-factura-pendiente");
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
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarResumenFinanzas();
