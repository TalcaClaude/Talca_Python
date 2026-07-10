// ---------------------------------------------------------------------------
// Helpers de gráficos livianos (SVG puro, sin librerías externas) para los
// dashboards de Panel del Dueño, Operativo y Finanzas. Cada función devuelve
// un string de HTML/SVG listo para inyectar con innerHTML — mismo patrón
// simple que ya usa el resto del código (no hay un sistema de componentes).
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

/**
 * Gráfico de línea con área rellena, para series mensuales de un solo valor.
 * datos: [{mes: "2026-07", valor: 12345}, ...]
 */
function buildLineChart(datos, opts = {}) {
  const w = opts.w || 700, h = opts.h || 220;
  const color = opts.color || "var(--accent)";
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
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Gráfico de dos series">
    ${grid}
    <path d="${pathOf(ptsA)}" fill="none" stroke="${colorA}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="${pathOf(ptsB)}" fill="none" stroke="${colorB}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="5 4"/>
    ${labels}
  </svg>
  <div class="chart-leyenda">
    <span><i style="background:${colorA}"></i>${labelA}</span>
    <span><i style="background:${colorB}"></i>${labelB}</span>
  </div>`;
}

/**
 * Barras horizontales para rankings (marcas, clientes, proveedores...).
 * datos: [{label: "Ford", valor: 310, extra: "opcional"}]
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
  let bars = "";
  datos.forEach((d, i) => {
    const y = 5 + i * rowH;
    const barH = rowH * 0.55;
    const bw = plotW * d.valor / vmax;
    bars += `<rect x="${padL}" y="${(y + (rowH - barH) / 2).toFixed(1)}" width="${bw.toFixed(1)}" height="${barH}" rx="3" fill="${color}"/>`;
    bars += `<text x="${padL - 8}" y="${(y + rowH / 2 + 4).toFixed(1)}" text-anchor="end" font-size="11.5" fill="var(--text-muted)">${escapeHtmlChart(d.label)}</text>`;
    bars += `<text x="${(padL + bw + 6).toFixed(1)}" y="${(y + rowH / 2 + 4).toFixed(1)}" font-size="11.5" fill="var(--text)">${escapeHtmlChart(d.extra || String(d.valor))}</text>`;
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
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" rx="3" fill="${color}"/>`;
    bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle" font-size="11" fill="var(--text)">${d.valor}</text>`;
    bars += `<text x="${(x + barW / 2).toFixed(1)}" y="${h - 12}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${escapeHtmlChart(d.label)}</text>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" aria-label="Distribución">${bars}</svg>`;
}

function escapeHtmlChart(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function chartFmtMoney(n) {
  return "$" + Math.round(n || 0).toLocaleString("es-CL");
}
