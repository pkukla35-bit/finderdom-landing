
// Vercel Serverless Function: POST /api/subscribe
// Zapisuje email + wysyla notyfikacje admin przez Resend
// MongoDB jest OPCJONALNE - jesli MONGO_URL ustawione, zapisuje tez do bazy

import { MongoClient } from 'mongodb';

let cachedClient = null;

async function getDb() {
  const uri = process.env.MONGO_URL;
  if (!uri || uri.includes('localhost')) return null;
  if (cachedClient) return cachedClient.db('finderdom');
  try {
    const client = new MongoClient(uri, {
      maxPoolSize: 3,
      serverSelectionTimeoutMS: 5000,
    });
    await client.connect();
    cachedClient = client;
    return client.db('finderdom');
  } catch (e) {
    console.error('Mongo connection failed:', e.message);
    return null;
  }
}

async function sendAdminEmail(email, isNew) {
  const apiKey = process.env.RESEND_API_KEY;
  const from = process.env.RESEND_FROM || 'FinderDom <onboarding@resend.dev>';
  const to = process.env.RESEND_OWNER_CC || process.env.ADMIN_EMAIL;
  if (!apiKey || !to) {
    console.error('Resend not configured');
    return false;
  }

  try {
    const res = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from,
        to: [to],
        subject: `Nowy zapis na FinderDom.pl: ${email}`,
        html: `
          <div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#f9fafb">
            <div style="background:#fff;padding:32px;border-radius:16px;box-shadow:0 2px 8px rgba(0,0,0,0.08)">
              <h2 style="color:#0B1836;margin:0 0 16px 0;font-size:22px">
                ${isNew ? 'Nowy zapis' : 'Ponowny zapis'} na FinderDom.pl
              </h2>
              <div style="background:#fff8e6;border-left:4px solid #FFB800;padding:16px;border-radius:8px;margin:20px 0">
                <div style="color:#666;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">Email</div>
                <div style="font-size:18px;font-weight:700;color:#0B1836">${email}</div>
              </div>
              <div style="color:#666;font-size:13px;margin-top:16px">
                <strong>Data:</strong> ${new Date().toLocaleString('pl-PL', { timeZone: 'Europe/Warsaw' })}<br>
                <strong>Status:</strong> ${isNew ? 'nowy subskrybent' : 'juz byl na liscie'}
              </div>
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">
              <p style="color:#999;font-size:12px;margin:0">Powiadomienie z landing page finderdom.pl</p>
            </div>
          </div>
        `,
      }),
    });
    return res.ok;
  } catch (e) {
    console.error('Resend error:', e);
    return false;
  }
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

const recentEmails = new Set();

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'Method not allowed' });

  try {
    const { email, source } = req.body || {};

    if (!email || typeof email !== 'string') {
      return res.status(400).json({ error: 'Email jest wymagany' });
    }

    const cleanEmail = email.trim().toLowerCase();
    if (!isValidEmail(cleanEmail)) {
      return res.status(400).json({ error: 'Podaj poprawny adres email' });
    }
    if (cleanEmail.length > 200) {
      return res.status(400).json({ error: 'Email jest za dlugi' });
    }

    let isNew = true;

    const db = await getDb();
    if (db) {
      try {
        const subscribers = db.collection('landing_subscribers');
        const now = new Date();
        const result = await subscribers.updateOne(
          { email: cleanEmail },
          {
            $setOnInsert: {
              email: cleanEmail,
              source: source || 'landing',
              created_at: now,
              ip: req.headers['x-forwarded-for']?.split(',')[0] || null,
              user_agent: req.headers['user-agent'] || null,
            },
            $set: { last_seen_at: now },
          },
          { upsert: true }
        );
        isNew = result.upsertedCount > 0;
      } catch (e) {
        console.error('Mongo write failed:', e.message);
        isNew = !recentEmails.has(cleanEmail);
        recentEmails.add(cleanEmail);
        if (recentEmails.size > 1000) recentEmails.clear();
      }
    } else {
      isNew = !recentEmails.has(cleanEmail);
      recentEmails.add(cleanEmail);
      if (recentEmails.size > 1000) recentEmails.clear();
    }

    sendAdminEmail(cleanEmail, isNew).catch(console.error);

    return res.status(200).json({
      ok: true,
      new: isNew,
      message: isNew ? 'Dziekujemy za zapis!' : 'Juz jestes zapisany.',
    });
  } catch (err) {
    console.error('Subscribe error:', err);
    return res.status(500).json({ error: 'Blad serwera. Sprobuj ponownie za chwile.' });
  }
}
