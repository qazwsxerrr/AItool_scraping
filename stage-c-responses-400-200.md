# Stage C 模型调用交替 400 / 200

本文记录 Stage C 筛选时，对本地 Responses 网关 `POST /v1/responses` 总是「失败一次、成功一次」的原因。结论已用 `.env` 中的真实模型和现有客户端代码实测确认。

## 1. 现象

C 阶段运行期间，httpx 日志呈现严格交替：

```text
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 400 Bad Request
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 200 OK
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 400 Bad Request
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 200 OK
...
```

时间特征也很稳定：

- 400 很快返回，几乎不推理
- 200 明显更慢，才是真正跑模型

httpx 只打印状态码，看不到 400 的 error body，所以看起来像「模型调用一半失败」。应用层其实把这次 400 吞掉，并立刻发了第二次请求。

## 2. 结论

**不是并发太高，也不是模型随机失败，更不是 C 的筛选规则在拒稿。**

根因是两层叠加：

1. 本地网关 `127.0.0.1:8317` 表面兼容 OpenAI Responses，接受 `previous_response_id`，但**不持久化**上一轮 `function_call`。
2. 现有客户端每一轮都**先按官方有状态协议试一次**；被拒后再去掉该字段，把完整 transcript 回放一遍。

因此从第 2 个工具回合开始，每个逻辑回合都会固定打出一次 400、再打一次 200。第二次 200 才是有效推理。业务上 agent 可以继续，最终仍能 finalize。

## 3. 不是什么

| 猜测 | 为什么排除 |
|---|---|
| 请求并发太高 | 单线程、一次只发一个请求，仍严格 `200 → 400 → 200`。400 不是 429，也不是超时或连接被踢。 |
| 模型质量不稳 | 400 来得太快，是请求体校验失败，不是生成失败。错误句固定。 |
| C 业务筛选在拒稿 | 发生在 HTTP 层，事件聚合还没拿到这一轮模型输出。 |
| 通用 400 重试写坏了 | `ResponsesProviderError.retryable` 只把 429 / 5xx 当可重试。普通 400 不会走通用重试。 |
| 任务恢复接回了旧会话 | `event_cluster_job` 入口固定传 `previous_response_id=None`。问题出在同一次 run **内部**的第 2、3、4… 轮。 |
| hosted web_search / `include` | C 的 `StageCAgentClient` 把 `hosted_tools` 设成空，搜索走本地 Tavily。 |

## 4. Stage C 实际怎么调模型

C 不是「一条资讯打一次补全」。它是同一条 agent 会话里的多轮 function calling：

```text
event_cluster_job
  → StageCAgentClient.run()
    → ResponsesClient.run_function_agent()
      → 循环：发 /v1/responses → 执行本地工具 → 把 tool output 交回模型
```

相关代码：

- `app/jobs/event_cluster_job.py`：C 任务入口，每次重跑都传 `previous_response_id=None`
- `app/ai/skills/stage_c_agent/client.py`：C 专用 Responses 客户端，不挂 hosted web_search
- `app/ai/responses.py`：`run_function_agent()` 负责链式续写和回放兼容

第 1 轮只把任务上下文发出去，例如 `run_id`、active/reserve 数量、近三期窗口。模型此时还看不到具体资讯，通常先调 `list_candidates` / `read_items` 等本地工具。

本地执行工具（查库、读 B 分析、写草稿）不经过模型。

第 2 轮的业务含义是：**把第 1 轮工具结果交回模型，让它继续做事件聚合。** 不是另开一场筛选。

## 5. 第 2 轮请求长什么样

### 5.1 第一次试发：官方链式续写（必现 400）

```text
instructions          = 完整 C 策略（每轮都重发）
tools                 = 全套本地工具
previous_response_id  = 第 1 轮 response.id
input                 = [{ type: function_call_output, call_id, output }]
```

这里故意不再带第 1 轮的 system/user，也不再带模型刚才那个 `function_call`。  
按 OpenAI Responses 的设计，这些应由网关靠 `previous_response_id` 自己补齐。

8317 补不齐，于是返回：

```text
HTTP 400
No tool call found for function call output with call_id <call_id>
```

这句话的意思是：网关当前会话里找不到这个 `call_id` 对应的 `function_call`，不能收这份 tool output。不是「对话少传了半截字段」，也不是并发把请求打乱了。

### 5.2 第二次请求：本地 transcript 回放（200）

`run_function_agent()` 识别到上述固定 400 后，只重试一次：

- 去掉 `previous_response_id`
- 把本地保存的完整 `replay_input` 整段放进 `input`

回放内容是：

```text
system（C 指令）
user（initial_context）
+ 第 1 轮模型 output（含 function_call）
+ 本地 function_call_output
```

网关这回能在**同一份请求体**里看到对应 tool call，于是真正跑模型并返回 200。

