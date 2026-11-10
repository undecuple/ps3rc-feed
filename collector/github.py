"""Read-only GitHub API client, safe by construction.

What the client can do: HTTPS GET to https://api.github.com and nothing
else. Every URL is checked before a transport is called: scheme https,
host exactly api.github.com, no port, no credentials, no fragment, an
absolute path without "..". Redirects are errors, never followed. The only
URL ever taken from a response is a `Link: rel="next"` URL, and it is
accepted only when it points at the same host and the same path as the
request that produced it, within a page cap.

Transports are callables `transport(method, url, headers, timeout) ->
Response`. `RecordedTransport` answers from canned responses and is what
the tests and the CLI's dry path use. `LiveTransport` uses urllib and is
constructed only by the CLI's --live path.

Budget: a hard cap on requests per client; page caps per listing. Rate
limits: an `x-ratelimit-remaining: 0` header, a 429, or a 403 carrying
`retry-after` or a zero remaining stops the client (RateLimited); the
client never sleeps, it reports `retry_after` and the reset time.

Responses: body capped (MAX_BODY), strict JSON (no duplicate keys, no
NaN/Infinity), shape checked against what the endpoint promises; errors
name the request path and never include the body or any header.

The token is wrapped in `Secret`, whose repr and str are redacted; it is
written only into the Authorization header handed to the transport.
"""

import json
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from . import schema

API_HOST = "api.github.com"
API_BASE = "https://" + API_HOST
MAX_BODY = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 10
USER_AGENT = "ps3rc-feed-collector (read-only; see repository README)"
API_VERSION = "2022-11-28"
ACCEPT_JSON = "application/vnd.github+json"
ACCEPT_RAW = "application/vnd.github.raw"
DEFAULT_PAGE_CAPS = {"search": 10, "forks": 10, "commits": 5}

_PATH_RE = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*\Z")
_LINK_RE = re.compile(r'<([^>]*)>\s*;\s*rel="([a-z]+)"')


class GitHubError(Exception):
    """Base class; messages never carry response bodies, headers or tokens."""


class TransportRefused(GitHubError):
    """The request would leave the allowlist; raised before any transport call."""


class ResponseError(GitHubError):
    """The response cannot be used: redirect, oversize, bad JSON or wrong shape."""


class RateLimited(GitHubError):
    """The API rate limit is reached; the run must stop."""


class BudgetExhausted(GitHubError):
    """The configured request budget is used up."""


