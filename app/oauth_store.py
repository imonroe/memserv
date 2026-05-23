import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("OAUTH_DB_PATH", "/app/data/oauth.db")


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                client_secret TEXT,
                redirect_uris TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )


def save_client(client_id: str, client_secret: str | None, redirect_uris: list[str]) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO clients VALUES (?, ?, ?, ?)",
            (client_id, client_secret, json.dumps(redirect_uris), str(time.time())),
        )


def get_client(client_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT client_id, client_secret, redirect_uris FROM clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    if not row:
        return None
    return {"client_id": row[0], "client_secret": row[1], "redirect_uris": json.loads(row[2])}


def delete_expired_codes() -> int:
    """Remove auth codes past their expiry. Returns the number deleted."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (time.time(),))
        return cur.rowcount


def save_code(
    code: str, client_id: str, redirect_uri: str, code_challenge: str, ttl: int = 300
) -> None:
    # Opportunistic cleanup so abandoned/expired codes don't accumulate.
    delete_expired_codes()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?)",
            (code, client_id, redirect_uri, code_challenge, time.time() + ttl),
        )


def consume_code(code: str) -> dict | None:
    """Fetch and delete an auth code (single-use). Returns None if missing or expired."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT client_id, redirect_uri, code_challenge, expires_at "
            "FROM auth_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
    if not row:
        return None
    if row[3] < time.time():
        return None
    return {"client_id": row[0], "redirect_uri": row[1], "code_challenge": row[2]}


def delete_expired_refresh_tokens() -> int:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (time.time(),))
        return cur.rowcount


def save_refresh_token(token: str, client_id: str, ttl: int = 30 * 24 * 3600) -> None:
    delete_expired_refresh_tokens()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO refresh_tokens VALUES (?, ?, ?)",
            (token, client_id, time.time() + ttl),
        )


def consume_refresh_token(token: str) -> dict | None:
    """Fetch and delete a refresh token (single-use, rotated on every use).

    Returns the bound client_id, or None if the token is unknown or expired.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT client_id, expires_at FROM refresh_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
    if not row or row[1] < time.time():
        return None
    return {"client_id": row[0]}
