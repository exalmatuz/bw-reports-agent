"""
BunkerWeb Reports Search API (FastAPI + Redis)

Este servicio expone una API HTTP local para consultar eventos/reports de BunkerWeb
almacenados en Redis y previamente indexados por `bw_indexer.py`.

Objetivo:
- Buscar por rango de tiempo (start/end) y filtrar por campos como:
  server_name, ip, security_mode (block/allow), reason, status, method, country, etc.
- Devolver resultados + agregados rápidos (top_ips/top_urls/top_reasons)
  para consumo por CLI, scripts o un chat conversacional (DeepSeek).

Dependencias:
- Redis (índices):
  - <prefix>:requests:by_date (ZSET) -> ids por timestamp
  - <prefix>:req:<id> (STRING) -> JSON completo por id
  - <prefix>:server:<server_name> (SET) -> ids
  - <prefix>:mode:<security_mode> (SET) -> ids
  - <prefix>:reason:<reason> (SET) -> ids
  - etc.

Notas:
- El servicio corre localmente en 127.0.0.1 (recomendado por seguridad).
- start/end aceptan ISO8601 o epoch (segundos o milisegundos).
- Python 3.9: se usa Optional[] en lugar de "str | None".
"""

import os, json
from typing import Optional
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
from fastapi import FastAPI, Query
from dateutil import parser as dtparser
from dotenv import load_dotenv

# Carga variables desde /opt/bw-reports-agent/.env (si existe)
load_dotenv()

def env(name, default=None):
    """
    Helper para variables de entorno con fallback a default
    (evita None o strings vacías).
    """
    v = os.getenv(name)
    return v if v not in (None, "") else default

# Zona horaria usada para interpretar fechas sin tz y para "_date_human"
TZ = ZoneInfo(env("TZ", "America/Monterrey"))

def to_epoch(x: str) -> float:
    """
    Convierte un valor de entrada a epoch (segundos).
    Soporta:
      - epoch en segundos (ej. 1767285096.831)
      - epoch en milisegundos (ej. 1767285096831)
      - ISO8601 (ej. 2026-01-07T10:00:00-06:00)
      - fechas "humanas" parseables por dateutil (ej. 2026-01-07 10:00)

    Regla:
      - Si es número y muy grande (> 10^10), se asume milisegundos y se divide /1000
      - Si la fecha no trae timezone, se asume TZ=America/Monterrey
    """
    s = str(x).strip()

    # Caso 1: numérico (epoch en seg o ms)
    if s.replace(".", "", 1).isdigit():
        n = float(s)
        return n / 1000 if n > 10_000_000_000 else n

    # Caso 2: ISO8601 u otras representaciones parseables
    dt = dtparser.parse(s)
    if dt.tzinfo is None:
        # Si no especifica tz, asumimos TZ local
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp()

# -----------------------------
# Conexión a Redis
# -----------------------------
host = env("REDIS_HOST", "127.0.0.1")
port = int(env("REDIS_PORT", "6379"))
db = int(env("REDIS_DB", "0"))

# decode_responses=False -> trabajamos con bytes; consistente con el indexer
r = redis.Redis(host=host, port=port, db=db, decode_responses=False)

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="BunkerWeb Reports Search API", version="1.0")

# Prefijo por defecto de las claves del índice
PREFIX_DEFAULT = "bw_idx"

@app.get("/health")
def health():
    """
    Healthcheck simple.
    Útil para systemd/liveness checks.
    """
    return {"ok": True}

