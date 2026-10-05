"""Lyric search over the word index (words table built by lyrics/build_db.py).

Everything is in memory: ~15k sung words, ~3k distinct ones. A query is
normalized with textnorm, then each query word is compared with every distinct
sung word once, giving a cost per candidate:

    0     same word
    0.3   the LAST query word is the start of the sung word ("a brill" → brillar),
          so the phrase can be typed incompletely (from 2 letters if it's the
          only word)
    0.5   sounds the same in Spanish: b/v, silent h, ll/y, z/ce/ci, double
          letters ("vestia", "eroe", "brilar")
    1, 2  other typos: edit distance (1 allowed from 4 letters, 2 from 7; short
          words must be exact, or "la" would match "lo", "le", "ya"...)

A hit is a run of consecutive sung words matching the query words in order;
its cost is the sum. If nothing matches, `closest_lines` scores every lyric
line by how many query words it contains (with typos) instead.

Ranking (see `search`): one card per song, songs ordered by best cost, then
hits that start a line, then songs where the phrase is sung more times, then
name. Inside a song the representative hit is the first one with the best cost;
the rest are listed as other occurrences.
"""

from dataclasses import dataclass, field
from functools import lru_cache

import textnorm

PREFIX_COST = 0.3
PREFIX_MIN = 2              # letters before a one-word query is completed as a prefix
SOUND_COST = 0.5
CLOSEST_MIN_SIM = 0.6       # word similarity to count as "contains the word"
CLOSEST_MIN_SCORE = 0.45


@dataclass
class Word:
    w: str          # normalized
    start: float
    end: float
    line: int       # line in the lyrics file, None if unknown
    tok: int        # index of the written word (several normalized words can share it)
    orig: str       # as written


@dataclass
class Hit:
    song: str
    i: int          # first and last word index (inclusive)
    j: int
    cost: float
    start: float
    end: float
    line_start: bool = False
    others: list = field(default_factory=list)    # other Hits of the same song

    @property
    def kind(self):
        """'exact', 'partial' (incomplete last word) or 'approx' (typos or closest line)."""
        if self.cost == 0:
            return "exact"
        return "partial" if self.cost == PREFIX_COST else "approx"    # only the last word can be a prefix


def allowed_typos(word):
    return 0 if len(word) < 4 else 1 if len(word) < 7 else 2


SOUNDS = [("ch", "C"), ("qu", "k"), ("ce", "se"), ("ci", "si"), ("c", "k"), ("z", "s"),
          ("v", "b"), ("ll", "y"), ("h", ""), ("w", "u")]


def squeeze(word):
    return "".join(c for n, c in enumerate(word) if n == 0 or c != word[n - 1])


@lru_cache(maxsize=None)
def sounds(word):
    """Spanish pronunciation keys: 'vestia'/'bestia', 'eroe'/'heroe' share one.

    Two keys, double letters squeezed after (ll -> y: 'calle'/'caye') and
    before (ll -> l: 'brilar'/'brillar') the sound replacements.
    """
    keys = set()
    for w in (word, squeeze(word)):
        for a, b in SOUNDS:
            w = w.replace(a, b)
        keys.add(squeeze(w))
    return frozenset(keys)


def levenshtein(a, b, limit=None):
    """Edit distance; stops early (returning limit + 1) once it exceeds `limit`."""
    if limit is not None and abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for x, ca in enumerate(a, 1):
        cur = [x]
        for y, cb in enumerate(b, 1):
            cur.append(min(prev[y] + 1, cur[y - 1] + 1, prev[y - 1] + (ca != cb)))
        if limit is not None and min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


