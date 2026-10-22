"""tools/reports.py on made-up reports: reviews decide the status of provisional fixes, sync
sends them to the site (here: the script the pod would run, on a local reports database)."""

import importlib.util
import json
import os
import subprocess
import sys

import pytest

from reports import ReportStore

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("reports_tool", os.path.join(HERE, "..", "..", "tools", "reports.py"))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def report(id, status=None, **fields):
    r = dict(id=id, created_at=f"2026-10-20T10:00:0{id}Z", song="cancion_de_prueba", start_ms=9300, end_ms=10100,
             problem="timing", fixed_start_ms=9000, fixed_end_ms=10100, query=None, line=None, db_version=None,
             first_tok=7, last_tok=8, first_word="sale", last_word="sobre", status=status, reviewed_at=None)
    r.update(fields)
    return r


@pytest.fixture
def files(tmp_path):
    rows = [report(1, "provisional"), report(2, "provisional"), report(3), report(4, "provisional")]
    export = tmp_path / "reports.json"
    export.write_text(json.dumps(rows))
    return export, tmp_path / "reports-reviewed.json", tmp_path / "timing_fixes.json"


def answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def test_review_decides_the_status(files, monkeypatch, capsys):
    export, reviewed, fixes_file = files
    rows = tool.load(export)
    # #1 accepted, #2 nothing wrong, #3 (not applied) nothing wrong, #4 left for later
    answers(monkeypatch, "a", "", "n", "", "n", "", "q")
    tool.review(rows, str(reviewed), db="/nonexistent", fixes_path=str(fixes_file))
    decided = json.loads(reviewed.read_text())
    assert [decided[tool.report_key(r)]["status"] for r in rows[:3]] == ["accepted", "revoked", None]
    assert tool.report_key(rows[3]) not in decided
    # the accepted one goes into timing_fixes.json with the words the server stored
    fix, = json.loads(fixes_file.read_text())
    assert (fix["first"], fix["last"], fix["first_word"], fix["last_word"], fix["start"]) == (7, 8, "sale", "sobre", 9.0)
    assert "end" not in fix
    out = capsys.readouterr().out
    assert "7-8 in the alignment: sale … sobre" in out and "tools/reports.py sync" in out


def test_re_review_drops_the_fix(files, monkeypatch):
    export, reviewed, fixes_file = files
    rows = tool.load(export)
    answers(monkeypatch, "a", "", "q")
    tool.review(rows, str(reviewed), db="/nonexistent", fixes_path=str(fixes_file))
    answers(monkeypatch, "n", "")
    tool.review(rows, str(reviewed), ids=[1], db="/nonexistent", fixes_path=str(fixes_file))
    assert json.loads(fixes_file.read_text()) == []
    assert json.loads(reviewed.read_text())[tool.report_key(rows[0])]["status"] == "revoked"


def test_pending_lists_unreviewed_provisional_fixes(files, capsys):
    export, reviewed, _ = files
    reviewed.write_text(json.dumps({tool.report_key(report(1)): {"decision": "x", "status": "accepted"}}))
    tool.pending(tool.load(export), str(reviewed))
    out = capsys.readouterr().out
    assert "2 provisional fix(es)" in out and "#2 " in out and "#4 " in out and "#1 " not in out


def test_sync_sets_the_statuses_on_the_site(files, tmp_path, monkeypatch, capsys):
    export, reviewed, _ = files
    site = tmp_path / "site.db"           # the pod's reports database, with the same reports
    store = ReportStore(str(site))
    for r in tool.load(export):
        store.add({k: v for k, v in r.items() if k not in ("id", "created_at", "reviewed_at")})
    with store._connect() as conn:       # same creation times as the export
        for r in tool.load(export):
            conn.execute("UPDATE reports SET created_at = ? WHERE id = ?", (r["created_at"], r["id"]))
    reviewed.write_text(json.dumps({tool.report_key(report(1)): {"status": "accepted"},
                                    tool.report_key(report(2)): {"status": "revoked"},
                                    tool.report_key(report(3)): {"status": None}}))

    def in_pod(command, stdin=None):      # what kubectl exec would run, against the local database
        assert command == "python -"
        return subprocess.run([sys.executable, "-"], input=stdin.replace("/reports/reports.db", str(site)),
                              capture_output=True, text=True)
    monkeypatch.setattr(tool, "in_pod", in_pod)

    tool.sync(str(export), str(reviewed), dry_run=True)
    assert [r["status"] for r in store.all()] == ["provisional", "provisional", None, "provisional"]
    tool.sync(str(export), str(reviewed))
    assert [r["status"] for r in store.all()] == ["accepted", "revoked", None, "provisional"]
    assert all(r["reviewed_at"] for r in store.all()[:2])
    assert [r["status"] for r in tool.load(export)] == ["accepted", "revoked", None, "provisional"]   # recorded
    capsys.readouterr()
    tool.sync(str(export), str(reviewed))
    assert "nothing to send" in capsys.readouterr().out


