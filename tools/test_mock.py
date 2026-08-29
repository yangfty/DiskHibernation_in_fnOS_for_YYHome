#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YYHomeFNAsst 本地冒烟测试（在开发机上运行，无需真实磁盘）
通过模拟 lsblk / hdparm 的输出，验证后端完整逻辑链与 HTTP 接口。
用法：python tools/test_mock.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

SERVICE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "YYHomeFNAsst", "app", "service"))
sys.path.insert(0, SERVICE_DIR)

# 配置写入独立的临时目录，避免污染本机 /tmp
TEST_VAR = tempfile.mkdtemp(prefix="fnasst_cfg_")
os.environ["YYHOMEFNASST_VAR"] = TEST_VAR

import main  # noqa: E402

# ---------------------------------------------------------------- 模拟数据

MOCK_STATE = {
    "/dev/sda": "active",    # SATA 系统盘，运行中
    "/dev/sdb": "standby",   # USB 盘，已休眠
    "/dev/sdc": "error",     # USB 桥接不支持状态查询（rc=1），但休眠指令可成功
    "/dev/sdd": "active",    # 与 sdb 序列号重复的盘
    "/dev/sdf": "idle",      # idle (low rpm)
}

# 这些硬盘的 hdparm -C 始终返回 unknown（rc=0），
# 模拟 WD50NDZW 等硬盘/桥接芯片：状态查不到，但休眠指令正常
ALWAYS_UNKNOWN = {"/dev/sde"}

LSBLK_JSON = {
    "blockdevices": [
        {
            "name": "sda", "path": "/dev/sda", "model": "WDC WD40EFRX-68N32N0",
            "serial": "WD-WX11A0B12345", "size": 4000787030016, "type": "disk",
            "tran": "sata", "rota": True, "wwn": "0x50014ee20c123456",
            "mountpoints": [None],
            "children": [
                {"name": "sda1", "path": "/dev/sda1", "size": 214748364800, "type": "part",
                 "tran": None, "rota": True, "mountpoints": ["/"], "children": []},
                {"name": "sda3", "path": "/dev/sda3", "size": 536870912, "type": "part",
                 "tran": None, "rota": True, "mountpoints": ["/boot/efi"], "children": []},
            ],
        },
        {
            "name": "sdb", "path": "/dev/sdb", "model": "Samsung Portable SSD T7",
            "serial": "S6GZNS0W123456", "size": 1000204886016, "type": "disk",
            "tran": "usb", "rota": False, "wwn": None, "mountpoints": [None],
            "children": [
                {"name": "sdb1", "path": "/dev/sdb1", "size": 1000204140544, "type": "part",
                 "tran": None, "rota": False, "mountpoints": ["/vol2/usb1"], "children": []},
            ],
        },
        {
            "name": "sdc", "path": "/dev/sdc", "model": "ST8000DM004-2CX188",
            "serial": "", "size": 8001563222016, "type": "disk",
            "tran": "sata", "rota": True, "wwn": "", "mountpoints": [None],
            "children": [
                {"name": "sdc1", "path": "/dev/sdc1", "size": 8001559529472, "type": "part",
                 "tran": None, "rota": True, "mountpoints": ["/vol3"], "children": []},
            ],
        },
        {
            "name": "sdd", "path": "/dev/sdd", "model": "Samsung Portable SSD T7",
            "serial": "S6GZNS0W123456", "size": 1000204886016, "type": "disk",
            "tran": "usb", "rota": False, "wwn": None, "mountpoints": [None],
        },
        {
            # 用户实际遇到的情况：hdparm -C 返回 unknown，但 hdparm -y 能正常休眠
            "name": "sde", "path": "/dev/sde", "model": "WDC WD50NDZW-11BCSS1",
            "serial": "WD-WX11A0B99999", "size": 5000981077504, "type": "disk",
            "tran": "usb", "rota": True, "wwn": None, "mountpoints": [None],
            "children": [
                {"name": "sde1", "path": "/dev/sde1", "size": 5000979992576, "type": "part",
                 "tran": None, "rota": True, "mountpoints": ["/vol4/wd"], "children": []},
            ],
        },
        {
            "name": "sdf", "path": "/dev/sdf", "model": "WDC WD60EZAZ-11SFB0",
            "serial": "WD-WX11A0B77777", "size": 6001175126016, "type": "disk",
            "tran": "sata", "rota": True, "wwn": "0x50014ee20c765432", "mountpoints": [None],
            "children": [
                {"name": "sdf1", "path": "/dev/sdf1", "size": 6001170526208, "type": "part",
                 "tran": None, "rota": True, "mountpoints": ["/vol5"], "children": []},
            ],
        },
        # 以下设备应被排除
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "model": "Samsung SSD 970", "serial": "S4EWNX0N123",
         "size": 512110190592, "type": "disk", "tran": "nvme", "rota": False, "mountpoints": [None]},
        {"name": "loop0", "path": "/dev/loop0", "size": 104857600, "type": "loop",
         "tran": None, "rota": False, "mountpoints": [None]},
    ]
}


