#!/bin/bash

echo "🔍 Looking for bot processes..."

# Find and kill bot.py processes
PIDS=$(ps aux | grep "bot.py" | grep -v grep | awk '{print $2}')

if [ -z "$PIDS" ]; then
    echo "✅ No bot processes found"
else
    echo "Found bot process(es): $PIDS"
    echo "💀 Killing..."
    kill -9 $PIDS
    echo "✅ Done"
fi
