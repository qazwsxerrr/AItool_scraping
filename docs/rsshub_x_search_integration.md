# RSSHub 接入 X 搜索内容实现文档

## 1. 目标

本实现的目标是：获取 X / Twitter 上 AI 相关内容，尤其是通过搜索语法发现 AI 工具、开源项目、模型发布、Agent 工作流、MCP 等内容，并接入当前 `AItool_scraping` 项目的抓取、入库、筛选流程。

整体思路不是直接在当前 Python 项目中编写 X 爬虫，而是使用 RSSHub 作为中间层：

```text
X / Twitter
  ↓
RSSHub Twitter keyword route
  ↓
RSS / Atom XML
  ↓
AItool_scraping
  ↓
feedparser 解析
  ↓
raw_items 入库
  ↓
normalize / prefilter / AI review
```

RSSHub 负责访问 X 并把搜索结果转换为 RSS；当前项目只需要把 RSSHub 生成的 RSS 地址当作普通 RSS 信息源处理。

---

## 2. 当前项目已有能力

当前项目已经具备 RSS / Atom / RSSHub 类型信息源配置能力。

当前 README 中已经说明项目支持：

- 读取 `app/config/source_registry.yaml` 中启用的信息源。
- 支持原生 RSS / Atom，以及通过 `RSSHUB_BASE_URL` 启用的 RSSHub 路由。
- 使用 `feedparser` 解析标题、链接、作者、发布时间、摘要、正文与原始 payload。
- 使用 SQLite + SQLAlchemy 保存 `sources` 与 `raw_items`。
- 对 `source_id + external_id`、`source_id + link`、`content_hash` 做幂等去重。
- 单个 source 抓取失败只记录失败，不中断其他 source。
- 抓取层内置有限重试；当 `httpx` 遇到 timeout / 403 / 429 时会尝试 `curl` fallback。

因此，X 内容接入的重点不是新增爬虫，而是：

```text
1. 部署 RSSHub
2. 配置 RSSHub 的 X 访问凭证
3. 在 source_registry.yaml 中配置 X 搜索 RSSHub 路由
4. 在 AItool_scraping 的 .env 中配置 RSSHUB_BASE_URL
5. 使用现有 fetch / normalize / prefilter 流程处理
```

---

## 3. 技术架构

### 3.1 RSSHub 侧

RSSHub 是一个 Node.js / TypeScript 项目，作用是把不提供 RSS 的网站内容转换为 RSS。

在本方案中使用 RSSHub 的 X / Twitter 搜索路由：

```text
/twitter/keyword/:keyword
```

例如：

```text
http://127.0.0.1:1200/twitter/keyword/AI%20agent
```

该路由会根据传入的关键词访问 X 搜索结果，并返回 RSS XML。

### 3.2 AItool_scraping 侧

当前项目侧不感知 X 的 API 细节，只处理 RSS：

```text
source_registry.yaml
  ↓
load_source_registry()
  ↓
HTTPFeedCollector.collect()
  ↓
parse_feed()
  ↓
RawItemRepository.insert_if_new()
  ↓
raw_items
```

因此，只要 RSSHub URL 能返回合法 RSS XML，当前项目即可抓取。

---

## 4. 准备工作

需要准备：

```text
1. Docker / Docker Compose
2. 一个可登录 X 的账号
3. X 登录态 cookie 中的 auth_token
4. 当前项目的 .env 配置
5. 可选：代理，例如 http://host.docker.internal:2080
```

注意：

```text
auth_token 属于敏感凭证，不要提交到 GitHub。
建议使用专门的小号，而不是主账号。
```

---

## 5. 部署 RSSHub

### 5.1 新建 RSSHub 运行目录

该目录可以独立于当前项目，例如：

```bash
mkdir rsshub-local
cd rsshub-local
```

### 5.2 创建 RSSHub 的 `.env`

在 `rsshub-local/.env` 中写入：

