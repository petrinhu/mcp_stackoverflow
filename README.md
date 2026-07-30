# mcp_stackoverflow

[![CI](https://github.com/petrinhu/mcp_stackoverflow/actions/workflows/ci.yml/badge.svg)](https://github.com/petrinhu/mcp_stackoverflow/actions/workflows/ci.yml)
[![License: AGPL v3+](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0%20(stdlib%20only)-brightgreen.svg)](#how-it-is-built)
[![MCP](https://img.shields.io/badge/MCP-stdio%20server-informational.svg)](https://modelcontextprotocol.io)

A small, **dependency-free** [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives any MCP client (Claude Code, Claude Desktop, Cursor, VS Code, ...) read access to **Stack Overflow and the whole Stack Exchange network** through the classic **Stack Exchange REST API** (`api.stackexchange.com/2.3`).

It exposes three tools so an AI assistant can search questions, pull a question, and read its top answers to ground its responses in community-verified content.

---

## Why this exists

Stack Overflow ships an official hosted MCP server at `mcp.stackoverflow.com`. In practice it sits behind a **Cloudflare "managed challenge"** (bot protection): every request from a **headless** MCP client returns **HTTP 403** with a `Just a moment...` interstitial, because passing the challenge requires a real browser executing JavaScript and setting cookies (`cf_clearance`).

This affects any non-browser MCP transport, from **any** network or IP:

- `npx mcp-remote https://mcp.stackoverflow.com` -> `StreamableHTTPError ... code: 403` (Cloudflare `cType: managed`).
- Claude Code's native HTTP transport -> `Failed to connect`.

A separate Node/HTTP process cannot inherit the browser's `cf_clearance` cookie, so the OAuth-in-a-browser step does not help the data connection. Bypassing a Cloudflare challenge is out of scope (and would be defeating a protection).

**This project sidesteps the problem** by talking to the classic, bot-friendly **Stack Exchange REST API v2.3** instead. That API has no managed challenge, needs **no OAuth** for read access, and is the same public API that has powered Stack Exchange integrations for years.

---

## How it is built

- **Pure Python 3 standard library. Zero pip dependencies** (`json`, `urllib`, `gzip`, `html`, `re`, `os`, `sys`).
- A **stdio** MCP server speaking **JSON-RPC 2.0**, one message per line. `stdout` carries only the protocol; all logs go to `stderr`.
- All network access funnels through a single `_http_get_json()` function, which makes the logic easy to unit-test with mocked responses.
- Handles **gzip** (the Stack Exchange API always gzips responses) and converts HTML question/answer bodies to plain text.
- Read-only. No credentials required for the free tier.

### Tools

| Tool | Arguments | Returns |
|---|---|---|
| `so_search` | `query` (required), `tag` (optional), `pagesize` (1..20, default 5), `site` (optional, default `stackoverflow`) | ranked question list: id, score, title, answered flag, link |
| `so_get_question` | `question_id` (required), `site` (optional, default `stackoverflow`) | title, tags, score, link, body |
| `so_get_answers` | `question_id` (required), `top` (1..10, default 3), `site` (optional, default `stackoverflow`) | top answers (accepted first, then by votes) with body |

`site` accepts any Stack Exchange site key, not just Stack Overflow: `askubuntu`, `superuser`, `serverfault`, `unix` (Unix & Linux), `math` (Mathematics), and the rest of the network. It defaults to `stackoverflow`, so existing calls that omit it behave exactly as before.

---

## Requirements

- **Python 3.10+** (standard library only, nothing to `pip install`). Below 3.10, `socket.timeout` is not an alias of the built-in `TimeoutError`, so the network-timeout handling in this server does not hold on older interpreters; Python 3.8/3.9 are also EOL (3.8 since October 2024).
- An **MCP-compatible client** (Claude Code, Claude Desktop, Cursor, VS Code, ...).

CI proves the supported range on every push: Python 3.10, 3.11, 3.12, 3.13, and 3.14 on Linux, plus 3.12 on Windows and macOS (7 jobs total).

---

## Install

```sh
git clone https://github.com/petrinhu/mcp_stackoverflow.git
```

### Claude Code

```sh
claude mcp add -s user stackoverflow -- python3 /absolute/path/to/mcp_stackoverflow/server.py
```

Verify it connected:

```sh
claude mcp get stackoverflow      # Status: Connected
```

Remove it with `claude mcp remove stackoverflow -s user`.

### Any MCP client (JSON config)

```json
{
  "mcpServers": {
    "stackoverflow": {
      "command": "python3",
      "args": ["/absolute/path/to/mcp_stackoverflow/server.py"]
    }
  }
}
```

---

## Usage

Once connected, the tools are available to the assistant automatically. Just ask in natural language, for example:

- "Search Stack Overflow for `asyncio gather vs wait`."
- "Show me the accepted answer and top answers for question 4260280."
- "Find questions tagged `rust` about lifetime elision."
- "Search Ask Ubuntu for how to fix a broken `apt` lock file." (uses `site: askubuntu`)

The assistant decides when to call `so_search`, `so_get_question`, and `so_get_answers`, including which `site` to target.

---

## API quota: works with or without a key

Using the server **without** a key is a fully supported, first-class way to run it: you just get a lower daily ceiling. Adding a key only raises that ceiling; it is **never required** for reads, because this API needs no OAuth.

| Mode | Daily quota | Setup |
|---|---|---|
| Keyless (default) | 300 requests/day per IP | none |
| With `STACKEXCHANGE_KEY` | 10,000 requests/day | free [Stack Apps key](https://stackapps.com/apps/oauth/register) |

Quota handling is visible end-to-end, not silent:

- **On startup**, the server logs to stderr which mode is active (keyless or keyed), without ever printing the key itself.
- **When quota runs low**, the tool's response text includes a warning, plus a pointer to `STACKEXCHANGE_KEY` if you're still keyless.
- **When the API asks for a backoff** before the next call, that is reported in the response text instead of the server sleeping; sleeping would freeze the whole stdio protocol loop, including unrelated requests.
- **When the API is throttling the client** (quota exhausted), the error message explains what happened and links straight to the free key registration page, instead of surfacing raw gzip bytes.

The server reads the key from the `STACKEXCHANGE_KEY` environment variable and **never** stores it in the repository or logs it.

```sh
# Claude Code: pass it as an env var (kept in ~/.claude.json, which is not committed)
claude mcp remove stackoverflow -s user
claude mcp add -s user -e STACKEXCHANGE_KEY=YOUR_KEY stackoverflow -- python3 /absolute/path/to/mcp_stackoverflow/server.py

# or export it in your shell profile
export STACKEXCHANGE_KEY=YOUR_KEY
```

If the variable is absent the server simply runs keyless (300/day).

---

## Testing

```sh
cd mcp_stackoverflow
python3 -m unittest test_server                  # offline, HTTP is mocked
SO_MCP_LIVE=1 python3 -m unittest test_server     # also runs one real API call
```

The suite (97 tests) covers URL building (including quota-key injection, `site` selection, and clamping), HTML-to-text conversion, quota/backoff messaging, result formatting, and the full JSON-RPC dispatch (`initialize`, `tools/list`, `tools/call`, notifications, error paths).

---

## Architecture notes

- `so_search` uses `/search/advanced` with `sort=relevance`; `so_get_question` and `so_get_answers` use `filter=withbody` to include content.
- Answer ordering is **accepted answer first, then by score**.
- Bodies arrive as HTML and are converted to text (markup is dropped, content is kept, which is what an LLM needs for grounding).
- The server is stateless and is launched by your MCP client as a subprocess over stdio.

For the internals (file layout, invariants, how to add a tool), see [`AGENTS.md`](AGENTS.md): that's the guide for anyone changing the code, not for plugging the server into a client.

---

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/petrinhu/mcp_stackoverflow). The test suite must pass before a PR is merged. Contributions are accepted under this project's license, AGPL-3.0-or-later. If you're changing `server.py`, read [`AGENTS.md`](AGENTS.md) first: it documents the invariants the server depends on.

## Security

To report a security issue, open an [issue on GitHub](https://github.com/petrinhu/mcp_stackoverflow/issues). The attack surface is intentionally small: the server is read-only, stores no credentials, and its only optional secret (`STACKEXCHANGE_KEY`) comes from an environment variable and is never logged or written to disk.

---

## License

**AGPL-3.0-or-later**. See [`LICENSE`](LICENSE).

---

## Acknowledgements

Built as a pragmatic workaround after the official `mcp.stackoverflow.com` proved unreachable from headless MCP clients due to Cloudflare bot protection. Data comes from the public [Stack Exchange API](https://api.stackexchange.com/docs); please respect its [terms and rate limits](https://api.stackexchange.com/docs/throttle).
