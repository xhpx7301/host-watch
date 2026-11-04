"""Configure imports for unittest discovery from the repository root."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "app", ROOT / "tests"):
    sys.path.insert(0, str(directory))
# Child-process checks exercise the source tree with the same module path.
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(ROOT / "app"), os.environ.get("PYTHONPATH", "")]
).rstrip(os.pathsep)
