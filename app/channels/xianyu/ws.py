# -*- coding: utf-8 -*-
r"""闲鱼 WSS 值守连接：注册、心跳、token 刷新、断线重连、入站帧解析。

移植自上游 `D:\dsh\XianyuAutoAgent\main.py`（连接与收帧部分），并按 7×24 值守需要修掉 5 个问题：

| 上游位置 | 上游行为 | 本实现 |
|---|---|---|
| `main.py:599-614` | 任意 `code==200` 帧都算心跳响应 | **按心跳 mid 精确匹配**才对账，超时即判定连接不可用 |
| `main.py:662-674` + `:369-382` | 同一帧 ACK 两次 | 收帧入口**只 ACK 一次** |
| 无 | 无消息幂等 | 每个入站帧按 mid 去重（`DedupeStore`，默认内存版，P0-6 换 SQLite） |
| `main.py:712` | 固定 `sleep(5)` 重连 | **指数退避**（1s→2s→4s…上限 60s），token 刷新后立即重连 |
| `main.py:518/502` | 同步阻塞调用（LLM/HTTP）跑在收帧循环里 | 本模块**只负责协议与连接**，入站消息交给 `on_message` 回调（由 engine 丢线程池），收帧循环永不做网络/LLM 调用 |

出站帧构造也在这里（`build_text_frame`），但**合规过滤/限速/拟人化延迟属于业务策略**，放在 `outbound.py`。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from websockets.asyncio.client import connect as ws_connect

from app.channels.xianyu.api import XianyuApi, XianyuAuthError, XianyuRiskControlError
from app.channels.xianyu.protocol import (
    IM_APP_KEY,
    WS_URL,
    ProtocolDecodeError,
    decode_sync_payload,
    generate_mid,
    generate_uuid,
)

logger = logging.getLogger("airobot.xianyu.ws")

CHAT_ACK_TEMPLATE: Dict[str, Any] = {"code": 200}

# 上游 main.py:62 的默认接管词是中文句号
DEFAULT_TOGGLE_KEYWORDS = "。"
# 上游 main.py:59 的消息时效：5 分钟
DEFAULT_MESSAGE_EXPIRE_MS = 300_000


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class InboundMessage:
    """一条买家（或卖家）聊天消息。"""

    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    item_id: Optional[str]
    create_time_ms: int
    from_seller: bool = False
    # 命中的负载形状（如 "1.10" / "compact"）：平台换过嵌套时，靠它一眼看出走了哪条分支
    shape: str = ""
    # 幂等键来源（payload_id / content / outer_mid）：排查"消息被判成重复丢掉"必须看它
    id_source: str = ""
    # 走了 outer_mid 兜底时的父层诊断（键名与类型），只在这种异常路径上才有值
    id_debug: str = ""
    # 内容类型（1=买家文本，14=平台提示卡；紧凑形没有该字段则为 None）
    content_type: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _structure(obj: Any, depth: int = 0, preview_len: int = 24) -> Any:
    """把帧/负载压成「结构摘要」（字段名 + 类型 + 列表长度），用于排查平台到底推了什么。

    短字符串（≤ preview_len）会带上值——排查「消息到底长什么样」必须看到 id 这类短字段；
    长文本只记长度，避免把买家正文写进诊断文件。
    """
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {str(k): _structure(v, depth + 1, preview_len) for k, v in list(obj.items())[:20]}
    if isinstance(obj, list):
        return {"__list_len__": len(obj), "sample": _structure(obj[0], depth + 1, preview_len) if obj else None}
    if isinstance(obj, str):
        return obj if len(obj) <= preview_len else f"str[{len(obj)}]"
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, (int, float)):
        return obj if depth <= 1 else "num"
    if isinstance(obj, bytes):
        return f"bytes[{len(obj)}]"
    return type(obj).__name__


class FrameDump:
    """原始帧诊断：把每帧的**结构摘要**（必要时含原始负载）追加到文件，带大小上限自动轮转。

    用途：买家消息「没动静」时，判断平台到底有没有推、推的字段是什么形状。
    默认只写结构摘要；`XIANYU_FRAME_DUMP_FULL=true` 时连同负载一起写（排查消息字段时必须），
    内容仅本机 `.logs/`，且截断到 `max_payload` 字符。
    """

    def __init__(self, path: str | Path, max_bytes: int = 5_000_000,
                 full: Optional[bool] = None, max_payload: int = 4000) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_payload = max_payload
        if full is None:
            full = os.getenv("XIANYU_FRAME_DUMP_FULL", "false").strip().lower() in ("1", "true", "yes", "on")
        self.full = full

    def write(self, record: Dict[str, Any], payload: Any = None) -> None:
        try:
            if payload is not None and self.full:
                try:
                    text = json.dumps(payload, ensure_ascii=False, default=str)
                except Exception:
                    text = repr(payload)
                record = {**record, "payload": text[: self.max_payload]}
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                self.path.write_text("", encoding="utf-8")
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.debug("写帧诊断文件失败", exc_info=True)


class DedupeStore(Protocol):
    """消息幂等存储（同一帧重复推送只处理一次）。"""

    def seen(self, key: str) -> bool:
        ...

    def mark(self, key: str) -> None:
        ...


class InMemoryDedupe:
    """内存版幂等存储（进程重启即失效）；SQLite 版随 `session.py` 落地。"""

    def __init__(self, max_entries: int = 5000) -> None:
        self._keys: Dict[str, float] = {}
        self._max = max_entries

    def seen(self, key: str) -> bool:
        return key in self._keys

    def mark(self, key: str) -> None:
        self._keys[key] = time.time()
        if len(self._keys) > self._max:
            for k, _ in sorted(self._keys.items(), key=lambda kv: kv[1])[: len(self._keys) // 4]:
                self._keys.pop(k, None)


class SyncCursor:
    r"""同步游标（pts）持久化：让「断线窗口内的消息」能被平台补推回来。

    为什么需要：`pts=now`（上游做法）等于告诉平台「我已经同步到现在了」，于是
    **掉线期间买家发的消息永远不会补推**——7×24 值守里每次重连都是一个静默丢消息的窗口
    （本机实测：本通道上线前的 4 条买家消息就是这样一条都没进链路）。
    `pts=0`（zero）会全量重放，但每次都重放太吵。折中：把上次同步到的 `maxPts` 存盘，
    重连时从那儿续——平台只补推这之后的消息。

    安全阀：游标为 0、或明显超出当前时间（时钟/字段异常）时退回 `now`，绝不用可疑值去要历史。
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._pts = 0
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._pts = int(data.get("pts") or 0)
            except Exception:
                logger.warning("同步游标文件损坏，忽略并从 0 开始: %s", self.path)
                self._pts = 0

    def get(self) -> int:
        return self._pts

    def sane(self, now_ms: Optional[int] = None) -> int:
        """返回可安全使用的游标（0 表示没有可用游标）。

        上界只给 1 小时余量：pts 是微秒时间戳，若拿一个「未来很久」的值去要历史，
        平台可能回一大坨数据或者干脆不认——宁可退回 now（只是这一轮不补推），
        也不拿可疑值去赌。
        """
        now_ms = now_ms or int(time.time() * 1000)
        upper = now_ms * 1000 + 3600 * 1_000_000
        if self._pts <= 0 or self._pts > upper:
            return 0
        return self._pts

    def update(self, pts: Any) -> bool:
        """记录新的游标（只在变大时落盘）。"""
        try:
            value = int(pts or 0)
        except Exception:
            return False
        if value <= self._pts:
            return False
        self._pts = value
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"pts": value, "updated_at": time.time()}),
                                     encoding="utf-8")
            except Exception:
                logger.debug("同步游标写盘失败（不影响值守）", exc_info=True)
        return True


