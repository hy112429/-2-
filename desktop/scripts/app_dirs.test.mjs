import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const app = { isPackaged: false, getPath: () => 'D:\\software\\codex\\test-user-data' }
const electron = { app }
const source = await readFile(new URL('../src/main/app-dirs.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const moduleValue = { exports: {} }
const mockedRequire = (name) => name === 'electron' ? electron : require(name)
new Function('exports', 'require', 'module', compiled)(moduleValue.exports, mockedRequire, moduleValue)
const { getIsolatedTestRoot } = moduleValue.exports

test('开发态隔离测试根目录仅允许 D 盘建议安装根目录下的独立子目录', () => {
  const previous = process.env.TOUGUANG_CLIENT_TEST_ROOT
  try {
    process.env.TOUGUANG_CLIENT_TEST_ROOT = 'D:\\software\\codex\\selection-ui-test'
    assert.equal(getIsolatedTestRoot(), resolve('D:\\software\\codex\\selection-ui-test'))

    process.env.TOUGUANG_CLIENT_TEST_ROOT = 'D:\\software\\codex'
    assert.throws(() => getIsolatedTestRoot(), /独立子目录/)

    process.env.TOUGUANG_CLIENT_TEST_ROOT = 'D:\\software\\outside-test'
    assert.throws(() => getIsolatedTestRoot(), /独立子目录/)

    process.env.TOUGUANG_CLIENT_TEST_ROOT = 'D:\\software\\codex\\..\\outside-test'
    assert.throws(() => getIsolatedTestRoot(), /独立子目录/)
  } finally {
    if (previous === undefined) delete process.env.TOUGUANG_CLIENT_TEST_ROOT
    else process.env.TOUGUANG_CLIENT_TEST_ROOT = previous
  }
})

test('打包版忽略开发态隔离测试环境变量', () => {
  const previous = process.env.TOUGUANG_CLIENT_TEST_ROOT
  try {
    app.isPackaged = true
    process.env.TOUGUANG_CLIENT_TEST_ROOT = 'D:\\software\\codex\\selection-ui-test'
    assert.equal(getIsolatedTestRoot(), undefined)
  } finally {
    app.isPackaged = false
    if (previous === undefined) delete process.env.TOUGUANG_CLIENT_TEST_ROOT
    else process.env.TOUGUANG_CLIENT_TEST_ROOT = previous
  }
})
