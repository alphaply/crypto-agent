from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import chat as routes
from backend.app.core.deps import get_current_user


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-admin"}
    return TestClient(app)


def test_chat_catalog_supports_paging_and_selected_market_validation(client, monkeypatch):
    catalog = Mock(return_value={"symbols": [], "selected_symbols": [], "total": 0})
    monkeypatch.setattr(routes, "list_market_symbols_payload", catalog)
    result = client.get('/api/chat/market-symbols', params={
        'exchange_profile_id': 'account', 'market_type': 'swap', 'keyword': 'BTC',
        'quote': 'USDT', 'limit': 50, 'offset': 100, 'linear_only': True,
        'symbols': ' BTC/USDT:USDT , ETH/USDT:USDT ',
    })
    assert result.status_code == 200
    catalog.assert_called_once_with('account', 'swap', 'BTC', quote='USDT',
                                    limit=50, offset=100,
                                    symbols=['BTC/USDT:USDT', 'ETH/USDT:USDT'], linear_only=True)
    # Chat can analyze another supported market using the same account credentials.
    assert 'require_profile_market' not in catalog.call_args.kwargs


@pytest.mark.parametrize('params', [{'limit': 201}, {'limit': 0}, {'offset': -1}, {'market_type': 'future'}])
def test_chat_catalog_rejects_invalid_filters(client, params):
    assert client.get('/api/chat/market-symbols', params={'exchange_profile_id': 'account', **params}).status_code == 422
