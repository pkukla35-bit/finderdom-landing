"""
🏠 „Sprzedam / Wynajmę” (ogłoszenia właścicieli i biur dodawane ręcznie) + 🔎 „Poszukuję” (zapytania kupujących/najemców).
Wzorowane na znajdom.pl/dla-wlascicieli (27.11.2026). Rejestrowane z api/index.py przez register(app, ctx).

Kolekcje:
  listings  — ogłoszenia właścicieli jako source="finderdom", external_id="fd<ObjectId>", owner_id, status active|hidden|expired,
              expires_at (60 dni, odświeżenie przedłuża), images = ["/api/photo/<id>", …] (zdjęcia w kolekcji photos jako data URL ≤ 1280 px)
  photos    — {_id, owner_id, listing_id, data (data URL JPEG), created_at}
  wanted    — poszukiwania: user_id, name, transaction, type, city, lat/lng, price_to, area_from, rooms, market, note, phone, email, expires_at (90 dni)
Kontakt do poszukującego widzą: konta z planem Firmowym/Solo, admin oraz właściciel z aktywnym pasującym ogłoszeniem „Sprzedam”.
Kontakt do właściciela (telefon) widzi każdy zalogowany; niezalogowany — tylko informację „zaloguj się”.
"""
import base64
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import Response as RawResponse
from pydantic import BaseModel, Field

TYPES = ("mieszkanie", "dom", "dzialka", "lokal")
TYPE_LABEL = {"mieszkanie": "Mieszkanie", "dom": "Dom", "dzialka": "Działka", "lokal": "Lokal"}
LISTING_DAYS, WANTED_DAYS, MAX_PHOTOS = 60, 90, 8


class ListingIn(BaseModel):
    transaction: str = "sprzedaz"
    type: str = Field(min_length=3, max_length=12)
    title: str = Field(min_length=5, max_length=120)
    description: str = Field(default="", max_length=4000)
    price: float = Field(gt=0, lt=100_000_000)
    area_m2: Optional[float] = Field(default=None, gt=0, lt=1_000_000)
    rooms: Optional[int] = Field(default=None, ge=0, le=30)
    floor: Optional[int] = Field(default=None, ge=-1, le=60)
    build_year: Optional[int] = Field(default=None, ge=1800, le=2030)
    market: Optional[str] = None            # pierwotny | wtorny
    city: str = Field(min_length=2, max_length=80)
    district: Optional[str] = Field(default=None, max_length=80)
    street: Optional[str] = Field(default=None, max_length=120)
    phone: str = Field(min_length=7, max_length=20)
    plot_area_m2: Optional[float] = Field(default=None, gt=0, lt=100_000_000)


class PhotoIn(BaseModel):
    data_url: str


class DescribeIn(BaseModel):
    transaction: str = "sprzedaz"
    type: str = "mieszkanie"
    city: str = ""
    district: Optional[str] = None
    price: Optional[float] = None
    area_m2: Optional[float] = None
    rooms: Optional[int] = None
    floor: Optional[int] = None
    build_year: Optional[int] = None
    market: Optional[str] = None
    plot_area_m2: Optional[float] = None
    notes: str = Field(default="", max_length=1500)   # luźne hasła od właściciela: „balkon, po remoncie, blisko szkoły”


class WantedIn(BaseModel):
    transaction: str = "sprzedaz"
    type: str = "mieszkanie"
    city: str = Field(min_length=2, max_length=80)
    district: Optional[str] = Field(default=None, max_length=80)
    radius_km: int = Field(default=10, ge=0, le=100)
    price_to: float = Field(gt=0, lt=100_000_000)
    area_from: Optional[float] = Field(default=None, ge=0, lt=100_000)
    rooms: List[int] = Field(default_factory=list)
    market: Optional[str] = None            # pierwotny | wtorny | None = dowolny
    note: str = Field(default="", max_length=600)
    name: str = Field(min_length=2, max_length=40)
    phone: str = Field(min_length=7, max_length=20)
    email: Optional[str] = Field(default=None, max_length=120)   # wymagany, gdy dodaje osoba niezalogowana
    consent: bool = True


def _clean_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) < 9:
        raise HTTPException(400, "Podaj poprawny numer telefonu (9 cyfr)")
    return "+48 " + " ".join([d[-9:-6], d[-6:-3], d[-3:]]) if len(d) >= 9 else p


def _trans(t: str) -> str:
    return "wynajem" if (t or "").lower().startswith("wyn") else "sprzedaz"


