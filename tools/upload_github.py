#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DiskHibeY - GitHub 上传工具（纯 GitHub REST API 实现，无需安装 git）

用法：
  # 上传到已有仓库
  python tools/upload_github.py --token ghp_xxx --repo DiskHibernation_in_fnOS_for_YYHome

  # 新建仓库并上传（公开）
  python tools/upload_github.py --token ghp_xxx --repo DiskHibernation_in_fnOS_for_YYHome --create

  # 新建私有仓库
  python tools/upload_github.py --token ghp_xxx --repo xxx --create --private

Token 也可通过环境变量 GITHUB_TOKEN 提供（--token 优先）。
需要 Token 具有 repo 权限：github.com/settings/tokens 生成。
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
API = "https://api.github.com"

# 需要上传的文件/目录（相对项目根目录）
INCLUDE = [
    "DiskHibeY",
    "tools/test_mock.py",
    "tools/upload_github.py",
    "com.yyhome.diskhibey.fpk",
]
# 排除项：目录名 / 文件扩展名
EXCLUDE_NAMES = {".git", "__pycache__", ".DS_Store", "Thumbs.db"}
EXCLUDE_EXTS = {".pyc", ".exe"}


def api(token, method, url, body=None, retries=3):
    """调用 GitHub REST API，返回 (status, json)。网络异常自动重试"""
    data = json.dumps(body).encode() if body is not None else None
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "DiskHibeY-uploader")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return e.code, {"message": raw.decode("utf-8", "replace")}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries:
                wait = attempt * 5
                print("    网络异常（%s），%d 秒后重试（%d/%d）..." % (e, wait, attempt, retries))
                time.sleep(wait)
    return 0, {"message": "网络错误：%s" % last_err}


def collect_files():
    """收集所有待上传文件（相对路径，正斜杠分隔）"""
    files = []
    for item in INCLUDE:
        p = os.path.join(ROOT, item.replace("/", os.sep))
        if os.path.isfile(p):
            files.append(item)
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in sorted(dirnames) if d not in EXCLUDE_NAMES]
                for fn in sorted(filenames):
                    if os.path.splitext(fn)[1].lower() in EXCLUDE_EXTS:
                        continue
                    rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
                    files.append(rel.replace(os.sep, "/"))
    return sorted(set(files))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                    help="GitHub Token（默认读取环境变量 GITHUB_TOKEN）")
    ap.add_argument("--owner", default="", help="仓库所有者（默认为 Token 所有者）")
    ap.add_argument("--repo", required=True, help="仓库名")
    ap.add_argument("--create", action="store_true", help="仓库不存在时自动新建")
    ap.add_argument("--private", action="store_true", help="新建仓库时设为私有")
    args = ap.parse_args()

    if not args.token:
        print("错误：未提供 Token（--token 或环境变量 GITHUB_TOKEN）")
        return 1

    # 校验 Token 并获取所有者
    code, user = api(args.token, "GET", API + "/user")
    if code != 200:
        print("错误：Token 无效（HTTP %s）：%s" % (code, user.get("message")))
        return 1
    owner = args.owner or user["login"]
    print("Token 所有者：%s" % owner)

    repo_url = "%s/repos/%s/%s" % (API, owner, args.repo)

    # 检查仓库是否存在
    code, info = api(args.token, "GET", repo_url)
    if code == 404:
        if args.create:
            print("仓库不存在，正在创建：%s/%s ..." % (owner, args.repo))
            code2, created = api(args.token, "POST", API + "/user/repos", {
                "name": args.repo,
                "description": "DiskHibeY - DiskHibernation in fnOS for YYHome：飞牛 fnOS 硬盘休眠管理应用（FPK）",
                "private": bool(args.private),
                "auto_init": False,
            })
            if code2 != 201:
                print("错误：创建仓库失败（HTTP %s）：%s" % (code2, created.get("message")))
                return 1
            print("仓库创建成功")
        else:
            print("错误：仓库 %s/%s 不存在（可加 --create 自动创建）" % (owner, args.repo))
            return 1
    elif code != 200:
        print("错误：查询仓库失败（HTTP %s）：%s" % (code, info.get("message")))
        return 1

    files = collect_files()
    print("待上传文件 %d 个" % len(files))

    ok = fail = skip = 0
    for f in files:
        full = os.path.join(ROOT, f.replace("/", os.sep))
        with open(full, "rb") as fh:
            content = fh.read()
        b64 = base64.b64encode(content).decode()

        # 查询远端文件，内容相同则跳过，不同则带 sha 更新
        code, remote = api(args.token, "GET", repo_url + "/contents/" + urllib.parse.quote(f))
        sha = None
        if code == 200 and isinstance(remote, dict):
            if remote.get("content", "").replace("\n", "") == b64:
                print("  [跳过] %s（内容相同）" % f)
                skip += 1
                continue
            sha = remote.get("sha")

        body = {"message": "更新 " + f, "content": b64}
        if sha:
            body["sha"] = sha
        code, result = api(args.token, "PUT", repo_url + "/contents/" + urllib.parse.quote(f), body)
        if code in (200, 201):
            print("  [成功] %s" % f)
            ok += 1
        else:
            msg = result.get("message") if isinstance(result, dict) else str(result)
            print("  [失败] %s（HTTP %s）：%s" % (f, code, msg))
            fail += 1

    print("\n结果：成功 %d，跳过 %d，失败 %d" % (ok, skip, fail))
    if fail == 0:
        print("仓库地址：https://github.com/%s/%s" % (owner, args.repo))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
