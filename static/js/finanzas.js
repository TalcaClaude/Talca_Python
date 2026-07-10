const state = {
  sesion: { username: null, nombre: null },
  cajaCategoriasVistas: [],
  cajaPendientesSeleccionados: new Set(),
  cajaMovimientos: [],
  cajaMovimientoActivoId: null,
  cxcCuentas: [],
  cxcAbonosCuentas: [],
  cxcOtActiva: null, // { ordenId, contexto: "cerradas" | "abonos" }
  cmfToken: null,
  cmfFacturas: [],
  cmfProductos: [],
  cmfProveedores: [],
  facturasPendientes: [],
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

function diasDesde(fechaIso) {
  if (!fechaIso) return "—";
  const d = new Date(fechaIso);
  if (isNaN(d)) return "—";
  const dias = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (dias < 0) return "0";
  return String(dias);
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
    err.data = data;
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

// OJO: excluye los botones de #cxc-subtabs (también tienen clase "tab") —
// si no, un click en "Por abonos"/"OTs cerradas" también dispara este
// listener (dataset.vista queda undefined, así que caía al "else" de cada
// línea y ocultaba TODAS las vistas, dejando la pantalla en blanco).
document.querySelectorAll(".tabs:not(.sub-tabs) > .tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tabs:not(.sub-tabs) > .tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-resumen").style.display = vista === "resumen" ? "block" : "none";
    document.getElementById("vista-caja").style.display = vista === "caja" ? "block" : "none";
    document.getElementById("vista-cxc").style.display = vista === "cxc" ? "block" : "none";
    document.getElementById("vista-cxp").style.display = vista === "cxp" ? "block" : "none";
    document.getElementById("vista-facturas").style.display = vista === "facturas" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    const titulos = {
      resumen: "Finanzas — Resumen",
      caja: "Finanzas — Caja Chica",
      cxc: "Finanzas — Cuentas por Cobrar",
      cxp: "Finanzas — Cuentas por Pagar",
      facturas: "Finanzas — Facturas",
      reportes: "Finanzas — Reportes",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "resumen") cargarResumenFinanzas();
    if (vista === "caja") cargarCaja();
    if (vista === "cxc") { cargarCxc(); cargarCxcAbonos(); }
    if (vista === "cxp") cargarCxp();
    if (vista === "facturas") cargarFacturasPendientesRecepcion();
  });
});

// ---------------------------------------------------------------------------
// Sub-pestañas de Cuentas por Cobrar: "OTs cerradas" (tabla de siempre,
// cuentas_por_cobrar) y "Por abonos" (OTs en reparación que todavía no
// alcanzan el anticipo pactado — ver /api/finanzas/cxc/abonos).
// ---------------------------------------------------------------------------

