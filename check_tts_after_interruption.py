import re

with open('sip_bot.log', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '🛑 InterruptionFrame received' in line:
        match = re.search(r'(\d{2}:\d{2}:\d{2}\.\d{3})', line)
        interrupt_time = match.group(1) if match else "unknown"
        print(f"\n{'='*70}")
        print(f"INTERRUPTION at {interrupt_time}")
        print(f"{'='*70}")
        
        # Show next 20 lines to see what happens after interruption
        for j in range(i+1, min(i+20, len(lines))):
            next_line = lines[j]
            match = re.search(r'(\d{2}:\d{2}:\d{2}\.\d{3})', next_line)
            timestamp = match.group(1) if match else ""
            
            if any(keyword in next_line for keyword in [
                'Generating TTS',
                'SIP output: received frame',
                'MiniMax',
                'BotStopped',
                'TTSStopped',
                'AudioRawFrame'
            ]):
                # Highlight key events
                print(f"  {timestamp}: {next_line.strip()}")
