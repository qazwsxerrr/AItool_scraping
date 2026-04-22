# AGENTS.md

## 项目名称
AI 工具情报抓取（文字版）

## 1. 目标
本项目用于自动抓取、筛选、聚合、归档并分发 AI 工具情报，输出形式仅限文字内容，不生成视频、不生成 TTS、不做画面渲染。

目标产物：
- 结构化工具卡片（canonical tool cards）
- 每日 Markdown 日报
- Notion 情报库写入
- Telegram 高分工具推送
- 可选：GitHub Pages + rss.xml 作为输出分发

非目标：
- 视频脚本生成
- 口播稿
- TTS
- ffmpeg 合成
- Selenium 截图
- HTML 卡片视频画面

---

## 2. Codex 在本仓库中的工作方式
你是本仓库的编码代理。你的首要任务不是一次性“写完整项目”，而是按模块稳步推进一个可运行、可测试、可迭代的 Python 项目。

工作原则：
1. 先读仓库，再行动。
2. 优先最小可运行实现（MVP），不要过度设计。
3. 每次只做一个明确任务，完成后说明改了什么、如何验证。
4. 除非用户明确要求，否则不要引入重量级基础设施。
5. 所有新增逻辑必须可测试、可复跑、可定位错误。
6. 输出必须偏工程化，而不是只写“示例代码”。

如果仓库为空：
- 先搭建骨架。
- 再实现最小抓取链路：RSS/Atom -> raw_items -> normalize -> AI analyze -> digest output。

如果仓库已有代码：
- 优先复用现有目录与约定。
- 不要无故大改目录结构。

---

## 3. 项目范围与核心流程
本项目仅做“文字情报系统”，核心流程分为三段：

### A. 信息采集
输入源：
- 原生 RSS / Atom
- RSSHub 路由
- GitHub feeds / GitHub API
- Product Hunt GraphQL API
- 可选：X 账号流（通过 RSSHub 或其他可维护方式）
- 手动补录（后置功能）

### B. 筛选与处理
对原始条目执行：
- 标准化
- 一级去重（原始条目去重）
- AI 初筛（是否值得进入工具库）
- 摘要生成
- 工具分类
- 分项评分
- 工具实体聚合（canonical tool merge）
- 72 小时近重检查 / 近期重复检查

### C. 修饰与分发
输出：
- 结构化工具卡片
- Markdown 每日短报
- Notion 数据库写入
- Telegram 消息推送
- 可选：静态归档页与 rss.xml

---

## 4. 推荐技术栈
默认使用 Python。

推荐依赖方向：
- Python 3.11+
- `httpx`：HTTP 请求
- `feedparser`：RSS / Atom 解析
- `pydantic`：数据模型与 schema 校验
- `sqlalchemy`：数据库 ORM
- `sqlite`：本地默认数据库（MVP）
- `typer`：CLI
- `jinja2`：Markdown / RSS / HTML 模板输出（如需要）
- `python-dateutil`：时间处理
- `rapidfuzz`：相似度去重
- `tenacity`：重试
- `pytest`：测试

默认不要引入：
- Celery
- Redis
- Kafka
- Docker Compose 多服务编排
- Playwright / Selenium
- 前端框架

除非用户明确要求，MVP 阶段全部避免。

---

## 5. 建议目录结构
如果需要初始化项目，优先采用以下结构：

```text
ai-tool-intel/
├─ app/
│  ├─ config/
│  │  ├─ settings.py
│  │  └─ source_registry.yaml
│  ├─ collectors/
│  │  ├─ base.py
│  │  ├─ rss_collector.py
│  │  ├─ atom_collector.py
│  │  ├─ github_collector.py
│  │  └─ producthunt_collector.py
│  ├─ parsers/
│  │  ├─ feed_parser.py
│  │  └─ content_cleaner.py
│  ├─ pipeline/
│  │  ├─ normalize.py
│  │  ├─ dedupe.py
│  │  ├─ entity_merge.py
│  │  ├─ analyze.py
│  │  ├─ score.py
│  │  └─ digest.py
│  ├─ ai/
│  │  ├─ dify_client.py
│  │  ├─ prompts.py
│  │  └─ schemas.py
│  ├─ storage/
│  │  ├─ db.py
│  │  ├─ models.py
│  │  └─ repository.py
│  ├─ notify/
│  │  ├─ notion_writer.py
│  │  ├─ telegram.py
│  │  └─ formatter.py
│  ├─ jobs/
│  │  ├─ fetch_job.py
│  │  ├─ analyze_job.py
│  │  └─ publish_job.py
│  └─ main.py
├─ scripts/
│  ├─ init_db.py
│  ├─ run_fetch_once.py
│  ├─ run_analyze_once.py
│  └─ run_publish_once.py
├─ tests/
├─ output/
│  ├─ BACKUP/
│  ├─ rss.xml
│  └─ latest.md
├─ .env.example
├─ pyproject.toml
└─ README.md
```

