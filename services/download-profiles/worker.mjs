const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
});

export function validate(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Invalid form submission.');
  if (payload.company_website) throw new Error('Please leave the hidden field empty.');
  const data = {};
  for (const field of ['name', 'email', 'linkedin', 'x', 'github', 'other_social']) {
    if (payload[field] != null && typeof payload[field] !== 'string') throw new Error('Invalid form field.');
    data[field] = (payload[field] || '').trim();
  }
  if (!data.name || data.name.length > 200) throw new Error('Enter your name (up to 200 characters).');
  data.email = data.email.toLowerCase();
  if (data.email.length > 254 || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(data.email)) throw new Error('Enter a valid email address.');
  for (const field of ['linkedin', 'x', 'github', 'other_social']) {
    if (!data[field]) continue;
    let url;
    try { url = new URL(data[field]); } catch (_) { /* handled below */ }
    if (data[field].length > 500 || !url || !['https:', 'http:'].includes(url.protocol) || url.username || url.password) {
      throw new Error('Social links must be complete http:// or https:// URLs (up to 500 characters).');
    }
  }
  return data;
}

export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname !== '/profiles') return json({ error: 'Not found.' }, 404);
    if (request.method !== 'POST') return json({ error: 'Method not allowed.' }, 405);
    if (!(request.headers.get('content-type') || '').toLowerCase().startsWith('application/json')) return json({ error: 'Expected JSON.' }, 415);
    let data;
    try {
      // Bound the read even when a client omits Content-Length.
      const reader = request.body?.getReader();
      const chunks = []; let size = 0;
      if (!reader) return json({ error: 'Invalid form submission.' }, 400);
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 8192) { await reader.cancel(); return json({ error: 'Submission too large.' }, 413); }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      let payload;
      try { payload = JSON.parse(new TextDecoder().decode(bytes)); } catch (_) { return json({ error: 'Invalid JSON.' }, 400); }
      data = validate(payload);
    } catch (error) { return json({ error: error.message }, 400); }
    try {
      // Same email identity as the app's API. Blank optional fields preserve existing socials.
      const result = await env.DB.prepare(`
        INSERT INTO profiles (email, name, linkedin, x, github, other_social) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET name = excluded.name,
          linkedin = COALESCE(NULLIF(excluded.linkedin, ''), profiles.linkedin),
          x = COALESCE(NULLIF(excluded.x, ''), profiles.x),
          github = COALESCE(NULLIF(excluded.github, ''), profiles.github),
          other_social = COALESCE(NULLIF(excluded.other_social, ''), profiles.other_social)
      `).bind(data.email, data.name, data.linkedin, data.x, data.github, data.other_social).run();
      if (!result.success) throw new Error('Write failed');
      return json({ ok: true });
    } catch (_) { return json({ error: 'Could not save your details. Please try again shortly.' }, 503); }
  },
};
