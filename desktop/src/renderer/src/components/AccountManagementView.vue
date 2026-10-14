<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

/**
 * 账号管理（Step 11 · 多账号矩阵）
 * ─────────────────────────────────────────────────────────────
 * 数据链路：
 *  - CRUD/统计/分组/千川 → engine:run 拉起 engine/account_manager.py
 *  - 账号级浏览器/登录态 → window.api.account.*（一账号一分区 + 指纹 + 加密备份）
 *  - 千川登录态 → window.api.qianchuan.*（独立分区，多开隔离）
 */

// ---------------------------------------------------------------- 类型

interface AccountRow {
  id: number
  name: string
  platform: string
  douyin_id: string | null
  qianchuan_id: string | null
  category: string | null
  phone: string | null
  status: string
  nickname: string | null
  follower_count: number | null
  aweme_count: number | null
  remark: string | null
  created_at: string
  groups: string[]
  qc_bind_count: number
  material_count: number
  pending_publish: number
  publish_rate: number
  karl_rate: number
  ad_count: number
}

interface GroupRow {
  id: number
  name: string
  remark: string | null
  member_count: number
}

interface QcRow {
  id: number
  qc_name: string | null
  qc_id: string | null
  subject: string | null
  login_state: string
  last_check: string | null
  remark: string | null
  bind_count: number
  bound_account_ids: number[]
}

interface StatsRow {
  account_count: number
  grouped_count: number
  material_count: number
  pending_publish: number
  qianchuan_count: number
}

const DB_PATH = 'D:\\software\\投广客户端\\data\\ad_client.db'
const TABS = ['账号列表', '账号分组', '千川账户'] as const

// ---------------------------------------------------------------- 状态

const activeTab = ref<(typeof TABS)[number]>('账号列表')
const stats = ref<StatsRow | null>(null)
const accounts = ref<AccountRow[]>([])
const groups = ref<GroupRow[]>([])
const qcs = ref<QcRow[]>([])
const selected = ref<Set<number>>(new Set())
const busy = ref(false)
const notice = ref('')
const errorMsg = ref('')

// 新建/编辑账号表单
const accountForm = reactive({
  id: 0,
  name: '',
  douyin_id: '',
  qianchuan_id: '',
  category: '',
  phone: '',
  remark: '',
  status: 'active'
})
const showAccountForm = ref(false)

// 分组表单
const groupForm = reactive({ id: 0, name: '', remark: '' })
const showGroupForm = ref(false)

// 千川表单 + 绑定弹层
const qcForm = reactive({ id: 0, qc_name: '', qc_id: '', subject: '', remark: '' })
const showQcForm = ref(false)
const bindTarget = ref<QcRow | null>(null)
const bindSelected = ref<Set<number>>(new Set())
const showBindPanel = ref(false)

// 批量移动分组
const moveGroupId = ref<number | null>(null)

// 浏览器视图宿主 + 账号级登录态
const browserHost = ref<HTMLDivElement | null>(null)
const scrollEl = ref<HTMLDivElement | null>(null)
const viewState = ref<AccountViewState>({ open: false })
const loginMap = reactive<Record<number, AccountLoginState>>({})
const qcLoginMap = reactive<Record<number, AccountLoginState>>({})
let unsubscribers: Array<() => void> = []

// ---------------------------------------------------------------- 引擎调用

function runCli(args: string[]): Promise<Record<string, unknown>> {
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
      if (e.type === 'result') {
        settle(() => resolve((e.data ?? {}) as Record<string, unknown>))
      } else if (e.type === 'error') {
        settle(() => reject(new Error(e.message ?? '引擎返回错误')))
      }
    })
    window.setTimeout(() => settle(() => reject(new Error('命令超时（30 秒）'))), 30_000)
    void window.api.engine
      .run({ script: 'account_manager.py', args })
      .then((reply) => {
        if (!reply.taskId) settle(() => reject(new Error(reply.error ?? '命令启动失败')))
        else taskId = reply.taskId
      })
  })
}

async function withBusy<T>(fn: () => Promise<T>): Promise<T | undefined> {
  busy.value = true
  errorMsg.value = ''
  try {
    return await fn()
  } catch (error) {
    errorMsg.value = String(error instanceof Error ? error.message : error)
    return undefined
  } finally {
    busy.value = false
  }
}

// ---------------------------------------------------------------- 数据加载

async function loadStats(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'stats'])
  stats.value = data as unknown as StatsRow
}

async function loadAccounts(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'account-list'])
  accounts.value = (data.accounts ?? []) as AccountRow[]
}

async function loadGroups(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'group-list'])
  groups.value = (data.groups ?? []) as GroupRow[]
}

