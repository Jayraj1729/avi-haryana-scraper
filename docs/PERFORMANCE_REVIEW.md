# Scraping reliability and throughput: 3 November 2026

The observed collapse had several causes. Portal errors and long response times
were real, and the client amplified them through global pauses, repeated login
checks and queue scheduling. An additional live check confirmed that a saved
property token can produce a login response while the authenticated session
remains valid. There is no established numeric portal quota or session lifetime.

## Evidence from the existing run

Times below use the app log's local time, IST. Measurements count successful
pipeline log events and include idle time within each window. Repeated checks
of the same PID in different generations count as separate pipelines.

| Window | Completed properties | Properties/min | Other observations |
| --- | ---: | ---: | --- |
| 12:26–12:36 | 281 | 28.10 | Successful request median 0.609 s; 95th percentile 4.218 s |
| 12:42–12:49 | 6 | 0.86 | 229 HTTP 500 responses and 319 successful login checks |
| 13:05–13:10 | 8 | 1.60 | 57 request timeouts; successful request median 8.215 s, 95th percentile 38.384 s |

These windows contain no HTTP 429 response establishing rate limiting. HTTP 500
and timeouts are consistent with transient backend failure or overload, but do
not prove a per-account, per-IP or session concurrency quota. One later response
was classified as authentication failure by the old client; that classification
alone cannot establish that the login expired.

The saved queue contains 574 jobs. At 20 parallel requests, round-robin scheduling
repeatedly admitted new colony setup/map work instead of keeping a small batch
of colonies advancing through property pipelines. Each HTTP 500 also paused all
jobs and triggered a login check. Successful login checks reopened the gate
without proving the failing work endpoints had recovered.

Aggregate source measurements are in `output/scale-analysis-nov3.json`.

## Live, bounded checks

An existing authenticated session was used with scraping paused. No OTP was
sent. Clients did not persist cookies. The production database was opened
read-only; downloads, parser diagnostics and queue experiments used temporary
storage.

A fresh map sample contained 12 properties, eight restricted from public detail
view. The same sample was processed at each level, including eligible detail
requests and photos, with at least 250 ms between request starts:

| Parallel pipelines | Completed | Elapsed | Properties/min | Requests | Request p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 12 | 14.028 s | 51.32 | 24 | 0.922 s |
| 2 | 12 | 8.746 s | 82.32 | 24 | 1.328 s |
| 4 | 12 | 6.334 s | 113.68 | 24 | 1.084 s |

All levels completed without errors. These short probes exclude continuing map
scans, checkpoint writes and exports. Reused sample URLs can benefit from server
caching. Their high restricted-property proportion also reduces request cost.
They demonstrate useful concurrency, not an hours-long sustainable maximum.
See `output/portal-probe-nov3.json`.

Single saved map requests for Ambala / 8 Marla Housing Scheme, Assandh / Old Area
Tractor Market and Panchkula / Sector 6 all succeeded, taking 2.937, 4.433 and
2.936 seconds respectively. Thus the earlier failures in those requests were
not reproduced at low concurrency. See `output/portal-map-check-nov3.json`.

The first full-engine trial on a temporary four-job backup stopped after a saved
detail token was classified as expired. A subsequent comparison confirmed that
the protected session page was valid both before and after a cached Ambala
detail token raised `AuthenticationError`. This is a false global-expiry case,
not a reason to repeatedly request OTPs. See `output/portal-auth-check-nov3.json`.
The aborted trial in `output/engine-probe-nov3.json` is not a throughput benchmark.

After fixing token renewal, a repeated full-engine trial processed **143
successful properties in 123.271 seconds**, including final checkpoint/export
shutdown: **69.6/minute**, or approximately **4,176/hour** over this short window.
It used four independent real jobs at concurrency four and 250 ms pacing on an
isolated backup, renewed 11 rejected tokens, published all four workbooks, and
had zero portal outages, zero false expiry events and zero new terminal property
errors. See `output/engine-probe-nov3-v2.json`. Production state still contained
574 jobs, all paused, with 342 completed and 1,024 discovered properties; the
verification did not migrate the production schema or write its dataset files.

This two-minute check includes real pipeline, map, checkpoint and export work,
but still does not establish an hours-long sustainable rate or a representative
public/restricted-property mix for the entire dataset. No live experiment was
run at 20 simultaneous pipelines.

## Implemented behavior

- Configured parallel requests remain a ceiling from 1–100. Initial effective
  concurrency is at most four, or the smaller configured ceiling. A bounded
  colony batch retains its slots while it progresses; queued jobs are admitted
  when slots become available. Existing pending property work has priority when
  admitting jobs. A single colony's detail/photo pipeline remains sequential.
