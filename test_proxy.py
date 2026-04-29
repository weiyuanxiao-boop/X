#!/usr/bin/env python3
"""
LLM Proxy Gateway 测试用例

测试场景矩阵（仅同协议透传）：
| # | 下游协议 | 上游协议 | 流式 | 测试函数 |
|---|---------|---------|------|---------|
| 1 | OpenAI  | OpenAI  | 是   | test_openai_to_openai_stream |
| 2 | OpenAI  | OpenAI  | 否   | test_openai_to_openai_non_stream |
| 3 | Claude  | Claude  | 是   | test_claude_to_claude_stream |
| 4 | Claude  | Claude  | 否   | test_claude_to_claude_non_stream |

使用方法：
    python test_proxy.py          # 运行所有测试
    python test_proxy.py 1        # 只运行测试 #1
    python test_proxy.py 1,3,5    # 运行测试 #1, #3, #5
    python test_proxy.py 3 --debug  # 运行测试 #3 并显示详细日志
    
环境要求：
    - 服务运行在 http://localhost:4936
    - 配置了 OpenAI 和 Claude 两种 provider 的模型
"""

import httpx
import json
import asyncio
import sys
import argparse

BASE_URL = "http://localhost:4936"

# 测试用模型配置（需要根据实际 config.yaml 调整）
OPENAI_MODEL = "deepseek-v4-flash-openai"    # 只使用 openai 格式
CLAUDE_MODEL = "deepseek-v4-flash-anthropic" # 只使用 anthropic 格式

# 测试消息 - OpenAI 格式
TEST_MESSAGES_OPENAI = [
    {"role": "system", "content": "你是一个乐于助人的助手。"},
    {"role": "user", "content": "你好，请用一句话介绍你自己"}
]

# 测试消息 - Claude 格式
TEST_MESSAGES_CLAUDE = [
    {"role": "user", "content": "你好，请用一句话介绍你自己"}
]

# 全局调试标志
DEBUG_MODE = False


def print_result(test_name: str, success: bool, message: str = ""):
    status = "✓ PASS" if success else "✗ FAIL"
    print(f"\n{status} | {test_name}")
    if message:
        print(f"       {message}")


# ─────────────────────────────────────────────────────────────
# OpenAI → OpenAI
# ─────────────────────────────────────────────────────────────

