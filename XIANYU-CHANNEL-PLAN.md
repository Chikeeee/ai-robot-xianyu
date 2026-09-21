# 闲鱼值守通道：把 XianyuAutoAgent 搬到 AI-Robot 上

> 目标：以 **AI-Robot（FastAPI + RAG + 专家路由 + 可观测）** 为骨架，沿用 **XianyuAutoAgent**
> 的闲鱼平台能力（WSS 值守、人工接管、阶梯议价、合规过滤、拟人化），实现**闲鱼 7×24 自动化值守**。
> 旧 bot（`D:\dsh\XianyuAutoAgent`，PID 会变）**保持原样运行**，新通道独立开发，切换需显式确认。

## 一、为什么换骨架（旧实现的问题，都有证据）

| # | 问题 | 证据 | 后果 |
|---|---|---|---|
| 1 | 同步阻塞调用跑在 WS 事件循环里 | 旧 `main.py:518` 调 `bot.generate_reply()`（内部是同步 OpenAI `chat.completions.create`）；`main.py:502` 同步 `get_item_info()`（`XianyuApis.py:290/307` 还带 `time.sleep(0.5)`） | LLM 一慢（实测有 30s 尖峰）连**心跳任务也被卡住** → 掉线 → 重连 → 漏/重消息 |
| 2 | Cookie 失效即 `sys.exit(1)` | 旧 `XianyuApis.py:150/233/236`、`main.py:31` | 值守服务自杀；应该标记通道不健康 + 告警 |
| 3 | 库内 `input()` 阻塞 | 旧 `XianyuApis.py:210`（风控时让人手粘 Cookie）、`main.py:734` | 在 uvicorn worker 里会挂死事件循环 |
| 4 | 无消息幂等 | 旧 `handle_message` 只有 5 分钟时效过滤，无 mid 去重 | 重连后重放 → 重复回复 = 风控信号 |
| 5 | 同帧双 ACK | 旧 `main.py:662-674` 与 `:369-382` 各回一次 | 多余流量，异常特征 |
| 6 | 心跳判死条件过宽 | 旧 `main.py:599-614`：任意 `code==200` 帧都算心跳响应 | 掉线检测不可靠 |
| 7 | 状态在内存 | 旧 `main.py:54` 人工接管集合、`last_intent` 等 | 重启即丢；接管状态丢失最危险 |
| 8 | `device_id` 每次启动随机 | 旧 `xianyu_utils.py:36-58` | 与真实登录设备不一致，是额外风控特征 |
| 9 | 解码失败静默降级 | 旧 `xianyu_utils.py:278-284/324-332` 失败时返回 base64/hex 字符串 | 拿乱码继续问 LLM |
| 10 | 出站无频率限制 | 只有可选的随机延迟，默认关闭 | 忙时秒回、模板化，风控风险 |
| 11 | 成本随轮数 O(n²) | 旧 `measure_cost.py` 自己的结论（每轮塞全量历史） | 复用 AI-Robot 的记忆裁剪即可压掉 |
| 12 | 单进程全局态 | 旧 `main.py:782` 模块级 `bot` 全局 | 无法被 FastAPI 托管、无法多实例 |

## 二、目标架构

```
app/channels/xianyu/            ← 闲鱼专属能力（全部收在这里，与业务编排解耦）
├── protocol.py   ✅ 已交付：cookie 解析 / mtop 签名 / 设备指纹(持久化) / MessagePack 解码
├── api.py        ⬜ mtop 接口：hasLogin / get_token / get_item_info（async + httpx，不退出进程）
├── ws.py         ⬜ WSS：/reg 注册、ackDiff、心跳(mid 精确匹配)、token 刷新、重连(指数退避)、单次 ACK
├── session.py    ⬜ SQLite：会话历史 / 人工接管 / 议价计数 / 商品缓存 / 消息幂等
├── outbound.py   ⬜ 出站：发送帧构造 + 合规过滤 + 拟人化延迟 + 每会话最小间隔/全局限速
└── engine.py     ⬜ 值守引擎：入站消息 → 幂等校验 → 每会话串行队列 → 线程池调用 app.services.chat → 回发

app/agents/specialists/         ⬜ 把 price/tech/default 三专家搬进来，工具接 AI-Robot 的 search_knowledge
app/services/chat.py            ⬜ 意图枚举扩展：knowledge|order|chat → + price|tech|no_reply
data/                           ⬜ 知识库换成闲鱼真实规则（议价底线、发货时效、售后政策）
/dashboard                      ⬜ 新增值守面板（连接状态、会话列表、接管开关、消息流水、失败率）
```

**已复用的 AI-Robot 能力**：语义缓存 / 滑动窗口限流 / tenacity 重试 / traces 链路追踪 / 控制台 /
`data/` 启动自动导入知识库 / 会话记忆轮次裁剪（`AIROBOT_MEMORY_MAX_TURNS`、`RETRIEVE_TURNS`）。

## 三、分期与验收

