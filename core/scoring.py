from __future__ import annotations

import json
import math
import re
from typing import Any

from .logic import as_text, clamp_score
from .profiles import clean_text

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

# Single source for the conclusion vocabulary: the chat report and the card
# renderer both print these, and the escalation override must not be re-derived
# in two places (that is how the v1.1.8 "总分 0" text drifted out of date).
ACTION_LABELS = {"approve": "建议同意", "reject": "建议拒绝", "block": "建议拉黑"}
ACTION_COLORS = {"approve": "#087f5b", "reject": "#c23b40", "block": "#ad5e08"}
MANUAL_LABEL = "需人工判断"
MANUAL_COLOR = "#ad5e08"
ESCALATION_PREFIX = "自动兜底："


def conclusion_label(record: dict[str, Any]) -> tuple[str, str]:
    """Return the (label, colour) for the conclusion line.

    A pending card that auto mode could not judge must not present a
    data-backed verdict, so it is labelled as needing a human instead; once a
    reviewer has decided, the real conclusion is shown again.
    """
    if record.get("escalation") and record.get("status") == "pending":
        return MANUAL_LABEL, MANUAL_COLOR
    action = (record.get("recommendation") or {}).get("action", "reject")
    return (
        ACTION_LABELS.get(action, ACTION_LABELS["reject"]),
        ACTION_COLORS.get(action, ACTION_COLORS["reject"]),
    )


def escalation_line(record: dict[str, Any]) -> str:
    """Return the「自动兜底」line for an escalated record, or ""."""
    note = as_text(record.get("escalation"))
    return (ESCALATION_PREFIX + note) if note else ""


def model_result(text: str, maximum: int) -> dict[str, Any]:
    def unknown(reason):
        return {"score": 0, "state": "unknown", "reason": reason, "tags": []}

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-finite JSON constant")

    raw = as_text(text).strip()
    if len(raw) > 65536:
        return unknown("模型返回内容过长")
    if "```" in raw:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        parsed = json.loads(
            raw, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
    except (ValueError, OverflowError, RecursionError):
        return unknown("模型返回不是有效 JSON")
    if not isinstance(parsed, dict):
        return unknown("模型返回不是对象")
    score = parsed.get("score")
    # Avoid converting arbitrarily large JSON integers to float: isfinite()
    # itself can overflow. Ordinary over-limit scores still use the old clamp.
    valid_score = (
        not isinstance(score, bool)
        and isinstance(score, (float, int))
        and abs(score) <= 2**53 - 1
        and (isinstance(score, int) or math.isfinite(score))
        and int(score) == score
        and score >= 0
    )
    is_unknown = parsed.get("unknown", False)
    if (
        not valid_score
        or not isinstance(parsed.get("hit"), bool)
        or not isinstance(parsed.get("reason"), str)
        or not isinstance(parsed.get("tags"), list)
        or not isinstance(is_unknown, bool)
    ):
        return unknown("模型 JSON 字段格式无效")
    reason = clean_text(parsed["reason"])
    if not reason:
        return unknown("模型未提供有效评分理由")
    if parsed["hit"] != (score > 0) or (is_unknown and score != 0):
        return unknown("模型命中状态与分值不一致")
    if is_unknown:
        return unknown(reason[:240])
    tags = []
    for value in parsed["tags"]:
        if not isinstance(value, str):
            continue
        tag = clean_text(value)[:30]
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= 6:
            break
    return {
        "score": clamp_score(score, maximum),
        "state": "scored",
        "reason": reason[:240],
        "tags": tags,
    }


def prompt_rubric(key: str, maximum: int) -> str:
    """Give providers the same evidence-based scale for a single score item."""
    targets = {
        "friend_verification": "验证信息是否表达具体、合理的添加目的或交流内容",
        "friend_content": "申请内容是否提供与配置目标有关的具体信息",
        "friend_avatar": "头像可见主体与配置目标的匹配程度",
        "friend_nickname": "昵称文字与配置目标的明确关联",
        "friend_signature": "签名文字与配置目标的明确关联",
        "group_profile": "群名、群备注、群简介等文字资料与配置目标的明确关联",
    }
    target = targets.get(key, "已提供资料与本项配置目标的匹配程度")
    scale = (
        "本项只有 0 或 1 分：明确符合目标计 1 分，其余计 0 分，不允许部分分。"
        if maximum == 1
        else (
            f"本项使用整数 0～{maximum} 分：0 分表示没有目标匹配证据或明确不符；"
            f"1～{max(1, maximum - 1)} 分表示只有部分或间接匹配证据；"
            f"{maximum} 分要求有清晰、具体且与目标直接相关的证据。"
        )
    )
    return (
        f"统一评分标准：评估{target}。{scale}"
        "理由必须简短引用输入中的文字或可见图像特征，不根据昵称、头像等推断真实身份、"
        "真实性或其它未提供信息。缺失字段不作为匹配证据，也不要重复评价其它评分项。"
        "无法识别或证据不足以进行本项判断时返回 unknown:true、score:0、hit:false，"
        "并说明无法判断的原因；能够判断但不匹配时返回 unknown:false、score:0、hit:false。"
        "得分大于0时 hit 必须为true，0分时 hit 必须为false。"
    )


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
    5 by default), so cards and reports must not
    print a denominator nobody can reach. Disabled items are not achievable and
    are excluded; the documented 10 point cap still applies.
    """
    if not record.get("items"):
        return 10
    return min(10, scoring_evidence(record)["enabled_max"])


def scoring_evidence(record: dict[str, Any]) -> dict[str, int | float]:
    """Describe evidence coverage by configured weight, without score capping."""
    enabled = evaluated = unknown = 0
    for entry in record.get("items") or []:
        if not isinstance(entry, dict) or entry.get("state") == "disabled":
            continue
        try:
            maximum = max(0, int(entry.get("max", 0)))
        except (TypeError, ValueError, OverflowError):
            continue
        enabled += maximum
        if entry.get("state") == "scored":
            evaluated += maximum
        elif entry.get("state") == "unknown":
            unknown += maximum
    return {
        "enabled_max": enabled,
        "evaluated_max": evaluated,
        "unknown_max": unknown,
        "coverage": evaluated / enabled if enabled else 0.0,
    }


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
