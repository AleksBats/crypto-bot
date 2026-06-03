"""
monitors/whale.py — detects large ASTER wallet movements.

⚠️  TODO: This monitor requires knowing ASTER's blockchain and contract address.

Supported sources (choose one based on ASTER's chain):

  A) Ethereum / EVM-compatible chain
     Etherscan Token Transfers:
     GET https://api.etherscan.io/api
         ?module=account&action=tokentx
         &contractaddress={ASTER_CONTRACT}
         &sort=desc&apikey={ETHERSCAN_API_KEY}
     Docs: https://docs.etherscan.io/api-endpoints/accounts#get-a-list-of-erc20-token-transfer-events-by-address

  B) BNB Smart Chain (BSC)
     BscScan Token Transfers:
     GET https://api.bscscan.com/api
         ?module=account&action=tokentx
         &contractaddress={ASTER_CONTRACT}
         &sort=desc&apikey={BSCSCAN_API_KEY}
     Docs: https://docs.bscscan.com/api-endpoints/accounts

  C) Astar Network (Polkadot EVM)
     Subscan Transfers:
     POST https://astar.api.subscan.io/api/scan/transfers
     Headers: X-API-Key: {SUBSCAN_API_KEY}
     Body: {"row": 10, "page": 0, "asset_symbol": "ASTR"}
     Docs: https://support.subscan.io/

  D) Whale Alert API (chain-agnostic, paid)
     GET https://api.whale-alert.io/v1/transactions
         ?api_key={WHALE_ALERT_KEY}&min_value=500000&currency=aster
     Docs: https://docs.whale-alert.io/

Steps to activate this monitor:
  1. Confirm ASTER's chain and contract address.
  2. Add correct API key to .env.
  3. Uncomment the appropriate fetcher below.
  4. Set ASTER_CONTRACT_ADDRESS in .env.

Currently: monitor logs a startup warning and does nothing.
"""

import asyncio
import logging
import time

import httpx

import config
from alert_engine import Signal, engine
from state import state
import telegram_bot as tg

logger = logging.getLogger(__name__)

# ── Known exchange hot-wallet labels (add more as discovered) ────────────────
EXCHANGE_LABELS: dict[str, str] = {
    # "0xaddress": "Binance Hot Wallet",
    # "0xaddress": "Bybit Deposit",
    # TODO: populate with verified exchange addresses for ASTER's chain
}


def _label_address(addr: str) -> str:
    return EXCHANGE_LABELS.get(addr.lower(), addr)


def _is_exchange(addr: str) -> bool:
    return addr.lower() in EXCHANGE_LABELS


# ── Fetcher skeleton (Etherscan-compatible) ──────────────────────────────────
async def _fetch_transfers_etherscan(client: httpx.AsyncClient) -> list[dict]:
    """
    TODO: Uncomment and configure once ASTER contract address is confirmed.
    """
    # resp = await client.get(
    #     "https://api.etherscan.io/api",
    #     params={
    #         "module": "account",
    #         "action": "tokentx",
    #         "contractaddress": config.ASTER_CONTRACT,
    #         "sort": "desc",
    #         "apikey": config.ETHERSCAN_API_KEY,
    #     }
    # )
    # resp.raise_for_status()
    # data = resp.json()
    # if data["status"] != "1":
    #     return []
    # return data["result"]
    return []   # TODO: remove when implemented


async def run_whale_monitor():
    if not config.ASTER_CONTRACT:
        logger.warning(
            "⚠️  WHALE MONITOR DISABLED: ASTER_CONTRACT_ADDRESS not set in .env.\n"
            "    See monitors/whale.py for setup instructions."
        )
        return

    if not (config.ETHERSCAN_API_KEY or config.BSCSCAN_API_KEY or config.SUBSCAN_API_KEY):
        logger.warning(
            "⚠️  WHALE MONITOR DISABLED: No on-chain API key configured.\n"
            "    Set ETHERSCAN_API_KEY, BSCSCAN_API_KEY, or SUBSCAN_API_KEY in .env."
        )
        return

    logger.info("Whale monitor started (TODO: activate fetcher).")
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                transfers = await _fetch_transfers_etherscan(client)

                for tx in transfers:
                    tx_hash = tx.get("hash", "")
                    if tx_hash in state.seen_whale_txs:
                        continue

                    value_raw   = int(tx.get("value", 0))
                    decimals    = int(tx.get("tokenDecimal", 18))
                    amount      = value_raw / (10 ** decimals)
                    price       = state.last_price or 0.0
                    usd_value   = amount * price

                    threshold_met = (
                        amount >= config.WHALE_THRESHOLD_ASTER
                        or (price > 0 and usd_value >= config.WHALE_THRESHOLD_ASTER * price)
                    )

                    if threshold_met:
                        from_addr = tx.get("from", "")
                        to_addr   = tx.get("to", "")

                        if _is_exchange(to_addr):
                            direction = "DEPOSIT TO EXCHANGE"
                        elif _is_exchange(from_addr):
                            direction = "WITHDRAWAL FROM EXCHANGE"
                        else:
                            direction = "WHALE TRANSFER"

                        state.seen_whale_txs.add(tx_hash)
                        await engine.submit(Signal(
                            key="whale_transfer",
                            strong=True,
                            message=tg.fmt_whale(
                                direction, amount, price,
                                tx_hash, from_addr, to_addr,
                            ),
                            priority=1,
                        ))

            except Exception as e:
                logger.error("Whale monitor error: %s", e)

            await asyncio.sleep(config.POLL_WHALE_SECS)
