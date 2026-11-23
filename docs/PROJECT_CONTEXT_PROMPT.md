# Project Context Prompt: ULB Haryana Property Scraper

Context snapshot: 20 November 2026.

Export schema 9 adds constant State=Haryana immediately after PID, renames Unit
to Plot Area Unit (while consuming legacy Unit fields), and splits every floor
area and its adjacent unit column without losing decimal precision. Mobile
validation also rejects ten repeated digits (0 through 9), including after
country-prefix removal. These rules apply to colony exports, retained baselines,
municipality master exports and missing-mobile target selection.

Municipality master export: each municipality row has Export Excel and a
persisted Last exported timestamp. It pauses that municipality and drains scoped
requests before a separate background executor builds a disk-backed, PID-unique
workbook. Other municipalities keep progressing. All saved sibling colonies,
including queue-deleted portable checkpoints or legacy CSV/XLSX folders, are
included. Live jobs use current SQLite rows; retained checkpoints are read-only.
The newest successful PID row wins and Colony retains all memberships. Photos
link to existing files relative to Master, with no download/copy. A timestamped
workbook replaces the previous managed municipality file safely; export metadata
is in Master/.municipality-ID.json and the municipalityExports app setting.
Export details records IST time, row/colony counts and a positive refresh note.
Large exports split sheets. Progress, OK/Open folder completion and error dialogs
run on the Qt thread. Jobs stay paused until manually resumed. Master and
Exported are ignored by dataset validation and revalidation. Implementation:
ulb_scraper/municipality_export.py; tests/test_municipality_export.py.

November 23 update: searchable selectors show checked/total active options,
excluding ALL and headings, with counts retained while filtering. The existing
output-folder note is red and bold. Export schema 8 separates Plot Area and Unit
into adjacent columns and validates every mobile/phone column plus Mobile
Numbers: retain ten digits, remove an optional Indian 91/+91 prefix, omit short,
masked and other invalid lengths, then deduplicate. Raw checkpoint phone data
is retained; missing-mobile updates classify older baselines through the same
validation. Schema upgrades refresh exports without refetching properties.

Windows HTTPS uses a shared explicit HTTPSHandler: native Windows certificate
validation through truststore plus certifi public roots bundled by PyInstaller.
Other platforms retain default system trust with the same supplementary roots.
Certificate and hostname verification stay enabled. All client clones and photo
downloads inherit this transport. Requirements pin certifi and truststore; the
Windows offline smoke test constructs the transport without sending an OTP.

November 20 update: imports fetch each municipality's colony catalog once and
reuse it for all imported colonies. Confirmation checks file fingerprints rather
than parsing CSV/XLSX snapshots again. Validation, import and deletion show
progress. Deletion pauses jobs on the worker, skips clean exports, avoids
unchanged checkpoint writes and retains portable datasets. Unchanged GUI rows
are not repainted.

Update data offers a missing-mobile checkbox. Jobs retain mode=update with
updateScope=missing_mobile, baseline mobileTarget flags and mobileReportRoot.
Map searches obtain current property links; only existing baseline PIDs without
usable Mobile Numbers are processed. Photos are not downloaded again. Newly
found phone fields update original baselines; other fields/photos and previous
values on failures are retained. CSV/XLSX reports contain only newly numbered
properties under Updated-mobileNos-{IST timestamp}, following the same folder
layout. These report roots are excluded from imports. Targets and report paths
survive restart, queue removal and moving the complete output root.

Export schema 7 adds Mr./Mrs./Ms. from recorded relationship/gender, omits absent
name components and shares, and uses R/O for a guardian without a relationship.
Older combined name strings are cleaned on export. A permanent GUI note asks
users to copy the full output folder before working with the data. Offline
regressions live in tests/test_mobile_updates.py; the scoped benchmark is in
output/dataset-operations-benchmark-2026-11-20.json.


November 3 implementation update: exports introduced schema version 6 and a compact
presentation. `compactExportRow()`/`compactColumns()` project normalized data
without discarding backend checkpoint fields. Combined owner names include
relationship and share; occupier names include relationship. Map-only owner
fallbacks go into `Owner 1: Name`; the aggregate `Owner Names` column is omitted.
The user-requested tax/dues, portal IDs/counters and redundant fields are hidden.
Mobile values are deduplicated across PID/owner/floor columns, while their full
union and `Data Fetched At (IST)` appear before remaining metadata. Existing
rows without recorded fetch times show `-`. Full mobile extraction is intentional;
do not reinstate a masked-only requirement. PID-linked mobile aliases are
consumed even when hidden full mobile data takes precedence. Export times use
IST (+05:30); stored timestamps remain absolute instants.

Municipality/colony selectors now have searchable checkboxes, grouped colonies
for multiple municipalities, and green queue indicators. Explicit ALL retains
its confirmed replacement/archive semantics; ordinary checked additions are
idempotent. Successful additions clear selections; failures preserve them.
Map coverage can remain fixed during detail/photo work, whose status now shows
`Processing: completed/discovered`. Queue metrics are Total Jobs, Active, Total properties, and resolved
property pipelines/minute. Concurrency is a 1-100 ceiling (default 2), starting
with at most four colony workers and a shared pacing gate. Bounded batches,
adaptive congestion backoff and single-work-probe recovery prevent queue floods
and false recovery from repeated login checks. Worker login responses require
protected-page confirmation; stale marker tokens refresh without resetting
coverage. No within-colony parallelism was added. See `docs/PERFORMANCE_REVIEW.md`
for evidence, actual limits of the measurements and runtime estimates.
Periodic stale-data refresh is implemented through **Update data**. Read
`docs/UPDATE_DATA_DESIGN.md` and `ulb_scraper/datasets.py` for folder validation,
legacy CSV/XLSX import, portable per-colony checkpoints, dataset identity,
update generations and previous-baseline retention. Update exports preserve old
rows on failures, retain missing properties for review, and distinguish last
checked from last changed in IST. Queue reconstruction works without the old
application database and after moving the full output root. Normal Add restores
portable jobs after queue deletion; legacy folders must use Update data.

