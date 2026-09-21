# -*- coding: utf-8 -*-
"""闲鱼通道 P0 门禁：一次跑完三个离线自检（全部不联网、不连闲鱼、不发真实消息）。

用法：python scripts/test_xianyu_p0.py
任一项失败即返回非 0，可直接挂 CI。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = [
    ("协议层（cookie/签名/设备指纹/MessagePack）", "test_xianyu_protocol.py"),
    ("接口层（hasLogin/get_token/get_item_info，MockTransport）", "test_xianyu_api.py"),
    ("WSS 客户端（注册/心跳/ACK/幂等/重连，本地假服务器）", "test_xianyu_ws.py"),
    ("值守引擎（影子模式/接管/议价/缓存/合规/异常隔离）", "test_xianyu_engine.py"),
    ("服务层（FastAPI 托管开关/健康与查询接口/失败隔离）", "test_xianyu_service.py"),
    ("专家层（提示词/路由/阶梯温度/no_reply/RAG 注入/traces）", "test_xianyu_specialists.py"),
    ("告警与面板（冷却去重/巡检/webhook/接口/控制台区块）", "test_xianyu_alerts.py"),
    ("P1 存活体检脚本（只连不回硬约束/报告脱敏/失败判定）", "test_xianyu_live_check.py"),
    ("影子模式复盘报表（统计口径/风险信号/建议/CLI）", "test_xianyu_shadow_report.py"),
    ("出站策略（合规/限速/去重/拟人化/静默时段/引擎联动）", "test_xianyu_outbound.py"),
    ("端到端干跑（影子模式硬约束 + 自动发送 + 报表闭环）", "test_xianyu_e2e_dryrun.py"),
    ("开跑前就绪体检（凭据/规则/开关/提示词判定）", "test_xianyu_preflight.py"),
    ("长跑体检脚本（重连/发送/RSS/频率判定与采样循环）", "test_xianyu_soak.py"),
    # 串起「假平台 socket → 交付队列 → 引擎 → 真的写出站帧」的完整实时链路；
    # 真机事故（收帧循环被业务堵住导致发送被打断）就是它这一层能抓住的
    ("实时链路端到端（交付队列/真出站帧/幂等/慢处理不堵循环）", "test_xianyu_live_seam.py"),
]


def main() -> int:
    results = []
    for title, script in SUITES:
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / script)],
                              capture_output=True, text=True, encoding="utf-8", errors="ignore")
        last = [ln for ln in (proc.stdout or "").splitlines() if "通过" in ln]
        results.append((title, proc.returncode == 0, last[-1] if last else f"exit={proc.returncode}"))
        print(f"[{'PASS' if proc.returncode == 0 else 'FAIL'}] {title} — {results[-1][2]}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nP0 门禁：{passed}/{len(results)} 个套件通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
