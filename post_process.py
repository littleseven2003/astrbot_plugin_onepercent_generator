"""
AI 响应后处理模块
完全复现原项目 parseAIResponse + stripGeneratedTitleAndGameName 逻辑
"""

import re


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
    与原项目 promptService.js 的 stripGeneratedTitleAndGameName 逻辑完全一致

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
    return result.strip()


def build_post_content(game_name: str, ai_body: str) -> str:
    """
    拼装最终帖子内容
    与原项目 promptService.js 的 buildPostContent 逻辑一致

    最终格式：
    游戏名称：<用户输入的游戏名>
    发售平台：<AI生成>
    游玩时间：<AI生成>
    推荐人群：<AI生成>

    <AI生成的正文>

    Args:
        game_name: 游戏名称（用户输入）
        ai_body: AI 生成的原始内容

    Returns:
        拼装后的帖子内容
    """
    body = strip_generated_title_and_game_name(ai_body)
    return f"游戏名称：{game_name}\n{body}" if body else f"游戏名称：{game_name}"


ACTIVITY_INTRO = "什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390"


def build_final_message(game_name: str, ai_body: str) -> str:
    """
    拼装最终发送给用户的消息
    包含活动说明链接 + 帖子内容

    最终格式：
    什么是【我的百分之一】见帖子说明：https://www.taptap.cn/moment/371075389700702390

    游戏名称：<用户输入的游戏名>
    发售平台：<AI生成>
    游玩时间：<AI生成>
    推荐人群：<AI生成>

    <AI生成的正文>

    Args:
        game_name: 游戏名称
        ai_body: AI 生成的原始内容

    Returns:
        最终消息文本
    """
    post_content = build_post_content(game_name, ai_body)
    return f"{ACTIVITY_INTRO}\n\n{post_content}"
