"""Download the historical per-province JSON from the Civil Protection
GitHub repository, validate every record, and normalize it according
to the rules defined during data analysis (see design notes)."""

import datetime

import httpx
from rich.console import Console
from rich.progress import Progress
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase, Session

from app.models import ProvinceCase, Province, Region

console = Console()

BATCH_SIZE = 5000

GITHUB_URL = (
    "https://raw.githubusercontent.com/pcm-dpc/COVID-19/master/"
    "dati-json/dpc-covid19-ita-province.json"
)

# Small file (~60 KB, vs. ~109 MB for the full historical file): used
# only to quickly check whether new days are available, without
# re-downloading the entire historical series on every startup.
GITHUB_LATEST_URL = (
    "https://raw.githubusercontent.com/pcm-dpc/COVID-19/master/"
    "dati-json/dpc-covid19-ita-province-latest.json"
)

# The two autonomous province codes are normalized to the codes used
# in the official regional file (21/22), which are stable across the
# whole historical series -- the per-province file instead switches to
# the aggregate code 4 from 25/06/2020 onward, ambiguous because it
# denotes both Bolzano and Trento.
REGION_CODE_OVERRIDES = {"P.A. Bolzano": 21, "P.A. Trento": 22}


def fetch_data() -> list[dict]:
    """Download the raw JSON from GitHub.

    Returns:
        list[dict]: The raw list of per-province, per-day records.

    Raises:
        httpx.HTTPStatusError: If the request fails.
    """
    response = httpx.get(GITHUB_URL, timeout=30.0)
    response.raise_for_status()
    return response.json()


def _normalize_region_code(region_code: int, region_name: str) -> int:
    """Map a raw region code to its canonical value.

    Args:
        region_code (int): The raw `codice_regione` from the source record.
        region_name (str): The corresponding `denominazione_regione`.

    Returns:
        int: `region_code` unchanged, unless `region_name` is one of
            the two autonomous provinces, in which case the canonical
            21/22 code is returned instead.
    """
    return REGION_CODE_OVERRIDES.get(region_name, region_code)


def _is_valid_province_code(code: int) -> bool:
    """Check whether a province code falls in one of the three ranges
    documented by the source: real provinces, out-of-region, or
    under-definition placeholders.

    Args:
        code (int): The `codice_provincia` to validate.

    Returns:
        bool: True if `code` is in 1-111, 879-899, or 979-999.
    """
    return (1 <= code <= 111) or (879 <= code <= 899) or (979 <= code <= 999)


def validate_and_normalize(records: list[dict]) -> dict:
    """Validate and normalize raw records, grouped by destination table.

    Args:
        records (list[dict]): Raw records as returned by `fetch_data`.

    Returns:
        dict: A dict with four keys:

            - "regions" (list[dict]): One entry per unique region, with
              keys matching the `Region` model's columns.
            - "provinces" (list[dict]): One entry per unique province,
              with keys matching the `Province` model's columns.
            - "cases" (list[dict]): One entry per valid record, with
              keys matching the `ProvinceCase` model's columns.
            - "rejected" (list[tuple[str, dict]]): (reason, record)
              pairs for records that failed validation.
    """
    regions: dict[int, dict] = {}
    provinces: dict[int, dict] = {}
    cases: list[dict] = []
    rejected: list[tuple[str, dict]] = []

    for record in records:
        province_code = record.get("codice_provincia")
        total_cases = record.get("totale_casi")

        if province_code is None or not _is_valid_province_code(province_code):
            rejected.append(("invalid province_code", record))
            continue
        if total_cases is None or total_cases < 0:
            rejected.append(("missing or negative total_cases", record))
            continue
        if not record.get("data") or not record.get("denominazione_regione"):
            rejected.append(("missing required field", record))
            continue

        region_code = _normalize_region_code(
            record["codice_regione"], record["denominazione_regione"]
        )

        if region_code not in regions:
            regions[region_code] = {
                "region_code": region_code,
                "region_name": record["denominazione_regione"],
                "nuts_1_code": record.get("codice_nuts_1"),
                "nuts_2_code": record.get("codice_nuts_2"),
            }
        else:
            # Some NUTS fields were absent in the first few months: if a
            # later row has them filled in, complete the record.
            if not regions[region_code]["nuts_1_code"]:
                regions[region_code]["nuts_1_code"] = record.get("codice_nuts_1")
            if not regions[region_code]["nuts_2_code"]:
                regions[region_code]["nuts_2_code"] = record.get("codice_nuts_2")

        if province_code not in provinces:
            provinces[province_code] = {
                "province_code": province_code,
                "province_name": record["denominazione_provincia"],
                "province_abbreviation": record.get("sigla_provincia"),
                "region_code": region_code,
                "latitude": record.get("lat"),
                "longitude": record.get("long"),
                "nuts_3_code": record.get("codice_nuts_3"),
            }
        elif not provinces[province_code]["nuts_3_code"]:
            provinces[province_code]["nuts_3_code"] = record.get("codice_nuts_3")

        cases.append(
            {
                "date": datetime.date.fromisoformat(record["data"][:10]),
                "province_code": province_code,
                "total_cases": total_cases,
                "notes": record.get("note") or None,
            }
        )

    return {
        "regions": list(regions.values()),
        "provinces": list(provinces.values()),
        "cases": cases,
        "rejected": rejected,
    }


