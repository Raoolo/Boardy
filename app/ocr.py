"""Vision OCR for rulebook photos — Google Gemini (one structured call per image).

Why Gemini and not DeepSeek: DeepSeek's API (`deepseek-chat`) is text-only — it
cannot read images. So the photo→text step uses a vision model. DeepSeek stays the
brain for everything else (chat, Q&A, tags, titles, library filter); Gemini is used
ONLY here. See `app/llm.py` for the main provider abstraction.

Pipeline role: `transcribe_images(images, game_name)` returns one structured result
per photo (transcribed Markdown text + a prose description of diagrams/icons, the
printed page number, a legibility flag and any issues). The photo orchestration in
`app/tools.py` then orders the pages, detects gaps, and feeds the text into the same
RAG ingest as PDFs (`rulebooks.ingest_pages`).

Config:
- `GEMINI_API_KEY`  — required (Google AI Studio key, https://aistudio.google.com/apikey).
- `GEMINI_OCR_MODEL` — optional override, default `gemini-2.5-flash`.

Cost: a few cents for a whole rulebook (~one cheap Flash call per photo).
"""
from __future__ import annotations

import os
import time

from pydantic import BaseModel

DEFAULT_MODEL = "gemini-2.5-flash"

# Gemini può rispondere 503/UNAVAILABLE (sovraccarico) o 429 (rate limit) in modo
# TEMPORANEO. Ritentiamo con backoff così un singolo intoppo non segna una pagina
# come illeggibile per sbaglio. Solo errori transitori — un 400/401 (chiave/input)
# fallisce subito.
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 4.0
_TRANSIENT_MARKERS = ("503", "429", "500", "502", "504", "unavailable",
                      "overloaded", "high demand", "resource_exhausted", "deadline")


def _is_transient(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


# Rate-limit / quota exhaustion (429 RESOURCE_EXHAUSTED) on the free tier does NOT
# recover within a request — retrying every page is just slow. Detect it so the
# caller can bail early and tell the user "riprova più tardi" instead of grinding
# through N pages × retries × backoff.
_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "quota", "rate limit", "rate_limit")


def _is_rate_limit(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _RATE_LIMIT_MARKERS)

# One image at a time keeps page-number detection and the per-page legibility
# verdict unambiguous (batching multiple pages in one call blurs both).
_PROMPT = (
    "Sei un trascrittore esperto di regolamenti di giochi da tavolo. "
    "Questa immagine è la FOTO di una pagina del regolamento del gioco «{game}».\n\n"
    "Trascrivi fedelmente TUTTO il contenuto della pagina in Markdown:\n"
    "- riporta il testo così com'è, mantenendo titoli, elenchi e l'ordine di lettura "
    "(colonne dall'alto in basso, da sinistra a destra);\n"
    "- per ogni icona, simbolo, diagramma di setup, tabella o illustrazione con valore "
    "di regole, aggiungi una DESCRIZIONE a parole tra parentesi quadre, es. "
    "«[Diagramma: la plancia va posizionata al centro con 3 segnalini sopra]», così che "
    "il significato resti cercabile anche senza vedere l'immagine;\n"
    "- NON inventare e NON completare parti che non riesci a leggere: se una porzione è "
    "illeggibile, scrivi «[illeggibile]» e segnalalo in `issues`.\n\n"
    "Campi da restituire:\n"
    "- `text_markdown`: la trascrizione completa (stringa vuota se la pagina è illeggibile);\n"
    "- `page_number_printed`: il numero di pagina STAMPATO sulla pagina, se visibile, "
    "altrimenti null (NON inventarlo);\n"
    "- `legibility`: \"ok\" se leggibile bene, \"poor\" se sfocata/riflessi/troppo storta;\n"
    "- `issues`: lista breve di problemi (in italiano), vuota se nessuno;\n"
    "- `looks_like_rulebook_page`: false se la foto non sembra una pagina di regolamento."
)


class PageOCR(BaseModel):
    """Structured result for a single rulebook photo."""
    text_markdown: str
    page_number_printed: int | None = None
    legibility: str = "ok"           # "ok" | "poor"
    issues: list[str] = []
    looks_like_rulebook_page: bool = True


def _client():
    """Build the Gemini client; raises a clear error if the key is missing."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY non configurata: serve per leggere le foto dei regolamenti "
            "(crea una chiave gratuita su https://aistudio.google.com/apikey e mettila nel .env)."
        )
    # Imported lazily so the rest of Boardy runs even without google-genai installed.
    from google import genai
    return genai.Client(api_key=api_key)


def _mime_for(image: bytes) -> str:
    """Sniff a image MIME type from magic bytes; default to JPEG."""
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def transcribe_images(images: list[bytes], game_name: str, *,
                      stop_on_rate_limit: bool = False) -> list[dict]:
    """OCR each photo via Gemini. Returns one dict per image, in input order.

    Each dict: {order, text_markdown, page_number_printed, legibility, issues,
    looks_like_rulebook_page, error?}. A per-image failure is captured in `error`
    rather than aborting the whole batch, so one bad photo doesn't lose the rest.

    `stop_on_rate_limit`: if a page fails with a quota/rate-limit error (429), stop
    processing the rest (they'd fail too) and mark them skipped. Used by the PDF
    auto-OCR path so a dead free-tier quota fails in seconds, not minutes. Photo
    uploads leave it False (fewer pages, worth attempting each).
    """
    from google.genai import types

    client = _client()
    model = os.environ.get("GEMINI_OCR_MODEL", DEFAULT_MODEL)
    prompt = _PROMPT.format(game=game_name)

    results: list[dict] = []
    rate_limited = False
    for i, image in enumerate(images):
        entry: dict = {"order": i}
        if rate_limited:
            # A prior page hit the quota wall; don't bother calling for the rest.
            entry.update({
                "text_markdown": "", "page_number_printed": None,
                "legibility": "poor", "issues": ["saltata: quota OCR esaurita"],
                "looks_like_rulebook_page": True,
                "error": "skipped: rate limit", "rate_limited": True,
            })
            results.append(entry)
            continue
        last_err: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=image, mime_type=_mime_for(image)),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=PageOCR,
                        temperature=0,
                    ),
                )
                page = resp.parsed if isinstance(resp.parsed, PageOCR) else PageOCR.model_validate_json(resp.text)
                entry.update(page.model_dump())
                last_err = None
                break
            except Exception as e:  # noqa: BLE001 — surface per-page, keep going
                last_err = e
                # 429/quota won't clear within the request → fail fast (no backoff
                # retries). 503/overload is worth retrying.
                if (_is_transient(e) and not _is_rate_limit(e)
                        and attempt < _MAX_RETRIES - 1):
                    time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                break
        if last_err is not None:
            is_rl = _is_rate_limit(last_err)
            entry.update({
                "text_markdown": "",
                "page_number_printed": None,
                "legibility": "poor",
                "issues": [f"errore di lettura: {type(last_err).__name__}"],
                "looks_like_rulebook_page": True,
                "error": str(last_err),
                "rate_limited": is_rl,
            })
            if is_rl and stop_on_rate_limit:
                rate_limited = True  # skip the remaining pages fast
        results.append(entry)
    return results