def _type(t: str) -> str:
    t = (t or "").lower().strip()
    if t not in TYPES:
        raise HTTPException(400, "Nieprawidłowy rodzaj nieruchomości")
    return t


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def register(app, ctx: Dict[str, Any]) -> None:
    database, current_user, send_email, mail_layout = ctx["database"], ctx["current_user"], ctx["send_email"], ctx["mail_layout"]
    effective_tier_async, BUSINESS_TIERS, ADMIN_EMAILS = ctx["effective_tier_async"], ctx["BUSINESS_TIERS"], ctx["ADMIN_EMAILS"]
    CITY_COORDS, normalize_city, DOMAIN, ai_cfg, AI_URL, httpx, logger, escape = (
        ctx["CITY_COORDS"], ctx["normalize_city"], ctx["MAIL_PUBLIC_DOMAIN"], ctx["ai_cfg"], ctx["AI_PROXY_URL"], ctx["httpx"], ctx["logger"], ctx["escape"])
    user_from_request = ctx["user_from_request"]

    def _coords(city: str):
        return CITY_COORDS.get(normalize_city(city))

    async def _is_broker(user: Optional[dict]) -> bool:
        if not user:
            return False
        if (user.get("email") or "").lower() in ADMIN_EMAILS:
            return True
        return (await effective_tier_async(user)) in BUSINESS_TIERS

    def _now():
        return datetime.now(timezone.utc)

    # ───────────────────────── zdjęcia ─────────────────────────
    def _process_photo(data_url: str) -> str:
        from PIL import Image, ImageOps
        m = re.match(r"^data:image/(png|jpeg|jpg|webp|heic|heif);base64,(.+)$", data_url.strip(), re.S)
        if not m:
            raise HTTPException(400, "Zdjęcie musi być w formacie JPG, PNG lub WEBP")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            raise HTTPException(400, "Nie udało się odczytać zdjęcia")
        if len(raw) > 6_000_000:
            raise HTTPException(400, "Zdjęcie jest za duże — maks. 6 MB")
        try:
            im = Image.open(io.BytesIO(raw)); im.load()
            im = ImageOps.exif_transpose(im)
        except Exception:
            raise HTTPException(400, "Plik nie jest poprawnym zdjęciem")
        im = im.convert("RGB"); im.thumbnail((1280, 1280))
        out = io.BytesIO(); im.save(out, format="JPEG", quality=82, optimize=True, progressive=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()

    @app.get("/api/photo/{photo_id}")
    async def get_photo(photo_id: str):
        try:
            doc = await database().photos.find_one({"_id": ObjectId(photo_id)}, {"data": 1})
        except Exception:
            raise HTTPException(404, "Brak zdjęcia")
        if not doc:
            raise HTTPException(404, "Brak zdjęcia")
        b64 = doc["data"].split(",", 1)[1]
        return RawResponse(content=base64.b64decode(b64), media_type="image/jpeg",
                           headers={"Cache-Control": "public, max-age=2592000, immutable"})

    # ───────────────────────── ogłoszenia właścicieli ─────────────────────────
    def _public(doc: dict) -> dict:
        doc = dict(doc); doc["id"] = str(doc.pop("_id")); doc.pop("owner_id", None)
        for k in ("expires_at", "added_at", "last_seen_at", "updated_at"):
            if isinstance(doc.get(k), datetime): doc[k] = doc[k].isoformat()
        return doc

    async def _my_listing(user: dict, listing_id: str) -> dict:
        try:
            doc = await database().listings.find_one({"_id": ObjectId(listing_id), "source": "finderdom", "owner_id": str(user["_id"])})
        except Exception:
            doc = None
        if not doc:
            raise HTTPException(404, "Nie znaleziono ogłoszenia")
        return doc

    @app.post("/api/my/listings", status_code=201)
    async def create_listing(body: ListingIn, user: dict = Depends(current_user)):
        db = database()
        if await db.listings.count_documents({"source": "finderdom", "owner_id": str(user["_id"]), "status": "active"}) >= 20:
            raise HTTPException(400, "Limit 20 aktywnych ogłoszeń na konto")
        t, trans = _type(body.type), _trans(body.transaction)
        broker = await _is_broker(user)
        now = _now(); oid = ObjectId()
        city = body.city.strip()
        cc = _coords(city)
        doc = {
            "_id": oid, "source": "finderdom", "external_id": f"fd{oid}", "owner_id": str(user["_id"]), "status": "active",
            "type": TYPE_LABEL[t].lower(), "title": body.title.strip(), "description": body.description.strip()[:4000],
            "url": f"{DOMAIN}/oferta?id=fd{oid}", "city": city, "district": (body.district or "").strip() or None,
            "street": (body.street or "").strip() or None,
            "location": ", ".join(x for x in [city, (body.district or "").strip(), (body.street or "").strip()] if x),
            "price": float(body.price), "area_m2": body.area_m2, "price_pm2": round(body.price / body.area_m2, 2) if body.area_m2 else None,
            "rooms": body.rooms, "floor": body.floor, "build_year": body.build_year, "plot_area_m2": body.plot_area_m2,
            "market_type": body.market if body.market in ("pierwotny", "wtorny") else None,
            "image": None, "images": [], "transaction_type": trans,
            "seller_type": "posrednik" if broker and user.get("account_type") == "business" else "prywatna",
            "agency_name": (user.get("company_name") or None) if broker else None,
            "phone": _clean_phone(body.phone), "seller_verified": True,
            "added_at": _iso(now), "posted_at": _iso(now), "last_seen_at": _iso(now), "updated_at": now,
            "expires_at": now + timedelta(days=LISTING_DAYS), "scraped_via": "owner-form", "owner_email": user.get("email"),
        }
        if cc: doc["lat"], doc["lng"] = cc
        await db.listings.insert_one(doc)
        try:
            await _notify_seekers_about_listing(doc)
        except Exception as e:
            logger.warning("wanted→listing mail failed: %s", str(e)[:120])
        return {"ok": True, "listing": _public(doc)}

    @app.get("/api/my/listings")
    async def my_listings(user: dict = Depends(current_user)):
        cur = database().listings.find({"source": "finderdom", "owner_id": str(user["_id"])}).sort("updated_at", -1)
        return {"items": [_public(d) async for d in cur]}

    @app.patch("/api/my/listings/{listing_id}")
    async def update_listing(listing_id: str, body: ListingIn, user: dict = Depends(current_user)):
        doc = await _my_listing(user, listing_id)
        t, trans = _type(body.type), _trans(body.transaction)
        city = body.city.strip(); cc = _coords(city)
        upd = {"type": TYPE_LABEL[t].lower(), "title": body.title.strip(), "description": body.description.strip()[:4000],
               "city": city, "district": (body.district or "").strip() or None, "street": (body.street or "").strip() or None,
               "location": ", ".join(x for x in [city, (body.district or "").strip(), (body.street or "").strip()] if x),
               "price": float(body.price), "area_m2": body.area_m2, "price_pm2": round(body.price / body.area_m2, 2) if body.area_m2 else None,
               "rooms": body.rooms, "floor": body.floor, "build_year": body.build_year, "plot_area_m2": body.plot_area_m2,
               "market_type": body.market if body.market in ("pierwotny", "wtorny") else None, "transaction_type": trans,
               "phone": _clean_phone(body.phone), "updated_at": _now(), "last_seen_at": _iso(_now())}
        if cc: upd["lat"], upd["lng"] = cc
        await database().listings.update_one({"_id": doc["_id"]}, {"$set": upd})
        return {"ok": True}

    @app.post("/api/my/listings/{listing_id}/status")
    async def listing_status(listing_id: str, action: str, user: dict = Depends(current_user)):
        """action: refresh (odświeża na 60 dni i podbija datę), hide, show, delete"""
        doc = await _my_listing(user, listing_id)
        db, now = database(), _now()
        if action == "delete":
            await db.photos.delete_many({"listing_id": str(doc["_id"])})
            await db.listings.delete_one({"_id": doc["_id"]})
            return {"ok": True}
        if action == "refresh":
            if doc.get("updated_at") and (now - doc["updated_at"]).total_seconds() < 6 * 3600 and doc.get("status") == "active":
                raise HTTPException(400, "Odświeżyć można raz na 6 godzin")
            await db.listings.update_one({"_id": doc["_id"]}, {"$set": {"status": "active", "added_at": _iso(now), "last_seen_at": _iso(now),
                                         "updated_at": now, "expires_at": now + timedelta(days=LISTING_DAYS)}, "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}})
            return {"ok": True}
        if action in ("hide", "show"):
            st = "hidden" if action == "hide" else "active"
            sets = {"status": st, "updated_at": now}
            if st == "hidden": sets.update({"is_stale": True, "stale_since": now, "stale_reason": "owner-hidden"})
            await db.listings.update_one({"_id": doc["_id"]}, {"$set": sets} if st == "hidden" else {"$set": sets, "$unset": {"is_stale": "", "stale_since": "", "stale_reason": ""}})
            return {"ok": True}
        raise HTTPException(400, "Nieznana akcja")

    @app.post("/api/my/listings/{listing_id}/photo", status_code=201)
    async def add_photo(listing_id: str, body: PhotoIn, user: dict = Depends(current_user)):
        doc = await _my_listing(user, listing_id)
        if len(doc.get("images") or []) >= MAX_PHOTOS:
            raise HTTPException(400, f"Maksymalnie {MAX_PHOTOS} zdjęć")
        data = _process_photo(body.data_url)
        res = await database().photos.insert_one({"owner_id": str(user["_id"]), "listing_id": str(doc["_id"]), "data": data, "created_at": _now()})
        url = f"/api/photo/{res.inserted_id}"
        imgs = (doc.get("images") or []) + [url]
        await database().listings.update_one({"_id": doc["_id"]}, {"$set": {"images": imgs, "image": imgs[0], "updated_at": _now()}})
        return {"ok": True, "url": url, "images": imgs}

    @app.delete("/api/my/listings/{listing_id}/photo")
    async def del_photo(listing_id: str, url: str, user: dict = Depends(current_user)):
        doc = await _my_listing(user, listing_id)
        imgs = [u for u in (doc.get("images") or []) if u != url]
        pid = url.rsplit("/", 1)[-1]
        try:
            await database().photos.delete_one({"_id": ObjectId(pid), "listing_id": str(doc["_id"])})
        except Exception:
            pass
        await database().listings.update_one({"_id": doc["_id"]}, {"$set": {"images": imgs, "image": imgs[0] if imgs else None}})
        return {"ok": True, "images": imgs}

    # ───────────────────────── opis AI ─────────────────────────
    @app.post("/api/ai/describe")
    async def ai_describe(body: DescribeIn, user: dict = Depends(current_user)):
        cfg = await ai_cfg()
        if not cfg.get("key"):
            raise HTTPException(503, "Generator opisów AI jest chwilowo niedostępny")
        t = _type(body.type); trans = _trans(body.transaction)
        facts = [f"rodzaj: {TYPE_LABEL[t].lower()}", f"transakcja: {'wynajem' if trans == 'wynajem' else 'sprzedaż'}", f"miejscowość: {body.city}"]
        if body.district: facts.append(f"dzielnica/okolica: {body.district}")
        if body.price: facts.append(f"cena: {int(body.price):,} zł".replace(",", " ") + ("/mies." if trans == "wynajem" else ""))
        if body.area_m2: facts.append(f"powierzchnia: {body.area_m2:g} m²")
        if body.rooms: facts.append(f"pokoje: {body.rooms}")
        if body.floor is not None: facts.append(f"piętro: {'parter' if body.floor == 0 else body.floor}")
        if body.build_year: facts.append(f"rok budowy: {body.build_year}")
        if body.market: facts.append(f"rynek: {body.market}")
        if body.plot_area_m2: facts.append(f"działka: {body.plot_area_m2:g} m²")
        if body.notes.strip(): facts.append(f"uwagi właściciela: {body.notes.strip()}")
        system = ("Piszesz ogłoszenia nieruchomości po polsku dla portalu FinderDom.pl. Styl: konkretny, ciepły, bez przesady i bez pustych frazesów "
                  "(„wymarzone”, „niepowtarzalna okazja”). 3 akapity, łącznie 120–200 słów: 1) co to jest i gdzie, 2) układ, stan, atuty z uwag właściciela, "
                  "3) okolica i dla kogo + zaproszenie do kontaktu. Używaj WYŁĄCZNIE podanych faktów — nie wymyślaj metrażu, pięter, komunikacji ani szkół, "
                  "których nie podano. Nie podawaj ceny w treści. Zwróć sam tekst opisu, bez nagłówków i bez markdown.")
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(AI_URL, headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
                                      json={"model": cfg["model"], "messages": [{"role": "system", "content": system},
                                                                                {"role": "user", "content": "Dane ogłoszenia:\n- " + "\n- ".join(facts)}],
                                            "max_tokens": 600, "temperature": 0.7})
            if r.status_code != 200:
                logger.error("AI describe %s: %s", r.status_code, r.text[:200]); raise HTTPException(502, "AI nie odpowiada. Spróbuj za chwilę.")
            text = ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
        except HTTPException:
            raise
        except Exception as e:
            logger.error("AI describe error: %s", str(e)[:200]); raise HTTPException(502, "AI nie odpowiada. Spróbuj za chwilę.")
        if not text:
            raise HTTPException(502, "AI nie zwróciło opisu")
        return {"description": text[:4000]}

    # ───────────────────────── poszukiwania ─────────────────────────
    def _wanted_public(w: dict, with_contact: bool) -> dict:
        out = {"id": str(w["_id"]), "transaction": w["transaction"], "type": w["type"], "type_label": TYPE_LABEL.get(w["type"], w["type"]),
               "city": w["city"], "district": w.get("district"), "radius_km": w.get("radius_km", 0), "price_to": w["price_to"],
               "area_from": w.get("area_from"), "rooms": w.get("rooms") or [], "market": w.get("market"), "note": w.get("note") or "",
               "name": w.get("name"), "created_at": w["created_at"].isoformat() if isinstance(w.get("created_at"), datetime) else w.get("created_at"),
               "lat": w.get("lat"), "lng": w.get("lng"), "contact_visible": with_contact}
        if with_contact:
            out["phone"], out["email"] = w.get("phone"), w.get("email")
        return out

    def _wanted_matches_listing(w: dict, l: dict) -> bool:
        if w["transaction"] != (l.get("transaction_type") or "sprzedaz"): return False
        if w["type"] != re.sub(r"\s.*", "", str(l.get("type") or "")).lower(): return False
        if normalize_city(w["city"]) != normalize_city(l.get("city") or ""): return False
        if l.get("price") and l["price"] > w["price_to"] * 1.05: return False
        if w.get("area_from") and l.get("area_m2") and l["area_m2"] < w["area_from"] * 0.9: return False
        if w.get("rooms") and l.get("rooms") and l["rooms"] not in w["rooms"]: return False
        if w.get("market") and l.get("market_type") and l["market_type"] != w["market"]: return False
        return True

    async def _owner_matches(user: Optional[dict], w: dict) -> bool:
        if not user: return False
        cur = database().listings.find({"source": "finderdom", "owner_id": str(user["_id"]), "status": "active"})
        return any(_wanted_matches_listing(w, l) async for l in cur)

    async def _notify_seekers_about_listing(l: dict):
        """Nowe ogłoszenie „Sprzedam” → mail do poszukujących z pasującymi kryteriami (oferty z portali obsługuje system alertów)."""
        q = {"active": True, "transaction": l.get("transaction_type") or "sprzedaz", "type": re.sub(r"\s.*", "", str(l.get("type") or "")).lower(),
             "city_norm": normalize_city(l.get("city") or ""), "expires_at": {"$gt": _now()}}
        sent = 0
        async for w in database().wanted.find(q).limit(50):
            if not _wanted_matches_listing(w, l) or not w.get("email"): continue
            url = f"{DOMAIN}/oferta?id={l['external_id']}"
            html = mail_layout("Nowa oferta pasuje do Twojego poszukiwania 🏠",
                               f"Właściciel właśnie dodał na FinderDom ogłoszenie, które pasuje do Twojego poszukiwania (<b>{escape(TYPE_LABEL[w['type']])}, {escape(w['city'])}, do {int(w['price_to']):,} zł</b>).".replace(",", " "),
                               [f"<b>{escape(l.get('title') or '')}</b> — {int(l['price']):,} zł".replace(",", " ") + (f", {l['area_m2']:g} m²" if l.get("area_m2") else "") + (f", {l['rooms']} pok." if l.get("rooms") else ""),
                                "To oferta bezpośrednio od właściciela — bez prowizji pośrednika.",
                                "Swoje poszukiwania zarządzasz w zakładce Moje konto → Poszukuję."],
                               "👉 Zobacz ofertę", url)
            await send_email(to=w["email"], subject=f"Nowa oferta: {l.get('title') or TYPE_LABEL[w['type']]} — {l['city']}", html=html)
            sent += 1
        return sent

    async def _notify_owners_about_wanted(w: dict):
        """Nowe „Poszukuję” → mail do właścicieli aktywnych ogłoszeń FinderDom, które pasują."""
        q = {"source": "finderdom", "status": "active", "transaction_type": w["transaction"], "type": TYPE_LABEL[w["type"]].lower(), "price": {"$lte": w["price_to"] * 1.05}}
        seen = set()
        async for l in database().listings.find(q).limit(100):
            if normalize_city(l.get("city") or "") != w["city_norm"] or not _wanted_matches_listing(w, l): continue
            email = l.get("owner_email")
            if not email or email in seen: continue
            seen.add(email)
            html = mail_layout("Ktoś szuka takiej nieruchomości jak Twoja 🔎",
                               f"Na FinderDom pojawiło się nowe poszukiwanie, do którego pasuje Twoje ogłoszenie <b>{escape(l.get('title') or '')}</b>.",
                               [f"Szuka: <b>{escape(TYPE_LABEL[w['type']])}</b> — {escape(w['city'])}" + (f", {escape(w['district'])}" if w.get("district") else "") + f", budżet do <b>{int(w['price_to']):,} zł</b>".replace(",", " "),
                                (f"Metraż od {w['area_from']:g} m²" if w.get("area_from") else "Metraż: dowolny") + (f", pokoje: {', '.join(map(str, w['rooms']))}" if w.get("rooms") else ""),
                                "Jako właściciel pasującego ogłoszenia widzisz telefon tej osoby za darmo — kliknij i zadzwoń pierwszy."],
                               "📞 Zobacz kontakt", f"{DOMAIN}/poszukiwania?id={w['_id']}")
            await send_email(to=email, subject=f"Kupujący szuka: {TYPE_LABEL[w['type']]} w {w['city']} do {int(w['price_to']):,} zł".replace(",", " "), html=html)

    # ───────────────── biura: powiadomienia o nowych poszukiwaniach z regionu ─────────────────
    def _km(a_lat, a_lng, b_lat, b_lng) -> float:
        import math
        return 2 * 6371 * math.asin(math.sqrt(math.sin(math.radians(b_lat - a_lat) / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(math.radians(b_lng - a_lng) / 2) ** 2))

    def _region_of(u: dict) -> dict:
        r = u.get("wanted_notify") or {}
        return {"mode": r.get("mode") or "all", "city": r.get("city") or "", "radius_km": int(r.get("radius_km") or 50)}

    def _broker_wants(u: dict, w: dict) -> bool:
        r = _region_of(u)
        if r["mode"] == "off": return False
        if r["mode"] == "all" or not r["city"]: return True
        if normalize_city(r["city"]) == w.get("city_norm"): return True
        cc = _coords(r["city"])
        if cc and w.get("lat") is not None:
            return _km(cc[0], cc[1], w["lat"], w["lng"]) <= r["radius_km"] + (w.get("radius_km") or 0)
        return False

    async def _notify_brokers_about_wanted(w: dict):
        """Nowe „Poszukuję” → mail do biur z aktywnym planem (Firmowy/Solo) z regionu — lead z telefonem."""
        db = database(); seen = set(); sent = 0
        cur = db.users.find({"active": {"$ne": False}, "$or": [{"tier": {"$in": list(BUSINESS_TIERS)}}, {"role": "agent"}]}, {"password_hash": 0}).limit(2000)
        async for u in cur:
            email = (u.get("email") or "").lower()
            if not email or email in seen or email == (w.get("email") or "").lower(): continue
            if (await effective_tier_async(u)) not in BUSINESS_TIERS: continue
            if not _broker_wants(u, w): continue
            seen.add(email)
            r = _region_of(u)
            where = f"{escape(w['city'])}" + (f", {escape(w['district'])}" if w.get("district") else "") + (f" (+{w['radius_km']} km)" if w.get("radius_km") else "")
            html = mail_layout("Nowy klient szuka w Twoim regionie 🔎",
                               f"Na FinderDom pojawiło się nowe poszukiwanie: <b>{escape('Wynajem' if w['transaction']=='wynajem' else 'Kupno')} — {escape(TYPE_LABEL[w['type']])}</b> w {where}.",
                               [f"Budżet do <b>{int(w['price_to']):,} zł</b>".replace(",", " ") + ("/mies." if w["transaction"] == "wynajem" else ""),
                                (f"Metraż od {w['area_from']:g} m²" if w.get("area_from") else "Metraż: dowolny") + (f", pokoje: {', '.join(map(str, w['rooms']))}" if w.get("rooms") else "") + (f", rynek {w['market']}" if w.get("market") else ""),
                                (f"Uwagi: „{escape(w['note'])}”" if w.get("note") else "Bez dodatkowych uwag"),
                                "Jako biuro z Planem Firmowym widzisz imię i telefon tej osoby — zadzwoń pierwszy, zanim zrobi to konkurencja.",
                                ("Dostajesz wszystkie poszukiwania z Polski — ustaw region działania w Moje konto → Poszukiwania, aby dostawać tylko ze swojej okolicy." if r["mode"] == "all" or not r["city"] else f"Region: {escape(r['city'])} +{r['radius_km']} km (zmień w Moje konto → Poszukiwania).")],
                               "📞 Zobacz kontakt klienta", f"{DOMAIN}/poszukiwania?id={w['_id']}")
            try:
                await send_email(to=email, subject=f"Lead: {'wynajem' if w['transaction']=='wynajem' else 'kupno'} {TYPE_LABEL[w['type']].lower()} — {w['city']} do {int(w['price_to']):,} zł".replace(",", " "), html=html)
                sent += 1
            except Exception as e:
                logger.warning("wanted→broker mail failed %s: %s", email, str(e)[:120])
            if sent >= 300: break
        return sent

    class RegionIn(BaseModel):
        mode: str = Field(default="all", pattern="^(all|region|off)$")
        city: str = Field(default="", max_length=80)
        radius_km: int = Field(default=50, ge=0, le=300)

    @app.get("/api/wlasciciele/region")
    async def get_region(user: dict = Depends(current_user)):
        return {"region": _region_of(user), "broker": await _is_broker(user)}

    @app.put("/api/wlasciciele/region")
    async def put_region(body: RegionIn, user: dict = Depends(current_user)):
        city = body.city.strip()
        if body.mode == "region" and not city:
            raise HTTPException(400, "Podaj miejscowość, wokół której działasz")
        r = {"mode": body.mode, "city": city[:80], "radius_km": body.radius_km}
        await database().users.update_one({"_id": user["_id"]}, {"$set": {"wanted_notify": r}})
        return {"ok": True, "region": r, "known_city": bool(_coords(city)) if city else None}

    @app.post("/api/wanted", status_code=201)
    async def create_wanted(body: WantedIn, request: Request):
        """Dodanie poszukiwania — zalogowany (e-mail z konta) albo anonimowo (imię + telefon + e-mail)."""
        db = database()
        user = await user_from_request(request)
        email = (user.get("email") if user else (body.email or "")).strip().lower()
        if not user:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise HTTPException(400, "Podaj prawidłowy e-mail — wyślemy na niego pasujące oferty")
            if not body.consent:
                raise HTTPException(400, "Potrzebna zgoda na kontakt od właścicieli i biur")
        owner_key = {"user_id": str(user["_id"])} if user else {"email": email}
        if await db.wanted.count_documents({**owner_key, "active": True}) >= 10:
            raise HTTPException(400, "Limit 10 aktywnych poszukiwań")
        t, trans = _type(body.type), _trans(body.transaction)
        now = _now(); city = body.city.strip(); cc = _coords(city)
        rooms = sorted({int(r) for r in body.rooms if 0 < int(r) <= 10})
        # ten sam e-mail + te same kryteria w ciągu godziny → nie dubluj (podwójne kliknięcie / banner w wyszukiwarce)
        dup = await db.wanted.find_one({"email": email, "active": True, "transaction": trans, "type": t, "city_norm": normalize_city(city), "price_to": float(body.price_to), "created_at": {"$gt": now - timedelta(hours=1)}})
        if dup:
            return {"ok": True, "wanted": _wanted_public(dup, True), "duplicate": True}
        w = {"user_id": str(user["_id"]) if user else None, "email": email, "name": body.name.strip(), "phone": _clean_phone(body.phone),
             "transaction": trans, "type": t, "city": city, "city_norm": normalize_city(city), "district": (body.district or "").strip() or None,
             "radius_km": body.radius_km, "price_to": float(body.price_to), "area_from": body.area_from or None, "rooms": rooms,
             "market": body.market if body.market in ("pierwotny", "wtorny") else None, "note": body.note.strip()[:600],
             "active": True, "created_at": now, "expires_at": now + timedelta(days=WANTED_DAYS)}
        if cc: w["lat"], w["lng"] = cc
        res = await db.wanted.insert_one(w); w["_id"] = res.inserted_id
        # alert e-mail na oferty ze WSZYSTKICH portali (istniejący system alertów)
        try:
            if email and await db.alerts.count_documents({"email": email, "active": True}) < 20:
                await db.alerts.insert_one({"email": email, "user_id": str(user["_id"]) if user else None, "type": t, "city": city, "transaction": trans,
                                            "price_to": float(body.price_to), "area_from": body.area_from or None, "rooms_from": (rooms[0] if rooms else None),
                                            "source_offer_id": None, "wanted_id": str(res.inserted_id), "active": True, "created_at": now,
                                            "last_checked_iso": now.isoformat(), "last_sent_at": None, "sent_count": 0})
        except Exception as e:
            logger.warning("wanted alert failed: %s", str(e)[:120])
        try:
            await _notify_owners_about_wanted(w)
        except Exception as e:
            logger.warning("wanted→owners mail failed: %s", str(e)[:120])
        try:
            await _notify_brokers_about_wanted(w)
        except Exception as e:
            logger.warning("wanted→brokers mail failed: %s", str(e)[:120])
        return {"ok": True, "wanted": _wanted_public(w, True)}

    @app.get("/api/wanted")
    async def list_wanted(request: Request, response: Response, transaction: Optional[str] = None, type: Optional[str] = None,
                          city: Optional[str] = None, limit: int = 60, skip: int = 0):
        db = database(); now = _now()
        q: Dict[str, Any] = {"active": True, "expires_at": {"$gt": now}}
        if transaction in ("sprzedaz", "wynajem"): q["transaction"] = transaction
        if type in TYPES: q["type"] = type
        if city: q["city_norm"] = normalize_city(city)
        user = await user_from_request(request)
        broker = await _is_broker(user)
        total = await db.wanted.count_documents(q)
        mine = []   # ogłoszenia zalogowanego właściciela — jedno zapytanie, dopasowanie w pamięci (bez N+1)
        if user and not broker:
            mine = [l async for l in db.listings.find({"source": "finderdom", "owner_id": str(user["_id"]), "status": "active"}).limit(50)]
        items = []
        async for w in db.wanted.find(q).sort("created_at", -1).skip(max(0, skip)).limit(max(1, min(limit, 100))):
            items.append(_wanted_public(w, broker or any(_wanted_matches_listing(w, l) for l in mine)))
        # mapa: liczba poszukiwań per miasto (tylko aktywne)
        pipeline = [{"$match": {"active": True, "expires_at": {"$gt": now}, "lat": {"$exists": True}}},
                    {"$group": {"_id": "$city", "n": {"$sum": 1}, "lat": {"$first": "$lat"}, "lng": {"$first": "$lng"}}}, {"$sort": {"n": -1}}, {"$limit": 300}]
        cities = [{"city": c["_id"], "n": c["n"], "lat": c["lat"], "lng": c["lng"]} async for c in db.wanted.aggregate(pipeline)]
        response.headers["Cache-Control"] = "private, max-age=30"
        return {"total": total, "items": items, "cities": cities, "broker": broker, "logged_in": bool(user)}

    @app.get("/api/wanted/{wanted_id}")
    async def get_wanted(wanted_id: str, request: Request):
        try:
            w = await database().wanted.find_one({"_id": ObjectId(wanted_id), "active": True})
        except Exception:
            w = None
        if not w:
            raise HTTPException(404, "Nie znaleziono poszukiwania")
        user = await user_from_request(request)
        allowed = await _is_broker(user) or (user and str(user["_id"]) == w["user_id"]) or await _owner_matches(user, w)
        return {"wanted": _wanted_public(w, bool(allowed)), "logged_in": bool(user)}

    @app.get("/api/my/wanted")
    async def my_wanted(user: dict = Depends(current_user)):
        cur = database().wanted.find({"user_id": str(user["_id"]), "active": True}).sort("created_at", -1)
        return {"items": [_wanted_public(w, True) async for w in cur]}

    @app.delete("/api/my/wanted/{wanted_id}")
    async def delete_wanted(wanted_id: str, user: dict = Depends(current_user)):
        db = database()
        try:   # usunięcie na wyraźne żądanie autora — kasujemy dane kontaktowe z bazy (RODO), filtr: własny wpis
            r = await db.wanted.delete_one({"_id": ObjectId(wanted_id), "user_id": str(user["_id"])})
        except Exception:
            r = None
        if not r or not r.deleted_count:
            raise HTTPException(404, "Nie znaleziono poszukiwania")
        await db.alerts.update_many({"wanted_id": wanted_id}, {"$set": {"active": False}})
        return {"ok": True}

    @app.get("/api/wlasciciele/listings")
    async def owners_listings(response: Response, transaction: Optional[str] = None, type: Optional[str] = None,
                              city: Optional[str] = None, limit: int = 60, skip: int = 0):
        """Publiczna lista + mapa ogłoszeń właścicieli (bez telefonu i e-maila) — strona /ogloszenia-wlascicieli."""
        import re as _re
        db = database(); now = _now()
        q: Dict[str, Any] = {"source": "finderdom", "status": "active", "expires_at": {"$gt": now}}
        if transaction in ("sprzedaz", "wynajem"): q["transaction_type"] = transaction
        if type in TYPES: q["type"] = TYPE_LABEL[type].lower()
        if city and city.strip(): q["city"] = {"$regex": "^" + _re.escape(city.strip()), "$options": "i"}
        total = await db.listings.count_documents(q)
        fields = ("external_id", "title", "city", "district", "type", "transaction_type", "price", "area_m2", "rooms", "floor",
                  "plot_area_m2", "images", "image", "added_at", "seller_type", "agency_name", "market_type", "lat", "lng")
        items = []
        async for d in db.listings.find(q).sort("added_at", -1).skip(max(0, skip)).limit(max(1, min(limit, 100))):
            it = {k: d.get(k) for k in fields}
            it["id"] = d["external_id"]; it["url"] = f"/oferta?id={d['external_id']}"
            if isinstance(it.get("added_at"), datetime): it["added_at"] = it["added_at"].isoformat()
            items.append(it)
        pipeline = [{"$match": {"source": "finderdom", "status": "active", "expires_at": {"$gt": now}, "lat": {"$exists": True}}},
                    {"$group": {"_id": "$city", "n": {"$sum": 1}, "lat": {"$first": "$lat"}, "lng": {"$first": "$lng"}}}, {"$sort": {"n": -1}}, {"$limit": 300}]
        cities = [{"city": c["_id"], "n": c["n"], "lat": c["lat"], "lng": c["lng"]} async for c in db.listings.aggregate(pipeline)]
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
        return {"total": total, "items": items, "cities": cities}

    @app.get("/api/wlasciciele/stats")
    async def owners_stats(response: Response):
        db = database(); now = _now()
        response.headers["Cache-Control"] = "public, max-age=300, s-maxage=600"
        return {"wanted": await db.wanted.count_documents({"active": True, "expires_at": {"$gt": now}}),
                "listings": await db.listings.count_documents({"source": "finderdom", "status": "active"})}

    async def expire_owner_content() -> dict:
        """Wygaszanie: ogłoszenia po 60 dniach → is_stale (archiwum), poszukiwania po 90 dniach → active=False. Wywoływane z tick-a."""
        db = database(); now = _now()
        r1 = await db.listings.update_many({"source": "finderdom", "status": "active", "expires_at": {"$lt": now}},
                                           {"$set": {"status": "expired", "is_stale": True, "stale_since": now, "stale_reason": "owner-expired"}})
        r2 = await db.wanted.update_many({"active": True, "expires_at": {"$lt": now}}, {"$set": {"active": False}})
        return {"listings_expired": r1.modified_count, "wanted_expired": r2.modified_count}

    ctx["expire_owner_content"] = expire_owner_content
