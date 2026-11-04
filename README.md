# Telegram Bot Toolkit

A small framework for running long-lived Telegram bots unattended: a queue-driven
channel posting engine, an imageboard notifier that polls an API and pushes new
matches, and the operational tooling around both (atomic state, deduplication,
backups, a read-only diagnostics CLI, systemd deployment).

Written in Python with two runtime dependencies — `pyTelegramBotAPI` and
`requests`. Everything else is standard library. ~6k lines of source, 194 tests.

> This is a public, anonymized extract of a private workspace. The private
> deployment runs six bots off this same engine; here it ships with one example
> channel and one notifier, so the architecture is visible without the channel
> configuration. Runtime data (`.env`, queues, state, backups) is git-ignored by
> design and never left the private repo.

## Why it exists

Posting to a Telegram channel on a schedule is easy. Doing it for months without
supervision is not — the failure modes are all in the boring parts:

- Telegram rate-limits you, times out mid-upload, or rejects an image for
  dimensions you did not check before spending the upload.
- The process dies while writing a queue file and you lose the queue.
- A transient network blip gets misread as "this post is unsendable" and the
  post is dropped forever — or the reverse, and an unsendable post is retried
  every ten minutes until the heat death of the universe.
- The same image gets reposted because the source URL changed shape slightly.

