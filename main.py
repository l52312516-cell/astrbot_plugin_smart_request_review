from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from typing import Any, AsyncGenerator

try:
    import aiohttp
except ImportError:  # pragma: no cover - AstrBot installs requirements in production
    aiohttp = None  # type: ignore[assignment]

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .core.logic import (
    as_int,
    as_text,
    clamp_score,
    contains_any,
    first_value,
    format_value,
    normalize_ids,
    parse_json_object,
    score_level,
    score_range,
    unwrap_data,
)
from .core.storage import JsonStore


PLUGIN_NAME = "astrbot_plugin_smart_request_review"
DEFAULT_TIMEOUT = 45
DEFAULT_TTL_HOURS = 72
DEFAULT_REJECTION_LIMIT = 3


def _raw_event(event: AstrMessageEvent) -> dict[str, Any]:
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _message_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("message_id") or value.get("id") or "")
    return str(getattr(value, "message_id", "") or getattr(value, "id", "") or "")


def _config_value(config: AstrBotConfig, key: str, default: Any) -> Any:
    try:
        value = config.get(key, default)
    except Exception:
        value = default
    return default if value is None else value


class SmartRequestReview(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.pending_store = JsonStore(data_dir / "pending.json", {})
        self.history_store = JsonStore(data_dir / "history.json", [])
        self.blacklist_store = JsonStore(data_dir / "blacklist.json", {"users": [], "groups": []})
        self.rejection_store = JsonStore(data_dir / "rejections.json", {})
        self.pending: dict[str, dict[str, Any]] = self.pending_store.load()
        self.history: list[dict[str, Any]] = self.history_store.load()
        self.blacklist: dict[str, list[str]] = self.blacklist_store.load()
        self.rejections: dict[str, int] = self.rejection_store.load()
        if not isinstance(self.pending, dict):
            self.pending = {}
        if not isinstance(self.history, list):
            self.history = []
        if not isinstance(self.blacklist, dict):
            self.blacklist = {"users": [], "groups": []}
        self.blacklist.setdefault("users", [])
        self.blacklist.setdefault("groups", [])
        if not isinstance(self.rejections, dict):
            self.rejections = {}
        self.seen_flags: dict[str, float] = {}
        self._prune_state()

    # ----------------------------- configuration -----------------------------
    def cfg(self, key: str, default: Any = None) -> Any:
        return _config_value(self.config, key, default)

    def ids(self, key: str) -> list[str]:
        return normalize_ids(self.cfg(key, []))

    def mode(self) -> str:
        return str(self.cfg("mode", "semi") or "semi").lower()

    def admin_users(self) -> set[str]:
        return set(self.ids("admin_users"))

    def _level_cfg(self, prefix: str, default_threshold: int = 15) -> tuple[int, int, int, int]:
        threshold = as_int(self.cfg(f"{prefix}_level_threshold", default_threshold), default_threshold)
        high_threshold = as_int(self.cfg(f"{prefix}_level_high_threshold", threshold + 15), threshold + 15)
        one = as_int(self.cfg(f"{prefix}_level_one_point", 1), 1)
        two = as_int(self.cfg(f"{prefix}_level_two_points", 2), 2)
        return threshold, high_threshold, one, two

    # ----------------------------- persistence -----------------------------
    def _save_state(self) -> None:
        self.pending_store.save(self.pending)
        self.history_store.save(self.history[-500:])
        self.blacklist_store.save(self.blacklist)
        self.rejection_store.save(self.rejections)

    def _prune_state(self) -> None:
        now = time.time()
        ttl = max(1, as_int(self.cfg("pending_expire_hours", DEFAULT_TTL_HOURS), DEFAULT_TTL_HOURS)) * 3600
        expired = [key for key, value in self.pending.items() if now - float(value.get("created_at", 0)) > ttl]
        for key in expired:
            self.pending.pop(key, None)
        self.seen_flags = {
            key: stamp for key, stamp in self.seen_flags.items() if now - stamp < 600
        }
        self._save_state()

    def _blacklisted(self, kind: str, subject_id: str) -> bool:
        if kind == "friend":
            return subject_id in set(self.blacklist.get("users", []))
        return subject_id in set(self.blacklist.get("users", []))

    def _add_blacklist(self, kind: str, subject_id: str) -> None:
        bucket = "users"
        values = self.blacklist.setdefault(bucket, [])
        if subject_id and subject_id not in values:
            values.append(subject_id)
        self._save_state()

    def _rejection_key(self, kind: str, subject_id: str) -> str:
        return f"{kind}:{subject_id}"

    def _record_rejection(self, kind: str, subject_id: str) -> tuple[int, bool]:
        key = self._rejection_key(kind, subject_id)
        count = as_int(self.rejections.get(key, 0), 0) + 1
        self.rejections[key] = count
        limit = max(1, as_int(self.cfg("rejection_limit", DEFAULT_REJECTION_LIMIT), DEFAULT_REJECTION_LIMIT))
        blocked = count >= limit
        if blocked:
            self._add_blacklist(kind, subject_id)
        self._save_state()
        return count, blocked

    def _clear_rejections(self, kind: str, subject_id: str) -> None:
        self.rejections.pop(self._rejection_key(kind, subject_id), None)
        self._save_state()

    # ----------------------------- OneBot helpers -----------------------------
    async def _call(self, bot: Any, action: str, **params: Any) -> tuple[bool, Any, str]:
        caller = getattr(bot, "call_action", None)
        if not callable(caller):
            api = getattr(bot, "api", None)
            caller = getattr(api, "call_action", None)
        if not callable(caller):
            return False, None, "bot 不支持 call_action"
        try:
            result = await caller(action, **params)
        except Exception as exc:
            return False, None, f"{type(exc).__name__}: {exc}"
        if isinstance(result, dict) and result.get("status") == "failed":
            return False, result.get("data"), str(result.get("wording") or result.get("message") or "接口返回失败")
        if isinstance(result, dict) and "data" in result and ("status" in result or "retcode" in result):
            return True, result.get("data"), ""
        return True, result, ""

    async def _data(self, bot: Any, action: str, **params: Any) -> Any:
        ok, value, _ = await self._call(bot, action, **params)
        return unwrap_data(value) if ok else None

    async def _approve(self, bot: Any, record: dict[str, Any], approve: bool, reason: str = "") -> tuple[bool, str]:
        flag = str(record.get("flag") or "")
        if not flag:
            return False, "申请缺少 flag，无法审批"
        if record.get("kind") == "friend":
            ok, _, error = await self._call(
                bot,
                "set_friend_add_request",
                flag=flag,
                approve=bool(approve),
            )
        else:
            ok, _, error = await self._call(
                bot,
                "set_group_add_request",
                flag=flag,
                sub_type="invite",
                approve=bool(approve),
                reason=reason if not approve else "",
            )
        return ok, error

    async def _send_action(self, bot: Any, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        ok, value, error = await self._call(bot, action, **params)
        if not ok:
            logger.warning(f"[{PLUGIN_NAME}] 发送消息失败: {error}")
            return None
        return value if isinstance(value, dict) else {}

    async def _send_segments(self, bot: Any, target: str, text: str, image: bytes | None = None) -> str:
        segments: list[dict[str, Any]] = [{"type": "text", "data": {"text": text}}]
        if image:
            encoded = base64.b64encode(image).decode("ascii")
            segments.append({"type": "image", "data": {"file": f"base64://{encoded}"}})
        target = str(target or "")
        if target.startswith("aiocqhttp:"):
            parts = target.split(":")
            if len(parts) >= 3 and "group" in parts[1].lower():
                target = f"group:{parts[-1]}"
            elif len(parts) >= 3:
                target = f"private:{parts[-1]}"
        if target.startswith("group:"):
            result = await self._send_action(bot, "send_group_msg", {"group_id": int(target.split(":", 1)[1]), "message": segments})
        elif target.startswith("private:") or target.startswith("friend:"):
            result = await self._send_action(bot, "send_private_msg", {"user_id": int(target.split(":", 1)[1]), "message": segments})
        elif target.isdigit():
            result = await self._send_action(bot, "send_group_msg", {"group_id": int(target), "message": segments})
        else:
            result = None
        return _message_id(result)

    async def _send_private(self, bot: Any, user_id: str, text: str) -> None:
        if user_id.isdigit():
            await self._send_segments(bot, f"private:{user_id}", text)

    # ----------------------------- profile collection -----------------------------
    async def _avatar(self, user_id: str) -> bytes | None:
        if not user_id.isdigit() or aiohttp is None:
            return None
        url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        return await self._download_image(url)

    async def _group_avatar(self, group_id: str) -> bytes | None:
        if not group_id.isdigit() or aiohttp is None:
            return None
        return await self._download_image(f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640/")

    async def _download_image(self, url: str) -> bytes | None:
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.read()
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 下载图片失败: {exc}")
            return None

    async def _friend_profile(self, bot: Any, user_id: str, comment: str) -> dict[str, Any]:
        raw = await self._data(bot, "get_stranger_info", user_id=int(user_id), no_cache=True) or {}
        if not raw:
            raw = await self._data(bot, "get_stranger_info", user_id=int(user_id)) or {}
        if not isinstance(raw, dict):
            raw = {}
        profile = {
            "user_id": user_id,
            "nickname": as_text(first_value(raw, "nickname", "nick", "name"), user_id),
            "level": as_int(first_value(raw, "qqLevel", "level", "qlevel")),
            "signature": as_text(first_value(raw, "long_nick", "longNick", "long_nickname", "signature")),
            "sex": as_text(raw.get("sex")),
            "age": as_text(raw.get("age")),
            "area": as_text(first_value(raw, "city", "area", "province")),
            "comment": comment or "无",
            "raw": raw,
        }
        profile["avatar"] = await self._avatar(user_id)
        return profile

    async def _group_info(self, bot: Any, group_id: str, inviter_id: str, flag: str, comment: str) -> dict[str, Any]:
        info: dict[str, Any] = {
            "group_id": group_id,
            "inviter_id": inviter_id,
            "comment": comment or "无",
            "errors": [],
            "notices": [],
            "essence": [],
            "members": [],
        }
        info["avatar"] = await self._group_avatar(group_id)
        for action in ("get_group_info", "get_group_info_ex"):
            value = await self._data(bot, action, group_id=int(group_id), no_cache=True)
            if not value:
                value = await self._data(bot, action, group_id=int(group_id))
            if isinstance(value, dict):
                info.update({
                    "name": as_text(first_value(value, "group_name", "name")),
                    "remark": as_text(value.get("group_remark")),
                    "memo": as_text(first_value(value, "group_description", "group_memo", "memo")),
                    "member_count": as_int(value.get("member_count")),
                    "max_member_count": as_int(value.get("max_member_count")),
                    "level": as_int(first_value(value, "group_level", "level")),
                    "create_time": as_int(value.get("group_create_time")),
                    "all_shut": bool(value.get("group_all_shut")),
                })
                if info.get("name") or info.get("member_count"):
                    break
        if not info.get("name"):
            info["errors"].append("群名未获取到：机器人可能尚未入群，协议端返回空壳资料")

        for action in ("get_group_system_msg", "get_group_ignored_notifies"):
            value = await self._data(bot, action)
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    if flag and str(item.get("flag") or "") != flag:
                        continue
                    if str(item.get("group_id") or group_id) != group_id:
                        continue
                    info["name"] = info.get("name") or as_text(item.get("group_name"))
                    info["inviter_nickname"] = as_text(first_value(item, "invitor_nick", "requester_nick"))
                    break
                if info.get("name") or info.get("inviter_nickname"):
                    break
        if not info.get("inviter_nickname") and inviter_id.isdigit():
            inviter = await self._data(bot, "get_stranger_info", user_id=int(inviter_id)) or {}
            if isinstance(inviter, dict):
                info["inviter_nickname"] = as_text(first_value(inviter, "nickname", "nick"), inviter_id)

        for action in ("_get_group_notice", "get_group_notice"):
            value = await self._data(bot, action, group_id=int(group_id))
            if isinstance(value, list):
                info["notices"] = [as_text(item.get("text") if isinstance(item, dict) else item) for item in value]
                break
        if not info["notices"]:
            info["errors"].append("群公告未获取到")

        for action in ("get_essence_msg_list", "get_group_honor_info"):
            value = await self._data(bot, action, group_id=int(group_id))
            if isinstance(value, list):
                info["essence"] = value
                break
        if not info["essence"]:
            info["errors"].append("群精华/荣誉未获取到")

        members = await self._data(bot, "get_group_member_list", group_id=int(group_id))
        if isinstance(members, list):
            info["members"] = members
            roles = [item for item in members if isinstance(item, dict) and item.get("role") in {"owner", "admin"}]
            info["admins"] = roles
        else:
            info["errors"].append("群成员列表未获取到：机器人可能尚未入群")
        return info

    # ----------------------------- model and scoring -----------------------------
    async def _provider_id(self, event: AstrMessageEvent, vision: bool = False) -> str:
        configured = as_text(self.cfg("vision_provider_id" if vision else "provider_id", ""))
        if configured:
            return configured
        try:
            current = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            if current:
                return str(current)
        except Exception:
            pass
        try:
            providers = self.context.get_all_providers()
            for provider in providers or []:
                meta = provider.meta()
                value = getattr(meta, "id", None) or getattr(provider, "id", None)
                if value:
                    return str(value)
        except Exception:
            pass
        return ""

    async def _model_score(
        self,
        event: AstrMessageEvent,
        prompt: str,
        maximum: int,
        image: bytes | None = None,
        vision: bool = False,
    ) -> dict[str, Any]:
        if not self.cfg("enable_llm", True):
            return {"score": 0, "available": False, "reason": "模型判断已关闭", "tags": []}
        provider_id = await self._provider_id(event, vision=vision)
        if not provider_id:
            return {"score": 0, "available": False, "reason": "未找到可用模型", "tags": []}
        images = None
        if image:
            images = [f"base64://{base64.b64encode(image).decode('ascii')}" ]
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt, image_urls=images),
                timeout=max(5, as_int(self.cfg("llm_timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT)),
            )
            raw = as_text(getattr(response, "completion_text", ""))
            parsed = parse_json_object(raw)
            if not parsed:
                return {"score": 0, "available": False, "reason": "模型返回不是有效 JSON", "tags": []}
            return {
                "score": clamp_score(parsed.get("score", 0), maximum),
                "available": True,
                "reason": as_text(parsed.get("reason"), "无理由"),
                "tags": [as_text(item) for item in parsed.get("tags", [])] if isinstance(parsed.get("tags", []), list) else [],
                "hit": bool(parsed.get("hit", False)),
            }
        except Exception as exc:
            return {"score": 0, "available": False, "reason": f"模型调用失败：{type(exc).__name__}", "tags": []}

    def _item(self, name: str, score: int, maximum: int, reason: str, available: bool = True, tags: list[str] | None = None) -> dict[str, Any]:
        return {"name": name, "score": max(0, min(maximum, score)), "max": maximum, "reason": reason, "available": available, "tags": tags or []}

    async def _friend_score(self, event: AstrMessageEvent, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        comment = profile["comment"]
        items: list[dict[str, Any]] = []
        items.append(await self._model_score(event, self._prompt("friend_verification_prompt", f"请评估这条好友申请验证信息的真实性和具体程度：{comment}"), 3))
        verification = items.pop()
        scored = [self._item("验证信息质量", verification["score"], 3, verification["reason"], verification["available"], verification.get("tags"))]
        positive = contains_any(comment, self.ids("approve_keywords"))
        scored.append(self._item("申请内容规则", 1 if comment and not contains_any(comment, self.ids("reject_keywords")) else 0, 1, f"命中：{', '.join(positive)}" if positive else "未命中正向规则", True, positive))
        threshold, high, one, two = self._level_cfg("friend")
        scored.append(self._item("QQ等级", score_level(profile.get("level"), threshold, high, one, two), 2, f"QQ等级：{format_value(profile.get('level'))}"))
        avatar_result = await self._model_score(event, self._prompt("friend_avatar_prompt", "请识别头像是否符合配置中的二次元/动漫角色等目标，只输出 JSON。"), 2, profile.get("avatar"), vision=True)
        scored.append(self._item("头像识别", avatar_result["score"], 2, avatar_result["reason"], avatar_result["available"], avatar_result.get("tags")))
        for label, key, config_key in (("昵称识别", "nickname", "friend_nickname_prompt"), ("签名识别", "signature", "friend_signature_prompt")):
            result = await self._model_score(event, self._prompt(config_key, f"请判断以下内容是否符合配置目标：{profile.get(key) or '未知'}"), 1)
            scored.append(self._item(label, result["score"], 1, result["reason"], result["available"], result.get("tags")))
        return scored, sum(item["score"] for item in scored)

    async def _group_score(self, event: AstrMessageEvent, info: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        corpus = "\n".join([as_text(info.get("name")), as_text(info.get("remark")), as_text(info.get("memo"))])
        profile_result = await self._model_score(event, self._prompt("group_profile_prompt", f"请判断以下群资料是否符合审核目标：\n{corpus}"), 3)
        comment_result = await self._model_score(event, self._prompt("group_comment_prompt", f"请评估群邀请验证信息：{info.get('comment', '无')}"), 2)
        min_count = as_int(self.cfg("group_member_min", 1), 1)
        max_count = as_int(self.cfg("group_member_max", 999999), 999999)
        count_points = score_range(info.get("member_count"), min_count, max_count, 2)
        level_threshold, high, one, two = self._level_cfg("group", 1)
        level_points = score_level(info.get("level"), level_threshold, high, one, two)
        text_result = await self._model_score(event, self._prompt("group_text_prompt", f"请判断群简介、公告和精华是否符合审核目标：\n{corpus}\n{info.get('notices', [])}\n{len(info.get('essence', []))}条精华"), 2)
        items = [
            self._item("群资料特征", profile_result["score"], 3, profile_result["reason"], profile_result["available"], profile_result.get("tags")),
            self._item("邀请验证信息", comment_result["score"], 2, comment_result["reason"], comment_result["available"], comment_result.get("tags")),
            self._item("群人数", count_points, 2, f"群人数：{format_value(info.get('member_count'))}"),
            self._item("群等级", level_points, 1, f"群等级：{format_value(info.get('level'))}"),
            self._item("群简介/公告/精华", text_result["score"], 2, text_result["reason"], text_result["available"], text_result.get("tags")),
        ]
        return items, sum(item["score"] for item in items)

    def _prompt(self, key: str, fallback: str) -> str:
        base = as_text(self.cfg("decision_prompt", ""))
        custom = as_text(self.cfg(key, ""))
        prefix = custom or base
        return f"{prefix}\n只输出 JSON，score 必须是 0 到指定最高分的整数。\n{fallback}"

    # ----------------------------- rules and reports -----------------------------
    def _hard_rule(self, kind: str, subject: str, text: str, secondary: str = "") -> dict[str, Any]:
        corpus = "\n".join([subject, text, secondary])
        if self._blacklisted(kind, subject):
            return {"action": "reject", "reason": "命中插件本地黑名单"}
        if kind == "friend":
            deny = self.ids("friend_blacklist") + self.ids("reject_keywords")
            allow = self.ids("friend_allowlist") + self.ids("approve_keywords")
        else:
            deny = self.ids("group_blacklist") + self.ids("reject_keywords")
            allow = self.ids("group_allowlist") + self.ids("approve_keywords")
        denied = contains_any(corpus, deny)
        if denied:
            return {"action": "reject", "reason": f"命中拒绝规则：{', '.join(denied)}", "hits": denied}
        allowed = contains_any(corpus, allow)
        if allowed:
            return {"action": "approve", "reason": f"命中允许规则：{', '.join(allowed)}", "hits": allowed}
        if self.cfg("require_allowlist", False):
            return {"action": "reject", "reason": "未命中白名单"}
        return {"action": "score", "reason": "未命中硬规则"}

    def _report(self, record: dict[str, Any], outcome: str = "待审批", error: str = "") -> str:
        profile = record.get("profile", {})
        info = record.get("group", {})
        lines = [
            "【智能好友/群邀请审核】",
            f"类型：{'好友申请' if record.get('kind') == 'friend' else '群邀请'}",
            f"状态：{outcome}",
            f"申请消息：{record.get('request_id', '未知')}",
        ]
        if record.get("kind") == "friend":
            lines.extend([
                f"QQ号：{self._display_id(profile.get('user_id'))}",
                f"昵称：{format_value(profile.get('nickname'))}",
                f"QQ等级：{format_value(profile.get('level'))}",
                f"签名：{format_value(profile.get('signature'))}",
                f"验证信息：{format_value(profile.get('comment'))}",
            ])
        else:
            lines.extend([
                f"群号：{format_value(info.get('group_id'))}",
                f"群名称：{format_value(info.get('name'))}",
                f"群备注：{format_value(info.get('remark'))}",
                f"群人数：{format_value(info.get('member_count'))}",
                f"群等级：{format_value(info.get('level'))}",
                f"邀请人：{format_value(info.get('inviter_nickname'))} ({self._display_id(info.get('inviter_id'))})",
                f"验证信息：{format_value(info.get('comment'))}",
                f"公告数量：{len(info.get('notices', []))}，精华/荣誉数量：{len(info.get('essence', []))}",
            ])
        hard = record.get("hard_rule", {})
        lines.append(f"硬规则：{hard.get('reason', '无')}")
        lines.append(f"评分：{record.get('score', 0)}/10，阈值：{record.get('threshold', 5)}")
        for item in record.get("items", []):
            tags = f" [{', '.join(item.get('tags', []))}]" if item.get("tags") else ""
            available = "" if item.get("available", True) else "（未知）"
            lines.append(f"- {item.get('name')}：{item.get('score')}/{item.get('max')} {available}{tags}：{item.get('reason')}")
        lines.append(f"累计拒绝：{record.get('rejection_count', 0)}")
        if error:
            lines.append(f"接口错误：{error}")
        if record.get("missing"):
            lines.append("缺失字段：" + "；".join(record["missing"]))
        if self.mode() == "semi" and outcome == "待审批":
            lines.append("请引用本消息回复：同意 / 拒绝 [理由] / 拉黑 [理由]")
        return "\n".join(lines)

    def _display_id(self, value: Any) -> str:
        text = format_value(value)
        if not self.cfg("mask_qq_in_notice", False) or not text.isdigit() or len(text) < 7:
            return text
        return f"{text[:3]}****{text[-3:]}"

    async def _notify_reviewers(self, bot: Any, event: AstrMessageEvent, record: dict[str, Any], text: str) -> list[str]:
        targets: list[str] = []
        session = as_text(self.cfg("review_session", ""))
        if session:
            targets.append(session)
        else:
            targets.extend(f"private:{uid}" for uid in self.ids("admin_users"))
        if not targets:
            targets.append(event.unified_msg_origin)
        message_ids: list[str] = []
        for target in targets:
            image = record.get("avatar") or record.get("group", {}).get("avatar")
            message_id = await self._send_segments(bot, target, text, image)
            if message_id:
                message_ids.append(message_id)
        return message_ids

    async def _notify_requester(self, bot: Any, record: dict[str, Any], text: str) -> None:
        if not self.cfg("requester_notice", True):
            return
        user_id = str(record.get("subject_id") or "")
        await self._send_private(bot, user_id, text)

    async def _persist_history(self, record: dict[str, Any], outcome: str, operator: str = "auto", error: str = "") -> None:
        snapshot = json.loads(json.dumps(record, ensure_ascii=False, default=str))
        snapshot.pop("avatar", None)
        if isinstance(snapshot.get("group"), dict):
            snapshot["group"].pop("avatar", None)
        snapshot["outcome"] = outcome
        snapshot["operator"] = operator
        snapshot["finished_at"] = time.time()
        if error:
            snapshot["error"] = error
        self.history.append(snapshot)
        self._save_state()

    # ----------------------------- request orchestration -----------------------------
    async def _process_request(self, event: AstrMessageEvent, raw: dict[str, Any]) -> None:
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        request_type = str(raw.get("request_type") or "")
        subtype = str(raw.get("sub_type") or "")
        if request_type == "friend":
            kind = "friend"
            subject_id = str(raw.get("user_id") or "")
            comment = as_text(raw.get("comment"), "无")
            profile = await self._friend_profile(bot, subject_id, comment)
            subject_text = f"{profile.get('nickname')}\n{comment}"
            hard = self._hard_rule(kind, subject_id, subject_text)
            record: dict[str, Any] = {
                "kind": kind, "subject_id": subject_id, "flag": str(raw.get("flag") or ""),
                "profile": {key: value for key, value in profile.items() if key != "avatar"},
                "avatar": profile.get("avatar"), "hard_rule": hard,
            }
        elif request_type == "group" and subtype == "invite":
            kind = "group"
            group_id = str(raw.get("group_id") or "")
            subject_id = str(raw.get("user_id") or "")
            info = await self._group_info(bot, group_id, subject_id, str(raw.get("flag") or ""), as_text(raw.get("comment"), "无"))
            hard = self._hard_rule(kind, group_id, f"{info.get('name')}\n{info.get('remark')}", info.get("comment", ""))
            record = {
                "kind": kind, "subject_id": subject_id, "flag": str(raw.get("flag") or ""),
                "group": info, "hard_rule": hard,
            }
        else:
            return
        if not subject_id or not record.get("flag"):
            return
        flag = str(record["flag"])
        if flag in self.seen_flags or any(item.get("flag") == flag for item in self.pending.values()):
            return
        self.seen_flags[flag] = time.time()
        record["created_at"] = time.time()
        record["request_id"] = flag
        record["threshold"] = max(0, min(10, as_int(self.cfg("score_threshold", 5), 5)))
        record["missing"] = record.get("group", {}).get("errors", []) if kind == "group" else []

        hard_action = record["hard_rule"].get("action")
        if hard_action == "reject":
            record["score"] = 0
            record["items"] = []
            await self._finish_auto(event, record, approve=False, reason=record["hard_rule"].get("reason", "硬规则拒绝"))
            return
        if hard_action == "approve" and self.mode() == "auto":
            record["score"] = 10
            record["items"] = []
            await self._finish_auto(event, record, approve=True, reason=record["hard_rule"].get("reason", "命中允许规则"))
            return
        if kind == "friend":
            profile_for_score = dict(record["profile"])
            profile_for_score["avatar"] = record.get("avatar")
            items, score = await self._friend_score(event, profile_for_score)
        else:
            items, score = await self._group_score(event, record["group"])
        record["items"] = items
        record["score"] = score
        if self.mode() == "semi":
            await self._queue_pending(event, record)
            return
        approve = hard_action == "approve" or score >= record["threshold"]
        await self._finish_auto(event, record, approve=approve, reason=record["hard_rule"].get("reason", "评分决定"))

    async def _queue_pending(self, event: AstrMessageEvent, record: dict[str, Any]) -> None:
        record["status"] = "pending"
        text = self._report(record, "待审批")
        bot = getattr(event, "bot", None)
        ids = await self._notify_reviewers(bot, event, record, text)
        if not ids:
            logger.warning(f"[{PLUGIN_NAME}] 未获取审核消息 ID，无法启用引用审批: {record.get('flag')}")
            return
        for message_id in ids:
            saved = dict(record)
            saved.pop("avatar", None)
            if isinstance(saved.get("group"), dict):
                saved["group"] = dict(saved["group"])
                saved["group"].pop("avatar", None)
            saved["request_id"] = record["flag"]
            self.pending[message_id] = saved
        self._save_state()
        await self._notify_requester(bot, record, f"已收到你的{'好友申请' if record['kind'] == 'friend' else '群邀请'}，等待管理员审核。")

    async def _finish_auto(self, event: AstrMessageEvent, record: dict[str, Any], approve: bool, reason: str) -> None:
        bot = getattr(event, "bot", None)
        ok, error = await self._approve(bot, record, approve, reason)
        if not ok:
            await self._notify_reviewers(bot, event, record, self._report(record, "审批接口失败", error))
            await self._persist_history(record, "error", error=error)
            return
        outcome = "已同意" if approve else "已拒绝"
        count = 0
        blocked = False
        if approve:
            self._clear_rejections(record["kind"], record["subject_id"])
        else:
            count, blocked = self._record_rejection(record["kind"], record["subject_id"])
        record["rejection_count"] = count
        record["blacklisted"] = blocked
        report = self._report(record, outcome)
        await self._notify_reviewers(bot, event, record, report)
        await self._notify_requester(bot, record, f"你的{'好友申请' if record['kind'] == 'friend' else '群邀请'}{outcome}。")
        await self._persist_history(record, outcome)

    # ----------------------------- quoted approval commands -----------------------------
    def _reply_id(self, event: AstrMessageEvent) -> str:
        for component in event.get_messages():
            if isinstance(component, Reply):
                return str(component.id)
        return ""

    async def _is_reviewer(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        if sender in self.admin_users():
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        group_id = str(event.get_group_id() or "")
        review_session = as_text(self.cfg("review_session", ""))
        configured_group = ""
        if review_session.startswith("group:"):
            configured_group = review_session.split(":", 1)[1]
        elif review_session.startswith("aiocqhttp:") and "GroupMessage:" in review_session:
            configured_group = review_session.rsplit(":", 1)[-1]
        elif review_session.isdigit():
            configured_group = review_session
        if not group_id or (configured_group and group_id != configured_group):
            return False
        info = await self._data(getattr(event, "bot", None), "get_group_member_info", group_id=int(group_id), user_id=int(sender))
        return isinstance(info, dict) and str(info.get("role") or "").lower() in {"owner", "admin"}

    async def _handle_command(self, event: AstrMessageEvent, action: str) -> AsyncGenerator[Any, None]:
        try:
            event.stop_event()
        except Exception:
            pass
        if not await self._is_reviewer(event):
            yield event.plain_result("你没有审批权限。")
            return
        reply_id = self._reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用机器人发出的申请审核消息后，再使用同意、拒绝或拉黑。")
            return
        record = self.pending.get(reply_id)
        if not record:
            yield event.plain_result("引用的消息不是有效的待审批申请，或申请已过期/处理。")
            return
        bot = getattr(event, "bot", None)
        reason = str(getattr(event, "message_str", "") or "").strip()
        reason = reason[len(action):].strip() if reason.startswith(action) else ""
        approve = action == "同意"
        if action == "拉黑":
            approve = False
        ok, error = await self._approve(bot, record, approve, reason)
        if not ok:
            yield event.plain_result(f"审批失败：{error}")
            return
        target_flag = str(record.get("flag") or "")
        for key, item in list(self.pending.items()):
            if str(item.get("flag") or "") == target_flag:
                self.pending.pop(key, None)
        outcome = "已同意" if approve else ("已拉黑并拒绝" if action == "拉黑" else "已拒绝")
        if approve:
            self._clear_rejections(record["kind"], record["subject_id"])
        else:
            count, blocked = self._record_rejection(record["kind"], record["subject_id"])
            record["rejection_count"] = count
            record["blacklisted"] = blocked
            if action == "拉黑":
                self._add_blacklist(record["kind"], record["subject_id"])
        await self._persist_history(record, outcome, operator=str(event.get_sender_id()))
        await self._notify_requester(bot, record, f"你的{'好友申请' if record['kind'] == 'friend' else '群邀请'}{outcome}。")
        self._save_state()
        yield event.plain_result(f"{outcome}。")

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def on_request(self, event: AstrMessageEvent):
        raw = _raw_event(event)
        if raw.get("post_type") != "request":
            return
        try:
            await self._process_request(event, raw)
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 处理申请失败: {exc}")

    @filter.command("同意")
    async def approve_command(self, event: AstrMessageEvent):
        async for result in self._handle_command(event, "同意"):
            yield result

    @filter.command("拒绝")
    async def reject_command(self, event: AstrMessageEvent):
        async for result in self._handle_command(event, "拒绝"):
            yield result

    @filter.command("拉黑")
    async def blacklist_command(self, event: AstrMessageEvent):
        async for result in self._handle_command(event, "拉黑"):
            yield result
