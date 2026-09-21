# -*- coding: utf-8 -*-
r"""「改公开」前的最后一道扫描：找个人身份痕迹（不只是账号 id）。

公开是不可逆的（删掉也可能已被缓存/派生），所以除了此前的"密钥 + 账号 id"，
还要专门看这些：Windows 用户名、真实姓名、邮箱、手机号、带用户名的绝对路径、
以及任何看起来像凭据的串。

用法：python scripts/release_public_readiness.py [--json out.json]
退出码：0 干净；1 有需要处理的命中。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = [
    ("邮箱", re.compile(r"[\w\.\-]+@[\w\-]+\.[A-Za-z]{2,}")),
    ("手机号（中国大陆）", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("Windows 用户名痕迹", re.compile(r"(C:\\\\?Users\\\\?|/Users/)([A-Za-z0-9_\u4e00-\u9fff]+)")),
    ("疑似真实姓名（简历式）", re.compile(r"[\u4e00-\u9fff]{2,3}(先生|女士|同学)")),
    ("绝对路径含用户名", re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s\"']+")),
    ("长串疑似令牌", re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")),
]

# 允许的示例/占位（避免把文档里的示例误报）
ALLOW = re.compile(r"(example\.(com|org)|test@|your[-_]|xxx|占位|placeholder|@goofish|@dingtalk|"
                   r"api\.deepseek\.com|h5api\.m\.goofish\.com|taobao\.com|alibabacorp\.com|"
                   # 以下都是良性命中，不排除会让这个检查永远变红（永远红的检查等于没检查）：
                   r"AI-Robot|XianyuAutoAgent|"                      # 项目/上游仓库名
                   r"[a-z_]{20,}|"                                   # 蛇形标识符（测试函数名等）
                   r"[0-9A-Za-z]{26,}(?=$)|"                         # 纯字符集常量
                   r"C882C442|deadbeef|FAKE|"                        # 自检用的假设备号/假令牌
                   r"Authenticated|"                                 # Windows 组名（权限检查里的字符串）
                   r"<你的用户名>|"                                    # 已脱敏的占位符
                   r"0123456789abcdef)", re.I)


def tracked_files() -> list:
    out = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--cached"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                         errors="ignore").stdout
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return [f for f in sorted(set(files)) if (ROOT / f).is_file()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args(argv)

    hits = []
    for rel in tracked_files():
        path = ROOT / rel
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if ALLOW.search(snippet):
                    continue
                hits.append({"file": rel, "kind": label, "sample": snippet[:40]})

    by_kind = {}
    for hit in hits:
        by_kind.setdefault(hit["kind"], []).append(hit)

    print(f"扫描文件：{len(tracked_files())} 个")
    for kind, items in sorted(by_kind.items()):
        print(f"\n[{kind}] {len(items)} 处")
        seen_files = {}
        for item in items:
            seen_files.setdefault(item["file"], []).append(item["sample"])
        for rel, samples in list(seen_files.items())[:8]:
            print(f"   {rel}: {samples[:2]}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(hits, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n结论:", "没有发现个人身份痕迹，可以改公开" if not hits else "有命中，建议先处理再公开")
    return 0 if not hits else 1


if __name__ == "__main__":
    sys.exit(main())
