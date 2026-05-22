"""Telegram client per Boardy.

Architettura: processo separato che parla a `POST /chat` della web app.
Nessuna duplicazione del chat loop — riusa tool gating, audit, persistenza
delle conversazioni gia' esistenti lato server.

Auth model:
- Owner allow-list via env `TELEGRAM_OWNER_IDS` (comma-separated user IDs).
  Gli owner si loggano in Boardy con `BOARDY_BOT_USERNAME` / `BOARDY_BOT_PASSWORD`,
  ottenendo il cookie di sessione → tutti i write tool disponibili, conversation
  persistita in `conversations` lato Boardy.
- Tutti gli altri (incluso chat di gruppo senza utenti owner) ricadono in
  guest mode: solo read tools, history client-side (qui in memoria del bot,
  niente DB).

Stato:
- Map chat_id Telegram → conversation_id Boardy: persistita su disco
  (`<data_dir>/telegram_chats.json`) cosi' un restart del bot non perde
  la conversazione in corso. Per i guest, l'history vive solo in memoria
  (specchia il comportamento del web frontend in guest mode).
- /new svuota la mappatura e l'history del singolo chat_id.

Run:
  uv run python -m bot.telegram_bot
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

LOG = logging.getLogger("boardy.telegram")

# ── Config da env ───────────────────────────────────────────────────────────
BOARDY_BASE_URL = os.environ.get("BOARDY_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OWNER_USERNAME = os.environ.get("BOARDY_BOT_USERNAME")
OWNER_PASSWORD = os.environ.get("BOARDY_BOT_PASSWORD")

# Parsing dell'allow-list. Accettiamo "123,456" o "123, 456" — strip e ignora i vuoti.
_raw_owner_ids = os.environ.get("TELEGRAM_OWNER_IDS", "") or ""
OWNER_IDS: set[int] = {
    int(tok) for tok in (t.strip() for t in _raw_owner_ids.split(",")) if tok
}

# Telegram rifiuta messaggi > 4096 char: spezziamo.
TG_MSG_LIMIT = 4096

# File di persistenza della mappatura chat_id → conv_id Boardy.
# Convenzione: stessa dir del DB Boardy (BOARDY_DB env in Docker, altrimenti
# repo-relative ./data/). Cosi' in Docker il file vive sul named volume.
def _state_file() -> Path:
    boardy_db = os.environ.get("BOARDY_DB")
    if boardy_db:
        return Path(boardy_db).parent / "telegram_chats.json"
    return Path(__file__).resolve().parent.parent / "data" / "telegram_chats.json"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _split(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Spezza un messaggio lungo preservando newline quando possibile.

    Strategia: cerca l'ultimo \\n prima del limit; se troppo presto (<limit/2)
    taglia "hard" al limit. Evita di chiudere un messaggio dopo 50 char
    quando ne avevamo 4000 da spedire.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id in OWNER_IDS


# ── Boardy HTTP client ──────────────────────────────────────────────────────

class BoardyClient:
    """Wrapper async su POST /chat e /auth/login.

    Mantiene il cookie owner in un AsyncClient persistente. Se il cookie
    scade (401 da /chat), tenta un re-login automatico e ripete la chiamata
    una sola volta — evita loop infiniti in caso di credenziali sbagliate.

    Per i guest usiamo un client a singolo uso (no cookie) per essere SICURI
    di non far passare per sbaglio il cookie owner a una chat guest.
    """

    def __init__(self, base_url: str, username: str | None, password: str | None) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        # Timeout alto: il tool-use loop puo' arrivare a 8 round, ognuno con
        # una chiamata LLM + un eventuale web_search. 120s e' un compromesso
        # tra "non bloccarsi se Boardy e' fermo" e "non far scadere risposte legittime".
        self._owner_client = httpx.AsyncClient(base_url=base_url, timeout=120.0)
        self._logged_in = False

    async def close(self) -> None:
        await self._owner_client.aclose()

    async def _login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError(
                "BOARDY_BOT_USERNAME / BOARDY_BOT_PASSWORD non configurati: "
                "owner mode non disponibile."
            )
        r = await self._owner_client.post(
            "/auth/login",
            json={"username": self.username, "password": self.password},
        )
        r.raise_for_status()
        self._logged_in = True
        LOG.info("Autenticato in Boardy come %s", self.username)

    async def chat_owner(self, message: str, conversation_id: int | None) -> dict[str, Any]:
        if not self._logged_in:
            await self._login()
        payload: dict[str, Any] = {"message": message}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        r = await self._owner_client.post("/chat", json=payload)
        if r.status_code == 401:
            # Cookie scaduto / ruotato / app riavviata con secret nuovo.
            # Tenta un singolo re-login e ripete.
            LOG.warning("401 da /chat → re-login")
            self._logged_in = False
            await self._login()
            r = await self._owner_client.post("/chat", json=payload)
        r.raise_for_status()
        return r.json()

    async def chat_guest(self, message: str, history: list[dict]) -> dict[str, Any]:
        # Client one-shot SENZA cookie: zero chance di portarsi dietro
        # accidentalmente l'auth dell'owner.
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as guest:
            r = await guest.post("/chat", json={"message": message, "history": history})
        r.raise_for_status()
        return r.json()


# ── Stato in-memory per chat_id ─────────────────────────────────────────────
# Schema:
#   owner → {"role": "owner",  "conversation_id": int | None}
#   guest → {"role": "guest",  "history": list[dict]}
# Solo conversation_id degli owner viene scritto su disco (l'history guest
# specchia il comportamento sessionStorage del web frontend: muore al
# riavvio, by design).

CHATS: dict[int, dict[str, Any]] = {}


def _load_state() -> None:
    """Carica la mappatura owner chat_id → conv_id da disco."""
    p = _state_file()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        LOG.warning("State file %s illeggibile (%s), riparto vuoto", p, e)
        return
    for chat_id_s, conv_id in (data or {}).items():
        try:
            CHATS[int(chat_id_s)] = {"role": "owner", "conversation_id": int(conv_id)}
        except (TypeError, ValueError):
            continue
    LOG.info("Ripristinate %d conversazioni owner da %s", len(CHATS), p)


def _save_state() -> None:
    """Scrive su disco SOLO le conv owner. Best-effort, errori loggati e ignorati."""
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        str(chat_id): state["conversation_id"]
        for chat_id, state in CHATS.items()
        if state.get("role") == "owner" and state.get("conversation_id") is not None
    }
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        LOG.warning("Impossibile salvare state file %s: %s", p, e)


def _ensure_state(chat_id: int, is_owner: bool) -> dict[str, Any]:
    """Restituisce lo state per la chat, creandolo se necessario.

    Se il ruolo cambia (es. chat condivisa dove un owner ed un guest
    si alternano, oppure il bot viene rimosso/riconfigurato lato env),
    ricreiamo da zero — non vogliamo che un guest erediti la conv
    di un owner ne' viceversa.
    """
    state = CHATS.get(chat_id)
    desired = "owner" if is_owner else "guest"
    if state is None or state.get("role") != desired:
        state = {"role": desired}
        if desired == "owner":
            state["conversation_id"] = None
        else:
            state["history"] = []
        CHATS[chat_id] = state
    return state


# ── Handlers Telegram ───────────────────────────────────────────────────────

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    is_owner = _is_owner(user.id if user else None)
    name = (user.first_name if user else "") or ""
    role_text = "*owner* (puoi anche aggiungere/modificare)" if is_owner else "*guest* (solo lettura)"
    await update.message.reply_text(
        f"Ciao {name}! Sono Boardy.\nSei {role_text}.\n\n"
        "Scrivi una domanda sui giochi, le buste, le regole o la wishlist.\n\n"
        "Comandi:\n"
        "/new — nuova conversazione (azzera storico)\n"
        "/whoami — chi sei e con che ruolo\n"
        "/help — aiuto",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_new(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    CHATS.pop(chat_id, None)
    _save_state()
    await update.message.reply_text("Nuova conversazione: storico azzerato.")


async def cmd_whoami(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid = user.id if user else None
    is_owner = _is_owner(uid)
    role = "owner" if is_owner else "guest"
    await update.message.reply_text(
        f"Telegram user_id: `{uid}`\nRuolo Boardy: *{role}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Boardy via Telegram — chiedi della tua collezione, delle buste, "
        "delle regole, della wishlist.\n\n"
        "Comandi:\n"
        "/new — reset conversazione\n"
        "/whoami — il tuo user_id Telegram e il ruolo Boardy\n"
        "/help — questo messaggio\n\n"
        "Solo testo per ora (no audio)."
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    is_owner = _is_owner(user.id if user else None)
    state = _ensure_state(chat_id, is_owner)

    client: BoardyClient = ctx.application.bot_data["boardy"]

    # "sta scrivendo..." — feedback immediato, anche se la risposta tarda.
    # Telegram fa scadere l'azione dopo ~5s; con tool use lunghi (web_search +
    # 2-3 round LLM) si vedra' "scrivendo..." sparire e ricomparire al messaggio
    # finale. Accettabile: il messaggio finale arriva, ed e' l'unica garanzia.
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass  # action e' best-effort, non blocchiamo la chat per questo

    try:
        if is_owner:
            resp = await client.chat_owner(msg.text, state.get("conversation_id"))
            new_conv = resp.get("conversation_id")
            # /chat ritorna 0 per i guest; per gli owner aspettiamo un id > 0.
            if isinstance(new_conv, int) and new_conv > 0:
                state["conversation_id"] = new_conv
                _save_state()
        else:
            resp = await client.chat_guest(msg.text, state.get("history") or [])
            state["history"] = resp.get("history") or []
    except httpx.HTTPStatusError as e:
        LOG.exception("Boardy /chat HTTP %s", e.response.status_code)
        await msg.reply_text(
            f"Boardy ha risposto con HTTP {e.response.status_code}. Riprova fra poco."
        )
        return
    except Exception as e:
        LOG.exception("Errore chiamando Boardy")
        await msg.reply_text(f"Errore: {e}")
        return

    reply = (resp.get("reply") or "").strip() or "(nessuna risposta)"
    for chunk in _split(reply):
        # Markdown legacy (parse_mode=Markdown) e' piu' tollerante di MarkdownV2:
        # supporta **bold**, *italic*, `code`, [link](url) senza escape di ogni
        # carattere. Se Telegram lo rifiuta (es. un asterisco non bilanciato in
        # una risposta lunga), facciamo fallback a plain text — meglio una
        # risposta brutta che nessuna risposta.
        try:
            await msg.reply_text(
                chunk,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception:
            await msg.reply_text(chunk, disable_web_page_preview=True)


# ── App bootstrap ───────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Crea il BoardyClient nel loop dell'app (httpx async vuole un loop attivo)."""
    app.bot_data["boardy"] = BoardyClient(BOARDY_BASE_URL, OWNER_USERNAME, OWNER_PASSWORD)


async def _post_shutdown(app: Application) -> None:
    client: BoardyClient | None = app.bot_data.get("boardy")
    if client is not None:
        await client.close()


def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN non impostato. Crea un bot via @BotFather e mettilo in .env."
        )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _load_state()
    LOG.info(
        "Owner IDs: %s | Boardy: %s | state file: %s",
        OWNER_IDS or "(nessuno: tutti guest)",
        BOARDY_BASE_URL,
        _state_file(),
    )
    app = build_app()
    # drop_pending_updates: ignora messaggi accumulati mentre il bot era giu'.
    # Per un bot personale e' la scelta giusta: evita di processare in batch
    # cose stantie quando si riavvia.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
