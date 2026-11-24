"""What text that code speaks or staff read may not hold: one check for every such text."""

import unicodedata

# Control characters (C0, DEL and C1) and format characters (zero-width ones, the soft hyphen,
# the byte order mark, the bidi overrides, embeddings and isolates): unseen, or able to make
# text read otherwise than it is stored.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf"})


def has_hidden_characters(text: str) -> bool:
    """Whether the text holds a control or format character."""
    return any(unicodedata.category(character) in _HIDDEN_CATEGORIES for character in text)
