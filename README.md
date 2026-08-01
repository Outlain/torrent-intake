# Torrent Intake MVP

A privacy-focused intake controller for qBittorrent.

This service accepts new torrent jobs, stages downloads in a controlled location, scans completed content for malware, deletes infected content, and only promotes clean files to an approved final destination root.

## What This Project Does

Given a magnet link and a final destination path, the app:

1. Creates a tracked intake job.
2. Adds the torrent to qBittorrent with controlled category/tags.
3. Stages download data in either local staging or NAS staging.
4. Scans completed content through a persistent ClamAV daemon (`clamd`).
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
- New scans are blocked when ClamAV is unavailable or definitions exceed the configured stale threshold.
- Scanner engine/policy changes invalidate old file checkpoints; routine signature updates do not restart long scans.
- Files above the configured ClamAV limit fail closed as a scanner safety error. They are never reported as clean or treated as malware.
- On infection, torrent and files are deleted; no promotion occurs.
- Telegram alerts are sent only on infection/deletion.

## Container Paths

These are the required in-container paths:

- NAS root visible to app and qBittorrent: `/downloads`
- Local staging inside app container: `/staging-local`
- App persistent data: `/app/data`
- App logs: `/app/logs`
- Shared ClamAV socket in the app: `/run/clamav/clamd.sock`

The ClamAV sidecar must mount `/staging-local`, `/downloads`, and every additional final root at exactly the same in-container paths as Torrent Intake. The daemon receives file paths over the Unix socket; it does not receive file contents over the network.

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
| `TI_SCANNER_BACKEND` | Yes | `clamd` for the provided sidecar; `command` is a diagnostic/backward-compatible fallback |
| `TI_CLAMD_SOCKET_PATH` | With clamd | Shared Unix socket path, default `/run/clamav/clamd.sock` |
| `TI_SCANNER_POLICY_VERSION` | Yes | Operator-controlled policy revision included in checkpoint identity |
| `TI_SCANNER_MAX_FILE_MIB` | Yes | Fail-closed per-file maximum; must match `clamd.conf` |
| `TI_SCANNER_DEFINITIONS_WARN_HOURS` | No | Signature-age warning threshold, default `36` |
| `TI_SCANNER_DEFINITIONS_STALE_HOURS` | No | Signature age that blocks new file scans, default `72` |
| `TI_MAX_CONCURRENT_SCANS` | No | Normal concurrent scan slots; default `2` |
| `TI_MAX_SCAN_SLOTS` | No | Hard ceiling offered by the UI; default `4` |
| `TI_MAX_CONCURRENT_LARGE_SCANS` | No | Concurrent scans at or above the large threshold; default `1` |
| `TI_LARGE_SCAN_GIB` | No | Torrent size classified as a high-I/O large scan; default `100` GiB |
| `TI_TELEGRAM_BOT_TOKEN` | Optional | Required only if Telegram alerts enabled |
| `TI_TELEGRAM_CHAT_ID` | Optional | Required only if Telegram alerts enabled |

Other tunables are documented in `.env.example`.

## Volume Mounts

The app container should mount:

- host runtime data -> `/app/data`
- host logs -> `/app/logs`
- host local staging -> `/staging-local`
- NAS mount -> `/downloads`
- shared `clamav-socket` volume -> `/run/clamav`

The ClamAV sidecar mounts the same staging/library paths read-only, a persistent definitions volume at `/var/lib/clamav`, and the same socket volume at `/tmp`.

The `clamav` user inside the sidecar must be able to read completed qBittorrent files. Keep the mounts read-only and correct host file/directory modes or ACLs if the UI reports permission-denied scan errors. The first start with an empty definitions volume may take several minutes while FreshClam downloads the initial databases.

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
      - clamav-socket:/run/clamav
    ports:
      - "8095:8000"
    depends_on:
      clamav:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000

  clamav:
    image: clamav/clamav:1.5_base
    pull_policy: always
    restart: unless-stopped
    environment:
      - FRESHCLAM_CHECKS=12
    volumes:
      - clamav-database:/var/lib/clamav
      - clamav-socket:/tmp
      - ./clamav/clamd.conf:/etc/clamav/clamd.conf:ro
      - /opt/torrent-intake/staging:/staging-local:ro
      - /mnt/media:/downloads:ro

volumes:
  clamav-database:
  clamav-socket:
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
- `POST /jobs/bulk-scan-next` prioritize selected queued/active scans
- `POST /jobs/bulk-scan-pause` pause selected scans safely after the current file
- `POST /jobs/bulk-scan-resume` resume selected paused scans
- `GET /scanner/status` get slots, active scans, backlog, and large-scan usage
- `POST /scanner/slots` change scan slots within the configured hard ceiling
- `POST /scanner/maintenance` drain after current files and block/re-enable new scans
- `DELETE /jobs/{job_id}` remove intake tracking row only; qBittorrent torrent is untouched
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
          Durable scan queue (2 slots by default)
                          |
                          v
                Malware scan (per-file checkpoints)
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