def mock_run_cmd(cmd, timeout=10):
    prog = cmd[0] if cmd else ""
    if prog == "lsblk":
        return 0, json.dumps(LSBLK_JSON), ""
    if prog == "hdparm":
        path = cmd[2] if len(cmd) > 2 else ""
        if cmd[1] == "-C":
            if path in ALWAYS_UNKNOWN:
                # 与真实场景一致：stderr 带 SG_IO 告警，stdout 仍输出 unknown，退出码 0
                return 0, "\n%s:\n drive state is:  unknown\n" % path, \
                    "SG_IO: bad/missing sense data, sb[]:  70 00 05 00 00 00 00 0a 00 00 00 00 20 00 00 00"
            st = MOCK_STATE.get(path)
            if st is None:
                return 1, "", "No such file or directory"
            if st == "error":
                return 1, "", "SG_IO: bad/missing sense data, sb[]:  70 00 05 00 00 00 00 0a 00 00 00 00 20 00 00 00"
            if st == "idle":
                return 0, "\n%s:\n drive state is:  idle (low rpm)\n" % path, ""
            return 0, "\n%s:\n drive state is:  %s\n" % (path, "active/idle" if st == "active" else st), ""
        if cmd[1] == "-y":
            if path not in MOCK_STATE and path not in ALWAYS_UNKNOWN:
                return 1, "", "No such file or directory"
            MOCK_STATE[path] = "standby"
            return 0, "\nissuing standby command\n", ""
    return 127, "", "mock: unknown command %s" % prog


main.run_cmd = mock_run_cmd
main.SLEEP_CHECK_DELAY = 0.1
main.SLEEP_CHECK_RETRY_DELAY = 0.1

# ---------------------------------------------------------------- 测试工具

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [通过] %s" % name)
    else:
        FAIL += 1
        print("  [失败] %s %s" % (name, extra))


def http(method, path, body=None):
    url = "http://127.0.0.1:18327%s" % path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ---------------------------------------------------------------- 用例

def test_list_and_state():
    print("\n== 磁盘识别与状态查询 ==")
    disks, err = main.get_all_disks()
    check("无错误", err is None, str(err))
    check("识别 6 块 sdX 硬盘（排除 NVMe/loop）", len(disks) == 6, "got %d" % len(disks))
    by_name = {d["name"]: d for d in disks}
    check("sda 识别为系统盘", by_name["sda"]["is_system"] is True)
    check("sda 状态 active", by_name["sda"]["state"] == "active")
    check("sda 挂载点收集", "/" in by_name["sda"]["mountpoints"] and "/boot/efi" in by_name["sda"]["mountpoints"])
    check("sdb 状态 standby", by_name["sdb"]["state"] == "standby")
    check("sdb 接口 usb", by_name["sdb"]["transport"] == "usb")
    check("sdc 无序列号时按路径标识", by_name["sdc"]["id"] == "path:/dev/sdc")
    check("sdc 状态 error 且带中文错误说明",
          by_name["sdc"]["state"] == "error" and "USB" in by_name["sdc"]["state_detail"])
    check("sdd 序列号重复时加 #2 后缀", by_name["sdd"]["id"] == "serial:S6GZNS0W123456#2")
    check("sda 容量透传", by_name["sda"]["size_bytes"] == 4000787030016)
    # 用户实际遇到的 WD 盘场景
    check("sde（WD 盘 -C 返回 unknown）状态为 unknown",
          by_name["sde"]["state"] == "unknown")
    check("sde 带中文解释说明", "不影响发送休眠指令" in by_name["sde"]["state_detail"],
          by_name["sde"]["state_detail"])
    check("sdf 识别 idle (low rpm) 为空闲状态", by_name["sdf"]["state"] == "idle")
    return by_name


