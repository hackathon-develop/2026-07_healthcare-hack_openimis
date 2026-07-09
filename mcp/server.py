"""
openIMIS MCP Server
--------------------
Exposes a small, fixed set of read-only, parameterized tools for querying an
openIMIS PostgreSQL database over MCP (streamable HTTP transport, via FastMCP).

SECURITY MODEL (read this before deploying):
- Connects with a dedicated, least-privilege, READ-ONLY Postgres role.
- No tool accepts raw SQL. Every query below is a fixed, parameterized
  statement — the LLM can only fill in parameters, never structure.
- Result sets are capped (LIMIT) to avoid dumping the whole database.
- Consider adding an audit log (see log_call helper) writing to a separate
  append-only store, since this data is sensitive (PII + health/insurance data).

TABLE NAMES:
openIMIS schema differs by version. This file is written for the LEGACY
schema (MSSQL-derived, mixed-case table/column names requiring double quotes
in Postgres): "tblInsuree", "tblPolicy", "tblClaim", "tblHF", "tblFamilies".

If you're on the newer modular/Django backend, table names instead look like
insuree_insuree, policy_policy, claim_claim, location_healthfacility (all
lowercase, Django app_label + model name). Run `\\dt` in psql against your DB
and adjust the SQL strings in the functions below accordingly — the tool
signatures and overall structure won't need to change, just the query text.
"""

import os
import logging
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()  # loads .env into os.environ if present; no-op if the file is missing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("openimis-mcp")

PORT = int(os.environ.get("PORT", 8080))

mcp = FastMCP(
    "openimis-mcp",
    stateless_http=True,  # required for correctness once Cloud Run scales beyond 1 instance
    host="0.0.0.0",
    port=PORT,
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    # Plain HTTP endpoint alongside the /mcp endpoint. Cloud Run's default
    # readiness check is just a TCP check on $PORT, so this isn't required,
    # but it's useful if you later add a custom startup probe, or just want
    # a quick `curl` to confirm the container is up without speaking MCP.
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})

DB_CONFIG = {
    "host": os.environ.get("OPENIMIS_DB_HOST", "localhost"),
    "port": os.environ.get("OPENIMIS_DB_PORT", "5432"),
    "dbname": os.environ.get("OPENIMIS_DB_NAME", "openimis"),
    "user": os.environ.get("OPENIMIS_DB_USER", "openimis_readonly"),
    "password": os.environ.get("OPENIMIS_DB_PASSWORD"),
    "sslmode": os.environ.get("OPENIMIS_DB_SSLMODE", "prefer"),
}
# If your Postgres is Cloud SQL and you're connecting via the Cloud SQL Unix
# socket (recommended over a public IP), set OPENIMIS_DB_HOST to
# "/cloudsql/PROJECT_ID:REGION:INSTANCE_NAME" instead of a hostname — psycopg2
# treats a host value starting with "/" as a socket directory automatically,
# so no other code changes are needed. See the deploy command below for the
# matching --add-cloudsql-instances flag.
#
# If your database is OUTSIDE Google Cloud entirely (on-prem, another cloud,
# a hosted Postgres provider) and reachable only over the public internet,
# set OPENIMIS_DB_SSLMODE=require (or verify-full, if you also configure a
# root cert) — never leave it at "prefer" for traffic crossing the public
# internet, since "prefer" silently falls back to an unencrypted connection
# if the server doesn't offer TLS.

MAX_ROWS = 50          # hard cap for record-level results (may contain PII)
MAX_ROWS_AGGREGATE = 180  # higher cap for pure count/aggregate results (no PII)


@contextmanager
def get_connection():
    """Short-lived, read-only connection. Never reused across requests."""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.set_session(readonly=True, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def run_query(sql: str, params: tuple, max_rows: int = MAX_ROWS) -> list[dict]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)  # always parameterized, never f-strings
            rows = cur.fetchmany(max_rows)
            return [dict(row) for row in rows]


