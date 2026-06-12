"""
llm_client.py — OpenRouter client.
Defaults to deepseek (best JSON compliance) for orchestrator,
qwen3-coder (best code output) for agents.
Override via .env: ORCHESTRATOR_MODEL, AGENT_MODEL
"""
import os
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()

ORCHESTRATOR_MODEL_DEFAULT = "openrouter/free"
AGENT_MODEL_DEFAULT        = "openrouter/free"

def get_orchestrator_llm() -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set. Get a free key at https://openrouter.ai")
    return ChatOpenAI(
        model=os.getenv("ORCHESTRATOR_MODEL", ORCHESTRATOR_MODEL_DEFAULT),
        openai_api_key=api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        temperature=0.0,
        max_tokens=400,
        default_headers={
            "HTTP-Referer": "https://github.com/agentic-platform",
            "X-Title": "Agentic SDLC Platform",
        },
    )

def get_agent_llm(temperature: float = 0.3) -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set.")
    return ChatOpenAI(
        model=os.getenv("AGENT_MODEL", AGENT_MODEL_DEFAULT),
        openai_api_key=api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        temperature=temperature,
        max_tokens=8192,
        default_headers={
            "HTTP-Referer": "https://github.com/agentic-platform",
            "X-Title": "Agentic SDLC Platform",
        },
    )
