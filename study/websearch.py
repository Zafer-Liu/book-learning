"""Optional web-search channel: Zhipu tools API by default, remote MCP opt-in.

The default transport is Zhipu's OpenAI-compatible tools endpoint
(paas/v4/tools, model web-search-pro) — the same channel and key the official
@z_ai/mcp-server npm package uses in ZHIPU mode, so an ordinary Zhipu API key
works. Setting STUDY_SEARCH_MCP_URL switches to a remote streamable-HTTP MCP
server instead (e.g. the GLM Coding Plan web_search_prime endpoint, which
requires its own plan-specific key). Either way results are normalised into
flat reference rows for the tutor. The channel is strictly opt-in: without
STUDY_SEARCH_API_KEY nothing here is reachable from the chat flow, and
failures surface as tool-level errors the model can route around instead of
killing an answer.
"""

import json
import logging
import os
import threading
from urllib.parse import urlsplit

import requests

LOG = logging.getLogger(__name__)

# Default: Zhipu tools API (same key family as the LLM providers).
DEFAULT_TOOLS_BASE = "https://open.bigmodel.cn/api/paas/v4"
SEARCH_MODEL = "web-search-pro"
# Opt-in: remote MCP server (streamable HTTP). Any server exposing an
# equivalent search tool works by setting STUDY_SEARCH_MCP_URL.
TOOL_NAME = "webSearchPrime"
# The query argument name is discovered from tools/list; these are the
# candidates tried when the server cannot be introspected.
QUERY_FIELDS = ("query", "search_query", "q", "keyword", "keywords")
COUNT_FIELDS = ("count", "num", "limit", "max_results", "top_k")
# Protocol versions accepted by the client, newest first; initialize retries
# down the list when a server rejects the first offer.
PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")
MAX_RESULTS = 8


class WebSearchError(Exception):
    """Raised for any transport, protocol or configuration failure."""


class _SessionExpired(WebSearchError):
    """Server discarded our session id; one transparent re-init is allowed."""


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _http_url(value):
    if not isinstance(value, str):
        return ""
    parts = urlsplit(value.strip())
    return value.strip()[:600] if parts.scheme in {"http", "https"} and parts.netloc else ""


def _normalize(name: str) -> str:
    """Fold camelCase / snake_case / kebab-case for tolerant tool matching."""
    return name.lower().replace("_", "").replace("-", "")


