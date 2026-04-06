"""Configuration for the K8s Agent application."""

import os

# LLM Configuration
LLM_API_URL = os.getenv(
    "LLM_API_URL",
    "https://aigateway-intern.ad.infosys.com/aigateway/chat/completions",
)
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("INFOSYS_CODER_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))


def is_llm_configured() -> bool:
    """Return True if the LLM endpoint and API key are both set."""
    return bool(LLM_API_URL and LLM_API_KEY)


# Application paths
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")

# Ensure directories exist
os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
