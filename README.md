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

- `torrent-intake`: FastAPI/UI, SQLite job state, qBittorrent control, path
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

The native raw-file and `INSTREAM` boundary remains `2000 MiB`. That is now a
routing boundary, not the application's final ceiling. A larger file is accepted
only when `ffprobe` identifies either an approved container with a real video
stream and no unsupported stream or attachment type, or narrowly validated raw
TrueHD audio. Torrent Intake then reads every byte in independent `512 MiB` ClamD
windows with a `1024 KiB` overlap. Up to four windows from that file are streamed
concurrently through separate private Unix-socket connections. The file is
opened once and read with explicit offsets; Torrent Intake does not copy it or
create temporary chunk files. The overlap allows short byte signatures crossing
a window edge to remain visible; it cannot preserve every signature or parser
context. Device, inode, size, mtime, and ctime are still checked throughout.

Approved large-video containers are ASF, AVI, FLV, Matroska/WebM, MOV/MP4, MPEG,
MPEG-TS, and Ogg. Raw TrueHD is the only approved audio-only format and must
have a `.thd` or `.truehd` suffix, byte-level `ffprobe` identification as
`truehd`, and exactly one TrueHD audio stream. Other audio-only formats remain
held. Media recognition never relies on the filename extension alone.
For example, an `.avi` filename whose contents are identified as ASF uses the
ASF route and still requires a video stream and supported stream types.

