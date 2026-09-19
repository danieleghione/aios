let csrf = '';
export function setCSRF(value: string) { csrf = value; }
export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api/v1/aios/' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: body === undefined ? undefined : JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || `HTTP ${response.status}`);
  return value;
}
export async function upload(file: File) {
  const form = new FormData(); form.append('file', file);
  const response = await fetch('/api/v1/aios/backups/upload', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf }, body: form });
  const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || 'Upload fallito'); return result;
}
export const bytes = (value: number) => { if (!Number.isFinite(value)) return '—'; const unit = value >= 1073741824 ? 1073741824 : 1048576; return `${(value / unit).toFixed(1)} ${unit === 1073741824 ? 'GiB' : 'MiB'}`; };
