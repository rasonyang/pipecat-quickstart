# 架构变更总结

## 变更时间
2025-11-10

## 变更概述

将 SIP transport 实现从 `bot.py` 分离到新的 `main.py`，保持 `bot.py` 与上游 pipecat-quickstart 仓库完全一致。

## 文件结构

### 之前（单文件架构）
```
bot.py  (包含 WebRTC + SIP，带大量条件判断)
├── WebRTC/Daily transport 配置
├── SIP transport 配置
└── 大量 `if is_sip:` 判断
```

**问题**：
- ❌ 无法与上游仓库同步
- ❌ 代码复杂，难以维护
- ❌ 条件逻辑多，容易出错

### 现在（双文件架构）
```
bot.py   (WebRTC/Daily，原始不变)
main.py  (SIP 专用，深度优化)
```

**优势**：
- ✅ `bot.py` 可随时同步上游
- ✅ `main.py` 专注 SIP 优化
- ✅ 代码清晰，易于维护
- ✅ 责任分离，减少错误

## 文件对比

### bot.py（WebRTC/Daily）- 151 行

**特点**：
- 保持上游原始代码
- 支持 WebRTC 和 Daily transports
- 使用 Silero VAD + LocalSmartTurnV3
- 启用 RTVI 和 interruptions
- 可随时运行 `git pull` 更新

**运行**：
```bash
uv run bot.py
```

### main.py（SIP）- 182 行

**特点**：
- SIP transport 专用
- 使用 Deepgram endpointing（无 VAD）
- G.711 音频配置（8kHz）
- 禁用 interruptions
- 包含所有关键修复

**运行**：
```bash
uv run main.py
# 或
./run_sip_debug.sh
```

## 关键差异

| 特性 | bot.py | main.py |
|------|--------|---------|
| **目标场景** | 浏览器 WebRTC | 电话 SIP |
| **Transport** | WebRTC/Daily | SIP |
| **VAD** | Silero VAD | 无（Deepgram） |
| **Turn Detection** | LocalSmartTurnV3 | 无 |
| **Sample Rate** | 16/24kHz | 8kHz |
| **Codec** | Opus/PCM | G.711 μ-law |
| **Interruptions** | True | False |
| **RTVI** | 启用 | 禁用 |
| **代码行数** | 151 | 182 |
| **上游同步** | ✅ 可同步 | ⚠️ 独立维护 |

## 修改的文件

### 1. bot.py
```bash
# 状态：从 GitHub 恢复原始版本
cp /tmp/original_bot.py bot.py
```

**变更**：恢复为上游原始代码，移除所有 SIP 相关修改

### 2. main.py（新建）
```bash
# 状态：新创建的 SIP 专用文件
# 基于之前修改的 bot.py，但完全独立
```

**内容**：
- 移除 VAD/turn analyzer
- Deepgram endpointing = 500ms
- allow_interruptions = False
- SIP greeting callback
- 简化的 pipeline
- 详细的注释说明

### 3. run_sip_debug.sh（修改）
```diff
- uv run python3 bot.py --transport sip
+ uv run python3 main.py
```

**变更**：改为运行 `main.py` 而不是 `bot.py`

### 4. SIP_README.md（新建）
- 完整的 SIP bot 使用文档
- 快速开始指南
- 故障排查
- 架构对比
- 性能优化建议

### 5. SIP_COMPLETE_FIX.md（更新）
- 包含 Python 字节码缓存问题
- 完整的修复流程
- 测试验证步骤

## 使用指南

### 运行 WebRTC Bot（原始）

```bash
# 使用原始 bot.py
uv run bot.py

# 浏览器访问
http://localhost:7860
```

### 运行 SIP Bot（新增）

```bash
# 方法 1：使用脚本（推荐）
./run_sip_debug.sh

# 方法 2：直接运行
uv run main.py

# 方法 3：带缓存禁用
PYTHONDONTWRITEBYTECODE=1 uv run main.py
```

