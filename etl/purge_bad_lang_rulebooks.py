"""Retroactive cleanup: drop rulebooks indexed in a NON-allowed language.

The download-time language gate (app/rulebooks.ingest_bytes) only protects NEW
ingests. Rulebooks indexed before the gate existed can still be poisoned — e.g.
a Thai PDF of the wrong game ("Trails") that got attached to Barrage, or a
Hungarian Vampire rulebook. Those make the RAG answer nonsense (and from the
wrong language), so we sweep the DB and remove them.

Detection reuses `rulebooks.detect_language` (py3langid) on the stored
`pdf_blob` text — the SAME check the live gate runs — so this stays consistent
with what would be allowed on a fresh download. Allowed set comes from
`rulebooks.allowed_rulebook_langs()` (env BOARDY_RULEBOOK_LANGS).

Usage:
  uv run python etl/purge_bad_lang_rulebooks.py            # dry-run: just report
  uv run python etl/purge_bad_lang_rulebooks.py --apply    # actually delete
  uv run python etl/purge_bad_lang_rulebooks.py --only Barrage

A rulebook whose text can't be parsed or whose language is uncertain ('?') is
LEFT ALONE (same lenient stance as the live gate — we don't delete on doubt).
"""
from __future__ import annotations

import argparse
import io
import os
import sys

from pypdf import PdfReader

# Windows console = cp1252; our output uses → / ✓ — force UTF-8 early.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Running `python etl/foo.py` puts etl/ on sys.path[0], not the repo root — add it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn                                       # noqa: E402
from app.rulebooks import allowed_rulebook_langs, detect_language  # noqa: E402


def _blob_lang(blob: bytes) -> str:
    try:
        txt = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages)
    except Exception:
        return "?"
    return detect_language(txt)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete the offending rulebooks (default: dry-run)")
    ap.add_argument("--only", metavar="NAME",
                    help="restrict to games whose name contains NAME (case-insensitive)")
    args = ap.parse_args()

    allowed = allowed_rulebook_langs()
    print(f"Lingue ammesse: {', '.join(sorted(allowed))}")
    print(f"Modalità: {'APPLY (cancella)' if args.apply else 'dry-run (solo report)'}\n")

    with get_conn() as conn:
        q = """SELECT rb.id, g.name, rb.source_path, rb.pdf_blob
               FROM rulebooks rb JOIN games g ON g.id = rb.game_id"""
        params: tuple = ()
        if args.only:
            q += " WHERE LOWER(g.name) LIKE ?"
            params = (f"%{args.only.lower()}%",)
        rows = conn.execute(q + " ORDER BY g.name", params).fetchall()

        bad: list[tuple[int, str, str]] = []
        for r in rows:
            if not r["pdf_blob"]:
                continue
            lang = _blob_lang(bytes(r["pdf_blob"]))
            if lang != "?" and lang not in allowed:
                bad.append((r["id"], r["name"], lang))
                print(f"  ✗ id={r['id']:<4} {r['name'][:40]:40} lang={lang}  src={r['source_path']}")

        if not bad:
            print("Nessun regolamento in lingua non ammessa. Tutto pulito.")
            return

        print(f"\nTrovati {len(bad)} regolamenti da rimuovere.")
        if not args.apply:
            print("Dry-run: niente cancellato. Rilancia con --apply per procedere.")
            return

        for rb_id, name, lang in bad:
            n = conn.execute("SELECT count(*) FROM rulebook_chunks WHERE rulebook_id=?",
                             (rb_id,)).fetchone()[0]
            conn.execute("DELETE FROM rulebook_chunks WHERE rulebook_id=?", (rb_id,))
            conn.execute("DELETE FROM rulebooks WHERE id=?", (rb_id,))
            print(f"  ✓ cancellato id={rb_id} ({name}, {lang}) + {n} chunk")
        conn.commit()
        print(f"\nFatto: {len(bad)} regolamenti rimossi.")


if __name__ == "__main__":
    main()
