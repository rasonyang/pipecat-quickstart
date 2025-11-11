#!/bin/bash

# Test script to verify SIP server is working

echo "Starting SIP bot..."
uv run python3 bot.py --transport sip 2>&1 | tee sip_test.log &
BOT_PID=$!

echo "Bot PID: $BOT_PID"
sleep 3

echo ""
echo "Checking if SIP server is listening on port 6060..."
netstat -an | grep 6060 || lsof -i :6060

echo ""
echo "Press Ctrl+C to stop the bot"
echo "Log file: sip_test.log"

wait $BOT_PID
