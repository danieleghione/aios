import {useEffect, useState, type FormEvent} from 'react';
import { api, bytes, setCSRF, upload } from './api';
import { Badge, Field, Modal, JOB_NAMES, setAsker, setNavigator, setNotifier, type Action, type Item } from './ui';
import { Dashboard, Hardware } from './pages/Overview';
import { Catalog, Downloads, Models, Repositories, Runtime } from './pages/Models';
import { Audit, Logs, System, Users } from './pages/Admin';
export { Badge };

const pages = ['Dashboard', 'Hardware', 'Catalogue', 'Installed models', 'Downloads', 'Runtime', 'Repositories', 'Users and roles', 'Audit', 'System', 'Logs'];
const icons = ['◫', '⌘', '▦', '▣', '↓', '▷', '⌗', '♙', '≡', '⚙', '⌁'];
// Eleven flat entries read as one list; three groups say what each is for.
const groups: [string, string[]][] = [['OVERVIEW', ['Dashboard', 'Hardware']], ['MODELS', ['Catalogue', 'Installed models', 'Downloads', 'Runtime', 'Repositories']], ['ADMINISTRATION', ['Users and roles', 'System', 'Audit', 'Logs']]];
// What happened, in words: "Operation completed." said nothing about which one.
function outcome(path: string, method: string, result: Item): string {
  const last = path.split('/').pop() || '';
  if (result?.state === 'QUEUED') return `${JOB_NAMES[result.action] || 'Operation'} queued: follow it under System operations.`;
  if (path.startsWith('runtime/')) return {start: 'Model starting: the state updates on its own.', stop: 'Model stopped.', restart: 'Model restarting with the saved configuration.'}[last] || 'Done.';
  if (path.startsWith('repositories/') && last === 'test') return `Connection test: ${result?.status || 'done'}${result?.found !== undefined ? ` · ${result.found} files found` : ''}.`;
  if (path.startsWith('repositories/') && last === 'sync') return 'Synchronisation started.';
  if (path.startsWith('repositories')) return 'Repository saved.';
  if (path.endsWith('/password') && path.startsWith('users/')) return 'Password reset: the user chooses a new one at the next sign-in.';
  if (path.startsWith('users')) return method === 'DELETE' ? 'User deleted.' : method === 'POST' ? 'User created: the password must be changed at first sign-in.' : 'User updated.';
  if (path.startsWith('models/') && last === 'install') return 'Download queued: follow it under Downloads.';
  if (path.startsWith('models/')) return method === 'DELETE' ? 'Model deleted.' : 'Model updated.';
  if (path.startsWith('downloads/')) return `Download ${last === 'retry' ? 'restarted' : last + 'd'}.`;
  if (path === 'system/release-key') return method === 'DELETE' ? 'Release key removed: signed updates are refused.' : 'Release key installed.';
  if (path === 'system/policy') return 'Model policy saved.';
  if (path.startsWith('hardware/')) return 'Benchmark completed.';
  if (path.startsWith('catalog/')) return 'Saved.';
  if (path.startsWith('apikeys')) return method === 'DELETE' ? 'Key revoked.' : 'Key created: copy it now.';
  return 'Done.';
}
export default function App() {
  const [question, setQuestion] = useState<{text: string, answer: (ok: boolean) => void} | null>(null), [menu, setMenu] = useState(false);
  setAsker(text => new Promise(answer => setQuestion({text, answer})));
  setNotifier(text => setNotice(text));
  setNavigator(p => {setPage(p); setNotice('');});
  const [user, setUser] = useState<Item | null>(null), [initialized, setInitialized] = useState(true), [checking, setChecking] = useState(true), [page, setPage] = useState('Dashboard'), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false);
  const load = async () => { try { const status = await api('auth/status'); setInitialized(status.initialized); if (status.initialized) { const u = await api('auth/me'); setUser(u); setCSRF(u.csrf); if (new URLSearchParams(location.search).get('signout') === 'chat') { await api('auth/logout','POST'); localStorage.removeItem('token'); location.replace('/admin/'); return; } if (!u.must_change && new URLSearchParams(location.search).get('next') === 'chat') { localStorage.removeItem('token'); location.replace('/'); } } } catch { setUser(null); } finally { setChecking(false); } };
  useEffect(() => { void load(); }, []);
  async function action(path: string, method = 'POST', body?: unknown) { setBusy(true); setNotice(''); try { const result = await api(path, method, body); setNotice(outcome(path, method, result)); return result; } catch (e) { setNotice(e instanceof Error ? e.message : 'Operation failed'); throw e; } finally { setBusy(false); } }
  if (checking) return <main className="login"><h1>AIOS</h1><p>Connecting to the appliance…</p></main>;
  if (!user) return <Login initialized={initialized} done={load}/>;
  if (user.must_change) return <Password done={() => {setUser(null); void load();}} action={action}/>;
  return <div className="shell"><aside><a className="brand" href="/admin/"><img src="/admin/aios-mark.png" alt="AIOS"/><small>INTELLIGENCE OPERATING SYSTEM</small></a><button className="menu-toggle" aria-expanded={menu} aria-controls="main-nav" onClick={() => setMenu(m => !m)}>{menu ? '× Close' : '☰ ' + page}</button><nav id="main-nav" className={menu ? 'open' : ''}>{groups.map(([label, entries]) => <div className="nav-group" key={label}><div className="nav-label">{label}</div>{entries.map(p => <button key={p} className={page === p ? 'selected' : ''} aria-current={page === p ? 'page' : undefined} onClick={() => {setPage(p); setNotice(''); setMenu(false);}}><span aria-hidden="true">{icons[pages.indexOf(p)]}</span>{p}</button>)}</div>)}</nav><div className="sidebar-bottom"><a className="chat-open" href="/" target="_blank" rel="noreferrer">Open WebUI ↗</a><p><span className="online-dot"/> Local inference</p></div></aside><main><header className="topbar"><div><small>AIOS / ADMINISTRATION</small><h1>{page}</h1></div><div className="account"><a className="chat-open" href="/" target="_blank" rel="noreferrer">Open WebUI ↗</a><span>{user.username}<small>{user.role}</small></span><button onClick={() => {void api('auth/logout','POST').then(() => setUser(null));}}>Sign out</button></div></header>{notice && <div role="status" className="notice">{notice}<button onClick={() => setNotice('')}>×</button></div>}{busy && <div className="busy"/>}{question && <Modal title="Please confirm" close={() => {question.answer(false); setQuestion(null);}}><p>{question.text}</p><div className="actions"><button onClick={() => {question.answer(false); setQuestion(null);}}>Cancel</button><button className="primary" autoFocus onClick={() => {question.answer(true); setQuestion(null);}}>Confirm</button></div></Modal>}<section className="content">{page === 'Dashboard' ? <Dashboard/> : page === 'Hardware' ? <Hardware action={action}/> : page === 'Catalogue' ? <Catalog action={action}/> : page === 'Installed models' ? <Models action={action}/> : page === 'Downloads' ? <Downloads action={action}/> : page === 'Runtime' ? <Runtime action={action}/> : page === 'Repositories' ? <Repositories action={action}/> : page === 'Users and roles' ? <Users action={action} me={user}/> : page === 'Audit' ? <Audit/> : page === 'System' ? <System action={action}/> : <Logs/>}</section><footer>AIOS {user?.version||''} · Processing on your own machine · <a href="/api/aios/docs">API reference</a></footer></main></div>;
}
function Login({initialized, done}: {initialized: boolean, done: () => Promise<void>}) {
  const [error,setError] = useState(''), [busy,setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) { e.preventDefault(); setBusy(true); setError(''); const data = Object.fromEntries(new FormData(e.currentTarget)); try { if (!initialized) await api('auth/bootstrap','POST',data); const result = await api('auth/login','POST',{username:data.username,password:data.password}); setCSRF(result.csrf); await done(); } catch(e) {setError((e as Error).message);} finally {setBusy(false);} }
  return <main className="login"><div className="login-art"><span className="eyebrow">YOUR HARDWARE. YOUR INTELLIGENCE.</span><img className="login-logo" src="/admin/aios-lockup.png" alt="AIOS — Intelligence Operating System"/><p>The control centre<br/>of your local AI.</p><div className="orbits"><i/><i/><i/></div></div><form onSubmit={submit}><span className="eyebrow">ADMIN PORTAL</span><h2>{initialized ? 'Sign in to the appliance' : 'Initialise AIOS'}</h2><p>{initialized ? 'Use your AIOS account, the same one as the chat.' : 'The one-time secret is shown on the local console. Choose your own password now.'}</p>{!initialized && <Field label="Bootstrap secret"><input name="secret" type="password" required autoComplete="off"/></Field>}<Field label="Username"><input name="username" required autoComplete="username" pattern="[a-zA-Z0-9_.@+\-]+"/></Field><Field label="Password"><input name="password" type="password" required minLength={initialized ? 1 : 12} autoComplete={initialized ? 'current-password' : 'new-password'}/></Field>{error && <p role="alert" className="error">{error}</p>}<button className="primary" disabled={busy}>{busy ? 'Connecting…' : initialized ? 'Sign in →' : 'Create administrator →'}</button><small>One account for both AIOS and Open WebUI.</small></form></main>;
}
function Password({done,action}: {done:()=>void,action:Action}) { return <div className="login"><form onSubmit={e=>{e.preventDefault();void action('auth/password','POST',Object.fromEntries(new FormData(e.currentTarget))).then(done).catch(()=>{});}}><h2>Change the initial password</h2><Field label="Current password"><input type="password" name="current" required/></Field><Field label="New password"><input type="password" name="password" minLength={12} required/></Field><button className="primary">Save and sign in again</button></form></div>; }
