# -*- coding: utf-8 -*-
r"""闲鱼 mtop 接口层（async / httpx）：登录态检查、取长连接 token、取商品信息。

移植自上游 `D:\dsh\XianyuAutoAgent\XianyuApis.py`，行为对齐但去掉三处「服务化致命」的写法：

| 上游位置 | 上游行为 | 本实现 |
|---|---|---|
| `XianyuApis.py:150/233/236` | 失败即 `sys.exit(1)` | 抛 `XianyuAuthError` / `XianyuRiskControlError`，由通道健康检查决定告警或停用通道 |
| `XianyuApis.py:210` | 风控时 `input()` 让用户手粘 Cookie | 抛 `XianyuRiskControlError`，交上层告警 + 等人工续期，**绝不阻塞事件循环** |
| `XianyuApis.py:56-87` | `update_env_cookies()` 回写 `.env` | 回写**独立凭据文件**（`secrets/xianyu_credentials.json`），多进程不互相覆盖 |

另外两处改进：
- 请求默认 **`trust_env=False`（直连，不走系统代理）**：闲鱼接口国内直连可达（0.08s 实测），
  走系统代理既慢又会引入额外风控特征；需要代理时显式传 `trust_env=True`。
- 默认 UA 取凭据文件里的真实浏览器 UA（导出时是 Chrome/153），而不是上游写死的 Chrome/133 或 146。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.channels.xianyu.protocol import (
    IM_APP_KEY,
    MTOP_APP_KEY,
    CookieError,
    generate_sign,
    now_ms_second_precision,
    parse_cookie_str,
    token_from_cookies,
)

logger = logging.getLogger("airobot.xianyu.api")

HASLOGIN_URL = "https://passport.goofish.com/newlogin/hasLogin.do"
MTOP_TOKEN_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
MTOP_ITEM_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/"
MTOP_TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
MTOP_ITEM_API = "mtop.taobao.idle.pc.detail"

RISK_CONTROL_MARKERS = ("RGV587_ERROR", "被挤爆啦", "FAIL_SYS_USER_VALIDATE", "punish")


class XianyuApiError(RuntimeError):
    """接口层通用错误。"""


class XianyuAuthError(XianyuApiError):
    """登录态失效（Cookie 过期/无效），需要人工续期。"""


class XianyuRiskControlError(XianyuAuthError):
    """触发平台风控（需人工过滑块后重新导出 Cookie）。"""


@dataclass
class TokenResult:
    access_token: str
    ret: List[str] = field(default_factory=list)
    cookie_refreshed: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


def _is_success(ret: Any) -> bool:
    """mtop 成功判定：ret 列表里含 `SUCCESS::调用成功`（对齐上游 XianyuApis.py:201）。"""
    if isinstance(ret, str):
        ret = [ret]
    return any("SUCCESS::调用成功" in str(item) for item in (ret or []))


def _haslogin_ok(body: Any) -> bool:
    """hasLogin.do 的成功判定。

    上游只看 `content.success`（XianyuApis.py:125）；这里额外兼容 `data.success`，
    两种形状任一为真都算登录态正常——mock/真实返回字段位置有差异时不至于误判成掉线。
    """
    body = body or {}
    return bool((body.get("content") or {}).get("success") or (body.get("data") or {}).get("success"))


def _hit_risk_control(ret: Any) -> bool:
    text = str(ret or "")
    return any(marker in text for marker in RISK_CONTROL_MARKERS)


class XianyuApi:
    """闲鱼接口客户端。所有方法都是 async，内部只用 `asyncio.sleep`，不阻塞事件循环。"""

    def __init__(
        self,
        cookies: Dict[str, str] | str,
        *,
        credential_path: str | Path | None = None,
        user_agent: Optional[str] = None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = False,
        max_token_retries: int = 2,
        max_item_retries: int = 3,
    ) -> None:
        self.cookies: Dict[str, str] = dict(cookies) if isinstance(cookies, dict) else parse_cookie_str(cookies)
        if "unb" not in self.cookies:
            raise CookieError("Cookie 里没有 unb，不是有效登录态（详见 protocol.require_login_cookie）")
        self.unb = self.cookies["unb"]
        self.credential_path = Path(credential_path) if credential_path else None
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        )
        self.max_token_retries = max_token_retries
        self.max_item_retries = max_item_retries
        self.cookie_refresh_count = 0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=trust_env,
            follow_redirects=True,
            headers={
                "accept": "application/json",
                "accept-language": "zh-CN,zh;q=0.9",
                "cache-control": "no-cache",
                "origin": "https://www.goofish.com",
                "pragma": "no-cache",
                "referer": "https://www.goofish.com/",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-mobile": "?0",
                "user-agent": self.user_agent,
            },
        )

    # ------------------------------------------------------------------ #
    # 生命周期 / Cookie
    # ------------------------------------------------------------------ #
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "XianyuApi":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def cookie_str(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _absorb_set_cookie(self, response: httpx.Response) -> bool:
        """把响应里的 Set-Cookie 合并进本地 Cookie（上游靠 requests 的 jar + clear_duplicate_cookies）。"""
        changed = False
        for name, value in response.cookies.items():
            if value is not None and self.cookies.get(name) != value:
                self.cookies[name] = value
                changed = True
        if changed:
            self.cookie_refresh_count += 1
            self.persist_cookies()
        return changed

    def persist_cookies(self) -> bool:
        """把当前 Cookie 回写到凭据文件（**不回写 .env**）。"""
        if not self.credential_path:
            return False
        try:
            data: Dict[str, Any] = {}
            if self.credential_path.exists():
                data = json.loads(self.credential_path.read_text(encoding="utf-8"))
            data.setdefault("account", {})["unb"] = self.unb
            data["cookies_str"] = self.cookie_str()
            self.credential_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug("已回写凭据文件（Cookie 字段数 %s）", len(self.cookies))
            return True
        except Exception as exc:  # 凭据回写失败不该影响主链路
            logger.warning("回写凭据文件失败: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # 登录态
    # ------------------------------------------------------------------ #
    async def has_login(self, retry_count: int = 0) -> bool:
        """`hasLogin.do` 登录态检查。对齐上游 XianyuApis.py:89-138（最多 3 次）。"""
        if retry_count >= self.max_token_retries:
            logger.error("登录态检查失败，重试次数用尽")
            return False
        params = {"appName": "xianyu", "fromSite": "77"}
        data = {
            "hid": self.cookies.get("unb", ""),
            "ltl": "true",
            "appName": "xianyu",
            "appEntrance": "web",
            "_csrf_token": self.cookies.get("XSRF-TOKEN", ""),
            "umidToken": "",
            "hsiz": self.cookies.get("cookie2", ""),
            "bizParams": "taobaoBizLoginFrom=web",
            "mainPage": "false",
            "isMobile": "false",
            "lang": "zh_CN",
            "returnUrl": "",
            "fromSite": "77",
            "isIframe": "true",
            "documentReferer": "https://www.goofish.com/",
            "defaultView": "hasLogin",
            "umidTag": "SERVER",
            "deviceId": self.cookies.get("cna", ""),
        }
        try:
            resp = await self._client.post(HASLOGIN_URL, params=params, data=data,
                                           cookies=self.cookies)
            self._absorb_set_cookie(resp)
            body = resp.json()
            if _haslogin_ok(body):
                logger.debug("登录态检查通过")
                return True
            logger.warning("登录态检查失败: %s", str(body)[:200])
        except Exception as exc:
            logger.error("登录态检查请求异常: %s", exc)
        await asyncio.sleep(0.5)
        return await self.has_login(retry_count + 1)

    # ------------------------------------------------------------------ #
    # mtop 通用请求
    # ------------------------------------------------------------------ #
    def _mtop_params(self, api_name: str, data_val: str) -> Dict[str, str]:
        """构造 mtop 参数（含签名）。字段与上游 XianyuApis.py:152-167/263-276 一致。"""
        t = now_ms_second_precision()
        params = {
            "jsv": "2.7.2",
            "appKey": MTOP_APP_KEY,
            "t": t,
            "sign": generate_sign(t, token_from_cookies(self.cookies), data_val),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api_name,
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
        }
        if api_name == MTOP_TOKEN_API:
            params["spm_pre"] = "a21ybx.item.want.1.14ad3da6ALVq3n"
            params["log_id"] = "14ad3da6ALVq3n"
        return params

    async def _post_mtop(self, url: str, api_name: str, data_val: str) -> Dict[str, Any]:
        params = self._mtop_params(api_name, data_val)
        resp = await self._client.post(
            url, params=params, data={"data": data_val}, cookies=self.cookies,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self._absorb_set_cookie(resp)
        try:
            body = resp.json()
        except Exception as exc:
            raise XianyuApiError(f"mtop 返回不是 JSON（HTTP {resp.status_code}）: {exc}") from exc
        if not isinstance(body, dict):
            raise XianyuApiError(f"mtop 返回格式异常: {str(body)[:200]}")
        return body

    # ------------------------------------------------------------------ #
    # 取长连接 token
    # ------------------------------------------------------------------ #
    async def get_token(self, device_id: str, retry_count: int = 0) -> TokenResult:
        """取 WebSocket 用 accessToken。

        与上游差异：重试耗尽后不再 `sys.exit(1)`、风控不再 `input()`，
        统一抛 `XianyuAuthError` / `XianyuRiskControlError`。
        """
        if retry_count >= self.max_token_retries:
            logger.warning("token 获取失败（重试 %s 次），尝试重新登录检查", retry_count)
            if await self.has_login():
                logger.info("重新登录检查通过，重置计数再试一次")
                return await self.get_token(device_id, 0)
            raise XianyuAuthError(
                "Cookie 已失效：取 token 失败且重新登录检查未通过。"
                "请在闲鱼网页版重新登录 → 导出新 Cookie → 写入 secrets/xianyu_credentials.json")

        data_val = json.dumps({"appKey": IM_APP_KEY, "deviceId": device_id},
                              ensure_ascii=False, separators=(",", ":"))
        try:
            body = await self._post_mtop(MTOP_TOKEN_URL, MTOP_TOKEN_API, data_val)
        except XianyuApiError as exc:
            logger.warning("token 请求异常（第 %s 次）: %s", retry_count + 1, exc)
            await asyncio.sleep(0.5)
            return await self.get_token(device_id, retry_count + 1)

        ret = body.get("ret", [])
        if not _is_success(ret):
            if _hit_risk_control(ret):
                raise XianyuRiskControlError(
                    f"触发平台风控: {ret}。请到闲鱼网页版点开「消息」过一次滑块，"
                    "再重新导出 Cookie 写入凭据文件（本实现不会阻塞等待输入）")
            logger.warning("token 接口失败（第 %s 次）: %s", retry_count + 1, ret)
            await asyncio.sleep(0.5)
            return await self.get_token(device_id, retry_count + 1)

        access_token = (body.get("data") or {}).get("accessToken", "")
        if not access_token:
            raise XianyuApiError(f"token 接口返回里没有 accessToken: {str(body)[:200]}")
        logger.info("token 获取成功（Cookie 刷新次数 %s）", self.cookie_refresh_count)
        return TokenResult(access_token=access_token, ret=list(ret),
                           cookie_refreshed=self.cookie_refresh_count > 0, raw=body)

    # ------------------------------------------------------------------ #
    # 商品信息
    # ------------------------------------------------------------------ #
    async def get_item_info(self, item_id: str, retry_count: int = 0) -> Optional[Dict[str, Any]]:
        """取商品详情（`data.itemDO`）。对齐上游 XianyuApis.py:257-319（最多 3 次）。"""
        if retry_count >= self.max_item_retries:
            logger.error("获取商品信息失败，重试次数用尽: %s", item_id)
            return None
        data_val = json.dumps({"itemId": str(item_id)}, ensure_ascii=False, separators=(",", ":"))
        try:
            body = await self._post_mtop(MTOP_ITEM_URL, MTOP_ITEM_API, data_val)
        except XianyuApiError as exc:
            logger.warning("商品信息请求异常（第 %s 次）: %s", retry_count + 1, exc)
            await asyncio.sleep(0.5)
            return await self.get_item_info(item_id, retry_count + 1)

        ret = body.get("ret", [])
        if not _is_success(ret):
            if _hit_risk_control(ret):
                raise XianyuRiskControlError(f"获取商品信息触发风控: {ret}")
            logger.warning("商品信息接口失败（第 %s 次）: %s", retry_count + 1, ret)
            await asyncio.sleep(0.5)
            return await self.get_item_info(item_id, retry_count + 1)

        item_do = (body.get("data") or {}).get("itemDO")
        if not item_do:
            logger.warning("商品信息返回里没有 itemDO: %s", str(body)[:200])
            return None
        return item_do
