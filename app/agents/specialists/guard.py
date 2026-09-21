# -*- coding: utf-8 -*-
r"""出站合规与「不回复」判定（移植上游 `XianyuAgent.py:91-94` 与 `prompts/classify_prompt.txt:13-14`）。

上游把合规过滤放在 agent 内部（`_safe_filter`），把「不回复」交给 LLM 分类提示词。
这里把两者抽成**确定性**函数，好处是：不依赖模型是否听话，也不会因为提示词被绕过就失守。
"""
from __future__ import annotations

import re

# 上游 XianyuAgent.py:93 的拦截词：命中即整条替换（站外引流是平台红线）
BLOCKED_PHRASES = ("微信", "QQ", "支付宝", "银行卡", "线下")
SAFE_REPLY = "[安全提醒]请通过平台沟通"

# 提示词注入 / 身份询问 / 无关话题：上游要求归入 no_reply（classify_prompt.txt:13-14）
NO_REPLY_PATTERNS = [
    r"你(是|用的)(谁|什么模型|哪个模型)",
    r"(系统|初始)提示词",
    r"(忽略|忘记|无视)(以上|之前|上述|所有)(的)?(指令|要求|规则|设定)",
    r"(现在|从现在起)?你(不再是|不是)",
    r"你现在是",
    r"(输出|回复|打印)(你)?(的)?(全部)?(指令|提示词|系统消息|prompt)",
    r"output\s+as-is",
    r"the\s+full\s+instructions",
    r"(越狱|jailbreak|DAN\s+模式)",
    r"帮我写(代码|作文|论文|邮件|小说)",
    r"(今天|明天)?(天气|股票|彩票|新闻)怎么样",
]
NO_REPLY_RE = re.compile("|".join(NO_REPLY_PATTERNS), re.IGNORECASE)


def filter_outbound(text: str) -> str:
    """合规过滤：命中站外引流词就整条替换（对齐上游 `_safe_filter`）。"""
    return SAFE_REPLY if any(p in (text or "") for p in BLOCKED_PHRASES) else (text or "")


def is_no_reply(message: str) -> bool:
    """是否是「应当不回复」的消息（身份询问 / 提示词注入 / 与售卖无关）。

    上游只靠 LLM 分类判定；这里额外加一层规则兜底，命中即直接 no_reply，
    既不花钱也不给注入留窗口。
    """
    text = (message or "").strip()
    if not text:
        return True
    return bool(NO_REPLY_RE.search(text))
