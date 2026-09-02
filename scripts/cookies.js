/**
 * FinderDom.pl Cookie Consent Banner (RODO/GDPR)
 * Vanilla JS — brak zależności. Zgodny z wymogami AdSense.
 *
 * Zachowanie:
 *  - Pokazuje baner tylko przy pierwszej wizycie
 *  - Zapisuje zgodę w localStorage ("finderdom_cookies")
 *  - Wartości: "accepted" / "rejected" / "custom"
 *  - Wyzwala event window.dispatchEvent(new Event('cookiesUpdated'))
 *  - Publikuje globalne API: window.FDCookies.hasConsent(category)
 */
(function () {
  'use strict';
  const KEY = 'finderdom_cookies_v1';
  const stored = safeGet();

  const CATEGORIES = {
    necessary: { label: 'Niezbędne', desc: 'Wymagane do działania (logowanie, koszyk).', locked: true, on: true },
    analytics: { label: 'Analityczne', desc: 'Anonimowe statystyki użytkowania (Google Analytics).', on: false },
    marketing: { label: 'Marketingowe', desc: 'Personalizacja reklam (Google AdSense).', on: false },
  };

  window.FDCookies = {
    hasConsent(category) {
      const s = safeGet();
      if (!s) return category === 'necessary';
      if (s.state === 'accepted') return true;
      if (s.state === 'rejected') return category === 'necessary';
      return s.categories && s.categories[category] === true;
    },
    open() { showModal(false); },
    reset() { localStorage.removeItem(KEY); location.reload(); },
  };

  if (!stored) {
    // Delay 800ms po załadowaniu strony żeby nie irytować
    document.addEventListener('DOMContentLoaded', () => setTimeout(showBanner, 800));
  }

  function safeGet() {
    try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { return null; }
  }
  function safeSet(v) {
    try { localStorage.setItem(KEY, JSON.stringify(v)); } catch {}
    window.dispatchEvent(new Event('cookiesUpdated'));
  }

  function css() {
    if (document.getElementById('fdc-style')) return;
    const s = document.createElement('style');
    s.id = 'fdc-style';
    s.textContent = `
    .fdc-banner{position:fixed;bottom:16px;left:16px;right:16px;max-width:640px;margin:auto;
      background:rgba(11,24,54,0.98);backdrop-filter:blur(20px);
      border:1px solid rgba(255,184,0,0.35);border-radius:16px;
      padding:20px 22px;color:#e6e9f2;z-index:9999;
      box-shadow:0 20px 50px rgba(0,0,0,0.5);
      font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;
      animation:fdcSlide .3s ease-out}
    @keyframes fdcSlide{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
    .fdc-title{font-size:15px;font-weight:900;margin:0 0 6px;color:#FFB800}
    .fdc-desc{font-size:13px;line-height:1.5;color:#c5d0e6;margin:0 0 14px}
    .fdc-desc a{color:#FFB800;text-decoration:none;font-weight:700}
    .fdc-desc a:hover{text-decoration:underline}
    .fdc-actions{display:flex;gap:8px;flex-wrap:wrap}
    .fdc-btn{padding:10px 18px;border-radius:100px;font-size:13px;font-weight:800;
      cursor:pointer;border:none;font-family:inherit;transition:transform .12s,opacity .12s}
    .fdc-btn:hover{transform:translateY(-1px)}
    .fdc-btn-primary{background:linear-gradient(135deg,#FFB800,#FFA200);color:#0B1836}
    .fdc-btn-outline{background:transparent;color:#c5d0e6;border:1px solid rgba(255,255,255,0.2)}
    .fdc-btn-ghost{background:transparent;color:#8ba3d4;padding:10px 12px}
    .fdc-modal-bg{position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9998;
      display:flex;align-items:center;justify-content:center;padding:20px}
    .fdc-modal{background:#0B1836;border:1px solid rgba(255,184,0,0.3);
      border-radius:20px;padding:28px;max-width:520px;width:100%;color:#e6e9f2;
      max-height:90vh;overflow-y:auto;font-family:inherit}
    .fdc-modal h2{margin:0 0 8px;font-size:22px;color:#FFB800;font-weight:900}
    .fdc-modal p{color:#c5d0e6;margin:0 0 20px;font-size:14px;line-height:1.5}
    .fdc-cat{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);
      border-radius:12px;padding:14px 16px;margin-bottom:10px;
      display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
    .fdc-cat-lbl{color:#fff;font-weight:800;font-size:14px;margin-bottom:4px}
    .fdc-cat-desc{color:#8ba3d4;font-size:12px;line-height:1.4}
    .fdc-switch{position:relative;flex-shrink:0;width:42px;height:22px;
      background:rgba(255,255,255,0.15);border-radius:22px;cursor:pointer;
      transition:background .2s}
    .fdc-switch.on{background:#FFB800}
    .fdc-switch.locked{opacity:0.5;cursor:not-allowed}
    .fdc-switch:after{content:'';position:absolute;top:2px;left:2px;
      width:18px;height:18px;background:#fff;border-radius:50%;
      transition:transform .2s}
    .fdc-switch.on:after{transform:translateX(20px)}
    .fdc-modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
    @media(max-width:600px){.fdc-actions{flex-direction:column-reverse}.fdc-btn{width:100%}}
    `;
    document.head.appendChild(s);
  }

  function showBanner() {
    css();
    const el = document.createElement('div');
    el.className = 'fdc-banner';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML = `
      <div class="fdc-title">🍪 Szanujemy Twoją prywatność</div>
      <div class="fdc-desc">
        Używamy plików cookie i podobnych technologii, aby zapewnić działanie strony, analizować ruch
        i wyświetlać reklamy. Wybierz jak chcesz z nich korzystać — więcej w
        <a href="/polityka-prywatnosci">Polityce Prywatności</a>.
      </div>
      <div class="fdc-actions">
        <button class="fdc-btn fdc-btn-ghost" data-fdc="settings">Ustawienia</button>
        <button class="fdc-btn fdc-btn-outline" data-fdc="reject">Odrzuć wszystkie</button>
        <button class="fdc-btn fdc-btn-primary" data-fdc="accept">Akceptuj wszystkie</button>
      </div>
    `;
    document.body.appendChild(el);
    el.addEventListener('click', e => {
      const btn = e.target.closest('[data-fdc]');
      if (!btn) return;
      const action = btn.getAttribute('data-fdc');
      if (action === 'accept') { save('accepted'); remove(el); }
      else if (action === 'reject') { save('rejected'); remove(el); }
      else if (action === 'settings') { remove(el); showModal(true); }
    });
  }

  function showModal(fromBanner) {
    css();
    const s = safeGet() || {};
    const cats = { ...CATEGORIES };
    if (s.categories) Object.keys(cats).forEach(k => cats[k].on = !!s.categories[k]);
    const bg = document.createElement('div');
    bg.className = 'fdc-modal-bg';
    bg.innerHTML = `
      <div class="fdc-modal" role="dialog">
        <h2>🍪 Ustawienia plików cookie</h2>
        <p>Wybierz jakie kategorie plików cookie akceptujesz. Wybór można zmienić w każdej chwili.</p>
        <div id="fdc-cats">
          ${Object.keys(cats).map(k => `
            <div class="fdc-cat">
              <div>
                <div class="fdc-cat-lbl">${cats[k].label}${cats[k].locked ? ' <span style="color:#8ba3d4;font-size:11px;font-weight:600">(zawsze aktywne)</span>' : ''}</div>
                <div class="fdc-cat-desc">${cats[k].desc}</div>
              </div>
              <div class="fdc-switch ${cats[k].on ? 'on' : ''} ${cats[k].locked ? 'locked' : ''}" data-cat="${k}"></div>
            </div>
          `).join('')}
        </div>
        <div class="fdc-modal-actions">
          <button class="fdc-btn fdc-btn-outline" data-fdc="reject">Odrzuć wszystkie</button>
          <button class="fdc-btn fdc-btn-outline" data-fdc="save">Zapisz wybór</button>
          <button class="fdc-btn fdc-btn-primary" data-fdc="accept">Akceptuj wszystkie</button>
        </div>
      </div>
    `;
    document.body.appendChild(bg);

    bg.addEventListener('click', e => {
      const sw = e.target.closest('.fdc-switch');
      if (sw && !sw.classList.contains('locked')) {
        sw.classList.toggle('on');
        const k = sw.getAttribute('data-cat');
        cats[k].on = sw.classList.contains('on');
        return;
      }
      const btn = e.target.closest('[data-fdc]');
      if (!btn) return;
      const a = btn.getAttribute('data-fdc');
      if (a === 'accept') { save('accepted'); remove(bg); }
      else if (a === 'reject') { save('rejected'); remove(bg); }
      else if (a === 'save') {
        const chosen = {};
        Object.keys(cats).forEach(k => chosen[k] = cats[k].on);
        chosen.necessary = true;
        save('custom', chosen);
        remove(bg);
      }
    });
  }

  function save(state, categories) {
    if (!categories) {
      categories = {};
      Object.keys(CATEGORIES).forEach(k => {
        categories[k] = state === 'accepted' || CATEGORIES[k].locked;
      });
    }
    safeSet({ state, categories, ts: Date.now() });
  }
  function remove(el) { el.style.transition = 'opacity .2s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 200); }
})();
