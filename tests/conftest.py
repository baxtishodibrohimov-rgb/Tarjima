import os
import sys
import tempfile
from pathlib import Path

_STORAGE = tempfile.mkdtemp(prefix="darslik-test-")
os.environ["STORAGE_DIR"] = _STORAGE
os.environ.setdefault("APP_USERNAME", "admin")
os.environ.setdefault("APP_PASSWORD", "test-password-123")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
