#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat SIP Transport Example.

The example runs a simple voice AI bot that you can connect to using SIP.

Required AI services:
- Tencent Cloud (Speech-to-Text)
- OpenAI (LLM)
- MiniMax (Text-to-Speech)

Run the bot using::

    uv run main.py
"""

import os

import aiohttp
from dotenv import load_dotenv
from loguru import logger

print("🚀 Starting Pipecat SIP bot...")
print("⏳ Loading models and imports (15 seconds, first run only)\n")

logger.info("Loading Local Smart Turn Analyzer V3...")
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

logger.info("✅ Local Smart Turn Analyzer V3 loaded")
logger.info("Loading Silero VAD model...")
from pipecat.audio.vad.silero import SileroVADAnalyzer

logger.info("✅ Silero VAD model loaded")

from pipecat.audio.vad.vad_analyzer import VADParams

logger.info("Loading pipeline components...")

# Enable more detailed logging for Tencent and MiniMax
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("pipecat.services.minimax").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("httpcore").setLevel(logging.DEBUG)

from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.types import RunnerArguments
from pipecat.services.minimax.tts import Language, MiniMaxHttpTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport
from services.tencent_stt import TencentSTTService

from sip_transport import SIPParams, SIPTransport

logger.info("✅ All components loaded successfully!")

load_dotenv(override=True)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info(f"Starting bot")

    # Validate MiniMax credentials
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    minimax_group_id = os.getenv("MINIMAX_GROUP_ID")

    if not minimax_api_key:
        raise ValueError("❌ MINIMAX_API_KEY not found in environment variables")
    if not minimax_group_id:
        raise ValueError("❌ MINIMAX_GROUP_ID not found in environment variables")

    logger.info(f"✅ MiniMax credentials found: group_id={minimax_group_id[:10]}...")

    # Create aiohttp session for MiniMax TTS (disable proxy to avoid connection issues)
    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        logger.info(f"✅ aiohttp session created (proxy disabled): {session}")

        # Tencent STT for SIP (8kHz Mandarin, VAD + Turn Analyzer handle speech detection)
        stt = TencentSTTService(
            secret_id=os.getenv("TENCENT_SECRET_ID"),
            secret_key=os.getenv("TENCENT_SECRET_KEY"),
            appid=os.getenv("TENCENT_APPID"),
            engine_model_type="8k_zh",  # 8kHz Mandarin for telephony
            sample_rate=8000,
            filter_dirty=1,  # Enable profanity filtering
            audio_passthrough=False,  # Prevent audio echo
        )

        # MiniMax TTS for SIP (8kHz sample rate)
        tts = MiniMaxHttpTTSService(
            api_key=minimax_api_key,
            group_id=minimax_group_id,
            aiohttp_session=session,
            base_url="https://api.minimaxi.com/v1/t2a_v2",  # Correct official endpoint
            sample_rate=8000,
            params=MiniMaxHttpTTSService.InputParams(language=Language.EN),
        )
        logger.info(f"✅ MiniMax TTS initialized with base_url=https://api.minimaxi.com/v1/t2a_v2")

        llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"))

        messages = [
            {
                "role": "system",
                "content": "You are a friendly AI assistant. Respond naturally and keep your answers conversational.",
            },
        ]

        context = LLMContext(messages)
        context_aggregator = LLMContextAggregatorPair(context)

        pipeline = Pipeline(
            [
                transport.input(),  # Transport user input
                stt,
                context_aggregator.user(),  # User responses
                llm,  # LLM
                tts,  # TTS
                transport.output(),  # Transport bot output
                context_aggregator.assistant(),  # Assistant spoken responses
            ]
        )

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
                allow_interruptions=True,  # Enable interruptions for barge-in
            ),
            enable_turn_tracking=False,  # Disable turn tracking for SIP
            idle_timeout_secs=None,  # Disable idle timeout observer for SIP
        )

        # Standard event handlers (like bot.py)
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"SIP call connected from {client}")
            # Kick off the conversation.
            messages.append({"role": "system", "content": "Say hello and briefly introduce yourself."})
            await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info(f"SIP call disconnected from {client}")
            # Don't cancel task - SIP server should continue running for next call

        runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)

        await runner.run(task)


async def bot(runner_args: RunnerArguments):
    """Main bot entry point for the bot starter."""

    params = SIPParams(
        host=os.getenv("SIP_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SIP_SERVER_PORT", "6060")),
        rtp_port_start=int(os.getenv("SIP_RTP_PORT_RANGE", "10000-15000").split("-")[0]),
        rtp_port_end=int(os.getenv("SIP_RTP_PORT_RANGE", "10000-15000").split("-")[1]),
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(sample_rate=8000, params=VADParams(stop_secs=0.2)),
        turn_analyzer=LocalSmartTurnAnalyzerV3(),
    )

    transport = SIPTransport(params)
    await transport.start()

    await run_bot(transport, runner_args)


if __name__ == "__main__":
    import asyncio
    import sys
    from dataclasses import dataclass

    @dataclass
    class SimpleRunnerArgs:
        handle_sigint: bool = True

    runner_args = SimpleRunnerArgs()

    try:
        asyncio.run(bot(runner_args))
    except KeyboardInterrupt:
        logger.info("\n🛑 Shutting down...")
        logger.info("✅ Bot stopped")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}", exc_info=True)
        sys.exit(1)
