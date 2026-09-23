// Real browser + real API + separate worker, isolated credentials and database.
// npm run test:e2e [-- --real-data]; Python defaults to backend/.venv/bin/python.
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomBytes } from 'node:crypto'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(path.join(root, 'frontend/package.json'))
const { chromium } = require('playwright')
const python = process.env.PYTHON || path.join(root, 'backend/.venv/bin/python')
const real = process.argv.includes('--real-data')
const temporary = await mkdtemp(path.join(tmpdir(), 'umytpa-browser-'))
const output = process.env.QA_OUTPUT_DIR || path.join(temporary, 'evidence')
await mkdir(output, { recursive: true })
const children = []
const failures = []
let browser

async function freePort() {
  const server = net.createServer()
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}
const apiPort = await freePort()
const uiPort = await freePort()
const origin = `http://127.0.0.1:${uiPort}`
const database = path.join(temporary, 'service.sqlite3')
const environment = {
  ...process.env, APP_ENV: 'development', APP_ORIGIN: origin,
  APP_DB_PATH: database, ORDER_DB_PATH: database, CORS_ORIGINS: '',
  DATA_SOURCE: real ? 'excel' : 'synthetic', DATA_DIR: root,
  OPENAI_API_KEY: '', API_PROXY_TARGET: `http://127.0.0.1:${apiPort}`,
}
const password = randomBytes(24).toString('base64url')
const user = 'browser-manager'
const initial = spawnSync(python, ['-c', `
import json, sys
from app.storage import migrate
from app.repositories import create_user
migrate()
credentials=json.load(sys.stdin)
create_user('browser-admin', credentials['password'], 'admin')
create_user(credentials['username'], credentials['password'], 'manager')
`], { cwd: path.join(root, 'backend'), env: environment,
  input: JSON.stringify({ username: user, password }), encoding: 'utf8' })
assert.equal(initial.status, 0, initial.stderr)

