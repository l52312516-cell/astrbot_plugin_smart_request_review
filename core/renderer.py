from __future__ import annotations

import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from .profiles import clean_text, meaningful, profile_value
from .scoring import (
    MANUAL_COLOR,
    conclusion_label,
    escalation_line,
    possible_total,
)

RESOURCE_DIR = Path(__file__).resolve().parent / "resource"
STATE_NAMES = {
    "scored": "已评分",
    "unknown": "未知 / 计 0 分",
    "disabled": "已关闭",
    "skipped": "硬规则跳过",
}


@lru_cache(maxsize=12)
def font(size: int):
    return ImageFont.truetype(str(RESOURCE_DIR / "NotoSansCJKsc-Regular.otf"), size)


def text_value(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "未知"
    if isinstance(value, str):
        return clean_text(value) or "未知"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def lines_for(value, width, size=25, max_lines=3):
    """Pixel-measured wrapping works for Chinese, emoji and long unbroken IDs."""
    face = font(size)
    text = text_value(value).replace("\r", "").replace("\t", " ")[:6000]
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    lines, line = [], ""
    for char in text:
        if char == "\n" or face.getlength(line + char) > width:
            lines.append(line)
            line = "" if char == "\n" else char
        else:
            line += char
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and line:
        lines.append(line)
    if len(lines) >= max_lines and len("\n".join(lines)) < len(text):
        suffix = "…（已截断）"
        last = lines[-1]
        while last and face.getlength(last + suffix) > width:
            last = last[:-1]
        lines[-1] = last + suffix
    return lines or ["未知"]


class ReviewCardRenderer:
    def render(self, record, outcome="待审批", error=""):
        width, pad = 1120, 40
        content_width = width - pad * 2
        blocks = []

        def add(text, size=25, color="#283748", max_lines=3, gap=12, indent=0):
            wrapped = lines_for(text, content_width - indent, size, max_lines)
            blocks.append(("text", wrapped, size, color, gap, indent))

        def section(title):
            add(title, 29, "#224c77", 1, 16)

        kind = record["kind"]
        p = record.get("profile") or record.get("group") or {}
        if kind == "friend":
            sex = profile_value(p, "sex")
            sex = {"male": "男", "female": "女"}.get(
                str(sex).casefold(), "" if sex is None else str(sex)
            )
            area = clean_text(profile_value(p, "area", default=""))
            location = " / ".join(value for value in (sex, area) if value)
            fields = [
                ("QQ", profile_value(p, "user_id")),
                ("昵称", profile_value(p, "nickname", "nick", "name")),
                ("QQ等级", profile_value(p, "level", "qq_level", "qqLevel")),
                ("性别 / 地区", location),
                ("签名", profile_value(p, "signature", "long_nick", "longNick")),
                ("验证信息", profile_value(p, "comment", "message")),
            ]
        else:
            count = profile_value(p, "member_count")
            maximum = profile_value(p, "max_member_count")
            count_label = "人数 / 上限" if count and maximum else "群人数"
            count_text = f"{count} / {maximum}" if count and maximum else count
            if not count and maximum:
                count_label, count_text = "人数上限", maximum
            inviter_name = clean_text(profile_value(p, "inviter_nickname", default=""))
            inviter_id = clean_text(
                profile_value(p, "inviter_id", default=record.get("subject_id", ""))
            )
            inviter = ""
            if inviter_name and inviter_id:
                inviter = f"{inviter_name}（QQ {inviter_id}）"
            elif inviter_name:
                inviter = inviter_name
            elif inviter_id:
                inviter = f"QQ {inviter_id}"
            description = profile_value(p, "description", "group_description")
            memo = profile_value(p, "memo", "group_memo", "groupMemo")
            fields = [
                ("群号", profile_value(p, "group_id")),
                ("群名称", profile_value(p, "name", "group_name", "groupName")),
                ("群备注", profile_value(p, "remark", "group_remark", "groupRemark")),
                (count_label, count_text),
                ("群等级", profile_value(p, "level", "group_level", "groupLevel")),
                ("邀请人", inviter),
                ("验证信息", profile_value(p, "comment", "message")),
                ("群简介", description or memo),
                ("群公告", memo if description and memo != description else None),
            ]
        section("申请资料")
        seen_fields = set()
        for label, value in fields:
            if meaningful(value):
                rendered = text_value(value)
                if label in {"群备注", "群简介", "群公告"} and rendered in seen_fields:
                    continue
                seen_fields.add(rendered)
                add(f"{label}：{rendered}", max_lines=2)
        section("审核依据")
        add(
            "硬规则：" + str(record.get("hard_rule", {}).get("reason", "未知")),
            color="#5d526b",
            max_lines=2,
        )
        total, raw = record.get("score", 0), record.get(
            "score_raw", record.get("score", 0)
        )
        items = record.get("items", [])
        active = [
            entry
            for entry in items
            if entry.get("state") != "disabled" and entry.get("max", 0) > 0
        ]
        evidence = record.get("score_evidence") or {}
        if evidence.get("enabled_max", 0) > 0 and any(
            entry.get("state") in {"scored", "unknown"} for entry in active
        ):
            coverage = max(0, min(1, evidence.get("coverage", 0)))
            add(
                f"有效评分依据 {evidence.get('evaluated_max', 0)} / {evidence['enabled_max']}（{coverage:.0%}）",
                24,
                "#536273",
                1,
            )
        skipped = any(x.get("state") == "skipped" for x in items) and all(
            x.get("state") in {"skipped", "disabled"} for x in items
        )
        add(
            (
                "评分未执行 · 硬规则决定结果"
                if skipped
                else (
                    "无启用评分项"
                    if not active
                    else f"总分  {total} / {possible_total(record)}     通过阈值  {record.get('threshold', 5)}"
                )
            ),
            34,
            "#173f68",
            2,
        )
        if not skipped and active:
            addition = " + ".join(str(x["score"]) for x in active)
            add(
                f"{addition} = {raw}" + ("，封顶后为 10 分" if raw > 10 else " 分"),
                23,
                "#67798a",
                2,
            )
        for entry in items:
            state = entry.get("state", "unknown")
            color = "#087f5b" if entry.get("score", 0) > 0 else "#697585"
            add(
                f"{entry['name']}     {entry['score']} / {entry['max']}     {STATE_NAMES.get(state, state)}",
                27,
                color,
                1,
                6,
            )
            ratio = entry["score"] / entry["max"] if entry["max"] else 0
            blocks.append(("bar", max(0, min(1, ratio)), color))
            tags = " · ".join(entry.get("tags") or [])
            add(
                (f"[{tags}] " if tags else "")
                + str(entry.get("reason") or "未提供理由"),
                23,
                "#536273",
                2,
                14,
                12,
            )
        add(f"累计拒绝：{record.get('rejection_count', 0)} 次", 25, "#44576a", 1)
        if error:
            add("执行失败：" + error, 25, "#c23b40", 3)
        if record.get("manual_reason"):
            add("人工理由：" + record["manual_reason"], 25, "#44576a", 3)
        conclusion = record.get("recommendation") or {
            "action": "reject",
            "reason": "待判断",
        }
        label, color = conclusion_label(record)
        section("原审核建议" if record.get("status") == "processed" else "审核建议")
        add(f"{label}：{conclusion['reason']}", 32, color, 3)
        escalation = escalation_line(record)
        if escalation:
            add(escalation, 26, MANUAL_COLOR, 3)
        if outcome != "待审批":
            section("最终处理结果")
            add("执行结果：" + outcome, 29, "#224c77", 2)
        else:
            add(
                "等待审核员引用本消息：同意 / 拒绝 [理由] / 拉黑 [理由]",
                23,
                "#536273",
                2,
            )

        header_height = 230
        height = header_height + 36
        for block in blocks:
            height += (
                18 if block[0] == "bar" else len(block[1]) * (block[2] + 12) + block[4]
            )
        canvas = Image.new("RGB", (width, height), "#edf3f8")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((16, 16, width - 16, height - 16), 24, fill="white")
        draw.rounded_rectangle((30, 30, width - 30, 205), 18, fill="#e9f2fa")
        avatar_box = (48, 48, 192, 192)
        avatar = record.get("avatar") or p.get("avatar")
        title_x = 48
        try:
            with Image.open(io.BytesIO(avatar or b"")) as source:
                av = ImageOps.fit(source.convert("RGB"), (144, 144))
            canvas.paste(av, avatar_box[:2])
            title_x = 218
        except Exception:
            pass
        title = "好友申请" if kind == "friend" else "群聊邀请"
        draw.text((title_x, 50), title + " · 智能审核", font=font(39), fill="#193d61")
        status = lines_for(outcome, 810, 26, 2)
        for i, line in enumerate(status):
            draw.text((title_x, 112 + i * 36), line, font=font(26), fill="#3d5f7d")
        y = header_height
        for block in blocks:
            if block[0] == "bar":
                _, ratio, bar_color = block
                draw.rounded_rectangle((pad, y, width - pad, y + 7), 3, fill="#e7edf3")
                if ratio:
                    draw.rounded_rectangle(
                        (pad, y, pad + content_width * ratio, y + 7), 3, fill=bar_color
                    )
                y += 18
            else:
                _, lines, size, color, gap, indent = block
                for line in lines:
                    draw.text((pad + indent, y), line, font=font(size), fill=color)
                    y += size + 12
                y += gap
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def render_list(self, title, entries, start_index=0, total=None, part=1, parts=1):
        total = len(entries) if total is None else total
        selected = entries
        rows = []
        for index, entry in enumerate(selected, start_index + 1):
            ident = profile_value(entry, "group_id", "user_id", default="")
            name = profile_value(
                entry,
                "group_name",
                "nickname",
                "remark",
                "group_remark",
                default="",
            )
            rows.append(
                (
                    f"{index}.  {ident}" if meaningful(ident) else f"{index}.",
                    lines_for(name, 710, 25, 2) if meaningful(name) else [],
                )
            )
        height = 172 + sum(max(80, len(name) * 38 + 22) for _, name in rows)
        canvas = Image.new("RGB", (1120, height), "#f1f6fb")
        draw = ImageDraw.Draw(canvas)
        draw.text((40, 26), title, font=font(38), fill="#173f68")
        suffix = f" · 长图 {part}/{parts}" if parts > 1 else ""
        draw.text(
            (40, 84),
            f"共 {total} 条 · 序号有效期 10 分钟{suffix}",
            font=font(24),
            fill="#647589",
        )
        y = 144
        for ident, names in rows:
            row_h = max(80, len(names) * 38 + 22)
            draw.rounded_rectangle((28, y, 1092, y + row_h - 8), 12, fill="white")
            draw.text((42, y + 12), ident, font=font(24), fill="#173f68")
            for i, name in enumerate(names):
                draw.text((350, y + 12 + i * 38), name, font=font(25), fill="#34475a")
            y += row_h
        if not rows:
            draw.text((40, 132), "列表为空", font=font(25), fill="#647589")
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
