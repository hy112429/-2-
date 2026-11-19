export type SelectionErrorClassification =
  | 'BAIYING_RATE_LIMIT'
  | 'BAIYING_NETWORK_ERROR'
  | 'BAIYING_11001_UNCONFIRMED'
  | 'CATEGORY_FILTER_APPLY_FAILED'
  | 'ACCOUNT_SWITCH_CAPTCHA'
  | 'ACCOUNT_SWITCH_LOGIN'
  | 'ACCOUNT_SWITCH_DISCONNECT'
  | 'SEARCHBOX_ZERO_EXHAUSTED'
  | 'SEARCHBOX_CONTROL_EXHAUSTED'
  | 'LOGIN_OR_PAGE_STATE_UNCONFIRMED'
  | 'PRODUCT_ID_MISMATCH'
  | 'PRODUCT_DETAIL_FAILED'
  | 'ACCOUNT_PERMISSION'

export interface UserSelectionError {
  userMessage: string
  classification?: SelectionErrorClassification
  diagnostic?: string
  cooldownSeconds?: number
}

type ErrorRecord = Record<string, unknown>

function asRecord(value: unknown): ErrorRecord | null {
  return value && typeof value === 'object' ? value as ErrorRecord : null
}

function firstText(values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (value instanceof Error && value.message.trim()) return value.message.trim()
  }
  return ''
}

function rawMessage(input: unknown): { message: string; diagnostic: string; explicitClassification: string } {
  const record = asRecord(input)
  const message = input instanceof Error
    ? input.message || input.name
    : typeof input === 'string'
      ? input
      : firstText([
          record?.message,
          record?.userMessage,
          record?.error,
          record?.reason,
          record?.diagnostic,
          record?.detail
        ])
  const stack = input instanceof Error ? input.stack : record?.stack
  const diagnostic = [
    message,
    typeof record?.diagnostic === 'string' ? record.diagnostic : '',
    typeof record?.detail === 'string' ? record.detail : '',
    typeof stack === 'string' ? stack : ''
  ]
    .filter(Boolean)
    .join('\n')
  return {
    message,
    diagnostic,
    explicitClassification: firstText([
      record?.classification,
      record?.code,
      typeof record?.code === 'number' ? String(record.code) : ''
    ])
  }
}

