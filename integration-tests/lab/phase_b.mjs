// Phase B in the browser: repository form per provider, dashboard trends,
// users' sign-ins, backup schedule and encryption, idle unload, installed-model tools.
import { chromium, BASE, ADMIN, PASSWORD, WORK, REPO } from './lab.mjs';
import { mkdirSync, readFileSync } from 'node:fs';
const OUT = WORK + 'phase-b-shots/'; mkdirSync(OUT, { recursive: true });
const results = {};
const check = (item, name, ok, detail = '') => { (results[item] ||= []).push(!!ok); console.log(`[${ok ? 'OK ' : 'BAD'}] ${item.padEnd(7)} ${name.padEnd(55)} ${String(detail).slice(0, 140)}`); };
const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1440, height: 1000 }, ignoreHTTPSErrors: true });
let dialogs = 0;
page.on('dialog', d => { dialogs++; d.dismiss(); });
await page.goto(`${BASE}/admin/`);
await page.waitForTimeout(1500);
await page.fill('input[name=username]', ADMIN);
await page.fill('input[name=password]', PASSWORD);
await page.click('button.primary');
await page.waitForTimeout(3000);
const go = async (name) => { await page.getByRole('button', { name, exact: true }).first().click(); await page.waitForTimeout(2500); };
const text = async () => page.locator('body').innerText();

// The release number the portal shows is the one this ISO was built as.
const version = readFileSync(REPO + 'VERSION', 'utf8').trim();
const footer = await page.locator('footer').innerText();
check('VER-02', `the portal shows release ${version}`, footer.includes(`AIOS ${version} `), footer.slice(0, 60));

// UX-16
await go('Dashboard');
const dash = await text();
check('UX-16', 'trends card with memory and speed', dash.includes('Trends') && dash.includes('Memory') && dash.includes('Generation speed'), '');
check('UX-16', 'a memory sparkline is drawn', await page.locator('.spark svg polyline').count() >= 1, await page.locator('.spark').count());
await page.screenshot({ path: OUT + '01-dashboard.png', fullPage: true });

// UX-12
await go('Repositories');
await page.getByRole('button', { name: 'Add repository' }).click(); await page.waitForTimeout(800);
const modal = page.locator('.modal');
const select = modal.locator('select').first();
const labels = async () => (await modal.locator('label').allInnerTexts()).join(' | ');
await select.selectOption('huggingface'); await page.waitForTimeout(300);
const hf = await labels();
check('UX-12', 'Hugging Face: publishers, search words, limit', hf.includes('Publishers') && hf.includes('Search words') && hf.includes('Models to list'), hf.slice(0, 120));
await select.selectOption('github'); await page.waitForTimeout(300);
const gh = await labels();
check('UX-12', 'GitHub: repositories, no publishers', gh.includes('Repositories (owner/name)') && !gh.includes('Publishers'), gh.slice(0, 120));
await select.selectOption('internal'); await page.waitForTimeout(300);
const internal = await labels();
check('UX-12', 'Internal: manifest URL, private address, public key', internal.includes('manifest') && internal.includes('this network') && internal.includes('Public key'), internal.slice(0, 120));
await select.selectOption('speech'); await page.waitForTimeout(300);
check('UX-12', 'speech provider offered, with its description', (await modal.innerText()).includes('whisper builds'), '');
await select.selectOption('huggingface'); await page.waitForTimeout(300);
await modal.getByLabel('Publishers, comma separated').fill('unsloth, bartowski');
await modal.getByRole('button', { name: 'Advanced options (JSON)' }).click(); await page.waitForTimeout(300);
const raw = await modal.locator('textarea').inputValue();
check('UX-12', 'the form and the JSON view agree', JSON.parse(raw).publishers?.join(',') === 'unsloth,bartowski', raw.replace(/\s+/g, ' '));
await page.screenshot({ path: OUT + '02-repository-form.png' });
await page.keyboard.press('Escape'); await page.waitForTimeout(500);
if (await modal.count()) await modal.getByRole('button').first().click().catch(() => {});

// UX-15
await go('Installed models');
const installed = await text();
check('UX-15', 'search, order and total size', await page.getByPlaceholder(/Search/).count() > 0 && /models? · .* on disk/.test(installed), '');
await go('Hardware');
check('UX-15', 'hardware gauges and benchmark', await page.locator('.gauge').count() >= 2 && (await text()).includes('Benchmark'), await page.locator('.gauge').count());

// UX-17
await go('Users and roles');
check('UX-17', 'users show their last sign-in and sessions', /Last sign-in .* active session/.test(await text()), '');
await page.screenshot({ path: OUT + '03-users.png', fullPage: true });

// FEA-01, FEA-03, UX-13
await go('System');
await page.getByRole('button', { name: 'Backup', exact: true }).click(); await page.waitForTimeout(1500);
const backup = await text();
check('FEA-01', 'daily schedule and encryption in System → Backup', backup.includes('Make a backup every day') && backup.includes('Encryption') && backup.includes('Keep the newest'), '');
await page.screenshot({ path: OUT + '04-backup.png', fullPage: true });
await page.getByRole('button', { name: 'Configuration', exact: true }).click(); await page.waitForTimeout(1500);
check('FEA-03', 'idle unload in the model policy', (await text()).includes('Unload a model unused for this many minutes'), '');
await page.getByRole('button', { name: 'Account', exact: true }).click(); await page.waitForTimeout(1500);
check('UX-17', 'your own sessions under Account', (await text()).includes('Where you are signed in') && (await text()).includes('This browser'), '');
check('UX-13', 'no browser dialog opened', dialogs === 0, dialogs);

await b.close();
console.log('\nRESULTS');
for (const [item, oks] of Object.entries(results)) console.log(`  ${item.padEnd(7)} ${oks.every(Boolean) ? 'PASS' : 'FAIL'} (${oks.filter(Boolean).length}/${oks.length})`);
