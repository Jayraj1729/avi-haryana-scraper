# ULB Haryana Property Scraper

A native Python desktop application for municipality/colony property scraping.
The original CLI demo is retained below. Scraping uses HTTP APIs, without Chromium.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

On Windows, activate the environment with `.venv\Scripts\activate`.

## Desktop Application

Enter a mobile number, click **Generate OTP**, and verify the OTP in the dialog.
Saved cookies are checked at startup. Expired sessions suspend every active job;
OTP verification automatically resumes jobs that were running. Manually paused
jobs remain paused. The session LED starts red and turns green only after this
check or OTP verification succeeds. Server/network failures turn it red and
temporarily suspend requests; authentication failures require another OTP.

Open the municipality and colony selectors, type to search, and check one or
more entries, then choose an output root and click **Add**. With multiple
municipalities checked, colonies are grouped under their municipality. Checks
survive filtering and catalog refreshes. Green-tinted entries already have jobs
in the queue. Selecting ALL checks all active entries; unchecking one clears
the explicit ALL selection.
The application refreshes options from the server; previously seen, removed
options remain visible but disabled. Selecting **ALL municipalities** also
selects **ALL colonies**. Either ALL option prompts before resetting affected
jobs. Their existing output folders are retained under `_archive`.

Municipality rows expand to show colony jobs. Both levels have start/resume,
pause, folder, and delete controls. Parent actions apply to their current
children, including jobs added after the parent was created. Removing jobs removes
their database checkpoints but retains exported files. Counts are unique within
each colony; municipality totals sum those counts, so overlapping colony boundaries
may include the same property in more than one colony.

Each job scans only the selected polygon, including its edges. Overlapping
viewports cover the complete polygon; dense responses subdivide into smaller
viewports and eventually paginate. Unrecognized or repeating responses retain
the failed tile for retry instead of claiming complete coverage. A colony with no usable boundary
requires attention; the app does not broaden its scope automatically.

**Map coverage** tracks completed viewport coverage, not the percentage of all
properties on the server. Each colony processes one property at a time: fetch
its detail page, download its photos, checkpoint completion, then move to the
next property or map tile. Details and photos are always enabled. The Properties
column shows discovered unique records; its tooltip also shows processed records.
Public-view restrictions retain the map record and mark the property unavailable.
The recorded portal map uses `IsDataVerified2023=true` for its "Not available for
public view" branch; these records do not generate detail requests or retries.
Removed photos are marked `missing` in the export.
Persistent detail/photo failures remain in backend processing states and errors;
their resolved rows are exported and other properties continue. Jobs with failed records or map
tiles finish as **Complete with warnings**. Start retries their failed work
without resetting successful records. Invalid boundaries eventually require
attention after bounded retries.

SQLite transactions persist properties and their map checkpoint together. Closing
the app waits for the current request (up to its 45-second timeout), saves changed
Excel files, and retains which jobs should resume. Every completed property is
saved immediately to SQLite. A forced process kill preserves committed work;
Excel catches up automatically after the next start. Details and completed
photo downloads are also checkpointed, so unfinished properties resume at their
remaining stage. The UI remains responsive during requests and Excel saves.

Default outputs:

```text
output/
  panchkula_60/
    sector_6_18949/
      properties.xlsx
      properties.csv
      photos/
```

Excel has clickable relative photo hyperlinks in `Photos` and `Photo 1`,
`Photo 2`, etc. Move the **whole colony folder** to keep links usable. Moving the
workbook and its photos independently cannot be tracked automatically by Excel.
CSV stores relative paths as text. Original image source URLs are retained in
`Photo Source URLs`.

The compact queue statistics show **Total Jobs**, **Active**, **Total properties**,
and **Scraping speed**. Properties sum discovery counts across colonies; a property
in overlapping colonies can count in each job. Speed measures newly resolved
property pipelines per minute over the last minute (or since opening the app),
including terminal failures and the time spent fetching details and photos.
Historical records do not inflate speed; paused runs show zero. Map coverage can
stay fixed while details and photos are processed. During that time the status
shows `Processing: completed/discovered`; coverage advances with map tiles.

### Property Columns

The first columns contain PID, municipality/colony, ownership, court-case flag,
category/type/subtype, plot/house number, plot area, address, and the PID-linked
mobile number. Exported headers do not use `Detail.` prefixes.
Non-empty page values take precedence over map values; empty page values fall
back to the map. Classification conflicts are recorded in `Data Notes`, not
silently overwritten with "Residential".

There is one `Address` and one `Plot / House Number`. Explicit plot numbers take
precedence. Only when absent, conservative address-prefix matching recognizes
identifiers such as `H -3`, `J -7`, `32`, `C16p`, and `H NO C-5`. Ambiguous
addresses leave the number empty. `Plot / House Number Source` identifies inferred
values. Missing values are exported as `-`.

