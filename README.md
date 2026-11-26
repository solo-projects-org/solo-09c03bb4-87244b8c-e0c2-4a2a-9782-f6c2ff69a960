# 港区水深基准改正表 + 航道复测断面最浅水深（Flask + SQLite）

海图组航道复测用两部分能力：

1. **基准改正表**：录入联测单后核算每个零点相对港区基准（D0，改正量固定 0）
   的稳健改正范围，要求改正表在**日后作废任意一张联测单后仍然成立**。
2. **断面最浅水深（本次新增）**：网页上传 CSV 整批替换原始测深，按现行改正表
   计算每个断面的保守最浅水深与并列最浅测点；零点缺改正数据或不可合图的断面
   标 **待核**；联测单更新后只增量重算受影响断面。

---

## 一、断面核算口径

CSV 每行四列（首行中英文表头可选，支持 UTF-8/带 BOM/GBK、逗号/制表符/分号）：

```
断面号,测点号,零点,原水深
SEC01,P01,D0,2000
SEC01,P02,Z1,2102
```

- 原水深：从所属零点向下计的**整数毫米**（非负）。
- **同一断面内测点号不得重复**（跨断面允许重号）；任何一行校验失败都整批拒绝，
  现行测深与断面结果不变。
- 每笔化算水深：`corrected = raw_depth − rob_hi(零点)`，`rob_hi` 取现行生效
  改正表快照中该零点的**稳健上界**。
- 断面最浅值 = 全部化算水深的最小值；所有取到最小值的测点号都列入
  `shallowest_points`（并列）。
- 只有断面涉及的**全部零点**都
  - 在改正表中存在（**有改正数据**），且
  - 状态为 `OK`（稳健范围有界、跨度 ≤ `SPAN_LIMIT`，**可合图**），

  才给出最浅值；否则断面 `status="PENDING"`（页面显示 **待核**），并在
  `missing_zeros` / `unfit_zeros` / `problems` 中分别列出
  “缺少改正数据”（改正表无此零点）与“不可合图”（状态 PENDING/FAIL 及原因）。

### 增量重算

补交/换报联测单后，比较新旧改正表每个零点的指纹
`(可合图布尔, rob_hi)`（`app/sections.py: changed_zero_points`）：

- 只有指纹变化的零点所被使用的断面才完整重算，**包括此前待核的断面**；
  响应/快照中的 `changed_zeros`、`recomputed_sections` 给出本次变化零点与
  重算断面，每个断面带 `recomputed` 标记。
- 仅资格细类变化（如“查无数据 MISSING”变为“已挂单但待联测 PENDING”，
  可合图布尔与 rob_hi 都没变）时不重算最浅值，只刷新待核零点/原因
  （`blockers_refreshed_sections`），保证页面措辞与现行改正表一致。

### 同一已生效状态 + 失败沿用旧状态

- `POST /api/updates` 支持在**一个 SQLite 事务**内同时整批替换测深 CSV 与
  提交联测单（补交/换报），改正表快照与断面快照在同一事务提交，断面结果必然
  对应同一份已生效的原始测深与改正表。
- 两个并发请求由 `BEGIN IMMEDIATE` 串行化，后到者在锁内读到前者已提交状态后
  再做增量合并，不会出现“新测深配旧改正表”的断面。
- CSV 校验错误（400）、联测单矛盾（409）、SQL/保存错误（500）一律回滚，
  **沿用最近一次成功生效的测深、联测单、改正表快照与断面结果**。

---

## 二、基准改正表核算口径

每张联测单填：编号、零点 A、零点 B、闭区间 `[lo, hi]`（整数毫米），含义

```
lo ≤ h_B − h_A ≤ hi
```

其中 `h_X` 为零点 X 相对港区基准 D0 的改正量（`h_D0 = 0`）。单据建模为
差分约束有向图（A→B 权 hi，B→A 权 −lo）：

- **矛盾检测**：超源 Bellman-Ford 找负环，负环即高差互相矛盾的冲突闭环，
  页面/接口列出闭环涉及的零点、单据编号与闭环推出的严格上界（负值）。
- **现行改正范围**：D0 正向最短路给上界、反向图最短路给下界。
- **稳健改正范围**（核心需求）：覆盖“撤掉任一张联测单后所有可能改正量”
  的最窄闭区间，即对所有撤单情形取 `min 下界 / max 上界`；撤单后与基准
  断开则该方向无界。
- **合图判定**：只有稳健范围有界且跨度 ≤ 20 mm（`SPAN_LIMIT` 可调）才可
  合图。原本未连到基准的标 **待联测**；其余不达标时指出撤哪张单会断开基准
  （Tarjan 桥）、分别撤哪张单得到最小下界/最大上界。
- 只对紧弧所属单据做撤单 SPFA；`tests/test_engine.py` 用暴力逐单撤除法
  对拍 60 个随机图。

---

## 三、接口

### 页面 / 表单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 单页：CSV 上传、断面结果、现行改正表、联测单（同一读事务取齐） |
| GET | `/sections` | 跳转到 `/#sections` |
| POST | `/soundings/upload` | 网页表单 CSV 整批替换（PRG 跳转，无 JS 可用） |
| POST | `/updates/submit` | 网页表单：CSV + 联测单 JSON 同一事务提交（PRG） |

