# Torrent Intake MVP

A privacy-focused intake controller for qBittorrent.

This service accepts new torrent jobs, stages downloads in a controlled location, scans completed content for malware, deletes infected content, and only promotes clean files to an approved final destination root.

## What This Project Does

Given a magnet link and a final destination path, the app:

1. Creates a tracked intake job.
2. Adds the torrent to qBittorrent with controlled category/tags.
3. Stages download data in either local staging or NAS staging.
4. Scans completed content with ClamAV (`clamscan` by default).
5. Deletes infected torrents and files.
6. Promotes clean torrents to the requested final destination.
7. Sends Telegram notifications only for malware detection/deletion events.

Bulk intake is supported from the UI textarea: paste multiple magnet links, review the detected rows, and either apply the same destination/category/staging settings to every torrent or edit them per row. The single-job `POST /jobs` endpoint rejects multi-magnet blobs; use `POST /jobs/bulk` so each magnet gets its own DB job, qBittorrent tag, and tracking lifecycle.

## Security Model

- Default staging mode is local staging at `/staging-local`.
- Optional staging mode is NAS staging at `/downloads/torrent-intake/staging`.
- `TI_LOCAL_MAX_GIB` is a hard per-torrent cutoff for local staging; larger local-preference jobs are moved to NAS staging automatically.
- `TI_LOCAL_OVERFLOW_POLICY=queue` is the default for aggregate local-space pressure on smaller local jobs: when local capacity would be exceeded, those jobs are paused and resumed automatically when space becomes available again.
- If you set `TI_LOCAL_OVERFLOW_POLICY=nas`, the app moves those aggregate-overflow local jobs to NAS staging instead of queueing them.
- If a job was requested as `nas`, it stays on NAS (no move back to local).
- Final paths must live under an explicit allowlist of approved roots. By default that is just `TI_FINAL_PARENT_PREFIX` (`/downloads`).
- Malware scan runs before any promotion step.
- On infection, torrent and files are deleted; no promotion occurs.
- Telegram alerts are sent only on infection/deletion.

## Container Paths

These are the required in-container paths:

- NAS root visible to app and qBittorrent: `/downloads`
- Local staging inside app container: `/staging-local`
- App persistent data: `/app/data`
- App logs: `/app/logs`

## Required Environment Variables

Copy `.env.example` to `.env` and set values for your environment.

| Variable | Required | Purpose |
|---|---|---|
| `TI_DATABASE_URL` | Yes | SQLite DB path, default `sqlite:////app/data/torrent_intake.db` |
| `TI_QBT_HOST` | Yes | qBittorrent WebUI URL |
| `TI_QBT_USERNAME` | Yes | qBittorrent username |
| `TI_QBT_PASSWORD` | Yes | qBittorrent password |
| `TI_QBT_VERIFY_CERTIFICATE` | Yes | TLS cert verification for qBittorrent |
| `TI_QBT_WEB_URL` | No | Browser-facing qBittorrent WebUI link shown in the intake UI |
| `TI_AUTO_CREATE_FINAL_CATEGORY` | Yes | If `true`, create missing final category in qBittorrent |
| `TI_LOCAL_STAGING_ROOT` | Yes | Must be `/staging-local` in container |
| `TI_NAS_STAGING_ROOT` | Yes | Usually `/downloads/torrent-intake/staging` |
| `TI_FINAL_PARENT_PREFIX` | Yes | Default final root used for prefill/autocomplete |
| `TI_FINAL_PARENT_PREFIXES` | Optional | Comma-separated allowlist of additional approved final roots |
| `TI_LOCAL_OVERFLOW_POLICY` | Yes | Aggregate overflow behavior for local jobs under the size limit: `queue` pauses for local space, `nas` auto-moves them to NAS staging |
| `TI_LOCAL_MAX_GIB` | Yes | Hard per-torrent local staging cutoff; larger local-preference jobs move to NAS staging automatically |
| `TI_LOCAL_FREE_SPACE_BUFFER_GIB` | Yes | Free-space buffer kept on local staging mount when aggregating active local reservations |
| `TI_COMPLETION_EVENT_TOKEN` | Optional | Shared secret for qB completion callback |
| `TI_CLAMDSCAN_BINARY` | Yes | Malware scanner binary |
| `TI_CLAMDSCAN_ARGS` | Yes | Scanner args |
| `TI_TELEGRAM_BOT_TOKEN` | Optional | Required only if Telegram alerts enabled |
| `TI_TELEGRAM_CHAT_ID` | Optional | Required only if Telegram alerts enabled |

