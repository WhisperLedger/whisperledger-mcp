"""Indexer configuration. All knobs in one place."""
from pathlib import Path

REPOS_DIR = Path.home() / "jarvis" / "repos"
LOGS_DIR = Path.home() / "jarvis" / "logs"

QDRANT_URL = "http://127.0.0.1:6333"
QDRANT_COLLECTION = "jarvis_code"
QDRANT_COLLECTION_PRS = "jarvis_prs"

# PR indexing
PR_LOOKBACK_DAYS = 180  # last 6 months
PR_BODY_MAX_CHARS = 6000  # truncate very long bodies

EMBED_MODEL = "voyage-code-3"
EMBED_DIM = 1024
# Voyage limits: 128 items/batch, 120k tokens/batch (their own tokenizer).
# tiktoken can under-count vs Voyage's tokenizer by 25-30% on some content,
# so leave a wide margin.
EMBED_BATCH_MAX_ITEMS = 128
EMBED_BATCH_MAX_TOKENS = 60_000
EMBED_INPUT_TYPE_DOC = "document"
EMBED_INPUT_TYPE_QUERY = "query"

# Chunking. Voyage-code-3 supports up to 32k tokens per chunk; we stay well under
# for retrieval quality (smaller chunks = sharper hits).
CHUNK_TARGET_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100
# Hard cap per chunk. Any chunk larger than this gets sub-split by tokens.
# Protects against single-line-blob files (minified JS, generated SQL/YAML).
CHUNK_MAX_TOKENS = 6000
MAX_FILE_BYTES = 1_500_000  # skip huge generated/vendored files

# Extensions worth embedding. Everything else is skipped at the walker.
CODE_EXTS = {
    "kt", "kts", "java", "scala", "sbt",
    "py",
    "ts", "tsx", "js", "jsx", "mjs", "cjs",
    "go", "rs", "rb", "php", "swift", "m", "mm", "c", "cc", "cpp", "h", "hpp",
    "sql",
    "tf", "hcl", "tfvars",
    "yaml", "yml", "json", "toml", "ini", "conf",
    "gradle", "gql", "graphql", "proto",
    "sh", "bash", "zsh",
    "html", "vue", "svelte", "hbs",
    "css", "scss", "sass", "less",
    "md", "mdx", "rst", "txt",
}

# Path segments to skip anywhere in the path.
SKIP_DIR_NAMES = {
    ".git", ".github/workflows-cache", ".gradle", ".idea", ".vscode",
    "node_modules", "bower_components",
    "build", "dist", "out", "target", ".next", ".nuxt", ".turbo", ".cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "vendor", "third_party", "third-party",
    "coverage", ".nyc_output",
    "android/build", "ios/build", "ios/Pods", "Pods",
    "generated", "gen", "_generated", "auto-generated",
    "openapi-generator-templates",
    "lotties",
}

# Filename suffixes to skip (lockfiles, minified, sourcemaps).
SKIP_FILENAME_PATTERNS = (
    ".min.js", ".min.css", ".bundle.js", ".bundle.css",
    ".map", ".lock", "-lock.json", "-lock.yaml",
    ".pb.go", ".pb.cc", ".pb.h", "_pb2.py", "_pb2_grpc.py",
)

EXACT_SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock",
    "go.sum", "gradle.lockfile",
    "alertmanager.yaml", "alertmanager.yml",
    ".npmrc",
}
