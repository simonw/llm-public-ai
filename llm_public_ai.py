import json
import time
from importlib.metadata import PackageNotFoundError, version

import click
import httpx
import llm
from llm.default_plugins.openai_models import (
    AsyncChat,
    Chat,
    combine_chunks,
    redact_data,
    remove_dict_none_values,
)
from llm.parts import StreamEvent

API_BASE = "https://api.publicai.co/v1"
KEY_NAME = "publicai"
KEY_ENV_VAR = "PUBLICAI_API_KEY"
CACHE_TIMEOUT = 3600

try:
    __version__ = version("llm-public-ai")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0"

# The Public AI API requires a User-Agent header identifying the client
USER_AGENT = "llm-public-ai/{} (+https://github.com/simonw/llm-public-ai)".format(
    __version__
)

# /v1/models does not describe model capabilities, so these were determined
# by testing each model against the API. Models not listed here are assumed
# to support tools and schemas but not images.
DEFAULT_CAPABILITIES = {"vision": False, "supports_tools": True, "supports_schema": True}
MODEL_CAPABILITIES = {
    "swiss-ai/apertus-8b-instruct": {"supports_tools": False, "supports_schema": False},
    "swiss-ai/apertus-70b-instruct": {
        "supports_tools": False,
        "supports_schema": False,
    },
    "swiss-ai/apertus-v1.5-70b": {"vision": True},
    "aisingapore/Gemma-SEA-LION-v4-27B-IT": {
        "vision": True,
        "supports_tools": False,
        "supports_schema": False,
    },
    "aisingapore/Qwen-SEA-LION-v4.5-27B-IT": {"vision": True},
}


class DownloadError(Exception):
    pass


def models_cache_path():
    return llm.user_dir() / "publicai_models.json"


def get_key():
    return llm.get_key(None, KEY_NAME, KEY_ENV_VAR)


