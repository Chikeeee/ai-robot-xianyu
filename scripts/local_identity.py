# -*- coding: utf-8 -*-
r"""本地身份读取（给运维/诊断脚本用）：从凭据文件与数据库取「我是谁 / 该看哪个会话」。

为什么要有这个模块：以前这些脚本把账号 id 硬编码在源码里，脱敏发布时只能换成假值，
结果**工具在本地就认不出自己发的消息了**（`xianyu_verify_sent.py` 会把已送达判成没送达）。
正确做法是让脚本从**本机凭据**读身份 —— 凭据本来就被 .gitignore 排除，
于是"仓库里没有账号 id"与"本地工具照常可用"两件事同时成立。

用法：
    from local_identity import my_unb, default_cid
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CRED = ROOT / "secrets" / "xianyu_credentials.json"
DB = ROOT / "data" / "xianyu.db"


def my_unb() -> str:
    """本机登录账号的 unb（读不到就返回空串，调用方自行降级）。"""
    try:
        data = json.loads(CRED.read_text(encoding="utf-8"))
        return str((data.get("account") or {}).get("unb") or "")
    except Exception:
        return ""


def my_unb_or_exit() -> str:
    unb = my_unb()
    if not unb:
        print(f"读不到本机账号（{CRED} 不存在或缺 account.unb）", file=sys.stderr)
        raise SystemExit(2)
    return unb


def default_cid() -> Optional[str]:
    """最近活跃的真实会话 id（取自本地数据库；用于命令行默认值）。"""
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute(
            "select chat_id from messages where chat_id <> '' and chat_id not like 'SIM%' "
            "and chat_id not glob '*[A-Za-z]*' order by id desc limit 1").fetchone()
        return str(row[0]) if row and row[0] else None
    except Exception:
        return None


if __name__ == "__main__":
    print("本机账号 unb :", (my_unb()[:4] + "***") if my_unb() else "（读不到）")
    cid = default_cid()
    print("最近会话 id  :", (cid[:4] + "***") if cid else "（无）")
