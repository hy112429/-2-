import { app, ipcMain, type IpcMainInvokeEvent } from 'electron'
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process'
import { delimiter, join, relative, resolve, sep } from 'path'
import { existsSync, readFileSync } from 'fs'
import { createInterface } from 'readline'
import { getClientPaths } from './app-dirs'

/**
 * Python 引擎子进程管家（T-11）
 * ─────────────────────────────────────────────────────────────
 * 职责：
 *  1. 定位引擎目录与 venv 解释器（开发态 / 打包态）
 *  2. spawn 子进程，把 stdout 按行解析成「NDJSON 事件流」实时转发给渲染进程
 *  3. 支持中途取消（Windows 按进程树强杀，避免遗留 ffmpeg 等孙进程）
 *  4. 把 Python 崩溃/异常翻译成一句可读中文错误，不吐原始堆栈
 *
 * NDJSON 协议（引擎脚本 → stdout，每行一条 JSON）：
 *   {"type":"stage",  "stage":"…","percent":30}    进度
 *   {"type":"log",    "level":"info","message":"…"} 日志
 *   {"type":"error",  "message":"…"}                 失败终态
 *   {"type":"result", …任意字段…}                    成功终态
 * 兼容旧脚本：输出整行 JSON 但**没有** type 字段 → 视为终态结果（douyin_parse.py 等）
 */

/** 允许从渲染进程拉起的脚本白名单 —— 白名单外的路径一律拒绝（IPC 防篡改） */
const ALLOWED_SCRIPTS = new Set([
  'ping.py',
  'demo_task.py',
  'product_query.py',
  'download_queue.py',
  'asset_library.py',
  'douyin_parse.py',
  'download.py',
  'db.py',
  'session_bridge.py',
  'baiying_cache.py',
  'baiying_promotion_videos.py',
  'douyin_batch.py',
  'link_parser.py',
  'link_batch.py',
  'read_vod.py',
  'account_manager.py',
  'selection_settings.py',
  'selection_recovery.py',
  'selection_registry_local.py',
  'publish_queue.py',
  'image_to_video.py',
  'auto_video_editor.py',
  'remove_watermark.py'
])

/** 仅主进程可调用：队列终态与千川页面执行不能由渲染进程任意伪造。 */
const INTERNAL_SCRIPTS = new Set([
  'qianchuan_upload.py', 'qianchuan_browser.py', 'qianchuan_material_status.py',
  'qianchuan_violations.py', 'qianchuan_plan.py', 'qianchuan_plan_browser.py',
  'qianchuan_plan_supplement_browser.py',
  'post_publish_material.py', 'baiying_cart_download_state.py',
  'video_visual_dedupe.py'
])

/** 正在运行的任务表：taskId -> 句柄（含取消标志与强杀入口） */
interface TaskHandle {
  flag: { canceled: boolean }
  kill: () => void
  promise: Promise<ScriptOutcome>
}

/** 单次脚本执行的终态结论 */
export interface ScriptOutcome {
  ok: boolean
  code?: number | null
  canceled?: boolean
  error?: string
  result?: unknown
}

/** 转发给渲染进程的事件（已带 taskId） */
export interface EngineEvent {
  type: 'stage' | 'log' | 'error' | 'result'
  stage?: string
  percent?: number
  level?: 'info' | 'error'
  message?: string
  data?: unknown
}

/** engine:run 的入参 */
export interface EngineRunParams {
  script: string
  args?: string[]
  /** 给引擎的 JSON（如登录态），走 stdin，不落盘 */
  stdinJson?: unknown
}

const RUNNING = new Map<string, TaskHandle>()
let taskSeq = 0

// ---------------------------------------------------------------- 环境定位

/** 引擎目录：打包后用 resources/engine，开发态用项目根下的 engine/ */
export function getEngineDir(): string {
  if (process.env['ENGINE_DIR']) return process.env['ENGINE_DIR']
  if (app.isPackaged) return join(process.resourcesPath, 'engine')
  return resolve(app.getAppPath(), '..', 'engine')
}

/** 安装版随包携带的独立 Python 运行时，不依赖构建机的用户目录或系统 Python。 */
function getBundledPythonRuntimeDir(): string | undefined {
  if (!app.isPackaged) return undefined
  const runtimeDir = join(getEngineDir(), 'runtime')
  return existsSync(join(runtimeDir, 'python.exe')) ? runtimeDir : undefined
}

