# AGENTS.md

本文件只规定 AI 编码代理在本仓库中的工作要求。项目背景、当前实现、运行方式和后续路线维护在 `README.md` 与 `docs/` 中。

## 1. 基本要求

1. 修改前先读取相关代码、文档和测试，确认当前运行入口与数据流。
2. 在现有目录、模型、Repository、Job、CLI 和测试约定上增量实现，避免无必要的大重构。
3. 每次只处理一个明确任务，保持改动聚焦；不要覆盖或回退用户已有的未提交修改。
4. 敏感配置必须来自环境变量或 `.env`，禁止硬编码 token、key、cookie 或私有 URL。
5. 外部接口的字段和行为必须以实际代码、fixture 或官方文档为依据，不得臆造。
6. 不主动提交、推送、拉取、重置 Git，除非用户明确要求。

## 1.1 搜索工具分工

1. Codex 交互式任务中的实时外部搜索，默认使用 Codex 原生搜索；不要为了主搜索自动调用 `smart-search`。
2. 只有用户明确指定 `smart-search`，或任务需要其 CLI 的结构化 JSON / Markdown、provider 诊断、可复现证据文件或独立流水线时，才调用 `smart-search`。
3. `smart-search` 的 `main_search` 通过模型生成回答，不能默认视为真实联网搜索；只有响应包含可验证 URL / citation 或经过独立来源检索时，才能作为搜索证据使用。
4. `gptpro.live`、本地 relay 和 Grok 等 OpenAI-compatible 模型接口默认只作为 AI 分析 / 改写 provider；未验证其搜索工具和引用能力前，不得把模型文本当作主搜索证据。
5. 数据抓取、定时任务和 Web UI 不得依赖 Codex 原生搜索；应使用 `app/collectors/`、`app/search/`、`app/evidence/` 和现有 Job 链路，并将可追溯来源写入数据库。
6. 本项目不安装或启用 Codex 的 `smart-search-cli` skill；除非用户明确要求恢复，不得执行 `smart-search setup --install-skills codex` 或 `smart-search skills update --targets codex`。`smart-search` CLI 属于系统外部工具，不属于本仓库，只有用户明确指定时才手动调用。

## 2. 两块职责边界

本项目由两个相互协作但职责分离的部分组成。新增功能应明确属于哪一块。

### 2.1 数据抓取与情报处理

数据侧负责从来源生产可追溯、可复跑的情报数据：

- `app/config/`：环境配置和 source registry。
- `app/collectors/`、`app/parsers/`：RSS、Atom、RSSHub、GitHub 等来源的采集与解析。
- `app/pipeline/`：标准化、去重、预筛、证据和核实等纯业务逻辑，尽量不直接写数据库。
- `app/ai/`、`app/search/`、`app/evidence/`：AI 客户端、搜索、证据抓取与分类。
- `app/storage/`：数据库模型、数据库连接、写入 Repository 和 UI 只读查询边界。
- `app/jobs/`、`scripts/`：阶段编排、数据库写入和可重复执行的 CLI 封装。
- `app/github/`：GitHub 项目情报的抽取、增强、排序和报告数据处理。

数据任务可以被 CLI、定时任务或手动调用，但不得依赖网页请求才能运行。新增阶段优先接入现有链路：

```text
fetch -> normalize -> prefilter -> ai-review -> claim-extract
-> evidence-search -> evidence-fetch -> evidence-classify
-> claim-verify -> ai-verify -> entity-resolve
-> recommendation-write -> export
```

### 2.2 网页 UI

网页侧负责展示、检索、审核和观察，不复制数据侧业务逻辑：

- `app/web/`：FastAPI 应用、路由、模板、静态资源和 Web 依赖注入。
- `app/storage/read_repository.py`：为 UI 提供稳定的只读查询和 DTO；复杂 SQL 不应堆在 route 中。
- `app/github/report_reader.py`：为 GitHub 热点页面提供报告读取能力；报告生成仍属于数据侧。

UI route 默认只读取数据库或已生成报告，不得在请求过程中直接运行 collector、pipeline、AI、搜索或 evidence job。
需要修改数据时，应通过明确的服务或 job 边界实现，并保持可审计；不要在模板或 route 中临时写业务规则。

## 3. 数据与可追溯性

1. 原始条目、标准化条目、候选、AI 结果、claim、evidence、verification、entity、recommendation 尽量保留关联关系。
2. 不能只保留最终推荐卡片而丢失来源、证据或判断过程。
3. 外部请求必须有超时、适度重试和错误记录；单条失败不能中断整个批次。
4. 可重复运行的阶段使用幂等 insert、update 或 upsert，避免重复行和重复副作用。
5. 上游 claim、evidence 或 verification 变化后，下游结果必须能标记 `stale` 或通过 `force` 重算。

## 4. AI 与证据规则

1. AI 响应必须按结构化 schema 解析、校验和记录原始响应；自由文本不能直接入库为事实。
2. AI 分数不能绕过本地 deterministic guard。
3. X、Reddit、RSSHub 和社区讨论只能作为线索，不能单独支撑高可信推荐。
4. 推荐卡片优先引用 direct support evidence；entity-only support 不得写成确定事实。
5. 强反证、失效链接、broken repo、social-only、claim contradiction 等风险必须能够阻止强推荐。

## 5. 测试与验证

新增或修改核心逻辑时补对应测试，至少覆盖改动涉及的边界：

- parser、normalize、dedupe、source 失败隔离和幂等写入。
- evidence classify、claim verify、support strength 和 deterministic guard。
- recommendation write、export、stale / force 重算。
- `read_repository` 和新增 Web route 的空状态、正常数据和过滤条件。

按改动范围运行最小必要验证；条件允许时运行全量测试：

```bash
python -m pytest
```

## 6. 限制事项

除非用户明确要求，不得：

- 引入视频、TTS、截图、ffmpeg 或浏览器自动化依赖。
- 引入 React/Vue 等重型前端框架、复杂构建链、微服务、消息队列或复杂 Docker 编排。
- 静默吞异常，例如 `except Exception: pass`。
- 把外部搜索结果或 AI 输出未经核实直接发布为事实。

完成任务后说明修改文件、实现原因、运行方式、验证结果和已知限制。