You are taking over an existing Python project as a developer or AI coding
assistant. Read this document and the relevant source before suggesting or
making changes. This is a continuation of an implemented application, not a
request to design a new scraper from scratch.

The source code and the user's newest request take precedence over this
snapshot. Inspect current files and checkpoints rather than assuming the counts,
sessions, or test results below remain current. Do not start scraping, send OTPs,
reset jobs, publish files, or modify production checkpoints merely because you
were given this context. Wait for the concrete task at the end of this prompt.

## 1. Product And Goal

Build and maintain a reliable, resumable desktop application that collects
property records from Haryana's property-tax portal:

```text
https://property.ulbharyana.gov.in
```

The eventual deployment is a standalone Windows `.exe`. Development currently
also happens on macOS. The user expects long-running scraping sessions lasting
hours or days, potentially collecting hundreds of thousands of records.

The application is intended for a finite data-collection effort rather than a
large SaaS product. Keep developer setup and maintenance simple without
sacrificing correctness, usability, or recovery from interruptions.

The hierarchy is:

```text
Municipality M1
  Colony C1 -> independent scraping job M1C1
  Colony C2 -> independent scraping job M1C2
Municipality M2
  Colony C3 -> independent scraping job M2C3
```

Each municipality has its own colony catalog. Names are display values; portal
IDs identify selections. Never assume identical colony names in different
municipalities refer to the same colony.

Each job produces one property dataset, an Excel workbook, a CSV companion,
and downloaded property photographs. Details and photos are mandatory parts
of normal processing, not optional enrichment in the desktop application.

## 2. Non-Negotiable Product Decisions

- Keep the production application in Python with a native desktop interface.
- Use direct authenticated HTTP requests. No Chromium, Selenium, Playwright
  scraping, browser windows, map dragging, or marker clicking is required.
- OTP delivery and verification use portal endpoints; the user manually enters
  the OTP. Never automate access to the user's messages or bypass OTP.
- Resume from committed checkpoints after pause, login renewal, or restart.
- Persist each property pipeline stage promptly; do not rely on Excel as the
  checkpoint or wait until the entire colony finishes to save data.
- Within a colony, finish or terminally resolve a property's details and photos
  before moving to the next property. Colonies may run concurrently.
- Restrict production map collection to the selected colony polygon, including
  its boundary. Do not return to an unbounded frontier scan.
- Export readable, consolidated columns, not duplicate `Detail.xyz` fields.
- Keep unknown useful fields and variable owner/floor records rather than
  silently discarding them to fit a small static schema.
- Existing user data, manual pause decisions, cookies, folders, and checkpoints
  must not be erased or reset without explicit authorization.
- Keep operational metadata at the end of exports, after actual property data.

## 3. Historical Decisions That Explain The Design

An early CLI prototype explored both direct property search and the map API.
The map API provides property markers and detail-page tokens without rendering
the map. Zooming and panning in the browser were useful for discovering those
requests, but the scraper does not reproduce those interactions.

The original idea was an adaptive grid/frontier scan that expanded into nearby
nonempty map viewports. Testing showed the portal can return markers well
outside the selected colony. The frontier could consequently continue through
neighboring areas and collect thousands of unrelated properties.

The current desktop implementation instead uses overlapping viewports covering
a finite polygon, filters every returned marker by its coordinates, and
subdivides dense viewports. This bounded design is intentional. Earlier requests
to capture everything "in and near" a highlighted colony are superseded by the
later agreement to stay inside the polygon.

The user initially wanted Excel rewritten after every property. They explicitly
accepted automatic small-batch workbook updates after learning that `.xlsx`
cannot be appended efficiently. Immediate durability is provided by SQLite,
not by rewriting an increasingly large workbook for each row.

The old CLI remains for reference and troubleshooting. It is not the authoritative
product interface, and not all of its flags or export conventions match the app.

## 4. Technology And Repository Map

Runtime dependencies currently pinned in `requirements.txt`:

| Dependency | Purpose |
| --- | --- |
| PySide6-Essentials 6.10.2 | Native Qt desktop UI and signal bridge |
| beautifulsoup4 4.14.3 | Structured property-page HTML parsing |
| shapely 2.1.2 | WKT geometry, polygon coverage, point filtering |
| openpyxl 3.1.5 | Streaming Excel generation and relative hyperlinks |
| pycryptodome 3.23.0 | Portal-compatible request-field encryption |

HTTP transport, cookies, SQLite, logging, threads, futures, JSON, and CSV use
Python's standard library. Do not introduce a web frontend, database service,
Redis, task broker, or distributed infrastructure without a demonstrated need.

The Windows workflow targets Python 3.12. Local verification has also run on
Python 3.14. The existing local virtual environment is `.aviEnv/`; a fresh
environment may use `.venv/` instead.

