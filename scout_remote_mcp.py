"""Remote MCP (streamable HTTP, JSON replies) for Claude, ChatGPT and other MCP clients.

Mounted by scout_server.py at /mcp and protected by Kestrel OAuth bearer tokens. Each
request runs against the same ROUTES table as the REST API with a verified tenant identity.
"""
import json
import os

from scout_core import ScoutError, CALLER
from scout_mcp import TOOLS, REQUIRED, GATEWAY_NOTE, SETUP_NOTE, UI, catalog, tool_list, filter_catalog, ui_meta
import scout_showcase

MAX_CHARS = 60000

# Signed-in users get only tools that run on their own Instagram token and their own database.
# Everything else spends the operator's API quota, server or accounts. Default is closed:
# a new route is unavailable to users until it is added here.
TENANT_ALLOWED_ROUTES = {
    "/v1/me/ig", "/v1/me/ig/disconnect", "/v1/me/postmortem", "/v1/me/besttime",
    "/v1/me/profile", "/v1/me/profile/set",
    "/v1/watchlist", "/v1/watchlist/add", "/v1/watchlist/remove",
}


USER_NOTE = ("These tools read the signed-in user's own Instagram account: insights, post reviews, best posting time, "
             "profile and a saved creator list. Research tools are not available on this connection.")


def _catalog_for_caller():
    rows = catalog()
    if not getattr(CALLER, "user_id", None):
        return rows
    return [row for row in rows if row["route"] in TENANT_ALLOWED_ROUTES]


PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _result(i, result):
    return {"jsonrpc": "2.0", "id": i, "result": result}


def _error(i, code, message):
    return {"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": message}}


def handle(scout, routes, conv, msg):
    """One JSON-RPC message in, one reply dict out (None for notifications)."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request")
    method, i = msg.get("method"), msg.get("id")
    if i is None:  # notification such as notifications/initialized
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return _result(i, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                           "capabilities": {"tools": {"listChanged": True}, "resources": {},
                                            "extensions": {"io.modelcontextprotocol/ui": {}}},
                           "serverInfo": {"name": "kestrel", "version": "1.4.0"},
                           "instructions": USER_NOTE if getattr(CALLER, "user_id", None) else SETUP_NOTE + GATEWAY_NOTE})
    if method == "ping":
        return _result(i, {})
    if method == "resources/list":
        return _result(i, {"resources": [{"uri": scout_showcase.APP_URI, "name": "Kestrel creator breakdown",
                                          "mimeType": scout_showcase.APP_MIME} ]})
    if method == "resources/templates/list":
        return _result(i, {"resourceTemplates": []})
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri")
        if uri != scout_showcase.APP_URI:
            return _error(i, -32002, "resource not found")
        page = scout_showcase.app_page(cta=os.environ.get("SCOUT_SHOWCASE_CTA", ""))
        return _result(i, {"contents": [{"uri": uri, "mimeType": scout_showcase.APP_MIME, "text": page,
                                         "_meta": {"ui": {"csp": scout_showcase.APP_CSP, "prefersBorder": False}}}]})
    if method == "tools/list":
        return _result(i, {"tools": tool_list(_catalog_for_caller())})
    if method == "tools/call":
        p = msg.get("params") or {}
        name, raw = p.get("name"), p.get("arguments") or {}
        if name == "kestrel_tools":
            return _result(i, {"content": [{"type": "text", "text": json.dumps({"tools": filter_catalog(_catalog_for_caller(), raw.get("search")),
                                                                                "note": GATEWAY_NOTE})}], "isError": False})
        if name == "kestrel_call":
            name, raw = raw.get("tool"), raw.get("args") or {}
        tool = {t[0]: t for t in TOOLS}.get(name)
        if not tool:
            return _result(i, {"content": [{"type": "text", "text": json.dumps({"error": "no Kestrel tool called %s. Call kestrel_tools for the list." % name})}],
                               "isError": True})
        if getattr(CALLER, "user_id", None) and tool[1] not in TENANT_ALLOWED_ROUTES:
            return _result(i, {"content": [{"type": "text", "text": json.dumps({
                "error": "This tool uses the server operator's quota or accounts and is not available to signed-in users."
            })}], "isError": True})
        _, want, spec = routes[tool[1]]
        method_name = routes[tool[1]][0]
        args = {k: v for k, v in raw.items() if v not in (None, "")}
        if name == "scout_creator_breakdown":
            args["user"] = str(args.get("user", "")).strip().lstrip("@")
        data = None
        try:
            kwargs = {}
            for k, v in args.items():
                if k not in spec:
                    raise ScoutError("unknown parameter: " + k, 400)
                kwargs[k] = conv(spec[k], v)
            data = getattr(scout, method_name)(**kwargs)
            text, bad = json.dumps(data, ensure_ascii=False), False
        except ScoutError as e:
            text, bad = json.dumps({"error": str(e)}), True
        except (ValueError, TypeError) as e:
            text, bad = json.dumps({"error": "missing or bad parameters: " + str(e)}), True
        except Exception:
            text, bad = json.dumps({"error": "internal error"}), True
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n...truncated. Narrow the request (limit, kind, max_age_days)."
        out = {"content": [{"type": "text", "text": text}], "isError": bad}
        if ui_meta(name):
            out["_meta"] = ui_meta(name)
            if isinstance(data, dict):
                out["structuredContent"] = data
        return _result(i, out)
    return _error(i, -32601, "method not found")