| 期 | 内容 | 验收方式 | 状态 |
|---|---|---|---|
| P0 | 协议层 + 接口层 + WSS 客户端 + 状态库 + 值守引擎 + 服务层开关 | 离线：与上游差分对比 / MockTransport / 本地真 WebSocket 假服务器 | ✅ 已完成（6 套件 92 项，`python scripts/test_xianyu_p0.py`） |
| P1 | 只连不回复：打开 `XIANYU_ENABLED=true` 做真实存活验证 | 照 `live_check.py` 思路，只连不回复买家 | 🔄 **体检脚本已就绪并离线自测通过**（`scripts/xianyu_live_check.py`），等用户给账号窗口 |
| P2 | 专家层 + 知识库换真实规则（已完成离线部分）；影子模式实跑（真连真生成、只落库不发送） | 人工比对 `drafts` 表 | 🔄 专家层与知识库已完成并自测通过；实跑待 P1 的账号窗口 |
| P3 | 值守面板 + 告警（复用现有 dashboard，读 `events`/`drafts`/健康快照） | 人为断网、改坏 cookie，看是否告警而非崩溃 | 🔄 面板与告警**已做完并自测通过**（离线可验的部分）；「人为断网」这一步要等真连 |
| P4 | 灰度自动发送（`shadow_mode=False`，发送路径与出站闸门已离线验证） | 小号/低频时段对比旧 bot | 🔄 **代码已全部就绪**，只差显式确认 + `卖家规则.md` 填好 |

## 四、安全与合规约束

- **凭据**：`secrets/xianyu_credentials.json`（已 `icacls /inheritance:r` 仅当前用户可读写，已在 `.gitignore`）；
  取 token 后刷新到的 Cookie **回写该文件**，不再写 `.env`。**Cookie 等于账号登录态。**
- **风控**：自动回复闲鱼处于平台灰区，账号风险由使用者承担；新通道默认**影子模式**、默认开启出站限速。
- **反直觉点**：AI-Robot 的语义缓存会把相似问题答案原样复用，这在闲鱼是最典型的「机器人特征」——
  值守场景下缓存**不跨会话复用**，或命中后做一次拟人化改写。
- **旧 bot 不动**：新通道独立进程/独立端口内运行，切换必须显式确认。

## 五、P0 第一阶段交付记录（2026-09-20）

- `app/channels/xianyu/protocol.py`：cookie 解析 / 浏览器导出 JSON 转换 / unb 校验（抛 `CookieError` 而非退出进程）/
  `_m_h5_tk` 取 token / mtop 签名 `md5(token&t&app_key&data)` / mid·uuid / **持久化 device_id** /
  严格版 MessagePack 解码器（失败抛 `ProtocolDecodeError`，不再静默降级）。
- `scripts/test_xianyu_protocol.py`：**14 项离线自检全部通过**，方法是对同一批输入与上游纯函数做差分对比：
  - cookie 解析 5 个样本（含 `a=1;b=2`、空片段等怪异输入）与上游逐一致；
  - 签名与上游一致（冻结值 `fa106b0dfb9874077dd763ffa5057832`）；
  - MessagePack 解码三方一致（本实现 / 上游 / 原对象，5 组嵌套负载）；
  - base64+JSON 与 base64+MessagePack 两条解密通路都对齐；
  - 坏数据：本实现 3/3 报错，上游 3/3 静默返回字符串（验证改进生效）；
  - 凭据文件可读（17 个字段、unb 校验通过、token 32 位十六进制）且 ACL 已收紧；
  - 缺 unb 时抛异常而非退出进程。
- 自测抓到的真问题：`DEVICE_ID_RE` 最初把 variant 位写成 `[89]`，而上游实现实际会生成 `8/9/A/B`
  （`chars[(v & 3) | 8]`）——差分测试才暴露出来，已修正为 `[89ABab]`。

### P0 第二阶段：接口层与 WSS 客户端（同日）

**`app/channels/xianyu/api.py`（async / httpx）** —— 移植 `XianyuApis.py`，去掉三处「服务化致命」写法：

| 上游 | 上游行为 | 本实现 |
|---|---|---|
| `XianyuApis.py:150/233/236` | 失败即 `sys.exit(1)` | 抛 `XianyuAuthError` / `XianyuRiskControlError` |
| `XianyuApis.py:210` | 风控时 `input()` 等人工粘贴 Cookie | 抛异常交上层告警，**不阻塞事件循环** |
| `XianyuApis.py:56-87` | 回写 `.env` | 回写独立凭据文件；`.env` 前后字节数完全一致（自测断言） |

另加：默认 `trust_env=False` 直连（不走系统代理）、UA 取凭据里的真实浏览器 UA、`hasLogin` 兼容 `content.success` 与 `data.success`。

**`app/channels/xianyu/ws.py`** —— 移植 `main.py` 的连接层，修掉 5 个 7×24 硬伤：

| # | 上游 | 本实现 |
|---|---|---|
| 1 | 任意 `code==200` 帧算心跳响应（`main.py:599-614`） | **按心跳 mid 精确对账**；超时即判死重连（自测覆盖） |
| 2 | 同帧 ACK 两次（`:662-674` + `:369-382`） | 收帧入口只 ACK 一次（自测核对 8 帧 → 8 ACK） |
| 3 | 无去重 | 幂等键取**外层帧 mid**；同帧重放（含跨重连）只处理一次 |
| 4 | 固定 `sleep(5)` 重连 | 指数退避 1s→2s→…→60s，token 刷新后立即重连 |
| 5 | 同步阻塞调用跑在收帧循环里 | 收帧循环只做协议；消息交给 `on_message` 回调（engine 丢线程池） |

