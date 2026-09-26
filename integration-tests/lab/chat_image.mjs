import { chromium, BASE, ADMIN, PASSWORD, WORK } from './lab.mjs';
const S = WORK;
const b = await chromium.launch(); const page = await b.newPage({ ignoreHTTPSErrors: true, viewport:{width:1400,height:900} });
const t0 = Date.now(); const el = () => Math.round((Date.now()-t0)/1000)+' s';
await page.goto(`${BASE}/admin/?next=chat`); await page.waitForTimeout(1500);
await page.fill('input[name=username]', ADMIN); await page.fill('input[name=password]', PASSWORD);
await page.click('button.primary'); await page.waitForTimeout(9000);
const ok = page.getByRole('button', { name: "Okay, Let's Go!" }); if (await ok.count()) { await ok.click(); await page.waitForTimeout(800); }
await page.locator('#integration-menu-button').click(); await page.waitForTimeout(800);
await page.locator('[role=menu] button[aria-pressed]', { hasText: 'Image' }).first().click(); await page.waitForTimeout(500); console.log('pressed', await page.locator('[role=menu] button[aria-pressed]', { hasText: 'Image' }).first().getAttribute('aria-pressed'));
await page.keyboard.press('Escape'); await page.waitForTimeout(500);
await page.screenshot({ path: S+'ver01-1-toggle.png' });
const t1 = Date.now();
await page.locator('#chat-input').click(); await page.keyboard.type('a red lighthouse on a rocky coast at sunset');
await page.keyboard.press('Enter');
let found = false;
for (let i = 0; i < 360; i++) {
  await page.waitForTimeout(5000);
  const imgs = await page.locator('img').evaluateAll(els => els.filter(e => e.naturalWidth >= 256 && (e.src.includes('/api/v1/files') || e.src.startsWith('data:image') || e.src.includes('/cache/image'))).map(e => e.src.slice(0, 80) + ' ' + e.naturalWidth + 'x' + e.naturalHeight));
  if (imgs.length) { console.log('IMAGE', el(), 'generation', Math.round((Date.now()-t1)/1000)+' s', imgs); found = true; break; }
  const err = await page.locator('.text-red-500, [role=alert], .toast, [data-sonner-toast]').allInnerTexts().catch(()=>[]);
  if (/error|failed/i.test(err.join(' '))) console.log('message', el(), err.join(' | ').slice(0, 300));
  if (i % 12 === 0) { console.log('waiting', el()); await page.screenshot({ path: S+'ver01-2-wait.png' }); }
}
await page.screenshot({ path: S+'ver01-3-result.png', fullPage: true });
console.log(found ? 'VER-01 PASS' : 'VER-01 FAIL');
await b.close();
