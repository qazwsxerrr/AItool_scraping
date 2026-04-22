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

暂不包含 AI 分析、canonical tool 聚合、Notion、Telegram、Markdown 日报、HTML 爬虫。

## 当前默认信息源

| source_id | 来源 | Feed |
|---|---|---|
| `openai_news` | OpenAI News | `https://openai.com/news/rss.xml` |
| `google_deepmind_blog` | Google DeepMind Blog | `https://deepmind.google/blog/rss.xml` |
| `huggingface_blog` | Hugging Face Blog | `https://huggingface.co/blog/feed.xml` |
| `producthunt_feed` | Product Hunt | `https://www.producthunt.com/feed` |
| `linux_do_latest` | LINUX DO 最新话题 | `https://linux.do/latest.rss` |
| `linux_do_top` | LINUX DO Top 话题 | `https://linux.do/top.rss` |
| `linux_do_hot` | LINUX DO Hot 话题 | `https://linux.do/hot.rss` |

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
python scripts/run_fetch_once.py --limit-per-source 30
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

## 数据表

当前阶段创建三张表：

- `sources`：来源配置与 `last_fetched_at`
- `raw_items`：原始抓取条目、原始 payload、内容 hash 与处理状态
- `normalized_items`：标准化后的标题、正文、URL、语言与 `dedupe_key`

默认数据库路径：`data/ai_tool_intel.db`。
