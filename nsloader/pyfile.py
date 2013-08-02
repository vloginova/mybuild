"""
Python-like loader which is able to customize default global namespace.
"""

from util.importlib.machinery import SourceFileLoader
from util.compat import *


class PyFileLoader(SourceFileLoader):
    """Loads Pybuild files and executes them as regular Python scripts.

    Upon creation of a new module initializes its namespace with defaults taken
    from the dictionary passed in __init__. Also adds a global variable
    pointing to a module corresponding to the namespace root.
    """

    MODULE   = 'PYFILE'
    FILENAME = 'Pyfile'

    @classmethod
    def init_ctx(cls, ctx, initials):
        return ctx, dict(initials)  # defaults

    def __init__(self, loader_ctx, fullname, path):
        super(PyFileLoader, self).__init__(fullname, path)
        self.ctx, self.defaults = loader_ctx

    def is_package(self, fullname):
        return False

    def _init_module(self, module):
        module.__dict__[self.ctx.namespace] = self.ctx.import_namespace()
        module.__dict__.update(self.defaults)

        super(PyFileLoader, self)._init_module(module)