async function loadQcs(): Promise<void> {
  const data = await runCli(['--db', DB_PATH, 'qianchuan-list'])
  qcs.value = (data.qianchuan_accounts ?? []) as QcRow[]
}

async function loadAll(): Promise<void> {
  await withBusy(async () => {
    await Promise.all([loadStats(), loadAccounts(), loadGroups(), loadQcs()])
  })
}

// ---------------------------------------------------------------- 账号增删改查

function openAddAccount(): void {
  Object.assign(accountForm, {
    id: 0, name: '', douyin_id: '', qianchuan_id: '', category: '', phone: '', remark: '', status: 'active'
  })
  showAccountForm.value = true
}

function openEditAccount(row: AccountRow): void {
  Object.assign(accountForm, {
    id: row.id,
    name: row.name,
    douyin_id: row.douyin_id ?? '',
    qianchuan_id: row.qianchuan_id ?? '',
    category: row.category ?? '',
    phone: row.phone ?? '',
    remark: row.remark ?? '',
    status: row.status
  })
  showAccountForm.value = true
}

async function saveAccount(): Promise<void> {
  await withBusy(async () => {
    if (accountForm.id === 0) {
      await runCli([
        '--db', DB_PATH, 'account-add',
        '--name', accountForm.name,
        '--douyin-id', accountForm.douyin_id,
        '--qianchuan-id', accountForm.qianchuan_id,
        '--category', accountForm.category,
        '--phone', accountForm.phone,
        '--remark', accountForm.remark,
        '--status', accountForm.status
      ])
    } else {
      const args = ['--db', DB_PATH, 'account-update', '--id', String(accountForm.id)]
      if (accountForm.name) args.push('--name', accountForm.name)
      if (accountForm.douyin_id) args.push('--douyin-id', accountForm.douyin_id)
      if (accountForm.qianchuan_id) args.push('--qianchuan-id', accountForm.qianchuan_id)
      if (accountForm.category) args.push('--category', accountForm.category)
      if (accountForm.phone) args.push('--phone', accountForm.phone)
      if (accountForm.remark) args.push('--remark', accountForm.remark)
      if (accountForm.status) args.push('--status', accountForm.status)
      await runCli(args)
    }
    showAccountForm.value = false
    await Promise.all([loadAccounts(), loadStats()])
  })
}

async function deleteAccount(row: AccountRow): Promise<void> {
  if (!window.confirm(`确定删除账号「${row.name}」？其商品/视频/分组关联将一并删除。`)) return
  await withBusy(async () => {
    await runCli(['--db', DB_PATH, 'account-delete', '--id', String(row.id)])
    await Promise.all([loadAccounts(), loadStats()])
  })
}

async function toggleStatus(row: AccountRow): Promise<void> {
  const next = row.status === 'active' ? 'disabled' : 'active'
  await withBusy(async () => {
    await runCli(['--db', DB_PATH, 'account-set-status', '--id', String(row.id), '--status', next])
    await loadAccounts()
  })
}

// ---------------------------------------------------------------- 批量控制（FR-G6-01）

const selectedIds = computed(() => Array.from(selected.value))

function toggleSelect(id: number): void {
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
}

function toggleSelectAll(): void {
  if (selected.value.size === accounts.value.length) selected.value.clear()
  else accounts.value.forEach((a) => selected.value.add(a.id))
}

/** 批量启用/停用 */
async function batchSetStatus(status: 'active' | 'disabled'): Promise<void> {
  if (selectedIds.value.length === 0) return setNotice('请先勾选账号')
  await withBusy(async () => {
    for (const id of selectedIds.value) {
      await runCli(['--db', DB_PATH, 'account-set-status', '--id', String(id), '--status', status])
    }
    setNotice(`已批量${status === 'active' ? '启用' : '停用'} ${selectedIds.value.length} 个账号`)
    await loadAccounts()
  })
}

/** 批量移动分组 */
async function batchMoveGroup(): Promise<void> {
  if (selectedIds.value.length === 0) return setNotice('请先勾选账号')
  if (moveGroupId.value == null) return setNotice('请先选择目标分组')
  await withBusy(async () => {
    for (const id of selectedIds.value) {
      await runCli([
        '--db', DB_PATH, 'group-assign',
        '--account-id', String(id), '--group-id', String(moveGroupId.value)
      ])
    }
    setNotice(`已把 ${selectedIds.value.length} 个账号移入分组`)
    await Promise.all([loadAccounts(), loadGroups()])
  })
}

