const login = document.querySelector('#login');
if (login || location.hash.startsWith('#login=')) {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const code = fragment.get('login');
    const thread = fragment.get('thread');
    const anchor = /^#thread-\d+$/.test(location.hash) ? location.hash :
        (/^\d+$/.test(thread || '') ? '#thread-' + thread : '');
    if (anchor) {
        for (const next of document.querySelectorAll('.sign input[name=next]')) {
            if (next.value.startsWith('/conversation/')) next.value += anchor;
        }
    }
    if (code) {
        history.replaceState(null, '', location.pathname + anchor);
        if (login) {
            login.elements.code.value = code;
            login.requestSubmit();
        } else if (thread) {
            document.getElementById('thread-' + thread)?.scrollIntoView();
        }
    }
}
for (const editor of document.querySelectorAll('[data-editor]')) {
    const field = editor.querySelector('textarea');
    const toggle = editor.querySelector('[data-preview]');
    const preview = editor.querySelector('.text-preview');
    function showPreview(show) {
        preview.textContent = field.value || 'Nothing to preview.';
        preview.hidden = !show;
        field.hidden = show;
        toggle.textContent = show ? 'Write' : 'Preview';
        toggle.setAttribute('aria-pressed', String(show));
    }
    toggle.hidden = false;
    toggle.addEventListener('click', () => showPreview(preview.hidden));
    field.addEventListener('invalid', () => showPreview(false));
}
for (const strip of document.querySelectorAll('.attachment-strip')) {
    const input = strip.querySelector('input');
    const field = input.form.querySelector('textarea');
    const list = strip.querySelector('span');
    const show = () => { list.textContent = Array.from(input.files, file => file.name).join(', '); };
    const addFiles = files => {
        const transfer = new DataTransfer();
        for (const file of [...input.files, ...files]) transfer.items.add(file);
        input.files = transfer.files;
        show();
    };
    input.addEventListener('change', show);
    input.form.addEventListener('dragover', event => {
        if (event.dataTransfer.types.includes('Files')) event.preventDefault();
    });
    input.form.addEventListener('drop', event => {
        if (!event.dataTransfer.files.length) return;
        event.preventDefault();
        addFiles(event.dataTransfer.files);
    });
    field.addEventListener('paste', event => {
        if (!event.clipboardData.files.length) return;
        event.preventDefault();
        addFiles(event.clipboardData.files);
    });
}
const account = document.querySelector('#account');
account?.addEventListener('toggle', async () => {
    if (!account.open || account.dataset.loaded) return;
    const status = account.querySelector('p');
    status.textContent = 'Checking the Shop…';
    try {
        const response = await fetch('/account/' + account.dataset.id);
        if (!response.ok) throw new Error();
        const info = await response.json();
        status.textContent = info.found ? [info.orders + ' orders', ...info.products.map(p => p.product + ' (' + p.status + ')'), ...info.themes].join(' · ') : 'No Shop account found.';
        account.dataset.loaded = '1';
    } catch {
        status.textContent = 'Shop account lookup unavailable.';
    }
});

for (const image of document.querySelectorAll('.entry img[src^="/v2/inline/"]')) {
    const explainMissing = async () => {
        try {
            const result = await fetch(image.src, {method: 'GET'});
            if (result.status === 415) {
                const link = document.createElement('a');
                const target = new URL(image.src);
                target.searchParams.set('download', '1');
                link.href = target.href;
                link.textContent = image.alt ? 'Download original file (' + image.alt + ')' : 'Download original file';
                image.replaceWith(link);
                return;
            }
            if (result.status !== 410) return;
            const message = document.createElement('span');
            message.textContent = image.alt ? image.alt + ' (original image unavailable)' : 'Original image unavailable';
            image.replaceWith(message);
        } catch {
            // The browser's image alternative remains visible during transient network failures.
        }
    };
    image.addEventListener('error', explainMissing, {once: true});
    if (image.complete && !image.naturalWidth) explainMissing();
}

