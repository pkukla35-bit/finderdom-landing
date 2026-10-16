/*
 * FinderDom.pl - Ads (Google AdSense) config
 * -------------------------------------------
 * GDZIE WKLEIĆ DANE Z ADSENSE:
 *
 * 1. Po zatwierdzeniu strony przez Google AdSense:
 *    - Zaloguj: https://adsense.google.com
 *    - Otwórz zakładkę "Reklamy" -> "Bloki reklam"
 *    - Utwórz bloki reklamowe i skopiuj ID (data-ad-slot)
 *
 * 2. Zmień w tym pliku:
 *    - CLIENT: skopiuj cały "ca-pub-XXXXXXXXXXXX" z panelu AdSense
 *    - SLOTS: wklej ID bloków reklamowych (10-cyfrowe liczby)
 *    (bez ID bloki działają jako ogólne bloki Display — reklamy ruszą po zatwierdzeniu witryny)
 *
 * Reklamy są AUTOMATYCZNIE UKRYWANE dla użytkowników z planem Osobisty (45 zł) i Firmowy (199 zł).
 */
window.FD_ADS = {
  // Konto FIRMOWE: Taxigo Paweł Kukla (Profil ID: 6953-2167-7812)
  CLIENT: 'ca-pub-9328001602201523', // ← Nowe konto Organizacja
  SLOTS: {
    'listing-top':     '0000000000',  // ← Do wygenerowania po zatwierdzeniu domeny przez AdSense
    'listing-inline':  '0000000000',
    'listing-bottom':  '0000000000',
    'oferta-top':      '0000000000',
    'oferta-bottom':   '0000000000',
    'home-bottom':     '0000000000',
  },
};

(function initAds(){
  // Sprawdź czy użytkownik jest zalogowany i ma plan premium
  function isPremium(){
    try {
      const u = JSON.parse(localStorage.getItem('finderdom_user') || 'null');
      return u && u.tier && u.tier !== 'free';
    } catch(e){ return false; }
  }

  // Wstaw skrypt AdSense (potrzebny do weryfikacji witryny przez Google)
  function loadAdSenseScript(){
    if (!window.FD_ADS.CLIENT || window.FD_ADS.CLIENT.includes('XXXX')) return;
    if (document.querySelector('script[src*="adsbygoogle.js"]')) return;  // już wstawiony w <head>
    const s = document.createElement('script');
    s.async = true;
    s.src = 'https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=' + encodeURIComponent(window.FD_ADS.CLIENT);
    s.crossOrigin = 'anonymous';
    s.dataset.fdAdsense = '1';
    document.head.appendChild(s);
  }

  // Styl boksu reklamowego (wspólny dla wszystkich podstron)
  function injectStyles(){
    if (document.getElementById('fd-ads-style')) return;
    const st = document.createElement('style');
    st.id = 'fd-ads-style';
    st.textContent = `
      .fd-ad-box{position:relative;padding:22px 10px 10px;border:1px dashed rgba(255,184,0,0.25);border-radius:14px;background:rgba(255,255,255,0.02);min-height:100px}
      .fd-ad-label{position:absolute;top:6px;left:12px;font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#8ba3d4;opacity:.8}
      .fd-ad-box .adsbygoogle{display:block;min-height:90px}
    `;
    document.head.appendChild(st);
  }

  // Renderuje pojedynczy slot — PRAWDZIWY blok AdSense.
  //   data-ad-slot jest opcjonalny dla bloków Display: bez ID blok działa jako ogólny blok display
  //   (reklamy ruszą automatycznie po zatwierdzeniu witryny; po utworzeniu bloków w panelu wklej ID do SLOTS).
  window.FD_renderAdSlot = function(el){
    if (!el || el.dataset.fdRendered === '1') return;
    if (isPremium()){ el.style.display = 'none'; return; }
    if (!window.FD_ADS.CLIENT || window.FD_ADS.CLIENT.includes('XXXX')){ el.style.display = 'none'; return; }
    el.dataset.fdRendered = '1';
    injectStyles();
    const slotName = el.dataset.slot;
    const slotId = (window.FD_ADS.SLOTS || {})[slotName] || '';
    const hasSlot = slotId && !slotId.startsWith('0000') && !slotId.includes('X');

    el.innerHTML = '';
    el.classList.add('fd-ad-box');
    const label = document.createElement('div');
    label.className = 'fd-ad-label';
    label.textContent = 'Reklama';
    el.appendChild(label);

    const ins = document.createElement('ins');
    ins.className = 'adsbygoogle';
    ins.style.display = 'block';
    ins.setAttribute('data-ad-client', window.FD_ADS.CLIENT);
    if (hasSlot) ins.setAttribute('data-ad-slot', slotId);
    ins.setAttribute('data-ad-format', el.dataset.format || 'auto');
    ins.setAttribute('data-full-width-responsive', 'true');
    el.appendChild(ins);
    try {
      (window.adsbygoogle = window.adsbygoogle || []).push({});
    } catch(e){
      console.warn('AdSense push failed:', e);
    }
  };

  // Renderuje wszystkie sloty na stronie
  window.FD_renderAllAds = function(){
    document.querySelectorAll('.fd-ad-slot').forEach(FD_renderAdSlot);
  };

  // Auto-init
  loadAdSenseScript();
  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', FD_renderAllAds);
  } else {
    FD_renderAllAds();
  }

  // Odśwież reklamy gdy zmieni się status usera (np. po zalogowaniu/wylogowaniu)
  window.addEventListener('storage', function(e){
    if (e.key === 'finderdom_user' || e.key === 'finderdom_token'){
      document.querySelectorAll('.fd-ad-slot').forEach(el => {
        el.dataset.fdRendered = '';
        el.style.display = '';
      });
      FD_renderAllAds();
    }
  });
})();