Other tunables are documented in `.env.example`.

## Volume Mounts

The app container should mount:

- host runtime data -> `/app/data`
- host logs -> `/app/logs`
- host local staging -> `/staging-local`
- NAS mount -> `/downloads`

Example host paths (examples only):

- project/build folder: `/opt/docker/torrent-intake`
- runtime data folder: `/opt/docker/torrent-intake-data`
- local staging host path: `/opt/torrent-intake/staging`
- NAS host path: `/mnt/media`

## Docker Compose Example

```yaml
services:
  torrent-intake:
    image: ghcr.io/outlain/torrent-intake-mvp:latest
    container_name: torrent-intake
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - /opt/docker/torrent-intake-data/data:/app/data
      - /opt/docker/torrent-intake-data/logs:/app/logs
      - /opt/torrent-intake/staging:/staging-local
      - /mnt/media:/downloads
    ports:
      - "8095:8000"
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

If you prefer local builds instead of pulling from GHCR, replace `image:` with `build: .`.

## Portainer Stack Example

See `portainer-stack.example.yml` and adjust host paths, qBittorrent endpoint, and credentials.

## API Summary

- `POST /jobs` submit intake job
- `POST /jobs/bulk` submit 1-50 individually tracked intake jobs
- `GET /jobs` list jobs
- `GET /jobs/{job_id}` job detail
- `POST /jobs/{job_id}/retry` retry errored job
- `DELETE /jobs/{job_id}` delete terminal job from intake DB
- `GET /qbt/categories` list qBittorrent categories
- `GET /qbt/final-path-suggestions` list known qB save path suggestions
- `GET /fs/final-path-suggestions` list live directory suggestions inside the approved final roots
- `POST /events/qbt-complete` JSON completion event
- `POST /events/qbt-complete-form` form completion event
- `GET /health` health endpoint
- `GET /ui` basic UI

Example job request:

```json
{
  "magnet_uri": "magnet:?xt=urn:btih:...",
  "final_parent": "/downloads/Movies",
  "final_category": "movies",
  "staging_preference": "local"
}
```

Example bulk job request:

```json
{
  "jobs": [
    {
      "magnet_uri": "magnet:?xt=urn:btih:...",
      "final_parent": "/downloads/Movies",
      "final_category": "movies",
      "staging_preference": "local"
    },
    {
      "magnet_uri": "magnet:?xt=urn:btih:...",
      "final_parent": "/downloads/Shows",
      "final_category": "tv",
      "staging_preference": "nas"
    }
  ]
}
```

## Workflow Architecture

```text
Client -> Intake API -> qBittorrent (staging path)
                          |
                          v
                    Download completes
                          |
                          v
                Malware scan (clamscan)
                  |                     |
                  | infected            | clean
                  v                     v
          Delete torrent+files     Move to final_parent
          Telegram alert sent       Optional final category
```

## Completion Logic

The intake worker only moves to scan/promotion when completion checks pass:

- `progress >= 1.0`
- `amount_left == 0` (when available)
- qBittorrent state is not one of active download/checking states

After that it pauses the torrent, scans content, and only then promotes and resumes for seeding.

## Local Capacity Guard

- `TI_LOCAL_MAX_GIB` remains a hard per-torrent cutoff for local staging.
- If a local-preference torrent is larger than `TI_LOCAL_MAX_GIB`, intake moves it to NAS staging automatically.
- The app measures actual free space on `TI_LOCAL_STAGING_ROOT`.
- It subtracts `TI_LOCAL_FREE_SPACE_BUFFER_GIB` as a safety margin.
- It then reserves the remaining bytes for each active job still downloading locally.
- It also reserves remaining bytes for other qBittorrent downloads already using the local staging mount, even if they were not submitted through intake.
- If a newer local job is still under `TI_LOCAL_MAX_GIB` but would push aggregate reserved bytes past the safe free-space budget:
  - with `TI_LOCAL_OVERFLOW_POLICY=queue`, the job is paused in `waiting_for_local_space` and resumes automatically when space becomes available
  - with `TI_LOCAL_OVERFLOW_POLICY=nas`, the job is moved to NAS staging automatically
- Operators can also manually move queued `waiting_for_local_space` jobs to NAS from the Recent Jobs toolbar.
- On worker startup, intake logs a one-time local staging diagnostic summary showing filesystem totals, current local torrent reservations, and per-torrent local staging usage.
- This decision uses qBittorrent metadata and remaining bytes only; the intake app does not query the public internet for torrent size.

## qBittorrent Completion Hook

Recommended deployment model:

- Configure qBittorrent "Run on torrent finished"
- Use the callback to trigger intake processing immediately
- Raise `TI_POLLING_INTERVAL_SECONDS` to `300` as a fallback safety net instead of relying on 60-second polling
- Ensure qBittorrent and `torrent-intake` share a Docker network so qB can resolve `http://torrent-intake:8000`

