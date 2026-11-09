<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import BrowserPane from './BrowserPane.vue'
import LinkToolView from './LinkToolView.vue'
import ProductQueryView from './ProductQueryView.vue'
import AssetLibraryView from './AssetLibraryView.vue'
import AccountManagementView from './AccountManagementView.vue'
import SelectionSettingsView from './SelectionSettingsView.vue'
import SelectionLogsView from './SelectionLogsView.vue'
import PublishQueueView from './PublishQueueView.vue'
import DashboardView from './DashboardView.vue'
import QianchuanOverviewView from './QianchuanOverviewView.vue'
import ProductListView from './ProductListView.vue'

const props = defineProps<{
  top: string
  nav: string
}>()

const emit = defineEmits<{ (event: 'navigate', value: string): void }>()
const selectionSettingsOpened = ref(false)
const focusedSection = ref(false)
const scheduledStart = ref<SelectionScheduleDue | null>(null)
const receivedScheduleDates = new Set<string>()
const pendingProductDraft = ref<{ accountId: number; productId: string; title: string; sourceUrl: string } | null>(null)
const accountManagementInitialTab = ref<'账号列表' | '账号分组' | '千川账户'>('账号列表')
let stopSelectionScheduleDue: (() => void) | undefined

function receiveScheduledStart(due: SelectionScheduleDue): void {
  if (receivedScheduleDates.has(due.date)) return
  receivedScheduleDates.add(due.date)
  scheduledStart.value = due
  selectionSettingsOpened.value = true
  // 登录失效时选品页需要显示现有预检对话框，避免无人看见后台暂停。
  emit('navigate', '选品设置')
}

async function acknowledgeScheduledStart(date: string, outcome: string): Promise<void> {
  const result = await window.api.selectionSchedule.acknowledge(date, outcome)
  if (scheduledStart.value?.date === date) scheduledStart.value = null
  // 非终态确认不会清除主进程的待处理计划；允许下一次广播重新交给页面处理。
  receivedScheduleDates.delete(date)
  if (!result.cleared) return
}

function createPublishTaskFromProduct(product: { accountId: number; productId: string; title: string; sourceUrl: string }): void {
  pendingProductDraft.value = product
  emit('navigate', '发布队列')
}

function clearPendingProductDraft(): void {
  pendingProductDraft.value = null
}

function openQianchuanAccounts(): void {
  accountManagementInitialTab.value = '千川账户'
  emit('navigate', '账号管理')
}

function toggleFocusedSection(): void {
  focusedSection.value = !focusedSection.value
}

watch(() => props.nav, (nav) => {
  if (nav === '选品设置') selectionSettingsOpened.value = true
}, { immediate: true })

onMounted(async () => {
  stopSelectionScheduleDue = window.api.selectionSchedule.onDue(receiveScheduledStart)
  try {
    const schedule = await window.api.selectionSchedule.get()
    if (schedule.enabled || schedule.pendingDue) selectionSettingsOpened.value = true
    if (schedule.pendingDue) receiveScheduledStart(schedule.pendingDue)
  } catch {
    // 定时选品设置暂不可读时不影响普通选品页面。
  }
})

onUnmounted(() => stopSelectionScheduleDue?.())

// 顶部菜单切走「百应工作台」时，摘除客户端内置会话视图；系统浏览器窗口不受影响。
// 内置视图仍供后台探针和自动化流程使用，不承担登录页面展示。
watch(
  () => props.top,
  (top) => {
    if (top !== '百应工作台') {
      void window.api.browser.close()
    }
  }
)
</script>

