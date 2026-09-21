# -*- coding: utf-8 -*-
r"""值守看门狗：健康检查失败就把服务拉起来（配合 Windows 计划任务每 5 分钟跑一次）。

为什么必须有：2026-09-20 22:52 本机进入 Modern Standby，9/21 08:08 才唤醒——
期间服务进程没了、一直没人拉起，**值守整整空了一夜**。
单靠"进程自己重连"不够：进程死了就什么都没了，必须有外部拉起。

判定：GET /api/v1/xianyu/health 能通且 started=true 才算活；
不通/未启动 → 调 start-local.ps1 拉起，并把动作写进日志（便于事后审计）。

用法：
    python scripts/local_watchdog.py            # 跑一次（计划任务用这个）
    python scripts/local_watchdog.py --dry-run  # 只看判定，不拉起
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 允许用环境变量覆盖（自测"检测到不健康"这条路径时不必真去停线上服务）
HEALTH = os.environ.get("WATCHDOG_HEALTH_URL") or "http://127.0.0.1:8000/api/v1/xianyu/health"
LOG = ROOT / ".logs" / "watchdog.log"


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def health_ok() -> tuple:
    """返回 (是否存活, 说明)。存活 = 接口能通且通道已启动。"""
    try:
        import httpx
        # 显式禁用代理：本机全局代理没开时会连 localhost 都失败
        with httpx.Client(timeout=10, trust_env=False) as client:
            resp = client.get(HEALTH)
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            body = resp.json()
            if not body.get("started"):
                return False, f"接口通但通道未启动（last_error={body.get('last_error')}）"
            ws = body.get("ws") or {}
            return True, (f"connected={ws.get('connected')} registered={ws.get('registered')} "
                          f"session_invalid={ws.get('session_invalid')}")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def start_service() -> bool:
    script = ROOT / "start-local.ps1"
    if not script.exists():
        log(f"拉起失败：找不到 {script}")
        return False
    log("服务不可用 → 调用 start-local.ps1 拉起")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(script), "-NoBrowser", "-SkipDeps"],
                         cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log(f"拉起异常：{type(exc).__name__}: {exc}")
        return False
    # 给它一点时间起来，再复核一次（避免"看起来拉起了其实又失败"）
    for _ in range(12):
        time.sleep(5)
        ok, detail = health_ok()
        if ok:
            log(f"拉起成功：{detail}")
            return True
    log("拉起后 60 秒内健康检查仍未通过，请人工看一眼 .logs/uvicorn.err.log")
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="健康时不写日志（计划任务里减少噪音）")
    args = parser.parse_args(argv)

    ok, detail = health_ok()
    if ok:
        if not args.quiet:
            log(f"健康：{detail}")
        return 0
    log(f"不健康：{detail}")
    if args.dry_run:
        return 1
    return 0 if start_service() else 1


if __name__ == "__main__":
    sys.exit(main())
