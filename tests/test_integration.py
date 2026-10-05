"""Protocol-level regression tests. No live QQ messages or model requests are made."""

from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.util
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def stub_astrbot():
    names = [
        "astrbot",
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.message_components",
        "astrbot.api.star",
        "astrbot.core",
        "astrbot.core.star",
        "astrbot.core.star.filter",
        "astrbot.core.star.filter.platform_adapter_type",
    ]
    for name in names:
        sys.modules.setdefault(name, types.ModuleType(name))

    class Star:
        def __init__(self, context):
            self.context = context

    class Config(dict):
        schema = None

        def save_config(self):
            pass

    class Reply:
        def __init__(self, id):
            self.id = id

    class At:
        def __init__(self, qq):
            self.qq = qq

    class Filter:
        def __getattr__(self, name):
            return lambda *a, **kw: lambda fn: fn

    api = sys.modules["astrbot.api"]
    api.AstrBotConfig = Config
    api.logger = logging.getLogger("review-test")
    event = sys.modules["astrbot.api.event"]
    event.AstrMessageEvent = object
    event.filter = Filter()
    components = sys.modules["astrbot.api.message_components"]
    components.Reply, components.At = Reply, At
    star = sys.modules["astrbot.api.star"]
    star.Star, star.Context = Star, object
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path("."))
    sys.modules[
        "astrbot.core.star.filter.platform_adapter_type"
    ].PlatformAdapterType = types.SimpleNamespace(AIOCQHTTP="aiocqhttp")
    return Config, Reply, At


Config, Reply, At = stub_astrbot()
package = types.ModuleType("review_under_test")
package.__path__ = [str(ROOT)]
sys.modules["review_under_test"] = package
plugin = importlib.import_module("review_under_test.main")
from review_under_test.core.providers import ProviderSelector
from review_under_test.core.scoring import model_result, summary
from review_under_test.core.renderer import ReviewCardRenderer, lines_for


class FakeBot:
    def __init__(self):
        self.calls = []
        self.mid = 100
        self.fail_approval = False
        self.fail_image = False
        self.fail_send = False
        self.role = "member"
        self.friends = [
            {"user_id": 10000 + i, "nickname": f"好友{i}"} for i in range(1, 24)
        ]
        self.groups = [
            {"group_id": 20000 + i, "group_name": f"群{i}"} for i in range(1, 24)
        ]

    async def call_action(self, action, **params):
        self.calls.append((action, copy.deepcopy(params)))
        await asyncio.sleep(0)
        if action.startswith("set_") and action.endswith("_request"):
            if self.fail_approval:
                return {"status": "ok", "retcode": 100, "data": None}
            return {"status": "ok", "retcode": 0, "data": {}}
        if action.startswith("send_"):
            if self.fail_send or (
                self.fail_image and any(s["type"] == "image" for s in params["message"])
            ):
                return {"status": "failed", "retcode": 100, "message": "cannot send"}
            self.mid += 1
            return {"status": "ok", "data": {"message_id": self.mid}, "retcode": 0}
        if action == "get_stranger_info":
            return {
                "nickname": "二次元爱好者",
                "qqLevel": 20,
                "long_nick": "动漫交流",
                "sex": "unknown",
            }
        if action in {"get_group_info", "get_group_info_ex"}:
            return {
                "group_name": "动漫交流群",
                "group_memo": "欢迎讨论",
                "member_count": 88,
                "max_member_count": 200,
                "group_level": 3,
            }
        if action == "get_group_member_info":
            return {"role": self.role}
        if action == "get_friend_list":
            return self.friends
        if action == "get_group_list":
            return self.groups
        if action == "get_group_honor_info":
            return {"talkative_list": [{"nickname": "活跃用户"}]}
        if action == "get_group_member_list":
            return [{"role": "owner", "nickname": "群主", "user_id": 66666}]
        if action in {"get_essence_msg_list", "_get_group_notice", "get_group_notice"}:
            return [{"text": "动漫讨论"}]
        return {}


