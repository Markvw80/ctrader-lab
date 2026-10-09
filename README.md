# ctrader-lab

Research-omgeving voor tradingstrategieën op cTrader (IC Markets). Eerste instrument: XAUUSD.
**Alleen research — geen live orders.** De orderlaag is een interface met een guard die
weigert tenzij `CTRADER_ENV=demo`.

## Status

| Fase | Onderdeel | Status |
|---|---|---|
| 1 | Structuur, Docker, compose | klaar |
| 2 | Datalaag + CSV-import | klaar |
| 3 | Backtest-engine + tests | klaar |
| 4 | Optimalisatie + walk-forward | klaar |
| 5 | Rapportage + webpagina | klaar |
| 6 | cTrader Open API | code klaar, app actief |

## Op de NAS starten (UGREEN DXP2800)

De NAS heeft geen git nodig. Vanaf je Mac (met SSH-host `nas` in `~/.ssh/config`):

```sh
./scripts/deploy-nas.sh
```

Dit script:
1. weigert als er niet-gecommitte wijzigingen zijn (gedeployde code = altijd een commit);
2. kopieert de code van die commit naar `/volume1/docker/ctrader-lab` (tar via SSH). `data/`,
   `results/` en `.env` op de NAS worden nooit aangeraakt;
3. maakt bij de eerste keer `.env` aan uit `.env.example` (rechten 600, PUID/PGID van jouw
   NAS-gebruiker);
4. bouwt en start de container met de commit-hash in het image (`docker compose up -d --build`).

Credentials invullen op de NAS: `ssh nas`, dan `vi /volume1/docker/ctrader-lab/.env`, daarna
`cd /volume1/docker/ctrader-lab && docker compose up -d` om ze te laden.

- Webpagina: `http://<nas-ip>:8088` (alleen LAN; open deze poort niet naar internet).
  Via Tailscale: zet `CTLAB_WEB_ALLOW_CIDRS=100.64.0.0/10` in `.env`.
- Data en resultaten staan in `./data` en `./results` en overleven een rebuild.
- Config (`./config`) is read-only gemount: aanpassen kan zonder rebuild (wel: `docker compose restart`).
- Resources: begrensd via `CTLAB_MEM_LIMIT` en `CTLAB_CPUS` in `.env`.
- Eigen compose-project (`ctrader-lab`) en eigen netwerk (`ctlab`); raakt geen andere containers.
- Alternatief met git op de NAS: `scripts/deploy.sh` (doet `git pull` + build).

### CLI gebruiken

```sh
docker compose exec lab ctlab --help
docker compose exec lab ctlab info
```

### Updaten / stoppen

```sh
./scripts/deploy-nas.sh   # vanaf de Mac: nieuwe versie
docker compose down       # op de NAS: stoppen (data blijft staan)
```

## Data

Conventies:
- Alle timestamps in **UTC**. `ts` van een candle = **openingstijd**.
- Basis is M1. Hogere timeframes worden on-the-fly uit M1 afgeleid (`ctlab.data.loader.load_bars`).
  Candles >= H1 volgen de brokerklok (New York + 7 u): D1 begint op de rollover van 17:00 New York.
- Opslag: `data/parquet/<SYMBOL>/<TF>/<jaar>.parquet` (ticks: `TICK/<jaar-maand>.parquet`).
  Bij overlap wint de nieuwste import; duplicaten bestaan niet in de opslag.

```sh
docker compose exec lab ctlab data import-csv /data/raw/xau_m1.csv --format mt5
docker compose exec lab ctlab data validate --symbol XAUUSD --timeframe M1
docker compose exec lab ctlab data list
```

Zet CSV-bestanden in `./data/raw` op de NAS (in de container: `/data/raw`).

### CSV-formaten

Een formaat is een kleine YAML in `config/csv_formats/` (meegeleverd: `generic`, `mt5`,
`dukascopy`, `dukascopy_ticks`). Hierin staan de kolomnamen, het tijdformaat en de bron-tijdzone
(IANA-naam, of `NY+7` voor een MetaTrader-serverklok). Een eigen bestand kan ook:
`--format /data/raw/mijnformaat.yaml`.

### Validatie

Elke import wordt gevalideerd; bij **fouten** wordt niets geschreven (overrulen met `--force`).

| Controle | Niveau |
|---|---|
| Dubbele timestamps met verschillende waarden | fout |
| Niet-uitgelijnde timestamps, OHLC inconsistent, prijs <= 0, ask < bid | fout |
| Tijdzone verdacht: dagelijkse pauze niet om 17:00 New York (per maand, vangt DST-fouten) | fout |
| Data in gesloten markt, sprong > 2% in één candle | waarschuwing |
| Gaten >= 60 min terwijl de markt open hoort te zijn (feestdagen, ontbrekende data) | waarschuwing |
| Korte gaten (normaal op M1 in stille momenten) | info |

Het rapport staat ook in `data/parquet/<SYMBOL>/<TF>/_validation.json`.

