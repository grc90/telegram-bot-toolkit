"""Create timestamped local backups of runtime JSON state files."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNTIME_JSON_FILES = (
    "imageboard_search_state.json",
    "imageboard_notifier_state.json",
    "examplechannel_queue.json",
    "examplechannel_settings.json",
    "recent_posts_examplechannel.json",
)

DEFAULT_QUEUE_RETENTION_HOURS = 24

_BACKUP_FOLDER_RE = re.compile(r"^state_(\d{8})_(\d{6})(?:_\d+)?$")


def find_latest_backup_for_file(file_path, backup_root: Path | None = None) -> Path | None:
    """Find the most recent backup copy of file_path under backups/<timestamp>/<filename>."""

    try:
        file_path = Path(file_path)
        backup_root = Path(backup_root) if backup_root else file_path.parent / "backups"

        if not backup_root.is_dir():
            return None

        filename = file_path.name
        candidates = sorted(backup_root.iterdir(), key=lambda p: p.name, reverse=True)

        for backup_dir in candidates:
            if not backup_dir.is_dir():
                continue

            candidate = backup_dir / filename

            if candidate.is_file():
                return candidate
    except Exception:
        pass

    return None


def create_backup(destination_root: Path | None = None) -> Path:
    destination_root = destination_root or ROOT / "backups"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = destination_root / f"state_{timestamp}"
    suffix = 1

    # mkdir (not the preceding exists() check) is the only atomic step across
    # processes: multiple bots can race to back up at the same wall-clock second,
    # so an exists()-then-mkdir pattern has a TOCTOU window where two processes
    # both see "free" and one loses with FileExistsError. Retrying on that error
    # instead of pre-checking makes name selection safe under concurrent callers.
    while True:
        try:
            backup_dir.mkdir(parents=True)
            break
        except FileExistsError:
            backup_dir = destination_root / f"state_{timestamp}_{suffix}"
            suffix += 1

    manifest_lines = [
        "Runtime JSON backup",
        f"created_at={datetime.now().isoformat(timespec='seconds')}",
        "",
        "files:",
    ]

    for relative_name in RUNTIME_JSON_FILES:
        source = ROOT / relative_name

        if not source.exists():
            manifest_lines.append(f"- {relative_name}: missing")
            continue

        shutil.copy2(source, backup_dir / relative_name)
        manifest_lines.append(f"- {relative_name}: copied")

    (backup_dir / "MANIFEST.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return backup_dir


def parse_backup_folder_timestamp(folder_name: str) -> datetime | None:
    """Parse the creation timestamp encoded in a ``state_YYYYMMDD_HHMMSS[_N]`` folder name.

    Returns None for any name that does not match the expected pattern, so unrecognized
    folders are never touched by the retention cleanup below.
    """
    match = _BACKUP_FOLDER_RE.match(folder_name)

    if not match:
        return None

    date_part, time_part = match.groups()

    try:
        return datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def find_old_queue_backup_files(
    retention_hours: float = DEFAULT_QUEUE_RETENTION_HOURS,
    backup_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Classify queue-backup files under backup_root into kept/to_delete/ignored_folders.

    Age is decided solely by the timestamp encoded in the folder name
    (``state_YYYYMMDD_HHMMSS``), never by the folder's own filesystem mtime: a
    directory's mtime updates whenever an entry inside it is added or removed, so a
    prior queue-only cleanup run (which deletes files from old folders) would bump
    old folders' mtimes to "now" and make an mtime-based check falsely treat them as
    recent forever after. The name is set once at backup creation time and never
    changes, so it is the only reliable age signal here. Folders whose name does not
    match the expected pattern are never inspected or touched (listed under
    ignored_folders instead). A file only ever becomes a deletion candidate if its
    name ends in ``_queue.json`` (matched by suffix, not by a fixed current-bot-name
    list, so backups made under old bot names before a rename are still recognized as
    queue backups); state/settings/recent-post backups and MANIFEST.txt are never
    touched.
    """
    backup_root = Path(backup_root) if backup_root else ROOT / "backups"
    now = now or datetime.now()
    cutoff = now - timedelta(hours=retention_hours)

    report = {"kept": [], "to_delete": [], "ignored_folders": []}

    if not backup_root.is_dir():
        return report

    for backup_dir in sorted(backup_root.iterdir()):
        if not backup_dir.is_dir():
            continue

        name_timestamp = parse_backup_folder_timestamp(backup_dir.name)

        if name_timestamp is None:
            report["ignored_folders"].append((backup_dir, "nombre de carpeta no reconocido"))
            continue

        within_retention = name_timestamp >= cutoff

        for entry in sorted(backup_dir.iterdir()):
            if not entry.is_file() or not entry.name.endswith("_queue.json"):
                continue

            if within_retention:
                report["kept"].append(entry)
            else:
                report["to_delete"].append(entry)

    return report


