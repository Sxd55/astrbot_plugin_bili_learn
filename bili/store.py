"""SQLite 审计 + bvid 去重与重试状态机。不是第二套向量库。"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .runlog import today_bj

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    bvid TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    doc_id TEXT NOT NULL DEFAULT '',
    chars INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    keyword TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    doc_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    material TEXT NOT NULL DEFAULT '',
    source_excerpt TEXT NOT NULL DEFAULT '',
    merged_round INTEGER NOT NULL DEFAULT 0,
    audit_at INTEGER NOT NULL DEFAULT 0,
    audit_status TEXT NOT NULL DEFAULT '',
    audit_note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS digests (
    keyword TEXT PRIMARY KEY,
    doc_name TEXT NOT NULL DEFAULT '',
    doc_id TEXT NOT NULL DEFAULT '',
    rounds INTEGER NOT NULL DEFAULT 0,
    sources INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '',
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    audit_at INTEGER NOT NULL DEFAULT 0,
    audit_status TEXT NOT NULL DEFAULT '',
    audit_note TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_stats (
    keyword TEXT NOT NULL,
    day TEXT NOT NULL,
    ingested INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (keyword, day)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    trigger TEXT NOT NULL DEFAULT 'manual',
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    ok INTEGER NOT NULL DEFAULT 1,
    candidates INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    ingested INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    kb_id TEXT NOT NULL DEFAULT '',
    embedding_provider TEXT NOT NULL DEFAULT '',
    rerank_provider TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'info',
    bvid TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id);
"""

FINAL_STATUSES = ("ingested", "excluded")
RETRY_LIMITS = {"no_subtitle": 2, "failed": 3}
RETRY_BASE_SECONDS = {"no_subtitle": 7 * 86400, "failed": 3600}
RETRY_MAX_SECONDS = {"no_subtitle": 7 * 86400, "failed": 86400}
KB_REASON_PREFIXES = ("kb_fail", "no_kb")


def now_ts() -> int:
    return int(time.time())


class AuditStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(runs)").fetchall()
        cols = {r[1] for r in rows}
        additions = {
            "run_id": "TEXT NOT NULL DEFAULT ''",
            "trigger": "TEXT NOT NULL DEFAULT 'manual'",
            "started_at": "INTEGER NOT NULL DEFAULT 0",
            "finished_at": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'completed'",
            "candidates": "INTEGER NOT NULL DEFAULT 0",
            "failed": "INTEGER NOT NULL DEFAULT 0",
            "kb_id": "TEXT NOT NULL DEFAULT ''",
            "embedding_provider": "TEXT NOT NULL DEFAULT ''",
            "rerank_provider": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in cols:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")

        video_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(videos)").fetchall()}
        video_additions = {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_retry_at": "INTEGER NOT NULL DEFAULT 0",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "duration": "INTEGER NOT NULL DEFAULT 0",
            "doc_name": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT ''",
            "material": "TEXT NOT NULL DEFAULT ''",
            "source_excerpt": "TEXT NOT NULL DEFAULT ''",
            "merged_round": "INTEGER NOT NULL DEFAULT 0",
            "audit_at": "INTEGER NOT NULL DEFAULT 0",
            "audit_status": "TEXT NOT NULL DEFAULT ''",
            "audit_note": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in video_additions.items():
            if name not in video_cols:
                self._conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")

        legacy_failed = self._conn.execute(
            """SELECT id FROM runs WHERE run_id='' OR run_id IS NULL"""
        ).fetchall()
        for row in legacy_failed:
            self._conn.execute("UPDATE runs SET run_id=? WHERE id=?", (f"legacy-{row[0]}", row[0]))
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_run_id ON runs(run_id)")

        self._conn.execute(
            """UPDATE videos SET status='failed', attempts=MAX(attempts, 1),
                   next_retry_at=CASE WHEN next_retry_at>0 THEN next_retry_at ELSE updated_at+3600 END
               WHERE status='skipped' AND (reason LIKE 'llm_fail%' OR reason LIKE 'kb_fail%'
                   OR reason LIKE 'view_fail%' OR reason='no_kb')"""
        )
        self._conn.execute(
            """UPDATE videos SET status='no_subtitle', attempts=MAX(attempts, 1),
                   next_retry_at=CASE WHEN next_retry_at>0 THEN next_retry_at ELSE updated_at+604800 END
               WHERE status='skipped' AND reason='no_subtitle'"""
        )
        self._conn.execute("UPDATE videos SET status='excluded' WHERE status='skipped'")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def get(self, bvid: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM videos WHERE bvid=?", (bvid,))
        return dict(rows[0]) if rows else None

    def should_process(self, bvid: str) -> tuple[bool, str]:
        row = self.get(bvid)
        if row is None:
            return True, "new"
        status = str(row.get("status") or "")
        if status in FINAL_STATUSES:
            return False, status
        reason = str(row.get("reason") or "")
        attempts = int(row.get("attempts") or 0)
        if status == "deferred":
            next_ts = int(row.get("next_retry_at") or 0)
            if next_ts and now_ts() < next_ts:
                return False, "cooldown"
            return True, "deferred"
        if status == "failed" and reason.startswith(KB_REASON_PREFIXES):
            next_ts = int(row.get("next_retry_at") or 0)
            if next_ts and now_ts() < next_ts:
                return False, "cooldown"
            return True, "kb_retry"
        limit = RETRY_LIMITS.get(status)
        if limit is not None:
            if attempts >= limit:
                return False, f"{status}_given_up"
            next_ts = int(row.get("next_retry_at") or 0)
            if next_ts and now_ts() < next_ts:
                return False, "cooldown"
            return True, status
        return True, status or "legacy"

    def _upsert(
        self,
        bvid: str,
        *,
        title: str = "",
        author: str = "",
        status: str,
        reason: str = "",
        doc_id: str = "",
        chars: int = 0,
        source: str = "",
        keyword: str = "",
        duration: int = 0,
        summary: str = "",
        doc_name: str = "",
        category: str = "",
        material: str = "",
        source_excerpt: str = "",
        attempts: int = 0,
        next_retry_at: int = 0,
    ) -> None:
        now = now_ts()
        self.execute(
            """INSERT INTO videos(bvid, title, author, status, reason, doc_id, chars, source, keyword,
                                   duration, summary, doc_name, category, material, source_excerpt,
                                   attempts, next_retry_at, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bvid) DO UPDATE SET
                 title=excluded.title, author=excluded.author, status=excluded.status,
                 reason=excluded.reason, doc_id=excluded.doc_id, chars=excluded.chars,
                 source=excluded.source, keyword=excluded.keyword, duration=excluded.duration,
                 summary=CASE WHEN excluded.summary!='' THEN excluded.summary ELSE videos.summary END,
                 doc_name=CASE WHEN excluded.doc_name!='' THEN excluded.doc_name ELSE videos.doc_name END,
                 category=CASE WHEN excluded.category!='' THEN excluded.category ELSE videos.category END,
                 material=CASE WHEN excluded.material!='' THEN excluded.material ELSE videos.material END,
                 source_excerpt=CASE WHEN excluded.source_excerpt!='' THEN excluded.source_excerpt ELSE videos.source_excerpt END,
                 attempts=excluded.attempts, next_retry_at=excluded.next_retry_at,
                 updated_at=excluded.updated_at""",
            (
                bvid, title, author, status, reason, doc_id, chars, source, keyword,
                duration, summary, doc_name, category, material, source_excerpt,
                attempts, next_retry_at, now, now,
            ),
        )

    def mark_ingested(
        self,
        bvid: str,
        *,
        title: str = "",
        author: str = "",
        doc_id: str = "",
        chars: int = 0,
        summary: str = "",
        doc_name: str = "",
        category: str = "",
        material: str = "",
        source_excerpt: str = "",
        source: str = "",
        keyword: str = "",
        duration: int = 0,
    ) -> None:
        self._upsert(
            bvid,
            title=title,
            author=author,
            status="ingested",
            reason="ok",
            doc_id=doc_id,
            chars=chars,
            summary=summary,
            doc_name=doc_name,
            category=category,
            material=material,
            source_excerpt=source_excerpt,
            source=source,
            keyword=keyword,
            duration=duration,
            attempts=0,
            next_retry_at=0,
        )

    def mark_excluded(
        self,
        bvid: str,
        reason: str,
        *,
        title: str = "",
        author: str = "",
        source: str = "",
        keyword: str = "",
        duration: int = 0,
        summary: str = "",
        category: str = "",
    ) -> None:
        self._upsert(
            bvid,
            title=title,
            author=author,
            status="excluded",
            reason=reason,
            summary=summary,
            category=category,
            source=source,
            keyword=keyword,
            duration=duration,
        )

    def mark_deferred(
        self,
        bvid: str,
        reason: str,
        next_retry_at: int,
        *,
        title: str = "",
        author: str = "",
        source: str = "",
        keyword: str = "",
        duration: int = 0,
        summary: str = "",
        doc_name: str = "",
        category: str = "",
        material: str = "",
        source_excerpt: str = "",
    ) -> None:
        row = self.get(bvid)
        attempts = int(row.get("attempts") or 0) if row else 0
        self._upsert(
            bvid,
            title=title,
            author=author,
            status="deferred",
            reason=reason,
            summary=summary,
            doc_name=doc_name,
            category=category,
            material=material,
            source_excerpt=source_excerpt,
            source=source,
            keyword=keyword,
            duration=duration,
            attempts=attempts,
            next_retry_at=next_retry_at,
        )

    def mark_retryable(
        self,
        bvid: str,
        status: str,
        reason: str,
        *,
        title: str = "",
        author: str = "",
        source: str = "",
        keyword: str = "",
        duration: int = 0,
    ) -> None:
        row = self.get(bvid)
        attempts = int(row.get("attempts") or 0) + 1 if row else 1
        limit = RETRY_LIMITS.get(status, 1)
        base = RETRY_BASE_SECONDS.get(status, 3600)
        cap = RETRY_MAX_SECONDS.get(status, 86400)
        delay = min(cap, base * (6 ** max(0, attempts - 1)))
        if status == "failed" and reason.startswith(KB_REASON_PREFIXES):
            # 知识库故障不属于视频本身的问题：不设次数上限，但要按退避冷却，避免每轮重抓重调模型。
            next_retry_at = now_ts() + delay
        else:
            next_retry_at = now_ts() + delay if attempts < limit else 0
        self._upsert(
            bvid,
            title=title,
            author=author,
            status=status,
            reason=reason[:400],
            source=source,
            keyword=keyword,
            duration=duration,
            attempts=attempts,
            next_retry_at=next_retry_at,
        )

    def update_summary(self, bvid: str, summary: str, doc_name: str = "") -> None:
        if doc_name:
            self.execute(
                "UPDATE videos SET summary=?, doc_name=?, updated_at=? WHERE bvid=?",
                (summary, doc_name, now_ts(), bvid),
            )
            return
        self.execute(
            "UPDATE videos SET summary=?, updated_at=? WHERE bvid=?",
            (summary, now_ts(), bvid),
        )

    def start_run(self, run_id: str, trigger: str = "manual", kb_id: str = "", embedding_provider: str = "", rerank_provider: str = "") -> None:
        now = now_ts()
        self.execute(
            """INSERT INTO runs(run_id, trigger, started_at, status, kb_id, embedding_provider, rerank_provider)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET trigger=excluded.trigger, started_at=excluded.started_at,
               status='running', finished_at=0, kb_id=excluded.kb_id,
               embedding_provider=excluded.embedding_provider, rerank_provider=excluded.rerank_provider""",
            (run_id, trigger, now, "running", kb_id, embedding_provider, rerank_provider),
        )
        self.add_event(run_id, "run", "started", "任务触发")

    def finish_run(self, run_id: str, status: str, candidates: int, processed: int, ingested: int, skipped: int, failed: int, detail: str = "") -> None:
        self.execute(
            """UPDATE runs SET finished_at=?, status=?, ok=?, candidates=?, processed=?, ingested=?, skipped=?, failed=?, detail=? WHERE run_id=?""",
            (now_ts(), status, int(status == "completed"), candidates, processed, ingested, skipped, failed, detail[:400], run_id),
        )
        self.add_event(run_id, "run", status, detail)

    def add_event(self, run_id: str, stage: str, status: str = "info", message: str = "", bvid: str = "", detail: str = "") -> None:
        self.execute(
            "INSERT INTO run_events(run_id, ts, stage, status, bvid, message, detail) VALUES(?,?,?,?,?,?,?)",
            (run_id, now_ts(), stage, status, bvid, message[:240], detail[:800]),
        )

    def get_meta(self, key: str) -> str:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return str(rows[0][0]) if rows else ""

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            """INSERT INTO meta(key, value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def run_events(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM run_events WHERE run_id=? ORDER BY id LIMIT ?", (run_id, limit))
        return [dict(r) for r in rows]

    def last_run(self) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def add_run(self, ok: bool, processed: int, ingested: int, skipped: int, detail: str = "") -> None:
        run_id = f"legacy-{now_ts()}"
        self.start_run(run_id)
        self.finish_run(run_id, "completed" if ok else "failed", processed, processed, ingested, skipped, 0 if ok else skipped, detail)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM videos ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def recent_brief(self, limit: int = 20) -> list[dict[str, Any]]:
        """列表展示用：不带 summary / source_excerpt，避免接口返回过大。"""
        rows = self.query(
            """SELECT bvid, title, author, status, reason, doc_id, doc_name, category,
                      duration, source, keyword, attempts, merged_round,
                      audit_at, audit_status, audit_note, created_at, updated_at
               FROM videos ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        def n(sql: str) -> int:
            return int(self.query(sql)[0][0] or 0)

        return {
            "videos": n("SELECT COUNT(*) FROM videos"),
            "ingested": n("SELECT COUNT(*) FROM videos WHERE status='ingested'"),
            "excluded": n("SELECT COUNT(*) FROM videos WHERE status='excluded'"),
            "no_subtitle": n("SELECT COUNT(*) FROM videos WHERE status='no_subtitle'"),
            "failed": n("SELECT COUNT(*) FROM videos WHERE status='failed'"),
            "deferred": n("SELECT COUNT(*) FROM videos WHERE status='deferred'"),
            "merged": n("SELECT COUNT(*) FROM videos WHERE merged_round>0"),
            "digests": n("SELECT COUNT(*) FROM digests WHERE rounds>0"),
        }

    def quota_used(self, keyword: str, day: str = "") -> int:
        day = day or today_bj()
        rows = self.query(
            "SELECT ingested FROM daily_stats WHERE keyword=? AND day=?", (keyword, day)
        )
        return int(rows[0][0]) if rows else 0

    def quota_add(self, keyword: str, day: str = "", amount: int = 1) -> None:
        day = day or today_bj()
        self.execute(
            """INSERT INTO daily_stats(keyword, day, ingested) VALUES(?,?,?)
               ON CONFLICT(keyword, day) DO UPDATE SET
                 ingested=daily_stats.ingested+excluded.ingested""",
            (keyword, day, max(0, int(amount))),
        )

    def daily_counts(self, day: str = "") -> dict[str, int]:
        day = day or today_bj()
        rows = self.query(
            "SELECT keyword, ingested FROM daily_stats WHERE day=? ORDER BY keyword", (day,)
        )
        return {str(r[0]): int(r[1]) for r in rows}

    def reset_daily(self, day: str = "") -> None:
        day = day or today_bj()
        self.execute("DELETE FROM daily_stats WHERE day=?", (day,))

    def unmerged_counts(self) -> dict[str, int]:
        rows = self.query(
            """SELECT category, COUNT(*) FROM videos
               WHERE status='ingested' AND merged_round=0 AND category!=''
               GROUP BY category ORDER BY COUNT(*) DESC""",
        )
        return {str(r[0]): int(r[1]) for r in rows}

    def unmerged_sources(self, category: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT bvid, title, summary, doc_name, doc_id, source_excerpt, material
               FROM videos WHERE status='ingested' AND merged_round=0 AND category=?
               ORDER BY updated_at ASC LIMIT ?""",
            (category, limit),
        )
        return [dict(r) for r in rows]

    def mark_merged(self, bvids: list[str], round_no: int) -> None:
        now = now_ts()
        for bvid in bvids:
            self.execute(
                "UPDATE videos SET merged_round=?, updated_at=? WHERE bvid=?",
                (round_no, now, bvid),
            )

    def doc_id_ref_count(self, doc_id: str) -> int:
        if not doc_id:
            return 0
        rows = self.query(
            "SELECT COUNT(*) FROM videos WHERE doc_id=? AND doc_id!=''", (doc_id,)
        )
        return int(rows[0][0] or 0)

    def get_digest(self, keyword: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM digests WHERE keyword=?", (keyword,))
        return dict(rows[0]) if rows else None

    def save_digest(
        self,
        keyword: str,
        *,
        doc_name: str = "",
        doc_id: str = "",
        rounds: int = 0,
        sources: int = 0,
        content: str = "",
        error: str = "",
    ) -> None:
        self.execute(
            """INSERT INTO digests(keyword, doc_name, doc_id, rounds, sources, content,
                                   last_attempt_at, last_error, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(keyword) DO UPDATE SET
                 doc_name=excluded.doc_name, doc_id=excluded.doc_id, rounds=excluded.rounds,
                 sources=excluded.sources, content=excluded.content,
                 last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error,
                 updated_at=excluded.updated_at""",
            (keyword, doc_name, doc_id, rounds, sources, content, now_ts(), error[:400], now_ts()),
        )

    def all_digests(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM digests ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def all_digest_briefs(self) -> list[dict[str, Any]]:
        """汇总列表展示用：不带 content，只列真正写入过的汇总。"""
        rows = self.query(
            """SELECT keyword, doc_name, doc_id, rounds, sources,
                      audit_at, audit_status, audit_note, updated_at
               FROM digests WHERE rounds>0 ORDER BY updated_at DESC"""
        )
        return [dict(r) for r in rows]

    def digest_sources(self, keyword: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT bvid, title, summary FROM videos
               WHERE status='ingested' AND category=? AND merged_round>0
               ORDER BY updated_at ASC LIMIT ?""",
            (keyword, limit),
        )
        return [dict(r) for r in rows]

    def audit_due_videos(self, interval_days: int, limit: int) -> list[dict[str, Any]]:
        cutoff = now_ts() - max(1, int(interval_days)) * 86400
        rows = self.query(
            """SELECT bvid, title, summary, source_excerpt, category, doc_name
               FROM videos
               WHERE status='ingested' AND summary!='' AND source_excerpt!=''
                 AND (audit_at=0 OR audit_at<?)
               ORDER BY audit_at ASC, updated_at ASC LIMIT ?""",
            (cutoff, max(1, int(limit))),
        )
        return [dict(r) for r in rows]

    def audit_due_digests(self, interval_days: int, limit: int) -> list[dict[str, Any]]:
        cutoff = now_ts() - max(1, int(interval_days)) * 86400
        rows = self.query(
            """SELECT keyword, doc_name, content FROM digests
               WHERE content!='' AND (audit_at=0 OR audit_at<?)
               ORDER BY audit_at ASC, updated_at ASC LIMIT ?""",
            (cutoff, max(1, int(limit))),
        )
        return [dict(r) for r in rows]

    def audited_since(self, ts: int) -> int:
        rows = self.query("SELECT COUNT(*) FROM videos WHERE audit_at>=?", (ts,))
        count = int(rows[0][0] or 0)
        rows = self.query("SELECT COUNT(*) FROM digests WHERE audit_at>=?", (ts,))
        return count + int(rows[0][0] or 0)

    def mark_audit(self, bvid: str, status: str, note: str = "") -> None:
        self.execute(
            "UPDATE videos SET audit_at=?, audit_status=?, audit_note=?, updated_at=? WHERE bvid=?",
            (now_ts(), status, note[:400], now_ts(), bvid),
        )

    def mark_digest_audit(self, keyword: str, status: str, note: str = "") -> None:
        self.execute(
            "UPDATE digests SET audit_at=?, audit_status=?, audit_note=?, updated_at=? WHERE keyword=?",
            (now_ts(), status, note[:400], now_ts(), keyword),
        )

    def audit_stats(self) -> dict[str, int]:
        def n(sql: str) -> int:
            return int(self.query(sql)[0][0] or 0)

        return {
            "ok": n("SELECT COUNT(*) FROM videos WHERE audit_status='ok'"),
            "suspect": n("SELECT COUNT(*) FROM videos WHERE audit_status='suspect'"),
            "error": n("SELECT COUNT(*) FROM videos WHERE audit_status='error'"),
            "pending": n("SELECT COUNT(*) FROM videos WHERE status='ingested' AND audit_at=0 AND source_excerpt!=''"),
            "digest_ok": n("SELECT COUNT(*) FROM digests WHERE audit_status='ok'"),
            "digest_suspect": n("SELECT COUNT(*) FROM digests WHERE audit_status='suspect'"),
        }

    def recent_suspects(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.query(
            """SELECT bvid, title, category, audit_note, audit_at FROM videos
               WHERE audit_status='suspect' ORDER BY audit_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]
