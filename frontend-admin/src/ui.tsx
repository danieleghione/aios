// Building blocks shared by every page of the portal.
import {useCallback, useEffect, useState, type ReactNode} from 'react';
import { api } from './api';

export type Item = Record<string, any>;
export type Action = (path: string, method?: string, body?: unknown) => Promise<any>;
export function Badge({children}: {children: ReactNode}) { return <span className={'badge ' + (['FAILED','ERROR','INCOMPATIBLE','CRITICAL'].includes(String(children)) ? 'danger' : ['WARNING','LIMITED'].includes(String(children)) ? 'warn' : ['RUNNING','PUBLISHED','ONLINE','HEALTHY','OPTIMAL','INSTALLED','COMPLETED','DEFAULT'].includes(String(children)) ? 'good' : '')}>{children}</span>; }
export function Json({value}: {value: unknown}) { return <pre className="json">{JSON.stringify(value, null, 2)}</pre>; }
export function Field({label, children}: {label: string, children: ReactNode}) { return <label className="field"><span>{label}</span>{children}</label>; }
export function Empty({text = 'Nothing to show yet.', next}: {text?: string, next?: [string, string]}) { return <div className="empty"><span>◌</span><p>{text}</p>{next && <button className="primary" onClick={() => goTo(next[1])}>{next[0]}</button>}</div>; }
// Lets an empty page point to the step that fills it; the shell installs it.
let goTo: (page: string) => void = () => {};
export const setNavigator = (fn: (page: string) => void) => { goTo = fn; };
export const goToPage = (page: string) => goTo(page);
export function Modal({title, children, close}: {title: string, children: ReactNode, close: () => void}) { return <div className="overlay" onClick={close}><section role="dialog" aria-modal="true" aria-label={title} className="modal" onClick={e => e.stopPropagation()}><header><h2>{title}</h2><button onClick={close} aria-label="Close">×</button></header>{children}</section></div>; }
export function useData(path: string, interval = 0) {
  const [data, setData] = useState<Item | null>(null), [error, setError] = useState('');
  const refresh = useCallback(() => api(path).then(setData).catch(e => setError(e.message)), [path]);
  useEffect(() => { setData(null); setError(''); void refresh(); if (interval) { const timer = setInterval(refresh, interval); return () => clearInterval(timer); } }, [refresh, interval]);
  return {data, error, refresh};
}
export function Collection({items, render, empty}: {items: Item[] | undefined, render: (item: Item) => ReactNode, empty?: ReactNode}) { return items?.length ? <div className="collection">{items.map((item, i) => <div key={item.id || i}>{render(item)}</div>)}</div> : (empty || <Empty/>); }

// A confirmation in the portal's own style, instead of the browser's dialog.
let askImpl: (text: string) => Promise<boolean> = async text => window.confirm(text);
export const setAsker = (fn: (text: string) => Promise<boolean>) => { askImpl = fn; };
export const ask = (text: string) => askImpl(text);
// Messages in the portal's notice bar rather than a browser dialog.
let notifyImpl: (text: string) => void = text => window.alert(text);
export const setNotifier = (fn: (text: string) => void) => { notifyImpl = fn; };
export const notify = (text: string) => notifyImpl(text);
export const JOB_NAMES: Record<string, string> = {system: 'System configuration', network: 'Network change', 'network-confirm': 'Network confirmation', 'network-rollback': 'Network rollback', tls: 'TLS certificate', backup: 'Backup', restore: 'Restore', 'component-remove': 'Component removal', 'chat-voice': 'Chat read-aloud voice', update: 'Component update', 'os-update': 'Operating system updates', reboot: 'Restart', shutdown: 'Power off'};
export const placement=(a:Item|null|undefined)=>{if(!a)return 'Device: not started yet'; if(!a.devices?.length)return 'Running on the CPU'; const names=a.devices.map((d:Item)=>d.description).join(', '); return a.offload?`Running on ${names} · ${a.offload.layers}/${a.offload.total} layers on the GPU`:`Running on ${names}`;};
export const humanAction=(a:string)=>{const t=String(a||'').replace(/_/g,' ');return t.charAt(0).toUpperCase()+t.slice(1);};
