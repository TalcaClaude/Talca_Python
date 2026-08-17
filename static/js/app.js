const state = {
  rol: null,
  puedeFinanzas: true,
  puedeBodega: true,
  esAdmin: true,
  productos: [],
  categorias: [],
  productoActivo: null,
  ubicaciones: [],
  paginaProductos: 1,
  porPaginaProductos: 50,
  totalProductos: 0,
  totalPaginasProductos: 1,
  origenModalUbicacion: "producto", // "producto" | "bodega"
  categoriasDisponibles: [],
  modelosDisponibles: [],
  marcasDisponibles: [],
  facturaEncontrada: null, // resultado del último lookup por número en el form de movimiento
  proveedores: [],
  proveedorActivoId: null,
  sesion: { username: null, nombre: null },
  usuarios: [],
};

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

function fmtMoney(n) {
  // Antes esto mostraba literalmente "$NaN" si 'n' venía null/undefined
  // (ej. un campo de precio todavía sin cargar) -- las otras dos copias de
  // este mismo formateador (pdFmtMoney en panel_dueno.js, la de charts.js)
  // ya devolvían "—" en ese caso; esta quedó desalineada.
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return "$" + Math.round(n).toLocaleString("es-CL");
}

// ---------------------------------------------------------------------------
// Búsqueda "de varias palabras, sin tildes" (mismo criterio que
// coincide_busqueda en core.py, para los buscadores que filtran en el
// navegador en vez de pedirle al servidor): minúsculas + sin
// tildes/diacríticos, y cada palabra escrita tiene que aparecer en alguno
// de los campos (concatenados) — así "6f35 bomba" encuentra algo cuyo
// código es "6F35" Y cuya descripción menciona "bomba", aunque sean campos
// distintos, y mientras más palabras se agregan más se acota el resultado.
// ---------------------------------------------------------------------------

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

function fmtFecha(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("es-CL", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtFechaSolo(isoFecha) {
  // Para fechas simples "YYYY-MM-DD" (sin hora), evita el desfase de un día
  // que puede dar new Date() al interpretarlas como UTC medianoche.
  if (!isoFecha) return "—";
  const [y, m, d] = isoFecha.split("-");
  return `${d}-${m}-${y}`;
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
  // Todos los modal-overlay comparten el mismo z-index en el CSS, así que
  // si se abre un modal desde ADENTRO de otro (ej. "+ Nuevo cliente" desde
  // el formulario de "Nueva orden"), cuál queda encima depende del orden en
  // el HTML, no de cuál se abrió último — con dos modales del mismo tipo
  // eso puede tapar al que se acaba de abrir y bloquear los clics. Se
  // resuelve subiendo el z-index del que se abre por sobre cualquier otro
  // modal que ya esté abierto en ese momento.
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

// Los modales ya NO se cierran al hacer click afuera (para no perder datos
// escritos por accidente). Solo se cierran con la X, "Cancelar", o ESC.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".modal-overlay.abierto").forEach(overlay => {
    overlay.classList.remove("abierto");
  });
});

// ---------------------------------------------------------------------------
// Pestañas: Productos / Bodega
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab").forEach(tabBtn => {
  tabBtn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("activo"));
    tabBtn.classList.add("activo");
    const vista = tabBtn.dataset.vista;
    document.getElementById("vista-dashboard").style.display = vista === "dashboard" ? "block" : "none";
    document.getElementById("vista-productos").style.display = vista === "productos" ? "block" : "none";
    document.getElementById("vista-bodega").style.display = vista === "bodega" ? "block" : "none";
    document.getElementById("vista-proveedores").style.display = vista === "proveedores" ? "block" : "none";
    document.getElementById("vista-movimientos").style.display = vista === "movimientos" ? "block" : "none";
    document.getElementById("vista-mermas").style.display = vista === "mermas" ? "block" : "none";
    document.getElementById("vista-rotacion").style.display = vista === "rotacion" ? "block" : "none";
    document.getElementById("vista-planificacion").style.display = vista === "planificacion" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    document.getElementById("vista-usuarios").style.display = vista === "usuarios" ? "block" : "none";
    document.getElementById("btn-nuevo").style.display = vista === "productos" ? "inline-block" : "none";
    document.getElementById("btn-importar-excel").style.display =
      vista === "productos" && state.puedeFinanzas ? "inline-block" : "none";
    const titulos = {
      dashboard: "Dashboard",
      productos: "Administración de Inventario",
      bodega: "Organización Física de Bodega",
      proveedores: "Proveedores",
      movimientos: "Control de Movimientos",
      mermas: "Mermas",
      rotacion: "Control de Rotación",
      planificacion: "Planificación de Compras",
      reportes: "Reportes y Exportaciones",
      usuarios: "Usuarios",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "dashboard") cargarDashboard();
    if (vista === "bodega") cargarUbicaciones();
    if (vista === "proveedores") cargarProveedores();
    if (vista === "movimientos") cargarMovimientos();
    if (vista === "mermas") cargarMermas();
    if (vista === "rotacion") cargarRotacion();
    if (vista === "planificacion") { cargarPlanificacion(); cargarRepuestosTipicosModelos(); }
    if (vista === "reportes") cargarResumenReportes();
    if (vista === "usuarios") cargarUsuarios();
  });
});

// El sidebar enlaza a /logistica#vista (ver _sidebar.html). Como los tabs
// se manejan 100% en JS (no hay rutas reales por pestaña), hay que leer el
// hash al cargar la página y también reaccionar si cambia estando ya en
// esta vista (un click en el sidebar a una pestaña de la MISMA página no
// dispara una navegación completa, solo cambia el hash).
function activarPestanaDesdeHash() {
  const vista = location.hash.replace("#", "");
  if (!vista) return;
  const tabBtn = document.querySelector(`.tab[data-vista="${vista}"]`);
  if (tabBtn) tabBtn.click();
}
window.addEventListener("hashchange", activarPestanaDesdeHash);
activarPestanaDesdeHash();

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

async function eliminarConReintento(url, mensajeConfirmacionInicial, bodyForzado) {
  if (!confirm(mensajeConfirmacionInicial)) return false;
  try {
    await api(url, { method: "DELETE" });
    return true;
  } catch (e) {
    if (e.requiereForzar && confirm(`${e.message}\n\n¿Eliminar de todas formas? Esta acción no se puede deshacer.`)) {
      const urlForzada = url + (url.includes("?") ? "&" : "?") + "forzar=1";
      const opciones = { method: "DELETE" };
      if (bodyForzado) opciones.body = JSON.stringify(bodyForzado);
      const resultado = await api(urlForzada, opciones);
      return resultado;
    }
    if (!e.requiereForzar) throw e;
    return false;
  }
}

async function cargarUbicaciones() {
  try {
    const data = await api("/api/ubicaciones");
    state.ubicaciones = data.ubicaciones;
    renderTablaUbicaciones();
    return data.ubicaciones;
  } catch (e) {
    toast(e.message, true);
    return [];
  }
}

let buscarBodegaTimeout;
const inputBuscarBodega = document.getElementById("buscar-bodega");
if (inputBuscarBodega) {
  inputBuscarBodega.addEventListener("input", () => {
    clearTimeout(buscarBodegaTimeout);
    buscarBodegaTimeout = setTimeout(renderTablaUbicaciones, 200);
  });
}

