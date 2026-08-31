// ---------------------------------------------------------------------------
// Ventana dedicada para registrar una factura de compra INTERNACIONAL, por
// pasos (ver /facturas/nueva-internacional en app.py) -- mismo criterio que
// /facturas/nueva (factura_nueva.js): página aparte en vez de modal, con
// utilidades compartidas (fmtMoney, toast, escapeHtml, api, etc.)
// duplicadas a propósito, mismo patrón que el resto del sistema.
//
// A diferencia de la factura nacional, acá NO hay "guardar para continuar
// más adelante": el registro real recién se crea al terminar (paso 3),
// porque hasta ese punto falta el tipo de cambio y/o los gastos de la
// importación para poder calcular el costo final de cada línea -- no tiene
// sentido guardar un pendiente a medio calcular. Por eso "Descartar y
// salir" no necesita borrar nada en el servidor (ver intentarSalir).
// ---------------------------------------------------------------------------

const state = {
  sesion: { username: null, nombre: null },
  productosCache: [],
  proveedoresCache: [],
  guardando: false,
};

function fmtMoney(n) {
  return "$" + Math.round(n).toLocaleString("es-CL");
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.add("visible");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("visible"), 3200);
}

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
    err.data = data;
    throw err;
  }
  return data;
}

function abrirModal(id) {
  document.getElementById(id).classList.add("abierto");
}
function cerrarModal(id) {
  document.getElementById(id).classList.remove("abierto");
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

async function cargarCatalogos() {
  const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
  state.productosCache = dataProductos.productos;
  state.proveedoresCache = dataProveedores.proveedores;
  repintarSelectProveedores();
}

function repintarSelectProveedores(seleccionar) {
  const select = document.getElementById("fi-proveedor-id");
  const actual = seleccionar != null ? seleccionar : select.value;
  select.innerHTML = '<option value="">Sin proveedor</option>' +
    state.proveedoresCache.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
  if (actual) select.value = actual;
}

// ---------------------------------------------------------------------------
// Navegación por pasos
// ---------------------------------------------------------------------------

function mostrarPaso(n) {
  for (let i = 1; i <= 3; i++) {
    document.getElementById(`fi-paso-${i}`).style.display = i === n ? "block" : "none";
    document.querySelector(`#fi-pasos [data-paso="${i}"]`).classList.toggle("activo", i === n);
  }
  if (n === 3) actualizarTotalesPaso3();
}

document.getElementById("fi-p1-siguiente").addEventListener("click", async () => {
  const numero = document.getElementById("fi-numero").value.trim();
  if (!numero) {
    toast("Ingresa el número de la factura antes de seguir", true);
    return;
  }
  const tipoCambio = document.getElementById("fi-tipo-cambio").value;
  if (!tipoCambio || Number(tipoCambio) <= 0) {
    toast("Ingresa el tipo de cambio de esta factura antes de seguir", true);
    return;
  }
  const btn = document.getElementById("fi-p1-siguiente");
  btn.disabled = true;
  try {
    const check = await api(`/api/facturas/verificar-numero/${encodeURIComponent(numero)}`);
    if (check.existe) {
      toast(check.mensaje, true);
      return;
    }
    mostrarPaso(2);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("fi-p2-atras").addEventListener("click", () => mostrarPaso(1));
document.getElementById("fi-p2-siguiente").addEventListener("click", () => {
  const filas = Array.from(document.querySelectorAll("#fi-lineas .fac-linea-row"));
  if (!filas.length) {
    toast("Agrega al menos un producto a la factura", true);
    return;
  }
  mostrarPaso(3);
});
document.getElementById("fi-p3-atras").addEventListener("click", () => mostrarPaso(2));

// ---------------------------------------------------------------------------
// Salir / cerrar a medio camino: acá no hay nada guardado en el servidor
// todavía (a diferencia de /facturas/nueva), así que "Descartar" solo
// navega de vuelta, sin llamar a la API.
// ---------------------------------------------------------------------------

function hayAlgoQuePerder() {
  const numero = document.getElementById("fi-numero").value.trim();
  const hayLineas = document.querySelectorAll("#fi-lineas .fac-linea-row").length > 0;
  return !!numero || hayLineas;
}

function intentarSalir() {
  if (!hayAlgoQuePerder()) {
    window.location.href = "/finanzas";
    return;
  }
  abrirModal("modal-fi-cerrar");
}

document.getElementById("fi-btn-salir").addEventListener("click", intentarSalir);
document.getElementById("fi-p1-salir").addEventListener("click", intentarSalir);
document.getElementById("fi-cerrar-cancelar").addEventListener("click", () => cerrarModal("modal-fi-cerrar"));
document.getElementById("fi-cerrar-descartar").addEventListener("click", () => {
  window.location.href = "/finanzas";
});

// ---------------------------------------------------------------------------
// Proveedor: catálogo + crear uno nuevo al vuelo (mismo patrón que
// /facturas/nueva)
// ---------------------------------------------------------------------------

document.getElementById("fi-btn-nuevo-proveedor").addEventListener("click", () => {
  const form = document.getElementById("fi-nuevo-proveedor-form");
  form.style.display = form.style.display === "none" ? "block" : "none";
});
document.getElementById("fi-prov-cancelar").addEventListener("click", () => {
  document.getElementById("fi-nuevo-proveedor-form").style.display = "none";
});
document.getElementById("fi-prov-guardar").addEventListener("click", async () => {
  const nombre = document.getElementById("fi-prov-nombre").value.trim();
  if (!nombre) {
    toast("Escribe el nombre del proveedor", true);
    return;
  }
  try {
    const nuevo = await api("/api/proveedores", {
      method: "POST",
      body: JSON.stringify({
        nombre,
        rut: document.getElementById("fi-prov-rut").value.trim(),
        telefono: document.getElementById("fi-prov-telefono").value.trim(),
      }),
    });
    state.proveedoresCache = [...state.proveedoresCache, nuevo];
    repintarSelectProveedores(nuevo.id);
    document.getElementById("fi-nuevo-proveedor-form").style.display = "none";
    document.getElementById("fi-prov-nombre").value = "";
    document.getElementById("fi-prov-rut").value = "";
    document.getElementById("fi-prov-telefono").value = "";
    toast(`Proveedor "${nuevo.nombre}" creado`);
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Líneas de producto: cada una con su costo unitario en DÓLARES (no en
// pesos, a diferencia de la factura nacional) -- ver
// _calcular_prorrateo_internacional en app.py para el cálculo real. Lo que
// se recalcula acá en el navegador (fiRecalcular) es solo una VISTA PREVIA
// en vivo; el servidor es siempre la autoridad final al guardar.
// ---------------------------------------------------------------------------

function filaLineaFacturaInternacional(productos, linea) {
  linea = linea || {};
  const div = document.createElement("div");
  div.className = "fac-linea-row";
  div.innerHTML = `
    <div class="campo fi-linea-producto-campo">
      <label>Producto</label>
      <select class="fi-linea-producto-id">
        <option value="">Selecciona un producto…</option>
        <option value="__nuevo__">✏️ No está en el catálogo — describir para Bodega</option>
        ${productos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("")}
      </select>
      <input type="text" class="fi-linea-nombre-nuevo" placeholder="¿Qué producto es? (ej. Kit de embrague ABC 2020)" style="display:none;margin-top:6px">
    </div>
    <div class="campo fi-linea-cantidad-campo">
      <label>Cantidad</label>
      <input type="number" class="fi-linea-cantidad" min="1" value="${linea.cantidad != null ? linea.cantidad : 1}">
    </div>
    <div class="campo fi-linea-costo-campo">
      <label>Costo unit. (US$)</label>
      <input type="number" class="fi-linea-costo-usd" min="0" step="0.01" value="${linea.costo_unitario_usd != null ? linea.costo_unitario_usd : 0}">
    </div>
    <p class="fi-linea-calculo">Completa el costo unitario y el tipo de cambio para ver el costo final estimado.</p>
    <button type="button" class="fac-linea-quitar">Quitar</button>
  `;
  const select = div.querySelector(".fi-linea-producto-id");
  const inputNombreNuevo = div.querySelector(".fi-linea-nombre-nuevo");
  if (linea.producto_id) select.value = linea.producto_id;
  function sincronizarVisibilidad() {
    inputNombreNuevo.style.display = select.value === "__nuevo__" ? "block" : "none";
  }
  sincronizarVisibilidad();
  select.addEventListener("change", () => { sincronizarVisibilidad(); fiRecalcular(); });
  div.querySelector(".fi-linea-cantidad").addEventListener("input", fiRecalcular);
  div.querySelector(".fi-linea-costo-usd").addEventListener("input", fiRecalcular);
  div.querySelector(".fac-linea-quitar").addEventListener("click", () => { div.remove(); fiRecalcular(); });
  return div;
}

// Reproduce en el navegador, como vista previa, la misma lógica de
// _calcular_prorrateo_internacional (app.py) -- ver el comentario largo de
// esa función para la explicación completa de las dos pasadas de
// prorrateo (flete+PayPal por costo unitario US$, después DHL por peso en
// pesos), incluyendo la nota sobre por qué la cantidad divide el resultado
// dos veces en la práctica (confirmado con el taller).
function fiRecalcular() {
  const filas = Array.from(document.querySelectorAll("#fi-lineas .fac-linea-row"));
  const tipoCambio = parseFloat(document.getElementById("fi-tipo-cambio").value) || 0;
  const shippingUsd = parseFloat(document.getElementById("fi-shipping-usd").value) || 0;
  const paypalUsd = parseFloat(document.getElementById("fi-paypal-usd").value) || 0;
  const dhlClp = parseFloat(document.getElementById("fi-dhl-clp").value) || 0;

  const datos = filas.map(fila => ({
    fila,
    cantidad: parseFloat(fila.querySelector(".fi-linea-cantidad").value) || 0,
    costoUsd: parseFloat(fila.querySelector(".fi-linea-costo-usd").value) || 0,
  }));
  const totalUsdUnitario = datos.reduce((acc, d) => acc + d.costoUsd, 0);

  let netoEstimado = 0;
  if (tipoCambio > 0 && totalUsdUnitario > 0 && datos.length) {
    let totalClpPreDhl = 0;
    datos.forEach(d => {
      d.pctFlete = d.costoUsd / totalUsdUnitario;
      d.clpPreDhl = (d.costoUsd + shippingUsd * d.pctFlete + paypalUsd * d.pctFlete) * tipoCambio;
      totalClpPreDhl += d.clpPreDhl;
    });
    datos.forEach(d => {
      const pctDhl = totalClpPreDhl > 0 ? d.clpPreDhl / totalClpPreDhl : 0;
      const totalClpLinea = d.clpPreDhl + dhlClp * pctDhl;
      const costoFinalUnit = d.cantidad > 0 ? Math.round(totalClpLinea / d.cantidad) : 0;
      d.fila.querySelector(".fi-linea-calculo").textContent = d.cantidad > 0
        ? `Prorrateo: ${(d.pctFlete * 100).toFixed(1)}% flete/PayPal, ${(pctDhl * 100).toFixed(1)}% DHL — costo final estimado: ${fmtMoney(costoFinalUnit)} c/u (${fmtMoney(totalClpLinea)} la línea)`
        : "Ingresa una cantidad válida para ver el costo final estimado.";
      netoEstimado += totalClpLinea;
    });
  } else {
    datos.forEach(d => {
      d.fila.querySelector(".fi-linea-calculo").textContent = "Completa el costo unitario y el tipo de cambio para ver el costo final estimado.";
    });
  }

  const exento = document.getElementById("fi-exento-iva").checked;
  const iva = exento ? 0 : netoEstimado * 0.19;
  document.getElementById("fi-total-neto").textContent = fmtMoney(netoEstimado);
  document.getElementById("fi-total-iva").textContent = fmtMoney(iva);
  document.getElementById("fi-total-vivo").textContent = fmtMoney(netoEstimado + iva);
  return { neto: netoEstimado, iva, total: netoEstimado + iva };
}

function actualizarTotalesPaso3() {
  const { neto, iva, total } = fiRecalcular();
  document.getElementById("fi-total-neto-p3").textContent = fmtMoney(neto);
  document.getElementById("fi-total-iva-p3").textContent = fmtMoney(iva);
  document.getElementById("fi-total-vivo-p3").textContent = fmtMoney(total);
}

document.getElementById("fi-agregar-linea").addEventListener("click", () => {
  document.getElementById("fi-lineas").appendChild(filaLineaFacturaInternacional(state.productosCache));
  fiRecalcular();
});

["fi-tipo-cambio", "fi-shipping-usd", "fi-paypal-usd", "fi-dhl-clp", "fi-exento-iva"].forEach(id => {
  document.getElementById(id).addEventListener("input", fiRecalcular);
  document.getElementById(id).addEventListener("change", fiRecalcular);
});

function renderResultadosFi(texto) {
  const resultados = document.getElementById("fi-producto-resultados");
  const q = texto.trim();
  if (!q) {
    resultados.style.display = "none";
    return;
  }
  const coincidencias = state.productosCache.filter(p =>
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
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const p = state.productosCache.find(x => x.id === parseInt(item.dataset.id, 10));
      if (p) agregarLineaDesdeAutocomplete(p);
    });
  });
}

function agregarLineaDesdeAutocomplete(producto) {
  const filaExistente = Array.from(document.querySelectorAll("#fi-lineas .fac-linea-row"))
    .find(fila => parseInt(fila.querySelector(".fi-linea-producto-id").value, 10) === producto.id);
  if (filaExistente) {
    const inputCantidad = filaExistente.querySelector(".fi-linea-cantidad");
    inputCantidad.value = (parseFloat(inputCantidad.value) || 0) + 1;
  } else {
    document.getElementById("fi-lineas").appendChild(filaLineaFacturaInternacional(state.productosCache, {
      producto_id: producto.id, cantidad: 1,
    }));
  }
  fiRecalcular();
  toast(`Agregado: ${producto.nombre}`);
}

document.getElementById("fi-buscar-producto").addEventListener("input", (e) => {
  renderResultadosFi(e.target.value);
});
document.getElementById("fi-buscar-producto").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosFi(e.target.value);
});
document.getElementById("fi-buscar-producto").addEventListener("blur", () => {
  setTimeout(() => { document.getElementById("fi-producto-resultados").style.display = "none"; }, 150);
});

document.querySelectorAll('input[name="fi-condicion"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const condicion = document.querySelector('input[name="fi-condicion"]:checked').value;
    document.getElementById("fi-campos-mixto").style.display = condicion === "mixto" ? "grid" : "none";
    document.getElementById("fi-campos-credito").style.display = condicion === "contado" ? "none" : "grid";
  });
});

