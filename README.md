# BunkerWeb Reports Agent (Redis Indexer + FastAPI + DeepSeek Chat)

Este proyecto agrega capacidades avanzadas de búsqueda y filtrado sobre los *reports/events* de BunkerWeb almacenados en Redis.
Construye índices en Redis para consultas rápidas (por rango de tiempo, `server_name`, `ip`, `reason`, etc.), expone una API local
con FastAPI y habilita un modo conversacional (CLI) usando DeepSeek (*tool calling*).

> ✅ Ideal para SecOps/Infra cuando la UI de BunkerWeb no permite filtros por fecha/hora u otros criterios.

---

## ¿Qué hace?

- **Indexa** los eventos de BunkerWeb desde Redis (`LIST requests`)
- **Crea índices** en Redis (`ZSET` por tiempo + `SETs` por filtros)
- **Expone una API local**: `GET /reports/search`
- **Permite preguntas naturales** en español con DeepSeek:
  - “Dame los bloqueos de hoy para www.example.com”
  - “Hoy de 5pm a 6pm, bloqueos en www.example.com”
  - “Top 10 IPs bloqueadas ayer en www.example.com”

---

## Estructura del repositorio

```
bw-reports-agent/
  src/
    bw_indexer.py
    bw_api.py
    bw_chat.py
  systemd/
    bw-reports-api.service
    bw-reports-index.service
    bw-reports-index.timer
  .env.example
  .gitignore
  requirements.txt
  README.md
  LICENSE
```

---

## Requisitos

- Python 3.9+
- Redis accesible desde el host
- BunkerWeb escribiendo eventos en Redis: `LIST requests`
- API Key de DeepSeek (solo para el chat)

---

## Instalación

### 1) Crear venv e instalar dependencias

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2) Configurar variables de entorno

```bash
cp .env.example .env
# Edita .env y configura DEEPSEEK_API_KEY
chmod 600 .env
```

> ⚠️ Nunca subas `.env` al repositorio público.

---

## Ejecución (manual)

### 1) Indexar eventos en Redis

Esto construye/actualiza el índice para búsquedas rápidas:

```bash
source venv/bin/activate
python -u src/bw_indexer.py --ttl_days 60
```

### 2) Levantar la API local (FastAPI)

Opción A (recomendada desde el root del repo):

```bash
source venv/bin/activate
uvicorn bw_api:app --app-dir src --host 127.0.0.1 --port 8811
```

Opción B (si ejecutas desde `src/`):

```bash
cd src
uvicorn bw_api:app --host 127.0.0.1 --port 8811
```

Healthcheck:

```bash
curl -sS http://127.0.0.1:8811/health
```

### 3) Ejecutar chat conversacional (DeepSeek)

```bash
source venv/bin/activate
python src/bw_chat.py
```

Escribe `exit` para salir.

---

## Ejemplos de preguntas (Spanish / natural language)

- `Dame los bloqueos de hoy para www.example.com`
- `Hoy de 5pm a 6pm, dame los bloqueos en www.example.com`
- `Hoy de 9am a 6pm, dame los bloqueos en www.example.com`
- `Top 10 IPs bloqueadas ayer en www.example.com`
- `Busca intentos a /wp-login.php hoy y muestra 5 ejemplos`

---

## Uso de la API (ejemplos)

### Solo por rango de tiempo

```bash
curl -sS "http://127.0.0.1:8811/reports/search?start=2026-01-01T00:00:00-06:00&end=2026-01-02T00:00:00-06:00&limit=5" \
| python -m json.tool
```

### Con filtros

```bash
curl -sS "http://127.0.0.1:8811/reports/search?start=2026-01-01T00:00:00-06:00&end=2026-01-02T00:00:00-06:00&server_name=www.example.com&security_mode=block&limit=5" \
| python -m json.tool
```

---

## Claves de Redis (resumen)

**Origen (BunkerWeb):**
- `requests` (LIST) -> JSON por evento

**Índice (por defecto prefijo `bw_idx`):**
- `bw_idx:requests:by_date` (ZSET) -> member=`id`, score=`date epoch`
- `bw_idx:req:<id>` (STRING) -> JSON completo por id
- `bw_idx:seen:<id>` (STRING) -> deduplicación (SET NX)
- `bw_idx:server:<server_name>` (SET) -> ids
- `bw_idx:ip:<ip>` (SET) -> ids
- `bw_idx:mode:<security_mode>` (SET) -> ids
- `bw_idx:reason:<reason>` (SET) -> ids
- `bw_idx:status:<status>` (SET) -> ids
- `bw_idx:country:<country>` (SET) -> ids
- `bw_idx:method:<method>` (SET) -> ids

---

## systemd (producción)

Los unit files sugeridos están en `systemd/`:

- `bw-reports-api.service` (API siempre arriba)
- `bw-reports-index.service` + `bw-reports-index.timer` (reindexado periódico)

Ejemplo de instalación:

```bash
sudo cp systemd/bw-reports-api.service /etc/systemd/system/
sudo cp systemd/bw-reports-index.service /etc/systemd/system/
sudo cp systemd/bw-reports-index.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now bw-reports-api
sudo systemctl enable --now bw-reports-index.timer
```

Ver estado:

```bash
systemctl status bw-reports-api --no-pager
systemctl list-timers --all | grep bw-reports-index
```

---

## Seguridad

- Mantén la API ligada a `127.0.0.1` (no la expongas públicamente).
- No subas `.env` ni tokens/API keys.
- Rota cualquier key expuesta.
- Usa permisos `chmod 600 .env`.

---

## Troubleshooting

### La API no levanta

```bash
systemctl status bw-reports-api --no-pager
journalctl -u bw-reports-api -n 200 --no-pager
ss -ltnp | grep :8811 || true
```

### La API responde pero `count=0`

Verifica índice:

```bash
redis-cli -h 127.0.0.1 -p 6379 -n 0 ZCARD bw_idx:requests:by_date
```

Si es 0, ejecuta indexador:

```bash
source venv/bin/activate
python -u src/bw_indexer.py --ttl_days 60
```

---

## Licencia

Ver `LICENSE`.

