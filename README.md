# AI 工具情报抓取（文字版）

本仓库用于构建面向 AI 工具发现、筛选、聚合、归档与文字分发的工程化情报系统。

当前阶段目标：实现 RSS/Atom/RSSHub 信息源抓取，解析后幂等写入 `raw_items`。

## 当前实现范围

- 读取 `app/config/source_registry.yaml` 中启用的信息源。
- 支持原生 RSS / Atom，以及通过 `RSSHUB_BASE_URL` 启用的 RSSHub 路由。
- 使用 `feedparser` 解析标题、链接、作者、发布时间、摘要、正文与原始 payload。
- 使用 SQLite + SQLAlchemy 保存 `sources` 与 `raw_items`。
- 对 `source_id + external_id`、`source_id + link`、`content_hash` 做幂等去重。
- 单个 source 抓取失败只记录失败，不中断其他 source。
- 将 `raw_items` 标准化为 `normalized_items`。
- 对标准化 URL / 标题生成 `dedupe_key`，避免同一条内容因追踪参数重复进入后续流程。
- 按规则预筛生成 `candidate_items` 候选池，先过滤低信号闲聊，再进入后续 AI 初筛。
- 将 `candidate_items` 中保留的候选导出为 Markdown / JSONL，便于 AI 初筛前人工审阅。
- 已预留通用 AI 初筛 API 客户端框架，调用地址和 key 通过环境变量配置。

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

初始化数据库：

```bash
python scripts/init_db.py
```

抓取单个源：

```bash
python scripts/run_fetch_once.py --source openai_news --limit-per-source 5
```

抓取所有启用源：

```bash
python scripts/run_fetch_once.py
```

按来源组抓取：

```bash
python scripts/run_fetch_once.py --group linux_do
python scripts/run_fetch_once.py --group reddit_local_llama
python scripts/run_fetch_once.py --group x
```

如不传 `--limit-per-source`，会使用每个 source 在 registry 中配置的 `default_limit`。

手动覆盖每源条数：

```bash
python scripts/run_fetch_once.py --group reddit_local_llama --limit-per-source 10
```

CLI 会输出每个 source 的：

- `fetched`
- `inserted`
- `skipped`
- `failed`

重复执行同一个 source 时，已入库条目会显示为 `skipped`。

标准化待处理 `raw_items`：

```bash
python scripts/run_normalize_once.py --limit 100
```

标准化会：

- 清理 HTML 标签与多余空白
- 规范化 URL 的 scheme / host
- 移除 `utm_*`、`ref`、`gclid`、`fbclid` 等常见追踪参数
- 生成 `dedupe_key`
- 将成功标准化的原始条目标记为 `normalized`
- 将同一标准化内容的重复条目标记为 `duplicate`

规则预筛生成候选池：

```bash
python scripts/run_prefilter_once.py --limit 100
```

预筛会根据以下信号生成 `candidate_items`：

- 目标保留：AI 工具、agent 工作流、MCP、skill、OpenAI-compatible API、2API、反代/中转/API gateway、模型部署/调用工具。
- 目标保留：明确的新模型、新开源权重、新产品或新能力发布。
- GitHub / Hugging Face / Product Hunt 等外链会从原始 HTML 中识别，但单独出现不再自动视为强保留信号。
- 明确丢弃：泛 benchmark、纯模型横评、硬件功耗 / VRAM / 吞吐调优、观点讨论、问题求推荐、个人部署踩坑、社区公告、抽奖、治理帖。
- 噪声关键词、个人体验类和低分内容会标记为 `dropped`。

导出 AI 初筛前人工审阅文件：

```bash
python scripts/run_review_export_once.py --limit 50
```

会在 `output/` 下生成两份文件：

- `review_candidates_YYYYMMDD_HHMMSS.md`：适合人工快速阅读和标注
- `review_candidates_YYYYMMDD_HHMMSS.jsonl`：适合后续接入 AI 初筛 API 或批处理

也可以通过 Typer CLI 执行：

```bash
python -m app.main review-export --limit 50
```

调用 AI API 对 `kept` 候选做初筛：

```bash
python scripts/run_ai_review_once.py --limit 5
```

或：

```bash
python -m app.main ai-review --limit 5
```

AI 初筛结果会写入 `ai_review_items` 表；重复运行不会重复审同一个 `candidate_item_id`。

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

## 数据表

当前阶段创建四张表：

- `sources`：来源配置与 `last_fetched_at`
- `raw_items`：原始抓取条目、原始 payload、内容 hash 与处理状态
- `normalized_items`：标准化后的标题、正文、URL、语言与 `dedupe_key`
- `candidate_items`：规则预筛后的候选池，保存分数、命中关键词、保留/丢弃理由
- `ai_review_items`：AI 初筛结果，保存 AI 是否保留、评分、分类、原因、中文摘要和原始响应

默认数据库路径：`data/ai_tool_intel.db`。
