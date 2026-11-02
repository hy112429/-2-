<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { notifyGlobal } from '../utils/notifications'

/**
 * 本地素材库（「素材与工具」导航）
 *
 * 数据链路：engine:run 拉起 engine/asset_library.py（scan/list/delete/tags/...），
 * 缩略图经主进程 assets:getThumb 转 base64 dataURL 显示。
 */

// ---------------------------------------------------------------- 类型

interface AssetRow {
  id: number
  file_path: string
  file_name: string
  file_size: number
  product_id: string | null
  video_id: string | null
  account: string | null
  account_id: number | null
  source_type: 'web_download' | 'detail_generated' | 'unknown'
  category: string
  status: string
  thumb_path: string | null
  tags: string[]
}

interface AccountAssetCount {
  account_key: string
  count: number
}

interface AccountOption { account_id: number; account_name: string }

const DB_PATH = 'D:\\software\\投广客户端\\data\\ad_client.db'
const ROOT = 'D:\\software\\投广客户端\\downloads'

// ---------------------------------------------------------------- 状态

const assets = ref<AssetRow[]>([])
const accountNames = ref<string[]>([])
const accountCounts = ref<AccountAssetCount[]>([])
const accountOptions = ref<AccountOption[]>([])
const chosenAccountId = ref(0)
const tags = ref<string[]>([])
const categories = ref<string[]>([])
const filter = reactive({ account: '', productId: '', tag: '', date: '', status: 'ready' })
const selected = ref<Set<number>>(new Set())
const viewMode = ref<'all' | 'account'>('all')
const scanning = ref(false)
const notice = ref('')
const thumbMap = reactive<Record<number, string>>({})
const UNASSIGNED_ACCOUNT = '__unassigned__'

const groupedAssets = computed(() => {
  const grouped = new Map<string, AssetRow[]>()
  for (const asset of assets.value) {
    const key = asset.account?.trim() || UNASSIGNED_ACCOUNT
    const rows = grouped.get(key) ?? []
    rows.push(asset)
    grouped.set(key, rows)
  }
  const counts = new Map(accountCounts.value.map((item) => [item.account_key, item.count]))
  return [...grouped.entries()]
    .sort(([left], [right]) => {
      if (left === UNASSIGNED_ACCOUNT) return 1
      if (right === UNASSIGNED_ACCOUNT) return -1
      return left.localeCompare(right, 'zh-CN')
    })
    .map(([key, rows]) => ({
      key,
      label: key === UNASSIGNED_ACCOUNT ? '未归属账号' : key,
      rows,
      count: counts.get(key) ?? rows.length
    }))
})

// ---------------------------------------------------------------- 引擎调用

/** 跑 asset_library.py，返回 result.data；stage 事件实时回调 */
function runCli(args: string[], onStage?: (s: string, p: number) => void): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let settled = false
    let taskId: string | null = null
    let off: () => void = () => undefined
    const settle = (fn: () => void): void => {
      if (settled) return
      settled = true
      off()
      fn()
    }
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'stage') {
        onStage?.(e.stage ?? '', e.percent ?? 0)
      } else if (e.type === 'result') {
        settle(() => resolve((e.data ?? {}) as Record<string, unknown>))
      } else if (e.type === 'error') {
        settle(() => reject(new Error(e.message ?? '引擎返回错误')))
      }
    })
    window.setTimeout(() => settle(() => reject(new Error('素材库命令超时（30 秒）'))), 30_000)
    void window.api.engine
      .run({ script: 'asset_library.py', args })
      .then((reply) => {
        if (!reply.taskId) settle(() => reject(new Error(reply.error ?? '命令启动失败')))
        else taskId = reply.taskId
      })
  })
}

// ---------------------------------------------------------------- 数据加载

async function loadList(): Promise<void> {
  const args = ['--db', DB_PATH, '--root', ROOT, 'list', '--limit', '500']
  if (filter.account) args.push('--account', filter.account)
  if (filter.productId.trim()) args.push('--product-id', filter.productId.trim())
  if (filter.tag) args.push('--tag', filter.tag)
  if (filter.date.trim()) args.push('--date', filter.date.trim())
  if (filter.status) args.push('--status', filter.status)
  const data = await runCli(args)
  assets.value = (data.rows ?? []) as AssetRow[]
  accountCounts.value = (data.account_counts ?? []) as AccountAssetCount[]
  await preloadThumbs()
}

