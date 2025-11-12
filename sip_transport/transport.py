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
from typing import Any, Callable, Optional

from loguru import logger
from pipecat.frames.frames import AudioRawFrame, CancelFrame, EndFrame, Frame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport, TransportParams


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


class RTPPortPool:
    """Manages allocation of RTP ports from a configured range."""

    def __init__(self, start: int, end: int):
        self.start = start
        self.end = end
        self.allocated: set[int] = set()
        self.lock = asyncio.Lock()

    async def allocate(self) -> int:
        """Allocate an available port from the pool."""
        async with self.lock:
            for _ in range(100):  # Try 100 random ports
                port = random.randint(self.start, self.end)
                if port not in self.allocated:
                    self.allocated.add(port)
                    return port
            raise RuntimeError("No available RTP ports in pool")

    async def release(self, port: int) -> None:
        """Release a port back to the pool."""
        async with self.lock:
            self.allocated.discard(port)


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

    async def start(self) -> None:
        """Start RTP session."""
        loop = asyncio.get_event_loop()

        # Create UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(('0.0.0.0', self.local_port))

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

        while self.running:
            try:
                current_time = loop.time()

                # Wait until next send time
                wait_time = next_send_time - current_time
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                # Check for severe drift (more than 100ms behind)
                current_time = loop.time()
                drift = current_time - next_send_time
                if drift > 0.1:
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
        """Handle INVITE request."""
        logger.info(f"Received INVITE from {addr}")
        logger.debug(f"INVITE body: {msg.body}")

        # Parse SDP
        sdp = self._parse_sdp(msg.body)
        if not sdp:
            logger.error("Failed to parse SDP")
            logger.error(f"SDP body was: {msg.body}")
            return

        logger.info(f"Parsed SDP: audio {sdp.audio_ip}:{sdp.audio_port}, codecs {sdp.codecs}")

        # Create call session
        call_info = {
            'remote_ip': sdp.audio_ip,
            'remote_port': sdp.audio_port,
            'headers': msg.headers,
            'addr': addr
        }

        # Get RTP port from call handler
        rtp_port = await self.on_call_handler(call_info)
        logger.info(f"Allocated RTP port: {rtp_port}")

        # Send 200 OK with SDP answer
        response = self._build_ok_response(msg, rtp_port)
        logger.info(f"Sending 200 OK to {addr}")
        logger.debug(f"Response:\n{response}")

        if self.transport:
            self.transport.sendto(response.encode('utf-8'), addr)
            logger.info("200 OK sent successfully")

    async def _handle_ack(self, msg: SIPMessage, addr: tuple) -> None:
        """Handle ACK request."""
        logger.info(f"✅ Call established (ACK received) from {addr}")

        # Call is now established - emit on_client_connected event
        if self.sip_server.transport_instance:
            # Create client info similar to Daily transport participant format
            client_info = {
                'addr': addr,
                'call_id': msg.headers.get('Call-ID', 'unknown'),
                'from': msg.headers.get('From', ''),
            }

            # Emit the standard event
            logger.info(f"🎉 Emitting on_client_connected event")
            await self.sip_server.transport_instance._call_event_handler(
                "on_client_connected",
                client_info
            )

    async def _handle_bye(self, msg: SIPMessage, addr: tuple) -> None:
        """Handle BYE request."""
        logger.info(f"Received BYE from {addr}")

        # Emit on_client_disconnected event
        if self.sip_server.transport_instance:
            client_info = {
                'addr': addr,
                'call_id': msg.headers.get('Call-ID', 'unknown'),
            }
            logger.info(f"🔌 Emitting on_client_disconnected event")
            await self.sip_server.transport_instance._call_event_handler(
                "on_client_disconnected",
                client_info
            )

        # Send 200 OK
        response = self._build_bye_response(msg)
        if self.transport:
            self.transport.sendto(response.encode('utf-8'), addr)

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
    """SIP transport for Pipecat."""

    def __init__(self, params: SIPParams, **kwargs):
        super().__init__(**kwargs)

        self._params = params
        self._port_pool = RTPPortPool(params.rtp_port_start, params.rtp_port_end)
        self._sip_server: Optional[SIPServer] = None
        self._rtp_session: Optional[RTPSession] = None
        self._current_rtp_port: Optional[int] = None

        self._input_processor: Optional[FrameProcessor] = None
        self._output_processor: Optional[FrameProcessor] = None

        self._receive_task: Optional[asyncio.Task] = None

        # Register supported event handlers (like Daily/WebRTC transport)
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")

    def input(self) -> FrameProcessor:
        """Get input frame processor."""
        if not self._input_processor:
            self._input_processor = SIPInputProcessor(self)
        return self._input_processor

    def output(self) -> FrameProcessor:
        """Get output frame processor."""
        if not self._output_processor:
            self._output_processor = SIPOutputProcessor(self)
        return self._output_processor

    async def start(self) -> None:
        """Start SIP transport."""
        # Start SIP server
        self._sip_server = SIPServer(
            self._params.host,
            self._params.port,
            self._handle_incoming_call,
            transport_instance=self  # Pass self for event handling
        )
        await self._sip_server.start()

    async def stop(self) -> None:
        """Stop SIP transport."""
        # Stop RTP session
        if self._rtp_session:
            await self._rtp_session.stop()
            if self._current_rtp_port:
                await self._port_pool.release(self._current_rtp_port)

        # Cancel receive task
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        # Stop SIP server
        if self._sip_server:
            await self._sip_server.stop()

    async def _handle_incoming_call(self, call_info: dict) -> int:
        """Handle incoming SIP call."""
        logger.info(f"Incoming call from {call_info['remote_ip']}:{call_info['remote_port']}")

        # Allocate RTP port
        rtp_port = await self._port_pool.allocate()
        self._current_rtp_port = rtp_port

        # Create RTP session
        self._rtp_session = RTPSession(
            local_port=rtp_port,
            remote_ip=call_info['remote_ip'],
            remote_port=call_info['remote_port']
        )
        await self._rtp_session.start()
        logger.info(f"RTP session started: local={rtp_port}, remote={call_info['remote_ip']}:{call_info['remote_port']}")

        # Start receiving audio
        self._receive_task = asyncio.create_task(self._receive_audio_loop())

        # Note: on_client_connected will be triggered by SIPServer after ACK is received
        logger.info("💡 RTP session ready, waiting for ACK to establish call")

        return rtp_port

    async def _receive_audio_loop(self) -> None:
        """Receive audio from RTP and push to pipeline.

        Voice activity detection is handled by Deepgram's endpointing feature.
        """
        import numpy as np

        frame_count = 0
        try:
            while self._rtp_session and self._rtp_session.running:
                pcm_audio = await self._rtp_session.receive_audio()
                frame_count += 1

                # Analyze audio energy to detect silence vs. actual content
                audio_array = np.frombuffer(pcm_audio, dtype=np.int16)
                if len(audio_array) > 0:
                    # Use float64 to avoid overflow and handle edge cases
                    mean_square = np.mean(audio_array.astype(np.float64)**2)
                    rms = np.sqrt(mean_square) if not np.isnan(mean_square) and mean_square >= 0 else 0.0
                else:
                    rms = 0.0

                # Log periodically with audio energy
                if frame_count % 50 == 1:  # Every ~1 second
                    logger.info(f"📥 RX audio #{frame_count}, {len(pcm_audio)} bytes, RMS={rms:.1f}")

                # Create audio frame (8kHz, 1 channel, 20ms = 160 samples)
                frame = AudioRawFrame(
                    audio=pcm_audio,
                    sample_rate=8000,
                    num_channels=1
                )

                # Push audio frame directly to input processor
                # STT service (Deepgram) handles endpointing internally
                if self._input_processor:
                    await self._input_processor.queue_frame(frame)  # type: ignore
        except asyncio.CancelledError:
            logger.info(f"Receive audio loop cancelled after {frame_count} frames")
            pass
        except Exception as e:
            logger.error(f"Error in receive audio loop: {e}")

    async def send_audio(self, frame: AudioRawFrame) -> None:
        """Send audio frame to RTP."""
        if self._rtp_session:
            # Calculate audio energy for diagnostic comparison with received audio
            import numpy as np
            audio_array = np.frombuffer(frame.audio, dtype=np.int16)
            rms = np.sqrt(np.mean(audio_array**2))
            logger.info(f"📤 TX audio: {len(frame.audio)} bytes, {frame.sample_rate}Hz, RMS={rms:.1f}")
            await self._rtp_session.send_audio(frame.audio, frame.sample_rate)
        else:
            logger.warning("❌ No RTP session available to send audio")


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
        from pipecat.frames.frames import (
            AudioRawFrame,
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            InterruptionTaskFrame
        )
        from pipecat.audio.vad.vad_analyzer import VADState

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
            UserStartedSpeakingFrame,
            TranscriptionFrame
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
            await self._transport.send_audio(frame)

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
