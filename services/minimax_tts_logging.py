#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""MiniMax TTS service with enhanced logging.

This module wraps MiniMaxHttpTTSService to add INFO-level logging for
input text, output completion, and timing metrics.
"""

import time
from typing import AsyncGenerator

from loguru import logger

from pipecat.frames.frames import Frame
from pipecat.services.minimax.tts import MiniMaxHttpTTSService


class MiniMaxTTSWithLogging(MiniMaxHttpTTSService):
    """MiniMax TTS service with enhanced logging.

    Extends MiniMaxHttpTTSService to add INFO-level logs for:
    - Input text being synthesized
    - Completion status and timing

    Example usage:
        ```python
        tts = MiniMaxTTSWithLogging(
            api_key=os.getenv("MINIMAX_API_KEY"),
            group_id=os.getenv("MINIMAX_GROUP_ID"),
            aiohttp_session=session,
            sample_rate=8000,
        )
        ```
    """

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        """Generate speech from text with logging.

        Args:
            text: Text to synthesize.

        Yields:
            Audio frames from TTS synthesis.
        """
        # Log input
        logger.info(f"🔊 TTS Input: {text}")

        # Record start time
        tts_start = time.time()

        # Run parent TTS
        async for frame in super().run_tts(text):
            yield frame

        # Log completion with timing
        duration_ms = int((time.time() - tts_start) * 1000)
        logger.info(f"✅ TTS Complete (⏱️ {duration_ms}ms)")
