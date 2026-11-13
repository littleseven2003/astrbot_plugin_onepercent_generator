"""
百分之一小作文生成器 - AstrBot 插件主入口
在 QQ 聊天中通过关键词触发，自动生成符合 TapTap《百分之一》活动格式的游戏推荐帖
"""

import random

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ai_client import AIClient, AIClientError
from .post_process import build_final_message
from .prompt import (
    GAME_REQUIRED_MESSAGE,
    build_main_prompt,
    choose_writing_variant,
    needs_explicit_game,
    parse_writing_request,
)
from .rate_limiter import RateLimiter
from .search_service import (
    BACKEND_LABELS,
    SearchService,
    build_search_summary,
    format_source_failures,
    prepare_search_digest,
)
from .whitelist import is_session_allowed


@register(
    "astrbot_plugin_onepercent_generator",
    "littleseven2003",
    "百分之一小作文生成器",
    "在QQ聊天中通过关键词触发，自动生成符合TapTap《百分之一》活动格式的游戏推荐帖",
    "0.4.0",
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
            api_format=ai_cfg.get("api_format", "chat_completions"),
            send_temperature=ai_cfg.get("send_temperature", True),
        )

        # 搜索配置
        search_cfg = self.config.get("search_config", {})
        self.search_service = SearchService(
            enabled=search_cfg.get("enabled", True),
            timeout_ms=search_cfg.get("timeout_ms", 8000),
            per_backend_timeout_ms=search_cfg.get(
                "per_backend_timeout_ms", 0
            ),
            summary_input_max_chars=search_cfg.get(
                "summary_input_max_chars", 6000
            ),
            result_count=search_cfg.get("result_count", 3),
            mode=search_cfg.get("mode"),
            fallback_enabled=search_cfg.get("fallback_enabled", False),
            api_key=search_cfg.get("api_key", ""),
            api_base_url=search_cfg.get(
                "api_base_url", "https://api.tavily.com"
            ),
            api_provider=search_cfg.get("api_provider", "tavily"),
            astrbot_context=self.context,
        )
        self.search_summarize_enabled = search_cfg.get("summarize_enabled", True)
        self.search_summarize_max_chars = search_cfg.get("summarize_max_chars", 150)
        try:
            self.search_summary_input_max_chars = max(
                200, int(search_cfg.get("summary_input_max_chars", 6000))
            )
        except (TypeError, ValueError):
            self.search_summary_input_max_chars = 6000

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

        # 白名单
        self.whitelist_groups = self.config.get("whitelist_groups", [])
        self.whitelist_privates = self.config.get("whitelist_privates", [])

        # 是否发送生成统计信息
        self.show_generation_stats = self.config.get("show_generation_stats", True)

        logger.info("[小作文生成器] 插件已加载")

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
        """兼容旧调用：只返回是否匹配和游戏名。"""
        matched, game_name, _experience = parse_writing_request(text)
        return matched, game_name

    # ========================================================================
    # 消息监听：关键词触发生成
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，匹配关键词触发"""
        text = event.message_str.strip()
        matched, game_name, experience = parse_writing_request(text)

        if not matched:
            return

        # 管理员不受白名单限制；非白名单用户沿用原有拒绝策略。
        sender_id = event.get_sender_id()
        if sender_id not in self.admin_qqs:
            session_id, session_type = self._get_session_info(event)
            if not is_session_allowed(
                session_id, session_type,
                self.whitelist_groups, self.whitelist_privates,
            ):
                yield event.plain_result("🚫 当前会话不在白名单中，无法使用小作文功能")
                event.stop_event()
                return

        # 白名单通过后再检查缺游戏名；该提示不消耗频率额度，也不搜索。
        if needs_explicit_game(game_name, experience):
            yield event.plain_result(GAME_REQUIRED_MESSAGE)
            event.stop_event()
            return

        # 只有实际执行生成的非管理员才消耗频率额度。
        if sender_id not in self.admin_qqs:
            rate_check = await self.rate_limiter.check_and_record(sender_id, self)
            if not rate_check["allowed"]:
                yield event.plain_result(rate_check["message"])
                event.stop_event()
                return

        # 确定游戏名称
        is_random = not game_name
        if is_random:
            game_name = self._get_random_game()
            yield event.plain_result(f"正在为你随机选择游戏《{game_name}》并生成小作文...")
        else:
            yield event.plain_result(f"正在为《{game_name}》生成小作文...")

        # 联网搜索（多后端，统一总预算）
        search_result = await self.search_service.search_game_info(
            game_name, event=event
        )
        search_results = search_result.get("results") or []
        raw_summary = build_search_summary(
            search_results,
            self.search_summary_input_max_chars,
        )
        if not raw_summary:
            raw_summary = prepare_search_digest(
                search_result.get("summary", ""),
                "",
                self.search_summary_input_max_chars,
            )
        logger.info(
            "[小作文生成器] 搜索状态: %s, 后端: %s, 有效结果: %s, 游戏: %s",
            search_result.get("status"),
            search_result.get("effective_backend") or "none",
            len(search_result.get("results", [])),
            game_name,
        )

        # AI 汇总搜索结果；失败/关闭时回退原始摘要，并统一执行字数预算
        ai_digest = ""
        if raw_summary and self.search_summarize_enabled:
            try:
                digest_result = await self.ai_client.summarize_search(
                    game_name, raw_summary, self.search_summarize_max_chars
                )
                ai_digest = str(digest_result.get("content") or "")
                logger.info(
                    "[小作文生成器] 搜索汇总完成: %s 字符",
                    len(ai_digest),
                )
            except AIClientError as e:
                logger.warning(
                    "[小作文生成器] 搜索汇总失败，回退结构化摘要: %s",
                    e,
                )
        final_budget = self.search_summarize_max_chars
        if (ai_digest or "").strip():
            search_digest = prepare_search_digest(
                "", ai_digest, final_budget
            )
        else:
            # AI 汇总关闭/失败/返回空时，从结构化结果重新构建平衡摘要；
            # 不能简单截断已拼接字符串，否则往往只剩第一条。
            search_digest = build_search_summary(
                search_results, final_budget
            )
            if not search_digest:
                search_digest = prepare_search_digest(
                    raw_summary, "", final_budget
                )

        # 组装 Prompt：使用已受预算约束的搜索汇总；真实经历由用户
        # 显式提供，写作风格随机选择，但不改变游戏名或事实边界。
        prompt = build_main_prompt(
            game_name,
            search_digest,
            experience=experience,
            variant=choose_writing_variant(random),
        )

        # 调用 AI 生成小作文
        try:
            ai_result = await self.ai_client.generate(prompt)
        except AIClientError as e:
            logger.error(f"[小作文生成器] AI 调用失败: {e}")
            yield event.plain_result(e.user_message)
            event.stop_event()
            return

        # 后处理并返回小作文
        ai_content = ai_result["content"]
        final_message = build_final_message(game_name, ai_content)
        yield event.plain_result(final_message)

        # 组装生成信息
        if self.show_generation_stats:
            sender_name = event.get_sender_name() or sender_id
            game_mode = f"随机-{game_name}" if is_random else game_name
            duration_s = f"{ai_result['duration_ms'] / 1000:.1f}秒"
            tokens = ai_result["token_usage"]
            search_provider = search_result.get("provider", "未使用")
            search_duration = f"{search_result.get('duration_ms', 0) / 1000:.1f}秒"
            search_backend = search_result.get("effective_backend", "")
            search_backend_label = BACKEND_LABELS.get(
                search_backend, search_backend or "无"
            )
            search_degraded = "是" if search_result.get("degraded") else "否"
            search_valid_count = len(search_result.get("results", []))
            search_failure_reason = search_result.get("failure_reason", "")
            search_partial = "是" if search_result.get("partial") else "否"
            search_partial_failures = format_source_failures(
                search_result.get("partial_failures", [])
            )
            search_cleanup = (
                "未完成"
                if search_result.get("cleanup_incomplete")
                else "完成"
            )

            # 用户使用情况
            usage = await self.rate_limiter.get_usage_status(sender_id, self)
            usage_str = (
                f"窗口：{usage['window_used']}/{usage['window_max']}（{usage['window_minutes']}分钟），"
                f"今日：{usage['daily_used']}/{usage['daily_max']}"
            )

            info_lines = [
                "📊 本次生成信息",
                f"调用者：{sender_name}（{sender_id}）",
                f"指定游戏：{game_mode}",
                f"生成模型：{ai_result['model']}",
                f"API格式：{ai_result['api_format']}",
                f"生成时间：{duration_s}",
                f"Token消耗：{tokens['total_tokens']}（输入{tokens['prompt_tokens']} + 输出{tokens['completion_tokens']}）",
                f"搜索服务：{search_provider}（{search_duration}）",
                f"搜索后端：{search_backend_label}",
                f"搜索降级：{search_degraded}",
                f"有效结果：{search_valid_count}",
                f"搜索部分可用：{search_partial}",
                f"搜索清理：{search_cleanup}",
                *(
                    [f"部分失败来源：{search_partial_failures}"]
                    if search_partial_failures
                    else []
                ),
                f"搜索汇总：{search_digest if search_digest else '无'}",
                *(
                    [f"搜索失败原因：{search_failure_reason}"]
                    if search_result.get("status") == "failed"
                    else []
                ),
                f"用户限制：{usage_str}",
            ]
            yield event.plain_result("\n".join(info_lines))
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
        result = await self.search_service.test_search(game_name, event=event)

        prefix = "✅" if result["success"] else "❌"
        lines = [f"{prefix} {result['message']}"]
        if result["summary"]:
            summary = result["summary"]
            preview = summary[:300] + ("..." if len(summary) > 300 else "")
            lines.append(f"\n搜索结果摘要：\n{preview}")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ========================================================================
    # 生命周期
    # ========================================================================

    async def terminate(self):
        """插件卸载时清理资源"""
        await self.ai_client.close()
        await self.search_service.close()
        logger.info("[小作文生成器] 插件已卸载")
