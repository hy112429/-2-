<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

interface PlanJob {
  id: number
  product_id: string
  plan_name: string
  status: string
  budget_fen: number
  remote_plan_id: string
  reason: string
  updated_at: string
}

const props = defineProps<{ qianchuanAccountId: number }>()
const loading = ref(false)
const saving = ref(false)
const enabled = ref(false)
const dailyBudgetYuan = ref<number | null>(null)
const materialSource = ref<'homepage' | 'uploaded' | 'both'>('homepage')
const smartCoupon = ref(true)
const aigcEnabled = ref(false)
const jobs = ref<PlanJob[]>([])
const message = ref('')
let timer: ReturnType<typeof setInterval> | null = null

function checked(result: Record<string, unknown>): Record<string, unknown> {
  if (result.ok === false) throw new Error(String(result.error ?? '读取计划配置失败'))
  return result
}

async function load(): Promise<void> {
  if (!props.qianchuanAccountId) { jobs.value = []; return }
  loading.value = true
  try {
    const [config, result] = await Promise.all([
      window.api.qianchuan.planConfig(props.qianchuanAccountId),
      window.api.qianchuan.planJobs(props.qianchuanAccountId)
    ])
    const row = checked(config).settings as Record<string, unknown>
    enabled.value = Number(row.enabled) === 1
    dailyBudgetYuan.value = row.daily_budget_fen == null ? null : Number(row.daily_budget_fen) / 100
    materialSource.value = (row.material_source as typeof materialSource.value) ?? 'homepage'
    smartCoupon.value = Number(row.smart_coupon) === 1
    aigcEnabled.value = Number(row.aigc_enabled) === 1
    jobs.value = Array.isArray(checked(result).rows) ? checked(result).rows as PlanJob[] : []
    message.value = ''
  } catch (error) { message.value = String(error) }
  finally { loading.value = false }
}

async function refreshJobs(): Promise<void> {
  if (!props.qianchuanAccountId || loading.value || saving.value) return
  try {
    const result = checked(await window.api.qianchuan.planJobs(props.qianchuanAccountId))
    jobs.value = Array.isArray(result.rows) ? result.rows as PlanJob[] : []
  } catch { /* 页面下一次加载时提示读取错误。 */ }
}

async function save(): Promise<void> {
  const budget = dailyBudgetYuan.value
  if (!props.qianchuanAccountId || saving.value) return
  if (budget == null || !Number.isFinite(budget) || budget <= 0 || Math.round(budget * 100) / 100 !== budget) {
    message.value = '请填写该千川账户的日预算金额，最多两位小数。'
    return
  }
  saving.value = true
  try {
    checked(await window.api.qianchuan.savePlanConfig({
      qianchuanAccountId: props.qianchuanAccountId,
      enabled: enabled.value,
      dailyBudgetYuan: budget,
      materialSource: materialSource.value,
      smartCoupon: smartCoupon.value,
      aigcEnabled: aigcEnabled.value
    }))
    message.value = enabled.value
      ? '已保存。仅对启用后新确认发布的作品，待所选素材就绪时自动创建并发布计划。'
      : '已保存配置，自动创建计划保持关闭。'
  } catch (error) { message.value = String(error) }
  finally { saving.value = false }
}

async function retrySafe(id: number): Promise<void> {
  try {
    checked(await window.api.qianchuan.retrySafePlan(id))
    message.value = `计划任务 #${id} 已重排，等待专属浏览器执行。`
    await refreshJobs()
  } catch (error) { message.value = String(error) }
}

async function reviewPresent(id: number): Promise<void> {
  const remotePlanId = window.prompt('请先在对应千川广告主账户核实计划已发布，再输入页面显示的计划 ID。')
  if (remotePlanId == null) return
  try {
    checked(await window.api.qianchuan.reviewPlanPresent({ id, remotePlanId }))
    message.value = `计划任务 #${id} 已登记远端计划 ID。`
    await refreshJobs()
  } catch (error) { message.value = String(error) }
}

async function reviewAbsent(id: number): Promise<void> {
  const note = window.prompt('请先在对应千川广告主账户按商品和计划名核实确实没有创建计划，再写至少 6 字核查依据。')
  if (note == null) return
  try {
    checked(await window.api.qianchuan.reviewPlanAbsent({ id, note }))
    message.value = `计划任务 #${id} 已按人工核查结论重排。`
    await refreshJobs()
  } catch (error) { message.value = String(error) }
}

