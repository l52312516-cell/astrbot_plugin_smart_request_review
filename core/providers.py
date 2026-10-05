from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ProviderSelector:
    """Discover AstrBot chat providers and resolve configured selections."""

    def __init__(self, context: Any, config: Any, schema_path: Path):
        self.context = context
        self.config = config
        self.schema_path = schema_path
        self.entries: list[tuple[str, str]] = []

    def _all(self) -> list[Any]:
        try:
            providers = list(self.context.get_all_providers() or [])
            if providers:
                return providers
        except Exception:
            pass
        try:
            manager = self.context.provider_manager
            return list(
                getattr(manager, "provider_insts", []) or manager.get_insts() or []
            )
        except Exception:
            return []

    def refresh(self) -> list[tuple[str, str]]:
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for provider in self._all():
            try:
                meta = provider.meta()
            except Exception:
                meta = None
            capability = getattr(meta, "provider_type", None)
            if (
                capability is not None
                and getattr(capability, "value", capability) != "chat_completion"
            ):
                continue
            pid = str(
                getattr(meta, "id", "") or getattr(provider, "id", "") or ""
            ).strip()
            name = str(
                getattr(meta, "name", "")
                or getattr(provider, "provider_name", "")
                or pid
            ).strip()
            if pid and pid not in seen:
                seen.add(pid)
                result.append((pid, f"{name} ({pid})" if name and name != pid else pid))
        self.entries = result
        options = ["", "__current__", "__auto__"] + [pid for pid, _ in result]
        labels = ["自动回退", "当前会话模型", "自动选择可用模型"] + [
            label for _, label in result
        ]
        for key in ("provider_id", "vision_provider_id"):
            configured = str(self.config.get(key, "") or "")
            if configured and configured not in options:
                options.append(configured)
                labels.append(configured + "（当前未加载）")
        try:
            schema = getattr(self.config, "schema", None)
            if isinstance(schema, dict):
                for key in ("provider_id", "vision_provider_id"):
                    if isinstance(schema.get(key), dict):
                        schema[key]["options"] = options
                        schema[key]["labels"] = labels
        except Exception:
            pass
        try:
            data = json.loads(self.schema_path.read_text(encoding="utf-8"))
            for key in ("provider_id", "vision_provider_id"):
                data.setdefault(key, {})["options"] = options
                data.setdefault(key, {})["labels"] = labels
            rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            if self.schema_path.read_text(encoding="utf-8") != rendered:
                temporary = self.schema_path.with_suffix(".json.tmp")
                temporary.write_text(rendered, encoding="utf-8")
                temporary.replace(self.schema_path)
        except Exception:
            pass
        return result

    async def resolve(self, event: Any, vision: bool = False) -> str:
        key = "vision_provider_id" if vision else "provider_id"
        try:
            configured = str(self.config.get(key, "") or "").strip()
        except Exception:
            configured = ""
        if vision and not configured:
            return await self.resolve(event, False)
        if configured not in {"", "__current__", "__auto__"}:
            return configured
        if configured in {"", "__current__"}:
            try:
                current = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                if current:
                    return str(current)
            except Exception:
                pass
        if not self.entries:
            self.refresh()
        return self.entries[0][0] if self.entries else ""
