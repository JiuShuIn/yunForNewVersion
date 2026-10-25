# -*- coding: utf-8 -*-
"""panel.py（本地图形工作台）的离线测试。

面板的核心承诺是"预检不联网、上传只认已确认的计划"，本文件钉住这几条：
  1. 预检全程走 FakeTransport，不会创建真实网络连接；
  2. 预检返回的每一步都带可展示的请求体，且批次数量与实际上传一致；
  3. 未预检 / 预检失败时拒绝上传；
  4. 上传进度的 sent 计数与实际 split 包数一致。
"""
import json
import os
import time

import pytest

import main as M
import panel
from yun_http import FakeTransport, SM2Box, YunClient

CFG = os.path.join("tests", "fixtures", "test_config.ini")


@pytest.fixture(autouse=True)
def _use_test_config(monkeypatch):
    """面板默认读项目根 config.ini（含真实凭据），测试一律指向夹具配置。"""
    monkeypatch.setattr(panel, "CONFIG_PATH", CFG)
    # 多用户配置：测试用独立的 configs 目录，并把夹具复制成 user.ini
    import shutil as _sh
    cdir = tmp_path_factory_dir = os.path.join(
        os.path.dirname(panel.BASE_DIR), "work_dir", "panel_cfg_test")
    _sh.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir, exist_ok=True)
    _sh.copyfile(CFG, os.path.join(cdir, "user.ini"))
    monkeypatch.setattr(panel, "CONFIG_DIR", cdir)
    monkeypatch.setattr(panel, "PICK_DIR",
                        os.path.join(os.path.dirname(panel.BASE_DIR), "work_dir", "panel_pick_test"))
    panel.STATE["plan"] = None
    panel.STATE["log"] = []
    panel.STATE["progress"] = {"phase": "idle", "sent": 0, "total": 0, "detail": ""}
    yield
    panel.STATE["plan"] = None
    _sh.rmtree(cdir, ignore_errors=True)


# ------------------------------------------------------------- 预检不联网
def test_preview_never_touches_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("预检阶段不得发起真实网络请求")

    import yun_http
    monkeypatch.setattr(yun_http, "_real_transport", boom)
    out = panel.build_preview("tasks_fch", "tasklist_0.json")
    assert "error" not in out, out.get("error")
    assert out["steps"]


def test_preview_uses_capture_transport():
    out = panel.build_preview("tasks_fch", "tasklist_0.json")
    assert "error" not in out, out.get("error")
    kinds = [s["kind"] for s in out["steps"]]
    assert kinds[0] == "start"
    assert kinds[-1] == "finish"
    assert "standard" in kinds
    assert kinds.count("split") == out["summary"]["batches"]


def test_preview_every_split_has_body_and_metrics():
    out = panel.build_preview("tasks_fch", "tasklist_0.json")
    assert "error" not in out, out.get("error")
    splits = [s for s in out["steps"] if s["kind"] == "split"]
    assert splits
    for s in splits:
        assert s["points"] >= 2                 # 不会出现单点包
        assert isinstance(s["runSteps"], float)
        assert 40 <= s["runSteps"] <= 300       # 服务端步频硬边界


def test_preview_reports_cadence_compliance():
    out = panel.build_preview("tasks_fch", "tasklist_3.json")
    assert "error" not in out, out.get("error")
    s = out["summary"]
    assert s["spm_ok"] is True
    assert s["spm_min"] >= 40 and s["spm_max"] <= 300
    assert int(s["dislikes"]) > 0


def test_preview_carries_track_and_cards():
    out = panel.build_preview("tasks_fch", "tasklist_0.json")
    assert len(out["track"]) > 100
    assert out["cards"]
    assert all("marked" in c for c in out["cards"])


def test_preview_missing_task_errors():
    out = panel.build_preview("tasks_fch", "no_such_task.json")
    assert "error" in out


# ------------------------------------------------------------- 上传守卫
def test_upload_requires_matching_plan():
    panel.STATE["plan"] = None
    r = panel.start_upload("tasks_fch", "tasklist_0.json")
    assert "error" in r

    panel.STATE["plan"] = {"campus": "tasks_fch", "task": "tasklist_1.json",
                           "summary": {}}
    r = panel.start_upload("tasks_fch", "tasklist_0.json")   # 表不匹配
    assert "error" in r


