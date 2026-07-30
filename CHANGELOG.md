# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0]

### Changed

- **All runtime messages returned to the MCP client are now in English** (they
  were in Portuguese before), including tool descriptions and parameter
  descriptions that the model reads to decide whether to call a tool. Anyone
  relying on the previous text will see different output.
- Raised the minimum supported Python version from 3.8 to **3.10**. Below
  3.10, `socket.timeout` is not an alias of `TimeoutError`, which the timeout
  handling relies on; Python 3.8 has also been end-of-life since October 2024.
- The `User-Agent` header now derives from the server version constant
  instead of carrying a fixed, diverging number.

### Added

- Optional `site` parameter on all three tools (default `stackoverflow`),
  opening up the whole Stack Exchange network (askubuntu, superuser,
  serverfault, unix.stackexchange, math, ...).
- Quota mode visibility: a startup line on stderr states whether the server
  is running keyless (300 requests/day per IP) or with a key (10,000/day),
  without ever printing the key; a warning is emitted when the quota runs
  low; and the API's `backoff` field is now reported instead of slept on
  (sleeping would stall the stdio protocol).
- Continuous integration on GitHub Actions: Python 3.10 through 3.14 on
  Linux, plus 3.12 on Windows and macOS.
- `AGENTS.md` (guide for coding agents) and `TODO.md` (task board).
- The test suite grew from 40 to 97 tests, including coverage of the
  `_http_get_json` network function (previously untested) and an end-to-end
  subprocess test proving the server survives malformed input.

### Fixed

- HTTP error bodies from the API arrive gzip-compressed and were being
  decoded as raw UTF-8, delivering unreadable bytes to the user. This is
  exactly the path a quota overrun takes (the API signals throttling as an
  HTTP 400 with `error_id` 502), so the most likely error for a keyless user
  was also the least readable one. The body is now decompressed, the API's
  own error is extracted, and the throttling case becomes an actionable
  message pointing at `STACKEXCHANGE_KEY`.
- A JSON-RPC request with an `id` could go **unanswered** when dispatch
  raised an exception (for example, with a non-object `params`), leaving the
  MCP client hanging until its own timeout. The server now replies with
  `-32603`; invalid JSON replies with `-32700`; a non-object `params` replies
  with `-32602`.
- Malformed UTF-8 bytes on standard input used to **kill the server
  process** (exit 1). Input is now decoded with substitution instead.
- `initialize` echoed back whatever `protocolVersion` the client sent, i.e.
  the server claimed to support versions it did not know.
- Invalid tool input leaked Python traceback jargon (`invalid literal for
  int()`) instead of an actionable message.
- `html_to_text` did not break lines at the close of list items, headings,
  divs, blockquotes, and table rows, running list text together, and lists
  are common in Stack Overflow answers.
- Two `__pycache__/*.pyc` bytecode files were tracked in the public repo; a
  `.gitignore` now excludes them.

## [0.1.0]

Initial release: an MCP stdio server exposing three tools (`so_search`,
`so_get_question`, and `so_get_answers`) over the classic Stack Exchange
REST API. Pure Python standard library, no dependencies.
