"""Entry-point for ``python -m igautomation.daemon``.

Usage::

    python -m igautomation.daemon                       # foreground, default config
    python -m igautomation.daemon --config daemon.yaml  # custom config
    python -m igautomation.daemon --db ig.db            # override DB path
"""

from __future__ import annotations

import argparse
import logging
import sys

from igautomation.daemon.loop import DaemonLoop
from igautomation.daemon.strategies import DaemonConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="igautomation.daemon",
        description="IG intelligence daemon — runs organic sessions continuously",
    )
    parser.add_argument(
        "--config", "-c", default="", help="YAML config file path"
    )
    parser.add_argument(
        "--db", default="igautomation.db", help="Database path (default: igautomation.db)"
    )
    parser.add_argument(
        "--verbose", "-V", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = DaemonConfig.from_yaml(args.config) if args.config else DaemonConfig()
    if args.db and args.db != "igautomation.db":
        # CLI override takes precedence
        cfg = cfg.model_copy(update={"db_path": args.db})

    # Auto-load LLM API key from environment if not set in config
    if not cfg.llm_api_key:
        import os
        key = os.environ.get("OPENPAI_API_KEY", "")
        base_url = os.environ.get("OPENPAI_BASE_URL", "")
        if not key:
            # Fallback: scan .env file
            from pathlib import Path
            env_path = Path(cfg.db_path).parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k == "OPENPAI_API_KEY":
                        key = v
                    elif k == "OPENPAI_BASE_URL":
                        base_url = v
        updates = {}
        if key:
            updates["llm_api_key"] = key
        if base_url:
            updates["llm_base_url"] = base_url
        if updates:
            cfg = cfg.model_copy(update=updates)
            logging.getLogger(__name__).info("LLM config loaded from environment")

    daemon = DaemonLoop(cfg)

    logging.getLogger(__name__).info(
        "Starting daemon — db=%s, llm=%s, model=%s",
        cfg.db_path, cfg.llm_enabled, cfg.llm_model,
    )
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Daemon stopped by user")


if __name__ == "__main__":
    main()
