const state = {
  productos: [],
  categorias: [],
  productoActivo: null,
  ubicaciones: [],
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

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.add("visible");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("visible"), 3200);
}

function abrirModal(id) {
  document.getElementById(id).classList.add("abierto");
}
function cerrarModal(id) {
  document.getElementById(id).classList.remove("abierto");
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
    document.getElementById("vista-reservas").style.display = vista === "reservas" ? "block" : "none";
    document.getElementById("vista-usuarios").style.display = vista === "usuarios" ? "block" : "none";
    document.getElementById("btn-nuevo").style.display = vista === "productos" ? "inline-block" : "none";
    const titulos = {
      dashboard: "Dashboard",
      productos: "Administración de Inventario",
      bodega: "Organización Física de Bodega",
      facturas: "Facturas de compra",
      proveedores: "Proveedores",
      movimientos: "Control de Movimientos",
      reservas: "Reserva de Productos",
      usuarios: "Usuarios",
    };
    document.getElementById("brand-sub").textContent = titulos[vista];
    if (vista === "dashboard") cargarDashboard();
    if (vista === "bodega") cargarUbicaciones();
    if (vista === "facturas") cargarFacturas();
    if (vista === "proveedores") cargarProveedores();
    if (vista === "movimientos") cargarMovimientos();
    if (vista === "reservas") cargarReservas();
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

function renderTablaUbicaciones() {
  const tbody = document.getElementById("tbody-ubicaciones");
  if (state.ubicaciones.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">Aún no hay ubicaciones creadas. Usa "+ Nueva ubicación" para empezar a mapear la bodega.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.ubicaciones.map(u => {
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
    state.categoriasDisponibles = cats.categorias;
    state.modelosDisponibles = modelos.modelos;
    renderSelectCategoria();
    renderSelectModelo();
  } catch (e) {
    console.error(e);
  }
}

function renderSelectCategoria(seleccionActual) {
  const select = document.getElementById("f-categoria");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  select.innerHTML = '<option value="">Sin categoría</option>' +
    state.categoriasDisponibles.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  select.value = actual || "";
}

function renderSelectModelo(seleccionActual) {
  const select = document.getElementById("f-modelo");
  const actual = seleccionActual !== undefined ? seleccionActual : select.value;
  select.innerHTML = '<option value="">Sin modelo</option>' +
    state.modelosDisponibles.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
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

async function cargarProductos() {
  const q = document.getElementById("buscar").value.trim();
  const categoria = document.getElementById("filtro-categoria").value;
  const criticos = document.getElementById("filtro-criticos").checked;

  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (categoria) params.set("categoria", categoria);
  if (criticos) params.set("criticos", "1");

  const tbody = document.getElementById("tbody-productos");
  try {
    const data = await api("/api/productos?" + params.toString());
    state.productos = data.productos;
    state.categorias = data.categorias;
    renderCategorias();
    renderTabla();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">Error al cargar productos: ${e.message}</td></tr>`;
  }
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
    tbody.innerHTML = `<tr><td colspan="9" class="vacio">No se encontraron productos.</td></tr>`;
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
    return `
      <tr data-id="${p.id}">
        <td><span class="codigo-pill">#${escapeHtml(p.id_sistema)}</span></td>
        <td>
          <div class="nombre-producto">${escapeHtml(p.nombre)} ${critico ? '<span class="badge-critico">Crítico</span>' : ""}</div>
          <div class="subtexto">${escapeHtml([p.codigo_interno, p.modelo].filter(Boolean).join(" · ")) || "&nbsp;"}</div>
        </td>
        <td>${escapeHtml(p.categoria) || "—"}</td>
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
  buscarTimeout = setTimeout(cargarProductos, 250);
});
document.getElementById("filtro-categoria").addEventListener("change", cargarProductos);
document.getElementById("filtro-criticos").addEventListener("change", cargarProductos);

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
  document.getElementById("f-stock-minimo").value = p.stock_minimo;
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
    stock_minimo: document.getElementById("f-stock-minimo").value,
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
    poblarSelectCategoriaMovimiento("Compra");
    document.getElementById("form-reservar").reset();
    await cargarReservasDelProducto(id);
    abrirModal("modal-detalle");
  } catch (e) {
    toast(e.message, true);
  }
}

async function cargarReservasDelProducto(productoId) {
  const cont = document.getElementById("reservas-lista");
  try {
    const data = await api(`/api/reservas?producto_id=${productoId}&estado=activa`);
    if (data.reservas.length === 0) {
      cont.innerHTML = `<p class="vacio">Sin reservas activas.</p>`;
      return;
    }
    cont.innerHTML = data.reservas.map(r => `
      <div class="mov-item">
        <span class="mov-tipo mov-tipo--ajuste">${r.cantidad}</span>
        <span class="mov-detalle">
          ${escapeHtml(r.orden_trabajo) || "Sin OT"} ${r.motivo ? " · " + escapeHtml(r.motivo) : ""}
          · ${escapeHtml(r.usuario)} · ${fmtFecha(r.creado_en)}
        </span>
        <button type="button" class="btn-link" data-consumir="${r.id}">Consumir</button>
        <button type="button" class="btn-link" data-liberar="${r.id}">Liberar</button>
      </div>
    `).join("");

    cont.querySelectorAll("[data-liberar]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/reservas/${btn.dataset.liberar}/liberar`, { method: "POST" });
          toast("Reserva liberada");
          await Promise.all([cargarReservasDelProducto(productoId), abrirDetalle(productoId), cargarProductos(), cargarResumen()]);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
    cont.querySelectorAll("[data-consumir]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("¿Marcar esta reserva como consumida? El stock reservado se descuenta del inventario.")) return;
        try {
          await api(`/api/reservas/${btn.dataset.consumir}/consumir`, {
            method: "POST",
            body: JSON.stringify({ usuario: state.sesion.nombre || state.sesion.username }),
          });
          toast("Reserva consumida");
          await Promise.all([cargarReservasDelProducto(productoId), abrirDetalle(productoId), cargarProductos(), cargarResumen()]);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
  } catch (e) {
    cont.innerHTML = `<p class="vacio">Error al cargar reservas: ${e.message}</p>`;
  }
}

document.getElementById("form-reservar").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = state.productoActivo;
  if (!p) return;
  const payload = {
    cantidad: document.getElementById("res-cantidad").value,
    orden_trabajo: document.getElementById("res-ot").value,
    motivo: document.getElementById("res-motivo").value,
    usuario: state.sesion.nombre || state.sesion.username,
  };
  try {
    await api(`/api/productos/${p.id}/reservar`, { method: "POST", body: JSON.stringify(payload) });
    toast("Stock reservado");
    document.getElementById("form-reservar").reset();
    await Promise.all([cargarProductos(), cargarResumen()]);
    await abrirDetalle(p.id);
  } catch (e) {
    toast(e.message, true);
  }
});

