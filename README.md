# Inventario Taller — Talleres Calificados

Sistema de gestión para el taller (Flask + SQLite), desplegado en Railway.
Nació como un inventario de repuestos basado en el documento `Software
inventario.docx` (ver secciones "Módulo N" más abajo, que documentan esa
primera etapa con detalle) y desde entonces creció hasta cubrir la operación
completa del taller.

## Estado actual (qué hay hoy, en una pasada)

- **Logística**: inventario de repuestos y aceites (categoría/marca/modelo,
  costo promedio ponderado, ubicación física en bodega, proveedores,
  facturas de compra con ingreso automático de stock).
- **Operativo**: órdenes de trabajo (OT) de principio a fin — ingreso,
  diagnóstico, presupuesto (con envío por correo al cliente), reparación,
  control de calidad, entrega —, fotos de recepción, garantías, clientes,
  seguimiento público de una OT por patente (sin login), dashboard con
  métricas del taller, y reportes exportables (Excel/PDF).
- **Finanzas**: Cuentas por Cobrar/Pagar, Caja Chica (gastos, boletas
  parciales, rendiciones), Estado de Resultados, Facturación Electrónica
  (DTE) simulada — ver nota abajo sobre SimpleAPI —, y respaldo de datos
  (copias locales automáticas + respaldo diario fuera de Railway).
- **Convertidores de Torque**: módulo aparte para ese tipo de trabajo
  específico del taller (inventario propio, ligado a OTs/garantías).
- **Agente IA**: lee facturas por foto/WhatsApp y ayuda con diagnóstico.
- **Administración**: usuarios y roles (multi-módulo por usuario),
  respaldo/restauración de la base de datos, estado del sistema.

**Nota sobre Facturación Electrónica real (SII)**: hoy el módulo de
Facturación Electrónica emite documentos en modo simulado — la integración
real con el proveedor (SimpleAPI) quedó pendiente a propósito (ver
`modulos/facturacion.py`, función que arma `BASE_URL`) porque el taller sigue
facturando por otro medio. Si en algún momento se necesita emitir DTE
válidos ante el SII desde este sistema, ese es el trabajo que falta —
conseguir una cuenta de SimpleAPI y su documentación real (Postman) y
programar contra eso.

Las secciones "Módulo N" que siguen documentan con detalle esa primera
etapa (inventario/bodega/facturas de compra) — siguen siendo válidas para
esas funciones puntuales, aunque el sistema hoy es bastante más grande que
eso. Para el detalle de despliegue, respaldos y variables de entorno, ver
`DEPLOY.md`.

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

## Reserva de Productos (Módulo 6)
- Para reservar stock (por ejemplo, para una orden de trabajo futura):
  desde "Registrar movimiento" en la ficha del producto, elige **Salida**
  → categoría **"Reserva"**, y anota la orden de trabajo en el motivo.
  Queda igual que cualquier otro movimiento, sin un formulario aparte.

## Robustez y seguridad del sistema
- **Protección contra fuerza bruta** en el login: 5 intentos fallidos
  seguidos bloquean esa IP por 5 minutos.
- **Cookies de sesión seguras** cuando corre en producción (con
  `SECRET_KEY` configurada).
- **Respaldo automático diario por correo**: si configuras las variables
  `SMTP_USER` / `SMTP_PASSWORD` (ver `DEPLOY.md`), el sistema se manda
  solo una copia de la base de datos por correo cada día — sin costo,
  usando tu Gmail. Se puede revisar el estado y mandar una prueba desde
  el botón "Respaldo".

## Ajustes según revisión del taller
- **Tipos de movimiento simplificados**: ahora son solo **Entrada**,
  **Salida** y **Pérdida** (se quitó "Ajuste" como tipo — se logra igual
  con Entrada o Salida + categoría "Ajuste de inventario").
- **Categorías restringidas según el tipo**: en una Entrada ya no
  aparecen categorías que son solo de Salida (Venta, Consumo interno,
  Reserva), y en una Salida ya no aparece "Compra". Se quitó también
  "Traslado interno" (no se usa).
