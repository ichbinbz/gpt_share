import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CWS_CONFIG_DIR", str(PROJECT_ROOT / "backend" / "config_templates"))
