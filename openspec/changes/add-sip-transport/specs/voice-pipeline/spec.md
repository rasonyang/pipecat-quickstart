# Voice Pipeline Specification

## MODIFIED Requirements

### Requirement: Transport Selection

The system SHALL support multiple transport types for voice connectivity, including Daily, WebRTC, and SIP.

#### Scenario: Daily transport selected

- **WHEN** the bot is launched with `--transport daily` argument
- **THEN** the system SHALL create a Daily transport with WebRTC capabilities
- **AND** the transport SHALL be configured with VAD and turn detection
- **AND** browser-based clients SHALL be able to connect

#### Scenario: WebRTC transport selected

- **WHEN** the bot is launched with `--transport webrtc` argument
- **THEN** the system SHALL create a WebRTC transport
- **AND** the transport SHALL be configured with VAD and turn detection
- **AND** WebRTC clients SHALL be able to connect

#### Scenario: SIP transport selected

- **WHEN** the bot is launched with `--transport sip` argument
- **THEN** the system SHALL create a SIP transport
- **AND** the transport SHALL be configured to accept incoming SIP calls
- **AND** SIP clients and trunks SHALL be able to connect via telephony protocol
