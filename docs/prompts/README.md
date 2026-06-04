# Companion prompts (skill packs)

These are **copy-paste prompt packs** that make the memory server more useful for
recurring kinds of work. They aren't code and they don't change the server — they
ride on top of the six MCP tools (`search_memories`, `add_memory`,
`list_memories`, `get_memory`, `update_memory`, `delete_memory`) that any
connected client exposes.

The idea is adapted from the [OB1 / Open Brain](https://github.com/NateBJones-Projects/OB1)
project's "skill packs," reworked for this server's single-user model and tool names.

For the baseline "always recall first, save durable facts, don't duplicate, don't
store secrets" instruction block, see
[Prompting agents to use memory](../USER_GUIDE.md#prompting-agents-to-use-memory)
in the User Guide. The packs here are the next layer up: structured workflows for
specific tasks.

## How to use a pack

1. Open the pack and copy its prompt block.
2. Paste it into your client where instructions live — a Claude Project, a
   `CLAUDE.md`, ChatGPT custom instructions, an `AGENTS.md`, or just inline at the
   start of a chat.
3. Adjust the tool names to match how your client surfaces them (Claude Code, for
   example, namespaces them like `mcp__mem0-remote__search_memories`).

All packs assume a single shared memory store: searches and lists span everything,
and `agent_id` is only a write-time provenance tag (it never partitions reads).

## Available packs

| Pack | Use it when you want to… |
|---|---|
| [Auto-capture](./auto-capture.md) | Have the agent save a structured summary of a work session at the end, so the next session starts with context. |
| [Research synthesis](./research-synthesis.md) | Turn sources or notes into findings with confidence levels and open questions, persisted to memory. |
| [Meeting synthesis](./meeting-synthesis.md) | Turn meeting notes or a transcript into decisions, action items, and risks, persisted to memory. |

Contributions of new packs are welcome — copy the shape of an existing file.
