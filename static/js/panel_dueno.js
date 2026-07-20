// ---------------------------------------------------------------------------
// Panel del dueño (pantalla de inicio, "/") — KPIs y gráfico cruzados entre
// Operativo y Finanzas. Deliberadamente NO depende de operativo.js/finanzas.js
// (cada módulo es autosuficiente, ver alerta_facturas_pendientes.js) así que
// trae sus propias copias mínimas de fmtMoney/api.
// ---------------------------------------------------------------------------

function pdFmtMoney(n) {
  if (n === null || n === undefined) return "—";
  return "$" + Math.round(n).toLocaleString("es-CL");
}

async function pdApi(path) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Error inesperado");
  return data;
}

const PD_RANGO_LABEL = { "12": "12 meses", "24": "24 meses", "36": "36 meses", "historico": "todo el historial" };

async function cargarPanelDueno() {
  try {
    const rango = document.getElementById("pd-filtro-rango").value || "12";
    const d = await pdApi(`/api/panel-dueno?rango=${encodeURIComponent(rango)}`);

    document.getElementById("pd-ingresos-mes").textContent = pdFmtMoney(d.ingresos_mes);
    const elVar = document.getElementById("pd-var-mes");
    elVar.classList.remove("kpi-delta--up", "kpi-delta--down");
    if (d.var_pct_mes === null) {
      elVar.textContent = "sin datos del mes anterior";
    } else {
      const signo = d.var_pct_mes >= 0 ? "▲" : "▼";
      // Mientras el mes esté en curso, la comparación es contra el mismo
      // tramo de días del mes anterior (no el mes anterior completo) para
      // no mostrar una baja artificial solo porque llevamos pocos días.
      const referencia = d.mes_en_curso ? "vs mismos días del mes anterior" : "vs mes anterior";
      elVar.textContent = `${signo} ${Math.abs(d.var_pct_mes)}% ${referencia}`;
      elVar.classList.add(d.var_pct_mes >= 0 ? "kpi-delta--up" : "kpi-delta--down");
    }

    document.getElementById("pd-proyeccion").textContent = pdFmtMoney(d.proyeccion_fin_mes);
    document.getElementById("pd-venta-total").textContent = pdFmtMoney(d.venta_total_mes);
    document.getElementById("pd-utilidad-estimada").textContent = pdFmtMoney(d.utilidad_estimada_mes);
    document.getElementById("pd-ot-abiertas").textContent = d.ot_abiertas;
    document.getElementById("pd-capacidad").textContent =
      `capacidad de referencia: ~${d.capacidad_semanal_promedio} OT/semana`;
    document.getElementById("pd-cxc").textContent = pdFmtMoney(d.cxc_pendiente);
    document.getElementById("pd-cxp").textContent = pdFmtMoney(d.cxp_pendiente);

    document.getElementById("pd-chart-tendencia-sub").textContent =
      `Pagos recibidos de clientes — ${PD_RANGO_LABEL[d.rango_aplicado] || "12 meses"}`;
    document.getElementById("pd-chart-tendencia").innerHTML = buildLineChartProyeccion(
      d.tendencia_mensual.map(m => ({ mes: m.mes, valor: m.ingresos })),
      (d.proyeccion_mensual || []).map(m => ({ mes: m.mes, valor: m.ingresos_proyectados }))
    );

    document.getElementById("pd-chart-clientes").innerHTML = buildBarChartH(
      (d.ranking_clientes_historico || []).map(c => ({ label: c.nombre, valor: c.monto, extra: pdFmtMoney(c.monto) })),
      { padL: 140 }
    );
  } catch (e) {
    console.error("Error cargando el panel del dueño:", e);
  }
}

document.getElementById("pd-filtro-rango").addEventListener("change", cargarPanelDueno);
cargarPanelDueno();