def _upsert_statement(session: Session, model: type[DeclarativeBase]):
    """Build an INSERT statement that ignores primary key conflicts.

    Uses the dialect matching the connected database (postgres in
    production, sqlite in tests), so re-running ingestion never
    duplicates rows that are already present.

    Args:
        session (Session): Active database session, used to detect
            the SQL dialect in use.
        model (type[DeclarativeBase]): The ORM model class to insert
            into (Region, Province, or ProvinceCase).

    Returns:
        A dialect-specific Insert construct with `.on_conflict_do_nothing()`
        available (the exact type depends on the dialect, hence not
        annotated more precisely).
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as insert_stmt
    else:
        from sqlalchemy.dialects.sqlite import insert as insert_stmt
    return insert_stmt(model)


def needs_ingestion(session: Session) -> bool:
    """Check whether the case table is empty.

    Args:
        session (Session): Active database session.

    Returns:
        bool: True if the app has never loaded data from GitHub yet.
    """
    count = session.execute(select(func.count()).select_from(ProvinceCase)).scalar()
    return count == 0


def check_for_updates(session: Session) -> bool:
    """Cheap check: download only the 'latest' file (~60 KB) and compare
    it against the most recent date already in the database.

    Args:
        session (Session): Active database session.

    Returns:
        bool: True if GitHub has published days we don't have yet.
    """
    response = httpx.get(GITHUB_LATEST_URL, timeout=15.0)
    response.raise_for_status()
    latest_records = response.json()

    if not latest_records:
        return False

    remote_latest_date = datetime.date.fromisoformat(latest_records[0]["data"][:10])
    local_latest_date = session.execute(select(func.max(ProvinceCase.date))).scalar()

    return local_latest_date is None or remote_latest_date > local_latest_date


def load_into_database(session: Session, validated: dict) -> None:
    """Write regions, provinces, and cases to the database, in this
    order (foreign keys require it: regions first, then the provinces
    that reference them, then the cases that reference the provinces).

    Args:
        session (Session): Active database session.
        validated (dict): The dict returned by `validate_and_normalize`.
    """

    console.print("[cyan]Writing data to the database...[/cyan]")

    session.execute(_upsert_statement(session, Region).on_conflict_do_nothing(), validated["regions"])
    console.print(f"[bold green]✓[/bold green] Regions written to database: {len(validated['regions'])}")

    session.execute(_upsert_statement(session, Province).on_conflict_do_nothing(), validated["provinces"])
    console.print(f"[bold green]✓[/bold green] Provinces written to database: {len(validated['provinces'])}")

    cases = validated["cases"]
    with Progress(console=console) as progress:
        task = progress.add_task(
            "[yellow]Writing daily case records to database...", total=len(cases)
        )
        for i in range(0, len(cases), BATCH_SIZE):
            batch = cases[i : i + BATCH_SIZE]
            session.execute(_upsert_statement(session, ProvinceCase).on_conflict_do_nothing(), batch)
            progress.update(task, advance=len(batch))

    session.commit()
    console.print(f"[bold green]✓[/bold green] Case records written to database: {len(cases)}")


def run_ingestion(session: Session) -> None:
    """Orchestrate the full pipeline: download, validate, load.

    Called from the lifespan only if the database is empty, or if
    `check_for_updates` found new data available.

    Args:
        session (Session): Active database session.
    """
    console.print("[cyan]Downloading data from GitHub...[/cyan]")
    raw_data = fetch_data()
    console.print(f"[cyan]Downloaded {len(raw_data)} raw records.[/cyan]")

    result = validate_and_normalize(raw_data)
    if result["rejected"]:
        console.print(
            f"[bold yellow]⚠[/bold yellow] {len(result['rejected'])} rows rejected during validation"
        )

    load_into_database(session, result)


if __name__ == "__main__":
    # Manual run: python -m app.services.ingestion
    from app.database import SessionLocal

    with SessionLocal() as session:
        if needs_ingestion(session):
            run_ingestion(session)
        elif check_for_updates(session):
            console.print("[yellow]New data available on GitHub: updating...[/yellow]")
            run_ingestion(session)
        else:
            console.print(
                "[cyan]Database already up to date: no ingestion needed.[/cyan]"
            )