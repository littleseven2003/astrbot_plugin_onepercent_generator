# 项目设计文档：百分之一小作文生成器 AstrBot 插件

## 1. 项目概述

### 1.1 项目名称
- 英文名：`astrbot_plugin_onepercent_generator`
- 中文名：百分之一小作文生成器

### 1.2 项目背景
原项目 [onepercent_taptap_generator](https://github.com/littleseven2003/onepercent_taptap_generator) 是一个面向 TapTap《百分之一》活动的 AI 推荐帖生成 Web 工具。现将其核心功能移植为 AstrBot 插件，使其能够在 QQ 群聊/私聊中通过关键词触发，自动生成符合活动格式的小作文。

### 1.3 项目目标
- 在 QQ 聊天环境中提供便捷的"百分之一小作文"生成功能
- 支持随机游戏和指定游戏两种触发方式
- 提供完善的管理功能，包括权限控制、频率限制、黑名单等
- 保持与原项目相同的 AI 调用标准（兼容 OpenAI 格式）

### 1.4 目标用户
- QQ 群聊中参与《百分之一》活动的玩家
- 需要快速生成游戏推荐帖的用户

---

## 2. 功能范围

### 2.1 用户侧功能
- 关键词触发：发送"小作文"或"我的百分之一" → 随机选择预设游戏列表中的一款游戏生成小作文
- 指定游戏触发：发送"小作文 XX"或"我的百分之一 XX" → 生成 XX 游戏对应的小作文
- 支持群聊和私聊两种场景

### 2.2 管理侧功能
- 管理员 QQ 号配置（支持多个，通过插件配置页面设置）
- 频率限制机制：按 QQ 号限制，可配置时间窗口和请求次数
- 模型配置：Base URL、API Key、Model Name（OpenAI 标准格式）
- 搜索配置：是否启用联网搜索、搜索超时时间
- 黑名单管理：通过 AstrBot 插件管理页面可视化管理（查看、添加、移除）
- 指令 `/开启小作文功能`：管理员针对当前群聊/私聊开启功能（从黑名单移除）
- 指令 `/关闭小作文功能`：管理员针对当前群聊/私聊关闭功能（加入黑名单）
- 指令 `/小作文模型测试`：管理员测试当前模型服务是否可用（不受频率限制）
- 指令 `/小作文搜索测试`：管理员测试联网搜索功能是否正常

---

## 3. 技术栈选择

### 3.1 插件框架
- AstrBot 插件系统（Python）
- 使用 AstrBot 提供的 API：消息事件处理、KV 存储、配置管理

### 3.2 AI 调用
- 使用 `httpx` 异步 HTTP 客户端
- 兼容 OpenAI Chat Completions API 格式
- 支持 DeepSeek、OpenAI、通义千问等服务商

### 3.3 数据存储
- AstrBot KV 存储：用于黑名单、频率限制记录
- 插件配置文件：用于管理员列表、模型配置、限制参数

### 3.4 技术栈选择理由
- **Python + AstrBot API**：原生支持 AstrBot 插件系统，无需额外依赖
- **httpx**：异步 HTTP 客户端，性能优于 requests
- **KV 存储**：AstrBot 内置，轻量且可靠，无需额外数据库

---

## 4. 系统架构

```text
QQ 用户消息
   ↓
AstrBot 消息路由
   ↓
astrbot_plugin_onepercent_generator
   ↓
┌─────────────────────────────────────────────────┐
│  消息解析 → 关键词匹配 → 权限检查               │
│      ↓                                           │
│  频率限制检查 → 预设游戏选择/用户指定            │
│      ↓                                           │
│  联网搜索游戏资料（Bing/百度）                   │
│      ↓                                           │
│  Prompt 组装（完全复现原项目 buildMainPrompt）   │
│      ↓                                           │
│  AI API 调用（temperature=0.8, max_tokens=2048）│
│      ↓                                           │
│  后处理：parseAIResponse + strip清洗             │
│      ↓                                           │
│  拼装最终帖子（游戏名由程序拼入，不经过AI）      │
│      ↓                                           │
│  返回消息                                        │
└─────────────────────────────────────────────────┘
   ↓
QQ 消息回复
```

---

## 5. 目录结构

```text
astrbot_plugin_onepercent_generator/
├── main.py                    # 插件主入口，消息处理、指令注册
├── metadata.yaml              # 插件元数据
├── _conf_schema.json          # 配置 Schema（管理员、模型、限制参数、搜索配置、预设游戏）
├── requirements.txt           # Python 依赖（httpx）
├── README.md                  # 插件说明文档
├── prompt.py                  # Prompt 模板组装（完全复现原项目逻辑）
├── post_process.py            # AI 响应后处理（清洗、拼装最终帖子）
├── ai_client.py               # AI API 调用封装
├── search_service.py          # 联网搜索服务（Bing/百度等）
├── rate_limiter.py            # 频率限制逻辑
├── blacklist.py               # 黑名单管理逻辑
└── pages/                     # WebUI 管理页面
    └── settings/
        ├── index.html         # 黑名单管理页面
        ├── app.js             # 页面逻辑
        └── style.css          # 页面样式
```

---

## 6. 核心模块设计

### 6.1 消息处理模块 (`main.py`)

**职责：**
- 注册消息监听器和指令处理器
- 解析用户消息，匹配关键词
- 协调各模块完成生成流程

**关键方法：**
```python
@filter.event_message_type(filter.EventMessageType.ALL)
async def on_message(self, event: AstrMessageEvent):
    """监听所有消息，匹配关键词触发"""

@filter.command("开启小作文功能")
async def enable_feature(self, event: AstrMessageEvent):
    """管理员开启当前会话功能"""

@filter.command("关闭小作文功能")
async def disable_feature(self, event: AstrMessageEvent):
    """管理员关闭当前会话功能"""

@filter.command("小作文模型测试")
async def test_model(self, event: AstrMessageEvent):
    """管理员测试模型服务"""
```

**关键词匹配规则：**
- 精确匹配："小作文"、"我的百分之一"
- 前缀匹配："小作文 "、"我的百分之一 "（带空格，后面跟游戏名）

### 6.2 游戏选择模块

**职责：**
- 从配置中读取预设游戏列表
- 提供随机选择功能

**接口：**
```python
import random

def get_random_game(preset_games: list[str]) -> str:
    """
    从预设列表中随机选择一个游戏名称
    如果列表为空，返回默认值"原神"
    """
    if not preset_games:
        return "原神"
    return random.choice(preset_games)
```

**配置示例：**
```json
"preset_games": ["原神", "星露谷物语", "艾尔登法环", "塞尔达传说：王国之泪", "博德之门3"]
```

**使用方式：**
- 用户发送"小作文"或"我的百分之一"时，调用 `get_random_game()` 随机选择游戏
- 用户发送"小作文 XX"或"我的百分之一 XX"时，直接使用用户指定的游戏名

### 6.3 Prompt 组装模块 (`prompt.py`)

**职责：**
- 组装与原项目完全一致的 Prompt
- 游戏名**完全不经过 AI 处理**，由程序直接拼装
- 支持联网搜索结果和用户手动填写字段

**核心原则：**
- 游戏名始终使用用户输入值，不交给 AI 改写
- AI 只负责生成正文内容（发售平台、游玩时间、推荐人群、个人故事）

**Prompt 结构（完全复现原项目 `buildMainPrompt`）：**

```python
def build_main_prompt(game_name: str, search_summary: str) -> str:
    """
    组装主 Prompt，与原项目逻辑完全一致
    """
    search_section = (
        f"联网搜索到的游戏信息摘要：\n{search_summary}"
        if search_summary
        else "未搜索到该游戏的详细信息，请根据你对该游戏的了解自然生成。"
    )
    
    # 无用户手动填写字段，全部走自动生成
    auto_fields = ["发售平台", "游玩时间", "推荐人群", "个人故事/推荐理由"]
    auto_lines = "\n".join(
        f"{label}：用户未填写，请根据游戏类型和帖子语境自然生成，不要过于具体。"
        for label in auto_fields
    )
    
    return f"""你是一个熟悉游戏社区发帖风格的中文写作助手。请根据玩家输入的游戏名称"{game_name}"生成一篇游戏推荐帖的正文内容。

【重要约束】
- 只把"{game_name}"当作游戏名称，不要使用活动标题格式或活动名称作为搜索、理解、生成的游戏关键词
- 不要输出帖子标题
- 不要输出"游戏名称："这一行，这一行会由程序固定填入玩家输入的游戏名称
- 不要改写、翻译、扩写或纠正玩家输入的游戏名称

【帖子结构】（按顺序写，不要输出段落标签，直接写内容）：
1. 先列出发售平台、游玩时间、推荐人群
2. 然后写游戏介绍和个人故事/推荐理由。这是最重要的部分，请务必写一段有真实感的个人体验，像真实玩家在论坛聊天分享。

【写作要求】
- 语气像真实玩家在论坛发帖分享，不要像广告或 AI 生成
- 个人故事要有一点细节感，但不能编造过于夸张的经历
- 不直接复制搜索信息，用自己的话表达
- 不要输出"标题：""正文："这类标记，直接输出帖子内容即可
- 不要写活动说明和玩家许愿信息，这些部分会另外处理
- 正文中提到游戏时，只能使用"{game_name}"这个名称

{search_section}

需要自动生成的字段：
{auto_lines}

请直接输出从"发售平台："开始的正文内容。"""
```

### 6.4 AI 响应后处理模块 (`post_process.py`)

**职责：**
- 解析 AI 返回内容
- 清洗 AI 可能误输出的标题、游戏名称行等
- 拼装最终帖子内容

**与原项目完全一致的后处理链：**

```python
import re

def parse_ai_response(text: str) -> dict:
    """
    解析 AI 返回内容，尝试提取标题
    与原项目 aiService.js 的 parseAIResponse 逻辑一致
    """
    # 尝试匹配【我的百分之一】+【...】格式的标题
    title_match = re.match(r'^【我的百分之一】\+【(.+?)】', text)
    title = title_match.group(1).strip() if title_match else ''
    
    body = text[title_match.end():].strip() if title_match else text.strip()
    
    # 去除开头的"正文："前缀
    body = re.sub(r'^正文：\s*', '', body).strip()
    
    return {"title": title, "content": body}


def strip_generated_title_and_game_name(text: str) -> str:
    """
    清洗 AI 误输出的标题、游戏名称行等
    与原项目 promptService.js 的 stripGeneratedTitleAndGameName 逻辑完全一致
    """
    result = (text or '')
        .replace(r'^标题：.*$', '', flags=re.MULTILINE)  # 删除"标题：xxx"
        .replace(r'^【我的百分之一】+【.*】.*$', '', flags=re.MULTILINE)  # 删除活动标题格式
        .replace(r'^游戏名称：.*$', '', flags=re.MULTILINE)  # 删除"游戏名称：xxx"
        .replace(r'^正文：\s*', '', flags=re.MULTILINE)  # 删除"正文："
        .strip()
    return result


def build_post_content(game_name: str, ai_body: str) -> str:
    """
    拼装最终帖子内容
    与原项目 promptService.js 的 buildPostContent 逻辑一致
    
    最终格式：
    游戏名称：<用户输入的游戏名>
    发售平台：<AI生成>
    游玩时间：<AI生成>
    推荐人群：<AI生成>
    
    <AI生成的正文>
    """
    body = strip_generated_title_and_game_name(ai_body)
    return f"游戏名称：{game_name}\n{body}" if body else f"游戏名称：{game_name}"


def build_final_message(game_name: str, ai_body: str) -> str:
    """
    拼装最终发送给用户的消息
    包含活动说明链接 + 帖子内容
    
    最终格式：
    什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390
    
    游戏名称：<用户输入的游戏名>
    发售平台：<AI生成>
    游玩时间：<AI生成>
    推荐人群：<AI生成>
    
    <AI生成的正文>
    """
    ACTIVITY_INTRO = "什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390"
    post_content = build_post_content(game_name, ai_body)
    return f"{ACTIVITY_INTRO}\n\n{post_content}"
```

**关键设计点：**
1. 游戏名完全由程序拼装，不经过 AI
2. AI 返回内容经过 `strip_generated_title_and_game_name` 清洗
3. 最终格式与原项目完全一致

### 6.4 AI 调用模块 (`ai_client.py`)

**职责：**
- 封装 OpenAI 格式的 API 调用
- 处理请求超时、错误重试
- 提供 Mock 模式（未配置 API Key 时）

**接口：**
```python
class AIClient:
    async def generate(self, prompt: str) -> str:
        """调用 AI 生成内容"""
        
    async def test_connection(self) -> bool:
        """测试 API 连接是否正常"""
```

### 6.5 联网搜索模块 (`search_service.py`)

**职责：**
- 联网搜索游戏公开资料（简介、平台、评价等）
- 支持多源 fallback：Bing → 百度
- 解析搜索结果，提取关键信息

**接口：**
```python
class SearchService:
    async def search_game_info(self, game_name: str) -> dict:
        """
        搜索游戏信息
        返回：{
            "status": "success" | "partial" | "failed" | "disabled",
            "summary": "游戏简介...",
            "platforms": "PC / PS5 / Switch",
            "reviews": "评价摘要..."
        }
        """
```

**搜索策略：**
- 使用 httpx 异步请求
- 自动 User-Agent 轮换
- 单个搜索源超时 8 秒
- 从 HTML 中提取 meta description 和段落文本

### 6.6 频率限制模块 (`rate_limiter.py`)

**职责：**
- 基于 QQ 号的**全局频率限制**（不区分群聊/私聊）
- 支持滑动窗口和每日上限
- 管理员不受限制

**核心原则：**
- 限制基于 QQ 号，与会话类型无关
- 同一用户在任何群聊或私聊中共享限制计数
- 示例：用户 12345 设置每小时 5 次，每天 10 次
  - 在群聊 A 使用 3 次 + 私聊使用 2 次 = 本小时已用 5 次，达到上限
  - 无论从哪个会话发起请求，都共享同一个计数器

**存储结构（KV）：**
```python
# Key: rate_limit:{qq_id}
# Value: {
#     "window_start": timestamp,      # 当前窗口开始时间
#     "window_count": int,            # 当前窗口内请求次数
#     "daily_count": int,             # 今日请求次数
#     "daily_date": "YYYY-MM-DD"      # 今日日期（北京时间）
# }
```

**限制逻辑：**
1. 检查每日上限（按北京时间自然日，跨日重置）
2. 检查滑动窗口内请求次数（窗口滑动，非固定）
3. 管理员不受限制
4. `/小作文模型测试` 和 `/小作文搜索测试` 不受限制

### 6.7 黑名单模块 (`blacklist.py`)

**职责：**
- 管理功能开关的黑名单
- 提供查询、添加、移除功能

**存储结构（KV）：**
```python
# Key: blacklist
# Value: {
#     "groups": ["group_id_1", "group_id_2"],
#     "privates": ["user_id_1", "user_id_2"]
# }
```

**会话 ID 规则：**
- 群聊：`group:{group_id}`
- 私聊：`private:{user_id}`

### 6.8 管理页面模块 (`pages/`)

**职责：**
- 提供可视化的黑名单管理界面
- 通过 AstrBot 插件管理页面访问

**功能：**
- 显示当前黑名单列表（群聊、私聊分类显示）
- 支持添加新的黑名单项（输入群号或用户QQ号）
- 支持移除黑名单项
- 实时更新，操作后立即生效

**页面结构：**
```html
<div class="blacklist-manager">
  <h2>黑名单管理</h2>
  
  <div class="section">
    <h3>群聊黑名单</h3>
    <div class="add-form">
      <input type="text" placeholder="输入群号" />
      <button>添加</button>
    </div>
    <ul class="blacklist-list">
      <!-- 动态渲染群聊黑名单 -->
      <li>
        <span>群号: 123456</span>
        <button>移除</button>
      </li>
    </ul>
  </div>
  
  <div class="section">
    <h3>私聊黑名单</h3>
    <div class="add-form">
      <input type="text" placeholder="输入QQ号" />
      <button>添加</button>
    </div>
    <ul class="blacklist-list">
      <!-- 动态渲染私聊黑名单 -->
    </ul>
  </div>
</div>
```

**后端 API：**
```python
# 注册 Web API
context.register_web_api(
    "/astrbot_plugin_onepercent_generator/blacklist/get",
    self.get_blacklist,
    ["GET"],
    "获取黑名单列表"
)

context.register_web_api(
    "/astrbot_plugin_onepercent_generator/blacklist/add",
    self.add_to_blacklist,
    ["POST"],
    "添加黑名单"
)

context.register_web_api(
    "/astrbot_plugin_onepercent_generator/blacklist/remove",
    self.remove_from_blacklist,
    ["POST"],
    "移除黑名单"
)
```

---

## 7. 配置设计

### 7.1 配置 Schema (`_conf_schema.json`)

```json
{
  "admin_qqs": {
    "description": "管理员QQ号列表",
    "type": "list",
    "hint": "输入管理员的QQ号，支持多个",
    "default": []
  },
  "ai_config": {
    "description": "AI 模型配置",
    "type": "object",
    "items": {
      "base_url": {
        "description": "API Base URL",
        "type": "string",
        "hint": "例如：https://api.deepseek.com/v1"
      },
      "api_key": {
        "description": "API Key",
        "type": "string",
        "hint": "输入你的 API Key",
        "obvious_hint": true
      },
      "model": {
        "description": "模型名称",
        "type": "string",
        "default": "deepseek-chat",
        "hint": "例如：deepseek-chat、gpt-3.5-turbo"
      }
    }
  },
  "rate_limit": {
    "description": "频率限制配置",
    "type": "object",
    "items": {
      "window_minutes": {
        "description": "限制时间窗口（分钟）",
        "type": "int",
        "default": 10
      },
      "max_requests_per_window": {
        "description": "窗口内最大请求次数（基于QQ号全局限制）",
        "type": "int",
        "default": 3
      },
      "daily_max": {
        "description": "每日最大请求次数（基于QQ号全局限制）",
        "type": "int",
        "default": 20
      }
    }
  },
  "preset_games": {
    "description": "预设游戏列表",
    "type": "list",
    "hint": "输入游戏名称，支持多个（每行一个）",
    "default": ["原神", "星露谷物语", "艾尔登法环", "塞尔达传说：王国之泪", "博德之门3"]
  },
  "search_config": {
    "description": "联网搜索配置",
    "type": "object",
    "items": {
      "enabled": {
        "description": "是否启用联网搜索",
        "type": "bool",
        "default": true
      },
      "timeout_ms": {
        "description": "搜索超时时间（毫秒）",
        "type": "int",
        "default": 8000
      }
    }
  },
  "default_enabled": {
    "description": "默认是否启用（新群聊/私聊）",
    "type": "bool",
    "default": true
  }
}
```

---

## 8. 数据设计

### 8.1 KV 存储键值

| Key | 说明 | 数据结构 |
|-----|------|----------|
| `blacklist` | 功能黑名单 | `{"groups": [...], "privates": [...]}` |
| `rate_limit:{qq_id}` | 用户频率限制记录 | `{"window_start": ts, "window_count": n, "daily_count": n, "daily_date": "YYYY-MM-DD"}` |

### 8.2 配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `admin_qqs` | list | 管理员 QQ 号列表 |
| `ai_config.base_url` | string | AI API 地址 |
| `ai_config.api_key` | string | AI API 密钥 |
| `ai_config.model` | string | 模型名称 |
| `preset_games` | list | 预设游戏名称列表 |
| `search_config.enabled` | bool | 是否启用联网搜索 |
| `search_config.timeout_ms` | int | 搜索超时时间（毫秒） |
| `rate_limit.window_minutes` | int | 限制窗口（分钟） |
| `rate_limit.max_requests_per_window` | int | 窗口内最大次数（QQ号全局限制） |
| `rate_limit.daily_max` | int | 每日最大次数（QQ号全局限制） |
| `default_enabled` | bool | 默认启用状态 |

---

## 9. 消息交互设计

### 9.1 用户触发生成

**场景 1：随机游戏**
```
用户：小作文
机器人：正在为你随机选择游戏并生成小作文...
```

**场景 2：指定游戏**
```
用户：小作文 原神
机器人：正在为《原神》生成小作文...
```

**最终输出示例（与原项目格式完全一致）：**
```
什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390

游戏名称：原神
发售平台：PC / PS4 / PS5 / iOS / Android
游玩时间：断断续续玩了一年多
推荐人群：喜欢开放世界探索和二次元风格的玩家

原神是一款开放世界冒险游戏，你将扮演"旅行者"在提瓦特大陆上展开冒险。游戏的美术风格非常独特，每个地区都有不同的文化氛围，从蒙德的欧式风情到璃月的中式韵味，再到稻妻的日式美学，每一处都让人流连忘返。

我是开服玩家，最开始被游戏的美术和音乐吸引，后来沉迷于各种角色的故事线。记得第一次抽到五星角色时的兴奋，还有和朋友一起刷副本的快乐。虽然有时候会吐槽体力不够用，但每次新版本更新都会忍不住回归探索。

如果你喜欢探索、收集和剧情，原神绝对值得一试。
```

**说明：**
- 第一行：活动说明链接
- `游戏名称：xxx`：程序直接拼装，不经过AI
- `发售平台/游玩时间/推荐人群`：AI生成，直接输出
- 正文内容：AI生成

### 9.2 管理员指令

**开启功能：**
```
管理员：/开启小作文功能
机器人：✅ 已为当前会话开启小作文功能
```

**关闭功能：**
```
管理员：/关闭小作文功能
机器人：🚫 已为当前会话关闭小作文功能
```

**测试模型：**
```
管理员：/小作文模型测试
机器人：正在测试模型连接...
机器人：✅ 模型服务正常，当前模型：deepseek-chat
```
或
```
机器人：❌ 模型连接失败，请检查配置
```

**测试搜索：**
```
管理员：/小作文搜索测试 原神
机器人：正在测试联网搜索...
机器人：✅ 搜索成功
游戏简介：《原神》是由米哈游开发的一款开放世界冒险游戏...
平台：PC / PS4 / PS5 / iOS / Android
```

### 9.3 错误提示

**频率限制：**
```
机器人：⏳ 请求过于频繁，请稍后再试（每 {window} 分钟最多 {max} 次，今日已用 {daily_used}/{daily_max} 次）
```
说明：频率限制基于 QQ 号全局生效，不区分群聊或私聊

**功能关闭：**
```
机器人：🚫 当前会话的小作文功能已关闭
```

**非管理员操作：**
```
机器人：⚠️ 该指令仅管理员可用
```

**生成失败：**
```
机器人：❌ 生成失败，请稍后重试
```

---

## 10. AI 调用设计

### 10.1 请求格式（与原项目完全一致）

```python
POST {base_url}/chat/completions
Headers:
    Authorization: Bearer {api_key}
    Content-Type: application/json

{
    "model": "{model}",
    "messages": [
        {"role": "system", "content": "你是一个中文写作助手，请严格按照用户要求的格式输出。"},
        {"role": "user", "content": "{prompt}"}
    ],
    "temperature": 0.8,
    "max_tokens": 2048
}
```

**超时设置：** 120 秒（与原项目一致）

### 10.2 System Prompt（与原项目完全一致）

```python
SYSTEM_PROMPT = "你是一个中文写作助手，请严格按照用户要求的格式输出。"
```

### 10.3 User Prompt（由 `build_main_prompt` 生成）

见 6.3 节，完全复现原项目的 `buildMainPrompt` 逻辑。

### 10.4 Mock 模式

当未配置 `api_key` 或值为空时，返回预设的示例内容，方便测试。

**Mock 响应示例：**
```python
MOCK_RESPONSE = """发售平台：PC / Switch / iOS / Android
游玩时间：断断续续玩了两年多
推荐人群：喜欢种田养老、温馨治愈风格的玩家

星露谷物语是一款像素风格的模拟经营游戏，你继承了爷爷留下的农场，从零开始打理一切。种地、钓鱼、挖矿、社交，每天都有做不完的事情，但节奏却出奇地让人放松。

我是被朋友安利入坑的，一开始觉得画面有点简陋，结果一玩就停不下来。最让我印象深刻的是和村民们的互动，每个人都有自己的故事，慢慢解锁他们的剧情线特别有成就感。冬天的时候窝在农场里整理仓库，听着背景音乐，感觉整个世界都安静下来了。

如果你想找个能玩很久又不会太累的游戏，星露谷物语绝对值得一试。"""
```

---

## 11. 安全与限制

### 11.1 权限控制
- 管理员指令仅允许配置的 QQ 号执行
- 默认所有群聊/私聊启用功能
- 管理员可针对特定会话关闭功能

### 11.2 频率限制
- 按 QQ 号限制，防止滥用
- 滑动窗口 + 每日上限双重限制
- 管理员不受限制
- `/小作文模型测试` 不受限制

### 11.3 敏感信息保护
- API Key 仅存储在 AstrBot 配置中，不暴露给用户
- `/小作文模型测试` 仅返回连接状态，不泄露配置详情

---

## 12. 开发阶段建议

### 阶段 1: 项目初始化
- 创建插件目录结构
- 编写 `metadata.yaml` 和 `_conf_schema.json`
- 实现基础消息监听和关键词匹配
- 提交 Git

### 阶段 2: 核心功能实现
- 实现预设游戏列表和随机选择
- 实现联网搜索模块（Bing/百度）
- 实现 Prompt 组装模块（集成搜索结果）
- 实现 AI 调用模块（含 Mock 模式）
- 实现基础生成功能
- 提交 Git

### 阶段 3: 管理功能实现
- 实现管理员权限检查
- 实现黑名单管理（开启/关闭功能）
- 实现黑名单管理页面（WebUI）
- 实现频率限制模块
- 实现 `/小作文模型测试` 指令
- 实现 `/小作文搜索测试` 指令
- 提交 Git

### 阶段 4: 测试与优化
- 本地测试所有功能
- 测试群聊和私聊场景
- 测试边界情况（空输入、超长输入等）
- 测试联网搜索功能
- 测试黑名单管理页面
- 完善错误提示
- 编写 README 文档
- 提交 Git

---

## 13. 给开发 Agent 的提示词

请根据本 design.md 实现 AstrBot 插件。要求：

1. **严格遵循 AstrBot 插件规范**：
   - 使用 `@register` 装饰器注册插件
   - 使用 `@filter.command` 注册指令
   - 使用 `@filter.event_message_type` 监听消息
   - 使用 AstrBot 提供的 KV 存储进行数据持久化

2. **使用异步编程**：
   - 使用 `httpx` 而非 `requests` 进行 HTTP 请求
   - 所有 IO 操作使用 `async/await`

3. **保持代码简洁**：
   - 每个模块职责单一
   - 适当添加注释
   - 使用 AstrBot 的 logger 进行日志记录

4. **优先完成功能闭环**：
   - 先实现核心生成流程
   - 再实现管理功能
   - 最后处理边界情况

5. **每完成一个阶段进行 Git 提交**：
   - 使用中文 Conventional Commit 风格
   - 例如：`feature: 实现基础生成功能`

6. **遇到不明确的地方**：
   - 优先选择简单、稳定的方案
   - 参考 AstrBot 官方插件示例
   - 如有疑问，在代码中添加 TODO 注释

---


---

## 附录：原项目核心逻辑参考

### 原项目生成流程

```
用户输入游戏名 → 参数校验 → 频率限制 → 联网搜索 → 组装Prompt → AI调用 → 后处理 → 返回
```

### 关键设计点（必须完全复现）

1. **游戏名不经过 AI 处理**
   - Prompt 中明确约束："不要输出'游戏名称：'这一行，这一行会由程序固定填入玩家输入的游戏名称"
   - Prompt 中明确约束："不要改写、翻译、扩写或纠正玩家输入的游戏名称"
   - 最终帖子中 `游戏名称：xxx` 由程序直接拼装

2. **AI 只负责生成正文内容**
   - 发售平台、游玩时间、推荐人群、个人故事/推荐理由

3. **System Prompt 固定为**
   ```
   你是一个中文写作助手，请严格按照用户要求的格式输出。
   ```

4. **AI 调用参数**
   - temperature: 0.8
   - max_tokens: 2048
   - timeout: 120000ms

5. **后处理链**
   - `parseAIResponse()`：尝试提取标题，去除"正文："前缀
   - `stripGeneratedTitleAndGameName()`：清洗 AI 误输出的标题、游戏名称行
   - `buildPostContent()`：拼装 `游戏名称：{gameName}\n{body}`

6. **最终帖子格式（必须完全一致）**

```
什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390

游戏名称：<用户输入的游戏名>
发售平台：<AI生成 或 用户填写>
游玩时间：<AI生成 或 用户填写>
推荐人群：<AI生成 或 用户填写>

<AI生成的游戏介绍、个人故事、推荐理由正文>
```

**格式要点：**
- 第一行：活动说明链接
- 空一行
- `游戏名称：xxx`（程序拼装，不经过AI）
- `发售平台：xxx`（AI生成，直接输出）
- `游玩时间：xxx`（AI生成，直接输出）
- `推荐人群：xxx`（AI生成，直接输出）
- 空一行
- 正文内容（AI生成）

### 插件版本流程

```
用户触发关键词 → 选择预设游戏/用户指定 → 联网搜索 → 组装Prompt（完全复现） → AI调用 → 后处理（完全复现） → 返回
```

与原项目的区别：
- 无用户手动填写字段，全部走自动生成
