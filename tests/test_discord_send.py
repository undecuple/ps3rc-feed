"""Sending Discord messages (collector/discord_send.py and the discord --send command), against a fake connection.

No test opens a socket (the helpers block it). The webhook address used
here is made up: it has the accepted form but belongs to nothing.
"""

import ast
import io
import json
import os
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

from helpers import CollectorTestCase

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ID = "123456789012345678"
TOKEN = "T" * 68
URL = "https://discord.com/api/webhooks/%s/%s" % (ID, TOKEN)
PATH = "/api/webhooks/%s/%s" % (ID, TOKEN)


class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def getheader(self, name, default=None):
        return self._headers.get(name, default)


class FakeNetwork:
    """A connection class that serves scripted responses (an exception instance is raised) and records requests."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.connections = []

    def __call__(self, host, port, timeout=None, context=None):
        self.connections.append((host, port, timeout, context))
        net = self

        class Conn:
            def request(self, method, path, body=None, headers=None):
                net.requests.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})

            def getresponse(self):
                item = net.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

            def close(self):
                pass

        return Conn()


def message(i, ids=None):
    return {"kind": "repository_discovered", "event_ids": ids or ["e%d" % i], "payload": {"content": "m%d" % i, "allowed_mentions": {"parse": []}}}


def make_plan(n):
    from collector import discord

    return {"baseline": False, "messages": [message(i) for i in range(1, n + 1)], "state": discord.empty_state()}


class WebhookAddressTests(CollectorTestCase):
    def test_only_the_exact_form_is_accepted(self):
        from collector import discord_send as ds

        self.assertEqual(ds.webhook_path(URL), PATH)
        bad = [None, "", 5, URL + "/", URL + "?wait=true", URL + "#x", URL + "\n", " " + URL, URL.replace("https", "http"),
               URL.replace("discord.com", "discord.com.evil.example"), URL.replace("discord.com", "discordapp.com"),
               URL.replace("https://", "https://user:pw@"), URL.replace("discord.com", "discord.com:444"),
               "https://discord.com/api/webhooks/12/" + TOKEN, "https://discord.com/api/webhooks/%s/short" % ID,
               "https://discord.com/api/v10/webhooks/%s/%s" % (ID, TOKEN), "https://discord.com/api/webhooks/%s/%s/github" % (ID, TOKEN)]
        for value in bad:
            with self.subTest(value=repr(value)[:30]), self.assertRaises(ds.Refused) as ctx:
                ds.webhook_path(value)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertNotIn(ID, str(ctx.exception))


class SenderTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        from collector import discord_send

        self.ds = discord_send
        self.sleeps = []
        self.sender = discord_send.WebhookSender(PATH, sleep=self.sleeps.append)

    def use(self, *script):
        net = FakeNetwork(*script)
        patch = unittest.mock.patch.object(self.ds, "connection_factory", net)
        patch.start()
        self.addCleanup(patch.stop)
        return net

    def test_a_2xx_is_delivered_and_the_request_is_a_plain_post(self):
        net = self.use(FakeResponse(204))
        payload = {"content": "héllo", "allowed_mentions": {"parse": []}}
        self.assertEqual(self.sender.send(payload), "ok")
        req = net.requests[0]
        self.assertEqual((req["method"], req["path"]), ("POST", PATH))
        self.assertEqual(json.loads(req["body"].decode("ascii")), payload)
        self.assertEqual(sorted(req["headers"]), ["Content-Length", "Content-Type", "User-Agent"])
        self.assertEqual(net.connections[0][:2], ("discord.com", 443))
        self.assertEqual(net.connections[0][2], self.ds.TIMEOUT)
        self.assertEqual(net.connections[0][3].verify_mode.name, "CERT_REQUIRED")

    def test_a_short_rate_limit_waits_once_then_retries(self):
        net = self.use(FakeResponse(429, b'{"retry_after": 1.5}'), FakeResponse(204))
        self.assertEqual(self.sender.send({}), "ok")
        self.assertEqual(self.sleeps, [1.5])
        self.assertEqual(len(net.requests), 2)

    def test_retry_after_header_is_used_when_the_body_has_none(self):
        self.use(FakeResponse(429, b"not json", {"Retry-After": "2"}), FakeResponse(204))
        self.assertEqual(self.sender.send({}), "ok")
        self.assertEqual(self.sleeps, [2.0])

    def test_a_long_or_repeated_or_unusable_rate_limit_stops(self):
        self.use(FakeResponse(429, b'{"retry_after": 31}'))
        self.assertEqual(self.sender.send({}), "limited")
        self.use(FakeResponse(429, b'{"retry_after": 1}'), FakeResponse(429, b'{"retry_after": 1}'))
        self.assertEqual(self.sender.send({}), "limited")
        self.use(FakeResponse(429, b"{}"))
        self.assertEqual(self.sender.send({}), "limited")
        self.use(FakeResponse(429, b'{"retry_after": -1}'))
        self.assertEqual(self.sender.send({}), "limited")

    def test_status_classes(self):
        for status, outcome in ((401, "rejected"), (403, "rejected"), (404, "rejected"), (400, "failed"), (413, "failed"),
                                (500, "error"), (503, "error"), (301, "error"), (302, "error")):
            with self.subTest(status=status):
                net = self.use(FakeResponse(status))
                self.assertEqual(self.sender.send({}), outcome)
                self.assertEqual(len(net.requests), 1)

    def test_network_errors_are_errors_and_the_address_stays_out_of_the_result(self):
        for exc in (OSError("boom " + URL), TimeoutError(), ValueError(TOKEN)):
            with self.subTest(exc=type(exc).__name__):
                self.use(exc)
                outcome = self.sender.send({})
                self.assertEqual(outcome, "error")
                self.assertNotIn(TOKEN, outcome)

    def test_a_huge_429_body_is_read_only_up_to_the_cap(self):
        asked = []

        class Spy(FakeResponse):
            def read(self, n=-1):
                asked.append(n)
                return super().read(n)

        net = self.use(Spy(429, b"x" * (self.ds.MAX_BODY * 3)))
        self.assertEqual(self.sender.send({}), "limited")
        self.assertEqual(len(net.requests), 1)
        self.assertEqual(asked, [self.ds.MAX_BODY + 1])


class DeliverTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        from collector import discord_send

        self.ds = discord_send

    def run_deliver(self, n, *script):
        net = FakeNetwork(*script)
        sleeps = []
        with unittest.mock.patch.object(self.ds, "connection_factory", net):
            out = self.ds.deliver(make_plan(n), self.ds.WebhookSender(PATH, sleep=sleeps.append), sleep=sleeps.append)
        return out, net, sleeps

    def test_all_delivered_with_pacing_between_messages(self):
        (results, outcomes), net, sleeps = self.run_deliver(3, FakeResponse(204), FakeResponse(204), FakeResponse(204))
        self.assertEqual(results, [True, True, True])
        self.assertEqual(outcomes, ["ok"] * 3)
        self.assertEqual(sleeps, [self.ds.PACE, self.ds.PACE])

    def test_a_refused_message_stops_the_run_and_counts_only_that_message(self):
        (results, outcomes), net, _ = self.run_deliver(3, FakeResponse(204), FakeResponse(400), FakeResponse(204))
        self.assertEqual(results, [True, False, None])
        self.assertEqual(outcomes, ["ok", "failed", "not sent"])
        self.assertEqual(len(net.requests), 2)

    def test_an_outage_stops_the_run_and_counts_nothing(self):
        (results, outcomes), net, _ = self.run_deliver(3, FakeResponse(204), FakeResponse(503))
        self.assertEqual((results, outcomes), ([True, None, None], ["ok", "error", "not sent"]))
        (results, outcomes), net, _ = self.run_deliver(2, OSError("down"))
        self.assertEqual((results, outcomes), ([None, None], ["error", "not sent"]))

    def test_progress_is_reported_after_every_message(self):
        seen = []
        net = FakeNetwork(FakeResponse(204), FakeResponse(204), FakeResponse(500))
        with unittest.mock.patch.object(self.ds, "connection_factory", net):
            self.ds.deliver(make_plan(3), self.ds.WebhookSender(PATH, sleep=lambda s: None), sleep=lambda s: None, progress=seen.append)
        self.assertEqual(seen, [[True], [True, True], [True, True, None]])

    def test_a_rejected_webhook_or_a_rate_limit_counts_nothing(self):
        (results, _), net, _ = self.run_deliver(2, FakeResponse(401))
        self.assertEqual(results, [None, None])
        self.assertEqual(len(net.requests), 1)
        (results, outcomes), _, _ = self.run_deliver(2, FakeResponse(429, b'{"retry_after": 99}'))
        self.assertEqual((results, outcomes), ([None, None], ["limited", "not sent"]))

    def test_delivery_results_feed_the_state(self):
        from collector import discord

        plan = make_plan(3)
        state = discord.apply_delivery(plan, [True, False, None])
        self.assertEqual(state["seen"], ["e1"])
        self.assertEqual(state["attempts"], {"e2": 1})


class CliSendTests(CollectorTestCase):
    def setUp(self):
        super().setUp()
        from collector import cli, discord_send

        self.cli, self.ds = cli, discord_send
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data)
        events = [{"id": "repo-%d:repository_discovered:%d" % (i, i), "type": "repository_discovered", "repo_id": "r%d" % i,
                   "summary": "s", "observed_at": "2026-11-11T10:0%d:00Z" % i} for i in range(1, 4)]
        repos = [{"id": "r%d" % i, "repo": "o%d/r%d" % (i, i), "url": "https://github.com/o%d/r%d" % (i, i), "fork": False,
                  "game": "G%d" % i, "game_confidence": "strong"} for i in range(1, 4)]
        self.write("feed.json", {"generated_at": "2026-11-11T11:00:00Z", "repos": repos, "events": events})
        self.events = events

    def write(self, name, doc):
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def read(self, name):
        with open(os.path.join(self.data, name), encoding="utf-8") as fh:
            return json.load(fh)

    def run_cli(self, argv, env=None, net=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"DISCORD_WEBHOOK_URL": URL} if env is None else env
        patches = [
            unittest.mock.patch.object(self.cli, "REPO_ROOT", self.tmp.name),
            unittest.mock.patch.object(self.cli, "DATA_ROOT", self.data),
            unittest.mock.patch.dict(os.environ, env, clear=False),
            unittest.mock.patch.object(self.ds, "connection_factory", net),
            unittest.mock.patch.object(self.ds.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = self.cli.main(argv)
        finally:
            for p in reversed(patches):
                p.stop()
        return code, out.getvalue(), err.getvalue()

    def test_without_send_nothing_is_written_or_sent(self):
        net = FakeNetwork()
        code, out, _ = self.run_cli(["discord", "--feed", "data/feed.json"], net=net)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["baseline"])
        self.assertEqual(net.requests, [])
        self.assertEqual(os.listdir(self.data), ["feed.json"])

    def test_send_needs_a_state_output_and_the_output_needs_send(self):
        self.assertEqual(self.run_cli(["discord", "--feed", "data/feed.json", "--send"])[0], 2)
        self.assertEqual(self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state-out", "data/s.json"])[0], 2)

    def test_the_first_send_is_a_baseline_that_posts_nothing_and_needs_no_webhook(self):
        net = FakeNetwork()
        code, out, _ = self.run_cli(["discord", "--feed", "data/feed.json", "--send", "--delivery-state-out", "data/s.json"], env={"DISCORD_WEBHOOK_URL": ""}, net=net)
        self.assertEqual(code, 0)
        self.assertEqual(net.requests, [])
        state = self.read("s.json")
        self.assertEqual(len(state["seen"]), 3)
        self.assertEqual(json.loads(out)["messages"], [])

    def test_a_send_posts_the_new_events_and_saves_the_state(self):
        self.write("s_in.json", {"format_version": 1, "seen": [self.events[0]["id"]], "attempts": {}})
        net = FakeNetwork(FakeResponse(204), FakeResponse(204))
        code, out, err = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/s_out.json"], net=net)
        self.assertEqual(code, 0)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual([m["result"] for m in json.loads(out)["messages"]], ["ok", "ok"])
        self.assertEqual(sorted(self.read("s_out.json")["seen"]), sorted(e["id"] for e in self.events))
        for text in (out, err):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(ID, text)
        # a rerun with the new state sends nothing
        net2 = FakeNetwork()
        code, _, _ = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_out.json", "--send", "--delivery-state-out", "data/s_out.json"], net=net2)
        self.assertEqual((code, net2.requests), (0, []))

    def test_a_failed_send_exits_3_and_still_saves_the_failure_count(self):
        self.write("s_in.json", {"format_version": 1, "seen": [], "attempts": {}})
        net = FakeNetwork(FakeResponse(400))
        code, out, _ = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/s_out.json"], net=net)
        self.assertEqual(code, 3)
        state = self.read("s_out.json")
        self.assertEqual(state["seen"], [])
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual([m["result"] for m in json.loads(out)["messages"]], ["failed", "not sent", "not sent"])

    def test_a_bad_webhook_is_refused_before_anything_is_sent_or_written(self):
        self.write("s_in.json", {"format_version": 1, "seen": [], "attempts": {}})
        net = FakeNetwork()
        for env in ({"DISCORD_WEBHOOK_URL": ""}, {"DISCORD_WEBHOOK_URL": "https://example.com/" + TOKEN}):
            code, out, err = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/s_out.json"], env=env, net=net)
            self.assertEqual(code, 2)
            self.assertNotIn(TOKEN, out + err)
        self.assertEqual(net.requests, [])
        self.assertNotIn("s_out.json", os.listdir(self.data))


    def test_an_outage_exits_3_and_counts_nothing(self):
        self.write("s_in.json", {"format_version": 1, "seen": [], "attempts": {}})
        code, out, err = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/s_out.json"], net=FakeNetwork(OSError(URL)))
        self.assertEqual(code, 3)
        self.assertEqual(self.read("s_out.json")["attempts"], {})
        self.assertEqual([m["result"] for m in json.loads(out)["messages"]], ["error", "not sent", "not sent"])
        self.assertNotIn(TOKEN, out + err)

    def test_the_state_is_saved_after_every_message_so_a_crash_keeps_earlier_sends(self):
        self.write("s_in.json", {"format_version": 1, "seen": [], "attempts": {}})
        real = self.cli._write_bytes
        calls = []

        def flaky(data, path, overwrite=False, flag="--out"):
            calls.append(json.loads(data))
            if len(calls) == 3:  # before the send, after message 1, then the write after message 2 fails
                raise OSError("disk full " + URL)
            real(data, path, overwrite=overwrite, flag=flag)

        net = FakeNetwork(FakeResponse(204), FakeResponse(204), FakeResponse(204))
        with unittest.mock.patch.object(self.cli, "_write_bytes", flaky):
            code, out, err = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/s_out.json"], net=net)
        self.assertEqual(code, 1)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual(self.read("s_out.json")["seen"], [self.events[0]["id"]])
        self.assertNotIn(TOKEN, out + err)
        self.assertIn("OSError", err)

    def test_an_unwritable_state_output_fails_before_anything_is_sent(self):
        self.write("s_in.json", {"format_version": 1, "seen": [], "attempts": {}})
        net = FakeNetwork(FakeResponse(204))
        with unittest.mock.patch.object(self.cli, "_write_bytes", side_effect=OSError("disk full")):
            code, _, _ = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/s_out.json"], net=net)
        self.assertEqual((code, net.requests), (1, []))

    def test_the_output_may_not_be_the_feed(self):
        self.write("s_in.json", {"format_version": 1, "seen": [], "attempts": {}})
        with open(os.path.join(self.data, "feed.json"), "rb") as fh:
            before = fh.read()
        code, _, err = self.run_cli(["discord", "--feed", "data/feed.json", "--delivery-state", "data/s_in.json", "--send", "--delivery-state-out", "data/feed.json"], net=FakeNetwork())
        self.assertEqual(code, 2)
        self.assertIn("same file", err)
        with open(os.path.join(self.data, "feed.json"), "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_baseline_will_not_overwrite_an_existing_state(self):
        self.write("s_out.json", {"format_version": 1, "seen": ["keep"], "attempts": {}})
        code, _, err = self.run_cli(["discord", "--feed", "data/feed.json", "--send", "--delivery-state-out", "data/s_out.json"], net=FakeNetwork())
        self.assertEqual(code, 2)
        self.assertEqual(self.read("s_out.json")["seen"], ["keep"])


class ImportBoundaryTests(CollectorTestCase):
    def test_discord_send_imports_only_what_it_needs(self):
        with open(os.path.join(REPO_ROOT, "collector", "discord_send.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add("relative" if node.level else node.module)
        self.assertEqual(names, {"http.client", "json", "re", "time", "relative"})

    def test_only_github_and_discord_send_import_network_modules(self):
        forbidden = {"http", "http.client", "urllib", "urllib.request", "socket", "ssl"}
        allowed = {"github.py", "discord_send.py"}
        root = os.path.join(REPO_ROOT, "collector")
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py") or name in allowed:
                continue
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=name)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    imported.add(node.module)
            self.assertEqual(imported & forbidden, set(), name)


if __name__ == "__main__":
    unittest.main()