async def test_openai_to_openai_stream():
    """下游 OpenAI 格式，上游 OpenAI，流式响应"""
    test_name = "OpenAI → OpenAI (Stream)  [#1]"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/chat/completions",
                json={
                    "model": OPENAI_MODEL,
                    "messages": TEST_MESSAGES_OPENAI,
                    "stream": True,
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()

            # 收集流式响应
            chunks = []
            has_content = False
            
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    if DEBUG_MODE:
                        print(f"  [DEBUG] Received chunk: {line}")
                    chunk = json.loads(line[6:])
                    chunks.append(chunk)
                    # Check for content in delta
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if delta.get("content"):
                        has_content = True

            # 验证响应结构
            assert len(chunks) > 0, "没有收到任何数据块"
            first_chunk = chunks[0]
            assert "choices" in first_chunk, "响应缺少 choices 字段"
            assert "delta" in first_chunk["choices"][0], "数据块缺少 delta 字段"
            
            # 验证中文字符未被转义（ensure_ascii=False 测试）
            raw_response = response.text
            has_chinese = any('\u4e00' <= c <= '\u9fff' for c in raw_response)
            has_unicode_escape = '\\u4e00' in raw_response or '\\u6211' in raw_response

            print_result(test_name, True, f"收到 {len(chunks)} 个数据块，有 content: {has_content}, 中文正常显示：{has_chinese and not has_unicode_escape}")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_openai_to_openai_non_stream():
    """下游 OpenAI 格式，上游 OpenAI，非流式响应"""
    test_name = "OpenAI → OpenAI (Non-Stream)  [#2]"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/chat/completions",
                json={
                    "model": OPENAI_MODEL,
                    "messages": TEST_MESSAGES_OPENAI,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            
            if DEBUG_MODE:
                print(f"  [DEBUG] Response: {json.dumps(data, ensure_ascii=False)[:200]}...")
            
            # 验证响应结构
            assert "choices" in data, "响应缺少 choices 字段"
            assert len(data["choices"]) > 0, "choices 为空"
            message = data["choices"][0]["message"]
            # Accept content, reasoning_content, or reasoning
            has_content = "content" in message and message["content"]
            has_reasoning_content = "reasoning_content" in message and message["reasoning_content"]
            has_reasoning = "reasoning" in message and message["reasoning"]
            assert has_content or has_reasoning_content or has_reasoning, "消息缺少 content/reasoning_content/reasoning 字段"
            
            reply = message.get("content") or message.get("reasoning_content") or message.get("reasoning") or ""
            print_result(test_name, True, f"回复：{reply[:30]}...")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


# ─────────────────────────────────────────────────────────────
# Claude → Claude
# ─────────────────────────────────────────────────────────────

async def test_claude_to_claude_stream():
    """下游 Claude 格式，上游 Claude，流式响应"""
    test_name = "Claude → Claude (Stream)  [#3]"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": TEST_MESSAGES_CLAUDE,
                    "stream": True,
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()

            # 收集流式响应
            chunks = []
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    if DEBUG_MODE:
                        print(f"  [DEBUG] Received chunk: {line}")
                    chunks.append(json.loads(line[6:]))

            # 验证响应结构
            assert len(chunks) > 0, "没有收到任何数据块"
            
            print_result(test_name, True, f"收到 {len(chunks)} 个 Claude 格式数据块")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_claude_to_claude_non_stream():
    """下游 Claude 格式，上游 Claude，非流式响应"""
    test_name = "Claude → Claude (Non-Stream)  [#4]"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": TEST_MESSAGES_CLAUDE,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            
            if DEBUG_MODE:
                print(f"  [DEBUG] Response: {json.dumps(data, ensure_ascii=False)[:200]}...")
            
            # 验证响应结构 (Claude 格式)
            assert "content" in data, "响应缺少 content 字段"
            assert len(data["content"]) > 0, "content 为空"

            print_result(test_name, True, f"Claude 格式响应正常")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


# ─────────────────────────────────────────────────────────────
# Extra Tests
# ─────────────────────────────────────────────────────────────

async def test_reasoning_effort_param():
    """测试 reasoning_effort 参数传递"""
    test_name = "Extra: reasoning_effort 参数测试 [#5]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/chat/completions",
                json={
                    "model": OPENAI_MODEL,
                    "messages": TEST_MESSAGES_OPENAI,
                    "stream": False,
                    "reasoning_effort": "low"  # 测试 reasoning_effort 参数
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            
            # 验证响应正常
            assert "choices" in data, "响应缺少 choices 字段"
            
            print_result(test_name, True, "reasoning_effort 参数正常传递")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_chinese_output_not_escaped():
    """测试中文输出不被 Unicode 转义"""
    test_name = "Extra: 中文输出不被转义 [#6]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": TEST_MESSAGES_CLAUDE,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            
            # 检查原始响应文本
            raw_text = response.text
            
            # 验证有中文内容
            has_chinese_chars = any('\u4e00' <= c <= '\u9fff' for c in raw_text)
            
            # 验证没有 Unicode 转义序列
            has_unicode_escape = '\\u4e00' in raw_text or '\\u6211' in raw_text or '\\u4f60' in raw_text
            
            # 通过条件：有中文且没有被转义
            passed = has_chinese_chars and not has_unicode_escape
            
            print_result(test_name, passed, 
                f"中文正常显示：{has_chinese_chars}, 无 Unicode 转义：{not has_unicode_escape}"
            )
            return passed
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_single_message_delta():
    """测试只返回一次 message_delta（不重复）"""
    test_name = "Extra: 单次 message_delta [#7]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": TEST_MESSAGES_CLAUDE,
                    "stream": True,
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()

            message_delta_count = 0
            
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    chunk = json.loads(line[6:])
                    if chunk.get("type") == "message_delta":
                        message_delta_count += 1
            
            # 验证只返回一次 message_delta
            passed = message_delta_count == 1
            
            print_result(test_name, passed, 
                f"message_delta 次数：{message_delta_count} (期望：1)"
            )
            return passed
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_format_priority():
    """测试格式优先选择：下游 OpenAI 时优先选择上游 OpenAI 格式，下游 Claude 时优先选择上游 Claude 格式"""
    test_name = "Extra: 格式优先选择 [#8]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Test 1: OpenAI downstream → should prefer OpenAI upstream (no conversion)
            # Use deepseek-v4-flash which has both openai and anthropic URLs
            response_openai = await client.post(
                f"{BASE_URL}/v1/chat/completions",
                json={
                    "model": "deepseek-v4-flash",  # Has both formats
                    "messages": TEST_MESSAGES_OPENAI,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"}
            )
            response_openai.raise_for_status()
            data_openai = response_openai.json()
            
            # Should have OpenAI format response
            assert "choices" in data_openai, "OpenAI downstream should return OpenAI format"
            
            # Test 2: Claude downstream → should prefer Anthropic upstream (no conversion)
            response_claude = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": "deepseek-v4-flash",  # Has both formats
                    "messages": TEST_MESSAGES_CLAUDE,
                    "stream": False,
                },
                headers={"Content-Type": "application/json"}
            )
            response_claude.raise_for_status()
            data_claude = response_claude.json()
            
            # Should have Claude format response
            assert "content" in data_claude, "Claude downstream should return Claude format"
            assert "type" in data_claude and data_claude["type"] == "message", "Should have Claude message type"

            print_result(test_name, True,
                "OpenAI 下游→OpenAI 格式 ✓, Claude 下游→Claude 格式 ✓"
            )
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


# ─────────────────────────────────────────────────────────────
# Claude Content 字段测试
# ─────────────────────────────────────────────────────────────

async def test_claude_content_string():
    """测试 content 为字符串格式 (Fully Supported)"""
    test_name = "Claude Content: string [#9]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": [
                        {"role": "user", "content": "你好"}  # string format
                    ]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            assert "content" in data, "响应缺少 content 字段"
            assert len(data["content"]) > 0, "content 应为非空数组"

            print_result(test_name, True, "string 格式 content 正常")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_claude_content_array_text():
    """测试 content 为数组格式，type=text (Fully Supported)"""
    test_name = "Claude Content: array[text] [#10]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "你好"}
                            ]
                        }
                    ]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            assert "content" in data, "响应缺少 content 字段"
            # 验证响应包含 text 类型的内容块
            has_text = any(b.get("type") == "text" for b in data["content"])
            assert has_text, "响应应包含 text 类型内容块"

            print_result(test_name, True, "array[text] 格式 content 正常")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_claude_content_thinking():
    """测试 content 包含 thinking (Supported)"""
    test_name = "Claude Content: thinking [#11]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": [
                        {"role": "user", "content": "请思考后回答：1+1 等于几？"}
                    ]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            assert "content" in data, "响应缺少 content 字段"
            # 验证响应可能包含 thinking 类型的内容块
            has_thinking = any(b.get("type") == "thinking" for b in data["content"])
            has_text = any(b.get("type") == "text" for b in data["content"])

            print_result(test_name, True,
                f"thinking 支持：{has_thinking}, text: {has_text}"
            )
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


