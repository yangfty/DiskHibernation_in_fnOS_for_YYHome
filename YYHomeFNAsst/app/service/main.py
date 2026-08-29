#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YYHomeFNAsst - DiskHibernation in fnOS for YYHome
fnOS 硬盘休眠管理后端服务（零依赖，仅使用 Python3 标准库）

功能：
- 自动识别系统中的物理硬盘（lsblk，按序列号/WWN 定位，不依赖固定 /dev/sdX）
- 查询硬盘电源状态（hdparm -C）
- 让指定硬盘立即休眠（hdparm -y），执行后自动复查状态
- 硬盘定时休眠：每天定时 / 空闲一段时间后自动发送休眠指令
- 监控空间清理：剩余空间低于阈值时，自动删除日期最早的录像文件夹
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

APP_NAME = "YYHomeFNAsst"
APP_VERSION = "0.1.1"
DEFAULT_PORT = 8327
PORT = int(os.environ.get("YYHOMEFNASST_PORT", str(DEFAULT_PORT)))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WWW_DIR = os.path.join(BASE_DIR, "www")

LSBLK_TIMEOUT = 10           # lsblk 超时（秒）
HDPARM_TIMEOUT = 10          # 单次 hdparm 命令超时（秒）
SLEEP_CHECK_DELAY = 2.5      # 发送休眠指令后首次检查延迟（秒）
SLEEP_CHECK_RETRY_DELAY = 3  # 二次检查延迟（秒）

# 每块硬盘一把锁，避免对同一设备并发执行 hdparm
_DEVICE_LOCKS = {}
_DEVICE_LOCKS_GUARD = threading.Lock()

# hdparm -C 输出状态 → 内部状态映射
# 不同版本 hdparm 可能输出：active/idle、active、idle、idle (low rpm)、
# standby、sleeping、unknown（unknown 表示该硬盘/桥接芯片不支持状态查询，
# 常见于 USB 硬盘盒，不影响发送休眠指令）
HDPARM_STATE_MAP = {
    "active/idle": "active",
    "active": "active",
    "idle": "idle",
    "idle (low rpm)": "idle",
    "standby": "standby",
    "sleeping": "sleeping",
    "unknown": "unknown",
}

# 每块硬盘最近一次休眠指令的执行结果（仅保存在内存中，服务重启后清空）
_LAST_SLEEP = {}


# ---------------------------------------------------------------- 配置管理

# 配置/运行时数据目录：优先使用 fnOS 注入的 TRIM_PKGVAR，
# 开发机上可通过 YYHOMEFNASST_VAR 环境变量覆盖
VAR_DIR = (os.environ.get("YYHOMEFNASST_VAR")
           or os.environ.get("TRIM_PKGVAR")
           or "/tmp/fnasst")
CONFIG_FILE = os.path.join(VAR_DIR, "config.json")

_CONFIG_LOCK = threading.RLock()
_CONFIG = None

SLEEP_MODES = ("off", "idle", "daily")  # 关闭 / 空闲后自动 / 每天定时


def _parse_hhmm(s):
    """校验 HH:MM 格式，返回 (时, 分) 或 None"""
    if isinstance(s, str):
        m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return h, mi
    return None


