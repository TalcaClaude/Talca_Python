// ---------------------------------------------------------------------------
// Aviso global de facturas de compra pendientes de recepción — se incluye
// en TODAS las plantillas autenticadas (ver _alerta_facturas_pendientes.html)
// para que revisar estas facturas no dependa de acordarse de entrar a un
// módulo en particular. Sondea el contador cada cierto tiempo y, mientras
// haya al menos una pendiente, deja un botón flotante fijo en pantalla
// (con una animación que llama la atención) que abre el listado desde
// cualquier lugar de la app.
//
// Deliberadamente NO depende de las utilidades del *.js de cada módulo
// (api(), toast(), etc.) — inicio.html (el selector de módulos) no carga
// ninguno de esos archivos, así que este script trae sus propias copias
// con prefijo "afp" para poder incluirse en cualquier plantilla sin
// preocuparse de qué otro script ya se cargó ahí.
// ---------------------------------------------------------------------------

const AFP_INTERVALO_SONDEO_MS = 25000;
let afpPendientesCache = [];
let afpDetalleActivoId = null;
let afpYaAbrioAutomaticamente = false;
let afpPuedeBodega = true; // se corrige apenas resuelve afpCargarRol() — hasta entonces no se ocultan los botones para evitar parpadeo

function afpEscapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function afpFmtMoney(n) {
  return "$" + Math.round(n).toLocaleString("es-CL");
}

function afpToast(msg, isError = false) {
  const el = document.getElementById("afp-toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.add("visible");
  clearTimeout(afpToast._t);
  afpToast._t = setTimeout(() => el.classList.remove("visible"), 3200);
}

function afpAbrirModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const abiertos = [...document.querySelectorAll(".modal-overlay.abierto")]
    .map(o => parseInt(getComputedStyle(o).zIndex, 10) || 50);
  el.style.zIndex = abiertos.length ? Math.max(...abiertos) + 1 : "";
  el.classList.add("abierto");
}
function afpCerrarModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("abierto");
  el.style.zIndex = "";
}

document.querySelectorAll('[data-close="afp-modal-lista"], [data-close="afp-modal-detalle"]').forEach(btn => {
  btn.addEventListener("click", () => afpCerrarModal(btn.dataset.close));
});

async function afpApi(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Error inesperado");
  return data;
}

async function afpCargarRol() {
  try {
    const s = await afpApi("/api/sesion");
    afpPuedeBodega = s.rol === "admin" || (s.modulos || []).includes("bodega");
  } catch (e) {
    // Sin sesión válida (ej. venimos de /login) — no hay nada que sondear.
  }
}

async function afpSondear() {
  const flotante = document.getElementById("afp-flotante");
  if (!flotante) return; // esta plantilla no incluyó el partial
  try {
    const d = await afpApi("/api/facturas/pendientes-recepcion/contador");
    const texto = document.getElementById("afp-flotante-texto");

    if (d.pendientes > 0) {
      flotante.style.display = "block";
      texto.textContent = d.pendientes === 1 ? "1 factura por revisar" : `${d.pendientes} facturas por revisar`;
      // La primera vez que esta pestaña detecta pendientes, se abre sola
      // el listado — después queda el botón flotante, para no ser tan
      // invasivo como para no poder trabajar en otra cosa mientras tanto.
      if (!afpYaAbrioAutomaticamente) {
        afpYaAbrioAutomaticamente = true;
        afpAbrirLista();
      }
    } else {
      flotante.style.display = "none";
    }
  } catch (e) {
    // Silencioso: si falla el sondeo (ej. la sesión expiró, o esta
    // plantilla es la de login sin sesión) no queremos spamear toasts de
    // fondo en cada pantalla de la app.
  }
}

