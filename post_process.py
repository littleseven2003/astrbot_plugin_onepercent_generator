"""
AI 响应后处理模块。

负责清理模型误输出的标题/游戏名，并把结构化正文、活动标题和
活动说明链接拼装成用户可复制的消息；不追加缺失字段或长度提示。
"""

import re

ACTIVITY_INTRO = (
    "什么是【我的百分之一】见帖子说明：\n"
    "https://www.taptap.cn/moment/371075389700702390\n"
    "（这个需要加，省得新玩家不知道是啥情况）"
)

PREFIX_FIELDS = ("发售平台", "游玩时间", "推荐人群")
# 官方活动字段写法（输出用）；内部仍用上面的规范键做解析。
PREFIX_LABELS = {
    "发售平台": "游戏当时发售平台",
    "游玩时间": "玩游戏的时间",
    "推荐人群": "推荐人群（理由）",
}
# 用户拍板：字段缺失时按游戏情况给完整自然的兜底，不再暴露
# 「【请补充……】」这类半成品占位符。正常路径由写作模型补全，
# 这里只在模型确实漏填时兜底。
PENDING_MARKERS = {
    "发售平台": "多平台",
    "游玩时间": "玩了一段时间",
    "推荐人群": "喜欢这类游戏的玩家",
}
BODY_FALLBACK = "这款游戏有自己的节奏，适合慢慢体验，具体感受等你自己上手。"

