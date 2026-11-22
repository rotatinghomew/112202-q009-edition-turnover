# 限量版柜面流转记录

`data/` 为离线合成业务记录。文件均为 UTF-8 的 Tab 分隔表，首行是列名，逗号分隔集合，`-` 表示不适用，`?` 表示未取得，不把它们当作零。标识符只在各表声明的归属范围内有效。

## 运行与资料核对

使用 Python 3.11。只需 Python 3.11 标准库。

```sh
python3 inspect_packet.py
python3 -m unittest discover -s tests
```

前一条命令只检查表头和记录形状并显示行数。可传入另一目录读取同格式记录。当前没有分析、决策或业务处理结果。

## 输入表

- `transactions.tsv`：serial 在发行档内编号。day/received 分别为发生及收到日。display/ pause 为上/撤柜；sell 为销售；void/return 撤销或退货，使用相同 order；reserve/fulfil 为预留和交付。 列顺序为 `event`、`serial`、`edition`、`site`、`day`、`received`、`kind`、`order`。
- `reviews.tsv`：basis=clue 为定向挑查，rotation 是按当天轮查名单；结果仅对应这枚章。 列顺序为 `check`、`serial`、`basis`、`day`、`outcome`。
- `policies.tsv`：max_false_hold 为核查中有效交付被错误搁置的最高比例；按发行档规定当班容量和最晚复核天数。 列顺序为 `edition`、`max_false_hold`、`capacity`、`delay`。
- `commitments.tsv`：尚未交付的客户约定、截止及人员归口。 列顺序为 `order`、`serial`、`promised`、`deadline`、`personnel`。
- `suggestions.tsv`：上一班处置建议状态。closed 为已关闭，assigned 仍有人承接。 列顺序为 `serial`、`issued`、`owner`、`state`。
