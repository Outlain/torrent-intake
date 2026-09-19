# Torrent Intake configuration reference

There are two separate kinds of configuration:

- **Docker deployment:** where containers, disks, sockets and networks exist.
  These must be configured before uploading a backup.
- **Application settings:** how Intake manages and scans torrents. These live in
  `/app/data/settings.json` and are included in encrypted backups.

With the standard container paths, **no application environment override is
required**, including when restoring a backup. Fresh installations start paused
so you can configure or restore them first. A working ClamD sidecar is still
required by the example stack's `depends_on` before the application starts.

## Deployment variables

These names are substitutions in [the stack example](portainer-stack.example.yml),
not UI settings. You can use literal values in YAML instead. A Portainer variable
does nothing unless the YAML refers to it. Defaults below are the YAML fallbacks;
the supplied `.env.example` explicitly selects UID/GID `3000:3000`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_OWNER` | `outlain` | GHCR owner of the two published images. |
| `IMAGE_TAG` | `latest` | Image release/tag; record the working version for recovery. |
| `INTAKE_UID`, `INTAKE_GID` | `10001`, `10001` | Numeric owners for both processes. Set **both to 3000** for the current deployment and retain matching host permissions. |
| `TI_UI_PUBLISH_IP` | `127.0.0.1` | Host interface for the UI. Localhost requires a tunnel/proxy for remote access; a host LAN address permits access through that interface. |
| `TI_UI_HOST_PORT` | `8095` | Browser-facing host port; the app still listens on container port `8000`. |
| `MEDIA_NETWORK` | `shared_media_net` | Existing external Docker network that can reach qBittorrent's API endpoint. VLAN/VPN routing is managed outside Intake. |
| `TI_DATA_HOST_DIR` | `/opt/docker/torrent-intake/data` | Local SSD/M.2 directory for SQLite, settings, token and restore work. |
| `TI_LOCAL_STAGING_HOST_DIR` | `/mnt/bulk/docker/torrent-intake/staging` | Local unfinished/intake torrent content. |
| `TI_MEDIA_HOST_DIR` | `/mnt/media` | Intentional broad media mount, including NAS staging. |
| `TI_EVENTS_HOST_DIR` | `/opt/docker/clamav-shared/events/torrent-intake` | Durable event spool read by the central notifier. |
| `TI_QUARANTINE_HOST_DIR` | `/opt/docker/clamav-shared/quarantine/torrent-intake` | Used for the optional `quarantine` infection action. |
| `TI_CLAMD_SOCKET_HOST_DIR` | `/opt/docker/clamav-shared/sockets/torrent-intake` | Private directory mounted into **both** app and sidecar. |
| `CLAMAV_DEFS_HOST_DIR` | `/opt/docker/clamav-shared/defs` | Same shared definitions directory used by the sole updater. Sidecar mounts it read-only. |
| `DEFINITIONS_MAX_AGE_SECONDS` | `172800` | Sidecar startup/health-check maximum definition age (48 hours). |
| `CLAMD_MAX_SCAN_SIZE_MIB` | `2000` | Sidecar cumulative parser/expanded-data budget; explicit opt-in up to `4000`. Not the raw-file size boundary. |

`DEFINITIONS_WAIT_TIMEOUT=1800` is set directly on the sidecar in the example:
it waits up to 30 minutes for readable, fresh definitions at startup.
`DEFINITIONS_DIR` and `CLAMD_SOCKET` were removed from the example because their
health-check defaults already match the image's fixed `DatabaseDirectory` and
`LocalSocket`. Leaving the old mappings at `/var/lib/clamav` and
`/run/clamav/clamd.sock` is harmless. Changing those environment values alone does
**not** change the daemon's configured paths; keep the standard container paths.

Image, network, bind mounts, `user`, port publishing, CPU/RAM/PID limits, tmpfs,
capabilities, restart policy and Docker log rotation remain in YAML. None can be
restored or provisioned by uploading the application backup. The app has no
Docker socket. Keep your actual YAML and Portainer variable values separately
in your deployment backup. The settings UI does not require pasted YAML. Any
legacy `deployment-notes.txt` is still preserved in encrypted backups/restores.

### Mounts that must exist

| App/sidecar destination | Required relationship |
| --- | --- |
| App `/app/data:rw` | Local persistent SSD, writable by the application UID. No sharing with qBittorrent. |
| App `/staging-local:rw` | qBittorrent must see the **same actual data at `/staging-local`**. |
| App `/downloads:rw` | qBittorrent must see the **same media/NAS data at `/downloads`**, including `/downloads/torrent-intake/staging`. |
| App `/events:rw` | The notifier's parent `/events` mount must include this producer directory. No private settings or backups belong here. |
| Both `/run/clamav:rw` | Exact same private socket directory and compatible ownership; no other media scanner shares this socket. |
| Sidecar `/var/lib/clamav:ro` | Updater's shared definitions; do not add a second FreshClam writer. |
| App `/quarantine:rw` | Needed for `infected_action=quarantine`; qBittorrent then needs the same actual directory at `/quarantine` too. Not needed for `hold` or `delete`. |
| Both `/tmp` tmpfs | Writable scratch for read-only-root containers; preserve sufficient bounded space for each service. |

The example includes the optional quarantine mount for convenience. It can be
removed when the restored policy is `hold` or `delete`. Resume still requires
accessible local and NAS staging directories for this standard two-tier setup.
Neither downloaded content nor qBittorrent's own state is in the Intake backup.

## Application settings

Priority: explicit container environment → legacy **in-container** `.env` →
`settings.json` → built-in default. Effective environment overrides are saved
locally on startup, so removing the mapping later retains the value. Blank is
an explicit value, not a request to use the saved value. The host Compose `.env`
is not automatically mounted into the application.

Use lower-case field names without `TI_` in the file: `TI_LOCAL_MAX_GIB` becomes
`"local_max_gib": 200`. Settings & Help shows active values, defaults, descriptions
and override sources, grouped into Connection, Downloads/storage, Scanning,
General, Backup/restore and Deployment sections. The legacy no-op `TI_APP_NAME`
is accepted in existing files but omitted from the editor.

Unlock with the local admin token, edit individual fields, then **Review changes**.
Nothing is saved during editing or review. **Pause and save** requests a whole-
controller pause and waits for workers to drain before saving atomically. If the
wait expires, edits stay unsaved and Intake stays paused; retry once drained.
Restart `torrent-intake` in Portainer, reopen the page, unlock and choose
**Verify & resume**. Saved-but-not-applied values are labeled separately from
active values. A stale draft from another session is rejected instead of
silently overwriting newer settings.

Scanner limits, definition ages and other advanced fields require both the
Scanning section's warning unlock and confirmation at review. Destructive
infection actions, filesystem/database boundaries, executable paths, ownership
tags, TLS policy and scanner implementation/policy identifiers remain
deployment-only. Change these offline in the file or by explicit environment
override, not through this UI. An environment-controlled field explains which
`TI_*` mapping to **remove entirely** and redeploy before editing locally.

Secret fields are blank replacement inputs: leave blank to retain the saved
secret. Optional fields have an explicit clear checkbox. Connection testing
uses the unsaved connection fields in a separate short-lived session; it only
authenticates and reads the API version, never changes torrents or saves settings.
The test has a short fixed request timeout, independent of the normal worker's
request timeout. **Check setup** tests active qBittorrent/ClamD settings and
directory permissions; it cannot prove that an intended NAS export is mounted.

`TI_DATA_DIR`, if changed, must be provided in the container environment because
it locates the settings file itself.

The tables below show built-in defaults, not overrides from your deployment.

### Application and connection

| Variable | Default | Purpose |
| --- | --- | --- |
| `TI_DATA_DIR` | `/app/data` | Bootstrap container data directory. Omit for the standard mount; not the host pathname. |
| `TI_DATABASE_URL` | SQLite file in `TI_DATA_DIR` named `torrent_intake.db` | Jobs/checkpoints database. Portable restore requires local file-based SQLite within the data directory. |
| `TI_DEBUG` | `false` | Verbose application logging; normally leave off. |
| `TI_UI_TITLE` | `Torrent Intake` | Page heading. |
| `TI_APP_NAME` | `torrent-intake` | **Legacy no-op**, accepted to avoid breaking older saved files. Not UI-editable; use `TI_UI_TITLE` for the heading. |
| `TI_QBT_HOST` | `http://qbittorrent:8080` | API address reachable from Intake's Docker network. Keep your working endpoint; there is no Gluetun requirement. |
| `TI_QBT_USERNAME`, `TI_QBT_PASSWORD` | `admin`, placeholder | qBittorrent credentials; must be set correctly or restored before resuming. |
| `TI_QBT_VERIFY_CERTIFICATE` | `false` | Validate the server certificate for HTTPS. Separate from the connection URL. |
| `TI_QBT_REQUEST_TIMEOUT_SECONDS` | `20` | Time bound on a qBittorrent API request. |
| `TI_QBT_WEB_URL` | unset | Browser-facing qBittorrent link; may differ from the internal API address. |
| `TI_INTAKE_CATEGORY` | `intake` | qBittorrent category while downloading/intaking. |
| `TI_MANAGED_TAG` | `torrent_intake` | Required ownership tag, not an ordinary descriptive/custom tag. |
| `TI_AUTO_CREATE_FINAL_CATEGORY` | `true` | Allow creation of a requested final qBittorrent category. |
| `TI_COMPLETION_EVENT_TOKEN` | unset | Optional authenticated completion callback. Polling works without it. Update the callback target when moving hosts. |