/** 批量删除 */
async function batchDelete(): Promise<void> {
  if (selectedIds.value.length === 0) return setNotice('请先勾选账号')
  if (!window.confirm(`确定删除选中的 ${selectedIds.value.length} 个账号？`)) return
  await withBusy(async () => {
    for (const id of selectedIds.value) {
      await runCli(['--db', DB_PATH, 'account-delete', '--id', String(id)])
    }
    selected.value.clear()
    setNotice(`已删除 ${selectedIds.value.length} 个账号`)
    await Promise.all([loadAccounts(), loadStats(), loadGroups()])
  })
}

/** 重置设置：清空勾选与筛选 */
function resetSettings(): void {
  selected.value.clear()
  moveGroupId.value = null
  setNotice('已重置勾选与批量设置')
}

/** 确认执行：对接批量执行引擎，当前占位提示 */
function confirmExecute(): void {
  setNotice('「确认执行」待接入批量执行引擎（后续模块）')
}

function setNotice(text: string): void {
  notice.value = text
  window.setTimeout(() => (notice.value = ''), 4000)
}

// ---------------------------------------------------------------- 分组管理

async function saveGroup(): Promise<void> {
  await withBusy(async () => {
    if (groupForm.id === 0) {
      await runCli(['--db', DB_PATH, 'group-add', '--name', groupForm.name, '--remark', groupForm.remark])
    } else {
      await runCli(['--db', DB_PATH, 'group-rename', '--id', String(groupForm.id), '--name', groupForm.name])
    }
    showGroupForm.value = false
    await Promise.all([loadGroups(), loadAccounts()])
  })
}

async function deleteGroup(row: GroupRow): Promise<void> {
  if (!window.confirm(`确定删除分组「${row.name}」？`)) return
  await withBusy(async () => {
    await runCli(['--db', DB_PATH, 'group-delete', '--id', String(row.id)])
    await loadGroups()
  })
}

async function autoGroup(): Promise<void> {
  await withBusy(async () => {
    const data = await runCli(['--db', DB_PATH, 'group-auto'])
    const r = data as { groups_created?: string[]; accounts_assigned?: number }
    setNotice(`自动分组完成：新建分组 ${r.groups_created?.length ?? 0} 个，归组账号 ${r.accounts_assigned ?? 0} 个`)
    await Promise.all([loadGroups(), loadAccounts(), loadStats()])
  })
}

/** 把单个账号移入/移出分组（分组行内快捷操作） */
async function assignToGroup(accountId: number, groupId: number | null): Promise<void> {
  if (groupId == null) return
  await withBusy(async () => {
    await runCli(['--db', DB_PATH, 'group-assign', '--account-id', String(accountId), '--group-id', String(groupId)])
    await Promise.all([loadAccounts(), loadGroups()])
  })
}

// ---------------------------------------------------------------- 千川账户

async function saveQc(): Promise<void> {
  await withBusy(async () => {
    if (qcForm.id === 0) {
      await runCli([
        '--db', DB_PATH, 'qianchuan-add',
        '--name', qcForm.qc_name, '--qc-id', qcForm.qc_id,
        '--subject', qcForm.subject, '--remark', qcForm.remark
      ])
    } else {
      const args = ['--db', DB_PATH, 'qianchuan-update', '--id', String(qcForm.id)]
      if (qcForm.qc_name) args.push('--name', qcForm.qc_name)
      if (qcForm.qc_id) args.push('--qc-id', qcForm.qc_id)
      if (qcForm.subject) args.push('--subject', qcForm.subject)
      if (qcForm.remark) args.push('--remark', qcForm.remark)
      await runCli(args)
    }
    showQcForm.value = false
    await loadQcs()
  })
}

async function deleteQc(row: QcRow): Promise<void> {
  if (!window.confirm(`确定删除千川账户「${row.qc_name ?? row.qc_id}」？`)) return
  await withBusy(async () => {
    await runCli(['--db', DB_PATH, 'qianchuan-delete', '--id', String(row.id)])
    await loadQcs()
  })
}

function openBindPanel(row: QcRow): void {
  bindTarget.value = row
  bindSelected.value = new Set(row.bound_account_ids ?? [])
  showBindPanel.value = true
}

async function saveBind(): Promise<void> {
  if (!bindTarget.value) return
  const target = bindTarget.value
  await withBusy(async () => {
    // 先解绑该千川已绑定的账号，再按勾选重新绑定（一个千川可关联多个抖音号）
    for (const id of target.bound_account_ids) {
      await runCli([
        '--db', DB_PATH, 'qianchuan-bind',
        '--qianchuan-id', String(target.id), '--account-id', String(id), '--unbind'
      ])
    }
    for (const id of bindSelected.value) {
      await runCli([
        '--db', DB_PATH, 'qianchuan-bind',
        '--qianchuan-id', String(target.id), '--account-id', String(id)
      ])
    }
    showBindPanel.value = false
    setNotice(`已保存千川「${target.qc_name ?? target.qc_id}」的账号绑定`)
    await Promise.all([loadQcs(), loadAccounts()])
  })
}

