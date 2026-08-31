// backfill-feed.js - one-shot: POST every Notebook entry to the nexus feed
// (source=aernbot), oldest first so the newest ends up on top. Date is preserved
// in the body since the feed timestamps at insertion. Run inside claude-relay.
const fs = require('fs');
const http = require('http');

const NOTEBOOK = '/workspace/obivault/Aernbot/Notebook.md';
const HOST = 'host.docker.internal';
const PORT = 5555;

function parseAll(md) {
  let body = md;
  const mark = md.indexOf('ENTRIES BELOW');
  if (mark !== -1) body = md.slice(md.indexOf('\n', mark) + 1);
  const lines = body.split('\n');
  const entries = [];
  let cur = null;
  for (const ln of lines) {
    if (ln.startsWith('## ')) {
      if (cur) entries.push(cur);
      const header = ln.replace(/^##\s*/, '').trim();
      const parts = header.split(' · '); // "<date> · <topic> · <title>"
      const date = parts[0] || '';
      const tag = parts.length > 2 ? parts[1].trim() : null;
      const title = parts.length > 2 ? parts.slice(2).join(' · ').trim()
                  : (parts.length > 1 ? parts[parts.length - 1].trim() : header);
      cur = { date, tag, title, body: [] };
    } else if (cur) {
      if (ln.trim() === '---') { entries.push(cur); cur = null; }
      else cur.body.push(ln);
    }
  }
  if (cur) entries.push(cur);
  return entries
    .map(e => ({ date: e.date, tag: e.tag, title: e.title, body: e.body.join('\n').trim() }))
    .filter(e => e.body);
}

function post(e) {
  return new Promise((res) => {
    const payload = JSON.stringify({
      source: 'aernbot',
      title: e.title,
      body: (e.date ? '[' + e.date + ']\n\n' : '') + e.body,
      tag: e.tag,
      url: 'obsidian://open?vault=Obivault&file=Aernbot%2FNotebook',
    });
    const r = http.request({
      host: HOST, port: PORT, path: '/api/nexus/feed', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    }, (resp) => { let d = ''; resp.on('data', c => d += c); resp.on('end', () => res(resp.statusCode === 200)); });
    r.on('error', () => res(false));
    r.write(payload); r.end();
  });
}

(async () => {
  const md = fs.readFileSync(NOTEBOOK, 'utf8');
  const entries = parseAll(md);
  console.log('found', entries.length, 'entries');
  let n = 0;
  for (const e of entries.reverse()) { if (await post(e)) { n++; console.log('  +', e.title); } }
  console.log('posted', n);
})();