def _to_int(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _to_float(v, lo, hi):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _default_disk_cfg():
    """单块硬盘的任务配置默认值"""
    return {
        "sleep_mode": "off",        # off / idle / daily
        "sleep_idle_min": 30,       # idle 模式：持续非休眠 N 分钟后自动休眠
        "sleep_daily_at": "02:00",  # daily 模式：每天定时休眠时间
        "sleep_daily_last": "",     # daily 模式：上次执行日期（YYYY-MM-DD）
        "space_enabled": False,     # 是否开启空间清理
        "space_threshold_gb": 20,   # 剩余空间低于该值（GB）时触发清理
        "space_path": "",           # 监控目录（绝对路径）
        "space_check_time": "00:00",  # 每天空间清理检查时间（每盘独立）
        "space_last": None,         # 上次清理结果
        "space_last_date": "",      # 上次自动检查日期（YYYY-MM-DD）
    }


def _default_config():
    return {"version": 1, "disks": {}}


def _sanitize_disk_cfg(dc):
    """把外部（配置文件/请求体）的硬盘配置规范化为完整结构，非法值回退默认值"""
    out = _default_disk_cfg()
    if dc.get("sleep_mode") in SLEEP_MODES:
        out["sleep_mode"] = dc["sleep_mode"]
    v = _to_int(dc.get("sleep_idle_min"), 1, 1440)
    if v is not None:
        out["sleep_idle_min"] = v
    if _parse_hhmm(dc.get("sleep_daily_at")):
        out["sleep_daily_at"] = dc["sleep_daily_at"].strip()
    if isinstance(dc.get("space_enabled"), bool):
        out["space_enabled"] = dc["space_enabled"]
    v = _to_float(dc.get("space_threshold_gb"), 1, 100000)
    if v is not None:
        out["space_threshold_gb"] = v
    p = dc.get("space_path")
    if isinstance(p, str) and p.strip() and os.path.isabs(p.strip()) and p.strip() not in ("/", "\\"):
        out["space_path"] = os.path.normpath(p.strip())
    if _parse_hhmm(dc.get("space_check_time")):
        out["space_check_time"] = dc["space_check_time"].strip()
    # 运行时状态字段直接透传
    for k in ("sleep_daily_last", "space_last_date", "space_last"):
        if k in dc:
            out[k] = dc[k]
    return out


def load_config():
    """加载配置（内存缓存，首次从磁盘读取）"""
    global _CONFIG
    with _CONFIG_LOCK:
        if _CONFIG is None:
            cfg = _default_config()
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    # 迁移：V0.0.3 及之前空间检查时间是全局配置，
                    # 下放到当时没有单独设置检查时间的硬盘
                    legacy_global_time = raw.get("space_check_time")
                    if not (isinstance(legacy_global_time, str) and _parse_hhmm(legacy_global_time)):
                        legacy_global_time = None
                    disks = raw.get("disks")
                    if isinstance(disks, dict):
                        for disk_id, dc in disks.items():
                            if isinstance(dc, dict):
                                san = _sanitize_disk_cfg(dc)
                                if legacy_global_time and "space_check_time" not in dc:
                                    san["space_check_time"] = legacy_global_time.strip()
                                cfg["disks"][disk_id] = san
            except (OSError, ValueError):
                pass  # 配置缺失或损坏时使用默认值
            _CONFIG = cfg
        return _CONFIG


def save_config():
    """把当前配置写入磁盘（原子替换）"""
    with _CONFIG_LOCK:
        try:
            os.makedirs(VAR_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_CONFIG, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except OSError as e:
            log("配置保存失败：%s" % e)


def update_config(body):
    """根据请求体合并更新配置。返回 (ok, message)"""
    with _CONFIG_LOCK:
        cfg = load_config()
        new_cfg = json.loads(json.dumps(cfg))  # 深拷贝，校验全部通过才生效

        disks = body.get("disks")
        if isinstance(disks, dict):
            for disk_id, dc in disks.items():
                if not disk_id or not isinstance(dc, dict):
                    continue
                cur = new_cfg["disks"].get(disk_id) or _default_disk_cfg()
                if "sleep_mode" in dc:
                    if dc["sleep_mode"] not in SLEEP_MODES:
                        return False, "休眠模式无效"
                    cur["sleep_mode"] = dc["sleep_mode"]
                if "sleep_idle_min" in dc:
                    v = _to_int(dc["sleep_idle_min"], 1, 1440)
                    if v is None:
                        return False, "空闲分钟数应在 1-1440 之间"
                    cur["sleep_idle_min"] = v
                if "sleep_daily_at" in dc:
                    if not _parse_hhmm(dc["sleep_daily_at"]):
                        return False, "定时休眠时间格式无效（应为 HH:MM）"
                    cur["sleep_daily_at"] = dc["sleep_daily_at"].strip()
                    cur["sleep_daily_last"] = ""  # 时间修改后允许当天重新触发
                if "space_enabled" in dc:
                    if not isinstance(dc["space_enabled"], bool):
                        return False, "空间清理开关应为布尔值"
                    cur["space_enabled"] = dc["space_enabled"]
                if "space_threshold_gb" in dc:
                    v = _to_float(dc["space_threshold_gb"], 1, 100000)
                    if v is None:
                        return False, "保留空间阈值应在 1-100000 GB 之间"
                    cur["space_threshold_gb"] = v
                if "space_path" in dc:
                    p = dc["space_path"]
                    if not isinstance(p, str) or (p.strip() and not os.path.isabs(p.strip())):
                        return False, "监控目录必须是绝对路径（以 / 开头）"
                    cur["space_path"] = p.strip()
                if "space_check_time" in dc:
                    if not _parse_hhmm(dc["space_check_time"]):
                        return False, "空间检查时间格式无效（应为 HH:MM）"
                    cur["space_check_time"] = dc["space_check_time"].strip()
                    cur["space_last_date"] = ""  # 时间修改后允许当天重新触发
                new_cfg["disks"][disk_id] = cur

        global _CONFIG
        _CONFIG = new_cfg
        save_config()
    return True, "配置已保存"


def _sched_update_disk(disk_id, **fields):
    """调度器更新单块硬盘的运行时状态字段并持久化"""
    with _CONFIG_LOCK:
        cfg = load_config()
        dcfg = cfg["disks"].setdefault(disk_id, _default_disk_cfg())
        dcfg.update(fields)
        save_config()


def _now_hms():
    return time.strftime("%H:%M:%S")


def _device_lock(key):
    with _DEVICE_LOCKS_GUARD:
        lock = _DEVICE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DEVICE_LOCKS[key] = lock
        return lock


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg), flush=True)