function start(command, args, cwd) {
  const child = spawn(command, args, { cwd, env: environment, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] })
  child.on('error', (error) => failures.push(error.message))
  let logs = ''
  child.stdout.on('data', (chunk) => { logs = (logs + chunk).slice(-10000) })
  child.stderr.on('data', (chunk) => { logs = (logs + chunk).slice(-10000) })
  children.push({ child, logs: () => logs })
  return child
}
async function ready(url) {
  for (let i = 0; i < 120; i++) {
    if (failures.length) throw new Error(failures.join('\n'))
    if (children.some(({ child }) => child.exitCode !== null)) throw new Error(children.map((c) => c.logs()).join('\n'))
    try { if ((await fetch(url)).ok) return } catch { /* server starting */ }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error(`Server did not become ready: ${url}`)
}
async function saved(page) {
  await page.waitForFunction(() => document.querySelector('.save-indicator')?.textContent.includes('Все изменения сохранены'), undefined, { timeout: 30000 })
}

try {
  start(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(apiPort), '--no-access-log'], path.join(root, 'backend'))
  start(python, ['-m', 'app.worker'], path.join(root, 'backend'))
  start(process.execPath, ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', String(uiPort), '--strictPort'], path.join(root, 'frontend'))
  await ready(`http://127.0.0.1:${apiPort}/api/health`)
  await ready(origin)
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true })
  const page = await context.newPage()
  page.on('pageerror', (error) => failures.push(error.message))
  await page.goto(origin)
  await page.getByLabel('Имя пользователя').fill(user)
  await page.getByLabel('Пароль', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Войти', exact: true }).click()
  await page.getByRole('button', { name: 'Новый расчёт', exact: true }).click()
  const calculate = page.getByRole('button', { name: 'Рассчитать заказ', exact: true })
  await page.waitForFunction(() => document.querySelector('.calculate-button')?.disabled === false, undefined, { timeout: 120000 })
  await page.screenshot({ path: path.join(output, 'calculate-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 320, height: 900 })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Calculation form must fit 320px')
  await page.screenshot({ path: path.join(output, 'calculate-mobile.png'), fullPage: true })
  await page.setViewportSize({ width: 1440, height: 1000 })
  const started = Date.now()
  await calculate.click()
  await page.getByRole('button', { name: 'Открыть заказ', exact: true }).waitFor({ timeout: 180000 })
  await page.getByRole('button', { name: 'Открыть заказ', exact: true }).click()
  const quantity = page.locator('.group .qty').first()
  await quantity.waitFor()
  const firstLabel = await quantity.getAttribute('aria-label')
  const initialQuantity = Number(await quantity.inputValue())
  const pack = Number(await quantity.getAttribute('step')) || 1
  const edited = Math.round((initialQuantity + pack) * 1e8) / 1e8
  await quantity.fill(String(edited))
  await saved(page)
  const orderUrl = page.url()
  await page.reload()
  await page.getByRole('spinbutton', { name: firstLabel, exact: true }).waitFor()
  assert.equal(Number(await page.getByRole('spinbutton', { name: firstLabel, exact: true }).inputValue()), edited, 'Edits must survive reload')
  const row = page.locator('.group tbody tr').first()
  await row.getByRole('button', { name: /^Обоснование/ }).click()
  await page.getByRole('dialog').waitFor()
  await page.getByRole('button', { name: 'Понятно', exact: true }).click()

  const widths = [320, 360, 390, 768, 1024, 1440]
  for (const width of widths) {
    await page.setViewportSize({ width, height: 1000 })
    const dimensions = await page.evaluate(() => ({ viewport: innerWidth, page: document.documentElement.scrollWidth }))
    assert.ok(dimensions.page <= width + 1, `Horizontal page overflow at ${width}: ${JSON.stringify(dimensions)}`)
    if (width < 768) {
      assert.equal(await row.evaluate((element) => getComputedStyle(element).display), 'grid')
      await page.waitForFunction(() => [...document.querySelectorAll('.group tbody')].every((body) => body.rows.length <= 5))
    }
    if (width === 390) {
      await page.screenshot({ path: path.join(output, 'order-mobile-viewport.png') })
      await row.screenshot({ path: path.join(output, 'order-mobile-card.png') })
    }
    if ([390, 1440].includes(width)) await page.screenshot({ path: path.join(output, `order-${width}.png`), fullPage: true })
  }
  const restoredQuantity = page.getByRole('spinbutton', { name: firstLabel, exact: true })
  await restoredQuantity.fill('-1')
  await page.getByText('Количество должно быть числом не меньше 0.', { exact: true }).first().waitFor()
  assert.equal(await page.getByRole('button', { name: 'Черновик Excel', exact: true }).isDisabled(), true)
  await restoredQuantity.fill(String(edited))
  await saved(page)
  await row.getByRole('checkbox').check()
  await saved(page)
  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('button', { name: /^Утверждённые · 1$/ }).click()
  const download = await downloadEvent
  const spreadsheet = path.join(output, 'approved_order.xlsx')
  await download.saveAs(spreadsheet)
  const verify = spawnSync(python, ['-c', `
import json,sys
from openpyxl import load_workbook
book=load_workbook(sys.argv[1],data_only=True)
sheet=book['Заказ поставщикам']
rows=list(sheet.values)
header=next(row for row in rows if 'К заказу, ед' in row)
start=rows.index(header)+1
items=[dict(zip(header,row)) for row in rows[start:] if row and row[0] is not None]
assert len(items)==1, len(items)
assert float(items[0]['К заказу, ед'])==float(sys.argv[2]),items[0]
print(json.dumps({'quantity':items[0]['К заказу, ед'],'sheets':book.sheetnames},ensure_ascii=False))
` , spreadsheet, String(edited)], { env: environment, encoding: 'utf8' })
  assert.equal(verify.status, 0, verify.stderr)

  // Two actual browser tabs must expose a version conflict, never silently overwrite.
  const second = await context.newPage()
  await second.goto(orderUrl)
  const secondQuantity = second.getByRole('spinbutton', { name: firstLabel, exact: true })
  await secondQuantity.waitFor()
  await restoredQuantity.fill(String(edited + pack))
  await saved(page)
  await secondQuantity.fill(String(edited + 2 * pack))
  await second.getByRole('heading', { name: 'Заказ изменён в другой сессии', exact: true }).waitFor()
  await second.getByRole('button', { name: 'Принять серверную версию', exact: true }).click()
  await saved(second)
  assert.equal(Number(await secondQuantity.inputValue()), edited + pack)
  assert.equal(failures.length, 0, failures.join('\n'))
  const report = { status: 'passed', source: real ? 'excel' : 'synthetic', browser: 'Chromium',
    widths, mobile_page_size: 5, calculation_form_width: 320, persisted_edit: { before: initialQuantity, after: edited },
    export: JSON.parse(verify.stdout), conflict: 'detected and explicitly resolved',
    elapsed_ms: Date.now() - started, production_approval: false }
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify({ ...report, evidence: output }, null, 2))
} catch (error) {
  await writeFile(path.join(output, 'failure.txt'), String(error.stack || error))
  console.error(`Browser smoke failed; evidence: ${output}`)
  throw error
} finally {
  await browser?.close()
  for (const { child } of children) {
    try { process.kill(process.platform === 'win32' ? child.pid : -child.pid, 'SIGTERM') } catch { /* exited */ }
  }
  await Promise.all(children.map(({ child }) => new Promise((resolve) => {
    if (child.exitCode !== null) return resolve()
    const timer = setTimeout(() => { try { process.kill(process.platform === 'win32' ? child.pid : -child.pid, 'SIGKILL') } catch { /* exited */ }; resolve() }, 5000)
    child.once('exit', () => { clearTimeout(timer); resolve() })
  })))
  for (const suffix of ['', '-wal', '-shm']) await rm(database + suffix, { force: true })
}
