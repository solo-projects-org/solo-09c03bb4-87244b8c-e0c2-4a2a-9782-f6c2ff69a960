# -*- coding: utf-8 -*-
"""航道复测断面功能测试：CSV 校验、最浅/并列、待核、增量重算、原子联合提交。

需要 flask（与 tests/test_storage_web.py 相同，用 PYTHONPATH 指向依赖目录）：
    PYTHONPATH=/tmp/pylibs python tests/test_sections.py
也可被 pytest 收集。
"""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import Sheet, compute  # noqa: E402
from app.sections import (  # noqa: E402
    Sounding,
    changed_zero_points,
    compute_sections,
    parse_sounding_csv,
    recompute_sections,
)
from app.storage import Store  # noqa: E402
from app.webapp import create_app  # noqa: E402


def _tmp_db():
    d = tempfile.mkdtemp(prefix="section-test-")
    return os.path.join(d, "test.db")


GOOD_CSV = """\
断面号,测点号,零点,原水深
SEC1,P1,D0,2000
SEC1,P2,Z1,2102
SEC1,P3,Z1,2300
SEC2,Q1,Z2,1599
SEC2,Q2,D0,1650
SEC3,R1,Z3,1400
SEC4,T1,Z4,1200
SEC5,U1,Z5,1300
SEC6,V1,Z9,1100
"""


def _sample_store(db=None):
    from app.sample_data import SAMPLE_SHEETS
    store = Store(db or _tmp_db())
    store.replace_all(SAMPLE_SHEETS)
    return store


# --------------------------------------------------------------------------- #
# 纯领域逻辑
# --------------------------------------------------------------------------- #
def test_parse_header_and_headerless_and_encoding():
    rows = parse_sounding_csv(GOOD_CSV)
    assert len(rows) == 9
    assert rows[0].section_id == "SEC1" and rows[0].raw_depth == 2000
    # 无表头
    rows2 = parse_sounding_csv("A,p1,D0,100\nA,p2,Z1,200\n")
    assert [p.point_id for p in rows2] == ["p1", "p2"]
    # 英文表头 + BOM
    en = "section,point,zero,depth\nA,p,D0,1234\n"
    rows3 = parse_sounding_csv("﻿" + en)
    assert rows3[0].raw_depth == 1234 and rows3[0].zero_point == "D0"
    # GBK 字节
    gbk = "断面号,测点号,零点,原水深\nA,p,D0,100\n".encode("gbk")
    assert parse_sounding_csv(gbk)[0].section_id == "A"
    print("test_parse_header_and_headerless_and_encoding: 通过")


def test_duplicate_point_in_section_rejected_but_cross_section_ok():
    try:
        parse_sounding_csv("A,p1,D0,1\nA,p1,Z1,2\n")
    except Exception as exc:
        assert "p1" in str(exc) and "重复" in str(exc)
    else:
        raise AssertionError("同断面重复测点号必须拒绝")
    rows = parse_sounding_csv("A,p1,D0,1\nB,p1,D0,2\n")
    assert len(rows) == 2
    print("test_duplicate_point_in_section_rejected_but_cross_section_ok: 通过")


def test_bad_rows_rejected():
    bad_cases = [
        "A,p,D0,not-an-int\n",
        "A,,D0,100\n",
        "A,p,,100\n",
        "A,p,D0,\n",
        "A,p,D0,1,extra\n",
        "",
        "a,b,c,d\n",  # 只有无法识别的表头
    ]
    for text in bad_cases:
        try:
            parse_sounding_csv(text)
        except Exception:
            pass
        else:
            raise AssertionError("非法 CSV 应被拒绝：%r" % text)
    print("test_bad_rows_rejected: 通过")


def test_shallowest_math_ties_and_pending_reasons():
    from app.sample_data import SAMPLE_SHEETS
    report = compute(SAMPLE_SHEETS)
    rows = parse_sounding_csv(GOOD_CSV)
    sections = {s["section_id"]: s for s in compute_sections(rows, report)}

    s1 = sections["SEC1"]
    assert s1["status"] == "OK"
    assert s1["shallowest_depth"] == 2000          # 2102 - rob_hi(Z1)=102
    assert s1["shallowest_points"] == ["P1", "P2"]  # 并列
    assert s1["shallowest_depth_m"] == 2.0

    s2 = sections["SEC2"]
    assert s2["shallowest_depth"] == 1500          # 1599 - 99
    assert s2["shallowest_points"] == ["Q1"]

    assert sections["SEC3"]["status"] == "PENDING"
    assert sections["SEC3"]["missing_zeros"] == []
    assert [z["point"] for z in sections["SEC3"]["unfit_zeros"]] == ["Z3"]

    assert [z["point"] for z in sections["SEC4"]["unfit_zeros"]] == ["Z4"]
    assert [z["point"] for z in sections["SEC5"]["unfit_zeros"]] == ["Z5"]

    s6 = sections["SEC6"]
    assert s6["status"] == "PENDING"
    assert s6["missing_zeros"] == ["Z9"]
    assert s6["shallowest_depth"] is None
    print("test_shallowest_math_ties_and_pending_reasons: 通过")