_TAIL_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:许愿卡牌?|游戏\s*[Ii][Dd]|账号\s*[Ii][Dd]|区服|服务器|系统)\s*(?:\*\*)?\s*[:：]"
)
_PLAY_TIME_LABEL_RE = re.compile(
    r"(?:玩游戏的时间|游玩时间|游戏时间|玩的时间|入坑时间|入坑的?时间)"
    r"\s*[：:]\s*([^\n]+)"
)
# 明确日期：2024年8月 / 2024年8月3日。
_PLAY_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*年"
    r"(?:\s*(?:1[0-2]|0?[1-9])\s*月)?"
    r"(?:\s*(?:3[01]|[12]\d|0?[1-9])\s*[日号])?"
)
# 小时/分钟/天/周/个月这类短时长；支持「半小时」「30 分钟」。
_PLAY_DURATION_RE = re.compile(
    r"(?:约|大约|差不多|将近|近|超过|不到)?\s*"
    r"(?:\d+(?:\.\d+)?\s*个?\s*(?:小时|钟头|分钟|分|天|周|星期|个月)"
    r"|半\s*个?\s*小时|半个小时)"
)
# 「年」只在明确谈游玩时才成立，避免把「发售 10 年了」当成游玩时间。
_PLAY_YEARS_RE = re.compile(
    r"(?:玩|游玩|肝|打|入坑)\s*(?:了|过)?\s*(?:约|大约|差不多|将近|近)?\s*"
    r"(\d+(?:\.\d+)?)\s*年"
)
_PLAY_SEASON_RE = re.compile(
    r"(?:(?:去年|前年|今年|那年|大前年|初中|高中|大学|小学|儿时|小时候)的?)?"
    r"(?:暑假|寒假|春天|夏天|秋天|冬天|国庆|五一|十一|春节|元旦|中秋|端午|清明|长假|假期)"
)
# 相对日期/时期：昨晚、上周、上个月这类，单独出现也能表示开始玩的时间。
_PLAY_RELATIVE_RE = re.compile(
    r"(?:昨晚|昨天晚上|昨天|前天|今天|今早|上周[一二三四五六日天]?|上周末|"
    r"上个月|上上个月|月初|月底|小时候|儿时|童年|初中时|高中时|大学时|"
    r"去年|前年|今年|那年)"
)
# 自由文本按句/分句切分：时间必须和游玩动作出现在同一分句里。
# ASCII 的 . 与 , 只在不是小数时当分隔符，避免「1.5 小时」被切成两段。
_PLAY_CLAUSE_SPLIT_RE = re.compile(r"(?:[，。；;!！?？\n]+|(?<!\d)[.,](?!\d))")
_PLAY_ACTION_RE = re.compile(
    r"(?:开始玩|开玩|玩|游玩|入坑|肝|打通|沉迷|上手)"
)
# 计划/未发生的动作不算已完成游玩：想玩、打算玩、没玩过、没打开过…
_PLAY_INTENT_CHARS = frozenset("想要打算准备计划没未不别")
# 动作前的计划语气可以夹「先/再/稍后」等词：打算先玩、准备以后再玩。
_INTENT_BEFORE_RE = re.compile(
    r"(?:打算|准备|计划|想要|想|要|考虑|预计|估计|决定)"
    r"[^，。；;,.!！?？\n]{0,3}\s*$"
)
# 时长必须紧跟在本人游玩动作之后，中间只允许这些语法成分（程度、语法助词、
# 范围词、连接词）。出现「还加班了」这类别的动作内容就说明时长不归这次游玩。
_PLAY_TIME_LINK_RE = re.compile(
    r"^(?:\s|了|过|的|就|也|又|大概|大约|约|差不多|将近|近|超过|不到|整整|足足|"
    r"一共|总共|连续|断断续续|有|只|才|都|加起来|前后|左右|到现在|至今|目前)*$"
)
_PLAY_GAP_LIMIT = 8
# 紧挨动作的否定：还没有玩过 / 还没真正玩过 / 没怎么玩 / 别玩了。
# 允许「没」与动作之间出现一两个程度副词；「好不容易」不算否定。
_NEGATION_BEFORE_RE = re.compile(
    r"(?:没|未|不|别)(?:有|能|再|曾|过|真正|真的|实际|怎么|开始|去|在)?\s*$"
)
# 动作的主语是他人（别人玩了 / 看哥哥玩 / 朋友在旁边看），不是发帖者本人。
_OTHER_PERSON_RE = re.compile(
    r"(?:别人|他人|朋友|室友|同学|同事|家人|亲戚|群友|主播|网友|大佬|"
    r"哥哥|姐姐|弟弟|妹妹|爸爸|妈妈|父亲|母亲|老公|老婆|男友|女友|对象|"
    r"孩子|小孩|楼主|层主|up主|UP主|他们|她们|他|她)"
)
_COMPANION_CHARS = frozenset("和跟与同陪")
# 等下载、安装、发售、等开黑这类分句说的是等待/安装/游戏年龄，不是游玩时间。
_NON_PLAY_CLAUSE_RE = re.compile(
    r"(?:下载|安装|等了|等待|排队|预约|预下载|解压|购买|退款|抽卡|氪金|"
    r"等开黑|等人|等队友|等朋友|等更新)"
)
# 「发售那年暑假开始玩」有玩家自己的时点依据，可结合已核实的首发信息补全。
_PLAY_RELEASE_SEASON_RE = re.compile(
    r"(?:发售|发行|上市|公测|开服|上线|推出)(?:那|当)?年?(?:的)?"
    r"(?:暑假|寒假|春天|夏天|秋天|冬天|国庆|春节)"
)


