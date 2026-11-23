# ✍️ AstrBot Onepercent Generator

基于 [AstrBot](https://github.com/Soulter/AstrBot) 框架开发的 AstrBot 插件 —— 在 QQ 聊天中通过关键词触发，自动生成符合 [TapTap《百分之一》](https://www.taptap.cn/moment/371075389700702390) 活动格式的游戏推荐帖。

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue?style=flat)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/blob/main/LICENSE)
[![Release](https://img.shields.io/github/v/release/littleseven2003/astrbot_plugin_onepercent_generator?style=flat)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-orange?style=flat&logo=robot&logoColor=white)](https://github.com/Soulter/AstrBot)

</div>

---

## ✨ 核心特性

* 🎮 **关键词触发**：发送「小作文」或「我的百分之一」即可触发，支持随机游戏或指定游戏生成
* 🔍 **联网搜索**：支持 AstrBot 官方搜索、AnySearch、Tavily API 和网页搜索；按配置中心列表从上到下依次尝试已勾选的分组
* ✅ **格式一致**：保留活动格式，游戏名由程序拼装不经过 AI；可提供真实经历
* ⏱️ **频率限制**：按连接器和发送者 UID 应用固定时间窗口与每日上限
* 📋 **会话控制**：按完整 UMO 管理会话，并可分别启用群聊和私聊
* 🔧 **模型兼容**：支持 AstrBot 已配置模型，或手动配置 Chat Completions、Responses、Anthropic Messages

---

## 🛠️ 安装依赖

插件依赖 `httpx`，AstrBot 环境通常已内置。如未安装，请在 AstrBot 的 Python 环境中执行：

```bash
pip install httpx
```

---

## 📖 使用指南

<details>
<summary><b>🚀 快速上手：从零到生成你的第一篇小作文 (点击展开)</b></summary>

### 步骤 1：安装插件

将本插件目录放入 AstrBot 的插件目录中，然后在 AstrBot 管理页面启用插件。

### 步骤 2：配置 AI 模型

前往 AstrBot 管理面板的插件配置中心，选择 AstrBot 已配置的聊天模型，或手动填写以下配置：

| 配置项 | 说明 | 示例 |
| :--- | :--- | :--- |
| API Base URL | 所选协议的服务地址 | `https://api.deepseek.com/v1` |
| API Key | 你的 API 密钥 | `sk-xxxxxxxx` |
| API 格式 | 服务使用的接口格式 | `chat_completions`、`responses` 或 `anthropic_messages` |
| 发送 temperature 参数 | 服务不接受该参数时可关闭，默认开启 | `true` / `false` |
| 模型名称 | 使用的模型 | `deepseek-chat` |

未设置 API 格式的旧配置默认使用 `chat_completions`，并默认发送 `temperature`。当服务要求 OpenAI Responses API 时，将 API 格式切换为 `responses`；插件会调用 `{Base URL}/responses`，无需把 `/responses` 手动写入 Base URL。部分模型不接受 `temperature` 参数，可在配置中关闭「发送 temperature 参数」；关闭后所有手动 API 格式均不发送该参数。具体支持情况以模型服务商文档为准。

### 步骤 3：配置管理员（可选）

在目标会话发送 AstrBot 的 `/sid`，在配置中心将连接器和显示的发送者 UID 一起加入管理员列表。QQ 官方机器人通常使用 OpenID，不一定是数字 QQ 号；管理员身份与会话 UMO 白名单分开。

### 步骤 4：开始使用

在 QQ 群聊或私聊中发送：

> `小作文` → 随机选择预设游戏生成
>
> `小作文 原神` → 指定游戏生成
>
> `小作文 原神 经历：我喜欢探索，玩了约 20 小时` → 结合自己提供的经历生成

提供经历时请同时指定游戏名；不提供经历也可以生成客观推荐。等待片刻即可收到生成的小作文。

</details>

<details>
<summary><b>🎮 用户指令：触发生成 (点击展开)</b></summary>

### 触发方式

| 指令 | 说明 |
| :--- | :--- |
| `小作文` | 从预设游戏列表中随机选择一款游戏生成小作文 |
| `小作文 游戏名` | 指定游戏生成小作文 |
| `小作文 游戏名 经历：真实经历` | 指定游戏，并提供自己的游玩经历 |
| `我的百分之一` | 同「小作文」 |
| `我的百分之一 游戏名` | 同「小作文 游戏名」 |
| `我的百分之一 游戏名 经历：真实经历` | 同指定游戏并提供经历 |

经历标记支持中文或英文冒号，也可以另起一行；经历最多取前 600 个字符。提供经历时必须同时填写游戏名。经历会发送给你配置的 AI 服务，请勿填写不想分享的信息。

### 输出格式

以下为提供了相应经历后的示意，实际信息请自行核对。正文目标为 800–1200 字，模型实际输出可能不同；没有提供经历时同样可以生成，前置信息缺失时会按游戏情况给出合理内容，不显示「请补充」类占位符，正文外不会追加缺失字段、长度不足或截断提示，也不会自动重试。

```
【我的百分之一】+【原神】

什么是【我的百分之一】见帖子说明：
https://www.taptap.cn/moment/371075389700702390
（这个需要加，省得新玩家不知道是啥情况）
游戏名称：原神
游戏当时发售平台：PC / PS4 / iOS / Android
玩游戏的时间：约 20 小时
推荐人群（理由）：喜欢开放世界探索和二次元风格的玩家，开放世界探索内容足够多
正文：原神是一款开放世界冒险游戏，你将扮演"旅行者"在提瓦特大陆上展开冒险……

我最喜欢的是探索地图，玩了约 20 小时后仍想看看还有哪些地方没有去过……

如果你喜欢探索、收集和剧情，原神绝对值得一试。
```

> 💡 说明：
> - 第一行：活动标题；第二段为活动说明与链接，并固定带「（这个需要加，省得新玩家不知道是啥情况）」
> - `游戏名称`：由程序直接拼装，不经过 AI
> - `游戏当时发售平台 / 玩游戏的时间 / 推荐人群（理由）`：按活动顺序由程序拼装；你在触发消息里写明的内容优先保留，素材里有明确的首发平台信息时也会优先采用，没有依据时按游戏情况生成合理内容，不省略整行、也不显示占位符
> - `游玩时间`：优先采用你在经历里写的真实时长或时期（如“初中暑假”）；没写时结合正文故事给出合理时期，搜索素材里有明确发售日期时不会早于该日期，没有发售日期素材时保留模型的合理时期
> - `许愿卡 / 游戏ID / 账号ID / 区服 / 系统`：仅当你在触发消息里明确写出时才作为可选尾注追加，不会编造
> - 正文由 AI 根据搜索素材和用户经历生成，正文里不再出现“游戏介绍：”“故事/感想：”小标题，发布前请核对事实
> - 没有提供经历时，插件直接生成客观推荐与合理的游玩场景，不会另发补充提示

</details>

<details>
<summary><b>🔐 管理员指令 (点击展开)</b></summary>

### 测试指令

| 指令 | 说明 |
| :--- | :--- |
| `/小作文模型测试` | 测试当前 AI 模型服务是否可用（不受频率限制） |
| `/小作文搜索测试 游戏名` | 测试联网搜索功能（默认测试「原神」） |

> 💡 以上指令仅插件配置的管理员发送者 ID 可使用。管理员不受白名单和频率限制。

</details>

<details>
<summary><b>⚙️ 完整配置说明 (点击展开)</b></summary>

日常配置通过 AstrBot 管理面板中的插件「小作文生成器」完成；旧配置字段保留用于迁移，不再是另一套日常设置入口。

### 模型与搜索

- 模型可选择 AstrBot 已配置的聊天模型，或手动填写服务地址、密钥、模型 ID 和 API 协议。手动配置支持 Chat Completions、Responses 和 Anthropic Messages；模型列表与测试操作会访问所选服务。切换手动服务地址时，按页面提示重新输入密钥。
- 搜索策略只有「Astrbot与搜索插件」「搜索API」「网页搜索」三组：拖动调整顺序并勾选启用项，运行时严格按列表从上到下尝试，首个返回有效结果的分组即停止；AstrBot 组内先官方搜索工具、再已安装的搜索插件。总开关关闭或三组全部取消勾选时不发起任何搜索调用。旧配置的自动/单模式与降级设置会在升级时幂等转换为三组列表。
- 搜索能力取决于实际安装、启用和配置情况；测试功能会发起请求，服务商可能计费。

### 会话、管理员与升级迁移

在目标会话发送 AstrBot 的 `/sid`：完整 UMO 用于会话授权；管理员需要添加明确的连接器和发送者 UID。两者不能互相替代。私聊和群聊可分别启用或关闭，会话列表非空时精确匹配完整 UMO。

旧 UMO 中能够识别的会话会保留；无法明确映射的旧限制和旧管理员需要在配置中心处理。旧管理员未指定连接器时不会自动获得所有连接器的权限。迁移尚未处理完时，未能明确放行的会话会被拒绝；清空限制并改为不限制需要明确确认。升级前建议备份插件配置及数据，升级后检查待处理项目和会话访问。

QQ 官方机器人通常使用 OpenID，不保证群聊的 `member_openid` 与私聊的 `user_openid` 相同。插件不会自动推断它们属于同一人；确认身份后可在配置中心手动关联，关联影响限流计数。管理员权限仍需单独配置。

### 频率限制与统计

- 限流按连接器和发送者 UID 计算，手动关联的身份共享计数；管理员不受限流限制。
- 窗口为 0–1440 分钟，0 表示窗口不限；启用窗口时次数为 1–1000。每日上限为 0–1000，0 表示每日不限；重置时区可配置，留空使用服务器本地时区。
- 关联或解除关联期间如计数迁移未完成，普通请求会暂时被拒绝，并在后续请求或保存前尝试恢复。持续失败时检查 AstrBot 日志与存储状态，不要手动删除迁移日志。
- 状态页展示已记录的请求、搜索调用和可取得的 Token 用量，并支持时间及会话筛选；未取得的 Token 不应理解为实际消耗为零。
</details>

---

## 📁 项目结构

```text
├── main.py                    # 插件主入口，消息处理、指令注册
├── prompt.py                  # 经历解析与 Prompt 模板组装
├── post_process.py            # AI 响应后处理（清洗、拼装最终帖子）
├── ai_client.py               # AI API 调用封装（兼容 OpenAI 格式）
├── search_service.py          # AstrBot、Tavily API 与网页搜索服务
├── rate_limiter.py            # 频率限制逻辑
├── whitelist.py               # 白名单管理逻辑
├── metadata.yaml              # 插件元数据
├── _conf_schema.json          # 配置 Schema（WebUI 配置页）
├── requirements.txt           # Python 依赖
├── LICENSE                    # MIT 开源协议
├── README.md                  # 说明文档
└── docs/                      # 项目文档
    ├── Design.md              # 详细设计文档
    ├── CHANGELOG.md           # 更新日志
    └── VERSIONING.md          # 分支、版本与发布约定
```

---

## 🗺️ 未来更新计划 (Roadmap)

- [ ] 支持用户手动填写字段（发售平台、游玩时间等）
- [ ] 支持生成结果图片化输出
- [ ] 支持自定义活动说明链接
- [ ] 支持多组 Prompt 模板切换

---

## 🙏 致谢

* [Soulter/AstrBot](https://github.com/Soulter/AstrBot) - AstrBot 框架
* [onepercent_taptap_generator](https://github.com/littleseven2003/onepercent_taptap_generator) - 原项目核心逻辑

---

## 📄 开源许可

本项目采用 [MIT](LICENSE) 开源许可证。

---

## 🛑 免责声明

本项目仅用于软件开发、AI 工具研究与技术交流学习，主要用于探索 Web 应用开发、AI 内容生成、搜索服务整合和开源项目维护流程。

本项目页面、提示词和生成内容可能涉及已上线游戏《百分之一》及 TapTap 活动相关信息。本项目不是《百分之一》官方产品，不代表游戏开发方、发行方或 TapTap 平台立场，也不提供任何商业化服务。

项目运行过程中获取或整理的公开资料仅用于学习、测试和内容生成演示。不得将本项目用于违规获取游戏资源、绕过平台或游戏规则、违规参与游戏活动、刷取奖励、伪造内容或其他可能损害游戏方、平台方及其他用户权益的行为。

使用者应遵守相关游戏、平台活动规则、版权、商标和社区规范。生成内容仅供参考，发布前请自行核对事实，并自行承担使用与发布责任。

---

*如果您觉得这个插件不赖，请给一个 ⭐ Star 以示支持！*
