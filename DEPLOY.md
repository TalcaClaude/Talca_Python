# Subir el sistema a internet (Railway)

Esta guía te deja el sistema andando en una dirección web real (algo como
`https://taller-inventario-produccion.up.railway.app`), accesible desde
cualquier computador o celular con internet, sin que tengas que instalar
nada en el taller.

Uso **Railway** porque: es gratis para empezar, no necesitas saber de
servidores, y lo más importante — a diferencia de otras plataformas
gratuitas, permite un **disco persistente** para que tu base de datos
(`inventario.db`) no se borre cada vez que se actualiza la app.

No puedo hacer este despliegue por ti porque no tengo acceso a internet
desde donde trabajo, pero son ~10 pasos y te acompaño si algo falla.

## Paso 1: Crear cuenta en Railway

1. Ve a https://railway.app
2. Crea una cuenta (puedes usar tu cuenta de GitHub, es lo más simple).

## Paso 2: Subir el código a GitHub

Railway despliega desde un repositorio de GitHub. Si nunca has usado
GitHub:

1. Ve a https://github.com y crea una cuenta (si no tienes).
2. Crea un repositorio nuevo, por ejemplo `taller-inventario` (puede ser
   privado).
3. Sube el contenido de esta carpeta (`taller-inventario/`) a ese
   repositorio. La forma más fácil sin usar la terminal:
   - En la página del repositorio, botón "Add file" → "Upload files".
   - Arrastra todos los archivos y carpetas de esta carpeta (`app.py`,
     `requirements.txt`, `Procfile`, `templates/`, `static/`, etc.)
   - **No subas** el archivo `inventario.db` si ya tienes uno con datos
     de prueba (mejor que la base de datos nazca limpia en el servidor).
   - Click "Commit changes".

## Paso 3: Crear el proyecto en Railway

1. En Railway, click "New Project" → "Deploy from GitHub repo".
2. Autoriza a Railway a acceder a tu cuenta de GitHub si te lo pide.
3. Selecciona el repositorio `taller-inventario`.
4. Railway va a detectar automáticamente que es una app Python (por el
   `requirements.txt` y el `Procfile`) y va a empezar a desplegarla sola.

## Paso 4: Agregar el disco persistente (importante)

Sin este paso, cada vez que actualices la app se te borraría la base de
datos.

1. Dentro del proyecto en Railway, ve a la pestaña del servicio (la app).
2. Ve a "Settings" → busca la sección "Volumes".
3. Click "New Volume".
   - Mount path: `/data`
4. Guarda.

## Paso 5: Configurar la variable de entorno de la base de datos

1. En el mismo servicio, ve a la pestaña "Variables".
2. Agrega estas dos variables:
   - Nombre: `DB_PATH` — Valor: `/data/inventario.db`
   - Nombre: `SECRET_KEY` — Valor: cualquier texto largo y aleatorio (por
     ejemplo, 40 caracteres al azar). Esto firma las sesiones de login;
     sin esto, el sistema de usuarios no es seguro.
3. Guarda. Railway va a reiniciar la app sola con este cambio.

## Paso 6: Obtener tu dirección web pública

1. Ve a la pestaña "Settings" del servicio → sección "Networking".
2. Click "Generate Domain".
3. Railway te da una dirección tipo
   `https://taller-inventario-produccion.up.railway.app`.
4. Esa es la dirección que vas a compartir con las 2-3 personas del
   taller. Funciona desde cualquier navegador, en PC o celular.

## Paso 7: Probarlo

1. Abre la dirección que te dio Railway.
2. Deberías ver la aplicación funcionando, igual que en tu PC, pero ahora
   accesible desde cualquier lugar.
3. Crea un producto de prueba para confirmar que se guarda bien.

## ¿Y si necesito actualizar la app más adelante?

Cada vez que subamos una versión nueva del código:
1. Reemplaza los archivos en tu repositorio de GitHub (mismo método del
   Paso 2: "Add file" → "Upload files", o usando Git si ya te manejas).
2. Railway detecta el cambio y vuelve a desplegar solo, en 1-2 minutos.
3. Gracias al disco persistente del Paso 4, tu base de datos (productos,
   facturas, movimientos) no se pierde entre actualizaciones.

## Costos

Railway da un monto de crédito gratis al mes para partir. Para un uso
como el tuyo (2-3 personas, uso liviano) probablemente entra dentro del
plan gratuito o cuesta unos pocos dólares al mes. Si en algún momento
te pide una tarjeta de crédito para continuar, es porque superaste el
plan gratuito — puedo ayudarte a estimar el costo real cuando llegue ese
momento.

## Seguridad

Esta versión **ya tiene login** (usuario y contraseña). Antes de compartir
la dirección con el resto del taller:
1. Cambia la contraseña del usuario `admin` (ver sección de arriba).
2. Crea una cuenta para cada persona desde la pestaña "Usuarios", en vez
   de compartir una sola cuenta entre todos — así cada movimiento queda
   asociado a quién lo hizo de verdad.
3. No olvides la variable `SECRET_KEY` del Paso 5: sin ella, el login
   funciona pero no es seguro.

## Si en algún momento se te vuelven a borrar los datos

Primero, entra a `https://tu-dominio.up.railway.app/api/estado` en el
navegador (sí, directamente esa dirección). Te muestra algo como:

```json
{
  "db_path": "/data/inventario.db",
  "parece_disco_persistente": true,
  "archivo_existe": true,
  "cantidad_productos": 12
}
```

- Si `db_path` **no** empieza con `/data`, la variable `DB_PATH` no está
  bien configurada (revisa el Paso 5).
- Si `parece_disco_persistente` da `false` aunque `db_path` diga
  `/data/...`, probablemente el volumen no está realmente montado (revisa
  el Paso 4 — a veces al editar el "Mount Path" Railway crea un volumen
  nuevo en vez de reutilizar el que ya tenías).
- Anota `cantidad_productos` antes y después de cada actualización, para
  confirmar si los datos sobreviven.

**Antes de cada actualización**, desde la aplicación misma, entra a
**Administración → Respaldo de datos** y descarga una copia. Si algo sale
mal, desde ese mismo lugar puedes subir ese archivo y restaurar todo tal
como estaba.

## Respaldo automático diario (recomendado)

Además del botón manual, el sistema puede mandarse solo un respaldo por
correo cada día — así, aunque te olvides de hacerlo a mano, siempre tienes
una copia reciente en tu bandeja de entrada. Es gratis.

**Importante:** Railway, en sus planes gratis/Trial/Hobby, **bloquea el
tráfico saliente por el protocolo SMTP** (el que usa Gmail tradicional)
para evitar que se use para mandar spam — aunque las credenciales estén
perfectas, el envío falla con un error de red ("Network is unreachable").
Por eso el método recomendado de acá abajo no usa SMTP: usa **Resend**,
un servicio que manda el correo por HTTPS (el mismo protocolo con el que
tu navegador carga cualquier página web), que no choca con ese bloqueo.

### Paso 1: crear una cuenta en Resend

1. Ve a https://resend.com y crea una cuenta gratis (no pide tarjeta de
   crédito). Puedes usar el mismo correo al que quieres que lleguen los
   respaldos (por ejemplo tu Gmail del taller).
2. Una vez adentro, ve a la sección "API Keys" y crea una nueva
   ("Create API Key"). Dale cualquier nombre, por ejemplo
   "Inventario Taller".
3. Copia la clave que te muestra (empieza con `re_...`). Solo se muestra
   una vez — guárdala en un lugar seguro.

Con la cuenta gratis, sin configurar nada más, puedes mandarte correos
**a ti mismo** (al correo con el que te registraste en Resend) sin límite
práctico para este uso (100 correos al día). Como el respaldo diario es
justamente para que te llegue a ti, no hace falta verificar un dominio
propio ni configurar nada extra.

### Paso 2: agregar las variables en Railway

En la pestaña "Variables" del servicio, agrega:

- `RESEND_API_KEY` — la clave del Paso 1 (empieza con `re_`)
- `SMTP_DESTINATARIO` — el mismo correo con el que te registraste en
  Resend (a ese correo es al único que puede mandarte mientras no
  verifiques un dominio propio)

