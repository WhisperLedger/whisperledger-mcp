"""Parse common stacktrace formats into structured frames.

Handles:
- Kotlin/Java JVM: `at money.jupiter.foo.Bar.baz(Bar.kt:42)`
- JavaScript/Node V8: `at Foo.bar (/path/to/foo.js:12:34)`
- Python: `  File "/path/to/foo.py", line 42, in bar`
- Scala: similar to JVM

Returns list of frames in deepest-first order (the order they appear in
the trace), with (file, line, function, raw_line) per frame. The agent
loop can then call lookup_symbol or read_file on the most-relevant frame.
"""
from __future__ import annotations
import json
import re

# ─── per-language patterns ────────────────────────────────────────────────────

# JVM: "at money.jupiter.foo.Bar.baz(Bar.kt:42)"
# Optional "Bar$Companion" wrappers, optional "Unknown Source" / "Native Method"
_JVM_RE = re.compile(
    r"\s*at\s+(?P<qual>[\w.$<>]+)\.(?P<method>[\w$<>]+)"
    r"\((?P<file>[\w$.]+)(?::(?P<line>\d+))?\)",
)

# V8 (Node): "at Foo.bar (/abs/path/to/foo.js:12:34)" or "at /abs/path:12:34"
# Also: "at Object.<anonymous> (/path:5:9)"
_V8_RE = re.compile(
    r"\s*at\s+(?:(?P<qual>[\w.<>$]+(?:\.[\w<>$]+)*)\s+)?"
    r"\((?P<file>[^)]+?):(?P<line>\d+)(?::\d+)?\)",
)
# V8 bare: "at /path/to/foo.js:12:34" (no parens)
_V8_BARE_RE = re.compile(
    r"\s*at\s+(?P<file>[/\w.\-]+\.(?:js|ts|jsx|tsx|mjs)):(?P<line>\d+)(?::\d+)?",
)

# Python: '  File "/path/to/foo.py", line 42, in bar'
_PY_RE = re.compile(
    r'\s*File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+),\s+in\s+(?P<method>\S+)',
)


def parse(stacktrace: str, max_frames: int = 20) -> list[dict]:
    """Return [{file, line, function, language, raw_line}, ...] for each frame."""
    frames: list[dict] = []
    for raw in stacktrace.splitlines():
        if not raw.strip():
            continue

        m = _JVM_RE.match(raw)
        if m:
            qual = m.group("qual") or ""
            method = m.group("method") or ""
            file = m.group("file") or ""
            line = int(m.group("line")) if m.group("line") else None
            # Class name is the last segment of qual
            cls = qual.rsplit(".", 1)[-1].split("$", 1)[0] if qual else ""
            func = f"{cls}.{method}" if cls else method
            frames.append({
                "language": "jvm",
                "file": file,
                "line": line,
                "function": func,
                "qualified_name": f"{qual}.{method}" if qual else method,
                "raw_line": raw.strip()[:300],
            })
            continue

        m = _V8_RE.match(raw)
        if m:
            frames.append({
                "language": "javascript",
                "file": m.group("file"),
                "line": int(m.group("line")),
                "function": m.group("qual") or "<anonymous>",
                "qualified_name": m.group("qual") or "",
                "raw_line": raw.strip()[:300],
            })
            continue

        m = _V8_BARE_RE.match(raw)
        if m:
            frames.append({
                "language": "javascript",
                "file": m.group("file"),
                "line": int(m.group("line")),
                "function": "<anonymous>",
                "qualified_name": "",
                "raw_line": raw.strip()[:300],
            })
            continue

        m = _PY_RE.match(raw)
        if m:
            frames.append({
                "language": "python",
                "file": m.group("file"),
                "line": int(m.group("line")),
                "function": m.group("method"),
                "qualified_name": m.group("method"),
                "raw_line": raw.strip()[:300],
            })
            continue

        if len(frames) >= max_frames:
            break
    return frames


def parse_stacktrace(stacktrace: str) -> str:
    """Agent tool entry point — returns JSON string."""
    frames = parse(stacktrace, max_frames=30)
    return json.dumps({
        "frame_count": len(frames),
        "frames": frames,
        "hint": (
            "Frames are listed deepest-first (top of trace). The top frame is "
            "USUALLY the actual failure site; the rest is the caller chain. "
            "Use lookup_symbol or read_file on the top 1-2 frames to investigate. "
            "Then optionally call why_was_this_changed on the same file:line to "
            "see if a recent commit caused this."
        ),
    }, ensure_ascii=False)
