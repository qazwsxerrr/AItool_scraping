# Stage C 模型调用交替 400 / 200

本文记录 Stage C 曾经对本地 Responses 网关 `POST /v1/responses` 出现「失败一次、成功一次」的原因，以及当前修复方式。结论已用 `.env` 中的真实模型和现有客户端代码测试确认。

## 1. 原现象

C 阶段运行期间，httpx 日志曾呈现严格交替：

```text
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 400 Bad Request
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 200 OK
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 400 Bad Request
POST http://127.0.0.1:8317/v1/responses  HTTP/1.1 200 OK
...
```

400 很快返回，几乎不推理；200 明显更慢，才是真正跑模型。httpx 默认只打印状态码，看不到 400 的 error body，所以容易误判成「模型调用一半失败」。

## 2. 根因

这不是并发太高、模型随机失败，也不是 C 的业务筛选在拒稿。

根因是本地网关 `127.0.0.1:8317` 表面兼容 OpenAI Responses，接受 `previous_response_id`，但不持久化上一轮 `function_call` 状态。旧客户端每一轮都先按官方有状态协议发：

```text
previous_response_id = 上一轮 response.id
input                = [{ type: function_call_output, call_id, output }]
```

按官方 Responses 语义，这个请求是完整的：服务端应通过 `previous_response_id` 找回上一轮 response 中对应的 `function_call`。但 8317 找不到这个 `call_id`，于是返回：

```text
HTTP 400
No tool call found for function call output with call_id <call_id>
```

旧代码随后去掉 `previous_response_id`，把本地保存的完整 transcript 回放给网关，所以第二次请求能 200 并继续运行。

## 3. 当前修复

当前仓库已移除「先试 `previous_response_id`，失败后再回放」的兼容路径。Stage C 的 function agent 现在始终由本地维护完整 transcript，并在每轮调用 `/v1/responses` 时直接发送完整上下文：

```text
system / user 初始上下文
+ 模型上一轮 output（含 function_call）
+ 本地 function_call_output
+ 后续模型 output
+ 后续本地 tool output
...
```

也就是说，C 阶段不再依赖 8317 保存 `previous_response_id` 状态。8317 每轮只需要处理当前请求体中已经完整包含的工具调用链。

相关代码：

- `app/ai/responses.py`：`ResponsesClient.run_function_agent()` 维护并发送完整 `transcript_input`
- `app/ai/skills/stage_c_agent/client.py`：Stage C 不再传递 `previous_response_id`
- `app/jobs/event_cluster_job.py`：C 任务入口不再注入空的 `previous_response_id`
- `tests/test_responses_agent.py`：测试覆盖每轮不带 `previous_response_id`，且完整 transcript 中包含 `function_call` 与 `function_call_output`

## 4. 会话语义

当前 C 阶段不是把每一轮当成互不相关的单次会话。它是由客户端本地 transcript 模拟多轮会话：

```text
本地保存上下文
→ 每轮把完整上下文发给 8317
→ 8317 不需要保存 response_id 状态
→ 模型仍能看到完整工具调用历史
```

因此成功路径里的业务上下文不缺，缺的只是旧实现依赖过的网关侧 function-call 状态。

## 5. 修复后实测

用项目根目录 `.env` 的配置测试：

| 配置项 | 值 |
|---|---|
| `AI_REVIEW_API_URL` | `http://127.0.0.1:8317/v1` |
| `AI_REVIEW_MODEL` | `gpt-5.6-luna` |

结果：

- 单元测试验证后续工具回合不再包含 `previous_response_id`
- 2026-08-26 draft 副本的 3 条 active 小样本 Stage C 跑完
- 实测结果：`processed=3 events=3 turns=7 tools=10 web=1`
- 运行期间所有 8317 `/v1/responses` 日志均为 `200 OK`，未再出现原来的第二轮 400

完整 100 条 active 的 draft 副本也验证了前两轮 8317 调用均为 `200 OK`；后续第三轮因 120 秒读超时失败，这是模型/请求耗时问题，不是 `previous_response_id` function-call 状态问题。

## 6. 剩余边界

完整 transcript 会随工具回合增长，请求体会比真正有状态的 `previous_response_id` 续写更大。在当前 8317 网关上这是必要取舍：它换掉了每个工具回合一次必现的无效 400，并让 Stage C 的传输方式与 Codex 长上下文常见模式一致，即由客户端保存上下文再发送给本地代理。
