# News module — agent instructions

Follow the repository rules in [../../AGENTS.md](../../AGENTS.md) first; this file only
adds what is specific to the `news` module.

## Purpose

News for everyone in IT-Ops: **app updates** (what changed in IT-Ops: release notes) and
**IT announcements** (news from the IT department: maintenance, changes, reminders). A
feed, newest first, with pinned posts on top. It is not documentation (that's Docs) and
doesn't send email or notifications (there's no notify module yet).

## Contract (keep in sync with `src/itops_news/manifest.toml`)

- **Permissions:** `news.read` (everyone with a role), `news.write` (draft posts, change
  drafts), `news.publish` (**human-only**: publish, change published posts, pin, archive,
  restore, delete drafts)
- **Events published:** `news.post_published`
- **Services provided:** `news.waiting` (core's *Waiting for you*)
- **MCP tools:** `news_list_posts`, `news_get_post` (read); `news_draft_post`,
  `news_update_draft` (`news.write`; drafts only)
- **Depends on:** core only

## Module-specific notes

- **One row per post** (`news.posts`), no version history: a draft is edited in place
  until published; after that only a person with `news.publish` changes it (a correction
  goes live at once, so the service refuses AI and writers). Every change is audited.
- Categories are fixed in `service.CATEGORIES` (`app`, `it`; a CHECK constraint in the
  migration matches). Adding one means both, plus a tone in `views.CATEGORY_TONE`.
- Drafts are visible only to people who can write news (the feed's Drafts card, the post
  page answers 404 to others). Published posts are in search; archived ones stay in,
  flagged, and leave the feed (`/news/archive` lists them).
- Content is the core block format; images go through the core file store
  (`/api/news/files`) and are claimed by the post.
- **Release notes:** when a release ships, an AI can draft the "app update" with
  `news_draft_post(category="app")`; a person publishes it.
- Code layout: `app.py` (the Module object), `models.py`, `service.py`, `views.py`,
  `tools.py`.
