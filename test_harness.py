"""Root runner for GridWise test harness."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent
gridwise_dir = root_dir / "gridwise"
sys.path.insert(0, str(gridwise_dir))

harness_path = gridwise_dir / "test_harness.py"
with open(harness_path, "r", encoding="utf-8") as f:
    code = f.read()

globs = {"__file__": str(harness_path), "__name__": "__main__"}
exec(compile(code, str(harness_path), "exec"), globs)
