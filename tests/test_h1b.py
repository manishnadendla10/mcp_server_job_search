"""H-1B loader + matching, using small synthetic CSVs in both USCIS formats."""

import pytest

import catalog
import db
import h1b

OLD_FORMAT = """Fiscal Year,Employer,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial,NAICS,Tax ID,State,City,ZIP
2023,CAPITAL ONE SERVICES LLC,"1,200",10,800,5,52,1234,VA,MCLEAN,22102
2023,CAPITAL ONE NATIONAL ASSOCIATION,300,2,100,1,52,5678,VA,MCLEAN,22102
2023,SALESFORCE.COM INC,900,9,600,3,51,4321,CA,SAN FRANCISCO,94105
2023,CAPITOL ONE DENTAL PC,1,0,0,0,62,9999,TX,DALLAS,75201
2022,CAPITAL ONE SERVICES LLC,1000,20,700,4,52,1234,VA,MCLEAN,22102
,Grand Total,99999,0,0,0,,,,,
"""

# Newer hub column names; Tableau exports this as UTF-16 with tabs.
NEW_FORMAT_HEADER = [
    "Fiscal Year", "Employer (Petitioner) Name", "Tax ID", "Industry (NAICS) Code",
    "Petitioner City", "Petitioner State", "Petitioner Zip Code",
    "New Employment Approval", "New Employment Denial", "Continuation Approval", "Continuation Denial",
    "Change of Employer Approval", "Change of Employer Denial", "Amended Approval", "Amended Denial",
]
NEW_FORMAT_ROW = ["2024", "Salesforce, Inc.", "4321", "51", "San Francisco", "CA", "94105",
                  "500", "5", "400", "2", "100", "1", "50", "0"]

COMPANIES = [
    {"name": "Capital One", "source": "workday", "board": "capitalone.wd12.myworkdayjobs.com/Capital_One"},
    {"name": "Salesforce", "source": "workday", "board": "salesforce.wd12.myworkdayjobs.com/External_Career_Site"},
    {"name": "Tiny Startup", "source": "ashby", "board": "tiny"},
]


@pytest.fixture
def loaded(tmp_path):
    old = tmp_path / "fy2023.csv"
    old.write_text(OLD_FORMAT, encoding="utf-8")
    new = tmp_path / "fy2024.csv"
    new.write_bytes(("\t".join(NEW_FORMAT_HEADER) + "\n" + "\t".join(NEW_FORMAT_ROW) + "\n").encode("utf-16"))
    stats = h1b.load([old, new])
    h1b.rebuild_company_map(COMPANIES)
    return stats


@pytest.mark.parametrize("raw, expected", [
    ("CAPITAL ONE SERVICES LLC", "capital one services"),
    ("Capital One, N.A.", "capital one"),
    ("SALESFORCE.COM INC", "salesforce"),
    ("Salesforce, Inc.", "salesforce"),
    ("AT&T SERVICES INC", "at and t services"),
    ("The Home Depot Co Inc", "home depot"),
    ("ACME LLC DBA ROADRUNNER", "acme"),
    ("MAPLEBEAR INC D B A INSTACART", "maplebear"),
    ("MAPLEBEAR INC D/B/A INSTACART", "maplebear"),
    ("FMR LLC D.B.A. FIDELITY INVESTMENTS", "fmr"),
    ("CITIBANK N A", "citibank"),
])
def test_normalize_name(raw, expected):
    assert h1b.normalize_name(raw) == expected


def test_load_both_formats_and_skips_total_rows(loaded):
    assert loaded["fiscal_years"] == [2022, 2023, 2024]
    assert loaded["rows"] == 6  # "Grand Total" row (no year) dropped


def test_new_format_maps_change_of_employer_to_initial(loaded):
    conn = db.connect()
    row = conn.execute("SELECT * FROM h1b_employers WHERE fiscal_year = 2024").fetchone()
    conn.close()
    assert row["initial_approvals"] == 600       # new employment 500 + change of employer 100
    assert row["continuing_approvals"] == 450    # continuation 400 + amended 50
    assert row["state"] == "CA" and row["city"] == "San Francisco"