function copyText(text: string): void {
  void navigator.clipboard.writeText(text).then(
    () => setNotice(`已复制：${text}`),
    () => setNotice('复制失败（剪贴板不可用）')
  )
}

/** 登录千川 + 检查登录态（在线状态回写） */
async function openQcLogin(row: QcRow): Promise<void> {
  await withBusy(async () => {
    const r = await window.api.qianchuan.openLogin(row.id)
    if (!r.ok) throw new Error(r.error ?? '打开千川登录失败')
    const state = await window.api.qianchuan.checkLogin(row.id)
    qcLoginMap[row.id] = state
    const stateText = state.loggedIn ? 'online' : 'offline'
    await runCli([
      '--db', DB_PATH, 'qianchuan-update', '--id', String(row.id),
      '--login-state', stateText, '--last-check', new Date().toISOString()
    ])
    await loadQcs()
  })
}

async function checkQcLogin(row: QcRow): Promise<void> {
  await withBusy(async () => {
    const state = await window.api.qianchuan.checkLogin(row.id)
    qcLoginMap[row.id] = state
    await runCli([
      '--db', DB_PATH, 'qianchuan-update', '--id', String(row.id),
      '--login-state', state.loggedIn ? 'online' : 'offline',
      '--last-check', new Date().toISOString()
    ])
    await loadQcs()
  })
}

// ---------------------------------------------------------------- 账号级操作

function syncBounds(): void {
  const el = browserHost.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  window.api.account.setBounds({
    x: Math.round(rect.left),
    y: Math.round(rect.top),
    width: Math.round(rect.width),
    height: Math.round(rect.height)
  })
}

/** 页面滚动时若浏览器视图打开，重报位置让覆盖层跟随，避免错位 */
function onScroll(): void {
  if (viewState.value.open) syncBounds()
}

async function openBrowser(row: AccountRow): Promise<void> {
  await withBusy(async () => {
    syncBounds()
    const r = await window.api.account.openBrowser(row.id)
    if (!r.ok) throw new Error(r.error ?? '打开浏览器失败')
    viewState.value = await window.api.account.getState()
    const state = await window.api.account.checkLogin(row.id)
    loginMap[row.id] = state
    if (state.loggedIn) setNotice(`账号「${row.name}」已登录`)
  })
}

async function closeBrowser(): Promise<void> {
  await window.api.account.closeBrowser()
  viewState.value = await window.api.account.getState()
}

async function openJingxuan(row: AccountRow): Promise<void> {
  await withBusy(async () => {
    syncBounds()
    const r = await window.api.account.openJingxuan(row.id)
    if (!r.ok) throw new Error(r.error ?? '打开精选页失败')
    viewState.value = await window.api.account.getState()
  })
}

async function checkAccountLogin(row: AccountRow): Promise<void> {
  const state = await window.api.account.checkLogin(row.id)
  loginMap[row.id] = state
  setNotice(state.loggedIn ? `「${row.name}」已登录（cookie ${state.cookieCount} 条，已加密备份）` : `「${row.name}」未登录`)
}

async function pullInfo(row: AccountRow): Promise<void> {
  if (!row.douyin_id) return setNotice(`「${row.name}」未填抖音号，无法拉取`)
  await withBusy(async () => {
    syncBounds()
    setNotice(`正在从平台拉取「${row.name}」公开信息…`)
    const info = await window.api.account.pullInfo({ accountId: row.id, douyinId: row.douyin_id! })
    if (!info) {
      setNotice(`「${row.name}」拉取失败：主页未抓到用户数据（请确认该账号已登录且抖音号正确）`)
      return
    }
    if ('error' in info) throw new Error(info.error)
    const u = info as AccountUserInfo
    await runCli([
      '--db', DB_PATH, 'info-update', '--account-id', String(row.id),
      '--nickname', u.nickname,
      '--follower-count', String(u.followerCount),
      '--aweme-count', String(u.awemeCount)
    ])
    await loadAccounts()
    setNotice(`已写入「${row.name}」：昵称 ${u.nickname}，粉丝 ${u.followerCount}，作品 ${u.awemeCount}`)
  })
}

async function openMaterialFolder(row: AccountRow): Promise<void> {
  const r = await window.api.account.openMaterialFolder(row.name)
  if (!r.ok) setNotice(r.error ?? '打开素材文件夹失败')
}

// ---------------------------------------------------------------- 生命周期