/** 安装版优先用随包运行时；开发态沿用 venv，基础 Python 缺失时退回 PATH。 */
export function getPythonPath(): string {
  const override = process.env['TOUGUANG_PYTHON_EXE']?.trim()
  if (override) return override

  const bundledRuntimeDir = getBundledPythonRuntimeDir()
  if (bundledRuntimeDir) return join(bundledRuntimeDir, 'python.exe')

  const venv = join(getEngineDir(), '.venv', 'Scripts', 'python.exe')
  if (!existsSync(venv)) return 'python'

  // Windows venv 的 python.exe 只是启动器，实际仍依赖 pyvenv.cfg 里的基础解释器。
  // electron-builder 会复制 venv，但目标电脑未必安装构建机上的绝对路径 Python。
  const configPath = join(getEngineDir(), '.venv', 'pyvenv.cfg')
  if (existsSync(configPath)) {
    try {
      const config = readFileSync(configPath, 'utf8')
      const executable = config.match(/^\s*executable\s*=\s*(.+?)\s*$/im)?.[1]
      if (executable && !existsSync(executable)) return 'python'
    } catch {
      // 配置不可读时仍沿用现有 venv，让进程返回真实诊断。
    }
  }
  return venv
}

/** 供所有 Python 子进程共用的运行环境；随包运行时必须显式设置标准库和依赖目录。 */
export function getPythonSpawnEnvironment(extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  const spawnEnv: NodeJS.ProcessEnv = {
    ...process.env,
    ...extra,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1'
  }
  const bundledRuntimeDir = process.env['TOUGUANG_PYTHON_EXE']?.trim()
    ? undefined
    : getBundledPythonRuntimeDir()
  if (bundledRuntimeDir) {
    const sitePackages = join(getEngineDir(), '.venv', 'Lib', 'site-packages')
    spawnEnv.PYTHONHOME = bundledRuntimeDir
    spawnEnv.PYTHONPATH = [sitePackages, process.env['PYTHONPATH']]
      .filter((value): value is string => Boolean(value?.trim()))
      .join(delimiter)
  }
  return spawnEnv
}

/** 将旧界面默认路径映射到当前运行版本的数据目录。 */
function resolveClientArgs(args: string[]): string[] {
  const paths = getClientPaths()
  const legacyDb = resolve('D:\\software\\投广客户端\\data\\ad_client.db').toLowerCase()
  const legacyDownloads = resolve('D:\\software\\投广客户端\\downloads').toLowerCase()
  return args.map((arg) => {
    const normalized = resolve(arg).toLowerCase()
    if (normalized === legacyDb) return paths.databasePath
    if (normalized === legacyDownloads) return paths.downloadsDir
    if (normalized.startsWith(`${legacyDownloads}${sep}`)) {
      const suffix = relative('D:\\software\\投广客户端\\downloads', arg)
      return join(paths.downloadsDir, suffix)
    }
    return arg
  })
}

// ---------------------------------------------------------------- 错误美化

/** 把 stderr 尾巴收敛成一句可读中文（剥掉 Traceback / File / ^ 等堆栈噪声行） */
function friendlyError(stderrTail: string, code: number | null): string {
  const lines = stderrTail
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(
      (line) =>
        line &&
        !line.startsWith('Traceback') &&
        !line.startsWith('File "') &&
        !line.startsWith('  File') &&
        !line.startsWith('^') &&
        !line.startsWith('During handling') &&
        !line.startsWith('The above exception')
    )
  const last = lines.at(-1) ?? ''
  const known: Record<string, string> = {
    ModuleNotFoundError: '缺少 Python 依赖，请在 engine 虚拟环境里重新安装 requirements.txt',
    FileNotFoundError: '文件不存在（路径或引擎脚本丢失）',
    PermissionError: '权限不足，无法访问目标文件或目录',
    JSONDecodeError: '引擎返回的不是合法 JSON',
    TimeoutError: '执行超时'
  }
  const exitText = code === null ? '' : `（退出码 ${code}）`
  for (const [name, cn] of Object.entries(known)) {
    if (last.startsWith(name)) return `${cn}${exitText}：${last}`
  }
  if (last) return `脚本执行失败${exitText}：${last}`
  return `脚本执行失败${exitText}，且没有任何错误输出`
}

// ---------------------------------------------------------------- 进程管理

/** Windows 上按进程树强杀（python 可能拉起 ffmpeg，必须连坐）；其他平台发 SIGTERM */
function killTree(child: ChildProcessWithoutNullStreams): void {
  if (process.platform === 'win32' && child.pid) {
    spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true })
  } else {
    child.kill('SIGTERM')
  }
}

