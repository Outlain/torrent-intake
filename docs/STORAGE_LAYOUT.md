# Stable TrueNAS storage paths for a Docker VM

This is a recommended deployment design, not a migration the application performs.
Intake never creates datasets, mounts NFS, edits `/etc/fstab`, or switches pools.
Use a maintenance window and backups when changing storage underneath active jobs.

## Keep physical storage separate from application paths

Create one dataset per **category**, not one dataset/share for every individual
film or episode. Example category datasets on the HDD pool:

```text
hdd/media/movies
hdd/media/tv
hdd/media/music
hdd/media/iso
hdd/media/software
hdd/media/misc
hdd/intake-staging
hdd/intake-copies                 optional extra copy destination
```

The pool names `hdd` and `ssd` are examples. For this straightforward design, use
genuinely separate storage pools backed by the intended HDDs and SSDs. Naming two
datasets `HDD` and `SSD` inside one pool does not select physical disks for their
contents; advanced storage-tiering features are not assumed here. When moving a
category to SSD, use an actual SSD-pool dataset, such as `ssd/media/movies`, while
keeping client-visible paths unchanged.

Create a separate NFS export for each category dataset you need. Do not rely on
exporting the parent to expose every child dataset, or share the whole pool.
TrueNAS documents dataset boundaries and recommends dataset-level shares. Use
the preset appropriate for your clients (Generic for straightforward Linux NFS;
review Multiprotocol when also sharing via SMB).
[TrueNAS NFS setup](https://www.truenas.com/docs/scale/25.10/scaletutorials/shares/addingnfsshares/)

Mount each share directly **inside the Linux VM running Docker**:

| TrueNAS export example | Stable Docker VM mount | Same path in qBittorrent and Intake |
| --- | --- | --- |
| `NAS:/mnt/hdd/media/movies` | `/mnt/nfs/movies` | `/downloads/Movies` |
| `NAS:/mnt/hdd/media/tv` | `/mnt/nfs/tv` | `/downloads/TV` |
| `NAS:/mnt/hdd/media/music` | `/mnt/nfs/music` | `/downloads/Music` |
| `NAS:/mnt/hdd/media/iso` | `/mnt/nfs/iso` | `/downloads/ISO` |
| `NAS:/mnt/hdd/media/software` | `/mnt/nfs/software` | `/downloads/Software` |
| `NAS:/mnt/hdd/media/misc` | `/mnt/nfs/misc` | `/downloads/Misc` |
| `NAS:/mnt/hdd/intake-staging` | `/mnt/nfs/intake-staging` | `/downloads/torrent-intake/staging` |
| Local SSD staging | `/data/docker/torrent-intake/staging` | `/staging-local` |
| `NAS:/mnt/hdd/intake-copies` | `/mnt/nfs/intake-copies` | Intake only: `/copy-target` |

For optional routed copies, create `movies`, `tv`, etc. inside the copy storage,
each with `.intake-copy-mount`. In Intake settings pair `/downloads/Movies` with
`/copy-target/movies`, `/downloads/TV` with `/copy-target/tv`, and so on. Only jobs
whose final parent matches an enabled source rule are copied. Relative payload
paths remain the same without a job-ID wrapper; the category is not continually
synchronized and unrelated existing library content is not backfilled. Private
copy records stay outside payloads in each copy root's `.intake-copy-state`.
You may instead mount independent copy exports at `/copy-target/movies`, etc.;
only Intake needs those binds. Do not point copy rules back at the original data
through an alias mount.

**Preserve the exact spelling/case already recorded by your jobs and qBittorrent.**
If the current library is `/downloads/movies` rather than `/downloads/Movies`,
keep the lowercase spelling. A host directory may have a different name because
the bind mount explicitly maps it to the fixed container path.

For a full Proxmox VM, direct guest NFS avoids adding a second host-to-guest
filesystem-sharing layer and keeps the hypervisor out of application permissions.
Give the VM network access to the NAS and manage its mounts there. Proxmox's
virtual disks/NICs and optional host-directory sharing are separate mechanisms;
merely mounting NFS on Proxmox does not mount it inside the guest. This
recommendation is for a full VM, not LXC bind-mount instructions.
[Proxmox VM documentation source](https://github.com/proxmox/pve-docs/blob/master/qm.adoc)

## Configure matching Docker mounts and final roots

Use the same category and staging binds on both qBittorrent and Intake. For example:

```yaml
# Repeat these under BOTH services; retain their existing private/config mounts.
volumes:
  - type: bind
    source: /mnt/nfs/movies
    target: /downloads/Movies
    bind:
      create_host_path: false
  - type: bind
    source: /mnt/nfs/tv
    target: /downloads/TV
    bind:
      create_host_path: false
  - type: bind
    source: /mnt/nfs/intake-staging
    target: /downloads/torrent-intake/staging
    bind:
      create_host_path: false
  - /data/docker/torrent-intake/staging:/staging-local:rw
```

Repeat for the other actual categories. Avoid layering these onto the old broad
`/mnt/nas-media:/downloads` mount once migration is complete. Bind mounts expose
host paths under independent container names; they neither merge different
filesystems nor eliminate their I/O/permission requirements.
[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

Configure Intake's allowed final roots to the **actual category mount points**,
not the otherwise empty `/downloads` parent. For example, merge these values into
the existing `settings` object in `settings.json` while Intake is stopped:

```json
{
  "final_parent_prefix": "/downloads/Movies",
  "final_parent_prefixes": "/downloads/TV,/downloads/Music,/downloads/ISO,/downloads/Software,/downloads/Misc",
  "nas_staging_root": "/downloads/torrent-intake/staging"
}
```

These mount-boundary settings remain deployment-managed, not ordinary UI edits.
Equivalent `TI_FINAL_PARENT_PREFIX` and `TI_FINAL_PARENT_PREFIXES` environment
overrides work too. Existing final paths must remain inside one of the configured
roots. Inspect old job paths before tightening the list. The parent `/downloads`
is not itself an NFS mount in this layout and may not be writable by UID 3000;
using it as the sole allowed root loses the intended per-mount checks.

Register named NAS staging locations in the UI only for actual temporary intake
directories, not the final library itself. Preserve the existing NAS staging
path for old jobs. If using markers, provision them on the corresponding export
and configure their exact container paths. Markers check presence, not export
authenticity; they cannot replace host mount verification.

## Permissions and startup

Keep `/app/data`, events, definitions, sockets and updater/notifier state on local
SSD/M.2 storage. The NAS holds bulk media and optionally NAS staging/copies.
Use UID/GID `3000:3000` consistently, with dataset ownership or ACLs granting the
needed access. Read-only consumers can mount only the categories they need, `ro`.
Do not fix access by recursively changing the entire pool to `777` or mapping all
clients to root. Restrict NFS to trusted client addresses/VLANs: ordinary `sec=sys`
uses numeric identities and is not cryptographic authentication.
[TrueNAS NFS access permissions](https://www.truenas.com/docs/scale/25.10/scaletutorials/shares/addingnfsshares/#adjusting-access-permissions)

Example `/etc/fstab` line **inside the Docker VM** (replace the export/IP):

```fstab
192.168.68.197:/mnt/hdd/media/movies /mnt/nfs/movies nfs rw,hard,_netdev,nofail 0 0
```

Create the mount point before mounting; repeat per export. `rw` is required for
qBittorrent/Intake writers, while the server must also permit writes. `hard` favors
integrity by retrying unanswered requests, but a NAS outage can block filesystem
calls; a script timeout is not a guarantee that blocked kernel I/O ends promptly.
Do not switch to `soft` simply to make an outage look responsive: Linux documents
possible data corruption with soft timeouts.
[Linux NFS mount options](https://man7.org/linux/man-pages/man5/nfs.5.html)

`nofail` lets the VM boot if an export is unavailable. It is **not** a safe-start
gate for Docker. Arrange boot ordering/preflight so affected media containers do
not start until the intended exports are mounted and verified, or leave them
stopped for manual start after verification. A systemd-managed stack may use
`RequiresMountsFor=` on its startup service; Portainer/automatic Docker restart
must also respect the host's chosen policy. Do not gate all local services on NAS
if you deliberately want local-only Intake work to remain available.
[systemd mount dependencies](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml)

An existing empty mount-point directory passes Docker's host-path existence check
even if the share failed to mount. Keep underlying unmounted directories unwritable
to the application, avoid creating markers there, and verify mounts explicitly:

```sh
findmnt -T /mnt/nfs/movies -o TARGET,SOURCE,FSTYPE,OPTIONS
docker exec torrent-intake stat -c '%A uid=%u gid=%g %n' /downloads/Movies
```

An NFS reboot with the same export differs from replacing that export with another
dataset. Never assume active Docker bind mounts will pick up a changed host mount;
default bind propagation is `rprivate`. Stop/recreate affected containers after
a deliberate storage switch.
[Docker bind propagation](https://docs.docker.com/engine/storage/bind-mounts/#configure-bind-propagation)

## Staging placement and performance tradeoff

One independent NAS staging dataset is easiest to isolate and maintain. Promotion
from that dataset to a category dataset crosses a filesystem boundary, so budget
for a real data transfer and temporary duplicate capacity. A fast rename/hardlink
cannot cross distinct filesystems; sharing the same pool or Docker parent path
does not change that.

If that copy cost matters, create a normal staging **directory** inside each
category dataset (not a child dataset), for example `movies/.intake-staging`.
Register `/downloads/Movies/.intake-staging` as a named staging location, keep it
away from consumers, and select it for movie jobs. Staging and final content can
then remain on one filesystem. Do not expose that same staging directory through
a second, separate bind mount and expect cross-bind renames to work. Automatic
overflow uses the single configured default; Intake does not infer category-based
routing. A central staging share remains the simpler initial choice.

10 Gb/s is approximately 1.25 GB/s of raw link capacity, not a guarantee of disk
throughput. A four-HDD RAIDZ2 pool can still bottleneck on random I/O, metadata,
concurrent downloading/scanning/copying, or pool fullness. Multiple datasets on the
same pool do not create more physical IOPS. Keep operation counts bounded and
measure the actual workload rather than choosing worker counts from NIC speed.
The built-in copy is another whole-payload read/write, so leave it disabled when
you do not need a second copy. It is not a durable snapshot-based backup system;
two copies on the same pool do not protect against loss of that pool or NAS.

## Moving only Movies from HDD to SSD

The stable paths let you move one category without changing qBittorrent save paths
or Intake jobs. It is a controlled cutover, **not live transparent storage tiering**:

1. Verify backups and SSD free capacity. Seed `ssd/media/movies` from
   `hdd/media/movies`, preferably with TrueNAS local ZFS snapshot replication.
2. Pause/drain Intake, stop active copy actions safely, pause qBittorrent work using
   Movies, and stop all other writers. Stop affected containers before unmounting.
3. Take and replicate the final snapshot/delta. Verify data, ACLs, UID/GID and
   required marker files. Keep the old dataset intact for rollback. Disable old
   replication schedules that would overwrite the now-active destination.
4. Export the SSD dataset with appropriate permissions. Change the source in the
   VM's Movies fstab line to `/mnt/ssd/media/movies` while keeping
   `/mnt/nfs/movies` unchanged. Unmount the old export and mount the new one in the
   maintenance window; `mount -a` alone does not replace an already mounted export.
5. Confirm `findmnt` shows the new source and `rw`, and verify access as UID 3000.
   Recreate the affected Docker containers so their binds point to the new mount.
6. Check qBittorrent's files and Intake's setup, then resume. Identity-sensitive
   scan checkpoints may need rescanning after changing filesystems; this is safe,
   but uninterrupted scan progress is not promised.

TrueNAS local replication can copy selected datasets between pools on the same
system. A replication destination may be read-only depending on its properties;
confirm it is writable before using it as the active library.
[TrueNAS local replication](https://www.truenas.com/docs/scale/25.10/scaletutorials/dataprotection/replication/localreplicationscale/)

Rollback uses the same stopped-container procedure to remount the retained HDD
export. If new files/changes were written to SSD after cutover, reconcile them
first; blindly reverting would lose those updates. Replacing an export may change
NFS file handles, so never cut over underneath active readers/writers and promise
they will not notice.
