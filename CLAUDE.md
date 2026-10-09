# redo: lyric search → song clip ("Extractos")

Search a phrase from a Patricio Rey y sus Redonditos de Ricota song and get an
audio clip of exactly where it's sung. Flask app + SQLite index of word timings.
Repo: github.com/3ll34ndr0/redo. User writes in Spanish/English.

## Layout
- `lyrics/music/<song>.mp3`: original songs (105). **Song id = this file name.**
- `lyrics/music_128/<song>.mp3`: same songs at 128 kbps for the live site (387 MB vs 957 MB), made by
  `lyrics/reencode.sh`; timing checked identical (0 ms lag). Point `LIBRARY_PATH` here when deploying.
- `lyrics/separated/htdemucs_ft/<song>/vocals.mp3`: Demucs vocal stems (same timeline as music, +0.025 s).
- `lyrics/text/`: **all lyrics, a separate PRIVATE repo** (github.com/3ll34ndr0/redo-letras,
  cloned there; ignored by this public repo: copyright). Paths in `eval/common.py` (TEXT, CORPUS, SUNG, LABELS).
  - `text/corpus/<name>.txt`: lyrics as scraped (stanzas separated by blank lines). Never edited by tools.
  - `text/sung/<song>.txt`: lyrics **as actually sung** (repeats added). Edit these by hand when a song's structure is wrong.
  - `text/labels/`, `text/labels_todo/`: alignment ground truth (Audacity labels hold lyric lines).
- `lyrics/corpus/`: now only leftovers (vocal mp3s, minicorpus experiments); ignored.
- `lyrics/eval/`: alignment pipeline + evaluation (see `lyrics/eval/README.md` for labelling).
- `lyrics/build_db.py`: alignment JSON → `web/redondos.db`.
- `web/app.py` + `web/textnorm.py`: the app. `lyrics/mfa/`, `tempo.py`, `webno/`: old MFA pipeline, superseded.
- Python: always `venv/bin/python` (torch/torchaudio 2.11, demucs, flask, praatio). No GPU.

## Pipeline (run from lyrics/eval unless noted)
1. `../../venv/bin/python ctc_align.py --all`: CTC alignment of corpus lyrics → `out/ctc.json` (~40 s/song; caches model output in `out/emissions/`).
2. `../../venv/bin/python sung.py --all`: spot stanzas, ADD missing repeats → `../text/sung/*.txt`.
3. `../../venv/bin/python ctc_align.py --sung --all`: re-align with sung lyrics → `out/ctc_sung.json` (instant with cache).
4. `cd .. && ../venv/bin/python build_db.py`: → `../web/redondos.db` (tables `words` + legacy `redondos_search`).
- After hand-editing `text/sung/<song>.txt`: `ctc_align.py --sung <song>` then step 4 (and commit in `text/`).
- Run app: `cd web && LIBRARY_PATH=../lyrics/music ../venv/bin/python app.py` → http://localhost:5000
  (`FLASK_DEBUG=1` for the debugger: local only, it allows running code).

## How alignment works (and why)
- **CTC forced alignment** (torchaudio `MMS_FA`, wav2vec2, 1100+ languages) on the vocal stem,
  wildcard token `*` between lines: absorbs intros/solos/bleed/unwritten repeats. Replaced MFA,
  which scattered words over intros (errors up to 15 s).
- **Silence guard**: no letters where the vocal stem is silent (±0.15 s margin). Fixed choruses
  the model can't recognize (group vocals) being placed in silent instrumental stretches.
- **Unstrand tiny words**: words ≤2 letters, ≤0.1 s, followed by silence inside the same line move
  to the next word ("A | brillar"). Applying it to all words broke songs (singers pause mid-line).
- **sung.py is additions-only**: spotting-based reordering/removal made things worse; only adds a
  stanza (score ≥ 0.15) where no file stanza is aligned. Model can't spot some choruses → add by hand.
- **Search** (`web/search.py`, in memory): `textnorm.words()` (lowercase, no accents/punctuation, ñ→n)
  on both index and query; phrase of any length on the `words` table, each query word costing 0 exact,
  0.3 last word as prefix ("a brill"), 0.5 Spanish sound-alike (b/v, h, ll/y, double letters), 1–2
  typos (edit distance, none under 4 letters). Nothing found → closest lyric lines (word-overlap score).
  One card per song: best cost, then line start, then most repeats; other occurrences as chips.
  `words` has `line`/`tok`/`orig` (build_db.py re-reads sung/corpus file) to show the line highlighted.
  `/suggest?q=` completes lines while typing. Clips cut lazily by `/clip/<song>/<ms>-<ms>.mp3`
  (phrase −0.2 s/+0.3 s, cached in SNIPPET_CACHE_DIR, file name has ms timing).
