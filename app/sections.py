# -*- coding: utf-8 -*-
"""航道复测：测深 CSV 解析与断面保守最浅水深核算（纯标准库）。

业务口径
========
* CSV 每行一笔测深：``断面号, 测点号, 零点, 原水深``；原水深为从该零点
  向下计的整数毫米。同一断面内测点号不得重复（跨断面可以重号）。
* 第 i 笔的保守化算水深 = 原水深 − 所属零点的**稳健上界**
  （改正表快照里的 ``rob_hi``）。
* 断面内所有零点都 *有改正数据*（出现在改正表中）且 *可合图*
  （节点状态为 ``OK``）时，才给出最浅值；最浅值取化算水深最小值，
  并列的测点全部列出。否则断面标 ``PENDING``（待核），并列出
  缺少改正数据（快照中无此零点）或不可合图（状态非 OK）的零点。
* 联测单变化后，仅当零点的合图资格（是否 OK）或稳健上界发生变化才
  可能影响断面；:func:`changed_zero_points` 求变化零点，
  :func:`recompute_sections` 只重算用到这些零点的断面（含待核断面），
  其余断面结果原样保留。

本模块不依赖 Flask / sqlite，便于直接单测。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "SoundingValidationError",
    "CsvValidationError",
    "Sounding",
    "SoundingRow",
    "ZeroInfo",
    "PointDepth",
    "SectionResult",
    "SectionReport",
    "parse_sounding_csv",
    "parse_csv",
    "parse_soundings",
    "soundings_from_dicts",
    "build_zero_index",
    "zero_index_from_snapshot",
    "evaluate_sections",
    "compute_sections",
    "compute_section_results",
    "compute_section",
    "changed_zero_points",
    "find_changed_zeros",
    "zero_qualification",
    "recompute_sections",
    "incremental_recompute",
    "build_section_report",
    "SECTION_OK",
    "SECTION_PENDING",
]

SECTION_OK = "OK"
SECTION_PENDING = "PENDING"  # 页面文案为“待核”

_INT_RE = re.compile(r"^[+-]?[0-9]+$")


class SoundingValidationError(ValueError):
    """测深 CSV / 记录不合法（整批拒绝，不得改动现行数据）。"""


# 常见别名
CsvValidationError = SoundingValidationError


# --------------------------------------------------------------------------- #
# 测深记录（兼容多种关键字命名 / 位置参数）
# --------------------------------------------------------------------------- #
class Sounding:
    """一笔原始测深。

    规范字段：``section_id, point_id, zero_point, raw_depth``。
    同时接受常见简写关键字：``section/section_no``、``point/point_no``、
    ``zero/zero_id``、``depth/raw/raw_mm``。
    """

    __slots__ = ("section_id", "point_id", "zero_point", "raw_depth")

    def __init__(
        self,
        section_id: Any = None,
        point_id: Any = None,
        zero_point: Any = None,
        raw_depth: Any = None,
        *,
        section: Any = None,
        section_no: Any = None,
        section_code: Any = None,
        transect: Any = None,
        point: Any = None,
        point_no: Any = None,
        point_code: Any = None,
        station: Any = None,
        zero: Any = None,
        zero_id: Any = None,
        zero_code: Any = None,
        datum_point: Any = None,
        depth: Any = None,
        raw: Any = None,
        raw_mm: Any = None,
        raw_depth_mm: Any = None,
    ) -> None:
        sec = _first(section_id, section, section_no, section_code, transect)
        pnt = _first(point_id, point, point_no, point_code, station)
        zro = _first(zero_point, zero, zero_id, zero_code, datum_point)
        dep = _first(raw_depth, depth, raw, raw_mm, raw_depth_mm)

        self.section_id = _clean_text(sec, "断面号")
        self.point_id = _clean_text(pnt, "测点号")
        self.zero_point = _clean_text(zro, "零点")
        if isinstance(dep, bool) or dep is None:
            raise SoundingValidationError(
                "断面 %s 测点 %s 的原水深必须是整数毫米"
                % (self.section_id or "?", self.point_id or "?")
            )
        if isinstance(dep, int):
            depth_val = dep
        elif isinstance(dep, str) and _INT_RE.match(dep.strip().replace(" ", "")):
            depth_val = int(dep.strip().replace(" ", ""))
        elif isinstance(dep, float) and dep.is_integer():
            depth_val = int(dep)
        else:
            raise SoundingValidationError(
                "断面 %s 测点 %s 的原水深必须是整数毫米，收到：%r"
                % (self.section_id or "?", self.point_id or "?", dep)
            )
        if depth_val < 0:
            raise SoundingValidationError(
                "断面 %s 测点 %s 的原水深为 %d mm，从零点向下计的原水深不能为负"
                % (self.section_id, self.point_id, depth_val)
            )
        self.raw_depth = depth_val

    # --- 简写别名，方便不同调用习惯 ---
    @property
    def section(self) -> str:
        return self.section_id

    @property
    def point(self) -> str:
        return self.point_id

    @property
    def zero(self) -> str:
        return self.zero_point

    @property
    def depth(self) -> int:
        return self.raw_depth

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return (self.section_id, self.point_id, self.zero_point, self.raw_depth)[key]
        return getattr(self, key)

    def __iter__(self):
        return iter((self.section_id, self.point_id, self.zero_point, self.raw_depth))

    def __repr__(self) -> str:
        return "Sounding(%r, %r, %r, %d)" % (
            self.section_id, self.point_id, self.zero_point, self.raw_depth)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sounding):
            return (self.section_id, self.point_id, self.zero_point,
                    self.raw_depth) == (other.section_id, other.point_id,
                                       other.zero_point, other.raw_depth)
        if isinstance(other, (tuple, list)) and len(other) == 4:
            return tuple(self) == tuple(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.section_id, self.point_id, self.zero_point, self.raw_depth))

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "section": self.section_id,
            "point_id": self.point_id,
            "point": self.point_id,
            "zero_point": self.zero_point,
            "zero": self.zero_point,
            "raw_depth": self.raw_depth,
            "depth": self.raw_depth,
        }


SoundingRow = Sounding


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _clean_text(value: Any, field_name: str) -> str:
    if value is None:
        raise SoundingValidationError("%s不能为空" % field_name)
    text = str(value).strip()
    if not text:
        raise SoundingValidationError("%s不能为空" % field_name)
    return text


# --------------------------------------------------------------------------- #
# CSV / 字典解析
# --------------------------------------------------------------------------- #
_HEADER_RULES: Sequence[Tuple[str, Tuple[str, ...]]] = (
    ("section_id", ("断面编号", "断面号", "断面", "sectionid", "section",
                    "transect", "profile", "crosssection", "duanmian")),
    ("point_id", ("测点编号", "测点号", "测点", "点号", "soundingpoint",
                  "surveypoint", "pointid", "stationid", "station", "point")),
    ("zero_point", ("零点编号", "零点号", "零点", "zeropointid", "zeropoint",
                    "zeroid", "datumpoint", "zero", "datum")),
    ("raw_depth", ("原始水深", "原水深", "水深值", "水深", "rawdepthmm",
                   "rawdepth", "soundingdepth", "depthmm", "sounding", "depth")),
)


def _norm_header(cell: str) -> str:
    text = str(cell).strip().lower()
    # 去掉括号单位等符号，仅保留中英文数字
    text = re.sub(r"[\s_\-./\\:：()（）\[\]【】*]+", "", text)
    return text


def _match_header(cols: Sequence[str]) -> Optional[Dict[str, int]]:
    """尝试把表头列名映射到规范字段；四列齐全才返回。"""
    norm = [_norm_header(c) for c in cols]
    mapping: Dict[str, int] = {}
    used: Set[int] = set()
    for field, aliases in _HEADER_RULES:
        best_idx = None
        best_len = -1
        for i, name in enumerate(norm):
            if i in used or not name:
                continue
            for alias in aliases:
                a = _norm_header(alias)
                if name == a or a in name:
                    # 取匹配别名最长（最具体）的一列
                    if len(a) > best_len:
                        best_len = len(a)
                        best_idx = i
        if best_idx is None:
            return None
        mapping[field] = best_idx
        used.add(best_idx)
    if len(set(mapping.values())) != 4:
        return None
    return mapping


def _decode_csv_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_sounding_csv(content: Any) -> List[Sounding]:
    """解析测深 CSV（文本或字节），返回 :class:`Sounding` 列表。

    支持首行中英文表头（自动识别并按列名映射），也支持无表头、固定列序
    （断面号, 测点号, 零点, 原水深）。整批校验，任何错误都抛
    :class:`SoundingValidationError`，调用方不得改动现行数据。
    """
    if isinstance(content, bytes):
        text = _decode_csv_bytes(content)
    elif isinstance(content, str):
        text = content
    else:
        raise SoundingValidationError("CSV 内容必须是文本或字节")

    # 去掉 BOM（utf-8-sig 已处理字节；字符串情形兜底）
    if text and text[0] == "﻿":
        text = text[1:]

    sample_lines = [ln for ln in text.splitlines() if ln.strip()][:3]
    delimiter = ","
    if sample_lines:
        counts = {d: sample_lines[0].count(d) for d in (",", "\t", ";")}
        if max(counts.values()) > counts[","]:
            delimiter = max(counts, key=counts.get)  # type: ignore[arg-type]

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    raw_rows = [row for row in reader if any(str(c).strip() for c in row)]
    if not raw_rows:
        raise SoundingValidationError("CSV 没有有效数据行（应为：断面号,测点号,零点,原水深）")

    first = [str(c).strip() for c in raw_rows[0]]
    col_map: Optional[Dict[str, int]] = None
    start = 0
    if len(first) >= 4:
        col_map = _match_header(first[:4] if len(first) >= 4 else first)
        if col_map is not None:
            start = 1
        elif not _looks_like_int(first[3]):
            # 首行第 4 列不是整数：当作无法识别的表头跳过，按固定列序解析
            start = 1
    rows = raw_rows[start:]
    if not rows:
        raise SoundingValidationError("CSV 只有表头、没有数据行")

    out: List[Sounding] = []
    seen: Dict[Tuple[str, str], int] = {}
    for offset, row in enumerate(rows, start=start + 1):  # CSV 行号（1 起，含表头）
        cells = [str(c).strip() for c in row]
        if not any(cells):
            continue
        if len(cells) != 4:
            raise SoundingValidationError(
                "CSV 第 %d 行应有 4 列（断面号,测点号,零点,原水深），实际 %d 列：%s"
                % (offset, len(cells), ",".join(cells))
            )
        if col_map is not None:
            kwargs = {f: cells[idx] for f, idx in col_map.items()}
            rec = Sounding(**kwargs)
        else:
            rec = Sounding(cells[0], cells[1], cells[2], cells[3])
        key = (rec.section_id, rec.point_id)
        if key in seen:
            raise SoundingValidationError(
                "断面 %s 内测点号 %s 重复（首次出现在第 %d 行，再次出现在第 %d 行）"
                % (rec.section_id, rec.point_id, seen[key], offset)
            )
        seen[key] = offset
        out.append(rec)

    if not out:
        raise SoundingValidationError("CSV 没有有效数据行")
    return out


parse_csv = parse_sounding_csv
parse_soundings = parse_sounding_csv


def _looks_like_int(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    return bool(_INT_RE.match(str(value).strip().replace(" ", "")))


def soundings_from_dicts(items: Iterable[Mapping[str, Any]]) -> List[Sounding]:
    """从 JSON 对象数组构造测深记录，并做同断面测点号查重。"""
    out: List[Sounding] = []
    seen: Dict[Tuple[str, str], int] = {}
    for i, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise SoundingValidationError("第 %d 笔测深必须是对象" % i)
        rec = Sounding(
            item.get("section_id", item.get("section")),
            item.get("point_id", item.get("point")),
            item.get("zero_point", item.get("zero")),
            item.get("raw_depth", item.get("depth")),
        )
        key = (rec.section_id, rec.point_id)
        if key in seen:
            raise SoundingValidationError(
                "断面 %s 内测点号 %s 重复" % (rec.section_id, rec.point_id)
            )
        seen[key] = i
        out.append(rec)
    if not out:
        raise SoundingValidationError("测深记录为空")
    return out


# --------------------------------------------------------------------------- #
# 改正表索引
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ZeroInfo:
    point: str
    has_data: bool
    status: Optional[str]
    eligible: bool
    rob_hi: Optional[int]
    reason: str = ""

    @property
    def robust_upper(self) -> Optional[int]:
        return self.rob_hi

    def to_dict(self) -> dict:
        return {
            "point": self.point,
            "has_data": self.has_data,
            "status": self.status,
            "eligible": self.eligible,
            "composable": self.eligible,
            "rob_hi": self.rob_hi,
            "robust_upper": self.rob_hi,
            "reason": self.reason,
        }


def _node_get(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, Mapping):
        return node.get(key, default)
    return getattr(node, key, default)


def build_zero_index(source: Any = None) -> Dict[str, ZeroInfo]:
    """把改正表快照 / ComputeReport / 节点列表 / 简易映射归一为零点索引。

    兼容的简易映射：``{"D0": 0, "Z1": 102}``（整数视为有稳健上界且可合图）、
    ``{"Z": (lo, hi)}``、``{"Z": {"status": "OK", "rob_hi": 102}}``。
    """
    if source is None:
        return {}

    # 1) 快照字典（{"nodes": [...]}）或 ComputeReport（.nodes）
    nodes: Any = None
    if isinstance(source, Mapping) and "nodes" in source:
        nodes = source["nodes"]
    elif hasattr(source, "nodes"):
        nodes = source.nodes
    elif isinstance(source, (list, tuple)):
        if source and all(not isinstance(x, (str, bytes)) and
                          (_node_get(x, "point", None) is not None) for x in source):
            nodes = source
        else:
            return _index_from_simple_mapping(source)  # type: ignore[arg-type]
    elif isinstance(source, Mapping):
        return _index_from_simple_mapping(source)
    else:
        raise SoundingValidationError("无法识别的改正表数据类型：%r" % type(source))

    index: Dict[str, ZeroInfo] = {}
    for n in nodes:
        point = _node_get(n, "point")
        if point is None:
            continue
        status = _node_get(n, "status")
        rob_hi = _node_get(n, "rob_hi", None)
        if rob_hi is None:
            rob_hi = _node_get(n, "robust_hi", _node_get(n, "robust_upper", None))
        eligible = (status == SECTION_OK) and rob_hi is not None
        index[str(point)] = ZeroInfo(
            point=str(point),
            has_data=True,
            status=status,
            eligible=eligible,
            rob_hi=None if rob_hi is None else int(rob_hi),
            reason=_node_get(n, "reason", "") or "",
        )
    return index


zero_index_from_snapshot = build_zero_index


def _index_from_simple_mapping(mapping: Mapping) -> Dict[str, ZeroInfo]:
    index: Dict[str, ZeroInfo] = {}
    for point, value in mapping.items():
        if isinstance(value, ZeroInfo):
            index[point] = value
        elif isinstance(value, Mapping):
            status = value.get("status", SECTION_OK)
            rob_hi = value.get("rob_hi", value.get("robust_upper",
                              value.get("robust_hi", value.get("hi"))))
            index[point] = ZeroInfo(
                point=point, has_data=value.get("has_data", True),
                status=status,
                eligible=bool(value.get("eligible", status == SECTION_OK)),
                rob_hi=None if rob_hi is None else int(rob_hi),
                reason=value.get("reason", ""),
            )
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            index[point] = ZeroInfo(point, True, SECTION_OK, True, int(value[1]))
        elif isinstance(value, int) and not isinstance(value, bool):
            index[point] = ZeroInfo(point, True, SECTION_OK, True, int(value))
        elif value is None:
            index[point] = ZeroInfo(point, False, None, False, None)
        else:
            raise SoundingValidationError("零点 %s 的改正数据无法识别：%r" % (point, value))
    return index


# --------------------------------------------------------------------------- #
# 断面结果（dict 子类：属性 / 下标两种访问都支持）
# --------------------------------------------------------------------------- #
class PointDepth(dict):
    """单个测点的化算结果；同时支持 ``p.corrected_depth`` 与 ``p["..."]``。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class SectionResult(dict):
    """单个断面的核定结果（字典形态，JSON 可直接序列化）。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def _problem_for_zero(point: str, info: Optional[ZeroInfo]) -> dict:
    if info is None or not info.has_data:
        return {
            "point": point, "zero_point": point, "zero": point,
            "kind": "MISSING", "code": "MISSING_CORRECTION",
            "label": "缺少改正数据",
            "status": "MISSING", "reason": "改正表中没有零点 %s 的改正数据" % point,
        }
    return {
        "point": point, "zero_point": point, "zero": point,
        "kind": "NOT_FIT", "code": info.status or "NOT_FIT",
        "label": "不可合图",
        "status": info.status,
        "reason": info.reason or ("零点 %s 当前状态为 %s，不可合图"
                                  % (point, info.status)),
        "rob_hi": info.rob_hi,
    }


def compute_section(
    section_id: str,
    rows: Sequence[Sounding],
    zero_index: Mapping[str, ZeroInfo],
) -> SectionResult:
    """计算单个断面。``rows`` 必须都属于该断面。"""
    zero_order: List[str] = []
    for r in rows:
        if r.zero_point not in zero_order:
            zero_order.append(r.zero_point)

    problems: List[dict] = []
    missing: List[str] = []
    unfit: List[dict] = []
    for z in zero_order:
        info = zero_index.get(z)
        if info is None or not info.has_data:
            missing.append(z)
            problems.append(_problem_for_zero(z, None))
        elif not info.eligible:
            prob = _problem_for_zero(z, info)
            unfit.append(prob)
            problems.append(prob)

    ok = not problems
    point_rows: List[PointDepth] = []
    corrected_values: List[int] = []
    for r in rows:
        info = zero_index.get(r.zero_point)
        rob_hi = info.rob_hi if info is not None else None
        corrected = (r.raw_depth - rob_hi) if rob_hi is not None else None
        if corrected is not None:
            corrected_values.append(corrected)
        point_rows.append(PointDepth(
            point_id=r.point_id,
            point=r.point_id,
            zero_point=r.zero_point,
            zero=r.zero_point,
            raw_depth=r.raw_depth,
            depth=r.raw_depth,
            rob_hi=rob_hi,
            robust_upper=rob_hi,
            corrected_depth=corrected,
            depth_corrected=corrected,
            eligible=info is not None and info.eligible,
            is_shallowest=False,
        ))

    shallowest_depth: Optional[int] = None
    shallowest_points: List[str] = []
    if ok:
        shallowest_depth = min(corrected_values)
        for pr in point_rows:
            if pr["corrected_depth"] == shallowest_depth:
                pr["is_shallowest"] = True
                shallowest_points.append(pr["point_id"])

    if ok:
        status = SECTION_OK
        status_label = "可查最浅"
        reason = "断面内 %d 个零点均可合图" % len(zero_order)
    else:
        status = SECTION_PENDING
        status_label = "待核"
        parts = []
        if missing:
            parts.append("缺少改正数据：%s" % "、".join(missing))
        if unfit:
            parts.append("不可合图：%s" % "、".join(u["point"] for u in unfit))
        reason = "；".join(parts)

    return SectionResult(
        section_id=section_id,
        section=section_id,
        id=section_id,
        status=status,
        status_code=status if status == SECTION_OK else "CHECK",
        verdict="SHOWN" if ok else "CHECK",
        status_label=status_label,
        label=status_label,
        pending=not ok,
        needs_check=not ok,
        compositable=ok,
        ok=ok,
        shallowest_depth=shallowest_depth,
        min_depth=shallowest_depth,
        least_depth=shallowest_depth,
        conservative_depth=shallowest_depth,
        depth_mm=shallowest_depth,
        shallowest_depth_m=(None if shallowest_depth is None
                            else round(shallowest_depth / 1000.0, 3)),
        shallowest_points=shallowest_points,
        tied_points=list(shallowest_points),
        min_points=list(shallowest_points),
        shallowest_point_ids=list(shallowest_points),
        points=[dict(p) for p in point_rows],
        point_results=[dict(p) for p in point_rows],
        zeros=zero_order,
        zero_points=list(zero_order),
        zero_count=len(zero_order),
        missing_zeros=missing,
        lacking_zeros=list(missing),
        absent_zeros=list(missing),
        unfit_zeros=unfit,
        not_composable_zeros=list(unfit),
        invalid_zeros=[u["point"] for u in unfit],
        problems=problems,
        blocking_zeros=[p["point"] for p in problems],
        reason=reason,
    )


def _group_by_section(soundings: Sequence[Sounding]) -> List[Tuple[str, List[Sounding]]]:
    order: List[str] = []
    groups: Dict[str, List[Sounding]] = {}
    for s in soundings:
        if s.section_id not in groups:
            groups[s.section_id] = []
            order.append(s.section_id)
        groups[s.section_id].append(s)
    return [(sid, groups[sid]) for sid in order]


def evaluate_sections(
    soundings: Sequence[Sounding],
    zeros: Any = None,
    *,
    snapshot: Any = None,
    correction: Any = None,
    zero_index: Optional[Mapping[str, ZeroInfo]] = None,
) -> List[SectionResult]:
    """按 CSV 中断面首次出现顺序计算全部断面，返回结果字典列表。"""
    if zero_index is None:
        source = zeros if zeros is not None else (snapshot if snapshot is not None
                                                  else correction)
        zero_index = build_zero_index(source)
    return [compute_section(sid, rows, zero_index)
            for sid, rows in _group_by_section(list(soundings))]


# 最可能被直接调用的名字
compute_sections = evaluate_sections
compute_section_results = evaluate_sections


# --------------------------------------------------------------------------- #
# 增量：变化零点 + 只重算受影响断面
# --------------------------------------------------------------------------- #
def zero_qualification(info: Any) -> str:
    """零点合图资格分类：MISSING / OK / PENDING / FAIL / OTHER。"""
    if info is None:
        return "MISSING"
    status = _node_get(info, "status", None)
    if status is None and isinstance(info, ZeroInfo):
        status = info.status
    if status == SECTION_OK:
        return SECTION_OK
    if status == SECTION_PENDING:
        return SECTION_PENDING
    if status in ("FAIL", "NOT_FIT", "ERROR", "CONTRADICTION"):
        return "FAIL"
    if status is None:
        return "MISSING"
    return str(status)


def _fingerprint(node: Any) -> Tuple[bool, Optional[int], str]:
    """规范指纹：(是否可合图, 稳健上界, 资格细类)。

    资格细类把“查无改正数据 / 待联测 / 不合图”区分开，保证补单后
    断面列出的待核原因仍与现行改正表一致；前两位对应题面明确点名的
    “合图资格（布尔意义）或稳健上界”。
    """
    if node is None:
        return (False, None, "MISSING")
    status = _node_get(node, "status", None)
    rob_hi = _node_get(node, "rob_hi", None)
    if rob_hi is None:
        rob_hi = _node_get(node, "robust_upper", None)
    eligible = (status == SECTION_OK and rob_hi is not None)
    return (bool(eligible),
            None if rob_hi is None else int(rob_hi),
            zero_qualification(node))


def changed_zero_points(old_source: Any, new_source: Any,
                        *, include_detail: bool = False) -> Set[str]:
    """求合图资格或稳健上界发生变化的零点集合。

    严格按题意：零点“可合图资格”的布尔值（``status == 'OK'`` 且有稳健
    上界）或 ``rob_hi`` 发生变化，才计入。入参可为快照字典 /
    ComputeReport / 节点列表 / ZeroInfo 映射。

    ``include_detail=True`` 时额外计入资格细类变化（如“查无改正数据
    MISSING”变为“待联测 PENDING”，两者都不可合图、rob_hi 均为 None），
    这类变化不需要重算最浅值，只刷新待核原因措辞。
    """
    old_map = _node_map(old_source)
    new_map = _node_map(new_source)
    changed: Set[str] = set()
    detail_only: Set[str] = set()
    for point in set(old_map) | set(new_map):
        old_node = old_map.get(point)
        new_node = new_map.get(point)
        old_fp = _fingerprint(old_node) if old_node is not None else _fingerprint(None)
        new_fp = _fingerprint(new_node) if new_node is not None else _fingerprint(None)
        if old_fp[0] != new_fp[0] or old_fp[1] != new_fp[1]:
            changed.add(point)
        elif old_fp[2] != new_fp[2]:
            detail_only.add(point)
    if include_detail:
        return changed | detail_only
    return changed


find_changed_zeros = changed_zero_points


def refresh_section_problems(section: Mapping[str, Any],
                             zero_index: Mapping[str, ZeroInfo]) -> dict:
    """按现行改正表刷新断面的待核零点/原因（不重算最浅值）。

    用于零点只发生“资格细类”变化（MISSING ↔ PENDING 等）而可合图布尔值
    与 rob_hi 未变的情形。若发现断面已全部可合图（资格实际翻转），返回
    None 让调用方改为完整重算。
    """
    out = dict(section)
    zeros = list(out.get("zeros") or out.get("zero_points") or [])
    if not zeros:
        zeros = [p.get("zero_point") for p in out.get("points", [])]
        zeros = [z for z in zeros if z]
    problems: List[dict] = []
    missing: List[str] = []
    unfit: List[dict] = []
    for z in zeros:
        info = zero_index.get(z)
        if info is not None and info.eligible:
            continue
        prob = _problem_for_zero(z, info)
        problems.append(prob)
        if prob["kind"] == "MISSING":
            missing.append(z)
        else:
            unfit.append(prob)
    if not problems and out.get("status") != SECTION_OK:
        # 资格其实发生了翻转：不能只刷新，必须完整重算
        return None  # type: ignore[return-value]
    out["problems"] = problems
    out["missing_zeros"] = missing
    out["lacking_zeros"] = list(missing)
    out["absent_zeros"] = list(missing)
    out["unfit_zeros"] = unfit
    out["not_composable_zeros"] = list(unfit)
    out["invalid_zeros"] = [u["point"] for u in unfit]
    out["blocking_zeros"] = [p["point"] for p in problems]
    if problems:
        out["status"] = SECTION_PENDING
        out["status_code"] = "CHECK"
        out["verdict"] = "CHECK"
        out["status_label"] = "待核"
        out["label"] = "待核"
        out["pending"] = True
        out["needs_check"] = True
        out["compositable"] = False
        parts = []
        if missing:
            parts.append("缺少改正数据：%s" % "、".join(missing))
        if unfit:
            parts.append("不可合图：%s" % "、".join(u["point"] for u in unfit))
        out["reason"] = "；".join(parts)
    return out


def _node_map(source: Any) -> Dict[str, Any]:
    if source is None:
        return {}
    if isinstance(source, Mapping) and "nodes" in source:
        nodes = source["nodes"]
    elif hasattr(source, "nodes"):
        nodes = source.nodes
    elif isinstance(source, Mapping):
        # 可能已经是 point -> node/info 的映射
        return dict(source)
    elif isinstance(source, (list, tuple)):
        out: Dict[str, Any] = {}
        for n in source:
            p = _node_get(n, "point", None)
            if p is not None:
                out[str(p)] = n
        return out
    else:
        return {}
    return {str(_node_get(n, "point")): n for n in nodes
            if _node_get(n, "point", None) is not None}


class SectionReport(list):
    """断面结果列表，附带增量核算元数据；本身可迭代/下标。"""

    def __init__(self, sections: Sequence[SectionResult], **meta: Any) -> None:
        super().__init__(sections)
        self.meta: Dict[str, Any] = dict(meta)

    # 元数据快捷属性
    @property
    def sections(self) -> List[dict]:
        return list(self)

    @property
    def results(self) -> List[dict]:
        return list(self)

    def __getattr__(self, name: str) -> Any:
        if name in self.meta:
            return self.meta[name]
        raise AttributeError(name)

    def to_dict(self) -> dict:
        d = {
            "sections": [dict(s) for s in self],
            "results": [dict(s) for s in self],
            "section_count": len(self),
            "ok_count": sum(1 for s in self if s.get("status") == SECTION_OK),
            "pending_count": sum(1 for s in self if s.get("status") == SECTION_PENDING),
        }
        d.update(self.meta)
        return d

    def __repr__(self) -> str:
        return "SectionReport(%d sections, meta=%r)" % (len(self), self.meta)


def build_section_report(
    soundings: Sequence[Sounding],
    correction_source: Any = None,
    *,
    computed_at: Optional[float] = None,
    mode: str = "full",
    changed_zeros: Iterable[str] = (),
    recomputed_sections: Iterable[str] = (),
    extra: Optional[dict] = None,
) -> SectionReport:
    """全量计算并包装成带元数据的 :class:`SectionReport`。"""
    sections = evaluate_sections(soundings, correction_source)
    import time as _time
    meta: Dict[str, Any] = {
        "mode": mode,
        "changed_zeros": sorted(set(changed_zeros)),
        "recomputed_sections": list(recomputed_sections)
        if not isinstance(recomputed_sections, set) else sorted(recomputed_sections),
        "computed_at": computed_at if computed_at is not None else _time.time(),
        "sounding_count": len(list(soundings)),
    }
    if extra:
        meta.update(extra)
    recomputed_set = set(meta["recomputed_sections"])
    for s in sections:
        s["recomputed"] = (not recomputed_set) or (s["section_id"] in recomputed_set)
    meta["section_count"] = len(sections)
    meta["ok_count"] = sum(1 for s in sections if s["status"] == SECTION_OK)
    meta["pending_count"] = len(sections) - meta["ok_count"]
    return SectionReport(sections, **meta)


def recompute_sections(
    soundings: Sequence[Sounding],
    new_correction: Any,
    *,
    old_correction: Any = None,
    old_report: Any = None,
    old_sections: Optional[Sequence[Mapping[str, Any]]] = None,
) -> SectionReport:
    """联测单变化后的增量重算。

    :param soundings: 现行（已生效）全部测深
    :param new_correction: 新改正表快照 / ComputeReport
    :param old_correction: 旧改正表（与 ``old_report`` 二选一）
    :param old_report: 旧断面快照 payload（含 ``sections``）；其中也可能
           内嵌 ``correction`` 节点表
    :returns: :class:`SectionReport`；只有用到变化零点的断面被重新计算，
           其余断面沿用旧结果字典（标记 ``recomputed=False``）。
    """
    soundings = list(soundings)
    if old_sections is None and isinstance(old_report, Mapping):
        old_sections = old_report.get("sections") or old_report.get("results")
    if old_correction is None and isinstance(old_report, Mapping):
        old_correction = (old_report.get("correction_snapshot")
                          or old_report.get("correction")
                          or old_report.get("zero_table"))

    changed = changed_zero_points(old_correction, new_correction)
    detail_changed = changed_zero_points(
        old_correction, new_correction, include_detail=True) - changed

    groups = _group_by_section(soundings)
    zeros_by_section: Dict[str, Set[str]] = {
        sid: {r.zero_point for r in rows} for sid, rows in groups
    }

    old_list: List[Mapping[str, Any]] = []
    if old_sections:
        old_list = list(old_sections)
    old_by_id = {str(s.get("section_id", s.get("section", s.get("id")))): s
                 for s in old_list if isinstance(s, Mapping)}

    # 题面规定：只重算用到“资格布尔或稳健上界变化零点”的断面。
    affected: Set[str] = {
        sid for sid, zs in zeros_by_section.items() if zs & changed
    }
    # 仅资格细类变化（MISSING↔PENDING）：不进重算集合，只刷新待核原因。
    refresh_only: Set[str] = {
        sid for sid, zs in zeros_by_section.items()
        if not (zs & changed) and (zs & detail_changed)
    }

    zero_index = build_zero_index(new_correction)
    merged: List[dict] = []
    recomputed_ids: List[str] = []
    refreshed_ids: List[str] = []

    # 以测深中的断面顺序为准
    for sid, rows in groups:
        if sid in affected or sid not in old_by_id:
            result = compute_section(sid, rows, zero_index)
            result["recomputed"] = True
            merged.append(dict(result))
            recomputed_ids.append(sid)
        elif sid in refresh_only:
            refreshed = refresh_section_problems(old_by_id[sid], zero_index)
            if refreshed is None:
                # 防御：资格实际翻转 -> 完整重算
                result = compute_section(sid, rows, zero_index)
                result["recomputed"] = True
                merged.append(dict(result))
                recomputed_ids.append(sid)
            else:
                refreshed["recomputed"] = False
                refreshed["blockers_refreshed"] = True
                merged.append(refreshed)
                refreshed_ids.append(sid)
        else:
            old = dict(old_by_id[sid])
            old["recomputed"] = False
            merged.append(old)

    # 旧结果里有、但新测深已不存在的断面自然丢弃；若旧结果含新测深未覆盖
    # 的断面也不保留（整批替换语义下不会发生，这里防御）。
    report = SectionReport(
        [SectionResult(**m) if isinstance(m, Mapping) else m for m in merged],
        mode="incremental",
        changed_zeros=sorted(changed),
        detail_changed_zeros=sorted(detail_changed),
        recomputed_sections=recomputed_ids,
        affected_sections=recomputed_ids,
        blockers_refreshed_sections=refreshed_ids,
        recomputed_count=len(recomputed_ids),
        total_sections=len(merged),
    )
    report.meta["section_count"] = len(merged)
    report.meta["ok_count"] = sum(1 for m in merged if m.get("status") == SECTION_OK)
    report.meta["pending_count"] = len(merged) - report.meta["ok_count"]
    return report


incremental_recompute = recompute_sections
recompute_affected_sections = recompute_sections
