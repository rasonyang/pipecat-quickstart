#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""SIP Transport Implementation.

Provides SIP server functionality with RTP audio streaming for Pipecat bots.
Supports incoming SIP calls with G.711 μ-law codec.
"""

import asyncio
import audioop
import os
import random
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from loguru import logger
from pipecat.frames.frames import AudioRawFrame, CancelFrame, EndFrame, Frame, StartFrame
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

# Forward declaration for type hints
if TYPE_CHECKING:
    from pipecat.pipeline.task import PipelineTask


class SIPParams(TransportParams):
    """Configuration parameters for SIP transport.

    Args:
        host: SIP server bind address (default: 0.0.0.0)
        port: SIP server port (default: 6060)
        rtp_port_start: Start of RTP port range (default: 10000)
        rtp_port_end: End of RTP port range (default: 15000)
    """

    host: str = "0.0.0.0"
    port: int = 6060
    rtp_port_start: int = 10000
    rtp_port_end: int = 15000

    def model_post_init(self, __context):
        """Pydantic post-initialization hook."""
        super().model_post_init(__context)

        # Set audio parameters for G.711
        self.audio_in_enabled = True
        self.audio_out_enabled = True
        self.audio_in_sample_rate = 8000  # G.711 sample rate
        self.audio_out_sample_rate = 8000

        # Validate configuration
        if self.rtp_port_end <= self.rtp_port_start:
            raise ValueError("rtp_port_end must be greater than rtp_port_start")
        if self.rtp_port_end - self.rtp_port_start < 10:
            raise ValueError("RTP port range must have at least 10 ports")


@dataclass
class SIPMessage:
    """Parsed SIP message."""
    method: str
    uri: str
    headers: dict[str, str]
    body: str
    raw: bytes


@dataclass
class SDPSession:
    """Parsed SDP session information."""
    audio_port: int
    audio_ip: str
    codecs: list[int]


@dataclass
class SIPSession:
    """Complete state of a single SIP call session.

    Each SIP call is represented by a unique Call-ID and has its own:
    - RTP session for audio streaming
    - Pipeline task for processing (STT/LLM/TTS)
    - Activity tracking for timeout detection
    """
    call_id: str                                    # SIP Call-ID (unique identifier)
    remote_ip: str                                  # Remote party IP address
    remote_port: int                                # Remote RTP port
    local_rtp_port: int                             # Local RTP port
    rtp_session: 'RTPSession'                       # RTP session instance
    pipeline_task: Optional['PipelineTask']         # Independent pipeline instance
    created_at: float                               # Session creation time (time.time())
    last_rtp_activity: float                        # Last RTP packet timestamp
    sip_headers: dict[str, str]                     # SIP headers (From/To/etc)
    state: str = "establishing"                     # State: establishing/active/terminating
    receive_task: Optional[asyncio.Task] = None     # Audio receive background task


class RTPPortPool:
    """Manages allocation of RTP ports from a configured range."""

    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end
        self.allocated: set[int] = set()
        self.lock = asyncio.Lock()

    def _test_port_bindable(self, port: int) -> bool:
        """Test if a port can actually be bound (not occupied by external process).

        Args:
            port: The port number to test.

        Returns:
            True if port is bindable, False if occupied or error.
        """
        test_sock = None
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_sock.bind(('0.0.0.0', port))
            return True
        except OSError:
            # Port is occupied or bind failed
            return False
        finally:
            if test_sock:
                test_sock.close()

    async def allocate(self) -> int:
        """Allocate an available port from the pool.

        Tests both memory allocation and actual bindability to prevent
        port leaks when external processes occupy ports.

        Returns:
            An available port number.

        Raises:
            RuntimeError: If no bindable ports available after 100 attempts.
        """
        async with self.lock:
            for _ in range(100):  # Try 100 random ports
                port = random.randint(self.start, self.end)
                # Check both memory allocation AND actual bindability
                if port not in self.allocated and self._test_port_bindable(port):
                    self.allocated.add(port)
                    return port
            raise RuntimeError("No available RTP ports in pool (all occupied or not bindable)")

    async def release(self, port: int) -> None:
        """Release a port back to the pool."""
        async with self.lock:
            self.allocated.discard(port)


class SIPSessionManager:
    """Manages multiple concurrent SIP call sessions.

    Responsibilities:
    - Map Call-ID to SIPSession instances
    - Create and destroy sessions
    - Track RTP activity for timeout detection
    - Automatically cleanup idle sessions (60s no RTP)
    - Provide session statistics
    """

    def __init__(self, port_pool: RTPPortPool, on_session_timeout_callback: Callable):
        self._sessions: dict[str, SIPSession] = {}  # Call-ID → Session
        self._port_pool = port_pool
        self._lock = asyncio.Lock()
        self._timeout_seconds = 60  # Timeout after 60s of no RTP activity
        self._timeout_task: Optional[asyncio.Task] = None
        self._on_timeout = on_session_timeout_callback
        self._running = False

    async def create_session(self, call_id: str, call_info: dict) -> SIPSession:
        """Create a new SIP session with allocated RTP port.

        Args:
            call_id: SIP Call-ID header value
            call_info: Dictionary containing remote_ip, remote_port, headers, addr

        Returns:
            Created SIPSession instance

        Raises:
            RuntimeError: If session with same Call-ID already exists or port allocation fails
        """
        async with self._lock:
            if call_id in self._sessions:
                raise RuntimeError(f"Session {call_id} already exists")

            # Track allocated resources for rollback on failure
            rtp_port: Optional[int] = None
            rtp_session: Optional[RTPSession] = None

            try:
                # Allocate RTP port
                rtp_port = await self._port_pool.allocate()

                # Create RTP session
                rtp_session = RTPSession(
                    local_port=rtp_port,
                    remote_ip=call_info['remote_ip'],
                    remote_port=call_info['remote_port']
                )
                await rtp_session.start()

                # Create SIP session object
                current_time = time.time()
                session = SIPSession(
                    call_id=call_id,
                    remote_ip=call_info['remote_ip'],
                    remote_port=call_info['remote_port'],
                    local_rtp_port=rtp_port,
                    rtp_session=rtp_session,
                    pipeline_task=None,  # Will be set later by SIPProtocol after ACK
                    created_at=current_time,
                    last_rtp_activity=current_time,
                    sip_headers=call_info.get('headers', {}),
                    state="establishing"
                )

                self._sessions[call_id] = session
                logger.info(f"✅ Session {call_id} created (RTP port {rtp_port})")
                logger.info(f"📊 Active sessions: {len(self._sessions)}")

                return session

            except Exception as e:
                # CRITICAL: Rollback allocated resources to prevent port leak
                logger.error(f"❌ Failed to create session {call_id}: {e}")

                # Cleanup: Stop RTP session if it was created
                if rtp_session:
                    try:
                        await rtp_session.stop()
                    except Exception as stop_err:
                        logger.error(f"Error stopping RTP session during rollback: {stop_err}")

                # Cleanup: Release port if it was allocated (PREVENTS PORT LEAK)
                if rtp_port:
                    await self._port_pool.release(rtp_port)
                    logger.info(f"♻️ Released port {rtp_port} after failed session creation")

                # Re-raise the original exception
                raise

    async def get_session(self, call_id: str) -> Optional[SIPSession]:
        """Get session by Call-ID.

        Args:
            call_id: SIP Call-ID to lookup

        Returns:
            SIPSession if found, None otherwise
        """
        async with self._lock:
            return self._sessions.get(call_id)

    async def remove_session(self, call_id: str) -> None:
        """Remove and cleanup a session.

        Args:
            call_id: SIP Call-ID to remove

        This method:
        - Stops RTP session
        - Cancels pipeline task
        - Releases RTP port
        - Cancels receive task
        """
        async with self._lock:
            session = self._sessions.pop(call_id, None)
            if not session:
                logger.warning(f"Session {call_id} not found for removal")
                return

            logger.info(f"🗑️ Removing session {call_id}")

            # Stop RTP session
            if session.rtp_session:
                await session.rtp_session.stop()

            # Release RTP port
            await self._port_pool.release(session.local_rtp_port)

            # Cancel pipeline task
            if session.pipeline_task:
                await session.pipeline_task.cancel()

            # Cancel receive task
            if session.receive_task:
                session.receive_task.cancel()
                try:
                    await session.receive_task
                except asyncio.CancelledError:
                    pass

            logger.info(f"✅ Session {call_id} removed (RTP port {session.local_rtp_port} released)")
            logger.info(f"📊 Active sessions: {len(self._sessions)}")

    async def update_rtp_activity(self, call_id: str) -> None:
        """Update last RTP activity timestamp for a session.

        Args:
            call_id: SIP Call-ID

        Called every time an RTP packet is received to prevent timeout.
        """
        async with self._lock:
            session = self._sessions.get(call_id)
            if session:
                session.last_rtp_activity = time.time()

    async def start_timeout_monitor(self) -> None:
        """Start background task to monitor session timeouts.

        Scans all sessions every 10 seconds and cleans up any session
        that has had no RTP activity for more than timeout_seconds (60s).
        """
        self._running = True
        self._timeout_task = asyncio.create_task(self._timeout_monitor_loop())
        logger.info(f"✅ Session timeout monitor started (timeout={self._timeout_seconds}s)")

    async def stop_timeout_monitor(self) -> None:
        """Stop the timeout monitoring task."""
        self._running = False
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass
            self._timeout_task = None
        logger.info("Session timeout monitor stopped")

    async def _timeout_monitor_loop(self) -> None:
        """Background loop to check for timed out sessions."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                current_time = time.time()
                timed_out_sessions = []

                async with self._lock:
                    for call_id, session in self._sessions.items():
                        idle_time = current_time - session.last_rtp_activity
                        if idle_time > self._timeout_seconds:
                            logger.warning(
                                f"⏰ Session {call_id} timed out "
                                f"(idle for {idle_time:.1f}s > {self._timeout_seconds}s)"
                            )
                            timed_out_sessions.append(call_id)

                # Cleanup timed out sessions (outside lock to avoid deadlock)
                for call_id in timed_out_sessions:
                    session = await self.get_session(call_id)
                    if session:
                        await self._on_timeout(session)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in timeout monitor: {e}")

    async def stop_all_sessions(self) -> None:
        """Stop and cleanup all active sessions.

        Called during transport shutdown.
        """
        async with self._lock:
            call_ids = list(self._sessions.keys())

        for call_id in call_ids:
            await self.remove_session(call_id)

    def get_active_session_count(self) -> int:
        """Get number of currently active sessions.

        Returns:
            Count of active sessions
        """
        return len(self._sessions)


