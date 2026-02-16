import os
import uuid
import sqlite3
import time
import base64
import shutil
import threading
import atexit
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static", static_url_path="")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB (reduced from 15MB to mitigate DoS)

RATE_LIMIT_STORAGE_URI = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100/minute"],
    storage_uri=RATE_LIMIT_STORAGE_URI,
    strategy="fixed-window",
    storage_options={"socket_connect_timeout": 2} if RATE_LIMIT_STORAGE_URI.startswith("redis") else {},
)
_base_dir = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(_base_dir, "kemuri.db"))
ATTACHMENT_DIR = Path(os.environ.get("ATTACHMENT_DIR", os.path.join(_base_dir, "attachments")))
ATTACHMENT_DIR.mkdir(exist_ok=True)

# Disk usage limits
MAX_ATTACHMENT_SIZE = 3 * 1024 * 1024  # 3MB per file (after Base64 decode)
MAX_TOTAL_STORAGE = 500 * 1024 * 1024  # 500MB total attachment storage

def get_attachment_storage_size():
    """Calculate total size of all attachment files."""
    total = 0
    for f in ATTACHMENT_DIR.iterdir():
        if f.is_file():
            total += f.stat().st_size
    return total

def save_attachment(note_id, data):
    """Save attachment binary data to filesystem. Returns the filename."""
    filename = note_id
    filepath = ATTACHMENT_DIR / filename
    filepath.write_bytes(data)
    return filename

def load_attachment(note_id):
    """Load attachment binary data from filesystem. Returns bytes or None."""
    filepath = ATTACHMENT_DIR / note_id
    if filepath.exists():
        return filepath.read_bytes()
    return None

def delete_attachment(note_id):
    """Delete attachment file from filesystem."""
    filepath = ATTACHMENT_DIR / note_id
    try:
        filepath.unlink(missing_ok=True)
    except OSError:
        pass

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
    if "has_attachment" not in columns:
        conn.execute("ALTER TABLE notes ADD COLUMN has_attachment INTEGER NOT NULL DEFAULT 0")
    # Migrate: move BLOB/TEXT attachment_data from DB to filesystem
    if "attachment_data" in columns:
        rows = conn.execute("SELECT id, attachment_data FROM notes WHERE attachment_data IS NOT NULL").fetchall()
        for row in rows:
            note_id, data = row[0], row[1]
            if isinstance(data, str):
                try:
                    data = base64.b64decode(data)
                except Exception:
                    data = data.encode('utf-8')
            if data:
                save_attachment(note_id, data)
                conn.execute("UPDATE notes SET has_attachment = 1 WHERE id = ?", (note_id,))
        conn.execute("ALTER TABLE notes DROP COLUMN attachment_data")
        conn.commit()
    conn.commit()
    conn.close()

# NOTE: Not thread-safe; multiple threads/workers may run cleanup
# concurrently, but cleanup_expired() is idempotent so this is harmless.
_last_cleanup_time = 0
_CLEANUP_INTERVAL = 60  # seconds

def cleanup_expired():
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    # Find expired notes with attachments so we can delete files
    expired = db.execute(
        "SELECT id FROM notes WHERE expires_at <= ? AND has_attachment = 1",
        (now,),
    ).fetchall()
    for row in expired:
        delete_attachment(row[0])
    db.execute("DELETE FROM notes WHERE expires_at <= ?", (now,))
    db.commit()

@app.before_request
def before_request_hook():
    global _last_cleanup_time
    now = time.monotonic()
    if now - _last_cleanup_time >= _CLEANUP_INTERVAL:
        _last_cleanup_time = now
        cleanup_expired()

# Background cleanup thread — runs independently of HTTP traffic
_cleanup_stop_event = threading.Event()

def _background_cleanup_loop():
    """Periodically clean up expired notes even when there is no traffic."""
    while not _cleanup_stop_event.wait(timeout=_CLEANUP_INTERVAL):
        try:
            # Use a standalone DB connection (no Flask app context)
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            now = datetime.now(timezone.utc).isoformat()
            expired = conn.execute(
                "SELECT id FROM notes WHERE expires_at <= ? AND has_attachment = 1",
                (now,),
            ).fetchall()
            for row in expired:
                delete_attachment(row["id"])
            conn.execute("DELETE FROM notes WHERE expires_at <= ?", (now,))
            conn.commit()
            conn.close()
        except Exception:
            pass  # Silently retry on next interval