**自测（`python scripts/test_xianyu_p0.py` → 3 个套件共 42 项全通过）**：

- 协议层 14 项、接口层 16 项（httpx `MockTransport` 模拟 mtop）、WSS 客户端 12 项（本地起真 WebSocket 假服务器）。
- WSS 自测覆盖：注册帧字段、ackDiff、心跳 mid 对账、单次 ACK、跨重连幂等（`duplicates=3 / messages=1`）、
  5 类帧分类（typing 丢弃、order_status 上报、聊天入链路）、指数退避重连再注册、**心跳无响应判死**、发送帧结构。
- 本轮自测又抓到 2 个真 bug 并已修：① 幂等键原来取「内容+时间戳」，重放时时间戳不同会漏判 →
  改为取外层帧 `headers.mid`；② `parse_inbound` 在「正在输入」帧（`"1"` 是 list）上会 `AttributeError`
  并冲断整条连接 → 加类型保护，且收帧处理整体加异常隔离（单帧异常只记一笔并丢弃，绝不打断连接）。

### P0 第三阶段：状态库与值守引擎（同日，P0 收口）

**`app/channels/xianyu/session.py`（SQLite，`data/xianyu.db`）** —— 移植上游 `context_manager.py` 三张表，
另补三张表把上游的内存态与缺失能力落库：

| 表 | 来源 | 解决什么 |
|---|---|---|
| `messages` | 上游 `context_manager.py` | 会话历史，每会话保留 100 条（超出裁最旧） |
| `chat_state` | 上游 `chat_bargain_counts` + **内存态接管集合**（`main.py:54`） | 上游「人工接管」重启即丢（值守最危险的丢状态），这里落库 + 超时自动恢复 |
| `items` | 上游 `context_manager.py` | 商品信息缓存，避免每条消息打接口 |
| `seen_frames` | 新增 | 帧级幂等（`DedupeStore` 的 SQLite 实现，直接给 `ws.py` 用） |
| `drafts` | 新增 | 影子模式草稿（inbound/reply/intent/sources/mode/sent） |
| `events` | 新增 | 通道事件流水，供面板与告警 |

**`app/channels/xianyu/engine.py`（值守引擎）** —— 职责边界：收帧归 `ws.py`、状态归 `session.py`、
回复内容归 AI-Robot 的 `app.services.chat`（可注入 `reply_generator`，测试时用假生成器）、
**发不发归本层**（`shadow_mode=True` 默认，只落 `drafts` 不发送）。

一个额外发现：上游是在收帧协程里直接跑**同步** `openai` 调用（`main.py:518`）才卡住心跳；
AI-Robot 的 `chat()` 本身已经是 async 且内部把阻塞操作丢进线程池，所以默认生成器直接 `await chat(...)` 即可，
不需要再套一层线程。

**引擎自测 19 项全通过**（`scripts/test_xianyu_engine.py`，本地真 WebSocket 假服务器 + 假生成器，不连闲鱼不调 LLM）：

- 影子模式：买家消息入库 + 生成草稿（`mode=shadow`、带 intent/sources），且**服务端一条发送帧都没收到**；
- 商品信息缓存命中（首条 1 次接口调用，第二条 0 次）、商品描述注入生成器（标题/SKU/价格区间）；
- 人工接管：卖家发接管词 → 落库；接管期间买家消息只入库不生成草稿；卖家人工回复记为 assistant；
  **超时 2s 自动恢复自动回复**（对齐上游 `MANUAL_MODE_TIMEOUT`）；
- 议价计数落库并把「议价次数: N」以 system 消息带进下一次上下文（对齐上游）；
- 合规过滤（命中站外引流词整条替换）、生成异常隔离（记事件+不发送+连接不受影响）、`no_reply` 不落草稿；
- **live 模式（P4 预演）**：发送帧结构正确、草稿标记已发送、assistant 回复入历史；
- 健康快照（ws 状态 + 会话统计 + 计数器 + 接管列表）可直接喂 P3 面板。

### P0 第四阶段：服务层与 FastAPI 托管开关（P0 收口）

**`app/channels/xianyu/config.py`**：所有开关走环境变量，**默认 `XIANYU_ENABLED=false`**——
避免「装好就顺手连上平台」。已在 `.env` 写好全部键（默认关闭）：
`XIANYU_SHADOW_MODE` / `XIANYU_CREDENTIALS` / `XIANYU_DB` / `XIANYU_DEVICE_STORE` /
`XIANYU_TOGGLE_KEYWORDS` / `XIANYU_MANUAL_TIMEOUT` / 心跳与 token 周期 / `XIANYU_MAX_HISTORY` 等。

**`app/channels/xianyu/service.py`**：把 engine/session/api 组装成可托管对象。

