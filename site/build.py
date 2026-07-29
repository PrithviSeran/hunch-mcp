#!/usr/bin/env python3
"""Render the blog Markdown in ../bench/blog into the static site.

Single source of truth: bench/blog/*.md (front matter + Markdown, with the
charts/architecture embedded as raw HTML). Run after editing a post:

    pip install markdown        # once
    python3 site/build.py

Regenerates site/blogs.html and site/blog/<slug>.html. index.html (the landing
page) is hand-authored and left untouched.
"""
import os, re, html as _html
import markdown as md

SITE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(SITE)
CONTENT = os.path.join(SITE, "content")

# order + display date; slug is the filename (front-matter date is a placeholder)
POSTS = [
    {"file": "do-macos-agents-hijack-your-screen.md",     "date": "July 2026"},
    {"file": "why-concurrency-didnt-speed-up-ax-reads.md", "date": "July 2026"},
]

def nav(active):
    h = ' aria-current="page"' if active == "home" else ''
    b = ' aria-current="page"' if active == "blogs" else ''
    return f'''<nav>
  <div class="nav-inner">
    <a class="brand" href="/"><img class="brand-logo" src="/assets/logo-mark.png" alt=""><span>Hunch</span></a>
    <div class="nav-links">
      <a href="/"{h}>Home</a>
      <a href="/blogs"{b}>Blogs</a>
    </div>
    <button class="toggle" id="themeBtn" aria-label="Toggle color theme">
      <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>
      <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.2 4.2l1.5 1.5M18.3 18.3l1.5 1.5M2 12h2M20 12h2M4.2 19.8l1.5-1.5M18.3 5.7l1.5-1.5"/></svg>
    </button>
  </div>
</nav>'''

FOOTER = '''<footer><div class="wrap"><div class="foot-inner">
  <span class="foot-brand"><img class="foot-logo" src="/assets/logo-mark.png" alt="">Hunch · focus-free computer use for macOS.</span>
  <span><a href="https://github.com/PrithviSeran/hunch-mcp" target="_blank" rel="noopener">GitHub</a> · <a href="/blogs">Blogs</a></span>
</div></div></footer>'''

def page(title, desc, ogtype, active, content):
    t = _html.escape(title, quote=True); d = _html.escape(desc, quote=True)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:type" content="{ogtype}">
<meta property="og:image" content="/assets/logo-app.png">
<meta name="twitter:card" content="summary">
<link rel="icon" href="/assets/favicon.png">
<link rel="stylesheet" href="/styles.css">
<script>(function(){{var r=document.documentElement;try{{if(localStorage.getItem('hunch-theme')==='dark')r.setAttribute('data-theme','dark');}}catch(e){{}}}})();</script>
</head>
<body>
{nav(active)}
<main>
{content}
</main>
{FOOTER}
<script src="/theme.js"></script>
</body>
</html>
'''

def parse_front_matter(text):
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    fm, body = m.group(1), text[m.end():]
    def field(k):
        mm = re.search(r'^' + k + r':\s*"(.*?)"\s*$', fm, re.M)
        return mm.group(1) if mm else ""
    return {"title": field("title"), "description": field("description")}, body

def render_markdown(body):
    body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
    raws = []
    def stash(m):
        raws.append(m.group(0)); return "\n\n@@RAW%d@@\n\n" % (len(raws) - 1)
    body = re.sub(r'<figure[^>]*>.*?</figure>', stash, body, flags=re.S)
    body = re.sub(r'<div class="arch">.*?<div class="cap">.*?</div>\s*</div>', stash, body, flags=re.S)
    out = md.markdown(body, extensions=["tables", "fenced_code", "sane_lists"])
    for i, raw in enumerate(raws):
        out = out.replace("<p>@@RAW%d@@</p>" % i, raw).replace("@@RAW%d@@" % i, raw)
    out = out.replace("<table>", '<div class="tablewrap"><table>').replace("</table>", "</table></div>")
    return out

def build():
    metas = []
    for post in POSTS:
        post["slug"] = os.path.splitext(post["file"])[0]
        raw = open(os.path.join(CONTENT, post["file"]), encoding="utf-8").read()
        fm, body = parse_front_matter(raw)
        metas.append({**post, **fm})
        article = ('<div class="wrap"><article><div class="art-body">\n'
                   '<a class="art-back" href="/blogs">← Blogs</a>\n'
                   f'<h1>{_html.escape(fm["title"])}</h1>\n'
                   f'<p class="art-date">{post["date"]}</p>\n'
                   + render_markdown(body) +
                   '\n<a class="art-back" style="margin:44px 0 0" href="/blogs">← Blogs</a>\n'
                   '</div></article></div>')
        open(os.path.join(SITE, "blog", post["slug"] + ".html"), "w", encoding="utf-8").write(
            page(f'{fm["title"]} | Hunch', fm["description"], "article", "blogs", article))
        print("  wrote blog/%s.html" % post["slug"])
    rows = "\n".join(
        f'''      <a class="post" href="/blog/{m["slug"]}">
        <h3>{_html.escape(m["title"])}</h3>
        <p class="date">{m["date"]}</p>
        <p class="desc">{_html.escape(m["description"])}</p>
      </a>''' for m in metas)
    listing = f'''  <div class="wrap">
    <div class="page-head"><h1>Blogs</h1></div>
    <div class="post-list">
{rows}
    </div>
  </div>'''
    open(os.path.join(SITE, "blogs.html"), "w", encoding="utf-8").write(
        page("Blog | Hunch",
             "Engineering write-ups from building Hunch: focus-free macOS computer use, the accessibility tree, and benchmarking screen disturbance.",
             "website", "blogs", listing))
    print("  wrote blogs.html")

if __name__ == "__main__":
    build(); print("done.")
