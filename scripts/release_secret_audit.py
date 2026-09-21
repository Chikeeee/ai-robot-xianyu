# -*- coding: utf-8 -*-
r"""发布前审计：扫描「将要提交的文件」里有没有密钥、凭据或真实账号痕迹。

为什么必须做：这个仓库里有真实闲鱼账号的运行痕迹（会话 id、买家 uid、设备指纹），
还有本地 .env 与凭据文件。一旦推到公开仓库，等于把账号数据与密钥一起送出去——
删掉也来不及（有缓存/派生）。所以推送前先用脚本把关，而不是凭印象。

扫描对象 = `git ls-files`（已跟踪）+ 未被 .gitignore 忽略的新文件；
被忽略的（.env / secrets/ / .logs/ / data/*.db）**不扫**，因为它们本来就进不了仓库，
但会单独提示一句"确认它们确实被忽略"。

用法：python scripts/release_secret_audit.py [--json out.json]
退出码：0 干净；1 有需要处理的命中。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 通用密钥形态
SECRET_PATTERNS = [
    ("OpenAI/DeepSeek 风格 key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("Bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("闲鱼 cookie 关键字段", re.compile(r"(_m_h5_tk|_m_h5_tk_enc|cookie2|sgcookie)\s*[=:]\s*[A-Za-z0-9_%\-]{12,}")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}")),
    ("疑似硬编码口令", re.compile(r"(password|passwd|secret|api_?key)\s*[=:]\s*['\"][^'\"]{8,}['\"]", re.I)),
]

# 允许出现的占位/示例值（避免把示例误报成泄漏）
ALLOWLIST = re.compile(r"(sk-xxx|deadbeef|your[-_]?key|示例|占位|placeholder|FAKE|fake-|<.*?>)", re.I)


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="ignore")
    return out.stdout or ""


def candidate_files() -> list:
    """需要扫描的文件：已跟踪 + 未忽略的新文件（二进制与大文件跳过）。"""
    files = []
    for line in git("ls-files").splitlines():
        if line.strip():
            files.append(line.strip())
    for line in git("ls-files", "--others", "--exclude-standard").splitlines():
        if line.strip():
            files.append(line.strip())
    return sorted(set(files))


def real_identifiers() -> dict:
    """从凭据文件与数据库里取**全部**真实标识符（不是只取第一个）。

    踩过的坑：只取第一条 `user_id` 时恰好取到模拟用的 `999999999`，于是真实买家 uid
    被漏检——审计脚本自己漏检等于没审。
    """
    ids = {}
    cred = ROOT / "secrets" / "xianyu_credentials.json"
    if cred.exists():
        try:
            data = json.loads(cred.read_text(encoding="utf-8"))
            account = data.get("account") or {}
            if account.get("unb"):
                ids.setdefault("卖家账号 unb", set()).add(str(account["unb"]))
            device = str(data.get("device_id") or "")
            if len(device) > 20:
                ids.setdefault("设备 ID", set()).add(device)
        except Exception:
            pass
    db = ROOT / "data" / "xianyu.db"
    if db.exists():
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for row in conn.execute("select distinct chat_id from messages where chat_id <> ''"):
                value = str(row[0] or "")
                # 只把**纯数字**当真实 cid（真机会话 id 是数字）；
                # "SIM-C1"/"SIM-PRICE" 是本地模拟会话名，不算隐私，否则每次审计都误报
                if value.isdigit() and len(value) >= 6:
                    ids.setdefault("会话 id", set()).add(value)
            for row in conn.execute("select distinct user_id from messages where user_id is not null"):
                value = str(row[0] or "")
                # 模拟会话用的占位 uid（999999999）与卖家自己的 uid 不算隐私
                if value.isdigit() and len(value) >= 8 and value != "999999999":
                    ids.setdefault("买家/卖家 uid", set()).add(value)
        except Exception:
            pass
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    args = parser.parse_args(argv)

    files = candidate_files()
    print(f"将要提交的文件数：{len(files)}")

    hits = []
    scanned = 0
    for rel in files:
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > args.max_bytes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        scanned += 1
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if ALLOWLIST.search(snippet):
                    continue
                # 排除"解析凭据的代码"这类假命中：形如 startswith("password=")、os.getenv("API_KEY")
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start:line_end if line_end > 0 else len(text)]
                if any(marker in line for marker in
                       ("startswith", "getenv", "environ", "credential", "header", "add_argument")):
                    continue
                hits.append({"file": rel, "kind": label, "sample": snippet[:24] + "…"})
    print(f"已扫描文本文件：{scanned}")

    ids = real_identifiers()
    print(f"真实标识符（来自凭据/数据库）："
          f"{ {k: len(v) for k, v in ids.items()} }")
    id_hits = []
    for rel in files:
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > args.max_bytes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, values in ids.items():
            for value in values:
                if value and value in text:
                    id_hits.append({"file": rel, "kind": label, "value_tail": value[-4:]})

    print("\n=== 密钥类命中 ===")
    if hits:
        for hit in hits[:20]:
            print(f"  [{hit['kind']}] {hit['file']}: {hit['sample']}")
    else:
        print("  无")
    print("\n=== 真实账号痕迹命中 ===")
    if id_hits:
        by_file = {}
        for hit in id_hits:
            by_file.setdefault(hit["file"], set()).add(hit["kind"])
        for rel, kinds in sorted(by_file.items()):
            print(f"  {rel}: {sorted(kinds)}")
    else:
        print("  无")

    print("\n=== 被忽略但必须确认的文件（不应进仓库）===")
    for rel in (".env", "secrets/xianyu_credentials.json", "data/xianyu.db", ".logs/xianyu_frames.jsonl"):
        ignored = bool(git("check-ignore", rel).strip())
        print(f"  {rel}: {'已忽略 ✓' if ignored else '★ 未忽略，危险'}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"files": len(files), "secret_hits": hits, "identity_hits": id_hits},
            ensure_ascii=False, indent=2), encoding="utf-8")

    ok = not hits and not id_hits
    print("\n审计结论:", "干净，可以推送" if ok else "有命中，必须先脱敏/移除")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
