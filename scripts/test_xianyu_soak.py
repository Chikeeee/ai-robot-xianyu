# -*- coding: utf-8 -*-
r"""长跑体检脚本的离线自检：用合成样本序列验证判定规则，用注入采样器验证采样循环（不等待、不联网）。

用法：python scripts/test_xianyu_soak.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

import xianyu_soak as soak  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def sample(at, **kw):
    base = {"at": at, "ok": True, "connected": True, "registered": True, "connects": 1,
            "reconnects": 0, "frames_in": 0, "heartbeats_sent": 0, "heartbeats_acked": 0,
            "heartbeat_timeouts": 0, "token_refreshes": 0, "texts_sent": 0,
            "decode_errors": 0, "frame_errors": 0, "messages": 0, "drafts": 0,
            "events": 0, "rss_mb": 200.0, "shadow_mode": True, "last_error": None}
    base.update(kw)
    return base


def scenario_healthy():
    samples = [sample(0, frames_in=10, heartbeats_sent=2, heartbeats_acked=2, rss_mb=200.0),
               sample(30, frames_in=14, heartbeats_sent=4, heartbeats_acked=4, rss_mb=201.0),
               sample(60, frames_in=18, heartbeats_sent=6, heartbeats_acked=6, rss_mb=200.5)]
    a = soak.analyze(samples)
    check("健康长跑判定为 ok（零重连、心跳全应答、RSS 平稳）",
          a["verdict"] == "ok" and a["duration_s"] == 60.0
          and any("RSS 平稳" in f["text"] for f in a["findings"]),
          f"verdict={a['verdict']} delta={a['delta']}")


def scenario_reconnect():
    samples = [sample(0), sample(30, reconnects=1, heartbeat_timeouts=1, connects=2),
               sample(60, reconnects=2, heartbeat_timeouts=1, connects=3)]
    a = soak.analyze(samples)
    check("出现重连 + 心跳超时 → 判为 blocker（连接不稳）",
          a["verdict"] == "blocker" and any("重连" in f["text"] for f in a["findings"]),
          f"verdict={a['verdict']}")


def scenario_shadow_sent():
    samples = [sample(0), sample(30, texts_sent=0), sample(60, texts_sent=2)]
    a = soak.analyze(samples)
    check("★ 影子模式下竟然发送 → 判为 blocker（这是最严重的安全问题）",
          a["verdict"] == "blocker" and any("影子模式" in f["text"] for f in a["findings"]),
          f"verdict={a['verdict']}")


def scenario_rss_growth():
    samples = [sample(0, rss_mb=200.0), sample(30, rss_mb=240.0), sample(60, rss_mb=300.0)]
    a = soak.analyze(samples)
    check("RSS 增长超阈值 → 提醒疑似内存泄漏（不阻塞，但要盯）",
          a["verdict"] == "attention" and any("RSS" in f["text"] for f in a["findings"]),
          f"verdict={a['verdict']} {[f['text'] for f in a['findings']]}")


def scenario_unhealthy_edges():
    a = soak.analyze([sample(0), sample(30, connected=False, decode_errors=3)])
    check("连接断开 + 解码错误 → blocker 且列出错误计数",
          a["verdict"] == "blocker"
          and any("连接未建立" in f["text"] for f in a["findings"])
          and any("解码/帧错误" in f["text"] for f in a["findings"]),
          str([f["text"] for f in a["findings"]]))
    a2 = soak.analyze([])
    check("没有有效样本时如实返回 unknown，不假装健康",
          a2["verdict"] == "unknown" and a2["findings"], a2["findings"][0]["text"])
    a3 = soak.analyze([sample(0, token_refreshes=3), sample(60, token_refreshes=3)])
    check("一小时内 token 刷新多次 → 提醒（当初「每分钟重连」就是这种形态）",
          a3["verdict"] == "attention" and any("刷新 token" in f["text"] for f in a3["findings"]),
          str([f["text"] for f in a3["findings"]]))


def scenario_sampling_loop():
    """注入采样器与假 sleep：验证采样次数、间隔调用与报告落盘（不真等待）。"""
    calls = []
    sleeps = []

    def fake_sampler(base_url=None, pid=None, db_path=None):
        calls.append(base_url)
        idx = len(calls)
        return sample(idx * 30, frames_in=idx * 4, heartbeats_sent=idx * 2, heartbeats_acked=idx * 2,
                      rss_mb=200.0 + idx * 0.1)

    report = soak.run_soak(1.0, 30.0, sampler=fake_sampler, sleeper=lambda s: sleeps.append(s))
    check("采样循环按 minutes/interval 决定次数，并只在间隔处 sleep",
          len(calls) == 2 and sleeps == [30.0], f"采样={len(calls)} 次 sleep={sleeps}")
    check("报告落盘且含分析与原始样本",
          Path(report["report_path"]).exists()
          and report["analysis"]["verdict"] == "ok" and len(report["samples"]) == 2,
          f"{report['analysis']['verdict']} samples={len(report['samples'])}")

    # 采样失败不应崩溃，只在分析里体现
    def failing_sampler(base_url=None, pid=None, db_path=None):
        return {"at": 0, "ok": False, "error": "ConnectError"}

    report2 = soak.run_soak(0.1, 30.0, sampler=failing_sampler, sleeper=lambda s: None)
    check("服务不可达时采样不抛异常，分析如实报 unknown",
          report2["analysis"]["verdict"] == "unknown", str(report2["analysis"]["findings"]))


def main():
    scenario_healthy()
    scenario_reconnect()
    scenario_shadow_sent()
    scenario_rss_growth()
    scenario_unhealthy_edges()
    scenario_sampling_loop()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"长跑体检脚本离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_soak_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n长跑体检脚本离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