def extract_user_play_time(experience: str) -> str:
    """只从用户文本里取游玩时间，模型不能改写或新增。

    优先保留用户明确填写的「玩游戏的时间：…」字段；自由文本按分句
    处理，时间必须和游玩动作出现在同一分句里，等下载、安装、发售这类
    分句整体跳过；同一分句取最早出现且最长的匹配（「去年冬天」优先于
    「去年」）。「年」只在明确谈游玩时成立，无法确认就省略。
    """
    text = str(experience or "")
    label = _PLAY_TIME_LABEL_RE.search(text)
    if label:
        value = label.group(1).strip()
        value = value.split("；")[0].split(";")[0].strip()
        value = value.rstrip("。.,，、!！")
        if value and value not in _PLACEHOLDER_VALUES:
            return value[:24]
    for clause in _PLAY_CLAUSE_SPLIT_RE.split(text):
        clause = clause.strip()
        if not clause or not _PLAY_ACTION_RE.search(clause):
            continue
        # 等下载/安装/发售分句里的时长不是游玩时间，无法确认就省略。
        if _NON_PLAY_CLAUSE_RE.search(clause):
            continue
        # 候选：(start, end, -len, value, is_point)。is_point 表示日期/时期
        # 这类“时点”，只有它们允许出现在游玩动作之前（去年冬天开始玩）；
        # 时长（小时/天/年）必须由动作在前直接修饰，避免把「加班 6 小时后
        # 才开始玩」当成游玩时长。
        point_patterns = (
            _PLAY_DATE_RE,
            _PLAY_SEASON_RE,
            _PLAY_RELATIVE_RE,
            _PLAY_RELEASE_SEASON_RE,
        )
        candidates = []
        for pattern in (
            _PLAY_DATE_RE,
            _PLAY_DURATION_RE,
            _PLAY_YEARS_RE,
            _PLAY_SEASON_RE,
            _PLAY_RELATIVE_RE,
            _PLAY_RELEASE_SEASON_RE,
        ):
            # 同一分句可能有多处时间（30 小时但我只玩了 2 小时），全部纳入后
            # 再按“是否紧跟已完成游玩动作”挑选，不能只取第一处。
            for match in pattern.finditer(clause):
                if pattern is _PLAY_YEARS_RE:
                    value = f"{match.group(1)} 年"
                    start = match.start(1)
                else:
                    value = match.group(0).strip()
                    start = match.start()
                candidates.append(
                    (start, match.end(), -len(value), value,
                     pattern in point_patterns)
                )
        if not candidates:
            continue
        # 同一位置优先取更长的表述：去年冬天 优先于 去年。
        candidates.sort(key=lambda item: (item[0], -item[1]))
        # 第一优先级：时间前面有已完成的游玩动作（玩了 1.5 小时）。
        completed = [
            item for item in candidates
            if _completed_play_before(clause, item[0])
        ]
        if completed:
            return completed[0][3]
        # 第二优先级：只有日期/时点可以放在动作之前（去年冬天开始玩、
        # 昨晚才开始玩）；时长绝不能走这个分支。
        followed = [
            item for item in candidates
            if item[4] and _play_action_follows(clause, item[1])
        ]
        if followed:
            return followed[0][3]
    return ""


def _action_is_poster_own(clause: str, start: int) -> bool:
    """动作是否是发帖者本人已完成的游玩。

    排除计划（想玩/打算玩）、否定（还没有玩过/没玩过），以及他人主语
    （别人玩了/看朋友玩）；「跟朋友一起玩」这类同伴表达仍算本人。
    """
    previous = clause[start - 1:start]
    if previous in _PLAY_INTENT_CHARS:
        return False
    if _INTENT_BEFORE_RE.search(clause[max(0, start - 8):start]):
        return False
    if _NEGATION_BEFORE_RE.search(clause[max(0, start - 6):start]):
        return False
    window_start = max(0, start - 8)
    window = clause[window_start:start]
    for person in _OTHER_PERSON_RE.finditer(window):
        absolute = window_start + person.start()
        if _is_companion(clause, absolute):
            continue
        return False
    return True


def _is_companion(clause: str, person_start: int) -> bool:
    """他人名词是否作为同伴出现，仍算发帖者本人。

    「和/跟/与/同/陪 + 朋友」「我和我的朋友」算同伴（发帖者在场一起玩）；
    「我的朋友玩了」不算——那是朋友自己的游玩。
    """
    previous = clause[person_start - 1:person_start]
    if previous in _COMPANION_CHARS:
        return True
    if previous == "的":
        # 物主代词前必须还带有「和/跟/与/同/陪」，才说明发帖者本人也在场。
        owner = clause[max(0, person_start - 4):person_start - 1]
        return (
            any(char in owner for char in "我俺咱")
            and any(char in owner for char in _COMPANION_CHARS)
        )
    return False


def _completed_play_before(clause: str, position: int) -> bool:
    """时间前面是否有发帖者本人已完成的游玩动作。

    动作与时间之间若插入他人（「我玩的时候朋友在旁边看了 6 小时」），
    这个时长归属不明确，不能算成本人的游玩时间。
    """
    for match in _PLAY_ACTION_RE.finditer(clause, 0, position):
        if not _action_is_poster_own(clause, match.start()):
            continue
        between = clause[match.end():position]
        if _OTHER_PERSON_RE.search(between):
            continue
        # 时长必须紧跟在本人游玩动作之后，中间只接受已支持的语法成分；
        # 结构不符（如「玩游戏那天我还加班了 6 小时」）说明时长另有归属。
        if not _PLAY_TIME_LINK_RE.match(between):
            continue
        return True
    return False


