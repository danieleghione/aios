// Round 2 browser checks. `node round2_ui.mjs pre` runs on a fresh appliance (nothing installed).
import { chromium, BASE, ADMIN, PASSWORD, WORK } from './lab.mjs';
import { mkdirSync } from 'node:fs';
const S = WORK;
const OUT = S + 'round2-shots/'; mkdirSync(OUT, { recursive: true });
const PRE = process.argv[2] === 'pre';
const results = {};
const check = (item, name, ok, detail = '') => { (results[item] ||= []).push(!!ok); console.log(`[${ok ? 'OK ' : 'BAD'}] ${item.padEnd(7)} ${name.padEnd(58)} ${String(detail).slice(0, 150)}`); };
const b = await chromium.launch();
let dialogs = 0;
async function open(width, height) {
  const page = await b.newPage({ viewport: { width, height }, ignoreHTTPSErrors: true });
  page.on('dialog', d => { dialogs++; d.dismiss(); });
  await page.goto(`${BASE}/admin/`); await page.waitForTimeout(1500);
  await page.fill('input[name=username]', ADMIN); await page.fill('input[name=password]', PASSWORD);
  await page.click('button.primary'); await page.waitForTimeout(3500);
  return page;
}
const page = await open(1440, 1000);
const go = async (name) => { await page.getByRole('button', { name, exact: true }).first().click(); await page.waitForTimeout(2500); };
const text = async () => page.locator('body').innerText();

