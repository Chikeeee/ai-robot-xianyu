# 闲鱼值守操作手册（P1 → P4）

> 面向操作：从「连上看看」到「灰度自动发送」的每一步、每条命令、每个失败信号怎么办。
> 设计与取舍见 `XIANYU-CHANNEL-PLAN.md`；本文件只讲**怎么操作**。

## 〇、开跑前：先跑一次就绪体检

```powershell
cd D:\dsh\ai-robot-agent
.\.venv\Scripts\python.exe .\scripts\xianyu_preflight.py     # 只读本地，不做任何平台请求
```

它会逐项判定并给出 blocker / 提醒：凭据可解析与 `unb`、`_m_h5_tk` 剩余有效期、`data/卖家规则.md`
还有多少【未配置】、值守规范是否存在、专家提示词是否齐全、`.env` 的通道开关/影子模式/回复引擎/出站限速/告警 webhook、
本地服务与通道接口可达性、旧 bot 是否最近仍在写库（同账号互挤风险）。
**blocker 为 0 才继续往下走**；退出码 0=可以开跑、1=有 blocker。

然后三件事：

| # | 事项 | 怎么做 |
|---|---|---|
| 1 | **账号窗口**：新通道与旧 bot 是同一个号，同时在线可能互相挤 | 停旧 bot（`D:\dsh\XianyuAutoAgent` 的那个窗口 Ctrl+C）→ 跑完体检再启回来；或换小号 |
| 2 | **凭据**：`secrets/xianyu_credentials.json`（ACL 仅本用户、已 gitignore） | Cookie 过期时：网页端重新登录 → F12 取完整 Cookie → 覆盖该文件的 `cookies_str` |
| 3 | **业务参数**：`data/卖家规则.md` | 议价底线/包邮/发货时效/保修/发票逐条填；**没填的部分 AI 会转人工，不会编** |

## 一、P1：存活体检（只连不回，60 秒）

```powershell
cd D:\dsh\ai-robot-agent
.\.venv\Scripts\python.exe .\scripts\xianyu_live_check.py --seconds 60
```

看什么：

| 字段 | 期望 | 不正常时 |
|---|---|---|
| `已连接 / 已注册` | True / True（注册通常 1 秒内） | 看 `problems`：网络/URL、token、握手 |
| `心跳` | 发送 ≥2、应答 ≥2、超时 0 | 「发 N 次应答 0」= 连接被静默丢弃（多半 Cookie/风控） |
| `收帧 / 聊天消息` | 收帧 > 0；有买家消息最好 | 收帧为 0 说明该时段没人说话，不代表故障 |
| `发送动作` | **必须为 0** | 不为 0 就是脚本被动过，立即停止 |
| `结论` | ✅ 通过 | ❌ 时按 `problems` 逐条处理 |

报告落盘：`.logs/xianyu_live_check.json`（账号与设备号已脱敏，无 Cookie 明文）。
跑完记得**把旧 bot 启回来**。

## 二、P2：开影子模式（真连真生成、只落库不发送）

> 当前状态（2026-09-20 试运行）：**已开启并稳定运行**——P1 真机体检通过（注册 2.36s、心跳 3/3、
> 发送 0 条），通道 `enabled=true / started=true / shadow_mode=true`，观察 150 秒 `connects=1 / reconnects=0`。
> 期间无买家消息，所以 `drafts` 还是空的。想看效果：让朋友或小号给在售商品发一条消息即可。

1) 改 `.env`（只动这两行）：

```ini
XIANYU_ENABLED=true
XIANYU_SHADOW_MODE=true
```

2) 重启并确认：

```powershell
.\stop-local.ps1 ; .\start-local.ps1 -NoBrowser
.\.venv\Scripts\python.exe -c "import httpx,json;print(json.dumps(httpx.get('http://127.0.0.1:8000/api/v1/xianyu/health').json(),ensure_ascii=False,indent=2))"
```

期望：`enabled=true, started=true, shadow_mode=true, ws.connected=true, ws.registered=true, last_error=null`。

3) 观察入口：

