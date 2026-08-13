# AI 情报抓取与处理

本项目支持一条可重复执行的 AI-only 文字情报链路：

```text
source registry -> fetch/normalize -> AI relevance + classification + short summary -> export -> read-only UI
```

AI review 对每个候选条目执行一次结构化分析，输出 `keep`、`content_class`、中文摘要、理由、风险标记、可选官方链接候选和置信度。AI 结果是编辑分析输出，不是来源背书；系统不会再启动旧的 claim、entity、事件聚类或日报编辑阶段。

## 内容类别与来源归因

| `content_class` | 典型来源 | 处理方式 |
| --- | --- | --- |
| `official_model_company` | 官方模型、公司产品、API 和研究更新 | 按来源身份、时间窗口和关键词筛选，再交给 AI 分类和摘要 |
| `project_tool` | GitHub、Product Hunt、AI 工具项目 | 按项目指标和时间窗口筛选；GitHub 项目可生成一次项目摘要 |
| `community_social` | X、Reddit、RSSHub、论坛 | 作为社区线索参与 AI 分类；输出保留来源归因和风险标记 |

导出和 UI 保留 `source_id`、`source_name`、`source_group`、`source_subtype`、`source_role`、`transport`、`tier` 与 `x_official` 等来源字段。X 官方账号可通过 `source_group=x_official`、`source_role=official` 和 `x_official=true` 归因；这些字段只描述来源身份。

## 抓取来源

来源配置位于 `app/config/source_registry.yaml`。唯一的抓取路由字段是 `transport`：`feed`、`rsshub` 或 `github`；Feed 细节在 `feed` 下，GitHub 细节在 `github` 下。当前保留原生 RSS/Atom、RSSHub、GitHub Trending/Search/Releases 和 Product Hunt Atom 采集器。

本地 RSSHub 的 X 认证路径只有 `TWITTER_AUTH_TOKEN`。`scripts/start_rsshub.sh` 会保留该 token 和 `PROXY_URI`，并显式移除 OAuth 与第三方 X API 变量；脚本在默认 Node 不受支持时会优先尝试 NVM Node 24，再回退到 Node 22。

## 数据模型

新数据库只由当前 ORM metadata 初始化，旧数据库不提供迁移兼容。保留的五张核心表为：

- `sources`：来源配置、内容类别、来源归因和健康状态。
- `fetch_attempts`：请求状态、HTTP 信息、重试和错误记录。
- `intel_runs`：一次抓取、AI review 或 `run-once` 的运行汇总。
- `intel_items`：标准化条目、原始 payload、指标、选择状态和来源关联。
- `ai_item_reviews`：每条最多一条结构化 AI 分析结果和原始响应。

## 安装与配置

```bash
uv sync --extra test
# 或
python -m pip install -e ".[test]"
```

复制 `.env.example` 为 `.env`，至少配置：

```env
DATABASE_URL=sqlite:///./data/ai_tool_intel.db
```

常用配置：

```env
RSSHUB_BASE_URL=http://127.0.0.1:1200
RSSHUB_PORT=1200
TWITTER_AUTH_TOKEN=
PROXY_URI=http://127.0.0.1:2080
GITHUB_TOKEN=
AI_REVIEW_API_URL=
AI_REVIEW_API_KEY=
AI_REVIEW_MODEL=
AI_REVIEW_API_STYLE=generic_json
AI_REVIEW_TIMEOUT_SECONDS=30
```

真实 token、API key、Cookie 和代理地址只放在本地 `.env`，不要写入 README 或提交到 Git。Product Hunt 使用公开 Atom feed，不需要额外 token。

## CLI

保留的命令：

```bash
python -m app.main fetch [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main fetch-only [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main ai-review [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main export [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N]
python -m app.main run-once [--source SOURCE_ID] [--class CONTENT_CLASS] [--limit N] [--force]
python -m app.main source-health [--source SOURCE_ID]
```

`run-once` 固定执行 `fetch -> ai-review -> export`。`fetch-only` 只抓取并输出标准化条目及来源归因，不调用 AI。`ai-review` 输出：

- `ai_review_candidates.jsonl`：AI 选择且分析成功的候选。
- `ai_review_audit.jsonl`：过滤、拒绝和 AI 失败等审计记录。
- `ai_review_digest.md`：候选的中文摘要和来源信息。

`export` 默认写入 `output/intel/`：

- `intel_items.jsonl`：AI 选择结果与来源归因。
- `intel_pending.jsonl`：尚未分析或 AI 失败的条目。
- `intel_digest.md`：分类、状态、指标、风险和链接摘要。

GitHub 项目保留抓取到的 stars、forks、Trending 周期指标、topics 和 README 摘要；AI 不替代项目指标筛选，也不会生成未提供的增长数据。

本地 RSSHub 启动：

```bash
bash scripts/start_rsshub.sh
```

## Web UI

FastAPI/Jinja UI 只读取数据库和已生成报告，展示来源归因、AI 分类、摘要、风险和选择状态；请求过程中不会运行 collector、AI、搜索或其他处理任务。

## 验证

```bash
TMPDIR=/tmp python -m pytest -q
python -m compileall -q app scripts
python -m app.main --help
```
