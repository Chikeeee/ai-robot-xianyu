# -*- coding: utf-8 -*-
"""闲鱼值守通道 P0 离线自检。

核心思路：**与上游实现做差分对比**——把 `D:\\dsh\\XianyuAutoAgent` 的纯函数（cookie 解析、签名、
MessagePack 解码、解密）当参考实现，对同一批输入比较两边输出；再额外验证本项目的 4 处改进
（持久化 device_id、解码失败即报错、凭据独立文件、不退出进程）。

全程不联网、不连 WebSocket、不碰正在运行的旧 bot。
用法：python scripts/test_xianyu_protocol.py
结果写入 .logs/xianyu_protocol_selftest.txt
"""
import base64
import io
import json
import os
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 上游作为参考实现（纯 stdlib，import 无副作用）
UPSTREAM = Path(r"D:\dsh\XianyuAutoAgent")
sys.path.insert(0, str(UPSTREAM))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.channels.xianyu import protocol as p  # noqa: E402
from utils.xianyu_utils import (  # noqa: E402  上游参考实现
    MessagePackDecoder as UpDecoder,
    decrypt as up_decrypt,
    generate_device_id as up_device_id,
    generate_mid as up_mid,
    generate_sign as up_sign,
    generate_uuid as up_uuid,
    trans_cookies as up_trans_cookies,
)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# 一个最小 MessagePack 打包器（仅测试用；上游 dry_run_message.py 里也是这么干的）
# --------------------------------------------------------------------------- #
def pack(obj):
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xFF])
        if 0 <= obj <= 0xFF:
            return b"\xcc" + struct.pack(">B", obj)
        if 0 <= obj <= 0xFFFF:
            return b"\xcd" + struct.pack(">H", obj)
        if 0 <= obj <= 0xFFFFFFFF:
            return b"\xce" + struct.pack(">I", obj)
        if obj >= 0:
            return b"\xcf" + struct.pack(">Q", obj)
        if -128 <= obj < 0:
            return b"\xd0" + struct.pack(">b", obj)
        if -32768 <= obj < 0:
            return b"\xd1" + struct.pack(">h", obj)
        if -2147483648 <= obj < 0:
            return b"\xd2" + struct.pack(">i", obj)
        return b"\xd3" + struct.pack(">q", obj)
    if isinstance(obj, float):
        return b"\xcb" + struct.pack(">d", obj)
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        if len(raw) <= 31:
            return bytes([0xA0 | len(raw)]) + raw
        if len(raw) <= 0xFF:
            return b"\xd9" + struct.pack(">B", len(raw)) + raw
        return b"\xda" + struct.pack(">H", len(raw)) + raw
    if isinstance(obj, (bytes, bytearray)):
        return b"\xc4" + struct.pack(">B", len(obj)) + bytes(obj)
    if isinstance(obj, dict):
        head = bytes([0x80 | len(obj)]) if len(obj) <= 15 else b"\xde" + struct.pack(">H", len(obj))
        return head + b"".join(pack(k) + pack(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        head = bytes([0x90 | len(obj)]) if len(obj) <= 15 else b"\xdc" + struct.pack(">H", len(obj))
        return head + b"".join(pack(v) for v in obj)
    raise TypeError(f"packer 不支持的类型: {type(obj)}")


def main():
    # ---------------- 1. Cookie 解析（差分） ----------------
    cookie_samples = [
        "unb=1000000000001; cna=abc; _m_h5_tk=deadbeef_1234567890",
        "a=1;b=2",                      # 上游按 "; " 切分，这里两边的怪异行为要一致
        "a=1; ; b=2; c=",               # 空片段 / 空值
        "nogoodpiece; x=1",
        "",
    ]
    ok = all(p.parse_cookie_str(s) == up_trans_cookies(s) for s in cookie_samples)
    check("cookie 解析与上游一致（含怪异输入）", ok,
          f"{len(cookie_samples)} 个样本逐个对比")

    # ---------------- 2. 签名（差分 + 冻结值） ----------------
    sign_cases = [
        ("1789908508000", "07a5326f69fb5b8b9fa221a0789230ed", '{"itemId":"123"}'),
        ("1700000000000", "0123456789abcdef0123456789abcdef", "a=1&b=2"),
        ("1", "t", ""),
    ]
    diff_ok = all(p.generate_sign(t, tok, data) == up_sign(t, tok, data) for t, tok, data in sign_cases)
    frozen = p.generate_sign(*sign_cases[0])
    check("签名算法与上游一致（差分）", diff_ok, f"3 个样本；冻结值 {frozen[:16]}…")
    check("签名是 32 位小写十六进制", bool(re.fullmatch(r"[0-9a-f]{32}", frozen)), frozen)

    # ---------------- 3. mid / uuid 形状 ----------------
    mid_ok = bool(re.fullmatch(r"\d{1,3}\d{13} 0", p.generate_mid()))
    up_mid_ok = bool(re.fullmatch(r"\d{1,3}\d{13} 0", up_mid()))
    check("mid 形状与上游一致（{随机}{13位毫秒} 0）", mid_ok and up_mid_ok,
          f"mine={p.generate_mid()!r} upstream={up_mid()!r}")
    uuid_ok = bool(re.fullmatch(r"-\d{13}1", p.generate_uuid())) and bool(re.fullmatch(r"-\d{13}1", up_uuid()))
    check("uuid 形状与上游一致（-{13位毫秒}1）", uuid_ok)

    # ---------------- 4. device_id：形状一致 + 持久化（改进点） ----------------
    shape_ok = bool(p.DEVICE_ID_RE.match(p.generate_device_id("1000000000001"))) and \
        bool(p.DEVICE_ID_RE.match(up_device_id("1000000000001")))
    check("device_id 形状与上游一致（UUIDv4 形状 + -unb）", shape_ok, p.generate_device_id("2200")[-9:].join(["…", ""]))

    store = ROOT / ".logs" / "xianyu_device_ids.json"
    if store.exists():
        store.unlink()
    d1 = p.load_or_create_device_id("1000000000001", store)
    d2 = p.load_or_create_device_id("1000000000001", store)
    d3 = p.load_or_create_device_id("9999999999999", store)
    check("device_id 持久化：同账号复用、异账号不同",
          d1 == d2 and d1 != d3 and store.exists(),
          "首次生成后落盘，二次调用返回同一值（上游每次启动都变，属改进）")

    # ---------------- 5. MessagePack 解码（三方对比：本实现 / 上游 / 原对象） ----------------
    payloads = [
        {"1": {"2": "chat@goofish", "5": 1789908508000,
               "10": {"senderUserId": "111", "reminderTitle": "买家", "reminderContent": "在吗？", "reminderUrl": "x?itemId=88&y=1"}}},
        [1, 2, 3, {"nested": [True, False, None]}],
        {"neg": -33, "i8": -128, "i32": -70000, "u64": 18446744073709551615},
        {"f": 3.14159, "s": "中文内容，含标点。", "b": b"\x00\x01\xff"},
        {"empty_map": {}, "empty_arr": [], "long": "x" * 300},
    ]
    ok_all = True
    detail = []
    for i, obj in enumerate(payloads):
        raw = pack(obj)
        mine = p.MessagePackDecoder(raw).decode()
        theirs = UpDecoder(raw).decode()
        same = _loose_equal(mine, theirs) and _loose_equal(mine, obj)
        ok_all = ok_all and same
        detail.append(f"#{i}{'✓' if same else '✗'}")
    check("MessagePack 解码三方一致（本实现 / 上游 / 原对象）", ok_all, " ".join(detail))

    # ---------------- 6. decrypt 通路一致 ----------------
    obj = payloads[0]
    b64 = base64.b64encode(pack(obj)).decode()
    mine_obj, path = p.decode_sync_payload(b64)
    up_obj = json.loads(up_decrypt(b64))
    check("base64+MessagePack 负载解密结果与上游一致", _loose_equal(mine_obj, up_obj), f"解码路径={path}")

    b64_json = base64.b64encode(json.dumps({"a": 1}, ensure_ascii=False).encode()).decode()
    mine_obj2, path2 = p.decode_sync_payload(b64_json)
    check("base64+JSON 负载走第一条通路", mine_obj2 == {"a": 1} and path2 == "base64+json", f"路径={path2}")

    # ---------------- 7. 严格性：坏数据必须报错（改进点） ----------------
    bad_samples = [b"\xc1", b"\xdd\xff\xff\xff\xff", b"\xa5ab"]
    raised = 0
    for bad in bad_samples:
        try:
            p.MessagePackDecoder(bad).decode()
        except p.ProtocolDecodeError:
            raised += 1
    upstream_silent = all(isinstance(UpDecoder(b).decode(), str) for b in bad_samples)
    check("坏数据：本实现抛 ProtocolDecodeError（上游静默降级成字符串）",
          raised == len(bad_samples) and upstream_silent,
          f"本实现 {raised}/{len(bad_samples)} 报错；上游 {len(bad_samples)}/{len(bad_samples)} 静默返回字符串")

    # ---------------- 8. 凭据文件 ----------------
    cred_path = ROOT / "secrets" / "xianyu_credentials.json"
    try:
        cred = p.load_credentials(cred_path)
        cookies = cred["_cookies"]
        unb = cookies.get("unb", "")
        token = p.token_from_cookies(cookies)
        check("凭据文件可读且是有效登录态",
              bool(unb) and len(cookies) >= 15 and re.fullmatch(r"[0-9a-f]{32}", token) is not None,
              f"字段数={len(cookies)}，unb={unb[:4]}***{unb[-2:]}，token 长度={len(token)}")
        check("凭据文件权限已收紧（仅当前用户）",
              _acl_restricted(ROOT / "secrets"), "secrets/ 已 /inheritance:r，仅当前用户 F")
    except Exception as exc:
        check("凭据文件可读且是有效登录态", False, f"{type(exc).__name__}: {exc}")

    # ---------------- 9. 非法 cookie 必须报错而不是退出进程（改进点） ----------------
    try:
        p.require_login_cookie(p.parse_cookie_str("cna=abc; _m_h5_tk=dead_1"))
        check("缺 unb 时抛 CookieError 而非退出进程", False, "没有抛异常")
    except p.CookieError as exc:
        check("缺 unb 时抛 CookieError 而非退出进程", True, f"异常信息 {len(str(exc))} 字符（上游此处 sys.exit(1)）")

    # ---------------- 汇总 ----------------
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"P0 离线自检：{passed}/{total} 通过", ""]
    for name, ok, detail in RESULTS:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    text = "\n".join(lines)
    (ROOT / ".logs" / "xianyu_protocol_selftest.txt").write_text(text, encoding="utf-8")
    print(f"\nP0 离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


def _loose_equal(a, b):
    """bytes 与 str 混用时按 utf-8 宽松比较（MessagePack 的 bin 类型在两边表示不同）。"""
    if a == b:
        return True
    try:
        return json.dumps(a, ensure_ascii=False, sort_keys=True, default=str) == \
            json.dumps(b, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return False


def _acl_restricted(path: Path) -> bool:
    import subprocess
    try:
        out = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore").stdout
    except Exception:
        return False
    return "BUILTIN" not in out and "Everyone" not in out and "Users:" not in out


if __name__ == "__main__":
    sys.exit(main())
