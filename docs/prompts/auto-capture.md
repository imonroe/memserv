# Auto-capture: save a session summary

**Goal:** when a working session wraps up, have the agent distill what happened
into a few durable memories so the *next* session can recall it. This closes the
loop that makes a personal memory store actually compound over time.

Adapted from the OB1 "Auto-Capture" skill for this server's six tools.

## Prompt block

Paste into a `CLAUDE.md`, a Project's custom instructions, an `AGENTS.md`, or
inline at the start of a session.

```markdown
## Session auto-capture

When a working session is wrapping up — I say we're done, the task is finished,
or the conversation is clearly ending — capture what's worth remembering:

1. First call `search_memories` for the session's main topic to see what's
   already stored, so you update instead of duplicating.
2. Then save the durable takeaways with `add_memory`, one clear fact per call:
   - decisions made and the reasoning behind them
   - conventions, preferences, or constraints I stated
   - unfinished work and the agreed next step
   - useful facts discovered (paths, commands, config, names) likely to recur
   Tag each with `agent_id` = "auto-capture" for provenance.
3. If a saved memory is now wrong, `update_memory` it rather than adding a new one.
4. Skip transient chatter, one-off details, and anything sensitive
   (passwords, API keys, private personal data).
5. Briefly list what you saved so I can correct it.
```

## Notes

- Keep each memory a single, self-contained statement — "We deploy to CapRover
  on push to `main`" beats a paragraph. Short facts retrieve and update cleanly.
- The `agent_id` tag (`auto-capture`) is write-only provenance; it does **not**
  scope future searches, so these memories surface for every client.
- Pair this with the baseline "recall first" instruction so the next session
  opens by reading what the previous one saved.
