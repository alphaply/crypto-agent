"""Task-specific model interfaces; execution still validates the live config."""
from copy import deepcopy

from backend.utils.exit_policy import effective_exit_mode


INDEPENDENT_EXIT_HELP = (
    '独立退出单按标的币数量管理，仅针对本任务当前周期的已成交仓位；加仓不扩大已有退出数量。'
    '附带TP/SL为空不代表没有独立退出单。本地周期记录不是实时成交证明，'
    '快照未见订单不排除交易所其他来源挂单；待核验时不能视为已生效保护或重复提交。'
    'WAITING为等待入场成交，ACTIVE为最近核验状态，EXITING为退出清理未完成。'
    '不能自动认领手动仓或其他任务仓位。'
)


def specialize_trade_tool(tool, config: dict):
    """Copy descriptions and JSON schemas without mutating shared Pydantic models."""
    mode = effective_exit_mode(config)
    independent = mode == 'independent_exits'
    name = tool.name
    if independent and name.startswith('update_position_protection_'):
        return None
    if not independent and name in {'update_exit_order', 'adopt_position_real'}:
        return None
    schema = deepcopy(tool.args_schema.model_json_schema())
    definitions = schema.get('$defs', {})
    for key in ('OpenOrderReal', 'OpenOrderStrategy'):
        order = definitions.get(key)
        if not order:
            continue
        order['description'] = '限价开仓，amount使用标的币数量。'
        for field in ('stop_loss', 'take_profit'):
            if independent:
                order['properties'].pop(field, None)
            elif mode == 'attached_required':
                order['properties'][field] = {
                    'type': 'number', 'exclusiveMinimum': 0,
                    'description': '必填止损触发价' if field == 'stop_loss' else '必填止盈触发价',
                }
                order.setdefault('required', []).append(field)
        if independent:
            order['additionalProperties'] = False
    if independent:
        # Entry amendments cannot attach protection in this mode, including batches.
        for item in (schema, definitions.get('AmendAction', {})):
            for field in ('stop_loss', 'take_profit'):
                item.get('properties', {}).pop(field, None)
        definitions.pop('ProtectionAction', None)
    elif config.get('mode') == 'STRATEGY' and 'CloseOrder' in definitions:
        properties = definitions['CloseOrder']['properties']
        properties['exit_type'] = {'enum': ['market', None], 'default': None, 'description': '仅支持市价减仓'}
        properties['entry_price'] = {'type': 'number', 'const': 0, 'default': 0}
        properties.pop('price', None)
        properties.pop('trigger_price', None)
    if name == 'execute_trade_actions':
        items = schema['properties']['actions']['items']
        if independent:
            items['oneOf'] = [ref for ref in items['oneOf'] if not ref['$ref'].endswith('/ProtectionAction')]
            items['discriminator']['mapping'].pop('update_protection', None)
        else:
            definitions['AmendAction']['properties']['action'] = {'type': 'string', 'const': 'amend_entry'}
            items['discriminator']['mapping'].pop('amend_exit', None)
    description = tool.description
    if name.startswith('open_position_'):
        suffix = 'real' if config.get('mode', '').upper() == 'REAL' else 'strategy'
        common = (
            '限价开仓/加仓：BUY_LIMIT开多LONG，SELL_LIMIT开空SHORT。'
            '必填amount（标的币数量）、entry_price和reason；提交委托不等于成交。'
            '失败或结果未知时停止并核对，不重复开仓；用户要求保持的挂单不得改动。'
        )
        if independent:
            description = common + f'不接受附带TP/SL，也不自动安装保护。确认成交后通过close_position_{suffix}按价格和数量分别创建退出单。'
        else:
            description = common + ('每次开仓必须同时提供stop_loss和take_profit。' if mode == 'attached_required'
                                    else 'stop_loss和take_profit可分别提供或省略。')
            description += '多单SL<入场<TP，空单TP<入场<SL；省略的保护不会新建。'
            if suffix == 'real':
                description += ('同方向加仓省略则继承，填写则先更新同方向整仓保护，另一项保留。'
                                '加仓失败不回滚已更新保护；后台在成交后安装条件市价保护，存在延迟，并非原子绑定。')
    if independent and name.startswith('update_entry_order_'):
        description = ('修改本任务未完全成交入场单，指定order_id、reason及entry_price或amount。'
                       'amount为含已成交部分的总标的币数量；省略字段保留，方向不变。'
                       '不接受附带TP/SL，退出订单使用update_exit_order修改。pending不可重复提交。')
    if independent:
        if name.startswith('close_position_') or name == 'execute_trade_actions':
            description += '\n' + INDEPENDENT_EXIT_HELP
    else:
        description = '\n'.join(line for line in description.splitlines() if '独立退出模式' not in line)
        if name == 'close_position_strategy':
            description += '\n仅支持按当前市价减仓，exit_type省略或填market；不接受条件退出价格。'
    if name == 'execute_trade_actions':
        description += ('\nopen禁止附带TP/SL；不支持update_protection，退出使用close或amend_exit。' if independent else
                        '\n不支持amend_exit。open必须同时提供stop_loss和take_profit。' if mode == 'attached_required' else
                        '\n不支持amend_exit。open的stop_loss和take_profit均可省略。')
    return tool.model_copy(update={'description': description, 'args_schema': schema})
