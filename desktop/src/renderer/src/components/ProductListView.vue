<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'
import { createSelectionFilterSettings } from '../utils/selection_filters'

const emit = defineEmits<{
  (event: 'navigate', page: string): void
  (event: 'create-publish-task', product: { accountId: number; productId: string; title: string; sourceUrl: string }): void
}>()

type ProductState = 'pending' | 'cart_added' | 'selection_failed' | 'needs_review' | 'publish_failed' | 'published' | 'uploaded'
interface ProductRow extends ProductListRow {
  candidateSnapshot: Record<string, unknown>
}
interface AccountOption { id: number; name: string }
interface ProductDraft { title: string; sku: string; categoryText: string }

const STATUS_LABELS: Record<ProductState, string> = {
  pending: '待选品',
  cart_added: '已加入选品车',
  selection_failed: '选品失败',
  needs_review: '待核查',
  publish_failed: '发布失败',
  published: '已发布',
  uploaded: '已上传'
}
const PAGE_SIZES = [10, 20, 30, 40, 50, 80, 100]

const rows = ref<ProductRow[]>([])
const accounts = ref<AccountOption[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const accountFilter = ref(0)
const statusFilter = ref('')
const searchText = ref('')
const loading = ref(false)
const busy = ref(false)
const notice = ref('')
const noticeError = ref(false)
const progressText = ref('')
const selectedKeys = ref(new Set<string>())
const editing = ref<ProductRow | null>(null)
const editDraft = reactive<ProductDraft>({ title: '', sku: '', categoryText: '' })
let searchTimer: number | undefined

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const selectedRows = computed(() => rows.value.filter((row) => selectedKeys.value.has(rowKey(row))))
const pendingSelected = computed(() => selectedRows.value.filter((row) => row.status === 'pending' || row.status === 'selection_failed'))
const pageLabel = computed(() => `${total.value ? (page.value - 1) * pageSize.value + 1 : 0}–${Math.min(page.value * pageSize.value, total.value)} / ${total.value}`)

function rowKey(row: ProductRow): string { return `${row.accountId}:${row.productId}` }
function statusLabel(status: string): string { return STATUS_LABELS[status as ProductState] ?? '待核查' }
function displayDate(value: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value.replace('T', ' ') : date.toLocaleString('zh-CN', { hour12: false })
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await window.api.productList.list({
      accountId: accountFilter.value || null,
      status: statusFilter.value,
      query: searchText.value.trim(),
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value
    })
    rows.value = result.rows as ProductRow[]
    total.value = result.total
    accounts.value = result.accounts
    const visibleKeys = new Set(rows.value.map(rowKey))
    selectedKeys.value = new Set([...selectedKeys.value].filter((key) => visibleKeys.has(key)))
  } catch (error) {
    noticeError.value = true
    notice.value = `商品列表读取失败：${String(error)}`
    notifyGlobal(notice.value, 'error')
  } finally {
    loading.value = false
  }
}

watch([accountFilter, statusFilter, pageSize], () => { page.value = 1; void load() })
watch(searchText, () => {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => { page.value = 1; void load() }, 280)
})

function toggleRow(row: ProductRow): void {
  const next = new Set(selectedKeys.value)
  if (next.has(rowKey(row))) next.delete(rowKey(row))
  else next.add(rowKey(row))
  selectedKeys.value = next
}

function toggleVisible(): void {
  const next = new Set(selectedKeys.value)
  const all = rows.value.length > 0 && rows.value.every((row) => next.has(rowKey(row)))
  for (const row of rows.value) all ? next.delete(rowKey(row)) : next.add(rowKey(row))
  selectedKeys.value = next
}

function openEdit(row: ProductRow): void {
  editing.value = row
  editDraft.title = row.title
  editDraft.sku = row.sku
  editDraft.categoryText = row.categoryLabels.join('，')
}

