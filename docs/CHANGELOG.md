# 更新日志

## v0.1.2 - 2026-06-11

### 修复

- 修复 KV 存储调用方式，使用 `Star` 基类的 `get_kv_data` / `put_kv_data` 方法，解决 `AttributeError: 'Context' object has no attribute 'get'`

## v0.1.1 - 2026-06-11

### 修复

- 修复插件模块导入方式，将绝对导入改为相对导入，解决 AstrBot 加载插件时报错 `No module named 'ai_client'`
- 添加 `__init__.py` 确保插件目录作为 Python 包正确加载

## v0.1.0 - 2026-06-11

### 新增

- 初始化项目目录结构与基础配置
- 实现关键词触发生成（"小作文"/"我的百分之一" + 可选游戏名）
- 实现 Prompt 模板组装（完全复现原项目 buildMainPrompt 逻辑）
- 实现 AI API 调用封装（兼容 OpenAI 格式，支持 Mock 模式）
- 实现联网搜索服务（Bing → 百度多源 fallback）
- 实现 AI 响应后处理（清洗、拼装最终帖子）
- 实现频率限制（滑动窗口 + 每日上限，管理员免限制）
- 实现黑名单管理（群聊/私聊独立管理）
- 实现管理员指令（开启/关闭功能、模型测试、搜索测试）
- 实现 WebUI 黑名单管理页面
- 添加插件元数据和配置 Schema
