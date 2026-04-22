# AI 工具情报抓取项目（文字版）精确实现文档

> 适用场景：使用 Codex / CLI + Python 实现一个 **只做文字情报** 的 AI 工具发现、筛选、归档与分发系统。  
> 不包含视频、TTS、画面生成、自动剪辑。  
> 本文档参考《我的 AI 实践：橘鸦 AI 早报》的三段式骨架——**信息采集 / 筛选处理 / 修饰分发**——但将“资讯事件流”改造成“工具实体流”。

---

# 1. 项目目标

本项目不是做“AI 新闻搬运”，而是做一个 **AI 工具情报系统**：

- 每天自动抓取新的 AI 工具、AI 产品、AI 开源项目、重要版本更新
- 对信息进行标准化、去重、AI 初筛、分类、评分
- 将同一个工具来自不同渠道的提及合并为一个“工具实体”
- 自动生成文字版情报卡片、日报、周报
- 将结果写入 Markdown / Notion / 数据库，并推送到 Telegram 等渠道

一句话概括：

> 从“杂乱的 AI 信息流”中，自动沉淀出“值得关注的 AI 工具清单”。

---

# 2. 项目边界

## 2.1 本项目要做的

本项目只做以下几类文字结果：

1. **工具主卡片**：单个工具的结构化记录
2. **每日情报短报**：当天最值得看的 3~10 个工具
3. **每周精选**：一周内高分工具整理
4. **知识库归档**：Notion / 本地数据库 / Markdown 文档

## 2.2 本项目明确不做的

第一版不做：

- 视频生成
- TTS 语音合成
- HTML 卡片截图
- ffmpeg 合成
- 口播稿
- 自动发 B 站 / 抖音 / 小红书
- 复杂多模态内容生成

原因：

> 当前最重要的是先做出一个能稳定运行的“文字情报引擎”，而不是内容包装系统。

---

# 3. 参考文章中真正值得保留的核心思想

参考文章里最有价值的，不是“AI 早报”这个表面形态，而是以下 6 个工程原则：

## 3.1 用 RSS / RSSHub 优先，不先上爬虫

适用于本项目的原因：

- RSS / Atom 数据天然结构化
- 抓取成本低
- 维护成本比爬虫低
- 更适合日常定时运行
- 更适合 Codex 先做 MVP

因此本项目的原则是：

> **优先使用 RSS / Atom / 官方 API；只有确实拿不到数据时，才考虑补充爬虫。**

## 3.2 信息源要广，但输出要收敛

参考文章靠多个来源保证信息广度；你这里也一样，但最终输出不能是“很多条零碎消息”，而必须收敛成“工具实体”。

也就是说：

- 输入：很多来源、很多碎片
- 输出：少量高价值工具卡片

## 3.3 AI 不直接代替全部流程

参考文章里，AI 先做初筛、摘要、关键词、旧闻排除、打分，再由人工核验和整合。你的项目也应保留这个思想：

- AI 负责高频、重复、结构化任务
- 人工负责核验与纠错

## 3.4 先做结构化中间结果，再做最终成品

参考文章不是采集后立刻出成品，而是先经过分类、摘要、打分、上下文整合。你这里更应该这么做：

- 原始条目
- 标准化条目
- 去重聚合后工具实体
- 工具卡片
- 日报 / 周报

## 3.5 旧闻排除非常重要

参考文章明确做了“与上一期和 72 小时内内容比对”的旧闻排除机制。你的项目里要变成：

- 同链接去重
- 同 repo 去重
- 同官网去重
- 同工具名高相似去重
- 最近 72 小时内相似更新去重

## 3.6 最终仍然要有人工核验点

尤其是下面这些情况：

- 工具名称相似但并非同一产品
- AI 分类错了
- AI 对价值判断失真
- 信息源不够官方

所以本项目必须保留一个：

> **人工复核队列**

---

# 4. 项目整体架构

推荐采用：

**Codex + Python 主项目 + Dify（可选）+ SQLite/PostgreSQL + Notion/Telegram**

## 4.1 各组件职责

### Codex

用于：

