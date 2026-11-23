"""
百分之一小作文生成器 - AstrBot 插件主入口
在 QQ 聊天中通过关键词触发，自动生成符合 TapTap《百分之一》活动格式的游戏推荐帖
"""

import asyncio
import inspect
import random
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ai_client import AIClientError
from .config_center import ConfigCenter, parse_umo
from .config_center_api import ConfigCenterAPI
from .model_config_api import ModelConfigAPI, _norm_base
from .migration import migrate_legacy_config
from .model_service import ModelService
from .stats_store import StatsStore
from .post_process import build_final_message
from .prompt import (
    GAME_REQUIRED_MESSAGE,
    build_main_prompt,
    choose_writing_variant,
    is_thin_experience,
    needs_explicit_game,
    parse_activity_fields,
    parse_optional_extras,
    parse_writing_request,
)
from .rate_limiter import RateLimiter
from .search_service import (
    BACKEND_LABELS,
    SearchService,
    build_material_block,
    build_search_summary,
    format_source_failures,
    prepare_search_digest,
    search_groups_from_config,
    search_outcome_label,
)

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
        self.last_umo = ""
        # 身份映射切换（配置保存/计数迁移）与普通请求的身份解析/限流检查
        # 必须共用同一把锁；锁顺序固定为 shared -> RateLimiter._lock。
        self.identity_rate_lock = asyncio.Lock()

        # 必须在 ConfigCenter 归一化前执行：AstrBotConfig 已按 schema 补过
        # 默认值，不能再用“新字段不存在”判断旧配置。
        migrate_legacy_config(self.config)

        # 统一模型来源：AstrBot Provider 或手动 OpenAI 兼容配置。
        self.model_service = ModelService(self.context, self.config)
        self.ai_client = self.model_service
        self.model_config_api = ModelConfigAPI(self)

        # 搜索与频率运行时
        self.search_service = self._build_search_service()
        self.rate_limiter = self._build_rate_limiter()

        # 统计、迁移和统一配置中心
        self.stats_store = StatsStore()
        self.config_center = ConfigCenter(self)
        self.preset_games = self.config.get("preset_games", [
            "原神", "星露谷物语", "艾尔登法环", "塞尔达传说：王国之泪", "博德之门3",
        ])
        self.show_generation_stats = self.config.get("show_generation_stats", True)
        self.config_center_api = ConfigCenterAPI(self)
        plugin_name = "astrbot_plugin_onepercent_generator"
        routes = (
            ("state", self.config_center_api.state, ["GET"]),
            ("stats", self.config_center_api.stats, ["GET"]),
            ("save", self.config_center_api.save, ["POST"]),
            ("model/models", self.config_center_api.model_models, ["POST"]),
            ("model/test", self.config_center_api.model_test, ["POST"]),
            ("search/test", self.config_center_api.search_test, ["POST"]),
        )
        for suffix, handler, methods in routes:
            self.context.register_web_api(
                f"/{plugin_name}/config-center/{suffix}",
                handler,
                methods,
                f"配置中心 {suffix}",
            )
        logger.info("[小作文生成器] 插件已加载")

    def _build_search_service(self) -> "SearchService":
        search_cfg = self.config.get("search_config", {})
        self._sync_search_attrs(search_cfg)
        api_base_url = search_cfg.get(
            "api_base_url", "https://api.tavily.com"
        )
        api_key = str(search_cfg.get("api_key") or "").strip()
        key_origin = str(search_cfg.get("api_key_origin") or "").strip()
        if api_key:
            # 配置中心保存 Key 时记录绑定地址。原生配置页可以只改可见的
            # api_base_url；此时绝不能把旧地址的 Key 发往新地址。
            if not key_origin:
                logger.warning(
                    "[小作文生成器] 搜索 API Key 缺少绑定地址，已停止发送；"
                    "请在统一配置中心重新保存 Key"
                )
                api_key = ""
            elif _norm_base(key_origin) != _norm_base(api_base_url):
                logger.warning(
                    "[小作文生成器] 搜索服务地址已变更，旧地址的 API Key "
                    "不会发送；请在统一配置中心为新地址重新保存 Key"
                )
                api_key = ""
        return SearchService(
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
            groups=search_groups_from_config(search_cfg),
            api_key=api_key,
            api_base_url=api_base_url,
            api_provider=search_cfg.get("api_provider", "tavily"),
            astrbot_context=self.context,
        )

    def _sync_search_attrs(self, search_cfg: dict) -> None:
        # 默认素材直传：只调用一次写作模型。AI 汇总保留为高级显式选项。
        self.search_summarize_enabled = bool(
            search_cfg.get("summarize_enabled", False)
        )
        self.search_summarize_max_chars = search_cfg.get(
            "summarize_max_chars", 150
        )
        try:
            self.search_summary_input_max_chars = max(
                200, int(search_cfg.get("summary_input_max_chars", 6000))
            )
        except (TypeError, ValueError):
            self.search_summary_input_max_chars = 6000
        try:
            self.search_material_max_chars = max(
                1000, min(int(search_cfg.get("material_max_chars", 3000)), 6000)
            )
        except (TypeError, ValueError):
            self.search_material_max_chars = 3000
        try:
            self.search_material_max_items = max(
                1, min(int(search_cfg.get("material_max_items", 6)), 10)
            )
        except (TypeError, ValueError):
            self.search_material_max_items = 6
        try:
            self.search_material_snippet_chars = max(
                120,
                min(int(search_cfg.get("material_snippet_chars", 400)), 600),
            )
        except (TypeError, ValueError):
            self.search_material_snippet_chars = 400

    async def reload_search_service(self) -> None:
        old = getattr(self, "search_service", None)
        self.search_service = self._build_search_service()
        if old is not None:
            await old.close()
        self._sync_search_attrs(self.config.get("search_config", {}))

    def _build_rate_limiter(self) -> "RateLimiter":
        rate_cfg = self.config.get("rate_limit", {})
        return RateLimiter(
            window_minutes=rate_cfg.get("window_minutes", 10),
            max_requests=rate_cfg.get("max_requests_per_window", 3),
            daily_max=rate_cfg.get("daily_max", 20),
            timezone_name=rate_cfg.get("reset_timezone", ""),
        )

    def reload_rate_limiter(self) -> None:
        self.rate_limiter = self._build_rate_limiter()

    def _event_identity(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        umo = self._get_umo(event)
        connector, _chat_type = parse_umo(umo)
        return connector, event.get_sender_id(), umo

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """管理员按连接器作用域 + 发送者 UID 判断。"""
        connector, sender_id, _umo = self._event_identity(event)
        return self.config_center.is_admin(connector, sender_id)

    async def _authorize_request(
        self, connector: str, sender_id: str, umo: str,
        game_name: str, experience: str,
    ) -> dict:
        """在同一把锁内完成身份解析、会话授权与频率计数。

        返回 {"kind": "ok", "is_admin": bool, "identity_key": str}，
        或 {"kind": "message", "text": str}。
        """
        async with self.identity_rate_lock:
            # 配置与 KV 没有跨存储事务；持久化日志未重放完之前，
            # 不能用“新映射 + 旧计数”授权，否则已用额度会被重置。
            recovered, recovery_message = (
                await self.config_center_api.ensure_identity_recovered()
            )
            if not recovered:
                return {"kind": "message", "text": recovery_message}
            is_admin = self.config_center.is_admin(connector, sender_id)
            # 管理员不受会话白名单和频率限制；普通用户先拒绝。
            if not is_admin:
                allowed = self.config_center.is_session_allowed(umo)
                migration_pending = self.config_center.migration_required()
                # 旧版 QQ/群号限制无法可靠映射成 UMO 时，会话列表会保持
                # null（sessions_unset）。此时应告诉管理员旧限制仍在，
                # 而不是用泛化的“会话配置无效”盖掉可执行的修复路径。
                if self.config_center.config_error and not (
                    self.config_center.config_error == "sessions_unset"
                    and migration_pending
                ):
                    return {
                        "kind": "message",
                        "text": (
                            "⚠️ 会话配置无效（会话列表尚未设置、类型错误或"
                            "包含无效 UMO），已拒绝本次请求，"
                            "请管理员在配置中心会话页修正。"
                        ),
                    }
                # 明确的会话列表里已有精确命中时继续放行；空列表代表
                # “不限制”，绝不能在旧限制尚未处理时用它绕过旧版暂停。
                sessions = self.config_center.settings.get("sessions")
                unrestricted = isinstance(sessions, list) and not sessions
                if migration_pending and (not allowed or unrestricted):
                    return {
                        "kind": "message",
                        "text": (
                            "⚠️ 检测到旧版 QQ 号/群号白名单限制，旧授权已停用。"
                            "请管理员在统一配置中心的会话页添加明确的 UMO 会话；"
                            "旧版 QQ/群号限制不会自动恢复授权。"
                        ),
                    }
                if not allowed:
                    return {
                        "kind": "message",
                        "text": (
                            "🚫 当前会话不在白名单（会话 UMO 精确匹配），"
                            "无法使用小作文功能"
                        ),
                    }
            # 缺游戏名不消耗额度，也不搜索；保持在白名单检查之后。
            if needs_explicit_game(game_name, experience):
                return {"kind": "game"}
            identity_key = self.config_center.identity_key(connector, sender_id)
            if not is_admin:
                rate_check = await self.rate_limiter.check_and_record(
                    identity_key, self
                )
                if not rate_check["allowed"]:
                    return {"kind": "message", "text": rate_check["message"]}
            return {
                "kind": "ok",
                "is_admin": is_admin,
                "identity_key": identity_key,
            }

    @staticmethod
    def _get_umo(event: AstrMessageEvent) -> str:
        """安全读取统一消息来源；缺失/异常时返回空字符串。"""
        try:
            value = getattr(event, "unified_msg_origin", "")
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    def _chat_meta(self, umo: str) -> dict:
        connector, chat_type = parse_umo(umo)
        tail = str(umo or "").split(":")[-1][-8:] if umo else ""
        return {
            "key": umo or f"{connector}:{chat_type}:unknown",
            "connector": connector or "unknown",
            "type": chat_type,
            "umo": umo,
            "label": f"{connector or 'unknown'} · {chat_type} · {tail}",
        }

    async def _record_request_start(
        self, umo: str, connector: str, sender_id: str,
        identity_key: str, is_admin: bool,
    ) -> str:
        request_id = f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        source = self.model_service.source
        model = (
            self.model_service.provider_id
            if source == "astrbot"
            else self.model_service.cfg.get("model", "")
        )
        try:
            await self.stats_store.record({
                "kind": "request",
                "request_id": request_id,
                "status": "started",
                "chat": self._chat_meta(umo),
                "user": {
                    "connector": connector or "unknown",
                    "uid": sender_id,
                    "identity": identity_key,
                    "admin": is_admin,
                },
                "model_source": source,
                "model": model,
            })
        except Exception:
            logger.exception("[小作文生成器] 记录请求统计失败")
        return request_id

    async def _record_model(
        self, method: str, umo: str, result=None, error=None,
        duration_ms: int = 0,
    ) -> None:
        kind = {
            "generate": "generate",
            "summarize_search": "summarize",
            "test_connection": "test",
        }.get(method, method)
        source = self.model_service.source
        model = (
            self.model_service.provider_id
            if source == "astrbot"
            else self.model_service.cfg.get("model", "")
        )
        usage = (result or {}).get("token_usage") if isinstance(result, dict) else None
        token_available = bool(
            isinstance(result, dict)
            and result.get("token_available", isinstance(usage, dict))
            and isinstance(usage, dict)
        )
        if token_available:
            usage = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        else:
            usage = None
        # 聊天内的模型测试用返回值里的 success 表达失败（不抛异常）。
        # 只依据 error 会把这种失败误记为成功，必须一并判定。
        failed = error is not None or (
            isinstance(result, dict) and result.get("success") is False
        )
        try:
            await self.stats_store.record({
                "kind": "model",
                "status": "failed" if failed else "success",
                "model_kind": kind,
                "chat": (
                    self._chat_meta(umo)
                    if umo or kind != "test" else None
                ),
                "model_source": source,
                "model": (result or {}).get("model", model) if isinstance(result, dict) else model,
                "token_usage": usage,
                "token_available": token_available,
                "duration_ms": int(duration_ms),
                "error_category": type(error).__name__ if error else "",
            })
        except Exception:
            logger.exception("[小作文生成器] 记录模型统计失败")

    async def _record_search(
        self, search_result: dict, umo: str, request_id: str = ""
    ) -> None:
        attempts = search_result.get("attempted") or []
        # 空 UMO 表示没有聊天归属（例如配置中心网页主动测试）。不能伪造
        # 一个 unknown 聊天，也不能借用“最后一次聊天”的上下文。
        chat = self._chat_meta(umo) if umo else None
        for index, item in enumerate(attempts):
            # 第一个真实尝试不是回退；只有它之后的尝试才计入回退次数。
            # search_result.degraded 描述整次请求，不能复制到每个 attempt，
            # 否则“首个失败、第二个成功”会被算成两次回退。
            fallback = index > 0
            try:
                await self.stats_store.record({
                    "kind": "search",
                    "attempted": True,
                    "request_id": request_id,
                    "status": item.get("status", ""),
                    "backend": item.get("backend", ""),
                    "reason": item.get("reason", ""),
                    "result_count": item.get("result_count", 0),
                    "duration_ms": item.get("duration_ms", 0),
                    "degraded": fallback,
                    "fallback": fallback,
                    "effective_backend": search_result.get(
                        "effective_backend", ""
                    ),
                    "search_mode": str(search_result.get("mode") or ""),
                    "chat": chat,
                })
            except Exception:
                logger.exception("[小作文生成器] 记录搜索统计失败")

    async def _call_model(self, method: str, *args, umo: str = "", **kwargs):
        """调用统一模型层；兼容测试中注入的旧 FakeAIClient 签名。"""
        fn = getattr(self.ai_client, method)
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            signature = None
        accepts_umo = False
        if signature is not None:
            accepts_umo = (
                "umo" in signature.parameters
                or any(
                    item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in signature.parameters.values()
                )
            )
        started = time.monotonic()
        try:
            if accepts_umo:
                result = await fn(*args, umo=umo, **kwargs)
            else:
                result = await fn(*args, **kwargs)
        except AIClientError as exc:
            await self._record_model(
                method, umo, error=exc,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        except Exception as exc:
            await self._record_model(
                method, umo, error=exc,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        await self._record_model(
            method, umo, result=result,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

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
        # 用户可能在消息里直接写「玩游戏的时间：半小时」这类固定字段，
        # 这些值优先于模型输出，且不能留在游戏名或故事里。
        explicit_fields = parse_activity_fields(text) if matched else {}

        if not matched:
            return
        self.last_umo = self._get_umo(event)
        connector, sender_id, umo = self._event_identity(event)

        decision = await self._authorize_request(
            connector, sender_id, umo, game_name, experience
        )
        if decision["kind"] == "message":
            yield event.plain_result(decision["text"])
            event.stop_event()
            return
        if decision["kind"] == "game":
            yield event.plain_result(GAME_REQUIRED_MESSAGE)
            event.stop_event()
            return

        is_admin = decision["is_admin"]
        identity_key = decision["identity_key"]
        # 活动可选尾注（许愿卡/游戏 ID/账号 ID/区服/系统）只在用户明确
        # 写出时使用，并从经历里移除，避免被写成故事细节。
        experience, optional_extras = parse_optional_extras(experience)

        request_id = await self._record_request_start(
            umo, connector, sender_id, identity_key, is_admin
        )

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
        await self._record_search(
            search_result, umo, request_id=request_id
        )

        # 默认素材直传：把清洗后的「标题、来源、链接、片段」直接交给生
        # 成模型，一次生成只调用一次写作模型。AI 汇总保留为高级显式选项。
        # 生成入模阶段区分查询方向：体验/剧情素材优先，机制/平台资料靠后，
        # 让搜索背景只服务故事，不容易把玩法介绍铺成正文主体。
        material_results = sorted(
            search_results,
            key=lambda item: 0 if item.get("query_index") == 1 else 1,
        )
        material_block = build_material_block(
            material_results,
            self.search_material_max_chars,
            self.search_material_max_items,
            self.search_material_snippet_chars,
        )
        if material_block:
            logger.info(
                "[小作文生成器] 素材直传: %s 条 / %s 字（预算 %s 字）",
                material_block.count("链接："),
                len(material_block),
                self.search_material_max_chars,
            )

        ai_digest = ""
        if raw_summary and self.search_summarize_enabled:
            try:
                digest_result = await self._call_model(
                    "summarize_search",
                    game_name,
                    raw_summary,
                    self.search_summarize_max_chars,
                    umo=self._get_umo(event),
                )
                ai_digest = str(digest_result.get("content") or "")
                logger.info(
                    "[小作文生成器] 搜索汇总完成: %s 字符（高级选项）",
                    len(ai_digest),
                )
            except AIClientError as e:
                logger.warning(
                    "[小作文生成器] 搜索汇总失败，回退素材直传: %s",
                    e,
                )
        if (ai_digest or "").strip():
            search_digest = prepare_search_digest(
                "", ai_digest, self.search_summarize_max_chars
            )
        else:
            search_digest = material_block
            material_budget = self.search_material_max_chars
            if not search_digest:
                # 链接放不下时退化为不含链接的素材块：标题与片段仍入模，
                # 字数、条数、单条片段同样受直传预算约束。
                search_digest = build_material_block(
                    search_results,
                    material_budget,
                    self.search_material_max_items,
                    self.search_material_snippet_chars,
                    include_link=False,
                )
            if not search_digest:
                # 没有可用结构化条目时才退回文本摘要，兜底截断到素材预算。
                search_digest = prepare_search_digest(
                    raw_summary, "", material_budget
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
            ai_result = await self._call_model(
                "generate", prompt, umo=self._get_umo(event)
            )
        except AIClientError as e:
            logger.error(f"[小作文生成器] AI 调用失败: {e}")
            yield event.plain_result(e.user_message)
            event.stop_event()
            return

        # 后处理并返回可复制的小作文；不自动重试、不自动发布，也不在
        # 用户消息中追加缺失字段、长度不足或截断提示。
        ai_content = ai_result["content"]
        final_message = build_final_message(
            game_name,
            ai_content,
            experience=experience,
            extras=optional_extras,
            explicit_fields=explicit_fields,
            # 素材里的已知事实用于成文兜底：模型漏填平台时按素材写，
            # 模型时间早于已知发售年时换成「发售后不久」。
            known_facts=search_digest,
        )
        yield event.plain_result(final_message)
        # 组装生成信息
        if self.show_generation_stats:
            sender_name = event.get_sender_name() or sender_id
            game_mode = f"随机-{game_name}" if is_random else game_name
            duration_s = f"{ai_result['duration_ms'] / 1000:.1f}秒"
            tokens = ai_result.get("token_usage") or {}
            token_available = ai_result.get("token_available") is True
            search_provider = search_result.get("provider", "未使用")
            search_duration = f"{search_result.get('duration_ms', 0) / 1000:.1f}秒"
            search_backend = search_result.get("effective_backend", "")
            search_backend_label = BACKEND_LABELS.get(
                search_backend, search_backend or "无"
            )
            search_status = search_result.get("status", "failed")
            search_outcome = search_outcome_label(search_result)
            if search_status == "success":
                if search_outcome == "已切换到其他后端":
                    search_degraded = "是（已切换）"
                elif search_result.get("partial"):
                    search_degraded = "是（部分失败）"
                else:
                    search_degraded = "否"
            elif search_status == "disabled":
                search_degraded = "未调用（搜索关闭）"
            elif search_status == "no_results":
                search_degraded = "否（无有效结果）"
            elif search_result.get("attempted"):
                search_degraded = "否（全部失败）"
            else:
                search_degraded = "未调用（无可用后端）"
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
            usage = await self.rate_limiter.get_usage_status(
                identity_key, self
            )
            if is_admin:
                usage_str = "管理员（不受会话频率限制）"
            elif usage["window_minutes"] <= 0 and usage["daily_max"] <= 0:
                usage_str = "不限制"
            elif usage["window_minutes"] <= 0:
                usage_str = (
                    f"窗口不限制，今日：{usage['daily_used']}/"
                    f"{usage['daily_max']}"
                )
            elif usage["daily_max"] <= 0:
                usage_str = (
                    f"窗口：{usage['window_used']}/{usage['window_max']}"
                    f"（{usage['window_minutes']}分钟），今日不限制"
                )
            else:
                usage_str = (
                    f"窗口：{usage['window_used']}/{usage['window_max']}"
                    f"（{usage['window_minutes']}分钟），今日："
                    f"{usage['daily_used']}/{usage['daily_max']}"
                )

            info_lines = [
                "📊 本次生成信息",
                f"调用者：{sender_name}（{sender_id}）",
                f"指定游戏：{game_mode}",
                f"生成模型：{ai_result['model']}",
                f"API格式：{ai_result['api_format']}",
                f"生成时间：{duration_s}",
                (
                    f"Token消耗：{tokens.get('total_tokens', 0)}"
                    f"（输入{tokens.get('prompt_tokens', 0)} + "
                    f"输出{tokens.get('completion_tokens', 0)}）"
                    if token_available
                    else "Token消耗：未知（上游未返回用量）"
                ),
                f"搜索服务：{search_provider}（{search_duration}）",
                f"搜索后端：{search_backend_label}",
                f"搜索状态：{search_outcome}",
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
        result = await self._call_model(
            "test_connection", umo=self._get_umo(event)
        )

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
        # 聊天内主动测试归实际发起的聊天；与网页测试共用按后端尝试统计。
        await self._record_search(
            result,
            self._get_umo(event),
            request_id=f"search-test-{time.time_ns()}",
        )

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
        await self.model_service.close()
        await self.search_service.close()
        logger.info("[小作文生成器] 插件已卸载")
