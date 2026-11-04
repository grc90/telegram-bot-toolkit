# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

A framework for long-lived Telegram bots: a queue-driven channel posting engine,
an imageboard notifier, and the operational tooling around both. See
[README.md](README.md) for the architecture overview.

This repo is the public extract of a private workspace. It ships one example
posting bot and one notifier; the private deployment runs several of each off the
same shared engine. Never send real Telegram messages or upload files during
development — use the dry-run paths below.

## Commands

Run tests:

```bash
python -m pytest -q
```

Run a single test file / test:

```bash
python -m pytest -q tests/test_posting_shared.py
python -m pytest -q tests/test_posting_shared.py::test_name
```

If a sandbox blocks atomic rename/replace (`os.replace`), rerun from a normal
local shell.

Local read-only diagnostics (never call Telegram, never mutate state — see
`dev.py` and `docs/operacion.md` for the full reference):

```bash
python dev.py health
python dev.py check-env <bot_name>
python dev.py queue examplechannel --preview 5
python dev.py filter-test imageboard_notifier --tag <tag>
python dev.py recent examplechannel --limit 10
python dev.py stats <bot_name> --lines 500
```

Dry-run the posting bot (does not send, does not touch queue/recent-post files —
verified by `tests/test_posting_dry_run.py`):

```bash
python example_posting_bot.py --dry-run --dry-run-count 1
```

Dry-run the notifier (shows filter matches without writing `*_state.json`):

```bash
python imageboard_notifier_bot.py --dry-run
```

Note this dry-run only exercises fetch + filtering; it never reaches
`send_post_to_chat()`. Changes to the delivery path have to be covered by unit
tests (`tests/test_notifier_photo_only.py`, `tests/test_notifier_send_retry.py`)
— a clean dry-run says nothing about them.

Back up runtime JSON before any migration or state-handling change:

```bash
python backup_state.py
```

## Architecture

Three entry points, each an independent bot process:

- `example_posting_bot.py` (`examplechannel`) — a thin config file that calls
  `create_posting_bot(**config)` from `posting_bot_shared.py` and gets back a
  `PostingBotApp` (queue engine, all Telegram commands, dry-run, scheduling,
  rate limiting, backup loop). Almost all posting logic lives in the shared
  module, not here. Adding a channel means copying this file and changing
  `bot_name` plus the env prefix.
- `imageboard_notifier_bot.py` (`imageboard_notifier`) — same pattern for the
  notifier: a config file that calls `create_notifier_bot(**config)` from
  `danbooru_notifier_shared.py` and gets back a `DanbooruNotifierApp`. Ratings,
  score thresholds, the `/last6` score band, env prefix and state file are
  config; a second notifier is a second config file. Tests patch the `APP`
  instance (`from imageboard_notifier_bot import APP`), not module globals.
- `imageboard_search_bot.py` (`imageboard_search`) — interactive operator-driven
  search. Without a Gold account the API allows 2 tags per search and `order:`
  counts as one, so a two-tag AND search goes out without `order:` and three or
  more combined tags are rejected up front.

Each bot has a stable **internal name** (in parentheses) independent of its `.py`
filename. It drives that bot's state/queue/settings/recent-post JSON file names,
its log file name, and its `.env` variable prefix. Renaming the entry-point
script changes none of those; grep for the internal name, not the filename, when
tracing a bot's on-disk footprint.

Shared modules are listed in the README table. The ones with non-obvious
contracts:

- `json_storage.py` — atomic JSON read/write (temp file + fsync + `os.replace`
  with retries). Raises explicit `MalformedJsonError`/`JsonTypeError` instead of
  silently defaulting, so callers can't clobber a corrupt-but-recoverable state
  file.
- `queue_dedupe.py` — deterministic `post_signature()` per post (normalized
  source URL, then Telegram file_id/media_group_id, then a stable JSON fallback),
  used for both queue dedup and recent-post matching.
- `recent_post_guard.py` — per-channel history (`recent_posts_<bot>.json`) with a
  lock file (`*.lock`) around read-prune-write, so concurrent bot processes don't
  race.
- `danbooru_notifier_shared.py` — the notifier engine. Each cycle asks only for
  posts created in the last `*_MAX_POST_AGE_DAYS` days (so re-tagged old posts
  are not re-sent once their id leaves the 10000-id history) and pages back only
  to `updated_cursor`, the start of the last complete cycle — usually one
  request. The cursor only advances when a cycle has no transient failure, so a
  failed post stays reachable. A state without a cursor re-bootstraps instead
  of flooding. Three consecutive API failures send one operator alert.
- `danbooru_client.py` — API HTTP (bounded retries on 429/5xx, honors
  `Retry-After` up to 60s) and the single media policy shared by the notifier
  and search: `photo_media_urls`, `photo_variant_metadata/rejection`, Telegram
  photo limits, `post_url`/`build_caption`.
