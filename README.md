# wayfinding-spillover

传统体育展区各入口的轮换单、队伍观察和现场启用记录。 其中人物代号、机构、交易及历史回执均为合成资料。

## 数据约定

`data/layout.json` 逐一解释业务表及 CSV 列顺序，`data/rules.json` 记录运营口径。CSV 使用 UTF-8、首行为列名；空白字段表示未提供或不适用，`+` 隔开多值字段。历史记录的业务时刻与送交时刻含义不同，按各表说明解释。

- `rotation.csv`：事前分配单；分钟自场馆当日零点起，N、E 为两个入口。
- `signs.csv`：实际换牌记录；同编号较大修订替代较早版本。
- `parties.csv`：每条为一个观测队伍，last_seen 的含义为最后有效观察分钟。
- `queues.csv`：在相应分钟起的两分钟观测；空白表示覆盖断档。
- `links.csv`：共同可达通道有效区间与通告时间，计划外通道另列；同号最高可见修订生效。
- `dispatch.csv`：运营提交的各入口安排及关联证据分配块、邻接；状态是输入事实而非分析决定。
- `next_round_constraints.csv`：指定日期口径下可随机轮换的时段。shared_buffer 引用同一个只能占用一次的共用隔离资源。
- `next_round_signatures.csv`：志愿班次可出借时间及既有签认；incompatible 表示不能再次配置该班。

## 本地检查

使用 Python 3.11 及标准库。`python3 inspect_inputs.py` 读取原始资料并列出记录数；`python3 -m unittest discover -s tests` 检查输入资料与分析口径。检查不计算或发布业务结论。

## 轮换设计与证据分析（wayfinding 包）

### 要回答的三个问题与估计单位

- **主效应**：入口自己换牌（old→new）对本口队伍的效果（`main_N`、`main_E`）。
- **外溢**：邻口换牌对本口队伍的影响（`spill_N`：E 换牌→N；`spill_E`：N 换牌→E）。
- **交互**：两口同时换牌与分别换牌的差别（`interaction`），需要四类组合（old/old、old/new、new/old、new/new）在**同一个分配块**内各出现一次。

估计与复制单位是“独立轮换块”（同日四轮），不是入口或队伍样本量；门槛 `min_comparable_blocks=4` 来自 `rules.json`。把两个相邻入口分别填成对照不能凑出一个交互块。

### 排期计划器 `wayfinding/planner.py`

读取 `next_round_constraints.csv` 与 `next_round_signatures.csv`，在硬约束内枚举整张轮换单：

- `signed` 时段固定不重抽；`incompatible` 时段不可再配置；`offered` 是未签认的出借，同时给出 `baseline`（拒绝）与 `offer_accepted`（接受）两套口径。
- `shared_buffer`：`R` 前缀为同日两个时段共用、一次只能占用一次的隔离资源（引用同一标识的两个时段至多一个换牌）；`B` 前缀各时段独有。
- `allowed` 集合约束可放组合（如 C1/C6 前两时段 N 必留旧牌）；`minimum_coverage=without_E` 不阻止排期，但 E 口结果类差别只给 `coverage` 级支持。

逐块支持等级：`full`（所需层级/四类组合齐备且位置允许随机互换）、`coverage`（组合齐备但结果观察覆盖不足）、`partial`（层级不全或位置全部已固定）、`absent`（连对照层级都没有）。

### 历史分析器 `wayfinding/analyze.py`

- 一律按**事前分配**做 ITT：撤牌（`signs.csv` 的 `R-` 事件）只登记偏离，不因拥堵撤牌后实际旧牌而重算分组。
- 跨轮滞留按“连接的两轮都扣支持”处理：队伍 `arrival`（无 arrival 时用 `last_seen`）达到同日下一轮起点，则滞留轮与后继轮同时失去干净资格；不跨日传播。
- 计划外通道（`links.csv` 中 `declared=no`，同号取最高修订）只扣除外溢/交互类比较；计划内 N-E 通告属设计口径。
- 队列空白读数不插补，只影响队列指标并标记 `incomplete_observation`；`closed`（闭馆确认未到达）与 `lost`（观察中断）分开计数、互不替代，均不进入到场时延均值。
- 证据不足三分类：`missing_combo`（缺组合）、`incomplete_observation`（观察不全）、`confounded`（有观测但主效应/外溢分不清）。主口径只用 clean 块，relaxed 口径另列仅断档块；90% 区间用块级 t 分布，区间半宽对照 `min_arrival_gain_minutes` 的事先业务门槛。

### 变更台账 `wayfinding/ledger.py`

局部签收、撤回、班次不可配置、约束修订均为只增事件（`ledger/` 下 CSV）。已签时段继续占用人员，不能按更有利组合事后重抽；`replay` 可回到任意事件序号重排整张单，`timeline` 列出每种比较在哪次变更后失去支持。剩余时段无法区分两种影响时，不受影响的部分可继续执行，但不再对外承诺原来的整轮比较。

### 命令

```bash
python3 -m wayfinding.cli plan                       # 两套口径的整张轮换单与逐差别审计
python3 -m wayfinding.cli analyze                    # 历史 12 块的 ITT 重估
python3 -m wayfinding.cli export --out output/       # 导出轮换单 CSV、设计审计与历史报告 JSON
python3 ledger/make_demo.py                          # 生成演示台账
python3 -m wayfinding.cli replay --ledger ledger/events_demo.csv
python3 -m wayfinding.cli timeline --ledger ledger/events_demo.csv
```

### 当前数据下的结论（工具输出，非业务承诺）

下一期 12 个候选块：主 N 有 10 个 full 块、主 E 10 个（另 2 块仅 coverage 级）、外溢到 N 12 个、外溢到 E 8 个、交互 9 个，均超过 4 块门槛。受限点为：10-21（C0）一固定一不可配置，交互缺一类组合且全天 without_E；10-23/10-25 的 R 类共用隔离窗口每天只能换两个时段，交互不可识别；10-27（C6）E 侧结果只有 coverage 级。

历史 12 块在严格口径下无差别同时满足“4 个 clean 块＋1 分钟精度门槛”：主 E 与外溢到 N 各有 3 个 clean 块（低于块数门槛，且区间半宽约 2.5–2.8 分钟，未达精度门槛）；主 N、外溢到 E、交互的对照层级均被跨轮滞留/计划外通道污染。这正是下一期需要补足独立轮换块、并把隔离窗口与滞留观察纳入排期的原因。

