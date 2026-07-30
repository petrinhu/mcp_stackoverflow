"""Tests for the `stackoverflow` MCP stdio server (stdlib only, unittest).

Run with: python3 -m unittest test_server -v

Live (network) integration test is gated by SO_MCP_LIVE=1 (opt-in, does not
run by default).
"""
import gzip
import io
import json
import os
import socket
import subprocess
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import server


# --- Fixtures in the api.stackexchange.com/2.3 format (already "decompressed") ---

FIXTURE_SEARCH = {
    "items": [
        {
            "question_id": 111,
            "title": "How to do X in Python?",
            "score": 10,
            "is_answered": True,
            "link": "https://stackoverflow.com/questions/111",
        },
        {
            "question_id": 222,
            "title": "Why Y fails &amp; breaks",
            "score": 3,
            "is_answered": False,
            "link": "https://stackoverflow.com/questions/222",
        },
    ],
    "quota_remaining": 299,
}

FIXTURE_SEARCH_EMPTY = {"items": [], "quota_remaining": 300}

FIXTURE_QUESTION = {
    "items": [
        {
            "title": "How to do X in Python?",
            "tags": ["python", "x"],
            "score": 10,
            "link": "https://stackoverflow.com/questions/111",
            "body": "<p>Use <code>x.do()</code>.</p><p>Thanks!</p>",
        }
    ],
    "quota_remaining": 298,
}

FIXTURE_QUESTION_EMPTY = {"items": [], "quota_remaining": 298}

FIXTURE_ANSWERS = {
    "items": [
        {"answer_id": 1, "score": 5, "is_accepted": False, "body": "<p>Try A.</p>"},
        {
            "answer_id": 2,
            "score": 20,
            "is_accepted": True,
            "body": "<p>Use B.<br>It works.</p>",
        },
        {"answer_id": 3, "score": 2, "is_accepted": False, "body": "<p>C option.</p>"},
    ],
    "quota_remaining": 297,
}

FIXTURE_API_ERROR = {
    "error_id": 400,
    "error_message": "bad parameter question_id",
    "error_name": "bad_parameter",
}

FIXTURE_SEARCH_LOW_QUOTA = {
    "items": [
        {
            "question_id": 111,
            "title": "How to do X in Python?",
            "score": 10,
            "is_answered": True,
            "link": "https://stackoverflow.com/questions/111",
        }
    ],
    "quota_remaining": 5,
}

FIXTURE_SEARCH_BACKOFF = {
    "items": [
        {
            "question_id": 111,
            "title": "How to do X in Python?",
            "score": 10,
            "is_answered": True,
            "link": "https://stackoverflow.com/questions/111",
        }
    ],
    "quota_remaining": 250,
    "backoff": 10,
}


class TestClamp(unittest.TestCase):
    def test_within_range(self):
        self.assertEqual(server.clamp(5, 1, 20), 5)

    def test_below_range(self):
        self.assertEqual(server.clamp(0, 1, 20), 1)

    def test_above_range(self):
        self.assertEqual(server.clamp(50, 1, 20), 20)

    def test_non_int_defaults_to_lo(self):
        self.assertEqual(server.clamp("abc", 1, 20), 1)

    def test_none_defaults_to_lo(self):
        self.assertEqual(server.clamp(None, 1, 20), 1)