Railway va a reiniciar la app sola. Desde ese momento, todos los días se
manda un respaldo solo, y queda registrado en Administración → Respaldo de
datos (ahí puedes ver la fecha del último envío exitoso, y mandar uno
de prueba para confirmar que quedó bien configurado).

### Alternativa: SMTP con Gmail (solo si no usas Railway, o tu plan lo permite)

Si en algún momento corres el sistema en otro lado que sí permita SMTP
saliente (tu propio PC, u otro hosting), puedes usar Gmail directamente
en vez de Resend:

1. Ve a https://myaccount.google.com/apppasswords (necesitas tener la
   verificación en dos pasos activada en tu cuenta de Google; si no la
   tienes, activala primero en https://myaccount.google.com/security).
2. Ponle un nombre, por ejemplo "Inventario Taller".
3. Google te va a mostrar un código de 16 letras (algo como
   `abcd efgh ijkl mnop`). Cópialo — es lo que vas a usar como contraseña
   en el paso siguiente. Guárdalo en un lugar seguro, no lo vuelves a ver.
4. Agrega estas variables (en Railway o donde corresponda):
   - `SMTP_USER` — tu correo de Gmail completo (ej: `tutaller@gmail.com`)
   - `SMTP_PASSWORD` — el código de 16 letras del paso anterior (sin espacios)
   - `SMTP_DESTINATARIO` — a qué correo quieres que lleguen los respaldos

Si están configuradas tanto `RESEND_API_KEY` como las variables de SMTP,
el sistema usa Resend primero.

## Respaldo automático del disco completo (mejora futura — requiere plan Pro)

Railway tiene una función propia, nativa, para respaldar todo el disco
persistente (`/data`) — más simple y más sólida que el respaldo por
correo porque no depende de que el correo logre enviarse ni de internet
saliente: hace "fotografías" (snapshots) automáticas de todo el disco,
no solo de la base de datos.