class Event:
    def __init__(
        self,
        bot,
        sender="99999",
        group="88888",
        bot_id="12345",
        platform="aiocqhttp",
        text="",
        reply=None,
        admin=False,
        ats=(),
    ):
        self.bot, self.sender, self.group, self.bot_id = bot, sender, group, bot_id
        self.unified_msg_origin = f"{platform}:{'GroupMessage' if group else 'FriendMessage'}:{group or sender}"
        self.message_str = text
        self.components = ([Reply(reply)] if reply else []) + [At(x) for x in ats]
        self.admin = admin
        self.stopped = False

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return self.bot_id

    def get_group_id(self):
        return self.group

    def get_messages(self):
        return self.components

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class Context:
    def __init__(self):
        self.providers = [
            types.SimpleNamespace(
                meta=lambda: types.SimpleNamespace(
                    id="text", name="Text", provider_type="chat_completion"
                )
            ),
            types.SimpleNamespace(
                meta=lambda: types.SimpleNamespace(
                    id="vision", name="Vision", provider_type="chat_completion"
                )
            ),
        ]
        self.llm_generate = AsyncMock(
            return_value=types.SimpleNamespace(
                completion_text='{"score":2,"hit":true,"tags":["动漫"],"reason":"符合目标"}'
            )
        )

    def get_all_providers(self):
        return self.providers

    async def get_current_chat_provider_id(self, umo):
        return "text"

    def get_config(self):
        return {"admins_id": ["99999"]}