class TestBuildUrls(unittest.TestCase):
    def test_search_no_tag(self):
        url = server.build_search_url("python list comprehension")
        self.assertIn("/search/advanced?", url)
        self.assertIn("order=desc", url)
        self.assertIn("sort=relevance", url)
        self.assertIn("site=stackoverflow", url)
        self.assertIn("filter=default", url)
        self.assertIn("pagesize=5", url)
        self.assertNotIn("tagged=", url)

    def test_search_with_tag(self):
        url = server.build_search_url("threads", tag="python")
        self.assertIn("tagged=python", url)

    def test_search_pagesize_clamp(self):
        url = server.build_search_url("x", pagesize=999)
        self.assertIn("pagesize=20", url)
        url2 = server.build_search_url("x", pagesize=0)
        self.assertIn("pagesize=1", url2)

    def test_question_url(self):
        url = server.build_question_url(123)
        self.assertIn("/questions/123?", url)
        self.assertIn("site=stackoverflow", url)
        self.assertIn("filter=withbody", url)

    def test_answers_url(self):
        url = server.build_answers_url(123, top=3)
        self.assertIn("/questions/123/answers?", url)
        self.assertIn("order=desc", url)
        self.assertIn("sort=votes", url)
        self.assertIn("filter=withbody", url)
        self.assertIn("pagesize=3", url)

    def test_answers_top_clamp(self):
        url = server.build_answers_url(123, top=999)
        self.assertIn("pagesize=10", url)

    def test_key_param_from_env(self):
        with patch.dict(os.environ, {"STACKEXCHANGE_KEY": "abc123"}):
            url = server.build_question_url(1)
        self.assertIn("key=abc123", url)

    def test_no_key_param_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            url = server.build_question_url(1)
        self.assertNotIn("key=", url)

    def test_search_default_site(self):
        url = server.build_search_url("x")
        self.assertIn("site=stackoverflow", url)

    def test_search_custom_site(self):
        url = server.build_search_url("x", site="askubuntu")
        self.assertIn("site=askubuntu", url)

    def test_question_custom_site(self):
        url = server.build_question_url(1, site="askubuntu")
        self.assertIn("site=askubuntu", url)

    def test_answers_custom_site(self):
        url = server.build_answers_url(1, site="askubuntu")
        self.assertIn("site=askubuntu", url)


class TestValidateSite(unittest.TestCase):
    def test_none_defaults_to_stackoverflow(self):
        self.assertEqual(server._validate_site(None), "stackoverflow")

    def test_valid_site_passes_through(self):
        self.assertEqual(server._validate_site("askubuntu"), "askubuntu")

    def test_site_with_dot_passes(self):
        self.assertEqual(server._validate_site("money.stackexchange"), "money.stackexchange")

    def test_rejects_space(self):
        with self.assertRaises(ValueError) as ctx:
            server._validate_site("foo bar")
        self.assertIn("Stack Exchange site key", str(ctx.exception))

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            server._validate_site("../evil")

    def test_rejects_non_string(self):
        with self.assertRaises(ValueError):
            server._validate_site(123)


class TestHtmlToText(unittest.TestCase):
    def test_strip_tags_and_entities(self):
        result = server.html_to_text("<p>Hello &amp; <b>world</b></p>")
        self.assertIn("Hello & world", result)
        self.assertNotIn("<b>", result)

    def test_br_becomes_newline(self):
        result = server.html_to_text("line1<br>line2")
        self.assertEqual(result, "line1\nline2")

    def test_br_self_closing_becomes_newline(self):
        result = server.html_to_text("line1<br/>line2")
        self.assertEqual(result, "line1\nline2")

    def test_p_and_pre_become_newline(self):
        result = server.html_to_text("<p>a</p><pre>code</pre>b")
        self.assertIn("a", result)
        self.assertIn("code", result)

    def test_list_items_become_newlines(self):
        result = server.html_to_text("<ul><li>a</li><li>b</li></ul>")
        self.assertEqual(result, "a\nb")

    def test_headings_become_newlines(self):
        result = server.html_to_text("<h1>Title</h1><h3>Sub</h3>body")
        self.assertEqual(result, "Title\nSub\nbody")

    def test_div_becomes_newline(self):
        result = server.html_to_text("<div>a</div><div>b</div>")
        self.assertEqual(result, "a\nb")

    def test_blockquote_becomes_newline(self):
        result = server.html_to_text("<blockquote>quoted</blockquote>after")
        self.assertEqual(result, "quoted\nafter")

    def test_table_row_becomes_newline(self):
        result = server.html_to_text("<table><tr><td>a</td></tr><tr><td>b</td></tr></table>")
        self.assertEqual(result, "a\nb")

    def test_empty_string(self):
        self.assertEqual(server.html_to_text(""), "")

    def test_none(self):
        self.assertEqual(server.html_to_text(None), "")

    def test_collapses_whitespace(self):
        result = server.html_to_text("a    b")
        self.assertEqual(result, "a b")


