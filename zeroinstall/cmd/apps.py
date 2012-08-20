"""
The B{0install apps} command-line interface.
"""

# Copyright (C) 2012, Thomas Leonard
# See the README file for details, or visit http://0install.net.

from __future__ import print_function

import sys

from zeroinstall.cmd import UsageError
from zeroinstall import helpers

syntax = ""

def add_options(parser):
	pass

def handle(config, options, args):
	if len(args) != 0:
		raise UsageError()

	result = helpers.get_selections_gui(None, ['--apps'], use_gui = options.gui)
	if result is helpers.DontUseGUI:
		apps = config.app_mgr.list_apps()
		if apps:
			for app in apps:
				print(app.get_name())
		else:
			print('No apps. Use "0install add" to add some.')