def test_matching_prefix_merges_entities_but_rejects_lookalikes(loaded):
    index = {"capital one services": 1, "capital one national association": 1, "capitol one dental": 1}
    names = {n for n, _, _ in h1b.match_company("Capital One", index)}
    assert names == {"capital one services", "capital one national association"}


# Real false positives seen in the FY2022-2026 USCIS data.
@pytest.mark.parametrize("company, index, expected", [
    ("Toast", ["toast", "toast wedding films", "toasty video"], {"toast"}),
    ("Intel", ["intel", "intel federal", "intel americas", "intel grids", "intel graph"],
     {"intel", "intel federal", "intel americas"}),
    ("Stripe", ["stripe", "stripe payments", "stripes"], {"stripe", "stripe payments"}),
    ("Affirm", ["affirm", "affirma", "affirm entertainment"], {"affirm"}),
    ("AT&T", ["at and t services", "a and t", "at and t mobility services"],
     {"at and t services", "at and t mobility services"}),
    ("Samsara", ["samsara", "samsara networks", "samsara biocapital"], {"samsara", "samsara networks"}),
    ("Databricks", ["databricks", "data bricks"], {"databricks", "data bricks"}),
    ("Pinterest", ["pinterest", "printerest"], {"pinterest", "printerest"}),  # long name: fuzzy allowed
])
def test_matching_rejects_real_false_positives(company, index, expected):
    got = {n for n, _, _ in h1b.match_company(company, dict.fromkeys(index, 1))}
    assert got == expected


def test_override_wins(loaded):
    index = {"capital one services": 1, "capital one national association": 1}
    out = h1b.match_company("Capital One", index, overrides=["CAPITAL ONE SERVICES LLC"])
    assert out == [("capital one services", "override", 100.0)]


def test_check_sponsorship_aggregates_years(loaded):
    r = h1b.check_sponsorship("Capital One", COMPANIES)
    assert r["found"] is True
    fy23 = next(y for y in r["by_fiscal_year"] if y["fiscal_year"] == 2023)
    assert fy23["initial_approvals"] == 1500 and fy23["continuing_approvals"] == 900
    assert fy23["approval_rate"] == round(2400 / (2400 + 18), 3)
    assert {e["employer"] for e in r["matched_uscis_employers"]} == {
        "CAPITAL ONE SERVICES LLC", "CAPITAL ONE NATIONAL ASSOCIATION"}


def test_check_sponsorship_untracked_and_missing(loaded):
    assert h1b.check_sponsorship("salesforce.com")["found"] is True  # on-the-fly match
    assert h1b.check_sponsorship("Tiny Startup", COMPANIES)["found"] is False


def test_check_sponsorship_without_data_explains():
    with pytest.raises(ValueError, match="No H-1B data loaded"):
        h1b.check_sponsorship("Capital One")


def test_sponsors_only_search_filter(loaded):
    conn = db.connect()
    with conn:
        for i, (src, board, co) in enumerate([(c["source"], c["board"], c["name"]) for c in COMPANIES]):
            conn.execute(
                "INSERT INTO jobs (id, source, board, company, title, location, remote, url, posted_at) "
                "VALUES (?, ?, ?, ?, 'Engineer', 'Dallas, TX', 0, 'u', '2026-11-01T00:00:00+00:00')",
                (f"{src}:{board}:{i}", src, board, co))
    conn.close()
    everyone = catalog.search(keywords=["engineer"])
    sponsors = catalog.search(keywords=["engineer"], sponsors_only=True)
    big = catalog.search(keywords=["engineer"], sponsors_only=True, min_h1b_approvals=3000)
    assert everyone["total_matches"] == 3
    assert {j["company"] for j in sponsors["jobs"]} == {"Capital One", "Salesforce"}
    assert [j["company"] for j in big["jobs"]] == ["Capital One"]   # 4,100 approvals vs Salesforce's 2,550
    assert all("h1b_approvals_5y" in j for j in everyone["jobs"])