document.querySelectorAll(".pfr-plazos-rapidos [data-plazo-fi]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.getElementById("fi-plazo-dias").value = btn.dataset.plazoFi;
  });
});

// ---------------------------------------------------------------------------
// Terminar: recién acá se crea el registro real (POST /api/facturas/internacional)
// -- ver docstring de vista_factura_internacional en app.py para por qué no
// hay un "guardar para continuar más adelante" en esta ventana.
// ---------------------------------------------------------------------------

document.getElementById("fi-p3-terminar").addEventListener("click", async () => {
  if (state.guardando) return;
  const tipoCambio = document.getElementById("fi-tipo-cambio").value;
  if (!tipoCambio || Number(tipoCambio) <= 0) {
    toast("Ingresa el tipo de cambio de esta factura", true);
    mostrarPaso(1);
    return;
  }
  const filas = Array.from(document.querySelectorAll("#fi-lineas .fac-linea-row"));
  const lineas = [];
  for (const fila of filas) {
    const valorSelect = fila.querySelector(".fi-linea-producto-id").value;
    const cantidad = fila.querySelector(".fi-linea-cantidad").value;
    const costo_unitario_usd = fila.querySelector(".fi-linea-costo-usd").value;
    if (valorSelect === "__nuevo__") {
      const nombreNuevo = fila.querySelector(".fi-linea-nombre-nuevo").value.trim();
      if (!nombreNuevo) {
        toast("Describe el producto en cada línea marcada como \"No está en el catálogo\" (o quítala)", true);
        mostrarPaso(2);
        return;
      }
      lineas.push({ producto_nuevo_nombre: nombreNuevo, cantidad, costo_unitario_usd });
    } else if (!valorSelect) {
      toast("Selecciona un producto en cada línea (o quítala)", true);
      mostrarPaso(2);
      return;
    } else {
      lineas.push({ producto_id: valorSelect, cantidad, costo_unitario_usd });
    }
  }
  if (!lineas.length) {
    toast("Agrega al menos un producto a la factura internacional", true);
    mostrarPaso(2);
    return;
  }

  const numero = document.getElementById("fi-numero").value.trim();
  const proveedor_id = document.getElementById("fi-proveedor-id").value || null;
  const fecha = document.getElementById("fi-fecha").value;
  const exento_iva = document.getElementById("fi-exento-iva").checked;
  const condicion = document.querySelector('input[name="fi-condicion"]:checked').value;
  const btn = document.getElementById("fi-p3-terminar");
  state.guardando = true;
  btn.disabled = true;
  try {
    const creada = await api("/api/facturas/internacional", {
      method: "POST",
      body: JSON.stringify({
        numero, proveedor_id, fecha,
        tipo_cambio: Number(tipoCambio),
        shipping_usd: Number(document.getElementById("fi-shipping-usd").value || 0),
        paypal_usd: Number(document.getElementById("fi-paypal-usd").value || 0),
        dhl_clp: Number(document.getElementById("fi-dhl-clp").value || 0),
        lineas,
        condicion_pago: condicion,
        porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("fi-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
        plazo_dias_credito: Number(document.getElementById("fi-plazo-dias").value || 30),
        exento_iva,
      }),
    });
    toast(creada.estado === "aprobada"
      ? "Factura internacional aplicada: stock actualizado y Cuenta por Pagar generada"
      : "Factura internacional registrada — queda pendiente de recepción (falta vincular un producto nuevo, ver el aviso 📦)");
    window.location.href = "/finanzas";
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.guardando = false;
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

(async function init() {
  await cargarSesion();
  try {
    await cargarCatalogos();
  } catch (e) {
    toast(e.message, true);
  }
  document.getElementById("fi-lineas").appendChild(filaLineaFacturaInternacional(state.productosCache));
  mostrarPaso(1);
})();
