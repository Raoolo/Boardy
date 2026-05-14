"""Authentication for Boardy — username/password locale + cookie firmato.

Threat model: 3-5 owner fissi + guest non autenticati. Per questa scala
non servono JWT (no interop con altri service), né session table su DB
(no revoca selettiva: se ruoti `BOARDY_SESSION_SECRET` tutti i cookie
diventano invalidi, abbastanza per 3 utenti).

Flow:
- Guest: niente cookie → tutti i write endpoint rispondono 401, la chat
  filtra fuori i write tools dal registry prima del loop tool-use.
- Owner: POST /auth/login → cookie firmato in HttpOnly → ogni richiesta
  successiva carica `get_current_user` via Depends.

Password storage: bcrypt diretto (la libreria `bcrypt` 5+). Originariamente
usavo passlib come wrapper, ma passlib 1.7.4 e' incompatibile con bcrypt 5+
(legge un attributo `__about__` rimosso in v5) — usare bcrypt diretto e'
piu' semplice, una dipendenza in meno, API identica.
Cost factor default (12 rounds) ≈ 250ms per verify su CPU normale.
"""
from __future__ import annotations

import os

import bcrypt
from fastapi import Cookie, HTTPException, Response
from itsdangerous import BadSignature, URLSafeSerializer

from .db import get_conn

COOKIE_NAME = "boardy_session"
# 30 giorni — lungo perché single-device personale; chi vuole logout esplicito
# clicca "Esci". Per uso pubblico ridurrei a 7 giorni.
COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _serializer() -> URLSafeSerializer:
    """Itsdangerous serializer scoped al salt 'boardy-session-v1'.

    Cambiare il salt invaliderebbe tutti i cookie esistenti — utile se in
    futuro cambiamo il payload shape. Cambiare invece BOARDY_SESSION_SECRET
    li invalida senza toccare il codice.
    """
    secret = os.environ.get("BOARDY_SESSION_SECRET")
    if not secret:
        raise RuntimeError(
            "BOARDY_SESSION_SECRET non configurato. Genera con: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return URLSafeSerializer(secret, salt="boardy-session-v1")


def hash_password(plain: str) -> str:
    # bcrypt accetta max 72 byte di password; troncarlo "manualmente" garantisce
    # comportamento prevedibile invece di lasciarlo crashare con stringhe lunghe.
    pw_bytes = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        pw_bytes = plain.encode("utf-8")[:72]
        return bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Hash malformato (corrotto o non-bcrypt) → password sbagliata, non crash.
        return False


def authenticate(username: str, password: str) -> dict | None:
    """Ritorna user dict su successo, None su fallimento (user inesistente o
    password errata — stesso risultato per evitare user enumeration).
    """
    with get_conn() as c:
        row = c.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username=?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def set_session_cookie(response: Response, user: dict) -> None:
    """Imposta il cookie firmato sulla response.

    `secure=True` (solo HTTPS) si attiva solo quando BOARDY_COOKIE_SECURE=1 nel
    deploy — di default off così funziona anche su http://localhost in dev.
    """
    token = _serializer().dumps({"uid": user["id"], "u": user["username"]})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("BOARDY_COOKIE_SECURE", "0") == "1",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def get_current_user(
    boardy_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> dict | None:
    """FastAPI dependency: ritorna user dict o None (guest).

    Non lancia errori: lascia che gli endpoint decidano se 401 è dovuto
    (write) o no (read pubblico).
    """
    if not boardy_session:
        return None
    try:
        payload = _serializer().loads(boardy_session)
    except BadSignature:
        # Cookie manomesso o firma scaduta → trattalo come guest.
        return None
    uid = payload.get("uid")
    if not uid:
        return None
    with get_conn() as c:
        row = c.execute(
            "SELECT id, username, role FROM users WHERE id=?", (uid,)
        ).fetchone()
    if row is None:
        # User cancellato dopo l'emissione del cookie → guest.
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def require_owner(user: dict | None) -> dict:
    """Helper: alza 401 se user è None. Da chiamare negli endpoint di scrittura."""
    if user is None:
        raise HTTPException(401, "login required")
    return user
