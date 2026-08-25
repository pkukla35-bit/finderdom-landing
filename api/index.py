"""
FinderDom.pl - API (auth + Stripe payments + PDF invoices)
Endpoints:
  GET  /api/health
  POST /api/auth/register
  POST /api/auth/login
  GET  /api/auth/me
  POST /api/payments/checkout
  POST /api/payments/webhook
  GET  /api/payments/verify
  GET  /api/invoices
  GET  /api/invoices/{id}/pdf
  GET  /api/leads/mine
  POST /api/leads/claim
"""
import asyncio
import io
import os
import re
import secrets
import logging
import ipaddress
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

import bcrypt
import httpx
import jwt
import stripe
from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

# --- Config ---
MONGODB_URI = os.environ.get("MONGODB_URI")
JWT_SECRET = os.environ.get("JWT_SECRET")
DB_NAME = os.environ.get("MONGODB_DB", "finderdom")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FinderDom.pl")
COMPANY_NIP = os.environ.get("COMPANY_NIP", "")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "")
DOMAIN = os.environ.get("DOMAIN", "https://finderdom.pl")
JWT_ALGORITHM = "HS256"
JWT_DAYS = 7

if not MONGODB_URI or not JWT_SECRET:
    raise RuntimeError("MONGODB_URI and JWT_SECRET must be configured")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

PLANS = {"individual": 35, "business": 199}  # gross PLN per 30 days
PLAN_LABELS = {"individual": "Osobisty", "business": "Firmowy"}
VALUATION_PRICE = 32  # PLN one-time per property valuation PDF

# Email (Emergent managed Resend)
EMAIL_BASE_URL = "https://integrations.emergentagent.com"  # constant, NOT env
EMAIL_KEY = os.environ.get("EMERGENT_EMAIL_KEY", "")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "FinderDom.pl")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "")

logger = logging.getLogger(__name__)

app = FastAPI(title="FinderDom API", docs_url="/docs", redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://finderdom.pl",
        "https://www.finderdom.pl",
        "http://localhost:3000",
        "http://localhost:8081",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Stripe-Signature"],
)

# Debug: return real error as JSON instead of generic 500
from fastapi.responses import JSONResponse
import traceback as _tb

@app.exception_handler(Exception)
async def _err(_req, exc):
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return JSONResponse(
        {"detail": f"{type(exc).__name__}: {str(exc)[:200]}",
         "trace": _tb.format_exc().split("\n")[-3:-1]},
        status_code=500,
    )

_client = None
_indexes_ready = False
security = HTTPBearer(auto_error=False)


def database():
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            MONGODB_URI, maxPoolSize=10, serverSelectionTimeoutMS=5000
        )
    return _client[DB_NAME]


async def ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    db = database()
    try:
        await db.users.create_index("email", unique=True)
    except Exception:
        pass
    try:
        await db.invoices.create_index([("user_id", 1), ("created_at", -1)])
    except Exception:
        pass
    try:
        await db.invoices.create_index("stripe_session_id", unique=True, sparse=True)
    except Exception:
        pass
    try:
        await db.leads.create_index("claim_token", unique=True, sparse=True)
        await db.leads.create_index([("city", 1), ("created_at", -1)])
        await db.leads.create_index([("claimed_by", 1), ("created_at", -1)])
    except Exception:
        pass
    # Note: _id is unique automatically; do NOT create additional unique index on it.
    _indexes_ready = True


async def users_collection():
    await ensure_indexes()
    return database().users


# --- Models ---
class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=72)
    account_type: str
    nip: Optional[str] = None
    company_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class CheckoutRequest(BaseModel):
    plan: str


# --- Helpers ---
def clean_email(email: str) -> str:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Nieprawidłowy adres email")
    return email


def money_grosze(pln) -> int:
    return int((Decimal(str(pln)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def as_dt(v):
    if not v:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def effective_tier(user: dict) -> str:
    """Return current tier, considering expiration."""
    tier = user.get("tier", "free")
    if tier == "free":
        return "free"
    exp = as_dt(user.get("expires_at"))
    if not exp or exp < datetime.now(timezone.utc):
        return "free"
    return tier


def public_user(user: dict) -> dict:
    tier_now = effective_tier(user)
    expires_at = user.get("expires_at")
    return {
        "id": str(user["_id"]),
        "email": user["email"],
        "account_type": user["account_type"],
        "nip": user.get("nip"),
        "company_name": user.get("company_name"),
        "tier": tier_now,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        "subscription_status": user.get("subscription_status", "active"),
        "created_at": user["created_at"].isoformat() if isinstance(user["created_at"], datetime) else user["created_at"],
    }


def token_for(user: dict) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user["_id"]), "type": "access", "iat": now,
         "exp": now + timedelta(days=JWT_DAYS)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


async def current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Wymagany token Bearer", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access" or not payload.get("sub"):
            raise ValueError()
        oid = ObjectId(payload["sub"])
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise HTTPException(401, "Nieprawidłowy lub wygasły token", headers={"WWW-Authenticate": "Bearer"})
    user = await (await users_collection()).find_one({"_id": oid})
    if not user:
        raise HTTPException(401, "Użytkownik nie istnieje")
    return user


async def next_invoice_number() -> str:
    now = datetime.now(timezone.utc)
    key = f"invoice:{now.year}:{now.month:02d}"
    doc = await database().counters.find_one_and_update(
        {"_id": key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return f"FV/{now.year}/{now.month:02d}/{doc['seq']:03d}"


# --- Email helpers (Emergent managed Resend) ---
_EMAIL_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl", "rebrand.ly")
_EMAIL_CRED_ASK = ("reply with your password", "reply with the code", "send your password",
                   "cvv", "send us your password", "enter your password below",
                   "confirm your card number", "your full card number", "seed phrase",
                   "recovery phrase", "verify your card", "social security number",
                   "confirm your bank details")
_EMAIL_HOSTISH = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)


def _email_host_ok(host: str) -> bool:
    if not host or "xn--" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not any(host == s or host.endswith("." + s) for s in _EMAIL_SHORTENERS)


def _email_same_site(shown: str, real: str) -> bool:
    return shown == real or real.endswith("." + shown) or shown.endswith("." + real)


class _EmailScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.urls, self.anchors = set(), [], []
        self._href, self._text = None, []
    def handle_starttag(self, tag, attrs):
        self.tags.add(tag.lower())
        self.urls += [v for k, v in attrs if k.lower() in ("href", "src") and v]
        if tag.lower() == "a":
            self._href = dict((k.lower(), v) for k, v in attrs).get("href")
            self._text = []
    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._text)))
            self._href, self._text = None, []