- 控制台 `http://127.0.0.1:8000/dashboard` 底部「🐟 闲鱼值守」面板（6 秒刷新）：连接状态、草稿列表、事件流水、接管会话；
- 接口：`/api/v1/xianyu/health`、`/drafts`、`/events`、`/alerts`、`/manual`；
- **不等真实买家也能看效果**（推荐先做这一步）：喂一条模拟买家消息，走真实专家层 + 真实知识库，**只落草稿不发送**：

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/xianyu/simulate -H "Content-Type: application/json" `
  -d "{\"message\":\"能便宜点吗？最低多少？\",\"chat_id\":\"SIM-C1\"}"
```

  返回里带 `intent` 与最新草稿；然后在控制台/报表里就能看到它。
  **安全约束**：该接口只在 `XIANYU_SHADOW_MODE=true` 且通道已启动时可用，其他情况返回 409；
  模拟路径也**不会**为取商品信息去打平台接口。
- 复盘报表（推荐每天看一次）：

```powershell
.\.venv\Scripts\python.exe .\scripts\xianyu_shadow_report.py --md .logs\shadow.md
```

报表会给：草稿数/未发送数、意图分布、**风险信号**（引用【未配置】、站外词漏网、回复过长、表情、承诺词、无 RAG 来源、议价无数字）、草稿抽查、事件与告警摘要、以及「下一步动作建议」。退出码 2 = 还没有影子数据。

## 三、人工接管（随时可用，两端都行）

- **客户端**：在该会话里发 `。`（`XIANYU_TOGGLE_KEYWORDS`）→ 切人工；再发一次恢复自动。接管期间买家消息只入库、不生成草稿。
- **控制台**：值守面板里点该会话的「恢复自动」；或调接口
  `curl.exe -X POST http://127.0.0.1:8000/api/v1/xianyu/manual/<chat_id>/toggle`
- 接管有超时（默认 1 小时）自动恢复自动回复；期间你自己发的回复会被记进会话历史。

## 四、P4 前置检查清单（决定是否开自动发送）

逐条确认，全部满足再改 `XIANYU_SHADOW_MODE=false`：

| # | 检查 | 命令 / 看哪里 | 通过标准 |
|---|---|---|---|
| 1 | 影子草稿人工比对通过 | `xianyu_shadow_report.py` | 抽查无明显错误、无【未配置】引用、无站外词漏网 |
| 2 | 业务参数已填 | `data/卖家规则.md` | 议价底线/发货时效/保修等不再是【未配置】 |
| 3 | 合规过滤生效 | 报表「合规过滤已生效」计数 | 有命中即说明拦得住 |
| 4 | 连接稳定 | `/api/v1/xianyu/health` 的 `ws` | 连续观察 ≥1 天无 `heartbeat_timeouts`、`reconnects` 不异常增长 |
| 5 | 告警能到达你 | `.env` 的 `XIANYU_ALERT_WEBHOOK` | 填了飞书/企微机器人地址，且手动触发能收到（见下） |
| 6 | 出站限速 | `.env` 的 `XIANYU_MIN_INTERVAL_PER_CHAT` / `MAX_PER_MINUTE` / `MAX_PER_HOUR` / `TYPING_SIMULATION` | **已实现**（`outbound.py`）：默认每会话 3 秒间隔、20 条/分、300 条/时、拟人化延迟开、重复文本 300 秒内拦；`/api/v1/xianyu/health` 的 `outbound` 字段可看到实时计数与限制 |
| 7 | 灰度计划 | —— | 先低频时段/小号，观察 1–2 天再加量 |

**手动验证告警通道**（不会真的连闲鱼）：临时把 `XIANYU_ALERT_WEBHOOK` 填成你的机器人地址、重启，然后在控制台把某个会话切两次人工接管——不会触发告警；更直接的办法是把 `secrets/xianyu_credentials.json` 临时改名后启动，通道会因「凭据不可用」在健康接口里报 `last_error`（这是启动期错误，走 `last_error` 而非 webhook；webhook 只在实际运行期告警时触发）。

## 四之二、P4 灰度自动发送（已于 2026-09-20 开启，用户明确授权）

### 开之前必须过的三道门

1. **影子复盘报表**（`scripts/xianyu_shadow_report.py`）：high 风险为 0；议价/技术意图都有知识库来源。
2. **发送帧核对**：`scripts/xianyu_preflight_send.py` 必须打印「核对结果: 通过」
   （`cid=<会话>@goofish`、`actualReceivers` 同时含买家与自己、负载是可解码的 `{"contentType":1,...}`）。
3. **出站闸门自带**：合规过滤、每会话最小间隔、每分钟/小时/天限速、重复文本窗口、拟人化打字延迟、静默时段。

### 灰度三级台阶（本次实际走的顺序）

| 台阶 | 配置 | 目的 |
|---|---|---|
| 1 | `XIANYU_SEND_ALLOWLIST=<自己的测试会话>` | **只给自己的会话发**，验证发送链路真的通（零客户风险） |
| 2 | 留空 allowlist | 放开给全部会话 |
| 3 | 视情况放宽 `XIANYU_MAX_PER_DAY` 等 | 以天为单位放大额度 |

**台阶 1 → 2 的验收标准**：`scripts/xianyu_verify_sent.py` 必须判定
「✅ 平台侧已存在我们发出的那条消息」——本端 `texts_sent=1` **只说明帧发出去了**，
只有平台侧**只读**拉回来能看到卖家侧那条，才算真送达。

### 当前生产配置

```
XIANYU_SHADOW_MODE=false         # 真的会发
XIANYU_SEND_ALLOWLIST=           # 空=不限制
XIANYU_MAX_PER_MINUTE=3          # 灰度小额度，先小后大
XIANYU_MAX_PER_HOUR=30
XIANYU_MAX_PER_DAY=30
```

### 想立刻停下来（三档，从轻到重）

```powershell
# ① 只停自动发送、保留连接与草稿（最轻，改完重启）
#    .env: XIANYU_SHADOW_MODE=true    → 恢复"只落库不发送"，草稿照常生成
# ② 只拦某些会话（别人照常）
#    .env: XIANYU_SEND_ALLOWLIST=10000000002   → 只给这个会话发，其余一律拦截（事件里记 not_in_allowlist）
# ③ 整个通道停下来（AI-Robot 其余能力不受影响）
#    .env: XIANYU_ENABLED=false  → 或直接 .\stop-local.ps1
```

**任何一条被拦都会留痕**：`send_blocked` 事件带 `reason`
（`not_in_allowlist` / `rate_limited_day` / `rate_limited_minute` / `rate_limited_hour` /
`duplicate_text` / `quiet_hours` / `chat_wait_too_long` / `empty_reply`），
面板「丢帧统计」与告警面板都能看到，**不存在"以为发了其实没发"**。

### 自动发送上线后的两条硬约定（真机踩出来的）

**① 收帧循环绝不能被业务堵住。** 入站消息只进 `_deliver_queue`，由 `_deliver_loop` 的消费者任务处理；
收帧循环立刻回去收下一帧。若改成内联 `await on_message(msg)`，处理一条消息要十几秒
（商品接口重试 + LLM + 拟人化延迟），这期间心跳 ACK 收不到 → 客户端判自己掉线并关连接 →
**正好把正在进行的发送打断**（2026-09-20 真机：买家问「考研资料」，草稿生成了却没发出去）。
回归用例：`test_xianyu_live_seam.py`（含"慢处理期间心跳照常被应答、连接不被自己关掉"）。

**② 一条消息只能答一次，两层防线各管一种重放。**

| 场景 | 谁拦 |
|---|---|
| 同一进程内整帧重放 | 收帧层内存去重（`ws_duplicate`） |
| **进程重启后重放**（内存去重已清空） | 引擎的 **SQLite 消息级幂等**（`seen_frames` 里 `msg:<message_id>`，事件 `duplicate_message_skipped`） |

真发送下这层必须有：内存去重挡不住重启，平台重连补推会把同一条消息再送一次，
没有它就会**给买家回两条**（上线前库里已查到 5 组重复 message_id 的草稿）。

### 发送失败怎么救

