import os
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory, g

app = Flask(__name__, static_folder="static")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "burnote.db")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id            TEXT PRIMARY KEY,
            content       TEXT NOT NULL,
            burn_after_read INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            read_count    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO stats (key, value) VALUES ('total_notes_created', 0);
    """)
    conn.commit()
    conn.close()


def cleanup_expired():
    """Delete expired notes. Called on every request."""
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("DELETE FROM notes WHERE expires_at <= ?", (now,))
    db.commit()


# ---------------------------------------------------------------------------
# CORS & cleanup middleware
# ---------------------------------------------------------------------------

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.before_request
def before_request_hook():
    cleanup_expired()


# ---------------------------------------------------------------------------
# Static / SPA
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/note/<path:path>")
def index(path=None):
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/notes", methods=["POST", "OPTIONS"])
def create_note():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True)
    if not data or not data.get("content"):
        return jsonify({"error": "content is required"}), 400

    content = data["content"]
    burn_after_read = bool(data.get("burn_after_read", False))
    expires_minutes = int(data.get("expires_minutes", 60))
    expires_minutes = max(1, min(expires_minutes, 1440))  # clamp 1-1440

    note_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=expires_minutes)

    db = get_db()
    db.execute(
        "INSERT INTO notes (id, content, burn_after_read, created_at, expires_at, read_count) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (note_id, content, int(burn_after_read), now.isoformat(), expires_at.isoformat()),
    )
    db.execute("UPDATE stats SET value = value + 1 WHERE key = 'total_notes_created'")
    db.commit()

    base_url = request.host_url.rstrip("/")
    return jsonify({
        "id": note_id,
        "url": f"{base_url}/note/{note_id}",
        "expires_at": expires_at.isoformat(),
    }), 201


@app.route("/api/notes/<note_id>", methods=["GET", "OPTIONS"])
def read_note(note_id):
    if request.method == "OPTIONS":
        return "", 204

    db = get_db()
    row = db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()

    if row is None:
        return jsonify({"error": "Note not found or has expired"}), 404

    # Increment read count
    db.execute("UPDATE notes SET read_count = read_count + 1 WHERE id = ?", (note_id,))
    db.commit()

    result = {
        "content": row["content"],
        "created_at": row["created_at"],
        "burn_after_read": bool(row["burn_after_read"]),
        "expires_at": row["expires_at"],
        "read_count": row["read_count"] + 1,
    }

    # Burn after read: delete the note now that we've captured it
    if row["burn_after_read"]:
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.commit()

    return jsonify(result)


@app.route("/api/notes/<note_id>/exists", methods=["GET", "OPTIONS"])
def note_exists(note_id):
    if request.method == "OPTIONS":
        return "", 204

    db = get_db()
    row = db.execute("SELECT id FROM notes WHERE id = ?", (note_id,)).fetchone()
    return jsonify({"exists": row is not None})


@app.route("/api/stats", methods=["GET", "OPTIONS"])
def stats():
    if request.method == "OPTIONS":
        return "", 204

    db = get_db()
    total = db.execute(
        "SELECT value FROM stats WHERE key = 'total_notes_created'"
    ).fetchone()
    active = db.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()

    return jsonify({
        "total_notes_created": total["value"] if total else 0,
        "active_notes": active["cnt"] if active else 0,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Ensure DB is initialised at import time (for gunicorn workers)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
