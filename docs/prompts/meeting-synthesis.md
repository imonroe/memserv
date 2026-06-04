# Meeting synthesis

**Goal:** turn raw meeting notes or a transcript into the things that actually
matter afterward — decisions, action items, and risks — and persist the durable
ones to memory so they resurface in later sessions.

Adapted from the OB1 "Meeting Synthesis" skill for this server's six tools.

## Prompt block

```markdown
## Meeting synthesis

When I paste meeting notes or a transcript:

1. Recall first: `search_memories` for the project or people involved to load
   prior context and avoid contradicting earlier decisions.
2. Produce four sections:
   - **Decisions** — what was decided, each as a one-line statement.
   - **Action items** — task, owner, and due date if stated.
   - **Risks / open issues** — anything flagged as a concern or unresolved.
   - **Deliverables** — concrete outputs expected downstream.
3. Persist the durable items with `add_memory`, one per call — every decision,
   and any action item or risk that outlives the meeting. Tag
   `agent_id` = "meeting" and put a date in `metadata`, e.g.
   `{"date": "2026-06-04"}`.
4. If a decision supersedes one already in memory, `update_memory` the old one
   instead of adding a conflicting record.
5. Skip small talk and scheduling noise. Never store sensitive personal data.
6. Output the four sections and note which items you saved.
```

## Notes

- Decisions are the highest-value thing to persist; action items often live in a
  tracker already, so save the ones you'll want recalled in conversation, not the
  whole list.
- Tagging with a `date` in metadata pairs well with the
  [recency-boosted search](../USER_GUIDE.md#search-memories--post-apiv1memoriessearch)
  option — raise `recency_weight` when you ask "what did we most recently decide?"
