"""
统一 UMO 会话白名单。

完全移除按 QQ 号/群号匹配的旧授权路径。只支持：

- 空列表：不限制；
- 非空列表：event.unified_msg_origin 完整、大小写敏感的精确匹配；
- 缺失/空/无效事件 UMO 且列表非空：拒绝；
- 显式 null/非法列表类型：拒绝，不当作空列表放行。

旧版 whitelist_groups / whitelist_privates 不再参与授权。升级时若检测到
旧版非空白名单、且没有非空 UMO 或管理员明确确认不限制，则暂停普通用户
使用，等待管理员迁移；不会把旧 QQ 号/群号猜成 UMO。
"""

_MISSING = object()


def validate_umo_list(value) -> str:
    """返回配置错误标识；空字符串表示有效。

    正常空列表（[] / () / set()）表示不限制；显式 null、字符串、数字等
    非法类型一律拒绝。不存在（未配置）由调用方使用空列表兼容新安装。
    """
    if value is None:
        return "invalid_whitelist_umos"
    if not isinstance(value, (list, tuple, set)):
        return "invalid_whitelist_umos"
    return ""


def is_umo_allowed(umo: str, whitelist_umos) -> bool:
    """非空白名单要求完整精确匹配；空列表不限制。"""
    error = validate_umo_list(whitelist_umos)
    if error:
        return False
    configured = list(whitelist_umos)
    if not configured:
        return True
    if not isinstance(umo, str) or not umo:
        return False
    return umo in configured


def legacy_restrictions_present(
    whitelist_groups, whitelist_privates
) -> bool:
    """检测旧版非空限制；空列表在旧版表示不限制，不触发迁移暂停。"""
    def present(value) -> bool:
        if value is None:
            return False
        if isinstance(value, (list, tuple, set)):
            return bool(value)
        return bool(value)

    return present(whitelist_groups) or present(whitelist_privates)


def migration_required(
    whitelist_umos,
    whitelist_groups,
    whitelist_privates,
) -> bool:
    """旧版有限制且没有明确新策略时暂停授权。

    唯一的新策略是非空 UMO 列表；不再提供“确认不限制”旁路。
    """
    if not legacy_restrictions_present(whitelist_groups, whitelist_privates):
        return False
    try:
        umos = list(whitelist_umos or [])
    except TypeError:
        umos = []
    if umos:
        return False
    return True


def is_session_allowed(umo: str, whitelist_umos) -> bool:
    """唯一授权入口：只按 UMO 精确匹配。"""
    return is_umo_allowed(umo, whitelist_umos)
