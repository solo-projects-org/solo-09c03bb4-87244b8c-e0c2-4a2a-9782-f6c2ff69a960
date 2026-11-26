# -*- coding: utf-8 -*-
"""存储层 + Flask 路由测试（需要 flask；用 PYTHONPATH 指向本地依赖运行）。"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import Sheet, ValidationError  # noqa: E402
from app.storage import ContradictionError, Store, StorageError  # noqa: E402
from app.webapp import create_app  # noqa: E402


def _tmp_db():
    d = tempfile.mkdtemp(prefix="datum-test-")
    return os.path.join(d, "test.db")


def test_store_empty_init_snapshot():
    store = Store(_tmp_db())
    snap = store.latest_snapshot()
    assert snap is not None and snap["sheet_count"] == 0
    assert any(n["point"] == "D0" and n["status"] == "OK" for n in snap["nodes"])
    store.close()
    print("test_store_empty_init_snapshot: 通过")


def test_add_replace_and_persist():
    db = _tmp_db()
    store = Store(db)
    report, all_sheets = store.submit_batch([Sheet("A1", "D0", "Z", 10, 14)])
    assert report.ok and len(all_sheets) == 1
    snap = store.latest_snapshot()
    z = next(n for n in snap["nodes"] if n["point"] == "Z")
    assert (z["rob_lo"], z["rob_hi"]) == (10, 14)

    # 补交
    store.submit_batch([Sheet("A2", "D0", "Z", 12, 16)])
    assert len(store.list_sheets()) == 2
    # 按号换报
    store.submit_batch([Sheet("A2", "D0", "Z", 12, 15)], replaced_ids=["A2"])
    got = store.get_sheet("A2")
    assert (got.lo, got.hi) == (12, 15)

    # 重开连接，数据与快照仍在
    store.close()
    store2 = Store(db)
    assert len(store2.list_sheets()) == 2
    assert store2.latest_snapshot()["sheet_count"] == 2
    store2.close()
    print("test_add_replace_and_persist: 通过")


def test_duplicate_sid_rejected_without_replace():
    store = Store(_tmp_db())
    store.submit_batch([Sheet("A1", "D0", "Z", 10, 14)])
    before = store.list_sheets()
    try:
        store.submit_batch([Sheet("A1", "D0", "Q", 0, 2)])
    except ValidationError:
        pass
    else:
        raise AssertionError("同号补交应拒绝")
    assert store.list_sheets() == before  # 现行数据不变
    print("test_duplicate_sid_rejected_without_replace: 通过")


def test_replace_missing_id_rejected():
    store = Store(_tmp_db())
    try:
        store.submit_batch([Sheet("X9", "D0", "Z", 0, 2)], replaced_ids=["X9"])
    except ValidationError:
        pass
    else:
        raise AssertionError("换报不存在编号应拒绝")
    print("test_replace_missing_id_rejected: 通过")


def test_contradiction_keeps_current_data():
    store = Store(_tmp_db())
    store.submit_batch([
        Sheet("C1", "D0", "A", 10, 12),
        Sheet("C2", "A", "B", 0, 0),
    ])
    good_snap = store.latest_snapshot()
    # 引入矛盾单 B->D0 精确 -9，闭环上界 -1
    try:
        store.submit_batch([Sheet("C3", "B", "D0", -9, -9)])
    except ContradictionError as exc:
        assert exc.report.contradictions
        assert set(exc.report.contradictions[0].cycle_sheet_ids) == {"C1", "C2", "C3"}
    else:
        raise AssertionError("矛盾单应抛 ContradictionError")
    # 现行单据仍是两张，改正表仍是旧快照
    assert [s.sid for s in store.list_sheets()] == ["C1", "C2"]
    assert store.latest_snapshot() == good_snap
    print("test_contradiction_keeps_current_data: 通过")


def test_replace_contradiction_keeps_current_data():
    store = Store(_tmp_db())
    store.submit_batch([
        Sheet("C1", "D0", "A", 10, 12),
        Sheet("C2", "A", "B", 0, 0),
        Sheet("C3", "B", "D0", -20, -8),   # 与 C1/C2 相容
    ])
    good = store.latest_snapshot()
    try:
        # 把 C3 换成精确 -9 -> 矛盾
        store.submit_batch([Sheet("C3", "B", "D0", -9, -9)], replaced_ids=["C3"])
    except ContradictionError:
        pass
    else:
        raise AssertionError("换报后矛盾应报错")
    assert store.get_sheet("C3").lo == -20  # 旧值保留
    assert store.latest_snapshot() == good
    print("test_replace_contradiction_keeps_current_data: 通过")


def test_flask_routes_happy_path():
    app = create_app(db_path=_tmp_db())
    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/table").status_code == 200
    # 补单
    r = client.post("/api/sheets", json={"sheets": [
        {"sid": "W1", "point_a": "D0", "point_b": "Z", "lo": 10, "hi": 14}
    ]})
    assert r.status_code == 201, r.data
    # 同号 -> PUT 换报
    r = client.put("/api/sheets/W1", json={
        "sid": "W1", "point_a": "D0", "point_b": "Z", "lo": 11, "hi": 13})
    assert r.status_code == 200
    assert client.get("/api/sheets").get_json()["sheets"][0]["lo"] == 11
    # 换报不存在编号
    r = client.put("/api/sheets/NOPE", json={
        "point_a": "D0", "point_b": "Z", "lo": 1, "hi": 2})
    assert r.status_code == 404
    # 坏 JSON / 坏字段
    assert client.post("/api/sheets", data="not-json",
                       content_type="application/json").status_code == 400
    r = client.post("/api/sheets", json={"sheets": [
        {"sid": "B1", "point_a": "D0", "point_b": "Z", "lo": 9, "hi": 1}]})
    assert r.status_code == 400
    # 首页渲染
    html = client.get("/").get_data(as_text=True)
    assert "港区水深基准改正表" in html and "W1" in html
    app.config["STORE"].close()
    print("test_flask_routes_happy_path: 通过")


def test_flask_contradiction_returns_409_and_keeps_state():
    app = create_app(db_path=_tmp_db())
    client = app.test_client()
    client.post("/api/sheets", json={"sheets": [
        {"sid": "C1", "point_a": "D0", "point_b": "A", "lo": 10, "hi": 12},
        {"sid": "C2", "point_a": "A", "point_b": "B", "lo": 0, "hi": 0},
    ]})
    r = client.post("/api/sheets", json={"sheets": [
        {"sid": "C3", "point_a": "B", "point_b": "D0", "lo": -9, "hi": -9}]})
    assert r.status_code == 409
    payload = r.get_json()
    assert payload["contradictions"]
    assert len(client.get("/api/sheets").get_json()["sheets"]) == 2
    app.config["STORE"].close()
    print("test_flask_contradiction_returns_409_and_keeps_state: 通过")


def test_sample_endpoint_guards_nonempty_db():
    app = create_app(db_path=_tmp_db())
    client = app.test_client()
    r = client.post("/api/sample")
    assert r.status_code == 201
    n = len(client.get("/api/sheets").get_json()["sheets"])
    assert n == 9
    r2 = client.post("/api/sample")
    assert r2.status_code == 409  # 非空库拒绝覆盖
    app.config["STORE"].close()
    print("test_sample_endpoint_guards_nonempty_db: 通过")


def test_read_during_write_uses_old_snapshot():
    """写事务已写新单但未提交（卡在核算中）时，另一线程仍读到旧快照。"""
    import threading

    import app.storage as storage_mod

    db = _tmp_db()
    store = Store(db)
    store.submit_batch([Sheet("R1", "D0", "Z", 10, 14)])

    entered = threading.Event()
    release = threading.Event()
    original_compute = storage_mod.compute

    def blocking_compute(sheets, datum, span_limit):
        entered.set()
        release.wait(timeout=5)
        return original_compute(sheets, datum=datum, span_limit=span_limit)

    storage_mod.compute = blocking_compute
    errors = []

    def writer():
        try:
            store.submit_batch([Sheet("R2", "D0", "Q", 0, 4)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert entered.wait(timeout=5)
        # 核算（写事务）进行中：读路径必须返回最后提交的旧数据且不阻塞
        snap = store.latest_snapshot()
        assert snap["sheet_count"] == 1
        assert [s.sid for s in store.list_sheets()] == ["R1"]
    finally:
        storage_mod.compute = original_compute
        release.set()
        t.join()
    assert not errors
    # 提交后新数据生效
    assert sorted(s.sid for s in store.list_sheets()) == ["R1", "R2"]
    assert store.latest_snapshot()["sheet_count"] == 2
    print("test_read_during_write_uses_old_snapshot: 通过")


if __name__ == "__main__":
    test_store_empty_init_snapshot()
    test_add_replace_and_persist()
    test_duplicate_sid_rejected_without_replace()
    test_replace_missing_id_rejected()
    test_contradiction_keeps_current_data()
    test_replace_contradiction_keeps_current_data()
    test_flask_routes_happy_path()
    test_flask_contradiction_returns_409_and_keeps_state()
    test_sample_endpoint_guards_nonempty_db()
    test_read_during_write_uses_old_snapshot()
    print("\n全部存储/Web 测试通过。")
