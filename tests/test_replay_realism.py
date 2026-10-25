# -*- coding: utf-8 -*-
"""打表回放真人化（时间轴 + 步频）的回归测试。

背景：tasks_fch 的旧抓包表里 runStep 按固定周期累加（首末差值折算 357~642 spm），
runTime 存在 Δt=0 与 60+ m/s 的区间。这两类字段互相矛盾，是最容易被服务端规则
命中的形态。main._normalize_replay_timeline / _normalize_replay_steps 在**不改坐标**
的前提下把它们修到真人范围内。

本文件钉住四条不变量：
  1. 正常表原样返回（幂等）；
  2. 修复后每段速度都落在 [0.8, 8] m/s（零位移段除外），且步频落在 [110, 220]；
  3. 总里程不变（尖峰平滑只在段间搬运）；
  4. 非法输入（里程回退等）仍然原样报错，不会被"修正"掩盖。
"""
import copy
import json

import pytest

import main as M


CFG = "tests/fixtures/test_config.ini"


@pytest.fixture()
def yun():
    M.set_args(CFG)
    y = M.Yun_For_New.__new__(M.Yun_For_New)
    y.task_map = {"data": {"recodeCadence": 150}}
    return y


def make_points(dms, dts, steps=None):
    """按段增量造点列：dms/dts 为每段的米数与秒数。"""
    pts = [{"point": "117.200000,31.770000", "runMileage": 0.0, "runTime": 0,
            "runStep": 0, "speed": "5.50"}]
    m, t = 0.0, 0
    for dm, dt in zip(dms, dts):
        m += dm
        t += dt
        pts.append({"point": "117.200000,31.770000", "runMileage": m,
                    "runTime": t, "runStep": 0, "speed": "5.50"})
    return pts


def speeds(pts):
    out = []
    for a, b in zip(pts, pts[1:]):
        dm = b["runMileage"] - a["runMileage"]
        dt = b["runTime"] - a["runTime"]
        if dm > 0 and dt > 0:
            out.append(dm / dt)
    return out


# ------------------------------------------------------------------ 幂等
def test_clean_table_is_untouched(yun):
    pts = make_points([5.0] * 100, [1] * 100)
    for i, p in enumerate(pts):
        p["runStep"] = i * 3          # 180 spm @ 1s 采样
    before = copy.deepcopy(pts)
    yun._normalize_replay_timeline(pts)
    yun._normalize_replay_steps(pts)
    assert pts == before


# ------------------------------------------------------------------ 时间轴
def test_zero_dt_intervals_are_rebuilt(yun):
    dms = [5.0] * 50
    dts = [1] * 50
    dts[10] = 0                       # 1 处 Δt=0 但有位移
    pts = make_points(dms, dts)
    total_before = pts[-1]["runMileage"]
    yun._normalize_replay_timeline(pts)
    assert all(dt >= 1 for dt in
               (b["runTime"] - a["runTime"] for a, b in zip(pts, pts[1:])))
    assert all(0.8 <= v <= 8.0 for v in speeds(pts))
    assert pts[-1]["runMileage"] == pytest.approx(total_before)


def test_gps_spike_is_smoothed_without_changing_distance(yun):
    dms = [5.0] * 60
    dms[30] = 83.0                    # 单点 83m 瞬移
    pts = make_points(dms, [1] * 60)
    total_before = pts[-1]["runMileage"]
    yun._normalize_replay_timeline(pts)
    assert pts[-1]["runMileage"] == pytest.approx(total_before)
    assert max(speeds(pts)) <= 8.0


def test_timeline_keeps_total_duration_when_feasible(yun):
    dms = [5.0] * 100
    dts = [1] * 100
    dts[20] = 1
    dts[21] = 30                      # 一段 0.17 m/s：明显越界，但重建后时长可还原
    pts = make_points(dms, dts)
    duration_before = pts[-1]["runTime"] - pts[0]["runTime"]
    yun._normalize_replay_timeline(pts)
    # 有位移的段数 <= 原总秒数时，总时长应完全保持不变
    assert pts[-1]["runTime"] - pts[0]["runTime"] == duration_before
    assert all(0.8 <= v <= 8.0 for v in speeds(pts))


