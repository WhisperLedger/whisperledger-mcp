"""Verify jove_client basics work end-to-end."""
from agent.jove_client import list_spaces, suggest_spaces, is_known_space

spaces = list_spaces()
print(f"loaded {len(spaces)} spaces from Jove")
print(f"is_known_space('TECH'): {is_known_space('TECH')}")
print(f"is_known_space('tech'): {is_known_space('tech')}  # case-insensitive")
print(f"is_known_space('NONEXISTENT'): {is_known_space('NONEXISTENT')}")
print(f"suggest_spaces('payment'): {suggest_spaces('payment')}")
print(f"suggest_spaces('prod'): {suggest_spaces('prod')}")
print(f"top-5 by page_count: {suggest_spaces('')[:5]}")
print()
print("=== top-15 spaces by page_count ===")
for k in sorted(spaces, key=lambda x: -(spaces[x].get('page_count') or 0))[:15]:
    s = spaces[k]
    print(f"  {k:<20} {s.get('space_name','?')[:30]:<32} pages={s.get('page_count'):<6} idx_at={s.get('latest_indexed_at','?')[:10]}")