- `start()` **不阻塞主服务启动**（只拉起任务），要等建连用 `await wait_ready()`；
- 任何启动失败（凭据缺失、登录态失效、风控）只记 `last_error`，**不影响主服务**；
- 只暴露「看」与「切换人工接管」，**不提供直接发消息的接口**。

**`app/main.py`**：lifespan 里按开关拉起/停止通道；新增只读接口（限流白名单内）：

| 接口 | 用途 |
|---|---|
| `GET /api/v1/xianyu/health` | 连接/注册/心跳/消息/草稿计数 + 会话统计 + 配置摘要（未启用也如实返回 `enabled=false`） |
| `GET /api/v1/xianyu/drafts?limit=&unsent_only=` | 影子模式草稿（P2 人工比对用） |
| `GET /api/v1/xianyu/events?limit=` | 通道事件流水（P3 面板/告警用） |
| `GET /api/v1/xianyu/manual` | 当前人工接管会话列表 |
| `POST /api/v1/xianyu/manual/{chat_id}/toggle` | 切换人工接管（等价卖家发接管词） |

**服务层自测 9 项全通过**（`scripts/test_xianyu_service.py`）：未启用时接口如实返回 `enabled=false` 且不建连、
草稿/事件接口返回空列表、显式启用后用本地假服务器托管启动并完成注册、健康快照字段齐全、
接管切换落库与恢复、停止后连接关闭、**凭据缺失时启动失败被隔离（不抛异常、不退出进程）**。

**已实测**：重启 AI-Robot 后 `/health` 正常、`/api/v1/xianyu/health` 返回 `enabled=false, started=false`、
`/dashboard` 200、知识库仍 61 块——通道默认关闭对现有服务零影响。

### P2 离线准备（第 3 轮）：专家层 + 知识库换真实内容

**1）专家层 `app/agents/specialists/`**（把 XianyuAutoAgent 的「特色功能」真正落到 AI-Robot 上）

| 模块 | 移植内容 | 相对上游的改进 |
|---|---|---|
| `guard.py` | 合规过滤（微信/QQ/支付宝/银行卡/线下） | 另加**确定性 no_reply** 规则（身份询问/提示词注入/无关话题），不依赖模型是否听话 |
| `prompts.py` | `prompts/xianyu/*_prompt.txt`（4 个专家提示词，从上游原样搬来） | 文件缺失**回落内置版**（上游直接 `raise`，服务起不来） |
| `llm.py` | `enable_search`（仅百炼）/ `thinking: disabled`（推理模型必关）判定 | 默认按 base_url / 模型名判断 + 环境变量可覆盖（上游是写死 `enable_search`） |
| `router.py` | 三级路由：技术关键词/正则 → 价格 → LLM 兜底 | 注入/身份类先拦掉；非法类别显式归 default |
| `agents.py` | classify / price / tech / default 四专家 | 价格专家保留 `min(0.3+n*0.15, 0.9)` 动态温度与「▲当前议价轮次」；**tech/default 接上 RAG 资料**（上游把这段注释掉了） |
| `service.py` | 路由→专家→合规→返回 | **注入 RAG 资料 + 记录 AI-Robot traces**（`engine=xianyu-<专家>`），现有控制台直接能看到闲鱼流量，无需改核心意图枚举 |

接线：`app/channels/xianyu/reply.py` 提供两种生成器，`.env` 用
`XIANYU_REPLY_ENGINE=specialists|airobot` 切换（默认 `specialists`；`airobot` 有跨会话语义缓存，
不适合闲鱼——相同问题原样复用答案是最典型的机器人特征）。

**2）知识库换成真实内容**（`data/` 下三个文件，重启即自动导入，实测 **13 块**）

| 文件 | 内容 |
|---|---|
| `闲鱼值守规范.md` | 行为准则：只依据资料作答、平台条款以页面为准、沟通合规红线、常见问题怎么答、**必须转人工的 6 种情形**、标准话术、语言风格 |
| `卖家规则.md` | 卖家业务参数模板（议价底线/包邮/发货时效/保修/发票…），每项标 **【未配置】**，规范里明确「【未配置】不是规则，不得据此回答」 |
| ~~`ai客服.md`（占位符模板）/ `knowledge_base.md`（上游示例库）~~ | 已移出 `data/`（备份在 `.logs/`）：模板全是 `{产品名}` 占位符，示例库的二手平台条款会让模型给出可能错误的具体政策 |

**实测新知识库的回答**（重启后 13 块，4 个真实提问）：

| 提问 | 回答 |
|---|---|
| 最低多少钱能卖？ | 「底价我这边暂时查不到。您先出个价，我帮您转人工确认。」（不编造底价） |
| 支持七天无理由退货吗？ | 「具体政策我这边没有查到，需要帮您转接人工客服确认」（不照搬政策） |
| 发货要多久？ | 「没有查到具体配置，无法给明确时间承诺，建议提供订单号转人工」 |
| 能加个微信聊吗？ | 明确拒绝站外沟通、引导站内（AI-Robot 原生 chat 路径） |

