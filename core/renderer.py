from __future__ import annotations

import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from .profiles import meaningful

RESOURCE_DIR = Path(__file__).resolve().parent / "resource"
STATE_NAMES = {
    "scored": "已评分",
    "unknown": "未知 / 计 0 分",
    "disabled": "已关闭",
    "skipped": "硬规则跳过",
}
COLORS = {"approve": "#087f5b", "reject": "#c23b40", "block": "#ad5e08"}


@lru_cache(maxsize=12)
def font(size: int):
    return ImageFont.truetype(str(RESOURCE_DIR / "NotoSansCJKsc-Regular.otf"), size)


def text_value(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "未知"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def lines_for(value, width, size=25, max_lines=3):
    """Pixel-measured wrapping works for Chinese, emoji and long unbroken IDs."""
    face = font(size)
    text = text_value(value).replace("\r", "").replace("\t", " ")[:6000]
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
        fields = (
            [
                ("QQ", p.get("user_id")),
                ("昵称", p.get("nickname")),
                ("QQ等级", p.get("level")),
                (
                    "性别 / 地区",
                    " / ".join(
                        str(p[k])
                        for k in ("sex", "area")
                        if meaningful(p.get(k)) and p[k] != "unknown"
                    ),
                ),
                ("签名", p.get("signature")),
                ("验证信息", p.get("comment")),
            ]
            if kind == "friend"
            else [
                ("群号", p.get("group_id")),
                ("群名称", p.get("name")),
                ("群备注", p.get("remark")),
                (
                    "人数 / 上限",
                    (
                        str(p["member_count"])
                        if meaningful(p.get("member_count"))
                        else ""
                    )
                    + (
                        f" / {p['max_member_count']}"
                        if meaningful(p.get("member_count"))
                        and meaningful(p.get("max_member_count"))
                        else ""
                    ),
                ),
                ("群等级", p.get("level")),
                (
                    "邀请人",
                    f"{p.get('inviter_nickname') or ''} ({p.get('inviter_id') or record.get('subject_id') or ''})".strip(),
                ),
                ("验证信息", p.get("comment")),
                ("群简介", p.get("memo")),
                (
                    "群主 / 管理员",
                    "、".join(
                        str(x.get("card") or x.get("nickname") or "未知")
                        for x in p.get("admins", [])
                        if isinstance(x, dict)
                    )
                    or None,
                ),
                ("公告摘要", p.get("notices")),
                ("精华摘要", p.get("essence")),
                ("荣誉摘要", p.get("honor")),
            ]
        )
        section("申请资料")
        for label, value in fields:
            if meaningful(value):
                add(f"{label}：{text_value(value)}", max_lines=2)
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
        skipped = any(x.get("state") == "skipped" for x in items) and all(
            x.get("state") in {"skipped", "disabled"} for x in items
        )
        add(
            (
                "评分未执行 · 硬规则决定结果"
                if skipped
                else f"总分  {total} / 10     通过阈值  {record.get('threshold', 5)}"
            ),
            34,
            "#173f68",
            2,
        )
        if not skipped:
            addition = " + ".join(str(x["score"]) for x in items)
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
        conclusion = record.get(
            "recommendation", {"action": "reject", "reason": "待判断"}
        )
        action = conclusion["action"]
        label = {"approve": "建议同意", "reject": "建议拒绝", "block": "建议拉黑"}[
            action
        ]
        color = COLORS[action]
        section("最终结论")
        add(f"{label}：{conclusion['reason']}", 32, color, 3)
        if outcome != "待审批":
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
            ident = entry.get("group_id") or entry.get("user_id") or "未知"
            name = entry.get("group_name") or entry.get("nickname") or "未知"
            rows.append((f"{index}.  {ident}", lines_for(name, 710, 25, 2)))
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
