// send-feed.js - POST the newest Aernbot Notebook entry to the nexus Feed.
// Idempotent: skips if the top entry was already sent (tracked in /workspace/.feed-last).
// Called by the heartbeat right after it writes+pushes a new Notebook entry.
const fs = require('fs');
const http = require('http');

const NOTEBOOK = '/workspace/obivault/Aernbot/Notebook.md';
const STATE = '/workspace/.feed-last';
const HOST = process.env.NEXUS_HOST || 'host.docker.internal';
const PORT = parseInt(process.env.NEXUS_PORT || '5555', 10);

function parseTop(md) {
  let body = md;
  const mark = md.indexOf('ENTRIES BELOW');
  if (mark !== -1) body = md.slice(md.indexOf('\n', mark) + 1);
  const lines = body.split('\n');
  let i = 0;
  while (i < lines.length && !lines[i].startsWith('## ')) i++;
  if (i >= lines.length) return null;
  const header = lines[i].replace(/^##\s*/, '').trim();
  const parts = header.split(' · '); // "<date> · <topic> · <title>"
  const tag = parts.length > 2 ? parts[1].trim() : null;
  const title = parts.length > 2 ? parts.slice(2).join(' · ').trim()
              : (parts.length > 1 ? parts[parts.length - 1].trim() : header);
  const bodyLines = [];
  for (let j = i + 1; j < lines.length; j++) {
    const ln = lines[j];
    if (ln.trim() === '---' || ln.startsWith('## ')) break;
    bodyLines.push(ln);
  }
  return { header, title, tag, body: bodyLines.join('\n').trim() };
}

function main() {
  let md;
  try { md = fs.readFileSync(NOTEBOOK, 'utf8'); } catch (e) { console.log('no notebook file'); return; }
  const top = parseTop(md);
  if (!top || !top.body) { console.log('no parseable entry'); return; }
  let last = '';
  try { last = fs.readFileSync(STATE, 'utf8').trim(); } catch (e) {}
  if (last === top.header) { console.log('already sent:', top.header); return; }

  const payload = JSON.stringify({
    source: 'aernbot',
    title: top.title,
    body: top.body,
    tag: top.tag,
    url: 'obsidian://open?vault=Obivault&file=Aernbot%2FNotebook'
  });
  const req = http.request({
    host: HOST, port: PORT, path: '/api/nexus/feed', method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
  }, (res) => {
    let d = ''; res.on('data', c => d += c);
    res.on('end', () => {
      if (res.statusCode === 200) { fs.writeFileSync(STATE, top.header); console.log('feed ok:', top.title); }
      else console.log('feed FAIL', res.statusCode, d);
    });
  });
  req.on('error', e => console.log('feed error:', e.message));
  req.write(payload); req.end();
}
main();