def _play_action_follows(clause: str, position: int) -> bool:
    """时间后面是否紧跟（允许少量连接词）本人已完成的游玩动作。"""
    window = clause[position:position + _PLAY_GAP_LIMIT]
    for match in _PLAY_ACTION_RE.finditer(window):
        if _action_is_poster_own(clause, position + match.start()):
            return True
    return False
FIELD_ORDER = (*PREFIX_FIELDS, "游戏介绍", "故事/感想")
OPTIONAL_TAIL_FIELDS = ("许愿卡", "游戏ID", "账号ID", "区服", "系统")
_BODY_LABELS = ("游戏介绍", "故事/感想")
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
    result = re.sub(
        r'^[*_\s]*游戏名称[*_\s]*[:：].*$', '', result, flags=re.MULTILINE
    )
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
    if any(
        key in label
        for key in ("游玩时间", "玩游戏的时间", "游戏时间", "游玩时长")
    ):
        return "游玩时间"
    if "推荐" in label:
        return "推荐人群"
    if "游戏介绍" in label:
        return "游戏介绍"
    if any(key in label for key in ("故事", "感想", "心得")):
        return "故事/感想"
    return label


def _parse_field_line(line: str) -> tuple[str, str] | None:
    """解析普通/Markdown 加粗字段行，识别活动原文等价写法。"""
    raw = line.strip()
    raw = re.sub(r"^[*_\s]+", "", raw)
    match = re.match(r"^([^：:]{1,32}?)\s*[：:]\s*(.*)$", raw)
    if not match:
        return None
    label = re.sub(r"[*_\s]+$", "", match.group(1)).strip()
    value = match.group(2).strip()
    value = re.sub(r"^[*_\s]+", "", value)
    value = re.sub(r"[*_\s]+$", "", value)
    canonical = _canonical_field(label)
    if canonical not in FIELD_ORDER:
        return None
    return canonical, value


def _is_placeholder(value: str) -> bool:
    normalized = value.strip()
    if "【请补充" in normalized or normalized.startswith("【请补充"):
        return True
    normalized = re.sub(r"^[*_\s]+|[*_\s]+$", "", normalized)
    normalized = normalized.strip().strip("。；;,.，")
    if not normalized:
        return True
    low = normalized.lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    return (
        normalized.startswith("未提供")
        or normalized.startswith("资料未提供")
        or normalized.startswith("用户未提供")
        or normalized.startswith("用户未填写")
        or normalized.startswith("待补充")
        or normalized.startswith("暂无")
    )


def split_article(
    ai_body: str,
    has_experience: bool = False,
    allow_play_time: bool = True,
) -> dict:
    """把模型输出拆成活动前置字段与正文。

    前置字段：发售平台、游玩时间、推荐人群（活动顺位由程序拼装）。
    正文：游戏介绍与故事/感想的小标题会被去掉，合并成自然段；没有
    经历时丢弃“故事/感想”整节，避免编造第一人称内容。
    返回 {"prefix": {...}, "body": str, "missing": [...]}。
    """
    body = strip_generated_title_and_game_name(ai_body or "")
    prefix: dict[str, str] = {}
    paragraphs: list[str] = []
    missing: list[str] = []
    skip_story = False

    for line in body.splitlines():
        # 模型伪造/误输出的许愿卡、游戏 ID、账号、区服、系统一律丢弃；
        # 这些只能由程序按用户明确提供的内容追加。
        if _TAIL_LINE_RE.match(line):
            continue
        parsed = _parse_field_line(line)
        if skip_story and parsed is None:
            # 无经历时整节“故事/感想”都要跳过，空行不会结束该节，
            # 只有遇到下一个可识别字段才恢复。
            continue
        if parsed is not None:
            canonical, value = parsed
            if canonical in _BODY_LABELS:
                if canonical == "故事/感想" and not has_experience:
                    skip_story = True
                    if canonical not in missing:
                        missing.append(canonical)
                    continue
                skip_story = False
                if _is_placeholder(value):
                    if canonical not in missing:
                        missing.append(canonical)
                    continue
                if value:
                    paragraphs.append(value)
                continue
            skip_story = False
            if canonical == "游玩时间" and not allow_play_time:
                # 兼容旧调用：显式关闭时才丢弃模型时间；默认允许模型结合
                # 发售时间和正文故事给出合理时期。
                if canonical not in missing:
                    missing.append(canonical)
                continue
            if _is_placeholder(value):
                if canonical not in missing:
                    missing.append(canonical)
                continue
            prefix[canonical] = value
            continue

        if skip_story:
            continue
        paragraphs.append(line.rstrip())

    text = "\n".join(paragraphs).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    ordered_missing = [
        label for label in FIELD_ORDER if label in missing
    ]
    return {"prefix": prefix, "body": text, "missing": ordered_missing}


