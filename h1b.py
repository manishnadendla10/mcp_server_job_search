"""H-1B sponsorship data: USCIS Employer Data Hub CSV loader + company name matching.

Usage:
    python h1b.py load data/h1b/*.csv     # load files, rebuild company matches, print report
    python h1b.py report                  # re-print the match report

The hard part is entity resolution. USCIS lists legal entities ("CAPITAL ONE
SERVICES LLC", "CAPITAL ONE NATIONAL ASSOCIATION", "SALESFORCE.COM INC"), while
companies.yaml says "Capital One" / "Salesforce". Strategy, most to least precise:

  1. override  - `h1b_names:` in companies.yaml, exact after normalization
  2. exact     - normalized names are identical
  3. prefix    - USCIS name = company name + only corporate qualifier words
                 ("capital one" -> "capital one services", but NOT "toast" -> "toast wedding films")
  4. fuzzy     - rapidfuzz ratio >= FUZZY_MIN, names >= FUZZY_MIN_LEN chars only
                 ("pinterest" -> "printerest", but NOT "stripe" -> "stripes")

Brand names that differ from the legal entity (Instacart = "MAPLEBEAR INC") can't be
inferred and need an `h1b_names` override.

Every match is stored with its method + score, and check_sponsorship shows the
matched names, so a wrong match is visible and fixable with an override.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz, process

import db

FUZZY_MIN = 94
FUZZY_MIN_LEN = 9        # short names make fuzzy matching meaningless: stripe~stripes, affirm~affirma
RECENT_YEARS = 5

# A prefix match ("capital one" -> "capital one services") is accepted only when every
# extra word is a corporate qualifier. This rejects unrelated businesses that merely
# share a first word: "toast wedding films", "intel grids", "citi diamonds".
_QUALIFIERS = {
    "services", "service", "technologies", "technology", "tech", "payments", "payment",
    "markets", "bank", "banking", "financial", "finance", "usa", "us", "america", "americas",
    "north", "international", "global", "glbl", "networks", "holdings", "group", "labs",
    "federal", "mobility", "securities", "clearing", "data", "national", "association",
    "investments", "brokerage", "logistics", "operations", "software", "digital", "platforms",
}

# Trailing legal-entity words removed during normalization. Deliberately NOT
# removing descriptive words like "services"/"technologies": "Amazon Web Services"
# must stay distinct from "Amazon".
_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "corp", "corporation", "co", "company",
    "ltd", "limited", "lp", "llp", "plc", "pc", "pllc", "na", "n a", "com", "the",
}
_SUFFIX_RE = re.compile(r"(?:\s+(?:" + "|".join(sorted(map(re.escape, _SUFFIXES), key=len, reverse=True)) + r"))+$")


def normalize_name(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()   # punctuation -> spaces
    s = re.sub(r"\b(dba|d b a)\b.*$", "", s).strip()  # "X LLC D/B/A Y", "X D.B.A. Y" -> "X LLC"
    s = re.sub(r"^the\s+", "", s)
    prev = None
    while prev != s:                             # strip stacked suffixes: "x co inc"
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
    return s


# ---------------------------------------------------------------- CSV loading

def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):    # Tableau crosstab exports are often UTF-16
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _int(v: str | None) -> int:
    v = (v or "").replace(",", "").strip()
    try:
        return int(float(v)) if v else 0
    except ValueError:
        return 0


def _classify_columns(headers: list[str]) -> dict:
    """Map whatever header names this export uses onto our fields.
    Handles the pre-2024 hub ('Employer', 'Initial Approval', ...) and the newer one
    ('Employer (Petitioner) Name', 'New Employment Approval', 'Continuation Approval', ...)."""
    norm = {h: re.sub(r"\s+", " ", h.strip().lower()) for h in headers}
    cols = {"initial_approvals": [], "initial_denials": [], "continuing_approvals": [], "continuing_denials": []}
    for h, n in norm.items():
        # Order matters: "Petitioner City" etc. must not be mistaken for the employer column.
        if "fiscal year" in n:
            cols["fiscal_year"] = h
        elif "tax id" in n:
            cols["tax_id"] = h
        elif n.endswith("city"):
            cols["city"] = h
        elif n.endswith("state"):
            cols["state"] = h
        elif n.endswith("zip") or "zip code" in n or "naics" in n:
            continue
        elif "employer" in n and "approval" not in n and "denial" not in n:
            cols.setdefault("employer", h)
        elif "approval" in n or "denial" in n:
            kind = "approvals" if "approval" in n else "denials"
            # "new" = first H-1B with this employer (initial / new employment / change of employer / concurrent)
            is_new = any(k in n for k in ("initial", "new employment", "change of employer", "new concurrent"))
            cols[f"{'initial' if is_new else 'continuing'}_{kind}"].append(h)
    missing = [k for k in ("fiscal_year", "employer") if k not in cols]
    if missing or not any(cols[k] for k in ("initial_approvals", "continuing_approvals")):
        raise ValueError(f"Unrecognized H-1B CSV header (missing {missing or 'approval columns'}): {headers}")
    return cols


def parse_csv(path: Path) -> list[tuple]:
    text = _read_text(path)
    first_line = text.split("\n", 1)[0]
    delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    cols = _classify_columns(reader.fieldnames or [])
    rows = []
    for r in reader:
        employer = (r.get(cols["employer"]) or "").strip()
        year = _int(r.get(cols["fiscal_year"]))
        if not employer or employer.lower() == "null" or not year:
            continue  # blank lines, Tableau "Grand Total" rows, redacted "Null" employers
        rows.append((
            year, employer, normalize_name(employer),
            (r.get(cols.get("tax_id", "")) or "").strip() or None,
            (r.get(cols.get("city", "")) or "").strip() or None,
            (r.get(cols.get("state", "")) or "").strip() or None,
            *(sum(_int(r.get(c)) for c in cols[k])
              for k in ("initial_approvals", "initial_denials", "continuing_approvals", "continuing_denials")),
        ))
    return rows


def load(paths: list[Path], replace: bool = True) -> dict:
    conn = db.connect()
    total, years = 0, set()
    with conn:
        if replace:
            conn.execute("DELETE FROM h1b_employers")
        for p in paths:
            rows = parse_csv(p)
            conn.executemany("INSERT INTO h1b_employers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            total += len(rows)
            years |= {r[0] for r in rows}
    conn.close()
    return {"rows": total, "fiscal_years": sorted(years), "files": len(paths)}


# ---------------------------------------------------------------- matching

def _employer_index(conn) -> dict[str, int]:
    """employer_norm -> total approvals (all years). Bigger = more likely the real company."""
    return {
        r[0]: r[1] for r in conn.execute(
            "SELECT employer_norm, SUM(initial_approvals + continuing_approvals) FROM h1b_employers GROUP BY employer_norm"
        )
    }


def match_company(name: str, index: dict[str, int], overrides: list[str] | None = None) -> list[tuple[str, str, float]]:
    """Return [(employer_norm, method, score)] for one company name."""
    if overrides:
        wanted = {normalize_name(o) for o in overrides}
        return [(n, "override", 100.0) for n in wanted if n in index]

    target = normalize_name(name)
    if not target:
        return []
    out: dict[str, tuple[str, float]] = {}
    if target in index:
        out[target] = ("exact", 100.0)
    squashed = target.replace(" ", "")
    prefix = target + " "
    for n in index:
        if n.startswith(prefix) and set(n[len(prefix):].split()) <= _QUALIFIERS:
            out.setdefault(n, ("prefix", 95.0))
        elif n.replace(" ", "") == squashed:          # "data bricks" == "databricks"
            out.setdefault(n, ("exact", 99.0))
    if len(target) >= FUZZY_MIN_LEN:
        for n, score, _ in process.extract(target, list(index), scorer=fuzz.ratio,
                                           score_cutoff=FUZZY_MIN, limit=10):
            out.setdefault(n, ("fuzzy", float(score)))
    return [(n, m, s) for n, (m, s) in out.items()]


def rebuild_company_map(companies: list[dict]) -> list[dict]:
    """Match every companies.yaml entry; refresh company_h1b_map + company_sponsors."""
    conn = db.connect()
    index = _employer_index(conn)
    latest = conn.execute("SELECT MAX(fiscal_year) FROM h1b_employers").fetchone()[0]
    report = []
    with conn:
        conn.execute("DELETE FROM company_h1b_map")
        conn.execute("DELETE FROM company_sponsors")
        if latest is None:
            return report
        first = latest - RECENT_YEARS + 1
        for c in companies:
            matches = match_company(c["name"], index, c.get("h1b_names"))
            conn.executemany(
                "INSERT OR REPLACE INTO company_h1b_map VALUES (?, ?, ?, ?, ?, ?)",
                [(c["source"], c["board"], c["name"], n, m, s) for n, m, s in matches],
            )
            approvals = 0
            if matches:
                names = [n for n, _, _ in matches]
                approvals = conn.execute(
                    f"""SELECT COALESCE(SUM(initial_approvals + continuing_approvals), 0) FROM h1b_employers
                        WHERE fiscal_year >= ? AND employer_norm IN ({','.join('?' * len(names))})""",
                    [first] + names,
                ).fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO company_sponsors VALUES (?, ?, ?, ?, ?)",
                (c["source"], c["board"], c["name"], approvals, f"{first}-{latest}"),
            )
            report.append({
                "company": c["name"], "recent_approvals": approvals,
                "matches": sorted(((n, m, round(s)) for n, m, s in matches), key=lambda x: -index.get(x[0], 0)),
            })
    conn.close()
    return report


# ---------------------------------------------------------------- lookup

def has_data() -> bool:
    conn = db.connect()
    try:
        return conn.execute("SELECT 1 FROM h1b_employers LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def check_sponsorship(company: str, companies: list[dict] | None = None) -> dict:
    conn = db.connect()
    try:
        if conn.execute("SELECT 1 FROM h1b_employers LIMIT 1").fetchone() is None:
            raise ValueError("No H-1B data loaded yet. Run: python h1b.py load data/h1b/*.csv")

        # Tracked company? Use its precomputed (and possibly overridden) match.
        names, how = [], None
        for c in companies or []:
            if company.lower() in (c["name"].lower(), c["board"].lower()):
                names = [r[0] for r in conn.execute(
                    "SELECT employer_norm FROM company_h1b_map WHERE source = ? AND board = ?", (c["source"], c["board"]))]
                company, how = c["name"], "precomputed match for tracked company"
                break
        if how is None:
            names = [n for n, _, _ in match_company(company, _employer_index(conn))]
            how = "matched on the fly (not in companies.yaml)"

        if not names:
            return {"company": company, "found": False, "match": how,
                    "message": "No matching employer in USCIS H-1B data. Try the legal name, e.g. 'Acme Technologies'."}

        ph = ",".join("?" * len(names))
        by_year = [dict(r) for r in conn.execute(
            f"""SELECT fiscal_year,
                       SUM(initial_approvals) AS initial_approvals, SUM(initial_denials) AS initial_denials,
                       SUM(continuing_approvals) AS continuing_approvals, SUM(continuing_denials) AS continuing_denials
                FROM h1b_employers WHERE employer_norm IN ({ph})
                GROUP BY fiscal_year ORDER BY fiscal_year DESC""", names)]
        for y in by_year:
            decided = sum(y[k] for k in ("initial_approvals", "initial_denials", "continuing_approvals", "continuing_denials"))
            y["approval_rate"] = round((y["initial_approvals"] + y["continuing_approvals"]) / decided, 3) if decided else None
        top_entities = [dict(r) for r in conn.execute(
            f"""SELECT employer, SUM(initial_approvals + continuing_approvals) AS approvals FROM h1b_employers
                WHERE employer_norm IN ({ph}) GROUP BY employer ORDER BY approvals DESC LIMIT 8""", names)]
    finally:
        conn.close()

    recent = by_year[:RECENT_YEARS]
    return {
        "company": company,
        "found": True,
        "match": how,
        "recent_initial_approvals": sum(y["initial_approvals"] for y in recent),
        "recent_total_approvals": sum(y["initial_approvals"] + y["continuing_approvals"] for y in recent),
        "by_fiscal_year": by_year,
        "matched_uscis_employers": top_entities,
        "notes": "initial = new H-1B employment with this employer (incl. transfers in); "
                 "continuing = extensions/amendments. Counts are USCIS first decisions per fiscal year.",
    }


# ---------------------------------------------------------------- CLI

def _print_report(report: list[dict]) -> None:
    for r in sorted(report, key=lambda x: -x["recent_approvals"]):
        flag = "  <-- no match: add h1b_names in companies.yaml?" if not r["matches"] else ""
        print(f"{r['recent_approvals']:>7,}  {r['company']}{flag}")
        for n, m, s in r["matches"][:4]:
            print(f"           {m:<8} {s:>3}  {n}")
        if len(r["matches"]) > 4:
            print(f"           ... +{len(r['matches']) - 4} more")


def main(argv: list[str]) -> int:
    import glob
    import catalog
    if not argv or argv[0] not in ("load", "report"):
        print(__doc__)
        return 2
    if argv[0] == "load":
        paths = [Path(p) for a in argv[1:] for p in (glob.glob(a) or [a])]
        if not paths:
            print("No files given. Example: python h1b.py load data/h1b/*.csv")
            return 2
        print(load(paths))
    _print_report(rebuild_company_map(catalog.load_companies()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
