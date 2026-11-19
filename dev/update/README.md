# Scheduled upstream update task

This checkout is a live installation. A daily Omnigent scheduled task brings
`working-branch` onto upstream `main` and reinstalls it through `./update.sh`.
This file is the versioned source for that task's prompt; the live copy lives in
the scheduled task record.

| Field | Value |
| --- | --- |
| Task ID | `c82145c88720411dbb251279c5dcf803` |
| Name | Update Omnigent working branch to latest main |
| Schedule | `FREQ=DAILY;BYHOUR=4;BYMINUTE=30`, `America/Sao_Paulo` |
| Workspace | `/mnt/storage/omnigent-working-branch` |
| Execution target | `connected_host`, host pinned |
| Session control | `agent_session_control: true` (may post Inbox notifications) |

The prompt below assumes the branch shape recorded in `AGENTS.md`: upstream
`main` plus one commit per private feature, rebased (never merged) onto the
current `origin/main`, with a clean tree between updates.

Changing the automation means editing the prompt here and then copying it into
the task (`sys_scheduled_task_update` needs `prompt` and `permission_mode`
resent). Keep both copies in step; the prompt is what an unattended run obeys.

## Operating facts the prompt relies on

- `update.sh` refuses to run without a single Alembic head, so a private/upstream
  migration divergence must be merged by `python scripts/merge_migration_heads.py`
  before the updater is invoked. The updater itself never upgrades the database;
  the server applies migrations on boot and the script asserts the revision
  afterwards.
- A pending migration makes the updater copy the live database, which is about
  11 GB. `update.sh` checks free space up front and prunes superseded
  `chat-before-update-*.db` copies after a verified success
  (`OMNIGENT_UPDATE_BACKUP_KEEP`, default 2).
- Every run writes `~/.omnigent/update-state.json` with the commit, code
  revision, database revision, result, duration, and backup path. Use it to
  confirm what is installed instead of comparing HEAD by hand.
- Read the update log from journald with a time window
  (`journalctl --user -u omnigent-update.service --since <start>`). A fixed
  `/tmp/omnigent-update.status` file keeps the previous run's success line when a
  later run dies before writing it.
- `web` type checks and the `web-tsc` pre-commit hook need node headroom on this
  host: export `NODE_OPTIONS=--max-old-space-size=3072`.
- The pre-push reference-policy hook cannot pass after a rebase: the pushed range
  starts below the current upstream tip, so it scans upstream's own commits. Use
  `--no-verify` only after checking that `python dev/lint/lint_reference_policy.py
  --base origin/main --head HEAD` reports no findings, and say so in the report.

## Prompt

```text
You are updating the live Omnigent installation at /mnt/storage/omnigent-working-branch
onto the latest upstream main, keeping the private branch's feature commits. This
touches a production installation with an ~11 GB SQLite database. Work in one pass,
verify as you go, and report once at the end.

## Shape of the branch (do not change it)

`working-branch` is upstream `origin/main` plus one commit per private feature.
Run `git log --oneline origin/main..HEAD` for the current stack; each commit there
is exactly one feature. Never merge; rebase. Never add a commit that mixes two
features, and fold a fix into the commit that owns it (`fixup!` +
`git rebase -i --autosquash origin/main`). The tree must be clean before and after.

## Invariants

1. The live checkout is /mnt/storage/omnigent-working-branch on `working-branch`.
   The authoritative services are the user-systemd units `omnigent-server.service`
   and `omnigent-host.service`. Never start a manual `omnigent server`/`omnigent host`.
2. Never run `git reset --hard`, `git clean -fd`, `git checkout -- .`, or switch
   branches in this checkout. If the tree is dirty, stop and report: uncommitted
   work must be committed as its own feature commit first.
3. Never edit an applied migration, never stamp `alembic_version` by hand, never
   delete or recreate the live database. Keep exactly one Alembic head; join a new
   divergence with a new merge revision that has a no-op downgrade.
4. Never run `PRAGMA quick_check`/`integrity_check`, `sqlite3 .dump`, or any
   row-level scan of the live database: on ~11 GB these run for tens of minutes.
   Keep every database read cheap and single-row.
5. Do not unpause or run the `Manager - codex`, `Manager - opencode`, or
   `Manager personal pc` schedules. Do not start browser/E2E-UI runs.
6. Install only through `./update.sh`, never with a manual rebuild or restart of
   the service units.

## Phase 1 - Recon

- `cd /mnt/storage/omnigent-working-branch`; record `git rev-parse HEAD`,
  `git branch --show-current`, and `git status --short` (must be empty).
- Confirm both units are active, `curl -fsS http://127.0.0.1:6767/health` is ok, and
  `cat ~/.omnigent/update-state.json` shows what is installed.
- `df -h /mnt/storage`: the updater copies the ~11 GB database when a migration is
  pending, so require at least 25 GB free. If less, delete the oldest
  `~/.omnigent/backups/chat-before-update-*.db` copies and say which.
