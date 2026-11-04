"""Tests for the queue-backup retention cleanup in backup_state.py."""

from datetime import datetime, timedelta
from pathlib import Path

import backup_state


def _make_backup_folder(root, name, files, mtime=None):
    folder = root / name
    folder.mkdir(parents=True)

    for filename, content in files.items():
        (folder / filename).write_text(content, encoding="utf-8")

    if mtime is not None:
        stamp = mtime.timestamp()
        import os

        os.utime(folder, (stamp, stamp))

    return folder


def test_parse_backup_folder_timestamp_valid():
    assert backup_state.parse_backup_folder_timestamp("state_20260705_171926") == datetime(2026, 7, 5, 17, 19, 26)


def test_parse_backup_folder_timestamp_valid_with_suffix():
    assert backup_state.parse_backup_folder_timestamp("state_20260705_171926_1") == datetime(2026, 7, 5, 17, 19, 26)


def test_parse_backup_folder_timestamp_unrecognized_returns_none():
    assert backup_state.parse_backup_folder_timestamp("not_a_backup_folder") is None
    assert backup_state.parse_backup_folder_timestamp("state_bad_format") is None


def test_recent_folder_is_kept(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    recent_name = f"state_{(now - timedelta(hours=1)).strftime('%Y%m%d_%H%M%S')}"
    _make_backup_folder(
        tmp_path,
        recent_name,
        {"examplechannel_queue.json": "[]", "imageboard_notifier_state.json": "{}"},
        mtime=now - timedelta(hours=1),
    )

    report = backup_state.find_old_queue_backup_files(retention_hours=24, backup_root=tmp_path, now=now)

    assert len(report["kept"]) == 1
    assert report["kept"][0].name == "examplechannel_queue.json"
    assert report["to_delete"] == []


def test_old_folder_queue_marked_for_deletion_but_state_is_never_touched(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    _make_backup_folder(
        tmp_path,
        old_name,
        {
            "examplechannel_queue.json": "[]",
            "imageboard_notifier_state.json": "{}",
            "examplechannel_settings.json": "{}",
            "recent_posts_examplechannel.json": "[]",
            "MANIFEST.txt": "manifest",
        },
        mtime=now - timedelta(hours=48),
    )

    report = backup_state.find_old_queue_backup_files(retention_hours=24, backup_root=tmp_path, now=now)

    assert [f.name for f in report["to_delete"]] == ["examplechannel_queue.json"]
    # Nada mas que termine en _queue.json debe aparecer en to_delete ni kept.
    non_queue_names = {"imageboard_notifier_state.json", "examplechannel_settings.json",
                        "recent_posts_examplechannel.json", "MANIFEST.txt"}
    touched_names = {f.name for f in report["to_delete"]} | {f.name for f in report["kept"]}
    assert not (touched_names & non_queue_names)


def test_old_name_wins_over_recent_mtime(tmp_path):
    """El nombre manda aunque el mtime de la carpeta sea reciente (ej. porque una
    limpieza previa borro un archivo de adentro y eso actualizo el mtime de la
    carpeta). Usar mtime aca haria que una carpeta vieja parezca "reciente" para
    siempre despues de la primera limpieza — por eso la edad se decide solo por
    el nombre, nunca por el mtime."""
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    _make_backup_folder(
        tmp_path,
        old_name,
        {"examplechannel_queue.json": "[]"},
        mtime=now - timedelta(hours=1),
    )

    report = backup_state.find_old_queue_backup_files(retention_hours=24, backup_root=tmp_path, now=now)

    assert len(report["to_delete"]) == 1
    assert report["kept"] == []


def test_unrecognized_folder_name_is_ignored_not_deleted(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    _make_backup_folder(
        tmp_path,
        "some_other_folder",
        {"examplechannel_queue.json": "[]"},
        mtime=now - timedelta(hours=100),
    )

    report = backup_state.find_old_queue_backup_files(retention_hours=24, backup_root=tmp_path, now=now)

    assert report["to_delete"] == []
    assert report["kept"] == []
    assert len(report["ignored_folders"]) == 1
    assert report["ignored_folders"][0][0].name == "some_other_folder"


def test_old_bot_naming_before_a_rename_is_still_detected_as_queue(tmp_path):
    """Backups creados antes de un rename de bots (ej. oldname_queue.json) siguen siendo
    reconocidos como backups de queue por el sufijo _queue.json, no por una lista fija
    de nombres de bots actuales."""
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    _make_backup_folder(
        tmp_path,
        old_name,
        {"oldname_queue.json": "[]", "renamedbot_queue.json": "[]"},
        mtime=now - timedelta(hours=48),
    )

    report = backup_state.find_old_queue_backup_files(retention_hours=24, backup_root=tmp_path, now=now)

    assert {f.name for f in report["to_delete"]} == {"oldname_queue.json", "renamedbot_queue.json"}


def test_clean_old_queue_backups_dry_run_does_not_delete(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        old_name,
        {"examplechannel_queue.json": "[]"},
        mtime=now - timedelta(hours=48),
    )

    report = backup_state.clean_old_queue_backups(retention_hours=24, backup_root=tmp_path, apply=False, now=now)

    assert len(report["to_delete"]) == 1
    assert (folder / "examplechannel_queue.json").exists()


def test_clean_old_queue_backups_apply_deletes_only_queue_files(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        old_name,
        {
            "examplechannel_queue.json": "[]",
            "imageboard_notifier_state.json": "{}",
            "MANIFEST.txt": "manifest",
        },
        mtime=now - timedelta(hours=48),
    )

    backup_state.clean_old_queue_backups(retention_hours=24, backup_root=tmp_path, apply=True, now=now)

    assert not (folder / "examplechannel_queue.json").exists()
    assert (folder / "imageboard_notifier_state.json").exists()
    assert (folder / "MANIFEST.txt").exists()


def test_missing_backup_root_returns_empty_report(tmp_path):
    report = backup_state.find_old_queue_backup_files(backup_root=tmp_path / "does_not_exist")

    assert report == {"kept": [], "to_delete": [], "ignored_folders": []}


# ---------------------------------------------------------------------------
# Whole-folder retention (find_old_backup_folders / clean_old_backup_folders)
# ---------------------------------------------------------------------------

def test_find_old_backup_folders_keeps_recent_folder_entirely(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    recent_name = f"state_{(now - timedelta(hours=1)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        recent_name,
        {"examplechannel_queue.json": "[]", "imageboard_notifier_state.json": "{}"},
        mtime=now - timedelta(hours=1),
    )

    report = backup_state.find_old_backup_folders(retention_hours=24, backup_root=tmp_path, now=now)

    assert report["kept"] == [folder]
    assert report["to_delete"] == []


def test_find_old_backup_folders_marks_whole_old_folder(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        old_name,
        {
            "examplechannel_queue.json": "[]",
            "imageboard_notifier_state.json": "{}",
            "recent_posts_examplechannel.json": "[]",
            "MANIFEST.txt": "manifest",
        },
        mtime=now - timedelta(hours=48),
    )

    report = backup_state.find_old_backup_folders(retention_hours=24, backup_root=tmp_path, now=now)

    assert report["to_delete"] == [folder]
    assert report["kept"] == []


def test_find_old_backup_folders_old_name_wins_over_recent_mtime(tmp_path):
    """Ver test_old_name_wins_over_recent_mtime: una limpieza previa de queues pudo
    haber actualizado el mtime de esta carpeta al borrar un archivo de adentro; el
    nombre sigue siendo la unica fuente de verdad para la edad de la carpeta."""
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        old_name,
        {"examplechannel_queue.json": "[]"},
        mtime=now - timedelta(hours=1),
    )

    report = backup_state.find_old_backup_folders(retention_hours=24, backup_root=tmp_path, now=now)

    assert report["to_delete"] == [folder]
    assert report["kept"] == []


def test_find_old_backup_folders_unrecognized_name_is_ignored(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    folder = _make_backup_folder(
        tmp_path,
        "some_other_folder",
        {"examplechannel_queue.json": "[]"},
        mtime=now - timedelta(hours=100),
    )

    report = backup_state.find_old_backup_folders(retention_hours=24, backup_root=tmp_path, now=now)

    assert report["kept"] == []
    assert report["to_delete"] == []
    assert report["ignored_folders"] == [(folder, "nombre de carpeta no reconocido")]


def test_clean_old_backup_folders_dry_run_does_not_delete(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        old_name,
        {"examplechannel_queue.json": "[]", "imageboard_notifier_state.json": "{}"},
        mtime=now - timedelta(hours=48),
    )

    report = backup_state.clean_old_backup_folders(retention_hours=24, backup_root=tmp_path, apply=False, now=now)

    assert report["to_delete"] == [folder]
    assert folder.exists()
    assert (folder / "imageboard_notifier_state.json").exists()


def test_clean_old_backup_folders_apply_removes_entire_folder(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    folder = _make_backup_folder(
        tmp_path,
        old_name,
        {
            "examplechannel_queue.json": "[]",
            "imageboard_notifier_state.json": "{}",
            "recent_posts_examplechannel.json": "[]",
            "MANIFEST.txt": "manifest",
        },
        mtime=now - timedelta(hours=48),
    )

    backup_state.clean_old_backup_folders(retention_hours=24, backup_root=tmp_path, apply=True, now=now)

    assert not folder.exists()


def test_clean_old_backup_folders_never_removes_unrecognized_folder(tmp_path):
    now = datetime(2026, 7, 5, 18, 0, 0)
    folder = _make_backup_folder(
        tmp_path,
        "some_other_folder",
        {"examplechannel_queue.json": "[]"},
        mtime=now - timedelta(hours=100),
    )

    backup_state.clean_old_backup_folders(retention_hours=24, backup_root=tmp_path, apply=True, now=now)

    assert folder.exists()


def test_missing_backup_root_returns_empty_report_for_folders(tmp_path):
    report = backup_state.find_old_backup_folders(backup_root=tmp_path / "does_not_exist")

    assert report == {"kept": [], "to_delete": [], "ignored_folders": []}


# ---------------------------------------------------------------------------
# One folder/file failing to delete must not abort the rest of the batch
# (regression test for the real PermissionError hit against a OneDrive-synced
# backups/ folder, where shutil.rmtree aborted the whole run on the first item).
# ---------------------------------------------------------------------------

def test_clean_old_backup_folders_one_failure_does_not_block_the_rest(tmp_path, monkeypatch):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name_a = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    old_name_b = f"state_{(now - timedelta(hours=72)).strftime('%Y%m%d_%H%M%S')}_1"
    folder_a = _make_backup_folder(tmp_path, old_name_a, {"examplechannel_queue.json": "[]"}, mtime=now)
    folder_b = _make_backup_folder(tmp_path, old_name_b, {"examplechannel_queue.json": "[]"}, mtime=now)

    real_rmtree = backup_state.shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        if Path(path) == folder_a:
            raise PermissionError("[WinError 5] Acceso denegado (simulated OneDrive lock)")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(backup_state.shutil, "rmtree", flaky_rmtree)

    report = backup_state.clean_old_backup_folders(retention_hours=24, backup_root=tmp_path, apply=True, now=now)

    assert folder_a.exists()  # failed deletion: left untouched, not silently lost
    assert not folder_b.exists()  # the other folder is still removed
    assert len(report["failed"]) == 1
    assert report["failed"][0][0] == folder_a


def test_clean_old_queue_backups_one_failure_does_not_block_the_rest(tmp_path, monkeypatch):
    now = datetime(2026, 7, 5, 18, 0, 0)
    old_name_a = f"state_{(now - timedelta(hours=48)).strftime('%Y%m%d_%H%M%S')}"
    old_name_b = f"state_{(now - timedelta(hours=72)).strftime('%Y%m%d_%H%M%S')}_1"
    folder_a = _make_backup_folder(tmp_path, old_name_a, {"examplechannel_queue.json": "[]"}, mtime=now)
    folder_b = _make_backup_folder(tmp_path, old_name_b, {"examplechannel_queue.json": "[]"}, mtime=now)

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == folder_a / "examplechannel_queue.json":
            raise PermissionError("[WinError 5] Acceso denegado (simulated lock)")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    report = backup_state.clean_old_queue_backups(retention_hours=24, backup_root=tmp_path, apply=True, now=now)

    assert (folder_a / "examplechannel_queue.json").exists()  # failed deletion: left untouched
    assert not (folder_b / "examplechannel_queue.json").exists()  # the other file is still removed
    assert len(report["failed"]) == 1
    assert report["failed"][0][0] == folder_a / "examplechannel_queue.json"


def test_latest_backup_age_seconds_uses_newest_folder_name(tmp_path):
    from datetime import datetime

    from backup_state import latest_backup_age_seconds

    for name in ("state_20260101_000000", "state_20260101_060000_1", "not_a_backup", "state_20260101_030000"):
        (tmp_path / name).mkdir()

    now = datetime(2026, 1, 1, 7, 0, 0)

    assert latest_backup_age_seconds(tmp_path, now=now) == 3600
    assert latest_backup_age_seconds(tmp_path / "missing", now=now) is None