发送失败会记 **`send_failed`**（critical 告警）+ 草稿保留未发送，并**回滚出站记账**
（否则 300 秒重复文本窗口会把重发拦掉 → "发失败 → 重发被自己挡住 → 买家永远收不到"）。重发：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/xianyu/drafts/40/resend
```

`scripts/xianyu_last_send_check.py` 一次看三处证据（草稿 sent 标记 / 事件 / 平台侧回捞）。

## 五、长跑体检（验证「7×24」站不站得住）

影子/自动跑起来之后，用长跑体检盯稳定性（**只读本地服务与进程，不连平台**）：

```powershell
.\.venv\Scripts\python.exe .\scripts\xianyu_soak.py --minutes 10                # 前台 10 分钟
.\.venv\Scripts\python.exe .\scripts\xianyu_soak.py --minutes 480 --interval 60 # 过夜 8 小时
```

每个采样点打印：连接状态、连接次数、收帧、心跳（发/应答）、重连、已发送、草稿数、进程 RSS；
结束时按规则给结论（**退出码 0=通过 / 2=要留意 / 1=有阻塞**）：

| 判定 | 含义 |
|---|---|
| 重连 > 0 且心跳超时 > 0 | **连接不稳**（blocker） |
| 影子模式下 `texts_sent > 0` | **严重异常**：影子模式就不该发送（blocker） |
| RSS 末值比首值增长 > 30% | 疑似内存泄漏（留意） |
| 一小时内 token 刷新多次 | 形态异常（当初「每分钟重连」的 bug 就是这种特征） |
| 解码/帧错误、`last_error` | 对应帧已丢弃/通道报错（留意） |
| 全程零重连、心跳全应答、零发送、RSS 平稳 | 通过 |

报告落盘 `.logs/xianyu_soak.json`（含全部原始采样）。

## 六、故障处理

| 症状 | 含义 | 处理 |
|---|---|---|
| `last_error: 凭据不可用/登录态失效` + 告警 `auth_error` | Cookie 过期 | 重新导出 Cookie 覆盖凭据文件，重启 |
| 告警 `session_invalid`（`token is not found`） | **静默僵尸连接**：平台不关连接，只是开始对每条请求回 401 | 见下方「七、会话被平台判定失效」；通道会**自动换 token 重连**，无需人工，但若反复出现说明有同账号第二处在抢会话 |
| 告警 `risk_control`（RGV587/被挤爆） | 触发风控 | 网页端点开消息过一次滑块 → 重新导出 Cookie，**暂停自动发送**观察 |
| 告警 `connection_lost` | 启用中却没连上 | 看 `last_error`；网络/凭据/token 三者之一 |
| 心跳「发 N 次应答 0」 | 连接被静默丢弃 | 同上；通道会自动退避重连 |
| 草稿为空字符串 | 推理模型没关思考 | 确认 `DISABLE_THINKING=true`（专家层已按模型名自动关） |
| 接口 400（专家层） | 服务商不支持 `enable_search` | `ENABLE_SEARCH=false` |
| 草稿里出现【未配置】 | 模型引用了模板占位 | 填 `data/卖家规则.md`；这是硬要求被违反，需复查提示词 |
| 事件里出现 `send_blocked` | 出站闸门拦下了一条（限速/重复/静默时段/空回复） | 看 `detail.reason`：`rate_limited_minute` 说明聊得太多（可放宽 `XIANYU_MAX_PER_MINUTE`）；`duplicate_text` 说明在重复同一句（多半是检索到旧答案，检查上下文）；`quiet_hours` 属预期 |
| 控制台面板显示「未启用」 | 开关没开或没重启 | 改 `.env` 后必须重启服务 |

## 六之二、买家消息「发了我这边没动静」怎么查

按顺序三步，**都能在服务运行时做，不用重启、不会发送任何消息**：

```powershell
# 1) 平台上到底有没有这条消息？字段长什么样？（只读查询，复用当前连接）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/xianyu/fetch-messages `
  -ContentType 'application/json' -Body '{"cid":"<会话id>","limit":5}' | ConvertTo-Json -Depth 6

# 2) 把这几条**真实消息**按时序喂进值守链路（影子模式只落草稿，绝不发送）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/xianyu/fetch-messages `
  -ContentType 'application/json' -Body '{"cid":"<会话id>","limit":5,"replay":true}' | ConvertTo-Json -Depth 6
