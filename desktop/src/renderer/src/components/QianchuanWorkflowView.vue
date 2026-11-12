<script setup lang="ts">
import { ref } from 'vue'
import QianchuanOverviewView from './QianchuanOverviewView.vue'
import QianchuanPlansView from './QianchuanPlansView.vue'

interface WorkflowBinding {
  account_id: number
  account_name: string
  account_status?: string
  qianchuan_account_id: number | null
  qc_name: string | null
  qc_id: string | null
}
interface WorkflowCandidate {
  publishQueueId: number
  accountId: number
  qianchuanAccountId: number
  productId: string
  productName: string
  canCreate: boolean
  reason?: string
  materialCount?: number
  dailyBudgetYuan?: number
  commissionRate?: number
  roiTarget?: number
}
interface WorkflowJob {
  id: number
  accountId?: number
  qianchuanAccountId?: number
  productId: string
  status: string
  scheduledDate?: string
  nextRetryAt?: string | null
  remoteMaterialId?: string
}
interface WorkflowRun {
  publishQueueId: number
  productId: string
  status: string
  reason?: string
}
interface WorkflowPlanJob {
  id: number
  account_id: number
  qianchuan_account_id: number
  product_id: string
  product_name?: string
  daily_budget_yuan: number
  commission_rate: number
  roi_target: number
  status: string
}

const emit = defineEmits<{
  (event: 'navigate', value: string): void
  (event: 'open-qianchuan-accounts'): void
}>()

const workflowBusy = ref(false)
const workflowProgress = ref('')
const workflowMessage = ref('')
const productSearch = ref('')

