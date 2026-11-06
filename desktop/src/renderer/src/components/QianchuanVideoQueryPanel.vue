<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

type Video = { videoId: string; awemeItemId: string; title: string; url: string; duration: number | null; advertiserId: string; advertiserName: string }
type SearchTarget = { advertiserId: string; awemeId: string; cursor: string; hasMore: boolean }
const MAX_AUTO_PAGES_PER_TARGET = 100
const appId = ref('')
const panelDetails = ref<HTMLDetailsElement | null>(null)
const secret = ref('')
const authUrl = ref('')
const awemeId = ref('')
const awemeAccounts = ref<Array<{ id: string; name: string }>>([])
const productId = ref('')
const configured = ref(false)
const authorized = ref(false)
const requiresQianchuanAuthorization = ref(false)
const authorizedIds = ref<string[]>([])
const authorizedIdentitySummary = ref('')
const accountNames = ref<Record<string, string>>({})
const callback = ref('http://127.0.0.1:8899/callback')
const authBrowserPath = ref('')
const videos = ref<Video[]>([])
const queryTargets = ref<SearchTarget[]>([])
const hasMore = ref(false)
const busy = ref(false)
const authWaiting = ref(false)
const message = ref('')
const downloadDir = ref('')
let offEngine: (() => void) | undefined
let activeDownload: string | undefined
let authPollTimer: ReturnType<typeof setInterval> | undefined
let authPollBusy = false

function stopAuthPoll(): void {
  if (authPollTimer) clearInterval(authPollTimer)
  authPollTimer = undefined
  authWaiting.value = false
}

function watchAuthCallback(previousRevision: number): void {
  stopAuthPoll()
  authWaiting.value = true
  const deadline = Date.now() + 15 * 60_000
  authPollTimer = setInterval(() => {
    if (authPollBusy) return
    authPollBusy = true
    void (async () => {
      try {
        const state = await window.api.qianchuan.apiStatus()
        if (state.authorized && state.authorizationRevision !== previousRevision) {
          stopAuthPoll()
          await refresh()
          message.value = `本机已收到新授权回调。${message.value}`
        } else if (Date.now() >= deadline) {
          stopAuthPoll()
          message.value = '本机 15 分钟内未收到授权回调。请在 Microsoft Edge 授权页确认当前登录用户有权访问目标千川广告主，并在授权范围中勾选该广告主后点“确认授权”；浏览器须跳转到本机回调成功页。'
        }
      } catch (error) {
        stopAuthPoll()
        message.value = `检查本机授权状态失败：${errText(error)}`
      } finally { authPollBusy = false }
    })()
  }, 2000)
}