# ------------------------------------------------------------- 上传进度
def _fake_client_factory(split_counter):
    home = {"code": 200, "data": {"cralist": [{
        "id": 1, "schoolId": "100", "raType": "T1", "raRunArea": "A",
        "raDislikes": 2, "raSingleMileageMin": 2.0, "raSingleMileageMax": 3.0,
        "raCadenceMin": 40, "raCadenceMax": 300, "runFaceStatus": "N",
        "points": "117.2,31.7|117.3,31.8", "passPointNum": 10}]}}

    def responder(router, env):
        if router.endswith("/run/getHomeRunInfo"):
            return home
        if router.endswith("/run/start"):
            return {"code": 200, "data": {"recordStartTime": "x", "id": 777,
                                          "studentId": "S1", "faceTime": 0,
                                          "canSport": "Y"}}
        if router.endswith("/run/isStandard"):
            return {"code": 200, "data": {"isStandard": "Y", "isCheat": "N"}}
        if router.endswith("/run/splitPointCheating"):
            split_counter[0] += 1
        return {"code": 200, "msg": "ok", "data": None}

    def factory():
        M.set_args(CFG)
        p = M.build_profile(M._CONF)
        fake = FakeTransport(responder, sm2box=SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY),
                             fallback_key_b64=p.cipherkey or None)
        return YunClient(p, base_url=M.my_host, transport=fake, sleep=lambda s: None)

    return factory


def test_upload_reports_progress(monkeypatch):
    counter = [0]
    monkeypatch.setattr(panel.M, "default_client", _fake_client_factory(counter))
    panel.STATE["plan"] = {"campus": "tasks_fch", "task": "tasklist_0.json",
                           "summary": {"batches": 51}}
    r = panel.start_upload("tasks_fch", "tasklist_0.json")
    assert r.get("ok"), r
    for _ in range(300):
        if panel.STATE["progress"]["phase"] in ("done", "error"):
            break
        time.sleep(0.05)
    pr = panel.STATE["progress"]
    assert pr["phase"] == "done", "".join(panel.STATE["log"])[-800:]
    assert pr["batches_sent"] == counter[0] > 0     # 批次数
    assert pr["sent"] > 0 and pr["sent"] <= pr["total"]   # 进度按点数
    assert "recordId=777" in "".join(panel.STATE["log"])


# ------------------------------------------------------------- 配置读写
def test_config_roundtrip_keeps_secrets(tmp_path, monkeypatch):
    cfg = tmp_path / "c.ini"
    cfg.write_text(
        "[Yun]\nschool_host = http://x.invalid\n"
        "[User]\ntoken = SECRET\n[Run]\nsplit_count = 10\n", encoding="utf-8")
    monkeypatch.setattr(panel, "CONFIG_PATH", str(cfg))

    got = panel.read_config()
    assert got["User"]["token"] == panel.MASK        # 敏感值不回传明文

    changed = panel.save_config({"Yun": {"school_host": "http://y.invalid"},
                                 "User": {"token": panel.KEEP},
                                 "Run": {"split_count": "10"}})
    assert changed == ["Yun.school_host"]            # 只记真正变化的键
    after = panel.read_config()
    assert after["User"]["token"] == panel.MASK      # 掩码没有落盘
    assert "SECRET" in cfg.read_text(encoding="utf-8")


# ------------------------------------------------------------- 校区可选范围
def test_all_campuses_are_listed():
    """不再强制过滤：所有 tasks_* 目录都应可选，由界面提示"未验证"。"""
    camps = list(panel.list_campuses())
    assert "tasks_fch" in camps
    assert panel.SUPPORTED_CAMPUS == "tasks_fch"     # 仅作"已验证"标记
    assert len(camps) >= 1


def test_preview_accepts_other_campus(tmp_path):
    """其他校区允许预检（只是未验证），不再被后端拒绝。"""
    out = panel.build_preview("tasks_txl", "tasklist_0.json")
    assert "只支持" not in (out.get("error") or "")


def test_upload_accepts_other_campus_after_preview():
    """其他校区同样走"预检 → 确认"流程，不再被硬性拒绝。"""
    panel.STATE["plan"] = {"campus": "tasks_txl", "task": "tasklist_0.json",
                           "summary": {}}
    r = panel.start_upload("tasks_txl", "tasklist_0.json")
    assert "只支持" not in (r.get("error") or "")
    panel.STATE["plan"] = None


def test_map_uses_gcj02_base_map():
    """底图必须与轨迹同坐标系（GCJ-02）；换成 OSM/WGS-84 会让轨迹偏移约 500m。"""
    with open(os.path.join(panel.BASE_DIR, "panel_ui.html"), encoding="utf-8") as f:
        html = f.read()
    assert "autonavi.com" in html          # 高德瓦片 = GCJ-02
    assert "openstreetmap" not in html     # 不得混用 WGS-84 底图


