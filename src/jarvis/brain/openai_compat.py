"""Reasoning through any OpenAI-compatible chat-completions endpoint.

This one backend covers the free hosted tiers (Groq, Google Gemini, OpenRouter
and others) and models running on this computer (Ollama, LM Studio), because
they all speak the same protocol. Jarvis owns the loop here: it sends the
conversation and the tool list, runs whatever tools the model asks for
through the shared safety gate, and repeats until the model answers in words.

Providers differ in small ways, so the code is deliberately forgiving: it
keeps whatever extra fields a provider attaches to a tool call and sends them
back unchanged, strips schema keywords some providers reject, and reports
errors in plain language.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx

from jarvis.brain.base import AgentBackend, PromptSource, TextSink, TurnResult
from jarvis.config import get_secret
from jarvis.tools import Services, ToolExecutor, TurnState

log = logging.getLogger(__name__)

# Well-known endpoints. Model names change often, so none is assumed: the user picks one from the provider's list.
PRESETS: dict[str, dict[str, str]] = {
    "groq": {"label": "Groq (free tier)", "base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY", "keys_page": "https://console.groq.com/keys"},
    "gemini": {"label": "Google Gemini (free tier)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key_env": "GEMINI_API_KEY", "keys_page": "https://aistudio.google.com/apikey"},
    "openrouter": {"label": "OpenRouter (free models)", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "keys_page": "https://openrouter.ai/keys"},
    "ollama": {"label": "Ollama (runs on this computer)", "base_url": "http://localhost:11434/v1", "api_key_env": "", "keys_page": ""},
}
_SCHEMA_KEYS = {"type", "properties", "required", "description", "enum", "items"}
_MAX_RETRY_WAIT = 30.0


class _ApiError(Exception):
    """A failure talking to the model provider, already phrased for the user."""


def simplify_schema(node: Any) -> Any:
    """Reduce a JSON Schema to the keywords every provider accepts."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: simplify_schema(sub) for name, sub in value.items()}
            elif key in _SCHEMA_KEYS:
                out[key] = simplify_schema(value) if key == "items" else value
        return out
    return node


