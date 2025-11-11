# Add SIP Transport

## Why

The current bot only supports Daily and WebRTC transports for voice connectivity. To enable telephony integration and allow users to interact with the bot via traditional phone systems, we need SIP (Session Initiation Protocol) transport support with RTP (Real-time Transport Protocol) for audio streaming.

## What Changes

- Add SIP transport as a new transport option alongside existing Daily/WebRTC transports
- Implement SIP server mode to accept incoming INVITE requests from SIP clients/trunks
- Support G.711 μ-law codec (PCMU) at 8kHz for telephony compatibility
- Enable RTP audio streaming with bidirectional audio (receive from caller, send to caller)
- Configure SIP server settings via environment variables (host, port, RTP port range)
- Integrate with existing Pipecat pipeline (STT → LLM → TTS)

## Impact

- **New capabilities:**
  - `sip-transport`: SIP/RTP transport implementation for telephony connectivity
  - Updates to `voice-pipeline`: Extend transport selection to include SIP option

- **Affected code:**
  - New SIP transport module with SIP server and RTP session handling
  - `bot.py`: Add SIP transport parameters to `transport_params` dictionary
  - `.env`: Add SIP configuration variables
  - `pyproject.toml`: Add SIP/RTP dependencies if needed

- **Deployment:**
  - Network configuration required for SIP (UDP port 6060) and RTP (UDP port range 10000-15000)
  - Firewall rules must allow inbound UDP traffic on configured ports