class RTPPacket:
    """RTP packet builder and parser."""

    def __init__(
        self,
        payload_type: int = 0,  # 0 = PCMU (G.711 μ-law)
        sequence: int = 0,
        timestamp: int = 0,
        ssrc: int = 0
    ):
        self.payload_type = payload_type
        self.sequence = sequence
        self.timestamp = timestamp
        self.ssrc = ssrc

    def build(self, payload: bytes) -> bytes:
        """Build RTP packet with header and payload."""
        # RTP header: version(2), padding(1), extension(1), cc(4), marker(1), pt(7)
        # sequence(16), timestamp(32), ssrc(32)
        header = struct.pack(
            '!BBHII',
            0x80,  # Version 2, no padding, no extension, no CSRC
            self.payload_type,  # No marker, payload type
            self.sequence & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc & 0xFFFFFFFF
        )
        return header + payload

    @staticmethod
    def parse(packet: bytes) -> tuple[bytes, int, int]:
        """Parse RTP packet and return payload, sequence, timestamp."""
        if len(packet) < 12:
            raise ValueError("RTP packet too short")

        header = struct.unpack('!BBHII', packet[:12])
        sequence = header[2]
        timestamp = header[3]
        payload = packet[12:]

        return payload, sequence, timestamp


class RTPSession:
    """RTP audio session for bidirectional streaming."""

    def __init__(self, local_port: int, remote_ip: str, remote_port: int):
        self.local_port = local_port
        self.remote_ip = remote_ip
        self.remote_port = remote_port

        self.sock: Optional[socket.socket] = None
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional['RTPProtocol'] = None

        # Queues with backpressure: blocking put() prevents overflow
        # ~2 seconds of audio at 20ms per packet = 100 packets
        self.tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self.rx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

        self.sequence = random.randint(0, 65535)
        self.timestamp = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)

        self.running = False
        self.tasks: list[asyncio.Task] = []

        # Track send loop start time to ignore startup transient drift
        self._loop_start_time: Optional[float] = None

    async def start(self) -> None:
        """Start RTP session.

        Raises:
            RuntimeError: If socket binding fails (port occupied or permission denied).
        """
        loop = asyncio.get_event_loop()

        # Create UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

        # Attempt to bind socket with error handling
        try:
            self.sock.bind(('0.0.0.0', self.local_port))
        except OSError as e:
            # Cleanup socket on bind failure to prevent resource leak
            if self.sock:
                self.sock.close()
                self.sock = None
            error_msg = f"Failed to bind RTP port {self.local_port}: {e.strerror} (errno {e.errno})"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

        # Create protocol
        self.transport, self.protocol = await loop.create_datagram_endpoint(
            lambda: RTPProtocol(self.rx_queue),
            sock=self.sock
        )

        self.running = True

        # Start send/receive tasks
        self.tasks.append(asyncio.create_task(self._send_loop()))
        self.tasks.append(asyncio.create_task(self._receive_loop()))

        logger.info(f"RTP session started on port {self.local_port}")

    async def stop(self) -> None:
        """Stop RTP session and cleanup resources."""
        self.running = False

        # Cancel tasks
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

        # Close transport
        if self.transport:
            self.transport.close()

        # Close socket
        if self.sock:
            self.sock.close()

        logger.info(f"RTP session stopped on port {self.local_port}")

    async def send_audio(self, pcm_audio: bytes, sample_rate: int = 8000) -> None:
        """Send PCM16 audio (will be converted to G.711 μ-law).

        Args:
            pcm_audio: PCM16 audio data
            sample_rate: Sample rate of input audio (will be resampled to 8kHz if needed)
        """
        if not self.running:
            logger.warning("RTP session not running, cannot send audio")
            return

        try:
            original_size = len(pcm_audio)

            # Resample to 8kHz if needed
            if sample_rate != 8000:
                logger.debug(f"Resampling from {sample_rate}Hz to 8000Hz")
                pcm_audio = audioop.ratecv(pcm_audio, 2, 1, sample_rate, 8000, None)[0]

            # Convert PCM16 to G.711 μ-law
            ulaw_audio = audioop.lin2ulaw(pcm_audio, 2)

            # Split into 160-byte (20ms) chunks for RTP
            chunk_size = 160  # 20ms at 8kHz
            chunk_count = 0
            for i in range(0, len(ulaw_audio), chunk_size):
                chunk = ulaw_audio[i:i + chunk_size]

                # Pad last chunk if needed
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\xFF' * (chunk_size - len(chunk))

                # Add to send queue (blocking to create backpressure)
                await self.tx_queue.put(chunk)
                chunk_count += 1

            logger.debug(f"Queued {chunk_count} RTP chunks ({original_size} bytes PCM -> {len(ulaw_audio)} bytes μ-law)")

        except Exception as e:
            logger.error(f"Error sending audio: {e}", exc_info=True)

    async def receive_audio(self) -> bytes:
        """Receive G.711 μ-law audio (will be converted to PCM16)."""
        ulaw_audio = await self.rx_queue.get()

        # Convert G.711 μ-law to PCM16
        pcm_audio = audioop.ulaw2lin(ulaw_audio, 2)
        return pcm_audio

    async def _send_loop(self) -> None:
        """Send RTP packets at 20ms intervals."""
        interval = 0.020  # 20ms
        loop = asyncio.get_event_loop()
        next_send_time = loop.time() + interval

        # Record loop start time to ignore startup transient drift
        if self._loop_start_time is None:
            self._loop_start_time = loop.time()

        while self.running:
            try:
                current_time = loop.time()

                # Wait until next send time
                wait_time = next_send_time - current_time
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                # Check for severe drift (more than 100ms behind)
                # Ignore drift during first second (startup transient)
                current_time = loop.time()
                drift = current_time - next_send_time
                elapsed_since_start = current_time - self._loop_start_time
                if drift > 0.1 and elapsed_since_start > 1.0:
                    logger.warning(f"RTP send timing drift detected: {drift*1000:.1f}ms behind, resetting timer")
                    # Reset timer but continue sending to avoid packet loss
                    next_send_time = current_time + interval

                # Get audio or send silence
                try:
                    audio = self.tx_queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Send silence frame (160 bytes of 0xFF for μ-law silence)
                    audio = b'\xFF' * 160

                # Build and send RTP packet
                packet = RTPPacket(
                    payload_type=0,  # PCMU
                    sequence=self.sequence,
                    timestamp=self.timestamp,
                    ssrc=self.ssrc
                ).build(audio)

                if self.transport:
                    self.transport.sendto(packet, (self.remote_ip, self.remote_port))
                    # Log periodically to verify sending
                    if self.sequence % 50 == 0:
                        logger.info(f"📡 RTP sending: seq={self.sequence}, {len(packet)} bytes to {self.remote_ip}:{self.remote_port}")

                # Update sequence and timestamp
                self.sequence = (self.sequence + 1) & 0xFFFF
                self.timestamp = (self.timestamp + 160) & 0xFFFFFFFF  # 160 samples per 20ms

                # Calculate next send time
                next_send_time += interval

            except Exception as e:
                if self.running:
                    logger.error(f"Error in RTP send loop: {e}")

    async def _receive_loop(self) -> None:
        """Receive RTP packets."""
        # Reception is handled by RTPProtocol.datagram_received
        # This loop just keeps the task alive
        while self.running:
            await asyncio.sleep(1)


