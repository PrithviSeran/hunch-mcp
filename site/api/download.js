// GET /api/download  →  count + 302 to the Hunch.dmg on R2
// Also reachable as /download via the rewrite in vercel.json.
const { DMG_URL, incrementDownloads } = require('../lib/r2');

module.exports = async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.setHeader('Allow', 'GET, HEAD');
    return res.status(405).json({ error: 'method not allowed' });
  }

  // Count best-effort: never block the download if R2 creds/stats write fail.
  try {
    await incrementDownloads();
  } catch (err) {
    console.error('download counter failed:', err && err.message ? err.message : err);
  }

  res.setHeader('Cache-Control', 'no-store');
  res.redirect(302, DMG_URL);
};