def clean_old_queue_backups(
    retention_hours: float = DEFAULT_QUEUE_RETENTION_HOURS,
    backup_root: Path | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> dict:
    """Delete queue-backup files older than retention_hours. Dry-run unless apply=True.

    Never touches *_state.json, *_settings.json, recent_posts_*.json, MANIFEST.txt,
    or any backup folder outside of backups/, regardless of apply. One file failing to
    delete (e.g. a transient OneDrive/AV lock) is recorded under report["failed"] and
    does not stop the rest of the batch.
    """
    report = find_old_queue_backup_files(retention_hours=retention_hours, backup_root=backup_root, now=now)
    report["failed"] = []

    if apply:
        for queue_file in report["to_delete"]:
            try:
                queue_file.unlink()
            except PermissionError:
                # OneDrive Files-On-Demand placeholders mark synced files read-only;
                # clear it and retry once before giving up (see _clear_readonly_and_retry).
                try:
                    os.chmod(queue_file, stat.S_IWRITE)
                    queue_file.unlink()
                except OSError as error:
                    report["failed"].append((queue_file, str(error)))
            except OSError as error:
                report["failed"].append((queue_file, str(error)))

    return report


def find_old_backup_folders(
    retention_hours: float = DEFAULT_QUEUE_RETENTION_HOURS,
    backup_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Classify entire backup folders under backup_root into kept/to_delete/ignored_folders.

    Same name-only age rule as find_old_queue_backup_files (see its docstring for why
    directory mtime is deliberately not used: it gets bumped by our own queue-only
    cleanup deleting files from old folders, which would otherwise make old folders
    look permanently "recent"). Folders whose name does not match the expected
    pattern are never touched. Unlike find_old_queue_backup_files, an entire folder is
    the deletion unit here — including its state/settings/recent-post backups and
    MANIFEST.txt, not just queue files.
    """
    backup_root = Path(backup_root) if backup_root else ROOT / "backups"
    now = now or datetime.now()
    cutoff = now - timedelta(hours=retention_hours)

    report = {"kept": [], "to_delete": [], "ignored_folders": []}

    if not backup_root.is_dir():
        return report

    for backup_dir in sorted(backup_root.iterdir()):
        if not backup_dir.is_dir():
            continue

        name_timestamp = parse_backup_folder_timestamp(backup_dir.name)

        if name_timestamp is None:
            report["ignored_folders"].append((backup_dir, "nombre de carpeta no reconocido"))
            continue

        within_retention = name_timestamp >= cutoff

        if within_retention:
            report["kept"].append(backup_dir)
        else:
            report["to_delete"].append(backup_dir)

    return report


def _clear_readonly_and_retry(func, path, exc_info):
    """shutil.rmtree onexc handler for OneDrive Files-On-Demand placeholders.

    Folders synced by OneDrive commonly carry the ReadOnly + ReparsePoint attributes
    as part of their cloud-placeholder bookkeeping, which makes the plain os.rmdir/
    os.unlink calls inside shutil.rmtree fail with WinError 5 (Access Denied) even
    though the operator plainly owns the file. Clearing the read-only bit and
    retrying the exact failed operation resolves it without shelling out to
    PowerShell's Remove-Item -Force (which does the same thing under the hood).
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def clean_old_backup_folders(
    retention_hours: float = DEFAULT_QUEUE_RETENTION_HOURS,
    backup_root: Path | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> dict:
    """Delete ENTIRE backup folders older than retention_hours. Dry-run unless apply=True.

    Unlike clean_old_queue_backups, this removes the whole backups/state_<timestamp>/
    folder — state/settings/recent-post backups included, not just queue files. Folders
    with an unrecognized name are never touched, regardless of apply. One folder failing
    to delete (e.g. a lock genuinely held by another process) is recorded under
    report["failed"] and does not stop the rest of the batch. Multiple posting bots can
    call this concurrently (each runs its own backup_loop); a folder already removed by
    a concurrent run by the time this one gets to it is treated as a no-op, not a
    failure.
    """
    report = find_old_backup_folders(retention_hours=retention_hours, backup_root=backup_root, now=now)
    report["failed"] = []

    if apply:
        for folder in report["to_delete"]:
            if not folder.exists():
                continue

            # OneDrive Files-On-Demand briefly locks files inside a just-superseded
            # backup folder while it finishes syncing them to the cloud, which makes
            # the first rmtree attempt fail with a transient sharing-violation OSError
            # even though the lock clears a moment later. Retry a few times with a
            # short pause before giving up, instead of surfacing every transient lock
            # as a real failure (this was previously spamming an operator alert on
            # almost every 6h cleanup cycle even though the folder always ended up
            # deleted on a later run).
            last_error = None
            for attempt in range(3):
                if attempt:
                    time.sleep(1)
                try:
                    shutil.rmtree(folder, onexc=_clear_readonly_and_retry)
                    last_error = None
                    break
                except OSError as error:
                    last_error = error
                    if not folder.exists():
                        break

            # A concurrent cleanup run (another bot's backup_loop) can delete entries
            # out from under this rmtree mid-walk, raising OSError even though the
            # folder ends up gone either way. Only a folder that is still present
            # after all retries is a genuine failure.
            if last_error is not None and folder.exists():
                report["failed"].append((folder, str(last_error)))

    return report


def _print_backup_folder_cleanup_report(report: dict, retention_hours: float, applied: bool) -> None:
    mode = "APLICADO" if applied else "DRY-RUN"
    print(f"=== Limpieza de carpetas de backup completas ({mode}, retencion {retention_hours}h) ===\n")

    print(f"Conservadas ({len(report['kept'])} carpeta(s), dentro de la ventana):")
    for folder in report["kept"]:
        print(f"  [KEEP] {folder.relative_to(ROOT)}")

    verb = "Eliminadas" if applied else "Se eliminarian"
    print(f"\n{verb} ({len(report['to_delete'])} carpeta(s), fuera de la ventana):")
    for folder in report["to_delete"]:
        print(f"  [{'DEL' if applied else 'DRY'}] {folder.relative_to(ROOT)}")

    print(f"\nCarpetas ignoradas por seguridad ({len(report['ignored_folders'])}):")
    for folder, reason in report["ignored_folders"]:
        print(f"  [SKIP] {folder.name}: {reason}")

    if report.get("failed"):
        print(f"\nNo se pudieron eliminar ({len(report['failed'])}):")
        for folder, error in report["failed"]:
            print(f"  [ERROR] {folder.relative_to(ROOT)}: {error}")


def _print_queue_cleanup_report(report: dict, retention_hours: float, applied: bool) -> None:
    mode = "APLICADO" if applied else "DRY-RUN"
    print(f"=== Limpieza de backups de queues ({mode}, retencion {retention_hours}h) ===\n")

    print(f"Conservados ({len(report['kept'])} archivo(s) de queue, dentro de la ventana):")
    for queue_file in report["kept"]:
        print(f"  [KEEP] {queue_file.relative_to(ROOT)}")

    verb = "Eliminados" if applied else "Se eliminarian"
    print(f"\n{verb} ({len(report['to_delete'])} archivo(s) de queue, fuera de la ventana):")
    for queue_file in report["to_delete"]:
        print(f"  [{'DEL' if applied else 'DRY'}] {queue_file.relative_to(ROOT)}")

    print(f"\nCarpetas ignoradas por seguridad ({len(report['ignored_folders'])}):")
    for folder, reason in report["ignored_folders"]:
        print(f"  [SKIP] {folder.name}: {reason}")

    if report.get("failed"):
        print(f"\nNo se pudieron eliminar ({len(report['failed'])}):")
        for queue_file, error in report["failed"]:
            print(f"  [ERROR] {queue_file.relative_to(ROOT)}: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up bot runtime JSON files locally.")
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "backups",
        help="Backup root directory. Defaults to ./backups.",
    )
    parser.add_argument(
        "--clean-queue-backups",
        action="store_true",
        help=(
            "Instead of creating a new backup, remove queue-backup files "
            "(*_queue.json inside backups/) older than --retention-hours. "
            "Never touches state/settings/recent-post backups. Dry-run unless --apply is set."
        ),
    )
    parser.add_argument(
        "--clean-old-backups",
        action="store_true",
        help=(
            "Instead of creating a new backup, remove ENTIRE backup folders "
            "(backups/state_<timestamp>/, including state/settings/recent-post copies) "
            "older than --retention-hours. Dry-run unless --apply is set."
        ),
    )
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=DEFAULT_QUEUE_RETENTION_HOURS,
        help="Retention window in hours for --clean-queue-backups / --clean-old-backups (default: 24).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag, --clean-queue-backups/--clean-old-backups only report.",
    )
    args = parser.parse_args()

    if args.clean_old_backups:
        report = clean_old_backup_folders(
            retention_hours=args.retention_hours,
            backup_root=args.destination,
            apply=args.apply,
        )
        _print_backup_folder_cleanup_report(report, args.retention_hours, args.apply)
        return 0

    if args.clean_queue_backups:
        report = clean_old_queue_backups(
            retention_hours=args.retention_hours,
            backup_root=args.destination,
            apply=args.apply,
        )
        _print_queue_cleanup_report(report, args.retention_hours, args.apply)
        return 0

    backup_dir = create_backup(args.destination)
    print(f"Created JSON state backup at: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