Most of this codebase is about those cases. The interesting decisions are in
[Design notes](#design-notes) below.

## Architecture

Each bot is an independent process with a stable **internal name** that is
separate from its filename. That name drives its state/queue/settings JSON file
names, its log file, and its `.env` variable prefix — so renaming the entry-point
script does not move any of its data.

### Entry points

| File | Internal name | What it does |
| --- | --- | --- |
| [example_posting_bot.py](example_posting_bot.py) | `examplechannel` | Posts a queue to a channel on a schedule. ~75 lines: a config block and `main()`. |
| [imageboard_notifier_bot.py](imageboard_notifier_bot.py) | `imageboard_notifier` | Polls the Danbooru API, filters by rating/score/age/tag blacklist, pushes new matches to subscribers. A config file over `danbooru_notifier_shared.py`. |
| [imageboard_search_bot.py](imageboard_search_bot.py) | `imageboard_search` | Interactive search: prompts the operator for tag, age, rating and ordering, then delivers the results. |

The posting bot is deliberately thin. All of the engine — queue posting,
scheduling, rate limiting, dry-run, pause/resume, duplicate checks, backup
retention — lives in `posting_bot_shared.py` and is handed over by
`create_posting_bot()`. Running a second channel means copying the config file
and changing two strings.

### Shared modules

| Module | Responsibility |
| --- | --- |
| [posting_bot_shared.py](posting_bot_shared.py) | The posting engine: `PostingBotApp` + `create_posting_bot()` factory. Queue, scheduling, rate limiting, commands, dry-run, backup loop. |
| [danbooru_notifier_shared.py](danbooru_notifier_shared.py) | The notifier engine: `DanbooruNotifierApp` + `create_notifier_bot()` factory. Fetch and filters, photo delivery, handled-post state, polling cycle, commands. |
| [json_storage.py](json_storage.py) | Atomic JSON read/write — temp file in the same directory, `fsync`, `os.replace` with retries. Raises `MalformedJsonError` / `JsonTypeError` instead of silently defaulting. |
| [queue_dedupe.py](queue_dedupe.py) | Deterministic `post_signature()` per post: normalized source URL first, then Telegram `file_id` / `media_group_id`, then a stable JSON fallback. |
| [recent_post_guard.py](recent_post_guard.py) | Per-channel repost history with a lock file around read-prune-write, so two bot processes cannot race. |
| [danbooru_client.py](danbooru_client.py) | HTTP against the Danbooru API: bounded retries on 429/5xx (honoring `Retry-After`), response validation, size-capped download into memory, and the photo-variant policy shared by the notifier and search. |
| [telegram_common.py](telegram_common.py) | Telegram error classification (transient vs definitive, `retry_after`) shared by every bot. |
| [tag_blacklist.py](tag_blacklist.py) | Wildcard (`*_suffix`) tag filter shared by the imageboard bots. |
| [config_env.py](config_env.py) | Dependency-free `.env` loader with `get_required_env` / `get_int_env` / `validate_telegram_token`. |
| [backup_state.py](backup_state.py) | Timestamped runtime-JSON backups plus retention cleanup (dry-run unless `--apply`). |
| [bot_logging.py](bot_logging.py) | Rotating-file + console logger, one file per bot. |
| [dev.py](dev.py) | Read-only diagnostics CLI. Never calls Telegram, never mutates state. |

## Design notes

The parts that were not obvious up front.

**Sends return a tri-state, not a bool.** `send_post_to_chat()` returns
`SENT` / `SKIPPED` / `FAILED`, and that distinction is the whole retry policy.
`SKIPPED` means "no variant of this image will ever satisfy Telegram, stop
trying"; `FAILED` means "transient, try again next cycle". Both `SENT` and
`SKIPPED` mark the post handled. Collapsing them into a boolean produces either
an infinite retry loop on an unsendable post, or a post dropped forever after
one network blip — depending on which way you collapse it.

**Image variants are pre-screened, not attempted.** Telegram rejects photos over
10 MB, with width+height above 10000, or an aspect ratio past 20:1. The API
publishes dimensions per variant, so `photo_variant_rejection()` checks them
before spending a download and an upload. Missing metadata means "try anyway"
rather than "skip" — the check is an optimization, not a gate.

**Retries rewind the upload stream.** A failed upload leaves the `BytesIO` at
EOF. Retrying with the same object uploads zero bytes and Telegram answers
`400 Bad Request: file must be non-empty`, which looks like a corrupt-file bug
and is not one. `try_telegram_detailed()` calls `rewind_media_arguments()` before
every retry; [tests/test_notifier_send_retry.py](tests/test_notifier_send_retry.py)
is there to keep it that way.

**Two Telegram wrappers, on purpose.** `call_telegram()` is for command replies:
it honors a per-bot API cooldown and returns `None` without calling while
paused. `try_telegram()` is for media sends: bounded retry with backoff. They
look interchangeable and are not — using the cooldown-aware one for a media send
silently drops posts during an outage.

**Polling pages back only to the last complete cycle.** The notifier keeps an
`updated_cursor` in its state and stops paginating `order:updated` results once
a page reaches posts older than it, so a normal cycle is one request instead of
ten. The cursor only advances when nothing failed transiently: otherwise a
failed post could fall outside the next cycle's pages and be lost. The query
also carries `age:<=Nd`, because `order:updated` resurfaces old posts whenever
someone re-tags them, long after their id left the handled-id history.

**Backup age comes from the folder name, not `mtime`.** Deleting files inside a
backup folder bumps its `mtime`, so `mtime`-based retention makes old folders
look permanently fresh and they are never collected. The timestamp is parsed out
of `state_YYYYMMDD_HHMMSS` instead.

**Corrupt state restores from backup and alerts.** A malformed state file does
not silently reset to defaults — it is restored from the newest usable backup,
the repaired state is written back, and the operator gets a message naming the
backup that was used. Silently defaulting on a notifier's state file means
re-notifying every post it has ever seen.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in tokens and ids
```

Run a bot:

```bash
python example_posting_bot.py
python imageboard_notifier_bot.py
python imageboard_search_bot.py
```

Preview what the posting bot would publish, without sending anything, touching
the queue, or updating the repost history:

```bash
python example_posting_bot.py --dry-run --dry-run-count 1
```

Preview the notifier's fetch + filter pass without writing state:

```bash
python imageboard_notifier_bot.py --dry-run
```

Inspect runtime data locally (read-only — no Telegram calls, no writes):

```bash
python dev.py health
python dev.py check-env examplechannel
python dev.py queue examplechannel --preview 5
python dev.py filter-test imageboard_notifier --tag ai-generated
python dev.py recent examplechannel --limit 10
python dev.py stats imageboard_notifier --lines 500
```

Back up runtime JSON before any migration:

```bash
python backup_state.py
```

## Telegram commands

Posting bot: `/help` `/status` `/queue` `/post_now` `/delete` `/pause` `/resume` `/stop`
`/special` `/done` `/backup` `/posts_per_batch`

Notifier: `/start` `/help` `/status` `/check` `/top` `/hot` `/blacklist` `/test`
`/stop` `/unsubscribe`

Two lookalike pairs are different on purpose. On the posting bot, `/pause` halts
scheduled publishing until `/resume`, while `/stop` only interrupts the batch
currently in flight and lets the next scheduled run proceed. On the notifier,
`/stop` is deliberately a no-op that points at `/unsubscribe` — a user typing
the reflexive `/stop` should not silently lose their subscription.

Every handler checks the sender against `BOTS_ALLOWED_USER_ID` before doing
anything, so the bots only answer their operator.

## Tests

```bash
python -m pytest -q
```

301 tests, fully synchronous, no network. Coverage is weighted toward the failure
paths rather than the happy path: corrupt state recovery, atomic-write retries,
concurrent recent-post access, rate limiting, dry-run isolation, and the photo
delivery invariants. [tests/test_notifier_photo_only.py](tests/test_notifier_photo_only.py)
asserts against the notifier engine's source, so reintroducing a `send_document` fallback or a
thumbnail URL fails the suite rather than quietly changing what subscribers get.

## Deployment

[deploy/](deploy/) has what is needed to run the bots unattended on a VPS: a
systemd template unit (one instance per entry point) and a bootstrap script that
creates the virtualenv and enables the units.
[docs/operacion.md](docs/operacion.md) is the day-to-day operations reference
(in Spanish): command listing, diagnostics, recovery steps.

One deployment gotcha worth stating up front: Telegram rejects two pollers on
the same token with a `409 Conflict`, so a bot has to be stopped locally before
the server copy is started.

## License

MIT — see [LICENSE](LICENSE).
