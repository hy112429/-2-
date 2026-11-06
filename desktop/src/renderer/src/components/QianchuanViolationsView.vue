<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

interface Violation { remoteMaterialId: string; materialName: string; verdict: string; reason: string; detectedAt: string; deletable: boolean }
interface Item { id: number; remote_material_id: string; material_name: string; status: string; outcome_reason: string; screenshot_path: string }
interface Batch { id: number; status: string; manifest_path: string }
interface History { id: number; status: string; item_count: number; deleted_count: number; created_at: string }
const props = defineProps<{ qianchuanAccountId: number }>()
const rows = ref<Violation[]>([])
const selected = ref<Set<string>>(new Set())
const batch = ref<Batch | null>(null)
const items = ref<Item[]>([])
const pastBatches = ref<History[]>([])
const busy = ref(false)
const complete = ref(false)
const message = ref('')
const progress = ref('')
const page = ref(1)
const pageSize = ref(20)
const sizes = [10, 20, 30, 40, 50, 80, 100]
const pages = computed(() => Math.max(1, Math.ceil(rows.value.length / pageSize.value)))
const visible = computed(() => rows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const selectedIds = computed(() => rows.value.filter(row => row.deletable && selected.value.has(row.remoteMaterialId)).map(row => row.remoteMaterialId))
watch(() => props.qianchuanAccountId, () => {
  rows.value = []; selected.value = new Set(); batch.value = null; items.value = []; complete.value = false; page.value = 1
  pastBatches.value = []; if (props.qianchuanAccountId) void loadHistory()
}, { immediate: true })
watch(pageSize, () => { page.value = 1 })
function checked(data: Record<string, unknown>): Record<string, unknown> {
  if (data.ok === false) throw new Error(String(data.error || '千川操作失败'))
  return data
}
function toggle(id: string): void { const next = new Set(selected.value); if (next.has(id)) next.delete(id); else next.add(id); selected.value = next }
async function loadHistory(): Promise<void> {
  try {
    const data = checked(await window.api.qianchuan.violationHistory(props.qianchuanAccountId))
    pastBatches.value = Array.isArray(data.rows) ? data.rows as History[] : []
  } catch (error) { message.value = String(error) }
}
async function openBatch(id: number): Promise<void> {
  try { const data = checked(await window.api.qianchuan.violationBatch(id)); batch.value = data.batch as Batch; items.value = data.items as Item[] }
  catch (error) { message.value = String(error) }
}
async function scan(): Promise<void> {
  if (!props.qianchuanAccountId || busy.value) return
  busy.value = true; message.value = ''; selected.value = new Set(); batch.value = null; items.value = []
  try {
    const data = checked(await window.api.qianchuan.scanViolations(props.qianchuanAccountId))
    rows.value = Array.isArray(data.rows) ? data.rows as Violation[] : []
    complete.value = data.complete === true; page.value = 1
    message.value = `读取到 ${rows.value.length} 条劣质或违规素材。${complete.value ? '' : '列表未完整读取，删除已禁用。'}`
  } catch (error) { message.value = String(error) }
  finally { busy.value = false }
}
async function prepare(): Promise<void> {
  if (!complete.value || !selectedIds.value.length || busy.value) return
  busy.value = true
  try {
    const data = checked(await window.api.qianchuan.prepareViolationDeletion({ qianchuanAccountId: props.qianchuanAccountId, ids: selectedIds.value }))
    batch.value = data.batch as Batch; items.value = data.items as Item[]
    message.value = `已生成 ${items.value.length} 条删除清单，保存在本机；可导出 CSV。`
    await loadHistory()
  } catch (error) { message.value = String(error) }
  finally { busy.value = false }
}
async function exportManifest(): Promise<void> {
  if (!batch.value) return
  try { const result = checked(await window.api.qianchuan.exportViolationManifest(batch.value.id)); if (!result.cancelled) message.value = `清单已导出：${String(result.path || '')}` }
  catch (error) { message.value = String(error) }
}
async function execute(): Promise<void> {
  const current = batch.value
  if (!current || current.status !== 'prepared' || !items.value.length || busy.value) return
  const count = items.value.length
  if (!window.confirm(`将删除千川账户中清单所列 ${count} 条素材。请核对账户与清单；删除不可撤销。继续吗？`)) return
  const confirmation = window.prompt(`二次确认：请输入“删除 ${count} 条”`)
  if (confirmation !== `删除 ${count} 条`) { message.value = '确认文字不匹配，未执行。'; return }
  busy.value = true; progress.value = `准备处理 ${count} 条…`
  try {
    const result = checked(await window.api.qianchuan.executeViolationDeletion({ batchId: current.id, count, confirmation }))
    batch.value = result.batch as Batch; items.value = result.items as Item[]
    message.value = `已核验删除 ${items.value.filter(item => item.status === 'deleted').length}/${count} 条；其余请查看状态与原因。`
    await loadHistory()
  } catch (error) { message.value = String(error) }
  finally { busy.value = false; progress.value = '' }
}
function openScreenshot(path: string): void { void window.api.assets.openFolder(path) }
let stopProgress: (() => void) | null = window.api.qianchuan.onViolationProgress(event => {
  if (batch.value?.id === event.batchId) progress.value = `已处理 ${event.completed}/${event.total} 条${event.reason ? ` · ${event.reason}` : ''}`
})
onBeforeUnmount(() => { stopProgress?.(); stopProgress = null })
</script>

<template>
  <section class="violations">
    <header><div><h2>千川违规素材治理</h2><p>读取专属账户的违规素材；删除前生成并留存清单。</p></div><button :disabled="!qianchuanAccountId || busy" @click="scan">读取违规列表</button></header>
    <p v-if="message" class="notice">{{ message }}</p><p v-if="progress" class="notice" role="status">{{ progress }}</p>
    <div v-if="rows.length" class="table-scroll"><table><thead><tr><th>选择</th><th>素材与 ID</th><th>判定</th><th>原因</th><th>检测时间</th></tr></thead><tbody><tr v-for="row in visible" :key="row.remoteMaterialId || row.materialName"><td><input type="checkbox" :checked="selected.has(row.remoteMaterialId)" :disabled="!row.deletable || !complete || !!batch" @change="toggle(row.remoteMaterialId)"></td><td>{{ row.materialName || '名称未识别' }}<small>{{ row.remoteMaterialId || 'ID 未识别，仅供查看' }}</small></td><td>{{ row.verdict }}</td><td>{{ row.reason || '页面未显示' }}</td><td>{{ row.detectedAt || '页面未显示' }}</td></tr></tbody></table></div>
    <div v-if="rows.length" class="actions"><span>已选 {{ selectedIds.length }} 条</span><label>每页 <select v-model.number="pageSize"><option v-for="size in sizes" :key="size" :value="size">{{ size }}</option></select></label><button :disabled="page <= 1" @click="page--">上一页</button><span>{{ page }}/{{ pages }}</span><button :disabled="page >= pages" @click="page++">下一页</button><button :disabled="busy || !!batch || !complete || !selectedIds.length || selectedIds.length > 50" @click="prepare">生成删除清单</button></div>
    <div v-if="batch" class="manifest"><h3>删除批次 #{{ batch.id }} · {{ items.length }} 条</h3><p>本机清单：{{ batch.manifest_path }}</p><div class="actions"><button @click="exportManifest">导出 CSV</button><button class="danger" :disabled="busy || batch.status !== 'prepared'" @click="execute">分批删除 {{ items.length }} 条</button></div><ul><li v-for="item in items" :key="item.id">{{ item.material_name }}（{{ item.remote_material_id }}）· {{ item.status }}<span v-if="item.outcome_reason"> · {{ item.outcome_reason }}</span><button v-if="item.screenshot_path" @click="openScreenshot(item.screenshot_path)">查看截图</button></li></ul></div>
    <details v-if="pastBatches.length" class="manifest"><summary>历史删除批次（{{ pastBatches.length }}）</summary><ul><li v-for="old in pastBatches" :key="old.id"><button @click="openBatch(old.id)">#{{ old.id }} · {{ old.status }} · 已删 {{ old.deleted_count || 0 }}/{{ old.item_count }} · {{ old.created_at }}</button></li></ul></details>
  </section>
</template>

<style scoped>
.violations{padding:14px;border:1px solid #e1e6ee;border-radius:7px;background:#fff;color:#263249;min-width:0;overflow:auto}.violations header,.actions{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}.violations h2{margin:0;font-size:16px}.violations p{font-size:12px;color:#6c788b;overflow-wrap:anywhere}.violations button,.violations select{min-height:30px;padding:4px 9px;border:1px solid #d8dee8;border-radius:5px;background:#fff;color:#263249;cursor:pointer}.violations button:disabled{opacity:.5;cursor:not-allowed}.violations .danger{background:#bd3939;color:#fff;border-color:#bd3939}.notice{padding:9px;background:#edf4ff}.table-scroll{max-height:400px;overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}th,td{text-align:left;padding:8px;border-bottom:1px solid #edf0f4;vertical-align:top;overflow-wrap:anywhere}th:first-child{width:56px}th:nth-child(2){width:27%}th:nth-child(3){width:14%}th:last-child{width:130px}td small{display:block;color:#7a8797}.actions{margin-top:10px;font-size:12px}.manifest{margin-top:14px;border-top:1px solid #edf0f4}.manifest h3{font-size:14px}.manifest ul{max-height:200px;overflow:auto;font-size:12px}
.violations { border-radius: var(--radius); box-shadow: var(--shadow-card); }
.violations header { padding-bottom: 10px; border-bottom: 1px solid var(--border); }
.violations button:hover:not(:disabled) { border-color: var(--brand); color: var(--brand); background: var(--brand-soft); }
.violations .danger:hover:not(:disabled) { border-color: #a92c2c; color: #fff; background: #a92c2c; }
</style>
