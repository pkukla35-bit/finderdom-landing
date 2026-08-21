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
"""
import asyncio
import io
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import bcrypt
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
    await db.users.create_index("email", unique=True)
    await db.invoices.create_index([("user_id", 1), ("created_at", -1)])
    try:
        await db.invoices.create_index("stripe_session_id", unique=True, sparse=True)
    except Exception:
        pass
    await db.stripe_events.create_index("_id", unique=True)
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
        s = event["data"]["object"]
        if s.get("payment_status") != "paid":
            return {"received": True}
        md = s.get("metadata") or {}
        user_id = md.get("user_id")
        plan = md.get("plan")
        if plan not in PLANS or not user_id:
            return {"received": True}
        await apply_payment(
            user_id, plan, s["id"], int(s.get("amount_total") or 0),
            s.get("customer") if isinstance(s.get("customer"), str) else None,
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
        session = await asyncio.to_thread(retrieve)
    except stripe.error.StripeError as e:
        raise HTTPException(502, f"Stripe error: {str(e)[:200]}")

    if session.get("payment_status") != "paid":
        return {"paid": False, "status": session.get("payment_status")}

    md = session.get("metadata") or {}
    if md.get("user_id") != str(user["_id"]):
        raise HTTPException(403, "Ta sesja płatności nie należy do tego użytkownika")
    plan = md.get("plan")
    if plan not in PLANS:
        raise HTTPException(400, "Nieprawidłowy plan w sesji")

    await apply_payment(
        str(user["_id"]), plan, session["id"], int(session.get("amount_total") or 0),
        session.get("customer") if isinstance(session.get("customer"), str) else None,
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
