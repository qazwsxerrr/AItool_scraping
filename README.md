# AI 工具情报抓取（文字版）

本仓库用于构建面向 AI 工具发现、筛选、聚合、归档与文字分发的工程化情报系统。

当前阶段目标：实现“抓取 → 标准化 → 规则预筛 → AI 二次筛选 → 人工审阅导出”的最小闭环。

## 当前实现范围

- 读取 `app/config/source_registry.yaml` 中启用的信息源。
- 支持原生 RSS / Atom，以及通过 `RSSHUB_BASE_URL` 启用的 RSSHub 路由。
- 使用 `feedparser` 解析标题、链接、作者、发布时间、摘要、正文与原始 payload。
- 使用 SQLite + SQLAlchemy 保存 `sources` 与 `raw_items`。
- 对 `source_id + external_id`、`source_id + link`、`content_hash` 做幂等去重。
- 单个 source 抓取失败只记录失败，不中断其他 source。
- 抓取层内置有限重试；当 `httpx` 遇到 timeout / 403 / 429 时会尝试 `curl` fallback。
- 将 `raw_items` 标准化为 `normalized_items`。
- 对标准化 URL / 标题生成 `dedupe_key`，避免同一条内容因追踪参数重复进入后续流程。
- 按规则预筛生成 `candidate_items` 候选池，先过滤低信号闲聊，再进入后续 AI 初筛。
- 将 `candidate_items` 中保留的候选导出为 Markdown / JSONL，便于 AI 初筛前人工审阅。
- 已支持通用 JSON API 与 OpenAI-compatible Chat Completions 风格 AI 初筛，调用地址、key 和模型通过环境变量配置。

暂不包含 canonical tool 聚合、Notion、Telegram、Markdown 日报、HTML 爬虫。

## 当前默认信息源

| source_id | 来源 | Feed |
|---|---|---|
| `openai_news` | OpenAI News | `https://openai.com/news/rss.xml` |
| `google_deepmind_blog` | Google DeepMind Blog | `https://deepmind.google/blog/rss.xml` |
| `huggingface_blog` | Hugging Face Blog | `https://huggingface.co/blog/feed.xml` |
| `producthunt_feed` | Product Hunt | `https://www.producthunt.com/feed` |
| `linux_do_top` | LINUX DO Top 话题 | `https://linux.do/top.rss` |
| `linux_do_hot` | LINUX DO Hot 话题 | `https://linux.do/hot.rss` |
| `reddit_local_llama_new` | Reddit r/LocalLLaMA new | `https://www.reddit.com/r/LocalLLaMA/new/.rss` |
| `reddit_local_llama_hot` | Reddit r/LocalLLaMA hot | `https://www.reddit.com/r/LocalLLaMA/hot/.rss` |
| `reddit_local_llama_top_day` | Reddit r/LocalLLaMA top day | `https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day` |
| `reddit_local_llama_top_week` | Reddit r/LocalLLaMA top week | `https://www.reddit.com/r/LocalLLaMA/top/.rss?t=week` |
| `reddit_local_llama_search_agent` | Reddit r/LocalLLaMA search | `agent` |
| `reddit_local_llama_search_open_weights` | Reddit r/LocalLLaMA search | `open weights` |
| `reddit_local_llama_search_gguf` | Reddit r/LocalLLaMA search | `gguf` |
| `reddit_local_llama_search_benchmark` | Reddit r/LocalLLaMA search | `benchmark` |
| `reddit_local_llama_search_mcp` | Reddit r/LocalLLaMA search | `mcp` |
| `reddit_local_llama_search_workflow` | Reddit r/LocalLLaMA search | `workflow` |
| `reddit_local_llama_search_2api` | Reddit r/LocalLLaMA search | `2api / openai-compatible / proxy` |
| `reddit_local_llama_search_claude_code` | Reddit r/LocalLLaMA search | `claude code workflow` |
| `reddit_local_llama_search_comfyui` | Reddit r/LocalLLaMA search | `comfyui workflow` |
| `reddit_local_llama_search_n8n_dify` | Reddit r/LocalLLaMA search | `n8n / dify` |

