// FIX-03 on the lab: a chat setting pointed away from the appliance is put back
// at the next boot, before the chat starts.
import { chromium, BASE, ADMIN, PASSWORD } from './lab.mjs';
const b = await chromium.launch();
const page = await b.newPage({ ignoreHTTPSErrors: true });
const results = [];
const check = (name, ok, detail = '') => { results.push(!!ok); console.log(`[${ok ? 'OK ' : 'BAD'}] FIX-03  ${name.padEnd(56)} ${String(detail).slice(0, 140)}`); };
async function chat() {
  for (let i = 0; i < 90; i++) {
    try {
      await page.goto(`${BASE}/admin/?next=chat`); await page.waitForTimeout(1500);
      if (await page.locator('input[name=username]').count()) {
        await page.fill('input[name=username]', ADMIN); await page.fill('input[name=password]', PASSWORD);
        await page.click('button.primary');
      }
      await page.waitForTimeout(6000);
      if (!(await page.content()).includes('The chat is starting') && await page.evaluate(() => Boolean(localStorage.token))) return true;
    } catch { /* the appliance is restarting */ }
    await page.waitForTimeout(10000);
  }
  return false;
}
const images = () => page.evaluate(async () => (await fetch('/api/v1/images/config', { headers: { Authorization: 'Bearer ' + localStorage.token } })).json());
check('chat reachable', await chat());
const before = await images();
const stale = await page.evaluate(async (config) => {
  const r = await fetch('/api/v1/images/config/update', { method: 'POST', headers: { Authorization: 'Bearer ' + localStorage.token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...config, IMAGES_OPENAI_API_BASE_URL: 'http://127.0.0.1:9/v1', IMAGES_OPENAI_API_KEY: 'stale-key' }) });
  return r.json();
}, before);
check('the chat saved a setting pointing elsewhere', stale.IMAGES_OPENAI_API_KEY === 'stale-key', stale.IMAGES_OPENAI_API_BASE_URL);
// Restart the appliance from the portal.
const login = await page.request.post(`${BASE}/api/v1/aios/auth/login`, { data: { username: ADMIN, password: PASSWORD } });
const csrf = (await login.json()).csrf;
const reboot = await page.request.post(`${BASE}/api/v1/aios/system/power/reboot`, { headers: { 'x-csrf-token': csrf } });
check('restart requested', reboot.ok(), reboot.status());
await page.waitForTimeout(90000);
check('chat back after the restart', await chat());
const after = await images();
check('the chat points at the appliance again', after.IMAGES_OPENAI_API_BASE_URL === 'http://127.0.0.1:8081/v1' && after.IMAGES_OPENAI_API_KEY !== 'stale-key', after.IMAGES_OPENAI_API_BASE_URL);
await b.close();
console.log('\nRESULTS');
console.log(`  FIX-03  ${results.every(Boolean) ? 'PASS' : 'FAIL'} (${results.filter(Boolean).length}/${results.length})`);
