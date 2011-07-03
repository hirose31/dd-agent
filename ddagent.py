# Standard imports
import logging
import os
import sys
from subprocess import Popen
from hashlib import md5
from datetime import datetime, timedelta

#Tornado
import tornado.httpserver
import tornado.httpclient
import tornado.ioloop
import tornado.web
from tornado.escape import json_decode
from tornado.options import define, parse_command_line, options

# agent import
from config import get_config, get_system_stats, get_parsed_args
from emitter import http_emitter, format_body
from checks.common import checks


CHECK_INTERVAL =  60 * 1000 # Every 60s
PROCESS_CHECK_INTERVAL = 1000 # Every second
TRANSACTION_FLUSH_INTERVAL = 5000 # Every 5 seconds


def plural(count):
    if count > 1:
        return "s"
    return ""

class Transaction(object):

    application = None

    _transactions = [] #List of all non commited transactions
    _counter = 0 # Global counter to assign a number to each transaction


    _trs_to_flush = None # Current transactions being flushed

    @staticmethod
    def set_application(app):
        Transaction.application = app

    def __init__(self, data):

        Transaction._counter = Transaction._counter + 1
        self._id = Transaction._counter
        
        self._data = data
        self._error_count = 0
        self._next_flush = datetime.now()

        #Append the transaction to the end of the list, we pop it later:
        # The most recent message is thus sent first.
        self._transactions.append(self)
        logging.info("Created transaction %d" % self._id)

    @staticmethod
    def flush():

        if Transaction._trs_to_flush is not None:
            logging.info("A flush is already in progress, not doing anything")
            return

        to_flush = []
        # Do we have something to do ?
        now = datetime.now()
        for tr in Transaction._transactions:
            if tr.time_to_flush(now):
                to_flush.append(tr)
           
        count = len(to_flush)
        if count > 0:
            logging.info("Flushing %s transaction%s" % (count,plural(count)))
            url = Transaction.application.agentConfig['ddUrl'] + '/intake/'
            Transaction._trs_to_flush = to_flush
            Transaction._flush_next(url)

    @staticmethod
    def _flush_next(url):

        if len(Transaction._trs_to_flush) > 0:
            tr = Transaction._trs_to_flush.pop()
            # Send Transaction to the intake
            req = tornado.httpclient.HTTPRequest(url, 
                         method = "POST", body = tr.get_data() )
            http = tornado.httpclient.AsyncHTTPClient()
            logging.info("Sending transaction %d to datadog" % tr._id)
            http.fetch(req, callback=lambda(x): Transaction.on_response(tr, url, x))
        else:
            Transaction._trs_to_flush = None

    @staticmethod
    def on_response(tr, url, response):
        if response.error: 
            tr.error()
        else:
            tr.finish()

        Transaction._flush_next(url)

    def time_to_flush(self,now = datetime.now()):
        return self._next_flush < now

    def get_data(self):
        try:
            return format_body(self._data, logging)
        except Exception, e:
            import traceback
            logger.error('http_emitter: Exception = ' + traceback.format_exc())

    def compute_next_flush(self):

        # Transactions are replayed, try to send them faster for newer transactions
        # Send them every minutes at most
        td = timedelta(seconds=self._error_count * 20)
        if td > timedelta(seconds=60):
            td = timedelta(seconds = 60)

        newdate = datetime.now() + td
        self._next_flush = newdate.replace(microsecond=0)

    def error(self):
        self._error_count = self._error_count + 1
        self.compute_next_flush()
        logging.info("Transaction %d in error (%s error%s), it will be replayed after %s" % 
          (self._id, self._error_count, plural(self._error_count), self._next_flush))

    def finish(self):
        logging.info("Transaction %d completed" % self._id)
        Transaction._transactions.remove(self)

class AgentInputHandler(tornado.web.RequestHandler):

    HASH = "hash"
    PAYLOAD = "payload"

    @staticmethod
    def parse_message(message, msg_hash):

        c_hash = md5(message).hexdigest()
        if c_hash != msg_hash:
            logging.error("Malformed message: %s != %s" % (c_hash, msg_hash))
            return None

        return json_decode(message)


    def post(self):
        """Read the message and forward it to the intake"""

        # read message
        msg = AgentInputHandler.parse_message(self.get_argument(self.PAYLOAD),
            self.get_argument(self.HASH))

        if msg is not None:
            # Setup a transaction for this message
            Transaction(msg)
            Transaction.flush()
        else:
            raise tornado.web.HTTPError(500)
   
