# Torrent Intake MVP

A Dockerized intake controller for qBittorrent. It adds torrents to an approved
staging root, watches completion, pauses them, scans every regular file with
restart-safe checkpoints, and only then performs the configured clean or infected
action.

The intentionally broad `/mnt/media:/downloads` mount is preserved. Final-path
validation allows normal media libraries below `/downloads` while rejecting
traversal, symlink escapes, staging roots, and operational locations such as
`/downloads/docker`, `/app`, `/state`, `/var/lib/clamav`, and `/quarantine`.

## Containers and ClamAV flow

This repository publishes two images:

- `torrent-intake-mvp`: FastAPI/UI, SQLite job state, qBittorrent control, path
  checks, and a descriptor-streaming ClamD client. It does not contain ClamAV.
- `torrent-intake-clamd`: persistent ClamD with the shared definitions mounted
  read-only. It has no media mount and `network_mode: none`.

The containers share only the dedicated host socket directory configured by
`TI_CLAMD_SOCKET_HOST_DIR`. The socket is mode `0600`, the directory is mode
`0750`, both processes use the same UID/GID, and there is no ClamD TCP listener
or Docker socket. A bind mount is used so a deployment UID other than the image's
default does not inherit incompatible ownership from a Docker-managed volume.
The sidecar never runs FreshClam; the separate
`clamav-defs-updater` is the sole signature writer. `SelfCheck 300` notices
atomic definition updates. ClamD writes to container stderr, so the Compose
`json-file` rotation policy covers daemon logs.

Normal scanning uses ClamD `INSTREAM`, not a new `clamscan` process. The app opens
each file with `O_NOFOLLOW`, checks device/inode/size/mtime/ctime against its
manifest, streams the descriptor, then verifies both descriptor and pathname
identity again. ClamAV limit detections, malformed replies, unavailable/stale
definitions, and socket failures never become clean verdicts.

The default per-file limit is `2000 MiB`, matching the bounded sidecar. A larger
file or a `Heuristics.Limits.Exceeded`/stream-limit reply becomes a durable
`scan_failed` event with `failure_kind=scan_policy_limit`; the job enters an error
state and the paused torrent is not promoted. Media extensions are not an
allow-list because an extension is not proof of content type or safety.

## qBittorrent safety gates

Before scanning, final promotion, quarantine, or deletion, the current
qBittorrent record is fetched again. Depending on the action, the service checks:

- the torrent hash still matches the job;
- both the managed tag and unique `ti_job_*` tag remain present;
- no other active job owns the hash or unique tag;
- progress and `amount_left` indicate completion and the state is not downloading
  or checking;
- qBittorrent has confirmed a paused/stopped state before any scan or destructive
  action;
- both `save_path` and `content_path` canonically resolve inside the job's exact
  local or NAS staging root;
- the torrent has not been manually moved elsewhere;
- the canonical final destination is still inside an approved media root; and
- post-move save/content paths match the expected final or quarantine boundary.

Clean promotion is only marked done after qBittorrent reports the canonical final
save path. The torrent is then optionally categorized and resumed for seeding.
Any failed safety check leaves the job retryable or in an explicit error state.

## Durable scan checkpoints

SQLite under `/app/data` stores one `scan_files` row per relative path, including
its full identity, verdict, ClamAV engine/database identity, policy identity,
attempt count, and timing. A restart returns only an in-progress file to pending;
already clean files remain checkpointed. The manifest is rebuilt after all
pending files finish, so additions, replacements, deletions, symlinks, and
special files block the final clean gate. Engine or policy changes reset relevant
checkpoints; a signature-only database update is recorded without discarding
completed per-file work.

Scan slots, large-job limits, leases, heartbeats, exponential retry, operator
pause/resume, prioritization, and maintenance drain are bounded and persistent.

## Infection actions

`TI_INFECTED_ACTION` supports:

