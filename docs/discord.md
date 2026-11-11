# Discord notifications (planning only)

`collector/discord.py` decides which feed events would become Discord messages. It sends nothing: it has no
network code, no clock and no webhook address, and `python3 -B -m collector discord --feed data/feed.json
[--delivery-state data/discord-state.json]` only prints the plan (it reads the two files and writes nothing).
Sending, the workflow job and the repository secret are separate steps that need their own review and the
repository owner's settings; until they exist nothing is posted anywhere.

## What would be sent

| Event type | Message |
| --- | --- |
| `repository_discovered`, `release_published` | one message each, oldest first, at most 5 per plan |
| more than 5 of those | one `[MORE]` message that only counts them and links the site |
| `commits_observed` | one digest per plan: up to 10 repositories, then "and N more" |
| anything else (forks, fork activity, game association changes, stale notices) | never sent, only marked seen |

A message names the repository, the game when the label is reviewed (strong or medium confidence, otherwise
"game unknown"), a fixed sentence saying the association is inferred, and the repository link in angle brackets
(no preview). Repository text (names, descriptions, release tags) is plain text only: controls stripped, mention
forms neutralised, links removed, Markdown punctuation escaped, bounded. Every payload sets `allowed_mentions` to
an empty `parse` list and suppresses embeds.

## Delivery state

`{"format_version": 1, "seen": [event ids], "attempts": {event id: failed sends}}`. An event in `seen` is never
planned again. With no state the plan is a **baseline**: no messages, and every event in the feed becomes seen, so
switching notifications on never posts the history. After a send, `apply_delivery` marks the events of delivered
messages seen; the events of a failed message keep a failure count and are tried again on the next plan, and are
given up (seen) after 3 failures. `seen` keeps the newest 2000 ids; the feed holds at most 500 events, and an id
that left the feed cannot return.

The state holds event ids only (already public in the feed). The webhook address is a secret and is never stored
in it, in the repository or in a log.
