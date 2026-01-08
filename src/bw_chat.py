"""
BunkerWeb Conversational Chat (DeepSeek + Tool Calling + Local API)

Este script permite hacer preguntas en lenguaje natural (español) sobre los bloqueos/eventos
de BunkerWeb almacenados en Redis (y expuestos vía la API local bw_api.py).

¿Cómo funciona?
1) El usuario escribe una pregunta (ej. "Hoy 5pm-6pm, bloqueos en www.never8.com")
2) Se envía esa pregunta al modelo de DeepSeek (deepseek-chat)
3) El modelo decide si debe llamar una "herramienta" (tool) llamada `search_reports`
   y genera los parámetros (start/end/server_name/security_mode/etc.)
4) Este script ejecuta la herramienta de verdad: llama a tu API local:
   GET http://127.0.0.1:8811/reports/search
5) Se regresa al modelo un resumen compacto de resultados (count, top, samples)
6) El modelo produce una respuesta final "humana" y el script la imprime

Ventajas:
- No necesitas aprender parámetros; preguntas en español natural.
- DeepSeek hace el parseo temporal y el mapeo de filtros a parámetros.
- La búsqueda real la hace tu API (rápida) sobre el índice en Redis.

Requisitos:
- /opt/bw-reports-agent/.env con:
  DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, API_HOST, API_PORT, TZ
- API local levantada: bw-reports-api.service (127.0.0.1:8811)
"""

import os, json, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from openai import OpenAI

# Cargar variables del archivo .env
load_dotenv()

# Zona horaria para contextualizar el "hoy/ayer/anoche"
TZ = ZoneInfo(os.getenv("TZ", "America/Monterrey"))

# Dirección local de la API que consulta Redis (bw_api.py)
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8811"))
API_BASE = f"http://{API_HOST}:{API_PORT}"

# Cliente OpenAI-compatible apuntando a DeepSeek
# NOTA: El paquete "openai" aquí se usa como SDK compatible con APIs tipo OpenAI.
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)

# Modelo recomendado para modo "chat + tool calling"
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