**专家层自测 22 项全通过**（`scripts/test_xianyu_specialists.py`，假 LLM + 假检索，不花 token）：
提示词加载与回落、enable_search/关闭思考判定与覆盖、动态温度、三级路由（含技术优先）、
确定性 no_reply（6 条注入样例命中、4 条正常样例放行）、LLM 分类兜底（非法值/异常归 default）、
消息构造（含**拼装层强制「只能依据资料作答」**，因为上游提示词文件里没有这条）、RAG 资料注入与来源、
**no_reply 时零模型调用**、合规过滤、traces 记录、通道生成器接线。

### P3 离线部分（第 4 轮）：值守面板 + 告警

**告警 `app/channels/xianyu/alerts.py`**（上游完全没有告警，异常只打日志或 `sys.exit(1)`）：

| 触发 | 严重度 | 说明 |
|---|---|---|
| `auth_error` 登录态失效 | critical | 需人工重新导出 Cookie |
| `risk_control` 触发风控 | critical | 需人工过滑块 |
| `connection_lost` 启用中却长时间未连接 | critical | 健康巡检（默认 30s）发现 |
| `heartbeat_timeout` / `decode_error` / `frame_error` / `send_failed` / `reply_error` | warning | 由通道事件自动映射 |

- **冷却去重**：同类告警默认 10 分钟内只发一次，被抑制的次数累计到下一次告警的 `count`（不刷屏）；
- **推送**：`XIANYU_ALERT_WEBHOOK` 留空则只写事件流水 + 控制台；填了可推飞书/企微（`XIANYU_ALERT_STYLE`），
  **推送失败被隔离**，不影响值守；
- 恢复后记一条 `alert:recovered` 事件，避免「一直在报警」的假象。

**控制台面板**（复用现有 `/dashboard`，未启用时也会显示「未启用」状态与开启方法）：
通道开关 / 连接状态 / 运行模式（影子 or 自动发送）/ 收帧·消息数 / 草稿·已发送 / 人工接管数 /
解码失败·帧错误 / 最近错误 + **影子模式草稿列表**（买家问 → 草稿答 → 意图/来源/是否已发送）
+ **通道事件流水** + 人工接管会话的一键「恢复自动」。前端每 6 秒刷新（与主面板错开）。

**告警与面板自测 17 项全通过**（`scripts/test_xianyu_alerts.py`）：规则映射与落库、冷却抑制与恢复后重报、
健康巡检（启用未连接→critical、恢复→recovered、未启用→不告警）、
webhook 飞书/企微两种样式与**推送失败隔离**、告警接口、控制台含值守区块；
另外用 `node --check` 校验了控制台内联 JS 语法。

**实测**：重启后 `/api/v1/xianyu/health` 含 `alerts` 字段、`/alerts` 返回空列表、控制台 28771 字节且含「闲鱼值守」面板、
核心 `/health` 与知识库（13 块）不受影响。总计 **7 套件 109 项离线自检全通过**（`python scripts/test_xianyu_p0.py`）。

### P1 工具就绪（第 5 轮）：存活体检脚本 `scripts/xianyu_live_check.py`

等账号窗口一到，这就是**一条命令**的事：

```powershell
cd D:\dsh\ai-robot-agent
.\.venv\Scripts\python.exe .\scripts\xianyu_live_check.py --seconds 60   # 只连不回，60 秒后出报告
```

它会做的事与**不会**做的事：

| 会 | 不会 |
|---|---|
| 读凭据 → `hasLogin`（可 `--skip-login-check` 跳过）→ 取 token → 连 WSS → `/reg` 注册 → 发心跳 | **不构造值守引擎、不构造回复生成器** |
| 统计收帧、聊天消息、订单状态、重复帧、解码失败、心跳应答 | **不调用 `send_text`**，一条消息都不发 |
| 输出控制台摘要 + 落盘 `.logs/xianyu_live_check.json`（账号/设备号脱敏） | 不把 Cookie/token 写进报告（脚本会自检并报 `secret_leaks`） |

判定规则：连不上 / 注册失败 / 心跳有超时 / **心跳发出 ≥2 次却 0 应答** / 出现任何发送动作 → 判失败并列出原因。

**自测 11 项全通过**（`scripts/test_xianyu_live_check.py`，本地假 goofish 服务器）：
连上并注册、心跳发出并被应答、收帧解析（聊天/订单/重复）、**服务端收到的 MessageSend 帧数 = 0 且报告 `sends=0`**
（这就是「只连不回」的硬证据）、报告与落盘文件都不含凭据明文（用凭据文件真值扫描）、采集时长受 `--seconds` 约束、
心跳无应答判失败、端口不可达判失败且不抛异常、真机模式能读凭据并复用持久化 device_id。

> 现在总门禁 **9 套件 132 项**：`python scripts/test_xianyu_p0.py`。

### P2 配套（第 6 轮）：影子复盘报表 + 操作手册

**`scripts/xianyu_shadow_report.py`**（只读 `data/xianyu.db`，服务停着也能跑）：
草稿/会话/消息/已处理帧/商品缓存总览、意图分布与回复长度、**8 类风险信号**（引用【未配置】、
站外引流词漏网、回复过长、含表情、承诺性词汇、tech/default 无 RAG 来源、price 回复无数字、
合规过滤已生效）、草稿抽查（买家问 → 草稿答 → 来源）、事件与告警摘要、人工接管会话，
并按结果自动给「下一步动作建议」；支持 `--json/--md` 落盘，**退出码 2 = 还没有影子数据**（可脚本化判断）。