// Times are stored in UTC. Show them in the reader's zone and keep the exact instant as a tooltip.
const dateTime = new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'});
const dateOnly = new Intl.DateTimeFormat(undefined, {dateStyle: 'medium'});
for (const stamp of document.querySelectorAll('time[datetime]')) {
    const when = new Date(stamp.dateTime);
    if (!stamp.dateTime || Number.isNaN(when.getTime())) continue;
    const minutes = (Date.now() - when.getTime()) / 60000;
    const compact = stamp.closest('.row, .past, .card .eyebrow, .insights .eyebrow');
    let text = dateTime.format(when);
    if (compact && !stamp.hasAttribute('data-clock')) {
        text = minutes < 0 ? dateOnly.format(when) : minutes < 60 ? Math.max(1, Math.round(minutes)) + ' min ago' :
            minutes < 1440 ? Math.round(minutes / 60) + ' h ago' :
            minutes < 43200 ? Math.round(minutes / 1440) + ' d ago' :
            minutes < 525960 ? Math.round(minutes / 43830) + ' mo ago' : Math.round(minutes / 525960) + ' y ago';
    }
    stamp.title = dateTime.format(when);
    stamp.textContent = stamp.closest('.receipt') ? when.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'}) : text;
}

// Long quoted mail is folded so the new text of each message leads.
for (const quote of document.querySelectorAll('.entry .body blockquote, .side .body blockquote')) {
    if (quote.textContent.trim().length < 280 || quote.closest('blockquote.folded')) continue;
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'quote-toggle';
    toggle.textContent = 'Show quoted text';
    toggle.setAttribute('aria-expanded', 'false');
    quote.classList.add('folded');
    quote.before(toggle);
    toggle.addEventListener('click', () => {
        const folded = quote.classList.toggle('folded');
        toggle.textContent = folded ? 'Show quoted text' : 'Hide quoted text';
        toggle.setAttribute('aria-expanded', String(!folded));
    });
}

const locationAfterSave = new URL(location.href);
const savedAudience = locationAfterSave.searchParams.get('saved');
if (savedAudience && document.querySelector('.ticket-composer')) {
    locationAfterSave.searchParams.delete('saved');
    history.replaceState(null, '', locationAfterSave);
}

// Unsent text survives a reload of the same conversation in this tab.
for (const field of document.querySelectorAll('textarea[data-keep]')) {
    const key = 'inbox-' + field.dataset.keep;
    try {
        // Earlier releases shared one scratch field for both audiences. Recover it privately.
        if (field.id === 'instruction') {
            const legacyKey = key.replace('inbox-instruction-', 'inbox-reply-');
            const legacy = sessionStorage.getItem(legacyKey);
            if (legacy && !sessionStorage.getItem(key)) {
                sessionStorage.setItem(key, legacy);
                sessionStorage.removeItem(legacyKey);
            }
        }
        if (field.dataset.keep === savedAudience + '-' + document.querySelector('#changed')?.dataset.conversation) sessionStorage.removeItem(key);
        const kept = sessionStorage.getItem(key);
        if (kept && !field.value) field.value = kept;
        field.addEventListener('input', () => {
            if (field.value) sessionStorage.setItem(key, field.value);
            else sessionStorage.removeItem(key);
        });
        if (!field.closest('.ticket-composer')) field.form?.addEventListener('submit', () => sessionStorage.removeItem(key));
    } catch {
        // Storage can be unavailable in a private window; the composer still works.
    }
}

// Agents write to conversations too. Tell the reader before a stale form is submitted.
const changed = document.querySelector('#changed');
if (changed) {
    const check = async () => {
        if (document.hidden || !changed.hidden) return;
        try {
            const response = await fetch('/conversation-version/' + encodeURIComponent(changed.dataset.conversation));
            if (response.ok) {
                const current = await response.json();
                if (current.version !== changed.dataset.version || current.request_state !== changed.dataset.requestState) changed.hidden = false;
            }
        } catch {
            // A missed check is retried on the next interval.
        }
    };
    setInterval(check, 20000);
    document.addEventListener('visibilitychange', check);
}

