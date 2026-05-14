"""CLI per creare owner di Boardy.

Uso:
    uv run python etl/create_user.py raulo
    uv run python etl/create_user.py amico --role owner

Password chiesta interattivamente (getpass — non finisce in shell history).
Hash bcrypt salvato in `users.password_hash`. Idempotente: se l'utente
esiste gia', errore esplicito (use `--reset` per cambiare password).
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

# Permetti l'esecuzione come script: sys.path include la repo root cosi'
# che `from app import ...` funzioni anche senza installare il pacchetto.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Console Windows = cp1252; evita UnicodeEncodeError sui simboli ✓/✗.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import schema
from app.auth import hash_password
from app.db import get_conn


MIN_PASSWORD_LEN = 8


def _prompt_password(label: str = "Password") -> str:
    pw1 = getpass.getpass(f"{label}: ")
    pw2 = getpass.getpass(f"{label} (conferma): ")
    if pw1 != pw2:
        print("Le password non corrispondono. Riprova.", file=sys.stderr)
        sys.exit(2)
    if len(pw1) < MIN_PASSWORD_LEN:
        print(f"Password troppo corta (minimo {MIN_PASSWORD_LEN} caratteri).", file=sys.stderr)
        sys.exit(2)
    return pw1


def cmd_create(username: str, role: str) -> None:
    pw = _prompt_password()
    h = hash_password(pw)
    with get_conn() as c:
        try:
            c.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, ?)",
                (username, h, role),
            )
            c.commit()
        except sqlite3.IntegrityError:
            print(f"Utente '{username}' esiste gia'. Usa --reset per cambiare password.", file=sys.stderr)
            sys.exit(1)
    print(f"✓ Utente '{username}' creato (role={role}).")


def cmd_reset(username: str) -> None:
    with get_conn() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            print(f"Utente '{username}' non esiste.", file=sys.stderr)
            sys.exit(1)
    pw = _prompt_password("Nuova password")
    h = hash_password(pw)
    with get_conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE username=?", (h, username))
        c.commit()
    print(f"✓ Password aggiornata per '{username}'.")


def cmd_list() -> None:
    with get_conn() as c:
        rows = c.execute(
            "SELECT username, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    if not rows:
        print("(nessun utente)")
        return
    print(f"{'USERNAME':<20} {'ROLE':<10} CREATED_AT")
    for r in rows:
        print(f"{r['username']:<20} {r['role']:<10} {r['created_at']}")


def main() -> None:
    schema.migrate()  # garantisce che `users` esista.

    parser = argparse.ArgumentParser(description="Crea/aggiorna owner Boardy.")
    sub = parser.add_subparsers(dest="cmd")

    p_create = sub.add_parser("create", help="Crea un nuovo utente")
    p_create.add_argument("username")
    p_create.add_argument("--role", default="owner")

    p_reset = sub.add_parser("reset", help="Cambia password di un utente esistente")
    p_reset.add_argument("username")

    sub.add_parser("list", help="Elenca utenti")

    # Compat: invocazione senza sottocomando = create (uso piu' comune)
    args, extra = parser.parse_known_args()
    if args.cmd is None and extra:
        # `python create_user.py raulo` → equivalente a `create raulo`
        args = parser.parse_args(["create", *extra])

    if args.cmd == "create":
        cmd_create(args.username, args.role)
    elif args.cmd == "reset":
        cmd_reset(args.username)
    elif args.cmd == "list":
        cmd_list()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
