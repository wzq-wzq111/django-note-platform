"""
AI 辅助功能 — 大模型调用封装

模型：DeepSeek（OpenAI 兼容接口）。密钥从环境变量读取（settings.LLM_API_KEY），
不进代码仓库。所有出错情况统一转成 LlmError，消息是给用户看的中文提示。
"""
from django.conf import settings
from openai import OpenAI

# 系统提示词：规定模型只输出正文，不带解释、前缀和客套话
SYSTEM_PROMPT = '你是中文笔记助手。只输出要求的 Markdown 正文，不要输出任何解释、前缀或客套话。'


class LlmError(Exception):
    """大模型调用失败（未配置密钥 / 超时 / 网络错误等）"""


def llm_chat(messages, temperature=0.3):
    """调用大模型对话接口，返回回复文本。

    messages: [{'role': 'system'|'user', 'content': str}, ...]
    失败时抛 LlmError，调用方捕获后转成友好提示返回给前端。
    """
    if not settings.LLM_API_KEY:
        raise LlmError('还没配置大模型 API 密钥，请设置环境变量 LLM_API_KEY')

    client = OpenAI(api_key=settings.LLM_API_KEY, base_url=settings.LLM_BASE_URL)
    try:
        # openai 库默认对网络类错误自动重试 2 次；timeout=30 覆盖默认值
        response = client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=messages,
            temperature=temperature,
            timeout=30,
        )
    except Exception as exc:
        raise LlmError('大模型调用失败，请稍后重试') from exc
    return response.choices[0].message.content