- `git fetch origin main`; report how many commits `HEAD` is behind.
- If nothing is behind and the database revision equals the single Alembic head,
  report "already current" and stop without reinstalling.

## Phase 2 - Rebase onto origin/main

- `git branch backup/pre-rebase-<new-main-sha> HEAD` first, so the old stack stays
  reachable.
- `git rebase --onto origin/main <old-base-sha> working-branch`. Resolve each
  conflict inside the commit that owns it, preferring upstream behaviour and
  re-applying only the private feature that commit is about. Typical conflict
  sites: the scheduled-task entity/routes/store/fire path and its tool schema,
  `_sessions` helpers and orchestration, session create validation, the composer
  components, `PullRequestPanel`, `github_resource`, `openapi.json`, `uv.lock`,
  `AGENTS.md`, `feature-map/session-orchestration.md`.
- Merge duplicate definitions rather than picking a side: repeated keyword
  arguments (`project_store`, `file_store`), duplicated model fields, duplicate
  imports, and orphaned markers are the usual damage. `python -m compileall -q
  omnigent` and `ruff check omnigent tests` must be clean before continuing.
- If `uv run --no-sync alembic -c omnigent/db/alembic.ini heads` lists more than
  one head, run `python scripts/merge_migration_heads.py` and commit that merge
  revision into the database-schema commit (`fixup!` + `git rebase -i
  --autosquash origin/main`). The migration test derives the head from the graph,
  so it needs no edit.
- If `python scripts/dump_openapi.py --check` reports drift, regenerate and fold
  the change into the commit that owns the routes.

## Phase 3 - Validate

- `export NODE_OPTIONS=--max-old-space-size=3072`.
- `cd web && node_modules/.bin/tsc -b` (must be clean).
- `python scripts/dump_openapi.py --check` (must be clean).
- `pre-commit run --files <changed files>` for the files each commit touches; run
  `pre-commit run --all-files` when the rebase touched shared code. Never pass
  `--no-verify` on a commit.
- Targeted Python: `tests/db`, `tests/server/scheduled`, the scheduled-task route
  and store suites, `tests/tools/test_manager.py`, `tests/runner/test_github_resource.py`,
  `tests/runner/test_session_pr_resources.py`, `tests/server/integration/test_inbox_notifications.py`.
- Targeted web: `vitest run` over the composer, scheduled, inbox, sidebar, and
  pull-request panel files touched by this update.
- Known baselines, do not chase: `tests/db/test_utils.py` and the preferences
  migration tests fail with a missing optional `psycopg` and with multi-head
  errors caused by the private merge revision; they fail identically on the
  pre-update tip. Vitest timeouts at 5 s under memory pressure are environmental:
  re-run the affected file with `--testTimeout=30000`.
- Anything else that fails is a regression: stop before installing.

## Phase 4 - Install

- Queue the sanctioned updater and read its log from journald, not from a status
  file:
  `systemd-run --user --unit=omnigent-update-<date> --property=WorkingDirectory=/mnt/storage/omnigent-working-branch --property=Environment=PATH=/home/vscunha/.local/bin:/usr/local/bin:/usr/bin:/bin --property=Environment=UV_DEFAULT_INDEX=https://pypi.org/simple --setenv=HOME=/home/vscunha /bin/bash -lc './update.sh'`
  then `journalctl --user -u omnigent-update-<date> --since "<start>"`.
  Success is the line `Services updated successfully.`.
- The updater now refuses to start when the database copy will not fit, checks the
  database revision right after the server restart, prunes superseded backups, and
  writes `~/.omnigent/update-state.json`. Trust those gates; do not work around
  them.
- If it fails before restarting the services, fix the cause and rerun; the script
  is idempotent.

## Phase 5 - Verify and publish

- Both units active, `/health` ok, `~/.omnigent/update-state.json` shows the new
  commit, and the database revision equals the single code head.
- Push the rebased branch to the fork with a lease:
  `git push --force-with-lease=<old-tip> fork HEAD:refs/heads/working-branch`.
  The reference-policy pre-push hook scans the whole pushed range, which after a
  rebase includes upstream's own commits. Confirm the private delta is clean with
  `python dev/lint/lint_reference_policy.py --base origin/main --head HEAD`; only
  then may that single push use `--no-verify`, and the report must say so.
- Report concisely: old -> new upstream revision, commits behind, conflict
  decisions, merge revision added, test counts, install result, database revision,
  backup created and pruned, push result, and anything unverified. State plainly
  if the update is incomplete.

## Stop and report instead of proceeding if

- the working tree cannot be made clean without discarding work;
- more than one Alembic head remains after the merge helper;
- finishing would require editing an applied migration or stamping the version
  table;
- free space cannot accommodate the database copy;
- an unexplained test failure remains, or a fix would need a private cross-session
  authority or a weakened native security check.
```
