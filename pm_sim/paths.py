from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "current.db"
DEFAULT_SCENARIO_PATH = REPO_ROOT / "scenarios" / "launch_readiness.json"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