- 生成项目骨架
- 编写抓取器、数据库模型、处理管线
- 修复报错
- 迭代模块
- 写 README 和测试

### Python 主项目

用于：

- 定时抓取
- 解析 RSS / Atom / API 返回
- 去重
- 入库
- 调用 AI 工作流
- 输出日报
- 推送消息

### Dify 或其他 LLM API

用于：

- AI 初筛
- 摘要生成
- 分类
- 评分
- 行动建议

### 数据库存储

建议第一版先用 SQLite，后续可以升级 PostgreSQL。

### Notion / Telegram

用于：

- Notion：长期归档和检索
- Telegram：即时提醒和日报推送

---

# 5. 信息源设计

本项目的目标不是“抓所有 AI 新闻”，而是“抓最有价值的 AI 工具相关信息”。

## 5.1 一级信息源（第一版就做）

### A. GitHub

抓取对象：

- 新发布 repo
- 高增长 repo
- agent / ai / llm / automation / productivity / education / search / rag 等主题 repo
- 重要 release / update / README 更新

用途：

- 验证工具是否真实存在
- 获取开源工具的一手信息

### B. Product Hunt

抓取对象：

- 当天新发布产品
- AI / productivity / developer tools / automation 类别

用途：

- 获取产品化工具信息
- 辅助判断热度和市场关注度

### C. RSS / Atom 博客源

抓取对象：

- 官方博客
- AI 工具作者博客
- model / tooling newsletter
- 独立开发者更新日志

用途：

- 获取版本更新和官方说明

### D. X 重点账号流（可选但很重要）

抓取对象：

- OpenAI / Anthropic / Google DeepMind / Perplexity / Mistral / Hugging Face 等
- 独立开发者 / Agent 工具作者
- AI 工具发现型账号

用途：

- 抢早期发现信号
- 弥补 Product Hunt / GitHub 更新滞后

## 5.2 二级信息源（第二阶段再做）

- Hacker News
- Reddit（如 LocalLLaMA、OpenAI、AI Tools 相关板块）
- Linux.do 指定板块
- 官方文档更新页

## 5.3 手动补录源

参考文章里有 Obsidian Web Clipper 的人工补录环节。你的项目也建议保留一个轻量手动入口：

- 浏览器剪藏为 Markdown
- 手动把 URL / 简介写入 inbox
- 后端自动进入统一处理流

这能解决：

- RSS 没抓到
- API 不稳定
- 临时看到的重要工具

---

# 6. 数据流设计

整个项目应该采用“分层数据流”，而不是抓完直接出结果。

## 6.1 第 1 层：原始采集层 raw_items

作用：

把所有抓到的内容原样落下来，先不做判断。

建议字段：

- `id`
- `source_id`
- `source_name`
- `source_type`
- `external_id`
- `title`
- `author`
- `link`
- `published_at`
- `fetched_at`
- `raw_summary`
- `raw_content`
- `raw_payload`
- `guid`
- `content_hash`
- `status`

## 6.2 第 2 层：标准化层 normalized_items

作用：

把不同来源统一成同一结构。

建议字段：

- `id`
- `raw_item_id`
- `title`
- `body_text`
- `url`
- `author`
- `published_at`
- `source_type`
- `candidate_tool_name`
- `language`
- `tags`
- `dedupe_key`

## 6.3 第 3 层：AI 分析层 analyzed_items

作用：

让 AI 判断这条信息是否值得保留，并生成结构化分析结果。

建议字段：

- `normalized_item_id`
- `keep`
- `drop_reason`
- `summary_cn`
- `tool_type`
- `scenarios`
- `key_features`
- `novelty_score`
- `heat_score`
- `business_score`
- `actionability_score`
- `total_score`
- `recommendation_level`
- `why_it_matters`
- `try_now`
- `risk`

## 6.4 第 4 层：工具实体层 canonical_tools

作用：

把同一工具来自不同来源的多条提及合并为一个工具实体。

建议字段：