### Storage and torrent placement

| Variable | Default | Purpose |
| --- | --- | --- |
| `TI_LOCAL_STAGING_ROOT` | `/staging-local` | Container-visible local intake boundary. |
| `TI_NAS_STAGING_ROOT` | `/downloads/torrent-intake/staging` | Temporary NAS intake boundary, not the final library destination. |
| `TI_FINAL_PARENT_PREFIX` | `/downloads` | Primary allowed final media root. |
| `TI_FINAL_PARENT_PREFIXES` | unset | Optional additional allowed roots, comma-separated; does not replace the primary root. |
| `TI_LOCAL_OVERFLOW_POLICY` | `queue` | Wait for aggregate local capacity (`queue`) or switch eligible work to NAS staging (`nas`). |
| `TI_LOCAL_MAX_GIB` | `200` | Per-torrent local-size ceiling; larger torrents use NAS staging. Not the scan limit. |
| `TI_LOCAL_FREE_SPACE_BUFFER_GIB` | `5` | Reserve space in addition to outstanding-download capacity accounting. |
| `TI_POLLING_INTERVAL_SECONDS` | `300` | Background management/reconciliation interval. Separate from scan scheduling. |
| `TI_COMPLETION_GRACE_SECONDS` | `15` | Delay before completion/pause verification. |

