/* Google Ads – śledzenie konwersji FinderDom.pl
 * Uzupełnij FD_GADS.id (AW-XXXXXXXXX) i etykiety z Google Ads → Cele → Konwersje → „Konfiguracja tagu”.
 * Puste wartości = nic się nie wysyła. Zgody (Consent Mode) obsługuje Clickio CMP w <head>. */
window.FD_GADS = {
  id: 'AW-18518225811',
  labels: {
    valuation: '0wptCKau5KAdEJPnlv5E',        // zakup wyceny PDF (35 zł) — konto Ads 485-628-9752
    plan: '',             // zakup planu Osobisty 45 zł / Firmowy 199 zł
    signup_business: '',  // rejestracja konta firmowego (lead)
    signup: '',           // rejestracja konta prywatnego
  },
};
(function () {
  const cfg = window.FD_GADS;
  if (!cfg.id) return;
  window.dataLayer = window.dataLayer || [];
  window.gtag = window.gtag || function () { window.dataLayer.push(arguments); };
  if (document.querySelector('script[src*="gtag/js?id=' + cfg.id + '"]')) return; // tag już wstawiony statycznie w <head>
  const s = document.createElement('script');
  s.async = true;
  s.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(cfg.id);
  document.head.appendChild(s);
  gtag('js', new Date());
  gtag('config', cfg.id, { allow_enhanced_conversions: true });
})();
window.fdConversion = function (name, value, txId) {
  try {
    const cfg = window.FD_GADS;
    const label = cfg && cfg.labels && cfg.labels[name];
    if (!cfg.id || !label || typeof window.gtag !== 'function') return;
    const key = 'fd_conv_' + name + '_' + (txId || 'x');
    if (txId && sessionStorage.getItem(key)) return; // nie licz dwa razy po odświeżeniu
    if (txId) sessionStorage.setItem(key, '1');
    gtag('event', 'conversion', { send_to: cfg.id + '/' + label, value: Number(value) || 0, currency: 'PLN', transaction_id: txId || undefined });
  } catch (e) { /* ignore */ }
};
