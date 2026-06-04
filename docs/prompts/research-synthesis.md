# Research synthesis

**Goal:** turn raw sources — articles, docs, notes, search results — into
structured findings with explicit confidence levels and open questions, and
persist the durable conclusions to memory so later work can build on them.

Adapted from the OB1 "Research Synthesis" skill for this server's six tools.

## Prompt block

```markdown
## Research synthesis

When I give you sources to synthesize (links, pasted text, notes, or a topic to
research):

1. Recall first: `search_memories` for the topic to surface anything I've already
   concluded, so you extend rather than repeat prior work.
2. Read the sources and produce:
   - **Findings** — the key claims, each as a one-line statement.
   - **Confidence** — mark each finding high / medium / low based on source
     quality and agreement.
   - **Contradictions** — where sources disagree, say so explicitly.
   - **Open questions** — what's still unresolved and worth following up.
3. Persist the durable conclusions with `add_memory`, one finding per call.
   Put the confidence and a short source reference in `metadata`, e.g.
   `{"confidence": "high", "source": "<url or title>"}`, and tag
   `agent_id` = "research".
4. Do not save low-confidence guesses as if they were facts; either omit them or
   store them clearly labeled `"confidence": "low"`.
5. Never store anything sensitive.
6. End with the findings list and note which ones you saved.
```

## Notes

- The `metadata` fields (`confidence`, `source`) are free-form today and are
  stored on the memory. A future server change (see the provenance/review issue in
  the backlog) may make them first-class and filterable; saving them now means the
  data is already there when that lands.
- Keep findings atomic — one claim per memory — so confidence and sources stay
  attached to the right statement and updates are surgical.
