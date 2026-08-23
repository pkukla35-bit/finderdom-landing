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
 *    - PLACEHOLDER: zmień na false (żeby ukryć placeholder i wyświetlić prawdziwe reklamy)
 *
 * Reklamy są AUTOMATYCZNIE UKRYWANE dla użytkowników z planem Osobisty (35 zł) i Firmowy (199 zł).
 */
window.FD_ADS = {
  CLIENT: 'ca-pub-XXXXXXXXXXXXXXXX', // <- WKLEJ TWÓJ ID KLIENTA ADSENSE
  SLOTS: {
    'listing-top':     '0000000000',  // <- BLOK REKLAMOWY: nad wynikami wyszukiwania
    'listing-inline':  '0000000000',  // <- BLOK REKLAMOWY: między ofertami (co 8 pozycji)
    'listing-bottom':  '0000000000',  // <- BLOK REKLAMOWY: pod wynikami wyszukiwania
    'oferta-top':      '0000000000',  // <- BLOK REKLAMOWY: na górze strony oferty
    'oferta-bottom':   '0000000000',  // <- BLOK REKLAMOWY: na dole strony oferty
    'home-bottom':     '0000000000',  // <- BLOK REKLAMOWY: strona główna, na dole
  },
  PLACEHOLDER: true, // TRUE = pokaż ładne placeholdery (do czasu zatwierdzenia AdSense). FALSE = pokaż prawdziwe reklamy.
};

(function initAds(){
  // Sprawdź czy użytkownik jest zalogowany i ma plan premium
  function isPremium(){
    try {
      const u = JSON.parse(localStorage.getItem('finderdom_user') || 'null');
      return u && u.tier && u.tier !== 'free';
    } catch(e){ return false; }
  }

  // Wstaw skrypt AdSense (jeśli nie placeholder i klient skonfigurowany)
  function loadAdSenseScript(){
    if (window.FD_ADS.PLACEHOLDER) return;
    if (!window.FD_ADS.CLIENT || window.FD_ADS.CLIENT.includes('XXXX')) return;
    if (document.querySelector('script[data-fd-adsense]')) return;
    const s = document.createElement('script');
    s.async = true;
    s.src = 'https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=' + encodeURIComponent(window.FD_ADS.CLIENT);
    s.crossOrigin = 'anonymous';
    s.dataset.fdAdsense = '1';
    document.head.appendChild(s);
  }

  // Renderuje pojedynczy slot
  window.FD_renderAdSlot = function(el){
    if (!el || el.dataset.fdRendered === '1') return;
    if (isPremium()){ el.style.display = 'none'; return; }
    el.dataset.fdRendered = '1';
    const slotName = el.dataset.slot;
    const slotId = (window.FD_ADS.SLOTS || {})[slotName];

    if (window.FD_ADS.PLACEHOLDER || !slotId || slotId.startsWith('0000') || !window.FD_ADS.CLIENT || window.FD_ADS.CLIENT.includes('XXXX')){
      // Ładny placeholder z informacją
      el.innerHTML = `
        <div class="fd-ad-placeholder">
          <div class="fd-ad-ph-inner">
            <span class="fd-ad-ph-icon">📢</span>
            <div>
              <div class="fd-ad-ph-title">Miejsce na reklamę</div>
              <div class="fd-ad-ph-sub">Aktywne po zatwierdzeniu AdSense</div>
            </div>
          </div>
        </div>`;
      return;
    }

    // Prawdziwy AdSense - wstaw <ins>
    const ins = document.createElement('ins');
    ins.className = 'adsbygoogle';
    ins.style.display = 'block';
    ins.setAttribute('data-ad-client', window.FD_ADS.CLIENT);
    ins.setAttribute('data-ad-slot', slotId);
    ins.setAttribute('data-ad-format', el.dataset.format || 'auto');
    ins.setAttribute('data-full-width-responsive', 'true');
    el.innerHTML = '';
    el.appendChild(ins);
    try { (window.adsbygoogle = window.adsbygoogle || []).push({}); } catch(e){ console.warn('AdSense push failed:', e); }
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