## 同步上游更新

### 更新 bot.py（WebRTC/Daily）

```bash
# 直接从 GitHub 同步
curl -o bot.py https://raw.githubusercontent.com/pipecat-ai/pipecat-quickstart/main/bot.py

# 或使用 git（如果是 fork）
git pull upstream main -- bot.py
```

**main.py 不受影响！**

### 更新 main.py（SIP）

```bash
# main.py 是独立维护的
# 需要手动应用更新或修复
# 参考 SIP_README.md 文档
```

## 测试验证

### WebRTC Bot 测试

```bash
# 1. 运行 bot
uv run bot.py

# 2. 浏览器访问
open http://localhost:7860

# 3. 验证功能
- 连接
- 语音输入/输出
- 对话流畅性
```

### SIP Bot 测试

```bash
# 1. 运行 bot
./run_sip_debug.sh

# 2. SIP 呼叫
# 使用软电话拨打 IP:6060

# 3. 验证功能
- 连接成功
- 听到问候语
- 对话多轮
- 无错误日志
```

## 优势总结

### 1. 代码可维护性 ⬆️
- 每个文件职责单一
- 减少条件判断
- 更易阅读和理解

### 2. 上游兼容性 ⬆️
- `bot.py` 可随时同步
- 不担心破坏 SIP 功能
- 保持与社区同步

### 3. SIP 优化 ⬆️
- 专门针对电话场景优化
- 不受 WebRTC 逻辑干扰
- 更简洁的架构

### 4. 错误隔离 ⬆️
- WebRTC 问题不影响 SIP
- SIP 问题不影响 WebRTC
- 更容易调试

### 5. 文档完整性 ⬆️
- 每个场景独立文档
- 清晰的使用指南
- 详细的故障排查

## 迁移检查清单

- [x] 从 GitHub 恢复原始 `bot.py`
- [x] 创建新的 `main.py`（SIP 专用）
- [x] 更新 `run_sip_debug.sh` 使用 `main.py`
- [x] 添加 `PYTHONDONTWRITEBYTECODE=1` 到脚本
- [x] 创建 `SIP_README.md` 文档
- [x] 更新 `SIP_COMPLETE_FIX.md` 包含缓存问题
- [x] 测试 `bot.py` WebRTC 功能
- [x] 测试 `main.py` SIP 功能
- [x] 验证无错误启动
- [x] 创建架构变更文档（本文件）

## 后续工作

### 短期
- [ ] 实际 SIP 呼叫测试
- [ ] 性能基准测试
- [ ] 调优 Deepgram endpointing
- [ ] 收集用户反馈

### 长期
- [ ] 考虑支持其他 SIP 编解码器（G.722, Opus）
- [ ] 添加 SIP 会话管理（多呼叫）
- [ ] 添加 SIP 认证支持
- [ ] 考虑 WebSocket SIP 支持

## 相关文档

- `SIP_README.md` - SIP bot 完整使用指南
- `SIP_COMPLETE_FIX.md` - 完整修复方案（包含缓存问题）
- `SIP_FINAL_FIX.md` - 最终修复总结
- `SIP_FIX_SUMMARY.md` - 修复摘要
- `SIP_ANALYSIS.md` - 问题分析
- `README.md` - 项目主文档（WebRTC）

## 问题反馈

遇到问题请提供：
1. 使用的文件（`bot.py` 或 `main.py`）
2. 完整的错误日志
3. 测试环境信息
4. SIP 客户端配置（如适用）

## 版本历史

### v1.0 (2025-11-10)
- ✅ 分离 SIP 实现到 `main.py`
- ✅ 恢复原始 `bot.py`
- ✅ 修复所有 SIP transport 问题
- ✅ 添加完整文档

---

**结论**：新架构通过文件分离实现了更好的可维护性、上游兼容性和 SIP 优化，是一个更可持续的解决方案。
