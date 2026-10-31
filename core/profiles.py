"""Normalize protocol profile data without inventing unavailable fields."""

import math
import re

PLACEHOLDERS = {
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "undefined",
    "unknown",
    "未知",
    "不详",
    "无",
    "暂无",
    "未设置",
    "无资料",
}


def clean_text(value):
    """Strip formatting noise and normalize common protocol placeholders."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = str(value).replace("\ufeff", "").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[CQ:[^\]\r\n]*\]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    text = re.sub(r"[\u200e\u200f\u2060]", "", text).strip()
    return "" if text.casefold() in PLACEHOLDERS or text == "0" else text


def meaningful(value):
    if isinstance(value, str):
        return bool(clean_text(value))
    if isinstance(value, dict):
        return any(
            meaningful(v)
            for k, v in value.items()
            if k not in {"group_id", "group_code"}
        )
    if isinstance(value, list):
        return any(meaningful(v) for v in value)
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return value is not None and value is not False and value != 0


def profile_value(data, *keys, default=None):
    """Return the first useful value, skipping empty aliases and placeholders."""
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if not meaningful(value):
            continue
        if isinstance(value, str):
            value = clean_text(value)
        return value
    return default


def profile_text(data, *keys):
    """Read a text alias without letting structured or placeholder values hide it."""
    if not isinstance(data, dict):
        return ""
    return next(
        (clean_text(data.get(key)) for key in keys if clean_text(data.get(key))), ""
    )


def positive_int(value):
    """Parse a positive integral protocol value without truncating decimals."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return (
            int(value)
            if math.isfinite(value) and value.is_integer() and value > 0
            else None
        )
    if isinstance(value, str):
        text = clean_text(value)
        if re.fullmatch(r"[1-9]\d*", text):
            try:
                return int(text)
            except (ValueError, OverflowError):
                return None
    return None


GROUP_FIELDS = {
    "name": ("group_name", "groupName", "name"),
    "remark": ("group_remark", "groupRemark", "remark"),
    "description": ("group_description", "description"),
    "memo": ("group_memo", "groupMemo", "memo", "group_description"),
    "member_count": ("member_count", "memberNum", "member_num"),
    "max_member_count": ("max_member_count", "maxMemberNum", "max_member_num"),
    "level": ("group_level", "groupLevel", "level"),
}


def merge_group(info, data):
    """First valid source wins. Zero group counters are protocol placeholders."""
    if not isinstance(data, dict):
        return
    for field, aliases in GROUP_FIELDS.items():
        # A description used as legacy memo fallback must not hide a later
        # real announcement; preserve both SnowLuma fields when available.
        memo_fallback = (
            field == "memo"
            and meaningful(info.get("description"))
            and info.get("memo") == info.get("description")
            and meaningful(profile_value(data, "group_memo", "groupMemo", "memo"))
        )
        if meaningful(info.get(field)) and not memo_fallback:
            continue
        for alias in aliases:
            value = data.get(alias)
            if field in {"member_count", "max_member_count", "level"}:
                value = positive_int(value)
                if value is None:
                    continue
            elif isinstance(value, str):
                value = clean_text(value)
            else:
                continue
            if meaningful(value):
                info[field] = value
                break
