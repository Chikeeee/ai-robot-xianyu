# -*- coding: utf-8 -*-
r"""影子模式复盘报表的离线自检：往临时库里塞人造草稿，验证统计口径与风险信号。

用法：python scripts/test_xianyu_shadow_report.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

import xianyu_shadow_report as report_mod  # noqa: E402
from app.channels.xianyu.session import ChannelSessionStore  # noqa: E402

RESULTS = []
SAFE = "[安全提醒]请通过平台沟通"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fresh_db(name):
    path = ROOT / ".logs" / f"ktest_shadow_{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return ChannelSessionStore(path), path


def seed():
    store, path = fresh_db("seed")
    # 正常草稿 ×2
    store.save_draft(message_id="a1", chat_id="C1", item_id="888", inbound="这个还在吗？",
                     reply="在的，成色如图～", intent="default", sources=["闲鱼值守规范.md#2"], engine="specialists")
    store.save_draft(message_id="a2", chat_id="C1", item_id="888", inbound="参数是多少？",
                     reply="支持蓝牙 5.3，续航 20 小时。", intent="tech",
                     sources=["闲鱼值守规范.md#3"], engine="specialists")
    # 议价（含数字）
    store.save_draft(message_id="a3", chat_id="C2", item_id="888", inbound="能便宜点吗？",
                     reply="可以少 10 元，运费到付。", intent="price",
                     sources=["卖家规则.md#1"], engine="specialists")
    # 风控信号：引用【未配置】
    store.save_draft(message_id="a4", chat_id="C3", item_id="999", inbound="发货要多久？",
                     reply="发货时效：【未配置】，请咨询人工。", intent="default",
                     sources=["卖家规则.md#2"], engine="specialists")
    # 风控信号：站外引流漏网
    store.save_draft(message_id="a5", chat_id="C4", item_id="999", inbound="在吗",
                     reply="加我微信聊更快", intent="default", sources=[], engine="specialists")
    # 合规过滤已生效
    store.save_draft(message_id="a6", chat_id="C5", item_id="999", inbound="能加微信吗",
                     reply=SAFE, intent="default", sources=[], engine="specialists")
    # 过长 + 表情 + 承诺词
    store.save_draft(message_id="a7", chat_id="C6", item_id="999", inbound="质量怎么样",
                     reply="保证绝对没问题😀" + "很" * 130, intent="default",
                     sources=["闲鱼值守规范.md#1"], engine="specialists")
    # no_reply
    store.save_draft(message_id="a8", chat_id="C7", item_id="999", inbound="你是什么模型",
                     reply="-", intent="no_reply", sources=[], engine="specialists")
    # 技术无来源
    store.save_draft(message_id="a9", chat_id="C8", item_id="999", inbound="这个参数",
                     reply="参数以页面为准", intent="tech", sources=[], engine="specialists")
    # 议价无数字
    store.save_draft(message_id="a10", chat_id="C9", item_id="999", inbound="便宜点",
                     reply="这个价格很实在了", intent="price", sources=[], engine="specialists")
    # 「先让买家出价」是提示词要求的策略，不应被当成问题
    store.save_draft(message_id="a11", chat_id="C10", item_id="999", inbound="最低多少",
                     reply="您先出个价吧，合适就成交", intent="price", sources=[], engine="specialists")
    # 事件与告警
    store.record_event("ws_registered", did="x")
    store.record_event("ws_message", chat_id="C1")
    store.record_event("alert:heartbeat_timeout", severity="warning", detail="21s 无响应")
    store.record_event("alert:auth_error", severity="critical", detail="cookie 失效")
    store.add_message("C1", "111", "888", "user", "这个还在吗？")
    store.add_message("C1", "1000000000001", "888", "assistant", "在的")
    store.enter_manual_mode("C2", timeout_seconds=3600)
    return path


def scenario_seeded():
    path = seed()
    report = report_mod.build_report(path)
    c = report["counts"]
    check("报表统计口径正确（草稿/未发送/会话/消息/已处理帧/商品缓存）",
          c["drafts"] == 11 and c["drafts_unsent"] == 11 and c["chats"] == 1
          and c["messages"] == 2 and c["seen_frames"] == 0,
          json.dumps(c, ensure_ascii=False))
    check("意图分布与回复长度统计正确",
          report["intent_distribution"] == {"default": 5, "tech": 2, "price": 3, "no_reply": 1}
          and 10 <= report["reply_len_by_intent"]["price"]["avg"] <= 20,
          json.dumps(report["intent_distribution"], ensure_ascii=False))

    risks = {r["name"]: r for r in report["risks"]}
    unconf = next(r for n, r in risks.items() if n.startswith("引用【未配置】"))
    offsite = next(r for n, r in risks.items() if n.startswith("站外引流"))
    safe_hit = next(r for n, r in risks.items() if n.startswith("合规过滤已生效"))
    overlong = next(r for n, r in risks.items() if n.startswith("回复过长"))
    emoji = next(r for n, r in risks.items() if n.startswith("含表情"))
    promise = next(r for n, r in risks.items() if n.startswith("出现承诺"))
    no_rag = next(r for n, r in risks.items() if "未命中知识库" in n)
    price_num = next(r for n, r in risks.items() if "price 意图" in n)
    check("风险信号：引用【未配置】/站外漏网/合规已生效 各 1 条",
          unconf["count"] == 1 and offsite["count"] == 1 and safe_hit["count"] == 1
          and unconf["severity"] == "high" and offsite["severity"] == "high",
          f"未配置={unconf['count']} 漏网={offsite['count']} 已拦截={safe_hit['count']}")
    check("风险信号：过长/表情/承诺词 命中同一条草稿",
          overlong["count"] == 1 and emoji["count"] == 1 and promise["count"] == 1,
          f"过长={overlong['count']} 表情={emoji['count']} 承诺={promise['count']}")
    # price 也属于需要 RAG 的意图（议价规则写在卖家规则里），所以无来源的 price 草稿同样计入：
    # a5(default) + a9(tech) + a10(price) + a11(price) = 4；合规替换那条(a6)刻意排除
    check("风险信号：无来源的 price/tech/default 草稿 4 条（合规替换那条不算）、无数字的 price 草稿 1 条",
          no_rag["count"] == 4 and price_num["count"] == 1,
          f"无来源={no_rag['count']}（a5 漏网 + a9 tech + a10/a11 price 无来源），"
          f"议价无数字={price_num['count']}（「您先出个价」是提示词要求的策略，已排除）")
    check("风险示例带上下文（买家问 + 草稿答）",
          unconf["examples"] and unconf["examples"][0]["inbound"] and unconf["examples"][0]["reply"],
          str(unconf["examples"][0])[:80])
    check("事件与告警摘要正确（含 2 条告警）",
          report["events"].get("ws_registered") == 1 and len(report["alerts"]) == 2
          and report["alerts"][0]["kind"].startswith("alert:"),
          f"events={report['events']}")
    check("人工接管会话被列出", report["manual_chats"] == ["C2"], str(report["manual_chats"]))
    check("自动建议覆盖关键动作（补卖家规则 / 修合规 / 补知识库 / P4 前置）",
          any("卖家规则" in s for s in report["suggestions"])
          and any("站外引流" in s for s in report["suggestions"])
          and any("知识库" in s for s in report["suggestions"])
          and any("SHADOW_MODE" in s for s in report["suggestions"]),
          f"{len(report['suggestions'])} 条建议")

    # 命令行 + 落盘
    json_out = ROOT / ".logs" / "ktest_shadow_report.json"
    md_out = ROOT / ".logs" / "ktest_shadow_report.md"
    code = report_mod.main(["--db", str(path), "--json", str(json_out), "--md", str(md_out)])
    dumped = json.loads(json_out.read_text(encoding="utf-8"))
    check("CLI 正常退出并把 JSON/Markdown 落盘",
          code == 0 and dumped["counts"]["drafts"] == 11 and "闲鱼影子模式复盘报表" in md_out.read_text(encoding="utf-8"),
          f"exit={code} md 字节={md_out.stat().st_size}")
    return report


def scenario_empty():
    empty_db = ROOT / ".logs" / "ktest_shadow_empty.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(empty_db) + suffix).unlink(missing_ok=True)
    code = report_mod.main(["--db", str(empty_db)])
    check("还没有影子数据时给出明确提示并返回码 2（可脚本化判断）", code == 2, f"exit={code}")

    missing = ROOT / ".logs" / "no_such_shadow.db"
    missing.unlink(missing_ok=True)
    report = report_mod.build_report(missing)
    check("数据库文件不存在时不抛异常，如实报告 db_exists=false",
          report["db_exists"] is False and report["has_data"] is False, str(report["db_exists"]))


def main():
    scenario_seeded()
    scenario_empty()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"影子复盘报表离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_shadow_report_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n影子复盘报表离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
