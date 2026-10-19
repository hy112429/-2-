<script setup lang="ts">
import { watch } from 'vue'
import BrowserPane from './BrowserPane.vue'
import LinkToolView from './LinkToolView.vue'
import ProductQueryView from './ProductQueryView.vue'
import AssetLibraryView from './AssetLibraryView.vue'
import AccountManagementView from './AccountManagementView.vue'

const props = defineProps<{
  top: string
  nav: string
}>()

// 顶部菜单切走「百应工作台」时，立即隐藏登录视图覆盖层（避免残留浏览器视图挡在别的页面上方）。
// 登录态保留在分区里不受影响；切回「百应工作台」后点「打开登录页」即重新显示。
// BrowserPane 卸载时也会 close（双保险，这里兜底解决 v-if 切换时序问题）。
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

    <!-- 顶部菜单「商品查询」：商品查询主界面 -->
    <template v-if="props.top === '商品查询'">
      <div class="query-body">
        <ProductQueryView />
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

    <!-- 默认「百应工作台」：登录面板 + 引擎控制台 -->
    <template v-else>
      <div class="content">
        <!-- 登录面板：工具栏 + 内置浏览器视图（WebContentsView） -->
        <div class="panel browser">
          <BrowserPane />
        </div>

        <!-- 链接解析下载工具（T-11 真实任务接入）：单条解析 + 下载 -->
        <div class="panel console">
          <LinkToolView />
        </div>
      </div>
    </template>
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
</style>