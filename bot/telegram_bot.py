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
  guest mode: solo read tools, conversazione persistita lato Boardy con
  metadata Telegram.

Stato:
- Map chat_id Telegram → conversation_id Boardy + ruolo: persistita su disco
  (`<data_dir>/telegram_chats.json`) cosi' un restart del bot non perde
  la conversazione in corso. Vale sia per owner sia per guest Telegram.
- /new svuota la mappatura e l'history del singolo chat_id.

Run:
  uv run python -m bot.telegram_bot
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.tg_format import html_to_plain, to_telegram

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

# File di persistenza della mappatura chat_id → conv_id Boardy.
# Convenzione: stessa dir del DB Boardy (BOARDY_DB env in Docker, altrimenti
# repo-relative ./data/). Cosi' in Docker il file vive sul named volume.
def _state_file() -> Path:
    boardy_db = os.environ.get("BOARDY_DB")
    if boardy_db:
        return Path(boardy_db).parent / "telegram_chats.json"
    return Path(__file__).resolve().parent.parent / "data" / "telegram_chats.json"


# ── Helpers ─────────────────────────────────────────────────────────────────

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

    async def chat_owner(
        self,
        message: str,
        conversation_id: int | None,
        *,
        actor_id: int | None,
        actor_name: str | None,
    ) -> dict[str, Any]:
        if not self._logged_in:
            await self._login()
        payload: dict[str, Any] = {
            "message": message,
            "client_origin": "telegram",
            "client_actor_id": str(actor_id) if actor_id is not None else None,
            "client_actor_name": actor_name,
        }
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

    async def upload_photos_owner(self, game_name: str, images: list[bytes]) -> dict[str, Any]:
        """Manda le foto di un regolamento a /rulebooks/upload-photos (owner-only).

        Multipart con campo ripetuto `files`. Timeout lungo: l'OCR fa ~1 chiamata
        Gemini per foto, quindi un regolamento intero può richiedere parecchi secondi.
        Re-login singolo se il cookie è scaduto (come chat_owner).
        """
        if not self._logged_in:
            await self._login()

        def _build():
            return [("files", (f"page{i:02d}.jpg", img, "image/jpeg"))
                    for i, img in enumerate(images)]

        data = {"game_name": game_name}
        r = await self._owner_client.post(
            "/rulebooks/upload-photos", data=data, files=_build(), timeout=600.0,
        )
        if r.status_code == 401:
            LOG.warning("401 da /rulebooks/upload-photos → re-login")
            self._logged_in = False
            await self._login()
            r = await self._owner_client.post(
                "/rulebooks/upload-photos", data=data, files=_build(), timeout=600.0,
            )
        r.raise_for_status()
        return r.json()

    async def chat_guest(
        self,
        message: str,
        conversation_id: int | None,
        *,
        actor_id: int | None,
        actor_name: str | None,
    ) -> dict[str, Any]:
        # Client one-shot SENZA cookie: zero chance di portarsi dietro
        # accidentalmente l'auth dell'owner.
        payload: dict[str, Any] = {
            "message": message,
            "persist_guest": True,
            "guest_origin": "telegram",
            "guest_actor_id": str(actor_id) if actor_id is not None else None,
            "guest_actor_name": actor_name,
        }
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120.0) as guest:
            r = await guest.post("/chat", json=payload)
        r.raise_for_status()
        return r.json()


# ── Stato in-memory per chat_id ─────────────────────────────────────────────
# Schema:
#   owner → {"role": "owner",  "conversation_id": int | None}
#   guest → {"role": "guest",  "conversation_id": int | None}
# Entrambi vengono scritti su disco. Il testo vive nella tabella `conversations`
# di Boardy; il file locale contiene solo la mappatura Telegram chat_id → conv_id.

CHATS: dict[int, dict[str, Any]] = {}