### 断面 / 测深 JSON

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sections` | 断面结果快照（`sections` 为列表，含 `changed_zeros` 等增量元数据） |
| GET | `/api/sections/<断面号>` | 单个断面结果 |
| GET | `/api/soundings` | 现行原始测深行 |
| GET | `/api/soundings.csv` | 下载现行 CSV（无原文时由行回生成） |
| POST | `/api/soundings` | 整批替换：JSON `{"csv":"..."}` / `{"soundings":[...]}`、原始 `text/csv` |
| POST | `/api/soundings/upload` | multipart 上传，文件字段 `file`/`csv`（别名 `/api/sections` POST、`/api/csv/upload`、`/api/upload_csv` 等） |
| POST | `/api/updates` | **联合提交**：JSON/multipart，可同时带 `csv`/`file` 与 `sheets:[...]`、`replace_ids:[...]` |

断面 JSON 同时给多组同义字段以方便取用：`section_id/section/id`、
`shallowest_depth/min_depth/conservative_depth/depth_mm`、
`shallowest_points/tied_points/min_points`、
`missing_zeros/lacking_zeros`、`unfit_zeros/not_composable_zeros`、
`status "OK"|"PENDING"` + `status_label "可查最浅"|"待核"` +
`verdict "SHOWN"|"CHECK"` + `pending/needs_check`。

### 改正表 / 联测单（原有）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/table` | 现行改正表 JSON 快照 |
| GET | `/api/sheets` | 现行联测单 |
| POST | `/api/sheets` | 补交，body `{"sheets":[{sid,point_a,point_b,lo,hi}]}`；成功响应附最新断面结果 |
| PUT | `/api/sheets/<sid>` | 按号换报（编号须已存在）；同样触发断面增量重算 |
| POST | `/api/sample` | 空库载入样例联测单 + 样例测深（非空库拒绝） |
| GET | `/healthz` | 健康检查 |

矛盾返回 HTTP 409 并带 `contradictions`；输入错误 400；保存失败 500 且
数据不变。

---

## 四、Docker Compose 启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
# 容器首次启动自动载入样例（LOAD_SAMPLE_ON_EMPTY=1），数据存命名卷 datum-data
```

环境变量：`DATUM_DB`（默认 `/data/datum.db`）、`DATUM_POINT`（默认 `D0`）、
`SPAN_LIMIT`（默认 `20`）、`LOAD_SAMPLE_ON_EMPTY`（默认 `1`）、`PORT`（本地
直跑时，默认 8000）。

不用 Docker 时：

```bash
pip install -r requirements.txt
python -m app.bootstrap            # 可选：建库并载样例
gunicorn --bind 0.0.0.0:8000 "app.webapp:create_app()"
# 或：python -m app.webapp
```

---

## 五、样例

9 张联测单 / 9 个零点 + 7 个样例断面（15 笔测深）：

- `SEC01` D0/Z1 可合图：`2000 − 0 = 2000`、`2102 − 102 = 2000`，P01/P02 并列最浅；
- `SEC02` Z2 可合图：Q01 `1599 − 99 = 1500` 为唯一最浅；
- `SEC03` Z3 稳健跨度 34 mm 不合图 → 待核；
- `SEC04` Z4 唯一通道 S06 是桥 → 待核；
- `SEC05` Z5/Z6 不连基准 → 待核，列两个不可合图零点；
- `SEC06` Z9 无任何联测单 → 待核，列为缺少改正数据；
- `SEC07` D0/Z1/Z2 混用，正常出值。

给 Z9 补两张紧复测单（如 D0–Z9 `[40,42]` ×2）后，`SEC06` 自动从待核变为
可查最浅 `1100 − 42 = 1058`，且响应显示只有 `SEC06` 被重算。

---

## 六、自测

```bash
python tests/test_engine.py             # 纯标准库：引擎 + 暴力对拍
PYTHONPATH=<flask 所在目录> python tests/test_storage_web.py
PYTHONPATH=<flask 所在目录> python tests/test_sections.py
# 或：PYTHONPATH=<依赖目录> python -m pytest tests/ -q
```

当前：`test_engine.py`、`test_storage_web.py`（10 项）、
`test_sections.py`（14 项，含 CSV 解析/查重、最浅并列、待核原因、
严格增量集合与细类刷新、事务原子性、PRG 表单）全部通过；
`pytest tests/ -q` 共 34 项通过。另用 gunicorn 双 worker 实测了
multipart 上传、并发混合写入（24 请求全部 201、无锁错误）与重启持久化。

---

## 七、目录

```
app/engine.py       差分约束/最短路/桥/稳健区间核算（纯标准库），末尾再导出断面类型
app/sections.py     测深 CSV 解析、断面最浅/并列/待核、变化零点与增量重算（纯标准库）
app/survey.py       兼容别名模块（转发 app.sections）
app/storage.py      SQLite 事务、改正表/断面快照、联合提交与增量落库
app/webapp.py       Flask 路由（页面 + JSON API + PRG 表单）
app/templates/      单页中文界面（上传、断面表、改正表、联测单）
app/sample_data.py  样例联测单 + 样例测深 CSV
app/bootstrap.py    容器启动引导
tests/              引擎对拍 + 存储/Web + 断面测试
```
