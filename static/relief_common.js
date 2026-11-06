// Shared helpers for the ScopePay Relief pages
const R = {
  gbp: (n, d = 2) => '£' + Number(n || 0).toLocaleString('en-GB', {minimumFractionDigits: d, maximumFractionDigits: d}),
  gbp0: n => R.gbp(n, 0),
  esc: s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  ago(t) {
    const s = Date.now() / 1000 - t;
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400 * 2) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' days ago';
  },
  when: t => new Date(t * 1000).toLocaleString('en-GB', {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}),
  initials: s => (s || '?').trim().split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase(),
  async api(path, opts = {}) {
    const key = localStorageGet('reliefOfficeKey');
    const headers = {'content-type': 'application/json', ...(key ? {'x-office-key': key} : {}), ...(opts.headers || {})};
    const r = await fetch(path, {...opts, headers, body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body});
    let j = null;
    try { j = await r.json(); } catch (e) { j = {error: 'Unexpected answer from the server'}; }
    if (!r.ok) {
      const msg = j.error || (Array.isArray(j.detail) ? j.detail.map(d => d.msg).join('; ') : j.detail) || r.statusText;
      throw new Error(msg);
    }
    return j;
  },
  async pot() {
    const want = new URLSearchParams(location.search).get('pot');
    const pots = await R.api('/api/relief/pots');
    return pots.find(p => p.id === want) || pots[0] || null;
  },
  async seed(btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Loading the demo…'; }
    const {pot_id} = await R.api('/api/relief/demo/seed', {method: 'POST'});
    location.search = '?pot=' + pot_id;
  },
  nav(active) {
    const q = new URLSearchParams(location.search).get('pot');
    const p = q ? '?pot=' + encodeURIComponent(q) : '';
    const links = [['donors', '/relief', 'For donors'], ['office', '/relief/office', 'Relief office'],
                   ['claim', '/relief/claim', 'Ask for help'], ['shop', '/relief/shop', 'Shop till']];
    document.querySelector('header').innerHTML = `<b>ScopePay Relief</b><nav>${links.map(([k, h, t]) =>
      `<a class="${k === active ? 'on' : ''}" href="${h}${p}">${t}</a>`).join('')}<a href="/wallet">Wallet</a></nav>`;
  },
  empty(el, text) {
    el.innerHTML = `<section class="card" style="text-align:center;padding:40px"><h2>No relief pot yet</h2>
      <p class="muted">${text || 'Load the Al Haouz demo: a pot with fictional donors, households and shops.'}</p>
      <button class="earth" onclick="R.seed(this)">Load the Al Haouz demo</button></section>`;
  },
};
function localStorageGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } }
