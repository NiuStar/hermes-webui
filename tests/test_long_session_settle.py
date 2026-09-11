"""Execute real frontend refresh and completion adoption seams in Node."""
import subprocess
from pathlib import Path


def test_long_session_settle():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["node", "tests/long_session_settle.cjs"], cwd=root, check=True)
