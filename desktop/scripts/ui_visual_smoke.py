"""在隔离的本地网页中检查 Electron 渲染界面，不调用真实 preload/API。"""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(r"D:\software\codex\touguang-ui-smoke")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass
MOCK_API = r"""
(() => {
  const account = {
    id: 1, name: '演示账号 A', nickname: '演示昵称', douyin_id: 'demo_001', qianchuan_id: 'QC001',
    selection_category: '食品：休闲零食；水果', success_count: 12, category: '食品', phone: '138****0000',
    status: 'active', material_count: 24, publish_rate: 80, karl_rate: 70, ad_count: 3,
    groups: ['食品组'], follower_count: 1800, aweme_count: 35
  };
  const fallback = () => Promise.resolve({ok:true, rows:[], accounts:[], groups:[], tags:[], categories:[], items:[], total:0});
  const listeners = [];
  let nextTask = 1;
  function dataFor(request) {
    const args = request.args || [];
    if (request.script === 'account_manager.py') {
      if (args.includes('stats')) return {account_count: 1, grouped_count: 1, material_count: 24, pending_publish: 0, qianchuan_count: 1};
      if (args.includes('account-list')) return {accounts: [account]};
      if (args.includes('group-list')) return {groups: []};
      if (args.includes('qianchuan-list')) return {qianchuan_accounts: []};
    }
    if (request.script === 'db.py') return {rows:[{n:0}]};
    return {ok:true, rows:[], accounts:[], groups:[], tags:[], categories:[], products:[], items:[], total:0};
  }
  const engine = {
    onEvent(callback) {listeners.push(callback); return () => {const i=listeners.indexOf(callback); if(i>=0) listeners.splice(i,1)}},
    run(request) {
      const taskId = `mock-${nextTask++}`;
      setTimeout(() => listeners.slice().forEach(callback => callback({taskId,type:'result',data:dataFor(request)})), 10);
      return Promise.resolve({taskId});
    }
  };
  const specific = {
    engine,
    appPaths: {get: async () => ({databasePath:'D:\\mock\\ad_client.db', databaseExists:true})},
    appInfo: {getVersion: async () => '0.0.10'},
    dir: {getDownloadDir: async () => 'D:\\演示素材', onDownloadDirChanged: () => () => {}},
    browser: {close: async () => ({ok:true}), getTargets: async () => []},
    login: {check: async () => ({targets:[], encryptionAvailable:false, encryptionBackend:'模拟'}), onStateChanged: () => () => {}},
    publish: {onProgress: () => () => {}},
    selectionSchedule: {onDue: () => () => {}, get: async () => ({enabled:false,pendingDue:null})},
    qianchuan: {
      apiStatus: async () => ({configured:true,authorized:true,appId:'1880896897289216',advertiserIds:['16761573861161'],callback:'http://127.0.0.1:8899/callback',expiresAt:Date.now()+3600000}),
      apiVideos: async () => ({videos:[{videoId:'mock-video',awemeItemId:'7391234567891234567',title:'模拟商品视频',url:'https://cdn.example.com/video.mp4',duration:10}],hasMore:false,cursor:'',totalReturned:1})
    }
  };
  const proxyNamespace = new Proxy({}, {get: (_, method) =>
    String(method).startsWith('on') ? () => () => {} : fallback
  });
  window.api = new Proxy(specific, {get: (target, name) => target[name] || proxyNamespace});
})();
"""