def test_changed_zero_points_signature():
    old = compute([Sheet("A1", "D0", "Z1", 10, 14)])
    new_same = compute([Sheet("A1", "D0", "Z1", 10, 14),
                        Sheet("A2", "D0", "Z2", 0, 4)])
    changed = changed_zero_points(old, new_same)
    assert "Z2" in changed          # 新零点（缺失 -> PENDING/OK 资格细类变化）
    assert "Z1" not in changed      # 资格与 rob_hi 均未变
    assert "D0" not in changed

    new_hi = compute([Sheet("A1", "D0", "Z1", 10, 20)])
    changed2 = changed_zero_points(old, new_hi)
    assert "Z1" in changed2         # rob_hi 14 -> 20
    print("test_changed_zero_points_signature: 通过")


def test_recompute_only_affected_sections_including_pending():
    from app.sample_data import SAMPLE_SHEETS
    store = _sample_store()
    payload = store.replace_soundings(parse_sounding_csv(GOOD_CSV))
    assert payload["section_count"] == 6

    # 待核断面 SEC6 用 Z9；补两张紧复测单让 Z9 可合图
    report, _ = store.submit_batch([
        Sheet("N1", "D0", "Z9", 40, 42),
        Sheet("N2", "D0", "Z9", 40, 42),
    ])
    snap = store.latest_section_snapshot()
    assert snap["mode"] == "incremental"
    assert snap["recomputed_sections"] == ["SEC6"], snap["recomputed_sections"]
    sec6 = next(s for s in snap["sections"] if s["section_id"] == "SEC6")
    assert sec6["status"] == "OK"
    assert sec6["shallowest_depth"] == 1058   # 1100 - 42
    others = [s for s in snap["sections"] if s["section_id"] != "SEC6"]
    assert all(s["recomputed"] is False for s in others)
    print("test_recompute_only_affected_sections_including_pending: 通过")


def test_recompute_strict_set_and_detail_refresh():
    """MISSING->PENDING 不改资格布尔/rob_hi：不重算，只刷新待核原因。"""
    store = _sample_store()
    store.replace_soundings(parse_sounding_csv(
        "D1,p1,ZZ,1000\nD1,p2,D0,1000\n"))
    before = store.latest_section_snapshot()
    d1 = next(s for s in before["sections"] if s["section_id"] == "D1")
    assert d1["missing_zeros"] == ["ZZ"]

    # 只把 ZZ 连到另一未联基准点（仍 PENDING，rob_hi 仍 None）
    store.submit_batch([Sheet("E1", "ZZ", "ZW", 3, 5)])
    snap = store.latest_section_snapshot()
    assert snap["changed_zeros"] == []
    assert snap["recomputed_sections"] == []
    d1 = next(s for s in snap["sections"] if s["section_id"] == "D1")
    assert d1["recomputed"] is False
    # 待核原因已从“缺少改正数据”刷新为“不可合图”
    assert d1["missing_zeros"] == []
    assert [z["point"] for z in d1["unfit_zeros"]] == ["ZZ"]
    assert d1.get("blockers_refreshed") is True

    # 再补两张紧单把 ZZ 联到 D0 -> 资格翻转，必须重算并出值
    store.submit_batch([
        Sheet("E2", "D0", "ZZ", 20, 22),
        Sheet("E3", "D0", "ZZ", 20, 22),
    ])
    snap = store.latest_section_snapshot()
    assert "ZZ" in snap["changed_zeros"]
    assert snap["recomputed_sections"] == ["D1"]
    d1 = next(s for s in snap["sections"] if s["section_id"] == "D1")
    assert d1["status"] == "OK"
    assert d1["shallowest_depth"] == 978  # 1000 - 22
    print("test_recompute_strict_set_and_detail_refresh: 通过")


