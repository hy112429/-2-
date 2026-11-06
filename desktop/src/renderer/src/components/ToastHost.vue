<script setup lang="ts">
import { appNotifications, dismissAllNotifications, dismissNotification } from '../utils/notifications'
</script>

<template>
  <TransitionGroup class="toast-host" name="toast" tag="section" aria-label="操作通知" aria-live="polite">
    <button v-if="appNotifications.length > 1" key="clear-all" type="button" class="clear-all" @click="dismissAllNotifications">清除全部通知</button>
    <article
      v-for="item in appNotifications"
      :key="item.id"
      class="toast"
      :class="item.level"
      :role="item.level === 'error' ? 'alert' : 'status'"
    >
      <span class="toast-icon" aria-hidden="true">
        {{ item.level === 'success' ? '✓' : item.level === 'error' ? '!' : item.level === 'warning' ? '⚠' : 'i' }}
      </span>
      <p>{{ item.message }}</p>
      <button type="button" aria-label="关闭通知" @click="dismissNotification(item.id)">×</button>
    </article>
  </TransitionGroup>
</template>

<style scoped>
.toast-host {
  position: fixed;
  z-index: 9999;
  top: calc(var(--h-top, 48px) + 12px);
  right: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: min(380px, calc(100vw - 36px));
  max-height: min(58vh, calc(100vh - var(--h-top, 48px) - 30px));
  overflow: auto;
  pointer-events: none;
}
.clear-all { align-self: flex-end; padding: 3px 9px; border: 1px solid var(--border); border-radius: 999px; background: #fff; color: var(--text-muted); font-size: 11px; box-shadow: var(--shadow-card); pointer-events: auto; }
.clear-all:hover { color: var(--brand); border-color: var(--brand); }

.toast {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) 24px;
  align-items: start;
  gap: 9px;
  padding: 12px;
  border: 1px solid #d7deea;
  border-left: 4px solid #64748b;
  border-radius: var(--radius);
  background: #fff;
  box-shadow: var(--shadow-float);
  color: #344054;
  pointer-events: auto;
}

.toast.success { border-left-color: #16a34a; }
.toast.info { border-left-color: #397af5; }
.toast.warning { border-left-color: #d97706; }
.toast.error { border-left-color: #dc2626; }

.toast-icon {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #eef2f7;
  color: #475467;
  font-size: 13px;
  font-weight: 700;
}

.success .toast-icon { background: #dcfce7; color: #15803d; }
.info .toast-icon { background: #dbeafe; color: #1d4ed8; }
.warning .toast-icon { background: #fef3c7; color: #b45309; }
.error .toast-icon { background: #fee2e2; color: #b91c1c; }

.toast p {
  max-height: 180px;
  margin: 0;
  overflow: auto;
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.toast button {
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: #667085;
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
}

.toast button:hover { background: #f2f4f7; color: #344054; }
.toast-enter-active, .toast-leave-active, .toast-move { transition: opacity 160ms ease, transform 160ms ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateX(14px); }
.toast-leave-active { position: absolute; right: 0; left: 0; }
</style>
