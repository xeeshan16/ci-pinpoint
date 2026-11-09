from __future__ import annotations

import json
import random

import httpx
import pytest

from ci_pinpoint.config import ConfigError, Settings
from ci_pinpoint.providers import (
    Message,
    ProviderError,
    build_chat,
    build_embedder,
    resolve_embedder,
    resolve_provider,
)
from ci_pinpoint.providers.anthropic import AnthropicChat
from ci_pinpoint.providers.fake import FakeChat, FakeEmbedder
from ci_pinpoint.providers.http import JsonHttpClient, RetryPolicy, parse_retry_after
from ci_pinpoint.providers.openai_compat import (
    AzureOpenAIChat,
    OpenAICompatibleChat,
    OpenAICompatibleEmbedder,
)

MSGS = [Message("system", "be brief"), Message("user", "hi")]


def _openai_body(text: str = '{"ok": true}') -> dict[str, object]:
    return {
        "model": "m-1",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }


def _client(handler, sleeps: list[float] | None = None) -> JsonHttpClient:  # type: ignore[no-untyped-def]
    return JsonHttpClient(
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(max_retries=2, base_delay_s=0.01),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
        rng=random.Random(0),
    )


def test_openai_compatible_request_and_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_openai_body())

    chat = OpenAICompatibleChat(
        base_url="http://localhost:11434/v1/",
        model="qwen2.5:3b",
        provider="ollama",
        http=_client(handler),
    )
    completion = chat.complete(MSGS, max_tokens=50, json_mode=True)
    assert completion.text == '{"ok": true}'
    assert completion.usage.input_tokens == 12
    assert completion.usage.output_tokens == 3
    assert completion.model == "m-1"
    request = seen[0]
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    body = json.loads(request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert "authorization" not in request.headers


def test_openai_sends_bearer_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json=_openai_body())

    chat = OpenAICompatibleChat(
        base_url="https://api.openai.com/v1",
        model="x",
        api_key="sk-test",
        provider="openai",
        http=_client(handler),
    )
    assert chat.complete(MSGS).text


def test_azure_url_headers_and_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/deployments/diag/chat/completions"
        assert request.url.params["api-version"] == "2024-10-21"
        assert request.headers["api-key"] == "az-key"
        return httpx.Response(200, json=_openai_body())

    chat = AzureOpenAIChat(
        endpoint="https://x.openai.azure.com/",
        deployment="diag",
        api_key="az-key",
        api_version="2024-10-21",
        http=_client(handler),
    )
    assert chat.provider == "azure"
    assert chat.complete(MSGS).text


def test_anthropic_request_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "ak"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["system"].startswith("be brief")
        assert "single JSON object" in body["system"]
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        return httpx.Response(
            200,
            json={
                "model": "claude-x",
                "content": [{"type": "text", "text": "{}"}, {"type": "tool_use"}],
                "usage": {"input_tokens": 7, "output_tokens": 2},
                "stop_reason": "end_turn",
            },
        )

    completion = AnthropicChat(api_key="ak", model="claude-x", http=_client(handler)).complete(
        MSGS, json_mode=True
    )
    assert completion.text == "{}"
    assert completion.usage.output_tokens == 2
    assert completion.finish_reason == "end_turn"


def test_anthropic_without_content_is_an_error() -> None:
    chat = AnthropicChat(
        api_key="ak", model="m", http=_client(lambda r: httpx.Response(200, json={}))
    )
    with pytest.raises(ProviderError, match="no content"):
        chat.complete(MSGS)


def test_retries_on_429_and_honours_retry_after() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"retry-after": "2"}, json={"error": {"message": "slow"}}
            )
        if calls["n"] == 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_openai_body())

    chat = OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler, sleeps))
    assert chat.complete(MSGS).text
    assert calls["n"] == 3
    assert sleeps[0] >= 2.0
    assert len(sleeps) == 2


def test_does_not_retry_client_errors() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    chat = OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler))
    with pytest.raises(ProviderError, match="HTTP 400: bad model") as info:
        chat.complete(MSGS)
    assert info.value.status == 400
    assert calls["n"] == 1


def test_connection_errors_are_retried_then_raised() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    chat = OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler, sleeps))
    with pytest.raises(ProviderError, match="failed to connect"):
        chat.complete(MSGS)
    assert len(sleeps) == 2


def test_timeouts_are_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError, match="timed out") as info:
        OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler)).complete(
            MSGS
        )
    assert info.value.retryable


def test_malformed_responses() -> None:
    chat = OpenAICompatibleChat(
        base_url="http://x/v1",
        model="m",
        http=_client(lambda r: httpx.Response(200, json={"choices": []})),
    )
    with pytest.raises(ProviderError, match="no choices"):
        chat.complete(MSGS)
    chat = OpenAICompatibleChat(
        base_url="http://x/v1",
        model="m",
        http=_client(lambda r: httpx.Response(200, text="<html>")),
    )
    with pytest.raises(ProviderError, match="non-JSON"):
        chat.complete(MSGS)


def test_parse_retry_after() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert parse_retry_after("soon") is None


