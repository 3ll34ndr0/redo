// Tus últimos audios: what this browser downloaded or shared, kept in localStorage (nothing on the
// server) and listed on the landing page. Entries become .audio-card elements, so this must load
// BEFORE the page's script, which gives those their play, share and download buttons.
//
//   RecentClips.add(card)    remember what the card downloads/shares now (again: to the top)
//   RecentClips.gone(card)   the server no longer has the clip: say so (only listed entries)
//
// Entry: {clip: '/clip/<song>/<ms>-<ms>.mp3', listen: 'mix'|'original', song, phrase, filename, at}
const RecentClips = (function () {
    const KEY = 'unaudio.recent', MAX = 20, SHOWN = 5;
    const CLIP = /^\/clip\/[^/?#]+\/\d+-\d+\.mp3$/;
    const VERSIONS = { mix: 'Resaltada', original: 'Original' };
    const id = e => e.listen + e.clip;      // Resaltada and Original are two files: two entries

    // Storage may be blocked or hold anything (an older version, another script): never fail on it
    function valid(e) {
        return e && CLIP.test(e.clip) && Object.hasOwn(VERSIONS, e.listen) &&
            ['song', 'phrase', 'filename'].every(k => typeof e[k] === 'string');
    }
    function read() {
        try {
            const list = JSON.parse(localStorage.getItem(KEY));
            return Array.isArray(list) ? list.filter(valid).slice(0, MAX) : [];
        } catch (err) { return []; }
    }
    function write(list) {
        try {
            if (list.length) localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)));
            else localStorage.removeItem(KEY);
        } catch (err) {}
    }

    function add(card) {
        const marks = [...card.querySelectorAll('.lyric mark')].map(m => m.textContent);
        const entry = {
            clip: card.dataset.clip, listen: card.dataset.listen, filename: card.dataset.filename,
            song: card.querySelector('h3').textContent,
            phrase: card.dataset.phrase || marks.join(' '), at: Date.now(),
        };
        if (valid(entry)) write([entry, ...read().filter(e => id(e) !== id(entry))]);
    }

    function gone(card) {
        if (!card.classList.contains('recent-item')) return;
        card.querySelector('.recent-actions').hidden = true;
        card.querySelector('.play-btn').hidden = true;
        card.querySelector('.recent-gone').hidden = false;
    }

    const section = document.getElementById('recent');      // only on the landing page
    if (!section) return { add, gone };
    const list = section.querySelector('.recent-list'), template = document.getElementById('recent-item');
    const more = section.querySelector('.recent-more');
    let expanded = false;
    const cards = () => [...list.children];

    function refresh() {
        cards().forEach((card, n) => { card.hidden = !expanded && n >= SHOWN; });
        more.hidden = expanded || cards().length <= SHOWN;
        section.hidden = !cards().length;
    }

    function makeCard(entry) {
        const card = template.content.firstElementChild.cloneNode(true);
        const out = entry.clip.replace('/clip/', entry.listen === 'mix' ? '/mix/' : '/original/');
        const phrase = entry.phrase || entry.song;
        Object.assign(card.dataset, { clip: entry.clip, listen: entry.listen, filename: entry.filename, phrase: entry.phrase });
        card.querySelector('h3').textContent = entry.song;
        card.querySelector('.recent-version').textContent = VERSIONS[entry.listen];
        card.querySelectorAll('.recent-search').forEach(a => { a.href = '/search?lyric=' + encodeURIComponent(phrase); });
        card.querySelector('.recent-phrase').textContent = phrase;
        card.querySelector('.play-btn').setAttribute('aria-label', `Escuchar: ${phrase}, ${entry.song}`);
        const audio = card.querySelector('audio');
        audio.dataset.clip = entry.clip;
        audio.src = out;
        // an error doesn't say why: ask whether the clip is gone or it was the network
        audio.addEventListener('error', () => fetch(out, { method: 'HEAD' })
            .then(r => { if (r.status === 404) gone(card); }).catch(() => {}));
        const download = card.querySelector('.download');
        download.download = entry.filename;
        download.href = out + '?name=' + encodeURIComponent(entry.filename);
        const remove = card.querySelector('.recent-remove');
        remove.setAttribute('aria-label', `Quitar de la lista: ${phrase}`);
        remove.addEventListener('click', () => {
            audio.pause();
            write(read().filter(e => id(e) !== id(entry)));
            card.remove();
            refresh();
        });
        return card;
    }

    const shown = read(), shownIds = shown.map(id).join();
    list.replaceChildren(...shown.map(makeCard));
    refresh();
    more.addEventListener('click', () => {
        expanded = true;
        refresh();
        cards()[SHOWN].querySelector('.play-btn').focus();
    });
    section.querySelector('.recent-clear').addEventListener('click', () => {
        if (!confirm('¿Borrar la lista de tus últimos audios?')) return;
        cards().forEach(card => card.querySelector('audio').pause());
        write([]);
        list.replaceChildren();
        refresh();
    });
    // Back from a search may show this page as it was left: without what was downloaded there
    addEventListener('pageshow', e => {
        if (e.persisted && read().map(id).join() !== shownIds) location.reload();
    });
    return { add, gone };
})();
