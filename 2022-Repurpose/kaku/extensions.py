# -*- coding: utf-8 -*-
"""
:copyright: (c) 2016 by Mike Taylor
:license: CC0 1.0 Universal, see LICENSE for more details.
"""

from flask_cache import Cache
from flask_debugtoolbar import DebugToolbarExtension


# Setup flask cache
cache         = Cache()
debug_toolbar = DebugToolbarExtension()