class OpenAICompatBackend(AgentBackend):
    name = "openai"

    def __init__(self, services: Services, executor: ToolExecutor, system_prompt: PromptSource, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(services, executor, system_prompt)
        self._transport = transport
        self._messages: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._interrupted = False
        self._last_usage: dict[str, Any] = {}

    @property
    def cfg(self):
        return self.services.config.openai

    @property
    def label(self) -> str:
        host = httpx.URL(self.cfg.base_url).host if self.cfg.base_url else "no endpoint set"
        return f"{self.cfg.model or 'no model chosen'} via {host}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.cfg.extra_headers}
        key = get_secret(self.services.paths, self.cfg.api_key_env) if self.cfg.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(self.cfg.request_timeout_seconds, connect=15.0)
        return httpx.AsyncClient(base_url=self.cfg.base_url, headers=self._headers(), timeout=timeout, transport=self._transport)

    def _problem(self) -> str | None:
        if not self.cfg.base_url:
            return "No AI service is set up yet. Open Settings, choose a provider and a model."
        if not self.cfg.model:
            return "No model is chosen yet. Open Settings and pick one from the provider's list."
        if self.cfg.api_key_env and not get_secret(self.services.paths, self.cfg.api_key_env):
            return "This AI service needs an API key. Paste it in Settings; it is stored privately on this computer."
        return None

    async def list_models(self) -> list[str]:
        """Model names the endpoint offers, for the settings panel."""
        async with self._client() as client:
            response = await client.get("/models")
            if response.status_code >= 400:
                raise _ApiError(_explain(response.status_code, response.text))
            data = response.json()
        rows = data.get("data") if isinstance(data, dict) else data
        return sorted(str(row.get("id")) for row in rows or [] if isinstance(row, dict) and row.get("id"))

    # ------------------------------------------------------------------- turn

    async def run_turn(self, prompt: str, turn: TurnState, on_text: TextSink) -> TurnResult:
        problem = self._problem()
        if problem:
            return TurnResult(error=problem)
        if not self._messages:
            self._messages.append({"role": "system", "content": self.system_prompt()})
        self._messages.append({"role": "user", "content": prompt})
        self._task, self._interrupted = asyncio.current_task(), False
        said: list[str] = []
        result = TurnResult()

        def emit(text: str) -> None:
            if text:
                said.append(text)
                on_text(text)

        tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": simplify_schema(t.schema())}}
            for t in self.executor.registry
        ]
        try:
            async with self._client() as client:
                for _ in range(self.services.config.brain.max_steps):
                    if said and not said[-1].endswith(("\n", " ")):
                        emit("\n\n")
                    message = await self._complete(client, tools, emit)
                    self._messages.append(message)
                    calls = message.get("tool_calls") or []
                    if not calls:
                        break
                    await self._run_tools(calls, turn)
                else:
                    emit("\n\nI stopped there: that request needed more steps than I am allowed in one go.")
        except _ApiError as exc:
            result.error = str(exc)
        except httpx.HTTPError as exc:
            result.error = f"Could not reach the AI service ({type(exc).__name__}). Check your internet connection and the address in Settings."
        except asyncio.CancelledError:
            self._repair()
            if not self._interrupted:
                raise
            result.interrupted = True
        finally:
            self._task = None
            self._repair()
            self._trim()
        result.text = "".join(said).strip()
        result.usage = dict(self._last_usage)
        return result

    async def _run_tools(self, calls: list[dict[str, Any]], turn: TurnState) -> None:
        images: list[dict[str, Any]] = []
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name") or ""
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError as exc:
                content = f"The arguments for {name} were not valid JSON ({exc}). Send them again as a JSON object."
            else:
                outcome = await self.executor.execute(name, arguments, turn)
                content = outcome.text
                for image in outcome.images:
                    data = base64.b64encode(image.data).decode()
                    images.append({"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{data}"}})
            self._messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": content})
        if images and self.cfg.supports_vision:
            # tool results cannot carry pictures in this protocol, so they follow as a user message
            self._messages.append({"role": "user", "content": [{"type": "text", "text": "Image(s) returned by the tool above:"}, *images]})

    async def _complete(self, client: httpx.AsyncClient, tools: list[dict[str, Any]], emit: TextSink) -> dict[str, Any]:
        """One model call. Streams text to `emit` and returns the assistant message to keep in the history."""
        body: dict[str, Any] = {
            "model": self.cfg.model, "messages": self._messages, "tools": tools, "tool_choice": "auto",
            "stream": True, "max_tokens": self.cfg.max_output_tokens,
        }
        if self.cfg.include_usage:
            body["stream_options"] = {"include_usage": True}
        for attempt in (1, 2):
            async with client.stream("POST", "/chat/completions", json=body) as response:
                if response.status_code in (429, 500, 502, 503, 529) and attempt == 1:
                    wait = _retry_after(response)
                    if wait is not None and wait <= _MAX_RETRY_WAIT:
                        await response.aread()
                        await asyncio.sleep(wait)
                        continue
                if response.status_code >= 400:
                    raise _ApiError(_explain(response.status_code, (await response.aread()).decode("utf-8", "replace")))
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    # the endpoint ignored the streaming request and answered in one piece
                    data = json.loads(await response.aread())
                    message = dict((data.get("choices") or [{}])[0].get("message") or {})
                    emit(message.get("content") or "")
                    self._last_usage = data.get("usage") or {}
                    return _clean_message(message)
                return await self._read_stream(response, emit)
        raise _ApiError("The AI service is busy. Please try again in a moment.")

    async def _read_stream(self, response: httpx.Response, emit: TextSink) -> dict[str, Any]:
        text: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if chunk.get("error"):
                detail = chunk["error"].get("message") if isinstance(chunk["error"], dict) else chunk["error"]
                raise _ApiError(f"The AI service reported an error: {detail}")
            if chunk.get("usage"):
                self._last_usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                    emit(delta["content"])
                for position, part in enumerate(delta.get("tool_calls") or []):
                    index = part.get("index", position)
                    call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    for key, value in part.items():
                        if key == "function":
                            if value.get("name"):
                                call["function"]["name"] = value["name"]
                            call["function"]["arguments"] += value.get("arguments") or ""
                        elif key == "id":
                            call["id"] = value or call["id"]
                        elif key not in ("index", "type"):
                            call[key] = value          # provider extras (e.g. reasoning signatures) go back untouched
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            ordered = [calls[i] for i in sorted(calls)]
            for number, call in enumerate(ordered):
                call["id"] = call["id"] or f"call_{int(time.time() * 1000)}_{number}"
            message["tool_calls"] = ordered
        return message

    # ---------------------------------------------------------------- history

    def _repair(self) -> None:
        """Keep the history valid after an interruption: every tool call must have a result."""
        answered = {m.get("tool_call_id") for m in self._messages if m.get("role") == "tool"}
        for index in range(len(self._messages) - 1, -1, -1):
            message = self._messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                missing = [c for c in message["tool_calls"] if c.get("id") not in answered]
                insert_at = index + 1
                while insert_at < len(self._messages) and self._messages[insert_at].get("role") == "tool":
                    insert_at += 1
                for call in missing:
                    self._messages.insert(insert_at, {"role": "tool", "tool_call_id": call.get("id"), "content": "Interrupted by the user before this ran."})
                break

    def _trim(self) -> None:
        """Drop the oldest exchanges once the history outgrows its budget (free tiers have small limits)."""
        def size(message: dict[str, Any]) -> int:
            return len(json.dumps(message, default=str))

        budget = self.cfg.context_char_budget
        while sum(size(m) for m in self._messages) > budget:
            # the second user message marks where the oldest exchange ends
            starts = [i for i, m in enumerate(self._messages) if m.get("role") == "user" and i > 0]
            if len(starts) < 2:
                break
            del self._messages[starts[0]:starts[1]]

    async def interrupt(self) -> None:
        if self._task is not None and not self._task.done():
            self._interrupted = True
            self._task.cancel()

    async def reset(self) -> None:
        self._messages.clear()

    def status(self) -> dict[str, Any]:
        return super().status() | {"model": self.cfg.model, "problem": self._problem(), "usage": self._last_usage}


def _clean_message(message: dict[str, Any]) -> dict[str, Any]:
    out = {"role": "assistant", "content": message.get("content")}
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    return out


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.5, float(response.headers.get("retry-after", "")))
    except ValueError:
        return 2.0 if response.status_code != 429 else None


def _explain(status: int, body: str) -> str:
    detail = ""
    try:
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            error = parsed[0].get("error")
        detail = (error.get("message") if isinstance(error, dict) else error) or ""
    except ValueError:
        detail = body[:200]
    detail = f" ({str(detail)[:300]})" if detail else ""
    if status in (401, 403):
        return f"The AI service rejected the API key{detail}. Check the key in Settings."
    if status == 404:
        return f"The AI service does not know that model or address{detail}. Pick a model from the list in Settings."
    if status == 429:
        return f"The AI service's usage limit was reached{detail}. Free tiers allow only a few requests per minute; wait a little, or choose another model or provider."
    if status == 400:
        return f"The AI service could not process the request{detail}. Another model may cope better with tools."
    if status >= 500:
        return f"The AI service is having trouble (error {status}). Please try again shortly."
    return f"The AI service returned error {status}{detail}."