function checked(data: Record<string, unknown>): Record<string, unknown> {
  if (data.ok === false) throw new Error(String(data.error ?? '操作失败'))
  return data
}
function rows<T>(data: Record<string, unknown>): T[] {
  return Array.isArray(data.rows) ? data.rows as T[] : []
}
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}
function today(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

function matchesProduct(candidate: WorkflowCandidate): boolean {
  const query = productSearch.value.trim()
  return !query || candidate.productId.includes(query) || candidate.productName.includes(query)
}

const retryableDownloadStatuses = new Set([
  'disabled', 'blocked_auth', 'blocked_binding', 'skipped_no_video', 'skipped_quota',
  'failed_query', 'failed_download', 'needs_review', 'needs_fill', 'candidate_exhausted'
])

async function runWorkflow(): Promise<void> {
  if (workflowBusy.value) return
  workflowBusy.value = true
  workflowMessage.value = ''
  const queryLabel = productSearch.value.trim() ? `，按商品 ID ${productSearch.value.trim()} 搜索` : ''
  workflowProgress.value = `第 1/7 步：进入工具 → 全域投放 → 素材管理 → 视频库${queryLabel}…`
  const createdPlanIds: number[] = []
  const createdQueueIds = new Set<number>()
  const blocked: string[] = []
  let publishedCount = 0
  try {
    const targetResult = checked(await window.api.qianchuan.uploadTargets())
    const bindings = rows<WorkflowBinding>(targetResult)
      .filter((row) => row.account_status === 'active' && Number(row.qianchuan_account_id) > 0 && /^\d{8,20}$/.test(String(row.qc_id ?? '')))
      .filter((row, index, all) => all.findIndex((candidate) =>
        candidate.account_id === row.account_id && candidate.qianchuan_account_id === row.qianchuan_account_id
      ) === index)
    if (!bindings.length) throw new Error('没有找到已启用且绑定有效千川广告主 ID 的账号，请先完成账号绑定和登录。')

    // 一键流程明确要求自动下载；下载器本身仍按账号、商品、额度和远端去重规则运行。
    checked(await window.api.qianchuan.setPostPublishEnabled(true))
    const runs = rows<WorkflowRun>(checked(await window.api.qianchuan.postPublishRuns()))

    // 先启动发布成功后的自动下载。已有上传队列或正在处理的任务不重复启动。
    for (const binding of bindings) {
      workflowProgress.value = `第 2/7 步：启动 ${binding.account_name} 的自动下载…`
      const candidateResult = checked(await window.api.qianchuan.planCandidates({
        accountId: binding.account_id,
        qianchuanAccountId: Number(binding.qianchuan_account_id)
      }))
      const candidates = rows<WorkflowCandidate>(candidateResult).filter(matchesProduct)
      const jobs = rows<WorkflowJob>(checked(await window.api.qianchuan.uploadJobs(binding.account_id)))
      for (const candidate of candidates) {
        const productJobs = jobs.filter((job) => job.productId === candidate.productId &&
          Number(job.qianchuanAccountId) === Number(binding.qianchuan_account_id))
        if (productJobs.some((job) => job.status === 'needs_review')) {
          blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：有待核查上传任务`)
          continue
        }
        const run = runs.find((item) => item.publishQueueId === candidate.publishQueueId)
        const hasActiveOrCompletedJob = productJobs.some((job) => ['queued', 'uploading', 'succeeded'].includes(job.status))
        if (!hasActiveOrCompletedJob && (!run || retryableDownloadStatuses.has(run.status))) {
          await window.api.qianchuan.retryPostPublishRun(candidate.publishQueueId)
        }
      }
    }

    // 下载编排是异步的；循环复用现有 Step17 上传锁，直到计划资格出现或发现需要人工核查。
    const deadline = Date.now() + 10 * 60 * 1000
    let lastReadyCount = -1
    while (Date.now() < deadline) {
      const readyCandidates: WorkflowCandidate[] = []
      let pending = 0
      const currentRuns = rows<WorkflowRun>(checked(await window.api.qianchuan.postPublishRuns()))
      for (const binding of bindings) {
        const qcId = Number(binding.qianchuan_account_id)
        const candidateResult = checked(await window.api.qianchuan.planCandidates({ accountId: binding.account_id, qianchuanAccountId: qcId }))
        const candidates = rows<WorkflowCandidate>(candidateResult).filter(matchesProduct)
        const jobs = rows<WorkflowJob>(checked(await window.api.qianchuan.uploadJobs(binding.account_id)))
        for (const candidate of candidates) {
          const productJobs = jobs.filter((job) => job.productId === candidate.productId && Number(job.qianchuanAccountId) === qcId)
          const reviewJob = productJobs.find((job) => job.status === 'needs_review')
          if (reviewJob) {
            if (!blocked.some((item) => item.includes(candidate.productId))) {
              blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：任务 #${reviewJob.id} 待核查`)
            }
            continue
          }
          const dueJobs = productJobs.filter((job) => job.status === 'queued' &&
            (!job.scheduledDate || job.scheduledDate <= today()) &&
            (!job.nextRetryAt || job.nextRetryAt <= new Date().toLocaleString('sv-SE').replace(' ', 'T')))
          if (dueJobs.length) {
            pending += dueJobs.length
            for (const job of dueJobs) {
              workflowProgress.value = `第 3/7 步：上传 ${binding.account_name} 的素材任务 #${job.id}…`
              const result = await window.api.qianchuan.runUpload(job.id)
              if (result.status === 'needs_review' || result.manualActionRequired === true) {
                blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：任务 #${job.id} 上传结果待核查`)
              }
            }
          }
          if (candidate.canCreate) readyCandidates.push(candidate)
          else if (productJobs.some((job) => ['queued', 'uploading'].includes(job.status))) pending += 1
          else {
            const currentRun = currentRuns.find((item) => item.publishQueueId === candidate.publishQueueId)
            const terminalDownload = currentRun && ['candidate_exhausted', 'skipped_no_video', 'failed_query', 'failed_download'].includes(currentRun.status)
            if (terminalDownload) {
              if (!blocked.some((item) => item.includes(candidate.productId))) {
                blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：${currentRun.reason || '自动下载未得到可用素材'}`)
              }
            } else if (candidate.reason && !/素材上传|可用.*素材|处理尚未完成|待核查/.test(candidate.reason)) {
              if (!blocked.some((item) => item.includes(candidate.productId))) {
                blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：${candidate.reason}`)
              }
            } else if (!candidate.reason?.includes('待核查')) pending += 1
          }
        }
      }
      const uniqueReady = [...new Map(readyCandidates.map((item) => [item.publishQueueId, item])).values()]
      if (uniqueReady.length !== lastReadyCount) {
        lastReadyCount = uniqueReady.length
        workflowProgress.value = `第 4/7 步：已回读 ${uniqueReady.length} 个商品的千川素材 ID…`
      }
      for (const candidate of uniqueReady) {
        if (createdQueueIds.has(candidate.publishQueueId)) continue
        try {
          workflowProgress.value = `第 5/7 步：为 ${candidate.productName || candidate.productId} 创建待发布计划…`
          const result = checked(await window.api.qianchuan.createManualPlan({
            publishQueueId: candidate.publishQueueId,
            qianchuanAccountId: candidate.qianchuanAccountId
          }))
          const job = result.job as Record<string, unknown> | undefined
          const id = Number(job?.id)
          if (Number.isSafeInteger(id) && id > 0) {
            createdPlanIds.push(id)
            createdQueueIds.add(candidate.publishQueueId)
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          if (!/已有计划任务/.test(message)) blocked.push(`${candidate.productName || candidate.productId}：${message}`)
        }
      }
      if (createdPlanIds.length || (pending === 0 && uniqueReady.length === 0)) break
      await sleep(5000)
    }

    if (!createdPlanIds.length) {
      workflowProgress.value = ''
      workflowMessage.value = blocked.length
        ? `流程已停止，需先处理：${blocked.slice(0, 3).join('；')}${blocked.length > 3 ? `；另有 ${blocked.length - 3} 项` : ''}`
        : '当前没有商品完成素材下载和上传，流程已保留在队列中，可稍后再次点击。'
      return
    }

    const planResult = checked(await window.api.qianchuan.planJobs({ limit: 200 }))
    const plans = rows<WorkflowPlanJob>(planResult).filter((job) => createdPlanIds.includes(job.id) && job.status === 'ready')
    if (!plans.length) {
      workflowMessage.value = '计划已经进入队列，但当前没有可安全提交的计划，请刷新计划列表查看状态。'
      return
    }
    const summary = plans.map((job) => `${job.product_name || job.product_id}：预算 ${job.daily_budget_yuan} 元，保本 ROI ${Number(job.roi_target).toFixed(2)}`).join('\n')
    workflowProgress.value = '第 6/7 步：等待甲方确认预算和 ROI…'
    if (!window.confirm(`素材下载、上传、素材 ID 回读和计划创建已完成。\n\n即将提交以下千川付费计划：\n${summary}\n\n确认预算和 ROI 后现在发布？`)) {
      workflowMessage.value = `已创建 ${plans.length} 个待发布计划，已暂停在预算确认前。`
      return
    }
    for (const plan of plans) {
      workflowProgress.value = `第 7/7 步：发布计划 ${plan.product_name || plan.product_id}…`
      const result = await window.api.qianchuan.runPlan(plan.id, plan.daily_budget_yuan)
      if (result.ok === false) blocked.push(`${plan.product_name || plan.product_id}：${String(result.error ?? '发布失败')}`)
      else publishedCount += 1
    }
    workflowProgress.value = ''
    workflowMessage.value = blocked.length
      ? `已发布 ${publishedCount}/${plans.length} 个计划；${blocked.slice(0, 3).join('；')}`
      : `全流程完成：已下载、上传、回读素材并发布 ${publishedCount} 个千川计划。`
  } catch (error) {
    workflowProgress.value = ''
    workflowMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    workflowBusy.value = false
  }
}
</script>

