import abc
import typing
import datetime as dt
from sentry_sdk import configure_scope

from shioaji.account import Account, AccountType, FutureAccount, StockAccount
from shioaji.backend.solace.utils import (
    get_contracts_filename,
    load_contracts_file,
    mockca,
    new_contracts,
)
from shioaji.backend.utils import create_solace
from shioaji.backend.solace.tick import TickSTKv1, TickFOPv1
from shioaji.backend.solace.bidask import BidAskSTKv1, BidAskFOPv1
from shioaji.backend.solace.quote import QuoteSTKv1
from shioaji.constant import (
    Action,
    Exchange,
    OrderState,
    SecurityType,
    Status,
    Unit,
    ScannerType,
    TicksQueryType,
    QuoteType,
    QuoteVersion,
)
from shioaji.contracts import (
    BaseContract,
    ComboContract,
    Contract,
    Contracts,
    FetchStatus,
    Future,
    Index,
    Option,
    Stock,
)
from shioaji.position import (
    FuturePosition,
    FuturePositionDetail,
    FutureProfitLoss,
    Margin,
    SettlementV1,
    StockPositionDetail,
    StockProfitLoss,
    StockPosition,
    Settlement,
    AccountBalance,
    StockProfitDetail,
    FutureProfitDetail,
    FutureProfitLossSummary,
    StockProfitLossSummary,
)
from shioaji.data import (
    DailyQuotes,
    ShortStockSource,
    Snapshot,
    Ticks,
    Kbars,
    CreditEnquire,
    ScannerItem,
    UsageStatus,
)
from shioaji.reserve import (
    EarmarkStocksDetailResponse,
    ReserveEarmarkingResponse,
    ReserveStockResponse,
    ReserveStocksDetailResponse,
    ReserveStocksSummaryResponse,
)
from shioaji.order import (
    ComboOrder,
    Order,
    OrderDealRecords,
    StrictInt,
    Trade,
    ComboTrade,
    conint,
)
from shioaji.orderprops import OrderProps
from shioaji.utils import (
    log,
    set_error_tracking,
    check_contract_cache,
    clear_outdated_contract_cache,
    LEGACY_TEST,
)
from shioaji.error import (
    AccountNotProvideError,
    AccountNotSignError,
    TargetContractNotExistError,
)


class Quote:
    @abc.abstractmethod
    def subscribe(
        self,
        contract: Contract,
        quote_type: QuoteType = QuoteType.Tick,
        intraday_odd: bool = False,
        version: QuoteVersion = QuoteVersion.v1,
    ):
        pass

    @abc.abstractmethod
    def unsubscribe(
        self,
        contract: Contract,
        quote_type: QuoteType = QuoteType.Tick,
        intraday_odd: bool = False,
        version: QuoteVersion = QuoteVersion.v1,
    ):
        pass

    @abc.abstractmethod
    def set_on_tick_stk_v1_callback(
        self,
        func: typing.Callable[[Exchange, TickSTKv1], None],
        bind: bool = False,
    ) -> None:
        pass

    @abc.abstractmethod
    def set_on_tick_fop_v1_callback(
        self,
        func: typing.Callable[[Exchange, TickFOPv1], None],
        bind: bool = False,
    ) -> None:
        pass

    @abc.abstractmethod
    def set_on_bidask_stk_v1_callback(
        self,
        func: typing.Callable[[Exchange, BidAskSTKv1], None],
        bind: bool = False,
    ) -> None:
        pass

    @abc.abstractmethod
    def set_on_bidask_fop_v1_callback(
        self,
        func: typing.Callable[[Exchange, BidAskFOPv1], None],
        bind: bool = False,
    ) -> None:
        pass

    @abc.abstractmethod
    def set_on_quote_stk_v1_callback(
        self,
        func: typing.Callable[[Exchange, QuoteSTKv1], None],
        bind: bool = False,
    ) -> None:
        pass


