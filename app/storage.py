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
from . import sections as secmod

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
CREATE TABLE IF NOT EXISTS soundings (
    section_id TEXT NOT NULL,
    point_id   TEXT NOT NULL,
    zero_point TEXT NOT NULL,
    raw_depth  INTEGER NOT NULL,
    ord        INTEGER NOT NULL,
    PRIMARY KEY (section_id, point_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_soundings_section
    ON soundings(section_id, ord);
CREATE TABLE IF NOT EXISTS section_snapshots (
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
            if conn.execute(
                "SELECT 1 FROM section_snapshots WHERE id = 1"
            ).fetchone() is None:
                empty_sections = self._empty_section_payload()
                conn.execute(
                    "INSERT INTO section_snapshots(id, payload, computed_at)"
                    " VALUES (1, ?, ?)",
                    (json.dumps(empty_sections, ensure_ascii=False), time.time()),
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
    # 测深与断面结果
    # ------------------------------------------------------------------ #
    def list_soundings(self) -> List[secmod.Sounding]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT section_id, point_id, zero_point, raw_depth"
                " FROM soundings ORDER BY ord"
            ).fetchall()
            return [secmod.Sounding(r["section_id"], r["point_id"],
                                    r["zero_point"], r["raw_depth"])
                    for r in rows]
        finally:
            conn.close()

    def latest_section_snapshot(self) -> Optional[dict]:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT payload, computed_at FROM section_snapshots WHERE id = 1"
            ).fetchone()
            if r is None:
                return None
            payload = json.loads(r["payload"])
            payload["computed_at"] = r["computed_at"]
            return payload
        finally:
            conn.close()

    def list_section_results(self) -> List[dict]:
        snap = self.latest_section_snapshot() or {}
        return list(snap.get("sections", []))

    # 命名别名（不同调用/测试习惯）
    get_sections = list_section_results
    list_sections = list_section_results

    def replace_soundings_csv(self, csv_text: str) -> dict:
        """解析 CSV 文本并整批替换（失败抛 ValidationError，状态不变）。"""
        soundings = secmod.parse_sounding_csv(csv_text)
        return self.replace_soundings(soundings, csv_text=csv_text)

    def save_soundings(self, data, csv_text: Optional[str] = None) -> dict:
        """灵活入口：CSV 文本/字节、Sounding 列表或字典数组均可。"""
        if isinstance(data, (str, bytes)):
            parsed = secmod.parse_sounding_csv(data)
            if csv_text is None and isinstance(data, str):
                csv_text = data
        elif data and isinstance(data[0], dict):
            parsed = secmod.soundings_from_dicts(data)
        else:
            parsed = list(data)
        return self.replace_soundings(parsed, csv_text=csv_text)

    def get_sounding_csv(self) -> str:
        """优先返回最近上传的原始 CSV 文本；否则由现行测深行生成。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", ("last_sounding_csv",)
            ).fetchone()
            if row is not None and row["value"]:
                return row["value"]
            rows = conn.execute(
                "SELECT section_id, point_id, zero_point, raw_depth"
                " FROM soundings ORDER BY ord"
            ).fetchall()
            if not rows:
                return ""
            lines = ["断面号,测点号,零点,原水深"]
            lines += ["%s,%s,%s,%d" % (r["section_id"], r["point_id"],
                                       r["zero_point"], r["raw_depth"]) for r in rows]
            return "\n".join(lines) + "\n"
        finally:
            conn.close()

    def dashboard(self) -> dict:
        """同一读事务内取齐页面所需状态，避免跨提交读到新旧两半。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            snap_row = conn.execute(
                "SELECT payload, computed_at FROM snapshots WHERE id = 1"
            ).fetchone()
            sec_row = conn.execute(
                "SELECT payload, computed_at FROM section_snapshots WHERE id = 1"
            ).fetchone()
            sheet_rows = conn.execute(
                "SELECT sid, point_a, point_b, lo, hi FROM sheets ORDER BY sid"
            ).fetchall()
            csv_row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", ("last_sounding_csv",)
            ).fetchone()
            conn.commit()
            snapshot = None
            if snap_row is not None:
                snapshot = json.loads(snap_row["payload"])
                snapshot["computed_at"] = snap_row["computed_at"]
            section = None
            if sec_row is not None:
                section = json.loads(sec_row["payload"])
                section["computed_at"] = sec_row["computed_at"]
            sheets = [Sheet(r["sid"], r["point_a"], r["point_b"], r["lo"], r["hi"])
                      for r in sheet_rows]
            return {
                "snapshot": snapshot,
                "section_snapshot": section,
                "sheets": sheets,
                "csv_text": csv_row["value"] if csv_row is not None else "",
                "datum": self.datum,
                "span_limit": self.span_limit,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _empty_section_payload(now: Optional[float] = None) -> dict:
        return {
            "sections": [],
            "results": [],
            "mode": "empty",
            "changed_zeros": [],
            "recomputed_sections": [],
            "section_count": 0,
            "ok_count": 0,
            "pending_count": 0,
            "sounding_count": 0,
            "computed_at": now if now is not None else time.time(),
        }

    @staticmethod
    def _write_soundings(conn: sqlite3.Connection,
                         soundings: Sequence[secmod.Sounding]) -> None:
        conn.execute("DELETE FROM soundings")
        conn.executemany(
            "INSERT INTO soundings(section_id, point_id, zero_point,"
            " raw_depth, ord) VALUES (?, ?, ?, ?, ?)",
            [(s.section_id, s.point_id, s.zero_point, s.raw_depth, i)
             for i, s in enumerate(soundings)],
        )

    @staticmethod
    def _read_soundings_conn(conn: sqlite3.Connection) -> List[secmod.Sounding]:
        rows = conn.execute(
            "SELECT section_id, point_id, zero_point, raw_depth"
            " FROM soundings ORDER BY ord"
        ).fetchall()
        return [secmod.Sounding(r["section_id"], r["point_id"],
                                r["zero_point"], r["raw_depth"])
                for r in rows]

    def _zero_table(self, report_dict: dict) -> Dict[str, dict]:
        return {
            n["point"]: {
                "status": n.get("status"),
                "rob_hi": n.get("rob_hi"),
                "rob_lo": n.get("rob_lo"),
                "eligible": n.get("status") == "OK"
                and n.get("rob_hi") is not None,
                "reason": n.get("reason", ""),
            }
            for n in report_dict.get("nodes", [])
        }

    def _store_section_payload(self, conn: sqlite3.Connection,
                               payload: dict, now: float) -> None:
        payload.setdefault("datum", self.datum)
        payload.setdefault("span_limit", self.span_limit)
        payload.setdefault("zero_table", {})
        conn.execute(
            "INSERT INTO section_snapshots(id, payload, computed_at)"
            " VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
            " computed_at=excluded.computed_at",
            (json.dumps(payload, ensure_ascii=False), now),
        )

    def _full_section_payload(
        self,
        conn: sqlite3.Connection,
        soundings: Sequence[secmod.Sounding],
        report_dict: dict,
        now: float,
        mode: str = "full",
    ) -> dict:
        report = secmod.build_section_report(
            list(soundings), report_dict, computed_at=now, mode=mode,
        )
        payload = report.to_dict()
        payload["correction_snapshot"] = {
            "datum": report_dict.get("datum", self.datum),
            "nodes": report_dict.get("nodes", []),
        }
        payload["zero_table"] = self._zero_table(report_dict)
        return payload

    def _incremental_section_payload(
        self,
        conn: sqlite3.Connection,
        soundings: Sequence[secmod.Sounding],
        new_report_dict: dict,
        old_report_dict: Optional[dict],
        now: float,
    ) -> dict:
        old_sec_row = conn.execute(
            "SELECT payload FROM section_snapshots WHERE id = 1"
        ).fetchone()
        old_section_payload = None
        if old_sec_row is not None:
            try:
                old_section_payload = json.loads(old_sec_row["payload"])
            except (ValueError, TypeError):
                old_section_payload = None
        report = secmod.recompute_sections(
            list(soundings),
            new_report_dict,
            old_correction=old_report_dict,
            old_report=old_section_payload,
        )
        payload = report.to_dict()
        payload["computed_at"] = now
        payload["correction_snapshot"] = {
            "datum": new_report_dict.get("datum", self.datum),
            "nodes": new_report_dict.get("nodes", []),
        }
        payload["zero_table"] = self._zero_table(new_report_dict)
        return payload

    def replace_soundings(
        self,
        soundings: Sequence[secmod.Sounding],
        csv_text: Optional[str] = None,
    ) -> dict:
        """整批替换原始测深并按现行改正表全量重算断面。

        校验/写入失败一律回滚，现行测深与断面结果保持不变。
        """
        soundings = list(soundings)
        if not soundings:
            raise ValidationError("测深 CSV 没有有效数据行")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 事务内再做一次同断面测点号查重（防御直接调用）
            seen = set()
            for s in soundings:
                key = (s.section_id, s.point_id)
                if key in seen:
                    raise ValidationError(
                        "断面 %s 内测点号 %s 重复" % key)
                seen.add(key)
            self._write_soundings(conn, soundings)

            snap_row = conn.execute(
                "SELECT payload FROM snapshots WHERE id = 1"
            ).fetchone()
            report_dict = json.loads(snap_row["payload"]) if snap_row else \
                self._serialize_report(compute([], self.datum, self.span_limit))
            now = time.time()
            payload = self._full_section_payload(
                conn, soundings, report_dict, now)
            self._store_section_payload(conn, payload, now)
            if csv_text is not None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('last_sounding_csv', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (csv_text,),
                )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('soundings_updated_at', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(now),),
            )
            conn.commit()
            return payload
        except (ValidationError, secmod.SoundingValidationError):
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

    # 方法定义后的别名
    upload_soundings = replace_soundings
    replace_sounding_rows = replace_soundings

    def apply_updates(
        self,
        new_sheets: Sequence[Sheet] = (),
        replaced_ids: Sequence[str] = (),
        soundings: Optional[Sequence[secmod.Sounding]] = None,
        csv_text: Optional[str] = None,
    ) -> dict:
        """单个事务内同时提交联测单更新与测深重传。

        断面结果与所依据的原始测深、改正表必然来自同一次提交后的状态；
        任何校验/矛盾/保存失败都整体回滚，沿用最近成功生效的数据。
        """
        new_sheets = list(new_sheets)
        replaced = set(replaced_ids)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            old_snap_row = conn.execute(
                "SELECT payload FROM snapshots WHERE id = 1"
            ).fetchone()
            old_report_dict = (json.loads(old_snap_row["payload"])
                               if old_snap_row is not None else None)

            existing_rows = conn.execute(
                "SELECT sid, point_a, point_b, lo, hi FROM sheets"
            ).fetchall()
            existing: Dict[str, Sheet] = {
                r["sid"]: Sheet(r["sid"], r["point_a"], r["point_b"],
                                r["lo"], r["hi"])
                for r in existing_rows
            }
            missing = sorted(sid for sid in replaced if sid not in existing)
            if missing:
                raise ValidationError(
                    "按号换报失败，编号不存在：%s" % "、".join(missing))
            incoming_ids = set()
            for s in new_sheets:
                s.validate()
                if s.sid in incoming_ids:
                    raise ValidationError("本次提交中编号重复：%s" % s.sid)
                incoming_ids.add(s.sid)
                if s.sid in existing and s.sid not in replaced:
                    raise ValidationError(
                        "编号 %s 已存在；如需更新请走按号换报" % s.sid)
                if s.sid not in existing and s.sid in replaced:
                    raise ValidationError(
                        "内部错误：替换编号 %s 不在旧单集中" % s.sid)
            merged_sheets: Dict[str, Sheet] = dict(existing)
            for s in new_sheets:
                merged_sheets[s.sid] = s
            ordered_sheets = [merged_sheets[k] for k in sorted(merged_sheets)]

            sounding_replaced = soundings is not None
            effective_soundings: List[secmod.Sounding]
            if sounding_replaced:
                effective_soundings = list(soundings)  # type: ignore[arg-type]
                seen = set()
                for s in effective_soundings:
                    key = (s.section_id, s.point_id)
                    if key in seen:
                        raise ValidationError(
                            "断面 %s 内测点号 %s 重复" % key)
                    seen.add(key)
                self._write_soundings(conn, effective_soundings)
            else:
                effective_soundings = self._read_soundings_conn(conn)

            self._write_sheets(conn, ordered_sheets)
            report = compute(ordered_sheets, datum=self.datum,
                             span_limit=self.span_limit)
            if not report.ok:
                raise ContradictionError(report)
            report_dict = self._serialize_report(report)
            now = time.time()
            conn.execute(
                "INSERT INTO snapshots(id, payload, computed_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " computed_at=excluded.computed_at",
                (json.dumps(report_dict, ensure_ascii=False), now),
            )

            if sounding_replaced or old_report_dict is None:
                section_payload = self._full_section_payload(
                    conn, effective_soundings, report_dict, now)
                section_payload["mode"] = (
                    "joint-full" if sounding_replaced else "full")
            else:
                section_payload = self._incremental_section_payload(
                    conn, effective_soundings, report_dict,
                    old_report_dict, now)
                if sounding_replaced:
                    section_payload["mode"] = "joint-full"
            self._store_section_payload(conn, section_payload, now)

            if sounding_replaced and csv_text is not None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('last_sounding_csv', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (csv_text,),
                )
                conn.execute(
                    "INSERT INTO meta(key, value)"
                    " VALUES('soundings_updated_at', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(now),),
                )
            conn.commit()
            return {
                "report": report,
                "sheets": ordered_sheets,
                "section_payload": section_payload,
                "soundings_replaced": sounding_replaced,
            }
        except (ContradictionError, ValidationError,
                secmod.SoundingValidationError):
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
            old_snap_row = conn.execute(
                "SELECT payload FROM snapshots WHERE id = 1"
            ).fetchone()
            old_report_dict = (json.loads(old_snap_row["payload"])
                               if old_snap_row is not None else None)
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
            report_dict = self._serialize_report(report)
            now = time.time()
            conn.execute(
                "INSERT INTO snapshots(id, payload, computed_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " computed_at=excluded.computed_at",
                (json.dumps(report_dict, ensure_ascii=False), now),
            )
            # 联测单变化 -> 增量重算用到“资格/稳健上界变化零点”的断面
            soundings = self._read_soundings_conn(conn)
            section_payload = self._incremental_section_payload(
                conn, soundings, report_dict, old_report_dict, now)
            self._store_section_payload(conn, section_payload, now)
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
            report_dict = self._serialize_report(report)
            now = time.time()
            conn.execute(
                "INSERT INTO snapshots(id, payload, computed_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,"
                " computed_at=excluded.computed_at",
                (json.dumps(report_dict, ensure_ascii=False), now),
            )
            soundings = self._read_soundings_conn(conn)
            section_payload = self._full_section_payload(
                conn, soundings, report_dict, now, mode="replace-all")
            self._store_section_payload(conn, section_payload, now)
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