def _load_state() -> None:
    """Carica la mappatura Telegram chat_id → conv_id da disco.

    Backward compatible col vecchio formato:
      {"613987510": 30}
    Nuovo formato:
      {"613987510": {"role": "owner", "conversation_id": 30}}
    """
    p = _state_file()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        LOG.warning("State file %s illeggibile (%s), riparto vuoto", p, e)
        return
    for chat_id_s, raw_state in (data or {}).items():
        try:
            chat_id = int(chat_id_s)
            if isinstance(raw_state, dict):
                role = raw_state.get("role")
                conv_id = raw_state.get("conversation_id")
                if role not in {"owner", "guest"}:
                    continue
                CHATS[chat_id] = {"role": role, "conversation_id": int(conv_id)}
            else:
                CHATS[chat_id] = {"role": "owner", "conversation_id": int(raw_state)}
        except (TypeError, ValueError):
            continue
    LOG.info("Ripristinate %d conversazioni Telegram da %s", len(CHATS), p)


def _save_state() -> None:
    """Scrive su disco le conv Telegram attive. Best-effort."""
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        str(chat_id): {
            "role": state.get("role"),
            "conversation_id": state.get("conversation_id"),
        }
        for chat_id, state in CHATS.items()
        if state.get("role") in {"owner", "guest"} and state.get("conversation_id") is not None
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
        state["conversation_id"] = None
        CHATS[chat_id] = state
    return state


# ── Bottoni / tastiera rapida ────────────────────────────────────────────────
# Etichette dei bottoni della tastiera persistente (MAIN_KB). I tap arrivano
# come normali messaggi di testo → li intercettiamo in on_message e li
# instradiamo all'azione giusta.
BTN_NEW = "🆕 Nuova chat"
BTN_GAMES = "🎲 I miei giochi"
BTN_SLEEVES = "📦 Riepilogo buste"
BTN_HELP = "ℹ️ Aiuto"