# ------------------------------------------------------------------ 步频
def test_absurd_cadence_is_redistributed(yun):
    # 1 秒采样、每点 +5 步 → 300 spm，属于旧抓包表的典型形态
    pts = make_points([5.0] * 600, [1] * 600)
    for i, p in enumerate(pts):
        p["runStep"] = i * 5
    yun._normalize_replay_steps(pts)
    steps = [p["runStep"] for p in pts]
    duration = pts[-1]["runTime"] - pts[0]["runTime"]
    spm = (steps[-1] - steps[0]) * 60 / duration
    assert M.REPLAY_SPM_SANE[0] <= spm <= M.REPLAY_SPM_SANE[1]
    assert steps == sorted(steps)                       # 单调不回退


def test_cadence_derived_from_pace_when_table_value_is_absurd(yun):
    yun.task_map = {"data": {"recodeCadence": 288}}      # 旧表自报值同样不可信
    pts = make_points([8.0] * 300, [1] * 300)            # 8 m/s ≈ 2:05/km
    for i, p in enumerate(pts):
        p["runStep"] = i * 8
    yun._normalize_replay_steps(pts)
    steps = [p["runStep"] for p in pts]
    spm = (steps[-1] - steps[0]) * 60 / (pts[-1]["runTime"] - pts[0]["runTime"])
    assert M.REPLAY_SPM_TARGET[0] <= spm <= M.REPLAY_SPM_TARGET[1]


# ------------------------------------------------------------------ 不掩盖非法输入
def test_invalid_input_still_rejected_before_normalization(tmp_path):
    """校验在归一化之前：里程回退必须原样报错。"""
    task = {"code": 200, "msg": "ok", "data": {
        "duration": 600, "recordMileage": 1.0, "recodeCadence": 150,
        "recodePace": "5.50", "recodeDislikes": 0, "manageList": [],
        "pointsList": [{"point": "117.2,31.7", "runMileage": 100.0,
                        "runTime": 10, "runStep": 10, "speed": "5.50"},
                       {"point": "117.2,31.7", "runMileage": 50.0,
                        "runTime": 20, "runStep": 20, "speed": "5.50"}]}}
    d = tmp_path / "tasks"
    d.mkdir()
    (d / "one.json").write_text(json.dumps(task), encoding="utf-8")
    M.set_args(CFG)
    yun = M.Yun_For_New.__new__(M.Yun_For_New)
    yun.strides = 0.8
    yun.distance_cap_m = 10000.0
    with pytest.raises(ValueError, match="回退或非法"):
        yun.do_by_points_map(path=str(d), random_choose=True)


# ------------------------------------------------------- 上传契约：平均步频 40~300
def _split_body(yun, pts):
    return M._build_split_body(42, "U1", "100", pts, 0.8)


def test_batch_cadence_clamped_high(yun):
    """整包步频 600 spm（每点 +10 步 / 1 秒）必须被夹回 300 以内。"""
    pts = make_points([5.0] * 20, [1] * 20)
    for i, p in enumerate(pts):
        p["runStep"] = i * 10
    pts = yun._clamp_batch_steps(pts)
    body = _split_body(yun, pts)
    assert M.SERVER_SPM_BOUNDS[0] <= body["runSteps"] <= M.SERVER_SPM_BOUNDS[1]
    steps = [p["runStep"] for p in body["cardPointList"]]
    assert steps == sorted(steps)


def test_batch_cadence_clamped_low(yun):
    """整包只有 5 步 / 60 秒（5 spm）必须被抬到 40 以上。"""
    pts = make_points([5.0] * 20, [3] * 20)
    for i, p in enumerate(pts):
        p["runStep"] = i // 4          # 每 4 点 1 步
    pts = yun._clamp_batch_steps(pts)
    body = _split_body(yun, pts)
    assert M.SERVER_SPM_BOUNDS[0] <= body["runSteps"] <= M.SERVER_SPM_BOUNDS[1]


def test_batch_in_range_is_untouched(yun):
    pts = make_points([5.0] * 20, [1] * 20)
    for i, p in enumerate(pts):
        p["runStep"] = i * 3           # 180 spm
    before = copy.deepcopy(pts)
    assert yun._clamp_batch_steps(pts) == before


def test_finish_cadence_clamped_and_consistent(yun):
    """finish 的 recodeCadence 与末点步数必须同源、且落在 40~300。"""
    pts = make_points([5.0] * 20, [1] * 20)
    for i, p in enumerate(pts):
        p["runStep"] = i * 10          # 600 spm
    yun.task_map = {"data": {"recodeCadence": 600, "pointsList": pts}}
    yun._update_task_summary(pts, pts[-1]["runTime"])
    data = yun.task_map["data"]
    assert M.SERVER_SPM_BOUNDS[0] <= data["recodeCadence"] <= M.SERVER_SPM_BOUNDS[1]
    derived = (pts[-1]["runStep"] - pts[0]["runStep"]) * 60 / data["duration"]
    assert derived == pytest.approx(data["recodeCadence"], abs=0.5)


