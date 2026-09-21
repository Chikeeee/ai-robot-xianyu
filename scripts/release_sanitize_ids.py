# -*- coding: utf-8 -*-
r"""发布前脱敏：把仓库里出现过的**真实账号标识符**一致替换成同形状的假值。

为什么要自动化：真实闲鱼会话 id / 买家 uid / 卖家 unb 散落在 18 个文件里（含测试夹具、
文档示例、诊断脚本常量）。手工改必漏，而"漏一个就等于把账号数据推到公开仓库"。

替换是**一致**的：同一个真实值在任何地方都换成同一个假值（长度也保持一致），
所以测试里"夹具值 == 断言值"的关系不变，脱敏后门禁仍然全绿。

用法：
    python scripts/release_sanitize_ids.py           # 预览（不改文件）
    python scripts/release_sanitize_ids.py --apply   # 真正替换
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRED = ROOT / "secrets" / "xianyu_credentials.json"
DB = ROOT / "data" / "xianyu.db"

# 这些不是隐私，不替换（模拟会话占位、公开常量）
KEEP = {"999999999"}


def collect_real_ids() -> list:
    values = []
    if CRED.exists():
        try:
            data = json.loads(CRED.read_text(encoding="utf-8"))
            unb = str((data.get("account") or {}).get("unb") or "")
            if unb:
                values.append(unb)
        except Exception:
            pass
    if DB.exists():
        try:
            conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            for row in conn.execute("select distinct chat_id from messages where chat_id <> ''"):
                value = str(row[0] or "")
                # 只认**纯数字**的会话 id：真机的 cid 是数字，
                # 而 "SIM-C1"/"SIM-PRICE" 这类是本地模拟会话名，替换它会误伤自测
                # （本机就因此把 service.py 里 simulate 的默认 chat_id 改坏过）
                if value.isdigit() and len(value) >= 6:
                    values.append(value)
            for row in conn.execute("select distinct user_id from messages where user_id is not null"):
                value = str(row[0] or "")
                if value.isdigit() and len(value) >= 8:
                    values.append(value)
        except Exception:
            pass
    out = []
    for value in values:
        if value and value not in KEEP and value not in out:
            out.append(value)
    return out


def fake_for(value: str, index: int) -> str:
    """生成同长度、明显是假值的替身（数字 → 1000...N，保证不与真值相同）。"""
    if value.isdigit():
        body = "1" + "0" * max(0, len(value) - 2) + str(index + 1)[-1]
        return body[:len(value)].ljust(len(value), "0")
    return "FAKE" + str(index + 1)


def candidate_files() -> list:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="ignore").stdout
    others = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="ignore").stdout
    files = [line.strip() for line in (out + "\n" + others).splitlines() if line.strip()]
    return sorted(set(files))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写回（默认只预览）")
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    args = parser.parse_args(argv)

    real_ids = collect_real_ids()
    mapping = {value: fake_for(value, i) for i, value in enumerate(real_ids)}
    print(f"待脱敏的真实标识符：{len(mapping)} 个（长度 {[len(v) for v in mapping]}）")
    if not mapping:
        print("没有需要脱敏的标识符")
        return 0

    # 长值优先替换：设备 ID 里嵌了 unb，先把长的换掉再换短的，避免换出半个串
    ordered = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    touched = []
    for rel in candidate_files():
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > args.max_bytes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        original = text
        counts = {}
        for real, fake in ordered:
            n = text.count(real)
            if n:
                text = text.replace(real, fake)
                counts[real[-4:]] = n
        if text != original:
            touched.append((rel, counts))
            if args.apply:
                # 显式 UTF-8 + 不改行尾（中文文件用脚本回写时最容易在这里坏掉）
                path.write_text(text, encoding="utf-8", newline="")

    print(f"\n命中文件 {len(touched)} 个：")
    for rel, counts in touched:
        print(f"  {rel}: {sum(counts.values())} 处")
    if args.apply:
        print("\n已写回。接着必须跑 release_secret_audit.py 与门禁确认干净且没坏。")
    else:
        print("\n这是预览。确认后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
