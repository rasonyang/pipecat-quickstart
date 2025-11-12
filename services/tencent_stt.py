#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Tencent Cloud speech-to-text service implementation."""

import asyncio
import hashlib
import hmac
import json
import random
import time
from datetime import datetime
from typing import AsyncGenerator, Optional
from urllib.parse import urlencode

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

try:
    import websockets
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error("In order to use Tencent STT, you need to `pip install websockets`.")
    raise Exception(f"Missing module: {e}")


class TencentSTTService(STTService):
    """Tencent Cloud speech-to-text service.

    Provides real-time speech recognition using Tencent Cloud ASR WebSocket API.
    Supports configurable models, languages, interim results, hotword boosting,
    and profanity filtering.
    """

    def __init__(
        self,
        *,
        secret_id: str,
        secret_key: str,
        appid: str,
        engine_model_type: str = "8k_zh",
        sample_rate: Optional[int] = None,
        voice_format: int = 1,  # 1=PCM, 4=speex, 6=silk, 8=mp3, 10=opus, 12=wav, 14=m4a, 16=aac
        hotword_id: Optional[str] = None,
        filter_dirty: int = 0,  # 0=off, 1=filter, 2=only_filter
        filter_modal: int = 0,  # 0=off, 1=filter modal words
        filter_punc: int = 0,  # 0=off, 1=filter punctuation
        word_info: int = 0,  # 0=no word timestamps, 1=include word timestamps
        vad_silence_time: int = 1000,  # VAD silence timeout in ms
        audio_passthrough: bool = False,  # Prevent audio echo by default
        disable_proxy: bool = True,  # Disable proxy for direct connections by default
        lazy_connect: bool = False,  # Delay connection until explicitly called
        **kwargs,
    ):
        """Initialize the Tencent STT service.

        Args:
            secret_id: Tencent Cloud Secret ID for authentication.
            secret_key: Tencent Cloud Secret Key for authentication.
            appid: Tencent Cloud Application ID.
            engine_model_type: Recognition engine model (e.g., "8k_zh", "16k_zh", "16k_en").
            sample_rate: Audio sample rate. If None, inferred from engine_model_type.
            voice_format: Audio encoding format (1=PCM).
            hotword_id: Optional hotword list ID for vocabulary boosting.
            filter_dirty: Profanity filtering (0=off, 1=filter, 2=only_filter).
            filter_modal: Modal word filtering (0=off, 1=on).
            filter_punc: Punctuation filtering (0=off, 1=on).
            word_info: Include word-level timestamps (0=no, 1=yes).
            vad_silence_time: VAD silence timeout in milliseconds.
            audio_passthrough: Whether to pass audio frames downstream (default False to prevent echo).
            disable_proxy: Disable proxy for direct connections (default True).
            lazy_connect: Delay WebSocket connection until explicitly called (default False).
            **kwargs: Additional arguments passed to the parent STTService.
        """
        # Infer sample rate from engine model type if not provided
        if sample_rate is None:
            if "8k" in engine_model_type:
                sample_rate = 8000
            elif "16k" in engine_model_type:
                sample_rate = 16000
            else:
                sample_rate = 16000  # default

        super().__init__(sample_rate=sample_rate, audio_passthrough=audio_passthrough, **kwargs)

        self._secret_id = secret_id
        self._secret_key = secret_key
        self._appid = appid
        self._engine_model_type = engine_model_type
        self._voice_format = voice_format
        self._hotword_id = hotword_id
        self._filter_dirty = filter_dirty
        self._filter_modal = filter_modal
        self._filter_punc = filter_punc
        self._word_info = word_info
        self._vad_silence_time = vad_silence_time
        self._disable_proxy = disable_proxy
        self._lazy_connect = lazy_connect

        self._connection = None
        self._receive_task = None
        self._voice_id = None

        self.set_model_name(engine_model_type)

    def can_generate_metrics(self) -> bool:
        """Check if this service can generate processing metrics.

        Returns:
            True, as Tencent STT service supports metrics generation.
        """
        return True

    async def set_model(self, model: str):
        """Set the Tencent STT model and reconnect.

        Args:
            model: The Tencent engine model type to use.
        """
        await super().set_model(model)
        logger.info(f"Switching STT model to: [{model}]")
        self._engine_model_type = model
        await self.disconnect()
        await self.connect()

    async def set_language(self, language: Language):
        """Set the recognition language and reconnect.

        Args:
            language: The language to use for speech recognition.
        """
        logger.info(f"Switching STT language to: [{language}]")
        # Map Language enum to Tencent engine model types
        language_to_model = {
            Language.ZH: "16k_zh",
            Language.EN: "16k_en",
            # Add more mappings as needed
        }
        if language in language_to_model:
            self._engine_model_type = language_to_model[language]
            await self.disconnect()
            await self.connect()
        else:
            logger.warning(f"Language {language} not directly supported, keeping current model")

    async def start(self, frame: StartFrame):
        """Start the Tencent STT service.

        Args:
            frame: The start frame containing initialization parameters.
        """
        await super().start(frame)
        # Only auto-connect if lazy_connect is False
        if not self._lazy_connect:
            await self.connect()

    async def stop(self, frame: EndFrame):
        """Stop the Tencent STT service.

        Args:
            frame: The end frame.
        """
        await super().stop(frame)
        await self.disconnect()

    async def cancel(self, frame: CancelFrame):
        """Cancel the Tencent STT service.

        Args:
            frame: The cancel frame.
        """
        await super().cancel(frame)
        await self.disconnect()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Send audio data to Tencent for transcription.

        Args:
            audio: Raw audio bytes to transcribe.

        Yields:
            Frame: None (transcription results come via WebSocket callbacks).
        """
        if self._connection and not self._connection.closed:
            try:
                await self._connection.send(audio)
            except Exception as e:
                logger.error(f"{self}: Error sending audio: {e}")
                await self.push_error(ErrorFrame(f"Error sending audio: {e}"))
        yield None

    def _generate_signature(self, params: dict) -> str:
        """Generate TC3-HMAC-SHA256 signature for authentication.

        Args:
            params: Dictionary of request parameters.

        Returns:
            Base64-encoded signature string.
        """
        # Sort parameters by key
        sorted_params = sorted(params.items())
        query_string = "&".join([f"{k}={v}" for k, v in sorted_params if k != "signature"])

        # Create string to sign using HMAC-SHA1 (for WebSocket API v2)
        # Note: Despite TC3 being available, the WebSocket ASR API still uses the older signature method
        string_to_sign = f"asr.cloud.tencent.com/asr/v2/{self._appid}?{query_string}"

        # Generate signature
        signature = hmac.new(
            self._secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
        ).digest()

        import base64

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
        import uuid

        self._voice_id = str(uuid.uuid4())

        params = {
            "secretid": self._secret_id,
            "timestamp": str(timestamp),
            "expired": str(expired),
            "nonce": str(nonce),
            "engine_model_type": self._engine_model_type,
            "voice_id": self._voice_id,
            "voice_format": str(self._voice_format),
            "needvad": "1",  # Enable VAD
            "vad_silence_time": str(self._vad_silence_time),
        }

        # Add optional parameters
        if self._hotword_id:
            params["hotword_id"] = self._hotword_id
        if self._filter_dirty:
            params["filter_dirty"] = str(self._filter_dirty)
        if self._filter_modal:
            params["filter_modal"] = str(self._filter_modal)
        if self._filter_punc:
            params["filter_punc"] = str(self._filter_punc)
        if self._word_info:
            params["word_info"] = str(self._word_info)

        # Generate signature
        signature = self._generate_signature(params)
        params["signature"] = signature

        return params

    async def connect(self):
        """Establish WebSocket connection to Tencent ASR service.

        This method can be called manually to establish connection when lazy_connect is True.
        """
        logger.debug("Connecting to Tencent ASR")

        params = self._build_connection_params()
        query_string = urlencode(params)
        url = f"wss://asr.cloud.tencent.com/asr/v2/{self._appid}?{query_string}"

        try:
            # Note: websockets v13.x doesn't support proxy parameter
            # Use environment variables (no_proxy, https_proxy) to control proxy behavior
            self._connection = await websockets.connect(url)
            logger.debug(f"{self}: Connected to Tencent ASR")

            # Start receiving messages
            self._receive_task = asyncio.create_task(self._receive_messages())

        except Exception as e:
            logger.error(f"{self}: Failed to connect to Tencent ASR: {e}", exc_info=True)
            logger.error(f"{self}: Connection URL (truncated): wss://asr.cloud.tencent.com/asr/v2/{self._appid}?...")
            await self.push_error(ErrorFrame(f"Failed to connect: {e}"))

    async def disconnect(self):
        """Disconnect from Tencent ASR service.

        This method can be called manually to close connection when needed.
        """
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._connection and not self._connection.closed:
            logger.debug("Disconnecting from Tencent ASR")
            await self._connection.close()
            self._connection = None

    async def _receive_messages(self):
        """Receive and process messages from Tencent ASR WebSocket."""
        try:
            async for message in self._connection:
                try:
                    data = json.loads(message)
                    await self._on_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"{self}: Failed to decode message: {e}")
                except Exception as e:
                    logger.error(f"{self}: Error processing message: {e}")
        except asyncio.CancelledError:
            logger.debug(f"{self}: Receive task cancelled")
        except Exception as e:
            logger.error(f"{self}: Error in receive loop: {e}")
            await self.push_error(ErrorFrame(f"Receive error: {e}"))

    async def start_metrics(self):
        """Start TTFB and processing metrics collection."""
        await self.start_ttfb_metrics()
        await self.start_processing_metrics()

    async def _on_message(self, data: dict):
        """Handle incoming message from Tencent ASR.

        Args:
            data: Parsed JSON message from Tencent ASR.
        """
        code = data.get("code", -1)
        if code != 0:
            error_msg = data.get("message", "Unknown error")
            logger.error(f"{self}: Tencent ASR error: {code} - {error_msg}")
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
        # slice_type: 0=sentence start, 1=sentence middle, 2=sentence end
        slice_type = result.get("slice_type", 0)
        is_final = slice_type == 2

        # Stop TTFB metrics on first transcription
        await self.stop_ttfb_metrics()

        # Determine language from engine model
        language = None
        if "zh" in self._engine_model_type:
            language = Language.ZH
        elif "en" in self._engine_model_type:
            language = Language.EN

        if is_final:
            # Push final transcription
            await self.push_frame(
                TranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=result,
                )
            )
            await self.stop_processing_metrics()
        else:
            # Push interim transcription
            await self.push_frame(
                InterimTranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=result,
                )
            )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames with Tencent-specific handling.

        Args:
            frame: The frame to process.
            direction: The direction of frame processing.
        """
        await super().process_frame(frame, direction)