如果仓库已有结构，尽量贴合现有结构，而不是强制迁移。

---

## 6. 数据模型要求
至少维护以下核心实体：

### sources
记录信息源配置。
关键字段：
- id
- name
- type
- url
- enabled
- priority
- fetch_interval
- parser_type
- last_fetched_at

### raw_items
记录原始抓取条目。
关键字段：
- id
- source_id
- external_id
- title
- link
- author
- published_at
- fetched_at
- raw_summary
- raw_content
- raw_payload
- content_hash
- status

### normalized_items
记录标准化后条目。
关键字段：
- id
- raw_item_id
- title
- body_text
- url
- author
- published_at
- language
- dedupe_key

### canonical_tools
记录工具主实体。
关键字段：
- id
- name
- canonical_url
- homepage_url
- github_url
- producthunt_url
- summary_cn
- tool_type
- scenarios
- total_score
- recommendation_level
- first_seen_at
- last_seen_at

### tool_mentions
记录来源条目与工具实体的映射。
关键字段：
- id
- tool_id
- normalized_item_id
- source_name
- mention_url
- matched_by

### digest_runs
记录每次日报与推送。
关键字段：
- id
- run_date
- total_items
- kept_items
- pushed_items
- status

---

## 7. 抓取规则
### 7.1 RSS / Atom
- 使用统一解析器抽取标题、链接、时间、作者、摘要、正文、guid。
- 必须保留原始 payload 便于复查。
- 解析失败时记录日志，不允许静默吞错。

### 7.2 GitHub
优先级：
1. 先支持公开 feed / Atom
2. 再按需要补 GitHub API

不要一开始就做复杂的多接口组合抓取。

### 7.3 Product Hunt
- 使用官方 GraphQL API
- 查询字段保持最小化
- 注意分页与限流
- 不要在 MVP 阶段过度丰富查询复杂度

### 7.4 X / RSSHub
- 作为补充信号源，而不是系统唯一主干
- 适合监控重点账号，不适合第一阶段做全量搜索

---

## 8. 去重与聚合规则
本项目必须实现两级去重。

### 一级去重：原始条目去重
优先依据：
- guid
- external_id
- link
- content_hash

### 二级去重：工具实体聚合
按以下顺序尝试：
1. GitHub repo URL 相同
2. Product Hunt URL 相同
3. 官网主域名相同
4. 工具名高相似度
5. 交叉链接关系一致

要求：
- 聚合结果必须可回溯
- 不允许只保留最终工具卡片而丢失来源条目
- 每个 canonical tool 必须保留 source mentions

### 近重检查
需要支持“72 小时近重”或等效的近期重复检查：
- 避免连续多天重复推送同一工具
- 若是重大版本更新，可重新进入高优先级流程，但必须记录原因

---

## 9. AI 分析规则
默认使用 Dify Workflow API 作为 AI 分析层。

分析拆成三个步骤：

### 9.1 初筛
判断：
- 是否 AI 相关
- 是否是工具 / 产品 / 项目 / 版本更新
- 是否有落地价值
- 是否应丢弃

### 9.2 摘要与分类
输出至少包括：
- name
- summary_cn
- tool_type
- scenarios
- target_users
- key_features

### 9.3 评分与建议
输出至少包括：
- novelty_score
- heat_score
- business_score
- actionability_score
- total_score
- recommendation_level
- why_it_matters
- try_now
- risk

### AI 输出约束
- 必须使用结构化 JSON schema 校验
- 不接受自由文本直接入库
- 失败时记录原始响应并重试 / 降级
- 不要把幻觉结果直接视为事实

---

## 10. 输出格式要求
本项目只输出文字信息。

### 10.1 工具卡片
统一字段：
- name
- canonical_url
- github_url
- producthunt_url
- homepage_url
- summary_cn
- tool_type
- scenarios
- target_users
- key_features
- total_score
- recommendation_level
- why_it_matters
- try_now
- risk
- source_mentions

### 10.2 Markdown 日报
建议包含：
- 日期
- 今日新增条目数
- 今日入库工具数
- 今日高优工具 3~10 条
- 每条工具的简述、链接、建议

### 10.3 Telegram 推送
仅推送高分工具，内容简洁：
- 名称
- 一句话摘要
- 推荐等级
- 为什么值得看
- 链接

### 10.4 RSS / GitHub Pages（可选）
当实现输出端分发时：
- 每天生成一篇 Markdown 存档
- 自动更新 `output/rss.xml`
- GitHub Pages 用于静态托管