class Shioaji:
    """shioaji api

    Functions:
        login
        logout
        activate_ca
        list_accounts
        set_default_account
        get_account_margin
        get_account_openposition
        get_account_settle_profitloss
        get_stock_account_funds
        get_stock_account_unreal_profitloss
        get_stock_account_real_profitloss
        place_order
        update_order
        update_status
        list_trades

    Objects:
        Quote
        Contracts
        Order
    """

    def __init__(
        self,
        simulation: bool = False,
        proxies: typing.Dict[str, str] = {},
        currency: str = "NTD",
        vpn: bool = False,
    ):
        """initialize Shioaji to start trading

        Args:
            simulation (bool):
                - False: to trading on real market (just use your Sinopac account to start trading)
                - True: become simulation account(need to contract as to open simulation account)
            proxies (dict): specific the proxies of your https
                ex: {'https': 'your-proxy-url'}
            currency (str): {NTX, USX, NTD, USD, HKD, EUR, JPY, GBP}
                set the default currency for display
        """

        self.quote: Quote
        self.stock_account = None
        self.futopt_account = None
        self.OrderProps = OrderProps
        self.Order = Order
        self.ComboOrder = ComboOrder
        self._currency = currency
        self.simulation = simulation
        self.proxies = proxies
        self.vpn = vpn

        # TODO: change it to False if paper trade go production
        self._simu_to_stag = False
        self._setup_solace()

    def _setup_solace(self):
        if self.vpn:
            self._solace = create_solace("vpn", self.proxies, self.simulation)
            self._solace_implicit = None
            self._solace.activated_ca = mockca()

        elif self._simu_to_stag:
            self._solace = create_solace("stag", self.proxies, self.simulation)
            # solace_implicit: connect to release site
            self._solace_implicit = create_solace("prod", self.proxies, self.simulation)

        else:
            self._solace = create_solace("prod", self.proxies, self.simulation)
            self._solace_implicit = None

        if self.simulation:
            self._solace.activated_ca = mockca()

        self.quote = self._solace

    def _trace_log(self, trade: Trade):
        if not self.simulation:
            return
        if dt.datetime.utcnow().weekday() >= 5:
            return
        elif dt.datetime.utcnow().hour >= 12:
            return

        if not self._simu_to_stag:
            accounts = [
                acc
                for acc in self._solace.list_accounts()
                if acc.account_type == trade.order.account.account_type
            ]
            not_signed = any([not acc.signed for acc in accounts])
            if not_signed:
                if not self._solace_implicit:
                    self._solace_implicit = create_solace(
                        "stag", self.proxies, self.simulation
                    )
                    simulation_token = self._solace.session._token
                    _ = self._solace_implicit.simulation_login(
                        simulation_token,
                        person_id=self._solace._person_id,
                        subscribe_trade=False,
                    )
                self._solace_implicit.trace_log(trade)
        else:
            self._solace.trace_log(trade)

    def _portfolio_default_account(self):
        if self.stock_account:
            return self.stock_account
        elif self.futopt_account:
            return self.futopt_account
        else:
            raise AccountNotProvideError("Please provide valid account.")

    def fetch_contracts(
        self,
        contract_download: bool = False,
        contracts_timeout: int = 0,
        contracts_cb: typing.Callable[[], None] = None,
    ):
        self.Contracts = self._solace.Contracts = new_contracts()
        contract_file = get_contracts_filename()
        clear_outdated_contract_cache(contract_file)
        todayfile_exist = check_contract_cache(contract_file)
        if contract_download or not todayfile_exist:
            self._solace.fetch_all_contract(contracts_timeout, contracts_cb)
        else:
            if self.Contracts.status == FetchStatus.Unfetch:
                self.Contracts.status = FetchStatus.Fetching
                self.Contracts = self._solace.Contracts = load_contracts_file()
                if not self.Contracts:
                    self._solace.fetch_all_contract(contracts_timeout, contracts_cb)
                else:
                    if contracts_cb:
                        for securitytype in SecurityType:
                            contracts_cb(securitytype)
            else:
                pass

    def usage(
        self,
        timeout: int = 5000,
        cb: typing.Callable[[UsageStatus], None] = None,
    ) -> UsageStatus:
        return self._solace.usage(timeout, cb)

    def login(
        self,
        api_key: str,
        secret_key: str,
        fetch_contract: bool = True,
        contracts_timeout: int = 0,
        contracts_cb: typing.Callable[[], None] = None,
        subscribe_trade: bool = True,
        receive_window: int = 30000,
    ) -> typing.List[Account]:
        if self._simu_to_stag:
            (
                accounts,
                contract_download,
                person_id,
            ) = self._solace_implicit.token_login(
                api_key, secret_key, subscribe_trade, receive_window
            )
            simulation_token = self._solace_implicit.session._token
            self._solace_implicit.logout()
            accounts, contract_download = self._solace.simulation_login(
                simulation_token,
                person_id,
                subscribe_trade,
            )
        else:
            accounts, contract_download, person_id = self._solace.token_login(
                api_key, secret_key, subscribe_trade, receive_window
            )
        if accounts:
            with configure_scope() as scope:
                scope.user = dict(id=person_id, username=accounts[0].username)
        error_tracking = self._solace.error_tracking(person_id)
        set_error_tracking(self.simulation, error_tracking)
        if fetch_contract:
            self.fetch_contracts(contract_download, contracts_timeout, contracts_cb)
        self.stock_account = self._solace.default_stock_account
        self.futopt_account = self._solace.default_futopt_account
        return accounts

    def logout(self) -> bool:
        """logout shioaji api"""
        res = self._solace.logout()
        return res

    def subscribe_trade(self, account: Account) -> bool:
        res = self._solace.subscribe_trade(account, True)
        return res

    def unsubscribe_trade(self, account: Account) -> bool:
        res = self._solace.subscribe_trade(account, False)
        return res

    def activate_ca(self, ca_path: str, ca_passwd: str, person_id: str = "", store: int = 0):
        """activate your ca for trading

        Args:
            ca_path (str):
                the path of your ca, support both absloutely and relatively path, use same ca with eleader
            ca_passwd (str): password of your ca
        """
        res = self._solace.activate_ca(ca_path, ca_passwd, person_id, store)
        return res

    def get_ca_expiretime(self, person_id: str) -> dt.datetime:
        res = self._solace.get_ca_expiretime(person_id)
        return res

    def list_accounts(self) -> typing.List[Account]:
        """list all account you have"""
        return self._solace.list_accounts()

    def set_default_account(self, account):
        """set default account for trade when place order not specific

        Args:
            account (:obj:Account):
                choice the account from listing account and set as default
        """
        if isinstance(account, StockAccount):
            self._solace.default_stock_account = account
            self.stock_account = account
        elif isinstance(account, FutureAccount):
            self._solace.default_futopt_account = account
            self.futopt_account = account

    def place_order(
        self,
        contract: Contract,
        order: Order,
        timeout: int = 5000,
        cb: typing.Callable[[Trade], None] = None,
    ) -> Trade:
        """placing order

        Args:
            contract (:obj:Shioaji.Contract):
            order (:obj:Shioaji.Order):
                pass Shioaji.Order object to place order
        """
        if not order.account:
            if isinstance(contract, Future) or isinstance(contract, Option):
                order.account = self.futopt_account
            elif isinstance(contract, Stock):
                order.account = self.stock_account
            else:
                log.error("Please provide the account place to.")
                return None

        if contract.target_code:
            if self.Contracts.Futures.get(contract.target_code) is None:
                raise TargetContractNotExistError(contract)
            contract = self.Contracts.Futures.get(contract.target_code)

        trade = self._solace.place_order(contract, order, timeout, cb)

        if LEGACY_TEST and self.simulation:
            self._trace_log(trade)

        return trade

    def update_order(
        self,
        trade: Trade,
        price: typing.Union[StrictInt, float] = None,
        qty: int = None,
        timeout: int = 5000,
        cb: typing.Callable[[Trade], None] = None,
    ) -> Trade:
        """update the order price or qty

        Args:
            trade (:obj:Trade):
                pass place_order return Trade object to update order
            price (float): the price you want to replace
            qty (int): the qty you want to subtract
        """
        trade = self._solace.update_order(trade, price, qty, timeout, cb)
        return trade

    def cancel_order(
        self,
        trade: Trade,
        timeout: int = 5000,
        cb: typing.Callable[[Trade], None] = None,
    ) -> Trade:
        """cancel order

        Args:
            trade (:obj:Trade):
                pass place_order return Trade object to cancel order
        """
        trade = self._solace.cancel_order(trade, timeout, cb)
        return trade

    def place_comboorder(
        self,
        combo_contract: ComboContract,
        order: ComboOrder,
        timeout: int = 5000,
        cb: typing.Callable[[ComboTrade], None] = None,
    ) -> ComboTrade:
        """placing combo order

        Args:
            combocontract (:obj:List of legs):
            order (:obj:Shioaji.Order):
                pass Shioaji.Order object to place combo order
        """
        if not len(combo_contract.legs) == 2:
            log.error("Just allow order with two contracts.")

        for leg in combo_contract.legs:
            if leg.target_code:
                target_contract = self.Contracts.Futures.get(leg.target_code)
                if target_contract is None:
                    raise TargetContractNotExistError(leg)
                leg.code = target_contract.code
                leg.name = target_contract.name
                leg.symbol = target_contract.symbol
                leg.target_code = target_contract.target_code

        if order.account:
            if order.account.account_type == AccountType.Future:
                return self._solace.place_comboorder(combo_contract, order, timeout, cb)
            else:
                raise AccountNotProvideError("Please provide valid account.")
        else:
            if not self.futopt_account:
                raise AccountNotProvideError("Please provide valid account.")
            else:
                order.account = self.futopt_account
                return self._solace.place_comboorder(combo_contract, order, timeout, cb)

    def cancel_comboorder(
        self,
        combotrade: ComboTrade,
        timeout: int = 5000,
        cb: typing.Callable[[ComboTrade], None] = None,
    ) -> ComboTrade:
        """cancel combo order

        Args:
            trade (:obj:Trade):
                pass place_order return Trade object to cancel order
        """
        trade = self._solace.cancel_comboorder(combotrade, timeout, cb)
        return trade

    def update_status(
        self,
        account: Account = None,
        trade: Trade = None,
        timeout: int = 5000,
        cb: typing.Callable[[typing.List[Trade]], None] = None,
    ):
        """update status of all trades you have"""
        if trade:
            self._solace.update_status(
                trade.order.account,
                seqno=trade.order.seqno,
                timeout=timeout,
                cb=cb,
            )
        elif account:
            if account.signed or self.simulation:
                self._solace.update_status(account, timeout=timeout, cb=cb)
        else:
            if self.stock_account:
                if self.stock_account.signed or self.simulation:
                    self._solace.update_status(
                        self.stock_account, timeout=timeout, cb=cb
                    )
            if self.futopt_account:
                if self.futopt_account.signed or self.simulation:
                    self._solace.update_status(
                        self.futopt_account, timeout=timeout, cb=cb
                    )

    def stock_reserve_summary(
        self,
        account: Account,
        timeout: int = 5000,
        cb: typing.Callable[[ReserveStocksSummaryResponse], None] = None,
    ) -> ReserveStocksSummaryResponse:
        if account.signed:
            resp = self._solace.stock_reserve_summary(account, timeout, cb)
            return resp
        else:
            raise AccountNotSignError(account)

    def stock_reserve_detail(
        self,
        account: Account,
        timeout: int = 5000,
        cb: typing.Callable[[ReserveStocksDetailResponse], None] = None,
    ) -> ReserveStocksDetailResponse:
        if account.signed:
            resp = self._solace.stock_reserve_detail(account, timeout, cb)
            return resp
        else:
            raise AccountNotSignError(account)

    def reserve_stock(
        self,
        account: Account,
        contract: Contract,
        share: int,
        timeout: int = 5000,
        cb: typing.Callable[[ReserveStockResponse], None] = None,
    ) -> ReserveStockResponse:
        if account.signed:
            resp = self._solace.reserve_stock(account, contract, share, timeout, cb)
            return resp
        else:
            raise AccountNotSignError(account)

    def earmarking_detail(
        self,
        account: Account,
        timeout: int = 5000,
        cb: typing.Callable[[EarmarkStocksDetailResponse], None] = None,
    ) -> EarmarkStocksDetailResponse:
        if account.signed:
            resp = self._solace.earmarking_detail(account, timeout, cb)
            return resp
        else:
            raise AccountNotSignError(account)

    def reserve_earmarking(
        self,
        account: Account,
        contract: Contract,
        share: int,
        price: float,
        timeout: int = 5000,
        cb: typing.Callable[[ReserveEarmarkingResponse], None] = None,
    ) -> ReserveEarmarkingResponse:
        if account.signed:
            resp = self._solace.reserve_earmarking(
                account, contract, share, price, timeout, cb
            )
            return resp
        else:
            raise AccountNotSignError(account)

    def update_combostatus(
        self,
        account: Account = None,
        timeout: int = 5000,
        cb: typing.Callable[[typing.List[ComboTrade]], None] = None,
    ):
        if account and account.signed:
            if account.account_type == "F":
                self._solace.update_combostatus(account, timeout=timeout, cb=cb)
            else:
                raise AccountNotProvideError("Please provide valid account.")
        else:
            if self.futopt_account and self.futopt_account.signed:
                self._solace.update_combostatus(
                    self.futopt_account, timeout=timeout, cb=cb
                )
            else:
                raise AccountNotProvideError("Please provide valid account.")

    def list_positions(
        self,
        account: Account = None,
        unit: Unit = Unit.Common,
        timeout: int = 5000,
        cb: typing.Callable[
            [typing.List[typing.Union[StockPosition, FuturePosition]]], None
        ] = None,
    ) -> typing.List[typing.Union[StockPosition, FuturePosition]]:
        """query account of unrealized gain or loss
        Args:
            account (:obj:Account):
                choice the account from listing account (Default: stock account)
        """
        if account:
            return self._solace.list_positions(
                account, unit=unit, timeout=timeout, cb=cb
            )
        else:
            default_account = self._portfolio_default_account()
            return self._solace.list_positions(
                default_account, unit=unit, timeout=timeout, cb=cb
            )

    def list_position_detail(
        self,
        account: Account = None,
        detail_id: int = 0,
        timeout: int = 5000,
        cb: typing.Callable[
            [typing.List[typing.Union[StockPositionDetail, FuturePositionDetail]]],
            None,
        ] = None,
    ) -> typing.List[typing.Union[StockPositionDetail, FuturePositionDetail]]:
        """query account of position detail

        Args:
            account (:obj:Account):
                choice the account from listing account (Default: stock account)
            detail_id (int): the id is from Position object, Position is from list_position
        """
        if account:
            return self._solace.list_position_detail(
                account, detail_id, timeout=timeout, cb=cb
            )
        else:
            default_account = self._portfolio_default_account()
            return self._solace.list_position_detail(
                default_account, detail_id, timeout=timeout, cb=cb
            )

    def list_profit_loss(
        self,
        account: Account = None,
        begin_date: str = "",
        end_date: str = "",
        unit: Unit = Unit.Common,
        timeout: int = 5000,
        cb: typing.Callable[
            [typing.List[typing.Union[StockProfitLoss, FutureProfitLoss]]], None
        ] = None,
    ) -> typing.List[typing.Union[StockProfitLoss, FutureProfitLoss]]:
        """query account of profit loss

        Args:
            account (:obj:Account):
                choice the account from listing account (Default: stock account)
            begin_date (str): the start date of query profit loss (Default: today)
            end_date (str): the end date of query profit loss (Default: today)
        """
        if account:
            return self._solace.list_profit_loss(
                account, begin_date, end_date, unit=unit, timeout=timeout, cb=cb
            )
        else:
            default_account = self._portfolio_default_account()
            return self._solace.list_profit_loss(
                default_account,
                begin_date,
                end_date,
                unit=unit,
                timeout=timeout,
                cb=cb,
            )

    def list_profit_loss_detail(
        self,
        account: Account = None,
        detail_id: int = 0,
        unit: Unit = Unit.Common,
        timeout: int = 5000,
        cb: typing.Callable[
            [typing.List[typing.Union[StockProfitDetail, FutureProfitDetail]]],
            None,
        ] = None,
    ) -> typing.List[typing.Union[StockProfitDetail, FutureProfitDetail]]:
        """query account of profit loss detail

        Args:
            account (:obj:Account):
                choice the account from listing account (Default: stock account)
            detail_id (int): the id is from ProfitLoss object, ProfitLoss is from list_profit_loss
        """
        if account:
            return self._solace.list_profit_loss_detail(
                account, detail_id, unit=unit, timeout=timeout, cb=cb
            )
        else:
            default_account = self._portfolio_default_account()
            return self._solace.list_profit_loss_detail(
                default_account, detail_id, unit=unit, timeout=timeout, cb=cb
            )

    def list_profit_loss_summary(
        self,
        account: Account = None,
        begin_date: str = "",
        end_date: str = "",
        timeout: int = 5000,
        cb: typing.Callable[
            [
                typing.List[
                    typing.Union[StockProfitLossSummary, FutureProfitLossSummary]
                ]
            ],
            None,
        ] = None,
    ) -> typing.List[typing.Union[StockProfitLossSummary, FutureProfitLossSummary]]:
        """query summary profit loss of a period time

        Args:
            account (:obj:Account):
                choice the account from listing account (Default: stock account)
            begin_date (str): the start date of query profit loss (Default: today)
            end_date (str): the end date of query profit loss (Default: today)
        """
        if account:
            return self._solace.list_profit_loss_summary(
                account, begin_date, end_date, timeout=timeout, cb=cb
            )
        else:
            default_account = self._portfolio_default_account()
            return self._solace.list_profit_loss_summary(
                default_account,
                begin_date,
                end_date,
                timeout=timeout,
                cb=cb,
            )

    def list_settlements(
        self,
        account: Account = None,
        timeout: int = 5000,
        cb: typing.Callable[[typing.List[Settlement]], None] = None,
    ) -> typing.List[Settlement]:
        """query stock account of settlements"""
        if self.stock_account:
            return self._solace.list_settlements(
                self.stock_account, timeout=timeout, cb=cb
            )

    def settlements(
        self,
        account: Account = None,
        timeout: int = 5000,
        cb: typing.Callable[[typing.List[SettlementV1]], None] = None,
    ) -> typing.List[Settlement]:
        """query stock account of settlements"""
        if account:
            return self._solace.settlements(account, timeout=timeout, cb=cb)
        else:
            if self.stock_account:
                return self._solace.settlements(
                    self.stock_account, timeout=timeout, cb=cb
                )

    def margin(
        self,
        account: Account = None,
        timeout: int = 5000,
        cb: typing.Callable[[Margin], None] = None,
    ) -> Margin:
        """query future account of margin"""
        if account:
            return self._solace.margin(account, timeout=timeout, cb=cb)
        else:
            if self.futopt_account:
                return self._solace.margin(self.futopt_account, timeout=timeout, cb=cb)

    def list_trades(self) -> typing.List[Trade]:
        """list all trades"""
        return self._solace.trades

    def list_combotrades(self) -> typing.List[ComboTrade]:
        """list all combotrades"""
        return self._solace.combotrades

    def ticks(
        self,
        contract: BaseContract,
        date: str = dt.date.today().strftime("%Y-%m-%d"),
        query_type: TicksQueryType = TicksQueryType.AllDay,
        time_start: typing.Union[str, dt.time] = None,
        time_end: typing.Union[str, dt.time] = None,
        last_cnt: int = 0,
        timeout: int = 30000,
        cb: typing.Callable[[Ticks], None] = None,
    ) -> Ticks:
        """get contract tick volumn

        Arg:
            contract (:obj:Shioaji.BaseContract)
            date (str): "2020-02-02"
        """
        ticks = self._solace.ticks(
            contract,
            date,
            query_type,
            time_start,
            time_end,
            last_cnt,
            timeout,
            cb,
        )
        return ticks

    def kbars(
        self,
        contract: BaseContract,
        start: str = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        end: str = dt.date.today().strftime("%Y-%m-%d"),
        timeout: int = 30000,
        cb: typing.Callable[[Kbars], None] = None,
    ) -> Kbars:
        """get Kbar

        Arg:
            contract (:obj:Shioaji.BaseContract)
            start (str): "2020-02-02"
            end (str): "2020-06-02"
        """
        kbars = self._solace.kbars(contract, start, end, timeout, cb)
        return kbars

    def daily_quotes(
        self,
        date: dt.date = dt.date.today(),
        exclude: bool = True,
        timeout: int = 5000,
        cb: typing.Callable[[DailyQuotes], None] = None,
    ) -> DailyQuotes:
        """get daily quote

        Args:
            date (:datetime:date):
                date for quote (Default: today)
            exclude (:bool):
                exclude warrant data (Default: True)
        """
        daily_quotes = self._solace.daily_quotes(date, exclude, timeout, cb)
        return daily_quotes

    def snapshots(
        self,
        contracts: typing.List[typing.Union[Option, Future, Stock, Index]],
        timeout: int = 30000,
        cb: typing.Callable[[Snapshot], None] = None,
    ) -> typing.List[Snapshot]:
        """get contract snapshot info

        Arg:
            contract (:obj:List of contract)
        """
        snapshots = self._solace.snapshots(contracts, timeout, cb)
        return snapshots

    def scanners(
        self,
        scanner_type: ScannerType,
        ascending: bool = True,
        date: str = None,
        count: conint(ge=1, le=200) = 100,
        timeout: int = 30000,
        cb: typing.Callable[[typing.List[ScannerItem]], None] = None,
    ) -> typing.List[ScannerItem]:
        """get contract snapshot info

        Arg:
            contract (:obj:List of contract)
        """
        scanners = self._solace.scanners(
            scanner_type, ascending, date, count, timeout, cb
        )
        return scanners

    def credit_enquires(
        self,
        contracts: typing.List[Stock],
        timeout: int = 30000,
        cb: typing.Callable[[CreditEnquire], None] = None,
    ) -> typing.List[CreditEnquire]:
        """get contract snapshot info

        Arg:
            contract (:obj:List of contract)
        """
        credit_enquires = self._solace.credit_enquires(contracts, timeout, cb)
        return credit_enquires

    def short_stock_sources(
        self,
        contracts: typing.List[Stock],
        timeout: int = 5000,
        cb: typing.Callable[[ShortStockSource], None] = None,
    ) -> typing.List[ShortStockSource]:
        """get contract snapshot info

        Arg:
            contract (:obj:List of contract)
        """
        short_stock_sources = self._solace.short_stock_sources(contracts, timeout, cb)
        return short_stock_sources

    def account_balance(
        self,
        timeout: int = 5000,
        cb: typing.Callable[[AccountBalance], None] = None,
    ) -> AccountBalance:
        """get stock account balance"""
        return self._solace.account_balance(self.stock_account, timeout=timeout, cb=cb)

    def order_deal_records(
        self,
        account: Account = None,
        timeout: int = 5000,
        cb: typing.Callable[[OrderDealRecords], None] = None,
    ) -> typing.List[OrderDealRecords]:
        """get order deal records"""
        return self._solace.order_deal_records(account, timeout, cb)

    def set_order_callback(
        self, func: typing.Callable[[OrderState, dict], None]
    ) -> None:
        self._solace.set_order_callback(func)

    def set_session_down_callback(self, func: typing.Callable[[], None]) -> None:
        self.quote.set_session_down_callback(func)

    def set_context(self, context: typing.Any):
        self.quote.set_context(context)

    def on_tick_stk_v1(
        self, bind: bool = False
    ) -> typing.Callable[[Exchange, TickSTKv1], None]:
        def wrap_deco(
            func: typing.Callable[[Exchange, TickSTKv1], None]
        ) -> typing.Callable[[Exchange, TickSTKv1], None]:
            self.quote.set_on_tick_stk_v1_callback(func, bind)
            return func

        return wrap_deco

    def on_tick_fop_v1(
        self, bind: bool = False
    ) -> typing.Callable[[Exchange, TickFOPv1], None]:
        def wrap_deco(
            func: typing.Callable[[Exchange, TickFOPv1], None]
        ) -> typing.Callable[[Exchange, TickFOPv1], None]:
            self.quote.set_on_tick_fop_v1_callback(func, bind)
            return func

        return wrap_deco

    def on_bidask_stk_v1(
        self, bind: bool = False
    ) -> typing.Callable[[Exchange, BidAskSTKv1], None]:
        def wrap_deco(
            func: typing.Callable[[Exchange, BidAskSTKv1], None]
        ) -> typing.Callable[[Exchange, BidAskSTKv1], None]:
            self.quote.set_on_bidask_stk_v1_callback(func, bind)
            return func

        return wrap_deco

    def on_bidask_fop_v1(
        self, bind: bool = False
    ) -> typing.Callable[[Exchange, BidAskFOPv1], None]:
        def wrap_deco(
            func: typing.Callable[[Exchange, BidAskFOPv1], None]
        ) -> typing.Callable[[Exchange, BidAskFOPv1], None]:
            self.quote.set_on_bidask_fop_v1_callback(func, bind)
            return func

        return wrap_deco

    def on_quote_stk_v1(
        self, bind: bool = False
    ) -> typing.Callable[[Exchange, QuoteSTKv1], None]:
        def wrap_deco(
            func: typing.Callable[[Exchange, QuoteSTKv1], None]
        ) -> typing.Callable[[Exchange, QuoteSTKv1], None]:
            self.quote.set_on_quote_stk_v1_callback(func, bind)
            return func

        return wrap_deco

    def on_quote(
        self, func: typing.Callable[[str, dict], None]
    ) -> typing.Callable[[str, dict], None]:
        self.quote.set_quote_callback(func)
        return func

    def on_event(
        self, func: typing.Callable[[int, int, str, str], None]
    ) -> typing.Callable[[int, int, str, str], None]:
        self.quote.set_event_callback(func)
        return func

    def on_session_down(
        self, func: typing.Callable[[], None]
    ) -> typing.Callable[[], None]:
        self.quote.set_session_down_callback(func)
        return func