const similar = document.querySelector('#similar');
similar?.addEventListener('toggle', async () => {
    if (!similar.open || similar.dataset.loaded) return;
    const status = similar.querySelector('p');
    const list = similar.querySelector('ul');
    status.textContent = 'Searching earlier conversations…';
    try {
        const response = await fetch('/similar/' + encodeURIComponent(similar.dataset.id));
        if (!response.ok) throw new Error();
        const found = (await response.json()).precedents;
        status.textContent = found.length ? '' : 'No similar answered conversation found.';
        for (const item of found) {
            const row = document.createElement('li');
            const link = document.createElement('a');
            link.href = item.web;
            link.textContent = item.subject || 'Conversation #' + item.number;
            const meta = document.createElement('small');
            meta.textContent = '#' + item.number + ' · ' + String(item.created || '').slice(0, 10) +
                (item.rating ? ' · rated ' + item.rating.replace('_', ' ') : '') +
                (item.shared_tags.length ? ' · ' + item.shared_tags.join(', ') : '');
            const more = document.createElement('details');
            const label = document.createElement('summary');
            label.textContent = 'Answer sent';
            const answer = document.createElement('p');
            answer.textContent = item.answer;
            more.append(label, answer);
            row.append(link, meta, more);
            list.append(row);
        }
        similar.dataset.loaded = '1';
    } catch {
        status.textContent = 'Similar conversations are unavailable.';
    }
});

const sendDialog = document.querySelector('#send-confirm');
let pendingSend;
for (const button of document.querySelectorAll('button[data-confirm]')) {
    button.addEventListener('click', event => {
        if (button.value === 'send-draft' && sendDialog) {
            event.preventDefault();
            pendingSend = button;
            sendDialog.querySelector('p').textContent = button.dataset.confirm;
            sendDialog.returnValue = 'cancel';
            sendDialog.showModal();
        } else if (!confirm(button.dataset.confirm)) event.preventDefault();
    });
}
sendDialog?.addEventListener('close', () => {
    const button = pendingSend;
    pendingSend = null;
    if (button && sendDialog.returnValue === 'send') button.form.requestSubmit(button);
});

for (const form of document.querySelectorAll('form.field')) {
    form.classList.add('auto');
    form.addEventListener('change', () => form.requestSubmit());
}
document.querySelector('[data-help]')?.addEventListener('click', () => document.querySelector('#shortcuts').showModal());
const rail = document.querySelector('#mailbox-views');
if (rail) {
    const small = matchMedia('(max-width: 700px)');
    const fit = () => { rail.open = !small.matches; };
    fit();
    small.addEventListener('change', fit);
}

for (const form of document.querySelectorAll('[data-rich-editor]')) {
    const editor = form.querySelector('[contenteditable]');
    form.hidden = false;
    form.addEventListener('submit', event => {
        if (!editor.innerText.trim()) {
            event.preventDefault();
            editor.focus();
            return;
        }
        form.elements.draft_rich.value = editor.innerHTML;
    });
    // Keep pasted mail as text. Existing links, emphasis and lists remain editable in place.
    editor.addEventListener('paste', event => {
        event.preventDefault();
        const selection = window.getSelection();
        if (!selection.rangeCount) return;
        const range = selection.getRangeAt(0);
        const fragment = document.createDocumentFragment();
        event.clipboardData.getData('text/plain').split(/\r?\n/).forEach((line, index) => {
            if (index) fragment.append(document.createElement('br'));
            fragment.append(document.createTextNode(line));
        });
        const last = fragment.lastChild;
        range.deleteContents();
        range.insertNode(fragment);
        selection.collapse(last, last.length);
        editor.dispatchEvent(new Event('input', {bubbles: true}));
    });
    editor.addEventListener('drop', event => event.preventDefault());
}

for (const actions of document.querySelectorAll('.draft-actions')) {
    const send = actions.querySelector('button[value="send-draft"]');
    if (!send) continue;
    const fields = [...actions.querySelectorAll('textarea, [contenteditable]')];
    const value = field => field.isContentEditable ? field.innerHTML : field.value;
    const original = fields.map(value);
    actions.addEventListener('input', () => {
        send.disabled = fields.some((field, index) => value(field) !== original[index]);
        actions.querySelector('.draft-dirty').hidden = !send.disabled;
    });
}