class TestFormatters(unittest.TestCase):
    def test_format_search_lists_titles(self):
        text = server.format_search(FIXTURE_SEARCH)
        self.assertIn("How to do X in Python?", text)
        self.assertIn("Why Y fails & breaks", text)
        self.assertIn("#111", text)
        self.assertIn("#222", text)

    def test_format_search_empty(self):
        text = server.format_search(FIXTURE_SEARCH_EMPTY)
        self.assertIn("no results found", text.lower())

    def test_format_question(self):
        text = server.format_question(FIXTURE_QUESTION)
        self.assertIn("How to do X in Python?", text)
        self.assertIn("python", text)
        self.assertIn("Use", text)

    def test_format_question_empty(self):
        text = server.format_question(FIXTURE_QUESTION_EMPTY)
        self.assertIn("not found", text.lower())

    def test_format_answers_accepted_first(self):
        text = server.format_answers(FIXTURE_ANSWERS, top=3)
        self.assertLess(text.index("Use B"), text.index("Try A"))
        self.assertIn("accepted", text)

    def test_format_search_low_quota_warning(self):
        with patch.dict(os.environ, {}, clear=True):
            text = server.format_search(FIXTURE_SEARCH_LOW_QUOTA)
        self.assertIn("WARNING", text)
        self.assertIn("5", text)

    def test_format_search_high_quota_no_warning(self):
        text = server.format_search(FIXTURE_SEARCH)  # quota_remaining: 299
        self.assertNotIn("WARNING", text)

    def test_format_search_backoff_note(self):
        text = server.format_search(FIXTURE_SEARCH_BACKOFF)
        self.assertIn("backoff of 10s", text)

    def test_format_search_no_backoff_no_note(self):
        text = server.format_search(FIXTURE_SEARCH)
        self.assertNotIn("backoff", text)

    def test_low_quota_warning_mentions_key_only_when_keyless(self):
        with patch.dict(os.environ, {}, clear=True):
            text = server.format_search(FIXTURE_SEARCH_LOW_QUOTA)
        self.assertIn("STACKEXCHANGE_KEY", text)

    def test_low_quota_warning_omits_key_when_already_keyed(self):
        with patch.dict(os.environ, {"STACKEXCHANGE_KEY": "abc123"}):
            text = server.format_search(FIXTURE_SEARCH_LOW_QUOTA)
        self.assertNotIn("STACKEXCHANGE_KEY", text)


class TestStartupMessage(unittest.TestCase):
    def test_keyless_message(self):
        with patch.dict(os.environ, {}, clear=True):
            msg = server._startup_message()
        self.assertIn("keyless mode", msg)
        self.assertIn("300", msg)
        self.assertIn("STACKEXCHANGE_KEY", msg)

    def test_keyed_message_does_not_leak_key(self):
        with patch.dict(os.environ, {"STACKEXCHANGE_KEY": "super-secret-key-999"}):
            msg = server._startup_message()
        self.assertIn("API key", msg)
        self.assertIn("10,000", msg)
        self.assertNotIn("super-secret-key-999", msg)