def test_meta_reports_local_mileage_cap():
    """前端要靠这个值显示"到 2km 自动结束"，不能读到 set_args 之前的残留 0。"""
    assert panel._cap_km() > 0            # 仓库 config.ini 配置了 2.0


def test_preview_respects_local_cap():
    """3.78km 的表在 2km 上限下应上报 2.00km，且批次数少于整表。"""
    out = panel.build_preview("tasks_fch", "tasklist_1.json")
    assert "error" not in out, out.get("error")
    s = out["summary"]
    assert float(s["km"]) == pytest.approx(2.0, abs=1e-6)
    assert s["cap_m"] == pytest.approx(2000.0)
    assert s["task_table_km"] > 2.0        # 原表更长，确实发生了截断
    last = [x for x in out["steps"] if x["kind"] == "split"][-1]
    assert last["body"]["cardPointList"][-1]["runMileage"] == pytest.approx(2000.0)


# --------------------------------------------------- 学校要求对照（预检右侧）
def _school_cfg(tmp_path, monkeypatch, min_dislikes=3):
    """写一份带学校要求的用户配置，供 build_preview（内部会 set_args）读取。"""
    src = open(CFG, encoding="utf-8").read()
    src = src.replace("min_dislikes = 0", "min_dislikes = %d" % min_dislikes)
    p = os.path.join(panel.CONFIG_DIR, "user.ini")
    with open(p, "w", encoding="utf-8") as f:
        f.write(src)


def test_requirements_include_school_min_dislikes(tmp_path, monkeypatch):
    """学校要求至少 3 个踩点：tasklist_0 只有 2 个 Y，必须被判不满足。"""
    _school_cfg(tmp_path, monkeypatch)
    out = panel.build_preview("tasks_fch", "tasklist_0.json")
    reqs = (out.get("summary") or {}).get("requirements") or []
    by = {r["key"]: r for r in reqs}
    assert "dislikes" in by
    assert by["dislikes"]["ok"] is False
    assert "3" in by["dislikes"]["required"]


def test_requirements_pass_for_enough_cards():
    out = panel.build_preview("tasks_fch", "tasklist_3.json")
    assert "error" not in out, out.get("error")
    by = {r["key"]: r for r in out["summary"]["requirements"]}
    assert by["dislikes"]["ok"] is True
    assert by["mileage"]["ok"] is True
    assert by["cadence"]["ok"] is True


def test_requirements_shown_even_when_validation_fails(tmp_path, monkeypatch):
    """踩点不足会在 start 前失败、没有任何请求；对照表仍要给出差距。"""
    _school_cfg(tmp_path, monkeypatch)
    out = panel.build_preview("tasks_fch", "tasklist_0.json")
    assert "error" in out
    reqs = out["summary"]["requirements"]
    by = {r["key"]: r for r in reqs}
    assert by["dislikes"]["ok"] is False
    assert "2 / 5" in by["dislikes"]["actual"]
    assert out["warnings"] and "踩点" in out["warnings"][0]


def test_min_dislikes_from_config_is_a_floor():
    """学校要求与任务下发值取大。"""
    import main as M
    M.set_args(CFG)
    old = M.min_dislikes
    try:
        M.min_dislikes = 5
        home = {"code": 200, "data": {"cralist": [{
            "id": 1, "schoolId": "100", "raType": "T1", "raRunArea": "A",
            "raDislikes": 2, "raSingleMileageMin": 1.0, "raSingleMileageMax": 3.0,
            "raCadenceMin": 40, "raCadenceMax": 300, "runFaceStatus": "N",
            "points": "", "passPointNum": 10}]}}
        import yun_http as yh
        fake = yh.FakeTransport(lambda r, e: home, sm2box=yh.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY))
        client = yh.YunClient(M.build_profile(M._CONF), base_url=M.my_host,
                              transport=fake, sleep=lambda s: None)
        yun = M.Yun_For_New(client=client, home_info=home)
        assert yun.server_dislikes == 2
        assert yun.raDislikes == 5          # 学校要求更大
    finally:
        M.min_dislikes = old


