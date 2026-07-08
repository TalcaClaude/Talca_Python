const state = {
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
  facturas: [],
  facturaActivaId: null,
  facturaEncontrada: null, // resultado del último lookup por número en el form de movimiento
  productosParaFactura: [],
  proveedores: [],
  proveedorActivoId: null,
  sesion: { username: null, nombre: null },
  usuarios: [],
};

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

function fmtMoney(n) {
  return "$" + Math.round(n).toLocaleString("es-CL");
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
    document.getElementById("vista-facturas").style.display = vista === "facturas" ? "block" : "none";
    document.getElementById("vista-proveedores").style.display = vista === "proveedores" ? "block" : "none";
    document.getElementById("vista-movimientos").style.display = vista === "movimientos" ? "block" : "none";
    document.getElementById("vista-mermas").style.display = vista === "mermas" ? "block" : "none";
    document.getElementById("vista-rotacion").style.display = vista === "rotacion" ? "block" : "none";
    document.getElementById("vista-planificacion").style.display = vista === "planificacion" ? "block" : "none";
    document.getElementById("vista-reportes").style.display = vista === "reportes" ? "block" : "none";
    document.getElementById("vista-usuarios").style.display = vista === "usuarios" ? "block" : "none";
    document.getElementById("btn-nuevo").style.display = vista === "productos" ? "inline-block" : "none";
    const titulos = {
      dashboard: "Dashboard",
      productos: "Administración de Inventario",
      bodega: "Organización Física de Bodega",
      facturas: "Facturas de compra",
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
    if (vista === "facturas") cargarFacturas();
    if (vista === "proveedores") cargarProveedores();
    if (vista === "movimientos") cargarMovimientos();
    if (vista === "mermas") cargarMermas();
    if (vista === "rotacion") cargarRotacion();
    if (vista === "planificacion") cargarPlanificacion();
    if (vista === "usuarios") cargarUsuarios();
  });
});

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
  const termino = (buscarInput ? buscarInput.value : "").trim().toLowerCase();

  const ubicacionesFiltradas = !termino ? state.ubicaciones : state.ubicaciones.filter(u => {
    const campos = [u.codigo, String(u.pasillo), String(u.estanteria), String(u.nivel)];
    u.productos.forEach(p => {
      campos.push(p.nombre, p.codigo_interno, p.categoria, p.modelo);
    });
    return campos.some(c => c && String(c).toLowerCase().includes(termino));
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
      <td><button class="btn-link" data-borrar-ubicacion="${u.id}">Eliminar</button></td>
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
}

function renderSelectUbicaciones(seleccionActualId) {
  const select = document.getElementById("f-ubicacion-id");
  select.innerHTML = '<option value="">Sin asignar</option>' +
    state.ubicaciones.map(u => `<option value="${u.id}">${escapeHtml(u.codigo)}${u.ocupada ? ` (${u.productos.length} producto/s)` : ""}</option>`).join("");
  select.value = seleccionActualId || "";
}

document.getElementById("btn-nueva-ubicacion").addEventListener("click", () => {
  state.origenModalUbicacion = "bodega";
  document.getElementById("form-ubicacion").reset();
  document.getElementById("u-preview").textContent = "Código resultante: —";
  abrirModal("modal-ubicacion");
});

document.getElementById("btn-crear-ubicacion-inline").addEventListener("click", () => {
  state.origenModalUbicacion = "producto";
  document.getElementById("form-ubicacion").reset();
  document.getElementById("u-preview").textContent = "Código resultante: —";
  abrirModal("modal-ubicacion");
});

["u-pasillo", "u-estanteria", "u-nivel"].forEach(id => {
  document.getElementById(id).addEventListener("input", () => {
    const p = document.getElementById("u-pasillo").value;
    const e = document.getElementById("u-estanteria").value;
    const n = document.getElementById("u-nivel").value;
    document.getElementById("u-preview").textContent =
      (p && e && n) ? `Código resultante: P${p}-E${e}-N${n}` : "Código resultante: —";
  });
});

document.getElementById("form-ubicacion").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    pasillo: document.getElementById("u-pasillo").value,
    estanteria: document.getElementById("u-estanteria").value,
    nivel: document.getElementById("u-nivel").value,
  };
  try {
    const nueva = await api("/api/ubicaciones", { method: "POST", body: JSON.stringify(payload) });
    toast("Ubicación creada: " + nueva.codigo);
    cerrarModal("modal-ubicacion");
    await cargarUbicaciones();
    if (state.origenModalUbicacion === "producto") {
      renderSelectUbicaciones(nueva.id);
      document.getElementById("f-ubicacion-id").value = nueva.id;
    }
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Categorías y modelos precargados (con alta rápida desde el formulario)
// ---------------------------------------------------------------------------

async function cargarCategoriasYMarcas() {
  try {
    const [cats, modelos] = await Promise.all([
      api("/api/categorias"),
      api("/api/modelos"),
    ]);
    // La API devuelve objetos {id, nombre, n_productos} (no strings sueltos)
    // para poder editarlos/eliminarlos desde el panel de gestión.
    state.categoriasDisponibles = cats.categorias;
    state.modelosDisponibles = modelos.modelos;
    renderSelectCategoria();
    renderSelectModelo();
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
  select.innerHTML = '<option value="">Sin categoría</option>' +
    state.categoriasDisponibles.map(c => `<option value="${escapeHtml(c.nombre)}">${escapeHtml(c.nombre)}</option>`).join("");
  select.value = actual || "";
}

function renderSelectModelo(seleccionActual) {
  const select = document.getElementById("f-modelo");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  select.innerHTML = '<option value="">Sin modelo</option>' +
    state.modelosDisponibles.map(m => `<option value="${escapeHtml(m.nombre)}">${escapeHtml(m.nombre)}</option>`).join("");
  select.value = actual || "";
}

const ETIQUETAS_ALTA_RAPIDA = {
  categoria: { texto: "categoría", render: renderSelectCategoria },
  modelo: { texto: "modelo", render: renderSelectModelo },
};

document.querySelectorAll("[data-nuevo]").forEach(btn => {
  btn.addEventListener("click", async () => {
    const tipo = btn.dataset.nuevo; // "categoria" | "modelo"
    const info = ETIQUETAS_ALTA_RAPIDA[tipo];
    const nombre = prompt(`Nombre de la nueva ${info.texto}:`);
    if (!nombre || !nombre.trim()) return;
    try {
      await api(`/api/${tipo}s`, { method: "POST", body: JSON.stringify({ nombre: nombre.trim() }) });
      await cargarCategoriasYMarcas();
      info.render(nombre.trim());
      toast(`${info.texto[0].toUpperCase() + info.texto.slice(1)} agregada`);
    } catch (e) {
      toast(e.message, true);
    }
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
    tbody.innerHTML = `<tr><td colspan="3" class="vacio">No hay categorías cargadas.</td></tr>`;
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
      const nuevo = prompt(`Nuevo nombre para la categoría "${btn.dataset.nombre}":`, btn.dataset.nombre);
      if (!nuevo || !nuevo.trim() || nuevo.trim().toUpperCase() === btn.dataset.nombre) return;
      try {
        const r = await api(`/api/categorias/${btn.dataset.editarCategoria}`, {
          method: "PUT", body: JSON.stringify({ nombre: nuevo.trim() }),
        });
        toast(`Categoría renombrada (${r.productos_actualizados} producto(s) actualizados)`);
        await Promise.all([cargarCategoriasYMarcas(), cargarProductos()]);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  tbody.querySelectorAll("[data-eliminar-categoria]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`¿Quitar "${btn.dataset.nombre}" de la lista de categorías precargadas? Los productos que ya la tienen NO se modifican.`)) return;
      try {
        await api(`/api/categorias/${btn.dataset.eliminarCategoria}`, { method: "DELETE" });
        toast("Categoría eliminada de la lista");
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
    tbody.innerHTML = `<tr><td colspan="3" class="vacio">No hay modelos cargados.</td></tr>`;
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
      const nuevo = prompt(`Nuevo nombre para el modelo "${btn.dataset.nombre}":`, btn.dataset.nombre);
      if (!nuevo || !nuevo.trim() || nuevo.trim().toUpperCase() === btn.dataset.nombre) return;
      try {
        const r = await api(`/api/modelos/${btn.dataset.editarModelo}`, {
          method: "PUT", body: JSON.stringify({ nombre: nuevo.trim() }),
        });
        toast(`Modelo renombrado (${r.productos_actualizados} producto(s) actualizados)`);
        await Promise.all([cargarCategoriasYMarcas(), cargarProductos()]);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  tbody.querySelectorAll("[data-eliminar-modelo]").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!confirm(`¿Quitar "${btn.dataset.nombre}" de la lista de modelos precargados? Los productos que ya lo tienen NO se modifican.`)) return;
      try {
        await api(`/api/modelos/${btn.dataset.eliminarModelo}`, { method: "DELETE" });
        toast("Modelo eliminado de la lista");
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
    toast("Categoría agregada");
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
    toast("Modelo agregado");
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

  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (categoria) params.set("categoria", categoria);
  if (criticos) params.set("criticos", "1");
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
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="vacio">Error al cargar productos: ${e.message}</td></tr>`;
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
  select.innerHTML = '<option value="">Todas las categorías</option>' +
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
    // El nombre visible se arma como "Categoría - Modelo" (o el código
    // interno / "Producto #N" si faltan ambos) — con Categoría y Modelo ya
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
        <td></td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => abrirDetalle(parseInt(tr.dataset.id, 10)));
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
  document.getElementById("f-stock-disponible").closest(".campo").style.display = "flex";
  document.getElementById("f-ubicacion-legacy").style.display = "none";
  await Promise.all([cargarUbicaciones(), cargarCategoriasYMarcas()]);
  renderSelectUbicaciones(null);
  renderSelectCategoria("");
  renderSelectModelo("");
  abrirModal("modal-producto");
});

async function abrirEdicion(p) {
  document.getElementById("f-id").value = p.id;
  document.getElementById("f-id-sistema-tag").textContent = `N° de sistema: #${p.id_sistema} (no editable)`;
  document.getElementById("f-id-sistema-tag").style.display = "block";
  document.getElementById("f-codigo").value = p.codigo_interno || "";
  document.getElementById("f-descripcion").value = p.descripcion || "";
  document.getElementById("f-costo").value = p.costo_compra;
  document.getElementById("f-costo-label").textContent = "Costo promedio actual ($ CLP)";

  await Promise.all([cargarUbicaciones(), cargarCategoriasYMarcas()]);
  renderSelectUbicaciones(p.ubicacion_id);
  renderSelectCategoria(p.categoria || "");
  renderSelectModelo(p.modelo || "");

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
    ubicacion_id: document.getElementById("f-ubicacion-id").value,
    costo_compra: document.getElementById("f-costo").value,
  };

  if (!id) {
    payload.stock_disponible = document.getElementById("f-stock-disponible").value;
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

async function abrirDetalle(id) {
  try {
    const p = await api(`/api/productos/${id}`);
    state.productoActivo = p;
    renderDetalle(p);
    resetearCampoFactura();
    document.getElementById("m-tipo").value = "entrada";
    document.getElementById("m-cantidad-label").textContent = "Cantidad";
    document.getElementById("m-costo-wrap").style.display = "flex";
    document.getElementById("m-factura-wrap").style.display = "flex";
    document.getElementById("m-categoria-wrap").style.display = "flex";
    document.getElementById("m-proveedor-merma-wrap").style.display = "none";
    poblarSelectCategoriaMovimiento(CATEGORIAS_ENTRADA, "Compra");
    abrirModal("modal-detalle");
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
    <dt>Categoría</dt><dd>${escapeHtml(p.categoria) || "—"}</dd>
    <dt>Modelo</dt><dd>${escapeHtml(p.modelo) || "—"}</dd>
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

function renderFotoProducto(p) {
  const preview = document.getElementById("detalle-foto-preview");
  const btnQuitar = document.getElementById("btn-quitar-foto");
  if (p.tiene_foto && p.foto_archivo) {
    preview.innerHTML = `<img src="/api/productos/foto/${encodeURIComponent(p.foto_archivo)}" alt="Foto de ${escapeHtml(p.nombre)}">`;
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
  const formData = new FormData();
  formData.append("foto", archivo);
  try {
    const res = await fetch(`/api/productos/${state.productoActivo.id}/foto`, { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo subir la foto");
    toast("Foto guardada");
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
    const factura = await api(`/api/facturas/numero/${encodeURIComponent(numero)}`);
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

document.getElementById("m-tipo").addEventListener("change", (e) => {
  const tipo = e.target.value;
  const esEntrada = tipo === "entrada";
  const esPerdida = tipo === "perdida";
  document.getElementById("m-cantidad-label").textContent = "Cantidad";
  document.getElementById("m-costo-wrap").style.display = esEntrada ? "flex" : "none";
  document.getElementById("m-factura-wrap").style.display = esEntrada ? "flex" : "none";
  document.getElementById("m-categoria-wrap").style.display = esPerdida ? "none" : "flex";
  document.getElementById("m-proveedor-merma-wrap").style.display = esPerdida ? "flex" : "none";
  if (!esPerdida) {
    poblarSelectCategoriaMovimiento(esEntrada ? CATEGORIAS_ENTRADA : CATEGORIAS_SALIDA, CATEGORIA_POR_DEFECTO[tipo]);
  } else {
    const productoId = document.getElementById("m-producto-id").value;
    if (productoId) cargarProveedoresMerma(productoId);
  }
  if (!esEntrada) resetearCampoFactura();
});

document.getElementById("form-movimiento").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("m-producto-id").value;
  const tipo = document.getElementById("m-tipo").value;
  const payload = {
    tipo,
    categoria: document.getElementById("m-categoria").value,
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
  try {
    await api(`/api/productos/${id}/movimiento`, { method: "POST", body: JSON.stringify(payload) });
    toast("Movimiento registrado");
    document.getElementById("form-movimiento").reset();
    document.getElementById("m-costo-wrap").style.display = "flex";
    document.getElementById("m-factura-wrap").style.display = "flex";
    document.getElementById("m-categoria-wrap").style.display = "flex";
    document.getElementById("m-proveedor-merma-wrap").style.display = "none";
    document.getElementById("m-cantidad-label").textContent = "Cantidad";
    document.getElementById("m-tipo").value = "entrada";
    poblarSelectCategoriaMovimiento(CATEGORIAS_ENTRADA, "Compra");
    resetearCampoFactura();
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
// Facturas (primera pieza del Módulo 4 - Ingreso de Compras)
// ---------------------------------------------------------------------------

async function cargarFacturas() {
  const tbody = document.getElementById("tbody-facturas");
  try {
    const data = await api("/api/facturas");
    state.facturas = data.facturas;
    renderTablaFacturas();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Error al cargar facturas: ${e.message}</td></tr>`;
  }
}

function renderTablaFacturas() {
  const tbody = document.getElementById("tbody-facturas");
  if (state.facturas.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Aún no hay facturas registradas. Usa "+ Nueva factura" para empezar.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.facturas.map(f => {
    const completa = f.total_lineas > 0 && f.lineas_aplicadas === f.total_lineas;
    const estado = completa
      ? '<span class="estado-pill estado-pill--ocupada">Todo ingresado</span>'
      : (f.lineas_aplicadas > 0
        ? `<span class="estado-pill estado-pill--ocupada">${f.lineas_aplicadas}/${f.total_lineas} ingresado</span>`
        : '<span class="estado-pill estado-pill--pendiente">Pendiente</span>');
    return `
      <tr data-id="${f.id}">
        <td class="mono">${escapeHtml(f.numero)}</td>
        <td>${escapeHtml(f.proveedor_display) || "—"}</td>
        <td class="mono">${escapeHtml(f.fecha) || "—"}</td>
        <td class="num mono">${f.total_lineas}</td>
        <td>${estado}</td>
        <td></td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => abrirFacturaDetalle(parseInt(tr.dataset.id, 10)));
  });
}

function filaLineaFactura(idx) {
  const div = document.createElement("div");
  div.className = "fac-linea-row";
  div.dataset.idx = idx;
  div.innerHTML = `
    <div class="campo">
      <label>Producto</label>
      <select class="fac-linea-producto">
        <option value="">Selecciona un producto…</option>
        ${state.productosParaFactura.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}${p.codigo_interno ? " (" + escapeHtml(p.codigo_interno) + ")" : ""}</option>`).join("")}
      </select>
    </div>
    <div class="campo">
      <label>Cantidad</label>
      <input type="number" class="fac-linea-cantidad" min="1" value="1">
    </div>
    <div class="campo">
      <label>Precio unit. ($ CLP)</label>
      <input type="number" class="fac-linea-precio" min="0" step="1" value="0">
    </div>
    <button type="button" class="fac-linea-quitar">Quitar</button>
  `;
  div.querySelector(".fac-linea-quitar").addEventListener("click", () => div.remove());
  return div;
}

document.getElementById("fac-agregar-linea").addEventListener("click", () => {
  const cont = document.getElementById("fac-lineas");
  cont.appendChild(filaLineaFactura(cont.children.length));
});

document.getElementById("btn-nueva-factura").addEventListener("click", async () => {
  document.getElementById("form-factura").reset();
  document.getElementById("fac-lineas").innerHTML = "";
  try {
    const [dataProductos, dataProveedores] = await Promise.all([
      api("/api/productos"),
      api("/api/proveedores"),
    ]);
    state.productosParaFactura = dataProductos.productos;
    state.proveedores = dataProveedores.proveedores;
  } catch (e) {
    toast(e.message, true);
    return;
  }
  renderSelectProveedorFactura("");
  document.getElementById("fac-lineas").appendChild(filaLineaFactura(0));
  abrirModal("modal-factura");
});

function renderSelectProveedorFactura(seleccionActual) {
  const select = document.getElementById("fac-proveedor-id");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  select.innerHTML = '<option value="">Sin proveedor</option>' +
    state.proveedores.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
  select.value = actual || "";
}

document.getElementById("fac-nuevo-proveedor").addEventListener("click", async () => {
  const nombre = prompt("Nombre del nuevo proveedor:");
  if (!nombre || !nombre.trim()) return;
  try {
    const nuevo = await api("/api/proveedores", { method: "POST", body: JSON.stringify({ nombre: nombre.trim() }) });
    const dataProveedores = await api("/api/proveedores");
    state.proveedores = dataProveedores.proveedores;
    renderSelectProveedorFactura(nuevo.id);
    toast("Proveedor agregado. Puedes completar RUT/teléfono/email después, en la pestaña Proveedores.");
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("form-factura").addEventListener("submit", async (e) => {
  e.preventDefault();
  const filas = Array.from(document.querySelectorAll("#fac-lineas .fac-linea-row"));
  const lineas = filas.map(fila => ({
    producto_id: fila.querySelector(".fac-linea-producto").value,
    cantidad: fila.querySelector(".fac-linea-cantidad").value,
    precio_unitario: fila.querySelector(".fac-linea-precio").value,
  }));

  if (lineas.some(l => !l.producto_id)) {
    toast("Selecciona un producto en cada línea", true);
    return;
  }

  const aplicarStock = document.getElementById("fac-aplicar-stock").checked;

  const payload = {
    numero: document.getElementById("fac-numero").value,
    proveedor_id: document.getElementById("fac-proveedor-id").value,
    fecha: document.getElementById("fac-fecha").value,
    lineas,
    aplicar_stock: aplicarStock,
    usuario: state.sesion.username || "Sistema",
  };

  try {
    const nueva = await api("/api/facturas", { method: "POST", body: JSON.stringify(payload) });
    if (aplicarStock) {
      toast(`Factura registrada y stock ingresado para ${nueva.lineas.length} producto(s)`);
    } else {
      toast("Factura registrada (stock pendiente de ingresar manualmente)");
    }
    cerrarModal("modal-factura");
    await Promise.all([cargarFacturas(), cargarProductos(), cargarResumen()]);
  } catch (e) {
    toast(e.message, true);
  }
});

async function abrirFacturaDetalle(id) {
  try {
    const f = await api(`/api/facturas/${id}`);
    state.facturaActivaId = f.id;
    document.getElementById("fac-detalle-titulo").textContent = `Factura ${f.numero}`;
    document.getElementById("fac-detalle-info").textContent =
      `Proveedor: ${f.proveedor_display || "—"}   ·   Fecha: ${f.fecha || "—"}   ·   Registrada: ${fmtFecha(f.creado_en)}`;

    const tbody = document.getElementById("fac-detalle-lineas");
    tbody.innerHTML = f.lineas.map(l => `
      <tr>
        <td>#${escapeHtml(l.producto_id_sistema)} — ${escapeHtml(l.producto_nombre)}</td>
        <td class="num mono">${l.cantidad}</td>
        <td class="num mono">${fmtMoney(l.precio_unitario)}</td>
        <td>${l.aplicada
          ? `<span class="estado-pill estado-pill--ocupada">Ingresado ${fmtFecha(l.aplicada_en)}</span>`
          : '<span class="estado-pill estado-pill--pendiente">Pendiente</span>'}</td>
      </tr>
    `).join("");

    abrirModal("modal-factura-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

document.getElementById("btn-eliminar-factura").addEventListener("click", async () => {
  const id = state.facturaActivaId;
  if (!id) return;
  try {
    const resultado = await eliminarConReintento(
      `/api/facturas/${id}`,
      "¿Eliminar esta factura? Esta acción no se puede deshacer.",
      { usuario: "Sistema" }
    );
    if (!resultado) return;
    if (resultado.revertido) {
      toast("Factura eliminada. El stock que había ingresado fue revertido (revisa el costo promedio si corresponde).");
    } else {
      toast("Factura eliminada");
    }
    cerrarModal("modal-factura-detalle");
    await cargarFacturas();
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
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Error al cargar proveedores: ${e.message}</td></tr>`;
  }
}

function renderTablaProveedores() {
  const tbody = document.getElementById("tbody-proveedores");
  if (state.proveedores.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="vacio">Aún no hay proveedores registrados. Usa "+ Nuevo proveedor" para empezar.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.proveedores.map(p => `
    <tr data-id="${p.id}">
      <td>${escapeHtml(p.nombre)}</td>
      <td class="mono">${escapeHtml(p.rut) || "—"}</td>
      <td>${escapeHtml(p.telefono) || "—"}</td>
      <td>${escapeHtml(p.email) || "—"}</td>
      <td class="num mono">${p.cantidad_facturas}</td>
      <td></td>
    </tr>
  `).join("");

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => abrirEdicionProveedor(parseInt(tr.dataset.id, 10)));
  });
}

// ---------------------------------------------------------------------------
// Carga masiva de facturas vía Excel
// ---------------------------------------------------------------------------

state.cargaMasivaToken = null;

function cmMostrarPaso(paso) {
  document.getElementById("cm-paso-subir").style.display = paso === "subir" ? "block" : "none";
  document.getElementById("cm-paso-preview").style.display = paso === "preview" ? "block" : "none";
}

document.getElementById("btn-carga-masiva-facturas").addEventListener("click", () => {
  document.getElementById("cm-archivo").value = "";
  state.cargaMasivaToken = null;
  cmMostrarPaso("subir");
  abrirModal("modal-carga-masiva");
});

document.getElementById("btn-cm-volver").addEventListener("click", () => {
  document.getElementById("cm-archivo").value = "";
  state.cargaMasivaToken = null;
  cmMostrarPaso("subir");
});

document.getElementById("btn-cm-previsualizar").addEventListener("click", async () => {
  const archivo = document.getElementById("cm-archivo").files[0];
  if (!archivo) {
    toast("Selecciona un archivo Excel primero", true);
    return;
  }
  const formData = new FormData();
  formData.append("archivo", archivo);
  try {
    const res = await fetch("/api/facturas/carga-masiva/previsualizar", { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "No se pudo leer el archivo");
    state.cargaMasivaToken = data.token;
    renderPreviewCargaMasiva(data);
    cmMostrarPaso("preview");
  } catch (e) {
    toast(e.message, true);
  }
});

function renderPreviewCargaMasiva(data) {
  const r = data.resumen;
  document.getElementById("cm-resumen").textContent =
    `${r.total_facturas} factura(s) detectada(s): ${r.validas} lista(s) para cargar, ${r.con_errores} con errores (no se van a importar).`;

  document.getElementById("cm-lista-facturas").innerHTML = data.facturas.map(f => {
    const claseEstado = f.valida ? "badge-urgencia badge-urgencia--proximo" : "badge-urgencia badge-urgencia--critico";
    const textoEstado = f.valida ? "Lista para cargar" : "Con errores — se omitirá";
    const proveedorTxt = f.proveedor_nombre
      ? `${escapeHtml(f.proveedor_nombre)}${f.proveedor_nuevo ? " (se creará)" : ""}`
      : "Sin proveedor";
    const lineasHtml = f.lineas.map(l => `
      <tr class="${l.errores.length ? "fila-con-error" : ""}">
        <td>${l.producto_nombre ? escapeHtml(l.producto_nombre) : `<span class="vacio-inline">Producto no identificado (fila ${l.fila})</span>`}</td>
        <td class="num">${l.cantidad ?? "—"}</td>
        <td class="num">${l.precio_unitario != null ? fmtMoney(l.precio_unitario) : "—"}</td>
        <td>${l.errores.length ? l.errores.map(escapeHtml).join("<br>") : ""}</td>
      </tr>`).join("");
    return `
      <div class="dash-panel" style="margin-bottom:14px">
        <h3 class="dash-titulo">${escapeHtml(f.numero)} <span class="${claseEstado}">${textoEstado}</span></h3>
        <p class="ayuda-inline">Proveedor: ${proveedorTxt}${f.fecha ? ` · Fecha: ${escapeHtml(f.fecha)}` : ""}</p>
        ${f.errores.length && !f.lineas.some(l => l.errores.length) ? `<p class="ayuda-inline ayuda-inline--bloqueado">${f.errores.map(escapeHtml).join("<br>")}</p>` : ""}
        <table class="tabla">
          <thead><tr><th>Producto</th><th class="num">Cantidad</th><th class="num">Precio unit.</th><th>Errores</th></tr></thead>
          <tbody>${lineasHtml}</tbody>
        </table>
      </div>`;
  }).join("");
}

document.getElementById("btn-cm-confirmar").addEventListener("click", async () => {
  if (!state.cargaMasivaToken) return;
  try {
    const data = await api("/api/facturas/carga-masiva/confirmar", {
      method: "POST",
      body: JSON.stringify({ token: state.cargaMasivaToken }),
    });
    const detalle = data.omitidas.length
      ? ` (se omitieron: ${data.omitidas.join(", ")})`
      : "";
    toast(`${data.total_creadas} factura(s) cargada(s)${detalle}`);
    cerrarModal("modal-carga-masiva");
    state.cargaMasivaToken = null;
    await Promise.all([cargarFacturas(), cargarProductos()]);
  } catch (e) {
    toast(e.message, true);
  }
});

// Formatea el RUT en vivo mientras el usuario escribe (puntos de miles + guión
// antes del dígito verificador), igual que lo hace el backend al guardar —
// así se ve de inmediato aunque el backend es quien manda la versión final.
function formatearRutEnVivo(valor) {
  const limpio = valor.replace(/[^0-9kK]/g, "");
  if (!limpio) return "";
  const cuerpo = limpio.slice(0, -1);
  const dv = limpio.slice(-1).toUpperCase();
  if (!cuerpo) return dv;
  const cuerpoConPuntos = cuerpo.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `${cuerpoConPuntos}-${dv}`;
}

document.getElementById("pv-rut").addEventListener("input", (e) => {
  const cursorAlFinal = e.target.selectionEnd === e.target.value.length;
  e.target.value = formatearRutEnVivo(e.target.value);
  if (cursorAlFinal) {
    e.target.setSelectionRange(e.target.value.length, e.target.value.length);
  }
});

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
  document.getElementById("pv-nombre").value = p.nombre;
  document.getElementById("pv-rut").value = p.rut || "";
  document.getElementById("pv-telefono").value = p.telefono || "";
  document.getElementById("pv-email").value = p.email || "";
  document.getElementById("pv-direccion").value = p.direccion || "";
  document.getElementById("modal-proveedor-titulo").textContent = "Editar proveedor";
  document.getElementById("pv-acciones-editar").style.display = "flex";
  state.proveedorActivoId = p.id;
  abrirModal("modal-proveedor");
}

document.getElementById("btn-eliminar-proveedor").addEventListener("click", async () => {
  const id = state.proveedorActivoId;
  const p = state.proveedores.find(x => x.id === id);
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
  } catch (e) {
    toast(e.message, true);
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

function nombreCortoProducto(p) {
  // Para el gráfico de rotación no hace falta el ID: basta categoría +
  // modelo (que es justo lo que identifica al producto para el taller).
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
// Respaldo de datos
// ---------------------------------------------------------------------------

document.getElementById("btn-respaldo").addEventListener("click", async () => {
  document.getElementById("respaldo-diagnostico").textContent = "Cargando diagnóstico…";
  abrirModal("modal-respaldo");
  try {
    const info = await api("/api/estado");
    const disco = info.parece_disco_persistente ? "✓ usando disco persistente (/data)" : "⚠ NO parece estar usando un disco persistente — revisa la variable DB_PATH en Railway";
    document.getElementById("respaldo-diagnostico").innerHTML = `
      Ruta de la base de datos: <span class="mono">${escapeHtml(info.db_path)}</span><br>
      ${disco}<br>
      Productos: ${info.cantidad_productos} · Facturas: ${info.cantidad_facturas} · Movimientos: ${info.cantidad_movimientos}
      (${info.tamano_kb} KB)
    `;
  } catch (e) {
    document.getElementById("respaldo-diagnostico").textContent = "No se pudo cargar el diagnóstico: " + e.message;
  }
  await cargarEstadoRespaldoAutomatico();
  await cargarEstadoAlertasStock();
});

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
    cerrarModal("modal-respaldo");
    input.value = "";
    await Promise.all([cargarDashboard(), cargarProductos(), cargarResumen()]);
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Sesión
// ---------------------------------------------------------------------------

async function cargarSesion() {
  try {
    const s = await api("/api/sesion");
    state.sesion = s;
    document.getElementById("sesion-usuario").textContent = s.nombre ? `👤 ${s.nombre}` : "";
    document.getElementById("m-usuario").value = s.nombre || s.username || "";
  } catch (e) {
    console.error(e);
  }
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
    tbody.innerHTML = `<tr><td colspan="4" class="vacio">Error al cargar usuarios: ${e.message}</td></tr>`;
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
      btn.addEventListener("click", async () => {
        const max = btn.dataset.max;
        const cantidad = prompt(`¿Cuántas unidades quieres descartar definitivamente? (máximo ${max})`, max);
        if (!cantidad) return;
        try {
          await api(`/api/productos/${btn.dataset.descartar}/merma/descartar`, {
            method: "POST",
            body: JSON.stringify({ cantidad, proveedor_id: btn.dataset.proveedor || null }),
          });
          toast("Descartado de la cuenta de merma");
          await Promise.all([cargarMermas(), cargarProductos(), cargarResumen()]);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });

    tbody.querySelectorAll("[data-revertir]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const max = btn.dataset.max;
        const cantidad = prompt(`¿Cuántas unidades quieres devolver a stock disponible? (máximo ${max})`, max);
        if (!cantidad) return;
        try {
          await api(`/api/productos/${btn.dataset.revertir}/merma/revertir`, {
            method: "POST",
            body: JSON.stringify({ cantidad, proveedor_id: btn.dataset.proveedor || null }),
          });
          toast("Merma revertida: stock devuelto a disponible");
          await Promise.all([cargarMermas(), cargarProductos(), cargarResumen()]);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });

    tbody.querySelectorAll("[data-ver-origen]").forEach(btn => {
      btn.addEventListener("click", () => verOrigenMerma(btn.dataset.verOrigen, btn.dataset.nombre));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Error al cargar mermas: ${e.message}</td></tr>`;
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
    tbody.innerHTML = `<tr><td colspan="5" class="vacio">Error: ${e.message}</td></tr>`;
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

  select.innerHTML = '<option value="">Todas las categorías</option>' +
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
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar movimientos: ${e.message}</td></tr>`;
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

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarProductos();
cargarDashboard();
