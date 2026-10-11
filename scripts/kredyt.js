/*
 * FinderDom.pl — Kalkulator raty kredytu hipotecznego + CTA afiliacyjne
 * --------------------------------------------------------------------
 * GDZIE WKLEIĆ LINK AFILIACYJNY (MyLead / Convertiser / Awin / Lendi / Notus):
 *   AFFILIATE_URL  — pełny link z Twoim ID partnera. Gdy pusty, przycisk prowadzi do poradnika kredytowego na blogu.
 *   AFFILIATE_LABEL — tekst przycisku.
 * Link afiliacyjny dostaje rel="nofollow sponsored" (wymóg Google) i otwiera się w nowej karcie.
 */
window.FD_KREDYT = {
  AFFILIATE_URL: '',
  AFFILIATE_LABEL: '🏦 Porównaj oferty kredytów →',
  AFFILIATE_NOTE: 'Bezpłatne porównanie w 20+ bankach · bez zobowiązań',
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
    const isAff = !!C.AFFILIATE_URL;
    const p0 = Math.round(price || 600000);
    const ctaHref = isAff ? C.AFFILIATE_URL : ('/kredyt?cena=' + p0);
    const ctaLabel = isAff ? C.AFFILIATE_LABEL : '🤝 Bezpłatne porównanie ofert kredytów →';
    const ctaAttrs = isAff ? 'target="_blank" rel="nofollow sponsored noopener"' : '';

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
        ${isAff ? `<div class="fdk-note">${C.AFFILIATE_NOTE}</div>` : ''}
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
      try { window.gtag && gtag('event', 'kredyt_cta_click', { price: el.dataset.fdPrice, source: opts.source || location.pathname, affiliate: isAff }); } catch (e) {}
    });
    recalc();
  };
})();
