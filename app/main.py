"""FastAPI entrypoint: application setup, startup/shutdown lifecycle,
error handling, and the two HTTP routes (page and export)."""

import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from rich.console import Console
from rich.traceback import Traceback
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.database import Base, SessionLocal, engine
from app import models  # noqa: F401 -- required: registers the classes on Base.metadata
from app.repository import format_date, resolve_target_date
from app.services.export import generate_xls
from app.services.ingestion import check_for_updates, needs_ingestion, run_ingestion

console = Console()


def wait_for_database(attempts: int = 3, wait_seconds: float = 2.0) -> None:
    """Retry connecting to the database before giving up.

    Useful if the app starts before Postgres is ready to accept
    connections -- locally (no healthcheck to protect you) or as an
    extra safety margin even inside Docker Compose.

    If every attempt fails, terminates the process directly (os._exit)
    instead of re-raising: this keeps the clear message just printed
    as the last visible thing, without SQLAlchemy's internal traceback
    printed on top by Uvicorn.

    Args:
        attempts (int): Maximum number of connection attempts.
        wait_seconds (float): Delay between attempts, in seconds.
    """
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect():
                return
        except OperationalError:
            if attempt == attempts:
                console.print(
                    f"\n[bold red]✗ Could not connect to the database after "
                    f"{attempts} attempts.[/bold red]"
                )
                console.print(
                    "[red]Check that the Postgres container is running: "
                    "run 'docker compose ps' and check the status of the 'db' service.[/red]\n"
                )
                sys.stderr.flush()
                sys.stdout.flush()
                os._exit(1)
            console.print(
                f"[yellow]Database not reachable yet "
                f"(attempt {attempt}/{attempts}), retrying in {wait_seconds:.0f}s...[/yellow]"
            )
            time.sleep(wait_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run once at server startup and shutdown, never on individual requests.

    On startup: wait for the database, create tables if missing, then
    populate them from GitHub if empty, or check for updates otherwise.

    Args:
        app (FastAPI): The application instance (required by FastAPI's
            lifespan protocol; not used directly here).
    """
    console.print("\n[bold cyan]→ Starting COVID-19 Regions application[/bold cyan]")

    wait_for_database()

    expected_tables = set(Base.metadata.tables.keys())
    existing_tables = set(inspect(engine).get_table_names())
    missing_tables = expected_tables - existing_tables

    if missing_tables:
        with console.status(
            f"[yellow]Creating tables: {', '.join(sorted(missing_tables))}...[/yellow]"
        ):
            Base.metadata.create_all(bind=engine)
        console.print(
            f"[bold green]✓[/bold green] Created {len(missing_tables)} new tables: "
            f"{', '.join(sorted(missing_tables))}"
        )
    else:
        console.print(
            f"[bold green]✓[/bold green] Tables already present "
            f"({len(expected_tables)}): {', '.join(sorted(expected_tables))}"
        )

    # Initial population if the database is empty, otherwise a cheap
    # check (~60 KB) for whether GitHub has published new days.
    with SessionLocal() as session:
        if needs_ingestion(session):
            console.print("[yellow]Database empty: starting initial population...[/yellow]")
            run_ingestion(session)
        elif check_for_updates(session):
            console.print("[yellow]New data available on GitHub: updating...[/yellow]")
            run_ingestion(session)
        else:
            console.print("[bold green]✓[/bold green] Data already up to date, no download needed")

    console.print("[bold green]✓[/bold green] Application ready\n")

    yield

    console.print("\n[bold cyan]→ Shutting down application[/bold cyan]")


app = FastAPI(title="COVID-19 Regions by Province", lifespan=lifespan)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Show a styled page for 404 errors; keep the default JSON for anything else.

    Args:
        request (Request): The incoming request (needed by Jinja2Templates).
        exc (StarletteHTTPException): The raised HTTP exception.

    Returns:
        Response: The error_404.html page for a 404, or the default
            JSON response for any other HTTP status code.
    """
    if exc.status_code == 404:
        return templates.TemplateResponse(
            request=request, name="error_404.html", status_code=404
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(OperationalError)
async def database_error_handler(request: Request, exc: OperationalError) -> Response:
    """Handle a database that becomes unreachable while the app is
    already running (e.g. the 'db' container is stopped mid-session) --
    distinct from wait_for_database, which only covers startup.

    More specific than the generic Exception handler below, so it takes
    precedence for this particular failure: a 503 with a clear message,
    not a generic 500 that implies a bug in the application itself.

    Args:
        request (Request): The incoming request (needed by Jinja2Templates).
        exc (OperationalError): The database connection failure.

    Returns:
        Response: The error_503.html page, with status 503.
    """
    console.print(f"[bold red]✗ Database error while handling a request:[/bold red] {exc}")
    return templates.TemplateResponse(
        request=request, name="error_503.html", status_code=503
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Catch-all for unexpected errors.

    The client only ever sees a clean page, with no internal details --
    but whoever runs the server still needs to see what really
    happened, so the full traceback is printed here, never sent back.

    Args:
        request (Request): The incoming request (needed by Jinja2Templates).
        exc (Exception): The unhandled exception that was raised.

    Returns:
        Response: The error_500.html page, with status 500.
    """
    console.print(Traceback.from_exception(type(exc), exc, exc.__traceback__))
    return templates.TemplateResponse(
        request=request, name="error_500.html", status_code=500
    )


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request, date: str | None = None, sort: str | None = None
) -> HTMLResponse:
    """Main page: total cases by region for a date, sorted (Task #1/#2/#4).

    Args:
        request (Request): The incoming request (needed by Jinja2Templates).
        date (str | None): Optional `date` query parameter (ISO format).
        sort (str | None): Optional `sort` query parameter.

    Returns:
        HTMLResponse: The rendered page, with the results table for the
            resolved date/sort, or an inline error banner if either
            was invalid.
    """
    with SessionLocal() as session:
        result = resolve_target_date(session, date, sort)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "shown_date": format_date(result.target_date),
            "shown_date_iso": result.target_date.isoformat(),
            "fallback": result.fallback,
            "error": result.error,
            "sort_error": result.sort_error,
            "min_date": format_date(result.earliest_date) if result.earliest_date else None,
            "max_date": format_date(result.latest_date) if result.latest_date else None,
            "min_date_iso": result.earliest_date.isoformat() if result.earliest_date else None,
            "max_date_iso": result.latest_date.isoformat() if result.latest_date else None,
            "sort_order": result.sort_order.value,
            "results": result.results,
        },
    )


@app.get("/export")
def export_xls(date: str | None = None, sort: str | None = None) -> Response:
    """Download the same data shown on the page as an .xls file (Task #3).

    Reuses `resolve_target_date`, so the export always matches the page
    for the same `date`/`sort` parameters.

    Args:
        date (str | None): Optional `date` query parameter (ISO format).
        sort (str | None): Optional `sort` query parameter.

    Returns:
        Response: The .xls file as a downloadable attachment.
    """
    with SessionLocal() as session:
        result = resolve_target_date(session, date, sort)

    content = generate_xls(result.results)
    file_name = f"covid19-regions-{result.target_date.isoformat()}.xls"

    return Response(
        content=content,
        media_type="application/vnd.ms-excel",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )