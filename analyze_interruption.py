import re

with open('sip_bot.log', 'r') as f:
    lines = f.readlines()

bot_speaking = False
bot_start_time = None

for line in lines:
    # Track bot speaking state
    if '🗣️ Bot started speaking' in line:
        bot_speaking = True
        match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if match:
            bot_start_time = match.group(1)
        print(f"\n{'='*60}")
        print(f"BOT STARTED SPEAKING at {bot_start_time}")
        print(f"{'='*60}")
    
    elif '🔇 Bot stopped speaking' in line or 'Bot stopped speaking' in line:
        if bot_speaking:
            match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            stop_time = match.group(1) if match else "unknown"
            print(f"BOT STOPPED SPEAKING at {stop_time}\n")
            bot_speaking = False
    
    # Show VAD and audio during bot speaking
    if bot_speaking:
        if 'VAD: Speech STARTING' in line or 'VAD: Speech STOPPING' in line:
            print(f"  VAD: {line.strip()}")
        elif '📥 RX audio' in line and 'RMS=' in line:
            # Extract RMS value
            match = re.search(r'RMS=([\d.]+)', line)
            if match:
                rms = float(match.group(1))
                if rms > 50:  # Show significant audio
                    print(f"  HIGH RX: {line.strip()}")
        elif 'interrupting' in line:
            print(f"  >>> INTERRUPTION: {line.strip()}")
