#!/bin/bash

# Disable Python bytecode caching to ensure code changes take effect
export PYTHONDONTWRITEBYTECODE=1

# Enable detailed logging
export LOGURU_LEVEL=DEBUG

echo "🚀 Starting SIP bot with detailed logging..."
echo "📝 Logs will be saved to: sip_bot.log"
echo "🔧 Cache disabled to ensure latest code is used"
echo ""

# Run SIP bot (main.py) and tee output to both screen and file
uv run main.py 2>&1 | tee sip_bot.log
