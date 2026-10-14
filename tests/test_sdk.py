"""Tests af SDK'en: testen spiller TheHome over et rør."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path

from example_counter_python import Example
from thehome_plugin import Entity, Entry, Flow, Integration, PluginError, serve


class LineSink:
    """stdout, der lægger hver hele linje i en kø."""

    def __init__(self) -> None:
        self.lines: queue.Queue = queue.Queue()
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.put(json.loads(line))
        return len(text)

    def flush(self) -> None:
        pass


class Harness:
    """Kører ``serve`` i en tråd og lader testen være TheHome."""

    def __init__(self, integration: Integration) -> None:
        r, w = os.pipe()
        self.stdin = os.fdopen(r, "r", encoding="utf-8")
        self.writer = os.fdopen(w, "w", encoding="utf-8")
        self.out = LineSink()
        self.thread = threading.Thread(target=lambda: asyncio.run(serve(integration, self.stdin, self.out)), daemon=True)
        self.thread.start()

    def send(self, msg: dict) -> None:
        self.writer.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.writer.flush()

    def recv(self) -> dict:
        return self.out.lines.get(timeout=5)

    def close(self) -> None:
        self.writer.close()
        self.thread.join(timeout=5)
        # læsetråden har set EOF og er færdig med røret
        threading.Event().wait(0.05)
        self.stdin.close()


class Lamp(Integration):
    async def setup_entry(self, host, entry):
        if entry.get("fail"):
            raise PluginError.auth_failed("forkert adgangskode")
        entity_id = await host.add_entity(entry.entry_id, Entity("switch", "lamp-1", name="Lampe"), handler=True)
        await host.set_state(entity_id, "off", {"kilde": "test"})


class ProtocolTest(unittest.TestCase):
    def test_initialize_setup_and_errors(self) -> None:
        h = Harness(Lamp())
        try:
            with tempfile.TemporaryDirectory() as d:
                h.send({"id": 1, "method": "initialize", "params": {"api_version": 1, "data_dir": d}})
                self.assertEqual(h.recv(), {"jsonrpc": "2.0", "id": 1, "result": {"api_version": 1}})

            # setup_entry kalder tilbage til TheHome, før den svarer
            h.send({"id": 2, "method": "setup_entry", "params": {"entry": {"entry_id": "e1", "data": {}}}})
            add = h.recv()
            self.assertEqual(add["method"], "entity/add")
            self.assertEqual(add["params"]["entity"], {"domain": "switch", "unique_id": "lamp-1", "name": "Lampe"})
            self.assertTrue(add["params"]["handler"])
            h.send({"id": add["id"], "result": {"entity_id": "switch.lampe"}})
            st = h.recv()
            self.assertEqual(st["method"], "state/set")
            self.assertEqual(st["params"], {"entity_id": "switch.lampe", "state": "off", "attributes": {"kilde": "test"}})
            h.send({"id": st["id"], "result": None})
            self.assertEqual(h.recv(), {"jsonrpc": "2.0", "id": 2, "result": None})

            # fejl har en kind
            h.send({"id": 3, "method": "setup_entry", "params": {"entry": {"entry_id": "e2", "data": {"fail": True}}}})
            err = h.recv()["error"]
            self.assertEqual(err["data"]["kind"], "auth_failed")
            self.assertEqual(err["message"], "forkert adgangskode")

            h.send({"id": 4, "method": "entity/call_service", "params": {"entry_id": "e1", "entity_id": "switch.lampe", "service": "turn_on"}})
            self.assertEqual(h.recv()["error"]["data"]["kind"], "not_supported")
            h.send({"id": 5, "method": "nope", "params": {}})
            self.assertEqual(h.recv()["error"]["data"]["kind"], "not_supported")
        finally:
            h.close()
        # stdin lukket: serve stopper
        self.assertFalse(h.thread.is_alive())

    def test_shutdown_stops(self) -> None:
        h = Harness(Lamp())
        h.send({"method": "shutdown"})
        h.thread.join(timeout=5)
        self.assertFalse(h.thread.is_alive())
        h.close()


class ExampleFlowTest(unittest.TestCase):
    def test_config_flow(self) -> None:
        ex = Example()

        async def go():
            first = await ex.config_flow(None, Flow(source="user"), "user", None)
            self.assertEqual(first.result["type"], "form")
            empty = await ex.config_flow(None, Flow(source="user"), "user", {"name": " ", "interval": 5})
            self.assertEqual(empty.result["errors"], {"name": "required"})
            done = await ex.config_flow(None, Flow(source="user"), "user", {"name": "Køkken", "interval": 5})
            self.assertEqual(done.to_json(), {
                "result": {"type": "create_entry", "title": "Køkken", "data": {"name": "Køkken", "interval": 5}},
                "unique_id": "køkken",
            })
            opts = await ex.options_flow(None, Flow(source="options", entry=Entry("e1", data={"interval": 7})), "options", None)
            self.assertEqual(opts.result["schema"]["properties"]["interval"]["default"], 7)

        asyncio.run(go())

    def test_count_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            host = type("H", (), {"data_dir": Path(d)})()
            Example.save(host, "e1", 41)
            self.assertEqual(Example.load(host, "e1"), 41)
            self.assertEqual(Example.load(host, "missing"), 0)


if __name__ == "__main__":
    unittest.main()
