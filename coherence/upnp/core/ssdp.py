# Licensed under the MIT license
# http://opensource.org/licenses/mit-license.php

# Copyright 2005, Tim Potter <tpot@samba.org>
# Copyright 2006 John-Mark Gurney <gurney_j@resnet.uroegon.edu>
# Copyright (C) 2006 Fluendo, S.A. (www.fluendo.com).
# Copyright 2006,2007,2008,2009 Frank Scholz <coherence@beebits.net>
#
# Implementation of a SSDP server under Twisted Python.
#

import random
import string
import time
import socket

from twisted.internet.protocol import DatagramProtocol
from twisted.internet import reactor, error
from twisted.internet import task
from twisted.web.http import datetimeToString
from coherence import log, SERVER_ID
import coherence.extern.louie as louie


SSDP_PORT = 1900
SSDP_ADDR = '239.255.255.250'


class SSDPServer(DatagramProtocol, log.Loggable):
    """A class implementing a SSDP server.  The notifyReceived and
    searchReceived methods are called when the appropriate type of
    datagram is received by the server."""
    logCategory = 'ssdp'

    def __init__(self, test=False, interface=''):
        # Create SSDP server
        log.Loggable.__init__(self)
        self.known = {}
        self._callbacks = {}
        self.test = test
        if not self.test:
          self.port = reactor.listenMulticast(SSDP_PORT, self, listenMultiple=True, interface=interface)

          self.port.joinGroup(SSDP_ADDR, interface=interface)

          self.resend_notify_loop = task.LoopingCall(self.resendNotify)
          self.resend_notify_loop.start(777.0, now=False)

          self.check_valid_loop = task.LoopingCall(self.check_valid)
          self.check_valid_loop.start(333.0, now=False)

        self.active_calls = []

    def shutdown(self):
        for call in reactor.getDelayedCalls():
            if call.func == self.send_it:
                call.cancel()
        if not self.test:
            if self.resend_notify_loop.running:
                self.resend_notify_loop.stop()
            if self.check_valid_loop.running:
                self.check_valid_loop.stop()
            '''Make sure we send out the byebye notifications.'''
            for st in self.known:
                if self.known[st]['MANIFESTATION'] == 'local':
                    self.doByebye(st)

    def datagramReceived(self, data, xxx_todo_changeme):
        """Handle a received multicast datagram."""
        (host, port) = xxx_todo_changeme
        try:
            header, payload = data.split('\r\n\r\n')[:2]
        except ValueError as err:
            print(err)
            print('Arggg,', data)
            import pdb; pdb.set_trace()

        lines = header.split('\r\n')
        cmd = string.split(lines[0], ' ')
        lines = [x.replace(': ', ':', 1) for x in lines[1:]]
        lines = [x for x in lines if len(x) > 0]

        headers = [string.split(x, ':', 1) for x in lines]
        headers = dict([(x[0].lower(), x[1]) for x in headers])

        self.msg('SSDP command %s %s - from %s:%d', cmd[0], cmd[1], host, port)
        self.debug('with headers: %s', headers)
        if cmd[0] == 'M-SEARCH' and cmd[1] == '*':
            # SSDP discovery
            self.discoveryRequest(headers, (host, port))
        elif cmd[0] == 'NOTIFY' and cmd[1] == '*':
            # SSDP presence
            self.notifyReceived(headers, (host, port))
        else:
            self.warning('Unknown SSDP command %s %s', cmd[0], cmd[1])

        # make raw data available
        # send out the signal after we had a chance to register the device
        louie.send('UPnP.SSDP.datagram_received', None, data, host, port)

    def register(self, manifestation, usn, st, location,
                        server=SERVER_ID,
                        cache_control='max-age=1800',
                        silent=False,
                        host=None):
        """Register a service or device that this SSDP server will
        respond to."""

        self.info('Registering %s (%s)', st, location)

        self.known[usn] = {}
        self.known[usn]['USN'] = usn
        self.known[usn]['LOCATION'] = location
        self.known[usn]['ST'] = st
        self.known[usn]['EXT'] = ''
        self.known[usn]['SERVER'] = server
        self.known[usn]['CACHE-CONTROL'] = cache_control

        self.known[usn]['MANIFESTATION'] = manifestation
        self.known[usn]['SILENT'] = silent
        self.known[usn]['HOST'] = host
        self.known[usn]['last-seen'] = time.time()

        self.msg(self.known[usn])

        if manifestation == 'local':
            self.doNotify(usn)

        if st == 'upnp:rootdevice':
            louie.send('Coherence.UPnP.SSDP.new_device', None, device_type=st, infos=self.known[usn])
            #self.callback("new_device", st, self.known[usn])

    def unRegister(self, usn):
        self.msg("Un-registering %s", usn)
        st = self.known[usn]['ST']
        if st == 'upnp:rootdevice':
            louie.send('Coherence.UPnP.SSDP.removed_device', None, device_type=st, infos=self.known[usn])
            #self.callback("removed_device", st, self.known[usn])

        del self.known[usn]

    def isKnown(self, usn):
        return usn in self.known

    def notifyReceived(self, headers, xxx_todo_changeme1):
        """Process a presence announcement.  We just remember the
        details of the SSDP service announced."""
        (host, port) = xxx_todo_changeme1
        self.info('Notification from (%s,%d) for %s', host, port, headers['nt'])
        self.debug('Notification headers: %s', headers)

        if headers['nts'] == 'ssdp:alive':
            try:
                self.known[headers['usn']]['last-seen'] = time.time()
                self.debug('updating last-seen for %r', headers['usn'])
            except KeyError:
                self.register('remote', headers['usn'], headers['nt'], headers['location'],
                              headers['server'], headers['cache-control'], host=host)
        elif headers['nts'] == 'ssdp:byebye':
            if self.isKnown(headers['usn']):
                self.unRegister(headers['usn'])
        else:
            self.warning('Unknown subtype %s for notification type %s',
                    headers['nts'], headers['nt'])
        louie.send('Coherence.UPnP.Log', None, 'SSDP', host, 'Notify %s for %s' % (headers['nts'], headers['usn']))

    def send_it(self, response, destination, delay, usn):
        self.info('send discovery response delayed by %ds for %s to %r', delay, usn, destination)
        try:
            self.transport.write(response, destination)
        except (AttributeError, socket.error) as msg:
            self.info("failure sending out byebye notification: %r", msg)

    def discoveryRequest(self, headers, xxx_todo_changeme2):
        """Process a discovery request.  The response must be sent to
        the address specified by (host, port)."""
        (host, port) = xxx_todo_changeme2
        self.info('Discovery request from (%s,%d) for %s', host, port, headers['st'])
        self.info('Discovery request for %s', headers['st'])

        louie.send('Coherence.UPnP.Log', None, 'SSDP', host, 'M-Search for %s' % headers['st'])

        # Do we know about this service?
        for i in list(self.known.values()):
            if i['MANIFESTATION'] == 'remote':
                continue
            if(headers['st'] == 'ssdp:all' and
               i['SILENT'] == True):
                continue
            if(i['ST'] == headers['st'] or
                headers['st'] == 'ssdp:all'):
                response = []
                response.append('HTTP/1.1 200 OK')

                for k, v in list(i.items()):
                    if k == 'USN':
                        usn = v
                    if k not in ('MANIFESTATION', 'SILENT', 'HOST'):
                        response.append('%s: %s' % (k, v))
                response.append('DATE: %s' % datetimeToString())

                response.extend(('', ''))
                delay = random.randint(0, int(headers['mx']))

                reactor.callLater(delay, self.send_it,
                                '\r\n'.join(response), (host, port), delay, usn)

    def doNotify(self, usn):
        """Do notification"""

        if self.known[usn]['SILENT'] == True:
            return
        self.info('Sending alive notification for %s', usn)

        resp = ['NOTIFY * HTTP/1.1',
            'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT),
            'NTS: ssdp:alive',
            ]
        stcpy = dict(iter(self.known[usn].items()))
        stcpy['NT'] = stcpy['ST']
        del stcpy['ST']
        del stcpy['MANIFESTATION']
        del stcpy['SILENT']
        del stcpy['HOST']
        del stcpy['last-seen']

        resp.extend([': '.join(x) for x in iter(stcpy.items())])
        resp.extend(('', ''))
        self.debug('doNotify content %s', resp)
        try:
            self.transport.write('\r\n'.join(resp), (SSDP_ADDR, SSDP_PORT))
            self.transport.write('\r\n'.join(resp), (SSDP_ADDR, SSDP_PORT))
        except (AttributeError, socket.error) as msg:
            self.info("failure sending out alive notification: %r", msg)

    def doByebye(self, usn):
        """Do byebye"""

        self.info('Sending byebye notification for %s', usn)

        resp = ['NOTIFY * HTTP/1.1',
                'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT),
                'NTS: ssdp:byebye',
                ]
        try:
            stcpy = dict(iter(self.known[usn].items()))
            stcpy['NT'] = stcpy['ST']
            del stcpy['ST']
            del stcpy['MANIFESTATION']
            del stcpy['SILENT']
            del stcpy['HOST']
            del stcpy['last-seen']
            resp.extend([': '.join(x) for x in iter(stcpy.items())])
            resp.extend(('', ''))
            self.debug('doByebye content %s', resp)
            if self.transport:
                try:
                    self.transport.write('\r\n'.join(resp), (SSDP_ADDR, SSDP_PORT))
                except (AttributeError, socket.error) as msg:
                    self.info("failure sending out byebye notification: %r", msg)
        except KeyError as msg:
            self.debug("error building byebye notification: %r", msg)

    def resendNotify(self):
        for usn in self.known:
            if self.known[usn]['MANIFESTATION'] == 'local':
                self.doNotify(usn)

    def check_valid(self):
        """ check if the discovered devices are still ok, or
            if we haven't received a new discovery response
        """
        self.debug("Checking devices/services are still valid")
        removable = []
        for usn in self.known:
            if self.known[usn]['MANIFESTATION'] != 'local':
                _, expiry = self.known[usn]['CACHE-CONTROL'].split('=')
                expiry = int(expiry)
                now = time.time()
                last_seen = self.known[usn]['last-seen']
                self.debug("Checking if %r is still valid - last seen %d (+%d), now %d", self.known[usn]['USN'], last_seen, expiry, now)
                if last_seen + expiry + 30 < now:
                    self.debug("Expiring: %r", self.known[usn])
                    if self.known[usn]['ST'] == 'upnp:rootdevice':
                        louie.send('Coherence.UPnP.SSDP.removed_device', None, device_type=self.known[usn]['ST'], infos=self.known[usn])
                    removable.append(usn)
        while len(removable) > 0:
            usn = removable.pop(0)
            del self.known[usn]

    def subscribe(self, name, callback):
        self._callbacks.setdefault(name, []).append(callback)

    def unsubscribe(self, name, callback):
        callbacks = self._callbacks.get(name, [])
        if callback in callbacks:
            callbacks.remove(callback)
        self._callbacks[name] = callbacks

    def callback(self, name, *args):
        for callback in self._callbacks.get(name, []):
            callback(*args)