def log_call(tool_name: str, **kwargs):
    # Minimal audit trail. In production, write this to a separate,
    # append-only log store (e.g. Cloud Logging) rather than stdout.
    logger.info("tool_call=%s args=%s", tool_name, kwargs)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_insuree(chf_id: Optional[str] = None, last_name: Optional[str] = None) -> list[dict]:
    """
    Search for an insuree (insured person) by CHF ID (exact) or last name (partial).
    Returns basic identifying info only — not full medical/claim history.

    Args:
        chf_id: Exact CHF/insuree identification number.
        last_name: Partial, case-insensitive match on last name.
    """
    log_call("search_insuree", chf_id=chf_id, last_name=last_name)

    if chf_id:
        sql = '''
            SELECT "CHFID" AS chf_id, "LastName" AS last_name, "OtherNames" AS other_names,
                   "DOB" AS date_of_birth, "Gender" AS gender
            FROM "tblInsuree"
            WHERE "CHFID" = %s AND "ValidityTo" IS NULL
        '''
        return run_query(sql, (chf_id,))
    elif last_name:
        sql = '''
            SELECT "CHFID" AS chf_id, "LastName" AS last_name, "OtherNames" AS other_names,
                   "DOB" AS date_of_birth, "Gender" AS gender
            FROM "tblInsuree"
            WHERE "LastName" ILIKE %s AND "ValidityTo" IS NULL
            ORDER BY "LastName"
        '''
        return run_query(sql, (f"%{last_name}%",))
    else:
        raise ValueError("Provide either chf_id or last_name")


@mcp.tool()
def get_active_policies(chf_id: str) -> list[dict]:
    """
    Get currently active insurance policies for an insuree, by CHF ID.

    Args:
        chf_id: Exact CHF/insuree identification number.
    """
    log_call("get_active_policies", chf_id=chf_id)
    sql = '''
        SELECT p."PolicyUUID" AS policy_uuid, pr."ProductCode" AS product_code,
               pr."ProductName" AS product_name, p."EffectiveDate" AS effective_date,
               p."ExpiryDate" AS expiry_date, p."PolicyStatus" AS status
        FROM "tblPolicy" p
        JOIN "tblInsureePolicy" ip ON ip."PolicyId" = p."PolicyID"
        JOIN "tblInsuree" i ON i."InsureeID" = ip."InsureeID"
        JOIN "tblProduct" pr ON pr."ProdID" = p."ProdID"
        WHERE i."CHFID" = %s
          AND p."ValidityTo" IS NULL
          -- AND p."ExpiryDate" >= CURRENT_DATE
          AND p."PolicyStatus" = 2
        ORDER BY p."ExpiryDate" DESC
    '''
    return run_query(sql, (chf_id,))


@mcp.tool()
def get_claims_for_insuree(chf_id: str, start_date: str, end_date: str) -> list[dict]:
    """
    Get claims for an insuree within a date range.

    Args:
        chf_id: Exact CHF/insuree identification number.
        start_date: ISO date (YYYY-MM-DD), inclusive.
        end_date: ISO date (YYYY-MM-DD), inclusive.
    """
    log_call("get_claims_for_insuree", chf_id=chf_id, start_date=start_date, end_date=end_date)
    sql = '''
        SELECT c."ClaimCode" AS claim_code, c."DateClaimed" AS date_claimed,
               c."ClaimStatus" AS status, c."Approved" AS approved_amount,
               hf."HFName" AS health_facility
        FROM "tblClaim" c
        JOIN "tblInsuree" i ON i."InsureeID" = c."InsureeID"
        JOIN "tblHF" hf ON hf."HfID" = c."HFID"
        WHERE i."CHFID" = %s
          AND c."DateClaimed" BETWEEN %s AND %s
          AND c."ValidityTo" IS NULL
        ORDER BY c."DateClaimed" DESC
    '''
    return run_query(sql, (chf_id, start_date, end_date))


@mcp.tool()
def list_health_facilities(district: Optional[str] = None) -> list[dict]:
    """
    List health facilities, optionally filtered by district name.

    Args:
        district: Optional partial, case-insensitive district name filter.
    """
    log_call("list_health_facilities", district=district)
    if district:
        sql = '''
            SELECT hf."HFCode" AS code, hf."HFName" AS name, hf."HFLevel" AS level,
                   loc."LocationName" AS district
            FROM "tblHF" hf
            JOIN "tblLocations" loc ON loc."LocationId" = hf."LocationId"
            WHERE loc."LocationName" ILIKE %s AND hf."ValidityTo" IS NULL
            ORDER BY hf."HFName"
        '''
        return run_query(sql, (f"%{district}%",))
    else:
        sql = '''
            SELECT hf."HFCode" AS code, hf."HFName" AS name, hf."HFLevel" AS level
            FROM "tblHF" hf
            WHERE hf."ValidityTo" IS NULL
            ORDER BY hf."HFName"
        '''
        return run_query(sql, ())