- `telegram_common.py` — Telegram error classification shared by every bot
  (`should_pause_for_telegram_error`, `telegram_retry_after_seconds`,
  `rewind_media_arguments`).
- `backup_state.py` — timestamped backups under `backups/state_<timestamp>/`,
  plus retention cleanup (24h default, dry-run unless `--apply`). Each posting bot
  runs `backup_loop()` every 6h: it skips the backup when another bot made one
  less than 3h ago, and always runs the cleanup; age is judged by the
  folder-name timestamp, not mtime, since deleting files inside a folder bumps
  its mtime.

`pytest.ini` disables the `anyio` pytest plugin (`-p no:anyio`); the suite is
fully synchronous, so this is invocation-only.

### Runtime data files (not source, not disposable)

`*_state.json`, `*_queue.json`, `*_settings.json`, `recent_posts_*.json`, and
`.env` are all git-ignored operational data, one set per bot/channel. Never
delete, reset, or casually overwrite them — back up with `backup_state.py` first.
`.env.example` documents every required/optional environment variable.

### Notifier delivery pipeline

The notifier delivers **photos only, at good quality**. Three deliberate
constraints, each enforced by `tests/test_notifier_photo_only.py` (which asserts
on `danbooru_notifier_shared` source, so reintroducing any of them fails the suite):

- `photo_media_urls()` yields `file_url` (original) then `large_file_url`
  (sample), in that order — original first because Telegram rescales to 1280px,
  so a larger source survives better. `preview_file_url` is excluded everywhere
  (a 180x180 thumbnail), including from `danbooru_client.media_url()`, so a post whose only
  media is a thumbnail is skipped rather than sent.
- No `send_document` fallback anywhere. A post that fits no photo variant is
  dropped, not sent as a file; extensions that could only go as documents are
  skipped too.
- When the photo path runs out of variants (permanent, no fallback left), the bot
  sends the post link plus the rejection reason so the post isn't silently lost.
  Transient failures stay silent — they are retried next cycle, so notifying
  would spam one message per post during any outage.

`photo_variant_metadata()` / `photo_variant_rejection()` pre-screen variants
against Telegram's photo limits (10 MB, width+height ≤ 10000, aspect ratio ≤
20:1) using the dimensions the API publishes per variant, so an impossible
variant costs no download and no upload. Missing metadata means "attempt it
anyway" rather than skip.

`send_post_to_chat()` returns `SEND_RESULT_SENT` / `SEND_RESULT_SKIPPED` /
`SEND_RESULT_FAILED`, **not a bool**, and that distinction drives retry behavior:
`check_danbooru_once()` passes both SENT and SKIPPED to `mark_handled()`, leaving
only FAILED to be retried next cycle. SKIPPED means "no variant will ever work,
stop trying"; FAILED means "transient, try again". Collapsing the two — or
returning a bool — silently produces either an infinite retry loop on unsendable
posts, or posts dropped forever after one network blip.

Two Telegram wrappers coexist and are easy to conflate:

- `call_telegram()` — for command replies. Honors the per-bot API cooldown
  (`API_FAILURE_COOLDOWN`, 300s, or Telegram's `retry_after` on a 429): returns
  `None` without calling while paused,
  and arms the pause on transient failures.
- `try_telegram()` / `try_telegram_detailed()` — for media sends. Bounded retry
  with backoff on transient errors; the `_detailed` variant also returns the
  exception so callers can classify SKIPPED vs FAILED via
  `should_pause_for_telegram_error()`.

Retries in `try_telegram_detailed()` call `rewind_media_arguments()` first, and
must keep doing so: a failed upload leaves the `BytesIO` at EOF, so retrying with
the same object uploads 0 bytes and Telegram answers `400 Bad Request: file must
be non-empty`.

### Command-vs-action distinction

Two lookalike pairs. On the posting bot: `/pause` stops scheduled publishing
indefinitely until `/resume` (the bot stays otherwise active), while `/stop`
interrupts only the batch currently in flight and lets the next scheduled run
proceed normally. On the notifier, `/stop` is a deliberate no-op that points
users at `/unsubscribe`.

## Safety rules

- Keep secrets only in `.env` (git-ignored); never print token values —
  `dev.py check-env` masks them for a reason.
- Do not stage `.env`, runtime JSON, `backups/`, `logs/`, or caches.
- Never make real Telegram posts to validate changes — use the `--dry-run` paths.
- Preserve the per-bot separation of queues/settings/recent-post histories; don't
  merge them or share one file across channels.
- This is a public repo. No channel names, handles, user ids, tokens, or personal
  paths in code, tests, docs, or commit messages.