async function afpAbrirLista() {
  const tbody = document.getElementById("afp-tbody-lista");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="5" class="vacio">Cargando…</td></tr>`;
  afpAbrirModal("afp-modal-lista");
  try {
    const data = await afpApi("/api/facturas/pendientes-recepcion?estado=pendiente");
    afpPendientesCache = data.pendientes;
    if (data.pendientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="vacio">No hay facturas pendientes de recepción. 👍</td></tr>`;
      return;
    }
    const etiquetaCondicion = { contado: "Contado", credito: "Crédito", mixto: "Mixto" };
    tbody.innerHTML = data.pendientes.map(p => `
      <tr data-id="${p.id}" class="fila-clicable">
        <td>${afpEscapeHtml(p.numero)}</td>
        <td>${afpEscapeHtml(p.proveedor_nombre) || "—"}</td>
        <td>${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}${p.condicion_pago === "credito" ? ` (${p.plazo_dias_credito}d)` : ""}${p.condicion_pago === "mixto" ? ` (${p.porcentaje_contado}% contado)` : ""}</td>
        <td class="num mono">${afpFmtMoney(p.monto_total)}</td>
        <td><button type="button" class="btn btn-secundario" data-revisar="${p.id}">Revisar</button></td>
      </tr>`).join("");
    tbody.querySelectorAll("[data-revisar]").forEach(btn => {
      btn.addEventListener("click", () => afpAbrirDetalle(parseInt(btn.dataset.revisar, 10)));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Error al cargar: ${e.message}</td></tr>`;
  }
}

function afpAbrirDetalle(id) {
  const p = afpPendientesCache.find(x => x.id === id);
  if (!p) return;
  afpDetalleActivoId = id;
  document.getElementById("afp-detalle-titulo").textContent = `Factura ${p.numero}`;
  document.getElementById("afp-nota-rechazo").value = "";

  const etiquetaCondicion = {
    contado: "Al contado",
    credito: `A crédito, ${p.plazo_dias_credito} días`,
    mixto: `Mixto — ${p.porcentaje_contado}% contado / ${100 - p.porcentaje_contado}% a crédito (${p.plazo_dias_credito} días)`,
  };
  document.getElementById("afp-detalle-ficha").innerHTML = `
    <dt>Proveedor</dt><dd>${afpEscapeHtml(p.proveedor_nombre) || "—"}</dd>
    <dt>Fecha</dt><dd>${afpEscapeHtml(p.fecha) || "—"}</dd>
    <dt>Condición de pago</dt><dd>${etiquetaCondicion[p.condicion_pago] || p.condicion_pago}</dd>
    <dt>Monto facturado</dt><dd class="mono">${afpFmtMoney(p.monto_total)}</dd>
    <dt>Registrada por</dt><dd>${afpEscapeHtml(p.creado_por) || "—"}</dd>
  `;

  const tbody = document.getElementById("afp-tbody-lineas");
  tbody.innerHTML = p.lineas.map(l => `
    <tr>
      <td>${afpEscapeHtml(l.producto_nombre) || `Producto #${l.producto_id}`}</td>
      <td class="num mono">${l.cantidad}</td>
      <td class="num afp-linea-recibido">
        <input type="number" min="0" step="1" value="${l.cantidad}" data-producto-id="${l.producto_id}" class="afp-input-recibido" ${afpPuedeBodega ? "" : "disabled"}>
      </td>
      <td class="num mono">${afpFmtMoney(l.precio_unitario)}</td>
    </tr>`).join("");

  // El backend siempre rechaza aprobar/rechazar si el rol no es
  // bodega/admin (ver @requiere_rol en app.py) — acá solo se evita
  // mostrar botones que de todas formas iban a fallar, dejando la
  // revisión visible en modo solo-lectura para cualquier otro rol
  // (transparencia: finanzas puede ver que su factura sigue pendiente,
  // aunque no pueda resolverla ella misma).
  document.getElementById("afp-btn-aprobar").style.display = afpPuedeBodega ? "" : "none";
  document.getElementById("afp-btn-rechazar").style.display = afpPuedeBodega ? "" : "none";
  const campoNotaRechazo = document.getElementById("afp-nota-rechazo").closest(".campo");
  campoNotaRechazo.style.display = afpPuedeBodega ? "" : "none";
  let avisoSoloLectura = document.getElementById("afp-aviso-solo-lectura");
  if (!avisoSoloLectura) {
    avisoSoloLectura = document.createElement("p");
    avisoSoloLectura.id = "afp-aviso-solo-lectura";
    avisoSoloLectura.className = "ayuda-inline ayuda-inline--bloqueado";
    campoNotaRechazo.after(avisoSoloLectura);
  }
  avisoSoloLectura.textContent = "Aprobar o rechazar la recepción es una acción del rol Bodega — esto queda solo como vista de revisión.";
  avisoSoloLectura.style.display = afpPuedeBodega ? "none" : "block";

  afpAbrirModal("afp-modal-detalle");
}

document.getElementById("afp-btn-flotante")?.addEventListener("click", afpAbrirLista);

document.getElementById("afp-btn-aprobar")?.addEventListener("click", async () => {
  const id = afpDetalleActivoId;
  if (!id) return;
  // Esta es la acción de mayor impacto del widget -- genera stock real y una
  // cuenta por pagar de inmediato, apenas se confirma -- así que pide
  // confirmación igual que "Rechazar factura" (ver más abajo), que ya la
  // tenía. Antes esta quedaba como la única sin confirmar pese a ser la más
  // riesgosa de las dos.
  if (!confirm("¿Aprobar esta recepción? Se va a generar stock y una cuenta por pagar de inmediato. Esto no se puede deshacer desde acá.")) return;
  const lineasRecibidas = [...document.querySelectorAll(".afp-input-recibido")].map(inp => ({
    producto_id: parseInt(inp.dataset.productoId, 10),
    cantidad_recibida: parseInt(inp.value || "0", 10),
  }));
  try {
    await afpApi(`/api/facturas/pendientes-recepcion/${id}/aprobar`, {
      method: "POST",
      body: JSON.stringify({ lineas_recibidas: lineasRecibidas }),
    });
    afpToast("Recepción aprobada — stock y cuentas por pagar generados");
    afpCerrarModal("afp-modal-detalle");
    await afpAbrirLista();
    await afpSondear();
  } catch (e) {
    afpToast(e.message, true);
  }
});

document.getElementById("afp-btn-rechazar")?.addEventListener("click", async () => {
  const id = afpDetalleActivoId;
  if (!id) return;
  const nota = document.getElementById("afp-nota-rechazo").value.trim();
  if (!nota) {
    afpToast("Escribe una nota explicando por qué se rechaza", true);
    return;
  }
  if (!confirm("¿Rechazar esta factura? No se aplicará ningún stock ni se generará una cuenta por pagar.")) return;
  try {
    await afpApi(`/api/facturas/pendientes-recepcion/${id}/rechazar`, {
      method: "POST",
      body: JSON.stringify({ nota }),
    });
    afpToast("Factura rechazada");
    afpCerrarModal("afp-modal-detalle");
    await afpAbrirLista();
    await afpSondear();
  } catch (e) {
    afpToast(e.message, true);
  }
});

if (document.getElementById("afp-flotante")) {
  afpCargarRol().then(afpSondear);
  setInterval(afpSondear, AFP_INTERVALO_SONDEO_MS);
}