class RTPProtocol(asyncio.DatagramProtocol):
    """Asyncio protocol for receiving RTP packets."""

    def __init__(self, rx_queue: asyncio.Queue):
        self.rx_queue = rx_queue

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        """Handle received RTP packet."""
        try:
            payload, seq, ts = RTPPacket.parse(data)

            # Log source address periodically to diagnose audio routing
            if seq % 50 == 0:
                logger.info(f"📡 RTP RX: from {addr[0]}:{addr[1]}, seq={seq}, {len(payload)} bytes")

            # Add to receive queue (drop if full)
            try:
                self.rx_queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop oldest frame
                try:
                    self.rx_queue.get_nowait()
                    self.rx_queue.put_nowait(payload)
                except asyncio.QueueEmpty:
                    pass
        except Exception as e:
            logger.error(f"Error processing RTP packet: {e}")


class SIPServer:
    """SIP server for handling incoming calls."""

    def __init__(self, host: str, port: int, on_call_handler, transport_instance=None):
        self.host = host
        self.port = port
        self.on_call_handler = on_call_handler
        self.transport_instance = transport_instance  # SIPTransport instance for event handling

        self.sock: Optional[socket.socket] = None
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional['SIPProtocol'] = None
        self.running = False

    async def start(self) -> None:
        """Start SIP server."""
        loop = asyncio.get_event_loop()

        # Create UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setblocking(False)

        try:
            self.sock.bind((self.host, self.port))
            logger.info(f"Socket bound to {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to bind socket: {e}")
            raise

        # Create protocol
        try:
            self.transport, self.protocol = await loop.create_datagram_endpoint(
                lambda: SIPProtocol(self.on_call_handler, self),  # Pass self for transport_instance access
                sock=self.sock
            )
            logger.info("SIP protocol created successfully")
        except Exception as e:
            logger.error(f"Failed to create datagram endpoint: {e}")
            raise

        self.running = True
        logger.info(f"✅ SIP server started on {self.host}:{self.port}")

        # Initialize VAD analyzer if present
        if self.transport_instance and hasattr(self.transport_instance, '_params'):
            if self.transport_instance._params.vad_analyzer:
                logger.info("Initializing VAD analyzer with sample_rate=8000")
                self.transport_instance._params.vad_analyzer.set_sample_rate(8000)
                logger.info("✅ VAD analyzer initialized")

        # Log socket info
        local_addr = self.sock.getsockname()
        logger.info(f"Listening on {local_addr}")

    async def stop(self) -> None:
        """Stop SIP server."""
        self.running = False

        if self.transport:
            self.transport.close()

        if self.sock:
            self.sock.close()

        logger.info("SIP server stopped")


