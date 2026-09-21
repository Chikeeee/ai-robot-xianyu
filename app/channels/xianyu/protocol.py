# -*- coding: utf-8 -*-
r"""闲鱼（goofish）协议层：cookie 解析、签名、设备指纹、MessagePack 解码。

来源：从 `D:\dsh\XianyuAutoAgent`（上游 shaxiu/XianyuAutoAgent）移植，
按本项目需要做了 4 处**行为改进**（都在函数 docstring 里标注了上游位置与差异）：

1. 解码失败**不再静默降级**成 base64/hex 字符串（上游 `xianyu_utils.py:278-284、324-332`
   会把解析失败伪装成正常消息，导致拿乱码去问 LLM）——这里直接抛 `ProtocolDecodeError`。
2. `device_id` **持久化**（上游 `generate_device_id` 每次启动随机，与真实登录设备不一致，是风控特征）。
3. 不再 `sys.exit(1)`／不再 `input()`（上游 `main.py:31`、`XianyuApis.py:150/233/236` 会杀掉宿主进程）。
4. token 提取、cookie 解析等纯函数补齐了显式异常与类型标注，便于在 FastAPI 里被拦截和告警。

本模块**只做纯计算，不做任何网络请求**，因此可以在没有登录态、不连服务器的情况下完整单测。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import struct
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("airobot.xianyu.protocol")

# ---- 上游硬编码常量（协议要求，不要随意改）----
# 来源：XianyuAutoAgent/utils/xianyu_utils.py:63、XianyuApis.py:154/168、main.py:179
MTOP_APP_KEY = "34839810"
IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"
WS_URL = "wss://wss-goofish.dingtalk.com/"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

# UUIDv4 形状 + "-" + unb。注意第 4 段首字符是 variant 位，上游实现会生成 8/9/A/B，
# 所以这里必须允许 [89ABab]，不能只写 [89]（本机自测踩过这个坑）。
DEVICE_ID_RE = re.compile(
    r"^[0-9A-Za-z]{8}-[0-9A-Za-z]{4}-4[0-9A-Za-z]{3}-[89ABab][0-9A-Za-z]{3}-[0-9A-Za-z]{12}-\d+$"
)


class ProtocolDecodeError(ValueError):
    """协议层解码失败（base64/MessagePack/JSON 任一环节）。"""


class CookieError(ValueError):
    """Cookie 缺失或格式不对（例如没有 unb 字段）。"""


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #
def parse_cookie_str(cookies_str: str) -> Dict[str, str]:
    """把 `k=v; k=v` 形式的 Cookie 串解析成字典。

    与上游 `trans_cookies`（xianyu_utils.py:9-19）等价，只是把裸 `except: continue` 收紧为
    「跳过没有 `=` 的片段」，仍然保持「解析不出来不报错」的宽松语义。
    """
    cookies: Dict[str, str] = {}
    for chunk in (cookies_str or "").split("; "):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        cookies[key] = value
    return cookies


def build_cookie_str(cookies: Iterable[Dict[str, Any]]) -> str:
    """把浏览器扩展导出的 cookies 数组（`[{"name":..,"value":..}, ...]`）拼成请求用的 Cookie 串。

    上游没有这个函数（它要求用户手动复制一整条 Cookie）；有了它就能直接吃浏览器导出的 JSON。
    """
    parts = []
    for item in cookies or []:
        name = (item or {}).get("name")
        value = (item or {}).get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def require_login_cookie(cookies: Dict[str, str]) -> str:
    """校验这是有效的闲鱼登录态，返回 unb（用户 ID）。

    上游在 main.py:24-33 用 `sys.exit(1)` 处理这种情况；这里抛 `CookieError`，
    由调用方（通道健康检查）决定是告警还是重试，**绝不允许杀掉宿主进程**。
    """
    unb = cookies.get("unb")
    if not unb:
        raise CookieError(
            "Cookie 里没有 unb 字段，不是有效的闲鱼登录态。取法：浏览器登录 https://www.goofish.com "
            "→ F12 → Network → Fetch/XHR → 任一请求 → 复制请求头里完整的 Cookie（unb 是 HttpOnly，"
            "Console 里 document.cookie 拿不到）"
        )
    return unb


def load_credentials(path: str | Path) -> Dict[str, Any]:
    """读取凭据文件（独立 0600 文件，不进仓库）。返回 dict，至少含 cookies_str / account.unb。"""
    path = Path(path)
    if not path.exists():
        raise CookieError(f"凭据文件不存在: {path}（应先写入 secrets/xianyu_credentials.json）")
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = parse_cookie_str(data.get("cookies_str", ""))
    require_login_cookie(cookies)
    data["_cookies"] = cookies
    return data


def token_from_cookies(cookies: Dict[str, str]) -> str:
    """从 Cookie 的 `_m_h5_tk` 里取签名用的 token（下划线前半段）。

    上游：XianyuApis.py:189/284 `self.cookies['_m_h5_tk'].split('_')[0]`。
    """
    raw = cookies.get("_m_h5_tk", "")
    if not raw:
        raise CookieError("Cookie 里没有 _m_h5_tk，无法计算签名（需要重新导出登录态）")
    return raw.split("_")[0]


# --------------------------------------------------------------------------- #
# 标识符 / 签名
# --------------------------------------------------------------------------- #
def generate_mid() -> str:
    """生成消息 mid。上游 xianyu_utils.py:22-27（`{随机0-999}{毫秒时间戳} 0`）。"""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def generate_uuid() -> str:
    """生成消息 uuid。上游 xianyu_utils.py:30-33（`-{毫秒时间戳}1`）。"""
    timestamp = int(time.time() * 1000)
    return f"-{timestamp}1"


def generate_device_id(user_id: str) -> str:
    """生成一个 UUIDv4 形状的设备号并拼上用户 ID（**随机**）。

    上游 xianyu_utils.py:36-58 的等价实现。注意上游每次启动都重新随机，
    这里仅作为「首次生成」用；正常路径请用 `load_or_create_device_id` 持久化复用。
    """
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result: List[str] = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            result.append("-")
        elif i == 14:
            result.append("4")
        elif i == 19:
            rand_val = int(16 * random.random())
            result.append(chars[(rand_val & 0x3) | 0x8])
        else:
            rand_val = int(16 * random.random())
            result.append(chars[rand_val])
    return "".join(result) + "-" + user_id


def load_or_create_device_id(user_id: str, store_path: str | Path) -> str:
    """持久化设备号：同一个账号永远复用同一个 device_id。

    **改进点**：上游每次启动都随机生成（DEPLOY 记录与子代理分析都指出「与真实登录设备不一致」
    是额外风控特征）。设备号稳定下来，指纹才和真实浏览器一致。
    """
    store_path = Path(store_path)
    if store_path.exists():
        try:
            data = json.loads(store_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    device_id = data.get(user_id)
    if not device_id or not DEVICE_ID_RE.match(device_id):
        device_id = generate_device_id(user_id)
        data[user_id] = device_id
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已生成并持久化 device_id（账号 %s…）", user_id[:4])
    return device_id


def now_ms_second_precision() -> str:
    """签名用的时间戳：秒级精度 × 1000。

    上游 XianyuApis.py:155/266 就是 `str(int(time.time())*1000)`（**不是**毫秒时间戳）。
    """
    return str(int(time.time()) * 1000)


def generate_sign(t: str, token: str, data: str, app_key: str = MTOP_APP_KEY) -> str:
    """mtop 签名：md5(f"{token}&{t}&{app_key}&{data}")。

    上游 xianyu_utils.py:61-69。参数顺序固定，改顺序平台就会拒绝。
    """
    msg = f"{token}&{t}&{app_key}&{data}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# MessagePack（纯 Python 实现，仅覆盖闲鱼实际用到的类型）
# --------------------------------------------------------------------------- #
class MessagePackDecoder:
    """MessagePack 解码器。移植自上游 xianyu_utils.py:72-284。

    差异：`decode()` **失败即抛 `ProtocolDecodeError`**，不再返回 base64 字符串
    （上游会把解析失败伪装成正常消息，下游拿到乱码还会继续问 LLM）。
    """

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.length = len(data)

    # --- 基础读取 ---
    def read_byte(self) -> int:
        if self.pos >= self.length:
            raise ProtocolDecodeError("unexpected end of data")
        byte = self.data[self.pos]
        self.pos += 1
        return byte

    def read_bytes(self, count: int) -> bytes:
        if self.pos + count > self.length:
            raise ProtocolDecodeError("unexpected end of data")
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def read_uint8(self) -> int:
        return self.read_byte()

    def read_uint16(self) -> int:
        return struct.unpack(">H", self.read_bytes(2))[0]

    def read_uint32(self) -> int:
        return struct.unpack(">I", self.read_bytes(4))[0]

    def read_uint64(self) -> int:
        return struct.unpack(">Q", self.read_bytes(8))[0]

    def read_int8(self) -> int:
        return struct.unpack(">b", self.read_bytes(1))[0]

    def read_int16(self) -> int:
        return struct.unpack(">h", self.read_bytes(2))[0]

    def read_int32(self) -> int:
        return struct.unpack(">i", self.read_bytes(4))[0]

    def read_int64(self) -> int:
        return struct.unpack(">q", self.read_bytes(8))[0]

    def read_float32(self) -> float:
        return struct.unpack(">f", self.read_bytes(4))[0]

    def read_float64(self) -> float:
        return struct.unpack(">d", self.read_bytes(8))[0]

    def read_string(self, length: int) -> str:
        return self.read_bytes(length).decode("utf-8")

    # --- 递归解码 ---
    def decode_value(self) -> Any:
        if self.pos >= self.length:
            raise ProtocolDecodeError("unexpected end of data")
        fmt = self.read_byte()

        if fmt <= 0x7F:                      # positive fixint
            return fmt
        if 0x80 <= fmt <= 0x8F:              # fixmap
            return self.decode_map(fmt & 0x0F)
        if 0x90 <= fmt <= 0x9F:              # fixarray
            return self.decode_array(fmt & 0x0F)
        if 0xA0 <= fmt <= 0xBF:              # fixstr
            return self.read_string(fmt & 0x1F)
        if fmt == 0xC0:
            return None
        if fmt == 0xC2:
            return False
        if fmt == 0xC3:
            return True
        if fmt == 0xC4:
            return self.read_bytes(self.read_uint8())
        if fmt == 0xC5:
            return self.read_bytes(self.read_uint16())
        if fmt == 0xC6:
            return self.read_bytes(self.read_uint32())
        if fmt == 0xCA:
            return self.read_float32()
        if fmt == 0xCB:
            return self.read_float64()
        if fmt == 0xCC:
            return self.read_uint8()
        if fmt == 0xCD:
            return self.read_uint16()
        if fmt == 0xCE:
            return self.read_uint32()
        if fmt == 0xCF:
            return self.read_uint64()
        if fmt == 0xD0:
            return self.read_int8()
        if fmt == 0xD1:
            return self.read_int16()
        if fmt == 0xD2:
            return self.read_int32()
        if fmt == 0xD3:
            return self.read_int64()
        if fmt == 0xD9:
            return self.read_string(self.read_uint8())
        if fmt == 0xDA:
            return self.read_string(self.read_uint16())
        if fmt == 0xDB:
            return self.read_string(self.read_uint32())
        if fmt == 0xDC:
            return self.decode_array(self.read_uint16())
        if fmt == 0xDD:
            return self.decode_array(self.read_uint32())
        if fmt == 0xDE:
            return self.decode_map(self.read_uint16())
        if fmt == 0xDF:
            return self.decode_map(self.read_uint32())
        if fmt >= 0xE0:                      # negative fixint
            return fmt - 256
        raise ProtocolDecodeError(f"unknown format byte: 0x{fmt:02x}")

    def decode_array(self, size: int) -> List[Any]:
        return [self.decode_value() for _ in range(size)]

    def decode_map(self, size: int) -> Dict[Any, Any]:
        result: Dict[Any, Any] = {}
        for _ in range(size):
            key = self.decode_value()
            result[key] = self.decode_value()
        return result

    def decode(self) -> Any:
        return self.decode_value()


def _json_safe(obj: Any) -> Any:
    """把解码结果里可能出现的 bytes 转成字符串，保证可以再 json 序列化/落库。"""
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return base64.b64encode(obj).decode("utf-8")
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def decode_msgpack_b64(data: str) -> Any:
    """把 base64 的 MessagePack 负载解成 Python 对象。

    上游等价实现：`decrypt()`（xianyu_utils.py:287-332）。差异同上：失败抛异常不降级。
    """
    cleaned = "".join(c for c in (data or "")
                      if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    if not cleaned:
        raise ProtocolDecodeError("空负载")
    while len(cleaned) % 4 != 0:
        cleaned += "="
    try:
        raw = base64.b64decode(cleaned)
    except Exception as exc:
        raise ProtocolDecodeError(f"base64 解码失败: {exc}") from exc
    return _json_safe(MessagePackDecoder(raw).decode())


def decode_sync_payload(data: str) -> tuple[Any, str]:
    """解析同步包里的 `data` 字段，返回 (对象, 解码路径)。

    对齐上游 main.py:399-412 的「先 base64+JSON，失败再 MessagePack」两条路径，
    但把两条路径的结果都返回出来，便于在 traces 里看出到底是哪种帧。
    """
    try:
        plain = base64.b64decode(data).decode("utf-8")
        return json.loads(plain), "base64+json"
    except Exception:
        pass
    return decode_msgpack_b64(data), "base64+msgpack"
