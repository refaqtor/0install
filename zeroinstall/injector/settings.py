"""
Holds the user's preferences and settings.
"""

# Copyright (C) 2011, Thomas Leonard
# See the README file for details, or visit http://0install.net.

from zeroinstall import _
import os
from logging import info, debug, warn
import ConfigParser

from zeroinstall.injector.namespaces import config_site, config_prog
from zeroinstall.support import basedir

class Settings(object):
	__slots__ = ['help_with_testing', 'freshness', 'network_use']

	def __init__(self):
		self.help_with_testing = False
		self.freshness = 60 * 60 * 24 * 30
		self.network_use = model.network_full

	def save_globals(self):
               """Write global settings."""
               parser = ConfigParser.ConfigParser()
               parser.add_section('global')

               parser.set('global', 'help_with_testing', self.help_with_testing)
               parser.set('global', 'network_use', self.network_use)
               parser.set('global', 'freshness', self.freshness)

               path = basedir.save_config_path(config_site, config_prog)
               path = os.path.join(path, 'global')
               parser.write(file(path + '.new', 'w'))
               os.rename(path + '.new', path)

def load_config():
	config = Config()
	parser = ConfigParser.RawConfigParser()
	parser.add_section('global')
	parser.set('global', 'help_with_testing', 'False')
	parser.set('global', 'freshness', str(60 * 60 * 24 * 30))	# One month
	parser.set('global', 'network_use', 'full')

	path = basedir.load_first_config(config_site, config_prog, 'global')
	if path:
		info("Loading configuration from %s", path)
		try:
			parser.read(path)
		except Exception, ex:
			warn(_("Error loading config: %s"), str(ex) or repr(ex))

	config.help_with_testing = parser.getboolean('global', 'help_with_testing')
	config.network_use = parser.get('global', 'network_use')
	config.freshness = int(parser.get('global', 'freshness'))

	assert config.network_use in model.network_levels, config.network_use

	return config