After that it pauses the torrent, queues a durable scan, and only promotes and resumes after every file has a clean checkpoint. The completion callback returns after queueing rather than waiting for ClamAV.

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

- The provided sidecar keeps one `clamd` engine resident instead of loading signatures for every file. It runs `freshclam` automatically and persists definitions in the `clamav-database` volume.
- Scanner status reports daemon availability, engine version, signature version/date/age, policy identity, queue capacity, and maintenance drain state. Definitions warn at 36 hours and block new file scans at 72 hours by default.
- A stale or temporarily unavailable daemon defers work without incrementing scanner failure attempts. An already-running file may finish, then the job returns to the queue before another file starts.
- Two scan workers run by default. `TI_MAX_CONCURRENT_LARGE_SCANS=1` keeps two torrents at or above `TI_LARGE_SCAN_GIB` from competing for disk I/O.
- The queue prefers explicitly prioritized work and smaller torrents. A long scan checks whether it should yield after `TI_SCAN_YIELD_AFTER_FILES` completed files.
- The UI can temporarily raise slots up to `TI_MAX_SCAN_SLOTS`. A boost resets to `TI_MAX_CONCURRENT_SCANS` once the backlog clears. Lowering slots never kills active work.
- **Prioritize Scan** applies only to selected queued/active scans. It never resumes a paused scan. **Pause After File** is cooperative: queued work pauses immediately; active work checkpoints its current file first.
- Progress is the exact ratio of checkpointed bytes (or files when byte totals are unavailable). ClamAV does not expose reliable intra-file progress, so the active file is shown as indeterminate. ETA begins after three measured files and includes a confidence rating; it is withdrawn when the current file no longer resembles the learned model.
- Active leases, manifests, timings, and per-file checkpoints are stored in SQLite. A container restart retries only the interrupted file. Definition-only updates preserve checkpoints; file size/mtime, engine, or policy changes invalidate the affected unfinished scan safely.
- Run one Uvicorn application process. Scanner concurrency is managed internally; do not add `--workers`.

### ClamAV Limits

ClamAV cannot scan an individual file larger than approximately 2 GiB. The provided policy uses a 2000 MiB ceiling in both the app and `clamd.conf`. A 500 GiB torrent made of smaller files is supported; a torrent containing a single file above that ceiling is not.

`AlertExceedsMax yes` converts configured size/recursion limit hits into `Heuristics.Limits.Exceeded...` results. Torrent Intake treats those as scanner-policy errors, not malware: promotion is blocked, but no torrent or file is auto-deleted. Review the error, adjust an explicitly audited policy if appropriate, increment `TI_SCANNER_POLICY_VERSION`, and retry.

ClamAV recommends substantial memory for daemon operation and concurrent signature reloads. Plan for roughly 4 GiB rather than constraining the sidecar to 2 GiB. See the [official Docker guidance](https://docs.clamav.net/manual/Installing/Docker.html) and the [upstream limit definitions](https://github.com/Cisco-Talos/clamav/blob/main/etc/clamd.conf.sample).

### Updates And Maintenance

Routine signature updates require no operator action. The sidecar checks 12 times per day, validates downloaded databases, and `clamd` reloads them concurrently. Files already scanning continue on the old in-memory engine; subsequent files use the new definitions. Signature revisions are recorded per file and do not discard completed checkpoints.

Engine/software updates are separate. The `clamav/clamav:1.5_base` feature tag follows 1.5.x patch/security releases when the image is pulled, but a normal container restart does not fetch a new image. Use this workflow:

1. Click **Start Maintenance**.
2. Wait until scanner status says **maintenance ready: drained**.
3. Pull and recreate only the `clamav` sidecar.
4. Confirm the UI reports a healthy daemon and the intended engine version.
5. Click **End Maintenance**.

```bash
docker compose -f docker-compose.example.yml pull clamav
docker compose -f docker-compose.example.yml up -d --no-deps clamav
```

Maintenance stops new files and lets each active scan finish its current file; it does not kill or restart ClamAV itself. Torrent Intake intentionally has no Docker socket. If ClamAV or the app is restarted unexpectedly, the current file returns to pending without losing earlier checkpoints or consuming a failure attempt.

If you change `clamd.conf` limits or policy rather than only updating signatures, increment `TI_SCANNER_POLICY_VERSION`. Engine/policy changes force a one-time rescan of completed files in unfinished jobs; normal signature updates do not.

The `command` backend remains available for diagnostics or legacy deployments through `TI_CLAMDSCAN_BINARY` and `TI_CLAMDSCAN_ARGS`. It is not the recommended normal workflow. Existing scheduled full-library scanners remain useful defense-in-depth and can run alongside the pre-promotion gate.

## Build and Run Locally

```bash
docker build -t torrent-intake-mvp:local .
docker compose -f docker-compose.example.yml up -d
```

For a local app build, replace the app service's `image:` line with `build: .`. The sidecar is required when `TI_SCANNER_BACKEND=clamd`.