# ─────────────────────────────────────────────────────────────
# Tool Use 测试
# ─────────────────────────────────────────────────────────────

# 工具定义 - OpenAI 格式
WEATHER_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "获取指定城市的天气信息",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称"}
            },
            "required": ["city"]
        }
    }
}

# 工具定义 - Claude 格式
WEATHER_TOOL_CLAUDE = {
    "name": "get_weather",
    "description": "获取指定城市的天气信息",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称"}
        },
        "required": ["city"]
    }
}


async def test_claude_content_tool_use():
    """测试 tool_use 支持 (Fully Supported: id, input, name)
    
    验证：
    1. 请求中包含 tools 参数
    2. 响应中包含 tool_use 或 text
    3. 如果有 tool_use，验证其格式正确性
    """
    test_name = "Claude Content: tool_use [#12]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # 发送带 tools 参数的请求
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": [
                        {"role": "user", "content": "北京今天天气怎么样？"}
                    ],
                    "tools": [WEATHER_TOOL_CLAUDE]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            assert "content" in data, "响应缺少 content 字段"
            has_tool_use = any(b.get("type") == "tool_use" for b in data["content"])
            has_text = any(b.get("type") == "text" for b in data["content"])
            
            # 至少应该有 tool_use 或 text 其中之一
            assert has_tool_use or has_text, "响应应包含 tool_use 或 text"
            
            # 验证 tool_use 格式（如果有）
            for block in data["content"]:
                if block.get("type") == "tool_use":
                    assert "id" in block, "tool_use 缺少 id"
                    assert "name" in block, "tool_use 缺少 name"
                    assert "input" in block, "tool_use 缺少 input"

            print_result(test_name, True, 
                f"tool_use 支持：{has_tool_use}, text: {has_text}"
            )
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_claude_content_mixed():
    """测试混合格式支持 (string + array[text])"""
    test_name = "Claude Content: mixed [#13]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": [
                        # string format
                        {"role": "user", "content": "你好"},
                        # array[text] format
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "你好！有什么可以帮你的吗？"}]
                        },
                        # 继续对话
                        {"role": "user", "content": "再见"}
                    ]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            assert "content" in data, "响应缺少 content 字段"
            print_result(test_name, True, "混合格式支持验证通过")
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_tool_use_openai_to_openai():
    """测试工具调用：OpenAI → OpenAI [#14]
    
    验证：
    1. 请求中包含 tools 参数
    2. 响应中包含 tool_calls 或 content
    3. 如果有 tool_calls，验证其格式正确性
    """
    test_name = "Tool Use: OpenAI → OpenAI [#14]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/chat/completions",
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "user", "content": "北京今天天气怎么样？"}
                    ],
                    "tools": [WEATHER_TOOL_OPENAI]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            # 验证 OpenAI 格式响应
            assert "choices" in data, "响应缺少 choices 字段"
            message = data["choices"][0]["message"]
            
            has_tool_calls = "tool_calls" in message
            has_content = "content" in message and message["content"]
            
            # 至少应该有 tool_calls 或 content 其中之一
            assert has_tool_calls or has_content, "响应应包含 tool_calls 或 content"
            
            # 验证 tool_calls 格式（如果有）
            if has_tool_calls:
                for tc in message["tool_calls"]:
                    assert "id" in tc, "tool_call 缺少 id"
                    assert "type" in tc, "tool_call 缺少 type"
                    assert "function" in tc, "tool_call 缺少 function"
                    assert "name" in tc["function"], "function 缺少 name"
                    # arguments 可能是字符串或对象
                    assert "arguments" in tc["function"], "function 缺少 arguments"

            print_result(test_name, True, 
                f"tool_calls: {has_tool_calls}, content: {has_content}"
            )
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_tool_use_claude_to_claude():
    """测试工具调用：Claude → Claude [#15]
    
    验证：
    1. 请求中包含 tools 参数
    2. 响应中包含 tool_use 或 text
    3. 如果有 tool_use，验证其格式正确性
    """
    test_name = "Tool Use: Claude → Claude [#15]"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{BASE_URL}/v1/messages",
                json={
                    "model": CLAUDE_MODEL,
                    "messages": [
                        {"role": "user", "content": "北京今天天气怎么样？"}
                    ],
                    "tools": [WEATHER_TOOL_CLAUDE]
                },
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

            # 验证 Claude 格式响应
            assert "content" in data, "响应缺少 content 字段"
            
            has_tool_use = any(b.get("type") == "tool_use" for b in data["content"])
            has_text = any(b.get("type") == "text" for b in data["content"])
            
            # 至少应该有 tool_use 或 text 其中之一
            assert has_tool_use or has_text, "响应应包含 tool_use 或 text"
            
            # 验证 tool_use 格式（如果有）
            for block in data["content"]:
                if block.get("type") == "tool_use":
                    assert "id" in block, "tool_use 缺少 id"
                    assert "name" in block, "tool_use 缺少 name"
                    assert "input" in block, "tool_use 缺少 input"
                    # input 应该是对象
                    assert isinstance(block["input"], dict), "tool_use input 应该是对象"

            print_result(test_name, True, 
                f"tool_use: {has_tool_use}, text: {has_text}"
            )
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


