"""SDK til eksterne TheHome-integrationer i Python.

En ekstern integration er et selvstændigt program. TheHome starter det og
taler med det over JSON-RPC 2.0 på stdin og stdout, én besked pr. linje.
stderr er integrationens log. Programmet stopper, når stdin lukkes.

Lav en underklasse af :class:`Integration` og kald :func:`run`::

    from thehome_plugin import Entity, Integration, run

    class Mine(Integration):
        async def setup_entry(self, host, entry):
            entity_id = await host.add_entity(entry.entry_id, Entity("sensor", "mine-1"))
            if entity_id:
                await host.set_state(entity_id, "42")

    run(Mine())

Kun standardbiblioteket bruges.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, TextIO

__all__ = [
    "API_VERSION",
    "Device",
    "Entity",
    "Entry",
    "Flow",
    "FlowReply",
    "Host",
    "Integration",
    "PluginError",
    "log",
    "run",
    "serve",
]

#: Versionen af protokollen, som denne SDK taler.
API_VERSION = 1

HOST_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# fejl


class PluginError(Exception):
    """En fejl til TheHome. ``kind`` afgør, hvad TheHome gør.

    ``not_ready`` prøver igen senere, ``auth_failed`` beder brugeren logge ind
    igen, ``invalid_argument``, ``not_supported``, ``unavailable`` og
    ``not_found`` vises ved tjenestekald, og ``failed`` er alt andet.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message

    @classmethod
    def not_ready(cls, message: str) -> "PluginError":
        return cls("not_ready", message)

    @classmethod
    def auth_failed(cls, message: str) -> "PluginError":
        return cls("auth_failed", message)

    @classmethod
    def failed(cls, message: str) -> "PluginError":
        return cls("failed", message)

    @classmethod
    def invalid(cls, message: str) -> "PluginError":
        return cls("invalid_argument", message)

    @classmethod
    def not_supported(cls, message: str) -> "PluginError":
        return cls("not_supported", message)

    @classmethod
    def unavailable(cls, message: str) -> "PluginError":
        return cls("unavailable", message)

    def to_wire(self) -> dict:
        return {"code": -32000, "message": self.message, "data": {"kind": self.kind}}


# ---------------------------------------------------------------------------
# data fra TheHome


