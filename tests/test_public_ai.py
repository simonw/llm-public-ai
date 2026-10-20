import json

import llm
import pytest
from click.testing import CliRunner
from llm.cli import cli

import llm_public_ai


def test_models_are_registered():
    model_ids = [model.model_id for model in llm.get_models()]
    assert "publicai/swiss-ai/apertus-70b-instruct" in model_ids
    assert "publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT" in model_ids
    async_model = llm.get_async_model("publicai/swiss-ai/apertus-70b-instruct")
    assert isinstance(async_model, llm_public_ai.AsyncPublicAIChat)


def test_model_capabilities():
    text_only = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    assert not text_only.supports_tools
    assert not text_only.supports_schema
    assert not text_only.attachment_types
    vision = llm.get_model("publicai/swiss-ai/apertus-v1.5-70b")
    assert vision.supports_tools
    assert vision.supports_schema
    assert "image/png" in vision.attachment_types
    # Models not in MODEL_CAPABILITIES get the defaults
    default = llm.get_model("publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT")
    assert default.supports_tools
    assert default.supports_schema
    assert not default.attachment_types


def test_model_key_and_headers():
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    assert model.needs_key == "publicai"
    assert model.key_env_var == "PUBLICAI_API_KEY"
    client_kwargs = model.client_kwargs(None)
    assert client_kwargs["base_url"] == "https://api.publicai.co/v1"
    assert client_kwargs["default_headers"]["User-Agent"].startswith("llm-public-ai/")
    assert str(model) == "Public AI: publicai/swiss-ai/apertus-70b-instruct"


def test_thinking_option():
    assert (
        "thinking"
        not in llm.get_model(
            "publicai/swiss-ai/apertus-70b-instruct"
        ).Options.model_fields
    )
    model = llm.get_model("publicai/swiss-ai/apertus-v1.5-70b")
    assert "thinking" in model.Options.model_fields
    for value, expected in ((True, True), (False, False)):
        prompt = llm.Prompt("hi", model=model, options=model.Options(thinking=value))
        kwargs = model.build_kwargs(prompt, [], stream=False)
        assert kwargs["extra_body"] == {
            "cache": {"no-cache": True},
            "chat_template_kwargs": {"enable_thinking": expected},
        }
    # Only the cache bypass is sent if the option is unset
    prompt = llm.Prompt("hi", model=model, options=model.Options())
    assert model.build_kwargs(prompt, [], stream=False)["extra_body"] == {
        "cache": {"no-cache": True}
    }
    # Models without a thinking toggle send neither
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    prompt = llm.Prompt("hi", model=model, options=model.Options())
    assert "extra_body" not in model.build_kwargs(prompt, [], stream=False)


@pytest.mark.parametrize("chunk_size", (1, 2, 3, 5, 100))
def test_inline_thinking_splitter(chunk_size):
    text = "<|inner_prefix|>Think <|inner hard.<|inner_suffix|>Answer is 51 <| ok"
    splitter = llm_public_ai.InlineThinkingSplitter()
    events = []
    for i in range(0, len(text), chunk_size):
        events.extend(splitter.feed(text[i : i + chunk_size]))
    events.extend(splitter.flush())
    reasoning = "".join(e.chunk for e in events if e.type == "reasoning")
    answer = "".join(e.chunk for e in events if e.type == "text")
    assert reasoning == "Think <|inner hard."
    assert answer == "Answer is 51 <| ok"


def test_build_messages_with_history():
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    prompt = llm.Prompt(
        None,
        model=model,
        options=model.Options(),
        messages=[
            llm.system("Be brief"),
            llm.user("Hi"),
            llm.assistant("Hello!"),
            llm.system("Be brief"),
            llm.user("And again"),
        ],
    )
    # The repeated, unchanged system prompt is not sent twice
    assert model.build_messages(prompt) == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "And again"},
    ]


def test_build_messages_with_image():
    model = llm.get_model("publicai/swiss-ai/apertus-v1.5-70b")
    prompt = llm.Prompt(
        "Describe this",
        model=model,
        options=model.Options(),
        attachments=[
            llm.Attachment(url="https://example.com/a.png", type="image/png"),
            llm.Attachment(content=b"fake", type="image/png"),
        ],
    )
    assert model.build_messages(prompt) == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/a.png"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,ZmFrZQ=="},
                },
            ],
        }
    ]


