"""Read-only GitHub API client, safe by construction.

What the client can do: HTTPS GET to https://api.github.com and nothing
else. Every URL is checked before a transport is called: printable ASCII
only, scheme https, authority exactly api.github.com (so no port, no
credentials, no other host), no fragment, an absolute path of plain
segments (no empty, "." or ".." segments, no percent-encoded dot, slash
or backslash). Redirects are errors, never followed. The only URL ever
taken from a response is a `Link: rel="next"` URL, accepted only when it
names the same host, the same path (or, for a /repos/{owner}/{repo}/...
request whose repository id is known, exactly /repositories/{id}/... with
the same suffix, which is how GitHub writes these links) and the same
query apart from `page`, whose value must be the current page plus one,
within a page cap.

Transports are callables `transport(method, url, headers, timeout) ->
Response`. `RecordedTransport` answers from canned responses and is what
the tests and the CLI's dry path use. `LiveTransport` talks http.client
directly (no proxy, no redirect handler, no netrc, no cookie jar) under a
wall-clock deadline enforced by a watchdog timer, and is constructed only
by the CLI's --live path. Every exception a transport raises is mapped to
`TransportFailed`, whose message names the exception class and nothing
else.

Budget: a hard cap on requests per client; page caps per listing; three
consecutive transport failures stop the client (`NetworkFailed`). Rate
limits are tracked per resource bucket (the x-ratelimit-resource header:
core, search, code_search, ...; an answer without it counts as core): a
parsed
remaining of 0 exhausts that bucket, and a request against an exhausted
bucket raises RateLimited naming it before any transport call. A 403 or
429 carrying retry-after, or a 429 while the bucket still has requests
left, is a secondary limit and stops everything. The client never sleeps and never forgets a
recorded limit within a run. Header values that are not bounded integers
are ignored (unknown); duplicate headers keep the most restrictive value.

Responses: body capped (MAX_BODY), headers capped (MAX_HEADER_BYTES),
strict JSON (no duplicate keys, no NaN/Infinity), shape checked against
what the endpoint promises; errors name the request path and never
include a body or a header.

The token is wrapped in `Secret`: it must be 1-4096 characters of letters,
digits, '_', '-' or '.' (classic ghp_ and github_pat_ tokens fit this, and
so do installation tokens such as the Actions GITHUB_TOKEN, which are
ghs_ followed by a JWT-like text of about 520 characters with '.'
between its parts; GitHub sets no upper length, 4096 is this client's
bound). A refusal states the allowed form and the observed length, never
a character of the value. Its repr and str are redacted, and its text is
written only into the Authorization header handed to the transport.
"""

import http.client
import json
import re
import ssl
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from . import schema

API_HOST = "api.github.com"
API_BASE = "https://" + API_HOST
MAX_BODY = 2 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_LINK_HEADER = 8192
DEFAULT_TIMEOUT = 10
DEFAULT_DEADLINE = 20
READ_CHUNK = 65536
USER_AGENT = "ps3rc-feed-collector (read-only; see repository README)"
API_VERSION = "2022-11-28"
ACCEPT_JSON = "application/vnd.github+json"
ACCEPT_RAW = "application/vnd.github.raw"
DEFAULT_PAGE_CAPS = {"search": 10, "code": 1, "forks": 10, "commits": 5}
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3
MAX_HEADER_INT = 10**12
CORE = "core"
SEARCH = "search"
# GET /search/code draws from its own bucket (about 10 requests a minute, token required).
CODE_SEARCH = "code_search"
CODE_SEARCH_PATH = "/search/code"

MAX_TOKEN_LENGTH = 4096
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]{1,%d}\Z" % MAX_TOKEN_LENGTH)
TOKEN_FORM = "1-%d characters of letters, digits, '_', '-' and '.'" % MAX_TOKEN_LENGTH
_PRINTABLE_RE = re.compile(r"[\x21-\x7e]{1,2048}\Z")
_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*\Z")
_BAD_ESCAPE_RE = re.compile(r"%(2e|2f|5c)", re.IGNORECASE)
_LINK_ENTRY_RE = re.compile(r"<([^<>\s]*)>((?:\s*;\s*[^;]*)*)\Z")
_DIGITS_RE = re.compile(r"[0-9]{1,13}\Z")
_RESOURCE_RE = re.compile(r"[a-z_]{1,32}\Z")


