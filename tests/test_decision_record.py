import json
import pytest

from backend.utils.decision_record import (
    is_transient_schema_error,
    compact_action_payload,
    compact_receipt_payload,
    compact_execution_receipt,
    memory_execution_evidence,
    append_execution_receipts,
    execution_identity,
)


def test_is_transient_schema_error():
    err1 = (
        "Error: 1 validation error for TradeActionsSchema\n"
        "  Value error, depends_on requires independent policy and an index list "
        "[type=value_error, input_value={'contract_version': 2, 'depends_on': [0]}], input_type=dict]\n"
        "    For further information visit https://errors.pydantic.dev/2.12/v/value_error"
    )
    assert is_transient_schema_error(err1) is True

    err2 = "ValidationError: 2 validation errors for OrderSchema"
    assert is_transient_schema_error(err2) is True

    valid_json = json.dumps({"contract_version": 2, "status": "completed", "results": []})
    assert is_transient_schema_error(valid_json) is False
    assert is_transient_schema_error(None) is False
    assert is_transient_schema_error("") is False


def test_compact_action_payload_strips_engine_metadata():
    raw_action = {
        "receipt_version": 1,
        "contract_version": 2,
        "status": "completed",
        "operation_id": "toolu_01YGUx7PSwq2AVPpiYn3SiSP:1",
        "action": "edit",
        "execution": {"phase": "verify", "submission_state": "submitted", "fill_status": "verified"},
        "position": {"side": "LONG", "amount": 0.02, "status": "verified", "observed_at": 1791889472.4048154},
        "cleanup": {"status": "not_required", "orders": [], "error": None},
        "continuation": {
            "blocked_actions": [], "rules": [], "assessment_state": "needs_refresh",
            "reason_code": None, "side": "LONG", "blocker_order_ids": [],
            "assessed_at": 1791889472.4106982, "advisory": True,
            "previous_assessment": {"rules": [], "blocker_order_ids": []},
            "evidence_versions": {"cycle": 14426, "scope": 3979}
        },
        "recovery": {
            "tool": "get_trade_operation", "target_operation_id": "toolu_01YGUx7PSwq2AVPpiYn3SiSP:1",
            "blocker_operation_ids": [], "last_error": None, "attempt_count": 616
        },
        "id": "1000002618522156",
        "client_id": "cai5063e71e76df06059cd6ff017b95",
        "episode_id": "1ac97a6735794997a210f15f384ef6e6",
        "amount": 0.02,
        "remaining": 0.02,
        "exit_type": "take_profit_market",
        "price": None,
        "trigger_price": 2565.0,
        "protection_state": "ACTIVE",
        "pending": False,
        "order_id": "algo:1000002618522156",
        "order_status": "waiting_trigger",
        "order_type": "take_profit_market",
        "filled_amount": 0.0,
        "remaining_amount": 0.02,
        "actual_remaining": 0.02,
        "requested_amount": 0.02,
        "side": "LONG",
        "edit_method": "cancel_replace",
        "previous_order_id": "algo:1000002618421056",
        "admission": {
            "version": 1, "decision": "allow", "blockers": [],
            "intent": {"action": "edit", "side": "LONG"}
        },
        "projected_at": 1791889492.692603,
        "evidence_source": "local_projection"
    }

    compact = compact_action_payload(raw_action)

    # Core fields are retained
    assert compact["action"] == "edit"
    assert compact["status"] == "completed"
    assert compact["order_id"] == "algo:1000002618522156"
    assert compact["order_status"] == "waiting_trigger"
    assert compact["order_type"] == "take_profit_market"
    assert compact["trigger_price"] == 2565.0
    assert compact["amount"] == 0.02
    assert compact["previous_order_id"] == "algo:1000002618421056"
    assert compact["position"] == {"side": "LONG", "amount": 0.02, "status": "verified"}

    # Bloat fields are stripped
    assert "continuation" not in compact
    assert "admission" not in compact
    assert "recovery" not in compact
    assert "client_id" not in compact
    assert "episode_id" not in compact
    assert "projected_at" not in compact
    assert "evidence_source" not in compact