_cleanup_thread = threading.Thread(target=_background_cleanup_loop, daemon=True)
_cleanup_thread.start()

def _stop_cleanup():
    _cleanup_stop_event.set()

atexit.register(_stop_cleanup)

@app.after_request
def set_security_headers(response):
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src blob:; "
        "frame-src blob:; "
        "connect-src 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

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
    # Decode Base64 to binary for file-based storage
    attachment_blob = None
    if attachment_data_b64:
        try:
            attachment_blob = base64.b64decode(attachment_data_b64)
        except Exception:
            return jsonify({"error": "attachment_data must be valid Base64"}), 400
        if len(attachment_blob) > MAX_ATTACHMENT_SIZE:
            return jsonify({"error": f"attachment too large (max {MAX_ATTACHMENT_SIZE // (1024*1024)}MB)"}), 413
        # Check total disk usage quota
        current_usage = get_attachment_storage_size()
        if current_usage + len(attachment_blob) > MAX_TOTAL_STORAGE:
            return jsonify({"error": "storage quota exceeded, please try again later"}), 507
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
    has_attachment = 0
    if attachment_blob:
        save_attachment(note_id, attachment_blob)
        has_attachment = 1
    db.execute(
        "INSERT INTO notes (id, content, burn_after_read, created_at, expires_at, read_count, max_reads, password_hash, has_attachment, attachment_meta) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (note_id, content or '', int(burn_after_read), now.isoformat(), expires_at.isoformat(), max_reads, pw_hash, has_attachment, attachment_meta),
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

    _NOTE_COLS = "id, content, burn_after_read, created_at, expires_at, read_count, max_reads, password_hash, has_attachment, attachment_meta"
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
        if row["has_attachment"]:
            attachment_bytes = load_attachment(row["id"])
            if attachment_bytes:
                resp["attachment_data"] = base64.b64encode(attachment_bytes).decode("ascii")
                resp["attachment_meta"] = row["attachment_meta"]
        return resp

    # Burn-after-read: atomic DELETE to prevent race conditions
    if row["burn_after_read"]:
        # Build response BEFORE deleting (need to read attachment file)
        response_data = _build_response(row, 1, True)
        deleted = db.execute(
            "DELETE FROM notes WHERE id = ? AND burn_after_read = 1 RETURNING id",
            (note_id,),
        ).fetchone()
        if deleted is None:
            # Another request already consumed this note
            return jsonify({"error": "Note not found or has expired"}), 404
        db.commit()
        if row["has_attachment"]:
            delete_attachment(note_id)
        return jsonify(response_data)
    # Normal notes with max_reads — atomic UPDATE to prevent TOCTOU race condition
    max_reads = row["max_reads"]
    if max_reads > 0:
        # Atomically increment read_count only if still under the limit
        updated = db.execute(
            "UPDATE notes SET read_count = read_count + 1 "
            "WHERE id = ? AND read_count < max_reads "
            "RETURNING read_count, max_reads",
            (note_id,),
        ).fetchone()
        if updated is None:
            # Already at or over limit (or note gone) — delete and return 404
            db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            db.commit()
            if row["has_attachment"]:
                delete_attachment(note_id)
            return jsonify({"error": "Note not found or has expired"}), 404
        new_count = updated["read_count"]
        response_data = _build_response(row, new_count, False)
        # If we just hit the limit, delete the note and attachment
        if new_count >= updated["max_reads"]:
            db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            if row["has_attachment"]:
                delete_attachment(note_id)
        db.commit()
        return jsonify(response_data)
    else:
        # No read limit — just increment
        db.execute("UPDATE notes SET read_count = read_count + 1 WHERE id = ?", (note_id,))
        db.commit()
        new_count = row["read_count"] + 1
        return jsonify(_build_response(row, new_count, False))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
