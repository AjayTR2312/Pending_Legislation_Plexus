# Deploying the webapp to Plexus (StreamLit template)

This app can be deployed to the AI Platform **Plexus** Kubernetes cluster as a
single container that serves HTTP on one port. The FastAPI backend serves both
the REST API and the built React UI, and the conversion worker runs inside the
same container (see [`entrypoint.sh`](./entrypoint.sh)).

Plexus pods are **ephemeral and replicated**, so the app must be **stateless**.
The code supports two backends, selected by environment variables:

| Concern            | Local default (dev/tests) | Plexus (stateless)         |
| ------------------ | ------------------------- | -------------------------- |
| Job store          | SQLite file               | **PostgreSQL** (`DATABASE_URL`) |
| File storage       | Local disk                | **S3** (`WEBAPP_S3_BUCKET`)     |
| SFTP/host features | Enabled                   | **Disabled** (`WEBAPP_DISABLE_SFTP=true`) |

When the Plexus variables are unset the app behaves exactly as the original
local trial, so local development and the test-suite are unaffected.

## 1. Infrastructure to provision

1. **PostgreSQL** database (e.g. AWS RDS) reachable from the Plexus account.
   The schema is created automatically on first boot (`db.init_db()`).
2. **S3 bucket** for uploaded PDFs, converted outputs, approved copies and the
   compare cache. Plexus StreamLit-template apps are automatically granted the
   `a207870-ml-deployment-storage-...-plexus-cf-role-prod` role; attach the S3
   (and KMS, if encrypted) bucket policy from the Plexus docs so that role can
   `Get*/Put*/List*`.
3. **LLM access** — the conversion engine calls Claude. Provide the API
   credentials the `app/`/`Opus4.6/` modules expect via Secrets Manager / env,
   or route through **AWS Bedrock** (available to Plexus apps).

## 2. Environment variables

Required for a stateless Plexus deployment:

```
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
WEBAPP_S3_BUCKET=a207870-...-your-bucket   # or set WEBAPP_S3_ENABLED=true to use the built-in default name
WEBAPP_DISABLE_SFTP=true
```

> The code ships a **placeholder** default bucket name
> (`DEFAULT_S3_BUCKET` in [`deployconfig.py`](./backend/deployconfig.py)) and
> region (`us-east-1`). Change the default, or override it with
> `WEBAPP_S3_BUCKET`, once the real bucket exists. A default name alone does
> **not** turn S3 on — S3 activates only with `WEBAPP_S3_ENABLED=true` or when
> `WEBAPP_S3_BUCKET` is set — so local dev and tests stay on local disk.

Optional:

```
WEBAPP_S3_ENABLED=true             # turn on S3 using the default/ configured bucket
WEBAPP_S3_REGION=us-east-1         # AWS region (default us-east-1)
WEBAPP_S3_PREFIX=webapp_trial      # key prefix inside the bucket (default: webapp_trial)
WEBAPP_S3_ENDPOINT_URL=            # for S3-compatible stores / MinIO
AWS_ACCESS_KEY_ID=                 # optional — falls back to the instance IAM role
AWS_SECRET_ACCESS_KEY=             # optional — falls back to the instance IAM role
WORKER_CONCURRENCY=2               # conversion workers per container (default 1)
PORT=8501                          # HTTP port (default 8501, the template default)
DATABASE_POOL_MAX=10               # Postgres pool size
WEBAPP_DATADOG_LOGGING=true        # enable ddtrace log/trace integration
WEBAPP_ROOT_PATH=/123456/prefix    # only if the ingress does NOT strip the prefix
```

Plus whatever the LLM layer needs (e.g. `ANTHROPIC_API_KEY`, or Bedrock/AWS
settings). Check the effective config at runtime via `GET /api/info`
(`deployment` block) or:

```
python webapp_trial/backend/deployconfig.py
```

## 3. Build the image

The build context **must be the repository root**, because the backend imports
the project's `app/` and `Opus4.6/` modules:

```
docker build -f webapp_trial/Dockerfile -t pending-legislation-webapp .
```

Then push to your Workspace ECR and register it as a **Model** in Model
Registry (the Image ARN must contain `-ml-model-registry-`). Create a Plexus
Deployment Job with job type `PLEXUS`, `StreamLit` template, container port
`8501`, and host path `/<asset>/<prefix>`.

### Cookie Affinity (recommended)

The review workspace keeps in-progress edit scratch on local disk. Enable
**Cookie Affinity** so a reviewer's requests stick to one pod during a session:

```
COOKIE_AFFINITY_ENABLED=true
COOKIE_AFFINITY_NAME=http-cookie
```

Durable artifacts (uploaded PDFs, converted outputs, approved snapshots) always
go to S3, so nothing is permanently lost even without affinity.

## 4. What the SFTP toggle disables

`WEBAPP_DISABLE_SFTP=true` cleanly disables the three features that need an
internal SFTP server / host-mounted pipeline folders that do not exist in a
Plexus pod:

* **Pipeline Sessions** view (reads host `tmp/pending_processing/`)
* **SFTP Downloads** (pulls ZIPs from the internal SFTP server)
* **SFTP Submit / Submit-all** (uploads approved files to Keyed)

The core **upload → convert → review → approve** flow is unaffected. The UI can
read the `sftp_features_enabled` flag from `GET /api/info` to hide these.

## 5. Tests

```
# all backend tests (SQLite + local-disk defaults)
pytest webapp_trial/backend

# Postgres backend (throwaway local server via pgserver, or TEST_DATABASE_URL)
pytest webapp_trial/backend/test_db_postgres.py

# S3 backend (mocked with moto)
pytest webapp_trial/backend/test_storage_s3.py

# deployment smoke tests (health, config, SFTP guard, core upload)
pytest webapp_trial/backend/test_deploy_smoke.py
```

Dev extras used by the backend tests: `pytest httpx "psycopg[binary]"
psycopg-pool pgserver "moto[s3]"`.
