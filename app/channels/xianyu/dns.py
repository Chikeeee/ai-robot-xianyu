# -*- coding: utf-8 -*-
r"""DNS 兜底：把指定域名强制解析到指定 IP（进程内生效，等价于临时 hosts 条目）。

为什么需要：本机实测出现过 ISP DNS 对闲鱼 IM 域名只返回 AAAA（IPv6）而本机没有可用 IPv6 路由，
于是 `getaddrinfo` 直接失败（`[Errno 11004]`），长连接再也连不上；而公共 DNS（119.29.29.29）
对同一域名能返回 A 记录。写 hosts 需要管理员权限，所以在**进程内**做这件事，
且**只改地址解析、不改 SNI/证书校验**（TLS 仍按原域名校验，安全性不受影响）。

平台 IP 会漂（实测 106.11.130.193 → 106.11.23.116，现象是 `ConnectionRefused`），
所以支持给同一个 host 配**多个候选 IP**：`getaddrinfo` 会依次返回它们，
`create_connection`（websockets 底层用的就是它）会逐个尝试，等于自带故障转移。

配置：`XIANYU_DNS_OVERRIDE="wss-goofish.dingtalk.com=106.11.23.116,wss-goofish.dingtalk.com=106.11.23.115"`
"""
from __future__ import annotations

import logging
import socket
from typing import Dict, List

logger = logging.getLogger("airobot.xianyu.dns")

_original_getaddrinfo = None
_applied: Dict[str, List[str]] = {}


def parse_spec(spec: str) -> Dict[str, List[str]]:
    """解析 `host=ip,host=ip` 形式的配置，**同一 host 的多个 IP 按顺序保留**。

    早期实现用 dict，重复的 host 键会把前一个候选**悄悄覆盖掉**——
    于是在 .env 里写两个候选 IP 实际上只生效最后一个（本机踩过，还以为故障转移已生效）。
    """
    out: Dict[str, List[str]] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        host, ip = item.split("=", 1)
        host, ip = host.strip().lower(), ip.strip()
        if not host or not ip:
            continue
        bucket = out.setdefault(host, [])
        if ip not in bucket:
            bucket.append(ip)
    return out


def apply_override(spec: str) -> Dict[str, List[str]]:
    """安装 DNS 覆盖（幂等）。返回生效的映射（host → IP 列表）。空配置则不做任何事。"""
    global _original_getaddrinfo
    mapping = parse_spec(spec)
    if not mapping:
        return {}
    if _original_getaddrinfo is None:
        _original_getaddrinfo = socket.getaddrinfo
    _applied.update(mapping)
    logger.warning("已启用 DNS 覆盖（仅地址解析，TLS 仍按域名校验）: %s", _applied)

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        targets = _applied.get(str(host).lower())
        if not targets:
            return _original_getaddrinfo(host, port, family, type, proto, flags)
        # **把所有候选都返回**（不是一个）：socket.create_connection 会按顺序逐个尝试，
        # 这样某个 IP 的 TCP 被拒时才会自动切到下一个候选——
        # 只返回第一个的话，TCP 层故障就没有故障转移（本机踩过：以为配了两个候选就安全了）
        merged = []
        last_error = None
        for target in targets:
            try:
                merged.extend(_original_getaddrinfo(target, port, family, type, proto, flags))
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("DNS 覆盖候选 %s 解析失败：%s", target, exc)
        if merged:
            return merged
        raise last_error if last_error is not None else OSError("DNS 覆盖：没有可用候选 IP")

    socket.getaddrinfo = patched
    return {host: list(ips) for host, ips in _applied.items()}


def active_overrides() -> Dict[str, List[str]]:
    return {host: list(ips) for host, ips in _applied.items()}
