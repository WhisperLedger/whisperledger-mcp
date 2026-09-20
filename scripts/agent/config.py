import os
from pathlib import Path

def get_env(name: str, default: str = "") -> str:
    """Get environment variable with ASTRA_ prefix, falling back to JARVIS_ prefix or bare name."""
    val = os.environ.get(f"ASTRA_{name}")
    if val is not None:
        return val
    val = os.environ.get(f"JARVIS_{name}")
    if val is not None:
        return val
    return os.environ.get(name, default)

def get_env_bool(name: str, default: bool = False) -> bool:
    val = get_env(name, "")
    if not val:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

# Bot settings
BOT_NAME = get_env("BOT_NAME", "Astra")
SLASH_COMMAND = get_env("SLASH_COMMAND", "/astra")

# Project info
GITHUB_ORG = get_env("GITHUB_ORG", "jupitermoney")
COMPANY_NAME = get_env("COMPANY_NAME", "Jupiter")
COMPANY_DOMAIN = get_env("COMPANY_DOMAIN", "jupiter.money")

# Root directory of the checked out bot codebase
ROOT_DIR = Path(get_env("ROOT", str(Path(__file__).resolve().parent.parent.parent)))

def get_env_file_path() -> Path:
    """Finds the path to the environment file, defaulting to ~/.config/astra/env or ~/.config/jarvis/env."""
    home = Path.home()
    astra_path = home / ".config" / "astra" / "env"
    if astra_path.exists():
        return astra_path
    return home / ".config" / "jarvis" / "env"
