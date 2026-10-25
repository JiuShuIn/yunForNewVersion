# -*- coding: utf-8 -*-
"""云运动 · 本地图形工作台（GUI）。

替代 web.py 的单页版本，围绕"预检 → 用户确认 → 上传"三段式设计：

  1. 配置   config.ini 的查看/修改（敏感字段掩码，未改动不落盘）
  2. 选路   选择校区/任务表（打表模式），地图上叠加显示轨迹与踩点
  3. 预检   用**假传输**把整条链路（getHomeRunInfo -> start -> 每个 split 包
           -> isStandard -> 尾批 -> finish）完整跑一遍，把**每个将要发出的
           请求体**摊开给用户看：点数、里程、步频、踩点、耗时、校验结论
  4. 上传   用户确认后才创建真实客户端；进度与最终结果实时回传

关键安全约束：
  - 预检阶段绝不创建真实网络连接（transport 注入 FakeTransport）；
  - 真实上传只接受"预检通过且用户勾选确认"的计划，参数由服务端进程持有，
    前端无法凭任意参数调起 main.py；
  - 只监听 127.0.0.1。

适用范围：**仅针对合肥工业大学 fch 校区的打表模式**做过完整验证与优化。
合成轨迹、人脸核验、其他校区的任务表请直接使用 `python main.py`。

启动：python panel.py [--port 8765] [--no-browser]
"""
import argparse
import configparser
import json
import os
import random
import shutil
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import main as M
import yun_route  # noqa: F401  （保留：面板与生成器共用同一套几何函数）
from yun_http import FakeTransport, SM2Box, YunClient

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
MASK = "__MASKED__"
KEEP = "__KEEP__"
SENSITIVE_KEYS = {"password", "token", "cipherkey", "cipherkeyencrypted"}
PICK_DIR = os.path.join(BASE_DIR, "work_dir", "panel_pick")

STATE = {
    "plan": None,          # 预检通过的发送计划（真实上传只认它）
    "log": [],
    "lock": threading.Lock(),
    "progress": {"phase": "idle", "sent": 0, "total": 0, "detail": ""},
}


# ------------------------------------------------------------------ 目录扫描
# 本面板只针对该校区做过完整验证（见 SUPPORTED_CAMPUS 的说明）。
SUPPORTED_CAMPUS = "tasks_fch"


def list_campuses():
    """可选的校区目录。

    只暴露 tasks_fch：面板的预检夹具、真人化修正与校验口径都是围绕
    合肥工业大学 fch 打表模式验证的，其他校区的任务表参数（里程/步频/
    踩点区间）没有实测依据，列出来容易让人误以为同样可用。
    其他场景请直接用 `python main.py`。
    """
    out = {}
    for d in sorted(os.listdir(BASE_DIR)):
        if d.startswith("tasks_") and os.path.isdir(os.path.join(BASE_DIR, d)):
            if d != SUPPORTED_CAMPUS:
                continue
            files = sorted(f for f in os.listdir(os.path.join(BASE_DIR, d))
                           if f.endswith(".json"))
            if files:
                out[d] = files
    return out


def _task_points(path):
    """任务表 -> ([[lon,lat],...], 里程米)。runMileage 单位是米（真实表）。"""
    j = json.load(open(path, encoding="utf-8-sig"))
    d = j.get("data") or j
    rows = d.get("pointsList") or []
    pts = []
    for r in rows:
        try:
            lon, lat = str(r.get("point", "")).split(",")
            pts.append([float(lon), float(lat)])
        except (ValueError, AttributeError):
            continue
    km = 0.0
    if rows:
        try:
            km = float(rows[-1].get("runMileage") or 0) / 1000.0
        except (TypeError, ValueError):
            km = 0.0
    return pts, km


def _card_points(path):
    j = json.load(open(path, encoding="utf-8-sig"))
    out = []
    for m in ((j.get("data") or {}).get("manageList") or []):
        try:
            lon, lat = str(m.get("point", "")).split(",")
            out.append({"lon": float(lon), "lat": float(lat),
                        "marked": str(m.get("marked", "")).upper() == "Y"})
        except (ValueError, AttributeError):
            continue
    return out