# ------------------------------------------------- 批边界：不得产生单点批次
def test_effective_point_count_uses_mileage_cap(yun):
    pts = make_points([100.0] * 30, [10] * 30)   # 31 个点，里程 0..3000
    yun.distance_cap_m = 1500.0
    assert yun._effective_point_count(pts) == 16  # 索引 15 的点里程 1500，首次达到上限
    yun.distance_cap_m = 0
    assert yun._effective_point_count(pts) == len(pts) == 31


def test_single_point_tail_is_avoided_by_shifting_boundary(yun):
    """总点数 ≡ 1 (mod 批大小) 时，若仍按满批发送就会留下 1 点尾批。

    单点包没有时长/位移，runSteps 只能是 0，无法满足服务端 40~300。
    批边界应提前一个点，让最后两批都 >= 2 点。
    """
    yun.upload_batch_size = 10
    pts = make_points([5.0] * 20, [1] * 20)      # 21 点 = 10 + 10 + 1
    sent, held = [], []
    emitted = []
    points = []
    for pt in pts:
        points.append(pt)
        emitted.append(pt)
        if len(points) == yun.upload_batch_size:
            to_send, points = yun._partition_batch(points, emitted, len(pts))
            if to_send:
                sent.append(len(to_send))
    sent.append(len(points))
    # 不是 [10, 10, 1]；21 点时第一批满 10 会剩 11 -> 正常发；
    # 第二批满 10 时只剩 1，故只发 9，尾批 2。
    assert sent == [10, 9, 2], sent
    assert all(n >= 2 for n in sent)


def test_partition_holds_whole_batch_when_too_small(yun):
    """批大小 2、总点数 3：满批时只剩 1，整批不发，尾批 3 点。"""
    yun.upload_batch_size = 2
    pts = make_points([5.0] * 2, [1] * 2)        # 3 点
    emitted, points, sent = [], [], []
    for pt in pts:
        points.append(pt)
        emitted.append(pt)
        if len(points) == yun.upload_batch_size:
            to_send, points = yun._partition_batch(points, emitted, len(pts))
            if to_send:
                sent.append(len(to_send))
    sent.append(len(points))
    assert all(n >= 2 for n in sent), sent


def test_partition_normal_case_sends_full_batch(yun):
    pts = make_points([5.0] * 30, [1] * 30)      # 31 点
    emitted = pts[:10]
    to_send, rest = yun._partition_batch(list(emitted), emitted, len(pts))
    assert len(to_send) == 10 and rest == []


# ------------------------------------------- 打表任务在 start 之前按学校参数校验
def _replay_yun(**over):
    M.set_args(CFG)
    y = M.Yun_For_New.__new__(M.Yun_For_New)
    y.raSingleMileageMin = over.get("min_km", 1.0)
    y.raSingleMileageMax = over.get("max_km", 2.0)
    y.server_min_mileage_km = over.get("server_min", 1.0)
    y.distance_cap_m = over.get("max_km", 2.0) * 1000
    y.raCadenceMin = over.get("cad_min", 40)
    y.raCadenceMax = over.get("cad_max", 300)
    y.raDislikes = over.get("dislikes", 0)
    y.task_map = {"data": {"manageList": over.get("manage", [])}}
    return y


def _points_1_1km():
    pts = make_points([100.0] * 11, [30] * 11)   # 12 点，1100 m
    for i, p in enumerate(pts):
        p["runStep"] = i * 75                     # 150 spm @ 30s
    return pts


def test_replay_validation_passes_for_matching_task():
    y = _replay_yun()
    y._validate_replay_task(_points_1_1km())      # 不抛异常即通过


def test_replay_validation_rejects_short_mileage():
    y = _replay_yun(min_km=6.0, max_km=7.0, server_min=6.0)
    with pytest.raises(ValueError, match="里程"):
        y._validate_replay_task(_points_1_1km())


def test_replay_validation_truncates_at_cap_instead_of_rejecting():
    """超过上限不是错误：实际会在上限处截断（与运行时 _cap_point 一致）。"""
    y = _replay_yun(min_km=0.5, max_km=1.0)
    y._validate_replay_task(_points_1_1km())      # 1.1km 表按 1.0km 上限截断后通过
    assert y._effective_point_count(_points_1_1km()) == 11   # 第 11 点里程=1000