- **Mermas con cuenta propia**: al registrar una "Pérdida", el stock no
  desaparece — pasa a una cuenta separada ("en merma"), visible en el
  Dashboard y en la nueva pestaña **"Mermas"**, donde puedes revisar cada
  ítem y **descartarlo definitivamente** cuando corresponda (por ejemplo,
  cuando ya se botó a la basura).
- **Reservas simplificadas**: se quitó el apartado separado de
  "Reservar/Liberar/Consumir". Ahora una reserva es simplemente una
  Salida con categoría "Reserva" — se registra igual que cualquier otro
  movimiento, sin un formulario aparte.

## Control de Movimientos (Módulo 5)
- Cada movimiento ahora tiene una **categoría**: Compra, Venta, Consumo
  interno, Reserva, Ajuste de inventario, Devolución, Traslado interno,
  Merma o pérdida — se sugiere una por defecto según el tipo (entrada →
  Compra, salida → Consumo interno, ajuste → Ajuste de inventario), pero
  se puede cambiar.
- Nueva pestaña **"Movimientos"**: bitácora completa de todo lo que pasó
  en el inventario (no solo por producto), con filtros por tipo,
  categoría, rango de fechas, y buscador por texto (motivo, producto,
  factura, responsable).

## Login (usuarios y contraseñas)
- Ahora hay que iniciar sesión para usar el sistema.
- **Usuario inicial creado automáticamente:**
  - Usuario: `admin`
  - Contraseña: `taller2026`
  - **Cámbiala apenas puedas** desde la pestaña "Usuarios" → click en tu
    usuario → escribe una nueva contraseña → Guardar.
- Desde la pestaña "Usuarios" puedes crear una cuenta para cada persona
  del taller (no se puede eliminar el último usuario que queda, para no
  quedar bloqueados).
- **Importante para Railway**: agrega una variable de entorno `SECRET_KEY`
  con un texto largo y aleatorio (esto firma las sesiones de login; sin
  esto, cualquiera que vea el código podría falsificar una sesión).

## Facturas: ingreso automático de stock
- Al crear una factura, el stock de cada producto se ingresa
  **automáticamente** (cantidad y precio exactos de la factura), sin
  tener que repetir el proceso producto por producto.
- Hay un check para desactivar esto en una factura puntual, por si la
  registras antes de que llegue físicamente la mercadería.

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
├── app.py                 -> Backend principal: servidor, login/CSRF, Logística/Inventario,
│                              Administración, respaldo, Sentry opcional, /health
├── core.py                 -> Utilidades compartidas por app.py y modulos/ (DB, roles, etc.)
├── modulos/
│   ├── operativo.py         -> Órdenes de trabajo, garantías, clientes, dashboard operativo
│   ├── finanzas.py          -> CxC/CxP, Caja Chica, Estado de Resultados, respaldos
│   ├── facturacion.py        -> Facturación Electrónica (DTE simulado, ver nota arriba)
│   └── convertidores.py      -> Módulo de Convertidores de Torque
├── inventario.db            -> Base de datos local (se crea sola; en Railway vive en el volumen)
├── requirements.txt          -> Dependencias de Python
├── tests/                    -> 13 archivos, ~595 tests (unittest, sin dependencias externas)
├── templates/                 -> Una plantilla Jinja2 por página (index, operativo, finanzas, ...)
└── static/
    ├── css/style.css          -> Estilos visuales (compartidos por todas las páginas)
    └── js/                     -> Un archivo JS por módulo (app.js, operativo.js, finanzas.js, ...)
```

## Próximos pasos

El grueso de la lista original (subirlo a internet, dashboard, proveedores,
Caja Chica, Facturación, Convertidores, WhatsApp, etc.) ya está construido y
en uso. Lo que queda realmente pendiente, a la fecha:

1. **Facturación Electrónica real (SII)** — solo si el taller alguna vez
   necesita emitir DTE válidos desde este sistema en vez de por otro medio
   (ver nota arriba). No es necesario para operar hoy.
2. Mejoras menores de monitoreo/hardening que no bloquean el uso diario
   (quedan registradas como comentarios en el código donde corresponde,
   ej. `SENTRY_DSN` en `app.py` para activar reporte de errores).

Si esta lista vuelve a quedar desactualizada, lo más simple es preguntarle
directamente a quien esté ayudando con el sistema "¿qué falta antes de
productivo?" en vez de confiar en este archivo a ciegas.