```env
TWITTER_AUTH_TOKEN=你的_x_auth_token
```

获取方式：

```text
浏览器登录 X
→ 打开开发者工具
→ Application / Storage
→ Cookies
→ 选择 https://x.com
→ 找到 auth_token
→ 复制 value
→ 写入 .env
```

### 5.3 创建 `docker-compose.yml`

在 `rsshub-local/docker-compose.yml` 中写入：

```yaml
services:
  rsshub:
    image: diygod/rsshub:latest
    container_name: rsshub
    restart: unless-stopped
    ports:
      - "1200:1200"
    env_file:
      - .env
    environment:
      NODE_ENV: production
      CACHE_TYPE: memory
      CACHE_EXPIRE: 600
      REQUEST_TIMEOUT: 30000
      REQUEST_RETRY: 2

      # 如果访问 X 需要代理，取消注释并按实际代理端口修改
      # PROXY_URI: http://host.docker.internal:2080
```

### 5.4 启动 RSSHub

```bash
docker compose up -d
```

查看容器：

```bash
docker ps
```

查看日志：

```bash
docker logs -f rsshub
```

启动成功后，本地 RSSHub 服务地址为：

```text
http://127.0.0.1:1200
```

---

## 6. 验证 RSSHub 是否能访问 X

### 6.1 测试简单关键词

```bash
curl "http://127.0.0.1:1200/twitter/keyword/AI%20agent"
```

正常情况应返回 RSS XML，例如包含：

```xml
<rss>
  <channel>
    <item>
      <title>...</title>
      <link>...</link>
      <description>...</description>
    </item>
  </channel>
</rss>
```

### 6.2 测试账号时间线

```bash
curl "http://127.0.0.1:1200/twitter/user/OpenAI"
```

### 6.3 测试复杂搜索

原始 X 搜索语法：

```text
("AI tool" OR agent OR MCP) (launch OR released OR github) -is:retweet -is:reply
```

URL encode 后：

```text
%28%22AI%20tool%22%20OR%20agent%20OR%20MCP%29%20%28launch%20OR%20released%20OR%20github%29%20-is%3Aretweet%20-is%3Areply
```

完整测试命令：

```bash
curl "http://127.0.0.1:1200/twitter/keyword/%28%22AI%20tool%22%20OR%20agent%20OR%20MCP%29%20%28launch%20OR%20released%20OR%20github%29%20-is%3Aretweet%20-is%3Areply"
```

如果这一步失败，优先排查 RSSHub、X 凭证、代理和网络，不要先排查当前 Python 项目。

---

## 7. 接入 AItool_scraping

### 7.1 配置项目 `.env`

在 `AItool_scraping/.env` 中添加：

```env
DATABASE_URL=sqlite:///./data/ai_tool_intel.db
RSSHUB_BASE_URL=http://127.0.0.1:1200
```

如果在 WSL 中运行 Python，而 RSSHub 运行在 Windows Docker 中，`127.0.0.1` 可能不通，可以尝试：

```env
RSSHUB_BASE_URL=http://host.docker.internal:1200
```

或使用 Windows 主机 IP。

---

## 8. 修改 `source_registry.yaml`

### 8.1 关键注意点

RSSHub 当前 X 搜索路由应使用：

```text
/twitter/keyword/
```

不要使用：

```text
/twitter/search/
```

因此，如果已有配置中出现：

```yaml
url: ${RSSHUB_BASE_URL}/twitter/search/...
```

应改为：

```yaml
url: ${RSSHUB_BASE_URL}/twitter/keyword/...
```

---

## 9. 推荐新增 X 搜索源

建议先配置四类搜索源：

```text
1. AI 工具 / 应用发布
2. GitHub 开源项目
3. Hugging Face 模型 / Space / 数据集
4. MCP / Agent 工作流
```

可加入 `app/config/source_registry.yaml`：

