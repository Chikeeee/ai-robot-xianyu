# 本机部署记录（Windows / 中文环境）

> 部署对象：<https://github.com/liubaijiangde-bot/AI-Robot-FastAPI-LangChain-RAG-CrewAI-Agent->
> 部署日期：2026-09-20 ｜ 部署机：Windows + Python 3.13.9（`D:\dsh\ai-robot-agent`）
> 本文只记录**本机实测**结论；README 里已有的一般说明不再重复。

## 1. 本机采用的架构

| 层 | 本机配置 | 说明 |
|---|---|---|
| LLM | **DeepSeek 云端 API**（`deepseek-flash`） | 用本机已有的 key；实测该 key 可用模型只有 `deepseek-v4-pro` / `deepseek-flash`（`deepseek-chat` 不在列表内） |
| Embedding | **本地 Ollama `nomic-embed-text`**（768 维） | 离线、免费；DeepSeek 无 Embedding 接口 |
| 向量库 | `inmemory`（默认） | 重启即初始，启动自动导入 `data/knowledge_base.md` |
| 重排 | **关闭**（`AIROBOT_RERANK_ENABLED=false`） | 未装 `sentence-transformers`/torch |
| 多智能体 | 未装 CrewAI → 自动降级为内置 LangChain 路由 | `/api/v1/stats` 中 `crew_available=false` 属正常 |

访问入口：

- 控制台 <http://127.0.0.1:8000/dashboard>
- API 文档 <http://127.0.0.1:8000/docs>
- 健康检查 <http://127.0.0.1:8000/health>

## 2. 目录与进程

| 项 | 位置 |
|---|---|
| 项目 | `D:\dsh\ai-robot-agent` |
| 虚拟环境 | `D:\dsh\ai-robot-agent\.venv`（Python 3.13.9） |
| 环境变量 | `D:\dsh\ai-robot-agent\.env`（已被 .gitignore 忽略） |
| 服务日志 | `D:\dsh\ai-robot-agent\.logs\uvicorn.out.log` / `.err.log` |
| Ollama 程序 | `C:\Users\<你的用户名>\AppData\Local\Programs\Ollama\ollama.exe`（版本 0.34.2） |
| Ollama 模型 | `C:\Users\<你的用户名>\.ollama\models` |
| Ollama 日志 | `C:\Users\<你的用户名>\AppData\Local\Ollama\server.log` / `app.log` |

## 3. 启动与停止

**推荐用本机部署版脚本**（上游 `start.ps1` 不会拉起 Ollama，也不会带代理环境）：

```powershell
cd D:\dsh\ai-robot-agent
.\start-local.ps1                 # 起 Ollama(带代理) + 校验向量模型 + 起 uvicorn + 打开控制台
.\start-local.ps1 -NoBrowser      # 不自动开浏览器（远程/CI）
.\start-local.ps1 -SkipDeps       # 跳过 pip 依赖检查（快）
.\stop-local.ps1                  # 只停应用（8000）
.\stop-local.ps1 -IncludeOllama   # 连 Ollama（11434）一起停
```

`start-local.ps1` 做的事：设置代理环境 → 检查/创建 `.venv` → 幂等装依赖（阿里云镜像）→
Ollama 未运行则用**带代理的进程**启动并等 `/api/version` → 缺 `nomic-embed-text` 则自动拉取 →
uvicorn 未运行则启动并等 `/health` → 打印 LLM/向量模型/分块数/重排/CrewAI 状态 → 打开控制台。

手动方式（日志更直观）：

