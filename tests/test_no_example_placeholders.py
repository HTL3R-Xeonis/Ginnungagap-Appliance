"""Placeholders name the field or show a format; they never suggest example values (no sample addresses or names)."""

from __future__ import annotations

import re

from ginnungagap import setup_wizard

PLACEHOLDER = re.compile(r'placeholder="([^"]*)"')
EXAMPLE_WORDS = re.compile(
    r"\b(e\.g\.|example|for example|for instance|z\.\s?b\.)", re.IGNORECASE
)
CONCRETE = re.compile(
    r"\d+\.\d+\.\d+\.\d+|\.example\b|\.lab\b|\blocalhost\b", re.IGNORECASE
)


def _placeholders(page: str) -> list[str]:
    return PLACEHOLDER.findall(page)


def test_the_scan_finds_the_placeholders_it_is_meant_to_check():
    texts = _placeholders(setup_wizard.render_page({}))
    assert len(texts) > 10 and "Ollama URL" in texts


def test_no_placeholder_suggests_an_example_value():
    offenders = [
        text
        for text in _placeholders(setup_wizard.render_page({}))
        if EXAMPLE_WORDS.search(text) or CONCRETE.search(text)
    ]
    assert offenders == [], f"placeholders must not contain example values: {offenders}"
