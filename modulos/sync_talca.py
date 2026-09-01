"""
modulos/sync_talca.py — trae hacia este sistema los datos que se siguen
escribiendo en Talca.PHP (el sistema legado: PHP 7.2 + MariaDB) durante el
mes en que ambos conviven. Hoy: clientes, vehículos (que acá son `equipos`) y
órdenes de trabajo. El inventario viene después con el mismo patrón.

Ver CLAUDE.md (raíz del repositorio) para el porqué de las decisiones de
arquitectura. Las tres que más condicionan este archivo:

1. **Pull, no push.** Es Python quien sale a preguntar "¿qué cambió?", cada
   tanto. Talca no empuja nada. Motivo: en Talca hay escrituras que ocurren
   dentro de Grocery CRUD, sin pasar por ningún modelo donde se pueda
   enganchar un push -- si se escapa una, los datos divergen en silencio.
   Además, si una vuelta del pull falla, la marca de agua no avanza y la
   siguiente trae lo pendiente: se repara solo.

2. **El match es por `talca_id`, jamás por RUT.** Los datos reales tienen 442
   clientes sin RUT, 90 RUT repetidos entre 195 filas y 127 con dígito
   verificador inválido. El `id` numérico de Talca es lo único estable: si
   mañana alguien corrige un RUT mal escrito, el vínculo no se rompe.

3. **Los timestamps son texto opaco.** Este módulo no parsea ni una sola
   fecha de sincronización, y no le hace aritmética a ninguna. La marca de
   agua es el campo `hasta` que devuelve Talca (hora del servidor de Talca),
   se guarda tal cual y se devuelve tal cual. Hasta el margen de 2 minutos de
   solapamiento lo aplica Talca dentro de su propio SQL. Así no hay dos
   relojes que conciliar: si Python usara el suyo, cualquier desfase con el
   hosting haría que se salten registros sin que nadie se entere.
   (La única fecha que sí se toca es `created_at` al copiarla a `creado_en`,
   y es un `replace(" ", "T")` de cosmética, no un cálculo.)

4. **Cada entidad depende de la anterior.** `equipos.cliente_id` y
   `ordenes_trabajo.cliente_id` son NOT NULL, así que un vehículo no se puede
   guardar sin su dueño ya importado ni una orden sin su cliente. Por eso
   `todo` corre en orden, y cada entidad a secas se niega a correr si la
   anterior nunca se sincronizó (ver `precondiciones` en cada `_Entidad*`).

5. **Talca es la fuente de verdad, salvo donde no sabe hablar.** Todo lo que
   Talca escribe se copia encima de lo que haya acá. Las dos excepciones son
   el estado de la orden y el del presupuesto: Talca no modela el flujo
   interno del taller (no tiene forma de decir 'reparacion' ni 'listo'), así
   que su ABIERTA nunca degrada el avance local. Ver `_estado_python`, que es
   donde está el razonamiento completo.

Se corre de dos formas, y las dos escriben el mismo `sync_talca_estado`:

    python -m modulos.sync_talca                  # todo, en orden
    python -m modulos.sync_talca clientes --dry-run
    python -m modulos.sync_talca vehiculos
    python -m modulos.sync_talca ordenes

y el hilo de fondo de app.py (`_hilo_sync_talca`), que llama a
`sincronizar_entidad` para las tres entidades cada SYNC_INTERVALO_SEGUNDOS.
**Ese hilo solo arranca si la variable está definida y es > 0**, así que en
local sigue existiendo únicamente el comando de arriba. Este módulo no sabe
nada del hilo: no importa nada de app.py (ver el docstring de core.py, es lo
que evita el import circular).

Sobre la conexión a la base: este módulo importa de core.py y nunca de app.py
ni de otro módulo, igual que el resto de modulos/ (ver el docstring de
core.py: es lo que evita el import circular).
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata

from core import _conectar_db, now_iso

try:
    import requests
except ImportError:  # pragma: no cover - solo pasa si el hosting no instaló la dependencia
    requests = None


# Cuántas filas pedir por página. 200 sobre 4.656 clientes son 24 vueltas.
SYNC_LIMITE_DEFECTO = int(os.environ.get("TALCA_SYNC_LIMITE", "200"))

# Tope de páginas por corrida. No es una expectativa de volumen, es un
# cinturón de seguridad: si el endpoint tuviera un error de paginación que lo
# hiciera devolver siempre la misma página, el comando termina con un error
# claro en vez de girar para siempre contra el servidor de producción.
SYNC_MAX_PAGINAS = int(os.environ.get("TALCA_SYNC_MAX_PAGINAS", "500"))

SYNC_TIMEOUT_DEFECTO = 30.0

# El hosting de Talca (Bluehosting) CORTA la conexión cuando el User-Agent
# dice "python-requests": no responde un 403, cierra el socket sin mandar
# ninguna respuesta HTTP. Del lado de Python eso llega como
# RemoteDisconnected / "connection aborted", que parece un problema de red y
# manda a diagnosticar en la dirección equivocada.
#
# Comprobado a mano: el mismo curl que funciona, agregándole
# -A "python-requests/2.32.3", falla con "server closed abruptly (missing
# close_notify)". Lo único que cambia es esa cabecera.
#
# Por eso el User-Agent es explícito y propio, y va en TODAS las peticiones.
TALCA_USER_AGENT = "TalcaSync/1.0"

# Reintentos por página, SOLO para fallas de conexión (ver
# ClienteTalcaHTTP._get_con_reintentos). Una carga inicial son 24 páginas
# contra un hosting compartido: que un corte puntual tire abajo la corrida
# entera sería absurdo.
#
# Los errores HTTP con respuesta (400, 401, 500) NO se reintentan: son
# determinísticos, la respuesta ya llegó y repetirla solo demora el
# diagnóstico.
SYNC_INTENTOS_POR_PAGINA = 3
# Segundos de espera antes del reintento 1, 2 y 3. Con 3 intentos se usan las
# dos primeras (1 s y 2 s); la de 4 s queda para el día que alguien suba
# SYNC_INTENTOS_POR_PAGINA a 4.
SYNC_ESPERAS_REINTENTO = (1, 2, 4)


# ---------------------------------------------------------------------------
# Migración -- cada módulo separado es dueño de la suya (ver el comentario de
# init_operativo_db en app.py:init_db). Se llama desde init_db() DESPUÉS de
# init_operativo_db(), que es quien crea la tabla `clientes`.
# ---------------------------------------------------------------------------

def init_sync_talca_db(db):
    columnas = [c[1] for c in db.execute("PRAGMA table_info(clientes)").fetchall()]

    # El id que esta fila tiene en Talca. Queda NULL en los clientes creados
    # acá: en SQLite un índice único considera cada NULL distinto de los
    # demás, así que pueden convivir miles de clientes locales sin talca_id y
    # seguir siendo imposible tener dos filas con el mismo talca_id.
    if "talca_id" not in columnas:
        db.execute("ALTER TABLE clientes ADD COLUMN talca_id INTEGER")
        db.commit()

    # Talca guarda la comuna en su propia columna. Acá no existía porque el
    # sistema nuevo nació con la comuna escrita dentro de la dirección; se
    # agrega para no perder el dato al sincronizar.
    if "comuna" not in columnas:
        db.execute("ALTER TABLE clientes ADD COLUMN comuna TEXT DEFAULT ''")
        db.commit()

    # Marca los clientes que el propio sync crea y necesita volver a
    # encontrar en cada corrida (hoy: el genérico al que se cuelgan los
    # vehículos huérfanos, ver _obtener_cliente_generico). No se usa el
    # nombre para buscarlo porque el taller lo va a renombrar en cuanto lo
    # vea en pantalla, ni un id fijo porque chocaría con el AUTOINCREMENT.
    if "marcador_sync" not in columnas:
        db.execute("ALTER TABLE clientes ADD COLUMN marcador_sync TEXT DEFAULT ''")
        db.commit()

    # -- Vehículos: en Talca son `vehiculos`, acá son `equipos`.
    columnas_equipos = [c[1] for c in db.execute("PRAGMA table_info(equipos)").fetchall()]

    # vehiculos.idd. La patente NO sirve de llave: hay 254 patentes repetidas
    # entre 570 filas del dump (CXMANO sola aparece 29 veces, es el comodín
    # para trabajos de caja de cambios, no un auto).
    if "talca_id" not in columnas_equipos:
        db.execute("ALTER TABLE equipos ADD COLUMN talca_id INTEGER")
        db.commit()

    # vehiculos.dueno tal como viene de Talca. Se guarda aunque ya esté
    # resuelto en cliente_id porque es lo que permite reenganchar solo un
    # vehículo que quedó en el cliente genérico: el que cambia después es el
    # CLIENTE, y ese cambio no vuelve a traer el vehículo por el pull (ver
    # reconciliar_duenos_pendientes).
    if "talca_dueno_id" not in columnas_equipos:
        db.execute("ALTER TABLE equipos ADD COLUMN talca_dueno_id INTEGER")
        db.commit()

    # Odómetro de la última visita. 4.043 de las 5.103 filas del dump (79%)
    # lo tienen cargado, con mediana de 114.135 km: es dato real que el
    # taller usa, no se descarta.
    if "kilometraje" not in columnas_equipos:
        db.execute("ALTER TABLE equipos ADD COLUMN kilometraje INTEGER")
        db.commit()

    # -- Órdenes de trabajo: en Talca `orden_trabajo`, acá `ordenes_trabajo`.
    #
    # Cada columna de acá abajo existe porque el dato no tiene destino en el
    # esquema actual y se perdería. Lo que SÍ calza con una columna que ya
    # existe se mapea sin agregar nada (requerimiento -> trabajo_a_realizar,
    # precio -> precio_referencial, coniva -> precio_iva, kilometraje,
    # estanque -> estanque_nivel, origen -> referencia, obs_presupuesto ->
    # presupuesto_notas, fecha_salida -> fecha_entrega_real).
    columnas_ordenes = [c[1] for c in db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]
    columnas_nuevas_ordenes = {
        # La llave del sync: orden_trabajo.id. NO se empareja por patente ni
        # por RUT (hay 285 patentes repetidas y 90 RUT repetidos), ni por
        # legacy_ot_no, que es nullable y lo escribió un backfill por patente.
        "talca_id": "INTEGER",

        # id_dueno / id_vehiculo tal como vienen. Se guardan aunque ya estén
        # resueltos en cliente_id/equipo_id, por el mismo motivo que
        # equipos.talca_dueno_id: son lo único que permite reenganchar una
        # orden huérfana cuando el cliente o el vehículo aparecen después. El
        # que cambia entonces es el cliente o el vehículo, y la orden NO se
        # vuelve a recibir por el pull mientras su propio updatedDtm no se
        # mueva (ver reconciliar_ordenes_pendientes).
        "talca_dueno_id": "INTEGER",
        "talca_vehiculo_id": "INTEGER",

        # La patente es un hecho de la ORDEN, no del vehículo: el dueño del
        # sistema cambió el modelo de Talca hace unos meses justamente para
        # poder corregir la patente de una OT sin tocar la del auto, y hay 14
        # OT donde difieren a propósito. Por eso se guarda acá y no se deriva
        # del equipo (ver resolver_patente).
        "patente": "TEXT DEFAULT ''",

        # orden_trabajo.nombre: el contratante tal como se imprimió. Difiere
        # del nombre del cliente en 147 OT, y en las 88 sin cliente resoluble
        # es el ÚNICO rastro de quién era.
        "cliente_nombre_talca": "TEXT DEFAULT ''",

        # El literal crudo del estado, sin mapear. `estados_ot` tiene 2 filas
        # pero en los datos hay 4 valores distintos: los otros dos los escribe
        # editNota a mano. Guardar el crudo es lo que hace que el mapeo se
        # pueda revisar y corregir después sin volver a pedirle nada a Talca.
        "estado_talca": "TEXT DEFAULT ''",

        # 'Nueva Orden' (5.589), 'Garantia' (1.440) o 'Inventario' (26). Las
        # tres van a esta misma tabla; ver _EntidadOrdenes para el porqué de
        # que las garantías NO vayan al módulo de garantías.
        "tipo_talca": "TEXT DEFAULT ''",

        # `tnr` = trabajo no realizado: 49 órdenes cerradas SIN trabajo. No
        # deben contar como trabajos realizados en ninguna métrica, y la
        # bandera es lo que las distingue de una cancelación normal.
        "trabajo_no_realizado": "INTEGER NOT NULL DEFAULT 0",

        # isDeleted: 84 filas. El borrado lógico existe y se usa (50 de esas
        # están ABIERTAS). Se importan a propósito -- ver _estado_python.
        "borrada_en_talca": "INTEGER NOT NULL DEFAULT 0",

        # fecha_cierre y fecha_salida son hechos DISTINTOS: ambas pobladas en
        # 6.845 filas y difieren en 393. fecha_salida va a fecha_entrega_real,
        # que ya existe; el cierre necesita columna propia.
        "fecha_cierre_talca": "TEXT",

        # 5.744 filas. El taller las lee.
        "observaciones_talca": "TEXT DEFAULT ''",

        # El número de la OT de origen de una garantía, rescatado del texto
        # libre de `observaciones` ("GARANTIA OT. 1160"). Solo 637 de las
        # 1.440 garantías lo tienen, y por eso mismo las garantías NO se
        # llevan al módulo de garantías (donde orden_origen_id es el vínculo
        # central y quedaría vacío en el 56% de las filas). Se registra igual
        # para que, si algún día se hace la separación, no haya que volver a
        # parsear texto libre. Ver _referencia_garantia.
        "garantia_ot_origen_talca": "INTEGER",

        # La fila cruda completa de Talca. Evita crear ocho columnas más para
        # campos con 30-200 filas de dato (glosa, glosa_obs, nombre_externo,
        # motor, factura, empleado1/2/3, createdBy...) y deja la importación
        # lossless y auditable.
        #
        # REGLA DE PROMOCIÓN: si un campo pasa a usarse en pantalla, se
        # promueve a columna. Sin esa regla el JSON se vuelve un basurero.
        # REGLA DE ESCRITURA: se REGENERA completo en cada update, nunca se
        # fusiona con el anterior -- si se fusionara, un campo borrado en
        # Talca sobreviviría acá para siempre y el JSON dejaría de ser un
        # espejo fiel.
        "datos_talca_json": "TEXT DEFAULT ''",
    }
    for columna, definicion in columnas_nuevas_ordenes.items():
        if columna not in columnas_ordenes:
            db.execute(f"ALTER TABLE ordenes_trabajo ADD COLUMN {columna} {definicion}")
    db.commit()

    db.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clientes_talca_id ON clientes (talca_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_equipos_talca_id ON equipos (talca_id);
        CREATE INDEX IF NOT EXISTS idx_equipos_talca_dueno_id ON equipos (talca_dueno_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ordenes_talca_id ON ordenes_trabajo (talca_id);
        CREATE INDEX IF NOT EXISTS idx_ordenes_talca_dueno_id ON ordenes_trabajo (talca_dueno_id);
        CREATE INDEX IF NOT EXISTS idx_ordenes_talca_vehiculo_id ON ordenes_trabajo (talca_vehiculo_id);
        CREATE INDEX IF NOT EXISTS idx_ordenes_tipo_talca ON ordenes_trabajo (tipo_talca);

        -- Índice único PARCIAL: puede haber miles de clientes con
        -- marcador_sync = '' (todos los normales), pero como mucho uno de
        -- cada marcador. Así "no crear un genérico nuevo en cada corrida"
        -- deja de ser una convención del código y pasa a ser un invariante
        -- que la base hace cumplir.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clientes_marcador_sync
            ON clientes (marcador_sync) WHERE marcador_sync != '';

        -- Una fila por entidad sincronizada ('clientes', 'vehiculos',
        -- 'ordenes', y más adelante 'inventario').
        CREATE TABLE IF NOT EXISTS sync_talca_estado (
            entidad TEXT PRIMARY KEY,
            marca_agua TEXT NOT NULL DEFAULT '',
            ultima_corrida_en TEXT NOT NULL DEFAULT '',
            ultimo_resultado TEXT NOT NULL DEFAULT '',
            ultimo_detalle TEXT NOT NULL DEFAULT '',
            filas_recibidas INTEGER NOT NULL DEFAULT 0,
            filas_creadas INTEGER NOT NULL DEFAULT 0,
            filas_actualizadas INTEGER NOT NULL DEFAULT 0,
            filas_omitidas INTEGER NOT NULL DEFAULT 0
        );

        -- Registro de las filas que el sync decidió NO importar (ver
        -- motivo_omision más abajo). Existe para que "se saltó 470 filas" sea
        -- una afirmación auditable y no un número en un log que ya se perdió.
        -- El UNIQUE hace que volver a ver la misma fila actualice el registro
        -- en vez de acumular una copia por cada corrida.
        CREATE TABLE IF NOT EXISTS sync_talca_omitidos (
            id INTEGER PRIMARY KEY,
            entidad TEXT NOT NULL,
            talca_id INTEGER NOT NULL,
            motivo TEXT NOT NULL,
            datos_json TEXT NOT NULL DEFAULT '',
            visto_en TEXT NOT NULL,
            UNIQUE (entidad, talca_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sync_omitidos_motivo ON sync_talca_omitidos (entidad, motivo);

        -- Filas que SÍ se importaron pero que hubo que colgar de otra cosa
        -- porque su referencia no resolvía (hoy: vehículos cuyo dueño no
        -- existe en clientes). Va aparte de sync_talca_omitidos a propósito:
        -- esa tabla significa "no se importó", y meter acá filas que sí
        -- entraron haría que la lista mienta justo cuando alguien la abra
        -- para revisar.
        --
        -- resuelto_en queda en blanco mientras el vehículo siga colgado del
        -- cliente genérico, y se estampa cuando el dueño real aparece y el
        -- pase de reconciliación lo reengancha.
        CREATE TABLE IF NOT EXISTS sync_talca_reasignados (
            id INTEGER PRIMARY KEY,
            entidad TEXT NOT NULL,
            talca_id INTEGER NOT NULL,
            motivo TEXT NOT NULL,
            referencia_talca_id INTEGER,
            asignado_a_id INTEGER,
            datos_json TEXT NOT NULL DEFAULT '',
            visto_en TEXT NOT NULL,
            resuelto_en TEXT NOT NULL DEFAULT '',
            UNIQUE (entidad, talca_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sync_reasignados_pendientes
            ON sync_talca_reasignados (entidad, resuelto_en);

        -- Filas locales que YA existían y que el sync reclamó en vez de
        -- duplicar (ver _adoptar_orden). Hoy solo pasa con las órdenes que
        -- dejó el importador histórico, emparejadas por legacy_ot_no.
        --
        -- Va en tabla propia y no en sync_talca_reasignados porque adoptar no
        -- es reasignar: la fila no se colgó de otra cosa, se identificó con
        -- una que ya estaba. Y se registra SIEMPRE, incluso cuando el cliente
        -- coincidió, porque legacy_ot_no lo escribió un backfill que
        -- emparejaba por patente y hay 285 patentes repetidas: la lista tiene
        -- que poder revisarse entera, no solo los conflictos.
        CREATE TABLE IF NOT EXISTS sync_talca_adopciones (
            id INTEGER PRIMARY KEY,
            entidad TEXT NOT NULL,
            talca_id INTEGER NOT NULL,
            resultado TEXT NOT NULL,
            fila_local_id INTEGER,
            cliente_id_local INTEGER,
            cliente_id_resuelto INTEGER,
            detalle TEXT NOT NULL DEFAULT '',
            visto_en TEXT NOT NULL,
            UNIQUE (entidad, talca_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sync_adopciones_resultado
            ON sync_talca_adopciones (entidad, resultado);
        """
    )
    db.commit()

    # -- Push Python -> Talca: columnas en clientes -------------------------
    # Se agregan acá (y no en init_operativo_db) porque son parte del
    # contrato de sincronización, igual que talca_id y marcador_sync.

    columnas = [c[1] for c in db.execute("PRAGMA table_info(clientes)").fetchall()]

    # El updated_at que Talca devolvió la última vez que esta fila se actualizó
    # (por pull o por push exitoso). Se guarda tal cual -- texto opaco, nunca
    # parseado -- y se manda como 'talca_updated_at_conocido' al hacer un PUT.
    # Talca lo compara con su updated_at actual para detectar conflictos.
    if "talca_updated_at" not in columnas:
        db.execute("ALTER TABLE clientes ADD COLUMN talca_updated_at TEXT NOT NULL DEFAULT ''")
        db.commit()

    # Flag de atomicidad: se pone en 1 en la MISMA transacción en que se
    # escribe el cambio local, antes del commit. El pull sync lo comprueba
    # antes de sobreescribir: si está en 1, la fila local es más reciente que
    # Talca y no se pisa. Se vuelve a 0 cuando el push confirma (200 o 409).
    if "push_pendiente" not in columnas:
        db.execute("ALTER TABLE clientes ADD COLUMN push_pendiente INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # -- Push Python -> Talca: las mismas dos columnas en equipos -----------
    # Mismo significado exacto que en clientes. `equipos` es la segunda (y por
    # ahora última) entidad bidireccional.
    columnas_equipos = [c[1] for c in db.execute("PRAGMA table_info(equipos)").fetchall()]

    if "talca_updated_at" not in columnas_equipos:
        db.execute("ALTER TABLE equipos ADD COLUMN talca_updated_at TEXT NOT NULL DEFAULT ''")
        db.commit()

    if "push_pendiente" not in columnas_equipos:
        db.execute("ALTER TABLE equipos ADD COLUMN push_pendiente INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # -- Push Python -> Talca: las mismas dos columnas en ordenes_trabajo ---
    columnas_ot = [c[1] for c in db.execute("PRAGMA table_info(ordenes_trabajo)").fetchall()]

    # Ojo con la escala: este timestamp NO es UTC como el de clientes y
    # equipos. `orden_trabajo.updatedDtm` es DATETIME en hora de Chile, escrito
    # por date() de PHP. Se guarda tal cual, como texto opaco, y solo se compara
    # contra sí mismo (es lo que viaja como talca_updated_at_conocido en el
    # PUT). Nunca mezclarlo con el de las otras dos entidades.
    if "talca_updated_at" not in columnas_ot:
        db.execute("ALTER TABLE ordenes_trabajo ADD COLUMN talca_updated_at TEXT NOT NULL DEFAULT ''")
        db.commit()

    if "push_pendiente" not in columnas_ot:
        db.execute("ALTER TABLE ordenes_trabajo ADD COLUMN push_pendiente INTEGER NOT NULL DEFAULT 0")
        db.commit()

    _crear_trigger_push_ordenes(db)

    db.executescript(
        """
        -- Cola de reintentos para el push Python -> Talca.
        --
        -- La entrada se crea dentro de la misma transacción que la escritura
        -- local (antes del commit), así la cola existe incluso si el proceso
        -- muere entre el commit y el primer intento de push.
        --
        -- resuelto_en vacío = pendiente. Se considera resuelto tanto el push
        -- exitoso como el conflicto (409): en ambos casos push_pendiente vuelve
        -- a 0 y no hay nada más que reintentar.
        CREATE TABLE IF NOT EXISTS sync_talca_push_pendientes (
            id              INTEGER PRIMARY KEY,
            entidad         TEXT NOT NULL,          -- 'clientes' | 'vehiculos'
            python_id       INTEGER NOT NULL,        -- id local de la fila
            talca_id        INTEGER,                 -- NULL para operacion='crear'
            operacion       TEXT NOT NULL,           -- 'crear' | 'actualizar'
            campos_json     TEXT NOT NULL DEFAULT '{}',  -- solo los campos que cambiaron
            talca_updated_at_conocido TEXT NOT NULL DEFAULT '',  -- para el locking optimista
            -- Solo vehículos. 1 = el payload necesita `dueno`, que es el
            -- talca_id del cliente y NO se guarda acá: se resuelve al momento
            -- de pushear leyendo equipos.cliente_id -> clientes.talca_id.
            --
            -- Que no esté guardado es deliberado. Si se guardara habría que
            -- copiarlo desde algún lado al encolar, y el candidato a mano es
            -- el cliente_id LOCAL: mandarlo sería colgar el vehículo de otro
            -- cliente sin ningún error de por medio. Resolviéndolo tarde, el
            -- único origen posible del campo es clientes.talca_id.
            --
            -- Efecto secundario buscado: si el cliente todavía no tiene
            -- talca_id (su propio push no confirmó), la entrada queda EN
            -- ESPERA en vez de fallar. Ver _ejecutar_push_entrada.
            requiere_dueno  INTEGER NOT NULL DEFAULT 0,
            -- UUID generado al encolar: viaja en cada intento del POST para que
            -- Talca devuelva el mismo id si ya procesó esta entrada. Esto hace
            -- el reintento de un CREATE idempotente incluso cuando la respuesta
            -- se perdió en la red (sin este campo, el reintento crea un duplicado).
            -- Solo aplica a operacion='crear'; en 'actualizar' se ignora.
            idempotency_key TEXT NOT NULL DEFAULT '',
            intentos        INTEGER NOT NULL DEFAULT 0,
            proximo_intento TEXT NOT NULL DEFAULT '',  -- ISO; vacío = disponible ahora
            ultimo_error    TEXT NOT NULL DEFAULT '',
            creado_en       TEXT NOT NULL,
            resuelto_en     TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_push_pendientes_activos
            ON sync_talca_push_pendientes (entidad, resuelto_en, proximo_intento);

        -- Registro permanente de conflictos: Talca tenía un cambio más
        -- reciente que el nuestro al momento del push. Python no sobrescribió.
        -- Se guarda para revisión manual; el conflicto se considera resuelto
        -- (Talca gana) en cuanto se registra acá.
        CREATE TABLE IF NOT EXISTS sync_talca_conflictos (
            id                   INTEGER PRIMARY KEY,
            entidad              TEXT NOT NULL,
            python_id            INTEGER NOT NULL,
            talca_id             INTEGER NOT NULL,
            nuestra_version_json TEXT NOT NULL DEFAULT '{}',
            version_talca_json   TEXT NOT NULL DEFAULT '{}',
            talca_updated_at     TEXT NOT NULL DEFAULT '',
            registrado_en        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_push_conflictos_entidad
            ON sync_talca_conflictos (entidad, registrado_en);
        """
    )
    db.commit()

    # Migraciones de la cola: columnas agregadas después de que la tabla ya
    # existía en bases desplegadas. Se hacen por ALTER para no perder las
    # entradas pendientes que pueda haber adentro.
    cols_cola = [c[1] for c in db.execute("PRAGMA table_info(sync_talca_push_pendientes)").fetchall()]
    if "idempotency_key" not in cols_cola:
        db.execute(
            "ALTER TABLE sync_talca_push_pendientes ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
        )
        db.commit()
    if "requiere_dueno" not in cols_cola:
        db.execute(
            "ALTER TABLE sync_talca_push_pendientes ADD COLUMN requiere_dueno INTEGER NOT NULL DEFAULT 0"
        )
        db.commit()


# Columnas de `ordenes_trabajo` cuyo cambio tiene que llegar a Talca. Es la
# lista que vigila el trigger de más abajo; el mapeo a los nombres de Talca
# vive en push_talca.CAMPOS_ORDENES, que es de donde sale esta lista.
COLUMNAS_PUSH_ORDENES = (
    "trabajo_a_realizar", "precio_referencial", "precio_iva", "kilometraje",
    "estanque_nivel", "observaciones_talca", "presupuesto_estado", "referencia",
    "patente", "fecha_cierre_talca", "fecha_entrega_real", "estado_talca",
    "cliente_nombre_talca", "tipo_talca",
    # `estado` no viaja (viaja estado_talca), pero cambiarlo puede cambiar el
    # carácter abierto/cerrado de la orden y con eso recalcular estado_talca.
    "estado",
)


def _crear_trigger_push_ordenes(db):
    """Trigger que levanta push_pendiente cuando cambia algo que Talca tiene
    que ver.

    **Por qué un trigger y no un hook en cada endpoint.** `ordenes_trabajo`
    tiene DIEZ escritores repartidos entre operativo.py, convertidores.py y
    app.py -- el tablero, el dictado, la recepción, el presupuesto, el envío
    del presupuesto, el agente de WhatsApp, la transición de cuentas por
    cobrar. Es exactamente la situación que hizo elegir pull en vez de push
    del lado de Talca (ver "Sync asimétrico" en CLAUDE.md): si el hook se pega
    a mano en cada sitio, el escritor número once que alguien agregue dentro
    de tres semanas no lo va a llevar, y esa orden va a divergir en silencio.

    El trigger corre DENTRO de la transacción del endpoint, así que da la
    misma atomicidad que poner el flag a mano, y no se puede olvidar.

    Los endpoints conocidos igual disparan el push de inmediato después del
    commit (ver push_talca.disparar_push): el trigger da correctitud, el
    disparo da latencia. Un escritor no contemplado viaja en la vuelta
    siguiente del hilo en vez de al instante, que es la degradación correcta.

    Ojo con el pull: `_upsert_orden` escribe estas mismas columnas con lo que
    manda Talca, y sin cuidado el trigger marcaría para push lo que se acaba
    de recibir. Por eso el upsert apaga el flag en una segunda sentencia --
    ver la nota ahí."""
    cambios = " OR ".join(
        f"OLD.{c} IS NOT NEW.{c}" for c in COLUMNAS_PUSH_ORDENES
    )
    db.execute("DROP TRIGGER IF EXISTS trg_ordenes_push_pendiente")
    db.execute(
        f"""
        CREATE TRIGGER trg_ordenes_push_pendiente
        AFTER UPDATE ON ordenes_trabajo
        FOR EACH ROW
        WHEN ({cambios})
        BEGIN
            UPDATE ordenes_trabajo SET push_pendiente = 1 WHERE id = NEW.id;
        END
        """
    )

    # Alta: una orden creada ACÁ tiene que llegar a Talca, que es quien
    # reparte los números de OT. Una traída por el pull, no -- ya está allá.
    #
    # `talca_id IS NULL` es lo que las distingue, y no es una heurística: el
    # pull siempre inserta con el id de Talca puesto (es su llave), y el alta
    # local no tiene de dónde sacarlo. Sin esta condición, la carga inicial
    # marcaría las 7.116 órdenes para empujarlas de vuelta.
    db.execute("DROP TRIGGER IF EXISTS trg_ordenes_push_alta")
    db.execute(
        """
        CREATE TRIGGER trg_ordenes_push_alta
        AFTER INSERT ON ordenes_trabajo
        FOR EACH ROW
        WHEN NEW.talca_id IS NULL
        BEGIN
            UPDATE ordenes_trabajo SET push_pendiente = 1 WHERE id = NEW.id;
        END
        """
    )
    db.commit()


# ---------------------------------------------------------------------------
# Capa de acceso a la API de Talca. Nada de acá abajo habla directo con
# `requests`: todo pasa por ClienteTalca, igual que Facturación pasa todo por
# EmisorDTE (ver modulos/facturacion.py). Así los tests corren contra
# ClienteTalcaFalso, sin red y sin servidor.
# ---------------------------------------------------------------------------

class ClienteTalcaError(Exception):
    """Cualquier problema al hablar con Talca: falta configuración, no hubo
    red, respondió un HTTP de error, devolvió algo que no es JSON, o la
    respuesta no cumple el contrato. El mensaje está pensado para mostrarse
    tal cual en consola, y nunca incluye la API key."""


class ClienteTalca:
    """Contrato que cualquier forma de hablar con Talca tiene que cumplir.

    `obtener_pagina` devuelve el dict de la respuesta tal como lo define
    CLAUDE.md: la lista bajo la clave de la entidad (`clientes`, `vehiculos`),
    más hasta, siguiente_desde, siguiente_desde_id, hay_mas y total.
    `desde`/`desde_id` se pasan tal cual vinieron en la página anterior --
    este lado no los interpreta."""

    nombre = "base"

    def obtener_pagina(self, entidad, desde=None, desde_id=None, limite=SYNC_LIMITE_DEFECTO):
        raise NotImplementedError

    def obtener_clientes(self, desde=None, desde_id=None, limite=SYNC_LIMITE_DEFECTO):
        return self.obtener_pagina("clientes", desde=desde, desde_id=desde_id, limite=limite)

    def obtener_vehiculos(self, desde=None, desde_id=None, limite=SYNC_LIMITE_DEFECTO):
        return self.obtener_pagina("vehiculos", desde=desde, desde_id=desde_id, limite=limite)

    def ping(self):
        """Una sola llamada a /api/ping, para diagnóstico. NO participa del
        sync: no lee ni escribe nada, no toca la marca de agua.

        Devuelve los hechos crudos del transporte, sin interpretarlos:

            {"url": ..., "status_code": 200, "json": {...}|None, "texto": ...}

        Un HTTP de error NO levanta excepción -- 401 y 403 son justamente lo
        que el diagnóstico necesita poder mostrar. Solo levanta
        ClienteTalcaError cuando no llegó ninguna respuesta HTTP, y en ese
        caso deja el error original en __cause__ para que se pueda clasificar
        (ver diagnostico_conexion)."""
        raise NotImplementedError


def url_entidad(base_url, entidad):
    """Arma la URL de una entidad a partir de TALCA_API_URL.

    **TALCA_API_URL es la raíz del SITIO de Talca**, no la de la API: este
    módulo le agrega `/api/<entidad>`. Se definió así porque el prefijo
    `/api` no es una variable de despliegue -- sale del nombre del
    controlador de CodeIgniter (`application/controllers/Api.php`), así que
    es parte del contrato y no algo que cada quien configure.

        https://claude.tallerescalificados.cl  ->  .../api/clientes

    Se toleran las cuatro formas en que la variable se escribió alguna vez en
    este proyecto -- con y sin barra final, con y sin `/api` al final -- y
    todas dan la misma URL. La primera versión de esto pegaba la entidad
    directo a la base y pedía `/clientes` en vez de `/api/clientes`; el
    endpoint respondía 404 y el sync moría sin filas. No vale la pena que eso
    dependa de cómo alguien tipeó una variable de entorno."""
    base = (base_url or "").strip().rstrip("/")
    if base.lower().endswith("/api"):
        base = base[: -len("/api")].rstrip("/")
    return f"{base}/api/{entidad}"


class ClienteTalcaHTTP(ClienteTalca):
    """El de verdad. Configurable por variable de entorno, leídas al momento
    de llamar y no al importar, para que cambiar la configuración no obligue
    a reiniciar el proceso (mismo criterio que get_emisor_dte()):

        TALCA_API_URL      raíz del sitio, ej. https://claude.tallerescalificados.cl
                           (ver url_entidad: el módulo agrega /api/<entidad>)
        TALCA_API_KEY      la clave del header X-API-Key
        TALCA_API_TIMEOUT  segundos, 30 por defecto

    `dormir` existe para que los tests no esperen de verdad entre reintentos.
    """

    nombre = "http"

    def __init__(self, base_url=None, api_key=None, timeout=None, dormir=None):
        self.base_url = (base_url if base_url is not None else os.environ.get("TALCA_API_URL", "")).strip()
        self.api_key = (api_key if api_key is not None else os.environ.get("TALCA_API_KEY", "")).strip()
        self.timeout = float(timeout or os.environ.get("TALCA_API_TIMEOUT") or SYNC_TIMEOUT_DEFECTO)
        self._dormir = dormir or time.sleep

    def obtener_pagina(self, entidad, desde=None, desde_id=None, limite=SYNC_LIMITE_DEFECTO):
        if requests is None:  # pragma: no cover
            raise ClienteTalcaError("Falta la dependencia 'requests' (pip install -r requirements.txt).")
        if not self.base_url:
            raise ClienteTalcaError(
                "Falta la variable de entorno TALCA_API_URL "
                "(la raíz del sitio de Talca, ej. https://claude.tallerescalificados.cl)."
            )
        if not self.api_key:
            raise ClienteTalcaError("Falta la variable de entorno TALCA_API_KEY.")

        params = {"limite": limite}
        if desde:
            params["desde"] = desde
            # desde_id solo tiene sentido acompañando a desde: es la marca de
            # que esto es continuación de página y que Talca NO debe volver a
            # aplicar el margen de 2 minutos.
            if desde_id is not None:
                params["desde_id"] = desde_id

        url = url_entidad(self.base_url, entidad)
        resp = self._get_con_reintentos(url, params)

        # La URL completa va en todos los mensajes de error a propósito: fue
        # lo que permitió ver de un vistazo que se estaba pidiendo /clientes
        # en vez de /api/clientes.
        if resp.status_code != 200:
            raise ClienteTalcaError(f"Talca respondió HTTP {resp.status_code} en {url} -- {_detalle_error(resp)}")

        try:
            datos = resp.json()
        except ValueError as exc:
            raise ClienteTalcaError(
                f"Talca respondió algo que no es JSON en {url}: {(resp.text or '')[:200]!r}"
            ) from exc

        if not isinstance(datos, dict):
            raise ClienteTalcaError(f"Talca respondió un JSON que no es un objeto: {type(datos).__name__}")
        # Un 200 con {"error": ...} no debería pasar, pero si pasa es mejor
        # tratarlo como error que procesar una lista vacía y dar la corrida
        # por buena (avanzando la marca de agua sobre datos que nunca vimos).
        if entidad not in datos and datos.get("error"):
            raise ClienteTalcaError(f"Talca devolvió un error: {datos.get('codigo', '')} {datos.get('error')}".strip())
        return datos

    def ping(self):
        """Ver ClienteTalca.ping. Pasa por el MISMO _get_con_reintentos que
        las páginas del sync -- mismo User-Agent, mismos reintentos, mismo
        timeout -- porque el objetivo es reproducir exactamente la petición
        que hace el sync, no una parecida. Si esta llamada pasa y el sync no,
        el problema no es de red."""
        if requests is None:  # pragma: no cover
            raise ClienteTalcaError("Falta la dependencia 'requests' (pip install -r requirements.txt).")

        url = url_entidad(self.base_url, PING_ENTIDAD)
        resp = self._get_con_reintentos(url, {})
        try:
            cuerpo = resp.json()
        except ValueError:
            cuerpo = None
        return {
            "url": url,
            "status_code": resp.status_code,
            "json": cuerpo,
            "texto": (getattr(resp, "text", "") or "")[:MAX_TEXTO_DIAGNOSTICO],
        }

    def _get_con_reintentos(self, url, params):
        """Un GET que reintenta SOLO cuando no llegó ninguna respuesta HTTP.

        Que `requests.get` levante una excepción significa justamente eso: se
        cayó la conexión, expiró el timeout, falló el DNS o el TLS. Ese es el
        caso que se recupera solo esperando un poco. En cambio, un 401 o un
        500 ya son una respuesta: ahí no se reintenta nada (ver el comentario
        de SYNC_INTENTOS_POR_PAGINA)."""
        cabeceras = {"X-API-Key": self.api_key, "User-Agent": TALCA_USER_AGENT}
        ultimo_error = None

        for intento in range(1, SYNC_INTENTOS_POR_PAGINA + 1):
            try:
                return requests.get(url, params=params, headers=cabeceras, timeout=self.timeout)
            except Exception as exc:  # requests.RequestException y cualquier fallo de red/DNS/TLS
                ultimo_error = exc
                if intento < SYNC_INTENTOS_POR_PAGINA:
                    self._dormir(SYNC_ESPERAS_REINTENTO[intento - 1])

        raise ClienteTalcaError(
            f"No se pudo conectar con Talca ({url}) en {SYNC_INTENTOS_POR_PAGINA} intentos: {ultimo_error}"
        ) from ultimo_error


def _detalle_error(resp):
    """Texto legible del cuerpo de una respuesta de error, prefiriendo el
    {"error", "codigo"} del contrato y cayendo al texto crudo recortado."""
    try:
        cuerpo = resp.json()
    except ValueError:
        return f"{(resp.text or '')[:200]!r}"
    if isinstance(cuerpo, dict) and (cuerpo.get("error") or cuerpo.get("codigo")):
        return f"{cuerpo.get('codigo', '')} {cuerpo.get('error', '')}".strip()
    return f"{str(cuerpo)[:200]!r}"


class ClienteTalcaFalso(ClienteTalca):
    """Para los tests: se le arman las páginas de antemano y devuelve una por
    llamada, dejando registrado con qué parámetros lo llamaron (self.llamadas)
    para poder afirmar cosas sobre el bucle de paginación. Un elemento que sea
    una excepción se levanta en vez de devolverse, para simular una caída a
    mitad de corrida."""

    nombre = "falso"

    def __init__(self, paginas=(), pings=(), base_url="https://falso.example", api_key="falsa"):
        self.paginas = list(paginas)
        self.llamadas = []
        # El diagnóstico lee estos dos para informar si la configuración está
        # puesta; el doble los trae con valor para que ese chequeo no sea lo
        # que corte la prueba. Un test que quiera ejercitar "falta la key" usa
        # ClienteTalcaHTTP con api_key="".
        self.base_url = base_url
        self.api_key = api_key
        self.pings = list(pings)
        self.pings_pedidos = 0

    def obtener_pagina(self, entidad, desde=None, desde_id=None, limite=SYNC_LIMITE_DEFECTO):
        self.llamadas.append({"entidad": entidad, "desde": desde, "desde_id": desde_id, "limite": limite})
        if not self.paginas:
            raise ClienteTalcaError("ClienteTalcaFalso: se pidió una página más de las preparadas.")
        pagina = self.paginas.pop(0)
        if isinstance(pagina, Exception):
            raise pagina
        return pagina

    def ping(self):
        self.pings_pedidos += 1
        if not self.pings:
            raise ClienteTalcaError("ClienteTalcaFalso: se pidió un ping más de los preparados.")
        respuesta = self.pings.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


def get_cliente_talca():
    return ClienteTalcaHTTP()


# ---------------------------------------------------------------------------
# Diagnóstico de conexión (no es parte del sync)
# ---------------------------------------------------------------------------
# Sirve para una pregunta que no se puede responder desde un equipo local:
# ¿el hosting de Talca contesta desde donde corre el sistema desplegado?
# Bluehosting ya demostró que corta la conexión por User-Agent, y mod_security
# suele bloquear rangos de datacenter -- los dos fallos se ven igual desde
# Python (RemoteDisconnected / connection aborted), y ninguno se puede
# reproducir desde una casa o una oficina.
#
# Por eso lo que importa acá no es "funcionó / no funcionó", sino DISTINGUIR
# los modos de falla: no es lo mismo que el socket se corte (bloqueo) que un
# 401 (la clave), un 404 (Api.php no desplegado) o un timeout (lentitud).
# Cada uno se arregla en un lugar distinto.

PING_ENTIDAD = "ping"

# Cuánto texto crudo de la respuesta se conserva. Lo suficiente para
# reconocer una página de bloqueo del WAF, no tanto como para llenar la
# pantalla con un HTML entero.
MAX_TEXTO_DIAGNOSTICO = 500

# Nombres de clase (propios y heredados) con que requests, urllib3 y la
# librería estándar reportan un timeout. Se comparan por NOMBRE y no con
# isinstance para no importar requests.exceptions: este módulo tiene que poder
# importarse aunque requests no esté instalado (ver el try/except del
# principio).
#
# Solo hay lista para el timeout, a propósito. No existe la lista simétrica de
# "errores de conexión" porque no serviría: en requests TODA excepción hereda
# de RequestException, que hereda de IOError (= OSError), así que una lista de
# nombres se queda corta apenas aparece una subclase nueva y el fallo más
# importante -- el socket cortado -- terminaría clasificado como
# "desconocido". Como _get_con_reintentos solo levanta cuando NO llegó
# ninguna respuesta HTTP, todo lo que no sea un timeout ya es, por
# definición, un problema de conexión.
_TIPOS_TIMEOUT = {
    "Timeout", "TimeoutError", "ConnectTimeout", "ReadTimeout",
    "ConnectTimeoutError", "ReadTimeoutError",
}


def _nombres_de_tipo(exc):
    return {clase.__name__ for clase in type(exc).__mro__}


def _clasificar_fallo_transporte(causa):
    """Traduce el error de red original a (clase, explicación).

    `causa` es el error que levantó requests, rescatado del __cause__ que
    _get_con_reintentos deja puesto. Puede ser None si el fallo no vino de la
    red (ej. falta la dependencia)."""
    if causa is None:
        return "desconocido", (
            "No llegó ninguna respuesta HTTP y no quedó registrado el error original."
        )

    # El timeout se evalúa primero: en requests, ConnectTimeout hereda de
    # Timeout Y de ConnectionError, y el diagnóstico útil es el primero.
    if _nombres_de_tipo(causa) & _TIPOS_TIMEOUT:
        return "timeout", (
            "El servidor no respondió dentro del timeout. La conexión no fue rechazada ni "
            "cortada: o el hosting está lento, o la petición quedó colgada. No es la firma "
            "de un bloqueo -- un bloqueo corta el socket, no lo deja abierto."
        )

    return "conexion", (
        "No llegó NINGUNA respuesta HTTP: el socket se cortó, o falló el DNS o el TLS. "
        "Esta es exactamente la firma de un bloqueo del hosting: mod_security y el filtro "
        "de User-Agent de Bluehosting cierran la conexión en vez de responder 403. "
        "Si desde un equipo local la misma llamada funciona y desde acá no, el candidato "
        "es un bloqueo por IP de datacenter. El error exacto va en 'tipo' y 'mensaje'."
    )


def _clasificar_respuesta(status, cuerpo_json, texto):
    """Traduce un HTTP que sí llegó a (ok, clase, explicación).

    Que haya llegado una respuesta ya es información: descarta el bloqueo por
    socket cortado, porque el hosting habló."""
    llego = "El hosting respondió, así que la red llega y no hay bloqueo por corte de socket."

    if status == 200:
        if cuerpo_json is None:
            return False, "respuesta_no_json", (
                f"{llego} Pero respondió 200 con un cuerpo que no es JSON. Suele ser una página "
                "intermedia del hosting o del WAF en vez de la respuesta de Api.php. Ver 'texto'."
            )
        return True, None, f"{llego} Api.php contestó el ping correctamente."

    if status == 401:
        return False, "credenciales", (
            f"{llego} El rechazo es de credenciales: la API key falta, está mal escrita, o es la "
            "de otro ambiente (pruebas y producción tienen claves distintas a propósito). "
            "Revisar TALCA_API_KEY contra el sync.php del ambiente al que apunta TALCA_API_URL."
        )

    if status == 403:
        return False, "prohibido", (
            f"{llego} Pero devolvió 403. Si el cuerpo es HTML y no el JSON del contrato, el que "
            "responde es mod_security y no Api.php: ahí el bloqueo es por IP o por la forma de la "
            "petición, y se levanta desde el panel del hosting, no desde este código."
        )

    if status == 404:
        return False, "ruta_inexistente", (
            f"{llego} Pero esa ruta no existe. Los dos motivos habituales: Api.php no está "
            "desplegado en ese ambiente, o TALCA_API_URL apunta al sitio equivocado. Recordar que "
            "TALCA_API_URL es la raíz del SITIO -- este módulo le agrega /api/ping."
        )

    if 500 <= status < 600:
        return False, "error_servidor", (
            f"{llego} El error es del lado de Talca ({status}): PHP se cayó atendiendo el ping. "
            "Mirar el log de errores del hosting."
        )

    return False, "http_inesperado", f"{llego} Respondió HTTP {status}, que no es ninguno de los esperados."


def diagnostico_conexion(cliente=None):
    """Una sola llamada a /api/ping con el cliente HTTP del sync, y un
    informe de qué pasó. Nunca levanta: los fallos son justamente el
    resultado que se quiere ver.

    **Nunca devuelve el valor de la API key**, solo si está definida. La URL
    sí va completa: es un hostname público y es la mitad de los diagnósticos
    (el 404 de /clientes vs /api/clientes se vio así)."""
    cliente = cliente or get_cliente_talca()

    base_url = (getattr(cliente, "base_url", "") or "").strip()
    api_key = (getattr(cliente, "api_key", "") or "").strip()
    timeout = getattr(cliente, "timeout", SYNC_TIMEOUT_DEFECTO)

    informe = {
        "ok": False,
        "url": url_entidad(base_url, PING_ENTIDAD) if base_url else None,
        "user_agent": TALCA_USER_AGENT,
        "duracion_ms": 0,
        "config": {
            "talca_api_url_definida": bool(base_url),
            "talca_api_url": base_url or None,
            "talca_api_key_definida": bool(api_key),
            "timeout_s": timeout,
            "intentos": SYNC_INTENTOS_POR_PAGINA,
        },
        "http_status": None,
        "respuesta": None,
        "error": None,
    }

    # Falta configuración: se informa sin salir a la red. Hacer la llamada
    # igual daría un error de red que mandaría a diagnosticar lo que no es.
    faltantes = [
        nombre for nombre, valor in (("TALCA_API_URL", base_url), ("TALCA_API_KEY", api_key))
        if not valor
    ]
    if faltantes:
        informe["error"] = {
            "clase": "configuracion",
            "tipo": "ConfiguracionIncompleta",
            "mensaje": f"Falta definir {' y '.join(faltantes)}.",
            "explicacion": (
                "No se hizo ninguna llamada: sin esas variables la petición no se puede armar. "
                "En Railway se definen como variables del servicio. TALCA_API_URL es la raíz del "
                "sitio de Talca (ej. https://claude.tallerescalificados.cl), sin /api."
            ),
        }
        return informe

    comienzo = time.perf_counter()
    try:
        resultado = cliente.ping()
    except ClienteTalcaError as exc:
        informe["duracion_ms"] = int((time.perf_counter() - comienzo) * 1000)
        causa = exc.__cause__
        clase, explicacion = _clasificar_fallo_transporte(causa)
        informe["error"] = {
            "clase": clase,
            "tipo": type(causa).__name__ if causa is not None else type(exc).__name__,
            "mensaje": str(exc),
            "explicacion": explicacion,
        }
        return informe
    except Exception as exc:  # el cliente no debería levantar otra cosa; si pasa, se informa igual
        informe["duracion_ms"] = int((time.perf_counter() - comienzo) * 1000)
        informe["error"] = {
            "clase": "inesperado",
            "tipo": type(exc).__name__,
            "mensaje": str(exc),
            "explicacion": "El cliente falló de una forma que este diagnóstico no previó.",
        }
        return informe

    informe["duracion_ms"] = int((time.perf_counter() - comienzo) * 1000)
    informe["url"] = resultado.get("url") or informe["url"]
    informe["http_status"] = resultado.get("status_code")
    informe["respuesta"] = {
        "json": resultado.get("json"),
        "texto": resultado.get("texto") or "",
    }

    ok, clase, explicacion = _clasificar_respuesta(
        informe["http_status"], resultado.get("json"), resultado.get("texto") or ""
    )
    informe["ok"] = ok
    if ok:
        informe["detalle"] = explicacion
    else:
        informe["error"] = {
            "clase": clase,
            "tipo": f"HTTP {informe['http_status']}",
            "mensaje": f"Talca respondió HTTP {informe['http_status']} en {informe['url']}.",
            "explicacion": explicacion,
        }
    return informe


# ---------------------------------------------------------------------------
# Filas que no son datos reales
# ---------------------------------------------------------------------------
# Contado sobre el dump del 10-07-2026 (4.600 filas): 440 completamente
# vacías y 4 de prueba. Nada más.
#
# **Los clientes de imputación de gastos SÍ se importan** (`GASTOS GENERALES
# ABRIL`, `GASTOS ADMON MAYO`, 24 filas). Se filtraban hasta que el taller
# aclaró que administración los usa como registro, junto con sus 24
# "vehículos" espejo. Por eso ya no existe la regla gasto_imputado -- y por
# eso tampoco hay vehículos con dueño omitido: sus dueños ahora entran.
#
# Las filas con nombre y RUT invertidos (3, ej. id 2316 con nombre
# '13530609-6' y rut 'FANNY MUÑOZ') NO se filtran: son clientes reales. Si el
# sync los "arreglara" solo, el día que alguien los corrija en Talca la
# corrección no se vería acá.

NOMBRES_FICTICIOS = {
    "GENERICO", "GENERICA", "PRUEBA", "CLIENTE PRUEBA", "TEST",
    "VENTA", "CLIENTE GENERAL", "SIN NOMBRE",
}

_CAMPOS_CON_DATOS = ("rut", "direccion", "comuna", "email", "telefono")


def _normalizar_nombre(valor):
    """Mayúsculas, sin tildes y con los espacios colapsados -- 'GASTOS ADMON
    MAYO' viene escrito con doble espacio en los datos reales."""
    texto = unicodedata.normalize("NFKD", str(valor or "").strip().upper())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto)


def motivo_omision(fila):
    """Devuelve el motivo por el que esta fila de Talca no se importa, o None
    si es un cliente de verdad."""
    nombre = _normalizar_nombre(fila.get("nombre_cliente"))
    if not nombre and not any(str(fila.get(c) or "").strip() for c in _CAMPOS_CON_DATOS):
        return "fila_vacia"
    if nombre in NOMBRES_FICTICIOS:
        return "cliente_ficticio"
    return None


def motivo_omision_vehiculo(fila):
    """Vehículos: el único descarte es la fila completamente vacía, y hoy no
    existe ninguna (verificado sobre las 5.103 filas del dump).

    Las patentes fuera de formato NO se filtran, aunque parezcan basura:
    `CXMANO`, `VENTA`, `GASTOSGEN`, `SIN/AUTO`. Están enganchadas a órdenes
    de trabajo reales, así que omitirlas dejaría esas OT sin vehículo cuando
    llegue el turno de esa entidad.

    La comprobación queda igual, como guardia para el día que aparezca una
    fila vacía nueva."""
    texto = ("marca", "modelo", "color", "chasis", "modelo_transmision", "patente")
    numeros = ("ano", "puertas", "kilometraje")
    if (not any(_texto(fila.get(c)) for c in texto)
            and not any(_entero_significativo(fila.get(c)) for c in numeros)):
        return "fila_vacia"
    return None


# ---------------------------------------------------------------------------
# Estado del sync
# ---------------------------------------------------------------------------

def leer_estado(db, entidad):
    fila = db.execute("SELECT * FROM sync_talca_estado WHERE entidad = ?", (entidad,)).fetchone()
    if fila is None:
        return {
            "entidad": entidad, "marca_agua": "", "ultima_corrida_en": "",
            "ultimo_resultado": "", "ultimo_detalle": "", "filas_recibidas": 0,
            "filas_creadas": 0, "filas_actualizadas": 0, "filas_omitidas": 0,
        }
    return dict(fila)


def _guardar_estado(db, entidad, marca_agua, resultado):
    db.execute(
        """
        INSERT INTO sync_talca_estado (
            entidad, marca_agua, ultima_corrida_en, ultimo_resultado, ultimo_detalle,
            filas_recibidas, filas_creadas, filas_actualizadas, filas_omitidas
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (entidad) DO UPDATE SET
            marca_agua = excluded.marca_agua,
            ultima_corrida_en = excluded.ultima_corrida_en,
            ultimo_resultado = excluded.ultimo_resultado,
            ultimo_detalle = excluded.ultimo_detalle,
            filas_recibidas = excluded.filas_recibidas,
            filas_creadas = excluded.filas_creadas,
            filas_actualizadas = excluded.filas_actualizadas,
            filas_omitidas = excluded.filas_omitidas
        """,
        (
            entidad, marca_agua, now_iso(), resultado["resultado"], resultado["detalle"][:500],
            resultado["recibidas"], resultado["creadas"], resultado["actualizadas"], resultado["omitidas"],
        ),
    )


def _registrar_omitido(db, entidad, talca_id, motivo, fila):
    db.execute(
        """
        INSERT INTO sync_talca_omitidos (entidad, talca_id, motivo, datos_json, visto_en)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (entidad, talca_id) DO UPDATE SET
            motivo = excluded.motivo,
            datos_json = excluded.datos_json,
            visto_en = excluded.visto_en
        """,
        (entidad, talca_id, motivo, json.dumps(fila, ensure_ascii=False, default=str)[:2000], now_iso()),
    )


# ---------------------------------------------------------------------------
# Upsert de clientes
# ---------------------------------------------------------------------------

def _texto(valor):
    """None -> ''. Cualquier campo de texto de Talca puede venir NULL: en el
    dump hay 935 emails, 87 comunas, 63 direcciones, 8 RUT y 8 teléfonos en
    NULL. Ninguna columna de `clientes` acá acepta NULL, así que se
    normalizan todos, no solo el email."""
    return str(valor).strip() if valor is not None else ""


def _texto_importado(valor):
    """Como _texto pero además colapsa los espacios repetidos de adentro.

    Los datos vienen escritos a mano durante años: 'MAURICIO ARISMENDI ' con
    espacio al final, 'GERARD  QUERELLE' y 'BERNARDO   JARA  OPAZO' con
    espacios dobles y triples. Son 319 filas del dump. Sin esto, buscar
    'GERARD QUERELLE' en el sistema nuevo no encuentra al cliente, y dos
    grabaciones del mismo nombre con distinto espaciado se ven como distintas
    en cualquier listado ordenado.

    Solo toca espacios: no cambia mayúsculas, no saca tildes, no "corrige"
    nada más. Cualquier normalización más agresiva estaría inventando datos
    que después no coinciden con lo que se ve en Talca."""
    return re.sub(r"\s+", " ", _texto(valor))


def _fecha_creacion(valor):
    """`created_at` de Talca ('2020-12-28 12:41:02') al formato ISO que usa el
    resto del sistema. Es cosmética de formato, no aritmética de fechas -- la
    marca de agua nunca pasa por acá.

    En los datos reales hay filas con '0000-00-00 00:00:00' (MariaDB permite
    esa fecha imposible), que se cambia por la hora actual: `creado_en` es NOT
    NULL y una fecha basura rompería cualquier orden o filtro posterior."""
    texto = _texto(valor)
    if not texto or texto.startswith("0000-00-00"):
        return now_iso()
    return texto.replace(" ", "T")


def _entero_significativo(valor):
    """El entero que trae este campo, o None si no hay dato.

    Dos cosas que parecen detalle y no lo son:

    **Los `int(8)` de MariaDB llegan como STRING.** El driver mysqli de PHP
    serializa todo como texto, así que la respuesta real trae
    `{"dueno": "1", "ano": "2006", "puertas": "5", "kilometraje": "0"}`. No
    se arregla del lado PHP porque tocar la configuración de la base
    afectaría a toda la aplicación de Talca. Comparar contra el entero 0
    sería un bug silencioso: `"0" == 0` es False en Python, así que los 331
    `ano`, 624 `puertas` y 1.060 `kilometraje` en cero pasarían sin traducir
    y el sistema mostraría fichas con "año 0, 0 puertas, 0 km".

    **El 0 es un centinela, no un valor.** En Talca esas columnas son NOT
    NULL, así que "no sé" se escribió como 0. Acá `anio` y `kilometraje` son
    nullables: el esquema nuevo sí sabe representar la ausencia de dato. Un
    auto que llega a un taller de transmisiones con 0 km es un odómetro que
    nadie anotó.

    La traducción vale SOLO para el 0. Un `ano` en 1 o en 20255 es un error
    de tipeo del taller y se importa tal cual -- corregirlo sería inventar."""
    texto = _texto(valor)
    if not texto:
        return None
    try:
        numero = int(texto)
    except ValueError:
        return None
    return None if numero == 0 else numero


def _puertas(valor):
    """`equipos.puertas` es TEXT (así nació el esquema, antes de que existiera
    esta integración), así que acá el centinela 0 se traduce a cadena vacía y
    no a NULL. Si alguna vez llegara algo no numérico se copia tal cual: en
    Talca la columna es int, pero no se descarta lo que no se entiende."""
    texto = _texto(valor)
    if not texto.lstrip("-").isdigit():
        return texto
    return "" if int(texto) == 0 else texto


def _numero(valor):
    """El número que trae este campo, CON el cero, o None si no hay dato.

    La contraparte de _entero_significativo, y la diferencia entre las dos es
    deliberada: el centinela solo se aplica donde el cero es IMPOSIBLE como
    lectura real.

    - `kilometraje` usa _entero_significativo: un auto no llega a un taller de
      transmisiones con 0 km, así que ese cero es "nadie lo anotó".
    - `precio` y `estanque` usan esta: 1.247 de las 1.440 garantías cuestan
      $0 de verdad, y `estanque` en 0 es el estanque vacío en el medidor (41
      OT). Traducirlos a NULL borraría dato real.

    Los enteros de MariaDB llegan como STRING (driver mysqli), así que el
    texto es la entrada normal, no un caso raro."""
    texto = _texto(valor)
    if not texto:
        return None
    try:
        return int(texto)
    except ValueError:
        pass
    try:
        return float(texto)
    except ValueError:
        return None


def _bandera(valor):
    """Un 0/1 a partir de lo que Talca guarde como bandera. Acepta el 0/1 de
    `coniva` e `isDeleted` (que llegan como "0"/"1") y también los SI/NO
    escritos a mano. Cualquier texto que no sea reconocible como "apagado"
    cuenta como encendido: una bandera con un valor inesperado es algo que
    alguien marcó, y perderla en silencio es peor que verla de más."""
    texto = _texto(valor).upper()
    if not texto or texto in {"0", "NO", "N", "FALSE", "0.0"}:
        return 0
    return 1


def _fecha_opcional(valor):
    """Una fecha de Talca que puede no estar, al formato ISO del sistema.

    Devuelve None cuando viene vacía, NULL o con el '0000-00-00' que MariaDB
    permite. A diferencia de _fecha_creacion NO cae a la hora actual: acá el
    destino es nullable (fecha_entrega_real, fecha_cierre_talca) y "no hay
    fecha de salida" es un hecho, no un dato que haya que inventar."""
    texto = _texto(valor)
    if not texto or texto.startswith("0000-00-00"):
        return None
    return texto.replace(" ", "T")


def _fecha_actualizacion(fila):
    """`updatedDtm` al ISO del sistema, con el mismo criterio de respaldo que
    el trigger trg_orden_trabajo_insert del lado de Talca: si viene en cero se
    usa `createdDtm`, y si ese también está en cero, la hora actual.

    Hacía falta porque la creación de órdenes en Talca (Nota_model::addNewUser)
    no escribía `updatedDtm` y la columna es DATETIME NOT NULL sin default: 15
    órdenes del dump nacieron con '0000-00-00 00:00:00'. Ya está corregido en
    la base de pruebas (backfill + trigger), pero el respaldo se queda: si el
    trigger falta en producción, `actualizado_en` es NOT NULL acá y una fecha
    imposible rompe cualquier orden o filtro posterior."""
    texto = _texto(fila.get("updatedDtm"))
    if not texto or texto.startswith("0000-00-00"):
        return _fecha_creacion(fila.get("createdDtm"))
    return texto.replace(" ", "T")


# ---------------------------------------------------------------------------
# El cliente al que se cuelgan los vehículos sin dueño
# ---------------------------------------------------------------------------
# 60 vehículos del dump tienen `dueno` apuntando a un cliente que ya no
# existe en Talca (44 dueños distintos; uno vale 17.579.505, que parece un
# RUT tecleado en el campo equivocado). Son autos reales -- VOLVO S60, SUZUKI
# XL7 -- así que se importan igual, pero `equipos.cliente_id` es NOT NULL y
# hay que colgarlos de alguien.
#
# Este cliente lo crea el sync la primera vez que hace falta, NO la
# migración: en una base donde no haya huérfanos no tiene por qué aparecer un
# cliente fantasma en la lista del taller.

MARCADOR_SIN_DUENO = "sin_dueno_talca"
NOMBRE_SIN_DUENO = "SIN DUEÑO REGISTRADO EN TALCA"
NOTAS_SIN_DUENO = (
    "Cliente creado automáticamente por la sincronización con Talca. Acá se "
    "cuelgan los vehículos cuyo dueño no existe en Talca. Si el dueño real "
    "aparece, el vehículo se reasigna solo en la corrida siguiente. "
    "Se puede renombrar sin romper nada."
)


def _obtener_cliente_generico(db):
    """El id del cliente genérico, creándolo si es la primera vez.

    Se busca por `marcador_sync`, no por nombre (el taller lo va a renombrar
    apenas lo vea) ni por un id fijo (chocaría con el AUTOINCREMENT). Queda
    con `talca_id` NULL porque no existe en Talca."""
    fila = db.execute(
        "SELECT id FROM clientes WHERE marcador_sync = ?", (MARCADOR_SIN_DUENO,)
    ).fetchone()
    if fila is not None:
        return fila["id"]

    cursor = db.execute(
        """
        INSERT INTO clientes (talca_id, nombre, rut, direccion, comuna, telefono,
                              email, notas, creado_en, marcador_sync)
        VALUES (NULL, ?, '', '', '', '', '', ?, ?, ?)
        """,
        (NOMBRE_SIN_DUENO, NOTAS_SIN_DUENO, now_iso(), MARCADOR_SIN_DUENO),
    )
    return cursor.lastrowid


def _registrar_reasignado(db, entidad, talca_id, motivo, referencia_talca_id, asignado_a_id, fila):
    db.execute(
        """
        INSERT INTO sync_talca_reasignados (
            entidad, talca_id, motivo, referencia_talca_id, asignado_a_id,
            datos_json, visto_en, resuelto_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '')
        ON CONFLICT (entidad, talca_id) DO UPDATE SET
            motivo = excluded.motivo,
            referencia_talca_id = excluded.referencia_talca_id,
            asignado_a_id = excluded.asignado_a_id,
            datos_json = excluded.datos_json,
            visto_en = excluded.visto_en,
            resuelto_en = ''
        """,
        (entidad, talca_id, motivo, referencia_talca_id, asignado_a_id,
         json.dumps(fila, ensure_ascii=False, default=str)[:2000], now_iso()),
    )


def reconciliar_duenos_pendientes(db):
    """Reengancha los vehículos que están colgados del cliente genérico y
    cuyo dueño real YA existe. Devuelve cuántos se reasignaron.

    Existe porque el pull nunca los traería de vuelta solos: el que cambia
    cuando aparece el dueño es el CLIENTE, y el vehículo no se vuelve a
    recibir mientras su propio `updated_at` no se mueva. Sin este pase, un
    auto quedaría clavado al genérico para siempre aunque su dueño se hubiera
    recreado en Talca al día siguiente.

    Corre al final de la sincronización de vehículos y también al final de la
    de clientes: es un par de UPDATE con índice, no cuesta nada, y si alguien
    corre solo clientes los reenganches no tienen por qué quedar esperando."""
    generico = db.execute(
        "SELECT id FROM clientes WHERE marcador_sync = ?", (MARCADOR_SIN_DUENO,)
    ).fetchone()
    if generico is None:
        return 0

    pendientes = db.execute(
        """
        SELECT e.id AS equipo_id, e.talca_id AS equipo_talca_id, c.id AS cliente_id
          FROM equipos e
          JOIN clientes c ON c.talca_id = e.talca_dueno_id
         WHERE e.cliente_id = ? AND e.talca_dueno_id IS NOT NULL
        """,
        (generico["id"],),
    ).fetchall()

    for fila in pendientes:
        db.execute(
            "UPDATE equipos SET cliente_id = ? WHERE id = ?",
            (fila["cliente_id"], fila["equipo_id"]),
        )
        db.execute(
            """
            UPDATE sync_talca_reasignados
               SET resuelto_en = ?
             WHERE entidad = 'vehiculos' AND talca_id = ? AND resuelto_en = ''
            """,
            (now_iso(), fila["equipo_talca_id"]),
        )
    return len(pendientes)


def _limpiar_omitidos_ya_importados(db, entidad, tabla):
    """Saca de sync_talca_omitidos las filas que hoy sí están importadas.

    Hace falta cuando una regla de filtrado se afloja: al dejar de descartar
    los 24 clientes de imputación de gastos, sus registros de "omitido"
    quedarían ahí contradiciendo a la tabla `clientes`, y la lista que el
    taller consulta para revisar diría que se saltaron filas que están
    perfectamente cargadas."""
    db.execute(
        f"""
        DELETE FROM sync_talca_omitidos
         WHERE entidad = ?
           AND talca_id IN (SELECT talca_id FROM {tabla} WHERE talca_id IS NOT NULL)
        """,
        (entidad,),
    )


def _upsert_cliente(db, fila):
    """Inserta o actualiza el cliente, buscándolo SOLO por talca_id. Devuelve
    'creada', 'actualizada' o 'saltada'.

    'saltada' significa que la fila tiene push_pendiente=1: Python tiene un
    cambio local aún no confirmado por Talca, y dejar que el pull sobreescriba
    con la versión vieja de Talca rompería el dato. La entrada de cola
    (sync_talca_push_pendientes) ya existe y el job de reintentos la procesará.

    En el update se tocan únicamente los campos que son de Talca. `notas` y
    `creado_en` quedan intactos a propósito: `notas` es un campo local del
    taller y el sync no tiene por qué pisar lo que alguien escribió acá.

    `talca_updated_at` sí se actualiza: es el timestamp de Talca al momento del
    pull, y lo necesita el push para el locking optimista."""
    talca_id = int(fila["id"])
    valores = (
        _texto_importado(fila.get("nombre_cliente")),
        _texto_importado(fila.get("rut")),
        _texto_importado(fila.get("direccion")),
        _texto_importado(fila.get("comuna")),
        _texto_importado(fila.get("telefono")),
        _texto_importado(fila.get("email")),
        _texto(fila.get("updated_at")),  # talca_updated_at
    )

    existente = db.execute(
        "SELECT id, push_pendiente FROM clientes WHERE talca_id = ?", (talca_id,)
    ).fetchone()
    if existente is not None:
        if existente["push_pendiente"]:
            # Hay un push en vuelo o en cola: el dato local es más reciente que
            # el de Talca. No pisar. El job de reintentos resolverá.
            return "saltada"
        db.execute(
            """
            UPDATE clientes
               SET nombre = ?, rut = ?, direccion = ?, comuna = ?,
                   telefono = ?, email = ?, talca_updated_at = ?
             WHERE talca_id = ?
            """,
            valores + (talca_id,),
        )
        return "actualizada"

    db.execute(
        """
        INSERT INTO clientes
            (talca_id, nombre, rut, direccion, comuna, telefono, email,
             talca_updated_at, notas, creado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?)
        """,
        (talca_id,) + valores + (_fecha_creacion(fila.get("created_at")),),
    )
    return "creada"


def _upsert_vehiculo(db, fila, cliente_id, dueno_talca_id):
    """Inserta o actualiza el equipo, buscándolo SOLO por talca_id (= idd).
    Devuelve 'creada', 'actualizada' o 'saltada'.

    El mapeo con Talca: patente->identificador, ano->anio, chasis->vin.
    `notas` y `motor_cc` no se tocan nunca: son campos locales (`motor_cc` ni
    siquiera existe en Talca), igual que `notas` en clientes.

    `saltada` es la mitad de pull del push bidireccional: si el equipo tiene
    push_pendiente = 1, lo que hay acá es más nuevo que lo que manda Talca y
    no se pisa. Mismo criterio que _upsert_cliente."""
    talca_id = int(fila["idd"])
    valores = (
        cliente_id,
        dueno_talca_id,
        _texto_importado(fila.get("patente")),
        _texto_importado(fila.get("marca")),
        _texto_importado(fila.get("modelo")),
        _texto_importado(fila.get("color")),
        _texto_importado(fila.get("modelo_transmision")),
        _texto_importado(fila.get("chasis")),
        _entero_significativo(fila.get("ano")),
        _puertas(fila.get("puertas")),
        _entero_significativo(fila.get("kilometraje")),
        # El updated_at de Talca, texto opaco: base del locking optimista del
        # push (viaja como talca_updated_at_conocido en el PUT).
        _texto_importado(fila.get("updated_at")),
    )

    existente = db.execute(
        "SELECT id, push_pendiente FROM equipos WHERE talca_id = ?", (talca_id,)
    ).fetchone()
    if existente is not None:
        if existente["push_pendiente"]:
            return "saltada"
        db.execute(
            """
            UPDATE equipos
               SET cliente_id = ?, talca_dueno_id = ?, identificador = ?, marca = ?,
                   modelo = ?, color = ?, modelo_transmision = ?, vin = ?,
                   anio = ?, puertas = ?, kilometraje = ?, talca_updated_at = ?
             WHERE talca_id = ?
            """,
            valores + (talca_id,),
        )
        return "actualizada"

    db.execute(
        """
        INSERT INTO equipos (talca_id, cliente_id, talca_dueno_id, identificador, marca,
                             modelo, color, modelo_transmision, vin, anio, puertas,
                             kilometraje, talca_updated_at, notas, creado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
        """,
        (talca_id,) + valores + (_fecha_creacion(fila.get("created_at")),),
    )
    return "creada"


# ---------------------------------------------------------------------------
# Órdenes de trabajo
# ---------------------------------------------------------------------------
# En Talca son `orden_trabajo` (7.116 filas) y acá `ordenes_trabajo`. Las tres
# cosas que esa tabla mezcla -- órdenes, garantías y OT de conteo -- van todas
# a la misma tabla de acá, distinguidas por `tipo_talca`. Ver _EntidadOrdenes.

# Estado de Talca -> estado del sistema nuevo. `estados_ot` tiene 2 filas pero
# en los datos hay 4 valores: `editNota` escribe los otros dos a mano. Por eso
# el mapeo NECESITA una rama por defecto -- esa tabla no es la fuente de
# verdad, y un valor nuevo no puede dejar la orden sin estado.
ESTADOS_TALCA = {
    "CERRADA": "entregado",
    "CERRADA BLOQUEADA": "entregado",
    "PRESUPUESTO NO ACEPTADO": "cancelado",
    "ABIERTA": "recibido",
}

# Los estados de Talca que son un HECHO TERMINAL sobre la orden: cerrada,
# cerrada y bloqueada, presupuesto rechazado. Mandan siempre, aunque acá la
# orden esté en otra etapa.
#
# Los que NO están en esta lista (ABIERTA, y cualquier valor futuro) no
# degradan el estado local: ver _estado_python.
ESTADOS_TALCA_TERMINALES = {"CERRADA", "CERRADA BLOQUEADA", "PRESUPUESTO NO ACEPTADO"}

# El estado con el que entra una orden abierta que acá todavía no existe.
ESTADO_INICIAL = "recibido"


def _estado_python(fila, estado_local=None):
    """El estado que le corresponde a esta orden acá, dado lo que dice Talca y
    lo que ya había en esta base.

    Tres reglas, en orden:

    1. **Los hechos terminales de Talca mandan siempre.** Una orden borrada
       (isDeleted, 84 filas) o marcada TNR (49) va a 'cancelado'; CERRADA y
       CERRADA BLOQUEADA van a 'entregado'. El matiz crudo no se pierde:
       queda en `estado_talca` y en `trabajo_no_realizado`.

       TNR va a 'cancelado' y no a 'entregado' a propósito: "cerrada sin
       trabajo" no es una entrega, y mandarlas a entregado las haría contar
       como trabajos realizados en las métricas.

    2. **ABIERTA no degrada el avance local.** Talca no modela el flujo
       interno del taller: no tiene forma de decir 'reparacion' ni 'listo'.
       Así que su ABIERTA no es evidencia en contra del estado que el taller
       puso acá. Sin esta regla, editar en Talca cualquier cosa de una OT
       abierta -- una nota, el kilometraje -- movería su updatedDtm, la
       traería por el pull y arrastraría la tarjeta del tablero de vuelta a
       'recibido', sin aviso y sin error.

       Todo lo demás de esa orden (precio, requerimiento, observaciones,
       kilometraje...) sí se actualiza con lo que manda Talca. Lo único que se
       conserva es la posición en el flujo.

    3. **Cualquier valor futuro se trata como ABIERTA**, por la misma razón:
       no se sabe qué significa, así que no puede ser motivo para retroceder.

    En la carga inicial `estado_local` es None y la regla 2 no cambia nada:
    las 151 abiertas entran todas como 'recibido'. Es la limitación anotada en
    el plan -- no hay dato en Talca para saber en qué etapa están."""
    if _bandera(fila.get("isDeleted")):
        return "cancelado"
    if _texto(fila.get("tnr")):
        return "cancelado"

    estado_talca = _texto_importado(fila.get("estado")).upper()
    if estado_talca in ESTADOS_TALCA_TERMINALES:
        return ESTADOS_TALCA[estado_talca]

    # ABIERTA o desconocido: solo se escribe si acá no hay avance que perder.
    if estado_local and estado_local != ESTADO_INICIAL:
        return estado_local
    return ESTADO_INICIAL


def _presupuesto_estado(fila, estado_local=None):
    """`apro = 'SI'` (354 filas) -> 'aprobado'. Nunca al revés.

    Misma lógica que _estado_python: `apro` es una bandera de un solo sentido
    en Talca, y el presupuesto de este sistema tiene un flujo propio
    (pendiente / aprobado / rechazado) que Talca no sabe expresar. Degradar un
    'aprobado' local cada vez que llega una orden con `apro` vacío borraría
    trabajo hecho acá. El valor crudo queda en `datos_talca_json`."""
    if _texto_importado(fila.get("apro")).upper() == "SI":
        return "aprobado"
    return estado_local or "pendiente"


# Los tres formatos de patente chilena que aparecen en los datos: 2 letras + 4
# dígitos (autos hasta 2007), 4 letras + 2 dígitos (autos actuales) y 3 letras
# + 2 dígitos (motos).
_RE_PATENTE_CHILENA = re.compile(r"^(?:[A-Z]{2}\d{4}|[A-Z]{4}\d{2}|[A-Z]{3}\d{2})$")


def _es_patente_chilena(texto):
    """Si el texto es, TAL CUAL, una patente chilena.

    Deliberadamente no sanea nada antes de comparar. La primera versión de
    esto sacaba los caracteres no alfanuméricos, y con eso `sept.21` -- la
    etiqueta de "cierre de septiembre 2021", una de las 418 órdenes
    administrativas -- se convertía en `SEPT21`, que es exactamente el formato
    de 4 letras + 2 dígitos de una patente actual. O sea que el saneo
    FABRICABA patentes a partir de fechas.

    En los datos las placas están guardadas planas (`zn1510`, `fgbb83`), así
    que exigir el formato exacto no pierde nada. Una placa que alguien haya
    escrito con separador (`ZN-1510`) simplemente cae a la siguiente rama de
    resolver_patente, que es el equipo: se resuelve igual, por el camino
    correcto."""
    return bool(_RE_PATENTE_CHILENA.match(_texto(texto).upper()))


def resolver_patente(fila, patente_equipo=""):
    """La patente que le corresponde a esta orden, y por qué rama salió.

    Devuelve `(patente, rama)`. La rama se cuenta en el resumen de la corrida
    para que el reparto sea auditable y no haya que confiar en que esto
    funcionó.

    Hace falta porque `orden_trabajo` tiene dos épocas, con corte el
    2024-09-25 entre las OT 5709 y 5710:

    - id 1000-5709 (4.710 filas): `patente` VACÍA, la placa está en la columna
      `vehiculo`, en minúsculas en 4.337 de ellas.
    - id 5710-8115 (2.406 filas): `patente` poblada.

    Pero `vehiculo` no siempre es una placa: en 418 de esas filas dice `venta`,
    `gastos gen`, `sept.21`, `admon marz`, un `.` suelto o un chasis completo.
    No son datos corruptos -- son órdenes administrativas (cierres de mes,
    ventas de mostrador, imputación de gastos) que usan ese campo como
    etiqueta del período. Se importan igual, pero copiar `sept.21` a un campo
    llamado "patente" sería inventar un dato. Por eso el fallback exige forma
    de patente chilena, y si no la tiene se cae al equipo.

    Nada se pierde en ninguna rama: `datos_talca_json` guarda la fila entera
    con su `vehiculo` crudo."""
    directa = _texto_importado(fila.get("patente"))
    if directa:
        return directa, "columna_patente"

    vehiculo = _texto_importado(fila.get("vehiculo"))
    if vehiculo and _es_patente_chilena(vehiculo):
        # Única normalización de esta función: mayúsculas. 4.337 de las OT
        # viejas la guardaron en minúsculas, y dejarla así la haría verse
        # distinta de la misma placa en equipos.identificador.
        return vehiculo.upper(), "columna_vehiculo"

    if patente_equipo:
        return patente_equipo, "equipo"

    return "", "sin_patente"


# "GARANTIA OT. 1160" dentro del texto libre de `observaciones`. Es la única
# forma en que existe el vínculo de una garantía con su orden de origen: no
# hay ninguna columna, y solo 637 de las 1.440 garantías lo traen.
#
# El patrón es deliberadamente estricto (exige la palabra GARANTIA y la sigla
# OT juntas). Con un 56% que no matchea de todos modos, un match equivocado
# -- apuntar a la orden de otro auto -- es peor que no encontrar nada.
# La 'O' de la clase de caracteres no sobra: _normalizar_nombre descompone la
# 'º' de "Nº" en una 'o' antes de que este patrón vea el texto.
_RE_GARANTIA_OT = re.compile(r"GARANTIA\s*(?:DE\s*)?(?:LA\s*)?OT\.?\s*(?:N[°ºO]?\.?\s*)?(\d{1,6})")


def _referencia_garantia(observaciones):
    """El número de OT de origen que menciona el texto, o None."""
    encontrado = _RE_GARANTIA_OT.search(_normalizar_nombre(observaciones))
    return int(encontrado.group(1)) if encontrado else None


# Columnas que el sync escribe en cada corrida. Todo lo que NO está acá es
# local y no se toca nunca: el diagnóstico (sintoma, diagnostico_tecnico,
# falla_detectada, mediciones...), el control de calidad, la prioridad, el
# mecánico, el equipamiento, recepcionado_por, fecha_entrega_estimada y
# `creado_en`. Mismo criterio que `notas` en clientes y `motor_cc` en equipos.
_COLUMNAS_ORDEN_SYNC = (
    "cliente_id", "equipo_id", "estado", "fecha_recepcion", "fecha_entrega_real",
    "actualizado_en", "kilometraje", "estanque_nivel", "referencia",
    "trabajo_a_realizar", "precio_referencial", "precio_iva",
    "presupuesto_estado", "presupuesto_notas", "legacy_ot_no",
    "talca_id", "talca_dueno_id", "talca_vehiculo_id", "patente",
    "cliente_nombre_talca", "estado_talca", "tipo_talca", "trabajo_no_realizado",
    "borrada_en_talca", "fecha_cierre_talca", "observaciones_talca",
    "garantia_ot_origen_talca", "datos_talca_json",
    # updatedDtm tal como vino, texto opaco. Base del locking optimista del
    # push. Está en hora de Chile, no en UTC -- ver la nota de la migración.
    "talca_updated_at",
)


def _valores_orden(fila, cliente_id, equipo_id, patente, local=None):
    """Los valores de _COLUMNAS_ORDEN_SYNC para esta fila, en ese orden.

    `local` es la fila que ya está en esta base (None en un alta). Solo la
    miran las dos reglas que no degradan avance local: el estado y el estado
    del presupuesto."""
    observaciones = _texto(fila.get("observaciones"))
    estado_local = local["estado"] if local is not None else None
    presupuesto_local = local["presupuesto_estado"] if local is not None else None
    talca_id = int(fila["id"])

    return (
        cliente_id,
        equipo_id,
        _estado_python(fila, estado_local),
        _fecha_creacion(fila.get("createdDtm")),
        _fecha_opcional(fila.get("fecha_salida")),
        _fecha_actualizacion(fila),
        # El único cero-centinela de la tabla: un auto no llega a un taller de
        # transmisiones con 0 km (ver _numero).
        _entero_significativo(fila.get("kilometraje")),
        # Sin validar contra 1-5: el dueño del sistema lo describió así, pero
        # en los datos va de 0 a 10 (322 filas fuera de ese rango).
        _numero(fila.get("estanque")),
        _texto_importado(fila.get("origen")),
        _texto(fila.get("requerimiento")),
        _numero(fila.get("precio")),
        _bandera(fila.get("coniva")),
        _presupuesto_estado(fila, presupuesto_local),
        _texto(fila.get("obs_presupuesto")),
        # legacy_ot_no significa exactamente "el número de OT en Talca", que
        # es lo mismo que talca_id. Se mantiene poblado para que los endpoints
        # que buscan por ahí (pagos, historial) sigan funcionando.
        talca_id,
        talca_id,
        _entero_significativo(fila.get("id_dueno")),
        _entero_significativo(fila.get("id_vehiculo")),
        patente,
        _texto_importado(fila.get("nombre")),
        _texto_importado(fila.get("estado")),
        _texto_importado(fila.get("tipo")),
        1 if _texto(fila.get("tnr")) else 0,
        _bandera(fila.get("isDeleted")),
        _fecha_opcional(fila.get("fecha_cierre")),
        observaciones,
        _referencia_garantia(observaciones),
        # Se regenera COMPLETO, nunca se fusiona con el anterior: si se
        # fusionara, un campo borrado en Talca sobreviviría acá para siempre.
        json.dumps(fila, ensure_ascii=False, default=str),
        _texto_importado(fila.get("updatedDtm")),
    )


def _upsert_orden(db, fila, cliente_id, equipo_id, patente, resultado):
    """Inserta, actualiza o ADOPTA la orden. Devuelve 'creada' o 'actualizada'.

    El match es por `talca_id` y nada más. Si no hay, se intenta adoptar una
    fila preexistente por `legacy_ot_no` (ver _adoptar_orden) antes de dar por
    hecho que es nueva."""
    talca_id = int(fila["id"])

    local = db.execute(
        "SELECT id, estado, presupuesto_estado, cliente_id, push_pendiente"
        "  FROM ordenes_trabajo WHERE talca_id = ?",
        (talca_id,),
    ).fetchone()

    if local is not None and local["push_pendiente"]:
        # Hay un cambio local sin confirmar en Talca: lo de acá es más nuevo
        # y no se pisa. Mismo criterio que _upsert_cliente y _upsert_vehiculo.
        return "saltada"

    if local is None:
        local = _adoptar_orden(db, talca_id, cliente_id, resultado)

    valores = _valores_orden(fila, cliente_id, equipo_id, patente, local)

    if local is not None:
        asignaciones = ", ".join(f"{c} = ?" for c in _COLUMNAS_ORDEN_SYNC)
        db.execute(
            f"UPDATE ordenes_trabajo SET {asignaciones} WHERE id = ?",
            valores + (local["id"],),
        )
        # El UPDATE de arriba dispara trg_ordenes_push_pendiente, que deja el
        # flag en 1 -- marcaría para push exactamente lo que se acaba de
        # RECIBIR de Talca. Se apaga en una sentencia aparte, dentro de la
        # misma transacción: como el trigger solo mira las columnas de negocio,
        # este segundo UPDATE no lo vuelve a disparar.
        #
        # Es seguro apagarlo sin condiciones porque llegar hasta acá significa
        # que estaba en 0: si hubiera estado en 1, la guardia de más arriba
        # habría devuelto 'saltada'.
        db.execute(
            "UPDATE ordenes_trabajo SET push_pendiente = 0 WHERE id = ?",
            (local["id"],),
        )
        return "actualizada"

    columnas = ", ".join(_COLUMNAS_ORDEN_SYNC) + ", creado_en"
    marcadores = ", ".join("?" for _ in _COLUMNAS_ORDEN_SYNC) + ", ?"
    db.execute(
        f"INSERT INTO ordenes_trabajo ({columnas}) VALUES ({marcadores})",
        valores + (_fecha_creacion(fila.get("createdDtm")),),
    )
    return "creada"


def _adoptar_orden(db, talca_id, cliente_id, resultado):
    """Reclama la orden que ya existía en esta base con este número de Talca,
    o None si no hay ninguna adoptable.

    El importador histórico (hoy bloqueado con 410) dejó órdenes con
    `legacy_ot_no` poblado y sin `talca_id`. Sin esto, el sync insertaría una
    copia y el taller vería la misma OT dos veces.

    **La salvaguarda no es opcional.** `legacy_ot_no` lo escribió
    `backfill-legacy-no` emparejando por PATENTE, y hay 285 patentes
    repetidas: un número puede estar puesto sobre la orden de otro auto. Por
    eso antes de adoptar se verifica que el cliente de la fila local sea el
    mismo que resolvió el sync desde Talca. Si no coincide, no se adopta: se
    inserta una fila nueva y el conflicto queda registrado para revisión.

    Dos filas locales con el mismo `legacy_ot_no` tampoco se adoptan: la
    columna no tiene índice único, así que puede pasar, y elegir una
    "cualquiera" es exactamente el error que este sync existe para no
    cometer."""
    candidatas = db.execute(
        """
        SELECT id, estado, presupuesto_estado, cliente_id
          FROM ordenes_trabajo
         WHERE legacy_ot_no = ? AND talca_id IS NULL
         ORDER BY id
        """,
        (talca_id,),
    ).fetchall()

    if not candidatas:
        return None

    if len(candidatas) > 1:
        _registrar_adopcion(
            db, "ordenes", talca_id, "conflicto_ambiguo", None, None, cliente_id,
            f"{len(candidatas)} filas locales tienen legacy_ot_no = {talca_id}: "
            f"ids {', '.join(str(c['id']) for c in candidatas)}. Se insertó una orden nueva.",
        )
        resultado.setdefault("adopciones_por_resultado", {})
        resultado["adopciones_por_resultado"]["conflicto_ambiguo"] = (
            resultado["adopciones_por_resultado"].get("conflicto_ambiguo", 0) + 1
        )
        return None

    candidata = candidatas[0]

    if candidata["cliente_id"] != cliente_id:
        _registrar_adopcion(
            db, "ordenes", talca_id, "conflicto_cliente", candidata["id"],
            candidata["cliente_id"], cliente_id,
            "La orden local está colgada de otro cliente que el que resuelve Talca. "
            "legacy_ot_no lo escribió un backfill por patente y hay 285 patentes "
            "repetidas. Se insertó una orden nueva; hay que revisar cuál sobra.",
        )
        resultado.setdefault("adopciones_por_resultado", {})
        resultado["adopciones_por_resultado"]["conflicto_cliente"] = (
            resultado["adopciones_por_resultado"].get("conflicto_cliente", 0) + 1
        )
        return None

    _registrar_adopcion(
        db, "ordenes", talca_id, "adoptada", candidata["id"],
        candidata["cliente_id"], cliente_id,
        "Orden preexistente reclamada por el sync en vez de duplicarla.",
    )
    resultado.setdefault("adopciones_por_resultado", {})
    resultado["adopciones_por_resultado"]["adoptada"] = (
        resultado["adopciones_por_resultado"].get("adoptada", 0) + 1
    )
    return candidata


def _registrar_adopcion(db, entidad, talca_id, resultado_adopcion, fila_local_id,
                        cliente_id_local, cliente_id_resuelto, detalle):
    db.execute(
        """
        INSERT INTO sync_talca_adopciones (
            entidad, talca_id, resultado, fila_local_id, cliente_id_local,
            cliente_id_resuelto, detalle, visto_en
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (entidad, talca_id) DO UPDATE SET
            resultado = excluded.resultado,
            fila_local_id = excluded.fila_local_id,
            cliente_id_local = excluded.cliente_id_local,
            cliente_id_resuelto = excluded.cliente_id_resuelto,
            detalle = excluded.detalle,
            visto_en = excluded.visto_en
        """,
        (entidad, talca_id, resultado_adopcion, fila_local_id, cliente_id_local,
         cliente_id_resuelto, detalle[:500], now_iso()),
    )


def reconciliar_ordenes_pendientes(db):
    """Reengancha las órdenes huérfanas cuyo cliente o vehículo YA apareció.
    Devuelve {"cliente": n, "equipo": n}.

    Es el mismo mecanismo que reconciliar_duenos_pendientes y existe por el
    mismo motivo: el que cambia cuando aparece el dueño o el auto es el
    CLIENTE o el VEHÍCULO, y la orden no se vuelve a recibir por el pull
    mientras su propio updatedDtm no se mueva. Sin este pase, las 88 órdenes
    sin cliente resoluble quedarían colgadas del genérico para siempre y las
    15 sin equipo quedarían sin auto, aunque alguien los recreara en Talca al
    día siguiente.

    Por eso corre al final de las tres sincronizaciones, no solo la de
    órdenes: son dos UPDATE con índice, no cuesta nada."""
    reenganchadas = {"cliente": 0, "equipo": 0}

    generico = db.execute(
        "SELECT id FROM clientes WHERE marcador_sync = ?", (MARCADOR_SIN_DUENO,)
    ).fetchone()

    if generico is not None:
        pendientes = db.execute(
            """
            SELECT o.id AS orden_id, o.talca_id AS orden_talca_id, c.id AS cliente_id
              FROM ordenes_trabajo o
              JOIN clientes c ON c.talca_id = o.talca_dueno_id
             WHERE o.cliente_id = ? AND o.talca_dueno_id IS NOT NULL AND o.talca_id IS NOT NULL
            """,
            (generico["id"],),
        ).fetchall()
        for fila in pendientes:
            db.execute(
                "UPDATE ordenes_trabajo SET cliente_id = ? WHERE id = ?",
                (fila["cliente_id"], fila["orden_id"]),
            )
            db.execute(
                """
                UPDATE sync_talca_reasignados
                   SET resuelto_en = ?
                 WHERE entidad = 'ordenes' AND talca_id = ? AND resuelto_en = ''
                """,
                (now_iso(), fila["orden_talca_id"]),
            )
        reenganchadas["cliente"] = len(pendientes)

    # El equipo va aparte: `ordenes_trabajo.equipo_id` SÍ acepta NULL, así que
    # una orden sin auto resoluble no se cuelga de nada -- no hay equipo
    # genérico. Lo que se busca acá es la que quedó en NULL y cuyo vehículo ya
    # está importado.
    sin_equipo = db.execute(
        """
        SELECT o.id AS orden_id, o.talca_id AS orden_talca_id, e.id AS equipo_id
          FROM ordenes_trabajo o
          JOIN equipos e ON e.talca_id = o.talca_vehiculo_id
         WHERE o.equipo_id IS NULL AND o.talca_vehiculo_id IS NOT NULL AND o.talca_id IS NOT NULL
        """
    ).fetchall()
    for fila in sin_equipo:
        db.execute(
            "UPDATE ordenes_trabajo SET equipo_id = ? WHERE id = ?",
            (fila["equipo_id"], fila["orden_id"]),
        )
        db.execute(
            """
            UPDATE sync_talca_reasignados
               SET resuelto_en = ?
             WHERE entidad = 'ordenes_equipo' AND talca_id = ? AND resuelto_en = ''
            """,
            (now_iso(), fila["orden_talca_id"]),
        )
    reenganchadas["equipo"] = len(sin_equipo)

    return reenganchadas


# ---------------------------------------------------------------------------
# Qué tiene de propio cada entidad
# ---------------------------------------------------------------------------
# El bucle de paginación, la marca de agua, los reintentos y los cinturones de
# seguridad son idénticos para todas las entidades, así que viven una sola vez
# en _sincronizar(). Lo que cambia por entidad se define acá.

class _Entidad:
    nombre = ""
    campo_id = "id"

    def precondiciones(self, db):
        """Mensaje de error si esta entidad todavía no se puede sincronizar,
        o None si se puede."""
        return None

    def motivo_omision(self, fila):
        return None

    def upsert(self, db, fila, resultado):
        raise NotImplementedError

    def al_terminar(self, db, resultado):
        """Corre una sola vez, después de la última página y antes de guardar
        la marca de agua."""


class _EntidadClientes(_Entidad):
    nombre = "clientes"
    campo_id = "id"

    def motivo_omision(self, fila):
        return motivo_omision(fila)

    def upsert(self, db, fila, resultado):
        return _upsert_cliente(db, fila)

    def al_terminar(self, db, resultado):
        _limpiar_omitidos_ya_importados(db, "clientes", "clientes")
        # Un cliente que acaba de aparecer puede ser el dueño que le faltaba
        # a un vehículo colgado del genérico -- o a una orden.
        resultado["reenganchados"] = reconciliar_duenos_pendientes(db)
        _sumar_reenganches_ordenes(resultado, reconciliar_ordenes_pendientes(db))


class _EntidadVehiculos(_Entidad):
    nombre = "vehiculos"
    campo_id = "idd"

    def __init__(self):
        self._cliente_generico_id = None

    def precondiciones(self, db):
        """`equipos.cliente_id` es NOT NULL y se resuelve contra
        `clientes.talca_id`: sin clientes sincronizados, TODOS los vehículos
        caerían en el cliente genérico y habría que deshacerlo a mano."""
        if not leer_estado(db, "clientes")["marca_agua"]:
            return ("Los vehículos dependen de los clientes y clientes nunca se sincronizó.\n"
                    "  Corre primero:  python -m modulos.sync_talca clientes")
        cargados = db.execute(
            "SELECT COUNT(*) FROM clientes WHERE talca_id IS NOT NULL"
        ).fetchone()[0]
        if not cargados:
            return ("No hay ningún cliente traído de Talca en esta base, así que ningún\n"
                    "  vehículo podría resolver su dueño.\n"
                    "  Corre primero:  python -m modulos.sync_talca clientes")
        return None

    def motivo_omision(self, fila):
        return motivo_omision_vehiculo(fila)

    def upsert(self, db, fila, resultado):
        talca_id = int(fila["idd"])
        dueno_talca_id = _entero_significativo(fila.get("dueno"))

        cliente_id = None
        if dueno_talca_id is not None:
            encontrado = db.execute(
                "SELECT id FROM clientes WHERE talca_id = ?", (dueno_talca_id,)
            ).fetchone()
            if encontrado is not None:
                cliente_id = encontrado["id"]

        if cliente_id is None:
            # Dos motivos distintos a propósito. 'dueno_inexistente' son autos
            # reales cuyo cliente se borró de Talca, y pueden sanar solos si
            # alguien lo recrea. 'dueno_omitido' HOY NO DEBE APARECER NUNCA:
            # existía cuando el filtro descartaba los clientes de gastos, y
            # ahora esos clientes se importan. Si aparece uno, algo cambió en
            # las reglas de filtrado y hay que mirarlo.
            motivo = "dueno_omitido" if self._fue_omitido(db, dueno_talca_id) else "dueno_inexistente"
            cliente_id = self._generico(db)
            _registrar_reasignado(db, "vehiculos", talca_id, motivo,
                                  dueno_talca_id, cliente_id, fila)
            claves = resultado.setdefault("reasignados_por_motivo", {})
            claves[motivo] = claves.get(motivo, 0) + 1

        return _upsert_vehiculo(db, fila, cliente_id, dueno_talca_id)

    def al_terminar(self, db, resultado):
        resultado["reenganchados"] = reconciliar_duenos_pendientes(db)
        # Un vehículo que acaba de aparecer puede ser el auto que le faltaba a
        # una orden importada antes.
        _sumar_reenganches_ordenes(resultado, reconciliar_ordenes_pendientes(db))

    def _generico(self, db):
        if self._cliente_generico_id is None:
            self._cliente_generico_id = _obtener_cliente_generico(db)
        return self._cliente_generico_id

    def _fue_omitido(self, db, dueno_talca_id):
        if dueno_talca_id is None:
            return False
        return db.execute(
            "SELECT 1 FROM sync_talca_omitidos WHERE entidad = 'clientes' AND talca_id = ?",
            (dueno_talca_id,),
        ).fetchone() is not None


def _sumar_reenganches_ordenes(resultado, reenganchadas):
    resultado["reenganchadas_ordenes_cliente"] += reenganchadas["cliente"]
    resultado["reenganchadas_ordenes_equipo"] += reenganchadas["equipo"]


class _EntidadOrdenes(_Entidad):
    """Las 7.116 filas de `orden_trabajo`, que en realidad son tres cosas:
    5.589 órdenes, 1.440 garantías y 26 OT de conteo de inventario.

    **Las tres van a `ordenes_trabajo`**, distinguidas por `tipo_talca`. Las
    garantías NO van al módulo `garantias` de este sistema, aunque exista:
    solo 637 (44%) tienen una referencia recuperable a su OT de origen, y está
    en texto libre dentro de `observaciones`. Llevarlas a un modelo donde
    `orden_origen_id` es el vínculo central para dejarlo vacío en el 56% de
    las filas es construir deuda. Además, en Talca las garantías aparecen en
    el mismo listado que las órdenes -- son filas de la misma tabla -- así que
    esto replica lo que el taller ve hoy, que es el objetivo del mes de
    convivencia.

    Es reversible: separarlas después es un UPDATE con criterio conocido.
    Fusionarlas después de haber creado garantías nativas con su propio
    correlativo sería mucho más enredado."""

    nombre = "ordenes"
    campo_id = "id"

    def __init__(self):
        self._cliente_generico_id = None

    def precondiciones(self, db):
        """Una orden se cuelga de un cliente (`cliente_id` NOT NULL) y de un
        equipo, y ambos se resuelven contra `talca_id`. Sin ellos cargados,
        las 7.116 órdenes caerían enteras en el cliente genérico y sin auto:
        habría que deshacerlo a mano."""
        for entidad, tabla, comando in (
            ("clientes", "clientes", "clientes"),
            ("vehiculos", "equipos", "vehiculos"),
        ):
            if not leer_estado(db, entidad)["marca_agua"]:
                return (f"Las órdenes dependen de {entidad} y {entidad} nunca se sincronizó.\n"
                        f"  Corre primero:  python -m modulos.sync_talca {comando}")
            cargados = db.execute(
                f"SELECT COUNT(*) FROM {tabla} WHERE talca_id IS NOT NULL"
            ).fetchone()[0]
            if not cargados:
                return (f"No hay ninguna fila de {entidad} traída de Talca en esta base, así que\n"
                        f"  ninguna orden podría resolver su referencia.\n"
                        f"  Corre primero:  python -m modulos.sync_talca {comando}")
        return None

    def motivo_omision(self, fila):
        """No se omite ninguna orden, a propósito -- ni siquiera las que
        parecen basura:

        - Las **84 con `isDeleted = 1`** se importan con `borrada_en_talca` y
          estado 'cancelado'. Si se filtraran, una orden borrada DESPUÉS de
          haber sido importada quedaría viva acá para siempre: el borrado
          lógico llega como un update normal y el filtro simplemente la
          saltaría. Importarlas es lo que hace que el borrado se propague.
        - Las **418 administrativas** (cierres de mes, ventas de mostrador,
          imputación de gastos) son registros reales del taller.
        - Las **30 con `glosa = 'CXMANO'`** son ventas no instaladas en un
          vehículo. Son transacciones comerciales, no basura.

        El único descarte posible sigue siendo la fila sin `id`, y de eso se
        encarga _procesar_fila."""
        return None

    def upsert(self, db, fila, resultado):
        talca_id = int(fila["id"])
        cliente_id = self._resolver_cliente(db, fila, talca_id, resultado)
        equipo_id, patente_equipo = self._resolver_equipo(db, fila, talca_id, resultado)

        patente, rama = resolver_patente(fila, patente_equipo)
        ramas = resultado.setdefault("patentes_por_rama", {})
        ramas[rama] = ramas.get(rama, 0) + 1

        return _upsert_orden(db, fila, cliente_id, equipo_id, patente, resultado)

    def al_terminar(self, db, resultado):
        _sumar_reenganches_ordenes(resultado, reconciliar_ordenes_pendientes(db))

    def _resolver_cliente(self, db, fila, talca_id, resultado):
        """El cliente local de esta orden. 88 de las 7.116 no lo tienen: 3 con
        `id_dueno = 0` y 85 apuntando a un cliente que ya no existe (46 dueños
        distintos). Van al mismo cliente genérico que los vehículos huérfanos.

        El nombre real no se pierde: `cliente_nombre_talca` guarda el
        `nombre` de la orden, que es justamente el único rastro de quién era."""
        dueno_talca_id = _entero_significativo(fila.get("id_dueno"))

        if dueno_talca_id is not None:
            encontrado = db.execute(
                "SELECT id FROM clientes WHERE talca_id = ?", (dueno_talca_id,)
            ).fetchone()
            if encontrado is not None:
                return encontrado["id"]

        if dueno_talca_id is None:
            motivo = "dueno_ausente"          # id_dueno = 0: 3 filas
        elif self._fue_omitido(db, dueno_talca_id):
            # Misma guardia que en vehículos: HOY tiene que dar cero, porque
            # ya no se filtra ningún cliente. Si aparece uno, cambiaron las
            # reglas de filtrado y hay que mirarlo.
            motivo = "dueno_omitido"
        else:
            motivo = "dueno_inexistente"      # 85 filas

        cliente_id = self._generico(db)
        _registrar_reasignado(db, "ordenes", talca_id, motivo, dueno_talca_id, cliente_id, fila)
        self._contar(resultado, motivo)
        return cliente_id

    def _resolver_equipo(self, db, fila, talca_id, resultado):
        """(equipo_id, patente del equipo). 15 órdenes no resuelven: 5 con
        `id_vehiculo = 0` y 10 apuntando a un vehículo que ya no existe.

        A diferencia del cliente, acá NO se cuelga de nada:
        `ordenes_trabajo.equipo_id` acepta NULL, así que la orden entra sin
        auto en vez de inventarle uno. Queda registrada, y si el vehículo
        aparece después, reconciliar_ordenes_pendientes la engancha.

        El registro va bajo la entidad 'ordenes_equipo' y no 'ordenes' porque
        `sync_talca_reasignados` tiene UNIQUE (entidad, talca_id): una orden
        que es huérfana de cliente Y de vehículo a la vez no cabría en una
        sola fila, y una de las dos causas se perdería."""
        vehiculo_talca_id = _entero_significativo(fila.get("id_vehiculo"))

        if vehiculo_talca_id is not None:
            encontrado = db.execute(
                "SELECT id, identificador FROM equipos WHERE talca_id = ?", (vehiculo_talca_id,)
            ).fetchone()
            if encontrado is not None:
                return encontrado["id"], _texto(encontrado["identificador"])

        motivo = "vehiculo_ausente" if vehiculo_talca_id is None else "vehiculo_inexistente"
        _registrar_reasignado(db, "ordenes_equipo", talca_id, motivo,
                              vehiculo_talca_id, None, fila)
        self._contar(resultado, motivo)
        return None, ""

    def _contar(self, resultado, motivo):
        claves = resultado.setdefault("reasignados_por_motivo", {})
        claves[motivo] = claves.get(motivo, 0) + 1

    def _generico(self, db):
        if self._cliente_generico_id is None:
            self._cliente_generico_id = _obtener_cliente_generico(db)
        return self._cliente_generico_id

    def _fue_omitido(self, db, dueno_talca_id):
        if dueno_talca_id is None:
            return False
        return db.execute(
            "SELECT 1 FROM sync_talca_omitidos WHERE entidad = 'clientes' AND talca_id = ?",
            (dueno_talca_id,),
        ).fetchone() is not None


ENTIDADES = {
    "clientes": _EntidadClientes,
    "vehiculos": _EntidadVehiculos,
    "ordenes": _EntidadOrdenes,
}


# ---------------------------------------------------------------------------
# El pull
# ---------------------------------------------------------------------------

def sincronizar_clientes(db, cliente_api=None, limite=None, dry_run=False, desde_forzado=None):
    return _sincronizar(db, _EntidadClientes(), cliente_api, limite, dry_run, desde_forzado)


def sincronizar_vehiculos(db, cliente_api=None, limite=None, dry_run=False, desde_forzado=None):
    return _sincronizar(db, _EntidadVehiculos(), cliente_api, limite, dry_run, desde_forzado)


def sincronizar_ordenes(db, cliente_api=None, limite=None, dry_run=False, desde_forzado=None):
    return _sincronizar(db, _EntidadOrdenes(), cliente_api, limite, dry_run, desde_forzado)


def sincronizar_entidad(db, nombre, cliente_api=None, limite=None, dry_run=False, desde_forzado=None):
    """Sincroniza una entidad por su nombre ('clientes', 'vehiculos',
    'ordenes'). Es la puerta de entrada para quien no sabe de antemano cuál
    va a correr: el comando manual y el hilo de fondo de app.py.

    Levanta KeyError si el nombre no existe -- es un error de programación,
    no una condición que valga la pena devolver como resultado."""
    return _sincronizar(db, ENTIDADES[nombre](), cliente_api, limite, dry_run, desde_forzado)


def _sincronizar(db, entidad_sync, cliente_api=None, limite=None, dry_run=False, desde_forzado=None):
    """Trae de Talca todo lo que cambió desde la última corrida exitosa.

    Devuelve un dict con el resumen (no levanta excepciones: una corrida
    fallida es un resultado, no un accidente -- el comando lo imprime y sale
    con código 1, y la marca de agua se queda donde estaba para que la vuelta
    siguiente reintente).

    `desde_forzado` pisa la marca de agua guardada: `""` fuerza una carga
    inicial completa. `dry_run` procesa todo y después hace ROLLBACK, para
    poder mirar qué haría contra los datos reales sin escribir nada.
    """
    entidad = entidad_sync.nombre
    cliente_api = cliente_api or get_cliente_talca()
    limite = int(limite or SYNC_LIMITE_DEFECTO)

    estado = leer_estado(db, entidad)
    marca_previa = estado["marca_agua"] if desde_forzado is None else desde_forzado

    resultado = {
        "entidad": entidad,
        "resultado": "ok",
        "detalle": "",
        "dry_run": bool(dry_run),
        "marca_agua_previa": marca_previa,
        "marca_agua": marca_previa,
        "paginas": 0,
        "recibidas": 0,
        "creadas": 0,
        "actualizadas": 0,
        "saltadas": 0,   # push_pendiente=1: dato local más reciente, no se pisa
        "omitidas": 0,
        "omitidas_por_motivo": {},
        "reasignados_por_motivo": {},
        "reenganchados": 0,
        "reenganchadas_ordenes_cliente": 0,
        "reenganchadas_ordenes_equipo": 0,
        "patentes_por_rama": {},
        "adopciones_por_resultado": {},
    }

    # Se comprueba ANTES de la primera petición: si vehículos corriera sin
    # clientes cargados, todos caerían en el cliente genérico y habría que
    # deshacerlo a mano.
    impedimento = entidad_sync.precondiciones(db)
    if impedimento:
        resultado["resultado"] = "error"
        resultado["detalle"] = impedimento
        return resultado

    # Primera llamada de la corrida: se manda `desde` sin `desde_id`, que es
    # como Talca sabe que tiene que aplicar el margen de 2 minutos. De ahí en
    # adelante se pasan textualmente el siguiente_desde/siguiente_desde_id que
    # vino en la página anterior.
    desde = marca_previa or None
    desde_id = None
    ids_pagina_anterior = None

    try:
        while True:
            resultado["paginas"] += 1
            if resultado["paginas"] > SYNC_MAX_PAGINAS:
                raise ClienteTalcaError(
                    f"La paginación pasó de {SYNC_MAX_PAGINAS} páginas sin terminar; se corta por las dudas."
                )

            respuesta = cliente_api.obtener_pagina(entidad, desde=desde, desde_id=desde_id, limite=limite)
            if not isinstance(respuesta, dict):
                raise ClienteTalcaError(f"Respuesta con forma inesperada: {type(respuesta).__name__}")

            filas = respuesta.get(entidad) or []
            hay_mas = bool(respuesta.get("hay_mas"))

            # Cinturón de seguridad: si Talca dice que hay más pero manda la
            # misma página otra vez (o ninguna fila), el bucle no avanza. Se
            # corta con un error entendible en vez de girar contra producción.
            ids_pagina = [f.get(entidad_sync.campo_id) for f in filas]
            if hay_mas and not filas:
                raise ClienteTalcaError(f"Talca dice hay_mas=true pero mandó una página sin {entidad}.")
            if hay_mas and ids_pagina_anterior is not None and ids_pagina == ids_pagina_anterior:
                raise ClienteTalcaError(
                    f"La página {resultado['paginas']} trajo exactamente los mismos ids que la anterior; "
                    "la paginación no está avanzando."
                )
            ids_pagina_anterior = ids_pagina

            for fila in filas:
                resultado["recibidas"] += 1
                _procesar_fila(db, entidad_sync, fila, resultado)

            # Commit por página: lo ya importado queda guardado aunque una
            # página posterior falle. La marca de agua, en cambio, se guarda
            # solo al final -- así lo de las páginas siguientes se vuelve a
            # pedir en la corrida que viene (y como el upsert es idempotente,
            # reprocesar lo ya guardado no hace daño).
            if not dry_run:
                db.commit()

            if not hay_mas:
                hasta = _texto(respuesta.get("hasta"))
                if not hasta:
                    raise ClienteTalcaError("La última página no trae 'hasta'; sin eso no hay marca de agua que guardar.")
                resultado["marca_agua"] = hasta
                break

            siguiente_desde = _texto(respuesta.get("siguiente_desde"))
            siguiente_desde_id = respuesta.get("siguiente_desde_id")
            if not siguiente_desde or siguiente_desde_id is None:
                raise ClienteTalcaError(
                    "Talca dice hay_mas=true pero no mandó siguiente_desde / siguiente_desde_id."
                )
            if siguiente_desde == desde and siguiente_desde_id == desde_id:
                raise ClienteTalcaError(
                    f"El cursor de paginación no avanzó (desde={desde!r}, desde_id={desde_id!r})."
                )
            desde, desde_id = siguiente_desde, siguiente_desde_id

    except Exception as exc:
        # Se deshace la página a medio procesar; lo confirmado en páginas
        # anteriores queda. La marca de agua se guarda SIN avanzar.
        db.rollback()
        resultado["resultado"] = "error"
        resultado["detalle"] = f"{type(exc).__name__}: {exc}"
        if not dry_run:
            _guardar_estado(db, entidad, marca_previa, resultado)
            db.commit()
        return resultado

    # El cierre (limpiar omitidos que ya entraron, reenganchar dueños) va
    # DENTRO de la corrida: si falla, cae en el except de arriba y la marca de
    # agua no avanza, igual que cualquier otro problema.
    try:
        entidad_sync.al_terminar(db, resultado)
    except Exception as exc:
        db.rollback()
        resultado["resultado"] = "error"
        resultado["detalle"] = f"{type(exc).__name__}: {exc}"
        if not dry_run:
            _guardar_estado(db, entidad, marca_previa, resultado)
            db.commit()
        return resultado

    if dry_run:
        db.rollback()
        return resultado

    resultado["detalle"] = (
        f"{resultado['creadas']} creadas, {resultado['actualizadas']} actualizadas, "
        f"{resultado['omitidas']} omitidas en {resultado['paginas']} página(s)"
    )
    _guardar_estado(db, entidad, resultado["marca_agua"], resultado)
    db.commit()
    return resultado


def _procesar_fila(db, entidad_sync, fila, resultado):
    entidad = entidad_sync.nombre
    if not isinstance(fila, dict):
        raise ClienteTalcaError(f"Un elemento de '{entidad}' no es un objeto: {type(fila).__name__}")

    try:
        talca_id = int(fila[entidad_sync.campo_id])
    except (KeyError, TypeError, ValueError):
        # Sin id no hay llave de sincronización posible, y ni el RUT ni la
        # patente sirven de reemplazo (ver el encabezado del módulo). Se
        # cuenta como omitida.
        resultado["omitidas"] += 1
        resultado["omitidas_por_motivo"]["sin_id"] = resultado["omitidas_por_motivo"].get("sin_id", 0) + 1
        return

    motivo = entidad_sync.motivo_omision(fila)
    if motivo:
        resultado["omitidas"] += 1
        resultado["omitidas_por_motivo"][motivo] = resultado["omitidas_por_motivo"].get(motivo, 0) + 1
        _registrar_omitido(db, entidad, talca_id, motivo, fila)
        return

    accion = entidad_sync.upsert(db, fila, resultado)
    if accion == "creada":
        resultado["creadas"] += 1
    elif accion == "saltada":
        # push_pendiente=1: el dato local es más reciente, no se pisa.
        resultado["saltadas"] = resultado.get("saltadas", 0) + 1
    else:
        resultado["actualizadas"] += 1


# ---------------------------------------------------------------------------
# Comando manual
# ---------------------------------------------------------------------------

def _tabla_existe(db, nombre):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nombre,)
    ).fetchone() is not None


