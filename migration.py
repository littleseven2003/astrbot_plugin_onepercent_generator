"""显式版本化的旧配置迁移。

AstrBotConfig 会先按新 schema 补默认值，因此不能用“新字段不存在”推断
是否需要迁移。这里使用 config_migration_version 和保留在 schema 中的旧字段。
"""

from __future__ import annotations

import logging

from .config_center import parse_umo, session_entry_error
from .search_service import MODES, SEARCH_GROUPS, resolve_search_groups
from .whitelist import validate_umo_list

logger = logging.getLogger("astrbot")
# v2 修复 v1 的静默丢弃：旧 UMO 列表全部无法识别时也必须保留待处理。
# v3 移除“确认不限制”旁路：v2 中已确认且列表为空的限制必须重新暂停。
# v4 为旧搜索 API Key 补记绑定地址，避免升级后把旧 Key 发往新地址。
MIGRATION_VERSION = 4
# 搜索偏好迁移版本：v1 只统一 mode 与旧五项列表；v2 收敛为三个搜索组
# （Astrbot与搜索插件/搜索API/网页搜索），并移除独立降级开关。
SEARCH_MODE_PREFS_VERSION = 2
# 搜索素材偏好版本：v1 把“默认 AI 汇总 + 150 字摘要”改为素材直传，
# 并补齐素材预算字段。旧开关只在版本未迁移时关闭一次，之后用户可在
# 管理页高级参数里重新开启 AI 汇总。
SEARCH_MATERIAL_PREFS_VERSION = 1


def _strings(value) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _legacy_reason(entry_error: str) -> str:
    """把 session_entry_error 的机器码映射回 legacy_umo_pending 的旧词汇。

    前端诊断沿用 empty / invalid_format / unknown_type 三个既有取值。
    """
    if entry_error == "umo_missing":
        return "empty"
    if entry_error == "umo_unknown_type":
        return "unknown_type"
    return "invalid_format"


def _classify_legacy_umos(value, error: str):
    """区分可精确迁移的 UMO 与必须由管理员处理的旧条目。

    返回 (migratable, pending)。pending 保留原始值，供配置中心展示；
    未知类型、缺少会话段、空条目和非法列表都不能静默消失。
    """
    if error:
        return [], [{"umo": "", "reason": "invalid_list"}]
    migratable = []
    pending = []
    seen_migratable = set()
    seen_pending = set()
    for item in value:
        text = "" if item is None else str(item).strip()
        # 与配置中心保存端共用同一套会话标识规则，避免旧白名单把
        # "qq:FriendMessage:" 这类第三段为空的值当成可迁移 UMO 静默放行。
        # session_entry_error 使用面向状态接口的机器码，这里映射回旧版
        # legacy_umo_pending 的既有 reason 词汇，保持前端诊断兼容。
        entry_error = session_entry_error({"umo": text})
        if not entry_error:
            if text not in seen_migratable:
                migratable.append(text)
                seen_migratable.add(text)
            continue
        reason = _legacy_reason(entry_error)
        key = (text, reason)
        if key not in seen_pending:
            pending.append({"umo": text, "reason": reason})
            seen_pending.add(key)
    return migratable, pending


def _legacy_empty_session_entry_umo(item) -> str:
    """识别旧候选（e0472c0）可能已写进 sessions 的坏条目。

    旧分类只要求“冒号数≥2 且平台/类型可识别”，因此会把
    `qq:FriendMessage:` 这类**平台和类型都合法、但完整会话 ID 为空**的
    值当有效 UMO 落盘。只有这种形态才可能由旧版自动迁移写入；用户
    手工填错的其他坏条目（缺段、未知类型、非对象等）不在此列，仍走
    原有“整表拒绝 + 逐条诊断”路径。

    返回该条目的 UMO 裸值；不是这种形态返回空串。
    """
    if not isinstance(item, dict):
        return ""
    text = str(item.get("umo") or "").strip()
    parts = text.split(":", 2)
    if len(parts) < 3:
        return ""
    platform, message_type, session_id = parts
    if not platform.strip() or not message_type.strip() or session_id:
        return ""
    connector, chat_type = parse_umo(text)
    if not connector or chat_type == "unknown":
        return ""
    return text


