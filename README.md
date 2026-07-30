# mcp_stackoverflow

A small, **dependency-free** [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives any MCP client (Claude Code, Claude Desktop, Cursor, VS Code, ...) read access to **Stack Overflow** through the classic **Stack Exchange REST API** (`api.stackexchange.com/2.3`).

It exposes three tools so an AI assistant can search Stack Overflow, pull a question, and read its top answers to ground its responses in community-verified content.

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
| `so_search` | `query` (required), `tag` (optional), `pagesize` (1..20, default 5) | ranked question list: id, score, title, answered flag, link |
| `so_get_question` | `question_id` | title, tags, score, link, body |
| `so_get_answers` | `question_id`, `top` (1..10, default 3) | top answers (accepted first, then by votes) with body |

---

## Requirements

- **Python 3.8+** (standard library only, nothing to `pip install`).
- An **MCP-compatible client** (Claude Code, Claude Desktop, Cursor, VS Code, ...).

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

The assistant decides when to call `so_search`, `so_get_question`, and `so_get_answers`.

---

## API quota and optional key

Without a key the Stack Exchange API allows **300 requests/day per IP**. A **free** [Stack Apps key](https://stackapps.com/apps/oauth/register) raises that to **10,000/day**. The key only raises quota; **no OAuth is needed for reads**.

The server reads the key from the `STACKEXCHANGE_KEY` environment variable and **never** stores it in the repository.

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

The suite covers URL building (including quota-key injection and clamping), HTML-to-text conversion, result formatting, and the full JSON-RPC dispatch (`initialize`, `tools/list`, `tools/call`, notifications, error paths).

---

## Architecture notes

- `site` is fixed to `stackoverflow`.
- `so_search` uses `/search/advanced` with `sort=relevance`; `so_get_question` and `so_get_answers` use `filter=withbody` to include content.
- Answer ordering is **accepted answer first, then by score**.
- Bodies arrive as HTML and are converted to text (markup is dropped, content is kept, which is what an LLM needs for grounding).
- The server is stateless and is launched by your MCP client as a subprocess over stdio.

---

## License

**AGPL-3.0-or-later**. See [`LICENSE`](LICENSE).

---

## Acknowledgements

Built as a pragmatic workaround after the official `mcp.stackoverflow.com` proved unreachable from headless MCP clients due to Cloudflare bot protection. Data comes from the public [Stack Exchange API](https://api.stackexchange.com/docs); please respect its [terms and rate limits](https://api.stackexchange.com/docs/throttle).