// ---------------------------------------------------------------- 核心：拉起脚本

interface RunOptions {
  args?: string[]
  stdinJson?: unknown
  onEvent: (event: EngineEvent) => void
  internal?: boolean
}

/** 拉起脚本直到结束。promise = 终态结论；kill() = 请求取消（幂等） */
export function runScript(script: string, options: RunOptions): TaskHandle {
  const flag = { canceled: false }
  let active: ChildProcessWithoutNullStreams | null = null

  const promise = new Promise<ScriptOutcome>((resolveOutcome) => {
    if (!ALLOWED_SCRIPTS.has(script) && !(options.internal && INTERNAL_SCRIPTS.has(script))) {
      resolveOutcome({ ok: false, error: `不允许执行脚本：${script}（需加入白名单）` })
      return
    }
    const scriptPath = join(getEngineDir(), script)
    if (!existsSync(scriptPath)) {
      resolveOutcome({ ok: false, error: `引擎脚本不存在：${scriptPath}` })
      return
    }
    const pythonPath = getPythonPath()

    try {
      const spawnEnv = getPythonSpawnEnvironment({
        TOUGUANG_DATA_DIR: getClientPaths().dataDir,
        TOUGUANG_DOWNLOADS_DIR: getClientPaths().downloadsDir
      })
      active = spawn(pythonPath, [scriptPath, ...resolveClientArgs(options.args ?? [])], {
        cwd: getEngineDir(),
        windowsHide: true,
        env: spawnEnv
      })
    } catch (err) {
      resolveOutcome({ ok: false, error: `无法启动 Python 子进程：${String(err)}` })
      return
    }
    const child = active

    // stdin：登录态等 JSON 从这里送，不落盘
    if (options.stdinJson !== undefined) {
      try {
        child.stdin.write(`${JSON.stringify(options.stdinJson)}\n`)
        child.stdin.end()
      } catch {
        /* stdin 已关闭则忽略 */
      }
    }

    let result: unknown = undefined
    let stderrTail = ''
    let settled = false
    const finish = (outcome: ScriptOutcome): void => {
      if (settled) return
      settled = true
      resolveOutcome(outcome)
    }

    // stdout：逐行解析（NDJSON 协议，兼容多行 JSON 如 db.py 的 indent=2 输出）
    let jsonBuffer = ''
    const handleJson = (parsed: Record<string, unknown>): void => {
      if (typeof parsed['type'] === 'string') {
        const evt = { ...parsed } as unknown as EngineEvent
        options.onEvent(evt)
        if (evt.type === 'error') {
          result = { ok: false, error: evt.message } // 让 close 时终态对齐
        } else if (evt.type === 'result') {
          // 去掉 type 标记，剩余字段就是终态结果
          const { type: _t, ...rest } = evt
          result = rest
        }
      } else {
        result = parsed // 无 type 的 JSON = 终态结果（兼容旧脚本）
        options.onEvent({ type: 'result', data: parsed })
      }
    }
    createInterface({ input: child.stdout }).on('line', (line) => {
      const text = line.trim()
      if (!text) return

      // 正在累积多行 JSON：追加并尝试解析
      if (jsonBuffer !== '') {
        jsonBuffer += `\n${text}`
        try {
          handleJson(JSON.parse(jsonBuffer) as Record<string, unknown>)
          jsonBuffer = ''
        } catch {
          /* 还没闭合，继续累积 */
        }
        return
      }

      // 单行 JSON
      if (text.startsWith('{') || text.startsWith('[')) {
        try {
          handleJson(JSON.parse(text) as Record<string, unknown>)
          return
        } catch {
          // 单行不完整：可能是多行 JSON 的第一行（如 db.py 的 indent=2 输出）
          jsonBuffer = text
          return
        }
      }
      options.onEvent({ type: 'log', level: 'info', message: text })
    })

    // stderr：只留尾巴 → 出错时美化
    child.stderr.on('data', (chunk: Buffer) => {
      stderrTail = `${stderrTail}${chunk.toString('utf8')}`.slice(-4000)
    })

    child.on('error', (err) => {
      const missing = (err as NodeJS.ErrnoException).code === 'ENOENT'
      finish({
        ok: false,
        error: missing
          ? '找不到可用的 Python 解释器：请安装 Python 3.10+ 并加入 PATH，或通过 TOUGUANG_PYTHON_EXE 指定 python.exe。'
          : `Python 子进程启动失败：${err.message}`
      })
    })

    child.on('close', (code) => {
      if (flag.canceled) {
        finish({ ok: false, canceled: true, code, error: '任务已被取消' })
        return
      }
      if (code !== 0) {
        const structuredResult =
          result !== null && typeof result === 'object' && !Array.isArray(result)
            ? result as Record<string, unknown>
            : null
        const structuredError = structuredResult?.['error']
        finish({
          ok: false,
          code,
          error:
            typeof structuredError === 'string' && structuredError.trim()
              ? `${structuredError}（退出码 ${code}）`
              : friendlyError(stderrTail, code)
        })
        return
      }
      if (result !== undefined) {
        finish({ ok: true, code, result })
        return
      }
      finish({ ok: false, code, error: '引擎脚本正常退出，但没有输出任何结果（协议不符合）' })
    })
  })

  return {
    flag,
    promise,
    kill: () => {
      if (flag.canceled) return
      flag.canceled = true
      if (active) {
        try {
          killTree(active)
        } catch {
          /* 进程已退出则忽略 */
        }
      }
    }
  }
}