class WebSearchClient:
    def __init__(self):
        # Explicit MCP URL switches to the remote-MCP transport; otherwise the
        # default Zhipu tools API is used (same key as the LLM providers).
        self.url = (os.getenv("STUDY_SEARCH_MCP_URL") or "").strip()
        self.mcp_mode = bool(self.url)
        self.base = (os.getenv("STUDY_SEARCH_BASE_URL") or DEFAULT_TOOLS_BASE).strip().rstrip("/")
        self.key = os.getenv("STUDY_SEARCH_API_KEY", "").strip()
        self.configured = bool(self.key and (self.url or self.base) and self._valid_url())
        self.session = requests.Session()
        self._guard = threading.Lock()
        self._mcp_session = ""
        self._tool_name = TOOL_NAME
        self._query_field = ""
        self._count_field = ""
        self._rpc_id = 0

    def _valid_url(self):
        """Same contract as the LLM base: HTTPS unless local, no query/fragment."""
        target = self.url if self.mcp_mode else self.base
        try:
            parts = urlsplit(target)
        except ValueError:
            return False
        local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        return (parts.scheme in {"http", "https"} and bool(parts.netloc)
                and (parts.scheme == "https" or local) and not parts.query and not parts.fragment)

    # ------------------------------------------------------------------
    # Default transport: Zhipu tools API (web-search-pro)
    # ------------------------------------------------------------------

    def _tools_search(self, query: str, count: int) -> list[dict]:
        payload = {"model": SEARCH_MODEL, "stream": False,
                   "messages": [{"role": "user", "content": query}]}
        try:
            with self._guard:
                with self.session.post(self.base + "/tools", json=payload,
                                       headers={"Authorization": "Bearer " + self.key},
                                       timeout=(5, 40), allow_redirects=False) as response:
                    response.raise_for_status()
                    data = response.json()
        except requests.RequestException as exc:
            raise WebSearchError("联网搜索服务请求失败或超时。") from exc
        except ValueError as exc:
            raise WebSearchError("联网搜索服务返回了无法解析的内容。") from exc
        if not isinstance(data, dict):
            raise WebSearchError("联网搜索服务返回了无效响应。")
        # web-search-pro nests results inside choices[].message.tool_calls[]
        # (a search_intent call followed by a search_result call); older
        # deployments may also return them at the top level.
        items = data.get("search_result") if isinstance(data.get("search_result"), list) else []
        if not items:
            for choice in data.get("choices") or []:
                for call in ((choice.get("message") or {}).get("tool_calls") or []):
                    if isinstance(call, dict) and isinstance(call.get("search_result"), list):
                        items.extend(call["search_result"])
        rows = [self._row(item) for item in items]
        rows = [row for row in rows if row["title"] or row["snippet"]]
        return rows[:count]

    # ------------------------------------------------------------------
    # Opt-in transport: JSON-RPC over streamable HTTP (remote MCP)
    # ------------------------------------------------------------------

    def _post(self, payload: dict, notification=False):
        """One MCP POST; returns the JSON-RPC response message, or None for an
        accepted notification. Raises WebSearchError or requests exceptions."""
        headers = {
            "Authorization": "Bearer " + self.key,
            "Accept": "application/json, text/event-stream",
        }
        if self._mcp_session:
            headers["Mcp-Session-Id"] = self._mcp_session
        with self.session.post(self.url, json=payload, headers=headers,
                               timeout=(5, 40), allow_redirects=False) as response:
            if response.status_code == 404 and self._mcp_session:
                # Spec signal for an expired server session: re-initialize.
                raise _SessionExpired("MCP session expired")
            if response.status_code == 202:
                return None  # accepted notification
            response.raise_for_status()
            if "text/event-stream" in (response.headers.get("Content-Type") or ""):
                # SSE bodies stream the JSON-RPC reply; .text may be empty.
                return self._sse_response(response, payload.get("id"))
            body = response.text
            if not body.strip():
                return None
            try:
                message = response.json()
            except ValueError as exc:
                raise WebSearchError("MCP 服务返回了无法解析的内容。") from exc
        if not isinstance(message, dict):
            raise WebSearchError("MCP 服务返回了无效响应。")
        return message

    @staticmethod
    def _sse_response(response, expected_id):
        """Extract the JSON-RPC response carried by an SSE body."""
        for raw in response.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict) and (expected_id is None or message.get("id") == expected_id):
                return message
        raise WebSearchError("MCP 服务的 SSE 响应缺少结果。")

    @staticmethod
    def _raise_protocol_error(message):
        error = message.get("error") or {}
        detail = error.get("message") if isinstance(error, dict) else None
        raise WebSearchError(f"MCP 服务返回错误：{_text(detail, 200) or '未知错误'}")

    def _ensure_session(self):
        """initialize → initialized → tools/list; caches the session id and the
        tool's argument names. Safe to call repeatedly (only the first call
        does work); version failures fall back down PROTOCOL_VERSIONS."""
        if self._mcp_session:
            return
        for version in PROTOCOL_VERSIONS:
            self._rpc_id += 1
            reply = self._post({
                "jsonrpc": "2.0", "id": self._rpc_id, "method": "initialize",
                "params": {"protocolVersion": version, "capabilities": {},
                           "clientInfo": {"name": "study-agent", "version": "1.0"}},
            })
            if reply is None:
                raise WebSearchError("MCP 初始化无响应。")
            if "error" in reply:
                if version != PROTOCOL_VERSIONS[-1]:
                    continue
                self._raise_protocol_error(reply)
            session = reply.get("result", {}).get("sessionId") or ""
            self._mcp_session = _text(session, 200) or self._mcp_session
            break
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._discover_schema()

    def _discover_schema(self):
        """Read the server's tool list: pick the real tool name (docs spell it
        webSearchPrime, the server registers web_search_prime) and its input
        schema so argument names match the server."""
        self._rpc_id += 1
        reply = self._post({"jsonrpc": "2.0", "id": self._rpc_id, "method": "tools/list"})
        if not isinstance(reply, dict) or "error" in reply:
            return
        tools = ((reply.get("result") or {}).get("tools") or [])
        if not isinstance(tools, list):
            return
        names = [tool.get("name") for tool in tools
                 if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
        chosen = next((name for name in names if name == TOOL_NAME), None)
        if chosen is None:
            # Normalized match folds camelCase / snake_case / kebab-case.
            by_normalized = {_normalize(name): name for name in names}
            chosen = by_normalized.get(_normalize(TOOL_NAME))
        if chosen is None:
            chosen = next((name for name in names if "search" in name.lower()), None)
        if chosen is None:
            LOG.warning("MCP server exposes no search tool; available: %s", names)
            return
        self._tool_name = chosen
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("name") != chosen:
                continue
            properties = ((tool.get("inputSchema") or {}).get("properties") or {})
            if not isinstance(properties, dict):
                return
            names = {name for name, spec in properties.items()
                     if isinstance(spec, dict) and (spec.get("type") == "string" or "anyOf" in spec)}
            self._query_field = next((name for name in QUERY_FIELDS if name in names), "")
            self._count_field = next((name for name in COUNT_FIELDS if name in properties), "")
            return

    def _call_tool(self, query: str, count: int):
        self._rpc_id += 1
        arguments = {self._query_field or "query": query}
        if self._count_field:
            arguments[self._count_field] = count
        request = {"jsonrpc": "2.0", "id": self._rpc_id, "method": "tools/call",
                   "params": {"name": self._tool_name, "arguments": arguments}}
        reply = self._post(request)
        if reply is None:
            raise WebSearchError("MCP 工具调用无响应。")
        if "error" in reply:
            self._raise_protocol_error(reply)
        result = reply.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            # isError responses carry the server's reason as text blocks
            # (e.g. "MCP error -401: Api key not found"); surface it instead
            # of a generic failure so misconfiguration is diagnosable.
            detail = ""
            content = result.get("content") if isinstance(result, dict) else None
            if isinstance(content, list):
                detail = " ".join(_text(block.get("text"), 200) for block in content
                                  if isinstance(block, dict))[:200].strip()
            raise WebSearchError(f"MCP 工具调用失败:{detail or '未知错误'}")
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, count: int = 6) -> list[dict]:
        """Run one web search; returns normalised reference rows (possibly
        empty). Raises WebSearchError on any failure."""
        if not self.configured:
            raise WebSearchError("未配置联网检索服务。")
        query = query.strip()[:400]
        if not query:
            raise WebSearchError("检索词为空。")
        count = max(1, min(int(count), MAX_RESULTS))
        if not self.mcp_mode:
            return self._tools_search(query, count)
        with self._guard:
            try:
                self._ensure_session()
                result = self._call_tool(query, count)
            except _SessionExpired:
                # One transparent re-initialization on an expired session.
                self._mcp_session = ""
                self._ensure_session()
                result = self._call_tool(query, count)
        return self._normalize(result, count)

    @staticmethod
    def _items(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("search_result", "search_results", "results", "data", "items"):
                if isinstance(value.get(key), list):
                    return value[key]
        return []

    def _normalize(self, result: dict, count: int) -> list[dict]:
        """Flatten tool output (text blocks and/or structuredContent) into
        {title, url, site, snippet, icon} rows; tolerant to key variations."""
        sources = []
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            sources.append(structured)
        texts = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" \
                    and isinstance(block.get("text"), str):
                text = block["text"].strip()
                if not text:
                    continue
                try:
                    decoded = json.loads(text)
                except ValueError:
                    texts.append(text)  # free-form text, kept as one snippet
                    continue
                sources.append(decoded)
        rows = []
        for source in sources:
            for item in self._items(source):
                if isinstance(item, dict):
                    rows.append(self._row(item))
        if not rows:
            # No structured results at all: keep the raw text (if any) as a
            # link-less reference so the model can still quote it honestly.
            for text in texts:
                rows.append({"title": "", "url": "", "site": "", "snippet": text[:800], "icon": ""})
        rows = [row for row in rows if row["title"] or row["snippet"]]
        return rows[:count]

    @staticmethod
    def _row(item: dict) -> dict:
        def pick(*keys, limit=200):
            return next((_text(item[key], limit) for key in keys if _text(item.get(key), 1)), "")
        url = next((_http_url(item[key]) for key in ("url", "link", "webpage_url", "webpageUrl")
                    if _http_url(item.get(key))), "")
        return {"title": pick("title", "name", "title_text"),
                "url": url,
                "site": pick("site_name", "siteName", "site", "source", "media", "website", limit=120),
                "snippet": pick("snippet", "content", "description", "summary", limit=800),
                "icon": next((_http_url(item[key]) for key in ("icon", "iconUrl", "favicon", "icon_url", "favicon_url")
                              if _http_url(item.get(key))), "")}
