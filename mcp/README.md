# openIMIS MCP Server

A read-only MCP server that exposes a small set of safe, parameterized tools
for querying an openIMIS PostgreSQL database:

- `search_insuree(chf_id, last_name)`
- `get_active_policies(chf_id)`
- `get_claims_for_insuree(chf_id, start_date, end_date)`
- `list_health_facilities(district)`

## 1. Confirm your schema first

openIMIS table naming differs by version:

- **Legacy schema** (migrated from the original MSSQL system): mixed-case,
  `tbl`-prefixed tables — `tblInsuree`, `tblPolicy`, `tblClaim`, `tblHF`,
  `tblFamilies`, `tblProduct`, `tblLocations`. Requires double-quoted
  identifiers in Postgres. `server.py` is written against this schema.
- **Modular/Django backend** (current openimis-be_py): lowercase,
  `app_label_modelname` tables — `insuree_insuree`, `policy_policy`,
  `claim_claim`, `location_healthfacility`, `location_location`.

Check yours:

```bash
psql -h $OPENIMIS_DB_HOST -U your_admin_user -d openimis -c '\dt'
```

If you're on the modular schema, rewrite the SQL strings inside each `@mcp.tool()`
function in `server.py` to match — table/column names only, the tool
signatures and overall structure stay the same.

## 2. Create a least-privilege, read-only DB role

Do not point this server at an admin or application account. Run something
like this against your openIMIS database (adjust table names per your schema):

```sql
CREATE ROLE openimis_readonly WITH LOGIN PASSWORD 'use-a-strong-secret';
GRANT CONNECT ON DATABASE openimis TO openimis_readonly;
GRANT USAGE ON SCHEMA public TO openimis_readonly;
GRANT SELECT ON "tblInsuree", "tblPolicy", "tblClaim", "tblHF",
                "tblFamilies", "tblProduct", "tblLocations"
  TO openimis_readonly;
```

Only grant `SELECT` on the specific tables the tools actually query — not the
whole schema. If a future tool needs another table, add it explicitly.

## 3. Run locally

```bash
uv sync
cp .env.example .env   # fill in real values
set -a; source .env; set +a
uv run server.py
```

This starts the MCP server on streamable HTTP at `http://localhost:8080`.

## 4. Deploy to Cloud Run — full walkthrough

### Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- A Google Cloud project with billing enabled, selected as default:
  ```bash
  gcloud config set project YOUR_PROJECT_ID
  ```
- Your openIMIS Postgres database reachable from Google Cloud (either Cloud SQL,
  or a network Cloud Run can reach via a Serverless VPC Access connector)

### Step 1 — Enable the required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  sqladmin.googleapis.com
```
(Skip `sqladmin.googleapis.com` if your Postgres isn't Cloud SQL.)

### Step 2 — Put the DB password in Secret Manager

Don't pass the DB password as a plain env var. Store it as a secret:

```bash
printf 'your-db-password' | gcloud secrets create openimis-db-password --data-file=-
```

### Step 3 — Create a dedicated service account for the Cloud Run service

```bash
gcloud iam service-accounts create openimis-mcp-sa \
  --display-name="openIMIS MCP server"

gcloud secrets add-iam-policy-binding openimis-db-password \
  --member="serviceAccount:openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

If your database is Cloud SQL, also grant the Cloud SQL Client role:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"
```

### Step 4 — Deploy

**If your Postgres is Cloud SQL** (recommended: connect via the Unix socket,
not a public IP):

```bash
gcloud run deploy openimis-mcp \
  --source=. \
  --region=YOUR_REGION \
  --no-allow-unauthenticated \
  --service-account=openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --add-cloudsql-instances=YOUR_PROJECT_ID:YOUR_REGION:YOUR_INSTANCE \
  --set-env-vars=OPENIMIS_DB_HOST=/cloudsql/YOUR_PROJECT_ID:YOUR_REGION:YOUR_INSTANCE,OPENIMIS_DB_NAME=openimis,OPENIMIS_DB_USER=openimis_readonly \
  --update-secrets=OPENIMIS_DB_PASSWORD=openimis-db-password:latest \
  --timeout=3600 \
  --concurrency=40
