"""
Types used on a per-build basis.
"""

__author__ = "Eldar Abusalimov"
__date__ = "2012-11-09"

__all__ = ["Context"]


from collections import defaultdict
from collections import deque
from collections import MutableSet
from contextlib import contextmanager
from functools import partial
from itertools import chain
from itertools import izip
from itertools import izip_longest
from itertools import product
from operator import attrgetter

from core import *
from dtree import Dtree
from instance import Instance
from instance import InstanceNode
import pdag
from util import NotifyingMixin

import logs as log


class Context(object):
    """docstring for Context"""

    def __init__(self):
        super(Context, self).__init__()
        self._modules = {}
        self._job_queue = deque()
        self._reent_locked = False
        self._atoms = {}

    def post(self, fxn):
        with self.reent_lock(): # to flush the queue on block exit
            self._job_queue.append(fxn)

    @contextmanager
    def reent_lock(self):
        was_locked = self._reent_locked
        self._reent_locked = True

        try:
            yield
        finally:
            if not was_locked:
                self._job_queue_flush()
            self._reent_locked = was_locked

    def _job_queue_flush(self):
        queue = self._job_queue

        while queue:
            fxn = queue.popleft()
            fxn()

    def consider(self, module, option=None, value=Ellipsis):
        domain = self.domain_for(module)
        if option is not None:
            domain.consider_option(option, value)

    def register(self, instance):
        self.domain_for(instance._module).register(instance)

    def domain_for(self, module, option=None):
        try:
            domain = self._modules[module]
        except KeyError:
            with self.reent_lock():
                domain = self._modules[module] = ModuleDomain(self, module)

        if option is not None:
            domain = domain.domain_for(option)

        return domain

    def atom_for(self, module, option=None, value=Ellipsis):
        cache = self._atoms
        cache_key = module, option, value

        try:
            return cache[cache_key]
        except KeyError:
            pass

        if option is not None:
            ret = self.domain_for(module, option).atom_for(value)
        else:
            ret = self.domain_for(module).atom

        cache[cache_key] = ret

        return ret

    def pnode_from(self, mslice):
        # TODO should accept arbitrary expr as well.
        optuple = mslice._to_optuple()
        module = optuple._module

        atom_for = self.atom_for

        return pdag.And(atom_for(module),
                        *(atom_for(module, option, value)
                          for option, value in optuple._iterpairs()))

    def create_pdag_with_constraint(self):
        constraint = pdag.And(*(module.create_pnode()
                                for module in self._modules.itervalues()))
        return pdag.Pdag(*self._atoms.itervalues()), constraint

class DomainBase(object):
    """docstring for DomainBase"""

    context = property(attrgetter('_context'))

    def __init__(self, context):
        super(DomainBase, self).__init__()
        self._context = context


class ModuleDomain(DomainBase):
    """docstring for ModuleDomain"""

    module = property(attrgetter('_module'))
    atom = property(attrgetter('_atom'))

    def __init__(self, context, module):
        super(ModuleDomain, self).__init__(context)

        self._module = module
        self._atom = module._atom_type()

        self._instances = {} # { optuple : InstanceDomain }
        self._options = module._options._make(OptionDomain(option)
                                              for option in module._options)

        self._instantiate_product(self._options)

    def _instantiate_product(self, iterables):
        make_optuple = self._options._make
        instances = self._instances

        for new_tuple in product(*iterables):
            new_optuple = make_optuple(new_tuple)

            assert new_optuple not in instances
            instances[new_optuple] = InstanceDomain(self._context, new_optuple)

    def consider_option(self, option, value):
        domain_to_extend = getattr(self._options, option)
        if value in domain_to_extend:
            return

        log.debug('mybuild: extending %r with %r', domain_to_extend, value)
        domain_to_extend.add(value)

        self._instantiate_product(option_domain
            if option_domain is not domain_to_extend else (value,)
            for option_domain in self._options)

    def domain_for(self, option):
        return getattr(self._options, option)

    def create_pnode(self):
        # TODO don't like it
        pdag.EqGroup(self._atom, *(option.create_pnode()
                                   for option in self._options))
        return pdag.And(*(instance.create_pnode()
                          for instance in self._instances.itervalues()))


class NotifyingSet(MutableSet, NotifyingMixin):
    """Set with notification support. Backed by a dictionary."""

    def __init__(self, values):
        super(NotifyingSet, self).__init__()
        self._dict = {}

        self |= values

    def _create_value_for(self, key):
        pass

    def add(self, value):
        if value in self:
            return
        self._dict[value] = self._create_value_for(value)

        self._notify(value)

    def discard(self, value):
        if value not in self:
            return
        raise NotImplementedError

    def __iter__(self):
        return iter(self._dict)
    def __len__(self):
        return len(self._dict)
    def __contains__(self, value):
        return value in self._dict

    def __str__(self):
        return '<%s: %s>' % (type(self).__name__, self._dict.keys())


class OptionDomain(NotifyingSet):
    """docstring for OptionDomain"""

    def __init__(self, option):
        self._option = option
        super(OptionDomain, self).__init__(option._values)

    def atom_for(self, value):
        if value not in self:
            raise ValueError
        return self._dict[value]

    def _create_value_for(self, value):
        return self._option._atom_type(value)

    def create_pnode(self):
        return pdag.AtMostOne(*self._dict.itervalues())


class InstanceDomain(DomainBase):

    optuple = property(attrgetter('_optuple'))
    module  = property(attrgetter('_module'))

    _init_fxn = property(attrgetter('_optuple._module._init_fxn'))

    def __init__(self, context, optuple):
        super(InstanceDomain, self).__init__(context)

        self._optuple = optuple

        self._instances = []
        self._node = root_node = InstanceNode()

        self.post_new(root_node)

    def post_new(self, node):
        instance = Instance(self, node)

        def new():
            with log.debug("mybuild: new %s", instance):
                try:
                    self._init_fxn(instance, *self._optuple)
                except InstanceError as e:
                    log.debug("mybuild: unviable %s: %s", instance, e)
                else:
                    log.debug("mybuild: succeeded %s", instance)
                    self._instances.append(instance)

        self._context.post(new)

    def create_pnode(self):
        context = self._context
        constraints = self._node.create_pnode(context)
        return pdag.Implies(context.pnode_from(self._optuple), constraints)


if __name__ == '__main__':
    from mybuild import module, option

    log.zones = {'mybuild'}
    log.verbose = True
    log.init_log()

    @module
    def conf(self):
        self.constrain(m1)

    @module
    def m1(self):
        self.constrain(m2(foo=17))

    @module
    def m2(self, foo=42):
        self.constrain(m2)

    context = Context()
    context.consider(conf)

    conf_atom = context.atom_for(conf)
    pdag, constraint = context.create_pdag_with_constraint()
    dtree = Dtree(pdag)
    solution = dtree.solve({constraint:True, conf_atom:True})

    from pprint import pprint
    pprint(solution)

