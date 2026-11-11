from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import answering
from jarvis.brain.openai_compat import OpenAICompatBackend, simplify_schema
from jarvis.config import set_secret
from jarvis.tools import TurnState


def sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def text_chunks(*pieces: str) -> list[dict]:
    return [{"choices": [{"delta": {"content": p}}]} for p in pieces]


def tool_chunks(call_id: str, name: str, arguments: str, extra: dict | None = None) -> list[dict]:
    half = len(arguments) // 2
    first = {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments[:half]}} | (extra or {})
    return [
        {"choices": [{"delta": {"role": "assistant", "tool_calls": [first]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]


class Provider:
    """A scripted stand-in for a chat-completions service."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]})
        reply = self.responses.pop(0)
        return reply() if callable(reply) else reply

    def body(self, index: int) -> dict:
        return json.loads(self.requests[index].content)


@pytest.fixture
def make(services, executor):
    services.config.openai.base_url = "https://api.example.test/v1"
    services.config.openai.model = "test-model"
    services.config.openai.api_key_env = "TEST_AI_KEY"
    set_secret(services.paths, "TEST_AI_KEY", "sk-test-123")

    def _make(provider: Provider) -> OpenAICompatBackend:
        return OpenAICompatBackend(services, executor, lambda: "You are Jarvis.", transport=httpx.MockTransport(provider))

    return _make


async def ask(backend, text="hello", turn=None):
    said: list[str] = []
    result = await backend.run_turn(text, turn or TurnState(), said.append)
    return result, said


async def test_a_plain_reply_is_streamed(make):
    provider = Provider(sse(*text_chunks("Good ", "morning", ".")))
    result, said = await ask(make(provider), "hi")
    assert said == ["Good ", "morning", "."] and result.text == "Good morning." and not result.error
    body = provider.body(0)
    assert [m["role"] for m in body["messages"]] == ["system", "user"] and body["model"] == "test-model" and body["stream"] is True
    assert provider.requests[0].headers["authorization"] == "Bearer sk-test-123"
    params = next(t for t in body["tools"] if t["function"]["name"] == "run_command")["function"]["parameters"]
    assert "additionalProperties" not in params and "default" not in params["properties"]["cwd"]   # only widely accepted keywords
    assert params["required"] == ["command"]


async def test_tool_calls_run_through_the_safety_gate_and_feed_back(make, workspace):
    args = json.dumps({"path": "notes.txt", "content": "hello\n"})
    provider = Provider(sse(*tool_chunks("call_1", "write_file", args)), sse(*text_chunks("Created ", "it.")))
    backend = make(provider)
    result, _ = await ask(backend, "make a note")
    assert (workspace / "notes.txt").read_text() == "hello\n" and result.text == "Created it."
    second = provider.body(1)["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[2]["tool_calls"][0]["function"] == {"name": "write_file", "arguments": args}
    assert second[3]["tool_call_id"] == "call_1" and '"created"' in second[3]["content"]


async def test_a_declined_action_is_reported_to_the_model_not_run(make, workspace, services):
    (workspace / "old.txt").write_text("x")
    provider = Provider(sse(*tool_chunks("c1", "delete_path", json.dumps({"path": "old.txt"}))), sse(*text_chunks("Left it alone.")))
    async with answering(services, False):
        result, _ = await ask(make(provider), "delete old.txt")
    assert (workspace / "old.txt").exists() and "declined" in provider.body(1)["messages"][-1]["content"]


async def test_provider_specific_fields_on_tool_calls_are_sent_back_untouched(make):
    extra = {"extra_content": {"google": {"thought_signature": "sig-abc"}}}
    provider = Provider(sse(*tool_chunks("c1", "system_status", "{}", extra)), sse(*text_chunks("Fine.")))
    await ask(make(provider))
    assert provider.body(1)["messages"][2]["tool_calls"][0]["extra_content"] == extra["extra_content"]


async def test_malformed_tool_arguments_are_returned_to_the_model(make):
    provider = Provider(sse(*tool_chunks("c1", "read_file", '{"path": "a.txt"')), sse(*text_chunks("Sorry.")))
    result, _ = await ask(make(provider))
    assert "not valid JSON" in provider.body(1)["messages"][-1]["content"] and result.text == "Sorry."


@pytest.mark.parametrize(("status", "fragment"), [(401, "API key"), (404, "model"), (429, "usage limit"), (500, "trouble")])
async def test_provider_errors_are_explained(make, status, fragment):
    # a second identical response covers the single automatic retry of server errors
    reply = httpx.Response(status, json={"error": {"message": "nope"}})
    result, _ = await ask(make(Provider(reply, reply)))
    assert result.error and fragment in result.error and not result.text


async def test_a_short_rate_limit_is_waited_out(make):
    provider = Provider(httpx.Response(429, headers={"retry-after": "0.5"}, json={"error": {"message": "slow down"}}), sse(*text_chunks("Done.")))
    result, _ = await ask(make(provider))
    assert result.text == "Done." and len(provider.requests) == 2


async def test_missing_setup_gives_a_helpful_message(services, executor):
    backend = OpenAICompatBackend(services, executor, lambda: "")
    assert "choose a provider" in (await ask(backend))[0].error
    services.config.openai.base_url = "https://api.example.test/v1"
    assert "pick one" in (await ask(backend))[0].error
    services.config.openai.model = "m"
    services.config.openai.api_key_env = "NO_SUCH_KEY_SET"
    assert "API key" in (await ask(backend))[0].error


async def test_answers_that_arrive_in_one_piece_are_accepted(make):
    whole = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "All at once."}}], "usage": {"total_tokens": 9}})
    result, said = await ask(make(Provider(whole)))
    assert result.text == "All at once." and said == ["All at once."] and result.usage == {"total_tokens": 9}


async def test_interrupting_stops_the_tool_and_leaves_a_valid_history(make, services):
    services.config.safety.command_allowlist = ["sleep"]
    provider = Provider(sse(*tool_chunks("c1", "run_command", json.dumps({"command": "sleep 60"}))), sse(*text_chunks("Next.")))
    backend = make(provider)
    task = asyncio.create_task(ask(backend, "wait a while"))
    await asyncio.sleep(0.6)
    await backend.interrupt()
    result, _ = await asyncio.wait_for(task, 5)
    assert result.interrupted
    roles = [m["role"] for m in backend._messages]
    assert roles == ["system", "user", "assistant", "tool"] and "Interrupted" in backend._messages[-1]["content"]
    result, _ = await ask(backend, "carry on")               # the conversation continues normally afterwards
    assert result.text == "Next."


async def test_old_exchanges_are_dropped_to_fit_small_limits(make, services):
    services.config.openai.context_char_budget = 4000
    provider = Provider(*[sse(*text_chunks("x" * 900)) for _ in range(8)])
    backend = make(provider)
    for i in range(8):
        await ask(backend, f"question {i} " + "y" * 600)
    assert backend._messages[0]["role"] == "system"
    assert sum(len(json.dumps(m)) for m in backend._messages) <= 4000 + 1700
    assert "question 7" in backend._messages[-2]["content"] and "question 0" not in json.dumps(backend._messages)


async def test_models_can_be_listed(make):
    assert await make(Provider()).list_models() == ["model-a", "model-b"]


def test_schema_simplification_keeps_structure():
    schema = {"type": "object", "additionalProperties": False, "properties": {"n": {"type": "integer", "minimum": 1, "default": 3, "description": "count"},
              "tags": {"type": "array", "items": {"type": "string", "maxLength": 5}}}, "required": ["n"]}
    assert simplify_schema(schema) == {"type": "object", "properties": {"n": {"type": "integer", "description": "count"},
                                       "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["n"]}
