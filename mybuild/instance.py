"""
Everything related to running @module functions.
"""

__author__ = "Eldar Abusalimov"
__date__ = "2012-12-14"


from itertools import izip
from operator import attrgetter

# do not import context due to bootstrapping issues
from core import Error
from pdag import *

import logs as log


class InstanceNodeBase(object):
    __slots__ = '_parent', '_childmap'

    parent = property(attrgetter('_parent'))

    def __init__(self, parent=None):
        super(InstanceNodeBase, self).__init__()

        self._parent = parent
        self._childmap = {}

    def get_child(self, key, value):
        return self._childmap[key][value]

    def _create_children(self, key, values):
        try:
            vmap = self._childmap[key]
        except KeyError:
            vmap = self._childmap[key] = {}

        for value in values:
            if value in vmap:
                raise ValueError
            vmap[value] = self._new_child(key, value)

        return (vmap[value] for value in values)

    def _new_child(self, parent_key=None, parent_value=None):
        cls = type(self)
        return cls(parent=self)

    def iter_children(self, key):
        return self._childmap[key].iteritems()


class InstanceNode(InstanceNodeBase):
    __slots__ = '_constraints', '_provideds', '_decisions'

    def __init__(self, parent=None):
        super(InstanceNode, self).__init__(parent)
        self._constraints = []
        self._provideds = []
        self._decisions = {}

    def _new_child(self, parent_key=None, parent_value=None):
        new = super(InstanceNode, self)._new_child(parent_key, parent_value)

        new._decisions.update(self._decisions)

        assert parent_key not in new._decisions
        new._decisions[parent_key] = parent_value

        return new

    def add_constraint(self, expr):
        self._constraints.append(expr)

    def add_provided(self, expr):
        self._provideds.append(expr)

    def make_decisions(self, module_expr, option=None, values=(True, False)):
        """
        Either retrieves an already taken decision (in case of replaying),
        or creates a new child for each value from 'values' iterable returning
        list of the resulting pairs.

        Returns: (value, node) pairs iterable.
        """
        key = module_expr, option

        try:
            value = self._decisions[key]

        except KeyError:
            values = tuple(values)
            return izip(values, self._create_children(key, values))

        else:
            return ((value, self),)

    def extend_decisions(self, module, option, value):
        key = module, option
        child, = self._create_children(key, (value,))
        return value, child

    def _cond_pnode(self, g, module_expr, option, value):
        if option is not None:
            # module_expr is definitely a plain module here
            pnode = g.atom_for(module_expr, option, value)

        else:
            # here value is bool
            pnode = g.pnode_for(module_expr)
            if not value:
                pnode = g.new(Not, pnode)

        return pnode

    def create_pnode(self, g):
        constraints = g.new(And,
            *(g.pnode_for(expr) for expr in self._constraints))

        def iter_conjuncts():
            for (module_expr, option), vmap in self._childmap.iteritems():
                for value, child in vmap.iteritems():

                    yield g.new(Implies,
                        self._cond_pnode(g, module_expr, option, value),
                        child.create_pnode(g))

        return g.new(And, constraints, *iter_conjuncts())

    def create_decisions_pnode(self, g):
        def iter_conjuncts():
            for (module_expr, option), value in self._decisions.iteritems():
                yield self._cond_pnode(g, module_expr, option, value)

        return g.new(And, *iter_conjuncts())

    def __repr__(self):
        decisions = self._decisions
        return ', '.join('%s%s=%s' %
                (module, '.' + option if option else '', value)
            for (module, option), value in decisions.iteritems())

class Instance(object):

    class _InstanceProxy(object):
        __slots__ = '_owner', '_optuple'

        def __init__(self, owner, optuple):
            super(Instance._InstanceProxy, self).__init__()
            self._owner = owner
            self._optuple = optuple

        def __nonzero__(self):
            return self._owner._decide(self._optuple)

        def __getattr__(self, attr):
            return self._owner._decide_option(self._optuple, attr)

    _context = property(attrgetter('_domain.context'))
    _optuple = property(attrgetter('_domain.optuple'))
    _spawn   = property(attrgetter('_domain.post_new'))

    def __init__(self, domain, node):
        super(Instance, self).__init__()
        self._domain = domain
        self._node = node

    def consider(self, mslice):
        optuple = mslice._to_optuple()
        module = optuple._module

        consider = self._context.consider

        consider(module)
        for option, value in optuple._iterpairs():
            consider(module, option, value)

    def constrain(self, expr):
        self.consider(expr)
        self._node.add_constraint(expr)

    def provides(self, expr):
        self.consider(expr)
        self._node.add_provided(expr)

    def _decide(self, expr):
        self.consider(expr)
        return self._make_decision(expr)

    def _decide_option(self, mslice, option):
        module = mslice._module

        def domain_gen():
            if not hasattr(mslice, option):
                raise AttributeError("'%s' module has no attribute '%s'" %
                                     (module._name, option))

            # Option without the module itself is meaningless.
            self.constrain(mslice)

            # Need to read and save the currnet node here
            # because '_make_decision' overwrites self._node it with its child.
            saved_node = self._node
            def on_domain_extend(new_value):
                _, child_node = saved_node.extend_decisions(module, option,
                                                            new_value)
                self._spawn(child_node)

            option_domain = self._context.domain_for(module, option)
            option_domain.subscribe(on_domain_extend)

            for value in option_domain:
                yield value

        return self._make_decision(module, option, domain_gen())

    def _make_decision(self, module_expr, option=None, domain=(True, False)):
        """
        Returns: a value taken.
        """
        decisions = iter(self._node.make_decisions(module_expr,
                                                   option, domain))

        try:
            # Retrieve the first one (if any) to return it.
            ret_value, self._node = decisions.next()

        except StopIteration:
            raise InstanceError('No viable choice to take')

        else:
            log.debug('mybuild: deciding %s%s=%s', module_expr,
                      '.' + option if option else '', ret_value)

        # Spawn for the rest ones.
        spawn = self._spawn
        for _, node in decisions:
            spawn(node)

        return ret_value

    def __repr__(self):
        optuple = self._optuple
        node_str = str(self._node)
        return '%s <%s>' % (optuple, node_str) if node_str else str(optuple)

class InstanceError(Error):
    """
    Throwing this kind of errors from inside a module function indicates that
    instance is not viable anymore and thus shouldn't be considered.
    """