async function saveEdit(): Promise<void> {
  if (!editing.value || busy.value) return
  busy.value = true
  try {
    await window.api.productList.update({
      accountId: editing.value.accountId,
      productId: editing.value.productId,
      title: editDraft.title.trim(),
      sku: editDraft.sku.trim(),
      categoryLabels: editDraft.categoryText.split(/[，,、]/).map((item) => item.trim()).filter(Boolean).slice(0, 6)
    })
    editing.value = null
    noticeError.value = false
    notice.value = '商品信息已保存。'
    await load()
  } catch (error) {
    noticeError.value = true
    notice.value = `保存失败：${String(error)}`
  } finally {
    busy.value = false
  }
}

async function deleteRow(row: ProductRow): Promise<void> {
  if (busy.value || !window.confirm(`从商品列表移除「${row.title || row.productId}」？关联发布任务、千川上传记录或待核查商品会被保护。`)) return
  busy.value = true
  try {
    await window.api.productList.delete({ accountId: row.accountId, productId: row.productId })
    selectedKeys.value.delete(rowKey(row))
    noticeError.value = false
    notice.value = '商品已从列表移除。'
    await load()
  } catch (error) {
    noticeError.value = true
    notice.value = `无法删除：${String(error)}`
  } finally {
    busy.value = false
  }
}

function createPublishTask(row: ProductRow): void {
  emit('create-publish-task', {
    accountId: row.accountId,
    productId: row.productId,
    title: row.title,
    sourceUrl: row.sourceUrl || `https://haohuo.jinritemai.com/views/product/item2?id=${encodeURIComponent(row.productId)}`
  })
}

function engineCommand(script: string, args: string[]): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let taskId = ''
    let settled = false
    let off = (): void => undefined
    let timer = 0
    const pendingEvents: EngineEventPayload[] = []
    const handleEvent = (event: EngineEventPayload): void => {
      if (!taskId || event.taskId !== taskId) return
      if (event.type === 'result') finish(() => resolve((event.data ?? {}) as Record<string, unknown>))
      if (event.type === 'error') finish(() => reject(new Error(event.message ?? '引擎操作失败')))
    }
    const finish = (action: () => void): void => {
      if (settled) return
      settled = true
      if (timer) window.clearTimeout(timer)
      off()
      action()
    }
    off = window.api.engine.onEvent((event) => {
      if (!taskId) pendingEvents.push(event)
      else handleEvent(event)
    })
    timer = window.setTimeout(() => finish(() => reject(new Error('操作超时'))), 30_000)
    void window.api.engine.run({ script, args }).then((reply) => {
      if (!reply.taskId) finish(() => reject(new Error(reply.error ?? '操作启动失败')))
      else {
        taskId = reply.taskId
        for (const event of pendingEvents.splice(0)) handleEvent(event)
      }
    }).catch((error) => finish(() => reject(error)))
  })
}

