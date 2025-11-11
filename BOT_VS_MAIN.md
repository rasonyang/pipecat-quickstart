# bot.py vs main.py 对比

## 文件大小

- `bot.py`: 151 行 - WebRTC/Daily transport
- `main.py`: 149 行 - SIP transport

**几乎完全相同的结构！** ✅

## 主要差异

### 1. Import 部分

**bot.py** (加载 VAD 和 turn analyzer):
```python
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIObserver, RTVIProcessor
from pipecat.runner.utils import create_transport
from pipecat.transports.daily.transport import DailyParams
```

**main.py** (不需要 VAD/turn analyzer):
```python
from sip_transport import SIPParams, SIPTransport
# 不导入 VAD, turn analyzer, RTVI
```

### 2. run_bot() 函数

**bot.py** (默认配置):
```python
stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

tts = CartesiaTTSService(
    api_key=os.getenv("CARTESIA_API_KEY"),
    voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",
)
```

**main.py** (SIP 专用配置):
```python
stt = DeepgramSTTService(
    api_key=os.getenv("DEEPGRAM_API_KEY"),
    sample_rate=8000,        # G.711
    interim_results=True,    # 实时结果
    endpointing=500,         # 500ms 静音检测
)

tts = CartesiaTTSService(
    api_key=os.getenv("CARTESIA_API_KEY"),
    voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",
    sample_rate=8000,        # G.711
)
```

### 3. Pipeline 构建

**bot.py** (包含 RTVI):
```python
rtvi = RTVIProcessor(config=RTVIConfig(config=[]))

pipeline = Pipeline([
    transport.input(),
    rtvi,              # RTVI processor
    stt,
    context_aggregator.user(),
    llm,
    tts,
    transport.output(),
    context_aggregator.assistant(),
])
```

**main.py** (不包含 RTVI):
```python
pipeline = Pipeline([
    transport.input(),
    # 无 RTVI
    stt,
    context_aggregator.user(),
    llm,
    tts,
    transport.output(),
    context_aggregator.assistant(),
])
```

### 4. PipelineTask 配置

**bot.py**:
```python
task = PipelineTask(
    pipeline,
    params=PipelineParams(
        enable_metrics=True,
        enable_usage_metrics=True,
        # 默认 allow_interruptions
    ),
    observers=[RTVIObserver(rtvi)],
)
```

**main.py**:
```python
task = PipelineTask(
    pipeline,
    params=PipelineParams(
        enable_metrics=True,
        enable_usage_metrics=True,
        allow_interruptions=False,  # 关键！必须禁用
    ),
    # 无 observers
)
```

### 5. 连接处理

**bot.py** (event handlers):
```python
@transport.event_handler("on_client_connected")
async def on_client_connected(transport, client):
    logger.info(f"Client connected")
    messages.append({"role": "system", "content": "Say hello and briefly introduce yourself."})
    await task.queue_frames([LLMRunFrame()])

@transport.event_handler("on_client_disconnected")
async def on_client_disconnected(transport, client):
    logger.info(f"Client disconnected")
    await task.cancel()
```

**main.py** (SIP callback):
```python
# SIP greeting callback
async def on_sip_connected():
    logger.info(f"SIP call connected")
    messages.append({"role": "system", "content": "Say hello and briefly introduce yourself."})
    await task.queue_frames([LLMRunFrame()])

transport._greeting_callback = on_sip_connected
```

### 6. bot() 函数 - Transport 创建

**bot.py** (使用 create_transport):
```python
async def bot(runner_args: RunnerArguments):
    transport_params = {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
            turn_analyzer=LocalSmartTurnAnalyzerV3(),
        ),
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
            turn_analyzer=LocalSmartTurnAnalyzerV3(),
        ),
    }

    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)
```

