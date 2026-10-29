"""Soft brevity guidance; source evidence and generated text are never sliced."""
from backend.utils.event_time import EVENT_TIME_POLICY

STRATEGY_BREVITY_POLICY = """摘要篇幅：未另行指定字数时，以100–200字、最多4条短句为目标。
只写本轮结论、关键价位/条件、实际执行结果与下一步；无变化时1–2句即可。
合并重复论证，不复述逐步思考、指标清单、原始JSON和完整交易账本。
区分计划与成交；未解决的交易核验引用应保留。目标是语义压缩，不得为凑字数省略关键风险条件或截断句子。""" + '\n' + EVENT_TIME_POLICY

MEMORY_BREVITY_POLICY = """记忆以300–500字为目标，整理近期实际操作、策略判断、关键价位和有效教训。
可以保留四小时收盘、CPI等策略观察条件。合并重复观点，不逐轮追加日志，不硬截断策略理由或关键价位。
历史操作保留事实，不当作实时仓位和挂单；省略操作身份、周期报错、恢复或解锁步骤、完整收益账本JSON。
来源及执行关联由程序保存，模型不负责恢复或证明交易。""" + '\n' + EVENT_TIME_POLICY
