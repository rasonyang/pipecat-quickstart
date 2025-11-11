# SIP Transport Specification

## ADDED Requirements

### Requirement: SIP Server Initialization

The system SHALL provide a SIP transport that accepts incoming SIP calls and integrates with the Pipecat pipeline framework.

#### Scenario: SIP server starts successfully

- **WHEN** the bot is launched with `--transport sip` argument
- **THEN** the SIP server SHALL bind to the configured host and port (SIP_SERVER_HOST:SIP_SERVER_PORT)
- **AND** the server SHALL listen for incoming SIP INVITE requests on UDP protocol
- **AND** the server SHALL allocate RTP port range from SIP_RTP_PORT_RANGE configuration

#### Scenario: Multiple concurrent calls supported

- **WHEN** the SIP server receives multiple INVITE requests simultaneously
- **THEN** each call SHALL be handled in a separate session with unique RTP ports
- **AND** each session SHALL maintain independent audio streams
- **AND** port allocation SHALL not conflict between sessions

### Requirement: SIP Call Signaling

The system SHALL handle SIP protocol messages for incoming call establishment and termination.

#### Scenario: Incoming call accepted

- **WHEN** the SIP server receives an INVITE request with SDP offer
- **THEN** the server SHALL respond with 200 OK including SDP answer with G.711 μ-law codec
- **AND** the SDP answer SHALL specify allocated RTP port for this call
- **AND** an RTP session SHALL be created for bidirectional audio streaming

#### Scenario: Call establishment confirmed

- **WHEN** the SIP server receives an ACK request after 200 OK response
- **THEN** the call session SHALL be marked as established
- **AND** audio streaming SHALL begin immediately
- **AND** the pipeline SHALL be triggered to start processing audio

#### Scenario: Call termination

- **WHEN** the SIP server receives a BYE request
- **THEN** the server SHALL respond with 200 OK
- **AND** the RTP session SHALL be closed
- **AND** allocated RTP port SHALL be released back to the port pool
- **AND** the pipeline task SHALL be cancelled gracefully

### Requirement: RTP Audio Streaming

The system SHALL handle Real-time Transport Protocol (RTP) audio packets for bidirectional voice communication.

#### Scenario: Incoming audio received

- **WHEN** RTP packets are received from the caller
- **THEN** the system SHALL extract G.711 μ-law audio payload from RTP packets
- **AND** the audio SHALL be converted from G.711 μ-law to PCM16 format
- **AND** PCM16 audio SHALL be encapsulated in AudioRawFrame objects
- **AND** frames SHALL be pushed to the pipeline input for STT processing

#### Scenario: Outgoing audio sent

- **WHEN** the pipeline outputs AudioRawFrame objects (from TTS)
- **THEN** the system SHALL convert PCM16 audio to G.711 μ-law format
- **AND** the audio SHALL be packetized into RTP packets with proper headers
- **AND** RTP packets SHALL be sent to the caller's address and port
- **AND** timing SHALL maintain 20ms frame intervals for smooth playback

#### Scenario: RTP session timing maintained

- **WHEN** audio is being streamed in either direction
- **THEN** RTP packets SHALL be sent/received at 50 packets per second (20ms intervals)
- **AND** sequence numbers SHALL increment monotonically for each packet
- **AND** timestamps SHALL reflect actual audio timing for synchronization

### Requirement: Audio Codec Support

The system SHALL support G.711 μ-law (PCMU) codec for telephony compatibility.

#### Scenario: G.711 μ-law encoding

- **WHEN** PCM16 audio frames are received from the TTS service
- **THEN** the audio SHALL be converted to G.711 μ-law format at 8kHz sample rate
- **AND** the conversion SHALL produce 160 bytes per 20ms frame
- **AND** the encoded audio SHALL be suitable for RTP transmission

#### Scenario: G.711 μ-law decoding

- **WHEN** G.711 μ-law audio is received in RTP packets
- **THEN** the audio SHALL be decoded to PCM16 format at 16kHz sample rate (for STT compatibility)
- **AND** sample rate conversion from 8kHz to 16kHz SHALL be performed if required by STT service
- **AND** the decoded audio quality SHALL be sufficient for accurate speech recognition

### Requirement: SIP Transport Configuration

The system SHALL be configurable via environment variables for deployment flexibility.

#### Scenario: Environment configuration loaded

- **WHEN** the bot starts with SIP transport
- **THEN** the system SHALL read SIP_SERVER_HOST, SIP_SERVER_PORT, and SIP_RTP_PORT_RANGE from environment
- **AND** default values SHALL be used if variables are not set (host: 0.0.0.0, port: 6060, RTP range: 10000-15000)
- **AND** configuration validation SHALL occur at startup with clear error messages for invalid values

#### Scenario: Invalid configuration detected

- **WHEN** SIP_RTP_PORT_RANGE is invalid (e.g., end < start, range too small)
- **THEN** the system SHALL raise a configuration error at startup
- **AND** an error message SHALL indicate the specific validation failure
- **AND** the bot SHALL not start until configuration is corrected

### Requirement: Transport Integration

The system SHALL integrate SIP transport with the existing Pipecat transport factory and pipeline.

#### Scenario: SIP transport factory registered

- **WHEN** the bot.py `transport_params` dictionary is initialized
- **THEN** a "sip" key SHALL be present with SIPParams factory function
- **AND** SIPParams SHALL configure audio settings compatible with the pipeline
- **AND** the transport SHALL be selectable via `--transport sip` CLI argument

#### Scenario: Pipeline connection established

- **WHEN** the SIP transport is created via `create_transport()`
- **THEN** `transport.input()` SHALL be compatible with pipeline input processor
- **AND** `transport.output()` SHALL be compatible with pipeline output processor
- **AND** the transport SHALL emit appropriate events (on_client_connected, on_client_disconnected)

#### Scenario: Client connected event triggered

- **WHEN** a SIP call is established (ACK received)
- **THEN** the "on_client_connected" event handler SHALL be triggered
- **AND** the pipeline SHALL queue initial greeting message
- **AND** the bot SHALL begin responding to caller audio

#### Scenario: Client disconnected event triggered

- **WHEN** a SIP call terminates (BYE received)
- **THEN** the "on_client_disconnected" event handler SHALL be triggered
- **AND** the pipeline task SHALL be cancelled
- **AND** resources (RTP session, ports) SHALL be cleaned up
