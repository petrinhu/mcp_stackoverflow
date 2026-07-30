# TODO — mcp_stackoverflow

Pending-work table (canonical). Ordered by wave: items in the same wave touch **disjoint files** and can run in parallel.
Source: CTO audit of commit `b4e404d` (2026-07-30). Scope approved by the project lead: **Track A + B-SITE**, Python floor **3.10+**, everything in English.

Status vocabulary (house contract, kept in pt-br): `⏳ Pendente` · `🔄 Em andamento` · `🟡 Parcial` · `💡 Decisão tomada` · `🎨 Pendente design` · `🔍 Pendente verificação` · `✅ Concluído`.
`✅` is only set after test/audit — delivered implementation goes to `🔍` first.

| ID | Onda | Grupo | Descrição Técnica | Prioridade | Pré-requisito | Dificuldade | Status | Estado Auditado |
|---|---|---|---|---|---|---|---|---|
| I18N-1 | 1 | i18n | Translate `server.py` + `test_server.py` to English: runtime strings the MCP client reads, tool/param descriptions, comments, docstrings and the matching test assertions | Alta | — | Média | ⏳ Pendente | — |
| FIX-1 | 1 | Testes | Cover `_http_get_json` (zero tests today): timeout, `URLError`, gzip body, non-gzip body, non-JSON body. Fix itself became unnecessary with the 3.10+ floor (`socket.timeout` is an alias of `TimeoutError`) | Alta | I18N-1 | Baixa | ⏳ Pendente | — |
| FIX-4 | 1 | Robustez | Input validation: `_parse_question_id` helper (rejects junk/float/bool with a clean message), non-dict `arguments`, whitespace-only `query`. No Python traceback jargon reaches the user | Alta | I18N-1 | Baixa | ⏳ Pendente | — |
| CLEAN-1 | 1 | Higiene | Drop the phantom `DESIGN.md` references, derive User-Agent from `SERVER_VERSION`, make `html_to_text` break lines on `</li>`, `</h1>`-`</h6>`, `</div>`, `</blockquote>`, `</tr>`, remove the dead expression in the test file | Média | I18N-1 | Baixa | ⏳ Pendente | — |
| GIT-1 | 1 | Higiene | Add `.gitignore`; untrack the two `__pycache__/*.pyc` currently committed to the public repo (index op done by the orchestrator) | Alta | — | Baixa | 🔍 Pendente verificação | — |
| CI-1 | 1 | CI | GitHub Actions workflow: Python 3.10-3.14 on Linux, 3.12 on Windows and macOS, `py_compile` + `unittest`. No pip install, no live test | Alta | — | Baixa | 🔍 Pendente verificação | — |
| DOC-AGENTS | 1 | Docs | `AGENTS.md` (agents.md convention): commands, the 5 invariants with rationale, layered architecture, the `TOOLS`+`TOOL_HANDLERS` pitfall, 3.10+ floor, style | Alta | — | Média | 🔍 Pendente verificação | — |
| FIX-2 | 2 | Rede | HTTP error bodies arrive gzipped and are decoded as raw UTF-8 → the user sees binary garbage. Decompress, parse the API error, and make `throttle_violation` (error_id 502) an actionable message pointing at `STACKEXCHANGE_KEY` | Crítica | I18N-1 | Média | ⏳ Pendente | — |
| FIX-3 | 2 | Protocolo | No request with an `id` may go unanswered: `-32603` when dispatch raises, `-32700` on invalid JSON, `-32602` on non-dict `params`; force UTF-8 on stdin so malformed bytes stop killing the process | Crítica | I18N-1 | Média | ⏳ Pendente | — |
| FEAT-QUOTA | 2 | Feature | Keyless/keyed mode visibility: startup log on stderr (never printing the key), low-quota warning, and reporting of the API `backoff` field | Alta | I18N-1 | Média | ⏳ Pendente | — |
| DOC-README | 2 | Docs | README refresh: badges (CI, license, Python, zero-dependency, MCP), 3.10+ floor, updated quota section, short Contributing and Security lines | Alta | CI-1 | Média | ⏳ Pendente | — |
| B-SITE | 3 | Feature | Optional `site` parameter on the three tools (default `stackoverflow`), opening the whole Stack Exchange network (askubuntu, superuser, serverfault, unix, math) | Média | FIX-2, FIX-3 | Baixa | ⏳ Pendente | — |
| DOC-CHANGELOG | 3 | Docs | `CHANGELOG.md` (Keep a Changelog) + version bump; highlight that runtime messages moved from pt-br to English | Média | Onda 2 | Baixa | ⏳ Pendente | — |
| DOC-CLAUDE | 3 | Docs | Rewrite `CLAUDE.md` in English, short, pointing at `AGENTS.md` instead of duplicating it; drop the now-obsolete pt-br convention | Média | DOC-AGENTS | Baixa | ⏳ Pendente | — |
| QA-FINAL | 4 | Auditoria | Independent adversarial audit by a `qa-engineer` (never an implementer): full suite, subprocess e2e, mutation spot-check, i18n sweep by enumeration, doc-vs-code consistency, real CI status | Crítica | Ondas 1-3 | Média | ⏳ Pendente | — |
| WIKI-1 | 5 | Docs | Repo Wiki (GitHub wiki-native) + extensive beginner-level `.md` documentation explaining every piece of jargon, derived from the existing docs (links, does not duplicate). Runs via `technical-writer` | Baixa | Tag de versão | Alta | ⏳ Pendente | — |

## INBOX

- Track B items deliberately deferred until real usage demand (consumer-driven cadence): search filters (`accepted`, `answers>=N`, date range), pagination, and a `so_get_comments` tool. Each one is permanent public surface exposed to the LLM and spends the same daily quota.
- Deliberately rejected as over-engineering, recorded so they are not re-proposed: automatic retry with sleep (would block the single-threaded stdio protocol, including `ping`), JSON-RPC batching (removed from the MCP spec in 2025-03-26), stdin line-size DoS guard (the client is the trusted local parent process), HTTP/2 and connection pooling.
