// Timing editor: the edges of a clip over the singer's waveform (/voice: the vocal stem around
// the clip), used by ¿No coincide? on the search page and by the review on /admin/reports.
// Drag an edge, or tap the waveform to bring the nearest edge there; arrows move the focused
// edge 0.1 s, with Shift 1 s. Inside the edges the waveform keeps its colour, outside it's
// greyed; dashed lines = the clip as served. "Escuchar la voz" plays the selection from the voice.
//
//   const editor = new WaveEditor(root, { onChange, onVoicePlay })   root: templates/_wave.html
//   editor.load(song, served, fixed, marks)   {start, end} in ms; fixed: initial selection (default served);
//                                              marks: more dotted lines [ms, ...] (e.g. the visitor's edges)
//   editor.fixed                               the selection now, {start, end}
//   WaveEditor.stopVoice()                     stop the voice preview, whichever editor plays it
(function () {
    const MIN_MS = 200, MAX_MS = 60000, MARGIN_MS = 5000, SNAP_MS = 10;
    const MAX_WINDOW_MS = MAX_MS + 2 * MARGIN_MS;       // what /voice serves (app.py MAX_VOICE_S)
    const COLOURS = { inside: '#74ACDF', outside: '#D1D5DB', served: '#9CA3AF', mark: '#F6B40E' };
    const clock = ms => `${Math.floor(ms / 60000)}:${(ms % 60000 / 1000).toFixed(1).padStart(4, '0')}`;
    const clamp = (x, lo, hi) => Math.min(Math.max(x, lo), hi);

    // One Web Audio context for the page (decoding works before any tap; playing resumes it)
    let audioCtx = null, stopVoice = () => {};
    const voiceCtx = () => audioCtx || (audioCtx = new (window.AudioContext || window.webkitAudioContext)());

    class WaveEditor {
        static stopVoice() { stopVoice(); }

        constructor(root, { onChange = () => {}, onVoicePlay = () => {} } = {}) {
            this.root = root;
            this.onChange = onChange;
            this.onVoicePlay = onVoicePlay;
            this.wave = root.querySelector('.wave');
            this.canvas = this.wave.querySelector('canvas');
            this.playhead = this.wave.querySelector('.wave-playhead');
            this.waveStatus = this.wave.querySelector('.wave-status');
            this.voiceBtn = root.querySelector('.voice-play');
            this.handles = { start: this.wave.querySelector('[data-edge=start]'), end: this.wave.querySelector('[data-edge=end]') };
            this.clip = this.fixed = this.win = null;   // as served / as adjusted / the waveform's window, in ms
            this.marks = [];
            this.voice = this.peaks = null;
            this.loading = 0;
            this.dragging = null;
            this.listen();
        }

        frac(ms) { return (ms - this.win.start) / (this.win.end - this.win.start); }

        load(song, served, fixed = served, marks = []) {
            stopVoice();
            this.clip = { song, start: served.start, end: served.end };
            this.fixed = { start: fixed.start, end: fixed.end };
            this.marks = marks;
            // around the served clip and the initial selection, as long as /voice serves
            const start = Math.max(0, Math.min(served.start, fixed.start) - MARGIN_MS);
            this.win = { start, end: Math.min(Math.max(served.end, fixed.end) + MARGIN_MS, start + MAX_WINDOW_MS) };
            this.loadVoice();
            this.show();
        }

        async loadVoice() {
            const mine = ++this.loading, { wave, waveStatus } = this;
            this.voice = this.peaks = null;
            wave.dataset.state = 'loading';
            waveStatus.textContent = 'Cargando la voz…';
            try {
                const r = await fetch(`/voice/${encodeURIComponent(this.clip.song)}/${this.win.start}-${this.win.end}.wav`);
                if (!r.ok) throw new Error(r.status);
                const solo = r.headers.get('X-Voice-Source') !== 'song';
                const data = await r.arrayBuffer();
                const buffer = await new Promise((ok, fail) => voiceCtx().decodeAudioData(data, ok, fail));
                if (mine !== this.loading) return;          // another clip was chosen meanwhile
                this.voice = buffer;
                this.win.end = Math.min(this.win.end, this.win.start + Math.round(buffer.duration * 1000));   // the song may end sooner
                const source = this.root.querySelector('.wave-source');
                if (source) source.textContent = solo ? 'solo la voz' : 'la canción (no hay voz separada)';
                wave.dataset.state = 'ready';
                waveStatus.textContent = '';
            } catch (err) {
                if (mine !== this.loading) return;
                wave.dataset.state = 'error';                // the edges still work, without the picture
                waveStatus.textContent = 'No se pudo cargar la voz';
            }
            this.show();
        }

        // Loudest sample per pixel column, scaled to the loudest column (computed once per width)
        columns(width) {
            if (this.peaks && this.peaks.length === width) return this.peaks;
            const data = this.voice.getChannelData(0), per = data.length / width;
            const peaks = this.peaks = new Float32Array(width);
            for (let x = 0; x < width; x++) {
                let max = 0;
                for (let i = Math.floor(x * per), end = Math.floor((x + 1) * per); i < end; i++) {
                    const v = Math.abs(data[i]);
                    if (v > max) max = v;
                }
                peaks[x] = max;
            }
            const top = Math.max(...peaks) || 1;
            peaks.forEach((v, x) => { peaks[x] = v / top; });
            return peaks;
        }

        draw() {
            const { canvas } = this, w = canvas.clientWidth, h = canvas.clientHeight;
            if (!w || !this.win) return;                     // hidden, or nothing loaded yet
            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
            const g = canvas.getContext('2d');
            g.scale(dpr, dpr);
            const x0 = this.frac(this.fixed.start) * w, x1 = this.frac(this.fixed.end) * w, mid = h / 2;
            const bars = this.voice ? this.columns(Math.round(w)) : null;
            for (let x = 0; x < w; x++) {
                g.fillStyle = x + 0.5 >= x0 && x + 0.5 <= x1 ? COLOURS.inside : COLOURS.outside;
                const half = bars ? Math.max(0.5, bars[x] * (mid - 4)) : 0.5;
                g.fillRect(x, mid - half, 1, half * 2);
            }
            // where the clip was, as served (dashed), and other marks (dotted)
            const line = (ms, colour, dash) => {
                const x = Math.round(this.frac(ms) * w) + 0.5;
                g.strokeStyle = colour;
                g.setLineDash(dash);
                g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke();
            };
            for (const ms of [this.clip.start, this.clip.end]) line(ms, COLOURS.served, [3, 3]);
            for (const ms of this.marks) line(ms, COLOURS.mark, [1, 2]);
        }

        show() {
            const { root, fixed, win } = this;
            root.querySelector('.report-start').textContent = clock(fixed.start);
            root.querySelector('.report-end').textContent = clock(fixed.end);
            for (const edge of ['start', 'end']) {
                const handle = this.handles[edge];
                handle.style.left = `${this.frac(fixed[edge]) * 100}%`;
                handle.setAttribute('aria-valuemin', win.start);
                handle.setAttribute('aria-valuemax', win.end);
                handle.setAttribute('aria-valuenow', fixed[edge]);
                handle.setAttribute('aria-valuetext', clock(fixed[edge]));
            }
            this.draw();
        }

        // Moves one edge, keeping the selection inside the window, at least MIN_MS and at most MAX_MS long
        move(edge, ms) {
            const { fixed, win } = this;
            ms = Math.round(ms / SNAP_MS) * SNAP_MS;
            if (edge === 'start') fixed.start = clamp(ms, Math.max(win.start, fixed.end - MAX_MS), fixed.end - MIN_MS);
            else fixed.end = clamp(ms, fixed.start + MIN_MS, Math.min(win.end, fixed.start + MAX_MS));
            this.show();
        }

        done() {
            stopVoice();
            this.onChange(this.fixed);
        }

        listen() {
            const { wave, handles } = this;
            const msAt = e => {
                const box = wave.getBoundingClientRect();
                return this.win.start + clamp((e.clientX - box.left) / box.width, 0, 1) * (this.win.end - this.win.start);
            };

            // Drag an edge, or tap/drag anywhere on the waveform to bring the nearest edge there
            wave.addEventListener('pointerdown', e => {
                if (e.button > 0 || !this.win) return;
                const handle = e.target.closest('.wave-handle'), at = msAt(e);
                this.dragging = handle ? handle.dataset.edge
                    : Math.abs(at - this.fixed.start) <= Math.abs(at - this.fixed.end) ? 'start' : 'end';
                wave.setPointerCapture(e.pointerId);
                handles[this.dragging].focus({ preventScroll: true });
                if (!handle) this.move(this.dragging, at);
                e.preventDefault();
            });
            wave.addEventListener('pointermove', e => { if (this.dragging) this.move(this.dragging, msAt(e)); });
            const release = () => { if (this.dragging) { this.dragging = null; this.done(); } };
            wave.addEventListener('pointerup', release);
            wave.addEventListener('pointercancel', release);

            Object.entries(handles).forEach(([edge, handle]) => handle.addEventListener('keydown', e => {
                const sign = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
                if (!sign || !this.win) return;
                e.preventDefault();
                this.move(edge, this.fixed[edge] + sign * (e.shiftKey ? 1000 : 100));
                this.done();
            }));

            window.addEventListener('resize', () => this.draw());
            this.voiceBtn.addEventListener('click', () => this.playVoice());
        }

        // The selection, from the voice: Web Audio plays exactly start..end of the decoded window
        // Doesn't wait for the audio context to start: in a browser without sound output it may
        // never do, and the button must still come back. The playhead and the stop follow the clock.
        playVoice() {
            const { voiceBtn, playhead } = this;
            if (voiceBtn.dataset.playing) return stopVoice();
            if (!this.voice) return;
            stopVoice();
            this.onVoicePlay();
            const ctx = voiceCtx();
            ctx.resume().catch(() => {});
            const source = ctx.createBufferSource();
            source.buffer = this.voice;
            source.connect(ctx.destination);
            const from = this.fixed.start, length = this.fixed.end - from, t0 = performance.now();
            source.start(0, (from - this.win.start) / 1000, length / 1000);
            voiceBtn.dataset.playing = '1';
            voiceBtn.textContent = '■ Parar';
            playhead.hidden = false;
            let stopped = false;
            const frame = () => {
                if (stopped) return;
                playhead.style.left = `${this.frac(from + Math.min(performance.now() - t0, length)) * 100}%`;
                requestAnimationFrame(frame);
            };
            frame();
            const stop = () => {
                if (stopped) return;
                stopped = true;
                clearTimeout(timer);
                try { source.stop(); } catch (err) { /* already ended */ }
                delete voiceBtn.dataset.playing;
                voiceBtn.textContent = '▶ Escuchar la voz';
                playhead.hidden = true;
                if (stopVoice === stop) stopVoice = () => {};
            };
            const timer = setTimeout(stop, length + 300);
            source.onended = stop;
            stopVoice = stop;
        }
    }

    window.WaveEditor = WaveEditor;
})();