- An isolated HTTP 500/502/503/504 uses the affected job's bounded retries.
  Multiple failures across jobs within 15 seconds indicate a shared outage.
  HTTP 429, explicit `Retry-After`, and transport failures trigger shared backoff.
- Shared congestion halves effective concurrency and increases effective pacing
  without changing saved preferences. Cooldowns increase from 5 seconds up to
  5 minutes, respecting longer `Retry-After` values. Existing requests drain;
  recovery admits one work probe. Two successful work requests permit normal
  scheduling at reduced concurrency. A login check cannot turn the portal green.
- Shared outages preserve pending property/tile retry allowances. Repeatedly
  failing probe jobs yield to other colonies. Three transport failures without
  a successful request in the same job/stage pause that colony for review,
  retaining its pending work. Other jobs can recover independently.
- Healthy work ramps the ceiling by one after at least 40 successful requests
  and 60 seconds. Sustained slow responses reduce concurrency. This is a client
  policy, not a claimed portal limit.
- Ambiguous HTTP-error login checks are coalesced to at most one per minute and
  deferred during cooldown. A worker login response is confirmed on the protected
  session page after releasing its request slot. When that session is valid,
  rejected detail/photo credentials cause a bounded current-marker refresh for
  the affected property. Checkpoints and map coverage remain intact. Confirmed
  session expiry still suspends enabled jobs for manual OTP renewal.
- Active shows actual colony work units versus the effective limit. Its tooltip
  includes the configured ceiling, active HTTP requests and effective interval.
  Portal cooldown and probe states remain visible. Request logs include stage,
  endpoint path without query values, HTTP error status, wait time and concurrency.
- A partial index on unfinished properties avoids scanning completed properties
  for each next-record lookup as a colony grows.

Start with a ceiling of **4** and **250 ms** pacing for this tested workload;
the controller may reduce load. Queue size is separate from active concurrency:
hundreds of colonies can wait behind that bounded batch. A 20-request ceiling is
not a proven safe operating point. A definitive sustained limit needs longer
measurements across representative municipalities, public/restricted mixes and
photo counts. No proxy, extra accounts or authentication bypass is used.

## Runtime for 12–13 lakh properties

The table assumes successful property throughput sustained 24 hours per day.
Pauses, login renewals, failed requests and variable photo counts increase time.

| Sustained properties/min | Properties/hour | 12 lakh | 13 lakh |
| ---: | ---: | ---: | ---: |
| 20 | 1,200 | 41.7 days | 45.1 days |
| 28.1 | 1,686 | 29.7 days | 32.1 days |
| 40 | 2,400 | 20.8 days | 22.6 days |
| 69.6 | 4,176 | 12.0 days | 13.0 days |
| 83.3 | about 5,000 | about 10 days | about 10.8 days |
| 100 | 6,000 | 8.3 days | 9.0 days |

At eight hours per day, calendar duration is roughly three times the table.
The earlier target of 5,000–6,000/hour requires 83–100 completed properties/minute
continuously. The tiny four-pipeline sample reached that range; the full-engine
two-minute check and existing longer log window did not. None proves a reliable
multi-day rate.

At 250 ms pacing there are at most four request starts/second, shared across map,
details, photos and control requests. The earlier healthy window used about
2.71 requests per completed pipeline, giving an optimistic pacing-only ceiling
of about 89 properties/minute for that particular mix. Latency, retries and map
work reduce it; properties with more images or accessible details cost more.
Increasing concurrency alone cannot bypass that shared request-start budget.

## Repeatable measurements

Verification: 159 automated tests passed, including 100 queued jobs with a
four-colony starting batch, shared-outage recovery, stale-token renewal,
confirmed expiry, portable schema migration and the existing export/update
regressions. Compilation and an offline app launch also passed. These checks
complement the bounded live experiments; they do not certify multi-day uptime.

For a wall-clock report from ordinary scraping, including idle periods:

```bash
python -m tools.analyze_performance \
  --since 2026-11-03T12:26:00 --until 2026-11-03T12:36:00 \
  --output output/performance-report.json
```

For a small live probe, first pause actual scraping and retain a valid login:

```bash
python -m tools.benchmark_portal --interval-ms 250 --samples 12 \
  --output output/new-portal-probe.json
```

The probe uses an existing colony (`--colony-id`, default Sector 6), fresh map
tokens, temporary photos, and fixed concurrency levels 1, 2 and 4. It stops
escalating on the first error or slow 95th-percentile response. Each level permits
at most 60 requests and 90 seconds of new request starts; in-flight requests
retain the client's 45-second timeout. Report paths must be new. This utility
does not change the scraping queue or exports.
