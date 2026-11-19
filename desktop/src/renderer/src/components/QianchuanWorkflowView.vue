<script setup lang="ts">
import { onMounted, ref } from 'vue'
import QianchuanOverviewView from './QianchuanOverviewView.vue'
import QianchuanPlansView from './QianchuanPlansView.vue'
import { useAppRefresh } from '../utils/app_refresh'

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
  publish_queue_id: number
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
const bindingReady = ref(false)
const bindingMessage = ref('正在检查千川 ID 绑定…')

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

async function refreshBindingGate(): Promise<void> {
  try {
    const result = checked(await window.api.qianchuan.uploadTargets())
    const active = rows<WorkflowBinding>(result).filter((row) => row.account_status === 'active')
    const bound = active.filter((row) => Number(row.qianchuan_account_id) > 0)
    const valid = bound.filter((row) =>
      /^\d{8,20}$/.test(String(row.qc_id ?? '').trim())
    )
    bindingReady.value = valid.length > 0
    bindingMessage.value = valid.length
      ? `已绑定 ${valid.length} 个有效千川广告主 ID，可进入千川发布。`
      : bound.length
        ? '已关联千川账户，但广告主 ID 缺失或格式无效（需 8–20 位数字）；请在账号管理核对后刷新。'
        : active.length
          ? '启用中的抖音账号尚未绑定千川账户；请在账号管理绑定后刷新。'
          : '当前没有启用中的抖音账号；请在账号管理核对账号状态后刷新。'
  } catch (error) {
    bindingReady.value = false
    bindingMessage.value = `无法确认千川 ID 绑定状态：${error instanceof Error ? error.message : String(error)}`
  }
}

const retryableDownloadStatuses = new Set([
  'disabled', 'blocked_auth', 'blocked_binding', 'skipped_no_video', 'skipped_quota',
  'failed_query', 'failed_download', 'needs_review', 'needs_fill', 'candidate_exhausted'
])