| File Or Directory | Responsibility |
| --- | --- |
| `app.py` | Entry point, app home, rotating logs, single-instance lock, smoke mode |
| `ulb_scraper/gui.py` | Native UI, OTP dialog, job tree, settings, controls, event bridge |
| `ulb_scraper/engine.py` | Coordinator, colony scheduling, shared request gate, recovery, exports |
| `ulb_scraper/portal.py` | OTP/session client, catalogs through inherited HTTP helpers, details, photos |
| `ulb_scraper/geometry.py` | Boundary validation, overlapping seed viewports, subdivision, filtering |
| `ulb_scraper/store.py` | SQLite schema, transactions, migrations, settings, catalog, job checkpoints |
| `ulb_scraper/property_data.py` | Dynamic HTML parsing, normalization, privacy filters, column ordering |
| `ulb_scraper/exporter.py` | Streaming CSV/XLSX snapshots and relative photo hyperlinks |
| `scraper.py` | Older CLI plus HTTP/session, protocol, and utility code still reused by the app |
| `tests/` | Offline unit, simulated-pipeline, GUI, migration, and export tests |
| `tools/preview_exports.py` | Safe offline export previews using a temporary database backup |
| `tools/preview_gui.py` | UI preview helper; read its arguments before running |
| `tools/diagnose_portal.py` | Older live diagnostic helper; review before use |
| `refrence/` | User-supplied HAR, HTML, PDF, and historical CSV references |
| `demo-files/`, `demo-files-2/` | User-created output datasets; not fixture scratch space |
| `output/` | Default source-run export root and isolated preview outputs |
| `build_windows.spec` | PyInstaller standalone Windows application specification |
| `.github/workflows/build_windows.yml` | Windows test/build/smoke/artifact workflow |
| `README.md` | Setup, operation, limitations, and legacy CLI commands |

The misspelling `refrence/` is intentional because it is the existing folder.
Do not casually rename it and break references or tests.

## 5. Architecture And Thread Ownership

```text
Qt main thread
  MainWindow + OTP dialog + Bridge signals
       |
       | queued commands / progress and result events
       v
Engine coordinator thread
  command queue, session lifecycle, bounded colony admission
       |
       +--> colony ThreadPoolExecutor, maximum 100 workers
       |      one in-flight work unit per colony job
       |      one independent HTTP client/cookie jar per worker and session epoch
       |             |
       |             v
       |      shared concurrency + request-start pacing gate
       |             |
       |             v
       |      authenticated portal HTTP endpoints
       |
       +--> one background Excel writer
       |
       +--> Store: SQLite WAL, independent short-lived connections
```

Qt widgets must only be touched on the Qt thread. Network activity and workbook
generation must not block the interface.

The coordinator keeps a bounded batch of colonies progressing and submits
bounded work units. Queued jobs take slots as active colonies complete, pause
or yield for backoff. A work unit prepares a boundary, handles one map request, or processes
one property's remaining detail/photo stages. A colony cannot have two workers
advancing its same checkpoint concurrently.

Worker clients clone authenticated cookies rather than sharing a mutable opener
and cookie jar across threads. A session epoch changes on login/session changes.
Late responses from an old epoch must not expire or contaminate a freshly
verified session.

## 6. Portal Authentication And Endpoint Contract

Relevant existing endpoints are below. Treat HARs as evidence of the recorded
protocol, not a guarantee that the portal will never change.

| Method | Path | Use |
| --- | --- | --- |
| GET | `/` | Login form and anti-forgery token |
| POST | `/Account/IsUserExist` | Generate login OTP |
| POST | `/Account/CheckOTP` | Verify the manually entered OTP |
| GET | `/SearchProperty/MyProperties` | Check authenticated access |
| GET | `/Widget/GetULBListPropertyWise` | Municipality catalog |
| GET | `/Widget/GetULBColonySurvey?fkULBId=...` | Municipality-specific colony catalog |
| GET | `/Widget/GetColonyBoundry?fkColonySurveyId=...` | Colony WKT boundary |
| POST | `/SearchProperty/getSearchPropertiesData` | Map viewport property records |
| GET | `/RegisterProperty/NewApplication?token=...` | Individual property details |

`generateOtp()` retrieves the anti-forgery token and sends portal-specific
fields. `verifyOtp()` follows the same-host redirect, checks authenticated
access, and persists verified cookies. Reuse the existing encryption/protocol
helpers; do not invent a different wire format or hardcode personal credentials.

Session behavior:

- The UI indicator is not green merely because a cookie file exists.
- Startup checks saved cookies against an authenticated endpoint.
- Green means access was verified; red means requests cannot currently proceed.
- Authentication expiry pauses all desired jobs and requires manual OTP renewal.
- Temporary transport/server problems also block requests, but may recover after
  backoff and a successful session check without requiring a fresh OTP.
- Login renewal resumes jobs whose `desired` state still requests running.
- Manually paused jobs remain paused.
- Recognize login redirects/forms and the standalone `Session Expired` document.
- A property-specific public-view denial is not a global session expiry.

An observed live spot-check returned `Session Expired` for a detail request.
Recognition was fixed, but the precise relationship between a stale detail token
and a still-accessible authenticated home page was not fully investigated. Do
not promise that renewing OTP automatically refreshes every saved property token.

## 7. Municipality And Colony Selection

Catalog options are fetched from the website and retained locally. Previously
seen entries that disappear from a successful current response become inactive
and are shown disabled/grey. Malformed responses must not retire the catalog.

Selection behavior:

- Search filters catalog entries without changing checked selections.
- Multiple checked municipalities show their colonies in labeled groups; colony
  selections retain municipality IDs even when names are identical.
- Green-tinted municipality entries have at least one queued colony; colony
  tinting identifies the exact municipality/colony pair already queued.
