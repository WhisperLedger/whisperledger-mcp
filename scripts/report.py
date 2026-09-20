"""Render a Jarvis batch JSONL into a styled HTML report for engineering review."""
from __future__ import annotations
import html
import json
import re
from pathlib import Path

CSS = """
:root {
  --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e;
  --accent:#79c0ff; --accent2:#d2a8ff; --green:#56d364; --red:#ff7b72; --code-bg:#1f242c;
  --table-row:#1c2129;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",system-ui,sans-serif;
  font-size:15px;line-height:1.55;}
.page{max-width:1000px;margin:0 auto;padding:30px 40px 60px;}
.header{display:flex;align-items:center;gap:14px;margin-bottom:20px;
  padding-bottom:18px;border-bottom:1px solid var(--border);}
.logo{width:38px;height:38px;border-radius:9px;
  background:linear-gradient(135deg,#6e40c9,#218bff);
  display:flex;align-items:center;justify-content:center;font-weight:700;color:white;font-size:18px;}
.title{font-size:22px;font-weight:600;}
.subtitle{color:var(--muted);font-size:13px;margin-top:2px;}
.badge{display:inline-block;background:rgba(86,211,100,0.12);color:var(--green);
  border:1px solid rgba(86,211,100,0.4);padding:2px 8px;border-radius:999px;
  font-size:11px;font-weight:500;margin-left:8px;}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0 30px;}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px 14px;}
.stat .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;}
.stat .value{font-size:20px;font-weight:600;margin-top:4px;}
.qa{margin-bottom:34px;border:1px solid var(--border);border-radius:10px;overflow:hidden;
  background:var(--panel);}
.qa-head{padding:14px 18px;border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:flex-start;gap:14px;}
.qnum{color:var(--muted);font-size:12px;}
.q{font-size:16px;font-weight:500;margin-top:4px;}
.q .asker{color:var(--muted);font-weight:400;font-size:13px;margin-left:6px;}
.qa-meta{font-size:11px;color:var(--muted);text-align:right;white-space:nowrap;}
.qa-body{padding:16px 20px;}
.qa-body h2{font-size:18px;margin:18px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border);}
.qa-body h3{font-size:15px;margin:14px 0 6px;color:var(--accent2);}
.qa-body h1{font-size:20px;margin:18px 0 10px;}
.qa-body p{margin:7px 0;}
.qa-body ul,.qa-body ol{margin:7px 0;padding-left:22px;}
.qa-body table{border-collapse:collapse;margin:10px 0;width:100%;font-size:13px;}
.qa-body th,.qa-body td{border:1px solid var(--border);padding:6px 9px;text-align:left;vertical-align:top;}
.qa-body th{background:var(--code-bg);color:var(--accent);font-weight:600;}
.qa-body tr:nth-child(even) td{background:var(--table-row);}
.qa-body code{font-family:"SF Mono","JetBrains Mono",Menlo,monospace;font-size:12.5px;
  background:var(--code-bg);color:var(--accent);padding:1px 6px;border-radius:4px;
  border:1px solid var(--border);}
.qa-body pre{background:var(--code-bg);border:1px solid var(--border);border-radius:6px;
  padding:11px 14px;overflow-x:auto;font-family:"SF Mono",Menlo,monospace;font-size:12.5px;line-height:1.5;}
.qa-body pre code{background:transparent;border:none;padding:0;color:var(--fg);}
.qa-body strong{color:var(--fg);}
.qa-body hr{border:none;border-top:1px solid var(--border);margin:14px 0;}
details{margin-top:14px;background:var(--code-bg);border:1px solid var(--border);
  border-radius:6px;padding:8px 12px;font-size:12px;}
details summary{cursor:pointer;color:var(--muted);}
details .tools{margin-top:8px;font-family:Menlo,monospace;font-size:11.5px;color:var(--fg);}
.tools .toolrow{padding:3px 0;border-bottom:1px dashed var(--border);}
.tools .toolrow:last-child{border-bottom:none;}
.error{color:var(--red);font-family:Menlo,monospace;font-size:13px;}
.validation{margin-top:14px;padding:10px 14px;background:rgba(121,192,255,0.06);
  border:1px solid rgba(121,192,255,0.3);border-radius:6px;font-size:13px;color:var(--muted);}
.validation strong{color:var(--accent);}
.footer{margin-top:30px;padding-top:14px;border-top:1px solid var(--border);
  color:var(--muted);font-size:12px;display:flex;justify-content:space-between;}
"""

