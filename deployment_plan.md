# Deployment plan

## Recommendation

Deploy the existing `Dockerfile` as **one always-on container with a persistent volume** on Fly.io (or Railway). Do not use Vercel or another serverless platform for the backend.

## Why not serverless

Serverless platforms (Vercel Functions, AWS Lambda) run your code only while a request is being served. The environment is frozen or destroyed afterwards, has no durable local disk, and can run many copies at once. This app depends on the opposite:

| App behaviour | Serverless problem |
| --- | --- |
| Slack Socket Mode holds a long-lived websocket to receive Approve clicks | The connection drops when the function is frozen, so approvals silently stop |
| Luma generation runs in the background after `POST /api/generations` returns | Work after the response is not guaranteed to finish |
| SQLite (`catalog.sqlite3`) and generated images live in `DATA_DIR` | Ephemeral disk, so data is lost between invocations |
| Startup jobs mark work cut off by a restart | Assumes one long-running process |

The same reasoning rules out running multiple replicas: each would open its own Socket Mode connection and write to the same SQLite file. **Run exactly one instance.**

## What is already in place

- `Dockerfile`: multi-stage build (Vite frontend, then Python 3.12 with `uv`), serves the API and the built `frontend/dist` from one process on port 8000.
- `DATA_DIR=/data` is set in the image. `backend/db.py` puts `catalog.sqlite3` and the `images` folder there, so mounting a volume at `/data` is enough for persistence.
- `backend/settings.py` and `backend/luma.py` read settings from environment variables first, so platform secrets work without `.env.local`.
- `.dockerignore` excludes `.env.local`, `runtime` and `.venv`, so secrets and local data stay out of the image.

## Configuration (platform secrets, not in the repo)

| Variable | Used for |
| --- | --- |
| `LUMA_AGENTS_API_KEY` | Luma image generation |
| `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN` | Slack Socket Mode and posting |
| `SLACK_APPROVER_USER_ID` | Optional. The only user whose Approve counts; unset, anyone in the channel can approve |
| `GOOGLE_CLIENT_ID` | Drive sign-in (public ID of a Web application OAuth client) |
| `GOOGLE_CLOUD_API_KEY` | Google Picker (public browser key, referrer-restricted) |
| `DATA_DIR=/data` | Already set in the Dockerfile; keep it pointed at the volume |

## Steps (Fly.io)

1. Install `flyctl` and run `fly launch --no-deploy` in the project root. Accept the existing Dockerfile and set the internal port to 8000.
2. Create a volume in the chosen region: `fly volumes create data --size 1`.
3. In `fly.toml`:
   - add a mount: `source = "data"`, `destination = "/data"`
   - set `auto_stop_machines = "off"` and `min_machines_running = 1` so the machine never sleeps (Socket Mode needs it awake)
   - keep a single machine (`fly scale count 1`)
4. Set secrets: `fly secrets set LUMA_AGENTS_API_KEY=... SLACK_APP_TOKEN=... SLACK_BOT_TOKEN=... GOOGLE_CLIENT_ID=... GOOGLE_CLOUD_API_KEY=...`. Add `SLACK_APPROVER_USER_ID=U...` only to restrict approval to one user.
5. `fly deploy`.
6. Check `fly logs` for a successful Slack Socket Mode connection and a clean startup.

Railway is equivalent: deploy from the Dockerfile, attach a volume at `/data`, set the same variables, and keep one replica. Render works only on a paid always-on instance with a disk, because its free tier sleeps.

## After deploying

1. **Google OAuth:** add the deployed URL (`https://<app>.fly.dev`) as an authorized JavaScript origin on the OAuth client, and to the API key's HTTP referrer restrictions. Drive sign-in and Picker will not work from the new domain until this is done.
2. **Consent screen:** it can stay in Testing for the take-home. Add each reviewer's Google account as a test user. Publishing it needs a home page and privacy policy.
3. **Slack:** no URL change is needed, because Socket Mode is outbound only. Confirm the bot is in the review channel.
4. **Smoke test:** upload `data/catalog.csv`, confirm the preview, generate for one product, check the candidates reach Slack, approve one, then Save to Drive.

## Risks and caveats

- **Single point of failure:** one machine and one volume. A redeploy briefly stops the app, and startup jobs mark interrupted work. Back up the volume (`fly volumes snapshots`) if the data matters.
- **No authentication on the web app:** the backend has no login of its own (only Slack approval is restricted). A public URL lets anyone who finds it upload CSVs and spend Luma credits. Consider platform-level access control, an unguessable URL shared only with reviewers, or adding basic auth before sharing widely. This is a decision for the project owner and is not covered by the current handoff.
- **Local data is not migrated:** `runtime/` is excluded from the image, so the deployed app starts with an empty catalog. Upload the CSV fresh (the assignment requires fresh CSV ingestion anyway).
- **AWS alternative:** a single ECS Fargate task (desired count 1) with an EFS volume, or one EC2 instance with an EBS volume. More setup than Fly.io for the same result. Avoid Lambda.

## Not verified

This plan was written from reading the `Dockerfile`, `backend/settings.py`, `backend/db.py` and `backend/luma.py`. The image has not been built or deployed. `ASSUMPTIONS.md` should record the chosen host and the single-instance constraint once deployed.