async function loadAccountNames(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'accounts'])
  accountNames.value = (data.accounts ?? []) as string[]
}

async function loadAccountOptions(): Promise<void> {
  const result = await window.api.qianchuan.uploadTargets()
  if (result.ok === false) return
  const targets = Array.isArray(result.rows) ? result.rows as AccountOption[] : []
  accountOptions.value = [...new Map(targets.map((row) => [row.account_id, row])).values()]
}

async function loadTags(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'tags'])
  tags.value = (data.tags ?? []) as string[]
}

async function loadCategories(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'categories'])
  categories.value = (data.categories ?? []) as string[]
}

/** 预加载可见素材的缩略图（reactive map，避免大列表全量 IO） */
async function preloadThumbs(): Promise<void> {
  const batch = assets.value.slice(0, 80).filter((a) => a.thumb_path && !thumbMap[a.id])
  for (const a of batch) {
    const url = await window.api.assets.getThumb(a.thumb_path!)
    if (url) thumbMap[a.id] = url
  }
}

async function scan(): Promise<void> {
  scanning.value = true
  notice.value = ''
  try {
    const data = await runCli(['--db', DB_PATH, '--root', ROOT, 'scan'], (stage, p) => {
      notice.value = `${stage}（${p}%）`
    })
    notice.value = `扫描完成：新增 ${data.added ?? 0}，更新 ${data.updated ?? 0}，缩略图 ${data.thumbs_ok ?? 0}，清理失效 ${data.removed_missing ?? 0}`
    await Promise.all([loadList(), loadAccountNames(), loadAccountOptions(), loadTags(), loadCategories()])
    notifyAssetChanged()
    notifyGlobal(notice.value, 'success')
  } catch (err) {
    notice.value = `扫描失败：${String(err)}`
    notifyGlobal(notice.value, 'error')
  } finally {
    scanning.value = false
  }
}

/** 广播素材库变更（供底部状态栏刷新记录数） */
function notifyAssetChanged(): void {
  window.dispatchEvent(new CustomEvent('asset-library-changed'))
}

async function refresh(): Promise<void> {
  await Promise.all([loadList(), loadAccountNames(), loadAccountOptions(), loadTags(), loadCategories()])
}

/** 网页下载流程不携带唯一抖音账号时，由操作人一次性确认素材归属。 */
async function assignSelectedAccount(): Promise<void> {
  if (!chosenAccountId.value || !selected.value.size) return
  let assigned = 0
  const errors: string[] = []
  for (const id of selected.value) {
    try {
      const result = await runCli(['--db', DB_PATH, 'assign-account', '--id', String(id), '--account-id', String(chosenAccountId.value)])
      if (result.ok) assigned += 1
      else errors.push(`#${id} ${String(result.error ?? '归属失败')}`)
    } catch (error) { errors.push(`#${id} ${String(error)}`) }
  }
  selected.value = new Set()
  await refresh()
  notice.value = `账号归属已确认 ${assigned} 条${errors.length ? `；失败 ${errors.length} 条：${errors[0]}` : ''}。来源未知的素材仍不能自动上传。`
  notifyGlobal(notice.value, errors.length ? 'error' : 'success')
}

