# -*- coding: utf-8 -*-
"""闲鱼专家层：从 XianyuAutoAgent 移植的「多专家 + 三级路由 + 阶梯议价」，
并把 AI-Robot 的 RAG 与 traces 接了上去。

- `guard.py`   合规过滤 + 确定性 no_reply 判定
- `prompts.py` 提示词加载（`prompts/xianyu/*.txt` 优先，缺失回落内置）
- `llm.py`     ChatOpenAI 封装（enable_search / 关闭推理模型的思考）
- `router.py`  规则优先 + LLM 兜底的三级路由
- `agents.py`  classify / price / tech / default 四个专家
- `service.py` 路由→专家→RAG 注入→合规→traces 的编排
"""
from app.agents.specialists.guard import filter_outbound, is_no_reply  # noqa: F401
from app.agents.specialists.service import (  # noqa: F401
    SpecialistService,
    extract_bargain_count,
    format_context,
    get_specialist_service,
)

__all__ = [
    "SpecialistService",
    "filter_outbound",
    "is_no_reply",
    "extract_bargain_count",
    "format_context",
    "get_specialist_service",
]
