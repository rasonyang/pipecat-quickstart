# Project Context

## Purpose

Pipecat quickstart project for building voice AI bots. Creates conversational AI bots that accept voice input, process through LLM, and respond with synthesized speech. Supports local development and Pipecat Cloud deployment.

## Tech Stack

- Python 3.12+ with `uv` package manager
- Pipecat AI framework (voice bot pipelines)
- Deepgram (Speech-to-Text)
- OpenAI (LLM inference)
- Cartesia (Text-to-Speech)
- Daily/WebRTC for transport (with SIP transport in development)
- Silero VAD (Voice Activity Detection)
- LocalSmartTurnAnalyzerV3 (turn detection)

## Project Conventions

### Code Style

- Line length: 100 characters (configured in `pyproject.toml`)
- Import sorting: Managed by `ruff` with `select = ["I"]`
- Type checking: `pyright` for static type analysis
- Linting: `ruff check` with auto-fix support

**Commands:**
```bash
uv run pyright          # Type checking
uv run ruff check       # Linting
uv run ruff check --fix # Auto-fix issues
```

### Architecture Patterns

**Pipeline Architecture:**
- Linear frame processing: `Transport Input → STT → LLM Context (User) → LLM → TTS → Transport Output → LLM Context (Assistant)`
- Frame-based processing: Audio, text, and LLM outputs flow as frame objects
- Event-driven: Handlers for client connection/disconnection lifecycle

**Single-file design:**
- Main bot logic in `bot.py` (simple, straightforward)
- Favor simplicity over abstraction
- Minimal dependencies, proven patterns

### Testing Strategy

- Manual testing during development via browser at http://localhost:7860
- Type checking required before commits (`pyright`)
- Import sorting validation (`ruff check`)
- End-to-end testing: Connect → speak → verify response → disconnect

### Git Workflow

- Main branch: `main`
- Untracked files: `.claude/`, `openspec/` (local development)
- Commit style: Short, descriptive messages focusing on "why"
- No force push to main/master

## Domain Context

**Voice AI Pipeline:**
- **STT (Speech-to-Text)**: Converts caller audio to text
- **LLM Context**: Maintains conversation history (system + user + assistant messages)
- **LLM**: Processes user input and generates responses
- **TTS (Text-to-Speech)**: Converts LLM text responses to audio
- **Transport**: Handles audio I/O (WebRTC, Daily, or SIP)

**Key Concepts:**
- **VAD**: Voice Activity Detection to determine when user is speaking
- **Turn Detection**: Identifies conversation turn boundaries
- **RTVI**: Real-Time Voice Interface processor for bot control
- **Frame**: Base unit of pipeline processing (audio, text, LLM outputs)

## Important Constraints

- Python 3.12+ required for dependency compatibility
- First run downloads models (~20 seconds delay)
- API keys required for all services (Deepgram, OpenAI, Cartesia)
- Local development serves on http://localhost:7860
- Docker required for Pipecat Cloud deployment
- Network ports: HTTP 7860 (local), SIP UDP 6060, RTP UDP 10000-15000 (when using SIP)

## External Dependencies

**AI Services (API keys required):**
- Deepgram: Real-time speech-to-text (DEEPGRAM_API_KEY)
- OpenAI: LLM inference (OPENAI_API_KEY)
- Cartesia: Text-to-speech synthesis (CARTESIA_API_KEY)
- Daily: WebRTC transport (DAILY_API_KEY, optional)

**Pipecat Framework:**
- `pipecat-ai[webrtc,daily,silero,deepgram,openai,cartesia,local-smart-turn-v3,runner]`
- BaseTransport abstraction for pluggable transports
- Pipeline/Task/Runner for bot execution
- Frame types for data flow

**Deployment:**
- Pipecat CLI: `pipecat-ai-cli` for cloud deployment
- Docker Hub: Image repository for cloud builds
- Pipecat Cloud: Production hosting platform
