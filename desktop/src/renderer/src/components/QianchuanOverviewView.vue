<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'

interface Target { account_id: number; account_name: string; qianchuan_account_id: number | null; qc_name: string | null; qc_id: string | null }
interface Asset { assetId: number; fileName: string; productId: string; eligible: boolean; reason?: string }
interface Job { id: number; materialName: string; productId: string; status: string; scheduledDate: string; remoteMaterialId?: string; reason?: string; screenshotPath?: string }
interface Limits { perProductVideos: number; perAccountProducts: number; perAccountVideos: number; perGroupProducts: number; perGroupVideos: number; maxConcurrentUploads: number; minStepDelayMs: number; maxStepDelayMs: number }

const emit = defineEmits<{ (event: 'navigate', value: string): void }>()
const targets = ref<Target[]>([])
const accountId = ref(0)
const qcId = ref(0)
const assets = ref<Asset[]>([])
const jobs = ref<Job[]>([])
const selected = ref<Set<number>>(new Set())
const busy = ref(false)
const loading = ref(false)
const message = ref('')
const progress = ref('')
const limits = reactive<Limits>({ perProductVideos: 10, perAccountProducts: 5, perAccountVideos: 50, perGroupProducts: 25, perGroupVideos: 250, maxConcurrentUploads: 1, minStepDelayMs: 800, maxStepDelayMs: 2000 })
const accounts = computed(() => [...new Map(targets.value.map(row => [row.account_id, { id: row.account_id, name: row.account_name }])).values()])
const linked = computed(() => targets.value.filter(row => row.account_id === accountId.value && row.qianchuan_account_id != null))
const eligible = computed(() => assets.value.filter(row => row.eligible))
const due = computed(() => jobs.value.filter(row => row.status === 'queued' && row.scheduledDate <= today()))
function today(): string { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` }
function rows<T>(data: Record<string, unknown>): T[] { return Array.isArray(data.rows) ? data.rows as T[] : [] }
function checked(data: Record<string, unknown>): Record<string, unknown> { if (data.ok === false) throw new Error(String(data.error ?? '操作失败')); return data }
function statusText(value: string): string { return ({ queued: '待上传', uploading: '上传中', succeeded: '已上传', failed: '失败', needs_review: '待核查' } as Record<string, string>)[value] ?? value }

async function loadTargets(): Promise<void> {
  targets.value = rows<Target>(checked(await window.api.qianchuan.uploadTargets()))
  if (!accounts.value.some(row => row.id === accountId.value)) accountId.value = accounts.value[0]?.id ?? 0
}
async function loadJobs(): Promise<void> { jobs.value = rows<Job>(checked(await window.api.qianchuan.uploadJobs(accountId.value))) }
async function loadAccount(): Promise<void> {
  selected.value = new Set()
  qcId.value = linked.value.some(row => row.qianchuan_account_id === qcId.value) ? qcId.value : linked.value[0]?.qianchuan_account_id ?? 0
  if (!accountId.value) { assets.value = []; jobs.value = []; return }
  loading.value = true
  try {
    const [a, j, q] = await Promise.all([window.api.qianchuan.eligibleAssets(accountId.value), window.api.qianchuan.uploadJobs(accountId.value), window.api.qianchuan.quotaConfig(accountId.value)])
    assets.value = rows<Asset>(checked(a)); jobs.value = rows<Job>(checked(j)); Object.assign(limits, checked(q).limits ?? {})
  } catch (error) { message.value = String(error) }
  finally { loading.value = false }
}
async function refresh(): Promise<void> {
  try {
    const previous = accountId.value
    await loadTargets()
    if (previous === accountId.value) await loadAccount()
  } catch (error) { message.value = String(error) }
}
watch(accountId, () => { void loadAccount() })
function toggle(row: Asset): void { if (!row.eligible) return; const next = new Set(selected.value); if (next.has(row.assetId)) next.delete(row.assetId); else next.add(row.assetId); selected.value = next }
function selectAll(): void { selected.value = selected.value.size === eligible.value.length ? new Set() : new Set(eligible.value.map(row => row.assetId)) }
async function openBrowser(): Promise<void> {
  if (!qcId.value) return
  const result = await window.api.qianchuan.openLogin(qcId.value)
  message.value = result.ok ? '已打开千川专属浏览器。请确认页面已登录。' : result.error ?? '浏览器未能打开'
}
async function runJobs(ids: number[]): Promise<void> {
  let success = 0; let failed = 0; let deferred = 0
  for (let i = 0; i < ids.length; i += 1) {
    progress.value = `上传 ${i + 1}/${ids.length} · 任务 #${ids[i]}`
    const result = await window.api.qianchuan.runUpload(ids[i])
    if (result.status === 'succeeded') success += 1
    else if (result.status === 'deferred') deferred += 1
    else {
      failed += 1
      message.value = `任务 #${ids[i]}：${String(result.error ?? '上传结果待核查')}`
      if (result.status === 'needs_review' || /登录|验证|网络|连接|页面|控件|浏览器/.test(String(result.error ?? ''))) break
    }
    await loadJobs()
  }
  progress.value = ''; await loadAccount()
  message.value = `本次确认上传 ${success} 条，顺延 ${deferred} 条，失败或待核查 ${failed} 条。`
  notifyGlobal(message.value, failed ? 'error' : 'success')
}
async function enqueue(): Promise<void> {
  if (!accountId.value || !qcId.value || !selected.value.size || busy.value) return
  busy.value = true
  try {
    const created: Array<{ id: number; status: string; scheduledDate: string }> = []
    const rejectedRows: Array<{ assetId: number; reason: string }> = []
    const ids = [...selected.value]
    for (let offset = 0; offset < ids.length; offset += 50) {
      const result = checked(await window.api.qianchuan.enqueueUpload({
        accountId: accountId.value, qianchuanAccountId: qcId.value,
        assetIds: ids.slice(offset, offset + 50)
      }))
      if (Array.isArray(result.jobs)) created.push(...result.jobs as typeof created)
      if (Array.isArray(result.rejected)) rejectedRows.push(...result.rejected as typeof rejectedRows)
    }
    const runnable = created.filter(row => row.status === 'queued' && row.scheduledDate <= today())
    message.value = `入队 ${created.length} 条，顺延 ${created.length - runnable.length} 条，未入队 ${rejectedRows.length} 条。${rejectedRows.length ? `首条：素材 #${rejectedRows[0].assetId} ${rejectedRows[0].reason}` : ''}`
    await loadJobs()
    if (runnable.length) await runJobs(runnable.map(row => row.id))
    else await loadAccount()
  } catch (error) { message.value = String(error); notifyGlobal(message.value, 'error') }
  finally { busy.value = false; progress.value = '' }
}
async function runDue(): Promise<void> { if (busy.value || !due.value.length) return; busy.value = true; try { await runJobs(due.value.map(row => row.id)) } finally { busy.value = false; progress.value = '' } }
async function retrySafe(id: number): Promise<void> {
  try { checked(await window.api.qianchuan.retrySafeUpload(id)); await loadJobs(); message.value = `任务 #${id} 已安全重排，按当前额度执行。` }
  catch (error) { message.value = String(error) }
}
function openScreenshot(path: string): void { void window.api.assets.openFolder(path) }
async function resolveReviewAbsent(id: number): Promise<void> {
  const note = window.prompt('请先在对应千川账户按素材名称核实确实不存在，再写明核查依据（至少 6 字）。')
  if (note === null) return
  try { checked(await window.api.qianchuan.reviewUploadAbsent({ id, note })); await loadJobs(); message.value = `任务 #${id} 已按人工核查结论重新排队。` }
  catch (error) { message.value = String(error) }
}
async function resolveReviewPresent(id: number): Promise<void> {
  const remoteMaterialId = window.prompt('请先在对应千川账户确认素材已存在，再输入页面上的千川素材 ID。')
  if (remoteMaterialId === null) return
  try { checked(await window.api.qianchuan.reviewUploadPresent({ id, remoteMaterialId })); await loadJobs(); message.value = `任务 #${id} 已补录远端素材 ID。` }
  catch (error) { message.value = String(error) }
}
async function saveLimits(): Promise<void> {
  if (!accountId.value) return
  try { const result = checked(await window.api.qianchuan.setQuotaConfig({ accountId: accountId.value, ...limits })); Object.assign(limits, result.limits ?? {}); message.value = '额度与执行间隔已保存。' }
  catch (error) { message.value = String(error) }
}
let refreshTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  void refresh()
  refreshTimer = setInterval(() => { if (accountId.value && !busy.value) void loadJobs().catch(() => undefined) }, 10_000)
})
onBeforeUnmount(() => { if (refreshTimer) clearInterval(refreshTimer) })
</script>