默认条数说明：

- `linux_do_top`：默认抓取 30 条。
- `linux_do_hot`：默认抓取 30 条。
- Reddit `new/hot/top/search` 源各自有独立默认条数，详见 `app/config/source_registry.yaml`。

## X / RSSHub 来源

X 账号流和搜索流已按 RSSHub 模板加入 `source_registry.yaml`。未配置 `RSSHUB_BASE_URL` 时会自动跳过，不影响 LINUX DO / Reddit 抓取。

示例：

```env
RSSHUB_BASE_URL=https://rsshub.example.com
```

当前预置：

- `x_account_openai`
- `x_account_huggingface`
- `x_account_local_llama`
- `x_search_github_launch`
- `x_search_huggingface_model`

## 安装

推荐 Python 3.11+。

### Windows conda（当前项目目录本地环境）

本项目已按 Windows conda 方式在项目目录创建本地环境：`.conda/`。

```cmd
conda activate D:\ai_code\ai_vibecode\AItool_scraping\.conda
python -m pytest
```

如需重建：

```cmd
conda create -y -p D:\ai_code\ai_vibecode\AItool_scraping\.conda python=3.12 pip
cd /d D:\ai_code\ai_vibecode\AItool_scraping
.conda\python.exe -m pip install -e ".[test]"
```

`.conda/` 为本地环境目录，已加入 `.gitignore`，不会上传到 GitHub。

