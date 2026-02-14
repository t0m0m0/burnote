import os
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static")

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100/minute"],
    storage_uri="memory://",
    strategy="fixed-window",
)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kemuri.db")

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
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id            TEXT PRIMARY KEY,
            content       TEXT NOT NULL,
            burn_after_read INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            read_count    INTEGER NOT NULL DEFAULT 0,
            max_reads     INTEGER NOT NULL DEFAULT 0,
            password_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS stats (
            key   TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO stats (key, value) VALUES ('total_notes_created', 0);
    """)
    # Migrate: add missing columns to existing tables
    cursor = conn.execute("PRAGMA table_info(notes)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "max_reads" not in existing_columns:
        conn.execute("ALTER TABLE notes ADD COLUMN max_reads INTEGER NOT NULL DEFAULT 0")
    if "password_hash" not in existing_columns:
        conn.execute("ALTER TABLE notes ADD COLUMN password_hash TEXT")
    conn.commit()
    conn.close()

def cleanup_expired():
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("DELETE FROM notes WHERE expires_at <= ?", (now,))
    db.commit()

@app.before_request
def before_request_hook():
    cleanup_expired()

@app.route("/")
@app.route("/note/<path:path>")
def index(path=None):
    return send_from_directory(app.static_folder, "index.html")

@app.route("/api/notes", methods=["POST", "OPTIONS"])
@limiter.limit("10/minute", methods=["POST"])
def create_note():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True)
    if not data or not data.get("content"):
        return jsonify({"error": "content is required"}), 400
    content = data["content"]
    burn_after_read = bool(data.get("burn_after_read", False))
    expires_minutes = int(data.get("expires_minutes", 60))
    expires_minutes = max(1, min(expires_minutes, 1440))
    max_reads = int(data.get("max_reads", 0))
    max_reads = max(0, min(max_reads, 100))
    password = data.get("password")
    pw_hash = generate_password_hash(password) if password else None
    note_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=expires_minutes)
    db = get_db()
    db.execute(
        "INSERT INTO notes (id, content, burn_after_read, created_at, expires_at, read_count, max_reads, password_hash) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (note_id, content, int(burn_after_read), now.isoformat(), expires_at.isoformat(), max_reads, pw_hash),
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
@limiter.limit("30/minute")
def read_note(note_id):
    if request.method == "OPTIONS":
        return "", 204
    db = get_db()
    row = db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Note not found or has expired"}), 404
    # Password check
    if row["password_hash"]:
        password = request.headers.get("X-Note-Password", "")
        if not password or not check_password_hash(row["password_hash"], password):
            return jsonify({"error": "password_required", "password_protected": True}), 403
    is_password_protected = row["password_hash"] is not None
    # Burn-after-read
    if row["burn_after_read"]:
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.commit()
        return jsonify({
            "content": row["content"],
            "created_at": row["created_at"],
            "burn_after_read": True,
            "expires_at": row["expires_at"],
            "read_count": 1,
            "max_reads": row["max_reads"],
            "password_protected": is_password_protected,
        })
    # Normal notes with max_reads
    max_reads = row["max_reads"]
    current_count = row["read_count"]
    if max_reads > 0 and current_count >= max_reads:
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.commit()
        return jsonify({"error": "Note not found or has expired"}), 404
    new_count = current_count + 1
    if max_reads > 0 and new_count >= max_reads:
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.commit()
    else:
        db.execute("UPDATE notes SET read_count = read_count + 1 WHERE id = ?", (note_id,))
        db.commit()
    return jsonify({
        "content": row["content"],
        "created_at": row["created_at"],
        "burn_after_read": False,
        "expires_at": row["expires_at"],
        "read_count": new_count,
        "max_reads": max_reads,
        "password_protected": is_password_protected,
    })

@app.route("/api/notes/<note_id>/exists", methods=["GET", "OPTIONS"])
@limiter.limit("30/minute")
def note_exists(note_id):
    if request.method == "OPTIONS":
        return "", 204
    db = get_db()
    row = db.execute("SELECT id, password_hash FROM notes WHERE id = ?", (note_id,)).fetchone()
    return jsonify({
        "exists": row is not None,
        "password_protected": row["password_hash"] is not None if row else False,
    })

@app.route("/api/stats", methods=["GET", "OPTIONS"])
def stats():
    if request.method == "OPTIONS":
        return "", 204
    db = get_db()
    total = db.execute("SELECT value FROM stats WHERE key = 'total_notes_created'").fetchone()
    active = db.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
    return jsonify({
        "total_notes_created": total["value"] if total else 0,
        "active_notes": active["cnt"] if active else 0,
    })

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
