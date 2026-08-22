import sqlite3
from datetime import datetime

DB_PATH = "data.db"


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS failures (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                message   TEXT NOT NULL,
                context   TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fixes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                failure_id  INTEGER REFERENCES failures(id),
                description TEXT NOT NULL,
                applied_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id       TEXT NOT NULL,
                step         TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, step)
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                status      TEXT NOT NULL DEFAULT 'in_progress'
            );
        """)
        _migrate_fixes_table(conn)


def _migrate_fixes_table(conn: sqlite3.Connection) -> None:
    """Additive migration — CREATE TABLE IF NOT EXISTS won't add columns
    to a table that already exists, so new columns need an explicit,
    guarded ALTER TABLE. No migration framework here, so every change
    checks PRAGMA table_info first rather than assuming a fresh schema."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fixes)")}
    if "source_file" not in cols:
        conn.execute("ALTER TABLE fixes ADD COLUMN source_file TEXT")
    if "backup_path" not in cols:
        conn.execute("ALTER TABLE fixes ADD COLUMN backup_path TEXT")
    if "applied_to_source" not in cols:
        conn.execute("ALTER TABLE fixes ADD COLUMN applied_to_source INTEGER NOT NULL DEFAULT 0")
    if "promoted" not in cols:
        conn.execute("ALTER TABLE fixes ADD COLUMN promoted INTEGER NOT NULL DEFAULT 0")


def log_failure(message: str, context: str = None, path: str = DB_PATH) -> int:
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO failures (message, context, created_at) VALUES (?, ?, ?)",
            (message, context, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def log_fix(failure_id: int, description: str, path: str = DB_PATH) -> int:
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO fixes (failure_id, description, applied_at) VALUES (?, ?, ?)",
            (failure_id, description, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def mark_fix_applied(fix_id: int, source_file: str, backup_path: str, promoted: bool, path: str = DB_PATH) -> None:
    """Records whether a patch was written back to source, where the
    pre-patch backup lives, and whether it was auto-promoted or landed
    as a .patched file pending manual review. This is the audit trail
    that answers 'what changed, when, and can we roll it back.'"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """UPDATE fixes
               SET source_file = ?, backup_path = ?, applied_to_source = 1, promoted = ?
               WHERE id = ?""",
            (source_file, backup_path, int(promoted), fix_id),
        )


def save_checkpoint(run_id: str, step: str, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints (run_id, step, completed_at) VALUES (?, ?, ?)",
            (run_id, step, datetime.utcnow().isoformat()),
        )


def load_checkpoints(run_id: str, path: str = DB_PATH) -> set[str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT step FROM checkpoints WHERE run_id = ?", (run_id,)
        ).fetchall()
    return {row[0] for row in rows}


def start_run(run_id: str, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, started_at) VALUES (?, ?)",
            (run_id, datetime.utcnow().isoformat()),
        )


def finish_run(run_id: str, status: str, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
            (status, datetime.utcnow().isoformat(), run_id),
        )


def get_recent_runs(limit: int = 10, path: str = DB_PATH) -> list[dict]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT r.run_id, r.started_at, r.finished_at, r.status,
                   COUNT(c.step) AS steps_completed
            FROM runs r
            LEFT JOIN checkpoints c ON c.run_id = r.run_id
            GROUP BY r.run_id
            ORDER BY r.started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_success_rate(path: str = DB_PATH) -> float | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successful
            FROM runs
            WHERE status != 'in_progress'
            """
        ).fetchone()
    total, successful = row
    if not total:
        return None
    return round(successful / total, 4)