def run_cmd(cmd, timeout=HDPARM_TIMEOUT):
    """执行外部命令，返回 (returncode, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except FileNotFoundError:
        return 127, "", "命令不存在：%s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "命令执行超时（%s 秒）" % timeout
    except Exception as e:
        return 1, "", "命令执行异常：%s" % e


# ---------------------------------------------------------------- 磁盘识别

def _collect_mounts(dev, mounts):
    """递归收集设备及其子分区的挂载点"""
    for mp in dev.get("mountpoints") or []:
        if mp:
            mounts.append(mp)
    for child in dev.get("children") or []:
        _collect_mounts(child, mounts)


def list_disks():
    """列出所有支持 hdparm 的物理硬盘（SATA/USB，即 /dev/sdX）

    返回 (disks, error)：成功时 error 为 None
    """
    code, out, err = run_cmd(
        ["lsblk", "-J", "-b", "-o",
         "NAME,PATH,MODEL,SERIAL,SIZE,TYPE,TRAN,ROTA,WWN,MOUNTPOINTS"],
        timeout=LSBLK_TIMEOUT,
    )
    if code != 0:
        return None, "lsblk 执行失败：%s" % (err.strip() or "未知错误")

    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None, "lsblk 输出解析失败"

    disks = []
    seen_ids = {}
    for dev in data.get("blockdevices") or []:
        if dev.get("type") != "disk":
            continue
        name = dev.get("name") or ""
        # 仅保留 /dev/sdX 设备（SATA / USB / SCSI）；
        # NVMe、eMMC 等不支持 hdparm -y 休眠指令，不列出
        if not re.match(r"^sd[a-z]+$", name):
            continue

        serial = (dev.get("serial") or "").strip()
        wwn = (dev.get("wwn") or "").strip()
        if serial:
            disk_id = "serial:" + serial
        elif wwn:
            disk_id = "wwn:" + wwn
        else:
            # 无序列号与 WWN 时只能按设备路径标识（重新插拔后可能变化）
            disk_id = "path:" + (dev.get("path") or ("/dev/" + name))

        # 处理极少数序列号重复的情况
        base_id = disk_id
        idx = 2
        while disk_id in seen_ids:
            disk_id = "%s#%d" % (base_id, idx)
            idx += 1
        seen_ids[disk_id] = True

        mounts = []
        _collect_mounts(dev, mounts)
        is_system = any(mp in ("/", "/boot", "/boot/efi") for mp in mounts)

        disks.append({
            "id": disk_id,
            "name": name,
            "path": dev.get("path") or ("/dev/" + name),
            "model": (dev.get("model") or "").strip(),
            "serial": serial,
            "wwn": wwn,
            "size_bytes": dev.get("size") or 0,
            "transport": (dev.get("tran") or "").strip(),
            "rotational": bool(dev.get("rota")),
            "mountpoints": mounts,
            "is_system": is_system,
            "state": "unknown",
            "state_detail": "",
        })
    return disks, None


# ---------------------------------------------------------------- 状态查询

def _translate_hdparm_error(code, detail):
    """将 hdparm 常见错误翻译为明确的中文提示"""
    d = (detail or "").lower()
    if "permission denied" in d:
        return "权限不足：应用可能未以 root 身份运行（%s）" % detail
    if "bad/missing sense data" in d:
        return "USB 桥接芯片不支持状态查询（SG_IO 错误），硬盘本身可能仍支持休眠指令"
    if "hdio_drive_cmd" in d or "hdio_get_identity" in d:
        return "硬盘或 USB 桥接芯片不支持该指令：%s" % detail
    if "no such file or directory" in d:
        return "设备不存在（可能已被移除或重新枚举），请刷新列表"
    if "timeout" in d or code == 124:
        return "设备无响应：可能已休眠、正在唤醒或已断开连接"
    return detail or "hdparm 返回码 %s" % code


def query_power_state(path):
    """查询硬盘电源状态，返回 (state, detail)

    state 取值：active / idle / standby / sleeping / unknown / error
    """
    code, out, err = run_cmd(["hdparm", "-C", path])
    text = (out + "\n" + err).strip()

    if code == 0:
        m = re.search(r"drive state is:\s*(.+)", out, re.IGNORECASE)
        if m:
            raw = m.group(1).strip().lower()
            state = HDPARM_STATE_MAP.get(raw)
            if state is None:
                # 宽松前缀匹配，兼容不同版本 hdparm 的输出差异
                for prefix, st in (("active", "active"), ("idle", "idle"),
                                   ("standby", "standby"), ("sleeping", "sleeping"),
                                   ("unknown", "unknown")):
                    if raw.startswith(prefix):
                        state = st
                        break
            if state == "unknown":
                return "unknown", (
                    "该硬盘无法查询电源状态（hdparm -C 返回 unknown，"
                    "常见于部分硬盘或 USB 桥接芯片）。这不影响发送休眠指令，"
                    "休眠效果请以硬盘停转声音或系统日志为准"
                )
            if state:
                return state, ""
        return "unknown", "无法解析 hdparm 输出：" + text

    detail = text or "hdparm 返回码 %s" % code
    return "error", _translate_hdparm_error(code, detail)


def _fs_free_bytes(path):
    """返回指定路径所在文件系统的可用空间（字节）"""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _disk_free_bytes(disk):
    """取硬盘第一个挂载点所在文件系统的可用空间，失败返回 None"""
    for mp in disk.get("mountpoints") or []:
        try:
            return _fs_free_bytes(mp)
        except (OSError, AttributeError):
            continue
    return None


def get_all_disks():
    """列出全部硬盘并并发查询电源状态"""
    disks, err = list_disks()
    if err:
        return None, err

    def check(disk):
        lock = _device_lock(disk["path"])
        with lock:
            state, detail = query_power_state(disk["path"])
            disk["state"] = state
            disk["state_detail"] = detail

    if disks:
        workers = min(4, len(disks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(check, disks))
    for disk in disks:
        disk["last_sleep"] = _LAST_SLEEP.get(disk["id"])
        disk["free_bytes"] = _disk_free_bytes(disk)
    return disks, None


# ---------------------------------------------------------------- 休眠操作

def do_sleep(disk_id):
    """让指定硬盘立即休眠，并自动复查状态。返回结果字典"""
    disks, err = list_disks()
    if err:
        return {"ok": False, "level": "error", "message": err}
    disk = next((d for d in disks if d["id"] == disk_id), None)
    if disk is None:
        return {"ok": False, "level": "error",
                "message": "未找到该硬盘（设备可能已被移除或重新枚举），请刷新列表"}
    disk["last_sleep"] = _LAST_SLEEP.get(disk_id)

    path = disk["path"]
    if not re.match(r"^/dev/sd[a-z]+$", path):
        return {"ok": False, "level": "error", "message": "非法的设备路径：%s" % path}

    lock = _device_lock(path)
    with lock:
        # 先确认当前状态，避免对已休眠硬盘重复发指令
        state, detail = query_power_state(path)
        if state == "standby":
            disk["state"] = state
            return {"ok": True, "level": "info", "disk": disk,
                    "message": "硬盘已处于休眠状态，无需重复操作", "detail": ""}
        if state == "sleeping":
            disk["state"] = state
            return {"ok": True, "level": "info", "disk": disk,
                    "message": "硬盘已处于深度睡眠状态", "detail": ""}

        # 发送休眠指令
        code, out, err2 = run_cmd(["hdparm", "-y", path])
        cmd_detail = (out + "\n" + err2).strip()
        if code != 0:
            _LAST_SLEEP[disk_id] = {"time": _now_hms(), "ok": False}
            disk["last_sleep"] = _LAST_SLEEP[disk_id]
            return {"ok": False, "level": "error", "disk": disk,
                    "message": "休眠指令执行失败：" + _translate_hdparm_error(code, cmd_detail),
                    "detail": cmd_detail}

        _LAST_SLEEP[disk_id] = {"time": _now_hms(), "ok": True}
        disk["last_sleep"] = _LAST_SLEEP[disk_id]

        # 等待硬盘完成休眠后自动复查状态
        time.sleep(SLEEP_CHECK_DELAY)
        state, detail = query_power_state(path)
        if state not in ("standby", "sleeping"):
            time.sleep(SLEEP_CHECK_RETRY_DELAY)
            state, detail = query_power_state(path)

        disk["state"] = state
        disk["state_detail"] = detail

        if state == "standby":
            return {"ok": True, "level": "success", "disk": disk,
                    "message": "硬盘已成功进入休眠（standby）", "detail": cmd_detail}
        if state == "sleeping":
            return {"ok": True, "level": "success", "disk": disk,
                    "message": "硬盘已进入深度睡眠（sleeping）", "detail": cmd_detail}
        if state == "active":
            return {"ok": True, "level": "info", "disk": disk,
                    "message": "休眠指令已发送，但硬盘仍显示为运行状态。部分 USB 桥接芯片会在状态查询时唤醒硬盘，请稍后刷新确认。",
                    "detail": cmd_detail}
        if state == "idle":
            return {"ok": True, "level": "info", "disk": disk,
                    "message": "休眠指令已成功发送，当前硬盘处于空闲（idle）状态，可能正在转入休眠，请稍后刷新确认",
                    "detail": cmd_detail}
        # unknown / error：休眠指令已成功执行，但该硬盘的电源状态无法查询
        return {"ok": True, "level": "success", "disk": disk,
                "message": "休眠指令已成功发送。该硬盘的电源状态无法查询，请以硬盘停转声音或系统日志确认效果",
                "detail": cmd_detail}


# ---------------------------------------------------------------- 空间清理

# 日期命名目录：如 20260621、2026062107、20260621073030（小米摄像头录像文件夹）
_DATE_DIR_RE = re.compile(r"^\d{8,14}$")


def _dir_size(path):
    """统计目录占用空间（字节）"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def _find_date_dirs(root):
    """递归找出所有以日期命名的目录，按日期从早到晚排序（跨摄像头全局排序）"""
    found = []
    for dirpath, dirnames, _filenames in os.walk(root):
        for d in dirnames:
            if _DATE_DIR_RE.match(d):
                found.append(os.path.join(dirpath, d))
    found.sort(key=lambda p: (os.path.basename(p), p))
    return found


