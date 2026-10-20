import json
import os

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
    assert model.headers["User-Agent"].startswith("llm-public-ai/")
    assert str(model) == "Public AI: publicai/swiss-ai/apertus-70b-instruct"


def test_no_models_without_key_or_cache(user_path, monkeypatch):
    monkeypatch.delenv("PUBLICAI_API_KEY")
    (user_path / "publicai_models.json").remove()
    assert not [
        model for model in llm.get_models() if model.model_id.startswith("publicai/")
    ]


@pytest.mark.vcr
def test_prompt():
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    response = model.prompt("Say hello in Romansh. Just the greeting.")
    assert str(response) == "Allegra!"
    assert response.input_tokens > 0
    assert response.output_tokens > 0


@pytest.mark.vcr
def test_prompt_no_stream():
    model = llm.get_model("publicai/swiss-ai/apertus-70b-instruct")
    response = model.prompt("Say hello in Romansh. Just the greeting.", stream=False)
    assert str(response) == "Allegra!"
    assert response.response_json["model"] == "swiss-ai/apertus-70b-instruct"


@pytest.mark.vcr
def test_prompt_streams_reasoning():
    model = llm.get_model("publicai/aisingapore/Nemotron-SEA-LION-v4.8-120B-A12B")
    response = model.prompt("What is 17*3? Reply with just the number.")
    events = list(response.stream_events())
    reasoning = "".join(e.chunk for e in events if e.type == "reasoning")
    text = "".join(e.chunk for e in events if e.type == "text")
    assert "51" in reasoning
    assert text.strip() == "51"


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
    response = model.prompt("Say hello in Romansh. Just the greeting.")
    chunks = [chunk async for chunk in response]
    assert "".join(chunks) == "Allegra!"
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
    assert "  supports: tools, schemas, images\n" in result.output


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