class SIPProtocol(asyncio.DatagramProtocol):
    """Asyncio protocol for handling SIP messages."""

    def __init__(self, on_call_handler, sip_server):
        self.on_call_handler = on_call_handler
        self.sip_server = sip_server  # Reference to SIPServer for accessing transport_instance
        self.transport: Optional[asyncio.DatagramTransport] = None
        logger.info("SIPProtocol initialized")

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        """Called when transport is ready."""
        self.transport = transport
        logger.info("SIP protocol connection made")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        """Handle received SIP message."""
        logger.info(f"🔵 DATAGRAM RECEIVED from {addr}: {len(data)} bytes")
        logger.debug(f"Raw data: {data[:200]}...")

        try:
            msg = self._parse_sip_message(data)
            logger.info(f"✅ Parsed SIP {msg.method} request from {addr}")
            asyncio.create_task(self._handle_message(msg, addr))
        except Exception as e:
            logger.error(f"❌ Error parsing SIP message: {e}", exc_info=True)
            logger.error(f"Raw message was: {data.decode('utf-8', errors='replace')}")

    def error_received(self, exc: Exception) -> None:
        """Handle errors."""
        logger.error(f"SIP protocol error: {exc}")

    def _parse_sip_message(self, data: bytes) -> SIPMessage:
        """Parse SIP message."""
        text = data.decode('utf-8', errors='ignore')
        lines = text.split('\r\n')

        # Parse request line
        request_line = lines[0]
        parts = request_line.split(' ')
        method = parts[0] if len(parts) > 0 else ''
        uri = parts[1] if len(parts) > 1 else ''

        # Parse headers
        headers = {}
        body_start = 0
        for i, line in enumerate(lines[1:], 1):
            if not line:
                body_start = i + 1
                break
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip()] = value.strip()

        # Extract body
        body = '\r\n'.join(lines[body_start:]) if body_start > 0 else ''

        logger.debug(f"Parsed SIP message: method={method}, uri={uri}, headers={list(headers.keys())}, body_length={len(body)}")

        return SIPMessage(method=method, uri=uri, headers=headers, body=body, raw=data)

    async def _handle_message(self, msg: SIPMessage, addr: tuple) -> None:
        """Handle SIP message based on method."""
        if msg.method == 'INVITE':
            await self._handle_invite(msg, addr)
        elif msg.method == 'ACK':
            await self._handle_ack(msg, addr)
        elif msg.method == 'BYE':
            await self._handle_bye(msg, addr)

    async def _handle_invite(self, msg: SIPMessage, addr: tuple) -> None:
        """Handle INVITE request with error handling for session creation failures."""
        logger.info(f"Received INVITE from {addr}")
        logger.debug(f"INVITE body: {msg.body}")

        # Parse SDP
        sdp = self._parse_sdp(msg.body)
        if not sdp:
            logger.error("Failed to parse SDP")
            logger.error(f"SDP body was: {msg.body}")
            # Send 400 Bad Request for malformed SDP
            if self.transport:
                response = self._build_error_response(msg, 400, "Bad Request")
                self.transport.sendto(response.encode('utf-8'), addr)
            return

        logger.info(f"Parsed SDP: audio {sdp.audio_ip}:{sdp.audio_port}, codecs {sdp.codecs}")

        # Create call session with error handling
        call_info = {
            'remote_ip': sdp.audio_ip,
            'remote_port': sdp.audio_port,
            'headers': msg.headers,
            'addr': addr
        }

        try:
            # Attempt to create session and allocate RTP port
            rtp_port = await self.on_call_handler(call_info)
            logger.info(f"Allocated RTP port: {rtp_port}")

            # Send 200 OK with SDP answer
            response = self._build_ok_response(msg, rtp_port)
            logger.info(f"Sending 200 OK to {addr}")
            logger.debug(f"Response:\n{response}")

            if self.transport:
                self.transport.sendto(response.encode('utf-8'), addr)
                logger.info("200 OK sent successfully")

        except RuntimeError as e:
            # Port allocation or binding failure
            error_msg = str(e)
            logger.error(f"Failed to create session: {error_msg}")

            # Determine appropriate error code
            if "No available RTP ports" in error_msg or "not bindable" in error_msg:
                # 503 Service Unavailable: Temporary resource shortage
                status_code = 503
                reason = "Service Unavailable"
                logger.warning(f"Sending 503 to {addr}: {error_msg}")
            else:
                # 500 Internal Server Error: Other failures
                status_code = 500
                reason = "Internal Server Error"
                logger.error(f"Sending 500 to {addr}: {error_msg}")

            # Send error response to caller
            if self.transport:
                response = self._build_error_response(msg, status_code, reason)
                self.transport.sendto(response.encode('utf-8'), addr)
                logger.info(f"Sent {status_code} {reason} to {addr}")

        except Exception as e:
            # Unexpected errors
            logger.exception(f"Unexpected error handling INVITE: {e}")
            if self.transport:
                response = self._build_error_response(msg, 500, "Internal Server Error")
                self.transport.sendto(response.encode('utf-8'), addr)
                logger.info(f"Sent 500 Internal Server Error to {addr}")

    async def _handle_ack(self, msg: SIPMessage, addr: tuple) -> None:
        """Handle ACK request and establish call.

        ACK confirms the call is established. At this point:
        1. Create the pipeline for this session
        2. Update session state to 'active'
        3. Trigger on_client_connected event
        """
        call_id = msg.headers.get('Call-ID', 'unknown')
        logger.info(f"✅ ACK received for session {call_id} from {addr}")

        transport = self.sip_server.transport_instance
        if not transport:
            logger.error("No transport instance available")
            return

        # Get the session
        session = await transport._session_manager.get_session(call_id)
        if not session:
            logger.error(f"Session {call_id} not found for ACK")
            return

        # Update session state
        session.state = "active"

        # Create pipeline for this session using the factory
        if transport._pipeline_factory:
            try:
                logger.info(f"Creating pipeline for session {call_id}")
                session.pipeline_task = await transport._pipeline_factory(call_id)

                # Start pipeline using TaskManager and PipelineRunner
                task_name = f"pipeline-{call_id[:8]}"
                runner = PipelineRunner(
                    handle_sigint=False,  # Don't intercept signals for background tasks
                    name=f"runner-{call_id[:8]}"
                )
                transport._task_manager.create_task(
                    runner.run(session.pipeline_task),
                    name=task_name
                )
                logger.info(f"✅ Pipeline created and started for session {call_id}")
            except Exception as e:
                logger.error(f"Failed to create pipeline for session {call_id}: {e}")
                await transport._cleanup_session(call_id, reason="pipeline_creation_error")
                return
        else:
            logger.warning(f"No pipeline factory set, session {call_id} won't have processing pipeline")

        # Emit on_client_connected event with session context
        client_info = {
            'call_id': call_id,  # ✅ Include session ID
            'addr': addr,
            'from': msg.headers.get('From', ''),
            'to': msg.headers.get('To', ''),
        }

        logger.info(f"🎉 Emitting on_client_connected event for session {call_id}")
        await transport._call_event_handler(
            "on_client_connected",
            client_info
        )

    async def _handle_bye(self, msg: SIPMessage, addr: tuple) -> None:
        """Handle BYE request and cleanup session.

        BYE means the caller is ending the call. We need to:
        1. Send 200 OK response
        2. Cleanup the session
        3. Trigger on_client_disconnected event
        """
        call_id = msg.headers.get('Call-ID', 'unknown')
        logger.info(f"📴 BYE received for session {call_id} from {addr}")

        # Send 200 OK immediately
        response = self._build_bye_response(msg)
        if self.transport:
            self.transport.sendto(response.encode('utf-8'), addr)
            logger.info(f"Sent 200 OK for BYE to {addr}")

        # Cleanup session (triggers on_client_disconnected event)
        transport = self.sip_server.transport_instance
        if transport:
            await transport._cleanup_session(call_id, reason="bye")

    def _parse_sdp(self, body: str) -> Optional[SDPSession]:
        """Parse SDP session."""
        try:
            audio_port = 0
            audio_ip = ''
            codecs = []

            for line in body.split('\r\n'):
                if line.startswith('c='):
                    # Connection info: c=IN IP4 <ip>
                    parts = line.split()
                    if len(parts) >= 3:
                        audio_ip = parts[2]
                elif line.startswith('m=audio'):
                    # Media: m=audio <port> RTP/AVP <codecs>
                    parts = line.split()
                    if len(parts) >= 2:
                        audio_port = int(parts[1])
                    if len(parts) >= 4:
                        codecs = [int(c) for c in parts[3:]]

            if audio_port and audio_ip:
                return SDPSession(audio_port=audio_port, audio_ip=audio_ip, codecs=codecs)

            return None
        except Exception as e:
            logger.error(f"Error parsing SDP: {e}")
            return None

    def _build_ok_response(self, msg: SIPMessage, rtp_port: int) -> str:
        """Build 200 OK response with SDP."""
        call_id = msg.headers.get('Call-ID', 'unknown')
        from_header = msg.headers.get('From', '')
        to_header = msg.headers.get('To', '')
        via = msg.headers.get('Via', '')
        cseq = msg.headers.get('CSeq', '')

        # Get local IP (simplified - use first non-loopback interface)
        local_ip = self._get_local_ip()

        sdp = f"""v=0
o=- 0 0 IN IP4 {local_ip}
s=Pipecat SIP Bot
c=IN IP4 {local_ip}
t=0 0
m=audio {rtp_port} RTP/AVP 0
a=rtpmap:0 PCMU/8000
"""

        # Get server port from transport socket
        server_port = 6060  # Default port
        if hasattr(self, 'transport') and self.transport:
            sock = self.transport.get_extra_info('socket')
            if sock:
                server_port = sock.getsockname()[1]

        response = f"""SIP/2.0 200 OK
Via: {via}
From: {from_header}
To: {to_header}
Call-ID: {call_id}
CSeq: {cseq}
Contact: <sip:{local_ip}:{server_port}>
Content-Type: application/sdp
Content-Length: {len(sdp)}

{sdp}"""

        return response

    def _build_bye_response(self, msg: SIPMessage) -> str:
        """Build 200 OK response for BYE."""
        call_id = msg.headers.get('Call-ID', 'unknown')
        from_header = msg.headers.get('From', '')
        to_header = msg.headers.get('To', '')
        via = msg.headers.get('Via', '')
        cseq = msg.headers.get('CSeq', '')

        response = f"""SIP/2.0 200 OK
Via: {via}
From: {from_header}
To: {to_header}
Call-ID: {call_id}
CSeq: {cseq}
Content-Length: 0

"""
        return response

    def _build_error_response(self, msg: SIPMessage, status_code: int, reason: str) -> str:
        """Build SIP error response (e.g., 503 Service Unavailable, 500 Internal Server Error).

        Args:
            msg: Original SIP message
            status_code: HTTP-style status code (e.g., 503, 500)
            reason: Reason phrase (e.g., "Service Unavailable", "Internal Server Error")

        Returns:
            Formatted SIP error response string
        """
        call_id = msg.headers.get('Call-ID', 'unknown')
        from_header = msg.headers.get('From', '')
        to_header = msg.headers.get('To', '')
        via = msg.headers.get('Via', '')
        cseq = msg.headers.get('CSeq', '')

        response = f"""SIP/2.0 {status_code} {reason}
Via: {via}
From: {from_header}
To: {to_header}
Call-ID: {call_id}
CSeq: {cseq}
Content-Length: 0

"""
        return response

    def _get_local_ip(self) -> str:
        """Get local IP address."""
        try:
            # Create a socket to determine local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return '127.0.0.1'


