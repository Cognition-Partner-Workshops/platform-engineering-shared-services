"""LLM client for the Infosys AI Gateway."""

import json
from typing import Generator, Optional

import requests

import config


SYSTEM_PROMPT = """You are an expert Kubernetes platform engineer specializing in on-premises
cluster administration. You have deep knowledge of:
- Kubernetes cluster setup with CRI-O container runtime and Flannel CNI
- kubeadm-based cluster bootstrapping and lifecycle management
- Cluster debugging, troubleshooting, and remediation
- Prometheus and Grafana monitoring stack setup and dashboard design
- Kubernetes log analysis, error correlation, and root cause analysis
- Security best practices including RBAC, network policies, and pod security standards

Always provide actionable, production-ready advice. When generating scripts, include
error handling and idempotency. When diagnosing issues, ask clarifying questions if
the provided information is insufficient."""


def query_llm(
    user_message: str,
    system_message: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Send a query to the LLM and return the response text.

    Args:
        user_message: The user's message/query.
        system_message: Optional system prompt override.
        conversation_history: Optional list of prior messages for context.
        temperature: Optional temperature override.
        max_tokens: Optional max tokens override.

    Returns:
        The assistant's response text.
    """
    messages = []

    sys_msg = system_message or SYSTEM_PROMPT
    messages.append({"role": "system", "content": sys_msg})

    if conversation_history:
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.LLM_API_KEY}",
    }

    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature if temperature is not None else config.LLM_TEMPERATURE,
        "max_tokens": max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS,
    }

    try:
        response = requests.post(
            config.LLM_API_URL,
            headers=headers,
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "Error: LLM request timed out. Please try again."
    except requests.exceptions.ConnectionError:
        return "Error: Could not connect to the LLM endpoint. Please check your network and LLM_API_URL configuration."
    except requests.exceptions.HTTPError as exc:
        return f"Error: LLM API returned HTTP {exc.response.status_code}: {exc.response.text}"
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        return f"Error: Unexpected LLM response format: {exc}"


def stream_llm(
    user_message: str,
    system_message: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Generator[str, None, None]:
    """Stream a response from the LLM token-by-token.

    Yields chunks of text as they arrive from the API.
    """
    messages = []

    sys_msg = system_message or SYSTEM_PROMPT
    messages.append({"role": "system", "content": sys_msg})

    if conversation_history:
        messages.extend(conversation_history)

    messages.append({"role": "user", "content": user_message})

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.LLM_API_KEY}",
    }

    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature if temperature is not None else config.LLM_TEMPERATURE,
        "max_tokens": max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS,
        "stream": True,
    }

    try:
        response = requests.post(
            config.LLM_API_URL,
            headers=headers,
            json=payload,
            timeout=120,
            stream=True,
        )
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[len("data: "):]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
    except requests.exceptions.RequestException as exc:
        yield f"\n\nError during streaming: {exc}"
