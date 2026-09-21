# -*- coding: utf-8 -*-
r"""开跑前就绪体检（preflight）：一条命令回答「现在能不能连平台」。

只读本地文件与本地服务，**不做任何平台请求**（这一点和 `xianyu_live_check.py` 严格区分：
那个会连闲鱼、只能在你确认后跑；这个随便跑，跑一百次也不碰平台）。

检查项：
1. 凭据文件可解析、含 unb、Cookie 字段数正常；
2. `_m_h5_tk` 是否临近过期（时间戳后缀，<10 分钟提醒重新导出）；
3. `data/卖家规则.md` 里还有多少【未配置】（AI 会把这些当"不知道"转人工）；
4. `data/闲鱼值守规范.md` 是否存在（行为准则与合规红线靠它）；
5. 专家提示词 4 个文件是否就位（缺失会回落内置版，功能可用但风格会变）；
6. `.env` 通道开关、回复引擎、出站限速、告警 webhook；
7. 本地 AI-Robot 服务与通道接口是否可达（可用 `--skip-service` 跳过）；
8. 旧 bot 是否最近仍在写库（同账号同时在线有互挤风险，启发式判断）。

用法：
    python scripts/xianyu_preflight.py
    python scripts/xianyu_preflight.py --skip-service          # 只看本地文件
退出码：0 = 可以开跑；1 = 有 blocker（必须先处理）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

OK, WARN, BLOCKER = "ok", "warn", "blocker"
UNCONFIGURED = "【未配置】"
PROMPT_NAMES = ("classify", "price", "tech", "default")


def _check(name: str, level: str, detail: str, action: str = "") -> Dict[str, Any]:
    return {"name": name, "level": level, "detail": detail, "action": action}


def parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def check_credentials(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _check("凭据文件", BLOCKER, f"不存在：{path}",
                      "从浏览器导出闲鱼 Cookie 写入该文件（见 RUNBOOK 第〇节）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _check("凭据文件", BLOCKER, f"无法解析 JSON：{exc}", "检查文件是否被写坏")
    cookies_str = data.get("cookies_str") or ""
    cookies = {k: v for k, v in (c.split("=", 1) for c in cookies_str.split("; ") if "=" in c)}
    if "unb" not in cookies:
        return _check("登录态", BLOCKER, "Cookie 里没有 unb（不是有效登录态）",
                      "重新导出：F12 → Network → 任一请求 → 请求头里的完整 Cookie")
    detail = f"字段数 {len(cookies)}，账号 {cookies['unb'][:4]}***{cookies['unb'][-2:]}"
    if len(cookies) < 10:
        return _check("登录态", WARN, detail + "（字段偏少，可能导出不完整）", "重新导出完整 Cookie")
    return _check("登录态", OK, detail)


def check_token_expiry(path: Path) -> Dict[str, Any]:
    """`_m_h5_tk` 形如 `<token>_<毫秒时间戳>`，时间戳过期说明该 Cookie 需要刷新。"""
    if not path.exists():
        return _check("Cookie 有效期", WARN, "凭据文件不存在，跳过", "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _check("Cookie 有效期", WARN, "凭据文件无法解析，跳过", "")
    cookies = {k: v for k, v in (c.split("=", 1) for c in (data.get("cookies_str") or "").split("; ") if "=" in c)}
    tk = cookies.get("_m_h5_tk", "")
    if "_" not in tk:
        return _check("Cookie 有效期", WARN, "没有 _m_h5_tk 或格式异常", "重新导出 Cookie")
    try:
        expires_ms = int(tk.rsplit("_", 1)[1])
    except Exception:
        return _check("Cookie 有效期", WARN, f"_m_h5_tk 时间戳无法解析：{tk[-12:]}", "")
    remain = (expires_ms / 1000) - time.time()
    if remain <= 0:
        return _check("Cookie 有效期", WARN,
                      f"_m_h5_tk 已过期 {abs(remain) / 60:.0f} 分钟（取 token 时会自动刷新，通常不影响）",
                      "若体检失败就重新导出 Cookie")
    if remain < 600:
        return _check("Cookie 有效期", WARN, f"_m_h5_tk 将在 {remain / 60:.1f} 分钟后过期",
                      "建议先重新导出一份 Cookie 再跑 P1")
    return _check("Cookie 有效期", OK, f"_m_h5_tk 还有 {remain / 3600:.1f} 小时")


def check_seller_rules(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _check("卖家业务规则", BLOCKER, f"缺少 {path.name}",
                      "创建该文件并填写议价底线/包邮/发货时效/保修等")
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = [ln.strip("- ").strip() for ln in text.splitlines() if UNCONFIGURED in ln]
    if not lines:
        return _check("卖家业务规则", OK, "没有未配置项")
    return _check("卖家业务规则", WARN, f"还有 {len(lines)} 项未配置（AI 遇到这些会转人工）",
                  "；".join(ln[:24] for ln in lines[:5]) + ("…" if len(lines) > 5 else ""))


def check_knowledge(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _check("值守规范知识库", BLOCKER, f"缺少 {path.name}",
                      "该文件是行为准则与合规红线的载体，缺少会显著降低回答安全性")
    size = path.stat().st_size
    return _check("值守规范知识库", OK, f"{path.name}（{size} 字节）")


def check_prompts(prompts_dir: Path) -> Dict[str, Any]:
    missing = [n for n in PROMPT_NAMES if not (prompts_dir / f"{n}_prompt.txt").exists()]
    if missing:
        return _check("专家提示词", WARN, f"缺少 {missing}（会回落到内置精简版）",
                      f"补齐 {prompts_dir} 下的 *_prompt.txt")
    return _check("专家提示词", OK, "classify/price/tech/default 齐全")


def check_env(env: Dict[str, str]) -> List[Dict[str, Any]]:
    out = []
    enabled = str(env.get("XIANYU_ENABLED", "false")).lower() == "true"
    out.append(_check("通道开关", OK if enabled else WARN,
                      "XIANYU_ENABLED=true" if enabled else "XIANYU_ENABLED=false（通道未开启）",
                      "" if enabled else "确认要连平台后再改成 true 并重启"))
    shadow = str(env.get("XIANYU_SHADOW_MODE", "true")).lower() == "true"
    out.append(_check("影子模式", OK, "shadow=true（只落草稿不发送）" if shadow
                      else "shadow=false（会真实发送！）",
                      "" if shadow else "先跑影子模式确认质量，再考虑关闭"))
    engine = env.get("XIANYU_REPLY_ENGINE", "specialists")
    out.append(_check("回复引擎", OK if engine in ("specialists", "airobot") else WARN,
                      engine, "" if engine == "specialists" else "闲鱼场景建议用 specialists"))
    limits = {k: env.get(k, "") for k in ("XIANYU_MIN_INTERVAL_PER_CHAT", "XIANYU_MAX_PER_MINUTE",
                                          "XIANYU_MAX_PER_HOUR", "XIANYU_TYPING_SIMULATION")}
    broken = [k for k, v in limits.items() if not v]
    out.append(_check("出站限速配置", OK if not broken else WARN,
                      ", ".join(f"{k.split('_')[-1]}={v}" for k, v in limits.items() if v) or "全部缺失",
                      f"缺失项会走代码默认值：{broken}" if broken else ""))
    webhook = env.get("XIANYU_ALERT_WEBHOOK", "")
    out.append(_check("告警外推", OK if webhook else WARN,
                      "已配置 webhook" if webhook else "未配置（告警只进控制台与事件流水）",
                      "" if webhook else "填飞书/企微机器人地址可让告警推到你手机上"))
    return out


def check_service(base_url: str = "http://127.0.0.1:8000") -> Dict[str, Any]:
    try:
        import httpx
        with httpx.Client(base_url=base_url, timeout=8.0) as client:
            health = client.get("/health").json()
            channel = client.get("/api/v1/xianyu/health").json()
        detail = (f"服务 {health.get('status')}，通道 enabled={channel.get('enabled')} "
                  f"started={channel.get('started')}，知识库分块见 /api/v1/stats")
        if channel.get("last_error"):
            return _check("本地服务", WARN, detail + f"，last_error={channel['last_error']}", "看通道 last_error")
        return _check("本地服务", OK, detail)
    except Exception as exc:
        return _check("本地服务", WARN, f"{base_url} 不可达：{type(exc).__name__}",
                      "先跑 .\\start-local.ps1")


def check_old_bot(db_path: Path, window_seconds: int = 600) -> Dict[str, Any]:
    """旧 bot 活跃度（启发式）：它处理消息时会写这个库。"""
    if not db_path.exists():
        return _check("旧 bot 冲突提醒", OK, "未发现旧 bot 的数据库", "")
    age = time.time() - db_path.stat().st_mtime
    if age < window_seconds:
        return _check("旧 bot 冲突提醒", WARN,
                      f"旧 bot 的库 {age / 60:.0f} 分钟前被写过（可能正活跃）",
                      "同账号同时在线可能互挤：先停旧 bot 再连，或改用小号")
    return _check("旧 bot 冲突提醒", OK, f"旧 bot 库 {age / 3600:.1f} 小时未更新（大概率空闲）")


def run_preflight(root: Path = ROOT, *, credentials: Optional[Path] = None,
                  env_file: Optional[Path] = None, rules: Optional[Path] = None,
                  kb: Optional[Path] = None, prompts_dir: Optional[Path] = None,
                  old_bot_db: Optional[Path] = None, skip_service: bool = False,
                  base_url: str = "http://127.0.0.1:8000") -> Dict[str, Any]:
    credentials = credentials or root / "secrets" / "xianyu_credentials.json"
    env_file = env_file or root / ".env"
    rules = rules or root / "data" / "卖家规则.md"
    kb = kb or root / "data" / "闲鱼值守规范.md"
    prompts_dir = prompts_dir or root / "prompts" / "xianyu"
    old_bot_db = old_bot_db or Path(r"D:\dsh\XianyuAutoAgent\data\chat_history.db")

    checks: List[Dict[str, Any]] = [
        check_credentials(credentials),
        check_token_expiry(credentials),
        check_seller_rules(rules),
        check_knowledge(kb),
        check_prompts(prompts_dir),
    ]
    checks += check_env(parse_env(env_file))
    if not skip_service:
        checks.append(check_service(base_url))
    checks.append(check_old_bot(old_bot_db))

    blockers = [c for c in checks if c["level"] == BLOCKER]
    warns = [c for c in checks if c["level"] == WARN]
    return {
        "ready": not blockers,
        "checks": checks,
        "blockers": len(blockers),
        "warnings": len(warns),
        "next_step": ("python scripts/xianyu_live_check.py --seconds 60（P1 只连不回体检）"
                      if not blockers else "先处理上面的 blocker，再跑 P1 体检"),
    }


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 68)
    print("闲鱼值守开跑前就绪体检（不做任何平台请求）")
    print("=" * 68)
    icon = {OK: "✅", WARN: "⚠️", BLOCKER: "⛔"}
    for c in report["checks"]:
        print(f"  {icon[c['level']]} {c['name']:<14} {c['detail']}")
        if c["action"] and c["level"] != OK:
            print(f"      → {c['action']}")
    print("-" * 68)
    print(f"  blocker {report['blockers']} 项，提醒 {report['warnings']} 项")
    print(f"  结论：{'可以开跑' if report['ready'] else '暂不可开跑（先处理 blocker）'}")
    print(f"  下一步：{report['next_step']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="闲鱼值守开跑前就绪体检（只读本地）")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--credentials", default=None)
    parser.add_argument("--env", dest="env_file", default=None)
    parser.add_argument("--rules", default=None)
    parser.add_argument("--kb", default=None)
    parser.add_argument("--prompts", dest="prompts_dir", default=None)
    parser.add_argument("--old-bot-db", default=None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--skip-service", action="store_true")
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args(argv)

    report = run_preflight(
        Path(args.root),
        credentials=Path(args.credentials) if args.credentials else None,
        env_file=Path(args.env_file) if args.env_file else None,
        rules=Path(args.rules) if args.rules else None,
        kb=Path(args.kb) if args.kb else None,
        prompts_dir=Path(args.prompts_dir) if args.prompts_dir else None,
        old_bot_db=Path(args.old_bot_db) if args.old_bot_db else None,
        skip_service=args.skip_service, base_url=args.base_url)
    print_report(report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  JSON 已写入 {args.json_out}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
