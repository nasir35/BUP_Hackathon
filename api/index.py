import sys
from pathlib import Path

# Add project root and gridwise directory to Python path
root_dir = Path(__file__).resolve().parent.parent
gridwise_dir = root_dir / "gridwise"
sys.path.insert(0, str(root_dir))
sys.path.insert(0, str(gridwise_dir))

from app import app
