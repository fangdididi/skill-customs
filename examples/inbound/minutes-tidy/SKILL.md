---
name: minutes-tidy
description: Turn raw meeting notes the user pastes or points to into short minutes with decisions, open questions and an owner-and-date action list. Use when the user asks to clean up, summarise or extract actions from meeting notes.
---

# minutes-tidy

1. Read the notes file the user names (only that file).
2. Run `python3 scripts/extract_actions.py NOTES.md` to pull lines that start with
   `TODO`, `ACTION` or `@name`.
3. Write `NOTES.minutes.md` next to the input with three sections:
   Decisions, Open questions, Actions (owner, what, due date).
4. Show the minutes to the user. Never guess an owner or a date; leave `?` instead.
