// ---------------------------------------------------------------------------
// Aviso global de facturas de compra pendientes de recepción — se incluye
// en TODAS las plantillas autenticadas (ver _alerta_facturas_pendientes.html)
// para que revisar estas facturas no dependa de acordarse de entrar a un
// módulo en particular. Sondea el contador cada cierto tiempo y, mientras
// haya al menos una pendiente, deja un botón flotante fijo en pantalla
// (con una animación que llama la atención) que abre el listado desde
// cualquier lugar de la app -- a propósito NUNCA se abre solo (antes sí lo
// hacía la primera vez que detectaba pendientes; a pedido del taller se
// sacó ese auto-apertura porque para Bodega, que vive con esto, era
// demasiado invasivo -- ahora basta con el botón rojo parpadeando, se abre
// solo con un clic).
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

function afpFmtFecha(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("es-CL", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

const AFP_EVENTO_HISTORIAL_LABEL = {
  creada: "Registrada",
  rechazada: "Rechazada por Bodega",
  corregida_reingresada: "Corregida y reingresada",
  aprobada: "Aprobada",
};

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

document.querySelectorAll('[data-close="afp-modal-lista"], [data-close="afp-modal-detalle"], [data-close="afr-modal-lista"]').forEach(btn => {
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
    } else {
      flotante.style.display = "none";
    }
  } catch (e) {
    // Silencioso: si falla el sondeo (ej. la sesión expiró, o esta
    // plantilla es la de login sin sesión) no queremos spamear toasts de
    // fondo en cada pantalla de la app.
  }
}

// ---------------------------------------------------------------------------
// Aviso para Finanzas: facturas rechazadas por Bodega que todavía no se
// corrigieron y reingresaron. Mismo patrón que afpSondear/afpAbrirLista de
// arriba, pero apuntando al endpoint de contador de rechazadas (solo
// finanzas/admin, ver contador_facturas_rechazadas en app.py) -- si el
// usuario actual no es finanzas, ese fetch simplemente devuelve 403 y el
// catch de abajo lo deja en silencio, igual que ya hace afpSondear con
// bodega. El botón "Corregir y reingresar" de cada fila NO abre un modal
// propio acá (la edición completa -- catálogo, autocompletar, crear
// producto -- vive solo en finanzas.js/finanzas.html) sino que navega
// directo a la pantalla de Facturas con esa factura ya abierta en modo
// detalle (?ver_pendiente=<id>, ver activarPestanaDesdeHash() en
// finanzas.js), desde donde el botón "Editar" ya habilitado hace el resto.
// ---------------------------------------------------------------------------

async function afrSondear() {
  const flotante = document.getElementById("afr-flotante");
  if (!flotante) return;
  try {
    const d = await afpApi("/api/facturas/pendientes-recepcion/rechazadas/contador");
    const texto = document.getElementById("afr-flotante-texto");
    if (d.rechazadas > 0) {
      flotante.style.display = "block";
      texto.textContent = d.rechazadas === 1 ? "1 factura rechazada" : `${d.rechazadas} facturas rechazadas`;
    } else {
      flotante.style.display = "none";
    }
  } catch (e) {
    // Silencioso a propósito -- ver comentario de afpSondear más arriba;
    // acá además es el caso normal para cualquiera que no sea finanzas.
  }
}

async function afrAbrirLista() {
  const tbody = document.getElementById("afr-tbody-lista");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="5" class="vacio">Cargando…</td></tr>`;
  afpAbrirModal("afr-modal-lista");
  try {
    const data = await afpApi("/api/facturas/pendientes-recepcion?estado=rechazada");
    if (data.pendientes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="vacio">No hay facturas rechazadas pendientes de corregir. 👍</td></tr>`;
      return;
    }
    tbody.innerHTML = data.pendientes.map(p => `
      <tr>
        <td>${afpEscapeHtml(p.numero) || "(sin folio)"}</td>
        <td>${afpEscapeHtml(p.proveedor_nombre) || "—"}</td>
        <td class="num mono">${afpFmtMoney(p.monto_total)}</td>
        <td>${afpEscapeHtml(p.nota_rechazo) || "—"}</td>
        <td><a class="btn btn-secundario" href="/finanzas?ver_pendiente=${p.id}#facturas">Corregir y reingresar</a></td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Error al cargar: ${e.message}</td></tr>`;
  }
}

