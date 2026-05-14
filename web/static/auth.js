// Boardy auth helper — caricato da tutte e 4 le pagine UI.
//
// Espone:
//   await BoardyAuth.state()  → {authenticated, username?, role?}
//                                ; promise cached, fa una sola fetch a /auth/me.
//   BoardyAuth.mountBadge(headerEl) → inserisce il chip "Guest · Accedi"
//                                     oppure "username · Esci" in fondo all'header.
//   BoardyAuth.isOwner()      → bool sincrono, valido SOLO dopo che state() ha
//                                completato almeno una volta (typical pattern:
//                                await state() prima di renderizzare).
//   BoardyAuth.logout()       → POST /auth/logout, poi reload.
//
// Pattern d'uso nelle pagine:
//   <script src="/static/auth.js"></script>
//   <script>
//     (async () => {
//       const s = await BoardyAuth.state();
//       BoardyAuth.mountBadge(document.querySelector('header'));
//       if (!s.authenticated) document.body.classList.add('guest-mode');
//       // …resto del bootstrap
//     })();
//   </script>

(function () {
  let cachedPromise = null;
  let cachedState = null;

  async function state() {
    if (cachedPromise) return cachedPromise;
    cachedPromise = (async () => {
      try {
        const r = await fetch('/auth/me', { credentials: 'same-origin' });
        if (!r.ok) return { authenticated: false };
        const data = await r.json();
        cachedState = data;
        return data;
      } catch (_) {
        return { authenticated: false };
      }
    })();
    return cachedPromise;
  }

  function isOwner() {
    return !!(cachedState && cachedState.authenticated);
  }

  async function logout() {
    try {
      await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
    } catch (_) { /* even if it fails, reload — server cookie may be cleared anyway */ }
    location.reload();
  }

  function _injectCss() {
    if (document.getElementById('authBadgeCss')) return;
    const css = document.createElement('style');
    css.id = 'authBadgeCss';
    // Banner sotto la header (riga discreta verde-dim), badge in topbar a destra.
    // Inserire CSS via JS evita di toccare i CSS di ciascuna pagina.
    css.textContent = `
      .auth-badge {
        display: flex; align-items: center; gap: 8px;
        font-size: 13px; color: var(--muted);
        margin-left: auto;
      }
      .auth-badge .who { color: var(--fg); font-weight: 500; }
      .auth-badge .who.guest { color: var(--muted); font-weight: 400; }
      .auth-badge a, .auth-badge button {
        color: var(--muted); text-decoration: none;
        background: transparent;
        border: 1px solid var(--field-edge); padding: 5px 10px;
        border-radius: 8px; font-size: 12px; cursor: pointer;
        font-family: inherit;
      }
      .auth-badge a:hover, .auth-badge button:hover {
        color: var(--accent); border-color: var(--accent);
      }
      .boardy-guest-banner {
        padding: 8px 24px;
        background: rgba(95, 184, 120, 0.08);
        border-bottom: 1px solid var(--field-edge);
        color: var(--muted); font-size: 13px;
      }
      .boardy-guest-banner a { color: var(--accent); text-decoration: none; }
      .boardy-guest-banner a:hover { text-decoration: underline; }
    `;
    document.head.appendChild(css);
  }

  function _mountBanner(authenticated) {
    // Banner unico (riusato tra pagine) sotto la <header>. Rimosso quando autenticato.
    let banner = document.getElementById('boardyGuestBanner');
    if (authenticated) {
      if (banner) banner.remove();
      return;
    }
    if (banner) return; // gia' presente
    const header = document.querySelector('header');
    if (!header) return;
    const next = encodeURIComponent(location.pathname + location.search);
    banner = document.createElement('div');
    banner.id = 'boardyGuestBanner';
    banner.className = 'boardy-guest-banner';
    banner.innerHTML = `Sei in modalità guest: vedi la collezione in sola lettura e le chat non vengono salvate. <a href="/login?next=${next}">Accedi</a> per scrivere.`;
    header.insertAdjacentElement('afterend', banner);
  }

  function mountBadge(headerEl) {
    if (!headerEl) return;
    _injectCss();
    // Evita duplicati (se chiamato due volte).
    let badge = headerEl.querySelector('.auth-badge');
    if (!badge) {
      badge = document.createElement('div');
      badge.className = 'auth-badge';
      headerEl.appendChild(badge);
    }
    state().then((s) => {
      if (s.authenticated) {
        badge.innerHTML = `<span class="who">👤 ${s.username}</span><button id="authLogoutBtn" type="button">Esci</button>`;
        badge.querySelector('#authLogoutBtn').addEventListener('click', logout);
        document.body.classList.remove('guest-mode');
      } else {
        const next = encodeURIComponent(location.pathname + location.search);
        badge.innerHTML = `<span class="who guest">👤 Guest</span><a href="/login?next=${next}">Accedi</a>`;
        document.body.classList.add('guest-mode');
      }
      _mountBanner(s.authenticated);
    });
  }

  window.BoardyAuth = { state, isOwner, logout, mountBadge };
})();
