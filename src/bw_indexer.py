"""
BunkerWeb Reports Indexer (Redis)

Este script toma los eventos/reportes que BunkerWeb guarda en Redis dentro de una LIST
(por defecto: `requests`) y construye un índice optimizado para búsquedas rápidas por:

- Rango de tiempo (ZSET por timestamp)
- Filtros exactos (SETS por server_name, ip, security_mode, reason, etc.)

¿Por qué?
- La UI de BunkerWeb no siempre permite filtrar por fecha/hora u otros criterios.
- Este índice permite que una API (FastAPI) responda rápido a queries como:
  "Hoy 5pm-6pm, bloqueos en www.never8.com"

Estructura de claves que genera (prefijo por defecto: bw_idx):

- ZSET (por tiempo):
  bw_idx:requests:by_date
    member = <id>
    score  = <date epoch (float)>

- JSON por evento (por id):
  bw_idx:req:<id>  -> STRING (JSON completo)

- Dedupe / "ya indexado":
  bw_idx:seen:<id> -> STRING "1" con TTL (opcional)

- Sets por filtro (para intersección rápida):
  bw_idx:server:<server_name>   -> SET de ids
  bw_idx:ip:<ip>                -> SET de ids
  bw_idx:mode:<security_mode>   -> SET de ids (block/allow)
  bw_idx:reason:<reason>        -> SET de ids
  bw_idx:status:<status>        -> SET de ids
  bw_idx:country:<country>      -> SET de ids
  bw_idx:method:<method>        -> SET de ids

Notas operativas:
- "TTL": si se configura, expira tanto el JSON como los sets y el marcador "seen:*"
  para mantener una retención limitada (ej. 60 días).
- Se procesa en chunks para evitar usar demasiada RAM (por defecto 500).
"""

import os, json, argparse
import redis
from dotenv import load_dotenv

# Carga variables desde .env en el directorio actual (si existe)
load_dotenv()

def env(name, default=None):
    """
    Helper para leer variables de entorno con un valor por defecto.
    - Si la variable no existe o está vacía, regresa default.
    """
    v = os.getenv(name)
    return default if v is None or v == "" else v