### Scanner and media policy

| Variable | Default | Purpose |
| --- | --- | --- |
| `TI_SCANNER_BACKEND` | `clamd` | Compatibility selector: only `clamd` is accepted. No need to set it. |
| `TI_CLAMD_SOCKET_PATH` | `/run/clamav/clamd.sock` | Application socket address; must match the sidecar. |
| `TI_SCANNER_POLICY_VERSION` | `clamav-policy-v5-media-attachments` | Checkpoint policy identity; changing it invalidates incompatible clean checkpoints. |
| `TI_SCANNER_MAX_FILE_MIB` | `2000` | Raw-size boundary for native streams (maximum 2000), not a whole-torrent limit. |
| `TI_SCANNER_HEALTH_CACHE_SECONDS` | `15` | Reuse a successful ClamD health result briefly. |
| `TI_SCANNER_CONNECT_TIMEOUT_SECONDS` | `5` | Private socket connection deadline. |
| `TI_SCANNER_SCAN_TIMEOUT_SECONDS` | `1200` | Native per-file/request deadline, not a whole-torrent deadline. |
| `TI_SCANNER_DEFINITIONS_WARN_HOURS` | `36` | Warn when the daemon-reported definition timestamp is old. |
| `TI_SCANNER_DEFINITIONS_STALE_HOURS` | `72` | Application fail-closed age. Explicit older-stack overrides such as 48 still win. |
| `TI_LARGE_MEDIA_ENABLED` | `true` | Enable validated large-video/raw-TrueHD routing; not arbitrary large-file acceptance. |
| `TI_LARGE_MEDIA_MAX_FILE_GIB` | `100` | Ceiling for one large media file, not the entire torrent. |
| `TI_LARGE_MEDIA_CHUNK_MIB` | `512` | Initial size of each independently scanned overlapping byte window. |
| `TI_LARGE_MEDIA_MIN_CHUNK_MIB` | `64` | Smallest subdivision after a raw file/stream size limit; parser/expansion failures are not bypassed. |
| `TI_LARGE_MEDIA_OVERLAP_KIB` | `1024` | Repeated edge bytes between windows. |
| `TI_LARGE_MEDIA_PROBE_TIMEOUT_SECONDS` | `120` | Deadline for each media validation/extraction helper invocation. |
| `TI_LARGE_MEDIA_SCAN_TIMEOUT_SECONDS` | `172800` | Total deadline for one large media file (two days), not the torrent's lifetime. |
| `TI_FFPROBE_BINARY` | `/usr/bin/ffprobe` | Image-provided media inspector; normally leave unchanged. |
| `TI_FFMPEG_BINARY` | `/usr/bin/ffmpeg` | Image-provided bounded attachment extractor; normally leave unchanged. |
| `TI_MEDIA_ATTACHMENT_MAX_MIB` | `16` | Per-attachment extraction budget, hard maximum 64. |
| `TI_MEDIA_ATTACHMENT_TOTAL_MIB` | `64` | All extracted attachments in one media file, hard maximum 256. |