- `hold` (default): keep the verified torrent paused in staging;
- `quarantine`: move it through qBittorrent into a new exclusive directory under
  `/quarantine`, never reusing an existing name; or
- `delete`: explicitly delete the qBittorrent torrent and files, then verify both
  qBittorrent disappearance and staging-content removal before marking success.

For `quarantine`, qBittorrent must also see
`/opt/docker/clamav-shared/quarantine/torrent-intake:/quarantine` at the same
container path. The safer default `hold` preserves compatibility with the current
qBittorrent mounts and needs no extra mount. Automatic deletion is never the only
choice.

Threats and actions are atomically written to
`/opt/docker/clamav-shared/events/torrent-intake`. Event IDs for terminal actions
are deterministic, so retries do not duplicate Telegram delivery. Operational
failures get distinct IDs for notifier aggregation. `clamav-notifier` is the sole
Telegram sender; the former direct Telegram code and dependency were removed.
Events never include passwords, tokens, passkeys, or magnet URIs.

## Paths and mounts

| Host | Container | Access/reason |
| --- | --- | --- |
| `/opt/docker/torrent-intake/data` | `/app/data` | rw SQLite and migrations |
| `/mnt/bulk/docker/torrent-intake/staging` | `/staging-local` | rw local staging |
| `/mnt/media` | `/downloads` | rw NAS staging and media destinations |
| `/opt/docker/clamav-shared/events/torrent-intake` | `/events` | rw durable events |
| `/opt/docker/clamav-shared/quarantine/torrent-intake` | `/quarantine` | rw optional infection action |
| `/opt/docker/clamav-shared/defs` | sidecar `/var/lib/clamav` | read-only definitions |
| `/opt/docker/clamav-shared/sockets/torrent-intake` | both `/run/clamav` | rw private socket only |

The examples run both images as UID/GID `10001:10001`. Prepare their dedicated
host directories without changing ownership of the whole media tree:

```sh
sudo install -d -m 0750 -o 10001 -g 10001 \
  /opt/docker/torrent-intake/data \
  /opt/docker/clamav-shared/events/torrent-intake \
  /opt/docker/clamav-shared/quarantine/torrent-intake \
  /opt/docker/clamav-shared/sockets/torrent-intake
```

If you set `INTAKE_UID`/`INTAKE_GID` to another identity, create every dedicated
directory above with that same numeric owner before starting Compose. The socket
directory is runtime-only; delete stale `clamd.sock` and `clamd.pid` files only
while both Torrent Intake containers are stopped.

Grant that identity the required access to the existing staging and media paths
with the host's normal group or ACL policy. The definition directory only needs
read/search access in the sidecar.

Application logs use stdout/stderr and the Compose `json-file` rotation policy;
there is no unused `/app/logs` bind mount. The current qBittorrent mounts remain:

```yaml
- /mnt/media:/downloads
- /mnt/bulk/docker/torrent-intake/staging:/staging-local
```

This stays compatible with qBittorrent running in Gluetun's network namespace.
Set `TI_QBT_HOST` to the Web API endpoint reachable from Torrent Intake. Attach
the application (not the ClamD sidecar) to an existing private Docker network if
that endpoint relies on Docker DNS.

## Important configuration

Copy `.env.example` to a mode-`0600` `.env`. The Compose and Portainer examples
show all bounded scanner settings. Important values include:

| Variable | Default/example | Purpose |
| --- | --- | --- |
| `TI_QBT_HOST` | `http://qbittorrent:8080` | reachable qB Web API |
| `TI_QBT_USERNAME`, `TI_QBT_PASSWORD` | required deployment values | qB credentials |
| `TI_COMPLETION_EVENT_TOKEN` | required in examples | authenticate completion hook |
| `INTAKE_UID`, `INTAKE_GID` | `10001` | shared numeric identity for the app, sidecar, and writable host paths |
| `TI_CLAMD_SOCKET_HOST_DIR` | `/opt/docker/clamav-shared/sockets/torrent-intake` | private host socket directory |
| `TI_LOCAL_STAGING_ROOT` | `/staging-local` | exact local staging boundary |
| `TI_NAS_STAGING_ROOT` | `/downloads/torrent-intake/staging` | exact NAS staging boundary |
| `TI_FINAL_PARENT_PREFIX` | `/downloads` | primary allowed media root |
| `TI_FINAL_PARENT_PREFIXES` | empty | optional additional mounted media roots |
| `TI_SCANNER_MAX_FILE_MIB` | `2000` | per-file fail-closed ClamD bound |
| `TI_SCANNER_SCAN_TIMEOUT_SECONDS` | `1200` | total per-file client deadline |
| `TI_MAX_CONCURRENT_SCANS` | `2` | normal scan slots |
| `TI_MAX_SCAN_SLOTS` | `4` | operator hard ceiling |
| `TI_INFECTED_ACTION` | `hold` | `hold`, `quarantine`, or `delete` |

Local capacity control retains the existing behavior: a hard per-torrent local
limit, a free-space buffer, reservation of remaining bytes for all local qB
downloads, and either queueing or NAS overflow. The worker logs startup capacity
diagnostics but never queries public torrent services.

The UI/API has no login. The example binds it to
`127.0.0.1:${TI_UI_HOST_PORT:-8095}`; place an authenticated reverse proxy in
front before remote exposure.

## Completion hook

Polling remains a recovery fallback. For faster handoff, qBittorrent can run:

```sh
curl -fsS -X POST "http://torrent-intake:8000/events/qbt-complete-form" \
  -F "token=REPLACE_WITH_RANDOM_TOKEN" \
  -F "qbt_hash=%I" \
  -F "tags=%G" \
  -F "content_path=%F"
```

Use the actual private service address in the Gluetun deployment and quote qB
placeholders. The background poller still discovers missed callbacks.

## API summary

- `POST /jobs` and `POST /jobs/bulk`: create intake jobs
- `GET /jobs`, `GET /jobs/{id}`: inspect jobs
- retry, bulk retry/delete/clear, move-to-NAS, and scan priority/pause/resume
  endpoints used by the UI
- `GET /scanner/status`, `POST /scanner/slots`, `POST /scanner/maintenance`
- qB category/transfer and approved final-path suggestion endpoints
- `POST /events/qbt-complete` and `/events/qbt-complete-form`
- `GET /health` and `GET /ui`

Job deletion endpoints remove only Torrent Intake tracking unless the explicitly
configured infected action is `delete`.

## Deployment, migration, and builds

Prepare all writable host directories for the configured numeric UID/GID, deploy
`clamav-defs-updater`, then start both services in this repository:

```sh
TI_QBT_PASSWORD=test TI_COMPLETION_EVENT_TOKEN=test \
  docker compose -f docker-compose.example.yml config --quiet
docker compose -f docker-compose.example.yml up -d
```

At startup SQLAlchemy creates new tables and `upgrade_schema()` applies additive,
idempotent columns/indexes to existing SQLite databases. Back up the database
before the first deployment. Per-file scan state and interrupted actions are
recovered automatically.

```sh
docker build -t torrent-intake-mvp:test .
docker build -f Dockerfile.clamd -t torrent-intake-clamd:test .
docker run --rm --mount type=bind,src="$PWD",dst=/workspace,readonly \
  --entrypoint python torrent-intake-mvp:test \
  -m unittest discover -s /workspace/tests -v
```

Both containers run non-root with read-only root filesystems, dropped
capabilities, no-new-privileges, bounded PIDs/CPU/memory, tmpfs scratch, health
checks, and rotated Docker logs. GitHub Actions tests and publishes both images
for `linux/amd64` and `linux/arm64`. See the parent
`REPOSITORY_TRANSITION.md` for suite-wide migration and rollback steps.
