"""
Corre la suite completa: UN PROCESO POR MÓDULO.

    python tests/correr_todo.py            # todo
    python tests/correr_todo.py sync push  # solo los módulos que contengan eso

**No usar `python -m unittest discover`.** Carga los 17 módulos en el mismo
proceso y ahí la suite no es válida:

  - Cada test_*.py crea su base temporal y fija `os.environ["DB_PATH"]` al
    importarse. Pero `core.py` lee esa variable UNA sola vez, al importarse él
    (`DB_PATH = os.environ.get("DB_PATH", ...)`, core.py:30), y `app.py` corre
    `init_db()` también al importarse. Con discover gana el DB_PATH del primer
    módulo que se cargue: los otros 16 fijan la variable cuando ya no sirve de
    nada y sus tests corren contra una base que no es la suya.
  - Además queda estado compartido que ningún reordenamiento arregla: config
    de Flask, cachés a nivel de módulo, y los monkeypatch que cada módulo deja
    puestos.

El síntoma es engañoso: no falla con "base equivocada" sino con asserts
sueltos por todos lados, y si además hay otra corrida en paralelo aparecen
"database is locked" que mandan a buscar un problema de concurrencia que no
existe.

Un proceso por módulo elimina las dos cosas de raíz: cada uno arranca con su
propio intérprete, importa core y app desde cero y ve solo su base.

**Los módulos corren sin las variables que salen hacia afuera** (correo, SMS,
WhatsApp, facturación, IA, Sentry, la API de Talca, Railway). Varios tests dan
por hecho que "en pruebas no hay transporte de correo"; si la máquina tiene
RESEND_API_KEY definida -- de este u otro sistema --, mandan correo real. Pasó
el 13-10-2026 desde una PC que tenía la llave de otro proyecto. Correr un
test_*.py suelto, a mano, NO pasa por este filtro.
"""

import os
import subprocess
import sys
import time

# La consola de Windows usa cp1252 y la salida de los tests trae acentos y el
# carácter de reemplazo U+FFFD. Sin esto, imprimir el detalle de un módulo que
# falló revienta con UnicodeEncodeError y se pierde justo lo que se necesitaba
# ver.
for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # pragma: no cover
        pass

DIR_TESTS = os.path.dirname(os.path.abspath(__file__))

# Variables que no llegan a ningún módulo de tests. Por prefijo, para que una
# variable nueva del mismo servicio quede cubierta sin tocar esto.
PREFIJOS_FUERA = (
    "RESEND_", "SMTP_", "TWILIO_", "META_WA_", "WHATSAPP_", "SIMPLEAPI_",
    "TALCA_", "RAILWAY_", "RESPALDO_", "ALERTA_STOCK_",
)
NOMBRES_FUERA = {
    "DB_PATH", "SYNC_INTERVALO_SEGUNDOS", "PROVEEDOR_DTE", "SENTRY_DSN",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PRESUPUESTO_COPIA_TALLER",
}


def entorno_de_pruebas():
    """Copia del entorno sin las variables de PREFIJOS_FUERA y NOMBRES_FUERA.
    Devuelve también los nombres quitados (nunca los valores)."""
    quitadas = sorted(
        k for k in os.environ
        if k in NOMBRES_FUERA or k.startswith(PREFIJOS_FUERA)
    )
    entorno = {k: v for k, v in os.environ.items() if k not in quitadas}
    return entorno, quitadas


def modulos(filtros):
    nombres = sorted(
        f[:-3] for f in os.listdir(DIR_TESTS)
        if f.startswith("test_") and f.endswith(".py")
    )
    if filtros:
        nombres = [n for n in nombres if any(f in n for f in filtros)]
    return nombres


def resumen_de(salida):
    """La última línea con forma de resultado de unittest: 'OK',
    'FAILED (errors=5)', etc. Se lee de la salida porque unittest escribe todo
    a stderr y el código de salida solo dice sí/no."""
    lineas = [l.strip() for l in salida.splitlines() if l.strip()]
    for linea in reversed(lineas):
        if linea.startswith(("OK", "FAILED")):
            return linea
    return "(sin resultado legible)"


def cuantos(salida):
    for linea in salida.splitlines():
        if linea.startswith("Ran ") and " test" in linea:
            try:
                return int(linea.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def main():
    nombres = modulos(sys.argv[1:])
    if not nombres:
        print("No hay módulos que calcen con ese filtro.")
        return 1

    entorno, quitadas = entorno_de_pruebas()
    if quitadas:
        print(f"Sin estas variables del entorno: {', '.join(quitadas)}")

    inicio = time.time()
    fallados, total_tests = [], 0

    for nombre in nombres:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", nombre],
            cwd=DIR_TESTS, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=entorno,
        )
        salida = (proc.stdout or "") + (proc.stderr or "")
        total_tests += cuantos(salida)
        estado = resumen_de(salida)
        print(f"{nombre:<40} {cuantos(salida):>4} tests  {estado}")
        if proc.returncode != 0:
            fallados.append((nombre, salida))

    print("-" * 72)
    print(f"{len(nombres)} módulos, {total_tests} tests, "
          f"{time.time() - inicio:.0f}s")

    if not fallados:
        print("OK")
        return 0

    # El detalle va al final y completo: buscarlo re-corriendo el módulo a
    # mano es exactamente la fricción que este script viene a sacar.
    for nombre, salida in fallados:
        print("\n" + "=" * 72)
        print(f"DETALLE: {nombre}")
        print("=" * 72)
        print(salida.rstrip())

    print(f"\nFALLARON {len(fallados)}: {', '.join(n for n, _ in fallados)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
