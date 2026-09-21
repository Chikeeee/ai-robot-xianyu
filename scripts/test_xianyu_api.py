# -*- coding: utf-8 -*-
"""闲鱼接口层（api.py）离线自检：用 httpx.MockTransport 模拟 mtop 响应，不联网。

验证四件事：
1. 请求构造与上游一致（URL、mtop 参数、签名可自洽复算、表单负载）；
2. 成功路径解析正确（accessToken / itemDO）；
3. 失败路径是**抛异常**而不是 `sys.exit(1)` 或 `input()` 阻塞（上游行为对比写在断言说明里）；
4. Cookie 刷新只回写独立凭据文件，绝不碰 `.env`。

用法：python scripts/test_xianyu_api.py
"""
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.channels.xianyu.api import (  # noqa: E402
    HASLOGIN_URL,
    IM_APP_KEY,
    MTOP_APP_KEY,
    MTOP_ITEM_URL,
    MTOP_TOKEN_URL,
    XianyuApi,
    XianyuAuthError,
    XianyuRiskControlError,
)

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"
TMP_CRED = ROOT / ".logs" / "xianyu_credentials_testcopy.json"
ENV_FILE = ROOT / ".env"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class FakeServer:
    """按脚本返回响应的假 mtop 服务器，同时记录收到的请求。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for idx, (matcher, resp) in enumerate(self.script):
            if matcher(request):
                self.script.pop(idx)
                return resp(request) if callable(resp) else resp
        return httpx.Response(404, json={"ret": ["FAIL::没有匹配的脚本响应"]})

    def transport(self):
        return httpx.MockTransport(self.handler)


def ok_ret(payload, ret=None, set_cookie=None):
    body = {"ret": ret or ["SUCCESS::调用成功"], "data": payload}
    headers = {}
    if set_cookie:
        headers["set-cookie"] = set_cookie
    return httpx.Response(200, json=body, headers=headers)


def haslogin_ok():
    """hasLogin.do 的成功形状（上游读的是 content.success）。"""
    return httpx.Response(200, json={"content": {"success": True}})


def haslogin_fail():
    return httpx.Response(200, json={"content": {"success": False}})


def fail_ret(ret):
    return httpx.Response(200, json={"ret": ret, "data": {}})


def cred_cookies():
    data = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    return data


def make_api(script, **kw):
    server = FakeServer(script)
    cookies = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    from app.channels.xianyu.protocol import parse_cookie_str
    return XianyuApi(parse_cookie_str(cookies["cookies_str"]),
                     credential_path=TMP_CRED, transport=server.transport(), **kw), server


def is_haslogin(req):
    return "hasLogin.do" in str(req.url)


def is_token(req):
    return "login.token" in str(req.url)


def is_item(req):
    return "idle.pc.detail" in str(req.url)


async def main():
    # 准备：凭据副本（测试只允许动这个副本）
    TMP_CRED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REAL_CRED, TMP_CRED)
    env_before = ENV_FILE.read_bytes()

    # ---------------- 1. has_login 成功 ----------------
    api, server = make_api([(is_haslogin, haslogin_ok())])
    ok = await api.has_login()
    req = server.requests[0]
    qs = parse_qs(urlparse(str(req.url)).query)
    form = parse_qs(req.content.decode())
    check("hasLogin 成功返回 True，请求构造与上游一致", ok and urlparse(str(req.url)).path == "/newlogin/hasLogin.do"
          and qs.get("appName") == ["xianyu"] and qs.get("fromSite") == ["77"]
          and form.get("ltl") == ["true"] and form.get("fromSite") == ["77"]
          and form.get("deviceId") == [api.cookies.get("cna", "")],
          f"params={sorted(qs)} form 字段数={len(form)}")
    await api.aclose()

    # ---------------- 2. has_login 失败：返回 False 而不是退出进程 ----------------
    fake = FakeServer([(is_haslogin, haslogin_fail())])
    api = XianyuApi({"unb": "1", "cna": "x"}, transport=fake.transport(), credential_path=None)
    api.max_token_retries = 2
    ok = await api.has_login()
    check("hasLogin 连续失败返回 False（与上游重试次数一致：1 次 + 1 次重试，且不退出进程）",
          ok is False and len(fake.requests) == 2, f"共请求 {len(fake.requests)} 次")
    await api.aclose()

    # ---------------- 3. get_token 成功 + 签名自洽 + 请求字段 ----------------
    api, server = make_api([(is_token, ok_ret({"accessToken": "TOKEN-ABC"}))])
    result = await api.get_token("dev-123")
    req = server.requests[0]
    qs = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
    form = parse_qs(req.content.decode())
    data_val = form.get("data", [""])[0]
    expected_sign = hashlib.md5(
        f"{api.cookies['_m_h5_tk'].split('_')[0]}&{qs['t']}&{MTOP_APP_KEY}&{data_val}".encode()).hexdigest()
    check("get_token 成功解析 accessToken", result.access_token == "TOKEN-ABC", f"ret={result.ret}")
    check("mtop 参数与上游一致（jsv/appKey/api/sessionOption/spm）",
          qs.get("appKey") == MTOP_APP_KEY and qs.get("api") == "mtop.taobao.idlemessage.pc.login.token"
          and qs.get("jsv") == "2.7.2" and qs.get("sessionOption") == "AutoLoginOnly"
          and qs.get("spm_cnt") == "a21ybx.im.0.0" and qs.get("v") == "1.0",
          f"api={qs.get('api')}")
    check("签名可被离线复算（token&t&appKey&data 的 md5）",
          qs.get("sign") == expected_sign and bool(re.fullmatch(r"[0-9a-f]{32}", qs.get("sign", ""))),
          f"sign={qs.get('sign', '')[:12]}… t={qs.get('t')}")
    check("t 是「秒级精度 × 1000」（13 位，对齐上游）", bool(re.fullmatch(r"\d{13}", qs.get("t", ""))), qs.get("t", ""))
    check("表单负载含 IM appKey 与 deviceId",
          json.loads(data_val).get("appKey") == IM_APP_KEY and json.loads(data_val).get("deviceId") == "dev-123",
          data_val)
    check("请求 URL 与上游一致", str(req.url).startswith(MTOP_TOKEN_URL), MTOP_TOKEN_URL)
    await api.aclose()

    # ---------------- 4. Cookie 刷新：只回写凭据文件，不碰 .env ----------------
    new_token = "0123456789abcdef0123456789abcdef_1789999999999"
    api, server = make_api([
        (is_token, ok_ret({"accessToken": "T1"}, set_cookie=f"_m_h5_tk={new_token}; Path=/; Domain=.goofish.com")),
    ])
    await api.get_token("dev-1")
    cred_after = json.loads(TMP_CRED.read_text(encoding="utf-8"))
    check("Set-Cookie 被吸收并回写凭据文件",
          api.cookies.get("_m_h5_tk") == new_token and new_token in cred_after["cookies_str"],
          f"cookie 刷新计数={api.cookie_refresh_count}")
    check("凭据文件保留原有其它字段（未被覆盖成空文件）",
          cred_after.get("account", {}).get("unb") and cred_after.get("notes"),
          f"字段={sorted(cred_after)}")
    await api.aclose()

    # ---------------- 5. 风控：抛异常，绝不 input() 阻塞 ----------------
    api, server = make_api([(is_token, fail_ret(["FAIL_SYS_USER_VALIDATE::RGV587_ERROR::SM::被挤爆啦，请稍后重试"]))])
    raised = None
    try:
        await asyncio.wait_for(api.get_token("dev-1"), timeout=5)
    except XianyuRiskControlError as exc:
        raised = exc
    except asyncio.TimeoutError:
        raised = "TIMEOUT（说明在阻塞等待输入）"
    check("风控错误抛 XianyuRiskControlError 且不阻塞（上游此处 input() 等人工粘贴）",
          isinstance(raised, XianyuRiskControlError), f"{type(raised).__name__ if raised else 'None'}")
    await api.aclose()

    # ---------------- 6. token 连续失败 + 登录检查失败 → XianyuAuthError ----------------
    api, server = make_api([
        (is_token, fail_ret(["FAIL_SYS_TOKEN_EXPIRED::令牌过期"])),
        (is_haslogin, haslogin_fail()),
    ])
    raised = None
    try:
        await api.get_token("dev-1")
    except XianyuAuthError as exc:
        raised = exc
    check("Cookie 失效抛 XianyuAuthError（上游此处 sys.exit(1)）",
          isinstance(raised, XianyuAuthError) and not isinstance(raised, XianyuRiskControlError),
          f"{type(raised).__name__ if raised else 'None'}；token 请求 {sum(1 for r in server.requests if is_token(r))} 次")
    await api.aclose()

    # ---------------- 7. 商品信息 ----------------
    api, server = make_api([(is_item, ok_ret({"itemDO": {"title": "测试商品", "soldPrice": 99.5, "desc": "描述"}}))])
    item = await api.get_item_info("123456")
    qs = {k: v[0] for k, v in parse_qs(urlparse(str(server.requests[0].url)).query).items()}
    check("get_item_info 解析 itemDO",
          item and item.get("title") == "测试商品" and qs.get("api") == "mtop.taobao.idle.pc.detail",
          f"api={qs.get('api')} price={item.get('soldPrice') if item else None}")
    await api.aclose()

    api, server = make_api([(is_item, fail_ret(["FAIL_BIZ_ITEM_NOT_FOUND::商品不存在"]))])
    item = await api.get_item_info("000")
    check("商品不存在：重试 3 次后返回 None（不抛、不退出）",
          item is None and sum(1 for r in server.requests if is_item(r)) == 3,
          f"请求 {len(server.requests)} 次")
    await api.aclose()

    # ---------------- 8. 不碰 .env + 代理默认直连 ----------------
    env_after = ENV_FILE.read_bytes()
    check(".env 全程未被修改（Cookie 只进凭据文件）", env_before == env_after,
          f"前后字节数 {len(env_before)}/{len(env_after)}")
    api, _ = make_api([])
    trust_env = getattr(api._client, "_trust_env", None)
    check("默认直连（trust_env=False），避免闲鱼流量走系统代理",
          trust_env is False, f"trust_env={trust_env}")
    await api.aclose()

    # ---------------- 汇总 ----------------
    TMP_CRED.unlink(missing_ok=True)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"闲鱼接口层离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "") for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_api_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n闲鱼接口层离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