**Esta función solo está disponible en el plan Pro de Railway (USD
20/mes)** — el plan Hobby (USD 5/mes, el que se usa hoy) no la incluye.
Por ahora el sistema se queda en Hobby, así que **el respaldo por correo
(arriba) es la protección automática principal**, y conviene reforzarlo
con el hábito de descargar un respaldo manual (botón "Descargar respaldo
(.db)" dentro del sistema) antes de cada actualización importante.

Si más adelante el taller crece y tiene sentido el gasto adicional, subir
a Pro y activar esto es la mejora más directa para dar un paso más de
seguridad. Queda documentado acá para cuando llegue ese momento.

### Cómo activarla (cuando se suba a plan Pro)

1. Dentro de tu proyecto en Railway, entra al servicio de la app.
2. Ve a la pestaña **"Backups"** (al lado de "Deployments", "Variables",
   etc. — ya la viste en una captura, existe desde antes, solo hay que
   configurarla).
3. Activa un calendario automático. Recomendado para tu caso:
   - **Diario**: se guarda una copia cada 24 horas, se conservan las
     últimas 6.
   - **Semanal**: se guarda una copia cada 7 días, se conserva 1 mes.
   - Puedes activar ambos calendarios a la vez — no cuesta más
     configurarlos juntos, y da más opciones de a qué fecha volver si
     necesitas restaurar.
4. También puedes generar una copia manual en cualquier momento con el
   botón correspondiente en esa misma pestaña — por ejemplo, justo antes
   de una actualización importante.

### Cómo restaurar si algo sale mal

1. En la pestaña "Backups", busca la copia por fecha y click en
   "Restore".
2. Railway te muestra el cambio antes de aplicarlo (no pasa nada hasta
   que confirmes).
3. **Importante**: al restaurar una copia, se pierden las copias más
   nuevas que esa (las que se hicieron después). Si tienes dudas, primero
   descarga un respaldo manual desde el botón "Descargar respaldo (.db)"
   del sistema, para no perder el estado actual antes de restaurar uno
   viejo.

### Resumen: por qué conviene usar los dos métodos juntos (cuando se active este)

- **Respaldos de Railway** (al subir a Pro): protección principal,
  automática, cubre todo el disco, restauración con un clic.
- **Respaldo por correo** (Resend/Gmail, ya activo hoy): copia
  secundaria, fuera de Railway — si algún día Railway completo tuviera
  un problema serio o perdieras acceso a la cuenta, igual tienes una
  copia reciente en tu Gmail.

Esta combinación es justamente lo que se conoce como la "regla 3-2-1" de
respaldos: varias copias, guardadas de más de una forma, y al menos una
fuera de donde vive el sistema original. Por ahora, mientras se está en
plan Hobby, el correo cumple ese rol solo — no es tan robusto como
tenerlo combinado con los snapshots de Railway, pero sigue siendo una
protección real y automática.

## Respaldos automáticos locales (recuperar hasta "hace pocos minutos")

El respaldo por correo (arriba) manda una copia completa **una vez al día**.
Si un día se borra o se corrompe algo por error, en el peor caso se pierde
todo lo que pasó desde el último correo (hasta 24 horas de trabajo).

Para bajar eso a minutos en vez de horas, el sistema además crea, cada 60
minutos (configurable con la variable de entorno
`RESPALDO_LOCAL_INTERVALO_MINUTOS`; era cada 15 minutos hasta julio 2026),
una copia consistente de la base de datos (usando `VACUUM INTO`, que
garantiza que la copia no queda a medio escribir aunque se tome mientras
alguien está guardando algo) en una carpeta aparte (`respaldos_locales/`)
dentro del mismo disco persistente de Railway. Guarda como máximo las
últimas 48 copias (`RESPALDO_LOCAL_MAX_COPIAS`, 2 días a ese ritmo -- bajado
de 96 en julio 2026, ver el incidente más abajo) y va borrando las más
viejas solas.

⚠ **El intervalo NO controla cuánto disco ocupan los respaldos** — eso lo
determina `RESPALDO_LOCAL_MAX_COPIAS` × el tamaño de la base de datos en ese
momento (la rotación borra la más vieja apenas se supera ese número). Subir
el intervalo solo hace que se tarde más en llegar a ese tope y que cubra más
historial por la misma cantidad de disco. Si el disco vuelve a quedar
justo, lo que hay que bajar es `RESPALDO_LOCAL_MAX_COPIAS`, no (solo) el
intervalo -- ver el incidente de julio 2026 más abajo.

Además, desde ese mismo incidente, antes de crear cada respaldo el sistema
revisa el espacio libre del disco (`RESPALDO_LOCAL_MIN_MB_LIBRES`, 100 MB
por defecto): si queda menos que eso, se salta el respaldo en vez de
arriesgarse a terminar de llenar el disco.

Esto se maneja todo desde la propia app, sin agregar ningún proveedor
externo nuevo: **Administración → Respaldo de datos**, sección "Respaldos
automáticos locales" (antes vivía como un botón "Respaldo" suelto dentro de
Logística; se juntó con Usuarios y Mantenimiento en un solo panel de
administración en julio 2026). Ahí se ve la lista de copias disponibles con
su fecha y tamaño, un botón para crear una copia manual en el momento (por
ejemplo justo antes de un cambio delicado), y un botón "Restaurar esta
copia" por cada una. La misma pantalla también tiene una sección "Uso de
disco del servidor" con el desglose de qué está ocupando espacio, y un
botón "Liberar espacio ahora" que borra de urgencia todos los respaldos
menos los 3 más recientes.

⚠ Restaurar reemplaza TODOS los datos actuales por los de esa copia -- solo
para recuperarse de un problema real. Todos estos endpoints exigen sesión
de administrador.

### Incidente de julio 2026: el volumen de Railway llegó al 95%

