"""Galerie zdjęć ze stron ofert portali listowych (Domiporta, Gratka, Morizon, Adresowo, Gethome, Okolica, KRN,
Sprzedajemy, OdWłaściciela, Lento, Allegro Lokalnie, ListaPrzetargów). Lista wyników daje 1 zdjęcie — tu dociągamy do 12.

Użycie (na końcu joba importu, PRZED stale-sweep): from gallery import enrich_galleries; enrich_galleries(coll, portal, log)
Env: PORTAL_GALLERY_LIMIT (domyślnie 150 ofert/job), PORTAL_GALLERY_DELAY (0.3 s).
"""
import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

try:
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover
    _creq = None

MAX_IMAGES = 12
_SESSION = None
_H = {"Accept-Language": "pl-PL,pl;q=0.9", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _uniq(urls: List[Optional[str]]) -> List[str]:
    out: List[str] = []
    for u in urls:
        if isinstance(u, str) and u.startswith("http") and u not in out:
            out.append(u)
    return out[:MAX_IMAGES]


def _jsonld_images(html: str) -> List[str]:
    out: List[str] = []
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        for obj in (d if isinstance(d, list) else [d]):
            if not isinstance(obj, dict):
                continue
            im = obj.get("image")
            if isinstance(im, list):
                out += [x if isinstance(x, str) else (x.get("url") or x.get("contentUrl")) for x in im if x]
            elif isinstance(im, str):
                out.append(im)
            elif isinstance(im, dict):
                out.append(im.get("url") or im.get("contentUrl"))
    return _uniq(out)


def _og_image(html: str) -> List[str]:
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html) or \
        re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', html)
    return _uniq([m.group(1)] if m else [])


