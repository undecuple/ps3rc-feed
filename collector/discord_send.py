"""Sending planned Discord messages (collector/discord.py) to one webhook.

The only code besides github.py that opens a connection. It does one thing:
POST a JSON payload to https://discord.com/api/webhooks/<id>/<token> over
verified TLS (github.tls_context), one message per request.

The webhook address is a secret. It is read from the environment by the
caller, checked here against one exact form (https, discord.com, a numeric
id and a token of URL-safe characters, nothing else: no port, user, query,
fragment or extra path), and used only to build the request path. It is
never printed, logged, stored or put in an exception: every refusal and
failure message is fixed wording that names no part of it, and a response
body is never read into a message.

Behaviour: no redirects (a 3xx is a failure), no proxy, no cookies, no
Authorization header, a socket timeout of TIMEOUT seconds on each phase, a response body
read up to a small cap and only to find retry_after. A 2xx is delivered. A
429 waits retry_after (or Retry-After) seconds when that is at most
MAX_WAIT, once per message, and otherwise stops the run. A 401, 403 or 404 means the webhook
was rejected (removed, wrong token): the run stops and no message is counted
as failed. Any other 4xx is a failed send that counts against the message; a 5xx or a
network error counts nothing. Each of these stops the run, so a broken
endpoint is hit once, not once per message. Delivery is at least once: a
timeout after the request went out is reported as an error and the message is
sent again by the next run, so a message can very rarely appear twice. Messages are paced at least
PACE seconds apart. deliver returns one result per planned message: True
(delivered), False (the send failed) or None (not attempted, or not the
message's fault; see deliver); discord.apply_delivery takes exactly that list.
"""

import http.client
import json
import re
import time

from . import github

HOST = "discord.com"
WEBHOOK_RE = re.compile(r"https://discord\.com/api/webhooks/([0-9]{17,20})/([A-Za-z0-9_-]{60,80})\Z")
TIMEOUT = 20
MAX_WAIT = 30
MAX_BODY = 4096
PACE = 1.0
# The webhook itself is refused (wrong token, removed, not allowed): nothing is wrong with the message.
REJECTED = (401, 403, 404)

# Tests replace this with a fake connection class; None means http.client.HTTPSConnection.
connection_factory = None


class Refused(ValueError):
    """The webhook address is not in the accepted form. Fixed wording, never the value."""


def webhook_path(value):
    """The request path for a webhook address, or Refused."""
    match = WEBHOOK_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise Refused("DISCORD_WEBHOOK_URL is not a https://discord.com/api/webhooks/<id>/<token> address")
    return "/api/webhooks/%s/%s" % match.groups()


def _retry_after(response, body):
    """Seconds to wait from a 429, or None when unusable."""
    candidates = []
    try:
        candidates.append(json.loads(body.decode("utf-8")).get("retry_after"))
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    candidates.append(response.getheader("Retry-After"))
    for value in candidates:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= seconds < 3600:
            return seconds
    return None


class WebhookSender:
    """Posts payloads to one webhook; send returns "ok", "failed", "error", "rejected" or "limited"."""

    def __init__(self, path, sleep=None):
        self._path = path
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))

    def _post(self, payload):
        factory = connection_factory or http.client.HTTPSConnection
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        connection = None
        try:
            connection = factory(HOST, 443, timeout=TIMEOUT, context=github.tls_context())
            connection.request("POST", self._path, body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body)), "User-Agent": "ps3rc-feed"})
            response = connection.getresponse()
            status = int(response.status)
            data = response.read(MAX_BODY + 1)[:MAX_BODY] if status == 429 else b""
            return status, response, data
        except Exception:
            return None, None, b""
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def send(self, payload):
        for attempt in range(2):
            status, response, data = self._post(payload)
            if status is None:
                return "error"
            if 200 <= status < 300:
                return "ok"
            if status == 429:
                wait = _retry_after(response, data)
                if attempt == 0 and wait is not None and wait <= MAX_WAIT:
                    self._sleep(wait)
                    continue
                return "limited"
            if status in REJECTED:
                return "rejected"
            if status >= 500 or status < 400:
                return "error"
            return "failed"
        return "limited"


def deliver(plan, sender, sleep=None, progress=None):
    """Send the plan's messages in order; returns (results, outcomes).

    progress, when given, is called as progress(results) after every message
    with the results so far (the caller saves its state there, so a crash
    never loses a send that was already made).

    results: one True, False or None per message for discord.apply_delivery.
    True is delivered. False is a send the endpoint refused for the message
    itself (a 4xx other than 401, 403, 404 and 429: "failed"), which counts
    against the message's events. None is "not attempted or not the
    message's fault": a rejected webhook (401, 403, 404), an exhausted rate
    limit, a 5xx or a network error ("error": the endpoint or the path to it
    is down), or a message after the run stopped; it counts nothing, so an
    outage or a removed webhook does not use up the retries of real events.
    outcomes: one of "ok", "failed", "error", "rejected", "limited" or "not sent" per message.
    The first message that is not delivered ends the run; the rest are not attempted.
    """
    sleep = sleep or (lambda seconds: time.sleep(seconds))
    results, outcomes = [], []
    stopped = False
    for index, message in enumerate(plan["messages"]):
        if stopped:
            results.append(None)
            outcomes.append("not sent")
            continue
        if index:
            sleep(PACE)
        outcome = sender.send(message["payload"])
        outcomes.append(outcome)
        results.append(True if outcome == "ok" else False if outcome == "failed" else None)
        if outcome != "ok":
            stopped = True
        if progress is not None:
            progress(list(results))
    return results, outcomes
