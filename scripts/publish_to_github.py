# -*- coding: utf-8 -*-
r"""通过 GitHub REST API 发布仓库（本机 git 传输通道不通时用）。

背景：本机 git 走代理 127.0.0.1:7897，代理没开就连不上；绕过代理直连 github.com:443 会被
reset。但 **api.github.com 是通的**，所以用 Git Data API 直接建树+提交：
blobs → tree → commit → ref，一次调用链产生**一个完整提交**，不需要 git push。

凭据取自 Windows 凭据管理器里已存的 github.com 口令（`git credential fill`），
**全程不打印令牌**。

用法：
    python scripts/publish_to_github.py --repo ai-robot-xianyu --dry-run   # 先看要发布什么
    python scripts/publish_to_github.py --repo ai-robot-xianyu --private --apply
"""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
MAX_FILE_BYTES = 5 * 1024 * 1024


def get_token() -> str:
    """从凭据管理器取 github.com 口令（不落盘、不打印）。"""
    proc = subprocess.run(["git", "credential", "fill"],
                          input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, timeout=30)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到 github.com 凭据（Windows 凭据管理器里没有？）")


def api(method: str, path: str, token: str, payload=None, timeout: float = 60.0,
        retries: int = 4):
    """带重试的 API 调用。

    本机到 api.github.com 的连接会偶发被对端断开（RemoteDisconnected）——118 个 blob 顺序上传时
    只要有一次抖动就会前功尽弃，所以必须重试；4xx（除限流）不重试，避免把真错误当抖动重试。
    """
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ai-robot-publisher")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            last = (exc.code, {"error": detail[:400]})
            throttled = exc.code in (403, 429) and ("rate limit" in detail.lower()
                                                    or "secondary" in detail.lower())
            if exc.code < 500 and not throttled:
                return last
            wait = float(exc.headers.get("Retry-After") or 0) or min(2 ** attempt, 15)
            print(f"    [{method} {path}] HTTP {exc.code}，{wait:.0f}s 后重试（{attempt}/{retries}）")
            time.sleep(wait)
        except Exception as exc:  # 连接被重置/超时/对端断开
            last = (0, {"error": f"{type(exc).__name__}: {exc}"})
            wait = min(2 ** attempt, 15)
            print(f"    [{method} {path}] {type(exc).__name__}，{wait}s 后重试（{attempt}/{retries}）")
            time.sleep(wait)
    return last


