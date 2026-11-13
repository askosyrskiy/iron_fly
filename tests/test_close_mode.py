"""close_mode and asymmetric pool on replay desk."""

from iron_fly.orders import wing_spans_from_lots
from iron_fly_strategy.spec import close_mode, leftover_stc, parse_spec, pool_reuse


def _quote(strike, call, put):
    return StrikeQuote(strike, call[0], call[1], put[0], put[1])


def test_straddle_only_defaults():
    bare = parse_spec("f1_at: '15:45'\nwings: 20\nregime: '±2.5/5'\nstop: 0\ndesk: auto\n")
    assert close_mode(bare.auto) == "fly"
    reuse = parse_spec(
        "f1_at: '15:45'\nwings: 20\nregime: '±2.5/5'\nstop: 0\n"
        "close_mode: straddle_only\n"
    )
    assert pool_reuse(reuse.auto) == "asymmetric"
    assert leftover_stc(reuse.auto) is False


def test_asymmetric_spans_from_lots():
    strike = 7400
    lots = [
        {"right": "put", "strike": 7385.0, "quantity": 1},
        {"right": "call", "strike": 7485.0, "quantity": 1},
    ]
    put_w, call_w = wing_spans_from_lots(strike, lots)
    assert put_w == 15
    assert call_w == 85

