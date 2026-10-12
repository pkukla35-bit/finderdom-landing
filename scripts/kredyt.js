/*
 * FinderDom.pl — Kalkulator raty kredytu hipotecznego + oferty banków (linki afiliacyjne)
 * -------------------------------------------------------------------------------------
 * JAK DODAĆ BANK: dopisz obiekt do tablicy BANKS (nazwa, link partnerski z MyLead/Convertiser/Awin, krótka cecha).
 *   - pierwszy bank na liście = duży przycisk główny,
 *   - kolejne = lista „Inne banki” pod przyciskiem.
 * Każdy link dostaje rel="nofollow sponsored" (wymóg Google) i otwiera się w nowej karcie.
 */
window.FD_KREDYT = {
  BANKS: [
    // Expander — pośrednik, porównanie 20+ banków (MyLead, CPL) → przycisk główny
    { name: 'Expander — porównanie 20+ banków', short: 'Expander', note: 'bezpłatne porównanie ofert banków online', url: 'https://goradar.xyz/a/4xl59Tl2k6h3jk4' },
    { name: 'Bank Millennium', note: 'wniosek online w kilka minut', url: 'https://myleadltd.ebrokerpartner.pl/targetclick.php?param=eyJsZWFkSUQiOi0xLCJ0YXJnZXRJRCI6IjYwOTgiLCJwYXJ0bmVySUQiOiIzMTE1MTQiLCJkb21haW5JRCI6IjMxNTI2OCJ9' },
    { name: 'Lendi — porównywarka kredytów', note: 'oferty 20+ banków online, bezpłatny doradca', url: 'https://goradar.xyz/a/rkPJYhDWqhLG89' },
    { name: 'PKO Bank Polski', note: 'największy bank hipoteczny w Polsce', url: 'https://goradar.xyz/a/NkVN8hM62Uyz1D' },
    { name: 'Kasa Stefczyka', note: 'kredyt hipoteczny, krótki wniosek online', url: 'https://goradar.xyz/a/1wg29CqAWkFmy75' },
    // { name: 'PKO BP', note: '…', url: 'https://…' },
    // { name: 'ING Bank Śląski', note: '…', url: 'https://…' },
  ],
  AFFILIATE_NOTE: 'Linki partnerskie · bez zobowiązań · wniosek online',
  // Przycisk: bezpłatna konsultacja z doradcą (nasz formularz leadowy na /kredyt)
  SECONDARY_LABEL: '🤝 Wolisz rozmowę z doradcą? Bezpłatna konsultacja →',
  DEFAULTS: { downPct: 20, years: 30, rate: 7.0 },
};

