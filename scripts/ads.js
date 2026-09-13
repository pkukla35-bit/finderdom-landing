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
  PLACEHOLDER: true, // TRUE = pokaż ładne placeholdery (do czasu utworzenia slotów). FALSE = pokaż prawdziwe reklamy.
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

    // Zawsze najpierw pokaż ładny placeholder — potem próbuj załadować prawdziwą reklamę
    const showPlaceholder = () => {
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
    };

    // Brak slot ID lub client — pokaż tylko placeholder
    if (!slotId || slotId.startsWith('0000') || !window.FD_ADS.CLIENT || window.FD_ADS.CLIENT.includes('XXXX')){
      showPlaceholder();
      return;
    }

    // Tryb PLACEHOLDER=true — pokaż placeholder, nie ładuj reklamy
    if (window.FD_ADS.PLACEHOLDER){
      showPlaceholder();
      return;
    }

    // Prawdziwy AdSense — pokaż placeholder JAKO FALLBACK, załaduj reklamę w tle
    showPlaceholder();
    setTimeout(() => {
      try {
        const ins = document.createElement('ins');
        ins.className = 'adsbygoogle';
        ins.style.display = 'block';
        ins.style.minHeight = '90px';
        ins.setAttribute('data-ad-client', window.FD_ADS.CLIENT);
        ins.setAttribute('data-ad-slot', slotId);
        ins.setAttribute('data-ad-format', el.dataset.format || 'auto');
        ins.setAttribute('data-full-width-responsive', 'true');
        el.innerHTML = '';
        el.appendChild(ins);
        (window.adsbygoogle = window.adsbygoogle || []).push({});
        // Po 3 sekundach sprawdź czy reklama się załadowała, jeśli nie — wróć do placeholdera
        setTimeout(() => {
          if (ins.dataset.adStatus === 'unfilled' || ins.offsetHeight < 40) {
            showPlaceholder();
          }
        }, 3000);
      } catch(e){
        console.warn('AdSense push failed:', e);
        showPlaceholder();
      }
    }, 100);
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