/** 旧素材没有可靠下载记录时，仅允许用户明确核对后标记网页来源。 */
async function confirmSelectedWebSource(): Promise<void> {
  if (!chosenAccountId.value || !selected.value.size) return
  if (!window.confirm('请确认所选 MP4 都是从网页下载的素材，不含商品详情图生成视频。确认后将同时登记所属账号。')) return
  let registered = 0
  const errors: string[] = []
  for (const id of selected.value) {
    const row = assets.value.find((asset) => asset.id === id)
    if (!row) continue
    const args = ['--db', DB_PATH, 'register', '--file-path', row.file_path,
      '--source-type', 'web_download', '--account-id', String(chosenAccountId.value)]
    if (row.product_id) args.push('--product-id', row.product_id)
    try {
      const result = await runCli(args)
      if (result.ok) registered += 1
      else errors.push(`#${id} ${String(result.error ?? '来源确认失败')}`)
    } catch (error) { errors.push(`#${id} ${String(error)}`) }
  }
  selected.value = new Set()
  await refresh()
  notice.value = `已确认网页下载来源 ${registered} 条${errors.length ? `；失败 ${errors.length} 条：${errors[0]}` : ''}。`
  notifyGlobal(notice.value, errors.length ? 'error' : 'success')
}

// ---------------------------------------------------------------- 检索

function applyFilter(): void {
  selected.value = new Set()
  void refresh()
}

// ---------------------------------------------------------------- 删除（二次确认）

async function removeSelected(): Promise<void> {
  if (selected.value.size === 0) return
  const ok = window.confirm(
    `确定删除选中的 ${selected.value.size} 个素材吗？\n对应的文件也会被永久删除。`
  )
  if (!ok) return
  notice.value = ''
  let deleted = 0
  let failed = 0
  let firstFailure = ''
  for (const id of selected.value) {
    try {
      const data = await runCli(['--db', DB_PATH, '--root', ROOT, 'delete', '--id', String(id)])
      if (data.ok) deleted += 1
      else {
        failed += 1
        firstFailure ||= String(data.error ?? '未知')
      }
    } catch (err) {
      failed += 1
      firstFailure ||= String(err)
    }
  }
  selected.value = new Set()
  await refresh()
  if (failed > 0) {
    notice.value = `删除完成：成功 ${deleted} 项，失败 ${failed} 项。首个失败原因：${firstFailure}`
    notifyGlobal(notice.value, 'error')
  }
  if (deleted > 0) {
    if (failed === 0) notice.value = `已删除 ${deleted} 个素材（文件已移除）`
    notifyAssetChanged()
    if (failed === 0) notifyGlobal(notice.value, 'success')
  }
}

