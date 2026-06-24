"""Local-RAG over board-game rulebooks.

Pipeline:
- `ingest(game_name, pdf_path)` — extract text per page, chunk, embed, store.
- `search(game_name, query, k=5)` — embed query, top-k cosine over that game's chunks.

Embedding model: `intfloat/multilingual-e5-base` (~280MB; CPU-friendly; IT+EN).
First call downloads weights to the user's HuggingFace cache (~/.cache/huggingface/).

Embeddings stored as raw float32 bytes in `rulebook_chunks.embedding`. Brute-force
cosine similarity at query time — at our scale (≲ 10k chunks total) this is fine.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

import numpy as np
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

from . import audit
from .db import get_conn

EMBED_MODEL_NAME = "intfloat/multilingual-e5-base"
CHUNK_TARGET_TOKENS = 350           # ~ approx; 1 token ≈ 0.75 words for e5
CHUNK_OVERLAP_TOKENS = 60


# --- rulebook language gating -------------------------------------------------
# We only index rulebooks in languages DeepSeek (chat/Q&A) + e5 (embeddings)
# handle well: Latin-script Western-European. The filename suffix on 1j1ju is
# NOT a reliable language signal (it's a French site — a `-rules.pdf` can be
# French text), so we sniff the ACTUAL extracted text instead. Default set
# overridable via BOARDY_RULEBOOK_LANGS="EN,IT,ES,DE,FR,PT".
_DEFAULT_ALLOWED_LANGS = {"EN", "IT", "ES", "DE", "FR", "PT"}


def allowed_rulebook_langs() -> set[str]:
    raw = os.environ.get("BOARDY_RULEBOOK_LANGS")
    if not raw:
        return set(_DEFAULT_ALLOWED_LANGS)
    langs = {tok.strip().upper() for tok in raw.split(",") if tok.strip()}
    return langs or set(_DEFAULT_ALLOWED_LANGS)


def detect_language(text: str) -> str:
    """Best-effort language tag of `text` as an uppercase ISO-639-1 code
    ('EN','IT','DE','RU','ZH', …), or '?' when there's too little text to judge.

    Backed by py3langid (a deterministic, offline port of langid.py covering 97
    languages) — far more robust than hand-rolled stopwords, and the project
    already carries much heavier deps. We classify over ALL languages (not a
    forced subset) and let the caller check membership in `allowed_rulebook_langs`,
    so a Czech/Swedish/Japanese manual is correctly identified and rejected.
    """
    sample = (text or "").strip()
    if len(sample) < 40:        # a cover page or near-empty extraction — don't guess
        return "?"
    import py3langid
    lang, _score = py3langid.classify(sample[:10000])
    return (lang or "?").upper()


# Lingue latine europee OLTRE l'allowlist: e5+DeepSeek le gestiscono in modo
# imperfetto ma USABILE → un regolamento corretto in queste lingue è meglio di
# nessun regolamento, quindi passano con un WARNING invece di un hard-reject.
# Tutto il resto fuori dall'allowlist (script non latini: thai/CJK/cirillico/
# greco/arabo…) è inutilizzabile per il Q&A → hard-reject quando enforce_lang.
_SOFT_LANGS = {"NL", "PL", "CS", "SK", "SL", "HR", "RO", "HU", "FI", "SV",
               "DA", "NO", "IS", "ET", "LV", "LT", "CA", "GL", "EU", "AF", "TR"}


def classify_rulebook_language(text: str) -> dict:
    """Decide cosa fare della lingua di un regolamento estratto.

    Ritorna `{lang, allowed, soft, usable}`:
      - allowed: lingua nell'allowlist (EN/IT/ES/DE/FR/PT) → ottimale.
      - soft:    latina europea fuori allowlist (NL/PL/…) → usabile con warning.
      - usable:  allowed OR soft OR lang sconosciuta ('?') → NON hard-reject.
                 False solo per script chiaramente ingestibili (thai/CJK/…).
    Separa "in che lingua è" (game-agnostic) dalla POLICY (decisa dal chiamante
    in base a enforce_lang).
    """
    lang = detect_language(text)
    allowed = lang in allowed_rulebook_langs()
    soft = (not allowed) and lang in _SOFT_LANGS
    usable = allowed or soft or lang == "?"
    return {"lang": lang, "allowed": allowed, "soft": soft, "usable": usable}


def _game_name_in_text(canonical: str, pages: list[tuple[int, str]]) -> bool:
    """True se un token significativo del nome gioco compare nelle prime pagine.

    Segnale DEBOLE (solo warning, mai hard-reject): cattura il caso "manuale del
    gioco sbagliato" (es. un PDF di «Trails» indicizzato sotto «Barrage») quando
    il nome non compare affatto. Token < 4 char ignorati per evitare match a caso;
    se non restano token (nome cortissimo) ritorna True (non possiamo giudicare).
    """
    tokens = [w for w in _norm_name(canonical).split() if len(w) >= 4]
    if not tokens:
        return True
    sample = " ".join(t for _, t in pages[:3]).lower()
    return any(w in sample for w in tokens)

_model: SentenceTransformer | None = None


def _model_lazy() -> SentenceTransformer:
    """Load the embedding model once per process. ~3s startup on CPU."""
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    return _model


# --- text utilities -----------------------------------------------------------

def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _approx_tokens(s: str) -> int:
    # rough heuristic that matches e5 tokenizer well enough for chunking
    return max(1, len(s) // 4)


def _chunk_pages(pages: list[tuple[int, str]]) -> list[dict]:
    """Slide a window of ~CHUNK_TARGET_TOKENS over the concatenated pages.

    Each chunk records page_start/page_end so we can cite "p. 12–13" in answers.
    """
    chunks: list[dict] = []
    buf: list[tuple[int, str]] = []   # list of (page_no, line)
    buf_tokens = 0

    def flush() -> None:
        if not buf:
            return
        text = "\n".join(line for _, line in buf).strip()
        if not text:
            return
        chunks.append({
            "page_start": buf[0][0],
            "page_end": buf[-1][0],
            "text": text,
        })

    for page_no, page_text in pages:
        for line in page_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            tok = _approx_tokens(line)
            if buf_tokens + tok > CHUNK_TARGET_TOKENS and buf:
                flush()
                # carry over tail for overlap
                tail: list[tuple[int, str]] = []
                tail_tokens = 0
                for entry in reversed(buf):
                    if tail_tokens >= CHUNK_OVERLAP_TOKENS:
                        break
                    tail.insert(0, entry)
                    tail_tokens += _approx_tokens(entry[1])
                buf = tail
                buf_tokens = tail_tokens
            buf.append((page_no, line))
            buf_tokens += tok
    flush()
    return chunks


def _pages_from_reader(reader: PdfReader) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        out.append((i, _clean(text)))
    return out


def _read_pdf_pages(pdf_path: Path) -> list[tuple[int, str]]:
    return _pages_from_reader(PdfReader(str(pdf_path)))


# --- game-name resolution -----------------------------------------------------
# Rulebook tools are often called by the model with a slightly-off name (extra
# colon, different spacing). Resolve tolerantly so auto + manual flows don't
# fail on cosmetics: exact LOWER match first, then a normalized fallback.

def _norm_name(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for fuzzy name match."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _resolve_game(conn, name: str):
    """Return the games row matching `name` (exact LOWER, then normalized).

    Returns the sqlite Row (with id, name) or None. Normalized match only
    accepts an UNAMBIGUOUS hit (exactly one row) to avoid silent mis-targeting.
    """
    row = conn.execute(
        "SELECT id, name FROM games WHERE LOWER(name)=LOWER(?)", (name,)
    ).fetchone()
    if row:
        return row
    target = _norm_name(name)
    matches = [r for r in conn.execute("SELECT id, name FROM games").fetchall()
               if _norm_name(r["name"]) == target]
    return matches[0] if len(matches) == 1 else None


# --- embedding helpers --------------------------------------------------------

def _embed_passages(texts: list[str]) -> np.ndarray:
    """E5 expects 'passage: ' prefix for documents."""
    prefixed = [f"passage: {t}" for t in texts]
    arr = _model_lazy().encode(prefixed, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    return arr.astype(np.float32, copy=False)


def _embed_query(text: str) -> np.ndarray:
    arr = _model_lazy().encode([f"query: {text}"], normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    return arr[0].astype(np.float32, copy=False)


# --- public API ---------------------------------------------------------------

def _store_rulebook(
    conn,
    *,
    game_id: int,
    source: str,
    pages: list[tuple[int, str]],
    pdf_blob: bytes,
    ocr_report: str | None = None,
    game_label: str | None = None,
    actor: str | None = None,
) -> tuple[int, int]:
    """Chunk → embed → store a rulebook + its chunks. Returns (rulebook_id, n_chunks).

    Shared core for both PDF ingestion (`ingest_bytes`) and photo/OCR ingestion
    (`ingest_pages`): the only difference upstream is HOW `pages` were produced
    (pypdf text extraction vs. vision transcription). Re-ingesting the same
    `source` replaces the prior row via the DELETE below (UNIQUE(game_id, source_path)).
    Raises ValueError("no-chunks") if the pages produce 0 chunks.

    Audit: every successful store logs one `insert` row into `changes`
    (table `rulebooks`, `source`=`actor`) so rulebook downloads/uploads are
    traceable just like games/sleeve writes. A re-ingest (same source) is logged
    as `replace` since the DELETE above drops the prior row.
    """
    chunks = _chunk_pages(pages)
    if not chunks:
        raise ValueError("no-chunks")

    embeddings = _embed_passages([c["text"] for c in chunks])

    # Replace any prior rulebook for this game+source
    prior = conn.execute(
        "SELECT id FROM rulebooks WHERE game_id=? AND source_path=?", (game_id, source)
    ).fetchone()
    conn.execute("DELETE FROM rulebooks WHERE game_id=? AND source_path=?", (game_id, source))
    cur = conn.execute(
        """INSERT INTO rulebooks(game_id, source_path, page_count, embedding_model, pdf_blob, ocr_report)
           VALUES(?,?,?,?,?,?)""",
        (game_id, source, len(pages), EMBED_MODEL_NAME, pdf_blob, ocr_report),
    )
    rb_id = cur.lastrowid
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        conn.execute(
            """INSERT INTO rulebook_chunks(rulebook_id, chunk_index, page_start, page_end, text, embedding)
               VALUES(?,?,?,?,?,?)""",
            (rb_id, i, chunk["page_start"], chunk["page_end"], chunk["text"], emb.tobytes()),
        )

    audit.log_change(
        conn, table="rulebooks", row_id=rb_id, row_label=game_label,
        action="replace" if prior else "insert", field=None,
        old=({"rulebook_id": prior["id"]} if prior else None),
        new={"source_path": source, "pages": len(pages), "chunks": len(chunks)},
        source=actor,
    )
    return rb_id, len(chunks)


def ingest_bytes(game_name: str, data: bytes, *, source: str,
                 enforce_lang: bool = True, actor: str | None = None) -> dict:
    """Index a rulebook from raw PDF bytes; stores the PDF itself in the DB.

    `source` is a stable handle (file path or origin URL) used for dedup via
    UNIQUE(game_id, source_path): re-ingesting the same source replaces the row.
    The bytes are persisted in `rulebooks.pdf_blob` so boardy.db is the single
    source of truth (no on-disk PDF needed).

    `enforce_lang` (default True): hard-reject manuals in an UNUSABLE language
    (non-Latin script the LLM can't answer from — thai/CJK/…). Set False for
    user-confirmed flows (explicit download/upload): the unusable case becomes a
    warning instead, so the owner can override. Latin-script European languages
    outside the allowlist (NL/PL/…) are ALWAYS soft (warning, never rejected).
    Returns `warnings: list[str]` alongside the result so the chat can surface
    them. Note: this is the LANGUAGE/usability gate only — the GAME-MATCH
    (does the PDF belong to this game?) integrity check lives upstream in
    `download_rulebook` (deterministic, via bgg_id/source provenance).
    """
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        return {"error": f"could not parse PDF: {type(e).__name__}: {e}"}

    with get_conn() as conn:
        game = _resolve_game(conn, game_name)
        if not game:
            return {"error": f"game {game_name!r} not found in DB"}
        game_id, canonical = game["id"], game["name"]

        pages = _pages_from_reader(reader)
        if not any(t for _, t in pages):
            return {"error": "no extractable text in PDF (might be a scanned image — "
                             "carica delle foto del regolamento per l'OCR)"}

        warnings: list[str] = []

        # Language gate (the filename suffix lies — 1j1ju is French — so we sniff
        # the real text). allowed → ok; soft (Latin-EU) → warning; unusable
        # (non-Latin) → hard-reject only when enforce_lang, else warning.
        cls = classify_rulebook_language("\n".join(t for _, t in pages))
        if cls["soft"]:
            warnings.append(f"regolamento in {cls['lang']}: lingua non primaria, "
                            "le risposte potrebbero essere meno precise.")
        elif not cls["usable"]:
            if enforce_lang:
                return {
                    "error": (f"regolamento in lingua non utilizzabile ({cls['lang']}); "
                              f"Boardy indicizza bene solo {', '.join(sorted(allowed_rulebook_langs()))}. "
                              "Cerca un'altra edizione (EN consigliata) o carica le foto."),
                    "detected_lang": cls["lang"],
                    "allowed_langs": sorted(allowed_rulebook_langs()),
                }
            warnings.append(f"⚠️ regolamento in {cls['lang']} (lingua che e5/DeepSeek "
                            "gestiscono male): indicizzato su tua conferma, le regole "
                            "potrebbero non essere recuperabili bene.")

        # Soft game-match fallback: il testo non menziona il gioco → possibile
        # manuale sbagliato. Solo warning (la verifica forte è in download_rulebook).
        if not _game_name_in_text(canonical, pages):
            warnings.append(f"il testo del PDF non menziona «{canonical}» nelle prime "
                            "pagine — verifica che sia davvero il suo regolamento.")

        try:
            rb_id, n_chunks = _store_rulebook(
                conn, game_id=game_id, source=source, pages=pages, pdf_blob=data,
                game_label=canonical, actor=actor,
            )
        except ValueError:
            return {"error": "PDF parsed but produced 0 chunks"}
        conn.commit()
    return {
        "ok": True,
        "game": canonical,
        "rulebook_id": rb_id,
        "pages": len(pages),
        "chunks": n_chunks,
        "bytes": len(data),
        "model": EMBED_MODEL_NAME,
        "detected_lang": cls["lang"],
        "warnings": warnings,
    }


def ingest_pages(
    game_name: str,
    pages: list[tuple[int, str]],
    *,
    source: str,
    pdf_blob: bytes,
    ocr_report: str | None = None,
    actor: str | None = None,
) -> dict:
    """Index a rulebook from already-transcribed pages (e.g. photo OCR).

    Skips PDF text extraction: `pages` is a list of (page_number, text) already
    produced upstream (see `app/ocr.py` + the photo orchestration in tools).
    `pdf_blob` is a PDF assembled from the source photos so `get_pdf` keeps
    returning a re-exportable artifact. `ocr_report` is the JSON quality report
    persisted on the rulebook row. Same chunk/embed/store core as `ingest_bytes`.
    """
    pages = [(n, _clean(t)) for n, t in pages if t and t.strip()]
    if not pages:
        return {"error": "nessun testo leggibile estratto dalle foto"}

    with get_conn() as conn:
        game = _resolve_game(conn, game_name)
        if not game:
            return {"error": f"game {game_name!r} not found in DB"}
        game_id, canonical = game["id"], game["name"]

        try:
            rb_id, n_chunks = _store_rulebook(
                conn, game_id=game_id, source=source, pages=pages,
                pdf_blob=pdf_blob, ocr_report=ocr_report,
                game_label=canonical, actor=actor,
            )
        except ValueError:
            return {"error": "le foto sono state lette ma non hanno prodotto testo indicizzabile"}
        conn.commit()
    return {
        "ok": True,
        "game": canonical,
        "rulebook_id": rb_id,
        "pages": len(pages),
        "chunks": n_chunks,
        "bytes": len(pdf_blob),
        "model": EMBED_MODEL_NAME,
    }


def ingest(game_name: str, pdf_path: str, *, enforce_lang: bool = False,
           actor: str | None = None) -> dict:
    """Parse a local PDF rulebook and index it under the matching game.

    Thin wrapper over `ingest_bytes` — reads the file, then stores its bytes in
    the DB (the on-disk file is no longer the source of truth, just the input).
    `enforce_lang=False` by default: a local file is an explicit user choice, so
    an unusable language is a warning, not a hard reject.
    """
    p = Path(pdf_path).expanduser().resolve()
    if not p.exists():
        return {"error": f"file not found: {p}"}
    if not p.suffix.lower() == ".pdf":
        return {"error": "expected a .pdf file"}
    return ingest_bytes(game_name, p.read_bytes(), source=str(p),
                        enforce_lang=enforce_lang, actor=actor)


def get_pdf(game_name: str) -> tuple[str, bytes] | None:
    """Return (filename, pdf_bytes) for a game's most-recent stored rulebook, or None."""
    with get_conn() as conn:
        game = _resolve_game(conn, game_name)
        if not game:
            return None
        row = conn.execute(
            """SELECT pdf_blob FROM rulebooks
               WHERE game_id=? AND pdf_blob IS NOT NULL
               ORDER BY ingested_at DESC LIMIT 1""",
            (game["id"],),
        ).fetchone()
    if not row or row["pdf_blob"] is None:
        return None
    return (f"{_norm_name(game['name']).replace(' ', '-')}.pdf", bytes(row["pdf_blob"]))


def search(game_name: str, query: str, k: int = 5) -> list[dict]:
    """Top-k cosine-similar chunks for a given game."""
    with get_conn() as conn:
        game = _resolve_game(conn, game_name)
        if not game:
            return []
        rows = conn.execute(
            """SELECT c.id, c.page_start, c.page_end, c.text, c.embedding
               FROM rulebook_chunks c
               JOIN rulebooks rb ON rb.id = c.rulebook_id
               WHERE rb.game_id = ?""",
            (game["id"],),
        ).fetchall()
    if not rows:
        return []

    qv = _embed_query(query)
    sims = []
    for r in rows:
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        # both query and docs are L2-normalized, so dot == cosine
        sims.append((float(qv @ vec), r))
    sims.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in sims[:k]:
        out.append({
            "score": round(score, 4),
            "page_start": r["page_start"],
            "page_end": r["page_end"],
            "text": r["text"],
        })
    return out


def list_rulebooks() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT g.name AS game, rb.id, rb.source_path, rb.page_count,
                      rb.ingested_at, rb.embedding_model,
                      (SELECT COUNT(*) FROM rulebook_chunks WHERE rulebook_id=rb.id) AS chunks
               FROM rulebooks rb JOIN games g ON g.id = rb.game_id
               ORDER BY rb.ingested_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]
