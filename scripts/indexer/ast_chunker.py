"""AST-aware chunker for Kotlin + TypeScript / TSX / JavaScript.

Walks the tree-sitter parse tree, packs sibling top-level declarations into
chunks ≤ CHUNK_TARGET_TOKENS, recurses one level into oversized containers
(class → methods), and falls back to the line-window chunker for any leaf
that still exceeds CHUNK_MAX_TOKENS. Preserves 1-indexed start_line/end_line
for citation accuracy.

Lazy-loaded — tree_sitter and grammars are only imported when first needed.
Languages without an AST chunker fall through to the default line-window
chunker in chunker.py.
"""
from __future__ import annotations

from typing import Iterator
from . import config

# Lazy parser cache. Loaded on first call.
_PARSERS: dict[str, object] = {}


def _get_parser(lang: str):
    if lang in _PARSERS:
        return _PARSERS[lang]
    import tree_sitter
    if lang == "kotlin":
        import tree_sitter_kotlin
        l = tree_sitter.Language(tree_sitter_kotlin.language())
    elif lang == "typescript":
        import tree_sitter_typescript
        l = tree_sitter.Language(tree_sitter_typescript.language_typescript())
    elif lang == "tsx":
        import tree_sitter_typescript
        l = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    else:
        raise ValueError(f"no AST parser for {lang}")
    _PARSERS[lang] = tree_sitter.Parser(l)
    return _PARSERS[lang]


# Node types that represent "containers we can recurse into" — a single one
# may be huge, but its children (methods/fields) are usually right-sized.
_CONTAINER_TYPES = {
    "class_declaration",
    "class_body",
    "object_declaration",
    "object_body",
    "interface_declaration",
    "interface_body",
    "enum_class",
    "enum_class_body",
    "companion_object",
    "namespace_declaration",
    "module_declaration",
    "internal_module",
    "ambient_declaration",
    "export_statement",     # `export class Foo { ... }` — unwrap to find Foo
    "abstract_class_declaration",
}

# Node types we treat as "useful top-level declarations" — emit them as chunks.
_DECL_TYPES = {
    # Kotlin
    "function_declaration",
    "property_declaration",
    "secondary_constructor",
    "type_alias",
    "anonymous_initializer",
    # TS/JS
    "lexical_declaration",
    "variable_statement",
    "method_definition",
    "public_field_definition",
    "abstract_method_signature",
    "function_signature",
    "type_alias_declaration",
    "enum_declaration",
    # Shared containers (when small enough we emit them whole)
    "class_declaration",
    "interface_declaration",
    "object_declaration",
    "namespace_declaration",
    "module_declaration",
    "abstract_class_declaration",
    "internal_module",
}

# Noise nodes we just pass over. Imports + package headers are PACKED, not
# skipped, so the first chunk carries real lexical context.
_SKIP_TYPES = {
    "shebang",
    ";",
    "{",
    "}",
}

# Node types we pack into chunks but DON'T extract symbol names from. Imports
# and package headers contain dozens of identifiers (every imported name)
# that aren't useful as `lookup_symbol` targets — they'd dilute the index.
_NO_SYMBOL_TYPES = {
    "import",
    "import_statement",
    "import_alias",
    "import_clause",
    "import_specifier",
    "named_imports",
    "package_header",
    "comment",
    "line_comment",
    "block_comment",
}


def _tok_count(s: str) -> int:
    # Use the tiktoken from the default chunker for consistency.
    from .chunker import _tok
    return _tok(s)


def _node_text(text: str, node) -> str:
    return text[node.start_byte:node.end_byte]