Example qBittorrent command when both containers share a Docker network:

```sh
curl -fsS -X POST "http://torrent-intake:8000/events/qbt-complete-form" \
  -F "token=REPLACE_WITH_RANDOM_TOKEN" \
  -F "qbt_hash=%I" \
  -F "tags=%G" \
  -F "content_path=%F"
```

Notes:

- Minimum practical fields are `qbt_hash` and either `tags` or `content_path`.
- `%G` is important because intake can recover the internal `ti_job_*` tag from qB tags.
- Use quotes around qB parameters because names and paths may contain spaces.
- If you do not want callback authentication, leave `TI_COMPLETION_EVENT_TOKEN` blank and omit the `token` form field.
- The callback triggers an immediate per-job processing pass; the background poller remains as a fallback.

## Path Suggestions

- The UI prefills the final path with `TI_FINAL_PARENT_PREFIX` so operators are not retyping the main root for every intake job.
- `TI_FINAL_PARENT_PREFIXES` can add more allowed roots without changing the default prefill.
- Live final-path suggestions browse real directories under the approved roots only.
- If you clear the field and type `/`, the UI suggests the approved roots so you can switch to another mounted destination quickly.
- Arbitrary container paths are still blocked; this protects qBittorrent moves from bad destinations like `/app`, `/var`, or other non-library paths.

## What Should Not Be Committed

Never commit:

- `.env` files
- tokens/passwords/chat IDs
- runtime DB/log files (`*.db`, `*.sqlite*`, `*.log`)
- app runtime directories (`data/`, `logs/`, `/app/data` snapshots)
- local caches/build artifacts (`__pycache__/`, virtualenvs, test caches)
- machine-local files (for example `.DS_Store`)

Use the provided `.gitignore` and `.dockerignore` to keep Git history and Docker build context clean.

## Scanner Notes

- Default scanner command is `clamscan --infected --no-summary --recursive`.
- Keep ClamAV signatures up to date in your deployment (for example via `freshclam` automation or a dedicated scanner sidecar).
- You can override scanner command/flags via `TI_CLAMDSCAN_BINARY` and `TI_CLAMDSCAN_ARGS`.

## Using External ClamAV Containers

If you already run sidecar containers like:

- `clamav_defs_updater` (freshclam loop)
- `clamav_scheduled` (periodic full-library scan/quarantine)
- `clamav_notifier` (log-based Telegram alerts)

you can and should keep them. They are complementary to intake scanning.

Important behavior:

- Torrent Intake scanning is a pre-promotion gate on each completed intake job.
- Scheduled full scans are defense-in-depth for your broader library.
- They are not redundant, especially when staging locally at `/staging-local` (outside `/downloads`).

Recommended integration for `torrent-intake`:

1. Mount shared ClamAV definitions into this container:
   - `/mnt/media/docker/clamav/defs:/var/lib/clamav:ro`
2. Keep `TI_CLAMDSCAN_BINARY=clamscan` unless you intentionally provide `clamdscan` in this same container image.
3. Keep intake Telegram alerts for intake malware deletion events; keep your sidecar notifier for scheduled scanner findings if desired.

Without fresh definitions available to intake scanner, detection quality may be poor or scanner may fail due to missing/outdated DB files.

## Build and Run Locally

```bash
docker build -t torrent-intake-mvp:local .
docker run --rm -p 8095:8000 --env-file .env \
  -v /opt/docker/torrent-intake-data/data:/app/data \
  -v /opt/docker/torrent-intake-data/logs:/app/logs \
  -v /opt/torrent-intake/staging:/staging-local \
  -v /mnt/media:/downloads \
  torrent-intake-mvp:local
```
