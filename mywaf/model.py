import os

import mybuild

from mybuild.loader import my_yaml
from mybuild.loader import myfile
from mybuild.loader import pybuild

from mybuild.context import Context, InstanceAtom
from mybuild.solver import solve

from mybuild.util.compat import *


from waflib.Task import Task
from waflib.TaskGen import feature, extension, after_method
from waflib.Tools import ccroot


def build_true_graph(bld, solution):
    bld.my_recurse(pnode.instance for pnode, value in iteritems(solution)
                   if value and isinstance(pnode, InstanceAtom))


def create_model(bld):
    def create_module(pymodule_name, dictionary):
        def module_func(self):
            for key, value in dictionary:
                setattr(self, key, value)
        module_func.__module__ = pymodule_name
        module_func.__name__ = dictionary.pop('id')
        return mybuild.module(module_func)

    def get_pybuild_defaults():
        from mybuild import module, option
        return locals()

    def get_mybuild_defaults():
        class fake(object):
            def __init__(self, *args, **kwargs):
                super(fake, self).__init__()
                print args, kwargs

        class module(fake):
            pass
        from mybuild import option
        return locals()

    loaders_init = {
        myfile .LOADER_NAME: get_mybuild_defaults(),
        my_yaml.LOADER_NAME: {'!module': create_module},
        pybuild.LOADER_NAME: get_pybuild_defaults(),
    }

    prj = bld.my_load('prj', ['src', bld.env.TEMPLATE], loaders_init)

    print '>>>>>>>>', prj.hello.yaml_module

    ################################
    conf = prj.conf.conf

    context = Context()
    context.consider(conf)

    g = context.create_pgraph()

    solution = solve(g, {g.atom_for(conf):True})

    true_g = build_true_graph(bld, solution)