@app.get("/reports/search")
def search_reports(
    # start/end son obligatorios: definen el rango temporal
    start: str = Query(..., description="ISO8601 o epoch(seg)"),
    end: str = Query(..., description="ISO8601 o epoch(seg)"),

    # prefijo del índice (en caso de tener varios índices)
    prefix: str = Query(PREFIX_DEFAULT),

    # Filtros exactos (intersección de SETs):
    # Nota: Optional[] por compatibilidad con Python 3.9
    server_name: Optional[str] = None,
    ip: Optional[str] = None,
    security_mode: Optional[str] = None,  # block|allow
    status: Optional[int] = None,
    reason: Optional[str] = None,
    country: Optional[str] = None,
    method: Optional[str] = None,

    # Filtros "contains" (evaluados después, sobre el JSON ya recuperado):
    # Útiles para búsquedas parciales por URL o User-Agent
    url_contains: Optional[str] = None,
    ua_contains: Optional[str] = None,

    # Orden (por defecto newest)
    order: str = Query("newest", description="newest|oldest"),

    # Límite de resultados devueltos (para no explotar memoria)
    limit: int = 50,
):
    """
    Endpoint principal de búsqueda.

    Estrategia de búsqueda:
    1) Obtener IDs por rango de tiempo desde el ZSET:
       <prefix>:requests:by_date
    2) Convertir la lista a set (candidates) para poder intersectar rápido.
    3) Por cada filtro exacto (server/ip/mode/status/...), intersectar contra el SET correspondiente:
       <prefix>:server:<server_name>, etc.
    4) Mantener el orden temporal original (ids list) y recortar a `limit`.
    5) Para cada id final, leer JSON completo:
       <prefix>:req:<id>
    6) Aplicar filtros contains (url_contains, ua_contains).
    7) Enriquecer con fecha humana y calcular top agregados.
    """

    # Convertimos start/end a epoch (segundos)
    start_ts = float(to_epoch(start))
    end_ts = float(to_epoch(end))

    # Clave del ZSET de tiempo
    z_ts = f"{prefix}:requests:by_date"

    # IDs dentro del rango temporal (bytes)
    # zrangebyscore devuelve members cuyo score está entre [start_ts, end_ts]
    ids = r.zrangebyscore(z_ts, start_ts, end_ts)

    # Mantener orden temporal:
    # - zrangebyscore regresa en orden ascendente por score
    # - si queremos newest, invertimos
    if order == "newest":
        ids = list(reversed(ids))

    # candidates = conjunto inicial de ids (por tiempo)
    # Luego se va reduciendo por intersecciones con SETs
    candidates = set(ids)

    def intersect(kind: str, value: str):
        """
        Intersecta candidates con el SET: <prefix>:<kind>:<value>
        Ejemplos:
          kind=server, value=magento.never8.com -> bw_idx:server:magento.never8.com
          kind=mode, value=block -> bw_idx:mode:block
        """
        nonlocal candidates
        key = f"{prefix}:{kind}:{value}".encode()
        members = r.smembers(key)  # devuelve bytes ids
        candidates = candidates.intersection(members)

    # Aplicar filtros exactos (si vienen)
    if ip:
        intersect("ip", ip)
    if server_name:
        intersect("server", server_name)
    if security_mode:
        intersect("mode", security_mode)
    if status is not None:
        # status está indexado como string en set_key(...)
        intersect("status", str(status))
    if reason:
        intersect("reason", reason)
    if country:
        intersect("country", country)
    if method:
        intersect("method", method)

    # Reconstruir lista manteniendo el orden original (ids) y aplicar limit:
    # - ids tiene orden temporal (newest/oldest)
    # - candidates tiene el filtro final
    ordered = [rid for rid in ids if rid in candidates][:limit]

    results = []
    for rid_b in ordered:
        # rid_b es bytes, lo pasamos a str para formar la key req:<id>
        rid = rid_b.decode("utf-8", "ignore")

        # Leer JSON completo del evento (STRING)
        raw = r.get(f"{prefix}:req:{rid}".encode())
        if not raw:
            # Puede pasar si expira la key, o si hubo inconsistencia temporal
            continue

        # Parsear JSON del evento
        obj = json.loads(raw.decode("utf-8", "replace"))

        # Filtros contains (después del filtro fuerte por tiempo/sets):
        # - Esto evita cargar demasiados registros antes de filtrar.
        if url_contains and url_contains not in str(obj.get("url", "")):
            continue
        if ua_contains and ua_contains.lower() not in str(obj.get("user_agent", "")).lower():
            continue

        # Agregar campo humano para lectura (ISO8601 en TZ)
        ts = float(obj.get("date", 0))
        obj["_date_human"] = datetime.fromtimestamp(ts, tz=TZ).isoformat()

        results.append(obj)

    # Agregados rápidos para "reporte SOC"
    # - most_common(10) para top 10
    top_ips = Counter([x.get("ip") for x in results if x.get("ip")]).most_common(10)
    top_urls = Counter([x.get("url") for x in results if x.get("url")]).most_common(10)
    top_reasons = Counter([x.get("reason") for x in results if x.get("reason")]).most_common(10)

    # Respuesta final (JSON)
    return {
        "count": len(results),
        "top_ips": top_ips,
        "top_urls": top_urls,
        "top_reasons": top_reasons,
        "results": results,
    }