def _record_space_result(disk_id, manual, free_before, free, threshold_bytes, deleted, message, ok=True):
    """把清理结果写入配置（持久化，供前端展示）并返回结果"""
    result = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "manual": bool(manual),
        "ok": bool(ok),
        "free_before": free_before,
        "free_after": free,
        "threshold_bytes": threshold_bytes,
        "deleted": deleted,
        "message": message,
    }
    with _CONFIG_LOCK:
        cfg = load_config()
        dcfg = cfg["disks"].setdefault(disk_id, _default_disk_cfg())
        dcfg["space_last"] = result
        save_config()
    return result


def run_space_check(disk_id, manual=False):
    """执行一次空间清理：剩余空间低于阈值时，删除日期最早的监控文件夹

    只会删除监控目录下以纯数字日期命名（8-14 位，如 2026062107）的文件夹，
    从最早开始删，直到剩余空间不低于阈值或无文件夹可删。
    """
    cfg = load_config()
    dcfg = cfg["disks"].get(disk_id)
    if not dcfg:
        return {"ok": False, "message": "未找到该硬盘的任务配置，请先保存设置"}
    path = (dcfg.get("space_path") or "").strip()
    threshold_gb = float(dcfg.get("space_threshold_gb") or 20)
    if not path:
        return {"ok": False, "message": "尚未设置监控目录"}
    if not os.path.isabs(path) or path in ("/", "\\"):
        return {"ok": False, "message": "监控目录无效：%s" % path}
    if not os.path.isdir(path):
        return {"ok": False, "message": "监控目录不存在：%s" % path}
    threshold_bytes = int(threshold_gb * 1024 ** 3)

    try:
        free_before = _fs_free_bytes(path)
    except (OSError, AttributeError) as e:
        return {"ok": False, "message": "无法读取剩余空间：%s" % e}

    deleted = []
    free = free_before
    try:
        for d in _find_date_dirs(path):
            if free >= threshold_bytes:
                break
            size = _dir_size(d)
            shutil.rmtree(d)
            deleted.append({
                "name": os.path.relpath(d, path).replace(os.sep, "/"),
                "size": size,
            })
            free = _fs_free_bytes(path)
    except OSError as e:
        return _record_space_result(
            disk_id, manual, free_before, free, threshold_bytes, deleted,
            "清理中断：%s" % e, ok=False)

    if deleted:
        message = "已删除 %d 个最早的录像文件夹，释放 %.1f GB，当前剩余 %.1f GB" % (
            len(deleted), (free - free_before) / 1024 ** 3, free / 1024 ** 3)
        if free < threshold_bytes:
            message += "，仍低于阈值 %.0f GB（监控目录内已没有可删除的日期文件夹）" % threshold_gb
    elif free < threshold_bytes:
        message = "剩余空间 %.1f GB 低于阈值 %.0f GB，但监控目录内已没有可删除的日期文件夹" % (
            free / 1024 ** 3, threshold_gb)
    else:
        message = "剩余空间 %.1f GB，高于阈值 %.0f GB，无需清理" % (
            free / 1024 ** 3, threshold_gb)
    result = _record_space_result(
        disk_id, manual, free_before, free, threshold_bytes, deleted, message)
    return result


