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
import re
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import history
import main as M
import yun_route  # noqa: F401  （保留：面板与生成器共用同一套几何函数）
from yun_http import FakeTransport, SM2Box, YunClient

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")   # 模板（新用户从这里复制）
CONFIG_DIR = os.path.join(BASE_DIR, "configs")        # 每用户一个 .ini
DEFAULT_USER = "user"
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


class _Tee:
    """把写入同时送到原始流和面板日志。

    main.py 的进度/结果都用 print() 输出，GUI 直接调用 Yun_For_New 时这些
    内容只进了进程 stdout，前端看不到。这里在**上传线程**内临时接管
    sys.stdout/sys.stderr，让终端里能看到的东西原样出现在 GUI 日志里。
    保留原始流写入，命令行启动时行为不变。
    """

    def __init__(self, original, sink):
        self._original = original
        self._sink = sink

    def write(self, text):
        if text:
            try:
                self._original.write(text)
            except Exception:
                pass
            if text.strip():
                self._sink(text.rstrip(chr(10)))
        return len(text) if text else 0

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def isatty(self):
        return False


# ------------------------------------------------------------------ 目录扫描
# 本面板只针对该校区做过完整验证（见 SUPPORTED_CAMPUS 的说明）。
SUPPORTED_CAMPUS = "tasks_fch"


def list_campuses():
    """所有可选的校区目录（不再强制过滤）。

    只有 tasks_fch（合工大翡翠湖）经过完整实测验证；其余目录同样列出，
    由界面提示"未验证"，避免用户以为面板不支持而误用命令行。
    """
    out = {}
    for d in sorted(os.listdir(BASE_DIR)):
        if d.startswith("tasks_") and os.path.isdir(os.path.join(BASE_DIR, d)):
            files = sorted(f for f in os.listdir(os.path.join(BASE_DIR, d))
                           if f.endswith(".json"))
            if files:
                out[d] = files
    return out


def safe_task_path(campus, task):
    """把 (campus, task) 解析成仓库内的任务表路径；越界返回 None。

    只允许 tasks_* 目录下的 .json，拒绝 ../、绝对路径、符号链接逃逸。
    所有接受前端路径参数的端点都必须走这里。
    """
    if not campus or not task:
        return None
    if "/" in campus or "\\" in campus or campus in (".", ".."):
        return None
    if not campus.startswith("tasks_"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.json", task) or task.startswith("."):
        return None
    base = os.path.realpath(os.path.join(BASE_DIR, campus))
    path = os.path.realpath(os.path.join(base, task))
    if not path.startswith(base + os.sep):
        return None
    return path if os.path.isfile(path) else None


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


# ------------------------------------------------------------------ 多用户配置
# 每个用户一个 configs/<name>.ini；config.ini 只作模板。
# 用户之间完全独立（token/设备/账号互不影响），但历史记录任务表是共享的。
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-一-龥]{1,24}$")


def _user_path(name):
    """用户名 -> 配置文件绝对路径。非法名返回 None（防目录穿越）。"""
    name = (name or "").strip()
    if not USER_NAME_RE.match(name):
        return None
    return os.path.join(CONFIG_DIR, name + ".ini")


def list_users():
    """已存在的用户列表，user 永远排第一。

    首次调用时先把 config.ini 复制成 configs/user.ini，保证至少有一个用户。
    """
    _ensure_default_user()
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(CONFIG_DIR)
                   if f.endswith(".ini"))
    if DEFAULT_USER in names:
        names.remove(DEFAULT_USER)
        names.insert(0, DEFAULT_USER)
    return names


