// Carga inicial desde Talca PHP (ver modulos/carga_inicial.py).
// "Ensayar" corre todo y lo deshace; "Aplicar" solo aparece si el ensayo cuadró.

function escapar(texto) {
  const div = document.createElement("div");
  div.textContent = texto == null ? "" : String(texto);
  return div.innerHTML;
}

function numero(valor) {
  return Number(valor).toLocaleString("es-CL");
}

async function enviarPlan(accion) {
  const input = document.getElementById("ci-archivo");
  if (!input.files.length) {
    alert("Elige primero el archivo plan_carga_*.json");
    return null;
  }
  const formData = new FormData();
  formData.append("archivo", input.files[0]);
  formData.append("accion", accion);
  if (accion === "aplicar") formData.append("confirmar_texto", document.getElementById("ci-confirmar").value);
  const res = await fetch("/api/admin/carga-inicial", { method: "POST", body: formData });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Error inesperado");
  return data;
}

function mostrarResultado(data) {
  document.getElementById("ci-resultado").style.display = "";
  const origen = data.origen_plan || {};
  document.getElementById("ci-titulo").textContent = data.aplicado
    ? `APLICADO. Respaldo previo: ${data.respaldo}`
    : `Ensayo: todo cuadró y no se escribió nada. Plan extraído el ${origen.extraido_en || "?"}.`;
  document.getElementById("ci-chequeos").innerHTML = data.chequeos.map(c => `
    <tr>
      <td>${escapar(c.nombre)}</td>
      <td class="num">${numero(c.real)}</td>
      <td class="num">${numero(c.esperado)}</td>
      <td>${c.ok ? "OK" : "DISTINTO"}</td>
    </tr>`).join("");
  document.getElementById("ci-detalle").textContent =
    `Proveedores creados: ${data.proveedores_creados} (reusados: ${data.proveedores_reusados}). ` +
    `Productos nuevos por caja chica: ${data.productos_nuevos_por_caja}.`;
  document.getElementById("ci-aplicar").style.display = data.aplicado ? "none" : "";
}

document.getElementById("btn-ci-ensayar").addEventListener("click", async () => {
  const btn = document.getElementById("btn-ci-ensayar");
  btn.disabled = true;
  btn.textContent = "Ensayando… (puede tardar un minuto)";
  try {
    const data = await enviarPlan("ensayar");
    if (data) mostrarResultado(data);
  } catch (e) {
    document.getElementById("ci-aplicar").style.display = "none";
    alert(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "1. Ensayar (no escribe nada)";
  }
});

document.getElementById("btn-ci-aplicar").addEventListener("click", async () => {
  if (!confirm("Esto escribe el inventario y la caja chica histórica en la base. ¿Aplicar?")) return;
  const btn = document.getElementById("btn-ci-aplicar");
  btn.disabled = true;
  btn.textContent = "Aplicando…";
  try {
    const data = await enviarPlan("aplicar");
    if (data) mostrarResultado(data);
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "2. Aplicar la carga";
  }
});