class GitHubError(Exception):
    """Base class; messages never carry response bodies, headers or tokens."""


class TransportRefused(GitHubError):
    """The request would leave the allowlist; raised before any transport call."""


class ResponseError(GitHubError):
    """The response cannot be used: redirect, oversize, bad JSON or wrong shape."""


class LinkError(ResponseError):
    """A Link header is present but not usable."""


class TransportFailed(GitHubError):
    """The transport could not complete the request. Names the exception class only."""

    def __init__(self, exc, path=""):
        GitHubError.__init__(self, "transport failure (%s) for %s" % (type(exc).__name__, path or "request"))
        self.kind = type(exc).__name__


class RateLimited(GitHubError):
    """A rate-limit bucket is exhausted (`resource`: core, search, ...) or a secondary limit was hit."""

    def __init__(self, message, resource=CORE):
        GitHubError.__init__(self, message)
        self.resource = resource


class BudgetExhausted(GitHubError):
    """The configured request budget is used up."""


class NetworkFailed(GitHubError):
    """Several transport failures in a row; the run must stop."""


class Secret:
    """Holds a token. Its text appears nowhere but in `reveal()`."""

    __slots__ = ("_value",)

    def __init__(self, value):
        if not isinstance(value, str):
            raise ValueError("token has an unexpected form (allowed: %s; got a non-string value)" % TOKEN_FORM)
        if _TOKEN_RE.fullmatch(value) is None:
            # Only the length is reported: no character of the value goes into the message.
            raise ValueError("token has an unexpected form (allowed: %s; got %d characters)" % (TOKEN_FORM, len(value)))
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other):
        return NotImplemented

    __hash__ = None


# --- headers ------------------------------------------------------------------


