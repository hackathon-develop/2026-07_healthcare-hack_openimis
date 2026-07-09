# openIMIS MCP Server

A read-only MCP server that exposes a small set of safe, parameterized tools
for querying an openIMIS PostgreSQL database:

- `search_insuree(chf_id, last_name)`
- `get_active_policies(chf_id)`
- `get_claims_for_insuree(chf_id, start_date, end_date)`
- `list_health_facilities(district)`
- `get_claims_trend_by_facility(period_days, end_date, min_claims, top_n)` —
  facilities with the fastest-changing claim volume (period-over-period)
- `get_daily_claims_for_facility(hf_code, start_date, end_date)` —
  day-by-day claim counts for one facility, for drilling into a trend

> Commands below are given for bash (macOS/Linux) with a PowerShell (Windows)
> equivalent alongside wherever the syntax actually differs — line
> continuation, quoting, or environment variables. Where there's no
> PowerShell block, the same command works as-is in both.

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

```powershell
psql -h $env:OPENIMIS_DB_HOST -U your_admin_user -d openimis -c '\dt'
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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python server.py
```

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # fill in real values
python server.py
```

> PowerShell execution policy blocking the activate script? Run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` first,
> then re-run `.venv\Scripts\Activate.ps1`.

`server.py` loads `.env` automatically on startup (via `python-dotenv`) — no
need to manually export the variables into your shell first. This also means
`.env` works the same way on Windows, macOS, and Linux.

This starts the MCP server on streamable HTTP at `http://localhost:8080`.

## 4. Where does the database actually live?

Pick whichever matches your setup — it changes a few flags in Step 4 of the
Cloud Run deployment below, nothing else.

**A. Cloud SQL** (Postgres managed by Google Cloud, in the same project).
Connect via the Cloud SQL Unix socket — this is the recommended, most secure
option since traffic never leaves Google's internal network.