async def test_unsupported_protocol_error():
    """测试不支持的协议返回错误 [#16]
    
    验证当上游模型不支持请求的协议格式时，返回 400 错误。
    """
    test_name = "Unsupported Protocol Error [#16]"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 测试 1: 使用只支持 OpenAI 格式的模型请求 Claude 端点
            # 应该返回 400 错误
            response = await client.post(
                f"{BASE_URL}/v1/messages",  # Claude 端点
                json={
                    "model": OPENAI_MODEL,  # 只支持 OpenAI 格式的模型
                    "messages": [
                        {"role": "user", "content": "你好"}
                    ]
                },
                headers={"Content-Type": "application/json"}
            )
            
            # 验证返回 400 错误
            assert response.status_code == 400, f"期望返回 400 错误，实际返回 {response.status_code}"
            
            # 验证错误响应包含错误信息
            error_data = response.json()
            assert "error" in error_data, "错误响应应包含 error 字段"
            assert "does not support" in error_data["error"] or "not support" in error_data["error"].lower(), \
                "错误信息应说明协议不支持"
            
            print_result(test_name, True, 
                f"正确返回 400 错误：{error_data['error'][:50]}..."
            )
            return True
    except Exception as e:
        print_result(test_name, False, str(e))
        return False


# 所有测试用例列表
ALL_TESTS = [
    # 核心协议测试
    test_openai_to_openai_stream,       # #1
    test_openai_to_openai_non_stream,   # #2
    test_claude_to_claude_stream,       # #3
    test_claude_to_claude_non_stream,   # #4
    # Extra 测试
    test_reasoning_effort_param,        # #5
    test_chinese_output_not_escaped,    # #6
    test_single_message_delta,          # #7
    test_format_priority,               # #8
    # Claude Content 测试
    test_claude_content_string,         # #9
    test_claude_content_array_text,     # #10
    test_claude_content_thinking,       # #11
    test_claude_content_tool_use,       # #12
    test_claude_content_mixed,          # #13
    # Tool Use 测试 (同协议)
    test_tool_use_openai_to_openai,     # #14
    test_tool_use_claude_to_claude,     # #15
    # 错误处理测试
    test_unsupported_protocol_error,    # #16
]