App definition-age checks use the loaded daemon's reported timestamp. Sidecar
startup/health checks inspect database files on disk. They are independent checks,
not interchangeable settings; the default ages differ, and an explicit 48-hour
application policy is preserved during migration.

### Scheduling, retries and actions

| Variable | Default | Purpose |
| --- | --- | --- |
| `TI_PER_JOB_SCAN_WORKERS` | `1` | Parallel large-file windows within one torrent; set 4 for the previously discussed four-worker setup. |
| `TI_CLAMD_MAX_INFLIGHT_REQUESTS` | `4` | Global concurrent scan requests across all torrents; maximum 4 for this sidecar. |
| `TI_MAX_CONCURRENT_SCANS` | `2` | Default torrent scan slots for a new queue or when a temporary boost ends. |
| `TI_MAX_SCAN_SLOTS` | `4` | Hard ceiling for live scan-slot controls. |
| `TI_MAX_CONCURRENT_LARGE_SCANS` | `1` | Concurrent torrents at or above the large-job threshold. |
| `TI_LARGE_SCAN_GIB` | `2` | Threshold for the previous rule; not the largest accepted torrent size. |
| `TI_SCAN_SCHEDULER_INTERVAL_SECONDS` | `3` | Interval between claiming pending scan work. |
| `TI_SCAN_LEASE_SECONDS` | `90` | Scan ownership lease; effective minimum is also three heartbeats and 30 seconds. |
| `TI_SCAN_HEARTBEAT_SECONDS` | `10` | Refresh cadence for active scan ownership/progress. |
| `TI_SCAN_RETRY_BASE_SECONDS` | `30` | Starting delay for exponential retries. |
| `TI_SCAN_MAX_FAILURES` | `3` | Consecutive failure threshold requiring an operator retry. |
| `TI_SCAN_YIELD_AFTER_FILES` | `10` | Checkpointed files per turn before yielding to other jobs. |
| `TI_PAUSE_CONFIRMATION_TIMEOUT_SECONDS` | `30` | Wait for qBittorrent to confirm pause before safety-sensitive operations. |
| `TI_INFECTED_ACTION` | `hold` | `hold`, `quarantine` or explicit `delete` after verified infection. Never editable in the UI. |
| `TI_QUARANTINE_ROOT` | `/quarantine` | Infection destination used only for `quarantine`. |
| `TI_EVENT_DIR` | `/events` | Persistent structured-event spool; central notifier handles Telegram. |