def _assert_safe_email(subject: str, html: str) -> None:
    scan = _EmailScan(); scan.feed(html)
    if scan.tags & {"form", "input", "textarea", "select"}:
        raise ValueError("No forms or input fields in email (G2)")
    body = f"{subject}\n{html}".lower()
    for p in _EMAIL_CRED_ASK:
        if p in body:
            raise ValueError(f"Email asks for credentials: {p!r} (G2)")
    for url in scan.urls:
        low = url.strip().lower()
        if low.startswith(("mailto:", "tel:", "cid:", "#")):
            continue
        if not low.startswith("https://"):
            raise ValueError(f"Email links must be absolute https: {url!r} (G3)")
        host = urlparse(low).hostname or ""
        if not _email_host_ok(host) or urlparse(low).username is not None:
            raise ValueError(f"Blocked URL: {url!r} (G3)")
    for href, text in scan.anchors:
        real = urlparse(href.strip().lower()).hostname or ""
        if not real:
            continue
        for m in _EMAIL_HOSTISH.finditer(text):
            if not _email_same_site(m.group(1).lower(), real):
                raise ValueError(f"Anchor text {m.group(1)!r} != host {real!r} (G3)")


async def send_email(*, to: str, subject: str, html: str) -> Optional[str]:
    """Send a transactional email via Emergent Resend. Returns provider id or None on failure."""
    if not EMAIL_KEY:
        logger.warning("EMERGENT_EMAIL_KEY not configured; skipping email to %s", to)
        return None
    _assert_safe_email(subject, html)
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    if EMAIL_REPLY_TO:
        payload["contact_email"] = EMAIL_REPLY_TO
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{EMAIL_BASE_URL}/api/v1/email/send",
                headers={"X-Email-Key": EMAIL_KEY},
                json=payload,
            )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception as e:
        logger.error("Email send failed to %s: %s", to, str(e)[:200])
        return None


# --- Lead notifications & claim ---
async def _notify_leads_to_businesses(lead_id: str, lead: dict) -> None:
    """Send email to all active Firmowy subscribers about a new lead. First-come-first-served claim."""
    try:
        db = database()
        now = datetime.now(timezone.utc)
        cursor = db.users.find({
            "tier": "business",
            "expires_at": {"$gt": now},
        })
        recipients = []
        async for u in cursor:
            e = (u.get("email") or "").strip()
            if e:
                recipients.append(e)
        if not recipients:
            logger.info("No active Firmowy subscribers to notify")
            return

        city = escape(lead.get("city") or "—")
        district = escape(lead.get("district") or "")
        loc = f"{city}" + (f", {district}" if district else "")
        typ_map = {"apartment": "Mieszkanie", "house": "Dom", "plot": "Działka"}
        typ = typ_map.get(lead.get("type", ""), escape(lead.get("type") or "Nieruchomość"))
        area = f"{int(lead['area_m2'])} m²" if lead.get("area_m2") else "—"
        reason_map = {"ciekawosc": "Sprawdza z ciekawości", "sprzedaz": "Chce sprzedać",
                      "agent": "Agent nieruchomości"}
        reason = reason_map.get(lead.get("reason", ""), "—")
        claim_url = f"{DOMAIN}/lead-claim?token={lead.get('claim_token','')}"

        subject = f"🔥 Nowy lead: {loc} ({typ}, {area})"
        html = f"""<table role="presentation" width="100%" style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;background:#f7f7f9">
<tr><td style="padding:24px">
  <div style="background:#0b1220;color:#fff;padding:20px 24px;border-radius:14px 14px 0 0">
    <h1 style="margin:0;font-size:22px;color:#FFB800">📞 Nowy klient chce wyceny</h1>
    <p style="margin:8px 0 0;color:#c5d0e6;font-size:13px">FinderDom.pl · Subskrypcja Firmowa</p>
  </div>
  <div style="background:#fff;padding:24px;border-radius:0 0 14px 14px;border:1px solid #e5e7eb;border-top:none">
    <p style="margin:0 0 16px;color:#1f2937;font-size:15px">Klient właśnie zapłacił za wycenę i wyraził zgodę na kontakt z profesjonalnym agentem nieruchomości.</p>
    <table role="presentation" width="100%" style="border-collapse:collapse;margin:16px 0">
      <tr><td style="padding:8px 0;color:#6b7280;font-size:13px;width:40%">Lokalizacja:</td>
          <td style="padding:8px 0;color:#111827;font-weight:600;font-size:14px">{loc}</td></tr>
      <tr><td style="padding:8px 0;color:#6b7280;font-size:13px">Typ nieruchomości:</td>
          <td style="padding:8px 0;color:#111827;font-weight:600;font-size:14px">{typ}</td></tr>
      <tr><td style="padding:8px 0;color:#6b7280;font-size:13px">Powierzchnia:</td>
          <td style="padding:8px 0;color:#111827;font-weight:600;font-size:14px">{area}</td></tr>
      <tr><td style="padding:8px 0;color:#6b7280;font-size:13px">Powód wyceny:</td>
          <td style="padding:8px 0;color:#111827;font-weight:600;font-size:14px">{reason}</td></tr>
    </table>
    <div style="background:#fef3c7;border:2px solid #FFB800;border-radius:12px;padding:16px;margin:20px 0;text-align:center">
      <p style="margin:0 0 8px;color:#78350f;font-size:13px;font-weight:600">⚡ WYŁĄCZNOŚĆ · Pierwszy który zabookuje wygrywa</p>
      <p style="margin:0;color:#111827;font-size:14px">Numer telefonu klienta pokaże się TYLKO pierwszemu biuru, które kliknie poniższy przycisk.</p>
    </div>
    <div style="text-align:center;margin:24px 0">
      <a href="{claim_url}" style="display:inline-block;background:#FFB800;color:#0b1220;padding:16px 32px;border-radius:12px;text-decoration:none;font-weight:700;font-size:16px">🎯 Zabookuj tego leada</a>
    </div>
    <p style="margin:16px 0 0;color:#6b7280;font-size:12px;text-align:center">Ten link zadziała tylko dla zalogowanego konta Firmowego z aktywną subskrypcją.</p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
    <p style="margin:0;color:#9ca3af;font-size:11px;text-align:center">Wysłane przez {escape(EMAIL_FROM_NAME)}. Nie prosimy o hasła ani dane karty w emailach.</p>
  </div>
</td></tr></table>"""

        for r in recipients:
            try:
                await send_email(to=r, subject=subject, html=html)
            except Exception as e:
                logger.error("Notify lead %s to %s failed: %s", lead_id, r, str(e)[:100])
    except Exception as e:
        logger.error("Notify leads to businesses failed: %s", str(e)[:200])


@app.get("/api/leads/mine")
async def list_my_leads(user: dict = Depends(current_user)):
    """List leads claimed by the current Firmowy user."""
    if effective_tier(user) != "business":
        raise HTTPException(403, "Tylko subskrypcja Firmowa")
    cursor = database().leads.find({"claimed_by": user["_id"]}).sort("claimed_at", -1).limit(100)
    out = []
    async for l in cursor:
        out.append({
            "id": str(l["_id"]),
            "phone": l.get("phone"),
            "email": l.get("email"),
            "city": l.get("city"),
            "district": l.get("district"),
            "type": l.get("type"),
            "area_m2": l.get("area_m2"),
            "reason": l.get("reason"),
            "claimed_at": (l.get("claimed_at") or l.get("created_at")).isoformat() if l.get("claimed_at") or l.get("created_at") else None,
        })
    return {"leads": out}


