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

import re
from pathlib import Path
from typing import Iterable

import numpy as np
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

from .db import get_conn

EMBED_MODEL_NAME = "intfloat/multilingual-e5-base"
CHUNK_TARGET_TOKENS = 350           # ~ approx; 1 token ≈ 0.75 words for e5
CHUNK_OVERLAP_TOKENS = 60

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


def _read_pdf_pages(pdf_path: Path) -> list[tuple[int, str]]:
    reader = PdfReader(str(pdf_path))
    out: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        out.append((i, _clean(text)))
    return out


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

def ingest(game_name: str, pdf_path: str) -> dict:
    """Parse a PDF rulebook and index it under the matching game."""
    p = Path(pdf_path).expanduser().resolve()
    if not p.exists():
        return {"error": f"file not found: {p}"}
    if not p.suffix.lower() == ".pdf":
        return {"error": "expected a .pdf file"}

    with get_conn() as conn:
        game = conn.execute("SELECT id FROM games WHERE LOWER(name)=LOWER(?)", (game_name,)).fetchone()
        if not game:
            return {"error": f"game {game_name!r} not found in DB"}
        game_id = game["id"]

        pages = _read_pdf_pages(p)
        if not any(t for _, t in pages):
            return {"error": "no extractable text in PDF (might be scanned image)"}

        chunks = _chunk_pages(pages)
        if not chunks:
            return {"error": "PDF parsed but produced 0 chunks"}

        embeddings = _embed_passages([c["text"] for c in chunks])

        # Replace any prior rulebook for this game+source
        conn.execute("DELETE FROM rulebooks WHERE game_id=? AND source_path=?", (game_id, str(p)))
        cur = conn.execute(
            """INSERT INTO rulebooks(game_id, source_path, page_count, embedding_model)
               VALUES(?,?,?,?)""",
            (game_id, str(p), len(pages), EMBED_MODEL_NAME),
        )
        rb_id = cur.lastrowid
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            conn.execute(
                """INSERT INTO rulebook_chunks(rulebook_id, chunk_index, page_start, page_end, text, embedding)
                   VALUES(?,?,?,?,?,?)""",
                (rb_id, i, chunk["page_start"], chunk["page_end"], chunk["text"], emb.tobytes()),
            )
        conn.commit()
    return {
        "ok": True,
        "game": game_name,
        "rulebook_id": rb_id,
        "pages": len(pages),
        "chunks": len(chunks),
        "model": EMBED_MODEL_NAME,
    }


def search(game_name: str, query: str, k: int = 5) -> list[dict]:
    """Top-k cosine-similar chunks for a given game."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.page_start, c.page_end, c.text, c.embedding
               FROM rulebook_chunks c
               JOIN rulebooks rb ON rb.id = c.rulebook_id
               JOIN games g ON g.id = rb.game_id
               WHERE LOWER(g.name) = LOWER(?)""",
            (game_name,),
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