function renderTablaUbicaciones() {
  const tbody = document.getElementById("tbody-ubicaciones");
  if (state.ubicaciones.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Aún no hay ubicaciones creadas. Usa "+ Nueva ubicación" para empezar a mapear la bodega.</td></tr>`;
    return;
  }

  const buscarInput = document.getElementById("buscar-bodega");
  const termino = buscarInput ? buscarInput.value.trim() : "";

  const ubicacionesFiltradas = !termino ? state.ubicaciones : state.ubicaciones.filter(u => {
    const campos = [u.codigo, u.pasillo, u.estanteria, u.nivel];
    u.productos.forEach(p => {
      campos.push(p.nombre, p.codigo_interno, p.categoria, p.modelo);
    });
    return coincideBusqueda(termino, ...campos);
  });

  if (ubicacionesFiltradas.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Ninguna ubicación coincide con "${escapeHtml(buscarInput.value.trim())}".</td></tr>`;
    return;
  }

  tbody.innerHTML = ubicacionesFiltradas.map(u => {
    const productosTexto = u.productos.length
      ? u.productos.map(p => `<span class="codigo-pill">${p.codigo_interno ? escapeHtml(p.codigo_interno) : "#" + String(p.id).padStart(4, "0")}</span> ${escapeHtml(p.nombre)}`).join("<br>")
      : "—";
    return `
    <tr>
      <td class="mono">${escapeHtml(u.codigo)}</td>
      <td class="mono">${u.pasillo}</td>
      <td class="mono">${u.estanteria}</td>
      <td class="mono">${u.nivel}</td>
      <td><span class="estado-pill estado-pill--${u.ocupada ? "ocupada" : "libre"}">${u.ocupada ? `${u.productos.length} producto(s)` : "Libre"}</span></td>
      <td>${productosTexto}</td>
      <td>
        ${u.ocupada ? `<button class="btn-link" data-reasignar-ubicacion="${u.id}">Reasignar</button> ` : ""}
        <button class="btn-link" data-borrar-ubicacion="${u.id}">Eliminar</button>
      </td>
    </tr>
  `;
  }).join("");

  tbody.querySelectorAll("[data-borrar-ubicacion]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.borrarUbicacion;
      try {
        const eliminado = await eliminarConReintento(
          `/api/ubicaciones/${id}`,
          "¿Eliminar esta ubicación?"
        );
        if (!eliminado) return;
        toast("Ubicación eliminada");
        await cargarUbicaciones();
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  tbody.querySelectorAll("[data-reasignar-ubicacion]").forEach(btn => {
    btn.addEventListener("click", () => abrirReasignarUbicacion(parseInt(btn.dataset.reasignarUbicacion, 10)));
  });
}

// Mueve de una sola vez TODOS los productos que hay en una ubicación hacia
// otra — para reorganizar la bodega sin tener que editar producto por
// producto (ej. "voy a dejar libre el pasillo 3 completo, todo lo que
// tenga se va al pasillo 8").
function abrirReasignarUbicacion(origenId) {
  const origen = state.ubicaciones.find(u => u.id === origenId);
  if (!origen || origen.productos.length === 0) return;
  document.getElementById("reasig-origen-id").value = origenId;
  document.getElementById("reasig-origen-info").textContent =
    `Se moverán los ${origen.productos.length} producto(s) que hay en ${origen.codigo} a la ubicación que elijas. ${origen.codigo} quedará libre.`;
  const select = document.getElementById("reasig-destino-id");
  const otras = state.ubicaciones.filter(u => u.id !== origenId);
  select.innerHTML = otras.length
    ? otras.map(u => `<option value="${u.id}">${escapeHtml(u.codigo)}${u.ocupada ? ` (ya tiene ${u.productos.length} producto/s)` : ""}</option>`).join("")
    : '<option value="">No hay otra ubicación disponible — crea una primero</option>';
  abrirModal("modal-reasignar-ubicacion");
}

document.getElementById("form-reasignar-ubicacion").addEventListener("submit", async (e) => {
  e.preventDefault();
  const origenId = document.getElementById("reasig-origen-id").value;
  const destinoId = document.getElementById("reasig-destino-id").value;
  if (!destinoId) {
    toast("Selecciona la ubicación de destino", true);
    return;
  }
  try {
    const resultado = await api(`/api/ubicaciones/${origenId}/reasignar`, {
      method: "PUT",
      body: JSON.stringify({ destino_id: destinoId }),
    });
    toast(`${resultado.movidos} producto(s) reasignado(s)`);
    cerrarModal("modal-reasignar-ubicacion");
    await cargarUbicaciones();
    if (state.productos.length) await cargarProductos();
  } catch (e2) {
    toast(e2.message, true);
  }
});

function renderSelectUbicaciones(seleccionActualId) {
  const select = document.getElementById("f-ubicacion-id");
  select.innerHTML = '<option value="">Sin asignar</option>' +
    state.ubicaciones.map(u => `<option value="${u.id}">${escapeHtml(u.codigo)}${u.ocupada ? ` (${u.productos.length} producto/s)` : ""}</option>`).join("");
  select.value = seleccionActualId || "";
}

function resetearModalUbicacion() {
  document.getElementById("form-ubicacion").reset();
  document.getElementById("u-modo-rango").checked = false;
  document.getElementById("u-campos-simple").style.display = "grid";
  document.getElementById("u-campos-rango").style.display = "none";
  document.getElementById("u-preview").textContent = "Código resultante: —";
  document.getElementById("modal-ubicacion-titulo").textContent = "Nueva ubicación";
  document.getElementById("u-btn-crear").textContent = "Crear ubicación";
}

document.getElementById("btn-nueva-ubicacion").addEventListener("click", () => {
  state.origenModalUbicacion = "bodega";
  resetearModalUbicacion();
  // El modo rango (crear varias ubicaciones de una sola vez) solo tiene
  // sentido viniendo de la pestaña Bodega — desde el formulario de
  // producto se necesita UNA ubicación concreta para asignarla ahí mismo.
  document.getElementById("u-toggle-rango-wrap").style.display = "flex";
  abrirModal("modal-ubicacion");
});

document.getElementById("btn-crear-ubicacion-inline").addEventListener("click", () => {
  state.origenModalUbicacion = "producto";
  resetearModalUbicacion();
  document.getElementById("u-toggle-rango-wrap").style.display = "none";
  abrirModal("modal-ubicacion");
});

function actualizarPreviewUbicacionSimple() {
  const p = document.getElementById("u-pasillo").value;
  const e = document.getElementById("u-estanteria").value;
  const n = document.getElementById("u-nivel").value;
  document.getElementById("u-preview").textContent =
    (p && e && n) ? `Código resultante: P${p}-E${e}-N${n}` : "Código resultante: —";
}

function actualizarPreviewUbicacionRango() {
  const pD = parseInt(document.getElementById("u-pasillo-desde").value, 10);
  const pH = parseInt(document.getElementById("u-pasillo-hasta").value, 10);
  const eD = parseInt(document.getElementById("u-estanteria-desde").value, 10);
  const eH = parseInt(document.getElementById("u-estanteria-hasta").value, 10);
  const nD = parseInt(document.getElementById("u-nivel-desde").value, 10);
  const nH = parseInt(document.getElementById("u-nivel-hasta").value, 10);
  const preview = document.getElementById("u-preview");
  if (![pD, pH, eD, eH, nD, nH].every(v => Number.isInteger(v) && v > 0)) {
    preview.textContent = "Código resultante: —";
    return;
  }
  if (pH < pD || eH < eD || nH < nD) {
    preview.textContent = "El valor \"hasta\" de cada rango debe ser mayor o igual al \"desde\".";
    return;
  }
  const total = (pH - pD + 1) * (eH - eD + 1) * (nH - nD + 1);
  preview.textContent = `Se crearán hasta ${total} ubicaciones, de P${pD}-E${eD}-N${nD} a P${pH}-E${eH}-N${nH} (las que ya existan se dejan tal cual, sin duplicar).`;
}

["u-pasillo", "u-estanteria", "u-nivel"].forEach(id => {
  document.getElementById(id).addEventListener("input", actualizarPreviewUbicacionSimple);
});
["u-pasillo-desde", "u-pasillo-hasta", "u-estanteria-desde", "u-estanteria-hasta", "u-nivel-desde", "u-nivel-hasta"].forEach(id => {
  document.getElementById(id).addEventListener("input", actualizarPreviewUbicacionRango);
});

document.getElementById("u-modo-rango").addEventListener("change", (e) => {
  const esRango = e.target.checked;
  document.getElementById("u-campos-simple").style.display = esRango ? "none" : "grid";
  document.getElementById("u-campos-rango").style.display = esRango ? "grid" : "none";
  document.getElementById("u-btn-crear").textContent = esRango ? "Crear ubicaciones" : "Crear ubicación";
  document.getElementById("u-preview").textContent = "Código resultante: —";
});

document.getElementById("form-ubicacion").addEventListener("submit", async (e) => {
  e.preventDefault();
  const esRango = document.getElementById("u-modo-rango").checked;

  if (esRango) {
    const payload = {
      pasillo_desde: document.getElementById("u-pasillo-desde").value,
      pasillo_hasta: document.getElementById("u-pasillo-hasta").value,
      estanteria_desde: document.getElementById("u-estanteria-desde").value,
      estanteria_hasta: document.getElementById("u-estanteria-hasta").value,
      nivel_desde: document.getElementById("u-nivel-desde").value,
      nivel_hasta: document.getElementById("u-nivel-hasta").value,
    };
    try {
      const resultado = await api("/api/ubicaciones/rango", { method: "POST", body: JSON.stringify(payload) });
      if (resultado.creadas > 0) {
        toast(`${resultado.creadas} ubicación(es) creada(s)` + (resultado.omitidas > 0 ? ` (${resultado.omitidas} ya existían y se dejaron igual)` : ""));
      } else {
        toast("Todas las ubicaciones de ese rango ya existían — no se creó ninguna nueva.", true);
      }
      cerrarModal("modal-ubicacion");
      await cargarUbicaciones();
    } catch (e2) {
      toast(e2.message, true);
    }
    return;
  }

  const p = document.getElementById("u-pasillo").value;
  const est = document.getElementById("u-estanteria").value;
  const n = document.getElementById("u-nivel").value;
  if (!p || !est || !n) {
    toast("Completa pasillo, estantería y nivel", true);
    return;
  }
  const payload = { pasillo: p, estanteria: est, nivel: n };
  try {
    const nueva = await api("/api/ubicaciones", { method: "POST", body: JSON.stringify(payload) });
    toast("Ubicación creada: " + nueva.codigo);
    cerrarModal("modal-ubicacion");
    await cargarUbicaciones();
    if (state.origenModalUbicacion === "producto") {
      renderSelectUbicaciones(nueva.id);
      document.getElementById("f-ubicacion-id").value = nueva.id;
    }
  } catch (e2) {
    toast(e2.message, true);
  }
});

// ---------------------------------------------------------------------------
// Categorías y modelos precargados (con alta rápida desde el formulario)
// ---------------------------------------------------------------------------

async function cargarCategoriasYMarcas() {
  try {
    const [cats, modelos, marcas] = await Promise.all([
      api("/api/categorias"),
      api("/api/modelos"),
      api("/api/marcas"),
    ]);
    // La API devuelve objetos {id, nombre, n_productos} (no strings sueltos)
    // para poder editarlos/eliminarlos desde el panel de gestión.
    state.categoriasDisponibles = cats.categorias;
    state.modelosDisponibles = modelos.modelos;
    state.marcasDisponibles = marcas.marcas;
    renderSelectCategoria();
    renderSelectModelo();
    renderSelectMarca();
    const modalGestion = document.getElementById("modal-gestion-categorias");
    if (modalGestion && modalGestion.classList.contains("abierto")) {
      renderGestionCategorias();
      renderGestionModelos();
    }
  } catch (e) {
    console.error(e);
  }
}

function renderSelectCategoria(seleccionActual) {
  const select = document.getElementById("f-categoria");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  select.innerHTML = '<option value="">Sin modelo</option>' +
    state.categoriasDisponibles.map(c => `<option value="${escapeHtml(c.nombre)}">${escapeHtml(c.nombre)}</option>`).join("");
  select.value = actual || "";
}

function renderSelectModelo(seleccionActual) {
  const select = document.getElementById("f-modelo");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  select.innerHTML = '<option value="">Sin detalle</option>' +
    state.modelosDisponibles.map(m => `<option value="${escapeHtml(m.nombre)}">${escapeHtml(m.nombre)}</option>`).join("");
  select.value = actual || "";
}

function renderSelectMarca(seleccionActual) {
  const select = document.getElementById("f-marca");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  // Ya no hay "Sin marca": la marca es obligatoria para crear/editar un
  // producto con modelo y/o detalle (ver crear_producto/editar_producto en
  // app.py) -- esta opción vacía es solo el placeholder de "todavía no
  // elegiste", no un valor válido para guardar.
  select.innerHTML = '<option value="">Selecciona una marca…</option>' +
    state.marcasDisponibles.map(m => `<option value="${escapeHtml(m.nombre)}">${escapeHtml(m.nombre)}</option>`).join("");
  select.value = actual || "";
}

// Aviso en vivo si el Modelo+Detalle que se está escribiendo ya existe en
// el catálogo con otra(s) marca(s) -- ayuda a decidir de un vistazo si
// corresponde agregar una marca nueva de verdad, o si es la misma pieza y
// solo falta elegir su marca ya cargada (evita duplicados por typo, ej.
// "BOSCH " con espacio de más quedando como si fuera otra marca distinta).
function actualizarHintVariantesMarca() {
  const hint = document.getElementById("f-marca-variantes-hint");
  if (!hint) return;
  const categoria = document.getElementById("f-categoria").value.trim();
  const modelo = document.getElementById("f-modelo").value.trim();
  const idActual = document.getElementById("f-id").value;
  if (!categoria && !modelo) {
    hint.style.display = "none";
    return;
  }
  const variantes = (state.productos || []).filter(p =>
    (p.categoria || "").trim().toLowerCase() === categoria.toLowerCase() &&
    (p.modelo || "").trim().toLowerCase() === modelo.toLowerCase() &&
    String(p.id) !== String(idActual)
  );
  if (!variantes.length) {
    hint.style.display = "none";
    return;
  }
  const detalle = variantes
    .map(p => `${p.marca ? escapeHtml(p.marca) : "sin marca (repuesto antiguo, corrígelo si lo ves)"} — ${fmtMoney(p.costo_compra)}`)
    .join(" · ");
  hint.innerHTML = `Este modelo/detalle ya existe en: ${detalle}. Si es la misma marca, usa ese producto (ingrésalo por factura) en vez de crear uno nuevo — si es una marca distinta, sigue no más.`;
  hint.style.display = "block";
}

// Nombres de función y de la tabla interna ("categoria"/"modelo"/"marca") se
// mantienen sin cambio -- ver relabeling en app.py/core.py -- solo el
// texto que ve el taller pasa a ser "Modelo"/"Detalle"/"Marca".
const ETIQUETAS_ALTA_RAPIDA = {
  categoria: { texto: "modelo", render: renderSelectCategoria },
  modelo: { texto: "detalle", render: renderSelectModelo },
  marca: { texto: "marca", render: renderSelectMarca },
};

// Alta rápida de modelo/detalle: antes usaba prompt(), que no deja ver el
// resto del formulario ni valida nada en vivo. Se reemplaza por un modal
// chico y reutilizable (un solo campo de texto), siguiendo el mismo patrón
// de callback que ya se usa para mermas y "nuevo proveedor" desde factura.
let altaRapidaCallback = null;

function abrirModalAltaRapida({ titulo, label, callback }) {
  document.getElementById("ar-titulo").textContent = titulo;
  document.getElementById("ar-label").textContent = label;
  const input = document.getElementById("ar-nombre");
  input.value = "";
  altaRapidaCallback = callback;
  abrirModal("modal-alta-rapida");
  setTimeout(() => input.focus(), 50);
}

document.getElementById("form-alta-rapida").addEventListener("submit", async (e) => {
  e.preventDefault();
  const nombre = document.getElementById("ar-nombre").value.trim();
  if (!nombre) {
    toast("Ingresa un nombre", true);
    return;
  }
  if (altaRapidaCallback) {
    await altaRapidaCallback(nombre);
  }
});

document.querySelectorAll("[data-nuevo]").forEach(btn => {
  btn.addEventListener("click", () => {
    const tipo = btn.dataset.nuevo; // "categoria" | "modelo"
    const info = ETIQUETAS_ALTA_RAPIDA[tipo];
    abrirModalAltaRapida({
      titulo: `Nueva ${info.texto}`,
      label: `Nombre de la ${info.texto}`,
      callback: async (nombre) => {
        try {
          await api(`/api/${tipo}s`, { method: "POST", body: JSON.stringify({ nombre }) });
          await cargarCategoriasYMarcas();
          info.render(nombre);
          toast(`${info.texto[0].toUpperCase() + info.texto.slice(1)} agregada`);
          cerrarModal("modal-alta-rapida");
        } catch (e) {
          toast(e.message, true);
        }
      },
    });
  });
});

// ---------------------------------------------------------------------------
// Panel de gestión: renombrar/eliminar categorías y modelos
// (para corregir una categoría mal ingresada sin editar producto por producto)
// ---------------------------------------------------------------------------

function renderGestionCategorias() {
  const tbody = document.getElementById("tbody-gestion-categorias");
  const lista = state.categoriasDisponibles;
  if (!lista.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="vacio">No hay modelos cargados.</td></tr>`;
    return;
  }
  tbody.innerHTML = lista.map(c => `
    <tr>
      <td>${escapeHtml(c.nombre)}</td>
      <td class="num mono">${c.n_productos}</td>
      <td>
        <button type="button" class="btn-link" data-editar-categoria="${c.id}" data-nombre="${escapeHtml(c.nombre)}">Renombrar</button>
        <button type="button" class="btn-link btn-link--peligro" data-eliminar-categoria="${c.id}" data-nombre="${escapeHtml(c.nombre)}">Eliminar</button>
      </td>
    </tr>`).join("");

  tbody.querySelectorAll("[data-editar-categoria]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const nuevo = prompt(`Nuevo nombre para el modelo "${btn.dataset.nombre}":`, btn.dataset.nombre);
      if (!nuevo || !nuevo.trim() || nuevo.trim().toUpperCase() === btn.dataset.nombre) return;
      try {
        const r = await api(`/api/categorias/${btn.dataset.editarCategoria}`, {
          method: "PUT", body: JSON.stringify({ nombre: nuevo.trim() }),
        });
        toast(`Modelo renombrado (${r.productos_actualizados} producto(s) actualizados)`);
        await Promise.all([cargarCategoriasYMarcas(), cargarProductos()]);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  tbody.querySelectorAll("[data-eliminar-categoria]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`¿Quitar "${btn.dataset.nombre}" de la lista de modelos precargados? Los productos que ya lo tienen NO se modifican.`)) return;
      try {
        await api(`/api/categorias/${btn.dataset.eliminarCategoria}`, { method: "DELETE" });
        toast("Modelo eliminado de la lista");
        await Promise.all([cargarCategoriasYMarcas(), cargarProductos()]);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
}

function renderGestionModelos() {
  const tbody = document.getElementById("tbody-gestion-modelos");
  const lista = state.modelosDisponibles;
  if (!lista.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="vacio">No hay detalles cargados.</td></tr>`;
    return;
  }
  tbody.innerHTML = lista.map(m => `
    <tr>
      <td>${escapeHtml(m.nombre)}</td>
      <td class="num mono">${m.n_productos}</td>
      <td>
        <button type="button" class="btn-link" data-editar-modelo="${m.id}" data-nombre="${escapeHtml(m.nombre)}">Renombrar</button>
        <button type="button" class="btn-link btn-link--peligro" data-eliminar-modelo="${m.id}" data-nombre="${escapeHtml(m.nombre)}">Eliminar</button>
      </td>
    </tr>`).join("");

  tbody.querySelectorAll("[data-editar-modelo]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const nuevo = prompt(`Nuevo nombre para el detalle "${btn.dataset.nombre}":`, btn.dataset.nombre);
      if (!nuevo || !nuevo.trim() || nuevo.trim().toUpperCase() === btn.dataset.nombre) return;
      try {
        const r = await api(`/api/modelos/${btn.dataset.editarModelo}`, {
          method: "PUT", body: JSON.stringify({ nombre: nuevo.trim() }),
        });
        toast(`Detalle renombrado (${r.productos_actualizados} producto(s) actualizados)`);
        await Promise.all([cargarCategoriasYMarcas(), cargarProductos()]);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  tbody.querySelectorAll("[data-eliminar-modelo]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`¿Quitar "${btn.dataset.nombre}" de la lista de detalles precargados? Los productos que ya lo tienen NO se modifican.`)) return;
      try {
        await api(`/api/modelos/${btn.dataset.eliminarModelo}`, { method: "DELETE" });
        toast("Detalle eliminado de la lista");
        await Promise.all([cargarCategoriasYMarcas(), cargarProductos()]);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
}

document.getElementById("btn-gestion-categorias").addEventListener("click", async () => {
  abrirModal("modal-gestion-categorias");
  await cargarCategoriasYMarcas();
  renderGestionCategorias();
  renderGestionModelos();
});

document.getElementById("gc-btn-agregar-categoria").addEventListener("click", async () => {
  const input = document.getElementById("gc-nueva-categoria");
  const nombre = input.value.trim();
  if (!nombre) return;
  try {
    await api("/api/categorias", { method: "POST", body: JSON.stringify({ nombre }) });
    input.value = "";
    toast("Modelo agregado");
    await cargarCategoriasYMarcas();
    renderGestionCategorias();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("gc-btn-agregar-modelo").addEventListener("click", async () => {
  const input = document.getElementById("gc-nuevo-modelo");
  const nombre = input.value.trim();
  if (!nombre) return;
  try {
    await api("/api/modelos", { method: "POST", body: JSON.stringify({ nombre }) });
    input.value = "";
    toast("Detalle agregado");
    await cargarCategoriasYMarcas();
    renderGestionModelos();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Carga de datos: productos
// ---------------------------------------------------------------------------

async function cargarResumen() {
  try {
    const r = await api("/api/resumen");
    document.getElementById("s-total-productos").textContent = r.total_productos;
    document.getElementById("s-total-unidades").textContent = r.total_unidades;
    document.getElementById("s-valor-total").textContent = fmtMoney(r.valor_total);
    document.getElementById("s-criticos").textContent = r.stock_critico;
  } catch (e) {
    console.error(e);
  }
}

async function cargarProductos(resetearPagina) {
  if (resetearPagina) state.paginaProductos = 1;

  const q = document.getElementById("buscar").value.trim();
  const categoria = document.getElementById("filtro-categoria").value;
  const criticos = document.getElementById("filtro-criticos").checked;
  const sinUbicacion = document.getElementById("filtro-sin-ubicacion").checked;

  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (categoria) params.set("categoria", categoria);
  if (criticos) params.set("criticos", "1");
  if (sinUbicacion) params.set("sin_ubicacion", "1");
  params.set("pagina", state.paginaProductos);
  params.set("por_pagina", state.porPaginaProductos);

  const tbody = document.getElementById("tbody-productos");
  try {
    const data = await api("/api/productos?" + params.toString());
    state.productos = data.productos;
    state.categorias = data.categorias;
    state.totalProductos = data.total;
    state.totalPaginasProductos = data.total_paginas || 1;
    state.paginaProductos = data.pagina || 1;
    renderCategorias();
    renderTabla();
    renderPaginacionProductos();
    const badge = document.getElementById("contador-sin-ubicacion");
    if (data.total_sin_ubicacion > 0) {
      badge.textContent = data.total_sin_ubicacion;
      badge.style.display = "inline-block";
    } else {
      badge.style.display = "none";
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="vacio">Error al cargar productos: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function renderPaginacionProductos() {
  const cont = document.getElementById("paginacion-productos");
  if (!cont) return;

  if (state.totalProductos === 0) {
    cont.innerHTML = "";
    return;
  }

  const desde = (state.paginaProductos - 1) * state.porPaginaProductos + 1;
  const hasta = Math.min(state.paginaProductos * state.porPaginaProductos, state.totalProductos);

  cont.innerHTML = `
    <span class="paginacion-info">Mostrando ${desde}–${hasta} de ${state.totalProductos} producto(s)</span>
    <div class="paginacion-botones">
      <button type="button" class="btn btn-secundario" id="btn-pagina-anterior" ${state.paginaProductos <= 1 ? "disabled" : ""}>← Anterior</button>
      <span class="paginacion-actual">Página ${state.paginaProductos} de ${state.totalPaginasProductos}</span>
      <button type="button" class="btn btn-secundario" id="btn-pagina-siguiente" ${state.paginaProductos >= state.totalPaginasProductos ? "disabled" : ""}>Siguiente →</button>
    </div>
  `;

  const btnAnt = document.getElementById("btn-pagina-anterior");
  const btnSig = document.getElementById("btn-pagina-siguiente");
  if (btnAnt) btnAnt.addEventListener("click", () => {
    if (state.paginaProductos > 1) {
      state.paginaProductos--;
      cargarProductos();
      document.querySelector(".tabla-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  if (btnSig) btnSig.addEventListener("click", () => {
    if (state.paginaProductos < state.totalPaginasProductos) {
      state.paginaProductos++;
      cargarProductos();
      document.querySelector(".tabla-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
}

function renderCategorias() {
  const select = document.getElementById("filtro-categoria");
  const actual = select.value;
  select.innerHTML = '<option value="">Todos los modelos</option>' +
    state.categorias.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  select.value = actual;
}

function renderTabla() {
  const tbody = document.getElementById("tbody-productos");
  if (state.productos.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" class="vacio">No se encontraron productos.</td></tr>`;
    return;
  }

  tbody.innerHTML = state.productos.map(p => {
    const critico = p.stock_disponible <= p.stock_minimo;
    let ubicacionTexto;
    if (p.ubicacion_codigo) {
      ubicacionTexto = `<span class="codigo-pill">${escapeHtml(p.ubicacion_codigo)}</span>`;
    } else if (p.ubicacion) {
      ubicacionTexto = `<span class="subtexto" title="Ubicación antigua, sin asignar a una ubicación estructurada">${escapeHtml(p.ubicacion)} · pendiente</span>`;
    } else {
      ubicacionTexto = "—";
    }
    // El nombre visible se arma como "Modelo - Detalle" (o el código
    // interno / "Producto #N" si faltan ambos) — con Modelo y Detalle ya
    // como columnas propias, mostrar aquí la descripción es lo que aporta
    // información distinta; si no hay descripción cargada, se usa el
    // nombre como respaldo para no dejar la fila vacía.
    const principal = p.descripcion || p.nombre;
    return `
      <tr data-id="${p.id}">
        <td><span class="codigo-pill">#${escapeHtml(p.id_sistema)}</span></td>
        <td>
          <div class="nombre-producto">${escapeHtml(principal)} ${critico ? '<span class="badge-critico">Crítico</span>' : ""}</div>
          ${p.codigo_interno ? `<div class="subtexto">${escapeHtml(p.codigo_interno)}</div>` : ""}
        </td>
        <td>${escapeHtml(p.categoria) || "—"}</td>
        <td>${escapeHtml(p.modelo) || "—"}</td>
        <td>${ubicacionTexto}</td>
        <td class="num mono">${p.stock_disponible}</td>
        <td class="num mono">${p.stock_reservado}</td>
        <td class="num mono">${p.stock_total}</td>
        <td class="num mono">${fmtMoney(p.costo_compra)}</td>
        <td><button type="button" class="btn-link" data-mov-rapido="${p.id}" title="Registrar un movimiento de stock para este producto">+ Movimiento</button></td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => abrirDetalle(parseInt(tr.dataset.id, 10)));
  });

  tbody.querySelectorAll("[data-mov-rapido]").forEach(btn => {
    btn.addEventListener("click", (e) => {
      // stopPropagation: si no, el click también le llega a la fila (que
      // abre el mismo detalle, pero sin el enfoque automático al formulario).
      e.stopPropagation();
      abrirDetalle(parseInt(btn.dataset.movRapido, 10), true);
    });
  });
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Filtros
// ---------------------------------------------------------------------------

let buscarTimeout;
document.getElementById("buscar").addEventListener("input", () => {
  clearTimeout(buscarTimeout);
  buscarTimeout = setTimeout(() => cargarProductos(true), 250);
});
document.getElementById("filtro-categoria").addEventListener("change", () => cargarProductos(true));
document.getElementById("filtro-criticos").addEventListener("change", () => cargarProductos(true));
document.getElementById("filtro-sin-ubicacion").addEventListener("change", () => cargarProductos(true));

// ---------------------------------------------------------------------------
// Importar Excel — reemplaza TODO el catálogo de productos por el
// contenido de un archivo subido. Dos pasos: previsualizar (solo lee el
// archivo y muestra un resumen, no toca la base) y confirmar (recién ahí
// borra los productos actuales e inserta los nuevos) — exige escribir
// "REEMPLAZAR" a mano antes de habilitar el botón de confirmar, porque es
// una acción destructiva que no se puede deshacer.
// ---------------------------------------------------------------------------

document.getElementById("btn-importar-excel").addEventListener("click", () => {
  document.getElementById("imp-excel-archivo").value = "";
  document.getElementById("imp-excel-resultado").style.display = "none";
  document.getElementById("imp-excel-preview-ok").style.display = "none";
  document.getElementById("imp-excel-error").style.display = "none";
  document.getElementById("imp-excel-confirmar-texto").value = "";
  document.getElementById("btn-imp-excel-confirmar").disabled = true;
  abrirModal("modal-importar-excel");
});

document.getElementById("imp-excel-confirmar-texto").addEventListener("input", (e) => {
  document.getElementById("btn-imp-excel-confirmar").disabled = e.target.value.trim().toUpperCase() !== "REEMPLAZAR";
});

document.getElementById("btn-imp-excel-previsualizar").addEventListener("click", async () => {
  const archivo = document.getElementById("imp-excel-archivo").files[0];
  const resultadoDiv = document.getElementById("imp-excel-resultado");
  const errorP = document.getElementById("imp-excel-error");
  const okDiv = document.getElementById("imp-excel-preview-ok");
  resultadoDiv.style.display = "block";
  okDiv.style.display = "none";
  errorP.style.display = "none";
  if (!archivo) {
    errorP.textContent = "Selecciona un archivo Excel (.xlsx) primero.";
    errorP.style.display = "block";
    return;
  }
  errorP.textContent = "";
  errorP.style.display = "none";
  try {
    const formData = new FormData();
    formData.append("archivo", archivo);
    const resp = await fetch("/api/productos/importar-excel/previsualizar", { method: "POST", body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || "No se pudo leer el archivo.");

    document.getElementById("imp-excel-total-filas").textContent = data.total_filas_validas;
    document.getElementById("imp-excel-total-advertencias").textContent = data.total_advertencias;
    document.getElementById("imp-excel-valor-total").textContent = fmtMoney(data.valor_total_estimado);
    document.getElementById("imp-excel-total-actuales").textContent = data.productos_actuales_a_reemplazar;

    document.getElementById("imp-excel-muestra").innerHTML = data.muestra.map(m => `
      <tr>
        <td>${escapeHtml(m.nombre_resultante)}</td>
        <td>${escapeHtml(m.ubicacion) || "—"}</td>
        <td class="num">${m.cantidad}</td>
        <td class="num">${fmtMoney(m.costo_compra)}</td>
      </tr>
    `).join("");

    const advWrap = document.getElementById("imp-excel-advertencias-wrap");
    if (data.advertencias && data.advertencias.length) {
      advWrap.style.display = "block";
      let lista = data.advertencias.map(a => `<li>${escapeHtml(a)}</li>`).join("");
      if (data.advertencias_recortadas > 0) {
        lista += `<li>… y ${data.advertencias_recortadas} advertencia(s) más.</li>`;
      }
      document.getElementById("imp-excel-advertencias-lista").innerHTML = lista;
    } else {
      advWrap.style.display = "none";
    }

    document.getElementById("imp-excel-confirmar-texto").value = "";
    document.getElementById("btn-imp-excel-confirmar").disabled = true;
    okDiv.style.display = "block";
  } catch (e) {
    errorP.textContent = e.message;
    errorP.style.display = "block";
  }
});

document.getElementById("btn-imp-excel-confirmar").addEventListener("click", async () => {
  const archivo = document.getElementById("imp-excel-archivo").files[0];
  const confirmarTexto = document.getElementById("imp-excel-confirmar-texto").value.trim();
  const errorP = document.getElementById("imp-excel-error");
  if (!archivo) {
    errorP.textContent = "Selecciona un archivo Excel (.xlsx) primero.";
    errorP.style.display = "block";
    return;
  }
  if (!confirm("Esto va a BORRAR todos los productos actuales y reemplazarlos por el contenido del Excel. ¿Continuar?")) {
    return;
  }
  const btn = document.getElementById("btn-imp-excel-confirmar");
  btn.disabled = true;
  const textoOriginal = btn.textContent;
  btn.textContent = "Reemplazando…";
  try {
    const formData = new FormData();
    formData.append("archivo", archivo);
    formData.append("confirmar_texto", confirmarTexto);
    const resp = await fetch("/api/productos/importar-excel/confirmar", { method: "POST", body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || "No se pudo reemplazar el catálogo.");
    toast(`Catálogo reemplazado: ${data.productos_eliminados} producto(s) eliminado(s), ${data.productos_creados} creado(s).`);
    cerrarModal("modal-importar-excel");
    await cargarProductos(true);
  } catch (e) {
    errorP.textContent = e.message;
    errorP.style.display = "block";
    btn.disabled = false;
    btn.textContent = textoOriginal;
  }
});

// ---------------------------------------------------------------------------
// Modal: crear / editar producto
// ---------------------------------------------------------------------------

const formProducto = document.getElementById("form-producto");

document.getElementById("btn-nuevo").addEventListener("click", async () => {
  formProducto.reset();
  document.getElementById("f-id").value = "";
  document.getElementById("f-id-sistema-tag").style.display = "none";
  document.getElementById("modal-producto-titulo").textContent = "Nuevo producto";
  document.getElementById("grid-stock-inicial").style.display = "grid";
  document.getElementById("ayuda-stock").style.display = "block";
  document.getElementById("f-costo-label").textContent = "Costo promedio inicial ($ CLP)";
  document.getElementById("f-costo").readOnly = false;
  document.getElementById("f-stock-disponible").closest(".campo").style.display = "flex";
  document.getElementById("f-ubicacion-legacy").style.display = "none";
  await Promise.all([cargarUbicaciones(), cargarCategoriasYMarcas()]);
  renderSelectUbicaciones(null);
  renderSelectCategoria("");
  renderSelectModelo("");
  renderSelectMarca("");
  actualizarHintVariantesMarca();
  abrirModal("modal-producto");
});

document.getElementById("f-categoria").addEventListener("change", actualizarHintVariantesMarca);
document.getElementById("f-modelo").addEventListener("change", actualizarHintVariantesMarca);

async function abrirEdicion(p) {
  document.getElementById("f-id").value = p.id;
  document.getElementById("f-id-sistema-tag").textContent = `N° de sistema: #${p.id_sistema} (no editable)`;
  document.getElementById("f-id-sistema-tag").style.display = "block";
  document.getElementById("f-codigo").value = p.codigo_interno || "";
  document.getElementById("f-descripcion").value = p.descripcion || "";
  // El costo promedio ya no se puede editar a mano acá — se recalcula solo
  // como promedio ponderado con cada entrada de stock (misma lógica que ya
  // se aplicaba al stock). Se deja solo como dato informativo, de solo
  // lectura, para no perder la trazabilidad de cómo se llegó a ese número.
  document.getElementById("f-costo").value = p.costo_compra;
  document.getElementById("f-costo").readOnly = true;
  document.getElementById("f-costo-label").textContent = "Costo promedio actual ($ CLP) — solo lectura";

  await Promise.all([cargarUbicaciones(), cargarCategoriasYMarcas()]);
  renderSelectUbicaciones(p.ubicacion_id);
  renderSelectCategoria(p.categoria || "");
  renderSelectModelo(p.modelo || "");
  renderSelectMarca(p.marca || "");
  actualizarHintVariantesMarca();

  const legacy = document.getElementById("f-ubicacion-legacy");
  if (!p.ubicacion_id && p.ubicacion) {
    legacy.textContent = `Ubicación anterior (sin migrar): "${p.ubicacion}". Selecciona una ubicación de la lista para reemplazarla.`;
    legacy.style.display = "block";
  } else {
    legacy.style.display = "none";
  }

  document.getElementById("modal-producto-titulo").textContent = "Editar producto";
  document.getElementById("grid-stock-inicial").style.display = "grid";
  document.getElementById("ayuda-stock").style.display = "none";
  document.getElementById("f-stock-disponible").closest(".campo").style.display = "none";

  abrirModal("modal-producto");
}

formProducto.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("f-id").value;

  const payload = {
    codigo_interno: document.getElementById("f-codigo").value,
    descripcion: document.getElementById("f-descripcion").value,
    categoria: document.getElementById("f-categoria").value,
    modelo: document.getElementById("f-modelo").value,
    marca: document.getElementById("f-marca").value,
    ubicacion_id: document.getElementById("f-ubicacion-id").value,
  };

  // Mismo criterio que valida el backend (ver _crear_producto_desde_datos /
  // editar_producto en app.py): con Modelo y/o Detalle, la Marca ya no es
  // opcional -- se avisa acá antes de mandar la petición para no hacerle
  // esperar el viaje al servidor solo para enterarse.
  if ((payload.categoria || payload.modelo) && !payload.marca.trim()) {
    toast('Elige (o crea con "+ Nuevo") una marca para este repuesto. Si no tiene una marca real, usa algo como "GENÉRICA".', true);
    return;
  }

  if (!id) {
    // El costo (inicial) y el stock inicial solo se piden al CREAR el
    // producto. Al editar, el costo promedio ya no viaja en el payload —
    // se sigue calculando solo con cada entrada de stock real, nunca a
    // mano desde este formulario (el backend además lo ignora si llegara).
    payload.stock_disponible = document.getElementById("f-stock-disponible").value;
    payload.costo_compra = document.getElementById("f-costo").value;
  }

  try {
    if (id) {
      await api(`/api/productos/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Producto actualizado");
    } else {
      await api("/api/productos", { method: "POST", body: JSON.stringify(payload) });
      toast("Producto creado");
    }
    cerrarModal("modal-producto");
    document.getElementById("f-stock-disponible").closest(".campo").style.display = "flex";
    await Promise.all([cargarProductos(), cargarResumen()]);
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Modal: detalle + movimientos
// ---------------------------------------------------------------------------

async function abrirDetalle(id, enfocarMovimiento) {
  try {
    const p = await api(`/api/productos/${id}`);
    state.productoActivo = p;
    renderDetalle(p);
    resetearCampoFactura();
    resetearCampoOrden();
    document.getElementById("m-tipo").value = "entrada";
    document.getElementById("m-cantidad-label").textContent = "Cantidad";
    document.getElementById("m-costo-wrap").style.display = "flex";
    document.getElementById("m-factura-wrap").style.display = "flex";
    document.getElementById("m-categoria-wrap").style.display = "flex";
    document.getElementById("m-proveedor-merma-wrap").style.display = "none";
    document.getElementById("m-proveedor-salida-wrap").style.display = "none";
    document.getElementById("m-orden-wrap").style.display = "none";
    poblarSelectCategoriaMovimiento(CATEGORIAS_ENTRADA, "Compra");
    abrirModal("modal-detalle");
    if (enfocarMovimiento) {
      // Botón "+ Movimiento" de la fila: se salta directo al formulario de
      // movimiento (que ya vive dentro de este mismo modal) en vez de
      // dejar que la persona tenga que ubicarlo scrolleando entre toda la
      // demás info del producto.
      setTimeout(() => {
        document.getElementById("form-movimiento").scrollIntoView({ behavior: "smooth", block: "center" });
        document.getElementById("m-cantidad").focus();
      }, 80);
    }
  } catch (e) {
    toast(e.message, true);
  }
}

function renderDetalle(p) {
  document.getElementById("detalle-nombre").textContent = `${p.nombre}`;
  document.getElementById("m-producto-id").value = p.id;
  renderFotoProducto(p);

  const ubicacionTexto = p.ubicacion_codigo
    ? `<span class="mono">${escapeHtml(p.ubicacion_codigo)}</span>`
    : (p.ubicacion ? `${escapeHtml(p.ubicacion)} <span class="subtexto">(pendiente de asignar)</span>` : "—");

  const ficha = document.getElementById("detalle-ficha");
  ficha.innerHTML = `
    <dt>N° de sistema</dt><dd class="mono">#${escapeHtml(p.id_sistema)}</dd>
    <dt>Código interno</dt><dd class="mono">${escapeHtml(p.codigo_interno) || "—"}</dd>
    <dt>Modelo</dt><dd>${escapeHtml(p.categoria) || "—"}</dd>
    <dt>Detalle</dt><dd>${escapeHtml(p.modelo) || "—"}</dd>
    <dt>Marca</dt><dd>${escapeHtml(p.marca) || "—"}</dd>
    <dt>Ubicación</dt><dd>${ubicacionTexto}</dd>
    <dt>Descripción</dt><dd>${escapeHtml(p.descripcion) || "—"}</dd>
    <dt>Disponible</dt><dd class="mono">${p.stock_disponible}</dd>
    <dt>Reservado</dt><dd class="mono">${p.stock_reservado}</dd>
    <dt>Total</dt><dd class="mono">${p.stock_total}</dd>
    <dt>En merma</dt><dd class="mono">${p.stock_merma}${p.stock_merma > 0 ? " <span class=\"subtexto\">(ver pestaña Mermas)</span>" : ""}</dd>
    <dt>Stock mínimo</dt><dd class="mono">${p.stock_minimo} <span class="badge-urgencia badge-urgencia--proximo" title="Se recalcula solo con cada movimiento de stock">AUTO</span></dd>
    <dt>Costo promedio</dt><dd class="mono">${fmtMoney(p.costo_compra)}</dd>
  `;

  const contCalculo = document.getElementById("detalle-stock-minimo-calculo");
  const c = p.stock_minimo_detalle;
  if (c) {
    const leadTxt = c.lead_time_es_estimado_por_defecto
      ? `${c.lead_time_dias} día(s) (estimación por defecto — todavía no hay suficientes compras históricas de este producto para calcularlo con datos reales)`
      : `${c.lead_time_dias} día(s) (estimado del historial real de compras)`;
    contCalculo.innerHTML = `¿Cómo se calculó? Consumo promedio ${c.consumo_diario_promedio}/día × ${leadTxt} de tiempo de entrega + ${c.stock_seguridad} de resguardo por variabilidad = ${c.stock_minimo_calculado}. Última actualización: ${fmtFecha(c.calculado_en)}.`;
  } else {
    contCalculo.textContent = "Todavía no se ha calculado (se hace automáticamente con el primer movimiento de stock de este producto).";
  }

  const lista = document.getElementById("historial-lista");
  if (!p.movimientos || p.movimientos.length === 0) {
    lista.innerHTML = `<p class="vacio">Sin movimientos aún.</p>`;
  } else {
    lista.innerHTML = p.movimientos.map(m => `
      <div class="mov-item">
        <span class="mov-tipo mov-tipo--${m.tipo}">${m.tipo}</span>
        <span class="mov-detalle">
          ${m.cantidad >= 0 ? "+" : ""}${m.cantidad} → stock: ${m.stock_resultante}
          ${m.categoria ? " · " + escapeHtml(m.categoria) : ""}
          ${m.costo_unitario !== null && m.costo_unitario !== undefined ? " · " + fmtMoney(m.costo_unitario) + " c/u" : ""}
          ${m.factura_numero ? " · factura " + escapeHtml(m.factura_numero) : ""}
          ${m.proveedor_nombre ? " · proveedor: " + escapeHtml(m.proveedor_nombre) : ""}
          ${m.motivo ? " · " + escapeHtml(m.motivo) : ""}
          ${m.usuario ? " · " + escapeHtml(m.usuario) : ""}
        </span>
        <span class="mov-fecha">${fmtFecha(m.fecha)}</span>
      </div>
    `).join("");
  }
}

// Un archivo .heic/.heif se sube y se guarda sin problema, pero casi
// ningún navegador de escritorio sabe MOSTRAR ese formato en una <img> —
// es el formato en que el iPhone guarda fotos por defecto, así que es el
// caso más común. En vez de dejar que la foto "desaparezca" (ícono roto,
// sin explicación), se avisa apenas se elige el archivo y se deja un
// mensaje + link de descarga en la previsualización si el navegador no
// pudo renderizarla.
const EXTENSIONES_FOTO_SIN_PREVIEW = [".heic", ".heif"];

function esFotoSinPreview(nombreArchivo) {
  const n = (nombreArchivo || "").toLowerCase();
  return EXTENSIONES_FOTO_SIN_PREVIEW.some(ext => n.endsWith(ext));
}

function renderFotoProducto(p) {
  const preview = document.getElementById("detalle-foto-preview");
  const btnQuitar = document.getElementById("btn-quitar-foto");
  if (p.tiene_foto && p.foto_archivo) {
    const url = `/api/productos/foto/${encodeURIComponent(p.foto_archivo)}`;
    if (esFotoSinPreview(p.foto_archivo)) {
      // Se sabe de antemano que este formato no se puede previsualizar —
      // ni se intenta poner el <img> (evita el ícono roto).
      preview.innerHTML = `
        <span class="vacio-inline">
          Hay una foto guardada, pero en formato HEIC — el navegador no puede mostrarla acá.
          <a href="${url}" download>Descargar para verla</a>
        </span>`;
    } else {
      preview.innerHTML = `<img src="${url}" alt="Foto de ${escapeHtml(p.nombre)}">`;
      // Aunque la extensión no sea heic/heif, por si acaso el navegador
      // igual no logra decodificarla (foto corrupta, formato raro, etc.)
      // se deja el mismo mensaje de respaldo en vez de un ícono roto.
      preview.querySelector("img").addEventListener("error", () => {
        preview.innerHTML = `
          <span class="vacio-inline">
            Hay una foto guardada, pero el navegador no pudo mostrarla acá.
            <a href="${url}" download>Descargar para verla</a>
          </span>`;
      }, { once: true });
    }
    btnQuitar.style.display = "inline-block";
  } else {
    preview.innerHTML = `<span class="vacio-inline">Sin foto</span>`;
    btnQuitar.style.display = "none";
  }
}

document.getElementById("btn-subir-foto").addEventListener("click", () => {
  document.getElementById("detalle-foto-archivo").click();
});

document.getElementById("detalle-foto-archivo").addEventListener("change", async (e) => {
  const archivo = e.target.files[0];
  if (!archivo || !state.productoActivo) return;
  const esHeic = esFotoSinPreview(archivo.name);
  const formData = new FormData();
  formData.append("foto", archivo);
  try {
    const res = await fetch(`/api/productos/${state.productoActivo.id}/foto`, { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo subir la foto");
    // Si es HEIC, este aviso reemplaza al "Foto guardada" normal (un solo
    // toast a la vez) — ya deja claro que sí se guardó, además de advertir
    // sobre el formato, en vez de mostrar dos mensajes que se pisan entre sí.
    if (esHeic) {
      toast("Foto guardada, pero en formato HEIC no se va a poder ver dentro del sistema (la mayoría de los navegadores no la muestra). Si puedes, conviértela a JPG — en iPhone: Ajustes > Cámara > Formatos > \"Más compatible\".", true);
    } else {
      toast("Foto guardada");
    }
    const actualizado = await api(`/api/productos/${state.productoActivo.id}`);
    state.productoActivo = actualizado;
    renderFotoProducto(actualizado);
  } catch (err) {
    toast(err.message, true);
  } finally {
    e.target.value = "";
  }
});

document.getElementById("btn-quitar-foto").addEventListener("click", async () => {
  if (!state.productoActivo) return;
  if (!confirm("¿Quitar la foto de este producto?")) return;
  try {
    await api(`/api/productos/${state.productoActivo.id}/foto`, { method: "DELETE" });
    toast("Foto eliminada");
    const actualizado = await api(`/api/productos/${state.productoActivo.id}`);
    state.productoActivo = actualizado;
    renderFotoProducto(actualizado);
  } catch (err) {
    toast(err.message, true);
  }
});

function desbloquearCamposMovimiento() {
  document.getElementById("m-cantidad").readOnly = false;
  document.getElementById("m-costo").readOnly = false;
}

function resetearCampoFactura() {
  document.getElementById("m-factura").value = "";
  document.getElementById("m-factura-preview").style.display = "none";
  desbloquearCamposMovimiento();
  state.facturaEncontrada = null;
}

async function buscarFacturaParaMovimiento() {
  const numero = document.getElementById("m-factura").value.trim();
  const preview = document.getElementById("m-factura-preview");
  desbloquearCamposMovimiento();
  state.facturaEncontrada = null;

  if (!numero || !state.productoActivo) {
    preview.style.display = "none";
    return;
  }

  try {
    // El número se guarda siempre en mayúsculas (crear_factura lo normaliza
    // con .strip().upper()), así que si se busca en minúscula o mixto la
    // comparación exacta en SQL no encontraba nada aunque la factura sí
    // existiera. Se normaliza acá también antes de buscar.
    const factura = await api(`/api/facturas/numero/${encodeURIComponent(numero.toUpperCase())}`);
    const linea = factura.lineas.find(l => l.producto_id === state.productoActivo.id);

    if (!linea) {
      preview.textContent = `La factura "${numero}" no incluye este producto.`;
      preview.style.display = "block";
      return;
    }
    if (linea.aplicada) {
      preview.textContent = `Esta línea ya fue ingresada a stock antes (${fmtFecha(linea.aplicada_en)}).`;
      preview.style.display = "block";
      return;
    }

    document.getElementById("m-cantidad").value = linea.cantidad;
    document.getElementById("m-costo").value = linea.precio_unitario;
    document.getElementById("m-cantidad").readOnly = true;
    document.getElementById("m-costo").readOnly = true;
    preview.textContent = `✓ Detectado en factura "${numero}": ${linea.cantidad} unidades a ${fmtMoney(linea.precio_unitario)} c/u (cantidad y costo se bloquean para que coincidan con la factura).`;
    preview.style.display = "block";
    state.facturaEncontrada = linea;
  } catch (e) {
    preview.textContent = e.message;
    preview.style.display = "block";
  }
}

let facturaLookupTimeout;
document.getElementById("m-factura").addEventListener("input", () => {
  clearTimeout(facturaLookupTimeout);
  facturaLookupTimeout = setTimeout(buscarFacturaParaMovimiento, 400);
});

const CATEGORIAS_ENTRADA = ["Compra", "Ajuste de inventario", "Devolución"];
const CATEGORIAS_SALIDA = ["Venta", "Consumo interno", "Reserva", "Ajuste de inventario", "Devolución"];
const CATEGORIA_POR_DEFECTO = { entrada: "Compra", salida: "Consumo interno" };

function poblarSelectCategoriaMovimiento(lista, seleccion) {
  const select = document.getElementById("m-categoria");
  select.innerHTML = lista.map(c => `<option value="${c}">${c}</option>`).join("");
  select.value = seleccion;
}

async function cargarProveedoresMerma(productoId) {
  const select = document.getElementById("m-proveedor-merma");
  select.innerHTML = '<option value="">Cargando…</option>';
  try {
    const data = await api(`/api/productos/${productoId}/proveedores-origen`);
    const opciones = ['<option value="">-- Selecciona un proveedor --</option>'];
    data.proveedores.forEach(p => {
      opciones.push(`<option value="${p.id}">${escapeHtml(p.nombre)}</option>`);
    });
    if (data.permite_sin_proveedor) {
      opciones.push('<option value="__sin_proveedor__">Sin proveedor conocido (stock inicial / sin factura)</option>');
    }
    select.innerHTML = opciones.join("");
  } catch (e) {
    select.innerHTML = '<option value="">Error al cargar proveedores</option>';
    toast(e.message, true);
  }
}

// Igual que cargarProveedoresMerma, pero para una salida real (venta,
// consumo interno, reserva, ajuste, devolución): en vez de mirar TODO el
// historial de compras, solo lista proveedores con stock disponible AHORA
// MISMO (balde > 0), porque de ahí es de donde efectivamente se puede
// descontar.
async function cargarProveedoresSalida(productoId) {
  const select = document.getElementById("m-proveedor-salida");
  select.innerHTML = '<option value="">Cargando…</option>';
  try {
    const data = await api(`/api/productos/${productoId}/stock-proveedores`);
    if (data.proveedores.length === 0) {
      select.innerHTML = '<option value="">Este producto no tiene stock disponible</option>';
      return;
    }
    const opciones = ['<option value="">-- Selecciona un proveedor --</option>'];
    data.proveedores.forEach(p => {
      const valor = p.proveedor_id === null ? "__sin_proveedor__" : p.proveedor_id;
      opciones.push(`<option value="${valor}">${escapeHtml(p.proveedor_nombre)} (${p.cantidad} disponibles, ${fmtMoney(p.costo_promedio)} c/u)</option>`);
    });
    select.innerHTML = opciones.join("");
  } catch (e) {
    select.innerHTML = '<option value="">Error al cargar proveedores</option>';
    toast(e.message, true);
  }
}

function resetearCampoOrden() {
  document.getElementById("m-orden-id").value = "";
  document.getElementById("m-orden-preview").style.display = "none";
  state.ordenEncontradaMovimiento = null;
}

async function buscarOrdenParaMovimiento() {
  const numero = document.getElementById("m-orden-id").value.trim();
  const preview = document.getElementById("m-orden-preview");
  state.ordenEncontradaMovimiento = null;

  if (!numero) {
    preview.style.display = "none";
    return;
  }
  try {
    const orden = await api(`/api/operativo/ordenes/${encodeURIComponent(numero)}`);
    const cliente = orden.cliente ? orden.cliente.nombre : "";
    const equipo = orden.equipo ? [orden.equipo.identificador, orden.equipo.anio].filter(Boolean).join(" · ") : "";
    preview.textContent = `✓ OT #${orden.id}${cliente ? " — " + cliente : ""}${equipo ? " (" + equipo + ")" : ""} · estado: ${orden.estado}`;
    preview.style.display = "block";
    state.ordenEncontradaMovimiento = orden;
  } catch (e) {
    preview.textContent = e.message || `No existe una orden de trabajo con el número "${numero}".`;
    preview.style.display = "block";
  }
}

let ordenLookupTimeout;
document.getElementById("m-orden-id").addEventListener("input", () => {
  clearTimeout(ordenLookupTimeout);
  ordenLookupTimeout = setTimeout(buscarOrdenParaMovimiento, 400);
});

// Muestra/oculta el campo de Orden de trabajo y ajusta si es obligatorio,
// según tipo + categoría: solo aplica a una salida con categoría "Consumo
// interno" (obligatorio -- siempre se sabe a qué OT se le descarga el
// repuesto) o "Reserva" (opcional -- se puede reservar para una OT que
// todavía no existe en el sistema).
function actualizarCampoOrdenMovimiento() {
  const tipo = document.getElementById("m-tipo").value;
  const categoria = document.getElementById("m-categoria").value;
  const esSalida = tipo === "salida";
  const ordenAplica = esSalida && (categoria === "Consumo interno" || categoria === "Reserva");
  const ordenObligatoria = esSalida && categoria === "Consumo interno";

  document.getElementById("m-orden-wrap").style.display = ordenAplica ? "flex" : "none";
  document.getElementById("m-orden-label").textContent = "Orden de trabajo" + (ordenObligatoria ? " *" : " (opcional — déjalo vacío si es para una OT que todavía no existe)");
  document.getElementById("m-motivo-label").textContent = ordenAplica
    ? "Detalle adicional (opcional)"
    : "Detalle / referencia (opcional)";
  if (!ordenAplica) resetearCampoOrden();
}

document.getElementById("m-categoria").addEventListener("change", actualizarCampoOrdenMovimiento);

document.getElementById("m-tipo").addEventListener("change", (e) => {
  const tipo = e.target.value;
  const esEntrada = tipo === "entrada";
  const esPerdida = tipo === "perdida";
  const esSalida = tipo === "salida";
  document.getElementById("m-cantidad-label").textContent = "Cantidad";
  document.getElementById("m-costo-wrap").style.display = esEntrada ? "flex" : "none";
  document.getElementById("m-factura-wrap").style.display = esEntrada ? "flex" : "none";
  document.getElementById("m-categoria-wrap").style.display = esPerdida ? "none" : "flex";
  document.getElementById("m-proveedor-merma-wrap").style.display = esPerdida ? "flex" : "none";
  document.getElementById("m-proveedor-salida-wrap").style.display = esSalida ? "flex" : "none";
  const productoId = document.getElementById("m-producto-id").value;
  if (!esPerdida) {
    poblarSelectCategoriaMovimiento(esEntrada ? CATEGORIAS_ENTRADA : CATEGORIAS_SALIDA, CATEGORIA_POR_DEFECTO[tipo]);
  } else if (productoId) {
    cargarProveedoresMerma(productoId);
  }
  if (esSalida && productoId) cargarProveedoresSalida(productoId);
  if (!esEntrada) resetearCampoFactura();
  actualizarCampoOrdenMovimiento();
});

document.getElementById("form-movimiento").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("m-producto-id").value;
  const tipo = document.getElementById("m-tipo").value;
  const categoria = document.getElementById("m-categoria").value;
  const payload = {
    tipo,
    categoria,
    cantidad: document.getElementById("m-cantidad").value,
    costo_unitario: document.getElementById("m-costo").value,
    factura_numero: document.getElementById("m-factura").value,
    motivo: document.getElementById("m-motivo").value,
  };
  if (tipo === "perdida") {
    const valorProveedor = document.getElementById("m-proveedor-merma").value;
    if (!valorProveedor) {
      toast("Selecciona el proveedor del cual viene este producto", true);
      return;
    }
    payload.proveedor_id = valorProveedor === "__sin_proveedor__" ? null : parseInt(valorProveedor, 10);
  }
  if (tipo === "salida") {
    const valorProveedor = document.getElementById("m-proveedor-salida").value;
    if (!valorProveedor) {
      toast("Selecciona de qué proveedor están saliendo estas unidades", true);
      return;
    }
    payload.proveedor_id = valorProveedor === "__sin_proveedor__" ? null : parseInt(valorProveedor, 10);

    if (categoria === "Consumo interno" || categoria === "Reserva") {
      const ordenTexto = document.getElementById("m-orden-id").value.trim();
      if (!ordenTexto && categoria === "Consumo interno") {
        toast("Indica el número de la orden de trabajo a la que se le está descargando este repuesto", true);
        return;
      }
      if (ordenTexto) payload.orden_id = ordenTexto;
    }
  }
  try {
    await api(`/api/productos/${id}/movimiento`, { method: "POST", body: JSON.stringify(payload) });
    toast("Movimiento registrado");
    document.getElementById("form-movimiento").reset();
    document.getElementById("m-costo-wrap").style.display = "flex";
    document.getElementById("m-factura-wrap").style.display = "flex";
    document.getElementById("m-categoria-wrap").style.display = "flex";
    document.getElementById("m-proveedor-merma-wrap").style.display = "none";
    document.getElementById("m-proveedor-salida-wrap").style.display = "none";
    document.getElementById("m-orden-wrap").style.display = "none";
    document.getElementById("m-cantidad-label").textContent = "Cantidad";
    document.getElementById("m-tipo").value = "entrada";
    poblarSelectCategoriaMovimiento(CATEGORIAS_ENTRADA, "Compra");
    resetearCampoFactura();
    resetearCampoOrden();
    document.getElementById("m-usuario").value = state.sesion.nombre || state.sesion.username || "";
    await Promise.all([cargarProductos(), cargarResumen()]);
    await abrirDetalle(id);
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-editar-producto").addEventListener("click", () => {
  cerrarModal("modal-detalle");
  abrirEdicion(state.productoActivo);
});

document.getElementById("btn-eliminar-producto").addEventListener("click", async () => {
  const p = state.productoActivo;
  if (!p) return;
  const identificador = p.codigo_interno || `#${p.id_sistema}`;
  try {
    const eliminado = await eliminarConReintento(
      `/api/productos/${p.id}`,
      `¿Eliminar "${p.nombre}" (${identificador})? Esta acción no se puede deshacer.`
    );
    if (!eliminado) return;
    toast("Producto eliminado");
    cerrarModal("modal-detalle");
    await Promise.all([cargarProductos(), cargarResumen()]);
  } catch (e) {
    toast(e.message, true);
  }
});


// ---------------------------------------------------------------------------
// Proveedores
// ---------------------------------------------------------------------------

async function cargarProveedores() {
  const tbody = document.getElementById("tbody-proveedores");
  try {
    const data = await api("/api/proveedores");
    state.proveedores = data.proveedores;
    renderTablaProveedores();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Error al cargar proveedores: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function renderTablaProveedores() {
  const tbody = document.getElementById("tbody-proveedores");
  if (state.proveedores.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Aún no hay proveedores registrados. Usa "+ Nuevo proveedor" para empezar.</td></tr>`;
    return;
  }

  const termino = document.getElementById("buscar-proveedores").value.trim();
  const filtrados = !termino ? state.proveedores : state.proveedores.filter(p => {
    return coincideBusqueda(termino, p.nombre, p.rut, p.telefono, p.email);
  });

  if (filtrados.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Ningún proveedor coincide con "${escapeHtml(document.getElementById("buscar-proveedores").value.trim())}".</td></tr>`;
    return;
  }

  tbody.innerHTML = filtrados.map(p => `
    <tr data-id="${p.id}">
      <td>${escapeHtml(p.nombre)}</td>
      <td class="mono">${escapeHtml(p.rut) || "—"}</td>
      <td>${escapeHtml(p.telefono) || "—"}</td>
      <td>${escapeHtml(p.email) || "—"}</td>
      <td class="num mono">${p.cantidad_facturas}</td>
      <td class="mono">${fmtFechaSolo(p.ultima_compra)}</td>
      <td></td>
    </tr>
  `).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => abrirEdicionProveedor(parseInt(tr.dataset.id, 10)));
  });
}

document.getElementById("buscar-proveedores").addEventListener("input", () => renderTablaProveedores());

document.getElementById("btn-nuevo-proveedor").addEventListener("click", () => {
  document.getElementById("form-proveedor").reset();
  document.getElementById("pv-id").value = "";
  document.getElementById("modal-proveedor-titulo").textContent = "Nuevo proveedor";
  document.getElementById("pv-acciones-editar").style.display = "none";
  abrirModal("modal-proveedor");
});

function abrirEdicionProveedor(id) {
  const p = state.proveedores.find(x => x.id === id);
  if (!p) return;
  document.getElementById("pv-id").value = p.id;
  document.getElementById("pv-nombre").value = p.nombre || "";
  document.getElementById("pv-rut").value = p.rut || "";
  document.getElementById("pv-telefono").value = p.telefono || "";
  document.getElementById("pv-email").value = p.email || "";
  document.getElementById("pv-direccion").value = p.direccion || "";
  document.getElementById("modal-proveedor-titulo").textContent = "Editar proveedor";
  document.getElementById("pv-acciones-editar").style.display = "flex";
  abrirModal("modal-proveedor");
}

document.getElementById("form-proveedor").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("pv-id").value;
  const payload = {
    nombre: document.getElementById("pv-nombre").value,
    rut: document.getElementById("pv-rut").value,
    telefono: document.getElementById("pv-telefono").value,
    email: document.getElementById("pv-email").value,
    direccion: document.getElementById("pv-direccion").value,
  };
  try {
    if (id) {
      await api(`/api/proveedores/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Proveedor actualizado");
    } else {
      await api("/api/proveedores", { method: "POST", body: JSON.stringify(payload) });
      toast("Proveedor creado");
    }
    cerrarModal("modal-proveedor");
    await cargarProveedores();
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-eliminar-proveedor").addEventListener("click", async () => {
  const id = document.getElementById("pv-id").value;
  const p = state.proveedores.find(x => x.id === parseInt(id, 10));
  if (!id || !p) return;
  try {
    const eliminado = await eliminarConReintento(
      `/api/proveedores/${id}`,
      `¿Eliminar el proveedor "${p.nombre}"?`
    );
    if (!eliminado) return;
    toast("Proveedor eliminado");
    cerrarModal("modal-proveedor");
    await cargarProveedores();
  } catch (e) {
    toast(e.message, true);
  }
});


// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

async function cargarDashboard() {
  try {
    const d = await api("/api/dashboard");

    document.getElementById("s-total-productos").textContent = d.resumen.total_productos;
    document.getElementById("s-total-unidades").textContent = d.resumen.total_unidades;
    document.getElementById("s-valor-total").textContent = fmtMoney(d.resumen.valor_total);
    document.getElementById("s-criticos").textContent = d.resumen.stock_critico;
    document.getElementById("s-merma").textContent = d.merma.unidades;

    const contCritico = document.getElementById("dash-stock-critico");
    contCritico.innerHTML = d.stock_critico.length
      ? d.stock_critico.map(p => `
        <div class="dash-item">
          <span class="dash-item-nombre">#${p.id_sistema} — ${escapeHtml(p.nombre)}</span>
          <span class="dash-item-valor">${p.stock_disponible} / mín. ${p.stock_minimo}</span>
        </div>`).join("")
      : `<p class="vacio">No hay productos en estado crítico. 👍</p>`;

    const contMov = document.getElementById("dash-ultimos-movimientos");
    contMov.innerHTML = d.ultimos_movimientos.length
      ? d.ultimos_movimientos.map(m => `
        <div class="dash-item">
          <span>
            <span class="mov-tipo mov-tipo--${m.tipo}">${m.tipo}</span>
            <span class="dash-item-nombre">#${m.producto_id_sistema} ${escapeHtml(m.producto_nombre)}</span>
            <div class="dash-item-detalle">${m.cantidad >= 0 ? "+" : ""}${m.cantidad} · ${fmtFecha(m.fecha)}</div>
          </span>
        </div>`).join("")
      : `<p class="vacio">Aún no hay movimientos registrados.</p>`;

    const contCompras = document.getElementById("dash-compras-recientes");
    contCompras.innerHTML = d.compras_recientes.length
      ? d.compras_recientes.map(f => `
        <div class="dash-item">
          <span>
            <span class="dash-item-nombre mono">${escapeHtml(f.numero)}</span>
            <div class="dash-item-detalle">${escapeHtml(f.proveedor_display) || "Sin proveedor"} · ${f.lineas_aplicadas}/${f.total_lineas} ingresado</div>
          </span>
        </div>`).join("")
      : `<p class="vacio">Aún no hay facturas registradas.</p>`;

    const contRotacion = document.getElementById("dash-mas-movidos");
    contRotacion.innerHTML = d.mas_movidos.length
      ? d.mas_movidos.map(p => `
        <div class="dash-item">
          <span class="dash-item-nombre">#${p.id_sistema} — ${escapeHtml(p.nombre)}</span>
          <span class="dash-item-valor">${p.total_salida} salidas</span>
        </div>`).join("")
      : `<p class="vacio">Todavía no hay suficientes salidas para calcular rotación.</p>`;

    const contSinMov = document.getElementById("dash-sin-movimiento");
    contSinMov.innerHTML = d.sin_movimiento.length
      ? d.sin_movimiento.map(p => `
        <div class="dash-item">
          <span class="dash-item-nombre">#${p.id_sistema} — ${escapeHtml(p.nombre)}</span>
        </div>`).join("")
      : `<p class="vacio">Todos los productos tienen movimientos registrados.</p>`;

    document.getElementById("dash-bodega-resumen").innerHTML = `
      <div class="dash-bodega-stat"><span class="valor">${d.bodega.total_ubicaciones}</span><span class="label">Ubicaciones</span></div>
      <div class="dash-bodega-stat"><span class="valor">${d.bodega.ubicaciones_ocupadas}</span><span class="label">Con stock</span></div>
      <div class="dash-bodega-stat"><span class="valor">${d.bodega.total_proveedores}</span><span class="label">Proveedores</span></div>
      <div class="dash-bodega-stat"><span class="valor">${d.bodega.facturas_pendientes}</span><span class="label">Facturas pendientes</span></div>
    `;

    cargarGraficosDashboardLogistica();
    cargarRepuestosTipicosModelosDashboard();
  } catch (e) {
    toast(e.message, true);
  }
}

// Resumen chico (top 3 modelos) de "Repuestos típicos por modelo" para el
// Dashboard — el detalle completo está en la pestaña Planificación
// (cargarRepuestosTipicosModelos), este solo da un vistazo rápido con link
// directo. Ver ese endpoint (app.py: logistica_repuestos_tipicos_modelos)
// para la explicación completa de cómo se arma la sugerencia.
async function cargarRepuestosTipicosModelosDashboard() {
  const cont = document.getElementById("dash-repuestos-tipicos-modelos");
  try {
    const d = await api("/api/logistica/repuestos-tipicos-modelos");
    if (!d.modelos.length) {
      cont.innerHTML = `<p class="vacio">Ningún modelo frecuente tiene OTs abiertas ahora mismo.</p>`;
      return;
    }
    cont.innerHTML = d.modelos.slice(0, 3).map(m => `
        <div class="dash-item">
          <span class="dash-item-nombre">${escapeHtml(m.marca)} ${escapeHtml(m.modelo)}</span>
          <span class="dash-item-valor">${m.ots_abiertas.length} OT(s) abierta(s)</span>
        </div>`).join("")
      + `<p class="bodega-intro" style="margin-top:8px">${d.modelos.length} modelo(s) frecuente(s) con OTs abiertas. <a href="/logistica#planificacion">Ver detalle completo →</a></p>`;
  } catch (e) {
    cont.innerHTML = `<p class="vacio">No se pudo cargar.</p>`;
  }
}

async function cargarGraficosDashboardLogistica() {
  try {
    const data = await api("/api/logistica/dashboard/graficos");

    document.getElementById("log-chart-movimientos").innerHTML = buildLineChartDoble(
      data.movimientos_mensual.map(d => ({ mes: d.mes, a: d.entradas, b: d.salidas })),
      "Entradas", "Salidas"
    );

    document.getElementById("log-chart-valor-categoria").innerHTML = buildBarChartH(
      data.valor_por_categoria.map(d => ({ label: d.categoria, valor: d.valor, extra: chartFmtMoney(d.valor) })),
      { padL: 140 }
    );
  } catch (e) {
    console.error("Error cargando los gráficos de Logística:", e);
  }
}

// ---------------------------------------------------------------------------
// Rotación (Módulo 8)
// ---------------------------------------------------------------------------

const TENDENCIA_BADGE = {
  subiendo: { icono: "▲", texto: "subiendo", clase: "tendencia--subiendo" },
  bajando: { icono: "▼", texto: "bajando", clase: "tendencia--bajando" },
  estable: { icono: "■", texto: "estable", clase: "tendencia--estable" },
};

const ROT_COLORES = ["#F5A623", "#4FA3E3", "#7ED08C", "#E36F9D", "#B98CF5"];

function fmtSemana(clave) {
  // "2026-27" (año-semana ISO simplificada) -> "Sem 27"
  const partes = String(clave).split("-");
  return partes.length === 2 ? `Sem ${parseInt(partes[1], 10)}` : clave;
}

async function cargarRotacion() {
  const dias = document.getElementById("rotacion-periodo").value;
  try {
    const d = await api(`/api/rotacion?dias=${dias}`);

    const listaProducto = (items, cont, vacioMsg, extra) => {
      cont.innerHTML = items.length
        ? items.map(p => `
          <div class="dash-item">
            <span class="dash-item-nombre">#${p.id_sistema} — ${escapeHtml(p.nombre)}</span>
            <span class="dash-item-valor">${extra(p)}</span>
          </div>`).join("")
        : `<p class="vacio">${vacioMsg}</p>`;
    };

    listaProducto(
      d.mas_utilizados, document.getElementById("rot-mas-utilizados"),
      "Todavía no hay suficientes salidas en este período.",
      p => {
        const b = TENDENCIA_BADGE[p.tendencia] || TENDENCIA_BADGE.estable;
        return `${p.total_salida_periodo} salidas · ${p.n_movimientos} mov.
          <span class="tendencia-badge ${b.clase}" title="Comparado con la primera mitad del período">${b.icono} ${b.texto}</span>`;
      }
    );

    listaProducto(
      d.baja_rotacion, document.getElementById("rot-baja-rotacion"),
      "No hay productos con baja rotación en este período.",
      p => `${p.total_salida_periodo} salidas`
    );

    listaProducto(
      d.inmovilizados, document.getElementById("rot-inmovilizados"),
      "Todos los productos con stock han tenido salidas en este período.",
      p => `${p.stock_total} en stock`
    );

    renderTendenciaChart(d.tendencia);
  } catch (e) {
    toast(e.message, true);
  }
}

async function cargarPlanificacion() {
  const dias = document.getElementById("planificacion-periodo").value;
  const tbodyReponer = document.getElementById("tbody-planificacion-reponer");
  const tbodyExceso = document.getElementById("tbody-planificacion-exceso");
  tbodyReponer.innerHTML = `<tr><td colspan="7" class="vacio">Cargando…</td></tr>`;
  tbodyExceso.innerHTML = `<tr><td colspan="5" class="vacio">Cargando…</td></tr>`;
  try {
    const d = await api(`/api/planificacion?dias=${dias}`);

    tbodyReponer.innerHTML = d.reponer.length
      ? d.reponer.map(p => {
          const nombre = `#${p.id_sistema} — ${escapeHtml(nombreCortoProducto(p))}`;
          const urgenciaClase = p.urgencia === "critico" ? "badge-urgencia badge-urgencia--critico" : "badge-urgencia badge-urgencia--proximo";
          const urgenciaTexto = p.urgencia === "critico" ? "Ya bajo el mínimo" : "Próximo a agotarse";
          let seAgota;
          if (p.dias_para_minimo === null) {
            seAgota = `<span class="${urgenciaClase}">${urgenciaTexto}</span>`;
          } else if (p.dias_para_minimo <= 0) {
            seAgota = `<span class="${urgenciaClase}">Ya bajo el mínimo</span>`;
          } else {
            seAgota = `<span class="${urgenciaClase}">${urgenciaTexto}</span><div class="dash-item-detalle">en ~${p.dias_para_minimo} día(s) (${fmtFechaSolo(p.fecha_estimada_minimo)})</div>`;
          }
          const proveedores = (p.proveedores && p.proveedores.length)
            ? p.proveedores.map(pr => escapeHtml(pr.nombre)).join(", ")
            : `<span class="vacio-inline">Sin proveedor conocido — revisar facturas</span>`;
          const det = p.stock_minimo_detalle;
          const tituloMinimo = det
            ? `Calculado solo: ${det.consumo_diario_promedio}/día × ${det.lead_time_dias} día(s) de entrega${det.lead_time_es_estimado_por_defecto ? " (estimación por defecto, poco historial)" : " (estimado del historial real)"} + ${det.stock_seguridad} de resguardo`
            : "Todavía sin calcular";
          return `
            <tr>
              <td>${nombre}</td>
              <td class="num">${p.stock_disponible}</td>
              <td class="num" title="${escapeHtml(tituloMinimo)}">${p.stock_minimo}</td>
              <td class="num">${p.promedio_semanal}</td>
              <td>${seAgota}</td>
              <td class="num"><strong>${p.cantidad_sugerida}</strong></td>
              <td>${proveedores}</td>
            </tr>`;
        }).join("")
      : `<tr><td colspan="7" class="vacio">No hay productos que necesiten reposición por ahora.</td></tr>`;

    tbodyExceso.innerHTML = d.exceso.length
      ? d.exceso.map(p => {
          const nombre = `#${p.id_sistema} — ${escapeHtml(nombreCortoProducto(p))}`;
          const valorInmovilizado = p.stock_disponible * (p.costo_compra || 0);
          const consumo = p.promedio_semanal > 0
            ? `${p.promedio_semanal} salidas/semana en promedio`
            : `Sin salidas en los últimos ${p.semanas_sin_consumo} semana(s)`;
          return `
            <tr>
              <td>${nombre}</td>
              <td class="num">${p.stock_disponible}</td>
              <td class="num">${p.stock_minimo}</td>
              <td class="num">${fmtMoney(valorInmovilizado)}</td>
              <td>${consumo}</td>
            </tr>`;
        }).join("")
      : `<tr><td colspan="5" class="vacio">No se detectó exceso de stock en el período elegido.</td></tr>`;
  } catch (e) {
    toast(e.message, true);
  }
}

// Repuestos típicos por modelo, para las OTs ABIERTAS ahora mismo (ver
// /api/logistica/repuestos-tipicos-modelos en app.py). Por cada uno de los
// 10 modelos con más historial que tenga algún auto abierto hoy, cruza qué
// repuestos aparecen seguido en las OTs YA CERRADAS de ese modelo (menos
// los genéricos tipo aceite/bencina) con el stock actual, para armar un
// checklist "esto suele hacerle falta a este modelo, revisa si tienes
// stock". Solo informativo, no toca stock ni se mezcla con el cálculo
// automático de Planificación (que sigue confiando 100% en movimientos
// reales).
async function cargarRepuestosTipicosModelos() {
  const cont = document.getElementById("repuestos-tipicos-modelos-cont");
  cont.innerHTML = `<p class="vacio">Cargando…</p>`;
  try {
    const d = await api("/api/logistica/repuestos-tipicos-modelos");
    if (!d.modelos.length) {
      cont.innerHTML = `<p class="vacio">Ninguno de los 10 modelos con más historial tiene OTs abiertas ahora mismo.</p>`;
      return;
    }
    cont.innerHTML = d.modelos.map(m => {
      const ots = m.ots_abiertas.map(o => `OT #${o.orden_id}${o.cliente_nombre ? ` (${escapeHtml(o.cliente_nombre)})` : ""}`).join(", ");
      const filasRepuestos = m.repuestos_tipicos.length
        ? m.repuestos_tipicos.map(r => {
            let estado;
            if (!r.producto_sugerido) {
              estado = `<span class="vacio-inline">Sin coincidencia en catálogo</span>`;
            } else if (r.producto_sugerido.stock_disponible <= r.producto_sugerido.stock_minimo) {
              estado = `<span class="badge-urgencia badge-urgencia--critico">Stock bajo: ${r.producto_sugerido.stock_disponible} / mín. ${r.producto_sugerido.stock_minimo}</span>`;
            } else {
              estado = `<span class="badge-urgencia badge-urgencia--proximo">En stock: ${r.producto_sugerido.stock_disponible}</span>`;
            }
            const nombreProducto = r.producto_sugerido ? ` — ${escapeHtml(r.producto_sugerido.nombre)} (${escapeHtml(r.producto_sugerido.codigo_interno)})` : "";
            return `
              <tr>
                <td>${escapeHtml(r.descripcion)}${nombreProducto}</td>
                <td class="num">${r.en_ots_cerradas}</td>
                <td class="num">${fmtMoney(r.costo_promedio)}</td>
                <td>${estado}</td>
              </tr>`;
          }).join("")
        : `<tr><td colspan="4" class="vacio">Sin suficiente historial de repuestos específicos para este modelo.</td></tr>`;
      return `
        <div class="dash-panel" style="margin-bottom:16px">
          <h4 style="margin:0 0 6px">${escapeHtml(m.marca)} ${escapeHtml(m.modelo)} <span class="badge-origen">${m.total_autos_historico} autos en el historial</span></h4>
          <p class="bodega-intro">OT(s) abierta(s) ahora: ${ots}</p>
          <div class="tabla-wrap">
            <table class="tabla">
              <thead>
                <tr>
                  <th>Repuesto típico</th>
                  <th class="num">En cuántas OTs cerradas</th>
                  <th class="num">Costo promedio</th>
                  <th>Stock actual</th>
                </tr>
              </thead>
              <tbody>${filasRepuestos}</tbody>
            </table>
          </div>
        </div>`;
    }).join("");
  } catch (e) {
    toast(e.message, true);
  }
}

function nombreCortoProducto(p) {
  // Para el gráfico de rotación no hace falta el ID: basta modelo +
  // detalle (que es justo lo que identifica al producto para el taller).
  const partes = [p.categoria, p.modelo].filter(Boolean);
  return partes.length ? partes.join(" — ") : p.nombre;
}

function renderTendenciaChart(tendencia) {
  const contChart = document.getElementById("rot-tendencia-chart");
  const contLeyenda = document.getElementById("rot-tendencia-leyenda");
  const semanas = tendencia.semanas || [];
  const total = tendencia.total || [];
  const productos = tendencia.productos || [];

  const hayDatos = total.some(v => v > 0) || productos.some(p => p.valores.some(v => v > 0));
  if (!semanas.length || !hayDatos) {
    contChart.innerHTML = `<p class="vacio">Todavía no hay suficientes salidas para ver una tendencia.</p>`;
    contLeyenda.innerHTML = "";
    return;
  }

  // Series a dibujar: el total general (línea gris de fondo) + hasta 5
  // productos con color propio.
  const totalPeriodo = total.reduce((a, b) => a + b, 0);
  const series = [
    { nombre: "Total general", valores: total, color: "#8A93A6", ancho: 3, esTotal: true, totalPeriodo },
    ...productos.map((p, i) => ({
      nombre: nombreCortoProducto(p),
      valores: p.valores,
      color: ROT_COLORES[i % ROT_COLORES.length],
      ancho: 2,
      esTotal: false,
      totalPeriodo: p.total_periodo,
    })),
  ];

  const maxValor = Math.max(1, ...series.flatMap(s => s.valores));
  const W = 900, H = 260;
  const padL = 36, padR = 12, padT = 14, padB = 28;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const n = semanas.length;
  const x = i => padL + (n === 1 ? innerW / 2 : (innerW * i) / (n - 1));
  const y = v => padT + innerH - (v / maxValor) * innerH;

  const lineas = series.map(s => {
    const puntos = s.valores.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const puntosCirculo = s.esTotal ? "" : s.valores.map((v, i) =>
      `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.6" fill="${s.color}"></circle>`
    ).join("");
    return `<polyline points="${puntos}" fill="none" stroke="${s.color}" stroke-width="${s.ancho}" stroke-linejoin="round" stroke-linecap="round" opacity="${s.esTotal ? 0.55 : 0.95}" />${puntosCirculo}`;
  }).join("");

  // Etiquetas del eje X: si hay muchas semanas, se muestran salteadas para no amontonarse.
  const paso = Math.max(1, Math.ceil(n / 10));
  const etiquetas = semanas.map((sem, i) => (i % paso !== 0 && i !== n - 1) ? "" : `
    <text x="${x(i).toFixed(1)}" y="${H - 8}" font-size="10" fill="var(--text-muted)" text-anchor="middle">${escapeHtml(fmtSemana(sem))}</text>
  `).join("");

  // Líneas guía horizontales (0, mitad, máximo).
  const guias = [0, 0.5, 1].map(f => {
    const val = Math.round(maxValor * f);
    const yy = y(val).toFixed(1);
    return `
      <line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="var(--line)" stroke-width="1" />
      <text x="${padL - 6}" y="${Number(yy) + 3}" font-size="10" fill="var(--text-muted)" text-anchor="end">${val}</text>
    `;
  }).join("");

  contChart.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="rot-svg" id="rot-svg-el">
      ${guias}
      ${lineas}
      <line id="rot-guia-vertical" x1="0" y1="${padT}" x2="0" y2="${H - padB}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="3,3" opacity="0" />
      ${series.map((s, si) => `<circle id="rot-punto-hover-${si}" r="4" fill="${s.color}" stroke="#fff" stroke-width="1.5" opacity="0" />`).join("")}
      ${etiquetas}
      <rect x="${padL}" y="${padT}" width="${innerW}" height="${innerH}" fill="transparent" id="rot-hitarea" style="cursor:crosshair" />
    </svg>
    <div class="rot-tooltip" id="rot-tooltip"></div>
  `;

  contLeyenda.innerHTML = series.map(s => `
    <span class="rot-leyenda-item">
      <span class="rot-leyenda-color" style="background:${s.color}"></span>
      ${escapeHtml(s.nombre)}
      <span class="rot-leyenda-cantidad">· ${s.totalPeriodo} un.</span>
    </span>
  `).join("");

  // ---- Interactividad: al pasar el mouse por el gráfico, se muestra una
  // línea guía vertical y un tooltip con el detalle de cada serie en esa
  // semana exacta (no solo el total pasivo) — para poder comparar de un
  // vistazo qué producto se movió más esa semana en particular.
  const svgEl = document.getElementById("rot-svg-el");
  const hitArea = document.getElementById("rot-hitarea");
  const guiaVertical = document.getElementById("rot-guia-vertical");
  const tooltip = document.getElementById("rot-tooltip");
  const puntosHover = series.map((_, si) => document.getElementById(`rot-punto-hover-${si}`));

  function indiceDesdeEvento(evt) {
    const pt = svgEl.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    const svgP = pt.matrixTransform(svgEl.getScreenCTM().inverse());
    const stepX = n === 1 ? innerW : innerW / (n - 1);
    const i = Math.round((svgP.x - padL) / stepX);
    return Math.max(0, Math.min(n - 1, i));
  }

  function mostrarTooltip(evt) {
    const i = indiceDesdeEvento(evt);
    guiaVertical.setAttribute("x1", x(i).toFixed(1));
    guiaVertical.setAttribute("x2", x(i).toFixed(1));
    guiaVertical.setAttribute("opacity", "1");

    series.forEach((s, si) => {
      puntosHover[si].setAttribute("cx", x(i).toFixed(1));
      puntosHover[si].setAttribute("cy", y(s.valores[i]).toFixed(1));
      puntosHover[si].setAttribute("opacity", "1");
    });

    const filas = series
      .slice()
      .map((s, si) => ({ s, valor: s.valores[i], color: s.color }))
      .sort((a, b) => b.valor - a.valor)
      .map(({ s, valor, color }) => `
        <div class="rot-tooltip-fila">
          <span class="rot-tooltip-punto" style="background:${color}"></span>
          <span>${escapeHtml(s.nombre)}</span>
          <span class="rot-tooltip-valor">${valor}</span>
        </div>`)
      .join("");

    tooltip.innerHTML = `<div class="rot-tooltip-titulo">Semana: ${escapeHtml(fmtSemana(semanas[i]))}</div>${filas}`;
    tooltip.classList.add("visible");

    // Posiciona el tooltip cerca del cursor, dentro del contenedor.
    const contRect = contChart.getBoundingClientRect();
    let left = evt.clientX - contRect.left + 14;
    let top = evt.clientY - contRect.top - 10;
    const anchoTooltip = 180;
    if (left + anchoTooltip > contRect.width) left = evt.clientX - contRect.left - anchoTooltip - 14;
    tooltip.style.left = `${Math.max(0, left)}px`;
    tooltip.style.top = `${Math.max(0, top)}px`;
  }

  function ocultarTooltip() {
    guiaVertical.setAttribute("opacity", "0");
    puntosHover.forEach(p => p.setAttribute("opacity", "0"));
    tooltip.classList.remove("visible");
  }

  hitArea.addEventListener("mousemove", mostrarTooltip);
  hitArea.addEventListener("mouseleave", ocultarTooltip);
}

// ---------------------------------------------------------------------------
// Sesión
// ---------------------------------------------------------------------------

async function cargarSesion() {
  try {
    const s = await api("/api/sesion");
    state.sesion = s;
    state.rol = s.rol;
    state.esAdmin = s.rol === "admin";
    // Desde que un usuario puede tener más de un módulo asignado a la vez
    // (ver migración 17), esto se decide mirando la lista 'modulos' (ver
    // /api/sesion), no comparando 'rol' contra un único valor -- alguien
    // con ["bodega", "finanzas"] sigue teniendo rol="bodega" (el primer
    // módulo elegido, solo representativo) pero SÍ tiene que ver lo de
    // finanzas acá igual.
    const modulos = s.modulos || [];
    state.puedeFinanzas = state.esAdmin || modulos.includes("finanzas");
    state.puedeBodega = state.esAdmin || modulos.includes("bodega");
    document.getElementById("sesion-usuario").textContent = s.nombre ? `👤 ${s.nombre}` : "";
    document.getElementById("m-usuario").value = s.nombre || s.username || "";
    aplicarVisibilidadPorRol();
  } catch (e) {
    console.error(e);
  }
}

// El backend siempre rechaza estas acciones si el rol no corresponde (ver
// @requiere_rol en app.py) — esto solo evita mostrar botones que de todas
// formas van a fallar. "Importar Excel" queda para finanzas; la pestaña
// "Usuarios" queda para admin. Registrar/aprobar facturas de compra ahora
// vive por completo en Finanzas → Cuentas por Pagar (ver finanzas.js).
function aplicarVisibilidadPorRol() {
  document.querySelector('.tab[data-vista="usuarios"]').style.display = state.esAdmin ? "" : "none";
  // Reaplica el toggle de "Importar Excel" (depende también de qué pestaña
  // está activa, no solo del rol) por si cargarSesion() resuelve después
  // de que ya se dibujó la pestaña actual.
  const vistaActiva = document.querySelector(".tab.activo")?.dataset.vista;
  document.getElementById("btn-importar-excel").style.display =
    vistaActiva === "productos" && state.puedeFinanzas ? "inline-block" : "none";
}

// ---------------------------------------------------------------------------
// Usuarios
// ---------------------------------------------------------------------------

async function cargarUsuarios() {
  const tbody = document.getElementById("tbody-usuarios");
  try {
    const data = await api("/api/usuarios");
    state.usuarios = data.usuarios;
    renderTablaUsuarios();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="vacio">Error al cargar usuarios: ${escapeHtml(e.message)}</td></tr>`;
  }
}

function renderTablaUsuarios() {
  const tbody = document.getElementById("tbody-usuarios");
  tbody.innerHTML = state.usuarios.map(u => `
    <tr data-id="${u.id}">
      <td class="mono">${escapeHtml(u.username)}</td>
      <td>${escapeHtml(u.nombre) || "—"}</td>
      <td class="mono">${fmtFecha(u.creado_en)}</td>
      <td></td>
    </tr>
  `).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => abrirEdicionUsuario(parseInt(tr.dataset.id, 10)));
  });
}

document.getElementById("btn-nuevo-usuario").addEventListener("click", () => {
  document.getElementById("form-usuario").reset();
  document.getElementById("us-id").value = "";
  document.getElementById("us-username").disabled = false;
  document.getElementById("us-password-label").textContent = "Contraseña *";
  document.getElementById("us-password").required = true;
  document.getElementById("modal-usuario-titulo").textContent = "Nuevo usuario";
  document.getElementById("us-acciones-editar").style.display = "none";
  abrirModal("modal-usuario");
});

function abrirEdicionUsuario(id) {
  const u = state.usuarios.find(x => x.id === id);
  if (!u) return;
  document.getElementById("us-id").value = u.id;
  document.getElementById("us-username").value = u.username;
  document.getElementById("us-username").disabled = true;
  document.getElementById("us-nombre").value = u.nombre || "";
  document.getElementById("us-password").value = "";
  document.getElementById("us-password").required = false;
  document.getElementById("us-password-label").textContent = "Nueva contraseña (déjalo vacío para no cambiarla)";
  document.getElementById("modal-usuario-titulo").textContent = "Editar usuario";
  document.getElementById("us-acciones-editar").style.display = "flex";
  abrirModal("modal-usuario");
}

document.getElementById("form-usuario").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("us-id").value;
  const payload = {
    username: document.getElementById("us-username").value,
    nombre: document.getElementById("us-nombre").value,
    password: document.getElementById("us-password").value,
  };
  try {
    if (id) {
      await api(`/api/usuarios/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Usuario actualizado");
    } else {
      await api("/api/usuarios", { method: "POST", body: JSON.stringify(payload) });
      toast("Usuario creado");
    }
    cerrarModal("modal-usuario");
    await Promise.all([cargarUsuarios(), cargarSesion()]);
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("btn-eliminar-usuario").addEventListener("click", async () => {
  const id = document.getElementById("us-id").value;
  const u = state.usuarios.find(x => x.id === parseInt(id, 10));
  if (!id || !u) return;
  if (!confirm(`¿Eliminar el usuario "${u.username}"?`)) return;
  try {
    await api(`/api/usuarios/${id}`, { method: "DELETE" });
    toast("Usuario eliminado");
    cerrarModal("modal-usuario");
    await cargarUsuarios();
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Mermas (cuenta separada de pérdidas)
// ---------------------------------------------------------------------------

const MESES_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"];

function fmtMes(clave) {
  // "2026-07" -> "Julio 2026"
  const [anio, mes] = String(clave).split("-").map(Number);
  if (!anio || !mes) return clave;
  return `${MESES_ES[mes - 1]} ${anio}`;
}

// Modal genérico de "cuántas unidades" para Descartar/Revertir una merma —
// reemplaza el prompt() nativo que se usaba antes (no daba contexto, y
// cualquier click accidental fuera de él perdía lo escrito).
let mermaCantidadCallback = null;

function abrirModalMermaCantidad({ titulo, info, max, accionTexto, callback }) {
  document.getElementById("mm-titulo").textContent = titulo;
  document.getElementById("mm-info").textContent = info;
  const input = document.getElementById("mm-cantidad");
  input.max = max;
  input.value = max;
  document.getElementById("mm-btn-confirmar").textContent = accionTexto;
  mermaCantidadCallback = callback;
  abrirModal("modal-merma-cantidad");
}

document.getElementById("form-merma-cantidad").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("mm-cantidad");
  const cantidad = parseInt(input.value, 10);
  const max = parseInt(input.max, 10);
  if (!cantidad || cantidad <= 0) {
    toast("Ingresa una cantidad válida", true);
    return;
  }
  if (max && cantidad > max) {
    toast(`No puede ser más de ${max}`, true);
    return;
  }
  if (mermaCantidadCallback) {
    await mermaCantidadCallback(cantidad);
  }
});

async function cargarMermas() {
  const tbody = document.getElementById("tbody-mermas");
  const mes = document.getElementById("mermas-filtro-mes").value;
  try {
    const params = mes ? `?mes=${encodeURIComponent(mes)}` : "";
    const data = await api("/api/mermas" + params);

    // Rellena el selector de meses una sola vez por carga (conserva la selección actual).
    const selectMes = document.getElementById("mermas-filtro-mes");
    if (selectMes.dataset.cargado !== "1") {
      selectMes.innerHTML = '<option value="">Actual (pendiente de revisión)</option>' +
        data.meses_disponibles.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(fmtMes(m))}</option>`).join("");
      selectMes.value = mes;
      selectMes.dataset.cargado = "1";
    }

    const esMes = data.modo === "mes";
    document.getElementById("mermas-intro").textContent = esMes
      ? `Historial de pérdidas registradas en ${fmtMes(data.mes)} (aunque ya se hayan descartado después).`
      : `Productos marcados como "pérdida" — quedan aquí guardados para revisión antes de descartarlos definitivamente.`;
    document.getElementById("mermas-label-unidades").textContent = esMes ? "Unidades perdidas el mes" : "Unidades en merma";
    document.getElementById("mermas-label-valor").textContent = esMes ? "Valor perdido el mes" : "Valor en merma";

    document.getElementById("mermas-total-unidades").textContent = data.total_unidades;
    document.getElementById("mermas-total-valor").textContent = fmtMoney(data.total_valor);

    // En modo "mes" no se puede descartar (es historial, no el saldo vivo),
    // así que esa columna de acción se oculta.
    document.querySelectorAll("#thead-mermas th.mermas-col-accion").forEach(th => th.style.display = esMes ? "none" : "");

    if (data.productos.length === 0) {
      const msg = esMes ? `No hubo pérdidas registradas en ${fmtMes(data.mes)}. 👍` : "No hay productos en merma actualmente. 👍";
      tbody.innerHTML = `<tr><td colspan="7" class="vacio">${msg}</td></tr>`;
      return;
    }

    // Cada fila es un bucket producto+proveedor (no un producto solo), así
    // se ve exactamente de qué proveedor son las unidades en merma, sin
    // mezclar ni adivinar.
    tbody.innerHTML = data.productos.map(p => {
      const cantidad = esMes ? p.cantidad_mes : p.cantidad_bucket;
      const valor = esMes ? p.valor_mes : p.valor_bucket;
      const proveedorAttr = p.proveedor_id === null || p.proveedor_id === undefined ? "" : p.proveedor_id;
      const accionDescartar = esMes
        ? ""
        : `<td><button class="btn-link" data-descartar="${p.id}" data-proveedor="${proveedorAttr}" data-max="${cantidad}">Descartar</button></td>`;
      const accionRevertir = esMes
        ? ""
        : `<td><button class="btn-link" data-revertir="${p.id}" data-proveedor="${proveedorAttr}" data-max="${cantidad}">Revertir</button></td>`;
      return `
      <tr>
        <td><button type="button" class="btn-link" data-ver-origen="${p.id}" data-nombre="${escapeHtml(p.nombre)}">#${escapeHtml(p.id_sistema)} ${escapeHtml(p.nombre)}</button></td>
        <td>${escapeHtml(p.categoria) || "—"}</td>
        <td>${escapeHtml(p.proveedor_nombre)}</td>
        <td class="num mono">${cantidad}</td>
        <td class="num mono">${fmtMoney(valor)}</td>
        ${accionDescartar || "<td></td>"}
        ${accionRevertir || "<td></td>"}
      </tr>`;
    }).join("");

    tbody.querySelectorAll("[data-descartar]").forEach(btn => {
      btn.addEventListener("click", () => {
        const max = btn.dataset.max;
        abrirModalMermaCantidad({
          titulo: "Descartar de la merma",
          info: `¿Cuántas unidades quieres descartar definitivamente? (máximo ${max})`,
          max,
          accionTexto: "Descartar",
          callback: async (cantidad) => {
            try {
              await api(`/api/productos/${btn.dataset.descartar}/merma/descartar`, {
                method: "POST",
                body: JSON.stringify({ cantidad, proveedor_id: btn.dataset.proveedor || null }),
              });
              toast("Descartado de la cuenta de merma");
              cerrarModal("modal-merma-cantidad");
              await Promise.all([cargarMermas(), cargarProductos(), cargarResumen()]);
            } catch (e) {
              toast(e.message, true);
            }
          },
        });
      });
    });

    tbody.querySelectorAll("[data-revertir]").forEach(btn => {
      btn.addEventListener("click", () => {
        const max = btn.dataset.max;
        abrirModalMermaCantidad({
          titulo: "Revertir merma",
          info: `¿Cuántas unidades quieres devolver a stock disponible? (máximo ${max})`,
          max,
          accionTexto: "Revertir",
          callback: async (cantidad) => {
            try {
              await api(`/api/productos/${btn.dataset.revertir}/merma/revertir`, {
                method: "POST",
                body: JSON.stringify({ cantidad, proveedor_id: btn.dataset.proveedor || null }),
              });
              toast("Merma revertida: stock devuelto a disponible");
              cerrarModal("modal-merma-cantidad");
              await Promise.all([cargarMermas(), cargarProductos(), cargarResumen()]);
            } catch (e) {
              toast(e.message, true);
            }
          },
        });
      });
    });

    tbody.querySelectorAll("[data-ver-origen]").forEach(btn => {
      btn.addEventListener("click", () => verOrigenMerma(btn.dataset.verOrigen, btn.dataset.nombre));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Error al cargar mermas: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function verOrigenMerma(productoId, nombre) {
  document.getElementById("modal-merma-origen-titulo").textContent = `Origen de: ${nombre}`;
  const tbody = document.getElementById("tbody-merma-origen");
  const resumen = document.getElementById("merma-origen-resumen");
  tbody.innerHTML = `<tr><td colspan="5" class="vacio">Cargando…</td></tr>`;
  resumen.textContent = "";
  abrirModal("modal-merma-origen");
  try {
    const data = await api(`/api/productos/${productoId}/merma/origenes`);
    if (data.origenes.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="vacio">No hay ingresos de stock (con factura) registrados para este producto.</td></tr>`;
      return;
    }
    resumen.textContent = data.n_proveedores_distintos > 1
      ? `Este producto ha sido comprado a ${data.n_proveedores_distintos} proveedores distintos — revisa cuál corresponde a la unidad en merma.`
      : "";
    tbody.innerHTML = data.origenes.map(o => `
      <tr>
        <td>${fmtFecha(o.fecha)}</td>
        <td class="num mono">${o.cantidad}</td>
        <td class="num mono">${fmtMoney(o.costo_unitario)}</td>
        <td>${escapeHtml(o.factura_numero) || "—"}</td>
        <td>${escapeHtml(o.proveedor_nombre) || "Sin proveedor asignado"}</td>
      </tr>`).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Error: ${escapeHtml(e.message)}</td></tr>`;
  }
}

document.getElementById("mermas-filtro-mes").addEventListener("change", cargarMermas);

// ---------------------------------------------------------------------------
// Movimientos (Módulo 5 - bitácora completa con filtros)
// ---------------------------------------------------------------------------

let categoriasMovFiltroCargadas = false;
// Igual criterio que el formulario "Registrar movimiento": qué categorías
// corresponden a cada tipo, para que el filtro no deje elegir combinaciones
// sin sentido (ej. Entrada + Venta).
state.categoriasPorTipoMov = null;

function actualizarOpcionesCategoriaMov() {
  if (!state.categoriasPorTipoMov) return;
  const tipo = document.getElementById("mov-filtro-tipo").value;
  const select = document.getElementById("mov-filtro-categoria");
  const seleccionActual = select.value;

  const opciones = tipo
    ? (state.categoriasPorTipoMov[tipo] || [])
    : Object.values(state.categoriasPorTipoMov).flat();
  const opcionesUnicas = [...new Set(opciones)].sort((a, b) => a.localeCompare(b, "es"));

  select.innerHTML = '<option value="">Todos los motivos</option>' +
    opcionesUnicas.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");

  // Si la categoría que tenías elegida ya no tiene sentido para el tipo
  // nuevo (ej. tenías "Venta" y cambiaste a "Entrada"), se limpia sola.
  select.value = opcionesUnicas.includes(seleccionActual) ? seleccionActual : "";
}

async function cargarMovimientos() {
  const tbody = document.getElementById("tbody-movimientos");
  const params = new URLSearchParams();
  const q = document.getElementById("mov-buscar").value.trim();
  const tipo = document.getElementById("mov-filtro-tipo").value;
  const categoria = document.getElementById("mov-filtro-categoria").value;
  const desde = document.getElementById("mov-filtro-desde").value;
  const hasta = document.getElementById("mov-filtro-hasta").value;
  if (q) params.set("q", q);
  if (tipo) params.set("tipo", tipo);
  if (categoria) params.set("categoria", categoria);
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);

  try {
    const data = await api("/api/movimientos?" + params.toString());

    if (!categoriasMovFiltroCargadas) {
      state.categoriasPorTipoMov = data.categorias_por_tipo;
      actualizarOpcionesCategoriaMov();
      categoriasMovFiltroCargadas = true;
    }

    const avisoLimite = document.getElementById("mov-aviso-limite");
    if (data.truncado) {
      avisoLimite.textContent =
        `Se están mostrando los ${data.limite_filas} movimientos más recientes que calzan con el filtro, de ${data.total_sin_limite} en total. ` +
        `Para ver el resto, achica el rango con los filtros de fecha, producto o tipo.`;
      avisoLimite.style.display = "block";
    } else {
      avisoLimite.style.display = "none";
    }

    if (data.movimientos.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" class="vacio">No se encontraron movimientos con esos filtros.</td></tr>`;
      return;
    }

    tbody.innerHTML = data.movimientos.map(m => `
      <tr>
        <td class="mono">${fmtFecha(m.fecha)}</td>
        <td>#${escapeHtml(m.producto_id_sistema)} ${escapeHtml(m.producto_nombre)}</td>
        <td><span class="mov-tipo mov-tipo--${m.tipo}">${m.tipo}</span></td>
        <td>${escapeHtml(m.categoria) || "—"}</td>
        <td class="num mono">${m.cantidad >= 0 ? "+" : ""}${m.cantidad}</td>
        <td class="num mono">${m.stock_resultante}</td>
        <td class="mono">${escapeHtml(m.factura_numero) || "—"}</td>
        <td>${escapeHtml(m.usuario) || "—"}</td>
        <td>${escapeHtml(m.motivo) || "—"}</td>
      </tr>
    `).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar movimientos: ${escapeHtml(e.message)}</td></tr>`;
    document.getElementById("mov-aviso-limite").style.display = "none";
  }
}

let movBuscarTimeout;
document.getElementById("mov-buscar").addEventListener("input", () => {
  clearTimeout(movBuscarTimeout);
  movBuscarTimeout = setTimeout(cargarMovimientos, 250);
});
document.getElementById("mov-filtro-tipo").addEventListener("change", () => {
  actualizarOpcionesCategoriaMov();
  cargarMovimientos();
});
document.getElementById("mov-filtro-categoria").addEventListener("change", cargarMovimientos);
document.getElementById("mov-filtro-desde").addEventListener("change", cargarMovimientos);
document.getElementById("mov-filtro-hasta").addEventListener("change", cargarMovimientos);
document.getElementById("mov-limpiar-filtros").addEventListener("click", () => {
  document.getElementById("mov-buscar").value = "";
  document.getElementById("mov-filtro-tipo").value = "";
  document.getElementById("mov-filtro-categoria").value = "";
  document.getElementById("mov-filtro-desde").value = "";
  document.getElementById("mov-filtro-hasta").value = "";
  cargarMovimientos();
});

document.getElementById("rotacion-periodo").addEventListener("change", cargarRotacion);
document.getElementById("planificacion-periodo").addEventListener("change", cargarPlanificacion);


document.getElementById("btn-planificacion-recalcular").addEventListener("click", async () => {
  const btn = document.getElementById("btn-planificacion-recalcular");
  btn.disabled = true;
  btn.textContent = "Recalculando…";
  try {
    const data = await api("/api/planificacion/recalcular-todos", { method: "POST" });
    toast(`Mínimos recalculados: ${data.n_productos} producto(s)`);
    await cargarPlanificacion();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Recalcular todos los mínimos ahora";
  }
});

// ---------------------------------------------------------------------------
// Reportes y Exportaciones (Módulo 11)
// ---------------------------------------------------------------------------

document.getElementById("btn-reporte-rotacion").addEventListener("click", () => {
  const dias = document.getElementById("reportes-rotacion-periodo").value;
  window.location.href = `/api/reportes/rotacion?dias=${dias}`;
});

document.getElementById("btn-reporte-rotacion-pdf").addEventListener("click", () => {
  const dias = document.getElementById("reportes-rotacion-periodo").value;
  window.location.href = `/api/reportes/rotacion/pdf?dias=${dias}`;
});

function rangoFechasParams(idDesde, idHasta) {
  const desde = document.getElementById(idDesde).value;
  const hasta = document.getElementById(idHasta).value;
  const params = new URLSearchParams();
  if (desde) params.set("desde", desde);
  if (hasta) params.set("hasta", hasta);
  return params.toString();
}

document.getElementById("btn-reporte-movimientos").addEventListener("click", () => {
  window.location.href = `/api/reportes/movimientos?${rangoFechasParams("reportes-mov-desde", "reportes-mov-hasta")}`;
});
document.getElementById("btn-reporte-movimientos-pdf").addEventListener("click", () => {
  window.location.href = `/api/reportes/movimientos/pdf?${rangoFechasParams("reportes-mov-desde", "reportes-mov-hasta")}`;
});

document.getElementById("btn-reporte-compras").addEventListener("click", () => {
  window.location.href = `/api/reportes/compras?${rangoFechasParams("reportes-compras-desde", "reportes-compras-hasta")}`;
});
document.getElementById("btn-reporte-compras-pdf").addEventListener("click", () => {
  window.location.href = `/api/reportes/compras/pdf?${rangoFechasParams("reportes-compras-desde", "reportes-compras-hasta")}`;
});

// Preview con contadores: antes de descargar cualquier reporte, se muestra
// cuántas filas y qué monto trae — así no hay que abrir el archivo a ciegas
// para saber si va a traer 3 filas o 3.000. Se recalcula al entrar a la
// pestaña y cada vez que cambia algún filtro (período de rotación, rangos
// de fecha de movimientos/compras).
async function cargarResumenReportes() {
  const dias = document.getElementById("reportes-rotacion-periodo").value;
  const params = new URLSearchParams();
  params.set("dias", dias);
  const movDesde = document.getElementById("reportes-mov-desde").value;
  const movHasta = document.getElementById("reportes-mov-hasta").value;
  if (movDesde) params.set("mov_desde", movDesde);
  if (movHasta) params.set("mov_hasta", movHasta);
  const comprasDesde = document.getElementById("reportes-compras-desde").value;
  const comprasHasta = document.getElementById("reportes-compras-hasta").value;
  if (comprasDesde) params.set("compras_desde", comprasDesde);
  if (comprasHasta) params.set("compras_hasta", comprasHasta);

  try {
    const r = await api(`/api/reportes/resumen?${params.toString()}`);
    document.getElementById("rep-contador-inventario").textContent =
      `${r.inventario.productos} producto(s) · valor total ${fmtMoney(r.inventario.valor_total)}`;
    document.getElementById("rep-contador-stock").textContent =
      `${r.stock.criticos} con stock crítico · ${r.stock.sin_movimiento} sin movimiento`;
    document.getElementById("rep-contador-rotacion").textContent =
      `${r.rotacion.mas_utilizados} más utilizados · ${r.rotacion.baja_rotacion} de baja rotación · ${r.rotacion.inmovilizados} inmovilizados`;
    document.getElementById("rep-contador-movimientos").textContent =
      `${r.movimientos.total} movimiento(s) en el rango seleccionado`;
    document.getElementById("rep-contador-compras").textContent =
      `${r.compras.facturas} factura(s) · monto total ${fmtMoney(r.compras.monto_total)}`;
  } catch (e) {
    ["rep-contador-inventario", "rep-contador-stock", "rep-contador-rotacion", "rep-contador-movimientos", "rep-contador-compras"]
      .forEach(id => { document.getElementById(id).textContent = "No se pudo calcular el resumen."; });
  }
}

document.getElementById("reportes-rotacion-periodo").addEventListener("change", cargarResumenReportes);
document.getElementById("reportes-mov-desde").addEventListener("change", cargarResumenReportes);
document.getElementById("reportes-mov-hasta").addEventListener("change", cargarResumenReportes);
document.getElementById("reportes-compras-desde").addEventListener("change", cargarResumenReportes);
document.getElementById("reportes-compras-hasta").addEventListener("change", cargarResumenReportes);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarProductos();
cargarDashboard();
