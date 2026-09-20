"""End-to-end smoke test of /jarvis ask flow via jove_client."""
from agent.jove_client import ask

res = ask(
    query="What is Project Claudify? Summarise the proposal.",
    user_id_email="rohit.pandey@jupiter.money",
    confluence_space="TECH",
    timeout_sec=90,
)
print("=" * 60)
print(f"keys returned: {list((res or {}).keys())}")
print(f"session_id:    {(res or {}).get('session_id')}")
print(f"skill:         {(res or {}).get('skill')}")
print("=" * 60)
print((res or {}).get("response", "(no response)")[:2500])
