"""Pure execution evidence classification, shared by recovery and admission."""
from __future__ import annotations

import json

TERMINAL = {'closed', 'filled', 'canceled', 'cancelled', 'expired', 'rejected'}
UNRESOLVED = {'submission_unknown', 'terminal_fill_unverified'}


def rejection_evidence(error: Exception) -> dict | None:
    """An exception class alone cannot prove a write was rejected.

    Accept explicit venue business rejections, never transport/unknown codes.
    Store only the code, not the response (which can contain private data).
    """
    text = str(error)
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
    except (ValueError, TypeError):
        return None
    code = str(payload.get('code', ''))
    if 'duplicate' in str(payload.get('msg', '')).lower():
        return None  # The same client identity may already have been accepted.
    binance_codes = {'-1013', '-1100', '-1101', '-1102', '-1103', '-1111', '-1116',
                     '-1117', '-1121', '-1130', '-2010', '-2011', '-2014', '-2015',
                     '-2019', '-2021', '-2022', '-2027', '-2028', '-4061', '-4130',
                     '-4164', '-4028', '-4003', '-4004', '-4005'}
    if code in binance_codes:
        return {'source': 'exchange_rejection', 'code': code, 'no_execution': True}
    # OKX per-order codes carry the business result; a top-level server error
    # is not evidence that the order was never accepted.
    rows = payload.get('data')
    if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
        code = str(rows[0].get('sCode', ''))
        if code in {'51000', '51001', '51006', '51008', '51010', '51020', '51119', '51121'}:
            return {'source': 'exchange_rejection', 'code': code, 'no_execution': True}
    return None


def classify_order(record: dict) -> dict:
    status = record.get('status')
    proof = record.get('submission_evidence') or {}
    contrary = bool(record.get('id') or record.get('execution_order_id')
                    or float(record.get('filled') or 0) > 0 or record.get('fill_pending')
                    or record.get('replacement') or (record.get('amendment') or {}).get('state') in
                    {'pending', 'terminal_unconfirmed'})
    unaccepted = (record.get('submission_state') == 'not_submitted' and not contrary
                  and proof.get('no_execution') is True
                  and proof.get('source') in {'preflight', 'exchange_rejection'})
    if unaccepted:
        state = 'not_accepted'
    elif not record.get('id') or record.get('submission_state') == 'unknown':
        state = 'submission_unknown'
    elif (record.get('fill_pending') or record.get('replacement')
          or (record.get('cancel_requested_at') and status not in TERMINAL)
          or (record.get('amendment') or {}).get('state') in {'pending', 'terminal_unconfirmed'}):
        state = 'terminal_fill_unverified' if status in TERMINAL else 'submission_unknown'
    elif status in TERMINAL:
        state = 'terminal_verified' if record.get('fill_verified') else 'terminal_fill_unverified'
    elif record.get('query_error'):
        state = 'submission_unknown'
    elif status in {'open', 'waiting_trigger', 'partially_filled', 'partial'} and (
            record.get('fill_verified') or record.get('waiting_trigger')):
        state = 'live_verified'
    else:
        state = 'submission_unknown'
    return {'version': 1, 'state': state, 'source': proof.get('source', 'order_observation'),
            'operation_id': record.get('operation_id'), 'client_id': record.get('client_id')}


def classify_saved_order(record: dict) -> dict:
    """Lazily annotate evidence, preserving original receipts and order facts."""
    if not record.get('submission_evidence') and record.get('submission_state') == 'not_submitted':
        proof = rejection_evidence(ValueError(record.get('execution_error') or ''))
        if proof:
            record['submission_evidence'] = {**proof, 'migrated': True}
    classification = classify_order(record)
    record['evidence_classification'] = classification
    return classification