def _migrate_search_key_origin(config) -> bool:
    """为 v3 及更早版本中没有绑定地址的旧搜索 Key 补记当前地址。

    api_key_origin 是 a6303dc 之后才由统一配置中心写入的字段。v3 及更早
    的配置只有可见的 api_base_url，无法判断 Key 原本属于哪个地址；升级到
    v4 时把当前地址视为它的绑定地址，之后修改地址就不会再发送旧 Key。

    迁移边界只到 v3->v4。已经是 v4 的配置缺少 origin 时，说明 Key 不是由
    统一配置中心保存的（或绑定信息已损坏），不能再把当前地址当成它的来
    源静默补记；运行时会拒绝发送该 Key，并要求用户重新输入或清除。
    已有 origin 的配置绝不被改写，因此重复调用和后续升级都幂等。
    """
    search = config.get("search_config")
    if not isinstance(search, dict):
        return False
    key = str(search.get("api_key") or "").strip()
    origin = str(search.get("api_key_origin") or "").strip()
    if not key or origin:
        return False
    base = str(search.get("api_base_url") or "").strip()
    if not base:
        base = "https://api.tavily.com"
    search["api_key_origin"] = base
    return True


def _migrate_search_mode_prefs(config) -> bool:
    """把旧搜索策略幂等迁移为三组有序列表。

    v1 及更早的配置可能只有单值 mode（auto/astrbot/api/web/off），也可能
    已有旧五项 mode_order/mode_enabled 列表。迁移规则：
    - 旧运行时（d8f9da9 及更早）只按可见 mode 与降级开关执行，列表只是
      展示偏好；因此 v1 及以后的完整列表也跟随 mode + fallback 展开；
    - v0 的列表是用户显式改动（旧版迁移会按列表回写 mode）：列表优先，
      回写后的 mode 再叠加原降级开关；旧默认对（五项顺序 + ["auto"]）
      仍代表宿主补默认，按 mode 处理；
    - 顺序字段缺失/损坏时保留显式具体勾选，只有残留 auto/off 或无勾选
      时才退回单值 mode；显式空选、勾选 off 始终表示关闭；
    - 迁移完成后 mode 只作兼容展示（首个启用组或 off），列表才是运行时
      依据；版本号保证重复加载不会重排或重新勾选已关闭的组。
    """
    search = config.get("search_config")
    if not isinstance(search, dict):
        return False
    try:
        version = int(search.get("mode_prefs_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    raw_order = search.get("mode_order")
    raw_enabled = search.get("mode_enabled")
    changed = False

    order_valid = isinstance(raw_order, (list, tuple))
    enabled_valid = isinstance(raw_enabled, (list, tuple, set))
    if version < SEARCH_MODE_PREFS_VERSION or not (
        order_valid and enabled_valid
    ):
        # 旧版本，或 v2 配置丢了某个列表字段（schema 默认 null）：按
        # 旧 mode/另一字段换算。缺省、显式空选、显式全选严格区分。
        order, enabled = _convert_legacy_search_prefs(
            search, raw_order, raw_enabled, version
        )
        changed = True
        version = SEARCH_MODE_PREFS_VERSION
    else:
        # 已有完整 v2 列表：只做形状兜底，绝不重排既有顺序或重新启用
        # 已关闭的组。
        ordered = []
        for item in raw_order:
            key = str(item).strip().lower()
            if key in SEARCH_GROUPS and key not in ordered:
                ordered.append(key)
        for key in SEARCH_GROUPS:
            if key not in ordered:
                ordered.append(key)
        saved_enabled = {
            str(item).strip().lower() for item in raw_enabled
        }
        order = ordered
        enabled = [key for key in order if key in saved_enabled]
        if order != list(raw_order) or enabled != list(raw_enabled):
            changed = True

    effective = next((key for key in order if key in enabled), "off")
    if search.get("mode") != effective:
        search["mode"] = effective
        changed = True
    if search.get("mode_order") != order:
        search["mode_order"] = order
        changed = True
    if search.get("mode_enabled") != enabled:
        search["mode_enabled"] = enabled
        changed = True
    if search.get("fallback_enabled") is not False:
        # 独立降级开关已移除；运行时只看有序列表。
        search["fallback_enabled"] = False
        changed = True
    if version != SEARCH_MODE_PREFS_VERSION or (
        search.get("mode_prefs_version") != SEARCH_MODE_PREFS_VERSION
    ):
        search["mode_prefs_version"] = SEARCH_MODE_PREFS_VERSION
        changed = True
    return changed


def _migrate_search_material_prefs(config) -> bool:
    """把旧的默认 AI 汇总迁移为素材直传，并补齐素材预算字段。

    旧版 schema 默认 ``summarize_enabled=true``、摘要 150 字，配置里几乎
    都存在 true，无法区分“默认”与“用户显式开启”。因此只在素材偏好
    迁移版本未升级时关闭一次；之后用户在管理页重新开启会被保留。
    """
    search = config.get("search_config")
    if not isinstance(search, dict):
        return False
    try:
        version = int(search.get("material_prefs_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if version >= SEARCH_MATERIAL_PREFS_VERSION:
        return False
    changed = False
    if search.get("summarize_enabled"):
        search["summarize_enabled"] = False
        changed = True
    defaults = (
        ("material_max_chars", 3000),
        ("material_max_items", 6),
        ("material_snippet_chars", 400),
    )
    for key, value in defaults:
        current = search.get(key)
        if isinstance(current, bool) or not isinstance(current, int):
            search[key] = value
            changed = True
    search["material_prefs_version"] = SEARCH_MATERIAL_PREFS_VERSION
    return True


def _convert_legacy_search_prefs(search: dict, raw_order, raw_enabled, version: int = 0):
    """把旧搜索策略换算为三组全序与勾选。

    旧运行时只看单值 ``mode`` 与独立降级开关，列表只是配置中心的展示
    偏好。因此换算规则按版本区分：
    - v1 及以后：可见 ``mode`` 由配置中心与宿主原配置页共同维护，完整
      列表也跟随 ``mode`` + ``fallback`` 展开；``auto`` 始终展开三组；
      勾选 ``off`` 而宿主把 mode 改成开启时跟随 mode；单个具体勾选按旧
      执行顺序写回列表；用户显式多选具体组时保留多选（不能被 mode 收窄）；
    - v0：列表是用户显式改动（旧版迁移会按列表回写 mode），列表优先，
      回写后的 mode 再叠加降级开关；旧 schema 默认对（五项顺序 +
      ``["auto"]``）仍代表宿主补默认，按 ``mode`` 处理。
    顺序字段缺失/损坏时保留显式具体勾选。显式空选 ``[]`` 与勾选中的
    ``off`` 始终表示关闭（v1 宿主把 mode 从关闭改为开启除外）。schema
    用 null 表示字段缺省，与 ``[]`` 区分。
    """
    order_valid = isinstance(raw_order, (list, tuple))
    enabled_valid = isinstance(raw_enabled, (list, tuple, set))
    raw_keys = [str(item).strip().lower() for item in raw_order] if order_valid else []
    enabled_raw = (
        {str(item).strip().lower() for item in raw_enabled}
        if enabled_valid else set()
    )
    canonical = list(SEARCH_GROUPS)
    legacy_modes = ("auto", "astrbot", "api", "web", "off")
    mode_raw = str(search.get("mode") or "").strip().lower()
    mode_valid = mode_raw in legacy_modes

    def preset_order():
        order: list[str] = []
        for key in raw_keys:
            if key in SEARCH_GROUPS and key not in order:
                order.append(key)
        for group in canonical:
            if group not in order:
                order.append(group)
        return order

    def mode_based():
        groups = set(resolve_search_groups(
            enabled=True,
            mode=search.get("mode"),
            fallback_enabled=bool(search.get("fallback_enabled", False)),
        ))
        order = preset_order() if order_valid else canonical
        return order, [key for key in order if key in groups]

    # 显式空列表（不是缺省 null）始终表示关闭。
    if enabled_valid and not enabled_raw:
        order = preset_order() if order_valid else canonical
        return order, []

    concrete_selected = bool(enabled_raw & set(SEARCH_GROUPS))

    def expand(base_mode: str, execution_order: bool = False):
        """按旧运行时的 mode + fallback 展开有序组列表。

        d8f9da9 及更早的运行时只看可见 mode 与独立降级开关：auto 依次尝试
        三组，astrbot/api 在允许降级时继续尝试后续组；列表不参与执行。
        ``execution_order`` 为真时把旧执行顺序写进列表（仅勾 API 且允许
        降级、未勾选的网页排在 API 之前，迁移后仍要先调 API）。
        """
        if base_mode == "off":
            return (preset_order() if order_valid else canonical), []
        if base_mode == "auto":
            return list(canonical), list(canonical)
        groups = resolve_search_groups(
            enabled=True,
            mode=base_mode,
            fallback_enabled=bool(search.get("fallback_enabled", False)),
        )
        keys = [key for key in groups if key in canonical]
        if execution_order:
            order = keys + [key for key in canonical if key not in keys]
            return order, keys
        order = preset_order()
        return order, [key for key in order if key in groups]

    # 勾选“关闭”：v1 起宿主可能只改可见 mode（从关闭改为开启），此时以
    # mode 为准；mode 也是关闭、或 v0 列表优先时保持关闭。
    if enabled_valid and "off" in enabled_raw:
        if version >= 1 and mode_valid and mode_raw != "off":
            return expand(mode_raw, execution_order=True)
        order = preset_order() if order_valid else canonical
        return order, []

    # 顺序字段缺失/损坏：显式勾选的具体组仍然有效（v1/v2 都可能丢顺序），
    # 只有残留 auto/off 或无勾选时才退回单值 mode。
    if not order_valid or not enabled_valid:
        if enabled_valid and concrete_selected:
            return canonical, [key for key in canonical if key in enabled_raw]
        return mode_based()

    # v0 旧 schema 的默认对（5 项顺序 + ["auto"]）只代表列表被宿主补过
    # 默认值，不是用户改动；此时旧版只看单值 mode，必须优先采用 mode。
    if (
        raw_keys == list(MODES)
        and enabled_raw == {"auto"}
    ):
        return mode_based()

    # 与 d8f9da9 一致：判定首个启用项前先补齐旧五项顺序。这样
    # ["web"] + ["api"] 这类缺项列表也能找回已勾选的 API，而不是判成关闭。
    completed_keys = list(raw_keys)
    for key in legacy_modes:
        if key not in completed_keys:
            completed_keys.append(key)

    first_enabled = next(
        (
            key for key in completed_keys
            if key in legacy_modes and key in enabled_raw
        ),
        None,
    )

    # 旧运行时只看可见 mode 与降级开关，列表只是展示偏好：v1 及以后
    # “旧配置中心保存后只改可见 mode”必须跟随 mode，否则 api->off 仍调用
    # API、astrbot->api 仍尝试 AstrBot 和 Anysearch。
    if version >= 1 and mode_valid:
        if mode_raw == "auto":
            # 旧 auto 不论同时勾选了哪些组，都按三组顺序执行。
            return expand("auto")
        # 多选只按三个具体组判定：残留的 auto/off 不算用户多选，否则
        # “mode=api、勾选 api+auto、允许降级”会被当成多选而反转执行顺序。
        checked = [
            key for key in completed_keys
            if key in SEARCH_GROUPS and key in enabled_raw
        ]
        if mode_raw not in enabled_raw or len(checked) <= 1:
            return expand(
                mode_raw, execution_order=concrete_selected
            )
        # 用户在旧列表里显式多选了具体组：保留这份多选，不能被 mode 收窄
        # （默认行顺序 + 勾选三组时不能只剩 AstrBot）；允许降级时再叠加
        # 旧降级开关带来的后续组。
        order = preset_order()
        keys = [key for key in order if key in enabled_raw]
        if search.get("fallback_enabled") and first_enabled:
            extra = resolve_search_groups(
                enabled=True, mode=first_enabled, fallback_enabled=True
            )
            keys = [key for key in order if key in keys or key in extra]
        return order, keys

    # v0 的列表是用户显式改动（旧版迁移会按列表回写 mode），列表优先；
    # 回写后的 mode 再叠加原降级开关，与旧运行时的实际调用一致。
    return expand(first_enabled or "off")


def _save(config) -> None:
    saver = getattr(config, "save_config", None)
    if saver is None:
        return
    try:
        saver()
    except Exception:
        logger.exception("[小作文生成器] 旧配置迁移保存失败")


def migrate_legacy_config(config) -> dict:
    """迁移旧字段；幂等、只在版本未完成时执行。

    返回 {"changed": bool, "sessions": int, "admin_pending": int,
          "restrictions_pending": bool}。
    """
    if not isinstance(config, dict):
        return {"changed": False, "sessions": 0, "admin_pending": 0,
                "restrictions_pending": False}
    try:
        version = int(config.get("config_migration_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    # 旧 Key 绑定只属于 v3->v4 迁移。version>=4 的配置再缺少 origin 时
    # 不得把当前地址当成 Key 的来源；否则原生配置页改地址后会误把旧 Key
    # 发往新地址。v4 运行时会拒绝无绑定地址的 Key，等待用户重新保存。
    changed = False
    if version < MIGRATION_VERSION:
        changed = _migrate_search_key_origin(config)
    if _migrate_search_mode_prefs(config):
        changed = True
    if _migrate_search_material_prefs(config):
        changed = True

    # 旧版曾把未限定连接器的管理员写进 admin_users；这种 legacy 条目不能再
    # 作为所有连接器的管理员。无论迁移版本如何都移出活动列表。
    admin_users = config.get("admin_users")
    if not isinstance(admin_users, list):
        admin_users = []
    pending = []
    raw_pending = config.get("legacy_admin_pending")
    if isinstance(raw_pending, list):
        for item in raw_pending:
            if isinstance(item, dict):
                uid = str(item.get("uid") or "").strip()
                if uid:
                    pending.append({"uid": uid, "connector": ""})
    active = []
    for item in admin_users:
        if not isinstance(item, dict):
            continue
        connector = str(item.get("connector") or "").strip()
        uid = str(item.get("uid") or "").strip()
        if not uid:
            continue
        if item.get("legacy") or not connector:
            pending.append({"uid": uid, "connector": ""})
            changed = True
            continue
        active.append({"connector": connector, "uid": uid, "legacy": False})
    if active != admin_users:
        config["admin_users"] = active
        changed = True
    if not isinstance(config.get("admin_users"), list):
        config["admin_users"] = active
        changed = True

    old_admins = _strings(config.get("admin_qqs"))
    # admin_qqs 属于 v1/v2 迁移；v2→v3 只做 v3 增量，避免已处理的管理员
    # 在每次升级时再次进入 pending。
    if version < 2 and old_admins:
        for uid in old_admins:
            pending.append({"uid": uid, "connector": ""})
        changed = True

    # 旧管理员的 UID 只在 pending 中展示；用户必须选择连接器后重新添加。
    dedup = []
    seen = set()
    for item in pending:
        key = (item.get("connector", ""), item["uid"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append({"uid": item["uid"], "connector": item.get("connector", "")})
    if dedup != config.get("legacy_admin_pending"):
        config["legacy_admin_pending"] = dedup
        changed = True

    if version >= 3:
        # v3 已经完成旧会话迁移。v4 只补搜索 Key 绑定，不能重新执行会话
        # 迁移：v3 可能保留了“旧版确认不限制”导致的 pending，而旧白名单
        # 和确认标记此时都已被清空；重跑会把 pending 误判为 false 并放行。
        # 保留已有 pending、sessions 和 legacy_umo_pending，只清理残留标记
        # 并升级版本号。
        residual_confirmed = config.get("umos_unrestricted_confirmed") is True
        if residual_confirmed:
            config["umos_unrestricted_confirmed"] = False
            changed = True
        # 残留的“旧版确认不限制”没有对应的 pending：清掉确认标记后，
        # 空会话列表会被当成“不限”。必须恢复暂停，等待管理员明确修正。
        settings = config.get("session_settings")
        raw_sessions = (
            settings.get("sessions") if isinstance(settings, dict) else None
        )
        has_sessions = isinstance(raw_sessions, (list, tuple, set)) and len(
            raw_sessions
        ) > 0
        if (
            residual_confirmed
            and not has_sessions
            and not config.get("legacy_restrictions_pending")
        ):
            config["legacy_restrictions_pending"] = True
            changed = True
        # 定向修复已落盘的 v4：旧候选（e0472c0）把 `qq:FriendMessage:` 这类
        # 平台/类型合法但完整会话 ID 为空的值写进 sessions 并落盘。整段重迁
        # 会误清 v3 的 pending，所以只把这种“旧版可能写出的坏条目”移回
        # legacy_umo_pending，保留其余有效条目；若移完后已无有效条目，必须
        # 回到 null（未配置）而不是 []（不限制），否则会静默放行全部会话。
        if isinstance(raw_sessions, list):
            kept = []
            moved = []
            existing = config.get("legacy_umo_pending")
            pending_list = list(existing) if isinstance(existing, list) else []
            seen_pending = {
                (str(entry.get("umo") or ""), str(entry.get("reason") or ""))
                for entry in pending_list if isinstance(entry, dict)
            }
            for item in raw_sessions:
                bad_umo = _legacy_empty_session_entry_umo(item)
                if not bad_umo:
                    kept.append(item)
                    continue
                moved.append(bad_umo)
                key = (bad_umo, "invalid_format")
                if key not in seen_pending:
                    pending_list.append({
                        "umo": bad_umo, "reason": "invalid_format",
                    })
                    seen_pending.add(key)
            if moved:
                settings["sessions"] = kept if kept else None
                config["legacy_umo_pending"] = pending_list
                config["legacy_restrictions_pending"] = True
                changed = True
                logger.info(
                    "[小作文生成器] v4 会话列表定向修复 moved=%s kept=%s",
                    len(moved), len(kept),
                )
        if version != MIGRATION_VERSION:
            config["config_migration_version"] = MIGRATION_VERSION
            changed = True
        if changed:
            _save(config)
        return {"changed": changed, "sessions": 0,
                "admin_pending": len(dedup),
                "restrictions_pending": bool(
                    config.get("legacy_restrictions_pending", False)
                )}

    old_umos = config.get("whitelist_umos")
    if "whitelist_umos" not in config:
        # 真正的新安装（schema 默认值尚未落盘）没有旧白名单，不是非法列表。
        migratable_umos = []
        legacy_umo_pending = []
        umo_error = ""
    else:
        umo_error = validate_umo_list(old_umos)
        if isinstance(old_umos, (list, tuple, set)):
            migratable_umos, legacy_umo_pending = _classify_legacy_umos(
                old_umos, umo_error
            )
        else:
            migratable_umos = []
            legacy_umo_pending = [{"umo": "", "reason": "invalid_list"}]
    legacy_groups = _strings(config.get("whitelist_groups"))
    legacy_privates = _strings(config.get("whitelist_privates"))
    restrictions_pending = bool(
        config.get("legacy_restrictions_pending", False)
    )
    sessions_added = 0

    settings = config.get("session_settings")
    if not isinstance(settings, dict):
        settings = {"allow_private": True, "allow_group": True}
        config["session_settings"] = settings
        changed = True
    raw_sessions = settings.get("sessions")
    if isinstance(raw_sessions, list):
        sessions = raw_sessions
    elif raw_sessions is None and migratable_umos:
        # 真实 4.29.4 无法区分“显式 null”和“字段缺省”，两者读出来都是
        # None。只有在旧白名单能可靠映射成精确 UMO 时才写入明确列表；
        # 否则保持 null（尚未配置），由配置中心拒绝普通会话并等待管理员
        # 显式保存。绝不能为兼容 v2 把它写成 []——空列表表示不限制。
        sessions = []
        settings["sessions"] = sessions
        changed = True
    else:
        # 已有但非数组（数字/字符串/对象）：保留原值，交给配置中心诊断为
        # 配置错误并拒绝普通会话。绝不能归一化成 []：空列表表示“不限制”，
        # 会把非法值静默洗成放行。
        sessions = []

    if migratable_umos and isinstance(settings.get("sessions"), list) and not sessions:
        # 能识别的旧 UMO 仍是明确、可安全映射的策略：保留精确限制。
        # 如果用户已经有非空新会话列表，不覆盖用户的新配置。
        for umo in migratable_umos:
            connector, chat_type = parse_umo(umo)
            sessions.append({
                "umo": umo,
                "connector": connector,
                "type": chat_type,
                "enabled": True,
            })
            sessions_added += 1
        changed = True

    # v2 允许“确认不限制”把 pending 写成 false（仅空列表时）。该开关已删除，
    # 升级时必须让这些遗留配置重新暂停；已有明确会话的配置不重复打扰。
    recheck_legacy = version >= 2 and not sessions
    confirmed_unrestricted = config.get("umos_unrestricted_confirmed") is True
    if legacy_groups or legacy_privates or legacy_umo_pending:
        # 旧群号/私聊号无法映射成 UMO；未知/无效 UMO 也绝不能静默丢弃。
        # 不再提供“确认不限制”开关：必须由管理员添加明确的 UMO 会话。
        if version < 2 or restrictions_pending or recheck_legacy:
            restrictions_pending = True
    elif confirmed_unrestricted and not sessions:
        # 旧版确认“不限制”时会清空 whitelist_* 和 legacy_umo_pending，
        # 只剩这个 schema 保留的标记；空会话列表必须重新暂停。
        restrictions_pending = True
    else:
        restrictions_pending = False

    if config.get("legacy_umo_pending") != legacy_umo_pending:
        config["legacy_umo_pending"] = legacy_umo_pending
        changed = True
    if bool(config.get("legacy_restrictions_pending", False)) != restrictions_pending:
        config["legacy_restrictions_pending"] = restrictions_pending
        changed = True
    if config.get("umos_unrestricted_confirmed") is not False:
        # 标记只用于识别一次旧版确认，不能继续代表“允许不限制”。
        config["umos_unrestricted_confirmed"] = False
        changed = True
    config["config_migration_version"] = MIGRATION_VERSION
    changed = True
    if changed:
        _save(config)
    logger.info(
        "[小作文生成器] 旧配置迁移 version=%s sessions_added=%s "
        "admin_pending=%s umo_pending=%s restrictions_pending=%s",
        MIGRATION_VERSION, sessions_added, len(dedup),
        len(legacy_umo_pending), restrictions_pending,
    )
    return {"changed": changed, "sessions": sessions_added,
            "admin_pending": len(dedup),
            "umo_pending": len(legacy_umo_pending),
            "restrictions_pending": restrictions_pending}