# ---------------------------------------------------------------- 后台调度

_AWAKE_SINCE = {}   # idle 模式：硬盘持续处于非休眠状态的起始时间戳
SCHED_INTERVAL = 20  # 调度器轮询间隔（秒）


def _scheduler_tick():
    """单次调度：处理每块硬盘的定时休眠与空间清理任务（均为每盘独立配置）"""
    cfg = load_config()
    active = {k: v for k, v in cfg["disks"].items()
              if v.get("sleep_mode") != "off" or v.get("space_enabled")}
    if not active:
        return
    disks, err = list_disks()
    if err:
        return

    now = time.localtime()
    now_min = now.tm_hour * 60 + now.tm_min
    today = time.strftime("%Y-%m-%d")

    for disk in disks:
        dcfg = active.get(disk["id"])
        if not dcfg:
            continue

        # ---- 定时休眠 ----
        mode = dcfg.get("sleep_mode")
        if mode == "idle":
            state, _ = query_power_state(disk["path"])
            if state in ("standby", "sleeping"):
                _AWAKE_SINCE.pop(disk["id"], None)
            else:
                start = _AWAKE_SINCE.setdefault(disk["id"], time.time())
                need = int(dcfg.get("sleep_idle_min") or 30) * 60
                if time.time() - start >= need:
                    r = do_sleep(disk["id"])
                    log("定时休眠 %s：%s" % (disk["path"], r.get("message")))
                    _AWAKE_SINCE.pop(disk["id"], None)
        elif mode == "daily":
            hm = _parse_hhmm(dcfg.get("sleep_daily_at"))
            if hm and now_min >= hm[0] * 60 + hm[1] and dcfg.get("sleep_daily_last") != today:
                r = do_sleep(disk["id"])
                log("定时休眠 %s：%s" % (disk["path"], r.get("message")))
                _sched_update_disk(disk["id"], sleep_daily_last=today)

        # ---- 空间清理（每盘独立检查时间，每天一次）----
        if dcfg.get("space_enabled") and dcfg.get("space_path"):
            hm = _parse_hhmm(dcfg.get("space_check_time"))
            if hm and now_min >= hm[0] * 60 + hm[1] and dcfg.get("space_last_date") != today:
                r = run_space_check(disk["id"])
                log("空间清理 %s：%s" % (disk["path"], r.get("message")))
                _sched_update_disk(disk["id"], space_last_date=today)


