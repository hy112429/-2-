import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = await readFile(new URL('../src/main/external_browser.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const moduleValue = { exports: {} }
const localRequire = (specifier) => {
  if (specifier === './engine') return { getEngineDir: () => '', getPythonPath: () => '' }
  if (specifier === './probe') return { captureExternalBaiyingResponse: () => undefined }
  return require(specifier)
}
new Function('exports', 'require', 'module', compiled)(moduleValue.exports, localRequire, moduleValue)
const { prepareManagedBrowserRetry, bridgeProcessTreeTermination } = moduleValue.exports

test('断线后只自动恢复带完整商品 ID 检查点的选品执行', () => {
  assert.deepEqual(
    prepareManagedBrowserRetry('inspect-selection', { includeCategoryTree: true }),
    { includeCategoryTree: true }
  )
  assert.deepEqual(
    prepareManagedBrowserRetry('open', { url: 'https://buyin.jinritemai.com' }),
    { url: 'https://buyin.jinritemai.com' }
  )
  assert.deepEqual(
    prepareManagedBrowserRetry('run-selection', { previewOnly: true }),
    { previewOnly: true }
  )
  const resumable = prepareManagedBrowserRetry('run-selection', {
    previewOnly: false,
    candidateSnapshot: [{ cardIndex: 4, productId: '商品ID-1' }],
    selectedIndexes: [4]
  })
  assert.equal(resumable.refreshCandidatesById, true)
  assert.equal(prepareManagedBrowserRetry('run-selection', { selectedIndexes: [4] }), null)
  assert.equal(prepareManagedBrowserRetry('run-selection', {
    candidateSnapshot: [{ cardIndex: 4, title: '缺少商品 ID' }],
    selectedIndexes: [4]
  }), null)
  assert.equal(prepareManagedBrowserRetry('run-selection', {
    candidateSnapshot: [{ cardIndex: 4, productId: '商品ID-1' }],
    selectedIndexes: [5]
  }), null)
})

test('Windows 客户端退出时按进程树清理桥接 Python，但不结束 Chrome', () => {
  assert.deepEqual(bridgeProcessTreeTermination('win32', 1234), {
    command: 'taskkill',
    args: ['/pid', '1234', '/T', '/F']
  })
  assert.equal(bridgeProcessTreeTermination('win32', undefined), null)
  assert.equal(bridgeProcessTreeTermination('linux', 1234), null)
})
