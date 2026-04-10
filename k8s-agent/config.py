"""Configuration for the K8s Agent application."""

import os
import shutil
import subprocess


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


# ── kubectl / helm path detection ─────────────────────────────────────────

# Common install locations to check when kubectl/helm are not in PATH
_KUBECTL_SEARCH_PATHS = [
    "/usr/local/bin/kubectl",
    "/usr/bin/kubectl",
    "/snap/bin/kubectl",
    os.path.expanduser("~/.local/bin/kubectl"),
    os.path.expanduser("~/bin/kubectl"),
    "/opt/bin/kubectl",
]

_HELM_SEARCH_PATHS = [
    "/usr/local/bin/helm",
    "/usr/bin/helm",
    "/snap/bin/helm",
    os.path.expanduser("~/.local/bin/helm"),
    os.path.expanduser("~/bin/helm"),
    "/opt/bin/helm",
]


def _find_binary(name: str, search_paths: list[str]) -> str:
    """Find a binary by name, checking PATH first then common locations.

    Strategy:
      1. ``shutil.which`` — honours $PATH as seen by the Python process.
      2. Probe well-known install directories with ``os.path.isfile``.
         (Skip the ``os.access`` X_OK check because some SELinux / mount
         configurations report False even though the file *is* executable.)
      3. Last resort: ask the OS via ``/usr/bin/which`` in a subprocess,
         which may see a different PATH than the Python process (e.g. when
         Streamlit is started through systemd or a virtualenv wrapper).
    """
    # 1. shutil.which
    found = shutil.which(name)
    if found:
        return found
    # 2. well-known paths — only check existence (skip os.access)
    for path in search_paths:
        if os.path.isfile(path):
            return path
    # 3. subprocess fallback — works when shell PATH differs from Python PATH
    for which_cmd in ("which", "/usr/bin/which", "/bin/which"):
        try:
            proc = subprocess.run(
                f"{which_cmd} {name}",
                shell=True, capture_output=True, text=True, timeout=5,
            )
            result = proc.stdout.strip()
            if proc.returncode == 0 and result and os.path.isfile(result):
                return result
        except Exception:
            continue
    return ""


def get_kubectl_path() -> str:
    """Return the full path to kubectl, or empty string if not found."""
    return _find_binary("kubectl", _KUBECTL_SEARCH_PATHS)


def get_helm_path() -> str:
    """Return the full path to helm, or empty string if not found."""
    return _find_binary("helm", _HELM_SEARCH_PATHS)


def get_kubeconfig_path(profile_name: str = "_temp") -> str:
    """Return the path where a kubeconfig file should be written for local commands."""
    kc_dir = os.path.join(DATA_DIR, "kubeconfigs")
    os.makedirs(kc_dir, exist_ok=True)
    return os.path.join(kc_dir, f"{profile_name}.kubeconfig")


def fetch_namespaces(kubeconfig_content: str) -> list[str]:
    """Fetch all namespaces from a cluster using kubectl with the given kubeconfig.

    Returns a list of namespace names, or an empty list on failure.
    """
    kubectl = get_kubectl_path()
    if not kubectl:
        return []
    kc_path = get_kubeconfig_path("_ns_fetch")
    os.makedirs(os.path.dirname(kc_path), exist_ok=True)
    with open(kc_path, "w") as f:
        f.write(kubeconfig_content)
    try:
        proc = subprocess.run(
            f"{kubectl} --kubeconfig=\"{kc_path}\" get namespaces -o jsonpath='{{.items[*].metadata.name}}'",
            shell=True, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return sorted(proc.stdout.strip().split())
        return []
    except Exception:
        return []