# Lightweight markdown → HTML. Handles headings, fenced code, inline code, bold,
# tables (GFM), unordered/ordered lists, hr, paragraphs. Good enough for Jarvis output.
def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    para: list[str] = []

    def flush_para():
        if para:
            txt = " ".join(para).strip()
            out.append(f"<p>{_inline(txt)}</p>")
            para.clear()

    def flush_code():
        out.append(f"<pre><code>{html.escape('\\n'.join(code_buf))}</code></pre>")
        code_buf.clear()

    while i < len(lines):
        line = lines[i]

        if in_code:
            if line.strip().startswith("```"):
                flush_code()
                in_code = False
                i += 1
                continue
            code_buf.append(line)
            i += 1
            continue

        stripped = line.strip()

        if stripped.startswith("```"):
            flush_para()
            in_code = True
            code_lang = stripped[3:].strip()
            i += 1
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # hr
        if re.match(r"^---+$", stripped):
            flush_para()
            out.append("<hr/>")
            i += 1
            continue

        # table (GFM): header | sep | rows
        if "|" in stripped and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i+1]):
            flush_para()
            header = _split_row(stripped)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i].strip()))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue

        # unordered list
        if re.match(r"^[-*]\s+", stripped):
            flush_para()
            items: list[str] = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(lines[i].strip()[2:].strip())
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + "</ul>")
            continue

        # ordered list
        if re.match(r"^\d+\.\s+", stripped):
            flush_para()
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + "</ol>")
            continue

        # blank line ends paragraph
        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(stripped)
        i += 1

    if in_code:
        flush_code()
    flush_para()
    return "\n".join(out)


def _split_row(row: str) -> list[str]:
    s = row.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def _inline(s: str) -> str:
    # escape first
    s = html.escape(s)
    # inline code
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # bold
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    # links [text](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def render_report(records: list[dict], out_path: Path) -> None:
    total = len(records)
    ok_records = [r for r in records if r.get("ok")]
    total_cost = round(sum(r.get("est_cost_usd", 0) for r in ok_records), 3)
    avg_time = round(sum(r.get("elapsed_sec", 0) for r in ok_records) / max(1, len(ok_records)), 1)
    avg_iters = round(sum(r.get("iterations", 0) for r in ok_records) / max(1, len(ok_records)), 1)

    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Jarvis batch report</title>",
        f"<style>{CSS}</style></head><body><div class='page'>",
        "<div class='header'><div class='logo'>J</div><div>",
        "<div class='title'>Jarvis <span class='badge'>batch report</span></div>",
        "<div class='subtitle'>Engineering validation pack — please verify cited file paths and behaviors</div>",
        "</div></div>",
        "<div class='summary'>",
        f"<div class='stat'><div class='label'>Questions</div><div class='value'>{total}</div></div>",
        f"<div class='stat'><div class='label'>Avg time / Q</div><div class='value'>{avg_time}s</div></div>",
        f"<div class='stat'><div class='label'>Avg iterations</div><div class='value'>{avg_iters}</div></div>",
        f"<div class='stat'><div class='label'>Total cost</div><div class='value'>${total_cost}</div></div>",
        "</div>",
    ]

    for r in records:
        i = r.get("i", "?")
        asker = f" <span class='asker'>— asked by {html.escape(r['asker'])}</span>" if r.get("asker") else ""
        meta = (f"{r.get('elapsed_sec', '?')}s · {r.get('iterations', '?')} iters · "
                f"{len(r.get('tool_calls', []))} tool calls · ${r.get('est_cost_usd', 0):.3f}") if r.get("ok") else "FAILED"
        parts.append("<div class='qa'>")
        parts.append(
            f"<div class='qa-head'><div><div class='qnum'>Q{i}</div>"
            f"<div class='q'>{html.escape(r.get('q',''))}{asker}</div></div>"
            f"<div class='qa-meta'>{meta}</div></div>"
        )
        parts.append("<div class='qa-body'>")
        if r.get("ok"):
            parts.append(md_to_html(r.get("answer", "")))
            # tool call detail
            if r.get("tool_calls"):
                rows = "\n".join(
                    f"<div class='toolrow'>{html.escape(tc['name'])}({html.escape(json.dumps(tc.get('args', {}), ensure_ascii=False))})</div>"
                    for tc in r["tool_calls"]
                )
                parts.append(
                    f"<details><summary>Tool calls ({len(r['tool_calls'])})</summary>"
                    f"<div class='tools'>{rows}</div></details>"
                )
            parts.append(
                "<div class='validation'><strong>For reviewer:</strong> Are the cited file paths real? "
                "Is the described behavior accurate? Anything Jarvis missed or got wrong?</div>"
            )
        else:
            parts.append(f"<div class='error'>{html.escape(r.get('error','unknown error'))}</div>")
        parts.append("</div></div>")

    parts.append(
        "<div class='footer'>"
        "<div>Stack: Voyage <code>voyage-code-3</code> · Qdrant 1.18 · Claude Sonnet 4.6</div>"
        "<div>5 repos · 29,612 chunks indexed</div>"
        "</div>"
    )
    parts.append("</div></body></html>")
    out_path.write_text("".join(parts), encoding="utf-8")
