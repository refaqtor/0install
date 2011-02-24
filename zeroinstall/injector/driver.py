"""
A driver manages the process of iteratively solving and downloading extra feeds, and
then downloading the implementations.
settings.
"""

# Copyright (C) 2011, Thomas Leonard
# See the README file for details, or visit http://0install.net.

from zeroinstall import _
import time
import os
from logging import info, debug, warn
import ConfigParser

from zeroinstall import zerostore, SafeException
from zeroinstall.injector import arch, model
from zeroinstall.injector.model import Interface, Implementation, network_levels, network_offline, DistributionImplementation, network_full
from zeroinstall.injector.handler import Handler
from zeroinstall.injector.namespaces import config_site, config_prog
from zeroinstall.support import tasks, basedir

# If we started a check within this period, don't start another one:
FAILED_CHECK_DELAY = 60 * 60	# 1 Hour

class Driver:
	"""Manages the process of downloading feeds, solving, and downloading implementations.
	Typical use:
	 1. Create a Driver object using a DriverFactory, giving it the Requirements about the program to be run.
	 2. Call L{solve_with_downloads}. If more information is needed, a L{fetch.Fetcher} will be used to download it.
	 3. When all downloads are complete, the L{solver} contains the chosen versions.
	 4. Use L{get_uncached_implementations} to find where to get these versions and download them
	    using L{download_uncached_implementations}.
	@ivar solver: solver used to choose a set of implementations
	@type solver: L{solve.Solver}
	@ivar watchers: callbacks to invoke after recalculating
	@ivar stale_feeds: set of feeds which are present but haven't been checked for a long time
	@type stale_feeds: set
	"""
	__slots__ = ['watchers', 'requirements', '_warned_offline', 'stale_feeds', 'solver']

	def __init__(self, requirements = None, solver = None):
		"""
		@param requirements: Details about the program we want to run
		@type requirements: L{requirements.Requirements}
		"""
		self.watchers = []
		self.target_arch = arch.get_architecture(requirements.os, requirements.cpu)
		self.requirements = requirements
		self.solver = solver

		self.stale_feeds = set()

		# If we need to download something but can't because we are offline,
		# warn the user. But only the first time.
		self._warned_offline = False

	def download_and_import_feed_if_online(self, feed_url):
		"""If we're online, call L{fetch.Fetcher.download_and_import_feed}. Otherwise, log a suitable warning."""
		if self.network_use != network_offline:
			debug(_("Feed %s not cached and not off-line. Downloading..."), feed_url)
			return self.fetcher.download_and_import_feed(feed_url, self.iface_cache)
		else:
			if self._warned_offline:
				debug(_("Not downloading feed '%s' because we are off-line."), feed_url)
			else:
				warn(_("Not downloading feed '%s' because we are in off-line mode."), feed_url)
				self._warned_offline = True

	def get_uncached_implementations(self):
		"""List all chosen implementations which aren't yet available locally.
		@rtype: [(L{model.Interface}, L{model.Implementation})]"""
		iface_cache = self.iface_cache
		uncached = []
		for uri, selection in self.solver.selections.selections.iteritems():
			impl = selection.impl
			assert impl, self.solver.selections
			if not self.stores.is_available(impl):
				uncached.append((iface_cache.get_interface(uri), impl))
		return uncached

	@tasks.async
	def solve_with_downloads(self, force = False, update_local = False):
		"""Run the solver, then download any feeds that are missing or
		that need to be updated. Each time a new feed is imported into
		the cache, the solver is run again, possibly adding new downloads.
		@param force: whether to download even if we're already ready to run.
		@param update_local: fetch PackageKit feeds even if we're ready to run."""

		downloads_finished = set()		# Successful or otherwise
		downloads_in_progress = {}		# URL -> Download

		host_arch = self.target_arch
		if self.requirements.source:
			host_arch = arch.SourceArchitecture(host_arch)

		# There are three cases:
		# 1. We want to run immediately if possible. If not, download all the information we can.
		#    (force = False, update_local = False)
		# 2. We're in no hurry, but don't want to use the network unnecessarily.
		#    We should still update local information (from PackageKit).
		#    (force = False, update_local = True)
		# 3. The user explicitly asked us to refresh everything.
		#    (force = True)

		try_quick_exit = not (force or update_local)

		while True:
			self.solver.solve(self.root, host_arch, command_name = self.command)
			for w in self.watchers: w()

			if try_quick_exit and self.solver.ready:
				break
			try_quick_exit = False

			if not self.solver.ready:
				force = True

			for f in self.solver.feeds_used:
				if f in downloads_finished or f in downloads_in_progress:
					continue
				if os.path.isabs(f):
					if force:
						self.iface_cache.get_feed(f, force = True)
						downloads_in_progress[f] = tasks.IdleBlocker('Refresh local feed')
					continue
				elif f.startswith('distribution:'):
					if force or update_local:
						downloads_in_progress[f] = self.fetcher.download_and_import_feed(f, self.iface_cache)
				elif force and self.network_use != network_offline:
					downloads_in_progress[f] = self.fetcher.download_and_import_feed(f, self.iface_cache)
					# Once we've starting downloading some things,
					# we might as well get them all.
					force = True

			if not downloads_in_progress:
				if self.network_use == network_offline:
					info(_("Can't choose versions and in off-line mode, so aborting"))
				break

			# Wait for at least one download to finish
			blockers = downloads_in_progress.values()
			yield blockers
			tasks.check(blockers, self.handler.report_error)

			for f in downloads_in_progress.keys():
				if f in downloads_in_progress and downloads_in_progress[f].happened:
					del downloads_in_progress[f]
					downloads_finished.add(f)

					# Need to refetch any "distribution" feed that
					# depends on this one
					distro_feed_url = 'distribution:' + f
					if distro_feed_url in downloads_finished:
						downloads_finished.remove(distro_feed_url)
					if distro_feed_url in downloads_in_progress:
						del downloads_in_progress[distro_feed_url]

	@tasks.async
	def solve_and_download_impls(self, refresh = False, select_only = False):
		"""Run L{solve_with_downloads} and then get the selected implementations too.
		@raise SafeException: if we couldn't select a set of implementations
		@since: 0.40"""
		refreshed = self.solve_with_downloads(refresh)
		if refreshed:
			yield refreshed
			tasks.check(refreshed)

		if not self.solver.ready:
			raise self.solver.get_failure_reason()

		if not select_only:
			downloaded = self.download_uncached_implementations()
			if downloaded:
				yield downloaded
				tasks.check(downloaded)

	def need_download(self):
		"""Decide whether we need to download anything (but don't do it!)
		@return: true if we MUST download something (feeds or implementations)
		@rtype: bool"""
		host_arch = self.target_arch
		if self.requirements.source:
			host_arch = arch.SourceArchitecture(host_arch)
		self.solver.solve(self.root, host_arch, command_name = self.command)
		for w in self.watchers: w()

		if not self.solver.ready:
			return True		# Maybe a newer version will work?

		if self.get_uncached_implementations():
			return True

		return False

	def download_uncached_implementations(self):
		"""Download all implementations chosen by the solver that are missing from the cache."""
		assert self.solver.ready, "Solver is not ready!\n%s" % self.solver.selections
		return self.fetcher.download_impls([impl for impl in self.solver.selections.values() if not self.stores.is_available(impl)],
						   self.stores)

class DriverFactory:
	def __init__(self, settings, iface_cache, stores, user_interface):
		self.settings = settings
		self.iface_cache = iface_cache
		self.stores = stores
		self.user_interface = user_interface

	def make_driver(self, requirements):
		from zeroinstall.injector.solver import DefaultSolver
		solver = DefaultSolver(self.settings, self.stores, self.iface_cache)

		if requirements.before or requirements.not_before:
			solver.extra_restrictions[self.iface_cache.get_interface(requirements.interface_uri)] = [
					model.VersionRangeRestriction(model.parse_version(requirements.before),
								      model.parse_version(requirements.not_before))]

		return Driver(requirements, solver = solver)
