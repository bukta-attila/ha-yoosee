import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "yoosee_cloud"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEV_ID = 10000001
