# Folder-based data updates

Implemented through **Update data** in the desktop application. This document
describes the workflow, persistence and limits of the implementation.

## User workflow

1. Choose **Update data**, optionally check **Update only properties with no
   mobile numbers**, and select an existing dataset root containing
   municipality and colony folders. Existing queue entries are not required.
2. Validate the folder structure, IDs, readable CSV/XLSX files, property
   identities and relative photo paths. Show the recognized colonies and any
   invalid or ambiguous folders before adding anything.
3. Confirm a short explanation: the app will fetch current map data, details
   and photos, keep previous rows available until successfully checked, add new
   properties, and preserve records not found in this pass for review. The work
   can be paused and resumed and can take approximately as long as a fresh run.
4. Recreate the selected municipality/colony jobs with an **update** mode. Start
   after a valid login, using the same pause, resume, folder and delete controls.
5. Show **Update queue** when every job is an update, **Scraping queue** when
   every job is a fresh scrape, and **Scraping / Update queue** when mixed.
   Active statuses distinguish **Updating** from **Scraping**. Paused, login
   and retry states remain visible for both modes.

The selector can add a missing municipality/colony as a fresh scraping job while
other jobs update existing data. A successful import selects that root for
subsequent additions. Re-adding an already queued pair in the same
dataset must focus or reuse that job instead of resetting its progress.

## Dataset identity must outlive the queue

Identify a dataset separately from a transient queue job. A durable dataset ID
plus municipality ID and colony ID identifies its colony data. Generated job
UUIDs and display names must not determine which historical records are updated.

The output root contains `.ulb-dataset.json`, with a durable dataset ID. Each
colony folder contains `.ulb-checkpoint.sqlite3`, with its source records,
baseline history, schema versions and update progress. Photos remain relative
files in that colony folder. Move or copy the entire root, including hidden
files and photos, to preserve resumability. This is local storage, with no
additional service. Login cookies and phone preferences are not copied into
portable checkpoints.

Deleting a queue entry should remove scheduling, not this durable dataset.
Deleting actual data would be a separate explicit action. Multiple delete/add
cycles then refer to the same dataset without losing its history. If a pair is
already queued against a different output root, import reports the conflict.
Remove that queue entry before importing the intended folder; its saved data
remains available. Datasets are not merged automatically.
If an earlier update generation is incomplete, offer **Resume update** or
**Start a new update**, defaulting to resume. A completed dataset selected for a
new refresh starts a new generation while preserving its previous baseline.

## Importing older folders

Existing folders, including `nov-3-test-run-2`, can lack a portable
dataset checkpoint. These use a legacy import path:

- Read municipality/colony IDs from the existing directory structure and verify
  them against the current catalog when authenticated. Names are display values;
  renamed colonies and inactive catalog entries need explicit handling.
- Prefer CSV as the structured baseline; read XLSX if CSV is absent. If both
  disagree, report the conflict instead of assuming they are the same snapshot.
- Match by PID within the identified colony, using internal property IDs as
  well when an original checkpoint is available. Duplicate or absent identities
  need review; do not silently merge or drop those rows.
- Preserve compact historical rows as a baseline. Combined name strings cannot
  reconstruct every original gender/guardian/share field, and CSV does not
  contain valid detail tokens. Fresh map responses provide current identities
  and tokens; fresh details rebuild the structured data.
- Retain usable existing photos and reject imported paths escaping the chosen
  root. Preserve the original files until validation, confirmation and a safe
  baseline snapshot are complete.

Queue reconstruction must work even when the original application's state
database no longer exists. If a matching checkpoint is available, reuse the
additional information, but do not require it for a full refresh.

## Update execution

Start a new persisted update generation with fresh boundaries and zero current
map coverage. Reusing a six-month-old completed map checkpoint would skip the
very requests needed to find changes and new properties.

Rescan current in-scope map tiles, obtain fresh detail tokens, and run each
property's detail/photo pipeline through the existing request gate. The local
application database checkpoints each stage. Portable checkpoints synchronize
incremental changes after each bounded work unit and before export, queue
deletion and application shutdown. Keep old values available while requests are pending or fail; a
transport/parser failure must not replace a good historical row with blanks.
An authoritative successful response can legitimately remove an old field;
compare complete successful rows rather than retaining every old nonempty cell.

