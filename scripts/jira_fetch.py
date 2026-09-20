"""Fetch a Jira ticket via Atlassian REST API and emit a small JSON blob the
bash wrapper consumes. Auth: CONFLUENCE_EMAIL + CONFLUENCE_API_TOKEN.

Usage:  python3 jira_fetch.py <TICKET-KEY>
Output (single-line JSON to stdout):
  {"ok": true, "summary": "...", "description": "...", "attachments_csv": "url1,url2"}
or
  {"ok": false, "error": "<short reason>"}
"""
import json, os, re, subprocess, sys

def emit_error(msg, code=1):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)

if len(sys.argv) < 2:
    emit_error("usage: jira_fetch.py <TICKET-KEY>")
key = sys.argv[1].strip()
if not re.match(r'^[A-Z][A-Z0-9_]+-[0-9]+$', key):
    emit_error(f"invalid ticket key shape: {key!r}")

email = os.environ.get("CONFLUENCE_EMAIL", "")
token = os.environ.get("CONFLUENCE_API_TOKEN", "")
if not email or not token:
    emit_error("CONFLUENCE_EMAIL or CONFLUENCE_API_TOKEN not set in env")

url = f"https://jupitermoney.atlassian.net/rest/api/3/issue/{key}?fields=summary,description,attachment,status&expand=renderedFields"
r = subprocess.run(["curl", "-sS", "--max-time", "20",
                    "-u", f"{email}:{token}",
                    "-H", "Accept: application/json", url],
                   capture_output=True, text=True)
if r.returncode != 0:
    emit_error(f"curl exited {r.returncode}: {r.stderr[:200]}")

try:
    d = json.loads(r.stdout)
except Exception as e:
    emit_error(f"failed to parse Jira response: {e}")

if d.get("errorMessages"):
    emit_error(f"jira: {d['errorMessages'][0]}")
if "fields" not in d:
    emit_error(f"jira response missing 'fields' (status={d.get('status')})")

fields = d["fields"]
summary = (fields.get("summary") or "").strip()
rd = (d.get("renderedFields", {}).get("description") or "")
# Strip HTML tags + normalise whitespace
desc = re.sub(r'<br/?>', '\n', rd, flags=re.I)
desc = re.sub(r'</p>', '\n\n', desc, flags=re.I)
desc = re.sub(r'<[^>]+>', '', desc)
desc = re.sub(r'\n{3,}', '\n\n', desc).strip()

atts = fields.get("attachment", []) or []
media_urls = [a["content"] for a in atts
              if a.get("mimeType","").split("/")[0] in ("image","video")
              and a.get("content")]

status = ((fields.get("status") or {}).get("name") or "").strip()
status_category = (((fields.get("status") or {}).get("statusCategory") or {}).get("key") or "").strip()

print(json.dumps({
    "ok": True,
    "ticket": key,
    "summary": summary,
    "status": status,
    "status_category": status_category,
    "description": desc,
    "attachments_csv": ",".join(media_urls),
    "attachments_count": len(media_urls),
    "total_attachments_in_jira": len(atts),
}))
