"""
Handles URL downloads.

This is the low-level interface for downloading interfaces, implementations, icons, etc.

@see: L{fetch} higher-level API for downloads that uses this module
"""

# Copyright (C) 2009, Thomas Leonard
# See the README file for details, or visit http://0install.net.

import tempfile, os, sys, subprocess

import gobject

from zeroinstall import SafeException
from zeroinstall.support import tasks
from zeroinstall.injector import wget
from logging import info, debug
from zeroinstall import _

gobject.threads_init()

download_starting = "starting"	# Waiting for UI to start it
download_fetching = "fetching"	# In progress
download_complete = "complete"	# Downloaded and cached OK
download_failed = "failed"

class DownloadError(SafeException):
	"""Download process failed."""
	pass

class DownloadAborted(DownloadError):
	"""Download aborted because of a call to L{Download.abort}"""
	def __init__(self, message = None):
		SafeException.__init__(self, message or _("Download aborted at user's request"))

class Download(gobject.GObject):
	"""A download of a single resource to a temporary file.
	@ivar url: the URL of the resource being fetched
	@type url: str
	@ivar tempfile: the file storing the downloaded data
	@type tempfile: file
	@ivar status: the status of the download
	@type status: (download_starting | download_fetching | download_failed | download_complete)
	@ivar expected_size: the expected final size of the file
	@type expected_size: int | None
	@ivar downloaded: triggered when the download ends (on success or failure)
	@type downloaded: L{tasks.Blocker}
	@ivar hint: hint passed by and for caller
	@type hint: object
	@ivar aborted_by_user: whether anyone has called L{abort}
	@type aborted_by_user: bool
	@ivar unmodified: whether the resource was not modified since the modification_time given at construction
	@type unmodified: bool
	"""
	__slots__ = ['url', 'tempfile', 'status', 'expected_size', 'downloaded',
			 'hint', '_final_total_size', 'aborted_by_user',
			 'modification_time', 'unmodified']

	# XXX: why? some threading issue?
	__gsignals__ = {
			'done': (
				gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
				[object, object, object]),
			}

	def __init__(self, url, hint = None, modification_time = None):
		"""Create a new download object.
		@param url: the resource to download
		@param hint: object with which this download is associated (an optional hint for the GUI)
		@param modification_time: string with HTTP date that indicates last modification time.
		  The resource will not be downloaded if it was not modified since that date.
		@postcondition: L{status} == L{download_starting}."""
		gobject.GObject.__init__(self)

		self.url = url
		self.status = download_starting
		self.hint = hint
		self.aborted_by_user = False
		self.modification_time = modification_time
		self.unmodified = False

		self.tempfile = None		# Stream for result
		self.downloaded = None

		self.expected_size = None	# Final size (excluding skipped bytes)
		self._final_total_size = None	# Set when download is finished

	def start(self):
		"""Create a temporary file and begin the download.
		@precondition: L{status} == L{download_starting}"""
		assert self.status == download_starting
		assert self.downloaded is None

		self.tempfile = tempfile.TemporaryFile(prefix='injector-dl-data-')
		self.downloaded = tasks.Blocker('download %s' % self.url)
		self.status = download_fetching

		self.connect('done', self.__done_cb)
		# Let the caller to read tempfile before closing the connection
		# TODO eliminate such unreliable workflow
		gobject.idle_add(wget.start, self.url, self.modification_time,
				self.tempfile.fileno(), self)

	def __done_cb(self, sender, status, reason, exception):
		self.disconnect_by_func(self.__done_cb)

		try:
			self._final_total_size = 0
			if self.aborted_by_user:
				raise DownloadAborted()
			elif status == 304:
				debug("No need to download as not modified %s", self.url)
				self.unmodified = True
			elif status == 200:
				self._final_total_size = self.get_bytes_downloaded_so_far()
				# Check that the download has the correct size,
				# if we know what it should be.
				if self.expected_size is not None and \
						self.expected_size != self._final_total_size:
					raise SafeException(
							_('Downloaded archive has incorrect size.\n'
							  'URL: %(url)s\n'
							  'Expected: %(expected_size)d bytes\n'
							  'Received: %(size)d bytes') % {
								  'url': self.url,
								  'expected_size': self.expected_size,
								  'size': self._final_total_size})
			elif exception is None:
				raise DownloadError(_('Download %s failed: %s') % \
						(self.url, reason))
		except Exception as error:
			__, ex, tb = sys.exc_info()
			exception = (ex, tb)

		if exception is None:
			self.status = download_complete
			self.downloaded.trigger()
		else:
			self.status = download_failed
			self.downloaded.trigger(exception=exception)

	def abort(self):
		"""Signal the current download to stop.
		@postcondition: L{aborted_by_user}"""
		if self.status == download_fetching:
			info(_("Aborting download of %s"), self.url)
			self.__done_cb(None, None, None, None)
			wget.abort(self.url)
			self.aborted_by_user = True
		else:
			self.status = download_failed

	def get_current_fraction(self):
		"""Returns the current fraction of this download that has been fetched (from 0 to 1),
		or None if the total size isn't known.
		@return: fraction downloaded
		@rtype: float | None"""
		if self.status is download_starting:
			return 0
		if self.tempfile is None:
			return 1
		if self.expected_size is None:
			return None		# Unknown
		current_size = self.get_bytes_downloaded_so_far()
		return float(current_size) / self.expected_size
	
	def get_bytes_downloaded_so_far(self):
		"""Get the download progress. Will be zero if the download has not yet started.
		@rtype: int"""
		if self.status is download_starting:
			return 0
		elif self.status is download_fetching:
			return os.fstat(self.tempfile.fileno()).st_size
		else:
			return self._final_total_size
	
	def __str__(self):
		return _("<Download from %s>") % self.url
