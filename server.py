#!/usr/bin/env python3
"""MCP stdio server `stackoverflow`, on top of the classic REST API api.stackexchange.com/2.3.

Python 3.10+ stdlib only (no pip).

IMPORTANT: stdout is the MCP protocol channel (JSON-RPC 2.0, one message per
line). No debug log/print may go to stdout - all of that goes to stderr.
Every response written to stdout is followed by a flush.
"""
import gzip
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.stackexchange.com/2.3"
DEFAULT_SITE = "stackoverflow"
HTTP_TIMEOUT_S = 20
SERVER_NAME = "stackoverflow"
SERVER_VERSION = "0.1.0"
# Ordered newest-first: a supported version is echoed back as-is; an
# unsupported (or missing) one falls back to SUPPORTED_PROTOCOL_VERSIONS[0].
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


# --------------------------------------------------------------------------
# Pure / independently testable
# --------------------------------------------------------------------------

def clamp(value, lo, hi):
    """Converts value to int and clamps it to the [lo, hi] range.

    An invalid/missing value falls back to the floor (lo) - fail-safe, never raises.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = lo
    return max(lo, min(hi, value))


def _key_param():
    """Appends &key=<STACKEXCHANGE_KEY> if the env var is set. Never hardcode the key."""
    key = os.environ.get("STACKEXCHANGE_KEY")
    if not key:
        return ""
    return f"&key={urllib.parse.quote(key)}"


_SITE_RE = re.compile(r"^[a-z0-9.-]+$")


def _validate_site(site):
    """Validates the *format* of a Stack Exchange site key (e.g.
    'stackoverflow', 'askubuntu'). Existence is left to the API itself
    (it 404s on an unknown site) - this only guards against building an
    absurd/unsafe URL from garbage input. None defaults to DEFAULT_SITE.
    """
    if site is None:
        return DEFAULT_SITE
    if not isinstance(site, str) or not _SITE_RE.match(site):
        raise ValueError(
            "'site' must be a Stack Exchange site key, e.g. 'stackoverflow' or 'askubuntu'"
        )
    return site


def build_search_url(query, tag=None, pagesize=5, site=DEFAULT_SITE):
    pagesize = clamp(pagesize, 1, 20)
    q = urllib.parse.quote(query or "")
    url = (
        f"{API_BASE}/search/advanced?order=desc&sort=relevance&q={q}"
        f"&site={site}&pagesize={pagesize}&filter=default"
    )
    if tag:
        url += f"&tagged={urllib.parse.quote(tag)}"
    url += _key_param()
    return url


def build_question_url(question_id, site=DEFAULT_SITE):
    return f"{API_BASE}/questions/{int(question_id)}?site={site}&filter=withbody{_key_param()}"


def build_answers_url(question_id, top=3, site=DEFAULT_SITE):
    top = clamp(top, 1, 10)
    return (
        f"{API_BASE}/questions/{int(question_id)}/answers?order=desc&sort=votes"
        f"&site={site}&filter=withbody&pagesize={top}{_key_param()}"
    )


_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"</(?:p|pre|li|h[1-6]|div|blockquote|tr)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_LINE_RE = re.compile(r"\n{3,}")


def html_to_text(s):
    """Converts HTML (question/answer body) into readable plain text.

    Preserves line breaks from block-level tags (<br>, </p>, </pre>, </li>,
    headings, </div>, </blockquote>, </tr>); loses code-block markup but
    keeps the text (acceptable for grounding).
    """
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_CLOSE_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _MULTI_BLANK_LINE_RE.sub("\n\n", s)
    return s.strip()


_LOW_QUOTA_THRESHOLD = 30


def _quota_warning(quota):
    """Returns a warning to surface to the model/user when the daily quota
    is running low (<= _LOW_QUOTA_THRESHOLD remaining), or "" otherwise.

    Only mentions STACKEXCHANGE_KEY as the fix in keyless mode - a caller
    that already has a key is already at the higher 10,000/day ceiling, so
    telling them to set it again would be noise.
    """
    if quota is None or quota > _LOW_QUOTA_THRESHOLD:
        return ""
    warning = f"WARNING: only {quota} Stack Exchange API requests left today."
    if not os.environ.get("STACKEXCHANGE_KEY"):
        warning += (
            " Set the STACKEXCHANGE_KEY environment variable to raise the daily "
            "quota from 300 to 10,000: https://stackapps.com/apps/oauth/register"
        )
    return warning


def _backoff_note(backoff):
    """Returns a note to surface when the API asked for a backoff before the
    next call, or "" otherwise.

    Deliberately does not sleep: blocking here would freeze the whole stdio
    protocol loop (including unrelated requests like `ping`) for however
    long the API wants backed off. Reporting it and letting the MCP client
    decide whether/how to slow down is the caller's call, not the server's.
    """
    if backoff is None:
        return ""
    return f"(the API requested a backoff of {backoff}s before the next call)"


def _quota_and_backoff_suffix(data):
    """Combines the quota-remaining line, low-quota warning, and backoff
    note into the trailing block every tool's text output ends with."""
    lines = []
    quota = data.get("quota_remaining")
    if quota is not None:
        lines.append(f"(quota remaining: {quota})")
    warning = _quota_warning(quota)
    if warning:
        lines.append(warning)
    note = _backoff_note(data.get("backoff"))
    if note:
        lines.append(note)
    return "\n".join(lines)


