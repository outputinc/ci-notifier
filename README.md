# CI Notifier

A Slack slash command that sends you a personal DM when a CircleCI pipeline on any branch completes.

```
/ci-notify Hydra main
/ci-notify opmV2 my-feature-branch
```

You get an immediate confirmation in Slack, then a DM when the pipeline finishes — pass, fail, or cancel.

## Architecture

```
Slack /ci-notify command
        │
        ▼
  Cloud Run /slash          ← validates Slack signature, ACKs within 3s
        │
        ▼
  Cloud Tasks queue         ← schedules poll tasks every 30s
        │
        ▼
  Cloud Run /poll           ← queries CircleCI API v2 for workflow status
        │
        ├── still running → reschedule poll
        │
        └── complete → Slack DM to the user who invoked the command
```

**Services involved:**

| Service | Role |
|---|---|
| Slack CI Notifier app | Receives slash commands, sends DMs |
| GCP Cloud Run (`ci-notifier`) | Python/Flask service hosting `/slash` and `/poll` |
| GCP Cloud Tasks (`ci-pipeline-poller`) | Schedules polling tasks |
| CircleCI API v2 | Source of pipeline and workflow status |

## Prerequisites

- GCP project with Cloud Run and Cloud Tasks APIs enabled
- Slack app with a `/ci-notify` slash command and `chat:write` + `im:write` bot scopes
- CircleCI personal API token

## Setup

### 1. Enable GCP APIs

```bash
gcloud services enable run.googleapis.com cloudtasks.googleapis.com cloudbuild.googleapis.com \
  --project=<your-gcp-project>
```

### 2. Create the Cloud Tasks queue

```bash
gcloud tasks queues create ci-pipeline-poller \
  --location=us-central1 \
  --project=<your-gcp-project>
```

### 3. Create a Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch
2. Under **OAuth & Permissions**, add bot scopes: `chat:write`, `im:write`
3. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`)
4. Under **Basic Information → App Credentials**, copy the **Signing Secret**
5. Under **Slash Commands**, create `/ci-notify` pointing at `<your-cloud-run-url>/slash`

### 4. Deploy

Copy `deploy.sh.example` to `deploy.sh`, fill in the values, then run:

```bash
bash deploy.sh
```

After the first deploy, update the `SERVICE_URL` env var with the Cloud Run URL:

```bash
gcloud run services update ci-notifier \
  --region us-central1 \
  --project <your-gcp-project> \
  --update-env-vars SERVICE_URL=<your-cloud-run-url>
```

## Environment variables

| Variable | Description |
|---|---|
| `CIRCLECI_TOKEN` | CircleCI personal API token |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `INTERNAL_TOKEN` | Shared secret for Cloud Tasks → Cloud Run auth (generate with `openssl rand -hex 32`) |
| `GCP_PROJECT` | GCP project ID |
| `GCP_LOCATION` | GCP region (default: `us-central1`) |
| `TASKS_QUEUE` | Cloud Tasks queue name |
| `SERVICE_URL` | Public Cloud Run URL |
| `ORG_PREFIX` | Default CircleCI org prefix (default: `github/outputinc`) |
| `POLL_INTERVAL_SECONDS` | How often to poll CircleCI in seconds (default: `30`) |

## Notification format

**Success:**
```
✅ Hydra pipeline #10437 on `main` — success
```

**Failure:**
```
❌ Hydra pipeline #10438 on `main` — failed
Failed workflows: `main_flow`
```

## Redeploying

```bash
cd ~/repos/ci-notifier && bash deploy.sh
```
