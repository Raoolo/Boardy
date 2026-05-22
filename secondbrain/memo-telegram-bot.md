# Boardy — Telegram bot (per me futuro)

Miniguida operativa per il bot Telegram. Setup, comandi, troubleshooting. Per il "perché" delle scelte di design vedi `../docs/LEARNINGS.md` (entry 2026-05-20).

> 💡 Il bot è un **thin client** di `POST /chat`. Non rifa il chat loop: passa il messaggio al server e mostra la risposta. Cambi i tool in `app/tools.py` → il bot li eredita gratis, niente da toccare qui.

---

## Mental model in 4 righe

- **Processo separato** dal server web. Crash del bot → la web app vive lo stesso. Restart indipendente.
- **Allow-list owner** = lista di `user_id` Telegram (interi). Chi è dentro parla con Boardy come owner (write tools). Chi è fuori parla come guest (read-only).
- **Owner**: il bot fa `POST /auth/login` a Boardy con `BOARDY_BOT_USERNAME`/`PASSWORD`, prende il cookie, lo riusa. Una conv Boardy persistente per chat Telegram.
- **Guest**: nessun cookie. History tenuta in memoria del bot. Restart bot = guest perdono il contesto (come `sessionStorage` del web).

---

## Setup iniziale (una tantum)

### 1. Crea il bot su Telegram

1. Apri Telegram → cerca `@BotFather` → `/newbot`.
2. Scegli un nome (es. "Boardy Personale") e uno username univoco (es. `boardy_raulo_bot`).
3. Copia il **token** che ti dà (formato `123456789:ABC-DEF...`). Tienilo segreto come una API key.

### 2. Configura `.env`

```bash
TELEGRAM_BOT_TOKEN=123456789:ABC-DEF...
BOARDY_BOT_USERNAME=raulo                  # account creato con etl/create_user.py
BOARDY_BOT_PASSWORD=<la_tua_password>
# TELEGRAM_OWNER_IDS=                       # lasciar vuoto inizialmente, lo riempi al passo 4
# BOARDY_BASE_URL=http://127.0.0.1:8765    # default OK in locale; Docker lo sovrascrive
```

> Se non hai ancora un account Boardy: `uv run python etl/create_user.py create <username>` → ti chiede la password.

### 3. Lancia il bot

```bash
# In locale (lascia uvicorn su un'altra finestra)
uv run python -m bot.telegram_bot

# Output atteso:
# Owner IDs: (nessuno: tutti guest) | Boardy: http://127.0.0.1:8765 | state file: …/data/telegram_chats.json
# Autenticato in Boardy come raulo
# (in attesa di messaggi)
```

### 4. Scopri il tuo `user_id` e mettilo nell'allow-list

1. Su Telegram cerca il bot per username → `/start`.
2. Manda `/whoami` → ti risponde con il tuo `user_id` numerico.
3. Aggiorna `.env`:
   ```
   TELEGRAM_OWNER_IDS=12345678
   ```
   (Più owner = CSV: `TELEGRAM_OWNER_IDS=12345,67890`)
4. Riavvia il bot (`Ctrl+C` + rilancio). Ora `/whoami` dice "ruolo: owner".

### 5. Smoke test

- Da owner: "ho comprato 100 buste 63.5x88" → deve confermare l'update inventory.
- Da guest (apri il bot con un altro account / amico): "che giochi ho?" → risponde, ma "aggiungi X" → si rifiuta (i write tools non sono nemmeno esposti).

---

## Comandi del bot

| Comando | Cosa fa |
|---|---|
| `/start` | Saluto + dice il tuo ruolo |
| `/new` | Reset conversazione corrente (azzera storico per QUESTA chat) |
| `/whoami` | Mostra il tuo `user_id` Telegram + ruolo Boardy |
| `/help` | Lista comandi |
| qualsiasi testo | Domanda a Boardy |

---

## Deploy: bot in Docker (server)

Il bot ha un profilo compose dedicato:

```bash
# Solo bot + boardy
docker compose -f deploy/docker-compose.yml --profile telegram up -d

# Bot + tunnel CF + boardy (production setup)
docker compose -f deploy/docker-compose.yml --profile tunnel --profile telegram up -d
```

Cose da sapere:
- L'image è la stessa di boardy (riusa layer cache, ~0 sec extra di build).
- Il container `boardy-telegram` legge `.env` dalla root del repo (stesso file di boardy).
- `BOARDY_BASE_URL` è sovrascritto a `http://boardy:8765` (DNS interno docker), non serve esporre la porta su internet.
- Lo stato (`telegram_chats.json`) vive nel named volume `boardy_db` insieme al DB. Sopravvive a restart e rebuild.

