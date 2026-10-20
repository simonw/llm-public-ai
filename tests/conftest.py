import json
import os

import llm
import pytest

API_KEY = os.environ.get("PUBLICAI_API_KEY") or "mock-key"

MODELS = [
    {
        "id": "swiss-ai/apertus-70b-instruct",
        "object": "model",
        "created": 1677610602,
        "owned_by": "swiss-ai",
        "pricing": {"input": 0.82, "output": 2.92},
    },
    {
        "id": "swiss-ai/apertus-v1.5-70b",
        "object": "model",
        "created": 1677610602,
        "owned_by": "swiss-ai",
        "pricing": {"input": 0.82, "output": 2.92},
    },
    {
        "id": "aisingapore/Nemotron-SEA-LION-v4.8-120B-A12B",
        "object": "model",
        "created": 1677610602,
        "owned_by": "aisingapore",
        "pricing": {"input": 0.5, "output": 2},
    },
    {
        "id": "aisingapore/Qwen-SEA-LION-v4-32B-IT",
        "object": "model",
        "created": 1677610602,
        "owned_by": "aisingapore",
        "pricing": {"input": 0.25, "output": 0.5},
    },
]


@pytest.fixture(autouse=True)
def user_path(tmpdir, monkeypatch):
    dir = tmpdir / "llm.datasette.io"
    dir.mkdir()
    monkeypatch.setenv("LLM_USER_PATH", str(dir))
    monkeypatch.setenv("PUBLICAI_API_KEY", API_KEY)
    # Pre-populate the models cache so tests don't hit /v1/models
    (dir / "publicai_models.json").write_text(json.dumps(MODELS), "utf-8")
    return dir


@pytest.fixture(scope="module")
def vcr_config():
    return {"filter_headers": ["authorization"]}


@pytest.fixture
def models():
    return MODELS
