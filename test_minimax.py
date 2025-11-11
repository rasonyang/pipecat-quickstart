import os
import asyncio
import aiohttp
from dotenv import load_dotenv

load_dotenv()

async def test_minimax():
    api_key = os.getenv("MINIMAX_API_KEY")
    group_id = os.getenv("MINIMAX_GROUP_ID")
    base_url = f"https://api.minimaxi.chat/v1/t2a_v2?GroupId={group_id}"
    
    headers = {
        "accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    
    payload = {
        "stream": True,
        "model": "speech-01-turbo",
        "text": "Hello, testing MiniMax TTS!",
        "voice_setting": {
            "voice_id": "male-qn-qingse",
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 8000,
            "bitrate": 128000,
            "format": "pcm",
            "channel": 1,
        },
        "language_boost": "English"
    }
    
    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        print(f"Making request to: {base_url}")
        print(f"Payload: {payload}")
        
        async with session.post(base_url, headers=headers, json=payload) as response:
            print(f"Response status: {response.status}")
            print(f"Response headers: {response.headers}")
            
            if response.status != 200:
                text = await response.text()
                print(f"Error response: {text}")
                return
            
            # Try to read response
            chunk_count = 0
            byte_count = 0
            async for chunk in response.content.iter_chunked(8192):
                chunk_count += 1
                byte_count += len(chunk)
                if chunk_count <= 3:
                    print(f"Chunk #{chunk_count}: {len(chunk)} bytes, first 100 bytes: {chunk[:100]}")
            
            print(f"Total chunks: {chunk_count}, total bytes: {byte_count}")

asyncio.run(test_minimax())
