# Torrent Intake MVP

A Dockerized intake controller for qBittorrent. It adds torrents to an approved
staging root, watches completion, pauses them, scans every regular file with
restart-safe checkpoints, and only then performs the configured clean or infected
action.

The intentionally broad `/mnt/media:/downloads` mount is preserved. Final-path
validation allows normal media libraries below `/downloads` while rejecting
traversal, symlink escapes, staging roots, and operational locations such as
`/downloads/docker`, `/app`, `/state`, `/var/lib/clamav`, and `/quarantine`.

## Adding magnets and .torrent files

In **New Intake**, paste magnets, select `.torrent` files, or combine both.
Desktop and mobile use the same controls. Choose the final destination, category,
staging preference and optional qBittorrent tags, then submit. Multiple items open
**Review Bulk Intake**, where you can apply the form settings to everyone or edit
each row's destination, category and staging choice. Custom tags apply to all rows.

- Up to **50 files and magnets combined** per submission. Selecting files again
  appends to the selection; **Clear selected files** starts that selection over.
- Each `.torrent` metadata file is limited to **32 MiB**. This is not a limit on
  the size of the downloaded torrent or its files; existing scan policies apply.
- File/mixed batches submit one item at a time, with progress and per-item errors.
  Successful items are removed from the review; failed items and their row settings
  remain. Check Recent Jobs before retrying an uncertain/network-failed submission:
  it may already have a durable error job that should use **Retry selected failed**.
- v1, v2 and hybrid metadata are supported. The application validates bounded
  bencoded metadata and rejects unsafe paths/symlink entries; qBittorrent performs
  the final torrent-format validation. It never extracts files or fetches metadata
  URLs itself. A `.torrent` file is a download recipe, not proof of safe content.

The original uploaded bytes (including private-tracker information and v2 metadata)
are submitted to qBittorrent and saved in the job's database record, not converted
to a replacement magnet. Duplicate checks use the info hash, including when the
same torrent was already added by magnet. Retrying a missing torrent after a restart
uses the original bytes. Staging admission, completion checks, pause-before-scan,
per-file checkpoints, malware handling and final promotion use the same workflow
as magnets. Uploading metadata does **not** bypass any scan.

No new containers, mounts or environment variables are needed. The original
metadata is retained with its job in `/app/data/torrent_intake.db` and included in
encrypted backups; ordinary job listings do not read or return the metadata BLOB.
Metadata can contain tracker passkeys, so protect the data volume as before.
Retained metadata counts toward the existing **512 MiB portable database backup
limit**. Downloaded payloads and qBittorrent's own state still need separate backups.

Before updating, take an encrypted backup, then pull/recreate `torrent-intake`
while keeping `/app/data`. Startup adds two nullable columns without rebuilding
jobs or resetting checkpoints. Existing magnet jobs and old backups remain valid.
For rollback to an image predating uploads, restore the pre-update backup while
Intake is paused; do not resume new file-upload jobs with that older image, which
does not know how to resubmit their original metadata.

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
and text suffix rules otherwise remain unchanged.