class Index:
    def __init__(self, rows):
        """rows: (song, w, start, end, line, tok, orig) ordered by song, i."""
        self.songs = {}
        for song, *word in rows:
            self.songs.setdefault(song, []).append(Word(*word))
        self.vocab = sorted({w.w for words in self.songs.values() for w in words})
        self.match_costs = lru_cache(maxsize=4096)(self._match_costs)
        self.similarities = lru_cache(maxsize=4096)(self._similarities)

    # --- word level

    def _match_costs(self, q, last):
        """{sung word: cost} for the sung words query word q can stand for."""
        costs = {}
        typos = allowed_typos(q)
        q_sounds = sounds(q)
        for v in self.vocab:
            if v == q:
                costs[v] = 0
                continue
            best = SOUND_COST if sounds(v) & q_sounds else None
            if typos and best is None:
                d = levenshtein(q, v, typos)
                if d <= typos:
                    best = d
            if last and len(v) > len(q):
                head = v[:len(q)]
                if head == q:
                    best = PREFIX_COST
                elif sounds(head) & q_sounds and (best is None or best > SOUND_COST + PREFIX_COST):
                    best = SOUND_COST + PREFIX_COST
                elif typos:
                    d = levenshtein(q, head, typos)
                    if d <= typos and (best is None or d + PREFIX_COST < best):
                        best = d + PREFIX_COST
            if best is not None:
                costs[v] = best
        return costs

    def _similarities(self, q, last):
        """{sung word: similarity 0..1} for sung words resembling q (closest_lines)."""
        sims = {}
        for v in self.vocab:
            target = v[:len(q)] if last and len(v) > len(q) else v
            n = max(len(q), len(target))
            limit = int(n * (1 - CLOSEST_MIN_SIM))
            d = levenshtein(q, target, limit)
            if d <= limit:
                sims[v] = 1 - d / n
        return sims

    # --- phrases

    def is_line_start(self, words, i):
        return i == 0 or words[i].line is None or words[i - 1].line != words[i].line

    @staticmethod
    def completes(q, n, prefix=True):
        """Whether query word n may be the start of a longer sung word."""
        return prefix and n == len(q) - 1 and (len(q) > 1 or len(q[n]) >= PREFIX_MIN)

    def find(self, query, prefix=True):
        """Every place the phrase is sung, exact or with typos: [Hit] in song order."""
        q = textnorm.words(query)
        if not q:
            return []
        costs = [self.match_costs(w, self.completes(q, n, prefix)) for n, w in enumerate(q)]
        hits = []
        for song, words in sorted(self.songs.items()):
            for i in range(len(words) - len(q) + 1):
                total = 0
                for k, c in enumerate(costs):
                    cost = c.get(words[i + k].w)
                    if cost is None:
                        break
                    total += cost
                else:
                    j = i + len(q) - 1
                    hits.append(Hit(song, i, j, total, words[i].start, words[j].end,
                                    self.is_line_start(words, i)))
        return hits

    def lines(self, song):
        """[(first word index, last word index)] per lyric line of the song."""
        words, spans = self.songs[song], []
        for i, w in enumerate(words):
            if self.is_line_start(words, i):
                spans.append([i, i])
            else:
                spans[-1][1] = i
        return spans

    def closest_lines(self, query):
        """Lines containing most query words (with typos), for when find() has nothing.

        Score = 0.75 × how much of the query the line has (weighted by word
        length: "a" matters less than "galopar") + 0.25 × how much of the line
        is the query (prefers short lines). Hit i..j is the whole line;
        `matched` (word indices) is set on each hit for highlighting.
        """
        q = textnorm.words(query)
        if not q:
            return []
        sims = [self.similarities(w, self.completes(q, n)) for n, w in enumerate(q)]
        weights = [len(w) for w in q]
        hits = []
        for song, words in sorted(self.songs.items()):
            for a, b in self.lines(song):
                line = words[a:b + 1]
                got, matched = 0, set()
                for s, weight in zip(sims, weights):
                    best = max(((s.get(w.w, 0), a + k) for k, w in enumerate(line)), default=(0, None))
                    if best[0]:
                        got += best[0] * weight
                        matched.add(best[1])
                if not matched:
                    continue
                score = 0.75 * got / sum(weights) + 0.25 * len(matched) / len(line)
                if score >= CLOSEST_MIN_SCORE:
                    hit = Hit(song, a, b, 1 + (1 - score) * 10, words[a].start, words[b].end, True)
                    hit.matched = matched
                    hits.append(hit)
        return hits

    # --- results

    def search(self, query, limit=10):
        """Best hit per song, ranked; falls back to the closest lines. [Hit]."""
        hits = self.find(query) or self.closest_lines(query)
        by_song = {}
        for h in hits:
            by_song.setdefault(h.song, []).append(h)
        best = []
        for song, song_hits in by_song.items():
            top = min(song_hits, key=lambda h: (h.cost, not h.line_start, h.start))
            top.others = sorted((h for h in song_hits if h is not top), key=lambda h: h.start)
            best.append(top)
        best.sort(key=lambda h: (round(h.cost, 3), not h.line_start, -len(h.others), h.song))
        return best[:limit]

    def suggest(self, query, limit=8):
        """Lyric lines that complete what's being typed: [(line text, song)].

        By cost, then lines the phrase starts, then lines sung more often
        (choruses); each distinct line once.
        """
        if len(query.strip()) < 2:
            return []
        seen = {}
        for h in self.find(query):
            text = self.line_text(h)
            key = " ".join(textnorm.words(text))
            if key in seen:
                seen[key][2] += 1
            else:
                seen[key] = [(h.cost, not h.line_start), (text, h.song), 1]
        ranked = sorted(seen.values(), key=lambda s: (s[0], -s[2]))
        return [s[1] for s in ranked[:limit]]

    # --- display

    def line_tokens(self, hit):
        """Written words of the line(s) around a hit: [(word, highlighted, new line)].

        Highlighted = part of the hit (or, for a closest-line hit, a word that
        resembles a query word). Without line numbers, 4 words of context.
        """
        words = self.songs[hit.song]
        if words[hit.i].line is None:
            a, b = max(0, hit.i - 4), min(len(words) - 1, hit.j + 4)
        else:
            a, b = hit.i, hit.j
            while a > 0 and words[a - 1].line == words[hit.i].line:
                a -= 1
            while b < len(words) - 1 and words[b + 1].line == words[hit.j].line:
                b += 1
        marked = getattr(hit, "matched", None) or set(range(hit.i, hit.j + 1))
        out = []
        for k in range(a, b + 1):
            w = words[k]
            if k > a and w.tok == words[k - 1].tok:      # same written word ("to-to-todo")
                out[-1][1] = out[-1][1] or k in marked
                continue
            new_line = k > a and w.line != words[k - 1].line
            out.append([w.orig, k in marked, new_line])
        return [tuple(t) for t in out]

    def line_text(self, hit):
        return " ".join(w for w, _, _ in self.line_tokens(hit))
