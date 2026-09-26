"""SQLite 저장소.

- 모든 작품 데이터는 자료실(workspace) ID로 분리한다. 다른 자료실의 작품은 조회·분석할 수 없다.
- 계정·이메일·이름은 수집하지 않는다. 자료실 ID는 브라우저가 만든 무작위 값이다.
- 마스킹 기록에는 항목과 개수만 남기고 원래 값은 남기지 않는다.
- 마지막 이용 후 보관 기간이 지난 작품은 분석 결과와 함께 삭제한다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ingest.normalize import Paragraph

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    category TEXT NOT NULL,
    genre TEXT NOT NULL,
    verse INTEGER NOT NULL DEFAULT 0,
    source_kind TEXT NOT NULL,
    source_filename TEXT,
    intro_text TEXT,
    intro_source_url TEXT,
    bib_json TEXT,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_workspace ON works(workspace_id);
CREATE TABLE IF NOT EXISTS paragraphs (
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    label TEXT NOT NULL,
    section TEXT,
    text TEXT NOT NULL,
    PRIMARY KEY (work_id, idx)
);
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    option TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analyses_work ON analyses(work_id, kind, option);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS masking_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
    category TEXT NOT NULL,
    count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_docs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    source TEXT NOT NULL,
    url TEXT,
    tier TEXT NOT NULL CHECK (tier IN ('A', 'B', 'C')),
    license TEXT NOT NULL,
    work_title TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_chunks (
    ref_id TEXT NOT NULL REFERENCES reference_docs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (ref_id, idx)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(value: str | None):
    return json.loads(value) if value else None


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                # 여러 워커 프로세스가 같은 파일을 쓸 때 읽기·쓰기 충돌을 줄인다
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.executescript(SCHEMA)
        self._last_purge: float | None = None

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA foreign_keys = ON")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 작품 ──────────────────────────────────────────────
    def create_work(
        self,
        workspace_id: str,
        *,
        title: str,
        author: str | None,
        category: str,
        genre: str,
        verse: bool,
        source_kind: str,
        source_filename: str | None,
        intro_text: str | None,
        intro_source_url: str | None,
        bib: dict | None,
        paragraphs: list[Paragraph],
        mask_counts: dict[str, int],
    ) -> str:
        work_id = uuid.uuid4().hex
        ts = now_iso()
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO works (id, workspace_id, title, author, category, genre, verse, source_kind,
                   source_filename, intro_text, intro_source_url, bib_json, created_at, last_accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    work_id, workspace_id, title, author, category, genre, int(verse), source_kind,
                    source_filename, intro_text, intro_source_url,
                    json.dumps(bib, ensure_ascii=False) if bib else None, ts, ts,
                ),
            )
            conn.executemany(
                "INSERT INTO paragraphs (work_id, idx, label, section, text) VALUES (?, ?, ?, ?, ?)",
                [(work_id, p.index, p.label, p.section, p.text) for p in paragraphs],
            )
            conn.executemany(
                "INSERT INTO masking_log (workspace_id, work_id, category, count, created_at) VALUES (?, ?, ?, ?, ?)",
                [(workspace_id, work_id, code, n, ts) for code, n in mask_counts.items() if n],
            )
        return work_id

    def _work_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        data["verse"] = bool(data["verse"])
        data["bib"] = _loads(data.pop("bib_json"))
        return data

    def list_works(self, workspace_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT w.id, w.title, w.author, w.category, w.genre, w.source_kind, w.created_at,
                          w.last_accessed_at, (SELECT COUNT(*) FROM paragraphs p WHERE p.work_id = w.id) AS paragraph_count
                   FROM works w WHERE w.workspace_id = ? ORDER BY w.last_accessed_at DESC""",
                (workspace_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_work(self, workspace_id: str, work_id: str, touch: bool = True) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM works WHERE id = ? AND workspace_id = ?", (work_id, workspace_id)
            ).fetchone()
        if row is None:
            return None
        if touch:
            with self.tx() as conn:
                conn.execute("UPDATE works SET last_accessed_at = ? WHERE id = ?", (now_iso(), work_id))
        return self._work_dict(row)

    def get_paragraphs(self, work_id: str) -> list[Paragraph]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx, label, section, text FROM paragraphs WHERE work_id = ? ORDER BY idx", (work_id,)
            ).fetchall()
        return [Paragraph(r["idx"], r["label"], r["section"], r["text"]) for r in rows]

    def delete_work(self, workspace_id: str, work_id: str) -> bool:
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM works WHERE id = ? AND workspace_id = ?", (work_id, workspace_id))
        return cur.rowcount > 0

    # ── 분석 결과 ─────────────────────────────────────────
    def get_analysis(self, work_id: str, kind: str, option: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT result_json FROM analyses WHERE work_id = ? AND kind = ? AND option = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (work_id, kind, option),
            ).fetchone()
        return _loads(row["result_json"]) if row else None

    def list_analyses(self, work_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, option, result_json FROM analyses WHERE work_id = ? ORDER BY created_at, rowid", (work_id,)
            ).fetchall()
        return [{"kind": r["kind"], "option": r["option"], "result": _loads(r["result_json"])} for r in rows]

    def save_analysis(self, work_id: str, kind: str, option: str, result: dict) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM analyses WHERE work_id = ? AND kind = ? AND option = ?", (work_id, kind, option))
            conn.execute(
                "INSERT INTO analyses (id, work_id, kind, option, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, work_id, kind, option, json.dumps(result, ensure_ascii=False), now_iso()),
            )

    def save_question(self, workspace_id: str, work_id: str, question: str, result: dict, mask_counts: dict[str, int]) -> str:
        qid = uuid.uuid4().hex
        ts = now_iso()
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO questions (id, work_id, question, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (qid, work_id, question, json.dumps(result, ensure_ascii=False), ts),
            )
            conn.executemany(
                "INSERT INTO masking_log (workspace_id, work_id, category, count, created_at) VALUES (?, ?, ?, ?, ?)",
                [(workspace_id, work_id, code, n, ts) for code, n in mask_counts.items() if n],
            )
        return qid

    def list_questions(self, work_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, question, result_json, created_at FROM questions WHERE work_id = ? ORDER BY created_at, rowid",
                (work_id,),
            ).fetchall()
        return [
            {"id": r["id"], "question": r["question"], "result": _loads(r["result_json"]), "created_at": r["created_at"]}
            for r in rows
        ]

    # ── 개인정보: 마스킹 집계, 보관 기간 ─────────────────
    def masking_summary(self, workspace_id: str, days: int = 30) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._conn.execute(
                """SELECT category, SUM(count) AS total FROM masking_log
                   WHERE workspace_id = ? AND created_at >= ? GROUP BY category""",
                (workspace_id, cutoff),
            ).fetchall()
        return {r["category"]: r["total"] for r in rows}

    def purge_if_due(self, retention_days: int, interval_seconds: float = 3600) -> int | None:
        """마지막 정리 후 interval_seconds가 지났으면 보관 기간 정리를 실행한다(오래 켜 둔 서버용)."""
        current = time.monotonic()
        with self._lock:
            if self._last_purge is not None and current - self._last_purge < interval_seconds:
                return None
            self._last_purge = current
        return self.purge_inactive(retention_days)

    def purge_inactive(self, retention_days: int, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = (current - timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM works WHERE last_accessed_at < ?", (cutoff,))
            conn.execute("DELETE FROM masking_log WHERE created_at < ?", (cutoff,))
        return cur.rowcount

    # ── 공용 참고자료 ─────────────────────────────────────
    def add_reference(
        self,
        *,
        title: str,
        author: str | None,
        source: str,
        url: str | None,
        tier: str,
        license: str,
        work_title: str | None,
        chunks: list[str],
    ) -> str:
        ref_id = uuid.uuid4().hex
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO reference_docs (id, title, author, source, url, tier, license, work_title, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ref_id, title, author, source, url, tier, license, work_title, now_iso()),
            )
            conn.executemany(
                "INSERT INTO reference_chunks (ref_id, idx, text) VALUES (?, ?, ?)",
                [(ref_id, i, text) for i, text in enumerate(chunks)],
            )
        return ref_id

    def reference_fingerprint(self) -> tuple:
        """참고자료가 바뀌었는지 가볍게 확인하는 값(다른 프로세스·CLI에서 가져온 자료도 감지)."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*), MAX(rowid), MAX(created_at) FROM reference_docs").fetchone()
        return tuple(row)

    def all_reference_chunks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.ref_id, c.idx, c.text, d.title, d.author, d.source, d.url, d.tier, d.license, d.work_title
                   FROM reference_chunks c JOIN reference_docs d ON d.id = c.ref_id ORDER BY d.created_at, c.idx"""
            ).fetchall()
        return [dict(r) for r in rows]

    def list_references(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, author, source, url, tier, license, work_title, created_at FROM reference_docs ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]
