#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tencent Cloud speech-to-text service with local VAD segmentation.

This module provides a segmented STT service for Tencent Cloud that uses local VAD
(Voice Activity Detection) to control when to end recognition. It uses WebSocket
connections with needvad=0, allowing local VAD to trigger recognition end while
still receiving real-time interim results.

Benefits:
- Lower latency (no server-side VAD wait time)
- Local control over speech segmentation
- Real-time interim results during recognition

Trade-offs:
- WebSocket connection rebuilt for each speech segment
- Connection management complexity
"""

import asyncio
import base64
import hashlib
import hmac
import json
import random
import time
import uuid
from typing import AsyncGenerator, Optional
from urllib.parse import urlencode

from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error("In order to use Tencent STT, you need to `pip install websockets`.")
    raise Exception(f"Missing module: {e}")


class TencentSegmentedSTTService(STTService):
    """Tencent Cloud STT service with local VAD-based segmentation.

    This service uses local VAD to control speech segmentation via WebSocket with needvad=0.
    Each speech segment triggers a new WebSocket connection that is maintained during recognition
    and closed after the final result is received.

    Example usage:
        ```python
        stt = TencentSegmentedSTTService(
            secret_id=os.getenv("TENCENT_SECRET_ID"),
            secret_key=os.getenv("TENCENT_SECRET_KEY"),
            appid=os.getenv("TENCENT_APPID"),
            engine_model_type="8k_zh",
            sample_rate=8000,
        )
        ```

    Args:
        secret_id: Tencent Cloud Secret ID for authentication
        secret_key: Tencent Cloud Secret Key for authentication
        appid: Tencent Cloud Application ID
        engine_model_type: Recognition engine model (e.g., "8k_zh", "16k_zh", "16k_en")
        sample_rate: Audio sample rate. If None, inferred from engine_model_type
        filter_dirty: Profanity filtering (0=off, 1=filter, 2=only_filter)
        noise_threshold: Noise suppression threshold (-1.0 to 1.0)
        **kwargs: Additional parameters passed to STTService
    """

    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        appid: str,
        engine_model_type: str = "8k_zh",
        sample_rate: Optional[int] = None,
        filter_dirty: int = 0,
        noise_threshold: Optional[float] = None,
        **kwargs,
    ):
        """Initialize Tencent Segmented STT service.

        Args:
            secret_id: Tencent Cloud Secret ID.
            secret_key: Tencent Cloud Secret Key.
            appid: Tencent Cloud Application ID.
            engine_model_type: Recognition engine model.
            sample_rate: Audio sample rate (inferred from model if None).
            filter_dirty: Profanity filtering level.
            noise_threshold: Noise suppression threshold.
            **kwargs: Additional parameters for STTService.
        """
        # Infer sample rate from engine model type if not provided
        if sample_rate is None:
            if "8k" in engine_model_type:
                sample_rate = 8000
            elif "16k" in engine_model_type:
                sample_rate = 16000
            else:
                sample_rate = 16000  # default

        super().__init__(sample_rate=sample_rate, **kwargs)

        self._secret_id = secret_id
        self._secret_key = secret_key
        self._appid = appid
        self._engine_model_type = engine_model_type
        self._filter_dirty = filter_dirty
        self._noise_threshold = noise_threshold

        self._connection = None
        self._receive_task = None
        self._voice_id = None
        self._user_speaking = False
        self._recognition_active = False
        self._stt_start_time = None

        self.set_model_name(engine_model_type)

        logger.info(
            f"TencentSegmentedSTTService initialized: model={engine_model_type}, "
            f"sample_rate={sample_rate}Hz (WebSocket + needvad=0, local VAD control)"
        )

    def can_generate_metrics(self) -> bool:
        """Check if this service can generate processing metrics.

        Returns:
            True, as Tencent STT service supports metrics generation.
        """
        return True

    async def set_model(self, model: str):
        """Set the Tencent STT model.

        Args:
            model: The Tencent engine model type to use.
        """
        await super().set_model(model)
        logger.info(f"Switching STT model to: [{model}]")
        self._engine_model_type = model

    async def set_language(self, language: Language):
        """Set the recognition language.

        Args:
            language: The language to use for speech recognition.
        """
        logger.info(f"Switching STT language to: [{language}]")
        # Map Language enum to Tencent engine model types
        language_to_model = {
            Language.ZH: "16k_zh",
            Language.EN: "16k_en",
        }
        if language in language_to_model:
            self._engine_model_type = language_to_model[language]
        else:
            logger.warning(f"Language {language} not directly supported, keeping current model")

    def _generate_signature(self, params: dict) -> str:
        """Generate HMAC-SHA1 signature for authentication.

        Args:
            params: Dictionary of request parameters.

        Returns:
            Base64-encoded signature string.
        """
        # Sort parameters by key
        sorted_params = sorted(params.items())
        query_string = "&".join([f"{k}={v}" for k, v in sorted_params if k != "signature"])

        # Create string to sign
        string_to_sign = f"asr.cloud.tencent.com/asr/v2/{self._appid}?{query_string}"

        # Generate signature
        signature = hmac.new(
            self._secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1
        ).digest()

        return base64.b64encode(signature).decode("utf-8")

    def _build_connection_params(self) -> dict:
        """Build WebSocket connection parameters with authentication.

        Returns:
            Dictionary of connection parameters.
        """
        timestamp = int(time.time())
        expired = timestamp + 86400  # 24 hours
        nonce = random.randint(1, 9999999999)

        # Generate unique voice_id for this session
        self._voice_id = str(uuid.uuid4())

        params = {
            "secretid": self._secret_id,
            "timestamp": str(timestamp),
            "expired": str(expired),
            "nonce": str(nonce),
            "engine_model_type": self._engine_model_type,
            "voice_id": self._voice_id,
            "voice_format": "1",  # PCM
            "needvad": "0",  # Disable server-side VAD, use local VAD
        }

        # Add optional parameters
        if self._filter_dirty:
            params["filter_dirty"] = str(self._filter_dirty)
        if self._noise_threshold is not None:
            params["noise_threshold"] = str(self._noise_threshold)

        # Generate signature
        signature = self._generate_signature(params)
        params["signature"] = signature

        return params

    async def _connect(self):
        """Establish WebSocket connection to Tencent ASR service."""
        if self._connection and not self._connection.closed:
            logger.warning("WebSocket already connected, skipping")
            return

        logger.debug(f"Connecting to Tencent ASR (needvad=0)")

        params = self._build_connection_params()
        query_string = urlencode(params)
        url = f"wss://asr.cloud.tencent.com/asr/v2/{self._appid}?{query_string}"

        try:
            self._connection = await websockets.connect(url)
            logger.debug(f"Connected to Tencent ASR (voice_id={self._voice_id[:8]}...)")

            # Start receiving messages
            self._receive_task = asyncio.create_task(self._receive_messages())
            self._recognition_active = True

        except Exception as e:
            logger.error(f"Failed to connect to Tencent ASR: {e}", exc_info=True)
            await self.push_error(ErrorFrame(f"Failed to connect: {e}"))

    async def _disconnect(self):
        """Gracefully disconnect from Tencent ASR service."""
        # Step 1: Close WebSocket connection first (send close frame)
        if self._connection and not self._connection.closed:
            logger.debug("Closing WebSocket connection")
            try:
                await self._connection.close()
            except Exception as e:
                logger.debug(f"Error during WebSocket close: {e}")
            self._connection = None

        # Step 2: Cancel receive task after connection is closed
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        self._recognition_active = False

    async def _send_end_signal(self):
        """Send end signal to trigger final recognition result."""
        if self._connection and not self._connection.closed:
            try:
                end_signal = json.dumps({"type": "end"})
                await self._connection.send(end_signal)
                logger.debug("Sent end signal to Tencent STT")
            except Exception as e:
                logger.error(f"Error sending end signal: {e}")

    async def _receive_messages(self):
        """Receive and process messages from Tencent ASR WebSocket."""
        try:
            async for message in self._connection:
                try:
                    data = json.loads(message)
                    await self._on_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode message: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
        except ConnectionClosed:
            # Normal connection closure (after final=1 or explicit close)
            logger.debug("WebSocket connection closed normally")
        except asyncio.CancelledError:
            logger.debug("Receive task cancelled")
        except Exception as e:
            # Unexpected errors only
            logger.error(f"Error in receive loop: {e}")
            await self.push_error(ErrorFrame(f"Receive error: {e}"))

    async def _on_message(self, data: dict):
        """Handle incoming message from Tencent ASR.

        Args:
            data: Parsed JSON message from Tencent ASR.
        """
        code = data.get("code", -1)
        if code != 0:
            error_msg = data.get("message", "Unknown error")
            logger.error(f"Tencent ASR error: {code} - {error_msg}")
            await self.push_error(ErrorFrame(f"ASR error {code}: {error_msg}"))
            return

        result = data.get("result")
        if not result:
            return

        # Extract transcription
        transcript = result.get("voice_text_str", "")
        if not transcript:
            return

        # Determine if this is a final or interim result
        slice_type = result.get("slice_type", 0)
        final = data.get("final", 0)

        # Determine language from engine model
        language = None
        if "zh" in self._engine_model_type:
            language = Language.ZH
        elif "en" in self._engine_model_type:
            language = Language.EN

        # Check if this is the final result (final=1 indicates connection will close)
        if final == 1:
            duration_ms = int((time.time() - self._stt_start_time) * 1000) if self._stt_start_time else 0
            logger.info(f"🎯 STT Final: {transcript} (⏱️ {duration_ms}ms)")
            await self.push_frame(
                TranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=result,
                )
            )
            # Connection will close automatically after final result
            await self._disconnect()
        elif slice_type == 2:
            # Sentence end (but not final) - this is the normal case in needvad=0 mode
            duration_ms = int((time.time() - self._stt_start_time) * 1000) if self._stt_start_time else 0
            logger.info(f"📝 STT Sentence: {transcript} (⏱️ {duration_ms}ms)")
            await self.push_frame(
                TranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=result,
                )
            )
        else:
            # Interim result
            logger.debug(f"Interim transcription: {transcript}")
            await self.push_frame(
                InterimTranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=result,
                )
            )

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Required abstract method implementation (not used in this service).

        This service uses WebSocket streaming instead of batch processing,
        so this method is not called. Audio is sent frame-by-frame via
        process_frame().

        Args:
            audio: Audio bytes (unused).

        Yields:
            No frames (this method is not used).
        """
        # This method is required by STTService but not used in our implementation
        # We handle audio streaming directly in process_frame()
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames with Tencent-specific handling.

        Args:
            frame: The frame to process.
            direction: The direction of frame processing.
        """
        await super().process_frame(frame, direction)

        # Handle user started speaking - establish WebSocket connection
        if isinstance(frame, UserStartedSpeakingFrame):
            if not frame.emulated:
                self._user_speaking = True
                self._stt_start_time = time.time()
                logger.info("🎤 STT Started")
                await self._connect()
                await self.start_ttfb_metrics()

        # Handle user stopped speaking - send end signal
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if not frame.emulated:
                self._user_speaking = False
                logger.debug("User stopped speaking - sending end signal")
                await self._send_end_signal()

        # Handle audio frames - send to WebSocket if connected
        elif isinstance(frame, AudioRawFrame):
            # Store user_id if available
            if hasattr(frame, "user_id"):
                self._user_id = frame.user_id

            # Send audio to WebSocket if connection is active
            if self._connection and not self._connection.closed and self._recognition_active:
                try:
                    await self._connection.send(frame.audio)
                except Exception as e:
                    logger.error(f"Error sending audio: {e}")
                    await self.push_error(ErrorFrame(f"Error sending audio: {e}"))

            # Pass through audio if configured
            if self._audio_passthrough:
                await self.push_frame(frame, direction)

    async def stop(self, frame: EndFrame):
        """Stop the Tencent STT service.

        Args:
            frame: The end frame.
        """
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        """Cancel the Tencent STT service.

        Args:
            frame: The cancel frame.
        """
        await super().cancel(frame)
        await self._disconnect()