- `id`
- `name`
- `canonical_url`
- `homepage_url`
- `github_url`
- `producthunt_url`
- `summary_cn`
- `tool_type`
- `scenarios`
- `key_features`
- `novelty_score`
- `heat_score`
- `business_score`
- `actionability_score`
- `total_score`
- `recommendation_level`
- `why_it_matters`
- `try_now`
- `risk`
- `first_seen_at`
- `last_seen_at`

## 6.5 第 5 层：映射层 tool_mentions

作用：

保留“这个工具是从哪些来源来的”。

建议字段：

- `id`
- `tool_id`
- `analyzed_item_id`
- `mention_url`
- `source_name`
- `published_at`
- `matched_by`

---

# 7. 三段式核心流程（文字版）

这是整套系统最重要的结构。

## 7.1 第一段：信息采集

### 目标

尽可能低成本、稳定地把工具相关信息抓进系统。

### 处理步骤

1. 定时任务触发
2. 遍历信息源注册表
3. 根据源类型调用不同 collector
4. 抓到的内容写入 `raw_items`
5. 生成抓取日志

### 采集策略

- RSS / Atom：优先
- 官方 API：优先级同样很高
- RSSHub：用于补齐无原生 RSS 的站点
- 手动补录：兜底

### 不建议

- 第一版就写复杂浏览器爬虫
- 第一版就做全网关键词爬取

---

## 7.2 第二段：筛选和处理

这是项目的核心。

### 步骤 1：标准化

把 `raw_items` 清洗成 `normalized_items`。

清洗内容包括：

- HTML 去标签
- 提取正文摘要
- 统一时间格式
- 统一链接格式
- 识别来源类型
- 尝试从标题/正文中抽取候选工具名

### 步骤 2：原始去重

先做严格去重，防止重复抓取同一条内容。

规则建议：

- 相同 `guid`
- 相同 `external_id`
- 相同 `link`
- 相同 `content_hash`

### 步骤 3：AI 初筛

AI 要先判断：

- 是否与 AI 工具/产品/项目相关
- 是否有“工具情报价值”
- 是否只是教程、体验贴、泛观点、无实质信息内容

输出：

- `keep = true/false`
- `drop_reason`

### 步骤 4：摘要

保留项生成简洁中文摘要。

要求：

- 50~80 字
- 说明它是什么
- 说明主要能力
- 尽量避免空泛形容词

### 步骤 5：关键词 / 分类

参考文章里是“归类到具体事件”，你的项目要改成“归类到工具类型与使用场景”。

#### 工具类型建议

- 模型
- Agent
- 自动化
- 开发工具
- 图像/视频
- 搜索/知识库
- 效率工具
- 教育工具
- 营销工具
- 数据工具

#### 使用场景建议

- 内容生产
- 教育教学
- 跨境贸易
- 内部自动化
- 研发提效
- 客服销售
- 知识管理

### 步骤 6：旧闻排除

参考文章用了“上一期 + 72 小时内比对”。你的项目建议保留这个思想：

#### 旧闻判定规则

- 最近 72 小时内是否已出现同一官网
- 最近 72 小时内是否已出现同一 GitHub repo
- 最近 72 小时内是否已出现同一 Product Hunt 页面
- 最近 72 小时内是否已有高相似标题摘要

如果只是重复提及，不再新建工具卡片，而是作为 mention 附加到已有工具实体。

### 步骤 7：智能打分

建议分 4 个维度：

#### 新颖性 novelty

- 是否为新发布 / 新版本
- 是否有新能力
- 是否与旧工具区别明显

#### 热度 heat

- GitHub star 增长
- Product Hunt upvote
- 社区提及度
- 是否多个信源同时出现

#### 业务相关性 business

重点评估是否适合：

- 教育项目
- 贸易项目
- 自动化项目
- 内容项目

#### 可落地性 actionability

- 是否有官网 / demo / repo
- 是否现在可用
- 是否是纯概念
- 是否有上手门槛说明

最后合成：

- `total_score`
- `recommendation_level`（high / medium / low）

### 步骤 8：工具实体聚合

这是和普通 AI 日报最大不同的地方。

不是“每条消息一条输出”，而是：

> 同一个工具的多条信息，合并成一个工具实体。

