import { ref } from 'vue'

export type NotificationLevel = 'success' | 'info' | 'warning' | 'error'

export interface AppNotification {
  id: number
  message: string
  level: NotificationLevel
}

export const appNotifications = ref<AppNotification[]>([])

const timers = new Map<number, number>()
let nextId = 1

function inferLevel(message: string): NotificationLevel {
  const failureCounts = Array.from(
    message.matchAll(/(?:失败|错误)\s*[：:]?\s*(\d+)\b/g),
    (match) => Number(match[1])
  )
  const isCompletionSummary = /完成|结束/.test(message)
  const hasOnlyZeroFailureCounts = failureCounts.length > 0 && failureCounts.every((count) => count === 0)
  const withoutZeroFailureCounts = message.replace(/(?:失败|错误)\s*[：:]?\s*0\b/g, '')

  // 汇总消息通常会同时写“成功 N / 失败 0”。“失败”是计数字段，
  // 不能按普通错误关键词处理，否则成功完成也会显示红色。
  if (
    isCompletionSummary &&
    hasOnlyZeroFailureCounts &&
    !/失败|错误|异常|超时|出错|无法|未能|无效|拒绝/.test(withoutZeroFailureCounts)
  ) {
    if (/部分完成|取消/.test(message) || /跳过\s*[：:]?\s*[1-9]\d*\b/.test(message)) return 'warning'
    const successCounts = Array.from(message.matchAll(/成功\s*[：:]?\s*(\d+)\b/g), (match) => Number(match[1]))
    return successCounts.some((count) => count > 0) ? 'success' : 'info'
  }

  if (/失败|错误|异常|超时|出错|无法|未能|无效|拒绝/.test(message)) return 'error'
  if (/请先|尚未|仍有|需要|不足|待恢复|待检查|取消/.test(message)) return 'warning'
  if (/成功|完成|已保存|已删除|已更新|已加入|已打开|已设置|已刷新|已结束/.test(message)) return 'success'
  return 'info'
}

export function notifyGlobal(message: string, level?: NotificationLevel): void {
  const text = message.trim()
  if (!text) return

  const resolvedLevel = level ?? inferLevel(text)
  if (appNotifications.value.some((item) => item.message === text && item.level === resolvedLevel)) return
  while (appNotifications.value.length >= 4) dismissNotification(appNotifications.value[0].id)
  const id = nextId++
  appNotifications.value = [...appNotifications.value, { id, message: text, level: resolvedLevel }]

  if (resolvedLevel !== 'error') {
    const duration = resolvedLevel === 'warning' ? 6500 : 4500
    timers.set(id, window.setTimeout(() => dismissNotification(id), duration))
  }
}

export function dismissNotification(id: number): void {
  const timer = timers.get(id)
  if (timer !== undefined) {
    window.clearTimeout(timer)
    timers.delete(id)
  }
  appNotifications.value = appNotifications.value.filter((item) => item.id !== id)
}

export function dismissAllNotifications(): void {
  for (const item of [...appNotifications.value]) dismissNotification(item.id)
}