Compare normalized data excluding volatile fetch times. Successful checks update
the last-checked time in IST even if content is unchanged; the last-changed time
advances only when property data changes. Photos are downloaded into generation
specific filenames and compared by their content hashes. An unchanged URL alone
does not prove unchanged image content. Relative links remain portable.

New properties are inserted. Previously known properties not encountered during
a complete successful scan are marked for review rather than immediately
deleted. A partial, paused or failed scan cannot establish absence. Newly added
municipalities and colonies appear through catalog refresh and can be selected
for fresh scraping.

## Performance limits

No portal change feed, modification filter or reliable conditional-request
contract has been established. Without one, deciding whether each property has
changed still requires fetching it. Comparing hashes reduces unnecessary local
rewrites and identifying unchanged images may reduce downloads, but neither
eliminates map/detail checks.

## Retention and review

The checkpoint retains the latest successful row for each PID and one previous
generation's baseline in `datasetHistory`. Older photo files are retained; this
version does not prune them. A successful response replaces the complete row,
including fields the portal has cleared. A failed detail or photo fetch retains
the previous successful row. A new public-view restriction also retains the
previous details and is marked for review.

Update exports add `Data Last Checked At (IST)`, `Data Last Changed At (IST)`
and `Update Review`. Review values distinguish pending, changed, unchanged, new,
failed, restricted and not-found records. Not-found rows remain in CSV/XLSX;
there is no automatic deletion or separate review editor. An unknown historical
last-changed time is shown as `-` until a change is observed.

CSV/XLSX disagreement blocks import before file changes. Keep the intended
consistent pair, or retain only the intended CSV or XLSX, then select the root
again. Confirmation fingerprints are rechecked before import; if active work
changes the files, pause that work and select the folder again. Catalog IDs are
verified before scanning; offline imports wait for login and catalog validation.


## Missing-mobile update scope (20 November 2026)

- Use the exported Mobile Numbers field to select existing baseline properties
  without a usable value. Store the target flags and report path in portable
  checkpoints so resume does not change the selection.
- Search the colony map for current links, but skip known-number properties and
  newly discovered PIDs. Fetch target details, reusing normal session/token
  recovery. Do not download photos again.
- Save newly found phone fields and audit times into the original baseline.
  Retain other fields and photos. Unsuccessful checks never erase old data.
- Export newly numbered properties only to Updated-mobileNos-{IST timestamp}
  under the source root, with the same municipality/colony folders. Keep photo
  links usable by copying referenced existing photos for these report rows.
  Empty-result colonies do not get report exports.
- Ignore Updated-mobileNos-* and Mobile-updates-* folders during validation.
  Moving a complete dataset rebases the resumed report path to its new root.
- Use the same checkbox when resuming an unfinished mobile update. Switching
  between full and missing-mobile scopes requires a new update generation.

## Import and removal performance

The November 19 logs show initial validation at 23:18:28 and confirmation at
23:18:31, followed by import at 23:18:43 and database import at 23:24:06.
Repeated colony-catalog calls dominated that gap. Catalog checks now run once
per municipality. Confirmation compares file fingerprints and folder inventory
without re-parsing unchanged spreadsheets. It still rejects changed sources.

Removal saves dirty exports and portable checkpoints, waits for active work,
and then deletes queue entries. It runs outside the GUI thread. Clean exports
and unchanged checkpoints are not rewritten; cascade deletion does not enqueue
portable changes for a removed parent job. Both import and removal report their
current stage and progress, with an animated indicator for unknown totals.

A synthetic offline benchmark with 1,400 CSV-only colony folders and 27,000 rows
measured approximately 22 seconds for import (including confirmation checks and
portable checkpoints) and 8 seconds for removal. It retained all 1,400 portable
checkpoints. This excludes real portal latency, XLSX parsing and photo I/O;
these numbers are not production speed guarantees. Raw results are saved in
output/dataset-operations-benchmark-2026-11-20.json.