#### 聚合顺序建议

1. GitHub repo URL 一致
2. Product Hunt URL 一致
3. 官网主域名一致
4. 工具名高相似
5. 内容互相交叉引用同一 URL

聚合后更新：

- `last_seen_at`
- `heat_score`
- `source_mentions`

### 步骤 9：人工复核队列

需要人工看的项目：

- 分数很高但信息很少
- 工具名冲突
- AI 判断不稳定
- 重要但不够官方

---

## 7.3 第三段：修饰与分发（只保留文字版）

这里明确删除视频相关逻辑，只做文字版。

### 步骤 1：生成工具卡片

每个高价值工具生成一张 Markdown 卡片。

建议模板：

```md
## 工具名
- 类型：
- 来源：
- 官网：
- GitHub：
- Product Hunt：
- 一句话摘要：
- 主要能力：
- 适用场景：
- 推荐等级：
- 为什么值得看：
- 现在怎么试：
- 风险点：
```

### 步骤 2：生成日报

从当天新增或更新的工具里选出：

- 总分最高的 3~10 个
- 每个工具给 3~6 行文字

日报建议包括：

- 今日新增工具总数
- 今日高优工具数
- Top 3 工具
- 每个工具一句行动建议

### 步骤 3：生成周报（后续）

每周汇总：

- 本周新增高分工具
- 本周值得试用工具
- 本周开源工具
- 本周教育/自动化/贸易方向重点工具

### 步骤 4：输出到 Markdown

参考文章里，文字版在分发前是 Markdown 文件。你的项目第一版也建议以 Markdown 为主输出格式。

原因：

- 易于版本管理
- 易于 Git 存档
- 易于后续转 Notion / 飞书 / 公众号
- 与 Codex 和本地工程工作流高度兼容

### 步骤 5：输出到 Notion

将工具实体同步到 Notion 数据库。

Notion 字段建议：

- Name
- Type
- Score
- Recommendation
- Summary
- Scenarios
- Homepage
- GitHub
- Product Hunt
- First Seen
- Last Seen
- Pushed?

### 步骤 6：输出到 Telegram

只推送高分项目，例如：

- `total_score >= 80`
- 或 `recommendation_level = high`

推送文本尽量短：

- 名称
- 一句话简介
- 推荐理由
- 链接

---