function toggleRow(id: number): void {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function toggleAll(): void {
  const all = assets.value.map((a) => a.id)
  const allSelected = all.every((id) => selected.value.has(id))
  selected.value = allSelected ? new Set() : new Set(all)
}

// ---------------------------------------------------------------- 标签 / 打开文件夹

async function editTags(row: AssetRow): Promise<void> {
  const input = window.prompt('设置标签（逗号分隔）：', row.tags.join(','))
  if (input === null) return
  await runCli(['--db', DB_PATH, 'tag-set', '--id', String(row.id), '--tags', input])
  await refresh()
}

function openFolder(row: AssetRow): void {
  void window.api.assets.openFolder(row.file_path)
}

// ---------------------------------------------------------------- 格式化

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// ---------------------------------------------------------------- 生命周期

let offEngine: (() => void) | undefined
onMounted(async () => {
  await refresh()
})
onBeforeUnmount(() => {
  offEngine?.()
})
</script>

<template>
  <section class="lib">
    <!-- ---------- 工具栏 ---------- -->
    <div class="toolbar">
      <label class="fld">
        <span class="lbl">账号</span>
        <select v-model="filter.account" class="input account-input" @change="applyFilter">
          <option value="">全部账号</option>
          <option v-for="name in accountNames" :key="name" :value="name">{{ name }}</option>
          <option :value="UNASSIGNED_ACCOUNT">未归属账号</option>
        </select>
      </label>

      <label class="fld">
        <span class="lbl">商品 ID</span>
        <input v-model="filter.productId" class="input id-input" placeholder="如 7076521897453267950" @keyup.enter="applyFilter" />
      </label>

      <label class="fld">
        <span class="lbl">标签</span>
        <select v-model="filter.tag" class="input" @change="applyFilter">
          <option value="">全部</option>
          <option v-for="t in tags" :key="t" :value="t">{{ t }}</option>
        </select>
      </label>

      <label class="fld">
        <span class="lbl">日期</span>
        <input v-model="filter.date" class="input date-input" type="date" @change="applyFilter" />
      </label>

      <label class="fld">
        <span class="lbl">状态</span>
        <select v-model="filter.status" class="input" @change="applyFilter">
          <option value="ready">已就绪</option>
          <option value="archived">已归档</option>
        </select>
      </label>

      <button class="btn primary" :disabled="scanning" @click="scan">
        {{ scanning ? '扫描中…' : '重新扫描目录' }}
      </button>
      <button class="btn" @click="refresh">刷新</button>
      <select v-model.number="chosenAccountId" class="input account-input" aria-label="为所选素材指定抖音账号">
        <option :value="0">指定所属账号…</option>
        <option v-for="account in accountOptions" :key="account.account_id" :value="account.account_id">{{ account.account_name }}</option>
      </select>
      <button class="btn" :disabled="selected.size === 0 || !chosenAccountId" @click="assignSelectedAccount">确认所选归属</button>
      <button class="btn" :disabled="selected.size === 0 || !chosenAccountId" @click="confirmSelectedWebSource">确认旧素材网页来源</button>
      <button class="btn danger" :disabled="selected.size === 0" @click="removeSelected">
        删除所选（{{ selected.size }}）
      </button>
    </div>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <div class="view-toolbar">
      <div class="view-switch" role="group" aria-label="素材展示方式">
        <button type="button" :class="{ active: viewMode === 'all' }" @click="viewMode = 'all'">全部素材</button>
        <button type="button" :class="{ active: viewMode === 'account' }" @click="viewMode = 'account'">按账号分组</button>
      </div>
      <span class="muted">{{ filter.account ? '当前账号筛选结果' : '当前筛选结果' }}：{{ assets.length }} 个素材</span>
    </div>

    <!-- ---------- 素材网格 ---------- -->
    <div class="grid-wrap">
      <div v-if="assets.length === 0" class="empty">
        <p>当前筛选条件下没有素材。清除筛选后可重新查看，或点「重新扫描目录」登记下载目录里的成品素材。</p>
        <p class="sub">约定目录：{下载目录}/{账号}/{日期}/{商品ID}/{文件}.mp4</p>
      </div>

      <div v-else-if="viewMode === 'all'" class="grid">
        <div v-for="a in assets" :key="a.id" class="card" :class="{ selected: selected.has(a.id) }">
          <div class="cover" @click="toggleRow(a.id)">
            <img v-if="thumbMap[a.id]" :src="thumbMap[a.id]" alt="" />
            <div v-else class="cover-ph">{{ a.file_name.slice(-3) }}</div>
          </div>

          <div class="meta">
            <div class="name" :title="a.file_name">{{ a.file_name }}</div>
            <div class="owner">账号：{{ a.account?.trim() || '未归属账号' }}</div>
            <div class="line muted">来源：{{ a.source_type === 'web_download' ? '网页下载' : a.source_type === 'detail_generated' ? '详情图生成' : '待确认' }}</div>
            <div class="line">
              <span v-if="a.category !== (a.account?.trim() || '')" class="badge">{{ a.category }}</span>
              <span v-if="a.product_id" class="prod mono" :title="a.product_id">ID {{ a.product_id }}</span>
            </div>
            <div v-if="a.tags.length" class="tags">
              <span v-for="t in a.tags" :key="t" class="tag">{{ t }}</span>
            </div>
            <div class="line muted">{{ fmtSize(a.file_size) }} · {{ a.status }}</div>
          </div>

          <div class="ops">
            <input type="checkbox" :checked="selected.has(a.id)" @change="toggleRow(a.id)" />
            <button class="btn small" title="打开所在文件夹" @click="openFolder(a)">📁</button>
            <button class="btn small" title="设置标签" @click="editTags(a)">🏷</button>
          </div>
        </div>
      </div>

      <div v-else class="account-groups">
        <section v-for="group in groupedAssets" :key="group.key" class="account-group">
          <header class="account-group-head">
            <h3>{{ group.label }}</h3>
            <span>{{ group.count }} 个素材</span>
          </header>
          <div class="grid">
            <div v-for="a in group.rows" :key="a.id" class="card" :class="{ selected: selected.has(a.id) }">
              <div class="cover" @click="toggleRow(a.id)">
                <img v-if="thumbMap[a.id]" :src="thumbMap[a.id]" alt="" />
                <div v-else class="cover-ph">{{ a.file_name.slice(-3) }}</div>
              </div>
              <div class="meta">
                <div class="name" :title="a.file_name">{{ a.file_name }}</div>
                <div class="owner">账号：{{ a.account?.trim() || '未归属账号' }}</div>
                <div class="line muted">来源：{{ a.source_type === 'web_download' ? '网页下载' : a.source_type === 'detail_generated' ? '详情图生成' : '待确认' }}</div>
                <div class="line">
                  <span v-if="a.category !== (a.account?.trim() || '')" class="badge">{{ a.category }}</span>
                  <span v-if="a.product_id" class="prod mono" :title="a.product_id">ID {{ a.product_id }}</span>
                </div>
                <div v-if="a.tags.length" class="tags">
                  <span v-for="t in a.tags" :key="t" class="tag">{{ t }}</span>
                </div>
                <div class="line muted">{{ fmtSize(a.file_size) }} · {{ a.status }}</div>
              </div>
              <div class="ops">
                <input type="checkbox" :checked="selected.has(a.id)" @change="toggleRow(a.id)" />
                <button class="btn small" title="打开所在文件夹" @click="openFolder(a)">📁</button>
                <button class="btn small" title="设置标签" @click="editTags(a)">🏷</button>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </section>
</template>

<style scoped>
.lib {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
  overflow: hidden;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  flex-shrink: 0;
}

.fld {
  display: flex;
  align-items: center;
  gap: 6px;
}

.lbl {
  color: var(--text-muted);
  font-size: 13px;
}

.input {
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
}

.id-input {
  width: 220px;
}

.date-input {
  width: 130px;
}

.account-input {
  min-width: 150px;
  max-width: 220px;
}

.btn {
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-status);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
}

.btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn.primary {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}

.btn.primary:hover:not(:disabled) {
  background: #2f6fd8;
}

.btn.danger {
  border-color: #dc2626;
  color: #dc2626;
}

.btn.small {
  height: 22px;
  padding: 0 6px;
  font-size: 12px;
}

.notice {
  margin: 0;
  padding: 6px 10px;
  background: #fff8e6;
  border: 1px solid #f0d9a0;
  border-radius: var(--radius);
  color: #7a5a10;
  font-size: 12px;
  flex-shrink: 0;
}

.grid-wrap {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.empty {
  padding: 40px;
  text-align: center;
  color: var(--text-muted);
}

.empty .sub {
  font-size: 12px;
  opacity: 0.8;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 10px;
}

.view-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-shrink: 0;
}

.view-switch {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-status);
}

.view-switch button {
  border: 0;
  border-radius: calc(var(--radius) - 2px);
  padding: 5px 10px;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.view-switch button.active {
  background: var(--brand);
  color: #fff;
}

.account-groups {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.account-group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

.account-group-head h3 {
  margin: 0;
  color: var(--text);
  font-size: 14px;
}

.account-group-head span,
.owner {
  color: var(--text-muted);
  font-size: 11px;
}

.card {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-panel);
  display: flex;
  flex-direction: column;
}

.card.selected {
  border-color: var(--brand);
  box-shadow: 0 0 0 1px var(--brand);
}

.cover {
  aspect-ratio: 16 / 9;
  background: var(--bg-placeholder);
  cursor: pointer;
  display: grid;
  place-items: center;
  overflow: hidden;
}

.cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.cover-ph {
  color: var(--text-muted);
  font-size: 18px;
  text-transform: uppercase;
}

.meta {
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
}

.name {
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.badge {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 11px;
}

.prod {
  color: var(--text-muted);
  font-size: 11px;
}

.muted {
  color: var(--text-muted);
}

.tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.tag {
  padding: 0 5px;
  border-radius: 4px;
  background: var(--bg-placeholder);
  color: var(--text-muted);
  font-size: 11px;
}

.ops {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px 6px;
  border-top: 1px solid var(--border);
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
}
</style>