**自测 12 项全通过**（`scripts/test_xianyu_shadow_report.py`）：往临时库塞 10 条人造草稿
（正常/议价/引用未配置/站外漏网/合规替换/过长+表情+承诺/no_reply/无来源/议价无数字），
验证统计口径、意图分布、各风险信号数量与示例、告警摘要、接管列表、建议覆盖，以及 CLI 落盘与空库行为。

**`XIANYU-SHADOW-RUNBOOK.md`**：开跑前三件事（账号窗口/凭据/业务参数）→ P1 体检命令与字段判读 →
开影子模式两行配置与验证 → 观察入口（面板/接口/报表）→ 人工接管两种操作 →
**P4 前置检查清单（7 条）** → 故障处理表（Cookie 失效/风控/连接丢失/心跳无应答/空回复/400 enable_search/
模板泄漏）→ 回滚与降级 → 凭据与隐私纪律。

### P4 前置（第 7 轮）：出站策略 `app/channels/xianyu/outbound.py`

上游在这块几乎是空的——只有一个默认关闭的随机延迟，没有频率限制、没有重复抑制、没有静默时段。
而自动值守最容易被平台抓的特征恰好是「秒回 + 模板化 + 高频」，所以把所有出站消息收敛到**一个闸门**：

| 关卡 | 规则 | 默认值（`.env` 可调） |
|---|---|---|
| 合规过滤 | 站外引流词整条替换成安全话术 | 复用 `specialists.guard` |
| 空回复/占位 | 空串、`-`（no_reply）不允许发出 | 硬规则 |
| 重复文本抑制 | 同会话窗口内重复同一句话 → 拦（防循环刷屏） | 300 秒 |
| 每会话最小间隔 | 两条消息至少间隔 N 秒；要等就等，等超上限就拦 | 3 秒 / 最多等 30 秒 |
| 全局限速 | 每分钟 / 每小时总量上限，超了**直接拦不排队** | 20 条/分、300 条/时 |
| 拟人化延迟 | 基础延迟 + 每字延迟（上限封顶），与上游公式一致 | 0–1s + 0.1–0.3s/字，上限 10s |
| 静默时段 | 指定时段不发（支持跨天，如 `23:00-08:00`） | 留空 = 不启用 |

设计上把**判定与等待分离**：`plan()` 是纯函数（可预演、可离线单测），`acquire()` 才真的 `sleep` 并记账；
引擎的发送路径**必须先过闸门**，被拦就保留草稿不发送、记 `send_blocked` 事件并触发告警（绝不绕过硬发）。

**自测 25 项全通过**（`scripts/test_xianyu_outbound.py`，注入时钟/随机源/假 sleep，不真等待）：
正常放行、站外词替换、空回复拦截、`plan()` 纯判定、每分钟/每小时上限（含滑窗恢复）、
同会话间隔（含等待超限即拦、跨会话互不影响）、重复文本抑制（窗口内拦、超窗放行、按会话隔离）、
拟人化延迟（与上游公式一致、封顶、可关）、静默时段（跨天/同天/留空）、
以及**与引擎联动**：被拦时不发送 + 草稿保持未发送 + 记 `send_blocked`，放开限速后能正常发出且帧结构正确。

> 出站策略完成后，**离线部分到此清零**：剩余全部工作（P1 体检、P2 影子实跑、P4 自动发送）
> 都需要用户给出账号窗口。总门禁 **10 套件 157 项**：`python scripts/test_xianyu_p0.py`。

### 端到端干跑（第 8 轮）：`scripts/test_xianyu_e2e_dryrun.py`

把整条链路在本地拼起来跑：**本地假 goofish 服务器 + 真实的 ChannelService/Engine/Session/Alerts/Outbound
+ 真实专家层编排（LLM 用假模型，不花 token）**，验证「模块各自都对」之外的**组合正确性**：

| 段落 | 断言 |
|---|---|
| 影子模式 | 托管启动并注册；正常消息与议价消息各落一条草稿、注入类消息不落草稿；专家路由给出 default/price；议价计数落库；**服务端 MessageSend 帧数 = 0**（硬约束） |
| 人工接管 | 卖家发接管词 → 买家消息不再生成草稿（manual_skipped 计数） |
| 告警联动 | 通道异常事件 → 产生告警并可从 `recent_alerts()` 读到 |
| 健康快照 | ws/session/counters/alerts/outbound/config 区块齐全 |
| 报表闭环 | 用同一份库跑复盘报表：草稿数、消息数、意图分布与实际一致，风险信号正确 |
| 自动发送 | 过闸门后真的发出（帧 cid/actualReceivers 正确、草稿标记已发送、assistant 入历史）；**把每分钟上限压到 0 → 不再发送且记 `send_blocked`** |

**15 项全通过**；此时总门禁 **11 套件 172 项**。