Font collection attachments ending in `.ttc` (case-insensitive) are supported,
including those labelled `application/octet-stream`. This follows the
[Matroska font attachment conventions](https://www.matroska.org/technical/attachments.html).
The suffix admits the attachment to bounded extraction and a complete ClamAV scan;
it does not prove that its bytes are a valid or harmless font. Unknown attachments
are not admitted merely because their MIME label claims to describe a font.

### Understanding media-policy errors

An unsupported format/stream/attachment is a **policy block**, not a malware
detection and not necessarily a size-limit failure. The shared media inspector
also checks attachments in smaller Matroska files, so its errors no longer label
every failure as "oversized media".

Media-validation rejections include the affected path, detected container, stream index/type,
codec and codec tag, and handler name where reported. Attachment rejections also
include the filename, extension, MIME label and declared size in bytes. Size/count
failures identify the applicable limit and reported/reserved amount; incomplete
extraction identifies expected and actual byte counts. Missing fields appear as
`unknown`. Metadata values are escaped and bounded, not dumped as raw payloads.
These details appear in the existing job error; no debug logging or new setting
is needed. Notifications retain their existing message-length bound.

For example, an MP4 rejection may report `stream_type="data"; codec="bin_data";
codec_tag="tmcd"`. Generic `data` tracks remain blocked in the media-fallback route,
including timecode tracks. The narrowly validated MP4 chapter-text exception is
described below; a handler label alone never grants an exception. Increasing size
limits will not fix an unsupported-type error. Share the diagnostic context
(redacting private path names) for review.

After updating the application image, use **Retry selected failed** for previously
blocked `.ttc` or MP4 chapter-text jobs. Stored error text is refreshed on the next
attempt, not when an image is pulled. No variables, mounts or database migrations are required.
This additive compatibility fix does not reset existing clean checkpoints; normal
file-identity, definition and policy checks still apply.

### MP4 chapter-text tracks

QuickTime-style MP4 chapter names can be reported as `data / bin_data / text`,
often with the misleading handler name `SubtitleHandler`. The media route admits
only this specific MOV/MP4 combination for additional validation; it does not
trust the handler name, accept arbitrary data streams or strip tracks from files.

A second, bounded FFprobe call disables chapter reinterpretation with
`-ignore_chapters 1`, so the original text samples are available rather than
silently discarded. **This does not skip the chapter scan.** The selected track
must resolve to `subtitle / mov_text / text`. Its sample count must be present,
between 1 and 4096, and match the complete packet list. Packet offsets, sizes,
corruption flags and text-length prefixes are checked before acceptance. Unknown,
overlapping, out-of-file, empty, truncated or excessive output holds the torrent.

The application reads those bounded sample ranges from the already-open source
descriptor, preserves **all raw sample bytes**, including length prefixes and
trailing boxes, and scans the concatenated track as one complete ClamAV object.
It does not scan just the exported chapter titles. The existing full-coverage
video-window scan still runs. Nothing is transcoded and no second movie copy is
created; the track payload alone uses a temporary file. Structural checks are
not proof that content is harmless, and antivirus detection is not a guarantee.

Chapter tracks share the existing per-attachment/total byte budgets and 64-object
count limit with fonts and cover art. Each chapter track reserves the full
per-object allowance. Helper processes retain their existing memory, output,
timeout and cancellation bounds; the extra packet list selects only the chapter
track and does not request decoded video frames.
Native scans that finish successfully are unchanged. Both oversized-media and
native-limit fallback scans use this additional chapter validation.

### Complete attachment scans

The large-media route extracts each reported attachment (including attached cover
images) and requires a complete native ClamD scan of that object before scanning
the video windows. Matroska content identified by its EBML header also gets this
attachment pass after a successful native scan, even below 2000 MiB. Small
audio-only Matroska files remain supported; cover art alone cannot qualify an
oversized file as video. This does not claim to extract every possible metadata
field or codec payload from every container.

Cover pictures marked by FFprobe as `attached_pic` are admitted by their detected
JPEG (`mjpeg`), PNG, GIF, or WebP codec, not by an optional filename or MIME tag.
This includes MP4 cover art with neither tag. A recognized codec is only permission
to extract and scan the image: every extracted picture must still pass its whole
ClamD scan. Unknown attached-picture codecs remain blocked, even if named `.jpg`.
Diagnostic names such as `attached-picture-43.jpg` are generated for nameless
pictures; they are never used as output paths.

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
Unknown-size cover images and chapter tracks reserve their per-attachment maximum
against the total budget. Empty, oversized, missing, or incomplete output holds the torrent.
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

### Editing a job's final destination

In **Recent Jobs**, use **Edit final location** beside an eligible job's final
path, on desktop or mobile. Change the container-visible destination (for
example `/downloads/TV/NewFolder`), then save. This is a per-job database change:
it takes effect immediately and needs no Portainer environment change or restart.

Jobs still in the download stage, including local-space/NAS/hash waits, remain
eligible until completion is recorded or a scan is queued. To change the final
location of an unfinished scan, select its row, choose **Pause After File**, and
wait for **Scan paused**. Then use **Edit final location**, save, and explicitly
choose **Resume Scan** when ready. A pause request alone is not enough: the worker
must have stopped, with no current file or scan verdict. Clean scans, promotion,
infected, failed and completed jobs cannot be redirected, nor can jobs with prior
promotion or post-promotion copy progress. If a job resumes or another browser
changes its destination while the editor is open, saving is rejected and the
draft stays open; close and reopen to review the current job before trying again.

This changes **only the eventual clean-promotion destination**. The current
download stays in its existing local/NAS staging location; its category, tags,
selected NAS fallback and scan policy are unchanged. Normal scanning and
promotion checks still apply. A paused scan remains paused with its existing
checkpoints; changing only the destination does not trigger a rescan. After a
clean scan and promotion, post-promotion copy rules use the **new final path**.
Paths must remain within the deployment's allowed
final roots and outside staging/operational directories. A new final subfolder
can be created by qBittorrent during promotion; editing does not create folders
or mount storage. Both applications still need matching container paths.

The action is unavailable while Intake is paused for maintenance/backup/restore,
like other normal job changes. No additional administrator unlock is required;
keep the Intake UI on a trusted network.

### Named NAS staging locations

**Settings & Help → Downloads/storage** can keep a named NAS staging list and one
default. Each record contains `id`, `label`, an absolute container `path`, and an
optional absolute `mount_marker` pointing to a pre-existing regular file. The
protected editor uses review, pause/save and restart before new settings apply.
These are temporary download/scan locations, not final media destinations.

Select a NAS location for NAS staging in New Intake or per row in bulk review.
A locally staged job remembers the current default for any later NAS fallback.
Each job pins its chosen NAS ID, path and marker; changing the global list/default does not relocate old
jobs. The selected NAS must be available: an unavailable path or configured marker
does not cause a silent switch to another NAS. Old jobs keep their saved paths.
Offline NAS checks are warnings at global resume, allowing unrelated local work.
This is not a mount manager: filesystem calls on a hard-mounted, unresponsive NFS
share can still block until the host/kernel's mount timeout or recovery occurs.

Leave `nas_staging_locations` empty to retain the single-root behavior, including
an existing `TI_NAS_STAGING_ROOT` override. The list, default and effective
environment overrides live in `settings.json` and encrypted backups. See the
[configuration example](CONFIGURATION.md#storage-and-torrent-placement).

Intake and qBittorrent must have matching mounts for every staging path. Before
using a marker, verify the host export and create the marker **on that export**, not
in the directory underneath an unmounted share. A sentinel helps catch missing
mounts but is not proof of NAS identity. The app never mounts exports or creates
mount markers. Docker mounts/permissions still need separate deployment recovery.

### Optional post-promotion copy

The built-in **Copy after successful promotion** action is **off by default**.
No script, `/hooks` mount, or application environment variable is required.
In **Settings & Help → Downloads and storage**, unlock advanced fields and add
copy rules pairing a **source final root** with a **copy destination root**.
Enable copying, review, pause/save, restart in Portainer, then verify and resume.
A trusted custom script remains a separate, mutually exclusive alternative.

For example:

| Source final root | Copy destination root | Result |
| --- | --- | --- |
| `/downloads/movies` | `/copy-target/movies` | A promoted `/downloads/movies/Action/Film/` copies to `/copy-target/movies/Action/Film/`. |
| `/downloads/music` | `/copy-target/music` | A promoted `/downloads/music/Artist/Album/` copies to `/copy-target/music/Artist/Album/`. |

Rules match the job's final parent path, including subdirectories. Matching is
case-sensitive and respects directory boundaries: `/downloads/movies-old` does
not match `/downloads/movies`. The most specific **enabled** matching source
wins, so each job gets at most one copy. Disabled rules are ignored, not exclusion
rules. **No match means no copy**; there is no automatic all-torrents fallback.
Removing all rules therefore stops new copies even if the master switch is on.
Rules select already mounted paths, not qBittorrent categories or remote servers.

Only the torrent's verified content is copied, including its own enclosing folder
when it has one, plus its path relative to the matching source root. A single-file
torrent stays a single file. There is **no `intake-job-*` payload wrapper** for
new routed copies. This preserves the payload layout, but is **not a full folder
mirror**: unrelated/old files, later edits, renames and deletions are not synced.

The sequence is:

1. Scan successfully and promote through qBittorrent as usual.
2. Confirm qBittorrent has finished moving into the final location.
3. Match a rule; if none matches, finish normally without copying.
4. Queue a durable copy request with its source root, destination root and relative
   content path saved for this job.
5. Wait five seconds by default, then recheck qBittorrent and final content.
6. Copy only this promoted file/folder without overwriting existing content.
7. Record copy success separately from the already successful promotion.

Changing rules does not redirect previously queued copies.
There is one copy worker, so copies do not start an unbounded number of `rsync`
processes. Existing completed jobs are not automatically copied when enabled.
The delay is not a substitute for checking that the move actually finished.

#### One-time deployment preparation

Mount existing, writable copy storage **only into Intake**, at `/copy-target`
or individual directories below it. qBittorrent and ClamD do not need these
mounts. For example, after
mounting your chosen export at `/mnt/nfs/intake-copies` in the Docker VM:

```yaml
# Add under torrent-intake.volumes; keep the existing mounts.
- type: bind
  source: /mnt/nfs/intake-copies
  target: /copy-target
  bind:
    create_host_path: false
```

The longer bind syntax refuses to create a missing host directory. It does not
prove that NFS is mounted. Verify the export and writable permissions first:

```sh
findmnt -T /mnt/nfs/intake-copies -o TARGET,SOURCE,FSTYPE,OPTIONS
```

After confirming the intended export, provision `movies` and/or `music`
destination directories inside it, writable by Intake's UID/GID. Create an empty
regular file named `.intake-copy-mount` **inside each rule's destination directory
on the export**. Do not put the
marker in the local directory underneath an unmounted share. If creating these
through the VM is denied by NFS permissions, provision them in TrueNAS with the
correct ACL instead of enabling root access or broad `777` permissions.

Add the above rule pairs in the UI, using container paths rather than VM host
paths. Each destination root must already exist, be at or below `/copy-target`,
and contain its `.intake-copy-mount` marker. Relative subdirectories beneath it
are created as needed. Intake does not mount NAS exports or create markers for
you. Keep the marker and destination stable while a copy is in progress. Omit
explicit `TI_POST_PROMOTION_COPY_*` environment overrides if you want these fields
editable in the UI. A target mounted on the VM as `/mnt/nfs/movie-copies` can be
bound to `/copy-target/movies`; it does not have to use that name on the host.

The resulting layout is:

```text
/copy-target/movies/
├── .intake-copy-mount
├── .intake-copy-state/         private action records, outside the payload
└── Action/
    └── Film/
        ├── Film.mkv
        └── subtitles.srt
```

The action uses image-provided `rsync`, not SSH or another container. It does not
delete the original, merge into an existing torrent target, overwrite existing
files, or copy your entire library. Existing category/relative parent directories
may be reused, but an occupied final torrent file/folder is a collision and fails
safely unless it is a verified completed copy of this same job. It requires real additional destination
space and reads/writes the whole payload: copying between two NAS shares through
the VM adds network and disk I/O. It is not a replacement for ZFS replication or
a snapshot-based backup system. See [the storage layout guide](docs/STORAGE_LAYOUT.md).

#### Status, interruption and recovery

Copy failure or timeout **does not repeat scanning, promotion, or qBittorrent
changes**. The main job remains done and the separate post-promotion status shows
the problem. The default whole-action timeout is `7200` seconds; adjust it in the
UI for expected size and speed. Restarting or pausing while an action runs marks
it interrupted and requires explicit review/retry, because some data may already
have been copied. Disabling an action leaves already queued work pending; it is
not cancellation. Queued/running work prevents **Clear completed** from forgetting
its job.

Directory targets are reserved exclusively and populated in place for NFS
compatibility. **They can be visible before copying finishes**; consumers must
wait for the job's successful copy status/private completion record. Single files
are copied privately and published using an exclusive hard link; hard-link
support is required. No `renameat2` support is required. Private action records
live under the destination root's `.intake-copy-state/<job-id>/`, never inside
the copied torrent; `complete.json` is the successful completion record.
Source identity and copied paths/sizes are checked after `rsync --fsync`.
The checks do not reread the whole payload for a cryptographic comparison and
cannot replace snapshots or protect against hostile concurrent writers. Keep the
source and target unchanged during copying. Symlinks and special files are rejected.

For a failed/interrupted copy, inspect the reported target and private action
record. Partial content is never silently resumed, overwritten or deleted.
Preserve/move aside only the affected partial content and action record after
review, then unlock administration and pause/drain Intake to retry the action.
Resume after queuing the retry. A completed, unchanged matching copy is a no-op
on retry. The ordinary failed-scan retry is not needed to repeat a copy.

Copy rules, pinned paths and action status are included in Intake's encrypted
backup. **Copied media, destination-side private action records, NAS mounts,
permissions and mount markers are not**; preserve/restore those separately.
With the built-in action there is no operator script file to back up.

#### Upgrading from the single copy destination

The old `post_promotion_copy_destination` value remains readable for compatibility
but does not apply to newly promoted jobs. Add explicit rules in the UI; without
them new promotions are skipped for copying. Already queued/retried legacy copies
keep their saved destination and old `intake-job-<id>` layout. Existing copies
are not moved, restructured or deleted by this update. New routed jobs save their
rule and relative target at promotion, so later rule edits cannot redirect them.

#### Advanced alternative: custom script

For behavior beyond copying, mount a trusted executable under read-only `/hooks`
and set `TI_POST_PROMOTION_SCRIPT`, or the equivalent local setting. Enable the
custom-script action instead of the built-in copy action. The UI deliberately
cannot edit arbitrary executable code. The script receives explicit arguments:

```text
/hooks/after-promotion-copy.py --source /downloads/Movies/Example.mkv --torrent-hash <hash> --torrent-name=<name> --job-id <id>
```

This is an argument list, not a shell command. Scripts run as Intake's non-root
identity but can access its writable mounts: they are trusted code, not a sandbox.
Keep host editing restricted to operators. Scripts must run in the foreground;
the runner stops normal process-group children, not deliberately escaped ones.
The [older copy script example](scripts/after-promotion-copy.py) remains available
for custom workflows, but is unnecessary for built-in copying. Its destination
constant must be edited if used. Script files/constants are not in Intake backups;
restore them and the read-only `/hooks` mount separately. No SSH client is installed.

### Upgrading existing installations

Pause/drain Intake and make an encrypted backup before deploying this update.
Keep the same `/app/data` mount. The additive migration preserves jobs and scan
checkpoints, pins existing staging choices, and leaves the optional hook disabled
unless explicitly enabled. No old completed jobs are automatically copied.
Mount any additional storage in both Intake and qBittorrent at matching container
paths, then configure the named locations through Settings & Help and restart.
For rollback, stop Intake and restore the **pre-update** data/configuration with
the older image; do not assume an older image understands the new settings keys.
Never run old and new Intake controllers against the same qBittorrent instance.

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
| `TI_NAS_STAGING_ROOT` | `/downloads/torrent-intake/staging` | legacy NAS staging boundary when the named list is empty |
| `TI_NAS_STAGING_LOCATIONS` | `[]` | named NAS staging records (`id`, `label`, `path`, optional `mount_marker`) |
| `TI_DEFAULT_NAS_STAGING_ID` | unset | default named location; per-job selections remain pinned |
| `TI_POST_PROMOTION_COPY_ENABLED` | `false` | built-in copy after verified clean promotion; editable in UI |
| `TI_POST_PROMOTION_COPY_RULES` | `[]` | UI-editable pairs of source final root, destination root and enabled switch; longest matching enabled rule wins; unmatched jobs are not copied |
| `TI_POST_PROMOTION_COPY_DESTINATION` | unset | legacy compatibility value only; configure rules for new jobs |
| `TI_POST_PROMOTION_ENABLED` | `false` | alternative trusted custom-script action; mutually exclusive with built-in copy |
| `TI_POST_PROMOTION_SCRIPT` | unset; example `/hooks/after-promotion-copy.py` | trusted deployment-only executable under read-only `/hooks` |
| `TI_POST_PROMOTION_DELAY_SECONDS` | `5` | delay before copy/script readiness checks |
| `TI_POST_PROMOTION_TIMEOUT_SECONDS` | `7200` | whole copy/script attempt deadline |
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

Open the **Settings & Help** gear button on desktop or mobile. Choose Connection,
Downloads and storage, Scanning, General, Backup and restore, or Deployment
information. Search works across sections. Each setting explains its purpose,
active value, default, and whether it is editable, advanced, deployment-only,
or controlled by a Portainer/environment override. The unused legacy application
name is hidden; existing settings files still accept it.

1. Unlock administration with the token from
   `docker exec torrent-intake cat /app/data/admin-token`.
2. Edit individual fields. Blank secret inputs keep the existing secret; use
   the explicit clear checkbox to remove an optional value. **Test connection**
   checks the edited qBittorrent credentials without saving or changing torrents.
3. In Scanning, unlock advanced fields if needed. **Review changes** shows a
   before/after summary without exposing secrets. Advanced changes need a second
   confirmation. Validation errors appear beside their fields. Discard abandons
   unsaved edits.
4. Choose **Pause and save**. Intake drains its workers before saving; qBittorrent
   downloads and previously requested moves can continue independently. Saved
   values are labeled **for next restart**, not mistaken for active values.
5. Restart **torrent-intake** in Portainer, reopen the page and unlock. Choose
   **Verify & resume**. Intake remains paused until its qBittorrent, scanner and
   storage checks pass. **Check setup** lists each result for troubleshooting.

There is no hot reload or Docker socket. Values overridden by container
environment are locked: remove the relevant mapping completely (not an empty
value) and redeploy to edit the saved local value. No new environment variables
are needed for this editor. Existing settings, checkpoints and backups keep
their formats. Keep deployment YAML separately; there is no paste-a-stack box.
Previously saved deployment notes are preserved in old and new backups.

The scan-slot and scanner-maintenance controls remain live and persist through
SQLite. They differ from the whole-controller pause used for settings/backups,
which also stops Intake's qBittorrent management actions.

Compose-only values—the image tag, published host port, numeric UID/GID,
host-side bind sources, and ClamD sidecar limits—cannot be discovered by the
application and therefore remain visible only in the deployed stack.

Passwords and completion tokens are displayed only as configured or not
configured. Credentials embedded in URLs and URL query strings are redacted.
Destructive actions (`TI_INFECTED_ACTION`), database/mount boundaries, ownership
tags, executable paths, TLS policy and scanner policy identifiers remain visible
but deployment-only. Advanced size/freshness/resource settings are editable with
the extra warning and server-side validation. This cannot change the sidecar's
separate limits or make an incomplete scan count as clean.

## Portable configuration and encrypted backups

### Where state lives

All application-owned durable state is on the existing `/app/data` mount:

| File | Contents |
| --- | --- |
| `torrent_intake.db` | Jobs, pinned NAS choices, hook status/attempts/output, private magnets, original uploaded .torrent metadata, tags, per-file checkpoints, scan queue and runtime scanner controls |
| `torrent_intake.db-wal`, `torrent_intake.db-shm` | SQLite's live journals; never copy just the main database while it is running |
| `settings.json` | All effective application settings, including connection secrets and explicit environment overrides |
| `admin-token` | Locally generated credential for configuration and backup/restore APIs; intentionally not exported/restored |
| `controller-paused.json` | Persistent whole-controller pause; survives container recreation |
| `deployment-notes.txt` | Legacy optional notes, preserved for backup compatibility; no longer edited in the UI |
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
    "qbt_host": "http://YOUR-QBITTORRENT-ENDPOINT:8080",
    "qbt_username": "admin",
    "qbt_password": "YOUR-PASSWORD",
    "local_max_gib": 200,
    "per_job_scan_workers": 4,
    "infected_action": "hold"
  }
}
```

Deployment-only settings such as `infected_action`, executable paths, and legacy
staging/database boundaries remain read-only in the web editor. Named NAS locations
use the protected Downloads/storage editor described above. Edit deployment-only values
while Intake is stopped, or explicitly override them in the stack. Scanner
limits use the advanced warning/unlock described above. Restoring a trusted
backup restores its saved policy, but automatic actions remain paused until you
acknowledge the receiving environment.

These settings **must stay in Docker/Portainer**, because they describe the
environment outside the application:

- image/version, published address and port, Docker networks and host VPN/VLAN routing;
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
3. Keep your **actual** stack and Portainer variable values in a separate protected
   deployment backup. The app cannot discover host mounts, image digests, resource
   limits or the sidecar configuration, and does not provision Docker.
4. Select **Pause Intake** and wait for **drained**. This prevents new
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
   action. **Verify & resume** checks ClamD, qBittorrent connectivity
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

- `POST /jobs` and `POST /jobs/bulk`: create magnet intake jobs (JSON)
- `POST /jobs/torrent`: create one file intake job (multipart `file` plus `settings`
  JSON containing `final_parent`, optional `final_category`, `staging_preference`
  and `custom_tags`). Bulk file clients submit this endpoint sequentially, as the
  UI does; do not encode file bytes in JSON or submit multiple files per request.
- `GET /jobs`, `GET /jobs/{id}`: inspect jobs
- `PATCH /jobs/{id}/final-destination`: change a downloading or safely paused
  unfinished scan job's planned final folder with `final_parent` and
  `expected_final_parent`. A stale destination or ineligible lifecycle state
  returns `409`; no files are moved and paused scans are not resumed by this
  endpoint. Existing scan checkpoints are preserved.
- retry, bulk retry/delete/clear, switch-waiting-jobs-to-NAS-staging, and scan
  priority/pause/resume endpoints used by the UI
- `GET /scanner/status`, `POST /scanner/slots`, `POST /scanner/maintenance`
- `GET /controller/status` (whole-controller pause, not only scanner maintenance)
- `/admin/status`, `/admin/pause`, `/admin/resume`, `/admin/checks`,
  `/admin/settings/review`, `/admin/settings`, `/admin/test-connection`,
  `/admin/backup`, `/admin/restore` (local admin token required)
- `/admin/deployment-notes` is retained only for compatibility with older clients
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
docker build -f tests/Dockerfile.qbt -t torrent-intake:qbt-test .
docker run --rm --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m \
  --mount type=bind,src="$PWD/tests",dst=/tests,readonly \
  --env PYTHONPATH=/app --entrypoint python torrent-intake:qbt-test \
  /tests/integration_upload.py
```