function ticketKey(key) {
    const targets = {e: '[data-edit-key] [contenteditable], [data-edit-key] textarea', n: '#instruction', l: '#tags', s: '#snooze input[name=until]', r: '#body'};
    if (key === 'a') {
        const send = document.querySelector('[data-send-key]');
        send?.click();
        return !!send;
    }
    const field = targets[key] && document.querySelector(targets[key]);
    if (!field) return false;
    for (let el = field.parentElement; el; el = el.parentElement) {
        if (el instanceof HTMLDetailsElement) el.open = true;
    }
    field.focus();
    return true;
}
const landing = document.querySelector('[data-landing]');
const intent = /^#desk-([aels])$/.exec(location.hash);
if (intent && document.querySelector('.ticket-grid')) {
    history.replaceState(null, '', location.pathname + location.search);
    ticketKey(intent[1]);
} else if (landing && !location.hash && !savedAudience) {
    document.fonts.ready.then(() => {
        const target = document.getElementById(landing.dataset.landing);
        target?.scrollIntoView({block: 'nearest'});
    });
}
// Keyboard: j/k through a list, Enter to open, / to search, g then a letter to switch section.
const keyed = document.querySelector('[data-keys]');
const items = keyed ? [...keyed.querySelectorAll(keyed.dataset.keys === 'rows' ? '.row' : '.card')] : [];
let cursor = -1;
let leader = 0;
const rowKeys = [...document.querySelectorAll('[data-row-key]')];
const select = (index, scroll = true) => {
    if (!items.length) return;
    items[cursor]?.classList.remove('selected');
    cursor = Math.max(0, Math.min(items.length - 1, index));
    items[cursor].classList.add('selected');
    for (const hint of rowKeys) {
        hint.hidden = 'ae'.includes(hint.dataset.rowKey) && !items[cursor].hasAttribute('data-draft');
    }
    if (scroll) {
        const link = items[cursor].matches('a') ? items[cursor] : items[cursor].querySelector('h2 a');
        link?.focus({preventScroll: true});
        items[cursor].scrollIntoView({block: 'nearest'});
    }
};
if (keyed?.dataset.keys === 'rows') select(0, false);
keyed?.addEventListener('focusin', event => {
    const index = items.indexOf(event.target.closest('.row, .card'));
    if (index >= 0) select(index, false);
});
document.addEventListener('keydown', event => {
    const target = event.target;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) {
        if (event.key === 'Escape') target.blur();
        return;
    }
    if (document.querySelector('dialog[open]')) return;
    const key = event.key;
    if (leader && Date.now() - leader < 1500) {
        leader = 0;
        if (key === 'h' && document.querySelector('#handled')) {
            const group = document.querySelector('#handled');
            group.focus();
            group.scrollIntoView({block: 'start'});
            event.preventDefault();
            return;
        }
        const place = {i: '/',  r: '/review', s: '/insights'}[key];
        if (place) { location.href = place; event.preventDefault(); }
        return;
    }
    if (key === 'g') leader = Date.now();
    else if (key === 'j') select(cursor + 1);
    else if (key === 'k') select(cursor - 1);
    else if (key === 'Enter' && items[cursor] && document.activeElement === document.body) {
        (items[cursor].matches('a') ? items[cursor] : items[cursor].querySelector('h2 a'))?.click();
    } else if (key === '/' && document.querySelector('#search')) {
        document.querySelector('#search').focus();
        event.preventDefault();
    } else if (key === 'u' && document.querySelector('#back')) document.querySelector('#back').click();
    else if (rowKeys.some(hint => !hint.hidden && hint.dataset.rowKey === key) && items[cursor]) {
        event.preventDefault();
        location.href = items[cursor].href + '#desk-' + key;
    } else if (ticketKey(key)) event.preventDefault();
    else if (key === '?') document.querySelector('#shortcuts')?.showModal();
});
