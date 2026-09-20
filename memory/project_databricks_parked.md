---
name: Databricks-as-Jarvis-tools — discussed, parked 2026-05-18
description: Proposal to add Databricks SQL + notebook search as Jarvis tools (not a separate bot, since no PII + T-1 data). Parked by Rohit for now; revisit when there's pull demand.
type: project
originSessionId: b7218088-f9d8-46d9-9ad2-fe62fb99e7e9
---
**Decision (2026-05-18):** Parked.

**The proposal:** Add Databricks as tools inside Jarvis (not a separate bot) so engineers can ask code-with-data-grounding questions — *"how many users hit this path last week"*, *"did conversion shift after 3.2.40"*, *"who's still on the old endpoint"*. Justified because Databricks at Jupiter has **no PII** and is **T-1 data** (per Rohit), so the PII-blast-radius / live-data concerns that pushed me toward a separate bot for general data access don't apply here.

**Proposed v0 tool surface** (1-2 days build if greenlit):
- `databricks_sql_query(sql, max_rows=1000)` — bounded, with EXPLAIN-based cost estimate + auto-LIMIT injection + timeout
- `databricks_list_tables(database)` + `databricks_describe_table(name)` — schema discovery
- `databricks_search_notebooks(query)` — RAG over notebooks (separate Qdrant collection)

**Why parked:** Rohit chose to defer; no specific user pull yet, and the OpsGenie integration just shipped — focus on landing that + the proposed Jericho bot before adding another domain to Jarvis.

**Trigger to revisit:**
- Real engineer asks "I wish Jarvis could query Databricks for X" 2+ times
- Or post-incident analysis comes up enough that the T-1 data would have meaningfully helped
- Or the Jericho design solidifies and we want to decide whether data is in Jericho's scope or its own

**Architectural rule established in the same discussion** (worth holding to):
> *Each new domain stays in Jarvis until it has a write surface that needs different safety semantics — then it spins out.*

This is the line that prevents bot-bloat: read-only domain integration → Jarvis. Write surface with different blast radius (Terraform apply, alert mute, ML model deploy) → separate bot composed via MCP.