class TestHttpGetJson(unittest.TestCase):
    """Exercises the network seam by mocking urllib.request.urlopen directly."""

    @staticmethod
    def _make_response(body_bytes, content_encoding=""):
        headers = {"Content-Encoding": content_encoding} if content_encoding else {}
        resp = MagicMock()
        resp.read.return_value = body_bytes
        resp.headers = headers
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_timeout_error_raises_runtime_error_with_timeout_message(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            with self.assertRaises(RuntimeError) as ctx:
                server._http_get_json("http://example.test")
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_socket_timeout_alias_raises_same_message(self):
        # socket.timeout is an alias of TimeoutError since Python 3.10; this
        # proves both trigger the same handling path.
        with patch("urllib.request.urlopen", side_effect=socket.timeout):
            with self.assertRaises(RuntimeError) as ctx:
                server._http_get_json("http://example.test")
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_url_error_raises_network_error(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(RuntimeError) as ctx:
                server._http_get_json("http://example.test")
        self.assertIn("network error", str(ctx.exception))

    def test_gzip_response_returns_dict(self):
        payload = json.dumps({"ok": True}).encode("utf-8")
        compressed = gzip.compress(payload)
        resp = self._make_response(compressed, content_encoding="gzip")
        with patch("urllib.request.urlopen", return_value=resp):
            result = server._http_get_json("http://example.test")
        self.assertEqual(result, {"ok": True})

    def test_plain_response_returns_dict(self):
        payload = json.dumps({"ok": True}).encode("utf-8")
        resp = self._make_response(payload)
        with patch("urllib.request.urlopen", return_value=resp):
            result = server._http_get_json("http://example.test")
        self.assertEqual(result, {"ok": True})

    def test_invalid_json_raises_runtime_error(self):
        resp = self._make_response(b"not json")
        with patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                server._http_get_json("http://example.test")
        self.assertIn("not valid JSON", str(ctx.exception))

    @staticmethod
    def _make_http_error(body_bytes, code=400, reason="Bad Request"):
        return urllib.error.HTTPError(
            "http://example.test", code, reason, None, io.BytesIO(body_bytes)
        )

    def test_http_error_gzipped_throttle_body_mentions_stackexchange_key(self):
        payload = json.dumps(
            {
                "error_id": 502,
                "error_message": "too many requests from this client",
                "error_name": "throttle_violation",
            }
        ).encode("utf-8")
        http_error = self._make_http_error(gzip.compress(payload))
        try:
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(RuntimeError) as ctx:
                    server._http_get_json("http://example.test")
        finally:
            http_error.close()
        self.assertIn("STACKEXCHANGE_KEY", str(ctx.exception))
        self.assertIn("502", str(ctx.exception))
        self.assertIn("throttle_violation", str(ctx.exception))

    def test_http_error_gzipped_body_has_no_replacement_characters(self):
        payload = json.dumps(
            {"error_id": 502, "error_message": "throttled", "error_name": "throttle_violation"}
        ).encode("utf-8")
        http_error = self._make_http_error(gzip.compress(payload))
        try:
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(RuntimeError) as ctx:
                    server._http_get_json("http://example.test")
        finally:
            http_error.close()
        self.assertNotIn("�", str(ctx.exception))

    def test_http_error_plain_json_body_is_parsed(self):
        payload = json.dumps(
            {"error_id": 400, "error_message": "bad parameter site", "error_name": "bad_parameter"}
        ).encode("utf-8")
        http_error = self._make_http_error(payload)
        try:
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(RuntimeError) as ctx:
                    server._http_get_json("http://example.test")
        finally:
            http_error.close()
        self.assertIn("bad parameter site", str(ctx.exception))

    def test_http_error_garbage_body_falls_back_to_generic_message(self):
        http_error = self._make_http_error(b"\x00\x01not json or gzip")
        try:
            with patch("urllib.request.urlopen", side_effect=http_error):
                with self.assertRaises(RuntimeError) as ctx:
                    server._http_get_json("http://example.test")
        finally:
            http_error.close()
        self.assertIn("HTTP error 400", str(ctx.exception))


class TestParseQuestionId(unittest.TestCase):
    def test_int_is_accepted(self):
        self.assertEqual(server._parse_question_id({"question_id": 111}), 111)

    def test_numeric_string_is_accepted(self):
        self.assertEqual(server._parse_question_id({"question_id": "123"}), 123)

    def test_numeric_string_with_whitespace_is_accepted(self):
        self.assertEqual(server._parse_question_id({"question_id": "  123  "}), 123)

    def test_garbage_string_raises_clean_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            server._parse_question_id({"question_id": "abc"})
        self.assertIn("must be an integer", str(ctx.exception))
        self.assertNotIn("invalid literal", str(ctx.exception))

    def test_non_integer_float_raises_clean_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            server._parse_question_id({"question_id": 1.5})
        self.assertIn("must be an integer", str(ctx.exception))

    def test_bool_raises_clean_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            server._parse_question_id({"question_id": True})
        self.assertIn("must be an integer", str(ctx.exception))

    def test_none_raises_required_error(self):
        with self.assertRaises(ValueError) as ctx:
            server._parse_question_id({"question_id": None})
        self.assertIn("required", str(ctx.exception))


class TestDispatch(unittest.TestCase):
    def test_initialize_echoes_supported_protocol_version(self):
        version = server.SUPPORTED_PROTOCOL_VERSIONS[-1]  # oldest supported, still valid
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version},
        }
        resp = server.handle_message(msg)
        self.assertEqual(resp["result"]["protocolVersion"], version)
        self.assertEqual(resp["result"]["serverInfo"]["name"], "stackoverflow")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_initialize_missing_protocol_version_falls_back_to_latest_supported(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = server.handle_message(msg)
        self.assertEqual(resp["result"]["protocolVersion"], server.SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_initialize_unsupported_protocol_version_falls_back_to_latest_supported(self):
        # a server must not dishonestly claim to support a version it doesn't
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        }
        resp = server.handle_message(msg)
        self.assertEqual(resp["result"]["protocolVersion"], server.SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_initialize_non_dict_params_is_invalid_params(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "x"}
        resp = server.handle_message(msg)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_notification_initialized_returns_none(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.assertIsNone(server.handle_message(msg))

    def test_any_notification_returns_none(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}
        self.assertIsNone(server.handle_message(msg))

    def test_tools_list(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp = server.handle_message(msg)
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"so_search", "so_get_question", "so_get_answers"})
        for tool in resp["result"]["tools"]:
            self.assertIn("description", tool)
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tools_list_includes_site_param(self):
        msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp = server.handle_message(msg)
        for tool in resp["result"]["tools"]:
            self.assertIn("site", tool["inputSchema"]["properties"])

    def test_ping(self):
        msg = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
        resp = server.handle_message(msg)
        self.assertEqual(resp["result"], {})

    def test_unknown_method_is_method_not_found(self):
        msg = {"jsonrpc": "2.0", "id": 4, "method": "foo/bar"}
        resp = server.handle_message(msg)
        self.assertEqual(resp["error"]["code"], -32601)


class TestToolsCall(unittest.TestCase):
    def setUp(self):
        self._orig_http = server._http_get_json

    def tearDown(self):
        server._http_get_json = self._orig_http

    def test_so_search_success(self):
        server._http_get_json = lambda url: FIXTURE_SEARCH
        msg = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "so_search", "arguments": {"query": "python"}},
        }
        resp = server.handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("How to do X in Python?", text)
        self.assertNotIn("isError", resp["result"])

    def test_so_search_missing_query_is_error(self):
        server._http_get_json = lambda url: FIXTURE_SEARCH
        msg = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "so_search", "arguments": {}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])

    def test_so_search_whitespace_only_query_is_error(self):
        server._http_get_json = lambda url: FIXTURE_SEARCH
        msg = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "so_search", "arguments": {"query": "   "}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("required", resp["result"]["content"][0]["text"])

    def test_so_get_question_success(self):
        server._http_get_json = lambda url: FIXTURE_QUESTION
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": 111}},
        }
        resp = server.handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("How to do X in Python?", text)
        self.assertIn("python", text)

    def test_so_get_question_numeric_string_id_succeeds(self):
        server._http_get_json = lambda url: FIXTURE_QUESTION
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": "123"}},
        }
        resp = server.handle_message(msg)
        self.assertNotIn("isError", resp["result"])

    def test_so_get_question_garbage_id_is_clean_error(self):
        server._http_get_json = lambda url: FIXTURE_QUESTION
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": "abc"}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("must be an integer", text)
        self.assertNotIn("invalid literal", text)

    def test_so_get_question_float_id_is_clean_error(self):
        server._http_get_json = lambda url: FIXTURE_QUESTION
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": 1.5}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("must be an integer", resp["result"]["content"][0]["text"])

    def test_so_get_question_bool_id_is_clean_error(self):
        server._http_get_json = lambda url: FIXTURE_QUESTION
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": True}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("must be an integer", resp["result"]["content"][0]["text"])

    def test_so_get_question_missing_id_is_clean_error(self):
        server._http_get_json = lambda url: FIXTURE_QUESTION
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": None}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("required", resp["result"]["content"][0]["text"])

    def test_non_object_arguments_is_clean_error(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": "x"},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        text = resp["result"]["content"][0]["text"]
        self.assertIn("'arguments' must be an object", text)
        self.assertNotIn("AttributeError", text)
        self.assertNotIn("NoneType", text)

    def test_so_get_answers_success_top_clamped_and_accepted_first(self):
        server._http_get_json = lambda url: FIXTURE_ANSWERS
        msg = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "so_get_answers",
                "arguments": {"question_id": 111, "top": 2},
            },
        }
        resp = server.handle_message(msg)
        text = resp["result"]["content"][0]["text"]
        self.assertIn("accepted", text)
        self.assertIn("Use B", text)

    def test_tools_call_non_dict_params_is_invalid_params(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]}
        resp = server.handle_message(msg)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_tool_is_error(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])

    def test_network_error_is_error(self):
        def boom(url):
            raise RuntimeError("network error: timeout")

        server._http_get_json = boom
        msg = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "so_search", "arguments": {"query": "x"}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("network error", resp["result"]["content"][0]["text"])

    def test_api_error_message_propagates_as_error(self):
        server._http_get_json = lambda url: FIXTURE_API_ERROR
        msg = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "so_get_question", "arguments": {"question_id": 1}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("bad parameter", resp["result"]["content"][0]["text"])

    def test_so_search_custom_site_reaches_the_url(self):
        captured = {}

        def fake_http_get_json(url):
            captured["url"] = url
            return FIXTURE_SEARCH

        server._http_get_json = fake_http_get_json
        msg = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "so_search",
                "arguments": {"query": "python", "site": "askubuntu"},
            },
        }
        resp = server.handle_message(msg)
        self.assertNotIn("isError", resp["result"])
        self.assertIn("site=askubuntu", captured["url"])

    def test_so_search_malformed_site_is_clean_error(self):
        server._http_get_json = lambda url: FIXTURE_SEARCH
        msg = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "so_search",
                "arguments": {"query": "python", "site": "foo bar"},
            },
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("site", resp["result"]["content"][0]["text"])

    def test_so_search_path_traversal_site_is_clean_error(self):
        server._http_get_json = lambda url: FIXTURE_SEARCH
        msg = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "so_search",
                "arguments": {"query": "python", "site": "../evil"},
            },
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])

    def test_stackexchange_key_never_leaks_in_http_error_message(self):
        with patch.dict(os.environ, {"STACKEXCHANGE_KEY": "super-secret-key-999"}):
            url = server.build_question_url(1)
            self.assertIn("super-secret-key-999", url)  # sanity: the key is really in the URL
            http_error = urllib.error.HTTPError(
                url, 400, "Bad Request", None, io.BytesIO(b"plain text error body")
            )
            try:
                with patch("urllib.request.urlopen", side_effect=http_error):
                    with self.assertRaises(RuntimeError) as ctx:
                        server._http_get_json(url)
            finally:
                http_error.close()
        self.assertNotIn("super-secret-key-999", str(ctx.exception))

    def test_stackexchange_key_never_leaks_in_tool_error_text(self):
        # end-to-end through the real network seam: build_question_url puts
        # the key in the URL, urlopen raises a real HTTPError carrying that
        # same URL, and the text that reaches the MCP client must not.
        with patch.dict(os.environ, {"STACKEXCHANGE_KEY": "super-secret-key-999"}):
            url = server.build_question_url(1)
            self.assertIn("super-secret-key-999", url)  # sanity: key reached the URL
            http_error = urllib.error.HTTPError(
                url, 400, "Bad Request", None, io.BytesIO(b"plain text error body")
            )
            try:
                with patch("urllib.request.urlopen", side_effect=http_error):
                    msg = {
                        "jsonrpc": "2.0",
                        "id": 14,
                        "method": "tools/call",
                        "params": {"name": "so_get_question", "arguments": {"question_id": 1}},
                    }
                    resp = server.handle_message(msg)
            finally:
                http_error.close()
        text = resp["result"]["content"][0]["text"]
        self.assertNotIn("super-secret-key-999", text)