- **Share/download** (per card): Descargar = `/clip/...mp3?name=<Song - words.mp3>` (attachment).
  Compartir = Web Share API with the mp3 file (WhatsApp etc.); only shown where the browser can share
  files and only over HTTPS/localhost (not `http://<LAN IP>`). Clip prefetched so iOS shares within the tap.

- **Clip reports ("¿No coincide?")**: each result card has a panel: problem (starts_late, starts_early,
  ends_early, ends_late, wrong_phrase, unsure = "Quisió... (No sabe, no responde)") + optional start/end nudges the visitor can hear (`/clip/<song>/<ms>-<ms>.mp3`).
  POST `/report` (validated, 10/min + 50/day per visitor, no IP stored) → SQLite `web/reports.py`, file
  REPORTS_DB = /reports/reports.db on PVC `extractos-reports` (local-path: deleting the claim deletes the data,
  so it's annotated Prune=false,Delete=false). Metric `extractos_clip_reports_total{problem}`, log event
  `clip_report`, dashboard row. Read them: `tools/reports.py export` (ssh + kubectl) then `tools/reports.py summary`
  (per song/line: problems, median correction). Next step (not done): apply corrections, e.g. a timing-fixes file
  used by build_db.py, or turn corrected reports into eval labels.

## Evaluation (lyrics/eval)
- Ground truth: Audacity labels in `text/labels/` for la_bestia_pop, divina_tv_führer, etiqueta_negra,
  la_murga_de_los_renegados. **Label on the vocals channel** of `eval/audio/<song>.flac` (L=mix, R=vocals):
  marking on the mix makes labels ~0.37 s late.
- `../../venv/bin/python score.py out/ctc_sung.json [other.json|../mfa]`
- Current (2026-10-05): CTC+sung+guard **93% of lines within 0.3 s, 98% within 1 s**; MFA 60%/63%.
  Known miss: la_bestia_pop "Vamos a brillar" at 209.9 s (+0.6 s, held note stretched).

## Gotchas
- la_bestia_pop.mp3's header says 247 s but it decodes to 240.8 s (damaged file; nothing sung is lost).
- torchcodec can't decode some original mp3s (bad packet) → decode with the ffmpeg CLI (see offset.py).
- Lyrics file names differ from song ids (accents, hyphens): use `common.lyrics_path(song)`;
  aliases in `common.LYRICS_ALIASES` (roto_molhado→rato_molhado, la_murga_de_la_virgencita→murga_de_la_virgencita).
- 3 instrumentals have empty lyrics (capricho_magyar, soga_de_caín, sushi); `bonus` has no lyrics file.
- ~1.2 GB MMS model in ~/.cache/torch/hub (first run downloads it slowly).

## Open
- Code committed 2026-10-05 (not pushed yet: pushing to main triggers the deploy workflow).
  `.gitignore` keeps out audio, `*.db`, `eval/out/`, `lyrics/text/` and the old experiments (`mfa/`,
  `la_bestia_pop/`, `.la_bestia_pop/`, `webno/`: they contain lyrics). `git add -A` is safe except `a` (user's note).
- Private lyrics repo redo-letras: pushed 2026-10-05.
- Go-live steps left: rename GitHub repo malvinasargentinas → redo; /srv/extractos + rsync on the VPS;
  DNS record; push; make the ghcr package public; then `kubectl apply -f argocd-application.yaml`.
- Check doubtful auto-additions: buenas_noticias (stanza at 22.5 s), roto_y_mal_parado ("(Le Tango)").
- Later (marso.ar README §9, ~/Documentos/laburo/leandro): move the UI into the Nicolino site, API-only here.

## Deploy (public site https://extractos.marso.ar, same pattern as ~/Documentos/thai-practice)
- Push to `main` touching web code/Dockerfile/`k8s/deployment.yaml` → `.github/workflows/deploy.yml` builds
  `ghcr.io/3ll34ndr0/redo:<sha>`, commits the tag into `k8s/deployment.yaml` → Argo CD app
  `extractos` (`argocd-application.yaml`, applied once by hand) syncs `k8s/` (namespace `extractos`).
- CI (`.github/workflows/deploy.yml`): unit-tests (`web/tests`, pytest) → in parallel: image (build →
  smoke test `web/tests/smoke_test.py <image>`: runs it read-only/non-root, checks pages, clip duration,
  metrics, JSON logs → Trivy: every fixable vulnerability uploaded (SARIF) to the repo's Security tab →
  Code scanning; GATE: a fixable HIGH/CRITICAL fails the job (no push, no deploy; table in the job summary) → push to ghcr.io) and
  browser-tests (`web/tests/browser_checks.py`, Playwright/Chromium: suggestions, ¿No coincide? panel,
  Descargar following the adjustment, reports, occurrences, phone width; ~8 s + install) → deploy (main only:
  commit the image tag to k8s/deployment.yaml). Run locally: `cd web && ../venv/bin/python -m pytest
  tests/browser_checks.py` (venv has playwright 1.55 + chromium).
  PRs run the tests only. Tests use MADE-UP data (`web/tests/fixture.py`): never real lyrics or audio
  (public repo). Run locally: `cd web && ../venv/bin/python -m pytest tests -q`;
  `docker build -t extractos:test . && python3 web/tests/smoke_test.py extractos:test`.
- **Argo CD does NOT auto-sync (2026-10-08): deploys are MANUAL.** After CI's bot commit "Deploy <sha>"
  lands on main, the user syncs `extractos` by hand in Argo CD. The controller (v3.3.7, default settings)
  only checks apps when woken (start-up, manual sync); its 2-minute periodic refresh never fires, also
  after a restart. User's choice: keep syncing manually. Options if revisited: GitHub webhook to
  https://argocd.marso.ar/api/webhook, or upgrading Argo CD.
- Image = code + standalone ffmpeg only (~400 MB). Songs + DB are NOT in the image: VPS folder
  `/srv/extractos/{music/,redondos.db}` (hostPath, read-only), copied with
  `rsync -av --chmod=D755,F644 lyrics/music_128/ vps:/srv/extractos/music/` and `rsync -av web/redondos.db vps:/srv/extractos/`.
  The app reloads the DB when it changes: new alignment = rsync the .db, no redeploy.
- HTTPS by Cloudflare (proxied A record `extractos` → 66.94.113.102 in the yo repo's `dns/dnsconfig.js`);
  Cloudflare → Traefik `web` entrypoint is HTTP. Clips sent with `Cache-Control: max-age=86400`.
- gunicorn 1 worker × 8 threads, 1 replica: rate limits (Flask-Limiter, per `CF-Connecting-IP`) and the
  index are in memory. Limits: search 30/min, suggest 120/min, clip 60/min per IP + 600/min total;
  2 ffmpeg at once; clip cache (emptyDir) pruned above 300 MB.
- **Observability** (`web/observability.py`): Prometheus metrics on port 9100 (`extractos_*`: requests,
  searches by outcome exact/partial/approx/none, clips cut/cached/failed × play/download, ffmpeg time,
  429s, shares reported by the page via POST `/event`, cache size, index size); one JSON log line per
  request/search/clip/share/error on stdout with `trace_id`, no visitor IPs; OpenTelemetry traces
  (request → `search` / `index.load` / `clip.ffmpeg`) sent only if `OTEL_EXPORTER_OTLP_ENDPOINT` is set
  (`k8s/deployment.yaml`: Alloy's receiver). Probes use `/healthz` (not logged).
  Collected by Grafana Alloy → Grafana Cloud https://leandro.grafana.net (setup and the Fleet pipeline
  in ~/Documentos/k3s-cluster, README "Monitoring"). The Grafana MCP in Claude Code is the user's WORK one, not this.
- Dashboard: `monitoring/extractos-dashboard.json`, generated by `monitoring/make_dashboard.py`. Deployed as
  code by OpenTofu (`monitoring/tofu/`: folder "Extractos" + dashboard uid `extractos`, data sources looked up
  by name) through `.github/workflows/grafana.yml`: PR = plan, push to main = apply. State in Contabo Object
  Storage, bucket `opentofu-states`, key `redo/grafana.tfstate` (S3 backend, `use_lockfile`; the workflow
  checks the bucket honours conditional writes, else warns), ENCRYPTED by OpenTofu (pbkdf2 + aes_gcm).
  GitHub secrets: GRAFANA_SA_TOKEN (stack service account, Editor), TOFU_STATE_PASSPHRASE,
  CONTABO_S3_ACCESS_KEY_ID, CONTABO_S3_SECRET_ACCESS_KEY; repo variables CONTABO_S3_ENDPOINT
  (https://<region>.contabostorage.com), optional GRAFANA_PROM_DS / GRAFANA_LOKI_DS.
  Change the dashboard = edit make_dashboard.py, run it, commit both. Local run: export TF_VAR_grafana_token,
  TF_VAR_state_passphrase, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3. Lost passphrase:
  delete the state object and re-apply (overwrite = true takes the dashboard over). Tested 2026-10-07 against a local
  Prometheus/Loki/Grafana with real app traffic: all 24 queries valid. Counters are pre-created at 0 in
  observability.py, otherwise increase() misses the first event of each label after a restart.
- Ideas: measure line ends (few labels have ends).
