
################# REGULAR PYTHON IMPORTS ##################
import pickle
import os


################# LIBRARY IMPORTS ##################

import zmq
import pandas as pd
import influxdb as db
import numpy as np

################# Object IMPORTS ##################

from tradex.market.parse_index import parse_hst
from influxdb.exceptions import InfluxDBClientError
from zmq.error import ContextTerminated

from tradex.config import MT4_PATH, MARKET_PUSH_PORTS, INTERMED_PORT

time_range = {
    "M1": list(range(1, 60, 1)),
    "M5": list(range(5, 60, 5)),
    "M15": list(range(15, 60, 15)),
    "H1": list(range(1, 24, 1)),
    "H4": list(range(4, 24, 4)),
    "D1": [0],
}


class MarketPair:

    ##############  NOTE: DO NOT! INHERIT FROM BASE CLASS DIRECTLY.... ########

    def __init__(self, pair, string_delimiter=';', port=None):
        """
        This is class is base class of Market Pairs

        [INIT VALUES]

        1. pair :== This is the name of market pair and it
        is passed as string to init function

        2. port :== This is the port that market pair runs on and
        should be unique to the specific market pair
        so as not to have more than one pair running in the system...

        """

        self.M1 = None
        self.M5 = None
        self.M15 = None
        self.H1 = None
        self.H4 = None
        self.D1 = None

        self._push_port = MARKET_PUSH_PORTS[pair]
        self.string_delimiter = string_delimiter

        self.context = zmq.Context()
        # Create the push and pull sockets
        self.push = self.context.socket(zmq.PUSH)
        self.pull_socket = self.context.socket(zmq.PULL)
        # self.req_socket = self.context.socket(zmq.REQ)
        # self.req_socket.setidentity(pair)
        # self.req_socket.connect("tcp://localhost")
        
        # Bind on pull, connect on push
        self.push.bind(f'tcp://*:{self._push_port}')
        self.pull_socket.connect(f'tcp://localhost:{self._push_port}')

        self._port = port if isinstance(port, int) else INTERMED_PORT
        self.pair_name = pair

        self.df_tick = pd.DataFrame(
            {}, index=pd.to_datetime([]), columns=['Bid', 'Ask']
        )

    """

    #####################################################################

            The start function defined by start();
            It connects MARKETCLIENT with the METATRADER 4 connnector;
            It parses data received and sends it on a push socket to ==
            thread based function that analyses data received and
            sends to strategy manager for signal...

    #####################################################################


    """

    @staticmethod
    def fill_missing_indexes_values(frame):
        date_range = pd.date_range(frame.index[0], frame.index[-1], freq='1T')
        for date in date_range:
            if date not in frame.index:
                ser = pd.Series(
                    np.array([np.NAN, np.NAN, np.NAN, np.NAN]),
                    index=['open', 'high', 'low', 'close'],
                    name=date, dtype=np.float64)
                frame = frame.append(ser)
        return frame.sort_index(axis=0)

    @staticmethod
    def resample_frame(time_interval, frame):
        """
                Returns DataFrame containing resampled data

                [NOTE] : This method depends solely on the ["self.M1"]
                attribute to be a DataFrame containing correct
                time values for it to work
                Anything short of that and it does not work,
                so the self.M1 must be a DataFrame
                with uniform values for it to work....
        """
        index = ['open', 'high', 'low', 'close']

        def kl(x, *args):

            nonlocal index
            # Check for missing/NAN values in resampled data
            # and return NAN series if any
            if x['open'].isna().any():  # Keep track of this potential bug
                return pd.Series(
                    np.array([np.NAN, np.NAN, np.NAN, np.NAN]),
                    index=index)

            init = x['open'][0]
            last = x['close'][-1]
            new_max = x[['high', 'low']].max().max()
            new_min = x[['high', 'low']].min().min()

            if (last - init) >= 0:
                high = new_max
                low = new_min
                return pd.Series([init, high, low, last], index=index)
            elif (last - init) < 0:
                high = new_min
                low = new_max
                return pd.Series([init, high, low, last], index=index)

            raise(Exception('Na Mad Error wey dey here so....'))

        return frame.resample(time_interval).apply(kl)

    def start(self):
        _context = zmq.Context()
        _subscribe = _context.socket(zmq.SUB)
        _subscribe.connect(f'tcp://localhost:{self._port}')

        _subscribe.setsockopt_string(zmq.SUBSCRIBE, self.pair_name)

        print('\n', '\t\t ##### receiving data and sending #####')

        while True:
            try:
                msg = _subscribe.recv_string(zmq.DONTWAIT)
                if msg != '':
                    print(msg.split(" "))
                    _symbol, _data = msg.split(" ")
                    if _data == 'kill':
                        print('Received killing code... Killing now')
                        self.push.send(b'kill')
                        self.push.close()
                        _subscribe.close()
                        _context.term()
                        break
                    _bid, _ask, _timestamp = _data.split(self.string_delimiter)

                    self.df_tick = self.df_tick.append(
                        pd.DataFrame({
                            'Bid': [float(_bid)], 'Ask': [float(_ask)]
                        },
                            index=pd.to_datetime(
                                [pd.Timestamp.fromtimestamp(
                                    float(_timestamp))])
                        ))

                    print('Received message .... ', msg)

                    last_last_val = self.df_tick.index[-2]
                    last_val = self.df_tick.index[-1]

                    if last_val.minute - last_last_val.minute == 1:
                        range_1 = str(
                            last_last_val.replace(
                                second=0, microsecond=0))

                        range_2 = str(last_last_val)

                        dump_data = pickle.dumps(
                            self.df_tick.loc[range_1:range_2]['Bid'].resample(
                                '1Min').ohlc()
                        )

                        self.push.send(dump_data)

            except zmq.error.Again:
                pass
            except ValueError:
                pass
            except IndexError:
                continue
            except KeyboardInterrupt:
                _subscribe.close()
                _context.term()