# --------------------------------------------------------------------------- #
# 帧分类（逐条对齐上游 main.py:198-267）
# --------------------------------------------------------------------------- #
def is_sync_package(frame: Any) -> bool:
    try:
        return (isinstance(frame, dict) and "body" in frame
                and "syncPushPackage" in frame["body"]
                and "data" in frame["body"]["syncPushPackage"]
                and len(frame["body"]["syncPushPackage"]["data"]) > 0)
    except Exception:
        return False


def history_text(message: Dict[str, Any]) -> tuple:
    r"""从「历史消息」的 `message` 节点里提取文本，返回 `(text, source)`。

    真机字段（`/r/MessageManager/listUserMessages` 实测抓包）：

        message.content.custom.data = base64('{"atUsers":[],"contentType":1,"text":{"text":"你好"}}')
        message.content.custom.summary = "你好"
        message.extension.reminderContent = "你好"

    `source` 会原样回传给调用方——**解析失败必须与「消息本来就是空的」区分开**，
    否则排查时会把「字段结构变了」误读成「对方没说话」。
    """
    text = ""
    source = "none"
    try:
        content = message.get("content") or {}
        custom = content.get("custom") or {} if isinstance(content, dict) else {}
        data = custom.get("data") if isinstance(custom, dict) else None
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "ignore")
        if data:
            try:
                payload = json.loads(base64.b64decode(data).decode("utf-8"))
                candidate = (payload.get("text") or {}).get("text", "")
                if candidate:
                    return candidate, "custom.data.text"
            except Exception:
                pass
        # 回退链：summary → reminderContent → detailNotice（都拿不到才算空消息）
        for origin, value in (("custom.summary", custom.get("summary") if isinstance(custom, dict) else None),
                              ("extension.reminderContent", (message.get("extension") or {}).get("reminderContent")),
                              ("extension.detailNotice", (message.get("extension") or {}).get("detailNotice"))):
            if value:
                text, source = str(value), origin
                break
    except Exception:
        return "", "error"
    return text, source


def history_content_type(message: Dict[str, Any]) -> Optional[int]:
    """`message.content.custom.type`：真机上 **1=买家真实文本**，14=平台/客户端注入的提示卡。

    实测抓包里「恭喜新手卖家，您的宝贝有人来询单啦…」那条 `type=14`，而它的 `senderUserId`
    **仍是买家 id**——所以只看发送者无法区分，必须看这个类型，否则会把平台提示当买家提问去回复。
    """
    try:
        custom = (message.get("content") or {}).get("custom") or {}
        value = custom.get("type")
        return int(value) if value is not None else None
    except Exception:
        return None


def parse_history_message(model: Dict[str, Any]) -> Dict[str, Any]:
    """解析一条历史消息模型（`userMessageModels[*]`）。"""
    message = model.get("message") or {}
    ext = message.get("extension") or {}
    text, source = history_text(message)
    content_type = history_content_type(message)
    created = message.get("createTime") or message.get("createAt")
    created_iso = None
    try:
        if created:
            created_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(created) / 1000))
    except Exception:
        created_iso = None
    return {
        "sender_id": ext.get("senderUserId"),
        "sender_name": ext.get("reminderTitle"),
        "text": text,
        "text_source": source,
        "content_type": content_type,
        # 只有 type=1（或字段缺失）才算买家真实文本；type=14 等是平台注入的提示卡
        "is_plain_text": content_type in (None, 1),
        "create_time": created,
        "create_time_iso": created_iso,
        "message_id": message.get("messageId"),
        "cid": message.get("cid"),
    }


def chat_message_content_type(node_parent: Any, node: Dict[str, Any]) -> Optional[int]:
    """取内容类型：完整消息形把它放在 `1.6.3.4`（1=买家真实文本，14=平台注入的提示卡）。

    紧凑提醒形没有这个字段，返回 None（此时按「可能是真实消息」处理，宁可多回一句也不漏）。
    """
    try:
        six = mget(node_parent, "6")
        if isinstance(six, dict):
            inner = mget(six, "3")
            if isinstance(inner, dict):
                value = mget(inner, "4")
                return int(value) if value is not None else None
    except Exception:
        return None
    return None


def _id_debug(parent: Any, inner_mid: Any) -> str:
    """幂等键走了兜底时，记录父层长什么样（键名 + 类型 + 关键字段的取值类型）。

    只用于诊断：把「父层为什么取不到 msgId / 创建时间」写进事件，避免再靠猜。
    """
    try:
        if not isinstance(parent, dict):
            return f"parent={type(parent).__name__}"
        # 用 repr：键如果是 bytes 会显示成 b'3'，是 int 会显示成 3，带不可见字符会显示成转义——
        # 用 str() 全都长得一样，上一轮就是这么被误导的
        keys = ",".join(f"{k!r}:{type(v).__name__}" for k, v in list(parent.items())[:14])
        return (f"parent_keys=[{keys}] "
                f"get3={type(parent.get('3')).__name__} get5={type(parent.get('5')).__name__} "
                f"inner={inner_mid!r}"[:300])
    except Exception as exc:  # 诊断本身绝不能影响值守
        return f"debug_error={type(exc).__name__}"


