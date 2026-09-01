// El número de OT que ve el taller.
//
// Durante el mes de convivencia Talca es quien reparte los números de orden.
// El `id` de Python es una llave técnica interna y NUNCA debe mostrarse: es
// de otra serie y choca con el talca_id de una orden distinta. Con 7.116
// órdenes importadas hay 6.117 números que existen en las dos series
// apuntando a órdenes diferentes, así que mostrar el id equivocado no es un
// detalle cosmético — es decirle al cliente un número que en Talca es el auto
// de otra persona.
//
// Este archivo se carga ANTES que los scripts de página en las siete
// plantillas autenticadas. Es a propósito: si viviera en uno de ellos, los
// otros lo tendrían disponible solo por casualidad del orden de carga, y ya
// hubo un bug de esa forma (ver "paginaActual" en CLAUDE.md).

// Las órdenes creadas acá todavía no tienen número de Talca: hasta que el
// push confirme, se muestran con prefijo L- (de "local") para que nadie las
// confunda con un número de Talca ni las tipee en el sistema viejo.
function numeroOT(orden) {
  if (!orden) return "—";
  const talca = orden.talca_id;
  if (talca !== null && talca !== undefined && talca !== "") return String(talca);
  const id = orden.id != null ? orden.id : orden.orden_id;
  return id != null ? `L-${id}` : "—";
}

// Con el "#" delante, para los títulos y las tarjetas.
function etiquetaOT(orden) {
  return `#${numeroOT(orden)}`;
}
