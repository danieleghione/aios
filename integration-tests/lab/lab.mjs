// Settings shared by the browser checks, from the environment (see lab.py).
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
export const REPO = new URL('../../', import.meta.url).pathname;
export const { chromium } = await import(REPO + 'frontend-admin/node_modules/playwright/index.mjs');
export const BASE = process.env.AIOS_LAB_URL || 'https://127.0.0.1:28443';
export const ADMIN = process.env.AIOS_LAB_ADMIN || 'admin@example.org';
export const WORK = (process.env.AIOS_LAB_DIR || REPO + 'build/lab') + '/work/';
mkdirSync(WORK, { recursive: true });
// The same password lab.py makes up on first use, kept outside the repository.
const stored = WORK + 'admin-password';
if (!process.env.AIOS_LAB_PASSWORD && !existsSync(stored)) writeFileSync(stored, randomBytes(18).toString('base64url'), { mode: 0o600 });
export const PASSWORD = process.env.AIOS_LAB_PASSWORD || readFileSync(stored, 'utf8').trim();
