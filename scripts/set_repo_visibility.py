# -*- coding: utf-8 -*-
r"""切换 GitHub 仓库可见性（public/private），并复核结果。

用法：
    python scripts/set_repo_visibility.py --repo ai-robot-xianyu --public --apply
    python scripts/set_repo_visibility.py --repo ai-robot-xianyu --private --apply

切换前建议先跑：
    python scripts/release_secret_audit.py
    python scripts/release_public_readiness.py
（公开是不可逆的：删掉也可能已被缓存/派生，所以先扫干净再切。）
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def token() -> str:
    proc = subprocess.run(["git", "credential", "fill"],
                          input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, timeout=30)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到凭据")


def api(method: str, path: str, tok: str, payload=None, retries: int = 4):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(path if path.startswith("http") else f"{API}{path}",
                                     data=data, method=method)
        req.add_header("Authorization", f"Bearer {tok}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ai-robot-visibility")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": exc.read().decode("utf-8", "ignore")[:300]}
        except Exception as exc:
            if attempt == retries:
                return 0, {"error": f"{type(exc).__name__}: {exc}"}
            time.sleep(min(2 ** attempt, 10))
    return 0, {"error": "unreachable"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="ai-robot-xianyu")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--public", action="store_true")
    group.add_argument("--private", action="store_true")
    parser.add_argument("--apply", action="store_true", help="真正切换（默认只查询当前状态）")
    args = parser.parse_args(argv)

    tok = token()
    status, user = api("GET", "/user", tok)
    owner = user.get("login")
    status, repo = api("GET", f"/repos/{owner}/{args.repo}", tok)
    if status != 200:
        print("取仓库失败:", status, repo.get("error"))
        return 2
    print(f"当前：{repo['html_url']} private={repo['private']}")

    want_private = bool(args.private)
    if repo["private"] == want_private:
        print("已经是目标可见性，无需切换")
        return 0
    if not args.apply:
        print(f"这是查询模式。要切到 {'private' if want_private else 'public'} 请加 --apply")
        return 0
    if not want_private:
        print("注意：改 public 不可逆（删掉也可能已被缓存/派生）。建议先跑过两道扫描。")

    status, updated = api("PATCH", f"/repos/{owner}/{args.repo}", tok, {"private": want_private})
    if status != 200:
        print("切换失败:", status, updated.get("error"))
        return 3
    print(f"已切换：private={updated['private']}  {updated['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