### venv

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
```

如果当前环境没有可用 `pip` / `venv`，也可以使用 `uv`：

```bash
uv run --extra test pytest
```

## 配置

复制环境变量模板：

```bash
cp .env.example .env
```

最小配置：

```env
DATABASE_URL=sqlite:///./data/ai_tool_intel.db
```

可选：配置自己的 RSSHub 实例后，`source_registry.yaml` 中的 RSSHub 源才会启用：

```env
RSSHUB_BASE_URL=https://rsshub.example.com
```

未配置 `RSSHUB_BASE_URL` 时，RSSHub 源会被跳过并记录 warning，不影响原生 RSS / Atom 抓取。

> 如果你是在 WSL 中直接调用项目目录下的 Windows `.conda/python.exe`，临时
> `export DATABASE_URL=...` 或 `export RSSHUB_BASE_URL=...` 这类环境变量可能不会透传给
> Windows Python。此时请优先把配置写入 `.env` 后再运行脚本。

AI 初筛 API 框架配置项：

```env
AI_REVIEW_API_URL=https://your-ai-endpoint.example/review
AI_REVIEW_API_KEY=your-key
AI_REVIEW_MODEL=your-model-name
AI_REVIEW_API_STYLE=generic_json
AI_REVIEW_TIMEOUT_SECONDS=30
AI_REVIEW_MIN_CANDIDATE_SCORE=70
```

当前只搭建 API 调用框架，不会在人工审阅导出时默认调用 AI。预期接口接受候选 JSON，返回：

```json
{
  "keep": true,
  "score": 80,
  "category": "model_release",
  "reason": "why this should continue",
  "summary_cn": "中文摘要"
}
```

如果使用 DeepSeek / OpenAI-compatible Chat Completions，把 URL 写为 base URL，并指定：

```env
AI_REVIEW_API_URL=https://api.deepseek.com
AI_REVIEW_MODEL=deepseek-v4-flash
AI_REVIEW_API_STYLE=openai_chat
```

## 运行

以下命令默认在项目根目录执行：

```powershell
cd D:\ai_code\ai_vibecode\AItool_scraping
```

如果在 WSL 中执行，则路径通常是：

```bash
cd /mnt/d/ai_code/ai_vibecode/AItool_scraping
```

### 1. 初始化数据库

PowerShell / Windows：

```powershell
./.conda/python.exe scripts/init_db.py
```

WSL / Linux 原生 Python：

```bash
python scripts/init_db.py
```

### 2. 抓取信息源

抓取单个源：

```powershell
./.conda/python.exe scripts/run_fetch_once.py --source openai_news --limit-per-source 5
```

抓取所有启用源：

```powershell
./.conda/python.exe scripts/run_fetch_once.py
```

按来源组抓取：

```powershell
./.conda/python.exe scripts/run_fetch_once.py --group linux_do
./.conda/python.exe scripts/run_fetch_once.py --group reddit_local_llama
./.conda/python.exe scripts/run_fetch_once.py --group x
```

如不传 `--limit-per-source`，会使用每个 source 在 registry 中配置的 `default_limit`。

手动覆盖每源条数：

```powershell
./.conda/python.exe scripts/run_fetch_once.py --group reddit_local_llama --limit-per-source 10
```

CLI 会输出每个 source 的：

- `fetched`
- `inserted`
- `skipped`
- `failed`

重复执行同一个 source 时，已入库条目会显示为 `skipped`。

### 3. 标准化待处理 `raw_items`

```powershell
./.conda/python.exe scripts/run_normalize_once.py --limit 300
```

标准化会：

- 清理 HTML 标签与多余空白
- 规范化 URL 的 scheme / host
- 移除 `utm_*`、`ref`、`gclid`、`fbclid` 等常见追踪参数
- 生成 `dedupe_key`
- 将成功标准化的原始条目标记为 `normalized`
- 将同一标准化内容的重复条目标记为 `duplicate`

### 4. 规则预筛生成候选池

```powershell
./.conda/python.exe scripts/run_prefilter_once.py --limit 300
```

预筛会根据以下信号生成 `candidate_items`：

- 目标保留：AI 工具、agent 工作流、MCP、skill、OpenAI-compatible API、2API、反代/中转/API gateway、模型部署/调用工具。
- 目标保留：明确的新模型、新开源权重、新产品或新能力发布。
- GitHub / Hugging Face / Product Hunt 等外链会从原始 HTML 中识别，但单独出现不再自动视为强保留信号。
- 明确丢弃：泛 benchmark、纯模型横评、硬件功耗 / VRAM / 吞吐调优、观点讨论、问题求推荐、个人部署踩坑、社区公告、抽奖、治理帖。
- 噪声关键词、个人体验类和低分内容会标记为 `dropped`。

### 5. 调用 AI API 对 `kept` 候选做二次筛选

```powershell
./.conda/python.exe scripts/run_ai_review_once.py --limit 50
```

这里的 `--limit 50` 只是本次 AI 审阅的最大上限，不代表一定会审 50 条。AI 阶段会先筛选：

```text
status = kept
candidate_score >= AI_REVIEW_MIN_CANDIDATE_SCORE
尚未写入 ai_review_items
```

然后按以下优先级选择：

```text
candidate_score 降序 → published_at 降序 → candidate_id 升序
```

因此如果本轮只有 12 条达到最低分，即使传 `--limit 50` 也只会审 12 条，不会为了凑满 50 条把低质量候选送给 AI。

可以临时覆盖最低分：

```powershell
./.conda/python.exe scripts/run_ai_review_once.py --limit 50 --min-score 80
```

或：

```powershell
./.conda/python.exe -m app.main ai-review --limit 50 --min-score 80
```

AI 初筛结果会写入 `ai_review_items` 表；重复运行不会重复审同一个 `candidate_item_id`。

### 6. 抽取 claim、Tavily 搜索证据、AI 核实与推荐导出

AI 初筛之后可以进入情报核实层。该层不会只根据标题/摘要推荐，而是先抽取实体与 claim，再用 Tavily 搜索外部证据，最后让 AI 基于证据做多维评分。

需要在本地 `.env` 配置：

```env
TAVILY_BASE_URL=https://api.tavily.com
TAVILY_API_KEY=your-local-key
TAVILY_SEARCH_DEPTH=basic
TAVILY_MAX_RESULTS=5
```

`CLAIM_EXTRACT_*` 与 `AI_VERIFY_*` 默认可复用 `AI_REVIEW_*`；如果要使用不同模型或 endpoint，可以单独配置。

运行顺序：

```powershell
./.conda/python.exe scripts/run_claim_extract_once.py --limit 50
./.conda/python.exe scripts/run_evidence_search_once.py --limit 30
./.conda/python.exe scripts/run_ai_verify_once.py --limit 30
./.conda/python.exe scripts/run_recommendation_export_once.py --limit 20
```

或使用 Typer CLI：

```powershell
./.conda/python.exe -m app.main claim-extract --limit 50
./.conda/python.exe -m app.main evidence-search --limit 30
./.conda/python.exe -m app.main ai-verify --limit 30
./.conda/python.exe -m app.main recommendation-export --limit 20
```

新增表：

- `extracted_claims`：候选实体、类型、关键 claim、抽取出的官网/GitHub/Hugging Face/Product Hunt 链接。
- `evidence_items`：Tavily 搜索结果和直接证据 URL，包含证据类型、域名、置信度、原始 payload。
- `verification_items`：基于证据的最终保留判断、多维评分、推荐等级、风险标签和推荐理由。

最终推荐条件默认：

```text
final_keep = true
final_score >= 75
credibility_score >= 60
spam_risk_score <= 40
evidence_items >= 1
无 hard negative flag
```

`recommendation-export` 会在 `output/` 下生成：

- `recommendations_YYYYMMDD_HHMMSS.md`
- `recommendations_YYYYMMDD_HHMMSS.jsonl`

### 7. 导出人工审阅文件

```powershell
./.conda/python.exe scripts/run_review_export_once.py --limit 100
```

会在 `output/` 下生成两份文件：

- `review_candidates_YYYYMMDD_HHMMSS.md`：适合人工快速阅读和标注
- `review_candidates_YYYYMMDD_HHMMSS.jsonl`：适合后续接入 AI 初筛 API 或批处理

也可以通过 Typer CLI 执行：

```powershell
./.conda/python.exe -m app.main review-export --limit 100
```

### 8. 一键顺序执行完整流程

推荐先跑重点来源，避免某些国外源网络超时拖慢全量流程：

```powershell
./.conda/python.exe scripts/init_db.py