class SIPTransport(BaseTransport):
    """SIP transport for Pipecat with multi-session support."""

    def __init__(self, params: SIPParams, **kwargs):
        super().__init__(**kwargs)

        self._params = params
        self._port_pool = RTPPortPool(params.rtp_port_start, params.rtp_port_end)
        self._sip_server: Optional[SIPServer] = None

        # ❌ Removed single-session fields:
        # self._rtp_session: Optional[RTPSession] = None
        # self._current_rtp_port: Optional[int] = None
        # self._receive_task: Optional[asyncio.Task] = None

        # ✅ Added multi-session manager:
        self._session_manager = SIPSessionManager(
            self._port_pool,
            self._on_session_timeout
        )

        # Pipeline factory function (injected from bot.py)
        self._pipeline_factory: Optional[Callable] = None

        # Task manager for pipeline coroutines
        self._task_manager = TaskManager()

        # Track TX audio count per session for periodic logging
        self._tx_audio_counts: dict[str, int] = {}

        self._input_processor: Optional[FrameProcessor] = None
        self._output_processor: Optional[FrameProcessor] = None

        # Register supported event handlers (like Daily/WebRTC transport)
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    def set_pipeline_factory(self, factory: Callable):
        """Set pipeline factory function for creating per-session pipelines.

        Args:
            factory: Async function that takes (session_id: str) and returns PipelineTask

        Example:
            async def create_pipeline(session_id: str) -> PipelineTask:
                # Create STT, LLM, TTS for this session
                return PipelineTask(...)

            transport.set_pipeline_factory(create_pipeline)
        """
        self._pipeline_factory = factory
        logger.info("Pipeline factory registered")

    async def _on_session_timeout(self, session: SIPSession) -> None:
        """Callback when session times out (60s no RTP activity).

        Args:
            session: Timed out session

        Triggers on_client_disconnected event and cleans up resources.
        """
        logger.warning(f"Session {session.call_id} timed out, cleaning up")
        await self._cleanup_session(session.call_id, reason="rtp_timeout")

    async def _cleanup_session(self, call_id: str, reason: str = "unknown") -> None:
        """Cleanup a session and trigger disconnected event.

        Args:
            call_id: Session Call-ID to cleanup
            reason: Reason for cleanup (bye/rtp_timeout/error)
        """
        # Remove session (handles all cleanup)
        await self._session_manager.remove_session(call_id)

        # Trigger on_client_disconnected event with session context
        await self._call_event_handler(
            "on_client_disconnected",
            {"call_id": call_id, "reason": reason}
        )

    def input(self, session_id: Optional[str] = None) -> FrameProcessor:
        """Get input frame processor for a specific session.

        Args:
            session_id: Call-ID of the session (required for multi-session)

        Returns:
            Session-specific input processor

        Note: For multi-session architecture, create processor per session in pipeline factory.
        """
        if session_id:
            return SessionSIPInputProcessor(self, session_id)
        else:
            # Fallback for backward compatibility (single session mode)
            if not self._input_processor:
                self._input_processor = SIPInputProcessor(self)
            return self._input_processor

    def output(self, session_id: Optional[str] = None) -> FrameProcessor:
        """Get output frame processor for a specific session.

        Args:
            session_id: Call-ID of the session (required for multi-session)

        Returns:
            Session-specific output processor

        Note: For multi-session architecture, create processor per session in pipeline factory.
        """
        if session_id:
            return SessionSIPOutputProcessor(self, session_id)
        else:
            # Fallback for backward compatibility (single session mode)
            if not self._output_processor:
                self._output_processor = SIPOutputProcessor(self)
            return self._output_processor

    async def start(self) -> None:
        """Start SIP transport and session management."""
        # Start SIP server
        self._sip_server = SIPServer(
            self._params.host,
            self._params.port,
            self._handle_incoming_call,
            transport_instance=self  # Pass self for event handling
        )
        await self._sip_server.start()

        # Setup task manager with event loop
        self._task_manager.setup(TaskManagerParams(loop=asyncio.get_event_loop()))
        logger.debug("TaskManager initialized for pipeline management")

        # Start session timeout monitor
        await self._session_manager.start_timeout_monitor()

    async def stop(self) -> None:
        """Stop SIP transport and cleanup all sessions."""
        # Cancel all running pipeline tasks
        for task in self._task_manager.current_tasks():
            try:
                await self._task_manager.cancel_task(task, timeout=5.0)
                logger.debug(f"Cancelled pipeline task: {task.get_name()}")
            except Exception as e:
                logger.warning(f"Error cancelling pipeline task: {e}")

        # Stop session timeout monitor
        await self._session_manager.stop_timeout_monitor()

        # Stop all active sessions
        await self._session_manager.stop_all_sessions()

        # Stop SIP server
        if self._sip_server:
            await self._sip_server.stop()

    async def _handle_incoming_call(self, call_info: dict) -> int:
        """Handle incoming SIP call and create new session.

        Args:
            call_info: Dict containing remote_ip, remote_port, headers, addr

        Returns:
            Allocated RTP port number

        This is called during INVITE processing, before ACK.
        The session is created but pipeline is not started until ACK.
        """
        call_id = call_info['headers'].get('Call-ID', f'unknown-{time.time()}')
        logger.info(f"📞 Incoming call {call_id} from {call_info['remote_ip']}:{call_info['remote_port']}")

        # Create new session (allocates RTP port, starts RTP session)
        session = await self._session_manager.create_session(call_id, call_info)

        # Start receiving audio for this session
        session.receive_task = asyncio.create_task(self._receive_audio_loop(session))

        logger.info(f"💡 Session {call_id} ready on RTP port {session.local_rtp_port}, waiting for ACK")

        return session.local_rtp_port

    async def _receive_audio_loop(self, session: SIPSession) -> None:
        """Receive audio from RTP for a specific session and push to its pipeline.

        Args:
            session: The SIP session to receive audio for

        Updates RTP activity timestamp on each packet to prevent timeout.
        """
        import numpy as np

        frame_count = 0
        call_id = session.call_id
        try:
            while session.rtp_session and session.rtp_session.running:
                pcm_audio = await session.rtp_session.receive_audio()
                frame_count += 1

                # Update RTP activity timestamp (prevents timeout)
                await self._session_manager.update_rtp_activity(call_id)

                # Analyze audio energy to detect silence vs. actual content
                audio_array = np.frombuffer(pcm_audio, dtype=np.int16)
                if len(audio_array) > 0:
                    # Use float64 to avoid overflow and handle edge cases
                    mean_square = np.mean(audio_array.astype(np.float64)**2)
                    rms = np.sqrt(mean_square) if not np.isnan(mean_square) and mean_square >= 0 else 0.0
                else:
                    rms = 0.0

                # Log periodically with audio energy and session ID
                if frame_count % 50 == 1:  # Every ~1 second
                    logger.info(f"📥 [{call_id[:8]}] RX audio #{frame_count}, {len(pcm_audio)} bytes, RMS={rms:.1f}")

                # Create audio frame (8kHz, 1 channel, 20ms = 160 samples)
                frame = AudioRawFrame(
                    audio=pcm_audio,
                    sample_rate=8000,
                    num_channels=1
                )

                # Push audio frame directly to this session's pipeline
                if session.pipeline_task:
                    await session.pipeline_task.queue_frames([frame])  # type: ignore
                else:
                    # Pipeline not ready yet (waiting for ACK), buffer or drop
                    if frame_count <= 5:
                        logger.debug(f"[{call_id[:8]}] Pipeline not ready, dropping early audio")

        except asyncio.CancelledError:
            logger.info(f"[{call_id[:8]}] Receive audio loop cancelled after {frame_count} frames")
        except Exception as e:
            logger.error(f"[{call_id[:8]}] Error in receive audio loop: {e}")
            await self._cleanup_session(call_id, reason="receive_error")

    async def send_audio(self, frame: AudioRawFrame, session_id: str) -> None:
        """Send audio frame to RTP for a specific session.

        Args:
            frame: Audio frame to send
            session_id: Call-ID of the session to send audio to
        """
        session = await self._session_manager.get_session(session_id)
        if session and session.rtp_session:
            # Track TX audio count for periodic logging
            if session_id not in self._tx_audio_counts:
                self._tx_audio_counts[session_id] = 0
            self._tx_audio_counts[session_id] += 1

            # Log periodically (every 50 frames = ~1 second)
            if self._tx_audio_counts[session_id] % 50 == 1:
                # Calculate audio energy for diagnostic comparison with received audio
                import numpy as np
                audio_array = np.frombuffer(frame.audio, dtype=np.int16)
                # Prevent NaN by checking for empty or zero arrays
                if len(audio_array) == 0 or np.all(audio_array == 0):
                    rms = 0.0
                else:
                    rms = np.sqrt(np.mean(audio_array**2))
                logger.info(f"📤 [{session_id[:8]}] TX audio #{self._tx_audio_counts[session_id]}: {len(frame.audio)} bytes, {frame.sample_rate}Hz, RMS={rms:.1f}")

            await session.rtp_session.send_audio(frame.audio, frame.sample_rate)
        else:
            logger.warning(f"❌ Session {session_id} not found or no RTP session, dropping audio")


