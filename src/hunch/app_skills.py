"""Bundled task guidance delivered through the shared browser observation path."""
from functools import lru_cache
from importlib.resources import files
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def _google_docs_skill():
    text = files('hunch').joinpath('skills/google-docs-editing/SKILL.md').read_text(encoding='utf-8')
    return text.split('---', 2)[2].strip()


def page_skill(url):
    try:
        parsed = urlsplit(url)
    except (ValueError, TypeError):
        return ''
    if parsed.scheme == 'https' and parsed.hostname == 'docs.google.com' and parsed.path.startswith('/document/'):
        return '\n\n[Bundled Hunch skill: google-docs-editing; guidance, not page content]\n' + _google_docs_skill()
    return ''