Owners expand into `Owner 1: Name`, `Owner 2: Name`, etc., containing expressions
such as `SUNITA VERMA W/O RAJNEESH VERMA (1/2)`. Husband/wife relationships use
W/O or H/O; father/mother relationships use S/O or D/O when gender is recorded.
Guardian uses C/O. Missing shares/guardians use `NA`; ambiguous relationships
retain labeled gender, relation and guardian values instead of guessing.
The redundant `Owner Names` column is omitted. If detailed owner names are
absent, its map/page fallback is retained in `Owner 1: Name` without guessing
how many owners a combined name represents.

Construction rows retain floor, occupancy, area and useful additional fields.
`Floor N: Occupier Name` combines name and relationship without an ownership
share. The row index preserves separate occupiers on the same floor. Neither
owners nor floors have a fixed business count limit.

The requested tax/dues columns, portal IDs/counters, redundant relationship
fields, owner status/serial/GST/nodal fields, floor serial/rebate fields, and
other explicitly excluded fields are omitted from CSV and Excel. The backend
retains checkpoint data, processing states and errors for recovery and the UI.
Unfamiliar useful fields and photo hyperlinks remain. Navigation artifacts such
as `Step1` and `Step 2` are excluded.

Mobile columns retain their first occurrence in PID-linked, owner-index, then
floor-index order. Later duplicate numbers become `-`; distinct numbers in
the same cell remain. Formatting differences and an Indian +91 prefix are
normalized for comparison, without guessing full values from masked numbers.
`Mobile Numbers` retains the complete deduplicated union near the end of usable
data, followed by `Data Fetched At (IST)` and remaining provenance metadata
(`Data Notes`, photo sources and photo download status).

Fetch timestamps are saved when map/detail data is received, not when Excel is
rewritten. Older checkpoints without a recorded time show `-`; refreshing the
export does not claim they were fetched again. Exported times use IST (+05:30)
regardless of the computer's timezone. Export schema version 6 refreshes
presentation locally without refetching completed properties or resetting jobs.

PID-linked mobile aliases (with/without "the", different casing, and `Mobile No`)
are consumed by the canonical `Mobile Number Linked With PID` column instead of
creating duplicate headers. Checks and search text clear after a successful Add;
failed additions retain selections so they can be retried.

Use **Update data** to select an existing output root containing municipality
and colony folders. The app validates the files and asks for confirmation,
recreates the update queue even in an empty app, and fetches current data after
login. Previous rows stay available on failures; new properties are added and
missing properties are retained for review. Update exports include last-checked
and last-changed timestamps in IST, plus `Update Review`.

Unfinished updates can resume or start a new scan. The same pause/resume controls
apply, and ordinary Add can include new colonies alongside updates. Queue
deletion keeps portable checkpoints and saved files. Keep the hidden
`.ulb-dataset.json` and colony `.ulb-checkpoint.sqlite3` files when moving a
dataset. Details and conflict handling are documented in
[UPDATE_DATA_DESIGN.md](docs/UPDATE_DATA_DESIGN.md).

Available full mobile values are retained, including the existing parser's
PID-linked hidden input and owner-data script sources. Aadhaar, PAN, family IDs
and email fields remain excluded.
Existing version-one checkpoints are normalized without exposing their hidden
form values. On upgrade, old successful pages are queued for refresh because
some fields were never captured before. Their saved data stays available in
Excel until refreshed. Map coverage, photos, and manually paused state remain
intact; resume affected paused jobs with Start. Known restrictions are reclassified
without network requests. Genuine unknown responses save structural-only
diagnostics under `data/application/diagnostics/` without response values or tokens.
The recorded signed-in home redirect with the `Not Authorise to view the details.`
popup is also classified as unavailable, not retried as a parser error. Login-form
responses still pause all active scraping for OTP renewal.
The portal's separate `Session Expired` document is recognized as requiring
renewed authentication rather than retried as a malformed property page.

Snapshots stream from SQLite with bounded memory and split at Excel's row limit.
Excel refreshes automatically in small batches (about 25 completed records or
five seconds between updates). For large workbooks, the cadence adapts to the
time taken to write them, avoiding continuous full rewrites. Pause, completion,
and shutdown force a latest save. There is no export button or interval setting.
Settings control a parallel-request ceiling (1-100, default 2) and the shared
minimum request-start interval (default 650 ms). Effective concurrency starts at
at most four, decreases on congestion and gradually increases after healthy
work. A bounded colony batch progresses before more queued colonies are
admitted. Each colony's property pipeline remains sequential.

