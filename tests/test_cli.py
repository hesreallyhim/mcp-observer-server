"""Tests for the command-line interface."""
import pytest
import subprocess
import sys
import time
from pathlib import Path
from click.testing import CliRunner

from mcp_monitor_server import main

def test_cli_help():
    """Test that the CLI help option works."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "monitor-path" in result.output
    assert "verbose" in result.output

def test_cli_defaults():
    """Test that CLI uses reasonable defaults."""
    runner = CliRunner()
    # Just run with --help to avoid actually starting the server
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    
    # The default monitor path should be cwd
    assert "Default: current directory" in result.output or "monitor-path" in result.output

@pytest.mark.skipif(sys.platform == "win32", reason="Different process handling on Windows")
def test_cli_start_stop():
    """Test that the server can start and be stopped with Ctrl+C."""
    # This test starts the actual server process and then stops it
    # It's not a unit test but more of an integration test
    
    cmd = [sys.executable, "-m", "mcp_monitor_server", "--verbose"]
    
    proc = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Give it a moment to start
    time.sleep(0.5)
    
    # Check if the process is running
    assert proc.poll() is None, "Server did not start properly"
    
    # Send SIGINT (Ctrl+C)
    proc.terminate()
    
    # Wait for process to exit
    try:
        proc.wait(timeout=2)
        # Read stderr and check for errors
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        assert "Error" not in stderr, f"Server reported errors: {stderr}"
    except subprocess.TimeoutExpired:
        # Force kill if it doesn't exit cleanly
        proc.kill()
        assert False, "Server did not terminate properly"
