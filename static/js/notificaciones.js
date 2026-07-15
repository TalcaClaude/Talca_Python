// ---------------------------------------------------------------------------
// Campanita de notificaciones global — se incluye en TODAS las plantillas
// autenticadas (ver _notificaciones.html), mismo criterio que
// alerta_facturas_pendientes.js: no depende de qué módulo esté cargado
// (inicio.html, el selector de módulos, no carga ningún *.js de módulo), así
// que trae sus propias copias de utilidades con prefijo "notif".
//
// Hoy solo cubre sugerencias de diagnóstico de mecánicos restringidos (ver
// migración 16 / agente_mecanicos.tipo en app.py) -- pensada para sumar más
// categorías más adelante sin rehacer el mecanismo (ver /api/notificaciones/
// resumen, que ya devuelve un dict con una clave por categoría).
// ---------------------------------------------------------------------------

const NOTIF_INTERVALO_SONDEO_MS = 25000;
let notifRolActual = null;
let notifModulosActuales = [];
let notifPanelAbierto = false;

function notifEscapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function notifFmtFecha(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso.replace(" ", "T"));
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString("es-CL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return iso;
  }
}

async function notifApi(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Error inesperado");
  return data;
}

function notifPuedeVer() {
  return notifRolActual === "admin" || notifModulosActuales.includes("operativo");
}

async function notifCargarRol() {
  try {
    const s = await notifApi("/api/sesion");
    notifRolActual = s.rol;
    notifModulosActuales = s.modulos || [];
  } catch (e) {
    // Sin sesión válida (ej. venimos de /login) — no hay nada que sondear.
    notifRolActual = null;
    notifModulosActuales = [];
  }
}

async function notifCargarLista() {
  const lista = document.getElementById("notif-bell-lista");
  if (!lista) return;
  try {
    const sugerencias = await notifApi("/api/notificaciones/sugerencias-diagnostico");
    if (!sugerencias.length) {
      lista.innerHTML = `<p class="ayuda-inline">Sin novedades.</p>`;
      return;
    }
    lista.innerHTML = sugerencias.map(s => `
      <div class="notif-item" data-id="${s.id}">
        <p class="notif-item-ot">
          <a href="/operativo?abrir_ot=${s.ot_id}#ordenes">OT #${s.ot_id}${s.cliente_nombre ? " — " + notifEscapeHtml(s.cliente_nombre) : ""}${s.patente ? " · " + notifEscapeHtml(s.patente) : ""}</a>
        </p>
        <p class="notif-item-texto">${notifEscapeHtml(s.texto)}</p>
        <p class="notif-item-meta">Por: ${notifEscapeHtml(s.mecanico_nombre)} — ${notifFmtFecha(s.creado_en)}</p>
        <!-- "Descartar sugerencia" (no "Marcar como revisada") a propósito --
             es el mismo texto que usa el modal de la OT para esta misma
             acción (operativo.js, "Descartar sin agregar"). El texto viejo
             no dejaba claro que la sugerencia de la IA se pierde, no que
             solo se "marca" como vista. -->
        <button type="button" class="btn btn-secundario btn-chico" data-notif-atender="${s.id}">Descartar sugerencia</button>
      </div>
    `).join("");
    lista.querySelectorAll("[data-notif-atender]").forEach(btn => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await notifApi(`/api/sugerencias-diagnostico/${btn.dataset.notifAtender}/atender`, { method: "POST" });
          await notifSondear();
        } catch (e) {
          btn.disabled = false;
        }
      });
    });
  } catch (e) {
    lista.innerHTML = `<p class="ayuda-inline">No se pudo cargar la lista.</p>`;
  }
}

async function notifSondear() {
  const wrap = document.getElementById("notif-bell");
  if (!wrap) return;
  if (!notifPuedeVer()) {
    wrap.style.display = "none";
    return;
  }
  try {
    const resumen = await notifApi("/api/notificaciones/resumen");
    const total = resumen.sugerencias_diagnostico || 0;
    wrap.style.display = "";
    const badge = document.getElementById("notif-bell-badge");
    if (total > 0) {
      badge.textContent = total > 99 ? "99+" : String(total);
      badge.style.display = "";
    } else {
      badge.style.display = "none";
    }
    if (notifPanelAbierto) await notifCargarLista();
  } catch (e) {
    // Sondeo de fondo — silencioso.
  }
}

document.getElementById("notif-bell-btn")?.addEventListener("click", async (e) => {
  e.stopPropagation();
  const panel = document.getElementById("notif-bell-panel");
  notifPanelAbierto = !notifPanelAbierto;
  panel.style.display = notifPanelAbierto ? "block" : "none";
  if (notifPanelAbierto) await notifCargarLista();
});

document.addEventListener("click", (e) => {
  const bell = document.getElementById("notif-bell");
  if (bell && notifPanelAbierto && !bell.contains(e.target)) {
    notifPanelAbierto = false;
    const panel = document.getElementById("notif-bell-panel");
    if (panel) panel.style.display = "none";
  }
});

(async function notifInit() {
  await notifCargarRol();
  await notifSondear();
  setInterval(notifSondear, NOTIF_INTERVALO_SONDEO_MS);
})();
