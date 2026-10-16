"""`python -m tinyprint_bridge`: load config, refuse to start unless it is safe, serve."""

import argparse
import asyncio
import logging
import os
import sys

from . import __version__
from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tinyprint-bridge", description=__doc__)
    parser.add_argument("--bind", help="IP address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="TCP port (default 8765)")
    parser.add_argument("--version", action="version", version=f"tinyprint-bridge {__version__}")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load_config(os.environ, bind=args.bind, port=args.port)
    except ConfigError as error:
        print(f"tinyprint-bridge: configuration error: {error}", file=sys.stderr)
        return 2
    from .server import serve

    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
