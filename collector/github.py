"""Read-only GitHub API client, safe by construction.

What the client can do: HTTPS GET to https://api.github.com and nothing
else. Every URL is checked before a transport is called: printable ASCII
only, scheme https, authority exactly api.github.com (so no port, no
credentials, no other host), no fragment, an absolute path of plain
segments (no empty, "." or ".." segments, no percent-encoded dot, slash
or backslash). Redirects are errors, never followed. The only URL ever
taken from a response is a `Link: rel="next"` URL, accepted only when it
names the same host, the same path and the same query apart from `page`,
within a page cap.

Transports are callables `transport(method, url, headers, timeout) ->
Response`. `RecordedTransport` answers from canned responses and is what
the tests and the CLI's dry path use. `LiveTransport` talks http.client
directly (no proxy, no redirect handler, no netrc, no cookie jar) and is
constructed only by the CLI's --live path. Every exception a transport
raises is mapped to `TransportFailed`, whose message names the exception
class and nothing else.

Budget: a hard cap on requests per client; page caps per listing; three
consecutive transport failures stop the client (`NetworkFailed`). Rate
limits: a parsed `x-ratelimit-remaining` of 0, a 429, or a 403 carrying
`retry-after` or a zero remaining stops the client (`RateLimited`); the
client never sleeps and never forgets a recorded limit within a run.
Header values that are not bounded integers are ignored (unknown).

Responses: body capped (MAX_BODY), strict JSON (no duplicate keys, no
NaN/Infinity), shape checked against what the endpoint promises; errors
name the request path and never include a body or a header.

The token is wrapped in `Secret`: it must be 1-255 characters of letters,
digits, '_' or '-' (GitHub tokens fit this), its repr and str are
redacted, and its text is written only into the Authorization header
handed to the transport.
"""

import http.client
import json
import re
import ssl
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from . import schema

API_HOST = "api.github.com"
API_BASE = "https://" + API_HOST
MAX_BODY = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 10
DEFAULT_DEADLINE = 20
READ_CHUNK = 65536
USER_AGENT = "ps3rc-feed-collector (read-only; see repository README)"
API_VERSION = "2022-11-28"
ACCEPT_JSON = "application/vnd.github+json"
ACCEPT_RAW = "application/vnd.github.raw"
DEFAULT_PAGE_CAPS = {"search": 10, "forks": 10, "commits": 5}
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3
MAX_HEADER_INT = 10**12

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{1,255}\Z")
_PRINTABLE_RE = re.compile(r"[\x21-\x7e]{1,2048}\Z")
_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*\Z")
_BAD_ESCAPE_RE = re.compile(r"%(2e|2f|5c)", re.IGNORECASE)
_LINK_RE = re.compile(r'<([^>]*)>\s*;\s*rel="([a-z]+)"')
_DIGITS_RE = re.compile(r"[0-9]{1,13}\Z")


class GitHubError(Exception):
    """Base class; messages never carry response bodies, headers or tokens."""


class TransportRefused(GitHubError):
    """The request would leave the allowlist; raised before any transport call."""


class ResponseError(GitHubError):
    """The response cannot be used: redirect, oversize, bad JSON or wrong shape."""


class TransportFailed(GitHubError):
    """The transport could not complete the request. Names the exception class only."""

    def __init__(self, exc, path=""):
        GitHubError.__init__(self, "transport failure (%s) for %s" % (type(exc).__name__, path or "request"))
        self.kind = type(exc).__name__


class RateLimited(GitHubError):
    """The API rate limit is reached; the run must stop."""


class BudgetExhausted(GitHubError):
    """The configured request budget is used up."""


class NetworkFailed(GitHubError):
    """Several transport failures in a row; the run must stop."""


class Secret:
    """Holds a token. Its text appears nowhere but in `reveal()`."""

    __slots__ = ("_value",)

    def __init__(self, value):
        if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
            raise ValueError("token has an unexpected form (allowed: 1-255 characters of letters, digits, '_' and '-')")
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other):
        return NotImplemented

    __hash__ = None


class Response:
    """What a transport returns. Header names are lower-cased."""

    def __init__(self, status, headers, body):
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
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
    why ("rate_limited", "budget", "network", "transport", "response",
    "status" or None); `status` is the first page's HTTP status (None when
    the first page produced no usable response).
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


def parse_link_next(link_header):
    """The rel="next" URL of a Link header, or None."""
    if not link_header:
        return None
    for url, rel in _LINK_RE.findall(link_header):
        if rel == "next":
            return url
    return None