Saved runtime slot/maintenance requests live in SQLite, not settings.json; a
restart does not simply discard those requests. Job-level paths, categories and
custom tags also belong to their database records, not the global settings file.

## Token and backup limit

The random administrator token is created only when `/app/data/admin-token` is
absent, then reused on subsequent starts. Read it whenever needed with:

```sh
docker exec torrent-intake cat /app/data/admin-token
```

This command **reads**, not creates or changes, the token. It authorizes only
the new administrative controls; it is not required for ordinary operation and
is not the passphrase encrypting backups. Page reloads forget the token in browser
memory. A recreated container retaining the data mount uses the same token; a
fresh data directory creates another. Restoring a backup preserves the receiving
installation's token, so the original token is not needed for recovery. The
backup passphrase **is** required and cannot be recovered by the app.

The **512 MiB ceiling applies only to portable backup/restore**, not the live
SQLite database, downloaded content or the amount a torrent can contain.
SQLite itself supports much larger databases ([SQLite limits](https://www.sqlite.org/limits.html)).
The backup budget bounds uploaded input, temporary copies, verification time,
and the browser's buffered download. Simply raising the constant would not
address every disk, timeout, proxy or browser-memory limit.

Retain 512 MiB for now unless your measured database size warrants a larger
backup design. A torrent's terabyte size does not imply a terabyte of database
data: Intake records metadata/checkpoints, not file contents. Many small files,
long retained job histories and long paths/errors can still grow the database;
there is no basis to guarantee it will never exceed 512 MiB. Unlock administration
to see its logical size (including committed WAL pages). This is a metadata-only
check, not a scan of all jobs or files. Larger databases need an offline backup
procedure until larger portable backups are implemented and tested.

## Redundancy review

- Removed default-only `TI_DATA_DIR`, `DEFINITIONS_DIR` and `CLAMD_SOCKET`
  mappings from the standard examples; old mappings at the standard paths work.
- `TI_APP_NAME` is a legacy no-op, omitted from the editor. It remains accepted
  so existing local files and backups are not broken by removal.
- `TI_SCANNER_BACKEND=clamd` is optional and retained as a compatibility guard;
  setting `clamscan` must still fail rather than silently change behavior.
- Keep native versus large-media limits/deadlines, default versus maximum slots,
  per-job versus global workers, and primary versus additional roots separate:
  they control different boundaries. Merging them would hide real behavior.
- Old `TI_CLAMDSCAN_*` and `TI_TELEGRAM_*` settings are not used by this app.
  Remove them from old stacks; ClamD scans and the central notifier sends Telegram.
- There is no Torrent Intake `APP_MODE=ui`, separate UI port variable, automatic
  host-mount restoration, backup-passphrase environment variable or admin-token
  environment variable. Do not add them to the stack.

See [the recovery procedure](README.md#download-and-restore) before simplifying
an existing stack: **run the new image with the old effective environment first**.