@app.post("/api/leads/claim")
async def claim_lead(token: str, user: dict = Depends(current_user)):
    """Atomically claim a lead. First-come-first-served. Returns phone number if success."""
    if effective_tier(user) != "business":
        raise HTTPException(403, "Tylko subskrypcja Firmowa może rezerwować leady")
    if not token or len(token) < 10:
        raise HTTPException(400, "Nieprawidłowy token")
    now = datetime.now(timezone.utc)
    # Atomic: only claim if not already claimed
    updated = await database().leads.find_one_and_update(
        {"claim_token": token, "claimed_by": None},
        {"$set": {"claimed_by": user["_id"], "claimed_at": now, "status": "claimed"}},
        return_document=ReturnDocument.AFTER,
    )
    if updated:
        return {
            "success": True,
            "lead": {
                "phone": updated.get("phone"),
                "email": updated.get("email"),
                "city": updated.get("city"),
                "district": updated.get("district"),
                "type": updated.get("type"),
                "area_m2": updated.get("area_m2"),
                "reason": updated.get("reason"),
            }
        }
    # Check if lead exists at all
    existing = await database().leads.find_one({"claim_token": token})
    if not existing:
        raise HTTPException(404, "Lead nie istnieje")
    # Already claimed - is it by this user?
    if str(existing.get("claimed_by")) == str(user["_id"]):
        return {
            "success": True,
            "already_yours": True,
            "lead": {
                "phone": existing.get("phone"),
                "email": existing.get("email"),
                "city": existing.get("city"),
                "district": existing.get("district"),
                "type": existing.get("type"),
                "area_m2": existing.get("area_m2"),
                "reason": existing.get("reason"),
            }
        }
    raise HTTPException(409, "Ten lead został już zarezerwowany przez inne biuro")


# --- Auth endpoints ---
@app.get("/api/health")
async def health():
    try:
        await database().command("ping")
        return {"ok": True, "db": "connected", "stripe": bool(STRIPE_SECRET_KEY)}
    except Exception as e:
        raise HTTPException(500, f"DB error: {str(e)[:100]}")


@app.post("/api/auth/register", status_code=201)
async def register(body: RegisterRequest):
    email = clean_email(body.email)
    account_type = body.account_type.strip().lower()
    if account_type not in ("personal", "business"):
        raise HTTPException(400, "account_type musi być 'personal' lub 'business'")

    nip = None
    company_name = None
    if account_type == "business":
        nip = re.sub(r"[\s-]", "", body.nip or "")
        if not re.fullmatch(r"\d{10}", nip):
            raise HTTPException(400, "NIP firmy musi zawierać 10 cyfr")
        company_name = (body.company_name or "").strip()
        if not company_name:
            raise HTTPException(400, "Nazwa firmy jest wymagana dla kont firmowych")

    users = await users_collection()
    if await users.find_one({"email": email}, {"_id": 1}):
        raise HTTPException(409, "Ten email jest już zarejestrowany")

    if len(body.password.encode("utf-8")) > 72:
        raise HTTPException(400, "Hasło jest zbyt długie (max 72 bajty)")
    password_hash = bcrypt.hashpw(body.password.encode("utf-8"), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc)
    user = {
        "email": email,
        "password_hash": password_hash,
        "account_type": account_type,
        "nip": nip,
        "company_name": company_name,
        "tier": "free",
        "expires_at": None,
        "subscription_status": "active",
        "payment_customer_id": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await users.insert_one(user)
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            raise HTTPException(409, "Ten email jest już zarejestrowany")
        raise
    user["_id"] = result.inserted_id
    return {"token": token_for(user), "user": public_user(user)}


@app.post("/api/auth/login")
async def login(body: LoginRequest):
    email = clean_email(body.email)
    users = await users_collection()
    user = await users.find_one({"email": email})
    valid = user and bcrypt.checkpw(
        body.password.encode("utf-8"), user["password_hash"].encode("utf-8")
    )
    if not valid:
        raise HTTPException(401, "Nieprawidłowy email lub hasło")
    return {"token": token_for(user), "user": public_user(user)}


@app.get("/api/auth/me")
async def me(user: dict = Depends(current_user)):
    return {"user": public_user(user)}


# --- Payments ---
@app.post("/api/payments/checkout")
async def create_checkout(body: CheckoutRequest, user: dict = Depends(current_user)):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Płatności są chwilowo niedostępne (Stripe nieskonfigurowany)")
    if body.plan not in PLANS:
        raise HTTPException(400, "Nieznany plan")

    def create():
        return stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card", "blik"],
            line_items=[{
                "price_data": {
                    "currency": "pln",
                    "unit_amount": money_grosze(PLANS[body.plan]),
                    "product_data": {
                        "name": f"FinderDom.pl — {PLAN_LABELS[body.plan]} (30 dni)",
                        "description": f"Dostęp do funkcji premium na 30 dni.",
                    },
                },
                "quantity": 1,
            }],
            customer_email=user["email"],
            billing_address_collection="required",
            metadata={"user_id": str(user["_id"]), "plan": body.plan},
            success_url=f"{DOMAIN}/platnosc-sukces?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{DOMAIN}/platnosc-anulowana",
            locale="pl",
        )

    try:
        session = await asyncio.to_thread(create)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {str(e)[:200]}")
    return {"url": session.url, "session_id": session.id}


