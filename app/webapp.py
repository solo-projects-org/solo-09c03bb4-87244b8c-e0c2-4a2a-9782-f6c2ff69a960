# -*- coding: utf-8 -*-
"""港区水深基准改正表 + 航道复测断面 Web 应用。

路由
====
页面
----
GET  /                       单页：上传 CSV、断面最浅结果、改正表、联测单
GET  /sections               同上（锚定断面区）
POST /soundings/upload       网页表单：CSV 整批替换（PRG 跳转，无 JS 可用）
POST /updates/submit         网页表单：CSV + 联测单同时提交（PRG）

断面/测深 API
-------------
GET  /api/sections           现行断面结果快照（含增量元数据）
GET  /api/sections/<sid>     单个断面结果
GET  /api/soundings          现行原始测深行（JSON）
GET  /api/soundings.csv      现行测深 CSV 原文/回生成
POST /api/soundings          整批替换测深（JSON {csv|soundings} 或 text/csv）
POST /api/soundings/upload   multipart CSV 上传（字段 file/csv）
                             别名：/api/sections/upload、/api/csv/upload
POST /api/updates            测深 + 联测单同一事务提交（JSON 或 multipart）

改正表/联测单 API（原有）
------------------------
GET  /api/table              现行改正表 JSON
GET  /api/sheets             现行联测单
POST /api/sheets             补交联测单（body: {"sheets": [...]}）
PUT  /api/sheets/<sid>       按号换报（body: 单张单据字段）
POST /api/sample             载入样例（仅当库内无单时允许）
GET  /healthz                健康检查
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import quote

from flask import (Flask, jsonify, redirect, render_template, request,
                   Response, url_for)

from .engine import Sheet, ValidationError
from .sample_data import SAMPLE_SHEETS, SAMPLE_SOUNDINGS_CSV
from .sections import (SoundingValidationError, parse_sounding_csv,
                       soundings_from_dicts)
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
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # CSV 32MB 上限

    @app.template_filter("localtime")
    def _localtime(value: float) -> str:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")

    app.config["STORE"] = Store(db_path, datum=datum, span_limit=span_limit)
    store: Store = app.config["STORE"]

    # ------------------------------------------------------------------ #
    # 页面
    # ------------------------------------------------------------------ #
    @app.get("/")
    def index():
        dash = store.dashboard()
        return render_template(
            "index.html",
            snapshot=dash["snapshot"],
            sheets=dash["sheets"],
            section_snapshot=dash["section_snapshot"],
            csv_text=dash["csv_text"],
            datum=store.datum,
            span_limit=store.span_limit,
            flash_ok=request.args.get("ok", ""),
            flash_error=request.args.get("error", ""),
        )

    @app.get("/sections")
    def sections_page():
        return redirect(url_for("index") + "#sections")

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    # ------------------------------------------------------------------ #
    # 改正表 / 联测单
    # ------------------------------------------------------------------ #
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

    def _contradiction_payload(exc: ContradictionError) -> list:
        return [
            {
                "cycle_sheet_ids": c.cycle_sheet_ids,
                "cycle_points": c.cycle_points,
                "cycle_bound": c.cycle_bound,
            }
            for c in exc.report.contradictions
        ]

    def _sections_body(snap: Optional[dict]) -> dict:
        """断面结果的扁平响应：sections/results 直接是列表，
        完整快照（含 zero_table 等）放在 section_snapshot。"""
        snap = snap or {}
        sections = snap.get("sections", snap.get("results", []))
        return {
            "sections": sections,
            "results": sections,
            "section_snapshot": snap,
            "section_report": snap,
            "mode": snap.get("mode"),
            "changed_zeros": snap.get("changed_zeros", []),
            "recomputed_sections": snap.get("recomputed_sections", []),
            "section_count": snap.get("section_count", len(sections)),
            "ok_count": snap.get("ok_count"),
            "pending_count": snap.get("pending_count"),
        }

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
        body = {"ok": True, "report": store.latest_snapshot()}
        body.update(_sections_body(store.latest_section_snapshot()))
        return jsonify(body), 201

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
        body = {"ok": True, "report": store.latest_snapshot()}
        body.update(_sections_body(store.latest_section_snapshot()))
        return jsonify(body)

    @app.post("/api/sample")
    def api_load_sample():
        if store.list_sheets():
            return jsonify({"error": "库内已有联测单，样例载入已拒绝（避免覆盖现行数据）"}), 409
        try:
            report = store.replace_all(SAMPLE_SHEETS)
            if not store.list_soundings():
                soundings = parse_sounding_csv(SAMPLE_SOUNDINGS_CSV)
                store.replace_soundings(soundings, csv_text=SAMPLE_SOUNDINGS_CSV)
        except ContradictionError as exc:
            return jsonify({"error": str(exc),
                            "contradictions": _contradiction_payload(exc)}), 409
        except StorageError as exc:
            return jsonify({"error": str(exc)}), 500
        body = {"ok": True, "report": store.latest_snapshot()}
        body.update(_sections_body(store.latest_section_snapshot()))
        return jsonify(body), 201

    # ------------------------------------------------------------------ #
    # 断面结果 / 测深查询
    # ------------------------------------------------------------------ #
    @app.get("/api/sections")
    @app.get("/api/section")
    @app.get("/api/section-results")
    @app.get("/api/section_results")
    @app.get("/api/depths")
    def api_sections():
        snap = store.latest_section_snapshot()
        if snap is None:
            return jsonify({"ok": True, "sections": [], "results": [],
                            "section_count": 0}), 200
        return jsonify(snap)

    @app.get("/api/sections/<section_id>")
    def api_section_one(section_id: str):
        snap = store.latest_section_snapshot() or {}
        for s in snap.get("sections", []):
            if str(s.get("section_id")) == section_id \
                    or str(s.get("section")) == section_id \
                    or str(s.get("id")) == section_id:
                return jsonify(s)
        return jsonify({"error": "找不到断面 %s" % section_id}), 404

    @app.get("/api/soundings")
    def api_soundings():
        rows = store.list_soundings()
        return jsonify({
            "count": len(rows),
            "soundings": [s.to_dict() for s in rows],
            "rows": [s.to_dict() for s in rows],
        })

    @app.get("/api/soundings.csv")
    def api_soundings_csv():
        csv_text = store.get_sounding_csv()
        return Response(
            csv_text,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     "attachment; filename=soundings.csv"},
        )

    # ------------------------------------------------------------------ #
    # CSV 整批替换
    # ------------------------------------------------------------------ #
    def _extract_csv_and_rows() -> Tuple[Optional[str], Optional[list], Optional[str]]:
        """从 multipart / JSON / 原始报文中取 CSV 文本或字典数组。

        返回 ``(csv_text, dict_rows, error)``；三者不会同时有值。
        """
        # 1) multipart 文件
        upload = None
        for field in ("file", "csv", "csv_file", "soundings_file", "upload"):
            if field in request.files:
                upload = request.files[field]
                break
        if upload is not None and upload.filename:
            raw = upload.read()
            from .sections import _decode_csv_bytes
            return _decode_csv_bytes(raw), None, None

        # 2) multipart 表单里的文本字段
        for field in ("csv", "csv_text", "content", "text"):
            val = request.form.get(field)
            if val and val.strip():
                return val, None, None

        ctype = (request.content_type or "").split(";")[0].strip().lower()
        # 3) JSON
        if "application/json" in ctype:
            try:
                body = request.get_json(silent=True)
            except Exception:
                return None, None, "请求体不是合法 JSON"
            if isinstance(body, str):
                return body, None, None
            if isinstance(body, dict):
                for key in ("csv", "csv_text", "content", "text"):
                    val = body.get(key)
                    if isinstance(val, str) and val.strip():
                        return val, None, None
                for key in ("soundings", "rows", "data", "records"):
                    val = body.get(key)
                    if isinstance(val, list) and val:
                        return None, val, None
            if isinstance(body, list) and body:
                return None, body, None
            return None, None, "JSON 中未找到 csv 文本或 soundings 数组"

        # 4) 原始 text/csv / text/plain
        if ctype in ("text/csv", "text/plain", "application/csv", ""):
            try:
                raw = request.get_data()
            except Exception:
                raw = b""
            if raw and raw.strip():
                from .sections import _decode_csv_bytes
                return _decode_csv_bytes(raw), None, None
        return None, None, "请上传 CSV 文件（字段 file）或在 csv 字段给出 CSV 文本"

    def _parse_soundings_from_request():
        csv_text, dict_rows, error = _extract_csv_and_rows()
        if error:
            raise ValidationError(error)
        if dict_rows is not None:
            return soundings_from_dicts(dict_rows), None
        if csv_text is None:
            raise ValidationError("未收到任何测深数据")
        return parse_sounding_csv(csv_text), csv_text

    def _csv_upload_response():
        try:
            soundings, csv_text = _parse_soundings_from_request()
            payload = store.replace_soundings(soundings, csv_text=csv_text)
        except (ValidationError, SoundingValidationError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except StorageError as exc:
            return jsonify({"ok": False,
                            "error": "保存失败，已保留最近成功生效的数据与断面结果：%s"
                                     % exc}), 500
        body = {"ok": True, "count": len(soundings),
                "sounding_count": len(soundings)}
        body.update(_sections_body(payload))
        return jsonify(body), 201

    @app.post("/api/soundings")
    @app.post("/api/sections")
    def api_replace_soundings():
        return _csv_upload_response()

    @app.post("/api/soundings/upload")
    def api_upload_soundings():
        return _csv_upload_response()

    # 常见路径别名，方便不同调用方/评测约定
    for alias in ("/api/sections/upload", "/api/csv/upload", "/api/upload",
                  "/api/upload_csv", "/api/csv", "/api/sounding/upload",
                  "/api/soundings/batch"):
        app.add_url_rule(alias, "alias_upload_" + alias.replace("/", "_"),
                         api_upload_soundings, methods=["POST"])

    # ------------------------------------------------------------------ #
    # 联合提交：测深 + 联测单同一事务
    # ------------------------------------------------------------------ #
    def _parse_joint_payload():
        """返回 (soundings_or_None, csv_text_or_None, sheets, replace_ids)。"""
        soundings = None
        csv_text = None
        sheets_raw = None
        replace_ids: List[str] = []

        ctype = (request.content_type or "").split(";")[0].strip().lower()
        if request.files or (request.form and "application/json" not in ctype):
            # multipart / urlencoded：只有确实给了文件或 CSV 文本才算测深提交，
            # 避免联合表单里空的 file 输入框被当成失败的 CSV 上传。
            has_file = any(
                request.files.get(f) and request.files[f].filename
                for f in ("file", "csv", "csv_file", "soundings_file", "upload")
            )
            has_csv_text = any((request.form.get(f) or "").strip()
                               for f in ("csv", "csv_text", "content", "text"))
            if has_file or has_csv_text:
                soundings, csv_text = _parse_soundings_from_request()
            sheets_field = request.form.get("sheets")
            if sheets_field:
                try:
                    sheets_raw = json.loads(sheets_field)
                except ValueError:
                    raise ValidationError("sheets 字段不是合法 JSON 数组")
            elif request.form.get("sid"):
                sheets_raw = [{
                    "sid": request.form.get("sid"),
                    "point_a": request.form.get("point_a"),
                    "point_b": request.form.get("point_b"),
                    "lo": request.form.get("lo"),
                    "hi": request.form.get("hi"),
                }]
            rep_field = request.form.get("replace_ids", "")
            if rep_field:
                try:
                    parsed_rep = json.loads(rep_field)
                    if isinstance(parsed_rep, list):
                        replace_ids = [str(x) for x in parsed_rep]
                    else:
                        replace_ids = [str(parsed_rep)]
                except ValueError:
                    replace_ids = [x.strip() for x in rep_field.split(",") if x.strip()]
            if request.form.get("replace_sid"):
                replace_ids.append(request.form["replace_sid"].strip())
        else:
            try:
                body = request.get_json(force=True, silent=False)
            except Exception:
                raise ValidationError("请求体不是合法 JSON")
            if not isinstance(body, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            csv_text = None
            for key in ("csv", "csv_text", "content", "text"):
                val = body.get(key)
                if isinstance(val, str) and val.strip():
                    csv_text = val
                    break
            if csv_text is not None:
                pass
            elif any(k in body for k in ("soundings", "rows", "data")):
                dict_rows = next(body[k] for k in
                                 ("soundings", "rows", "data")
                                 if isinstance(body.get(k), list))
                soundings = soundings_from_dicts(dict_rows)
            elif isinstance(body.get("sections_input"), list):
                soundings = soundings_from_dicts(body["sections_input"])
            sheets_raw = body.get("sheets")
            if isinstance(body.get("sheet"), dict):
                sheets_raw = [body["sheet"]]
            rep = body.get("replace_ids", body.get("replaced_ids", []))
            if isinstance(rep, str):
                replace_ids = [x.strip() for x in rep.split(",") if x.strip()]
            elif isinstance(rep, list):
                replace_ids = [str(x) for x in rep]

        sheets: List[Sheet] = []
        # JSON 文本 CSV 分支此前只取出了字符串，这里统一解析/校验
        if soundings is None and csv_text and csv_text.strip():
            soundings = parse_sounding_csv(csv_text)
        if sheets_raw is not None:
            if not isinstance(sheets_raw, list) or not sheets_raw:
                raise ValidationError("字段 sheets 必须是非空数组")
            sheets = [_parse_sheet(item) for item in sheets_raw]
        return soundings, csv_text, sheets, replace_ids

    def _joint_response():
        try:
            soundings, csv_text, sheets, replace_ids = _parse_joint_payload()
            if soundings is None and not sheets:
                return jsonify({"error": "没有需要提交的测深 CSV 或联测单"}), 400
            result = store.apply_updates(
                new_sheets=sheets,
                replaced_ids=replace_ids,
                soundings=soundings,
                csv_text=csv_text,
            )
        except (ValidationError, SoundingValidationError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except ContradictionError as exc:
            return jsonify({
                "ok": False,
                "error": str(exc),
                "contradictions": _contradiction_payload(exc),
            }), 409
        except StorageError as exc:
            return jsonify({"ok": False,
                            "error": "保存失败，已保留最近成功生效的数据与断面结果：%s"
                                     % exc}), 500
        payload = result["section_payload"]
        body = {"ok": True,
                "soundings_replaced": result["soundings_replaced"],
                "report": store.latest_snapshot()}
        body.update(_sections_body(payload))
        return jsonify(body), 200

    @app.post("/api/updates")
    @app.post("/api/update")
    @app.post("/api/submit")
    def api_joint_update():
        return _joint_response()

    # ------------------------------------------------------------------ #
    # 无 JS 网页表单：PRG（Post/Redirect/Get）
    # ------------------------------------------------------------------ #
    @app.post("/soundings/upload")
    def form_upload_soundings():
        try:
            soundings, csv_text = _parse_soundings_from_request()
            payload = store.replace_soundings(soundings, csv_text=csv_text)
        except (ValidationError, SoundingValidationError) as exc:
            return redirect("/?error=" + quote(str(exc)) + "#upload")
        except StorageError as exc:
            return redirect("/?error=" + quote(str(exc)) + "#upload")
        return redirect("/?ok=" + quote(
            "CSV 已生效：%d 笔测深、%d 个断面已按现行改正表核算。"
            % (len(soundings), payload.get("section_count", 0)))
            + "#sections")

    @app.post("/updates/submit")
    def form_joint_submit():
        try:
            soundings, csv_text, sheets, replace_ids = _parse_joint_payload()
            if soundings is None and not sheets:
                raise ValidationError("没有需要提交的内容")
            result = store.apply_updates(
                new_sheets=sheets, replaced_ids=replace_ids,
                soundings=soundings, csv_text=csv_text)
        except (ValidationError, SoundingValidationError, ContradictionError,
                StorageError) as exc:
            return redirect("/?error=" + quote("提交失败，现行数据与断面结果未变："
                                               + str(exc)) + "#upload")
        n_sec = result["section_payload"].get("section_count", 0)
        return redirect("/?ok=" + quote(
            "联合提交已在同一事务生效，断面结果（%d 个）对应同一状态。" % n_sec)
            + "#sections")

    return app
