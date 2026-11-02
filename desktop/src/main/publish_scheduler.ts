export const PUBLISH_SCHEDULER_BASE_INTERVAL_MS = 10_000
export const DEFAULT_PUBLISH_JITTER_MIN_SECONDS = 5
export const DEFAULT_PUBLISH_JITTER_MAX_SECONDS = 15

export function readPublishJitterSeconds(value: unknown, fallback: number): number {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 3600 ? parsed : fallback
}

export function getPublishSchedulerJitterMs(
  minimumSeconds: number,
  maximumSeconds: number,
  random: () => number = Math.random
): number {
  if (!Number.isInteger(minimumSeconds) || !Number.isInteger(maximumSeconds) ||
      minimumSeconds < 0 || maximumSeconds < minimumSeconds || maximumSeconds > 3600) {
    throw new RangeError('发布调度随机间隔配置无效')
  }
  if (minimumSeconds === maximumSeconds) return minimumSeconds * 1000
  const sample = Math.min(0.999999999, Math.max(0, random()))
  return (minimumSeconds + Math.floor(sample * (maximumSeconds - minimumSeconds + 1))) * 1000
}

export function getNextPublishSchedulerDelayMs(
  minimumSeconds: number,
  maximumSeconds: number,
  random: () => number = Math.random
): number {
  return PUBLISH_SCHEDULER_BASE_INTERVAL_MS +
    getPublishSchedulerJitterMs(minimumSeconds, maximumSeconds, random)
}
