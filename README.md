# BunkerWeb Reports Agent (Redis Indexer + FastAPI + DeepSeek Chat)

This project adds advanced search and filtering capabilities over BunkerWeb *reports/events* stored in Redis.
It builds Redis indexes for fast queries (time range, `server_name`, `ip`, `reason`, etc.), exposes a local FastAPI
service, and provides a conversational CLI powered by DeepSeek (tool calling).

> ✅ Ideal for SecOps/Infra teams when the BunkerWeb UI doesn’t provide date/time filters or other needed criteria.

---

## What it does

- **Indexes** BunkerWeb events from Redis (`LIST requests`)
- **Builds Redis indexes** (`ZSET` for time + `SETs` for filters)
- **Exposes a local API**: `GET /reports/search`
- **Enables natural-language questions** (Spanish) via DeepSeek:
  - “Dame los bloqueos de hoy para www.example.com”
  - “Hoy de 5pm a 6pm, bloqueos en www.example.com”
  - “Top 10 IPs bloqueadas ayer en www.example.com”

---

## Repository layout

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

## Requirements

- Python 3.9+
- Redis reachable from the host
- BunkerWeb writing events into Redis: `LIST requests`
- DeepSeek API key (chat only)

---

## Installation

### 1) Create a venv and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2) Configure environment variables

```bash
cp .env.example .env
# Edit .env and set DEEPSEEK_API_KEY
chmod 600 .env
```

> ⚠️ Never commit `.env` to a public repository.

---

## Run (manual)

### 1) Index events in Redis

This builds/refreshes the indexes used for fast searches:

```bash
source venv/bin/activate
python -u src/bw_indexer.py --ttl_days 60
```

### 2) Start the local API (FastAPI)

Option A (recommended from the repo root):

```bash
source venv/bin/activate
uvicorn bw_api:app --app-dir src --host 127.0.0.1 --port 8811
```

Option B (if you run from `src/`):

```bash
cd src
uvicorn bw_api:app --host 127.0.0.1 --port 8811
```

Healthcheck:

```bash
curl -sS http://127.0.0.1:8811/health
```

### 3) Start the conversational chat (DeepSeek)

```bash
source venv/bin/activate
python src/bw_chat.py
```

Type `exit` to quit.

---

## Example queries (natural language)

- `Dame los bloqueos de hoy para www.example.com`
- `Hoy de 5pm a 6pm, dame los bloqueos en www.example.com`
- `Hoy de 9am a 6pm, dame los bloqueos en www.example.com`
- `Top 10 IPs bloqueadas ayer en www.example.com`
- `Busca intentos a /wp-login.php hoy y muestra 5 ejemplos`

---

## API usage (examples)

### Time range only

```bash
curl -sS "http://127.0.0.1:8811/reports/search?start=2026-01-01T00:00:00-06:00&end=2026-01-02T00:00:00-06:00&limit=5" \
| python -m json.tool
```

### With filters

```bash
curl -sS "http://127.0.0.1:8811/reports/search?start=2026-01-01T00:00:00-06:00&end=2026-01-02T00:00:00-06:00&server_name=www.example.com&security_mode=block&limit=5" \
| python -m json.tool
```

---

## Redis keys (summary)

**Source (BunkerWeb):**
- `requests` (LIST) -> one JSON per event

**Index (default prefix: `bw_idx`):**
- `bw_idx:requests:by_date` (ZSET) -> member=`id`, score=`date epoch`
- `bw_idx:req:<id>` (STRING) -> full JSON by id
- `bw_idx:seen:<id>` (STRING) -> dedup marker (SET NX)
- `bw_idx:server:<server_name>` (SET) -> ids
- `bw_idx:ip:<ip>` (SET) -> ids
- `bw_idx:mode:<security_mode>` (SET) -> ids
- `bw_idx:reason:<reason>` (SET) -> ids
- `bw_idx:status:<status>` (SET) -> ids
- `bw_idx:country:<country>` (SET) -> ids
- `bw_idx:method:<method>` (SET) -> ids

---

## systemd (production)

Suggested unit files live under `systemd/`:

- `bw-reports-api.service` (API always on)
- `bw-reports-index.service` + `bw-reports-index.timer` (periodic re-indexing)

Example install:

```bash
sudo cp systemd/bw-reports-api.service /etc/systemd/system/
sudo cp systemd/bw-reports-index.service /etc/systemd/system/
sudo cp systemd/bw-reports-index.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now bw-reports-api
sudo systemctl enable --now bw-reports-index.timer
```

Check status:

```bash
systemctl status bw-reports-api --no-pager
systemctl list-timers --all | grep bw-reports-index
```

---

## Security

- Keep the API bound to `127.0.0.1` (do not expose it publicly).
- Never commit `.env` or any API keys/tokens.
- Rotate any key that was exposed.
- Use strict perms: `chmod 600 .env`.

---

## Troubleshooting

### API won’t start

```bash
systemctl status bw-reports-api --no-pager
journalctl -u bw-reports-api -n 200 --no-pager
ss -ltnp | grep :8811 || true
```

### API responds but `count=0`

Verify indexes:

```bash
redis-cli -h 127.0.0.1 -p 6379 -n 0 ZCARD bw_idx:requests:by_date
```

If it’s 0, run the indexer:

```bash
source venv/bin/activate
python -u src/bw_indexer.py --ttl_days 60
```

---

## License

See `LICENSE`.

