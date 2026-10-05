"""Text normalization shared by the index builder (lyrics/build_db.py) and the app.

Both sides must normalize identically, or searches silently stop matching.
"""

import re
import unicodedata


def words(text):
    """Lowercase, accents and punctuation removed, split into words.

    'Mi héroe es "La Gran Bestia Pop"' -> ['mi', 'heroe', 'es', 'la', 'gran', 'bestia', 'pop']
    'sueños' -> ['suenos'],  'Führer' -> ['fuhrer'],  'to-to-todo' -> ['to', 'to', 'todo']
    """
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", text)
