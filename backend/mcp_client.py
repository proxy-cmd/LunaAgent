"""Small, HTTP-only MCP client for explicitly configured Streamable HTTP servers."""

from __future__ import annotations

import asyncio
import ipaddress
import base64
import json
import re
import re
import socket
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from backend import providers

MCP_FILE = providers.DATA_DIR / "mcp.json"
MODERN = "2026-07-28"
LEGACY = "2025-11-25"
CLIENT_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "Luna Desk", "version": "0.2.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}
sessions: dict[str, dict] = {}
SECRET = re.compile(
    r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b|\bBearer\s+[A-Za-z0-9._~-]{16,}|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*['\"]?[^\s,'\";]+"
)


def _clean(value: str) -> str:
    return SECRET.sub("[redacted]", value)


def _read() -> dict:
    if not MCP_FILE.exists():
        return {"servers": []}
    try:
        data = json.loads(MCP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "Could not read MCP server settings.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("servers", []), list):
        raise HTTPException(500, "MCP server settings are invalid.")
    return data


def _write(data: dict) -> None:
    MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = MCP_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(MCP_FILE)


def _url(value: str) -> str:
    parsed = urlparse(value.strip().rstrip("/"))
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise HTTPException(422, "Enter an HTTP(S) MCP endpoint without credentials, query, or fragment.")
    if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(422, "Use HTTPS for remote MCP servers. Plain HTTP is limited to localhost.")
    return value.strip().rstrip("/")


def add_server(name: str, url: str) -> dict:
    name = name.strip()
    if not name or len(name) > 80:
        raise HTTPException(422, "Server name must contain 1 to 80 characters.")
    endpoint = _url(url)
    data = _read()
    if any(item["url"] == endpoint for item in data["servers"]):
        raise HTTPException(409, "This MCP endpoint is already configured.")
    server = {"id": uuid.uuid4().hex, "name": name, "url": endpoint,
              "enabled": False, "protocol": None, "tools": [], "last_error": None}
    data["servers"].append(server)
    _write(data)
    return _public(server)


def _public(server: dict) -> dict:
    result = {key: server.get(key) for key in ("id", "name", "url", "enabled", "protocol", "tools", "last_error")}
    if result["last_error"]:
        result["last_error"] = _clean(result["last_error"])
    for tool in result.get("tools") or []:
        tool["description"] = _clean(tool.get("description", ""))
    return result | {
        "connected": server["id"] in sessions}


def list_servers() -> list[dict]:
    return [_public(item) for item in _read()["servers"]]


def _server(server_id: str) -> dict:
    item = next((item for item in _read()["servers"] if item["id"] == server_id), None)
    if not item:
        raise HTTPException(404, "MCP server not found.")
    return item


def _check_address(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise HTTPException(502, "Could not resolve the MCP server host.") from exc
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise HTTPException(422, "MCP endpoints must resolve to public addresses or explicit localhost.")


def _rpc_response(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(f"MCP server returned HTTP {response.status_code}.")
    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = []
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    messages.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    continue
        message = next((item for item in reversed(messages) if "id" in item),
                       messages[-1] if messages else {})
    else:
        try:
            message = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError("MCP server returned invalid JSON.") from exc
    if message.get("error"):
        error = message["error"]
        raise RuntimeError("MCP request failed (" + str(error.get("code", "unknown")) + "): "
                           + str(error.get("message", "protocol error"))[:200])
    result = message.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("MCP server returned no result object.")
    return result


async def _post(client: httpx.AsyncClient, server: dict, method: str,
                params: dict, protocol: str, request_id: int,
                name: str | None = None, session_id: str | None = None,
                extra_headers: dict | None = None) -> tuple[dict, str | None]:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if protocol == MODERN:
        params["_meta"] = dict(CLIENT_META)
        headers.update({"MCP-Protocol-Version": MODERN, "Mcp-Method": method})
        if name is not None:
            headers["Mcp-Name"] = name
    elif protocol == LEGACY:
        headers["MCP-Protocol-Version"] = LEGACY
        if session_id:
            headers["Mcp-Session-Id"] = session_id
    if extra_headers:
        headers.update(extra_headers)
    response = await client.post(server["url"], headers=headers, json=body)
    result = _rpc_response(response)
    return result, response.headers.get("Mcp-Session-Id")


def _valid_tools(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 200:
        raise RuntimeError("MCP server returned an invalid tool list.")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("MCP tool entry must be an object.")
        name = item.get("name")
        schema = item.get("inputSchema", {"type": "object", "properties": {}})
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
                or name in seen or not isinstance(schema, dict) or schema.get("type") != "object"
                or not isinstance(schema.get("properties", {}), dict)):
            continue
        description = item.get("description", "")
        if not isinstance(description, str):
            description = ""
        try:
            header_paths(schema)
        except ValueError:
            continue
        result.append({"name": name, "description": _clean(description[:1000]), "inputSchema": schema})
        seen.add(name)
    return result


def header_paths(schema: dict) -> list[tuple[tuple[str, ...], str, str]]:
    found = []

    def walk(node: dict, prefix: tuple[str, ...]):
        if not isinstance(node, dict):
            return
        for key, spec in node.get("properties", {}).items():
            if not isinstance(spec, dict):
                continue
            header = spec.get("x-mcp-header")
            if header is not None:
                if (not isinstance(header, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header)
                        or spec.get("type") not in {"string", "integer", "boolean"}):
                    raise ValueError("Invalid MCP header annotation.")
                found.append((prefix + (key,), header, spec["type"]))
            walk(spec, prefix + (key,))

    walk(schema, ())
    names = [item[1].casefold() for item in found]
    if len(names) != len(set(names)):
        raise ValueError("MCP header names must be unique.")
    return found


def _header_value(value) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if (text != text.strip() or any(ord(char) < 32 or ord(char) > 126 for char in text)
            or (text.startswith("=?base64?") and text.endswith("?="))):
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return "=?base64?" + encoded + "?="
    return text


async def _discover(server: dict) -> tuple[str, list[dict], str | None]:
    _check_address(server["url"])
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        try:
            result, _ = await _post(client, server, "server/discover",
                                    {"_meta": dict(CLIENT_META)}, MODERN, 1)
            protocol = result.get("protocolVersion", MODERN)
            if protocol != MODERN:
                raise RuntimeError("MCP server negotiated an unsupported protocol version.")
            tools, session = await _post(client, server, "tools/list", {}, MODERN, 2)
            return MODERN, _valid_tools(tools.get("tools")), session
        except RuntimeError as exc:
            detail = str(exc)
            old_server = any(value in detail for value in (
                "HTTP 400", "HTTP 404", "HTTP 405", "-32601", "-32022"))
            if not old_server:
                raise
        init, session = await _post(client, server, "initialize", {
            "protocolVersion": LEGACY,
            "capabilities": {},
            "clientInfo": {"name": "Luna Desk", "version": "0.2.0"},
        }, "legacy", 1)
        version = init.get("protocolVersion")
        if version not in {"2025-11-25", "2025-06-18", "2025-03-26"}:
            raise RuntimeError("MCP server negotiated an unsupported legacy version.")
        initialized = await client.post(server["url"], headers={
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "MCP-Protocol-Version": version,
            **({"Mcp-Session-Id": session} if session else {}),
        }, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        if initialized.status_code >= 400:
            raise RuntimeError("MCP server rejected the initialized notification.")
        tools, session = await _post(client, server, "tools/list", {}, version, 2,
                                     session_id=session)
        return version, _valid_tools(tools.get("tools")), session


async def connect_server(server_id: str) -> dict:
    data = _read()
    server = next((item for item in data["servers"] if item["id"] == server_id), None)
    if not server:
        raise HTTPException(404, "MCP server not found.")
    try:
        protocol, tool_list, session = await _discover(server)
    except (httpx.RequestError, RuntimeError, ValueError) as exc:
        server["last_error"] = _clean(str(exc)[:300])
        server["tools"] = []
        sessions.pop(server_id, None)
        _write(data)
        raise HTTPException(502, server["last_error"]) from exc
    server["protocol"] = protocol
    server["tools"] = tool_list
    server["last_error"] = None
    sessions[server_id] = {"protocol": protocol, "session_id": session}
    _write(data)
    return _public(server)


def set_enabled(server_id: str, enabled: bool) -> dict:
    data = _read()
    server = next((item for item in data["servers"] if item["id"] == server_id), None)
    if not server:
        raise HTTPException(404, "MCP server not found.")
    if enabled and server_id not in sessions:
        raise HTTPException(409, "Connect to the server and inspect its tools before enabling it.")
    server["enabled"] = enabled
    _write(data)
    return _public(server)


def disconnect(server_id: str) -> dict:
    server = _server(server_id)
    sessions.pop(server_id, None)
    return _public(server)


def remove_server(server_id: str) -> None:
    data = _read()
    if not any(item["id"] == server_id for item in data["servers"]):
        raise HTTPException(404, "MCP server not found.")
    data["servers"] = [item for item in data["servers"] if item["id"] != server_id]
    sessions.pop(server_id, None)
    _write(data)


def _aliases() -> list[tuple[str, dict, dict]]:
    entries = []
    for server in _read()["servers"]:
        if not server.get("enabled") or server["id"] not in sessions:
            continue
        for index, tool in enumerate(server.get("tools", [])):
            entries.append((f"mcp_{server['id'][:8]}_{index}", server, tool))
    return entries


def tool_specs() -> list[dict]:
    result = []
    for alias, server, tool in _aliases():
        description = f"MCP server {server['name']}, tool {tool['name']}: {tool['description']}"
        result.append({"type": "function", "function": {
            "name": alias, "description": description[:1500],
            "parameters": tool["inputSchema"],
        }})
    return result


def _validate_args(schema: dict, args: dict) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(args, dict) or any(name not in args for name in required):
        raise ValueError("MCP tool arguments do not satisfy required fields.")
    if schema.get("additionalProperties") is False and any(name not in properties for name in args):
        raise ValueError("MCP tool arguments contain unknown fields.")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "object": dict, "array": list}
    for name, value in args.items():
        spec = properties.get(name, {})
        expected = types.get(spec.get("type")) if isinstance(spec, dict) else None
        if expected and (not isinstance(value, expected) or
                         (spec.get("type") == "integer" and isinstance(value, bool))):
            raise ValueError(f"MCP argument {name!r} has the wrong type.")


async def call_tool(alias: str, args: dict, cancel_event=None) -> str:
    entry = next((item for item in _aliases() if item[0] == alias), None)
    if not entry:
        raise ValueError("MCP tool is unavailable or disabled.")
    _, server, tool = entry
    _validate_args(tool["inputSchema"], args)
    session = sessions[server["id"]]
    protocol = session["protocol"]
    params = {"name": tool["name"], "arguments": args}
    extra_headers = {}
    if protocol == MODERN:
        for path, header, _ in header_paths(tool["inputSchema"]):
            value = args
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if value is not None:
                extra_headers["Mcp-Param-" + header] = _header_value(value)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        request = asyncio.create_task(_post(client, server, "tools/call", params, protocol, 99,
            name=tool["name"], session_id=session.get("session_id"), extra_headers=extra_headers))
        cancelled = asyncio.create_task(cancel_event.wait()) if cancel_event else None
        waiting = {request} | ({cancelled} if cancelled else set())
        done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done and cancel_event and cancel_event.is_set():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            raise asyncio.CancelledError
        if cancelled:
            cancelled.cancel()
        result, _ = request.result()
    if result.get("isError"):
        raise RuntimeError("MCP tool reported an error.")
    content = result.get("content", [])
    text = "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False)
    return _clean(text[:20_000])
