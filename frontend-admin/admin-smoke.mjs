import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
const auth=JSON.parse(fs.readFileSync(process.env.AIOS_BROWSER_CREDENTIALS,'utf8'));
const output=process.env.AIOS_BROWSER_OUTPUT;
fs.mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true});
const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});
const page=await context.newPage();
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
try {
 await page.goto(auth.base+'/admin/');
 await page.getByLabel('Nome utente',{exact:true}).fill(auth.username);
 await page.getByLabel('Password',{exact:true}).fill(auth.password);
 await page.getByRole('button',{name:'Accedi →'}).click();
 await page.getByText('La tua AI, sotto controllo.').waitFor();
 await page.screenshot({path:path.join(output,'admin-dashboard.png'),fullPage:true});
 for (const name of ['Hardware','Catalogo','Nuove release','Modelli installati','Download','Runtime','Repository','Utenti e ruoli','Audit','Sistema','Log']) {
  await page.getByRole('navigation').getByRole('button',{name,exact:true}).click();
  await page.waitForTimeout(500);
  if ((await page.locator('body').innerText()).includes('Internal operation failed')) throw new Error('Page failed: '+name);
 }
 await page.getByRole('navigation').getByRole('button',{name:'Modelli installati',exact:true}).click();
 await page.getByRole('button',{name:'Configura runtime'}).first().waitFor();
 await page.screenshot({path:path.join(output,'admin-models.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.getByRole('navigation').getByRole('button',{name:'Dashboard',exact:true}).click();
 await page.getByText('La tua AI, sotto controllo.').waitFor();
 if (await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+2)) throw new Error('Mobile layout overflows viewport');
 await page.screenshot({path:path.join(output,'admin-mobile.png'),fullPage:true});
 if (errors.length) throw new Error(errors.join('\n'));
 fs.writeFileSync(path.join(output,'browser-report.json'),JSON.stringify({status:'PASSED',pages:12,console_errors:errors,viewports:['1440x1000','390x844']},null,2));
} finally {await browser.close();}