def _scheduler_loop():
    while True:
        try:
            _scheduler_tick()
        except Exception as e:
            log("后台调度异常：%s" % e)
        time.sleep(SCHED_INTERVAL)


def start_scheduler():
    """启动后台调度线程（定时休眠 + 空间清理）"""
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    t.start()
    return t


# ---------------------------------------------------------------- HTTP 服务

_INDEX_CACHE = {"data": None}
_ICON_CACHE = {"data": None}


def load_index():
    """加载并缓存前端页面（缓存后访问页面不再触碰磁盘）"""
    if _INDEX_CACHE["data"] is None:
        path = os.path.join(WWW_DIR, "index.html")
        try:
            with open(path, "rb") as f:
                _INDEX_CACHE["data"] = f.read()
        except OSError:
            _INDEX_CACHE["data"] = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                                    "<title>YYHomeFNAsst</title></head><body>"
                                    "<h1>YYHomeFNAsst</h1><p>前端页面缺失</p></body></html>").encode("utf-8")
    return _INDEX_CACHE["data"]


def load_icon():
    """加载并缓存应用图标（与 fpk 内 ICON.PNG 同源，页头与 favicon 使用）"""
    if _ICON_CACHE["data"] is None:
        path = os.path.join(WWW_DIR, "icon.png")
        try:
            with open(path, "rb") as f:
                _ICON_CACHE["data"] = f.read()
        except OSError:
            _ICON_CACHE["data"] = b""
    return _ICON_CACHE["data"]


