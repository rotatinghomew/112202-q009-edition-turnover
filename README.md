# 限量版柜面流转记录

`data/` 为离线合成业务记录。文件均为 UTF-8 的 Tab 分隔表，首行是列名，逗号分隔集合，`-` 表示不适用，`?` 表示未取得，不把它们当作零。标识符只在各表声明的归属范围内有效。

## 运行与资料核对

使用 Python 3.11。只需 Python 3.11 标准库。

```sh
python3 inspect_packet.py
python3 -m unittest discover -s tests

# 时点看板（默认知识截止到资料中的最大 received/day=12）
python3 cabinet_audit.py report
python3 cabinet_audit.py report --at 9
python3 cabinet_audit.py vouchers --at 8 --shift SH3 --format json
python3 cabinet_audit.py scenarios RQ4 --at 8
python3 cabinet_audit.py cases --at 12
python3 cabinet_audit.py policy --at 12
python3 cabinet_audit.py shifts
python3 cabinet_audit.py replay
```

`inspect_packet.py` 只检查表头和记录形状并显示行数。`cabinet_audit.py`
提供只读的时点匹配、指标区间、凭证情景、联合主案和历史回演；它不会把
后来收到的记录回放到更早的 `--at` 时点。

## 分析口径

- 唯一库存/销量单位是发行编号 `serial`。换渠道透视时，同一编号仍只计一次；
  事件站点用于追踪跨柜历程，不把同一件在南北柜各加一件。
- `--at d` 表示只使用 `received <= d` 的交易/回执，以及 `day <= d` 的复核和班次。
  交易的实际发生日 `day` 与可知日 `received` 分开保留。
- `unresolved_edges.tsv` 的每个 `event_key` 是一个未知编号事件，候选不带概率。
  程序枚举全部候选组合，再排除会让同一编号在无撤销/退货间隔时承担两个互斥
  出库订单的匹配。被排除组合仍在 JSON 的 `all_world_assignments` 中留痕；
  若真实回执直接确认该组合，应转为硬冲突主案，而不是假装该回执不存在。
- `register.grade` 只直接覆盖 5 个编号。看板同时给 `direct_grade=未登记`
  和依据“已登记样本中 etched=standard、stone=premium”得到的推断价带；
  两个口径不混用。
- 复核表的全部记录都恰好位于首次上架后第 5 天，因此 `review_board` 以 5 天
  作为资料中可观察到的复核节奏。这不是 `policies.delay` 的替代解释；
  建议单 SLA 仍按 `issued + policies.delay` 单独判断。
- 资料没有记录某次 `legit` 复核是否实际导致搁置，因此无法从现有字段计算
  `max_false_hold` 的真实比率；不能把随机复核的 legit 比例直接当作误搁率。

## 下一班从哪里接手

先读 `HANDOFF.md`，再用：

```sh
python3 cabinet_audit.py cases --at 12
python3 cabinet_audit.py direction --at 12
python3 cabinet_audit.py vouchers --at 12 --format json
python3 cabinet_audit.py item N-035 --at 12
```

待回执、待决定和可新发凭证是不同状态：`pending_replies` / `vouchers`
中的 `receipt_window_open`、`receipt_due_today` 表示等回执；
`expired_without_confirmation` 才进入待决定；未发的 RQ5 是当前唯一仍有
未覆盖目标的候选凭证。重叠请求按并集计算，不能把 RQ3 与 RQ5 对 E-U19
的风险/库存影响相加。

## 输入表

- `transactions.tsv`：serial 在发行档内编号。day/received 分别为发生及收到日。display/ pause 为上/撤柜；sell 为销售；void/return 撤销或退货，使用相同 order；reserve/fulfil 为预留和交付。 列顺序为 `event`、`serial`、`edition`、`site`、`day`、`received`、`kind`、`order`。
- `reviews.tsv`：basis=clue 为定向挑查，rotation 是按当天轮查名单；结果仅对应这枚章。 列顺序为 `check`、`serial`、`basis`、`day`、`outcome`。
- `policies.tsv`：max_false_hold 为核查中有效交付被错误搁置的最高比例；按发行档规定当班容量和最晚复核天数。 列顺序为 `edition`、`max_false_hold`、`capacity`、`delay`。
- `commitments.tsv`：尚未交付的客户约定、截止及人员归口。 列顺序为 `order`、`serial`、`promised`、`deadline`、`personnel`。
- `suggestions.tsv`：上一班处置建议状态。closed 为已关闭，assigned 仍有人承接。 列顺序为 `serial`、`issued`、`owner`、`state`。
- `register.tsv`：mark 是实物包装上的打印串，可能因重印共用；custody 为最初柜台；不能用 mark 替代发行编号。 列为 `serial`、`edition`、`mark`、`custody`、`grade`、`created`。
- `unresolved_edges.tsv`：未知编号事件的一种可行对应，event_key 相同表示同一条柜面凭证；原始候选不带概率。 列为 `evidence`、`event_key`、`candidate`、`site`、`day`、`received`、`kind`、`order`。
- `requests.tsv`：targets 为此凭证在独立核查时能区分的报送事件；turnaround 如 1,2 是可能的最快和最慢回执日数；班组有对应访问权限才能发询。 列为 `request`、`targets`、`available`、`turnaround`、`minutes`、`access`。
- `actual_replies.tsv`：issued 和 received 分开；waiting 暂无证据；工程台账里同一调取可以制成多行回执。 列为 `reply`、`request`、`issued`、`received`、`outcome`、`event_key`、`serial`。
- `shift_capacity.tsv`：sampled=rotation 记录已知轮查时段，clue 是选择性复核；inclusion 是轮查设计比例或未知。 列为 `shift`、`day`、`access`、`minutes`、`sampled`、`inclusion`。
