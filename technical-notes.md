# Technical Decisions

A web application that reads COVID-19 case data published by the Italian Civil Protection Department, stores it in PostgreSQL, and shows total cases by region — searchable, sortable, and exportable to `.xls`.

---

## 1. Understanding the data

Before writing any code, the source dataset was analysed directly. Several of its characteristics turned out to have a direct impact on the schema design — two of them would have caused silent, hard-to-spot bugs if discovered later.

The source is `dpc-covid19-ita-province.json`: the full historical series at province level, published by the Italian Civil Protection Department.

| Property | Value |
|---|---|
| Total records | 262,807 |
| Distinct dates | 1,781 (24/02/2020 – 08/01/2025, no gaps) |
| Distinct regions | 21 |
| Distinct provinces | 149 |

**The 149 "provinces" are not 149 provinces.** Italy has 107 provinces, but the dataset contains 149 distinct province codes. The extra 42 are placeholder entries the source publishes for each region, documented in the official data notes:

| Kind | Count | Code range |
|---|---|---|
| Real provinces | 107 | 1–111 |
| "Out of region / autonomous province" | 21 | 879–899 |
| "Under definition / update" | 21 | 979–999 |

These placeholders carry real case counts (cases not yet attributed to a specific province, or belonging to people from another region), so they **must** be included in regional totals. They have `NULL` for abbreviation, latitude and longitude — so validation can't simply reject rows with missing geographic data. The documented ranges were verified against the actual data and matched exactly; they're now used as a validation rule during ingestion.

**The number of provinces per day is not constant.** The "out of region" placeholders only appear from 25/06/2020 onward. Before that date, each day has 128 rows (107 + 21); after it, 149. Any validation assuming a fixed row count would fail across the first four months of the pandemic.

**Timestamps are not consistent.** The `data` field is a full timestamp, but the time component varies: `17:00:00` on 261,527 rows and `18:00:00` on 1,280 (an early daylight-saving artifact). Searching for an exact timestamp match would silently return nothing for those dates. Ingestion truncates to date only.

**Cross-validation against an independent source.** As a final check, the aggregation produced by this application (summing province-level values) was compared against the pre-aggregated regional file published by the same source. All 21 regions matched exactly — confirming both the aggregation logic and, separately, the region code normalization described below.

---

## 2. Database

### 2.1 Why relational

The core operation is `GROUP BY region, SUM(cases), ORDER BY` — relational algebra in its purest form. The data is perfectly rectangular with a fixed schema, writes are essentially a one-time bulk load, and there's no write concurrency to manage. A document store would add operational complexity without addressing any actual constraint of this workload.

### 2.2 Why separate tables

Three designs were built and measured against the real dataset:

| Design | Aggregate query | Database size |
|---|---|---|
| Three normalized tables | 0.088 ms | **13.0 MB** |
| Two tables (region denormalized onto facts) | 0.090 ms | 13.6 MB |
| Single flat table, no joins | 0.082 ms | 24.2 MB |

The flat table is faster by 6 microseconds — statistical noise — while using 86% more space. Joins cost almost nothing here: the dimension tables are tiny (21 and 149 rows, effectively cached after first access), the join is on an integer primary key, and the date filter reduces the fact rows to ~150 before any join happens.

**The stronger argument is correctness, not performance.** In the flat design, the region name is stored 262,807 times. A single row with a slightly different spelling would silently produce a phantom 22nd region in the `GROUP BY`, with no error raised. In the normalized design the name exists once, and a foreign key rejects any reference to a region that doesn't exist.

### 2.3 Database schema

```sql
CREATE TABLE regions (
    region_code    INTEGER PRIMARY KEY,
    region_name    TEXT NOT NULL UNIQUE,
    nuts_1_code    TEXT,
    nuts_2_code    TEXT
);

CREATE TABLE provinces (
    province_code          INTEGER PRIMARY KEY,
    province_name          TEXT NOT NULL,
    province_abbreviation  TEXT,
    region_code            INTEGER NOT NULL REFERENCES regions(region_code),
    latitude               REAL,
    longitude              REAL,
    nuts_3_code            TEXT
);

CREATE TABLE province_cases (
    date              DATE    NOT NULL,
    province_code     INTEGER NOT NULL REFERENCES provinces(province_code),
    total_cases       INTEGER NOT NULL CHECK (total_cases >= 0),
    notes             TEXT,
    PRIMARY KEY (date, province_code)
);
```