onMounted(() => {
  void loadAll()
  const off1 = window.api.account.onStateChanged((s) => (viewState.value = s))
  const off2 = window.api.account.onLoginStateChanged((s) => {
    if (s.kind === 'acct') loginMap[s.id] = s
    else qcLoginMap[s.id] = s
  })
  const off3 = window.api.account.onNeedBounds(() => syncBounds())
  unsubscribers.push(off1, off2, off3)
  // 页面整体可滚动：滚动时若浏览器视图打开，重报 bounds 保持覆盖层跟随
  scrollEl.value?.addEventListener('scroll', onScroll)
})

onBeforeUnmount(() => {
  scrollEl.value?.removeEventListener('scroll', onScroll)
  unsubscribers.forEach((off) => off())
})

// ---------------------------------------------------------------- 展示辅助

function fmtCount(v: number | null | undefined): string {
  if (v == null) return '—'
  return String(v)
}

function statusText(row: AccountRow): string {
  return row.status === 'active' ? '启用' : '停用'
}

/** 千川绑定弹层的账号勾选（与表格勾选独立） */
function toggleSelectBind(id: number): void {
  if (bindSelected.value.has(id)) bindSelected.value.delete(id)
  else bindSelected.value.add(id)
}
</script>

<template>
  <div class="acct-view" ref="scrollEl">
    <!-- 全局统计卡 -->
    <div class="stats">
      <div class="stat-card"><div class="num">{{ stats?.account_count ?? 0 }}</div><div class="label">账号数</div></div>
      <div class="stat-card"><div class="num">{{ stats?.grouped_count ?? 0 }}</div><div class="label">已分组</div></div>
      <div class="stat-card"><div class="num">{{ stats?.material_count ?? 0 }}</div><div class="label">素材数</div></div>
      <div class="stat-card"><div class="num warn">{{ stats?.pending_publish ?? 0 }}</div><div class="label">待发布</div></div>
      <div class="stat-card"><div class="num">{{ stats?.qianchuan_count ?? 0 }}</div><div class="label">千川账户</div></div>
    </div>

    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="tabs">
        <button
          v-for="tab in TABS"
          :key="tab"
          type="button"
          class="tab"
          :class="{ active: activeTab === tab }"
          @click="activeTab = tab"
        >{{ tab }}</button>
      </div>
      <div class="spacer" />
      <button v-if="activeTab === '账号列表'" type="button" class="btn primary" :disabled="busy" @click="openAddAccount">新建账号</button>
        <button type="button" class="btn" :disabled="busy" @click="loadAll">刷新</button>
    </div>

    <div v-if="errorMsg" class="error-bar">{{ errorMsg }}</div>
    <div v-if="notice" class="notice-bar">{{ notice }}</div>

    <!-- ==================== 账号列表 Tab ==================== -->
    <template v-if="activeTab === '账号列表'">
      <!-- 批量控制条（FR-G6-01） -->
      <div class="batch-bar">
        <label class="check-all">
          <input type="checkbox" :checked="selected.size === accounts.length && accounts.length > 0" @change="toggleSelectAll" />
          全选
        </label>
        <button type="button" class="btn mini" @click="batchSetStatus('active')">批量启用</button>
        <button type="button" class="btn mini" @click="batchSetStatus('disabled')">批量停用</button>
        <select v-model.number="moveGroupId" class="mini-select">
          <option :value="null">移动到分组…</option>
          <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
        </select>
        <button type="button" class="btn mini" @click="batchMoveGroup">设置分组</button>
        <button type="button" class="btn mini" @click="batchDelete">批量删除</button>
        <button type="button" class="btn mini" @click="confirmExecute">确认执行</button>
        <button type="button" class="btn mini" @click="resetSettings">重置设置</button>
        <span class="batch-info">已选 {{ selected.size }} 个</span>
      </div>

      <!-- 账号表格 -->
      <div class="table-wrap">
        <table class="grid">
          <thead>
            <tr>
              <th style="width: 36px"></th>
              <th>名称</th>
              <th>抖音号</th>
              <th>千川ID</th>
              <th>运营分类</th>
              <th>手机号</th>
              <th>账号状态</th>
              <th>素材数</th>
              <th>计划发布率</th>
              <th>卡优率</th>
              <th>广告条数</th>
              <th>分组</th>
              <th>登录态</th>
              <th style="width: 330px">账号级操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in accounts" :key="row.id">
              <td><input type="checkbox" :checked="selected.has(row.id)" @change="toggleSelect(row.id)" /></td>
              <td>
                <div class="cell-main">{{ row.name }}</div>
                <div v-if="row.nickname" class="cell-sub">{{ row.nickname }} · 粉丝 {{ fmtCount(row.follower_count) }} · 作品 {{ fmtCount(row.aweme_count) }}</div>
              </td>
              <td>{{ row.douyin_id ?? '—' }}</td>
              <td>{{ row.qianchuan_id ?? '—' }}</td>
              <td>{{ row.category ?? '—' }}</td>
              <td>{{ row.phone ?? '—' }}</td>
              <td>
                <span class="tag" :class="row.status === 'active' ? 'ok' : 'muted'">{{ statusText(row) }}</span>
              </td>
              <td>{{ row.material_count }}</td>
              <td>{{ row.publish_rate }}</td>
              <td>{{ row.karl_rate }}</td>
              <td>{{ row.ad_count }}</td>
              <td>
                <span v-for="g in row.groups" :key="g" class="tag ok">{{ g }}</span>
                <span v-if="row.groups.length === 0" class="muted">未分组</span>
              </td>
              <td>
                <span class="tag" :class="loginMap[row.id]?.loggedIn ? 'ok' : 'muted'">
                  {{ loginMap[row.id] ? (loginMap[row.id]!.loggedIn ? '已登录' : '未登录') : '未知' }}
                </span>
              </td>
              <td class="ops">
                <button type="button" class="btn mini" @click="openBrowser(row)">打开浏览器</button>
                <button type="button" class="btn mini" @click="openJingxuan(row)">精选页</button>
                <button type="button" class="btn mini" @click="pullInfo(row)">拉取信息</button>
                <button type="button" class="btn mini" @click="openMaterialFolder(row)">素材夹</button>
                <button type="button" class="btn mini" @click="checkAccountLogin(row)">查登录态</button>
                <button type="button" class="btn mini" @click="toggleStatus(row)">{{ row.status === 'active' ? '停用' : '启用' }}</button>
                <button type="button" class="btn mini" @click="openEditAccount(row)">编辑</button>
                <button type="button" class="btn mini danger" @click="deleteAccount(row)">删除</button>
              </td>
            </tr>
            <tr v-if="accounts.length === 0">
              <td colspan="14" class="empty">暂无账号，点右上「新建账号」开始</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 浏览器视图宿主（覆盖图层，位置由本容器上报） -->
      <div v-if="viewState.open" class="browser-host" ref="browserHost">
        <div class="browser-bar">
          <span class="browser-title">{{ viewState.title || viewState.url }}</span>
          <button type="button" class="btn mini" @click="closeBrowser">关闭浏览器</button>
        </div>
      </div>
    </template>

    <!-- ==================== 账号分组 Tab ==================== -->
    <template v-else-if="activeTab === '账号分组'">
      <div class="batch-bar">
        <button type="button" class="btn mini" @click="groupForm.id = 0; groupForm.name = ''; groupForm.remark = ''; showGroupForm = true">新建分组</button>
        <button type="button" class="btn mini" @click="autoGroup">自动分组（按运营分类）</button>
      </div>
      <div class="group-grid">
        <div v-for="g in groups" :key="g.id" class="group-card">
          <div class="group-head">
            <strong>{{ g.name }}</strong>
            <span class="muted">{{ g.member_count }} 个账号</span>
          </div>
          <div class="group-ops">
            <button type="button" class="btn mini" @click="groupForm.id = g.id; groupForm.name = g.name; groupForm.remark = g.remark ?? ''; showGroupForm = true">重命名</button>
            <button type="button" class="btn mini danger" @click="deleteGroup(g)">删除</button>
          </div>
          <div class="group-members">
            <div v-for="a in accounts.filter((x) => x.groups.includes(g.name))" :key="a.id" class="member-row">
              <span>{{ a.name }}</span>
              <button type="button" class="btn mini" @click="assignToGroup(a.id, null)">移出</button>
            </div>
            <div v-if="accounts.filter((x) => x.groups.includes(g.name)).length === 0" class="muted">暂无账号</div>
          </div>
        </div>
      </div>
    </template>

    <!-- ==================== 千川账户 Tab ==================== -->
    <template v-else>
      <div class="batch-bar">
        <button type="button" class="btn mini" @click="qcForm.id = 0; qcForm.qc_name = ''; qcForm.qc_id = ''; qcForm.subject = ''; qcForm.remark = ''; showQcForm = true">新增千川账户</button>
      </div>
      <div class="table-wrap">
        <table class="grid">
          <thead>
            <tr>
              <th>千川账户名</th>
              <th>千川ID</th>
              <th>所属主体</th>
              <th>在线状态</th>
              <th>登录态</th>
              <th>绑定账号数</th>
              <th>最近校验</th>
              <th>备注</th>
              <th style="width: 300px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in qcs" :key="row.id">
              <td>{{ row.qc_name ?? '—' }}</td>
              <td>
                {{ row.qc_id ?? '—' }}
                <button v-if="row.qc_id" type="button" class="btn mini" @click="copyText(row.qc_id!)">复制</button>
              </td>
              <td>{{ row.subject ?? '—' }}</td>
              <td>
                <span class="tag" :class="row.login_state === 'online' ? 'ok' : 'muted'">{{ row.login_state === 'online' ? '在线' : '离线' }}</span>
              </td>
              <td>
                <span class="tag" :class="qcLoginMap[row.id]?.loggedIn ? 'ok' : 'muted'">
                  {{ qcLoginMap[row.id] ? (qcLoginMap[row.id]!.loggedIn ? '已登录' : '未登录') : '—' }}
                </span>
              </td>
              <td>{{ row.bind_count }}</td>
              <td class="muted">{{ row.last_check ? new Date(row.last_check).toLocaleString('zh-CN') : '—' }}</td>
              <td>{{ row.remark ?? '—' }}</td>
              <td class="ops">
                <button type="button" class="btn mini" @click="openQcLogin(row)">登录千川</button>
                <button type="button" class="btn mini" @click="checkQcLogin(row)">查状态</button>
                <button type="button" class="btn mini" @click="openBindPanel(row)">绑定账号</button>
                <button type="button" class="btn mini" @click="qcForm.id = row.id; qcForm.qc_name = row.qc_name ?? ''; qcForm.qc_id = row.qc_id ?? ''; qcForm.subject = row.subject ?? ''; qcForm.remark = row.remark ?? ''; showQcForm = true">编辑</button>
                <button type="button" class="btn mini danger" @click="deleteQc(row)">删除</button>
              </td>
            </tr>
            <tr v-if="qcs.length === 0">
              <td colspan="9" class="empty">暂无千川账户，点左上「新增千川账户」开始</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- ==================== 弹层：账号表单 ==================== -->
    <div v-if="showAccountForm" class="modal-mask" @click.self="showAccountForm = false">
      <div class="modal">
        <h3>{{ accountForm.id === 0 ? '新建账号' : '编辑账号' }}</h3>
        <div class="form-grid">
          <label>名称 *<input v-model="accountForm.name" placeholder="账号名（也是素材目录名）" /></label>
          <label>抖音号<input v-model="accountForm.douyin_id" placeholder="抖音号 / sec_uid" /></label>
          <label>千川ID<input v-model="accountForm.qianchuan_id" placeholder="快捷绑定千川ID" /></label>
          <label>运营分类<input v-model="accountForm.category" placeholder="如：美妆、食品" /></label>
          <label>绑定手机号<input v-model="accountForm.phone" /></label>
          <label>状态
            <select v-model="accountForm.status">
              <option value="active">启用</option>
              <option value="disabled">停用</option>
            </select>
          </label>
          <label class="full">备注<input v-model="accountForm.remark" /></label>
        </div>
        <div class="modal-ops">
          <button type="button" class="btn" @click="showAccountForm = false">取消</button>
          <button type="button" class="btn primary" :disabled="busy || !accountForm.name" @click="saveAccount">保存</button>
        </div>
      </div>
    </div>

    <!-- ==================== 弹层：分组表单 ==================== -->
    <div v-if="showGroupForm" class="modal-mask" @click.self="showGroupForm = false">
      <div class="modal">
        <h3>{{ groupForm.id === 0 ? '新建分组' : '重命名分组' }}</h3>
        <div class="form-grid">
          <label>分组名 *<input v-model="groupForm.name" /></label>
          <label>备注<input v-model="groupForm.remark" /></label>
        </div>
        <div class="modal-ops">
          <button type="button" class="btn" @click="showGroupForm = false">取消</button>
          <button type="button" class="btn primary" :disabled="busy || !groupForm.name" @click="saveGroup">保存</button>
        </div>
      </div>
    </div>

    <!-- ==================== 弹层：千川表单 ==================== -->
    <div v-if="showQcForm" class="modal-mask" @click.self="showQcForm = false">
      <div class="modal">
        <h3>{{ qcForm.id === 0 ? '新增千川账户' : '编辑千川账户' }}</h3>
        <div class="form-grid">
          <label>千川账户名<input v-model="qcForm.qc_name" /></label>
          <label>千川ID<input v-model="qcForm.qc_id" /></label>
          <label>所属主体<input v-model="qcForm.subject" /></label>
          <label>备注<input v-model="qcForm.remark" /></label>
        </div>
        <div class="modal-ops">
          <button type="button" class="btn" @click="showQcForm = false">取消</button>
          <button type="button" class="btn primary" :disabled="busy" @click="saveQc">保存</button>
        </div>
      </div>
    </div>

    <!-- ==================== 弹层：千川绑定账号 ==================== -->
    <div v-if="showBindPanel && bindTarget" class="modal-mask" @click.self="showBindPanel = false">
      <div class="modal">
        <h3>绑定账号到「{{ bindTarget.qc_name ?? bindTarget.qc_id ?? '千川账户' }}」（一个千川可关联多个抖音号）</h3>
        <div class="bind-list">
          <label v-for="a in accounts" :key="a.id" class="bind-item">
            <input type="checkbox" :checked="bindSelected.has(a.id)" @change="toggleSelectBind(a.id)" />
            {{ a.name }}（{{ a.douyin_id ?? '无抖音号' }}）
          </label>
        </div>
        <div class="modal-ops">
          <button type="button" class="btn" @click="showBindPanel = false">取消</button>
          <button type="button" class="btn primary" :disabled="busy" @click="saveBind">保存绑定</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.acct-view {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: var(--bg-app);
  /* 页面整体可滚动（修复「不能下滑」）；横向防溢出。
     浏览器视图打开时滚动会触发 onScroll 重报 bounds，覆盖层跟随不错位 */
  overflow-y: auto;
  overflow-x: hidden;
}

.stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  flex-shrink: 0; /* 内容超高时不被压缩，交给 .acct-view 溢出滚动 */
}

.stat-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 14px;
}

.stat-card .num {
  font-size: 24px;
  font-weight: 600;
  color: var(--brand);
}

.stat-card .num.warn {
  color: #e67e22;
}

.stat-card .label {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.tabs {
  display: flex;
  gap: 2px;
}

.tab {
  padding: 6px 14px;
  border-radius: var(--radius);
  color: var(--text-muted);
}

.tab.active {
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
}

.spacer {
  flex: 1;
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px;
}

.check-all {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-right: 4px;
  font-size: 13px;
}

.batch-info {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-muted);
}

.mini-select {
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #fff;
}

.table-wrap {
  /* 纵向滚动交给页面整体（.acct-view），这里只处理表格横向溢出 */
  flex: none;
  overflow-x: auto;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.grid th,
.grid td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  white-space: nowrap;
}

.grid th {
  position: sticky;
  top: 0;
  background: #f7f8fa;
  color: var(--text-muted);
  font-weight: 500;
  z-index: 1;
}

.grid tr:hover td {
  background: #fafbfc;
}

.cell-main {
  font-weight: 600;
}

.cell-sub {
  font-size: 12px;
  color: var(--text-muted);
}

