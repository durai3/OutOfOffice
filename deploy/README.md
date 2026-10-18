# Deploying DELEG8 to Google Cloud Run (free tier)

Two Cloud Run services (API, web console), a free Postgres on Neon, secrets in
Secret Manager. At a few evaluation sessions this stays inside the free tier.

## One-time setup

1. **GCP project + billing.** console.cloud.google.com, create a project, activate the
   $300 free trial (a card is required for verification; the trial never auto-charges).
   Then Billing -> Budgets & alerts -> create a budget of $1 as a tripwire.
2. **Install gcloud:** `brew install --cask google-cloud-sdk`, then `gcloud auth login`.
3. **Free Postgres:** neon.tech -> new project -> copy the connection string
   (`postgresql://...?sslmode=require`). The app rewrites it to the psycopg driver itself.
4. **Free Gemini key:** aistudio.google.com/apikey (no card).

## Deploy

```bash
export PROJECT_ID=your-project-id
export DATABASE_URL='postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require'
export GEMINI_API_KEY='AIza...'
./deploy/deploy.sh
```

It prints the judges' URL (`<web-url>/judges`) at the end. Re-run the script to ship changes.

## Why these settings

| Setting | Reason |
|---|---|
| `--min-instances 0` | scales to zero, so an idle app costs nothing |
| API `--max-instances 1` | playground sessions and the vector index are per-process; one instance keeps them consistent and caps spend |
| API `--memory 2Gi`, `--cpu-boost` | the local embedding model (torch) needs it; boost shortens cold starts |
| `DELEG8_DISPATCH_MODE=inprocess` | Cloud Run has no Docker daemon, so meetings are driven through the Playground |
| Telegram token unset | the long-polling bot needs an always-on instance, which is not free |

## Before the judges look

- Open the console once yourself a minute ahead (cold start is 20 to 40 s).
- Optional, for judging day only: `gcloud run services update deleg8-api --region us-central1 --min-instances 1`
  keeps it warm. Set it back to `0` afterwards.
- The vector index lives on the instance's temporary disk. If the instance was recycled, press
  **Load sample data** in the Playground again; it re-indexes missing files.

## Cleaning up

`gcloud projects delete $PROJECT_ID` removes everything and stops all billing.
