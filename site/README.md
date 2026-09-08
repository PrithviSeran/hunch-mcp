# Hunch — website

Static marketing site + engineering blog for Hunch. No framework, no
`node_modules`. Fully self-contained: everything it needs is in this folder.

The landing page is hand-authored HTML; **the blog pages are generated from the
Markdown in `content/`** by `build.py`.

## Structure
- `index.html` — landing page (hand-authored)
- `content/<slug>.md` — blog post source (front matter + Markdown) — the source of truth
- `blogs.html`, `blog/<slug>.html` — generated from `content/`
- `build.py` — renders `content/*.md` → `blogs.html` + `blog/*.html`
- `styles.css` — all styles (light default + `[data-theme="dark"]`)
- `theme.js` — light/dark toggle (persisted in localStorage)
- `assets/` — logos + favicon
- `vercel.json` — `cleanUrls` (drops `.html`) + no trailing slash; `/download` → download form
- `api/download.js` — increments the DMG download counter on R2, then 302s to the file
- `api/stats.js` — returns `{ downloads, updated_at }` (also at `/api/stats`)
- `api/_lib/r2.js` — private shared R2/S3 SigV4 helper (underscore = not an HTTP route)

## Editing / adding a blog post
Posts are the single source of truth in `content/*.md` (YAML front matter +
Markdown, with charts/diagrams embedded as raw HTML). The file name is the URL
slug (`content/my-post.md` → `/blog/my-post`).

    pip install markdown            # once
    # edit or add content/<slug>.md
    python3 site/build.py           # regenerate the HTML
    git add -A && git commit

Front matter used: `title`, `description`. Post order + display date live in the
`POSTS` list at the top of `build.py`; add an entry there for a new post.

The generated HTML is committed so the host needs no build step.

## Deploy to Vercel
1. Push this repo to GitHub (the site lives in the `site/` subdirectory).
2. Vercel → New Project → import the repo.
3. **Root Directory = `site`**, **Framework Preset = Other** (no build command).
4. Deploy. `cleanUrls` serves `/blogs` and `/blog/<slug>` without `.html`.
5. Add your domain under the project's Domains tab.

### Download form and profiles

Every macOS download button points to `/download` (`download.html`). The form requires
name and email and accepts optional LinkedIn, X, GitHub, and other social/website URLs.
`download.js` sends JSON to `POST /api/download`. That endpoint saves the profile via
`hunch-download-api`, then returns the DMG URL. Save failures keep the visitor on the
form and never start the download. `GET`/`HEAD /api/download` redirects to the form.

The Worker lives in `../services/download-profiles/`. It uses the existing
`hunch-profiles` D1 database and **the existing `profiles` table**, with email as its
case-insensitive primary key. Repeat email submissions update the name and supplied
social links; empty social fields preserve stored links. The original app profile API
continues to work with the added columns. No new profile table is created.

The additive migration `0002_profile_socials.sql` was applied on September 8, 2026.
Do not reapply it to a database where those columns already exist. This service shares
a database with the native app's profile service; inspect the schema before migrations.

Deploy the Worker from `services/download-profiles` with `wrangler deploy`, then deploy
this site to the existing Vercel project. No new Vercel secrets are required. Tests:

```bash
node --test services/download-profiles/tests/download.test.mjs
```

The existing R2 aggregate counter now increments after a successful form save rather
than on a GET/HEAD request. Repeat submissions may increment it again; it is not a
unique-profile count. The existing R2 environment variables remain required for counting:
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT`, `R2_BUCKET`.
The total is readable at `https://www.tryhunch.ca/api/stats`.

The stable DMG remains in the `hunch-updates` R2 bucket. Direct file downloads and the
Sparkle appcast are unchanged. The form is a website download flow, not access control
for the publicly available app. Profile email addresses are self-reported, not verified.

Local static preview: `cd site && python3 -m http.server 8000`, then visit
`/download.html`. Submitting requires the Vercel API (or a local Vercel dev server).