```powershell
cd D:\dsh\ai-robot-agent
$env:NO_PROXY='localhost,127.0.0.1,::1,api.deepseek.com'   # 见第 5 节，必须
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> 上游 `start.ps1` 仍可用（应用已跑、Ollama 已在线时它就是检查+启动）。
> 但它会用 `AIROBOT_LLM_MODEL` 去本地 Ollama 里找模型，本机 LLM 走 DeepSeek，
> 因此会提示 “缺少模型，请先拉取 deepseek-flash”。这是**提示不算错误**，可忽略。

> **开机自启**：Ollama 安装后**没有**写 HKCU Run 自启项（已确认），所以重启后需要手动跑
> `.\start-local.ps1`，或自行把 `start-local.ps1` 加进启动项/计划任务。

## 4. 依赖安装（本机实测的坑）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

- 清华镜像 `pypi.tuna.tsinghua.edu.cn` 本机下载 whl 报 **HTTP 403**，换阿里云镜像通过。
- 实际装到的是 **langchain 1.4.1 / langchain-openai 1.6.2 / fastapi 0.141.1**（比 README 写的 0.3.x 新）。
  已实测 `import app.main` 与全链路功能正常，无需降级。
- 可选能力（**本机未装**，装了才有完整多 Agent 与重排）：
  ```powershell
  .\.venv\Scripts\python.exe -m pip install -r requirements-extra.txt   # crewai[tools] + sentence-transformers(含 torch，体积大)
  .\.venv\Scripts\python.exe -m pip install -r requirements-eval.txt    # RAGAS 评测
  ```

## 5. 两个必须知道的网络坑（不修则整条 RAG 链路失效）

### 坑 1：Ollama 拉模型直连被阻断

现象：`ollama pull` 卡在 0 字节 / 16GB，`server.log` 反复报

```
Get "https://dd20bb891979d25aebc8bec07b2b3bbc.r2.cloudflarestorage.com/...": net/http: TLS handshake timeout
```

实测：`registry.ollama.ai` 直连 200，但真正的 blob 落在 **Cloudflare R2**，直连握手超时；
走代理 `http://127.0.0.1:7897` 才能下（本机代理必须处于运行状态）。

处理：给 **Ollama 进程**配代理（已设为用户级环境变量，重开终端/重启 Ollama 生效）：

```powershell
[Environment]::SetEnvironmentVariable('HTTP_PROXY','http://127.0.0.1:7897','User')
[Environment]::SetEnvironmentVariable('HTTPS_PROXY','http://127.0.0.1:7897','User')
[Environment]::SetEnvironmentVariable('NO_PROXY','localhost,127.0.0.1,::1,api.deepseek.com','User')
```

> 注意是给 **serve 进程**配，不是给 `ollama pull` 客户端配——下载是服务端做的。

### 坑 2：系统代理会把 localhost 也代理走 → 502

现象：应用启动后向量化全部失败，`openai.InternalServerError: Error code: 502`；

```
httpx.post('http://localhost:11434/v1/embeddings')  -> 502
httpx.Client(trust_env=False).post(同地址)            -> 200
curl http://localhost:11434/v1/embeddings             -> 200
```

原因：系统代理开关（`HKCU:\...\Internet Settings\ProxyEnable`）被打开后，
**httpx（openai / langchain-openai 的底层）会读取系统代理**，把 `localhost:11434` 也发给
Clash，Clash 返回 502；而 curl 不走系统代理所以正常。

处理：`.env` 中已显式声明（`python-dotenv` 会在建连前写入 `os.environ`）：

```ini
NO_PROXY=localhost,127.0.0.1,::1,api.deepseek.com
no_proxy=localhost,127.0.0.1,::1,api.deepseek.com
```

**结论：改系统代理开关后若出现 502，先查这里。**

## 6. 关键 `.env` 配置

```ini
AIROBOT_LLM_BASE_URL=https://api.deepseek.com/v1
AIROBOT_LLM_API_KEY=sk-****                       # 本机已有 key
AIROBOT_LLM_MODEL=deepseek-flash

AIROBOT_EMBEDDING_BASE_URL=http://localhost:11434/v1
AIROBOT_EMBEDDING_API_KEY=ollama
AIROBOT_EMBEDDING_MODEL=nomic-embed-text

AIROBOT_RERANK_ENABLED=false                      # 未装 sentence-transformers
NO_PROXY=localhost,127.0.0.1,::1,api.deepseek.com # 见 5.2
```

## 7. 验收记录（2026-09-20 实测）

