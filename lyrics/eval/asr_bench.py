#!/usr/bin/env python3
"""Benchmark Orchardrun's speech recognition (the mic button's ASR) on the vocal stems.

Usage: asr_bench.py run [--lines-per-song 5] [--workers 1] [--only song,...]
       asr_bench.py report

Two kinds of items, both cut from lyrics/vocals_64/<song>.mp3 (the stems the site serves):

  song   the whole vocal stem → word error rate against every sung word of the song
         (the words table: the lyrics as sung, repeats included)
  line   one lyric line cut by our alignment timings, N random lines per song (seed 0, lines
         of 3+ words) → WER against the line, and what the mic does with it: search the
         transcript (web/search.py) and see whether the line's song comes first / in the top 5

Each call costs money: results go to out/asr/results.jsonl as they arrive and a re-run only
sends the items that have no answer yet (failed ones are retried). The key is read at run time
from the cluster (lectura/lectura-secrets, ORCHARDRUN_API_KEY) into this process only.
Transcripts are lyrics: out/ is git-ignored and must stay so.

WER = (substitutions + deletions + insertions) / reference words, on textnorm.words() of both
sides (lowercase, no accents or punctuation), so it can go above 1 when the ASR invents text.
"""

import argparse
import base64
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "..", "web")
sys.path.insert(0, WEB)
import search  # noqa: E402
import stt  # noqa: E402
import textnorm  # noqa: E402

DB = os.path.join(WEB, "redondos.db")
VOCALS = os.path.join(HERE, "..", "vocals_64")
OUT = os.path.join(HERE, "out", "asr")
RESULTS = os.path.join(OUT, "results.jsonl")
REPORT = os.path.join(OUT, "report.md")
CLUSTER = "root@fs2-usw.delightvoip.com"
SEED = 0
MIN_LINE_WORDS = 3
SONG_TIMEOUT_S = 300        # a whole song takes much longer than the mic's few seconds
RETRIES = 3


def load_index():
    with sqlite3.connect(DB) as conn:
        rows = conn.execute("SELECT song, w, start, end, line, tok, orig FROM words ORDER BY song, i").fetchall()
    return search.Index(rows)


def plan(index, per_song):
    """Every item to transcribe: [{id, kind, song, start, end, ref}]."""
    items = []
    for song, words in sorted(index.songs.items()):
        if not os.path.exists(os.path.join(VOCALS, f"{song}.mp3")):
            continue
        items.append({"id": f"song:{song}", "kind": "song", "song": song, "start": None, "end": None,
                      "ref": [w.w for w in words]})
        lines = [(a, b) for a, b in index.lines(song) if b - a + 1 >= MIN_LINE_WORDS]
        for a, b in sorted(random.Random(f"{SEED}:{song}").sample(lines, min(per_song, len(lines)))):
            items.append({"id": f"line:{song}:{a}", "kind": "line", "song": song,
                          "start": words[a].start, "end": words[b].end,
                          "ref": [w.w for w in words[a:b + 1]]})
    return items


def api_key():
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", CLUSTER,
                          "kubectl get secret lectura-secrets -n lectura -o jsonpath={.data.ORCHARDRUN_API_KEY}"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode or not out.stdout.strip():
        sys.exit(f"could not read the key from the cluster (ssh exit {out.returncode})")
    return base64.b64decode(out.stdout.strip()).decode().strip()


def audio(item):
    """(bytes, file name, content type) to send."""
    path = os.path.join(VOCALS, f"{item['song']}.mp3")
    if item["kind"] == "song":
        with open(path, "rb") as f:
            return f.read(), f"{item['song']}.mp3", "audio/mpeg"
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{item['start']:.3f}", "-to", f"{item['end']:.3f}",
                        "-i", path, "-ac", "1", "-ar", "16000", tmp.name], check=True)
        with open(tmp.name, "rb") as f:
            return f.read(), "linea.wav", "audio/wav"


def transcribe(item):
    data, name, ctype = audio(item)
    for attempt in range(RETRIES):
        t0 = time.monotonic()
        try:
            text = stt.transcribe(data, name, ctype)
            return {"text": text, "ms": round((time.monotonic() - t0) * 1000), "bytes": len(data)}
        except stt.Unavailable as e:
            error = str(e)
            if attempt + 1 < RETRIES:
                time.sleep(5 * 2 ** attempt)
    return {"error": error, "bytes": len(data)}


def read_results():
    done = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            for line in f:
                r = json.loads(line)
                done[r["id"]] = r          # the last answer for an id wins
    return done


def run(args):
    index = load_index()
    items = plan(index, args.lines_per_song)
    if args.only:
        only = set(args.only.split(","))
        items = [it for it in items if it["song"] in only]
    done = read_results()
    todo = [it for it in items if "text" not in done.get(it["id"], {})]
    print(f"{len(items)} items, {len(items) - len(todo)} already answered, sending {len(todo)}")
    if not todo:
        return
    os.environ["ORCHARDRUN_API_KEY"] = api_key()
    stt.TIMEOUT_S = SONG_TIMEOUT_S
    os.makedirs(OUT, exist_ok=True)
    lock, count = threading.Lock(), [0]

    def one(item):
        result = {"id": item["id"], **transcribe(item)}
        with lock, open(RESULTS, "a") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
            count[0] += 1
            status = "error: " + result["error"][:80] if "error" in result else f"{result['ms']} ms"
            print(f"[{count[0]}/{len(todo)}] {item['id']}  {status}", flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(one, todo))


