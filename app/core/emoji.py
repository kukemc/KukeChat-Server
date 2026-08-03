from __future__ import annotations

import unicodedata

from fastapi import HTTPException, status

EMOJI_MAX_CODEPOINTS = 16
EMOJI_JOINERS = {"\u200d"}
EMOJI_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
EMOJI_KEYCAP = "\u20e3"
EMOJI_KEYCAP_BASES = set("0123456789#*")


def validate_reaction_emoji(emoji: str) -> str:
    normalized = emoji.strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Emoji is required")
    if len(normalized) > EMOJI_MAX_CODEPOINTS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Emoji is too long")
    if normalized.endswith(EMOJI_KEYCAP):
        base = normalized[:-1].replace("\ufe0f", "")
        if len(base) == 1 and base in EMOJI_KEYCAP_BASES:
            return normalized
    has_symbol = False
    for char in normalized:
        if char in EMOJI_JOINERS or char in EMOJI_VARIATION_SELECTORS:
            continue
        category = unicodedata.category(char)
        if category in {"So", "Sk"}:
            has_symbol = True
            continue
        if category == "Mn" and char == EMOJI_KEYCAP:
            continue
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Only emoji reactions are allowed")
    if not has_symbol:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Only emoji reactions are allowed")
    return normalized