def test_progress_counts_points_not_batches(monkeypatch):
    """进度分母是"实际会上报的点数"，不是预检估算的批次数。

    真实批量由服务端 passPointNum 决定，可能与预检假设不同；
    旧实现用批次数做分母，会出现 62/58 这种"超过 100%"的假象。
    """
    counter = [0]
    monkeypatch.setattr(panel.M, "default_client", _fake_client_factory(counter))
    panel.STATE["plan"] = {"campus": "tasks_fch", "task": "tasklist_3.json",
                           "summary": {"batches": 1}}   # 故意给个错的批次估计
    r = panel.start_upload("tasks_fch", "tasklist_3.json")
    assert r.get("ok"), r
    for _ in range(400):
        if panel.STATE["progress"]["phase"] in ("done", "error"):
            break
        time.sleep(0.05)
    pr = panel.STATE["progress"]
    assert pr["phase"] == "done", "".join(panel.STATE["log"])[-600:]
    assert pr["total"] > 0
    assert pr["sent"] <= pr["total"]              # 不会超过 100%
    assert pr["sent"] == pr["total"]              # 跑完后正好用满
    assert pr["batches_sent"] == counter[0]


# ------------------------------------------------------------- 历史记录
def test_history_rejects_invalid_table_name():
    """表名必须匹配白名单，防止路径穿越/注入到服务端参数里。"""
    for bad in ("../etc/passwd", "a b", "t;drop", ""):
        assert "error" in panel.history_runs(bad)
        assert "error" in panel.history_fetch("123", bad)


def test_history_fetch_requires_id_and_table():
    assert "error" in panel.history_fetch("", "")
    assert "error" in panel.history_fetch("123", "")


def test_history_save_appends_numbered_files(tmp_path):
    """保存沿用 history.save_history_record：编号续接、不覆盖。"""
    import history
    payload = json.dumps({"code": 200, "data": {"recordMileage": 2.0,
                                               "pointsList": []}})
    p0 = history.save_history_record(payload, str(tmp_path))
    p1 = history.save_history_record(payload, str(tmp_path))
    assert os.path.basename(p0) == "tasklist_0.json"
    assert os.path.basename(p1) == "tasklist_1.json"
    assert json.load(open(p0, encoding="utf-8"))["code"] == 200


def test_history_endpoints_registered():
    """三个历史记录端点都要在 Handler 里可路由。"""
    import inspect
    src = inspect.getsource(panel.Handler)
    for path in ("/api/history/terms", "/api/history/runs", "/api/history/fetch"):
        assert path in src


# ------------------------------------------------------------- 多用户配置
def test_default_user_is_created_from_template():
    assert "user" in panel.list_users()
    assert os.path.exists(os.path.join(panel.CONFIG_DIR, "user.ini"))


def test_create_and_delete_user():
    r = panel.create_user("alice")
    assert r.get("ok"), r
    assert "alice" in panel.list_users()
    assert os.path.exists(os.path.join(panel.CONFIG_DIR, "alice.ini"))

    # 新用户身份字段必须清空，避免误用别人的 token
    cfg = panel.read_config("alice")
    assert cfg["User"]["token"] in ("", panel.MASK)

    assert panel.delete_user("alice").get("ok")
    assert "alice" not in panel.list_users()


def test_default_user_cannot_be_deleted():
    assert "error" in panel.delete_user("user")


def test_user_name_rejects_path_traversal():
    for bad in ("../etc/passwd", "a/b", "", "x" * 40):
        assert "error" in panel.create_user(bad)


def test_users_are_independent():
    panel.create_user("bob")
    try:
        panel.save_config({"Run": {"max_mileage_km": "5.0"}}, "bob")
        assert panel.read_config("bob")["Run"]["max_mileage_km"] == "5.0"
        # user 不受影响
        assert panel.read_config("user")["Run"]["max_mileage_km"] != "5.0"
    finally:
        panel.delete_user("bob")


def test_sensitive_values_masked_per_user():
    panel.create_user("carol")
    try:
        panel.save_config({"User": {"token": "SECRET123"}}, "carol")
        assert panel.read_config("carol")["User"]["token"] == panel.MASK
        # 落盘的是明文
        raw = open(os.path.join(panel.CONFIG_DIR, "carol.ini"), encoding="utf-8").read()
        assert "SECRET123" in raw
    finally:
        panel.delete_user("carol")


def test_history_task_tables_are_shared_across_users():
    """历史记录任务表不绑定用户：所有用户共用 tasks_else。"""
    import inspect
    src = inspect.getsource(panel.history_fetch)
    assert "tasks_else" in src
    # 保存路径不包含用户名
    assert "user" not in src.split("target_dir")[1].split("save_history_record")[0]
