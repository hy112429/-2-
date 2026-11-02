<script setup lang="ts">
import { ref, watch } from 'vue'
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

const props = defineProps<{
  top: string
  nav: string
}>()

const emit = defineEmits<{ (event: 'navigate', value: string): void }>()
const selectionSettingsOpened = ref(false)

watch(() => props.nav, (nav) => {
  if (nav === '选品设置') selectionSettingsOpened.value = true
}, { immediate: true })

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
  <main class="work">
    <div class="crumb">{{ props.top }} / {{ props.nav }}</div>

    <!-- 发布计划在两个顶部菜单下都可访问 -->
    <template v-if="props.nav === '发布队列'">
      <div class="query-body">
        <PublishQueueView />
      </div>
    </template>

    <!-- 顶部菜单「商品查询」：商品查询主界面 -->
    <template v-else-if="props.top === '商品查询'">
      <div class="query-body">
        <ProductQueryView />
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
        <AccountManagementView />
      </div>
    </template>

    <template v-else-if="props.nav === '千川管理'">
      <div class="query-body">
        <QianchuanOverviewView @navigate="emit('navigate', $event)" />
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
      <SelectionSettingsView @navigate="emit('navigate', $event)" />
    </div>
  </main>
</template>

<style scoped>
.work {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--bg-app);
  padding: 12px 16px 16px;
  /* 这里**不能**是 auto：一旦这个容器能滚动，用户滚动时
     覆盖层（浏览器视图）不会跟着动，位置就会错位。
     所以让内容自适应、不产生滚动。 */
  overflow: hidden;
}

.crumb {
  flex-shrink: 0;
  margin-bottom: 8px;
  color: var(--text-muted);
  font-size: 13px;
}

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
</style>
