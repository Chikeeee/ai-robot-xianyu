# -*- coding: utf-8 -*-
r"""诊断：闲鱼 WSS 到底卡在哪一步（DNS / TCP / TLS / 握手）。

真机现象：日志里只有 `ConnectionRefusedError [WinError 1225]`，但用 Test-NetConnection
测同一个 IP 的 443 又是通的。所以必须分步复现：
1. 应用 DNS 覆盖后 getaddrinfo 解析出什么；
2. 裸 TCP 能否连上；
3. TLS 握手（带 SNI）能否完成；
4. websockets 客户端连接能否建立。

用法：python scripts/xianyu_ws_diagnose.py
"""
import asyncio
import os
import socket
import ssl
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOST = "wss-goofish.dingtalk.com"
PORT = 443


def load_env_override() -> str:
    env = ROOT / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("XIANYU_DNS_OVERRIDE="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    spec = load_env_override()
    print("1) .env 里的 DNS 覆盖配置:", spec or "（未配置）")

    from app.channels.xianyu.dns import apply_override, parse_spec
    mapping = parse_spec(spec)
    print("   解析成映射:", mapping, "（注意：同 host 多 IP 会被后者覆盖）")
    if mapping:
        applied = apply_override(spec)
        print("   已应用:", applied)

    try:
        infos = socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)
        addrs = sorted({info[4][0] for info in infos})
        print("2) getaddrinfo 解析到:", addrs)
    except Exception as exc:
        print("2) getaddrinfo 失败:", type(exc).__name__, exc)
        return 2

    for addr in addrs:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(8)
        t0 = time.time()
        try:
            sock.connect((addr, PORT))
            print(f"3) TCP 连接 {addr}:{PORT} 成功（{(time.time()-t0)*1000:.0f}ms）")
            ctx = ssl.create_default_context()
            try:
                tls = ctx.wrap_socket(sock, server_hostname=HOST)
                print(f"   TLS 握手成功 protocol={tls.version()} cipher={tls.cipher()[0]}")
                tls.close()
            except Exception as exc:
                print("   TLS 握手失败:", type(exc).__name__, exc)
        except Exception as exc:
            print(f"3) TCP 连接 {addr}:{PORT} 失败:", type(exc).__name__, exc)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    print("4) 代理环境变量（websockets 会读它们，代理没开就会 ConnectionRefused）:")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "NO_PROXY"):
        print(f"   {key} = {os.environ.get(key)}")

    async def ws_probe():
        from websockets.asyncio.client import connect as ws_connect
        for label, extra in (("直连（proxy=None，与线上一致）", {"proxy": None}),
                             ("走环境代理（默认行为，对比用）", {})):
            try:
                async with ws_connect(f"wss://{HOST}/", open_timeout=15, **extra) as ws:
                    print(f"5) websockets 连接建立成功 — {label}")
                    await ws.close()
            except Exception as exc:
                print(f"5) websockets 连接失败 — {label}: {type(exc).__name__} {exc}")

    asyncio.run(ws_probe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