- One municipality plus one colony adds that exact job.
- One municipality plus ALL colonies expands its current active colony list.
- ALL municipalities implies ALL colonies; the colony selection is disabled.
- Ordinary duplicate additions are idempotent, not implicit resets.
- ALL selections require a confirmation explaining their fresh-run semantics.
- A confirmed municipality-level ALL replaces that municipality's jobs only.
- A confirmed global ALL replaces all jobs.
- Replacement pauses and drains affected workers and the writer before mutation.
- Existing affected output folders are moved under `_archive` rather than lost.
- Validate/fetch the selection before destructive replacement.

Municipality parent actions resolve the current child jobs at action time, so
newly added colonies are included. Ordinary Delete removes selected checkpoints
and queue entries, but retains exported files and downloaded photos.

## 8. Map Coverage Algorithm And Stop Conditions

`readBoundary()` parses WGS84 WKT, repairs invalid geometry when possible, and
requires a usable Polygon or MultiPolygon with sane longitude/latitude bounds.
Interior holes and boundary edges are respected.

`seedBounds()` generates overlapping rectangles intersecting the polygon. Current
viewport dimensions are inherited from the CLI constants, approximately
0.0009401 degrees longitude by 0.0006688 degrees latitude; grid spacing is 75%
of each dimension. These are angular viewports, not a universal meter distance.

Each map request includes the municipality's portal-encoded ID, colony ID, WKT
rectangle, `pageNo`, `pageSize=500`, and `searchType=3`, plus the existing empty
search filters expected by the portal.

The server may return out-of-colony markers despite those request fields.
Always filter coordinates with `geometry.covers(Point(lon, lat))` before insertion.
Reject missing, nonnumeric, or nonfinite coordinates rather than silently
expanding geographic scope.

Dense-result handling:

1. Detect a potentially capped response from row count or `TotalRecord`.
2. Subdivide into overlapping child rectangles intersecting the selected polygon.
3. Subdivide to the current maximum depth of 8; then paginate when needed.
4. Fingerprint page identities to detect the same page being returned repeatedly.
5. Retain failed work rather than declaring a repeating response complete.
6. Stop and require review if pagination exceeds its safety bound of 10,000 pages.

The grid generator also rejects boundaries that would create over two million
candidate viewports. These are safeguards, not proof of completeness at any
arbitrary geographic scale.

A tile's accepted records and its page/split/completion checkpoint are saved in
one transaction. Repeated markers from overlapping viewports deduplicate through
the job/property primary key.

The scan stops when finite in-scope tiles, pages, and property pipelines are
resolved. Failed tiles remain explicit warnings rather than empty success.

Map coverage is a weighted measure of completed viewport work, not the percentage
of properties on the server. Overlap means it is not an exact union-area metric
either. Discovery counts and completed-property counts are separate quantities.
100% map coverage alone does not prove all records have finished processing.

## 9. Property Pipeline And Availability

For a discovered property, the normal sequence is:

```text
map record saved
  -> fetch/parse details, or classify public-view unavailability
  -> checkpoint details and discover additional photo URLs
  -> download/checkpoint every pending photo
  -> mark property complete or terminally failed
  -> expose the resolved row to automatic exports
  -> advance to the next property
```

Stages are checkpointed independently. A crash after details or some photos
does not require starting the property again from its first stage.

The internal property identity prefers `pkPropertyId`, then available PID fields.
The visible `PID` and internal numeric record ID are different things. The same
property may appear in two overlapping colony polygons; deduplication is within
each job, not a statewide ownership assignment or cross-colony deduplication.

### Public-View Restrictions

The recorded map JavaScript uses this counterintuitive rule:

```text
IsDataVerified2023 = true -> Not available for public view
IsDataVerified2023 = false -> detail link may be exposed
```

Do not reverse this interpretation because the field name sounds like a positive
verification flag. The flag, not the presence of a token, controls the recorded
public-view branch. Known restricted records skip detail HTTP requests and do
not consume a request-gate slot for that local classification.

A recorded restricted detail request also shows:

```text
302 /RegisterProperty/NewApplication?token=...
  -> 200 /Home/Index, still signed in
  -> emitted showNotification error: Not Authorise to view the details.
```

The parser recognizes that specific emitted notice in a signed-in shell, not
any arbitrary JavaScript string or every redirect to Home. Visible public-view
restriction messages are also recognized. Generic error pages remain errors.

Unavailable properties retain their map data and `-` for absent fields. They
are not retry failures by themselves. Any available property photos still go
through the normal photo pipeline.

## 10. Dynamic Parsing And Normalization

Use `parsePropertyPage()` and `normalizeProperty()` as the central mechanisms.
`scraper.py` retains compatibility wrappers, and saved payloads still have an
internal `{'Detail': ...}` envelope. That internal format is not an Excel naming
convention and must not be exposed as `Detail.*` headers.

The parser reads labeled display fields and actual HTML tables with BeautifulSoup.
It expands `rowspan` and `colspan` before matching values to headers. It detects
owner and construction tables by normalized headers and preserves additional
tables and unfamiliar useful fields.

There is no fixed business limit such as 10 owners, 12 owners, or 3 floors.
Defensive table-expansion limits still apply to malformed or excessive HTML.
Do not describe the parser as unlimited in memory or in Excel column capacity.

Normalization rules:

- A nonempty detail-page value takes precedence over the corresponding map value.
- If the page is blank or a placeholder, fall back to the map value.
- Keep one Address, one Plot / House Number, and one category/type/subtype set.
- Record conflicting page/map classification values in `Data Notes`.
- Never default all properties to Residential just because samples use it.
- Preserve unfamiliar structured map metadata without exposing raw JSON strings
  as unreadable single cells when it can be flattened meaningfully.
- Preserve blank table fields so a known column is not lost merely because a
  particular property has no value.