```

**If your Postgres is elsewhere on a VPC** (self-managed, GKE, etc.), add a
[Serverless VPC Access connector](https://cloud.google.com/vpc/docs/configure-serverless-vpc-access)
and use `--vpc-connector` instead of `--add-cloudsql-instances`, with
`OPENIMIS_DB_HOST` set to the DB's normal internal hostname/IP.

Notes on the flags:
- `--no-allow-unauthenticated` is not optional — see Security notes below.
- `--timeout=3600` extends Cloud Run's default 5-minute request timeout.
  Streamable HTTP can hold a connection open for a client session, and the
  default is too short for anything beyond trivial queries.
- `--concurrency=40` caps how many requests one instance handles at once.
  The default (80) is fine to start with; lower it if you see connection
  pressure on the database, since each in-flight request holds a Postgres
  connection.
- Cloud Build will build the container from source automatically — you
  don't need to build/push an image yourself for this command to work.

### Step 5 — Grant yourself (or your client's identity) permission to call it

```bash
gcloud run services add-iam-policy-binding openimis-mcp \
  --region=YOUR_REGION \
  --member="user:you@example.com" \
  --role="roles/run.invoker"
```

### Step 6 — Connect and test

Get an authenticated local proxy to the deployed service:

```bash
gcloud run services proxy openimis-mcp --region=YOUR_REGION --port=3000
```

Then point `test_server.py` at it — change `SERVER_URL` in that file to
`http://localhost:3000/mcp`, or set it via an env var if you prefer — and run:

```bash
uv run test_server.py --chf-id <a real CHF ID>
```

For a real MCP client (Claude, Gemini CLI, etc.) instead of the local proxy,
you'll authenticate with an IAM identity token in the `Authorization` header
per [Cloud Run's MCP authentication docs](https://docs.cloud.google.com/run/docs/host-mcp-servers) —
the exact client-side config depends on which client you're using.

### Troubleshooting

- **400 error mentioning Host/Origin on every request**: some FastMCP
  versions validate the `Host` header on streamable HTTP requests to guard
  against DNS rebinding. If you hit this once deployed, you'll need to
  allow-list the Cloud Run service's hostname in the FastMCP server config —
  check the installed version's docs for the exact setting name, since this
  has moved around across releases.
- **"session not found" errors**: confirms `stateless_http=True` didn't make
  it into the deployed image — check you're deploying the updated
  `server.py`, not a stale build.
- **Connection refused to the DB**: for Cloud SQL, double check
  `--add-cloudsql-instances` matches your instance connection name exactly
  (`gcloud sql instances describe YOUR_INSTANCE` to confirm it), and that
  `OPENIMIS_DB_HOST` is the `/cloudsql/...` socket path, not a hostname.

## Testing

**Get test data.** Don't point this at a production database. Spin up
openIMIS's own demo dataset locally:

```bash
git clone https://github.com/openimis/openimis-dist_dkr.git
cd openimis-dist_dkr
cp .env.example .env   # uncomment DEMO_DATASET=true, keep DB_DEFAULT=postgresql
docker compose up -d
```

Confirm the schema and grab a real CHF ID to test with:

```bash
docker compose exec db psql -U postgres -d openimis -c '\dt'
docker compose exec db psql -U postgres -d openimis -c 'SELECT "CHFID", "LastName" FROM "tblInsuree" LIMIT 5;'
```

**Run the server**, pointed at that demo DB (see step 3 above).

**Poke at it interactively** with MCP Inspector — a browser UI for calling
tools by hand without needing an LLM client wired up:

```bash
npx @modelcontextprotocol/inspector uv run server.py
```

**Or run the scripted smoke test** included here (`test_server.py`), which
connects as a real MCP client and calls each tool in sequence:

```bash
uv run test_server.py                    # lists a few insurees first
uv run test_server.py --chf-id <a real CHF ID from above>
```

If a tool call errors out, it's almost always one of: wrong table/column
name for your schema version (see step 1), the read-only role missing a
grant on that specific table, or a date format mismatch.

## Security notes (read before going further)

- **Don't add a generic "run arbitrary SQL" tool.** Every tool here is a
  fixed, parameterized query. If you need a new capability, write a new
  function with its own fixed query rather than opening up free-form SQL —
  this is the difference between a model that *can't* leak the whole
  claims table and one that accidentally can, given the right prompt.
- **Row caps.** `MAX_ROWS` caps every result. Don't remove it.
- **Audit logging.** `log_call()` currently just logs to stdout. In a real
  deployment, write these calls to an append-only audit log (Cloud Logging,
  or a separate table) — this is health and insurance PII, and you'll likely
  need an access trail for compliance purposes in your jurisdiction.
- **Field minimization.** Tools return only the fields needed for the stated
  purpose (e.g. `search_insuree` doesn't return full medical history). Keep
  new tools narrow the same way — expose the minimum needed, not `SELECT *`.
- **Data protection law.** Depending on where this is deployed, health and
  insurance data may fall under regulations like GDPR, a national health
  data protection act, or similar. This project doesn't handle
  consent/retention/right-to-erasure requirements — that's a layer you'll
  need on top, not something an MCP server design solves by itself.