def test_sleep(by_name):
    print("\n== 休眠操作 ==")
    r = main.do_sleep(by_name["sdb"]["id"])
    check("已休眠的盘提示无需重复操作", r["ok"] and "无需重复" in r["message"], str(r))

    r = main.do_sleep(by_name["sdc"]["id"])
    check("状态查询失败但休眠指令仍可成功（USB 场景）",
          r["ok"] and r["disk"]["state"] == "standby" and r["level"] == "success", str(r))

    # 用户实际遇到的 WD 盘场景：状态一直 unknown，但休眠指令成功
    r = main.do_sleep(by_name["sde"]["id"])
    check("WD 盘（状态 unknown）休眠指令成功且提示明确",
          r["ok"] and r["level"] == "success" and "已成功发送" in r["message"]
          and r["disk"]["state"] == "unknown", str(r))
    check("WD 盘休眠结果记录上次指令时间",
          r["disk"].get("last_sleep", {}).get("ok") is True, str(r.get("disk")))

    r = main.do_sleep(by_name["sdf"]["id"])
    check("空闲（idle）状态的硬盘可正常发送休眠指令",
          r["ok"] and r["disk"]["state"] == "standby" and r["level"] == "success", str(r))

    r = main.do_sleep("serial:NOT-EXIST")
    check("不存在的硬盘给出明确提示", not r["ok"] and "未找到" in r["message"], str(r))

    # 上次休眠记录应出现在列表接口数据中
    disks2, _ = main.get_all_disks()
    sde2 = next((d for d in disks2 if d["name"] == "sde"), None)
    check("刷新列表时附带上次休眠指令记录",
          sde2 is not None and sde2.get("last_sleep", {}).get("ok") is True)


