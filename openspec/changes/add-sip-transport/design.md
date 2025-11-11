# SIP Transport Design

## Context

The Pipecat quickstart currently supports Daily and WebRTC transports for browser-based voice interactions. To enable telephony integration, we need to add SIP (Session Initiation Protocol) transport that can accept incoming calls from traditional phone systems, SIP trunks, or VoIP clients.

Reference implementation: https://github.com/aicc2025/sip-to-ai

**Constraints:**
- Must integrate cleanly with existing Pipecat transport abstraction
- Must maintain compatibility with existing Daily/WebRTC transports
- Should leverage Pipecat framework patterns rather than reimplementing everything
- Must support standard telephony codec (G.711 μ-law)

## Goals / Non-Goals

**Goals:**
- Add SIP as a selectable transport option via `create_transport()` mechanism
- Support incoming SIP calls (server mode) with proper SIP signaling (INVITE, ACK, BYE)
- Handle RTP audio streaming with G.711 μ-law codec
- Integrate seamlessly with existing pipeline (STT → LLM → TTS)
- Configure via environment variables for deployment flexibility

**Non-Goals:**
- Outgoing call support (client mode) - deferred to future work
- Advanced SIP features (authentication, registration, call transfer, hold)
- Multiple codec support - only G.711 μ-law initially
- DTMF handling - can be added later if needed
- Recording or call analytics - handled by pipeline processors

## Decisions

### Decision: Use Pipecat Transport Pattern

**Rationale:**
- Maintains consistency with existing transports (Daily, WebRTC)
- Leverages Pipecat's transport abstraction for input/output frames
- Simplifies integration with pipeline components
- Follows project architecture conventions

**Alternative considered:**
- Pure Python asyncio implementation (sip-to-ai style) - More code duplication, doesn't leverage Pipecat framework

### Decision: Server Mode Only (Incoming Calls)

**Rationale:**
- Simpler implementation for initial version
- Matches common use case (bot as service endpoint)
- Reference implementation provides proven patterns
- Can add client mode later without breaking changes

### Decision: G.711 μ-law Only

**Rationale:**
- Standard telephony codec, universally supported
- 8kHz sample rate matches PSTN
- Simple codec (no compression complexity)
- Reference implementation validates this choice

### Decision: Environment Variable Configuration

**Rationale:**
- Consistent with existing config pattern (.env file)
- Easy deployment to Pipecat Cloud (via secrets)
- No code changes needed for different environments

**Configuration variables:**
```bash
SIP_SERVER_HOST=172.16.204.89
SIP_SERVER_PORT=6060
SIP_RTP_PORT_RANGE=10000-15000
```

## Architecture

### Component Structure

```
SIPTransport (extends BaseTransport)
├── SIPServer (handles SIP signaling)
│   ├── INVITE handler → creates call session
│   ├── ACK handler → confirms session
│   └── BYE handler → terminates session
├── RTPSession (handles audio streaming)
│   ├── Receiver thread → raw audio → AudioRawFrame
│   └── Sender thread → AudioRawFrame → RTP packets
└── Audio codec conversion (G.711 ↔ PCM16)
```

### Audio Flow

```
Incoming call:
SIP INVITE → Create RTPSession → RTP receive (G.711)
  → Convert to PCM16 → AudioRawFrame
  → DeepgramSTTService → LLM → CartesiaTTSService
  → AudioRawFrame → Convert to G.711 → RTP send

Outgoing audio:
Pipeline output → SIPTransport.output() → RTP packets
```

### Integration Points

1. **Transport Factory** (`bot.py`):
   - Add `"sip"` key to `transport_params` dictionary
   - Return `SIPTransport` instance with `SIPParams`

2. **Pipeline** (unchanged):
   - `transport.input()` receives audio frames from RTP
   - `transport.output()` sends audio frames to RTP

3. **Runner Arguments**:
   - Add `--transport sip` CLI option
   - Load SIP config from environment variables

## Implementation Strategy

### Phase 1: SIP Transport Foundation
1. Create `SIPTransport` class extending `BaseTransport`
2. Implement `SIPParams` for configuration
3. Add basic SIP server (INVITE, ACK, BYE handling)
4. Implement RTP session management

### Phase 2: Audio Integration
1. Implement G.711 ↔ PCM16 codec conversion
2. Wire up `input()` method to receive RTP audio
3. Wire up `output()` method to send RTP audio
4. Handle audio frame buffering and timing

### Phase 3: Bot Integration
1. Add SIP transport to `bot.py` transport factory
2. Add environment variables to `.env.example`
3. Update `pcc-deploy.toml` if needed for port configuration
4. Test end-to-end call flow

## Risks / Trade-offs

### Risk: UDP Port Range Management
RTP requires dynamic port allocation from a configured range. Multiple concurrent calls need different ports.

**Mitigation:**
- Implement port pool management
- Track allocated ports per call session
- Release ports on call termination
- Configure sufficiently large port range (5000 ports: 10000-15000)

### Risk: Network Configuration Complexity
SIP/RTP requires specific firewall rules and network setup, especially in cloud deployments.

**Mitigation:**
- Document required firewall rules clearly
- Provide deployment examples for common scenarios
- Add network troubleshooting guide
- Use standard ports (6060 for SIP, 10000-15000 for RTP)

### Risk: Audio Quality Issues
G.711 at 8kHz is lower quality than typical WebRTC (48kHz). Conversion between formats may introduce artifacts.

**Mitigation:**
- Use proven codec libraries (e.g., audioop or pydub)
- Test with various SIP clients and trunks
- Document expected audio quality characteristics
- Consider future support for wideband codecs (G.722)

### Trade-off: Feature Simplicity vs Completeness
Starting with server mode only and G.711 only limits functionality but reduces complexity.

**Justification:**
- Covers primary use case (inbound calls to bot)
- Faster implementation and testing
- Clear path for future enhancements
- Reference implementation validates minimal approach

## Migration Plan

**No migration needed** - this is a new feature addition. Existing Daily/WebRTC deployments are unaffected.

**Adoption path:**
1. Add SIP configuration to `.env` file
2. Change `--transport` argument from `daily` to `sip`
3. Deploy with updated network configuration
4. Point SIP trunk or client to bot's SIP server address

**Rollback:**
Simply switch back to `--transport daily` or `--transport webrtc`.

## Open Questions

1. **Should we add SIP authentication (digest auth)?**
   - Initial: No (assume trusted network or upstream SIP proxy handles auth)
   - Can add later if needed for public-facing deployments

2. **How should we handle SIP registration/presence?**
   - Initial: Not needed for server mode (bot is always available)
   - Client mode (future) would need registration

3. **Should we support SIP over TCP or TLS in addition to UDP?**
   - Initial: UDP only (simplest, most common)
   - Can add TCP/TLS later for NAT traversal or security requirements

4. **How should we integrate with Pipecat Cloud deployment?**
   - Needs investigation: Does Pipecat Cloud support custom UDP ports?
   - May need special deployment configuration or load balancer setup