@mcp.tool()
def get_claims_trend_by_facility(
    period_days: int = 14,
    end_date: Optional[str] = None,
    min_claims: int = 5,
    top_n: int = 15,
) -> list[dict]:
    """
    Find health facilities where claim volume is changing fastest, by
    comparing two adjacent time windows: the most recent `period_days` days
    vs. the `period_days` days immediately before that. Returns facilities
    sorted by absolute increase in claim count (largest increase first) —
    use this to answer questions like "where are claims increasing fast?".

    Returns only facility identifiers and counts, no patient-level data.

    Args:
        period_days: Length of each comparison window, in days. Default 14
            (a two-week-over-two-week comparison). Use a larger value (e.g.
            30) for a slower-moving, less noisy signal.
        end_date: ISO date (YYYY-MM-DD) marking the end of the "recent"
            window, exclusive. Defaults to today.
        min_claims: Minimum combined claim count (recent + previous) for a
            facility to be included. Filters out noise from very low-volume
            facilities, where a jump from 1 to 3 claims looks like a 200%
            increase but isn't meaningful.
        top_n: Max number of facilities to return (capped at 50).
    """
    top_n = min(max(top_n, 1), 50)
    end = date.fromisoformat(end_date) if end_date else date.today()
    recent_start = end - timedelta(days=period_days)
    prev_start = end - timedelta(days=2 * period_days)

    log_call(
        "get_claims_trend_by_facility",
        period_days=period_days, end_date=str(end), min_claims=min_claims, top_n=top_n,
    )

    sql = '''
        WITH recent AS (
            SELECT c."HFID" AS hf_id, COUNT(*) AS cnt
            FROM "tblClaim" c
            WHERE c."DateClaimed" >= %s AND c."DateClaimed" < %s
              AND c."ValidityTo" IS NULL
            GROUP BY c."HFID"
        ),
        previous AS (
            SELECT c."HFID" AS hf_id, COUNT(*) AS cnt
            FROM "tblClaim" c
            WHERE c."DateClaimed" >= %s AND c."DateClaimed" < %s
              AND c."ValidityTo" IS NULL
            GROUP BY c."HFID"
        )
        SELECT hf."HFCode" AS code, hf."HFName" AS name,
               COALESCE(r.cnt, 0) AS claims_recent,
               COALESCE(p.cnt, 0) AS claims_previous,
               COALESCE(r.cnt, 0) - COALESCE(p.cnt, 0) AS absolute_change,
               CASE WHEN COALESCE(p.cnt, 0) = 0 THEN NULL
                    ELSE ROUND((COALESCE(r.cnt, 0) - COALESCE(p.cnt, 0))::numeric / p.cnt * 100, 1)
               END AS pct_change
        FROM "tblHF" hf
        LEFT JOIN recent r ON r.hf_id = hf."HfID"
        LEFT JOIN previous p ON p.hf_id = hf."HfID"
        WHERE hf."ValidityTo" IS NULL
          AND (COALESCE(r.cnt, 0) + COALESCE(p.cnt, 0)) >= %s
        ORDER BY absolute_change DESC
        LIMIT %s
    '''
    params = (recent_start, end, prev_start, recent_start, min_claims, top_n)
    return run_query(sql, params, max_rows=top_n)


@mcp.tool()
def get_daily_claims_for_facility(hf_code: str, start_date: str, end_date: str) -> list[dict]:
    """
    Get a day-by-day claim count for a single health facility. Use this to
    drill into a trend flagged by get_claims_trend_by_facility and see its
    actual shape over time (steady climb vs. a single spike day, etc).

    Returns only dates and counts, no patient-level data.

    Args:
        hf_code: Health facility code (from list_health_facilities or
            get_claims_trend_by_facility).
        start_date: ISO date (YYYY-MM-DD), inclusive.
        end_date: ISO date (YYYY-MM-DD), inclusive. Keep the range to
            roughly 6 months or less — results are capped at 180 rows (one
            per day) and will silently truncate beyond that.
    """
    log_call("get_daily_claims_for_facility", hf_code=hf_code, start_date=start_date, end_date=end_date)
    sql = '''
        SELECT c."DateClaimed"::date AS claim_date, COUNT(*) AS claim_count
        FROM "tblClaim" c
        JOIN "tblHF" hf ON hf."HfID" = c."HFID"
        WHERE hf."HFCode" = %s
          AND c."DateClaimed" BETWEEN %s AND %s
          AND c."ValidityTo" IS NULL
        GROUP BY c."DateClaimed"::date
        ORDER BY claim_date
    '''
    return run_query(sql, (hf_code, start_date, end_date), max_rows=MAX_ROWS_AGGREGATE)


if __name__ == "__main__":
    # host/port are configured on the FastMCP constructor above, not here —
    # passing them to run() directly raises TypeError on current SDK versions.
    mcp.run(transport="streamable-http")