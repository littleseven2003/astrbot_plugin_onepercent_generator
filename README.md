# ✍️ AstrBot Onepercent Generator

基于 [AstrBot](https://github.com/Soulter/AstrBot) 框架开发的 AstrBot 插件 —— 在 QQ 聊天中通过关键词触发，自动生成符合 [TapTap《百分之一》](https://www.taptap.cn/moment/371075389700702390) 活动格式的游戏推荐帖。

<div align="center">

[![Stars](https://img.shields.io/github/stars/littleseven2003/astrbot_plugin_onepercent_generator?style=social)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/stargazers)
[![Forks](https://img.shields.io/github/forks/littleseven2003/astrbot_plugin_onepercent_generator?style=social)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/network/members)
[![Issues](https://img.shields.io/github/issues/littleseven2003/astrbot_plugin_onepercent_generator?style=social)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/issues)
[![Pull Requests](https://img.shields.io/github/issues-pr/littleseven2003/astrbot_plugin_onepercent_generator?style=social)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/pulls)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue?style=flat)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/blob/main/LICENSE)
[![Release](https://img.shields.io/badge/Release-v0.1.0-green?style=flat)](https://github.com/littleseven2003/astrbot_plugin_onepercent_generator/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-orange?style=flat&logo=robot&logoColor=white)](https://github.com/Soulter/AstrBot)

</div>

---

## ✨ 核心特性

* 🎮 **关键词触发**：发送「小作文」或「我的百分之一」即可触发，支持随机游戏或指定游戏生成
* 🔍 **联网搜索**：自动搜索游戏公开资料（Bing → 百度多源 fallback），让 AI 生成更贴合实际的内容
* ✅ **格式一致**：完全复现原项目 Prompt 与后处理逻辑，游戏名由程序拼装不经过 AI
* ⏱️ **频率限制**：基于 QQ 号的滑动窗口 + 每日上限双重限制，防止滥用
* 🚫 **黑名单管理**：管理员可针对特定群聊/私聊开启或关闭功能
* 🖥️ **WebUI 管理**：通过 AstrBot 插件管理页面可视化管理黑名单
* 🔧 **模型兼容**：兼容 OpenAI 格式 API（DeepSeek / OpenAI / 通义千问等），支持 Mock 模式

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

前往 AstrBot WebUI 插件配置页，填写以下配置：

| 配置项 | 说明 | 示例 |
| :--- | :--- | :--- |
| API Base URL | 兼容 OpenAI 格式的 API 地址 | `https://api.deepseek.com/v1` |
| API Key | 你的 API 密钥 | `sk-xxxxxxxx` |
| 模型名称 | 使用的模型 | `deepseek-chat` |

### 步骤 3：配置管理员（可选）

在插件配置页的「管理员QQ号列表」中添加你的 QQ 号，用于后续管理指令。

### 步骤 4：开始使用

在 QQ 群聊或私聊中发送：

> `小作文` → 随机选择预设游戏生成
>
> `小作文 原神` → 指定游戏生成

等待片刻即可收到生成的小作文。

</details>

<details>
<summary><b>🎮 用户指令：触发生成 (点击展开)</b></summary>

### 触发方式

| 指令 | 说明 |
| :--- | :--- |
| `小作文` | 从预设游戏列表中随机选择一款游戏生成小作文 |
| `小作文 游戏名` | 指定游戏生成小作文 |
| `我的百分之一` | 同「小作文」 |
| `我的百分之一 游戏名` | 同「小作文 游戏名」 |

### 输出格式

```
什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390

游戏名称：原神
发售平台：PC / PS4 / PS5 / iOS / Android
游玩时间：断断续续玩了一年多
推荐人群：喜欢开放世界探索和二次元风格的玩家

原神是一款开放世界冒险游戏，你将扮演"旅行者"在提瓦特大陆上展开冒险……

我是开服玩家，最开始被游戏的美术和音乐吸引，后来沉迷于各种角色的故事线……

如果你喜欢探索、收集和剧情，原神绝对值得一试。
```

> 💡 说明：
> - 第一行：活动说明链接（固定）
> - `游戏名称`：由程序直接拼装，不经过 AI
> - `发售平台 / 游玩时间 / 推荐人群`：AI 自动生成
> - 正文内容：AI 自动生成

</details>

<details>
<summary><b>🔐 管理员指令 (点击展开)</b></summary>

### 功能控制

| 指令 | 说明 |
| :--- | :--- |
| `/开启小作文功能` | 为当前群聊/私聊开启小作文功能（从黑名单移除） |
| `/关闭小作文功能` | 为当前群聊/私聊关闭小作文功能（加入黑名单） |

### 测试指令

| 指令 | 说明 |
| :--- | :--- |
| `/小作文模型测试` | 测试当前 AI 模型服务是否可用（不受频率限制） |
| `/小作文搜索测试 游戏名` | 测试联网搜索功能（默认测试「原神」） |

> 💡 以上指令仅配置的管理员 QQ 号可使用。

</details>

<details>
<summary><b>⚙️ 完整配置说明 (点击展开)</b></summary>

在 AstrBot WebUI 插件配置页中可配置以下项目：

| 配置项 | 类型 | 说明 | 默认值 |
| :--- | :--- | :--- | :--- |
| 管理员QQ号列表 | 列表 | 支持多个管理员 | 空 |
| AI Base URL | 字符串 | API 地址，兼容 OpenAI 格式 | — |
| AI API Key | 字符串 | API 密钥 | — |
| AI Model | 字符串 | 模型名称 | `deepseek-chat` |
| 预设游戏列表 | 列表 | 随机生成时的游戏池 | 原神、星露谷物语、艾尔登法环、塞尔达传说：王国之泪、博德之门3 |
| 限制时间窗口 | 整数 | 滑动窗口（分钟） | `10` |
| 窗口内最大请求次数 | 整数 | 每 QQ 号窗口内限制 | `3` |
| 每日最大请求次数 | 整数 | 每 QQ 号每日限制 | `20` |
| 是否启用联网搜索 | 布尔 | 是否搜索游戏资料 | `true` |
| 搜索超时时间 | 整数 | 搜索超时（毫秒） | `8000` |
| 默认是否启用 | 布尔 | 新群聊/私聊的默认状态 | `true` |

### 频率限制说明

- 限制基于 QQ 号**全局生效**，不区分群聊或私聊
- 滑动窗口与每日上限双重限制
- 管理员不受频率限制
- `/小作文模型测试` 不受频率限制

</details>

---

## 📁 项目结构

```text
├── main.py                    # 插件主入口，消息处理、指令注册
├── prompt.py                  # Prompt 模板组装（完全复现原项目逻辑）
├── post_process.py            # AI 响应后处理（清洗、拼装最终帖子）
├── ai_client.py               # AI API 调用封装（兼容 OpenAI 格式）
├── search_service.py          # 联网搜索服务（Bing / 百度）
├── rate_limiter.py            # 频率限制逻辑
├── blacklist.py               # 黑名单管理逻辑
├── metadata.yaml              # 插件元数据
├── _conf_schema.json          # 配置 Schema（WebUI 配置页）
├── requirements.txt           # Python 依赖
├── LICENSE                    # MIT 开源协议
├── README.md                  # 说明文档
├── pages/settings/            # WebUI 黑名单管理页面
└── docs/                      # 项目文档
    ├── Design.md              # 详细设计文档
    └── CHANGELOG.md           # 更新日志
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