class SIPInputProcessor(FrameProcessor):
    """Frame processor for SIP transport input with VAD support."""

    def __init__(self, transport: SIPTransport):
        super().__init__()
        self._transport = transport
        self._frame_count = 0
        self._vad_analyzer = transport._params.vad_analyzer
        # Initialize VAD state to track changes (avoid duplicate events)
        from pipecat.audio.vad.vad_analyzer import VADState
        self._vad_state = VADState.QUIET
        # Track if bot is currently speaking (for interruption detection)
        self._bot_is_speaking = False

    async def process_frame(self, frame, direction: FrameDirection):
        """Process incoming frames with VAD analysis."""
        from pipecat.audio.vad.vad_analyzer import VADState
        from pipecat.frames.frames import (
            AudioRawFrame,
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            InterruptionTaskFrame,
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
        )

        await super().process_frame(frame, direction)

        # Track bot speaking state by listening to frames flowing downstream
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_is_speaking = True
            logger.debug("SIPInput: Bot started speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_is_speaking = False
            logger.debug("SIPInput: Bot stopped speaking")

        # Log first few frames and then occasionally to debug pipeline flow
        self._frame_count += 1
        if self._frame_count <= 10 or self._frame_count % 100 == 0:
            frame_type = type(frame).__name__
            logger.info(f"📨 SIPInput processing frame #{self._frame_count}: {frame_type}")

        # Run VAD on audio frames - only emit events on state changes
        # Pattern from BaseInputTransport._handle_vad()
        if self._vad_analyzer and isinstance(frame, AudioRawFrame):
            new_vad_state = await self._vad_analyzer.analyze_audio(frame.audio)

            # Only emit events when state changes AND not in transitional states
            if (
                new_vad_state != self._vad_state
                and new_vad_state != VADState.STARTING  # Ignore transitional state
                and new_vad_state != VADState.STOPPING  # Ignore transitional state
            ):
                if new_vad_state == VADState.SPEAKING:
                    logger.info("VAD: Speech STARTING (user)")
                    await self.push_frame(UserStartedSpeakingFrame(), direction)

                    # If bot is speaking, trigger interruption
                    if self._bot_is_speaking:
                        logger.info("🚨 User interrupting bot - sending InterruptionTaskFrame")
                        await self.push_frame(InterruptionTaskFrame(), FrameDirection.UPSTREAM)

                elif new_vad_state == VADState.QUIET:
                    logger.info("VAD: Speech STOPPING (user)")
                    await self.push_frame(UserStoppedSpeakingFrame(), direction)

                # Update state after emitting event
                self._vad_state = new_vad_state

        await self.push_frame(frame, direction)


class SIPOutputProcessor(FrameProcessor):
    """Frame processor for SIP transport output."""

    def __init__(self, transport: SIPTransport):
        super().__init__()
        self._transport = transport
        self._frame_count = 0
        self._speaking = False
        # Timeout-based bot speaking detection (similar to BaseOutputTransport)
        self._last_audio_time = 0
        self._stop_timeout = 0.5  # seconds of silence before considering bot stopped
        self._stop_check_task = None
        # Track if we're in interrupted state (drop audio until user speaks again)
        self._interrupted = False

    async def process_frame(self, frame, direction: FrameDirection):
        """Process outgoing frames."""
        import time

        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            InterruptionFrame,
            TranscriptionFrame,
            UserStartedSpeakingFrame,
        )

        # DEBUG: Log ALL frames received by SIPOutputProcessor
        logger.debug(f"📦 SIPOutput received: {frame.__class__.__name__}, direction={direction}")

        await super().process_frame(frame, direction)

        # Handle interruption - stop audio output immediately and drop future audio
        if isinstance(frame, InterruptionFrame):
            logger.info("🛑 InterruptionFrame received - stopping bot speech and dropping audio")
            self._interrupted = True  # Set interrupted state
            if self._speaking:
                self._speaking = False
                # Cancel stop check task
                if self._stop_check_task:
                    self._stop_check_task.cancel()
                    self._stop_check_task = None
                # Notify upstream that bot stopped speaking
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            # Pass interruption frame downstream
            await self.push_frame(frame, direction)
            return

        # Clear interrupted state when user starts speaking (new input)
        if isinstance(frame, UserStartedSpeakingFrame) or isinstance(frame, TranscriptionFrame):
            if self._interrupted:
                logger.info("🔄 User speaking - clearing interrupted state")
                self._interrupted = False

        if isinstance(frame, AudioRawFrame):
            # Drop audio frames if we're in interrupted state
            if self._interrupted:
                logger.debug(f"⏭️  Dropping audio frame (interrupted state)")
                return  # Don't send audio, don't pass frame downstream

            self._frame_count += 1
            if self._frame_count % 50 == 1:  # Log every ~1 second
                logger.info(f"SIP output: received frame #{self._frame_count}, {len(frame.audio)} bytes, {frame.sample_rate}Hz")
            await self._transport.send_audio(frame, "legacy")  # type: ignore  # Legacy single-session fallback

            # Send BotStartedSpeakingFrame UPSTREAM on first audio frame
            if not self._speaking:
                self._speaking = True
                logger.info("🗣️ Bot started speaking")
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)

                # Start background task to monitor for stop
                if not self._stop_check_task:
                    self._stop_check_task = self.create_task(self._check_bot_stopped())

            # Update last audio time
            self._last_audio_time = time.time()

            # AudioRawFrame已通过RTP发送，不要继续传递到下游（防止pipeline回路）
            return

        # Pass all non-audio frames downstream
        await self.push_frame(frame, direction)

    async def _check_bot_stopped(self):
        """Background task to detect when bot stops speaking based on audio timeout."""
        import time

        from pipecat.frames.frames import BotStoppedSpeakingFrame

        try:
            while self._speaking:
                await asyncio.sleep(0.1)  # Check every 100ms

                # If no audio for _stop_timeout seconds, bot stopped speaking
                if self._speaking and time.time() - self._last_audio_time > self._stop_timeout:
                    self._speaking = False
                    logger.info("🔇 Bot stopped speaking (timeout)")
                    await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._stop_check_task = None


