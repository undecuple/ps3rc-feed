# Discord notifications

`collector/discord.py` decides which feed events would become Discord messages. It has no network code, no clock
and no webhook address, and `python3 -B -m collector discord --feed data/feed.json
[--delivery-state data/discord-state.json]` only prints the plan (it reads the two files and writes nothing).
`collector/discord_send.py` posts the planned messages, and only `discord --send` reaches it. No workflow calls
it yet: the job and the repository secret are separate steps that need their own review and the repository
owner's settings, so nothing is posted automatically.

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

## Sending

`python3 -B -m collector discord --feed data/feed.json --delivery-state data/discord-state.json --send
--delivery-state-out data/discord-state.json`

- The webhook address comes only from the `DISCORD_WEBHOOK_URL` environment variable, never from an argument or a
  file. It must be exactly `https://discord.com/api/webhooks/<numeric id>/<token>` (no other host, port, user,
  query or path); otherwise the command refuses (exit 2) with wording that names no part of the address. The address
  is never printed, logged or stored.
- With no `--delivery-state` the command writes the baseline state and sends nothing, and needs no webhook. It
  refuses to do that when the output file already exists (a forgotten `--delivery-state` must not reset the state),
  and the output may never be the `--feed` file. The output may be the input state, which is the normal use.
- One verified-TLS POST per message, at least 1 second apart, no redirects, a 20 second socket timeout. A 429 waits
  its `retry_after` once when that is 30 seconds or less, otherwise the run stops. The first message that is not
  delivered ends the run; the rest are not attempted.
- The state is written before the first send (an unwritable path fails first) and again after every message, so a
  crash or a kill loses at most the message in flight.
- What counts against an event's 3 attempts: only a 4xx other than 401, 403, 404 and 429 (`failed`: Discord refused
  that message). A 5xx or a network error (`error`), an exhausted rate limit (`limited`), a rejected webhook (401,
  403, 404: `rejected`) and messages not attempted count nothing, so an outage or a removed webhook does not use up
  the retries; the next run tries again, one request at a time.
- Delivery is at least once: if a timeout hits after Discord took the message, the next run sends it again, so a
  message can very rarely appear twice.
- The output lists each message with its result (`ok`, `failed`, `error`, `rejected`, `limited` or `not sent`).
  Exit 0 when every planned message was delivered or none was planned, 3 otherwise, 2 when refused.

## The workflow

`.github/workflows/discord.yml` is a separate workflow from the refresh, so the refresh never holds the webhook
address. It does nothing unless the repository variable `DISCORD_ENABLED` is `true`.

- It starts when a refresh run completes successfully on `main`, and reads `feed.json` from the `feed-state` branch.
  A refresh that failed or was skipped posts nothing.
- The delivery state is the one file `discord-state.json` on the `feed-discord` branch, updated with plain
  fast-forward pushes (never a force push). Runs never overlap (own concurrency group).
- The first run has no `feed-discord` branch: it is a baseline. It posts nothing, needs no webhook and records every
  event now in the feed as seen, so the history is never posted.
- The webhook secret `DISCORD_WEBHOOK_URL` is an environment value of the one step that sends. That step has no
  repository token; the step that pushes the state has the token and no secret.
- A run that cannot deliver every message still pushes the state (so the failure counts persist) and then fails, so
  it shows red; the next refresh tries again. A rejected or missing webhook refuses before anything is sent and
  leaves the state as it was. If the state push fails after messages were sent, the next run may send them again.
- Turning it on is two settings by the maintainer: create the secret, then set the variable. Setting the variable
  alone only records the baseline. A removed variable stops the workflow; the `feed-discord` branch keeps the state.