def mget(mapping: Any, *names: Any) -> Any:
    r"""按名字取值，**兼容 msgpack 解出来的整数键**。

    真机实测（这是本项目最隐蔽的一个坑）：平台下发的同步负载里，**外层消息表的键是整数**
    （msgpack 把 "1"/"2"/"3"… 编成 int 1/2/3…），而内层扩展表（`10`）的键是字符串。
    于是 `payload["1"]`、`parent.get("2")` 这类写法在真机上**全部取不到值**：

    - `frame.get("1")` → None（键是 int 1）
    - `parent.get("2")`（会话 id）→ None → **chat_id 变成空字符串**（所有买家的会话被并成一个）
    - `parent.get("3")`（消息 id）、`parent.get("5")`（时间）→ None → 幂等键退化成外层 mid

    更早的「消息被静默丢弃」也是同一个根因：老代码用 `"10" in frame["1"]` 判定，键是 int 就恒为 False。
    离线自检之所以没发现，是因为早期夹具用的是字符串键（JSON 反序列化的样子），
    与真机的 msgpack 负载不一致。

    这里按 str → int → bytes 三种键依次尝试；`names` 可以给多个别名。
    """
    if not isinstance(mapping, dict):
        return None
    for name in names:
        if name in mapping:
            return mapping[name]
        if isinstance(name, str):
            if name.isdigit():
                try:
                    if int(name) in mapping:
                        return mapping[int(name)]
                except Exception:
                    pass
            try:
                if name.encode("utf-8") in mapping:
                    return mapping[name.encode("utf-8")]
            except Exception:
                pass
        elif isinstance(name, int):
            if str(name) in mapping:
                return mapping[str(name)]
    return None


def has_key(mapping: Any, *names: Any) -> bool:
    """`mget` 的判定版（键类型无关，且值可以是假值如 0/""）。"""
    return any(_key_present(mapping, name) for name in names)


def _key_present(mapping: Any, name: Any) -> bool:
    if not isinstance(mapping, dict):
        return False
    if name in mapping:
        return True
    if isinstance(name, str):
        if name.isdigit() and int(name) in mapping:
            return True
        return name.encode("utf-8") in mapping
    if isinstance(name, int):
        return str(name) in mapping
    return False


def auth_error(frame: Any) -> Optional[str]:
    r"""识别「会话已失效」帧，返回原因字符串（否则 None）。

    真机实测：平台在长连接上**不关连接**，只是开始对每条请求回
    `code=401 body.reason="token is not found" / body.code="4000001" / scope="reg"`。
    这类帧以前被当普通非同步帧丢掉，于是 health 一直显示 connected=true/registered=true，
    实际已经收不到任何消息——「静默僵尸连接」，是 7×24 值守最危险的盲区。
    """
    try:
        if not isinstance(frame, dict):
            return None
        code = frame.get("code")
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        reason = str(body.get("reason") or "")
        dev = str(body.get("developerMessage") or "")
        bcode = str(body.get("code") or "")
        if code in (401,) or bcode == "4000001" or "token is not found" in reason \
                or "token is not found" in dev or "未登录" in reason:
            return reason or dev or f"code={code} body.code={bcode}"
        return None
    except Exception:
        return None


TEXT_KEYS = ("reminderContent", "detailNotice", "content")


def find_message_node(frame: Any, max_depth: int = 6):
    r"""在同步负载里定位「消息节点」，返回 `(parent, node, path)`；找不到返回 `(None, None, "")`。

    **为什么不能只认一种嵌套**：真机实测平台发过至少三种形状——

    1. 紧凑提醒形：`{"1": {"2": cid, "5": ms, "10": {"reminderContent": "你好", "senderUserId": ...}}}`
    2. **完整消息形**（本次真机抓到、且被旧代码丢掉的那种）：
       `{"1": {"1": {"1": "<买家>@goofish"}, "2": cid, "3": "<msgId>.PNM", "5": ms,
                "6": {"1": 101, "3": {..., "5": "<内容 JSON>"}},
                "10": {"reminderContent": "你好", "detailNotice": "你好", "senderUserId": ...}}, "3": {...}}`
    3. 历史接口形（见 `parse_history_message`）

    旧实现写死 `frame["1"]["10"]["reminderContent"]`，形状 2 恰好也满足，但形状再变一次就会
    **静默丢消息**（`is_chat_message` 把所有异常吞成 False，最后落到 `not_chat` 丢掉——
    本次排查就卡在这里）。改成**有界递归查找**：找到含文本键的 dict 就当消息节点，
    并回传路径用于观测。
    """
    if not isinstance(frame, dict):
        return None, None, ""

    def scan(node: Any, path: str, depth: int):
        if depth > max_depth:
            return None, None, ""
        if isinstance(node, dict):
            if has_key(node, *TEXT_KEYS):
                # 命中：父层由调用方补（cid/时间/msgId 都在父层，必须回传正确的父子关系）
                return None, node, path or "root"
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    parent, found, found_path = scan(value, f"{path}.{key}", depth + 1)
                    if found is not None:
                        return (parent if parent is not None else node), found, found_path
            return None, None, ""
        if isinstance(node, list):
            for index, item in enumerate(node):
                parent, found, found_path = scan(item, f"{path}[{index}]", depth + 1)
                if found is not None:
                    return (parent if parent is not None else node), found, found_path
            return None, None, ""
        return None, None, ""

    # 优先从 "1"（真机固定的消息入口）往下找，找不到再全量扫。
    # 注意键类型：真机是 int 键，用 mget 才取得到
    roots = [("1", mget(frame, "1"))]
    for key, value in frame.items():
        if key in ("1", 1, b"1"):
            continue
        roots.append((str(key), value))
    for key, value in roots:
        parent, node, path = scan(value, str(key), 1)
        if node is not None:
            return (parent if parent is not None else frame), node, path
    return None, None, ""


def chat_message_text(node: Dict[str, Any]) -> str:
    """从消息节点取正文（reminderContent → detailNotice → content.text）。键类型无关。"""
    value = mget(node, "reminderContent", "detailNotice")
    if value:
        return str(value)
    content = mget(node, "content")
    if isinstance(content, dict):
        text = mget(content, "text")
        if isinstance(text, dict) and mget(text, "text"):
            return str(mget(text, "text"))
    return ""


def chat_message_sender(parent: Dict[str, Any], node: Dict[str, Any]) -> str:
    """取发送者 id。完整消息形把发送者放在父层的 `1.1`（形如 `<uid>@goofish`）。"""
    sender = mget(node, "senderUserId")
    if sender:
        return str(sender).split("@")[0]
    if isinstance(parent, dict):
        inner = mget(parent, "1")
        if isinstance(inner, dict):
            inner = mget(inner, "1")
        if isinstance(inner, str):
            return inner.split("@")[0]
    return ""


def is_chat_message(frame: Any) -> bool:
    """紧凑提醒形判定（键类型无关：真机外层键是 int）。"""
    try:
        one = mget(frame, "1")
        if not isinstance(one, dict):
            return False
        ten = mget(one, "10")
        return isinstance(ten, dict) and has_key(ten, "reminderContent")
    except Exception:
        return False


