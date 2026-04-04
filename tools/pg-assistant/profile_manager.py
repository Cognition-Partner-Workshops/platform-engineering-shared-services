"""Database connection profile manager — save and load profiles as JSON."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_DIR = os.path.join(str(Path.home()), ".pg-assistant")
PROFILES_FILE = "profiles.json"


class ProfileManager:
    """Manages saved database connection profiles."""

    def __init__(self, profiles_dir: str = DEFAULT_PROFILES_DIR) -> None:
        self.profiles_dir = profiles_dir
        self.profiles_path = os.path.join(profiles_dir, PROFILES_FILE)
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Create the profiles directory if it doesn't exist."""
        os.makedirs(self.profiles_dir, exist_ok=True)

    def _load_all(self) -> dict[str, dict[str, Any]]:
        """Load all profiles from disk."""
        if not os.path.exists(self.profiles_path):
            return {}
        try:
            with open(self.profiles_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load profiles: %s", exc)
        return {}

    def _save_all(self, profiles: dict[str, dict[str, Any]]) -> None:
        """Save all profiles to disk."""
        try:
            with open(self.profiles_path, "w", encoding="utf-8") as f:
                json.dump(profiles, f, indent=2)
            logger.info("Profiles saved to %s", self.profiles_path)
        except OSError as exc:
            logger.error("Failed to save profiles: %s", exc)

    def list_profiles(self) -> list[str]:
        """Return a list of saved profile names."""
        return list(self._load_all().keys())

    def get_profile(self, name: str) -> Optional[dict[str, Any]]:
        """Retrieve a saved profile by name.

        Args:
            name: The profile name.

        Returns:
            A dict with connection parameters, or None if not found.
        """
        profiles = self._load_all()
        return profiles.get(name)

    def save_profile(
        self,
        name: str,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
    ) -> None:
        """Save a database connection profile.

        Args:
            name: A friendly name for the profile.
            host: PostgreSQL host.
            port: PostgreSQL port.
            database: Database name.
            user: Database user.
            password: Database password.
            sslmode: SSL mode (default: prefer).
        """
        profiles = self._load_all()
        profiles[name] = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
            "sslmode": sslmode,
        }
        self._save_all(profiles)
        logger.info("Profile '%s' saved", name)

    def delete_profile(self, name: str) -> bool:
        """Delete a saved profile.

        Args:
            name: The profile name to delete.

        Returns:
            True if deleted, False if not found.
        """
        profiles = self._load_all()
        if name in profiles:
            del profiles[name]
            self._save_all(profiles)
            logger.info("Profile '%s' deleted", name)
            return True
        return False