/** 主进程后台服务执行白名单引擎命令，不依赖某个渲染窗口存活。 */
export async function runEngineCommand(script: string, args: string[] = []): Promise<ScriptOutcome> {
  const handle = runScript(script, { args, internal: true, onEvent: () => undefined })
  return handle.promise
}

// ---------------------------------------------------------------- IPC 注册

/** 连通性测试的结果形状 */
export interface EnginePingResult {
  ok: boolean
  python?: string
  platform?: string
  requests?: string
  engine_dir?: string
  error?: string
}

export function registerEngineIpc(): void {
  // 测试连通性：跑 ping.py，返回结构化结论（成功带 Python 版本 / 失败带原因）
  ipcMain.handle('engine:ping', async (): Promise<EnginePingResult> => {
    const handle = runScript('ping.py', { onEvent: () => undefined })
    const outcome = await handle.promise
    if (outcome.ok && outcome.result && typeof outcome.result === 'object') {
      return { ok: true, ...(outcome.result as Record<string, unknown>) } as EnginePingResult
    }
    return { ok: false, error: outcome.error ?? '连通性测试失败' }
  })

  // 启动任务：立刻返回 {taskId}；进度/终态走 engine:event 事件通道
  ipcMain.handle('engine:run', (event: IpcMainInvokeEvent, params: EngineRunParams) => {
    const script = typeof params?.script === 'string' ? params.script : ''
    if (!script) {
      return { ok: false, error: '缺少 script 参数' }
    }
    const taskId = `task-${++taskSeq}`
    const sender = event.sender
    let ended = false // 是否已经发过终态事件（result / error）

    const handle = runScript(script, {
      args: params.args,
      stdinJson: params.stdinJson,
      onEvent: (evt) => {
        if (evt.type === 'result' || evt.type === 'error') ended = true
        if (sender.isDestroyed()) return
        if (evt.type === 'result') {
          // 统一终态载荷：脚本 result 行已带 data 字段就直接用；
          // 平铺字段（如 demo_task 的 {ok,steps}）则收进 data。
          // 渲染进程一律只读 e.data。
          const { type: _t, data, ...rest } = evt
          const payload = data !== undefined ? data : rest
          sender.send('engine:event', { taskId, type: 'result', data: payload })
          return
        }
        sender.send('engine:event', { taskId, ...evt })
      }
    })
    RUNNING.set(taskId, handle)

    // 终态兜底：脚本角色没发 result/error 时，由这里补发一条（保证事件流必有结尾）
    void handle.promise.then((outcome) => {
      RUNNING.delete(taskId)
      if (ended || sender.isDestroyed()) return
      if (outcome.canceled) {
        sender.send('engine:event', { taskId, type: 'error', message: '任务已被取消' })
      } else if (!outcome.ok) {
        sender.send('engine:event', { taskId, type: 'error', message: outcome.error })
      } else {
        sender.send('engine:event', { taskId, type: 'result', data: outcome.result })
      }
    })

    return { taskId }
  })

  // 取消任务：强杀进程树
  ipcMain.handle('engine:cancel', (_e, taskId: string) => {
    const handle = RUNNING.get(taskId)
    if (!handle) return { ok: true, alreadyEnded: true }
    handle.kill()
    return { ok: true }
  })
}

/** 应用退出前收尾：杀掉所有还没结束的引擎任务 */
export function disposeEngine(): void {
  for (const handle of RUNNING.values()) {
    handle.kill()
  }
  RUNNING.clear()
}
