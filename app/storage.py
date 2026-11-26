# -*- coding: utf-8 -*-
"""SQLite 持久化层。

关键不变量
==========
* 一次"补单 / 按号换报"在 *单个事务* 内完成：先写入新单据集，再核算，
  核算通过（无矛盾）才把改正表快照一起提交；任何一步失败都 ROLLBACK，
  现行单据与现行改正表保持原样，仍可在网页查询。
* 改正表以 JSON 快照保存（NodeResult 全字段 + 核算时间戳），读表不重算。
* WAL + 每次操作独立连接：写事务进行中，其他请求的连接读到的仍是最后
  一次提交的现行单据与改正表，且不会被写锁阻塞。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .engine import ComputeReport, NodeResult, Sheet, ValidationError, compute

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    sid      TEXT PRIMARY KEY,
    point_a  TEXT NOT NULL,
    point_b  TEXT NOT NULL,
    lo       INTEGER NOT NULL,
    hi       INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    TEXT NOT NULL,
    computed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DATUM_KEY = "datum"
SPAN_LIMIT_KEY = "span_limit"


class StorageError(RuntimeError):
    pass


class ContradictionError(StorageError):
    def __init__(self, report: ComputeReport):
        self.report = report
        super().__init__("高差互相矛盾，存在冲突闭环，已保留现行单据与改正表")


class Store:
    def __init__(self, db_path: str, datum: str = "D0", span_limit: int = 20):
        self.db_path = db_path
        self.datum = datum
        self.span_limit = span_limit
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # 建库/建表与初始空快照在一个连接内完成
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (DATUM_KEY, datum),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (SPAN_LIMIT_KEY, str(span_limit)),
            )
            if conn.execute("SELECT 1 FROM snapshots WHERE id = 1").fetchone() is None:
                empty_report = compute([], datum=datum, span_limit=span_limit)
                conn.execute(
                    "INSERT INTO snapshots(id, payload, computed_at) VALUES (1, ?, ?)",
                    (json.dumps(self._serialize_report(empty_report), ensure_ascii=False),
                     time.time()),
                )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def close(self) -> None:
        """兼容旧调用；连接按操作开闭，此处无资源持有。"""
        return None

    # ------------------------------------------------------------------ #
    def list_sheets(self) -> List[Sheet]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT sid, point_a, point_b, lo, hi FROM sheets ORDER BY sid"
            ).fetchall()
            return [Sheet(r["sid"], r["point_a"], r["point_b"], r["lo"], r["hi"])
                    for r in rows]
        finally:
            conn.close()

    def get_sheet(self, sid: str) -> Optional[Sheet]:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT sid, point_a, point_b, lo, hi FROM sheets WHERE sid = ?",
                (sid,),
            ).fetchone()
            if r is None:
                return None
            return Sheet(r["sid"], r["point_a"], r["point_b"], r["lo"], r["hi"])
        finally:
            conn.close()

    def latest_snapshot(self) -> Optional[dict]:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT payload, computed_at FROM snapshots WHERE id = 1"
            ).fetchone()
            if r is None:
                return None
            payload = json.loads(r["payload"])
            payload["computed_at"] = r["computed_at"]
            return payload
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    @classmethod
    def _serialize_report(cls, report: ComputeReport) -> dict:
        return {
            "datum": report.datum,
            "sheet_count": report.sheet_count,
            "point_count": report.point_count,
            "contradictions": [
                {
                    "cycle_sheet_ids": c.cycle_sheet_ids,
                    "cycle_points": c.cycle_points,
                    "cycle_bound": c.cycle_bound,
                }
                for c in report.contradictions
            ],
            "nodes": [cls._node_to_dict(n) for n in report.nodes],
        }

    @staticmethod
    def _node_to_dict(n: NodeResult) -> dict:
        return {
            "point": n.point,
            "reachable": n.reachable,
            "cur_lo": n.cur_lo,
            "cur_hi": n.cur_hi,
            "rob_lo": n.rob_lo,
            "rob_hi": n.rob_hi,
            "unbounded_on_removal": n.unbounded_on_removal,
            "min_lo_sheet": n.min_lo_sheet,
            "max_hi_sheet": n.max_hi_sheet,
            "status": n.status,
            "reason": n.reason,
        }

    @staticmethod
    def _write_sheets(conn: sqlite3.Connection, sheets: Sequence[Sheet]) -> None:
        now = time.time()
        conn.execute("DELETE FROM sheets")
        conn.executemany(
            "INSERT INTO sheets(sid, point_a, point_b, lo, hi, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(s.sid, s.point_a, s.point_b, s.lo, s.hi, now) for s in sheets],
        )

    # ------------------------------------------------------------------ #
    def submit_batch(
        self,
        new_sheets: Sequence[Sheet],
        replaced_ids: Sequence[str] = (),
    ) -> Tuple[ComputeReport, List[Sheet]]:
        """原子地补单/换报。

        - new_sheets：本次提交的全部新单（补单时为追加，换报时含替换单）
        - replaced_ids：声明要替换（按号换报）的既有编号；编号必须已存在，
          其余 new_sheets 的编号必须不存在
        失败（校验/矛盾/SQL 错误）时回滚，返回前现行数据不变。
        """
        new_sheets = list(new_sheets)
        replaced = set(replaced_ids)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_rows = conn.execute(
                "SELECT sid, point_a, point_b, lo, hi FROM sheets"
            ).fetchall()
            existing: Dict[str, Sheet] = {
                r["sid"]: Sheet(r["sid"], r["point_a"], r["point_b"], r["lo"], r["hi"])
                for r in existing_rows
            }
            missing = sorted(sid for sid in replaced if sid not in existing)
            if missing:
                raise ValidationError("按号换报失败，编号不存在：%s" % "、".join(missing))

            incoming_ids = set()
            for s in new_sheets:
                s.validate()
                if s.sid in incoming_ids:
                    raise ValidationError("本次提交中编号重复：%s" % s.sid)
                incoming_ids.add(s.sid)
                if s.sid in existing and s.sid not in replaced:
                    raise ValidationError(
                        "编号 %s 已存在；如需更新请走按号换报" % s.sid
                    )
                if s.sid not in existing and s.sid in replaced:
                    raise ValidationError("内部错误：替换编号 %s 不在旧单集中" % s.sid)

            merged: Dict[str, Sheet] = dict(existing)
            for s in new_sheets:
                merged[s.sid] = s  # 换报：同号覆盖；补单：新增
            ordered = [merged[k] for k in sorted(merged)]

            self._write_sheets(conn, ordered)
            report = compute(ordered, datum=self.datum, span_limit=self.span_limit)
            if not report.ok:
                raise ContradictionError(report)
            conn.execute(
                "INSERT INTO snapshots(id, payload, computed_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " computed_at=excluded.computed_at",
                (json.dumps(self._serialize_report(report), ensure_ascii=False),
                 time.time()),
            )
            conn.commit()
            return report, ordered
        except (ContradictionError, ValidationError):
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorageError(str(exc)) from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace_all(self, sheets: Sequence[Sheet]) -> ComputeReport:
        """整库置换（仅供初始化样例/测试使用），同样原子。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._write_sheets(conn, list(sheets))
            report = compute(list(sheets), datum=self.datum, span_limit=self.span_limit)
            if not report.ok:
                raise ContradictionError(report)
            conn.execute(
                "INSERT INTO snapshots(id, payload, computed_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " computed_at=excluded.computed_at",
                (json.dumps(self._serialize_report(report), ensure_ascii=False),
                 time.time()),
            )
            conn.commit()
            return report
        except (ContradictionError,):
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise StorageError(str(exc)) from exc
        finally:
            conn.close()