**Update workflow** del bot (identico a boardy):
```bash
git pull
docker compose -f deploy/docker-compose.yml restart telegram-bot
```

---

## Cosa persiste e cosa no

| Cosa | Dove | Sopravvive a restart? |
|---|---|---|
| Conv attiva per chat owner | `<BOARDY_DB dir>/telegram_chats.json` (mapping `chat_id → conv_id`) + tabella `conversations` lato Boardy | **Sì** |
| Storia messaggi owner | Tabella `conversations` lato Boardy (saved a ogni turno) | **Sì** |
| Storia messaggi guest | Memoria del processo bot | **No** (by design, specchia sessionStorage del web) |
| Allow-list owner | `.env` (`TELEGRAM_OWNER_IDS`) | **Sì** |
| Cookie sessione del bot | In memoria + lo rifa se serve | N/A (re-login automatico) |

---

## Troubleshooting comune

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| `TELEGRAM_BOT_TOKEN non impostato` | env mancante | Aggiungi `TELEGRAM_BOT_TOKEN` in `.env` |
| Bot parte ma `/whoami` dice "guest" anche a me | il mio user_id non è in `TELEGRAM_OWNER_IDS` | Aggiungilo, riavvia il bot |
| Owner riceve "ruolo: owner" ma write fallisce | `BOARDY_BOT_USERNAME`/`PASSWORD` mancanti o sbagliati → bot fa fallback a guest mode | Verifica login con `curl -d '{"username":"…","password":"…"}' -H 'content-type: application/json' http://127.0.0.1:8765/auth/login` |
| `HTTP 401 da /chat` ricorrente | Cookie scaduto + relogin fallisce | Controlla che l'account esista (`uv run python etl/create_user.py list`); ruota password se serve |
| Bot non risponde, niente log | Polling bloccato / token revocato | `docker compose logs telegram-bot` → se vedi 401 da Telegram, regenera il token da BotFather |
| Risposta troncata | Telegram limita a 4096 char per messaggio | Il bot già spezza in chunk; se vedi una risposta SINGOLA troncata è un bug, segnalalo |
| Markdown rotto (vedi `**`, `*` letterali) | Il fallback a plain text è scattato (asterischi non bilanciati) | Cosmetico, ignora. Se ricorrente, rivedi il prompt di Boardy |
| `state file illeggibile` | JSON corrotto su disco | `rm data/telegram_chats.json` (perdi solo il mapping chat→conv, riparti puliti) |

---

## Modifiche frequenti

### Aggiungere un owner

```bash
# in .env (locale o sul server)
TELEGRAM_OWNER_IDS=12345,99999          # aggiungi separato da virgola
# poi:
docker compose -f deploy/docker-compose.yml restart telegram-bot
```

### Disattivare temporaneamente

```bash
docker compose -f deploy/docker-compose.yml stop telegram-bot
```
Il container resta, niente polling. `start` per riattivare.

### Cambiare la password owner di Boardy

1. `uv run python etl/create_user.py reset <username>` (ti chiede la nuova).
2. Aggiorna `BOARDY_BOT_PASSWORD` in `.env`.
3. Restart bot. Il prossimo messaggio owner farà re-login automatico.

### Aggiungere un nuovo comando

Edit `bot/telegram_bot.py`: aggiungi una `async def cmd_x(...)` + `app.add_handler(CommandHandler("x", cmd_x))` in `build_app()`. Restart bot. Niente da toccare lato server.

---

## Sicurezza in 4 punti

1. **Token bot** in `.env`, mai in repo. Se leak → @BotFather `/revoke` e rigenera.
2. **Allow-list user_id**: l'API Telegram garantisce l'autenticità dell'`user_id` mittente, non è spoofabile. Solo chi controlla quell'account può fare write.
3. **Credenziali Boardy** (`BOARDY_BOT_USERNAME/PASSWORD`) in `.env`. Cookie firmato lato server, ruotabile via `BOARDY_SESSION_SECRET` (invalida tutto).
4. **Niente messaggi sensibili nei log Telegram**: per questo non c'è un login via `/start <password>` — il messaggio finirebbe in chiaro nei log lato Telegram.

---

## File di riferimento nel repo

- `bot/telegram_bot.py` — implementazione (~280 righe)
- `bot/__init__.py` — package marker (vuoto)
- `deploy/docker-compose.yml` — service `telegram-bot` sotto profilo `telegram`
- `pyproject.toml` — dep `python-telegram-bot>=21.0`
- `.env.example` — sezione "Telegram bot" con tutte le env documentate
- `../docs/LEARNINGS.md` (2026-05-20) — decisioni di design
- `memo-boardy-future.md` §3 — contesto storico (alternative scartate: WhatsApp, Discord)