def api_search_reports(**kwargs):
    """
    Ejecuta la búsqueda real contra tu API local /reports/search.
    `kwargs` son los parámetros que el LLM generó:
      start, end, server_name, security_mode, reason, limit, etc.

    Devuelve un resumen compacto (para ahorrar tokens) que el LLM usará
    para redactar una respuesta final clara y "human-friendly".
    """
    resp = requests.get(f"{API_BASE}/reports/search", params=kwargs, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Resumen compacto para el LLM (reduce costo y latencia)
    return {
        "count": data.get("count", 0),
        "top_ips": data.get("top_ips", [])[:10],
        "top_urls": data.get("top_urls", [])[:10],
        "top_reasons": data.get("top_reasons", [])[:10],
        "samples": [
            # "muestras" = subset de campos para dar contexto sin enviar el JSON completo
            {k: r.get(k) for k in ("_date_human", "server_name", "ip", "reason",
                                   "security_mode", "status", "method", "url")}
            for r in (data.get("results") or [])[:10]
        ],
    }

# Definición de herramienta (tool) para el modelo.
# Esto es lo que el LLM "ve" como función invocable.
# Cuando el LLM decide que necesita datos, emite un tool_call con args JSON.
tools = [{
  "type": "function",
  "function": {
    "name": "search_reports",
    "description": "Busca reports de BunkerWeb por fecha/hora y filtros. Devuelve conteos, tops y muestras.",
    "parameters": {
      "type": "object",
      "properties": {
        # IMPORTANT: start/end obligatorios (mínimo para acotar consultas)
        "start": {"type": "string", "description": "ISO8601 con offset -06:00 o epoch(seg)."},
        "end": {"type": "string", "description": "ISO8601 con offset -06:00 o epoch(seg)."},

        # Filtros exactos soportados por bw_api.py
        "server_name": {"type": "string"},
        "ip": {"type": "string"},
        "security_mode": {"type": "string", "description": "block|allow"},
        "status": {"type": "integer"},
        "reason": {"type": "string"},
        "country": {"type": "string"},
        "method": {"type": "string"},

        # Filtros contains
        "url_contains": {"type": "string"},
        "ua_contains": {"type": "string"},

        # Control del orden y tamaño de respuesta
        "order": {"type": "string", "description": "newest|oldest"},
        "limit": {"type": "integer"}
      },
      "required": ["start", "end"]
    }
  }
}]

def now_iso():
    """
    Retorna la hora actual en ISO8601 con timezone.
    Se usa para dar contexto al LLM en el prompt del sistema.
    """
    return datetime.now(tz=TZ).isoformat()

# Prompt del sistema:
# Aquí definimos el "rol" del modelo y reglas de interpretación de fechas.
# Esto es clave para que DeepSeek convierta "hoy/ayer/anoche" a start/end.
SYSTEM = (
    "Eres un asistente SecOps para BunkerWeb.\n"
    f"Zona horaria: {TZ.key}. Hora actual: {now_iso()}.\n"
    "El usuario pregunta en español de forma natural (hoy, ayer, anoche 10-11pm, etc.).\n"
    "Debes convertir eso a filtros y llamar la herramienta search_reports.\n"
    "Reglas:\n"
    "- Si el usuario no da rango de tiempo, asume 'hoy 00:00-23:59' en America/Monterrey.\n"
    "- Si dice 'ayer', usa el día anterior completo.\n"
    "- Si dice 'anoche 10 a 11', interpreta 22:00-23:00 del día anterior "
    "(si ya pasó medianoche, anoche=ayer).\n"
    "- Si pide 'top', usa limit=200 para tener mejor muestra.\n"
    "- Después responde con: resumen, top_ips, top_urls, top_reasons y 3-10 muestras.\n"
)

def main():
    """
    Loop interactivo:
    - Lee input del usuario en consola.
    - Envía historial (messages) + tools al modelo.
    - Si el modelo pide tool_call -> ejecuta la API -> devuelve resultados al modelo.
    - Imprime la respuesta final y conserva el historial.
    """
    print("✅ DeepSeek chat listo. Escribe tu consulta (o 'exit').\n")

    # Historial de conversación. Empezamos con el system prompt.
    messages = [{"role": "system", "content": SYSTEM}]

    while True:
        q = input("> ").strip()
        if q.lower() in ("exit", "quit"):
            break

        # Agrega el mensaje del usuario al historial
        messages.append({"role": "user", "content": q})

        # 1) Primer llamado al modelo con tools habilitadas
        #    - El modelo puede contestar directo o pedir tool_call
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
        )
        msg = resp.choices[0].message

        # Si el modelo pidió ejecutar una herramienta (tool_call)
        if getattr(msg, "tool_calls", None):
            # Tomamos el primer tool_call (para este caso solo usamos 1 tool)
            tc = msg.tool_calls[0]

            # Arguments vienen como JSON string generado por el LLM
            args = json.loads(tc.function.arguments)

            # 2) Ejecutamos la búsqueda real contra nuestra API local
            data = api_search_reports(**args)

            # Guardamos el mensaje del asistente (que contiene el tool_call)
            messages.append(msg)

            # 3) Respondemos al modelo con el resultado de la herramienta
            #    - role="tool" y tool_call_id enlazan este resultado con el tool_call
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(data, ensure_ascii=False)
            })

            # 4) Segundo llamado al modelo, ahora SIN tools, para que redacte
            #    la respuesta final basada en el resultado de la herramienta.
            resp2 = client.chat.completions.create(
                model=MODEL,
                messages=messages,
            )
            answer = resp2.choices[0].message.content or ""

            # Imprime y guarda en historial
            print("\n" + answer + "\n")
            messages.append({"role": "assistant", "content": answer})

        else:
            # Si no hubo tool_calls, imprime la respuesta directa del modelo
            print("\n" + (msg.content or "") + "\n")
            messages.append({"role": "assistant", "content": msg.content or ""})

if __name__ == "__main__":
    main()

