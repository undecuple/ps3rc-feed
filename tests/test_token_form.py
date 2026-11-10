"""The token form `Secret` accepts, and that a long installation token works end to end.

Every token here is a fictional string. No network: tests/helpers.py blocks
sockets at import, and the CLI runs below use a RecordedTransport in place
of the live one.
"""

import json
import os
import tempfile
import unittest.mock

from helpers import CollectorTestCase
from test_github import RUN_AT, _run_cli, config_for_network, make_client, ok, api_repo, routes_for_network

FRAME = "frame-org/ps3recomp-example"

# A fictional installation token shaped like GitHub's current ones: ghs_ and three dotted parts, 520 characters.
_HEAD = "eyJhbGciOiJSUzI1NiJ9FAKE"
_SIG = "FAKEsig-" + "Zq9_" * 30
_BODY = "eyJGAKEpayload_" + "Xk7-" * 200
# A distinct marker that appears in no message the collector writes.
MARKER = "QxFAKEtokenMarker9"


def _long_token():
    head = "ghs_" + _HEAD + "."
    sig = "." + _SIG
    middle = (_BODY * 3)[: 520 - len(head) - len(sig)]
    return head + middle + sig


class TokenForm(CollectorTestCase):
    def test_the_long_token_fixture_has_the_installation_shape(self):
        token = _long_token()
        self.assertEqual(len(token), 520)
        self.assertEqual(token.count("."), 2)
        self.assertTrue(token.startswith("ghs_"))

    def test_classic_fine_grained_installation_and_long_tokens_are_accepted(self):
        from collector import github

        for good in (
            "ghp_" + "A1b2C3d4" * 4 + "e5f6",
            "github_pat_11FAKE0000_" + "aB3-" * 20,
            "ghs_" + "Q" * 36,
            "gho_" + "z" * 36,
            _long_token(),
            "x",
            "a" * 4096,
            "a." * 2048,
            "-._",
        ):
            secret = github.Secret(good)
            self.assertEqual(secret.reveal(), good, len(good))
            self.assertNotIn(good, repr(secret) + str(secret))

    def test_4097_characters_and_empty_are_refused_with_only_the_length(self):
        from collector import github

        for bad, length in (("a" * 4097, 4097), ("a.b-" * 1500, 6000), ("", 0)):
            with self.assertRaises(ValueError) as ctx:
                github.Secret(bad)
            self.assertEqual(
                str(ctx.exception),
                "token has an unexpected form (allowed: 1-4096 characters of letters, digits, '_', '-' and '.'; got %d characters)" % length,
            )

    def test_non_strings_are_refused(self):
        from collector import github

        for bad in (None, b"ghp_" + b"A" * 36, 12345, ["ghp_x"]):
            with self.assertRaises(ValueError) as ctx:
                github.Secret(bad)
            self.assertIn("non-string", str(ctx.exception))
            self.assertNotIn("ghp_", str(ctx.exception))

    def test_each_disallowed_character_is_refused_and_never_echoed(self):
        from collector import github

        disallowed = [chr(c) for c in range(0x00, 0x20)] + ["\x7f"]
        disallowed += list(" !\"#$%&'()*+,/:;<=>?@[\\]^`{|}~")
        disallowed += ["é", " ", " ", "１", "а", "​", "﻿"]
        base = MARKER + "." + _long_token()[:100]
        for ch in disallowed:
            for bad in (base + ch, ch + base, base[:50] + ch + base[50:]):
                with self.assertRaises(ValueError, msg=repr(ch)) as ctx:
                    github.Secret(bad)
                message = str(ctx.exception) + repr(ctx.exception)
                self.assertEqual(
                    str(ctx.exception),
                    "token has an unexpected form (allowed: 1-4096 characters of letters, digits, '_', '-' and '.'; got %d characters)" % len(bad),
                    repr(ch),
                )
                self.assertNotIn(MARKER, message)
                self.assertNotIn(base[50:80], message)

    def test_the_header_carries_the_long_token_unchanged_and_nothing_else_does(self):
        from collector import github

        token = _long_token()
        routes = {"GET /repositories/90": ok(api_repo(90, FRAME))}
        client, transport = make_client(routes, token=github.Secret(token))
        client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer " + token)
        others = {k: v for k, v in transport.calls[0]["headers"].items() if k != "Authorization"}
        leaks = [repr(client), str(client), repr(client.token), str(client.token), json.dumps(client.log), json.dumps(client.summary()), json.dumps(others), transport.calls[0]["url"]]
        for text in leaks:
            self.assertNotIn(token, text)
            self.assertNotIn(_SIG, text)

    def test_the_live_transport_sends_the_long_token_unchanged(self):
        from collector import github
        from test_github_hardening import FakeConnection, FakeHTTPResponse, fake_live

        token = _long_token()
        body = json.dumps(api_repo(90, FRAME)).encode("utf-8")
        transport = fake_live(FakeHTTPResponse(200, {"content-type": "application/json", "content-length": str(len(body))}, body))
        client = github.Client(transport, token=github.Secret(token), max_requests=3)
        result = client.get_json("/repositories/90", None, expect="object")
        self.assertEqual(result.status, 200)
        sent = FakeConnection.instances[0].requests[0]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer " + token)
        self.assertNotIn(token, sent["url"])

    def test_cli_refuses_a_bad_token_with_only_its_length(self):
        from collector import github

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump(config_for_network(), fh)
            with unittest.mock.patch.object(github, "LiveTransport", side_effect=AssertionError("live transport constructed")):
                for bad in (MARKER + "/x", MARKER + "=", "ghs_" + MARKER + "." * 4097):
                    code, out, err = _run_cli(["collect", "--config", config_path, "--live", "--max-requests", "3", "--out", "data/never.json"], env={"GITHUB_TOKEN": bad})
                    self.assertEqual(code, 2, len(bad))
                    self.assertIn("GITHUB_TOKEN", err)
                    self.assertIn("got %d characters" % len(bad), err)
                    self.assertNotIn(MARKER, out + err)


class LongTokenEndToEnd(CollectorTestCase):
    """A 520-character dotted token through `collect --live` against the recorded transport."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(config_for_network(), fh)
        self.fake_repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.fake_repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_live_with_a_long_installation_token(self):
        from collector import cli, github

        token = _long_token()
        holder = {}

        def factory(*args, **kwargs):
            holder["transport"] = github.RecordedTransport(routes_for_network())
            return holder["transport"]

        data_root = os.path.join(self.fake_repo, "data")
        with unittest.mock.patch.object(cli, "REPO_ROOT", self.fake_repo), unittest.mock.patch.object(cli, "DATA_ROOT", data_root):
            with unittest.mock.patch.object(github, "LiveTransport", factory):
                code, out, err = _run_cli(
                    ["collect", "--config", self.config_path, "--live", "--max-requests", "20", "--run-at", RUN_AT, "--out", "data/long.json"],
                    env={"GITHUB_TOKEN": token},
                )
        self.assertEqual(code, 0, err)
        transport = holder["transport"]
        self.assertTrue(transport.calls)
        for call in transport.calls:
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + token)
            self.assertNotIn(token, call["url"])
        with open(os.path.join(data_root, "long.json"), encoding="utf-8") as fh:
            run_text = fh.read()
        self.assertTrue(json.loads(run_text)["repositories"])
        for text in (out, err, run_text):
            self.assertNotIn(token, text)
            self.assertNotIn(_SIG, text)
            self.assertNotIn(_HEAD, text)