- Join appropriate multiple contact values with commas and remove duplicates.
- Missing exported values are `-`, not fabricated data.

Plot-number fallback is conservative and only runs when no explicit plot/house
field is available. Examples recognized from an address prefix include `H -3`,
`J -7`, `32`, `C16p`, `H NO C-5`, and `G -8`. Do not blindly use everything
before the first comma. Reject ambiguous locality prefixes and leave them blank.

Known regression: Gurukul PID `1FT2GK62` has a usable map address but no page
address/plot value. Its normalized address is retained and its inferred plot
number is `G-8`.

### Owner And Construction Columns

Examples of repeated columns:

```text
Owner 1: Name
Owner 1: Mobile
Owner 2: Name
...
Floor 1: Floor
Floor 1: Occupancy Type
Floor 1: Covered Area
Floor 1: Occupier Name
Floor 1: Occupier Mobile
Floor 2: Floor
...
```

The numeric prefix is a record index. Two construction rows for the same floor
must remain separate rather than combining unrelated occupiers into one record.
New rows/fields expand the export schema; absent fields in other properties
become `-`. Sort repeated columns naturally: Owner 2 before Owner 10.

The backend still preserves the source gender, guardian, relationship and share.
Exported owner names combine them, for example `NAME W/O SPOUSE (1/2)`.
Occupier names combine relationship fields without a share. Missing information
uses explicit NA values. Unknown gender does not imply S/O or D/O. An aggregate
map owner name becomes Owner 1 only when individual detailed names are absent.

Internal authentication status must contain the displayed status, such as `Aadhar` or
`Pending`, not a personal identifier or checkbox instructions. In particular,
remove the instruction:

```text
Certify that Family id (Parivar Pehchan Patra) and Aadhar Number Not Available.
```

Hidden status text and form-control labels must not contaminate the status.
If only an instruction was captured in an existing checkpoint, retain an empty status rather
than inventing Pending, Aadhar, or Not Available. Do not manufacture Aadhaar
numbers from the fact that a status label says Aadhar.

## 11. Export Schema And File Semantics

Important property fields come first:

```text
PID, Municipality, Colony, Old PID,
Old Owner / Occupier Name, Ownership Type,
Have Any Court Case, Stay / Legal Proceeding,
Property Category, Property Type, Property Subtype,
Plot / House Number, Plot Area, Address,
Mobile Number Linked With PID
```

Owner/construction columns follow, then other property data and local photo
links. Lower-signal empty/zero extra property fields follow useful extras.
Do not discard real zero values merely because their columns are lower priority.

`Mobile Numbers` and `Data Fetched At (IST)` end usable data, followed by remaining
metadata: Data Notes, Photo Source URLs and Photo Download Status. Duplicate
contacts are removed from later PID/owner/floor cells in numeric index order;
the union retains all distinct numbers. IST timestamps come from map/detail
receipt, never export time; historical unknown times remain `-`.

Schema 4 omits the requested dues/tax fields, portal IDs/counters, redundant
person fields, owner/floor serials, owner authentication/GST/nodal fields, floor
rebates, Authorized Status, Property Availability, Processing Status/Error,
Availability Note, Plot / House Number Source, Locations Data Json, Possession
File, membership/property card and registered society fields. The full list is
in `compactExportRow()`; indexed rules apply to every owner and floor, not just
the first three. These fields remain in backend checkpoints where present.

Omit wizard/navigation artifacts such as `Step1`, `Step2`, `Step 1`, and `Step 2`.
Do not reintroduce `(Masked)` suffixes on contact headers or `Detail.` prefixes.

Each job's output structure is:

```text
<chosen output root>/
  <municipality_slug>_<municipality_id>/
    <colony_slug>_<colony_id>/
      properties.xlsx
      properties.csv
      photos/
        <property_identity>_<image_url_hash>.<extension>
```

Relative hyperlinks appear in `Photos`, `Photo 1`, `Photo 2`, etc. Move the whole
colony folder to preserve links. Moving Excel and photos independently cannot
automatically preserve those links. These are workbook hyperlinks, not OS-level
symbolic links. The CSV contains relative paths as plain text.

The downloader combines map/page photo sources, deduplicates them, excludes
known site assets, checks image content type and size, and uses a stable filename.
Downloads use `.part` files and replacement after success. A 404/410 resolves
to a missing photo; an authentication page is not a valid image.

`exportJob()` uses two streaming passes over a consistent SQLite read snapshot:
first discover/order the schema, then write CSV and a write-only workbook. It
escapes formula-like workbook text as text and bounds cell content to Excel's
length restrictions. It splits worksheets at the Excel row limit.

Files are written to `.part` paths before replacing published files. Each
replacement is atomic, but CSV/XLSX publication is not a single cross-file
transaction; a process kill between replacements is not guaranteed to publish
both files at the same version. SQLite remains the recovery source of truth.

There is one automatic writer. Updates normally occur around 25 new completed
properties or a time-based refresh, with adaptive delay `max(5 seconds, last
write duration * 10)`. Pause, completion, session expiry, and shutdown also
request final/latest snapshots. The scheduler's exact readiness/cooldown logic
is in `flushExports()`; there is no user-facing export button or interval setting.

Locked workbook failures leave checkpoints and the previous workbook intact
and are retried automatically. An open Excel file is not a reason to lose data.

## 12. Persistence And Migrations

SQLite uses WAL and foreign keys. Each operation owns an independent connection,
with a transaction context for commit/rollback. Do not share one SQLite connection
unsafely across Qt, colony workers, and the writer.

Tables and their main purpose:

| Table | Purpose |
| --- | --- |
| `settings` | Persisted UI preferences, pacing settings, schema/parser versions |
| `catalog` | Current/historical municipality and colony options with active flags |
| `jobs` | Selection, output folder, desired/status, counts, boundary, export versions |
| `tiles` | Bounds, depth, pagination, fingerprint, work state, coverage weight, retry |
| `properties` | Job/property identity, sanitized map data, parsed details, pipeline state |
| `photos` | Per-property URL, relative path, outcome, retry/error information |

`jobs` is unique by municipality/colony pair. Properties are unique by
`(jobId, identity)`. Photos are unique by `(jobId, identity, url)`.

`desired` is user intent, distinct from the currently displayed state. Waiting
for login is not the same as a manual pause. Completed jobs normally have
`desired=0`. Preserve this distinction when adding recovery features.

`dataVersion` and `exportVersion` determine whether published files are behind
committed data. `retainedForExport` keeps earlier rows available while a legacy
detail refresh is pending, including while newly discovered photos are pending.

Version snapshot:

```text
PARSERVERSION = 2
EXPORTVERSION = 5
```

The version-one parser missed fields, so its earlier successful pages may need
one refresh. The migration preserves old detail values, map coverage, downloaded
photos, and manual pauses while queuing that work. Old rows remain exportable
until finished. Known public-view errors are reclassified without repeated HTTP.

The version-two-to-three and version-three-to-four export changes affect only local presentation/cleanup.
It marks exports dirty without resetting jobs or fetching all details again.
Do not bump the parser version or force network refresh for a header rename.

For investigations, open the real database read-only and use SQLite's backup
API to create a consistent disposable copy, including committed WAL data. Merely
copying the main `.sqlite3` file while WAL is active can miss committed work.
Never instantiate `Store` on the real database just to inspect it: construction
can perform migrations and change records.

## 13. States, Errors, Retry, And Shutdown

Important job states include queued, preparing, scraping, paused, waiting_login,
retrying, error, complete, and complete_warnings. Some legacy/internal state
labels also remain in the UI mapping; do not expose obsolete pipeline phases
as new user controls merely because a label exists.

- Retrying means persisted work is waiting for a retry after a real request or
  response failure, not that empty map results inherently require retries.
- Complete means the current job has no remaining pending or failed work.
- Complete with warnings means bounded retries left failed properties or tiles.
- Needs attention represents an error requiring intervention, for example an
  unusable boundary after preparation retries.
- Unavailable public data is not a warning by itself.

Property detail/photo work currently has a three-attempt budget before terminal
resolution of the failing stage. Tile and preparation work have five attempts.
Backoff grows exponentially, with a normal delay cap of 120 seconds; a longer
server `Retry-After` is respected. Each photo gets its own retry progress rather
than consuming the entire property's retry budget permanently.

Isolated HTTP 5xx responses retry affected work; clustered errors across jobs,
HTTP 429, explicit Retry-After and transport failures trigger shared cooldown.
Shared outages preserve pending retry budgets and recover through one work
probe, with two successful work requests needed before normal admission.
Ambiguous login checks are coalesced and never establish portal recovery.
Worker login responses are confirmed on the protected page; a rejected saved
token can require marker renewal while the session remains valid. Confirmed
expiry still requires manual OTP renewal. Unknown HTML,
repeated pages, and invalid JSON must not be converted into empty successful
map responses or completed coverage.

Start on a warning job retries failed work while preserving successful records.
Pause stops scheduling further requests; already-started requests may complete
and checkpoint. Delete/reset must drain affected workers and coordinate with the
writer before deleting checkpoints or moving folders.

Graceful shutdown waits for in-flight work, with HTTP requests using a 45-second
timeout, and flushes exports. Do not promise instantaneous termination. Forced
termination can lose uncommitted work or leave Excel behind SQLite, but committed
stages resume and dirty exports can be rebuilt on the next start.

## 14. Concurrency And Performance

The parallel-requests setting is global, not multiplied separately per colony.
The allowed range is currently 1-100, default 2. The executor itself has 100
colony workers; the coordinator starts with at most four active colonies (or
the smaller configured ceiling), reduces on congestion, and increases by one
after 40 healthy work requests and at least 60 seconds.

All HTTP starts share a configurable pacing budget, default 650 ms between
request starts. Concurrency controls simultaneous requests; pacing controls how
quickly requests are started. Raising concurrency does not bypass the interval.

There is no within-colony property parallelization in the current design.
Consequently, setting parallelism to 8 while running one colony does not create
eight simultaneous property pipelines. Do not describe this as already supported.

Details and several images per property mean many more requests than the old
map-only CLI. A historical map-only record count is not a fair benchmark for
fully processed properties with photos. Measure completed properties per time,
request timings, throttle waits, retries, and workbook-writing cost separately.

Current optimizations include avoiding known-restricted detail requests, caching
frequently read throttle settings, an indexed retained-export lookup, background
workbook writes, streaming exports, durable stage-level resume, and an index
on unfinished properties. Backend schema upgrades rebuild portable snapshots
before incremental writes; do not write new columns into an older snapshot.

No official numeric portal rate limit has been confirmed. Do not infer unlimited
capacity or increase load to evade throttling. Respect the portal's terms and
authorization requirements, handle Retry-After, and start conservatively.

## 15. UI Expectations

Keep the native interface compact, clear, and responsive:

- Mobile entry, Generate OTP, and OTP verification dialog.
- Session LED reflecting verified readiness, not just stored credentials.
- Live municipality/colony dropdowns and ALL behavior described above.
- Expandable municipality parent rows with colony jobs underneath.
- Live discovered-property counts and map-work coverage.
- Start/resume, pause, delete, and open-folder controls at both levels.
- Search/filter and sorting without breaking identity or action scope.
- Compact aggregate statistics below the queue, not oversized dashboard cards.
- No photo count requirement, dedicated detail/photo progress columns, export
  button, Excel snapshot column, or snapshot-interval preference.
