"""Walk a repo and yield (relative_path, text, content_hash) for files worth indexing."""
from __future__ import annotations
from pathlib import Path
from typing import Iterator
from . import config, hashes


def _should_skip_dir(dir_path: Path, repo_root: Path) -> bool:
    rel_parts = dir_path.relative_to(repo_root).parts
    return any(part in config.SKIP_DIR_NAMES for part in rel_parts)


_SECRET_FILE_PATTERNS = (
    ".tfvars",        # terraform per-env values often have raw secrets
    ".pem", ".key",   # private keys
    ".p12", ".jks",   # keystore formats
)
_SECRET_NAME_FRAGMENTS = (
    "secrets.yaml", "secrets.yml", "secret.yaml", "secret.yml",
    "credentials", ".env",
    "id_rsa", "id_dsa", "id_ed25519",
)


def _should_skip_file(p: Path) -> bool:
    name = p.name
    name_lower = name.lower()
    # Belt-and-braces secret-file skip — never index these even if gitleaks says clean
    if any(name_lower.endswith(suf) for suf in _SECRET_FILE_PATTERNS):
        return True
    if any(frag in name_lower for frag in _SECRET_NAME_FRAGMENTS):
        return True
    if name in config.EXACT_SKIP_FILES:
        return True
    if any(name.endswith(suf) for suf in config.SKIP_FILENAME_PATTERNS):
        return True
    ext = p.suffix.lstrip(".").lower()
    if ext not in config.CODE_EXTS:
        return True
    try:
        if p.stat().st_size > config.MAX_FILE_BYTES:
            return True
    except OSError:
        return True
    return False


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def walk_repo(repo_root: Path) -> Iterator[tuple[str, str, str]]:
    """Yield (relative_path, text, content_hash) for each indexable file."""
    repo_root = repo_root.resolve()
    for path in repo_root.rglob("*"):
        if path.is_dir():
            if _should_skip_dir(path, repo_root):
                # rglob doesn't let us prune; we just keep skipping descendants by check
                continue
            continue
        if not path.is_file():
            continue
        # parent dir skip check
        if any(part in config.SKIP_DIR_NAMES for part in path.relative_to(repo_root).parts[:-1]):
            continue
        if _should_skip_file(path):
            continue
        text = _read_text(path)
        if text is None or not text.strip():
            continue
        yield str(path.relative_to(repo_root)), text, hashes.content_hash(text)