@pytest.mark.vcr
def test_prompt():
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    response = model.prompt("Reply with just the word pelican, lowercase, nothing else")
    assert str(response).strip() == "pelican"
    assert response.input_tokens > 0
    assert response.output_tokens > 0


@pytest.mark.vcr
def test_prompt_no_stream():
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    response = model.prompt(
        "Reply with just the word pelican, lowercase, nothing else", stream=False
    )
    assert str(response).strip() == "pelican"
    assert response.response_json["model"] == "swiss-ai/apertus-70b-instruct"


@pytest.mark.vcr
def test_prompt_streams_reasoning():
    model = llm.get_model("publicai/aisingapore/Nemotron-SEA-LION-v4.8-120B-A12B")
    response = model.prompt("What is 17*3?", thinking=True)
    events = list(response.stream_events())
    reasoning = "".join(e.chunk for e in events if e.type == "reasoning")
    text = "".join(e.chunk for e in events if e.type == "text")
    assert reasoning
    assert "51" in text
    assert response.response_json["reasoning"] == reasoning


@pytest.mark.vcr
def test_apertus_inline_thinking():
    model = llm.get_model("publicai/swiss-ai/apertus-v1.5-70b")
    response = model.prompt("What is 17*3?", thinking=True)
    events = list(response.stream_events())
    reasoning = "".join(e.chunk for e in events if e.type == "reasoning")
    text = "".join(e.chunk for e in events if e.type == "text")
    assert "51" in reasoning
    assert "51" in text
    assert "<|inner" not in reasoning + text
    assert response.response_json["reasoning"] == reasoning


@pytest.mark.vcr
def test_tools():
    def multiply(a: int, b: int) -> int:
        "Multiply two integers"
        return a * b

    model = llm.get_model("publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT")
    chain = model.chain("What is 1234 * 5678? Use the tool.", tools=[multiply])
    responses = list(chain.responses())
    tool_calls = responses[0].tool_calls()
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "multiply"
    assert tool_calls[0].arguments == {"a": 1234, "b": 5678}
    assert "7,006,652" in chain.text() or "7006652" in chain.text()


@pytest.mark.vcr
def test_schema():
    model = llm.get_model("publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT")
    response = model.prompt(
        "Invent a dog",
        schema=llm.schema_dsl("name, age int"),
        stream=False,
    )
    data = json.loads(response.text())
    assert set(data.keys()) == {"name", "age"}
    assert isinstance(data["age"], int)


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_async_prompt():
    model = llm.get_async_model("publicai/swiss-ai/apertus-70b-instruct")
    response = model.prompt("Reply with just the word pelican, lowercase, nothing else")
    chunks = [chunk async for chunk in response]
    assert "".join(chunks).strip() == "pelican"
    usage = await response.usage()
    assert usage.input > 0


def test_models_command():
    result = CliRunner().invoke(cli, ["publicai", "models"])
    assert result.exit_code == 0, result.output
    assert (
        "- id: publicai/swiss-ai/apertus-70b-instruct\n"
        "  owned_by: swiss-ai\n"
        "  pricing: $0.82 input, $2.92 output (per million tokens)\n"
        "  supports: text only\n"
    ) in result.output
    assert "  supports: tools, schemas, images, thinking\n" in result.output


def test_models_command_json(models):
    result = CliRunner().invoke(cli, ["publicai", "models", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == models


@pytest.mark.vcr
def test_refresh_command(user_path):
    result = CliRunner().invoke(cli, ["publicai", "refresh"])
    assert result.exit_code == 0, result.output
    assert "Added models: " in result.output
    assert "BSC-LT/ALIA-40b-instruct" in result.output
    cached = json.loads((user_path / "publicai_models.json").read_text("utf-8"))
    assert "BSC-LT/ALIA-40b-instruct" in {model["id"] for model in cached}


def test_refresh_command_requires_key(monkeypatch):
    monkeypatch.delenv("PUBLICAI_API_KEY")
    result = CliRunner().invoke(cli, ["publicai", "refresh"])
    assert result.exit_code == 1
    assert "PUBLICAI_API_KEY" in result.output
