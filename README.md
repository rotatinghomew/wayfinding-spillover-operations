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

使用 Python 3.11 及标准库。`python3 inspect_inputs.py` 读取原始资料并列出记录数；`python3 -m unittest discover -s tests` 检查输入资料。检查不计算或发布业务结论。
