from unittest.mock import MagicMock
from backend.utils.spot_execution import spot_budget_status


def test_spot_budget_status_does_not_block_on_unknown_reservations(monkeypatch):
    config = {'dca_amount': 30, 'dca_budget': 100}
    mock_conn = MagicMock()

    def fake_execute(query, params=None):
        mock_cursor = MagicMock()
        if "spot_budget_cycles" in query:
            mock_cursor.fetchone.return_value = None
        elif "spot_budget_reservations" in query:
            mock_cursor.fetchall.return_value = [
                {'config_id': 'cfg', 'symbol': 'BTC/USDT', 'order_id': 'lost', 'status': 'unknown',
                 'quote_cost': 30, 'cycle_id': 'old-cycle', 'reservation_kind': 'buy',
                 'allocation_ceiling': None}
            ]
            mock_cursor.fetchone.return_value = (1,)
        elif "orders o LEFT JOIN spot_order_fills" in query:
            mock_cursor.fetchall.return_value = []
        else:
            mock_cursor.fetchone.return_value = (1,)
            mock_cursor.fetchall.return_value = []
        return mock_cursor

    mock_conn.execute.side_effect = fake_execute
    mock_conn.__enter__.return_value = mock_conn
    monkeypatch.setattr('backend.database.get_db_conn', MagicMock(return_value=mock_conn))

    status = spot_budget_status('cfg', config, 'new-cycle')
    assert status['pending_unknown'] is False
    assert status['period_remaining'] == 30
    assert status['available'] == 30


def test_system_prompt_does_not_forbid_buys():
    import inspect
    from backend.agent import agent_graph
    source = inspect.getsource(agent_graph.start_node)
    assert '存在尚未核实结果的下单，当前禁止新增买入' not in source