async function resolveCartContext(accountId: number, productId: string): Promise<{ settings: Record<string, unknown>; profilePath?: string; downloadPath: string }> {
  const paths = await window.api.appPaths.get()
  const saved = await engineCommand('selection_settings.py', ['--db', paths.databasePath, 'get', '--account-id', String(accountId)])
  const settingsRoot = (saved.settings ?? {}) as { global?: Record<string, unknown>; account?: Record<string, unknown> }
  const globalSettings = { ...(settingsRoot.global ?? {}) }
  const accountSettings = settingsRoot.account ?? {}
  const settings: Record<string, unknown> = { ...globalSettings }
  settings.categoryTree = Array.isArray(saved.categories) ? saved.categories : []
  const accountMode = String(accountSettings.selectionMode ?? 'inherit')
  const accountCategoryMode = String(accountSettings.categoryMode ?? 'inherit')
  settings.selectionMode = accountMode === 'inherit' ? globalSettings.selectionMode : accountMode
  settings.categoryMode = accountCategoryMode === 'inherit' ? globalSettings.categoryMode : accountCategoryMode
  if (accountMode === 'category' && accountSettings.categorySelection) settings.categorySelection = accountSettings.categorySelection
  if (accountSettings.filterOverrides && typeof accountSettings.filterOverrides === 'object') {
    Object.assign(
      settings,
      createSelectionFilterSettings(
        accountSettings.filterOverrides,
        createSelectionFilterSettings(globalSettings)
      )
    )
  }
  const quotaOverride = accountSettings.dailyQuotaOverride as Record<string, unknown> | undefined
  if (quotaOverride?.enabled === true) {
    settings.quota = {
      ...((settings.quota ?? {}) as Record<string, unknown>),
      perProductVideos: globalSettings.quota && (globalSettings.quota as Record<string, unknown>).perProductVideos,
      perAccountProducts: quotaOverride.perAccountProducts,
      perAccountVideos: quotaOverride.perAccountVideos
    }
  }
  const profileKey = `account-${accountId}`
  const profilePathValue = String(accountSettings.browserProfilePath ?? '').trim()
  const profile = await window.api.browser.validateProfilePath({ profileKey, profilePath: profilePathValue || undefined })
  if (!profile.ok || !profile.path) throw new Error(profile.error ?? '账号浏览器缓存路径未通过校验')
  const downloadRoot = (await window.api.dir.getDownloadDir()).replace(/[\\/]+$/, '')
  const configuredDownload = String(accountSettings.downloadPath ?? '').trim()
  const path = await window.api.dir.canonicalizePath(configuredDownload || `${downloadRoot}\\账号-${accountId}`)
  if (!path.ok || !path.path) throw new Error(path.error ?? '账号下载目录未通过校验')
  const quota = await engineCommand('selection_settings.py', ['--db', paths.databasePath, 'quota', 'get', '--account-id', String(accountId)])
  const usage = (quota.account ?? {}) as { products?: number; videos?: number }
  const productUsage = (quota.productUsage ?? {}) as Record<string, number>
  const quotaSettings = (settings.quota ?? {}) as Record<string, unknown>
  if (Number(usage.products ?? 0) >= Number(quotaSettings.perAccountProducts ?? 5)) {
    throw new Error('账号今日选品配额已用完，请到选品设置查看配额。')
  }
  if (Number(productUsage[productId] ?? 0) >= Number(quotaSettings.perProductVideos ?? 10)) {
    throw new Error('该商品今日视频配额已用完。')
  }
  const locks = await engineCommand('publish_queue.py', ['--db', paths.databasePath, 'product-locks', '--account-id', String(accountId)])
  const lockedIds = Array.isArray(locks.locks)
    ? (locks.locks as Array<Record<string, unknown>>).map((item) => String(item.product_id ?? item.productId ?? ''))
    : []
  if (lockedIds.includes(productId)) throw new Error('该商品已关联发布队列或已发布，本次不重复加车。')
  return { settings, profilePath: profilePathValue ? profile.path : undefined, downloadPath: path.path }
}

async function addOneToCart(row: ProductRow): Promise<boolean> {
  if (row.status === 'needs_review') throw new Error('该商品加车结果待核查，核查前不能重试。')
  const context = await resolveCartContext(row.accountId, row.productId)
  const savedCandidate = row.candidateSnapshot
  const cardIndex = Number(savedCandidate.cardIndex)
  if (!Number.isInteger(cardIndex)) throw new Error('商品缺少原候选快照，无法安全重试。')
  const candidate = { ...savedCandidate, productId: row.productId, title: row.title, cartAdded: false, cartAttempted: false }
  progressText.value = `${row.accountName} · ${row.title || row.productId}：正在复核并加入百应选品车…`
  let reply: Record<string, unknown>
  try {
    reply = await window.api.browser.runBaiyingSelection({
      profileKey: `account-${row.accountId}`,
      profilePath: context.profilePath,
      settings: context.settings,
      accountId: row.accountId,
      accountKey: `local:${row.accountId}`,
      downloadPath: context.downloadPath,
      selectedIndexes: [cardIndex],
      candidateSnapshot: [candidate],
      allowCandidateFallback: false,
      recoverExistingCart: false,
      refreshCandidatesById: true,
      analyzeBeforeCart: true,
      maxSelectedProducts: 1,
      stopAfterCart: true,
      previewOnly: false
    })
  } catch (error) {
    const reason = String(error)
    if (!/未向百应发送加车命令|本机登记前已停止平台操作|未执行加车/.test(reason)) {
      await window.api.productList.cartOutcome({ accountId: row.accountId, productId: row.productId, status: 'needs_review', reason })
    }
    throw error
  }
  const completed = Array.isArray(reply.completed) ? reply.completed as Array<Record<string, unknown>> : []
  const result = completed.find((item) => String(item.productId ?? '') === row.productId)
  if (result?.cartAdded === true) {
    await window.api.productList.cartOutcome({ accountId: row.accountId, productId: row.productId, status: 'cart_added' })
    const paths = await window.api.appPaths.get()
    try {
      await engineCommand('selection_settings.py', [
        '--db', paths.databasePath, 'quota', 'record', '--account-id', String(row.accountId), '--product-id', row.productId, '--videos', '0'
      ])
    } catch (error) {
      throw new Error(`百应已确认加车，但本机配额记录失败：${String(error)}`)
    }
    return true
  }
  if (result?.skipped === true || result?.cartAttempted === true) {
    const reason = String(result.status ?? result.error ?? '百应操作结果未能确认；请先核查选品车。')
    await window.api.productList.cartOutcome({ accountId: row.accountId, productId: row.productId, status: 'needs_review', reason })
    throw new Error(`结果待核查：${reason}`)
  }
  const reason = String(result?.status ?? result?.error ?? reply.error ?? '未确认加入选品车。')
  await window.api.productList.cartOutcome({ accountId: row.accountId, productId: row.productId, status: 'selection_failed', reason })
  throw new Error(reason)
}

