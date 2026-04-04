"""LLM client module for communicating with Ollama API."""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "codellama"
DEFAULT_TIMEOUT = 120


class LLMClient:
    """Client for interacting with the Ollama LLM API."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.generate_url = f"{self.base_url}/api/generate"

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        """Send a prompt to Ollama and return the generated text.

        Args:
            prompt: The user prompt to send.
            system_prompt: Optional system-level instruction.

        Returns:
            The generated text response.

        Raises:
            ConnectionError: If the Ollama server is unreachable.
            RuntimeError: If the API returns an error.
        """
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt

        logger.debug("Sending request to Ollama: model=%s", self.model)
        start = time.monotonic()

        try:
            response = requests.post(
                self.generate_url,
                json=payload,
                timeout=self.timeout,
            )
        except requests.ConnectionError as exc:
            logger.error("Cannot reach Ollama at %s", self.base_url)
            raise ConnectionError(
                f"Cannot connect to Ollama at {self.base_url}. "
                "Ensure Ollama is running (ollama serve)."
            ) from exc
        except requests.Timeout as exc:
            logger.error("Ollama request timed out after %ds", self.timeout)
            raise RuntimeError(
                f"Ollama request timed out after {self.timeout}s."
            ) from exc

        elapsed = time.monotonic() - start
        logger.debug("Ollama responded in %.2fs", elapsed)

        if response.status_code != 200:
            error_detail = response.text[:500]
            logger.error("Ollama API error %d: %s", response.status_code, error_detail)
            raise RuntimeError(
                f"Ollama API returned status {response.status_code}: {error_detail}"
            )

        data = response.json()
        generated_text: str = data.get("response", "").strip()

        if not generated_text:
            logger.warning("Ollama returned an empty response")

        return generated_text

    def health_check(self) -> bool:
        """Check whether the Ollama server is reachable.

        Returns:
            True if the server responds, False otherwise.
        """
        try:
            response = requests.get(f"{self.base_url}/", timeout=5)
            return response.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    def list_models(self) -> Optional[list]:
        """List available models on the Ollama server.

        Returns:
            A list of model info dicts, or None on failure.
        """
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=10)
            if response.status_code == 200:
                return response.json().get("models", [])
        except (requests.ConnectionError, requests.Timeout):
            pass
        return None
