/*
 * FinderDom.pl — dymek czatu AI (prawy dolny róg, wszystkie strony)
 * - tylko zalogowani (także plan Free) — niezalogowany widzi zachętę do logowania
 * - historia rozmowy trzymana po stronie serwera (/api/chat/history)
 * - limit dzienny wiadomości (ustawiany w /admin → Czat AI)
 */
(function () {
  if (window.__fdChatInit) return; window.__fdChatInit = true;
  var API = '/api/chat';
  var token = function () { try { return localStorage.getItem('finderdom_token') || ''; } catch (e) { return ''; } };

  var css = '\
#fdChatBtn{position:fixed;right:18px;bottom:18px;z-index:99990;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;font-size:26px;box-shadow:0 10px 30px rgba(37,99,235,.45);display:flex;align-items:center;justify-content:center;transition:transform .15s}\
#fdChatBtn:hover{transform:scale(1.06)}\
#fdChatBtn .fd-dot{position:absolute;top:-2px;right:-2px;width:14px;height:14px;border-radius:50%;background:#22c55e;border:2px solid #0b1220}\
#fdChat{position:fixed;right:18px;bottom:88px;z-index:99991;width:370px;max-width:calc(100vw - 24px);height:560px;max-height:calc(100vh - 110px);background:#0f172a;color:#e6edf3;border:1px solid rgba(255,255,255,.12);border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.55);display:none;flex-direction:column;overflow:hidden;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}\
#fdChat.open{display:flex}\
#fdChat .fd-head{display:flex;align-items:center;gap:10px;padding:12px 14px;background:linear-gradient(135deg,#1d4ed8,#6d28d9);color:#fff}\
#fdChat .fd-head b{font-size:15px}\
#fdChat .fd-head small{display:block;font-size:11px;opacity:.85}\
#fdChat .fd-head button{margin-left:auto;background:rgba(255,255,255,.15);border:0;color:#fff;width:34px;height:34px;border-radius:10px;cursor:pointer;font-size:16px}\
#fdChat .fd-body{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}\
#fdChat .fd-msg{max-width:86%;padding:10px 12px;border-radius:14px;font-size:14px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}\
#fdChat .fd-msg.ai{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.08);align-self:flex-start;border-bottom-left-radius:4px}\
#fdChat .fd-msg.me{background:#2563eb;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}\
#fdChat .fd-msg a{color:#93c5fd}\
#fdChat .fd-typing{align-self:flex-start;color:#8ba3d4;font-size:13px;padding:4px 12px}\
#fdChat .fd-foot{padding:10px;border-top:1px solid rgba(255,255,255,.1);background:#0b1220}\
#fdChat .fd-row{display:flex;gap:8px}\
#fdChat textarea{flex:1;resize:none;min-height:44px;max-height:110px;padding:11px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);color:#fff;font-size:14px;font-family:inherit}\
#fdChat textarea:focus{outline:none;border-color:#60a5fa}\
#fdChat .fd-send{min-width:44px;height:44px;border-radius:12px;border:0;background:#2563eb;color:#fff;font-size:18px;cursor:pointer}\
#fdChat .fd-send:disabled{opacity:.5;cursor:default}\
#fdChat .fd-meta{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#8ba3d4;margin-top:6px}\
#fdChat .fd-meta button{background:none;border:0;color:#8ba3d4;cursor:pointer;font-size:11px;text-decoration:underline}\
#fdChat .fd-login{padding:26px 20px;text-align:center;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;flex:1}\
#fdChat .fd-login p{margin:0;color:#c7d2fe;font-size:14px;line-height:1.5}\
#fdChat .fd-login a{display:inline-block;padding:12px 20px;border-radius:12px;background:#2563eb;color:#fff;font-weight:800;text-decoration:none;min-height:44px;box-sizing:border-box}\
#fdChat .fd-login a.sec{background:rgba(255,255,255,.1)}\
#fdChat .fd-chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 10px}\
#fdChat .fd-chip{font-size:12px;padding:7px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.05);color:#c7d2fe;cursor:pointer}\
@media (max-width:480px){#fdChat{right:0;bottom:0;width:100vw;max-width:100vw;height:100dvh;max-height:100dvh;border-radius:0}#fdChatBtn{bottom:16px;right:14px}}\
';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var btn = document.createElement('button');
  btn.id = 'fdChatBtn'; btn.type = 'button'; btn.setAttribute('aria-label', 'Czat z asystentem AI'); btn.title = 'Zapytaj asystenta AI';
  btn.innerHTML = '💬<span class="fd-dot"></span>';
  document.body.appendChild(btn);

  var box = document.createElement('div'); box.id = 'fdChat';
  box.innerHTML = '<div class="fd-head"><span style="font-size:22px">🤖</span><div><b>Asystent FinderDom</b><small>Odpowiada AI · pytania o portal, plany, wyceny</small></div><button type="button" id="fdChatClose" aria-label="Zamknij">✕</button></div><div id="fdChatInner" style="display:flex;flex-direction:column;flex:1;min-height:0"></div>';
  document.body.appendChild(box);

  var inner = box.querySelector('#fdChatInner');
  var state = { loaded: false, busy: false, used: 0, limit: 0 };

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function fmt(s) {
    var t = esc(s);
    t = t.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    t = t.replace(/(https?:\/\/[^\s)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    t = t.replace(/(^|[\s(])(\/(?:szukaj|wycena|kredyt|rejestracja|logowanie|konto|kontakt|pomoc|dla-biur|dla-prywatnych|admin)[^\s).,]*)/g, '$1<a href="$2">$2</a>');
    return t;
  }

  function renderLogin() {
    inner.innerHTML = '<div class="fd-login"><div style="font-size:40px">🔒</div><p><b>Czat AI jest dla zalogowanych</b><br>Konto jest darmowe — po zalogowaniu zapytasz o plany, wyceny, działanie wyszukiwarki, a jeśli trzeba, zostawisz kontakt do zespołu.</p><a href="/logowanie?next=' + encodeURIComponent(location.pathname + location.search) + '">Zaloguj się</a><a class="sec" href="/rejestracja">Załóż darmowe konto</a></div>';
  }

  function renderChat() {
    inner.innerHTML = '<div class="fd-body" id="fdChatBody"></div><div class="fd-chips" id="fdChatChips"></div><div class="fd-foot"><div class="fd-row"><textarea id="fdChatInput" rows="1" placeholder="Napisz pytanie…" maxlength="2000"></textarea><button class="fd-send" id="fdChatSend" type="button" aria-label="Wyślij">➤</button></div><div class="fd-meta"><span id="fdChatQuota"></span><button type="button" id="fdChatClear">wyczyść rozmowę</button></div></div>';
    var body = inner.querySelector('#fdChatBody');
    var input = inner.querySelector('#fdChatInput');
    var send = inner.querySelector('#fdChatSend');
    var chips = inner.querySelector('#fdChatChips');
    var quota = inner.querySelector('#fdChatQuota');

    function add(role, text) {
      var d = document.createElement('div'); d.className = 'fd-msg ' + (role === 'user' ? 'me' : 'ai'); d.innerHTML = fmt(text);
      body.appendChild(d); body.scrollTop = body.scrollHeight; return d;
    }
    function setQuota() { quota.textContent = state.limit ? ('Dziś: ' + state.used + ' / ' + state.limit + ' wiadomości') : ''; }
    function showChips(show) {
      chips.innerHTML = '';
      if (!show) return;
      ['Czym różnią się plany?', 'Jak działa wycena PDF?', 'Mam konto firmowe — co daje CRM?', 'Chcę kontakt z zespołem'].forEach(function (q) {
        var c = document.createElement('button'); c.type = 'button'; c.className = 'fd-chip'; c.textContent = q;
        c.onclick = function () { input.value = q; doSend(); }; chips.appendChild(c);
      });
    }
    function autosize() { input.style.height = 'auto'; input.style.height = Math.min(110, input.scrollHeight) + 'px'; }
    input.addEventListener('input', autosize);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); } });
    send.onclick = doSend;
    inner.querySelector('#fdChatClear').onclick = function () {
      if (!confirm('Wyczyścić historię rozmowy?')) return;
      fetch(API + '/history', { method: 'DELETE', headers: { Authorization: 'Bearer ' + token() } }).then(function () { body.innerHTML = ''; greet(); });
    };
    function greet() {
      add('assistant', 'Cześć! Jestem asystentem FinderDom. Odpowiem na pytania o wyszukiwarkę, plany, CRM, wyceny PDF i kredyt. Jeśli będzie trzeba — przekażę Twój kontakt zespołowi. W czym pomóc?');
      showChips(true);
    }

    async function doSend() {
      var text = input.value.trim(); if (!text || state.busy) return;
      state.busy = true; send.disabled = true; showChips(false);
      add('user', text); input.value = ''; autosize();
      var typing = document.createElement('div'); typing.className = 'fd-typing'; typing.textContent = 'Asystent pisze…'; body.appendChild(typing); body.scrollTop = body.scrollHeight;
      try {
        var r = await fetch(API + '/message', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token() }, body: JSON.stringify({ message: text, page: location.pathname + location.search }) });
        var d = await r.json().catch(function () { return {}; });
        typing.remove();
        if (r.status === 401) { renderLogin(); return; }
        if (!r.ok) { add('assistant', '⚠️ ' + (d.detail || 'Nie udało się wysłać wiadomości. Spróbuj ponownie.')); return; }
        add('assistant', d.reply || '…');
        if (d.lead_saved) add('assistant', '✅ Twoje dane trafiły do zespołu FinderDom.');
        state.used = d.used_today || state.used + 1; state.limit = d.limit_today || state.limit; setQuota();
      } catch (e) {
        typing.remove(); add('assistant', '⚠️ Brak połączenia. Sprawdź internet i spróbuj ponownie.');
      } finally { state.busy = false; send.disabled = false; input.focus(); }
    }

    fetch(API + '/history', { headers: { Authorization: 'Bearer ' + token() } }).then(function (r) {
      if (r.status === 401) { renderLogin(); return null; }
      return r.json();
    }).then(function (d) {
      if (!d) return;
      state.used = d.used_today || 0; state.limit = d.limit_today || 0; setQuota();
      if (d.enabled === false) { add('assistant', 'Czat AI jest chwilowo niedostępny. Napisz do nas: kontakt@finderdom.pl'); send.disabled = true; return; }
      if (d.messages && d.messages.length) { d.messages.forEach(function (m) { add(m.role, m.content); }); showChips(false); }
      else greet();
      setTimeout(function () { input.focus(); }, 50);
    }).catch(function () { add('assistant', '⚠️ Nie udało się wczytać rozmowy. Odśwież stronę.'); });
  }

  function open() {
    box.classList.add('open'); btn.innerHTML = '✕';
    if (!token()) { renderLogin(); return; }
    if (!state.loaded) { state.loaded = true; renderChat(); }
  }
  function close() { box.classList.remove('open'); btn.innerHTML = '💬<span class="fd-dot"></span>'; }
  btn.onclick = function () { box.classList.contains('open') ? close() : open(); };
  box.querySelector('#fdChatClose').onclick = close;
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && box.classList.contains('open')) close(); });
})();