def prepare_article_body(
    ai_body: str, has_experience: bool = False
) -> tuple[str, list[str]]:
    """返回（正文, 明确的空字段）。保留旧调用签名。"""
    data = split_article(ai_body, has_experience)
    return data["body"], data["missing"]


def article_body_length(ai_body: str, has_experience: bool = False) -> int:
    """统计清理后正文的非空白字符数，不把标题/提示算入。"""
    body, _missing = prepare_article_body(ai_body, has_experience)
    return len(re.sub(r"\s+", "", body))



# 成文兜底用素材里已知的事实：首发平台与发售日期。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 明确日期：2022 年 2 月 25 日 / 2022 年 2 月 / 2022 年，以及
# 2022-02-25、2022/02/25 这类纯数字写法。
_RELEASE_DATE_RE = re.compile(
    r"((?:19|20)\d{2})\s*年"
    r"(?:\s*(1[0-2]|0?[1-9])\s*月)?"
    r"(?:\s*(3[01]|[12]\d|0?[1-9])\s*[日号])?"
)
_DIGIT_DATE_RE = re.compile(
    r"((?:19|20)\d{2})[-/.]\s*(1[0-2]|0?[1-9])[-/.]\s*"
    r"(3[01]|[12]\d|0?[1-9])(?!\d)"
)
_DATE_PATTERNS = (_RELEASE_DATE_RE, _DIGIT_DATE_RE)
# 每项：(展示名, 代际键, 别名)。先匹配更长更具体的名字，Switch 2 不能
# 退化成 Switch，Xbox One 与 Xbox Series 也必须各自成立。
_PLATFORM_DISPLAY = (
    (
        "Nintendo Switch 2",
        "switch2",
        (r"nintendo\s*switch\s*2", r"switch\s*2", r"\bns2\b", r"任天堂\s*switch\s*2"),
    ),
    (
        "Nintendo Switch",
        "switch",
        (r"nintendo\s*switch(?!\s*2)", r"\bswitch\b(?!\s*[2２])", r"\bns\b"),
    ),
    ("PlayStation 5", "ps5", (r"playstation\s*5", r"\bps5\b", r"\bps\s*5\b")),
    ("PlayStation 4", "ps4", (r"playstation\s*4", r"\bps4\b", r"\bps\s*4\b")),
    ("Xbox Series X|S", "xboxseries", (r"xbox\s*series", r"xboxseries")),
    ("Xbox One", "xboxone", (r"xbox\s*one", r"xboxone")),
    ("PC", "pc", (r"\bpc\b", r"\bsteam\b", r"\bwindows\b")),
    ("iOS", "ios", (r"\bios\b", r"\biphone\b", r"\bipad\b")),
    ("Android", "android", (r"\bandroid\b", r"安卓")),
)
_LAUNCH_PLATFORM_PATTERNS = (
    re.compile(r"首发平台\s*(?:仅为|为|是|仅|包括|包含|[:：])?\s*([^\n。；;，,]+)"),
    re.compile(r"首发于\s*([^\n。；;，,]+)"),
)
# 首发短语只修饰紧挨着的平台或连续平台列表：遇到后续动作/移植/换年就结束。
_LAUNCH_FOLLOW_UP_RE = re.compile(
    r"(?:并|且|随后|之后|后来|同时|又|再|次年|翌年|于\s*(?:19|20)\d{2})"
)
# 否定说法的平台不是首发依据：「首发平台不包括 PS5」不能当 PS5。
_PLATFORM_NEGATION_RE = re.compile(r"^\s*(?:不|非|未|没有|并非|不含)")
_RELEASE_VERBS = re.compile(r"发售|上市|首发|推出|发行|上线")