class MarketParser(MarketPair):

    """ This class inherits from Market pair and adds extra functionality


    It initially queries INFLUXDB database for all data in minute candle,

    then adds this initially to 1min dataframe, before comparing today's time

    with influxdb time, getting loophole and querying out loophole values

    from hst archive, then creating another dataframe minute candle

    and appending to initial minute candlestick before the main process
    starts...

    Note that if InfluxDB database is empty, or not created, then it
    is automatically created and filled with a large dataset
    of hst archive values
    for 1min candle
    so we dont ave to query hst archive again when we want
    to get old tick values but loop holes instead to fill in the gaps

    This class implements the process of resampling data
    to different timeframes and appending each data value
    to timeframe variables

    Note initial dataframes have None as value, this changes over time..

    """

    def __init__(self, pair):

        super().__init__(pair=pair)

        self.pair_hst_file = self.pair_name + '1.hst'

        """
            [ Initialize database connection and Query Database for values ]
        """

        self.client = db.DataFrameClient(
            host='localhost', port=8086, database=pair)

        """ Query DataBase for values,
        if No Database get raw or remote data"""

        self.M1 = self.initials()[-10000:]
        self.M1 = MarketParser.fill_missing_indexes_values(self.M1)
        self.M5 = MarketParser.resample_frame('5T', self.M1)
        self.M15 = MarketParser.resample_frame('15T', self.M1)
        self.H1 = MarketParser.resample_frame('1H', self.M1)
        self.H4 = MarketParser.resample_frame('4H', self.M1)
        self.D1 = MarketParser.resample_frame('1D', self.M1)

        print('\nDone, exiting [INIT] block')

    """

    #####################################################################

            Fetches data from hst archive

    #####################################################################


    """

    def fetch_history_parse(self, year_val=None, range_1=None, range_2=None):
        """
                This method returns a dataframe containing parsed data from
                the imported parse_hst function and raises a FileNotFoundError
                if the path to the file does not exist....
        """
        _path = os.path.join(MT4_PATH, self.pair_hst_file)
        if os.path.exists(_path):
            return parse_hst(_path, year_val, range1=range_1, range2=range_2)

        raise FileNotFoundError(
            '''The file was not found on this device, try changing paths...'''
        )

    """

    #####################################################################

            The initials function defined by initials();
            It is the first function run in this class and it's work is
            to fill up the M1(1 minute dataframe) with recent data
            from either database or hst archive...

    #####################################################################


    """

    def initials(self):

        f"""
        This is the initial method that is been run in the __init__ method of
        the MarketParser class, and is responsible for fetching initial data
        from either InfluxDB or parse_hst

        [One of two conditions in try block has to be fulfilled]
        1. If there is a database for the market pair, then we just
                a. fetch data from database
                b. create range that spans from last value in
                database to present time
                c. Use this range to fetch missing data from
                [parse_hst] function
                d. Append missing data to original data in
                database and save to database
                e. Append missing data to initial dataframe
                then return it to the init method attribute

        2. If the database does not exist, then we
                a. just call [parse_hst] function and fetch data from it
                b. Create a new database with the market pair name
                c. Write the dataframe into this database
                d. return the fetched data
        """

        try:
            _M1 = self.client.query(
                f'select * from {self.pair_name}'
            )[f'{self.pair_name}']

            _now = pd.Timestamp.now('UTC')

            _range_1 = _M1.index[-1].timestamp()
            _range_2 = (
                _now.replace(second=0, microsecond=0) - pd.Timedelta(minutes=1)
            ).timestamp()

            _loop_frame = self.fetch_history_parse(_range_1, _range_2)

            _M1 = _M1.append(_loop_frame).sort_index(axis=0)
            _M1.columns = ['open', 'high', 'low', 'close']

            return _M1

        except InfluxDBClientError:
            # This error occurs because a database was not found...
            # In case of any other influx error check code well
            print(
                'Database error, >>>>> \
                Creating new database \nFetching Data from HST \
                Archive as replcacement')
            # raise e('Database was not found fot this market pair')

            _M1 = self.fetch_history_parse(year_val=2018)
            print('Gotten values')
            self.client.create_database(f'{self.pair_name}')
            self.client.write_points(_M1, f'{self.pair_name}', protocol='json')

            print('Done and dusted')
            print(_M1[:10])
            return _M1

        except Exception:
            self.shutdown_sockets()
            raise Exception

    def validate_tick_crossing(self):
        """

        This method is called when the tick value in self.df_tick crosses the
        minute mark....
        It checks for all frames except M1 if their time mark has been passed,
        creates a data range and then resamples the
        1min data based on the range
        to that frame's time,and appends returned
        data to original

        """

        pivot_time = self.M1.index[-1]

        for val in ['M5', 'M15']:
            if pivot_time.minute in time_range[val]:
                pass

        for valx in ['H1', 'H4']:
            if pivot_time.hour in time_range[valx] and pivot_time.minute == 0:
                pass

        for valx in ['D1']:
            if pivot_time.hour == 0 and pivot_time.minute == 0:
                pass
        pass

    """

    #####################################################################

            This is the main logic and would be responsible for receiving
            data and passing to strategy manager class

    #####################################################################


    """

    def main_logic(self):
        """
        The main logic method is the backbone of the whole code
        and is what actually sends
        our code to the strategy class for signal creation....
        It also has a kill option that kills the socket
        and everything in it, terminating the process
        """

        print('\n', '\t\t ##### Polling for data on pull socket #####')
        while True:
            try:
                msg = self.pull_socket.recv()
                if msg != '':
                    if msg == b'kill':
                        print('killing logic thread... Killing now')
                        self.pull_socket.close()
                        self.context.term()
                        raise ContextTerminated
                    frame = pickle.loads(msg)
                    self.M1 = self.M1.append(frame)
                    print(self.M1.iloc[-1])
                    # self.validate_tick_crossing()

            except zmq.error.Again:
                pass
            except ValueError:
                pass
            except KeyboardInterrupt:
                self.pull_socket.close()
                self.context.term()
            except ContextTerminated:
                break

    def shutdown_sockets(self):
        self.pull_socket.close()
        self.push.close()
        self.context.term()


# if __name__ == '__main__':
#     af = DWX_ZeroMQ_Connector()
#     ad = MarketParser('EURUSD')

#     af._DWX_MTX_SUBSCRIBE_MARKETDATA_('EURUSD')

#     thread1 = Thread(target=ad.start)
#     thread1.start()
#     thread2 = Thread(target=ad.main_logic)
#     thread2.start()