| 用例 | 结果 |
|---|---|
| `GET /health` | 200 `{"status":"ok","llm_model":"deepseek-flash","embedding_model":"nomic-embed-text"}` |
| `GET /api/v1/stats` | `total_chunks=7`、`bm25_ready=true`、`hybrid_enabled=true`、`crew_available=false`（未装 CrewAI） |
| `POST /api/v1/chat` 知识问答「退货运费谁承担？」 | 200 / 3.3s，`intent=knowledge`，答案正确，`sources=[knowledge_base.md#4,#3,#5,#1,#2]` |
| 同会话追问「那买家自己原因的退货呢？」 | 200，多轮记忆生效 |
| `POST /api/v1/chat` 「帮我查一下订单 12345 到哪了」 | 200，`intent=order`，走 Mock 订单工具返回物流 |
| `POST /api/v1/chat` 「你好呀」 | 200，`intent=chat` |
| 语义缓存（同问题 + 新会话） | **0.04s 命中**（`cache_hit=true`，未命中时 3.3s） |
| `POST /api/v1/chat/stream` SSE | 200，118 个 token 事件，首字 3.4s，事件序列 stage→intent→token*→done |
| `GET /api/v1/traces` | 200，`summary`: p95=4116ms、avg=2194ms、cache_hit_rate=0.2 |
| `GET /dashboard` | 200，22464 字节单页 |

复跑验收脚本：

```powershell
cd D:\dsh\ai-robot-agent
$env:NO_PROXY='localhost,127.0.0.1,::1,api.deepseek.com'
.\.venv\Scripts\python.exe .\scripts\smoke_local.py
```

附带的本机脚本（**均为本次部署新增，不属于上游代码**）：

| 脚本 | 用途 |
|---|---|
| `scripts/smoke_local.py` | 上表 11 项验收，一次性输出 health/stats/对话/记忆/工具/缓存/SSE/traces/dashboard |
| `scripts/bench_thinking.py` | 对比 DeepSeek 开/关思维链的延迟与输出（用于第 11.4 条决策） |
| `scripts/upload_kb.py` | 批量上传知识文件（目录或文件清单），撞限流 429 会自动等待重试 |
| `scripts/test_ingest_local.py` | 上传路径自测（md/txt/docx/不支持后缀 + 检索验证），**会往知识库里塞测试文档**，跑完建议重启服务 |

## 8. 知识库上传（2026-09-20 实测）

> **知识库内容已换代（同日）**：原来的 `data/ai客服.md`（占位符模板）与上游示例库 `data/knowledge_base.md`
> 已移出 `data/`（备份在 `.logs/backup_*.md`，不会进知识库），换成：
> - `data/闲鱼值守规范.md` —— 行为准则（只依据资料作答、平台条款以页面为准、沟通合规红线、必须转人工的 6 种情形、标准话术）
> - `data/卖家规则.md` —— 卖家业务参数模板（议价底线/包邮/发货时效/保修/发票…），每项标 **【未配置】**，
>   规范里写明「【未配置】不是规则，AI 不得据此回答，必须引导人工」
>
> 所以现在重启后知识库是 **13 块**（原来 61 块）；往 `data/` 丢新文件仍会自动导入。
> 想恢复示例库：把 `.logs/backup_knowledge_base_demo.md` 拷回 `data/knowledge_base.md` 再重启。

**接口**：`POST /api/v1/ingest`，multipart 表单，字段名固定为 `file`；支持 `.pdf / .docx / .md / .txt / .markdown`。

```powershell
# 1) 单个文件（PowerShell 5.1 请用 curl.exe，不要用 Invoke-RestMethod 传文件/中文）
curl.exe -X POST http://127.0.0.1:8000/api/v1/ingest -F "file=@D:\docs\产品手册.pdf"

# 2) 批量（本机新增脚本，支持传目录）
cd D:\dsh\ai-robot-agent
.\.venv\Scripts\python.exe .\scripts\upload_kb.py D:\docs\知识库目录
```

返回：`{"file_name":"产品手册.pdf","chunks":12,"total_chunks":19}`

也可以走浏览器：<http://127.0.0.1:8000/docs> → `POST /api/v1/ingest` → Try it out → 选文件 → Execute。

**实测结果**

| 操作 | 结果 |
|---|---|
| 上传 `会员积分规则.md`（3 个标题段） | 200，`chunks=3`，总量 7→10，0.72s |
| 上传 `发票与开票.txt` | 200，`chunks=1`，总量 →11，0.11s |
| 上传 `配送范围说明.docx` | 200，`chunks=1`，总量 →12，0.28s |
| 上传 `不该支持.csv` | **400** `{"detail":"仅支持 pdf / docx / md / txt"}` |
| 提问「积分怎么获得？签到有奖励吗？」 | 答对（1 元 1 分 / 签到 5 分 / 连续 7 天 +50），2.55s |
| 提问「开票需要多久？」 | 答对「3 个工作日」，2.14s |
| 重启服务后问同一问题 | 分块回到 7，模型如实回答「资料里没有会员积分的信息」（测试内容已随重启清空） |

