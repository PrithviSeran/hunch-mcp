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
- `vercel.json` — `cleanUrls` (drops `.html`) + no trailing slash; `/download` → API
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

### DMG download tracking
The hero **Download for macOS** button hits `/download`, which counts the hit in
`stats/dmg.json` on the `hunch-updates` R2 bucket, then redirects to
`https://pub-8748b4003e764f8a888e32c8e2ce7057.r2.dev/Hunch.dmg`.

Read the running total anytime:
```
curl https://www.tryhunch.ca/api/stats
# → {"downloads":12,"updated_at":"2026-…"}
```

For the counter to increment, set these **Production** env vars in the Vercel
project (same values as the local `rclone` `r2` remote), then redeploy:

- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_ENDPOINT` — `https://<accountid>.r2.cloudflarestorage.com`
- `R2_BUCKET` — `hunch-updates`

Optional: `HUNCH_DMG_URL` overrides the public DMG URL.

Without the credentials the redirect still works; only the count write is skipped.
Until they're set, `/api/stats` still reads the public `stats/dmg.json` on R2.
Re-upload a new DMG over the stable key when you cut a release:
```
rclone copyto dist/Hunch.dmg r2:hunch-updates/Hunch.dmg --s3-no-check-bucket
```

Local preview: `cd site && python3 -m http.server 8000` → http://localhost:8000
(the dev server doesn't rewrite clean URLs; click through from the homepage or
hit `.html` paths directly — Vercel handles the rewrite in production).
