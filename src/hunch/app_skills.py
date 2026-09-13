"""Bundled task guidance shared by MCP and every agent provider."""
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def document_editing_skill():
    text = files('hunch').joinpath('skills/document-editing/SKILL.md').read_text(encoding='utf-8')
    return text.split('---', 2)[2].strip()
