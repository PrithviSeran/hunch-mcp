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
- `vercel.json` — `cleanUrls` (drops `.html`) + no trailing slash

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

Local preview: `cd site && python3 -m http.server 8000` → http://localhost:8000
(the dev server doesn't rewrite clean URLs; click through from the homepage or
hit `.html` paths directly — Vercel handles the rewrite in production).
