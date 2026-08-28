#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YYHomeFNAsst - GitHub Release 发版工具（纯 GitHub REST API 实现）

为指定版本创建 Release 并上传 fpk 安装包作为附件，供用户下载历史版本。

用法：
  python tools/release_github.py --token ghp_xxx --repo YYHomeFNAsst --version 0.0.3 \
      --fpk com.yyhome.fnasst.fpk --notes "更新说明（可省略，默认读取 manifest 的 changelog）"

  # 附带多个文件（如源码快照）
  python tools/release_github.py --token ghp_xxx --repo YYHomeFNAsst --version 0.0.3 \
      --fpk dist/YYHomeFNAsst-0.0.3.fpk --extra dist/other-file.txt

Token 也可通过环境变量 GITHUB_TOKEN 提供（--token 优先）。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
API = "https://api.github.com"


def api(token, method, url, body=None, raw=None, retries=3):
    """调用 GitHub REST API。body 为 JSON 对象，raw 为二进制附件内容"""
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": "YYHomeFNAsst-uploader",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/vnd.github+json" if raw is None else "application/vnd.github.v3+json"

    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw_resp = resp.read()
                return resp.status, (json.loads(raw_resp) if raw_resp else None)
        except urllib.error.HTTPError as e:
            raw_resp = e.read()
            try:
                return e.code, (json.loads(raw_resp) if raw_resp else None)
            except (ValueError, UnicodeDecodeError):
                return e.code, {"message": raw_resp.decode("utf-8", "replace")}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < retries:
                wait = attempt * 5
                print("    网络异常（%s），%d 秒后重试（%d/%d）..." % (e, wait, attempt, retries))
                time.sleep(wait)
    return 0, {"message": "网络错误：%s" % last_err}


def read_changelog(version):
    """从 manifest 读取指定版本的 changelog（仅当前检出版本有效，历史版本请用 --notes）"""
    try:
        with open(os.path.join(ROOT, "YYHomeFNAsst", "manifest"), "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^version\s*=\s*%s\b" % re.escape(version), text, re.M)
        c = re.search(r"^changelog\s*=\s*(.+)$", text, re.M)
        if m and c:
            return c.group(1).strip()
    except OSError:
        pass
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    ap.add_argument("--owner", default="", help="仓库所有者（默认为 Token 所有者）")
    ap.add_argument("--repo", required=True, help="仓库名")
    ap.add_argument("--version", required=True, help="版本号，如 0.0.3")
    ap.add_argument("--fpk", required=True, help="fpk 安装包路径（上传为 Release 附件）")
    ap.add_argument("--notes", default="", help="发布说明（默认读取 manifest 的 changelog）")
    ap.add_argument("--prerelease", action="store_true", help="标记为预发布版本")
    ap.add_argument("--extra", action="append", default=[], help="附加文件路径，可多次指定")
    args = ap.parse_args()

    if not args.token:
        print("错误：未提供 Token（--token 或环境变量 GITHUB_TOKEN）")
        return 1

    fpk_path = args.fpk if os.path.isabs(args.fpk) else os.path.join(ROOT, args.fpk)
    if not os.path.isfile(fpk_path):
        print("错误：fpk 文件不存在：%s" % fpk_path)
        return 1

    code, user = api(args.token, "GET", API + "/user")
    if code != 200:
        print("错误：Token 无效（HTTP %s）：%s" % (code, user.get("message")))
        return 1
    owner = args.owner or user["login"]

    tag = "v" + args.version.lstrip("vV")
    notes = args.notes or read_changelog(args.version) or ("YYHomeFNAsst V%s" % args.version)
    repo_url = "%s/repos/%s/%s" % (API, owner, args.repo)

    # 已存在同名 Release 则复用，否则创建
    code, rel = api(args.token, "GET", repo_url + "/releases/tags/" + tag)
    if code == 200:
        print("Release %s 已存在，更新附件" % tag)
        release_id = rel["id"]
        # 删除同名旧附件，便于重复执行
        for asset in rel.get("assets", []):
            api(args.token, "DELETE", repo_url + "/releases/assets/%d" % asset["id"])
    else:
        code, rel = api(args.token, "POST", repo_url + "/releases", {
            "tag_name": tag,
            "name": "V%s" % args.version,
            "body": notes,
            "prerelease": bool(args.prerelease),
        })
        if code != 201:
            print("错误：创建 Release 失败（HTTP %s）：%s" % (code, rel.get("message")))
            return 1
        release_id = rel["id"]
        print("Release %s 创建成功" % tag)

    # 上传附件
    upload_base = "https://uploads.github.com/repos/%s/%s/releases/%d/assets" % (owner, args.repo, release_id)
    ok = fail = 0
    for path in [fpk_path] + [p if os.path.isabs(p) else os.path.join(ROOT, p) for p in args.extra]:
        if not os.path.isfile(path):
            print("  [失败] %s（文件不存在）" % path)
            fail += 1
            continue
        name = os.path.basename(path)
        with open(path, "rb") as f:
            content = f.read()
        url = upload_base + "?name=" + urllib.parse.quote(name)
        code, result = api(args.token, "POST", url, raw=content)
        if code == 201:
            print("  [成功] %s（%.0f KB）" % (name, len(content) / 1024))
            ok += 1
        else:
            print("  [失败] %s（HTTP %s）：%s" % (name, code, result.get("message")))
            fail += 1

    code, rel = api(args.token, "GET", repo_url + "/releases/%d" % release_id)
    if code == 200:
        print("\n发布页：%s" % rel["html_url"])
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
