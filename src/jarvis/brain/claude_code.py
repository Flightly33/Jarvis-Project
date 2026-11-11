"""Reasoning through Claude Code's programmatic mode (the Claude Agent SDK).

This is what lets Jarvis think with a Claude *subscription* rather than a
pay-per-use API key: the SDK drives the unmodified Claude Code program, which
signs in with the user's own Claude account. Jarvis never sees or stores those
credentials.

Claude Code's own file and shell tools are switched off. The model gets
Jarvis's tools instead (served in-process over MCP), so every action passes
through the same safety gate as with any other backend. The only built-in
tools left on are web search and web fetch, and those are gated here too.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import warnings
from typing import Any

from jarvis import __version__
from jarvis.brain.base import AgentBackend, BackendUnavailable, PromptSource, TextSink, TurnResult
from jarvis.safety import Assessment
from jarvis.tools import Services, ToolError, ToolExecutor, TurnState
from jarvis.tools.web import ensure_public

log = logging.getLogger(__name__)

_SERVER = "jarvis"
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_ERRORS = {
    "authentication_failed": "Your Claude sign-in has expired or is missing. Sign in again with your Claude account, then repeat your request.",
    "billing_error": "Claude reported a billing problem with the account. Nothing was charged by Jarvis; check your Claude plan.",
    "rate_limit": "Your Claude plan's usage limit has been reached. Jarvis will work again when the limit resets.",
    "invalid_request": "Claude rejected the request as invalid. Starting a new conversation usually clears this.",
    "server_error": "Claude's servers had a problem. Please try again in a moment.",
    "max_output_tokens": "The reply was cut off because it became too long.",
}


class ClaudeCodeBackend(AgentBackend):
    name = "claude"

    def __init__(self, services: Services, executor: ToolExecutor, system_prompt: PromptSource) -> None:
        super().__init__(services, executor, system_prompt)
        self._sdk: Any = None
        self._client: Any = None
        self._turn: TurnState | None = None
        self._busy = False
        self._interrupted = False
        self._web_calls: dict[str, tuple[str, str, float]] = {}
        self._info: dict[str, Any] = {}
        self._plan: dict[str, Any] = {}
        self._auth_problem: str | None = None

    @property
    def label(self) -> str:
        cfg = self.services.config.claude
        how = "your Claude plan" if cfg.auth == "subscription" else "an Anthropic API key (pay-per-use)"
        return f"Claude ({self._info.get('model') or cfg.model}) on {how}"

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:
            raise BackendUnavailable(
                "The Claude backend needs the Claude Agent SDK: pip install \"jarvis-assistant[claude]\""
            ) from exc
        self._sdk = sdk
        warnings.filterwarnings("ignore", category=sdk.CanUseToolShadowedWarning)
        if self.services.config.claude.auth == "subscription":
            # An API key in the environment would silently switch Claude Code to pay-per-use billing.
            for var in _API_KEY_VARS:
                if os.environ.pop(var, None) is not None:
                    log.warning("%s was set; ignoring it so that usage stays on the Claude subscription", var)
                    self.services.bus.emit(
                        "notice", level="info",
                        text=f"{var} was set in the environment. Jarvis ignored it so usage stays on your Claude plan.",
                    )

    def _build_options(self) -> Any:
        sdk, cfg = self._sdk, self.services.config.claude
        tools = [self._wrap(tool) for tool in self.executor.registry]
        server = sdk.create_sdk_mcp_server(name=_SERVER, version=__version__, tools=tools)
        self.services.paths.claude_workspace.mkdir(parents=True, exist_ok=True)
        return sdk.ClaudeAgentOptions(
            model=cfg.model,
            system_prompt=self.system_prompt(),
            tools=["WebSearch", "WebFetch"] if cfg.web else [],      # every other built-in tool is off
            mcp_servers={_SERVER: server},
            strict_mcp_config=True,
            allowed_tools=[f"mcp__{_SERVER}__{name}" for name in self.executor.registry.names()],
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            setting_sources=[],                    # do not inherit the user's own Claude Code settings, hooks or MCP servers
            include_partial_messages=True,
            effort=cfg.effort,
            max_turns=cfg.max_turns,
            cwd=str(self.services.paths.claude_workspace),
            verbatim_prompts=True,                 # no @file expansion or slash commands inside what the user said
            cli_path=cfg.cli_path or None,
            stderr=lambda line: log.debug("claude: %s", line.rstrip()),
            env={"CLAUDE_AGENT_SDK_CLIENT_APP": f"jarvis-assistant/{__version__}", "MCP_TOOL_TIMEOUT": "1800000"},
        )

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._sdk is None:
            await self.start()
        client = self._sdk.ClaudeSDKClient(options=self._build_options())
        try:
            await client.connect()
        except self._sdk.CLINotFoundError as exc:
            raise BackendUnavailable("Claude Code could not be started. Reinstall with: pip install -U claude-agent-sdk") from exc
        except self._sdk.ClaudeSDKError as exc:
            raise BackendUnavailable(f"Could not start Claude Code: {exc}") from exc
        self._client = client
        return client

    async def reset(self) -> None:
        client, self._client = self._client, None
        self._web_calls.clear()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - shutting down a child process must not raise into the caller
                log.debug("error while disconnecting Claude Code", exc_info=True)

    async def interrupt(self) -> None:
        if self._client is not None and self._busy:
            self._interrupted = True
            try:
                await self._client.interrupt()
            except Exception:  # noqa: BLE001
                log.debug("interrupt failed", exc_info=True)

    def status(self) -> dict[str, Any]:
        return super().status() | {"model": self._info.get("model"), "plan": self._plan, "auth_problem": self._auth_problem}

    # ------------------------------------------------------------------ tools

    def _wrap(self, tool: Any) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            turn = self._turn or TurnState(source="system")
            outcome = await self.executor.execute(tool.name, args, turn)
            content: list[dict[str, Any]] = [{"type": "text", "text": outcome.text}]
            for image in outcome.images:
                content.append({"type": "image", "data": base64.b64encode(image.data).decode(), "mimeType": image.mime_type})
            return {"content": content, "is_error": outcome.is_error}

        return self._sdk.tool(tool.name, tool.description, tool.schema())(handler)

    async def _can_use_tool(self, name: str, tool_input: dict[str, Any], context: Any) -> Any:
        """Gate for Claude Code's built-in tools. Jarvis's own tools never reach here; they are gated in the executor."""
        sdk = self._sdk
        turn = self._turn or TurnState(source="system")
        if name == "WebSearch":
            title = f"Search the web for '{str(tool_input.get('query', ''))[:80]}'"
        elif name == "WebFetch":
            url = str(tool_input.get("url", ""))
            title = f"Read {url[:120]}"
            try:
                await ensure_public(url)
            except ToolError as exc:
                return sdk.PermissionResultDeny(behavior="deny", message=str(exc), interrupt=False)
        else:
            return sdk.PermissionResultDeny(behavior="deny", message=f"{name} is not available in Jarvis.", interrupt=False)

        assessment = Assessment(title=title, ingests_untrusted=True, sends_external=True, grant_key="web")
        allowed, message = await self.executor.authorize(name, assessment, tool_input, turn)
        if not allowed:
            return sdk.PermissionResultDeny(behavior="deny", message=message, interrupt=False)
        turn.flags.tainted = True      # whatever comes back is outside content
        call_id = getattr(context, "tool_use_id", None) or f"web-{time.monotonic_ns()}"
        self._web_calls[call_id] = (name, title, time.monotonic())
        self.services.bus.emit("tool_start", id=call_id, name=name, title=title, risk="read-only", input=tool_input)
        return sdk.PermissionResultAllow(behavior="allow", updated_input=tool_input)

    # ------------------------------------------------------------------- turn

    async def run_turn(self, prompt: str, turn: TurnState, on_text: TextSink) -> TurnResult:
        sdk = self._sdk
        try:
            client = await self._ensure_client()
        except BackendUnavailable as exc:
            return TurnResult(error=str(exc))
        sdk = self._sdk
        self._turn, self._busy, self._interrupted = turn, True, False
        parts: list[str] = []
        result = TurnResult()
        streamed_this_message = False

        def emit(text: str) -> None:
            if text:
                parts.append(text)
                on_text(text)

        try:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, sdk.StreamEvent):
                    if message.parent_tool_use_id:
                        continue
                    event = message.event
                    kind = event.get("type")
                    if kind == "message_start":
                        streamed_this_message = False
                    elif kind == "content_block_start" and event.get("content_block", {}).get("type") == "text":
                        if parts and not parts[-1].endswith(("\n", " ")):
                            emit("\n\n")       # separate text that came before and after a tool call
                    elif kind == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                        streamed_this_message = True
                        emit(event["delta"].get("text", ""))
                elif isinstance(message, sdk.AssistantMessage):
                    if message.error:
                        result.error = _ERRORS.get(message.error, f"Claude reported an error: {message.error}.")
                        if message.error == "authentication_failed":
                            result.action = "login"
                    elif not streamed_this_message and not message.parent_tool_use_id:
                        for block in message.content:
                            if isinstance(block, sdk.TextBlock):
                                emit(block.text)
                elif isinstance(message, sdk.UserMessage):
                    self._finish_web_calls(message)
                elif isinstance(message, sdk.SystemMessage):
                    if message.subtype == "init":
                        problem = self._check_init(message.data)
                        if problem:
                            result.error = problem
                            await client.interrupt()
                elif isinstance(message, sdk.RateLimitEvent):
                    self._on_rate_limit(message.rate_limit_info)
                elif isinstance(message, sdk.ResultMessage):
                    result.usage = {
                        "duration_ms": message.duration_ms, "steps": message.num_turns,
                        "nominal_cost_usd": message.total_cost_usd, **_tokens(message.usage),
                    }
                    if (message.terminal_reason or "").startswith("aborted") or self._interrupted:
                        result.interrupted = True
                    elif message.subtype == "error_max_turns":
                        emit("\n\nI stopped there: that request needed more steps than I am allowed in one go.")
                    elif message.is_error and not result.error:
                        detail = "; ".join(message.errors or []) or message.result or message.subtype
                        result.error = _status_error(message.api_error_status) or f"The request failed: {detail}"
        except sdk.ClaudeSDKError as exc:
            log.warning("Claude Code connection failed: %s", exc)
            await self.reset()         # the next request starts a clean process
            if not self._interrupted:
                result.error = f"The connection to Claude Code was lost ({type(exc).__name__}). Please try again."
            result.interrupted = self._interrupted
        finally:
            self._busy = False
            self._turn = None
            for call_id, (name, title, _) in list(self._web_calls.items()):
                self.services.bus.emit("tool_end", id=call_id, name=name, title=title, ok=False, summary="Interrupted")
            self._web_calls.clear()
        result.text = "".join(parts).strip()
        if self._auth_problem and not result.error:
            result.error = self._auth_problem
        return result

    def _finish_web_calls(self, message: Any) -> None:
        content = message.content if isinstance(message.content, list) else []
        for block in content:
            if isinstance(block, self._sdk.ToolResultBlock) and block.tool_use_id in self._web_calls:
                name, title, started = self._web_calls.pop(block.tool_use_id)
                self.services.bus.emit(
                    "tool_end", id=block.tool_use_id, name=name, title=title, ok=not block.is_error,
                    summary="failed" if block.is_error else "done", duration_ms=int((time.monotonic() - started) * 1000),
                )

    # --------------------------------------------------- billing safeguards

    def _check_init(self, data: dict[str, Any]) -> str | None:
        self._info = {"model": data.get("model"), "version": data.get("claude_code_version"), "auth": data.get("apiKeySource")}
        cfg = self.services.config.claude
        source = data.get("apiKeySource") or "none"
        if cfg.auth == "subscription" and source != "none":
            self._auth_problem = (
                f"Claude Code is using an API key ({source}) instead of your Claude subscription, which would be "
                "billed per use. Jarvis stopped rather than spend money. Remove the key (or sign in with `claude`), "
                "or set claude.auth = \"api_key\" in the settings if pay-per-use is what you want."
            )
            if cfg.require_subscription:
                return self._auth_problem
            self.services.bus.emit("notice", level="warning", text=self._auth_problem)
        else:
            self._auth_problem = None
        return None

    def _on_rate_limit(self, info: Any) -> None:
        previous = self._plan.get("overage")
        self._plan = {
            "status": info.status, "window": info.rate_limit_type, "utilization": info.utilization,
            "resets_at": info.resets_at, "overage": info.overage_status, "overage_disabled_reason": info.overage_disabled_reason,
        }
        self.services.bus.emit("plan", **self._plan)
        if info.status == "allowed_warning":
            self.services.bus.emit("notice", level="warning", text="You are close to your Claude plan's usage limit.")
        # Usage credits ("extra usage") let requests continue past the plan limit at pay-per-use prices.
        if info.overage_status in ("allowed", "allowed_warning") and previous != info.overage_status:
            self.services.bus.emit(
                "notice", level="warning",
                text="Usage credits are enabled on your Claude account, so requests beyond your plan's limit would be "
                     "billed. Turn them off under Settings > Usage on claude.ai if you never want extra charges.",
            )


def _tokens(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    return {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") if usage.get(k) is not None}


def _status_error(status: int | None) -> str | None:
    if status == 429:
        return _ERRORS["rate_limit"]
    if status in (401, 403):
        return _ERRORS["authentication_failed"]
    if status and status >= 500:
        return _ERRORS["server_error"]
    return None
