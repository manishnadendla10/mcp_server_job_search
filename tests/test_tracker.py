from datetime import datetime, timedelta, timezone

import pytest

import db
import tracker

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def days(n):
    return T0 + timedelta(days=n)


def test_save_from_cache_snapshots_job_fields(cached_job):
    app = tracker.save_job(cached_job, notes="Referral from Sam", now=T0)
    assert app["company"] == "Acme"
    assert app["title"] == "Backend Engineer"
    assert app["status"] == "saved"
    assert app["posting_open"] is True
    assert app["history"] == [{"status": "saved", "note": "Referral from Sam", "created_at": T0.isoformat()}]


def test_application_survives_posting_closing(cached_job):
    app = tracker.save_job(cached_job, now=T0)
    conn = db.connect()
    with conn:
        conn.execute("DELETE FROM jobs")  # board refresh dropped the closed posting
    conn.close()
    again = tracker.get_application(app["id"])
    assert again["title"] == "Backend Engineer"
    assert again["posting_open"] is False


def test_saving_twice_does_not_duplicate(cached_job):
    first = tracker.save_job(cached_job, now=T0)
    second = tracker.save_job(cached_job, notes="still interested", now=days(1))
    assert second["id"] == first["id"]
    assert second["already_tracked"] is True
    assert len(second["history"]) == 2
    assert tracker.list_applications()["counts_by_status"]["saved"] == 1


def test_manual_entry_without_job_id():
    app = tracker.save_job(company="Globex", title="SRE", url="https://globex.example/jobs/9",
                           status="applied", now=T0)
    assert app["job_id"] is None
    assert app["status"] == "applied"


def test_save_requires_job_or_company_and_title():
    with pytest.raises(ValueError):
        tracker.save_job(company="Globex")
    with pytest.raises(ValueError, match="isn't in the cache"):
        tracker.save_job("greenhouse:gone:404")


def test_status_flow_records_history(cached_job):
    app = tracker.save_job(cached_job, now=T0)
    tracker.update_application(app["id"], status="applied", now=days(1))
    final = tracker.update_application(app["id"], status="interviewing", note="Phone screen Tue", now=days(5))
    assert final["status"] == "interviewing"
    assert [h["status"] for h in final["history"]] == ["saved", "applied", "interviewing"]
    assert final["history"][-1]["note"] == "Phone screen Tue"


def test_update_rejects_bad_input(cached_job):
    app = tracker.save_job(cached_job, now=T0)
    with pytest.raises(ValueError, match="Unknown status"):
        tracker.update_application(app["id"], status="ghosted")
    with pytest.raises(ValueError, match="No application"):
        tracker.update_application(999, status="applied")
    with pytest.raises(ValueError, match="status, a note"):
        tracker.update_application(app["id"])


def test_list_filters_by_status_and_company(cached_job):
    tracker.save_job(cached_job, now=T0)
    tracker.save_job(company="Globex", title="SRE", status="applied", now=T0)
    applied = tracker.list_applications(statuses=["applied"])
    assert [a["company"] for a in applied["applications"]] == ["Globex"]
    assert applied["counts_by_status"]["saved"] == 1
    assert [a["company"] for a in tracker.list_applications(company="acm")["applications"]] == ["Acme"]


def test_followups_due_only_stale_active_applications(cached_job):
    stale = tracker.save_job(company="Old Co", title="Engineer", status="applied", now=T0)
    tracker.save_job(company="Fresh Co", title="Engineer", status="applied", now=days(8))
    tracker.save_job(cached_job, now=T0)  # 'saved' is not active -> never due
    rejected = tracker.save_job(company="Nope Co", title="Engineer", status="applied", now=T0)
    tracker.update_application(rejected["id"], status="rejected", now=days(1))

    due = tracker.followups_due(days=7, now=days(10))["due"]
    assert [a["company"] for a in due] == ["Old Co"]
    assert due[0]["days_since_update"] == 10
    assert due[0]["id"] == stale["id"]


def test_note_resets_followup_clock():
    app = tracker.save_job(company="Old Co", title="Engineer", status="applied", now=T0)
    assert tracker.followups_due(days=7, now=days(10))["due"]
    tracker.update_application(app["id"], note="Emailed recruiter", now=days(10))
    assert tracker.followups_due(days=7, now=days(12))["due"] == []
