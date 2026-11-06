import assert from 'node:assert/strict'
import { readFileSync, mkdtempSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'
import { test } from 'node:test'
import * as nodeFs from 'node:fs'
import * as nodePath from 'node:path'
import * as nodeHttp from 'node:http'
import * as nodeCrypto from 'node:crypto'
import * as nodeChild from 'node:child_process'
import ts from 'typescript'

const source = readFileSync(new URL('../src/main/qianchuan_api.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const handlers = new Map()
const root = mkdtempSync('D:\\software\\codex\\qianchuan-api-test-')
let openedUrl = ''
let openedProfilePath = ''
let apiQuery = ''
let refreshCount = 0
let accountMode = 'shop'
const fakeFetch = async (url, options = {}) => {
  const value = String(url)
  if (value.endsWith('/oauth2/access_token/')) {
    const body = JSON.parse(options.body)
    assert.equal(body.auth_code, 'test-code')
    return new Response(JSON.stringify({ code: 0, data: { access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 0, refresh_token_expires_in: 3600, advertiser_ids: [] } }))
  }
  if (value.endsWith('/oauth2/refresh_token/')) {
    refreshCount++
    return new Response(JSON.stringify({ code: 0, data: { access_token: 'access-2', refresh_token: 'refresh-2', expires_in: 3600, refresh_token_expires_in: 7200 } }))
  }
  if (new URL(value).pathname === '/open_api/oauth2/advertiser/get/') {
    assert.equal(new URL(value).searchParams.get('access_token'), 'access-2')
    if (accountMode === 'ordinary' || accountMode === 'managed') return new Response('{"code":0,"data":{"list":[{"account_id":1878463096327180,"account_type":"CUSTOMER_ADMIN","is_valid":true},{"account_id":1878900382409740,"account_type":"ADVERTISER","is_valid":true}]}}')
    return new Response('{"code":0,"data":{"list":[{"account_id":7693872361947709724,"account_name":"测试店铺","account_type":"PLATFORM_ROLE_SHOP_ACCOUNT","is_valid":true}]}}')
  }
  if (new URL(value).pathname === '/open_api/v1.0/qianchuan/shop/advertiser/list/') {
    assert.equal(new URL(value).searchParams.get('shop_id'), '7693872361947709724')
    assert.equal(options.headers['Access-Token'], 'access-2')
    if (accountMode === 'shop_no_permission') return new Response('{"code":40002,"message":"advertiser does no grant you /qianchuan/shop/advertiser/list/ permission"}')
    return new Response('{"code":0,"data":{"adv_id_list":[{"adv_id":16761573861161,"adv_name":"测试广告主"}],"page_info":{"total_page":1}}}')
  }
  if (new URL(value).pathname === '/open_api/2/customer_center/advertiser/list/') {
    assert.equal(new URL(value).searchParams.get('cc_account_id'), '1878463096327180')
    assert.equal(new URL(value).searchParams.get('account_source'), 'QIANCHUAN')
    if (accountMode === 'managed') return new Response('{"code":0,"data":{"list":[{"advertiser_id":16761573861161,"advertiser_name":"工作台千川广告主"}],"page_info":{"total_page":1}}}')
    return new Response('{"code":0,"data":{"list":[],"page_info":{"total_page":1}}}')
  }
  if (new URL(value).pathname === '/open_api/v1.0/qianchuan/aweme/authorized/get/') {
    assert.equal(new URL(value).searchParams.get('advertiser_id'), '16761573861161')
    return new Response('{"code":0,"data":{"aweme_id_list":[{"aweme_id":7391234567891234567,"aweme_name":"测试抖音号"}],"page_info":{"total_page":1}}}')
  }
  apiQuery = value
  assert.equal(options.headers['Access-Token'], 'access-2')
  return new Response('{"code":0,"data":{"video_list":[{"video_id":"vid-1","aweme_item_id":7391234567891234567,"title":"测试视频","url":"https://cdn.example.com/a.mp4","duration":13}],"page_info":{"cursor":1,"has_more":0}}}')
}
const electron = {
  app: { getPath: () => root },
  ipcMain: { handle: (key, fn) => handlers.set(key, fn) },
  safeStorage: {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(value),
    decryptString: (value) => value.toString('utf8')
  },
  shell: { openExternal: async (url) => { openedUrl = url } }
}
const mod = { exports: {} }
new Function('exports', 'require', 'module', 'fetch', 'AbortSignal', compiled)(mod.exports, (name) => {
  if (name === 'electron') return electron
  if (name === 'node:fs') return nodeFs
  if (name === 'node:path') return nodePath
  if (name === 'node:http') return nodeHttp
  if (name === 'node:crypto') return nodeCrypto
  if (name === 'node:child_process') return nodeChild
  if (name === './app-dirs') return { getDownloadDir: () => root }
  if (name === './engine') return { getEngineDir: () => root }
  if (name === './external_browser') return { openQianchuanOAuthInPersistentProfile: async (url) => {
    openedUrl = url
    openedProfilePath = 'D:\\software\\投广客户端\\browser-profiles\\qianchuan-product-query-auth'
    return { ok: true, url, profilePath: openedProfilePath }
  } }
  throw new Error(`Unexpected import: ${name}`)
}, mod, fakeFetch, AbortSignal)

test('千川 OAuth 校验 state、保留长 ID、刷新 Token 后继续按商品 ID 查询', async () => {
  try {
    assert.equal(mod.exports.parseBigIds('{"id":7391234567891234567,"text":"7391234567891234567"}').id, '7391234567891234567')
    mod.exports.registerQianchuanApiIpc()
    handlers.get('qianchuan:api:configure')(null, { appId: '1880896897289216', secret: 'local-secret' })
    const authUrl = 'https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html?app_id=1880896897289216&is_new_connect=0&is_new_user=0&material_auth=1&rid=test-rid&state=your_custom_params'
    const firstOpen = await handlers.get('qianchuan:api:authorize')(null, authUrl)
    const state = new URL(openedUrl).searchParams.get('state')
    assert.ok(state && state.length > 24)
    assert.notEqual(state, 'your_custom_params')
    assert.equal(firstOpen.profilePath, openedProfilePath)
    assert.equal(new URL(openedUrl).searchParams.get('rid'), 'test-rid')
    const bad = await globalThis.fetch('http://127.0.0.1:8899/callback?auth_code=test-code&state=wrong')
    assert.equal(bad.status, 400)
    assert.equal(handlers.get('qianchuan:api:status')().authorizationRevision, 0)
    const good = await globalThis.fetch(`http://127.0.0.1:8899/callback?auth_code=test-code&state=${state}`)
    assert.equal(good.status, 200)
    assert.equal(handlers.get('qianchuan:api:status')().authorizationRevision, 1)
    assert.deepEqual(handlers.get('qianchuan:api:status')().advertiserIds, [])
    const discovered = await handlers.get('qianchuan:api:accounts')()
    assert.deepEqual(discovered.accounts.map((account) => account.id), ['16761573861161'])
    assert.deepEqual(handlers.get('qianchuan:api:status')().advertiserIds, ['16761573861161'])
    const awemes = await handlers.get('qianchuan:api:awemes')(null, '16761573861161')
    assert.deepEqual(awemes, [{ id: '7391234567891234567', name: '测试抖音号' }])
    await assert.rejects(() => handlers.get('qianchuan:api:videos')(null, { advertiserId: '16761573861161', productId: '3851408796480045486' }), /抖音号 ID/)
    const videos = await handlers.get('qianchuan:api:videos')(null, { advertiserId: '16761573861161', awemeId: awemes[0].id, productId: '3851408796480045486' })
    assert.equal(refreshCount, 1)
    assert.equal(new URL(apiQuery).searchParams.get('filtering'), '{"product_id":3851408796480045486}')
    assert.equal(new URL(apiQuery).searchParams.get('count'), '50')
    assert.equal(new URL(apiQuery).searchParams.get('aweme_id'), '7391234567891234567')
    assert.equal(videos.videos[0].awemeItemId, '7391234567891234567')
    assert.equal(videos.videos[0].url, 'https://cdn.example.com/a.mp4')
    assert.deepEqual(handlers.get('qianchuan:api:status')().advertiserIds, ['16761573861161'])
    accountMode = 'ordinary'
    const wrongAccounts = await handlers.get('qianchuan:api:accounts')()
    assert.equal(wrongAccounts.requiresQianchuanAuthorization, true)
    assert.deepEqual(wrongAccounts.accounts, [])
    assert.deepEqual(handlers.get('qianchuan:api:status')().advertiserIds, [])
    accountMode = 'managed'
    const managedAccounts = await handlers.get('qianchuan:api:accounts')()
    assert.equal(managedAccounts.requiresQianchuanAuthorization, false)
    assert.deepEqual(managedAccounts.accounts.map((account) => account.id), ['16761573861161'])
    accountMode = 'shop_no_permission'
    const blockedShop = await handlers.get('qianchuan:api:accounts')()
    assert.deepEqual(blockedShop.accounts, [])
    assert.equal(blockedShop.requiresQianchuanAuthorization, true)
    assert.match(blockedShop.warnings.join(' '), /直接勾选“千川号”/)
  } finally {
    mod.exports.disposeQianchuanApi()
    const safeRoot = resolve('D:\\software\\codex')
    assert.ok(resolve(root).startsWith(`${safeRoot}\\`))
    rmSync(root, { recursive: true, force: true })
  }
})
