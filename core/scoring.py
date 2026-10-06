from __future__ import annotations

import json
import math
import re
from typing import Any

from .logic import as_text, clamp_score

SCORE_SPECS = {
    "friend": [
        ("friend_verification", "验证信息质量", 3),
        ("friend_content", "申请内容规则", 1),
        ("friend_level", "QQ等级", 2),
        ("friend_avatar", "头像识别", 2),
        ("friend_nickname", "昵称识别", 1),
        ("friend_signature", "签名识别", 1),
    ],
    "group": [
        ("group_profile", "群资料特征", 3),
        ("group_member", "群人数", 2),
    ],
}


def model_result(text: str, maximum: int) -> dict[str, Any]:
    raw = as_text(text).strip()
    if "```" in raw:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "score": 0,
            "state": "unknown",
            "reason": "模型返回不是有效 JSON",
            "tags": [],
        }
    if not isinstance(parsed, dict):
        return {
            "score": 0,
            "state": "unknown",
            "reason": "模型返回不是对象",
            "tags": [],
        }
    score = parsed.get("score")
    if (
        isinstance(score, bool)
        or not isinstance(score, (float, int))
        or not math.isfinite(score)
        or int(score) != score
        or not isinstance(parsed.get("hit"), bool)
        or not isinstance(parsed.get("reason"), str)
        or not isinstance(parsed.get("tags"), list)
    ):
        return {
            "score": 0,
            "state": "unknown",
            "reason": "模型 JSON 字段格式无效",
            "tags": [],
        }
    tags = parsed.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    return {
        "score": clamp_score(parsed.get("score", 0), maximum),
        "state": "scored",
        "reason": as_text(parsed.get("reason"), "无理由"),
        "tags": [as_text(x)[:30] for x in tags[:6]],
    }


def item(
    key: str,
    name: str,
    maximum: int,
    *,
    score: int = 0,
    state: str = "unknown",
    reason: str = "",
    tags: list[str] | None = None,
    provider_id: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "score": max(0, min(maximum, int(score))) if state == "scored" else 0,
        "max": maximum,
        "state": state,
        "available": state == "scored",
        "reason": reason,
        "tags": tags or [],
        "provider_id": provider_id,
    }


def possible_total(record: dict[str, Any]) -> int:
    """Highest score this record can reach with the items that are enabled.

    A request type with fewer enabled items tops out lower (group invites total
    8 after the invite-comment item was removed), so cards and reports must not
    print a denominator nobody can reach. Disabled items are not achievable and
    are excluded; the documented 10 point cap still applies.
    """
    items = record.get("items") or []
    reachable = sum(
        int(x.get("max", 0)) for x in items if x.get("state") != "disabled"
    )
    if reachable <= 0:
        return 10
    return min(10, reachable)


def summary(record: dict[str, Any], limit: int) -> dict[str, str]:
    hard = record.get("hard_rule") or {}
    if hard.get("action") == "approve":
        return {
            "action": "approve",
            "reason": str(hard.get("reason") or "命中允许规则"),
        }
    if hard.get("action") == "reject":
        reason = str(hard.get("reason") or "命中拒绝规则")
        block = int(record.get("rejection_count", 0)) + 1 >= limit and not hard.get(
            "local_blacklist"
        )
        return {
            "action": "block" if block else "reject",
            "reason": reason + (f"；本次拒绝后将达到 {limit} 次" if block else ""),
        }
    if int(record.get("score", 0)) >= int(record.get("threshold", 5)):
        return {
            "action": "approve",
            "reason": f"得分 {record.get('score', 0)}，达到阈值 {record.get('threshold', 5)}",
        }
    low = [
        x["name"] + ("（未知）" if x.get("state") == "unknown" else "")
        for x in record.get("items", [])
        if x.get("state") in {"scored", "unknown"}
        and x.get("score", 0) < x.get("max", 0)
    ]
    reason = f"得分 {record.get('score', 0)}，低于阈值 {record.get('threshold', 5)}" + (
        "；主要未得分项：" + "、".join(low[:3]) if low else ""
    )
    block = int(record.get("rejection_count", 0)) + 1 >= limit
    return {
        "action": "block" if block else "reject",
        "reason": reason + (f"；本次拒绝后将达到 {limit} 次" if block else ""),
    }
