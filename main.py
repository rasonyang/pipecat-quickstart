#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Pipecat SIP Transport Example.

The example runs a simple voice AI bot that you can connect to using SIP.

Required AI services:
- Tencent Cloud (Speech-to-Text)
- Zhipu AI (LLM)
- MiniMax (Text-to-Speech)

Run the bot using::

    uv run main.py
"""

import asyncio
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

# Configure logging levels
import logging

logging.basicConfig(level=logging.INFO)  # Set global level to INFO

# Enable DEBUG for specific services if needed
logging.getLogger("pipecat.services.minimax").setLevel(logging.DEBUG)

# Reduce noise from third-party libraries
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("websockets.client").setLevel(logging.WARNING)  # Silence BINARY messages

from pipecat.frames.frames import LLMContextFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport

from services.glm_llm import GLMLLMService
from services.minimax_tts_logging import MiniMaxTTSWithLogging
from services.tencent_segmented_stt import TencentSegmentedSTTService
from sip_transport import SIPParams, SIPTransport

logger.info("✅ All components loaded successfully!")

load_dotenv(override=True)


class LLMContextPruner(FrameProcessor):
    """Prunes LLM context to keep only recent exchanges.

    Maintains system message + last N user/assistant message pairs
    to prevent context from growing indefinitely.
    """

    def __init__(self, max_exchanges: int = 10):
        """Initialize context pruner.

        Args:
            max_exchanges: Maximum number of user/assistant exchange pairs to keep
        """
        super().__init__()
        self._max_exchanges = max_exchanges

    async def process_frame(self, frame, direction: FrameDirection):
        """Process frames, pruning LLMContextFrame if needed."""
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            context = frame.context
            messages = context.messages

            # Calculate max messages: system (1) + exchanges (user + assistant pairs)
            max_messages = 1 + (self._max_exchanges * 2)

            if len(messages) > max_messages:
                # Keep system message (first) + last N exchanges
                # Modify the list in-place to preserve the reference
                system_msg = messages[0]
                recent_exchanges = list(messages[-(self._max_exchanges * 2):])
                original_len = len(messages)

                # Clear and rebuild messages list in-place
                messages.clear()
                messages.append(system_msg)
                messages.extend(recent_exchanges)

                logger.info(
                    f"Pruned LLM context: {original_len} → {len(messages)} messages "
                    f"(keeping {self._max_exchanges} exchanges)"
                )

        await self.push_frame(frame, direction)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    logger.info(f"Starting bot with multi-session support")

    # Validate MiniMax credentials
    minimax_api_key = os.getenv("MINIMAX_API_KEY")
    minimax_group_id = os.getenv("MINIMAX_GROUP_ID")

    if not minimax_api_key:
        raise ValueError("❌ MINIMAX_API_KEY not found in environment variables")
    if not minimax_group_id:
        raise ValueError("❌ MINIMAX_GROUP_ID not found in environment variables")

    logger.info(f"✅ MiniMax credentials found: group_id={minimax_group_id[:10]}...")

    # Create aiohttp session for MiniMax TTS (shared across all sessions)
    connector = aiohttp.TCPConnector()
    aiohttp_session = aiohttp.ClientSession(connector=connector, trust_env=False)
    logger.info(f"✅ aiohttp session created (proxy disabled): {aiohttp_session}")

    # Define pipeline factory function for creating per-session pipelines
    async def create_session_pipeline(session_id: str) -> PipelineTask:
        """Create an independent pipeline for a specific session.

        Args:
            session_id: The Call-ID of the SIP session

        Returns:
            Configured and started PipelineTask for this session
        """
        logger.info(f"[{session_id[:8]}] Creating pipeline for session")

        # Create independent service instances for this session
        stt = TencentSegmentedSTTService(
            secret_id=os.getenv("TENCENT_SECRET_ID") or "",
            secret_key=os.getenv("TENCENT_SECRET_KEY") or "",
            appid=os.getenv("TENCENT_APPID") or "",
            engine_model_type="8k_zh",  # 8kHz Mandarin for telephony
            sample_rate=8000,
            filter_dirty=1,  # Enable profanity filtering
            noise_threshold=0.2,  # Server-side noise reduction
            audio_passthrough=False,  # Disable audio passthrough for SIP
        )

        # MiniMax TTS for SIP (8kHz sample rate, Chinese language)
        tts = MiniMaxTTSWithLogging(
            api_key=minimax_api_key,
            group_id=minimax_group_id,
            aiohttp_session=aiohttp_session,  # Share session across all instances
            base_url="https://api.minimaxi.com/v1/t2a_v2",
            sample_rate=8000,
            model="speech-2.6-turbo",
            params=MiniMaxTTSWithLogging.InputParams(language=Language.ZH_CN),
        )

        llm = GLMLLMService(
            api_key=os.getenv("ZHIPUAI_API_KEY") or "",
            model="glm-4-flashx-250414",
        )

        # Independent LLM context for this session
        messages: list = [
            {
                "role": "system",
                "content": "You are a friendly AI assistant. Respond naturally and keep your answers conversational.",
            },
            {
                "role": "user",
                "content": "Please say hello and briefly introduce yourself.",
            },
        ]
        context = LLMContext(messages)  # type: ignore
        context_aggregator = LLMContextAggregatorPair(context)

        # Create context pruner to limit memory usage
        context_pruner = LLMContextPruner(max_exchanges=10)

        # Create session-specific input/output processors
        pipeline = Pipeline(
            [
                transport.input(session_id),  # Session-specific input  # type: ignore
                stt,  # STT with server-side noise_threshold (no local pre-filtering)  # type: ignore
                context_aggregator.user(),  # User responses  # type: ignore
                context_pruner,  # Prune context before sending to LLM  # type: ignore
                llm,  # LLM
                tts,  # TTS
                transport.output(session_id),  # Session-specific output  # type: ignore
                context_aggregator.assistant(),  # Assistant spoken responses  # type: ignore
            ]
        )  # type: ignore

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

        logger.info(f"[{session_id[:8]}] ✅ Pipeline created")
        return task

    # Register the pipeline factory with transport
    transport.set_pipeline_factory(create_session_pipeline)  # type: ignore

    # Event handler for new sessions (called after ACK, pipeline already created)
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        session_id = client['call_id']
        logger.info(f"📞 [{session_id[:8]}] Session connected: {client}")

        # Get the session to access its pipeline
        session = await transport._session_manager.get_session(session_id)  # type: ignore
        if session and session.pipeline_task:
            # Kick off the conversation with a greeting
            await session.pipeline_task.queue_frames([LLMRunFrame()])
            logger.info(f"[{session_id[:8]}] Greeting queued")
        else:
            logger.error(f"[{session_id[:8]}] No pipeline task found for session")

    # Event handler for session termination
    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        session_id = client['call_id']
        reason = client.get('reason', 'unknown')
        logger.info(f"📴 [{session_id[:8]}] Session disconnected (reason: {reason})")

    # For SIP transport, we don't run a single task but let the transport manage sessions
    # Keep the runner alive to handle incoming calls
    logger.info("✅ Bot ready to accept SIP calls")
    # type: ignore for accessing custom transport attributes
    if hasattr(transport, '_params'):
        logger.info(f"📊 Listening on {transport._params.host}:{transport._params.port}")  # type: ignore

    try:
        # Create a dummy task that never completes (keeps runner alive)
        import asyncio
        await asyncio.Event().wait()  # Wait forever
    finally:
        # Cleanup aiohttp session on shutdown
        await aiohttp_session.close()
        logger.info("✅ aiohttp session closed")


async def bot(runner_args: RunnerArguments):
    """Main bot entry point for the bot starter."""

    params = SIPParams(
        host=os.getenv("SIP_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SIP_SERVER_PORT", "6060")),
        rtp_port_start=int(os.getenv("SIP_RTP_PORT_RANGE", "10000-15000").split("-")[0]),
        rtp_port_end=int(os.getenv("SIP_RTP_PORT_RANGE", "10000-15000").split("-")[1]),
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(
            sample_rate=8000,
            params=VADParams(
                stop_secs=0.5,     # Increase to 500ms to allow natural pauses in speech
                start_secs=0.3,    # Increase from default 0.2 to reduce false positives
                min_volume=0.7,    # Increase from default 0.6 to require louder speech
            )
        ),
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
        asyncio.run(bot(runner_args))  # type: ignore
    except KeyboardInterrupt:
        logger.info("\n🛑 Shutting down...")
        logger.info("✅ Bot stopped")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}", exc_info=True)
        sys.exit(1)
