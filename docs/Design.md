# 百分之一小作文生成器：设计文档

## 1. 项目定位

本项目是一个 AstrBot 插件。用户在 QQ 群聊或私聊中发送关键词后，插件根据指定或随机选择的游戏生成符合 TapTap《百分之一》活动格式的推荐帖。

当前设计目标：

- 游戏名称始终使用用户输入或预设值，不交给模型改写。
- 联网搜索失败时仍可继续生成，外部搜索不是强依赖。
- 管理员、白名单和频率限制均通过 AstrBot 插件配置及 KV 存储实现。
- AI 服务保持 OpenAI 兼容接口，网络访问全部使用异步客户端。

## 2. 运行流程

```text
QQ 消息
  → 关键词匹配
  → 管理员/白名单校验
  → 用户频率限制
  → 选择或读取游戏名称
  → Bing 与百度并发搜索
  → 可选的 AI 搜索摘要
  → 生成推荐帖正文
  → 清理模型误输出的标题和游戏名称
  → 发送正文及可选统计信息
```

管理员测试指令直接进入相应服务，不受普通用户白名单及频率限制。

## 3. 模块职责

| 模块 | 职责 |
| --- | --- |
| `main.py` | 插件注册、配置加载、事件处理和整体流程编排 |
| `ai_client.py` | AI 请求、响应解析、错误分类、连接复用及资源释放 |
| `search_service.py` | Bing/百度并发搜索、HTML 文本提取和结果合并 |
| `prompt.py` | 系统提示词及正文提示词组装 |
| `post_process.py` | 清理模型输出并拼装活动说明与最终正文 |
| `rate_limiter.py` | 基于 QQ 号的窗口计数和每日计数 |
| `whitelist.py` | 群聊及私聊白名单判断 |
| `_conf_schema.json` | AstrBot 插件设置菜单的数据结构 |
| `metadata.yaml` | 插件名称、版本、作者和仓库信息 |

## 4. 配置模型

### AI 配置

- `ai_config.base_url`：兼容服务的 API 根地址，末尾斜杠会被移除。
- `ai_config.api_key`：Bearer Token，不写入日志。
- `ai_config.model`：提交给服务端的模型标识。

### 搜索配置

- `search_config.enabled`：是否启用联网搜索。
- `search_config.timeout_ms`：单次 HTTP 请求超时。
- `search_config.result_count`：每个搜索源采用的结果数，运行时限制为 1–10。
- `search_config.summarize_enabled`：是否调用 AI 整理搜索结果。
- `search_config.summarize_max_chars`：搜索摘要的目标字数。

### 权限及限流配置

- `admin_qqs`：管理员 QQ 号列表。
- `whitelist_groups`：允许使用功能的群号；为空时允许全部群聊。
- `whitelist_privates`：允许使用功能的 QQ 号；为空时允许全部私聊。
- `rate_limit.*`：按 QQ 号全局生效的窗口及每日请求上限。
- `show_generation_stats`：是否在正文后发送模型、耗时、Token 和限流状态。

## 5. 外部服务边界

### AI 服务

当前版本通过 `POST {base_url}/chat/completions` 调用兼容 OpenAI Chat Completions 的服务。请求包含 system/user 两条消息，响应正文来自 `choices[0].message.content`。

所有 AI 调用统一转换为：

```text
content
model
duration_ms
token_usage.prompt_tokens
token_usage.completion_tokens
token_usage.total_tokens
```

上层流程不直接依赖服务端原始响应结构。

### 搜索服务

Bing 和百度同时请求。单个来源失败不会中断另一个来源；所有来源失败时返回空摘要，正文生成使用模型自身知识继续执行。

搜索页 HTML 结构可能变化，因此解析失败必须被视为可恢复故障。

## 6. 数据与生命周期

频率限制数据使用 `rate_limit:<qq_id>` 作为 AstrBot KV 键，保存窗口起点、窗口计数、当天日期和每日计数。

`AIClient` 与 `SearchService` 各自复用一个 `httpx.AsyncClient`。插件卸载时由 `terminate()` 关闭客户端。

## 7. 错误处理

- AI 未配置、超时、认证失败、限流和服务端错误转换为用户可理解的提示。
- 搜索异常只记录警告并降级，不向用户暴露堆栈。
- 日志记录模型、服务地址、耗时和 Token，不记录 API Key。
- 模型返回结构异常统一包装为 `AIClientError`。

## 8. 测试与持续集成

测试使用 Python 标准库 `unittest`，不调用真实 AI 或搜索服务。基础测试覆盖：

- Prompt 和最终消息格式；
- 白名单行为；
- 搜索辅助解析及禁用状态；
- 频率限制计数；
- AI 未配置错误。

GitHub Actions 在 Python 3.10 和 3.12 上执行语法检查与单元测试。涉及真实 AstrBot 事件、QQ 适配器或第三方 API 的行为仍需在 AstrBot 测试环境进行集成验证。

## 9. 已知约束

- 搜索依赖公开网页结构，不能保证长期稳定。
- KV 计数采用读取后写回方式，不提供跨进程原子性。
- 当前窗口算法为固定窗口计数，并非逐请求淘汰的严格滑动窗口。
- 请求在进入外部调用前计数，因此后续生成失败仍会占用额度。

这些约束应在后续版本中通过独立变更和测试逐项处理。
