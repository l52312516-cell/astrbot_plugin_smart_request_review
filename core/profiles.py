"""Normalize protocol profile data without inventing unavailable fields."""


def meaningful(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(
            meaningful(v)
            for k, v in value.items()
            if k not in {"group_id", "group_code"}
        )
    if isinstance(value, list):
        return any(meaningful(v) for v in value)
    return value is not None and value is not False and value != 0


GROUP_FIELDS = {
    "name": ("group_name", "groupName", "name"),
    "remark": ("group_remark", "groupRemark", "remark"),
    "memo": ("group_description", "group_memo", "groupMemo", "memo"),
    "member_count": ("member_count", "memberNum", "member_num"),
    "max_member_count": ("max_member_count", "maxMemberNum", "max_member_num"),
    "level": ("group_level", "groupLevel", "level"),
}


def merge_group(info, data):
    """First valid source wins. Zero group counters are protocol placeholders."""
    if not isinstance(data, dict):
        return
    for field, aliases in GROUP_FIELDS.items():
        if meaningful(info.get(field)):
            continue
        for alias in aliases:
            value = data.get(alias)
            if field in {"member_count", "max_member_count", "level"}:
                try:
                    value = int(value)
                except (ValueError, TypeError, OverflowError):
                    continue
                if value <= 0:
                    continue
            elif isinstance(value, str):
                value = value.strip()
            else:
                continue
            if meaningful(value):
                info[field] = value
                break
