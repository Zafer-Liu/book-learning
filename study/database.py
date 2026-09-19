"""SQLite persistence; callers must bind both owner and book identifiers."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Shared system account owning the baked-in textbooks visible to every user.
BUILTIN_OWNER = "builtin"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL, filename TEXT NOT NULL, source_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0, section_count INTEGER NOT NULL DEFAULT 0,
    index_backend TEXT NOT NULL DEFAULT 'lexical', created_at TEXT NOT NULL,
    UNIQUE(owner_id, id)
);
CREATE INDEX IF NOT EXISTS books_owner ON books(owner_id, created_at);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, section TEXT NOT NULL, page INTEGER,
    text TEXT NOT NULL, embedding TEXT, embedding_space TEXT,
    FOREIGN KEY(owner_id, book_id) REFERENCES books(owner_id, id) ON DELETE CASCADE,
    UNIQUE(book_id, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_scope ON chunks(owner_id, book_id, section);
CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version TEXT NOT NULL, start INTEGER NOT NULL CHECK(start >= 0),
    end INTEGER NOT NULL CHECK(end > start),
    quote TEXT NOT NULL CHECK(length(quote) BETWEEN 1 AND 4000),
    note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 4000),
    color TEXT NOT NULL DEFAULT 'yellow' CHECK(color IN ('yellow','blue','green')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS annotations_scope ON annotations(owner_id, book_id, start, id);
CREATE INDEX IF NOT EXISTS annotations_book ON annotations(book_id);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
    title TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
    UNIQUE(owner_id, book_id, id)
);
CREATE INDEX IF NOT EXISTS conversations_scope ON conversations(owner_id, book_id);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    mode TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS messages_scope ON messages(owner_id, book_id, conversation_id, created_at);
CREATE TABLE IF NOT EXISTS test_codes (
    code TEXT PRIMARY KEY, bound_username TEXT NOT NULL DEFAULT '',
    bound_user_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, bound_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    message_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL, rating INTEGER NOT NULL, created_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feedback_scope ON feedback(owner_id, created_at);
CREATE TABLE IF NOT EXISTS feedback_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    owner_id TEXT NOT NULL, book_id TEXT NOT NULL, message_id TEXT NOT NULL,
    rating INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '', question TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feedback_archive_age ON feedback_archive(created_at);
CREATE TABLE IF NOT EXISTS feedback_stats (
    period_start TEXT PRIMARY KEY, period_end TEXT NOT NULL,
    up INTEGER NOT NULL, down INTEGER NOT NULL, clears INTEGER NOT NULL,
    reasons TEXT NOT NULL, questions TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
    owner_id TEXT NOT NULL DEFAULT '', level TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS app_logs_scope ON app_logs(owner_id, id);
CREATE INDEX IF NOT EXISTS app_logs_age ON app_logs(created_at);
"""


class Database:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "study.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            self._migrate_shared_books(db)
            # Older deployments created feedback without the reason column.
            columns = [row[1] for row in db.execute("PRAGMA table_info(feedback)")]
            if "reason" not in columns:
                db.execute("ALTER TABLE feedback ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            # Older deployments created users without the BYO api_key column.
            columns = [row[1] for row in db.execute("PRAGMA table_info(users)")]
            if "api_key" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN api_key TEXT NOT NULL DEFAULT ''")
            # Older deployments created conversations without the compaction
            # columns (rolling summary + watermark of the last covered message).
            columns = [row[1] for row in db.execute("PRAGMA table_info(conversations)")]
            if "summary" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
            if "summary_mark" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN summary_mark TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens)")
            db.executescript("""
                CREATE TRIGGER IF NOT EXISTS chunks_delete_fts AFTER DELETE ON chunks BEGIN
                    DELETE FROM chunks_fts WHERE rowid=old.id;
                END;
            """)
            # A single web worker owns indexing jobs; interrupted work can be retried.
            db.execute("UPDATE books SET status='error', error=? WHERE status IN ('queued','indexing')",
                       ("上次索引被服务重启中断，请点击重新索引。",))

    @staticmethod
    def _migrate_shared_books(db):
        """Rebuild conversations/messages so shared builtin books can host
        per-user conversations. Old schema used composite foreign keys that
        required (owner_id, book_id) to exist in books for the SAME owner."""
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='conversations'").fetchone()
        if not row or "REFERENCES books(owner_id, id)" not in row["sql"]:
            return
        db.execute("PRAGMA foreign_keys=OFF")
        db.executescript("""
            BEGIN;
            CREATE TABLE conversations_v2 (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                title TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
                UNIQUE(owner_id, book_id, id)
            );
            INSERT INTO conversations_v2 SELECT id,owner_id,book_id,title,created_at FROM conversations;
            CREATE TABLE messages_v2 (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, book_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                mode TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            INSERT INTO messages_v2 SELECT id,owner_id,book_id,conversation_id,role,content,mode,payload,created_at FROM messages;
            DROP TABLE messages;
            DROP TABLE conversations;
            ALTER TABLE conversations_v2 RENAME TO conversations;
            ALTER TABLE messages_v2 RENAME TO messages;
            CREATE INDEX conversations_scope ON conversations(owner_id, book_id);
            CREATE INDEX messages_scope ON messages(owner_id, book_id, conversation_id, created_at);
            COMMIT;
        """)
        db.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def public_book(row):
    result = dict(row)
    result["builtin"] = result.pop("owner_id", "") == BUILTIN_OWNER
    result.pop("source_path", None)
    return result


def public_message(row):
    result = json.loads(row["payload"])
    result.update({key: row[key] for key in ("id", "role", "content", "mode", "created_at")})
    return result