- A remembered output folder, remembered queue, and a restart flow needing only
  authentication renewal when the portal requires it.

Use existing Qt widgets, styles, delegates, and event patterns. Do not replace
the interface with Electron, React, a local web server, or an unrelated UI stack.

## 16. Storage Locations, Logging, And Sensitive Data

Source-run app state normally lives at `data/application/`. Packaged Windows
state lives at `%LOCALAPPDATA%\ULBPropertyScraper`; the packaged default output
root is `Documents\ULB Property Records`. `--data-dir PATH` selects a different
state directory. `QLockFile` prevents two app instances using the same state.

App state includes `state.sqlite3`, `cookies.lwp`, logs, and structural diagnostics.
The old CLI uses separate cookies/checkpoints such as `data/http_cookies.lwp`;
do not confuse the CLI and desktop sessions or silently merge them.

Desktop logs rotate at 5 MB, with ten backup files. Request logs contain stage,
elapsed time, gate wait, and outcome. Unknown detail layouts save structural JSON
under the app home's `diagnostics/`, not routine full HTML bodies.

Preserve the current privacy boundary:

- Exclude Aadhaar/PAN/family identifiers, passport/date-of-birth fields, and email
  fields according to the existing filters.
- New map checkpoints use `protectRecord()` to sanitize nested/JSON metadata.
- Do not submit objections, self-certify properties, change ownership/mobile
  details, or use correction forms. This application is read-only apart from
  normal authentication and local output/checkpoint writes.

The older `tools/diagnose_portal.py` can write raw HTML. Review and isolate it
before use; prefer structural diagnostics for new investigations.

Important repository hygiene: the current `.gitignore` excludes state/output
directories but does not automatically protect every `refrence/` or
`demo-files*` asset. Audit/redact or exclude sensitive references and datasets
before any GitHub push. Do not assume an existing build workflow means private
data is already safe to commit.

## 17. Reference Evidence And How To Use It

Relevant user-supplied assets include:

- `refrence/search_property.har`: manual direct-search exploration.
- `refrence/map_view.har`: map API and recorded public-view flag logic.
- `refrence/map_view_panchkula.har` and `map_view_panchkula_2.har`: map exploration.
- `refrence/otp_login.har`: login request sequence.
- `refrence/jaigopal.html`, `refrence/embros.html`, and `refrence/jaiGopal.har`:
  representative detail pages and navigation.
- `refrence/1F7CL8R0.html`: recorded two-owner, two-construction-row detail page.
- `refrence/1F7CL8R0_p1.pdf`, `1F8FHFV1_p2.pdf`, `1FJWF3Q7_p3.pdf`: displayed
  examples with different owner/floor counts.
- `refrence/not_available_for_public_full.har`: complete restricted-property
  redirect and signed-in authorization-popup evidence.
- `refrence/not_available_for_public.har`: earlier incomplete capture containing
  only NotificationCounter, not adequate restriction-response evidence.
- `refrence/ulbhryndc_org_mcname_assandh_0[11184].csv`: a poor-quality historical
  export for comparison, not the target schema or a completeness oracle.

PDFs demonstrate visible layout, not exact DOM or embedded request behavior.
Saved HTML is better for parser regression tests. HAR bodies can be missing,
base64-encoded, sensitive, stale, or specific to one session. Inspect metadata
and decode safely rather than dumping every response to logs.

The recorded HTML for `1F7CL8R0` should yield two owners, two construction rows,
ownership shares of 6/12 each, status Aadhar, and the correct property scalars.
Do not hardcode this PID, those counts, or those shares into production parsing.

## 18. Verification Snapshot And Remaining Limits

Latest completed automated run: 159 tests passed, plus an offscreen desktop smoke
test and compilation checks. This is a dated result, not a promise about future
changes; rerun appropriate tests after modifying code.

The tests cover geometry/holes/edges, out-of-scope filtering, deduplication,
dense splitting, repeated pagination, transactions, migration, OTP/session
recovery, concurrency/pacing, pause/restart/delete/ALL, dynamic tables,
restriction handling, export ordering, workbook lock recovery,
portable photo links, and safe preview generation. Network pipelines use
recorded/simulated responses; do not describe them as fully live portal tests.
Update coverage includes validation/confirmation, legacy imports, moved-folder
recovery into an empty database, delete/re-add, catalog renames, partial resume,
new generations, changed/unchanged/new/missing rows, failed details/photos,
restrictions, incremental checkpoint writes and mixed queue controls. A temporary
copy of `nov-3-test-run-2` retained all 184 PIDs and checked address/owner/mobile
values through import/export; 331 individual photo links resolved. SHA-256 file
checks confirmed the original dataset remained unchanged.

Earlier November 3 review of `/Users/jayraj.karelia/Desktop/avi/nov-3-test-run-2`:

| Job | Observed State |
| --- | --- |
| JIND / Amrehdi | Complete, 13 exported records |
| PANCHKULA / Gurukul Colony | Complete, 36 exported records |
| PANCHKULA / Sector 6 | Paused, 55/335 map tiles, about 16.4% coverage; 145 discovered, 135 completed/exported, 10 pending |