class Secret:
    """Holds a token. Its text appears nowhere but in `reveal()`."""

    __slots__ = ("_value",)

    def __init__(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError("a secret must be a non-empty string")
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
    """Items gathered over pages; `complete` is False when the cap stopped the walk."""

    def __init__(self, items, complete, next_cursor, status):
        self.items = items
        self.complete = complete
        self.next_cursor = next_cursor
        self.status = status


# --- URL rules --------------------------------------------------------------


def check_url(url):
    """Return (path, query) if `url` is an allowed GET target, else raise TransportRefused."""
    if not isinstance(url, str) or len(url) > 2048:
        raise TransportRefused("refused: URL is not a bounded string")
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
    if not path.startswith("/") or _PATH_RE.fullmatch(path) is None or "/../" in path + "/" or "/./" in path + "/":
        raise TransportRefused("refused: path is not a plain API path")
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


def check_next_url(next_url, request_path):
    """A next URL must be on the API host and name the same path as the request."""
    path, _ = check_url(next_url)
    if path != request_path:
        raise TransportRefused("refused: Link next URL names a different path")
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
    """Canned responses keyed by "GET <path>?<query>". Unknown requests get a 404."""

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
        body = entry.get("body", "")
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        return Response(entry.get("status", 200), entry.get("headers", {}), body)


class LiveTransport:
    """urllib transport for the --live path only. Never constructed by tests.

    https only (no http handler is installed), redirects are not followed
    (a 3xx comes back as its status and the client refuses it), the body
    is read up to the cap plus one byte so oversize responses are detected.
    """

    def __init__(self, timeout=DEFAULT_TIMEOUT, max_body=MAX_BODY):
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        self.timeout = timeout
        self.max_body = max_body
        self._opener = urllib.request.OpenerDirector()
        self._opener.add_handler(_NoRedirect())
        self._opener.add_handler(urllib.request.HTTPSHandler())
        self._opener.add_handler(urllib.request.HTTPErrorProcessor())
        self._opener.add_handler(urllib.request.HTTPDefaultErrorHandler())

    def __call__(self, method, url, headers, timeout):
        import urllib.error
        import urllib.request

        if method != "GET":
            raise TransportRefused("refused: only GET is allowed")
        check_url(url)
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                body = response.read(self.max_body + 1)
                return Response(response.status, dict(response.headers), body)
        except urllib.error.HTTPError as error:
            body = error.read(self.max_body + 1) if hasattr(error, "read") else b""
            return Response(error.code, dict(error.headers or {}), body)


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

    def __repr__(self):
        return "Client(requests_made=%d, max_requests=%d, rate_limited=%r, token=%s)" % (
            self.requests_made,
            self.max_requests,
            self.rate_limited,
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
            "authenticated": self.token is not None,
        }

    def budget_left(self):
        return self.max_requests - self.requests_made

    # -- requests --

    def request(self, method, url, accept=ACCEPT_JSON):
        """GET an allowed URL. Returns Response; raises before the transport on any refusal."""
        if method != "GET":
            raise TransportRefused("refused: only GET is allowed")
        path, query = check_url(url)
        if self.rate_limited:
            raise RateLimited("rate limit reached; not requesting %s" % path)
        if self.requests_made >= self.max_requests:
            raise BudgetExhausted("request budget of %d used; not requesting %s" % (self.max_requests, path))
        headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION}
        if self.token is not None:
            headers["Authorization"] = "Bearer " + self.token.reveal()
        self.requests_made += 1
        self.log.append({"method": "GET", "path": path, "query": query})
        response = self._transport("GET", url, headers, self.timeout)
        if not isinstance(response, Response):
            raise ResponseError("transport returned no response for %s" % path)
        self._note_rate_limit(response, path)
        if 300 <= response.status < 400:
            raise ResponseError("redirect refused for %s (status %d)" % (path, response.status))
        if len(response.body) > self.max_body:
            raise ResponseError("response for %s exceeds %d bytes" % (path, self.max_body))
        return response

    def _note_rate_limit(self, response, path):
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        retry_after = response.headers.get("retry-after")
        if reset is not None:
            self.rate_limit_reset = reset
        if retry_after is not None:
            self.retry_after = retry_after
        limited = remaining == "0" or response.status == 429 or (response.status == 403 and retry_after is not None)
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

        Returns Pages. A non-200 first page yields items None and that
        status. A page cap stops with complete=False and next_cursor
        "page=N" naming the page that was not fetched.
        """
        if not isinstance(cap, int) or cap < 1:
            raise ValueError("page cap must be a positive integer")
        params = dict(params or {})
        params["page"] = 1
        url = build_url(path, params)
        items = []
        page = 1
        extra = {}
        while True:
            response = self.get_url(url)
            result = self._parse(response, path, expect)
            if result.status != 200:
                if page == 1:
                    return Pages(None, False, None, result.status)
                return Pages(items, False, "page=%d" % page, result.status)
            data = result.data
            if items_key is not None:
                if not isinstance(data.get(items_key), list):
                    raise ResponseError("response for %s lacks the %r array" % (path, items_key))
                for key, value in data.items():
                    if key != items_key:
                        extra[key] = value
                items.extend(data[items_key])
            else:
                items.extend(data)
            next_url = parse_link_next(response.headers.get("link"))
            if next_url is None:
                pages = Pages(items, True, None, 200)
                pages.extra = extra
                return pages
            check_next_url(next_url, path)
            if page >= cap:
                pages = Pages(items, False, "page=%d" % (page + 1), 200)
                pages.extra = extra
                return pages
            page += 1
            url = next_url


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