if (PRE) {
  const dash = await text();
  check('UX-03', 'getting-started guide on a new appliance', dash.includes('Getting started') && dash.includes('Enable a repository'), '');
  await page.screenshot({ path: OUT + 'pre-01-dashboard.png', fullPage: true });
  await page.getByRole('button', { name: 'Enable a repository →' }).click(); await page.waitForTimeout(1500);
  check('UX-03', 'guide step opens the right page', (await page.locator('.topbar h1').innerText()) === 'Repositories', await page.locator('.topbar h1').innerText());
  await go('Installed models');
  await page.screenshot({ path: OUT + 'pre-02-installed-empty.png' });
  const empty = page.getByRole('button', { name: 'Open the catalogue' });
  check('UX-03', 'empty page points to the next step', await empty.count() === 1, '');
  await empty.click(); await page.waitForTimeout(1500);
  check('UX-03', 'next-step button navigates', (await page.locator('.topbar h1').innerText()) === 'Catalogue', '');
  await go('Runtime');
  check('UX-03', 'empty runtime page explains', (await text()).includes('No model has been started yet'), '');
} else {
  // UX-04 navigation, desktop
  const labels = await page.locator('nav .nav-label').allInnerTexts();
  check('UX-04', 'navigation grouped', labels.join('|') === 'OVERVIEW|MODELS|ADMINISTRATION', labels.join('|'));
  // UX-08 dashboard
  const points = await page.locator('svg.chart polyline').getAttribute('points');
  check('UX-08', 'CPU chart drawn at first paint', points && points.trim().split(' ').length >= 10, `${points ? points.trim().split(' ').length : 0} points`);
  const dash = await text();
  check('UX-08', 'active model shown with device and memory', dash.includes('Active models') && /Running on the CPU · [\d.]+ (MiB|GiB) in memory/.test(dash), (dash.match(/Running on[^\n]*/) || [''])[0]);
  check('UX-08', 'token/s explained instead of a dash', /Token\/s\s*(no answer yet|no model running|[\d.]+)/.test(dash), (dash.match(/Token\/s[^\n]*/) || [''])[0]);
  check('UX-03', 'guide hidden once a model is installed', !dash.includes('Getting started'), '');
  await page.screenshot({ path: OUT + '01-dashboard.png', fullPage: true });
  // UX-01 / UX-02 catalogue
  await go('Catalogue');
  const cards = page.locator('article.model-card');
  const first = await cards.first().innerText();
  const selects = await page.locator('article.model-card select').count();
  // Every card with a choice must preselect exactly one recommended file.
  const choices = await page.locator('article.model-card select').evaluateAll(els => els.map(s => [...s.options].filter(o => o.text.includes('recommended')).length));
  check('UX-01', 'cards grouped with a quantisation choice', choices.length > 0 && choices.every(n => n === 1), `${await cards.count()} cards, ${choices.length} with a choice`);
  const pager = await page.locator('.pager .muted').innerText().catch(() => '');
  check('UX-01', 'pager counts models', pager.includes('models'), pager);
  check('UX-02', 'parameters shown, derived ones marked', (await page.locator('article.model-card', { hasText: 'from the name' }).count()) > 0, first.split('\n')[1]);
  const period = await page.locator('select[aria-label=Period] option').allInnerTexts();
  check('CAT-01', 'period filter says released', period.includes('Released in the last 7 days'), period.join(','));
  await page.screenshot({ path: OUT + '02-catalogue.png' });
  // UX-05 audit
  await go('Audit');
  const row = await page.locator('article.audit-row').first().innerText();
  check('UX-05', 'audit shows names and readable actions', row.includes(ADMIN) && !/[0-9a-f]{8}-[0-9a-f]{4}-/.test(row.split('\n').slice(0, 3).join(' ')) && /^[A-Z]/.test(row.split('\n')[1] || ''), row.split('\n').slice(0, 3).join(' | '));
  await page.screenshot({ path: OUT + '03-audit.png' });
  // UX-06 logs
  await go('Logs');
  const sources = await page.locator('select[aria-label=Service] option').allInnerTexts();
  check('UX-06', 'all services selectable', sources.length >= 10, sources.join(','));
  const firstLine = await page.locator('.log-line').first().innerText().catch(() => '');
  check('UX-06', 'lines dated, newest first', /\d{1,2}\/\d{1,2}\/\d{4}/.test(firstLine) && (await text()).includes('Newest first'), firstLine.slice(0, 90));
  await page.locator('select[aria-label=Service]').selectOption('open-webui'); await page.waitForTimeout(2500);
  const owui = await page.locator('.log-view').innerText();
  check('UX-06', 'Open WebUI lines are text', owui.length > 100 && !/"MESSAGE"|\[\d{2,3},\s*\d{2,3}/.test(owui), owui.split('\n')[0].slice(0, 90));
  await page.screenshot({ path: OUT + '04-logs.png' });
  // UX-07, UX-09, UX-11 system
  await go('System');
  const ops = await page.locator('.job').first().innerText().catch(() => '');
  check('UX-07', 'operations named, dated, readable', /^(System configuration|Network change|Network confirmation|Operating system updates|Backup|Restore|Restart|TLS certificate)/.test(ops) && /\d{4}/.test(ops) && !ops.startsWith('{'), ops.split('\n').slice(0, 2).join(' | '));
  await page.getByRole('button', { name: 'TLS', exact: true }).click(); await page.waitForTimeout(1500);
  const tls = await text();
  check('UX-09', 'current certificate and expiry shown', tls.includes('Current certificate') && tls.includes('days left') && tls.includes('SHA-256 fingerprint'), (tls.match(/Valid until[^\n]*\n[^\n]*/) || [''])[0]);
  await page.screenshot({ path: OUT + '05-tls.png', fullPage: true });
  // ENG-04 API keys page
  await page.getByRole('button', { name: 'API keys', exact: true }).click(); await page.waitForTimeout(1000);
  await page.fill('input[name=name]', 'browser test'); await page.locator('form button.primary', { hasText: 'Create key' }).click(); await page.waitForTimeout(1500);
  const shown = await page.locator('.notice .mono').innerText().catch(() => '');
  check('ENG-04', 'key shown once in the page', shown.startsWith('aios_'), shown.slice(0, 12) + '…');
  await page.getByRole('button', { name: 'Done' }).click(); await page.waitForTimeout(500);
  check('ENG-04', 'key not shown again', !(await text()).includes(shown), '');
  await page.screenshot({ path: OUT + '06-apikeys.png', fullPage: true });
  // UX-11 portal confirmation and specific outcome
  const before = dialogs;
  await page.locator('.row', { hasText: 'browser test' }).getByRole('button', { name: 'Revoke' }).click(); await page.waitForTimeout(800);
  const modal = await page.locator('.modal').innerText().catch(() => '');
  check('UX-11', 'confirmation in the portal, not the browser', modal.includes('Please confirm') && dialogs === before, modal.split('\n')[0]);
  await page.locator('.modal button.primary', { hasText: 'Confirm' }).click(); await page.waitForTimeout(1500);
  const notice = await page.locator('.notice').first().innerText().catch(() => '');
  check('UX-11', 'outcome says what happened', notice.startsWith('Key revoked.'), notice);
  check('ENG-04', 'revoked key marked', (await page.locator('.row', { hasText: 'browser test' }).innerText()).includes('REVOKED'), '');
  await go('Repositories');
  await page.locator('article.card', { hasText: 'DeepSeek (GGUF)' }).getByRole('button', { name: 'Synchronise' }).click(); await page.waitForTimeout(1500);
  check('UX-11', 'synchronise reports it started', (await page.locator('.notice').first().innerText()).startsWith('Synchronisation started.'), '');
  // UX-04 phone
  const phone = await open(390, 844);
  const nav = await phone.locator('nav#main-nav').isVisible();
  const toggle = phone.locator('.menu-toggle');
  const signOut = await phone.getByRole('button', { name: 'Sign out' }).boundingBox();
  check('UX-04', 'phone: menu folded behind a button', !nav && await toggle.isVisible(), await toggle.innerText());
  check('UX-04', 'phone: Sign out inside the screen', signOut && signOut.x >= 0 && signOut.x + signOut.width <= 390, JSON.stringify(signOut));
  await phone.screenshot({ path: OUT + '07-phone-closed.png' });
  await toggle.click(); await phone.waitForTimeout(500);
  const entries = await phone.locator('nav#main-nav button').count();
  check('UX-04', 'phone: all 11 pages reachable', entries === 11 && await phone.locator('nav#main-nav').isVisible(), entries);
  await phone.screenshot({ path: OUT + '08-phone-open.png', fullPage: true });
  await phone.getByRole('button', { name: 'Logs', exact: true }).click(); await phone.waitForTimeout(1000);
  check('UX-04', 'phone: menu closes after choosing', !(await phone.locator('nav#main-nav').isVisible()) && (await phone.locator('.topbar h1').innerText()) === 'Logs', '');
  const scroll = await phone.evaluate(() => document.documentElement.scrollWidth);
  check('UX-04', 'phone: no horizontal scroll', scroll <= 390, scroll);
}
check('UX-11', 'no native browser dialog used', dialogs === 0, dialogs);
await b.close();
console.log('\nRESULTS');
for (const [k, v] of Object.entries(results)) console.log(`  ${k.padEnd(7)} ${v.every(Boolean) ? 'PASS' : 'FAIL'} (${v.filter(Boolean).length}/${v.length})`);
