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
- Deepgram (Speech-to-Text)
- OpenAI (LLM inference)
- Cartesia (Text-to-Speech)
- Daily/WebRTC/SIP for transport

## Development Commands

### Initial Setup

```bash
# Install dependencies
uv sync

# Configure environment variables
cp env.example .env
# Edit .env with your API keys (DEEPGRAM_API_KEY, OPENAI_API_KEY, CARTESIA_API_KEY)
```

### Running Locally

#### WebRTC Transport (Default)

```bash
# Run the bot (serves on http://localhost:7860)
uv run bot.py
```

First run may take ~20 seconds to download models. Open http://localhost:7860 in browser and click "Connect" to interact with the bot.

#### SIP Transport (Telephony)

```bash
# Run with SIP transport for incoming telephone calls
uv run bot.py --transport sip
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
- `Dockerfile`: Multi-stage build using `dailyco/pipecat-base` image
