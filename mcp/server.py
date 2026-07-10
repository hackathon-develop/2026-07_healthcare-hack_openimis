"""
openIMIS MCP Server
--------------------
Exposes a small, fixed set of tools for querying — and, for claims,
creating — data in an openIMIS installation, over MCP (streamable HTTP
transport, via FastMCP).

TWO DIFFERENT BACKENDS ARE USED HERE, ON PURPOSE:
- Read tools (search_insuree, get_active_policies, etc.) talk directly to
  the openIMIS PostgreSQL database with a read-only role.
- Claim creation talks to openIMIS's own GraphQL API instead of the
  database. A raw SQL insert into tblClaim would skip openIMIS's own
  pricing, ceiling, and policy-coverage validation, its claim code
  generation, and its audit trail (mutation log) — going through
  createClaim/submitClaim preserves all of that.

SECURITY MODEL (read this before deploying):
- DB reads use a dedicated, least-privilege, READ-ONLY Postgres role.
- No read tool accepts raw SQL. Every query is a fixed, parameterized
  statement — the LLM can only fill in parameters, never structure.
- Claim creation uses a SEPARATE openIMIS technical user account, scoped to
  only the create_claim (111002) and submit_claim (111007) rights — never
  an admin account. Configure this in openIMIS's own user/role admin.
- Result sets from read tools are capped (LIMIT) to avoid dumping the
  whole database.
- Consider adding an audit log (see log_call helper) writing to a separate
  append-only store, since this data is sensitive (PII + health/insurance data).

TABLE NAMES:
openIMIS schema differs by version. This file is written for the LEGACY
schema (MSSQL-derived, mixed-case table/column names requiring double quotes
in Postgres): "tblInsuree", "tblPolicy", "tblClaim", "tblHF", "tblFamilies".
The diagnosis/medical service/medical item table and column names (including
"tblICDCodes", "tblServices", "tblItems", "tblClaimItems") are
a BEST GUESS based on common legacy openIMIS naming conventions and are NOT
verified against a live instance — check them with `\\dt`/`\\d` against your
own database before relying on the tools that use them.

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
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(override=False)  # real environment variables always win; .env only
                              # fills in whatever isn't already set in the environment

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

# ---------------------------------------------------------------------------
# Database config (read tools)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# openIMIS GraphQL API config (claim creation only)
# ---------------------------------------------------------------------------

OPENIMIS_GRAPHQL_URL = os.environ.get("OPENIMIS_GRAPHQL_URL")
OPENIMIS_TECH_USER = os.environ.get("OPENIMIS_TECH_USER")
OPENIMIS_TECH_PASSWORD = os.environ.get("OPENIMIS_TECH_PASSWORD")
# This should be a dedicated openIMIS TechnicalUser (or interactive user)
# scoped to ONLY the create_claim and submit_claim rights (111002, 111007
# by default — confirm in your instance's role admin). Never point this at
# an admin account.


@contextmanager
def get_connection():
    """Short-lived, read-only Postgres connection. Never reused across requests."""
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
    # append-only log store (e.g. Cloud Logging) rather than stdout — and
    # for the claim-creation tools especially, since this is a write action
    # against health/insurance data.
    logger.info("tool_call=%s args=%s", tool_name, kwargs)


# Shared across calls so the CSRF cookie fetched by _get_csrf_token() is
# still attached when _openimis_graphql() sends the matching X-CSRFToken
# header — a fresh session per call would never have the cookie.
_http_session = requests.Session()


def _get_csrf_token() -> Optional[str]:
    """
    Prime _http_session with a Django CSRF cookie. Mutations on this
    instance are CSRF-protected even though they're JWT-authenticated, so
    without this, createClaim/submitClaim fail with "CSRF token missing or
    incorrect."

    The GraphQL endpoint itself is POST-only here (a GET returns 415, so it
    doesn't render GraphiQL/set a cookie) — instead, hit the Django admin
    login page on the same origin, whose template always renders
    {% csrf_token %} and triggers the Set-Cookie.

    Returns None (rather than raising) if no cookie shows up — some
    deployments may not need this, and callers fall back to sending no
    X-CSRFToken header in that case.
    """
    parsed = urlsplit(OPENIMIS_GRAPHQL_URL)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in ("/admin/login/", "/"):
        try:
            _http_session.get(origin + path, timeout=15)
        except requests.RequestException:
            continue
        token = _http_session.cookies.get("csrftoken")
        if token:
            return token
    return None


def _get_openimis_token() -> str:
    """
    Authenticate against openIMIS's own GraphQL API and return a JWT.

    Fetched fresh on every call rather than cached/refreshed — claim
    creation is a low-frequency write path, so simplicity wins over the
    small efficiency cost of re-authenticating each time.
    """
    if not OPENIMIS_GRAPHQL_URL:
        raise RuntimeError(
            "OPENIMIS_GRAPHQL_URL is not configured — set OPENIMIS_GRAPHQL_URL, "
            "OPENIMIS_TECH_USER, and OPENIMIS_TECH_PASSWORD in .env"
        )
    mutation = '''
        mutation TokenAuth($username: String!, $password: String!) {
            tokenAuth(username: $username, password: $password) {
                token
            }
        }
    '''
    csrf_token = _get_csrf_token()
    headers = {"X-CSRFToken": csrf_token, "Referer": OPENIMIS_GRAPHQL_URL} if csrf_token else {}
    resp = _http_session.post(
        OPENIMIS_GRAPHQL_URL,
        json={"query": mutation, "variables": {"username": OPENIMIS_TECH_USER, "password": OPENIMIS_TECH_PASSWORD}},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data or not data.get("data", {}).get("tokenAuth"):
        raise RuntimeError(f"openIMIS authentication failed: {data.get('errors', data)}")
    return data["data"]["tokenAuth"]["token"]


def _openimis_graphql(query: str, variables: dict) -> dict:
    """Run an authenticated GraphQL request against openIMIS's own API."""
    token = _get_openimis_token()
    csrf_token = _http_session.cookies.get("csrftoken")
    headers = {
        # django-graphql-jwt historically expects the "JWT" prefix rather
        # than "Bearer" — if you get auth errors despite a valid token,
        # try changing this to f"JWT {token}".
        "Authorization": f"Bearer {token}",
    }
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token
        headers["Referer"] = OPENIMIS_GRAPHQL_URL
    resp = _http_session.post(
        OPENIMIS_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=30,
    )
    if resp.status_code >= 400:
        # Surface the response body (often a GraphQL "errors" list with the
        # actual validation/auth message) instead of letting raise_for_status
        # hide it behind a bare "400 Client Error".
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"openIMIS GraphQL request failed ({resp.status_code}): {detail}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"openIMIS GraphQL error: {data['errors']}")
    return data["data"]


def _unwrap_gql_type(type_ref: dict) -> str:
    """Collapse GraphQL's NON_NULL/LIST wrapper layers down to a readable type name."""
    name = type_ref.get("name")
    if name:
        return name
    of_type = type_ref.get("ofType")
    return _unwrap_gql_type(of_type) if of_type else "?"


def _introspect_input_type(type_name: str) -> Optional[list[dict]]:
    query = '''
        query IntrospectInputType($name: String!) {
            __type(name: $name) {
                name
                inputFields {
                    name
                    type { name kind ofType { name kind ofType { name kind } } }
                }
            }
        }
    '''
    result = _openimis_graphql(query, {"name": type_name})
    t = result["__type"]
    return t["inputFields"] if t else None


# ---------------------------------------------------------------------------
# Read tools (Postgres)
# ---------------------------------------------------------------------------

@mcp.tool()
def search_insuree(chf_id: Optional[str] = None, last_name: Optional[str] = None) -> list[dict]:
    """
    Search for an insuree (insured person) by CHF ID (exact) or last name (partial).
    Returns basic identifying info plus the internal numeric insuree_id —
    that numeric ID (not the CHF ID) is what create_claim's "insuree" field
    expects.

    Args:
        chf_id: Exact CHF/insuree identification number.
        last_name: Partial, case-insensitive match on last name.
    """
    log_call("search_insuree", chf_id=chf_id, last_name=last_name)

    if chf_id:
        sql = '''
            SELECT "InsureeID" AS insuree_id, "CHFID" AS chf_id, "LastName" AS last_name,
                   "OtherNames" AS other_names, "DOB" AS date_of_birth, "Gender" AS gender
            FROM "tblInsuree"
            WHERE "CHFID" = %s AND "ValidityTo" IS NULL
        '''
        return run_query(sql, (chf_id,))
    elif last_name:
        sql = '''
            SELECT "InsureeID" AS insuree_id, "CHFID" AS chf_id, "LastName" AS last_name,
                   "OtherNames" AS other_names, "DOB" AS date_of_birth, "Gender" AS gender
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
def get_claim_by_code(claim_code: str) -> list[dict]:
    """
    Look up a claim by its claim code — mainly useful for confirming a claim
    created via create_claim actually landed, and checking its status,
    since claim creation through openIMIS's GraphQL API can be processed
    asynchronously (it may not appear the instant create_claim returns).

    Args:
        claim_code: The claim code (as provided in claim_input to
            create_claim, or as returned/generated by openIMIS).
    """
    log_call("get_claim_by_code", claim_code=claim_code)
    sql = '''
        SELECT c."ClaimCode" AS claim_code, c."ClaimUUID" AS claim_uuid,
               c."DateClaimed" AS date_claimed, c."ClaimStatus" AS status,
               c."Approved" AS approved_amount, i."CHFID" AS chf_id,
               hf."HFName" AS health_facility
        FROM "tblClaim" c
        JOIN "tblInsuree" i ON i."InsureeID" = c."InsureeID"
        JOIN "tblHF" hf ON hf."HfID" = c."HFID"
        WHERE c."ClaimCode" = %s AND c."ValidityTo" IS NULL
    '''
    return run_query(sql, (claim_code,))


@mcp.tool()
def list_health_facilities(district: Optional[str] = None) -> list[dict]:
    """
    List health facilities, optionally filtered by district name. Returns
    the facility code/name plus the internal numeric hf_id — that numeric
    ID (not the facility code) is what create_claim's "healthFacility"
    field expects.

    Args:
        district: Optional partial, case-insensitive district name filter.
    """
    log_call("list_health_facilities", district=district)
    if district:
        sql = '''
            SELECT hf."HfID" AS hf_id, hf."HFCode" AS code, hf."HFName" AS name,
                   hf."HFLevel" AS level, loc."LocationName" AS district
            FROM "tblHF" hf
            JOIN "tblLocations" loc ON loc."LocationId" = hf."LocationId"
            WHERE loc."LocationName" ILIKE %s AND hf."ValidityTo" IS NULL
            ORDER BY hf."HFName"
        '''
        return run_query(sql, (f"%{district}%",))
    else:
        sql = '''
            SELECT hf."HfID" AS hf_id, hf."HFCode" AS code, hf."HFName" AS name,
                   hf."HFLevel" AS level
            FROM "tblHF" hf
            WHERE hf."ValidityTo" IS NULL
            ORDER BY hf."HFName"
        '''
        return run_query(sql, ())


@mcp.tool()
def search_diagnosis(query: str) -> list[dict]:
    """
    Search ICD diagnosis codes by code or partial name match. Use this to
    resolve a valid diagnosis ID/code before calling create_claim.

    NOTE: table/column names here ("tblICDCodes") are a best guess and NOT
    verified against a live openIMIS instance — confirm with \\dt / \\d in
    psql and adjust if needed.

    Args:
        query: Partial ICD code or diagnosis name (case-insensitive).
    """
    log_call("search_diagnosis", query=query)
    sql = '''
        SELECT "ICDID" AS icd_id, "ICDCode" AS icd_code, "ICDName" AS name
        FROM "tblICDCodes"
        WHERE ("ICDCode" ILIKE %s OR "ICDName" ILIKE %s) AND "ValidityTo" IS NULL
        ORDER BY "ICDCode"
    '''
    pattern = f"%{query}%"
    return run_query(sql, (pattern, pattern))


@mcp.tool()
def search_medical_service(query: str) -> list[dict]:
    """
    Search configured medical services (procedures, consultations, etc.)
    by code or partial name match. Use this to resolve a valid service
    ID/code before calling create_claim.

    NOTE: table/column names here ("tblServices") are a best guess
    and NOT verified against a live openIMIS instance — confirm with
    \\dt / \\d in psql and adjust if needed.

    Args:
        query: Partial service code or name (case-insensitive).
    """
    log_call("search_medical_service", query=query)
    sql = '''
        SELECT "ServiceID" AS service_id, "ServCode" AS code, "ServName" AS name,
               "ServPrice" AS price
        FROM "tblServices"
        WHERE ("ServCode" ILIKE %s OR "ServName" ILIKE %s) AND "ValidityTo" IS NULL
        ORDER BY "ServName"
    '''
    pattern = f"%{query}%"
    return run_query(sql, (pattern, pattern))


@mcp.tool()
def search_medical_item(query: str) -> list[dict]:
    """
    Search configured medical items (drugs, supplies, etc.) by code or
    partial name match. Use this to resolve a valid item ID/code before
    calling create_claim.

    NOTE: table/column names here ("tblItems") are a best guess and
    NOT verified against a live openIMIS instance — confirm with \\dt / \\d
    in psql and adjust if needed.

    Args:
        query: Partial item code or name (case-insensitive).
    """
    log_call("search_medical_item", query=query)
    sql = '''
        SELECT "ItemID" AS item_id, "ItemCode" AS code, "ItemName" AS name,
               "ItemPrice" AS price
        FROM "tblItems"
        WHERE ("ItemCode" ILIKE %s OR "ItemName" ILIKE %s) AND "ValidityTo" IS NULL
        ORDER BY "ItemName"
    '''
    pattern = f"%{query}%"
    return run_query(sql, (pattern, pattern))


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


@mcp.tool()
def detect_diagnosis_anomalies(
    baseline_days: int = 90,
    recent_days: int = 7,
    min_total_claims: int = 3,
    z_threshold: float = 2.0,
    top_n: int = 20,
) -> list[dict]:
    """
    Screen for possible disease outbreak signals by comparing recent claim
    volume per diagnosis category per district against a historical
    baseline, using a z-score (standard deviations above the baseline
    daily average). This is the same statistical idea behind CDC's EARS
    C1/C2 aberration detection: flag statistically unusual counts, not
    just numerically high ones.

    IMPORTANT CAVEATS — this is a screening signal, not an outbreak
    diagnosis:
    - Claims reflect coded diagnoses among people who sought and paid for
      care, not confirmed disease incidence in the whole population.
    - A high z-score can also reflect a coding change, a new facility,
      population growth, or normal seasonality (e.g. malaria in rainy
      season) — always treat results as "worth a human looking into",
      never as automated confirmation of an outbreak.

    Diagnoses are grouped by the first 3 characters of their ICD code
    (e.g. all of A00.x groups together), which approximates a disease
    category rather than a single exact code.

    Args:
        baseline_days: How many days of history to build the baseline
            from, ending right before the recent window. Default 90.
        recent_days: Length of the "recent" window being screened, in
            days. Default 7.
        min_total_claims: Minimum total claims (baseline + recent
            combined) for a diagnosis-category/district pair to be
            considered — filters out very rare codes where a baseline
            can't be meaningfully computed.
        z_threshold: Minimum z-score to include in results. Default 2.0
            (roughly the 95th percentile for a normal distribution).
        top_n: Max rows to return, sorted by z-score descending (capped
            at 50).
    """
    top_n = min(max(top_n, 1), 50)
    today = date.today()
    recent_start = today - timedelta(days=recent_days)
    baseline_start = today - timedelta(days=baseline_days + recent_days)

    log_call(
        "detect_diagnosis_anomalies",
        baseline_days=baseline_days, recent_days=recent_days,
        min_total_claims=min_total_claims, z_threshold=z_threshold, top_n=top_n,
    )

    # NOTE: "ICDID" as the principal-diagnosis foreign key on tblClaim is a
    # best guess (not verified against a live instance) — confirm with
    # \d "tblClaim" and adjust if your schema names this differently, or
    # if diagnoses are stored in a separate tblClaimDiagnosis-style table.
    sql = '''
        WITH observed AS (
            -- disease-category / district pairs with enough volume to
            -- bother computing a baseline for
            SELECT LEFT(icd."ICDCode", 3) AS disease_category,
                   loc."LocationName" AS district_name
            FROM "tblClaim" c
            JOIN "tblICDCodes" icd ON icd."ICDID" = c."ICDID"
            JOIN "tblHF" hf ON hf."HfID" = c."HFID"
            JOIN "tblLocations" loc ON loc."LocationId" = hf."LocationId"
            WHERE c."ValidityTo" IS NULL AND c."DateClaimed" >= %(baseline_start)s
            GROUP BY 1, 2
            HAVING COUNT(*) >= %(min_total_claims)s
        ),
        date_series AS (
            SELECT generate_series(%(baseline_start)s::date, %(today)s::date - INTERVAL '1 day', '1 day')::date AS claim_date
        ),
        grid AS (
            -- every (day, category, district) combo, so days with zero
            -- claims are represented — essential for a correct baseline
            SELECT ds.claim_date, o.disease_category, o.district_name
            FROM date_series ds
            CROSS JOIN observed o
        ),
        actual_counts AS (
            SELECT c."DateClaimed"::date AS claim_date,
                   LEFT(icd."ICDCode", 3) AS disease_category,
                   loc."LocationName" AS district_name,
                   COUNT(*) AS cnt
            FROM "tblClaim" c
            JOIN "tblICDCodes" icd ON icd."ICDID" = c."ICDID"
            JOIN "tblHF" hf ON hf."HfID" = c."HFID"
            JOIN "tblLocations" loc ON loc."LocationId" = hf."LocationId"
            WHERE c."ValidityTo" IS NULL AND c."DateClaimed" >= %(baseline_start)s
            GROUP BY 1, 2, 3
        ),
        daily AS (
            SELECT g.claim_date, g.disease_category, g.district_name,
                   COALESCE(a.cnt, 0) AS cnt
            FROM grid g
            LEFT JOIN actual_counts a
              ON a.claim_date = g.claim_date
             AND a.disease_category = g.disease_category
             AND a.district_name = g.district_name
        ),
        baseline AS (
            SELECT disease_category, district_name,
                   AVG(cnt) AS baseline_avg, STDDEV(cnt) AS baseline_stddev
            FROM daily
            WHERE claim_date < %(recent_start)s
            GROUP BY disease_category, district_name
        ),
        recent AS (
            SELECT disease_category, district_name,
                   AVG(cnt) AS recent_avg, SUM(cnt) AS recent_total
            FROM daily
            WHERE claim_date >= %(recent_start)s
            GROUP BY disease_category, district_name
        )
        SELECT r.district_name, r.disease_category,
               r.recent_total AS recent_total_claims,
               ROUND(r.recent_avg::numeric, 2) AS recent_avg_per_day,
               ROUND(b.baseline_avg::numeric, 2) AS baseline_avg_per_day,
               ROUND(b.baseline_stddev::numeric, 2) AS baseline_stddev,
               CASE WHEN COALESCE(b.baseline_stddev, 0) = 0 THEN NULL
                    ELSE ROUND(((r.recent_avg - b.baseline_avg) / b.baseline_stddev)::numeric, 2)
               END AS z_score
        FROM recent r
        JOIN baseline b USING (disease_category, district_name)
        WHERE COALESCE(((r.recent_avg - b.baseline_avg) / NULLIF(b.baseline_stddev, 0)), 0) >= %(z_threshold)s
        ORDER BY z_score DESC NULLS LAST
        LIMIT %(top_n)s
    '''
    params = {
        "baseline_start": baseline_start,
        "recent_start": recent_start,
        "today": today,
        "min_total_claims": min_total_claims,
        "z_threshold": z_threshold,
        "top_n": top_n,
    }
    return run_query(sql, params, max_rows=top_n)


@mcp.tool()
def get_reimbursement_delay_by_facility(
    lookback_days: int = 180,
    min_claims: int = 5,
    top_n: int = 20,
) -> list[dict]:
    """
    Find health facilities (providers) with the longest delay between
    submitting a claim and it being processed/adjudicated — a proxy for
    "slow to be reimbursed". Returns facilities sorted by average delay,
    longest first.

    IMPORTANT CAVEAT: this measures days from DateClaimed (submission) to
    DateProcessed (adjudication/decision), NOT necessarily the date money
    actually left the bank account. If your openIMIS instance tracks a
    separate payment/disbursement date (e.g. through a batch payment
    module) rather than treating DateProcessed as the final step, that
    field would be the more accurate one to use here — check with
    `\\d "tblClaim"` for something like DatePaid, or a related payment
    table, before treating this as literal payment delay.

    Only claims that have actually been processed are included (claims
    still pending aren't counted as "delayed" — they're just not done yet).

    Args:
        lookback_days: How far back to look, based on claim submission
            date. Default 180 (roughly 6 months).
        min_claims: Minimum number of processed claims a facility needs to
            be included — filters out noise from very low-volume
            facilities where one slow claim skews the average heavily.
        top_n: Max number of facilities to return (capped at 50).
    """
    top_n = min(max(top_n, 1), 50)
    start_date = date.today() - timedelta(days=lookback_days)

    log_call(
        "get_reimbursement_delay_by_facility",
        lookback_days=lookback_days, min_claims=min_claims, top_n=top_n,
    )

    sql = '''
        SELECT hf."HFCode" AS code, hf."HFName" AS name,
               COUNT(*) AS processed_claims,
               ROUND(AVG(c."DateProcessed"::date - c."DateClaimed"::date)::numeric, 1) AS avg_delay_days,
               ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
                   ORDER BY (c."DateProcessed"::date - c."DateClaimed"::date)
               )::numeric, 1) AS median_delay_days,
               MAX(c."DateProcessed"::date - c."DateClaimed"::date) AS max_delay_days
        FROM "tblClaim" c
        JOIN "tblHF" hf ON hf."HfID" = c."HFID"
        WHERE c."ValidityTo" IS NULL
          AND c."DateProcessed" IS NOT NULL
          AND c."DateClaimed" >= %(start_date)s
        GROUP BY hf."HFCode", hf."HFName"
        HAVING COUNT(*) >= %(min_claims)s
        ORDER BY avg_delay_days DESC
        LIMIT %(top_n)s
    '''
    params = {"start_date": start_date, "min_claims": min_claims, "top_n": top_n}
    return run_query(sql, params, max_rows=top_n)


@mcp.tool()
def detect_item_supply_risk(
    baseline_days: int = 90,
    recent_days: int = 14,
    min_total_dispensed: int = 5,
    z_threshold: float = -2.0,
    top_n: int = 20,
) -> list[dict]:
    """
    Screen for possible drug/item shortages by flagging facility-item pairs
    where recent dispensing volume has dropped statistically far below
    their own historical baseline — the mirror image of
    detect_diagnosis_anomalies (there, spikes are the signal being
    screened for; here, drops are).

    IMPORTANT — this is a weaker, noisier signal than the outbreak-detection
    tool, and should be treated with real caution:
    - Claims data reflects what was DISPENSED AND BILLED, not what's
      currently in stock. A drop can mean a real shortage, but can just as
      easily mean falling demand, a provider substituting a different
      item, a coding change, or a facility temporarily closed — none of
      which are shortages.
    - If your openIMIS deployment has an actual stock/inventory module
      (some do — check for tables with "stock" in the name via \\dt), USE
      THAT INSTEAD for a direct answer (stock on hand vs. reorder
      threshold) rather than this indirect claims-based proxy.
    - Treat any result here as "worth someone calling the facility to ask
      whether they're running low", never as a confirmed shortage.

    NOTE: "tblClaimItems" and its column names are a best guess (not
    verified against a live instance) — confirm with \\d "tblClaimItems"
    and adjust if your schema differs.

    Args:
        baseline_days: Days of history for the baseline, ending right
            before the recent window. Default 90.
        recent_days: Length of the recent window being screened. Default 14
            — shorter than detect_diagnosis_anomalies's default, since
            shortages can develop faster than disease trends.
        min_total_dispensed: Minimum combined dispense count (baseline +
            recent) for an item/facility pair to be included — filters out
            noise from rarely-used items.
        z_threshold: Flag pairs at or below this z-score (negative = below
            baseline). Default -2.0.
        top_n: Max rows to return, most negative z-score first (capped at 50).
    """
    top_n = min(max(top_n, 1), 50)
    today = date.today()
    recent_start = today - timedelta(days=recent_days)
    baseline_start = today - timedelta(days=baseline_days + recent_days)

    log_call(
        "detect_item_supply_risk",
        baseline_days=baseline_days, recent_days=recent_days,
        min_total_dispensed=min_total_dispensed, z_threshold=z_threshold, top_n=top_n,
    )

    sql = '''
        WITH observed AS (
            SELECT ci."ItemID" AS item_id, hf."HfID" AS hf_id,
                   mi."ItemName" AS item_name, hf."HFCode" AS hf_code, hf."HFName" AS hf_name
            FROM "tblClaimItems" ci
            JOIN "tblClaim" c ON c."ClaimID" = ci."ClaimID"
            JOIN "tblHF" hf ON hf."HfID" = c."HFID"
            JOIN "tblItems" mi ON mi."ItemID" = ci."ItemID"
            WHERE ci."ValidityTo" IS NULL AND c."ValidityTo" IS NULL
              AND c."DateClaimed" >= %(baseline_start)s
            GROUP BY 1, 2, 3, 4, 5
            HAVING COUNT(*) >= %(min_total_dispensed)s
        ),
        date_series AS (
            SELECT generate_series(%(baseline_start)s::date, %(today)s::date - INTERVAL '1 day', '1 day')::date AS claim_date
        ),
        grid AS (
            SELECT ds.claim_date, o.item_id, o.hf_id, o.item_name, o.hf_code, o.hf_name
            FROM date_series ds
            CROSS JOIN observed o
        ),
        actual_counts AS (
            SELECT c."DateClaimed"::date AS claim_date, ci."ItemID" AS item_id, hf."HfID" AS hf_id,
                   COUNT(*) AS cnt
            FROM "tblClaimItems" ci
            JOIN "tblClaim" c ON c."ClaimID" = ci."ClaimID"
            JOIN "tblHF" hf ON hf."HfID" = c."HFID"
            WHERE ci."ValidityTo" IS NULL AND c."ValidityTo" IS NULL
              AND c."DateClaimed" >= %(baseline_start)s
            GROUP BY 1, 2, 3
        ),
        daily AS (
            SELECT g.claim_date, g.item_id, g.hf_id, g.item_name, g.hf_code, g.hf_name,
                   COALESCE(a.cnt, 0) AS cnt
            FROM grid g
            LEFT JOIN actual_counts a
              ON a.claim_date = g.claim_date AND a.item_id = g.item_id AND a.hf_id = g.hf_id
        ),
        baseline AS (
            SELECT item_id, hf_id, AVG(cnt) AS baseline_avg, STDDEV(cnt) AS baseline_stddev
            FROM daily
            WHERE claim_date < %(recent_start)s
            GROUP BY item_id, hf_id
        ),
        recent AS (
            SELECT item_id, hf_id, item_name, hf_code, hf_name,
                   AVG(cnt) AS recent_avg, SUM(cnt) AS recent_total
            FROM daily
            WHERE claim_date >= %(recent_start)s
            GROUP BY item_id, hf_id, item_name, hf_code, hf_name
        )
        SELECT r.hf_code AS facility_code, r.hf_name AS facility_name,
               r.item_name,
               r.recent_total AS recent_dispensed,
               ROUND(r.recent_avg::numeric, 2) AS recent_avg_per_day,
               ROUND(b.baseline_avg::numeric, 2) AS baseline_avg_per_day,
               ROUND(b.baseline_stddev::numeric, 2) AS baseline_stddev,
               CASE WHEN COALESCE(b.baseline_stddev, 0) = 0 THEN NULL
                    ELSE ROUND(((r.recent_avg - b.baseline_avg) / b.baseline_stddev)::numeric, 2)
               END AS z_score
        FROM recent r
        JOIN baseline b USING (item_id, hf_id)
        WHERE b.baseline_avg > 0  -- only flag items that WERE being used regularly before
          AND COALESCE(((r.recent_avg - b.baseline_avg) / NULLIF(b.baseline_stddev, 0)), 0) <= %(z_threshold)s
        ORDER BY z_score ASC NULLS LAST
        LIMIT %(top_n)s
    '''
    params = {
        "baseline_start": baseline_start,
        "recent_start": recent_start,
        "today": today,
        "min_total_dispensed": min_total_dispensed,
        "z_threshold": z_threshold,
        "top_n": top_n,
    }
    return run_query(sql, params, max_rows=top_n)


# ---------------------------------------------------------------------------
# Write tools (openIMIS GraphQL API — claim creation)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_claim_mutation_schema() -> dict:
    """
    Introspect openIMIS's own GraphQL schema for the createClaim,
    updateClaim, and submitClaim mutations. ALWAYS call this before
    create_claim or submit_claim — the exact input field names differ
    across openIMIS versions/deployments, and this returns the real,
    current schema from your instance rather than a guess.

    Returns a dict like:
        {"createClaim": {"input": {"type": "ClaimInputType",
                                    "fields": [{"name": ..., "type": ...}, ...]}},
         "submitClaim": {...}}
    """
    log_call("get_claim_mutation_schema")
    mutations_query = '''
        query IntrospectMutations {
            __schema {
                mutationType {
                    fields {
                        name
                        args {
                            name
                            type { name kind ofType { name kind ofType { name kind } } }
                        }
                    }
                }
            }
        }
    '''
    result = _openimis_graphql(mutations_query, {})
    all_fields = result["__schema"]["mutationType"]["fields"]
    relevant = [f for f in all_fields if f["name"] in ("createClaim", "updateClaim", "submitClaim")]

    schema = {}
    for f in relevant:
        args_info = {}
        for arg in f["args"]:
            type_name = _unwrap_gql_type(arg["type"])
            input_fields = _introspect_input_type(type_name)
            args_info[arg["name"]] = {
                "type": type_name,
                "fields": [
                    {"name": fld["name"], "type": _unwrap_gql_type(fld["type"])}
                    for fld in (input_fields or [])
                ],
            }
        schema[f["name"]] = args_info
    return schema


@mcp.tool()
def introspect_input_type(type_name: str) -> list[dict]:
    """
    Introspect any named GraphQL input type on openIMIS's own schema and
    return its field names/types. get_claim_mutation_schema only expands the
    top-level input type for createClaim/updateClaim/submitClaim — use this
    to look inside nested input types it references (e.g. ClaimItemInputType,
    ClaimServiceInputType, ClaimAttachmentInputType) before building
    claim_input for create_claim, rather than guessing field names.

    Args:
        type_name: Exact GraphQL input type name, e.g. "ClaimItemInputType"
            (as seen in a "type" value returned by get_claim_mutation_schema).
    """
    log_call("introspect_input_type", type_name=type_name)
    fields = _introspect_input_type(type_name)
    if fields is None:
        return []
    return [
        {"name": fld["name"], "type": _unwrap_gql_type(fld["type"])}
        for fld in fields
    ]


@mcp.tool()
def create_claim(claim_input: dict) -> dict:
    """
    Create a claim in openIMIS via its own GraphQL createClaim mutation —
    NOT a direct database write. This preserves openIMIS's own pricing,
    ceiling, and policy-coverage validation, and its audit trail.

    Leaves the claim in its normal draft/"entered" state — it is NOT
    submitted automatically. Call get_claim_by_code afterward to confirm it
    landed (creation may be processed asynchronously), then call
    submit_claim explicitly once you're satisfied it's correct. Keeping
    these as separate steps means a human/agent reviews before a claim
    enters openIMIS's real validation and adjudication workflow.

    ALWAYS call get_claim_mutation_schema first to get the exact input
    shape for your instance, and resolve every referenced ID using
    search_insuree, list_health_facilities, search_diagnosis,
    search_medical_service, and search_medical_item — never guess an ID.

    Args:
        claim_input: A dict matching the `input` argument shape returned by
            get_claim_mutation_schema()["createClaim"]["input"]["fields"].
            Typically includes the insuree, health facility, diagnosis,
            claim admin, service period dates, and a list of services
            and/or items with quantities.
    """
    log_call("create_claim")
    mutation = '''
        mutation CreateClaim($input: CreateClaimMutationInput!) {
            createClaim(input: $input) {
                clientMutationId
                internalId
            }
        }
    '''
    # NOTE: confirmed via get_claim_mutation_schema() introspection that this
    # instance's input type is "CreateClaimMutationInput" (not the more
    # commonly-named "ClaimInputType" seen in some openIMIS deployments) —
    # re-check with get_claim_mutation_schema() if you point this at a
    # different instance.
    return _openimis_graphql(mutation, {"input": claim_input})


@mcp.tool()
def submit_claim(claim_uuid: str) -> dict:
    """
    Submit a previously created claim, moving it from draft/"entered" into
    openIMIS's normal validation and adjudication workflow. This is a
    deliberate separate step from create_claim — call get_claim_by_code
    first to confirm the claim exists and looks correct.

    ALWAYS call get_claim_mutation_schema first to confirm the exact
    argument shape expected — some openIMIS versions key this mutation by
    UUID list, others by a filter; adjust the mutation below to match.

    Args:
        claim_uuid: The UUID of the claim to submit (from get_claim_by_code
            or the response of create_claim).
    """
    log_call("submit_claim", claim_uuid=claim_uuid)
    mutation = '''
        mutation SubmitClaim($uuids: [String]!) {
            submitClaim(uuids: $uuids) {
                clientMutationId
            }
        }
    '''
    return _openimis_graphql(mutation, {"uuids": [claim_uuid]})


if __name__ == "__main__":
    # host/port are configured on the FastMCP constructor above, not here —
    # passing them to run() directly raises TypeError on current SDK versions.
    mcp.run(transport="streamable-http")