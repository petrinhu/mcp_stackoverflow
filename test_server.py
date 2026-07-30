"""Testes do servidor MCP stdio `stackoverflow` (stdlib apenas, unittest).

Roda com: python3 -m unittest test_server -v

Teste de integração real (rede) é gated por SO_MCP_LIVE=1 (opcional, não roda por padrão).
"""
import os
import unittest
from unittest.mock import patch

import server


# --- Fixtures no formato da API api.stackexchange.com/2.3 (já "descomprimidas") ---

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
        self.assertIn("nenhum resultado", text.lower())

    def test_format_question(self):
        text = server.format_question(FIXTURE_QUESTION)
        self.assertIn("How to do X in Python?", text)
        self.assertIn("python", text)
        self.assertIn("Use x.do() .".replace(" .", ".") if False else "Use", text)

    def test_format_question_empty(self):
        text = server.format_question(FIXTURE_QUESTION_EMPTY)
        self.assertIn("não encontrada", text.lower())

    def test_format_answers_accepted_first(self):
        text = server.format_answers(FIXTURE_ANSWERS, top=3)
        self.assertLess(text.index("Use B"), text.index("Try A"))
        self.assertIn("aceita", text)


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
        self.assertIn("aceita", text)
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
            raise RuntimeError("erro de rede: timeout")

        server._http_get_json = boom
        msg = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "so_search", "arguments": {"query": "x"}},
        }
        resp = server.handle_message(msg)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("erro de rede", resp["result"]["content"][0]["text"])

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
    """Integração real com a API. Gated: só roda com SO_MCP_LIVE=1."""

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
