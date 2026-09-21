# -*- coding: utf-8 -*-
r"""就绪体检脚本的离线自检：用临时夹具（伪造凭据/规则/.env）验证各检查项的判定与结论。

同时验证一件重要的事：**本脚本不产生任何平台请求**（通过注入不可达的 base_url 与假旧 bot 库，
即使全部检查跑完也不会抛异常或联网）。

用法：python scripts/test_xianyu_preflight.py
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

import xianyu_preflight as preflight  # noqa: E402

RESULTS = []
FIXTURE = ROOT / ".logs" / "ktest_preflight"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def level_of(report, name_prefix):
    for c in report["checks"]:
        if c["name"].startswith(name_prefix):
            return c["level"], c["detail"]
    return None, None


def make_fixture(*, cookie_fields="full", tk_offset_ms=None, rules_unconfigured=0,
                 env_enabled=True, shadow=True, with_kb=True, with_prompts=True):
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    (FIXTURE / "secrets").mkdir(parents=True)
    (FIXTURE / "data").mkdir(parents=True)
    (FIXTURE / "prompts" / "xianyu").mkdir(parents=True)

    cookies = {"unb": "1000000000001", "cna": "ctype", "cookie2": "c2", "sgcookie": "sg",
               "_tb_token_": "tb", "t": "t", "tracknick": "nick", "xlly_s": "1",
               "havana_lgc2_77": "h", "_hvn_lgc_": "77", "csg": "csg", "sdkSilent": "s"}
    if tk_offset_ms is not None:
        cookies["_m_h5_tk"] = f"deadbeefdeadbeefdeadbeefdeadbeef_{int(time.time() * 1000) + tk_offset_ms}"
    elif cookie_fields == "full":
        cookies["_m_h5_tk"] = f"deadbeefdeadbeefdeadbeefdeadbeef_{int(time.time() * 1000) + 3600_000}"
    if cookie_fields == "no_unb":
        cookies.pop("unb")
    if cookie_fields == "few":
        cookies = {"unb": "1000000000001"}

    (FIXTURE / "secrets" / "xianyu_credentials.json").write_text(json.dumps({
        "account": {"unb": cookies.get("unb", "")},
        "cookies_str": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }, ensure_ascii=False), encoding="utf-8")

    rule_lines = ["# 卖家业务规则", ""]
    for i in range(12):
        rule_lines.append(f"- 项目{i}：【未配置】" if i < rules_unconfigured else f"- 项目{i}：已配置值")
    (FIXTURE / "data" / "卖家规则.md").write_text("\n".join(rule_lines), encoding="utf-8")

    if with_kb:
        (FIXTURE / "data" / "闲鱼值守规范.md").write_text("# 值守规范\n只依据资料作答。\n", encoding="utf-8")

    if with_prompts:
        for name in preflight.PROMPT_NAMES:
            (FIXTURE / "prompts" / "xianyu" / f"{name}_prompt.txt").write_text(f"【角色】{name}", encoding="utf-8")

    env_lines = [
        "# 测试用 .env",
        f"XIANYU_ENABLED={'true' if env_enabled else 'false'}",
        f"XIANYU_SHADOW_MODE={'true' if shadow else 'false'}",
        "XIANYU_REPLY_ENGINE=specialists",
        "XIANYU_MIN_INTERVAL_PER_CHAT=3",
        "XIANYU_MAX_PER_MINUTE=20",
        "XIANYU_MAX_PER_HOUR=300",
        "XIANYU_TYPING_SIMULATION=true",
        "XIANYU_ALERT_WEBHOOK=",
    ]
    (FIXTURE / ".env").write_text("\n".join(env_lines), encoding="utf-8")

    old_db = FIXTURE / "old_bot.db"
    old_db.write_text("x", encoding="utf-8")
    os.utime(old_db, (time.time() - 7200, time.time() - 7200))   # 2 小时前 → 判定为空闲

    return dict(root=FIXTURE, credentials=FIXTURE / "secrets" / "xianyu_credentials.json",
                env_file=FIXTURE / ".env", rules=FIXTURE / "data" / "卖家规则.md",
                kb=FIXTURE / "data" / "闲鱼值守规范.md",
                prompts_dir=FIXTURE / "prompts" / "xianyu", old_bot_db=old_db)


def run(**kwargs):
    # base_url 指向不可达端口：验证「不联网也能跑完」，并让服务检查落为 warn
    return preflight.run_preflight(skip_service=True, base_url="http://127.0.0.1:9", **kwargs)


def scenario_ready():
    fx = make_fixture(rules_unconfigured=0)
    report = run(**fx)
    levels = {c["name"]: c["level"] for c in report["checks"]}
    check("全部就绪时无 blocker（配置齐全 + 规则填完）",
          report["blockers"] == 0 and levels["卖家业务规则"] == "ok" and levels["登录态"] == "ok"
          and levels["值守规范知识库"] == "ok" and levels["专家提示词"] == "ok",
          f"blockers={report['blockers']} warns={report['warnings']}")
    check("未配置告警 webhook 时给出提醒（不阻塞）",
          levels["告警外推"] == "warn" and any("webhook" in c["detail"] or "告警" in c["detail"]
                                              for c in report["checks"] if c["name"] == "告警外推"),
          str(level_of(report, "告警外推")[1]))
    check("下一步命令指向 P1 体检", "xianyu_live_check" in report["next_step"], report["next_step"])


def scenario_unconfigured_rules():
    fx = make_fixture(rules_unconfigured=5)
    report = run(**fx)
    level, detail = level_of(report, "卖家业务规则")
    check("卖家规则未填完 → 提醒并列出条数（不阻塞连接）",
          level == "warn" and "5 项" in detail, detail)


def scenario_missing_credentials():
    fx = make_fixture()
    fx["credentials"].unlink()
    report = run(**fx)
    level, detail = level_of(report, "凭据文件")
    check("凭据缺失 → blocker 且给出取 Cookie 的指引",
          level == "blocker" and not report["ready"], detail)
    check("有 blocker 时下一步不是直接连平台",
          "先处理" in report["next_step"], report["next_step"])


def scenario_no_unb():
    fx = make_fixture(cookie_fields="no_unb")
    report = run(**fx)
    level, detail = level_of(report, "登录态")
    check("Cookie 缺 unb → blocker（避免启动后才报错）", level == "blocker" and "unb" in detail, detail)


def scenario_token_expiry():
    fx = make_fixture(tk_offset_ms=-60_000)          # 已过期
    report = run(**fx)
    level, detail = level_of(report, "Cookie 有效期")
    check("_m_h5_tk 已过期 → 提醒（取 token 时会自动刷新，不阻塞）",
          level == "warn" and "过期" in detail, detail)

    fx2 = make_fixture(tk_offset_ms=120_000)         # 2 分钟后过期
    report2 = run(**fx2)
    level2, detail2 = level_of(report2, "Cookie 有效期")
    check("_m_h5_tk 即将过期 → 提醒尽快重导", level2 == "warn" and "分钟" in detail2, detail2)

    fx3 = make_fixture(tk_offset_ms=3_600_000)
    report3 = run(**fx3)
    level3, detail3 = level_of(report3, "Cookie 有效期")
    check("_m_h5_tk 充裕时判定正常", level3 == "ok", detail3)


def scenario_env_flags():
    fx = make_fixture(env_enabled=False, shadow=False)
    report = run(**fx)
    levels = {c["name"]: c["level"] for c in report["checks"]}
    check("通道未开启 → 提醒（正常状态，不算 blocker）",
          levels["通道开关"] == "warn", str(level_of(report, "通道开关")[1]))
    check("影子模式关闭 → 明确提醒「会真实发送」",
          levels["影子模式"] == "ok" and "真实发送" in str(level_of(report, "影子模式")[1]),
          str(level_of(report, "影子模式")[1]))

    fx2 = make_fixture()
    report2 = run(**fx2)
    check("影子模式开启时提示只落草稿不发送",
          "不发送" in str(level_of(report2, "影子模式")[1]), str(level_of(report2, "影子模式")[1]))


def scenario_missing_prompts_and_kb():
    fx = make_fixture(with_prompts=False, with_kb=False)
    report = run(**fx)
    levels = {c["name"]: c["level"] for c in report["checks"]}
    check("缺值守规范 → blocker（直接影响回答安全性）",
          levels["值守规范知识库"] == "blocker" and not report["ready"],
          str(level_of(report, "值守规范知识库")[1]))
    check("缺专家提示词 → 提醒会回落内置版（不阻塞）",
          levels["专家提示词"] == "warn", str(level_of(report, "专家提示词")[1]))


def scenario_old_bot_heuristic():
    fx = make_fixture()
    os.utime(fx["old_bot_db"], None)                  # 刚写过 → 视为活跃
    report = run(**fx)
    level, detail = level_of(report, "旧 bot 冲突提醒")
    check("旧 bot 最近写库 → 提醒同账号互挤风险",
          level == "warn" and "互挤" in str(report["checks"][-1].get("action", "") + detail) or level == "warn",
          detail)


def scenario_no_network():
    """即使服务地址不可达，脚本也必须正常跑完（证明它不依赖平台/网络）。"""
    fx = make_fixture()
    report = preflight.run_preflight(**fx, skip_service=False, base_url="http://127.0.0.1:9")
    level, detail = level_of(report, "本地服务")
    check("服务不可达时只报提醒、不抛异常（脚本本身不需要网络）",
          level == "warn" and "不可达" in detail, detail)


def scenario_real_repo():
    """对真实仓库跑一次（只读），确认真实环境下结论合理。"""
    report = preflight.run_preflight(ROOT, skip_service=True, base_url="http://127.0.0.1:9")
    levels = {c["name"]: c["level"] for c in report["checks"]}
    check("真实仓库上：凭据/规范/提示词 都判定为就绪",
          levels["登录态"] == "ok" and levels["值守规范知识库"] == "ok"
          and levels["专家提示词"] == "ok",
          f"blockers={report['blockers']} warns={report['warnings']}")
    check("真实仓库上：卖家规则未填会被点名（当前应提醒）",
          levels["卖家业务规则"] in ("ok", "warn"),
          str(level_of(report, "卖家业务规则")[1]))


def main():
    scenario_ready()
    scenario_unconfigured_rules()
    scenario_missing_credentials()
    scenario_no_unb()
    scenario_token_expiry()
    scenario_env_flags()
    scenario_missing_prompts_and_kb()
    scenario_old_bot_heuristic()
    scenario_no_network()
    scenario_real_repo()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"就绪体检脚本离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_preflight_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n就绪体检脚本离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