def test_http():
    print("\n== HTTP 接口 ==")
    server = main.ThreadingHTTPServer(("127.0.0.1", 18327), main.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # 首页
        with urllib.request.urlopen("http://127.0.0.1:18327/", timeout=5) as resp:
            html = resp.read().decode()
        check("GET / 返回前端页面", "YYHomeFNAsst" in html and "立即休眠" in html)
        check("前端页面包含版本号显示", "V0.1.0" in html and "verBadge" in html)
        check("前端页面包含定时任务入口", "定时任务" in html and "空间清理" in html and "tmSpacePath" in html)
        check("前端页面包含每盘独立的检查时间设置", "tmCheckAt" in html and "globalModal" not in html)
        check("前端页面包含弹窗背景滚动锁定", "modal-open" in html and "overscroll-behavior" in html)

        # 版本接口
        status, data = http("GET", "/api/about")
        check("GET /api/about 返回版本 0.1.0", status == 200 and data.get("version") == "0.1.0")

        # 磁盘列表
        status, data = http("GET", "/api/disks")
        check("GET /api/disks 返回 200", status == 200)
        check("接口返回 6 块硬盘", data["ok"] and len(data["disks"]) == 6)

        # 休眠 sda
        status, data = http("POST", "/api/sleep", {"id": "serial:WD-WX11A0B12345"})
        check("POST /api/sleep 成功休眠并自动复查", status == 200 and data["ok"] and data["disk"]["state"] == "standby")

        # 休眠状态无法查询的 WD 盘
        status, data = http("POST", "/api/sleep", {"id": "serial:WD-WX11A0B99999"})
        check("WD 盘（状态 unknown）休眠接口返回成功提示",
              status == 200 and data["level"] == "success"
              and data["disk"]["last_sleep"]["ok"] is True, str(data))

        # 异常分支
        status, data = http("POST", "/api/sleep", {"id": "xxx"})
        check("休眠不存在的硬盘返回 400 + 中文提示", status == 400 and "未找到" in data["message"])
        status, data = http("POST", "/api/sleep", {})
        check("缺少 id 返回 400", status == 400 and "缺少" in data["message"])
        status, data = http("GET", "/api/nonexist")
        check("未知接口返回 404", status == 404)

        # 配置接口
        status, data = http("GET", "/api/config")
        check("GET /api/config 返回配置", status == 200 and data["ok"]
              and "disks" in data["config"])
        status, data = http("POST", "/api/config",
                            {"disks": {"serial:HTTPTEST": {"sleep_mode": "daily", "sleep_daily_at": "03:30",
                                                           "space_check_time": "01:30"}}})
        check("POST /api/config 保存硬盘任务", status == 200 and data["ok"]
              and data["config"]["disks"]["serial:HTTPTEST"]["sleep_daily_at"] == "03:30"
              and data["config"]["disks"]["serial:HTTPTEST"]["space_check_time"] == "01:30", str(data))
        status, data = http("POST", "/api/config", {"disks": {"serial:HTTPTEST": {"sleep_mode": "bad"}}})
        check("POST /api/config 非法模式返回 400", status == 400 and not data["ok"])
        status, data = http("POST", "/api/space_check", {})
        check("POST /api/space_check 缺少 id 返回 400", status == 400 and "缺少" in data["message"])
    finally:
        server.shutdown()
        server.server_close()


def test_config():
    print("\n== 任务配置 ==")
    ok, msg = main.update_config({"disks": {"serial:TIME": {"space_check_time": "00:30"}}})
    check("每盘独立的空间检查时间可设置",
          ok and main.load_config()["disks"]["serial:TIME"]["space_check_time"] == "00:30", msg)

    ok, msg = main.update_config({"disks": {"x": {"space_check_time": "25:00"}}})
    check("非法时间被拒绝", not ok, msg)
    ok, msg = main.update_config({"disks": {"x": {"sleep_mode": "bad"}}})
    check("非法休眠模式被拒绝", not ok)
    ok, msg = main.update_config({"disks": {"x": {"space_path": "relative/path"}}})
    check("相对路径监控目录被拒绝", not ok)
    ok, msg = main.update_config({"disks": {"x": {"sleep_idle_min": 0}}})
    check("空闲分钟数越界被拒绝", not ok)

    ok, msg = main.update_config({"disks": {"serial:WD-WX11A0B77777": {
        "sleep_mode": "daily", "sleep_daily_at": "01:05"}}})
    cfg = main.load_config()["disks"]["serial:WD-WX11A0B77777"]
    check("硬盘定时休眠配置可保存", ok and cfg["sleep_mode"] == "daily"
          and cfg["sleep_daily_at"] == "01:05", msg)
    check("未提交字段回退默认值", cfg["sleep_idle_min"] == 30
          and cfg["space_threshold_gb"] == 20 and cfg["space_enabled"] is False
          and cfg["space_check_time"] == "00:00")

    # 配置持久化：清空内存缓存后重新加载
    main._CONFIG = None
    cfg2 = main.load_config()["disks"]["serial:WD-WX11A0B77777"]
    check("配置持久化到磁盘并可重新加载",
          cfg2["sleep_mode"] == "daily"
          and main.load_config()["disks"]["serial:TIME"]["space_check_time"] == "00:30")

    # 旧版配置迁移：全局 space_check_time 下放到没有单独设置检查时间的硬盘
    legacy = {"version": 1, "space_check_time": "02:30", "disks": {
        "serial:MIG-A": {"sleep_mode": "off"},                      # 应继承全局 02:30
        "serial:MIG-B": {"sleep_mode": "off", "space_check_time": "05:00"},  # 保留自己的 05:00
    }}
    with open(main.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(legacy, f, ensure_ascii=False)
    main._CONFIG = None
    migrated = main.load_config()["disks"]
    check("旧全局检查时间迁移到未单独设置的硬盘", migrated["serial:MIG-A"]["space_check_time"] == "02:30",
          str(migrated.get("serial:MIG-A")))
    check("硬盘单独设置的检查时间在迁移后保留", migrated["serial:MIG-B"]["space_check_time"] == "05:00")
    check("迁移后顶层不再有全局检查时间字段", "space_check_time" not in main.load_config())


def _make_mock_camera_dir():
    """构造模拟的小米摄像头目录：两个摄像头 SN，各含日期文件夹"""
    root = tempfile.mkdtemp(prefix="fnasst_space_")
    for cam, dates in (("94F8275876BE", ["2026062107", "2026062108", "2026062109", "notadate"]),
                       ("AABBCCDDEEFF", ["2026062105", "2026062106"])):
        for d in dates:
            p = os.path.join(root, cam, d)
            os.makedirs(p)
            with open(os.path.join(p, "video.mp4"), "wb") as f:
                f.write(b"x" * 4096)
    return root


def test_space_check(by_name):
    print("\n== 空间清理 ==")
    GB = 1024 ** 3
    root = _make_mock_camera_dir()
    disk_id = by_name["sdf"]["id"]

    # 模拟剩余空间：初始 18G（低于阈值 20G），每删除一个文件夹释放 12G
    fake_free = {"val": 18 * GB}
    orig_free = main._fs_free_bytes
    orig_rmtree = main.shutil.rmtree

    def fake_rmtree(p, *a, **kw):
        orig_rmtree(p, *a, **kw)
        fake_free["val"] += 12 * GB

    main._fs_free_bytes = lambda p: fake_free["val"]
    main.shutil.rmtree = fake_rmtree
    try:
        ok, _ = main.update_config({"disks": {disk_id: {
            "space_enabled": True, "space_threshold_gb": 20, "space_path": root}}})
        check("空间清理配置可保存", ok)

        r = main.run_space_check(disk_id, manual=True)
        check("空间不足时删除最早日期文件夹", r["ok"] and len(r["deleted"]) == 1, str(r))
        check("删除的是全局最早的文件夹（第二台摄像头 2026062105）",
              r["deleted"][0]["name"].replace("\\", "/") == "AABBCCDDEEFF/2026062105", str(r))
        check("非日期命名的文件夹不被删除",
              os.path.isdir(os.path.join(root, "94F8275876BE", "notadate")))
        check("较新的日期文件夹被保留",
              os.path.isdir(os.path.join(root, "94F8275876BE", "2026062109")))
        check("删除结果写入配置", main.load_config()["disks"][disk_id]["space_last"]["ok"] is True)

        # 剩余空间充足时不应删除任何文件夹
        fake_free["val"] = 100 * GB
        r = main.run_space_check(disk_id, manual=True)
        check("空间充足时不删除", r["ok"] and len(r["deleted"]) == 0 and "无需清理" in r["message"], str(r))

        # 全部删完仍不足时给出明确提示
        fake_free["val"] = 1 * GB
        main._fs_free_bytes = lambda p: 1 * GB  # 恒为 1G，删完也不够
        r = main.run_space_check(disk_id, manual=True)
        check("删完仍不足时提示明确",
              r["ok"] and "仍低于阈值" in r["message"], str(r))
        check("全部日期文件夹已被删除",
              not os.path.exists(os.path.join(root, "94F8275876BE", "2026062107")))

        # 未设置监控目录时的提示
        main.update_config({"disks": {by_name["sda"]["id"]: {"space_enabled": True}}})
        r = main.run_space_check(by_name["sda"]["id"])
        check("未设置监控目录时给出提示", not r["ok"] and "尚未设置监控目录" in r["message"], str(r))
    finally:
        main._fs_free_bytes = orig_free
        main.shutil.rmtree = orig_rmtree
        shutil.rmtree(root, ignore_errors=True)
        main.update_config({"disks": {disk_id: {"space_enabled": False, "space_path": ""}}})


def test_scheduler(by_name):
    print("\n== 后台调度 ==")
    today = time.strftime("%Y-%m-%d")
    disk_id = by_name["sdd"]["id"]  # sdd 与 sdb 序列号相同，用其独立 id

    # ---- 每天定时休眠 ----
    MOCK_STATE["/dev/sdd"] = "active"
    main.update_config({"disks": {disk_id: {"sleep_mode": "daily", "sleep_daily_at": "00:00"}}})
    main._scheduler_tick()
    check("到达设定时间后自动休眠", MOCK_STATE["/dev/sdd"] == "standby")
    check("执行日期被记录（避免重复执行）",
          main.load_config()["disks"][disk_id]["sleep_daily_last"] == today)

    MOCK_STATE["/dev/sdd"] = "active"
    main._scheduler_tick()
    check("同一天不会重复执行", MOCK_STATE["/dev/sdd"] == "active")

    # ---- 空闲后自动休眠 ----
    main.update_config({"disks": {disk_id: {"sleep_mode": "idle", "sleep_idle_min": 30}}})
    main._AWAKE_SINCE.clear()
    main._scheduler_tick()
    check("空闲计时的首个周期不发送指令", MOCK_STATE["/dev/sdd"] == "active")

    main._AWAKE_SINCE[disk_id] = time.time() - 31 * 60
    main._scheduler_tick()
    check("持续非休眠超过设定时长后自动休眠", MOCK_STATE["/dev/sdd"] == "standby")

    main._AWAKE_SINCE[disk_id] = time.time() - 31 * 60
    main._scheduler_tick()
    check("已休眠的盘重置计时且不重复发指令", MOCK_STATE["/dev/sdd"] == "standby"
          and disk_id not in main._AWAKE_SINCE)

    # ---- 空间清理调度（每盘独立检查时间）----
    GB = 1024 ** 3
    root = _make_mock_camera_dir()
    fake_free = {"val": 18 * GB}
    orig_free = main._fs_free_bytes
    main._fs_free_bytes = lambda p: fake_free["val"]
    try:
        # 检查时间设为 23:59（必然未到），不应执行
        main.update_config({"disks": {disk_id: {"sleep_mode": "off",
                                                "space_enabled": True,
                                                "space_threshold_gb": 20,
                                                "space_path": root,
                                                "space_check_time": "23:59"}}})
        main._scheduler_tick()
        check("未到该硬盘检查时间时不执行空间清理",
              main.load_config()["disks"][disk_id].get("space_last_date") != today)

        # 改为 00:00（必然已过），应执行一次
        main.update_config({"disks": {disk_id: {"space_check_time": "00:00"}}})
        main._scheduler_tick()
        dcfg = main.load_config()["disks"][disk_id]
        check("到达该硬盘检查时间后自动执行空间清理",
              dcfg.get("space_last_date") == today and dcfg.get("space_last", {}).get("ok") is True)
        main._scheduler_tick()
        check("同一天不会重复执行空间清理",
              main.load_config()["disks"][disk_id]["space_last_date"] == today)
    finally:
        main._fs_free_bytes = orig_free
        shutil.rmtree(root, ignore_errors=True)
        main.update_config({"disks": {disk_id: {"sleep_mode": "off", "space_enabled": False,
                                                "space_path": ""}}})


if __name__ == "__main__":
    print("YYHomeFNAsst 冒烟测试（模拟环境）")
    by_name = test_list_and_state()
    test_sleep(by_name)
    test_config()
    test_space_check(by_name)
    test_scheduler(by_name)
    test_http()
    shutil.rmtree(TEST_VAR, ignore_errors=True)
    print("\n结果：%d 通过，%d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