(function () {
  const D = window.FD_KREDYT.DEFAULTS;
  const fmt = (n) => Math.round(n).toLocaleString('pl-PL');

  // Rata annuitetowa
  function monthly(principal, ratePct, years) {
    const r = ratePct / 100 / 12, n = years * 12;
    if (principal <= 0 || n <= 0) return 0;
    if (r === 0) return principal / n;
    return principal * r / (1 - Math.pow(1 + r, -n));
  }

  // Szybka rata "od" dla kart (domyślne założenia)
  window.FD_rataOd = function (price) {
    if (!price || price < 50000) return 0;
    return monthly(price * (1 - D.downPct / 100), D.rate, D.years);
  };

  function injectStyles() {
    if (document.getElementById('fd-kredyt-style')) return;
    const st = document.createElement('style');
    st.id = 'fd-kredyt-style';
    st.textContent = `
      .fdk{background:linear-gradient(135deg,rgba(255,184,0,0.08),rgba(255,255,255,0.03));border:1px solid rgba(255,184,0,0.25);border-radius:16px;padding:18px}
      .fdk-title{font-weight:900;color:#fff;font-size:15px;margin-bottom:4px}
      .fdk-sub{font-size:12px;color:#8ba3d4;margin-bottom:14px}
      .fdk-row{display:flex;gap:8px;margin-bottom:10px}
      .fdk-field{flex:1;min-width:0}
      .fdk-field label{display:block;font-size:11px;color:#8ba3d4;margin-bottom:4px;font-weight:700;letter-spacing:.3px}
      .fdk-field input,.fdk-field select{width:100%;min-height:40px;background:rgba(11,24,54,0.7);border:1px solid rgba(255,255,255,0.12);border-radius:10px;color:#fff;padding:8px 10px;font-size:14px;font-weight:700;outline:none}
      .fdk-field input:focus,.fdk-field select:focus{border-color:#FFB800}
      .fdk-result{margin:14px 0 10px;padding:14px;background:rgba(11,24,54,0.6);border-radius:12px;text-align:center}
      .fdk-result .lbl{font-size:11px;color:#8ba3d4;text-transform:uppercase;letter-spacing:1px;font-weight:800}
      .fdk-result .val{font-size:28px;font-weight:900;color:#FFB800;line-height:1.2;margin:4px 0}
      .fdk-result .meta{font-size:12px;color:#c5d0e6}
      .fdk-cta{display:block;text-align:center;min-height:44px;padding:12px 14px;line-height:1.3;box-sizing:border-box;background:linear-gradient(135deg,#FFB800,#FFA200);color:#0B1836 !important;font-weight:900;border-radius:12px;text-decoration:none;font-size:14px}
      .fdk-note{font-size:11px;color:#8ba3d4;text-align:center;margin-top:8px}
      .fdk-banks{margin-top:12px;border-top:1px dashed rgba(255,255,255,0.1);padding-top:10px}
      .fdk-banks-title{font-size:10px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:#8ba3d4;margin-bottom:6px}
      .fdk-bank{display:flex;align-items:center;gap:8px;min-height:44px;padding:8px 10px;margin-bottom:6px;background:rgba(11,24,54,0.5);border:1px solid rgba(255,255,255,0.1);border-radius:10px;color:#fff !important;text-decoration:none;font-weight:800;font-size:13px}
      .fdk-bank:hover{border-color:#FFB800}
      .fdk-bank-note{flex:1;font-weight:500;font-size:11px;color:#8ba3d4;text-align:right}
      .fdk-bank-go{color:#FFB800}
      .fdk-cta2{display:block;text-align:center;margin-top:10px;padding:10px 12px;border:1px solid rgba(255,184,0,0.35);border-radius:12px;color:#FFB800 !important;font-weight:800;font-size:12.5px;text-decoration:none;line-height:1.3}
      .fdk-disc{font-size:10px;color:#6b7fa8;margin-top:10px;line-height:1.4}
    `;
    document.head.appendChild(st);
  }

  /**
   * Renderuje kalkulator w kontenerze.
   * @param el   kontener
   * @param price cena nieruchomości (zł)
   * @param opts {compact?: boolean, source?: string}
   */
  window.FD_renderKredyt = function (el, price, opts) {
    if (!el) return;
    opts = opts || {};
    injectStyles();
    const C = window.FD_KREDYT;
    const banks = (C.BANKS || []).filter(b => b && b.url);
    const isAff = banks.length > 0;
    const p0 = Math.round(price || 600000);
    const main = banks[0];
    const ctaHref = isAff ? main.url : ('/kredyt?cena=' + p0);
    const ctaLabel = isAff ? `🏦 ${main.name} →` : '🤝 Bezpłatne porównanie ofert kredytów →';
    const ctaAttrs = isAff ? 'target="_blank" rel="nofollow sponsored noopener"' : '';
    const others = banks.slice(1);
    const esc = (t) => String(t || '').replace(/</g, '&lt;');
    const othersHtml = others.length ? `<div class="fdk-banks"><div class="fdk-banks-title">Porównywarki i banki</div>${others.map((b, i) => `<a class="fdk-bank" href="${b.url}" target="_blank" rel="nofollow sponsored noopener" data-k="bank" data-bank="${esc(b.name)}"><span>🏦 ${esc(b.name)}</span><span class="fdk-bank-note">${esc(b.note)}</span><span class="fdk-bank-go">→</span></a>`).join('')}</div>` : '';

    el.innerHTML = `
      <div class="fdk" data-fd-kredyt>
        <div class="fdk-title">💳 Kalkulator raty kredytu</div>
        <div class="fdk-sub">Policz orientacyjną ratę dla tej ceny</div>
        <div class="fdk-row">
          <div class="fdk-field"><label>Cena (zł)</label><input type="number" inputmode="numeric" data-k="price" value="${p0}" min="50000" step="10000"></div>
          <div class="fdk-field"><label>Wkład własny</label>
            <select data-k="down">${[10, 20, 30, 40, 50].map(v => `<option value="${v}" ${v === D.downPct ? 'selected' : ''}>${v}%</option>`).join('')}</select></div>
        </div>
        <div class="fdk-row">
          <div class="fdk-field"><label>Okres</label>
            <select data-k="years">${[15, 20, 25, 30, 35].map(v => `<option value="${v}" ${v === D.years ? 'selected' : ''}>${v} lat</option>`).join('')}</select></div>
          <div class="fdk-field"><label>Oprocentowanie</label><input type="number" inputmode="decimal" data-k="rate" value="${D.rate}" min="1" max="20" step="0.1"></div>
        </div>
        <div class="fdk-result">
          <div class="lbl">Rata miesięczna</div>
          <div class="val" data-k="rata">—</div>
          <div class="meta" data-k="meta"></div>
        </div>
        <a class="fdk-cta" href="${ctaHref}" ${ctaAttrs} data-k="cta">${ctaLabel}</a>
        ${isAff && main.note ? `<div class="fdk-note">${esc(main.note)} · link partnerski</div>` : ''}
        ${othersHtml}
        ${(isAff && !opts.noSecondary) ? `<a class="fdk-cta2" href="/kredyt?cena=${p0}#leadCard" data-k="cta2">${C.SECONDARY_LABEL}</a>` : ''}
        <div class="fdk-disc">Wyliczenie orientacyjne (rata równa, bez prowizji i ubezpieczeń). Rzeczywista rata zależy od oferty banku i Twojej zdolności kredytowej.</div>
      </div>`;

    const q = (k) => el.querySelector(`[data-k="${k}"]`);
    function recalc() {
      const price = parseFloat(q('price').value) || 0;
      const down = parseFloat(q('down').value) || 0;
      const years = parseInt(q('years').value) || 30;
      const rate = parseFloat(q('rate').value) || 0;
      const principal = price * (1 - down / 100);
      const m = monthly(principal, rate, years);
      const total = m * years * 12;
      q('rata').textContent = m ? fmt(m) + ' zł' : '—';
      q('meta').textContent = m ? `Kredyt ${fmt(principal)} zł · wkład ${fmt(price - principal)} zł · odsetki łącznie ${fmt(total - principal)} zł` : '';
      // Pomiar klików CTA (GA4) z kontekstem
      el.dataset.fdPrice = String(Math.round(price));
    }
    el.querySelectorAll('input,select').forEach(i => i.addEventListener('input', recalc));
    q('cta').addEventListener('click', () => {
      try { window.gtag && gtag('event', 'kredyt_cta_click', { price: el.dataset.fdPrice, source: opts.source || location.pathname, affiliate: isAff, bank: isAff ? main.name : '' }); } catch (e) {}
    });
    el.querySelectorAll('[data-k="bank"]').forEach(a => a.addEventListener('click', () => {
      try { window.gtag && gtag('event', 'kredyt_cta_click', { price: el.dataset.fdPrice, source: opts.source || location.pathname, affiliate: true, bank: a.dataset.bank }); } catch (e) {}
    }));
    recalc();
  };
})();
