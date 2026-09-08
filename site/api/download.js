const { DMG_URL, incrementDownloads } = require('./_lib/r2');

module.exports = async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method === 'GET' || req.method === 'HEAD') return res.redirect(303, '/download');
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'GET, HEAD, POST');
    return res.status(405).json({ error: 'method not allowed' });
  }
  if (!(req.headers['content-type'] || '').toLowerCase().startsWith('application/json')) {
    return res.status(415).json({ error: 'Please use the download form with JavaScript enabled.' });
  }
  if (req.headers['sec-fetch-site'] === 'cross-site') {
    return res.status(403).json({ error: 'Please submit the form from the Hunch website.' });
  }
  let payload;
  try {
    payload = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error();
    if (Buffer.byteLength(JSON.stringify(payload)) > 8192) return res.status(413).json({ error: 'Your submission is too large. Please shorten the fields.' });
  } catch (_) { return res.status(400).json({ error: 'Invalid form submission.' }); }
  try {
    const upstream = await fetch('https://hunch-download-api.prithviseran0.workers.dev/profiles', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload), signal: AbortSignal.timeout(10000),
    });
    const result = await upstream.json();
    if (!upstream.ok || !result.ok) {
      const code = [400, 413, 429].includes(upstream.status) ? upstream.status : 503;
      return res.status(code).json({ error: code === 503
        ? 'Could not save your details. Please try again shortly.'
        : result.error || 'Please check the form and try again.' });
    }
    // Form views no longer count as downloads; a failed counter must not block a saved profile.
    try { await incrementDownloads(); } catch (_) { console.error('download counter update failed'); }
    return res.status(200).json({ download_url: DMG_URL });
  } catch (_) {
    // Never log submitted personal details or upstream response bodies.
    return res.status(503).json({ error: 'Could not save your details. Please try again shortly.' });
  }
};