async function runCart(rowsToAdd: ProductRow[], label: string): Promise<void> {
  const uniqueRows = [...new Map(rowsToAdd.map((row) => [rowKey(row), row])).values()]
    .filter((row) => row.status === 'pending' || row.status === 'selection_failed')
  if (!uniqueRows.length || busy.value) return
  if (!window.confirm(`将使用每个商品所属账号的百应隔离登录态，逐个复核并加入 ${uniqueRows.length} 个商品。遇到登录失效、限流或结果不明时会停止并保留核查状态。继续？`)) return
  busy.value = true
  let succeeded = 0
  let failed = 0
  try {
    for (const [index, row] of uniqueRows.entries()) {
      try {
        const added = await addOneToCart(row)
        if (added) succeeded++
      } catch (error) {
        failed++
        const message = String(error)
        noticeError.value = true
        notice.value = `${row.title || row.productId}：${message}`
        if (/登录|验证码|限流|超时|待核查|不确定|连接/.test(message)) break
      }
      progressText.value = `${label} ${Math.min(index + 1, uniqueRows.length)}/${uniqueRows.length} · 成功 ${succeeded} · 需处理 ${failed}`
      await load()
    }
    if (!failed) {
      noticeError.value = false
      notice.value = `加车处理完成：已确认加入 ${succeeded} 个商品。`
    }
  } finally {
    busy.value = false
    progressText.value = ''
    selectedKeys.value = new Set()
    await load()
    notifyGlobal(notice.value || '加车批次已结束。', noticeError.value ? 'warning' : 'success')
  }
}

async function addAllPending(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    const pending: ProductRow[] = []
    let offset = 0
    while (true) {
      const result = await window.api.productList.list({ accountId: accountFilter.value || null, status: 'pending', limit: 100, offset })
      pending.push(...result.rows as ProductRow[])
      offset += 100
      if (offset >= result.total || !result.rows.length) break
    }
    busy.value = false
    await runCart(pending, '一键加车')
  } catch (error) {
    busy.value = false
    noticeError.value = true
    notice.value = `读取待选品失败：${String(error)}`
  }
}

function goPage(next: number): void { page.value = Math.max(1, Math.min(pageCount.value, next)); void load() }

onMounted(() => { void load() })
</script>

