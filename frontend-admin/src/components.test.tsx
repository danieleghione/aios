// @vitest-environment jsdom
// The portal's main components, rendered with the data the API returns.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RepositoryForm, Models, Repositories, VoiceStudio } from './pages/Models';
import { Users, Notifications } from './pages/Admin';
import { CudaPackage, Spark } from './pages/Overview';
import { setAsker } from './ui';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

// Answers the portal's GET requests from a table keyed by API path.
function serve(answers: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const path = String(url).replace('/api/v1/aios/', '');
    return { ok: path in answers, status: path in answers ? 200 : 404, json: async () => answers[path] ?? { error: { message: 'Not found' } } };
  }));
}

describe('Repository form', () => {
  const repo = { name: 'Hugging Face', provider: 'huggingface', url: 'https://huggingface.co', enabled: true, config: { publishers: ['unsloth'], custom: 'kept' } };

  it('asks each provider for its own options', () => {
    render(<RepositoryForm repo={repo} close={() => {}} action={vi.fn()} />);
    expect(screen.getByLabelText('Publishers, comma separated')).toHaveProperty('value', 'unsloth');
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'github' } });
    expect(screen.getByLabelText('Repositories (owner/name), comma separated')).toBeTruthy();
    expect(screen.queryByLabelText('Publishers, comma separated')).toBeNull();
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'internal' } });
    expect(screen.getByText("Public key of the manifest's signer (Ed25519 PEM, optional)")).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'voice' } });
    expect(screen.getByText(/Voice models: text to speech/)).toBeTruthy();
  });

  it('keeps options it has no field for, and shows them as JSON', async () => {
    const action = vi.fn(async () => ({}));
    render(<RepositoryForm repo={repo} close={() => {}} action={action} />);
    fireEvent.change(screen.getByLabelText('Search words, comma separated'), { target: { value: 'Qwen, Gemma' } });
    fireEvent.click(screen.getByText('Advanced options (JSON)'));
    const json = JSON.parse((screen.getByLabelText('Provider options (JSON)') as HTMLTextAreaElement).value);
    expect(json).toEqual({ publishers: ['unsloth'], custom: 'kept', search: ['Qwen', 'Gemma'] });
    fireEvent.click(screen.getByText('Back to the form'));
    fireEvent.click(screen.getByText('Save repository'));
    await waitFor(() => expect(action).toHaveBeenCalled());
    expect(action.mock.calls[0]).toEqual(['repositories', 'POST', expect.objectContaining({ config: { publishers: ['unsloth'], custom: 'kept', search: ['Qwen', 'Gemma'] } })]);
  });

  it('refuses advanced options that are not JSON', () => {
    render(<RepositoryForm repo={repo} close={() => {}} action={vi.fn()} />);
    fireEvent.click(screen.getByText('Advanced options (JSON)'));
    fireEvent.change(screen.getByLabelText('Provider options (JSON)'), { target: { value: '{not json' } });
    fireEvent.click(screen.getByText('Back to the form'));
    expect(screen.getByText('The advanced options are not valid JSON')).toBeTruthy();
  });
});

describe('Dashboard trends', () => {
  it('says so when a machine has no sensor', () => {
    render(<Spark label="Hottest sensor" values={[null, null]} unit=" °C" />);
    expect(screen.getByText('Not measured on this machine')).toBeTruthy();
  });

  it('draws the samples it has and shows the latest', () => {
    const { container } = render(<Spark label="Memory" values={[40, null, 42.5]} unit="%" max={100} />);
    expect(container.querySelector('polyline')?.getAttribute('points')?.split(' ')).toHaveLength(2);
    expect(screen.getByText('42.5%')).toBeTruthy();
  });
});

describe('Installed models', () => {
  const model = (id: string, extra: object) => ({ id, display_name: 'Model ' + id, quantization: 'Q4_K_M', size: 2 ** 30, license: 'MIT', kind: 'text',
    state: 'INSTALLED', published: 0, installed_at: 1, config: {}, gguf: {}, ...extra });

  it('offers the newer revision and marks the one it replaced', async () => {
    serve({ models: { items: [
      model('a', { update: { id: 'n', revision: 'r2', size: 2 ** 30, license: 'MIT', released: '2026-09-01' } }),
      model('b', { replaced_by: 'a', update: null }),
      model('v', { kind: 'voice', display_name: 'Qwen3-TTS 1.7B · Q4_K_M', update: null }),
    ] } });
    const action = vi.fn(async () => ({}));
    render(<Models action={action} />);
    await screen.findByText('UPDATE AVAILABLE');
    expect(screen.getByText('PREVIOUS REVISION')).toBeTruthy();
    expect(screen.getByText('VOICE MODEL')).toBeTruthy();
    expect(screen.getByText('Speak a text')).toBeTruthy();
    fireEvent.click(screen.getByText('Update'));
    await waitFor(() => expect(action).toHaveBeenCalledWith('models/a/update', 'POST', { accept_license: false }));
  });

  it('asks before an update that changes the licence', async () => {
    serve({ models: { items: [model('a', { update: { id: 'n', revision: 'r2', size: 1, license: 'other', released: null } })] } });
    const asked: string[] = [];
    setAsker(async text => { asked.push(text); return true; });
    const action = vi.fn(async () => ({}));
    render(<Models action={action} />);
    fireEvent.click(await screen.findByText('Update'));
    await waitFor(() => expect(action).toHaveBeenCalledWith('models/a/update', 'POST', { accept_license: true }));
    expect(asked[0]).toContain('another licence: other');
  });
});

