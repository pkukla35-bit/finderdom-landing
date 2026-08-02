// Vercel Serverless Function: POST /api/subscribe
// Zapisuje email w MongoDB Atlas + wysyła notyfikację admin przez Resend

import { MongoClient } from 'mongodb';

let cachedClient = null;

async function getDb() {
  if (cachedClient) return cachedClient.db('finderdom');
  const uri = process.env.MONGO_URL;
  if (!uri) throw new Error('MONGO_URL not configured');
  const client = new MongoClient(uri, {
    maxPoolSize: 3,
    serverSelectionTimeoutMS: 8000,
  });
  await client.connect();
  cachedClient = client;
  return client.db('finderdom');
}

async function sendAdminEmail(email) {
  const apiKey = process.env.RESEND_API_KEY;
  const from = process.env.RESEND_FROM || 'FinderDom <onboarding@resend.dev>';
  const to = process.env.RESEND_OWNER_CC || process.env.ADMIN_EMAIL;
  if (!apiKey || !to) return;

  try {
    await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from,
        to: [to],
        subject: `🎉 Nowy zapis na FinderDom.pl: ${email}`,
        html: `
          <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:24px">
            <h2 style="color:#0B1836">Nowy zapis na landing FinderDom.pl</h2>
            <p><strong>Email:</strong> ${email}</p>
            <p><strong>Data:</strong> ${new Date().toLocaleString('pl-PL', { timeZone: 'Europe/Warsaw' })}</p>
            <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
            <p style="color:#666;font-size:12px">Powiadomienie z landing page finderdom.pl</p>
          </div>
        `,
      }),
    });
  } catch (e) {
    console.error('Resend error:', e);
  }
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

export default async function handler(req, res) {
  // CORS
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
      return res.status(400).json({ error: 'Email jest za długi' });
    }

    const db = await getDb();
    const subscribers = db.collection('landing_subscribers');

    // Upsert (unique by email)
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

    const isNew = result.upsertedCount > 0;

    // Send admin notification only for new signups (fire and forget)
    if (isNew) {
      sendAdminEmail(cleanEmail).catch(console.error);
    }

    return res.status(200).json({
      ok: true,
      new: isNew,
      message: isNew ? 'Dziękujemy za zapis!' : 'Już jesteś zapisany.',
    });
  } catch (err) {
    console.error('Subscribe error:', err);
    return res.status(500).json({ error: 'Błąd serwera. Spróbuj ponownie za chwilę.' });
  }
}
