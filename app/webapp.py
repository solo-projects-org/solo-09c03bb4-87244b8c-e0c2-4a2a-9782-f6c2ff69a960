# -*- coding: utf-8 -*-
"""港区水深基准改正表 Flask 应用。

路由
====
GET  /                 现行改正表 + 单据列表（核算期间照常可读旧快照）
GET  /api/table        现行改正表 JSON
GET  /api/sheets       现行单据 JSON
POST /api/sheets       补交联测单（body: {"sheets": [...]}）
PUT  /api/sheets/<sid> 按号换报（body: 单张单据字段）
POST /api/sample       载入样例（仅当库内无单时允许）
GET  /healthz          健康检查
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import List

from flask import Flask, jsonify, render_template, request

from .engine import Sheet, ValidationError
from .sample_data import SAMPLE_SHEETS
from .storage import ContradictionError, StorageError, Store


def _parse_sheet(payload: dict, sid_forced: str = None) -> Sheet:
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    sid = sid_forced or str(payload.get("sid", "")).strip()
    if not sid:
        raise ValidationError("联测单编号不能为空")
    try:
        lo = int(payload["lo"])
        hi = int(payload["hi"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError("高差上下界必须是整数毫米")
    return Sheet(
        sid=sid,
        point_a=str(payload.get("point_a", "")).strip(),
        point_b=str(payload.get("point_b", "")).strip(),
        lo=lo,
        hi=hi,
    )


def create_app(db_path: str = None, datum: str = None, span_limit: int = None) -> Flask:
    db_path = db_path or os.environ.get("DATUM_DB", "/data/datum.db")
    datum = datum or os.environ.get("DATUM_POINT", "D0")
    span_limit = int(span_limit or os.environ.get("SPAN_LIMIT", "20"))

    app = Flask(__name__)

    @app.template_filter("localtime")
    def _localtime(value: float) -> str:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")

    app.config["STORE"] = Store(db_path, datum=datum, span_limit=span_limit)
    store: Store = app.config["STORE"]

    @app.get("/")
    def index():
        snapshot = store.latest_snapshot()
        sheets = store.list_sheets()
        return render_template(
            "index.html",
            snapshot=snapshot,
            sheets=sheets,
            datum=store.datum,
            span_limit=store.span_limit,
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.get("/api/table")
    def api_table():
        snap = store.latest_snapshot()
        if snap is None:
            return jsonify({"error": "尚无改正表快照"}), 404
        return jsonify(snap)

    @app.get("/api/sheets")
    def api_sheets():
        return jsonify({
            "sheets": [
                {"sid": s.sid, "point_a": s.point_a, "point_b": s.point_b,
                 "lo": s.lo, "hi": s.hi}
                for s in store.list_sheets()
            ]
        })

    @app.post("/api/sheets")
    def api_add_sheets():
        try:
            body = request.get_json(force=True, silent=False)
        except Exception:
            return jsonify({"error": "请求体不是合法 JSON"}), 400
        raw_list = body.get("sheets") if isinstance(body, dict) else None
        if not isinstance(raw_list, list) or not raw_list:
            return jsonify({"error": "字段 sheets 必须是非空数组"}), 400
        try:
            parsed: List[Sheet] = [_parse_sheet(item) for item in raw_list]
            report, _ = store.submit_batch(parsed, replaced_ids=[])
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except ContradictionError as exc:
            return jsonify({
                "error": str(exc),
                "contradictions": _contradiction_payload(exc),
            }), 409
        except StorageError as exc:
            return jsonify({"error": "保存失败，已保留现行单据和改正表：%s" % exc}), 500
        return jsonify({"ok": True, "report": store.latest_snapshot()}), 201

    @app.put("/api/sheets/<sid>")
    def api_replace_sheet(sid: str):
        try:
            body = request.get_json(force=True, silent=False)
        except Exception:
            return jsonify({"error": "请求体不是合法 JSON"}), 400
        try:
            sheet = _parse_sheet(body, sid_forced=sid)
            existing = store.get_sheet(sid)
            if existing is None:
                return jsonify({"error": "编号 %s 不存在，不能换报；补交请用 POST" % sid}), 404
            report, _ = store.submit_batch([sheet], replaced_ids=[sid])
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except ContradictionError as exc:
            return jsonify({
                "error": str(exc),
                "contradictions": _contradiction_payload(exc),
            }), 409
        except StorageError as exc:
            return jsonify({"error": "保存失败，已保留现行单据和改正表：%s" % exc}), 500
        return jsonify({"ok": True, "report": store.latest_snapshot()})

    @app.post("/api/sample")
    def api_load_sample():
        if store.list_sheets():
            return jsonify({"error": "库内已有联测单，样例载入已拒绝（避免覆盖现行数据）"}), 409
        try:
            report = store.replace_all(SAMPLE_SHEETS)
        except ContradictionError as exc:
            return jsonify({"error": str(exc),
                            "contradictions": _contradiction_payload(exc)}), 409
        except StorageError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"ok": True, "report": store.latest_snapshot()}), 201

    return app


def _contradiction_payload(exc: ContradictionError) -> list:
    return [
        {
            "cycle_sheet_ids": c.cycle_sheet_ids,
            "cycle_points": c.cycle_points,
            "cycle_bound": c.cycle_bound,
        }
        for c in exc.report.contradictions
    ]


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
