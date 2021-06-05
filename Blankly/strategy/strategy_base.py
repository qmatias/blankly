"""
    Abstraction for creating event driven user strategies
    Copyright (C) 2021  Emerson Dove, Brandon Fan

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Lesser General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import warnings
import tempfile
import json
import os

from Blankly.strategy.strategy_state import StrategyState
from Blankly.utils.utils import AttributeDict
import typing
import time

import pandas as pd
import numpy as np
import datetime
import Blankly
from Blankly.exchanges.Paper_Trade.backtest_controller import BackTestController
from uuid import uuid4
from Blankly.exchanges.exchange import Exchange
from Blankly.strategy.order import Order
from Blankly.utils.time_builder import time_interval_to_seconds


class Strategy:
    def __init__(self, exchange: Exchange, currency_pair='BTC-USD'):
        self.__exchange = exchange
        self.Ticker_Manager = Blankly.TickerManager(self.__exchange.get_type(), currency_pair)
        self.Orderbook_Manager = Blankly.OrderbookManager(self.__exchange.get_type(), currency_pair)

        self.__scheduling_pair = []  # Object to hold a currency and the resolution its pulled at: ["BTC-USD", 60]
        self.Interface = exchange.get_interface()

        # Create a cache for the current interface, and a wrapped paper trade object for user backtesting
        self.__interface_cache = self.Interface
        self.__paper_trade_exchange = Blankly.PaperTrade(self.__exchange)
        self.__schedulers = []
        self.__variables = {}

    def add_price_event(self, callback: typing.Callable, currency_pair: str, resolution: str):
        """
        Add Orderbook Event
        Args:
            callback: The price event callback that will be added to the current ticker and run at the proper resolution
            currency_pair: Currency pair to create the price event for
            resolution: The resolution that the callback will be run - in seconds
        """
        resolution = time_interval_to_seconds(resolution)
        
        self.__scheduling_pair.append([currency_pair, resolution])
        callback_id = str(uuid4())
        self.__variables[callback_id] = AttributeDict({})
        if resolution < 10:
            # since it's less than 10 sec, we will just use the websocket feed - exchanges don't like fast calls
            self.Ticker_Manager.create_ticker(self.__idle_event, currency_id=currency_pair)
            self.__schedulers.append(
                Blankly.Scheduler(self.__price_event_websocket, resolution,
                                  initially_stopped=True,
                                  callback=callback,
                                  resolution=resolution,
                                  variables=self.__variables[callback_id],
                                  currency_pair=currency_pair)
            )
        else:
            # Use the API
            self.__schedulers.append(
                Blankly.Scheduler(self.__price_event_rest, resolution,
                                  initially_stopped=True,
                                  callback=callback,
                                  variables=self.__variables[callback_id],
                                  currency_pair=currency_pair)
            )

    def __idle_event(self):
        """
        Function to skip & ignore callbacks
        """
        pass

    # def __process_orders(self, orders: typing.Any, currency_pair: str):
    #     if orders is None:
    #         return
    #     is_list_of_orders = isinstance(orders, list) and not isinstance(orders[0], Order)
    #     is_np_array_of_orders = isinstance(orders, np.array) and not isinstance(orders[0], Order)
    #
    #     if is_list_of_orders or is_np_array_of_orders or not isinstance(orders, Order):
    #         raise ValueError("Expected an Order or a list of Orders but instead " + type(orders[0]))
    #
    #     if isinstance(orders, list) or isinstance(orders, np.array):
    #         for order in orders:
    #             self.__submit_order(order, currency_pair)
    #     else:
    #         self.__submit_order(orders, currency_pair)
    #
    # def __submit_order(self, order: Order, currency_pair: str):
    #     if order.type == 'market':
    #         self.Interface.market_order(currency_pair, order.side, order.amount)
    #     if order.type == 'limit':
    #         self.Interface.limit_order(currency_pair, order.side, order.price, order.amount)
        
    def __price_event_rest(self, **kwargs):
        callback = kwargs['callback']
        currency_pair = kwargs['currency_pair']
        resolution = kwargs['resolution']
        variables = kwargs['variables']
        price = self.Interface.get_price(currency_pair)

        state = StrategyState(self, self.Interface, variables, resolution)
        orders = callback(price, currency_pair, self.Interface, state)
        # self.__process_orders(orders, currency_pair)

    def __price_event_websocket(self, **kwargs):
        callback = kwargs['callback']
        currency_pair = kwargs['currency_pair']
        resolution = kwargs['resolution']
        variables = kwargs['variables']

        price = self.Ticker_Manager.get_most_recent_tick(override_currency=currency_pair)
        state = state = StrategyState(self, self.Interface, variables, resolution)
        orders = callback(price, currency_pair, self.Interface, state)
        # self.__process_orders(orders, currency_pair)

    def add_orderbook_event(self, callback: typing.Callable, currency_pair: str):
        """
        Add Orderbook Event
        Args:
            callback: The price event callback that will be added to the current ticker and run at the proper resolution
            currency_pair: Currency pair to create the orderbook for
        """
        # since it's less than 10 sec, we will just use the websocket feed - exchanges don't like fast calls
        self.Orderbook_Manager.create_orderbook(self.__idle_event, currency_id=currency_pair)

        # TODO the tickers need some type of argument passing & saving like scheduler so that the 1 second min isn't
        #  required
        callback_id = str(uuid4())
        self.__variables[callback_id] = {}
        self.__schedulers.append(
            Blankly.Scheduler(self.__orderbook_event_websocket, 1,
                              initially_stopped=True,
                              variables = self.__variables[callback_id],
                              callback=callback, currency_pair=currency_pair)
        )

    def start(self):
        for i in self.__schedulers:
            i.start()

    def __orderbook_event_websocket(self, **kwargs):
        callback = kwargs['callback']
        currency_pair = kwargs['currency_pair']
        variables = kwargs['variables']

        price = self.Orderbook_Manager.get_most_recent_tick(override_currency=currency_pair)
        state = StrategyState(self, self.Interface, variables)
        orders = callback(price, currency_pair, self.Interface, state)
        # is_arr_type = isinstance(orders, list) or isinstance(orders, np.array)
        # if is_arr_type and isinstance(orders[0], Order):
        #     raise ValueError("It is best that you directly use the interface for orderbook event orders")
    
    def backtest(self, 
                 initial_value: int or float = None,
                 initial_values: dict = None,
                 to: str = None,
                 start_date: str = None,
                 end_date: str = None,
                 save: bool = False,
                 use_price: str = 'close',
                 smooth_prices: bool = False,
                 GUI_output: bool = False,
                 show_tickers_with_zero_delta: bool = False,
                 save_initial_account_value: bool = True,
                 show_progress_during_backtest: bool = True,
                 ):
        """
        Turn this strategy into a backtest.

        Args:
            ** We expect either an initial_value (in USD) or a dictionary of initial values, we also expect
            either `to` to be set or `start_date` and `end_date` **

            initial_value (int or float): The initial value (in USD) that the portfolio will use to backtest
            initial_values (dict): Dictionary of initial value sizes (i.e { 'BTC': 3, 'USD': 5650})
            to (str): Declare an amount of time before now to backtest from: ex: '5y' or '10h'
            start_date (str): Override argument "to" by specifying a start date such as "03/06/2018"
            end_date (str): End the backtest at a date such as "03/06/2018"
            save (bool): Save the price data that is required for the backtest
        """
        fd, path = tempfile.mkstemp()
        start = None
        end = None
        backtest_dict = {
            "price_data": {
                "assets": []
            },
            "settings": {
                "use_price": use_price,
                "smooth_prices": smooth_prices,
                "GUI_output": GUI_output,
                "show_tickers_with_zero_delta": show_tickers_with_zero_delta,
                "save_initial_account_value": save_initial_account_value,
                "show_progress_during_backtest": show_progress_during_backtest
            }
        }

        values = {}
        
        if initial_value is not None and initial_values is not None:
            raise ValueError("Error, please input either an initial values or dictionary of initial values, we received both")
        
        if initial_value is not None:
            values["USD"] = initial_value
        elif initial_values is not None:
            values = initial_values
        backtest_dict["initial_values"] = values

        if to is not None:
            start = time.time() - time_interval_to_seconds(to)
            end = time.time()

        if start_date is not None:
            start_date = pd.to_datetime(start_date)
            epoch = datetime.datetime.utcfromtimestamp(0)
            start = (start_date - epoch).total_seconds()

        if end_date is not None:
            end_date = pd.to_datetime(end_date)
            epoch = datetime.datetime.utcfromtimestamp(0)
            end = (end_date - epoch).total_seconds()


        for pair in self.scheduling_pair:
            ticker = pair[0]
            resolution = pair[1]
            data = [ticker, start, end, resolution]
            backtest_dict["price_data"]["assets"].append(data)

        
        backtest_dict["price_data"]["cache_location"] = "./price_caches"
        
        with os.fdopen(fd, 'w') as tmp:
            json.dump(backtest_dict, tmp)


        self.Interface = self.__paper_trade_exchange.get_interface()
        backtesting_controller = BackTestController(self.__paper_trade_exchange, backtest_settings_path=path)
        # Append each of the events the class defines into the backtest
        for i in self.__schedulers:
            kwargs = i.get_kwargs()
            backtesting_controller.append_backtest_price_event(callback=kwargs['callback'],
                                                               asset_id=kwargs['currency_pair'],
                                                               time_interval=i.get_interval()
                                                               )

        # Run the backtest & return results
        results = backtesting_controller.run()

        # Clean up
        self.Interface = self.__interface_cache
        os.remove(path) # remove tmp backtest.json
        return results