def test_reports_without_stored_words_get_no_status():
    # reports from before the words were stored: the site never applies them, nothing to sync
    old = report(9, first_tok=None, last_tok=None, first_word=None, last_word=None)
    assert tool.status_after(old, "a") is None and tool.status_after(report(1, "provisional"), "a") == "accepted"
    decided = {tool.report_key(old): {"decision": "apply the adjustment", "status": "accepted"}}   # recorded before
    assert tool.status_changes([old], decided) == []


@pytest.mark.parametrize("namespace, export, reviewed", [
    (None, "reports.json", "reports-reviewed.json"),
    ("extractos", "reports.json", "reports-reviewed.json"),
    ("unaudio-dev", "reports-unaudio-dev.json", "reports-reviewed-unaudio-dev.json"),
])
def test_each_site_has_its_own_files(monkeypatch, namespace, export, reviewed):
    if namespace:
        monkeypatch.setenv("EXTRACTOS_NAMESPACE", namespace)
    else:
        monkeypatch.delenv("EXTRACTOS_NAMESPACE", raising=False)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert (fresh.EXPORT, fresh.REVIEWED) == (export, reviewed)


def site(id, decision, status, **fields):
    """A report decided on the site (/admin)."""
    fields = dict(dict(note=None, reviewed_at="2026-10-21T14:00:00Z", review_start_ms=None, review_end_ms=None), **fields)
    return report(id, status, decision=decision, reviewed_by="admin@example.com", **fields)


def test_pull_takes_the_sites_decisions(tmp_path, capsys):
    reviewed, fixes_file = tmp_path / "reports-reviewed.json", tmp_path / "timing_fixes.json"
    rows = [site(1, "apply", "accepted", note="bien"),                      # the visitor's adjustment
            site(2, "apply", "accepted", review_start_ms=9100, review_end_ms=10300),   # the admin's own times
            site(3, "delete", "deleted"),
            site(4, "nothing", "revoked"),
            report(5, "provisional")]                                        # not decided on the site
    tool.merge_site_decisions(rows, str(reviewed), str(fixes_file))
    fixes_ = {f["report"]: f for f in json.loads(fixes_file.read_text())}
    assert set(fixes_) == {tool.report_key(r) for r in rows[:3]}
    one, two, three = (fixes_[tool.report_key(r)] for r in rows[:3])
    assert (one["action"], one["start"], one["note"], one["added"]) == ("adjust", 9.0, "bien", "2026-10-21") and "end" not in one
    assert (two["start"], two["end"]) == (9.1, 10.3)
    assert three["action"] == "delete" and "start" not in three
    decided = json.loads(reviewed.read_text())
    assert [decided[tool.report_key(r)]["decision"] for r in rows[:4]] == \
        ["apply the adjustment", "apply the adjustment", "delete the words", "nothing wrong"]
    assert tool.report_key(rows[4]) not in decided
    assert tool.status_changes(rows, decided) == []                          # the site already has them
    assert "4 new decision(s)" in capsys.readouterr().out


def test_pull_takes_each_site_decision_once(tmp_path, monkeypatch, capsys):
    reviewed, fixes_file = tmp_path / "reports-reviewed.json", tmp_path / "timing_fixes.json"
    rows = [site(1, "apply", "accepted")]
    tool.merge_site_decisions(rows, str(reviewed), str(fixes_file))
    # reviewed again here: nothing wrong after all. A second pull doesn't bring the old one back
    answers(monkeypatch, "n", "")
    tool.review(rows, str(reviewed), ids=[1], db="/nonexistent", fixes_path=str(fixes_file))
    tool.merge_site_decisions(rows, str(reviewed), str(fixes_file))
    assert json.loads(fixes_file.read_text()) == []
    # ... until the site decides it again
    rows[0].update(decision="delete", status="deleted", reviewed_at="2026-10-22T09:00:00Z")
    capsys.readouterr()
    tool.merge_site_decisions(rows, str(reviewed), str(fixes_file))
    assert [f["action"] for f in json.loads(fixes_file.read_text())] == ["delete"]
    assert "1 new decision(s)" in capsys.readouterr().out