class Handler(BaseHTTPRequestHandler):
    server_version = "YYHomeFNAsst/" + APP_VERSION

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", load_index())
            elif route == "/icon.png":
                icon = load_icon()
                if icon:
                    self._send(200, "image/png", icon)
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            elif route == "/api/disks":
                disks, err = get_all_disks()
                if err:
                    self._json({"ok": False, "error": err}, 500)
                else:
                    self._json({"ok": True, "disks": disks, "time": int(time.time())})
            elif route == "/api/about":
                self._json({"ok": True, "name": APP_NAME, "version": APP_VERSION, "port": PORT})
            elif route == "/api/config":
                self._json({"ok": True, "config": load_config()})
            else:
                self._json({"ok": False, "error": "接口不存在"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._safe_error(e)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/api/sleep":
                body = self._read_body()
                disk_id = str(body.get("id") or "").strip()
                if not disk_id:
                    self._json({"ok": False, "level": "error", "message": "缺少硬盘标识（id）"}, 400)
                    return
                result = do_sleep(disk_id)
                self._json(result, 200 if result.get("ok") else 400)
            elif route == "/api/config":
                ok, msg = update_config(self._read_body())
                self._json({"ok": ok, "message": msg,
                            "config": load_config() if ok else None},
                           200 if ok else 400)
            elif route == "/api/space_check":
                body = self._read_body()
                disk_id = str(body.get("id") or "").strip()
                if not disk_id:
                    self._json({"ok": False, "message": "缺少硬盘标识（id）"}, 400)
                    return
                result = run_space_check(disk_id, manual=True)
                self._json(result, 200 if result.get("ok") else 400)
            else:
                self._json({"ok": False, "error": "接口不存在"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._safe_error(e)

    def _safe_error(self, e):
        try:
            self._json({"ok": False, "error": "服务器内部错误：%s" % e}, 500)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    start_scheduler()
    log("%s v%s 服务已启动，监听端口 %d" % (APP_NAME, APP_VERSION, PORT))
    log("Web 界面：http://<NAS的IP>:%d/" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
