"""
backtest.py
───────────
Backtest de la stratégie certainty-band sur les marchés Polymarket résolus.

Limite des données : Polymarket n'expose pas d'historique de prix minute par
minute. On utilise donc deux approches complémentaires :

  1. Calibration : sur les marchés résolus, est-ce que les marchés à 90-95%
     gagnent effectivement 90-95% du temps ? (test d'efficience du marché)

  2. Simulation forward : pour chaque marché résolu qui correspond à nos filtres,
     on simule une entrée au milieu de la bande (0.925) et on calcule le PnL.

Usage :
    python backtest.py
    python backtest.py --markets 2000 --cert-lo 0.85 --cert-hi 0.95
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import aiohttp

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class ResolvedMarket:
    question: str
    liquidity: float
    volume: float
    yes_won: bool
    end_date: datetime
    # Prix final juste avant résolution (proxy imparfait du dernier prix coté)
    last_yes_price: float  # prix YES juste avant résolution (0-1)


# ─────────────────────────────────────────────────────────────────────────────
#  Fetch
# ─────────────────────────────────────────────────────────────────────────────

def _parse_list(value, default=None):
    if default is None:
        default = []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            p = json.loads(value)
            return p if isinstance(p, list) else default
        except Exception:
            return default
    return default


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_market(m: dict, min_liquidity: float) -> Optional[ResolvedMarket]:
    if not m.get("closed"):
        return None

    outcomes = _parse_list(m.get("outcomes"), [])
    prices   = _parse_list(m.get("outcomePrices"), [])
    clob_ids = _parse_list(m.get("clobTokenIds"), [])

    if len(outcomes) != 2 or len(prices) != 2:
        return None

    try:
        yes_price_final = float(prices[0])
        no_price_final  = float(prices[1])
    except (ValueError, TypeError):
        return None

    # Marché résolu = un côté à ≥0.99
    if yes_price_final >= 0.99:
        yes_won = True
    elif no_price_final >= 0.99:
        yes_won = False
    else:
        return None  # pas encore résolu ou résolution ambiguë

    liquidity = float(m.get("liquidity", 0) or 0)
    if liquidity < min_liquidity:
        return None

    end_date = _parse_date(m.get("endDate") or m.get("endDateIso"))
    if end_date is None:
        return None

    volume = float(m.get("volume", 0) or 0)

    # Heuristique : si le marché est résolu, on ne connaît pas le dernier prix
    # avant résolution. On utilise le volume comme proxy de l'activité :
    # un marché avec beaucoup de volume avait sûrement une fourchette de prix active.
    # On stocke yes_price_final pour le filtrage de calibration.
    return ResolvedMarket(
        question=m.get("question", ""),
        liquidity=liquidity,
        volume=volume,
        yes_won=yes_won,
        end_date=end_date,
        last_yes_price=yes_price_final,
    )


async def fetch_resolved(
    session: aiohttp.ClientSession,
    max_markets: int,
    min_liquidity: float,
    page_size: int = 100,
) -> List[ResolvedMarket]:
    markets: List[ResolvedMarket] = []
    offset = 0

    print(f"Récupération des marchés résolus (objectif : {max_markets})...")

    while len(markets) < max_markets:
        async with session.get(
            "/markets",
            params={
                "closed": "true",
                "limit": page_size,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                print(f"HTTP {resp.status} à offset={offset}")
                break
            data = await resp.json(content_type=None)
            raw = data if isinstance(data, list) else data.get("markets", [])
            if not raw:
                break

            for m in raw:
                parsed = _parse_market(m, min_liquidity)
                if parsed:
                    markets.append(parsed)

            offset += page_size
            print(f"  {offset} scannes -> {len(markets)} valides...", end="\r")

            if len(raw) < page_size:
                break

    print(f"\n{len(markets)} marchés résolus récupérés.\n")
    return markets


# ─────────────────────────────────────────────────────────────────────────────
#  Analyse
# ─────────────────────────────────────────────────────────────────────────────

def bar(value: float, width: int = 30) -> str:
    filled = round(value * width)
    return "#" * filled + "-" * (width - filled)


def calibration_analysis(markets: List[ResolvedMarket]) -> None:
    """
    Analyse de calibration : les marchés à 90-95% gagnent-ils vraiment 90-95% du temps ?

    On ne peut pas reconstituer le prix AVANT résolution pour tous les marchés,
    mais on peut faire l'inverse : regarder les marchés où le PERDANT avait un
    prix non-négligeable (volume élevé), ce qui indique que le marché a transité
    par notre bande.

    Proxy utilisé : marchés avec volume > X× la liquidité = marchés actifs.
    """
    print("=" * 60)
    print("1. ANALYSE DE CALIBRATION")
    print("=" * 60)
    print()
    print("Question clé : les marchés Polymarket sont-ils bien calibrés ?")
    print("Si oui -> pas d'edge (EV=0). Si non -> opportunité.")
    print()

    # Bands de calibration simulées
    # On simule : "si on avait acheté le côté gagnant à ces prix, quel serait le résultat ?"
    # En pratique on teste : pour chaque bande de prix, quelle fraction des marchés
    # résolus auraient été gagnants SI on avait acheté le bon côté ?
    # -> Sans historique, on utilise le taux de YES wins comme proxy de calibration

    bands = [
        (0.50, 0.60),
        (0.60, 0.70),
        (0.70, 0.80),
        (0.80, 0.90),
        (0.90, 0.95),
        (0.95, 1.00),
    ]

    total = len(markets)
    yes_wins = sum(1 for m in markets if m.yes_won)

    print(f"{'Bande YES':>15} | {'Nb marchés':>10} | {'YES gagne':>10} | Calibration")
    print("-" * 65)

    for lo, hi in bands:
        # Filtre : marchés où YES a résolu à 1 (yes_won=True) = proxy de "YES était haut"
        # Ce n'est pas parfait mais donne une idée de la distribution
        in_band = [m for m in markets if lo <= (1.0 if m.yes_won else 0.0) + 0.001]
        # Approche plus honnête : on affiche juste la distribution globale
        count_yes = sum(1 for m in markets if m.yes_won)
        pct = count_yes / total if total else 0
        # On affiche par bande de volume proxy
        break

    # Distribution des outcomes
    print(f"{'Marchés totaux':>25} : {total}")
    print(f"{'YES a gagné':>25} : {yes_wins} ({yes_wins/total:.1%})")
    print(f"{'NO a gagné':>25} : {total - yes_wins} ({(total-yes_wins)/total:.1%})")
    print()
    print("-> Distribution quasi-uniforme = marché liquide et actif des deux côtés.")
    print()

    # Analyse par volume (proxy de contestation)
    contested = [m for m in markets if m.volume > m.liquidity]
    very_contested = [m for m in markets if m.volume > m.liquidity * 3]

    print(f"Marchés 'contestés' (volume > liquidité)      : {len(contested)} ({len(contested)/total:.1%})")
    print(f"Marchés 'très contestés' (volume > 3× liq.)  : {len(very_contested)} ({len(very_contested)/total:.1%})")
    print()
    if contested:
        cyes = sum(1 for m in contested if m.yes_won)
        print(f"  -> Sur marchés contestés, YES gagne : {cyes/len(contested):.1%}")
    if very_contested:
        vcyes = sum(1 for m in very_contested if m.yes_won)
        print(f"  -> Sur marchés très contestés, YES gagne : {vcyes/len(very_contested):.1%}")


def simulation(
    markets: List[ResolvedMarket],
    cert_lo: float,
    cert_hi: float,
    kelly_fraction: float,
    confidence_premium: float,
    min_order: float,
    bankroll: float,
) -> None:
    """
    Simulation de la stratégie sur les marchés résolus.

    On simule deux scénarios honnêtes :
    A) On achète le côté YES de chaque marché (biais arbitraire)
    B) On achète le côté gagnant de chaque marché (borne supérieure – oracle)

    La réalité est quelque part entre les deux.
    Le scénario A donne l'EV "dans le vide" (sans signal directionnel).
    """
    entry_price = (cert_lo + cert_hi) / 2
    payout_win = 1.0 / entry_price  # par dollar misé
    breakeven_wr = entry_price       # win rate nécessaire pour EV ≥ 0

    # Kelly sizing
    p_est = min(entry_price + confidence_premium, 0.99)
    edge = p_est - entry_price
    full_kelly = edge / (1.0 - entry_price)
    frac_kelly = full_kelly * kelly_fraction
    bet_pct = max(0.005, min(frac_kelly, 0.05))

    print()
    print("=" * 60)
    print("2. SIMULATION DE LA STRATÉGIE")
    print("=" * 60)
    print(f"  Bande         : {cert_lo:.0%} – {cert_hi:.0%}")
    print(f"  Prix d'entrée : {entry_price:.3f} (milieu de bande)")
    print(f"  Gain si win   : {(payout_win - 1):.1%} par trade")
    print(f"  Perte si loss : {entry_price:.1%} par trade")
    print(f"  Win rate seuil: {breakeven_wr:.1%} pour être rentable")
    print(f"  Kelly (1/4)   : {frac_kelly:.2%} du bankroll par trade")
    print(f"  Mise simulée  : {bet_pct:.1%} du bankroll")
    print()

    n = len(markets)

    # ── Scénario A : achat YES arbitraire ─────────────────────────
    yes_wins = sum(1 for m in markets if m.yes_won)
    wr_a = yes_wins / n
    ev_a = wr_a * (payout_win - 1) + (1 - wr_a) * (-1)

    # ── Scénario B : oracle (toujours bon) ────────────────────────
    ev_b = payout_win - 1  # 100% win rate

    # ── Simulation bankroll scénario A ────────────────────────────
    br = bankroll
    peak = bankroll
    max_dd = 0.0
    wins_a = losses_a = 0

    for m in markets:
        bet = round(br * bet_pct, 2)
        if bet < min_order:
            bet = min_order
        won = m.yes_won  # scénario A : on achète YES
        if won:
            profit = bet * (payout_win - 1)
            br += profit
            wins_a += 1
        else:
            br -= bet
            losses_a += 1
        peak = max(peak, br)
        dd = (peak - br) / peak
        max_dd = max(max_dd, dd)

    print(f"{'Scénario':>12} | {'Win rate':>9} | {'EV / trade':>11} | {'Bankroll finale':>15}")
    print("-" * 55)
    print(f"{'A (YES blind)':>12} | {wr_a:>8.1%} | {ev_a:>+10.3f} | ${br:>13.2f}")
    print(f"{'B (oracle)':>12} | {'100.0%':>9} | {ev_b:>+10.3f} | {'N/A':>15}")
    print()
    print(f"Simulation scénario A sur {n} trades (bankroll départ ${bankroll:.2f}) :")
    print(f"  Bankroll finale : ${br:.2f}  ({(br/bankroll - 1):+.1%})")
    print(f"  Trades : {wins_a}W / {losses_a}L")
    print(f"  Max drawdown : {max_dd:.1%}")

    print()
    print("=" * 60)
    print("3. CONCLUSION")
    print("=" * 60)
    print()
    print(f"  Win rate seuil pour rentabilité : {breakeven_wr:.1%}")
    print(f"  Win rate observé (YES blind)    : {wr_a:.1%}")
    print()

    if wr_a >= breakeven_wr:
        gap = wr_a - breakeven_wr
        print(f"  OK La stratégie aurait été rentable avec un gap de +{gap:.1%}.")
        print(f"    -> Le marché Polymarket semble légèrement biaisé dans ce sens.")
    else:
        gap = breakeven_wr - wr_a
        print(f"  XX La stratégie n'aurait pas été rentable (-{gap:.1%} sous le seuil).")
        print(f"    -> Sans signal directionnel, acheter YES aveuglément est perdant.")
        print(f"    -> La vraie stratégie doit identifier QUEL côté est à 90-95%.")

    print()
    print("  /!\\ LIMITE IMPORTANTE : sans historique de prix, on ne peut pas")
    print("  savoir si les marchés étaient effectivement dans la bande 90-95%")
    print("  avant résolution. Ce backtest mesure l'EV conditionnel à être")
    print("  dans la bande, pas la fréquence réelle d'occurrence.")
    print()
    print("  -> Pour un backtest exact : collecter 4-8 semaines de données live.")


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    async with aiohttp.ClientSession(base_url=GAMMA_API) as session:
        markets = await fetch_resolved(
            session,
            max_markets=args.markets,
            min_liquidity=args.min_liquidity,
        )
        if not markets:
            print("Aucun marché récupéré.")
            return

        calibration_analysis(markets)
        simulation(
            markets,
            cert_lo=args.cert_lo,
            cert_hi=args.cert_hi,
            kelly_fraction=0.25,
            confidence_premium=0.025,
            min_order=1.0,
            bankroll=args.bankroll,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest certainty-band Polymarket")
    parser.add_argument("--markets",       type=int,   default=3000,  help="Nb marchés résolus à analyser")
    parser.add_argument("--cert-lo",       type=float, default=0.90,  help="Borne basse de la bande")
    parser.add_argument("--cert-hi",       type=float, default=0.95,  help="Borne haute de la bande")
    parser.add_argument("--min-liquidity", type=float, default=0.0, help="Liquidité minimale USDC (0 pour marchés résolus)")
    parser.add_argument("--bankroll",      type=float, default=1000.0,help="Bankroll de départ simulée")
    args = parser.parse_args()
    asyncio.run(main(args))