function classify(input: unknown, raw: string, explicitClassification: string): SelectionErrorClassification | undefined {
  const combined = `${explicitClassification}\n${raw}`.toUpperCase()
  if (/BAIYING_NETWORK_ERROR/i.test(explicitClassification)) return 'BAIYING_NETWORK_ERROR'
  if (/BAIYING_11001_UNCONFIRMED/i.test(explicitClassification)) return 'BAIYING_11001_UNCONFIRMED'
  if (/BAIYING_RATE_LIMIT/i.test(explicitClassification)) return 'BAIYING_RATE_LIMIT'
  if (/\b11001\b/.test(combined)) {
    // 业务码 11001 单独不能说明是网络错误还是频控；只按百应原始响应提示分类。
    const platformMessage = combined.match(/(?:百应响应说明|BUSINESSMESSAGE)\s*[:：=]\s*([^；;\n]{0,160})/i)?.[1] ?? ''
    if (/限流|频控|选品(?:太多|过多)|(?:请求|访问|操作)(?:过多|太多|频繁|太频繁|过于频繁)|RATE[_ -]?LIMIT|TOO MANY REQUESTS|FREQUENCY CONTROL/i.test(platformMessage)) {
      return 'BAIYING_RATE_LIMIT'
    }
    if (/网络(?:不稳定|错误|异常|故障|超时)|网络连接(?:失败|异常)/i.test(platformMessage)) {
      return 'BAIYING_NETWORK_ERROR'
    }
    return 'BAIYING_11001_UNCONFIRMED'
  }
  if (
    /BAIYING_RATE_LIMIT|RATE[_ -]?LIMIT|TOO MANY REQUESTS|TOO MANY SELECTIONS|REQUEST TOO FREQUENT|FREQUENCY CONTROL/i.test(combined) ||
    /百应.{0,12}(限流|冷却|频控|选品太多|请求过多|请求太多|请求频繁|访问频繁|操作频繁)|(?:限流|冷却|频控|请求频繁|访问频繁|操作频繁).{0,12}百应|(?:选品太多|选品过多|请求过多|请求太多|接口请求过多|请求频繁|访问频繁|操作频繁|操作过于频繁)/i.test(combined)
  ) {
    return 'BAIYING_RATE_LIMIT'
  }
  if (/SEARCHBOX_ZERO_EXHAUSTED/.test(combined)) return 'SEARCHBOX_ZERO_EXHAUSTED'
  if (/SEARCHBOX_CONTROL_EXHAUSTED/.test(combined)) return 'SEARCHBOX_CONTROL_EXHAUSTED'
  if (/PRODUCT_ID_MISMATCH|商品 ID.{0,12}(?:不一致|无法确认)|身份无法唯一确认|多个包含该商品/.test(combined)) {
    return 'PRODUCT_ID_MISMATCH'
  }
  if (/PRODUCT_DETAIL_FAILED|详情(?:页)?(?:打开|读取|回读).{0,12}(?:失败|超时)|无法返回选品列表/.test(combined)) {
    return 'PRODUCT_DETAIL_FAILED'
  }
  if (/ACCOUNT_PERMISSION|权限不足|无权限|未授权|广告主.*不可见/.test(combined)) return 'ACCOUNT_PERMISSION'
  if (
    /ACCOUNT_SWITCH_CAPTCHA|验证码|滑块(?:验证)?|安全验证|人工验证|CAPTCHA|VERIFICATION REQUIRED|VERIFICATION CHALLENGE|SECURITY CHALLENGE|VERIFY-CENTER/i.test(combined)
  ) {
    return 'ACCOUNT_SWITCH_CAPTCHA'
  }
  if (
    /ACCOUNT_SWITCH_DISCONNECT|BROWSER (?:HAS BEEN )?CLOSED|BROWSER DISCONNECTED|CONTEXT (?:HAS BEEN )?CLOSED|TARGET CLOSED|PAGE (?:HAS BEEN )?CLOSED|CONNECTION CLOSED|WEBSOCKET.{0,24}(?:CLOSED|DISCONNECTED|ERROR)|CDP.{0,24}(?:DISCONNECTED|CLOSED)|(?:浏览器|托管 CHROME|CDP|WEBSOCKET).{0,20}(?:断开|已关闭|连接失效)|(?:断开|已关闭|连接失效).{0,20}(?:浏览器|CDP|WEBSOCKET)/i.test(combined)
  ) {
    return 'ACCOUNT_SWITCH_DISCONNECT'
  }
  if (
    /ACCOUNT_SWITCH_LOGIN|LOGIN REQUIRED|NOT LOGGED IN|UNAUTHORIZED|AUTHENTICATION REQUIRED|需登录|需要登录|尚未登录|未登录|未登陆|登录失效|登陆失效|登录态.{0,10}(?:失效|无法确认|未确认)|登陆态.{0,10}(?:失效|无法确认|未确认)|登录状态.{0,10}(?:无法确认|失效)|登陆状态.{0,10}(?:无法确认|失效)/i.test(combined)
  ) {
    return 'ACCOUNT_SWITCH_LOGIN'
  }
  if (/CATEGORY_FILTER_APPLY_FAILED|MATERIAL_LIST_REQUEST_PENDING|FILTER_APPLY_RETRIES_EXHAUSTED/.test(combined)) {
    return 'CATEGORY_FILTER_APPLY_FAILED'
  }
  if (combined.includes('LOGIN_OR_PAGE_STATE_UNCONFIRMED')) return 'LOGIN_OR_PAGE_STATE_UNCONFIRMED'
  return undefined
}

/** 分类接口错误供批次恢复策略复用，避免文案分类与检查点类型分叉。 */
export function classifySelectionError(input: unknown): SelectionErrorClassification | undefined {
  const extracted = rawMessage(input)
  return classify(
    input,
    `${extracted.explicitClassification}\n${extracted.message}\n${extracted.diagnostic}`,
    extracted.explicitClassification
  )
}

function numericSeconds(value: unknown): number | undefined {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN
  if (!Number.isFinite(parsed) || parsed < 0) return undefined
  return Math.min(3_600, Math.ceil(parsed))
}

function cooldownSeconds(input: unknown, raw: string, fallback = 60, allowLooseDuration = true): number {
  const record = asRecord(input)
  const nested = asRecord(record?.data)
  for (const value of [
    record?.cooldownRemaining,
    record?.cooldownSeconds,
    record?.remainingSeconds,
    record?.retryAfterSeconds,
    nested?.cooldownRemaining,
    nested?.cooldownSeconds
  ]) {
    const parsed = numericSeconds(value)
    if (parsed !== undefined) return parsed
  }
  const patterns = [
    /(?:还需|剩余|冷却(?:剩余)?)(?:约)?\s*(\d+(?:\.\d+)?)\s*秒/i,
    /约\s*(\d+(?:\.\d+)?)\s*秒/i
  ]
  if (allowLooseDuration) patterns.push(/(\d+(?:\.\d+)?)\s*秒/i)
  for (const pattern of patterns) {
    const match = raw.match(pattern)
    if (match) return numericSeconds(match[1]) ?? fallback
  }
  return fallback
}