def edits(ref, hyp):
    """Word-level edit distance (substitutions + deletions + insertions)."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def pct(x):
    return f"{100 * x:.1f}%"


def report(args):
    index = load_index()
    items = plan(index, args.lines_per_song)
    done = read_results()
    rows = {"song": [], "line": []}
    failed = []
    for it in items:
        r = done.get(it["id"])
        if not r:
            continue
        if "text" not in r:
            failed.append((it["id"], r["error"]))
            continue
        hyp = textnorm.words(r["text"])
        row = {**it, "text": r["text"], "ms": r["ms"], "hyp": hyp, "edits": edits(it["ref"], hyp)}
        if it["kind"] == "line":
            ranked = [h.song for h in index.search(r["text"], limit=5)] if hyp else []
            row["rank"] = ranked.index(it["song"]) + 1 if it["song"] in ranked else None
        rows[it["kind"]].append(row)

    def wer_block(name, rs):
        if not rs:
            return [f"## {name}", "", "No answers yet.", ""]
        n_ref = sum(len(r["ref"]) for r in rs)
        per = [r["edits"] / len(r["ref"]) for r in rs]
        out = [f"## {name}", "",
               f"- items answered: {len(rs)} of {sum(1 for it in items if it['kind'] == rs[0]['kind'])}",
               f"- WER (all words together): **{pct(sum(r['edits'] for r in rs) / n_ref)}** over {n_ref} words",
               f"- WER per item: median {pct(statistics.median(per))}, "
               f"best {pct(min(per))}, worst {pct(max(per))}",
               f"- empty transcripts: {sum(1 for r in rs if not r['hyp'])}",
               f"- time per call: median {statistics.median(r['ms'] for r in rs) / 1000:.1f} s", ""]
        return out

    lines = [f"# Orchardrun ASR benchmark: Redondos vocal stems", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `lyrics/eval/asr_bench.py`, "
             f"{args.lines_per_song} lines per song (seed {SEED}), language=es. PRIVATE: contains lyrics.", ""]
    lines += wer_block("Whole songs", rows["song"])
    lines += wer_block("Lines (what the mic gets)", rows["line"])
    if rows["line"]:
        ls = rows["line"]
        top1 = sum(1 for r in ls if r["rank"] == 1)
        top5 = sum(1 for r in ls if r["rank"])
        lines += ["### Search with the transcript", "",
                  f"- right song first: **{pct(top1 / len(ls))}** ({top1}/{len(ls)})",
                  f"- right song in the top 5: {pct(top5 / len(ls))} ({top5}/{len(ls)})", ""]
        by_len = {}
        for r in ls:
            by_len.setdefault("3–5 words" if len(r["ref"]) <= 5 else "6–8 words" if len(r["ref"]) <= 8
                              else "9+ words", []).append(r)
        lines += ["| line length | lines | WER | right song first |", "|---|---|---|---|"]
        for k in ("3–5 words", "6–8 words", "9+ words"):
            if k in by_len:
                g = by_len[k]
                lines.append(f"| {k} | {len(g)} | {pct(sum(r['edits'] for r in g) / sum(len(r['ref']) for r in g))} "
                             f"| {pct(sum(1 for r in g if r['rank'] == 1) / len(g))} |")
        lines.append("")
    if rows["song"]:
        lines += ["## Per song", "", "| song | words | WER | lines found first |", "|---|---|---|---|"]
        by_song = {}
        for r in rows["line"]:
            by_song.setdefault(r["song"], []).append(r["rank"] == 1)
        for r in sorted(rows["song"], key=lambda r: r["edits"] / len(r["ref"])):
            found = by_song.get(r["song"], [])
            lines.append(f"| {r['song']} | {len(r['ref'])} | {pct(r['edits'] / len(r['ref']))} "
                         f"| {sum(found)}/{len(found)} |")
        lines.append("")
    misses = [r for r in rows["line"] if r["rank"] != 1]
    if misses:
        lines += ["## Lines whose song didn't come first", "", "| line | heard | rank |", "|---|---|---|"]
        for r in sorted(misses, key=lambda r: r["id"]):
            lines.append(f"| {r['song']}: {' '.join(r['ref'])} | {' '.join(r['hyp']) or '(nothing)'} "
                         f"| {r['rank'] or '-'} |")
        lines.append("")
    if failed:
        lines += ["## Failed calls", ""] + [f"- {i}: {e[:150]}" for i, e in failed] + [""]
    os.makedirs(OUT, exist_ok=True)
    with open(REPORT, "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines[:40]))
    print(f"... full report: {REPORT}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["run", "report"])
    p.add_argument("--lines-per-song", type=int, default=5)
    p.add_argument("--workers", type=int, default=1)     # hobby tier: 1 call in flight per key
    p.add_argument("--only", help="comma-separated song ids (run only)")
    args = p.parse_args()
    run(args) if args.command == "run" else report(args)


if __name__ == "__main__":
    main()