**分块规则**：`.md/.markdown` 先按 H1–H4 标题切分（标题保留在正文），单段超长再按 400 字/重叠 80 二次切；
其他格式直接 `RecursiveCharacterTextSplitter`（400/80，中文标点优先断句）。

**四个必须知道的点**

1. **重启会清空「HTTP 上传」的内容**：向量库是 `inmemory`，`POST /api/v1/ingest` 传进去的内容只活在进程内存里。
   但**放进 `data/` 的文件不会丢**——已打补丁让启动自动导入 `data/` 顶层全部知识文件（见第 9.1 节），
   所以「要长期保留的文档请放 `data/`」。要彻底持久化/多实例共享得切 Milvus
   （`.env` 设 `AIROBOT_VECTOR_STORE=milvus` + `docker-compose-milvus.yml`，需要 Docker，本机未装）。
2. **重复上传不去重**：同一文件再传一次会再加一遍（实测 11→12→13）；换文件名也一样，因为 `doc_id = 文件名#序号`。
   想要干净的知识库：重启服务（回到 7 块），或改用 Milvus 并关掉 `AIROBOT_MILVUS_RESET_ON_START`。
3. **来源标注显示临时文件名（上游 bug）**：上传文件的 `sources` 会是 `tmpd9wq_l47.md#2` 这种，
   而不是原始文件名——因为 `app/main.py` 把临时文件路径交给 `kb.ingest_file()`，后者用 `path.name` 当标题。
   一行即可修（把 `file.filename` 传进去），本机**未改源码**，需要就说。
4. **其他**：接口把整个文件读进内存（超大 PDF 注意内存）；扫描版/图片型 PDF 用 pypdf 抽不出文字；
   接口按 IP 限流 30 次/分钟，批量上传会撞 429（`upload_kb.py` 会自动等待重试）；
   上游 `scripts/ingest.py` 是**独立进程各自一份内存知识库**，对正在跑的服务无效，别用它给线上服务加料。

## 9. 本机对上游源码的三处改动（都可回滚）

### 9.1 `app/main.py`：启动自动导入 `data/` 全部知识文件（原本只导入 `knowledge_base.md`）

为了「文档丢进 `data/` → 重启即生效」，把 lifespan 里写死的单文件导入改成扫描 `data/` 顶层：

- 支持 `.md/.markdown/.txt/.pdf/.docx`（与 `POST /api/v1/ingest` 一致），不递归（避免吃进 `data/uploads/`）；
- 按文件名排序逐个导入，单个文件失败只记日志不影响启动；
- 来源标注就是文件名本身（`knowledge_base.md#3`、`ai客服.md#12`），比走 HTTP 上传更可追溯。

**实测**：`data/` 放 `knowledge_base.md`(7 块) + `ai客服.md`(54 块) 后重启，`total_chunks=61`，且来源显示为 `ai客服.md#N`。

### 9.2 `app/rag/retriever.py`：修复 BM25 索引不累加导致的「命中错块」（**重要**）

`KnowledgeBase.ingest_text` 原本这样写：

```python
self._bm25.add_documents([d.page_content for d in docs])   # 只传本次新增
```

而 `BM25Index.add_documents` 是**整体重建**语义（`self._corpus = texts` 覆盖 + 重算 BM25Okapi），
`search_bm25` 又拿 BM25 返回的下标去索引**全量** `self._documents`。于是**入库第二个文件后**：

1. BM25 只检索**最后一个入库文件**的内容；
2. 下标与 `_documents` 错位，命中结果被标注到完全不相干的块上。

本机硬证据（`scripts/check_bm25_alignment.py` 输出）：