async def apply_payment(user_id: str, plan: str, stripe_session_id: str, amount_grosze: int, customer_id: Optional[str] = None):
    """Idempotently upgrade user and create invoice. Safe to call twice."""
    db = database()
    try:
        oid = ObjectId(user_id)
    except Exception:
        return False
    user = await db.users.find_one({"_id": oid})
    if not user:
        return False

    now = datetime.now(timezone.utc)
    old = as_dt(user.get("expires_at"))
    start = old if old and old > now else now
    expires = start + timedelta(days=30)
    update = {
        "tier": plan,
        "expires_at": expires,
        "subscription_status": "active",
        "updated_at": now,
    }
    if customer_id:
        update["payment_customer_id"] = customer_id

    # Check if invoice already exists for this session (idempotency)
    existing = await db.invoices.find_one({"stripe_session_id": stripe_session_id})
    if existing:
        return True

    gross = Decimal(PLANS[plan])
    net = (gross / Decimal("1.23")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    vat = (gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    number = await next_invoice_number()

    invoice = {
        "user_id": user["_id"],
        "stripe_session_id": stripe_session_id,
        "amount": amount_grosze,
        "plan": plan,
        "invoice_number": number,
        "gross": str(gross),
        "net": str(net),
        "vat": str(vat),
        "buyer_email": user["email"],
        "buyer_company": user.get("company_name"),
        "buyer_nip": user.get("nip"),
        "buyer_account_type": user.get("account_type", "personal"),
        "created_at": now,
    }
    try:
        await db.invoices.insert_one(invoice)
    except DuplicateKeyError:
        return True
    await db.users.update_one({"_id": user["_id"]}, {"$set": update})
    return True


@app.post("/api/payments/webhook")
async def stripe_webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        # Webhook not configured; still accept 200 but do not process
        return {"received": True, "warning": "STRIPE_WEBHOOK_SECRET not set"}
    try:
        event = stripe.Webhook.construct_event(raw, sig, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(400, "Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid signature")

    db = database()
    try:
        await db.stripe_events.insert_one({"_id": event["id"], "created_at": datetime.now(timezone.utc)})
    except DuplicateKeyError:
        return {"received": True}

    if event["type"] == "checkout.session.completed":
        s_obj = event["data"]["object"]
        s = s_obj.to_dict() if hasattr(s_obj, "to_dict") else dict(s_obj)
        if s.get("payment_status") != "paid":
            return {"received": True}
        md = s.get("metadata") or {}

        # Handle valuation_custom: save phone lead for agent callback
        if md.get("type") == "valuation_custom":
            try:
                import json as _json
                prop = _json.loads(md.get("property_json") or "{}")
                phone = (prop.get("phone") or "").strip()
                if phone:
                    claim_token = secrets.token_urlsafe(24)
                    lead_doc = {
                        "phone": phone,
                        "email": md.get("email", ""),
                        "city": (prop.get("city") or "").strip(),
                        "district": (prop.get("district") or "").strip(),
                        "type": prop.get("type", ""),
                        "area_m2": prop.get("area_m2"),
                        "reason": prop.get("reason", ""),
                        "session_id": s.get("id"),
                        "status": "new",
                        "claim_token": claim_token,
                        "claimed_by": None,
                        "claimed_at": None,
                        "created_at": datetime.now(timezone.utc),
                    }
                    result = await database().leads.insert_one(lead_doc)
                    # Notify all Firmowy subscribers (await - Vercel serverless kills after response)
                    try:
                        await _notify_leads_to_businesses(str(result.inserted_id), lead_doc)
                    except Exception as e:
                        logger.error(f"Lead notify failed: {e}")
            except Exception as e:
                logger.error(f"Lead save failed: {e}")
            return {"received": True}

        user_id = md.get("user_id")
        plan = md.get("plan")
        if plan not in PLANS or not user_id:
            return {"received": True}
        customer = s.get("customer")
        await apply_payment(
            user_id, plan, s["id"], int(s.get("amount_total") or 0),
            customer if isinstance(customer, str) else None,
        )
    return {"received": True}


@app.get("/api/payments/verify")
async def verify_payment(session_id: str, user: dict = Depends(current_user)):
    """Fallback verification: user returns from Stripe, we check server-side."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe niedostępny")
    if not session_id:
        raise HTTPException(400, "Brak session_id")

    def retrieve():
        return stripe.checkout.Session.retrieve(session_id)

    try:
        session_obj = await asyncio.to_thread(retrieve)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {str(e)[:200]}")

    session = session_obj.to_dict() if hasattr(session_obj, "to_dict") else dict(session_obj)

    if session.get("payment_status") != "paid":
        return {"paid": False, "status": session.get("payment_status")}

    md = session.get("metadata") or {}
    if md.get("user_id") != str(user["_id"]):
        raise HTTPException(403, "Ta sesja płatności nie należy do tego użytkownika")
    plan = md.get("plan")
    if plan not in PLANS:
        raise HTTPException(400, "Nieprawidłowy plan w sesji")

    customer = session.get("customer")
    await apply_payment(
        str(user["_id"]), plan, session["id"], int(session.get("amount_total") or 0),
        customer if isinstance(customer, str) else None,
    )

    refreshed = await (await users_collection()).find_one({"_id": user["_id"]})
    return {"paid": True, "user": public_user(refreshed)}


# --- Invoices ---
@app.get("/api/invoices")
async def list_invoices(user: dict = Depends(current_user)):
    cursor = database().invoices.find({"user_id": user["_id"]}).sort("created_at", -1)
    out = []
    async for inv in cursor:
        out.append({
            "id": str(inv["_id"]),
            "invoice_number": inv["invoice_number"],
            "amount": inv["amount"],  # grosze
            "gross": inv.get("gross"),
            "plan": inv["plan"],
            "created_at": inv["created_at"].isoformat(),
        })
    return {"invoices": out}


def _register_pdf_font():
    """Try to register a Unicode TTF (Polish chars). Returns font name."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for path in [
        os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf"),
        "/var/task/api/fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("DejaVu", path))
                # Also register bold if available
                bold = path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
                if os.path.exists(bold):
                    pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
                return "DejaVu"
            except Exception:
                continue
    return "Helvetica"


def build_invoice_pdf(inv: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    )
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT

    face = _register_pdf_font()
    face_bold = "DejaVu-Bold" if face == "DejaVu" else "Helvetica-Bold"

    normal = ParagraphStyle("n", fontName=face, fontSize=10, leading=13)
    small = ParagraphStyle("s", fontName=face, fontSize=8, leading=11, textColor=colors.grey)
    title = ParagraphStyle("t", fontName=face_bold, fontSize=20, leading=24, textColor=colors.HexColor("#0B1836"))
    label = ParagraphStyle("l", fontName=face_bold, fontSize=9, leading=12, textColor=colors.HexColor("#8ba3d4"))
    right = ParagraphStyle("r", fontName=face, fontSize=10, leading=13, alignment=TA_RIGHT)
    right_b = ParagraphStyle("rb", fontName=face_bold, fontSize=13, leading=16, alignment=TA_RIGHT)

    seller_lines = [COMPANY_NAME]
    if COMPANY_NIP:
        seller_lines.append(f"NIP: {COMPANY_NIP}")
    if COMPANY_ADDRESS:
        seller_lines.append(COMPANY_ADDRESS)
    seller_html = "<br/>".join(seller_lines)

    buyer_name = inv.get("buyer_company") or inv.get("buyer_email", "")
    buyer_lines = [buyer_name]
    if inv.get("buyer_nip"):
        buyer_lines.append(f"NIP: {inv['buyer_nip']}")
    if inv.get("buyer_email") and inv.get("buyer_company"):
        buyer_lines.append(f"Email: {inv['buyer_email']}")
    buyer_html = "<br/>".join(buyer_lines)

    gross = inv.get("gross", "0")
    net = inv.get("net", "0")
    vat = inv.get("vat", "0")
    plan_label = PLAN_LABELS.get(inv.get("plan", ""), inv.get("plan", ""))
    created = inv["created_at"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created.replace("Z", "+00:00"))
    date_str = created.strftime("%Y-%m-%d")
    sale_date_str = date_str
    due_str = (created + timedelta(days=0)).strftime("%Y-%m-%d")

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm, bottomMargin=18*mm,
    )

    header_tbl = Table(
        [[Paragraph("FAKTURA VAT", title),
          Paragraph(f"<b>Nr:</b> {inv['invoice_number']}<br/>"
                    f"<b>Data wystawienia:</b> {date_str}<br/>"
                    f"<b>Data sprzedaży:</b> {sale_date_str}<br/>"
                    f"<b>Termin płatności:</b> opłacone", right)]],
        colWidths=[90*mm, 84*mm],
    )
    header_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    parties_tbl = Table(
        [[Paragraph("SPRZEDAWCA", label), Paragraph("NABYWCA", label)],
         [Paragraph(seller_html, normal), Paragraph(buyer_html, normal)]],
        colWidths=[87*mm, 87*mm],
    )
    parties_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#c5d0e6")),
    ]))

    items_tbl = Table([
        [Paragraph("<b>Lp.</b>", normal),
         Paragraph("<b>Nazwa usługi</b>", normal),
         Paragraph("<b>Ilość</b>", right),
         Paragraph("<b>Netto</b>", right),
         Paragraph("<b>VAT 23%</b>", right),
         Paragraph("<b>Brutto (PLN)</b>", right)],
        ["1",
         f"Dostęp FinderDom.pl — {plan_label}, 30 dni",
         Paragraph("1", right),
         Paragraph(f"{net}", right),
         Paragraph(f"{vat}", right),
         Paragraph(f"<b>{gross}</b>", right)],
    ], colWidths=[10*mm, 74*mm, 15*mm, 22*mm, 25*mm, 28*mm])
    items_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), face),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F7FB")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5d0e6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))

    summary_tbl = Table([
        [Paragraph("Razem netto:", normal), Paragraph(f"{net} PLN", right)],
        [Paragraph("VAT 23%:", normal), Paragraph(f"{vat} PLN", right)],
        [Paragraph("<b>DO ZAPŁATY:</b>", normal), Paragraph(f"<b>{gross} PLN</b>", right_b)],
    ], colWidths=[100*mm, 74*mm])
    summary_tbl.setStyle(TableStyle([
        ("LINEABOVE", (0, 2), (-1, 2), 1, colors.HexColor("#FFB800")),
        ("TOPPADDING", (0, 2), (-1, 2), 6),
    ]))

    story = [
        header_tbl, Spacer(1, 8*mm),
        parties_tbl, Spacer(1, 8*mm),
        items_tbl, Spacer(1, 6*mm),
        summary_tbl, Spacer(1, 10*mm),
        Paragraph("Sposób zapłaty: Stripe (karta / BLIK) — opłacone.", small),
        Paragraph(f"Faktura wygenerowana automatycznie {date_str}.", small),
    ]
    doc.build(story)
    return out.getvalue()


