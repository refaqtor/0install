# Copyright (C) 2011, Aleksey Lim
# See the README file for details, or visit http://0install.net.

import os
import sys
import json
import atexit
import thread
import socket
import urllib2
import urlparse
import threading
from select import select
from httplib import HTTPConnection, HTTPException


PAGE_SIZE = 4096
# Convenient way to set maximum number of workers and maximum number
# of simultaneous connections per domain at the same time
# 15 is a Nettiquete..
MAX_RUN_WORKERS_AND_POOL = 15

_queue = None
_resolve_cache = {}
_proxy_support = None


def start(url, modification_time, outfile, receiver):
	_init()
	_queue.push({'requested_url': url,
				 'modification_time': modification_time,
				 'outfile': outfile,
				 'receiver': receiver,
				 })


def _split_hostport(host):
	i = host.rfind(':')
	j = host.rfind(']')		 # ipv6 addresses have [...]
	if i > j:
		try:
			port = int(host[i+1:])
		except ValueError:
			raise InvalidURL("nonnumeric port: '%s'" % host[i+1:])	# XXX
		host = host[:i]
	else:
		port = self.default_port
	if host and host[0] == '[' and host[-1] == ']':
		host = host[1:-1]
	return host, port


def abort(url):
	_init()
	_queue.abort(url)


def shutdown():
	global _queue
	if _queue is not None:
		_queue.clear()
		_queue = None


def _init():
	global _queue, _proxy_support

	if _queue is not None:
		return

	_proxy_support = urllib2.ProxyHandler()

	_queue = _RequestsQueue()
	atexit.register(shutdown)


class _RequestsQueue(object):

	def __init__(self):
		self._mutex = threading.Lock()
		self._condition = threading.Condition(self._mutex)
		self._workers = []
		self._requests = {}
		self._requests_in_process = {}
		self._workders_in_wait = 0
		self._pool = _ConnectionsPool()
		self._exiting = False

	def push(self, request):
		worker = None

		self._mutex.acquire()
		try:
			self._requests[request['requested_url']] = request
			if self._workders_in_wait:
				self._condition.notify()
			if len(self._workers) < MAX_RUN_WORKERS_AND_POOL:
				worker = _Worker(self._pop)
				self._workers.append(worker)
		finally:
			self._mutex.release()

		if worker is not None:
			worker.start()

	def abort(self, url):
		self._mutex.acquire()
		try:
			if url in self._requests:
				del self._requests[url]
			if url in self._requests_in_process:
				self._requests_in_process[url].close()
		finally:
			self._mutex.release()

	def clear(self):
		self._mutex.acquire()
		try:
			self._exiting = True
			self._requests.clear()
			for connection in self._requests_in_process.values():
				connection.close()
			self._condition.notify_all()
		finally:
			self._mutex.release()

	def _pop(self, prev_connection):
		self._mutex.acquire()
		try:
			if prev_connection is not None:
				del self._requests_in_process[
						prev_connection.requested['requested_url']]
				self._pool.push(prev_connection)

			if hasattr(prev_connection, 'redirect'):
				location_url, request = prev_connection.redirect
				delattr(prev_connection, 'redirect')
			else:
				while not self._requests:
					if self._exiting:
						return None, None, None
					self._workders_in_wait += 1
					self._condition.wait()
					self._workders_in_wait -= 1
				location_url, request = self._requests.popitem()

			req = urllib2.Request(location_url)
			meth = req.get_type() + '_open'
			new_request = getattr(_proxy_support, meth)(req)
			if new_request:
				req = new_request

			# XXX: loses authn information
			host, port = _split_hostport(req.get_host())
			connection_url = (req.get_type(), host, port)

			connection = self._pool.pop(connection_url)
		finally:
			self._mutex.release()

		request['location_url'] = location_url
		request['connection_url'] = connection_url

		scheme, host, port = connection_url
		if connection is None and scheme == 'http':
			connection = HTTPConnection(_resolve(host), port)

		if connection is None:
			openner = _urllib_openner
		else:
			connection.requested = request
			self._requests_in_process[request['requested_url']] = connection
			openner = _http_openner

		return request, connection, openner


class _Redirect(Exception):

	def __init__(self, location):
		self.location = location


class _ConnectionsPool(object):

	def __init__(self):
		self._connections = {}

	def __iter__(self):
		for i in self._connections.values():
			yield i

	def __getitem__(self, connection_url):
		pool = self._connections.get(connection_url)
		if pool is None:
			pool = self._connections[connection_url] = []
		return pool

	def push(self, connection):
		if connection is None:
			return
		pool = self[connection.requested['connection_url']]
		# That should not happen because max number of workers is equal to
		# max number of simultaneous connections per domain
		assert len(pool) <= MAX_RUN_WORKERS_AND_POOL
		pool.insert(0, connection)

	def pop(self, connection_url):
		pool = self[connection_url]
		if pool:
			connection = pool.pop()
			if isinstance(connection, HTTPConnection) and \
					connection.sock is not None and \
					select([connection.sock], [], [], 0.0)[0]:
				# Either data is buffered (bad), or the connection is dropped
				connection.close()
			return connection


class _Worker(threading.Thread):

	def __init__(self, pop_request_cb):
		threading.Thread.__init__(self)
		# To not wait for the thread on process exit
		self.daemon = True
		self._pop_request = pop_request_cb

	def run(self):
		try:
			connection = None
			while True:
				request, connection, openner = self._pop_request(connection)
				if openner is None:
					break

				try:
					status, reason = openner(connection, request)
					exception = None
				except _Redirect, redirect:
					connection.redirect = (redirect.location, request)
					continue
				except (urllib2.HTTPError, urllib2.URLError, HTTPException, socket.error) as ex:
					if isinstance(ex, urllib2.HTTPError):
						status = ex.status
					else:
						status = None
					reason = '%s %r' % (ex, request)
					__, ex, tb = sys.exc_info()
					import download	# XXX
					from zeroinstall import _ # XXX
					exception = (download.DownloadError(_('Error downloading {url}: {ex}').format(url = request, ex = ex)), tb)
				except Exception, error:
					__, ex, tb = sys.exc_info()
					exception = (ex, tb)

				request['receiver'].emit('done', status, reason, exception)
		except KeyboardInterrupt, e:
			thread.interrupt_main()
			raise
		except Exception, e:
			thread.interrupt_main()
			raise


def _http_openner(connection, request):
	headers = {'connection': 'keep-alive'}
	if request.get('modification_time'):
		headers['If-Modified-Since'] = request['modification_time']
	connection.request('GET', request['location_url'])

	response = connection.getresponse()
	try:
		# Handle redirection
		if response.status in [301, 302, 303, 307] and \
				response.getheader('location'):
			raise _Redirect(response.getheader('location'))
		if response.status == 200:
			_read_file(request, response)
		return response.status, response.reason
	finally:
		response.close()


def _urllib_openner(connection, request):
	url_request = urllib2.Request(request['location_url'])
	if request['location_url'].startswith('http:') and \
			request.get('modification_time'):
		url_request.add_header(
				'If-Modified-Since', request['modification_time'])

	response = urllib2.urlopen(url_request)
	try:
		_read_file(request, response)
	finally:
		response.close()

	return 200, None


def _read_file(request, response):
	while True:
		data = response.read(PAGE_SIZE)
		if not data:
			request['outfile'].flush()
			break
		request['outfile'].write(data)


def _resolve(hostname):
	addr = _resolve_cache.get(hostname)
	if not addr:
		addrinfo = socket.getaddrinfo(hostname, 0)[0]
		addr = _resolve_cache[hostname] = addrinfo[4][0]
	return addr
