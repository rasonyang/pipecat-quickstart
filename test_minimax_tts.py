#!/usr/bin/env python3
"""Test MiniMax TTS to verify audio generation."""

import asyncio
import os
import aiohttp
from dotenv import load_dotenv
from loguru import logger

load_dotenv(override=True)

from pipecat.frames.frames import LLMTextFrame, AudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.minimax.tts import MiniMaxHttpTTSService
from pipecat.transcriptions.language import Language


class TestProcessor:
    """Simple processor to capture TTS output."""

    def __init__(self):
        self.audio_frames_received = 0
        self.total_audio_bytes = 0

    async def process_frame(self, frame, direction):
        """Capture frames."""
        if isinstance(frame, AudioRawFrame):
            self.audio_frames_received += 1
            self.total_audio_bytes += len(frame.audio)
            logger.info(f"✅ Received AudioRawFrame #{self.audio_frames_received}: "
                       f"{len(frame.audio)} bytes, {frame.sample_rate}Hz")
        else:
            logger.debug(f"📦 Received {frame.__class__.__name__}")


async def test_minimax_tts():
    """Test MiniMax TTS service."""

    # Get credentials
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    minimax_group_id = os.getenv("MINIMAX_GROUP_ID")

    if not minimax_api_key or not minimax_group_id:
        logger.error("❌ Missing MINIMAX_API_KEY or MINIMAX_GROUP_ID")
        return

    logger.info("🚀 Testing MiniMax TTS...")
    logger.info(f"API Key: {minimax_api_key[:20]}...")
    logger.info(f"Group ID: {minimax_group_id}")

    # Create test processor
    test_processor = TestProcessor()

    # Create aiohttp session
    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:

        # Create TTS service with Chinese language
        tts = MiniMaxHttpTTSService(
            api_key=minimax_api_key,
            group_id=minimax_group_id,
            aiohttp_session=session,
            base_url="https://api.minimaxi.com/v1/t2a_v2",
            sample_rate=8000,
            params=MiniMaxHttpTTSService.InputParams(language=Language.ZH_CN),
        )

        logger.info("✅ MiniMax TTS service created")

        # Link processors: TTS → TestProcessor
        tts.link(test_processor)

        logger.info("🔗 Processors linked: TTS → TestProcessor")

        # Test text
        test_texts = [
            "你好！",
            "很高兴见到你！",
            "我是一个友好的人工智能助手。",
        ]

        for i, text in enumerate(test_texts, 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"Test {i}/{len(test_texts)}: {text}")
            logger.info(f"{'='*60}")

            # Send text to TTS
            text_frame = LLMTextFrame(text)
            await tts.process_frame(text_frame, FrameDirection.DOWNSTREAM)

            # Wait a bit for processing
            await asyncio.sleep(1)

        logger.info(f"\n{'='*60}")
        logger.info("📊 Test Results:")
        logger.info(f"  Audio frames received: {test_processor.audio_frames_received}")
        logger.info(f"  Total audio bytes: {test_processor.total_audio_bytes}")

        if test_processor.audio_frames_received > 0:
            logger.info("✅ MiniMax TTS is working correctly!")
        else:
            logger.error("❌ No audio frames received - TTS is not generating audio!")

        logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(test_minimax_tts())
