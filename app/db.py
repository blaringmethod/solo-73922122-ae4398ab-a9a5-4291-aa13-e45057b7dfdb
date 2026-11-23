"""SQLite 持久层：申报、核定版、预览快照。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

from flask import current_app, g

from .supply import replace_interval

# 按录音的换版互斥锁：同一录音的换版在进程内串行，杜绝 WAL 快照竞争。
_switch_locks: dict[int, threading.Lock] = {}
_switch_locks_guard = threading.Lock()


def _get_switch_lock(recording_id: int) -> threading.Lock:
    with _switch_locks_guard:
        lock = _switch_locks.get(recording_id)
        if lock is None:
            lock = threading.Lock()
            _switch_locks[recording_id] = lock
        return lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS recording (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS declaration (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recording(id),
    order_no     TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    shares_json  TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, order_no)
);

CREATE TABLE IF NOT EXISTS declaration_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    declaration_id INTEGER NOT NULL REFERENCES declaration(id),
    order_no       TEXT NOT NULL,
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    shares_json    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    changed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settlement_version (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id     INTEGER NOT NULL REFERENCES recording(id),
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    version_no       INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'published',
    returned_orders  TEXT NOT NULL,
    retained_orders  TEXT NOT NULL,
    segments_json    TEXT NOT NULL,
    input_signature  TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, period_start, period_end, version_no)
);

CREATE TABLE IF NOT EXISTS preview_snapshot (
    token          TEXT PRIMARY KEY,
    recording_id   INTEGER NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    input_signature TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 供数批号：每次成功换版生成一条；result_json 为当时完整供数结果快照，
-- 旧批结果不随后续换版改变（按批号复查时按原样取数）。
CREATE TABLE IF NOT EXISTS supply_batch (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id      INTEGER NOT NULL REFERENCES recording(id),
    batch_no          TEXT NOT NULL,
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    target_version_id INTEGER NOT NULL REFERENCES settlement_version(id),
    switch_start      TEXT NOT NULL,
    switch_end        TEXT NOT NULL,
    result_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, batch_no)
);

-- 现行供数（物化视图）：同一录音的来源段互不相交，查现行份额直接读本表，
-- 无需重放换版历史。updated_batch_id 记录该段最后一次被哪次换版写入。
CREATE TABLE IF NOT EXISTS supply_assignment (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id      INTEGER NOT NULL REFERENCES recording(id),
    start_date        TEXT NOT NULL,
    end_date          TEXT NOT NULL,
    source_version_id INTEGER NOT NULL REFERENCES settlement_version(id),
    updated_batch_id  INTEGER NOT NULL REFERENCES supply_batch(id),
    UNIQUE (recording_id, start_date, end_date)
);

CREATE INDEX IF NOT EXISTS idx_supply_batch_rec
    ON supply_batch(recording_id, id);
CREATE INDEX IF NOT EXISTS idx_assignment_rec
    ON supply_assignment(recording_id, start_date, end_date);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            # autocommit 模式：事务完全由显式 BEGIN/COMMIT/ROLLBACK 控制，
            # 避免驱动隐式延迟事务与 BEGIN IMMEDIATE 冲突（并发换版可见性）。
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # 并发换版写事务在锁上等待，而不是立刻抛 database is locked
        conn.execute("PRAGMA busy_timeout = 10000")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- 录音 / 申报 ----------

def get_or_create_recording(title: str) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM recording WHERE title = ?", (title,)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO recording (title) VALUES (?)", (title,))
    conn.commit()
    return cur.lastrowid


def list_recordings() -> list[sqlite3.Row]:
    return get_db().execute("SELECT * FROM recording ORDER BY title").fetchall()


def get_recording(recording_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM recording WHERE id = ?", (recording_id,)
    ).fetchone()


def declaration_count(recording_id: int) -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS c FROM declaration WHERE recording_id = ?",
        (recording_id,),
    ).fetchone()["c"]


def _row_to_decl(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "recording_id": row["recording_id"],
        "order_no": row["order_no"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "shares": json.loads(row["shares_json"]),
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def list_declarations(recording_id: int) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM declaration WHERE recording_id = ? ORDER BY order_no",
        (recording_id,),
    ).fetchall()
    return [_row_to_decl(r) for r in rows]


def get_declaration(declaration_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
    ).fetchone()
    return _row_to_decl(row) if row else None


def find_declaration(recording_id: int, order_no: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM declaration WHERE recording_id = ? AND order_no = ?",
        (recording_id, order_no),
    ).fetchone()
    return _row_to_decl(row) if row else None


def upsert_declaration(
    recording_id: int,
    order_no: str,
    start_date: str,
    end_date: str,
    shares: dict[str, int],
) -> tuple[dict, bool]:
    """新增或更正申报；更正时把旧版写入 history。返回 (申报, 是否更正)。"""
    conn = get_db()
    shares_json = json.dumps(shares, ensure_ascii=False, sort_keys=True)
    existing = conn.execute(
        "SELECT * FROM declaration WHERE recording_id = ? AND order_no = ?",
        (recording_id, order_no),
    ).fetchone()

    if existing is None:
        if declaration_count(recording_id) >= 12:
            raise ValueError("每首录音最多 12 单申报")
        cur = conn.execute(
            """INSERT INTO declaration
               (recording_id, order_no, start_date, end_date, shares_json)
               VALUES (?, ?, ?, ?, ?)""",
            (recording_id, order_no, start_date, end_date, shares_json),
        )
        conn.commit()
        return get_declaration(cur.lastrowid), False

    conn.execute(
        """INSERT INTO declaration_history
           (declaration_id, order_no, start_date, end_date, shares_json, version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            existing["id"],
            existing["order_no"],
            existing["start_date"],
            existing["end_date"],
            existing["shares_json"],
            existing["version"],
        ),
    )
    conn.execute(
        """UPDATE declaration
           SET start_date = ?, end_date = ?, shares_json = ?,
               version = version + 1, updated_at = datetime('now')
           WHERE id = ?""",
        (start_date, end_date, shares_json, existing["id"]),
    )
    conn.commit()
    return get_declaration(existing["id"]), True


