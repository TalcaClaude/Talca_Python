#!/usr/bin/env python3
"""
Simula un mensaje de WhatsApp entrante, FIRMADO como lo firma el proveedor.

Soporta los dos: Twilio (form-encoded, HMAC-SHA1 sobre la URL + parámetros) y
la Cloud API de Meta (JSON anidado, HMAC-SHA256 sobre el cuerpo crudo).

Existe porque Twilio bloquea el sandbox en cuentas trial: sin esto no hay forma
de ejercitar el webhook de punta a punta contra un despliegue real. Con Meta
sigue siendo útil por lo mismo — probar sin depender de que el teléfono esté
conectado y sin gastar mensajes reales.

Lo que permite comprobar:

  1. que la firma se acepta;
  2. que el agente entiende el mensaje;
  3. que resuelve la OT por `talca_id` y no por el id interno de Python;
  4. que el cambio se escribe y queda encolado hacia Talca.

USO
    python scripts/simular_whatsapp.py "OT 8112 lista"
    python scripts/simular_whatsapp.py "OT 8112 lista" --proveedor meta
    python scripts/simular_whatsapp.py "hola" --de +56946596850 --dry-run
    python scripts/simular_whatsapp.py --proveedor meta --verificar
    python scripts/simular_whatsapp.py --proveedor meta --tipo status

VARIABLES DE ENTORNO
    Según el proveedor, y tienen que ser EXACTAMENTE las mismas que tiene
    configurada la instancia contra la que se apunta, o dará 403.

    --proveedor twilio  TWILIO_AUTH_TOKEN
    --proveedor meta    META_WA_APP_SECRET    (firma el cuerpo crudo)
                        META_WA_VERIFY_TOKEN  (solo para --verificar)

    WEBHOOK_URL         opcional: la raíz del sitio, si no se pasa --url.

NUNCA imprime ninguna credencial. Lo más parecido que muestra es un prefijo de
la firma, que no es secreta y sirve para cotejarla con el log del servidor.

OJO: esto ESCRIBE datos reales. Un "OT 8112 lista" contra producción mueve esa
orden de estado de verdad y dispara el push hacia Talca. Para ver qué se
mandaría sin mandarlo, usar --dry-run.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

try:
    import requests
except ImportError:
    sys.exit("Falta la dependencia 'requests' (pip install -r requirements.txt).")


# User-Agents reales de cada plataforma. Se usan los mismos por si algún día
# hay un filtro por UA en el hosting (Bluehosting ya cortó conexiones por eso,
# ver CLAUDE.md).
USER_AGENT = {
    "twilio": "TwilioProxy/1.1",
    "meta": "facebookexternalua",
}


# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------

def firmar(url, params, token):
    """La firma tal como la calcula Twilio: HMAC-SHA1 del AUTH_TOKEN sobre la
    URL completa seguida de cada par clave+valor del POST en orden alfabético
    de clave, codificado en base64.

    **La URL tiene que ser la pública `https://`**, no la que ve Flask por
    dentro del proxy: es sobre esa que Twilio calcula la firma."""
    base = url + "".join(clave + params[clave] for clave in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")


def construir_params(mensaje, de, para, num_media=0):
    """El cuerpo tal como lo manda Twilio: form-encoded, con estos campos.

    Los SID llevan el prefijo correcto (SM para mensajes, AC para cuentas) y
    32 hexadecimales, que es el formato real."""
    return {
        "Body": mensaje,
        "From": f"whatsapp:{de}",
        "To": f"whatsapp:{para}",
        "MessageSid": "SM" + uuid.uuid4().hex,
        "AccountSid": "AC" + uuid.uuid4().hex,
        "NumMedia": str(num_media),
    }


# ---------------------------------------------------------------------------
# Meta (Cloud API)
# ---------------------------------------------------------------------------

def firmar_meta(cuerpo_bytes, app_secret):
    """HMAC-SHA256 del App Secret sobre el CUERPO CRUDO, con prefijo 'sha256='.

    **Sobre los bytes exactos que se van a mandar**, no sobre el dict: firmar
    una serialización y mandar otra (otro orden de claves, otros espacios) es
    la forma más fácil de que la firma no coincida y el 403 parezca del
    servidor."""
    return "sha256=" + hmac.new(
        app_secret.encode("utf-8"), cuerpo_bytes, hashlib.sha256
    ).hexdigest()


def construir_payload_meta(mensaje, de, phone_number_id, display_phone):
    """El JSON anidado que manda la Cloud API. La ruta del mensaje es
    entry[0].changes[0].value.messages[0], verificada contra la documentación
    de Meta (payload-examples).

    Ojo con `from`: Meta lo manda **SIN el '+'**, y por eso se le quita acá —
    es justamente el caso que hacía que un mecánico registrado como
    '+56946596850' no se reconociera."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "0" * 15,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": display_phone.lstrip("+"),
                        "phone_number_id": phone_number_id,
                    },
                    "contacts": [{
                        "profile": {"name": "Simulador"},
                        "wa_id": de.lstrip("+"),
                    }],
                    "messages": [{
                        "from": de.lstrip("+"),
                        "id": "wamid." + base64.b64encode(uuid.uuid4().bytes).decode(),
                        "timestamp": str(int(time.time())),
                        "type": "text",
                        "text": {"body": mensaje},
                    }],
                },
            }],
        }],
    }


