import json
import time
from importlib.metadata import PackageNotFoundError, version

import click
import httpx
import llm
import openai
from llm.parts import (
    AttachmentPart,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from llm.utils import remove_dict_none_values
from pydantic import Field

API_BASE = "https://api.publicai.co/v1"
KEY_NAME = "publicai"
KEY_ENV_VAR = "PUBLICAI_API_KEY"
CACHE_TIMEOUT = 3600

try:
    __version__ = version("llm-public-ai")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0"

# The Public AI API requires a User-Agent header identifying the client
USER_AGENT = f"llm-public-ai/{__version__} (+https://github.com/simonw/llm-public-ai)"

# /v1/models does not describe model capabilities, so these were determined
# by testing each model against the API. Models not listed here are assumed
# to support tools and schemas but not images or a thinking toggle.
DEFAULT_CAPABILITIES = {
    "vision": False,
    "supports_tools": True,
    "supports_schema": True,
    "thinking": False,
}
MODEL_CAPABILITIES = {
    "swiss-ai/apertus-8b-instruct": {"supports_tools": False, "supports_schema": False},
    "swiss-ai/apertus-70b-instruct": {
        "supports_tools": False,
        "supports_schema": False,
    },
    "swiss-ai/apertus-v1.5-70b": {"vision": True, "thinking": True},
    "aisingapore/Gemma-SEA-LION-v4-27B-IT": {
        "vision": True,
        "supports_tools": False,
        "supports_schema": False,
    },
    "aisingapore/Qwen-SEA-LION-v4.5-27B-IT": {"vision": True, "thinking": True},
    "aisingapore/Nemotron-SEA-LION-v4.8-120B-A12B": {"thinking": True},
}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def model_capabilities(model_name):
    return {**DEFAULT_CAPABILITIES, **MODEL_CAPABILITIES.get(model_name, {})}


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
        kwargs = dict(
            model_id=f"publicai/{model_name}",
            model_name=model_name,
            **model_capabilities(model_name),
        )
        register(PublicAIChat(**kwargs), AsyncPublicAIChat(**kwargs))


class PublicAIOptions(llm.Options):
    temperature: float | None = Field(
        description=(
            "What sampling temperature to use, between 0 and 2. Higher values "
            "like 0.8 make the output more random, lower values like 0.2 make "
            "it more focused and deterministic."
        ),
        ge=0,
        le=2,
        default=None,
    )
    max_tokens: int | None = Field(
        description="Maximum number of tokens to generate.", default=None
    )
    top_p: float | None = Field(
        description=(
            "Nucleus sampling: only consider tokens comprising the top_p "
            "probability mass. Recommended to alter this or temperature but "
            "not both."
        ),
        ge=0,
        le=1,
        default=None,
    )
    frequency_penalty: float | None = Field(
        description=(
            "Number between -2.0 and 2.0. Positive values penalize new tokens "
            "based on their existing frequency in the text so far."
        ),
        ge=-2,
        le=2,
        default=None,
    )
    presence_penalty: float | None = Field(
        description=(
            "Number between -2.0 and 2.0. Positive values penalize new tokens "
            "based on whether they appear in the text so far."
        ),
        ge=-2,
        le=2,
        default=None,
    )
    stop: str | list[str] | None = Field(
        description="A string (or list of strings) where the API will stop generating",
        default=None,
    )
    seed: int | None = Field(
        description="Integer seed to attempt to sample deterministically",
        default=None,
    )


class PublicAIThinkingOptions(PublicAIOptions):
    thinking: bool | None = Field(
        description=(
            "Turn the model's thinking mode on or off. Leave unset to use the "
            "model's default."
        ),
        default=None,
    )


class InlineThinkingSplitter:
    """
    Apertus v1.5 returns its thinking inline in the content, wrapped in
    <|inner_prefix|> and <|inner_suffix|> markers. This splits a stream of
    text into reasoning and text StreamEvents, holding back anything that
    might be the start of a marker split across chunks.
    """

    OPEN = "<|inner_prefix|>"
    CLOSE = "<|inner_suffix|>"

    def __init__(self):
        self.buffer = ""
        self.thinking = False

    def feed(self, text):
        self.buffer += text
        while True:
            marker = self.CLOSE if self.thinking else self.OPEN
            index = self.buffer.find(marker)
            if index == -1:
                break
            yield from self._emit(self.buffer[:index])
            self.buffer = self.buffer[index + len(marker) :]
            self.thinking = not self.thinking
        held = 0
        for size in range(min(len(marker) - 1, len(self.buffer)), 0, -1):
            if marker.startswith(self.buffer[-size:]):
                held = size
                break
        cut = len(self.buffer) - held
        yield from self._emit(self.buffer[:cut])
        self.buffer = self.buffer[cut:]

    def flush(self):
        yield from self._emit(self.buffer)
        self.buffer = ""

    def _emit(self, text):
        if text:
            yield StreamEvent(type="reasoning" if self.thinking else "text", chunk=text)


class _ResponseState:
    "Collects the pieces of a (streamed or not) response as events are emitted"

    def __init__(self, inline_thinking):
        self.splitter = InlineThinkingSplitter() if inline_thinking else None
        self.content = ""
        self.reasoning = ""
        self.tool_calls = {}
        self.usage = None
        self.finish_reason = None
        self.id = None
        self.model = None

    def text_events(self, text):
        if not text:
            return
        events = (
            self.splitter.feed(text)
            if self.splitter
            else [StreamEvent(type="text", chunk=text)]
        )
        for event in events:
            if event.type == "text":
                self.content += event.chunk
            else:
                self.reasoning += event.chunk
            yield event

    def reasoning_events(self, reasoning):
        if reasoning:
            self.reasoning += reasoning
            yield StreamEvent(type="reasoning", chunk=reasoning)

    def chunk_events(self, chunk):
        "Turn a streamed chunk into StreamEvents"
        self.id = self.id or chunk.id
        self.model = self.model or chunk.model
        if chunk.usage:
            self.usage = chunk.usage.model_dump()
        if not chunk.choices:
            return
        choice = chunk.choices[0]
        if choice.finish_reason:
            self.finish_reason = choice.finish_reason
        delta = choice.delta
        if not delta:
            return
        yield from self.reasoning_events(getattr(delta, "reasoning_content", None))
        for tool_call in delta.tool_calls or []:
            if tool_call.index not in self.tool_calls:
                self.tool_calls[tool_call.index] = {
                    "id": tool_call.id,
                    "name": tool_call.function.name or "",
                    "arguments": "",
                }
                yield StreamEvent(
                    type="tool_call_name",
                    chunk=tool_call.function.name or "",
                    tool_call_id=tool_call.id,
                )
            existing = self.tool_calls[tool_call.index]
            if tool_call.function.arguments:
                existing["arguments"] += tool_call.function.arguments
                yield StreamEvent(
                    type="tool_call_args",
                    chunk=tool_call.function.arguments,
                    tool_call_id=existing["id"],
                )
        yield from self.text_events(delta.content)

    def completion_events(self, completion):
        "Turn a non-streamed completion into StreamEvents"
        self.id = completion.id
        self.model = completion.model
        self.usage = completion.usage.model_dump() if completion.usage else None
        choice = completion.choices[0]
        self.finish_reason = choice.finish_reason
        message = choice.message
        yield from self.reasoning_events(getattr(message, "reasoning_content", None))
        for index, tool_call in enumerate(message.tool_calls or []):
            self.tool_calls[index] = {
                "id": tool_call.id,
                "name": tool_call.function.name,
                "arguments": tool_call.function.arguments or "",
            }
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
        yield from self.text_events(message.content)

    def finish_events(self):
        if self.splitter:
            for event in self.splitter.flush():
                if event.type == "text":
                    self.content += event.chunk
                else:
                    self.reasoning += event.chunk
                yield event

    def apply_to(self, response, messages):
        for tool_call in self.tool_calls.values():
            response.add_tool_call(
                llm.ToolCall(
                    tool_call_id=tool_call["id"],
                    name=tool_call["name"],
                    arguments=json.loads(tool_call["arguments"] or "{}"),
                )
            )
        if self.usage:
            usage = remove_dict_none_values(self.usage)
            details = {}
            reasoning_tokens = (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            )
            if reasoning_tokens:
                details["reasoning_tokens"] = reasoning_tokens
            cached_tokens = (usage.get("prompt_tokens_details") or {}).get(
                "cached_tokens"
            )
            if cached_tokens:
                details["cached_tokens"] = cached_tokens
            response.set_usage(
                input=usage.get("prompt_tokens"),
                output=usage.get("completion_tokens"),
                details=details or None,
            )
        response.response_json = remove_dict_none_values(
            {
                "id": self.id,
                "model": self.model,
                "content": self.content,
                "reasoning": self.reasoning or None,
                "finish_reason": self.finish_reason,
                "tool_calls": list(self.tool_calls.values()) or None,
                "usage": self.usage,
            }
        )
        response._prompt_json = {"messages": _redact_data_urls(messages)}


def _redact_data_urls(messages):
    "Avoid logging base64 image data in full"
    redacted = []
    for message in messages:
        if isinstance(message.get("content"), list):
            content = []
            for item in message["content"]:
                url = (item.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    item = {
                        **item,
                        "image_url": {"url": url.split(",")[0] + ",..."},
                    }
                content.append(item)
            message = {**message, "content": content}
        redacted.append(message)
    return redacted


def _attachment(attachment):
    url = attachment.url
    if not url:
        url = f"data:{attachment.resolve_type()};base64,{attachment.base64_content()}"
    return {"type": "image_url", "image_url": {"url": url}}


class _SharedPublicAI:
    needs_key = KEY_NAME
    key_env_var = KEY_ENV_VAR
    can_stream = True

    def __init__(
        self,
        model_id,
        model_name,
        vision=False,
        supports_tools=False,
        supports_schema=False,
        thinking=False,
    ):
        self.model_id = model_id
        self.model_name = model_name
        self.supports_tools = supports_tools
        self.supports_schema = supports_schema
        self.attachment_types = set(IMAGE_TYPES) if vision else set()
        self.thinking = thinking
        self.Options = PublicAIThinkingOptions if thinking else PublicAIOptions
        # Apertus v1.5 models return their thinking inline in the content
        self.inline_thinking = model_name.startswith("swiss-ai/apertus-v1.5")

    def __str__(self):
        return f"Public AI: {self.model_id}"

    def build_messages(self, prompt):
        "Translate prompt.messages into the OpenAI chat completions format"
        messages = []
        current_system = None
        for message in prompt.messages:
            text_bits = []
            attachments = []
            tool_calls = []
            tool_results = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    text_bits.append(part.text)
                elif isinstance(part, AttachmentPart) and part.attachment:
                    attachments.append(_attachment(part.attachment))
                elif isinstance(part, ToolCallPart):
                    tool_calls.append(
                        {
                            "type": "function",
                            "id": part.tool_call_id,
                            "function": {
                                "name": part.name,
                                "arguments": json.dumps(part.arguments),
                            },
                        }
                    )
                elif isinstance(part, ToolResultPart):
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": part.tool_call_id,
                            "content": part.output,
                        }
                    )
            if message.role == "tool":
                messages.extend(tool_results)
                continue
            text = "".join(text_bits) if text_bits else None
            if message.role == "system":
                # Don't repeat an unchanged system prompt
                if text == current_system:
                    continue
                current_system = text
            if attachments:
                content = [{"type": "text", "text": text}] if text else []
                entry = {"role": message.role, "content": content + attachments}
            else:
                entry = {"role": message.role, "content": text}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            elif entry["content"] is None and message.role != "assistant":
                continue
            messages.append(entry)
        return messages

    def build_kwargs(self, prompt, messages, stream):
        options = {key: value for key, value in prompt.options if value is not None}
        thinking = options.pop("thinking", None)
        kwargs = dict(model=self.model_name, messages=messages, stream=stream)
        kwargs.update(options)
        if self.thinking:
            # The gateway's response cache ignores chat_template_kwargs, so it
            # can return a response generated with the other thinking setting
            extra_body = {"cache": {"no-cache": True}}
            if thinking is not None:
                extra_body["chat_template_kwargs"] = {"enable_thinking": thinking}
            kwargs["extra_body"] = extra_body
        if prompt.schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": prompt.schema},
            }
        if prompt.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or None,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in prompt.tools
            ]
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    def client_kwargs(self, key):
        return dict(
            base_url=API_BASE,
            api_key=self.get_key(key),
            default_headers={"User-Agent": USER_AGENT},
        )