```

`sender_id`／`cid` 从控制台面板或 `/api/v1/xianyu/health` 的会话列表里取。

判读方法（**先分清是"没收到"还是"收到了但没回"**）：

| 观察 | 结论 |
|---|---|
| 第 1 步 `count>0`，通道却 `messages=0` | 消息在平台上，但**没被推送过来**：多半是当时连接已失效（看 `auth_errors`／`session_invalid`）或平台没重放历史。用第 2 步 `replay` 补回 |
| 第 1 步 `unparsed>0` | **解析没跟上字段变化**：看该条的 `text_source`（`none`=真没文本，`error`=解析异常） |
| 第 1 步 `is_plain_text=false`（`content_type=14`） | 这是平台/客户端注入的**提示卡**（如"恭喜新手卖家…"），发给它的 `senderUserId` 却是买家 id；**不是买家提问，重放会自动跳过**，不要去回复它 |
| 第 1 步 `count=0` 且 HTTP 409 | 平台拒绝了查询（会话失效/无连接），**不是"这个会话没消息"**——旧的空结果会把这两件事混为一谈 |

## 六之三、会话被平台判定失效（`token is not found`）

真机抓到的形状（**平台不关连接**，只在长连接上回一帧）：

```json
{"headers": {"dt": "j", "mid": "..."}, "code": 401,
 "body": {"reason": "token is not found", "code": "4000001", "scope": "reg"}}
```

这是 7×24 值守最危险的盲区：`connected=true`、`registered=true`、心跳照常应答，**实际一条消息都收不到**。
现在的处理：识别该帧 → 记 `auth_errors` → 发 `session_invalid` 告警 → 置 `force_token_refresh` → 关连接重连 →
**重连注册时换一张新 token**（回归测试 `test_xianyu_ws.py` 的 `scenario_session_invalid_recovery` 覆盖，
断言第二次注册带的是 TOKEN-2 而不是复用的 TOKEN-1）。

顺带两条纪律：

1. **查询请求的应答本身就可能是这条失效帧**——判定必须排在「请求/应答关联」之前，否则帧被交给调用方后
   `return`，客户端侧永远不会重连（这个顺序错误在开发中真实发生过一次）。
2. **平台报错必须抛出**，不能退化成空结果：`code=400/401` 的帧没有 body，照原样返回会让"连接失效"伪装成
   "会话里没有消息"。

若 `session_invalid` 反复出现，检查是否有**同账号的第二处连接**（另一个机器人实例／网页端开着消息页）在抢会话：
同账号多连接会互相把对方的 reg 会话挤掉。查法：

```powershell
Get-NetTCPConnection -State Established | Where-Object { $_.RemotePort -eq 443 } |
  Select-Object OwningProcess, RemoteAddress, RemotePort
```


## 六之四、消息「收到了但没进链路」的三种静默失败（都已修，回归测试覆盖）

这三种在面板上**看不出来**（`connected=true`、无异常日志），只有抓帧才能发现：

| 症状 | 真因 | 现在怎么处理 |
|---|---|---|
| 买家消息推到连接上，`dropped.not_chat` 涨，`messages` 不涨 | 平台换过负载嵌套（紧凑提醒形 / 完整消息形 / 历史接口形），解析器写死一种就静默丢 | `find_message_node()` 有界递归找「带文本的消息节点」，命中的形状记进 `ws_message` 事件的 `shape` 字段 |
| 买家连发两句，只有第一句被回答 | 平台把**多条消息塞进一个同步包且共用外层 mid**（实测 entries=13、7 条消息），拿外层 mid 当幂等键 → 第 2 条起全判「重复」丢弃 | 幂等键三级回退：负载 msgId → `会话:createdAt:正文` → `外层mid#包内序号`；`id_source` 记录用了哪一级 |
| AI 去回复「恭喜新手卖家…」这类平台通知 | 提示卡 `content.custom.type=14`，但 **senderUserId 就是买家自己**，光看发送者分不出 | 按内容类型跳过，记 `notice_skipped` 事件 |

判读入口（都在 `ws_message` / `ws_duplicate` 事件里）：

- `shape`：命中的负载路径（如 `1.10`）。平台再次改嵌套时，这里会先变。
- `id_source`：`payload_id`（最稳）/ `content`（稳定内容键）/ `outer_mid`（**兜底，说明负载里没有可用 id 和时间**）。
  出现大量 `outer_mid` 就要警惕上面第二种故障。

### 同步游标：掉线窗口的消息不再永久丢

`XIANYU_SYNC_PTS` 三档：

