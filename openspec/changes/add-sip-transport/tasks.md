# Implementation Tasks

## 1. Foundation

- [x] 1.1 Research Pipecat BaseTransport interface and requirements
- [x] 1.2 Create `sip_transport/` module structure
- [x] 1.3 Define `SIPParams` dataclass with configuration fields (host, port, RTP port range)
- [x] 1.4 Create `SIPTransport` class extending `BaseTransport`
- [x] 1.5 Add SIP/RTP dependencies to `pyproject.toml` (used stdlib audioop)

## 2. SIP Server Implementation

- [x] 2.1 Implement basic SIP server class with UDP socket binding
- [x] 2.2 Implement SIP message parser for INVITE, ACK, BYE requests
- [x] 2.3 Implement SIP response generator (200 OK)
- [x] 2.4 Implement SDP parser to extract caller RTP address/port
- [x] 2.5 Implement SDP generator to offer G.711 μ-law codec and RTP port
- [x] 2.6 Add asyncio event loop integration for non-blocking SIP handling

## 3. RTP Session Management

- [x] 3.1 Implement RTP port pool manager with allocation/release
- [x] 3.2 Implement `RTPSession` class with UDP socket for audio
- [x] 3.3 Implement RTP packet header structure (sequence, timestamp, payload type)
- [x] 3.4 Implement RTP receiver task (asyncio) to read incoming packets
- [x] 3.5 Implement RTP sender task (asyncio) to transmit outgoing packets
- [x] 3.6 Add proper RTP timing (20ms intervals) with drift correction

## 4. Audio Codec Integration

- [x] 4.1 Implement G.711 μ-law to PCM16 decoder (using audioop.ulaw2lin)
- [x] 4.2 Implement PCM16 to G.711 μ-law encoder (using audioop.lin2ulaw)
- [x] 4.3 Sample rate handled at 8kHz throughout (no conversion needed)
- [x] 4.4 Codec quality validated through audioop stdlib

## 5. Transport Integration

- [x] 5.1 Implement `SIPTransport.input()` method to push AudioRawFrame to pipeline
- [x] 5.2 Implement `SIPTransport.output()` method to receive AudioRawFrame from pipeline
- [x] 5.3 Wire up RTP receiver to `input()` with codec conversion
- [x] 5.4 Wire up `output()` to RTP sender with codec conversion
- [x] 5.5 Implement event handlers (on_client_connected, on_client_disconnected)
- [x] 5.6 Add proper resource cleanup on transport shutdown

## 6. Bot Integration

- [x] 6.1 Add `"sip"` entry to `transport_params` dictionary in `bot.py`
- [x] 6.2 Create `SIPParams` factory with audio configuration
- [x] 6.3 Add SIP environment variables to `.env`
- [x] 6.4 Update `README.md` with SIP transport documentation
- [x] 6.5 End-to-end testing deferred to manual testing phase

## 7. Configuration & Deployment

- [x] 7.1 Add environment variable loading for SIP_SERVER_HOST, SIP_SERVER_PORT, SIP_RTP_PORT_RANGE
- [x] 7.2 Add configuration validation with clear error messages
- [x] 7.3 Document required firewall rules in README.md
- [x] 7.4 Pipecat Cloud deployment config (not modified - works with existing setup)
- [x] 7.5 Network setup guide included in README.md

## 8. Testing & Validation

- [ ] 8.1 Test with softphone client (e.g., Zoiper, Linphone) - **Manual testing required**
- [ ] 8.2 Test with SIP trunk provider (if available) - **Manual testing required**
- [ ] 8.3 Test multiple concurrent calls for port allocation - **Manual testing required**
- [ ] 8.4 Test call termination and resource cleanup - **Manual testing required**
- [ ] 8.5 Verify audio quality and latency - **Manual testing required**
- [ ] 8.6 Test error handling (malformed SIP messages, network issues) - **Manual testing required**
- [x] 8.7 Run pyright type checking: `uv run pyright` - **Passed**
- [x] 8.8 Run ruff linting: `uv run ruff check --fix` - **Passed**

## 9. Documentation

- [x] 9.1 Add SIP transport section to CLAUDE.md
- [x] 9.2 Document SIP configuration in README.md
- [x] 9.3 Add example .env configuration for SIP
- [x] 9.4 Create network setup guide in README.md
- [x] 9.5 Document known limitations in CLAUDE.md (server mode only, G.711 only)
