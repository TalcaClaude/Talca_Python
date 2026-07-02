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
2. Agrega una nueva variable:
   - Nombre: `DB_PATH`
   - Valor: `/data/inventario.db`
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

## Seguridad (léelo antes de compartir la dirección)

Esta primera versión **no tiene sistema de usuarios/contraseña**:
cualquiera que tenga la dirección web puede entrar y modificar datos.
Como es solo para 2-3 personas de confianza en tu taller, por ahora está
bien, pero si más adelante quieres restringir el acceso (usuario y
contraseña, o que cada persona tenga su propio login), es el siguiente
paso natural a construir — avísame cuando quieras eso.
