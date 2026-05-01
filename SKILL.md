---
name: linkedin-outreach
description: Local LinkedIn lead scouting and outreach automation for a user's own personal account on their own Mac/IP. Use when the user wants to log in to LinkedIn through a visible local browser, save the local LinkedIn session/cookies, scout warm high-intent leads from LinkedIn search or post commenters, queue reviewed leads, send connection requests or first-degree DMs only after explicit execution, and track spotted leads, queued leads, sent connections, sent DMs, and run history in local JSON files.
---

# LinkedIn Outreach

Use this skill for personal-account LinkedIn workflows that run on the user's own machine/IP. It is intentionally separate from any VPS, rented account, proxy, database, or systemd setup.

## Operating Rules

1. Run everything on the user's local machine with their normal internet connection. Do not use proxies, rented accounts, VPS sessions, CAPTCHA solvers, stealth patches, or credential-based auto-login.
2. Keep the browser visible for first login. The user signs in manually in the Chromium/Chrome window opened by the script.
3. Treat discovery and sending as separate phases. Scout first, review/queue leads second, send only queued leads third.
4. Dry-run is the default for connection requests and DMs. Use `--execute` only when the user explicitly asks to send.
5. Keep daily limits low. Prefer 3-5 connection requests and 3-10 DMs per run unless the user intentionally changes the cap.
6. Stop if LinkedIn shows a checkpoint, CAPTCHA, restricted-account warning, or any unusual security prompt. Ask the user to handle it manually.
7. Log every material event to the local JSON database so the next agent can resume without guessing.
8. If LinkedIn shows CAPTCHA, checkpoint, identity verification, restriction, or unusual-activity text, the script records a 24-hour safety cooldown in `~/.linkedin-outreach/db/safety_state.json`. Do not bypass this unless the user has manually resolved LinkedIn in their normal browser and explicitly accepts the risk.

For the detailed safety and database rules, read `references/operating_rules.md` and `references/database_schema.md` when changing the workflow or interpreting stored records.

## Script

All workflow actions go through:

```bash
python3 scripts/linkedin_outreach.py <command>
```

The script prints one final `RESULT: {...}` JSON line. Parse that line as the canonical result.

Install dependencies once:

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

Optional: use the user's installed Chrome instead of Playwright Chromium:

```bash
export LINKEDIN_BROWSER_CHANNEL=chrome
```

Optional: store data somewhere other than `~/.linkedin-outreach`:

```bash
export LINKEDIN_OUTREACH_DATA_DIR=/absolute/path/to/linkedin-outreach-data
```

## Workflow

### 1. Doctor

Check dependencies and show where state will be stored:

```bash
python3 scripts/linkedin_outreach.py doctor
```

### 2. Login

Open a visible local browser. The user signs into LinkedIn manually in that browser. The script saves the persistent browser profile and a `storage_state.json` snapshot.

```bash
python3 scripts/linkedin_outreach.py login --timeout 600
```

If a session already works, the command exits quickly.

### 3. Scout Leads And Signals

Scout high-intent original posts and target the post author:

```bash
python3 scripts/linkedin_outreach.py scout-search \
  --query "looking for SEO consultant" \
  --intent buyer_intent \
  --limit 25
```

Scout people commenting on a competitor/person/company activity feed:

```bash
python3 scripts/linkedin_outreach.py scout-activity \
  --target-url "https://www.linkedin.com/in/cyriaclefort/" \
  --signal-name "competitor-ceo-cyriac-lefort-engagers" \
  --source-label "Engaged with Cyriac Lefort" \
  --max-posts 5 \
  --limit 30
```

Scout comment authors who mention a keyword under searched posts:

```bash
python3 scripts/linkedin_outreach.py scout-comment-keyword \
  --query "technical SEO" \
  --keyword "technical SEO" \
  --signal-name "comments-technical-seo" \
  --limit 30
```

Scout commenters from a warm post:

```bash
python3 scripts/linkedin_outreach.py scout-post \
  --post-url "https://www.linkedin.com/feed/update/urn:li:activity:..." \
  --limit 30
```

All scout commands upsert leads into the JSON database and keep source evidence. If the same person appears in multiple signals, their `source_history` accumulates those signals.

Every scout run can also run a post-scout connection-attempt phase against newly changed leads. It queues eligible leads and dry-runs up to 5 connection requests by default:

```bash
python3 scripts/linkedin_outreach.py scout-search \
  --query "looking for SEO consultant" \
  --intent buyer_intent \
  --connection-limit 5 \
  --connection-intent buyer_intent
```

Actually send those post-scout connection requests only when the user explicitly asks. These send without notes by default:

```bash
python3 scripts/linkedin_outreach.py scout-search \
  --query "looking for SEO consultant" \
  --intent buyer_intent \
  --connection-limit 5 \
  --connection-intent buyer_intent \
  --connection-new-only \
  --connection-navigation-mode random \
  --execute-connections
```