@dataclass
class Entry:
    """En opsætning af integrationen (en "config entry")."""

    entry_id: str
    domain: str = ""
    title: str = ""
    data: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    unique_id: Optional[str] = None

    @classmethod
    def from_json(cls, v: Any) -> "Entry":
        v = v if isinstance(v, dict) else {}
        return cls(
            entry_id=v.get("entry_id", ""),
            domain=v.get("domain") or "",
            title=v.get("title") or "",
            data=v.get("data") if isinstance(v.get("data"), dict) else {},
            options=v.get("options") if isinstance(v.get("options"), dict) else {},
            unique_id=v.get("unique_id"),
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Værdi fra options først, ellers fra data."""
        if key in self.options:
            return self.options[key]
        return self.data.get(key, default)


@dataclass
class Flow:
    """Et opsætningsforløb i gang."""

    flow_id: str = ""
    #: ``user``, ``options``, ``reauth``, ``zeroconf`` ...
    source: str = ""
    discovery: Any = None
    #: Data samlet i tidligere trin.
    data: dict = field(default_factory=dict)
    unique_id: Optional[str] = None
    #: Ved options- og reauth-forløb: opsætningen, der ændres.
    entry: Optional[Entry] = None

    @classmethod
    def from_json(cls, v: Any) -> "Flow":
        v = v if isinstance(v, dict) else {}
        return cls(
            flow_id=v.get("flow_id") or "",
            source=v.get("source") or "",
            discovery=v.get("discovery"),
            data=v.get("data") if isinstance(v.get("data"), dict) else {},
            unique_id=v.get("unique_id"),
            entry=Entry.from_json(v["entry"]) if isinstance(v.get("entry"), dict) else None,
        )


class FlowReply:
    """Svaret på et trin i et opsætningsforløb."""

    def __init__(self, result: dict):
        self.result = result
        self.data: Optional[dict] = None
        self.unique_id: Optional[str] = None

    @classmethod
    def form(cls, step_id: str, schema: dict) -> "FlowReply":
        """Vis en formular. ``schema`` er et JSON Schema-objekt (``type: object``)."""
        return cls({"type": "form", "step_id": step_id, "schema": schema})

    @classmethod
    def form_with_errors(cls, step_id: str, schema: dict, errors: dict) -> "FlowReply":
        """Vis formularen igen med fejl, f.eks. ``{"base": "cannot_connect"}``."""
        return cls({"type": "form", "step_id": step_id, "schema": schema, "errors": errors})

    @classmethod
    def menu(cls, step_id: str, options: list[str]) -> "FlowReply":
        return cls({"type": "menu", "step_id": step_id, "options": list(options)})

    @classmethod
    def create(cls, title: str, data: dict) -> "FlowReply":
        """Afslut og opret opsætningen. I et options-forløb bliver ``data`` de nye options."""
        return cls({"type": "create_entry", "title": title, "data": data})

    @classmethod
    def abort(cls, reason: str) -> "FlowReply":
        return cls({"type": "abort", "reason": reason})

    def with_data(self, data: dict) -> "FlowReply":
        """Gem data til de næste trin."""
        self.data = data
        return self

    def with_unique_id(self, unique_id: str) -> "FlowReply":
        """Sæt et unikt id. TheHome afbryder med ``already_configured``, hvis en
        anden opsætning har det."""
        self.unique_id = unique_id
        return self

    def to_json(self) -> dict:
        out: dict = {"result": self.result}
        if self.data is not None:
            out["data"] = self.data
        if self.unique_id is not None:
            out["unique_id"] = self.unique_id
        return out


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class Device:
    """En enhed. Felterne svarer til TheHomes enhedsregister."""

    name: str
    #: F.eks. ``[("mit_domæne", "serienummer")]``. Holder enheden stabil.
    identifiers: list = field(default_factory=list)
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    sw_version: Optional[str] = None
    serial_number: Optional[str] = None
    suggested_area: Optional[str] = None
    configuration_url: Optional[str] = None

    def to_json(self) -> dict:
        d = _compact(asdict(self))
        d["identifiers"] = [list(i) for i in self.identifiers]
        return d


@dataclass
class Entity:
    """En entity. Felterne svarer til TheHomes entity-register."""

    #: ``sensor``, ``switch``, ``light``, ``button``, ``climate`` ...
    domain: str
    #: Stabilt id, der aldrig må ændre sig.
    unique_id: str
    #: Navn efter enhedens navn. ``None`` betyder enhedens hoved-entity.
    name: Optional[str] = None
    device_id: Optional[str] = None
    icon: Optional[str] = None
    device_class: Optional[str] = None
    unit_of_measurement: Optional[str] = None
    state_class: Optional[str] = None
    entity_category: Optional[str] = None
    #: Faste attributter, f.eks. ``supported_features`` eller ``options``.
    capabilities: Optional[dict] = None

    def to_json(self) -> dict:
        return _compact(asdict(self))


# ---------------------------------------------------------------------------
# integrationen


class Integration:
    """Det, en integration implementerer. Kun :meth:`setup_entry` er påkrævet."""

    async def config_flow(self, host: "Host", flow: Flow, step: str, user_input: Optional[dict]) -> FlowReply:
        """Opsætningsforløbet. Første kald har ``step`` = kilden (``user`` ...) og
        ``user_input`` = ``None``. De næste har den sidste formulars ``step_id``."""
        return FlowReply.abort("not_supported")

    async def options_flow(self, host: "Host", flow: Flow, step: str, user_input: Optional[dict]) -> FlowReply:
        """Indstillinger for en eksisterende opsætning (``flow.entry`` er sat)."""
        return FlowReply.abort("not_supported")

    async def setup_entry(self, host: "Host", entry: Entry) -> None:
        """Sæt en opsætning op: registrér enheder og entities, start arbejde."""
        raise NotImplementedError

    async def unload_entry(self, host: "Host", entry_id: str) -> None:
        """Stop arbejdet for en opsætning. TheHome fjerner selv dens tilstande."""

    async def entity_service(self, host: "Host", entry_id: str, entity_id: str, service: str, data: dict) -> None:
        """Et tjenestekald på en entity tilføjet med ``handler=True``, f.eks. ``switch.turn_on``."""
        raise PluginError.not_supported(f"{service} på {entity_id}")

    async def domain_service(self, host: "Host", entry_id: str, service: str, data: dict, entity_ids: list[str]) -> Any:
        """Et kald af en tjeneste registreret med :meth:`Host.register_service`."""
        raise PluginError.not_supported(service)

    async def diagnostics(self, host: "Host", entry: Entry) -> Any:
        """Fejlsøgningsdata. Skjul hemmeligheder."""
        return None

    async def remove_device(self, host: "Host", entry: Entry, device: dict) -> None:
        """Brugeren sletter en enhed."""


# ---------------------------------------------------------------------------
# forbindelsen til TheHome


class Host:
    """Forbindelsen til TheHome."""

    def __init__(self, send, data_dir: Path):
        self._send = send
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self.data_dir = data_dir

    async def request(self, method: str, params: Any = None) -> Any:
        """Kald en metode i TheHome og vent på svaret."""
        msg_id = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, HOST_TIMEOUT)
        except asyncio.TimeoutError:
            raise PluginError.failed(f"{method}: TheHome svarede ikke") from None
        finally:
            self._pending.pop(msg_id, None)

    def _resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("id"))
        if fut is None or fut.done():
            return
        err = msg.get("error")
        if err:
            kind = (err.get("data") or {}).get("kind") or "failed"
            fut.set_exception(PluginError(kind, err.get("message") or ""))
        else:
            fut.set_result(msg.get("result"))

    def _close(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PluginError.not_ready("forbindelsen til TheHome er lukket"))

    async def register_device(self, entry_id: str, device: Device) -> str:
        """Registrér en enhed. Returnerer dens id."""
        v = await self.request("device/register", {"entry_id": entry_id, "device": device.to_json()})
        if not isinstance(v, dict) or not v.get("id"):
            raise PluginError.failed("intet enheds-id")
        return v["id"]

    async def add_entity(self, entry_id: str, entity: Entity, handler: bool = False) -> Optional[str]:
        """Tilføj en entity. Med ``handler`` sender TheHome dens tjenestekald til
        :meth:`Integration.entity_service`. Returnerer entity_id, eller ``None``
        hvis brugeren har slået den fra."""
        v = await self.request("entity/add", {"entry_id": entry_id, "entity": entity.to_json(), "handler": handler})
        return v.get("entity_id") if isinstance(v, dict) else None

    async def set_state(self, entity_id: str, state: Any, attributes: Optional[dict] = None) -> None:
        await self.request("state/set", {"entity_id": entity_id, "state": str(state), "attributes": attributes or {}})

    async def set_unavailable(self, entity_id: str) -> None:
        await self.request("state/unavailable", {"entity_id": entity_id})

    async def get_state(self, entity_id: str) -> Any:
        """Tilstanden af en vilkårlig entity, eller ``None``."""
        return await self.request("state/get", {"entity_id": entity_id})

    async def register_service(self, entry_id: str, service: str, description: Optional[dict] = None) -> None:
        """Registrér tjenesten ``<domæne>.<service>``. Kald ender i
        :meth:`Integration.domain_service`."""
        await self.request("service/register", {"entry_id": entry_id, "service": service, "description": description})

    async def call_service(self, domain: str, service: str, data: Optional[dict] = None) -> Any:
        """Kald en tjeneste i TheHome, f.eks. ``light.turn_on``."""
        return await self.request("hub/call_service", {"domain": domain, "service": service, "data": data or {}})

    async def fire_event(self, event_type: str, data: Optional[dict] = None) -> None:
        await self.request("event/fire", {"event_type": event_type, "data": data or {}})

    async def create_issue(self, entry_id: str, issue_id: str, severity: str, title: str, description: str) -> None:
        """Bed brugeren rette noget. ``severity``: ``warning`` eller ``error``."""
        await self.request(
            "issue/create",
            {"entry_id": entry_id, "issue_id": issue_id, "severity": severity, "title": title, "description": description},
        )

    async def delete_issue(self, entry_id: str, issue_id: str) -> None:
        await self.request("issue/delete", {"entry_id": entry_id, "issue_id": issue_id})

    async def update_entry_data(self, entry_id: str, data: dict) -> None:
        """Gem ny data (f.eks. et fornyet token) på opsætningen."""
        await self.request("entry/update", {"entry_id": entry_id, "data": data})


def log(level: str, message: str) -> None:
    """Skriv til TheHomes log. ``level``: ``ERROR``, ``WARN``, ``INFO`` eller ``DEBUG``."""
    print(f"{level} {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# løkken


def _utf8(stream: TextIO) -> TextIO:
    """Windows bruger ellers tegnsættet fra konsollen."""
    if isinstance(stream, io.TextIOWrapper):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return stream


def run(integration: Integration) -> None:
    """Kør integrationen, indtil TheHome lukker stdin eller beder om at stoppe."""
    stdin = _utf8(sys.stdin)
    stdout = _utf8(sys.stdout)
    _utf8(sys.stderr)
    try:
        asyncio.run(serve(integration, stdin, stdout))
    except KeyboardInterrupt:
        pass


async def serve(integration: Integration, stdin: TextIO, stdout: TextIO) -> None:
    """Læs beskeder fra ``stdin`` og svar på ``stdout``. Til test kan begge
    være vilkårlige tekststrømme."""
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue = asyncio.Queue()

    # En tråd læser stdin; asyncio kan ikke læse rør på alle platforme.
    def reader() -> None:
        try:
            while True:
                line = stdin.readline()
                if not line:
                    break
                loop.call_soon_threadsafe(lines.put_nowait, line)
        except (OSError, ValueError, RuntimeError):
            pass
        try:
            loop.call_soon_threadsafe(lines.put_nowait, None)
        except RuntimeError:
            # løkken er allerede stoppet
            pass

    threading.Thread(target=reader, name="thehome-stdin", daemon=True).start()

    write_lock = threading.Lock()

    def send(msg: dict) -> None:
        text = json.dumps(msg, separators=(",", ":"))
        with write_lock:
            try:
                stdout.write(text + "\n")
                stdout.flush()
            except (OSError, ValueError):
                pass

    host = Host(send, Path(os.environ.get("THEHOME_DATA_DIR") or "data"))
    tasks: set[asyncio.Task] = set()

    while True:
        line = await lines.get()
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("WARN", f"ugyldig besked fra TheHome: {line}")
            continue
        method = msg.get("method")
        if method is None:
            host._resolve(msg)
            continue
        if method == "shutdown":
            break
        task = asyncio.create_task(_answer(integration, host, send, msg))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    host._close()
    for t in list(tasks):
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _answer(integration: Integration, host: Host, send, msg: dict) -> None:
    msg_id = msg.get("id")
    try:
        result = await _handle(integration, host, msg["method"], msg.get("params") or {})
        reply = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except PluginError as e:
        reply = {"jsonrpc": "2.0", "id": msg_id, "error": e.to_wire()}
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - alle fejl skal tilbage til TheHome
        log("ERROR", f"{msg['method']}: {e!r}")
        reply = {"jsonrpc": "2.0", "id": msg_id, "error": PluginError.failed(str(e) or type(e).__name__).to_wire()}
    if msg_id is not None:
        send(reply)


async def _handle(i: Integration, host: Host, method: str, p: dict) -> Any:
    if method == "initialize":
        if p.get("data_dir"):
            host.data_dir = Path(p["data_dir"])
        return {"api_version": API_VERSION}
    if method in ("config_flow", "options_flow"):
        flow = Flow.from_json(p.get("flow"))
        step = p.get("step") or ""
        user_input = p.get("input")
        fn = i.config_flow if method == "config_flow" else i.options_flow
        reply = await fn(host, flow, step, user_input)
        return reply.to_json()
    if method == "setup_entry":
        await i.setup_entry(host, Entry.from_json(p.get("entry")))
        return None
    if method == "unload_entry":
        await i.unload_entry(host, p.get("entry_id") or "")
        return None
    if method == "entity/call_service":
        await i.entity_service(host, p.get("entry_id") or "", p.get("entity_id") or "", p.get("service") or "", p.get("data") or {})
        return None
    if method == "service/call":
        return await i.domain_service(host, p.get("entry_id") or "", p.get("service") or "", p.get("data") or {}, p.get("entity_ids") or [])
    if method == "diagnostics":
        return await i.diagnostics(host, Entry.from_json(p.get("entry")))
    if method == "remove_device":
        await i.remove_device(host, Entry.from_json(p.get("entry")), p.get("device") or {})
        return None
    raise PluginError.not_supported(f"ukendt metode {method}")