def _imprimir_resumen(resultado):
    print()
    print("=" * 62)
    print(f"  Sync Talca -> Python   entidad: {resultado['entidad']}"
          + ("   [DRY RUN: no se escribió nada]" if resultado["dry_run"] else ""))
    print("=" * 62)
    print(f"  marca de agua antes : {resultado['marca_agua_previa'] or '(ninguna: carga inicial completa)'}")
    print(f"  marca de agua ahora : {resultado['marca_agua'] or '(sin cambios)'}")
    print(f"  páginas             : {resultado['paginas']}")
    print(f"  filas recibidas     : {resultado['recibidas']}")
    print(f"    creadas           : {resultado['creadas']}")
    print(f"    actualizadas      : {resultado['actualizadas']}")
    if resultado.get("saltadas"):
        print(f"    saltadas (push pendiente): {resultado['saltadas']}")
    print(f"    omitidas          : {resultado['omitidas']}")
    for motivo, cuantas in sorted(resultado["omitidas_por_motivo"].items(), key=lambda kv: -kv[1]):
        print(f"      - {motivo:<18}: {cuantas}")
    ramas = resultado.get("patentes_por_rama") or {}
    if ramas:
        # Las órdenes tienen dos épocas y 418 filas administrativas donde la
        # columna `vehiculo` no es una placa: de qué rama salió cada patente
        # es lo que hace auditable que resolver_patente hizo lo correcto.
        print("  patente resuelta por:")
        for rama, cuantas in sorted(ramas.items(), key=lambda kv: -kv[1]):
            print(f"      - {rama:<18}: {cuantas}")
    reasignados = resultado.get("reasignados_por_motivo") or {}
    if reasignados:
        print(f"  con referencia sin resolver    : {sum(reasignados.values())}")
        for motivo, cuantas in sorted(reasignados.items(), key=lambda kv: -kv[1]):
            print(f"      - {motivo:<18}: {cuantas}")
    adopciones = resultado.get("adopciones_por_resultado") or {}
    if adopciones:
        print("  filas locales preexistentes:")
        for clave, cuantas in sorted(adopciones.items(), key=lambda kv: -kv[1]):
            print(f"      - {clave:<18}: {cuantas}")
    if resultado.get("reenganchados"):
        print(f"  vehículos reenganchados a su dueño : {resultado['reenganchados']}")
    if resultado.get("reenganchadas_ordenes_cliente"):
        print(f"  órdenes reenganchadas a su cliente : {resultado['reenganchadas_ordenes_cliente']}")
    if resultado.get("reenganchadas_ordenes_equipo"):
        print(f"  órdenes reenganchadas a su vehículo: {resultado['reenganchadas_ordenes_equipo']}")
    print("-" * 62)
    if resultado["resultado"] == "ok":
        print("  RESULTADO: ok")
    else:
        print("  RESULTADO: ERROR -- la marca de agua NO avanzó.")
        print(f"  {resultado['detalle']}")
        print("  La próxima corrida vuelve a pedir desde el último punto bueno.")
    if resultado["omitidas"] and not resultado["dry_run"]:
        print("  El detalle de las filas omitidas quedó en la tabla sync_talca_omitidos.")
    if reasignados and not resultado["dry_run"]:
        print("  Las reasignaciones quedaron en la tabla sync_talca_reasignados.")
    if adopciones and not resultado["dry_run"]:
        print("  Las adopciones y sus conflictos quedaron en sync_talca_adopciones.")
    print("=" * 62)
    print()


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m modulos.sync_talca",
        description="Sincroniza datos desde Talca.PHP hacia este sistema. Se ejecuta a mano.",
    )
    parser.add_argument("entidad", nargs="?", default="todo",
                        choices=["todo", "clientes", "vehiculos", "ordenes"],
                        help="Qué sincronizar. Por defecto 'todo': clientes, después vehículos y "
                             "después órdenes. El orden importa: los vehículos necesitan a sus "
                             "dueños ya cargados, y las órdenes necesitan las dos cosas.")
    parser.add_argument("--limite", type=int, default=None,
                        help=f"Filas por página (por defecto {SYNC_LIMITE_DEFECTO}).")
    parser.add_argument("--desde", default=None,
                        help="Pisa la marca de agua guardada, formato 'YYYY-MM-DD HH:MM:SS'. "
                             "Solo se acepta con una entidad puntual, no con 'todo'.")
    parser.add_argument("--recarga-completa", action="store_true",
                        help="Ignora la marca de agua y vuelve a traer TODO desde cero. Los "
                             "upserts son idempotentes, así que lo ya cargado se actualiza en "
                             "vez de duplicarse.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Procesa todo y deshace los cambios al terminar. No escribe nada.")
    args = parser.parse_args(argv)

    if args.desde is not None and args.recarga_completa:
        parser.error("--desde y --recarga-completa piden cosas distintas; usa una sola.")

    if args.desde is not None and args.entidad == "todo":
        # Cada entidad tiene su propia marca de agua y su propia escala de
        # tiempo; una sola fecha para las tres no significa nada, y con
        # órdenes deja de ser un tecnicismo: `clientes` y `vehiculos` usan
        # TIMESTAMP (UTC) y `orden_trabajo` usa DATETIME en hora de Santiago,
        # 4 horas de diferencia medida. --recarga-completa sí vale para las
        # tres, porque no nombra ningún instante.
        parser.error("--desde necesita una entidad puntual: 'clientes', 'vehiculos' u 'ordenes'.")

    # --recarga-completa existe además de `--desde ""` porque en PowerShell la
    # cadena vacía se pierde antes de llegar a Python: `--desde ""` llega como
    # `--desde` a secas y argparse aborta pidiendo un valor. Funcionan
    # `--desde=` y `'--desde='`, pero depender de esa sutileza para la
    # operación más delicada del sync (volver a traerlo todo) es pedir un
    # problema. La bandera dice lo que hace y no depende del shell.
    desde_forzado = "" if args.recarga_completa else args.desde

    a_correr = ["clientes", "vehiculos", "ordenes"] if args.entidad == "todo" else [args.entidad]

    db = _conectar_db()
    resultados = []
    try:
        for tabla in ("clientes", "equipos", "ordenes_trabajo"):
            if not _tabla_existe(db, tabla):
                print(f"La base ({os.environ.get('DB_PATH', 'inventario.db')}) todavía no tiene la tabla '{tabla}'.")
                print("Arranca app.py una vez para que init_db() cree el esquema, y vuelve a correr esto.")
                return 1
        init_sync_talca_db(db)

        for nombre in a_correr:
            resultado = sincronizar_entidad(
                db, nombre, limite=args.limite,
                dry_run=args.dry_run, desde_forzado=desde_forzado,
            )
            resultados.append(resultado)
            if resultado["resultado"] != "ok":
                # Cada entidad cuelga de la anterior: si clientes falló, la
                # corrida de vehículos colgaría del genérico lo que en realidad
                # sí tiene dueño, y las órdenes heredarían las dos cosas.
                break
    finally:
        db.close()

    for resultado in resultados:
        _imprimir_resumen(resultado)
    return 0 if all(r["resultado"] == "ok" for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(_main())