| 值 | 含义 | 适用 |
|---|---|---|
| `now` | 告诉平台「我已同步到现在」——**掉线期间的消息永远不会补推** | 上游老做法，不推荐长期值守用 |
| `last` | 从上次同步到的 `pts` 续（落盘 `data/xianyu_sync_pts.json`） | **默认推荐**：重连时平台补推断线窗口内的消息 |
| `zero` | 全量重放历史 | 只在排查「消息到底有没有到平台」时临时用 |

游标有安全阀：为 0、或比当前时间超前超过 1 小时（时钟/字段异常）时**退回 now**——
宁可这一轮不补推，也不拿可疑值去要历史。查当前用的是哪个 pts：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/xianyu/events?limit=50 |
  Select-Object -ExpandProperty events | Where-Object { $_.kind -eq 'ws_sync_cursor' }
```

## 六之五、★ 真机负载的键是**整数**（本项目最隐蔽的坑，改解析前必读）

平台下发的同步负载里，**外层消息表的键是整数**（msgpack 把 `"1"/"2"/…/"10"` 编成 `int`），
而内层扩展表（`10`）的键是字符串。也就是说真机的样子是：

```python
{1: {1: {1: "买家@goofish"}, 2: "会话@goofish", 3: "消息id.PNM", 5: 创建时间ms,
     6: {1: 101, 3: {4: 内容类型}}, 10: {"reminderContent": "你好", "senderUserId": "买家"}}}
```

后果（**全部是静默的**，面板与日志都看不出来）：

| 写法 | 真机结果 | 症状 |
|---|---|---|
| `"10" in frame["1"]` | 恒 False | 买家消息被判 `not_chat` **直接丢弃**（"发了没动静"） |
| `frame["1"]["10"]["reminderContent"]` | KeyError | 同上 |
| `parent.get("2")`（会话 id） | None | **`chat_id` 变成空字符串**：所有买家并成一个会话，上下文/接管/限速全乱，P4 真发送还会因拿不到会话而发不出 |
| `parent.get("3")`/`get("5")` | None | 幂等键退化成外层 mid（同包多条互相当成重复）、消息过期判断失效 |

**统一用 `mget(表, "键名")` / `has_key(...)` 取值**（`ws.py` 顶部，自动兼容 str / int / bytes 三种键）。

排查同类问题的两条纪律：

1. **离线夹具必须与真机一致**。早期夹具用 JSON 反序列化的字符串键，于是自检全绿、线上全错。
   `scripts/test_xianyu_ws.py` 的 `scenario_int_keys_real_wire_shape` 会拿同一份负载分别按
   字符串键 / 整数键各解析一次并断言结果相同——**别再删掉这个用例**。
2. **诊断输出用 `repr` 而不是 `str`**：`str(b"3")` 显示成 `b'3'`、`str(3)` 显示成 `3`、
   带不可见字符的键也长得像正常键。事件里的 `id_debug` 字段就是为此加的（只在幂等键走兜底时才出现）。

诊断工具：

```powershell
# 把抓到的任意真机帧喂回**真实收帧路径**，直接看判定与幂等键来源
.\.venv\Scripts\python.exe scripts\xianyu_replay_frame.py --mid "04c50006 0"
# 同一负载按「字符串键 / 整数键」各解析一次，确认两者一致
.\.venv\Scripts\python.exe scripts\xianyu_key_type_check.py --mid "04c50006 0"
```

## 七、回滚与降级

```powershell
# 关掉闲鱼通道（AI-Robot 其余能力不受影响）
# .env 改 XIANYU_ENABLED=false 后：
.\stop-local.ps1 ; .\start-local.ps1 -NoBrowser
```

- 通道状态库 `data/xianyu.db` 可保留（重启不丢；不需要就删）；
- 想让旧 bot 继续主用：把它的窗口重新启起来即可（本通道默认关闭，不动它）；
- 代码层面的改动可整体回滚：`git diff --stat` 查看，`git checkout -- <file>` 恢复单个文件。

## 八、隐私与凭据纪律

- Cookie = 账号登录态：只存在 `secrets/xianyu_credentials.json`（0600/仅本用户、已 gitignore），**不进仓库、不贴聊天**；
- 买家对话落 `data/xianyu.db`（已 gitignore）：含会话内容，外发前先脱敏；
- 报文与报告脚本都做了脱敏：`xianyu_live_check.py` 会自检报告里是否混入 Cookie/token 明文（`secret_leaks` 必须为空）。