Run the integration script as a non-root Docker user. It uses only disposable
test directories and isolated, read-only-root containers, never deployment
volumes. Real FFmpeg creates ASF/AVI-name and Kodi-attachment MKV fixtures; real
ClamD scans them over a private Unix socket with a tiny EICAR test signature
database. It checks clean files, embedded EICAR, an overlapping-window boundary,
native and large-media attachment scans (including synthetic TTC-named objects
with generic MIME labels), diagnostic rejection of a real MP4 timecode track,
clean/infected MP4 chapter tracks, complete-track hash detection and malformed
chapter samples, cover images, a hash-only attachment
signature missed by the opaque whole-container scan, encrypted-archive holding,
and malformed-media rejection. Window sizes are reduced for these small tests;
this is not a multi-gigabyte throughput test or a full signature-database test.

The portability integration test uses real HTTP and the image's startup launcher
inside one isolated test container. It checks first-run setup, local settings,
environment override persistence, encrypted export/import, token enforcement,
offline replacement, duplicate-controller locking, and paused recovery. It never
connects to your qBittorrent or uses production mounts. Unit tests also simulate
corrupt archives, invalid schemas, symlinks and interruption midway through restore.

The upload integration test runs real qBittorrent beside the Intake HTTP server
in a test-only image with **network disabled**, temporary profiles and no mounted
download directories. It checks v1/v2/hybrid multipart submissions, preserved
private tracker metadata, matching staging/tags, hash resolution and file/magnet
duplicates. Neither the test qBittorrent package nor its profile is part of the
published application image. Unit tests cover original-byte retry after restart,
metadata size/depth/count limits, unsafe paths, malformed/interrupted uploads,
temporary-file cleanup, additive migration and encrypted backup inclusion.

The optional browser test needs development-only Puppeteer and Chrome (neither
is added to the app image). After building `torrent-intake:test`, run
`node tests/test_settings_ui.cjs` and `node tests/test_upload_ui.cjs` with Puppeteer
on Node's module path. The upload browser test stubs submission responses to test
mixed bulk review, partial failures, per-row settings, tags and sequential uploads
on desktop/mobile. Set
`CHROME_PATH` if using an existing Chrome executable. It creates and removes
its own disposable container/volume, publishes only to localhost, and checks
desktop/narrow-mobile layouts, field locks/errors, advanced confirmation,
connection-test isolation, save/restart/pending values and failed-resume guidance.
Set `TI_UI_TEST_IMAGE` to test another local build; `TI_UI_SCREENSHOT_DIR` optionally
captures desktop/mobile screenshots in an existing directory. This script must
not be pointed at a production server; it always creates its own test instance.

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