.tag {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--bg-placeholder);
  color: var(--text-muted);
  margin-right: 3px;
}

.tag.ok {
  background: var(--brand-soft);
  color: var(--brand);
}

.tag.muted {
  background: var(--bg-placeholder);
  color: var(--text-muted);
}

.muted {
  color: var(--text-muted);
}

.ops {
  display: flex;
  gap: 4px;
}

.empty {
  text-align: center;
  color: var(--text-muted);
  padding: 30px 0;
}

.btn {
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: #fff;
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
}

.btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
}

.btn.primary {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}

.btn.mini {
  padding: 2px 7px;
  font-size: 12px;
}

.btn.danger {
  color: #d64545;
  border-color: #efd0d0;
}

.btn.danger:hover {
  border-color: #d64545;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.error-bar {
  background: #fdecec;
  color: #b33939;
  border: 1px solid #f2c4c4;
  border-radius: var(--radius);
  padding: 6px 10px;
  font-size: 13px;
  flex-shrink: 0;
}

.notice-bar {
  background: #eef6ff;
  color: #1e6fd9;
  border: 1px solid #cfe5ff;
  border-radius: var(--radius);
  padding: 6px 10px;
  font-size: 13px;
  flex-shrink: 0;
}

/* 浏览器视图宿主：覆盖图层的位置上报容器 */
.browser-host {
  height: 260px;
  flex-shrink: 0;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.browser-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 5px 10px;
  background: #f7f8fa;
  border-bottom: 1px solid var(--border);
}

.browser-title {
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 分组 */
.group-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
  align-content: start;
  flex: none; /* 不被压缩，内容超高时由 .acct-view 溢出滚动 */
}

.group-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px;
}

.group-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.group-ops {
  display: flex;
  gap: 4px;
  margin-bottom: 8px;
}

.group-members {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.member-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  padding: 2px 4px;
  border-radius: 4px;
}

.member-row:hover {
  background: #f7f8fa;
}

/* 弹层 */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: grid;
  place-items: center;
  z-index: 50;
}

.modal {
  background: #fff;
  border-radius: var(--radius);
  padding: 16px;
  min-width: 420px;
  max-width: 560px;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.15);
}

.modal h3 {
  margin: 0 0 12px;
  font-size: 15px;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: var(--text-muted);
}

.form-grid label.full {
  grid-column: 1 / -1;
}

.form-grid input,
.form-grid select {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 13px;
}

.modal-ops {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}

.bind-list {
  max-height: 300px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.bind-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
</style>
