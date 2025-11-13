<!-- OPENSPEC:START -->
# OpenSpec Instructions

These instructions are for AI assistants working in this project.

Always open `@/openspec/AGENTS.md` when the request:
- Mentions planning or proposals (words like proposal, spec, change, plan)
- Introduces new capabilities, breaking changes, architecture shifts, or big performance/security work
- Sounds ambiguous and you need the authoritative spec before coding

Use `@/openspec/AGENTS.md` to learn:
- How to create and apply change proposals
- Spec format and conventions
- Project structure and guidelines

Keep this managed block so 'openspec update' can refresh the instructions.

<!-- OPENSPEC:END -->

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Pipecat quickstart project for building voice AI bots. The application creates a conversational AI bot that accepts voice input, processes it through an LLM, and responds with synthesized speech. The bot can run locally for development and be deployed to Pipecat Cloud for production.

**Technology Stack:**
- Python 3.12+ with `uv` package manager
- Pipecat AI framework for voice bot pipelines
- Tencent Cloud (Speech-to-Text) - custom service in `services/tencent_stt.py`
- Zhipu AI (LLM inference) - custom service in `services/glm_llm.py` (glm-4-flashx model)
- Cartesia/MiniMax (Text-to-Speech)
- Daily/WebRTC/SIP for transport

**Important Files:**
- `bot.py`: Main bot implementation supporting both WebRTC/Daily and SIP transports
- `main.py`: Alternative SIP-specific implementation with MiniMax TTS
- `services/`: Custom service implementations for Chinese AI providers (Tencent STT, Zhipu GLM)

## Development Commands

### Initial Setup

```bash
# Install dependencies
uv sync

# Configure environment variables
cp env.example .env
# Edit .env with your API keys (TENCENT_SECRET_ID, TENCENT_SECRET_KEY, TENCENT_APPID, ZHIPUAI_API_KEY, CARTESIA_API_KEY, MINIMAX_API_KEY, MINIMAX_GROUP_ID)
```

### Running Locally

The project has two main entry points:

**`bot.py` (Recommended):** Unified implementation supporting both WebRTC/Daily and SIP transports via command-line flag. Uses Cartesia TTS.

**`main.py` (Alternative):** SIP-specific implementation with MiniMax TTS and enhanced logging configuration.

#### WebRTC Transport (Default)

```bash
# Run the bot (serves on http://localhost:7860)
uv run bot.py
```

First run may take ~20 seconds to download models. Open http://localhost:7860 in browser and click "Connect" to interact with the bot.

#### SIP Transport (Telephony)

```bash
# Using bot.py (Cartesia TTS)
uv run bot.py --transport sip

# Using main.py (MiniMax TTS)
uv run main.py
```

Requires SIP configuration in `.env`:
```bash
SIP_SERVER_HOST=0.0.0.0
SIP_SERVER_PORT=6060
SIP_RTP_PORT_RANGE=10000-15000
```

**Network requirements:**
- UDP port 6060 for SIP signaling
- UDP ports 10000-15000 for RTP audio
- Point SIP trunk/client to bot server address

**Note:** SIP transport supports G.711 μ-law codec only.

### Code Quality

```bash
# Type checking
uv run pyright

# Linting (import sorting)
uv run ruff check

# Auto-fix linting issues
uv run ruff check --fix
```

### Testing and Debugging

```bash
# Test MiniMax TTS directly
uv run test_minimax_tts.py

# Test UDP connectivity (for SIP transport debugging)
./test_udp.py

# Kill all running bot processes
./kill_bot.sh

# Run SIP bot with debug logging
./run_sip_debug.sh
```

### Deployment to Pipecat Cloud

```bash
# Authenticate to Pipecat Cloud
pipecat cloud auth login

# Upload secrets from .env file
pipecat cloud secrets set quickstart-secrets --file .env

# Build and push Docker image
pipecat cloud docker build-push

# Deploy to production
pipecat cloud deploy
```

