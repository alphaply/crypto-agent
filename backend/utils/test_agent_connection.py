import os
import json
import time
from typing import List
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.utils.logger import setup_logger

load_dotenv()

logger = setup_logger("TestAgent")

API_KEY = "your_api_key_here"
BASE_URL = "https://api.deepseek.com"
MODEL_NAME = "deepseek-chat"


class OrderParams(BaseModel):
    reason: str = Field(description="简短的决策理由")
    action: str = Field(description="动作", pattern="^(BUY_LIMIT|SELL_LIMIT|CANCEL|CLOSE|NO_ACTION)$")
    pos_side: str = Field(description="平仓方向", default="")
    cancel_order_id: str = Field(description="撤单ID", default="")
    entry_price: float = Field(description="价格")
    amount: float = Field(description="数量", default=0.0)
    take_profit: float = Field(description="止盈", default=0.0)
    stop_loss: float = Field(description="止损", default=0.0)

class MarketSummaryParams(BaseModel):
    current_trend: str = Field(description="趋势判断")
    key_levels: str = Field(description="关键点位")
    strategy_thought: str = Field(description="思维链")
    predict: str = Field(description="预测")

class AgentOutput(BaseModel):
    summary: MarketSummaryParams
    orders: List[OrderParams]

# ==========================================
# 2. 你提供的 Input (Hardcoded for Test)
# ==========================================

