# iTermSysMon

A lightweight macOS system monitor that runs as an iTerm2 web panel.

## Features

- CPU, memory, GPU, network, ports, and file activity
- Process and application usage
- Configurable disk usage monitoring and scan interval for temporary, cache, and build folders
- Configurable process-network sampling interval
- CSV export

## Install

1. In iTerm2, enable **Settings > General > Magic > Python API**.
2. Copy `sysmon.py` to:
   `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/`
3. Restart iTerm2. The **System Monitor** panel will be available from the toolbelt.

The script creates `watched_folders.json` beside itself on first launch. Existing settings are preserved.

No separate Python installation or package setup is required.

## Watched folders

Edit `watched_folders.json` or add and remove folders in the panel. Set the scan interval to 1–60 minutes, hours, or days. The last scan time and sizes are saved, so restarting iTerm2 does not trigger an unnecessary scan. Default paths avoid nested duplicate scans. The monitor never deletes files; folders over 5 GB are highlighted.

## Network sampling

Set process-traffic sampling to 1–60 seconds or minutes in the Network panel. The default is 10 seconds.

File activity works without administrator access. Process names for file activity require `SYSMON_FS_USAGE=1` before launch and an optional `sudoers` rule for `fs_usage`.

## Performance changes

Connection detail rows share one system-wide snapshot for five seconds, including
failed attempts. Refreshes no longer overlap, identical in-flight requests are
shared, and hidden pages stop UI polling. File event queries stop when the disk
section is collapsed. Hardware memory constants are cached.

File activity defaults to FSEvents. Detailed process attribution requires launching
with `SYSMON_FS_USAGE=1` and the existing optional sudoers rule; it traces system-wide
filesystem calls and can be expensive during builds or large file transfers.

Folder scans still run at the configured interval, even with the panel closed.
They recursively traverse every watched folder; `nice` lowers CPU priority but does
not limit disk I/O. Keep broad cache-folder scans infrequent or remove busy roots.
The saved folder and network intervals are preserved during installation.

Startup acquires the HTTP port before starting collectors, so duplicate instances
fail before launching background work.


## Collection controls

Network starts collapsed on every page load. Expanding it starts per-process
`nettop` sampling; collapsing or pausing it stops that collector. Expanded process
rows request connection snapshots on demand. Interface totals remain available
through the lightweight interface sampler.

Disk Activity always starts paused. Press its Play button to start file event
collection, and Pause to stop the underlying collector. If detailed tracing was
opted into via `SYSMON_FS_USAGE=1`, these controls also start/stop `fs_usage`.

Hidden viewers release collection demand. Closing or losing a viewer releases it
within 30 seconds. Multiple viewers keep collection active while any one requests
it. Per-process network history has gaps while its collector is stopped.

## Validation

On macOS, run `python3 -B test_sysmon.py` and `node test_ui.cjs`.
Tests stub iTerm2 and collectors so they do not launch tracing commands.
