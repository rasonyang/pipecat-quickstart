#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Zhipu AI GLM LLM Service.

This module provides a custom LLM service for Zhipu AI's GLM models (智谱AI).
The service uses OpenAI-compatible API format and supports:
- Streaming responses
- HTTP proxy bypass (trust_env=False)
- Standard OpenAI message format

API Reference: https://docs.bigmodel.cn/api-reference/模型-api/对话补全
"""

import asyncio
import time
from typing import Any, AsyncGenerator, Optional

import httpx
from loguru import logger
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from openai.types.chat import ChatCompletionChunk

from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    LLMContextFrame,
)
from pipecat.metrics.metrics import LLMTokenUsage
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService


class GLMLLMService(LLMService):
    """Zhipu AI GLM LLM Service with streaming support and proxy bypass.

    This service provides integration with Zhipu AI's GLM models (智谱AI),
    using their OpenAI-compatible API format. It supports:

    - Streaming chat completions
    - HTTP proxy bypass (disabled by default)
    - OpenAI-compatible message format
    - Token usage metrics
    - Function calling (via OpenAI adapter)

    Example usage:
        ```python
        llm = GLMLLMService(
            api_key=os.getenv("ZHIPUAI_API_KEY"),
            model="glm-4.5-airx",
        )
        ```

    Args:
        api_key: Zhipu AI API key (required)
        model: Model name (default: "glm-4.5-airx")
        base_url: API endpoint (default: Zhipu AI endpoint)
        disable_proxy: Disable HTTP proxy from environment (default: True)
        **kwargs: Additional parameters passed to LLMService
    """

    # Use OpenAI adapter for compatibility
    adapter_class = OpenAILLMAdapter

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "glm-4-flashx",
        base_url: str = "https://open.bigmodel.cn/api/paas/v4/",
        disable_proxy: bool = True,
        **kwargs,
    ):
        """Initialize GLM LLM service.

        Args:
            api_key: Zhipu AI API key.
            model: GLM model name (default: glm-4-flashx).
            base_url: Zhipu AI API endpoint.
            disable_proxy: Whether to disable HTTP proxy (default: True).
            **kwargs: Additional parameters for LLMService.
        """
        super().__init__(**kwargs)

        self._api_key = api_key
        self._base_url = base_url
        self._disable_proxy = disable_proxy
        self.set_model_name(model)

        # Create AsyncOpenAI client with optional proxy bypass
        self._client = self._create_client()

        logger.info(
            f"GLMLLMService initialized: model={model}, "
            f"base_url={base_url}, proxy_disabled={disable_proxy}"
        )

    def _create_client(self) -> AsyncOpenAI:
        """Create AsyncOpenAI client with optional proxy bypass.

        Returns:
            Configured AsyncOpenAI client.
        """
        # Create connection limits
        limits = httpx.Limits(
            max_keepalive_connections=100,
            max_connections=1000,
            keepalive_expiry=None,
        )

        # Create httpx client with optional proxy bypass
        if self._disable_proxy:
            logger.debug("HTTP proxy disabled (trust_env=False)")
            http_client = DefaultAsyncHttpxClient(
                limits=limits,
                trust_env=False,
            )
        else:
            http_client = DefaultAsyncHttpxClient(limits=limits)

        return AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            http_client=http_client,
        )

    async def _stream_chat_completions(
        self, context: LLMContext
    ) -> AsyncGenerator[ChatCompletionChunk, None]:
        """Stream chat completions from Zhipu AI API.

        Args:
            context: LLM context with messages and parameters.

        Yields:
            Chat completion chunks from the API.
        """
        # Get params from adapter
        adapter = self.get_llm_adapter()
        params = adapter.get_llm_invocation_params(context)

        # Build request parameters
        request_params = {
            "model": self.model_name,
            "stream": True,
            "messages": params["messages"],
        }

        # Add optional parameters if present
        if "temperature" in params:
            request_params["temperature"] = params["temperature"]
        if "max_tokens" in params:
            request_params["max_tokens"] = params["max_tokens"]
        if "top_p" in params:
            request_params["top_p"] = params["top_p"]
        if "tools" in params and params["tools"]:
            request_params["tools"] = params["tools"]
        if "tool_choice" in params:
            request_params["tool_choice"] = params["tool_choice"]

        logger.debug(f"Streaming request: model={self.model_name}, messages={len(params['messages'])}")

        # Stream responses from API
        stream = await self._client.chat.completions.create(**request_params)

        async for chunk in stream:
            yield chunk

    async def _process_context(self, context: LLMContext):
        """Process LLM context and stream responses.

        This is the main method that handles:
        1. Sending request to Zhipu AI API
        2. Streaming response chunks
        3. Handling function calls
        4. Tracking metrics

        Args:
            context: LLM context to process.
        """
        try:
            # Log LLM input (last user message)
            messages = context.messages
            user_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if user_msg:
                logger.info(f"💭 LLM Input: {user_msg.get('content', '')}")

            # Record start time
            llm_start_time = time.time()

            # Signal start of LLM response
            await self.push_frame(LLMFullResponseStartFrame())
            await self.start_processing_metrics()
            await self.start_ttfb_metrics()

            # Track function calls
            function_calls = []
            function_name = ""
            arguments = ""
            tool_call_id = ""

            # Stream chat completions
            chunk_num = 0
            total_chars = 0  # Track total characters for final summary
            full_response = ""  # Track full response content
            async for chunk in self._stream_chat_completions(context):
                chunk_num += 1

                # Stop TTFB metrics after first chunk
                if chunk_num == 1:
                    await self.stop_ttfb_metrics()

                # Process chunk choices
                if not chunk.choices:
                    continue

                choice = chunk.choices[0]

                # Handle text content (streaming deltas)
                if choice.delta and choice.delta.content:
                    text = choice.delta.content
                    total_chars += len(text)
                    full_response += text
                    await self.push_frame(LLMTextFrame(text))

                # Handle function/tool calls
                if choice.delta and choice.delta.tool_calls:
                    for tool_call in choice.delta.tool_calls:
                        # Start of function call
                        if tool_call.function and tool_call.function.name:
                            function_name = tool_call.function.name
                            tool_call_id = tool_call.id or ""
                            arguments = ""
                            logger.debug(f"Function call started: {function_name}")

                        # Accumulate function arguments
                        if tool_call.function and tool_call.function.arguments:
                            arguments += tool_call.function.arguments

                # Check if function call is complete
                if choice.finish_reason == "tool_calls" and function_name:
                    function_calls.append({
                        "function_name": function_name,
                        "tool_call_id": tool_call_id,
                        "arguments": arguments,
                    })
                    logger.info(f"Function call complete: {function_name}({arguments[:100]}...)")

                # Handle usage metrics (if provided in final chunk)
                if hasattr(chunk, 'usage') and chunk.usage:
                    tokens = LLMTokenUsage(
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.total_tokens,
                    )
                    await self.start_llm_usage_metrics(tokens)
                    logger.debug(
                        f"Token usage: prompt={tokens.prompt_tokens}, "
                        f"completion={tokens.completion_tokens}, "
                        f"total={tokens.total_tokens}"
                    )

            # Execute function calls if any
            if function_calls:
                logger.info(f"Executing {len(function_calls)} function call(s)")
                await self.run_function_calls(function_calls)

            # Log complete response with timing
            if total_chars > 0:
                duration_ms = int((time.time() - llm_start_time) * 1000)
                logger.info(f"💬 LLM Response: {full_response} (⏱️ {duration_ms}ms)")
                logger.debug(f"LLM stats: {chunk_num} chunks, {total_chars} chars")

        except asyncio.CancelledError:
            logger.warning("LLM request cancelled")
            raise
        except Exception as e:
            logger.exception(f"GLMLLMService error: {e}")
            await self.push_error(ErrorFrame(f"LLM processing error: {e}"))
        finally:
            # Signal end of LLM response
            await self.stop_processing_metrics()
            await self.push_frame(LLMFullResponseEndFrame())

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames.

        Handles LLMContextFrame to trigger LLM processing.

        Args:
            frame: Frame to process.
            direction: Frame direction (upstream/downstream).
        """
        await super().process_frame(frame, direction)

        # Handle LLM context frames
        if isinstance(frame, LLMContextFrame):
            await self._process_context(frame.context)
        else:
            # Pass through other frames
            await self.push_frame(frame, direction)
