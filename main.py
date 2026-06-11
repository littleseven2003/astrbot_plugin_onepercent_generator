"""
百分之一小作文生成器 - AstrBot 插件主入口
在 QQ 聊天中通过关键词触发，自动生成符合 TapTap《百分之一》活动格式的游戏推荐帖
"""

import random

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ai_client import AIClient, AIClientError
from .blacklist import (
    add_to_blacklist as bl_add,
    disable_session,
    enable_session,
    get_blacklist,
    is_enabled,
    remove_from_blacklist as bl_remove,
)
from .post_process import build_final_message
from .prompt import build_main_prompt
from .rate_limiter import RateLimiter
from .search_service import SearchService


@register(
    "astrbot_plugin_onepercent_generator",
    "littleseven2003",
    "百分之一小作文生成器",
    "在QQ聊天中通过关键词触发，自动生成符合TapTap《百分之一》活动格式的游戏推荐帖",
    "0.1.4",
)
class OnePercentGenerator(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # AI 配置
        ai_cfg = self.config.get("ai_config", {})
        self.ai_client = AIClient(
            base_url=ai_cfg.get("base_url", ""),
            api_key=ai_cfg.get("api_key", ""),
            model=ai_cfg.get("model", "deepseek-chat"),
        )

        # 搜索配置
        search_cfg = self.config.get("search_config", {})
        self.search_service = SearchService(
            enabled=search_cfg.get("enabled", True),
            timeout_ms=search_cfg.get("timeout_ms", 8000),
        )

        # 频率限制配置
        rate_cfg = self.config.get("rate_limit", {})
        self.rate_limiter = RateLimiter(
            window_minutes=rate_cfg.get("window_minutes", 10),
            max_requests=rate_cfg.get("max_requests_per_window", 3),
            daily_max=rate_cfg.get("daily_max", 20),
        )

        # 管理员列表
        self.admin_qqs = self.config.get("admin_qqs", [])

        # 预设游戏列表
        self.preset_games = self.config.get("preset_games", [
            "原神", "星露谷物语", "艾尔登法环", "塞尔达传说：王国之泪", "博德之门3",
        ])

        # 默认启用状态
        self.default_enabled = self.config.get("default_enabled", True)

        logger.info("百分之一小作文生成器插件已加载")

        # 注册 Web API（用于黑名单管理页面）
        self._register_web_apis()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否为管理员"""
        sender_id = event.get_sender_id()
        return sender_id in self.admin_qqs

    def _get_session_info(self, event: AstrMessageEvent) -> tuple[str, str]:
        """
        获取会话信息

        Returns:
            (session_id, session_type) - session_type 为 "group" 或 "private"
        """
        group_id = event.get_group_id()
        if group_id:
            return group_id, "group"
        return event.get_sender_id(), "private"

    def _get_random_game(self) -> str:
        """从预设列表中随机选择游戏"""
        if not self.preset_games:
            return "原神"
        return random.choice(self.preset_games)

    def _match_keyword(self, text: str) -> tuple[bool, str]:
        """
        匹配关键词

        Returns:
            (matched, game_name)
            - matched: 是否匹配到关键词
            - game_name: 游戏名称（空字符串表示随机选择）
        """
        text = text.strip()

        # 精确匹配 → 随机游戏
        if text in ("小作文", "我的百分之一"):
            return True, ""

        # 前缀匹配 → 指定游戏
        for prefix in ("小作文 ", "我的百分之一 "):
            if text.startswith(prefix):
                game_name = text[len(prefix):].strip()
                if game_name:
                    return True, game_name

        return False, ""

    # ========================================================================
    # 消息监听：关键词触发生成
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，匹配关键词触发"""
        text = event.message_str.strip()
        matched, game_name = self._match_keyword(text)

        if not matched:
            return

        # 获取会话信息
        session_id, session_type = self._get_session_info(event)

        # 检查功能是否启用
        if not await is_enabled(session_id, session_type, self.default_enabled, self):
            yield event.plain_result("🚫 当前会话的小作文功能已关闭")
            event.stop_event()
            return

        # 检查频率限制（管理员不受限制）
        sender_id = event.get_sender_id()
        if sender_id not in self.admin_qqs:
            rate_check = await self.rate_limiter.check_and_record(sender_id, self)
            if not rate_check["allowed"]:
                yield event.plain_result(rate_check["message"])
                event.stop_event()
                return

        # 确定游戏名称
        if not game_name:
            game_name = self._get_random_game()
            yield event.plain_result(f"正在为你随机选择游戏《{game_name}》并生成小作文...")
        else:
            yield event.plain_result(f"正在为《{game_name}》生成小作文...")

        # 联网搜索
        search_result = await self.search_service.search_game_info(game_name)
        search_summary = search_result.get("summary", "")
        logger.info(f"搜索状态: {search_result['status']}, 游戏: {game_name}")

        # 组装 Prompt
        prompt = build_main_prompt(game_name, search_summary)

        # 调用 AI
        try:
            ai_response = await self.ai_client.generate(prompt)
        except AIClientError as e:
            logger.error(f"[小作文生成器] AI 调用失败: {e}")
            yield event.plain_result(e.user_message)
            event.stop_event()
            return

        # 后处理并返回
        final_message = build_final_message(game_name, ai_response)
        yield event.plain_result(final_message)
        event.stop_event()

    # ========================================================================
    # 管理员指令：开启/关闭功能
    # ========================================================================

    @filter.command("开启小作文功能")
    async def enable_feature(self, event: AstrMessageEvent):
        """管理员针对当前会话开启小作文功能"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用")
            event.stop_event()
            return

        session_id, session_type = self._get_session_info(event)
        msg = await enable_session(session_id, session_type, self)
        yield event.plain_result(msg)
        event.stop_event()

    @filter.command("关闭小作文功能")
    async def disable_feature(self, event: AstrMessageEvent):
        """管理员针对当前会话关闭小作文功能"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用")
            event.stop_event()
            return

        session_id, session_type = self._get_session_info(event)
        msg = await disable_session(session_id, session_type, self)
        yield event.plain_result(msg)
        event.stop_event()

    # ========================================================================
    # 管理员指令：模型测试
    # ========================================================================

    @filter.command("小作文模型测试")
    async def test_model(self, event: AstrMessageEvent):
        """管理员测试当前模型服务是否可用"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用")
            event.stop_event()
            return

        yield event.plain_result("正在测试模型连接...")
        result = await self.ai_client.test_connection()

        if result["success"]:
            yield event.plain_result(f"✅ {result['message']}")
        else:
            yield event.plain_result(f"❌ {result['message']}")
        event.stop_event()

    # ========================================================================
    # 管理员指令：搜索测试
    # ========================================================================

    @filter.command("小作文搜索测试")
    async def test_search(self, event: AstrMessageEvent, game_name: str = "原神"):
        """管理员测试联网搜索功能"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用")
            event.stop_event()
            return

        yield event.plain_result(f"正在测试联网搜索（游戏：{game_name}）...")
        result = await self.search_service.test_search(game_name)

        if result["success"]:
            summary_preview = result["summary"][:300] + ("..." if len(result["summary"]) > 300 else "")
            yield event.plain_result(
                f"✅ {result['message']}\n\n搜索结果摘要：\n{summary_preview}"
            )
        else:
            yield event.plain_result(f"❌ {result['message']}")
        event.stop_event()

    # ========================================================================
    # Web API：黑名单管理
    # ========================================================================

    def _register_web_apis(self):
        """注册黑名单管理 Web API"""
        try:
            self.context.register_web_api(
                "/astrbot_plugin_onepercent_generator/blacklist/get",
                self._api_get_blacklist,
                ["GET"],
                "获取黑名单列表",
            )
            self.context.register_web_api(
                "/astrbot_plugin_onepercent_generator/blacklist/add",
                self._api_add_blacklist,
                ["POST"],
                "添加黑名单",
            )
            self.context.register_web_api(
                "/astrbot_plugin_onepercent_generator/blacklist/remove",
                self._api_remove_blacklist,
                ["POST"],
                "移除黑名单",
            )
            logger.info("黑名单管理 Web API 注册成功")
        except (AttributeError, TypeError) as e:
            logger.warning(f"Web API 注册不支持，黑名单管理仅可通过指令操作: {e}")

    async def _api_get_blacklist(self, request):
        """GET: 获取黑名单列表"""
        import json
        from aiohttp import web

        bl = await get_blacklist(self)
        return web.json_response(bl)

    async def _api_add_blacklist(self, request):
        """POST: 添加黑名单"""
        from aiohttp import web

        try:
            data = await request.json()
            target_id = str(data.get("target_id", "")).strip()
            target_type = data.get("target_type", "group")

            if not target_id:
                return web.json_response({"message": "请输入有效的ID"}, status=400)

            result = await bl_add(target_id, target_type, self)
            return web.json_response({"message": result})
        except Exception as e:
            return web.json_response({"message": f"操作失败: {e}"}, status=500)

    async def _api_remove_blacklist(self, request):
        """POST: 移除黑名单"""
        from aiohttp import web

        try:
            data = await request.json()
            target_id = str(data.get("target_id", "")).strip()
            target_type = data.get("target_type", "group")

            if not target_id:
                return web.json_response({"message": "请输入有效的ID"}, status=400)

            result = await bl_remove(target_id, target_type, self)
            return web.json_response({"message": result})
        except Exception as e:
            return web.json_response({"message": f"操作失败: {e}"}, status=500)

    # ========================================================================
    # 生命周期
    # ========================================================================

    async def terminate(self):
        """插件卸载时清理资源"""
        await self.ai_client.close()
        await self.search_service.close()
        logger.info("百分之一小作文生成器插件已卸载")
