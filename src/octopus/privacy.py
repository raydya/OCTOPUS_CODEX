"""Conservative lexical admission checks, not a semantic privacy proof.

These checks reject clear requests before they reach a model or a provider.
They cannot recognize every paraphrase, inference or multi-turn attack. The
data broker must independently restrict execution to source-backed read-only
pro queries and non-personal result fields allowed by its own policy.
"""
from __future__ import annotations

import re
import unicodedata


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in normalized
                   if unicodedata.category(char) != "Cf" and not "\ufe00" <= char <= "\ufe0f")


_PERSONAL = re.compile(
    r"姓名|手机号(?:码)?|手机号码|电话号码|联系电话|联系方式|邮箱|电子邮件|"
    r"(?:收货|家庭|居住|联系|通讯|住宅)地址|住址|身份证|证件号|护照|银行卡|银行账号|支付账号|"
    r"(?:个人|用户|会员|客户)(?:的)?(?:账号|账户|帐号|帐户|地址)|"
    r"(?:用户|会员|客户)[\s_-]*(?:id|编号|标识|身份|信息|资料|画像)|"
    r"个人(?:信息|资料|数据|记录|明细|标识|id)|设备[\s_-]*(?:id|编号|序列号|标识|指纹|信息)|"
    r"\b(?:user[\s_-]*id|customer[\s_-]*id|member[\s_-]*id|device[\s_-]*id|"
    r"open[\s_-]*id|union[\s_-]*id|uid|imei|imsi|idfa|oaid|"
    r"phone(?:[\s_-]*numbers?)?|mobile(?:[\s_-]*numbers?)?|e[\s_-]*mail|"
    r"(?:real|full|first|last)[\s_-]*names?|(?:home|shipping|residential|contact)[\s_-]*addresses?|"
    r"(?:user|customer|member|person|personal)[\s_-]*(?:names?|addresses?|accounts?|identity)|"
    r"passports?|"
    r"personal[\s_-]*(?:data|information|records?))\b", re.ASCII,
)
_CODE = re.compile(
    r"代码|源码|逻辑|实现|校验|验证|正则|字段|函数|定义|数据结构|调用链|"
    r"脱敏|加密|哈希|解析|生成规则|处理流程|"
    r"\b(?:code|source|logic|implementation|implement|validation|validate|"
    r"schema|field|function|definition|regex|hash|masking|encryption)\b"
)
_EXPLAIN = re.compile(
    r"怎么|如何|怎样|为什么|为何|解释|说明|分析|实现|逻辑|规则|含义|定义|设计|原因|机制|流程|"
    r"\b(?:how|why|explain|describe|design|code|logic|implementation|validation|schema)\b"
)
_EXTRACT = re.compile(
    r"给我|发我|提供|列出|展示|显示|返回|获取|查出|导出|下载|拉取|检索|搜索|"
    r"查询|查一下|查下|找出|打印|复制|泄露|泄漏|披露|还原|"
    r"\b(?:show|give|list|return|get|fetch|retrieve|find|search|query|export|download|dump)\b"
)
_ROWS = re.compile(
    r"明细|列表|名单|全量|全部|所有|真实|原始|具体值|实际值|(?:字段|列)(?:的)?(?:值|内容)|"
    r"\b(?:all|every|actual|raw|details?|records?|rows?|values?)\b"
)
_BEHAVIOR = re.compile(
    r"为什么|为何|怎么实现|如何实现|怎样实现|怎么处理|如何处理|原因|机制|流程|逻辑|"
    r"校验|验证|格式|定义|规则|含义|"
    r"\b(?:why|how.*(?:works?|implemented)|implementation|validation|definition|logic)\b"
)
_LITERAL = re.compile(
    r"(?<!\d)1[3-9]\d{9}(?!\d)|"
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b|"
    r"(?:用户|客户|会员|账号|账户|uid|user[_ -]?id)\s*[=:：]?\s*\d{3,}"
)
_TECHNICAL_SUFFIX = re.compile(
    r"^(?:的)?(?:字段|格式|校验|验证|脱敏|加密|哈希|生成|解析|匹配|规则|处理|逻辑|代码|"
    r"(?:如何|怎么|怎样)(?:校验|验证|生成|处理|脱敏|加密)|为空|缺失|格式错误)|"
    r"^\s*(?:field|validation|format|regex|generation|is generated|is validated)\b"
)
_TECHNICAL_PREFIX = re.compile(
    r"(?:校验|验证|脱敏|加密|哈希|解析|处理|生成|匹配|保护)(?:这些|用户的)?$|"
    r"\b(?:validating|validate|hashing|masking|encrypting|generating)\s*$"
)


def _code_only(clause: str) -> bool:
    if not _CODE.search(clause) or not _EXPLAIN.search(clause) or _LITERAL.search(clause):
        return False
    if _EXTRACT.search(clause) and _ROWS.search(clause):
        return False
    matches = list(_PERSONAL.finditer(clause))
    return bool(matches) and all(
        _TECHNICAL_SUFFIX.search(clause[match.end():match.end() + 32])
        or _TECHNICAL_PREFIX.search(clause[max(0, match.start() - 32):match.start()])
        for match in matches
    )


