# Cloud monitor migration

## Responsibilities

Binance execution stays on its approved Oracle VPS and fixed egress IP. This
migration does not move its broker credentials, place an order, change its
trigger, or add Google Cloud fixed egress/NAT infrastructure.

Cloud Run infrastructure monitoring continues to use the provider's existing
logs, metrics and alert policies. CPU, memory, HTTP failures and instance health
are not reimplemented in a second collector. These observations do not prove a
complete trading cycle, account identity, reconciliation or strategy health.

The independent monitor Job reuses `health_cycle.py` and QPK for strategy/data
coverage and drift. The common checks do not branch on Oracle, Google Cloud or
the broker. Self-hosted path/process differences stay in the source adapter;
ordinary producer-authored run records remain inputs to the existing Binance
live-run consumer. The adapter never calls Binance's trading API. Missing live
stream/window bindings remain unavailable, not inferred from a report date.

## Read-only qualification

`Dockerfile.cloud-preview` builds an immutable Python 3.12 image with the
repository's existing hashed Linux dependency lock and QPK pin. Explicit
40-character revisions bind the four strategy code trees. It uses a dedicated
identity with get access to one protected input object; no Bot, broker,
GitHub-writing or model credentials are injected. No Scheduler is enabled for
qualification.

`cloud_run_health_job.py` defaults to `MONITOR_MODE=preview`. It reads
`current.tar.gz` under the protected `MONITOR_STATE_GCS_URI`, validates every
archive member before extraction, and recomputes in a temporary workspace.
`MONITOR_PROJECT_CODE_ROOT` contains code from the image. The archive may carry
only data, never Python, a Git directory, environment files, keys or links.
Stored observation times, missing data and unknown delivery states are retained.

Preview disables Telegram, issue creation, AI diagnosis and alert clearing in
the actual shared health-cycle entry point. It does not refresh inputs, publish
the website, reserve an execution or write GCS state. Full reports stay inside
the protected runtime; Cloud Logging receives only bounded status, data-status
and count fields. Source/QPK differences from the original capture are reported
explicitly and are not qualified as equivalent merely because imports succeed.

The CLI rejects production mode in this preparation release. Its portable state
and cycle primitives have offline tests, but that is not production adoption.
The existing VPS cycle remains the active owner until cutover is qualified.

## Source adapter and state

`export_local_monitor_state.py` runs manually on the approved source host. A
protected operator config binds the monitor root, four strategy and four snapshot-pipeline data roots,
source/QPK revisions, existing gcloud identity hash and destination prefix.
It captures only explicit data subtrees, including `data/alert-state`, the
separate diagnosis consumer, and the explicitly bound external Actions
checkpoint directory mapped to `monitor/data/diagnosis-actions`. These are
distinct ledgers: one cannot substitute for another. Cross-day records and
`job_id:null` submissions remain byte-for-byte intact. Files changing during capture or links escaping
the approved data roots cause rejection. It never copies `.git`, a venv,
credentials or a local advisory lock. The archive goes directly from that host
to protected GCS through its existing client, with generation-zero creation.
Refreshing this task's unused staging capture requires its exact observed
generation; it is never an unconditional overwrite or an empty replacement.
Private bodies must not transit a developer workstation or public CI artifact.

Reuse QPK's existing PerformanceStore for lifecycle data. `CloudState` is only a
narrow GCS adapter for the generation preconditions needed by portable local
monitor state; the existing ObjectStore interface has no equivalent guarded
overwrite/delete operations. It is not a new storage framework or database.

For a future adopted cycle, claim `active.json` with generation zero **before**
reading mutable state. Restore the exact generation, execute once and commit
against that generation before releasing the exact claim generation. Capacity
limits alone do not prevent cross-instance duplicates. An uncertain effect or
commit retains its claim; do not expire it, replay effects or reset to an empty
ledger. This preserves pending/unknown delivery and diagnosis history across
container shutdown. A read failure is not a successful empty state.

`checkpoint(raw, state_generation=..., claim_generation=...)` verifies the
claim, durably saves an intermediate state, returns the new state generation
and retains the claim. The diagnosis caller uses it after saving intent before
POST, after acceptance before GET, and after receipt before success.
`release(claim_generation=...)` is separate. A failed/uncertain checkpoint must
stop the caller and retain ownership, never proceed to POST or polling.

## Adoption order

The existing website snapshot publication remains part of the computing cycle.
The dedicated sync credential moves directly from the protected source client
to its own Secret Manager secret, with access only for the monitor runtime.
The website's existing bearer contract and original token stay unchanged.
Set `STRATEGY_HEALTH_SYNC_URL` to the original endpoint and
`STRATEGY_HEALTH_SYNC_SECRET_VERSION` to its approved numeric resource reference
(`projects/PROJECT_NUMBER/secrets/SECRET_NAME/versions/VERSION_NUMBER`). The Job
reads that exact version using its existing runtime ADC only during publication;
do not inject the website token as a startup-required secret. This avoids making
website credential availability a prerequisite for independent computation.
The existing Secret access binding is retained, without a new storage object,
identity or dependency. The standalone `publish_snapshot(token=...)` transport
continues to support its existing restricted callers.

