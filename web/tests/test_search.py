"""Search behaviour (web/search.py, web/textnorm.py) on the made-up songs of fixture.py."""

import pytest

import textnorm
from fixture import rows
from search import Index, levenshtein, sounds


@pytest.fixture(scope="module")
def ix():
    return Index(rows())


def texts(ix, hits):
    return [ix.line_text(h) for h in hits]


# --- normalization

@pytest.mark.parametrize("text, words", [
    ('Mi héroe es "La Gran Bestia"', ["mi", "heroe", "es", "la", "gran", "bestia"]),
    ("sueños", ["suenos"]),
    ("¡Pingüino!", ["pinguino"]),
    ("to-to-todo", ["to", "to", "todo"]),
    ("  ¿?  ", []),
])
def test_words(text, words):
    assert textnorm.words(text) == words


def test_levenshtein_stops_at_limit():
    assert levenshtein("brillar", "brilar") == 1
    assert levenshtein("brillar", "tambor", limit=2) == 3     # limit + 1: "more than 2"


def test_sound_alikes_share_a_key():
    assert sounds("vestia") & sounds("bestia")
    assert sounds("eroe") & sounds("heroe")
    assert sounds("brilar") & sounds("brillar")
    assert not sounds("luna") & sounds("cuna")


# --- finding phrases

def test_exact_phrase(ix):
    hits = ix.search("vamos a brillar esta noche")
    assert len(hits) == 1                              # one card per song...
    assert hits[0].kind == "exact"
    assert hits[0].song == "cancion_de_prueba"
    assert len(hits[0].others) == 1                    # ...the chorus is sung twice
    assert hits[0].start == 5.0                        # first word of the first line


def test_accents_and_punctuation_ignored(ix):
    hit = ix.search("el pinguino baila y suena")[0]
    assert hit.kind == "exact"
    assert "pingüino" in ix.line_text(hit)


def test_hyphenated_word(ix):
    assert ix.search("todo vuelve a empezar")[0].kind == "exact"


def test_typo(ix):
    hits = ix.search("tanbor")
    assert hits and hits[0].kind == "approx"
    assert "tambor" in ix.line_text(hits[0])


def test_spanish_sound_alike(ix):
    # v/b and a missing double l: 2 edits, more than a 6-letter word may have as typos,
    # so only the sound-alike rule finds it (find(): no closest-line fallback)
    hits = ix.find("vrilar", prefix=False)
    assert {h.song for h in hits} == {"cancion_de_prueba", "otra_cancion"}
    assert all(h.cost == 0.5 for h in hits)
    hit = ix.search("vamos a vrilar")[0]
    assert hit.kind == "approx" and hit.song == "cancion_de_prueba"


def test_last_word_completed(ix):
    hit = ix.search("la luna sa")[0]
    assert hit.kind == "partial"
    assert ix.line_text(hit) == "La luna sale sobre el puerto"


def test_one_letter_completion_needs_earlier_words(ix):
    assert ix.search("sobre el p")[0].kind == "partial"
    assert ix.find("p") == []                          # alone, one letter completes nothing


def test_short_words_must_be_exact(ix):
    assert ix.find("le", prefix=False) == []           # not "la", "el"…


def test_closest_lines_when_nothing_matches(ix):
    hits = ix.search("puerto de la luna")              # not sung like that
    assert hits and hits[0].kind == "approx"
    assert ix.line_text(hits[0]) == "La luna sale sobre el puerto"


def test_nonsense_finds_nothing(ix):
    assert ix.search("xyzzy qwerty") == []
    assert ix.search("") == []


def test_ranking(ix):
    hits = ix.search("brillar")                        # exact in both songs
    assert [h.kind for h in hits] == ["exact", "exact"]
    # a hit that starts a line ("Brillar no cuesta nada") ranks before one sung more times
    assert [h.song for h in hits] == ["otra_cancion", "cancion_de_prueba"]
    assert ix.search("brilar")[0].kind == "approx"     # typos rank after exact (other query)


def test_limit(ix):
    assert len(ix.search("a", limit=1)) == 1


# --- display

def test_line_tokens_highlight_the_hit(ix):
    hit = ix.search("sale sobre")[0]
    tokens = ix.line_tokens(hit)
    assert [w for w, marked, _ in tokens if marked] == ["sale", "sobre"]
    assert [w for w, _, _ in tokens] == "La luna sale sobre el puerto".split()


def test_phrase_across_lines_shows_both(ix):
    hit = ix.search("en la ciudad el pinguino")[0]
    tokens = ix.line_tokens(hit)
    assert any(new_line for _, _, new_line in tokens)  # "/" between the two lines
    assert tokens[0][0] == "Nadie"


def test_suggestions(ix):
    assert ix.suggest("vamos a") == [("Vamos a brillar esta noche", "cancion_de_prueba")]
    assert ix.suggest("v") == []                       # too short