## Lokaal ontwikkelen

```sh
uv sync
uv run pytest
uv run ctlab info
```

## Secrets

Alleen in `.env` (staat in `.gitignore`). Nooit in code, config of git.

## Backtest

```sh
docker compose exec lab ctlab strategies                       # strategieën + parameters
docker compose exec lab ctlab backtest session_breakout --start 2023-01-01 --end 2025-01-01 -p rr=2.0
docker compose exec lab ctlab backtest mean_reversion --spread-stat p90 --risk 0.25
docker compose exec lab ctlab check mean_reversion              # look-ahead-controle
```

Elke run krijgt een id en een map `results/runs/<id>/` met `meta.json` (strategie, parameters,
symbool, timeframes, gevraagde en werkelijke datarange, data-fingerprint, accountinstellingen,
volledig kostenmodel, git-commit, uv.lock-hash, commando), `metrics.json`, trades, equity en
uitsplitsingen per maand, weekdag en sessie.

### Hoe de engine rekent

- **Twee timeframes.** De strategie beslist op haar eigen timeframe (bijv. M15); fills, SL/TP en
  kosten worden per M1-candle gesimuleerd.
- **Geen look-ahead.** Een beslissing op de slotkoers van candle *j* wordt uitgevoerd op de open
  van de eerste M1-candle ná het sluiten van *j*. De strategie ziet alleen data t/m *j*.
- **Bid/ask.** Candles zijn bid. Ask = bid + spread (per uur New York-tijd; na `calibrate-spread`
  gemeten uit echte ticks). Long koopt op ask en verkoopt op bid, short omgekeerd.
- **Conservatief:** SL en TP in dezelfde candle telt als SL; op de candle waarin een
  stop/limit-order vult wordt alleen SL gecontroleerd; opent de koers voorbij de SL, dan vult
  het op de open. Slippage (standaard 2 ticks) op market, stop-entry en SL; niet op TP/limit.
- **Kosten:** commissie per kant (op notional, per lot of %), swap per rollover (17:00 New York,
  drievoudig op woensdag, niet in het weekend), spread en slippage. Alles staat per trade apart.
- **Risico:** lotgrootte = risico% van equity / (verlies per lot bij de SL incl. slippage en
  commissie), naar beneden afgerond op de lotstap; ook begrensd door `max_leverage`.
  Dagelijkse verlieslimiet per handelsdag (start 17:00 New York): bij overschrijding wordt de
  positie bij de volgende open gesloten en worden nieuwe orders die dag geweigerd.
- Eén positie tegelijk; zodra een positie opent vervallen andere pending orders (OCO).

## Optimalisatie en walk-forward

```sh
docker compose exec lab ctlab optimize session_breakout --trials 100      # hele onderzoeksperiode
docker compose exec lab ctlab walkforward session_breakout --trials 50    # oordeel: ROBUST / NOT ROBUST
docker compose exec lab ctlab holdout <run-id>                            # één keer, aan het eind
docker compose exec lab ctlab runs
```

Werkwijze: ontwikkel en optimaliseer alleen op de onderzoeksperiode, beoordeel met
`walkforward`, en test de gekozen parameters pas helemaal aan het eind **één keer** op de holdout.

- **Holdout.** Bij het eerste gebruik wordt de laatste 20% van de data afgesplitst en de
  startdatum vastgelegd in `data/research/holdout_XAUUSD.json`. Die datum schuift nooit op:
  nieuw opgehaalde data valt ook in de holdout. `optimize` en `walkforward` lezen nooit data
  vanaf die datum. `holdout` waarschuwt als de holdout voor dezelfde strategie al eerder is
  gebruikt (elke extra blik maakt hem minder onafhankelijk).
- **Walk-forward.** In-sample 12 maanden optimaliseren, out-of-sample 3 maanden testen met de
  beste parameters, 3 maanden doorschuiven (instelbaar; ook `--anchored`). Elk OOS-venster
  start met de eindbalans van het vorige: de gecombineerde OOS-equity is één eerlijke reeks.
  Indicatoren krijgen `warmup_days` aan eerdere data, zonder trades in die periode.
- **Minimum trades.** In-sample trials met minder dan `min_trades_per_window` trades worden
  afgewezen; OOS-vensters met minder dan `min_trades_oos` trades tellen niet als bewijs.
- **Gevoeligheid.** Elke parameter wordt een stap en ~10% van zijn bereik verschoven. Zakt de
  doelwaarde dan onder 50% van het optimum, of is minder dan 75% van de buren winstgevend,
  dan heet het optimum "sensitive" (een piek in plaats van een plateau).
- **Oordeel NOT ROBUST** bij één of meer van: OOS verliesgevend; OOS-rendement per jaar
  < 50% van in-sample; < 50% van de (beoordeelbare) OOS-vensters winstgevend; te weinig
  OOS-trades; vensters waarin zelfs de beste in-sample-parameters verliezen; gevoelig optimum.
  Alle drempels staan in `config/settings.yaml` onder `optimization`.