def main():
    # -----------------------------
    # Args CLI (operación flexible)
    # -----------------------------
    ap = argparse.ArgumentParser()
    ap.add_argument("--source_key", default="requests",
                    help="Nombre de la LIST en Redis donde BunkerWeb guarda los eventos")
    ap.add_argument("--prefix", default="bw_idx",
                    help="Prefijo base para todas las claves del índice")
    ap.add_argument("--ttl_days", type=int, default=60,
                    help="Retención del índice en días. 0 = sin expiración/TTL")
    ap.add_argument("--chunk", type=int, default=500,
                    help="Tamaño de lote para leer la LIST y procesar en partes")
    args = ap.parse_args()

    # -----------------------------
    # Conexión a Redis (desde .env)
    # -----------------------------
    host = env("REDIS_HOST", "127.0.0.1")
    port = int(env("REDIS_PORT", "6379"))
    db = int(env("REDIS_DB", "0"))
    password = env("REDIS_PASSWORD", None)  # puede ser None si no existe auth

    # decode_responses=False -> trabajamos con bytes (más eficiente y consistente)
    r = redis.Redis(host=host, port=port, db=db, password=password, decode_responses=False)

    # TTL en segundos (si ttl_days > 0)
    ttl = args.ttl_days * 86400 if args.ttl_days and args.ttl_days > 0 else None

    # Cantidad total de elementos en la LIST origen
    total = r.llen(args.source_key)
    print(
        f"==> Indexer starting: redis={host}:{port} db={db} "
        f"source={args.source_key} total={total} prefix={args.prefix}",
        flush=True
    )

    # -----------------------------
    # Clave principal del índice
    # -----------------------------
    # ZSET donde guardamos los ids ordenados por timestamp (score)
    z_ts = f"{args.prefix}:requests:by_date".encode()

    # Helpers para construir claves de Redis (todas en bytes)
    def seen_key(rid: str) -> bytes:
        # Marcador para no re-indexar el mismo id (dedupe)
        return f"{args.prefix}:seen:{rid}".encode()

    def req_key(rid: str) -> bytes:
        # JSON completo del evento
        return f"{args.prefix}:req:{rid}".encode()

    def set_key(kind: str, val: str) -> bytes:
        # Sets por atributo (server/ip/mode/reason/status/country/method)
        return f"{args.prefix}:{kind}:{val}".encode()

    # -----------------------------
    # Contadores de métricas
    # -----------------------------
    new_count = 0   # cuántos eventos se indexaron NUEVOS en esta corrida
    bad_json = 0    # cuántos registros no se pudieron parsear
    no_id = 0       # registros sin campo 'id'
    no_date = 0     # registros sin campo 'date' o con date inválido

    # -----------------------------
    # Loop principal por chunks
    # -----------------------------
    for start in range(0, total, args.chunk):
        # Leemos un slice de la LIST: [start, end]
        items = r.lrange(args.source_key, start, min(start + args.chunk - 1, total - 1))
        if not items:
            break

        # ============================================================
        # 1) Primera pasada: parsea JSON + decide cuáles son "nuevos"
        #    usando SET NX sobre bw_idx:seen:<id>
        # ============================================================
        # Pipeline sin transacción = más rápido y suficiente para este caso
        p = r.pipeline(transaction=False)
        parsed = []  # lista de tuplas (rid, obj, raw) únicamente válidas

        for raw in items:
            # raw es bytes -> decodificamos y parseamos JSON
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                bad_json += 1
                continue

            # id único del evento
            rid = obj.get("id")
            if not rid:
                no_id += 1
                continue

            parsed.append((rid, obj, raw))

            # SET NX: solo se setea si no existía antes
            # - Si devuelve True -> es nuevo y debe indexarse
            # - Si devuelve False -> ya estaba indexado en una corrida previa
            if ttl:
                p.set(seen_key(rid), b"1", nx=True, ex=ttl)
            else:
                p.set(seen_key(rid), b"1", nx=True)

        # Ejecuta el pipeline, obteniendo lista de booleans (nuevo / no)
        is_new_list = p.execute() if parsed else []

        # ============================================================
        # 2) Segunda pasada: indexa SOLO los nuevos
        # ============================================================
        p = r.pipeline(transaction=False)

        # zip(parsed, is_new_list) mantiene el orden: cada parsed[i] corresponde a is_new_list[i]
        for (rid, obj, raw), is_new in zip(parsed, is_new_list):
            if not is_new:
                continue

            # timestamp epoch (float) usado para ordenar en ZSET
            ts = obj.get("date")
            try:
                ts = float(ts)
            except Exception:
                no_date += 1
                continue

            # 2.1 Guardar JSON completo (para "leer el evento" después)
            p.set(req_key(rid), raw)
            if ttl:
                p.expire(req_key(rid), ttl)

            # 2.2 Índice por fecha/hora (ZSET)
            # Permite queries rápidas por rango: ZRANGEBYSCORE(start_ts, end_ts)
            p.zadd(z_ts, {rid: ts})

            # 2.3 Índices por campos comunes (SETS)
            # Permite intersección: ids_en_rango ∩ ids_por_server ∩ ids_por_mode ...
            for kind, field in [
                ("ip", "ip"),
                ("server", "server_name"),
                ("mode", "security_mode"),
                ("status", "status"),
                ("reason", "reason"),
                ("country", "country"),
                ("method", "method"),
            ]:
                v = obj.get(field)
                if v is None or v == "":
                    continue

                k = set_key(kind, str(v))
                # guardamos rid como bytes dentro del SET
                p.sadd(k, rid.encode())

                # TTL al SET para mantener retención similar a los eventos
                if ttl:
                    p.expire(k, ttl)

            new_count += 1

        # Ejecutar escritura del chunk (si hubo nuevos)
        # Nota: este "if new_count" es global, no por chunk.
        # Funciona, pero si quieres micro-optimizar, se puede cambiar a
        # un contador por-chunk.
        if new_count:
            p.execute()

    # -----------------------------
    # Resumen final
    # -----------------------------
    print(
        f"✅ Done. new_indexed={new_count} bad_json={bad_json} "
        f"no_id={no_id} no_date={no_date}",
        flush=True
    )
    print(
        f"   ZSET created: {args.prefix}:requests:by_date (ZCARD={r.zcard(z_ts)})",
        flush=True
    )

if __name__ == "__main__":
    main()