Matroska attachments named exactly `kodi-metadata` or `kodi-override-metadata`
are accepted with a declared `application/xml`, `text/xml`, or `text/plain` MIME
type. These are [Kodi's embedded NFO names](https://kodi.wiki/view/Video_file_tagging#MKV_tag_options),
which have no filename extension.
This is an admission rule, not proof that the attachment is harmless: its bytes
are still included in the ClamD windows and are also scanned as complete objects.
No attachment is opened in Kodi or used to fetch a URL. Missing/other MIME types and other
extensionless attachments remain blocked. The existing font, image, subtitle,
and text suffix rules are unchanged.

### Complete attachment scans

The large-media route extracts each reported attachment (including attached cover
images) and requires a complete native ClamD scan of that object before scanning
the video windows. Matroska content identified by its EBML header also gets this
attachment pass after a successful native scan, even below 2000 MiB. Small
audio-only Matroska files remain supported; cover art alone cannot qualify an
oversized file as video. This does not claim to extract every possible metadata
field or codec payload from every container.

Extraction uses FFmpeg stream-copy/attachment output, not movie transcoding or
general-purpose torrent archive extraction. It reads from the already-open source
descriptor. One object at a time is bounded while arriving through a pipe, then
written to a private, application-named temporary file under `/tmp`. The source
identity is checked again; each temporary object is scanned whole and removed
before the next. Embedded filenames are never used as output paths. An attachment
limit/error cannot enter the media fallback or be subdivided.

Defaults are `TI_MEDIA_ATTACHMENT_MAX_MIB=16` per attachment and
`TI_MEDIA_ATTACHMENT_TOTAL_MIB=64` per media file, with at most 64 attachments.
The configurable hard ceilings are 64 MiB per attachment and 256 MiB total.
Unknown-size cover images reserve their per-attachment maximum against the total
budget. Empty, oversized, missing, or incomplete output holds the torrent.
There is no extra persistent volume or database. The application's existing
256 MiB `/tmp` tmpfs covers the default bounded temporary files; streamed movie
windows still use the separate ClamD tmpfs.

FFprobe and FFmpeg run with 512 MiB address-space, CPU-time, and wall-clock bounds,
a 64 MiB single-allocation limit, and no regular-file output allowance. FFprobe
stdout is limited to 1 MiB and both tools' stderr to 64 KiB *while being read*.
Attachment stdout is limited to its remaining extraction budget. Pause/lost-lease
requests terminate and reap the helper. Protocol and format allowlists exclude
network/playlist demuxers. These are resource and input restrictions, not a
complete security sandbox for a compromised parser; keep FFmpeg and the image
updated. `TI_LARGE_MEDIA_PROBE_TIMEOUT_SECONDS` bounds each helper invocation.

`MaxScanSize` measures parser/expanded data, not only the input file's raw size.
It defaults to `2000 MiB` and the sidecar permits a bounded deployment override
up to `4000 MiB` with `CLAMD_MAX_SCAN_SIZE_MIB`. Consequently, a file below
`2000 MiB` can still reach the default limit during its native scan; selecting
`4000` gives ClamAV more internal-analysis headroom without raising its technical
raw-file or stream boundary. When that limit response still occurs, Torrent
Intake requires the same `ffprobe` media validation and retries the file through
the bounded overlapping-window route. This fallback never applies merely because
a filename looks like media: archives, unknown formats, and unsafe attachments
remain held without a clean verdict.

The sidecar validates `CLAMD_MAX_SCAN_SIZE_MIB` as a whole number from `1` through
`4000`, writes a private runtime configuration under the `/tmp` tmpfs, and changes
only `MaxScanSize`; the image's read-only base configuration is never edited.
Invalid or larger values stop startup with a clear error. Raising the value
increases the maximum parser and archive-expansion work ClamAV may perform, so
`2000` remains the default. The example's CPU, memory, temporary-space, timeout,
and concurrency limits keep a `4000` opt-in bounded. Resource exhaustion or any
other incomplete scan still fails closed.

Only a raw stream or `MaxFileSize` limit allows a window to split into smaller
overlapping windows, down to the configured `64 MiB` minimum. `MaxScanSize` within
a window, recursion limits, file-count limits, and unknown limits remain held;
splitting must not hide an unresolved expansion/inspection problem. A native
`MaxScanSize` result can still enter the verified-media route once, but every
attachment must pass a complete scan and every media window must finish without
an expansion limit. A limit at the minimum remains a policy failure, never a
clean or malware verdict. A single application-wide semaphore caps
all active ClamD streams at four, matching the sidecar's `MaxThreads 4`; `MaxQueue
8` remains burst capacity rather than eight active scanners.

ClamD stages every active `INSTREAM` request in its temporary directory before
scanning it. The examples therefore give the sidecar a `4 GiB` `/tmp` tmpfs for
four concurrent `512 MiB` windows, leaving working headroom above the `2 GiB`
raw-stream total. This is a maximum, not preallocated memory, and its actual use
counts toward the sidecar's `8 GiB` memory limit. If either the window size or
in-flight request cap is increased, increase both limits deliberately; an
undersized temporary filesystem can make ClamD close a socket before returning a
verdict.

This policy is intentionally recorded as
`media_windows_and_attachments`, not a native whole-file ClamAV verdict.
Native scans with the extra attachment pass record `clamd_native_with_attachments`;
other completed native scans record `clamd_native`. Historical method values
remain readable in existing checkpoints.
ClamD sees all raw bytes and `ffprobe` identifies the container and stream table;
it does not fully decode or prove the file is harmless. Whole-file hashes and
parsers cannot span independent ClamD windows. The default bounded
ceiling is `100 GiB`, so normal 5-50 GiB MKV/MP4 files can complete without being
skipped. Oversized archives, disk images, executables, unapproved audio-only
files, unknown formats, unsafe media attachments, files above the configured
ceiling, and limit/error responses that the validated-media fallback cannot
safely resolve remain held with no clean verdict. A filename extension never
selects the large-media path.

### Inspection warnings and remaining limits

The sidecar enables encrypted-content and broken-executable/image warnings.
Torrent Intake holds these as inspection-policy failures, **not infections**,
including when `TI_INFECTED_ACTION=delete`. Known malware and other threat
detections still follow the configured infection action. ClamAV's broken-media
warning covers certain image formats; it is not a full AVI/MKV decoder test.

Sending every byte does not mean every detection method ran. The sidecar keeps
bounded `MaxEmbeddedPE 40M` and `PCREMaxFileSize 100M` checks; some advanced checks
can be omitted on larger inputs. Complete small attachments restore their own
whole-object hash and parser context, not the whole movie's context. The 512 MiB
window default is unchanged pending representative full-signature/NAS throughput
measurements. An optional smaller-window profile is `TI_LARGE_MEDIA_CHUNK_MIB=32`
with `TI_LARGE_MEDIA_MIN_CHUNK_MIB=16`. It is below those advanced-check size caps,
but trades more requests and ~3.2% overlapping reads for smaller parser context;
it is not universally better for every signature.

Use occasional full scans with current definitions as well as changed-file scans.
FreshClam updates signatures only: update ClamAV/FFmpeg images and media players
separately. Do not treat a no-detection result as proof that opening media is safe.

### Upgrading to the attachment policy

Pause/stop Torrent Intake before recreating **both** the application and its ClamD
sidecar from this release. Do not run an old application against the newly enabled
warning options: old response parsers may misclassify them as infections. No mount
or database-schema change is needed; keep `/app/data`. The default label is now
`clamav-policy-v5-media-attachments`. A built-in implementation revision is also
included in the policy fingerprint, so retaining an explicit older environment
label cannot reuse weaker clean checkpoints. Active jobs re-evaluate their old
clean files once under this policy; already-promoted jobs are not recalled.
Newly completed checkpoints retain the usual restart recovery behavior.

For rollback, restore matching application and sidecar image versions together,
keeping the database and mounts. Older policy fingerprints may require another
scan of active jobs. No automatic deletion or new infection action is introduced.

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
Directory enumeration errors also block promotion; an unreadable or disappeared
subfolder must not silently remove files from the manifest or their checkpoints.

Parallel range workers never write SQLite. The owning torrent worker collects
their results and performs every checkpoint update serially. A restart during
one large file retries that file from its first window, while previously clean
files in the same torrent remain checkpointed.

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
| `/opt/docker/torrent-intake/data` | `/app/data` | rw local-SSD SQLite, settings, admin token and backup/restore work |
| `/mnt/bulk/docker/torrent-intake/staging` | `/staging-local` | rw local staging |
| `/mnt/media` | `/downloads` | rw NAS staging and media destinations |
| `/opt/docker/clamav-shared/events/torrent-intake` | `/events` | rw durable events |
| `/opt/docker/clamav-shared/quarantine/torrent-intake` | `/quarantine` | rw optional infection action |
| `/opt/docker/clamav-shared/defs` | sidecar `/var/lib/clamav` | read-only definitions |
| `/opt/docker/clamav-shared/sockets/torrent-intake` | both `/run/clamav` | rw private socket only |

The image and unset Compose fallbacks use UID/GID `10001:10001`. The supplied
`.env.example` selects `3000:3000` to match the current media deployment. For
`3000:3000`, prepare dedicated host directories as follows, without changing
ownership of the whole media tree:

```sh
sudo install -d -m 0750 -o 3000 -g 3000 \
  /opt/docker/torrent-intake/data \
  /opt/docker/clamav-shared/events/torrent-intake \
  /opt/docker/clamav-shared/quarantine/torrent-intake \
  /opt/docker/clamav-shared/sockets/torrent-intake
```

If you set `INTAKE_UID`/`INTAKE_GID` to another identity, create every dedicated
directory above with that same numeric owner before starting Compose. Existing
SQLite/settings files must also be owned by that identity; bind mounts hide
the image's built-in directory ownership. The socket
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

See [the complete configuration reference](CONFIGURATION.md) for every application
setting, deployment variable, required mount relationship, and compatibility-only
setting. The Settings & Help drawer shows the actual current values.

Copy `.env.example` to `.env` for Docker Compose, or enter those deployment values
in Portainer. The examples now keep application configuration in the persistent
`/app/data/settings.json` file. An explicit `TI_*` container environment value
still overrides that file and is persisted into it at startup. Merely adding a
variable to Portainer's variable list or Compose's host `.env` does **not** pass it
to an application unless the stack has a corresponding `environment:` mapping.

On a fresh installation the controller starts paused. Open Settings & Help,
unlock administration with the local token, configure qBittorrent, and restart
before resuming. Existing installations retain their environment configuration
and normal restart recovery. Important application settings below can also be
written without the `TI_` prefix in `settings.json`:

| Variable | Default/example | Purpose |
| --- | --- | --- |
| `TI_QBT_HOST` | `http://qbittorrent:8080` | reachable qB Web API |
| `TI_QBT_USERNAME`, `TI_QBT_PASSWORD` | required connection values | qB credentials; environment or private local settings |
| `TI_COMPLETION_EVENT_TOKEN` | optional unless using a completion hook | authenticate completion hook; polling works without it |
| `INTAKE_UID`, `INTAKE_GID` | `10001` | shared numeric identity for the app, sidecar, and writable host paths |
| `TI_CLAMD_SOCKET_HOST_DIR` | `/opt/docker/clamav-shared/sockets/torrent-intake` | private host socket directory |
| `CLAMD_MAX_SCAN_SIZE_MIB` | `2000` | sidecar cumulative parser/expanded-data limit; bounded to `1`-`4000`, with `4000` as an explicit higher-work opt-in |
| `TI_LOCAL_STAGING_ROOT` | `/staging-local` | exact local staging boundary |
| `TI_NAS_STAGING_ROOT` | `/downloads/torrent-intake/staging` | exact NAS staging boundary |
| `TI_FINAL_PARENT_PREFIX` | `/downloads` | primary allowed media root |
| `TI_FINAL_PARENT_PREFIXES` | empty | optional additional mounted media roots |
| `TI_SCANNER_MAX_FILE_MIB` | `2000` | native ClamD boundary; larger verified video and raw TrueHD content use the large-media route |
| `TI_SCANNER_POLICY_VERSION` | `clamav-policy-v5-media-attachments` | checkpoint policy identity; changing it deliberately reschedules prior file checkpoints |
| `TI_SCANNER_SCAN_TIMEOUT_SECONDS` | `1200` | total per-file client deadline |
| `TI_LARGE_MEDIA_ENABLED` | `true` | enable verified oversized-video and raw-TrueHD routing |
| `TI_LARGE_MEDIA_MAX_FILE_GIB` | `100` | hard ceiling for one oversized media file |
| `TI_LARGE_MEDIA_CHUNK_MIB` | `512` | initial independent ClamD window, below the native limit |
| `TI_LARGE_MEDIA_MIN_CHUNK_MIB` | `64` | smallest adaptive retry window after a ClamD limit response |
| `TI_LARGE_MEDIA_OVERLAP_KIB` | `1024` | repeated bytes between adjacent windows |
| `TI_LARGE_MEDIA_PROBE_TIMEOUT_SECONDS` | `120` | ffprobe container-validation deadline |
| `TI_LARGE_MEDIA_SCAN_TIMEOUT_SECONDS` | `172800` | total deadline for one large media file (two days) |
| `TI_PER_JOB_SCAN_WORKERS` | `4` in the examples; built-in fallback `1` | concurrent byte ranges within one large media file |
| `TI_CLAMD_MAX_INFLIGHT_REQUESTS` | `4` | application-wide active ClamD stream cap; keep at or below ClamD `MaxThreads` |
| `TI_MAX_CONCURRENT_SCANS` | `2` | normal scan slots |
| `TI_MAX_SCAN_SLOTS` | `4` | operator hard ceiling |
| `TI_LARGE_SCAN_GIB` | `2` | jobs at/above this size use the bounded large-scan slot |
| `TI_INFECTED_ACTION` | `hold` | `hold`, `quarantine`, or `delete` |

Local capacity control retains the existing behavior: a hard per-torrent local
limit, a free-space buffer, reservation of remaining bytes for all local qB
downloads, and either queueing or NAS overflow. The worker logs startup capacity
diagnostics but never queries public torrent services.

The UI/API has no login. The example binds it to
`127.0.0.1:${TI_UI_HOST_PORT:-8095}`; place an authenticated reverse proxy in
front before remote exposure.

To give native scans more parser/expanded-data headroom for raw files that remain
below the `2000 MiB` file and stream boundary, set this on the
`torrent-intake-clamd` service and recreate that sidecar:

```yaml
environment:
  CLAMD_MAX_SCAN_SIZE_MIB: "4000"
```

Do not raise `TI_SCANNER_MAX_FILE_MIB`, `MaxFileSize`, or `StreamMaxLength` above
`2000`; those represent a different ClamAV limitation. Existing failed scan-file
checkpoints remain retryable after the sidecar is recreated.

## Optional qBittorrent tags

Each single or bulk intake submission can include up to 20 ordinary
qBittorrent tags. The UI fetches qBittorrent's existing tags on load and manual
refresh, searches them locally, and renders at most 40 suggestions at a time.
Type a new name and use **Add Tag** to select it; qBittorrent creates a missing
tag only when the torrent is submitted. Leaving unfinished search text blocks
submission instead of silently creating a partial tag. Tags selected for a bulk
submission apply to every torrent in that batch.

qBittorrent trims tag names and uses commas as the tag-list delimiter. Torrent
Intake therefore rejects empty names and commas. It also applies its own
64-character limit, rejects control and invalid surrogate characters, and
prevents use of the managed tag or private `ti_job_*` namespace. Private current
and historical Intake tags are filtered server-side and never appear in the
suggestion list. qBittorrent automatically creates a missing valid tag when the
torrent is added, so no separate tag-creation request is needed.

Selected tags are stored with the job and reused if a failed job must recreate
its missing qBittorrent torrent. They are descriptive rather than ownership
credentials: manually removing one does not block scanning or promotion. The
managed tag and generated unique job tag remain the only required safety tags.

Current upstream behavior is documented by qBittorrent's
[WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29#add-torrent-tags)
and [`Tag::isValid`](https://github.com/qbittorrent/qBittorrent/blob/master/src/base/tag.cpp).

## Settings and help panel

The UI has a **Settings & Help** gear button. Its drawer shows every effective
`TI_*` application setting, the built-in default, a plain-language explanation,
and how the setting is managed. The list is searchable and includes a short
workflow guide plus a link back to the live scanner controls.

Ordinary application settings can now be edited after unlocking administration
and pausing the whole controller. Saved changes take effect on container restart,
not midway through an active job. Values overridden by the container environment
are locked in the editor; remove the override and recreate the container to use
the saved value instead. The existing scan-slot and scanner-maintenance controls
remain live and persist through SQLite. They are different from the whole-controller
pause used for backup/configuration, which also stops qBittorrent management actions.

Compose-only values—the image tag, published host port, numeric UID/GID,
host-side bind sources, and ClamD sidecar limits—cannot be discovered by the
application and therefore remain visible only in the deployed stack.

Passwords and completion tokens are displayed only as configured or not
configured. Credentials embedded in URLs and URL query strings are redacted.
Safety-critical values such as staging boundaries, the scanner policy, and
`TI_INFECTED_ACTION` remain visible with their current behavior explained, but
cannot be changed through the UI.

## Portable configuration and encrypted backups

### Where state lives

All application-owned durable state is on the existing `/app/data` mount:

| File | Contents |
| --- | --- |
| `torrent_intake.db` | Jobs, private magnets, tags, per-file checkpoints, scan queue and runtime scanner controls |
| `torrent_intake.db-wal`, `torrent_intake.db-shm` | SQLite's live journals; never copy just the main database while it is running |
| `settings.json` | All effective application settings, including connection secrets and explicit environment overrides |
| `admin-token` | Locally generated credential for configuration and backup/restore APIs; intentionally not exported/restored |
| `controller-paused.json` | Persistent whole-controller pause; survives container recreation |
| `deployment-notes.txt` | Your optional actual stack YAML, Portainer variable values, and host/network notes |
| `restart-required`, `.restore-pending/` | Pending changes that require a container restart |
| `before-restore-<id>/`, `last-restore.json` | Previous database/settings retained for offline rollback and a record of their location |

Keep `TI_DATA_HOST_DIR` on **local SSD/M.2 storage**, not NFS/SMB or a media mount.
This is particularly important for SQLite WAL, locking, and reliable atomic
replacement. The default `/opt/docker/torrent-intake/data` is only a pathname;
verify the host actually stores it on a local disk. This does not move your
torrents onto that disk: their staging/media mounts are separate.

Files containing secrets use mode `0600`; backup work directories use `0700`.
The live settings file must remain readable without a passphrase for unattended
startup, so it is **not encrypted at rest** by the application. Protect the host
data volume. Exported backups are encrypted. No new database, volume, container,
queue service, or Docker socket is required. `cryptography` is the one added
Python dependency, installed in the image at build time.

### Settings precedence and Docker-only settings

With the standard mounts, no application environment overrides are required for
a fresh setup or restore. The examples omit redundant `TI_DATA_DIR=/app/data`;
old stacks that still include it work unchanged.

The order is container environment → legacy in-container `.env` → `settings.json`
→ built-in defaults. The host Compose `.env` and an in-container `.env` are not
the same file. Legacy in-container dotenv loading remains for compatibility;
new deployments do not need it.

At startup all resolved application values are saved atomically to `settings.json`.
For example, an explicit `TI_LOCAL_MAX_GIB=200` wins over a file value of `100`
and saves `200` locally. Removing the environment mapping later preserves `200`.
Leave an override **absent**, not blank, when the local file should control it.
Unknown/malformed local setting names fail startup rather than silently reset
the configuration. A file may initially contain only selected values; defaults
are filled in and saved at the next successful startup. Example:

```json
{
  "schema_version": 1,
  "settings": {
    "qbt_host": "http://YOUR-WORKING-GLUETUN-ENDPOINT:8080",
    "qbt_username": "admin",
    "qbt_password": "YOUR-PASSWORD",
    "local_max_gib": 200,
    "per_job_scan_workers": 4,
    "infected_action": "hold"
  }
}
```

Safety-critical settings such as `infected_action`, staging boundaries, and
scanner limits remain read-only in the web editor. Edit their local file values
while Intake is stopped, or explicitly override them in the stack. Restoring a
trusted backup restores its saved policy, but automatic actions remain paused
until you acknowledge the receiving environment.

These settings **must stay in Docker/Portainer**, because they describe the
environment outside the application:

- image/version, published address and port, Docker networks and Gluetun routing;
- host bind paths, mount modes, numeric UID/GID, CPU/RAM/PID limits and tmpfs sizes;
- `TI_DATA_DIR` only if changing the bootstrap **container** path from `/app/data`;
- ClamD sidecar environment, including `CLAMD_MAX_SCAN_SIZE_MIB`, definition
  freshness and socket configuration. It is a separate process without the app
  data mount, so the app cannot read or change those values.

The new `.env.example` covers deployment variables. In particular,
`TI_DATA_HOST_DIR` selects the host SSD directory mounted at `/app/data`; it is
not a UI setting. Application paths can be stored locally, but Docker still has
to mount the corresponding content. qBittorrent and Intake must continue seeing
the same staging/media content at the same **container** paths. Intake and its
ClamD sidecar must share the same private socket directory and UID/GID. Keep your
working qBittorrent API URL; do not assume `qbittorrent:8080` works with Gluetun.

### Download and restore

The admin token is generated once when `admin-token` is absent. Reading it does
not create/change it. Keeping the data mount keeps the same token across updates
and restarts; a fresh installation has a new token. You only paste it when using
administrative controls, not for routine operation. It is intentionally separate
from the backup passphrase and not imported from a backup. On a new installation,
use its new token to unlock restore and the original backup passphrase to decrypt.

1. Use the new image with your **existing full environment first**. This saves
   the currently working values to `/app/data/settings.json`; do not strip your
   old stack first. Confirm the displayed settings, then the verbose application
   environment mappings may be removed. Keep any intentional overrides.
2. In Settings & Help, obtain the token with
   `docker exec torrent-intake cat /app/data/admin-token` and unlock administration.
   Use HTTPS or a trusted localhost tunnel. The general job UI/API still requires
   private-network protection; only the new admin endpoints require this token.
3. Save your **actual** stack and Portainer variable values in Deployment Notes.
   The app cannot discover host mounts, image digests, resource limits or the
   sidecar configuration. Notes are included in the encrypted backup, but never
   executed or used to provision Docker.
4. Select **Pause Intake Controller** and wait for **drained**. This prevents new
   management/API mutations and cooperatively interrupts scan workers. Existing
   management requests finish first. It does not pause qBittorrent downloads.
   A qBittorrent location move may also continue independently. Before copying
   torrent data or qBittorrent's state, wait for those operations to finish and
   pause/stop qBittorrent as appropriate for that separate backup.
5. Enter a strong unique passphrase (12+ characters) and download the `.tibak`
   backup. Keep the passphrase separately; the app cannot recover it. Resume the
   original controller only if it will remain the active installation.
6. On the receiving machine, prepare the local data directory and the necessary
   mounts/network/ClamD, using the same or a compatible newer application image.
   Keep the same container-visible media/staging paths and restore/copy their
   contents separately if needed. Stop the old controller before handover.
7. Start the receiving stack, unlock with **its own** local admin token, pause
   and drain if it is an existing installation, select the backup, enter its
   passphrase, and select **Stage Restore for Next Restart**. Confirm replacement.
8. Restart the container in Portainer. Before opening the app database, the image
   launcher validates and applies the staged restore. The previous installation
   is kept in `before-restore-<id>/`. Receiving environment overrides still win;
   the receiving database/data-directory locations and admin token are retained.
   Keep the image's standard entrypoint; a normal `command: uvicorn ...` override
   still runs through that launcher.
9. Verify mounts, qBittorrent contents, connection settings and the infection
   action. **Verify & Resume Controller** checks ClamD, qBittorrent connectivity
   and content directory access before enabling work. Individual jobs still go
   through the existing ownership/tag/path/file-identity checks.

Matching path strings alone are insufficient: both containers must see the
same actual torrent data. If files were copied to another host, verify the copy
and qBittorrent's torrent data before resuming. Update the completion callback's
target URL if Intake's hostname/address changed; polling remains the fallback.
While the whole controller is paused, the UI skips live qBittorrent enrichment
so stale connection details do not prevent you from opening the settings page.

This is restart recovery, not zero downtime or a snapshot of qBittorrent itself.
The currently interrupted file may be scanned again. Copied files with new
device/inode identities are deliberately rescanned rather than trusting old
checkpoints. Old backups may describe a qBittorrent state that has since changed;
such jobs can require review. Never run both old and restored controllers against
the same torrents. The local controller lock prevents duplicate processes using
one data directory, not separate copied directories on different computers.

The backup uses SQLite's [online backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup),
not a plain copy of a live WAL database. An uncompressed, allowlisted archive is
encrypted using AES-256-GCM and a passphrase-derived scrypt key. Authentication
must succeed before archive parsing; restore rejects unexpected paths, symlinks,
compressed members, malformed databases, and database triggers/views. Database
backup size is bounded to 512 MiB (torrent data size is unrelated). This does not
cap the working database; it bounds only portable backup/restore. Unlocking
administration shows the logical database size including committed WAL pages,
using lightweight metadata queries, not a full row count. Temporary work
uses the local data disk with bounded memory; allow several times the database
size in free space. Larger databases need an offline backup procedure. Browser
downloads also need enough memory for the encrypted file. Keep this backup limit
unless measured database growth justifies a larger, tested transfer/restore
design; many small files and retained history matter more than torrent byte size.

Snapshots exclude torrent content, qBittorrent's own configuration/fast-resume
database, shared definitions, notifier database/events and Docker images. Back
those up separately when moving the whole stack. Keep at least one tested backup
outside the Docker host, along with a matching image version and its passphrase.

For rollback, stop/pause the controller and restore a known-good encrypted backup
through the same process. The additional `before-restore-<id>` copy is retained
for offline recovery; never copy it over a running SQLite database or retain old
WAL/SHM journals with a different database. Before manual recovery, stop the app,
preserve the current data directory, and restore the previous database/settings
with no other process using them. Rollback directories are private but plaintext
and are not pruned automatically. Remove them only after validating recovery.

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
- retry, bulk retry/delete/clear, switch-waiting-jobs-to-NAS-staging, and scan
  priority/pause/resume endpoints used by the UI
- `GET /scanner/status`, `POST /scanner/slots`, `POST /scanner/maintenance`
- `GET /controller/status` (whole-controller pause, not only scanner maintenance)
- `/admin/status`, `/admin/pause`, `/admin/resume`, `/admin/settings`,
  `/admin/deployment-notes`, `/admin/backup`, `/admin/restore` (local admin token required)
- qB category/transfer and approved final-path suggestion endpoints
- server-filtered qB tag suggestions at `GET /qbt/tags`
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
idempotent columns/indexes to existing SQLite databases, including the per-file
`scan_method` audit field. Back up the database before the first deployment.
Per-file scan state and interrupted actions are recovered automatically. The
large-media policy-version change intentionally invalidates old clean
checkpoints so eligible files are evaluated under the new route once.

```sh
docker build -t torrent-intake:test .
docker build -f Dockerfile.clamd -t torrent-intake-clamd:test .
docker run --rm --mount type=bind,src="$PWD",dst=/workspace,readonly \
  --entrypoint python torrent-intake:test \
  -m unittest discover -s /workspace/tests -v
bash tests/run_media_integration.sh
docker run --rm --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m \
  --mount type=bind,src="$PWD/tests",dst=/tests,readonly \
  --env PYTHONPATH=/app --entrypoint python torrent-intake:test \
  /tests/integration_portability.py
```

Run the integration script as a non-root Docker user. It uses only disposable
test directories and isolated, read-only-root containers, never deployment
volumes. Real FFmpeg creates ASF/AVI-name and Kodi-attachment MKV fixtures; real
ClamD scans them over a private Unix socket with a tiny EICAR test signature
database. It checks clean files, embedded EICAR, an overlapping-window boundary,
native and large-media attachment scans, cover images, a hash-only attachment
signature missed by the opaque whole-container scan, encrypted-archive holding,
and malformed-media rejection. Window sizes are reduced for these small tests;
this is not a multi-gigabyte throughput test or a full signature-database test.

The portability integration test uses real HTTP and the image's startup launcher
inside one isolated test container. It checks first-run setup, local settings,
environment override persistence, encrypted export/import, token enforcement,
offline replacement, duplicate-controller locking, and paused recovery. It never
connects to your qBittorrent or uses production mounts. Unit tests also simulate
corrupt archives, invalid schemas, symlinks and interruption midway through restore.

For a repeatable synthetic advanced-check/window-size comparison, run
`TI_TEST_BENCHMARK_WINDOWS=1 bash tests/run_media_integration.sh`. It uses a sparse
128 MiB fixture, a tiny test database, disabled engine cache, and forced media-window
routing. It compares 512 and 32 MiB windows with a PCRE-only logical detection;
timings do not predict real movie, NAS, or production-signature throughput.

Both containers run non-root with read-only root filesystems, dropped
capabilities, no-new-privileges, bounded PIDs/CPU/memory, tmpfs scratch, health
checks, and rotated Docker logs. GitHub Actions validates on native amd64 and
arm64 runners (including helper resource limits and the synthetic window check)
before publishing both images for `linux/amd64` and `linux/arm64`. See the parent
`REPOSITORY_TRANSITION.md` for suite-wide migration and rollback steps.
