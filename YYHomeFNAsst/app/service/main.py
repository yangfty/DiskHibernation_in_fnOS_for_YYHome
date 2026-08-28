#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YYHomeFNAsst - DiskHibernation in fnOS for YYHome
fnOS 硬盘休眠管理后端服务（零依赖，仅使用 Python3 标准库）

功能：
- 自动识别系统中的物理硬盘（lsblk，按序列号/WWN 定位，不依赖固定 /dev/sdX）
- 查询硬盘电源状态（hdparm -C）
- 让指定硬盘立即休眠（hdparm -y），执行后自动复查状态
"""

import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

APP_NAME = "YYHomeFNAsst"
APP_VERSION = "0.0.2"
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


# ---------------------------------------------------------------- HTTP 服务

_INDEX_CACHE = {"data": None}


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
            elif route == "/api/disks":
                disks, err = get_all_disks()
                if err:
                    self._json({"ok": False, "error": err}, 500)
                else:
                    self._json({"ok": True, "disks": disks, "time": int(time.time())})
            elif route == "/api/about":
                self._json({"ok": True, "name": APP_NAME, "version": APP_VERSION, "port": PORT})
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
