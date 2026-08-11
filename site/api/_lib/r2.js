// Minimal S3/R2 helper (AWS SigV4). Used by /api/download and /api/stats.
// Needs R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY in the Vercel project env.
// Endpoint/bucket/public URL have safe defaults matching the hunch-updates bucket.

const crypto = require('crypto');

const ENDPOINT = process.env.R2_ENDPOINT || 'https://60fa7f1300642083cd42d209a2a8d4ae.r2.cloudflarestorage.com';
const BUCKET = process.env.R2_BUCKET || 'hunch-updates';
const REGION = 'auto';
const STATS_KEY = 'stats/dmg.json';
const DMG_URL =
  process.env.HUNCH_DMG_URL ||
  'https://pub-8748b4003e764f8a888e32c8e2ce7057.r2.dev/Hunch.dmg';

function sha256(data) {
  return crypto.createHash('sha256').update(data, 'utf8').digest('hex');
}

function hmac(key, data) {
  return crypto.createHmac('sha256', key).update(data, 'utf8').digest();
}

function amzDate(d = new Date()) {
  const iso = d.toISOString().replace(/[:-]|\.\d{3}/g, '');
  return { amz: iso, date: iso.slice(0, 8) };
}

async function signedFetch(method, key, body = null) {
  const accessKey = process.env.R2_ACCESS_KEY_ID;
  const secretKey = process.env.R2_SECRET_ACCESS_KEY;
  if (!accessKey || !secretKey) {
    const err = new Error('R2 credentials not configured');
    err.code = 'NO_CREDS';
    throw err;
  }

  const url = new URL(`${ENDPOINT.replace(/\/$/, '')}/${BUCKET}/${key}`);
  const { amz, date } = amzDate();
  const payloadHash = sha256(body || '');
  const headers = {
    host: url.host,
    'x-amz-content-sha256': payloadHash,
    'x-amz-date': amz,
  };
  if (body != null) headers['content-type'] = 'application/json';

  // Signed headers must be sorted alphabetically.
  const signedHeaders = Object.keys(headers).sort().join(';');
  const canonicalHeaders = Object.keys(headers)
    .sort()
    .map((k) => `${k}:${headers[k]}\n`)
    .join('');
  const canonicalRequest = [
    method,
    url.pathname,
    '',
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join('\n');

  const credentialScope = `${date}/${REGION}/s3/aws4_request`;
  const stringToSign = [
    'AWS4-HMAC-SHA256',
    amz,
    credentialScope,
    sha256(canonicalRequest),
  ].join('\n');

  const signingKey = hmac(
    hmac(hmac(hmac(`AWS4${secretKey}`, date), REGION), 's3'),
    'aws4_request',
  );
  const signature = crypto
    .createHmac('sha256', signingKey)
    .update(stringToSign, 'utf8')
    .digest('hex');

  const requestHeaders = {
    Authorization:
      `AWS4-HMAC-SHA256 Credential=${accessKey}/${credentialScope}, ` +
      `SignedHeaders=${signedHeaders}, Signature=${signature}`,
    'x-amz-content-sha256': payloadHash,
    'x-amz-date': amz,
  };
  if (body != null) requestHeaders['Content-Type'] = 'application/json';

  return fetch(url, {
    method,
    headers: requestHeaders,
    body: body == null ? undefined : body,
  });
}

async function readStats() {
  const res = await signedFetch('GET', STATS_KEY);
  if (res.status === 404) return { downloads: 0, updated_at: null };
  if (!res.ok) {
    throw new Error(`R2 GET stats failed: ${res.status} ${await res.text()}`);
  }
  const data = await res.json();
  return {
    downloads: Number(data.downloads) || 0,
    updated_at: data.updated_at || null,
  };
}

async function writeStats(stats) {
  const body = JSON.stringify(stats);
  const res = await signedFetch('PUT', STATS_KEY, body);
  if (!res.ok) {
    throw new Error(`R2 PUT stats failed: ${res.status} ${await res.text()}`);
  }
}

async function incrementDownloads() {
  const current = await readStats();
  const next = {
    downloads: current.downloads + 1,
    updated_at: new Date().toISOString(),
  };
  await writeStats(next);
  return next;
}

module.exports = {
  DMG_URL,
  STATS_KEY,
  readStats,
  incrementDownloads,
};