def format_search(data):
    items = data.get("items") or []
    if not items:
        return "no results found."
    lines = []
    for it in items:
        qid = it.get("question_id")
        score = it.get("score")
        title = html.unescape(it.get("title") or "")
        answered = "answered" if it.get("is_answered") else "no accepted answer"
        link = it.get("link") or ""
        lines.append(f"#{qid} [{score}] {title} ({answered}) - {link}")
    suffix = _quota_and_backoff_suffix(data)
    if suffix:
        lines.append(suffix)
    return "\n".join(lines)


def format_question(data):
    items = data.get("items") or []
    if not items:
        return "question not found."
    q = items[0]
    title = html.unescape(q.get("title") or "")
    tags = ", ".join(q.get("tags") or [])
    score = q.get("score")
    link = q.get("link") or ""
    body = html_to_text(q.get("body") or "")
    parts = [f"{title}", f"tags: {tags}", f"score: {score}", f"link: {link}", "", body]
    suffix = _quota_and_backoff_suffix(data)
    if suffix:
        parts.append("\n" + suffix)
    return "\n".join(parts)


def format_answers(data, top=3):
    items = data.get("items") or []
    if not items:
        return "no answers found."
    top = clamp(top, 1, 10)
    # accepted first, then by score desc (the API already sorts by votes; reinforced here)
    ordered = sorted(
        items, key=lambda a: (not a.get("is_accepted", False), -(a.get("score") or 0))
    )[:top]
    parts = []
    for a in ordered:
        accepted = "accepted" if a.get("is_accepted") else "not accepted"
        score = a.get("score")
        body = html_to_text(a.get("body") or "")
        parts.append(f"[score {score}] ({accepted})\n{body}")
    text = "\n\n---\n\n".join(parts)
    suffix = _quota_and_backoff_suffix(data)
    if suffix:
        text += "\n\n" + suffix
    return text


# --------------------------------------------------------------------------
# Network - the only testing seam
# --------------------------------------------------------------------------

