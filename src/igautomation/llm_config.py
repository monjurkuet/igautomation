"""Centralized LLM configuration — single source of truth.

All modules that need LLM credentials (daemon, analysis, content analysis)
import from here instead of reimplementing env-var / .env scanning.

Usage::

    from igautomation.llm_config import load_llm_config
    cfg = load_llm_config()
    # cfg.api_key, cfg.base_url, cfg.model
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LLMConfig:
    """LLM connection parameters."""

    api_key: str = ""
    base_url: str = "https://llm.datasolved.org/v1"
    model: str = "gemini-2.5-flash-lite"

    def __post_init__(self) -> None:
        if self.base_url and not self.base_url.endswith("/"):
            self.base_url += "/"


_config: LLMConfig | None = None


def load_llm_config(
    *,
    force_reload: bool = False,
    project_root: Path | str | None = None,
) -> LLMConfig:
    """Load LLM configuration from environment and .env file.

    Sources (in order of precedence):
    1. Environment variables: OPENPAI_API_KEY, OPENPAI_BASE_URL, LLM_MODEL
    2. .env file in project root directory

    Args:
        force_reload: Re-scan environment even if already cached.
        project_root: Override the project root directory for .env lookup.

    Returns:
        LLMConfig with the resolved credentials.
    """
    global _config

    if _config is not None and not force_reload:
        return _config

    api_key = os.environ.get("OPENPAI_API_KEY", "")
    base_url = os.environ.get("OPENPAI_BASE_URL", "")
    model = os.environ.get("LLM_MODEL", "")

    if not api_key or not base_url:
        _scan_dotenv_for_llm(
            project_root=project_root,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        api_key = os.environ.get("OPENPAI_API_KEY", "") or api_key
        base_url = os.environ.get("OPENPAI_BASE_URL", "") or base_url
        model = os.environ.get("LLM_MODEL", "") or model

    _config = LLMConfig(
        api_key=api_key,
        base_url=base_url or "https://llm.datasolved.org/v1",
        model=model or "gemini-2.5-flash-lite",
    )
    return _config


def _scan_dotenv_for_llm(
    *,
    project_root: Path | str | None = None,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> None:
    """Read .env file and load LLM vars into os.environ (low-precedence)."""
    if project_root is None:
        _guess = Path.cwd()
    else:
        _guess = Path(project_root)

    env_path = _guess / ".env"
    if not env_path.exists():
        env_path = Path(__file__).resolve().parent.parent.parent / ".env"

    if not env_path.exists():
        return

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")

            if k == "OPENPAI_API_KEY" and not api_key and k not in os.environ:
                os.environ["OPENPAI_API_KEY"] = v
            elif k == "OPENPAI_BASE_URL" and not base_url and k not in os.environ:
                os.environ["OPENPAI_BASE_URL"] = v
            elif k == "LLM_MODEL" and not model and k not in os.environ:
                os.environ["LLM_MODEL"] = v
    except OSError:
        pass