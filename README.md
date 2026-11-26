# 港区水深基准改正表（Flask + SQLite）

海图组用：录入联测单后自动核算每个零点相对港区基准（D0，改正量固定为 0）
的水深基准改正范围，要求改正表在**日后作废任意一张联测单后仍然成立**。

## 核算口径

每张联测单填：编号、零点 A、零点 B、闭区间 `[lo, hi]`（整数毫米），含义

```
lo ≤ h_B − h_A ≤ hi
```

其中 `h_X` 为零点 X 相对港区基准 D0 的改正量（`h_D0 = 0`）。单据建模为
差分约束有向图（A→B 权 hi，B→A 权 −lo）：

- **矛盾检测**：超源 Bellman-Ford 找负环，负环即高差互相矛盾的冲突闭环，
  页面/接口列出闭环涉及的零点、单据编号与闭环推出的严格上界（负值）。
- **现行改正范围**：D0 正向最短路给上界、反向图最短路给下界。
- **稳健改正范围**（核心需求）：覆盖"撤掉任一张联测单后所有可能改正量"
  的最窄闭区间，即对所有撤单情形取 `min 下界 / max 上界`；撤单后与基准
  断开则该方向无界。
- **合图判定**：只有稳健范围有界且跨度 ≤ 20 mm（`SPAN_LIMIT` 可调）才可
  合图。原本未连到基准的标 **待联测**；其余不达标时指出：
  - 撤哪张单会断开基准（无向图 Tarjan 桥，平行复测单按编号分别计、互不为桥）；
  - 分别撤哪张单得到最小下界 / 最大上界。
- **避免逐单重算全港**：只对紧弧（出现在某条最短路上的弧）所属单据做撤单
  SPFA；桥单据的影响由一次 Tarjan 直接给出。`tests/test_engine.py` 用暴力
  逐单撤除法对拍 60 个随机图，结果与优化实现逐项一致。

## 数据与事务

- SQLite（WAL 模式），单据表 `sheets` + 改正表 JSON 快照 `snapshots`。
- 补单/换报在**单个事务**内完成：写新单据 → 核算 → 无矛盾才连同改正表快照
  一起提交；矛盾或保存失败一律回滚，**现行单据与现行改正表原样保留**，
  核算期间其他请求仍可读到旧快照（连接按请求开闭，WAL 快照读不阻塞）。
- 同一对零点的复测单可同值，按编号分别存储、分别参与核算。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 现行改正表 + 单据页面（含补单/换报表单） |
| GET | `/api/table` | 现行改正表 JSON 快照 |
| GET | `/api/sheets` | 现行联测单 |
| POST | `/api/sheets` | 补交，body `{"sheets":[{sid,point_a,point_b,lo,hi}]}` |
| PUT | `/api/sheets/<sid>` | 按号换报（编号须已存在），body 为单据字段 |
| POST | `/api/sample` | 空库时载入样例（非空库拒绝） |
| GET | `/healthz` | 健康检查 |

矛盾返回 HTTP 409 并带 `contradictions`；输入错误 400；保存失败 500 且
数据不变。

## Docker Compose 启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
# 容器首次启动自动载入样例（LOAD_SAMPLE_ON_EMPTY=1），数据存命名卷 datum-data
```

环境变量：`DATUM_DB`（默认 `/data/datum.db`）、`DATUM_POINT`（默认 `D0`）、
`SPAN_LIMIT`（默认 `20`）、`LOAD_SAMPLE_ON_EMPTY`（默认 `1`）。

不用 Docker 时：

```bash
pip install -r requirements.txt
python -m app.bootstrap            # 可选：建库并载样例
gunicorn --bind 0.0.0.0:8000 "app.webapp:create_app()"
```

## 样例覆盖的情形

9 张单 / 9 个零点：Z1 复测双单冗余（可合图）、Z2 多路径冗余（可合图）、
Z3 松单致稳健跨度 34 mm（不合图，给出极值单号）、Z4 唯一通道 S06 是桥
（撤 S06 即断开基准，无界）、Z5/Z6 与 Z7/Z8 不连基准（待联测）。

## 自测

```bash
python tests/test_engine.py            # 纯标准库，含暴力对拍
PYTHONPATH=<flask 所在目录> python tests/test_storage_web.py
```

## 目录

```
app/engine.py       差分约束/最短路/桥/稳健区间核算（纯标准库）
app/storage.py      SQLite 事务与快照
app/webapp.py       Flask 路由
app/templates/      单页中文界面
app/sample_data.py  样例联测单
app/bootstrap.py    容器启动引导
tests/              引擎对拍 + 存储/Web 测试
```