def privacy_request_denied(text: str) -> bool:
    """Reject explicit personal-data retrieval requests.

    Technical questions about fields or validation may be answered from code;
    mentioning "code" does not excuse a separate request for personal values.
    """
    if not isinstance(text, str) or len(text) > 32000:
        return True
    normalized = _normalize(text)
    # Remove spacing between Chinese characters without joining English words.
    normalized = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)
    if _LITERAL.search(normalized):
        return True
    # This exclusion is a restriction on the requested output, not permission
    # to fetch any personal value mentioned elsewhere in the same question.
    normalized = re.sub(r"不(?:含|包含)(?:任何)?个人标识", "", normalized)
    for clause in re.split(r"[，,。；;!?！？\n]|(?:并且|同时|然后|再给|再查)", normalized):
        if _BEHAVIOR.search(clause) and not _EXTRACT.search(clause) and not _ROWS.search(clause):
            continue
        if _PERSONAL.search(clause) and not _code_only(clause):
            return True
    return False


_DIRECT_DATA_TOOL = re.compile(r"\bd\s*m\s*s\b|数据库管理服务", re.ASCII)
_DIRECT_SQL = re.compile(
    r"(?:执行|运行|提交|跑|调用|使用|用)\s*(?:这条|以下|下面的|这个|一下)?\s*sql|"
    r"sql\s*[:：]|```\s*sql\b|\b(?:execute|run|submit)\s+(?:this\s+)?sql\b|"
    r"\bselect\b[\s\S]{0,400}\bfrom\b|\bwith\s+\w+\s+as\s*\(|"
    r"\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bdelete\s+from\b|"
    r"\b(?:drop|alter|truncate|create)\s+(?:table|database|view)\b", re.ASCII,
)
_BYPASS_CODE = re.compile(
    r"(?:跳过|绕过|不用|不要|无需|不必|别|忽略|省略)[^，,。；;!?！？\n]{0,16}(?:代码|源码)|"
    r"(?:直接|只要|只需)[^，,。；;!?！？\n]{0,12}(?:查库|查数据库|查生产|查pro|取数)|"
    r"\b(?:skip|bypass|ignore)\b[^\n.!?;]{0,32}\b(?:code|source|evidence)\b|"
    r"\bwithout\s+(?:reading|checking|searching)\s+(?:the\s+)?(?:code|source)\b"
)


def dms_request_allowed(text: str) -> bool:
    """A user naming a data tool, supplying SQL or bypassing code grants nothing."""
    if privacy_request_denied(text):
        return False
    normalized = _normalize(text)
    return bool(normalized.strip()) and not any(pattern.search(normalized) for pattern in (
        _DIRECT_DATA_TOOL, _DIRECT_SQL, _BYPASS_CODE,
    ))


_PHONE_VALUE = re.compile(r"(?<![\dA-Za-z])(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_EMAIL_VALUE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_GOVERNMENT_ID = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_PRIVATE_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])[\"']?(?:user[_-]?id|device[_-]?id|open[_-]?id|union[_-]?id|"
    r"real[_-]?name|full[_-]?name|person[_-]?name|phone(?:[_-]?number)?|mobile(?:[_-]?number)?|"
    r"email|e_mail|address|home[_-]?address|shipping[_-]?address|id[_-]?card|passport|"
    r"姓名|真实姓名|手机号|手机号码|电话号码|邮箱|收货地址|家庭住址|身份证号|用户ID|设备ID)"
    r"[\"']?\s*[:=：]\s*)(?P<value>\[(?:PRIVATE(?: [A-Z]+)?|REDACTED)\]|"
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"[^,\n\r;，；}\]]+?)(?=\s+[\w\u3400-\u9fff-]+\s*[:=：]|[,\n\r;，；}\]]|$)",
    re.I,
)


def redact_private_text(text: str) -> str:
    """Mask common explicit personal-value formats, not arbitrary semantics.

    Business IDs, operator labels and internal free text are retained. Personal
    literals embedded inside those values still receive the same masking. This
    deliberately complements broker field controls and cannot prove that an
    arbitrary text contains no personal information.
    """
    def assignment(match: re.Match) -> str:
        value = match.group("value").strip()
        quoted = len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]
        plain = value[1:-1] if quoted else value
        if re.fullmatch(r"\[(?:PRIVATE(?: [A-Z]+)?|REDACTED)\]", plain):
            return match.group(0)
        if plain.casefold() in {"", "none", "null", "string", "str", "int", "integer", "user_id", "device_id"}:
            return match.group(0)
        # Keep recognizable source references/expressions, but do not treat a
        # bare name or multi-word address as a variable merely for being ASCII.
        if not quoted and (re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", plain)
                           or re.search(r"[(){}+*/]|\s(?:if|else|and|or)\s", plain)):
            return match.group(0)
        replacement = value[0] + "[PRIVATE]" + value[0] if quoted else "[PRIVATE]"
        return match.group("prefix") + replacement

    text = _PRIVATE_ASSIGNMENT.sub(assignment, text)
    text = _PHONE_VALUE.sub("[PRIVATE PHONE]", text)
    text = _EMAIL_VALUE.sub("[PRIVATE EMAIL]", text)
    return _GOVERNMENT_ID.sub("[PRIVATE ID]", text)
