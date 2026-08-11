// GET /api/stats  →  { "downloads": N, "updated_at": "..." }
const { readStats } = require('./_lib/r2');

module.exports = async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.setHeader('Allow', 'GET, HEAD');
    return res.status(405).json({ error: 'method not allowed' });
  }

  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Access-Control-Allow-Origin', '*');

  try {
    const stats = await readStats();
    return res.status(200).json(stats);
  } catch (err) {
    if (err && err.code === 'NO_CREDS') {
      // Fall back to the public stats object so the count is still readable
      // even before write credentials are configured on Vercel.
      try {
        const pub = await fetch(
          'https://pub-8748b4003e764f8a888e32c8e2ce7057.r2.dev/stats/dmg.json',
        );
        if (pub.ok) return res.status(200).json(await pub.json());
      } catch (_) { /* ignore */ }
      return res.status(503).json({ error: 'stats unavailable', downloads: null });
    }
    console.error('stats read failed:', err && err.message ? err.message : err);
    return res.status(500).json({ error: 'stats read failed' });
  }
};