async def collect(gen):
    return [x async for x in gen]


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.path_patch = patch.object(
            plugin.StarTools, "get_data_dir", return_value=self.data
        )
        self.path_patch.start()
        self.context, self.bot = Context(), FakeBot()
        self.config = Config(
            {
                "mode": "semi",
                "review_session": "group:88888",
                "admin_users": ["99999"],
                "requester_notice": False,
                "enable_llm": True,
            }
        )
        self.p = plugin.SmartRequestReview(self.context, self.config)
        self.p.providers.schema_path = self.data / "schema.json"
        self.p.providers.schema_path.write_text(
            '{"provider_id":{},"vision_provider_id":{}}', encoding="utf-8"
        )
        self.p._download_image = AsyncMock(return_value=None)
        self.p.renderer = types.SimpleNamespace(
            render=lambda *a: b"image", render_list=lambda *a: b"image"
        )
        self.event = Event(self.bot)

    async def asyncTearDown(self):
        await self.p.terminate()
        self.path_patch.stop()
        self.temp.cleanup()

    async def request(
        self, kind="friend", flag="F1", uid="55555", gid="22222", comment="希望一起交流"
    ):
        raw = {
            "post_type": "request",
            "request_type": kind,
            "sub_type": "invite",
            "user_id": uid,
            "group_id": gid,
            "flag": flag,
            "comment": comment,
        }
        await self.p._process_request(self.event, raw)
        return raw

    def approvals(self):
        return [
            (a, p)
            for a, p in self.bot.calls
            if a in {"set_friend_add_request", "set_group_add_request"}
        ]

    def pending(self):
        return next(iter(self.p.pending.values()))

    def reply_event(self, action="同意", **kw):
        return Event(self.bot, text=action, reply=self.pending()["message_id"], **kw)

    def kick(self, **kw):
        return {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "kick_me",
            "self_id": "12345",
            "user_id": "12345",
            "group_id": "22222",
            "operator_id": "55555",
            "time": 123456,
            **kw,
        }

    async def test_friend_and_group_semi_cards(self):
        for kind in ("friend", "group"):
            await self.request(kind, flag=kind)
        self.assertEqual(len(self.p.pending), 2)
        self.assertEqual(self.approvals(), [])
        for record in self.p.pending.values():
            self.assertIn(
                record["recommendation"]["action"], {"approve", "reject", "block"}
            )
            self.assertEqual(record["review_session"], "group:88888")
        messages = [p["message"] for a, p in self.bot.calls if a == "send_group_msg"]
        self.assertTrue(
            all({x["type"] for x in m} == {"text", "image"} for m in messages)
        )

    async def test_ignore_add(self):
        await self.p._process_request(
            self.event, {"request_type": "group", "sub_type": "add"}
        )
        self.assertFalse(self.bot.calls)

    async def test_local_blacklist_rejects_both_modes_both_types(self):
        self.p._add_blacklist("users", "55555", "test")
        for mode in ("semi", "auto"):
            self.config["mode"] = mode
            for kind in ("friend", "group"):
                await self.request(kind, flag=f"{mode}-{kind}")
        self.assertEqual(len(self.approvals()), 4)
        self.assertTrue(all(not p["approve"] for a, p in self.approvals()))
        self.assertEqual(self.p.pending, {})
        self.context.llm_generate.assert_not_awaited()

    async def test_group_blacklist_different_inviter(self):
        self.p._add_blacklist("groups", "22222", "test")
        await self.request("group", uid="77777")
        self.assertFalse(self.approvals()[0][1]["approve"])
        self.assertEqual(self.approvals()[0][1]["sub_type"], "invite")

    async def test_rules_exact_ids_and_deny_priority(self):
        self.config.update(friend_allowlist=["动漫"], friend_blacklist=["555", "广告"])
        await self.request(uid="55555", comment="动漫广告")
        self.assertEqual(self.pending()["hard_rule"]["action"], "reject")
        self.assertTrue(self.pending()["items"])
        self.assertEqual(self.approvals(), [])
        self.p.pending.clear()
        await self.request(flag="F2", comment="你好")
        self.assertEqual(self.pending()["hard_rule"]["action"], "score")

    async def test_auto_whitelist_skips_score_not_ten(self):
        self.config.update(mode="auto", friend_allowlist=["55555"])
        await self.request()
        self.assertTrue(self.approvals()[0][1]["approve"])
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual(self.p.history[-1]["score"], 0)
        self.assertTrue(
            all(x["state"] == "skipped" for x in self.p.history[-1]["items"])
        )

    async def test_threshold_inheritance_and_independent(self):
        self.config.update(
            score_threshold=8, friend_score_threshold=-1, group_score_threshold=2
        )
        self.assertEqual(self.p._threshold("friend"), 8)
        self.assertEqual(self.p._threshold("group"), 2)
        self.config.update(friend_score_threshold=0)
        self.assertEqual(self.p._threshold("friend"), 0)

    async def test_missing_fields_never_model_scored(self):
        p = {
            "kind": "friend",
            "subject_id": "55555",
            "profile": {},
            "hard_rule": {"action": "score"},
            "threshold": 5,
        }
        await self.p._score(self.event, p)
        self.assertEqual(p["score"], 0)
        self.assertTrue(all(x["state"] == "unknown" for x in p["items"]))
        self.context.llm_generate.assert_not_awaited()

    async def test_disabled_and_custom_max_cap(self):
        self.config.update(
            friend_avatar_enabled=False,
            friend_verification_max_score=9,
            friend_nickname_max_score=9,
        )
        self.context.llm_generate.return_value.completion_text = (
            '{"score":99,"hit":true,"tags":[],"reason":"匹配"}'
        )
        await self.request()
        r = self.pending()
        self.assertEqual(r["score"], 10)
        self.assertGreater(r["score_raw"], 10)
        self.assertEqual(
            next(x for x in r["items"] if x["key"] == "friend_avatar")["state"],
            "disabled",
        )

    async def test_json_failure_and_timeout_and_vision_error(self):
        for response in (
            "bad json",
            '{"score":"2"}',
            '{"score":NaN,"hit":true,"tags":[],"reason":"x"}',
        ):
            self.context.llm_generate.return_value.completion_text = response
            r = await self.p._model_score(self.event, "friend_verification", "hello", 3)
            self.assertEqual(r["state"], "unknown")
        for error in (asyncio.TimeoutError(), RuntimeError("vision unsupported")):
            self.context.llm_generate.side_effect = error
            r = await self.p._model_score(
                self.event, "friend_avatar", "image", 2, b"png"
            )
            self.assertEqual(r["score"], 0)
            self.assertEqual(r["state"], "unknown")

    async def test_provider_fallback_and_options(self):
        self.config.update(provider_id="vision", vision_provider_id="")
        self.config.schema = {"provider_id": {}, "vision_provider_id": {}}
        self.p.providers.refresh()
        self.assertIn("vision", self.config.schema["provider_id"]["options"])
        self.assertEqual(await self.p.providers.resolve(self.event, True), "vision")
        self.config["vision_provider_id"] = "__current__"
        self.assertEqual(await self.p.providers.resolve(self.event, True), "text")
        self.config["provider_id"] = "__auto__"
        self.context.providers = []
        self.p.providers.refresh()
        self.assertEqual(await self.p.providers.resolve(self.event), "")
        r = await self.p._model_score(self.event, "friend_verification", "hello", 3)
        self.assertEqual(r["score"], 0)

    async def test_quote_required_all_three(self):
        for command in ("同意", "拒绝", "拉黑"):
            result = await collect(
                self.p._handle_command(Event(self.bot, text=command), command)
            )
            self.assertIn("引用", result[0])
        self.assertEqual(self.approvals(), [])

    async def test_normal_message_wrong_bot_wrong_session(self):
        await self.request()
        cases = [
            Event(self.bot, reply="ordinary"),
            self.reply_event(bot_id="54321"),
            self.reply_event(group="", sender="99999"),
            self.reply_event(platform="another"),
        ]
        for event in cases:
            result = await collect(self.p._handle_command(event, "同意"))
            self.assertIn("有效审核", result[0])
        self.assertFalse(self.approvals())

    async def test_unrelated_group_ignored_even_admin(self):
        result = await collect(
            self.p._handle_command(Event(self.bot, group="11111", admin=True), "同意")
        )
        self.assertEqual(result, [])
        self.assertFalse(self.approvals())

    async def test_expired_quote(self):
        await self.request()
        e = self.reply_event()
        self.pending()["created_at"] -= 73 * 3600
        result = await collect(self.p._handle_command(e, "同意"))
        self.assertIn("过期", result[0])
        self.assertFalse(self.approvals())

    async def test_concurrent_approval_once_and_group_subtype(self):
        await self.request("group")
        e = self.reply_event()
        a, b = await asyncio.gather(
            collect(self.p._handle_command(e, "同意")),
            collect(self.p._handle_command(e, "同意")),
        )
        self.assertEqual(len(self.approvals()), 1)
        self.assertEqual(self.approvals()[0][1]["sub_type"], "invite")
        self.assertTrue(any("已经处理" in x for x in a + b))

    async def test_approval_failure_no_success_no_rejection_count(self):
        await self.request()
        self.bot.fail_approval = True
        result = await collect(self.p._handle_command(self.reply_event("拒绝"), "拒绝"))
        self.assertIn("失败", result[0])
        self.assertEqual(self.pending()["status"], "pending")
        self.assertEqual(self.p.rejections, {})

    async def test_rejection_limit_and_success_reset(self):
        for i in range(3):
            await self.request(flag=str(i))
            record = next(x for x in self.p.pending.values() if x["flag"] == str(i))
            e = Event(self.bot, reply=record["message_id"], text="拒绝 测试 理由")
            await collect(self.p._handle_command(e, "拒绝", "测试"))
        self.assertIn("55555", self.p.blacklist["users"])
        self.assertEqual(self.p.history[-1]["manual_reason"], "测试 理由")
        await self.request(flag="later")
        self.assertFalse(self.approvals()[-1][1]["approve"])
        self.p.rejections["friend:66666"] = 2
        await self.request(flag="new", uid="66666")
        rec = next(x for x in self.p.pending.values() if x["flag"] == "new")
        await collect(
            self.p._handle_command(
                Event(self.bot, reply=rec["message_id"], text="同意"), "同意"
            )
        )
        self.assertNotIn("friend:66666", self.p.rejections)

    async def test_kick_operator_and_group_persist_before_query(self):
        await self.p._handle_kick(self.event, self.kick())
        self.assertIn("55555", self.p.blacklist["users"])
        self.assertNotIn("12345", self.p.blacklist["users"])
        self.assertIn("22222", self.p.blacklist["groups"])
        count = len(self.p.blacklist["sources"])
        await self.p._handle_kick(self.event, self.kick())
        self.assertEqual(len(self.p.blacklist["sources"]), count)
        self.assertEqual(self.p.rejections, {})
        restored = plugin.SmartRequestReview(self.context, self.config)
        self.assertEqual(restored.blacklist, self.p.blacklist)

    async def test_kick_other_member_leave_missing_operator(self):
        for raw in (
            self.kick(user_id="77777"),
            self.kick(sub_type="leave"),
            self.kick(operator_id="12345"),
        ):
            await self.p._handle_kick(self.event, raw)
        self.assertFalse(self.p.blacklist["users"])
        self.assertFalse(self.p.blacklist["groups"])
        await self.p._handle_kick(self.event, self.kick(operator_id="0"))
        self.assertFalse(self.p.blacklist["users"])
        self.assertEqual(self.p.blacklist["groups"], ["22222"])

    async def test_kick_toggles(self):
        self.config.update(kick_block_user=False, kick_block_group=True)
        await self.p._handle_kick(self.event, self.kick())
        self.assertEqual(self.p.blacklist["users"], [])
        self.assertEqual(self.p.blacklist["groups"], ["22222"])
        self.config.update(kick_block_user=True, kick_block_group=False)
        await self.p._handle_kick(self.event, self.kick(group_id="33333", time=123457))
        self.assertEqual(self.p.blacklist["users"], ["55555"])
        self.assertNotIn("33333", self.p.blacklist["groups"])

    async def test_kick_review_group_falls_back_private(self):
        await self.p._handle_kick(self.event, self.kick(group_id="88888"))
        self.assertTrue(
            any(
                a == "send_private_msg" and p["user_id"] == 99999
                for a, p in self.bot.calls
            )
        )
        self.assertFalse(
            any(
                a == "send_group_msg" and p["group_id"] == 88888
                for a, p in self.bot.calls
            )
        )

    async def test_old_pending_blocked_after_kick(self):
        await self.request("group")
        e = self.reply_event()
        await self.p._handle_kick(self.event, self.kick())
        result = await collect(self.p._handle_command(e, "同意"))
        self.assertIn("黑名单", result[0])
        self.assertFalse(self.approvals())

    async def test_restart_restores_pending_and_counts(self):
        await self.request()
        self.p.rejections["friend:55555"] = 2
        self.p._save_state()
        restored = plugin.SmartRequestReview(self.context, self.config)
        self.assertEqual(restored.pending, self.p.pending)
        self.assertEqual(restored.rejections, self.p.rejections)
        await collect(restored._handle_command(self.reply_event(), "同意"))
        self.assertEqual(len(self.approvals()), 1)

    async def test_image_failure_falls_back_with_scores(self):
        self.bot.fail_image = True
        await self.request()
        sends = [p["message"] for a, p in self.bot.calls if a == "send_group_msg"]
        self.assertEqual(len(sends), 2)
        self.assertEqual([x["type"] for x in sends[-1]], ["text"])
        self.assertIn("QQ等级", sends[-1][0]["data"]["text"])
        self.assertTrue(self.p.pending)

    async def test_render_failure_falls_back(self):
        self.p.renderer.render = lambda *a: None
        await self.request()
        self.assertTrue(self.p.pending)
        sent = next(p["message"] for a, p in self.bot.calls if a == "send_group_msg")
        self.assertEqual(sent[0]["type"], "text")

    async def test_failed_send_not_pending(self):
        self.bot.fail_send = True
        await self.request()
        self.assertEqual(self.p.pending, {})
        self.assertEqual(self.p.history[-1]["outcome"], "notification_failed")

    async def test_group_owner_approval_not_management(self):
        event = Event(self.bot, sender="77777")
        self.bot.role = "owner"
        self.assertTrue(await self.p._is_reviewer(event))
        self.assertFalse(await self.p._is_reviewer(event, True))
        self.config["review_session"] = ""
        self.assertFalse(await self.p._is_reviewer(event))

    async def test_long_lists_and_range_snapshot(self):
        await collect(self.p._send_list(self.event, "group"))
        self.bot.groups = list(reversed(self.bot.groups))
        self.event.message_str = "退群 1-3"
        await collect(self.p._relationship_action(self.event, "group", "1-3"))
        removed = [p["group_id"] for a, p in self.bot.calls if a == "set_group_leave"]
        self.assertEqual(sorted(removed), [20001, 20002, 20003])

    async def test_complete_list_and_large_list_keep_all_indexes(self):
        for count in (23, 245):
            self.bot.groups = [
                {"group_id": 20000 + i, "group_name": f"群{i}"} for i in range(count)
            ]
            rendered = []

            def render(title, entries, start, total, part, parts):
                rendered.extend(
                    (start + i, row["group_id"]) for i, row in enumerate(entries)
                )
                return b"image"

            self.p.renderer.render_list = render
            await collect(self.p._send_list(self.event, "group"))
            self.assertEqual([i for i, _ in rendered], list(range(count)))
            self.assertEqual(
                len(
                    self.p.list_snapshots[self.p._snapshot_key(self.event, "group")][1]
                ),
                count,
            )

    async def test_long_image_failure_splits_then_text_preserves_every_row(self):
        self.bot.fail_image = True
        self.bot.friends = [
            {"user_id": 10000 + i, "nickname": f"好友{i}"} for i in range(60)
        ]
        self.p.renderer.render_list = lambda *args: b"image"
        await collect(self.p._send_list(self.event, "friend"))
        texts = [
            p["message"][0]["data"]["text"]
            for a, p in self.bot.calls
            if a == "send_group_msg" and all(s["type"] == "text" for s in p["message"])
        ]
        self.assertEqual(len(texts), 4)
        lines = "\n".join(texts).splitlines()
        for i in range(60):
            self.assertEqual(lines.count(f"{i+1}. {10000+i} 好友{i}"), 1)

    async def test_adapter_expanded_at_with_digits_in_nickname(self):
        event = Event(self.bot, text="删好友 @玩家2026(10001)", ats=["10001"])
        await collect(self.p._relationship_action(event, "friend"))
        removed = [p["user_id"] for a, p in self.bot.calls if a == "delete_friend"]
        self.assertEqual(removed, [10001])
        event = Event(self.bot, text="加审批员 @玩家2026(77777)", ats=["77777"])
        await collect(self.p._manage_reviewer(event, True))
        self.assertIn("77777", self.p.admin_users())

    async def test_auto_success_deduplicated_after_restart(self):
        self.config["mode"] = "auto"
        await self.request()
        restored = plugin.SmartRequestReview(self.context, self.config)
        old = self.p
        self.p = restored
        await self.request()
        self.p = old
        self.assertEqual(len(self.approvals()), 1)

    async def test_no_stale_index_but_direct_id_allowed(self):
        self.event.message_str = "退群 1"
        result = await collect(self.p._relationship_action(self.event, "group"))
        self.assertIn("快照", result[0])
        self.event.message_str = "退群 20001"
        await collect(self.p._relationship_action(self.event, "group"))
        self.assertTrue(any(a == "set_group_leave" for a, p in self.bot.calls))

    async def test_at_delete_and_reviewer_management(self):
        event = Event(self.bot, text="删好友", ats=["10001"])
        await collect(self.p._relationship_action(event, "friend"))
        self.assertTrue(
            any(
                a == "delete_friend" and p["user_id"] == 10001
                for a, p in self.bot.calls
            )
        )
        event = Event(self.bot, text="加审批员", ats=["77777"])
        await collect(self.p._manage_reviewer(event, True))
        self.assertIn("77777", self.p.admin_users())
        event = Event(self.bot, sender="77777", text="减审批员", ats=["99999"])
        await collect(self.p._manage_reviewer(event, False))
        self.assertNotIn("99999", self.p.admin_users())


