# LinkedIn Local Outreach Skill

<p align="center">
  <b>Built by the team at <a href="https://distribb.io">Distribb</a> - SEO and backlinks autopilot for operators.</b><br/>
  <a href="https://distribb.io"><b>-> Try Distribb free at distribb.io</b></a>
</p>

---

> A local-first LinkedIn outreach skill for AI agents. Scout warm leads
> from LinkedIn activity, keep a local review database, send low-volume
> connection requests from your own visible browser session, sync accepted
> connections, and send first-degree DMs only after explicit approval.

This skill is designed for a user's own LinkedIn account on their own Mac
or workstation. It does **not** ship proxies, rented accounts, credential
login, CAPTCHA bypasses, stealth patches, or any shared database.

## Install As A Claude Code Skill

```bash
npx skills add Bomx/linkedin-outreach-skill
```

Or clone manually:

```bash
git clone https://github.com/Bomx/linkedin-outreach-skill.git
cd linkedin-outreach-skill
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

Claude reads `SKILL.md` for the actual workflow.

## What This Repo Includes

| File | Purpose |
|---|---|
| `SKILL.md` | Agent playbook: operating rules, commands, and workflows. |
| `scripts/linkedin_outreach.py` | Local CLI for login, scouting, review, connection requests, sync, DMs, status, and export. |
| `requirements.txt` | Python dependencies. |
| `references/operating_rules.md` | Safety rules and stop conditions. |
| `references/database_schema.md` | Local JSON database schema. |
| `agents/openai.yaml` | Skill UI metadata. |

## What This Repo Does Not Include

Your local data is intentionally excluded:

- no LinkedIn cookies or browser profiles
- no `storage_state.json`
- no `leads.json`
- no `signals.json`
- no action logs, run logs, screenshots, or exports
- no personal templates or message history

By default, runtime state is stored outside the repo at:

```text
~/.linkedin-outreach/
```

You can override that location:

```bash
export LINKEDIN_OUTREACH_DATA_DIR=/absolute/path/to/linkedin-outreach-data
```

The `.gitignore` blocks common local-state paths as a second layer of
protection, but the safest pattern is still: never put session data or
lead databases inside a public repo.

## Setup

### 1. Login Locally

```bash
python3 scripts/linkedin_outreach.py login --timeout 600
```

A visible Chromium window opens. Sign into LinkedIn manually. The script
saves the local browser profile under `~/.linkedin-outreach/session/`.

### 2. Check The Environment

```bash
python3 scripts/linkedin_outreach.py doctor
python3 scripts/linkedin_outreach.py status
```

### 3. Add Reusable Signals

High-intent SEO buyer posts:

```bash
python3 scripts/linkedin_outreach.py signal-add \
  --name "seo-buyer-intent-posts" \
  --type post_intent \
  --query "looking for SEO consultant" \
  --intent buyer_intent \
  --source-label "SEO buyer intent post"
```

Active SEO agency owners:

```bash
python3 scripts/linkedin_outreach.py signal-add \
  --name "active-seo-agency-owners" \
  --type post_intent \
  --query "SEO agency founder" \
  --source-label "Active SEO agency owner" \
  --limit 50 \
  --scrolls 6
```

Warm competitor/person engagement:

```bash
python3 scripts/linkedin_outreach.py signal-add \
  --name "competitor-engagers" \
  --type activity \
  --target-url "https://www.linkedin.com/in/example/" \
  --source-label "Engaged with watched profile" \
  --limit 30
```

## Typical Workflow

Scout without sending:

```bash
python3 scripts/linkedin_outreach.py scout-search \
  --query "SEO agency founder" \
  --source-label "Active SEO agency owner" \
  --signal-name "active-seo-agency-owners" \
  --limit 50 \
  --scrolls 6 \
  --no-connect
```

Review leads:

```bash
python3 scripts/linkedin_outreach.py review --list --limit 30
python3 scripts/linkedin_outreach.py review --queue li_abc123 li_def456
```

Dry-run queued connection requests:

```bash
python3 scripts/linkedin_outreach.py connect --queued --limit 3
```

Actually send only after explicit approval:

```bash
python3 scripts/linkedin_outreach.py connect \
  --queued \
  --limit 3 \
  --execute \
  --no-note \
  --navigation-mode random \
  --min-delay 45 \
  --max-delay 90
```

Before DMs, sync accepted connections:

```bash
python3 scripts/linkedin_outreach.py sync-connections --contacted --limit 20
```

Send one first-degree DM:

```bash
python3 scripts/linkedin_outreach.py dm \
  --lead-id li_abc123 \
  --message "Hi {first_name}, thanks for connecting. Thought this might be useful..." \
  --execute
```

## Safety Defaults

- Outbound commands default to dry-run.
- Connection requests send without a note by default.
- Custom connection notes are allowed only for exactly one selected lead.
- The browser is visible for login and runs locally.
- The script stops on LinkedIn checkpoints, CAPTCHA, restrictions, or unusual security prompts.
- Already `sent`, `pending`, `connected`, or `skipped` leads are not reopened for duplicate connection requests.
- `sync-connections` should run before DMs so accepted requests are marked `connected`.

## CLI Reference

```bash
python3 scripts/linkedin_outreach.py --help
python3 scripts/linkedin_outreach.py scout-search --help
python3 scripts/linkedin_outreach.py connect --help
python3 scripts/linkedin_outreach.py dm --help
```

Every command emits one final machine-readable line:

```text
RESULT: {"status": "ok", ...}
```

Agents should treat the `RESULT:` line as the source of truth.

## Privacy And Security

- **No shared database.** Leads, actions, templates, and signals live on
  the user's machine.
- **No cookies in Git.** LinkedIn sessions are saved under
  `~/.linkedin-outreach/session/`, outside this repo by default.
- **No proxy or stealth system.** This is deliberate local automation for
  normal, low-volume personal-account workflows.
- **No telemetry.** The script writes local JSON files and drives the
  user's visible browser session.

## Want The Rest Of Your SEO Engine Automated?

This skill helps with LinkedIn lead scouting and outreach. **Distribb**
handles the broader SEO system:

- keyword research with buyer-intent signals
- content publishing to WordPress, Webflow, and Shopify
- original-data research pages
- internal linking and content repurposing
- backlink exchange through a high-DR partner network

**[-> Try Distribb free at distribb.io](https://distribb.io)**

Built by [Borja Obeso](https://www.linkedin.com/in/borja-obeso/), founder of
[Distribb](https://distribb.io).