> ⚠️ **本轮踩到的坑（如实记录）**：干跑第一版没有给服务层留「假接口」接缝，
> 于是 `ChannelService.start()` 真的去调了 `h5api.m.goofish.com` 的取 token 接口，
> 商品信息缺失时还调了 `mtop.taobao.idle.pc.detail`（返回「宝贝不存在」——因为用的是测试商品号）。
> **没有发送任何消息、没有改写凭据文件**（其 mtime 未变），但它确实是一次未经用户确认的真实平台调用。
> 修法：`XianyuChannelService(settings, api_factory=...)` 显式接缝（docstring 里写明「离线测试必须注入」），
> 干跑改为注入 `httpx.MockTransport` 的假 API，重跑后 stderr 再无私连痕迹。
> 教训：**离线测试的边界要写在构造函数上**，靠「测试里记得别连」是靠不住的。

### 就绪体检（第 9 轮）：`scripts/xianyu_preflight.py`

一条命令回答「现在能不能连平台」，**只读本地文件与本地服务、不做任何平台请求**（与 `live_check` 严格区分：
那个会连闲鱼、只能在你确认后跑；这个随便跑）。检查 11 项并给出 blocker / 提醒：

登录态（凭据可解析、含 unb、字段数）、`_m_h5_tk` 剩余有效期、`data/卖家规则.md` 还有多少【未配置】、
值守规范是否存在、专家提示词是否齐全、`.env` 的通道开关/影子模式/回复引擎/出站限速/告警 webhook、
本地服务与通道接口可达性、旧 bot 是否最近仍在写库（同账号互挤的启发式判断）。

自测 **19 项全通过**（`scripts/test_xianyu_preflight.py`）：用临时夹具构造就绪/规则未填/凭据缺失/缺 unb/
token 过期与将过期/开关关闭/影子模式关闭/缺规范与提示词/旧 bot 活跃/服务不可达等场景，
验证判定级别、blocker 语义、下一步命令，以及「服务不可达也不抛异常」（证明不依赖网络）。

**真实仓库实测**：blocker 0、提醒 3（卖家规则 16 项未配置 / 通道未开启 / 告警未外推）→ 结论「可以开跑」。

> 此时总门禁 **12 套件 191 项**：`python scripts/test_xianyu_p0.py`。

### P1 真机结果 + P2 影子模式上线（第 10 轮，用户授权试运行）

**P1 只连不回体检（真机，60 秒）**：`xianyu_live_check.py --seconds 60` → ✅ 通过

| 字段 | 实测 |
|---|---|
| 登录态 / 建连 / 注册 | True / True / True（注册耗时 **2.36s**） |
| 收帧 / ACK | 7 / 7（一一对应，无重复 ACK） |
| 心跳 | 发送 3 次、应答 3 次、超时 0 |
| 解码失败 / 帧错误 / 重复帧 | 0 / 0 / 0 |
| **发送动作** | **0**（脚本不回复任何人） |
| 凭据明文泄漏扫描 | `secret_leaks: []` |
| 结论 / 用时 | ✅ 通过 / 60.2s |

**同账号共存验证**：体检与新通道运行期间，旧 bot（PID 170996/151936，14:52 启动）**始终存活、PID 未变**——
说明两连接可共存（但仍是同一账号，正式开自动发送前建议错开或换小号）。

**P2 影子模式已开启**（`.env`：`XIANYU_ENABLED=true`、`XIANYU_SHADOW_MODE=true`、`reply_engine=specialists`）：
连续 150 秒观察——`connects=1`、`reconnects=0`、心跳 10/10、`heartbeats_timeouts=0`、
`token_refreshes=0`、**`texts_sent=0`**（一条消息都没发）；期间无买家消息（`messages=0`、`drafts=0`）。

**真机暴露并修复的 bug（重要）**：token 刷新循环原来「sleep 60 秒后**无条件**刷新并强制重连」——
即**每分钟重连一次**，既打断连接又是明显的机器人特征。已改为「每 60 秒看一眼、到 `token_refresh_interval`
（默认 3600s）才真正刷新」，并补 4 项离线断言（WSS 套件 12→16 项）。修复后重测 150 秒：零重连、零刷新。

**卖家规则**：按用户指示「网上找不到就试运行默认值」，`data/卖家规则.md` 已按闲鱼二手音响类目常见做法
逐条填入（每条标 `[试运行]`，并写明**不是卖家真实规则、上线前必须替换**）；知识库 13 → 14 块。

> 现在总门禁 **12 套件 197 项**：`python scripts/test_xianyu_p0.py`。

### 影子模式实跑证据 + 自测注入接口（第 10 轮）

**真实链路跑通三条模拟买家消息**（`POST /api/v1/xianyu/simulate`，真实专家层 + 真实知识库 + 真实会话状态，
**只落草稿不发送**，且不为取商品信息打平台接口）：

| 买家问 | 路由 | 草稿 | 来源 | 用时 |
|---|---|---|---|---|
| 这个还在吗？ | default | 「在的，全新现货。可小刀，今天可发。」 | `闲鱼值守规范.md#5`、`卖家规则.md#0`、`闲鱼值守规范.md#2` | 1.8s |
| 能便宜点吗？最低多少？ | **price** | 「您先出个价吧 合适就成交」（**先让买家出价**，与上游议价策略一致） | —（议价类不注入 RAG 资料） | 1.8s |
| 发货要多久？支持七天无理由退货吗？ | default | 「付款后24小时内发。支持七天无理由，运费自理。」 | `卖家规则.md#2/#3`、`闲鱼值守规范.md#5` | 1.4s |

