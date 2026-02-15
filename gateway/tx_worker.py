"""Background transaction worker — processes pending txs from the queue."""
import asyncio
import json
import structlog
from database import get_pending_txs, update_tx_status, award_xp
from contract import client

logger = structlog.get_logger()


async def process_pending_txs():
    """Process all pending transactions."""
    pending = await get_pending_txs(limit=5)
    for tx in pending:
        await process_single_tx(tx)


ENTRY_BOND = 10_000_000_000_000_000  # 0.01 MON in wei


async def process_single_tx(tx: dict):
    """Submit a single transaction to the chain."""
    tx_id = tx["id"]
    method = tx["method"]
    params = json.loads(tx["params"])

    try:
        await update_tx_status(tx_id, "submitting")
        # enterDungeon requires ENTRY_BOND as msg.value
        value = ENTRY_BOND if method == "enterDungeon" else 0
        tx_hash = await client.send_tx(method, *params, value=value)
        await update_tx_status(tx_id, "submitted", tx_hash=tx_hash)
        logger.info("tx_submitted", tx_id=tx_id, tx_hash=tx_hash)

        # Poll for receipt (up to 30s)
        for _ in range(15):
            await asyncio.sleep(2)
            receipt = client.get_receipt(tx_hash)
            if receipt:
                status = "mined" if receipt.get("status") == 1 else "failed"
                await update_tx_status(tx_id, status, tx_hash=tx_hash)
                if status == "mined":
                    await award_xp(
                        idempotency_key=f"tx_mined:{tx_id}",
                        moltbook_id=tx["moltbook_id"],
                        session_id=0,
                        epoch_id=0,
                        event_type=f"tx_{method}",
                        xp_amount=10,
                        source="tx_worker",
                    )
                logger.info("tx_mined", tx_id=tx_id, status=status)
                return
        # Still pending after 30s
        logger.warning("tx_receipt_timeout", tx_id=tx_id, tx_hash=tx_hash)

    except Exception as e:
        logger.error("tx_failed", tx_id=tx_id, error=str(e))
        await update_tx_status(tx_id, "failed", error=str(e))


async def tx_worker_loop():
    """Background loop that processes transactions every 5 seconds."""
    while True:
        try:
            await process_pending_txs()
        except Exception as e:
            logger.error("tx_worker_error", error=str(e))
        await asyncio.sleep(5)