触发条件写死在 `_requires_function_call_replay()`，非常窄：

```python
status_code == 400
and "no tool call found for function call output" in message
```

不是泛化重试所有 400。仓库测试 `test_responses_agent_replays_function_calls_for_a_gateway_without_response_state` 复现的就是同一组请求。

## 6. 会话传输缺不缺

| 请求 | 客户端发出的内容 | 缺不缺 |
|---|---|---|
| 第 2 轮第一次（400） | 只发增量：`previous_response_id` + `function_call_output` | 对官方协议来说不缺。对 8317 来说，服务端会话是空的，等价于缺了上一轮 `function_call`。 |
| 第 2 轮回放（200） | 本地整段重发 `system + user + function_call + function_call_output` | 语义上不缺。实测就是这个形状，并且能继续。 |
| 任务中断后重跑 | 入口固定 `previous_response_id=None` | 模型侧旧会话不接。草稿在数据库里，靠工具再读，不靠网关记忆。 |

更准确的说法：

- **不是**本地把 tool 结果弄丢了。`call_id` 和 `output` 都在。
- **是**第一次续写把历史寄托在网关会话上，而 8317 没有这份会话。
- **回放那次没有传丢。** 客户端本地一直存着完整 `replay_input`。

同一次筛选里，成功路径的业务上下文是完整的。之后每一轮仍会先发短续写、再被拒、再回放。transcript 会越来越长，但每一次成功请求都是「当前完整历史」，不是残缺会话。

## 7. 实测记录

用项目根目录 `.env` 的配置，走现有 `StageCAgentClient` / `ResponsesClient.run_function_agent()`，做最小两轮工具循环（`ping` → `finish`）：

| 配置项 | 值 |
|---|---|
| `AI_REVIEW_API_URL` | `http://127.0.0.1:8317/v1` |
| `AI_REVIEW_MODEL` | `gpt-5.6-luna` |

| 次序 | HTTP | 耗时 | 请求形态 | 结果 |
|---|---|---|---|---|
| 1 | 200 | 3070ms | 无 `previous_response_id`，`input=['system','user']` | 模型返回 `function_call` |
| 2 | 400 | 2350ms | 带 `previous_response_id`，`input=['function_call_output']` | `No tool call found for function call output with call_id call_hQ0wcvLuZwk6v0Qgzp8xyc8E.` |
| 3 | 200 | 2853ms | 去掉 `previous_response_id`，回放完整 transcript | 模型再返回 `function_call`，agent 正常结束 |

最终：`finalized=true`，`turns=2`，`tool_calls=2`。  
这与线上 C 阶段日志同一模式。探测工具很轻，所以 400/200 的快慢差没有正式筛选那么夸张，但请求形态和错误句已经足够定性。

若日志片段从 400 开头，通常只是没把真正的第 1 轮 200 一起拷进来。按代码，首轮不带 `previous_response_id`，一般应先有一次 200。

## 8. 责任划分

| 层 | 角色 |
|---|---|
| 8317 网关 | 半兼容 Responses：字段能传，function-call 状态链断了。这是能力缺口。 |
| 现有客户端 | 每一轮都先按官方协议试，失败后再回放。这是刻意写的兼容路径，不是偶发 bug。 |
| C 业务筛选 | 无关。400 发生在 HTTP 传输层。 |

所以这是 **网关能力 + 客户端兼容策略** 的组合，不是并发问题，也不是「实现写错导致任务失败」。400 被精确识别后任务继续，说明兼容补丁在反复生效。

副作用：

- 每个工具回合多一次必现的无效 HTTP 请求
- 回放请求的 `input` 会随回合变长，后续 200 更贵、更慢
- 日志噪声大，容易误判成模型或限流问题

## 9. 如何再次确认

看任意一次 400 的响应体（网关日志，或把 `ResponsesProviderError` 打出来）。若是：

```text
No tool call found for function call output with call_id ...
```

就可以排除模型质量和 C 筛选逻辑。

对应代码：

- 链式续写与回放：`app/ai/responses.py` 中 `run_function_agent()`
- 400 识别：`app/ai/responses.py` 中 `_requires_function_call_replay()`
- 单元测试：`tests/test_responses_agent.py` 中 `test_responses_agent_replays_function_calls_for_a_gateway_without_response_state`

## 10. 若要消除交替 400

方向只有两类，本文只记录，不在此修改代码：

1. 换或改造网关，使其真正持久化 `previous_response_id` 后的 function-call 状态。这样第一次短续写就会 200，回放路径不再触发。
2. 改客户端：对已知半兼容网关，从第 2 轮起直接走无状态回放，不要先发那次注定失败的链式续写。

在现网关上，当前实现能保证 C 继续跑完；付出的代价是每个工具回合多一次 400。