**Prerequisites for deployment:**
- Docker installed and logged in to Docker Hub (`docker login`)
- Pipecat CLI installed: `uv tool install pipecat-ai-cli`
- Update `pcc-deploy.toml` with your Docker Hub username in the `image` field

## Architecture

The bot is implemented in a single file `bot.py` with a pipeline architecture:

**Pipeline Flow:**
```
User Input → sip transport → STT (Speech-to-Text) → LLM Context Aggregator (User)
→ LLM → TTS (Text-to-Speech) → sip transport → LLM Context Aggregator (Assistant)
```

**Key Components:**

1. **Transport Layer**:
   - **WebRTC/Daily**: Browser-based connections with VAD (Voice Activity Detection) using Silero and turn detection using LocalSmartTurnAnalyzerV3
   - **SIP**: Telephony connections via SIP/RTP protocol for traditional phone systems (supports G.711 μ-law codec)

2. **Processing Pipeline**: Linear pipeline of processors that transform frames (audio, text, LLM outputs) sequentially

3. **LLM Context**: Maintains conversation history with system message and user/assistant messages

4. **RTVI**: Real-Time Voice Interface processor for standardized bot control (WebRTC/Daily only)

**Event Handlers:**
- `on_client_connected`: Triggers initial greeting when user connects
- `on_client_disconnected`: Cleans up task when user disconnects

## Configuration Files

- `pyproject.toml`: Python dependencies managed by `uv`
- `pcc-deploy.toml`: Pipecat Cloud deployment configuration (agent name, Docker image, secrets, scaling)
- `.env`: API keys for AI services (not committed to git)
- `env.example`: Template for required environment variables
- `Dockerfile`: Multi-stage build using `dailyco/pipecat-base` image

## Custom Services

The project includes custom service implementations for Chinese AI providers located in the `services/` directory:

### Tencent STT Service (`services/tencent_stt.py`)

Custom Speech-to-Text implementation using Tencent Cloud ASR WebSocket API.

**Key features:**
- Real-time streaming recognition via WebSocket
- Support for 8kHz (telephony) and 16kHz (general) audio
- Configurable VAD, profanity filtering, and hotword boosting
- Lazy connection mode for SIP transport (connect only when call is established)
- Audio passthrough disabled by default to prevent echo

**Configuration parameters:**
- `engine_model_type`: Recognition engine ("8k_zh" for telephony, "16k_zh" for general)
- `sample_rate`: Auto-inferred from model type (8000 or 16000 Hz)
- `filter_dirty`: Profanity filtering (0=off, 1=filter, 2=only_filter)
- `lazy_connect`: Delay connection until `await stt.connect()` is called (useful for SIP)
- `audio_passthrough`: Pass audio downstream (False prevents echo in SIP)

### GLM LLM Service (`services/glm_llm.py`)

Custom LLM implementation for Zhipu AI's GLM models using OpenAI-compatible API.

**Key features:**
- Streaming chat completions
- HTTP proxy bypass (`disable_proxy=True` by default for Chinese networks)
- OpenAI adapter for function calling support
- Token usage metrics

**Configuration parameters:**
- `model`: GLM model name (default: "glm-4-flashx")
- `base_url`: API endpoint (default: Zhipu AI endpoint)
- `disable_proxy`: Bypass HTTP proxy from environment (True recommended for direct connections)

## Network Configuration

**For Chinese AI Services (Tencent, Zhipu, MiniMax):**
- Set `disable_proxy=True` in service initialization to bypass HTTP proxies
- Tencent STT uses WebSocket (wss://asr.cloud.tencent.com)
- Zhipu GLM uses HTTPS (https://open.bigmodel.cn)
- Direct connections are recommended for lower latency

**For SIP Transport:**
- Ensure UDP ports are open (6060 for SIP signaling, 10000-15000 for RTP audio)
- Configure firewall rules for incoming SIP traffic
- Test UDP connectivity with `./test_udp.py`