def _normalize_platform_text(value: str) -> str:
    text = re.sub(r"^[\s:：,，、仅]+", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" 。；;,，")
    return text


def platform_generations(value: str) -> set[str]:
    """把平台说法归并到代际键：PS5/PlayStation 5 同代，Switch 与 Switch 2 不同代。"""
    text = str(value or "")
    generations = set()
    for _display, generation, aliases in _PLATFORM_DISPLAY:
        if any(re.search(alias, text, re.IGNORECASE) for alias in aliases):
            generations.add(generation)
    return generations


def extract_launch_platform(facts: str) -> str:
    """从素材里提取明确写出的首发平台（如「首发平台仅 Nintendo Switch」）。

    只认紧跟首发短语的平台或连续平台列表：「首发于 PC 并于次年移植到
    PS5」里的 PS5 是后续移植，不算首发平台。
    """
    text = str(facts or "")
    for pattern in _LAUNCH_PLATFORM_PATTERNS:
        for match in pattern.finditer(text):
            candidate = _normalize_platform_text(match.group(1))
            # 「不包括 / 不含 / 非」是排除说法，不能当首发依据，继续找正例。
            if _PLATFORM_NEGATION_RE.match(candidate):
                continue
            candidate = _normalize_platform_text(
                _LAUNCH_FOLLOW_UP_RE.split(candidate)[0]
            )
            found = []
            for display, _generation, aliases in _PLATFORM_DISPLAY:
                positions = [
                    match.start()
                    for alias in aliases
                    if (match := re.search(alias, candidate, re.IGNORECASE))
                ]
                if positions:
                    found.append((min(positions), display))
            if found:
                # 按素材里出现的先后顺序输出平台列表。
                found.sort()
                return " / ".join(display for _position, display in found)
    return ""


def _iter_date_parts(text: str):
    """按出现位置产出（位置, 年, 月|None, 日|None）；中文与纯数字写法都认。"""
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            yield (
                match.start(),
                int(match.group(1)),
                int(match.group(2)) if match.group(2) else None,
                int(match.group(3)) if match.group(3) else None,
            )


def extract_release_date(facts: str) -> tuple[int, int | None, int | None] | None:
    """素材里与发售/上市/首发等动词同句的最早日期（尽量精确到月日）。"""
    dates = []
    for sentence in re.split(r"[。；;\n]", str(facts or "")):
        if not _RELEASE_VERBS.search(sentence):
            continue
        for _position, year, month, day in _iter_date_parts(sentence):
            dates.append((year, month, day))
    if not dates:
        return None
    return min(dates, key=lambda item: (item[0], item[1] or 0, item[2] or 0))


def extract_release_year(facts: str) -> int | None:
    """从素材里提取与发售/上市/首发等动词同句的最早年份。"""
    release = extract_release_date(facts)
    return release[0] if release else None


def _play_time_predates_release(
    value: str, release: tuple[int, int | None, int | None]
) -> bool:
    """模型时间是否早于素材里已知的发售日期。

    素材只确定到年就只比年份；有月份/日期时按能确定的精度比较，同年
    1 月、2 月 24 日都算早于 2 月 25 日发售；精度不明（只写「2022 年
    春天」）时不判穿帮。
    """
    parts = sorted(_iter_date_parts(str(value or "")), key=lambda item: item[0])
    if not parts:
        return False
    _position, year, month, day = parts[0]
    release_year, release_month, release_day = release
    if year != release_year:
        return year < release_year
    if release_month is None or month is None:
        return False
    if month != release_month:
        return month < release_month
    if release_day is None or day is None:
        return False
    return day < release_day


def _resolve_platform(prefix: dict, explicit: str, facts: str) -> str:
    if explicit:
        return explicit
    model_value = str(prefix.get("发售平台") or "").strip()
    fact_value = extract_launch_platform(facts)
    if not model_value or _is_placeholder(model_value):
        return fact_value or PENDING_MARKERS["发售平台"]
    if fact_value:
        fact_generations = platform_generations(fact_value)
        model_generations = platform_generations(model_value)
        # 素材明确首发平台时，模型写的代际必须落在素材范围内：素材只有
        # Switch 2 就不能放行 Switch，只有 Xbox One 就不能放行 Series。
        if fact_generations and (
            not model_generations or not model_generations <= fact_generations
        ):
            return fact_value
    return model_value