**Natural keys, not surrogate ids.** Both `region_code` (after normalization, see 2.4) and `province_code` were verified stable and unique across all 262,807 rows — same name, abbreviation, coordinates and parent region throughout. There's no reason to invent a key when the source provides a reliable one.

**Composite primary key on the fact table.** `(date, province_code)` is the real natural key — verified: zero duplicates across the entire series. This makes ingestion idempotent (re-running it can't duplicate rows, via `ON CONFLICT DO NOTHING`), and the implicit index has `date` as its leading column, which covers the main query without a separate index: measured, the aggregate query goes from 13.36 ms without it to 0.06 ms with it.

**NUTS codes** (`nuts_1`/`nuts_2` on regions, `nuts_3` on provinces) are placed as stable attributes of the geographic entity: verified constant per entity across the whole series, not daily-varying facts.

**Deliberately omitted:** the `stato` field (always `"ITA"`), and a column classifying the placeholder provinces — the source's own published summaries list them as ordinary rows, so distinguishing them adds complexity the application never needs.

### 2.4 The region code problem, and how ingestion resolves it

The two autonomous provinces of Bolzano and Trento are treated as separate regions by the source (which is why there are 21 "regions" rather than 20), but their region code is not stable across the source's own files:

| File | Bolzano | Trento |
|---|---|---|
| Per-province file, until 24/06/2020 | `21` | `22` |
| Per-province file, from 25/06/2020 | `4` | `4` |
| Official regional file, entire series | `21` | `22` |

From 25/06/2020 the per-province file switches to `4` — the ISTAT code for Trentino-Alto Adige — for **both**. That single code maps to two distinct entities, so it cannot serve as a key: aggregating by region code as-is would silently merge Bolzano and Trento from mid-2020 onward.

The regional file keeps `21`/`22` for the entire series and never uses `4`, so those codes are adopted as canonical. Ingestion normalizes the per-province values using the region *name* — which is stable throughout — to disambiguate:

```python
REGION_CODE_OVERRIDES = {"P.A. Bolzano": 21, "P.A. Trento": 22}

def _normalize_region_code(region_code: int, region_name: str) -> int:
    return REGION_CODE_OVERRIDES.get(region_name, region_code)
```

Every other region's code passes through unchanged. This runs once, in one place, before any row reaches the database — so the ambiguity never propagates any further than the ingestion function itself.

---

## 3. Application

### 3.1 Packages and languages used

Python, with:

- **FastAPI** + **Uvicorn** — web framework and ASGI server. Native type hints, automatic request validation, and free OpenAPI documentation at `/docs` as a side effect of documenting the route functions.
- **SQLAlchemy** — ORM. Parameterizes every value automatically, which is what makes SQL injection on the date parameter structurally impossible rather than a matter of discipline.
- **PostgreSQL**, containerized — chosen as the more production-representative option over SQLite (which would have been technically sufficient for this workload), with Docker removing the setup burden that would normally be its drawback.
- **Jinja2** — server-side HTML templates. No frontend framework: the app is a single page with a form and a table, and state lives in the URL, which doesn't warrant one.
- **xlwt** — generates real legacy `.xls` files, as required — not `.xlsx`, which is what most modern libraries (e.g. `openpyxl`) write instead.
- **httpx** — downloads the dataset from GitHub.
- **rich** — readable, colour-coded console output during startup and ingestion.
- **pytest** — automated test suite.
- **Docker** / **Docker Compose** — containerized database and application.

### 3.2 Web application structure

Two routes, both `GET`, both accepting the same two optional query parameters:

| Route | Returns |
|---|---|
| `/?date=…&sort=…` | The HTML page |
| `/export?date=…&sort=…` | The `.xls` file |

Both delegate to a single shared function, `resolve_target_date()`, which validates the parameters, applies fallbacks, and runs the query. This guarantees the export can never disagree with the page for the same inputs, without keeping two code paths in sync.

The application is stateless: no session, no server-side memory of previous requests. Every request re-runs the query from scratch — cheap, thanks to the primary key index — which means a URL can be shared or bookmarked and will always render the same view.

### 3.3 File overview

```
app/
├── main.py              # App setup, startup lifecycle, routes, error handlers
├── config.py             # Environment-based configuration (.env)
├── database.py             # SQLAlchemy engine/session setup
├── models.py                # ORM models: Region, Province, ProvinceCase
├── repository.py              # Aggregation queries, sorting, date resolution
├── services/
│   ├── ingestion.py              # Download, validate, and load data from GitHub
│   └── export.py                   # .xls file generation
└── templates/
    ├── index.html                    # Main page
    ├── error_404.html                  # Not found
    ├── error_500.html                    # Unexpected error
    └── error_503.html                      # Database unreachable
```

| File | Responsibility |
|---|---|
| `config.py` | Reads `.env` into a typed `Settings` object; builds the database connection string |
| `database.py` | Creates the SQLAlchemy engine (with a bounded connection timeout) and session factory |
| `models.py` | The three ORM classes mapping directly to the schema in §2.3 |
| `repository.py` | All read queries: aggregation, sorting whitelist, date validation and fallback logic |
| `main.py` | Wires everything together: startup sequence, the two routes, the three error handlers |
| `services/ingestion.py` | Everything related to getting data from GitHub into the database |
| `services/export.py` | Turns a list of `(region, cases)` pairs into an `.xls` file |

### 3.4 Table creation and population logic

Everything below runs once, at server startup, inside FastAPI's lifespan handler — never per request:

```
1. Wait for the database (retries with a bounded timeout)
   ↓
2. Create tables if missing (idempotent, safe to call every time)
   ↓
3. Database empty?
   ├── YES → download full dataset, validate, load
   └── NO  → download only the small "latest" file and compare dates
             ├── new data available → download full dataset and update
             └── already current, or check failed → skip
   ↓
4. Server starts accepting requests
```

Re-downloading 109 MB on every startup just to check whether anything changed would be wasteful. The source also publishes a small file containing only the most recent day; comparing its date against `MAX(date)` in the database answers the question for roughly 0.05% of the bandwidth.

### 3.5 How each feature works

**Regional overview and sorting.** The main query:

```python
select(Region.region_name, func.sum(ProvinceCase.total_cases).label("total"))
    .join(Province, Province.region_code == Region.region_code)
    .join(ProvinceCase, ProvinceCase.province_code == Province.province_code)
    .where(ProvinceCase.date == target_date)
    .group_by(Region.region_code, Region.region_name)
    .order_by(*order_by)
```

`total_cases` in the source is **cumulative** — the running total for that province since the start of the pandemic, not new cases that day. The query sums across provinces *within a single date*, never across dates: summing the same province over multiple days would count the same cases repeatedly.

The sort order (`order_by`) is chosen from four fixed options via a Python `Enum`, never built from a raw string — see §4.2.

**Date search.** `date` is parsed with `datetime.date.fromisoformat()`. Anything that isn't a valid ISO date is rejected with a clear message; a date outside the available range produces a specific message stating the actual boundary. In every failure case, the page still falls back to showing the most recent available date rather than an empty table.

The source stopped publishing on 08/01/2025, so "today" — accessed with no search — has no data. Rather than an empty page, the application falls back to the most recent available date and states this explicitly. That notice appears only when no date was explicitly requested; searching a specific past date returns exactly what was asked for, with no notice.

**Export.** Calls the same `resolve_target_date()` as the page, then hands the resulting rows to `generate_xls()`, which writes two columns (region, total cases) into a single sheet using `xlwt`, and returns the raw file bytes as a downloadable attachment. Because it shares the resolution logic with the page, the exported file is always consistent with whatever is currently shown — including the same fallback and validation behaviour.

---

## 4. Security

### 4.1 Handling an unreachable database and GitHub rate limiting

Three distinct failure scenarios are handled, each verified by actually triggering it:

**Database unreachable at startup.** `wait_for_database()` retries the connection (3 attempts, 3-second timeout each) before giving up. The timeout is explicit and deliberate: without one, an unreachable database fails according to the operating system's own default TCP timeout, which varies enough between Windows and Linux to make the application appear frozen rather than waiting — discovered directly while testing on both platforms. If every attempt fails, the process terminates cleanly (`os._exit`) with a clear console message, rather than letting Uvicorn print a raw internal traceback on top of it.

**Database becomes unreachable while the app is already running** (e.g. the database container is stopped mid-session, tested directly with `docker compose stop db`). A dedicated exception handler for `OperationalError` returns a `503 Service Unavailable` with a specific page, distinct from a generic error — this is an infrastructure condition, not a bug in the application. Verified: the very next request, once the database is reachable again, succeeds normally with no restart needed.

**GitHub rate-limits the update check.** `check_for_updates()` makes a request to GitHub on every startup where data already exists. This call is wrapped so that any network failure — rate limiting, timeout, GitHub being unreachable — is caught, logged, and treated as "no update available" rather than allowed to crash the startup. This was found directly during development: the unprotected version, hit with a real `429 Too Many Requests` after many rapid restarts while testing, crashed the whole application on startup, which combined with the container's automatic restart policy produced a crash loop that kept re-triggering the same rate limit instead of recovering from it.

### 4.2 Security checks and their responses

Every check below was verified with an actual malicious payload, not assumed from the design.

| Check | How it's enforced | Verified response |
|---|---|---|
| SQL injection via `date` | `datetime.date.fromisoformat()` — anything invalid raises before touching SQL; the parsed value is then a bound parameter, never interpolated | Malformed input → clear error message, page still renders |
| SQL injection via `sort` | Mapped through a closed `Enum` (`cases_desc`, `cases_asc`, `name_asc`, `name_desc`); `ORDER BY` cannot be parameterized like a value, so this is the actual defense | Payload `x; DROP TABLE regions;--` → falls back to default sort, tables intact afterwards |
| Cross-site scripting | Invalid parameter values are echoed in error messages; Jinja2 autoescaping is on by default for `.html` templates | Payload `<script>alert(1)</script>` → appears only as escaped text (`&lt;script&gt;`), never executable |
| Information disclosure on unexpected errors | A catch-all exception handler logs the full traceback server-side, returns only a generic page to the client | Forced an error whose message contained a database password → password never appeared in the client response |
| Secrets in source control / image | `.env` excluded from both Git (`.gitignore`) and the Docker build context (`.dockerignore`) — two separate mechanisms, both necessary | `.env` confirmed absent from `git status` before every commit, and from the built image |

**Deliberately out of scope:** CSRF (both routes are `GET` and read-only, nothing for a third party to forge), rate limiting (an infrastructure-layer concern, not application code), HTTPS (a deployment concern, not the application itself).

---

## 5. Testing

### 5.1 File structure

```
tests/
├── conftest.py           # Shared fixtures: temporary SQLite database, fake seed data
├── test_repository.py     # Aggregation, sorting, date/sort validation
├── test_export.py           # .xls file generation
└── test_routes.py             # Full HTTP requests through the two routes
```

`conftest.py` is the foundation: before the application is even imported, it swaps the database engine for a temporary SQLite file, creates the schema, and seeds it with three fake regions, three provinces, and two dates — with case counts chosen so that sorting by cases and sorting by name always produce *different* orderings, making sort assertions unambiguous. Region names (`TestRegionA`, `TestRegionB`, `TestRegionZ`) are deliberately unlike any real region, so a bug that accidentally reached real data would be obvious. Network calls (`needs_ingestion`, `check_for_updates`) are patched out, so the suite never contacts GitHub.

This is a deliberate trade-off over testing against the real 262,807 records: slower, dependent on an internet connection, and liable to break whenever the upstream data changes. The fixed dataset makes every assertion exact and every run identical — the full suite runs in well under a second.

The dialect-aware upsert helper in `ingestion.py` is what makes this possible for the ingestion path specifically: it selects PostgreSQL or SQLite syntax based on the connected database, so the same production code runs unmodified against the test database.

### 5.2 What the tests do

| File | Coverage |
|---|---|
| `test_repository.py` | Aggregation correctness; all four sort orders; date validation across every edge case (valid, out of range, malformed); an injection attempt on the sort parameter |
| `test_export.py` | The generated file is genuine OLE2; headers and data land in the right columns; empty input still produces a valid file |
| `test_routes.py` | Full HTTP round-trips; fallback behaviour; error banners; XSS and injection payloads; consistency between the page and the export for identical parameters |

That last check in `test_routes.py` is the automated version of a bug found manually during development, when the export link was static and silently ignored the current search — it fetches both the page and the export with the same parameters and asserts they contain exactly the same data.

Running them:

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

---

## 6. AI assistance

This project was developed with the assistance of Claude (Anthropic) — specifically Claude Sonnet 5, with high/extended reasoning enabled. It was used for:

- Research and documentation lookup on which packages and libraries to use for a given problem, and how to use them idiomatically
- Support in managing security aspects
- Writing and translating the documentation embedded in the code (docstrings, comments), README file and this document
- Building the automated test suite