REAL_PROMPT_INPUT = """
你是由 qwen3-max-2026-01-23 驱动的 **高胜率稳健合约交易员**。
当前时间: 2026-01-28 19:45:20 (周三)
当前监控: ETH/USDT | 模式: 实盘交易 | 杠杆: 10x
当前价格: 3027.09 | 15m ATR: 9.49

【角色任务】
捕捉日内 结构清晰 的波段机会。你的目标是稳定盈利，而非频繁刷单。
如果市场出现符合策略的高盈亏比机会，你却因为过度犹豫而选择观望，将被视为严重失职。
**实盘模式下，你不需要设置止盈止损 (TP/SL)，专注于优异的进场位置与出场位置。**
开单要有明确的信心支撑
做单方式：双向持仓 做多做空均可

【资金管理 (RISK MANAGEMENT)】
1. **严禁梭哈 (No All-In)**: 单笔交易的保证金占用不得超过可用余额的 50% (或者你想要的比例)。

【权限与指令】
1. **BUY_LIMIT**: 挂单开多 (价格必须 < 现价)。
2. **SELL_LIMIT**: 挂单开空 (价格必须 > 现价)。
3. **CLOSE**: 挂限价单平多或平空 (Limit Close)。**注意：必须在 `entry_price` 中填入平仓价格**，不要留空。CLOSE只支持限价单。
4. **CANCEL**: 撤销指定的挂单。
5. **NO_ACTION**: 没有极高把握时，保持空仓。

【决策铁律】
1. **点位精准**: 不要在半山腰挂单。
2. **防滑点**: 严禁使用市价开仓/平仓，必须使用 Limit 单。平仓时请计算好想要退出的 Limit 价格。
3. **趋势顺势**: 你尊重中长线指标，但是你是短线稳健性交易员。
4. 仅在信心 > 70% 时出手。
5. 要保持高胜率以及高回报率

【资金与持仓】
可用余额: 97.52 USDT

现有持仓: 
[LONG] ETH/USDT | Amt: 0.042 | Entry: 2988.0 | PnL: +1.642

活跃挂单 (Active Orders): 
[BUY] LIMIT | ID: '8389766085181740333' | Price: 2988.0 | Amt: 0.033

【全量市场数据】
**Snapshot** | Price: 3027 | 15m ATR: 9.49
Sentiment: Fund: 0.0100% | OI: 2.3M | Vol24h: 11.7B
| TF | Price | ATR | RSI | Vol Status | Recent Closes (Last 5) | EMA (20/50/100/200) | POC | VA Range | HVN |
|---|---|---|---|---|---|---|---|---|---|
| 5m | 3027 | 5.48 | 62.7 | Low | 3019, 3018, 3019, 3025, 3027 | 3019/3013/3008/2998 | 3017 | 2933-3030 | 3017,2989,2970 |
| 15m | 3027 | 9.49 | 62.0 | Low | 3024, 3016, 3019, 3025, 3027 | 3013/3005/2987/2963 | 2925 | 2835-2959 | 3014,2925,2844 |
| 1h | 3027 | 23.45 | 62.6 | Low | 3004, 2989, 3016, 3024, 3027 | 2997/2965/2956/2989 | 2935 | 2790-3224 | 3301,3208,3109 |
| 4h | 3026 | 47.88 | 62.6 | Low | 2979, 3024, 3003, 3004, 3026 | 2957/2989/3043/3073 | 2940 | 2900-3191 | 3318,3107,2940 |

【历史思路回溯 (Context)】
以下是最近 3 次的分析记录，请参考过去的时间线和思路演变：
----------------------------------------
 [2026-01-28 19:30:37] qwen3-max-2026-01-23: [REAL] Trend: 短期偏多，但上方面临3035强阻力，动能衰竭，量能低迷。当前价格3020处于5m EMA20（3018）与15m EMA20（3011）之上，结构仍偏强，但缺乏上攻动力。
Predict: 预计价格将围绕3017-3035区间震荡，若放量突破3035则打开上行空间；若失守3017则可能快速下探2988。 | Logic: 现有0.042 ETH多单浮盈+1.344 USDT，成本2988，可继续持有。原挂单2988（ID:8389766085181740333）为高胜率支撑位，应保留。当前价格3020未突破3035，也未跌破3017，无新增高确定性机会。若价格回踩3017企稳，可考虑加多，但当前无>70%信心的新开仓点位。
 [2026-01-28 19:15:36] qwen3-max-2026-01-23: [REAL] Trend: 短期偏多但受阻于3035，价格已从高点回落至3017，动能衰竭，量能低迷。当前处于EMA20（3017）支撑位，若跌破则可能测试2988强支撑。
Predict: 预计价格将围绕3017震荡，若失守则下探2988；若站稳3020则可能再测3035。 | Logic: 当前价格3017.23正好位于5m EMA20与5m POC重合位，属于短线多空分界。若此处企稳可轻仓试多，但更优策略是等待回踩2988确认支撑后再加仓。现有0.042 ETH多单浮盈+1.228 USDT，成本2988，可继续持有。原挂单2988（ID:8389766085181740333）为合理支撑位，应保留。当前无新增高确定性机会，不新开仓。
 [2026-01-28 19:00:34] qwen3-max-2026-01-23: [REAL] Trend: 短期偏多但受阻于3035，RSI高位钝化，量能低迷，回调概率上升
Predict: 预计价格将震荡回落测试2988支撑，若企稳可加多；若跌破2988则趋势转弱。 | Logic: 当前价格3023.91已从3035回落，未放量突破，动能衰竭。现有0.074 ETH多单浮盈+2.626 USDT，成本2988。若价格回踩2988企稳，可加多；若跌破2988则趋势转弱。当前无>70%信心的新开仓点位，但可在2988挂多单博反弹，因该位为前低+EMA密集区+POC共振支撑，胜率较高。
----------------------------------------

【输出要求】
1. **时效性检查**: 现在的价格 (3027.09) 是否已经跌破/突破了历史记录中的支撑/阻力位？
2.
   - BUY_LIMIT 入场价格必须 <= 3027.09
   - SELL_LIMIT 入场价格必须 >= 3027.09
   - CLOSE 价格务必合理（多单止盈价 > 现价，空单止盈价 < 现价，或者为了快速跑路选一个接近现价的位置）。

思路 解读 中文描述
请输出 JSON，包含 `orders` 列表。
- `action`: BUY_LIMIT / SELL_LIMIT / CLOSE / CANCEL / NO_ACTION
- `pos_side`: 如果是 CLOSE，必须填 'LONG' 或 'SHORT'；其他情况留空
- `entry_price`: 挂单价格 / 平仓价格 (CLOSE 必须填此项)
- `amount`: 下单数量 (注意单位是币的数量而不是USDT的数量)
- `reason`: 简短的执行理由
- `take_profit`: 填 0
- `stop_loss`: 填 0
- `cancel_order_id`: 填要撤销的订单 ID (如8389766084576502933)
"""