```yaml
  - id: x_search_ai_tool_launch
    name: X Search AI Tool Launch via RSSHub
    type: rsshub
    url: ${RSSHUB_BASE_URL}/twitter/keyword/%28%22AI%20tool%22%20OR%20%22AI%20app%22%20OR%20agent%20OR%20workflow%29%20%28launch%20OR%20released%20OR%20introducing%20OR%20%22open%20source%22%29%20-is%3Aretweet%20-is%3Areply
    enabled: true
    priority: 152
    fetch_interval: 14400
    parser_type: feedparser
    source_group: x
    source_subtype: search
    search_query: '("AI tool" OR "AI app" OR agent OR workflow) (launch OR released OR introducing OR "open source") -is:retweet -is:reply'
    default_limit: 20

  - id: x_search_github_ai_tool
    name: X Search GitHub AI Tool via RSSHub
    type: rsshub
    url: ${RSSHUB_BASE_URL}/twitter/keyword/url%3Agithub.com%20%28agent%20OR%20LLM%20OR%20MCP%20OR%20%22AI%20tool%22%20OR%20%22open%20source%22%29%20-is%3Aretweet%20-is%3Areply
    enabled: true
    priority: 153
    fetch_interval: 14400
    parser_type: feedparser
    source_group: x
    source_subtype: search
    search_query: 'url:github.com (agent OR LLM OR MCP OR "AI tool" OR "open source") -is:retweet -is:reply'
    default_limit: 20

  - id: x_search_huggingface_model
    name: X Search Hugging Face Model via RSSHub
    type: rsshub
    url: ${RSSHUB_BASE_URL}/twitter/keyword/url%3Ahuggingface.co%20%28model%20OR%20space%20OR%20dataset%20OR%20gguf%20OR%20weights%29%20-is%3Aretweet%20-is%3Areply
    enabled: true
    priority: 154
    fetch_interval: 14400
    parser_type: feedparser
    source_group: x
    source_subtype: search
    search_query: 'url:huggingface.co (model OR space OR dataset OR gguf OR weights) -is:retweet -is:reply'
    default_limit: 20

  - id: x_search_mcp_agent
    name: X Search MCP Agent via RSSHub
    type: rsshub
    url: ${RSSHUB_BASE_URL}/twitter/keyword/%28MCP%20OR%20%22model%20context%20protocol%22%20OR%20%22AI%20agent%22%20OR%20%22agent%20workflow%22%29%20%28github%20OR%20release%20OR%20launch%20OR%20tool%29%20-is%3Aretweet%20-is%3Areply
    enabled: true
    priority: 155
    fetch_interval: 14400
    parser_type: feedparser
    source_group: x
    source_subtype: search
    search_query: '(MCP OR "model context protocol" OR "AI agent" OR "agent workflow") (github OR release OR launch OR tool) -is:retweet -is:reply'
    default_limit: 20
```

---

## 10. 运行抓取

### 10.1 抓取 X 组信息源

在项目根目录运行：

```bash
python scripts/run_fetch_once.py --group x --limit-per-source 5
```

Windows conda 环境：

```powershell
./.conda/python.exe scripts/run_fetch_once.py --group x --limit-per-source 5
```

预期输出类似：

```text
Fetch stats:
  - x_search_ai_tool_launch: fetched=5 inserted=5 skipped=0 failed=0
  - x_search_github_ai_tool: fetched=5 inserted=4 skipped=1 failed=0
Totals: fetched=10 inserted=9 skipped=1 failed=0
```

### 10.2 后续标准化

```bash
python scripts/run_normalize_once.py --limit 300
```

### 10.3 规则预筛

```bash
python scripts/run_prefilter_once.py --limit 300
```

---

## 11. 调试顺序

必须按以下顺序排查：

