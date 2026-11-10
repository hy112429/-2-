const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const ts = require('typescript')

const sourcePath = path.resolve(__dirname, '../src/main/cart_download_slots.ts')
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const isolated = new Module(sourcePath, module)
isolated.filename = sourcePath
isolated.paths = module.paths
isolated._compile(compiled, sourcePath)
const { withCartDownloadSlot } = isolated.exports

async function waitFor(condition) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (condition()) return
    await new Promise((resolve) => setTimeout(resolve, 1))
  }
  throw new Error('下载工作槽未按预期推进')
}

async function main() {
  let active = 0
  let peak = 0
  const started = []
  const release = new Map()
  const tasks = Array.from({ length: 12 }, (_, index) => withCartDownloadSlot(async () => {
    started.push(index)
    peak = Math.max(peak, ++active)
    await new Promise((resolve) => release.set(index, resolve))
    active--
    return index
  }))
  await waitFor(() => started.length === 4)
  assert.equal(active, 4)
  for (let group = 0; group < 3; group++) {
    assert.deepEqual(started, Array.from({ length: Math.min((group + 1) * 4, 12) }, (_, index) => index))
    for (let index = group * 4; index < group * 4 + 4; index++) release.get(index)()
    if (group < 2) await waitFor(() => started.length === (group + 2) * 4)
  }
  assert.deepEqual(await Promise.all(tasks), Array.from({ length: 12 }, (_, index) => index))
  assert.equal(peak, 4)
  await assert.rejects(withCartDownloadSlot(async () => { throw new Error('模拟下载失败') }), /模拟下载失败/)
  assert.equal(await withCartDownloadSlot(async () => '继续下载'), '继续下载')
  console.log('Cart download global slots: PASS (12 jobs, 4 concurrent, failure releases slot)')
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