@app.get("/api/invoices/{invoice_id}/pdf")
async def download_invoice(invoice_id: str, user: dict = Depends(current_user)):
    try:
        oid = ObjectId(invoice_id)
    except Exception:
        raise HTTPException(400, "Nieprawidłowe ID faktury")
    inv = await database().invoices.find_one({"_id": oid, "user_id": user["_id"]})
    if not inv:
        raise HTTPException(404, "Faktura nie znaleziona")
    data = await asyncio.to_thread(build_invoice_pdf, inv)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{inv["invoice_number"].replace("/", "-")}.pdf"'
        },
    )



# --- Valuation PDF (one-time 32 PLN) ---
class ValuationCheckoutReq(BaseModel):
    listing_id: str
    email: str


class ValuationCheckoutCustomReq(BaseModel):
    property: dict
    email: str


@app.post("/api/valuation/checkout-custom")
async def valuation_checkout_custom(body: ValuationCheckoutCustomReq):
    """Wycena wprowadzona ręcznie przez właściciela (bez listing_id)."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe niedostępny")
    email = clean_email(body.email)
    prop = body.property or {}

    # Walidacja
    if not prop.get("city"):
        raise HTTPException(400, "Podaj miasto")
    if not prop.get("area_m2"):
        raise HTTPException(400, "Podaj metraż")
    if not prop.get("type"):
        raise HTTPException(400, "Wybierz typ nieruchomości")

    # Sanityzacja i skrócenie
    safe_prop = {
        "type": str(prop.get("type", ""))[:20],
        "transaction": "sprzedaz",
        "market_type": str(prop.get("market_type", "wtorny"))[:20],
        "city": str(prop.get("city", ""))[:60],
        "district": str(prop.get("district", ""))[:60],
        "location": str(prop.get("location", ""))[:120],
        "area_m2": float(prop.get("area_m2") or 0),
        "rooms": int(prop.get("rooms") or 0) or None,
        "floor": prop.get("floor"),
        "max_floor": prop.get("max_floor"),
        "build_year": prop.get("build_year"),
        "standard": str(prop.get("standard", ""))[:20],
        "elevator": str(prop.get("elevator", ""))[:10],
        "basement": str(prop.get("basement", ""))[:10],
        "parking": str(prop.get("parking", ""))[:20],
        "garden": str(prop.get("garden", ""))[:10],
        "attic": str(prop.get("attic", ""))[:20],
        "plot_area": float(prop.get("plot_area") or 0) or None,
        "land_type": str(prop.get("land_type", ""))[:30],
        "utilities": str(prop.get("utilities", ""))[:30],
        "road_access": str(prop.get("road_access", ""))[:30],
        "reason": str(prop.get("reason", ""))[:20],
        "building_type": str(prop.get("building_type", ""))[:30],
        "phone": str(prop.get("phone", ""))[:20],
        "price": int(prop.get("price") or 0) or None,
    }
    if safe_prop["price"] and safe_prop["area_m2"]:
        safe_prop["price_pm2"] = int(safe_prop["price"] / safe_prop["area_m2"])

    import json as _json
    prop_json = _json.dumps(safe_prop, ensure_ascii=False)
    if len(prop_json) > 490:
        raise HTTPException(400, "Za dużo danych - skróć adres/lokalizację")

    def create():
        return stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card", "blik"],
            line_items=[{
                "price_data": {
                    "currency": "pln",
                    "unit_amount": money_grosze(VALUATION_PRICE),
                    "product_data": {
                        "name": "FinderDom.pl — Wycena nieruchomości (PDF)",
                        "description": f"Wycena: {safe_prop['city']}, {int(safe_prop['area_m2'])} m²",
                    },
                },
                "quantity": 1,
            }],
            customer_email=email,
            metadata={"type": "valuation_custom", "property_json": prop_json, "email": email},
            success_url=f"{DOMAIN}/wycena-sukces?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{DOMAIN}/wycena",
            locale="pl",
        )

    try:
        session = await asyncio.to_thread(create)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {str(e)[:200]}")
    return {"url": session.url}


@app.post("/api/valuation/checkout")
async def valuation_checkout(body: ValuationCheckoutReq):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe niedostępny")
    email = clean_email(body.email)
    listing_id = str(body.listing_id).strip()
    if not listing_id:
        raise HTTPException(400, "Brak listing_id")

    def create():
        return stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card", "blik"],
            line_items=[{
                "price_data": {
                    "currency": "pln",
                    "unit_amount": money_grosze(VALUATION_PRICE),
                    "product_data": {
                        "name": "FinderDom.pl — Wycena nieruchomości (PDF)",
                        "description": f"Raport wyceny z analizą AI + rynek okolicy",
                    },
                },
                "quantity": 1,
            }],
            customer_email=email,
            metadata={"type": "valuation", "listing_id": listing_id, "email": email},
            success_url=f"{DOMAIN}/wycena-sukces?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{DOMAIN}/oferta.html?id={listing_id}",
            locale="pl",
        )

    try:
        session = await asyncio.to_thread(create)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {str(e)[:200]}")
    return {"url": session.url}


@app.get("/api/valuation/download")
async def valuation_download(session_id: str):
    """Verify Stripe payment and stream PDF."""
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe niedostępny")

    def retrieve():
        return stripe.checkout.Session.retrieve(session_id)

    try:
        s_obj = await asyncio.to_thread(retrieve)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {str(e)[:200]}")

    session = s_obj.to_dict() if hasattr(s_obj, "to_dict") else dict(s_obj)
    if session.get("payment_status") != "paid":
        raise HTTPException(402, "Płatność jeszcze niezakończona")
    md = session.get("metadata") or {}
    val_type = md.get("type")
    if val_type not in ("valuation", "valuation_custom"):
        raise HTTPException(400, "Sesja nie jest wyceną")

    # Fetch public listings.json (used dla obu typów)
    import urllib.request, json as _json
    try:
        with urllib.request.urlopen(f"{DOMAIN}/data/listings.json", timeout=15) as r:
            data = _json.loads(r.read().decode())
    except Exception as e:
        raise HTTPException(500, f"Nie można pobrać listingów: {str(e)[:100]}")

    all_listings = data if isinstance(data, list) else data.get("listings", [])

    if val_type == "valuation":
        listing_id = md.get("listing_id")
        listing = next((l for l in all_listings if str(l.get("id")) == str(listing_id)), None)
        if not listing:
            raise HTTPException(404, "Oferta nie znaleziona")
    else:
        # Custom valuation - property is in metadata
        listing = _json.loads(md.get("property_json") or "{}")
        listing["id"] = f"custom-{session_id[-8:]}"

        # Geocode: użyj mediany lat/lon istniejących ofert w tym mieście+dzielnicy
        # (SALE only, same type), żeby radius search miał punkt odniesienia
        city_lc = (listing.get("city") or "").lower()
        district_lc = (listing.get("district") or "").lower()
        same_txn_type = [x for x in all_listings
                         if x.get("city", "").lower() == city_lc
                         and x.get("type") == listing.get("type")
                         and x.get("transaction") == "sprzedaz"
                         and x.get("is_original") is not False
                         and x.get("lat") is not None and x.get("lon") is not None]
        # Prefer dzielnica jeśli podana
        center_pool = [x for x in same_txn_type if district_lc and x.get("district", "").lower() == district_lc]
        if not center_pool:
            center_pool = same_txn_type
        if center_pool:
            lats = sorted(x["lat"] for x in center_pool)
            lons = sorted(x["lon"] for x in center_pool)
            n = len(lats)
            listing["lat"] = lats[n//2] if n % 2 == 1 else (lats[n//2-1] + lats[n//2]) / 2
            listing["lon"] = lons[n//2] if n % 2 == 1 else (lons[n//2-1] + lons[n//2]) / 2

        # Comparable dla ai_rcn (median of SALE listings, same city+type)
        comparable = [x for x in same_txn_type if x.get("price_pm2")]
        if comparable:
            sorted_p = sorted(x["price_pm2"] for x in comparable)
            n = len(sorted_p)
            median = sorted_p[n//2] if n % 2 == 1 else (sorted_p[n//2-1] + sorted_p[n//2]) / 2
            listing["ai_rcn_pm2"] = int(median * 0.94)

    pdf_data = await asyncio.to_thread(build_valuation_pdf, listing, all_listings, md.get("email", ""))
    fname = f"wycena-{listing.get('id','custom')}.pdf".replace("/", "-")
    return StreamingResponse(
        io.BytesIO(pdf_data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _build_map_png(main_lat, main_lon, offers, width=900, height=520):
    """Generate a static OpenStreetMap PNG with main property + offer pins.
    Returns bytes or None on failure. Uses staticmap library (no API key)."""
    try:
        from staticmap import StaticMap, CircleMarker
        from PIL import Image, ImageDraw
        import io as _io

        # Use tiles from OSM (default), with 2 max threads to be polite
        m = StaticMap(width, height, url_template="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")

        # Add offer pins (red circles, smaller)
        offer_count = 0
        for o in offers[:10]:
            olat, olon = o.get("lat"), o.get("lon")
            if olat is not None and olon is not None:
                m.add_marker(CircleMarker((olon, olat), "#DC2626", 14))
                m.add_marker(CircleMarker((olon, olat), "#ffffff", 6))
                offer_count += 1

        # Add main property pin (bigger, yellow/orange) LAST so it's on top
        m.add_marker(CircleMarker((main_lon, main_lat), "#0B1836", 22))
        m.add_marker(CircleMarker((main_lon, main_lat), "#FFB800", 16))

        img = m.render()  # auto-fits all markers

        # Add pin numbers as overlay using PIL
        try:
            from PIL import ImageFont
            from staticmap import _lon_to_x as _sm_lon_to_x, _lat_to_y as _sm_lat_to_y
            font = None
            for fp in ["/app/finderdom-landing/api/fonts/DejaVuSans-Bold.ttf",
                       "api/fonts/DejaVuSans-Bold.ttf",
                       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
                try:
                    font = ImageFont.truetype(fp, 11)
                    break
                except Exception:
                    pass
            if font is None:
                font = ImageFont.load_default()

            draw = ImageDraw.Draw(img)
            for i, o in enumerate(offers[:10], 1):
                olat, olon = o.get("lat"), o.get("lon")
                if olat is None or olon is None:
                    continue
                try:
                    x = _sm_lon_to_x(olon, m.zoom)
                    y = _sm_lat_to_y(olat, m.zoom)
                    px = int(m._x_to_px(x))
                    py = int(m._y_to_px(y))
                    txt = str(i)
                    bbox = draw.textbbox((0, 0), txt, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    draw.text((px - tw/2, py - th/2 - 1), txt, fill="white", font=font)
                except Exception:
                    pass
            # Main pin label "★"
            try:
                x = _sm_lon_to_x(main_lon, m.zoom)
                y = _sm_lat_to_y(main_lat, m.zoom)
                px = int(m._x_to_px(x))
                py = int(m._y_to_px(y))
                txt = "★"
                bbox = draw.textbbox((0, 0), txt, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.text((px - tw/2, py - th/2 - 2), txt, fill="#0B1836", font=font)
            except Exception:
                pass
        except Exception as _e:
            pass

        buf = _io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        try:
            logger.error("Map generation failed: %s", str(e)[:200])
        except Exception:
            pass
        return None


def build_valuation_pdf(l, all_listings, buyer_email):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

    face = _register_pdf_font()
    face_bold = "DejaVu-Bold" if face == "DejaVu" else "Helvetica-Bold"

    # Compute local median (5km for mieszkania, 8km for domy/dzialki)
    # ZAWSZE tylko oferty sprzedaży (nie wynajem)
    local_offers = []
    used_radius = 0
    txn = l.get("transaction") or "sprzedaz"
    # Custom valuation may pass "sale" - normalize
    if txn == "sale":
        txn = "sprzedaz"

    if l.get("lat") is not None and l.get("lon") is not None:
        # Try radiuses 5,8,12,20 km until we get min 3 offers
        candidates_by_km = {}
        max_km = 20
        for x in all_listings:
            if (x.get("id") != l.get("id")
                and x.get("type") == l.get("type")
                and x.get("transaction") == "sprzedaz"
                and x.get("is_original") is not False
                and x.get("lat") is not None and x.get("lon") is not None
                and x.get("price_pm2")):
                d = _haversine_km(l["lat"], l["lon"], x["lat"], x["lon"])
                if d <= max_km:
                    candidates_by_km[x["id"]] = (d, x)

        # Choose smallest radius with min 3 offers, default 5km for mieszkania, 8 for reszta
        min_km = 5 if l.get("type") == "mieszkanie" else 8
        for km in [min_km, min_km + 3, min_km + 7, min_km + 15]:
            local_offers = [{**x, "_dist": round(d, 2)}
                            for _, (d, x) in candidates_by_km.items() if d <= km]
            if len(local_offers) >= 3:
                used_radius = km
                break
        else:
            local_offers = [{**x, "_dist": round(d, 2)} for _, (d, x) in candidates_by_km.items()]
            used_radius = max_km
        local_offers.sort(key=lambda x: x["_dist"])
        local_offers = local_offers[:10]

    # Fallback: brak GPS - użyj SALE-only comparable z tego samego miasta
    if not local_offers:
        for x in all_listings:
            if (x.get("city", "").lower() == (l.get("city") or "").lower()
                and x.get("type") == l.get("type")
                and x.get("transaction") == "sprzedaz"
                and x.get("is_original") is not False
                and x.get("price_pm2")):
                local_offers.append({**x, "_dist": None})
        local_offers = local_offers[:10]
        used_radius = 0  # mark as "same city"

    ppm2_this = l.get("price_pm2") or 0
    ppm2_local = 0
    if local_offers:
        sorted_p = sorted(x["price_pm2"] for x in local_offers)
        n = len(sorted_p)
        ppm2_local = sorted_p[n//2] if n % 2 == 1 else (sorted_p[n//2-1] + sorted_p[n//2]) / 2
    ppm2_rcn = int(ppm2_local * 0.94) if ppm2_local else (l.get("ai_rcn_pm2") or 0)

    # Verdict
    delta_pct = 0
    verdict_text = "W normie"
    verdict_color = colors.HexColor("#8ba3d4")
    recommendation = "Oferta w normie rynkowej — możesz negocjować niewielką obniżkę (2-5%)."
    if ppm2_local and ppm2_this:
        delta_pct = ((ppm2_this - ppm2_local) / ppm2_local) * 100
        if delta_pct <= -8:
            verdict_text = f"OKAZJA — {abs(delta_pct):.0f}% poniżej rynku"
            verdict_color = colors.HexColor("#22c55e")
            recommendation = "Warto działać szybko — oferta znacząco poniżej mediany rynkowej. Zweryfikuj stan techniczny i kupuj."
        elif delta_pct >= 8:
            verdict_text = f"DROGO — {delta_pct:.0f}% powyżej rynku"
            verdict_color = colors.HexColor("#ef4444")
            recommendation = f"Cena wyraźnie powyżej mediany okolicy ({abs(delta_pct):.0f}%). Negocjuj minimum {abs(delta_pct):.0f}% lub szukaj alternatyw."
        else:
            verdict_text = f"NORMA — {delta_pct:+.0f}% od mediany"

    # Estimate value range
    area = l.get("area_m2") or 0
    est_low = int(ppm2_local * 0.92 * area) if ppm2_local else 0
    est_mid = int(ppm2_local * area) if ppm2_local else 0
    est_high = int(ppm2_local * 1.08 * area) if ppm2_local else 0

    def fmt_pln(n):
        try:
            return f"{int(n):,}".replace(",", " ") + " zł"
        except: return "—"

    def fmt_pm2(n):
        try:
            return f"{int(n):,}".replace(",", " ") + " zł/m²"
        except: return "—"

    # Styles
    title_style = ParagraphStyle("t", fontName=face_bold, fontSize=22, leading=26, textColor=colors.HexColor("#0B1836"))
    h2 = ParagraphStyle("h2", fontName=face_bold, fontSize=14, leading=18, textColor=colors.HexColor("#0B1836"), spaceBefore=8, spaceAfter=6)
    normal = ParagraphStyle("n", fontName=face, fontSize=10, leading=14, textColor=colors.HexColor("#333333"))
    small = ParagraphStyle("s", fontName=face, fontSize=8, leading=11, textColor=colors.grey)
    verdict_p = ParagraphStyle("v", fontName=face_bold, fontSize=18, leading=22, textColor=verdict_color, alignment=TA_CENTER)
    big_num = ParagraphStyle("bn", fontName=face_bold, fontSize=20, leading=24, textColor=colors.HexColor("#FFB800"), alignment=TA_CENTER)
    label_st = ParagraphStyle("l", fontName=face_bold, fontSize=8, leading=11, textColor=colors.HexColor("#8ba3d4"), alignment=TA_CENTER)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm, title="Wycena FinderDom.pl")
    story = []

    # === Page 1 header ===
    story.append(Paragraph("WYCENA NIERUCHOMOŚCI", title_style))
    listing_id_short = str(l.get('id', ''))[-8:].replace('otodom-', '').replace('otodom', '')
    story.append(Paragraph(f"FinderDom.pl · Raport nr FD-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}-{listing_id_short}", small))
    story.append(Spacer(1, 6*mm))

    # Photo (jeśli listing ma image_url)
    photo_url = l.get('image_url') or (l.get('image_urls') or [None])[0]
    if photo_url and str(photo_url).startswith('http'):
        try:
            import urllib.request
            from reportlab.platypus import Image as RLImage
            req = urllib.request.Request(photo_url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*',
            })
            with urllib.request.urlopen(req, timeout=8) as r:
                img_data = r.read()
            img_buf = io.BytesIO(img_data)
            img = RLImage(img_buf, width=174*mm, height=100*mm, kind='proportional')
            img.hAlign = 'CENTER'
            story.append(img)
            story.append(Spacer(1, 5*mm))
        except Exception:
            pass  # ignore image errors

    # Location
    story.append(Paragraph("Lokalizacja i parametry", h2))
    loc = l.get("location") or f"{l.get('city','')}{', ' + l.get('district','') if l.get('district') else ''}"
    story.append(Paragraph(f"<b>Adres:</b> {loc}", normal))
    story.append(Paragraph(f"<b>Typ:</b> {(l.get('type') or '').capitalize()} · <b>Rynek:</b> {(l.get('market_type') or '—').capitalize()}", normal))

    elevator_label = {"tak": "Tak", "nie": "Nie", "": "—"}.get(l.get("elevator", ""), "—")
    specs_data = [
        ["Powierzchnia", f"{area} m²", "Pokoje", str(l.get("rooms") or "—")],
        ["Piętro", f"{l.get('floor','—')}/{l.get('max_floor','—')}" if l.get('max_floor') else str(l.get('floor','—')), "Rok budowy", str(l.get("build_year") or "—")],
        ["Standard", (l.get("standard") or "—").capitalize(), "Budynek", (l.get("building_type") or "—").capitalize()],
        ["Winda", elevator_label, "Typ", (l.get("type") or "—").capitalize()],
    ]
    specs_tbl = Table(specs_data, colWidths=[35*mm, 45*mm, 35*mm, 45*mm])
    specs_tbl.setStyle(TableStyle([
        ("FONTNAME", (0,0), (-1,-1), face),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("BACKGROUND", (0,0), (0,-1), colors.HexColor("#F5F7FB")),
        ("BACKGROUND", (2,0), (2,-1), colors.HexColor("#F5F7FB")),
        ("TEXTCOLOR", (0,0), (0,-1), colors.HexColor("#8ba3d4")),
        ("TEXTCOLOR", (2,0), (2,-1), colors.HexColor("#8ba3d4")),
        ("FONTNAME", (0,0), (0,-1), face_bold),
        ("FONTNAME", (2,0), (2,-1), face_bold),
        ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#c5d0e6")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(specs_tbl)
    story.append(Spacer(1, 6*mm))

    # Wycena AI
    story.append(Paragraph("Wycena AI", h2))
    val_tbl = Table([
        [Paragraph("MINIMUM", label_st), Paragraph("WARTOŚĆ RYNKOWA", label_st), Paragraph("MAKSIMUM", label_st)],
        [Paragraph(fmt_pln(est_low), big_num), Paragraph(fmt_pln(est_mid), ParagraphStyle("m", fontName=face_bold, fontSize=24, leading=28, textColor=colors.HexColor("#0B1836"), alignment=TA_CENTER)), Paragraph(fmt_pln(est_high), big_num)],
    ], colWidths=[52*mm, 56*mm, 52*mm])
    val_tbl.setStyle(TableStyle([
        ("BACKGROUND", (1,0), (1,-1), colors.HexColor("#FFF9E6")),
        ("BOX", (0,0), (-1,-1), 0.5, colors.HexColor("#c5d0e6")),
        ("LINEBEFORE", (1,0), (1,-1), 1, colors.HexColor("#FFB800")),
        ("LINEAFTER", (1,0), (1,-1), 1, colors.HexColor("#FFB800")),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 12),
    ]))
    story.append(val_tbl)
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(f"<b>Zakres cen za m²:</b> {fmt_pm2(ppm2_local*0.92)} — <b>{fmt_pm2(ppm2_local)}</b> — {fmt_pm2(ppm2_local*1.08)}<br/>"
                           f"<b>Cena ofertowa:</b> {fmt_pln(l.get('price'))} ({fmt_pm2(ppm2_this)})<br/>"
                           f"<b>Analiza w promieniu:</b> {(str(used_radius) + ' km') if used_radius else 'całe miasto'} · {len(local_offers)} porównywalnych ofert (tylko sprzedaż)<br/>"
                           f"<b>Szacowana wartość RCN (transakcje):</b> {fmt_pm2(ppm2_rcn)}", normal))
    story.append(Spacer(1, 6*mm))

    # Verdict
    story.append(Paragraph(verdict_text, verdict_p))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(f"<b>Rekomendacja:</b> {recommendation}", normal))

    # === Page 2 ===
    story.append(PageBreak())
    story.append(Paragraph("Mapa okolicznych ofert", h2))
    story.append(Paragraph(
        "⭐ Twoja nieruchomość (żółta) · 🔴 10 ofert sprzedaży z okolicy (numerowane 1–10)",
        small
    ))
    story.append(Spacer(1, 3*mm))

    # Generate map image
    map_added = False
    if l.get("lat") is not None and l.get("lon") is not None:
        offers_with_gps = [o for o in local_offers[:10]
                           if o.get("lat") is not None and o.get("lon") is not None]
        map_bytes = _build_map_png(l["lat"], l["lon"], offers_with_gps, width=900, height=520)
        if map_bytes:
            from reportlab.platypus import Image as RLImage
            map_buf = io.BytesIO(map_bytes)
            img = RLImage(map_buf, width=170*mm, height=98*mm)
            img.hAlign = "CENTER"
            story.append(img)
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph(
                "Źródło map: © OpenStreetMap contributors (openstreetmap.org/copyright)",
                ParagraphStyle("attr", fontName=face, fontSize=7, textColor=colors.grey, alignment=TA_CENTER)
            ))
            story.append(Spacer(1, 5*mm))
            map_added = True

    if not map_added:
        story.append(Paragraph(
            "<i>Mapa niedostępna — brak współrzędnych GPS dla tej lokalizacji.</i>",
            small
        ))
        story.append(Spacer(1, 5*mm))

    story.append(Paragraph("Transakcje referencyjne", h2))
    scope_txt = f"promień {used_radius} km" if used_radius else f"miasto {l.get('city') or '—'}"
    story.append(Paragraph(f"Poniżej {min(10, len(local_offers))} podobnych <b>ofert sprzedaży</b> z okolicy ({scope_txt}):", small))
    story.append(Spacer(1, 3*mm))

    if local_offers[:10]:
        ref_rows = [["Lp.", "Lokalizacja", "m²", "Cena", "zł/m²", "Odl."]]
        for i, o in enumerate(local_offers[:10], 1):
            dist_val = o.get('_dist')
            dist_txt = f"{dist_val} km" if dist_val is not None else "—"
            ref_rows.append([
                str(i),
                (o.get("location") or o.get("city") or "—")[:38],
                str(o.get("area_m2","—")),
                fmt_pln(o.get("price")),
                fmt_pm2(o.get("price_pm2")),
                dist_txt,
            ])
        ref_tbl = Table(ref_rows, colWidths=[10*mm, 65*mm, 15*mm, 30*mm, 30*mm, 15*mm])
        ref_tbl.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), face),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B1836")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), face_bold),
            ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#c5d0e6")),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
            ("ALIGN", (2,1), (-1,-1), "RIGHT"),
        ]))
        story.append(ref_tbl)
    else:
        story.append(Paragraph("Brak wystarczających danych z okolicy do porównania.", small))

    story.append(Spacer(1, 8*mm))
    story.append(Paragraph("Informacje o raporcie", h2))
    story.append(Paragraph(
        f"<b>Data wygenerowania:</b> {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')}<br/>"
        f"<b>Zamawiający:</b> {buyer_email or '—'}<br/>"
        f"<b>Płatność:</b> Stripe (opłacone {VALUATION_PRICE} zł)<br/><br/>"
        f"<b>Metodologia:</b> Wycena AI porównuje cenę ofertową z medianą aktualnych <b>ofert sprzedaży</b> "
        f"{'w promieniu ' + str(used_radius) + ' km' if used_radius else 'z tego samego miasta'}. "
        f"Uwzględniane są wyłącznie oferty tego samego typu nieruchomości (bez wynajmu, bez duplikatów). "
        f"Szacowana wartość RCN (transakcje) to około 94% mediany ofertowej (typowa różnica między ceną wywoławczą a ceną transakcyjną). "
        f"Raport ma charakter informacyjny i nie stanowi wyceny w rozumieniu ustawy o gospodarce nieruchomościami.", small
    ))
    story.append(Spacer(1, 10*mm))

    footer = ParagraphStyle("f", fontName=face_bold, fontSize=10, leading=13, textColor=colors.HexColor("#FFB800"), alignment=TA_CENTER)
    footer_small = ParagraphStyle("fs", fontName=face, fontSize=8, leading=11, textColor=colors.grey, alignment=TA_CENTER)
    story.append(Paragraph(f"FinderDom.pl · {COMPANY_NAME}", footer))
    if COMPANY_NIP:
        story.append(Paragraph(f"NIP: {COMPANY_NIP} · {COMPANY_ADDRESS}", footer_small))

    doc.build(story)
    return out.getvalue()