def test_recompute_pure_carries_untouched_sections():
    from app.sample_data import SAMPLE_SHEETS
    old_report = compute(SAMPLE_SHEETS)
    rows = parse_sounding_csv(GOOD_CSV)
    first = recompute_sections(rows, old_report, old_correction=old_report,
                               old_report={"sections": []})
    # 无旧断面 -> 全部新算
    assert set(first.recomputed_sections) == {
        "SEC1", "SEC2", "SEC3", "SEC4", "SEC5", "SEC6"}

    new_sheets = SAMPLE_SHEETS + [
        Sheet("N1", "D0", "Z9", 40, 42),
        Sheet("N2", "D0", "Z9", 40, 42),
    ]
    new_report = compute(new_sheets)
    second = recompute_sections(
        rows, new_report, old_correction=old_report,
        old_report={"sections": first.to_dict()["sections"]})
    assert second.recomputed_sections == ["SEC6"]
    by_id = {s["section_id"]: s for s in second}
    assert by_id["SEC6"]["status"] == "OK"
    assert by_id["SEC1"]["recomputed"] is False
    print("test_recompute_pure_carries_untouched_sections: 通过")


# --------------------------------------------------------------------------- #
# 存储事务 / Web
# --------------------------------------------------------------------------- #
def test_store_replace_and_persist_soundings():
    db = _tmp_db()
    store = _sample_store(db)
    payload = store.replace_soundings(parse_sounding_csv(GOOD_CSV),
                                      csv_text=GOOD_CSV)
    assert payload["section_count"] == 6
    store.close()
    store2 = Store(db)
    assert len(store2.list_soundings()) == 9
    snap = store2.latest_section_snapshot()
    assert any(s["section_id"] == "SEC1" for s in snap["sections"])
    assert "SEC1,P1,D0,2000" in store2.get_sounding_csv()
    store2.close()
    print("test_store_replace_and_persist_soundings: 通过")


def test_failed_csv_keeps_previous_state():
    store = _sample_store()
    good = parse_sounding_csv(GOOD_CSV)
    store.replace_soundings(good)
    before_rows = len(store.list_soundings())
    before_snap = store.latest_section_snapshot()
    try:
        store.replace_soundings(parse_sounding_csv("A,p1,D0,1\nA,p1,Z,2\n"))
    except Exception:
        pass
    else:
        raise AssertionError("重复测点 CSV 应失败")
    assert len(store.list_soundings()) == before_rows
    assert store.latest_section_snapshot() == before_snap
    print("test_failed_csv_keeps_previous_state: 通过")


def test_web_upload_csv_and_view():
    app = create_app(db_path=_tmp_db())
    c = app.test_client()
    r = c.post("/api/sample")
    assert r.status_code == 201
    # 样例已含测深
    page = c.get("/").get_data(as_text=True)
    assert "SEC01" in page and "待核" in page and "并列最浅测点号" in page

    csv_bytes = GOOD_CSV.encode("utf-8")
    r = c.post("/api/soundings/upload",
               data={"file": (io.BytesIO(csv_bytes), "h.csv")},
               content_type="multipart/form-data")
    assert r.status_code == 201, r.data
    data = r.get_json()
    assert data["ok"] and data["count"] == 9
    assert isinstance(data["sections"], list)
    sec1 = next(s for s in data["sections"] if s["section_id"] == "SEC1")
    assert sec1["shallowest_depth"] == 2000
    assert sec1["shallowest_points"] == ["P1", "P2"]

    # JSON 原始 text/csv
    r = c.post("/api/soundings", data="W,p,D0,888\n",
               content_type="text/csv")
    assert r.status_code == 201
    assert r.get_json()["sections"][0]["shallowest_depth"] == 888

    # JSON {csv: ...}
    r = c.post("/api/soundings", json={"csv": "J,p,D0,777\n"})
    assert r.status_code == 201
    assert c.get("/api/soundings").get_json()["count"] == 1

    # 坏 CSV -> 400
    r = c.post("/api/soundings", json={"csv": "J,p,D0,x\n"})
    assert r.status_code == 400
    assert c.get("/api/soundings").get_json()["count"] == 1  # 旧数据保留

    # 断面查询 / CSV 下载
    assert c.get("/api/sections/J").status_code == 200
    assert c.get("/api/sections/NOPE").status_code == 404
    assert "J,p,D0,777" in c.get("/api/soundings.csv").get_data(as_text=True)
    app.config["STORE"].close()
    print("test_web_upload_csv_and_view: 通过")