STRATEGY_PROMPT_TEMPLATE = """
你是由 qwen3-max-2026-01-23 驱动的 **资深加密货币策略分析师 (Crypto Strategist)**。
当前时间: 2026-01-28 18:00:20 (周三)
当前监控: BTC/USDT | 模式: 策略分析 (STRATEGY IDEA)
当前价格: 89565.3 | 15m ATR: 196.36

【角色任务】
你需要分析中长线趋势，生成具有高盈亏比 (R/R Ratio) 的交易计划。(4h级别日线级别)
你要做的是长线趋势单策略，而非频繁短线交易。
长线趋势单精准接针是一个非常重要的技能。
**策略模式下，必须明确给出 止损(SL) 和 止盈(TP) 点位。**

【策略要求】
1. **盈亏比**: 预期 R/R 必须 > 2.0。
2. **逻辑支撑**: 必须基于结构位 (Structure)、供需区 (Supply/Demand) 或流动性 (Liquidity) 制定计划。
3. **完整性**: 必须包含入场价、止损价、止盈价。
4. 你捕捉的是中长线趋势，稳健是你的目标，要稳稳赚钱。
5. **动态调整**: 请检查下方的【活跃策略挂单】，如果之前的挂单逻辑已失效（如价格已远离或趋势改变），**请务必输出 CANCEL 指令**来清理旧单。
6. 仅在信心 > 80% 时出手。
7. 要保持高胜率以及高回报率

【当前状态】
现有持仓: 
无持仓 (No Positions)

活跃策略挂单 (Strategy Orders): 
无活跃挂单 (No Active Orders)

【全量市场数据】
**Snapshot** | Price: 89565 | 15m ATR: 196.36
Sentiment: Fund: 0.0100% | OI: 102.2K | Vol24h: 10.4B
| TF | Price | ATR | RSI | Vol Status | Recent Closes (Last 5) | EMA (20/50/100/200) | POC | VA Range | HVN |
|---|---|---|---|---|---|---|---|---|---|
| 15m | 89565 | 196.36 | 65.9 | Low | 88912, 88968, 89294, 89542, 89565 | 89164/89034/88802/88575 | 87788 | 87270-88984 | 89216,88680,88288 |
| 1h | 89565 | 448.82 | 61.1 | Low | 89287, 89106, 88901, 89542, 89565 | 88980/88628/88709/89459 | 89297 | 86140-94478 | 96800,95133,92512 |
| 4h | 89553 | 925.34 | 58.2 | Low | 88378, 89197, 89079, 89106, 89553 | 88646/89448/90348/90750 | 87817 | 87039-92556 | 96729,95173,90081 |
| 1d | 89552 | 2335 | 46.7 | Low | 89180, 86628, 88300, 89197, 89552 | 90351/91292/94685/98408 | 84031 | 78079-108612 | 117669,108871,104214 |
| 1w | 89552 | 8219 | 41.9 | Low | 91497, 90964, 93614, 86628, 89552 | 96120/96741/86238/68608 | 20171 | 14654-61237 | 63076,42236,27526 |

【历史思路回溯 (Context)】
以下是最近 3 次的分析记录，请参考过去的时间线和思路演变：
----------------------------------------
 [2026-01-28 16:00:37] qwen3-max-2026-01-23: [STRATEGY] Trend: 中长期下降通道，但日线级别接近关键需求区
Predict: 若未来价格回撤至86500-87000并出现结构性看涨确认（如pin bar、吞没、双底等），则可重新部署高盈亏比多单；否则维持观望。 | Logic: 当前价格89108已远离此前挂单区域86750，且未出现回调至理想多头入场区。结合4h与日线EMA仍为空头排列，短期缺乏做多动能。原挂单逻辑基于深度回踩后结构确认，但当前价格未触发该条件，且市场无明显反转信号，继续持有挂单将降低资金效率并增加无效暴露风险。因此应取消旧挂单，等待更清晰的结构信号。
 [2026-01-28 15:00:40] qwen3-max-2026-01-23: [STRATEGY] Trend: 中长期下降通道，但日线级别接近关键需求区
Predict: BTC若回踩86500-87000区域获得支撑，有望展开中期反弹至92000-95000区间 | Logic: 当前价格尚未回踩理想多头入场区，若回调至86500-87000并出现结构确认，则具备高盈亏比做多机会
----------------------------------------

【输出要求】
思路 解读 中文描述
请输出 JSON。
- `action`: BUY_LIMIT / SELL_LIMIT / CANCEL / NO_ACTION
- `cancel_order_id`: 如果 action 是 CANCEL，请填写要撤销的单据 ID。
- `entry_price`: 建议入场价
- `take_profit`: 建议止盈价 (必填)
- `stop_loss`: 建议止损价 (必填)
- `reason`: 详细的策略逻辑，包含 R/R 计算。
"""

