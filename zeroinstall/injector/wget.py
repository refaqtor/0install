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
from httplib import HTTPConnection


PAGE_SIZE = 4096
# Convenient way to set maximum number of workers and maximum number
# of simultaneous connections per domain at the same time
# 15 is a Nettiquete..
MAX_RUN_WORKERS_AND_POOL = 15

_queue = None
_http_proxy_host = None
_http_proxy_port = None
_resolve_cache = {}


def start(url, modification_time, fd, receiver):
	"""Queue url to be downloaded, writing the contents to fd.
	When done, emit the signal "done(sender, status, reason, exception)" on receiver.
	If modification_time is not None, and the resource hasn't been modified since then,
	the status may be 304 (Not Modified) and the file is not downloaded."""
	_init()
	_queue.push({'requested_url': url,
				 'modification_time': modification_time,
				 'fd': fd,
				 'receiver': receiver,
				 })


def abort(url):
	"""Stop downloading url (or remove it from the queue if still pending)."""
	_init()
	_queue.abort(url)


def shutdown():
	global _queue
	if _queue is not None:
		_queue.clear()
		_queue = None


def _init():
	global _queue, _http_proxy_host, _http_proxy_port

	if _queue is not None:
		return

	proxy_detector = urllib2.ProxyHandler()
	if 'http' in proxy_detector.proxies:
		proxy = proxy_detector.proxies['http'].split(':') + [80]
		_http_proxy_host = proxy[0]
		_http_proxy_port = int(proxy[1])

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

			location_parts = urlparse.urlparse(location_url)
			if _http_proxy_host and location_parts.scheme == 'http':
				connection_url = (location_parts.scheme,
						_http_proxy_host, _http_proxy_port)
			else:
				connection_url = (location_parts.scheme,
						location_parts.hostname, location_parts.port or '80')
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
				except Exception, error:
					if isinstance(error, urllib2.HTTPError):
						status = error.status
					else:
						status = None
					reason = '%s %r' % (error, request)
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

	_read_file(request, response)

	return 200, None


def _read_file(request, response):
	while True:
		data = response.read(PAGE_SIZE)
		if not data:
			break
		os.write(request['fd'], data)	# XXX: return value ignored


def _resolve(hostname):
	addr = _resolve_cache.get(hostname)
	if not addr:
		addrinfo = socket.getaddrinfo(hostname, 0)[0]
		addr = _resolve_cache[hostname] = addrinfo[4][0]
	return addr
