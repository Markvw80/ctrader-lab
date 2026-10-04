# ctrader-lab

Research-omgeving voor tradingstrategieën op cTrader (IC Markets). Eerste instrument: XAUUSD.
**Alleen research — geen live orders.** De orderlaag is een interface met een guard die
weigert tenzij `CTRADER_ENV=demo`.

## Status

| Fase | Onderdeel | Status |
|---|---|---|
| 1 | Structuur, Docker, compose | klaar |
| 2 | Datalaag + CSV-import | – |
| 3 | Backtest-engine + tests | – |
| 4 | Optimalisatie + walk-forward | – |
| 5 | Rapportage + webpagina | – |
| 6 | cTrader Open API | – |

## Op de NAS starten (UGREEN DXP2800)

Eenmalig, via SSH op de NAS:

```sh
cd /volume1/docker
git clone <jouw-private-repo-url> ctrader-lab
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