def test_compact_batch_receipt_payload():
    batch = {
        "contract_version": 2,
        "status": "completed",
        "operation_id": "toolu_batch_1",
        "execution_policy": "independent",
        "results": [
            {
                "index": 0,
                "action": "close",
                "status": "completed",
                "result": {
                    "action": "close",
                    "status": "completed",
                    "order_id": "regular:123",
                    "order_status": "filled",
                    "amount": 0.02,
                    "filled_amount": 0.02,
                    "continuation": {"blocked_actions": []},
                    "recovery": {"attempt_count": 500}
                }
            }
        ],
        "summary": {"completed": 1, "failed": 0}
    }

    compact = compact_receipt_payload(batch)
    assert compact["status"] == "completed"
    assert compact["operation_id"] == "toolu_batch_1"
    assert len(compact["results"]) == 1
    sub = compact["results"][0]
    assert sub["action"] == "close"
    assert sub["result"]["order_id"] == "regular:123"
    assert "continuation" not in sub["result"]
    assert "recovery" not in sub["result"]


def test_compact_execution_receipt_transient_error():
    raw_error = (
        "Error: 1 validation error for TradeActionsSchema\n"
        "  Value error, depends_on requires independent policy"
    )
    item = {
        "tool": "execute_trade_actions",
        "tool_call_id": "toolu_err_1",
        "status": "failed",
        "result": raw_error,
        "current_evidence": {"resolution": "unresolved", "operation_id": "toolu_err_1"}
    }

    compact = compact_execution_receipt(item)
    assert compact["tool"] == "execute_trade_actions"
    assert compact["status"] == "failed"
    assert compact["error"] == "parameter_validation_failed"
    assert "current_evidence" not in compact
    assert "result" not in compact


def test_memory_execution_evidence_excludes_transient_error():
    schema_err = (
        "Error: 1 validation error for TradeActionsSchema\n"
        "  Value error, depends_on requires independent policy"
    )
    summary = {
        "config_id": "test_cfg",
        "symbol": "ETH/USDT",
        "decision_json": json.dumps({
            "version": 1,
            "messages": [],
            "execution_results": [
                {
                    "tool": "execute_trade_actions",
                    "tool_call_id": "toolu_err_1",
                    "status": "failed",
                    "result": schema_err,
                },
                {
                    "tool": "execute_trade_actions",
                    "tool_call_id": "toolu_ok_1",
                    "status": "completed",
                    "result": json.dumps({
                        "contract_version": 2,
                        "status": "completed",
                        "operation_id": "toolu_ok_1",
                        "results": [
                            {
                                "index": 0,
                                "action": "close",
                                "status": "completed",
                                "result": {
                                    "action": "close",
                                    "status": "completed",
                                    "order_id": "regular:999",
                                    "order_status": "filled",
                                    "amount": 0.05,
                                    "filled_amount": 0.05,
                                    "continuation": {"blocked_actions": []},
                                    "recovery": {"attempt_count": 999}
                                }
                            }
                        ]
                    })
                }
            ]
        })
    }

    evidence = memory_execution_evidence(summary)
    # The transient schema error item MUST be excluded
    assert len(evidence) == 1
    res_dict = json.loads(evidence[0]["result"]) if isinstance(evidence[0]["result"], str) else evidence[0]["result"]
    sub_res = res_dict["results"][0]["result"]
    assert sub_res["order_id"] == "regular:999"
    assert "continuation" not in sub_res
    assert "recovery" not in sub_res


def test_append_execution_receipts_skips_when_only_transient_error():
    schema_err = "Error: 1 validation error for TradeActionsSchema\nValue error"
    summary = {
        "config_id": "test_cfg",
        "symbol": "ETH/USDT",
        "decision_json": json.dumps({
            "version": 1,
            "messages": [],
            "execution_results": [
                {
                    "tool": "execute_trade_actions",
                    "tool_call_id": "toolu_err_1",
                    "status": "failed",
                    "result": schema_err,
                }
            ]
        })
    }

    base_text = "策略分析正文"
    result_text = append_execution_receipts(base_text, summary)
    # Because only transient schema error existed, no actual tool receipt block is appended
    assert result_text == base_text
