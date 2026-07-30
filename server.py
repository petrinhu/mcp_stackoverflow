#!/usr/bin/env python3
"""Servidor MCP stdio `stackoverflow`, sobre a API REST classica api.stackexchange.com/2.3.

Python 3 stdlib apenas (sem pip). Ver DESIGN.md no mesmo diretorio.

IMPORTANTE: stdout eh o canal do protocolo MCP (JSON-RPC 2.0, uma mensagem por
linha). Nenhum log/print de debug pode ir para stdout - tudo isso vai para
stderr. Cada resposta escrita em stdout eh seguida de flush.
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
# Puras / testaveis isoladamente
# --------------------------------------------------------------------------

def clamp(value, lo, hi):
    """Converte value para int e o restringe ao intervalo [lo, hi].

    Valor invalido/ausente cai no piso (lo) - fail-safe, nunca lanca.
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = lo
    return max(lo, min(hi, value))


def _key_param():
    """Anexa &key=<STACKEXCHANGE_KEY> se a env var existir. Nunca hardcodar a key."""
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
_P_CLOSE_RE = re.compile(r"</p>", re.IGNORECASE)
_PRE_CLOSE_RE = re.compile(r"</pre>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_LINE_RE = re.compile(r"\n{3,}")


def html_to_text(s):
    """Converte HTML (corpo de pergunta/resposta) em texto plano legivel.

    Preserva quebras de linha de <br>/</p>/</pre>; perde marcacao de bloco de
    codigo mas preserva o texto (aceitavel para grounding, conforme DESIGN).
    """
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    s = _P_CLOSE_RE.sub("\n", s)
    s = _PRE_CLOSE_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _MULTI_BLANK_LINE_RE.sub("\n\n", s)
    return s.strip()


def format_search(data):
    items = data.get("items") or []
    if not items:
        return "nenhum resultado encontrado."
    lines = []
    for it in items:
        qid = it.get("question_id")
        score = it.get("score")
        title = html.unescape(it.get("title") or "")
        answered = "respondida" if it.get("is_answered") else "sem resposta aceita"
        link = it.get("link") or ""
        lines.append(f"#{qid} [{score}] {title} ({answered}) - {link}")
    quota = data.get("quota_remaining")
    if quota is not None:
        lines.append(f"(quota restante: {quota})")
    return "\n".join(lines)


def format_question(data):
    items = data.get("items") or []
    if not items:
        return "pergunta não encontrada."
    q = items[0]
    title = html.unescape(q.get("title") or "")
    tags = ", ".join(q.get("tags") or [])
    score = q.get("score")
    link = q.get("link") or ""
    body = html_to_text(q.get("body") or "")
    parts = [f"{title}", f"tags: {tags}", f"score: {score}", f"link: {link}", "", body]
    quota = data.get("quota_remaining")
    if quota is not None:
        parts.append(f"\n(quota restante: {quota})")
    return "\n".join(parts)


def format_answers(data, top=3):
    items = data.get("items") or []
    if not items:
        return "nenhuma resposta encontrada."
    top = clamp(top, 1, 10)
    # aceita primeiro, depois por score desc (a API ja ordena por votos; reforca aqui)
    ordered = sorted(
        items, key=lambda a: (not a.get("is_accepted", False), -(a.get("score") or 0))
    )[:top]
    parts = []
    for a in ordered:
        accepted = "aceita" if a.get("is_accepted") else "não aceita"
        score = a.get("score")
        body = html_to_text(a.get("body") or "")
        parts.append(f"[score {score}] ({accepted})\n{body}")
    text = "\n\n---\n\n".join(parts)
    quota = data.get("quota_remaining")
    if quota is not None:
        text += f"\n\n(quota restante: {quota})"
    return text


# --------------------------------------------------------------------------
# Rede - unica costura de teste
# --------------------------------------------------------------------------

def _http_get_json(url):
    """Faz GET, descomprime gzip se necessario, retorna dict. Nunca deixa passar
    para stdout; erros viram RuntimeError com mensagem clara.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "gzip",
            "User-Agent": "claude-mcp-stackoverflow/0.1 (stdlib)",
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
        raise RuntimeError(f"erro HTTP {exc.code}: {exc.reason} {body}".strip()) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"erro de rede: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout de rede ({HTTP_TIMEOUT_S}s)") from exc

    # a API sempre manda gzip, mas trata resposta nao-comprimida por seguranca
    if content_encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise RuntimeError(f"falha ao descomprimir resposta gzip: {exc}") from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"resposta não é JSON válido: {exc}") from exc


def _check_api_error(data):
    if isinstance(data, dict) and data.get("error_id") is not None:
        msg = data.get("error_message") or "erro desconhecido da API"
        raise RuntimeError(f"erro da API StackExchange ({data.get('error_id')}): {msg}")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def tool_so_search(args):
    query = (args or {}).get("query")
    if not query:
        raise ValueError("parâmetro 'query' é obrigatório")
    tag = (args or {}).get("tag")
    pagesize = clamp((args or {}).get("pagesize", 5), 1, 20)
    data = _http_get_json(build_search_url(query, tag, pagesize))
    _check_api_error(data)
    return format_search(data)


def tool_so_get_question(args):
    question_id = (args or {}).get("question_id")
    if question_id is None:
        raise ValueError("parâmetro 'question_id' é obrigatório")
    data = _http_get_json(build_question_url(question_id))
    _check_api_error(data)
    return format_question(data)


def tool_so_get_answers(args):
    question_id = (args or {}).get("question_id")
    if question_id is None:
        raise ValueError("parâmetro 'question_id' é obrigatório")
    top = clamp((args or {}).get("top", 3), 1, 10)
    data = _http_get_json(build_answers_url(question_id, top))
    _check_api_error(data)
    return format_answers(data, top)


TOOLS = [
    {
        "name": "so_search",
        "description": "Busca perguntas no Stack Overflow por texto e, opcionalmente, tag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "texto de busca"},
                "tag": {"type": "string", "description": "filtro de tag (opcional)"},
                "pagesize": {
                    "type": "integer",
                    "description": "quantidade de resultados (1-20, default 5)",
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
        "description": "Obtem titulo, tags, score, link e corpo de uma pergunta do Stack Overflow por ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "integer", "description": "ID da pergunta"},
            },
            "required": ["question_id"],
        },
    },
    {
        "name": "so_get_answers",
        "description": "Obtem as melhores respostas de uma pergunta do Stack Overflow (aceita primeiro, depois por votos).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question_id": {"type": "integer", "description": "ID da pergunta"},
                "top": {
                    "type": "integer",
                    "description": "quantidade de respostas (1-10, default 3)",
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
# Dispatch JSON-RPC (puro, sem I/O)
# --------------------------------------------------------------------------

def _result_response(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg):
    """Despacha uma mensagem JSON-RPC ja parseada. Retorna dict de resposta ou
    None quando a mensagem e uma notificacao (sem 'id', sem resposta).
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
        # notificacao generica (sem id): nao ha resposta a dar, mesmo se
        # o metodo nao seguir a convencao notifications/*
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
                    "content": [{"type": "text", "text": f"tool desconhecida: {name}"}],
                    "isError": True,
                },
            )
        try:
            text = handler(arguments)
            return _result_response(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:
            print(f"[stackoverflow-mcp] erro na tool '{name}': {exc}", file=sys.stderr)
            return _result_response(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"erro: {exc}"}],
                    "isError": True,
                },
            )

    return _error_response(msg_id, -32601, "Method not found")


# --------------------------------------------------------------------------
# Loop stdio
# --------------------------------------------------------------------------

def main():
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"[stackoverflow-mcp] JSON inválido na entrada: {exc}", file=sys.stderr)
            continue

        try:
            response = handle_message(msg)
        except Exception as exc:  # nunca derruba o servidor por erro de dispatch
            print(f"[stackoverflow-mcp] erro inesperado no dispatch: {exc}", file=sys.stderr)
            continue

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
