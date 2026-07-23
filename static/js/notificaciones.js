// ---------------------------------------------------------------------------
// Campanita de notificaciones global — se incluye en TODAS las plantillas
// autenticadas (ver _notificaciones.html), mismo criterio que
// alerta_facturas_pendientes.js: no depende de qué módulo esté cargado
// (inicio.html, el selector de módulos, no carga ningún *.js de módulo), así
// que trae sus propias copias de utilidades con prefijo "notif".
//
// Cubre tres categorías, todas dejadas por mecánicos restringidos vía
// WhatsApp (ver migración 16/21/30 / agente_mecanicos.tipo en app.py):
// sugerencias de diagnóstico, pruebas de ruta y Pre entrega -- pensada para
// sumar más categorías más adelante sin rehacer el mecanismo (ver
// /api/notificaciones/resumen, que ya devuelve un dict con una clave por
// categoría).
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

async function notifCargarListaSugerencias() {
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
          <a href="/operativo?abrir_ot=${s.ot_id}&tab=diagnostico#ordenes">OT #${s.ot_id}${s.cliente_nombre ? " — " + notifEscapeHtml(s.cliente_nombre) : ""}${s.patente ? " · " + notifEscapeHtml(s.patente) : ""}</a>
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

async function notifCargarListaPruebasRuta() {
  const lista = document.getElementById("notif-bell-lista-pruebas-ruta");
  if (!lista) return;
  try {
    const pruebas = await notifApi("/api/notificaciones/pruebas-ruta");
    if (!pruebas.length) {
      lista.innerHTML = `<p class="ayuda-inline">Sin novedades.</p>`;
      return;
    }
    lista.innerHTML = pruebas.map(p => `
      <div class="notif-item" data-id="${p.id}">
        <p class="notif-item-ot">
          <a href="/operativo?abrir_ot=${p.orden_id}&tab=qc#ordenes">OT #${p.orden_id}${p.cliente_nombre ? " — " + notifEscapeHtml(p.cliente_nombre) : ""}${p.patente ? " · " + notifEscapeHtml(p.patente) : ""}</a>
        </p>
        <p class="notif-item-texto">${notifEscapeHtml(p.texto)}${p.foto_id ? " 📷" : ""}</p>
        <p class="notif-item-meta">Por: ${notifEscapeHtml(p.autor)} — ${notifFmtFecha(p.creado_en)}</p>
        <button type="button" class="btn btn-secundario btn-chico" data-notif-atender-prueba="${p.id}">Marcar como revisada</button>
      </div>
    `).join("");
    lista.querySelectorAll("[data-notif-atender-prueba]").forEach(btn => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await notifApi(`/api/operativo/pruebas-ruta/${btn.dataset.notifAtenderPrueba}/atender`, { method: "POST" });
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

async function notifCargarListaPreEntrega() {
  const lista = document.getElementById("notif-bell-lista-pre-entrega");
  if (!lista) return;
  try {
    const entradas = await notifApi("/api/notificaciones/pre-entrega");
    if (!entradas.length) {
      lista.innerHTML = `<p class="ayuda-inline">Sin novedades.</p>`;
      return;
    }
    lista.innerHTML = entradas.map(p => `
      <div class="notif-item" data-id="${p.id}">
        <p class="notif-item-ot">
          <a href="/operativo?abrir_ot=${p.orden_id}&tab=pre_entrega#ordenes">OT #${p.orden_id}${p.cliente_nombre ? " — " + notifEscapeHtml(p.cliente_nombre) : ""}${p.patente ? " · " + notifEscapeHtml(p.patente) : ""}</a>
        </p>
        <p class="notif-item-texto">${notifEscapeHtml(p.texto)}${p.foto_id ? " 📷" : ""}</p>
        <p class="notif-item-meta">Por: ${notifEscapeHtml(p.autor)} — ${notifFmtFecha(p.creado_en)}</p>
        <button type="button" class="btn btn-secundario btn-chico" data-notif-atender-pre-entrega="${p.id}">Marcar como revisada</button>
      </div>
    `).join("");
    lista.querySelectorAll("[data-notif-atender-pre-entrega]").forEach(btn => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await notifApi(`/api/operativo/pre-entrega/${btn.dataset.notifAtenderPreEntrega}/atender`, { method: "POST" });
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

async function notifCargarLista() {
  await Promise.all([notifCargarListaSugerencias(), notifCargarListaPruebasRuta(), notifCargarListaPreEntrega()]);
}

async function notifAbrirPanel() {
  const panel = document.getElementById("notif-bell-panel");
  if (!panel) return;
  notifPanelAbierto = true;
  panel.style.display = "block";
  // El panel siempre se despliega pegado a la campanita (arriba a la
  // derecha), aunque se haya abierto desde el botón flotante de abajo --
  // se deja la campanita a la vista para que no quede raro "aparecer"
  // desconectado de donde se hizo clic.
  document.getElementById("notif-bell")?.scrollIntoView({ behavior: "smooth", block: "center" });
  await notifCargarLista();
}

async function notifSondear() {
  const wrap = document.getElementById("notif-bell");
  const flotante = document.getElementById("notif-flotante");
  if (!wrap) return;
  if (!notifPuedeVer()) {
    wrap.style.display = "none";
    if (flotante) flotante.style.display = "none";
    return;
  }
  try {
    const resumen = await notifApi("/api/notificaciones/resumen");
    const sugerencias = resumen.sugerencias_diagnostico || 0;
    const pruebasRuta = resumen.pruebas_ruta || 0;
    const preEntrega = resumen.pre_entrega || 0;
    const total = sugerencias + pruebasRuta + preEntrega;
    wrap.style.display = "";
    const badge = document.getElementById("notif-bell-badge");
    if (total > 0) {
      badge.textContent = total > 99 ? "99+" : String(total);
      badge.style.display = "";
    } else {
      badge.style.display = "none";
    }
    // Botón flotante abajo a la derecha (mismo estilo que el de facturas
    // pendientes de Bodega) -- a diferencia de ese, este NUNCA se abre
    // solo: solo queda ahí parpadeando hasta que la persona hace clic,
    // para no interrumpir a mitad de otra cosa cada vez que llega un
    // mensaje nuevo por WhatsApp.
    if (flotante) {
      if (total > 0) {
        flotante.style.display = "block";
        document.getElementById("notif-flotante-texto").textContent =
          total === 1 ? "1 novedad por revisar" : `${total} novedades por revisar`;
      } else {
        flotante.style.display = "none";
      }
    }
    if (notifPanelAbierto) await notifCargarLista();
  } catch (e) {
    // Sondeo de fondo — silencioso.
  }
}

document.getElementById("notif-bell-btn")?.addEventListener("click", async (e) => {
  e.stopPropagation();
  const panel = document.getElementById("notif-bell-panel");
  if (notifPanelAbierto) {
    notifPanelAbierto = false;
    panel.style.display = "none";
  } else {
    await notifAbrirPanel();
  }
});

document.getElementById("notif-btn-flotante")?.addEventListener("click", async (e) => {
  e.stopPropagation();
  const panel = document.getElementById("notif-bell-panel");
  if (notifPanelAbierto) {
    notifPanelAbierto = false;
    panel.style.display = "none";
  } else {
    await notifAbrirPanel();
  }
});

document.addEventListener("click", (e) => {
  const bell = document.getElementById("notif-bell");
  const flotante = document.getElementById("notif-flotante");
  const dentroFlotante = flotante && flotante.contains(e.target);
  if (bell && notifPanelAbierto && !bell.contains(e.target) && !dentroFlotante) {
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
