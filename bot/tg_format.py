"""Conversione da Markdown (output di Boardy, pensato per la UI web) al
formato adatto a Telegram.

Perche' serve: Boardy risponde in Markdown con **tabelle a `|`** (le rende la
UI web via marked.js). Telegram NON renderizza le tabelle Markdown — le mostra
come testo grezzo con le barrette — e il suo parser Markdown legacy e' fragile:
un `*`/`_` non bilanciato (o un taglio del messaggio a 4096 char in mezzo a un
`**...**`) fa fallire l'intero invio. Qui risolviamo:

- le **tabelle** diventano una *lista verticale* (una voce per blocco), molto
  piu' leggibile su telefono;
- il resto del testo viene convertito in **HTML di Telegram** (<b>/<i>/<code>/<a>);
- l'output e' gia' spezzato in messaggi <= 4096 char su **confini di riga**,
  cosi' non si spezza mai un tag a meta'.

Usiamo `parse_mode=HTML` (non Markdown): le entita' sono tag espliciti, quindi
underscore/asterischi nel CONTENUTO (es. "to_sleeve") non rompono nulla.
"""
from __future__ import annotations

import html
import re

TG_MSG_LIMIT = 4096

# Mappa header-tabella -> icona, per la riga di dettaglio della lista verticale.
# Match case-insensitive su sottostringa: "Giocatori"/"Gioc"/"Players" -> 👥.
_HEADER_ICONS: list[tuple[tuple[str, ...], str]] = [
    (("gioc", "player"), "👥"),
    (("durat", "tempo"), "⏱"),
    (("compless", "peso", "weight"), "🧩"),
    (("voto", "bgg", "rating", "valutaz"), "⭐"),
    (("anno", "year"), "📅"),
    (("imbust", "sleev", "buste"), "🎴"),
    (("prezzo", "costo", "price"), "💶"),
    (("priorit",), "🔝"),
]

# Header che indicano una colonna-indice (numero di riga): la usiamo come
# prefisso "N." invece che come dettaglio.
_INDEX_HEADERS = {"", "#", "n", "n.", "nr", "num", "no", "idx"}

_EMPTY_VALUES = {"-", "–", "—", "n/a", "na", ""}


def _icon_for(header: str) -> str | None:
    h = header.strip().lower()
    for keys, icon in _HEADER_ICONS:
        if any(k in h for k in keys):
            return icon
    return None


# ── Inline Markdown -> HTML Telegram ─────────────────────────────────────────

def _inline(md: str) -> str:
    """Converte il markdown inline di un frammento in HTML Telegram.

    Escapa PRIMA i caratteri HTML, poi inserisce i tag: cosi' i `<`/`>`/`&`
    presenti nel contenuto non possono generare tag spuri.
    """
    s = html.escape(md, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)              # code span
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)  # link
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)              # **bold**
    s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)                  # __bold__
    # *italic* singolo (dopo aver consumato i **): niente _italic_ per non
    # rompere identificatori tipo to_sleeve.
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    return s


def _strip_md(md: str) -> str:
    """Testo (HTML-escaped) senza marcatori bold/italic/code/link."""
    s = _inline(md)
    s = re.sub(r"</?(?:b|i|code)>", "", s)
    s = re.sub(r'<a href="[^"]*">([^<]*)</a>', r"\1", s)
    return s.strip()


# ── Tabelle ──────────────────────────────────────────────────────────────────

_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def _is_row(line: str) -> bool:
    return _ROW_RE.match(line) is not None


def _is_sep(line: str) -> bool:
    """Riga separatore di una tabella markdown: |---|:--:|---|."""
    m = _ROW_RE.match(line)
    if not m:
        return False
    return bool(re.fullmatch(r"[\s:|-]+", m.group(0))) and "-" in line


def _cells(line: str) -> list[str]:
    return [c.strip() for c in _ROW_RE.match(line).group(1).split("|")]


def _clean_value(header: str, val: str) -> str:
    h = header.lower()
    v = val
    if any(k in h for k in ("compless", "peso", "weight")):
        v = re.sub(r"^\s*\d+\.\s*", "", v)        # "3. Medio (2.48)" -> "Medio (2.48)"
        v = re.sub(r"\s*\([^)]*\)\s*$", "", v)     # -> "Medio"
    return v.strip()


def _convert_table(lines: list[str]) -> str:
    rows = [_cells(l) for l in lines if not _is_sep(l)]
    if len(rows) < 2:
        return ""
    header = rows[0]
    data = rows[1:]
    ncol = len(header)
    has_index = bool(header) and header[0].strip().lower() in _INDEX_HEADERS
    title_col = 1 if (has_index and ncol > 1) else 0

    out: list[str] = []
    for r in data:
        r = r + [""] * (ncol - len(r))            # pad righe corte
        num = r[0].strip() if has_index else ""
        title = _strip_md(r[title_col]) or "(senza nome)"
        prefix = f"{num}. " if num else "• "
        out.append(f"{prefix}<b>{title}</b>")

        details: list[str] = []
        for i, h in enumerate(header):
            if i == title_col or (has_index and i == 0):
                continue
            raw = r[i].strip()
            if raw.lower() in _EMPTY_VALUES:
                continue
            val = _inline(_clean_value(h, raw))
            icon = _icon_for(h)
            details.append(f"{icon} {val}" if icon else f"{_inline(h.strip())}: {val}")
        if details:
            out.append("   " + " · ".join(details))
        out.append("")                            # riga vuota tra le voci
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# ── Testo non-tabellare ──────────────────────────────────────────────────────

def _convert_text_block(text: str) -> str:
    out: list[str] = []
    for ln in text.split("\n"):
        m = re.match(r"^\s*#{1,6}\s+(.*)$", ln)
        if m:
            out.append(f"<b>{_strip_md(m.group(1))}</b>")
            continue
        ln = re.sub(r"^(\s*)[-*]\s+", r"\1• ", ln)  # bullet "- "/"* " -> "• "
        out.append(_inline(ln))
    return "\n".join(out)


# ── Splitter su confini di riga ──────────────────────────────────────────────

def _split_lines(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text] if text.strip() else []
    chunks: list[str] = []
    cur = ""
    for ln in text.split("\n"):
        while len(ln) > limit:                      # riga singola enorme (raro)
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:limit])
            ln = ln[limit:]
        add = ln if not cur else "\n" + ln
        if len(cur) + len(add) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur += add
    if cur:
        chunks.append(cur)
    return chunks


# ── API pubblica ─────────────────────────────────────────────────────────────

def to_telegram(md: str) -> list[str]:
    """Reply Markdown di Boardy -> lista di messaggi HTML pronti per Telegram."""
    lines = md.split("\n")
    blocks: list[str] = []
    buf: list[str] = []
    i = 0

    def flush() -> None:
        if buf:
            txt = "\n".join(buf).strip("\n")
            if txt.strip():
                blocks.append(_convert_text_block(txt))
            buf.clear()

    while i < len(lines):
        line = lines[i]
        if _is_row(line) and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            flush()
            tbl = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and _is_row(lines[i]):
                tbl.append(lines[i])
                i += 1
            block = _convert_table(tbl)
            if block:
                blocks.append(block)
        else:
            buf.append(line)
            i += 1
    flush()

    full = "\n\n".join(b for b in blocks if b.strip())
    return _split_lines(full)


def html_to_plain(s: str) -> str:
    """Rimuove i tag HTML (fallback se Telegram rifiuta comunque il messaggio)."""
    s = re.sub(r'<a href="[^"]*">([^<]*)</a>', r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)