class PublicAIChat(_SharedPublicAI, llm.KeyModel):
    def execute(self, prompt, stream, response, conversation=None, key=None):
        messages = self.build_messages(prompt)
        client = openai.OpenAI(**self.client_kwargs(key))
        completion = client.chat.completions.create(
            **self.build_kwargs(prompt, messages, stream)
        )
        state = _ResponseState(self.inline_thinking)
        if stream:
            for chunk in completion:
                yield from state.chunk_events(chunk)
        else:
            yield from state.completion_events(completion)
        yield from state.finish_events()
        state.apply_to(response, messages)


class AsyncPublicAIChat(_SharedPublicAI, llm.AsyncKeyModel):
    async def execute(self, prompt, stream, response, conversation=None, key=None):
        messages = self.build_messages(prompt)
        client = openai.AsyncOpenAI(**self.client_kwargs(key))
        completion = await client.chat.completions.create(
            **self.build_kwargs(prompt, messages, stream)
        )
        state = _ResponseState(self.inline_thinking)
        if stream:
            async for chunk in completion:
                for event in state.chunk_events(chunk):
                    yield event
        else:
            for event in state.completion_events(completion):
                yield event
        for event in state.finish_events():
            yield event
        state.apply_to(response, messages)


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
                f"Set the '{KEY_NAME}' key with 'llm keys set {KEY_NAME}' or the {KEY_ENV_VAR} "
                "environment variable"
            )
        if json_:
            click.echo(json.dumps(all_models, indent=2))
            return
        for model in all_models:
            capabilities = model_capabilities(model["id"])
            features = [
                label
                for label, key in (
                    ("tools", "supports_tools"),
                    ("schemas", "supports_schema"),
                    ("images", "vision"),
                    ("thinking", "thinking"),
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
                f"Set the '{KEY_NAME}' key with 'llm keys set {KEY_NAME}' or the {KEY_ENV_VAR} "
                "environment variable"
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
