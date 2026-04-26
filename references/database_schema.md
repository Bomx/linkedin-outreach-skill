# Local Database Schema

The default data directory is `~/.linkedin-outreach`. Override it with `LINKEDIN_OUTREACH_DATA_DIR` or `--data-dir`.

## db/leads.json

```json
{
  "version": 1,
  "updated_at": "2026-04-25T12:00:00Z",
  "leads": [
    {
      "id": "li_ab12cd34ef56",
      "profile_url": "https://www.linkedin.com/in/example/",
      "profile_handle": "example",
      "full_name": "Example Person",
      "first_name": "Example",
      "headline": "Founder at ExampleCo",
      "location": null,
      "lead_score": 0.75,
      "status": "spotted",
      "connection_status": "not_sent",
      "messaging_status": "not_started",
      "tags": [],
      "notes": "",
      "source": {
        "type": "post_intent",
        "label": "SEO buyer intent post",
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:...",
        "match_text": "Short evidence snippet",
        "signal_name": "seo-buyer-intent-posts",
        "target_role": "original_poster",
        "search_query": "looking for SEO consultant"
      },
      "source_history": [
        {
          "type": "activity_engagement",
          "label": "Engaged with competitor CEO",
          "url": "https://www.linkedin.com/feed/update/urn:li:activity:...",
          "signal_name": "competitor-ceo-engagers",
          "target_role": "comment_engager",
          "target_url": "https://www.linkedin.com/in/example/recent-activity/all/"
        }
      ],
      "last_connection_note": null,
      "connected_at": null,
      "last_dm": null,
      "created_at": "2026-04-25T12:00:00Z",
      "updated_at": "2026-04-25T12:00:00Z",
      "last_seen_at": "2026-04-25T12:00:00Z"
    }
  ]
}
```

## Signal Source Types

- `post_intent`: the lead authored a post that matched an intent/keyword query. Usually target role `original_poster`.
- `activity_engagement`: the lead engaged with a watched person/company activity feed. Usually target role `comment_engager`.
- `comment_keyword`: the lead authored a comment matching tracked keywords. Usually target role `comment_author`.
- `post_comment`: the lead commented on one explicit watched post.

Intent labels:

- `buyer_intent`: post/comment appears to express demand for a person, tool, vendor, solution, or recommendation.
- `hiring_role`: post appears to be hiring for a role rather than buying a service.
- `provider_promo`: post/comment appears to be a provider promoting their own service.
- `discussion`: relevant discussion without a direct buying phrase.

## Lead Status Fields

`status` tracks review state:

- `spotted`: discovered but not reviewed.
- `queued`: approved for an outbound action.
- `rejected`: intentionally skipped.
- `contacted`: at least one outbound action has been attempted.

`connection_status` tracks the connection workflow:

- `not_sent`
- `queued`
- `sent`
- `pending`
- `connected`
- `failed`
- `skipped`

`messaging_status` tracks DMs:

- `not_started`
- `initial_sent`
- `responded`
- `failed`
- `skipped`

## db/actions.jsonl

Append-only log. One JSON object per line:

```json
{"timestamp":"2026-04-25T12:00:00Z","type":"connection_sent","lead_id":"li_ab12cd34ef56","profile_url":"https://www.linkedin.com/in/example/","details":{"note_preview":"Hi Example..."}}
```

Common action types:

- `lead_spotted`
- `lead_updated`
- `lead_queued`
- `lead_rejected`
- `connection_dry_run`
- `connection_sent`
- `connection_status_synced`
- `connection_failed`
- `dm_dry_run`
- `dm_sent`
- `dm_failed`

## db/signals.json

Reusable radar definitions:

```json
{
  "version": 1,
  "updated_at": "2026-04-25T12:00:00Z",
  "signals": [
    {
      "name": "competitor-ceo-cyriac-lefort-engagers",
      "type": "activity",
      "enabled": true,
      "source_label": "Engaged with Cyriac Lefort",
      "target_url": "https://www.linkedin.com/in/cyriaclefort/",
      "query": null,
      "keywords": [],
      "intent": null,
      "limit": 10,
      "max_posts": 3,
      "scrolls": 5,
      "expand_rounds": 3,
      "created_at": "2026-04-25T12:00:00Z",
      "updated_at": "2026-04-25T12:00:00Z"
    },
    {
      "name": "seo-buyer-intent-posts",
      "type": "post_intent",
      "enabled": true,
      "source_label": "SEO buyer intent post",
      "target_url": null,
      "query": "looking for SEO consultant",
      "keywords": [],
      "intent": "buyer_intent",
      "limit": 10,
      "max_posts": 5,
      "scrolls": 4,
      "expand_rounds": 4,
      "created_at": "2026-04-25T12:00:00Z",
      "updated_at": "2026-04-25T12:00:00Z"
    }
  ]
}
```

Signal types:

- `activity`: run `scout-activity`.
- `comment_keyword`: run `scout-comment-keyword`.
- `post_intent`: run `scout-search` with optional intent filtering.

## db/templates.json

Editable templates. Connection requests send without notes by default; `connection_note`
is kept blank so notes are only sent when a final one-off custom message is passed with
`--message` for a single lead.

```json
{
  "connection_note": "",
  "initial_dm": "Hi {first_name}, thanks for connecting. I noticed {source_label} and thought it could be useful to compare notes."
}
```

Supported DM placeholders:

- `{first_name}`
- `{full_name}`
- `{headline}`
- `{source_label}`
- `{profile_url}`
