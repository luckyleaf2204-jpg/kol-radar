"""A token swapped against a stablecoin (e.g. a GMGN route paying out USDC) is not a plain transfer: the event keeps
its kind (no SOL price) but carries quote / quote_amt. Found on SCINU (Early Smart Money research, 2026-11-25).
Synthetic transactions only, no network."""
from insiders.track import parse_mint_events
from insiders.watch import analyse

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
OTHER = "5uGbn3xfFEgmy3qhLmXPUGeBQee8hwTuAnHCktTfFRXC"
MINT = "CAjtTHvC878f8cZ4zEwdvgjkjFM7rbYN8Mb1go1cpump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def b(i, owner, mint, amount):
    return {"accountIndex": i, "owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(amount), "decimals": 6}}


def mk(pre, post, native=(-15_000, 0)):
    return {"blockTime": 1, "slot": 7, "meta": {"err": None, "fee": 15_000, "preBalances": [10**9, 10**9], "postBalances": [10**9 + native[0], 10**9 + native[1]],
                                                "preTokenBalances": pre, "postTokenBalances": post, "innerInstructions": [], "logMessages": []},
            "transaction": {"signatures": ["sig1"], "message": {"accountKeys": [{"pubkey": WALLET, "signer": True}, {"pubkey": OTHER}], "instructions": []}}}


def test_sell_for_usdc_is_tagged_not_a_plain_send():
    t = mk([b(1, WALLET, MINT, 7_000_000), b(2, WALLET, USDC, 322_326_360)],
           [b(1, WALLET, MINT, 0), b(2, WALLET, USDC, 342_887_320)])
    (e,) = analyse(t, WALLET, with_quote=True)
    assert e["kind"] == "SEND" and e["sol"] == 0                 # no SOL price: kind unchanged for every consumer
    assert e["quote"] == "USDC" and e["quote_amt"] == 20.56096


def test_buy_with_usdc_is_tagged():
    t = mk([b(2, WALLET, USDC, 20_000_000)], [b(1, WALLET, MINT, 5_000_000), b(2, WALLET, USDC, 10_000_000)])
    (e,) = analyse(t, WALLET, with_quote=True)
    assert e["kind"] == "RECEIVE" and e["quote"] == "USDC" and e["quote_amt"] == 10.0


def test_plain_transfer_has_no_quote():
    t = mk([b(1, WALLET, MINT, 7_000_000)], [b(1, WALLET, MINT, 0), b(3, OTHER, MINT, 7_000_000)])
    e = [x for x in analyse(t, WALLET, with_quote=True) if x["wallet"] == WALLET][0]
    assert e["kind"] == "SEND" and "quote" not in e


def test_usdc_moving_the_same_way_is_not_a_swap():
    t = mk([b(1, WALLET, MINT, 7_000_000), b(2, WALLET, USDC, 20_000_000)], [b(1, WALLET, MINT, 0), b(2, WALLET, USDC, 0)])
    (e,) = analyse(t, WALLET, with_quote=True)
    assert e["kind"] == "SEND" and "quote" not in e              # token AND usdc out: not a sale


def test_parse_mint_events_keeps_the_quote():
    t = mk([b(1, WALLET, MINT, 7_000_000), b(2, WALLET, USDC, 0)], [b(1, WALLET, MINT, 0), b(2, WALLET, USDC, 5_000_000)])
    (e,) = parse_mint_events(t, MINT)
    assert e["quote"] == "USDC" and e["quote_amt"] == 5.0 and e["kind"] == "SEND"


def test_default_analyse_output_is_unchanged_for_old_consumers():
    """watch / vip / wallets call analyse() without the flag: same keys as before the fix (alert payloads unchanged)."""
    t = mk([b(1, WALLET, MINT, 7_000_000), b(2, WALLET, USDC, 0)], [b(1, WALLET, MINT, 0), b(2, WALLET, USDC, 5_000_000)])
    (e,) = analyse(t, WALLET)
    assert set(e) == {"ts", "sig", "wallet", "kind", "mint", "sol", "tokens"} and e["kind"] == "SEND" and e["sol"] == 0