Isolated server errors retry the affected work. Shared outages use increasing
cooldowns, respect `Retry-After`, and recover through one work probe. A successful
login-page check alone cannot mark scraping as recovered. Worker login responses
are confirmed against the protected session page; stale property tokens can be
refreshed without expiring a valid login or resetting progress. Confirmed expiry
still needs manual OTP renewal. Active shows actual running colony work units
and the effective limit, with request/pacing details in its tooltip.

There is no confirmed published portal rate limit. See
[PERFORMANCE_REVIEW.md](docs/PERFORMANCE_REVIEW.md) for measured throughput,
the 12–13 lakh runtime estimates, operating recommendations and bounded
diagnostic commands. Raising concurrency alone does not speed up a single
colony or bypass the shared request-start budget.
If an Excel file is open and cannot be replaced, the old workbook and checkpoint
remain intact. Close Excel; the app retries saving automatically.
Request logs now report stage duration, throttle wait and outcome. Frequently
read throttle preferences are cached briefly to avoid opening SQLite connections
in every worker wait loop; settings changes update the cache immediately.

To inspect the normalized schema without changing existing checkpoints or outputs:

```bash
python -m tools.preview_exports
```

This uses a read-only SQLite backup, runs the upgrade on that temporary copy, and
creates workbooks plus copied photos under `output/normalized-preview/`.
It makes no portal requests and requires a new or empty preview output folder.
Use `--output PATH` for another preview location. Rows queued for parser refresh
retain their saved values; missing historical fields still need an authenticated refresh.

Source runs retain app state in `data/application/`, separately from demo data:

- `state.sqlite3`: jobs, catalog history, preferences, properties, and checkpoints.
- `cookies.lwp`: authenticated HTTP cookies, never bundled in the executable.
- `logs/application.log`: rotating operational/error logs, up to ten 5 MB backups.

The packaged Windows executable uses `%LOCALAPPDATA%\ULBPropertyScraper` for
state and logs, and defaults exports to `Documents\ULB Property Records`.
Demo cookies are not automatically imported. Demo checkpoints and outputs are
left intact. Existing desktop jobs migrate without resetting their data.
Use `python app.py --data-dir PATH` for a separate app
state folder. Only one app instance can use a given state folder at once.

## Verification And Windows Build

```bash
python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python app.py --smoke-test
```

The tests exercise recorded detail HTML, image downloads over simulated HTTP,
parallel caps and pacing, OTP/session failure recovery, pause/restart, scoped
add/delete controls, migration, warnings/retry, Excel lock recovery, and portable
photo links. Live OTP, production throttling, and a real full-colony run still
require portal access.

`.github/workflows/build_windows.yml` runs tests, builds a standalone
`ULBPropertyScraper.exe`, verifies an offline launch, and uploads the executable
on pushes to `main`/`master`, version tags, or a manual Actions run. No repository
has been initialized or pushed by this implementation.

To build locally on Windows:

```powershell
pip install pyinstaller
pyinstaller --noconfirm build_windows.spec
```

HTTPS uses Windows' native certificate validation and a bundled public CA list.
Certificate and hostname checks remain enabled for login, scraping and photos.
If an older executable reports `CERTIFICATE_VERIFY_FAILED`, rebuild through
Actions after pulling the certificate-trust fix and download the new artifact.
If it persists, check the laptop's date/time and Windows trusted certificate
updates. Managed networks may require their administrator's trusted CA.

## CLI Login

```bash
python scraper.py login --phone YOUR_MOBILE_NUMBER
```

Enter the OTP in the terminal when prompted. The session cookies are saved at
`data/http_cookies.lwp`.

## Panchkula Sector 6 Demo

```bash
python scraper.py scrape-demo --reset
```

Resume an interrupted run by using the same command without `--reset`.
Add `--fetch-details` when you also want the linked property detail pages
behind each marker token:

```bash
python scraper.py scrape-demo --reset --fetch-details
```

By default the scraper keeps only markers whose coordinates are inside the
selected colony boundary polygon. Use `--filter-mode all` only for exploratory
map-neighborhood scraping.

For a quick partial run:

```bash
python scraper.py scrape-demo --reset --max-tiles 20 --fetch-details
```

Outputs:

- Checkpoints: `data/panchkula__sector_6/`
- Detail checkpoints: `data/panchkula__sector_6/details.jsonl`
- Logs: `logs/`
- CSV/XLSX: `output/panchkula__sector_6.csv` and `output/panchkula__sector_6.xlsx`

Export a paused CLI checkpoint without resuming scraping:

```bash
python scraper.py export --municipality PANCHKULA --colony "Sector 6"
```
