// Phase D in the chat: with a voice model published, Open WebUI's read-aloud
// button speaks through this appliance, and the chat's model list holds only
// language models.
import { chromium, BASE, ADMIN, PASSWORD } from './lab.mjs';
const results = [];
const check = (name, ok, detail = '') => { results.push(!!ok); console.log(`[${ok ? 'OK ' : 'BAD'}] FEA-06  ${name.padEnd(56)} ${String(detail).slice(0, 140)}`); };
const b = await chromium.launch();
const page = await b.newPage({ ignoreHTTPSErrors: true });
await page.goto(`${BASE}/admin/?next=chat`); await page.waitForTimeout(1500);
await page.fill('input[name=username]', ADMIN); await page.fill('input[name=password]', PASSWORD);
await page.click('button.primary'); await page.waitForTimeout(9000);
const config = await page.evaluate(async () => (await fetch('/api/config', { headers: { Authorization: 'Bearer ' + localStorage.token } })).json());
const voices = await page.evaluate(async () => (await fetch('/api/v1/audio/voices', { headers: { Authorization: 'Bearer ' + localStorage.token } })).json());
check('the chat offers the appliance voices', JSON.stringify(voices).includes('Onyx · deep'), JSON.stringify(voices).slice(0, 120));
check('the chat reads aloud with the appliance voice', config?.audio?.tts?.engine === 'openai', JSON.stringify(config?.audio?.tts || {}));
const spoken = await page.evaluate(async () => {
  const r = await fetch('/api/v1/audio/speech', { method: 'POST', headers: { Authorization: 'Bearer ' + localStorage.token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ input: 'Hello from the chat.', voice: 'alloy', model: 'aios-voice' }) });
  return { status: r.status, type: r.headers.get('content-type'), size: (await r.arrayBuffer()).byteLength };
});
check('the chat\'s own speech route answers with audio', spoken.status === 200 && /audio/.test(spoken.type || '') && spoken.size > 1000, JSON.stringify(spoken));
// The portal's player: the spoken text arrives as a blob the page must be allowed to play.
const portal = await b.newPage({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 1000 } });
const blocked = [];
portal.on('console', m => { if (/Content Security Policy/i.test(m.text())) blocked.push(m.text()); });
await portal.goto(`${BASE}/admin/`); await portal.waitForTimeout(1500);
await portal.fill('input[name=username]', ADMIN); await portal.fill('input[name=password]', PASSWORD);
await portal.click('button.primary'); await portal.waitForTimeout(3000);
await portal.getByRole('button', { name: 'Installed models', exact: true }).first().click(); await portal.waitForTimeout(2500);
await portal.locator('article.card', { hasText: 'VOICE MODEL' }).first().getByRole('button', { name: 'Speak a text' }).click();
await portal.getByRole('textbox', { name: 'Text' }).fill('Buongiorno a tutti.');
await portal.getByRole('button', { name: 'Speak', exact: true }).click();
await portal.locator('audio').waitFor({ timeout: 600000 });
const played = await portal.locator('audio').evaluate(a => new Promise(done => {
  if (a.readyState >= 1) return done({ duration: a.duration, error: null });
  a.addEventListener('loadedmetadata', () => done({ duration: a.duration, error: null }));
  a.addEventListener('error', () => done({ duration: 0, error: a.error && a.error.code }));
  setTimeout(() => done({ duration: 0, error: 'timeout' }), 20000);
}));
check('the portal player loads the spoken audio', played.duration > 0.5 && !blocked.length, JSON.stringify(played) + (blocked[0] || '').slice(0, 80));
await b.close();
console.log('\nRESULTS');
console.log(`  FEA-06  ${results.every(Boolean) ? 'PASS' : 'FAIL'} (${results.filter(Boolean).length}/${results.length})`);