# ==========================================
# 3. 执行测试逻辑
# ==========================================

def test_llm_connection():
    logger.info(f"\n🚀 开始测试 LLM 配置 (.env)")
    
    api_key = API_KEY
    base_url = BASE_URL
    model_name = MODEL_NAME
    
    logger.info(f"   Model: {model_name}")
    logger.info(f"   Base URL: {base_url}")
    logger.info(f"   API Key: {api_key[:6]}******" if api_key else "   ❌ API Key 未找到")

    # 初始化 LLM
    try:
        llm = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0
        ).with_structured_output(AgentOutput,method="function_calling") # 尝试使用tool use方式
        
        logger.info("✅ LLM 客户端初始化成功")
        return llm
    except Exception as e:
        logger.error(f"❌ LLM 初始化失败: {e}")
        return None

def run_test(llm, test_name, prompt_content):
    logger.info(f"\n------------------------------------------------")
    logger.info(f"🧪 测试场景: {test_name}")
    logger.info(f"------------------------------------------------")
    logger.info("⏳ 正在发送请求给 LLM (这可能需要几秒钟)...")
    
    start_t = time.time()
    try:
        # 发送 SystemMessage
        response = llm.invoke([SystemMessage(content=prompt_content)])
        
        # 打印结果
        logger.info(f"✅ 响应成功 (耗时 {time.time()-start_t:.2f}s)")
        logger.info("\n👇 LLM 返回的 JSON 数据:")
        logger.info(response)
        logger.info(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))
        
        # 简单的逻辑检查
        if response.orders:
            logger.info(f"\n💡 生成了 {len(response.orders)} 个订单指令。")
        else:
            logger.info("\n💡 未生成订单 (NO_ACTION 或 观望)。")
            
    except Exception as e:
        logger.error(f"❌ 调用失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    llm_client = test_llm_connection()
    
    if llm_client:
        # 测试 1: 实盘模式
        run_test(llm_client, "实盘模式 (ETH/USDT)", REAL_PROMPT_INPUT)
        
        # 休息一下避免速率限制
        time.sleep(1)
        
        # 测试 2: 策略模式
        run_test(llm_client, "策略模式 (BTC/USDT)", STRATEGY_PROMPT_TEMPLATE)
