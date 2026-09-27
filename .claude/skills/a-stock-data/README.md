# a-stock-data（随仓库附带的 Claude Code Skill）

来源：https://github.com/silencebin1993/a-stock-data （fork 自 https://github.com/simonlin1212/a-stock-data ，
Apache-2.0，许可证见同目录 LICENSE），版本 V3.10.0，提交 f814dcf（2026-09-25）。`SKILL.md` 原样复制，没有改动。

用途：在这个仓库里用 Claude Code 时，需要临时取某类 A 股数据（研报、宏观、期货、可转债等量化助手还没接的数据），
Claude 会按这份 Skill 里的现成代码去取。

量化助手本身已经把其中几项接成了数据源 `quant_web/providers/astock_src.py`（深交所交易日历、中证/国证指数成分和权重、
中证指数估值、东财研报、同花顺强势股题材、东财机构调研），在“数据源”页面能看到和测试。

更新：从 a-stock-data 仓库重新复制 `SKILL.md`，并改上面的版本号和提交号。
