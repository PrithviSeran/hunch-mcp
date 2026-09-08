import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import worker from '../worker.mjs';

function database() {
  const db = new DatabaseSync(':memory:');
  db.exec('CREATE TABLE profiles (email TEXT PRIMARY KEY NOT NULL COLLATE NOCASE, name TEXT NOT NULL)');
  db.exec("INSERT INTO profiles VALUES ('existing@example.com', 'Existing')");
  db.exec(readFileSync(new URL('../migrations/0002_profile_socials.sql', import.meta.url), 'utf8'));
  return { db, env: { DB: { prepare(sql) { return { bind(...values) { return { async run() {
    const result = db.prepare(sql).run(...values);
    return { success: true, meta: { changes: result.changes } };
  } }; } }; } } } };
}
const request = (body) => new Request('https://example.test/profiles', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

test('migration preserves existing rows; same email upserts and blank socials preserve values', async () => {
  const { db, env } = database();
  const profile = { name: " O'Brien ", email: ' EXISTING@Example.com ', github: 'https://github.com/example' };
  assert.equal((await worker.fetch(request(profile), env)).status, 200);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM profiles').get().n, 1);
  assert.equal(db.prepare('SELECT name FROM profiles').get().name, "O'Brien");
  await worker.fetch(request({ name: 'Updated', email: 'existing@example.com' }), env);
  assert.equal(db.prepare('SELECT github FROM profiles').get().github, profile.github);
  assert.equal((await worker.fetch(request({ name: 'New', email: 'new@example.com', x: 'https://x.com/new' }), env)).status, 200);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM profiles').get().n, 2);
  // The app's existing two-column upsert remains compatible with the new columns.
  db.prepare('INSERT INTO profiles (email, name) VALUES (?, ?) ON CONFLICT(email) DO UPDATE SET name = excluded.name').run('existing@example.com', 'App');
  assert.equal(db.prepare('SELECT github FROM profiles WHERE email = ?').get('existing@example.com').github, profile.github);
  db.close();
});

test('invalid input and oversized payloads never write profiles', async () => {
  const { db, env } = database();
  for (const payload of [null, [], {}, {name:'',email:'x@y.com'}, {name:'N',email:'bad'},
    {name:['N'],email:'x@y.com'}, {name:'N',email:'x@y.com',github:'javascript:alert(1)'},
    {name:'N',email:'x@y.com',company_website:'bot'}, {name:'N',email:'x@y.com',github:'https://user:pass@example.com'}]) {
    assert.equal((await worker.fetch(request(payload), env)).status, 400);
  }
  assert.equal((await worker.fetch(request({ name: 'N'.repeat(9000) }), env)).status, 413);
  assert.equal(db.prepare('SELECT COUNT(*) AS n FROM profiles').get().n, 1);
  db.close();
});

test('database errors fail closed without leaking details', async () => {
  const response = await worker.fetch(request({name:'N',email:'x@y.com'}), {DB:{prepare(){throw new Error('private database error');}}});
  assert.equal(response.status, 503);
  assert.ok(!(await response.text()).includes('private'));
});

const require = createRequire(import.meta.url);
const r2 = require('../../../site/api/_lib/r2.js');
let counts = 0;
r2.incrementDownloads = async () => { counts++; };
const handler = require('../../../site/api/download.js');
function response() {
  return { code:200, headers:{}, setHeader(k,v){this.headers[k]=v;}, status(c){this.code=c;return this;},
    json(body){this.body=body;return this;}, redirect(c,url){this.code=c;this.location=url;return this;} };
}
test('website API gates downloads on a successful save; GET/HEAD do not count', async () => {
  const original = globalThis.fetch;
  try {
    for (const method of ['GET','HEAD']) {
      const res = response(); await handler({method,headers:{}},res);
      assert.equal(res.location,'/download'); assert.equal(counts,0);
    }
    const req = {method:'POST',headers:{'content-type':'application/json'},body:{name:'N',email:'n@example.com'}};
    globalThis.fetch = async () => new Response(JSON.stringify({error:'Unavailable'}),{status:503});
    const failed = response(); await handler(req,failed); assert.equal(failed.code,503);
    assert.equal(counts,0); assert.equal(failed.body.download_url,undefined);
    globalThis.fetch = async () => new Response(JSON.stringify({ok:true}));
    const saved = response(); await handler(req,saved); assert.equal(saved.code,200);
    assert.match(saved.body.download_url,/Hunch\.dmg$/); assert.equal(counts,1);
  } finally { globalThis.fetch = original; }
});
