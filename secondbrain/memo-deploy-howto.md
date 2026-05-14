# Boardy — guida operativa Docker + git (per me futuro)

Reference rapido per quando ho dimenticato la sequenza. Documentazione *esecutiva*, non concettuale — per il "perché" leggi `LEARNINGS.md`.

---

## Mental model in 4 righe

- **L'image Docker** è statica: Python + deps + modello e5. Si rebuilda solo quando cambiano `pyproject.toml` / `uv.lock`.
- **Il container in esecuzione** monta `app/`, `etl/`, `web/` come bind-mount read-only dal repo. Cambia il codice → `docker compose restart boardy` → 5 secondi.
- **Lo stato persistente** (DB + rulebooks PDFs) vive in volumi/host, sopravvive a qualunque rebuild.
- **I segreti** (`.env`) sono letti a runtime, mai impacchettati nell'image.

---

## Setup iniziale (una tantum, da fare sul server amico)

```bash
# 1. install
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER          # poi logout/login per applicare

# 2. clone
git clone https://github.com/Raoolo/Boardy.git ~/boardy
cd ~/boardy

# 3. config
cp .env.example .env
nano .env                              # incolla le chiavi: ANTHROPIC, TAVILY, BGG, DEEPSEEK (opzionale)

# 4. (se vuoi CF Tunnel built-in) genera il token sul dashboard Cloudflare
#    Zero Trust → Networks → Tunnels → Create → copy token → in .env CF_TUNNEL_TOKEN=...
#    Public Hostname: boardy.<dominio>.tld → http://boardy:8765

# 5. trasferisci il DB esistente (oppure salta e re-importa via ETL)
scp /path/to/local/boardy.db user@server:~/boardy-init.db
docker volume create boardy_boardy_db                    # se non esiste già
docker run --rm -v boardy_boardy_db:/data -v ~:/host alpine \
  cp /host/boardy-init.db /data/boardy.db

# 6. up
docker compose --profile tunnel up -d  # con tunnel cloudflared incluso
# OR
docker compose up -d                   # solo boardy su 127.0.0.1:8765 (l'amico gestisce il reverse proxy)

# 7. verifica
docker compose ps                      # tutto "Up (healthy)"?
docker compose logs -f boardy          # niente stack trace?
curl http://127.0.0.1:8765/            # restituisce HTML?
```

---

## Update workflow (la cosa che farai 90% delle volte)

```bash
# LATO TUO (Windows, sviluppo)
cd ~/boardy                            # OneDrive / il tuo path
# ...edit code, test in locale con `uv run uvicorn app.main:app --port 8765`...
git add <files>
git commit -m "Messaggio sensato"
git push origin main

# LATO SERVER AMICO (SSH in)
cd ~/boardy
git pull
docker compose restart boardy          # solo per cambiamenti Python
# HTML/CSS/JS: niente comando, basta refresh browser
```

**Quanto è veloce**:
- HTML only: 0 sec (bind-mount + browser refresh)
- Python: 5 sec (restart container)
- Deps cambiate: ~2 min (`docker compose --profile tunnel up -d --build`)
- Schema DB: 0 sec extra (migrazione idempotente, gira a ogni boot)

---

## Quando rebuildare l'image (raro)

Solo se in commit cambi:
- `pyproject.toml` o `uv.lock` → nuove dipendenze
- `Dockerfile` → cambi nella base image / nel setup

Comando:
```bash
docker compose --profile tunnel up -d --build
```

L'image build ricostruisce dal layer cache, quindi se non hai toccato `pyproject.toml` il primo build prende ~3-5 min e quelli successivi <1 min. Il modello e5 sta in un layer suo, non si ri-scarica.

---

## Troubleshooting comune

| Sintomo | Comando |
|---|---|
| Container non parte | `docker compose logs boardy` |
| "command not found" su `uvicorn` | Image vecchia o build interrotta → `docker compose up -d --build` |
| 502 dal browser (tunnel mode) | `docker compose logs cloudflared` — token sbagliato? hostname non puntato? |
| DB corrotto / vuoto | `docker compose exec boardy ls -la /data` — il volume c'è? `BOARDY_DB=/data/boardy.db` è settato? |
| Modello e5 si ri-scarica all'avvio | Image stale: `docker compose build --no-cache boardy` |
| Voglio entrare nel container per debug | `docker compose exec boardy bash` |
| Voglio backup del DB | `docker compose exec boardy cat /data/boardy.db > backup-$(date +%F).db` |
| Stop everything | `docker compose --profile tunnel down` (rimuove i container, lascia i volumi) |
| Reset completo (PERDE IL DB) | `docker compose --profile tunnel down -v` |

---

## Cosa NON è dentro l'image (chiarimento per amico paranoico)

L'image Docker è "innocua": non contiene segreti né dati personali. Si compone solo di:
- `python:3.13-slim` base
- `.venv` con FastAPI/Anthropic SDK/etc. (deps dichiarate in `pyproject.toml`)
- Modello `multilingual-e5-base` pre-scaricato dalla Hugging Face Hub

**Non c'è nell'image**:
- `boardy.db` (vive nel named volume `boardy_db`)
- `data/ElencoGiochi.xlsx` (mai COPYato; resta sul filesystem dell'host se serve all'ETL)
- `.env` (letto da Docker a runtime via `env_file`, mai impacchettato)
- `rulebooks/*.pdf` (bind-mountato dall'host)
- Qualunque cosa in `secondbrain/`, `.git/`, `__pycache__/`, `_imports/` (escluso da `.dockerignore`)

Quindi anche se l'amico pushasse l'image su un registry pubblico non ci sarebbero leak. Le chiavi API stanno solo nel `.env` del server che esegue il container.

---

## Stop al server locale (Windows)

Quando finisco di sviluppare:
```powershell
# Trova il PID di uvicorn:
Get-NetTCPConnection -LocalPort 8765 | Select-Object OwningProcess
Stop-Process -Id <PID> -Force
```

Oppure se sto facendo dev attivo e mi serve auto-restart:
```bash
uv run uvicorn app.main:app --port 8765 --reload
```

Il `--reload` watch dei file Python e re-launcha il processo a ogni save. Utile in dev, da NON usare in prod (overhead + race conditions su workers multipli).

---

## Cheat-sheet finale

```bash
# Sviluppo locale (Windows)
uv run uvicorn app.main:app --port 8765 [--reload]

# Server amico, ciclo standard
git pull && docker compose restart boardy

# Server amico, deps cambiate
docker compose --profile tunnel up -d --build

# Server amico, debug
docker compose logs -f boardy
docker compose exec boardy bash

# Server amico, fermare tutto
docker compose --profile tunnel down
```