def _node_lines(node) -> tuple[int, int]:
    """1-indexed line range covered by a node."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def _node_symbol(node, text: str) -> str | None:
    """Extract the declared name from a function/class/etc node.

    tree-sitter exposes the identifier as a child of the node, varying by
    grammar. We pick the first `identifier`-typed child (or
    `type_identifier` / `simple_identifier` for Kotlin). Returns None
    when no name can be found (anonymous functions, lambdas, etc).
    """
    NAME_TYPES = {"identifier", "type_identifier", "simple_identifier",
                  "property_identifier", "variable_declarator"}
    # Walk top-level + nested children one level deep — the name node is
    # usually a direct or near-direct child.
    candidates = list(node.children)
    for child in node.children:
        candidates.extend(child.children if hasattr(child, "children") else [])
    for c in candidates:
        if c.type in NAME_TYPES:
            name = text[c.start_byte:c.end_byte].strip()
            if name and name.isidentifier():
                return name
            # For variable_declarator (`const X = ...`), recurse one more level
            for sub in getattr(c, "children", []):
                if sub.type in NAME_TYPES:
                    sub_name = text[sub.start_byte:sub.end_byte].strip()
                    if sub_name and sub_name.isidentifier():
                        return sub_name
    return None


def _emit_window_fallback(text_slice: str, start_line: int):
    """Fall back to line-window chunks for oversized leaves (rare)."""
    from .chunker import _chunk_default, Chunk
    sub = _chunk_default(text_slice)
    return [Chunk(text=c.text,
                  start_line=c.start_line + start_line - 1,
                  end_line=c.end_line + start_line - 1) for c in sub]


def _container_body(node):
    """Return the inner body of a container node, or the node itself if
    we can't identify a body field. Walking the body's children gets us
    the methods/fields of a class without re-visiting the header.
    """
    # tree-sitter exposes named fields via `child_by_field_name`; not all
    # grammars use them consistently. Easier path: pick the first child
    # whose type ends in `_body`. Falls back to all children.
    for c in node.children:
        if c.type.endswith("_body"):
            return c
    return node


def _walk(nodes, text: str, target: int, max_t: int):
    """Greedily pack sibling decls into chunks; recurse into oversized
    containers. Yields (Chunk, symbols: list[str]) tuples in source order.
    Symbols are merged into Chunk.symbols by ast_chunk's final step.
    """
    from .chunker import Chunk

    # Buffer for the running pack.
    buf_text: list[str] = []
    buf_start_line: int | None = None
    buf_end_line: int = 0
    buf_tokens: int = 0
    buf_symbols: list[str] = []

    def flush():
        nonlocal buf_text, buf_start_line, buf_end_line, buf_tokens, buf_symbols
        if buf_start_line is None or not buf_text:
            return None
        ch = Chunk(text="".join(buf_text),
                   start_line=buf_start_line,
                   end_line=buf_end_line)
        symbols = list(buf_symbols)
        buf_text = []
        buf_start_line = None
        buf_end_line = 0
        buf_tokens = 0
        buf_symbols = []
        return (ch, symbols)

    for node in nodes:
        t = node.type
        if t in _SKIP_TYPES:
            continue
        node_text = _node_text(text, node)
        if not node_text.strip():
            continue

        node_tokens = _tok_count(node_text)
        sl, el = _node_lines(node)

        # Containers ≥ TARGET get split into per-member chunks via recursion.
        # A 4000-token class is well within max_t but emitting it as one chunk
        # defeats the point of AST chunking — we want one chunk per method.
        if node_tokens > target and t in _CONTAINER_TYPES:
            ch = flush()
            if ch is not None:
                yield ch
            # Inject the container's own name so children's chunks can be
            # discovered by the enclosing class/object name too.
            container_sym = _node_symbol(node, text)
            body = _container_body(node)
            for sub_chunk, sub_syms in _walk(list(body.children), text, target, max_t):
                merged = ([container_sym] if container_sym else []) + sub_syms
                yield (sub_chunk, merged)
            continue

        # If too big and NOT a container (or a container with no body), fall
        # back to line-window inside it. Rare on real code.
        if node_tokens > max_t:
            ch = flush()
            if ch is not None:
                yield ch
            for fb in _emit_window_fallback(node_text, sl):
                yield (fb, [])
            continue

        # Fits — pack it in the running buffer if it'd stay within target.
        if buf_tokens > 0 and buf_tokens + node_tokens > target:
            ch = flush()
            if ch is not None:
                yield ch

        if buf_start_line is None:
            buf_start_line = sl
        buf_text.append(node_text)
        # If there's a gap of blank lines / unhandled siblings, preserve them
        # by appending a newline so chunks read naturally.
        if not node_text.endswith("\n"):
            buf_text.append("\n")
        buf_end_line = el
        buf_tokens += node_tokens
        # Capture this declaration's symbol name (if any) so the chunk is
        # discoverable via lookup_symbol(<name>). Skip import/package/comment
        # nodes whose identifiers aren't useful lookup targets.
        if t not in _NO_SYMBOL_TYPES:
            sym = _node_symbol(node, text)
            if sym and len(sym) >= 3:
                buf_symbols.append(sym)

    ch = flush()
    if ch is not None:
        yield ch


# Trailing `export default Foo;` and similar one-liners would otherwise
# become tiny chunks that dominate name-overlap queries (the chunk is short,
# the embedding is name-heavy). Merge any chunk below this threshold into
# its previous neighbour when the combined size still fits within MAX.
MIN_CHUNK_TOKENS = 80


def _merge_small(pairs, max_t: int):
    """Post-pass: pull any small chunk into the previous one. If that would
    overflow MAX, try the next chunk instead. Leaves alone only when both
    neighbours are full. Operates on (Chunk, symbols) pairs.
    """
    if not pairs:
        return pairs
    from .chunker import _tok, Chunk
    out: list = []
    sizes: list[int] = []
    for ch, syms in pairs:
        sz = _tok(ch.text)
        if sz < MIN_CHUNK_TOKENS and out and sizes[-1] + sz <= max_t:
            prev_ch, prev_syms = out[-1]
            out[-1] = (Chunk(
                text=prev_ch.text + ch.text,
                start_line=prev_ch.start_line,
                end_line=ch.end_line,
            ), list(prev_syms) + list(syms))
            sizes[-1] = sizes[-1] + sz
            continue
        out.append((ch, list(syms)))
        sizes.append(sz)
    # Second pass: small chunks at the front of a run merge forward.
    final: list = []
    i = 0
    while i < len(out):
        ch, syms = out[i]
        sz = sizes[i]
        if sz < MIN_CHUNK_TOKENS and i + 1 < len(out):
            nxt_ch, nxt_syms = out[i + 1]
            nxt_sz = sizes[i + 1]
            if sz + nxt_sz <= max_t:
                final.append((Chunk(
                    text=ch.text + nxt_ch.text,
                    start_line=ch.start_line,
                    end_line=nxt_ch.end_line,
                ), list(syms) + list(nxt_syms)))
                i += 2
                continue
        final.append((ch, syms))
        i += 1
    return final


def ast_chunk(text: str, lang: str) -> list:
    """AST-aware chunker entrypoint. Returns list[Chunk] with Chunk.symbols
    populated. Raises on parser load failures so the dispatcher can fall
    back cleanly.
    """
    from .chunker import Chunk
    parser = _get_parser(lang)
    tree = parser.parse(text.encode("utf-8", errors="replace"))
    root = tree.root_node
    pairs = list(_walk(list(root.children), text,
                       target=config.CHUNK_TARGET_TOKENS,
                       max_t=config.CHUNK_MAX_TOKENS))
    merged = _merge_small(pairs, max_t=config.CHUNK_MAX_TOKENS)
    # Promote symbols into the Chunk dataclass for downstream consumers.
    out: list = []
    for ch, syms in merged:
        # Dedupe symbol list, preserve order.
        seen = set()
        ordered = [s for s in syms if not (s in seen or seen.add(s))]
        ch.symbols = ordered  # set attribute; Chunk now carries symbols
        out.append(ch)
    return out


# --- Dispatch ---------------------------------------------------------------

# Map file extension → AST language. Anything not in here falls through to
# the line-window chunker.
_EXT_TO_LANG = {
    "kt": "kotlin",
    "kts": "kotlin",
    "ts": "typescript",
    "tsx": "tsx",
    "js": "tsx",   # ts-grammar tsx variant parses modern JS reasonably well
    "jsx": "tsx",
    "mjs": "tsx",
    "cjs": "tsx",
}


def ast_supported(ext: str) -> str | None:
    """Return the AST language id for this extension, or None."""
    return _EXT_TO_LANG.get(ext.lower())
