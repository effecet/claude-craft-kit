---
description: Review open pending items and resolve or archive the ones that are done
---

Review the pending work queue and clear out what is finished.

Requires the optional memory backend (`BRAIN_ENABLED=true`) — it reads and writes
the `pending` table through the memory-persistor MCP server.

1. Call `mcp__memory-persistor__pending_list` with `status: "open"` and `limit: 50`.
2. Present the items grouped by priority, one line each: `[priority] title (id-prefix)`.
3. For each item, judge from the current repo state and this session's context
   whether it is already done. Do NOT guess — if you cannot tell, leave it open.
4. Present the proposed resolutions as a single batch and wait for approval,
   per `rules/operations.md` § Proposal Gate:

   ```
   📋 Proposed: resolve N pending items
   📁 Scope: <titles>
   ⚠️  Risks: none (reversible — status flips back with an UPDATE)

   Proceed? [yes / no / modify]
   ```
5. On approval, call `mcp__memory-persistor__pending_resolve` once per approved
   item, passing a one-line `resolution` explaining what closed it.
6. Report the new open count.

Never resolve an item the user did not approve. Never delete rows — `pending_resolve`
sets `status='done'`, which is reversible.