def header_int(value, lo=0, hi=MAX_HEADER_INT):
    """A header value as a bounded non-negative integer, or None when it is anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _DIGITS_RE.fullmatch(text) is None:
        return None
    number = int(text)
    if number < lo or number > hi:
        return None
    return number


def merge_headers(pairs):
    """Lower-cased header dict. Duplicates: Link joined; rate-limit values most restrictive; others first.

    A Content-Length repeated with different values becomes "conflict",
    which the transports treat as a failed response.
    """
    seen = {}
    for key, value in pairs:
        seen.setdefault(str(key).lower(), []).append(str(value))
    merged = {}
    for key, values in seen.items():
        if len(values) == 1:
            merged[key] = values[0]
        elif key == "link":
            merged[key] = ", ".join(values)
        elif key in ("x-ratelimit-remaining", "x-ratelimit-reset"):
            numbers = [n for n in (header_int(v, lo=1 if key.endswith("reset") else 0) for v in values) if n is not None]
            merged[key] = str(min(numbers)) if numbers else values[0]
        elif key == "retry-after":
            numbers = [n for n in (header_int(v) for v in values) if n is not None]
            merged[key] = str(max(numbers)) if numbers else values[0]
        elif key == "content-length":
            merged[key] = values[0] if len(set(values)) == 1 else "conflict"
        else:
            merged[key] = values[0]
    return merged


class Response:
    """What a transport returns. Headers may be a dict or a list of pairs; names are lower-cased."""

    def __init__(self, status, headers, body):
        self.status = int(status)
        pairs = headers.items() if isinstance(headers, dict) else list(headers or [])
        self.headers = merge_headers(pairs)
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")


class Result:
    """A parsed response: HTTP status, parsed data (None unless 200), headers."""

    def __init__(self, status, data, headers):
        self.status = status
        self.data = data
        self.headers = headers


class Pages:
    """Items gathered over pages.

    complete is False when the cap or a stop ended the walk; `stopped` says
    why ("rate_limited", "search_rate_limited", "code_search_rate_limited",
    "budget", "network",
    "transport", "response", "status" or None); `status` is the first
    page's HTTP status (None when the first page produced no usable
    response).
    """

    def __init__(self, items, complete, next_cursor, status, stopped=None, extra=None):
        self.items = items
        self.complete = complete
        self.next_cursor = next_cursor
        self.status = status
        self.stopped = stopped
        self.extra = extra if extra is not None else {}


# --- URL rules --------------------------------------------------------------


def check_url(url):
    """Return (path, query) if `url` is an allowed GET target, else raise TransportRefused."""
    if not isinstance(url, str) or _PRINTABLE_RE.fullmatch(url) is None:
        raise TransportRefused("refused: URL must be printable ASCII without whitespace, at most 2048 characters")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise TransportRefused("refused: only https is allowed")
    # The whole authority must be the bare host: this refuses ports,
    # credentials and any other host in one comparison.
    if parts.netloc != API_HOST:
        raise TransportRefused("refused: only https://%s is allowed" % API_HOST)
    if parts.fragment:
        raise TransportRefused("refused: fragments are not allowed")
    path = parts.path
    if not path.startswith("/") or _PATH_RE.fullmatch(path) is None:
        raise TransportRefused("refused: path is not a plain API path")
    if _BAD_ESCAPE_RE.search(path) is not None:
        raise TransportRefused("refused: percent-encoded dot, slash or backslash in path")
    segments = path.split("/")[1:]
    if any(segment in ("", ".", "..") for segment in segments):
        raise TransportRefused("refused: empty or dot path segment")
    return path, parts.query


def resource_for_path(path):
    """The rate-limit bucket a request path draws from: code_search, search or core."""
    if path == CODE_SEARCH_PATH:
        return CODE_SEARCH
    return SEARCH if path.startswith("/search/") else CORE


def canonical_query(params):
    """Deterministic query string: sorted keys, each value a str or int."""
    if not params:
        return ""
    items = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TransportRefused("refused: query value for %r must be a string or integer" % key)
        items.append((key, str(value)))
    return urlencode(items)


def build_url(path, params=None):
    if not isinstance(path, str) or not path.startswith("/"):
        raise TransportRefused("refused: path must start with /")
    query = canonical_query(params)
    url = API_BASE + path + ("?" + query if query else "")
    check_url(url)
    return url


def parse_link(header):
    """Entries of a Link header as (url, set of lower-cased rel tokens).

    Absent header -> []. A header that is present but has no well-formed
    entry, or is too long, raises LinkError. rel values may be quoted or
    bare and may hold several space-separated tokens.
    """
    if header is None:
        return []
    if not isinstance(header, str) or len(header) > MAX_LINK_HEADER or not header.strip():
        raise LinkError("Link header is not usable")
    parts = []
    depth = 0
    current = []
    for ch in header:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    entries = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        match = _LINK_ENTRY_RE.fullmatch(part)
        if match is None:
            raise LinkError("Link header entry is malformed")
        rels = set()
        for param in match.group(2).split(";"):
            param = param.strip()
            if not param:
                continue
            if "=" not in param:
                raise LinkError("Link header parameter is malformed")
            name, value = param.split("=", 1)
            if name.strip().lower() == "rel":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                rels.update(token.lower() for token in value.split())
        entries.append((match.group(1), rels))
    if not entries:
        raise LinkError("Link header has no entries")
    return entries


def parse_link_next(link_header):
    """The single rel="next" URL of a Link header, or None. More than one is a LinkError."""
    nexts = [url for url, rels in parse_link(link_header) if "next" in rels]
    if len(nexts) > 1:
        raise LinkError("Link header names more than one next page")
    return nexts[0] if nexts else None


def _query_dict(query):
    pairs = parse_qsl(query, keep_blank_values=True)
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportRefused("refused: repeated query parameter")
        result[key] = value
    return result


def equivalent_path(request_path, next_path, repository_id=None):
    """True when next_path is the request path, or its /repositories/{id}/... spelling.

    GitHub writes the next link of /repos/{owner}/{repo}/<suffix> as
    /repositories/{id}/<suffix>. That spelling is accepted only when the
    collector already knows the repository's id and it matches exactly.
    """
    if next_path == request_path:
        return True
    if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id < 1:
        return False
    if not request_path.startswith("/repos/"):
        return False
    parts = request_path.split("/")
    if len(parts) < 4:
        return False
    suffix = "/".join(parts[4:])
    expected = "/repositories/%d" % repository_id + ("/" + suffix if suffix else "")
    return next_path == expected


def check_next_url(next_url, request_path, request_query, expected_page=None, repository_id=None):
    """A next URL must be on the API host, name the same (or equivalent) path and the same query apart from `page`.

    `page` must be digits; when expected_page is given it must equal it
    (the walk never jumps or loops).
    """
    path, query = check_url(next_url)
    if not equivalent_path(request_path, path, repository_id):
        raise TransportRefused("refused: Link next URL names a different path")
    mine = _query_dict(request_query)
    theirs = _query_dict(query)
    mine.pop("page", None)
    page = theirs.pop("page", None)
    if page is None or _DIGITS_RE.fullmatch(page) is None:
        raise TransportRefused("refused: Link next URL has no page number")
    if expected_page is not None and int(page) != expected_page:
        raise TransportRefused("refused: Link next URL does not name the next page")
    if mine != theirs:
        raise TransportRefused("refused: Link next URL changes the query")
    return next_url


# --- strict JSON ------------------------------------------------------------


def _pairs_hook(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key in JSON object")
        obj[key] = value
    return obj


def _reject_constant(name):
    raise ValueError("non-finite number %s" % name)


def parse_json(body, path):
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ResponseError("response for %s is not valid UTF-8" % path) from None
    try:
        return json.loads(text, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ResponseError("response for %s is not strict JSON" % path) from None


# --- transports -------------------------------------------------------------


class RecordedTransport:
    """Canned responses keyed by "GET <path>?<query>". Unknown requests get a 404.

    An entry may be a response dict, a list of them (served in turn), or a
    dict with "raise": "<ExceptionName>" to simulate a transport failure.
    Headers may be a dict or a list of [name, value] pairs.
    """

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []
        self._served = {}

    @classmethod
    def from_file(cls, path):
        data = schema.read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("responses"), dict):
            raise schema.SchemaError("%s: expected an object with a 'responses' object" % path)
        return cls(data["responses"])

    def __call__(self, method, url, headers, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "timeout": timeout})
        parts = urlsplit(url)
        key = "%s %s" % (method, parts.path + ("?" + parts.query if parts.query else ""))
        entry = self.routes.get(key)
        if isinstance(entry, list):
            index = self._served.get(key, 0)
            entry = entry[min(index, len(entry) - 1)] if entry else None
            self._served[key] = index + 1
        if entry is None:
            return Response(404, {"x-recorded": "no-route"}, json.dumps({"message": "Not Found"}))
        if "raise" in entry:
            raise _simulated_exception(entry["raise"])
        body = entry.get("body", "")
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        return Response(entry.get("status", 200), entry.get("headers", {}), body)


def _simulated_exception(name):
    table = {
        "TimeoutError": TimeoutError("simulated"),
        "ConnectionResetError": ConnectionResetError("simulated"),
        "OSError": OSError("simulated"),
        "IncompleteRead": http.client.IncompleteRead(b""),
        "RemoteDisconnected": http.client.RemoteDisconnected("simulated"),
        "SSLError": ssl.SSLError("simulated"),
    }
    return table.get(name, RuntimeError(name))


def tls_context():
    """The TLS context the live transport uses: certificate and hostname verified, TLS 1.2 or newer.

    Python's default context trusts the system store; OpenSSL also honours
    the SSL_CERT_FILE and SSL_CERT_DIR environment variables.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _abort_connection(connection):
    """Watchdog action: close the socket so a hanging phase ends with an exception."""
    for target in (getattr(connection, "sock", None), connection):
        try:
            if target is not None:
                target.close()
        except Exception:
            pass