function renderDetalle(p) {
  document.getElementById("detalle-nombre").textContent = `${p.nombre}`;
  document.getElementById("m-producto-id").value = p.id;

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
    <dt>Stock mínimo</dt><dd class="mono">${p.stock_minimo}</dd>
    <dt>Costo promedio</dt><dd class="mono">${fmtMoney(p.costo_compra)}</dd>
  `;

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
          ${m.motivo ? " · " + escapeHtml(m.motivo) : ""}
          ${m.usuario ? " · " + escapeHtml(m.usuario) : ""}
        </span>
        <span class="mov-fecha">${fmtFecha(m.fecha)}</span>
      </div>
    `).join("");
  }
}

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

const CATEGORIAS_MOVIMIENTO = ["Compra", "Venta", "Consumo interno", "Reserva", "Ajuste de inventario", "Devolución", "Traslado interno", "Merma o pérdida"];
const CATEGORIA_POR_DEFECTO = { entrada: "Compra", salida: "Consumo interno", ajuste: "Ajuste de inventario" };

function poblarSelectCategoriaMovimiento(seleccion) {
  const select = document.getElementById("m-categoria");
  select.innerHTML = CATEGORIAS_MOVIMIENTO.map(c => `<option value="${c}">${c}</option>`).join("");
  select.value = seleccion;
}

document.getElementById("m-tipo").addEventListener("change", (e) => {
  const label = document.getElementById("m-cantidad-label");
  const esEntrada = e.target.value === "entrada";
  label.textContent = e.target.value === "ajuste" ? "Nuevo stock disponible (valor final)" : "Cantidad";
  document.getElementById("m-costo-wrap").style.display = esEntrada ? "flex" : "none";
  document.getElementById("m-factura-wrap").style.display = esEntrada ? "flex" : "none";
  poblarSelectCategoriaMovimiento(CATEGORIA_POR_DEFECTO[e.target.value]);
  if (!esEntrada) resetearCampoFactura();
});