<template>
  <section class="product-list-view">
    <header class="page-header">
      <div>
        <div class="eyebrow">发布与计划 / 商品管理</div>
        <h1>商品列表</h1>
        <p>按账号跟踪选品、加车和后续发布状态；选品成功后会自动登记到这里。</p>
      </div>
      <div class="header-actions">
        <button class="btn" :disabled="loading || busy" @click="load">刷新</button>
        <button class="btn primary" :disabled="busy" @click="addAllPending">一键加入选品车</button>
      </div>
    </header>

    <div v-if="notice" class="notice" :class="{ error: noticeError }">{{ notice }}</div>
    <div v-if="progressText" class="progress-line"><span class="spinner"></span>{{ progressText }}</div>

    <div class="toolbar card">
      <label>账号
        <select v-model.number="accountFilter">
          <option :value="0">全部账号</option>
          <option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name }}</option>
        </select>
      </label>
      <label>状态
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="(label, status) in STATUS_LABELS" :key="status" :value="status">{{ label }}</option>
        </select>
      </label>
      <label class="search">搜索
        <input v-model="searchText" placeholder="商品名称、商品 ID 或账号" />
      </label>
      <div class="toolbar-end">
        <button class="btn" :disabled="!rows.length" @click="toggleVisible">{{ rows.length && rows.every((row) => selectedKeys.has(rowKey(row))) ? '取消全选' : '全选本页' }}</button>
        <button class="btn primary" :disabled="!pendingSelected.length || busy" @click="runCart(pendingSelected, '批量加车')">批量加入选品车<span v-if="pendingSelected.length">（{{ pendingSelected.length }}）</span></button>
      </div>
    </div>

    <div class="table-card card">
      <div class="table-caption">
        <div><strong>选品记录</strong><span>{{ pageLabel }}</span></div>
        <small>加车只重试待处理商品；结果不明时会转为待核查，避免重复提交。</small>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th class="check-col"></th><th>商品信息</th><th>状态</th><th>编码</th><th>日期时间</th><th class="action-col">操作</th></tr>
          </thead>
          <tbody v-if="!loading && rows.length">
            <tr v-for="row in rows" :key="rowKey(row)">
              <td><input type="checkbox" :checked="selectedKeys.has(rowKey(row))" @change="toggleRow(row)" /></td>
              <td>
                <div class="product-cell">
                  <div class="thumb">
                    <img v-if="row.coverUrl" :src="row.coverUrl" :alt="row.title" @error="($event.target as HTMLImageElement).style.display='none'" />
                    <span v-else>商品</span>
                  </div>
                  <div class="product-copy">
                    <strong :title="row.title">{{ row.title || '未获取商品名称' }}</strong>
                    <span>{{ row.accountName }} · ID {{ row.productId }}</span>
                    <small>规格：{{ row.sku || '—' }}　分类：{{ row.categoryLabels.join(' / ') || '—' }}</small>
                    <small v-if="row.failureReason" class="failure-reason">{{ row.failureReason }}</small>
                  </div>
                </div>
              </td>
              <td><span class="status-pill" :class="`status-${row.status}`">{{ statusLabel(row.status) }}</span></td>
              <td class="code-cell">{{ row.commodityId || '—' }}</td>
              <td class="date-cell" :title="`选品时间：${displayDate(row.selectedAt)}`">{{ displayDate(row.updatedAt) }}</td>
              <td>
                <div class="row-actions">
                  <button v-if="row.status === 'pending' || row.status === 'selection_failed'" class="text-button" :disabled="busy" @click="runCart([row], '单品加车')">加车</button>
                  <button class="text-button" disabled title="当前版本没有经验证的百应上架接口">上架</button>
                  <button class="text-button" @click="openEdit(row)">编辑</button>
                  <button class="text-button" :disabled="row.status !== 'cart_added'" :title="row.status !== 'cart_added' ? '商品确认加车后才可创建发布计划' : '创建发布计划'" @click="createPublishTask(row)">出单</button>
                  <button class="text-button danger" :disabled="busy" @click="deleteRow(row)">删除</button>
                </div>
              </td>
            </tr>
          </tbody>
          <tbody v-else-if="!loading">
            <tr><td colspan="6" class="empty-state"><div class="empty-icon">▤</div><strong>还没有商品记录</strong><span>完成一次自动选品后，选中的商品会自动显示在这里。</span><button class="btn" @click="emit('navigate', '选品设置')">前往选品设置</button></td></tr>
          </tbody>
          <tbody v-else><tr><td colspan="6" class="loading-state">正在读取商品列表…</td></tr></tbody>
        </table>
      </div>
      <footer class="pagination">
        <label>每页
          <select v-model.number="pageSize">
            <option v-for="size in PAGE_SIZES" :key="size" :value="size">{{ size }} 条</option>
          </select>
        </label>
        <button class="btn" :disabled="page <= 1 || loading" @click="goPage(page - 1)">上一页</button>
        <span>第 {{ page }} / {{ pageCount }} 页</span>
        <button class="btn" :disabled="page >= pageCount || loading" @click="goPage(page + 1)">下一页</button>
      </footer>
    </div>

    <div v-if="editing" class="modal-backdrop" @click.self="editing = null">
      <section class="edit-dialog" role="dialog" aria-modal="true" aria-label="编辑商品信息">
        <header><div><h2>编辑商品信息</h2><span>{{ editing.accountName }} · {{ editing.productId }}</span></div><button class="close-button" @click="editing = null">×</button></header>
        <label>商品名称<input v-model="editDraft.title" maxlength="500" /></label>
        <label>规格 / SKU<input v-model="editDraft.sku" maxlength="200" placeholder="可选" /></label>
        <label>分类标签<input v-model="editDraft.categoryText" placeholder="多个分类用逗号分隔" /></label>
        <p>编辑仅更新本机商品列表显示，不会修改百应平台商品信息。</p>
        <footer><button class="btn" @click="editing = null">取消</button><button class="btn primary" :disabled="busy || !editDraft.title.trim()" @click="saveEdit">保存</button></footer>
      </section>
    </div>
  </section>
