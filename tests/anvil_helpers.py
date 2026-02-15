"""Anvil RPC helpers: time warp, snapshot/revert, mine blocks."""
from web3 import Web3


def warp_time(w3: Web3, seconds: int):
    """Advance Anvil time by `seconds` and mine a block."""
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])


def snapshot(w3: Web3) -> str:
    """Take an EVM snapshot, return snapshot id."""
    resp = w3.provider.make_request("evm_snapshot", [])
    return resp["result"]


def revert(w3: Web3, snap_id: str):
    """Revert to a previous snapshot."""
    w3.provider.make_request("evm_revert", [snap_id])


def mine_blocks(w3: Web3, n: int = 1):
    """Mine `n` blocks."""
    for _ in range(n):
        w3.provider.make_request("evm_mine", [])


def set_balance(w3: Web3, address: str, balance_wei: int):
    """Set an account's ETH balance (Anvil cheatcode)."""
    w3.provider.make_request("anvil_setBalance", [address, hex(balance_wei)])
