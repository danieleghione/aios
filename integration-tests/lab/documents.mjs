// ENG-03: a document attached in the chat is embedded offline and retrievable.
import { chromium, BASE, ADMIN, PASSWORD, WORK } from './lab.mjs';
const b = await chromium.launch(); const page = await b.newPage({ ignoreHTTPSErrors: true });
await page.goto(`${BASE}/admin/?next=chat`); await page.waitForTimeout(1500);
await page.fill('input[name=username]', ADMIN); await page.fill('input[name=password]', PASSWORD);
await page.click('button.primary');
for (let i = 0; i < 60; i++) { await page.waitForTimeout(5000); if (!(await page.content()).includes('The chat is starting')) break; await page.reload(); }
await page.waitForTimeout(5000);
const out = await page.evaluate(async () => {
  const h = { 'Authorization': 'Bearer ' + localStorage.token };
  const emb = await (await fetch('/api/v1/retrieval/embedding', { headers: h })).json();
  const fd = new FormData();
  fd.append('file', new Blob(['Lab notes.\n\nThe AIOS lab appliance codename is BLUE HERON. It was installed on 22 September 2026 on a two-core virtual machine.\n\nThe backup rotation keeps seven daily archives.'], { type: 'text/plain' }), 'lab-notes.txt');
  const file = await (await fetch('/api/v1/files/?process=true', { method: 'POST', headers: h, body: fd })).json();
  let status;
  for (let i = 0; i < 60; i++) { await new Promise(r => setTimeout(r, 3000)); const f = await (await fetch('/api/v1/files/' + file.id, { headers: h })).json(); status = f?.data?.status; if (status !== 'pending') break; }
  const q = await fetch('/api/v1/retrieval/query/doc', { method: 'POST', headers: { ...h, 'Content-Type': 'application/json' }, body: JSON.stringify({ collection_name: 'file-' + file.id, query: 'What is the codename of the lab appliance?', k: 1 }) });
  const res = await q.json();
  return { model: emb.RAG_EMBEDDING_MODEL, engine: emb.RAG_EMBEDDING_ENGINE, status, query: q.status, hit: JSON.stringify(res.documents || res).slice(0, 200) };
});
console.log(JSON.stringify(out));
const ok = out.status === 'completed' && out.hit.includes('BLUE HERON');
console.log(`[${ok ? 'OK ' : 'BAD'}] ENG-03  document embedded offline and retrieved`);
await b.close();
