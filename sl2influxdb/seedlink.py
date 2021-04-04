#!/usr/bin/env python3

from datetime import datetime
from io import StringIO
import logging
import queue
import re
import sys

from lxml import etree
from obspy import UTCDateTime
from obspy.clients.seedlink import EasySeedLinkClient
from obspy.clients.seedlink.seedlinkexception import SeedLinkException

from sl2influxdb.threads import q, shutdown_event

# default logger
logger = logging.getLogger('obspy.seedlink')


class MySeedlinkClient(EasySeedLinkClient):
    def __init__(self, server, streams, statefile, recover):
        EasySeedLinkClient.__init__(self, server)
        self.selected_streams = []
        # get from server available streams
        self.stream_xml = self.get_info('STREAMS')
        logger.info("stream_xml " + self.stream_xml)
        # streams wanted by user
        for patterns in streams:
            if self.select_stream_re(patterns) is False:
                logger.critical("Error parsing %s. Ignoring ..." % patterns)

        self.statefile = statefile
        self.recover = recover

        # write in "trace queue"  timeout
        self.queue_timeout = 15  # sec

        # Ignore trace older than SL_TIME_MAX sec.
        self.SL_PACKET_TIME_MAX = 60. * 30.

        # resample signal if not None
        self.resample_rate = 10.  # Hz

        # self.conn.DFT_READBUF_SIZE = 10240

        # Time in seconds after which a `collect()` call will be interrupted.
        self.conn.timeout = 360

        # Network timeout (seconds) (default is 120 sec)
        self.conn.netto = 360

        # Network reconnect delay (seconds)  (default is 30 sec)
        # self.conn.netdly = 30

        self.show_too_old_packet_msg = {}

        if self.recover:
            self.conn.statefile = statefile
            # recover last packet indexes from previous run
            try:
                self.conn.recover_state(statefile)
            except SeedLinkException as e:
                logger.error(e)
            # prevent statefile to be overwitten each time a new packet
            # arrives (take too much ressources)
            self.conn.statefile = None

    def get_stream_info(self):
        """Parse xml stream info returned by server."""
        stream_info = []
        self.stream_xml = self.stream_xml.replace('encoding="utf-8"', '')
        parser = etree.XMLParser(remove_blank_text=True)
        tree = etree.parse(StringIO(self.stream_xml), parser)
        root = tree.getroot()
        for s in root.iterchildren():
            if s.tag == "station":
                s_dic = dict(zip(s.keys(), s.values()))
                s_dic['channel'] = []
                stream_info.append(s_dic)
                for c in s.iterchildren():
                    if c.tag == "stream":
                        c_dic = dict(zip(c.keys(), c.values()))
                        s_dic['channel'].append(c_dic)
        return stream_info

    def show_stream_info(self):
        """Show xml parsed stream info returned by server."""
        info = self.get_stream_info()
        for s in info:
            for c in s['channel']:
                print(s['network'], s['name'])
                print(c['location'], c['seedname'], s['stream_check'])

    def select_stream_re(self, pattern):
        """Select stream based on regular expression."""
        try:
            net_re = re.compile(pattern[0])
            sta_re = re.compile(pattern[1])
            chan_re = re.compile(pattern[2])
            loc_re = re.compile(pattern[3])
        except Exception:
            return False

        info = self.get_stream_info()  # available streams from server
        for s in info:
            if not net_re.match(s['network']) or not sta_re.match(s['name']):
                continue
            for c in s['channel']:
                if chan_re.match(c['seedname']) \
                   and loc_re.match(c['location']):
                    self.add_stream(s['network'], s['name'],
                                    c['seedname'], c['location'])
        return True

    def add_stream(self, net, sta, chan, loc):
        """Add stream to be proceseed

        It is not possible to add location code here
        """
        self.select_stream(net, sta, chan)
        stream = ".".join([net, sta, loc, chan])
        self.selected_streams.append(stream)
        logger.info("stream %s added" % stream)

    def on_data(self, trace):
        """Implement the on_data callback."""
        channel = trace.get_id()
        if channel not in self.selected_streams:
            return

        now = datetime.utcnow()
        endtime = trace.stats['endtime']
        sample_rate = trace.stats['sampling_rate']

        # filter out too old data
        latency = UTCDateTime(now) - endtime
        if self.SL_PACKET_TIME_MAX and latency > self.SL_PACKET_TIME_MAX:
            if logger.getEffectiveLevel() <= logging.INFO:
                # Show when a channel becomes unavailable
                # ie. latency > threshold
                if channel not in self.show_too_old_packet_msg or \
                   self.show_too_old_packet_msg[channel] is True:
                    msg = "[%s] latency too high (%.1f s), " % \
                           (channel, latency) + \
                           "trace disabled until latency < %.1f s" % \
                           (self.SL_PACKET_TIME_MAX)
                    logger.info(msg)
                    self.show_too_old_packet_msg[channel] = False

            if shutdown_event.isSet():
                logger.info("%s thread has catched *shutdown_event*" %
                            self.__class__.__name__)
                self.stop_seedlink()
            return

        if logger.getEffectiveLevel() <= logging.INFO:
            # Show when a channel becomes available ie. latency < threshold
            if channel not in self.show_too_old_packet_msg or \
               self.show_too_old_packet_msg[channel] is False:
                self.show_too_old_packet_msg[channel] = True
                msg = "[%s] latency is %.1f  < %.1f s, " % \
                      (channel, latency, self.SL_PACKET_TIME_MAX) + \
                      "trace enabled"
                logger.info(msg)

        # resample data
        if self.resample_rate:
            try:
                trace.resample(self.resample_rate)
            except Exception:
                msg = "Can't resample %s(%.2f Hz) to %.2f Hz" % \
                    (channel, sample_rate, self.resample_rate)
                logger.warning(msg)

        try:
            q.put(trace, block=True, timeout=self.queue_timeout)
        except queue.Full:
            logger.error("Queue is full and timeout(%ds) reached !" %
                         self.queue_timeout)
            logger.error("Ignoring data !")

        if shutdown_event.isSet():
            logger.info("%s thread has catched *shutdown_event*" %
                        self.__class__.__name__)
            self.stop_seedlink()

    def on_seedlink_error(self):
        logger.error("[%s] seedlink error." % datetime.utcnow())

    def stop_seedlink(self):
        # force packets indexes to be written on statefile
        self.conn.statefile = self.statefile
        try:
            self.conn.save_state(self.statefile)
        except SeedLinkException as e:
            logger.error(e)
        self.conn.close()
        sys.exit(0)