```
_documents 块数 = 61
BM25 corpus 条数 = 7      <- 只有最后入库的 knowledge_base.md
[0] doc_id=ai客服.md#0        一致=False
      _documents : 下面是一份可直接放进 RAG 知识库的 **AI 客服常见问题文档模板**...
      bm25 corpus: # 平台帮助中心（示例知识库） ## 如何发布商品 ...
查询「退货运费谁承担？」BM25 原始 index = [4, 3]
  index 4 -> _documents[4] = ai客服.md#4 | 内容: ### 3. 怎么收费？...
            BM25 corpus[4] 实际内容: ## 退货运费由谁承担 1. 因商品质量问题...
```

修法（一行）：改成传全量文档，让 BM25 与 `_documents` 始终对齐：

```python
self._bm25.add_documents([d.page_content for d in self._documents])
```

代价：每次入库重建整份 BM25（分词 O(N)），知识库上千块时入库会略慢，检索不受影响。

**修复前后对比（`scripts/diagnose_retrieval.py`，同一套查询）**

| 查询 | 期望块 | 修复前是否进 prompt | 修复后 |
|---|---|---|---|
| 遇到投诉/辱骂/法律风险怎么处理 | `ai客服.md#1` | ✗（BM25 命中被错标） | ✓ |
| 通用的转人工话术是什么 | `ai客服.md#1` | ✗ | ✗（RRF 排到第 7 位，仍被挤出前 5） |
| 产品怎么收费/支持试用吗 | `ai客服.md#4` | ✗ | ✓（第 1 位） |
| 退货运费谁承担（示例库内容） | `knowledge_base.md#4` | ✗ | ✗（RRF 第 8 位） |

端到端（`scripts/verify_auto_import.py`）：「投诉/法律风险」从修前的「资料里没有相关指引」
变成修后正确回答「应立即转人工」+ 原样引用通用转人工话术，来源含 `ai客服.md#1`。

### 9.3 `app/services/chat.py`：意图路由加 few-shot 与 order 线索兜底（治误判）

现象（改造前实测）：规则类问题被判成 `order`，于是走订单工具返回 mock 订单——
问「怎么申请退款？」给出的是一串物流信息。原因：分类提示词只有一句「只输出 JSON」，没有判定边界与示例。

改动（都在 `classify_intent` 一条链路上，`/api/v1/chat` 与 `/api/v1/chat/stream` 共用，改一处两处都生效）：

1. **提示词加判定规则 + 8 条 few-shot 示例**（`INTENT_PROMPT`）：明确「问规则/政策/流程/时限/价格一律 knowledge，
   即使句子里有订单、退款、物流这些词」「只有明确查某一个具体订单才 order」「不确定优先 knowledge」。
2. **解析容错**（`parse_intent_json`）：用正则抽第一个 `{...}`，容忍 ` ```json ` 代码块与前后多余文字，
   降低「解析失败兜底成 chat」的概率。
3. **确定性兜底**（`has_order_cue`）：模型判 `order` 但句子里既没有 6 位以上数字、也没有
   「订单/单号/运单/物流/快递/包裹/发货/到货/签收…」等线索时，改判为 `knowledge`（知识库为空则 `chat`）。
   **单向**：只会把 order 改小，绝不会把别的意图升级成 order。

**A/B 评测**（`scripts/eval_intent_router.py`，同一条 DeepSeek 链路、temperature 0.3，16 个用例 × 每例 2 次 = 32 次判定）：

| 实现 | 准确率 | 判错用例 |
|---|---|---|
| 旧（复刻上游提示词） | **81.2%（26/32）** | 「怎么申请退款？」→order、「物流一直不动怎么办？」→order、「今天天气怎么样」→knowledge |
| 新（few-shot + 兜底） | **100%（32/32）** | 无 |

**兜底通路确定性验证**（`scripts/test_intent_guard.py`，把模型输出强制成 order，排除 LLM 随机性）：5/5 通过——
规则类问题被改判 knowledge，带单号/快递字样的真实订单查询保持 order。

**上线后端到端验证**（`scripts/verify_intent_e2e.py`，走真实 HTTP + SSE）：**7/7 正确**，
含「怎么申请退款？」「港澳台能配送吗？」这两个原本被判成 order 的问题，SSE 的 `intent` 事件也正确。

> 附带观测：重启后第一次请求出现过一次 `llm_ms=34.3s` 的尖峰（后续请求 1.5–2.1s），
> 发生在 RAG 生成阶段，判断是 DeepSeek 侧的单次抖动（另有 tenacity 重试兜底），与本次改动无关。

## 10. 检索质量现状与提升建议（诚实结论）

即使修掉 BM25 错位，当前仍有约一半查询取不到正确块。实测原因（不是猜）：

1. **向量路基本帮不上忙**：查询「通用的转人工话术是什么」时，正确块 `ai客服.md#1`
   **连向量 top-30 都没进**；`nomic-embed-text` 是英文为主的嵌入模型，中文短查询表现弱。