def construir_payload_estado_meta(de, phone_number_id):
    """Un evento de ESTADO de entrega: trae `statuses` en vez de `messages`.

    Sirve para comprobar que el webhook lo ignora con 200 sin romperse, que es
    el caso que más se repite en producción (por cada mensaje que se manda
    llegan varios de estos)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "0" * 15,
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "0", "phone_number_id": phone_number_id},
                    "statuses": [{
                        "id": "wamid." + base64.b64encode(uuid.uuid4().bytes).decode(),
                        "status": "delivered",
                        "timestamp": str(int(time.time())),
                        "recipient_id": de.lstrip("+"),
                    }],
                },
            }],
        }],
    }


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Simula un mensaje de WhatsApp firmado, para Twilio o Meta.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Ejemplo:\n  python scripts/simular_whatsapp.py "OT 8112 lista" --proveedor meta',
    )
    p.add_argument("mensaje", nargs="?", default="",
                   help="El texto del mensaje, tal como lo escribiría el mecánico.")
    p.add_argument("--proveedor", choices=("twilio", "meta"), default="twilio",
                   help="Formato y firma a simular. Tiene que coincidir con "
                        "WHATSAPP_PROVEEDOR de la instancia. Defecto: twilio.")
    p.add_argument("--url", default=os.environ.get("WEBHOOK_URL", "http://localhost:5000"),
                   help="Raíz del sitio (se le agrega /webhook/whatsapp). "
                        "Por defecto WEBHOOK_URL, o http://localhost:5000.")
    p.add_argument("--de", default="+56946596850",
                   help="Teléfono del mecánico que manda, con código de país. "
                        "Tiene que estar registrado y activo, o el agente no lo reconoce.")
    p.add_argument("--para", default="+14155238886",
                   help="Número de WhatsApp del taller.")
    p.add_argument("--phone-number-id", default="000000000000000",
                   help="Solo Meta: el phone_number_id que va en el payload. "
                        "El webhook no lo valida; se manda para que el payload sea realista.")
    p.add_argument("--tipo", choices=("texto", "status"), default="texto",
                   help="Solo Meta: 'status' manda un evento de entrega en vez "
                        "de un mensaje, para comprobar que se ignora con 200.")
    p.add_argument("--verificar", action="store_true",
                   help="Solo Meta: hace el GET de verificación del webhook "
                        "(hub.mode/hub.challenge) en vez de mandar un mensaje.")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra lo que se mandaría y NO manda nada. Útil contra "
                        "producción, donde un mensaje real cambia datos.")
    p.add_argument("--sin-firma", action="store_true",
                   help="Manda sin firma, para comprobar que el webhook rechaza con 403.")
    args = p.parse_args()

    url = args.url.rstrip("/")
    url_webhook = url if url.endswith("/webhook/whatsapp") else url + "/webhook/whatsapp"

    if args.verificar and args.proveedor != "meta":
        sys.exit("--verificar solo aplica con --proveedor meta (Twilio no verifica el webhook).")
    if not args.verificar and not args.mensaje and args.tipo == "texto":
        sys.exit('Falta el mensaje. Ej: python scripts/simular_whatsapp.py "OT 8112 lista"')

    # -- GET de verificación (solo Meta) ------------------------------------
    if args.verificar:
        verify = os.environ.get("META_WA_VERIFY_TOKEN", "").strip()
        if not verify:
            sys.exit("Falta META_WA_VERIFY_TOKEN (la misma que configuraste en el App Dashboard).")
        challenge = str(uuid.uuid4().int % 10**9)
        print("=" * 70)
        print("VERIFICACIÓN DEL WEBHOOK (Meta)")
        print("=" * 70)
        print(f"  URL       : {url_webhook}")
        print(f"  challenge : {challenge}")
        print(f"  token     : definido ({len(verify)} chars, no se imprime)")
        print()
        if args.dry_run:
            print("--dry-run: no se mandó nada.")
            return 0
        try:
            resp = requests.get(url_webhook, params={
                "hub.mode": "subscribe",
                "hub.verify_token": verify,
                "hub.challenge": challenge,
            }, timeout=args.timeout)
        except requests.RequestException as exc:
            print(f"NO HUBO RESPUESTA: {type(exc).__name__}: {exc}")
            return 2
        print(f"HTTP {resp.status_code}")
        print(f"cuerpo: {resp.text.strip()!r}")
        if resp.status_code == 200 and resp.text.strip() == challenge:
            print("\nOK: devolvió el challenge tal cual. El webhook queda verificable.")
            return 0
        print("\nFALLÓ. Revisá que META_WA_VERIFY_TOKEN sea idéntica acá y en la")
        print("instancia, y que WHATSAPP_PROVEEDOR=meta allá (con twilio da 405).")
        return 1

    # -- POST de un mensaje --------------------------------------------------
    if args.proveedor == "twilio":
        secreto = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        nombre_var = "TWILIO_AUTH_TOKEN"
    else:
        secreto = os.environ.get("META_WA_APP_SECRET", "").strip()
        nombre_var = "META_WA_APP_SECRET"

    if not secreto and not args.sin_firma:
        sys.exit(
            f"Falta {nombre_var}.\n"
            f'  PowerShell: $env:{nombre_var} = "<el valor>"\n'
            f"  bash:       export {nombre_var}='<el valor>'\n"
            "Tiene que ser el MISMO que tiene configurada la instancia de destino."
        )

    cabeceras = {"User-Agent": USER_AGENT[args.proveedor]}
    firma = None

    if args.proveedor == "twilio":
        params = construir_params(args.mensaje, args.de, args.para)
        cuerpo_kwargs = {"data": params}
        resumen = {"De": params["From"], "Mensaje": params["Body"],
                   "MessageSid": params["MessageSid"]}
        if not args.sin_firma:
            firma = firmar(url_webhook, params, secreto)
            cabeceras["X-Twilio-Signature"] = firma
    else:
        if args.tipo == "status":
            payload = construir_payload_estado_meta(args.de, args.phone_number_id)
            resumen = {"Tipo": "evento de estado (statuses) — debe ignorarse con 200",
                       "De": args.de.lstrip("+")}
        else:
            payload = construir_payload_meta(
                args.mensaje, args.de, args.phone_number_id, args.para)
            resumen = {"De (sin '+', como Meta)": args.de.lstrip("+"),
                       "Mensaje": args.mensaje}
        # Se serializa UNA vez y se manda exactamente eso: la firma cubre
        # estos bytes.
        cuerpo_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cabeceras["Content-Type"] = "application/json"
        cuerpo_kwargs = {"data": cuerpo_bytes}
        if not args.sin_firma:
            firma = firmar_meta(cuerpo_bytes, secreto)
            cabeceras["X-Hub-Signature-256"] = firma

    print("=" * 70)
    print(f"SIMULACIÓN DE MENSAJE DE WHATSAPP — proveedor: {args.proveedor.upper()}")
    print("=" * 70)
    print(f"  URL          : {url_webhook}")
    for clave, valor in resumen.items():
        print(f"  {clave:<12} : {valor!r}" if clave == "Mensaje" else f"  {clave:<12} : {valor}")
    print(f"  Firma        : {firma[:24] + '…' if firma else '(sin firma, a propósito)'}")
    print(f"  Secreto      : {nombre_var} "
          f"{'definido (' + str(len(secreto)) + ' chars, no se imprime)' if secreto else '(no definido)'}")
    print()

    if args.dry_run:
        print("--dry-run: no se mandó nada.")
        print("Quitá --dry-run para mandarlo de verdad.")
        return 0

    if not url_webhook.startswith("http://localhost") and not url_webhook.startswith("http://127."):
        print("OJO: esto va contra una instancia REAL y puede cambiar datos.")
        print()

    try:
        resp = requests.post(url_webhook, headers=cabeceras, timeout=args.timeout,
                             **cuerpo_kwargs)
    except requests.RequestException as exc:
        print(f"NO HUBO RESPUESTA: {type(exc).__name__}: {exc}")
        print()
        print("Si dice 'connection aborted' o similar sin status HTTP, no llegó a")
        print("la app: revisá la URL, o si hay un WAF cortando la conexión.")
        return 2

    print(f"HTTP {resp.status_code}")
    print("-" * 70)
    print(resp.text.strip() or "(cuerpo vacío)")
    print("-" * 70)

    if resp.status_code == 403:
        print()
        print("FIRMA RECHAZADA. Qué mirar, en orden:")
        print(f"  1. Que {nombre_var} acá sea el mismo que el de la instancia.")
        if args.proveedor == "twilio":
            print("  2. Que --url sea la URL PÚBLICA exacta (https://, sin barra final,")
            print("     mismo host que tiene Twilio configurado): la firma cubre la URL.")
        else:
            print("  2. Que la instancia tenga WHATSAPP_PROVEEDOR=meta. Con 'twilio'")
            print("     esperaría una firma de Twilio y este payload no la trae.")
        print("  3. El log del servidor: dice con qué se calculó el HMAC.")
    elif resp.status_code == 200:
        print()
        if args.proveedor == "meta":
            print("Aceptado. **El cuerpo vacío es lo correcto con Meta**: la respuesta")
            print("al mecánico se manda aparte, por la Graph API. Si no le llega, mirá")
            print("el log del servidor — el envío es best-effort y ahí queda el motivo.")
            if args.tipo == "status":
                print("Y como era un evento de estado, no debía procesarse nada.")
        else:
            print("Aceptado. La respuesta de arriba es el TwiML que le llegaría al")
            print("mecánico por WhatsApp.")
        print()
        print("Qué comprobar ahora:")
        print("  - Que el número que usaste esté registrado y activo.")
        print("  - En /operativo, que la OT haya cambiado como pedías.")
        print("  - En /api/admin/sync/estado, que el push a Talca quedó encolado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
