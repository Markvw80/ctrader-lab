# ctrader-lab

Research-omgeving voor tradingstrategieën op cTrader (IC Markets). Eerste instrument: XAUUSD.
**Alleen research — geen live orders.** De orderlaag is een interface met een guard die
weigert tenzij `CTRADER_ENV=demo`.

## Status

| Fase | Onderdeel | Status |
|---|---|---|
| 1 | Structuur, Docker, compose | klaar |
| 2 | Datalaag + CSV-import | klaar |
| 3 | Backtest-engine + tests | – |
| 4 | Optimalisatie + walk-forward | – |
| 5 | Rapportage + webpagina | – |
| 6 | cTrader Open API | – |

## Op de NAS starten (UGREEN DXP2800)

Eenmalig, via SSH op de NAS. De repo is privé, dus de NAS krijgt een eigen read-only deploy key:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/ctrader_lab_deploy -N ""
cat ~/.ssh/ctrader_lab_deploy.pub
# -> github.com/Markvw80/ctrader-lab/settings/keys -> Add deploy key (zonder write access)
cat >> ~/.ssh/config <<'CFG'
Host github-ctrader-lab
    HostName github.com
    User git
    IdentityFile ~/.ssh/ctrader_lab_deploy
    IdentitiesOnly yes
CFG
```

Daarna:

```sh
cd /volume1/docker
git clone github-ctrader-lab:Markvw80/ctrader-lab.git ctrader-lab
cd ctrader-lab
cp .env.example .env
id                        # zet PUID/PGID in .env op jouw uid/gid
vi .env                   # vul later de cTrader-credentials in
./scripts/deploy.sh
```

`deploy.sh` doet `git pull`, stempelt de git-commit in het image (voor reproduceerbare runs)
en draait `docker compose up -d --build`.

- Webpagina: `http://<nas-ip>:8088` (alleen LAN; open deze poort niet naar internet)
- Health: `http://<nas-ip>:8088/health`
- Data en resultaten staan in `./data` en `./results` en overleven een rebuild.
- Config (`./config`) is read-only gemount: aanpassen kan zonder rebuild.
- Resources: begrensd via `CTLAB_MEM_LIMIT` en `CTLAB_CPUS` in `.env`.
- Eigen compose-project (`ctrader-lab`) en eigen netwerk (`ctlab`); raakt geen andere containers.

### CLI gebruiken

```sh
docker compose exec lab ctlab --help
docker compose exec lab ctlab info
```

### Updaten / stoppen

```sh
./scripts/deploy.sh       # nieuwe versie
docker compose down       # stoppen (data blijft staan)
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

## Nieuwe strategie toevoegen

Wordt uitgewerkt in fase 3. In het kort: één bestand in `src/ctlab/strategy/`, erft van
`Strategy`, declareert getypeerde parameters met bereik, en implementeert `on_bar`.