./.conda/python.exe scripts/run_fetch_once.py --group linux_do
./.conda/python.exe scripts/run_fetch_once.py --group reddit_local_llama

./.conda/python.exe scripts/run_normalize_once.py --limit 300
./.conda/python.exe scripts/run_prefilter_once.py --limit 300
./.conda/python.exe scripts/run_ai_review_once.py --limit 50
./.conda/python.exe scripts/run_claim_extract_once.py --limit 50
./.conda/python.exe scripts/run_evidence_search_once.py --limit 30
./.conda/python.exe scripts/run_ai_verify_once.py --limit 30
./.conda/python.exe scripts/run_recommendation_export_once.py --limit 20
./.conda/python.exe scripts/run_review_export_once.py --limit 100
```

如果要抓所有启用来源：

```powershell
./.conda/python.exe scripts/init_db.py

./.conda/python.exe scripts/run_fetch_once.py
./.conda/python.exe scripts/run_normalize_once.py --limit 500
./.conda/python.exe scripts/run_prefilter_once.py --limit 500
./.conda/python.exe scripts/run_ai_review_once.py --limit 80
./.conda/python.exe scripts/run_claim_extract_once.py --limit 80
./.conda/python.exe scripts/run_evidence_search_once.py --limit 50
./.conda/python.exe scripts/run_ai_verify_once.py --limit 50
./.conda/python.exe scripts/run_review_export_once.py --limit 150
```

## 网络、代理与超时处理

### RSSHub warning 是否正常

未配置 `RSSHUB_BASE_URL` 时，会看到类似 warning：

```text
Skipping source x_account_openai: missing env: RSSHUB_BASE_URL
```

这是正常行为，表示 X / Anthropic / GitHub Trending 等 RSSHub 源被跳过；不会影响 LINUX DO、Reddit、OpenAI、DeepMind、Product Hunt 等原生 RSS / Atom 源。

### 如果抓取一直 timeout

PowerShell 下先测试系统 `curl.exe` 是否能访问：

```powershell
curl.exe -L --max-time 20 https://linux.do/top.rss -o $env:TEMP\linux_top.rss
Get-Item $env:TEMP\linux_top.rss
```

再测试 Python/httpx：

```powershell
./.conda/python.exe -c "import httpx; r=httpx.get('https://linux.do/top.rss', timeout=20, follow_redirects=True); print(r.status_code, len(r.content))"
```

判断方式：

- `curl.exe` 成功、Python/httpx 超时：程序会自动尝试 `curl` fallback；也可以缩短超时加快 fallback。
- `curl.exe` 和 Python/httpx 都超时：通常是当前 PowerShell 网络/代理未配置，需要先配置代理。

常见 Clash / Mihomo HTTP 代理示例：

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"
```

