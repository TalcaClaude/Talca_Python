#!/bin/sh
# Punto de entrada del contenedor en Railway.
#
# - Si las variables de Litestream/R2 NO están configuradas todavía, arranca
#   la app normal (gunicorn), exactamente como hoy -- así este cambio no
#   rompe nada mientras el usuario no haya creado su cuenta de Cloudflare R2.
# - Si SÍ están configuradas: antes de arrancar, si la base de datos local no
#   existe (disco nuevo, o el volumen se perdió), intenta restaurarla desde
#   el último punto replicado en R2. Después arranca la app envuelta en
#   "litestream replicate", que sigue subiendo cada cambio (WAL) a R2 en
#   streaming mientras la app corre.
set -e

# --timeout 300: por defecto gunicorn corta cualquier request que tarde más
# de 30 segundos (lo trata como "worker colgado" y lo mata). Eso alcanza a
# cortar a mitad de camino operaciones legítimas pero lentas, como restaurar
# un respaldo grande (guardar el archivo + validar + reemplazar la base +
# correr todas las migraciones de nuevo sobre los datos restaurados). Se
# sube a 5 minutos para dar margen de sobra sin dejarlo indefinido.
GUNICORN_CMD="gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --timeout 300"

if [ -z "$LITESTREAM_BUCKET" ] || [ -z "$LITESTREAM_ACCESS_KEY_ID" ] || [ -z "$LITESTREAM_SECRET_ACCESS_KEY" ]; then
  echo "[entrypoint] Litestream no está configurado (faltan variables LITESTREAM_*) -- arrancando sin respaldo continuo."
  exec $GUNICORN_CMD
fi

DB_PATH="${DB_PATH:-/data/inventario.db}"

if [ ! -f "$DB_PATH" ]; then
  echo "[entrypoint] No existe $DB_PATH localmente -- intentando restaurar desde R2 (si hay una copia)..."
  litestream restore -if-replica-exists -config /app/litestream.yml "$DB_PATH" || true
fi

echo "[entrypoint] Arrancando con replicación continua a R2 (Litestream)."
exec litestream replicate -config /app/litestream.yml -exec "$GUNICORN_CMD"