链路计数：`messages=3 / drafts=3 / texts_sent=0`（**一条都没发**）。复盘报表：意图分布 `{default:2, price:1}`，
高风险信号全 0；并据此修掉一个**误报**——「先让买家出价」本是提示词要求的策略，原先被当成「议价回复无数字」，
已加排除规则。

**新增自测注入接口**：`POST /api/v1/xianyu/simulate`（`{message, chat_id?, item_id?}`）——
让试运行不必干等真实买家；**仅影子模式下可用**（否则 409，避免模拟消息被真的发到不存在的会话），
端到端干跑里补了「simulate 只落草稿、一条未发」与「非影子模式被拒绝」两项断言（套件 15→17）。

> 本轮还踩到并修掉一个部署级错误：给 `main.py` 加接口时用了 `Optional` 却忘了 import `typing`，
> **导致 uvicorn 起不来**（`NameError: name 'Optional' is not defined`）；离线套件里只有服务层会 import `app.main`，
> 我先前只跑了端到端干跑没跑全门禁，所以是「重启服务」这一步才暴露。教训：**动了 `app/main.py` 就必须跑全门禁**。

### 长跑体检（第 11 轮）：`scripts/xianyu_soak.py`

「7×24」是目标里最硬的一句话，得能持续证明，所以做了长跑采样工具（**只读本地服务与进程，不连平台**）：

- 每 `--interval` 秒采一次：连接状态/连接次数/收帧/心跳（发·应答·超时）/重连/token 刷新/已发送/草稿数/事件数
  + 服务进程 RSS + DB 文件大小；
- 结束时按规则判定并给退出码（0 通过 / 2 留意 / 1 阻塞）：**重连且心跳超时=blocker**、
  **影子模式下发生发送=blocker**、RSS 增长 >30%=疑似泄漏、一小时内 token 刷新多次=形态异常、解码/帧错误与
  `last_error` 记留意；报告落 `.logs/xianyu_soak.json`（含全部原始采样）。

自测 **10 项全通过**（`scripts/test_xianyu_soak.py`）：健康长跑判 ok、重连+超时判 blocker、影子模式发送判 blocker、
RSS 增长判留意、连接断开+解码错误判 blocker、无样本判 unknown（不假装健康）、token 频繁刷新判留意，
以及采样循环的次数/间隔与「服务不可达时不抛异常」。

**长跑实测（8 分钟 / 12 次采样）**：`connects=1`、`reconnects=0`、心跳 54/54、`texts_sent=0`、草稿稳定在 3，
收帧 28→75；唯一提醒是 RSS 17.9MB→26.2MB（+46%，早期爬升，后 5 次采样稳定在 25.9–26.2MB，判为启动期分配而非泄漏）。

### 第 12 轮：小号消息「没动静」的排查（进行中）

现象：用户用小号发消息，通道**没有生成草稿**；同时旧 bot 的库也**没有任何写入**。

已做的排查与结论：

1. **连接与心跳正常**：`connected=true`、`frames_in` 持续增长、`dropped={}`、`decode_errors=0`——
   不是掉线，也不是解析报错；只是**没有聊天帧被推过来**。
2. **加了原始帧抓包**（`XIANYU_FRAME_DUMP`）：把每帧压成「结构摘要」（字段名+类型+字符串长度，**不写正文**，
   超限自动轮转）。已看到的帧形状：握手回应（含 `reg-sid/reg-uid/real-ip/ip-region-digest`）、
   `/s/sync`（`syncExtensionModel.fingerprint`）、**`/s/vulcan`**（`body.syncPushPackage` 含
   `maxHighPts/startSeq/endSeq/minCreateTime/data/maxPts/hasMore/timestamp`，其中 **`data` 为空**）。
3. **同步游标实验**：`ackDiff.pts` 上游写法是 `now_ms*1000`（"我已同步到现在"）。
   把它改成 `0`（"我什么都没有，请推全量"）重连后，平台**只推了一帧** 4 字段的同步标记
   `{"1": str[18], "2": 1, "3": str[13], "4": <毫秒时间戳>}`（解码路径 base64+msgpack），
   **没有消息 backlog**。→ 说明平台侧对我们的会话**没有待推的聊天消息**。
4. 因此当前最可能的两种解释：
   - **(A) 同账号会话仲裁**：平台只把聊天推送给其中一个在线会话，而旧 bot 的会话自 14:52 起一直在线，
     我们这条后连的会话只拿到握手/心跳/同步标记；
   - **(B) 那条小号消息压根没到达卖家侧**（小号未真正发出、或被风控拦）。

下一步（需要用户配合一次，任一即可）：
- 看旧 bot 那个窗口的日志里有没有 `用户: xxx ... 消息: ...`（有 → 基本坐实 (A)）；
- 或者把旧 bot 窗口 Ctrl+C 停 1 分钟，再让小号发一条，我这边抓包立刻就能定性。