function errText(error: unknown): string { return error instanceof Error ? error.message : String(error) }
function resetSearch(): void {
  videos.value = []
  queryTargets.value = []
  hasMore.value = false
}
async function refresh(): Promise<void> {
  try {
    const state = await window.api.qianchuan.apiStatus()
    configured.value = state.configured
    authorized.value = state.authorized
    appId.value = state.appId || appId.value
    let ids = state.advertiserIds
    let warnings: string[] = []
    authorizedIdentitySummary.value = ''
    requiresQianchuanAuthorization.value = false
    if (state.authorized) {
      try {
        const discovered = await window.api.qianchuan.apiAccounts()
        const listPermissionBlocked = discovered.accounts.length === 0 && discovered.warnings.some((warning) => /qianchuan\/shop\/advertiser\/list|customer_center\/advertiser\/list|店铺.*广告主列表.*40002|工作台.*千川广告主列表/i.test(warning))
        requiresQianchuanAuthorization.value = discovered.requiresQianchuanAuthorization === true || listPermissionBlocked
        ids = discovered.accounts.map((account) => account.id)
        accountNames.value = Object.fromEntries(discovered.accounts.map((account) => [account.id, account.name || account.source]))
        const typeNames: Record<string, string> = { CUSTOMER_ADMIN: '工作台', CUSTOMER_OPERATOR: '工作台操作员', ADVERTISER: '普通广告主', PLATFORM_ROLE_SHOP_ACCOUNT: '抖店', PLATFORM_ROLE_AWEME: '抖音号管理员', QIANCHUAN: '千川广告主' }
        authorizedIdentitySummary.value = (discovered.identities ?? []).map((item) => `${typeNames[item.type] ?? item.type} ${item.id}`).join('、')
        warnings = discovered.warnings
      } catch (error) { warnings = [`账户发现失败：${errText(error)}`] }
    }
    authorizedIds.value = ids
    callback.value = state.callback
    if (state.authorized && ids.length === 0) { awemeId.value = ''; awemeAccounts.value = [] }
    message.value = state.authorized
      ? requiresQianchuanAuthorization.value ? warnings.join('；')
        : warnings.length ? `OAuth 已授权；账户列表提示：${warnings.join('；')}。只有本次授权清单中的千川广告主 ID 才能查询。`
        : ids.length ? `已连接 ${ids.length} 个千川广告主；输入商品 ID 即可在授权范围内搜索。` : '已授权，但未返回千川广告主清单。请在授权页勾选千川业务账户。'
      : state.configured ? '应用凭证已保存；请完成账户授权。' : '请先在本机保存应用凭证。'
  } catch (error) { message.value = errText(error) }
}
async function configure(): Promise<void> {
  busy.value = true
  try {
    await window.api.qianchuan.apiConfigure({ appId: appId.value, secret: secret.value })
    secret.value = ''
    await refresh()
  } catch (error) { message.value = errText(error) }
  finally { busy.value = false }
}
async function authorize(): Promise<void> {
  busy.value = true
  try {
    const previousRevision = (await window.api.qianchuan.apiStatus()).authorizationRevision
    awemeId.value = ''
    authorizedIds.value = []
    awemeAccounts.value = []
    videos.value = []
    hasMore.value = false
    queryTargets.value = []
    const opened = await window.api.qianchuan.apiAuthorize(authUrl.value)
    authBrowserPath.value = opened.profilePath
    watchAuthCallback(previousRevision)
    message.value = '已在 Microsoft Edge 当前默认用户资料中打开授权页，正在等待本机回调。页面右上角显示当前登录用户；请将目标“倩姐严选”千川广告主纳入授权范围并点击页面底部“确认授权”。如需切换登录用户，可点击账号卡片旁的“更换账号授权”。'
  } catch (error) { stopAuthPoll(); message.value = errText(error); await refresh() }
  finally { busy.value = false }
}
async function loadAwemes(): Promise<void> {
  if (!authorizedIds.value.length) return
  busy.value = true
  try {
    const found = new Map<string, { id: string; name: string }>()
    const failures: string[] = []
    for (const id of authorizedIds.value) {
      try {
        const accounts = await window.api.qianchuan.apiAwemes(id)
        for (const account of accounts) found.set(`${id}:${account.id}`, account)
      } catch (error) { failures.push(`${accountNames.value[id] || id}：${errText(error)}`) }
    }
    awemeAccounts.value = Array.from(found.values())
    message.value = awemeAccounts.value.length
      ? `已读取授权范围内 ${awemeAccounts.value.length} 个抖音号；正常查询会自动遍历，无需手动选择。${failures.length ? `部分账户未能读取：${failures.join('；')}` : ''}`
      : `没有读取到可用抖音号。请确认广告主下已授权抖音号并申请“获取已授权抖音号”接口权限。${failures.length ? `详情：${failures.join('；')}` : ''}`
  } catch (error) {
    awemeAccounts.value = []
    message.value = `读取抖音号失败：${errText(error)}。可在高级选项中填写已确认的抖音号 ID。`
  } finally { busy.value = false }
}
async function query(nextPage = false): Promise<void> {
  const selectedProduct = productId.value.trim()
  if (!selectedProduct) {
    message.value = '填写商品 ID 后即可查询。'
    return
  }
  if (!authorizedIds.value.length || requiresQianchuanAuthorization.value) {
    message.value = '当前授权范围没有可查询的千川广告主，请先授权中枢下载账号可访问的千川账户。'
    return
  }
  busy.value = true
  try {
    let targets: SearchTarget[]
    if (nextPage) {
      targets = queryTargets.value.filter((target) => target.hasMore)
    } else {
      videos.value = []
      queryTargets.value = []
      hasMore.value = false
      awemeAccounts.value = []
      targets = []
      const failures: string[] = []
      for (const advertiserId of authorizedIds.value) {
        if (awemeId.value.trim()) {
          targets.push({ advertiserId, awemeId: awemeId.value.trim(), cursor: '', hasMore: false })
          continue
        }
        message.value = `正在读取 ${accountNames.value[advertiserId] || advertiserId} 下已授权的抖音号…`
        try {
          const accounts = await window.api.qianchuan.apiAwemes(advertiserId)
          awemeAccounts.value.push(...accounts)
          targets.push(...accounts.map((account) => ({ advertiserId, awemeId: account.id, cursor: '', hasMore: false })))
        } catch (error) { failures.push(`${accountNames.value[advertiserId] || advertiserId}：${errText(error)}`) }
      }
      if (!targets.length) {
        message.value = `授权范围内未读取到可查询的抖音号。请为应用开通“获取已授权抖音号”权限，或在高级选项填写抖音号 ID。${failures.length ? `详情：${failures.join('；')}` : ''}`
        hasMore.value = false
        return
      }
      queryTargets.value = targets
    }
    if (!targets.length) {
      hasMore.value = false
      message.value = '没有更多视频了。'
      return
    }

    const results: Video[] = []
    const failures: string[] = []
    let returned = 0
    for (let index = 0; index < targets.length; index += 1) {
      const target = targets[index]
      const accountName = accountNames.value[target.advertiserId] || target.advertiserId
      message.value = `正在搜索 ${accountName}（${index + 1}/${targets.length}）…`
      try {
        let cursor = nextPage ? target.cursor : ''
        const visitedCursors = new Set<string>()
        let pageCount = 0
        target.hasMore = false
        while (pageCount < MAX_AUTO_PAGES_PER_TARGET) {
          if (visitedCursors.has(cursor)) {
            failures.push(`${accountName}：分页游标重复，已停止避免重复请求`)
            target.hasMore = false
            break
          }
          visitedCursors.add(cursor)
          const result = await window.api.qianchuan.apiVideos({
            advertiserId: target.advertiserId,
            awemeId: target.awemeId,
            productId: selectedProduct,
            cursor: cursor || undefined
          })
          pageCount += 1
          returned += result.totalReturned
          results.push(...result.videos.map((video) => ({ ...video, advertiserId: target.advertiserId, advertiserName: accountName })))
          target.cursor = result.cursor
          target.hasMore = result.hasMore
          if (!result.hasMore) break
          if (!result.cursor || result.cursor === cursor) {
            failures.push(`${accountName}：接口标记有下一页但游标未前进，已停止避免重复请求`)
            target.hasMore = false
            break
          }
          cursor = result.cursor
        }
        if (pageCount >= MAX_AUTO_PAGES_PER_TARGET && target.hasMore) {
          failures.push(`${accountName}：单账号分页达到 ${MAX_AUTO_PAGES_PER_TARGET} 页保护上限，可点击“继续加载剩余视频”`)
        }
      } catch (error) {
        failures.push(`${accountName}：${errText(error)}`)
      }
    }
    queryTargets.value = [...queryTargets.value]
    const seen = new Set(videos.value.map((video) => `${video.advertiserId}:${video.videoId || video.url || video.awemeItemId}`))
    const uniqueResults = results.filter((video) => {
      const key = `${video.advertiserId}:${video.videoId || video.url || video.awemeItemId}`
      if (key && seen.has(key)) return false
      if (key) seen.add(key)
      return true
    })
    videos.value = nextPage ? [...videos.value, ...uniqueResults] : uniqueResults
    hasMore.value = queryTargets.value.some((target) => target.hasMore)
    const withUrl = uniqueResults.filter((video) => video.url).length
    message.value = `已搜索 ${authorizedIds.value.length} 个千川广告主、${targets.length} 个抖音号；本次遍历 ${returned} 条，其中 ${withUrl} 条有视频地址。${failures.length ? `部分账户查询提示：${failures.join('；')}` : ''}${hasMore.value ? '仍有后续页，可继续加载。' : ''}`
  } catch (error) {
    const detail = errText(error)
    message.value = /doesn't exist or the role is wrong|该广告主 ID 不在本次授权账户/.test(detail)
      ? `${detail}。请确认该千川广告主已纳入中枢下载账号的授权范围，再刷新授权状态。`
      : /qianchuan\/aweme\/authorized\/get|获取已授权抖音号|40002/i.test(detail)
        ? `自动读取抖音号失败：${detail}。可在高级选项填入抖音号 ID；也可以为应用申请“获取已授权抖音号”接口权限，让软件自动检索。`
        : detail
  }
  finally { busy.value = false }
}
function localDate(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}
function localTimestamp(): string {
  const now = new Date()
  return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}`
}
function fileToken(value: string): string { return value.replace(/[^\w-]/g, '_').slice(0, 80) || 'video' }

async function downloadVideos(selectedVideos: Video[]): Promise<void> {
  if (activeDownload || !selectedVideos.length) return
  downloadDir.value = await window.api.dir.getDownloadDir()
  const invalid = selectedVideos.filter((video) => {
    try { return new URL(video.url).protocol !== 'https:' } catch { return true }
  })
  const downloadable = selectedVideos.filter((video) => !invalid.includes(video))
  if (!downloadable.length) { message.value = '没有有效的 HTTPS 视频地址，已停止下载。'; return }
  const date = localDate()
  const timestamp = localTimestamp()
  const product = fileToken(productId.value.trim())
  const tasks = downloadable.map((video, index) => {
    const globalIndex = videos.value.indexOf(video)
    const sequence = (globalIndex >= 0 ? globalIndex : index) + 1
    const accountFolder = fileToken(`${video.advertiserName || '千川'}_${video.advertiserId}`)
    const videoId = fileToken(video.videoId || video.awemeItemId || `v${sequence}`)
    const dest = `${downloadDir.value}\\${accountFolder}\\${date}\\${product}\\${product}_${videoId}_${String(sequence).padStart(3, '0')}_${timestamp}.mp4`
    return { id: `qc-${Date.now()}-${sequence}-${index}`, product_id: product, url: video.url, dest, verify_video: true }
  })
  message.value = `正在下载 ${tasks.length} 条视频并验证 MP4…${invalid.length ? `（跳过 ${invalid.length} 条无效地址）` : ''}`
  let verified = 0
  let failed = invalid.length
  const checks: Promise<void>[] = []
  const destinations = new Map(tasks.map((task) => [task.id, task.dest]))
  offEngine?.()
  offEngine = window.api.engine.onEvent((event) => {
    if (event.taskId !== activeDownload) return
    if (event.type === 'task' && event.status === 'failed') {
      failed += 1
      message.value = `下载失败：${event.message ?? '视频直链不可用'}（${verified + failed}/${selectedVideos.length}）`
    }
    if (event.type === 'task' && (event.status === 'success' || event.status === 'skipped')) {
      const path = event.file_path ?? destinations.get(String(event.tid)) ?? ''
      checks.push(window.api.qianchuan.apiVerifyVideo(path).then((check) => {
        if (check.ok) verified += 1
        else failed += 1
      }).catch(() => { failed += 1 }))
    }
    if (event.type === 'error') {
      message.value = `下载失败：${event.message ?? '引擎异常'}`
      activeDownload = undefined
    }
    if (event.type === 'result') {
      void Promise.all(checks).then(() => {
        message.value = `批量下载完成：MP4 验证通过 ${verified} 条，失败或验证未通过 ${failed} 条。`
        activeDownload = undefined
      })
    }
  })
  try {
    const response = await window.api.engine.run({ script: 'download_queue.py', args: ['--workers', '1', '--max-retries', '2'], stdinJson: tasks })
    if (!response.taskId) throw new Error(response.error ?? '下载任务启动失败')
    activeDownload = response.taskId
  } catch (error) { message.value = errText(error); activeDownload = undefined }
}

async function download(video: Video): Promise<void> { await downloadVideos([video]) }
async function downloadAll(): Promise<void> { await downloadVideos(videos.value.filter((video) => Boolean(video.url))) }
async function searchProduct(product: string): Promise<{ videoCount: number; reason: string; authorized: boolean }> {
  productId.value = product.trim()
  resetSearch()
  panelDetails.value?.setAttribute('open', '')
  await refresh()
  if (!authorized.value || requiresQianchuanAuthorization.value || !authorizedIds.value.length) {
    message.value = '不能直接查询：请先完成中枢千川账户授权，并确认目标商品所属广告主在授权范围内。'
    return { videoCount: 0, reason: message.value, authorized: false }
  }
  await query(false)
  return { videoCount: videos.value.filter((video) => Boolean(video.url)).length, reason: message.value, authorized: true }
}
defineExpose({ searchProduct })
onMounted(async () => { downloadDir.value = await window.api.dir.getDownloadDir(); await refresh() })
onBeforeUnmount(() => { stopAuthPoll(); offEngine?.() })
</script>

<template>
  <details ref="panelDetails" class="qc-panel">
    <summary>千川开放平台 · 按商品 ID 查询视频</summary>
    <p class="hint">本页上方“搜索”会自动查询千川；这里保留单独重查入口。软件只搜索中枢下载账号获准访问的千川广告主及抖音号，工作台账号需能通过账户列表接口读取其下千川账户。</p>
    <p class="qc-message" role="status">{{ message }}</p>
    <div class="qc-row qc-query-row">
      <input v-model="productId" class="qc-input qc-product" placeholder="商品 ID" inputmode="numeric" aria-label="商品 ID" @input="resetSearch" @keyup.enter="query(false)" />
      <button class="qc-primary" :disabled="busy || !authorized || requiresQianchuanAuthorization || !authorizedIds.length || !productId" @click="query(false)">{{ busy ? '正在查询…' : '查询视频' }}</button>
      <button :disabled="busy" @click="refresh">刷新授权</button>
    </div>
    <p v-if="authorizedIds.length" class="hint">搜索范围：{{ authorizedIds.length }} 个已授权千川广告主（{{ authorizedIds.map((id) => accountNames[id] || id).join('、') }}）</p>
    <p v-if="authorized && requiresQianchuanAuthorization" class="qc-warning">当前没有发现可查询的千川广告主。可在授权页直接勾选千川业务账户；若用工作台管理员作为中枢账号，则需为应用开通“工作台下账户列表”权限。</p>
    <details class="qc-advanced">
      <summary>高级选项：读取或指定抖音号（可选）</summary>
      <p class="hint">默认搜索全部授权广告主下的抖音号。若自动读取接口无权限，可申请“获取已授权抖音号”权限，或填写一个已确认的抖音号 ID。</p>
      <div class="qc-row">
        <input v-model="awemeId" class="qc-input" list="qc-aweme-ids" placeholder="抖音号 ID（可选，留空自动检索）" inputmode="numeric" aria-label="可选抖音号 ID" @input="resetSearch" />
        <datalist id="qc-aweme-ids"><option v-for="account in awemeAccounts" :key="account.id" :value="account.id">{{ account.name }}</option></datalist>
        <button :disabled="busy || !authorized || requiresQianchuanAuthorization || !authorizedIds.length" @click="loadAwemes">读取已授权抖音号</button>
      </div>
    </details>
    <details class="qc-setup" :open="!configured || !authorized || requiresQianchuanAuthorization">
      <summary>首次设置 / 应用授权</summary>
      <p class="hint">App Secret 仅在本机输入并加密保存，不要发到聊天或截图。平台显示旧账户“已授权”不代表当前客户端已收到 Token。</p>
      <div class="qc-row">
        <input v-model="appId" class="qc-input" placeholder="App ID" inputmode="numeric" aria-label="千川 App ID" />
        <input v-model="secret" class="qc-input" type="password" autocomplete="off" placeholder="App Secret（仅本机加密保存）" aria-label="千川 App Secret" />
        <button :disabled="busy || !appId || !secret" @click="configure">保存应用凭证</button>
      </div>
      <div class="qc-row">
        <input v-model="authUrl" class="qc-input wide" placeholder="粘贴开放平台应用详情里生成的授权 URL" aria-label="千川授权 URL" />
        <button :disabled="busy || !configured || !authUrl" @click="authorize">从本客户端打开授权</button>
      </div>
      <p class="hint">此入口会在 Microsoft Edge 默认用户资料中打开授权页，复用该 Edge 现有登录态。页面右上角显示当前登录用户；要查询“倩姐严选”，须在授权范围中勾选对应千川广告主。需要更换登录用户时，使用账号卡片旁的“更换账号授权”；请勿直接粘贴原始授权 URL。</p>
      <p v-if="authBrowserPath" class="hint">Edge 用户资料目录：{{ authBrowserPath }}</p>
      <p v-if="authWaiting" class="qc-message" role="status">正在等待本机授权回调（最多 15 分钟）…</p>
      <p class="hint">本机回调：{{ callback }} · {{ authorized ? 'OAuth 已授权' : '未授权' }}<span v-if="authorizedIds.length"> · 千川广告主：{{ authorizedIds.join('、') }}</span><span v-else-if="authorized"> · {{ requiresQianchuanAuthorization ? '本次授权未包含千川广告主' : '千川业务账户待确认' }}</span></p>
      <p v-if="authorizedIdentitySummary" class="hint">本次授权账户：{{ authorizedIdentitySummary }}</p>
    </details>
    <p class="hint">下载目录：{{ downloadDir }}</p>
    <div v-for="(video, index) in videos" :key="`${video.videoId}-${index}`" class="qc-result">
      <span class="qc-title">{{ video.title || video.videoId || `视频 ${index + 1}` }}<small class="qc-origin">{{ video.advertiserName }}</small></span>
      <span>{{ video.url ? '有视频地址' : '无视频地址' }}</span>
      <button :disabled="!video.url || !!activeDownload" @click="download(video)">下载验证</button>
    </div>
    <div v-if="videos.some((video) => video.url)" class="qc-result-actions">
      <button :disabled="!!activeDownload" @click="downloadAll">下载全部已采集视频（{{ videos.filter((video) => video.url).length }}）</button>
    </div>
    <button v-if="hasMore" :disabled="busy" @click="query(true)">继续加载剩余视频</button>
  </details>
</template>

<style scoped>
.qc-panel { border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: var(--bg-panel); }
.qc-panel summary { cursor: pointer; font-weight: 700; color: var(--text); }
.qc-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; align-items: center; }
.qc-input { min-width: 145px; max-width: 100%; flex: 1 1 170px; padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-panel); color: var(--text); }
.qc-input.wide { flex-basis: 360px; }
.qc-query-row { padding: 10px; border-radius: 8px; background: var(--bg-subtle, var(--bg-panel)); }
.qc-product { min-width: 220px; flex: 2 1 300px; }
.qc-primary { background: var(--primary, #3678f6); border-color: var(--primary, #3678f6); color: #fff; font-weight: 700; }
.qc-warning { border-left: 3px solid #dc2626; padding: 8px 10px; color: #a12b2b; background: rgba(220, 38, 38, .06); }
.qc-advanced, .qc-setup { margin-top: 10px; border-top: 1px solid var(--border); padding-top: 10px; }
.qc-advanced summary, .qc-setup summary { cursor: pointer; color: var(--text-muted); font-size: 13px; }
button { border: 1px solid var(--border); border-radius: 6px; background: var(--bg-panel); color: var(--text); padding: 7px 10px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
.hint { color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
.qc-message { color: var(--text); overflow-wrap: anywhere; }
.qc-result { display: flex; gap: 10px; align-items: center; border-top: 1px solid var(--border); padding: 7px 0; }
.qc-result-actions { display: flex; justify-content: flex-end; padding-top: 8px; }
.qc-title { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.qc-origin { display: block; color: var(--text-muted); font-size: 11px; }
</style>