document.querySelectorAll("#cxc-subtabs .tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll("#cxc-subtabs .tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const sub = tabBtn.dataset.subvista;
    document.getElementById("cxc-vista-cerradas").style.display = sub === "cerradas" ? "block" : "none";
    document.getElementById("cxc-vista-abonos").style.display = sub === "abonos" ? "block" : "none";
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
    state.cxcCuentas = data.cuentas;
    document.getElementById("cxc-total-pendiente").textContent = fmtMoney(data.total_pendiente);

    if (data.cuentas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="vacio">No se encontraron cuentas por cobrar con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.cuentas.map(c => `
      <tr data-orden-id="${c.orden_id}" class="fila-clicable">
        <td>${escapeHtml(c.cliente_nombre)}</td>
        <td class="mono">#${c.orden_id}</td>
        <td class="mono">${c.fecha_entrega_real ? fmtFecha(c.fecha_entrega_real) : "—"}</td>
        <td class="num mono">${fmtMoney(c.monto_total)}</td>
        <td class="num mono">${fmtMoney(c.total_pagado)}</td>
        <td class="num mono">${fmtMoney(c.saldo)}</td>
        <td><span class="mov-tipo mov-tipo--${c.estado === "pagada" ? "entrada" : c.estado === "parcial" ? "salida" : "perdida"}">${ESTADO_CXX_LABEL[c.estado] || c.estado}</span></td>
        <td class="num mono">${diasDesde(c.fecha_emision)}</td>
      </tr>`).join("");
    tbody.querySelectorAll("tr[data-orden-id]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleOtCxc(parseInt(tr.dataset.ordenId, 10), "cerradas"));
    });
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
// Cuentas por Cobrar "por abonos" — OTs que ya entraron a reparación y
// todavía no alcanzan el anticipo pactado (ver /api/finanzas/cxc/abonos en
// modulos/finanzas.py). Es una consulta en vivo, no una tabla congelada
// como cuentas_por_cobrar: por eso no tiene filtro de estado ni columna de
// estado, solo "cuánto falta para llegar al anticipo".
// ---------------------------------------------------------------------------

async function cargarCxcAbonos() {
  const tbody = document.getElementById("tbody-cxc-abonos");
  const params = new URLSearchParams();
  const q = document.getElementById("cxc-abonos-buscar").value.trim();
  if (q) params.set("q", q);

  try {
    const data = await api("/api/finanzas/cxc/abonos?" + params.toString());
    state.cxcAbonosCuentas = data.cuentas;
    document.getElementById("cxc-abonos-total-pendiente").textContent = fmtMoney(data.total_pendiente);

    if (data.cuentas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No hay OTs en reparación que estén bajo el anticipo pactado. 👍</td></tr>`;
      return;
    }

    tbody.innerHTML = data.cuentas.map(c => {
      const equipo = [c.equipo_marca, c.equipo_modelo, c.equipo_identificador ? `(${c.equipo_identificador})` : ""]
        .filter(Boolean).join(" ") || "—";
      return `
      <tr data-orden-id="${c.orden_id}" class="fila-clicable">
        <td>${escapeHtml(c.cliente_nombre)}</td>
        <td class="mono">#${c.orden_id}</td>
        <td>${escapeHtml(equipo)}</td>
        <td class="mono">${c.fecha_recepcion ? fmtFecha(c.fecha_recepcion) : "—"}</td>
        <td class="num mono">${fmtMoney(c.valor_total)}</td>
        <td class="num mono">${fmtMoney(c.anticipo_esperado)}</td>
        <td class="num mono">${fmtMoney(c.total_pagado)}</td>
        <td class="num mono">${fmtMoney(c.saldo_anticipo)}</td>
        <td class="num mono">${diasDesde(c.fecha_recepcion)}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll("tr[data-orden-id]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleOtCxc(parseInt(tr.dataset.ordenId, 10), "abonos"));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar CxC por abonos: ${e.message}</td></tr>`;
  }
}

let cxcAbonosBuscarTimeout;
document.getElementById("cxc-abonos-buscar").addEventListener("input", () => {
  clearTimeout(cxcAbonosBuscarTimeout);
  cxcAbonosBuscarTimeout = setTimeout(cargarCxcAbonos, 250);
});

// ---------------------------------------------------------------------------
// Detalle de una OT desde Cuentas por Cobrar (cualquiera de las dos
// sub-pestañas) — datos de cliente/equipo, plata (total/pagado/saldo, y el
// anticipo esperado si viene del contexto "abonos"), y un mini-formulario
// para registrar un pago sin tener que salir a Operativo. Reutiliza
// GET /api/operativo/ordenes/<id> (mismo endpoint que la ficha completa de
// la OT) y POST .../pagos.
// ---------------------------------------------------------------------------

async function abrirDetalleOtCxc(ordenId, contexto) {
  state.cxcOtActiva = { ordenId, contexto };
  document.getElementById("cxcd-titulo").textContent = `OT #${ordenId}`;
  document.getElementById("cxcd-orden-id").value = ordenId;
  document.getElementById("cxcd-ficha").innerHTML = "<dt>Cargando…</dt><dd></dd>";
  document.getElementById("form-cxcd-pago").reset();
  abrirModal("modal-cxc-ot-detalle");
  try {
    const o = await api(`/api/operativo/ordenes/${ordenId}`);
    const equipo = o.equipo
      ? [o.equipo.marca, o.equipo.modelo, o.equipo.identificador ? `(${o.equipo.identificador})` : ""].filter(Boolean).join(" ")
      : "—";
    let filas = `
      <dt>Cliente</dt><dd>${escapeHtml(o.cliente ? o.cliente.nombre : "—")}</dd>
      <dt>Teléfono</dt><dd>${escapeHtml(o.cliente ? o.cliente.telefono : "") || "—"}</dd>
      <dt>Equipo</dt><dd>${escapeHtml(equipo)}</dd>
      <dt>Estado de la OT</dt><dd>${escapeHtml(o.estado)}</dd>
      <dt>Valor total</dt><dd class="mono">${fmtMoney(o.valor_total)}</dd>
      <dt>Pagado</dt><dd class="mono">${fmtMoney(o.total_pagado)}</dd>
      <dt>Saldo pendiente</dt><dd class="mono">${fmtMoney(o.total_por_pagar)}</dd>
    `;
    if (contexto === "abonos") {
      const c = state.cxcAbonosCuentas.find(x => x.orden_id === ordenId);
      if (c) {
        filas += `
          <dt>Anticipo esperado (50%)</dt><dd class="mono">${fmtMoney(c.anticipo_esperado)}</dd>
          <dt>Falta para el anticipo</dt><dd class="mono">${fmtMoney(c.saldo_anticipo)}</dd>
        `;
      }
    }
    document.getElementById("cxcd-ficha").innerHTML = filas;
    document.getElementById("cxcd-monto").value = o.total_por_pagar > 0 ? o.total_por_pagar : "";
  } catch (e) {
    document.getElementById("cxcd-ficha").innerHTML = `<dt>Error</dt><dd>${escapeHtml(e.message)}</dd>`;
  }
}

document.getElementById("form-cxcd-pago").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ordenId = document.getElementById("cxcd-orden-id").value;
  try {
    await api(`/api/operativo/ordenes/${ordenId}/pagos`, {
      method: "POST",
      body: JSON.stringify({
        monto: Number(document.getElementById("cxcd-monto").value),
        metodo_pago: document.getElementById("cxcd-metodo").value,
      }),
    });
    toast("Pago registrado");
    cerrarModal("modal-cxc-ot-detalle");
    // No hace falta lógica especial acá: se vuelve a pedir el listado que
    // estaba abierto y, si el pago hizo que la fila ya no califique (llegó
    // al anticipo, o se saldó del todo), el backend simplemente deja de
    // devolverla.
    const contexto = state.cxcOtActiva ? state.cxcOtActiva.contexto : "cerradas";
    if (contexto === "abonos") await cargarCxcAbonos();
    else await cargarCxc();
    await cargarResumenFinanzas();
  } catch (e) {
    toast(e.message, true);
  }
});

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
const ORIGEN_PFR_LABEL = { manual: "Manual", excel: "Excel", foto: "Foto", whatsapp: "WhatsApp" };

async function cargarFacturasPendientesRecepcion() {
  const tbody = document.getElementById("tbody-facturas-pendientes");
  try {
    const data = await api("/api/facturas/pendientes-recepcion");
    state.facturasPendientes = data.pendientes;
    if (data.pendientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="vacio">Todavía no se ha registrado ninguna factura por este camino.</td></tr>`;
      return;
    }
    const etiquetaCondicion = { contado: "Contado", credito: "Crédito", mixto: "Mixto" };
    tbody.innerHTML = data.pendientes.map(p => {
      const acciones = [
        p.estado === "pendiente" ? `<button type="button" class="btn btn-secundario btn-chico" data-pfr-editar="${p.id}">Editar</button>` : "",
        p.origen === "whatsapp" ? `<a class="btn btn-secundario btn-chico" href="/api/facturas/pendientes-recepcion/${p.id}/foto" target="_blank" rel="noopener">Ver foto</a>` : "",
      ].filter(Boolean).join(" ");
      return `
      <tr class="fila-clicable" data-id="${p.id}">
        <td>${escapeHtml(p.numero) || "—"}</td>
        <td>${escapeHtml(p.proveedor_nombre) || "—"}</td>
        <td>${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}${p.condicion_pago === "credito" ? ` (${p.plazo_dias_credito}d)` : ""}${p.condicion_pago === "mixto" ? ` (${p.porcentaje_contado}% contado)` : ""}</td>
        <td class="num mono">${fmtMoney(p.monto_total)}</td>
        <td><span class="mov-tipo mov-tipo--${p.estado === "aprobada" ? "entrada" : p.estado === "rechazada" ? "perdida" : "salida"}">${ESTADO_PFR_LABEL[p.estado] || p.estado}</span></td>
        <td><span class="badge-origen">${ORIGEN_PFR_LABEL[p.origen] || p.origen || "—"}</span></td>
        <td>${escapeHtml(p.creado_por) || "—"}</td>
        <td>${acciones}</td>
      </tr>`;
    }).join("");
    // La fila entera es clickeable para revisar (ver detalle, o eliminar
    // si ya fue aprobada) — los botones de "Editar"/"Ver foto" están
    // dentro de la misma fila, así que se detiene la propagación del
    // click para que no abran el detalle Y la acción del botón a la vez.
    tbody.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.addEventListener("click", () => abrirDetalleFacturaPendiente(parseInt(tr.dataset.id, 10)));
    });
    tbody.querySelectorAll("[data-pfr-editar]").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        abrirEdicionFacturaPendiente(parseInt(btn.dataset.pfrEditar, 10));
      });
    });
    tbody.querySelectorAll("a[href]").forEach(a => a.addEventListener("click", (e) => e.stopPropagation()));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="vacio">Error al cargar: ${e.message}</td></tr>`;
  }
}

// ---------------------------------------------------------------------------
// Detalle de una factura (pendiente, aprobada o rechazada) al hacer click en
// su fila — para las ya aprobadas, muestra las líneas tal como quedaron en
// la factura real (con si se aplicaron o no a stock) y permite eliminarla
// (mismo endpoint DELETE /api/facturas/<id> de siempre, que revierte el
// stock que haya aplicado — pide confirmación en dos pasos igual que el
// viejo modal de Logística: primero avisa qué se va a revertir, y solo al
// confirmar de nuevo ejecuta el borrado con forzar=1).
// ---------------------------------------------------------------------------

let pfdPendienteActivo = null;
let pfdFacturaIdReal = null;

async function abrirDetalleFacturaPendiente(id) {
  const p = state.facturasPendientes.find(x => x.id === id);
  if (!p) return;
  pfdPendienteActivo = p;
  pfdFacturaIdReal = null;

  document.getElementById("pfd-titulo").textContent = `Factura ${p.numero || "(sin folio)"}`;
  document.getElementById("pfd-confirmar-eliminar").style.display = "none";
  document.getElementById("pfd-btn-eliminar-confirmar").style.display = "none";
  document.getElementById("pfd-btn-eliminar").style.display = "none";
  document.getElementById("pfd-btn-editar").style.display = "none";

  const etiquetaCondicion = {
    contado: "Al contado",
    credito: `A crédito, ${p.plazo_dias_credito} días`,
    mixto: `Mixto — ${p.porcentaje_contado}% contado / ${100 - p.porcentaje_contado}% a crédito (${p.plazo_dias_credito} días)`,
  };

  let lineasParaMostrar = p.lineas || [];
  let facturaReal = null;
  if (p.estado === "aprobada" && p.factura_id) {
    try {
      facturaReal = await api(`/api/facturas/${p.factura_id}`);
      lineasParaMostrar = facturaReal.lineas;
      pfdFacturaIdReal = p.factura_id;
    } catch (e) {
      // Si no se pudo traer la factura real (ej. se eliminó desde otro
      // lado), se sigue mostrando lo que había quedado guardado en el
      // pendiente, solo que sin poder ofrecer "Eliminar".
    }
  }

  document.getElementById("pfd-info").innerHTML = `
    <p><strong>Proveedor:</strong> ${escapeHtml(p.proveedor_nombre) || "—"}</p>
    <p><strong>Fecha:</strong> ${escapeHtml(p.fecha) || "—"}</p>
    <p><strong>Condición de pago:</strong> ${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}</p>
    <p><strong>Estado:</strong> ${ESTADO_PFR_LABEL[p.estado] || p.estado}</p>
    <p><strong>Origen:</strong> ${ORIGEN_PFR_LABEL[p.origen] || p.origen || "—"}</p>
    <p><strong>Registrada por:</strong> ${escapeHtml(p.creado_por) || "—"}</p>
    ${p.estado === "rechazada" && p.nota_rechazo ? `<p><strong>Motivo de rechazo:</strong> ${escapeHtml(p.nota_rechazo)}</p>` : ""}
  `;

  document.getElementById("pfd-lineas").innerHTML = lineasParaMostrar.map(l => `
    <tr>
      <td>${escapeHtml(l.producto_nombre) || `Producto #${l.producto_id}`}</td>
      <td class="num mono">${l.cantidad}</td>
      <td class="num mono">${fmtMoney(l.precio_unitario)}</td>
      <td>${facturaReal ? (l.aplicada ? "Ingresada a stock" : "No aplicada") : "—"}</td>
    </tr>`).join("");

  if (p.estado === "pendiente") {
    document.getElementById("pfd-btn-editar").style.display = "";
  }
  if (pfdFacturaIdReal) {
    document.getElementById("pfd-btn-eliminar").style.display = "";
  }

  abrirModal("modal-factura-pendiente-detalle");
}

document.getElementById("pfd-btn-editar").addEventListener("click", () => {
  cerrarModal("modal-factura-pendiente-detalle");
  if (pfdPendienteActivo) abrirEdicionFacturaPendiente(pfdPendienteActivo.id);
});

document.getElementById("pfd-btn-eliminar").addEventListener("click", async () => {
  if (!pfdFacturaIdReal) return;
  try {
    await api(`/api/facturas/${pfdFacturaIdReal}`, { method: "DELETE" });
    toast("Factura eliminada");
    cerrarModal("modal-factura-pendiente-detalle");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    if (e.requiereForzar && e.data && e.data.lineas_a_revertir) {
      document.getElementById("pfd-eliminar-resumen").textContent = e.data.error;
      document.getElementById("pfd-eliminar-tbody").innerHTML = e.data.lineas_a_revertir.map(l => `
        <tr>
          <td>${escapeHtml(l.producto_nombre)}${l.codigo_interno ? ` (#${escapeHtml(l.codigo_interno)})` : ""}</td>
          <td class="num mono">${l.cantidad}</td>
        </tr>`).join("");
      document.getElementById("pfd-confirmar-eliminar").style.display = "block";
      document.getElementById("pfd-btn-eliminar").style.display = "none";
      document.getElementById("pfd-btn-eliminar-confirmar").style.display = "";
    } else {
      toast(e.message, true);
    }
  }
});

document.getElementById("pfd-btn-eliminar-confirmar").addEventListener("click", async () => {
  if (!pfdFacturaIdReal) return;
  try {
    await api(`/api/facturas/${pfdFacturaIdReal}?forzar=1`, { method: "DELETE" });
    toast("Factura eliminada y stock revertido");
    cerrarModal("modal-factura-pendiente-detalle");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    toast(e.message, true);
  }
});

function actualizarTotalFacturaPendiente() {
  const filas = Array.from(document.querySelectorAll("#pfr-lineas .fac-linea-row"));
  const total = filas.reduce((acc, fila) => {
    const cantidad = parseFloat(fila.querySelector(".pfr-linea-cantidad").value) || 0;
    const precio = parseFloat(fila.querySelector(".pfr-linea-precio").value) || 0;
    return acc + cantidad * precio;
  }, 0);
  document.getElementById("pfr-total-vivo").textContent = fmtMoney(total);
}

function filaLineaFacturaPendiente(productos, linea) {
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
      <input type="number" class="pfr-linea-cantidad" min="1" value="${linea && linea.cantidad != null ? linea.cantidad : 1}">
    </div>
    <div class="campo">
      <label>Precio unit. ($ CLP)</label>
      <input type="number" class="pfr-linea-precio" min="0" step="1" value="${linea && linea.precio_unitario != null ? linea.precio_unitario : 0}">
    </div>
    <button type="button" class="fac-linea-quitar">Quitar</button>
  `;
  if (linea && linea.producto_id) {
    div.querySelector(".pfr-linea-producto-id").value = linea.producto_id;
  }
  div.querySelector(".fac-linea-quitar").addEventListener("click", () => {
    div.remove();
    actualizarTotalFacturaPendiente();
  });
  div.querySelector(".pfr-linea-cantidad").addEventListener("input", actualizarTotalFacturaPendiente);
  div.querySelector(".pfr-linea-precio").addEventListener("input", actualizarTotalFacturaPendiente);
  return div;
}

let pfrProductosCache = [];
let pfrProveedoresCache = [];

async function pfrCargarCatalogos() {
  const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
  pfrProductosCache = dataProductos.productos;
  pfrProveedoresCache = dataProveedores.proveedores;
  const select = document.getElementById("pfr-proveedor-id");
  select.innerHTML = '<option value="">Sin proveedor</option>' +
    pfrProveedoresCache.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
}

document.getElementById("btn-nueva-factura-pendiente").addEventListener("click", async () => {
  document.getElementById("form-factura-pendiente").reset();
  document.getElementById("pfr-id").value = "";
  document.getElementById("pfr-titulo").textContent = "Registrar factura de compra";
  document.getElementById("pfr-btn-guardar").textContent = "Registrar factura";
  document.getElementById("pfr-lineas").innerHTML = "";
  document.getElementById("pfr-campos-mixto").style.display = "none";
  document.getElementById("pfr-campos-credito").style.display = "block";
  document.getElementById("pfr-seccion-condicion").style.display = "block";
  document.getElementById("pfr-aviso-condicion-fija").style.display = "none";
  document.getElementById("pfr-aviso-origen").style.display = "none";
  document.getElementById("pfr-foto-whatsapp").style.display = "none";
  document.getElementById("pfr-producto-resultados").style.display = "none";
  try {
    await pfrCargarCatalogos();
  } catch (e) {
    toast(e.message, true);
    return;
  }
  document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache));
  actualizarTotalFacturaPendiente();
  abrirModal("modal-factura-pendiente");
});

// ---- Editar un pendiente antes de aprobar/rechazar (clave para completar
// folio/productos de los que llegaron por WhatsApp con datos incompletos) ----

async function abrirEdicionFacturaPendiente(id) {
  try {
    const [p] = await Promise.all([api(`/api/facturas/pendientes-recepcion/${id}`), pfrCargarCatalogos()]);
    document.getElementById("form-factura-pendiente").reset();
    document.getElementById("pfr-id").value = p.id;
    document.getElementById("pfr-titulo").textContent = `Editar factura pendiente #${p.id}`;
    document.getElementById("pfr-btn-guardar").textContent = "Guardar cambios";
    document.getElementById("pfr-numero").value = p.numero || "";
    document.getElementById("pfr-proveedor-id").value = p.proveedor_id || "";
    document.getElementById("pfr-fecha").value = (p.fecha || "").slice(0, 10);

    const etiquetaCondicion = { contado: "Al contado", credito: `A crédito, ${p.plazo_dias_credito} días`, mixto: `Mixto — ${p.porcentaje_contado}% contado` };
    document.getElementById("pfr-seccion-condicion").style.display = "none";
    const avisoCondicion = document.getElementById("pfr-aviso-condicion-fija");
    avisoCondicion.textContent = `Condición de pago ya pactada: ${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}. La condición de pago ya se fijó al registrar esta factura y no se puede editar acá — solo el número, proveedor, fecha y productos.`;
    avisoCondicion.style.display = "block";

    const avisoOrigen = document.getElementById("pfr-aviso-origen");
    const fotoImg = document.getElementById("pfr-foto-whatsapp");
    if (p.origen === "whatsapp") {
      avisoOrigen.textContent = "Esta factura llegó por WhatsApp — revisa que el folio y cada producto queden bien identificados antes de aprobarla.";
      avisoOrigen.style.display = "block";
      fotoImg.src = `/api/facturas/pendientes-recepcion/${p.id}/foto`;
      fotoImg.style.display = "block";
    } else {
      avisoOrigen.style.display = "none";
      fotoImg.style.display = "none";
    }

    const cont = document.getElementById("pfr-lineas");
    cont.innerHTML = "";
    (p.lineas && p.lineas.length ? p.lineas : [{}]).forEach(linea => {
      cont.appendChild(filaLineaFacturaPendiente(pfrProductosCache, linea));
    });
    actualizarTotalFacturaPendiente();
    document.getElementById("pfr-producto-resultados").style.display = "none";
    abrirModal("modal-factura-pendiente");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("pfr-agregar-linea").addEventListener("click", () => {
  document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache));
  actualizarTotalFacturaPendiente();
});

// ---------------------------------------------------------------------------
// Buscador de producto para agregar líneas a una factura de compra: al
// hacer clic en un resultado se agrega la línea al tiro (cantidad 1, precio
// sugerido = costo de compra del producto), SIN cerrar la lista de
// resultados — así se puede seguir escribiendo/clickeando otros productos
// sin perder el foco ni tener que reabrir nada (mismo patrón que
// renderResultadosOpoi en operativo.js, para "Carga de repuestos" en una OT).
// ---------------------------------------------------------------------------

function renderResultadosPfr(texto) {
  const resultados = document.getElementById("pfr-producto-resultados");
  const q = texto.trim();
  if (!q) {
    resultados.style.display = "none";
    return;
  }
  const coincidencias = pfrProductosCache.filter(p =>
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
    // mousedown (no click) para que se dispare antes del blur del input,
    // que si no cerraría la lista antes de alcanzar a procesar el click.
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const p = pfrProductosCache.find(x => x.id === parseInt(item.dataset.id, 10));
      if (p) agregarLineaFacturaDesdeAutocomplete(p);
    });
  });
}