class SessionSIPInputProcessor(FrameProcessor):
    """Session-specific input processor for multi-session SIP transport.

    This processor is created per session and bound to a specific Call-ID.
    It handles VAD analysis and interruption detection for the session.
    """

    def __init__(self, transport: SIPTransport, session_id: str):
        super().__init__()
        self._transport = transport
        self._session_id = session_id
        self._frame_count = 0
        # Initialize VAD analyzer from transport params
        self._vad_analyzer = transport._params.vad_analyzer
        # Initialize VAD state to track changes (avoid duplicate events)
        from pipecat.audio.vad.vad_analyzer import VADState
        self._vad_state = VADState.QUIET
        # Track if bot is currently speaking (for interruption detection)
        self._bot_is_speaking = False

    async def process_frame(self, frame, direction: FrameDirection):
        """Process incoming frames for this session with VAD analysis."""
        from pipecat.audio.vad.vad_analyzer import VADState
        from pipecat.frames.frames import (
            AudioRawFrame,
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            InterruptionTaskFrame,
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
        )

        await super().process_frame(frame, direction)

        # Track bot speaking state by listening to frames flowing downstream
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_is_speaking = True
            logger.debug(f"[{self._session_id[:8]}] Bot started speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_is_speaking = False
            logger.debug(f"[{self._session_id[:8]}] Bot stopped speaking")

        # Log first few frames and then occasionally to debug pipeline flow
        self._frame_count += 1
        if self._frame_count <= 10 or self._frame_count % 100 == 0:
            frame_type = type(frame).__name__
            logger.debug(f"[{self._session_id[:8]}] SessionInput frame #{self._frame_count}: {frame_type}")

        # Run VAD on audio frames - only emit events on state changes
        # Pattern from BaseInputTransport._handle_vad()
        if self._vad_analyzer and isinstance(frame, AudioRawFrame):
            new_vad_state = await self._vad_analyzer.analyze_audio(frame.audio)

            # Only emit events when state changes AND not in transitional states
            if (
                new_vad_state != self._vad_state
                and new_vad_state != VADState.STARTING  # Ignore transitional state
                and new_vad_state != VADState.STOPPING  # Ignore transitional state
            ):
                if new_vad_state == VADState.SPEAKING:
                    logger.info(f"[{self._session_id[:8]}] VAD: Speech STARTING (user)")
                    await self.push_frame(UserStartedSpeakingFrame(), direction)

                    # If bot is speaking, trigger interruption
                    if self._bot_is_speaking:
                        logger.info(f"🚨 [{self._session_id[:8]}] User interrupting bot - sending InterruptionTaskFrame")
                        await self.push_frame(InterruptionTaskFrame(), FrameDirection.UPSTREAM)

                elif new_vad_state == VADState.QUIET:
                    logger.info(f"[{self._session_id[:8]}] VAD: Speech STOPPING (user)")
                    await self.push_frame(UserStoppedSpeakingFrame(), direction)

                # Update state after emitting event
                self._vad_state = new_vad_state

        # Pass frame downstream
        await self.push_frame(frame, direction)


class SessionSIPOutputProcessor(FrameProcessor):
    """Session-specific output processor for multi-session SIP transport.

    This processor is created per session and bound to a specific Call-ID.
    It handles routing audio frames to the correct RTP session.
    """

    def __init__(self, transport: SIPTransport, session_id: str):
        super().__init__()
        self._transport = transport
        self._session_id = session_id
        self._frame_count = 0
        self._speaking = False
        self._last_audio_time = 0
        self._stop_timeout = 0.5
        self._stop_check_task = None
        self._interrupted = False

    async def process_frame(self, frame, direction: FrameDirection):
        """Process outgoing frames for this session."""
        import time

        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            InterruptionFrame,
            LLMFullResponseStartFrame,
            TranscriptionFrame,
            UserStartedSpeakingFrame,
        )

        await super().process_frame(frame, direction)

        # Handle interruption
        if isinstance(frame, InterruptionFrame):
            logger.info(f"[{self._session_id[:8]}] 🛑 InterruptionFrame - stopping audio")
            self._interrupted = True
            if self._speaking:
                self._speaking = False
                if self._stop_check_task:
                    self._stop_check_task.cancel()
                    self._stop_check_task = None
                await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            await self.push_frame(frame, direction)
            return

        # Clear interrupted state when user starts speaking
        if isinstance(frame, UserStartedSpeakingFrame) or isinstance(frame, TranscriptionFrame):
            if self._interrupted:
                logger.info(f"[{self._session_id[:8]}] 🔄 User speaking - clearing interrupted state")
                self._interrupted = False

        # Clear interrupted state when new LLM response begins
        if isinstance(frame, LLMFullResponseStartFrame):
            if self._interrupted:
                logger.info(f"[{self._session_id[:8]}] 🔄 New LLM response - clearing interrupted state")
                self._interrupted = False

        if isinstance(frame, AudioRawFrame):
            # Drop audio if interrupted
            if self._interrupted:
                logger.debug(f"[{self._session_id[:8]}] ⏭️ Dropping audio (interrupted)")
                return

            self._frame_count += 1
            if self._frame_count % 50 == 1:
                logger.info(f"[{self._session_id[:8]}] SessionOutput frame #{self._frame_count}, {len(frame.audio)} bytes")

            # Send audio to this session's RTP
            await self._transport.send_audio(frame, self._session_id)

            # Track bot speaking state
            if not self._speaking:
                self._speaking = True
                logger.info(f"[{self._session_id[:8]}] 🗣️ Bot started speaking")
                await self.push_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
                if not self._stop_check_task:
                    self._stop_check_task = self.create_task(self._check_bot_stopped())

            self._last_audio_time = time.time()
            return  # Don't pass audio frames downstream

        # Pass non-audio frames downstream
        await self.push_frame(frame, direction)

    async def _check_bot_stopped(self):
        """Background task to detect when bot stops speaking."""
        import time

        from pipecat.frames.frames import BotStoppedSpeakingFrame

        try:
            while self._speaking:
                await asyncio.sleep(0.1)
                if self._speaking and time.time() - self._last_audio_time > self._stop_timeout:
                    self._speaking = False
                    logger.info(f"[{self._session_id[:8]}] 🔇 Bot stopped speaking")
                    await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._stop_check_task = None