The schema-5 offline preview under `output/nov3-run2-fixes-preview` preserves all
184 exported rows, PIDs, addresses, plot numbers, classification values and full
contact numbers. Its 504 workbook photo hyperlinks resolve to existing files.
Timestamps retain their instants while converting to IST. Alias removal reduces
Amrehdi from 66 to 63 columns, Gurukul from 51 to 48, and Sector 6 from 66 to 63.
The source database was opened read-only and backed up; original outputs were
not replaced. This review did not repeat the earlier image-decoding or geometric
audit, which had passed on the first November 3 run's 127 exported rows.

The second run's logs explain the perceived Gurukul progress freeze: at 56.25%
coverage, between 03:50:24 and 03:51:24 IST, 11 Gurukul properties and 29 properties
across all jobs finished processing. Coverage waited for the next map tile.
The run completed 184 properties at an observed rate of about 1,900/hour over
the first-to-last property completion window. About 512 map/detail/photo requests
were logged, or 2.8 requests/property for this sample. At that mix, a 650 ms
shared interval caps throughput near 2,000/hour before other overhead; 250 ms
allows about 5,200/hour ideally, and 6,000/hour needs roughly 216 ms or less.
These are sample-based estimates, not a live benchmark at the faster settings.

Known validation limits and work not yet established:

- Sector 6 was not complete at the last review; do not compare 145 against a
  manual whole-colony count as if it were the finished result.
- The early pilot allowed a rough manual-count tolerance of about ten markers,
  but unexplained failures or missing coverage must still be investigated.
- Complete a controlled full-colony comparison and an overnight pause/restart
  test before a statewide run.
- No demonstrated statewide/hundreds-of-thousands-record performance result exists.
- No official published rate-limit number has been confirmed.
- Login/token behavior and unusual layouts still need live checks as they occur.
- The instruction-only status cleanup is validated on saved checkpoint values
  and synthetic HTML cases; the latest live affected-page check hit Session Expired.
- Exact recorded HTML is not available for every PDF layout or municipality.
- Geometric coverage cannot prove the portal returned every real-world property,
  especially if it silently truncates responses below the advertised cap.
- Very wide dynamic schemas can hit Excel's column limit; row splitting alone
  does not solve that. Large-scale memory/disk/export behavior needs measurement.
- The GitHub Actions configuration exists, but that alone is not evidence a
  Windows artifact was built, downloaded, and fully exercised on a real user PC.

## 19. Setup, Run, Preview, And Build Commands

Run commands from the project root. A fresh developer setup on macOS/Linux is:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and use
`python app.py`. With the existing local environment, `.aviEnv/bin/python app.py`
is the corresponding macOS command.

Use isolated app state for development:

```bash
python app.py --data-dir development-state
```

Offline verification, using a disposable smoke-state directory:

```bash
python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python app.py --smoke-test --data-dir /tmp/ulb-smoke-state
python -m compileall -q app.py scraper.py ulb_scraper tests tools
```

On Windows, set `$env:QT_QPA_PLATFORM = 'offscreen'` in PowerShell instead of
the Unix environment-variable prefix, and choose a local disposable path.

Generate safe offline preview exports:

```bash
python -m tools.preview_exports --source data/application/state.sqlite3 --output output/new-preview
```

The preview destination must be new or empty. The tool backs up the source
read-only, migrates the temporary copy, and writes preview workbooks with copied
photos. It makes no portal requests. Preview exports alone do not prove live
scraping or a parser refresh has happened.

Build on Windows, not by assuming cross-compilation from macOS:

```powershell
python -m pip install -r requirements.txt pyinstaller==6.19.0
pyinstaller --noconfirm build_windows.spec
```

The intended artifact is `dist/ULBPropertyScraper.exe`. The GitHub workflow uses
Windows and Python 3.12; it runs tests, builds, smoke-tests the executable, and
uploads an artifact for main/master pushes, `v*` tags, or manual dispatch.
Do not bundle cookies, HARs, data folders, or developer credentials.

## 20. Working Rules For The Next Developer Or AI

- Read the implementation before proposing a rewrite. Extend the existing
  Store/Engine/PortalClient/parser/exporter contracts where practical.
- Respect the user's SWAT-style guidance: descriptive camelCase identifiers,
  single-quoted strings, separated/alphabetized import groups, logging rather
  than print in application code, f-string messages, useful public API
  docstrings, and no trailing whitespace. Preserve local consistency.
- Use `rg` for searches and `apply_patch` for manual source edits when available.
- Keep changes scoped. Avoid broad renames or dependency churn while fixing a
  specific parser or state-machine issue.
- Do not silently swallow unknown schemas, alter failure counts to look clean,
  or mark incomplete map work successful.
- Keep operations idempotent where possible and preserve exception information.
- Add focused regression tests, expanding pipeline tests when shared behavior
  or pause/delete/session races change.
- Prefer read-only database inspection and disposable backups for migrations
  and export experiments. Never run `--reset` on user datasets without consent.
- Review API calls before live diagnostics; do not overload the portal, send
  OTPs without being asked, or submit property-editing forms.
- Explain exactly what was verified offline, simulated, live, or not verified.
- Keep the user informed while investigating. Finish with a concise change and
  verification summary, including any remaining risks.

Before declaring a change done, check that it preserves geographic scope,
deduplication, stage-level persistence, manual pause intent, public-view denial
handling, session-epoch isolation, output-folder portability, dynamic schemas,
and the latest human-readable export conventions.

## 21. Concrete Task For This Handoff

The next user/developer should provide the task . If it is blank, summarize
your understanding and wait; do not start modifying the app or scraping data.

```text
Current task:
[Describe the bug, feature, review, or explanation requested.]

Acceptance criteria:
[State the expected result and any test data or scenarios.]

Constraints or protected data:
[Identify folders/checkpoints that must remain untouched and any live-access limits.]
```