document.getElementById("form-movimiento").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("m-producto-id").value;
  const payload = {
    tipo: document.getElementById("m-tipo").value,
    categoria: document.getElementById("m-categoria").value,
    cantidad: document.getElementById("m-cantidad").value,
    costo_unitario: document.getElementById("m-costo").value,
    factura_numero: document.getElementById("m-factura").value,
    motivo: document.getElementById("m-motivo").value,
    usuario: document.getElementById("m-usuario").value,
  };
  try {
    await api(`/api/productos/${id}/movimiento`, { method: "POST", body: JSON.stringify(payload) });
    toast("Movimiento registrado");
    document.getElementById("form-movimiento").reset();
    document.getElementById("m-costo-wrap").style.display = "flex";
    document.getElementById("m-factura-wrap").style.display = "flex";
    document.getElementById("m-cantidad-label").textContent = "Cantidad";
    poblarSelectCategoriaMovimiento("Compra");
    resetearCampoFactura();
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
});

async function cargarEstadoRespaldoAutomatico() {
  const cont = document.getElementById("respaldo-auto-estado");
  cont.textContent = "Cargando…";
  try {
    const info = await api("/api/backup/estado-automatico");
    if (!info.configurado) {
      cont.innerHTML = `⚠ No está configurado todavía. Se necesita agregar las variables <span class="mono">SMTP_USER</span> y <span class="mono">SMTP_PASSWORD</span> en Railway (ver guía DEPLOY.md).`;
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
// Reservas (Módulo 6 - vista general)
// ---------------------------------------------------------------------------

async function cargarReservas() {
  const tbody = document.getElementById("tbody-reservas");
  const estado = document.getElementById("res-filtro-estado").value;
  const params = estado ? `?estado=${estado}` : "";
  try {
    const data = await api("/api/reservas" + params);
    if (data.reservas.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" class="vacio">No hay reservas con ese filtro.</td></tr>`;
      return;
    }
    tbody.innerHTML = data.reservas.map(r => {
      const estadoClase = r.estado === "activa" ? "ocupada" : (r.estado === "consumida" ? "libre" : "pendiente");
      const acciones = r.estado === "activa"
        ? `<button class="btn-link" data-consumir-tab="${r.id}">Consumir</button> <button class="btn-link" data-liberar-tab="${r.id}">Liberar</button>`
        : "";
      return `
        <tr>
          <td>#${escapeHtml(r.producto_id_sistema)} ${escapeHtml(r.producto_nombre)}</td>
          <td class="num mono">${r.cantidad}</td>
          <td class="mono">${escapeHtml(r.orden_trabajo) || "—"}</td>
          <td>${escapeHtml(r.motivo) || "—"}</td>
          <td>${escapeHtml(r.usuario) || "—"}</td>
          <td class="mono">${fmtFecha(r.creado_en)}</td>
          <td><span class="estado-pill estado-pill--${estadoClase}">${r.estado}</span></td>
          <td>${acciones}</td>
        </tr>
      `;
    }).join("");

    tbody.querySelectorAll("[data-liberar-tab]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/reservas/${btn.dataset.liberarTab}/liberar`, { method: "POST" });
          toast("Reserva liberada");
          await Promise.all([cargarReservas(), cargarProductos(), cargarResumen()]);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
    tbody.querySelectorAll("[data-consumir-tab]").forEach(btn => {
      btn.addEventListener("click", async () => {
        if (!confirm("¿Marcar esta reserva como consumida?")) return;
        try {
          await api(`/api/reservas/${btn.dataset.consumirTab}/consumir`, {
            method: "POST",
            body: JSON.stringify({ usuario: state.sesion.nombre || state.sesion.username }),
          });
          toast("Reserva consumida");
          await Promise.all([cargarReservas(), cargarProductos(), cargarResumen()]);
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="vacio">Error al cargar reservas: ${e.message}</td></tr>`;
  }
}

document.getElementById("res-filtro-estado").addEventListener("change", cargarReservas);

// ---------------------------------------------------------------------------
// Movimientos (Módulo 5 - bitácora completa con filtros)
// ---------------------------------------------------------------------------

let categoriasMovFiltroCargadas = false;

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
      const select = document.getElementById("mov-filtro-categoria");
      select.innerHTML = '<option value="">Todas las categorías</option>' +
        data.categorias.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
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
document.getElementById("mov-filtro-tipo").addEventListener("change", cargarMovimientos);
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

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

cargarSesion();
cargarProductos();
cargarDashboard();