El volumen persistente estaba configurado en 500 MB. Con el intervalo de 15
minutos y `RESPALDO_LOCAL_MAX_COPIAS=96` de ese momento, los respaldos
automáticos por sí solos llegaron a ocupar la mayoría del disco disponible
(96 copias completas de la base de datos), y el disco casi lleno terminó
rompiendo TODO el sistema -- cualquier escritura a SQLite (no solo los
respaldos) empezó a fallar con `disk I/O error`. Se resolvió: (1) subiendo
el volumen de Railway a 5 GB, (2) agregando el endpoint de diagnóstico de
disco y el botón de limpieza de urgencia (ver arriba), y (3) el guard de
espacio libre mínimo antes de crear un respaldo nuevo. El disco se seguía
llenando más rápido de lo esperado incluso con el volumen ya en 5 GB, así
que además se bajó `RESPALDO_LOCAL_MAX_COPIAS` de 96 a 48 para acotar
también el techo real, hasta confirmar que el consumo se estabiliza. Si
esto vuelve a pasar, el primer lugar para mirar es **Administración →
Respaldo de datos → Uso de disco del servidor**.

### Por qué no se usó Litestream + Cloudflare R2

Se evaluó (y se llegó a construir) una alternativa con **Litestream**,
replicando la base de datos en streaming a un bucket de Cloudflare R2, que
hubiera bajado la ventana de pérdida posible a segundos en vez de minutos.
Se descartó a favor de este enfoque más simple porque:

- Suma un proveedor externo más (Cloudflare) a una lista que ya incluye
  Railway, GitHub, Twilio y las plataformas de IA -- y minimizar cuántos
  proveedores hay que administrar es una prioridad explícita.
- Requiere un `Dockerfile` y un proceso aparte corriendo junto a la app, más
  piezas que pueden fallar.
- Para el volumen de trabajo real del taller, la diferencia entre "hace 15
  minutos" y "hace unos segundos" no justifica esa complejidad extra --
  sobre todo combinado con el respaldo diario por correo como red de
  seguridad adicional, ya fuera de Railway.

### Los dos respaldos trabajando juntos

- **Copias automáticas locales (cada 60 min)**: protección principal para el
  día a día, restaurable con un clic desde la propia app.
- **Correo diario (Resend/Gmail)**: copia secundaria, fuera de Railway, útil
  si algún día se pierde el volumen persistente completo (algo que las
  copias locales, al vivir en el mismo disco, no pueden cubrir) o se
  necesita una copia offline.

## Dominio propio (para que la dirección se vea profesional)

Por defecto, Railway te da una dirección como
`https://talleres.up.railway.app` — funciona perfecto, pero se nota que
es una dirección "gratuita" de la plataforma. Para verte más serio ante
clientes o una comitiva, puedes usar tu propio dominio (por ejemplo
`https://sistema.tutaller.cl` o `https://tutaller.com`), sin cambiar nada
del sistema ni perder el disco persistente ni los datos.

### Paso 1: comprar un dominio

Un dominio se compra por año, normalmente entre USD 10 y 20 al año para
una terminación `.com`, o el equivalente si eliges una terminación local
(por ejemplo `.cl` en Chile, a través de NIC Chile). Algunos lugares
donde comprarlo: Namecheap, Cloudflare Registrar, GoDaddy, o el registrador
local de tu país. No importa cuál elijas — todos permiten después apuntar
el dominio a Railway.

### Paso 2: agregar el dominio en Railway

1. Dentro de tu proyecto en Railway, ve a la pestaña "Settings" del
   servicio → sección "Networking" → "Custom Domain".
2. Escribe el dominio que compraste (ej: `sistema.tutaller.cl`).
3. Railway te va a mostrar dos registros para agregar en la configuración
   DNS de donde compraste el dominio:
   - Un registro **CNAME**
   - Un registro **TXT**
4. Entra al panel de tu registrador de dominios (Namecheap, NIC Chile,
   etc.), busca la sección "DNS" o "Manejo de DNS", y agrega esos dos
   registros exactamente como Railway te los muestra.
5. Espera — puede tardar desde minutos hasta un par de horas en
   propagarse. Railway detecta el cambio solo y activa un certificado de
   seguridad (HTTPS) gratis y automático para tu dominio nuevo.
6. Una vez activo, `https://talleres.up.railway.app` sigue funcionando
   igual (por si acaso), pero ya puedes compartir y usar tu dirección
   propia como la principal.

No puedo comprar el dominio ni tocar la configuración DNS por ti —
son cuentas y pagos que son tuyos — pero si algo no calza al agregar los
registros, mándame una captura de lo que ves y te ayudo a revisarlo.