def test_embedder_batches_and_orders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(inputs)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    emb = OpenAICompatibleEmbedder(
        base_url="http://x/v1", model="e", http=_client(handler), batch_size=2
    )
    assert emb.embed(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert emb.model == "e"


def test_embedder_count_mismatch() -> None:
    emb = OpenAICompatibleEmbedder(
        base_url="http://x/v1",
        model="e",
        http=_client(lambda r: httpx.Response(200, json={"data": []})),
    )
    with pytest.raises(ProviderError, match="0 vectors for 1"):
        emb.embed(["a"])


def test_fake_embedder_is_deterministic_and_normalised() -> None:
    emb = FakeEmbedder(dimensions=32)
    a, b, c = emb.embed(["connection refused", "connection refused", "assertion failed"])
    assert a == b
    assert a != c
    assert sum(x * x for x in a) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}, "anthropic"),
        ({"AZURE_OPENAI_ENDPOINT": "https://x"}, "azure"),
        ({"OPENAI_API_KEY": "o"}, "openai"),
        ({}, "ollama"),
    ],
)
def test_auto_provider_resolution(env: dict[str, str], expected: str) -> None:
    assert resolve_provider(Settings(), env) == expected


def test_base_url_selects_openai_compatible() -> None:
    assert resolve_provider(Settings(base_url="http://vllm:8000/v1"), {}) == "openai-compatible"


def test_build_chat_variants() -> None:
    ollama = build_chat(Settings(), {"OLLAMA_HOST": "gpu-box:11434"})
    assert (ollama.provider, ollama.model) == ("ollama", "qwen2.5:3b")
    assert isinstance(build_chat(Settings(provider="fake"), {}), FakeChat)
    anthropic = build_chat(Settings(), {"ANTHROPIC_API_KEY": "a"})
    assert anthropic.provider == "anthropic"
    openai = build_chat(Settings(model="gpt-x"), {"OPENAI_API_KEY": "o"})
    assert (openai.provider, openai.model) == ("openai", "gpt-x")
    azure = build_chat(
        Settings(),
        {
            "AZURE_OPENAI_ENDPOINT": "https://x",
            "AZURE_OPENAI_API_KEY": "k",
            "AZURE_OPENAI_DEPLOYMENT": "d",
        },
    )
    assert (azure.provider, azure.model) == ("azure", "d")
    vllm = build_chat(Settings(base_url="http://vllm:8000/v1", model="qwen"), {})
    assert vllm.provider == "openai-compatible"


def test_build_chat_reports_missing_configuration() -> None:
    with pytest.raises(ConfigError, match="AZURE_OPENAI_API_KEY"):
        build_chat(
            Settings(provider="azure"),
            {"AZURE_OPENAI_ENDPOINT": "https://x", "AZURE_OPENAI_DEPLOYMENT": "d"},
        )
    with pytest.raises(ConfigError, match="CI_PINPOINT_BASE_URL"):
        build_chat(Settings(provider="openai-compatible", model="m"), {})
    with pytest.raises(ConfigError, match="CI_PINPOINT_MODEL"):
        build_chat(Settings(provider="openai-compatible", base_url="http://x"), {})
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        build_chat(Settings(provider="openai"), {})


def test_build_embedder_variants() -> None:
    ollama = build_embedder(Settings(), {})
    assert ollama is not None
    assert ollama.model == "nomic-embed-text"
    assert isinstance(build_embedder(Settings(embedder="fake"), {}), FakeEmbedder)
    openai = build_embedder(Settings(embedder="openai"), {"OPENAI_API_KEY": "o"})
    assert openai is not None
    assert openai.model == "text-embedding-3-small"
    custom = build_embedder(
        Settings(embedder="openai-compatible", embed_model="bge"),
        {"CI_PINPOINT_EMBED_BASE_URL": "http://x/v1"},
    )
    assert custom is not None
    assert custom.model == "bge"
    assert build_embedder(Settings(embedder="none"), {}) is None
    with pytest.raises(ConfigError, match="CI_PINPOINT_EMBED_MODEL"):
        build_embedder(Settings(embedder="openai-compatible"), {"CI_PINPOINT_EMBED_BASE_URL": "x"})


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "ollama"),
        ({"OPENAI_API_KEY": "o"}, "openai"),
        ({"ANTHROPIC_API_KEY": "a"}, "none"),
        ({"AZURE_OPENAI_ENDPOINT": "https://x"}, "none"),
    ],
)
def test_auto_embedder_follows_the_chat_provider(env: dict[str, str], expected: str) -> None:
    assert resolve_embedder(Settings(), env) == expected


def test_auto_embedder_does_not_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    emb = build_embedder(Settings(), {}, transport=httpx.MockTransport(handler))
    assert emb is not None
    with pytest.raises(ProviderError):
        emb.embed(["x"])
    assert calls["n"] == 1


SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def test_json_schema_is_sent_as_structured_output() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_openai_body())

    chat = OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler))
    chat.complete(MSGS, json_mode=True, json_schema=SCHEMA)
    response_format = seen[0]["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == SCHEMA


def test_rejected_json_schema_falls_back_to_json_mode_once() -> None:
    formats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = json.loads(request.content)["response_format"]["type"]
        formats.append(kind)
        if kind == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return httpx.Response(200, json=_openai_body())

    chat = OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler))
    chat.complete(MSGS, json_schema=SCHEMA)
    chat.complete(MSGS, json_schema=SCHEMA)
    assert formats == ["json_schema", "json_object", "json_object"]


def test_other_errors_do_not_trigger_the_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    chat = OpenAICompatibleChat(base_url="http://x/v1", model="m", http=_client(handler))
    with pytest.raises(ProviderError, match="401"):
        chat.complete(MSGS, json_schema=SCHEMA)
