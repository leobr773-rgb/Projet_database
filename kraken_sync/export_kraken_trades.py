#!/usr/bin/env python3
"""
Exporte l'historique de trading Kraken (Spot/Pro ou Futures) vers un fichier
JSON compatible avec le dashboard `dashboard_trading.html`.

Utilisation :
    python3 export_kraken_trades.py            # export normal -> data/kraken_trades.json
    python3 export_kraken_trades.py --debug    # dump brut des réponses API -> data/debug_*.json
                                                 # (utile pour ajuster le mapping des champs
                                                  selon ce que Kraken renvoie réellement)

Configuration : voir .env.example -> copier en .env et remplir vos clés API
(lecture seule uniquement).
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import ccxt
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

EXCHANGE_ID = os.getenv("KRAKEN_EXCHANGE", "krakenfutures")
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
STARTING_BALANCE = float(os.getenv("KRAKEN_STARTING_BALANCE", "0"))
QUOTE_CCY = os.getenv("KRAKEN_QUOTE_CURRENCY", "USD")

DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEBUG = "--debug" in sys.argv


def get_exchange():
    if not API_KEY or not API_SECRET:
        sys.exit(
            "ERREUR : KRAKEN_API_KEY / KRAKEN_API_SECRET manquants.\n"
            "Copiez kraken_sync/.env.example vers kraken_sync/.env et remplissez vos clés."
        )
    if not hasattr(ccxt, EXCHANGE_ID):
        sys.exit(f"ERREUR : exchange ccxt inconnu: {EXCHANGE_ID}")

    klass = getattr(ccxt, EXCHANGE_ID)
    exchange = klass({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
    })
    return exchange


def fetch_all_my_trades(exchange, symbol=None):
    """Récupère tout l'historique de trades, paginé par timestamp."""
    all_trades = []
    since = None
    while True:
        batch = exchange.fetch_my_trades(symbol=symbol, since=since, limit=200)
        if not batch:
            break
        all_trades.extend(batch)
        last_ts = batch[-1]["timestamp"]
        if since == last_ts:
            break
        since = last_ts + 1
        if len(batch) < 2:
            break
        time.sleep(exchange.rateLimit / 1000)
    # dédoublonnage par id
    seen, unique = set(), []
    for t in all_trades:
        tid = t.get("id") or (t["timestamp"], t["symbol"], t["amount"], t["price"])
        if tid in seen:
            continue
        seen.add(tid)
        unique.append(t)
    unique.sort(key=lambda t: t["timestamp"])
    return unique


def fetch_all_ledger(exchange):
    """Récupère le ledger (dépôts/retraits/trades) si l'exchange le supporte."""
    if not exchange.has.get("fetchLedger"):
        return []
    all_entries = []
    since = None
    while True:
        try:
            batch = exchange.fetch_ledger(since=since, limit=200)
        except Exception as e:
            print(f"  (fetchLedger non disponible: {e})")
            return []
        if not batch:
            break
        all_entries.extend(batch)
        last_ts = batch[-1]["timestamp"]
        if since == last_ts:
            break
        since = last_ts + 1
        if len(batch) < 2:
            break
        time.sleep(exchange.rateLimit / 1000)
    all_entries.sort(key=lambda e: e["timestamp"])
    return all_entries


def realized_pnl_from_trade(trade):
    """
    Essaie d'extraire le PnL réalisé d'un fill depuis les champs bruts Kraken.
    Le nom du champ varie selon kraken / krakenfutures et selon la version de ccxt.
    Retourne None si introuvable (le fill ne ferme pas de position, ex: ouverture).
    """
    info = trade.get("info", {}) or {}
    for key in ("pnl", "realizedPnl", "realized_pnl", "closedPnl", "closed_pnl"):
        if key in info:
            try:
                return float(info[key])
            except (TypeError, ValueError):
                pass
    return None


def side_fr(side):
    return "Achat" if side == "buy" else "Vente"


def build_trades_json(trades):
    """
    Construit la structure attendue par dashboard_trading.html :
    { label, soldeInitial, trades: [ {jour, date, actif, sens, lots, rrPrevu,
      rrReel, resultat, pnl, risqueMax, pct, note, solde}, ... ] }

    Notes :
    - rrPrevu / rrReel / risqueMax ne sont PAS fournis par l'API Kraken
      (ce sont des informations de plan de trade, propres à votre journal
      manuel) -> laissés à null.
    - "solde" est reconstitué = solde initial + somme cumulée des PnL réalisés
      des fills. Si certains fills n'ont pas de PnL exploitable (ex: ouverture
      de position), ils sont quand même listés avec pnl=0.
    """
    out_trades = []
    running = STARTING_BALANCE
    for i, t in enumerate(trades, start=1):
        pnl = realized_pnl_from_trade(t)
        if pnl is None:
            pnl = 0.0
        running += pnl
        date = datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        pct = (pnl / (running - pnl) * 100) if (running - pnl) != 0 else 0.0
        out_trades.append({
            "jour": f"JOUR {i}",
            "date": date,
            "actif": t.get("symbol", ""),
            "sens": side_fr(t.get("side", "")),
            "lots": t.get("amount"),
            "rrPrevu": None,
            "rrReel": None,
            "resultat": "Gain" if pnl > 0 else ("Perte" if pnl < 0 else "BE"),
            "pnl": round(pnl, 4),
            "risqueMax": None,
            "pct": round(pct, 4),
            "note": f"Importé automatiquement depuis Kraken (ordre {t.get('order') or t.get('id')}, frais: {t.get('fee', {}).get('cost', 0)} {t.get('fee', {}).get('currency', '')})",
            "solde": round(running, 4),
        })

    return {
        "label": f"Compte 3 — Kraken {('Futures' if EXCHANGE_ID == 'krakenfutures' else 'Pro/Spot')} (connecté API)",
        "soldeInitial": STARTING_BALANCE,
        "trades": out_trades,
    }


def main():
    exchange = get_exchange()
    print(f"Connexion à {EXCHANGE_ID}...")

    print("Récupération des trades (fetch_my_trades)...")
    trades = fetch_all_my_trades(exchange)
    print(f"  -> {len(trades)} fill(s) trouvé(s)")

    print("Récupération du ledger (fetch_ledger)...")
    ledger = fetch_all_ledger(exchange)
    print(f"  -> {len(ledger)} entrée(s) de ledger trouvée(s)")

    if DEBUG:
        with open(os.path.join(DATA_DIR, "debug_trades.json"), "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(DATA_DIR, "debug_ledger.json"), "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, ensure_ascii=False, default=str)
        print(f"Dumps bruts écrits dans {DATA_DIR}/debug_trades.json et debug_ledger.json")
        print("Inspectez ces fichiers (notamment le champ 'info') pour vérifier le mapping")
        print("du PnL réalisé dans realized_pnl_from_trade(), puis relancez sans --debug.")
        return

    if not trades:
        print("Aucun trade trouvé. Vérifiez vos clés API et leurs permissions.")
        return

    result = build_trades_json(trades)
    out_path = os.path.join(DATA_DIR, "kraken_trades.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"OK -> {out_path} ({len(result['trades'])} trades)")
    print("Ouvrez dashboard_trading.html (via un serveur local) pour voir le compte 'Kraken (connecté API)'.")


if __name__ == "__main__":
    main()