def _nuxt_photos(html: str, host: str) -> List[str]:
    """Gratka/Morizon: __NUXT_DATA__ (devalue — płaska tablica indeksów) → photos[].id (base64 URL źródła) → URL miniatury."""
    m = re.search(r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    def res(i, depth=0):
        v = data[i] if isinstance(i, int) and 0 <= i < len(data) else i
        if depth > 5:
            return v
        if isinstance(v, dict):
            return {k: res(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [res(x, depth + 1) for x in v]
        return v

    for v in data:
        if isinstance(v, dict) and "photos" in v and "title" in v:
            photos = res(v["photos"])
            out = []
            for p in photos if isinstance(photos, list) else []:
                if isinstance(p, dict) and p.get("id"):
                    out.append(f"https://{host}/thumb/{p['id']}/3x2_l:fill_and_crop/{p.get('name') or 'zdjecie'}.jpg")
            return _uniq(out)
    return []


def _domiporta(html: str) -> List[str]:
    imgs = [u for u in _jsonld_images(html) if "galeria.domiporta.pl" in u]
    return imgs or _og_image(html)


def _adresowo(html: str) -> List[str]:
    # galeria = slajdy swipera; inne oferty na stronie mają własne <img>, ale poza .swiper-slide
    slides = re.findall(r'<div class="[^"]*swiper-slide[^"]*">(.*?)</div>\s*</div>', html, re.S)
    out = []
    for s in slides:
        for u in re.findall(r'(?:data-src|src)="(https://s\d\.adresowa\.pl/[^"]+\.jpe?g)"', s):
            out.append(u.replace("_big.jpg", "_xbig.jpg"))
    return _uniq(out) or _og_image(html)


def _gethome(html: str) -> List[str]:
    # podpisane URL-e miniatur (nie da się zbudować samemu) — bierzemy największy wariant każdego pliku
    best: Dict[str, str] = {}
    for u in re.findall(r'https:\\?/\\?/thumbs\.gethome\.pl\\?/[^"\'\s]+', html):
        u = u.replace("\\/", "/").replace("\\u002F", "/")
        key = u.rsplit("/media/", 1)[-1]
        w = re.search(r"/(\d{2,4})x(\d{1,4})/", u)
        width = int(w.group(1)) if w else 0
        if key not in best or width > best[key][0]:
            best[key] = (width, u)
    return _uniq([v[1] for v in best.values()]) or _og_image(html)


def _okolica(html: str) -> List[str]:
    imgs = re.findall(r'https://www\.okolica\.pl/media/cache/cart_zoom/[^"\'\s]+', html)
    return _uniq(imgs) or _og_image(html)


def _krn(html: str) -> List[str]:
    out = []
    for u in re.findall(r'https://www\.krn\.pl/imager_v2\.php\?p=([A-Za-z0-9+/=]+)[^"\'\s]*', html):
        try:
            path = base64.b64decode(u + "=" * (-len(u) % 4)).decode("utf8", "ignore")
        except Exception:
            continue
        if path.startswith("/foto/oferty/"):
            out.append(f"https://www.krn.pl/imager_v2.php?p={u}&w=1200&h=900&f=3")
    return _uniq(out) or _og_image(html)


def _odwlasciciela(html: str) -> List[str]:
    paths = re.findall(r'(?:https://odwlasciciela\.pl)?(/assets/userfiles/offers/[^"\'\s]+\.(?:jpe?g|png|webp))', html, re.I)
    return _uniq(["https://odwlasciciela.pl" + p for p in paths]) or _og_image(html)


def _generic(html: str) -> List[str]:
    return _jsonld_images(html) or _og_image(html)


EXTRACTORS: Dict[str, Callable[[str], List[str]]] = {
    "domiporta": _domiporta,
    "gratka": lambda h: _nuxt_photos(h, "thumbs.cdngr.pl") or _og_image(h),
    "morizon": lambda h: _nuxt_photos(h, "img1.staticmorizon.com.pl") or _og_image(h),
    "adresowo": _adresowo,
    "gethome": _gethome,
    "okolica": _okolica,
    "krn": _krn,
    "odwlasciciela": _odwlasciciela,
    "sprzedajemy": _generic,
    "lento": _generic,
    "allegrolokalnie": _generic,
    "listaprzetargow": _generic,
    "homeportal": lambda h: _dedupe([u for u in re.findall(r'(https://homeportal\.pl/agencies/[^"\s]+?_l\.jpg/?)', h)]) or _generic(h),
    "domy": _generic,
    "klikmapa": lambda h: _dedupe(re.findall(r'(https://cdn\d*\.beldom\.pl/imgs/[^"&\s]+?_800\.jpg)', h.replace("&quot;", '"'))) or _generic(h),
    "domplace": lambda h: _dedupe(re.findall(r'(https://domplace\.pl/uploads/listings/\d+/[A-Za-z0-9-]+\.(?:webp|jpg|jpeg|png))', h.replace("\\u002F", "/").replace("\\/", "/"))) or _generic(h),
    "szybko": lambda h: _dedupe(re.findall(r'(https://mediaproxy\.szybko\.pl/1180x600/photo/asset/[^"\s]+)', h)) or _generic(h),
}


def _dedupe(urls):
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def extract_gallery(portal: str, html: str) -> List[str]:
    fn = EXTRACTORS.get(portal, _generic)
    try:
        return fn(html)
    except Exception:
        return _og_image(html)


def _fetch(url: str):
    global _SESSION
    if _SESSION is None:
        _SESSION = _creq.Session(impersonate="chrome")
    return _SESSION.get(url, headers=_H, timeout=25, allow_redirects=True)


def enrich_galleries(coll, portal: str, log, limit: Optional[int] = None) -> Dict[str, int]:
    """Najnowsze oferty portalu bez galerii (images < 2) → pobierz stronę, zapisz images (≤12). 404/410 → is_stale."""
    if _creq is None:
        log.warning("galerie %s: brak curl_cffi — pomijam", portal)
        return {}
    limit = int(limit if limit is not None else os.environ.get("PORTAL_GALLERY_LIMIT", "150"))
    delay = float(os.environ.get("PORTAL_GALLERY_DELAY", "0.3"))
    if limit <= 0:
        return {}
    q = {"source": portal, "is_stale": {"$ne": True}, "url": {"$regex": "^https?://"}, "gallery_checked_at": {"$exists": False},
         "$or": [{"images": {"$exists": False}}, {"images.1": {"$exists": False}}]}
    docs = list(coll.find(q, {"_id": 1, "url": 1}).sort("added_at", -1).limit(limit))
    if not docs:
        return {"checked": 0}
    now = datetime.now(timezone.utc)
    st = {"checked": 0, "galleries": 0, "gone": 0, "failed": 0, "blocked": 0}
    t0 = time.time()
    for d in docs:
        st["checked"] += 1
        try:
            r = _fetch(d["url"])
        except Exception as e:
            st["failed"] += 1; log.debug("galeria %s: %s", d["url"][:60], str(e)[:60]); continue
        if r.status_code in (404, 410):
            coll.update_one({"_id": d["_id"]}, {"$set": {"is_stale": True, "stale_since": now, "stale_reason": f"page_gone:gallery_{r.status_code}",
                                                        "stale_checked_at": now, "gallery_checked_at": now.isoformat()}})
            st["gone"] += 1; continue
        if r.status_code in (403, 429):
            st["blocked"] += 1
            if st["blocked"] >= 8:
                log.warning("galerie %s: portal blokuje (8× %s) — przerywam", portal, r.status_code); break
            time.sleep(3); continue
        if r.status_code != 200:
            st["failed"] += 1; continue
        imgs = extract_gallery(portal, r.text)
        upd = {"gallery_checked_at": now.isoformat()}
        if len(imgs) > 1:
            upd["images"] = imgs
            upd["image"] = imgs[0] if not d.get("image") else d.get("image")
            st["galleries"] += 1
        coll.update_one({"_id": d["_id"]}, {"$set": upd})
        if delay:
            time.sleep(delay)
    log.info("galerie %s: sprawdzono=%d galerii=%d zdjęte=%d nieudane=%d blokady=%d w %.0fs",
             portal, st["checked"], st["galleries"], st["gone"], st["failed"], st["blocked"], time.time() - t0)
    return st
