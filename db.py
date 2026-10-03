import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".neurosync" / "index.db"


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout + WAL нужны в каждом процессе (ns, бот), иначе пока ns
    # индексирует диск, бот ловит "database is locked" и молчит.
    conn = sqlite3.connect(DB_PATH, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error:
        pass
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY,
        name TEXT,
        ext TEXT,
        size INTEGER,
        mtime REAL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS tags (
        path TEXT,
        tag TEXT
    )
    """)

    conn.commit()
    conn.close()
