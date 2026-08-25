// ---------------------------------------------------------------------------
// Nueva ventana dedicada para registrar una factura de compra, por pasos
// (ver /facturas/nueva en app.py). Reemplaza al modal "Registrar factura de
// compra" de Finanzas como punto de entrada manual -- el taller ahora opera
// con una sola persona a cargo de Bodega y Facturas, así que esto se
// simplifica todo lo posible: sin textos explicativos de más, sin pasos que
// no aportan, crear un producto (con marca) queda siempre a mano en el
// mismo paso 2, y un repuesto se puede mandar derecho a una OT sin tener
// que pasar después por Operativo a descargarlo.
//
// Utilidades compartidas (fmtMoney, toast, escapeHtml, api, etc.) están
// duplicadas de finanzas.js/app.js a propósito -- cada módulo carga su
// propio JS, mismo criterio que ya usa el resto del sistema (ver comentario
// equivalente en finanzas.js).
// ---------------------------------------------------------------------------

const state = {
  sesion: { username: null, nombre: null },
  pendienteId: null,
  productosCache: [],
  proveedoresCache: [],
  guardando: false,
};

const RUBRO_GAV_LABEL = {
  generales: "Gastos generales",
  servicios_basicos: "Servicios básicos",
  combustible: "Combustible",
  bancarios: "Gastos bancarios",
  mantencion: "Mantención y equipos",
  otros: "Otros gastos",
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

function optionsRubroGavFactura(valorActual) {
  return Object.entries(RUBRO_GAV_LABEL)
    .map(([valor, label]) => `<option value="${valor}" ${valor === (valorActual || "") ? "selected" : ""}>${label}</option>`)
    .join("");
}

// ---------------------------------------------------------------------------
// Un mismo repuesto (categoría + modelo) puede tener varias marcas -- cada
// marca es un producto propio en el catálogo, con su propio stock y su
// propio precio (ej. "FRICTION KIT — 01J" en BOSCH y en GENÉRICA son dos
// productos distintos, ver marca en _crear_producto_desde_datos en
// app.py). Acá se agrupan solo para MOSTRARLOS juntos al armar la
// factura -- así, al elegir un repuesto que ya existe, siempre se
// pregunta explícitamente de qué marca es esta compra (con las marcas ya
// cargadas + la opción de agregar una nueva), en vez de asumir cualquiera.
// Productos sin categoría/modelo (ej. creados solo con "nombre") quedan
// sueltos, sin agrupar.
// ---------------------------------------------------------------------------

function agruparProductosPorRepuesto(productos) {
  const grupos = new Map();
  const sueltos = [];
  for (const p of productos) {
    const categoria = (p.categoria || "").trim();
    const modelo = (p.modelo || "").trim();
    if (!categoria && !modelo) {
      sueltos.push(p);
      continue;
    }
    const clave = `${categoria.toLowerCase()}|||${modelo.toLowerCase()}`;
    if (!grupos.has(clave)) grupos.set(clave, { categoria, modelo, variantes: [] });
    grupos.get(clave).variantes.push(p);
  }
  const listaGrupos = Array.from(grupos.values()).sort((a, b) =>
    (a.categoria + a.modelo).localeCompare(b.categoria + b.modelo));
  return { grupos: listaGrupos, sueltos };
}

// ---------------------------------------------------------------------------
// Tipo de documento: factura o boleta (ver Migración 35 en app.py). Una
// boleta siempre es al contado -- no tiene condición de pago que elegir,
// así que el paso 3 esconde ese bloque entero y se avisa con una nota en
// vez de dejar los campos de crédito/mixto ahí sin sentido.
// ---------------------------------------------------------------------------

function tipoDocumentoActual() {
  const marcado = document.querySelector('input[name="fn-tipo-documento"]:checked');
  return marcado ? marcado.value : "factura";
}

function sincronizarTipoDocumento() {
  const esBoleta = tipoDocumentoActual() === "boleta";
  document.getElementById("fn-numero-label").textContent = esBoleta ? "N° de boleta *" : "N° de factura *";
  document.getElementById("fn-condicion-pago-wrap").style.display = esBoleta ? "none" : "block";
  document.getElementById("fn-boleta-contado-nota").style.display = esBoleta ? "block" : "none";
  const btnTerminar = document.getElementById("fn-p3-terminar");
  if (btnTerminar) btnTerminar.textContent = esBoleta ? "Terminar boleta" : "Terminar factura";
}

document.querySelectorAll('input[name="fn-tipo-documento"]').forEach(radio => {
  radio.addEventListener("change", sincronizarTipoDocumento);
});

// ---------------------------------------------------------------------------
// Navegación por pasos
// ---------------------------------------------------------------------------

function mostrarPaso(n) {
  for (let i = 1; i <= 3; i++) {
    document.getElementById(`fn-paso-${i}`).style.display = i === n ? "block" : "none";
    document.querySelector(`#fn-pasos [data-paso="${i}"]`).classList.toggle("activo", i === n);
  }
  if (n === 3) {
    sincronizarTipoDocumento();
    actualizarTotalesPaso3();
  }
}

document.getElementById("fn-p1-siguiente").addEventListener("click", async () => {
  const numero = document.getElementById("fn-numero").value.trim();
  if (!numero) {
    toast("Ingresa el número de la factura antes de seguir", true);
    return;
  }
  const btn = document.getElementById("fn-p1-siguiente");
  btn.disabled = true;
  try {
    const params = state.pendienteId != null ? `?excluir_pendiente_id=${state.pendienteId}` : "";
    const check = await api(`/api/facturas/verificar-numero/${encodeURIComponent(numero)}${params}`);
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
document.getElementById("fn-p2-atras").addEventListener("click", () => mostrarPaso(1));
document.getElementById("fn-p2-siguiente").addEventListener("click", () => {
  const filas = Array.from(document.querySelectorAll("#fn-lineas .fac-linea-row"));
  if (!filas.length) {
    toast("Agrega al menos un producto a la factura", true);
    return;
  }
  mostrarPaso(3);
});
document.getElementById("fn-p3-atras").addEventListener("click", () => mostrarPaso(2));

// ---------------------------------------------------------------------------
// Salir / cerrar a medio camino: doble check con 3 opciones (ver
// /facturas/nueva en app.py y la petición original del taller).
// ---------------------------------------------------------------------------

function hayAlgoQuePerder() {
  const numero = document.getElementById("fn-numero").value.trim();
  const hayLineas = document.querySelectorAll("#fn-lineas .fac-linea-row").length > 0;
  return state.pendienteId != null || !!numero || hayLineas;
}

function intentarSalir() {
  if (!hayAlgoQuePerder()) {
    window.location.href = "/finanzas";
    return;
  }
  abrirModal("modal-fn-cerrar");
}

document.getElementById("fn-btn-salir").addEventListener("click", intentarSalir);
document.getElementById("fn-p1-salir").addEventListener("click", intentarSalir);

document.getElementById("fn-cerrar-cancelar").addEventListener("click", () => {
  cerrarModal("modal-fn-cerrar");
});

document.getElementById("fn-cerrar-descartar").addEventListener("click", async () => {
  try {
    if (state.pendienteId != null) {
      await api(`/api/facturas/pendientes-recepcion/${state.pendienteId}`, { method: "DELETE" });
    }
    window.location.href = "/finanzas";
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("fn-cerrar-guardar").addEventListener("click", async () => {
  const ok = await guardarBorrador();
  if (ok) window.location.href = "/finanzas";
});

// ---------------------------------------------------------------------------
// Proveedor: catálogo + crear uno nuevo al vuelo
// ---------------------------------------------------------------------------

async function cargarCatalogos() {
  const [dataProductos, dataProveedores] = await Promise.all([api("/api/productos"), api("/api/proveedores")]);
  state.productosCache = dataProductos.productos;
  state.proveedoresCache = dataProveedores.proveedores;
  repintarSelectProveedores();
}

function repintarSelectProveedores(seleccionar) {
  const select = document.getElementById("fn-proveedor-id");
  const actual = seleccionar != null ? seleccionar : select.value;
  select.innerHTML = '<option value="">Sin proveedor</option>' +
    state.proveedoresCache.map(p => `<option value="${p.id}">${escapeHtml(p.nombre)}</option>`).join("");
  if (actual) select.value = actual;
}

document.getElementById("fn-btn-nuevo-proveedor").addEventListener("click", () => {
  const form = document.getElementById("fn-nuevo-proveedor-form");
  form.style.display = form.style.display === "none" ? "block" : "none";
});
document.getElementById("fn-prov-cancelar").addEventListener("click", () => {
  document.getElementById("fn-nuevo-proveedor-form").style.display = "none";
});
document.getElementById("fn-prov-guardar").addEventListener("click", async () => {
  const nombre = document.getElementById("fn-prov-nombre").value.trim();
  if (!nombre) {
    toast("Escribe el nombre del proveedor", true);
    return;
  }
  try {
    const nuevo = await api("/api/proveedores", {
      method: "POST",
      body: JSON.stringify({
        nombre,
        rut: document.getElementById("fn-prov-rut").value.trim(),
        telefono: document.getElementById("fn-prov-telefono").value.trim(),
      }),
    });
    state.proveedoresCache = [...state.proveedoresCache, nuevo];
    repintarSelectProveedores(nuevo.id);
    document.getElementById("fn-nuevo-proveedor-form").style.display = "none";
    document.getElementById("fn-prov-nombre").value = "";
    document.getElementById("fn-prov-rut").value = "";
    document.getElementById("fn-prov-telefono").value = "";
    toast(`Proveedor "${nuevo.nombre}" creado`);
  } catch (e) {
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------------------
// Líneas de producto (repuesto a stock / repuesto directo a una OT / gasto
// general) -- ver filaLineaFacturaPendiente en finanzas.js, de donde nace
// esta versión (adaptada: acá el producto nuevo SIEMPRE se puede crear al
// vuelo, sin depender de que ya exista un pendiente guardado, y se agrega
// la opción de destino "directo a una OT").
// ---------------------------------------------------------------------------

let fnLineaContador = 0;

function filaLineaFacturaNueva(productos, linea) {
  const div = document.createElement("div");
  div.className = "fac-linea-row";
  const sinResolverAlAbrir = !!(linea && linea.producto_id == null && (linea.texto_original || linea.cantidad != null));
  const esGastoGeneralAlAbrir = !!(linea && linea.es_gasto_general);
  const esOtAlAbrir = !!(linea && linea.destino === "ot");
  const grupo = `fn-modo-${++fnLineaContador}`;
  let modoInicial = "producto";
  if (esGastoGeneralAlAbrir) modoInicial = "gasto_general";
  else if (esOtAlAbrir) modoInicial = "producto_ot";

  // Agrupados por repuesto (categoría + modelo, ver agruparProductosPorRepuesto
  // arriba) -- el select de "Producto" elige el REPUESTO (una sola opción
  // por categoría+modelo, sin importar cuántas marcas tenga cargadas), y un
  // segundo select de "Marca" (que aparece al lado, ver poblarSelectMarca
  // más abajo) es el que elige entre las marcas ya cargadas de ESE
  // repuesto en particular. 'grupos' queda en el closure para que ambos
  // selects (y sus handlers de "change") lo puedan indexar por posición.
  const { grupos, sueltos } = agruparProductosPorRepuesto(productos);
  const optionsSueltos = sueltos.map(p => `<option value="${p.id}">#${p.id_sistema} — ${escapeHtml(p.nombre)}</option>`).join("");
  const optionsGrupos = grupos.map((g, idx) =>
    `<option value="__grupo__:${idx}">${escapeHtml(g.categoria)} — ${escapeHtml(g.modelo)}</option>`
  ).join("");

  function opcionesMarca(idxGrupo) {
    const g = grupos[idxGrupo];
    if (!g) return "";
    return g.variantes
      .slice()
      .sort((a, b) => (a.marca || "").localeCompare(b.marca || ""))
      .map(p => `<option value="${p.id}">${escapeHtml(p.marca) || "(sin marca)"} — ${fmtMoney(p.costo_compra)} (stock: ${p.stock_disponible})</option>`)
      .join("");
  }

  div.innerHTML = `
    <div class="pfr-linea-modo-campo" style="grid-column:1/-1;margin-bottom:6px">
      <label class="toggle-critico">
        <input type="radio" name="${grupo}" class="fn-linea-modo-producto" value="producto" ${modoInicial === "producto" ? "checked" : ""}>
        Repuesto (va a stock)
      </label>
      <label class="toggle-critico">
        <input type="radio" name="${grupo}" class="fn-linea-modo-ot" value="producto_ot" ${modoInicial === "producto_ot" ? "checked" : ""}>
        Repuesto directo a una orden de trabajo
      </label>
      <label class="toggle-critico">
        <input type="radio" name="${grupo}" class="fn-linea-modo-gg" value="gasto_general" ${modoInicial === "gasto_general" ? "checked" : ""}>
        Gasto general — no va a inventario
      </label>
    </div>
    <div class="fn-modo-producto" style="display:${modoInicial === "gasto_general" ? "none" : "contents"}">
      <div class="campo fn-linea-producto-campo">
        <label>Producto</label>
        <select class="fn-linea-producto-id">
          <option value="">Selecciona un repuesto…</option>
          ${optionsSueltos}
          ${optionsGrupos}
        </select>
        <button type="button" class="btn-link fn-linea-btn-nuevo-repuesto">+ Repuesto nuevo (no está en el catálogo)</button>
        <input type="hidden" class="fn-linea-producto-final-id" value="">
        <input type="text" class="fn-linea-nombre-nuevo" placeholder="¿Qué producto es? (ej. Kit de embrague ABC 2020)"
               style="display:none;margin-top:6px"
               value="${linea && linea.texto_original ? escapeHtml(linea.texto_original) : ""}">
        <button type="button" class="btn btn-secundario btn-chico fn-linea-crear-producto" style="display:none;margin-top:6px">
          Crear este producto
        </button>
      </div>
      <div class="campo fn-linea-marca-campo">
        <label>Marca</label>
        <select class="fn-linea-marca-id" disabled>
          <option value="">— elige un repuesto primero —</option>
        </select>
        <button type="button" class="btn-link fn-linea-btn-nueva-marca" style="display:none">+ Nueva marca de este repuesto…</button>
      </div>
      <div class="campo fn-linea-cantidad-campo">
        <label>Cantidad</label>
        <input type="number" class="fn-linea-cantidad" min="1" value="${linea && linea.cantidad != null ? linea.cantidad : 1}">
      </div>
      <div class="campo fn-linea-precio-campo">
        <label>Precio unit. ($ CLP)</label>
        <input type="number" class="fn-linea-precio" min="0" step="1" value="${linea && linea.precio_unitario != null ? linea.precio_unitario : 0}">
      </div>
      <div class="fn-linea-crear-form" style="display:none;margin-top:6px;grid-column:1/-1">
        <p class="ayuda-inline fn-linea-marca-hint" style="display:none;margin:0 0 6px"></p>
        <input type="text" class="fn-crear-categoria" placeholder="Modelo (ej. JF010)" style="margin-bottom:4px">
        <input type="text" class="fn-crear-modelo" placeholder="Detalle (ej. Pulley Set)" style="margin-bottom:4px">
        <input type="text" class="fn-crear-marca" placeholder="Marca (ej. BOSCH — obligatoria; si no tiene, escribe GENÉRICA)"
               value="${document.getElementById("fn-marca-todas") ? escapeHtml(document.getElementById("fn-marca-todas").value.trim()) : ""}" style="margin-bottom:4px">
        <p class="ayuda-inline fn-crear-marca-hint" style="display:none;margin:0 0 6px"></p>
        <input type="text" class="fn-crear-codigo-interno" placeholder="Código interno / N° de parte del fabricante" style="margin-bottom:4px">
        <button type="button" class="btn btn-primary btn-chico fn-crear-confirmar">Crear y usar</button>
      </div>
      <div class="campo fn-modo-ot-extra" style="display:${modoInicial === "producto_ot" ? "block" : "none"};grid-column:1/-1">
        <label>N° de orden de trabajo</label>
        <div class="campo-select-mas">
          <input type="number" class="fn-linea-ot-numero" min="1" placeholder="Ej: 4521" value="${linea && linea.orden_id ? linea.orden_id : ""}">
          <button type="button" class="btn-link fn-linea-ot-buscar">Buscar</button>
        </div>
        <p class="ayuda-inline fn-linea-ot-info"></p>
      </div>
    </div>
    <div class="fn-modo-gasto-general" style="display:${modoInicial === "gasto_general" ? "contents" : "none"}">
      <div class="campo fn-linea-descripcion-gg-campo">
        <label>Descripción</label>
        <input type="text" class="fn-linea-descripcion-gg" placeholder="Ej: Bencina para el generador"
               value="${linea && linea.descripcion ? escapeHtml(linea.descripcion) : ""}">
      </div>
      <div class="campo fn-linea-rubro-gg-campo">
        <label>Rubro</label>
        <select class="fn-linea-rubro-gg">
          <option value="">Selecciona…</option>
          ${optionsRubroGavFactura(linea ? linea.rubro_gav : "")}
        </select>
      </div>
      <div class="campo fn-linea-monto-gg-campo">
        <label>Monto ($ CLP)</label>
        <input type="number" class="fn-linea-monto-gg" min="0" step="1" value="${linea && linea.monto != null ? linea.monto : 0}">
      </div>
    </div>
    <button type="button" class="fac-linea-quitar">Quitar</button>
  `;

  const select = div.querySelector(".fn-linea-producto-id");
  const selectMarca = div.querySelector(".fn-linea-marca-id");
  const btnNuevoRepuesto = div.querySelector(".fn-linea-btn-nuevo-repuesto");
  const btnNuevaMarca = div.querySelector(".fn-linea-btn-nueva-marca");
  const finalId = div.querySelector(".fn-linea-producto-final-id");
  const inputNombreNuevo = div.querySelector(".fn-linea-nombre-nuevo");
  const btnCrear = div.querySelector(".fn-linea-crear-producto");
  const formCrear = div.querySelector(".fn-linea-crear-form");
  const marcaHint = div.querySelector(".fn-linea-marca-hint");
  const inputCrearCategoria = div.querySelector(".fn-crear-categoria");
  const inputCrearModelo = div.querySelector(".fn-crear-modelo");
  const inputCrearMarca = div.querySelector(".fn-crear-marca");
  const radioProducto = div.querySelector(".fn-linea-modo-producto");
  const radioOt = div.querySelector(".fn-linea-modo-ot");
  const radioGG = div.querySelector(".fn-linea-modo-gg");
  const bloqueProducto = div.querySelector(".fn-modo-producto");
  const bloqueGG = div.querySelector(".fn-modo-gasto-general");
  const bloqueOtExtra = div.querySelector(".fn-modo-ot-extra");

  // El repuesto (categoría+modelo) y la marca de ESE repuesto ahora se
  // eligen en dos selects separados -- "Producto" primero, y recién ahí
  // "Marca" se llena con las marcas ya cargadas de ese repuesto en
  // particular (más el botón "+ Nueva marca" debajo). El id de producto
  // que de verdad se manda al guardar queda en el input oculto
  // 'fn-linea-producto-final-id', porque el value del select "Producto"
  // ahora es un índice sintético (__grupo__:N) cuando agrupa varias
  // marcas, no un id real.
  function poblarSelectMarca(idxGrupo) {
    selectMarca.innerHTML = '<option value="">Selecciona una marca…</option>' + opcionesMarca(idxGrupo);
  }

  function ocultarFormularioCrear() {
    inputNombreNuevo.style.display = "none";
    btnCrear.style.display = "none";
    formCrear.style.display = "none";
    marcaHint.style.display = "none";
    inputCrearCategoria.readOnly = false;
    inputCrearModelo.readOnly = false;
  }

  function sincronizarSeleccionProducto() {
    const valor = select.value;
    ocultarFormularioCrear();
    btnNuevaMarca.style.display = "none";
    if (valor.startsWith("__grupo__:")) {
      const idx = parseInt(valor.split(":")[1], 10);
      poblarSelectMarca(idx);
      selectMarca.disabled = false;
      selectMarca.value = "";
      btnNuevaMarca.style.display = "inline-block";
      finalId.value = "";
    } else {
      selectMarca.innerHTML = '<option value="">— elige un repuesto primero —</option>';
      selectMarca.disabled = true;
      finalId.value = valor; // "" o el id de un producto suelto (sin categoría/modelo)
    }
  }

  function abrirNuevoRepuesto() {
    // Puede llegar acá justo después de haber abierto "+ Nueva marca" (que
    // deja categoría/modelo de solo lectura, precargados con el repuesto
    // elegido) -- para un repuesto de verdad nuevo esos dos campos tienen
    // que quedar libres para escribirse desde cero. Si ya estaba en este
    // mismo modo, no se pisa lo que el usuario ya haya escrito.
    if (finalId.value !== "__nuevo__") {
      inputCrearCategoria.value = "";
      inputCrearModelo.value = "";
    }
    inputCrearCategoria.readOnly = false;
    inputCrearModelo.readOnly = false;
    select.value = "";
    selectMarca.innerHTML = '<option value="">— elige un repuesto primero —</option>';
    selectMarca.disabled = true;
    btnNuevaMarca.style.display = "none";
    finalId.value = "__nuevo__";
    inputNombreNuevo.style.display = "block";
    btnCrear.style.display = "inline-block";
  }

  function abrirNuevaMarca() {
    const valor = select.value;
    if (!valor.startsWith("__grupo__:")) return;
    const idx = parseInt(valor.split(":")[1], 10);
    const g = grupos[idx];
    // Repuesto que ya existe (categoría/modelo ya identificados por el
    // repuesto elegido) pero con una marca todavía no cargada -- se salta
    // directo al formulario de creación, con categoría/modelo ya fijos (de
    // solo lectura: no hace falta volver a tipearlos) y el foco puesto en
    // lo único que de verdad falta: la marca (y, si quiere, el código
    // interno).
    inputCrearCategoria.value = g ? g.categoria : "";
    inputCrearModelo.value = g ? g.modelo : "";
    inputCrearCategoria.readOnly = true;
    inputCrearModelo.readOnly = true;
    formCrear.style.display = "block";
    marcaHint.textContent = `Nueva marca para "${g ? `${g.categoria} — ${g.modelo}` : "este repuesto"}" -- especifica la marca de esta compra en particular.`;
    marcaHint.style.display = "block";
    finalId.value = "__pendiente_nueva_marca__";
    inputCrearMarca.focus();
  }

  function sincronizarModo() {
    const esGG = radioGG.checked;
    const esOt = radioOt.checked;
    bloqueProducto.style.display = esGG ? "none" : "contents";
    bloqueGG.style.display = esGG ? "contents" : "none";
    bloqueOtExtra.style.display = esOt ? "block" : "none";
    actualizarTotalFacturaNueva();
  }

  if (linea && linea.producto_id != null) {
    const idxGrupo = grupos.findIndex(g => g.variantes.some(v => Number(v.id) === Number(linea.producto_id)));
    if (idxGrupo >= 0) {
      select.value = `__grupo__:${idxGrupo}`;
      poblarSelectMarca(idxGrupo);
      selectMarca.disabled = false;
      selectMarca.value = linea.producto_id;
      btnNuevaMarca.style.display = "inline-block";
      finalId.value = linea.producto_id;
    } else {
      select.value = linea.producto_id;
      finalId.value = linea.producto_id;
    }
  } else if (sinResolverAlAbrir) {
    abrirNuevoRepuesto();
  } else {
    sincronizarSeleccionProducto();
  }

  select.addEventListener("change", sincronizarSeleccionProducto);
  selectMarca.addEventListener("change", () => { finalId.value = selectMarca.value; });
  btnNuevoRepuesto.addEventListener("click", abrirNuevoRepuesto);
  btnNuevaMarca.addEventListener("click", abrirNuevaMarca);
  radioProducto.addEventListener("change", sincronizarModo);
  radioOt.addEventListener("change", sincronizarModo);
  radioGG.addEventListener("change", sincronizarModo);
  div.querySelector(".fac-linea-quitar").addEventListener("click", () => {
    div.remove();
    actualizarTotalFacturaNueva();
  });
  div.querySelector(".fn-linea-cantidad").addEventListener("input", actualizarTotalFacturaNueva);
  div.querySelector(".fn-linea-precio").addEventListener("input", actualizarTotalFacturaNueva);
  div.querySelector(".fn-linea-monto-gg").addEventListener("input", actualizarTotalFacturaNueva);

  // Buscar la OT antes de guardar, solo para mostrar de inmediato a quién
  // pertenece (cliente/equipo/estado) y evitar mandar un número que no
  // existe o una OT ya cerrada -- la validación real (la que de verdad
  // importa) siempre la hace el backend al guardar/aprobar.
  div.querySelector(".fn-linea-ot-buscar").addEventListener("click", async () => {
    const numeroOt = div.querySelector(".fn-linea-ot-numero").value.trim();
    const info = div.querySelector(".fn-linea-ot-info");
    if (!numeroOt) {
      info.textContent = "Escribe el número de la OT";
      return;
    }
    try {
      const orden = await api(`/api/operativo/ordenes/${numeroOt}`);
      const cliente = orden.cliente ? orden.cliente.nombre : "—";
      const equipo = orden.equipo ? `${orden.equipo.marca || ""} ${orden.equipo.modelo || ""}`.trim() : "—";
      info.textContent = `OT #${orden.id} — ${cliente} — ${equipo} — estado: ${orden.estado}`;
    } catch (e) {
      info.textContent = `No se encontró esa OT (${e.message})`;
    }
  });

  // Aviso en vivo: si el Modelo+Detalle que se está escribiendo ya existe
  // en el catálogo con otra(s) marca(s), se avisa de inmediato -- ayuda a
  // decidir si es una marca nueva de verdad o conviene revisar antes de
  // duplicar por un typo.
  const inputCategoria = div.querySelector(".fn-crear-categoria");
  const inputModelo = div.querySelector(".fn-crear-modelo");
  const hintMarca = div.querySelector(".fn-crear-marca-hint");
  function actualizarHintVariantes() {
    const categoria = inputCategoria.value.trim().toLowerCase();
    const modelo = inputModelo.value.trim().toLowerCase();
    if (!categoria && !modelo) {
      hintMarca.style.display = "none";
      return;
    }
    const variantes = productos.filter(p =>
      (p.categoria || "").trim().toLowerCase() === categoria &&
      (p.modelo || "").trim().toLowerCase() === modelo
    );
    if (!variantes.length) {
      hintMarca.style.display = "none";
      return;
    }
    const detalle = variantes
      .map(p => `${p.marca ? escapeHtml(p.marca) : "sin marca (repuesto antiguo)"} — ${fmtMoney(p.costo_compra)}`)
      .join(" · ");
    hintMarca.innerHTML = `Ya existe este modelo/detalle en: ${detalle}. Si es la misma marca, no crees uno nuevo -- selecciónalo arriba de la lista de productos.`;
    hintMarca.style.display = "block";
  }
  inputCategoria.addEventListener("input", actualizarHintVariantes);
  inputModelo.addEventListener("input", actualizarHintVariantes);

  btnCrear.addEventListener("click", () => {
    formCrear.style.display = formCrear.style.display === "none" ? "block" : "none";
    if (formCrear.style.display === "block" && !div.querySelector(".fn-crear-modelo").value) {
      div.querySelector(".fn-crear-modelo").value = inputNombreNuevo.value || "";
    }
    if (formCrear.style.display === "block") actualizarHintVariantes();
  });

  div.querySelector(".fn-crear-confirmar").addEventListener("click", async () => {
    const codigoInterno = div.querySelector(".fn-crear-codigo-interno").value.trim();
    const categoria = div.querySelector(".fn-crear-categoria").value.trim();
    const modelo = div.querySelector(".fn-crear-modelo").value.trim();
    const marca = div.querySelector(".fn-crear-marca").value.trim();
    if (!categoria && !modelo) {
      toast("Ingresa al menos modelo o detalle para crear el producto", true);
      return;
    }
    if (!marca) {
      toast('Escribe una marca para este repuesto (ej. BOSCH, o "GENÉRICA" si no tiene una marca real).', true);
      return;
    }
    try {
      const productoCreado = await api("/api/productos", {
        method: "POST",
        body: JSON.stringify({ codigo_interno: codigoInterno, categoria, modelo, marca }),
      });
      toast(`Producto "${productoCreado.nombre}" creado`);
      state.productosCache = [...state.productosCache, productoCreado];
      const cantidadActual = div.querySelector(".fn-linea-cantidad").value;
      const precioActual = div.querySelector(".fn-linea-precio").value;
      const modoActual = radioOt.checked ? "ot" : (radioGG.checked ? "gg" : "producto");
      const otNumeroActual = div.querySelector(".fn-linea-ot-numero").value;
      const nuevaFila = filaLineaFacturaNueva(state.productosCache, {
        producto_id: productoCreado.id, cantidad: cantidadActual, precio_unitario: precioActual,
        destino: modoActual === "ot" ? "ot" : undefined, orden_id: otNumeroActual || undefined,
      });
      div.replaceWith(nuevaFila);
      actualizarTotalFacturaNueva();
    } catch (err) {
      toast(err.message, true);
    }
  });

  return div;
}

document.getElementById("fn-agregar-linea").addEventListener("click", () => {
  document.getElementById("fn-lineas").appendChild(filaLineaFacturaNueva(state.productosCache));
  actualizarTotalFacturaNueva();
});

document.getElementById("fn-btn-aplicar-marca-todas").addEventListener("click", () => {
  const marca = document.getElementById("fn-marca-todas").value.trim();
  const campos = document.querySelectorAll("#fn-lineas .fn-crear-marca");
  campos.forEach(input => { input.value = marca; });
  toast(marca
    ? `Marca "${marca}" aplicada a ${campos.length} producto(s) nuevo(s) en pantalla`
    : "Se limpió la marca de los productos nuevos en pantalla");
});

// ---------------------------------------------------------------------------
// Buscador de producto para agregar líneas
// ---------------------------------------------------------------------------

function renderResultadosFn(texto) {
  const resultados = document.getElementById("fn-producto-resultados");
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
      <span class="autocomplete-item-meta">Marca: ${p.marca ? escapeHtml(p.marca) : "—"} · Stock: ${p.stock_disponible}</span>
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
  const filaExistente = Array.from(document.querySelectorAll("#fn-lineas .fac-linea-row"))
    .find(fila => parseInt(fila.querySelector(".fn-linea-producto-final-id").value, 10) === producto.id);
  if (filaExistente) {
    const inputCantidad = filaExistente.querySelector(".fn-linea-cantidad");
    inputCantidad.value = (parseFloat(inputCantidad.value) || 0) + 1;
  } else {
    document.getElementById("fn-lineas").appendChild(filaLineaFacturaNueva(state.productosCache, {
      producto_id: producto.id,
      cantidad: 1,
      precio_unitario: producto.costo_compra || 0,
    }));
  }
  actualizarTotalFacturaNueva();
  toast(`Agregado: ${producto.nombre}`);
}

document.getElementById("fn-buscar-producto").addEventListener("input", (e) => {
  renderResultadosFn(e.target.value);
});
document.getElementById("fn-buscar-producto").addEventListener("focus", (e) => {
  if (e.target.value.trim()) renderResultadosFn(e.target.value);
});
document.getElementById("fn-buscar-producto").addEventListener("blur", () => {
  setTimeout(() => { document.getElementById("fn-producto-resultados").style.display = "none"; }, 150);
});

// ---------------------------------------------------------------------------
// Totales en vivo (neto / IVA / total) -- mismo criterio que finanzas.js:
// el precio de cada línea es el NETO.
// ---------------------------------------------------------------------------

function calcularNetoFactura() {
  const filas = Array.from(document.querySelectorAll("#fn-lineas .fac-linea-row"));
  return filas.reduce((acc, fila) => {
    if (fila.querySelector(".fn-linea-modo-gg").checked) {
      return acc + (parseFloat(fila.querySelector(".fn-linea-monto-gg").value) || 0);
    }
    const cantidad = parseFloat(fila.querySelector(".fn-linea-cantidad").value) || 0;
    const precio = parseFloat(fila.querySelector(".fn-linea-precio").value) || 0;
    return acc + cantidad * precio;
  }, 0);
}

function actualizarTotalFacturaNueva() {
  const neto = calcularNetoFactura();
  const exento = document.getElementById("fn-exento-iva").checked;
  const iva = exento ? 0 : Math.round(neto * 0.19 * 100) / 100;
  const total = Math.round((neto + iva) * 100) / 100;
  document.getElementById("fn-total-neto").textContent = fmtMoney(neto);
  document.getElementById("fn-total-iva").textContent = fmtMoney(iva);
  document.getElementById("fn-total-vivo").textContent = fmtMoney(total);
}

function actualizarTotalesPaso3() {
  actualizarTotalFacturaNueva();
  const neto = calcularNetoFactura();
  const exento = document.getElementById("fn-exento-iva").checked;
  const iva = exento ? 0 : Math.round(neto * 0.19 * 100) / 100;
  const total = Math.round((neto + iva) * 100) / 100;
  document.getElementById("fn-total-neto-p3").textContent = fmtMoney(neto);
  document.getElementById("fn-total-iva-p3").textContent = fmtMoney(iva);
  document.getElementById("fn-total-vivo-p3").textContent = fmtMoney(total);
}

document.getElementById("fn-exento-iva").addEventListener("change", actualizarTotalFacturaNueva);

document.querySelectorAll('input[name="fn-condicion"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const condicion = document.querySelector('input[name="fn-condicion"]:checked').value;
    document.getElementById("fn-campos-mixto").style.display = condicion === "mixto" ? "grid" : "none";
    document.getElementById("fn-campos-credito").style.display = condicion === "contado" ? "none" : "grid";
  });
});

document.querySelectorAll(".pfr-plazos-rapidos [data-plazo]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.getElementById("fn-plazo-dias").value = btn.dataset.plazo;
  });
});

// ---------------------------------------------------------------------------
// Armar el payload de líneas a partir de lo que hay en pantalla
// ---------------------------------------------------------------------------

function leerLineasDesdePantalla() {
  const filas = Array.from(document.querySelectorAll("#fn-lineas .fac-linea-row"));
  const lineas = [];
  for (const fila of filas) {
    if (fila.querySelector(".fn-linea-modo-gg").checked) {
      const descripcion = fila.querySelector(".fn-linea-descripcion-gg").value.trim();
      const rubro_gav = fila.querySelector(".fn-linea-rubro-gg").value;
      const monto = fila.querySelector(".fn-linea-monto-gg").value;
      if (!descripcion) return { error: 'Describe cada línea marcada como "Gasto general" (ej. "Bencina para el generador")' };
      if (!rubro_gav) return { error: 'Elige un rubro para cada línea marcada como "Gasto general"' };
      lineas.push({ es_gasto_general: true, descripcion, rubro_gav, monto });
      continue;
    }
    const valorSelect = fila.querySelector(".fn-linea-producto-final-id").value;
    const cantidad = fila.querySelector(".fn-linea-cantidad").value;
    const precio_unitario = fila.querySelector(".fn-linea-precio").value;
    const esOt = fila.querySelector(".fn-linea-modo-ot").checked;
    const extraDestino = {};
    if (esOt) {
      const ordenId = fila.querySelector(".fn-linea-ot-numero").value.trim();
      if (!ordenId) return { error: "Indica el número de OT en cada línea marcada como \"directo a una orden de trabajo\"" };
      extraDestino.destino = "ot";
      extraDestino.orden_id = ordenId;
    }
    if (valorSelect === "__nuevo__") {
      const nombreNuevo = fila.querySelector(".fn-linea-nombre-nuevo").value.trim();
      if (!nombreNuevo) return { error: 'Describe el producto en cada línea marcada como "No está en el catálogo" (o crea el producto con el botón, o quita la línea)' };
      lineas.push({ producto_nuevo_nombre: nombreNuevo, cantidad, precio_unitario, ...extraDestino });
    } else if (valorSelect === "__pendiente_nueva_marca__") {
      // Eligió "+ Nueva marca de este repuesto" pero todavía no la creó
      // con el botón "Crear y usar" -- ese valor sintético no es un
      // producto_id real, no se puede mandar tal cual.
      return { error: 'Termina de crear la marca nueva en cada línea marcada así (botón "Crear y usar"), o quita la línea' };
    } else if (!valorSelect) {
      return { error: "Selecciona un producto (y su marca) en cada línea, o quítala" };
    } else {
      lineas.push({ producto_id: valorSelect, cantidad, precio_unitario, ...extraDestino });
    }
  }
  return { lineas };
}

function leerCondicionPago() {
  if (tipoDocumentoActual() === "boleta") {
    // Una boleta siempre es al contado -- ni se muestran los campos de
    // crédito/mixto en el paso 3 (ver sincronizarTipoDocumento), así que
    // acá se fuerza directo en vez de leer un radio que no está visible.
    return { condicion_pago: "contado", porcentaje_contado: 100, plazo_dias_credito: 30 };
  }
  const condicion = document.querySelector('input[name="fn-condicion"]:checked').value;
  return {
    condicion_pago: condicion,
    porcentaje_contado: condicion === "mixto" ? Number(document.getElementById("fn-porcentaje-contado").value) : (condicion === "contado" ? 100 : 0),
    plazo_dias_credito: Number(document.getElementById("fn-plazo-dias").value || 30),
  };
}

// ---------------------------------------------------------------------------
// Guardar para continuar más adelante: crea o actualiza un pendiente real
// en la base de datos (ver POST/PUT /api/facturas/pendientes-recepcion) --
// a propósito NO intenta aplicarla, queda "pendiente" hasta que se retome
// y se termine desde el paso 3.
// ---------------------------------------------------------------------------

async function guardarBorrador() {
  const numero = document.getElementById("fn-numero").value.trim();
  if (!numero) {
    toast("Ingresa el número de factura antes de guardar", true);
    return false;
  }
  const { lineas, error } = leerLineasDesdePantalla();
  if (error) {
    toast(error, true);
    return false;
  }
  if (!lineas.length) {
    toast("Agrega al menos un producto antes de guardar para continuar más adelante", true);
    return false;
  }
  const proveedor_id = document.getElementById("fn-proveedor-id").value || null;
  const fecha = document.getElementById("fn-fecha").value;
  const exento_iva = document.getElementById("fn-exento-iva").checked;
  const tipo_documento = tipoDocumentoActual();
  try {
    if (state.pendienteId == null) {
      const condicion = leerCondicionPago();
      const creada = await api("/api/facturas/pendientes-recepcion", {
        method: "POST",
        body: JSON.stringify({ numero, proveedor_id, fecha, lineas, exento_iva, tipo_documento, ...condicion }),
      });
      // Puede haber quedado aplicada de una si todos los productos ya
      // estaban resueltos (ver _crear_pendiente_y_aplicar_si_posible) --
      // igual sirve como "guardado", solo que ya no hace falta retomarla.
      state.pendienteId = creada.estado === "aprobada" ? null : creada.id;
      toast(creada.estado === "aprobada" ? "Factura aplicada de una — no queda nada pendiente" : "Factura guardada — puedes continuarla más adelante desde Facturas");
    } else {
      await api(`/api/facturas/pendientes-recepcion/${state.pendienteId}`, {
        method: "PUT",
        body: JSON.stringify({ numero, proveedor_id, fecha, lineas, exento_iva, tipo_documento }),
      });
      toast("Factura guardada — puedes continuarla más adelante desde Facturas");
    }
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

document.getElementById("fn-p3-guardar-despues").addEventListener("click", async () => {
  const ok = await guardarBorrador();
  if (ok) window.location.href = "/finanzas";
});

// ---------------------------------------------------------------------------
// Terminar factura: guarda todo y la aplica de inmediato (stock + Cuenta
// por Pagar) -- pensado para que, con un solo operador a cargo de Bodega y
// Facturas, no haga falta un segundo paso de "aprobar" aparte.
// ---------------------------------------------------------------------------

document.getElementById("fn-p3-terminar").addEventListener("click", async () => {
  if (state.guardando) return;
  const numero = document.getElementById("fn-numero").value.trim();
  if (!numero) {
    toast("Ingresa el número de factura antes de terminar", true);
    return;
  }
  const { lineas, error } = leerLineasDesdePantalla();
  if (error) {
    toast(error, true);
    return;
  }
  if (!lineas.length) {
    toast("Agrega al menos un producto a la factura", true);
    return;
  }
  const proveedor_id = document.getElementById("fn-proveedor-id").value || null;
  const fecha = document.getElementById("fn-fecha").value;
  const exento_iva = document.getElementById("fn-exento-iva").checked;
  const tipo_documento = tipoDocumentoActual();
  const condicion = leerCondicionPago();
  const btn = document.getElementById("fn-p3-terminar");
  state.guardando = true;
  btn.disabled = true;
  try {
    let resultado;
    if (state.pendienteId == null) {
      resultado = await api("/api/facturas/pendientes-recepcion", {
        method: "POST",
        body: JSON.stringify({ numero, proveedor_id, fecha, lineas, exento_iva, tipo_documento, ...condicion }),
      });
      if (resultado.estado !== "aprobada") {
        // Solo puede pasar si quedó una línea sin producto resuelto -- con
        // el botón "Crear este producto" siempre a mano en el paso 2, no
        // debería ocurrir, pero si pasa se avisa igual en vez de fallar
        // en silencio.
        state.pendienteId = resultado.id;
        toast("No se pudo aplicar todavía: falta crear/vincular un producto en alguna línea. La factura quedó guardada como pendiente.", true);
        return;
      }
    } else {
      resultado = await api(`/api/facturas/pendientes-recepcion/${state.pendienteId}`, {
        method: "PUT",
        body: JSON.stringify({ numero, proveedor_id, fecha, lineas, exento_iva, tipo_documento, aplicar: true }),
      });
    }
    toast("Factura terminada: stock actualizado y Cuenta por Pagar generada");
    window.location.href = "/finanzas";
  } catch (e) {
    toast(e.message, true);
  } finally {
    state.guardando = false;
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Retomar un borrador guardado (?pendiente=<id>)
// ---------------------------------------------------------------------------

async function retomarBorrador(id) {
  const p = await api(`/api/facturas/pendientes-recepcion/${id}`);
  if (p.estado !== "pendiente") {
    toast("Esa factura ya no está pendiente — vuelve a Facturas para verla", true);
    window.location.href = "/finanzas";
    return;
  }
  state.pendienteId = p.id;
  document.getElementById("fn-numero").value = p.numero || "";
  document.getElementById("fn-proveedor-id").value = p.proveedor_id || "";
  document.getElementById("fn-fecha").value = (p.fecha || "").slice(0, 10);
  document.getElementById("fn-exento-iva").checked = !!p.exento_iva;
  const radioTipo = document.querySelector(`input[name="fn-tipo-documento"][value="${p.tipo_documento === "boleta" ? "boleta" : "factura"}"]`);
  if (radioTipo) radioTipo.checked = true;
  sincronizarTipoDocumento();
  const cont = document.getElementById("fn-lineas");
  cont.innerHTML = "";
  (p.lineas || []).forEach(linea => {
    cont.appendChild(filaLineaFacturaNueva(state.productosCache, linea));
  });
  actualizarTotalFacturaNueva();
  toast(`Retomando factura #${p.id} — completa lo que falte`);
}

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
  const idPendiente = new URLSearchParams(window.location.search).get("pendiente");
  if (idPendiente) {
    try {
      await retomarBorrador(parseInt(idPendiente, 10));
    } catch (e) {
      toast(e.message, true);
    }
  }
  mostrarPaso(1);
})();
