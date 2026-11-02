import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/main/publish_scheduler.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(
  moduleValue.exports,
  () => { throw new Error('publish_scheduler.ts should not load dependencies') },
  moduleValue
)

const {
  DEFAULT_PUBLISH_JITTER_MAX_SECONDS,
  DEFAULT_PUBLISH_JITTER_MIN_SECONDS,
  PUBLISH_SCHEDULER_BASE_INTERVAL_MS,
  getNextPublishSchedulerDelayMs,
  getPublishSchedulerJitterMs,
  readPublishJitterSeconds
} = moduleValue.exports

test('调度轮询在基础间隔上增加闭区间随机错峰', () => {
  assert.equal(PUBLISH_SCHEDULER_BASE_INTERVAL_MS, 10_000)
  assert.equal(getPublishSchedulerJitterMs(5, 15, () => 0), 5_000)
  assert.equal(getPublishSchedulerJitterMs(5, 15, () => 0.999999), 15_000)
  assert.equal(getNextPublishSchedulerDelayMs(5, 15, () => 0), 15_000)
  assert.equal(getNextPublishSchedulerDelayMs(5, 15, () => 0.999999), 25_000)
  assert.equal(getNextPublishSchedulerDelayMs(9, 9, () => 0.5), 19_000)
})

test('错峰设置读取无效时回到默认范围，非法区间拒绝计算', () => {
  assert.equal(DEFAULT_PUBLISH_JITTER_MIN_SECONDS, 5)
  assert.equal(DEFAULT_PUBLISH_JITTER_MAX_SECONDS, 15)
  assert.equal(readPublishJitterSeconds('12', 5), 12)
  assert.equal(readPublishJitterSeconds(-1, 5), 5)
  assert.equal(readPublishJitterSeconds(3601, 5), 5)
  assert.throws(() => getPublishSchedulerJitterMs(16, 15), /配置无效/)
})
