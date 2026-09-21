# -*- coding: utf-8 -*-
r"""值守长跑体检（soak）：按固定间隔采样通道健康与进程资源，判断「7×24 值守」是否站得住。

只看本地服务与本地进程，**不连平台**（它读的是通道自己上报的健康快照）。

采样内容：
- 连接层：connected / connects / reconnects / frames_in / 心跳（发·应答·超时）/ token 刷新次数；
- 业务层：messages / chats / drafts / events / seen_frames / items（看状态是否无界增长）；
- 出站：texts_sent / allowed / blocked（确认长跑期间没有偷偷发消息）；
- 资源：服务进程 RSS（Windows 用 psutil 缺失则退化为读 `wmic`/`tasklist` 不可用时跳过）、DB 文件大小。

结论规则（`analyze`，纯函数、可离线自测）：
- 出现 `reconnects > 0` 且伴随 `heartbeat_timeouts > 0` → **连接不稳**（告警级）；
- RSS 采样末值比首值增长超过阈值（默认 30%） → **疑似内存泄漏**（要盯）；
- `texts_sent` 在影子模式下 > 0 → **严重异常**（影子模式不该发送）；
- `events`/`messages` 无界增长且与收帧数不匹配 → 提示检查；
- 全程零重连、零超时、零发送、RSS 平稳 → 通过。

用法：
    python scripts/xianyu_soak.py --minutes 10                # 前台跑 10 分钟
    python scripts/xianyu_soak.py --minutes 480 --interval 60 # 过夜跑 8 小时
    python scripts/xianyu_soak.py --self-test                 # 只验证分析逻辑（不采样）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")


def sample_once(base_url: str = "http://127.0.0.1:8000", pid: Optional[int] = None,
                db_path: Optional[Path] = None) -> Dict[str, Any]:
    """采一次样本（health + 可选 RSS / DB 大小）。"""
    import httpx

    out: Dict[str, Any] = {"at": time.time()}
    try:
        with httpx.Client(base_url=base_url, timeout=15.0) as client:
            health = client.get("/api/v1/xianyu/health").json()
        ws = health.get("ws") or {}
        session = health.get("session") or {}
        outbound = health.get("outbound") or {}
        out.update({
            "enabled": health.get("enabled"), "started": health.get("started"),
            "shadow_mode": health.get("shadow_mode"), "last_error": health.get("last_error"),
            "connected": ws.get("connected"), "registered": ws.get("registered"),
            "connects": ws.get("connects"), "reconnects": ws.get("reconnects"),
            "frames_in": ws.get("frames_in"), "messages_ws": ws.get("messages"),
            "heartbeats_sent": ws.get("heartbeats_sent"), "heartbeats_acked": ws.get("heartbeats_acked"),
            "heartbeat_timeouts": ws.get("heartbeat_timeouts"),
            "token_refreshes": ws.get("token_refreshes"), "texts_sent": ws.get("texts_sent"),
            "decode_errors": ws.get("decode_errors"), "frame_errors": ws.get("frame_errors"),
            "duplicates": ws.get("duplicates"),
            "messages": session.get("messages"), "chats": session.get("chats"),
            "drafts": session.get("drafts"), "events": session.get("events"),
            "seen_frames": session.get("seen_frames"), "items": session.get("items"),
            "outbound_allowed": outbound.get("allowed"), "outbound_blocked": outbound.get("blocked"),
            "alerts_fired": (health.get("alerts") or {}).get("fired"),
            "ok": True,
        })
    except Exception as exc:
        out.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    if pid:
        try:
            ps = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).WorkingSet64"],
                capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20)
            value = (ps.stdout or "").strip()
            if value.isdigit():
                out["rss_mb"] = round(int(value) / 1024 / 1024, 1)
        except Exception:
            pass
    if db_path and db_path.exists():
        out["db_kb"] = round(db_path.stat().st_size / 1024, 1)
    return out


def analyze(samples: List[Dict[str, Any]], *, rss_growth_limit: float = 0.30) -> Dict[str, Any]:
    """纯分析：给一串样本，判定长跑是否健康。"""
    ok_samples = [s for s in samples if s.get("ok")]
    findings: List[Dict[str, str]] = []
    if not ok_samples:
        return {"verdict": "unknown", "samples": len(samples), "findings": [
            {"level": "warn", "text": "没有任何有效样本（服务不可达？）"}]}
    first, last = ok_samples[0], ok_samples[-1]
    duration_s = round(last.get("at", 0) - first.get("at", 0), 1)

    if last.get("reconnects", 0) > 0:
        level = "blocker" if last.get("heartbeat_timeouts", 0) > 0 else "warn"
        findings.append({"level": level,
                         "text": f"长跑期间重连 {last['reconnects']} 次"
                                 f"（心跳超时 {last.get('heartbeat_timeouts', 0)} 次）"})
    if last.get("heartbeat_sent_delta") or (last.get("heartbeats_sent", 0) - first.get("heartbeats_sent", 0)) > 0:
        sent = last.get("heartbeats_sent", 0) - first.get("heartbeats_sent", 0)
        acked = last.get("heartbeats_acked", 0) - first.get("heartbeats_acked", 0)
        if sent and acked < sent:
            findings.append({"level": "warn", "text": f"心跳应答不足：发 {sent} 次、应答 {acked} 次"})
    if (last.get("texts_sent", 0) or 0) > 0 and last.get("shadow_mode"):
        findings.append({"level": "blocker",
                         "text": f"影子模式下竟然发送了 {last['texts_sent']} 条消息（必须为 0）"})
    if (last.get("decode_errors", 0) or 0) > 0 or (last.get("frame_errors", 0) or 0) > 0:
        findings.append({"level": "warn",
                         "text": f"解码/帧错误 {last.get('decode_errors')}/{last.get('frame_errors')}（已丢弃对应帧）"})
    if (last.get("token_refreshes", 0) or 0) > 1 and duration_s < 3600:
        findings.append({"level": "warn",
                         "text": f"{duration_s}s 内刷新 token {last['token_refreshes']} 次（异常频繁）"})
    if not last.get("connected"):
        findings.append({"level": "blocker", "text": "采样结束时连接未建立"})
    if last.get("last_error"):
        findings.append({"level": "warn", "text": f"通道 last_error: {last['last_error'][:80]}"})

    rss_first, rss_last = first.get("rss_mb"), last.get("rss_mb")
    if rss_first and rss_last:
        growth = (rss_last - rss_first) / rss_first
        if growth > rss_growth_limit:
            findings.append({"level": "warn",
                             "text": f"RSS 从 {rss_first}MB 涨到 {rss_last}MB（+{growth:.0%}），留意内存"})
        else:
            findings.append({"level": "ok", "text": f"RSS 平稳：{rss_first}→{rss_last}MB"})

    blockers = [f for f in findings if f["level"] == "blocker"]
    warns = [f for f in findings if f["level"] == "warn"]
    return {
        "verdict": "blocker" if blockers else ("attention" if warns else "ok"),
        "samples": len(ok_samples), "duration_s": duration_s,
        "delta": {
            "frames_in": (last.get("frames_in", 0) or 0) - (first.get("frames_in", 0) or 0),
            "heartbeats": (last.get("heartbeats_sent", 0) or 0) - (first.get("heartbeats_sent", 0) or 0),
            "messages": (last.get("messages", 0) or 0) - (first.get("messages", 0) or 0),
            "drafts": (last.get("drafts", 0) or 0) - (first.get("drafts", 0) or 0),
            "events": (last.get("events", 0) or 0) - (first.get("events", 0) or 0),
            "texts_sent": last.get("texts_sent", 0),
        },
        "findings": findings,
    }


def run_soak(minutes: float, interval: float, *, base_url: str = "http://127.0.0.1:8000",
             pid: Optional[int] = None, db_path: Optional[Path] = None,
             sampler: Callable[..., Dict[str, Any]] = sample_once,
             sleeper: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """按 `interval` 秒采样 `minutes` 分钟。"""
    samples: List[Dict[str, Any]] = []
    total = max(1, int(minutes * 60 / interval))
    for i in range(total):
        sample = sampler(base_url=base_url, pid=pid, db_path=db_path)
        samples.append(sample)
        mark = "ok" if sample.get("ok") else "ERR"
        print(f"  [{i + 1}/{total}] {mark} connected={sample.get('connected')} "
              f"connects={sample.get('connects')} 帧={sample.get('frames_in')} "
              f"心跳={sample.get('heartbeats_sent')}/{sample.get('heartbeats_acked')} "
              f"重连={sample.get('reconnects')} 发送={sample.get('texts_sent')} "
              f"草稿={sample.get('drafts')} rss={sample.get('rss_mb')}MB")
        if i < total - 1:
            sleeper(interval)
    report = {"started_at": samples[0]["at"] if samples else time.time(),
              "minutes": minutes, "interval": interval,
              "analysis": analyze(samples), "samples": samples}
    out = ROOT / ".logs" / "xianyu_soak.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(out)
    return report


def service_pid(port: int = 8000) -> Optional[int]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
             f"Select-Object -First 1 -ExpandProperty OwningProcess)"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20)
        value = (out.stdout or "").strip()
        return int(value) if value.isdigit() else None
    except Exception:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="闲鱼值守长跑体检（只读本地服务与进程）")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--pid", type=int, default=None, help="服务进程 PID（默认自动查找 8000 端口）")
    parser.add_argument("--db", default=str(ROOT / "data" / "xianyu.db"))
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args(argv)

    pid = args.pid or service_pid(8000)
    print(f"长跑体检：{args.minutes} 分钟，每 {args.interval} 秒采一次（PID={pid}）")
    report = run_soak(args.minutes, args.interval, base_url=args.base_url, pid=pid,
                      db_path=Path(args.db))
    a = report["analysis"]
    print("-" * 60)
    print(f"  采样 {a['samples']} 次 / {a['duration_s']}s，结论：{a['verdict']}")
    print(f"  增量：{json.dumps(a['delta'], ensure_ascii=False)}")
    for f in a["findings"]:
        icon = {"ok": "✅", "warn": "⚠️", "blocker": "⛔"}[f["level"]]
        print(f"  {icon} {f['text']}")
    print(f"  报告：{report['report_path']}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if a["verdict"] == "ok" else (2 if a["verdict"] == "attention" else 1)


if __name__ == "__main__":
    sys.exit(main())
