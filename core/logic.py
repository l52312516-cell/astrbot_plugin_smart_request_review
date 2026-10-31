from __future__ import annotations

import json
import re
from typing import Any


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def first_value(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
        return value["data"]
    return value


def normalize_ids(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[,，\s]+", values)
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return [str(item).strip() for item in values if str(item).strip()]


def contains_any(text: str, values: Any) -> list[str]:
    haystack = as_text(text).casefold()
    hits: list[str] = []
    for item in normalize_ids(values):
        if item.casefold() in haystack and item not in hits:
            hits.append(item)
    return hits


def clamp_score(value: Any, maximum: int) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(maximum, score))


def parse_json_object(text: str) -> dict[str, Any] | None:
    raw = as_text(text).strip()
    if not raw:
        return None
    if "```" in raw:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def score_level(
    level: Any, threshold: int, high_threshold: int, one: int, two: int
) -> int:
    value = as_int(level)
    if value >= high_threshold:
        return max(0, two)
    if value >= threshold:
        return max(0, one)
    return 0


def score_range(value: Any, minimum: int, maximum: int, points: int) -> int:
    number = as_int(value)
    if number <= 0:
        return 0
    if minimum <= number <= maximum:
        return max(0, points)
    return 0


def specific_content(value: Any) -> bool:
    """Reward usable request text, not empty greetings or character padding.

    This only measures whether there is content to review; it never claims
    that the supplied identity, purpose, or statements are truthful.
    """
    from .profiles import clean_text

    text = "".join(c for c in clean_text(value).casefold() if c.isalnum())
    if len(text) < 4 or len(set(text)) < 2 or text.isdigit():
        return False
    generic = {
        "你好",
        "您好",
        "你好啊",
        "你好呀",
        "添加好友",
        "请求添加好友",
        "我想添加你为好友",
        "我想加你为好友",
        "加个好友",
        "加我好友",
        "请通过",
        "请通过一下",
        "请同意",
        "通过一下",
        "helloworld",
        "hello",
        "hi",
        "加一下",
        "申请通过",
        "好友申请",
    }
    return text not in generic


def format_value(value: Any, fallback: str = "未知") -> str:
    text = as_text(value)
    return text if text else fallback
