from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, SEGMENT_STATUSES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        seg_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in SEGMENT_STATUSES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS shoreline_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    location TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ({seg_statuses})),
                    film_threshold_um REAL NOT NULL,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    active_job_id INTEGER,
                    completed_by TEXT,
                    completed_at TEXT,
                    film_thickness_um REAL,
                    cleaned_quantity REAL,
                    reinspection_note TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, code)
                );
                CREATE TABLE IF NOT EXISTS segment_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    segment_id INTEGER NOT NULL
                        REFERENCES shoreline_segments(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    team TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    claimed_by TEXT NOT NULL,
                    finished_at TEXT,
                    finished_by TEXT,
                    film_thickness_um REAL,
                    cleaned_quantity REAL,
                    outcome TEXT CHECK(outcome IN ('completed','reinspection')),
                    completion_invalid INTEGER NOT NULL DEFAULT 0,
                    invalidated_at TEXT,
                    invalidated_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_segment_active_job
                    ON segment_jobs(segment_id) WHERE completion_invalid=0 AND finished_at IS NULL;
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 岸线段台账 ----
    def create_segment(self, item_id: int, code: str, location: str,
                       sensitivity: str, threshold_um: float,
                       actor: str) -> Dict[str, Any]:
        self.get_item(item_id)
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO shoreline_segments(item_id, code, location, sensitivity,
                       status, film_threshold_um, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?, 'open', ?, 1, ?, ?, ?)""",
                    (item_id, code, location, sensitivity, threshold_um, actor, now, now),
                )
                segment_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该事件下段编号已存在") from exc
        return self.get_segment(segment_id)

    def get_segment(self, segment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM shoreline_segments WHERE id=?", (segment_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("岸线段不存在")
        return dict(row)

    def list_segments(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM shoreline_segments"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def claim_segment(self, segment_id: int, team: str, actor: str,
                      expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            seg = self.conn.execute(
                "SELECT * FROM shoreline_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if seg is None:
                raise NotFoundError("岸线段不存在")
            if seg["status"] not in ("open", "reoiled"):
                raise ConflictError("该段已有未结束作业，不能重复领取")
            try:
                cur = self.conn.execute(
                    """INSERT INTO segment_jobs(segment_id, item_id, team,
                       claimed_at, claimed_by) VALUES(?,?,?,?,?)""",
                    (segment_id, seg["item_id"], team, now, actor),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该段已被其他队伍领取") from exc
            job_id = int(cur.lastrowid)
            updated = self.conn.execute(
                """UPDATE shoreline_segments
                   SET status='claimed', claimed_by=?, claimed_at=?, active_job_id=?,
                       completed_by=NULL, completed_at=NULL, film_thickness_um=NULL,
                       cleaned_quantity=NULL, reinspection_note=NULL, reviewed_by=NULL,
                       reviewed_at=NULL, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (team, now, job_id, now, segment_id, expected_version),
            )
            if updated.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_segment(segment_id)

    def complete_segment(self, segment_id: int, film_thickness_um: Optional[float],
                         cleaned_quantity: Optional[float], outcome: str,
                         actor: str, expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            seg = self.conn.execute(
                "SELECT * FROM shoreline_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if seg is None:
                raise NotFoundError("岸线段不存在")
            if seg["status"] != "claimed" or seg["active_job_id"] is None:
                raise ConflictError("只有清理中的段可以完成")
            self.conn.execute(
                """UPDATE segment_jobs SET finished_at=?, finished_by=?,
                   film_thickness_um=?, cleaned_quantity=?, outcome=?
                   WHERE id=?""",
                (now, actor, film_thickness_um, cleaned_quantity, outcome,
                 seg["active_job_id"]),
            )
            updated = self.conn.execute(
                """UPDATE shoreline_segments
                   SET status=?, completed_by=?, completed_at=?, film_thickness_um=?,
                       cleaned_quantity=?, active_job_id=NULL,
                       reinspection_note=CASE WHEN ?='reinspection'
                           THEN '实测油膜超阈值或资料不全，待复检' ELSE NULL END,
                       version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (outcome, actor, now, film_thickness_um, cleaned_quantity, outcome,
                 now, segment_id, expected_version),
            )
            if updated.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_segment(segment_id)

    def review_segment(self, segment_id: int, passed: bool,
                       note: Optional[str], actor: str,
                       expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            seg = self.conn.execute(
                "SELECT * FROM shoreline_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if seg is None:
                raise NotFoundError("岸线段不存在")
            if seg["status"] != "reinspection":
                raise ConflictError("只有待复检的段可以复核")
            if passed:
                target, re_note = "completed", None
            else:
                target, re_note = "open", (note or "复核未通过，重新领取清理")
            updated = self.conn.execute(
                """UPDATE shoreline_segments
                   SET status=?, reviewed_by=?, reviewed_at=?, reinspection_note=?,
                       claimed_by=CASE WHEN ?='open' THEN NULL ELSE claimed_by END,
                       claimed_at=CASE WHEN ?='open' THEN NULL ELSE claimed_at END,
                       version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, actor, now, re_note, target, target, now,
                 segment_id, expected_version),
            )
            if updated.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_segment(segment_id)

    def reoil_segment(self, segment_id: int, note: str, actor: str,
                      expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            seg = self.conn.execute(
                "SELECT * FROM shoreline_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if seg is None:
                raise NotFoundError("岸线段不存在")
            if seg["status"] not in ("completed", "reinspection"):
                raise ConflictError("只有已完成或待复检的段可以登记返油")
            self.conn.execute(
                """UPDATE segment_jobs SET completion_invalid=1, invalidated_at=?,
                   invalidated_reason='涨潮返油，原完成失效'
                   WHERE segment_id=? AND completion_invalid=0 AND finished_at IS NOT NULL""",
                (now, segment_id),
            )
            updated = self.conn.execute(
                """UPDATE shoreline_segments
                   SET status='reoiled', active_job_id=NULL, claimed_by=NULL,
                       claimed_at=NULL, reinspection_note=?, reviewed_by=NULL,
                       reviewed_at=NULL, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (note, now, segment_id, expected_version),
            )
            if updated.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_segment(segment_id)

    def list_segment_jobs(self, segment_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM segment_jobs WHERE segment_id=? ORDER BY id",
                (segment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