async function runWorkflow(): Promise<void> {
  if (workflowBusy.value) return
  await refreshBindingGate()
  if (!bindingReady.value) {
    workflowMessage.value = bindingMessage.value
    return
  }
  workflowBusy.value = true
  workflowMessage.value = ''
  workflowProgress.value = '正在核对已发布商品、千川账户和计划配置…'
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

    const videoSources = new Map<number, string>()
    for (const binding of bindings) {
      const config = checked(await window.api.qianchuan.planSettings(Number(binding.qianchuan_account_id)))
      const settings = config.settings as Record<string, unknown> | undefined
      videoSources.set(Number(binding.qianchuan_account_id), String(settings?.video_source ?? 'uploaded'))
    }
    const needsUploadedVideos = bindings.some((binding) => videoSources.get(Number(binding.qianchuan_account_id)) !== 'my_video')
    if (needsUploadedVideos) checked(await window.api.qianchuan.setPostPublishEnabled(true))
    const runs = needsUploadedVideos
      ? rows<WorkflowRun>(checked(await window.api.qianchuan.postPublishRuns())) : []

    // 只有配置了计划页上传视频的账户才需要本地下载与上传队列。
    for (const binding of bindings) {
      if (videoSources.get(Number(binding.qianchuan_account_id)) === 'my_video') continue
      workflowProgress.value = `正在准备 ${binding.account_name} 的本地视频…`
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
    let pendingAtExit = 0
    while (Date.now() < deadline) {
      const readyCandidates: WorkflowCandidate[] = []
      let pending = 0
      const currentRuns = needsUploadedVideos
        ? rows<WorkflowRun>(checked(await window.api.qianchuan.postPublishRuns())) : []
      for (const binding of bindings) {
        const qcId = Number(binding.qianchuan_account_id)
        const usesUploadedVideo = videoSources.get(qcId) !== 'my_video'
        const candidateResult = checked(await window.api.qianchuan.planCandidates({ accountId: binding.account_id, qianchuanAccountId: qcId }))
        const candidates = rows<WorkflowCandidate>(candidateResult).filter(matchesProduct)
        const jobs = usesUploadedVideo
          ? rows<WorkflowJob>(checked(await window.api.qianchuan.uploadJobs(binding.account_id))) : []
        for (const candidate of candidates) {
          if (createdQueueIds.has(candidate.publishQueueId)) continue
          const productJobs = jobs.filter((job) => job.productId === candidate.productId && Number(job.qianchuanAccountId) === qcId)
          const reviewJob = usesUploadedVideo ? productJobs.find((job) => job.status === 'needs_review') : undefined
          if (reviewJob) {
            if (!blocked.some((item) => item.includes(candidate.productId))) {
              blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：任务 #${reviewJob.id} 待核查`)
            }
            continue
          }
          const dueJobs = productJobs.filter((job) => usesUploadedVideo && job.status === 'queued' &&
            (!job.scheduledDate || job.scheduledDate <= today()) &&
            (!job.nextRetryAt || job.nextRetryAt <= new Date().toLocaleString('sv-SE').replace(' ', 'T')))
          if (dueJobs.length) {
            pending += dueJobs.length
            for (const job of dueJobs) {
              workflowProgress.value = `正在计划页为 ${binding.account_name} 上传视频任务 #${job.id}…`
              const result = await window.api.qianchuan.runUpload(job.id)
              if (result.status === 'needs_review' || result.manualActionRequired === true) {
                blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：任务 #${job.id} 上传结果待核查`)
              }
            }
          }
          if (candidate.canCreate) readyCandidates.push(candidate)
          else if (usesUploadedVideo && productJobs.some((job) => ['queued', 'uploading'].includes(job.status))) pending += 1
          else {
            const currentRun = currentRuns.find((item) => item.publishQueueId === candidate.publishQueueId)
            const terminalDownload = usesUploadedVideo && currentRun &&
              ['candidate_exhausted', 'skipped_no_video', 'failed_query', 'failed_download'].includes(currentRun.status)
            if (terminalDownload) {
              if (!blocked.some((item) => item.includes(candidate.productId))) {
                blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：${currentRun.reason || '自动下载未得到可用素材'}`)
              }
            } else if (candidate.reason && !/素材上传|可用.*素材|处理尚未完成|待核查/.test(candidate.reason)) {
              if (!blocked.some((item) => item.includes(candidate.productId))) {
                blocked.push(`${binding.account_name} / ${candidate.productName || candidate.productId}：${candidate.reason}`)
              }
            } else if (usesUploadedVideo && !candidate.reason?.includes('待核查')) pending += 1
          }
        }
      }
      const uniqueReady = [...new Map(readyCandidates.map((item) => [item.publishQueueId, item])).values()]
      if (uniqueReady.length !== lastReadyCount) {
        lastReadyCount = uniqueReady.length
        workflowProgress.value = `已有 ${uniqueReady.length} 个商品具备建计划条件…`
      }
      for (const candidate of uniqueReady) {
        if (createdQueueIds.has(candidate.publishQueueId)) continue
        try {
          workflowProgress.value = `正在为 ${candidate.productName || candidate.productId} 创建本机待发布计划…`
          const result = checked(await window.api.qianchuan.createManualPlan({
            publishQueueId: candidate.publishQueueId,
            qianchuanAccountId: candidate.qianchuanAccountId
          }))
          const job = result.job as Record<string, unknown> | undefined
          const id = Number(job?.id)
          if (Number.isSafeInteger(id) && id > 0) {
            createdQueueIds.add(candidate.publishQueueId)
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          if (!/已有计划任务/.test(message)) blocked.push(`${candidate.productName || candidate.productId}：${message}`)
        }
      }
      pendingAtExit = pending
      if (pending === 0) break
      await sleep(5000)
    }

    const planResult = checked(await window.api.qianchuan.planJobs({ limit: 200 }))
    const plans = rows<WorkflowPlanJob>(planResult).filter((job) => job.status === 'ready' &&
      createdQueueIds.has(job.publish_queue_id) &&
      bindings.some((binding) => binding.account_id === job.account_id &&
        Number(binding.qianchuan_account_id) === job.qianchuan_account_id) &&
      (!productSearch.value.trim() || job.product_id.includes(productSearch.value.trim())))
    if (!plans.length) {
      workflowProgress.value = ''
      workflowMessage.value = blocked.length
        ? `流程已停止，需先处理：${blocked.slice(0, 3).join('；')}${blocked.length > 3 ? `；另有 ${blocked.length - 3} 项` : ''}`
        : '当前没有可安全提交的计划；素材或我的视频尚未就绪时，请先查看具体账号的任务状态。'
      return
    }
    if (pendingAtExit > 0) blocked.push(`另有 ${pendingAtExit} 个素材任务仍在处理，本次只提交已就绪计划`)
    const summary = plans.map((job) => `${job.product_name || job.product_id}：预算 ${job.daily_budget_yuan} 元，保本 ROI ${Number(job.roi_target).toFixed(2)}`).join('\n')
    workflowProgress.value = '正在确认千川计划预算和 ROI…'
    if (!window.confirm(`以下商品已具备计划发布条件：\n\n${summary}\n\n确认预算和 ROI 后现在发布？`)) {
      workflowMessage.value = `已创建 ${plans.length} 个待发布计划，已暂停在预算确认前。`
      return
    }
    for (const plan of plans) {
      workflowProgress.value = `正在商品全域投放页发布 ${plan.product_name || plan.product_id}…`
      const result = await window.api.qianchuan.runPlan(plan.id, plan.daily_budget_yuan)
      const outcome = result.job as Record<string, unknown> | undefined
      if (result.ok === false || outcome?.status !== 'published' || !/^\d{8,20}$/.test(String(outcome.remote_plan_id ?? ''))) {
        blocked.push(`${plan.product_name || plan.product_id}：${String(result.error ?? outcome?.reason ?? '尚未回读到唯一千川计划 ID')}`)
      } else publishedCount += 1
    }
    workflowProgress.value = ''
    workflowMessage.value = blocked.length
      ? `已发布 ${publishedCount}/${plans.length} 个计划；${blocked.slice(0, 3).join('；')}`
      : `已回读 ${publishedCount} 个千川计划 ID，计划发布完成。`
  } catch (error) {
    workflowProgress.value = ''
    workflowMessage.value = error instanceof Error ? error.message : String(error)
  } finally {
    workflowBusy.value = false
  }
}
useAppRefresh(() => refreshBindingGate())
onMounted(() => { void refreshBindingGate() })
</script>

<template>
  <section class="qianchuan-workflow">
    <header class="workflow-heading">
      <div>
        <h1>千川投放</h1>
        <p>进入商品全域投放，先选投放商品与控成本投放，再为同一商品添加视频、确认预算并发布计划。</p>
      </div>
      <div class="workflow-actions">
        <label class="product-search">商品 ID 搜索<input v-model="productSearch" inputmode="numeric" maxlength="30" placeholder="输入商品 ID" @input="productSearch = productSearch.replace(/\D/g, '').slice(0, 30)" /></label>
        <button class="btn primary" type="button" :disabled="workflowBusy || !bindingReady" @click="runWorkflow">{{ workflowBusy ? '流程执行中…' : '一键执行千川全流程' }}</button>
        <button class="btn" type="button" @click="emit('navigate', '千川日志')">发布日志</button>
        <button class="btn" type="button" @click="emit('open-qianchuan-accounts')">账号与登录</button>
      </div>
    </header>

    <div class="binding-gate" :class="{ ready: bindingReady }">
      <strong>{{ bindingReady ? '千川 ID 已绑定' : '必须先绑定千川 ID' }}</strong>
      <span>{{ bindingMessage }}</span>
      <button v-if="!bindingReady" class="btn" type="button" @click="emit('open-qianchuan-accounts')">去绑定千川 ID</button>
    </div>

    <div v-if="workflowProgress || workflowMessage" class="workflow-feedback" :class="{ error: workflowMessage && !workflowProgress }">
      <strong v-if="workflowProgress">{{ workflowProgress }}</strong>
      <span v-if="workflowMessage">{{ workflowMessage }}</span>
    </div>

    <ol class="platform-flow" aria-label="千川商品全域投放步骤">
      <li><b>1</b><span>进入商品全域投放<br><small>核对目标广告主</small></span></li>
      <li><b>2</b><span>选择投放商品<br><small>核对精确商品 ID</small></span></li>
      <li><b>3</b><span>控成本投放<br><small>设置预算和保本 ROI</small></span></li>
      <li><b>4</b><span>选择素材所属商品<br><small>再次核对同一商品 ID</small></span></li>
      <li><b>5</b><span>添加视频<br><small>我的视频或计划页上传</small></span></li>
      <li><b>6</b><span>确认计划设置<br><small>提交前确认预算</small></span></li>
      <li><b>7</b><span>发布并回读<br><small>取得唯一千川计划 ID</small></span></li>
    </ol>

    <div class="workflow-scroll">
      <section class="workflow-step">
        <header class="workflow-step-head"><span class="step-number">1</span><div><strong>投放商品与计划设置</strong><small>选择目标商品、控成本投放、视频来源及预算</small></div></header>
        <QianchuanPlansView @open-qianchuan-accounts="emit('open-qianchuan-accounts')" />
      </section>

      <section class="workflow-step">
        <header class="workflow-step-head"><span class="step-number">2</span><div><strong>视频准备与上传记录</strong><small>仅使用计划页上传视频时需要本地文件和素材 ID；我的视频直接在计划页核对</small></div></header>
        <QianchuanOverviewView @navigate="emit('navigate', $event)" @open-qianchuan-accounts="emit('open-qianchuan-accounts')" />
      </section>
    </div>
  </section>
</template>

<style scoped>
.qianchuan-workflow { display:flex; flex:1 1 auto; flex-direction:column; gap:10px; width:100%; height:100%; min-width:0; min-height:0; overflow:auto; padding:2px 2px 14px; color:var(--text); }
.workflow-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.workflow-heading h1 { margin:0; font-size:21px; }
.workflow-heading p { margin:4px 0 0; color:var(--text-muted); font-size:12px; line-height:1.5; }
.workflow-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.product-search { display:flex; align-items:center; gap:6px; color:var(--text-muted); font-size:12px; }
.product-search input { width:170px; min-height:30px; border:1px solid var(--border); border-radius:5px; padding:4px 8px; color:var(--text); background:#fff; }
.workflow-feedback { display:flex; flex-direction:column; gap:4px; flex:0 0 auto; padding:9px 11px; border:1px solid #c9dcff; border-radius:7px; background:#f3f7ff; color:#285bad; font-size:12px; line-height:1.45; }
.binding-gate { display:flex; align-items:center; flex-wrap:wrap; gap:8px; padding:9px 11px; border:1px solid #f0c5c0; border-radius:7px; background:#fff4f2; color:#a23d35; font-size:12px; line-height:1.45; }
.binding-gate.ready { border-color:#bfe3ce; background:#f1fbf5; color:#197044; }
.binding-gate span { color:inherit; opacity:.9; }
.workflow-feedback.error { border-color:#f3c2bd; background:#fff2f0; color:#b54743; }
.platform-flow { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:6px; margin:0; padding:0; list-style:none; }
.platform-flow li { display:flex; align-items:flex-start; gap:6px; min-width:0; padding:8px 7px; border:1px solid #e1e8f3; border-radius:7px; background:#f8fbff; color:#33415a; font-size:11px; line-height:1.35; }
.platform-flow b { display:inline-flex; align-items:center; justify-content:center; flex:0 0 auto; width:19px; height:19px; border-radius:50%; background:#e8f0ff; color:#2f6fe4; font-size:10px; }
.platform-flow span { min-width:0; }
.platform-flow small { color:#77849a; font-size:10px; }
.workflow-scroll { flex:0 0 auto; min-width:0; min-height:0; overflow:visible; padding:1px 3px 0; }
.workflow-step { margin-bottom:10px; border:1px solid #dfe6f0; border-radius:10px; background:#fff; box-shadow:var(--shadow-card); overflow:visible; }
.workflow-step-head { display:flex; align-items:center; gap:10px; padding:13px 15px; border-bottom:1px solid var(--border); }
.step-number { display:inline-flex; align-items:center; justify-content:center; width:25px; height:25px; border-radius:50%; background:#e8f0ff; color:#2f6fe4; font-weight:700; font-size:12px; }
.workflow-step-head div { display:flex; flex-direction:column; gap:3px; min-width:0; }
.workflow-step-head strong { font-size:14px; }
.workflow-step-head small { color:#77849a; font-size:11px; line-height:1.4; }
.workflow-step > :deep(.qc-page), .workflow-step > :deep(.plans-page) { flex:0 0 auto; min-height:auto; border-top:1px solid var(--border); padding:12px 15px 16px; overflow:visible; }
.workflow-step > :deep(.qc-page > .heading), .workflow-step > :deep(.plans-page > .page-head) { display:none; }
.workflow-step > :deep(.qc-page), .workflow-step > :deep(.plans-page) { height:auto; }
.workflow-step > :deep(.qc-page .list), .workflow-step > :deep(.qc-page .post-publish-list) { max-height:none; overflow:visible; }
.btn { min-height:32px; border:1px solid var(--border); border-radius:6px; padding:5px 11px; background:#fff; color:var(--text); cursor:pointer; font-size:12px; }
.btn.primary { border-color:var(--brand); background:var(--brand); color:#fff; }
.btn:disabled { opacity:.55; cursor:not-allowed; }
.btn:hover { border-color:var(--brand); color:var(--brand); }
@media (max-width: 1200px) { .platform-flow { grid-template-columns:repeat(4,minmax(0,1fr)); } }
@media (max-width: 900px) { .platform-flow { grid-template-columns:repeat(2,minmax(0,1fr)); } .workflow-step-head { align-items:flex-start; } }
.qianchuan-workflow { gap: 14px; padding: 4px 4px 17px; }
.workflow-heading h1 { font-size: 24px; letter-spacing: -.03em; }
.workflow-feedback { border-color: var(--border); border-radius: var(--radius-control); background: var(--brand-soft); color: var(--brand); }
.workflow-step { margin-bottom: 12px; border-color: var(--border); border-radius: var(--radius); }
.workflow-step-head { padding: 14px 16px; }
.step-number { background: var(--brand-soft); color: var(--brand); }
.btn { min-height: 32px; border-radius: var(--radius-control); }
.btn.primary:hover:not(:disabled) { border-color: var(--brand-hover); background: var(--brand-hover); color: #fff; }
</style>