def test_web_joint_submission_atomic():
    app = create_app(db_path=_tmp_db())
    c = app.test_client()
    c.post("/api/sample")
    before_soundings = c.get("/api/soundings").get_json()["count"]

    # 成功的联合提交
    r = c.post("/api/updates", json={
        "csv": "J1,a,D0,500\nJ1,b,JZ,600\n",
        "sheets": [
            {"sid": "J01", "point_a": "D0", "point_b": "JZ", "lo": 5, "hi": 8},
            {"sid": "J02", "point_a": "D0", "point_b": "JZ", "lo": 5, "hi": 8},
        ],
    })
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["mode"] == "joint-full"
    j1 = next(s for s in body["sections"] if s["section_id"] == "J1")
    assert j1["status"] == "OK" and j1["shallowest_depth"] == 500

    # 矛盾联测单 + 新 CSV：409，测深/单据/断面全部沿用旧状态
    cur_sections = c.get("/api/sections").get_json()["sections"]
    cur_sheets = len(c.get("/api/sheets").get_json()["sheets"])
    r = c.post("/api/updates", json={
        "csv": "SHOULD,GONE,D0,1\n",
        "sheets": [
            {"sid": "B1", "point_a": "D0", "point_b": "A", "lo": 10, "hi": 12},
            {"sid": "B2", "point_a": "A", "point_b": "B", "lo": 0, "hi": 0},
            {"sid": "B3", "point_a": "B", "point_b": "D0", "lo": -9, "hi": -9},
        ],
    })
    assert r.status_code == 409
    assert r.get_json()["contradictions"]
    assert c.get("/api/soundings").get_json()["count"] == 2
    assert len(c.get("/api/sheets").get_json()["sheets"]) == cur_sheets
    after_sections = c.get("/api/sections").get_json()["sections"]
    assert after_sections == cur_sections
    assert not any(s["section_id"] == "SHOULD" for s in after_sections)

    # 坏 CSV + 好联测单：400，联测单也不得生效
    r = c.post("/api/updates", json={
        "csv": "DUP,p,D0,1\nDUP,p,D0,2\n",
        "sheets": [{"sid": "L1", "point_a": "D0", "point_b": "LZ",
                    "lo": 1, "hi": 2}],
    })
    assert r.status_code == 400
    assert all(s["sid"] != "L1"
               for s in c.get("/api/sheets").get_json()["sheets"])
    assert c.get("/api/soundings").get_json()["count"] == 2
    assert before_soundings  # 样例原本有测深（这里仅确认变量有意义）

    # multipart 联合提交
    r = c.post(
        "/api/updates",
        data={
            "file": (io.BytesIO(b"M1,p,D0,333\n"), "m.csv"),
            "sheets": '[{"sid":"M01","point_a":"D0","point_b":"MZ",'
                      '"lo":1,"hi":1}]',
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.data
    m1 = next(s for s in r.get_json()["sections"]
              if s["section_id"] == "M1")
    assert m1["shallowest_depth"] == 333
    app.config["STORE"].close()
    print("test_web_joint_submission_atomic: 通过")


def test_web_prg_form_upload_and_flash():
    app = create_app(db_path=_tmp_db())
    c = app.test_client()
    r = c.post("/soundings/upload", data={"csv": "PRG,p1,D0,777\n"})
    assert r.status_code in (301, 302)
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "PRG" in page and "CSV 已生效" in page

    r = c.post("/soundings/upload", data={"csv": "BAD,p,D0,oops\n"})
    assert r.status_code in (301, 302)
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "整数毫米" in page
    # 旧结果仍在
    assert c.get("/api/sections/PRG").status_code == 200
    app.config["STORE"].close()
    print("test_web_prg_form_upload_and_flash: 通过")


def test_sounding_constructor_aliases():
    a = Sounding("S", "P", "D0", 100)
    b = Sounding(section="S", point="P", zero="D0", depth=100)
    assert a == b and a.section == "S" and a.zero == "D0"
    assert tuple(a) == ("S", "P", "D0", 100)
    print("test_sounding_constructor_aliases: 通过")


def _run_all():
    test_parse_header_and_headerless_and_encoding()
    test_duplicate_point_in_section_rejected_but_cross_section_ok()
    test_bad_rows_rejected()
    test_shallowest_math_ties_and_pending_reasons()
    test_changed_zero_points_signature()
    test_recompute_only_affected_sections_including_pending()
    test_recompute_strict_set_and_detail_refresh()
    test_recompute_pure_carries_untouched_sections()
    test_store_replace_and_persist_soundings()
    test_failed_csv_keeps_previous_state()
    test_web_upload_csv_and_view()
    test_web_joint_submission_atomic()
    test_web_prg_form_upload_and_flash()
    test_sounding_constructor_aliases()
    print("\n全部断面/测深测试通过。")


if __name__ == "__main__":
    _run_all()