class TestStdioProcessSurvives(unittest.TestCase):
    """End-to-end: spawns the real server as a subprocess and feeds it lines
    that used to be able to kill it or leave a request unanswered."""

    def test_survives_bad_json_and_bad_utf8_bytes_and_still_answers(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
        proc = subprocess.Popen(
            [sys.executable, script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            proc.stdin.write(b"this is not json at all\n")
            # invalid UTF-8 bytes embedded in an otherwise well-formed line;
            # this used to raise UnicodeDecodeError out of the stdin
            # iterator itself and kill the whole process
            proc.stdin.write(
                b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"\xff\xfe"}}\n'
            )
            proc.stdin.write(
                (
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}
                    )
                    + "\n"
                ).encode("utf-8")
            )
            proc.stdin.close()
            out, _err = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.assertEqual(proc.returncode, 0, "the server process must not crash/exit")

        lines = [line for line in out.decode("utf-8", errors="replace").splitlines() if line.strip()]
        responses = [json.loads(line) for line in lines]
        self.assertEqual(len(responses), 3, f"expected one response per input line, got {responses}")

        # line 1: invalid JSON -> Parse error with id: null
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIsNone(responses[0]["id"])

        # line 2: survived the bad bytes (replaced, not fatal) and got a response
        self.assertEqual(responses[1]["id"], 1)

        # line 3: process is still alive and dispatches normally afterwards
        self.assertEqual(responses[2]["id"], 2)
        self.assertIn("protocolVersion", responses[2]["result"])


class TestLive(unittest.TestCase):
    """Real integration with the API. Gated: only runs with SO_MCP_LIVE=1."""

    @unittest.skipUnless(
        os.environ.get("SO_MCP_LIVE") == "1", "live test opt-in via SO_MCP_LIVE=1"
    )
    def test_real_search(self):
        result = server.tool_so_search(
            {"query": "python list comprehension", "pagesize": 1}
        )
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)


if __name__ == "__main__":
    unittest.main()