def test_replay_validation_rejects_mileage_below_cap_when_truncation_too_short():
    """上限很低时截断后里程可能低于下限，这种情况必须拒绝。"""
    y = _replay_yun(min_km=0.95, max_km=1.0)
    pts = _points_1_1km()
    pts = pts[:11]                                # 只到 1000 m
    y._validate_replay_task(pts)                  # 恰好 1.0 km，通过
    y.raSingleMileageMin = 1.05
    with pytest.raises(ValueError, match="里程"):
        y._validate_replay_task(pts)


def test_replay_validation_rejects_cadence_out_of_range():
    y = _replay_yun(cad_min=200, cad_max=300)     # 表是 150 spm
    with pytest.raises(ValueError, match="步频"):
        y._validate_replay_task(_points_1_1km())


def test_replay_validation_requires_card_points():
    y = _replay_yun(dislikes=3, manage=[])        # 任务要 3 个踩点，表里没有
    with pytest.raises(ValueError, match="踩点"):
        y._validate_replay_task(_points_1_1km())


def test_replay_validation_rejects_insufficient_marked_cards():
    manage = [{"point": "1,2", "marked": "Y", "index": 0},
              {"point": "3,4", "marked": "N", "index": 1}]
    y = _replay_yun(dislikes=2, manage=manage)    # 只有 1 个 Y
    with pytest.raises(ValueError, match="踩点"):
        y._validate_replay_task(_points_1_1km())


# ------------------------------------------------------------- 本地里程硬上限
def _cap_yun(local_km, server_max_km=3.0):
    M.set_args(CFG)
    M.max_mileage_km = local_km
    home = {"code": 200, "data": {"cralist": [{
        "id": 1, "schoolId": "100", "raType": "T1", "raRunArea": "A",
        "raDislikes": 0, "raSingleMileageMin": 1.0,
        "raSingleMileageMax": server_max_km,
        "raCadenceMin": 40, "raCadenceMax": 300, "runFaceStatus": "N",
        "points": "", "passPointNum": 10}]}}
    import yun_http as yh
    fake = yh.FakeTransport(lambda r, e: home if r.endswith("getHomeRunInfo") else
                            ({"code": 200, "data": {"id": 1, "canSport": "Y",
                                                    "recordStartTime": "x",
                                                    "studentId": "S"}}
                             if r.endswith("/run/start") else
                             {"code": 200, "data": {"isStandard": "Y", "isCheat": "N"}}
                             if r.endswith("/run/isStandard") else
                             {"code": 200, "msg": "ok", "data": None}),
                            sm2box=yh.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY))
    client = yh.YunClient(M.build_profile(M._CONF), base_url=M.my_host,
                          transport=fake, sleep=lambda s: None)
    return M.Yun_For_New(client=client, home_info=home)


def test_local_cap_overrides_larger_server_cap():
    yun = _cap_yun(2.0, server_max_km=5.0)
    assert yun.distance_cap_m == pytest.approx(2000.0)
    assert yun.server_cap_m == pytest.approx(5000.0)
    assert yun.local_cap_m == pytest.approx(2000.0)


def test_server_cap_wins_when_smaller():
    yun = _cap_yun(2.0, server_max_km=1.5)
    assert yun.distance_cap_m == pytest.approx(1500.0)   # 取较小值


def test_zero_local_cap_means_unlimited():
    yun = _cap_yun(0.0, server_max_km=3.0)
    assert yun.distance_cap_m == pytest.approx(3000.0)
    assert yun.local_cap_m == 0.0


def test_replay_stops_at_cap_and_reports_that_mileage():
    """4.8km 的表在 2km 上限处截断：末点里程 = 2000，之后进结束链。"""
    yun = _cap_yun(2.0, server_max_km=5.0)
    yun.start()
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    task = {"code": 200, "data": {
        "duration": 1200, "recordMileage": 4.8, "recodeCadence": 150,
        "recodePace": "5.50", "recodeDislikes": 0, "manageList": [],
        "pointsList": [{"point": "117.2,31.7", "speed": "5.50",
                        "runMileage": i * 200.0, "runTime": i * 40,
                        "runStep": i * 100} for i in range(25)]}}
    import json as _json
    with open(_os.path.join(d, "tasklist_0.json"), "w", encoding="utf-8") as f:
        _json.dump(task, f)
    yun.do_by_points_map(path=d, random_choose=True)
    yun.finish_by_points_map()
    sent = yun.task_map["data"]
    assert float(sent["recordMileage"]) == pytest.approx(2.0, abs=1e-6)
    assert sent["pointsList"][-1]["runMileage"] == pytest.approx(2000.0)
    M.max_mileage_km = 0.0
