"""Split a file's text into overlapping chunks sized in tokens.

Default: line-aware token-budgeted splitter.
OpenAPI YAML files: split each top-level path block as its own chunk so
semantic search lands on the specific endpoint definition (rather than a
random line-range slice). Established 2026-05-28 after Aura's
/rew/v1/jewels-history-for-instrument query hit max iterations — the
endpoint was at line 4477 of api.yaml but the indexed chunk boundary
landed at line 4057, so semantic search never surfaced the path.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
import tiktoken
from . import config

_enc = tiktoken.get_encoding("cl100k_base")

# OpenAPI detection: file has top-level `openapi: ` or `swagger: ` declaration
# AND a `paths:` section. Both required to avoid false-positive on arbitrary YAML.
_OPENAPI_DETECT = re.compile(r"^(?:openapi|swagger)\s*:\s*['\"]?\d", re.M)
# A path entry under `paths:` looks like `  /users/{id}:` at indent 2
_OPENAPI_PATH_LINE = re.compile(r"^  (/[^:\s]+)\s*:\s*$")


@dataclass
class Chunk:
    text: str
    start_line: int  # 1-indexed
    end_line: int
    symbols: list = None  # populated by ast_chunker; None means "no AST info"


def _tok(s: str) -> int:
    return len(_enc.encode(s, disallowed_special=()))


def _refine(chunks: list[Chunk]) -> list[Chunk]:
    """Sub-split any chunk that exceeds CHUNK_MAX_TOKENS by raw token slices.

    Protects against pathological single-line-blob files (minified JS, generated
    SQL, huge YAML values) that would otherwise blow Voyage's per-batch limit.
    """
    cap = config.CHUNK_MAX_TOKENS
    out: list[Chunk] = []
    for ch in chunks:
        toks = _enc.encode(ch.text, disallowed_special=())
        if len(toks) <= cap:
            out.append(ch)
            continue
        for s in range(0, len(toks), cap):
            sub_toks = toks[s:s + cap]
            sub_text = _enc.decode(sub_toks)
            out.append(Chunk(text=sub_text, start_line=ch.start_line,
                             end_line=ch.end_line, symbols=ch.symbols))
    return out


def _is_openapi(text: str) -> bool:
    if not _OPENAPI_DETECT.search(text):
        return False
    # `paths:` must appear as a top-level key (no leading whitespace).
    return bool(re.search(r"^paths\s*:\s*$", text, re.M))


def _chunk_openapi(text: str) -> list[Chunk]:
    """Split an OpenAPI YAML file into per-path chunks. Returns chunks
    covering: (a) header before `paths:`, (b) one chunk per top-level path
    under `paths:`, (c) everything after the `paths:` section line-chunked
    via the default splitter.
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)

    # Locate `paths:` and the next top-level key (which ends the paths section).
    paths_start = None
    paths_end = n  # default to EOF if no following top-level key
    for i, line in enumerate(lines):
        if re.match(r"^paths\s*:\s*$", line):
            paths_start = i
            # Find the next non-indented non-blank line — that ends the paths section
            for j in range(i + 1, n):
                stripped = lines[j].rstrip("\n")
                if stripped and not stripped[0].isspace():
                    paths_end = j
                    break
            break

    if paths_start is None:
        # Shouldn't happen since _is_openapi confirmed `paths:` exists, but be safe
        return _chunk_default(text)

    chunks: list[Chunk] = []

    # (a) Header chunk: lines before `paths:` (inclusive of empty lines so
    # the chunk text shows context like openapi:/info:/servers: etc.)
    if paths_start > 0:
        header_text = "".join(lines[:paths_start])
        chunks.append(Chunk(text=header_text, start_line=1, end_line=paths_start))

    # (b) Per-path chunks: find every `  /...:` line under paths:
    path_starts: list[tuple[int, str]] = []  # (line index, path name)
    for k in range(paths_start + 1, paths_end):
        m = _OPENAPI_PATH_LINE.match(lines[k])
        if m:
            path_starts.append((k, m.group(1)))

    for idx, (start, path_name) in enumerate(path_starts):
        end = path_starts[idx + 1][0] if idx + 1 < len(path_starts) else paths_end
        # Prepend `paths:` header so the chunk is self-describing.
        chunk_body = "paths:\n" + "".join(lines[start:end])
        chunks.append(Chunk(text=chunk_body, start_line=start + 1, end_line=end))

    # (c) Tail (components/schemas/etc.): default-chunk it
    if paths_end < n:
        tail_text = "".join(lines[paths_end:])
        tail_chunks = _chunk_default(tail_text)
        for tc in tail_chunks:
            chunks.append(Chunk(
                text=tc.text,
                start_line=tc.start_line + paths_end,
                end_line=tc.end_line + paths_end,
            ))

    return _refine(chunks)


def _chunk_default(text: str) -> list[Chunk]:
    """Original line-aware token-budgeted chunker."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    target = config.CHUNK_TARGET_TOKENS
    overlap = config.CHUNK_OVERLAP_TOKENS

    line_tokens = [_tok(line) for line in lines]

    chunks: list[Chunk] = []
    i = 0
    n = len(lines)
    while i < n:
        cur_tokens = 0
        j = i
        while j < n and cur_tokens + line_tokens[j] <= target:
            cur_tokens += line_tokens[j]
            j += 1
        # Make sure we always advance, even if a single line is huge.
        if j == i:
            j = i + 1
        chunk_text_str = "".join(lines[i:j])
        chunks.append(Chunk(text=chunk_text_str, start_line=i + 1, end_line=j))
        if j >= n:
            break
        # back off by overlap tokens
        back_tokens = 0
        k = j
        while k > i + 1 and back_tokens < overlap:
            k -= 1
            back_tokens += line_tokens[k]
        i = max(k, i + 1)
    return _refine(chunks)


def chunk_text(text: str, ext: str | None = None) -> list[Chunk]:
    """Dispatch: OpenAPI YAML → per-path chunker. Kotlin/TS/TSX/JS → AST
    chunker (function/class boundaries). Everything else → default line-window.
    AST failures fall back to default so a grammar bug never blocks indexing.
    """
    if _is_openapi(text):
        return _chunk_openapi(text)
    if ext:
        from . import ast_chunker
        lang = ast_chunker.ast_supported(ext)
        if lang:
            try:
                return _refine(ast_chunker.ast_chunk(text, lang))
            except Exception as e:
                # AST grammar failure on a specific file — log + fall through.
                print(f"[chunker] AST fallback for ext={ext}: {type(e).__name__}: {e!s}",
                      flush=True)
    return _chunk_default(text)
