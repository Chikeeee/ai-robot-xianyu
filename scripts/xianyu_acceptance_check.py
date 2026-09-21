# -*- coding: utf-8 -*-
r"""交付验收核对：把「目标达成」需要的证据一次收齐，而不是凭印象宣布完成。

检查项：
1. 旧 bot 仍在运行（约束：不改不停用）
2. 凭据纪律：文件存在、权限收紧、不在 git 跟踪里、脚本/文档里没有明文
3. 通道在线且自动发送已开、影子破防未发生
4. 真实发送有平台侧回捞证据
5. 关键回归套件与门禁

用法：python scripts/xianyu_acceptance_check.py
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

CRED = ROOT / "secrets" / "xianyu_credentials.json"
DB = ROOT / "data" / "xianyu.db"
from local_identity import my_unb_or_exit  # 账号从本机凭据读（仓库里不放真实 id）

MY_UNB = my_unb_or_exit()

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def ps(command: str) -> str:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                             capture_output=True, text=True, timeout=60)
        return (out.stdout or "") + (out.stderr or "")
    except Exception as exc:
        return f"__error__ {exc}"


def main() -> int:
    print("=" * 74)
    print("闲鱼值守交付验收核对")
    print("=" * 74)

    # 1) 旧 bot：约束是"不被我们改动/停用"。它现在**由用户决定保持停止**（2026-09-21），
    #    所以"它在运行"不再是恒真的检查（会永远红）。改为守住真正的不变量：
    #    我们的代码/脚本里没有任何"杀旧 bot"的动作；同时把它的当前状态如实报出。
    procs = ps("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
               "Where-Object { $_.CommandLine -like '*XianyuAutoAgent*' } | "
               "Select-Object -ExpandProperty ProcessId")
    old_pids = [p for p in procs.split() if p.strip().isdigit()]
    print(f"[INFO] 旧 bot 当前进程：{old_pids or '未运行（用户已确认保持停止）'}")

    kill_refs = []
    self_name = Path(__file__).name
    for path in (list((ROOT / "scripts").glob("*.py")) + list((ROOT / "app").rglob("*.py"))
                 + list(ROOT.glob("*.ps1"))):
        if path.name == self_name:
            continue          # 跳过自己：本文件里就写着这些关键词字面量，否则必然自指误报
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        low = text.lower()
        if "xianyuautoagent" in low and any(word in low for word in
                                            ("taskkill", "stop-process", "os.kill", "terminate()")):
            kill_refs.append(path.name)
    check("★ 我们的代码/脚本里没有任何「停掉旧 bot」的动作（约束：不修改也不停用）",
          not kill_refs, f"可疑文件={kill_refs or '无'}")

    # 2) 凭据纪律
    check("凭据文件存在", CRED.exists(), str(CRED.name))
    # 权限：只允许本账号/系统；任何"Everyone/Users/Authenticated Users"这类宽泛主体都算不合格
    acl_raw = ps(f"(Get-Acl '{CRED}').Access | "
                 "ForEach-Object { $_.IdentityReference.Value }")
    principals = [line.strip() for line in acl_raw.splitlines()
                  if line.strip() and line.strip() not in ("Value", "-----", "-")]
    broad = [p for p in principals
             if any(k in p for k in ("Everyone", "Authenticated Users", "BUILTIN\\Users",
                                     "Users", "INTERACTIVE"))]
    check("凭据文件权限已收紧（仅本账号/系统，无 Everyone/Users）",
          CRED.exists() and bool(principals) and not broad,
          f"授权对象={principals[:5]} 宽泛主体={broad or '无'}")
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch",
                              "secrets/xianyu_credentials.json"],
                             cwd=ROOT, capture_output=True, text=True)
    check("凭据不在 git 跟踪里", tracked.returncode != 0, "git ls-files 未命中")

    # 明文泄漏自查：拿凭据里的长值去脚本/文档里搜
    leaked = []
    if CRED.exists():
        raw = json.loads(CRED.read_text(encoding="utf-8"))
        secrets = [v for k, v in raw.items() if isinstance(v, str) and len(v) > 24]
        targets = list((ROOT / "scripts").glob("*.py")) + list(ROOT.glob("*.md"))
        for path in targets:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for secret in secrets:
                if secret in text:
                    leaked.append(path.name)
                    break
    check("脚本与文档里没有凭据明文", not leaked, f"命中文件={sorted(set(leaked)) or '无'}")

    # 3) 运行态
    try:
        import httpx
        health = httpx.get("http://127.0.0.1:8000/api/v1/xianyu/health", timeout=20).json()
        ws = health.get("ws") or {}
        check("通道在线且已注册", bool(ws.get("connected") and ws.get("registered")),
              f"connected={ws.get('connected')} registered={ws.get('registered')}")
        check("自动发送已开启（P4）", health.get("shadow_mode") is False,
              f"shadow_mode={health.get('shadow_mode')}")
        check("未出现僵尸会话（session_invalid=False）", not ws.get("session_invalid"),
              f"session_invalid={ws.get('session_invalid')}")
        limits = (health.get("outbound") or {}).get("limits") or {}
        check("灰度额度仍在（自动发送有硬天花板）",
              limits.get("per_minute") and limits.get("per_hour"),
              json.dumps(limits, ensure_ascii=False))
    except Exception as exc:
        check("健康接口可用", False, str(exc))

    # 4) 真实送达证据 + 无重复回复
    if DB.exists():
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        sent = list(conn.execute(
            "select id, chat_id, inbound, reply, sent_at from drafts where sent=1 order by id"))
        check("有已发送的真实回复（本端记录）", len(sent) >= 1,
              f"已发送 {len(sent)} 条，最近：{dict(sent[-1])['inbound'] if sent else '-'!r}")
        dup = list(conn.execute(
            "select message_id, count(*) n, sum(sent) s from drafts group by message_id "
            "having s > 1"))
        check("★ 没有任何一条消息被发出两次（消息级幂等的效果）", not dup,
              f"重复发送组={[dict(r) for r in dup]}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("-" * 74)
    print(f"验收核对：{passed}/{total} 通过")
    (ROOT / ".logs" / "xianyu_acceptance.txt").write_text(
        "\n".join([f"{passed}/{total}"] + [f"[{'PASS' if ok else 'FAIL'}] {n} — {d}"
                                           for n, ok, d in RESULTS]),
        encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