def is_conversation_list(frame: Any) -> bool:
    """是否是「会话列表」推送（真机里紧挨着消息帧出现，形如 `{"1": [{"1": "<cid>@goofish", "2": .., "3": .., "4": "<uid>@goofish"}]}`）。

    上游把这类帧一律当「正在输入」丢掉——不会丢消息，但会让计数器骗人（排查时误导）。这里单独识别。
    """
    try:
        one = mget(frame, "1")
        if not (isinstance(one, list) and one):
            return False
        item = one[0]
        if not isinstance(item, dict):
            return False
        head = mget(item, "1")
        return isinstance(head, str) and "@goofish" in head and len(item) > 1
    except Exception:
        return False


def is_typing_status(frame: Any) -> bool:
    try:
        one = mget(frame, "1")
        if not (isinstance(one, list) and one and isinstance(one[0], dict)):
            return False
        head = mget(one[0], "1")
        return isinstance(head, str) and "@goofish" in head
    except Exception:
        return False


def is_system_message(frame: Any) -> bool:
    try:
        three = mget(frame, "3")
        return isinstance(three, dict) and mget(three, "needPush") == "false"
    except Exception:
        return False


def is_bracket_system_message(text: Any) -> bool:
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def order_status(frame: Any) -> Optional[str]:
    """订单状态提醒（上游 main.py:416-430 只记日志，这里把状态回传给 engine 供后续扩展）。"""
    try:
        three = mget(frame, "3")
        return mget(three, "redReminder") if isinstance(three, dict) else None
    except Exception:
        return None


def extract_item_id(frame: Dict[str, Any]) -> Optional[str]:
    """从 `1.10.reminderUrl` 取商品号（键类型无关）。"""
    try:
        one = mget(frame, "1")
        ten = mget(one, "10") if isinstance(one, dict) else None
        return extract_item_id_from_url(mget(ten, "reminderUrl") if isinstance(ten, dict) else None)
    except Exception:
        return None


def extract_item_id_from_url(url: Any) -> Optional[str]:
    """从 `reminderUrl`（形如 `fleamarket://message_chat?itemId=...&peerUserId=...`）取商品号。"""
    try:
        text = str(url or "")
        return text.split("itemId=")[1].split("&")[0] if "itemId=" in text else None
    except Exception:
        return None


def parse_inbound(frame: Dict[str, Any], my_id: str,
                  message_expire_ms: int = DEFAULT_MESSAGE_EXPIRE_MS,
                  message_id: Optional[str] = None,
                  position: Optional[int] = None) -> tuple[str, Optional[InboundMessage]]:
    """把解密后的负载解析成 `InboundMessage`。

    返回 (原因, 消息)。原因用于可观测：`ok` / `typing` / `not_chat` / `system` /
    `expired` / `no_item` / `bracket_system`。

    ⚠️ 判定顺序很关键：**先判是不是聊天消息，再判系统消息**。
    真机抓包显示买家消息负载形如 `{"1": {..., "10": {"reminderContent": "你好", ...}}, "3": {"needPush": "true"}}`，
    但平台也会把 `needPush=false` 装在**带正文**的负载上；若先判系统消息，就会把真实买家消息当系统消息丢掉。

    ⚠️ 幂等键不能只用外层帧 mid：**平台会把多条消息塞进一个同步包**
    （实测一条包 13 条数据、7 条消息，全部共用同一个外层 mid），用外层 mid 当键会让
    第 2 条起全被判成「重复」而静默丢弃——买家连发两句只有第一句会被回答。
    """
    raw_first = mget(frame, "1")
    chat_message = raw_first if isinstance(raw_first, dict) else {}

    # 形状容错：不再写死 frame["1"]["10"]，而是在负载里有界递归找「带文本的消息节点」。
    # 真机发过完整消息形（正文在 1.10，发送者在 1.1.1，msgId 在 1.3），写死嵌套会静默丢消息。
    node_parent, node, node_path = find_message_node(frame)
    if node is not None:
        create_time = int(mget(node_parent, "5") or mget(chat_message, "5") or 0)
        if create_time and (time.time() * 1000 - create_time) > message_expire_ms:
            return "expired", None
        text = chat_message_text(node)
        if not text:
            return "not_chat", None
        if is_bracket_system_message(text):
            return "bracket_system", None
        item_id = extract_item_id(frame) or extract_item_id_from_url(mget(node, "reminderUrl"))
        cid = mget(node_parent, "2") or mget(chat_message, "2") or ""
        chat_id = str(cid).split("@")[0]
        sender_id = chat_message_sender(node_parent or {}, node)

        # 幂等键优先级：负载自带消息 id → 「会话+时间+正文」→ 外层 mid#包内序号
        inner_mid = mget(node_parent, "3")
        if isinstance(inner_mid, (bytes, bytearray)):
            inner_mid = bytes(inner_mid).decode("utf-8", "ignore")
        if isinstance(inner_mid, str) and inner_mid:
            msg_id, id_source = inner_mid, "payload_id"
        elif create_time:
            # create_time 来自负载本身，重放同一帧时不变 → 既能去重，又不会把同包多条误判成重复
            msg_id, id_source = f"{chat_id}:{create_time}:{abs(hash(text))}", "content"
        else:
            fallback = f"{message_id}#{position}" if message_id is not None and position is not None \
                else str(message_id or f"{chat_id}:{abs(hash(text))}")
            msg_id, id_source = fallback, "outer_mid"
        return "ok", InboundMessage(
            message_id=str(msg_id),
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=str(mget(node, "reminderTitle", "reminderNotice") or ""),
            text=str(text),
            item_id=item_id,
            create_time_ms=create_time,
            from_seller=(sender_id == my_id),
            shape=node_path or ("compact" if is_chat_message(frame) else "unknown"),
            id_source=id_source,
            # 只在走了兜底键时才写诊断：把「父层到底长什么样」原样带出来，
            # 免得下次又要在「离线跑得好好的、线上不一样」之间猜（本机已猜过两轮）
            id_debug=_id_debug(node_parent, inner_mid) if id_source == "outer_mid" else "",
            content_type=chat_message_content_type(node_parent, node),
            raw=frame,
        )

    if is_conversation_list(frame):
        return "conv_list", None
    if is_typing_status(frame):
        return "typing", None
    if is_system_message(frame):
        return "system", None
    return "not_chat", None