# ------------------------------------------------------------------ 配置读写
def read_config():
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    out = {}
    for sec in cp.sections():
        out[sec] = {k: (MASK if k.lower() in SENSITIVE_KEYS and cp[sec][k]
                        else cp[sec][k]) for k in cp[sec]}
    return out


def save_config(payload):
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    changed = []
    for sec, kv in (payload or {}).items():
        sec = str(sec).strip()
        if not sec or sec.lower() == "DEFAULT":
            continue
        if not cp.has_section(sec):
            cp.add_section(sec)
        for k, v in (kv or {}).items():
            k = str(k).strip()
            if v in (KEEP, MASK):
                continue
            new = "" if v is None else str(v)
            if cp[sec].get(k) != new:
                changed.append(f"{sec}.{k}")
            cp[sec][k] = new
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)
    return changed


# ------------------------------------------------------------------ 预检引擎
class CaptureTransport(FakeTransport):
    """FakeTransport + 记录每个请求的业务体（用于给用户逐条展示）。

    继承离线假传输：绝无网络，回包由 dry_run_responder 构造。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.trace = []

    def __call__(self, url, data, headers, timeout):
        resp = super().__call__(url, data, headers, timeout)
        last = self.calls[-1]
        self.trace.append({
            "router": last["router"],
            "business": last.get("business"),
            "business_size": len(json.dumps(last.get("business") or {},
                                            ensure_ascii=False)),
        })
        return resp


def _cap_km():
    """本地里程硬上限（公里）；0 表示未配置。

    直接读配置文件而不是 M.max_mileage_km：后者要等 set_args() 之后才有值，
    而 /api/meta 在预检之前就会被前端调用，读到的是上一次的残留（通常是 0）。
    """
    try:
        cp = configparser.ConfigParser()
        cp.read(CONFIG_PATH, encoding="utf-8")
        return float(cp.get("Run", "max_mileage_km", fallback="0") or 0)
    except (configparser.Error, ValueError):
        return 0.0


def _home_fixture(task_path, campus):
    """由所选任务表反推一份 getHomeRunInfo 夹具。

    预检不冒充服务端，只把"脚本将要发出的东西"算出来。任务参数由任务表
    自身的里程/踩点数构造，结果里显式标注 synthetic —— 真实下发值只有
    真跑时才知道。
    """
    j = json.load(open(task_path, encoding="utf-8-sig"))
    d = j.get("data") or {}
    rows = d.get("pointsList") or []
    km = 0.0
    if rows:
        try:
            km = float(rows[-1].get("runMileage") or 0) / 1000.0
        except (TypeError, ValueError):
            km = 0.0
    cards = [str(m.get("point", "")) for m in (d.get("manageList") or [])
             if str(m.get("marked", "")).upper() == "Y"]
    return {
        "code": 200,
        "msg": "panel preview fixture（由任务表反推，非服务端下发）",
        "data": {"cralist": [{
            "id": 900001, "schoolId": "100", "raType": "T1",
            "raRunArea": campus, "raDislikes": len(cards),
            # 学校上限：本地 max_mileage_km（学校规定的一次性里程上限）优先；
            # 未配置时按任务表里程 +0.5 推算。
            # 下限随之锚定在上限上，否则"表里程 3.7 km、上限 2 km"会出现
            # 下限 3.38 > 上限 2.0 的倒挂区间，任何截断都过不了。
            "raSingleMileageMin": (round(max(0.0, _cap_km() - 0.1), 2)
                                   if _cap_km() else max(0.0, round(km - 0.3, 2))),
            "raSingleMileageMax": (round(_cap_km(), 2)
                                   if _cap_km() else round(km + 0.5, 2)),
            "raCadenceMin": 40, "raCadenceMax": 300,
            "runFaceStatus": "N",
            "points": "|".join(cards), "cardRange": 20,
            "passPointNum": 10,
        }]},
        "synthetic": True,
    }


def build_preview(campus, task):
    """跑一次完整的离线流程，返回逐步明细。不发任何真实请求。"""
    if campus != SUPPORTED_CAMPUS:
        return {"error": "本面板只支持 %s（合工大 fch 打表模式）；"
                         "其他场景请使用 python main.py" % SUPPORTED_CAMPUS}
    task_path = os.path.join(BASE_DIR, campus, task)
    if not os.path.isfile(task_path):
        return {"error": "任务表不存在：%s/%s" % (campus, task)}

    M.set_args(CONFIG_PATH)
    profile = M.build_profile(M._CONF)
    home = _home_fixture(task_path, campus)

    capture = CaptureTransport(
        M.dry_run_responder(home),
        sm2box=SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY),
        fixed_pair=((profile.cipherkey_encrypted, profile.cipherkey)
                    if profile.cipherkey_encrypted else None),
        fallback_key_b64=profile.cipherkey or None,
    )
    client = YunClient(profile, base_url=M.my_host, transport=capture,
                       rng=random.Random(20261025), sleep=lambda s: None)

    # 只跑这一张表：复制到独占目录，避免 random_choose 选中别的表
    os.makedirs(PICK_DIR, exist_ok=True)
    for old in os.listdir(PICK_DIR):
        os.remove(os.path.join(PICK_DIR, old))
    shutil.copyfile(task_path, os.path.join(PICK_DIR, task))

    result = {"campus": campus, "task": task, "synthetic_home": True,
              "warnings": [], "steps": [], "summary": {}}
    try:
        yun = M.Yun_For_New(client=client, home_info=home)
        yun.start()
        yun.do_by_points_map(path=PICK_DIR, random_choose=True)
        yun.finish_by_points_map()
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["steps"] = _trace_to_steps(capture.trace)
        # 校验在 start 之前失败（如踩点不足）时没有任何请求，右侧对照表仍要
        # 给出"学校要求 vs 任务表实际"，否则用户只看到一句报错不知道差多少。
        result["summary"] = _requirements_from_table(task_path, str(exc))
        result["warnings"] = [str(exc)]
        pts, km = _task_points(task_path)
        result["track"] = pts
        result["track_km"] = round(km, 3)
        result["cards"] = _card_points(task_path)
        return result

    result["steps"] = _trace_to_steps(capture.trace)
    pts, km = _task_points(task_path)
    result["summary"] = _summarize(yun, capture.trace, km)
    result["warnings"] = _warnings(result["summary"])
    result["track"] = pts
    result["track_km"] = round(km, 3)
    result["cards"] = _card_points(task_path)
    return result


def _trace_to_steps(trace):
    steps = []
    split_idx = 0
    for t in trace:
        r = t["router"]
        b = t.get("business") or {}
        if r.endswith("/run/getHomeRunInfo"):
            steps.append({"kind": "home", "title": "读取跑步任务", "router": r})
        elif r.endswith("/run/start"):
            steps.append({"kind": "start", "title": "创建跑步记录",
                          "router": r, "body": b})
        elif r.endswith("/run/splitPointCheating"):
            split_idx += 1
            pts = b.get("cardPointList") or []
            steps.append({
                "kind": "split", "title": "上传第 %d 批" % split_idx,
                "router": r, "body": b,
                "points": len(pts),
                "mileage": round(float(b.get("mileage") or 0), 1),
                "times": b.get("times"),
                "runSteps": round(float(b.get("runSteps") or 0), 1),
                "strides": round(float(b.get("strides") or 0), 3),
                "speeds": round(float(b.get("speeds") or 0), 2),
            })
        elif r.endswith("/run/isStandard"):
            steps.append({"kind": "standard", "title": "结束前状态检查",
                          "router": r, "body": b})
        elif r.endswith("/run/finish"):
            steps.append({"kind": "finish", "title": "提交结束",
                          "router": r, "body": b})
        else:
            steps.append({"kind": "other", "title": r, "router": r, "body": b})
    return steps


def _summarize(yun, trace, task_table_km=None):
    splits = [s for s in trace if s["router"].endswith("/run/splitPointCheating")]
    fin = next((s["business"] for s in trace
                if s["router"].endswith("/run/finish")), {}) or {}
    spms = [float((s.get("business") or {}).get("runSteps") or 0) for s in splits]
    return {
        "record_id": getattr(yun, "crsRunRecordId", None),
        "batches": len(splits),
        "total_points": sum(len((s.get("business") or {}).get("cardPointList") or [])
                            for s in splits),
        "km": fin.get("recordMileage"),
        "cadence": fin.get("recodeCadence"),
        "pace": fin.get("recodePace"),
        "dislikes": fin.get("recodeDislikes"),
        "duration": fin.get("duration"),
        "spm_min": round(min(spms), 1) if spms else None,
        "spm_max": round(max(spms), 1) if spms else None,
        "spm_ok": all(40 <= x <= 300 for x in spms) if spms else None,
        "sys_edition": fin.get("sysEdition"),
        "app_edition": fin.get("appEdition"),
        "manage_list": fin.get("manageList") or [],
        "cap_m": getattr(yun, "distance_cap_m", None),
        "local_cap_m": getattr(yun, "local_cap_m", None),
        "server_cap_m": getattr(yun, "server_cap_m", None),
        "task_table_km": (round(task_table_km, 3)
                          if task_table_km is not None else None),
        # 学校要求 vs 实际值，供预检界面右侧做对照
        "requirements": _requirements(yun, fin),
    }


def _requirements_from_table(task_path, error):
    """start 之前就失败时，用任务表自身的数据给出对照（没有请求体可展示）。"""
    km = 0.0
    marked = 0
    total = 0
    try:
        j = json.load(open(task_path, encoding="utf-8-sig"))
        d = j.get("data") or {}
        rows = d.get("pointsList") or []
        if rows:
            km = float(rows[-1].get("runMileage") or 0) / 1000.0
        manage = d.get("manageList") or []
        total = len(manage)
        marked = sum(1 for m in manage
                     if str(m.get("marked", "")).strip().upper() == "Y")
    except (OSError, ValueError, TypeError):
        pass
    cap = _cap_km()
    need = int(getattr(M, "min_dislikes", 0) or 0)
    shown_km = min(km, cap) if cap else km
    return {
        "batches": 0, "total_points": 0,
        "km": "%.2f" % shown_km,
        "cap_m": cap * 1000 if cap else None,
        "local_cap_m": cap * 1000 if cap else None,
        "task_table_km": round(km, 3),
        "requirements": [
            {"key": "mileage", "label": "总里程",
             "required": "≤ %.2f km" % cap if cap else "—",
             "actual": "%.2f km" % shown_km,
             "ok": True if cap and shown_km <= cap + 1e-9 else None,
             "note": "本地硬上限" if cap else ""},
            {"key": "cadence", "label": "平均步频",
             "required": "40 ~ 300 spm", "actual": "未计算",
             "ok": None, "note": "校验未通过，未构造请求"},
            {"key": "dislikes", "label": "踩点数",
             "required": "≥ %d 个" % need if need else "—",
             "actual": "%d / %d 个" % (marked, total),
             "ok": (marked >= need) if need else None,
             "note": "任务表标记为 Y 的数量"},
        ],
    }


def _requirements(yun, fin):
    """把"学校要求"与"本次实际提交值"配对，供界面逐行对照。"""
    marked = sum(1 for m in (fin.get("manageList") or [])
                 if str(m.get("marked", "")).strip().upper() == "Y")
    return [
        {"key": "mileage", "label": "总里程",
         "required": "≤ %.2f km" % (getattr(yun, "distance_cap_m", 0) / 1000.0),
         "actual": "%s km" % fin.get("recordMileage"),
         "ok": _num_ok(fin.get("recordMileage"), 0,
                       getattr(yun, "distance_cap_m", 0) / 1000.0),
         "note": ("本地硬上限" if getattr(yun, "local_cap_m", 0) else "学校下发上限")},
        {"key": "cadence", "label": "平均步频",
         "required": "40 ~ 300 spm",
         "actual": "%s spm" % fin.get("recodeCadence"),
         "ok": _num_ok(fin.get("recodeCadence"), 40, 300)},
        {"key": "dislikes", "label": "踩点数",
         "required": "≥ %d 个" % int(getattr(yun, "raDislikes", 0) or 0),
         "actual": "%d 个" % marked,
         "ok": marked >= int(getattr(yun, "raDislikes", 0) or 0),
         "note": ("学校要求 %d，任务下发 %d" %
                  (int(getattr(yun, "raDislikes", 0) or 0),
                   int(getattr(yun, "server_dislikes", 0) or 0))
                  if getattr(yun, "server_dislikes", 0) !=
                  int(getattr(yun, "raDislikes", 0) or 0) else "")},
        {"key": "pace", "label": "配速",
         "required": "任务区间内",
         "actual": "%s min/km" % fin.get("recodePace")},
    ]


def _num_ok(v, lo, hi):
    try:
        return lo <= float(v) <= hi
    except (TypeError, ValueError):
        return None


def _warnings(summary):
    out = []
    if summary.get("spm_ok") is False:
        out.append("步频越界：批内 runSteps 范围 %s~%s，服务端要求 40~300"
                   % (summary.get("spm_min"), summary.get("spm_max")))
    km = summary.get("km")
    try:
        if km is not None and float(km) < 1.0:
            out.append("总里程仅 %.2f km，请确认学校任务下限" % float(km))
    except (TypeError, ValueError):
        pass
    for req in (summary.get("requirements") or []):
        if req.get("key") == "dislikes" and req.get("ok") is False:
            out.append("踩点数不足：要求 %s，实际 %s（%s）"
                       % (req.get("required"), req.get("actual"),
                          req.get("note") or "服务端会判不合格"))
    out.append("预检使用本地夹具推算的任务参数，真实下发值以服务端为准")
    return out


# ------------------------------------------------------------------ 真实上传
def start_upload(campus, task):
    """按预检计划真实上传。只有预检通过且用户确认后才会走到这里。"""
    if campus != SUPPORTED_CAMPUS:
        return {"error": "本面板只支持 %s（合工大 fch 打表模式）；"
                         "其他场景请使用 python main.py" % SUPPORTED_CAMPUS}
    with STATE["lock"]:
        plan = STATE["plan"]
        if not plan or plan.get("campus") != campus or plan.get("task") != task:
            return {"error": "请先完成预检并确认，再开始上传"}
        if STATE["progress"].get("phase") == "running":
            return {"error": "已有上传在运行"}
        STATE["log"] = []
        STATE["progress"] = {"phase": "starting", "sent": 0,
                             "total": plan["summary"].get("batches", 0),
                             "detail": "启动中"}
    threading.Thread(target=_run_upload, args=(campus, task), daemon=True).start()
    return {"ok": True}


def _run_upload(campus, task):
    task_path = os.path.join(BASE_DIR, campus, task)
    os.makedirs(PICK_DIR, exist_ok=True)
    for old in os.listdir(PICK_DIR):
        os.remove(os.path.join(PICK_DIR, old))
    shutil.copyfile(task_path, os.path.join(PICK_DIR, task))

    def log(s):
        with STATE["lock"]:
            STATE["log"].append(s if s.endswith("\n") else s + "\n")

    try:
        M.set_args(CONFIG_PATH)
        client = M.default_client()
        real_post = client.post_json
        counter = {"n": 0}

        def counted(router, *a, **kw):
            obj = real_post(router, *a, **kw)
            if router.endswith("/run/splitPointCheating"):
                counter["n"] += 1
                # 点数优先：raw_bytes 是 gzip 后的 split 体，解出来数 cardPointList。
                n_pts = 0
                raw = kw.get("raw_bytes")
                if raw:
                    try:
                        import gzip as _gz
                        body = json.loads(_gz.decompress(raw).decode("utf-8"))
                        n_pts = len(body.get("cardPointList") or [])
                    except Exception:
                        n_pts = 0
                with STATE["lock"]:
                    pr = STATE["progress"]
                    pr["batches_sent"] = counter["n"]
                    if n_pts:
                        pr["sent"] = pr.get("sent", 0) + n_pts
                    pr["detail"] = "已上传 %d 批 / %d 点" % (
                        counter["n"], pr.get("sent", 0))
            return obj

        client.post_json = counted
        with STATE["lock"]:
            STATE["progress"]["phase"] = "running"
        yun = M.Yun_For_New(client=client)
        log("任务参数已读取，正在创建跑步记录…")
        yun.start()
        log("跑步记录已创建：recordId=%s" % yun.crsRunRecordId)
        # 真实批量由服务端下发的 passPointNum 决定，可能与预检假设不同。
        # 因此进度以"点数"为准，批次只做展示：分母用实际会上报的点数。
        total_pts = yun._effective_point_count(
            json.load(open(os.path.join(PICK_DIR, task), encoding="utf-8"))["data"]["pointsList"])
        with STATE["lock"]:
            STATE["progress"]["total"] = total_pts
            STATE["progress"]["batch_size"] = yun.upload_batch_size
            STATE["progress"]["unit"] = "points"
            STATE["progress"]["detail"] = "共 %d 点，每批 %d 点" % (total_pts, yun.upload_batch_size)
        log("批量=%d 点/批，共 %d 点（服务端 passPointNum 决定）"
            % (yun.upload_batch_size, total_pts))
        yun.do_by_points_map(path=PICK_DIR, random_choose=True)
        yun.finish_by_points_map()
        with STATE["lock"]:
            STATE["progress"].update(phase="done", detail="上传完成")
        log("上传完成")
    except Exception as exc:
        with STATE["lock"]:
            STATE["progress"].update(phase="error",
                                     detail="%s: %s" % (type(exc).__name__, exc))
        log("失败：%s: %s" % (type(exc).__name__, exc))
        log(traceback.format_exc())


# ------------------------------------------------------------------ HTTP
PAGE_PATH = os.path.join(BASE_DIR, "panel_ui.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "YunPanel/2.0"

    def _send(self, code, ctype, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ";charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                with open(PAGE_PATH, encoding="utf-8") as f:
                    self._send(200, "text/html", f.read())
            elif u.path == "/api/meta":
                self._json({"campuses": list_campuses(),
                            "max_mileage_km": _cap_km(),
                            "supported_campus": SUPPORTED_CAMPUS})
            elif u.path == "/api/config":
                self._json(read_config())
            elif u.path == "/api/track":
                campus = (q.get("campus") or [""])[0]
                task = (q.get("task") or [""])[0]
                path = os.path.join(BASE_DIR, campus, task)
                if not os.path.isfile(path):
                    self._json({"error": "任务表不存在"})
                else:
                    pts, km = _task_points(path)
                    self._json({"track": pts, "km": round(km, 3),
                                "cards": _card_points(path)})
            elif u.path == "/api/preview":
                campus = (q.get("campus") or [""])[0]
                task = (q.get("task") or [""])[0]
                out = build_preview(campus, task)
                with STATE["lock"]:
                    STATE["plan"] = (None if out.get("error")
                                     else {"campus": campus, "task": task,
                                           "summary": out["summary"]})
                self._json(out)
            elif u.path == "/api/upload/status":
                with STATE["lock"]:
                    self._json({"progress": dict(STATE["progress"]),
                                "log": "".join(STATE["log"])})
            else:
                self._send(404, "text/plain", "not found")
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            self._json({"error": "bad json: %s" % exc})
            return
        try:
            if u.path == "/api/config":
                self._json({"ok": True, "changed": save_config(body)})
            elif u.path == "/api/upload":
                self._json(start_upload(body.get("campus", ""),
                                        body.get("task", "")))
            else:
                self._send(404, "text/plain", "not found")
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)})

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="云运动本地图形工作台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    port = args.port
    for _ in range(12):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("没有可用端口")
    url = "http://127.0.0.1:%d/" % port
    print("[panel] 云运动工作台已启动：%s  （Ctrl+C 退出）" % url)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
