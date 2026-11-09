"""CLI: drives jobsearch.main() like a user would, against a temp database."""

import json

import pytest

import db
import jobsearch


@pytest.fixture
def jobs():
    conn = db.connect()
    with conn:
        conn.executemany(
            """INSERT INTO jobs (id, source, board, company, title, location, remote, url, posted_at, description)
               VALUES (?, 'greenhouse', 'acme', 'Acme', ?, ?, ?, ?, ?, ?)""",
            [
                ("greenhouse:acme:1", "Backend Engineer", "Dallas, TX", 0, "https://acme/1",
                 "2099-01-02T00:00:00+00:00", "Build APIs.\n\nPython and AWS."),
                ("greenhouse:acme:2", "Cloud Engineer", "Remote - US", 1, "https://acme/2",
                 "2099-01-01T00:00:00+00:00", "Run Kubernetes."),
                ("greenhouse:acme:3", "Recruiter", "Dallas, TX", 0, "https://acme/3",
                 "2099-01-03T00:00:00+00:00", "Hire people."),
            ],
        )
    conn.close()


def run(capsys, *argv):
    code = jobsearch.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_search_table(jobs, capsys):
    code, out, _ = run(capsys, "search", "backend", "cloud", "--no-refresh")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split()[:3] == ["POSTED", "COMPANY", "TITLE"]
    assert "Backend Engineer" in lines[2] and "Cloud Engineer" in lines[3]   # newest first
    assert "Recruiter" not in out
    assert "2 matches" in out


def test_search_filters_and_json(jobs, capsys):
    code, out, _ = run(capsys, "search", "-l", "dallas", "--no-remote", "--json", "--no-refresh")
    assert code == 0
    r = json.loads(out)
    assert [j["id"] for j in r["jobs"]] == ["greenhouse:acme:3", "greenhouse:acme:1"]

    _, out, _ = run(capsys, "search", "--remote", "--json", "--no-refresh")
    assert [j["id"] for j in json.loads(out)["jobs"]] == ["greenhouse:acme:2"]


def test_search_limit_hint(jobs, capsys):
    _, out, _ = run(capsys, "search", "-n", "1", "--no-refresh")
    assert "3 matches (showing 1" in out


def test_show(jobs, capsys):
    code, out, _ = run(capsys, "show", "greenhouse:acme:1")
    assert code == 0
    assert out.splitlines()[0] == "Backend Engineer"
    assert "Apply:  https://acme/1" in out and "Python and AWS." in out


def test_show_missing(capsys):
    code, _, err = run(capsys, "show", "greenhouse:nope:9")
    assert code == 1 and "no job with id" in err


def test_tracker_workflow(jobs, capsys):
    code, out, _ = run(capsys, "save", "greenhouse:acme:1", "--note", "referral")
    assert code == 0 and out.startswith("Saved.") and "#1  Backend Engineer @ Acme  [saved]" in out

    code, out, _ = run(capsys, "update", "1", "--status", "applied", "--note", "applied on site")
    assert code == 0 and "[applied]" in out and "applied - applied on site" in out

    run(capsys, "save", "--company", "Globex", "--title", "SRE", "--status", "interviewing")
    code, out, _ = run(capsys, "list", "--status", "applied")
    assert code == 0 and "Backend Engineer" in out and "SRE" not in out
    assert "saved" not in out.splitlines()[-1]          # counts line only lists non-zero statuses
    assert "applied: 1" in out and "interviewing: 1" in out

    # Both are fresh, so nothing is due yet.
    _, out, _ = run(capsys, "followups", "--days", "7")
    assert out.startswith("Nothing to follow up on")


def test_tracker_errors(capsys):
    code, _, err = run(capsys, "save")
    assert code == 1 and "Pass a job_id" in err
    code, _, err = run(capsys, "update", "42", "--note", "x")
    assert code == 1 and "No application with id 42" in err
    with pytest.raises(SystemExit):              # argparse rejects unknown statuses
        jobsearch.main(["update", "1", "--status", "ghosted"])


def test_list_and_followups_empty(capsys):
    _, out, _ = run(capsys, "list")
    assert out.startswith("No applications yet")
    _, out, _ = run(capsys, "followups")
    assert "you have no applied/interviewing applications" in out


def test_sponsor_without_data(capsys):
    code, _, err = run(capsys, "sponsor", "Acme")
    assert code == 1 and "No H-1B data loaded" in err