def main() -> None:
    renderer = ROOT / "out" / "renderer"
    if not (renderer / "index.html").exists():
        raise SystemExit("先在 desktop 目录执行 npm run build")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    handler = partial(QuietHandler, directory=str(renderer))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors: list[str] = []
    results: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
            context = browser.new_context(viewport={"width": 1255, "height": 820}, device_scale_factor=1)
            context.add_init_script(MOCK_API)
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(f"http://127.0.0.1:{server.server_port}/") else route.abort())
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
            page.wait_for_selector(".shell")

            screens = [
                ("看板", "投放看板", ".dashboard"),
                ("账号", "账号管理", ".acct-view"),
                ("素材", "素材与工具", ".lib"),
                ("日志", "选品日志", ".logs-page"),
                ("千川", "千川管理", ".qc-page"),
                ("工具", "连接与工具", ".content"),
                ("选品", "选品设置", ".selection-page"),
                ("发布", "发布队列", ".publish-page"),
            ]
            for number, (name, nav, selector) in enumerate(screens, 1):
                if number > 1:
                    page.get_by_role("button", name=nav, exact=True).first.click()
                page.locator(selector).wait_for(timeout=5000)
                page.wait_for_timeout(80)
                overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
                if overflow:
                    errors.append(f"{name}：页面横向溢出")
                page.screenshot(path=str(OUTPUT / f"{number:02d}-{name}.png"))
                results.append(f"{name}: 已显示，横向溢出={overflow}")
                if nav == "投放看板":
                    page.get_by_role("button", name="刷新数据").click()
                elif nav == "素材与工具":
                    page.get_by_role("button", name="按账号分组").click()
                    page.get_by_role("button", name="全部素材").click()
                elif nav == "选品日志":
                    page.get_by_role("button", name="筛选", exact=True).click()
                    page.get_by_role("button", name="重置", exact=True).click()
                elif nav == "发布队列":
                    page.get_by_role("button", name="调度设置", exact=True).click()
                    page.get_by_role("button", name="刷新队列", exact=True).click()

            page.get_by_role("button", name="商品查询", exact=True).click()
            page.wait_for_timeout(250)
            if page.locator(".query").count():
                results.append("商品查询: 已显示")
            else:
                errors.append("商品查询：未渲染主视图")
            page.screenshot(path=str(OUTPUT / "09-商品查询.png"))
            if page.locator(".nav-item[aria-current='page']").count():
                errors.append("商品查询：侧栏错误高亮旧页面")
            page.locator(".search-bar .id-input").fill("1234567890123456789")
            page.get_by_role("button", name="搜索", exact=True).click()
            try:
                page.locator(".qc-panel summary").click()
                page.locator(".qc-panel").get_by_role("textbox", name="商品 ID", exact=True).fill("3851408796480045486")
                page.get_by_role("button", name="查询视频", exact=True).click()
                page.get_by_text("模拟商品视频").wait_for(timeout=3000)
                if not page.get_by_role("button", name="下载验证").is_enabled():
                    errors.append("千川查询：视频地址返回后下载按钮仍不可用")
                page.screenshot(path=str(OUTPUT / "09b-千川视频查询.png"))
                results.append("千川商品视频查询: 入口、查询结果与下载按钮可用")
            except Exception as exc:
                errors.append(f"千川查询交互：{exc}")

            try:
                page.get_by_role("button", name="账号管理", exact=True).click()
                page.get_by_role("button", name="账号分组", exact=True).click()
                page.get_by_role("button", name="千川账户", exact=True).click()
                page.get_by_role("button", name="账号列表", exact=True).click()
                page.get_by_role("button", name="新建账号", exact=True).click()
                page.get_by_role("heading", name="新建账号").wait_for(timeout=5000)
                results.append("账号管理: 三个标签及新建弹窗可操作")
                page.screenshot(path=str(OUTPUT / "10-账号弹窗.png"))
                page.get_by_role("button", name="取消", exact=True).click()
                page.set_viewport_size({"width": 1024, "height": 700})
                page.screenshot(path=str(OUTPUT / "11-最小窗口账号.png"))
                if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                    errors.append("1024 宽窗口：页面横向溢出")
                if not page.locator(".compact-card .compact-actions button").first.is_visible():
                    errors.append("1024 宽窗口：账号主要操作不可见")
            except Exception as exc:
                errors.append(f"账号管理交互：{exc}")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print("\n".join(results))
    print(f"截图目录：{OUTPUT}")
    if errors:
        raise SystemExit("界面烟测问题：\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