def _http_get_json(url):
    """Performs a GET, decompresses gzip if needed, returns a dict. Never lets
    anything through to stdout; errors become RuntimeError with a clear message.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "gzip",
            "User-Agent": f"claude-mcp-stackoverflow/{SERVER_VERSION} (stdlib)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
            content_encoding = resp.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(_format_http_error(exc)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"network timeout ({HTTP_TIMEOUT_S}s)") from exc

    # the API always sends gzip, but non-compressed responses are handled defensively
    if content_encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise RuntimeError(f"failed to decompress gzip response: {exc}") from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"response is not valid JSON: {exc}") from exc


def _format_api_error(data):
    """Builds the Stack Exchange API error message for an already-parsed
    error payload (a dict with an 'error_id' key). Returns None when `data`
    isn't an API error payload.

    Shared by the HTTP-200-with-error-body path (_check_api_error) and the
    HTTPError path (_format_http_error) so both produce the same message
    format - the quota-exhausted case (error_id 502, throttle_violation) in
    particular arrives as a plain HTTP 400, not a 200.
    """
    if not isinstance(data, dict) or data.get("error_id") is None:
        return None
    error_id = data.get("error_id")
    error_name = data.get("error_name") or "unknown_error"
    error_message = data.get("error_message") or "unknown API error"
    text = f"Stack Exchange API error {error_id} ({error_name}): {error_message}"
    if error_id == 502:
        text += (
            " The API is throttling this client. Wait before retrying, or set "
            "the STACKEXCHANGE_KEY environment variable (free key, raises the "
            "daily quota from 300 to 10,000): "
            "https://stackapps.com/apps/oauth/register"
        )
    return text


def _format_http_error(exc):
    """Builds a clean message for an HTTPError.

    The API's error bodies (including the throttle_violation one sent as a
    plain HTTP 400 when the daily quota is exhausted) are gzip-compressed
    just like successful responses - decompressing them here is what keeps
    a keyless client that hits the 300/day ceiling from seeing raw gzip
    bytes instead of an explanation.
    """
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # not valid gzip after all - fall through with the raw bytes
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    api_error_text = _format_api_error(data)
    if api_error_text is not None:
        return api_error_text
    body = raw.decode("utf-8", errors="replace") if raw else ""
    return f"HTTP error {exc.code}: {exc.reason} {body}".strip()


def _check_api_error(data):
    text = _format_api_error(data)
    if text is not None:
        raise RuntimeError(text)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _require_object(args):
    """Ensures args is a dict (JSON object); None becomes {}.

    Raises ValueError with a clean message for any other type, instead of
    letting an AttributeError leak from a later `.get()` call.
    """
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ValueError("'arguments' must be an object")
    return args


def _parse_question_id(args):
    """Extracts and validates 'question_id' from args.

    Accepts int and numeric string (surrounding whitespace is stripped).
    Rejects bool, float, and anything that doesn't parse to an integer, with
    a message meant for the tool caller (never Python traceback jargon).
    """
    value = args.get("question_id")
    if value is None:
        raise ValueError("'question_id' parameter is required")
    if isinstance(value, bool):
        raise ValueError("'question_id' must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ValueError("'question_id' must be an integer") from None
    raise ValueError("'question_id' must be an integer")


def _log_backoff(data):
    """Logs to stderr (never stdout) when the API asked for a backoff.
    Side-effecting on purpose - kept out of the pure formatters so their
    text-building stays trivially testable without capturing stderr.
    """
    backoff = data.get("backoff") if isinstance(data, dict) else None
    if backoff is not None:
        print(f"[stackoverflow-mcp] API requested a backoff of {backoff}s", file=sys.stderr)


def tool_so_search(args):
    args = _require_object(args)
    query = args.get("query")
    if isinstance(query, str):
        query = query.strip()
    if not query:
        raise ValueError("'query' parameter is required")
    tag = args.get("tag")
    pagesize = clamp(args.get("pagesize", 5), 1, 20)
    site = _validate_site(args.get("site"))
    data = _http_get_json(build_search_url(query, tag, pagesize, site))
    _check_api_error(data)
    _log_backoff(data)
    return format_search(data)


def tool_so_get_question(args):
    args = _require_object(args)
    question_id = _parse_question_id(args)
    site = _validate_site(args.get("site"))
    data = _http_get_json(build_question_url(question_id, site))
    _check_api_error(data)
    _log_backoff(data)
    return format_question(data)


def tool_so_get_answers(args):
    args = _require_object(args)
    question_id = _parse_question_id(args)
    top = clamp(args.get("top", 3), 1, 10)
    site = _validate_site(args.get("site"))
    data = _http_get_json(build_answers_url(question_id, top, site))
    _check_api_error(data)
    _log_backoff(data)
    return format_answers(data, top)


_SITE_PROPERTY = {
    "type": "string",
    "description": (
        "Stack Exchange site key (default 'stackoverflow'). Set this to search "
        "a different site in the Stack Exchange network instead of Stack "
        "Overflow, e.g. 'askubuntu' (Ask Ubuntu), 'superuser' (Super User), "
        "'serverfault' (Server Fault), 'unix' (Unix & Linux), or 'math' "
        "(Mathematics)."
    ),
    "default": "stackoverflow",
}

TOOLS = [
    {
        "name": "so_search",
        "description": (
            "Searches Stack Overflow (or another Stack Exchange site via "
            "'site') for questions matching a text query, optionally "
            "filtered by tag. Use this to find relevant questions before "
            "drilling into a specific one with so_get_question or "
            "so_get_answers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search text"},
                "tag": {"type": "string", "description": "tag filter (optional)"},
                "pagesize": {
                    "type": "integer",
                    "description": "number of results (1-20, default 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
                "site": _SITE_PROPERTY,
            },
            "required": ["query"],
        },
    },
    {
        "name": "so_get_question",
        "description": (
            "Fetches a question by ID from Stack Overflow (or another Stack "
            "Exchange site via 'site'): title, tags, score, link, and full "
            "body text. Use this once you have a question_id (e.g. from "
            "so_search) and need the full question content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "integer", "description": "question ID"},
                "site": _SITE_PROPERTY,
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "so_get_answers",
        "description": (
            "Fetches the top answers to a question from Stack Overflow (or "
            "another Stack Exchange site via 'site') - accepted answer "
            "first, then ordered by vote score. Use this to read solutions "
            "once you have a question_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "integer", "description": "question ID"},
                "top": {
                    "type": "integer",
                    "description": "number of answers (1-10, default 3)",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 10,
                },
                "site": _SITE_PROPERTY,
            },
            "required": ["question_id"],
        },
    },
]

TOOL_HANDLERS = {
    "so_search": tool_so_search,
    "so_get_question": tool_so_get_question,
    "so_get_answers": tool_so_get_answers,
}


# --------------------------------------------------------------------------
# JSON-RPC dispatch (pure, no I/O)
# --------------------------------------------------------------------------

def _result_response(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _coerce_params(params):
    """Returns `params` as a dict for methods that need object params.

    None (params omitted) becomes {}. Anything else that isn't a dict
    raises ValueError - callers turn that into a clean -32602 Invalid
    params response instead of letting a later `.get()` call raise
    AttributeError (e.g. params sent as a string or a list).
    """
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("'params' must be an object")
    return params


def handle_message(msg):
    """Dispatches an already-parsed JSON-RPC message. Returns a response dict
    or None when the message is a notification (no 'id', no response).
    """
    if not isinstance(msg, dict):
        return None

    method = msg.get("method")
    has_id = "id" in msg
    msg_id = msg.get("id")

    if method is None:
        return _error_response(msg_id, -32600, "Invalid Request") if has_id else None

    if isinstance(method, str) and method.startswith("notifications/"):
        return None

    if not has_id:
        # generic notification (no id): no response to give, even if the
        # method doesn't follow the notifications/* convention
        return None

    if method == "initialize":
        try:
            params = _coerce_params(msg.get("params"))
        except ValueError:
            return _error_response(msg_id, -32602, "Invalid params")
        requested_version = params.get("protocolVersion")
        if requested_version in SUPPORTED_PROTOCOL_VERSIONS:
            protocol_version = requested_version
        else:
            # unsupported (or missing) version: per the MCP spec, respond
            # with the most recent version this server does support instead
            # of dishonestly echoing back a version it can't honor
            protocol_version = SUPPORTED_PROTOCOL_VERSIONS[0]
        result = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return _result_response(msg_id, result)

    if method == "ping":
        return _result_response(msg_id, {})

    if method == "tools/list":
        return _result_response(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        try:
            params = _coerce_params(msg.get("params"))
        except ValueError:
            return _error_response(msg_id, -32602, "Invalid params")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _result_response(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True,
                },
            )
        try:
            text = handler(arguments)
            return _result_response(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:
            print(f"[stackoverflow-mcp] error in tool '{name}': {exc}", file=sys.stderr)
            return _result_response(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                },
            )

    return _error_response(msg_id, -32601, "Method not found")


# --------------------------------------------------------------------------
# stdio loop
# --------------------------------------------------------------------------

def _startup_message():
    """Returns the keyless/keyed startup line logged to stderr on boot.

    Never includes the key's value - only whether one is set - so the
    operator can tell at a glance which quota ceiling applies (300/day
    keyless vs 10,000/day with a free key) without the key ever touching
    a log line.
    """
    if os.environ.get("STACKEXCHANGE_KEY"):
        return "[stackoverflow-mcp] starting with an API key: up to 10,000 requests/day"
    return (
        "[stackoverflow-mcp] starting in keyless mode: 300 API requests/day per IP "
        "(set STACKEXCHANGE_KEY to raise it to 10,000/day)"
    )


def main():
    print(_startup_message(), file=sys.stderr)
    try:
        # a malformed (non-UTF-8) byte on stdin must never kill the process:
        # without this, a single bad byte raises UnicodeDecodeError straight
        # out of the `for raw_line in sys.stdin` iteration itself, outside
        # every try/except below, and the server exits.
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # stdin may have been replaced by something without reconfigure (e.g. tests)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[stackoverflow-mcp] invalid JSON on input: {exc}", file=sys.stderr)
            # JSON-RPC requires a response even when the request itself
            # couldn't be parsed enough to know its id
            response = _error_response(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        try:
            response = handle_message(msg)
        except Exception as exc:  # never crash the server on a dispatch error
            print(f"[stackoverflow-mcp] unexpected dispatch error: {exc}", file=sys.stderr)
            # every request with an id is owed a response (JSON-RPC 2.0);
            # silently dropping it would hang the caller until its timeout
            if isinstance(msg, dict) and "id" in msg:
                response = _error_response(msg.get("id"), -32603, "Internal error")
            else:
                response = None

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
