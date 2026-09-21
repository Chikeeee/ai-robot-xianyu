# -*- coding: utf-8 -*-
"""闲鱼（goofish）值守通道。

模块划分（P0 起逐步落地）：
- `protocol.py`  cookie 解析 / mtop 签名 / 设备指纹 / MessagePack 解码（纯计算，可离线单测）
- `api.py`       mtop 接口（登录态检查、取 token、取商品信息），async + httpx
- `ws.py`        WSS 长连接：注册、心跳、token 刷新、断线重连
- `session.py`   会话状态：人工接管、议价计数、商品缓存、消息幂等（SQLite）
- `outbound.py`  出站：发送帧构造、合规过滤、拟人化延迟、频率限制
- `engine.py`    值守引擎：把入站消息交给 AI-Robot 的 chat 编排，再决定是否回复
"""
from app.channels.xianyu import protocol  # noqa: F401

__all__ = ["protocol"]
