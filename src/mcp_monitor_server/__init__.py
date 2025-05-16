"""
MCP Monitor Server - A file system monitoring server using Model Context Protocol
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

# from typing import Optional
import click

from mcp_monitor_server.server import run

__version__ = "0.1.0"


@click.command()
@click.option(
    "--monitor-path",
    "-m",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    help="Path to monitor for changes",
)
@click.option("-v", "--verbose", count=True, help="Increase logging verbosity")
def main(monitor_path: Optional[Path] = None, verbose: int = 0):
    """Start the MCP Monitor Server to track file changes and notify clients.
    
    Args:
        monitor_path (Path): The path to monitor for changes. Defaults to the current working directory.
        verbose (int): Verbosity level for logging. Use -v for INFO, -vv for DEBUG.
    """
    # Configure logging based on verbosity level
    log_level = logging.WARNING
    if verbose == 1:
        log_level = logging.INFO
    elif verbose > 1:
        log_level = logging.DEBUG

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )

    if not monitor_path:
        monitor_path = Path.cwd()
        logging.info(f"No path specified, monitoring current directory: {monitor_path}")

    # Run the server
    try:
        # Convert Path object to string if it's a Path
        path_str = str(monitor_path) if monitor_path else None
        asyncio.run(run(path_str))
    except KeyboardInterrupt:
        logging.info("Server stopped by user")
    except Exception as e:
        logging.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