def published_files() -> list:
    """要发布的文件：已跟踪 + 未忽略的新文件，且磁盘上确实存在。"""
    def run(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="ignore").stdout or ""

    names = set()
    for line in (run("ls-files") + "\n" + run("ls-files", "--others", "--exclude-standard")).splitlines():
        rel = line.strip()
        if rel:
            names.add(rel)
    out = []
    for rel in sorted(names):
        path = ROOT / rel
        if path.is_file():
            out.append((rel, path))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="ai-robot-xianyu")
    parser.add_argument("--private", action="store_true", default=True)
    parser.add_argument("--public", dest="private", action="store_false")
    parser.add_argument("--description", default="AI-Robot（FastAPI + LangChain + RAG）之上移植的闲鱼 7×24 自动值守通道")
    parser.add_argument("--message", default="feat: 闲鱼 7×24 值守通道（协议层/影子模式/面板告警/灰度自动发送）")
    parser.add_argument("--apply", action="store_true", help="真正发布（默认只做 dry-run）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    files = published_files()
    total = sum(p.stat().st_size for _, p in files)
    big = [(rel, p.stat().st_size) for rel, p in files if p.stat().st_size > MAX_FILE_BYTES]
    print(f"待发布文件：{len(files)} 个，合计 {total/1024:.0f} KB")
    if big:
        print(f"超过 {MAX_FILE_BYTES//1024//1024}MB 的文件（会被跳过）：{big}")
    for rel, _ in files[:12]:
        print(f"   {rel}")
    if len(files) > 12:
        print(f"   …其余 {len(files)-12} 个")

    token = get_token()
    status, user = api("GET", "/user", token)
    if status != 200:
        print("令牌不可用:", status, user.get("error"))
        return 2
    owner = user["login"]
    print(f"\n账号：{owner}（令牌有效）")
    scopes = ""
    status2, repo_info = api("GET", f"/repos/{owner}/{args.repo}", token)
    if status2 == 200:
        print(f"同名仓库已存在：{repo_info.get('html_url')}（将复用并新增一个提交）")
    else:
        print(f"仓库 {owner}/{args.repo} 尚不存在（将创建，private={args.private}）")

    if not args.apply:
        print("\n这是 dry-run：确认后加 --apply 才会真正创建/推送。")
        return 0

    # 1) 建仓
    if status2 != 200:
        status3, created = api("POST", "/user/repos", token, {
            "name": args.repo, "description": args.description, "private": args.private,
            "auto_init": False, "has_issues": True, "has_wiki": False})
        if status3 not in (201, 200):
            print("建仓失败:", status3, created.get("error"))
            return 3
        print(f"已创建：{created.get('html_url')}（private={created.get('private')}）")
        time.sleep(2)
    else:
        created = repo_info

    # 2) 空仓库必须先有一个提交/分支，否则 GitHub 对 git data 接口回 409「Git Repository is empty」。
    #    用 Contents API 先写一个 README 引导出初始分支，稍后再用单次根提交覆盖它（历史保持干净）。
    status0, ref0 = api("GET", f"/repos/{owner}/{args.repo}/git/ref/heads/main", token)
    if status0 != 200:
        print("仓库还是空的 → 先用 Contents API 引导出初始分支")
        readme = next((rel for rel, _ in files if rel.lower().startswith("readme")), None)
        boot_rel = readme or files[0][0]
        boot_path = ROOT / boot_rel
        status_boot, boot = api("PUT", f"/repos/{owner}/{args.repo}/contents/{boot_rel}", token, {
            "message": "chore: 初始化仓库", "branch": "main",
            "content": base64.b64encode(boot_path.read_bytes()).decode("ascii")})
        if status_boot not in (201, 200):
            print("引导初始分支失败:", status_boot, boot.get("error"))
            return 8
        print(f"  已引导: {boot_rel}")
        time.sleep(2)

    # 3) blobs
    tree_entries = []
    for index, (rel, path) in enumerate(files, 1):
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            print(f"  跳过（过大）: {rel}")
            continue
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        status4, blob = api("POST", f"/repos/{owner}/{args.repo}/git/blobs", token,
                            {"content": content, "encoding": "base64"})
        if status4 not in (201, 200):
            print(f"  blob 失败 {rel}: {status4} {blob.get('error')}")
            return 4
        mode = "100755" if rel.endswith((".sh", ".ps1")) else "100644"
        tree_entries.append({"path": rel, "mode": mode, "type": "blob", "sha": blob["sha"]})
        if index % 25 == 0:
            print(f"  已上传 {index}/{len(files)}")

    # 3) tree → commit → ref
    status5, tree = api("POST", f"/repos/{owner}/{args.repo}/git/trees", token,
                        {"tree": tree_entries})
    if status5 not in (201, 200):
        print("建 tree 失败:", status5, tree.get("error"))
        return 5

    # 用**单个根提交**承载全部文件：不挂父提交，最后强推覆盖引导提交，历史干净（一次提交）
    status7, commit = api("POST", f"/repos/{owner}/{args.repo}/git/commits", token,
                          {"message": args.message, "tree": tree["sha"], "parents": []})
    if status7 not in (201, 200):
        print("建 commit 失败:", status7, commit.get("error"))
        return 6

    status8, updated = api("PATCH", f"/repos/{owner}/{args.repo}/git/refs/heads/main", token,
                           {"sha": commit["sha"], "force": True})
    if status8 not in (201, 200):
        print("更新分支失败:", status8, updated.get("error"))
        return 7

    print("\n发布完成：")
    print(f"  仓库   : {created.get('html_url')}")
    print(f"  提交   : {commit['sha'][:12]}（{len(tree_entries)} 个文件）")
    print(f"  可见性 : {'private' if created.get('private') else 'public'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
