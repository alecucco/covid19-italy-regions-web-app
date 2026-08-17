# COVID-19 Cases by Italian Region

A web application that reads the per-province COVID-19 dataset published by the Italian Civil Protection Department ([pcm-dpc/COVID-19](https://github.com/pcm-dpc/COVID-19)), stores it in a PostgreSQL database, and shows the total number of cases aggregated by region — searchable by date, sortable, and exportable to `.xls`.

Built as a technical hiring assessment.

📄 For a more detailed discussion of the data analysis and database design process — including the full schema rationale and data validation rules — see [`technical-notes.md`](technical-notes.md).

<p align="center">
  <img src="images/homepage.png" alt="Main page screenshot" width="80%">
</p>


## Features

- **Regional overview** — the main page shows total COVID-19 cases aggregated by region for the current day, sorted from the highest to the lowest, with region name as an alphabetical tiebreaker.
- **Search by date** — look up any day between 24/02/2020 and today.
- **Sortable results** — change the sort order (total cases or region name, ascending or descending) directly from the browser.
- **Export to .xls** — download the exact data currently shown on the page as a two-column Excel spreadsheet.

Date and sort order are both optional and independent: pick a date, a sort order, or both, then click **Search** to apply them.

**Note on the data source:** the source repository stopped publishing new data on 08/01/2025. The application detects this automatically at startup (checking for updates without re-downloading the full dataset) and, when "today" has no data, falls back to the most recent available date — stating this explicitly on the page rather than silently showing stale data as current.

## Tech stack

- **FastAPI** + **Uvicorn** — web framework and ASGI server
- **PostgreSQL** + **SQLAlchemy** — database and ORM
- **Jinja2** — server-side HTML templates (no frontend framework: the app is a single page with a form and a table, which doesn't warrant one)
- **xlwt** — generates real legacy `.xls` files, not `.xlsx`
- **httpx** — downloads the dataset from GitHub
- **rich** — readable, color-coded console output during startup and data ingestion
- **pytest** — automated test suite
- **Docker** / **Docker Compose** — containerized database and application

## Getting started

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) only — no local Python installation needed.

```bash
git clone <this-repository-url>
cd <repository-folder>
cp .env.example .env
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000). On first run, the application creates the database schema and downloads the full historical dataset (~110 MB) automatically — this takes a few seconds. Subsequent runs skip this step, checking for new data with a much smaller request instead.

## Running the tests

Tests run against a temporary, in-memory-backed SQLite database with fixed sample data — no network access or running Postgres instance required.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements-dev.txt
pytest tests/ -v
```

25 tests cover aggregation and sorting logic, date/sort parameter validation, the `.xls` export, full HTTP routes, and security checks (SQL injection attempts, XSS payloads, and consistency between the page and the export for identical parameters).

## Notable technical decisions

- **Region codes for the two autonomous provinces (Bolzano/Trento)** are normalized to the codes used in the official regional dataset (`21`/`22`), because the per-province file switches to a single ambiguous code (`4`) for both after 25/06/2020.
- **Sort order is a closed whitelist** (a Python `Enum`), never a raw string interpolated into SQL — the primary defense against injection on that parameter.
- **Database connection retries with a bounded timeout** at startup: without an explicit timeout, an unreachable database can hang for a duration that depends on the operating system's own defaults, which vary widely between Linux and Windows.
- **The `/export` route reuses the exact same date/sort resolution logic as the main page**, so the downloaded file always matches what's currently shown — never a fixed default regardless of the current search.
- **Unhandled errors** are logged in full server-side but shown to the client only as a generic page, to avoid leaking internal details (verified explicitly with a forced error containing a database credential, confirming it never reaches the client).

## Project structure

```
app/
├── main.py              # FastAPI app, startup lifecycle, routes, error handlers
├── config.py             # Environment-based configuration
├── database.py            # SQLAlchemy engine/session setup
├── models.py               # ORM models: Region, Province, ProvinceCase
├── repository.py            # Aggregation queries, sorting, date resolution
├── services/
│   ├── ingestion.py           # Download, validate, and load data from GitHub
│   └── export.py                # .xls file generation
└── templates/
    ├── index.html                # Main page
    ├── error_404.html             # Custom 404 page
    └── error_500.html              # Custom 500 page

tests/                    # Automated test suite (pytest)
docker-compose.yml         # Postgres + application services
Dockerfile                  # Application image
requirements.txt             # Runtime dependencies
requirements-dev.txt          # Adds pytest for local testing
```