function agregarLineaFacturaDesdeAutocomplete(producto) {
  // Si el producto ya tiene una línea en la factura, en vez de duplicarla
  // se le suma 1 a la cantidad existente (mismo criterio de "no permitir
  // producto repetido en la misma factura" que ya validaba el registro
  // manual — ver _validar_lineas_pendiente en app.py).
  const filaExistente = Array.from(document.querySelectorAll("#pfr-lineas .fac-linea-row"))
    .find(fila => parseInt(fila.querySelector(".pfr-linea-producto-id").value, 10) === producto.id);
  if (filaExistente) {
    const inputCantidad = filaExistente.querySelector(".pfr-linea-cantidad");
    inputCantidad.value = (parseFloat(inputCantidad.value) || 0) + 1;
  } else {
    document.getElementById("pfr-lineas").appendChild(filaLineaFacturaPendiente(pfrProductosCache, {
      producto_id: producto.id,
      cantidad: 1,
      precio_unitario: producto.costo_compra || 0,
    }));
  }
  actualizarTotalFacturaPendiente();
  toast(`Agregado: ${producto.nombre}`);
}

document.getElementById("pfr-buscar-producto").addEventListener("input", (e) => {
  renderResultadosPfr(e.target.value);
});
document.getElementById("pfr-buscar-producto").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosPfr(e.target.value);
});
document.getElementById("pfr-buscar-producto").addEventListener("blur", () => {
  // Pequeño delay para que el mousedown del resultado alcance a dispararse
  // antes de que el blur oculte la lista.
  setTimeout(() => { document.getElementById("pfr-producto-resultados").style.display = "none"; }, 150);
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

  const id = document.getElementById("pfr-id").value;

  try {
    if (id) {
      await api(`/api/facturas/pendientes-recepcion/${id}`, {
        method: "PUT",
        body: JSON.stringify({
          numero: document.getElementById("pfr-numero").value,
          proveedor_id: document.getElementById("pfr-proveedor-id").value || null,
          fecha: document.getElementById("pfr-fecha").value,
          lineas,
        }),
      });
      toast("Factura pendiente actualizada");
    } else {
      const condicion = document.querySelector('input[name="pfr-condicion"]:checked').value;
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
    }
    cerrarModal("modal-factura-pendiente");
    await cargarFacturasPendientesRecepcion();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Carga masiva de facturas de compra (Excel o foto escaneada) — deja cada
// factura válida como pendiente de recepción, con la misma condición de
// pago elegida para todo el lote (ver POST /api/facturas/carga-masiva/*
// en app.py). Reutiliza el mismo token de previsualización para Excel y
// para foto: la única diferencia es qué endpoint arma la previsualización.
// ---------------------------------------------------------------------------

function cmfMostrarPaso(paso) {
  document.getElementById("cmf-paso-subir").style.display = paso === "subir" ? "block" : "none";
  document.getElementById("cmf-paso-preview").style.display = paso === "preview" ? "block" : "none";
}

async function cmfCargarCatalogos() {
  if (state.cmfProductos.length && state.cmfProveedores.length) return;
  const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
  state.cmfProductos = dataProductos.productos;
  state.cmfProveedores = dataProveedores.proveedores;
}

function cmfAbrirPreview(data) {
  state.cmfToken = data.token;
  state.cmfFacturas = data.facturas;
  cmfMostrarPaso("preview");
  cmfRenderPreview(data.resumen);
}

function cmfRenderPreview(resumen) {
  document.getElementById("cmf-resumen").textContent =
    `${resumen.total_facturas} factura(s) leída(s) — ${resumen.validas} lista(s) para confirmar, ${resumen.con_errores} con errores por corregir.`;

  document.getElementById("cmf-lista-facturas").innerHTML = state.cmfFacturas.map(f => {
    const proveedorOpciones = '<option value="">— Sin proveedor —</option>' +
      state.cmfProveedores.map(p => `<option value="${p.id}" ${p.id === f.proveedor_id ? "selected" : ""}>${escapeHtml(p.nombre)}</option>`).join("");
    const erroresGenerales = (f.errores || []).filter(err => !(f.lineas || []).some(l => (l.errores || []).includes(err)));
    return `
      <div class="dash-panel" style="margin-bottom:16px" data-cmf-factura="${escapeHtml(f.numero)}">
        <h3 class="dash-titulo">${escapeHtml(f.numero)} ${f.valida ? "✅" : "⚠️"}</h3>
        ${erroresGenerales.length ? `<p class="ayuda-inline ayuda-inline--bloqueado">${erroresGenerales.map(escapeHtml).join(" · ")}</p>` : ""}
        <div class="campo">
          <label>Proveedor${f.proveedor_nuevo ? " (nuevo — se creará al confirmar)" : ""}</label>
          <select class="cmf-factura-proveedor-id" data-cmf-factura="${escapeHtml(f.numero)}">${proveedorOpciones}</select>
        </div>
        <div class="tabla-wrap">
          <table class="tabla">
            <thead><tr><th>Producto</th><th class="num">Cantidad</th><th class="num">Precio unit.</th><th></th></tr></thead>
            <tbody>
              ${(f.lineas || []).map(l => `
                <tr data-cmf-linea="${escapeHtml(f.numero)}|${l.fila}">
                  <td>
                    <select class="cmf-linea-producto-id">
                      <option value="">Selecciona un producto…</option>
                      ${state.cmfProductos.map(p => `<option value="${p.id}" ${p.id === l.producto_id ? "selected" : ""}>#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("")}
                    </select>
                    ${l.texto_original ? `<p class="ayuda-inline">Leído: "${escapeHtml(l.texto_original)}"</p>` : ""}
                  </td>
                  <td class="num"><input type="number" class="cmf-linea-cantidad" min="1" value="${l.cantidad ?? ""}" style="width:70px"></td>
                  <td class="num"><input type="number" class="cmf-linea-precio" min="0" step="1" value="${l.precio_unitario ?? ""}" style="width:90px"></td>
                  <td>
                    ${(l.errores || []).length ? `<p class="ayuda-inline ayuda-inline--bloqueado">${l.errores.map(escapeHtml).join(" · ")}</p>` : ""}
                    <button type="button" class="btn btn-secundario btn-chico cmf-linea-guardar">Guardar corrección</button>
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }).join("");

  document.querySelectorAll("#cmf-lista-facturas .cmf-linea-guardar").forEach(btn => {
    btn.addEventListener("click", () => cmfGuardarCorreccionLinea(btn.closest("tr[data-cmf-linea]")));
  });
  document.querySelectorAll("#cmf-lista-facturas .cmf-factura-proveedor-id").forEach(select => {
    select.addEventListener("change", () => cmfGuardarCorreccionProveedor(select));
  });
}

async function cmfGuardarCorreccionLinea(tr) {
  const [numeroFactura, filaStr] = tr.dataset.cmfLinea.split("|");
  const productoId = tr.querySelector(".cmf-linea-producto-id").value;
  const cantidad = tr.querySelector(".cmf-linea-cantidad").value;
  const precio = tr.querySelector(".cmf-linea-precio").value;
  try {
    const data = await api("/api/facturas/carga-masiva/corregir-linea", {
      method: "POST",
      body: JSON.stringify({
        token: state.cmfToken,
        numero_factura: numeroFactura,
        fila: parseInt(filaStr, 10),
        producto_id: productoId || null,
        cantidad: cantidad || null,
        precio_unitario: precio || null,
      }),
    });
    const idx = state.cmfFacturas.findIndex(f => f.numero === numeroFactura);
    if (idx >= 0) state.cmfFacturas[idx] = data.factura;
    cmfRenderPreview(data.resumen);
    toast("Corrección guardada");
  } catch (e) {
    toast(e.message, true);
  }
}

async function cmfGuardarCorreccionProveedor(select) {
  const numeroFactura = select.dataset.cmfFactura;
  try {
    const data = await api("/api/facturas/carga-masiva/corregir-proveedor", {
      method: "POST",
      body: JSON.stringify({ token: state.cmfToken, numero_factura: numeroFactura, proveedor_id: select.value || null }),
    });
    const idx = state.cmfFacturas.findIndex(f => f.numero === numeroFactura);
    if (idx >= 0) state.cmfFacturas[idx] = data.factura;
    cmfRenderPreview(data.resumen);
    toast("Proveedor corregido");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-cxp-carga-masiva").addEventListener("click", async () => {
  document.getElementById("cmf-archivo").value = "";
  document.getElementById("cmf-titulo").textContent = "Carga masiva de facturas (Excel)";
  state.cmfToken = null;
  state.cmfFacturas = [];
  cmfMostrarPaso("subir");
  abrirModal("modal-cxp-carga-masiva");
});

document.getElementById("btn-cmf-volver").addEventListener("click", () => {
  document.getElementById("cmf-archivo").value = "";
  document.getElementById("cmf-titulo").textContent = "Carga masiva de facturas (Excel)";
  cmfMostrarPaso("subir");
});

document.getElementById("btn-cmf-previsualizar").addEventListener("click", async () => {
  const archivo = document.getElementById("cmf-archivo").files[0];
  if (!archivo) {
    toast("Selecciona un archivo Excel primero", true);
    return;
  }
  const formData = new FormData();
  formData.append("archivo", archivo);
  try {
    await cmfCargarCatalogos();
    const res = await fetch("/api/facturas/carga-masiva/previsualizar", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo previsualizar el archivo");
    cmfAbrirPreview(data);
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-cxp-escanear-foto").addEventListener("click", () => {
  document.getElementById("input-cxp-escanear-foto").click();
});

document.getElementById("input-cxp-escanear-foto").addEventListener("change", async (e) => {
  const archivo = e.target.files[0];
  if (!archivo) return;
  const formData = new FormData();
  formData.append("foto", archivo);
  document.getElementById("cmf-archivo").value = "";
  state.cmfToken = null;
  state.cmfFacturas = [];
  cmfMostrarPaso("subir");
  document.getElementById("cmf-titulo").textContent = "Factura escaneada — revisa antes de confirmar";
  abrirModal("modal-cxp-carga-masiva");
  try {
    await cmfCargarCatalogos();
    const res = await fetch("/api/facturas/escanear-foto", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo leer la foto");
    document.getElementById("btn-cmf-volver").textContent = "Escanear otra foto";
    cmfAbrirPreview(data);
  } catch (e) {
    toast(e.message, true);
    cerrarModal("modal-cxp-carga-masiva");
  }
  e.target.value = "";
});

document.querySelectorAll('input[name="cmf-condicion"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const condicion = document.querySelector('input[name="cmf-condicion"]:checked').value;
    document.getElementById("cmf-campos-mixto").style.display = condicion === "mixto" ? "grid" : "none";
    document.getElementById("cmf-campos-credito").style.display = condicion === "contado" ? "none" : "grid";
  });
});

document.querySelectorAll("[data-plazo-cmf]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.getElementById("cmf-plazo-dias").value = btn.dataset.plazoCmf;
  });
});

document.getElementById("btn-cmf-confirmar").addEventListener("click", async () => {
  if (!state.cmfToken) return;
  const condicion = document.querySelector('input[name="cmf-condicion"]:checked').value;
  try {
    const data = await api("/api/facturas/carga-masiva/confirmar", {
      method: "POST",
      body: JSON.stringify({
        token: state.cmfToken,
        condicion_pago: condicion,
        porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("cmf-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
        plazo_dias_credito: Number(document.getElementById("cmf-plazo-dias").value || 30),
      }),
    });
    toast(`${data.total_creadas} factura(s) quedaron pendientes de recepción` +
      (data.omitidas.length ? ` — se omitieron ${data.omitidas.length} con errores (${data.omitidas.join(", ")})` : ""));
    cerrarModal("modal-cxp-carga-masiva");
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