# --------------------------------------------------------------------------- #
# 出站帧
# --------------------------------------------------------------------------- #
def build_text_frame(chat_id: str, to_id: str, my_id: str, text: str) -> Dict[str, Any]:
    """构造发送文本消息的帧（对齐上游 main.py:119-163）。"""
    payload = {"contentType": 1, "text": {"text": text}}
    payload_b64 = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("utf-8")
    return {
        "lwp": "/r/MessageSend/sendByReceiverScope",
        "headers": {"mid": generate_mid()},
        "body": [
            {
                "uuid": generate_uuid(),
                "cid": f"{chat_id}@goofish",
                "conversationType": 1,
                "content": {"contentType": 101, "custom": {"type": 1, "data": payload_b64}},
                "redPointPolicy": 0,
                "extension": {"extJson": "{}"},
                "ctx": {"appVersion": "1.0", "platform": "web"},
                "mtags": {},
                "msgReadStatusSetting": 1,
            },
            {"actualReceivers": [f"{to_id}@goofish", f"{my_id}@goofish"]},
        ],
    }


def build_ack(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """构造 ACK（一帧一次）。"""
    headers = frame.get("headers") if isinstance(frame, dict) else None
    if not isinstance(headers, dict) or "mid" not in headers:
        return None
    ack: Dict[str, Any] = {"code": 200, "headers": {"mid": headers["mid"], "sid": headers.get("sid", "")}}
    for key in ("app-key", "ua", "dt"):
        if key in headers:
            ack["headers"][key] = headers[key]
    return ack


# --------------------------------------------------------------------------- #
# 连接客户端
# --------------------------------------------------------------------------- #
class XianyuWSClient:
    """闲鱼 WSS 长连接客户端（只做协议与连接，业务交给回调）。"""

    def __init__(
        self,
        api: XianyuApi,
        device_id: str,
        *,
        url: str = WS_URL,
        user_agent: Optional[str] = None,
        on_message: Optional[Callable[[InboundMessage], Awaitable[None]]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        dedupe: Optional[DedupeStore] = None,
        heartbeat_interval: int = 15,
        heartbeat_timeout: int = 5,
        token_refresh_interval: int = 3600,
        token_retry_interval: int = 300,
        message_expire_ms: int = DEFAULT_MESSAGE_EXPIRE_MS,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 60.0,
        max_reconnects: Optional[int] = None,
        open_timeout: float = 20.0,
        frame_dump: Optional[str | Path] = None,
        sync_pts_mode: Optional[str] = None,
        sync_cursor: Optional[SyncCursor] = None,
        deliver_workers: int = 3,
        deliver_queue_size: int = 200,
    ) -> None:
        self.api = api
        self.device_id = device_id
        self.url = url
        self.my_id = api.unb
        # 诊断路径只由构造参数决定（不再读环境变量）：否则离线测试套件会往同一份诊断文件里写，
        # 把真实平台的帧和测试假帧混在一起——本轮排查就踩过这个坑。
        self.dump: Optional[FrameDump] = FrameDump(frame_dump) if frame_dump else None
        # 同步游标（ackDiff.pts）：now=上游做法（"我已同步到现在"，不拉历史，但掉线期间的消息会永久丢）
        #                     zero=拉全量历史（排查用）
        #                     last=从上次同步到的 pts 续（推荐：平台会补推断线窗口内的消息）
        self.sync_pts_mode = (sync_pts_mode or os.getenv("XIANYU_SYNC_PTS") or "now").strip().lower()
        self.sync_cursor = sync_cursor
        self.user_agent = user_agent or api.user_agent
        self.on_message = on_message
        self.on_event = on_event
        self.dedupe = dedupe or InMemoryDedupe()

        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.token_refresh_interval = token_refresh_interval
        self.token_retry_interval = token_retry_interval
        self.message_expire_ms = message_expire_ms
        self.reconnect_initial = reconnect_initial
        self.reconnect_max = reconnect_max
        self.max_reconnects = max_reconnects
        self.open_timeout = open_timeout

        self.current_token: Optional[str] = None
        self.connected = False
        self.registered = False
        self.stop_requested = False
        self._restart = False
        self._pending_heartbeats: Dict[str, float] = {}
        self._pending_requests: Dict[str, asyncio.Future] = {}   # mid → 等待中的请求应答
        self._current_ws: Any = None  # 当前连接对象（发送用）
        self.last_token_refresh_time = 0.0  # 上次取到 token 的时间（用于按周期刷新，而不是每轮都刷）
        self.last_heartbeat_response = 0.0
        # 平台判定会话失效时置位：下次注册前强制换一张新 token（缓存 token 已不可用）
        self.force_token_refresh = False
        self.stats: Dict[str, Any] = {
            "connects": 0, "reconnects": 0, "frames_in": 0, "acks_sent": 0, "heartbeats_sent": 0,
            "heartbeats_acked": 0, "heartbeat_timeouts": 0, "messages": 0, "duplicates": 0, "dropped": {},
            "decode_errors": 0, "frame_errors": 0, "token_refreshes": 0, "auth_errors": 0,
            "risk_control": 0, "texts_sent": 0, "last_frame_at": None, "last_message_at": None,
            "last_error": None, "deliver_errors": 0, "deliver_dropped": 0,
        }
        # 入站消息**不在收帧循环里直接处理**：业务处理要调 LLM / 商品接口 / 拟人化延迟，
        # 动辄十几秒。以前是 `await self.on_message(msg)` 内联等待，结果收帧循环被堵住，
        # 心跳 ACK 收不到 → 自己判自己掉线并关连接，正好把正在进行的发送打断
        # （2026-09-20 真机踩过：买家问「考研资料」，草稿生成了却没发出去）。
        # 现在只把消息塞进队列，由独立消费者任务处理，收帧循环永不被业务拖住。
        self._deliver_queue: asyncio.Queue = asyncio.Queue(maxsize=deliver_queue_size)
        self._deliver_task: Optional[asyncio.Task] = None
        self._deliver_workers = max(1, int(deliver_workers))

    # ---------------- 可观测 ----------------
    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # 回调异常不能影响连接
                logger.debug("on_event 回调异常", exc_info=True)

    def _count_drop(self, reason: str) -> None:
        self.stats["dropped"][reason] = self.stats["dropped"].get(reason, 0) + 1

    def health(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "registered": self.registered,
            # 平台判定会话失效后、重连换 token 之前，这里为 True：
            # 此时 connected/registered 仍可能是 True，但实际收不到消息（"静默僵尸连接"）
            "session_invalid": bool(self.force_token_refresh),
            "my_id": self.my_id[:4] + "***" if self.my_id else "",
            "url": self.url,
            # 交付队列积压：>0 说明业务处理慢（LLM/接口），面板与长跑体检都看它
            "deliver_queue": self._deliver_queue.qsize() if self._deliver_queue else 0,
            **self.stats,
        }

    # ---------------- 注册 ----------------
    async def _register(self, ws) -> None:
        if self.force_token_refresh:
            # 会话被平台判定失效：旧 token 已不可用，必须换新的再注册
            result = await self.api.get_token(self.device_id)
            self.current_token = result.access_token
            self.last_token_refresh_time = time.time()
            self.stats["token_refreshes"] = self.stats.get("token_refreshes", 0) + 1
            self.force_token_refresh = False
            self._emit("token_refreshed", cookie_refreshed=result.cookie_refreshed, forced=True)
        if not self.current_token:
            result = await self.api.get_token(self.device_id)
            self.current_token = result.access_token
            self.last_token_refresh_time = time.time()
        reg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": IM_APP_KEY,
                "token": self.current_token,
                "ua": self.user_agent,
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid(),
            },
        }
        await ws.send(json.dumps(reg))
        await asyncio.sleep(1)  # 上游 main.py:191 同样等待 1 秒再发 ackDiff
        now_ms = int(time.time() * 1000)
        if self.sync_pts_mode == "zero":
            pts = 0
        elif self.sync_pts_mode == "last" and self.sync_cursor:
            pts = self.sync_cursor.sane(now_ms) or now_ms * 1000
        else:
            pts = now_ms * 1000
        self._emit("sync_cursor", mode=self.sync_pts_mode, pts=pts)
        await ws.send(json.dumps({
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": generate_mid()},
            "body": [{"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync",
                      "highPts": 0, "pts": pts, "seq": 0, "timestamp": now_ms}],
        }))
        self.registered = True
        self._emit("registered", did=self.device_id[-8:], token_len=len(self.current_token or ""),
                   sync_pts_mode=self.sync_pts_mode, pts=pts)

    # ---------------- 心跳 ----------------
    async def _heartbeat_loop(self, ws) -> None:
        """按心跳 mid 精确对账：只有收到同一 mid 的响应才算活着（上游用任意 200 帧，属漏判）。"""
        self.last_heartbeat_response = time.time()
        last_sent = time.time()
        while True:
            now = time.time()
            if now - last_sent >= self.heartbeat_interval:
                mid = generate_mid()
                self._pending_heartbeats[mid] = now
                await ws.send(json.dumps({"lwp": "/!", "headers": {"mid": mid}}))
                self.stats["heartbeats_sent"] += 1
                last_sent = now
            # 只保留最近 3 个心跳，避免长期堆积
            if len(self._pending_heartbeats) > 3:
                oldest = sorted(self._pending_heartbeats.items(), key=lambda kv: kv[1])[0][0]
                self._pending_heartbeats.pop(oldest, None)
            if (now - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                self.stats["heartbeat_timeouts"] += 1
                self._emit("heartbeat_timeout",
                           since=round(now - self.last_heartbeat_response, 1))
                logger.warning("心跳超时（%.1fs 无响应），判定连接不可用", now - self.last_heartbeat_response)
                await ws.close()
                return
            await asyncio.sleep(1)

    def _match_heartbeat(self, frame: Dict[str, Any]) -> bool:
        headers = frame.get("headers") or {}
        mid = headers.get("mid")
        if mid and mid in self._pending_heartbeats:
            self._pending_heartbeats.pop(mid, None)
            self.last_heartbeat_response = time.time()
            self.stats["heartbeats_acked"] = self.stats.get("heartbeats_acked", 0) + 1
            return True
        return False

    # ---------------- token 刷新 ----------------
    def should_refresh_token(self, now: Optional[float] = None) -> bool:
        """是否到了刷新 token 的时间（按 `token_refresh_interval` 周期，不是每轮都刷）。

        真实平台实测踩过的坑：循环里只 sleep 60 秒就无条件刷新，会**每分钟重连一次**——
        既是明显的机器人特征，也让连接频繁中断。现在改成「每 60 秒看一眼，到周期才刷」。
        """
        now = time.time() if now is None else now
        return (now - self.last_token_refresh_time) >= self.token_refresh_interval

    def token_check_interval(self) -> float:
        """检查周期：最多 60 秒看一眼（保证及时性），但不小于 5 秒。"""
        return float(min(60, max(5, self.token_refresh_interval)))

    async def _token_refresh_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self.token_check_interval())
            if not self.should_refresh_token():
                continue
            try:
                result = await self.api.get_token(self.device_id)
                self.current_token = result.access_token
                self.last_token_refresh_time = time.time()
                self.stats["token_refreshes"] += 1
                self._emit("token_refreshed", cookie_refreshed=result.cookie_refreshed)
                logger.info("token 已刷新，主动重连以启用新 token")
                self._restart = True
                await ws.close()
                return
            except XianyuRiskControlError as exc:
                self.stats["risk_control"] += 1
                self.stats["last_error"] = str(exc)
                self._emit("risk_control", error=str(exc))
                logger.error("刷新 token 触发风控，停止重试（等人工处理）: %s", exc)
                return
            except XianyuAuthError as exc:
                self.stats["auth_errors"] += 1
                self.stats["last_error"] = str(exc)
                self._emit("auth_error", error=str(exc))
                logger.error("刷新 token 失败（登录态失效），停止重试（等人工续期）: %s", exc)
                return
            except Exception as exc:
                self.stats["last_error"] = str(exc)
                logger.warning("刷新 token 异常，%ss 后重试: %s", self.token_retry_interval, exc)
                await asyncio.sleep(self.token_retry_interval)

    # ---------------- 收帧 ----------------
    async def _handle_frame(self, frame: Dict[str, Any], ws) -> None:
        """处理一帧。**任何异常都只记一笔并丢弃该帧**，绝不让单帧异常打断连接（7×24 前提）。"""
        try:
            await self._handle_frame_inner(frame, ws)
        except Exception as exc:  # noqa: BLE001
            self.stats["frame_errors"] = self.stats.get("frame_errors", 0) + 1
            self._count_drop("frame_error")
            self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
            self._emit("frame_error", error=self.stats["last_error"])
            logger.warning("处理帧出错（已丢弃该帧）: %s", exc)

    async def _handle_frame_inner(self, frame: Dict[str, Any], ws) -> None:
        self.stats["frames_in"] += 1
        self.stats["last_frame_at"] = time.time()

        # 一帧一次 ACK（上游在收帧循环和 handle_message 里各 ACK 一次）
        ack = build_ack(frame)
        if ack:
            await ws.send(json.dumps(ack))
            self.stats["acks_sent"] += 1

        # 请求/应答关联：等这条 mid 的调用方（如按会话拉历史）先拿到帧
        req_mid = (frame.get("headers") or {}).get("mid")
        future = self._pending_requests.pop(req_mid, None) if req_mid else None

        # 会话失效（平台不关连接，只回 401/token is not found）：必须刷新 token 并重连，
        # 否则连接看着是好的、实际已收不到任何消息。
        # 注意**必须排在请求应答关联之前**：查询请求的应答本身就可能是这条失效帧，
        # 若先 `return` 把帧交给调用方，客户端侧就不会触发重连（回归测试覆盖）。
        auth_reason = auth_error(frame)
        if auth_reason:
            if future and not future.done():
                future.set_result(frame)  # 让调用方拿到帧并抛出明确错误，而不是空结果
            self.stats["auth_errors"] = self.stats.get("auth_errors", 0) + 1
            self.stats["last_error"] = f"session_invalid: {auth_reason}"
            self._emit("session_invalid", reason=auth_reason)
            logger.error("平台判定会话失效（%s），刷新 token 并重连", auth_reason)
            self.force_token_refresh = True
            self._restart = True
            await ws.close()
            return

        if future and not future.done():
            future.set_result(frame)
            if self.dump:
                self.dump.write({"ts": time.time(), "kind": "request_response", "mid": req_mid,
                                 "lwp": frame.get("lwp"), "structure": _structure(frame)})
            return

        if self._match_heartbeat(frame):
            if self.dump:
                self.dump.write({"ts": time.time(), "kind": "heartbeat_ack", "mid": (frame.get("headers") or {}).get("mid")})
            return

        if not is_sync_package(frame):
            status = order_status(frame)
            if status:
                self._emit("order_status", status=status)
            if self.dump:
                self.dump.write({"ts": time.time(), "kind": "non_sync",
                                 "lwp": frame.get("lwp"), "mid": (frame.get("headers") or {}).get("mid"),
                                 "structure": _structure(frame)})
            return

        try:
            package = frame["body"]["syncPushPackage"]
            entries = package["data"]
            if not isinstance(entries, list) or not entries:
                raise KeyError("syncPushPackage.data 为空")
        except (KeyError, IndexError, TypeError) as exc:
            self.stats["decode_errors"] += 1
            self._count_drop("bad_package")
            self._emit("decode_error", error=f"{type(exc).__name__}: {exc}")
            return

        # 记下平台给出的同步进度：下次重连用它当 pts，掉线窗口内的消息才会被补推
        if self.sync_cursor is not None:
            for key in ("maxPts", "maxHighPts"):
                try:
                    if self.sync_cursor.update(package.get(key)):
                        self._emit("sync_cursor_saved", field=key,
                                   pts=self.sync_cursor.get(), has_more=package.get("hasMore"))
                except Exception:
                    logger.debug("同步游标更新失败（不影响值守）", exc_info=True)

        # 逐个处理：平台可能在同一条同步包里塞多条数据（真机抓包里 conversation-list 与消息会同时出现），
        # 只取 data[0] 会漏掉后面那条真正的消息。
        for index, entry in enumerate(entries):
            try:
                raw = entry["data"]
            except (KeyError, TypeError):
                self._count_drop("bad_package_entry")
                continue
            try:
                payload, path = decode_sync_payload(raw)
            except ProtocolDecodeError as exc:
                # 改进点：解码失败明确丢弃并告警，不拿乱码继续走链路
                self.stats["decode_errors"] += 1
                self._count_drop("decode_failed")
                self._emit("decode_error", error=str(exc))
                logger.warning("同步包解码失败，已丢弃: %s", exc)
                continue

            if not isinstance(payload, dict):
                self._count_drop("payload_not_dict")
                if self.dump:
                    self.dump.write({"ts": time.time(), "kind": "payload_not_dict", "path": path,
                                     "index": index, "payload_structure": _structure(payload)})
                continue

            status = order_status(payload)
            if status:
                self._emit("order_status", status=status)
                if self.dump:
                    self.dump.write({"ts": time.time(), "kind": "order_status", "status": status,
                                     "index": index, "payload_structure": _structure(payload)})
                continue

            # 幂等键由 parse_inbound 决定（负载消息 id → 内容键 → 外层 mid#包内序号）：
            # 一个同步包可能含多条消息且共用外层 mid，position 让兜底键仍然逐条唯一
            outer_mid = (frame.get("headers") or {}).get("mid")
            reason, message = parse_inbound(payload, self.my_id, self.message_expire_ms,
                                            message_id=outer_mid, position=index)
            if self.dump:
                self.dump.write({"ts": time.time(), "kind": f"sync_{reason}", "mid": outer_mid,
                                 "index": index, "entries": len(entries), "decode_path": path,
                                 "payload_structure": _structure(payload)}, payload=payload)
            if message is None:
                self._count_drop(reason)
                continue

            # 平台注入的提示卡（完整消息形带内容类型；14=「恭喜新手卖家…」这类）不是买家提问，
            # 喂进去只会生成答非所问的草稿。紧凑形没有该字段 → None，按真实消息处理。
            if message.content_type is not None and message.content_type != 1:
                self._count_drop(f"notice_type_{message.content_type}")
                self._emit("notice_skipped", chat_id=message.chat_id,
                           content_type=message.content_type, text_len=len(message.text))
                continue

            # 交付（幂等校验必须在**循环内**：一条同步包可能含多条数据，
            # 之前把交付写在循环外，导致「最后一条被丢弃」时循环变量悬空 → frame_error，本机自测抓到）
            if self.dedupe.seen(message.message_id):
                self.stats["duplicates"] += 1
                self._count_drop("duplicate")
                self._emit("duplicate", message_id=message.message_id, chat_id=message.chat_id,
                           id_source=message.id_source, id_debug=message.id_debug, shape=message.shape)
                continue
            self.dedupe.mark(message.message_id)

            self.stats["messages"] += 1
            self.stats["last_message_at"] = time.time()
            self._emit("message", chat_id=message.chat_id, sender=message.sender_id[:4] + "***",
                       item_id=message.item_id, decode_path=path, shape=message.shape,
                       id_source=message.id_source, id_debug=message.id_debug,
                       from_seller=message.from_seller)
            # 交给消费者处理：**不 await**，收帧循环立刻回去收下一帧
            self._deliver(message)

    # ---------------- 单次连接生命周期 ----------------
    async def run_once(self) -> str:
        """建立一次连接并处理到断开。返回结束原因：restart / closed / error。"""
        self._ensure_delivery()   # 兼容单独调用 run_once 的场景（否则消息只进队列没人处理）
        headers = {
            "Cookie": self.api.cookie_str(),
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": self.user_agent,
            "Origin": "https://www.goofish.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        reason = "closed"
        try:
            # proxy=None：**平台长连接必须直连**。
            # websockets 默认会读 HTTP(S)_PROXY 环境变量，而本机全局代理指向 127.0.0.1:7897，
            # 代理没开时连接会被拒（日志只有 ConnectionRefused，极易误判成"IP 挂了"——
            # 2026-09-21 早上就绕了一圈：DNS/TCP/TLS 全通，最后卡在已停的代理上）。
            async with ws_connect(self.url, additional_headers=headers,
                                  open_timeout=self.open_timeout, proxy=None) as ws:
                self.connected = True
                self._current_ws = ws
                self.stats["connects"] += 1
                self._restart = False
                await self._register(ws)
                self._emit("connected", url=self.url)
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                token_task = asyncio.create_task(self._token_refresh_loop(ws))
                try:
                    async for raw in ws:
                        if self._restart:
                            reason = "restart"
                            break
                        try:
                            frame = json.loads(raw)
                        except Exception:
                            self._count_drop("bad_json")
                            continue
                        if not isinstance(frame, dict):
                            self._count_drop("not_dict")
                            continue
                        await self._handle_frame(frame, ws)
                finally:
                    for task in (heartbeat_task, token_task):
                        task.cancel()
                    await asyncio.gather(heartbeat_task, token_task, return_exceptions=True)
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as exc:
            reason = "error"
            self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
            self._emit("connection_error", error=self.stats["last_error"])
            logger.warning("连接异常: %s", exc)
        finally:
            self.connected = False
            self.registered = False
            self._current_ws = None
            self._pending_heartbeats.clear()
        return reason

    # ---------------- 常驻（指数退避重连） ----------------
    def _ensure_delivery(self) -> None:
        """确保交付消费者在跑。

        必须在这里兜住：`run_once()` 也可能被单独调用（P1 存活体检就是这么用的），
        只在 `run()` 里起消费者会导致**消息进队列却永远没人处理**（本机自测抓到）。
        """
        if self._deliver_task is None or self._deliver_task.done():
            self._deliver_task = asyncio.create_task(self._deliver_loop())

    async def drain_delivery(self, timeout: float = 5.0) -> bool:
        """等队列里已收到的消息处理完（体检/测试收尾用）。返回是否已清空。"""
        if self._deliver_queue.empty():
            return True
        try:
            await asyncio.wait_for(self._deliver_queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def run(self) -> None:
        # 交付消费者：与连接生命周期解耦（重连不影响队列里的消息）
        self._ensure_delivery()
        attempt = 0
        try:
            while not self.stop_requested:
                reason = await self.run_once()
                if self.stop_requested:
                    break
                if self.max_reconnects is not None and attempt >= self.max_reconnects:
                    self._emit("reconnect_giveup", attempts=attempt)
                    break
                attempt += 1
                self.stats["reconnects"] += 1
                delay = 0.0 if reason in ("restart",) else min(
                    self.reconnect_initial * (2 ** (attempt - 1)), self.reconnect_max)
                self._emit("reconnect", attempt=attempt, delay=delay, reason=reason)
                logger.info("连接结束（%s），%.1fs 后重连（第 %s 次）", reason, delay, attempt)
                if delay:
                    await asyncio.sleep(delay)
        finally:
            await self._stop_delivery()

    def _deliver(self, message: InboundMessage) -> None:
        """把入站消息交给消费者（**非阻塞**，绝不在收帧循环里等业务）。"""
        try:
            self._deliver_queue.put_nowait(message)
        except asyncio.QueueFull:
            # 队列满说明业务严重堆积：宁可丢这条并计数，也不能把收帧循环拖死
            self.stats["deliver_dropped"] = self.stats.get("deliver_dropped", 0) + 1
            self._count_drop("deliver_queue_full")
            self._emit("deliver_dropped", chat_id=message.chat_id,
                       queue_size=self._deliver_queue.qsize())

    async def _deliver_loop(self) -> None:
        """消费者：并发处理消息（默认 3 个 worker），单个会话的顺序由 engine 的会话锁保证。"""
        async def worker(index: int) -> None:
            while True:
                message = await self._deliver_queue.get()
                try:
                    if self.on_message:
                        await self.on_message(message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 单条处理失败不能影响连接与其它消息
                    self.stats["deliver_errors"] = self.stats.get("deliver_errors", 0) + 1
                    self.stats["last_error"] = f"deliver: {type(exc).__name__}: {exc}"
                    self._emit("deliver_error", chat_id=message.chat_id,
                               error=f"{type(exc).__name__}: {exc}")
                    logger.warning("处理消息出错（连接不受影响）: %s", exc)
                finally:
                    self._deliver_queue.task_done()

        try:
            await asyncio.gather(*(worker(i) for i in range(self._deliver_workers)))
        except asyncio.CancelledError:
            pass

    async def _stop_delivery(self, timeout: float = 10.0) -> None:
        """收尾：给队列里剩余消息一点时间处理完，再取消消费者。"""
        if not self._deliver_task:
            return
        try:
            if not self._deliver_queue.empty():
                await asyncio.wait_for(self._deliver_queue.join(), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            pass
        self._deliver_task.cancel()
        await asyncio.gather(self._deliver_task, return_exceptions=True)
        self._deliver_task = None

    def request_stop(self) -> None:
        self.stop_requested = True

    @property
    def current_ws(self):
        """当前连接对象（未连接时为 None）。发送消息用它。"""
        return self._current_ws

    # ---------------- 发送 ----------------
    async def send_text(self, ws, chat_id: str, to_id: str, text: str) -> Dict[str, Any]:
        frame = build_text_frame(chat_id, to_id, self.my_id, text)
        await ws.send(json.dumps(frame))
        self.stats["texts_sent"] = self.stats.get("texts_sent", 0) + 1
        self._emit("sent", chat_id=chat_id, to_id=to_id[:4] + "***", text_len=len(text))
        return frame

    async def request(self, lwp: str, body: Any, timeout: float = 20.0) -> Dict[str, Any]:
        """在当前连接上发一条请求并等**同一 mid** 的应答（用于按会话拉历史等只读查询）。

        这样就不必为了查询再开一条连接（同账号多连接有互挤风险）。

        **平台报错必须抛出**，不能静默退化成空结果：实测平台会用 `code=400/401` 回一条
        没有 body 的帧，若照原样返回，调用方会以为「拉到了 0 条消息」，把
        「连接已失效」误判成「会话里没有消息」（排查时踩过这个坑）。
        """
        ws = self.current_ws
        if ws is None:
            raise RuntimeError("当前没有活动连接")
        mid = generate_mid()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_requests[mid] = future
        try:
            await ws.send(json.dumps({"lwp": lwp, "headers": {"mid": mid}, "body": body}))
            frame = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_requests.pop(mid, None)

        reason = auth_error(frame)
        if reason:
            raise RuntimeError(f"平台会话已失效（{reason}），正在刷新 token 重连，请稍后重试")
        code = frame.get("code")
        if code not in (None, 200):
            raise RuntimeError(f"平台拒绝了这条查询：code={code} body={str(frame.get('body'))[:200]}")
        return frame