<template>
  <section class="qianchuan-workflow">
    <header class="workflow-heading">
      <div>
        <h1>千川投放</h1>
        <p>按真实千川顺序进入视频库，按商品 ID 搜索，再完成下载、上传、素材 ID 回读和计划发布。</p>
      </div>
      <div class="workflow-actions">
        <label class="product-search">商品 ID 搜索<input v-model="productSearch" inputmode="numeric" maxlength="30" placeholder="输入商品 ID" @input="productSearch = productSearch.replace(/\D/g, '').slice(0, 30)" /></label>
        <button class="btn primary" type="button" :disabled="workflowBusy" @click="runWorkflow">{{ workflowBusy ? '流程执行中…' : '一键执行千川全流程' }}</button>
        <button class="btn" type="button" @click="emit('navigate', '千川日志')">发布日志</button>
        <button class="btn" type="button" @click="emit('open-qianchuan-accounts')">账号与登录</button>
      </div>
    </header>

    <div v-if="workflowProgress || workflowMessage" class="workflow-feedback" :class="{ error: workflowMessage && !workflowProgress }">
      <strong v-if="workflowProgress">{{ workflowProgress }}</strong>
      <span v-if="workflowMessage">{{ workflowMessage }}</span>
    </div>

    <ol class="platform-flow" aria-label="千川真实投放步骤">
      <li><b>1</b><span>工具 → 全域投放 → 视频库<br><small>按商品 ID 搜索</small></span></li>
      <li><b>2</b><span>自动下载本地视频<br><small>只使用当前账号商品素材</small></span></li>
      <li><b>3</b><span>上传千川素材<br><small>文件上传后等待回执</small></span></li>
      <li><b>4</b><span>回读远端素材 ID<br><small>没有 ID 就停在待核查</small></span></li>
      <li><b>5</b><span>生成待发布计划<br><small>上传成功后才创建</small></span></li>
      <li><b>6</b><span>确认预算和 ROI<br><small>甲方确认后才提交</small></span></li>
      <li><b>7</b><span>发布千川计划<br><small>回读千川计划 ID</small></span></li>
    </ol>

    <div class="workflow-scroll">
      <section class="workflow-step">
        <header class="workflow-step-head"><span class="step-number">1</span><div><strong>全域投放与素材处理</strong><small>按商品 ID 定位投放商品；自动下载、上传或复用素材，并回读千川素材 ID</small></div></header>
        <QianchuanOverviewView @navigate="emit('navigate', $event)" @open-qianchuan-accounts="emit('open-qianchuan-accounts')" />
      </section>

      <section class="workflow-step">
        <header class="workflow-step-head"><span class="step-number">2</span><div><strong>投放设置与计划发布</strong><small>控成本投放、标题、计划名称、预算确认后发布</small></div></header>
        <QianchuanPlansView @open-qianchuan-accounts="emit('open-qianchuan-accounts')" />
      </section>
    </div>
  </section>
