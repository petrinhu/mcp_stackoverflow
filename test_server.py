"""Tests for the `stackoverflow` MCP stdio server (stdlib only, unittest).

Run with: python3 -m unittest test_server -v

Live (network) integration test is gated by SO_MCP_LIVE=1 (opt-in, does not
run by default).
"""
import gzip
import json
import os
import socket
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
    def test_initialize_echoes_protocol_version(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-01-01"},
        }
        resp = server.handle_message(msg)
        self.assertEqual(resp["result"]["protocolVersion"], "2025-01-01")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "stackoverflow")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_initialize_default_protocol_version(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = server.handle_message(msg)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")

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
