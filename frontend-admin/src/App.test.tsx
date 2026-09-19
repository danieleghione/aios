import { describe,it,expect } from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {Badge} from './App';
import {bytes} from './api';
describe('Admin UI presentation',()=>{
 it('formats hardware and download capacities',()=>{expect(bytes(2*1024**3)).toBe('2.0 GiB');expect(bytes(NaN)).toBe('—');});
 it('distinguishes lifecycle failures and success',()=>{expect(renderToStaticMarkup(<Badge>FAILED</Badge>)).toContain('danger');expect(renderToStaticMarkup(<Badge>PUBLISHED</Badge>)).toContain('good');});
 it('escapes untrusted repository display text',()=>{expect(renderToStaticMarkup(<Badge>{'<script>alert(1)</script>'}</Badge>)).not.toContain('<script>');});
});
