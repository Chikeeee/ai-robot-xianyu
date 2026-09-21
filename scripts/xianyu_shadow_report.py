# -*- coding: utf-8 -*-
r"""影子模式复盘报表：直接读通道 SQLite（只读），把草稿质量、意图分布、风险信号汇总出来。

影子模式的目的是「真连真生成、只落库不发送」，所以必须有人看这批草稿到底答得怎么样。
本脚本不看源码、不发请求、不连平台，只读 `data/xianyu.db`：

- 总览：草稿/会话/消息/已处理帧/重复帧、时间范围；
- 意图分布（专家层：price/tech/default/no_reply）与回复长度；
- **风险信号**（每条都带示例，便于定位）：
  - 引用了【未配置】内容（说明模型违反了「未配置不得据此回答」的硬要求）
  - 出现站外引流词但没被替换成安全话术（合规过滤漏网）
  - 回复过长（>120 字，违反专家提示词的风格约束）
  - 含表情符号
  - 出现承诺性词汇（保证/一定/100%）
  - tech/default 意图却没有任何 RAG 来源（知识库没命中）
  - price 意图但回复里没有数字（可能没正面回应议价）
- 事件与告警摘要、人工接管会话；
- 自动给出「下一步动作建议」。

用法：
    python scripts/xianyu_shadow_report.py                    # 控制台看
    python scripts/xianyu_shadow_report.py --json out.json      # 同时落 JSON
    python scripts/xianyu_shadow_report.py --md out.md          # 同时落 Markdown
退出码：0 正常；2 还没有影子数据（未开通道或未收到消息）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

DEFAULT_DB = ROOT / "data" / "xianyu.db"
SAFE_REPLY = "[安全提醒]请通过平台沟通"
OFFSITE_WORDS = ("微信", "QQ", "支付宝", "银行卡", "线下")
PROMISE_WORDS = ("保证", "一定", "100%", "绝对")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
UNCONFIGURED_RE = re.compile(r"【未配置】")
RAG_INTENTS = ("price", "tech", "default")  # 与 app/agents/specialists/service.py 的 RAG_INTENTS 保持一致
MAX_REPLY_LEN = 120
SAMPLES = 8


def _connect(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _rows(conn: sqlite3.Connection, sql: str, *args) -> List[Dict[str, Any]]:
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


def _flag(name: str, hits: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {"name": name, "count": len(hits), **extra,
            "examples": [{"chat_id": h.get("chat_id"), "inbound": (h.get("inbound") or "")[:40],
                          "reply": (h.get("reply") or "")[:80]} for h in hits[:3]]}


def build_report(db_path: Path = DEFAULT_DB, limit: int = 500,
                 include_sim: bool = False) -> Dict[str, Any]:
    """读库并算出报表（纯读，可重复执行）。

    `include_sim=False`（默认）会排除**模拟会话**（`/api/v1/xianyu/simulate` 造的 `SIM-*`）：
    影子复盘是决定「能不能开自动发送」的依据，混进自测数据会把口径算虚。
    """
    report: Dict[str, Any] = {
        "db_path": str(db_path), "db_exists": db_path.exists(), "has_data": False,
        "counts": {}, "intent_distribution": {}, "reply_len_by_intent": {},
        "risks": [], "samples": [], "events": {}, "alerts": [], "manual_chats": [],
        "suggestions": [], "include_sim": include_sim,
    }
    conn = _connect(db_path)
    if conn is None:
        return report
    try:
        drafts = _rows(conn, "SELECT * FROM drafts ORDER BY id DESC LIMIT ?", limit)
        if not include_sim:
            drafts = [d for d in drafts if not str(d.get("chat_id") or "").upper().startswith("SIM")]
        messages = _rows(conn, "SELECT COUNT(*) AS c FROM messages")
        chats = _rows(conn, "SELECT COUNT(DISTINCT chat_id) AS c FROM messages")
        if not include_sim:
            messages = _rows(conn, "SELECT COUNT(*) AS c FROM messages WHERE chat_id NOT LIKE 'SIM%'")
            chats = _rows(conn, "SELECT COUNT(DISTINCT chat_id) AS c FROM messages WHERE chat_id NOT LIKE 'SIM%'")
        seen = _rows(conn, "SELECT COUNT(*) AS c FROM seen_frames")
        events = _rows(conn, "SELECT kind, COUNT(*) AS c FROM events GROUP BY kind ORDER BY c DESC")
        alerts = _rows(conn, "SELECT kind, ts, payload FROM events WHERE kind LIKE 'alert:%' ORDER BY id DESC LIMIT 20")
        manual = _rows(conn, "SELECT chat_id, manual_since FROM chat_state WHERE manual_mode=1")
        items = _rows(conn, "SELECT COUNT(*) AS c FROM items")

        report["has_data"] = bool(drafts) or bool(events)
        report["counts"] = {
            "drafts": len(drafts),
            "drafts_unsent": sum(1 for d in drafts if not d.get("sent")),
            "drafts_sent": sum(1 for d in drafts if d.get("sent")),
            "messages": (messages[0]["c"] if messages else 0),
            "chats": (chats[0]["c"] if chats else 0),
            "seen_frames": (seen[0]["c"] if seen else 0),
            "cached_items": (items[0]["c"] if items else 0),
        }
        if drafts:
            times = sorted([d.get("created_at") or "" for d in drafts if d.get("created_at")])
            report["time_range"] = {"from": times[0], "to": times[-1]} if times else None

        # 意图分布与长度
        dist: Dict[str, int] = {}
        lens: Dict[str, List[int]] = {}
        for d in drafts:
            intent = (d.get("intent") or "unknown").strip() or "unknown"
            dist[intent] = dist.get(intent, 0) + 1
            lens.setdefault(intent, []).append(len(d.get("reply") or ""))
        report["intent_distribution"] = dict(sorted(dist.items(), key=lambda kv: -kv[1]))
        report["reply_len_by_intent"] = {k: {"avg": round(sum(v) / len(v), 1), "max": max(v)}
                                        for k, v in lens.items() if v}

        # 风险信号
        unconfigured = [d for d in drafts if UNCONFIGURED_RE.search(d.get("reply") or "")]
        offsite = [d for d in drafts if (d.get("reply") or "") != SAFE_REPLY
                   and any(w in (d.get("reply") or "") for w in OFFSITE_WORDS)]
        safe_replaced = [d for d in drafts if (d.get("reply") or "") == SAFE_REPLY]
        overlong = [d for d in drafts if len(d.get("reply") or "") > MAX_REPLY_LEN]
        emoji = [d for d in drafts if EMOJI_RE.search(d.get("reply") or "")]
        promise = [d for d in drafts if any(w in (d.get("reply") or "") for w in PROMISE_WORDS)]
        no_rag = [d for d in drafts if (d.get("intent") or "") in RAG_INTENTS
                  and not json.loads(d.get("sources") or "[]")
                  and (d.get("reply") or "") != SAFE_REPLY]  # 合规替换是刻意为之，不算 RAG 漏命中
        # 「先让买家出价」是议价专家提示词里明确要求的策略，不算"没正面回应议价"
        ASK_FIRST_HINTS = ("你先", "您先", "先出价", "出个价", "说说你", "你说个价")
        price_no_num = [d for d in drafts if (d.get("intent") or "") == "price"
                        and not re.search(r"\d", d.get("reply") or "")
                        and not any(h in (d.get("reply") or "") for h in ASK_FIRST_HINTS)]
        report["risks"] = [
            _flag("引用【未配置】内容（违反硬要求，必须修）", unconfigured, severity="high"),
            _flag("站外引流词漏网（合规过滤未生效）", offsite, severity="high"),
            _flag("回复过长（>%d 字，违反风格约束）" % MAX_REPLY_LEN, overlong, severity="low"),
            _flag("含表情符号", emoji, severity="low"),
            _flag("出现承诺性词汇", promise, severity="medium"),
            _flag("tech/default 意图但未命中知识库（无来源）", no_rag, severity="medium"),
            _flag("price 意图但回复无数字（未正面回应议价）", price_no_num, severity="low"),
            _flag("合规过滤已生效（被替换成安全话术）", safe_replaced, severity="info"),
        ]
        report["samples"] = [{
            "created_at": d.get("created_at"), "chat_id": d.get("chat_id"),
            "item_id": d.get("item_id"), "inbound": (d.get("inbound") or "")[:80],
            "reply": (d.get("reply") or "")[:160], "intent": d.get("intent"),
            "sources": d.get("sources"), "mode": d.get("mode"), "sent": bool(d.get("sent")),
        } for d in drafts[:SAMPLES]]

        report["events"] = {e["kind"]: e["c"] for e in events}
        report["alerts"] = [{"kind": a["kind"], "ts": a["ts"],
                             "detail": (a.get("payload") or "")[:120]} for a in alerts[:10]]
        report["manual_chats"] = [m["chat_id"] for m in manual]
        report["suggestions"] = _suggest(report, unconfigured, offsite, overlong, no_rag)
    finally:
        conn.close()
    return report


def _suggest(report: Dict[str, Any], unconfigured: list, offsite: list,
             overlong: list, no_rag: list) -> List[str]:
    out = []
    counts = report["counts"]
    if not counts.get("drafts"):
        out.append("还没有草稿：确认 XIANYU_ENABLED=true 已开启、连接状态为「已连接」，并且真的来过买家消息。")
        return out
    if unconfigured:
        out.append(f"有 {len(unconfigured)} 条草稿引用了【未配置】内容 → 赶紧把 data/卖家规则.md 的真实参数填上。")
    if offsite:
        out.append(f"有 {len(offsite)} 条草稿出现站外引流词且未被替换 → 检查合规过滤是否被绕过（这是平台红线）。")
    if no_rag:
        out.append(f"有 {len(no_rag)} 条技术/客服类草稿没有命中知识库 → 建议补商品资料或平台规则到 data/。")
    if overlong:
        out.append(f"有 {len(overlong)} 条草稿过长 → 专家提示词里的「每句≤10字」没被遵守，可考虑收紧 max_tokens 或换更强的模型。")
    if counts.get("drafts_unsent") and report["intent_distribution"].get("no_reply"):
        ratio = report["intent_distribution"]["no_reply"] / max(1, counts["drafts"])
        if ratio > 0.3:
            out.append(f"no_reply 占比 {ratio:.0%} 偏高 → 检查是否把正常咨询误判成注入/无关话题。")
    if not report["alerts"]:
        out.append("期间没有告警：可视为连接与登录态稳定。")
    out.append("人工比对通过后再把 XIANYU_SHADOW_MODE 改成 false 进入 P4（自动发送）。")
    return out


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 70)
    print("闲鱼影子模式复盘报表")
    print("=" * 70)
    if not report["db_exists"] or not report["has_data"]:
        print(f"  数据库: {report['db_path']}")
        print("  状态  : 还没有影子数据（通道未启用，或还没收到买家消息）")
        return
    c = report["counts"]
    print(f"  数据库  : {report['db_path']}")
    print(f"  时间范围: {(report.get('time_range') or {}).get('from')} ~ {(report.get('time_range') or {}).get('to')}")
    print(f"  草稿    : {c['drafts']}（未发送 {c['drafts_unsent']} / 已发送 {c['drafts_sent']}）")
    print(f"  会话/消息: {c['chats']} / {c['messages']}   已处理帧: {c['seen_frames']}   商品缓存: {c['cached_items']}")
    print(f"  意图分布: {report['intent_distribution']}")
    print(f"  回复长度: {report['reply_len_by_intent']}")
    print("\n  ── 风险信号 ──")
    for risk in report["risks"]:
        mark = "⚠️" if risk["severity"] in ("high", "medium") else ("ℹ️" if risk["severity"] == "info" else "·")
        print(f"  {mark} [{risk['severity']:>6}] {risk['name']}: {risk['count']}")
        for ex in risk["examples"][:2]:
            print(f"         例: 买家「{ex['inbound']}」→ 草稿「{ex['reply']}」")
    print("\n  ── 草稿抽查 ──")
    for s in report["samples"]:
        print(f"  [{s['created_at']}] {s['intent']:>9} | 买家: {s['inbound']}")
        print(f"{'':>14}草稿: {s['reply']}   来源={s['sources']}")
    print("\n  ── 事件/告警 ──")
    print(f"  事件: {report['events']}")
    print(f"  告警: {report['alerts'] or '无'}")
    print(f"  人工接管: {report['manual_chats'] or '无'}")
    print("\n  ── 建议 ──")
    for s in report["suggestions"]:
        print(f"  · {s}")


def to_markdown(report: Dict[str, Any]) -> str:
    lines = ["# 闲鱼影子模式复盘报表", ""]
    if not report["has_data"]:
        lines.append(f"- 数据库：`{report['db_path']}`")
        lines.append("- 状态：还没有影子数据（通道未启用，或还没收到买家消息）")
        return "\n".join(lines)
    c = report["counts"]
    lines += [f"- 数据库：`{report['db_path']}`",
              f"- 草稿：{c['drafts']}（未发送 {c['drafts_unsent']} / 已发送 {c['drafts_sent']}）",
              f"- 会话/消息/已处理帧：{c['chats']} / {c['messages']} / {c['seen_frames']}",
              f"- 意图分布：{report['intent_distribution']}", "",
              "## 风险信号", ""]
    for risk in report["risks"]:
        lines.append(f"- **{risk['name']}**（{risk['severity']}）：{risk['count']}")
    lines += ["", "## 草稿抽查", ""]
    for s in report["samples"]:
        lines.append(f"- `{s['created_at']}` [{s['intent']}] 买家：{s['inbound']} → 草稿：{s['reply']}")
    lines += ["", "## 建议", ""] + [f"- {s}" for s in report["suggestions"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="闲鱼影子模式复盘报表（只读）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--md", dest="md_out", default=None)
    parser.add_argument("--include-sim", action="store_true",
                        help="把 simulate 造的模拟会话（SIM-*）也算进来（默认排除）")
    args = parser.parse_args(argv)

    report = build_report(Path(args.db), args.limit, include_sim=args.include_sim)
    print_report(report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  JSON 已写入 {args.json_out}")
    if args.md_out:
        Path(args.md_out).write_text(to_markdown(report), encoding="utf-8")
        print(f"  Markdown 已写入 {args.md_out}")
    return 0 if report["has_data"] else 2


if __name__ == "__main__":
    sys.exit(main())