def _query_dict(query):
    pairs = parse_qsl(query, keep_blank_values=True)
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportRefused("refused: repeated query parameter")
        result[key] = value
    return result


def check_next_url(next_url, request_path, request_query):
    """A next URL must be on the API host, name the same path and the same query apart from `page`."""
    path, query = check_url(next_url)
    if path != request_path:
        raise TransportRefused("refused: Link next URL names a different path")
    mine = _query_dict(request_query)
    theirs = _query_dict(query)
    mine.pop("page", None)
    page = theirs.pop("page", None)
    if page is None or _DIGITS_RE.fullmatch(page) is None:
        raise TransportRefused("refused: Link next URL has no page number")
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


# --- transports -------------------------------------------------------------


class RecordedTransport:
    """Canned responses keyed by "GET <path>?<query>". Unknown requests get a 404.

    An entry may be a response dict, a list of them (served in turn), or a
    dict with "raise": "<ExceptionName>" to simulate a transport failure.
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


class LiveTransport:
    """http.client transport for the --live path only. Tests drive it with a fake connection class.

    One HTTPSConnection to api.github.com per request, default SSL
    context, no proxy (http.client reads no proxy environment), no
    redirect following (a 3xx is returned as its status and the client
    refuses it), no netrc, no cookies, no Accept-Encoding. The body is read
    in chunks up to the cap plus one byte under a wall-clock deadline; a
    body shorter than its Content-Length is a failure, not a 200.
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
        connection = None
        try:
            connection = factory(API_HOST, 443, timeout=timeout or self.timeout, context=ssl.create_default_context())
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            status = int(response.status)
            response_headers = {str(k).lower(): str(v) for k, v in response.getheaders()}
            body = self._read(response, response_headers, path)
        except GitHubError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            raise TransportFailed(exc, path) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        return Response(status, response_headers, body)

    def _read(self, response, headers, path):
        chunks = []
        total = 0
        started = time.monotonic()
        limit = self.max_body + 1
        while total < limit:
            if time.monotonic() - started > self.deadline:
                raise TransportFailed(TimeoutError("deadline"), path)
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
        self.rate_limited = False
        self.rate_limit_reset = None
        self.retry_after = None
        self.consecutive_failures = 0
        self.transport_failures = 0
        self.network_failed = False

    def __repr__(self):
        return "Client(requests_made=%d, max_requests=%d, rate_limited=%r, network_failed=%r, token=%s)" % (
            self.requests_made,
            self.max_requests,
            self.rate_limited,
            self.network_failed,
            "set" if self.token is not None else "none",
        )

    __str__ = __repr__

    def summary(self):
        return {
            "requests_made": self.requests_made,
            "max_requests": self.max_requests,
            "rate_limited": self.rate_limited,
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
        if self.rate_limited:
            raise RateLimited("rate limit reached; not requesting %s" % path)
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
        remaining = header_int(response.headers.get("x-ratelimit-remaining"))
        reset = header_int(response.headers.get("x-ratelimit-reset"), lo=1)
        retry_after = header_int(response.headers.get("retry-after"))
        if reset is not None and (self.rate_limit_reset is None or reset < self.rate_limit_reset):
            self.rate_limit_reset = reset
        if retry_after is not None:
            self.retry_after = retry_after
        limited = remaining == 0 or response.status == 429 or (response.status == 403 and retry_after is not None)
        if limited:
            self.rate_limited = True
        if response.status in (403, 429) and limited:
            raise RateLimited("rate limited at %s (status %d)" % (path, response.status))

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

    def paginate(self, path, params, cap, expect="array", items_key=None):
        """Walk Link rel="next" pages from page 1 up to `cap` pages.

        Returns Pages. A stop (rate limit, budget, network) on page 1 is
        raised; a failure (transport, unusable response) on page 1 returns
        no items with `stopped` set; after page 1 the items already fetched
        are returned with complete=False, a "page=N" cursor and `stopped`.
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
            next_url = parse_link_next(response.headers.get("link"))
            if next_url is None:
                return Pages(items, True, None, 200, None, extra)
            try:
                check_next_url(next_url, path, request_query)
            except TransportRefused:
                return Pages(items, False, "page=%d" % (page + 1), 200, "response", extra)
            if page >= cap:
                return Pages(items, False, "page=%d" % (page + 1), 200, None, extra)
            page += 1
            url = next_url


def _stop_reason(exc):
    if isinstance(exc, RateLimited):
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