class Application(tornado.web.Application):

    def __init__(self, options, agentConfig):

        handlers = [
            (r"/intake/?", AgentInputHandler),
        ]

        settings = dict(
            cookie_secret="12oETzKXQAGaYdkL5gEmGeJJFuYh7EQnp2XdTP1o/Vo=",
            xsrf_cookies=False,
            debug=True,
        )

        self._check_pid = -1
        self.agentConfig = agentConfig

        tornado.web.Application.__init__(self, handlers, **settings)

        http_server = tornado.httpserver.HTTPServer(self)
        http_server.listen(options.port)
        self.run_checks(True)

    def run_checks(self, firstRun = False):

        if self._check_pid > 0 :
            logging.warning("Not running checks because a previous instance is still running")
            return False

        args = [sys.executable]
        args.append(__file__)
        args.append("--action=runchecks")

        if firstRun:
            args.append("--firstRun=yes")

        logging.info("Running local checks")
        logging.info("  args = %s" % str(args))

        try:
            p = Popen(args)
            self._check_pid = p.pid
        except Exception, e:
            logging.exception(e)
            return False
  
        return True

    def process_check(self):

        if self._check_pid > 0:
            logging.debug("Checking on child process")
            # Try to join the process running checks
            (pid, status) = os.waitpid(self._check_pid,os.WNOHANG)
            if (pid, status) != (0, 0):
                logging.debug("child (pid: %s) exited with status: %s" % (pid, status))
                if status != 0:
                    logging.error("Error while running checks")
                self._check_pid = -1
            else:
                logging.debug("child (pid: %s) still running" % self._check_pid)

def main():

    define("action", type=str, default="start", help="Action to run")
    define("firstRun", type=bool, default=False, help="First check run ?")
    define("port", type=int, default=17123, help="Port to listen on")
    define("log", type=str, default="ddagent.log", help="Log file to use")

    args = parse_command_line()

    # Remove know options so it won't get parsed (and fails because
    # get_config don't know about our option and python OptParser breaks on
    # unkown options)
    newargs = []
    knownoptions = [ "--" + o for o in options.keys()]
    for arg in sys.argv:
        known = False
        for opt in knownoptions:
            if arg.startswith(opt):
                known = True
                break

        if not known:
            newargs.append(arg)

    sys.argv = newargs

    # set up logging
    formatter = logging.Formatter(fmt="%(asctime)s %(levelname)s %(filename)s:%(lineno)d %(message)s")
    handler = logging.FileHandler(filename=options.log)
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    #logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger().setLevel(logging.INFO)
    
    agentConfig, rawConfig = get_config()

    if options.action == "start":
        logging.info("Starting ddagent tornado")

        # Set up tornado
        app = Application(options,agentConfig)
        Transaction.set_application(app)

        # Register callbacks
        mloop = tornado.ioloop.IOLoop.instance() 

        def run_checks():
            logging.info("Running checks...")
            app.run_checks()
    
        def process_check():
            app.process_check()

        def flush_trs():
            Transaction.flush()

        check_scheduler = tornado.ioloop.PeriodicCallback(run_checks,CHECK_INTERVAL, io_loop = mloop) 
        check_scheduler.start()

        p_checks_scheduler = tornado.ioloop.PeriodicCallback(process_check,PROCESS_CHECK_INTERVAL, io_loop = mloop) 
        p_checks_scheduler.start()

        tr_sched = tornado.ioloop.PeriodicCallback(flush_trs,TRANSACTION_FLUSH_INTERVAL, io_loop = mloop)
        tr_sched.start()

        # Start me up!
        mloop.start()

    elif options.action == "runchecks":

        #Create checks instance
        agentLogger = logging.getLogger('agent')

        systemStats = False
        if options.firstRun:
            agentLogger.debug('Collecting basic system stats')
            systemStats = get_system_stats()
            agentLogger.debug('System: ' + str(systemStats))
            
        agentLogger.debug('Creating checks instance')

        emitter = http_emitter
       
        mConfig = dict(agentConfig)
        mConfig['ddUrl'] = "http://localhost:" + str(options.port)
        _checks = checks(mConfig, rawConfig, emitter)
        _checks._doChecks(options.firstRun,systemStats)

if __name__ == "__main__":
    main()