<template>
  <section class="qc-page">
    <header class="heading"><div><h1>千川素材上传</h1><p>从本地素材库按账号选取网页下载素材，抖音同商品确认发布后上传到绑定的千川账户。</p></div><button class="btn" @click="emit('navigate', '素材与工具')">查看素材库</button></header>
    <div class="toolbar">
      <label>抖音账号 <select v-model.number="accountId"><option :value="0">选择账号</option><option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name }}</option></select></label>
      <label>千川账户 <select v-model.number="qcId" :disabled="!linked.length"><option :value="0">选择已绑定账户</option><option v-for="target in linked" :key="target.qianchuan_account_id ?? 0" :value="target.qianchuan_account_id ?? 0">{{ target.qc_name || target.qc_id || `千川 #${target.qianchuan_account_id}` }}</option></select></label>
      <button class="btn" :disabled="!qcId" @click="openBrowser">打开专属浏览器</button><button class="btn" @click="refresh">刷新</button>
    </div>
    <p v-if="accountId && !linked.length" class="hint">此账号尚未绑定千川账户，请先在账号管理完成关联。</p>
    <p class="scope-note">当前额度按本机数据库登记；多台电脑共用额度时须接入共享原子登记服务。</p>
    <p v-if="message" class="hint">{{ message }}</p><p v-if="progress" class="progress" role="status">{{ progress }}</p>
    <div class="columns">
      <section class="panel"><header class="panel-head"><div><h2>可选素材</h2><small>可入队 {{ eligible.length }} / {{ assets.length }}</small></div><button class="btn" :disabled="!eligible.length" @click="selectAll">全选可用</button></header>
        <p class="explainer">仅显示已归属此账号的网页下载素材；发布未完成会显示原因。</p>
        <div v-if="loading" class="empty">读取中…</div><div v-else-if="!assets.length" class="empty">暂无符合来源和账号归属的素材，请先在素材库核对。</div>
        <div v-else class="list"><label v-for="asset in assets" :key="asset.assetId" class="asset" :class="{ unavailable: !asset.eligible }"><input type="checkbox" :disabled="!asset.eligible" :checked="selected.has(asset.assetId)" @change="toggle(asset)" /><span><strong :title="asset.fileName">{{ asset.fileName }}</strong><small>商品 {{ asset.productId }} · 素材 #{{ asset.assetId }}</small><small v-if="!asset.eligible" class="reason">{{ asset.reason }}</small></span></label></div>
        <footer class="panel-actions"><span>已选 {{ selected.size }} 条</span><button class="btn primary" :disabled="busy || !selected.size || !qcId" @click="enqueue">{{ busy ? '处理中…' : '入队并上传今日额度' }}</button></footer>
      </section>
      <section class="panel"><header class="panel-head"><div><h2>上传队列</h2><small>{{ jobs.length }} 条记录</small></div><button class="btn" :disabled="busy || !due.length" @click="runDue">执行到期任务</button></header>
        <div v-if="!jobs.length" class="empty">尚无上传任务。</div><div v-else class="list"><article v-for="job in jobs" :key="job.id" class="job"><div class="job-title"><strong :title="job.materialName">{{ job.materialName }}</strong><span :class="['status', job.status]">{{ statusText(job.status) }}</span></div><small>任务 #{{ job.id }} · 商品 {{ job.productId }} · 排程 {{ job.scheduledDate }}</small><small v-if="job.remoteMaterialId">千川素材 ID：{{ job.remoteMaterialId }}</small><small v-if="job.reason" class="reason">{{ job.reason }}</small><button v-if="job.screenshotPath" class="btn" @click="openScreenshot(job.screenshotPath)">查看失败截图</button><button v-if="job.status === 'failed'" class="btn" :disabled="busy" @click="retrySafe(job.id)">确认未上传后重排</button><template v-if="job.status === 'needs_review'"><button class="btn" :disabled="busy" @click="resolveReviewPresent(job.id)">已在千川，补录 ID</button><button class="btn" :disabled="busy" @click="resolveReviewAbsent(job.id)">核实未上传后重排</button></template></article></div>
      </section>
    </div>
    <details class="settings"><summary>上传额度与执行间隔</summary><div class="settings-grid"><label>每品条数<input v-model.number="limits.perProductVideos" type="number" min="1" /></label><label>账号每日品数<input v-model.number="limits.perAccountProducts" type="number" min="1" /></label><label>账号每日条数<input v-model.number="limits.perAccountVideos" type="number" min="1" /></label><label>五账号组每日品数<input v-model.number="limits.perGroupProducts" type="number" min="1" /></label><label>五账号组每日条数<input v-model.number="limits.perGroupVideos" type="number" min="1" /></label><label>最大并发<input v-model.number="limits.maxConcurrentUploads" type="number" min="1" /></label><label>最小间隔（毫秒）<input v-model.number="limits.minStepDelayMs" type="number" min="1" /></label><label>最大间隔（毫秒）<input v-model.number="limits.maxStepDelayMs" type="number" min="1" /></label></div><button class="btn primary" :disabled="!accountId || busy" @click="saveLimits">保存该账号配置</button></details>
  </section>