def _resolve_play_time(prefix: dict, explicit: str, facts: str) -> str:
    if explicit:
        return explicit
    value = str(prefix.get("游玩时间") or "").strip()
    if _is_placeholder(value):
        return PENDING_MARKERS["游玩时间"]
    release = extract_release_date(facts)
    if release and _play_time_predates_release(value, release):
        # 明显穿帮（早于已知发售月日）：换成发售后不久，不写具体假日期。
        return f"{release[0]} 年发售后不久"
    return value or PENDING_MARKERS["游玩时间"]

def _prefix_lines(prefix: dict[str, str]) -> list[str]:
    return [
        f"{PREFIX_LABELS.get(label, label)}：{prefix[label]}"
        for label in PREFIX_FIELDS
        if prefix.get(label)
    ]


def build_post_content(
    game_name: str, ai_body: str, has_experience: bool = False
) -> str:
    """拼装正文区：游戏名称 + 前置字段 + 正文（不含活动标题/说明）。"""
    data = split_article(ai_body, has_experience)
    lines = [f"游戏名称：{game_name}"]
    lines.extend(_prefix_lines(data["prefix"]))
    content = "\n".join(lines)
    if data["body"]:
        content += "\n\n" + data["body"]
    return content


def build_final_message(
    game_name: str,
    ai_body: str,
    experience: str = "",
    extras: dict | None = None,
    explicit_fields: dict | None = None,
    known_facts: str = "",
) -> str:
    """拼装可复制的标题、活动说明和帖子内容。

    严格按官方模板：标题、活动说明与链接、游戏名称、游戏当时发售平台、
    玩游戏的时间、推荐人群（理由）、正文，五个固定字段逐行存在。用户
    显式填写或文本里明确给出的时间/平台优先；都没有时该行输出醒目的
    待补占位；用户显式填写优先，模型没填时给自然的兜底文案，保证输出
    是可直接复制的完整帖子。
    """
    experience_text = str(experience or "").strip()
    has_experience = bool(experience_text)
    fields = explicit_fields or {}
    explicit_time = str(fields.get("游玩时间") or "").strip()
    user_play_time = explicit_time or extract_user_play_time(experience_text)
    # 用户没给时间时允许模型结合发售时间与正文故事补一个合理时期；
    # 用户给过的时间仍由下面的显式字段覆盖。
    data = split_article(ai_body, has_experience, True)
    if user_play_time:
        data["prefix"]["游玩时间"] = user_play_time
    explicit_platform = str(fields.get("发售平台") or "").strip()
    data["prefix"]["发售平台"] = _resolve_platform(
        data["prefix"], explicit_platform, known_facts
    )
    data["prefix"]["游玩时间"] = _resolve_play_time(
        data["prefix"], user_play_time, known_facts
    )
    title = f"【我的百分之一】+【{game_name}】"
    explicit_audience = str(fields.get("推荐人群") or "").strip()
    if explicit_audience:
        data["prefix"]["推荐人群"] = explicit_audience
    lines = [f"游戏名称：{game_name}"]
    for label in PREFIX_FIELDS:
        value = str(data["prefix"].get(label) or "").strip()
        if _is_placeholder(value):
            value = ""
        # 缺失时给完整自然的兜底文案，不再输出「【请补充……】」。
        lines.append(f"{PREFIX_LABELS[label]}：{value or PENDING_MARKERS[label]}")
    content = "\n".join(lines)
    # 官方模板：正文标签与首段同一行，后续段落保留原有换行。
    content += "\n正文：" + (data["body"].lstrip() if data["body"] else BODY_FALLBACK)
    tail = []
    for label in OPTIONAL_TAIL_FIELDS:
        value = str((extras or {}).get(label) or "").strip()
        if value:
            tail.append(f"{label}：{value}")
    if tail:
        content += "\n\n" + "\n".join(tail)
    return f"{title}\n\n{ACTIVITY_INTRO}\n{content}"
