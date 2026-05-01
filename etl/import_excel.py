"""Excel -> SQLite ETL for Boardy.

Reads sheet 'Elenco Premium' from `1) ElencoGiochi.xlsx`, parses the messy
SLEEVE column with regex, and writes a normalized SQLite database at
`boardy.db`. Idempotent: drops & recreates tables on every run.

Run:
    uv run python etl/import_excel.py
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
EXCEL_PATH = ROOT / "1) ElencoGiochi.xlsx"
DB_PATH = ROOT / "boardy.db"
UNPARSED_PATH = ROOT / "etl" / "unparsed.txt"
SHEET_NAME = "Elenco Premium"
HEADER_ROW = 3  # row 3 is the header; data starts at row 4

SCHEMA = """
DROP TABLE IF EXISTS sleeve_requirements;
DROP TABLE IF EXISTS sleeve_inventory;
DROP TABLE IF EXISTS game_designers;
DROP TABLE IF EXISTS game_publishers;
DROP TABLE IF EXISTS game_categories;
DROP TABLE IF EXISTS game_mechanics;
DROP TABLE IF EXISTS games;

CREATE TABLE games (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  bgg_id INTEGER UNIQUE,
  year_published INTEGER,
  players_min INTEGER,
  players_max INTEGER,
  players_best TEXT,
  duration_min INTEGER,
  duration_min_min INTEGER,
  duration_max_min INTEGER,
  age_min INTEGER,
  complexity_label TEXT,
  complexity_weight REAL,
  bgg_rating REAL,
  description TEXT,
  thumbnail_url TEXT,
  image_url TEXT,
  language TEXT,
  condition TEXT,
  notes TEXT,
  sleeve_status TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS designers  (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS publishers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS mechanics  (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);

CREATE TABLE game_designers  (game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, designer_id INTEGER NOT NULL REFERENCES designers(id) ON DELETE CASCADE, PRIMARY KEY(game_id,designer_id));
CREATE TABLE game_publishers (game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, publisher_id INTEGER NOT NULL REFERENCES publishers(id) ON DELETE CASCADE, PRIMARY KEY(game_id,publisher_id));
CREATE TABLE game_categories (game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, PRIMARY KEY(game_id,category_id));
CREATE TABLE game_mechanics  (game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, mechanic_id INTEGER NOT NULL REFERENCES mechanics(id) ON DELETE CASCADE, PRIMARY KEY(game_id,mechanic_id));

CREATE TABLE sleeve_requirements (
  id INTEGER PRIMARY KEY,
  game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  count INTEGER NOT NULL,
  width_mm REAL NOT NULL,
  height_mm REAL NOT NULL,
  note TEXT
);

CREATE TABLE sleeve_inventory (
  id INTEGER PRIMARY KEY,
  width_mm REAL NOT NULL,
  height_mm REAL NOT NULL,
  count_owned INTEGER NOT NULL DEFAULT 0,
  brand TEXT,
  UNIQUE(width_mm, height_mm, brand)
);

CREATE INDEX idx_req_size ON sleeve_requirements(width_mm, height_mm);
CREATE INDEX idx_inv_size ON sleeve_inventory(width_mm, height_mm);
"""


def _upsert_dim(conn, table: str, name: str) -> int:
    name = name.strip()
    conn.execute(f"INSERT OR IGNORE INTO {table}(name) VALUES(?)", (name,))
    return conn.execute(f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()[0]

# Primary: count first — "166-63,5x88", "200pz 63.5x88", "21-70x120".
SLEEVE_PATTERN = re.compile(
    r"(\d+)\s*(?:pz|x)?\s*[-x\s]\s*"
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
# Fallback: size first, count after — "65x100 100x" / "65x100 100pz".
SLEEVE_PATTERN_REV = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s+(\d+)\s*(?:pz|x)\b",
    re.IGNORECASE,
)
PLAYERS_PATTERN = re.compile(r"(\d+)\s*-\s*(\d+)")
SINGLE_PLAYER_PATTERN = re.compile(r"^(\d+)$")

# 'no' and 'na' both map to 'na' — they were originally distinct
# (intentional skip vs. not-applicable) but in practice they mean the
# same thing for this collection: doesn't need sleeving.
STATUS_MAP = {
    "sleeved": "sleeved",
    "sleevato": "sleeved",
    "no": "na",
    "n.a.": "na",
    "na": "na",
    "n/a": "na",
}


def classify_sleeve(raw: str | None) -> tuple[str, list[tuple[int, float, float, str | None]]]:
    """Return (status, [(count, w, h, note), ...]).

    Status is one of: sleeved, na, to_sleeve, unknown.
    The list contains parsed per-size requirements (may be empty).

    Status mapping rules (refined 2026-04-29 after misclassification audit):
    - Cell == "Sleeved" / "Sleevato"            → sleeved, []
    - Cell == "No" / "n.a." / "n/a" / "na"      → na, []
    - Cell contains "DA COMPRARE"               → to_sleeve, [parsed reqs]
    - Cell contains "COMPRATE"                  → sleeved, [] (purchased & applied;
                                                  invariant: sleeved must NOT carry reqs)
    - Cell has only numeric size info, no marker → unknown, [parsed reqs]
        (NOT sleeved! The Excel column listed CARD SIZES; sleeving status
         is unverified, requires manual review post-import.)
    """
    if raw is None or not str(raw).strip():
        return "unknown", []

    text = str(raw).strip()
    lowered = text.lower()
    if lowered in STATUS_MAP:
        return STATUS_MAP[lowered], []

    note: str | None = None
    if "comprate" in lowered:
        note = "COMPRATE"
    elif "da comprare" in lowered or "comprare" in lowered:
        note = "DA COMPRARE"

    requirements: list[tuple[int, float, float, str | None]] = []
    consumed: list[tuple[int, int]] = []
    for m in SLEEVE_PATTERN.finditer(text):
        count = int(m.group(1))
        width = float(m.group(2).replace(",", "."))
        height = float(m.group(3).replace(",", "."))
        requirements.append((count, width, height, note))
        consumed.append(m.span())
    for m in SLEEVE_PATTERN_REV.finditer(text):
        # Skip overlaps with primary matches
        if any(s <= m.start() < e or s < m.end() <= e for s, e in consumed):
            continue
        width = float(m.group(1).replace(",", "."))
        height = float(m.group(2).replace(",", "."))
        count = int(m.group(3))
        requirements.append((count, width, height, note))

    if requirements:
        if note == "DA COMPRARE":
            return "to_sleeve", requirements
        if note == "COMPRATE":
            # Already purchased AND applied. Invariant: sleeved games carry
            # no requirements rows — they'd inflate `sleeve_summary.to_buy`.
            # The size info is preserved in this audit/log path via the
            # original Excel cell only.
            return "sleeved", []
        # No status marker — just numeric sizes. Don't assume sleeved!
        # Default to unknown so the user can confirm post-import.
        return "unknown", requirements

    # Couldn't parse — still report as unknown for manual review
    return "unknown", []


def parse_players(raw) -> tuple[str | None, int | None, int | None]:
    if raw is None:
        return None, None, None
    text = str(raw).strip()
    if not text:
        return None, None, None
    m = PLAYERS_PATTERN.search(text)
    if m:
        return text, int(m.group(1)), int(m.group(2))
    m = SINGLE_PLAYER_PATTERN.match(text)
    if m:
        n = int(m.group(1))
        return text, n, n
    return text, None, None


def parse_int(raw) -> int | None:
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def main() -> None:
    if not EXCEL_PATH.exists():
        raise SystemExit(f"Excel file not found: {EXCEL_PATH}")

    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    ws = wb[SHEET_NAME]

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    unparsed: list[str] = []
    games_count = 0
    req_count = 0

    for row in ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True):
        name = row[0]
        if not name or not str(name).strip():
            continue
        name = str(name).strip()

        producer = (str(row[1]).strip() if row[1] else None)
        publisher = (str(row[2]).strip() if row[2] else None)
        players_raw, p_min, p_max = parse_players(row[3])
        duration = parse_int(row[4])
        complexity = (str(row[5]).strip() if row[5] else None)
        condition = (str(row[6]).strip() if row[6] else None)
        sleeve_raw = row[7]
        sleeve_raw_str = str(sleeve_raw).strip() if sleeve_raw is not None else None

        status, reqs = classify_sleeve(sleeve_raw)

        # If raw text is present but produced no status match and no parsed reqs,
        # log it for manual review. The raw cell itself is NOT stored — it was
        # an Excel-import artifact that duplicated info already in
        # sleeve_status / sleeve_requirements (see etl/cleanup_sleeve.py).
        if (
            sleeve_raw_str
            and status == "unknown"
            and not reqs
        ):
            unparsed.append(f"{name!r}: {sleeve_raw_str!r}")

        cur = conn.execute(
            """INSERT INTO games(name, players_min, players_max, players_best,
                                 duration_min, complexity_label, condition,
                                 sleeve_status)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                name, p_min, p_max, players_raw,
                duration, complexity, condition, status,
            ),
        )
        game_id = cur.lastrowid
        games_count += 1

        # Bridge designers (split CSV)
        if producer:
            for d in [s.strip() for s in producer.split(",") if s.strip()]:
                did = _upsert_dim(conn, "designers", d)
                conn.execute("INSERT OR IGNORE INTO game_designers(game_id, designer_id) VALUES(?,?)", (game_id, did))
        if publisher:
            for p in [s.strip() for s in publisher.split(",") if s.strip()]:
                pid = _upsert_dim(conn, "publishers", p)
                conn.execute("INSERT OR IGNORE INTO game_publishers(game_id, publisher_id) VALUES(?,?)", (game_id, pid))

        # Invariant: sleeved/na games must not carry pending requirements rows.
        # `classify_sleeve` already enforces this, but we double-check here so a
        # future regression doesn't silently re-introduce phantom sleeves.
        if status in ("sleeved", "na"):
            reqs = []
        for count, w, h, note in reqs:
            conn.execute(
                """INSERT INTO sleeve_requirements(game_id, count, width_mm, height_mm, note)
                   VALUES(?,?,?,?,?)""",
                (game_id, count, w, h, note),
            )
            req_count += 1

    conn.commit()
    conn.close()

    UNPARSED_PATH.write_text("\n".join(unparsed) + ("\n" if unparsed else ""), encoding="utf-8")

    print(f"Imported {games_count} games, {req_count} sleeve-requirement rows.")
    print(f"Unparsed cells: {len(unparsed)} (see {UNPARSED_PATH.name})")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