<template>
  <div class="work-shell">
    <div v-if="focusedSection" class="work-focus-backdrop" @click="focusedSection = false"></div>
    <main class="work" :class="{ 'work--focused': focusedSection }">
      <div class="crumb">
        <span>{{ props.top === '商品查询' ? '商品查询' : `${props.top} / ${props.nav}` }}</span>
        <button class="focus-button" type="button" :aria-label="focusedSection ? '还原当前板块' : '单独显示当前板块'" @click="toggleFocusedSection">
          {{ focusedSection ? '还原板块' : '单独显示' }}
        </button>
      </div>

    <!-- 顶部菜单优先决定主界面，避免停留在发布队列时点「商品查询」仍看到旧页面。 -->
    <template v-if="props.top === '商品查询'">
      <div class="query-body">
        <ProductQueryView />
      </div>
    </template>

    <template v-else-if="props.nav === '商品列表'">
      <div class="query-body">
        <ProductListView
          @navigate="emit('navigate', $event)"
          @create-publish-task="createPublishTaskFromProduct"
        />
      </div>
    </template>

    <template v-else-if="props.nav === '发布队列'">
      <div class="query-body">
        <PublishQueueView :product-draft="pendingProductDraft" @product-draft-consumed="clearPendingProductDraft" />
      </div>
    </template>

    <!-- 默认入口是实际运营看板；浏览器与链接工具保留在其他工作区中。 -->
    <template v-else-if="props.nav === '投放看板'">
      <div class="query-body dashboard-body">
        <DashboardView @navigate="emit('navigate', $event)" />
      </div>
    </template>

    <!-- 「素材与工具」导航：本地素材库 -->
    <template v-else-if="props.nav === '素材与工具'">
      <div class="query-body">
        <AssetLibraryView />
      </div>
    </template>

    <!-- 「账号管理」导航（Step 11）：多账号矩阵 + 分组 + 千川账户 + 账号级登录态 -->
    <template v-else-if="props.nav === '账号管理'">
      <div class="query-body">
        <AccountManagementView :initial-tab="accountManagementInitialTab" />
      </div>
    </template>

    <template v-else-if="props.nav === '千川管理'">
      <div class="query-body">
        <QianchuanOverviewView @navigate="emit('navigate', $event)" @open-qianchuan-accounts="openQianchuanAccounts" />
      </div>
    </template>


    <template v-else-if="props.nav === '选品日志'">
      <div class="query-body">
        <SelectionLogsView />
      </div>
    </template>

    <!-- 账号连接与单条链接解析工具 -->
    <template v-else-if="props.nav !== '选品设置'">
      <div class="content">
        <!-- 登录面板：系统浏览器打开入口 + 客户端缓存态检查 -->
        <div class="panel browser">
          <BrowserPane />
        </div>

        <!-- 链接解析下载工具（T-11 真实任务接入）：单条解析 + 下载 -->
        <div class="panel console">
          <LinkToolView />
        </div>
      </div>
    </template>

    <!-- 首次打开选品后保持组件挂载，切换导航时冷却恢复协调器仍可接收主进程到期事件。 -->
    <div v-if="selectionSettingsOpened" v-show="props.nav === '选品设置' && props.top !== '商品查询'" class="query-body">
      <SelectionSettingsView
        :active="props.nav === '选品设置' && props.top !== '商品查询'"
        :scheduled-start="scheduledStart"
        @navigate="emit('navigate', $event)"
        @scheduled-start-handled="acknowledgeScheduledStart"
      />
    </div>
    </main>
  </div>
</template>

<style scoped>
.work-shell {
  position: relative;
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
}

.work {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: radial-gradient(circle at 94% -10%, #e8efff 0, transparent 34%), var(--bg-app);
  padding: 13px 18px 18px;
  /* 这里**不能**是 auto：一旦这个容器能滚动，用户滚动时
     覆盖层（浏览器视图）不会跟着动，位置就会错位。
     所以让内容自适应、不产生滚动。 */
  overflow: hidden;
}

.work-focus-backdrop {
  position: fixed;
  inset: 0;
  z-index: 80;
  background: rgba(15, 23, 42, .48);
}

.work--focused {
  position: fixed;
  inset: clamp(18px, 5vh, 48px) clamp(18px, 6vw, 96px);
  z-index: 90;
  padding: 15px 18px 18px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-app);
  box-shadow: var(--shadow-float);
}

.crumb {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 10px;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
}
.crumb > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.crumb::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--brand); box-shadow: 0 0 0 3px var(--brand-soft); }
.focus-button { flex: 0 0 auto; min-height: 26px; margin-left: auto; padding: 0 9px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-panel); color: var(--text-muted); font-size: 11px; }
.focus-button:hover { border-color: var(--brand); color: var(--brand); background: var(--brand-soft); }

.content {
  display: flex;
  gap: 12px;
  flex: 1;
  min-width: 0;
  min-height: 0;
}

.panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow-card);
}

.panel.browser {
  flex: 1.6;
}

.panel.console {
  flex: 1;
}

/* 商品查询视图占满工作区（自身内部滚动） */
.query-body {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
}

.dashboard-body {
  overflow: hidden;
}

@media (max-width: 1120px) {
  .work { padding: 10px 12px 12px; }
  .content { flex-direction: column; overflow: auto; }
  .panel.browser, .panel.console { flex: 0 0 auto; min-height: 280px; }
}

@media (max-height: 700px) {
  .work { padding-top: 8px; padding-bottom: 8px; }
  .crumb { margin-bottom: 6px; }
  .panel.browser, .panel.console { min-height: 220px; }
}
</style>
