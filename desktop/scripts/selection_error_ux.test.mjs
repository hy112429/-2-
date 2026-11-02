import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/renderer/src/utils/selection_error_ux.ts', import.meta.url), 'utf8')
const settingsView = await readFile(new URL('../src/renderer/src/components/SelectionSettingsView.vue', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(moduleValue.exports, () => undefined, moduleValue)
const { toUserSelectionError, toUserSelectionMessage } = moduleValue.exports

test('11001 映射为限流说明并保留倒计时，不泄露 IPC 和 Trace', () => {
  const result = toUserSelectionError(
    "Error invoking remote method 'browser:runBaiyingSelection': material_list 11001，冷却剩余 42 秒。 Trace ID: abc"
  )
  assert.equal(result.classification, 'BAIYING_RATE_LIMIT')
  assert.match(result.userMessage, /约 42 秒后可继续选品/)
  assert.doesNotMatch(result.userMessage, /Error invoking|browser:|Trace|material_list/)
  assert.match(result.diagnostic, /Trace ID: abc/)
})

test('11001 冷却结束时提供可继续选品的业务提示', () => {
  const result = toUserSelectionError({ code: 11001, cooldownRemaining: 0, message: '业务错误' })
  assert.match(result.userMessage, /冷却已结束，可以继续选品/)
  assert.equal(toUserSelectionError({ classification: 'BAIYING_RATE_LIMIT', cooldownRemaining: 23 }).cooldownSeconds, 23)
  const diagnosticOnly = toUserSelectionError({ message: '选品未完成', diagnostic: 'BAIYING_RATE_LIMIT，冷却剩余 17 秒' })
  assert.equal(diagnosticOnly.classification, 'BAIYING_RATE_LIMIT')
  assert.match(diagnosticOnly.userMessage, /约 17 秒后可继续选品/)
})

test('选品太多、请求过多和常见频控文案统一识别为百应限流', () => {
  for (const message of [
    '百应提示：选品太多，请稍后再试',
    '接口请求过多，请等待冷却后继续',
    '请求频繁，请稍后重试',
    'Too many requests, please wait 30 seconds'
  ]) {
    const result = toUserSelectionError(message)
    assert.equal(result.classification, 'BAIYING_RATE_LIMIT', message)
    assert.match(result.userMessage, /百应暂时限流/)
    assert.doesNotMatch(result.userMessage, /Error invoking|browser:|Trace|IPC/i)
  }
})

test('验证码、登录和浏览器断连映射为换号可读文案', () => {
  const cases = [
    ['百应要求手动完成滑块验证', 'ACCOUNT_SWITCH_CAPTCHA', /验证码|安全验证/],
    ['当前账号创作者中心尚未登录', 'ACCOUNT_SWITCH_LOGIN', /登录状态/],
    ['托管 Chrome 已关闭，CDP 连接失效', 'ACCOUNT_SWITCH_DISCONNECT', /浏览器连接已断开/]
  ]
  for (const [message, classification, phrase] of cases) {
    const result = toUserSelectionError(`Error invoking remote method 'browser:runBaiyingSelection': ${message}. Trace ID: hidden`)
    assert.equal(result.classification, classification, message)
    assert.match(result.userMessage, phrase)
    assert.match(result.userMessage, /停止该账号并继续其他账号/)
    assert.doesNotMatch(result.userMessage, /Error invoking|browser:|Trace|IPC|CDP/i)
  }
})

test('登录和连接状态分类可与限流分类区分', () => {
  assert.equal(toUserSelectionError('LOGIN_OR_PAGE_STATE_UNCONFIRMED').classification, 'LOGIN_OR_PAGE_STATE_UNCONFIRMED')
  assert.equal(toUserSelectionError('Too many requests 11001, wait for cooldown').classification, 'BAIYING_RATE_LIMIT')
})

test('筛选和页面状态按 classification 映射为业务可读文案', () => {
  assert.equal(
    toUserSelectionMessage({ classification: 'CATEGORY_FILTER_APPLY_FAILED', message: "Error invoking remote method 'browser:runBaiyingSelection'" }),
    '百应筛选条件经过自动重试仍未确认，请检查当前筛选项后启动新批次。'
  )
  assert.equal(
    toUserSelectionMessage({ classification: 'LOGIN_OR_PAGE_STATE_UNCONFIRMED', message: 'Locator.click timeout' }),
    '该账号登录状态或页面无法确认，本轮已停止该账号并继续其他账号；请检查该账号专属浏览器后再恢复。'
  )
})

test('普通中文业务进度原样显示，未知技术异常隐藏在诊断详情', () => {
  assert.equal(toUserSelectionMessage('正在读取当前账号的候选商品'), '正在读取当前账号的候选商品')
  const result = toUserSelectionError(new Error('TimeoutError: internal failure'))
  assert.equal(result.userMessage, '选品未完成，请查看诊断详情后重试。')
  assert.match(result.diagnostic, /TimeoutError: internal failure/)
})

test('选品通知、进度和结果绑定映射文案，冷却后有恢复入口', () => {
  assert.match(settingsView, /notifyGlobal\(presentation\.userMessage/)
  assert.match(settingsView, /selectionProgressPresentation\.userMessage/)
  assert.match(settingsView, /selectionErrorPresentation\.userMessage/)
  assert.match(settingsView, /selectionNoticePresentation\.userMessage/)
  assert.match(settingsView, /冷却中（\$\{selectionCooldownRemaining\} 秒）/)
  assert.match(settingsView, /'继续选品'/)
  assert.match(settingsView, /saveCandidateFetchRateLimitCheckpoint/)
  assert.match(settingsView, /evaluateSelectionCooldownResume/)
  assert.match(settingsView, /scheduleCooldownAutoResume/)
  assert.match(settingsView, /requestedAutoResumeBatchId/)
  assert.match(settingsView, /requestedAutoResumeAccountId/)
  assert.match(settingsView, /const runSelectionBatchOnce = createSingleFlight\(runSelectionBatch\)/)
  assert.match(settingsView, /冷却已结束，正在从保存的检查点自动继续/)
  const unmountHook = settingsView.slice(settingsView.indexOf('onUnmounted(() =>'))
  assert.match(unmountHook, /clearAutoResumeTimer\(\)/)
})

test('账号级验证失败写入 failed 后交给串行调度器，候选预览失败可排除单账号', () => {
  const accountPreflight = settingsView.slice(
    settingsView.indexOf('const switchFailure = accountSwitchFailures.get(account.id)'),
    settingsView.indexOf('const contextError = contextErrors.get(account.id)')
  )
  assert.match(accountPreflight, /state: 'failed', stage: 'account_preflight'/)
  assert.match(accountPreflight, /throw new Error\(switchFailure.reason\)/)

  const candidateFailover = settingsView.slice(
    settingsView.indexOf('async function prepareBatchCandidatePlans('),
    settingsView.indexOf('async function prepareAccountRunContext(')
  )
  assert.match(candidateFailover, /ACCOUNT_SWITCH_CAPTCHA/)
  assert.match(candidateFailover, /ACCOUNT_SWITCH_LOGIN/)
  assert.match(candidateFailover, /ACCOUNT_SWITCH_DISCONNECT/)
  assert.match(candidateFailover, /accountSwitchFailures\.set\(account\.id/)
  assert.match(candidateFailover, /continue/)

  const accountRun = settingsView.slice(
    settingsView.indexOf('const switchItem = returned.find'),
    settingsView.indexOf("await persistStage('quota_cart'", settingsView.indexOf('const switchItem = returned.find'))
  )
  assert.match(accountRun, /manualAction === 'complete_baiying_verification'/)
  assert.match(accountRun, /state: 'failed', stage: failedStage/)
  assert.match(accountRun, /throw new Error\(reason\)/)

  const pendingCaptcha = settingsView.slice(
    settingsView.indexOf("if (item.manualAction === 'complete_baiying_verification' && !resumeAfterCart)"),
    settingsView.indexOf('if (resumeAfterCart && item.manualAction', settingsView.indexOf("if (item.manualAction === 'complete_baiying_verification' && !resumeAfterCart)"))
  )
  assert.match(pendingCaptcha, /state: 'failed', stage: 'video_pending'/)
  assert.match(pendingCaptcha, /throw new Error\(reason\)/)
})