def _ensure_default_user():
    """首次运行时把 config.ini 复制成 configs/user.ini。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = _user_path(DEFAULT_USER)
    if not os.path.exists(path) and os.path.exists(CONFIG_PATH):
        shutil.copyfile(CONFIG_PATH, path)
    return path


def create_user(name):
    """新建用户：复制模板（或当前 user）为 configs/<name>.ini。"""
    name = (name or "").strip()
    path = _user_path(name)
    if path is None:
        return {"error": "用户名只能用中英文、数字、下划线或短横线（1~24 字）"}
    if os.path.exists(path):
        return {"error": "用户 %s 已存在" % name}
    src = _ensure_default_user() or CONFIG_PATH
    os.makedirs(CONFIG_DIR, exist_ok=True)
    shutil.copyfile(src, path)
    # 新用户清空身份字段，避免误用别人的 token
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if cp.has_section("User"):
        for k in ("token", "device_id", "device_name", "utc", "uuid", "sign"):
            cp["User"][k] = ""
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)
    return {"ok": True, "users": list_users(), "current": name}


def delete_user(name):
    if name == DEFAULT_USER:
        return {"error": "默认用户 user 不能删除"}
    path = _user_path(name)
    if path is None or not os.path.exists(path):
        return {"error": "用户不存在"}
    os.remove(path)
    return {"ok": True, "users": list_users()}


def read_config(name=DEFAULT_USER):
    path = _user_path(name)
    if path is None:
        raise ValueError("用户名非法：%r" % (name,))
    if not os.path.exists(path):
        path = _ensure_default_user()
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    out = {}
    for sec in cp.sections():
        out[sec] = {k: (MASK if k.lower() in SENSITIVE_KEYS and cp[sec][k]
                        else cp[sec][k]) for k in cp[sec]}
    return out


def save_config(payload, name=DEFAULT_USER):
    path = _user_path(name)
    if path is None:
        raise ValueError("用户名非法")
    _ensure_default_user()
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
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
                changed.append("%s.%s" % (sec, k))
            cp[sec][k] = new
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)
    return changed


def config_path_for(name):
    """上传/预检用：返回该用户的配置文件绝对路径。

    非法用户名直接报错（不能静默回退到默认用户，否则会用别人的 token 上传）。
    合法但尚未创建的用户回退到默认配置。
    """
    path = _user_path(name)
    if path is None:
        raise ValueError("用户名非法：%r" % (name,))
    return path if os.path.exists(path) else _ensure_default_user()


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


def _school_requirements():
    """读取学校/本地的硬性要求（来自 config.ini [Run] 与固定契约）。"""
    cp = configparser.ConfigParser()
    cp.read(config_path_for(DEFAULT_USER), encoding="utf-8")
    try:
        max_km = float(cp.get("Run", "max_mileage_km", fallback="0") or 0)
    except ValueError:
        max_km = 0.0
    try:
        min_dislikes = int(cp.get("Run", "min_dislikes", fallback="0") or 0)
    except ValueError:
        min_dislikes = 0
    return {
        "max_km": max_km if max_km > 0 else None,
        "min_dislikes": min_dislikes,
        "spm_bounds": M.SERVER_SPM_BOUNDS,     # (40, 300)
    }


def _card_points_from_manage(manage_list):
    """manageList -> [(lon, lat, marked)]，用于几何覆盖校验。"""
    out = []
    for m in (manage_list or []):
        try:
            lon, lat = str(m.get("point", "")).split(",")
            out.append((float(lon), float(lat),
                        str(m.get("marked", "")).strip().upper() == "Y"))
        except (ValueError, AttributeError):
            continue
    return out


def _check_naturalized(task, req, card_points):
    """校验一条自然化任务是否满足学校要求。返回错误列表（空 = 通过）。"""
    errs = []
    data = task.get("data") or {}
    pts = data.get("pointsList") or []

    # 1. 里程上限
    km = float(data.get("recordMileage") or 0)
    if req["max_km"] and km > req["max_km"] + 1e-6:
        errs.append("里程 %.2f km 超过上限 %.2f km" % (km, req["max_km"]))

    # 2. 步频：逐包检查（与上传时的 runSteps 同口径）
    lo, hi = req["spm_bounds"]
    batch = 11
    for i in range(0, len(pts), batch):
        chunk = pts[i:i + batch]
        if len(chunk) < 2:
            continue
        dur = int(chunk[-1]["runTime"]) - int(chunk[0]["runTime"])
        if dur <= 0:
            continue
        steps = int(chunk[-1]["runStep"]) - int(chunk[0]["runStep"])
        spm = steps * 60.0 / dur
        if not lo <= spm <= hi:
            errs.append("第 %d 批步频 %.0f 超出 %g~%g" %
                        (i // batch + 1, spm, lo, hi))
            break
    cad = float(data.get("recodeCadence") or 0)
    if cad and not lo <= cad <= hi:
        errs.append("汇总步频 %.0f 超出 %g~%g" % (cad, lo, hi))

    # 3. 踩点：既看标记数量，也看几何是否真的经过打卡点
    marked = sum(1 for m in (data.get("manageList") or [])
                 if str(m.get("marked", "")).strip().upper() == "Y")
    if marked < req["min_dislikes"]:
        errs.append("标记踩点 %d 个，要求至少 %d 个" %
                    (marked, req["min_dislikes"]))
    if card_points:
        track = []
        for r in pts:
            try:
                lon, lat = str(r.get("point", "")).split(",")
                track.append((float(lon), float(lat)))
            except (ValueError, AttributeError):
                continue
        # 打卡半径沿用服务端下发的默认 20m
        covered = sum(1 for lon, lat, _ in card_points
                      if any(yun_route.distance((lon, lat), p) <= 20.0
                             for p in track))
        need = max(marked, req["min_dislikes"])
        if covered < need:
            errs.append("轨迹实际只经过 %d/%d 个打卡点，要求至少 %d 个" %
                        (covered, len(card_points), need))
    return errs


def naturalize(campus, task, strength="medium", seed=None, target_km=None):
    """把任务表轨迹形变成一条新的自然轨迹，返回可上传的任务表 + 对比数据。

    只做几何与节奏生成，**不联网**。输出是完整任务表，可保存后在预检里使用。

    strength: light / medium / strong，对应 NATURAL_STRENGTHS 三档。
    seed:     None 时随机（每次不同）；给固定值可复现。
    target_km: 目标里程；None 时沿用源表里程。
    """
    task_path = safe_task_path(campus, task)
    if not task_path:
        return {"error": "任务表不存在或路径非法：%s/%s" % (campus, task)}
    if strength not in yun_route.NATURAL_STRENGTHS:
        return {"error": "未知强度：%s" % strength}

    src, metrics = yun_route.load_source(task_path)
    j = json.load(open(task_path, encoding="utf-8-sig"))
    d = j.get("data") or {}
    km = float(d.get("recordMileage") or 0) or (metrics["length_m"] / 1000.0)
    target = float(target_km) if target_km else km
    seed = int(seed) if seed not in (None, "") else random.randrange(1, 2**31)

    prof = dict(yun_route.NATURAL_PROFILE, **yun_route.NATURAL_STRENGTHS[strength])
    cfg = {
        "source_points": [[p[0], p[1]] for p in src],
        "coordinate_system": "GCJ-02",
        "distance_m": round(target * 1000, 3),
        "sample_seconds": 2,
        "seed": seed,
        "deform_profile": prof,
        # 节奏沿用源表的实测值，越界时由 generate_cfg 回退名义值
        "pace_min_km": round(metrics["pace_min_km"] or 6.0, 3),
        "cadence_spm": round(metrics["cadence_spm"] or 160.0, 3),
        "pace_offset_pct": 0.0,
        "cadence_offset_pct": 0.0,
    }
    # 学校要求：里程上限、最少踩点数、步频区间
    req = _school_requirements()
    # 目标里程不得超过学校上限
    if req["max_km"] and target > req["max_km"]:
        target = req["max_km"]
        cfg["distance_m"] = round(target * 1000, 3)
    src_manage = d.get("manageList") or []
    card_points = _card_points_from_manage(src_manage)

    # 生成 → 校验 → 不满足就换种子重抽。
    # 必须在这里就保证合规，否则用户保存后才发现预检不过。
    attempts = 8
    last_err = ""
    gen = None
    used_seed = seed
    for i in range(attempts):
        used_seed = seed + i * 100003
        cfg["seed"] = used_seed
        try:
            cand = yun_route.generate_cfg(cfg, Path(BASE_DIR))
        except ValueError as exc:
            last_err = str(exc)
            continue
        # 继承踩点信息
        if src_manage:
            cand["data"]["manageList"] = [dict(m) for m in src_manage]
            cand["data"]["recodeDislikes"] = sum(
                1 for m in src_manage
                if str(m.get("marked", "")).strip().upper() == "Y")
        errs = _check_naturalized(cand, req, card_points)
        if not errs:
            gen = cand
            break
        last_err = "；".join(errs)
    if gen is None:
        return {"error": "连续 %d 次生成都不满足学校要求（最后一次：%s）"
                         % (attempts, last_err), "attempts": attempts}

    gen["metadata"]["naturalized_from"] = "%s/%s" % (campus, task)
    gen["metadata"]["natural_strength"] = strength
    gen["metadata"]["natural_seed"] = used_seed
    gen["metadata"]["school_checked"] = True
    seed = used_seed

    out = [(float(r["point"].split(",")[0]), float(r["point"].split(",")[1]))
           for r in gen["data"]["pointsList"]]
    devs = sorted(min(yun_route.distance(p, q) for q in src) for p in out)
    chord = sum(yun_route.distance(a, b) for a, b in zip(out, out[1:]))
    return {
        "task": gen,
        "seed": seed,
        "strength": strength,
        "source_track": [[p[0], p[1]] for p in src],
        "new_track": [[p[0], p[1]] for p in out],
        "metrics": {
            "src_km": round(metrics["length_m"] / 1000.0, 3),
            "new_km": round(gen["data"]["recordMileage"], 3),
            "chord_m": round(chord, 1),
            "dev_median": round(devs[len(devs) // 2], 2),
            "dev_p90": round(devs[int(len(devs) * 0.9)], 2),
            "dev_max": round(devs[-1], 2),
            "points": len(out),
            "cadence": round(gen["data"]["recodeCadence"], 1),
            "pace": round(gen["data"]["recodePace"], 2),
        },
    }


def _temp_track_paths():
    """本次会话里由自然化产生、待上传成功后删除的临时任务表。"""
    return STATE.setdefault("temp_tracks", [])


def register_temp_track(rel_path):
    """登记一个临时任务表，上传成功后自动删除。"""
    lst = _temp_track_paths()
    if rel_path and rel_path not in lst:
        lst.append(rel_path)
    return {"ok": True, "tracked": list(lst)}


def cleanup_temp_tracks(keep_failed=True):
    """删除本次会话登记的临时任务表。

    keep_failed=True 时只在调用方明确表示"上传成功"后才删；
    失败时保留文件，方便用户排查或重试。
    """
    removed, kept = [], []
    for rel in list(_temp_track_paths()):
        path = os.path.join(BASE_DIR, rel)
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(rel)
        except OSError:
            kept.append(rel)
    STATE["temp_tracks"] = kept
    return {"removed": removed, "kept": kept}


def save_naturalized(task_obj, name):
    """把自然化结果保存成 tasks_else 下的任务表（编号续接）。"""
    if not isinstance(task_obj, dict) or "data" not in task_obj:
        return {"error": "任务表格式非法"}
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name or ""):
        return {"error": "名称只能用字母数字下划线短横线"}
    target_dir = os.path.join(BASE_DIR, "tasks_else")
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, name + ".json")
    if os.path.exists(path):
        return {"error": "文件已存在：tasks_else/%s.json" % name}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task_obj, f, ensure_ascii=False, indent=1)
    rel = "tasks_else/%s.json" % name
    register_temp_track(rel)
    return {"saved": rel, "temp": True}


def build_preview(campus, task, user=DEFAULT_USER):
    """跑一次完整的离线流程，返回逐步明细。不发任何真实请求。"""
    task_path = safe_task_path(campus, task)
    if not task_path:
        return {"error": "任务表不存在或路径非法：%s/%s" % (campus, task)}

    M.set_args(config_path_for(user))
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

    # 只跑这一张表：复制到**本次调用独占**的子目录，避免 random_choose
    # 选中别的表，也避免并发的预检/上传互相删文件。
    os.makedirs(PICK_DIR, exist_ok=True)
    pick = os.path.join(PICK_DIR, "preview_%d" % threading.get_ident())
    shutil.rmtree(pick, ignore_errors=True)
    os.makedirs(pick, exist_ok=True)
    shutil.copyfile(task_path, os.path.join(pick, task))

    result = {"campus": campus, "task": task, "synthetic_home": True,
              "warnings": [], "steps": [], "summary": {}}
    try:
        yun = M.Yun_For_New(client=client, home_info=home)
        yun.start()
        yun.do_by_points_map(path=pick, random_choose=True)
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
def start_upload(campus, task, user=DEFAULT_USER):
    """按预检计划真实上传。只有预检通过且用户确认后才会走到这里。"""
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
    threading.Thread(target=_run_upload, args=(campus, task, user), daemon=True).start()
    return {"ok": True}


def _run_upload(campus, task, user=DEFAULT_USER):
    task_path = safe_task_path(campus, task)
    if not task_path:
        log_line = "任务表不存在或路径非法：%s/%s" % (campus, task)
        with STATE["lock"]:
            STATE["progress"].update(phase="error", detail=log_line)
            STATE["log"].append(log_line + chr(10))
        return
    # 每个上传用独立子目录：预检与上传可能并发，共用一个目录会互相删文件。
    pick = os.path.join(PICK_DIR, "%s_%d" % (
        os.path.splitext(task)[0], threading.get_ident()))
    shutil.rmtree(pick, ignore_errors=True)
    os.makedirs(pick, exist_ok=True)
    shutil.copyfile(task_path, os.path.join(pick, task))

    def log(s):
        with STATE["lock"]:
            STATE["log"].append(s if s.endswith("\n") else s + "\n")

    # 接管本线程的 stdout/stderr：main.py 的 print 内容同步进 GUI 日志。
    _old_out, _old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(_old_out, log)
    sys.stderr = _Tee(_old_err, log)
    try:
        M.set_args(config_path_for(user))
        client = M.default_client()
        real_post = client.post_json
        counter = {"n": 0, "t0": time.time()}

        def counted(router, *a, **kw):
            t0 = time.time()
            obj = real_post(router, *a, **kw)
            cost = int((time.time() - t0) * 1000)
            code = (obj or {}).get("code")
            name = router.rsplit("/", 1)[-1]
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
                log("[%s] 第 %d 批 %d 点 · %dms · code=%s"
                    % (time.strftime("%H:%M:%S"), counter["n"], n_pts, cost, code))
            else:
                log("[%s] %s · %dms · code=%s"
                    % (time.strftime("%H:%M:%S"), name, cost, code))
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
            json.load(open(os.path.join(pick, task), encoding="utf-8"))["data"]["pointsList"])
        with STATE["lock"]:
            STATE["progress"]["total"] = total_pts
            STATE["progress"]["batch_size"] = yun.upload_batch_size
            STATE["progress"]["unit"] = "points"
            STATE["progress"]["detail"] = "共 %d 点，每批 %d 点" % (total_pts, yun.upload_batch_size)
        log("批量=%d 点/批，共 %d 点（服务端 passPointNum 决定）"
            % (yun.upload_batch_size, total_pts))
        yun.do_by_points_map(path=pick, random_choose=True)
        yun.finish_by_points_map()
        d = yun.task_map.get("data") or {}
        log("=" * 52)
        log("[OK] 上传成功  总耗时 %.1fs" % (time.time() - counter["t0"]))
        log("  总里程    %s km" % d.get("recordMileage"))
        log("  配速      %.2f min/km" % float(d.get("recodePace") or 0))
        log("  平均步频  %d spm" % round(float(d.get("recodeCadence") or 0)))
        log("  踩点数    %s" % d.get("recodeDislikes"))
        log("  时长      %s 秒" % d.get("duration"))
        log("  批次      %d 批" % counter["n"])
        log("=" * 52)
        # 上传成功：删掉本次生成的临时轨迹
        cleaned = cleanup_temp_tracks()
        if cleaned["removed"]:
            log("已删除临时轨迹：" + "、".join(cleaned["removed"]))
        with STATE["lock"]:
            STATE["progress"].update(
                phase="done", detail="上传成功",
                removed=cleaned["removed"],
                summary={"km": d.get("recordMileage"),
                         "pace": round(float(d.get("recodePace") or 0), 2),
                         "cadence": round(float(d.get("recodeCadence") or 0)),
                         "dislikes": d.get("recodeDislikes"),
                         "duration": d.get("duration"),
                         "batches": counter["n"]})
    except Exception as exc:
        with STATE["lock"]:
            STATE["progress"].update(phase="error",
                                     detail="%s: %s" % (type(exc).__name__, exc))
        log("[FAIL] 上传失败：%s: %s" % (type(exc).__name__, exc))
        log(traceback.format_exc())
    finally:
        sys.stdout, sys.stderr = _old_out, _old_err


# ------------------------------------------------------------------ 历史记录
def history_terms(user=DEFAULT_USER):
    """拉取学期列表（只读）。"""
    M.set_args(config_path_for(user))
    client = M.default_client()
    obj = client.post_json("/run/listXnYearXqByStudentId", "",
                           raise_on_business_code=False)
    if obj.get("code") != 200:
        return {"error": "获取学期列表失败：%s" % obj.get("msg")}
    terms = obj.get("data") or []
    return {"terms": [{"key": t.get("key"), "value": t.get("value"),
                       "sjd": t.get("sjd")} for t in terms]}


def history_runs(table, user=DEFAULT_USER):
    """拉取某学期的跑步记录列表（只读）。"""
    if not table or not re.fullmatch(r"[A-Za-z0-9_]+", table):
        return {"error": "学期表名非法"}
    M.set_args(config_path_for(user))
    client = M.default_client()
    obj = client.post_json("/run/crsReocordInfoList",
                           json.dumps({"tableName": table}),
                           raise_on_business_code=False)
    if obj.get("code") != 200:
        return {"error": "获取记录列表失败：%s" % obj.get("msg")}
    runs = []
    for month in (obj.get("data") or {}).get("rank") or []:
        runs.extend(month.get("rankList") or [])
    out = []
    for r in runs:
        out.append({
            "id": r.get("id"),
            "endTime": (r.get("endTime") or "").strip(),
            "recordMileage": r.get("recordMileage"),
            "isQualified": r.get("isQualified"),
            "raName": r.get("raName"),
        })
    return {"runs": out}


def history_fetch(rec_id, table, out_dir=None, user=DEFAULT_USER):
    """拉取单条记录详情并保存为任务表文件（只读 + 本地写盘）。

    复用 history.py 的保存逻辑：文件名为 tasklist_N.json，编号续接。
    """
    if not rec_id or not table:
        return {"error": "缺少记录 id 或学期表名"}
    if not re.fullmatch(r"[A-Za-z0-9_]+", str(table)):
        return {"error": "学期表名非法"}
    M.set_args(config_path_for(user))
    client = M.default_client()
    text = client.post("/run/crsReocordInfo",
                       json.dumps({"id": rec_id, "tableName": table}))
    try:
        detail = json.loads(text)
    except ValueError:
        return {"error": "服务端返回的不是 JSON"}
    if detail.get("code") != 200:
        return {"error": "获取详情失败：%s" % detail.get("msg")}

    target_dir = out_dir or os.path.join(BASE_DIR, "tasks_else")
    saved = history.save_history_record(text, target_dir)
    d = detail.get("data") or {}
    return {
        "saved": os.path.relpath(saved, BASE_DIR).replace("\\", "/"),
        "summary": {
            "start": d.get("recordStartTime"),
            "end": d.get("recordEndTime"),
            "km": d.get("recordMileage"),
            "pace": d.get("recodePace"),
            "cadence": d.get("recodeCadence"),
            "duration": d.get("duration"),
            "dislikes": d.get("recodeDislikes"),
            "points": len(d.get("pointsList") or []),
        },
    }


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
                            "supported_campus": SUPPORTED_CAMPUS,
                            "temp_tracks": list(_temp_track_paths())})
            elif u.path == "/api/users":
                self._json({"users": list_users(), "current": DEFAULT_USER})
            elif u.path == "/api/config":
                self._json(read_config((q.get("user") or [DEFAULT_USER])[0]))
            elif u.path == "/api/track":
                campus = (q.get("campus") or [""])[0]
                task = (q.get("task") or [""])[0]
                path = safe_task_path(campus, task)
                if not path:
                    self._json({"error": "任务表不存在或路径非法"})
                else:
                    pts, km = _task_points(path)
                    self._json({"track": pts, "km": round(km, 3),
                                "cards": _card_points(path)})
            elif u.path == "/api/preview":
                campus = (q.get("campus") or [""])[0]
                task = (q.get("task") or [""])[0]
                out = build_preview(campus, task,
                                    (q.get("user") or [DEFAULT_USER])[0])
                with STATE["lock"]:
                    STATE["plan"] = (None if out.get("error")
                                     else {"campus": campus, "task": task,
                                           "summary": out["summary"]})
                self._json(out)
            elif u.path == "/api/naturalize":
                self._json(naturalize(
                    (q.get("campus") or [""])[0],
                    (q.get("task") or [""])[0],
                    (q.get("strength") or ["medium"])[0],
                    (q.get("seed") or [None])[0],
                    (q.get("km") or [None])[0] or None))
            elif u.path == "/api/history/terms":
                self._json(history_terms((q.get("user") or [DEFAULT_USER])[0]))
            elif u.path == "/api/history/runs":
                self._json(history_runs((q.get("table") or [""])[0],
                                        (q.get("user") or [DEFAULT_USER])[0]))
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
                self._json({"ok": True,
                            "changed": save_config(body.get("data") or {},
                                                   body.get("user") or DEFAULT_USER)})
            elif u.path == "/api/users/create":
                self._json(create_user(body.get("name")))
            elif u.path == "/api/users/delete":
                self._json(delete_user(body.get("name")))
            elif u.path == "/api/naturalize/cleanup":
                self._json(cleanup_temp_tracks())
            elif u.path == "/api/naturalize/save":
                self._json(save_naturalized(body.get("task"), body.get("name")))
            elif u.path == "/api/history/fetch":
                self._json(history_fetch(body.get("id"), body.get("table"),
                                         body.get("out_dir"),
                                         body.get("user") or DEFAULT_USER))
            elif u.path == "/api/upload":
                self._json(start_upload(body.get("campus", ""),
                                        body.get("task", ""),
                                        body.get("user") or DEFAULT_USER))
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
