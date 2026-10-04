"""cTrader Open API session (read-only: auth, accounts, symbols, history).

Built on the official SDK (ctrader-open-api, Twisted). There is deliberately no order code here.
The Twisted reactor can only run once per process: CLI commands wrap their work in
ctlab.broker.run.run().
"""

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoErrorRes
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAErrorRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetTickDataReq,
    ProtoOAGetTrendbarsReq,
    ProtoOARefreshTokenReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAQuoteType,
    ProtoOATrendbarPeriod,
)
from twisted.internet import defer, reactor

from ctlab.broker.decode import Bar, Tick, decode_ticks, decode_trendbar
from ctlab.config import Env, tokens

CONNECT_TIMEOUT_S = 20
MAX_PARALLEL = 5  # the API allows 5 historical-data requests per second


class ApiError(RuntimeError):
    def __init__(self, code: str, description: str = ""):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code


class Session:
    def __init__(self, cfg: Env):
        self.cfg = cfg
        self.client: Client | None = None
        self.account_id: int | None = None
        self.sem = defer.DeferredSemaphore(MAX_PARALLEL)

    # --- connection ---------------------------------------------------------------------
    async def connect(self, live: bool = False) -> None:
        host = EndPoints.PROTOBUF_LIVE_HOST if live else EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        connected: defer.Deferred = defer.Deferred()
        self.client.setConnectedCallback(lambda _c: connected.called or connected.callback(None))
        self.client.startService()
        connected.addTimeout(CONNECT_TIMEOUT_S, reactor)
        await connected
        await self.request(ProtoOAApplicationAuthReq(
            clientId=self.cfg.ctrader_client_id,
            clientSecret=self.cfg.ctrader_client_secret.get_secret_value(),
        ))

    def close(self) -> None:
        if self.client is not None:
            self.client.stopService()
            self.client = None

    async def request(self, req, timeout: int = 60):
        msg = await self.client.send(req, responseTimeoutInSeconds=timeout)
        payload = Protobuf.extract(msg)
        if isinstance(payload, (ProtoOAErrorRes, ProtoErrorRes)):
            raise ApiError(payload.errorCode, payload.description)
        return payload

    async def open(self) -> None:
        """Connect to the host matching the account (demo/live) and authorize the account.

        Read-only use: historical data may come from a live account; the order layer is separate
        and refuses anything but demo.
        """
        await self.connect(live=False)
        accounts = await self.accounts()
        if not accounts:
            raise ApiError("NO_ACCOUNTS", "Access token has no trading accounts")
        wanted = self.cfg.ctrader_account_id
        acc = next((a for a in accounts if str(a.ctidTraderAccountId) == str(wanted)), None)
        if acc is None:
            ids = ", ".join(str(a.ctidTraderAccountId) for a in accounts)
            raise ApiError("ACCOUNT_NOT_FOUND",
                           f"CTRADER_ACCOUNT_ID={wanted!r} not in token's accounts ({ids}). "
                           "Run `ctlab api accounts`.")
        if acc.isLive:
            self.close()
            await self.connect(live=True)
        await self.request(ProtoOAAccountAuthReq(
            ctidTraderAccountId=acc.ctidTraderAccountId,
            accessToken=tokens()[0],
        ))
        self.account_id = acc.ctidTraderAccountId

    # --- queries ------------------------------------------------------------------------
    async def accounts(self) -> list:
        res = await self.request(ProtoOAGetAccountListByAccessTokenReq(
            accessToken=tokens()[0]))
        return list(res.ctidTraderAccount)

    async def refresh_token(self) -> dict:
        res = await self.request(ProtoOARefreshTokenReq(
            refreshToken=tokens()[1]))
        return {"access_token": res.accessToken, "refresh_token": res.refreshToken,
                "expires_in": res.expiresIn}

    async def symbol(self, name: str):
        res = await self.request(ProtoOASymbolsListReq(ctidTraderAccountId=self.account_id))
        light = next((s for s in res.symbol if s.symbolName.upper() == name.upper()), None)
        if light is None:
            raise ApiError("SYMBOL_NOT_FOUND", name)
        res = await self.request(ProtoOASymbolByIdReq(ctidTraderAccountId=self.account_id,
                                                      symbolId=[light.symbolId]))
        return res.symbol[0]

    async def trendbars(self, symbol_id: int, period: str, from_ms: int, to_ms: int) -> list[Bar]:
        async def _do():
            res = await self.request(ProtoOAGetTrendbarsReq(
                ctidTraderAccountId=self.account_id, symbolId=symbol_id,
                period=ProtoOATrendbarPeriod.Value(period),
                fromTimestamp=from_ms, toTimestamp=to_ms))
            return [decode_trendbar(tb) for tb in res.trendbar]
        return await self.sem.run(lambda: defer.ensureDeferred(_do()))

    async def ticks(self, symbol_id: int, side: str, from_ms: int, to_ms: int) -> tuple[list[Tick], bool]:
        async def _do():
            res = await self.request(ProtoOAGetTickDataReq(
                ctidTraderAccountId=self.account_id, symbolId=symbol_id,
                type=ProtoOAQuoteType.Value(side), fromTimestamp=from_ms, toTimestamp=to_ms))
            return decode_ticks(res.tickData), bool(res.hasMore)
        return await self.sem.run(lambda: defer.ensureDeferred(_do()))