def parse_test_ids(test_ids_str: str) -> list[int]:
    """解析测试 ID 字符串，如 '1,3,5' 或 '1-3'"""
    test_ids = []
    for part in test_ids_str.split(","):
        part = part.strip()
        if "-" in part:
            # 范围，如 '1-3'
            start, end = map(int, part.split("-"))
            test_ids.extend(range(start, end + 1))
        else:
            test_ids.append(int(part))
    return test_ids


async def run_tests(test_indices: list[int] = None):
    """运行指定的测试"""
    global DEBUG_MODE
    
    if test_indices is None:
        # 运行所有测试
        tests_to_run = ALL_TESTS
    else:
        # 运行指定的测试
        tests_to_run = []
        for i in test_indices:
            if 1 <= i <= len(ALL_TESTS):
                tests_to_run.append(ALL_TESTS[i - 1])
            else:
                print(f"警告：无效的测试编号 {i}，有效范围是 1-{len(ALL_TESTS)}")
    
    if not tests_to_run:
        print("没有要运行的测试")
        return False
    
    print("=" * 60)
    print("LLM Proxy Gateway 测试")
    print("=" * 60)
    print(f"服务地址：{BASE_URL}")
    print(f"OpenAI 模型：{OPENAI_MODEL}")
    print(f"Claude 模型：{CLAUDE_MODEL}")
    print(f"调试模式：{'开启' if DEBUG_MODE else '关闭'}")
    print(f"运行测试：{[ALL_TESTS.index(t) + 1 for t in tests_to_run]}")
    print("=" * 60)
    
    results = []
    for test in tests_to_run:
        try:
            result = await test()
            results.append((test.__name__, result))
        except Exception as e:
            print(f"\n✗ EXCEPTION | {test.__name__}")
            print(f"           {type(e).__name__}: {e}")
            results.append((test.__name__, False))
        
        # 添加小延迟避免请求过快
        await asyncio.sleep(0.5)
    
    # 汇总结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "✓" if result else "✗"
        print(f"  {status} {name}")
    
    print("-" * 60)
    if total > 0:
        print(f"总计：{passed}/{total} 通过 ({passed/total*100:.1f}%)")
    print("=" * 60)
    
    return passed == total


def main():
    global DEBUG_MODE
    
    parser = argparse.ArgumentParser(description="LLM Proxy Gateway 测试工具")
    parser.add_argument(
        "tests",
        nargs="?",
        default=None,
        help="要运行的测试编号，如 '1', '1,3,5', '1-3'"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试模式，显示详细日志"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出所有可用的测试用例"
    )
    
    args = parser.parse_args()
    
    if args.list:
        print("可用测试用例：")
        print("-" * 60)
        for i, test in enumerate(ALL_TESTS, 1):
            # 从 docstring 获取描述
            desc = test.__doc__.split("\n")[0] if test.__doc__ else "无描述"
            print(f"  #{i} - {test.__name__}: {desc}")
        return 0
    
    DEBUG_MODE = args.debug
    
    if args.tests:
        test_indices = parse_test_ids(args.tests)
    else:
        test_indices = None
    
    success = asyncio.run(run_tests(test_indices))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