**main.py** (直接创建 SIP transport):
```python
async def bot(runner_args: RunnerArguments):
    params = SIPParams(
        host=os.getenv("SIP_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SIP_SERVER_PORT", "6060")),
        rtp_port_start=int(os.getenv("SIP_RTP_PORT_RANGE", "10000-15000").split("-")[0]),
        rtp_port_end=int(os.getenv("SIP_RTP_PORT_RANGE", "10000-15000").split("-")[1]),
        audio_in_enabled=True,
        audio_out_enabled=True,
    )

    transport = SIPTransport(params)
    await transport.start()
    await run_bot(transport, runner_args)
```

### 7. __main__ 入口

**bot.py** (使用 pipecat runner):
```python
if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
```

**main.py** (直接运行):
```python
if __name__ == "__main__":
    import asyncio
    import sys
    from dataclasses import dataclass

    @dataclass
    class SimpleRunnerArgs:
        handle_sigint: bool = True

    runner_args = SimpleRunnerArgs()

    try:
        asyncio.run(bot(runner_args))
    except KeyboardInterrupt:
        logger.info("\n🛑 Shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}", exc_info=True)
        sys.exit(1)
```

## 关键修改总结

| 特性 | bot.py | main.py |
|------|--------|---------|
| **VAD** | ✅ Silero VAD | ❌ 无（Deepgram endpointing）|
| **Turn Analyzer** | ✅ LocalSmartTurnV3 | ❌ 无 |
| **RTVI** | ✅ 启用 | ❌ 禁用 |
| **Sample Rate** | 16kHz (默认) | 8kHz (G.711) |
| **Endpointing** | ❌ 无 | ✅ 500ms |
| **allow_interruptions** | True (默认) | False |
| **Observers** | ✅ RTVIObserver | ❌ 无 |
| **Transport** | WebRTC/Daily | SIP |
| **启动方式** | pipecat runner | 直接 asyncio |
| **连接事件** | event_handler | callback |

## 运行方式

**bot.py**:
```bash
uv run bot.py
# 浏览器访问 http://localhost:7860
```

**main.py**:
```bash
uv run main.py
# 或
./run_sip_debug.sh
# SIP 呼叫 IP:6060
```

## 测试验证

### bot.py 启动测试
```bash
$ uv run bot.py
🚀 Starting Pipecat bot...
⏳ Loading models and imports (20 seconds, first run only)
✅ All components loaded successfully!
INFO:     Uvicorn running on http://localhost:7860
```

### main.py 启动测试
```bash
$ uv run main.py
🚀 Starting Pipecat SIP bot...
⏳ Loading models and imports (5 seconds, first run only)
✅ All components loaded successfully!
✅ SIP server started on 172.16.204.89:6060
Pipeline ready
```

## 架构对比

### bot.py 架构
```
WebRTC/Daily → [Silero VAD] → [RTVI] → STT → Context → LLM → TTS → Output
                     ↓
          [LocalSmartTurnV3]
                     ↓
            [TurnTrackingObserver]
```

### main.py 架构
```
SIP → STT (Deepgram endpointing) → Context → LLM → TTS → Output
```

**main.py 更简单！** ✅

## 为什么这样设计？

### 1. 保持 bot.py 与上游一致
- ✅ 可以随时从 GitHub 同步
- ✅ 不破坏原有 WebRTC 功能
- ✅ 与社区保持同步

### 2. main.py 专注 SIP 优化
- ✅ 针对电话场景优化
- ✅ 简化架构（无 VAD）
- ✅ 包含所有必要修复

### 3. 结构高度相似
- ✅ 几乎相同的代码行数（151 vs 149）
- ✅ 相同的函数结构
- ✅ 相同的命名风格
- ✅ 易于理解和维护

## 结论

`main.py` 完全模仿 `bot.py` 的结构和风格，只是把 transport 改为 SIP，同时包含所有必要的 SIP 优化：

1. ✅ 无 VAD（使用 Deepgram endpointing）
2. ✅ 禁用 interruptions（避免 frame.id 错误）
3. ✅ 配置 G.711 音频（8kHz）
4. ✅ 移除 RTVI（SIP 不需要）
5. ✅ 简化架构（更少的组件）

**main.py 是 bot.py 的 SIP 版本，完全可用！** ✅
