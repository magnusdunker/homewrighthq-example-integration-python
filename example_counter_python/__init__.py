"""Eksempel-tæller: en ekstern integration til TheHome, skrevet i Python.

Hver opsætning får en enhed med

- en sensor, der tæller én op med et fast interval,
- en kontakt, der sætter tælleren på pause,
- en knap, der nulstiller den.

Den viser også et opsætningsforløb med validering, et options-forløb, en
tjeneste (``example_counter_python.set``), en hændelse og en fil i
integrationens datamappe, så tallet overlever en genstart.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from thehome_plugin import Device, Entity, Entry, Flow, FlowReply, Host, Integration, PluginError, log

__version__ = "0.1.0"

DOMAIN = "example_counter_python"


def user_schema(name: str, interval: int) -> dict:
    return {
        "type": "object",
        "required": ["name", "interval"],
        "properties": {
            "name": {"type": "string", "title": "Navn", "default": name},
            "interval": {
                "type": "integer",
                "title": "Interval (sekunder)",
                "minimum": 1,
                "maximum": 3600,
                "default": interval,
                "description": "Hvor tit tælleren tæller én op",
            },
        },
    }


def options_schema(interval: int) -> dict:
    return {
        "type": "object",
        "required": ["interval"],
        "properties": {
            "interval": {"type": "integer", "title": "Interval (sekunder)", "minimum": 1, "maximum": 3600, "default": interval},
        },
    }


def interval_of(entry: Entry) -> int:
    try:
        v = int(entry.get("interval", 10))
    except (TypeError, ValueError):
        v = 10
    return min(max(v, 1), 3600)


async def register_set_service(host: Host, entry_id: str) -> None:
    """``example_counter_python.set`` gælder alle opsætninger; målet afgør
    hvilken. TheHome knytter tjenesten til den opsætning, der registrerede den."""
    await host.register_service(
        entry_id,
        "set",
        {
            "name": "Sæt tæller",
            "description": "Sætter en tæller til en bestemt værdi",
            "target": {"entity": {"integration": DOMAIN, "domain": "sensor"}},
            "fields": {
                "value": {"name": "Værdi", "required": True, "example": 100, "selector": {"number": {"min": 0, "max": 1000000}}},
            },
        },
    )


#: Hvor tit tællerens stand skrives i TheHomes log (sekunder).
LOG_EVERY = 10


@dataclass
class Counter:
    name: str
    count: int
    running: bool
    count_id: Optional[str]
    running_id: Optional[str]
    reset_id: Optional[str]
    task: Optional[asyncio.Task] = None
    log_task: Optional[asyncio.Task] = None


class Example(Integration):
    def __init__(self) -> None:
        self.counters: dict[str, Counter] = {}

    # -- filer -------------------------------------------------------------

    @staticmethod
    def _file(host: Host, entry_id: str):
        return host.data_dir / f"{entry_id}.json"

    @classmethod
    def load(cls, host: Host, entry_id: str) -> int:
        try:
            return int(json.loads(cls._file(host, entry_id).read_text("utf-8")).get("count", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    @classmethod
    def save(cls, host: Host, entry_id: str, count: int) -> None:
        try:
            host.data_dir.mkdir(parents=True, exist_ok=True)
            cls._file(host, entry_id).write_text(json.dumps({"count": count}), "utf-8")
        except OSError as e:
            log("WARN", f"kunne ikke gemme tælleren: {e}")

    # -- tilstande ---------------------------------------------------------

    async def publish(self, host: Host, entry_id: str) -> None:
        """Send tællerens og kontaktens tilstand til TheHome."""
        c = self.counters.get(entry_id)
        if c is None:
            return
        if c.count_id:
            await host.set_state(c.count_id, c.count)
        if c.running_id:
            await host.set_state(c.running_id, "on" if c.running else "off")

    async def set_count(self, host: Host, entry_id: str, value: int) -> None:
        c = self.counters.get(entry_id)
        if c is None:
            raise PluginError.not_ready("opsætningen kører ikke")
        c.count = value
        self.save(host, entry_id, value)
        await self.publish(host, entry_id)

    async def reset(self, host: Host, entry_id: str) -> None:
        await self.set_count(host, entry_id, 0)
        c = self.counters.get(entry_id)
        if c and c.reset_id:
            # en knaps tilstand er tidspunktet for sidste tryk
            await host.set_state(c.reset_id, datetime.now(timezone.utc).isoformat())
        await host.fire_event(f"{DOMAIN}_reset", {"entry_id": entry_id})

    async def tick(self, host: Host, entry_id: str, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            c = self.counters.get(entry_id)
            if c is None:
                return
            if not c.running:
                continue
            c.count += 1
            self.save(host, entry_id, c.count)
            if c.count_id:
                try:
                    await host.set_state(c.count_id, c.count)
                except PluginError as e:
                    log("WARN", f"kunne ikke opdatere {c.count_id}: {e}")

    async def log_state(self, entry_id: str) -> None:
        while True:
            await asyncio.sleep(LOG_EVERY)
            c = self.counters.get(entry_id)
            if c is None:
                return
            log("INFO", f"{c.name}: tælleren står på {c.count} ({'kører' if c.running else 'pause'})")

    # -- forløb ------------------------------------------------------------

    async def config_flow(self, host: Host, flow: Flow, step: str, user_input: Optional[dict]) -> FlowReply:
        if step != "user":
            raise PluginError.invalid(f"ukendt trin {step}")
        if user_input is None:
            return FlowReply.form("user", user_schema("Tæller", 10))
        name = str(user_input.get("name") or "").strip()
        try:
            interval = int(user_input.get("interval", 10))
        except (TypeError, ValueError):
            interval = 10
        if not name:
            return FlowReply.form_with_errors("user", user_schema("", interval), {"name": "required"})
        return FlowReply.create(name, {"name": name, "interval": interval}).with_unique_id(name.lower())

    async def options_flow(self, host: Host, flow: Flow, step: str, user_input: Optional[dict]) -> FlowReply:
        current = interval_of(flow.entry) if flow.entry else 10
        if user_input is None:
            return FlowReply.form("init", options_schema(current))
        try:
            interval = int(user_input.get("interval", current))
        except (TypeError, ValueError):
            interval = current
        return FlowReply.create("", {"interval": interval})

    # -- opsætning ---------------------------------------------------------

    async def setup_entry(self, host: Host, entry: Entry) -> None:
        entry_id = entry.entry_id
        name = entry.get("name") or entry.title
        interval = interval_of(entry)

        device = await host.register_device(
            entry_id,
            Device(
                name=name,
                identifiers=[(DOMAIN, entry_id)],
                manufacturer="TheHome",
                model="Eksempel-tæller (Python)",
                sw_version=__version__,
            ),
        )
        count_id = await host.add_entity(
            entry_id,
            Entity("sensor", f"{entry_id}-count", name="Antal", device_id=device, icon="mdi:counter", state_class="total_increasing"),
        )
        running_id = await host.add_entity(
            entry_id, Entity("switch", f"{entry_id}-running", name="Tæller", device_id=device, icon="mdi:play-pause"), handler=True
        )
        reset_id = await host.add_entity(
            entry_id, Entity("button", f"{entry_id}-reset", name="Nulstil", device_id=device, icon="mdi:restart"), handler=True
        )
        await register_set_service(host, entry_id)

        count = self.load(host, entry_id)
        counter = Counter(name=name, count=count, running=True, count_id=count_id, running_id=running_id, reset_id=reset_id)
        self.counters[entry_id] = counter
        if reset_id:
            await host.set_state(reset_id, "unknown")
        await self.publish(host, entry_id)
        counter.task = asyncio.create_task(self.tick(host, entry_id, interval))
        counter.log_task = asyncio.create_task(self.log_state(entry_id))
        log("INFO", f"{name} tæller hvert {interval}. sekund fra {count}")

    async def unload_entry(self, host: Host, entry_id: str) -> None:
        c = self.counters.pop(entry_id, None)
        if c:
            for task in (c.task, c.log_task):
                if task:
                    task.cancel()
        # TheHome fjerner tjenesten med opsætningen; de andre skal stadig have den
        other = next(iter(self.counters), None)
        if other:
            await register_set_service(host, other)

    # -- tjenester ---------------------------------------------------------

    async def entity_service(self, host: Host, entry_id: str, entity_id: str, service: str, data: dict) -> None:
        c = self.counters.get(entry_id)
        if c is None:
            raise PluginError.unavailable(entity_id)
        if entity_id == c.running_id and service in ("turn_on", "turn_off", "toggle"):
            c.running = {"turn_on": True, "turn_off": False}.get(service, not c.running)
            await self.publish(host, entry_id)
        elif entity_id == c.reset_id and service == "press":
            await self.reset(host, entry_id)
        else:
            raise PluginError.not_supported(f"{service} på {entity_id}")

    async def domain_service(self, host: Host, entry_id: str, service: str, data: dict, entity_ids: list[str]) -> Any:
        if service != "set":
            raise PluginError.not_supported(service)
        value = data.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PluginError.invalid("value mangler")
        if value < 0:
            raise PluginError.invalid("value skal være 0 eller mere")
        targets = [eid for eid, c in self.counters.items() if c.count_id in entity_ids or c.running_id in entity_ids]
        if not targets:
            raise PluginError.invalid("vælg en tæller som mål")
        for eid in targets:
            await self.set_count(host, eid, int(value))
        return None

    async def diagnostics(self, host: Host, entry: Entry) -> Any:
        c = self.counters.get(entry.entry_id)
        if c is None:
            return {"loaded": False}
        return {"count": c.count, "running": c.running, "interval": interval_of(entry)}
