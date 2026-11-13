"""
白名单管理模块。

支持两种显式模式，不做新旧列表的无条件 OR：
- legacy：沿用 whitelist_groups / whitelist_privates；
- umo：按 AstrBot event.unified_msg_origin 完整字符串精确匹配。

管理员身份不属于会话白名单，由主流程单独按发送者 ID 判断。
"""

WHITELIST_MODE_LEGACY = "legacy"
WHITELIST_MODE_UMO = "umo"
WHITELIST_MODES = (WHITELIST_MODE_LEGACY, WHITELIST_MODE_UMO)


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


def is_umo_allowed(umo: str, whitelist_umos: list[str]) -> bool:
    """UMO 模式：空列表不限制；非空时只允许完整、逐字符相同的 UMO。

    事件没有有效 UMO 时，配置了非空列表必须拒绝，不能因取值失败放行。
    """
    if whitelist_umos is None:
        return True
    if isinstance(whitelist_umos, str):
        if not whitelist_umos:
            return False
        configured = [whitelist_umos]
    elif isinstance(whitelist_umos, (list, tuple, set)):
        configured = list(whitelist_umos)
    else:
        return False
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
    mode: str = WHITELIST_MODE_LEGACY,
    umo: str = "",
    whitelist_umos: list[str] | None = None,
) -> bool:
    """按显式模式检查会话权限。

    Args:
        session_id: 旧模式下的群号或发送者 ID。
        session_type: "group" 或 "private"。
        whitelist_groups: 旧群白名单。
        whitelist_privates: 旧私聊白名单。
        mode: "legacy" 或 "umo"；未知值按 legacy 处理。
        umo: 实际事件的 event.unified_msg_origin。
        whitelist_umos: UMO 白名单。

    Returns:
        是否允许。
    """
    normalized_mode = str(mode or WHITELIST_MODE_LEGACY).strip().lower()
    if normalized_mode == WHITELIST_MODE_UMO:
        return is_umo_allowed(umo, whitelist_umos)
    if session_type == "group":
        return is_group_allowed(session_id, whitelist_groups)
    return is_private_allowed(session_id, whitelist_privates)
