import os
import uuid
import sqlite3
import time
import base64
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_from_directory, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static")
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024  # 15MB (encrypted file + base64 overhead)

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
        DROP TABLE IF EXISTS stats;
    """)
    # Migrate: add missing columns to existing tables
    cursor = conn.execute("PRAGMA table_info(notes)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}  # name -> type
    if "max_reads" not in columns:
        conn.execute("ALTER TABLE notes ADD COLUMN max_reads INTEGER NOT NULL DEFAULT 0")
    if "password_hash" not in columns:
        conn.execute("ALTER TABLE notes ADD COLUMN password_hash TEXT")
    if "attachment_meta" not in columns:
        conn.execute("ALTER TABLE notes ADD COLUMN attachment_meta TEXT")
    if "attachment_data" not in columns:
        # Fresh install: add as BLOB directly
        conn.execute("ALTER TABLE notes ADD COLUMN attachment_data BLOB")
    elif columns.get("attachment_data", "").upper() == "TEXT":
        # Migrate TEXT -> BLOB: rename old column, add new BLOB column, copy data, drop old
        conn.executescript("""
            ALTER TABLE notes RENAME COLUMN attachment_data TO attachment_data_old;
            ALTER TABLE notes ADD COLUMN attachment_data BLOB;
        """)
        # Convert existing Base64 TEXT data to binary BLOB
        rows = conn.execute("SELECT id, attachment_data_old FROM notes WHERE attachment_data_old IS NOT NULL").fetchall()
        for row in rows:
            try:
                binary_data = base64.b64decode(row[1])
                conn.execute("UPDATE notes SET attachment_data = ? WHERE id = ?", (binary_data, row[0]))
            except Exception:
                # If decoding fails, store raw bytes as-is
                conn.execute("UPDATE notes SET attachment_data = ? WHERE id = ?", (row[1].encode('utf-8'), row[0]))
        conn.execute("ALTER TABLE notes DROP COLUMN attachment_data_old")
    conn.commit()
    conn.close()

# NOTE: Not thread-safe; multiple threads/workers may run cleanup
# concurrently, but cleanup_expired() is idempotent so this is harmless.
_last_cleanup_time = 0
_CLEANUP_INTERVAL = 60  # seconds

def cleanup_expired():
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("DELETE FROM notes WHERE expires_at <= ?", (now,))
    db.commit()

@app.before_request
def before_request_hook():
    global _last_cleanup_time
    now = time.monotonic()
    if now - _last_cleanup_time >= _CLEANUP_INTERVAL:
        _last_cleanup_time = now
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
    if not data:
        return jsonify({"error": "request body is required"}), 400
    content = data.get("content")
    attachment_data_b64 = data.get("attachment_data")
    attachment_meta = data.get("attachment_meta")
    if bool(attachment_data_b64) != bool(attachment_meta):
        return jsonify({"error": "attachment_data and attachment_meta must both be provided"}), 400
    if not content and not attachment_data_b64:
        return jsonify({"error": "content or attachment is required"}), 400
    # Decode Base64 to binary for BLOB storage
    attachment_blob = None
    if attachment_data_b64:
        try:
            attachment_blob = base64.b64decode(attachment_data_b64)
        except Exception:
            return jsonify({"error": "attachment_data must be valid Base64"}), 400
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
        "INSERT INTO notes (id, content, burn_after_read, created_at, expires_at, read_count, max_reads, password_hash, attachment_data, attachment_meta) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (note_id, content or '', int(burn_after_read), now.isoformat(), expires_at.isoformat(), max_reads, pw_hash, attachment_blob, attachment_meta),
    )
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
    now = datetime.now(timezone.utc).isoformat()

    _NOTE_COLS = "id, content, burn_after_read, created_at, expires_at, read_count, max_reads, password_hash, attachment_data, attachment_meta"
    row = db.execute(f"SELECT {_NOTE_COLS} FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return jsonify({"error": "Note not found or has expired"}), 404
    # Reject expired notes
    if row["expires_at"] <= now:
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.commit()
        return jsonify({"error": "Note not found or has expired"}), 404
    # Password check (before any destructive operation)
    if row["password_hash"]:
        password = request.headers.get("X-Note-Password", "")
        if not password or not check_password_hash(row["password_hash"], password):
            return jsonify({"error": "password_required", "password_protected": True}), 403
    is_password_protected = row["password_hash"] is not None

    def _build_response(row, read_count, burn):
        resp = {
            "content": row["content"],
            "created_at": row["created_at"],
            "burn_after_read": burn,
            "expires_at": row["expires_at"],
            "read_count": read_count,
            "max_reads": row["max_reads"],
            "password_protected": is_password_protected,
        }
        if row["attachment_data"]:
            # Convert BLOB back to Base64 for JSON response
            attachment_bytes = row["attachment_data"]
            if isinstance(attachment_bytes, bytes):
                resp["attachment_data"] = base64.b64encode(attachment_bytes).decode("ascii")
            else:
                resp["attachment_data"] = attachment_bytes
            resp["attachment_meta"] = row["attachment_meta"]
        return resp

    # Burn-after-read: atomic DELETE to prevent race conditions
    if row["burn_after_read"]:
        deleted = db.execute(
            "DELETE FROM notes WHERE id = ? AND burn_after_read = 1 RETURNING id",
            (note_id,),
        ).fetchone()
        if deleted is None:
            # Another request already consumed this note
            return jsonify({"error": "Note not found or has expired"}), 404
        db.commit()
        return jsonify(_build_response(row, 1, True))
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
    return jsonify(_build_response(row, new_count, False))

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



init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