function statusLabel(value: string): string {
  return ({ waiting_material: '等待素材', queued: '待执行', creating: '创建中',
    succeeded: '已发布', failed: '失败', needs_review: '待核查', blocked_config: '配置已变更' } as Record<string, string>)[value] ?? value
}

watch(() => props.qianchuanAccountId, () => { void load() })
onMounted(() => {
  void load()
  timer = setInterval(() => { void refreshJobs() }, 10_000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <details class="plan-settings">
    <summary>商品全域投放计划 · 自动创建与发布</summary>
    <p class="note">按千川账户配置日预算。默认采用放量投放、从今天起长期投放、智能优惠券开启、AIGC 关闭。新账户默认关闭自动计划；没有预算时不会发布。</p>
    <p v-if="!qianchuanAccountId" class="note">请先选择千川账户。</p>
    <template v-else>
      <div class="form">
        <label>日预算（元）<input v-model.number="dailyBudgetYuan" type="number" min="0.01" step="0.01" placeholder="按账户填写" /></label>
        <label>视频来源<select v-model="materialSource"><option value="homepage">已发布的抖音主页作品</option><option value="uploaded">Step 17 已上传 MP4</option><option value="both">两种来源都选</option></select></label>
        <label class="check"><input v-model="smartCoupon" type="checkbox" /> 智能优惠券</label>
        <label class="check"><input v-model="aigcEnabled" type="checkbox" /> AIGC 动态创意</label>
        <label class="check"><input v-model="enabled" type="checkbox" /> 按配置自动发布计划</label>
        <button class="save" :disabled="saving || loading" @click="save">{{ saving ? '保存中…' : '保存该千川账户配置' }}</button>
      </div>
      <p class="note">抖音同账号同商品确认“已发布”后才会建任务；选择 Step 17 MP4 时还须上传成功并取得远端素材 ID。提交结果不明时停止自动重发，转为待核查。</p>
      <p v-if="message" class="message">{{ message }}</p>
      <div v-if="jobs.length" class="jobs"><article v-for="job in jobs" :key="job.id">
        <strong>{{ job.plan_name }}</strong><span>{{ statusLabel(job.status) }}</span>
        <small>商品 {{ job.product_id }} · 预算 {{ (job.budget_fen / 100).toFixed(2) }} 元/日 · 更新 {{ job.updated_at }}</small>
        <small v-if="job.remote_plan_id">千川计划 ID：{{ job.remote_plan_id }}</small>
        <small v-if="job.reason" class="reason">{{ job.reason }}</small>
        <button v-if="job.status === 'failed'" class="action" @click="retrySafe(job.id)">确认未点击发布后重试</button>
        <template v-if="job.status === 'needs_review'"><button class="action" @click="reviewPresent(job.id)">已在千川，补录计划 ID</button><button class="action" @click="reviewAbsent(job.id)">核实未发布后重排</button></template>
      </article></div>
      <p v-else class="note">暂无自动计划记录。</p>
    </template>
  </details>
</template>

<style scoped>
.plan-settings{border:1px solid #e1e6ee;border-radius:7px;background:#fff;padding:12px;box-shadow:var(--shadow-card)}
.plan-settings summary{cursor:pointer;font-size:13px;font-weight:600;color:#263249}
.note{margin:9px 0;color:#6c788b;font-size:12px;line-height:1.6}
.form{display:flex;align-items:flex-end;flex-wrap:wrap;gap:12px;margin:14px 0}
.form label{display:flex;flex-direction:column;gap:5px;color:#536078;font-size:12px}
.form label.check{flex-direction:row;align-items:center;padding-bottom:7px}
.form input[type=number],.form select{min-height:30px;border:1px solid #d8dee8;border-radius:5px;padding:4px 7px;background:#fff;color:#263249}
.form input[type=number]{width:125px}.form select{max-width:220px}
.save{min-height:30px;padding:4px 10px;border:1px solid #3676e7;border-radius:5px;background:#3676e7;color:#fff;cursor:pointer;font-size:12px}.save:disabled{opacity:.5}
.message{padding:9px 11px;border-radius:5px;background:#fff7e9;color:#835c19;font-size:12px}
.jobs{max-height:230px;overflow:auto}.jobs article{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;padding:8px 0;border-top:1px solid #eef1f5;font-size:12px}.jobs article small{flex-basis:100%;color:#7a8797}.jobs article .reason{color:#b54743}
.action{min-height:26px;border:1px solid #d8dee8;border-radius:5px;background:#fff;color:#285bad;cursor:pointer;font-size:12px}
</style>
