"""CLI de inspección y diagnóstico local para el sistema de bots.

Todas las operaciones son de SOLO LECTURA sobre datos runtime (queues, state, recent).
No realiza ninguna llamada a Telegram ni modifica archivos de estado.

Uso:
    python dev.py health
    python dev.py queue examplechannel
    python dev.py queue examplechannel --preview 5
    python dev.py queue examplechannel --search "danbooru.donmai.us/posts/123"
    python dev.py filter-test imageboard_notifier --tag ai-generated
    python dev.py filter-test imageboard_notifier --tag "sample_watermark"
    python dev.py check-env imageboard_notifier
    python dev.py check-env examplechannel
    python dev.py recent examplechannel --limit 10
    python dev.py stats imageboard_notifier
    python dev.py stats examplechannel --lines 500
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Bot metadata
# ---------------------------------------------------------------------------

# Cada bot desplegado agrega una entrada acá. El repo público trae un solo
# canal de ejemplo; un despliegue real corre N entradas del mismo tipo.
DANBOORU_BOTS = {"imageboard_notifier"}

POSTING_BOTS = {"examplechannel"}

ALL_BOTS = DANBOORU_BOTS | POSTING_BOTS | {"imageboard_search"}

QUEUE_FILES = {
    "examplechannel": ROOT / "examplechannel_queue.json",
}

STATE_FILES = {
    "imageboard_notifier": ROOT / "imageboard_notifier_state.json",
}

RECENT_FILES = {
    "examplechannel": ROOT / "recent_posts_examplechannel.json",
}

RUNTIME_FILES = {
    "imageboard_notifier_state.json",
    "examplechannel_queue.json",
    "examplechannel_settings.json",
    "recent_posts_examplechannel.json",
}

REQUIRED_ENV = {
    "imageboard_notifier": [
        "IMAGEBOARD_NOTIFIER_TELEGRAM_TOKEN",
        "BOTS_ALLOWED_USER_ID",
    ],
    "imageboard_search": [
        "IMAGEBOARD_SEARCH_TELEGRAM_TOKEN",
        "BOTS_ALLOWED_USER_ID",
    ],
    "examplechannel": [
        "EXAMPLECHANNEL_TELEGRAM_TOKEN",
        "EXAMPLECHANNEL_CHANNEL_ID",
        "BOTS_ALLOWED_USER_ID",
    ],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path):
    """Return parsed JSON or None on error; never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _load_env():
    """Load .env file values into os.environ without overriding existing vars."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


def _ok(msg):
    print(f"  [OK]   {msg}")


def _warn(msg):
    print(f"  [WARN] {msg}")


def _err(msg):
    print(f"  [ERR]  {msg}")


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def cmd_health(args):
    """Check that all runtime files exist and are valid JSON."""
    print("=== Health check ===\n")
    any_error = False

    print("Runtime files:")
    for name in sorted(RUNTIME_FILES):
        path = ROOT / name
        if not path.exists():
            _warn(f"{name}: not found (may be normal if bot never ran)")
        else:
            data = _load_json(path)
            if data is None:
                _err(f"{name}: exists but could not be parsed as JSON")
                any_error = True
            else:
                item_count = len(data) if isinstance(data, (list, dict)) else "?"
                _ok(f"{name}: OK ({item_count} items)")

    print()
    log_dir = ROOT / "logs"
    print("Log files:")
    if not log_dir.exists():
        _warn("logs/ directory not found — no bot has started yet")
    else:
        for bot in sorted(ALL_BOTS):
            log_file = log_dir / f"{bot}.log"
            if log_file.exists():
                size_kb = log_file.stat().st_size // 1024
                mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
                _ok(f"{bot}.log: {size_kb} KB, last modified {mtime.strftime('%Y-%m-%d %H:%M')}")
            else:
                _warn(f"{bot}.log: not found (bot may not have started)")

    print()
    env_path = ROOT / ".env"
    if env_path.exists():
        _ok(".env file found")
    else:
        _err(".env file missing — bots will fail to start")
        any_error = True

    print()
    if any_error:
        print("Health check FAILED — see [ERR] entries above.")
        return 1
    else:
        print("Health check OK.")
        return 0


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def cmd_queue(args):
    bot = args.bot.lower()
    if bot not in QUEUE_FILES:
        print(f"Unknown bot '{bot}'. Queue bots: {', '.join(sorted(QUEUE_FILES))}", file=sys.stderr)
        return 1

    path = QUEUE_FILES[bot]
    if not path.exists():
        print(f"Queue file not found: {path}")
        return 0

    data = _load_json(path)
    if data is None:
        print(f"ERROR: could not parse {path}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print(f"ERROR: queue file is not a list (got {type(data).__name__})", file=sys.stderr)
        return 1

    print(f"=== Queue: {bot} ===")
    print(f"Total items: {len(data)}\n")

    if args.search:
        term = args.search.lower()
        matches = []
        for i, item in enumerate(data):
            searchable = json.dumps(item).lower()
            if term in searchable:
                matches.append((i, item))
        print(f"Search '{args.search}': {len(matches)} match(es)\n")
        for idx, item in matches[:20]:
            _print_post_summary(idx, item)
        return 0

    limit = args.preview if args.preview else (5 if not args.count else 0)

    if args.count:
        print(f"Count: {len(data)}")
        return 0

    posts_to_show = data[:limit]
    for i, item in enumerate(posts_to_show):
        _print_post_summary(i, item)

    if len(data) > limit:
        print(f"  ... and {len(data) - limit} more")

    return 0


def _print_post_summary(index, post):
    if not isinstance(post, dict):
        print(f"  [{index}] <malformed: {type(post).__name__}>")
        return

    post_type = post.get("type", "?")
    source_url = post.get("source_url", post.get("caption", ""))
    file_id = post.get("file_id", "")
    created_at = post.get("created_at")
    age = ""
    if created_at:
        try:
            age_s = int(datetime.now(timezone.utc).timestamp() - float(created_at))
            if age_s < 3600:
                age = f" ({age_s // 60}m ago)"
            elif age_s < 86400:
                age = f" ({age_s // 3600}h ago)"
            else:
                age = f" ({age_s // 86400}d ago)"
        except (TypeError, ValueError):
            pass

    display = source_url or file_id or "<no url>"
    if len(display) > 80:
        display = display[:77] + "..."
    print(f"  [{index}] {post_type}{age}: {display}")


# ---------------------------------------------------------------------------
# filter-test
# ---------------------------------------------------------------------------

def cmd_filter_test(args):
    bot = args.bot.lower()
    if bot not in DANBOORU_BOTS:
        print(f"filter-test only applies to Danbooru bots: {', '.join(sorted(DANBOORU_BOTS))}", file=sys.stderr)
        return 1

    # Import blacklist — doesn't need env vars
    try:
        from tag_blacklist import BLACKLISTED_TAGS
    except ImportError as e:
        print(f"ERROR importing tag_blacklist: {e}", file=sys.stderr)
        return 1

    tag = (args.tag or "").strip().lower()
    if not tag:
        print("Provide a tag with --tag <tag>", file=sys.stderr)
        return 1

    print(f"=== Filter test: bot={bot}, tag='{tag}' ===\n")

    matched = [blocked for blocked in BLACKLISTED_TAGS if fnmatch.fnmatchcase(tag, blocked)]
    if matched:
        print(f"BLOCKED — matched by: {', '.join(sorted(matched))}")
    else:
        print(f"NOT BLOCKED — '{tag}' does not match any of the {len(BLACKLISTED_TAGS)} blacklist patterns.")

    # Also load score threshold from env (optional, won't crash if .env missing)
    _load_env()
    threshold = int(os.environ.get("IMAGEBOARD_NOTIFIER_SCORE_THRESHOLD", "10"))
    print(f"\nActive score threshold: {threshold} (IMAGEBOARD_NOTIFIER_SCORE_THRESHOLD)")
    print("Active ratings: g")

    return 0


# ---------------------------------------------------------------------------
# check-env
# ---------------------------------------------------------------------------

def cmd_check_env(args):
    bot = args.bot.lower()
    if bot not in REQUIRED_ENV:
        print(f"Unknown bot '{bot}'. Known bots: {', '.join(sorted(REQUIRED_ENV))}", file=sys.stderr)
        return 1

    print(f"=== Env check: {bot} ===\n")
    _load_env()

    any_error = False
    token_re = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{35,}$")

    for var in REQUIRED_ENV[bot]:
        value = os.environ.get(var, "")
        if not value:
            _err(f"{var}: MISSING")
            any_error = True
        elif "TOKEN" in var and not token_re.match(value):
            _warn(f"{var}: present but format looks wrong (expected <digits>:<35+ chars>)")
        else:
            masked = value[:6] + "..." if "TOKEN" in var else value
            _ok(f"{var}: {masked}")

    print()
    if any_error:
        print("FAIL — set missing variables in .env before starting the bot.")
        return 1
    else:
        print("OK — all required variables are set.")
        return 0


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------

def cmd_recent(args):
    bot = args.bot.lower()
    if bot not in RECENT_FILES:
        print(f"Unknown bot '{bot}'. Recent-history bots: {', '.join(sorted(RECENT_FILES))}", file=sys.stderr)
        return 1

    path = RECENT_FILES[bot]
    if not path.exists():
        print(f"No recent history file for {bot} (not found: {path})")
        return 0

    data = _load_json(path)
    if data is None:
        print(f"ERROR: could not parse {path}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print(f"ERROR: recent file is not a list (got {type(data).__name__})", file=sys.stderr)
        return 1

    limit = args.limit or 10
    entries = sorted(data, key=lambda e: e.get("recorded_at", 0) if isinstance(e, dict) else 0, reverse=True)

    print(f"=== Recent posts: {bot} (last {min(limit, len(entries))} of {len(entries)}) ===\n")

    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        recorded_at = entry.get("recorded_at")
        post_type = entry.get("post_type", "?")
        sig = str(entry.get("signature", ""))[:60]
        age = ""
        if recorded_at:
            try:
                age_s = int(datetime.now(timezone.utc).timestamp() - float(recorded_at))
                if age_s < 3600:
                    age = f"{age_s // 60}m ago"
                elif age_s < 86400:
                    age = f"{age_s // 3600}h ago"
                else:
                    age = f"{age_s // 86400}d ago"
            except (TypeError, ValueError):
                age = "?"
        print(f"  {age:12} | {post_type:10} | {sig}")

    return 0


# ---------------------------------------------------------------------------
# stats (log parser)
# ---------------------------------------------------------------------------

_LOG_PATTERNS = {
    "published": re.compile(r"Published (scheduled|special) \w+ post", re.IGNORECASE),
    "sent_danbooru": re.compile(r"sent Danbooru post \d+", re.IGNORECASE),
    "error": re.compile(r"\b(ERROR|failed|exception|Traceback)\b", re.IGNORECASE),
    "polling_error": re.compile(r"Polling error", re.IGNORECASE),
    "api_pause": re.compile(r"Pausing sends for", re.IGNORECASE),
    "skipped_duplicate": re.compile(r"Skipped duplicate", re.IGNORECASE),
    "skipped_recent": re.compile(r"Skipped.*recently", re.IGNORECASE),
    "bootstrapped": re.compile(r"bootstrapped \d+ current matching", re.IGNORECASE),
    "dry_run": re.compile(r"DRY RUN", re.IGNORECASE),
}


def cmd_stats(args):
    bot = args.bot.lower()
    log_dir = ROOT / "logs"
    log_file = log_dir / f"{bot}.log"

    if not log_file.exists():
        print(f"Log file not found: {log_file}")
        return 0

    limit = args.lines or 1000
    print(f"=== Stats: {bot} (last {limit} log lines) ===\n")

    try:
        with open(log_file, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as e:
        print(f"ERROR reading log: {e}", file=sys.stderr)
        return 1

    recent_lines = lines[-limit:]
    counts = {key: 0 for key in _LOG_PATTERNS}
    last_timestamps = []

    for line in recent_lines:
        for key, pattern in _LOG_PATTERNS.items():
            if pattern.search(line):
                counts[key] += 1

        # Capture timestamps from lines that look like log entries
        ts_match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if ts_match:
            last_timestamps.append(ts_match.group(1))

    if last_timestamps:
        print(f"  Log range: {last_timestamps[0]} → {last_timestamps[-1]}")
        print(f"  Lines analyzed: {len(recent_lines)}\n")

    labels = {
        "published": "Posts published (posting bots)",
        "sent_danbooru": "Posts sent (Danbooru bots)",
        "skipped_duplicate": "Duplicates skipped",
        "skipped_recent": "Skipped (published recently)",
        "api_pause": "API cooldowns triggered",
        "polling_error": "Polling errors",
        "error": "Error lines (total)",
        "bootstrapped": "Bootstrap events",
        "dry_run": "Dry-run invocations",
    }

    for key, label in labels.items():
        count = counts[key]
        if count > 0:
            print(f"  {count:5}  {label}")

    if all(v == 0 for v in counts.values()):
        print("  (no recognizable events found in this log range)")

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local inspection CLI for the bot system. All operations are read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # health
    sub.add_parser("health", help="Check runtime files, logs, and .env")

    # queue
    p_queue = sub.add_parser("queue", help="Inspect a posting-bot queue")
    p_queue.add_argument("bot", help="Posting bot name, e.g. examplechannel")
    p_queue.add_argument("--count", action="store_true", help="Show only count")
    p_queue.add_argument("--preview", type=int, metavar="N", help="Show first N items")
    p_queue.add_argument("--search", metavar="TERM", help="Search items containing TERM")

    # filter-test
    p_filter = sub.add_parser("filter-test", help="Test if a tag/post would be filtered")
    p_filter.add_argument("bot", help="Bot name: imageboard_notifier")
    p_filter.add_argument("--tag", metavar="TAG", help="Tag name to test against blacklist")

    # check-env
    p_env = sub.add_parser("check-env", help="Validate required env vars for a bot")
    p_env.add_argument("bot", help="Bot name")

    # recent
    p_recent = sub.add_parser("recent", help="Show recent post history for a posting bot")
    p_recent.add_argument("bot", help="Posting bot name, e.g. examplechannel")
    p_recent.add_argument("--limit", type=int, default=10, metavar="N", help="Number of entries to show")

    # stats
    p_stats = sub.add_parser("stats", help="Parse bot log and show event summary")
    p_stats.add_argument("bot", help="Bot name")
    p_stats.add_argument("--lines", type=int, default=1000, metavar="N", help="Analyze last N log lines")

    args = parser.parse_args(argv)

    dispatch = {
        "health": cmd_health,
        "queue": cmd_queue,
        "filter-test": cmd_filter_test,
        "check-env": cmd_check_env,
        "recent": cmd_recent,
        "stats": cmd_stats,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
