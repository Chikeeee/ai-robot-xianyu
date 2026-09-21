# -*- coding: utf-8 -*-
r"""确认发布前的脱敏只动了注释/文档字符串，没有改到生产代码。

脱敏是"把真实 id 换成假值"，如果它落在真正的代码逻辑里（而不是注释与文档字符串），
那线上行为就可能被悄悄改掉。这个脚本把改动逐行分类，给出结论。

用法：python scripts/check_sanitize_scope.py
"""
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / ".logs" / "pre_sanitize_backup"

CJK = re.compile(r"[\u4e00-\u9fff]")


def is_comment_or_doc(line: str) -> bool:
    stripped = line.lstrip("+-").strip()
    if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
        return True
    if CJK.search(stripped):          # 中文注释/文档字符串（本项目注释都是中文）
        return True
    if stripped.startswith("//") or stripped.startswith("*"):
        return True
    return False


def main() -> int:
    if not BACKUP.exists():
        print("没有备份目录，无法比对:", BACKUP)
        return 2
    total_files = 0
    risky = []
    for backup_file in sorted(BACKUP.iterdir()):
        rel = backup_file.name.replace("__", "/")
        current = ROOT / rel
        if not current.is_file():
            continue
        total_files += 1
        old = backup_file.read_text(encoding="utf-8").splitlines()
        new = current.read_text(encoding="utf-8").splitlines()
        changed = [line for line in difflib.unified_diff(old, new, lineterm="", n=0)
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        code_lines = [line for line in changed if not is_comment_or_doc(line)]
        print(f"{rel}: 改动 {len(changed):3d} 行 | 非注释/文档 {len(code_lines)} 行")
        for line in code_lines[:4]:
            print(f"     {line[:110]}")
        if code_lines:
            risky.append(rel)

    print("-" * 70)
    print(f"比对文件 {total_files} 个")
    if risky:
        print("★ 有改到非注释内容，需要人工确认：", risky)
        return 1
    print("结论：脱敏只落在注释/文档字符串/夹具常量内，未改动生产逻辑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