class LiveTransport:
    """http.client transport for the --live path only. Tests drive it with a fake connection class.

    One HTTPSConnection to api.github.com per request, TLS context from
    tls_context(), no proxy (http.client reads no proxy environment), no
    redirect following (a 3xx is returned as its status and the client
    refuses it), no netrc, no cookies, no Accept-Encoding. A wall-clock
    deadline covers connect, request, headers and body: a watchdog timer
    closes the connection at the deadline and the socket timeout is set
    from the remaining time before each phase and each read. Headers are
    capped in total size, a 1xx final status and a conflicting or invalid
    Content-Length are failures, the body is read in chunks up to the cap
    plus one byte, and a body shorter than its Content-Length is a
    failure, not a 200.
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT, max_body=MAX_BODY, deadline=DEFAULT_DEADLINE, connection_factory=None):
        self.timeout = timeout
        self.max_body = max_body
        self.deadline = deadline
        self._factory = connection_factory

    def __call__(self, method, url, headers, timeout):
        if method != "GET":
            raise TransportRefused("refused: only GET is allowed")
        path, query = check_url(url)
        target = path + ("?" + query if query else "")
        factory = self._factory or http.client.HTTPSConnection
        started = time.monotonic()
        per_phase = timeout or self.timeout

        def remaining():
            left = self.deadline - (time.monotonic() - started)
            if left <= 0:
                raise TransportFailed(TimeoutError("deadline"), path)
            return min(per_phase, left)

        connection = None
        watchdog = None
        try:
            connection = factory(API_HOST, 443, timeout=remaining(), context=tls_context())
            watchdog = threading.Timer(self.deadline, _abort_connection, args=(connection,))
            watchdog.daemon = True
            watchdog.start()
            connection.request("GET", target, headers=dict(headers))
            self._set_timeout(connection, remaining())
            response = connection.getresponse()
            # The header phase is covered by the watchdog and by the first remaining() in _read.
            status = int(response.status)
            if 100 <= status < 200:
                raise TransportFailed(http.client.HTTPException("1xx"), path)
            pairs = list(response.getheaders())
            if sum(len(str(k)) + len(str(v)) for k, v in pairs) > MAX_HEADER_BYTES:
                raise TransportFailed(http.client.LineTooLong("headers"), path)
            merged = merge_headers(pairs)
            declared = merged.get("content-length")
            if declared is not None and header_int(declared) is None:
                raise TransportFailed(http.client.HTTPException("content-length"), path)
            body = self._read(response, merged, path, connection, remaining)
        except GitHubError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise TransportFailed(exc, path) from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        return Response(status, merged, body)

    @staticmethod
    def _set_timeout(connection, seconds):
        connection.timeout = seconds
        sock = getattr(connection, "sock", None)
        settimeout = getattr(sock, "settimeout", None)
        if settimeout is not None:
            try:
                settimeout(seconds)
            except Exception:
                pass

    def _read(self, response, headers, path, connection, remaining):
        chunks = []
        total = 0
        limit = self.max_body + 1
        while total < limit:
            self._set_timeout(connection, remaining())
            chunk = response.read(min(READ_CHUNK, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        body = b"".join(chunks)
        declared = header_int(headers.get("content-length"))
        if declared is not None and total <= self.max_body and total < declared:
            raise TransportFailed(http.client.IncompleteRead(b""), path)
        return body


# --- the client -------------------------------------------------------------


class Client:
    def __init__(self, transport, token=None, max_requests=100, page_caps=None, timeout=DEFAULT_TIMEOUT, max_body=MAX_BODY):
        if token is not None and not isinstance(token, Secret):
            raise TypeError("token must be a Secret")
        if not isinstance(max_requests, int) or isinstance(max_requests, bool) or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        self._transport = transport
        self.token = token
        self.max_requests = max_requests
        self.page_caps = dict(DEFAULT_PAGE_CAPS)
        self.page_caps.update(page_caps or {})
        self.timeout = timeout
        self.max_body = max_body
        self.requests_made = 0
        self.log = []
        self.exhausted = set()
        self.secondary_limited = False
        self.limits = {}
        self.rate_limit_reset = None
        self.retry_after = None
        self.consecutive_failures = 0
        self.transport_failures = 0
        self.network_failed = False

    @property
    def rate_limited(self):
        """True when the run must stop for rate-limit reasons (core bucket exhausted or a secondary limit)."""
        return CORE in self.exhausted or self.secondary_limited

    def bucket_exhausted(self, resource):
        return resource in self.exhausted

    def __repr__(self):
        return "Client(requests_made=%d, max_requests=%d, rate_limited=%r, exhausted=%s, network_failed=%r, token=%s)" % (
            self.requests_made,
            self.max_requests,
            self.rate_limited,
            sorted(self.exhausted),
            self.network_failed,
            "set" if self.token is not None else "none",
        )

    __str__ = __repr__

    def summary(self):
        return {
            "requests_made": self.requests_made,
            "max_requests": self.max_requests,
            "rate_limited": self.rate_limited,
            "exhausted_resources": sorted(self.exhausted),
            "secondary_limited": self.secondary_limited,
            "limits": {k: dict(v) for k, v in sorted(self.limits.items())},
            "rate_limit_reset": self.rate_limit_reset,
            "retry_after": self.retry_after,
            "transport_failures": self.transport_failures,
            "network_failed": self.network_failed,
            "authenticated": self.token is not None,
        }

    def budget_left(self):
        return self.max_requests - self.requests_made

    # -- requests --

    def request(self, method, url, accept=ACCEPT_JSON):
        """GET an allowed URL. Returns Response; raises before the transport on any refusal or stop."""
        if method != "GET":
            raise TransportRefused("refused: only GET is allowed")
        path, query = check_url(url)
        resource = resource_for_path(path)
        if self.rate_limited:
            raise RateLimited("rate limit reached; not requesting %s" % path, CORE)
        if resource in self.exhausted:
            raise RateLimited("%s rate limit reached; not requesting %s" % (resource, path), resource)
        if self.network_failed:
            raise NetworkFailed("%d consecutive transport failures; not requesting %s" % (self.consecutive_failures, path))
        if self.requests_made >= self.max_requests:
            raise BudgetExhausted("request budget of %d used; not requesting %s" % (self.max_requests, path))
        headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}
        if self.token is not None:
            headers["Authorization"] = "Bearer " + self.token.reveal()
        self.requests_made += 1
        self.log.append({"method": "GET", "path": path, "query": query})
        try:
            response = self._transport("GET", url, headers, self.timeout)
        except TransportFailed as exc:
            raise self._note_transport_failure(exc) from None
        except GitHubError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise self._note_transport_failure(TransportFailed(exc, path)) from None
        if not isinstance(response, Response):
            raise self._note_transport_failure(TransportFailed(TypeError("no response"), path))
        # Any answer from the server, whatever its status, ends a failure streak.
        self.consecutive_failures = 0
        self._note_rate_limit(response, path)
        if 300 <= response.status < 400:
            raise ResponseError("redirect refused for %s (status %d)" % (path, response.status))
        if len(response.body) > self.max_body:
            raise ResponseError("response for %s exceeds %d bytes" % (path, self.max_body))
        return response

    def _note_transport_failure(self, failure):
        """Count a transport failure; the one that reaches the threshold becomes a NetworkFailed stop."""
        self.transport_failures += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
            self.network_failed = True
            return NetworkFailed("%d consecutive transport failures (last: %s)" % (self.consecutive_failures, failure))
        return failure

    def _note_rate_limit(self, response, path):
        """Record the bucket's state; raise RateLimited when this answer says the run (or the bucket) is done.

        The bucket is the x-ratelimit-resource header. An answer that does
        not name one is treated as core, so an unidentified limit stops the
        whole run rather than letting it continue. The stop raised for a
        403/429 is labelled by the request path: a non-search path always
        stops as core whatever the header says, so it ends the run.
        """
        remaining = header_int(response.headers.get("x-ratelimit-remaining"))
        reset = header_int(response.headers.get("x-ratelimit-reset"), lo=1)
        retry_after = header_int(response.headers.get("retry-after"))
        resource = response.headers.get("x-ratelimit-resource")
        if not isinstance(resource, str) or _RESOURCE_RE.fullmatch(resource.strip().lower()) is None:
            resource = CORE
        else:
            resource = resource.strip().lower()
        limit = self.limits.setdefault(resource, {"remaining": None, "reset": None})
        if remaining is not None:
            limit["remaining"] = remaining
        if reset is not None:
            limit["reset"] = reset
            if self.rate_limit_reset is None or reset < self.rate_limit_reset:
                self.rate_limit_reset = reset
        if retry_after is not None:
            self.retry_after = retry_after
        if (response.status in (403, 429) and retry_after is not None) or (response.status == 429 and remaining != 0):
            # A secondary limit (retry-after, or a 429 while the bucket still has requests left): everything stops.
            self.secondary_limited = True
            raise RateLimited("secondary rate limit at %s (status %d)" % (path, response.status), CORE)
        if remaining == 0:
            self.exhausted.add(resource)
        if response.status in (403, 429) and remaining == 0:
            label = resource if resource_for_path(path) != CORE else CORE
            self.exhausted.add(label)
            raise RateLimited("%s rate limit reached at %s (status %d)" % (resource, path, response.status), label)

    def get_url(self, url, accept=ACCEPT_JSON):
        return self.request("GET", url, accept)

    def get(self, path, params=None, accept=ACCEPT_JSON):
        return self.request("GET", build_url(path, params), accept)

    def get_json(self, path, params=None, expect="object"):
        """GET and parse. 200 bodies must be the expected shape; other statuses carry no data."""
        response = self.get(path, params)
        return self._parse(response, path, expect)

    def _parse(self, response, path, expect):
        if response.status != 200:
            return Result(response.status, None, response.headers)
        data = parse_json(response.body, path)
        wanted = dict if expect == "object" else list
        if not isinstance(data, wanted):
            raise ResponseError("response for %s is not a JSON %s" % (path, expect))
        return Result(200, data, response.headers)

    def get_text(self, path, params=None, max_chars=16384):
        """GET a raw document; the text is cut to max_chars. None on a non-200 status."""
        response = self.get(path, params, accept=ACCEPT_RAW)
        if response.status != 200:
            return Result(response.status, None, response.headers)
        return Result(200, response.body.decode("utf-8", errors="replace")[:max_chars], response.headers)

    def paginate(self, path, params, cap, expect="array", items_key=None, repository_id=None):
        """Walk Link rel="next" pages from page 1 up to `cap` pages.

        Returns Pages. A stop (rate limit, budget, network) on page 1 is
        raised; a failure (transport, unusable response or Link) on page 1
        returns no items with `stopped` set; after page 1 the items already
        fetched are returned with complete=False, a "page=N" cursor and
        `stopped`. The next page must be numbered page + 1 and may use the
        /repositories/{repository_id}/... spelling of the request path.
        """
        if not isinstance(cap, int) or cap < 1:
            raise ValueError("page cap must be a positive integer")
        params = dict(params or {})
        params["page"] = 1
        url = build_url(path, params)
        _, request_query = check_url(url)
        items = []
        page = 1
        extra = {}
        while True:
            try:
                response = self.get_url(url)
                result = self._parse(response, path, expect)
            except (RateLimited, BudgetExhausted, NetworkFailed) as exc:
                if page == 1:
                    raise
                return Pages(items, False, "page=%d" % page, 200, _stop_reason(exc), extra)
            except (TransportFailed, ResponseError, TransportRefused) as exc:
                reason = "transport" if isinstance(exc, TransportFailed) else "response"
                if page == 1:
                    return Pages(None, False, None, None, reason, extra)
                return Pages(items, False, "page=%d" % page, 200, reason, extra)
            if result.status != 200:
                if page == 1:
                    return Pages(None, False, None, result.status, None, extra)
                return Pages(items, False, "page=%d" % page, 200, "status", extra)
            data = result.data
            if items_key is not None:
                if not isinstance(data.get(items_key), list):
                    if page == 1:
                        return Pages(None, False, None, None, "response", extra)
                    return Pages(items, False, "page=%d" % page, 200, "response", extra)
                for key, value in data.items():
                    if key != items_key:
                        extra[key] = value
                items.extend(data[items_key])
            else:
                items.extend(data)
            try:
                next_url = parse_link_next(response.headers.get("link"))
                if next_url is not None:
                    check_next_url(next_url, path, request_query, page + 1, repository_id)
            except (LinkError, TransportRefused):
                return Pages(items, False, "page=%d" % (page + 1), 200, "response", extra)
            if next_url is None:
                return Pages(items, True, None, 200, None, extra)
            if page >= cap:
                return Pages(items, False, "page=%d" % (page + 1), 200, None, extra)
            page += 1
            url = next_url


def _stop_reason(exc):
    if isinstance(exc, RateLimited):
        if exc.resource == SEARCH:
            return "search_rate_limited"
        if exc.resource == CODE_SEARCH:
            return "code_search_rate_limited"
        return "rate_limited"
    if isinstance(exc, BudgetExhausted):
        return "budget"
    return "network"


def cursor_page(cursor):
    """Page number named by a "page=N" cursor, or None."""
    if not isinstance(cursor, str):
        return None
    pairs = dict(parse_qsl(cursor))
    try:
        page = int(pairs.get("page", ""))
    except ValueError:
        return None
    return page if page > 0 else None


def path_for_repo(full_name, suffix):
    """/repos/{owner}/{name}{suffix} with the validated name components quoted."""
    owner, name = full_name.split("/", 1)
    return "/repos/%s/%s%s" % (quote(owner, safe=""), quote(name, safe=""), suffix)