# 8. 推荐项目目录结构

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
│  │  ├─ producthunt_collector.py
│  │  └─ manual_ingest.py
│  ├─ parsers/
│  │  ├─ feed_parser.py
│  │  ├─ html_cleaner.py
│  │  └─ title_extractor.py
│  ├─ pipeline/
│  │  ├─ normalize.py
│  │  ├─ dedupe.py
│  │  ├─ analyze.py
│  │  ├─ entity_merge.py
│  │  ├─ digest.py
│  │  └─ review_queue.py
│  ├─ ai/
│  │  ├─ dify_client.py
│  │  ├─ prompts.py
│  │  └─ schemas.py
│  ├─ storage/
│  │  ├─ db.py
│  │  ├─ models.py
│  │  ├─ repository.py
│  │  └─ notion_writer.py
│  ├─ notify/
│  │  ├─ telegram.py
│  │  └─ markdown_exporter.py
│  ├─ jobs/
│  │  ├─ fetch_job.py
│  │  ├─ analyze_job.py
│  │  └─ publish_job.py
│  └─ main.py
├─ scripts/
│  ├─ init_db.py
│  ├─ backfill_feeds.py
│  ├─ run_fetch_once.py
│  ├─ run_analyze_once.py
│  └─ run_publish_once.py
├─ tests/
├─ .env.example
├─ pyproject.toml
└─ README.md
```

---

# 9. 任务调度设计

建议拆成 3 个独立 job。

## 9.1 fetch_job

职责：

- 拉取启用的信息源
- 保存 raw_items
- 不做复杂分析

## 9.2 analyze_job

职责：

- 处理未分析条目
- 标准化
- 去重
- 调 AI
- 聚合工具实体

## 9.3 publish_job

职责：

- 生成 Markdown 卡片
- 生成日报
- 写 Notion
- 推 Telegram

这样拆分的优点：

- 抓取失败不影响分析
- AI 服务故障时能重跑 analyze
- 推送失败时能补跑 publish

---

# 10. AI 提示词职责设计

不要让一个 Prompt 做所有事，建议拆成 3 类。

## 10.1 初筛 Prompt

目标：判断是否保留。

输入：

- title
- body_text
- url
- source_name

输出：

- keep
- drop_reason

判定标准：

- 是否为 AI 工具 / AI 产品 / AI 项目 / 更新
- 是否有具体产品信息
- 是否具有情报价值
- 是否是纯教程、闲聊、观点

## 10.2 摘要分类 Prompt

目标：生成结构化工具情报。

输出：

- summary_cn
- tool_type
- scenarios
- key_features
- candidate_tool_name

## 10.3 评分建议 Prompt

目标：判断值得不值得现在看。

输出：

- novelty_score
- heat_score
- business_score
- actionability_score
- total_score
- recommendation_level
- why_it_matters
- try_now
- risk

---

# 11. 人工介入点设计

参考文章强调“不是纯全自动”，你的项目也应该如此。

建议保留 3 个人工介入点：

## 11.1 重要条目复核

适用场景：

- 总分很高
- 信息源不够官方
- AI 判断疑似偏差

## 11.2 聚合冲突处理

适用场景：

- 两个工具被错误合并
- 同名但不同产品
- 官网域名冲突

## 11.3 日报最终确认

适用场景：

- 今天推送哪些条目
- 是否人工补一句说明
- 是否有明显遗漏

---

# 12. 第一版 MVP 实现范围

不要一开始就做全量系统。

第一版只要求打通：

**GitHub / RSS / Product Hunt → raw_items → normalize → AI analyze → canonical_tool → Markdown / Telegram**

只要做到这条链路，就已经是一个完整的 MVP。

## MVP 完成标准

- 每天能自动跑 1 次
- 每次能抓到至少 10 条候选内容
- 能自动筛掉无关内容
- 能输出结构化工具卡片
- 能输出当天日报 Markdown
- 能推送高分工具到 Telegram

---

# 13. 第二版升级方向

在第一版稳定后，再考虑：

## 13.1 扩展更多信息源

- X 账号流
- Reddit
- Hacker News
- Linux.do

## 13.2 增加更多输出端

- Notion 自动归档
- 飞书机器人
- 邮件日报
- 周报自动生成

## 13.3 增加人工工作台

- review queue 页面
- 聚合冲突手动修正
- 评分重写

## 13.4 增加分析维度

- 价格模式
- 开源/闭源
- 是否支持 API
- 是否支持中国用户访问
- 是否适合教育 / 贸易 / 自动化场景

---

# 14. 你如何用 Codex 推进这个项目

最好的方式不是一句话让它“全写完”，而是按任务模块逐个推进。

## Task 01
初始化项目骨架

## Task 02
实现 RSS / Atom 抓取器

## Task 03
实现 GitHub / Product Hunt collector

## Task 04
建立数据库模型和初始化脚本

## Task 05
实现 normalize 和原始去重

## Task 06
实现 AI 分析客户端和 schema 校验

## Task 07
实现工具实体聚合逻辑

## Task 08
实现 Markdown 卡片与日报输出

## Task 09
实现 Telegram 推送

## Task 10
补充 README、测试、定时运行脚本

---

# 15. 最终结论

如果只做文字版，那么参考文章中最值得借鉴的结构可以压缩成一句话：

> **用 RSS / API 稳定采集，用 AI 做初筛、摘要、分类、旧闻排除和打分，再将多来源信息聚合成工具实体，最终只输出高价值的文字情报。**

你这个项目和普通 AI 日报最大的区别在于：

- 普通 AI 日报：输出的是“事件”
- 你的项目：输出的是“工具”

所以它的核心不是“写稿”，而是：

1. 抓对源
2. 做好去重
3. 做好工具实体聚合
4. 控制 AI 漂移
5. 让最终输出结构化、稳定、可复用

这五件事才是项目成败关键。