---

## 11. 作业拆分要求
任务应拆成三个 job，而不是一个大脚本：

### fetch_job
负责：
- 拉取源
- 解析源
- 保存 raw_items

### analyze_job
负责：
- 标准化
- 去重
- 调 AI
- 聚合实体
- 更新 canonical_tools

### publish_job
负责：
- 生成日报
- 写 Notion
- 推送 Telegram
- 更新可选的 RSS 输出

要求：
- 每个 job 可单独执行
- 每个 job 可重复运行
- 每个 job 有清晰日志
- 失败后可重跑，不应破坏已有数据

---

## 12. 配置要求
敏感信息必须来自环境变量，不允许硬编码。

至少包括：
- DATABASE_URL
- DIFY_BASE_URL
- DIFY_API_KEY
- NOTION_API_KEY
- NOTION_DATABASE_ID
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- PRODUCTHUNT_TOKEN
- GITHUB_TOKEN（如需要）

需要提供：
- `.env.example`
- `settings.py`
- 配置缺失时的明确报错

---

## 13. 日志与错误处理
必须具备：
- 结构化日志或至少清晰文本日志
- 每个外部请求的异常日志
- 重试机制（适度，不无限）
- 超时设置
- 可区分抓取失败、解析失败、AI 失败、发布失败

不要：
- `except Exception: pass`
- 静默跳过关键错误
- 让整个批次因为单条数据失败而全部中断

---

## 14. 测试要求
新增功能时，优先补以下测试：
- RSS/Atom 解析测试
- 去重逻辑测试
- 实体聚合测试
- Dify 响应 schema 测试
- Markdown 日报生成测试

如果时间有限，至少保证：
1. 核心 parser 有测试
2. 核心 dedupe 有测试
3. 核心 schema 有测试

---

## 15. 代码风格要求
- 使用类型注解
- 函数保持小而清晰
- 避免巨型脚本
- 每个模块只做一类事
- 公共数据结构优先用 Pydantic / dataclass
- 复杂逻辑写 docstring 或简洁注释
- 不要滥用全局变量

命名要求：
- 变量名清晰，不用模糊缩写
- 与“工具实体”“来源条目”“日报”相关的名字要稳定一致

---

## 16. 提交一个任务时应满足的“完成定义”
当你完成一个任务时，输出必须包含：
1. 改了哪些文件
2. 为什么这样改
3. 如何运行
4. 如何验证
5. 已知限制

只有在以下条件满足时，才算真正完成：
- 代码可运行
- 关键逻辑可验证
- 没有明显破坏既有结构
- 用户可以继续在此基础上迭代

---

## 17. 禁止事项
除非用户明确要求，否则不要：
- 引入视频相关依赖
- 实现 TTS / 口播 / 截图 / ffmpeg
- 增加前端框架
- 把仓库重构成微服务
- 引入消息队列
- 使用需要复杂部署的组件
- 编造 API 字段或平台能力
- 在未验证时假设第三方接口返回结构固定

---

## 18. 优先级顺序
实现顺序严格遵守：
1. 项目骨架
2. 配置加载
3. 数据模型与数据库初始化
4. RSS / Atom 抓取
5. GitHub / Product Hunt 接入
6. 标准化与一级去重
7. Dify 分析
8. 工具实体聚合
9. Markdown 日报输出
10. Telegram / Notion 分发
11. 输出端 RSS / GitHub Pages

不要跳步。

---

## 19. 给 Codex 的执行偏好
当用户请求“继续实现”时，优先做当前阶段最小闭环，而不是新开分支功能。

偏好顺序：
- 先补缺口
- 再修 bug
- 再做增强
- 最后再美化

如果发现需求不明确：
- 给出最合理的默认实现
- 同时说明哪里是可配置点
- 不要因为小问题停在原地反复追问

---

## 20. 首轮任务建议
如果这是一个新仓库，请按以下顺序启动：

### Task 01
初始化 Python 项目骨架、`pyproject.toml`、`.env.example`、日志配置。

### Task 02
实现数据库模型与初始化脚本。

### Task 03
实现通用 RSS/Atom 抓取器与解析测试。

### Task 04
实现 raw_items 入库。

### Task 05
实现 normalize + 一级去重。

### Task 06
实现 Dify client 与 schema 校验。

### Task 07
实现 analyze_job。

### Task 08
实现 canonical tool 聚合。

### Task 09
实现 Markdown 日报。

### Task 10
实现 Telegram / Notion 分发。

---

## 21. 最终原则
这个项目的本质不是“做一个新闻号”，而是构建一个：

**面向 AI 工具发现、筛选、聚合、归档与文字分发的工程化情报系统。**

始终围绕这个目标行动。
