#!/usr/bin/env python3
"""Direct test of MiniMax TTS API."""

import asyncio
import os
import json
import aiohttp
from dotenv import load_dotenv

load_dotenv(override=True)


async def test_minimax_direct():
    """Test MiniMax API directly."""

    api_key = os.getenv("MINIMAX_API_KEY")
    group_id = os.getenv("MINIMAX_GROUP_ID")

    if not api_key or not group_id:
        print("❌ Missing API credentials")
        return

    print(f"🔑 API Key: {api_key[:20]}...")
    print(f"🆔 Group ID: {group_id}")

    url = "https://api.minimaxi.com/v1/t2a_v2"

    headers = {
        "accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": "speech-2.6-turbo",  # 使用最新的支持 emotion 的模型
        "text": "你好！",
        "stream": False,
        "voice_setting": {
            "voice_id": "female-tianmei",
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
            "emotion": "happy",
        },
        "audio_setting": {
            "sample_rate": 8000,
            "bitrate": 128000,
            "format": "pcm",
        },
        "group_id": group_id,
    }

    print(f"\n📤 Sending request to {url}")
    print(f"📝 Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")

    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        try:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as response:
                print(f"\n📥 Response status: {response.status}")
                print(f"📋 Response headers: {dict(response.headers)}")

                if response.status != 200:
                    error_text = await response.text()
                    print(f"❌ Error response: {error_text}")
                    return

                # Try to read response
                content_type = response.headers.get('Content-Type', '')
                print(f"📦 Content-Type: {content_type}")

                if 'json' in content_type:
                    data = await response.json()
                    print(f"\n✅ JSON Response:")
                    print(json.dumps(data, indent=2, ensure_ascii=False))

                    # Check for audio data
                    if 'data' in data and 'audio' in data['data']:
                        audio_hex = data['data']['audio']
                        print(f"\n🎵 Audio data length: {len(audio_hex)} chars (hex)")
                        print(f"🎵 Audio bytes: {len(audio_hex) // 2} bytes")

                        # Convert hex to bytes
                        audio_bytes = bytes.fromhex(audio_hex)
                        print(f"✅ Successfully converted to {len(audio_bytes)} bytes of audio")
                    else:
                        print("❌ No audio data in response!")
                else:
                    # Try reading as text
                    text = await response.text()
                    print(f"\n📄 Text response (first 500 chars):")
                    print(text[:500])

        except asyncio.TimeoutError:
            print("❌ Request timeout!")
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_minimax_direct())