class UnitTests(unittest.TestCase):
    def test_schema_unique_and_defaults(self):
        def unique(pairs):
            keys = [k for k, v in pairs]
            self.assertEqual(len(keys), len(set(keys)))
            return dict(pairs)

        schema = json.loads(
            (ROOT / "_conf_schema.json").read_text(encoding="utf-8"),
            object_pairs_hook=unique,
        )
        self.assertEqual(schema["group_member_max"]["default"], 999999)
        self.assertEqual(schema["group_member_max_score"]["default"], 2)
        self.assertEqual(schema["friend_score_threshold"]["default"], -1)
        for key in ("provider_id", "vision_provider_id"):
            self.assertEqual(schema[key]["_special"], "select_provider")

    def test_summary_block_only_when_rejected(self):
        r = {
            "hard_rule": {"action": "score"},
            "score": 5,
            "threshold": 5,
            "rejection_count": 2,
        }
        self.assertEqual(summary(r, 3)["action"], "approve")
        r["score"] = 4
        self.assertEqual(summary(r, 3)["action"], "block")
        self.assertIn("3 次", summary(r, 3)["reason"])

    def test_strict_model_and_cap(self):
        self.assertEqual(
            model_result('{"score":8,"hit":true,"reason":"ok","tags":[]}', 2)["score"],
            2,
        )
        for value in (
            "{}",
            "null",
            '{"score":true,"hit":true,"reason":"x","tags":[]}',
            "prefix {} suffix",
        ):
            self.assertEqual(model_result(value, 2)["state"], "unknown")

    def test_render_cards_and_list(self):
        from PIL import Image

        render = ReviewCardRenderer()
        for kind in ("friend", "group"):
            p = {
                "kind": kind,
                "profile": (
                    {"nickname": "中文长昵称" * 30, "comment": "验证信息" * 200}
                    if kind == "friend"
                    else {}
                ),
                "group": {"name": "测试群", "memo": "长群简介" * 150},
                "score": 3,
                "threshold": 5,
                "hard_rule": {"reason": "未命中硬规则"},
                "items": [
                    {
                        "key": "test",
                        "name": "头像识别",
                        "score": 2,
                        "max": 2,
                        "state": "scored",
                        "reason": "动漫角色" * 60,
                        "tags": ["动漫"],
                    }
                ],
                "recommendation": {"action": "reject", "reason": "资料不足" * 30},
                "missing": ["接口失败" * 40],
            }
            data = render.render(p)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.width, 1120)
                self.assertLess(image.height, 4000)
        data = render.render_list(
            "好友列表",
            [{"user_id": 10000 + i, "nickname": "中文名字" * 50} for i in range(21)],
        )
        with Image.open(io.BytesIO(data)) as image:
            self.assertGreater(image.height, 2000)
        self.assertLessEqual(len(lines_for("长名字" * 100, 100, max_lines=2)), 2)


if __name__ == "__main__":
    unittest.main()
