# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` is the working manual for this repo — commands, the five
invariants, `server.py`'s layered architecture, the `TOOLS`/`TOOL_HANDLERS`
trap, the Python version floor, and style rules. Read it first; this file
only adds what `AGENTS.md` does not cover.

## What this is

A dependency-free MCP stdio server that gives read-only access to Stack
Overflow through the classic Stack Exchange REST API
(`api.stackexchange.com/2.3`). It exists because the official hosted MCP
server sits behind a Cloudflare managed challenge and returns 403 to
headless clients; the classic REST API has no such block and needs no
OAuth for reads.

## Rejected on purpose — do not re-propose

- **Automatic retry with sleep.** The protocol is stdio, single-threaded,
  synchronous; sleeping inside a tool call blocks the whole server,
  including `ping`.
- **JSON-RPC batching.** Removed from the MCP spec on 2025-03-26.
- **A line-length guard on stdin.** The client is the local parent process
  and is trusted; there is no untrusted network input to bound.
- **HTTP/2 and connection pooling.** Not worth the stdlib workaround for a
  server that makes one request per tool call.
- **`CONTRIBUTING.md`, `SECURITY.md`, `DESIGN.md`, a `docs/` tree.** A
  two-file project does not sustain a doc tree; `README.md` and
  `AGENTS.md` already cover it.

## Pending work

See `TODO.md` for tracked items.