After evaluation, checkpoint the complete computed/notification state before
publishing bounded GCS projections. Website publication then checkpoints a
bounded `monitor/data/health/website-publication.json` record as `unconfirmed`
before reading credentials or issuing HTTP. Only a verified response changes
it to `accepted`. Commit the latest publication record with the existing core
archive and release its exact claim even when website delivery is unconfirmed;
that outcome produces a warning, not a claim that the website was updated.
The record describes only the latest snapshot publication and contains its
digest and the actual record time, never the snapshot, secret or raw error.

`monitor_website_publish.py` posts each cycle's original snapshot once with no
redirects, immediate retries or credential-bearing command arguments. A later
cycle publishes its newly computed snapshot rather than replaying the failed
cycle. An uncertain checkpoint still stops before further operations and retains
ownership. Notification delivery and unknown-effect ledgers keep their existing
protection. Preview mode cannot invoke either publication path or read the Secret.

The hosted `quant-monitor-inputs.yml` adapter uses separate, short-lived App
identities for source Actions reads and four-repository issue-only writes.
Bind them with `QUANT_MONITOR_READER_APP_ID` /
`QUANT_MONITOR_READER_APP_PRIVATE_KEY` and `QUANT_MONITOR_ISSUES_APP_ID` /
`QUANT_MONITOR_ISSUES_APP_PRIVATE_KEY` in protected deployment configuration.
The reader needs only Actions read and implicit metadata read on the four
strategy repositories and BinancePlatform; the synchronizers call only Actions
run/job/artifact APIs. The writer needs only Issues write and metadata read on
the four strategy repositories. Do not extend a broad code/workflow-writing App
to Binance merely because a minted token can be narrowed. Missing installations
or credential bindings require a concrete access decision; do not fall back to
the source host's personal token. Preview/state verification skips both Apps.
Its direct WIF identity can mutate only its input archive and claim, and read
the bounded strategy-findings projection. It cannot read the computing state.
Input refresh is enabled with an explicit deployment variable; manual checks
default to read-only verification. Neither path executes broker code or models.
The cloud Job reads refreshed inputs and retains sole ownership of computed
monitor history and the existing notification ledger. Each delivery intent and
receipt is checkpointed before the next operation. An uncertain checkpoint
keeps the common claim and stops processing. The independent diagnosis consumer
continues to own its separate JSON checkpoint; the Job cannot submit diagnoses.

Portable inputs retain source version history, original live records and all
eight strategy/snapshot data roots. Only flattened active artifact aliases are
regenerated by the existing synchronizer. Unavailable upstream data remains an
explicit error; input timestamps are not extended to make stale data pass.

The completed manual `monitor-runtime-digest-once.yml` operation is retired.
Its original source receipts and reusable consumer/tests are retained. The
unified daily Cloud Run sender owns subsequent daily delivery.

During the rolling migration, `run_health_with_projection.sh` wraps only the
original self-hosted health entry point, preserving its exit code and all
existing settings. After an existing saved cycle, the lightweight
`publish_saved_monitor_observation.py` adapter publishes the bounded observation
with an exact generation precondition. It never runs an extra health cycle,
model or notification. The object metadata `monitor-producer=self_hosted_transition`
identifies this temporary owner. It refuses to overwrite a newer observation
or a different producer. Once the cloud Job takes over and changes that
producer marker, the source adapter stops writing. Remove the adapter-owned
drop-in when retiring the original monitor timer; do not alter the Binance
execution service or its fixed IP.

1. Qualify the read-only image and captured inputs. Compare the original source,
   dependencies, original state, coverage and missing-data reasons separately.
   A successful execution can still correctly report unavailable data.
2. Follow the owner's updated order: complete migration and the approved
   extension before joint acceptance. A future natural daily-report window is
   not a prerequisite to migration. Retain the targeted source/identity and
   no-duplicate checks required for a safe cutover; do not relabel a manual
   verification as a natural business event.
3. Confirm the source timer/service and in-flight work. Stop only the monitor
   timer after the cloud checks pass; retain Binance execution and other VPS
   services. Capture final state again after the old cycle ends into the
   production prefix with create-only semantics. A staging capture is not the
   final delivery/diagnosis history. Preserve the original source files.
4. Wire the existing approved input refresh, publish and effect identities;
   qualify native AI authentication without exporting the VPS's credentials.
   Keep task retries off and configure one independent half-hour Scheduler.
5. Verify one natural cloud cycle and persistent state before declaring the
   monitor migrated. Never enable both senders as independent failovers or
   enable a disabled trading target to obtain evidence.

Google documentation: [scheduled Jobs](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule),
[ephemeral container filesystem](https://docs.cloud.google.com/run/docs/container-contract#file_system),
[conditional storage copy](https://docs.cloud.google.com/sdk/gcloud/reference/storage/cp).
