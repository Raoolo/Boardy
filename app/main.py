"""FastAPI app: serves the chat UI, /chat endpoint, and conversation CRUD."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
import re
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import conversations as conv
from . import schema
from . import rulebooks as rb
from .chat import chat
from .db import get_conn

load_dotenv()
schema.migrate()
conv.migrate()

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"
LIBRARY = ROOT / "web" / "library.html"
RULEBOOKS_DIR = ROOT / "rulebooks"
RULEBOOKS_DIR.mkdir(exist_ok=True)


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "file"

app = FastAPI(title="Boardy")


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


class ChatResponse(BaseModel):
    reply: str
    history: list[dict]
    conversation_id: int


@app.get("/")
def index() -> FileResponse:
    return FileResponse(INDEX)


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest) -> ChatResponse:
    conv_id = req.conversation_id
    if conv_id is None:
        conv_id = conv.create_conversation()
        history = []
    else:
        loaded = conv.get_conversation(conv_id)
        if loaded is None:
            raise HTTPException(404, f"conversation {conv_id} not found")
        history = loaded["history"]

    reply, history = chat(req.message, history, conversation_id=conv_id)
    conv.save_conversation(conv_id, history)
    return ChatResponse(reply=reply, history=history, conversation_id=conv_id)


@app.get("/conversations")
def list_conversations() -> list[dict]:
    return conv.list_conversations()


@app.get("/conversations/{conv_id}")
def get_conversation(conv_id: int) -> dict:
    c = conv.get_conversation(conv_id)
    if c is None:
        raise HTTPException(404, f"conversation {conv_id} not found")
    return c


@app.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: int) -> dict:
    conv.delete_conversation(conv_id)
    return {"ok": True}


@app.get("/games/names")
def games_names() -> list[str]:
    """Names only — for autocomplete dropdowns in the UI."""
    with get_conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM games ORDER BY name")]


@app.get("/library")
def library_page() -> FileResponse:
    return FileResponse(LIBRARY)


@app.get("/library/data")
def library_data() -> dict:
    """All games with their dim arrays + the universe of categories/mechanics for filter dropdowns."""
    with get_conn() as c:
        games = []
        rows = c.execute("""
            SELECT id, name, bgg_id, year_published,
                   players_min, players_max, players_best,
                   duration_min, duration_min_min, duration_max_min,
                   complexity_label, complexity_weight, bgg_rating,
                   thumbnail_url, sleeve_status
            FROM games ORDER BY name
        """).fetchall()
        for r in rows:
            gid = r["id"]
            cats = [x["name"] for x in c.execute(
                "SELECT d.name FROM categories d JOIN game_categories b ON b.category_id=d.id WHERE b.game_id=? ORDER BY d.name",
                (gid,)).fetchall()]
            mechs = [x["name"] for x in c.execute(
                "SELECT d.name FROM mechanics d JOIN game_mechanics b ON b.mechanic_id=d.id WHERE b.game_id=? ORDER BY d.name",
                (gid,)).fetchall()]
            games.append({**dict(r), "categories": cats, "mechanics": mechs})
        all_categories = [r["name"] for r in c.execute("SELECT name FROM categories ORDER BY name")]
        all_mechanics  = [r["name"] for r in c.execute("SELECT name FROM mechanics ORDER BY name")]
    return {"games": games, "categories": all_categories, "mechanics": all_mechanics}


@app.post("/rulebooks/upload")
async def upload_rulebook(
    game_name: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "expected a .pdf file")
    safe = _safe_filename(file.filename)
    dest = RULEBOOKS_DIR / safe
    contents = await file.read()
    dest.write_bytes(contents)
    result = rb.ingest(game_name, str(dest))
    if "error" in result:
        # keep the file even on ingest error so user can retry / debug
        raise HTTPException(400, result["error"])
    return {**result, "saved_to": str(dest)}