</template>

<style scoped>
.qianchuan-workflow { display:flex; flex:1 1 auto; flex-direction:column; gap:10px; width:100%; height:100%; min-width:0; min-height:0; overflow:auto; padding:2px 2px 14px; color:#263249; }
.workflow-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.workflow-heading h1 { margin:0; font-size:21px; }
.workflow-heading p { margin:4px 0 0; color:#6c788b; font-size:12px; line-height:1.5; }
.workflow-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.product-search { display:flex; align-items:center; gap:6px; color:#536078; font-size:12px; }
.product-search input { width:170px; min-height:30px; border:1px solid #d8dee8; border-radius:5px; padding:4px 8px; color:#263249; background:#fff; }
.workflow-feedback { display:flex; flex-direction:column; gap:4px; flex:0 0 auto; padding:9px 11px; border:1px solid #c9dcff; border-radius:7px; background:#f3f7ff; color:#285bad; font-size:12px; line-height:1.45; }
.workflow-feedback.error { border-color:#f3c2bd; background:#fff2f0; color:#b54743; }
.platform-flow { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:6px; margin:0; padding:0; list-style:none; }
.platform-flow li { display:flex; align-items:flex-start; gap:6px; min-width:0; padding:8px 7px; border:1px solid #e1e8f3; border-radius:7px; background:#f8fbff; color:#33415a; font-size:11px; line-height:1.35; }
.platform-flow b { display:inline-flex; align-items:center; justify-content:center; flex:0 0 auto; width:19px; height:19px; border-radius:50%; background:#e8f0ff; color:#2f6fe4; font-size:10px; }
.platform-flow span { min-width:0; }
.platform-flow small { color:#77849a; font-size:10px; }
.workflow-scroll { flex:0 0 auto; min-width:0; min-height:0; overflow:visible; padding:1px 3px 0; }
.workflow-step { margin-bottom:10px; border:1px solid #dfe6f0; border-radius:10px; background:#fff; box-shadow:var(--shadow-card); overflow:visible; }
.workflow-step-head { display:flex; align-items:center; gap:10px; padding:13px 15px; border-bottom:1px solid #e8ecf2; }
.step-number { display:inline-flex; align-items:center; justify-content:center; width:25px; height:25px; border-radius:50%; background:#e8f0ff; color:#2f6fe4; font-weight:700; font-size:12px; }
.workflow-step-head div { display:flex; flex-direction:column; gap:3px; min-width:0; }
.workflow-step-head strong { font-size:14px; }
.workflow-step-head small { color:#77849a; font-size:11px; line-height:1.4; }
.workflow-step > :deep(.qc-page), .workflow-step > :deep(.plans-page) { flex:0 0 auto; min-height:auto; border-top:1px solid #e8ecf2; padding:12px 15px 16px; overflow:visible; }
.workflow-step > :deep(.qc-page > .heading), .workflow-step > :deep(.plans-page > .page-head) { display:none; }
.workflow-step > :deep(.qc-page), .workflow-step > :deep(.plans-page) { height:auto; }
.workflow-step > :deep(.qc-page .list), .workflow-step > :deep(.qc-page .post-publish-list) { max-height:none; overflow:visible; }
.btn { min-height:32px; border:1px solid #d8dee8; border-radius:6px; padding:5px 11px; background:#fff; color:#263249; cursor:pointer; font-size:12px; }
.btn.primary { border-color:#3676e7; background:#3676e7; color:#fff; }
.btn:disabled { opacity:.55; cursor:not-allowed; }
.btn:hover { border-color:#3676e7; color:#3676e7; }
@media (max-width: 1200px) { .platform-flow { grid-template-columns:repeat(4,minmax(0,1fr)); } }
@media (max-width: 900px) { .platform-flow { grid-template-columns:repeat(2,minmax(0,1fr)); } .workflow-step-head { align-items:flex-start; } }
</style>
