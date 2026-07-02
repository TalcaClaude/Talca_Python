# Inventario Taller — Módulos 1 y 2 + primera pieza del Módulo 4

Sistema de logística y abastecimiento para el taller, basado en el documento
`Software inventario.docx`.

## Módulo 2: Administración de Inventario
- **N° de sistema** correlativo y único por producto (ej: `#0007`).
- **Código interno** opcional (no todas las facturas lo traen).
- **No existe un campo "nombre"**: el producto se identifica por
  **categoría + modelo**, que se combinan automáticamente para mostrarlo
  en las listas (ej: "Discos de Hierro - 722.9"). Si faltan ambos, usa
  marca, código interno, o el N° de sistema como respaldo.
- **Categoría**, **Marca** y **Modelo** se eligen de listas precargadas
  (botón "+ Nueva" para agregar sobre la marcha):
  - Categorías: Cuerpo de Válvulas, Discos de Hierro, Discos de Fricción, Kit de Sellos.
  - Modelos: JF011, JF015, 722.9, 722.6, MPS6, 10R80.
  - Marca: sin precarga por ahora (se agrega libremente).
- **Costo Promedio Ponderado (CPP)**: el costo del producto se recalcula
  solo cada vez que registras una entrada de stock con su costo real.

## Módulo 3: Organización Física de Bodega
- Pestaña "Bodega": pasillos → estanterías → niveles, código automático,
  un producto por ubicación.

## Primera pieza del Módulo 4: Facturas de compra
- Pestaña "Facturas": registra una factura (número, proveedor, fecha) con
  el listado de productos que trae y su precio real.
- Al registrar una **entrada de stock**, escribes el número de factura.
  El sistema detecta si ese producto está en la factura y, si lo está,
  **completa y bloquea automáticamente la cantidad y el precio exactos**
  de la factura (no se pueden alterar a mano, para evitar errores de
  tipeo). El número de factura queda visible en el historial del producto.
- Cada línea de una factura solo se puede usar una vez, para que no se
  ingrese el mismo stock dos veces por accidente.
- Una factura no se puede eliminar si alguna de sus líneas ya fue usada
  para ingresar stock.

## Eliminar productos y facturas con historial (corrección de errores)
- Por defecto, el sistema **bloquea** borrar un producto con movimientos
  registrados, o una factura con productos ya ingresados a stock — para
  evitar pérdidas de información por accidente.
- Si de verdad necesitas borrarlo (error humano), el sistema te avisa qué
  se va a perder y te pregunta **"¿Eliminar de todas formas?"**. Si aceptas:
  - Al forzar un **producto**: se elimina junto con todo su historial de
    movimientos y su presencia en facturas.
  - Al forzar una **factura**: el sistema **revierte automáticamente el
    stock** que esa factura había ingresado (siempre que ese stock siga
    disponible y no se haya usado después). Si ya se consumió parte de
    ese stock, no te deja eliminarla hasta que ajustes el stock a mano
    primero — para no dejar el inventario descuadrado.

## Dashboard (Módulo 1)
- Nueva pestaña "Dashboard" (ahora es la pantalla de inicio): valor total
  del inventario, productos con stock crítico, últimos movimientos,
  compras recientes, productos sin movimiento, productos con mayor
  rotación (más salidas), y un resumen general de la bodega.

## Respaldo de datos
- Botón "Respaldo" arriba a la derecha:
  - **Diagnóstico**: muestra si la base de datos está usando el disco
    persistente de Railway (importante para confirmar que los datos no
    se van a perder en la próxima actualización).
  - **Descargar respaldo**: baja un archivo `.db` con todo lo cargado.
    Hazlo antes de cada actualización, como hábito.
  - **Restaurar**: sube un archivo de respaldo y reemplaza todos los
    datos actuales por los de ese archivo (por si algo se pierde).

## Ajustes de esta ronda
- **Se eliminó "Marca"** del sistema. La categoría + el modelo son
  suficientes para identificar cada producto. Si ya tenías productos con
  marca cargada, se limpió sola al abrir esta versión.
- **Una ubicación ahora puede tener varios productos** (antes era 1 a 1,
  lo cual no reflejaba la realidad de una bodega).
- **No se pueden crear dos productos con la misma categoría + modelo.**
  Si necesitas más stock de algo que ya existe, se ingresa con una
  factura (Registrar movimiento), no creando el producto de nuevo.
- **El N° de sistema ahora reutiliza números libres**: si creas un
  producto y lo borras, el siguiente que crees puede reutilizar ese
  número (antes seguía saltándose los números borrados para siempre).

## Proveedores (Módulo 4)
- Pestaña "Proveedores": nombre, RUT, teléfono, email y dirección.
- Al crear una factura, eliges el proveedor de una lista (con "+ Nuevo"
  para agregar uno rápido sin salir del formulario).
- Si intentas borrar un proveedor que ya tiene facturas, el sistema te
  avisa: puedes forzar la eliminación, y las facturas se conservan (solo
  quedan sin proveedor asignado, no se pierden).

## Moneda
- Todo el sistema quedó en **pesos chilenos (CLP)**: sin decimales/centavos,
  con el formato de miles con punto (ej: `$12.345`).

## Subir esto a internet
Mira el archivo **`DEPLOY.md`** en esta misma carpeta: es una guía paso a
paso para publicarlo con Railway (gratis para empezar), con disco
persistente para que la base de datos no se borre entre actualizaciones.

## Otros ajustes
- Los formularios ya no se cierran al hacer click afuera. Solo con la X,
  "Cancelar" o la tecla ESC.
- Se quitó el campo de foto.

## Cómo correrlo en tu computador

1. Necesitas Python 3 instalado.
2. En la terminal, dentro de esta carpeta:

   ```
   pip install -r requirements.txt
   python app.py
   ```

3. Abre el navegador en: http://localhost:5000

Si ya tenías un `inventario.db` de una versión anterior, cópialo a esta
carpeta antes de correr `app.py`. La migración es automática y no se
pierde ningún producto ni movimiento — incluye una corrección automática
para los productos que hayan quedado con "JF011", "722.9", etc. guardados
como marca en vez de modelo (el error de la versión anterior).

## Estructura del proyecto

```
taller-inventario/
├── app.py              -> Backend (servidor + toda la lógica y la API)
├── inventario.db        -> Base de datos (se crea sola)
├── requirements.txt      -> Lo que hay que instalar
├── templates/index.html  -> Estructura de la página
└── static/
    ├── css/style.css     -> Estilos visuales
    └── js/app.js          -> Lógica de la interfaz (conecta con el backend)
```

## Próximos pasos (los vamos armando juntos)

1. Subir esto a internet para que lo usen 2-3 personas del taller desde
   cualquier lugar.
2. Completar el Módulo 4 (proveedores como entidad propia, carga masiva
   desde Excel, adjuntar el PDF/foto de la factura).
3. Dashboard visual.
4. El resto de los módulos del documento original.



