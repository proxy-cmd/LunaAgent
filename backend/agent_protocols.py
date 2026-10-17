"""Provider request and response adapters for the agent runtime."""

from __future__ import annotations

import json


def response_tools(chat_tools: list[dict]) -> list[dict]:
    """Convert Chat Completions function specs to Responses function specs."""
    converted = []
    for item in chat_tools:
        function = item.get("function", {})
        converted.append({
            "type": "function",
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object"}),
            "strict": False,
        })
    return converted


def response_input(messages: list[dict]) -> list[dict]:
    """Convert the shared conversation history to Responses input items."""
    items = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id", ""),
                "output": message.get("content", ""),
            })
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            continue
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            items.append({
                "type": "function_call",
                "call_id": call.get("id", ""),
                "name": function.get("name", ""),
                "arguments": function.get("arguments", "{}"),
            })
        content = message.get("content")
        if content:
            items.append({"role": role, "content": content})
    return items


def response_body(model: str, messages: list[dict], tools: list[dict]) -> dict:
    return {
        "model": model,
        "input": response_input(messages),
        "tools": response_tools(tools),
        "tool_choice": "auto",
        "max_output_tokens": 4096,
    }


def chat_result(data: dict) -> dict:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Chat Completions returned no choices.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("Chat Completions returned an invalid message.")
    if message.get("refusal"):
        raise PermissionError("The model declined this request.")
    return {
        "model": data.get("model"),
        "message": message,
        "usage": data.get("usage"),
        "id": data.get("id"),
    }


def responses_result(data: dict) -> dict:
    if data.get("status") == "incomplete" or data.get("incomplete_details"):
        raise ValueError("Responses API returned an incomplete response.")
    if data.get("error"):
        raise ValueError("Responses API returned an error.")
    output = data.get("output")
    if not isinstance(output, list):
        raise ValueError("Responses API returned no output items.")
    text = data.get("output_text")
    calls = []
    refusal = False
    if not isinstance(text, str):
        text_parts = []
        for item in output:
            kind = item.get("type")
            if kind == "function_call":
                calls.append({
                    "id": item.get("call_id") or item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })
            elif kind == "refusal":
                refusal = True
            elif kind == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "refusal":
                        refusal = True
        text = "".join(text_parts)
    else:
        for item in output:
            if item.get("type") == "function_call":
                calls.append({
                    "id": item.get("call_id") or item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })
            elif item.get("type") == "refusal":
                refusal = True
    if refusal:
        raise PermissionError("The model declined this request.")
    message = {"role": "assistant", "content": text or None, "tool_calls": calls}
    return {
        "model": data.get("model"),
        "message": message,
        "usage": data.get("usage"),
        "id": data.get("id"),
    }


def parse_tool_arguments(value: str) -> dict:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Tool arguments must be an object.")
    return parsed
