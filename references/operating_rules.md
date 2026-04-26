# Operating Rules

This skill is for a user's own LinkedIn account on their own computer and IP address.

## Boundaries

- Do not use VPS sessions, proxies, rented accounts, CAPTCHA solvers, stealth browser patches, or automated credential login.
- Open a visible local browser for first login and let the user complete any security prompts manually.
- Do not bypass LinkedIn checkpoints, identity prompts, rate limits, warnings, or account restrictions.
- Default to dry-run for outbound actions. Use `--execute` only after explicit user approval.
- Keep each run small enough to look like deliberate manual work. The default caps are intentionally low.
- Treat `--connection-limit` in live post-scout runs as a successful-send target; already pending, connected, cached, or failed attempts should not count as new invitations.
- Prefer relevant, personalized messages over generic mass outreach.

## Recommended Flow

1. Run `doctor`.
2. Run `login` and let the user sign in manually.
3. Run `scout-search` or `scout-post`.
4. Run `review --list`.
5. Queue only leads the user would reasonably contact manually.
6. Run `connect` or `dm` without `--execute` first.
7. Before sending DMs, run `sync-connections --contacted` so accepted requests are marked `connected`.
8. Run the same command with `--execute` only after reviewing the dry-run output.
9. Check `status` and keep the JSON database as the source of truth.

## Stop Conditions

Stop the automation and ask the user to inspect the browser if any of these happen:

- LinkedIn shows a CAPTCHA, checkpoint, identity verification, account restriction, or unusual login prompt.
- The account appears logged out unexpectedly.
- The UI asks for information that should only be provided by the account owner.
- A selector starts clicking the wrong element or sends unexpected text.

## Message Guidance

- Send connection requests without notes by default.
- Use a connection note only when the operator or AI has written a final, one-off, personalized note for that exact lead.
- Keep connection notes under LinkedIn's 300-character limit.
- Use the lead source as context only when it can be phrased naturally; never insert raw internal labels.
- Avoid claims of prior relationship unless the user actually has one.
- Avoid sending the same exact DM to many people. Edit `db/templates.json` between campaigns when needed.
