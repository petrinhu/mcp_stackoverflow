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
SITE = "stackoverflow"
HTTP_TIMEOUT_S = 20
SERVER_NAME = "stackoverflow"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


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


def build_search_url(query, tag=None, pagesize=5):
    pagesize = clamp(pagesize, 1, 20)
    q = urllib.parse.quote(query or "")
    url = (
        f"{API_BASE}/search/advanced?order=desc&sort=relevance&q={q}"
        f"&site={SITE}&pagesize={pagesize}&filter=default"
    )
    if tag:
        url += f"&tagged={urllib.parse.quote(tag)}"
    url += _key_param()
    return url


def build_question_url(question_id):
    return f"{API_BASE}/questions/{int(question_id)}?site={SITE}&filter=withbody{_key_param()}"


def build_answers_url(question_id, top=3):
    top = clamp(top, 1, 10)
    return (
        f"{API_BASE}/questions/{int(question_id)}/answers?order=desc&sort=votes"
        f"&site={SITE}&filter=withbody&pagesize={top}{_key_param()}"
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
    quota = data.get("quota_remaining")
    if quota is not None:
        lines.append(f"(quota remaining: {quota})")
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
    quota = data.get("quota_remaining")
    if quota is not None:
        parts.append(f"\n(quota remaining: {quota})")
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
    quota = data.get("quota_remaining")
    if quota is not None:
        text += f"\n\n(quota remaining: {quota})"
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
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP error {exc.code}: {exc.reason} {body}".strip()) from exc
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


def _check_api_error(data):
    if isinstance(data, dict) and data.get("error_id") is not None:
        msg = data.get("error_message") or "unknown API error"
        raise RuntimeError(f"Stack Exchange API error ({data.get('error_id')}): {msg}")


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


def tool_so_search(args):
    args = _require_object(args)
    query = args.get("query")
    if isinstance(query, str):
        query = query.strip()
    if not query:
        raise ValueError("'query' parameter is required")
    tag = args.get("tag")
    pagesize = clamp(args.get("pagesize", 5), 1, 20)
    data = _http_get_json(build_search_url(query, tag, pagesize))
    _check_api_error(data)
    return format_search(data)


def tool_so_get_question(args):
    args = _require_object(args)
    question_id = _parse_question_id(args)
    data = _http_get_json(build_question_url(question_id))
    _check_api_error(data)
    return format_question(data)


def tool_so_get_answers(args):
    args = _require_object(args)
    question_id = _parse_question_id(args)
    top = clamp(args.get("top", 3), 1, 10)
    data = _http_get_json(build_answers_url(question_id, top))
    _check_api_error(data)
    return format_answers(data, top)


TOOLS = [
    {
        "name": "so_search",
        "description": (
            "Searches Stack Overflow for questions matching a text query, "
            "optionally filtered by tag. Use this to find relevant questions "
            "before drilling into a specific one with so_get_question or "
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
            },
            "required": ["query"],
        },
    },
    {
        "name": "so_get_question",
        "description": (
            "Fetches a Stack Overflow question by ID: title, tags, score, "
            "link, and full body text. Use this once you have a question_id "
            "(e.g. from so_search) and need the full question content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "integer", "description": "question ID"},
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "so_get_answers",
        "description": (
            "Fetches the top answers to a Stack Overflow question (accepted "
            "answer first, then ordered by vote score). Use this to read "
            "solutions once you have a question_id."
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
        params = msg.get("params") or {}
        protocol_version = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
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
        params = msg.get("params") or {}
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

def main():
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[stackoverflow-mcp] invalid JSON on input: {exc}", file=sys.stderr)
            continue

        try:
            response = handle_message(msg)
        except Exception as exc:  # never crash the server on a dispatch error
            print(f"[stackoverflow-mcp] unexpected dispatch error: {exc}", file=sys.stderr)
            continue

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