如果你的代理端口不是 `7890`，请改成实际端口。

临时缩短抓取超时与重试次数：

```powershell
$env:REQUEST_TIMEOUT_SECONDS="10"
$env:REQUEST_RETRIES="1"
./.conda/python.exe scripts/run_fetch_once.py --group linux_do
```

## limit 参数怎么理解

不同阶段的 `limit` 含义不同：

| 阶段 | 参数 | 选择方式 |
|---|---|---|
| 抓取 | `--limit-per-source` | 每个 source 最多取多少条 feed item；不传则使用 `source_registry.yaml` 的 `default_limit`。 |
| 标准化 | `--limit` | 从待标准化 `raw_items` 中按入库顺序处理最多 N 条，成本低，目标是清空库存。 |
| 规则预筛 | `--limit` | 从未预筛 `normalized_items` 中按入库顺序处理最多 N 条，成本低，目标是生成候选池。 |
| AI 二次筛选 | `--limit` + `--min-score` | `limit` 是最大上限；只处理高于最低分的候选，并按分数/时间优先级排序，允许不足 N 条。 |
| 人工审阅导出 | `--limit` | 从候选池按分数降序导出最多 N 条，便于人工检查。 |

### 为什么重复运行显示 skipped

例如：

```text
linux_do_top: fetched=30 inserted=0 skipped=30 failed=0
```

表示实际抓到了 30 条，但这些条目已在数据库中，因此被幂等去重跳过。不是抓取失败。

## 测试

```bash
pytest
```

或：

```bash
uv run --extra test pytest
```

当前测试覆盖：

- RSS 样例解析
- Atom 样例解析
- RSSHub 环境变量插值与缺失跳过
- `raw_items` 幂等去重
- 单 source 抓取失败不影响其他 source
- 标准化清洗与 URL 去追踪参数
- `normalized_items` 幂等去重
- normalize job 重跑不重复入库
- source group 抓取与 source 默认条数
- 规则预筛与 `candidate_items` 幂等入库
- AI 初筛前人工审阅 Markdown / JSONL 导出
- AI 初筛 API 客户端配置、请求载荷和响应解析
- AI 初筛 job 幂等入库
- Tavily evidence search 请求载荷、Bearer 鉴权和响应解析
- claim 抽取、证据搜索、AI 核实与推荐导出 job 幂等入库
- final_score 多维公式、无证据降分和 hard negative 拦截

## 数据表

当前阶段创建以下表：

- `sources`：来源配置与 `last_fetched_at`
- `raw_items`：原始抓取条目、原始 payload、内容 hash 与处理状态
- `normalized_items`：标准化后的标题、正文、URL、语言与 `dedupe_key`
- `candidate_items`：规则预筛后的候选池，保存分数、命中关键词、保留/丢弃理由
- `ai_review_items`：AI 初筛结果，保存 AI 是否保留、评分、分类、原因、中文摘要和原始响应
- `extracted_claims`：AI 从候选中抽取出的实体、类型、claim 和关键外链
- `evidence_items`：Tavily 搜索结果与直接证据链接
- `verification_items`：基于证据的最终核实结果、评分、推荐等级和风险标签

默认数据库路径：`data/ai_tool_intel.db`。
