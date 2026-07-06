// Página pública de seguimiento (sin login): el cliente ingresa su patente y
// ve en qué va su vehículo. Consume /api/seguimiento?patente=XXXX (ver
// modulos/operativo.py). Se mantiene deliberadamente simple y autocontenido:
// no comparte código con operativo.js porque esa API es interna y requiere
// sesión iniciada.

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtFechaSolo(isoFecha) {
  if (!isoFecha) return "—";
  const fecha = isoFecha.split("T")[0];
  const [y, m, d] = fecha.split("-");
  return `${d}-${m}-${y}`;
}

function mostrarError(msg) {
  const el = document.getElementById("seg-error");
  el.textContent = msg;
  el.style.display = msg ? "" : "none";
}

function renderResultado(data) {
  document.getElementById("seg-resultado").classList.add("visible");

  document.getElementById("seg-vehiculo-nombre").textContent =
    [data.marca, data.modelo].filter(Boolean).join(" ") || "Vehículo";
  document.getElementById("seg-vehiculo-detalle").textContent =
    `Patente ${data.patente} — OT #${data.orden_numero}`;

  const cancelada = data.estado === "cancelado";
  document.getElementById("seg-cancelada").style.display = cancelada ? "" : "none";
  document.getElementById("seg-stepper").style.display = cancelada ? "none" : "";

  if (!cancelada) {
    const idxActual = data.pasos.indexOf(data.estado);
    const stepperHtml = data.pasos.map((paso, i) => {
      let clase = "seg-paso";
      if (i < idxActual) clase += " completado";
      else if (i === idxActual) clase += " actual";
      const esUltimo = i === data.pasos.length - 1;
      return `
        <div class="${clase}">
          <div class="seg-paso-linea">
            <div class="seg-paso-punto"></div>
            ${esUltimo ? "" : '<div class="seg-paso-conector"></div>'}
          </div>
          <div class="seg-paso-texto">${escapeHtml(data.pasos_label[paso] || paso)}</div>
        </div>`;
    }).join("");
    document.getElementById("seg-stepper").innerHTML = stepperHtml;
  }

  const filasFecha = [["Recepción", fmtFechaSolo(data.fecha_recepcion)]];
  if (data.estado === "entregado" && data.fecha_entrega_real) {
    filasFecha.push(["Entregado", fmtFechaSolo(data.fecha_entrega_real)]);
  } else if (data.fecha_entrega_estimada) {
    filasFecha.push(["Entrega estimada", fmtFechaSolo(data.fecha_entrega_estimada)]);
  }
  document.getElementById("seg-fechas").innerHTML = filasFecha
    .map(([k, v]) => `<div class="op-resumen-fila"><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`)
    .join("");
}

async function consultarPatente(patente) {
  mostrarError("");
  document.getElementById("seg-resultado").classList.remove("visible");
  if (!patente) return;
  try {
    const resp = await fetch(`/api/seguimiento?patente=${encodeURIComponent(patente)}`);
    const data = await resp.json();
    if (!resp.ok) {
      mostrarError(data.error || "No se pudo consultar. Intenta de nuevo.");
      return;
    }
    if (!data.encontrado) {
      mostrarError("No encontramos ninguna orden asociada a esa patente. Verifica que esté bien escrita o contáctanos directamente.");
      return;
    }
    renderResultado(data);
  } catch (e) {
    mostrarError("No se pudo conectar. Verifica tu conexión e intenta de nuevo.");
  }
}

document.getElementById("form-seguimiento").addEventListener("submit", (e) => {
  e.preventDefault();
  const patente = document.getElementById("seg-patente").value.trim();
  consultarPatente(patente);
});

// Si llegan por un link ya armado (?patente=XXXX), precarga y consulta sola —
// así el cliente no tiene que volver a tipear la patente.
(function autocompletarDesdeUrl() {
  const params = new URLSearchParams(window.location.search);
  const patente = params.get("patente");
  if (patente) {
    document.getElementById("seg-patente").value = patente.toUpperCase();
    consultarPatente(patente);
  }
})();
