// ---------------------------------------------------------------------------
// Helpers de gráficos livianos (SVG puro, sin librerías externas) para los
// dashboards de Panel del Dueño, Operativo y Finanzas. Cada función devuelve
// un string de HTML/SVG listo para inyectar con innerHTML — mismo patrón
// simple que ya usa el resto del código (no hay un sistema de componentes).
//
// Todas las funciones build* emiten además elementos con atributo data-tt
// (JSON con título + filas a mostrar) para que el hover global de más abajo
// dibuje un tooltip — así ningún sitio que llama a innerHTML necesita tocarse.
// ---------------------------------------------------------------------------

const MESES_LABEL_CORTO = {
  "01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr", "05": "May", "06": "Jun",
  "07": "Jul", "08": "Ago", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic",
};

function chartFmtCompacto(n) {
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(n % 1e6 === 0 ? 0 : 1) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(0) + "k";
  return String(Math.round(n));
}

function escapeHtmlChart(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function chartFmtMoney(n) {
  return "$" + Math.round(n || 0).toLocaleString("es-CL");
}

// ---------------------------------------------------------------------------
// Tooltip interactivo — infraestructura compartida por todos los gráficos.
// data-tt lleva un JSON: { t: "título", r: [{c: "color"|null, l: "label", v: "valor ya formateado"}] }
// ---------------------------------------------------------------------------

function chartAttrJson(obj) {
  return JSON.stringify(obj).replace(/&/g, "&amp;").replace(/'/g, "&#39;");
}

function chartTooltipEnsure() {
  let el = document.getElementById("chart-tooltip-global");
  if (!el) {
    el = document.createElement("div");
    el.id = "chart-tooltip-global";
    el.className = "chart-tooltip";
    document.body.appendChild(el);
  }
  return el;
}

function chartTooltipRender(el, data) {
  el.textContent = "";
  if (data.t) {
    const titulo = document.createElement("div");
    titulo.className = "chart-tooltip-title";
    titulo.textContent = data.t;
    el.appendChild(titulo);
  }
  (data.r || []).forEach((fila) => {
    const row = document.createElement("div");
    row.className = "chart-tooltip-row";
    if (fila.c) {
      const key = document.createElement("span");
      key.className = "chart-tooltip-key";
      key.style.background = fila.c;
      row.appendChild(key);
    }
    const label = document.createElement("span");
    label.className = "chart-tooltip-label";
    label.textContent = fila.l || "";
    row.appendChild(label);
    const valor = document.createElement("span");
    valor.className = "chart-tooltip-value";
    valor.textContent = fila.v || "";
    row.appendChild(valor);
    el.appendChild(row);
  });
}

function chartTooltipShow(evt, target) {
  const raw = target.getAttribute("data-tt");
  if (!raw) return;
  let data;
  try {
    data = JSON.parse(raw.replace(/&#39;/g, "'").replace(/&amp;/g, "&"));
  } catch (e) {
    return;
  }
  const el = chartTooltipEnsure();
  chartTooltipRender(el, data);
  el.classList.add("chart-tooltip--visible");
  chartTooltipMove(evt);
}

function chartTooltipMove(evt) {
  const el = document.getElementById("chart-tooltip-global");
  if (!el || !el.classList.contains("chart-tooltip--visible")) return;
  const margen = 14;
  const rect = el.getBoundingClientRect();
  let x = evt.clientX + margen;
  let y = evt.clientY + margen;
  if (x + rect.width > window.innerWidth - 6) x = evt.clientX - rect.width - margen;
  if (y + rect.height > window.innerHeight - 6) y = evt.clientY - rect.height - margen;
  if (x < 6) x = 6;
  if (y < 6) y = 6;
  el.style.transform = `translate(${x}px, ${y}px)`;
}

function chartTooltipHide() {
  const el = document.getElementById("chart-tooltip-global");
  if (el) el.classList.remove("chart-tooltip--visible");
}

(function chartTooltipWireGlobalListeners() {
  document.addEventListener("mouseover", (evt) => {
    const target = evt.target.closest("[data-tt]");
    if (target) chartTooltipShow(evt, target);
  });
  document.addEventListener("mousemove", (evt) => {
    if (evt.target.closest("[data-tt]")) chartTooltipMove(evt);
  });
  document.addEventListener("mouseout", (evt) => {
    const target = evt.target.closest("[data-tt]");
    if (target && !(evt.relatedTarget && evt.relatedTarget.closest("[data-tt]") === target)) {
      chartTooltipHide();
    }
  });
  document.addEventListener("scroll", chartTooltipHide, true);
})();

/**
 * Gráfico de línea con área rellena, para series mensuales de un solo valor.
 * datos: [{mes: "2026-07", valor: 12345}, ...]
 */
function buildLineChart(datos, opts = {}) {
  const w = opts.w || 700, h = opts.h || 220;
  const color = opts.color || "var(--accent)";
  const fmtValor = opts.tooltipValueFmt || chartFmtMoney;
  const padL = 46, padR = 16, padT = 16, padB = 26;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  if (!datos.length) return `<p class="ayuda-inline">Sin datos suficientes todavía.</p>`;
  const vals = datos.map(d => d.valor);
  const vmax = Math.max(...vals, 1) * 1.15;
  const n = datos.length;
  const pts = datos.map((d, i) => {
    const x = padL + (n > 1 ? plotW * i / (n - 1) : plotW / 2);
    const y = padT + plotH - (plotH * d.valor / vmax);
    return [x, y];
  });
  const path = "M " + pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" L ");
  const area = path + ` L ${pts[pts.length - 1][0].toFixed(1)},${padT + plotH} L ${pts[0][0].toFixed(1)},${padT + plotH} Z`;
  let grid = "";
  for (let gi = 0; gi < 4; gi++) {
    const gy = padT + plotH * gi / 3;
    const gval = vmax * (1 - gi / 3);
    grid += `<line x1="${padL}" y1="${gy.toFixed(1)}" x2="${w - padR}" y2="${gy.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>`;
    grid += `<text x="${padL - 8}" y="${(gy + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--text-muted)">${chartFmtCompacto(gval)}</text>`;
  }
  let labels = "";
  datos.forEach((d, i) => {
    if (n > 8 && i % 2 !== 0 && i !== n - 1) return;
    const x = padL + (n > 1 ? plotW * i / (n - 1) : plotW / 2);
    const mm = d.mes.split("-")[1];
    labels += `<text x="${x.toFixed(1)}" y="${h - 8}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${MESES_LABEL_CORTO[mm] || d.mes}</text>`;
  });
  let hits = "";
  datos.forEach((d, i) => {
    const x0 = i === 0 ? padL : (pts[i - 1][0] + pts[i][0]) / 2;
    const x1 = i === n - 1 ? w - padR : (pts[i][0] + pts[i + 1][0]) / 2;
    const mm = d.mes.split("-")[1];
    const filas = d.tt && d.tt.length ? d.tt.map(f => ({ c: f.c || color, l: f.l, v: f.v })) : [{ c: color, l: "Valor", v: fmtValor(d.valor) }];
    const tt = chartAttrJson({ t: `${MESES_LABEL_CORTO[mm] || d.mes} ${d.mes.split("-")[0]}`, r: filas });
    hits += `<rect class="chart-hit" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${plotH}" data-tt='${tt}'/>`;
    hits += `<circle class="chart-punto-hover" cx="${pts[i][0].toFixed(1)}" cy="${pts[i][1].toFixed(1)}" r="2.5" fill="${color}" opacity="0"/>`;
  });
  const gradId = "grad" + Math.random().toString(36).slice(2, 9);
  const lastPt = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Gráfico de línea">
    <defs><linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#${gradId})"/>
    <path d="${path}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${lastPt[0].toFixed(1)}" cy="${lastPt[1].toFixed(1)}" r="3.5" fill="${color}"/>
    ${labels}
    ${hits}
  </svg>`;
}

/**
 * Gráfico de línea con DOS series (ej. ingresos vs gastos), con leyenda.
 * datos: [{mes, a: n, b: n}, ...]
 */
function buildLineChartDoble(datos, labelA, labelB, opts = {}) {
  const w = opts.w || 700, h = opts.h || 220;
  const colorA = opts.colorA || "var(--accent)";
  const colorB = opts.colorB || "var(--danger)";
  const fmtValor = opts.tooltipValueFmt || chartFmtMoney;
  const padL = 46, padR = 16, padT = 16, padB = 26;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  if (!datos.length) return `<p class="ayuda-inline">Sin datos suficientes todavía.</p>`;
  const vmax = Math.max(...datos.map(d => Math.max(d.a, d.b)), 1) * 1.15;
  const n = datos.length;
  const linea = (campo) => datos.map((d, i) => {
    const x = padL + (n > 1 ? plotW * i / (n - 1) : plotW / 2);
    const y = padT + plotH - (plotH * d[campo] / vmax);
    return [x, y];
  });
  const ptsA = linea("a"), ptsB = linea("b");
  const pathOf = (pts) => "M " + pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" L ");
  let grid = "";
  for (let gi = 0; gi < 4; gi++) {
    const gy = padT + plotH * gi / 3;
    const gval = vmax * (1 - gi / 3);
    grid += `<line x1="${padL}" y1="${gy.toFixed(1)}" x2="${w - padR}" y2="${gy.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>`;
    grid += `<text x="${padL - 8}" y="${(gy + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--text-muted)">${chartFmtCompacto(gval)}</text>`;
  }
  let labels = "";
  datos.forEach((d, i) => {
    if (n > 8 && i % 2 !== 0 && i !== n - 1) return;
    const x = padL + (n > 1 ? plotW * i / (n - 1) : plotW / 2);
    const mm = d.mes.split("-")[1];
    labels += `<text x="${x.toFixed(1)}" y="${h - 8}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${MESES_LABEL_CORTO[mm] || d.mes}</text>`;
  });
  let hits = "";
  datos.forEach((d, i) => {
    const x0 = i === 0 ? padL : (ptsA[i - 1][0] + ptsA[i][0]) / 2;
    const x1 = i === n - 1 ? w - padR : (ptsA[i][0] + ptsA[i + 1][0]) / 2;
    const mm = d.mes.split("-")[1];
    const tt = chartAttrJson({
      t: `${MESES_LABEL_CORTO[mm] || d.mes} ${d.mes.split("-")[0]}`,
      r: [{ c: colorA, l: labelA, v: fmtValor(d.a) }, { c: colorB, l: labelB, v: fmtValor(d.b) }],
    });
    hits += `<rect class="chart-hit" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${plotH}" data-tt='${tt}'/>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Gráfico de dos series">
    ${grid}
    <path d="${pathOf(ptsA)}" fill="none" stroke="${colorA}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="${pathOf(ptsB)}" fill="none" stroke="${colorB}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="5 4"/>
    ${labels}
    ${hits}
  </svg>
  <div class="chart-leyenda">
    <span><i style="background:${colorA}"></i>${labelA}</span>
    <span><i style="background:${colorB}"></i>${labelB}</span>
  </div>`;
}

/**
 * Gráfico de línea con tendencia real + proyección a futuro (regresión
 * simple calculada en el backend, ver panel_dueno() en app.py). El tramo
 * real se dibuja igual que buildLineChart (línea sólida + área); el tramo
 * proyectado continúa la misma línea pero punteada y en un tono más claro,
 * para que nunca se confunda un dato real con una estimación -- siempre
 * con su propia entrada en la leyenda, nunca solo por el color.
 * reales: [{mes, valor}], proyectados: [{mes, valor}] (continúa desde el
 * último punto real).
 */
function buildLineChartProyeccion(reales, proyectados, opts = {}) {
  const w = opts.w || 700, h = opts.h || 220;
  const color = opts.color || "var(--accent)";
  const colorProy = opts.colorProyeccion || "var(--text-muted)";
  const fmtValor = opts.tooltipValueFmt || chartFmtMoney;
  const padL = 46, padR = 16, padT = 16, padB = 26;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  if (!reales.length) return `<p class="ayuda-inline">Sin datos suficientes todavía.</p>`;
  const todos = reales.concat(proyectados);
  const n = todos.length;
  const vmax = Math.max(...todos.map(d => d.valor), 1) * 1.15;
  const pts = todos.map((d, i) => {
    const x = padL + (n > 1 ? plotW * i / (n - 1) : plotW / 2);
    const y = padT + plotH - (plotH * d.valor / vmax);
    return [x, y];
  });
  const nReales = reales.length;
  const ptsReales = pts.slice(0, nReales);
  const ptsProy = pts.slice(nReales - 1); // incluye el último punto real como enlace
  const pathReal = "M " + ptsReales.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" L ");
  const area = pathReal + ` L ${ptsReales[ptsReales.length - 1][0].toFixed(1)},${padT + plotH} L ${ptsReales[0][0].toFixed(1)},${padT + plotH} Z`;
  const pathProy = ptsProy.length > 1
    ? "M " + ptsProy.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" L ")
    : "";
  let grid = "";
  for (let gi = 0; gi < 4; gi++) {
    const gy = padT + plotH * gi / 3;
    const gval = vmax * (1 - gi / 3);
    grid += `<line x1="${padL}" y1="${gy.toFixed(1)}" x2="${w - padR}" y2="${gy.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>`;
    grid += `<text x="${padL - 8}" y="${(gy + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--text-muted)">${chartFmtCompacto(gval)}</text>`;
  }
  let labels = "";
  todos.forEach((d, i) => {
    if (n > 8 && i % 2 !== 0 && i !== n - 1) return;
    const x = padL + (n > 1 ? plotW * i / (n - 1) : plotW / 2);
    const mm = d.mes.split("-")[1];
    labels += `<text x="${x.toFixed(1)}" y="${h - 8}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${MESES_LABEL_CORTO[mm] || d.mes}</text>`;
  });
  let hits = "";
  todos.forEach((d, i) => {
    const esProyeccion = i >= nReales;
    const x0 = i === 0 ? padL : (pts[i - 1][0] + pts[i][0]) / 2;
    const x1 = i === n - 1 ? w - padR : (pts[i][0] + pts[i + 1][0]) / 2;
    const mm = d.mes.split("-")[1];
    const tt = chartAttrJson({
      t: `${MESES_LABEL_CORTO[mm] || d.mes} ${d.mes.split("-")[0]}`,
      r: [{ c: esProyeccion ? colorProy : color, l: esProyeccion ? "Proyección" : "Real", v: fmtValor(d.valor) }],
    });
    hits += `<rect class="chart-hit" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${plotH}" data-tt='${tt}'/>`;
  });
  const gradId = "grad" + Math.random().toString(36).slice(2, 9);
  const lastReal = ptsReales[ptsReales.length - 1];
  const lastProy = ptsProy.length > 1 ? ptsProy[ptsProy.length - 1] : null;
  return `<div>
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Gráfico de línea con proyección">
      <defs><linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.28"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient></defs>
      ${grid}
      <path d="${area}" fill="url(#${gradId})"/>
      <path d="${pathReal}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      ${pathProy ? `<path d="${pathProy}" fill="none" stroke="${colorProy}" stroke-width="2.5" stroke-dasharray="5 5" stroke-linecap="round" stroke-linejoin="round"/>` : ""}
      <circle cx="${lastReal[0].toFixed(1)}" cy="${lastReal[1].toFixed(1)}" r="3.5" fill="${color}"/>
      ${lastProy ? `<circle cx="${lastProy[0].toFixed(1)}" cy="${lastProy[1].toFixed(1)}" r="3.5" fill="${colorProy}"/>` : ""}
      ${labels}
      ${hits}
    </svg>
    ${proyectados.length ? `<div class="chart-leyenda">
      <span><i style="background:${color}"></i>Real</span>
      <span><i style="background:${colorProy}"></i>Proyección (próximos meses)</span>
    </div>` : ""}
  </div>`;
}

/**
 * Barras horizontales para rankings (marcas, clientes, proveedores...).
 * datos: [{label: "Ford", valor: 310, extra: "opcional",
 *          tt: [{l:"OTs", v:"12"}, ...] (opcional -- filas de tooltip a
 *          medida; si no viene, el tooltip cae al default "Valor: <extra>"),
 *          tendencia: "sube"|"baja"|"estable" (opcional -- dibuja una
 *          flechita de color junto al valor de la derecha)}]
 * El tooltip a medida existe para no repetir en el hover el mismo dato que
 * ya se lee en la barra (ver dataviz: el hover debe aportar algo nuevo).
 */
function buildBarChartH(datos, opts = {}) {
  const w = opts.w || 340;
  const color = opts.color || "var(--accent)";
  const rowH = opts.rowH || 34;
  const padL = opts.padL || 110, padR = 50;
  const h = datos.length * rowH + 10;
  const plotW = w - padL - padR;
  if (!datos.length) return `<p class="ayuda-inline">Sin datos suficientes todavía.</p>`;
  const vmax = Math.max(...datos.map(d => d.valor), 1);
  const TENDENCIA_FLECHA = { sube: "▲", baja: "▼", estable: "→" };
  const TENDENCIA_COLOR = { sube: "var(--success)", baja: "var(--danger)", estable: "var(--text-muted)" };
  let bars = "";
  datos.forEach((d, i) => {
    const y = 5 + i * rowH;
    const barH = rowH * 0.55;
    const bw = plotW * d.valor / vmax;
    const filas = d.tt && d.tt.length ? d.tt.map(f => ({ c: f.c || null, l: f.l, v: f.v })) : [{ c: color, l: "Valor", v: String(d.extra || d.valor) }];
    const tt = chartAttrJson({ t: String(d.label), r: filas });
    const flecha = d.tendencia && TENDENCIA_FLECHA[d.tendencia]
      ? `<tspan fill="${TENDENCIA_COLOR[d.tendencia]}">${TENDENCIA_FLECHA[d.tendencia]}</tspan> `
      : "";
    bars += `<rect x="${padL}" y="${(y + (rowH - barH) / 2).toFixed(1)}" width="${bw.toFixed(1)}" height="${barH}" rx="3" fill="${color}" data-tt='${tt}'/>`;
    bars += `<text x="${padL - 8}" y="${(y + rowH / 2 + 4).toFixed(1)}" text-anchor="end" font-size="11.5" fill="var(--text-muted)">${escapeHtmlChart(d.label)}</text>`;
    bars += `<text x="${(padL + bw + 6).toFixed(1)}" y="${(y + rowH / 2 + 4).toFixed(1)}" font-size="11.5" fill="var(--text)">${flecha}${escapeHtmlChart(d.extra || String(d.valor))}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Gráfico de barras">${bars}</svg>`;
}

/**
 * Barras verticales simples de una serie corta (ej. distribución por
 * estado). datos: [{label, valor}]
 */
function buildBarChartV(datos, opts = {}) {
  const w = opts.w || 700, h = opts.h || 200;
  const color = opts.color || "var(--accent)";
  const padL = 34, padR = 10, padT = 14, padB = 34;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  if (!datos.length) return `<p class="ayuda-inline">Sin datos suficientes todavía.</p>`;
  const n = datos.length;
  const gap = plotW / n;
  const barW = gap * 0.5;
  const vmax = Math.max(...datos.map(d => d.valor), 1);
  let bars = "";
  datos.forEach((d, i) => {
    const bh = plotH * d.valor / vmax;
    const x = padL + gap * i + (gap - barW) / 2;
    const y = padT + plotH - bh;
    const tt = chartAttrJson({ t: String(d.label), r: [{ c: color, l: "Valor", v: String(d.valor) }] });
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="${color}" data-tt='${tt}'/>`;
    bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle" font-size="11" fill="var(--text)">${d.valor}</text>`;
    bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${h - 12}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${escapeHtmlChart(d.label)}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Distribución">${bars}</svg>`;
}
