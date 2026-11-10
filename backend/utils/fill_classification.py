"""Classify execution purpose from explicit position direction, never PnL."""
def classify_fill(fill: dict, metadata: dict, role: str | None = None) -> tuple[str | None, str | None]:
    side = str(fill.get('position_side') or '').upper()
    if side not in {'LONG', 'SHORT'}:
        side = str(metadata.get('side') or '').upper()
    if side not in {'LONG', 'SHORT'}:
        return role, None
    if not role:
        action = str(fill.get('side') or '').lower()
        if action in {'buy', 'sell'}:
            role = 'entry' if (action == 'buy') == (side == 'LONG') else 'external_unknown_exit'
    return role, side