function hasTechnicalDetails(value: string, input: unknown, explicitClassification: string): boolean {
  const record = asRecord(input)
  const technicalPattern = /Error invoking|remote method|browser:[\w-]+|\bIPC\b|Trace(?:\s*ID)?\s*[:=#]|HTTP\s*\d{3}|material_list|Traceback|Call log:|Locator\.|(?:Type|Runtime|Timeout|Playwright)Error\b|\bat\s+[^\s]+\.(?:ts|js|py):\d+/i
  return Boolean(
    explicitClassification ||
    record?.classification ||
    record?.code ||
    technicalPattern.test(value) ||
    (record && (typeof record.stack === 'string' || typeof record.diagnostic === 'string'))
  )
}

function safeUnwrappedMessage(value: string): string {
  let message = value.trim()
  message = message.replace(/^Error invoking remote method\s+['"][^'"]+['"]:\s*/i, '')
  message = message.replace(/^(?:Error|TypeError|RuntimeError|TimeoutError):\s*/i, '')
  return message.trim()
}

export function toUserSelectionError(input: unknown): UserSelectionError {
  const extracted = rawMessage(input)
  const classification = classifySelectionError(input)
  if (classification === 'BAIYING_NETWORK_ERROR' || classification === 'BAIYING_11001_UNCONFIRMED') {
    const networkError = classification === 'BAIYING_NETWORK_ERROR'
    const seconds = cooldownSeconds(input, `${extracted.message}\n${extracted.diagnostic}`, networkError ? 30 : 60, false)
    const pausedByUser = /手动暂停|等待点击.{0,8}继续选品/.test(extracted.message)
    const nextStep = pausedByUser
      ? '已手动暂停，等待点击“继续选品”。'
      : `客户端约 ${seconds} 秒后自动续选。`
    return {
      userMessage: networkError
        ? `百应返回 11001，平台提示网络错误；${nextStep}`
        : `百应返回 11001，原因未确认；${nextStep}`,
      classification,
      diagnostic: extracted.diagnostic || undefined,
      cooldownSeconds: seconds
    }
  }
  if (classification === 'BAIYING_RATE_LIMIT') {
    const seconds = cooldownSeconds(input, `${extracted.message}\n${extracted.diagnostic}`)
    return {
      userMessage: seconds > 0
        ? `百应暂时限流，约 ${seconds} 秒后可继续选品。`
        : '百应暂时限流，冷却已结束，可以继续选品。',
      classification,
      diagnostic: extracted.diagnostic || undefined,
      cooldownSeconds: seconds
    }
  }
  if (classification === 'CATEGORY_FILTER_APPLY_FAILED') {
    return {
      userMessage: '百应筛选条件经过自动重试仍未确认，请检查当前筛选项后启动新批次。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'ACCOUNT_SWITCH_CAPTCHA') {
    return {
      userMessage: '该账号需要手动完成验证码或安全验证，本轮已停止该账号并继续其他账号；请在该账号专属浏览器完成验证后再恢复。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'ACCOUNT_SWITCH_DISCONNECT') {
    return {
      userMessage: '该账号的浏览器连接已断开，本轮已停止该账号并继续其他账号；请恢复该账号专属浏览器后再恢复。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'ACCOUNT_SWITCH_LOGIN') {
    return {
      userMessage: '该账号需要先登录百应或恢复登录状态，本轮已停止该账号并继续其他账号。请打开该账号专属浏览器完成登录，再重新检查并继续选品。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED') {
    return {
      userMessage: '该账号登录状态或页面无法确认，本轮已停止该账号并继续其他账号；请检查该账号专属浏览器后再恢复。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'SEARCHBOX_ZERO_EXHAUSTED' || classification === 'SEARCHBOX_CONTROL_EXHAUSTED') {
    return {
      userMessage: '该账号的百应选品搜索控件经有限恢复仍无法唯一定位；已保留诊断与截图路径，本轮跳过该账号并继续其他账号。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'PRODUCT_ID_MISMATCH' || classification === 'PRODUCT_DETAIL_FAILED') {
    return {
      userMessage: '当前商品身份或详情核验未确认，已跳过该商品并继续候选。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }
  if (classification === 'ACCOUNT_PERMISSION') {
    return {
      userMessage: '当前账号来源权限不足，已跳过当前账号并继续后续账号。',
      classification,
      diagnostic: extracted.diagnostic || undefined
    }
  }

  const safeMessage = safeUnwrappedMessage(extracted.message)
  if (!safeMessage) return { userMessage: '选品未完成，请查看诊断详情后重试。' }
  if (hasTechnicalDetails(extracted.diagnostic || safeMessage, input, extracted.explicitClassification)) {
    return {
      userMessage: '选品未完成，请查看诊断详情后重试。',
      diagnostic: extracted.diagnostic || safeMessage
    }
  }
  // 保留明确的中文业务校验文案；未知技术异常统一显示中性提示。
  if (/[\u3400-\u9fff]/.test(safeMessage)) return { userMessage: safeMessage }
  return { userMessage: '选品未完成，请查看诊断详情后重试。', diagnostic: extracted.diagnostic || safeMessage }
}

/** 成功进度原样显示；错误对象或技术异常则共用业务文案映射。 */
export function toUserSelectionMessage(input: unknown): string {
  return toUserSelectionError(input).userMessage
}