- Elke Optuna-studie staat als `optuna.db` (SQLite) in de run-map; de seed staat in de
  settings, dus een run is reproduceerbaar.

## cTrader Open API

Eenmalig een access token aanmaken:
1. https://openapi.ctrader.com/apps → jouw app → **Playground**
2. Scope **accounts** (alleen lezen: genoeg voor research, kan geen orders plaatsen) → *Get token*
3. Log in met je cTrader ID en geef toegang tot je (demo)account
4. Zet `CTRADER_ACCESS_TOKEN` en `CTRADER_REFRESH_TOKEN` in `.env` op de NAS en herstart:
   `docker compose up -d`

```sh
docker compose exec lab ctlab api check                 # stap voor stap: app, token, account
docker compose exec lab ctlab api accounts              # account-ids -> CTRADER_ACCOUNT_ID in .env
docker compose exec lab ctlab api symbol                # echte contract-, commissie- en swapwaarden
docker compose exec lab ctlab data calibrate-spread --days 20
docker compose exec lab ctlab data fetch --start 2020-01-01
docker compose exec lab ctlab data fetch                # later: alleen nieuwe candles
docker compose exec lab ctlab api refresh-token         # token verloopt na ~30 dagen
```

Broker-waarden (symbol spec en gemeten spread) komen in `data/broker/XAUUSD.json` en gaan voor
op `config/symbols/XAUUSD.yaml`. Een vernieuwd token komt in `data/broker/.tokens.json` (0600)
en gaat voor op `.env`.

## Rapporten en webpagina

Na elke `backtest`, `optimize`, `walkforward` en `holdout` staat er een HTML-rapport in
`results/runs/<id>/report.html`: kerncijfers, equity en drawdown, resultaat per maand, weekdag
en sessie, kosten, exit-redenen, trades, en bij optimalisatie/walk-forward ook het oordeel,
de vensters, de gevoeligheidstabel en per parameter een scatter (plateau of piek?).
Rapporten zijn één bestand zonder externe scripts en werken dus ook offline.

```sh
docker compose exec lab ctlab report <run-id>     # opnieuw maken
docker compose exec lab ctlab report              # alle runs opnieuw (bijv. na een update)
```

Webpagina: `http://<nas-ip>:8088`

- Overzicht van alle runs met filter op soort en strategie
- Rapport per run, trades als CSV-download
- Vergelijken: vink 2–6 runs aan → rendementscurves over elkaar, metrics naast elkaar
  (beste waarde vet), parameters naast elkaar
- Alleen-lezen: vanaf de pagina kun je niets starten of wijzigen
- Verzoeken van buiten privé-netwerkadressen krijgen 403. Open poort 8088 niet in je router.

## Nieuwe strategie toevoegen

1. Maak een bestand in `src/ctlab/strategies/`, bijvoorbeeld `mijn_strategie.py`:

```python
import numpy as np
import polars as pl

from ctlab.strategy.base import Context, Strategy, register
from ctlab.strategy.params import FloatParam, IntParam


@register
class MijnStrategie(Strategy):
    name = "mijn_strategie"
    timeframe = "M15"
    warmup_bars = 50

    fast = IntParam(10, 3, 50, doc="snelle EMA")
    slow = IntParam(40, 20, 200, doc="trage EMA")
    sl_usd = FloatParam(8.0, 2.0, 30.0, step=0.5)
    rr = FloatParam(2.0, 0.5, 4.0, step=0.1)

    def prepare(self, bars: pl.DataFrame) -> dict[str, np.ndarray]:
        # Vectorized, maar ALLEEN causaal: geen shift(-n), geen centered windows.
        c = pl.col("close")
        df = bars.select(c.ewm_mean(span=self.fast, adjust=False).alias("f"),
                         c.ewm_mean(span=self.slow, adjust=False).alias("s"))
        return {"fast": df["f"].to_numpy(), "slow": df["s"].to_numpy()}

    def on_bar(self, ctx: Context) -> None:
        f, s = ctx.bars.fast, ctx.bars.slow          # alleen t/m de huidige candle
        crossed_up = f[-2] <= s[-2] and f[-1] > s[-1]
        if ctx.position is None and crossed_up and ctx.now("sess_london"):
            ctx.buy(self.sl_usd, self.sl_usd * self.rr)
```

2. Beschikbaar in `prepare`/`ctx`: `open high low close tick_volume ts`, plus `trading_day`,
   `ny_minute`, `ny_weekday`, `sess_asia`, `sess_london`, `sess_newyork`.
3. Acties: `buy/sell(sl, tp)`, `buy_stop/sell_stop/buy_limit/sell_limit(price, sl, tp, expire_bars)`,
   `close()`, `cancel_all()`. SL/TP zijn **afstanden in prijs** vanaf de werkelijke fill.
4. Controleer: `ctlab check mijn_strategie` (moet OK zijn) en `uv run pytest`.
5. Deploy: commit + push, op de NAS `./scripts/deploy.sh`.
