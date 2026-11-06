# Codex 任务：修「正式加车·联盟榜单唯一定位失败」

项目根目录：`D:\抖音电商\投广管理客户端`  
工作区证据：`D:\software\投广客户端\小g工作区\`（可写 verify）  
可视化会话继续本仓库既有「投广管理客户端」thread；**不要**开 Trae；**不要** push / 打包 / 删除 / 登录验证码。

---

## 1. 证据摘要（已核实）

- 批次 `89823589-04fa-43a7-9ff6-20b349ce0b0c`，主号A，`stage=cart_pending`，`state=failed`
- 商品 ID：`3785655121220863019`  
  标题：`【亮妈妈洗衣膏】亮妈妈衣物清洁膏强力去渍除菌膏小白鞋多效清洁膏`
- 候选：`sources=[allianceRank]`，`sourceKey=allianceRank`，`selectionMode=general`，`eligible=true`
- 抛错链：
  - `engine/browser_cdp.py` `_execute_visible_source_candidates` → `_candidate_card_is_available` 失败  
    →「百应联盟榜单当前可见列表无法唯一定位商品 ID …；已停止加车。」
  - `desktop/src/main/login.ts` 包装为「百应执行结果不确定…禁止重复选品」；registry=`UNCERTAIN`
- diagnostics：
  - `priority-source-allianceRank-20261029-020318-*.json`：`recognizedProductIdCount=20`，`readinessWaitMs≈27722`
  - 同份 HTML dump：**完整标题存在**，所在节点是  
    `<tr class="aurora-table-row …" data-row-key="3785655467091559039">`  
    **周围 `__card___` / `__row___` 计数为 0**（行用 aurora-table-row / `tbody tr`）
  - HTML 文本中**没有**商品 ID `3785655121220863019`（ID 来自 React `__reactProps$`，采集路径能读到；`data-row-key` 是另一套 ID，不能当 productId）
  - 该商品 ID 仅稳定出现在 `category-enrichment-…020329….json` 的 failureDetails（类目未知说明），不在 alliance HTML 明文里——属预期，**不是「采集归因错了」的充分证据**
- 采集选择器（联盟）`BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR` =  
  `'[class*="__card___"], [class*="__row___"], tbody tr, [role="row"], [class~="kol-product-card"]'`
- 加车定位 `_locate_candidate_card` **只**用 `page.locator('[class*="__card___"]')`，标题回退祖先也**只**认 `div[contains(@class,'__card___')]`
- `_execute_visible_source_candidates` 对联盟/热点：`goto(wait_until=commit)` 后**没有**采集同款就绪等待；仅 `_candidate_card_is_available` 内对「找不到卡片」重试约 **5 秒**。而本次联盟采集就绪约 **28 秒**。

该商品 registry 仍为 UNCERTAIN：**不要**指导用户重复选品；本任务只修代码与做静态验证。

---

## 2. 根因（一句话）

正式加车复核打开联盟榜时，定位逻辑只认 `__card___` 卡片，而联盟榜可见商品是表格行（`tbody tr` / `aurora-table-row`），且打开后未等待与采集相同的列表就绪，导致已采集到的 allianceRank 商品无法按商品 ID 唯一定位并安全停加车。

---

## 3. 最小修复方案（只动本缺陷相关）

文件：主要 `engine/browser_cdp.py`（若有极小共享常量抽取可同文件内完成；**不要**改无关 UI/登录）。

### 3.1 对齐定位选择器（必做）

改 `_locate_candidate_card`（及如有必要的标题祖先解析）：

- 卡片/行根节点选择器与优先来源采集对齐，至少覆盖：
  - `[class*="__card___"]`
  - `[class*="__row___"]`
  - `tbody tr`
  - `[role="row"]`
  - `[class~="kol-product-card"]`
- 可直接复用 `BAIYING_PRIORITY_ALLIANCE_ROW_SELECTOR` 或抽一个「可加车行根」常量，避免再写死只认 card。
- 标题 exact 回退时，祖先容器也要能落到上述行/卡片（不要只找 `__card___` 的 div）。
- 仍必须：**优先精确 productId**（经 `_candidate_card_product_ids` / React props）；多命中、ID 冲突继续 fail-closed；禁止把 `data-row-key` / 推广 ID 当商品 ID。

### 3.2 加车复核前等待就绪（必做）

改 `_execute_visible_source_candidates`：对 `hotSpot` / `allianceRank`（非 selectionSquare）在 `goto` + 身份/提示检查之后、调用 `_candidate_card_is_available` 之前：

- 复用或抽出与 `_read_priority_source_product_ids` 相同的就绪等待（列表出现可识别唯一 productId 的可见行，或明确空状态；超时策略与采集一致/接近，勿只靠 5 秒）。
- selectionSquare 现有 `_apply_one_category_and_collect_candidates` 路径保持。

### 3.3 `_candidate_card_is_available`（建议）

- 在已做就绪等待的前提下，可保留短重试；若仍要兜底，勿把「选择器根本匹配不到行」误当成「商品已从榜消失」而不给够渲染时间。
- 错误语义保持 fail-closed：定位失败继续停加车（由上层保留 UNCERTAIN）。

### 3.4 不要做

- 不要 push / 打包 / 删除文件 / 触发登录或验证码
- 不要改 registry 状态、不要清 UNCERTAIN、不要指导重复选品
- 不要大范围重构选品策略 / A-105 筛选 UI
- 不要把「HTML dump 看不到 ID」误修成放宽成只按标题加车且不做 ID 校验

---

## 4. 验收方式

在项目根执行，日志写入：

`D:\software\投广客户端\小g工作区\verify-alliance-locate-20261029\`

至少：

```text
python -m py_compile engine/browser_cdp.py
```

若本缺陷未改 TS/Vue，**不必**强跑 npm typecheck；若动了 TS，再 `npm run typecheck`（或项目既有等价命令）。

可选静态自检（推荐写进 verify 日志）：

- `rg`/`Select-String` 确认 `_locate_candidate_card` 不再只出现单一 `__card___` 定位
- 确认 `_execute_visible_source_candidates` 在联盟/热点路径含就绪等待调用

**不做**：真实预览/正式加车实跑、扫码登录、跨账号批次。

---

## 5. 完成后请输出

1. 改动文件清单（路径 + 简述）
2. 根因一句话（可与上文一致或更精确）
3. verify 目录路径与命令退出码
4. 做了 / 没做 / 风险
5. 是否需人工在 Codex/客户端目视（本缺陷建议：代码静态通过后，由测试在联盟榜正式加车路径回归；开发勿擅自实跑加车）

把 verify 日志落到上述目录；若该目录不存在则创建。