def _main_kb(is_owner: bool) -> ReplyKeyboardMarkup:
    """Tastiera rapida sotto la casella di testo.

    resize_keyboard la rende compatta; resta visibile finche' non viene
    sostituita. "Riepilogo buste" e' un bottone riservato agli owner.
    """
    second_row = [BTN_GAMES, BTN_SLEEVES] if is_owner else [BTN_GAMES]
    return ReplyKeyboardMarkup(
        [[BTN_NEW], second_row, [BTN_HELP]],
        resize_keyboard=True,
    )


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
        "/help — aiuto\n\n"
        "Oppure usa i bottoni qui sotto 👇",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_kb(is_owner),
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
    # Plain text di proposito: "user_id" contiene un underscore che il parser
    # legacy Markdown interpreta come inizio corsivo → BadRequest "can't find
    # end of the entity". Qui non serve formattazione, mandiamo testo nudo.
    await update.message.reply_text(
        f"Telegram user_id: {uid}\nRuolo Boardy: {role}"
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    is_owner = _is_owner(user.id if user else None)
    lines = [
        "Sono <b>Boardy</b>, il tuo assistente per la collezione di giochi da "
        "tavolo. Scrivimi in linguaggio naturale, per esempio:",
        "",
        "• <b>Info su un gioco</b> — “quanto dura Wingspan?”, “in quanti si gioca a Catan?”",
        "• <b>Cerca nella collezione</b> — “che giochi cooperativi ho?”, “qualcosa di veloce per 2”",
        "• <b>Regole</b> — “come si vince ad Azul?”, “il setup di Dune Imperium”",
        "• <b>Buste</b> — “che buste servono per 7 Wonders?”, “cosa devo ancora imbustare?”",
        "• <b>Wishlist</b> — “cosa ho in wishlist?”",
    ]
    if is_owner:
        lines += [
            "",
            "Essendo <b>owner</b>, puoi anche modificare la collezione:",
            "• <b>Aggiungere giochi</b> — “ho comprato Wingspan” (recupero dati e buste in automatico)",
            "• <b>Registrare buste</b> — “ho preso 100 buste 63.5x88”",
            "• <b>Aggiornare</b> — “segna Azul come imbustato”, “togli X dalla wishlist”",
            "• <b>Regolamento da foto</b> — fotografa le pagine e mandamele con didascalia "
            "“regolamento di &lt;gioco&gt;”, oppure usa /regolamento &lt;gioco&gt; e poi le foto. "
            "Le leggo (OCR) e ti avviso se qualche pagina è poco chiara o se ne manca qualcuna.",
        ]
    lines += [
        "",
        "Comandi: /new (nuova conversazione), /whoami (chi sei), /help. "
        "Oppure usa i bottoni qui sotto 👇",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_kb(is_owner),
        disable_web_page_preview=True,
    )


# ── Foto del regolamento → OCR ───────────────────────────────────────────────
# L'utente fotografa le pagine di un regolamento fisico e le manda al bot. Le foto
# vengono lette da un modello di visione lato Boardy (DeepSeek non legge immagini)
# e indicizzate come un regolamento qualsiasi. Solo owner: è una scrittura sul DB.
#
# Telegram manda gli album come update SEPARATI con lo stesso `media_group_id`:
# li bufferizziamo per gruppo e li elaboriamo insieme dopo un breve debounce
# (niente job-queue: usiamo un task asyncio per non aggiungere dipendenze).

PHOTO_DEBOUNCE_S = 2.5

# Buffer album in corso: key f"{chat_id}:{media_group_id}" → dati raccolti.
_ALBUMS: dict[str, dict[str, Any]] = {}
# Sessione di scansione avviata con /regolamento <gioco>: chat_id → nome gioco.
_SCAN_GAME: dict[int, str] = {}
# Foto ricevute senza sapere il gioco: chat_id → lista file_id (in attesa di /regolamento).
_PENDING_PHOTOS: dict[int, list[str]] = {}

# Prefissi che l'utente usa naturalmente: "questo è il regolamento di X",
# "regole di X", "manuale di X". Cattura il nome gioco dopo il connettore.
_CAPTION_RE = re.compile(
    r"(?:regolamento|regole|manuale|rule[s]?|rulebook)\s+(?:di|del|della|dello|dei|of|for)\s+(.+)",
    re.IGNORECASE,
)


def _game_from_caption(caption: str | None) -> str | None:
    """Estrae il nome del gioco dalla didascalia delle foto, se presente."""
    if not caption:
        return None
    c = caption.strip()
    m = _CAPTION_RE.search(c)
    if m:
        return m.group(1).strip(" .!?\"'»«")
    # Didascalia breve senza connettori → trattala come nome del gioco.
    if 0 < len(c) <= 60 and "\n" not in c:
        return c
    return None


def _format_photo_report(data: dict[str, Any]) -> str:
    """Costruisce il messaggio di esito (pagine lette + avvisi) in Markdown."""
    lines = [
        f"📕 **{data.get('game', '?')}** indicizzato — "
        f"{data.get('pages', '?')} pagine, {data.get('chunks', '?')} chunk."
    ]
    warnings = data.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("⚠️ **Da controllare:**")
        lines += [f"- {w}" for w in warnings]
    lines.append("")
    lines.append("Ora puoi farmi domande sulle regole di questo gioco.")
    return "\n".join(lines)


async def _send_report(msg, data: dict[str, Any]) -> None:
    for chunk in to_telegram(_format_photo_report(data)) or ["(ok)"]:
        try:
            await msg.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            await msg.reply_text(html_to_plain(chunk), disable_web_page_preview=True)


async def _run_ocr(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, game: str,
                   file_ids: list[str], reply_msg) -> None:
    """Scarica le foto, le manda a Boardy per l'OCR e risponde con l'esito."""
    client: BoardyClient = ctx.application.bot_data["boardy"]
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    images: list[bytes] = []
    for fid in file_ids:
        try:
            tg_file = await ctx.bot.get_file(fid)
            images.append(bytes(await tg_file.download_as_bytearray()))
        except Exception:
            LOG.exception("Download foto %s fallito", fid)

    if not images:
        await reply_msg.reply_text("Non sono riuscito a scaricare le foto. Riprova.")
        return

    await reply_msg.reply_text(
        f"Sto leggendo {len(images)} foto del regolamento di «{game}»… "
        "(può richiedere qualche secondo)"
    )
    try:
        data = await client.upload_photos_owner(game, images)
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        if e.response.status_code == 400 and detail:
            await reply_msg.reply_text(
                f"Non sono riuscito a indicizzare: {detail}\n\n"
                "Se è il nome del gioco, riprova con: /regolamento <nome esatto del gioco>"
            )
        else:
            await reply_msg.reply_text(f"Boardy ha risposto HTTP {e.response.status_code}. Riprova fra poco.")
        return
    except Exception as e:
        LOG.exception("Errore OCR foto regolamento")
        await reply_msg.reply_text(f"Errore durante la lettura: {e}")
        return

    await _send_report(reply_msg, data)


async def _flush_album(ctx: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    """Dopo il debounce, elabora l'album bufferizzato per `key`."""
    try:
        await asyncio.sleep(PHOTO_DEBOUNCE_S)
    except asyncio.CancelledError:
        return
    buf = _ALBUMS.pop(key, None)
    if not buf or not buf["file_ids"]:
        return

    chat_id = buf["chat_id"]
    reply_msg = buf["reply_msg"]
    game = _SCAN_GAME.get(chat_id) or _game_from_caption(buf.get("caption"))
    if not game:
        # Non sappiamo il gioco: parcheggia le foto e chiedi.
        _PENDING_PHOTOS[chat_id] = buf["file_ids"]
        await reply_msg.reply_text(
            f"Ho ricevuto {len(buf['file_ids'])} foto ma non so di che gioco è.\n"
            "Scrivimi: /regolamento <nome del gioco> (le elaboro subito), "
            "oppure rimanda le foto con didascalia «regolamento di <gioco>»."
        )
        return
    await _run_ocr(ctx, chat_id, game, buf["file_ids"], reply_msg)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Riceve foto / immagini-documento e le bufferizza per album."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None:
        return
    if not _is_owner(user.id if user else None):
        await msg.reply_text("Le foto dei regolamenti può caricarle solo l'owner.")
        return

    file_id: str | None = None
    if msg.photo:
        file_id = msg.photo[-1].file_id          # la PhotoSize più grande
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    if not file_id:
        return

    chat_id = update.effective_chat.id
    mgid = msg.media_group_id or f"single:{msg.message_id}"
    key = f"{chat_id}:{mgid}"
    buf = _ALBUMS.get(key)
    if buf is None:
        buf = {"file_ids": [], "caption": None, "task": None,
               "chat_id": chat_id, "reply_msg": msg}
        _ALBUMS[key] = buf
    buf["file_ids"].append(file_id)
    if msg.caption and not buf["caption"]:
        buf["caption"] = msg.caption

    # (ri)avvia il debounce: ogni nuova foto dell'album rimanda il flush.
    if buf["task"]:
        buf["task"].cancel()
    buf["task"] = asyncio.create_task(_flush_album(ctx, key))


async def cmd_regolamento(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/regolamento <gioco>`: avvia una scansione e/o elabora foto in attesa."""
    msg = update.effective_message
    user = update.effective_user
    chat_id = update.effective_chat.id
    if not _is_owner(user.id if user else None):
        await msg.reply_text("Solo l'owner può caricare i regolamenti.")
        return
    game = " ".join(ctx.args).strip() if ctx.args else ""
    if not game:
        await msg.reply_text(
            "Uso: /regolamento <nome del gioco>, poi mandami le foto delle pagine.\n"
            "Quando hai finito scrivi /fine."
        )
        return
    _SCAN_GAME[chat_id] = game
    pending = _PENDING_PHOTOS.pop(chat_id, None)
    if pending:
        await _run_ocr(ctx, chat_id, game, pending, msg)
    else:
        await msg.reply_text(
            f"Ok! Mandami le foto del regolamento di «{game}» (anche più album). "
            "Le leggo automaticamente. Scrivi /fine quando hai finito."
        )


async def cmd_fine(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Chiude la sessione di scansione regolamento aperta con /regolamento."""
    chat_id = update.effective_chat.id
    had = _SCAN_GAME.pop(chat_id, None)
    _PENDING_PHOTOS.pop(chat_id, None)
    if had:
        await update.message.reply_text(f"Scansione del regolamento di «{had}» chiusa.")
    else:
        await update.message.reply_text("Nessuna scansione in corso.")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not msg.text:
        return

    # Tap sui bottoni della tastiera rapida: arrivano come testo. Quelli che
    # corrispondono a un'azione li instradiamo; "I miei giochi" diventa una
    # query normale verso Boardy.
    text = msg.text
    if text == BTN_NEW:
        return await cmd_new(update, ctx)
    if text == BTN_HELP:
        return await cmd_help(update, ctx)
    if text == BTN_GAMES:
        text = "che giochi ho?"
    elif text == BTN_SLEEVES:
        text = ("fammi un riepilogo delle buste: quante e quali servono ancora, "
                "cosa è da imbustare e cosa è già imbustato")

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
        actor_name = None
        actor_id = None
        if user is not None:
            actor_id = user.id
            actor_name = user.username or user.full_name or user.first_name

        if is_owner:
            resp = await client.chat_owner(
                text,
                state.get("conversation_id"),
                actor_id=actor_id,
                actor_name=actor_name,
            )
            new_conv = resp.get("conversation_id")
            # /chat ritorna 0 per i guest; per gli owner aspettiamo un id > 0.
            if isinstance(new_conv, int) and new_conv > 0:
                state["conversation_id"] = new_conv
                _save_state()
        else:
            resp = await client.chat_guest(
                text,
                state.get("conversation_id"),
                actor_id=actor_id,
                actor_name=actor_name,
            )
            new_conv = resp.get("conversation_id")
            if isinstance(new_conv, int) and new_conv > 0:
                state["conversation_id"] = new_conv
                _save_state()
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
    # to_telegram() converte il Markdown di Boardy (tabelle incluse) in HTML
    # Telegram, gia' spezzato su confini di riga (<=4096, mai a meta' tag).
    # Vedi bot/tg_format.py per il perche'.
    chunks = to_telegram(reply) or ["(nessuna risposta)"]
    for chunk in chunks:
        # parse_mode=HTML: entita' come tag espliciti → robusto con `_`/`*` nel
        # testo. Fallback a plain text (tag rimossi) se Telegram rifiuta comunque.
        try:
            await msg.reply_text(
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            await msg.reply_text(html_to_plain(chunk), disable_web_page_preview=True)


# ── App bootstrap ───────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Crea il BoardyClient nel loop dell'app (httpx async vuole un loop attivo)."""
    app.bot_data["boardy"] = BoardyClient(BOARDY_BASE_URL, OWNER_USERNAME, OWNER_PASSWORD)
    # Menu comandi "/" (il pulsante Menu accanto alla casella di testo su Telegram).
    await app.bot.set_my_commands([
        BotCommand("new", "Nuova conversazione (azzera lo storico)"),
        BotCommand("regolamento", "Leggi un regolamento dalle foto (owner)"),
        BotCommand("fine", "Chiudi la scansione del regolamento"),
        BotCommand("whoami", "Chi sei e con che ruolo"),
        BotCommand("help", "Aiuto"),
    ])


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
    app.add_handler(CommandHandler("regolamento", cmd_regolamento))
    app.add_handler(CommandHandler("fine", cmd_fine))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("help", cmd_help))
    # Foto delle pagine del regolamento (anche inviate come immagine-documento).
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
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