2. **文档结构放大问题**：这份 `ai客服.md` 是**模板**，54 块几乎同构（`标准回答/关键词/转人工条件`），
   向量空间里彼此高度相似，排序接近随机。
3. **RRF 双列表加成**：同时被向量与 BM25 命中的块得分翻倍，单列表命中的正确块容易被挤出前 5。
4. **未装重排**：跨编码器重排正是治「向量排序不准」的手段，本机没装（缺 torch）。

按性价比排序的建议：

| 方案 | 做法 | 代价 | 预期 |
|---|---|---|---|
| ① 换中文向量模型 | `ollama pull bge-m3`（或 bge-large-zh-v1.5），改 `.env` 的 `AIROBOT_EMBEDDING_MODEL` | ~1.2GB 下载（走代理可成） | 直接改善第 1 条，可量化验证 |
| ② 装重排 | `pip install -r requirements-extra.txt`，`AIROBOT_RERANK_ENABLED=true` | torch 约 2–3GB + 每次检索约 2s | 治第 3 条 |
| ③ 换真实内容 | 把 `{产品名}`、`{套餐A}` 等占位符替换成真实业务规则 | 人工 | 治第 2 条，收益最大 |
| ④ 清理干扰 | 不需要演示数据时删掉 `data/knowledge_base.md` | 无 | 减少跨文档抢位 |

## 11. 已知问题 / 待办

1. **Ollama 模型（本机已就绪）**：

   | 模型 | 大小 | 用途 |
   |---|---|---|
   | `nomic-embed-text:latest` | 274 MB | 应用正在用的向量模型（768 维） |
   | `qwen2.5:1.5b` | 986 MB | 全离线模式的本地 LLM 备选（把 `.env` 的 `AIROBOT_LLM_*` 指向 `http://localhost:11434/v1` 即可切换） |

   拉取过程曾长期卡在 R2 的 TLS 超时，最终靠「代理 + `OLLAMA_MAX_TRANSFER_STREAMS=1`」拉完；
   再拉新模型时若卡住，先确认 Ollama 进程带没带代理（用 `start-local.ps1` 启动即带）。
2. **16.16 GB 孤儿下载分片**：`C:\Users\<你的用户名>\.ollama\models\blobs\sha256-dfd98d2734212d0c...-partial*`
   是早前从 Ollama GUI 发起、被同一网络问题卡住的大模型残留，确认不用可直接删除回收磁盘。
3. **CrewAI 多 Agent / bge-reranker 重排未装** → 当前走内置 LangChain 路由、检索保留 RRF 融合顺序。
   要完整体验：装 `requirements-extra.txt` 并把 `AIROBOT_RERANK_ENABLED` 改回 `true`。
4. **推理模型延迟**：`deepseek-v4-pro/flash` 带思维链，实测首字 ~3.4s。
   实测关闭思考可降延迟（意图分类：flash 1.49s→1.16s，pro 2.92s→1.78s，返回内容与 JSON 均正常），
   如需压延迟可在 `app/rag/retriever.py::build_llm` 与 `app/services/chat.py::get_llm` 的
   `ChatOpenAI(...)` 上加 `extra_body={"thinking": {"type": "disabled"}}`（本机**未改源码**，保持与上游一致）。
5. **内存态组件**：向量库/会话记忆/语义缓存/限流均为进程内实现，重启即清空；
   生产化替换方案见 README「设计决策」表格（Redis / Chroma / Milvus / 网关限流）。
6. **限流按 IP、60 秒窗口、默认 30 次/分钟**：本机 `127.0.0.1` 单 IP 压测容易撞 429
   （`/api/v1/stats`、`/api/v1/traces` 已白名单豁免）。