```text
1. RSSHub 容器是否启动
2. RSSHub URL 是否能返回 RSS XML
3. RSSHUB_BASE_URL 是否在 AItool_scraping 的 .env 中生效
4. source_registry.yaml 中 X 搜索源是否使用 /twitter/keyword/
5. run_fetch_once.py --group x 是否能抓取
6. normalize / prefilter 是否正常处理
```

不要先从 Python 项目内部排查。只要 RSSHub URL 本身不返回 XML，当前项目一定无法抓取。

---

## 12. 常见问题

### 12.1 `Twitter API is not configured`

原因：

```text
RSSHub 没有读到 TWITTER_AUTH_TOKEN
```

检查：

```bash
docker exec -it rsshub printenv | grep TWITTER
```

确认是否存在：

```text
TWITTER_AUTH_TOKEN=...
```

### 12.2 返回 403 / 429

可能原因：

```text
1. X 限流
2. auth_token 失效
3. IP 被限制
4. 没有代理
```

处理方式：

```text
1. 更换 X 小号 token
2. 降低抓取频率
3. 配置 PROXY_URI
4. 增大 fetch_interval
```

### 12.3 RSSHub URL 可以访问，但项目抓不到

检查项目 `.env`：

```env
RSSHUB_BASE_URL=http://127.0.0.1:1200
```

如果在 WSL 中运行项目，尝试：

```env
RSSHUB_BASE_URL=http://host.docker.internal:1200
```

也可以直接用：

```bash
curl "$RSSHUB_BASE_URL/twitter/keyword/AI%20agent"
```

确认项目运行环境能访问 RSSHub。

### 12.4 配置了 X 源但被跳过

如果 `RSSHUB_BASE_URL` 未配置，项目会跳过 `${RSSHUB_BASE_URL}` 模板源。这是预期行为，不是错误。

---

## 13. 推荐抓取策略

X 搜索源噪声较高，不建议过于频繁抓取。

推荐：

```yaml
fetch_interval: 14400  # 4 小时
default_limit: 20
```

优先保证搜索质量，而不是数量。

推荐保留：

```text
-is:retweet
-is:reply
```

这样可以减少大量重复转发和低价值回复。

建议搜索语法中优先使用：

```text
url:github.com
url:huggingface.co
launch
released
introducing
open source
MCP
agent
workflow
```

不建议只搜：

```text
AI
LLM
ChatGPT
```

这些关键词过泛，噪声极高。

---

## 14. 最小可行闭环

最小实现流程如下：

```bash
# 1. 启动 RSSHub
cd rsshub-local
docker compose up -d

# 2. 验证 RSSHub 能抓 X
curl "http://127.0.0.1:1200/twitter/keyword/AI%20agent"

# 3. 回到 AItool_scraping 项目
cd /path/to/AItool_scraping

# 4. 配置 .env
# RSSHUB_BASE_URL=http://127.0.0.1:1200

# 5. 修改 source_registry.yaml，使用 /twitter/keyword/

# 6. 抓取 X 源
python scripts/run_fetch_once.py --group x --limit-per-source 5

# 7. 标准化
python scripts/run_normalize_once.py --limit 300

# 8. 预筛
python scripts/run_prefilter_once.py --limit 300
```

---

## 15. 实现结论

本方案的核心不是新增 X 爬虫，而是引入 RSSHub 作为中间层：

```text
RSSHub 负责访问 X 和生成 RSS
AItool_scraping 负责抓 RSS、入库、去重、筛选
```

这样做的优点是：

```text
1. 当前 Python 项目不需要直接处理 X 登录态、反爬、接口变化
2. RSSHub 的输出是标准 RSS，能复用现有 feedparser 管线
3. source_registry.yaml 只需新增或调整 RSSHub URL
4. 后续可以继续扩展更多 X 搜索词，而不改核心代码
```

当前最关键的修改点是：

```text
将 X 搜索源 URL 从 /twitter/search/ 改为 /twitter/keyword/
配置 RSSHUB_BASE_URL
部署 RSSHub 并设置 TWITTER_AUTH_TOKEN
```