def fetch_models(key):
    response = httpx.get(
        API_BASE + "/models",
        headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["data"]


def get_models(skip_cache=False):
    """
    Return the list of model dictionaries, using a cached copy if it is less
    than an hour old. Falls back to a stale cache if the API can't be reached.
    """
    path = models_cache_path()
    cache_timeout = 0 if skip_cache else CACHE_TIMEOUT
    if path.is_file() and time.time() - path.stat().st_mtime < cache_timeout:
        return json.loads(path.read_text())
    key = get_key()
    if key:
        try:
            models = fetch_models(key)
        except httpx.HTTPError as ex:
            if path.is_file() and not skip_cache:
                return json.loads(path.read_text())
            raise DownloadError(f"Failed to fetch Public AI models: {ex}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(models, indent=2))
        return models
    if path.is_file():
        return json.loads(path.read_text())
    return []


@llm.hookimpl
def register_models(register):
    try:
        models = get_models()
    except DownloadError:
        return
    for model in models:
        model_name = model["id"]
        capabilities = {
            **DEFAULT_CAPABILITIES,
            **MODEL_CAPABILITIES.get(model_name, {}),
        }
        kwargs = dict(
            model_id="publicai/{}".format(model_name),
            model_name=model_name,
            api_base=API_BASE,
            headers={"User-Agent": USER_AGENT},
            **capabilities,
        )
        register(PublicAIChat(**kwargs), AsyncPublicAIChat(**kwargs))


class _PublicAIShared:
    needs_key = KEY_NAME
    key_env_var = KEY_ENV_VAR

    def __str__(self):
        return "Public AI: {}".format(self.model_id)

    def _chunk_events(self, chunk, tool_calls):
        "Turn a streamed chunk into StreamEvents, accumulating tool calls"
        if not chunk.choices or not chunk.choices[0].delta:
            return
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            yield StreamEvent(type="reasoning", chunk=reasoning)
        for tool_call in delta.tool_calls or []:
            if tool_call.function.arguments is None:
                tool_call.function.arguments = ""
            idx = tool_call.index
            if idx not in tool_calls:
                tool_calls[idx] = tool_call
                yield StreamEvent(
                    type="tool_call_name",
                    chunk=tool_call.function.name or "",
                    tool_call_id=tool_call.id,
                )
            else:
                tool_calls[idx].function.arguments += tool_call.function.arguments
            if tool_call.function.arguments:
                yield StreamEvent(
                    type="tool_call_args",
                    chunk=tool_call.function.arguments,
                    tool_call_id=tool_calls[idx].id,
                )
        if delta.content:
            yield StreamEvent(type="text", chunk=delta.content)

    def _completion_events(self, completion):
        "Turn a non-streamed completion into StreamEvents"
        message = completion.choices[0].message
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning:
            yield StreamEvent(type="reasoning", chunk=reasoning)
        for tool_call in message.tool_calls or []:
            yield StreamEvent(
                type="tool_call_name",
                chunk=tool_call.function.name or "",
                tool_call_id=tool_call.id,
            )
            yield StreamEvent(
                type="tool_call_args",
                chunk=tool_call.function.arguments or "",
                tool_call_id=tool_call.id,
            )
        if message.content is not None:
            yield StreamEvent(type="text", chunk=message.content)

    def _add_tool_calls(self, response, tool_calls):
        for tool_call in tool_calls:
            response.add_tool_call(
                llm.ToolCall(
                    tool_call_id=tool_call.id,
                    name=tool_call.function.name,
                    arguments=json.loads(tool_call.function.arguments or "{}"),
                )
            )

    def _prepare(self, prompt, conversation, stream):
        if prompt.system and not self.allows_system_prompt:
            raise NotImplementedError("Model does not support system prompts")
        messages = self.build_messages(
            prompt,
            conversation,
            image_detail=getattr(prompt.options, "image_detail", None),
        )
        kwargs = self.build_kwargs(prompt, stream)
        return messages, kwargs


class PublicAIChat(_PublicAIShared, Chat):
    def execute(self, prompt, stream, response, conversation=None, key=None):
        messages, kwargs = self._prepare(prompt, conversation, stream)
        client = self.get_client(key)
        usage = None
        completion = client.chat.completions.create(
            model=self.model_name, messages=messages, stream=stream, **kwargs
        )
        if stream:
            chunks = []
            tool_calls = {}
            for chunk in completion:
                chunks.append(chunk)
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                yield from self._chunk_events(chunk, tool_calls)
            response.response_json = remove_dict_none_values(combine_chunks(chunks))
            self._add_tool_calls(response, tool_calls.values())
        else:
            usage = completion.usage.model_dump() if completion.usage else None
            response.response_json = remove_dict_none_values(completion.model_dump())
            self._add_tool_calls(response, completion.choices[0].message.tool_calls or [])
            yield from self._completion_events(completion)
        self.set_usage(response, usage)
        response._prompt_json = redact_data({"messages": messages})


class AsyncPublicAIChat(_PublicAIShared, AsyncChat):
    async def execute(self, prompt, stream, response, conversation=None, key=None):
        messages, kwargs = self._prepare(prompt, conversation, stream)
        client = self.get_client(key, async_=True)
        usage = None
        completion = await client.chat.completions.create(
            model=self.model_name, messages=messages, stream=stream, **kwargs
        )
        if stream:
            chunks = []
            tool_calls = {}
            async for chunk in completion:
                chunks.append(chunk)
                if chunk.usage:
                    usage = chunk.usage.model_dump()
                for event in self._chunk_events(chunk, tool_calls):
                    yield event
            response.response_json = remove_dict_none_values(combine_chunks(chunks))
            self._add_tool_calls(response, tool_calls.values())
        else:
            usage = completion.usage.model_dump() if completion.usage else None
            response.response_json = remove_dict_none_values(completion.model_dump())
            self._add_tool_calls(response, completion.choices[0].message.tool_calls or [])
            for event in self._completion_events(completion):
                yield event
        self.set_usage(response, usage)
        response._prompt_json = redact_data({"messages": messages})


@llm.hookimpl
def register_commands(cli):
    @cli.group()
    def publicai():
        "Commands relating to the llm-public-ai plugin"

    @publicai.command()
    @click.option("json_", "--json", is_flag=True, help="Output as JSON")
    def models(json_):
        "List available Public AI models"
        try:
            all_models = get_models()
        except DownloadError as ex:
            raise click.ClickException(str(ex))
        if not all_models and not get_key():
            raise click.ClickException(
                "Set the '{}' key with 'llm keys set {}' or the {} "
                "environment variable".format(KEY_NAME, KEY_NAME, KEY_ENV_VAR)
            )
        if json_:
            click.echo(json.dumps(all_models, indent=2))
            return
        for model in all_models:
            capabilities = {
                **DEFAULT_CAPABILITIES,
                **MODEL_CAPABILITIES.get(model["id"], {}),
            }
            features = [
                label
                for label, key in (
                    ("tools", "supports_tools"),
                    ("schemas", "supports_schema"),
                    ("images", "vision"),
                )
                if capabilities[key]
            ]
            bits = [f"- id: publicai/{model['id']}"]
            if model.get("owned_by"):
                bits.append(f"  owned_by: {model['owned_by']}")
            if model.get("context_length"):
                bits.append(f"  context_length: {model['context_length']:,}")
            pricing = model.get("pricing")
            if pricing:
                bits.append(
                    "  pricing: ${} input, ${} output (per million tokens)".format(
                        pricing.get("input"), pricing.get("output")
                    )
                )
            bits.append("  supports: {}".format(", ".join(features) or "text only"))
            click.echo("\n".join(bits))

    @publicai.command()
    def refresh():
        "Refresh the cached list of Public AI models"
        if not get_key():
            raise click.ClickException(
                "Set the '{}' key with 'llm keys set {}' or the {} "
                "environment variable".format(KEY_NAME, KEY_NAME, KEY_ENV_VAR)
            )
        path = models_cache_path()
        previous = (
            {model["id"] for model in json.loads(path.read_text())}
            if path.is_file()
            else set()
        )
        try:
            models = get_models(skip_cache=True)
        except DownloadError as ex:
            raise click.ClickException(str(ex))
        current = {model["id"] for model in models}
        added = sorted(current - previous)
        removed = sorted(previous - current)
        if added:
            click.echo("Added models: {}".format(", ".join(added)), err=True)
        if removed:
            click.echo("Removed models: {}".format(", ".join(removed)), err=True)
        if not added and not removed:
            click.echo("No changes", err=True)
