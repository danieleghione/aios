// The screenshots of docs/demo.md, taken from a lab appliance after
// demo_prepare.py. Each one is independent: a page that cannot be reached is
// reported and the others are still taken.
import { chromium, BASE, ADMIN, PASSWORD, WORK, REPO } from './lab.mjs';
const OUT = REPO + 'docs/images/';
const only = process.argv.slice(2);
const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1440, height: 900 }, ignoreHTTPSErrors: true, colorScheme: 'dark' });
const go = async (name) => { await page.getByRole('button', { name, exact: true }).first().click(); await page.waitForTimeout(2500); };
const tab = async (name) => { await page.getByRole('button', { name, exact: true }).first().click(); await page.waitForTimeout(1500); };
const shot = async (file, full = false) => { await page.screenshot({ path: OUT + file, fullPage: full }); console.log('saved', file); };
const card = (text) => page.locator('article.card', { hasText: text }).first();
async function take(name, steps) {
  if (only.length && !only.includes(name)) return;
  try { await steps(); } catch (e) { console.log('SKIPPED', name, String(e.message).split('\n')[0]); await page.keyboard.press('Escape').catch(() => {}); }
}

await page.goto(`${BASE}/admin/`); await page.waitForTimeout(1500);
await take('sign-in', () => shot('portal-sign-in.png'));
await page.fill('input[name=username]', ADMIN); await page.fill('input[name=password]', PASSWORD);
await page.click('button.primary'); await page.waitForTimeout(4000);

await take('dashboard', async () => { await go('Dashboard'); await page.waitForTimeout(3000); await shot('portal-dashboard.png'); });
await take('hardware', async () => { await go('Hardware'); await shot('portal-hardware.png'); });
await take('repositories', async () => { await go('Repositories'); await shot('portal-repositories.png'); });
await take('repository-form', async () => {
  await go('Repositories'); await page.getByRole('button', { name: 'Add repository' }).click(); await page.waitForTimeout(800);
  await page.locator('.modal select').first().selectOption('huggingface'); await page.waitForTimeout(300);
  await page.getByLabel('Name').fill('Qwen and Gemma'); await page.getByLabel('HTTPS URL').fill('https://huggingface.co');
  await page.getByLabel('Publishers, comma separated').fill('unsloth, bartowski, ggml-org');
  await page.getByLabel('Search words, comma separated').fill('Qwen3, Gemma');
  await shot('portal-repository-form.png'); await page.locator('[aria-label=Close]').click();
});
await take('catalogue', async () => { await go('Catalogue'); await shot('portal-catalogue.png'); });
await take('catalogue-voice', async () => {
  await go('Catalogue'); await page.getByPlaceholder(/Search/).first().fill('TTS'); await page.waitForTimeout(2500);
  await shot('portal-voice-catalogue.png');
});
await take('installed', async () => { await go('Installed models'); await shot('portal-installed-models.png'); });
await take('runtime', async () => { await go('Runtime'); await shot('portal-runtime.png'); });
await take('downloads', async () => { await go('Downloads'); await shot('portal-downloads.png'); });
await take('users', async () => { await go('Users and roles'); await shot('portal-users.png'); });
await take('backup', async () => { await go('System'); await tab('Backup'); await shot('portal-backup.png'); });
await take('notifications', async () => { await go('System'); await tab('Notifications'); await shot('portal-notifications.png'); });
await take('api-keys', async () => { await go('System'); await tab('API keys'); await shot('portal-api-keys.png'); });
await take('updates', async () => { await go('System'); await tab('Updates'); await shot('portal-updates.png'); });
await take('speak', async () => {
  await go('Installed models'); await card('VOICE MODEL').getByRole('button', { name: 'Speak a text' }).click();
  await page.getByRole('textbox', { name: 'Text' }).fill('Good morning! This is the voice of this appliance: I read your texts without sending them anywhere.');
  await page.getByLabel('Voice (the same for the whole text)').selectOption('nova');
  await page.getByRole('button', { name: 'Speak', exact: true }).click();
  await page.locator('audio').waitFor({ timeout: 900000 }); await page.waitForTimeout(1500);
  await shot('portal-speak.png'); await page.locator('[aria-label=Close]').click();
});
await take('transcribe', async () => {
  await go('Installed models'); await card('SPEECH MODEL').getByRole('button', { name: 'Transcribe a file' }).click();
  await page.locator('.modal input[type=file]').setInputFiles(WORK + 'demo-recording.wav');
  await page.getByRole('button', { name: 'Transcribe', exact: true }).click();
  await page.locator('.transcript').waitFor({ timeout: 900000 }); await page.waitForTimeout(500);
  await shot('portal-transcribe.png'); await page.locator('[aria-label=Close]').click();
});
await take('image', async () => {
  await go('Installed models'); await card('IMAGE MODEL').getByRole('button', { name: 'Generate a picture' }).click();
  await page.getByLabel('Prompt').fill('a lighthouse on a rocky coast at sunset, photorealistic');
  await page.getByLabel('Steps').fill('8');
  await page.getByRole('button', { name: 'Generate', exact: true }).click();
  await page.locator('.modal img').first().waitFor({ timeout: 1800000 }); await page.waitForTimeout(1000);
  await shot('portal-image-studio.png'); await page.locator('[aria-label=Close]').click();
});
await take('chat', async () => {
  const chat = await b.newPage({ viewport: { width: 1440, height: 900 }, ignoreHTTPSErrors: true, colorScheme: 'dark' });
  await chat.goto(`${BASE}/admin/?next=chat`); await chat.waitForTimeout(1500);
  if (await chat.locator('input[name=username]').count()) { await chat.fill('input[name=username]', ADMIN); await chat.fill('input[name=password]', PASSWORD); await chat.click('button.primary'); }
  await chat.waitForTimeout(9000);
  const ok = chat.getByRole('button', { name: "Okay, Let's Go!" }); if (await ok.count()) { await ok.click(); await chat.waitForTimeout(800); }
  await chat.locator('#chat-input').click(); await chat.keyboard.type('Write a Python function that returns the sum of a list, with a short explanation.');
  await chat.keyboard.press('Enter');
  for (let i = 0; i < 120; i++) { await chat.waitForTimeout(5000); if (!(await chat.locator('button[aria-label="Stop"], #stop-response-button').count())) break; }
  await chat.waitForTimeout(3000);
  await chat.screenshot({ path: OUT + 'chat-answer.png' }); console.log('saved chat-answer.png');
  await chat.close();
});
await b.close();