def list_history(declaration_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM declaration_history WHERE declaration_id = ? ORDER BY version",
        (declaration_id,),
    ).fetchall()


# ---------- 预览快照 ----------

def save_preview(token: str, recording_id: int, start: str, end: str,
                  result: dict, signature: str) -> None:
    conn = get_db()
    conn.execute(
        """INSERT INTO preview_snapshot
           (token, recording_id, period_start, period_end, result_json, input_signature)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token, recording_id, start, end,
         json.dumps(result, ensure_ascii=False), signature),
    )
    conn.commit()


def get_preview(token: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM preview_snapshot WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    return {
        "token": row["token"],
        "recording_id": row["recording_id"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "result": json.loads(row["result_json"]),
        "input_signature": row["input_signature"],
    }


# ---------- 核定版 ----------

def list_versions(recording_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM settlement_version WHERE recording_id = ? "
        "ORDER BY period_start, period_end, version_no",
        (recording_id,),
    ).fetchall()


def get_version(version_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM settlement_version WHERE id = ?", (version_id,)
    ).fetchone()


def next_version_no(recording_id: int, start: str, end: str) -> int:
    row = get_db().execute(
        "SELECT COALESCE(MAX(version_no), 0) AS m FROM settlement_version "
        "WHERE recording_id = ? AND period_start = ? AND period_end = ?",
        (recording_id, start, end),
    ).fetchone()
    return row["m"] + 1


def publish_version(recording_id: int, start: str, end: str, result: dict,
                    signature: str, fail: bool = False) -> int:
    """发布核定版；fail=True 时模拟写入失败（事务回滚，不产生新版）。"""
    conn = get_db()
    try:
        version_no = next_version_no(recording_id, start, end)
        if fail:
            raise sqlite3.OperationalError("模拟写入失败")
        conn.execute(
            """INSERT INTO settlement_version
               (recording_id, period_start, period_end, version_no, status,
                returned_orders, retained_orders, segments_json, input_signature)
               VALUES (?, ?, ?, ?, 'published', ?, ?, ?, ?)""",
            (
                recording_id, start, end, version_no,
                json.dumps(result["returned"], ensure_ascii=False),
                json.dumps(result["retain"], ensure_ascii=False),
                json.dumps(result["segments"], ensure_ascii=False),
                signature,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM settlement_version WHERE recording_id = ? "
            "AND period_start = ? AND period_end = ? AND version_no = ?",
            (recording_id, start, end, version_no),
        ).fetchone()
        return row["id"]
    except Exception:
        conn.rollback()
        raise


# ---------- 供数批号 / 现行供数 ----------

def list_assignments(recording_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM supply_assignment WHERE recording_id = ? "
        "ORDER BY start_date, end_date",
        (recording_id,),
    ).fetchall()


def list_batches(recording_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM supply_batch WHERE recording_id = ? ORDER BY id",
        (recording_id,),
    ).fetchall()


def get_batch(batch_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM supply_batch WHERE id = ?", (batch_id,)
    ).fetchone()


def latest_batch(recording_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM supply_batch WHERE recording_id = ? "
        "ORDER BY id DESC LIMIT 1", (recording_id,)
    ).fetchone()


def _open_writer() -> sqlite3.Connection:
    """换版写事务专用连接：独立于请求连接，打开后无早期读，
    BEGIN IMMEDIATE 取锁时拿到的是最新 WAL 快照，避免请求连接在
    进入事务前已固定旧快照而覆盖并发换版的不相交改动。"""
    conn = sqlite3.connect(
        current_app.config["DATABASE"],
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit：事务由显式 BEGIN/COMMIT 控制
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def perform_switch(
    recording_id: int,
    version: sqlite3.Row,
    switch_start: str,
    switch_end: str,
    build_snapshot,
    fail: bool = False,
) -> dict:
    """在一个串行化写事务内完成换版。

    并发正确性（SQLite WAL 语义）
    -----------------------------
    一个连接可能在其他换版事务提交之前就排队拿到写锁，此时它的读快照仍停留在
    获锁瞬间，直接写入会覆盖并发换版的不相交改动。这里用两道措施保证正确：

    1. 进程内按录音的互斥锁（_get_switch_lock）：同一录音的换版完全串行，
       后一个换版等前一个提交并释放锁后才开始，天然在最新快照上运行。
    2. 写事务使用独立的专用连接（_open_writer），无请求期早期读，取锁即见
       最新已提交数据，并以 BEGIN IMMEDIATE 取得数据库写锁。

    任一步失败（含 fail=True 模拟失败）整体回滚，继续使用原批号和份额。

    build_snapshot: 回调，入参为 (新来源段列表, 事务内版本读取函数)，
    返回该批完整结果快照 dict。
    """
    # 进程内按录音加互斥锁：同一录音的换版完全串行，每个写事务都在前一个
    # 事务提交并释放锁之后才开始，从而在最新快照上读取来源段，彻底避免
    # SQLite WAL 下“排队获锁但读快照停留在获锁前”导致的并发覆盖。
    # 不同录音使用不同锁，互不阻塞。
    with _get_switch_lock(recording_id):
        return _perform_switch_locked(
            recording_id, version, switch_start, switch_end,
            build_snapshot, fail,
        )


def _perform_switch_locked(
    recording_id: int,
    version: sqlite3.Row,
    switch_start: str,
    switch_end: str,
    build_snapshot,
    fail: bool,
) -> dict:
    # 专用连接：独立于请求连接，无早期读，取锁即见最新已提交快照。
    conn = _open_writer()
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = [
            {
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "source_version_id": r["source_version_id"],
                "updated_batch_id": r["updated_batch_id"],
            }
            for r in conn.execute(
                "SELECT * FROM supply_assignment WHERE recording_id = ? "
                "ORDER BY start_date, end_date",
                (recording_id,),
            ).fetchall()
        ]

        row = conn.execute(
            "SELECT COUNT(*) AS c FROM supply_batch WHERE recording_id = ?",
            (recording_id,),
        ).fetchone()
        batch_no = f"B{row['c'] + 1:04d}"
        cur = conn.execute(
            """INSERT INTO supply_batch
               (recording_id, batch_no, period_start, period_end,
                target_version_id, switch_start, switch_end, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, '')""",
            (
                recording_id, batch_no,
                switch_start, switch_end,
                version["id"], switch_start, switch_end,
            ),
        )
        batch_id = cur.lastrowid

        new_assignments = replace_interval(
            existing, switch_start, switch_end, version["id"], batch_id
        )
        conn.execute(
            "DELETE FROM supply_assignment WHERE recording_id = ?", (recording_id,)
        )
        conn.executemany(
            """INSERT INTO supply_assignment
               (recording_id, start_date, end_date, source_version_id, updated_batch_id)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    recording_id, a["start_date"], a["end_date"],
                    a["source_version_id"], a["updated_batch_id"],
                )
                for a in new_assignments
            ],
        )

        def txn_get_version(version_id: int) -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM settlement_version WHERE id = ?", (version_id,)
            ).fetchone()

        snapshot = build_snapshot(new_assignments, txn_get_version)
        conn.execute(
            "UPDATE supply_batch SET result_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), batch_id),
        )

        if fail:
            raise sqlite3.OperationalError("模拟写入失败")

        conn.commit()
        return {"batch_id": batch_id, "batch_no": batch_no,
                "assignments": new_assignments, "snapshot": snapshot}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 资料指纹 ----------

def input_signature(declarations: list[dict], start: str, end: str) -> str:
    """对结算期内相关申报做规范哈希，确认时与预览指纹核对。"""
    relevant = sorted(
        (d for d in declarations
         if d["end_date"] > start and d["start_date"] < end),
        key=lambda d: (d["order_no"], d["id"]),
    )
    payload = [
        {
            "order_no": d["order_no"],
            "start_date": d["start_date"],
            "end_date": d["end_date"],
            "shares": {k: d["shares"][k] for k in sorted(d["shares"])},
            "version": d["version"],
        }
        for d in relevant
    ]
    blob = json.dumps(
        {"period": [start, end], "declarations": payload},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
