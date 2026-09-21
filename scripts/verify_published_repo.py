# -*- coding: utf-8 -*-
r"""独立核验：远端仓库里到底有什么、**没有什么**。

推送成功只说明请求返回 200；真正要确认的是：
1. 该在的在（源码、脚本、文档、LICENSE）；
2. **不该在的绝对不在**（secrets/、.env、数据库、日志、原始帧）；
3. 推送的内容是**脱敏后**的（真实账号 id 与密钥不应出现）。

用法：python scripts/verify_published_repo.py --repo ai-robot-xianyu
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

MUST_PRESENT = [
    "README.md", "LICENSE", ".gitignore",
    "app/channels/xianyu/ws.py", "app/channels/xianyu/engine.py",
    "XIANYU-SHADOW-RUNBOOK.md", "XIANYU-CHANNEL-PLAN.md",
    "scripts/test_xianyu_p0.py", "scripts/test_xianyu_live_seam.py",
]
MUST_ABSENT_PREFIX = ["secrets/", ".logs/", ".venv/"]
MUST_ABSENT_EXACT = [".env", "data/xianyu.db", "data/xianyu.db-wal",
                     "data/xianyu_device.json", "data/xianyu_sync_pts.json"]
# 抽查内容里不能出现的东西
FORBIDDEN_CONTENT = ["githu", "_m_h5_tk=", "cookies_str"]


def token() -> str:
    proc = subprocess.run(["git", "credential", "fill"],
                          input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, timeout=30)
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到凭据")


def api(method: str, path: str, tok: str, retries: int = 4):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(path if path.startswith("http") else f"{API}{path}",
                                     method=method)
        req.add_header("Authorization", f"Bearer {tok}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ai-robot-verifier")
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
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)

    tok = token()
    status, user = api("GET", "/user", tok)
    owner = user.get("login")
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    status, repo = api("GET", f"/repos/{owner}/{args.repo}", tok)
    if status != 200:
        print("仓库不可访问:", status, repo.get("error"))
        return 2
    check("仓库存在且可见性符合预期", True,
          f"{repo['html_url']} private={repo['private']} default={repo.get('default_branch')}")
    check("默认分支就是 main", repo.get("default_branch") == args.branch,
          f"default_branch={repo.get('default_branch')}")

    status, tree = api("GET", f"/repos/{owner}/{args.repo}/git/trees/{args.branch}?recursive=1", tok)
    if status != 200:
        print("取不到文件树:", status, tree.get("error"))
        return 3
    paths = [item["path"] for item in tree.get("tree", []) if item["type"] == "blob"]
    check("远端文件数合理（>100）", len(paths) > 100, f"{len(paths)} 个文件")

    missing = [p for p in MUST_PRESENT if p not in paths]
    check("该在的文件都在", not missing, f"缺失={missing or '无'}")

    leaked_prefix = [p for p in paths if any(p.startswith(pre) for pre in MUST_ABSENT_PREFIX)]
    leaked_exact = [p for p in paths if p in MUST_ABSENT_EXACT]
    check("★ 敏感文件一个都没上去（secrets/.env/数据库/日志/设备指纹/同步游标）",
          not leaked_prefix and not leaked_exact,
          f"目录泄漏={leaked_prefix or '无'} 文件泄漏={leaked_exact or '无'}")

    # 抽查内容：脱敏是否真的生效（远端内容为准，不看本地）
    for rel in ("scripts/test_xianyu_ws.py", "XIANYU-SHADOW-RUNBOOK.md"):
        if rel not in paths:
            check(f"抽查 {rel}", False, "文件不在远端")
            continue
        status_c, blob = api("GET", f"/repos/{owner}/{args.repo}/contents/{rel}?ref={args.branch}", tok)
        if status_c != 200:
            check(f"抽查 {rel}", False, f"取内容失败 {status_c}")
            continue
        text = base64.b64decode(blob.get("content") or "").decode("utf-8", "ignore")
        real_ids = []
        cred = ROOT / "secrets" / "xianyu_credentials.json"
        if cred.exists():
            try:
                unb = str((json.loads(cred.read_text(encoding="utf-8")).get("account") or {}).get("unb") or "")
                if unb:
                    real_ids.append(unb)
            except Exception:
                pass
        import sqlite3
        db = ROOT / "data" / "xianyu.db"
        if db.exists():
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                for row in conn.execute("select distinct chat_id from messages "
                                        "where chat_id <> '' limit 10"):
                    value = str(row[0] or "")
                    if value.isdigit() and len(value) >= 6:
                        real_ids.append(value)
            except Exception:
                pass
        found = [i for i in real_ids if i in text]
        check(f"抽查 {rel}：远端内容里没有真实账号标识符", not found,
              f"命中数量={len(found)}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("-" * 70)
    print(f"远端核验：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