</template>

<style scoped>
.product-list-view { display:flex; flex-direction:column; gap:14px; min-height:0; height:100%; color:var(--text-main); }
.page-header { display:flex; align-items:flex-end; justify-content:space-between; gap:18px; padding:4px 2px 2px; }
.eyebrow { color:var(--text-muted); font-size:11px; margin-bottom:6px; }
h1 { margin:0; font-size:22px; letter-spacing:.01em; }
.page-header p { margin:6px 0 0; color:var(--text-muted); font-size:12px; }
.header-actions,.toolbar-end { display:flex; align-items:center; gap:8px; flex:0 0 auto; }
.card { border:1px solid var(--border); border-radius:10px; background:var(--bg-card,#fff); box-shadow:0 2px 8px rgba(20,45,90,.045); }
.btn { min-height:32px; padding:0 11px; border:1px solid var(--border); border-radius:7px; background:#fff; color:var(--text-main); font-size:12px; cursor:pointer; white-space:nowrap; }
.btn:hover:not(:disabled) { border-color:#80a9fa; color:#2563eb; }
.btn:disabled { opacity:.48; cursor:not-allowed; }
.btn.primary { color:#fff; background:var(--brand,#3478f6); border-color:var(--brand,#3478f6); }
.notice,.progress-line { padding:9px 12px; border-radius:8px; color:#1d4ed8; background:#eff6ff; font-size:12px; }
.notice.error { color:#b42318; background:#fef2f2; }
.progress-line { display:flex; align-items:center; gap:8px; }
.spinner { width:13px; height:13px; border:2px solid #bfd4ff; border-top-color:#3478f6; border-radius:50%; animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.toolbar { display:flex; align-items:flex-end; gap:11px; padding:11px 12px; flex-wrap:wrap; }
.toolbar label,.pagination label { display:flex; flex-direction:column; gap:4px; color:var(--text-muted); font-size:10px; }
select,input { height:32px; min-width:135px; padding:0 9px; border:1px solid var(--border); border-radius:6px; color:var(--text-main); background:#fff; font-size:12px; }
.search { flex:1 1 220px; min-width:190px; }
.search input { width:100%; }
.toolbar-end { margin-left:auto; }
.table-card { display:flex; flex-direction:column; flex:1; min-height:200px; overflow:hidden; }
.table-caption { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:12px 14px; border-bottom:1px solid var(--border); }
.table-caption div { display:flex; align-items:baseline; gap:10px; }
.table-caption strong { font-size:13px; }
.table-caption span,.table-caption small { color:var(--text-muted); font-size:11px; }
.table-scroll { min-height:0; flex:1; overflow:auto; }
table { width:100%; border-collapse:collapse; table-layout:fixed; }
thead { position:sticky; top:0; z-index:1; background:#f7f9fc; }
th { padding:10px 9px; color:var(--text-muted); font-size:11px; font-weight:600; text-align:left; white-space:nowrap; }
td { padding:9px; border-top:1px solid #edf0f5; font-size:12px; vertical-align:middle; }
.check-col { width:38px; }.action-col { width:216px; }
th:nth-child(3) { width:116px; } th:nth-child(4) { width:125px; } th:nth-child(5) { width:150px; }
.product-cell { display:flex; align-items:center; min-width:0; gap:10px; }
.thumb { width:52px; height:52px; flex:0 0 52px; display:grid; place-items:center; overflow:hidden; border:1px solid #e6eaf0; border-radius:7px; color:#98a3b5; background:#f3f5f8; font-size:10px; }
.thumb img { width:100%; height:100%; object-fit:cover; }
.product-copy { display:flex; flex-direction:column; min-width:0; gap:3px; }
.product-copy strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.product-copy span,.product-copy small { color:var(--text-muted); font-size:10px; }
.failure-reason { overflow:hidden; color:#b42318!important; text-overflow:ellipsis; white-space:nowrap; }
.status-pill { display:inline-flex; align-items:center; min-height:24px; padding:0 8px; border-radius:99px; background:#eff6ff; color:#2563eb; font-size:10px; white-space:nowrap; }
.status-cart_added,.status-published,.status-uploaded { color:#168044; background:#e9f8ef; }
.status-selection_failed,.status-publish_failed { color:#b42318; background:#fff0ef; }
.status-needs_review { color:#a15c00; background:#fff5dc; }
.code-cell { color:#475569; font-variant-numeric:tabular-nums; }.date-cell { color:var(--text-muted); font-size:11px; white-space:nowrap; }
.row-actions { display:flex; align-items:center; flex-wrap:wrap; gap:7px; }
.text-button { padding:3px 0; border:0; color:#3478f6; background:transparent; font-size:11px; cursor:pointer; white-space:nowrap; }
.text-button:disabled { color:#a5adba; cursor:not-allowed; }.text-button.danger { color:#cf3f3f; }
.empty-state,.loading-state { height:220px; color:var(--text-muted); text-align:center; }
.empty-state { display:flex; align-items:center; justify-content:center; flex-direction:column; gap:8px; }
.empty-state strong { color:var(--text-main); font-size:14px; }.empty-state span { font-size:11px; margin-bottom:4px; }.empty-icon { color:#9db9f4; font-size:26px; }
.loading-state { padding-top:90px; }
.pagination { display:flex; align-items:center; justify-content:flex-end; gap:9px; padding:10px 12px; border-top:1px solid var(--border); color:var(--text-muted); font-size:11px; }
.pagination select { min-width:84px; height:29px; }
.pagination .btn { min-height:29px; }
.modal-backdrop { position:fixed; inset:0; z-index:30; display:grid; place-items:center; background:rgba(15,23,42,.42); }
.edit-dialog { width:min(480px,calc(100vw - 36px)); padding:18px; border:1px solid var(--border); border-radius:12px; background:#fff; box-shadow:0 18px 48px rgba(15,23,42,.22); }
.edit-dialog header,.edit-dialog footer { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.edit-dialog h2 { margin:0 0 4px; font-size:16px; }.edit-dialog header span,.edit-dialog p { color:var(--text-muted); font-size:11px; }
.close-button { border:0; background:transparent; color:var(--text-muted); font-size:24px; cursor:pointer; }
.edit-dialog label { display:flex; flex-direction:column; gap:5px; margin-top:14px; font-size:11px; color:var(--text-muted); }
.edit-dialog input { width:100%; }.edit-dialog p { margin:13px 0; }.edit-dialog footer { justify-content:flex-end; margin-top:14px; }
@media (max-width:1100px) { th:nth-child(4),td:nth-child(4),th:nth-child(5),td:nth-child(5) { display:none; }.action-col { width:170px; } }
@media (max-width:920px) { .page-header { align-items:flex-start; flex-direction:column; }.header-actions { align-self:flex-end; }.toolbar-end { width:100%; justify-content:flex-end; }.action-col { width:160px; }.row-actions { gap:5px; } }
</style>
