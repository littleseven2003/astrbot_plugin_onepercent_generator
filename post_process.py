"""
AI 响应后处理模块。

负责清理模型误输出的标题/游戏名，并把结构化正文、活动标题、
活动说明链接和发布前补充提示拼装成用户可复制的消息。
"""

import re

ACTIVITY_INTRO = "什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390"

FIELD_ORDER = (
    "发售平台",
    "游玩时间",
    "推荐人群及理由",
    "游戏介绍",
    "故事/感想",
)
_PLACEHOLDER_VALUES = frozenset({
    "未提供",
    "资料未提供",
    "待补充",
    "用户未提供",
    "用户未填写",
    "暂无",
    "无",
    "未知",
    "不详",
    "n/a",
    "na",
    "none",
    "null",
})
_FIELD_RE = re.compile(r"^\s*([^：:]{1,24})\s*[：:]\s*(.*)$")


def parse_ai_response(text: str) -> dict:
    """
    解析 AI 返回内容，尝试提取标题
    与原项目 aiService.js 的 parseAIResponse 逻辑一致

    Args:
        text: AI 返回的原始文本

    Returns:
        {"title": str, "content": str}
    """
    title_match = re.match(r'^【我的百分之一】\+【(.+?)】', text)
    title = title_match.group(1).strip() if title_match else ''

    body = text[title_match.end():].strip() if title_match else text.strip()

    # 去除开头的"正文："前缀
    body = re.sub(r'^正文：\s*', '', body).strip()

    return {"title": title, "content": body}


def strip_generated_title_and_game_name(text: str) -> str:
    """
    清洗 AI 误输出的标题、游戏名称行等
    与原项目 promptService.js 的 stripGeneratedTitleAndGameName 逻辑一致

    Args:
        text: AI 返回内容

    Returns:
        清洗后的文本
    """
    if not text:
        return ''
    result = text
    result = re.sub(r'^标题：.*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'^【我的百分之一】\+【.*】.*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'^游戏名称：.*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'^正文：\s*', '', result, flags=re.MULTILINE)
    result = re.sub(
        r'^.*什么是【我的百分之一】.*$', '', result, flags=re.MULTILINE
    )
    result = re.sub(
        r'^.*taptap\.cn/moment/371075389700702390.*$',
        '',
        result,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    return result.strip()


def _canonical_field(label: str) -> str:
    label = label.strip()
    if "发售平台" in label:
        return "发售平台"
    if "游玩时间" in label:
        return "游玩时间"
    if "推荐" in label:
        return "推荐人群及理由"
    if "游戏介绍" in label:
        return "游戏介绍"
    if "故事" in label or "感想" in label:
        return "故事/感想"
    return label


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("。；;,.，")
    if not normalized:
        return True
    low = normalized.lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    return (
        normalized.startswith("资料未提供")
        or normalized.startswith("用户未提供")
        or normalized.startswith("用户未填写")
        or normalized.startswith("待补充")
    )


def prepare_article_body(
    ai_body: str, has_experience: bool = False
) -> tuple[str, list[str]]:
    """清理正文占位值，并返回正文外需要用户补充的字段。

    - 占位值不进入正文；
    - 未提供个人经历时，删除“故事/感想”字段及其后续段落；
    - 缺失字段只在正文外提示，不伪造内容。
    """
    body = strip_generated_title_and_game_name(ai_body or "")
    lines = body.splitlines()
    cleaned: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    skip_story = False

    for line in lines:
        match = _FIELD_RE.match(line)
        if match:
            raw_label, value = match.group(1), match.group(2)
            canonical = _canonical_field(raw_label)
            if canonical == "故事/感想" and not has_experience:
                skip_story = True
                if canonical not in missing:
                    missing.append(canonical)
                continue
            if canonical in FIELD_ORDER:
                skip_story = False
                seen.add(canonical)
            if skip_story:
                continue
            if _is_placeholder(value):
                if canonical in FIELD_ORDER and canonical not in missing:
                    missing.append(canonical)
                continue
            cleaned.append(line.rstrip())
            continue

        if skip_story:
            continue
        cleaned.append(line.rstrip())

    if not has_experience and "故事/感想" not in missing:
        missing.append("故事/感想")

    for label in FIELD_ORDER:
        if label == "故事/感想" and not has_experience:
            if label not in missing:
                missing.append(label)
        elif label not in seen and label not in missing:
            missing.append(label)

    ordered_missing = [label for label in FIELD_ORDER if label in missing]
    body = "\n".join(cleaned).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body, ordered_missing


def article_body_length(ai_body: str, has_experience: bool = False) -> int:
    """统计清理后正文的非空白字符数，不把标题/提示算入。"""
    body, _missing = prepare_article_body(ai_body, has_experience)
    return len(re.sub(r"\s+", "", body))


def build_post_content(
    game_name: str, ai_body: str, has_experience: bool = False
) -> str:
    """拼装正文区：游戏名称 + 清理后的正文（不含活动标题/说明）。"""
    body, _missing = prepare_article_body(ai_body, has_experience)
    return f"游戏名称：{game_name}\n{body}" if body else f"游戏名称：{game_name}"


def build_final_message(
    game_name: str,
    ai_body: str,
    experience: str = "",
    warnings: list[str] | None = None,
) -> str:
    """拼装可复制的标题、活动说明和正文。

    不把“未提供/待补充”等占位值放进正文；缺失信息、长度不足和
    截断提示放在正文外，并说明插件不会自动发布或提交活动。
    """
    has_experience = bool(str(experience or "").strip())
    body, missing = prepare_article_body(ai_body, has_experience)
    title = f"【我的百分之一】+【{game_name}】"
    post = build_post_content(game_name, body, has_experience)

    notes: list[str] = []
    if missing:
        notes.append("正文暂缺（不属于正文）：" + "、".join(missing))
    if not has_experience:
        notes.append(
            "未提供个人经历。客观介绍不能替代个人故事，"
            "请勿把搜索结果写成自己的游玩经历。"
        )
    if missing or not has_experience:
        notes.append(
            f"补充方式：小作文 {game_name} 经历：<游玩时间、故事或感想>"
        )
    for warning in warnings or []:
        if warning:
            notes.append(str(warning))
    notes.append(
        "请复制标题和正文后自行发布；插件不会自动发布、添加图片或提交活动。"
    )
    notes.append("生成内容仍可能不准确，发布前请自行核对。")

    final = f"{title}\n\n{ACTIVITY_INTRO}\n\n{post}"
    return final + "\n\n---\n📝 发布前提示（不属于正文）：\n" + "\n".join(notes)