</template>

<style scoped>
.qc-page{display:flex;flex-direction:column;gap:12px;width:100%;min-width:0;min-height:0;overflow:auto;padding:2px 2px 14px;color:#263249}.heading,.toolbar,.panel-head,.panel-actions,.job-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.heading h1{margin:0;font-size:21px}.heading p,.explainer,.scope-note{margin:4px 0 0;color:#6c788b;font-size:12px;line-height:1.5}.toolbar{justify-content:flex-start;flex-wrap:wrap;padding:13px;border:1px solid #e1e6ee;border-radius:7px;background:#fff}.toolbar label,.settings-grid label{display:flex;align-items:center;gap:8px;font-size:12px;color:#536078}select,input[type=number]{min-height:30px;border:1px solid #d8dee8;border-radius:5px;padding:4px 7px;background:#fff;color:#263249}.toolbar select{min-width:170px}.btn{min-height:30px;border:1px solid #d8dee8;border-radius:5px;padding:4px 10px;background:#fff;color:#263249;cursor:pointer;font-size:12px}.btn.primary{background:#3676e7;border-color:#3676e7;color:#fff}.btn:disabled{opacity:.48;cursor:not-allowed}.hint,.progress{margin:0;padding:9px 11px;border-radius:5px;background:#fff7e9;color:#835c19;font-size:12px}.progress{background:#edf4ff;color:#285bad}.scope-note{margin:0}.columns{display:grid;grid-template-columns:1fr 1fr;gap:12px;min-height:390px}.panel{display:flex;flex-direction:column;min-width:0;border:1px solid #e1e6ee;border-radius:7px;background:#fff}.panel-head{padding:12px;border-bottom:1px solid #e8ecf2}.panel-head h2{margin:0;font-size:15px}.panel-head small{color:#8290a2}.explainer{padding:0 12px}.list{flex:1;min-height:0;max-height:380px;overflow:auto;padding:5px 12px}.asset,.job{display:flex;align-items:flex-start;gap:9px;border-bottom:1px solid #eef1f5;padding:10px 0;font-size:12px}.asset.unavailable{opacity:.7}.asset span{display:flex;flex-direction:column;min-width:0;gap:4px}.asset strong,.job-title strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.asset small,.job small{display:block;color:#7a8797}.reason{color:#b54743!important}.job{display:block}.job-title{margin-bottom:5px}.job small{margin-top:3px}.status{flex-shrink:0;padding:2px 6px;border-radius:9px;background:#edf1f7;color:#5c6a7c}.status.succeeded{background:#e6f7ed;color:#16834d}.status.failed,.status.needs_review{background:#fff0ed;color:#b3483d}.panel-actions{padding:12px;border-top:1px solid #e8ecf2;font-size:12px}.empty{padding:35px 14px;text-align:center;color:#8a95a5;font-size:12px}.settings{border:1px solid #e1e6ee;border-radius:7px;background:#fff;padding:12px}.settings summary{cursor:pointer;font-size:13px;font-weight:600}.settings-grid{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}.settings-grid label{flex-direction:column;align-items:flex-start}.settings-grid input{width:128px}@media(max-width:950px){.columns{grid-template-columns:1fr}}
</style>