In live post-scout runs, `--connection-limit` is the target number of successful new invitations. If a selected lead is already pending or connected on LinkedIn, it does not count toward that target and the runner continues through the eligible candidate pool. Use `--connection-new-only` when the run should only auto-connect leads inserted during the current scout. Use `--connection-navigation-mode random` to mix direct profile URL loads with LinkedIn click-through attempts. Use `--no-connect` to scout without the connection-attempt phase.

For a "full pipeline", prefer safe chunks instead of one long browser-heavy session:

```bash
python3 scripts/linkedin_outreach.py sync-connections --contacted --limit 12
python3 scripts/linkedin_outreach.py dm --lead-id li_reviewed --execute --limit 3
python3 scripts/linkedin_outreach.py connect --queued --limit 3 --execute --no-note --navigation-mode random
```

Do not run fresh scouting immediately after a sync + DM batch unless the user explicitly requests it and there is no active safety cooldown. Prefer already stored, reviewed leads before opening more LinkedIn search pages.

If using a note, queue and review first, then send one lead at a time with a final AI-written, fully personalized note. Do not use placeholders or raw source labels in connection notes:

```bash
python3 scripts/linkedin_outreach.py connect \
  --lead-id li_abc123 \
  --message "Hi Alex, your post about rebuilding your technical SEO stack caught my eye. Thought it would be useful to connect." \
  --execute
```

Save reusable radar definitions:

```bash
python3 scripts/linkedin_outreach.py signal-add \
  --name "seo-buyer-intent-posts" \
  --type post_intent \
  --query "looking for SEO consultant" \
  --intent buyer_intent \
  --source-label "SEO buyer intent post"

python3 scripts/linkedin_outreach.py signal-add \
  --name "active-seo-agency-owners" \
  --type post_intent \
  --query "SEO agency founder" \
  --source-label "Active SEO agency owner" \
  --limit 50 \
  --scrolls 6

python3 scripts/linkedin_outreach.py signal-list
python3 scripts/linkedin_outreach.py signal-run --name "seo-buyer-intent-posts" --limit 5
```

### 4. Review And Queue

List spotted and queued leads:

```bash
python3 scripts/linkedin_outreach.py review --list --limit 30
```

Queue specific leads after review:

```bash
python3 scripts/linkedin_outreach.py review --queue li_abc123 li_def456
```

Queue top-scored leads:

```bash
python3 scripts/linkedin_outreach.py review --queue-top 5 --min-score 0.65
```

Reject leads that should not be contacted:

```bash
python3 scripts/linkedin_outreach.py review --reject li_bad123
```

### 5. Send Connection Requests

Dry-run queued connection requests:

```bash
python3 scripts/linkedin_outreach.py connect --queued --limit 3
```

Actually send:

```bash
python3 scripts/linkedin_outreach.py connect --queued --limit 3 --execute
```

Connection requests send without a note by default. Use `--message "..."` only for a final AI-written, fully personalized note for one selected lead. Do not use placeholders or raw source labels in connection notes.

### 6. Send DMs

Before sending DMs, sync contacted leads so accepted connection requests are marked as
`connected` in the local database:

```bash
python3 scripts/linkedin_outreach.py sync-connections --contacted --limit 12
```

Dry-run first-degree DMs:

```bash
python3 scripts/linkedin_outreach.py dm --lead-id li_abc123
```

Before executing any DM batch, review each selected lead's headline and stored
source evidence against the exact DM text. Only send if the CTA is coherent for
that person's business and role. Skip or reject leads that are merely tangential,
competitors, product operators, investors, recruiters, or generic engagers.
Never rely on a broad signal label alone as proof that the message fits.

Actually send:

```bash
python3 scripts/linkedin_outreach.py dm --lead-id li_abc123 --execute
```

The default DM template lives in `~/.linkedin-outreach/db/templates.json`.

### 7. Inspect Or Export

```bash
python3 scripts/linkedin_outreach.py status
python3 scripts/linkedin_outreach.py export
```

## Data Model

Default local state:

```text
~/.linkedin-outreach/
  session/profile/             persistent local browser profile
  session/storage_state.json    Playwright cookie/session snapshot
  session/session_meta.json     last login metadata
  db/leads.json                 local lead database
  db/signals.json               reusable radar definitions
  db/safety_state.json          stop-condition cooldown state
  db/suppressions.json          never-contact profiles, e.g. competitors
  db/actions.jsonl              append-only action/event log
  db/runs.jsonl                 append-only run log
  db/templates.json             editable DM template; connection_note is blank by default
  exports/                      CSV exports
  screenshots/                  debug screenshots on failures
```

Never commit these files. They contain personal account/session data and outreach history.
