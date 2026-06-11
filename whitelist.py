"""
白名单管理模块
基于配置的白名单模式：仅白名单中的群聊/私聊可使用小作文功能
管理员不受白名单限制
"""


def is_group_allowed(group_id: str, whitelist_groups: list[str]) -> bool:
    """
    检查群聊是否在白名单中

    Args:
        group_id: 群号
        whitelist_groups: 白名单群聊列表

    Returns:
        是否允许
    """
    if not whitelist_groups:
        return True  # 空白名单 = 全部允许
    return group_id in whitelist_groups


def is_private_allowed(sender_id: str, whitelist_privates: list[str]) -> bool:
    """
    检查私聊是否在白名单中

    Args:
        sender_id: QQ 号
        whitelist_privates: 白名单私聊列表

    Returns:
        是否允许
    """
    if not whitelist_privates:
        return True  # 空白名单 = 全部允许
    return sender_id in whitelist_privates


def is_session_allowed(
    session_id: str,
    session_type: str,
    whitelist_groups: list[str],
    whitelist_privates: list[str],
) -> bool:
    """
    检查当前会话是否允许使用

    Args:
        session_id: 会话 ID（群号或 QQ 号）
        session_type: "group" 或 "private"
        whitelist_groups: 白名单群聊列表
        whitelist_privates: 白名单私聊列表

    Returns:
        是否允许
    """
    if session_type == "group":
        return is_group_allowed(session_id, whitelist_groups)
    return is_private_allowed(session_id, whitelist_privates)