describe('Users and sessions', () => {
  it('shows sign-ins and offers to sign an account out', async () => {
    serve({ users: { items: [
      { id: 'me', username: 'admin', role: 'SUPERADMIN', last_login: 1790000000, sessions: 1 },
      { id: 'u1', username: 'analyst', role: 'VIEWER', last_login: 1790000000, sessions: 2 },
      { id: 'u2', username: 'newcomer', role: 'VIEWER', last_login: null, sessions: 0 },
    ] } });
    render(<Users action={vi.fn()} me={{ id: 'me', role: 'SUPERADMIN' }} />);
    await screen.findByText('analyst');
    expect(screen.getByText(/2 active sessions/)).toBeTruthy();
    expect(screen.getByText(/Never signed in · 0 active sessions/)).toBeTruthy();
    expect(screen.getAllByText('Sign out everywhere')).toHaveLength(1);
  });
});

describe('Notifications', () => {
  it('keeps saved secrets out of the form and shows the last delivery', async () => {
    serve({ 'system/notifications': { min_severity: 'WARNING', webhook_url: 'https://hooks.example.org/a', webhook_secret_set: true, email_to: [],
      email_from: '', smtp_host: '', smtp_port: 587, smtp_security: 'starttls', smtp_username: '', smtp_password_set: false,
      status: { webhook: { ok: false, error: 'Webhook answered HTTP 500', at: 1790000000 } } } });
    const action = vi.fn(async () => ({}));
    render(<Notifications action={action} />);
    await screen.findByText('Signing secret (saved; type to replace)');
    expect(screen.getByText(/Webhook answered HTTP 500/)).toBeTruthy();
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(action).toHaveBeenCalled());
    const [path, method, body] = action.mock.calls[0] as unknown as [string, string, Record<string, unknown>];
    expect([path, method, body.webhook_url, body.webhook_secret, body.smtp_password]).toEqual(['system/notifications', 'PUT', 'https://hooks.example.org/a', null, null]);
  });
});

describe('Voice', () => {
  it('speaks only a text that is there, in the voice chosen', async () => {
    serve({ 'speech/voices': { default: 'alloy', voices: [{ id: 'alloy', name: 'Alloy · medium' }, { id: 'onyx', name: 'Onyx · deep' }] } });
    render(<VoiceStudio model={{ id: 'v', display_name: 'Qwen3-TTS' }} close={() => {}} />);
    const speak = screen.getByText('Speak') as HTMLButtonElement;
    expect(speak.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Buongiorno' } });
    expect(speak.disabled).toBe(false);
    expect(screen.getByLabelText('Language')).toHaveProperty('value', '');
    await screen.findByText('Onyx · deep');
    fireEvent.change(screen.getByLabelText('Voice (the same for the whole text)'), { target: { value: 'onyx' } });
    expect(screen.getByLabelText('Voice (the same for the whole text)')).toHaveProperty('value', 'onyx');
  });
});

describe('CUDA package', () => {
  it('says what is installed and offers the way back', async () => {
    const asked: string[] = [];
    setAsker(async text => { asked.push(text); return true; });
    const action = vi.fn(async () => ({}));
    render(<CudaPackage cuda={{ installed: true, version: '1.11.0-cuda' }} nvidia={{ state: 'nvidia' }} action={action} />);
    expect(screen.getByText('CUDA package 1.11.0-cuda')).toBeTruthy();
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(action).toHaveBeenCalledWith('system/update/cuda/remove', 'POST'));
    expect(asked[0]).toContain('go back to Vulkan');
  });

  it('points NVIDIA owners at the package and says nothing to others', () => {
    const { container, rerender } = render(<CudaPackage cuda={{ installed: false, version: '' }} nvidia={null} />);
    expect(container.textContent).toBe('');
    rerender(<CudaPackage cuda={{ installed: false, version: '' }} nvidia={{ state: 'nvidia' }} />);
    expect(container.textContent).toContain('optional CUDA package');
  });
});

describe('Repositories', () => {
  it('shows no stray number for a disabled repository', async () => {
    serve({ repositories: { items: [{ id: 'r', name: 'Hugging Face', provider: 'huggingface', url: 'https://huggingface.co', enabled: 0, status: 'DISABLED',
      error: 'an old error', has_token: false, found: 0, last_sync: null, duration: null, config: {} }] } });
    const { container } = render(<Repositories action={vi.fn()} />);
    await screen.findByText('Hugging Face');
    expect([...container.querySelectorAll('p, div')].some(e => e.childNodes.length === 1 && e.textContent === '0')).toBe(false);
    expect(screen.queryByText('an old error')).toBeNull();
  });
});