**B. Self-managed Postgres on a Google Cloud VPC** (a VM, GKE, or on-prem
reachable via Cloud VPN/Interconnect into that VPC). Cloud Run reaches it
through a [Serverless VPC Access connector](https://cloud.google.com/vpc/docs/configure-serverless-vpc-access).

**C. A database outside Google Cloud entirely** — on-prem with a public IP,
another cloud provider, a hosted Postgres service (Supabase, Neon, RDS,
Azure Database for PostgreSQL, etc.), or simply a laptop/server exposed via
a tunnel for a hackathon. Cloud Run has outbound internet access by default,
so this is often the *simplest* option to wire up — you're just pointing
`OPENIMIS_DB_HOST`/`OPENIMIS_DB_PORT` at a normal public hostname and port.
Two things matter more here than in A or B, though:

- **Encrypt in transit.** Set `OPENIMIS_DB_SSLMODE=require` (the code
  already supports this — see `.env.example`), since the connection now
  crosses the public internet rather than staying inside Google's network.
- **Firewall allow-listing.** If the external database's firewall requires
  allow-listing specific source IPs (common for hosted Postgres providers
  and on-prem setups), you have a problem: Cloud Run doesn't have a static
  outbound IP by default, and its IP range can change. Two ways to handle it:
  - **Simplest**: if the DB provider lets you allow-list "all IPs" and rely
    on password + TLS for security instead (fine for a hackathon/demo, not
    for production PII), just do that.
  - **Proper fix**: give Cloud Run a static, reservable outbound IP via
    [Direct VPC egress + Cloud NAT](https://cloud.google.com/run/docs/configuring/static-outbound-ip),
    then allow-list *that* IP on the external database's firewall. Rough
    shape of it:
    ```bash
    gcloud compute networks vpc-access connectors create openimis-connector \
      --region=YOUR_REGION --network=default --range=10.8.0.0/28

    gcloud compute addresses create openimis-static-ip \
      --region=YOUR_REGION

    gcloud compute routers create openimis-router \
      --region=YOUR_REGION --network=default

    gcloud compute routers nats create openimis-nat \
      --router=openimis-router --region=YOUR_REGION \
      --nat-external-ip-pool=openimis-static-ip \
      --nat-all-subnet-ip-ranges
    ```
    Then deploy with `--vpc-connector=openimis-connector --vpc-egress=all-traffic`
    (see Step 4 below), and allow-list the IP shown by
    `gcloud compute addresses describe openimis-static-ip --region=YOUR_REGION`
    on your external database.

## 5. Deploy to Cloud Run — full walkthrough

### Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- A Google Cloud project with billing enabled, selected as default:
  ```bash
  gcloud config set project YOUR_PROJECT_ID
  ```
- Your openIMIS Postgres database reachable per one of the options in Step 4 above

### Step 1 — Enable the required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  sqladmin.googleapis.com
```

```powershell
gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  secretmanager.googleapis.com `
  sqladmin.googleapis.com
```

(Skip `sqladmin.googleapis.com` if your Postgres isn't Cloud SQL — i.e. for
options B or C above.)

### Step 2 — Put the DB password in Secret Manager

Don't pass the DB password as a plain env var. Store it as a secret:

```bash
printf 'your-db-password' | gcloud secrets create openimis-db-password --data-file=-
```

```powershell
Set-Content -Path password.txt -Value "your-db-password" -NoNewline
gcloud secrets create openimis-db-password --data-file=password.txt
Remove-Item password.txt
```

### Step 3 — Create a dedicated service account for the Cloud Run service

```bash
gcloud iam service-accounts create openimis-mcp-sa \
  --display-name="openIMIS MCP server"

gcloud secrets add-iam-policy-binding openimis-db-password \
  --member="serviceAccount:openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

```powershell
gcloud iam service-accounts create openimis-mcp-sa `
  --display-name="openIMIS MCP server"

gcloud secrets add-iam-policy-binding openimis-db-password `
  --member="serviceAccount:openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" `
  --role="roles/secretmanager.secretAccessor"
```

If your database is Cloud SQL (option A), also grant the Cloud SQL Client role:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"
```

```powershell
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID `
  --member="serviceAccount:openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" `
  --role="roles/cloudsql.client"
```

### Step 4 — Deploy

**Option A — Cloud SQL** (connect via the Unix socket, not a public IP):

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

```powershell
gcloud run deploy openimis-mcp `
  --source=. `
  --region=YOUR_REGION `
  --no-allow-unauthenticated `
  --service-account=openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com `
  --add-cloudsql-instances=YOUR_PROJECT_ID:YOUR_REGION:YOUR_INSTANCE `
  --set-env-vars=OPENIMIS_DB_HOST=/cloudsql/YOUR_PROJECT_ID:YOUR_REGION:YOUR_INSTANCE,OPENIMIS_DB_NAME=openimis,OPENIMIS_DB_USER=openimis_readonly `
  --update-secrets=OPENIMIS_DB_PASSWORD=openimis-db-password:latest `
  --timeout=3600 `
  --concurrency=40
```

**Option B — Self-managed Postgres on a VPC**: same command, but swap the
Cloud SQL flags for a VPC connector:

```bash
gcloud run deploy openimis-mcp \
  --source=. \
  --region=YOUR_REGION \
  --no-allow-unauthenticated \
  --service-account=openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --vpc-connector=YOUR_CONNECTOR_NAME \
  --set-env-vars=OPENIMIS_DB_HOST=your-internal-db-host,OPENIMIS_DB_NAME=openimis,OPENIMIS_DB_USER=openimis_readonly \
  --update-secrets=OPENIMIS_DB_PASSWORD=openimis-db-password:latest \
  --timeout=3600 \
  --concurrency=40
```

**Option C — Database outside Google Cloud** (public internet, TLS-encrypted):

```bash
gcloud run deploy openimis-mcp \
  --source=. \
  --region=YOUR_REGION \
  --no-allow-unauthenticated \
  --service-account=openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars=OPENIMIS_DB_HOST=your-external-db-hostname,OPENIMIS_DB_PORT=5432,OPENIMIS_DB_NAME=openimis,OPENIMIS_DB_USER=openimis_readonly,OPENIMIS_DB_SSLMODE=require \
  --update-secrets=OPENIMIS_DB_PASSWORD=openimis-db-password:latest \
  --timeout=3600 \
  --concurrency=40
```

```powershell
gcloud run deploy openimis-mcp `
  --source=. `
  --region=YOUR_REGION `
  --no-allow-unauthenticated `
  --service-account=openimis-mcp-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com `
  --set-env-vars=OPENIMIS_DB_HOST=your-external-db-hostname,OPENIMIS_DB_PORT=5432,OPENIMIS_DB_NAME=openimis,OPENIMIS_DB_USER=openimis_readonly,OPENIMIS_DB_SSLMODE=require `
  --update-secrets=OPENIMIS_DB_PASSWORD=openimis-db-password:latest `
  --timeout=3600 `
  --concurrency=40
```

No `--add-cloudsql-instances` and no `--vpc-connector` needed here — Cloud
Run reaches the public internet by default. If the external database's
firewall requires allow-listing specific source IPs, see the static-IP setup
under Step 4/Option C notes above; add `--vpc-connector=openimis-connector
--vpc-egress=all-traffic` to whichever deploy command you use once that's
set up.

Notes on the flags (all options):
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

```powershell
gcloud run services add-iam-policy-binding openimis-mcp `
  --region=YOUR_REGION `
  --member="user:you@example.com" `
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
python test_server.py --chf-id <a real CHF ID>
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
- **Connection refused to the DB (Cloud SQL)**: double check
  `--add-cloudsql-instances` matches your instance connection name exactly
  (`gcloud sql instances describe YOUR_INSTANCE` to confirm it), and that
  `OPENIMIS_DB_HOST` is the `/cloudsql/...` socket path, not a hostname.
- **Connection refused / timeout to an external DB (option C)**: usually a
  firewall allow-list issue — see Step 4/Option C above. Confirm you can
  reach it at all first with `psql` from your own machine using the exact
  same host/port/credentials before assuming it's a Cloud Run-side problem.
- **SSL required / SSL negotiation errors on an external DB**: try
  `OPENIMIS_DB_SSLMODE=require` if you had it at `prefer`, or check the
  provider's docs for their expected sslmode — some hosted providers
  (e.g. Supabase) require specific pooler hostnames for external connections.

## Testing

**Get test data.** Don't point this at a production database. Spin up
openIMIS's own demo dataset locally:

```bash
git clone https://github.com/openimis/openimis-dist_dkr.git
cd openimis-dist_dkr
cp .env.example .env   # uncomment DEMO_DATASET=true, keep DB_DEFAULT=postgresql
docker compose up -d
```

```powershell
git clone https://github.com/openimis/openimis-dist_dkr.git
cd openimis-dist_dkr
Copy-Item .env.example .env   # uncomment DEMO_DATASET=true, keep DB_DEFAULT=postgresql
docker compose up -d
```

Confirm the schema and grab a real CHF ID to test with:

```bash
docker compose exec db psql -U postgres -d openimis -c '\dt'
docker compose exec db psql -U postgres -d openimis -c 'SELECT "CHFID", "LastName" FROM "tblInsuree" LIMIT 5;'
```

(Same commands work unchanged in PowerShell.)

**Run the server**, pointed at that demo DB (see Step 3 above).

**Poke at it interactively** with MCP Inspector — a browser UI for calling
tools by hand without needing an LLM client wired up:

```bash
npx @modelcontextprotocol/inspector python server.py
```

**Or run the scripted smoke test** included here (`test_server.py`), which
connects as a real MCP client and calls each tool in sequence:

```bash
python test_server.py                    # lists a few insurees first
python test_server.py --chf-id <a real CHF ID from above>
```

If a tool call errors out, it's almost always one of: wrong table/column
name for your schema version (see Step 1), the read-only role missing a
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
- **`.env` stays local.** It's excluded from the Docker build (`.dockerignore`)
  and from version control (`.gitignore`) on purpose — it holds a DB password.
  On Cloud Run, configuration comes from `--set-env-vars`/`--update-secrets`
  instead, not from a `.env` file baked into the image.
- **Encrypt in transit for external databases.** If your Postgres lives
  outside Google Cloud (option C), set `OPENIMIS_DB_SSLMODE=require` —
  `prefer` silently allows plaintext if the server doesn't offer TLS, which
  matters a lot more once traffic is crossing the public internet.
- **Data protection law.** Depending on where this is deployed, health and
  insurance data may fall under regulations like GDPR, a national health
  data protection act, or similar. This project doesn't handle
  consent/retention/right-to-erasure requirements — that's a layer you'll
  need on top, not something an MCP server design solves by itself.