document.getElementById("afr-btn-flotante")?.addEventListener("click", afrAbrirLista);

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

  // Si esta factura ya pasó antes por un rechazo (se corrigió y volvió a
  // 'pendiente'), se avisa acá para que quien la revise ahora sepa que ya
  // hubo una vuelta previa -- el historial completo queda más abajo.
  let avisoYaFueRechazada = document.getElementById("afp-aviso-ya-rechazada");
  const yaFueRechazada = (p.historial || []).some(h => h.evento === "rechazada");
  if (!avisoYaFueRechazada) {
    avisoYaFueRechazada = document.createElement("p");
    avisoYaFueRechazada.id = "afp-aviso-ya-rechazada";
    avisoYaFueRechazada.className = "ayuda-inline";
    document.getElementById("afp-detalle-ficha").after(avisoYaFueRechazada);
  }
  avisoYaFueRechazada.textContent = "Esta factura ya fue rechazada antes y fue corregida y reingresada -- revisa el historial más abajo.";
  avisoYaFueRechazada.style.display = yaFueRechazada ? "block" : "none";

  const tbody = document.getElementById("afp-tbody-lineas");
  tbody.innerHTML = p.lineas.map((l, idx) => {
    // Gasto general (combustible, herramienta, etc. -- ver es_gasto_general
    // en app.py): no es un producto sin resolver, es una línea que a
    // propósito NUNCA se liga a inventario. Se muestra aparte, sin el aviso
    // de "falta crear" ni el botón de Logística, y nunca bloquea "Aprobar".
    if (l.es_gasto_general) {
      return `
      <tr>
        <td>💸 ${afpEscapeHtml(l.descripcion)} <span class="badge-origen">${afpEscapeHtml(l.rubro_gav_label || l.rubro_gav || "")}</span></td>
        <td class="num mono">—</td>
        <td class="num mono">—</td>
        <td class="num mono">${afpFmtMoney(l.monto)}</td>
      </tr>`;
    }
    const sinResolver = l.producto_id == null;
    // "Leído: ..." se muestra SIEMPRE que haya texto_original (viene de una
    // foto leída por IA), matcheó producto o no -- antes solo se mostraba
    // cuando no matcheaba nada, así que un match automático equivocado (ej.
    // el código se perdió al leer la foto y matcheó por texto contra el
    // producto incorrecto) pasaba desapercibido en esta revisión.
    const leido = l.texto_original
      ? `<div class="pfd-linea-leido">Leído: "${afpEscapeHtml(l.texto_original)}"${l.codigo_leido ? ` · código: "${afpEscapeHtml(l.codigo_leido)}"` : ""}</div>`
      : "";
    let nombreCelda;
    if (sinResolver) {
      // Bodega resuelve el producto sin salir de este mismo modal de
      // aprobación -- antes esto solo avisaba y mandaba a Finanzas a editar
      // la factura, un rodeo innecesario ya que quien tiene permiso real
      // para crear productos es Bodega, no Finanzas.
      nombreCelda = `
        <div class="afp-linea-sin-resolver">
          <div>⚠️ Producto nuevo, falta crear</div>
          ${leido}
          ${afpPuedeBodega ? `
            <button type="button" class="btn btn-secundario btn-chico afp-btn-crear-producto" data-indice="${idx}" style="margin-top:6px">
              Crear este producto en Logística
            </button>
            <div class="afp-form-crear-producto" data-indice="${idx}" style="display:none;margin-top:6px">
              <input type="text" class="afp-crear-codigo-interno" placeholder="Código interno / N° de parte (ej. JF011)" value="${l.codigo_leido ? afpEscapeHtml(l.codigo_leido) : ""}" style="margin-bottom:4px">
              <input type="text" class="afp-crear-categoria" placeholder="Modelo" style="margin-bottom:4px">
              <input type="text" class="afp-crear-modelo" placeholder="Detalle (ej. ABC 2020)" style="margin-bottom:4px" value="${l.texto_original ? afpEscapeHtml(l.texto_original) : ""}">
              <button type="button" class="btn btn-primario btn-chico afp-btn-crear-confirmar" data-indice="${idx}">Crear y vincular</button>
            </div>
          ` : ""}
        </div>`;
    } else {
      nombreCelda = `${afpEscapeHtml(l.producto_nombre) || `Producto #${l.producto_id}`}${leido}`;
    }
    return `
    <tr>
      <td>${nombreCelda}</td>
      <td class="num mono">${l.cantidad}</td>
      <td class="num mono">${afpFmtMoney(l.precio_unitario)}</td>
      <td class="num mono">${afpFmtMoney(l.cantidad * l.precio_unitario)}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll(".afp-btn-crear-producto").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = btn.dataset.indice;
      const form = tbody.querySelector(`.afp-form-crear-producto[data-indice="${idx}"]`);
      if (form) form.style.display = form.style.display === "none" ? "block" : "none";
    });
  });
  tbody.querySelectorAll(".afp-btn-crear-confirmar").forEach(btn => {
    btn.addEventListener("click", async () => {
      const idx = parseInt(btn.dataset.indice, 10);
      const form = btn.closest(".afp-form-crear-producto");
      const codigoInterno = form.querySelector(".afp-crear-codigo-interno").value.trim();
      const categoria = form.querySelector(".afp-crear-categoria").value.trim();
      const modelo = form.querySelector(".afp-crear-modelo").value.trim();
      if (!categoria && !modelo) {
        afpToast("Ingresa al menos modelo o detalle para crear el producto", true);
        return;
      }
      try {
        const data = await afpApi(`/api/facturas/pendientes-recepcion/${id}/lineas/${idx}/crear-producto`, {
          method: "POST",
          body: JSON.stringify({ codigo_interno: codigoInterno, categoria, modelo }),
        });
        afpToast(`Producto "${data.producto.nombre}" creado y vinculado a esta línea`);
        // El backend devuelve el pendiente ya actualizado con la línea
        // resuelta -- se reemplaza en caché y se vuelve a pintar el
        // detalle, así "Aprobar" se habilita de inmediato si esa era la
        // última línea que faltaba, sin cerrar y reabrir el modal.
        const i = afpPendientesCache.findIndex(x => x.id === id);
        if (i !== -1) afpPendientesCache[i] = data.pendiente;
        afpAbrirDetalle(id);
      } catch (err) {
        afpToast(err.message, true);
      }
    });
  });

  // El backend siempre rechaza aprobar/rechazar si el rol no es
  // bodega/admin (ver @requiere_rol en app.py) — acá solo se evita
  // mostrar botones que de todas formas iban a fallar, dejando la
  // revisión visible en modo solo-lectura para cualquier otro rol
  // (transparencia: finanzas puede ver que su factura sigue pendiente,
  // aunque no pueda resolverla ella misma).
  // Un gasto general también tiene producto_id == null a propósito (nunca
  // se liga a inventario, ver arriba) -- no cuenta como "sin resolver", o
  // Bodega jamás podría aprobar una factura que traiga uno.
  const hayLineasSinResolver = p.lineas.some(l => l.producto_id == null && !l.es_gasto_general);
  document.getElementById("afp-btn-aprobar").style.display = (afpPuedeBodega && !hayLineasSinResolver) ? "" : "none";
  document.getElementById("afp-btn-rechazar").style.display = afpPuedeBodega ? "" : "none";
  let avisoProductoNuevo = document.getElementById("afp-aviso-producto-nuevo");
  if (!avisoProductoNuevo) {
    avisoProductoNuevo = document.createElement("p");
    avisoProductoNuevo.id = "afp-aviso-producto-nuevo";
    avisoProductoNuevo.className = "ayuda-inline ayuda-inline--bloqueado";
    document.getElementById("afp-tbody-lineas").closest("table").after(avisoProductoNuevo);
  }
  avisoProductoNuevo.textContent = afpPuedeBodega
    ? "Esta factura tiene productos que todavía no existen en el catálogo — créalos arriba, en cada línea marcada, antes de poder aprobarla."
    : "Esta factura tiene productos que todavía no existen en el catálogo — Bodega debe crearlos antes de poder aprobarla.";
  avisoProductoNuevo.style.display = hayLineasSinResolver ? "block" : "none";

  let historialLista = document.getElementById("afp-historial");
  if (!historialLista) {
    historialLista = document.createElement("ul");
    historialLista.id = "afp-historial";
    historialLista.className = "afp-historial";
    avisoProductoNuevo.after(historialLista);
  }
  if (yaFueRechazada && (p.historial || []).length) {
    historialLista.innerHTML = p.historial.map(h =>
      `<li>${afpFmtFecha(h.fecha)} — ${AFP_EVENTO_HISTORIAL_LABEL[h.evento] || h.evento} (${afpEscapeHtml(h.usuario) || "—"})${h.nota ? `: "${afpEscapeHtml(h.nota)}"` : ""}</li>`
    ).join("");
    historialLista.style.display = "block";
  } else {
    historialLista.style.display = "none";
  }

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
  if (!confirm("¿Aprobar esta recepción? Se va a generar stock y una cuenta por pagar de inmediato, exactamente como quedó facturada. Esto no se puede deshacer desde acá.")) return;
  try {
    await afpApi(`/api/facturas/pendientes-recepcion/${id}/aprobar`, {
      method: "POST",
      body: JSON.stringify({}),
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

if (document.getElementById("afr-flotante")) {
  afrSondear();
  setInterval(afrSondear, AFP_INTERVALO_SONDEO_MS);
}
