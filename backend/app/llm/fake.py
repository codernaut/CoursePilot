"""A scriptable chat model for tests (``LLM_PROVIDER=fake``). Never offered by setup.

``responder`` receives the message list of every call and returns the reply text; ``calls``
records the message lists so tests can assert on the prompts. Replies are streamed word by word.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

_WORDS = re.compile(r"\S+\s*")


def default_responder(messages: list[BaseMessage]) -> str:
    """Cites the first source when the prompt has one, otherwise says the material is silent."""
    prompt = "\n".join(str(m.content) for m in messages)
    if 'id="C1"' in prompt:
        return "The material explains this [C1]."
    return "I could not find this in the uploaded material."


def _tokens(text: str) -> int:
    return len(text.split())


class FakeChatModel(BaseChatModel):
    # Callable[[list[BaseMessage]], str]
    responder: Any = default_responder
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    delay_s: float = 0.0  # pause before each streamed word (to test cancellation and timeouts)
    error: Any = None  # an exception instance raised on every call
    model_name: str = "fake-chat"

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def _reply(self, messages: list[BaseMessage]) -> str:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return str(self.responder(list(messages)))

    @staticmethod
    def _usage(messages: list[BaseMessage], reply: str) -> Any:
        prompt = sum(_tokens(str(m.content)) for m in messages)
        out = _tokens(reply)
        return {"input_tokens": prompt, "output_tokens": out, "total_tokens": prompt + out}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self._reply(messages)
        message = AIMessage(content=reply, usage_metadata=self._usage(messages, reply))
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self._reply(messages)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        message = AIMessage(content=reply, usage_metadata=self._usage(messages, reply))
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        reply = self._reply(messages)
        for word in _WORDS.findall(reply):
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield ChatGenerationChunk(message=AIMessageChunk(content=word))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", usage_metadata=self._usage(messages, reply))
        )
