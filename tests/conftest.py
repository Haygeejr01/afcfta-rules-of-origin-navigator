import sys
from pathlib import Path

# Put ``src`` on the path so tests import the modules the same way the CLI does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
