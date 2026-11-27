// Libro de visitas (the section at the bottom of the page): messages are left by voice only.
// The green button works like a chat app's voice note: hold it to record, release to send; slide
// left to cancel, slide up to lock (hands-free: then ➤ sends, 🗑 discards). From the keyboard,
// Enter/Space starts a locked recording. The server turns it into text (POST /guestbook, Orchardrun);
// an admin approves a message before it's shown here.
(() => {
    const root = document.getElementById('mensajes');
    if (!root) return;

    // Scrolled down to it: the page's background turns into the chat's, as if it were another page
    const backdrop = document.querySelector('.gb-backdrop');
    if ('IntersectionObserver' in window) {
        new IntersectionObserver(([entry]) => document.body.classList.toggle('in-guestbook', entry.isIntersecting),
            { rootMargin: '0px 0px -45% 0px' }).observe(root);
    } else if (backdrop) {
        backdrop.classList.add('gb-solid');
    }

    const composer = root.querySelector('.gb-composer');
    if (!composer) return;                                   // no key, or out of credit: nothing to record
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder)) {
        root.querySelector('.gb-unsupported').hidden = false;
        return;
    }
    composer.hidden = false;

    const MAX_MS = 60000, MIN_MS = 1000, CANCEL_PX = 90, LOCK_PX = 70;
    const mic = root.querySelector('.gb-mic'), time = root.querySelector('.gb-time');
    const slide = root.querySelector('.gb-slide'), status = root.querySelector('.gb-status');
    const list = root.querySelector('.gb-messages');
    // starting: waiting for the microphone (the first time the browser asks for permission)
    let state = 'idle';             // idle | starting | recording | locked | sending
    let recorder, stream, startedAt, ticker, discard, pending, lockWanted;
    let origin = null, pointer = null, tapToSend = false;

    const clock = ms => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
    const say = text => { status.textContent = text; };

    function setState(s) {
        state = s;
        root.dataset.state = s;
        mic.setAttribute('aria-label', s === 'locked' ? 'Enviar el mensaje'
            : s === 'recording' ? 'Grabando: soltá para enviar' : 'Grabar un mensaje: mantené apretado y soltá para enviar');
        if (s !== 'recording') slide.style.transform = '';
    }

    async function start(locked) {
        if (state !== 'idle') return;
        setState('starting');
        say('');
        pending = null;
        lockWanted = locked;
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch (err) {
            setState('idle');
            say('Sin permiso para usar el micrófono.');
            return;
        }
        if (pending) {                      // let go (or cancelled) before the microphone was ready
            stream.getTracks().forEach(t => t.stop());
            setState('idle');
            if (pending === 'send') say('Listo. Ahora mantené apretado mientras hablás, y soltá para enviar.');
            return;
        }
        const chunks = [];
        recorder = new MediaRecorder(stream);
        recorder.addEventListener('dataavailable', e => { if (e.data.size) chunks.push(e.data); });
        recorder.addEventListener('stop', () => {
            clearInterval(ticker);
            stream.getTracks().forEach(t => t.stop());      // the browser's "recording" mark goes away
            const ms = Date.now() - startedAt;
            if (discard) return setState('idle');
            send(new Blob(chunks, { type: recorder.mimeType }), ms);
        });
        recorder.start();
        startedAt = Date.now();
        time.textContent = '0:00';
        ticker = setInterval(() => {
            const ms = Date.now() - startedAt;
            time.textContent = clock(ms);
            if (ms >= MAX_MS) finish('send');
        }, 200);
        setState(lockWanted ? 'locked' : 'recording');
    }

    function finish(how) {          // how: 'send' | 'cancel'
        if (state === 'starting') { pending = how; return; }
        if (state !== 'recording' && state !== 'locked') return;
        const short = Date.now() - startedAt < MIN_MS;
        discard = how === 'cancel' || short;
        if (how === 'cancel') say('Mensaje descartado.');
        else if (short) say('Mantené apretado mientras hablás, y soltá para enviar.');
        setState('sending');        // until the recorder's stop event
        recorder.stop();
    }

    function bubble(seconds) {
        root.querySelector('.gb-empty')?.remove();
        const li = document.createElement('li'), text = document.createElement('p'), meta = document.createElement('p');
        li.className = 'gb-bubble gb-out text-gray-800';
        text.textContent = `🎤 ${clock(seconds * 1000)} · Pasando a texto…`;
        text.className = 'text-gray-500 italic';
        meta.className = 'gb-meta';
        li.append(text, meta);
        list.append(li);
        li.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        return { li, text, meta };
    }

    async function send(blob, ms) {
        setState('sending');
        const seconds = Math.round(ms / 100) / 10;
        const b = bubble(seconds);
        const type = (blob.type || 'audio/webm').split(';')[0];
        const body = new FormData();
        body.append('audio', new File([blob], 'mensaje.' + (type.split('/')[1] || 'webm'), { type }));
        body.append('seconds', seconds);
        try {
            const r = await fetch('/guestbook', { method: 'POST', body });
            if (r.status === 201) {
                const { text } = await r.json();
                b.text.textContent = text;
                b.text.className = '';
                b.meta.textContent = 'Enviado ✓✓';
                say('¡Gracias! Lo leemos y, si va, queda acá para todos.');
            } else if (r.ok) {          // nothing heard
                b.li.remove();
                say('No se escuchó nada. Probá de nuevo, más cerca del micrófono.');
            } else {
                b.li.remove();
                if (r.status === 503) {
                    composer.hidden = true;
                    say('Por ahora no se pueden dejar mensajes. Volvé en unos días.');
                } else {
                    say({ 429: 'Ya dejaste varios mensajes. Probá más tarde.', 413: 'El mensaje es muy largo: hasta un minuto.' }[r.status]
                        || 'No se pudo enviar el mensaje. Probá de nuevo.');
                }
            }
        } catch (err) {
            b.li.remove();
            say('No se pudo enviar el mensaje. Probá de nuevo.');
        }
        setState('idle');
    }

    // Hold to record: the pointer is captured, so moving off the button still counts
    mic.addEventListener('pointerdown', e => {
        if (e.button !== 0) return;
        e.preventDefault();
        if (state === 'locked') { tapToSend = true; return; }       // locked: a tap sends
        if (state !== 'idle') return;
        mic.setPointerCapture(e.pointerId);
        pointer = e.pointerId;
        origin = { x: e.clientX, y: e.clientY };
        start(false);
    });
    mic.addEventListener('pointermove', e => {
        if (e.pointerId !== pointer || !origin) return;
        const dx = e.clientX - origin.x, dy = e.clientY - origin.y;
        slide.style.transform = `translateX(${Math.max(-CANCEL_PX, Math.min(0, dx))}px)`;
        slide.style.color = dx < -CANCEL_PX / 2 ? '#ea0038' : '';
        if (dx < -CANCEL_PX) {
            origin = null;
            finish('cancel');
        } else if (dy < -LOCK_PX) {
            origin = null;
            if (state === 'starting') lockWanted = true;
            else if (state === 'recording') setState('locked');
        }
    });
    mic.addEventListener('pointerup', e => {
        if (tapToSend) { tapToSend = false; return finish('send'); }
        if (e.pointerId !== pointer || !origin) return;
        origin = null;
        slide.style.color = '';
        finish('send');
    });
    mic.addEventListener('pointercancel', e => {
        if (e.pointerId !== pointer || !origin) return;       // already locked or cancelled
        origin = null;
        finish('cancel');
    });
    mic.addEventListener('contextmenu', e => e.preventDefault());     // a long press on phones
    // Keyboard (a click with no pointer: detail 0): Enter/Space starts hands-free, again sends
    mic.addEventListener('click', e => {
        if (e.detail !== 0) return;
        if (state === 'idle') start(true);
        else if (state === 'locked') finish('send');
    });
    root.querySelector('.gb-trash').addEventListener('click', () => finish('cancel'));
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && ['starting', 'recording', 'locked'].includes(state)) finish('cancel');
    });
})();
