import { ElectronAPI } from '@electron-toolkit/preload'

/**
 * 给渲染进程用的类型声明。
 *
 * 这些接口**故意重复声明**（与 preload/index.ts 里一致）：.d.ts 里再写一遍是常规做法，
 * 这样渲染进程的类型检查完全不依赖 Node / electron 的类型，配置更稳。
 * 改 IPC 接口时**两处都要改**。
 *
 * 注意：接口写在 declare global 里，是为了让 .vue 文件能直接用这些类型名。
 */

declare global {
  /** 内置浏览器视图的实时状态 */
  interface BrowserViewState {
    open: boolean
    targetId?: string
    url?: string
    title?: string
    loading?: boolean
    canGoBack?: boolean
    canGoForward?: boolean
  }

  /** 单个登录目标的登录态 */
  interface TargetLoginState {
    id: string
    name: string
    loggedIn: boolean
    matchedCookies: string[]
    cookieCount: number
    /** 只有 cookie 名字，**没有值** */
    cookieNames: string[]
  }

  /** 登录态总览 */
  interface LoginState {
    /** 系统是否提供加密能力（Windows 上是 DPAPI） */
    encryptionAvailable: boolean

    /** 加密后端的人类可读说明 */
    encryptionBackend: string

    targets: TargetLoginState[]

    /** 凭证加密备份的状态（只含元信息，不含 cookie 值） */
    credentialBackup: CredentialBackupMeta
    checkedAt: string
  }

  /** 凭证加密备份的可见元信息（B+ / T-16） */
  interface CredentialBackupMeta {
    exists: boolean
    updatedAt?: string
    cookieCount?: number
    error?: string
  }

  /**
   * 导出给 Python 引擎的会话。
   * ⚠️ cookieHeader 是明文，只在内存里流转，不要写日志或存文件。
   */
  interface EngineSession {
    cookieHeader: string
    userAgent: string
    cookieCount: number
    cookieNames: string[]
    targetId: string
    exportedAt: string
  }

  /** 引擎事件（主进程 → 渲染进程，engine:event） */
  interface EngineEventPayload {
    /** 所属任务 ID，与 run() 返回的 taskId 对应 */
    taskId: string
    type: 'stage' | 'log' | 'error' | 'result' | 'task'
    stage?: string
    percent?: number
    level?: 'info' | 'error'
    message?: string
    data?: unknown
    /** 队列任务事件（download_queue.py）：tid = 队列任务 ID */
    tid?: string
    /** 队列任务状态：pending / running / retrying / success / failed / skipped */
    status?: string
    /** 队列任务重试次数 */
    attempts?: number
    /** 队列任务落盘路径（success / skipped 时携带） */
    file_path?: string
  }

  interface SelectionProgressPayload {
    accountId: number
    stage: string
    message?: string
    index?: number
    total?: number
    percent?: number
    unit?: string
    indeterminate?: boolean
    productId?: string
    productTitle?: string
  }

  interface PublishProgressPayload {
    state: 'idle' | 'running' | 'succeeded' | 'warning' | 'failed'
    accountId?: number
    queueId?: number
    productName?: string
    stage: string
    message?: string
    percent?: number
    indeterminate?: boolean
    updatedAt?: string
    error?: string
    classification?: string
    canConfirmNoDraft?: boolean
  }

  /** engine:run 入参 */
  interface EngineRunParams {
    script: string
    args?: string[]
    /** 给引擎的 JSON（如登录态），走 stdin，不落盘 */
    stdinJson?: unknown
  }

  /** engine:run 返回值：立刻返回 taskId；终态走事件流 */
  interface EngineRunReply {
    taskId?: string
    ok?: boolean
    error?: string
  }

  /** engine:ping 返回值 */
  interface EnginePingResult {
    ok: boolean
    python?: string
    platform?: string
    requests?: string
    engine_dir?: string
    error?: string
  }

  /** baiying:batchStart 返回值 */
  interface BaiyingBatchStartReply {
    ok?: boolean
    taskId?: string
    error?: string
  }

  /** 批量抓取事件（主进程 → 渲染进程，baiying:event） */
  interface BaiyingBatchEvent {
    taskId: string
    type: 'stage' | 'product' | 'result' | 'error'
    index?: number
    total?: number
    percent?: number
    productId?: string
    status?: 'success' | 'failed'
    message?: string
    success?: number
    failed?: number
    canceled?: boolean
  }

  /** 头条批量解析事件（主进程 → 渲染进程，toutiao:event） */
  interface ToutiaoBatchEvent {
    taskId: string
    type: 'stage' | 'item' | 'result' | 'error'
    index?: number
    total?: number
    percent?: number
    input?: string
    status?: 'success' | 'failed'
    message?: string
    success?: number
    failed?: number
    items?: Array<{ input: string; ok: boolean; vod_url: string | null; error: string | null }>
    canceled?: boolean
  }

  /** 登录目标 */
  interface LoginTarget {
    id: string
    name: string
    url: string
  }

  /** 视图位置矩形（CSS 像素） */
  interface BoundsRect {
    x: number
    y: number
    width: number
    height: number
  }

  /** 账号级会话主体：抖音账号（acct）或千川账户（qc），分区互不共用 */
  interface AccountSubject {
    kind: 'acct' | 'qc'
    id: number
    name: string
  }

  /** 账号级登录态（只含 cookie 名，不含值） */
  interface AccountLoginState {
    kind: 'acct' | 'qc'
    id: number
    loggedIn: boolean
    matchedCookies: string[]
    cookieCount: number
    cookieNames: string[]
    /** 加密备份文件是否存在（safeStorage / DPAPI） */
    backupExists: boolean
    backupError?: string
    checkedAt: string
  }

  /** 账号级浏览器视图状态 */
  interface AccountViewState {
    open: boolean
    subject?: AccountSubject
    url?: string
    title?: string
    loading?: boolean
    canGoBack?: boolean
    canGoForward?: boolean
  }

  /** 「拉取信息」返回的账号公开信息 */
  interface AccountUserInfo {
    nickname: string
    followerCount: number
    awemeCount: number
  }

  /** 账号级导出给 Python 引擎的会话（cookieHeader 为明文，仅内存流转） */
  interface AccountEngineSession {
    cookieHeader: string
    userAgent: string
    cookieCount: number
    cookieNames: string[]
    subject: AccountSubject
    exportedAt: string
  }

  interface Window {
    electron: ElectronAPI
    api: {
      appInfo: {
        name: string
        getVersion(): Promise<string>
      }
      /** 当前运行版本的本地数据目录 */
      appPaths: {
        get(): Promise<{
          rootDir: string
          dataDir: string
          databasePath: string
          databaseExists: boolean
          downloadsDir: string
          browserProfilesDir: string
        }>
      }
      /** 限流冷却结束自动续选；deadline 由主进程计时，失焦或隐藏时仍会到点通知。 */
      selectionCooldown: {
        schedule(params: { batchId: string; deadline: number }): Promise<{ ok: boolean; error?: string }>
        acknowledge(params: { batchId: string; deadline: number; resumeQueued?: boolean }): Promise<{ ok: boolean; matched: boolean }>
        trace(params: { batchId?: string; stage: string; details?: Record<string, unknown> }): Promise<{ ok: boolean }>
        cancel(): Promise<{ ok: boolean }>
        onExpired(cb: (event: { batchId: string; deadline: number; sendCount: number }) => void): () => void
      }
      /** 托管外部 Chrome 登录入口（页面通过本机 CDP 接入） */
      browser: {
        getTargets(): Promise<LoginTarget[]>
        open(
          targetId: string,
          options?: { profileKey?: string; profilePath?: string }
        ): Promise<{ ok: boolean; url: string; external: boolean; profilePath?: string; port?: number; title?: string; error?: string }>
        inspectBaiyingSelection(options: { profileKey: string; profilePath?: string; includeCategoryTree?: boolean }): Promise<Record<string, unknown>>
        checkAccountLogins(params: { accountId: number; profilePath?: string }): Promise<Record<string, unknown>>
        inspectAccountPages(params: { accountId: number; profilePath?: string }): Promise<{
          ok: boolean
          inspectable: boolean
          baiyingOpen: boolean
          creatorCenterOpen: boolean
        }>
        validateProfilePath(options: { profileKey: string; profilePath?: string }): Promise<{ ok: boolean; path?: string; error?: string }>
        runBaiyingSelection(params: {
          profileKey: string
          profilePath?: string
          settings: Record<string, unknown>
          accountId: number
          accountKey?: string
          downloadPath: string
          selectedIndexes?: number[]
          candidateSnapshot?: unknown[]
          refreshCandidatesById?: boolean
          analyzeBeforeCart?: boolean
          maxSelectedProducts?: number
          stopAfterCart?: boolean
          previewOnly?: boolean
        }): Promise<Record<string, unknown>>
        getSelectionRegistrySnapshot(accountKey: string, candidates: Array<{ productId: string; leafCategoryId: string }>): Promise<{
          usedProductIds: string[]
          accountUsedCategoryIds: string[]
          categoryUsage: Record<string, number>
          mode: 'local'
        }>
        onSelectionProgress(cb: (progress: SelectionProgressPayload) => void): () => void
        prepareDouyinPublish(params: {
          accountId: number
          queueId?: number
          profilePath?: string
          productId: string
          productName: string
          productUrl: string
          videoPath: string
          description: string
          autoPublish?: boolean
        }): Promise<Record<string, unknown>>
        submitDouyinPublish(params: {
          accountId: number
          queueId: number
          profilePath?: string
          productId: string
          productName: string
          productUrl: string
          videoPath: string
          description: string
        }): Promise<Record<string, unknown>>
        onPublishProgress(cb: (progress: PublishProgressPayload) => void): () => void
        openDouyinCreator(params: { accountId: number; profilePath?: string }): Promise<Record<string, unknown>>
        close(): Promise<{ ok: boolean }>
        setBounds(rect: BoundsRect): void
        goBack(): Promise<{ ok: boolean }>
        goForward(): Promise<{ ok: boolean }>
        reload(): Promise<{ ok: boolean }>
        getState(): Promise<BrowserViewState>
        onStateChanged(cb: (state: BrowserViewState) => void): () => void
        onNeedBounds(cb: () => void): () => void
        onLoadFailed(cb: (e: { code: number; desc: string; url: string }) => void): () => void
      }
      publish: {
        getProgress(): Promise<PublishProgressPayload>
        runNow(queueId: number): Promise<PublishProgressPayload>
        runImmediately(queueId: number): Promise<PublishProgressPayload>
        retryManually(queueId: number, options?: { confirmedNotSubmitted?: boolean; confirmedNoDraft?: boolean }): Promise<PublishProgressPayload>
        onProgress(cb: (progress: PublishProgressPayload) => void): () => void
      }
      /** 登录态 */
      login: {
        check(): Promise<LoginState>
        clear(): Promise<{ ok: boolean }>
        exportForEngine(targetId: string): Promise<EngineSession>
        onStateChanged(cb: (state: LoginState) => void): () => void
      }
      /** Python 引擎（T-11） */
      engine: {
        /** 测试连通性：跑 engine/ping.py */
        ping(): Promise<EnginePingResult>
        /** 启动任务，立刻返回 {taskId}；进度与终态走 onEvent */
        run(params: EngineRunParams): Promise<EngineRunReply>
        /** 取消任务 */
        cancel(taskId: string): Promise<{ ok: boolean; alreadyEnded?: boolean }>
        /** 订阅引擎事件流，返回取消订阅函数 */
        onEvent(cb: (e: EngineEventPayload) => void): () => void
      }
      /** 本地设置（下载目录等，T-11 商品查询用） */
      dir: {
        /** 当前下载目录 */
        getDownloadDir(): Promise<string>
        /** 订阅全局下载目录变更 */
        onDownloadDirChanged(cb: (path: string) => void): () => void
        /** 选择本地 MP4 视频文件，取消返回 null */
        choosePublishVideoFile(): Promise<string | null>
        /** 系统目录选择器，返回新目录或 null（取消） */
        chooseDownloadDir(): Promise<string | null>
        /** 选择账号浏览器缓存目录 */
        chooseProfileDir(): Promise<string | null>
        /** 选择账号下载目录 */
        chooseAccountDownloadDir(): Promise<string | null>
        /** 校验缓存目录内存在 Default 子目录 */
        validateProfileDir(path: string): Promise<{ ok: boolean; path?: string; error?: string }>
        /** 将本机路径解析为规范绝对路径，用于账号隔离校验 */
        canonicalizePath(path: string): Promise<{ ok: boolean; path?: string; error?: string }>
      }
      /** 素材库辅助（缩略图 / 打开文件夹） */
      assets: {
        /** 缩略图文件 → dataURL */
        getThumb(filePath: string): Promise<string | null>
        /** 打开所在文件夹并选中文件 */
        openFolder(filePath: string): Promise<{ ok: boolean; error?: string }>
      }
      /** 百应批量抓取（T-15） */
      baiying: {
        /** 启动批量抓取：按商品ID列表驱动登录视图逐页抓取缓存 */
        batchStart(params: { productIds: string[]; intervalMs?: number }): Promise<BaiyingBatchStartReply>
        /** 停止批量抓取 */
        batchCancel(taskId: string): Promise<{ ok: boolean; alreadyEnded?: boolean }>
        /** 订阅批量抓取事件流，返回取消订阅函数 */
        onBatchEvent(cb: (e: BaiyingBatchEvent) => void): () => void
      }
      /** 头条页面批量解析（Step 10） */
      toutiao: {
        /** 启动批量解析：内置浏览器逐页打开头条链接，探针捕获 vod 直链 */
        batchStart(params: { links: string[]; intervalMs?: number }): Promise<BaiyingBatchStartReply>
        /** 停止批量解析 */
        batchCancel(taskId: string): Promise<{ ok: boolean; alreadyEnded?: boolean }>
        /** 订阅批量解析事件流，返回取消订阅函数 */
        onBatchEvent(cb: (e: ToutiaoBatchEvent) => void): () => void
      }
      /** 账号级登录态（Step 11：一账号一分区 + 指纹 + 加密备份） */
      account: {
        openBrowser(accountId: number, profilePath?: string): Promise<{ ok: boolean; url: string; external?: boolean; profilePath?: string; port?: number; error?: string }>
        closeBrowser(): Promise<{ ok: boolean }>
        openJingxuan(accountId: number, profilePath?: string): Promise<{ ok: boolean; url: string; external?: boolean; profilePath?: string; port?: number; error?: string }>
        pullInfo(params: { accountId: number; douyinId: string }): Promise<AccountUserInfo | { error: string } | null>
        checkLogin(accountId: number): Promise<AccountLoginState>
        exportForEngine(accountId: number): Promise<AccountEngineSession>
        clearLogin(accountId: number): Promise<{ ok: boolean }>
        openMaterialFolder(accountName: string): Promise<{ ok: boolean; error?: string }>
        setBounds(rect: BoundsRect): void
        getState(): Promise<AccountViewState>
        onStateChanged(cb: (state: AccountViewState) => void): () => void
        onLoginStateChanged(cb: (state: AccountLoginState) => void): () => void
        onNeedBounds(cb: () => void): () => void
        onLoadFailed(cb: (e: { code: number; desc: string; url: string }) => void): () => void
      }
      /** 千川账户登录态（Step 11：独立分区多开隔离，SRS A-12） */
      qianchuan: {
        openLogin(qcId: number): Promise<{ ok: boolean; url: string; external?: boolean; profilePath?: string; port?: number; error?: string }>
        checkLogin(qcId: number): Promise<AccountLoginState>
        exportForEngine(qcId: number): Promise<AccountEngineSession>
        clearLogin(qcId: number): Promise<{ ok: boolean }>
        uploadTargets(): Promise<Record<string, unknown>>
        eligibleAssets(accountId: number): Promise<Record<string, unknown>>
        uploadJobs(accountId?: number): Promise<Record<string, unknown>>
        enqueueUpload(params: { accountId: number; assetIds: number[]; qianchuanAccountId: number }): Promise<Record<string, unknown>>
        runUpload(jobId: number): Promise<Record<string, unknown>>
        retrySafeUpload(jobId: number): Promise<Record<string, unknown>>
        reviewUploadAbsent(params: { id: number; note: string }): Promise<Record<string, unknown>>
        reviewUploadPresent(params: { id: number; remoteMaterialId: string }): Promise<Record<string, unknown>>
        quotaConfig(accountId?: number): Promise<Record<string, unknown>>
        setQuotaConfig(params: { accountId: number; perProductVideos: number; perAccountProducts: number; perAccountVideos: number }): Promise<Record<string, unknown>>
      }
      /** 去水印辅助（动态水印去除的框选模板交互） */
      watermark: {
        getFrame(videoPath: string): Promise<{ ok: boolean; dataUrl?: string; error?: string }>
        getFrames(
          videoPath: string,
          n: number
        ): Promise<{ ok: boolean; frames?: WmFrame[]; error?: string }>
        saveTemplate(dataUrl: string): Promise<{ ok: boolean; path?: string; error?: string }>
      }
    }
  }
}
