# LLM Proxy Gateway 改动总结

本文档总结了项目的所有重要改动和对应的测试验证。

## 最新改动 (2026-04-29)

### 移除交叉协议转换，仅支持同协议透传
**改动内容**:
- 移除所有 OpenAI ↔ Claude 交叉协议转换代码
- 下游协议必须与上游协议一致
- 如果上游模型不支持请求的协议格式，返回 400 错误

**架构**:
- **OpenAI → OpenAI**: 完全透传
- **Claude → Claude**: 完全透传
- ~~OpenAI → Claude~~: **已移除**
- ~~Claude → OpenAI~~: **已移除**

**文件**:
- `app/models.py` (移除转换相关模型)
- `app/proxy.py` (仅保留 `*_passthrough` 函数)
- `app/main.py` (移除转换逻辑，添加协议不匹配错误处理)
- `test_proxy.py` (移除交叉协议测试)

**测试覆盖**:
- ✅ 测试 #1-2: OpenAI → OpenAI (流式/非流式)
- ✅ 测试 #3-4: Claude → Claude (流式/非流式)
- ✅ 测试 #14-15: Tool Use (同协议)

---

### 完全透传 passthrough 调用
**改动内容**:
- `call_*_passthrough` 和 `stream_*_passthrough` 完全透传所有参数
- 使用 `model_dump(exclude_none=True)` 导出所有字段（包括 extra 字段）
- 流式响应原样转发每一行（包括 `data:`, `[DONE]` 等）

**文件**:
- `app/proxy.py` (passthrough 函数简化)

---

### 去掉参数验证（允许额外字段）
**改动内容**:
- `ClaudeRequest` 和 `OpenAIRequest` 使用 `ConfigDict(extra="allow")`
- 可以接收任何未来可能添加的参数而不会报错

**文件**:
- `app/models.py`

---

## 测试用例列表

| # | 测试名称 | 验证内容 |
|---|---------|---------|
| 1 | OpenAI → OpenAI (Stream) | OpenAI 格式流式响应，中文正常显示 |
| 2 | OpenAI → OpenAI (Non-Stream) | OpenAI 格式非流式响应 |
| 3 | Claude → Claude (Stream) | Claude 格式流式响应 |
| 4 | Claude → Claude (Non-Stream) | Claude 格式非流式响应 |
| 5 | reasoning_effort 参数测试 | 参数正常传递 |
| 6 | 中文输出不被转义 | ensure_ascii=False 验证 |
| 7 | 单次 message_delta | 不重复返回 stop_reason |
| 8 | 格式优先选择 | 下游 OpenAI 优先上游 OpenAI，下游 Claude 优先上游 Anthropic |
| 9 | Claude Content: string | string 格式 content |
| 10 | Claude Content: array[text] | array[text] 格式 content |
| 11 | Claude Content: thinking | thinking 类型 content |
| 12 | Claude Content: tool_use | tool_use 类型 content |
| 13 | Claude Content: mixed | 混合格式支持 |
| 14 | Tool Use: OpenAI → OpenAI | OpenAI 格式工具调用 |
| 15 | Tool Use: Claude → Claude | Claude 格式工具调用 |
| 16 | Unsupported Protocol Error | 不支持的协议返回 400 错误 |

## 运行测试

```bash
# 运行所有测试
python test_proxy.py

# 运行特定测试
python test_proxy.py 1        # 只运行测试 #1
python test_proxy.py 1,3,5    # 运行多个测试

# 调试模式
python test_proxy.py 5 --debug

# 列出所有测试
python test_proxy.py --list
```

## 测试状态

✅ **15/15 测试通过 (100.0%)**

所有核心功能都有对应的测试验证。
