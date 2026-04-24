"""LLM client — optional integration with OpenAI-compatible or Ollama endpoints.

Supports two providers:
  * **openai** — Any OpenAI-compatible chat completions API (default).
  * **ollama** — Local Ollama instance (no API key required).

All public functions gracefully return a fallback message when the LLM is not
configured.  The rest of the application works without any LLM dependency.
"""

import json
from typing import Generator, Optional

import requests

import config

_NOT_CONFIGURED_MSG = (
    "LLM is not configured. Set the LLM provider and connection details in "
    "the sidebar LLM Settings panel, or via environment variables "
    "(LLM_PROVIDER, LLM_API_URL / OLLAMA_BASE_URL)."
)


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


def _build_messages(
    user_message: str,
    system_message: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
) -> list[dict]:
    """Assemble the messages list shared by both query and stream."""
    messages = []
    sys_msg = system_message or SYSTEM_PROMPT
    messages.append({"role": "system", "content": sys_msg})
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})
    return messages


def _build_headers() -> dict:
    """Return request headers for the active provider."""
    headers = {"Content-Type": "application/json"}
    if config.LLM_PROVIDER != "ollama" and config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    return headers


def _build_payload(
    messages: list[dict],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    stream: bool = False,
) -> dict:
    """Return the request payload for the active provider."""
    temp = temperature if temperature is not None else config.LLM_TEMPERATURE
    model = config.get_active_model()

    if config.LLM_PROVIDER == "ollama":
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": temp,
            },
        }
        if max_tokens is not None or config.LLM_MAX_TOKENS:
            payload["options"]["num_predict"] = (
                max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS
            )
        return payload

    # OpenAI-compatible
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS,
    }
    if stream:
        payload["stream"] = True
    return payload


def query_llm(
    user_message: str,
    system_message: Optional[str] = None,
    conversation_history: Optional[list[dict]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Send a query to the LLM and return the response text.

    Supports both OpenAI-compatible and Ollama endpoints.
    """
    if not config.is_llm_configured():
        return _NOT_CONFIGURED_MSG

    messages = _build_messages(user_message, system_message, conversation_history)
    headers = _build_headers()
    payload = _build_payload(messages, temperature, max_tokens, stream=False)
    url = config.get_active_llm_url()

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()

        # Ollama returns {"message": {"content": "..."}}
        if config.LLM_PROVIDER == "ollama":
            return data.get("message", {}).get("content", "")

        # OpenAI returns {"choices": [{"message": {"content": "..."}}]}
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        return "Error: LLM request timed out. Please try again."
    except requests.exceptions.ConnectionError:
        return (
            f"Error: Could not connect to the LLM endpoint at {url}. "
            "Please check your network and LLM configuration."
        )
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

    Supports both OpenAI-compatible and Ollama endpoints.
    Yields chunks of text as they arrive from the API.
    """
    if not config.is_llm_configured():
        yield _NOT_CONFIGURED_MSG
        return

    messages = _build_messages(user_message, system_message, conversation_history)
    headers = _build_headers()
    payload = _build_payload(messages, temperature, max_tokens, stream=True)
    url = config.get_active_llm_url()

    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=120, stream=True,
        )
        response.raise_for_status()

        if config.LLM_PROVIDER == "ollama":
            # Ollama streams newline-delimited JSON objects
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue
        else:
            # OpenAI SSE format: "data: {...}\n"
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


def list_ollama_models(base_url: str = "") -> list[str]:
    """Fetch available model names from an Ollama instance.

    Returns a list of model name strings, or an empty list on failure.
    """
    url = (base_url or config.OLLAMA_BASE_URL).rstrip("/") + "/api/tags"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []
