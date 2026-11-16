"""
白名单管理模块。

支持两种显式模式，不做新旧列表的无条件 OR：
- legacy：沿用 whitelist_groups / whitelist_privates；
- umo：按 AstrBot event.unified_msg_origin 完整字符串精确匹配。

缺失 mode 表示历史配置，按 legacy 兼容；显式 null/未知 mode 属于
无效配置，必须拒绝而不是回退 legacy。管理员身份不属于会话白名单。
"""

WHITELIST_MODE_LEGACY = "legacy"
WHITELIST_MODE_UMO = "umo"
WHITELIST_MODES = (WHITELIST_MODE_LEGACY, WHITELIST_MODE_UMO)
_MISSING = object()


def normalize_whitelist_mode(mode=_MISSING) -> str:
    """缺失 mode → legacy；显式无效 mode → 空字符串（拒绝）。"""
    if mode is _MISSING:
        return WHITELIST_MODE_LEGACY
    if not isinstance(mode, str):
        return ""
    normalized = mode.strip().lower()
    return normalized if normalized in WHITELIST_MODES else ""


def validate_whitelist_config(
    mode=_MISSING,
    mode_configured: bool = True,
    whitelist_umos=_MISSING,
    umos_configured: bool = True,
) -> str:
    """返回配置错误标识；空字符串表示有效。"""
    normalized = normalize_whitelist_mode(mode if mode_configured else _MISSING)
    if not normalized:
        return "invalid_whitelist_mode"
    if (
        normalized == WHITELIST_MODE_UMO
        and umos_configured
        and whitelist_umos is not _MISSING
    ):
        if not isinstance(whitelist_umos, (list, tuple, set)):
            return "invalid_whitelist_umos"
    return ""


def is_group_allowed(group_id: str, whitelist_groups: list[str]) -> bool:
    """旧模式：群白名单为空表示不限制，否则精确匹配群号/群 ID。"""
    if not whitelist_groups:
        return True
    return group_id in whitelist_groups


def is_private_allowed(sender_id: str, whitelist_privates: list[str]) -> bool:
    """旧模式：私聊白名单为空表示不限制，否则精确匹配发送者 ID。"""
    if not whitelist_privates:
        return True
    return sender_id in whitelist_privates


def is_umo_allowed(umo: str, whitelist_umos=_MISSING) -> bool:
    """UMO 模式：空列表不限制；非空时只允许完整、逐字符相同的 UMO。

    缺失列表（历史调用）按空列表兼容；显式 null、非法类型、空字符串
    配置均 fail closed。事件没有有效 UMO 时，非空列表拒绝。
    """
    if whitelist_umos is _MISSING:
        return True
    if whitelist_umos is None:
        return False
    if not isinstance(whitelist_umos, (list, tuple, set)):
        return False
    configured = list(whitelist_umos)
    if not configured:
        return True
    if not isinstance(umo, str) or not umo:
        return False
    return umo in configured


def is_session_allowed(
    session_id: str,
    session_type: str,
    whitelist_groups: list[str],
    whitelist_privates: list[str],
    *,
    mode=_MISSING,
    umo: str = "",
    whitelist_umos=_MISSING,
) -> bool:
    """按显式模式检查会话权限。

    - 不传 mode：历史调用，按 legacy；
    - 显式 mode=null/拼错/未知：拒绝；
    - legacy：旧群/私聊列表各自为空时放行；
    - umo：只使用 UMO 列表，空列表不限制，非空完整精确匹配。
    """
    normalized_mode = normalize_whitelist_mode(mode)
    if not normalized_mode:
        return False
    if normalized_mode == WHITELIST_MODE_UMO:
        return is_umo_allowed(umo, whitelist_umos)
    if session_type == "group":
        return is_group_allowed(session_id, whitelist_groups)
    return is_private_allowed(session_id, whitelist_privates)
