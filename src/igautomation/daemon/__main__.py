"""Entry-point for ``python -m igautomation.daemon``.

Usage::

    python -m igautomation.daemon                       # foreground, default config
    python -m igautomation.daemon --config daemon.yaml  # custom config
    python -m igautomation.daemon --db ig.db            # override DB path
"""

from __future__ import annotations

import argparse
import logging

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
    # Suppress noisy per-query logging from aiosqlite and HTTP client
    for noisy in ("aiosqlite", "urllib3.connectionpool", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = DaemonConfig.from_yaml(args.config) if args.config else DaemonConfig()
    if args.db:
        cfg = cfg.model_copy(update={"db_path": args.db})

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
