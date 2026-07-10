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

async function cargarPanelDueno() {
  try {
    const d = await pdApi("/api/panel-dueno");

    document.getElementById("pd-ingresos-mes").textContent = pdFmtMoney(d.ingresos_mes);
    const elVar = document.getElementById("pd-var-mes");
    if (d.var_pct_mes === null) {
      elVar.textContent = "sin datos del mes anterior";
    } else {
      const signo = d.var_pct_mes >= 0 ? "▲" : "▼";
      elVar.textContent = `${signo} ${Math.abs(d.var_pct_mes)}% vs mes anterior`;
      elVar.classList.add(d.var_pct_mes >= 0 ? "kpi-delta--up" : "kpi-delta--down");
    }

    document.getElementById("pd-proyeccion").textContent = pdFmtMoney(d.proyeccion_fin_mes);
    document.getElementById("pd-margen").textContent = pdFmtMoney(d.margen_bruto_mes);
    document.getElementById("pd-ot-abiertas").textContent = d.ot_abiertas;
    document.getElementById("pd-capacidad").textContent =
      `capacidad de referencia: ~${d.capacidad_semanal_promedio} OT/semana`;
    document.getElementById("pd-cxc").textContent = pdFmtMoney(d.cxc_pendiente);
    document.getElementById("pd-cxp").textContent = pdFmtMoney(d.cxp_pendiente);

    document.getElementById("pd-chart-tendencia").innerHTML = buildLineChart(
      d.tendencia_mensual.map(m => ({ mes: m.mes, valor: m.ingresos }))
    );
  } catch (e) {
    console.error("Error cargando el panel del dueño:", e);
  }
}

cargarPanelDueno();
