"""
Types used on a per-build basis.
"""

__author__ = "Eldar Abusalimov"
__date__ = "2012-11-09"

__all__ = [
    "Context",
    "resolve",
]


from _compat import *

from collections import deque
from functools import partial
from itertools import product
from itertools import starmap

from mybuild.core import *
from mybuild.pgraph import *
from mybuild.solver import solve

from util.itertools import pop_iter

import logging
logger = logging.getLogger(__name__)


class Context(object):
    """docstring for Context"""

    def __init__(self):
        super(Context, self).__init__()
        self._domains = dict()   # {module: domain}, domain is optuple of sets
        self._providers = dict() # {module: provider}
        self._instantiation_queue = deque()

        self.pgraph = ContextPgraph(self)
        self.instance_nodes = list()

    def domain_for(self, module):
        try:
            domain = self._domains[module]
        except KeyError:
            domain = self._domains[module] = \
                module._opmake(set(optype._values)
                               for optype in module._optypes)
            self.post_product(domain)

        return domain

    def post(self, optuple, origin=None):
        logger.debug("add %s (posted by %s)", optuple, origin)
        self._instantiation_queue.append((optuple, origin))

    def post_product(self, iterables_optuple, origin=None):
        for optuple in map(iterables_optuple._make,
                           product(*iterables_optuple)):
            self.post(optuple, origin)

    def post_discover(self, optuple, origin=None):
        domain = self.domain_for(optuple._module)

        logger.debug("discover %s (posted by %s)", optuple, origin)
        for value, domain_to_extend in optuple._zipwith(domain):
            if value in domain_to_extend:
                continue

            domain_to_extend.add(value)

            self.post_product(optuple._make(option_domain
                    if option_domain is not domain_to_extend else (value,)
                    for option_domain in domain), origin)

    def init_module_providers(self, module):
        if module not in self._providers:
            self._providers[module] = set()

    def init_instance_providers(self, instance):
        self.init_module_providers(type(instance))
        for module in instance.provides:
            # Just in case it is not discovered yet.
            self.init_module_providers(module)
            self._providers[module].add(instance)

    def instantiate(self, optuple, origin=None):
        g = self.pgraph
        node = g.node_for(optuple)

        logger.debug("new %s (posted by %s)", optuple, origin)
        try:
            instance = optuple._instantiate_module()

        except InstanceError as error:
            logger.debug("    %s inviable: %s", optuple, error)

            node.error = error
            g.new_const(False, node,
                        why=why_inviable_instance_is_disabled)

        else:
            instance._post_init()

            node.instance = instance

            for constraint, condition in instance._constraints:
                self.post_discover(constraint, instance)
                if condition:
                    node.implies(g.node_for(constraint),
                                 why=why_instance_implies_its_constraints)

            self.init_instance_providers(instance)

        self.instance_nodes.append(node)

        return node

    def discover_all(self, initial_optuple):
        self.post_discover(initial_optuple)

        for optuple, origin in pop_iter(self._instantiation_queue,
                                        pop_meth='popleft'):
            self.instantiate(optuple, origin)

    def init_pgraph_domains(self):
        g = self.pgraph

        for module, domain in iteritems(self._domains):
            atom_for_module = partial(g.atom_for, module)
            module_atom = atom_for_module()

            for option, values in domain._iterpairs():
                atom_for_option = partial(atom_for_module, option)

                option_node = AtMostOne(g, map(atom_for_option, values),
                        why_one_operand_zero_implies_others_identity=
                            why_option_can_have_at_most_one_value,
                        why_identity_implies_all_operands_identity=
                            why_disabled_option_cannot_have_a_value,
                        why_all_operands_identity_implies_identity=
                            why_option_with_no_value_must_be_disabled)

                module_atom.equivalent(option_node,
                        why_becauseof=why_option_implies_module,
                        why_therefore=why_module_implies_option)

    def init_pgraph_providers(self):
        g = self.pgraph
        for module, providers in iteritems(self._providers):
            module_atom = g.atom_for(module)

            providers_node = AtMostOne(g,
                    (g.node_for(instance._optuple) for instance in providers),
                    why_one_operand_zero_implies_others_identity=
                        why_module_can_have_at_most_one_provider,
                    why_identity_implies_all_operands_identity=
                        why_not_included_module_cannot_have_a_provider,
                    why_all_operands_identity_implies_identity=
                        why_module_with_no_provider_must_not_be_included)

            module_atom.equivalent(providers_node,
                    why_becauseof=why_another_module_provides_this,
                    why_therefore=why_module_must_be_provided_by_anything)


    def resolve(self, initial_module):
        optuple = initial_module()

        self.discover_all(optuple)
        self.init_pgraph_domains()
        self.init_pgraph_providers()

        solution = solve(self.pgraph, {self.pgraph.node_for(optuple): True})

        instances = [node.instance
                     for node in self.instance_nodes if solution[node]]
        instance_map = dict((type(instance), instance)
                            for instance in instances)
        return instance_map


class ContextPgraph(Pgraph):

    def __init__(self, context):
        super(ContextPgraph, self).__init__()
        self.context = context

    def atom_for(self, module, option=None, value=Ellipsis):
        if option is not None:
            return self.new_node(OptionValueAtom, module, option, value)
        else:
            return self.new_node(ModuleAtom, module)

    def node_for(self, mslice):
        # TODO should accept arbitrary expr as well.
        return self.new_node(OptupleNode, mslice())


@ContextPgraph.node_type
class ModuleAtom(Atom):

    def __init__(self, module):
        super(ModuleAtom, self).__init__()
        self.module = module

        # Firstly, to build a default provider since it might not be included
        # explicitly
        is_default = any(module == interface.default_provider
                         for interface in module.provides)
        if is_default:
            self[True].level = 0

        self[False].level = 1  # then, try not to build a module

    def __repr__(self):
        return repr(self.module)


@ContextPgraph.node_type
class OptionValueAtom(Atom):

    def __init__(self, module, option, value):
        super(OptionValueAtom, self).__init__()
        self.module = module
        self.option = option
        self.value  = value

        is_default = (value == module._optype(option).default)
        if is_default:
            # Whenever possible prefer default option value,
            # but do it after a stage of disabling modules.
            self[True].level = 2

    def __repr__(self):
        return repr(self.module(**{self.option: self.value}))


@ContextPgraph.node_type
class OptupleNode(And):

    _optimize_new = True

    @classmethod
    def _new(cls, optuple):
        new_atom = partial(cls.pgraph.atom_for, optuple._module)
        option_atoms = tuple(starmap(new_atom, optuple._iterpairs()))

        if not option_atoms:
            return cls.pgraph.atom_for(optuple._module)
        else:
            return super(OptupleNode, cls)._new(option_atoms, optuple)

    def __init__(self, option_atoms, optuple):
        super(OptupleNode, self).__init__(option_atoms,
                why_identity_implies_all_operands_identity=None,  # TODO
                why_all_operands_identity_implies_identity=None)  # TODO

        self.optuple = optuple

    def __repr__(self):
        return repr(self.optuple)


def why_option_can_have_at_most_one_value(outcome, *causes):
    return 'option can have at most one value: %s: %s' % (outcome, causes)
def why_disabled_option_cannot_have_a_value(outcome, *causes):
    return 'disabled option cannot have a value: %s: %s' % (outcome, causes)
def why_option_with_no_value_must_be_disabled(outcome, *causes):
    return 'option with no value must be disabled: %s: %s' % (outcome, causes)
def why_option_implies_module(outcome, *causes):
    return 'option implies module: %s: %s' % (outcome, causes)
def why_module_implies_option(outcome, *causes):
    return 'module implies option: %s: %s' % (outcome, causes)

def why_module_can_have_at_most_one_provider(outcome, *causes):
    return 'module can have at most one provider: %s: %s' % (outcome, causes)
def why_not_included_module_cannot_have_a_provider(outcome, *causes):
    return 'not included module {0} cannot have a provider'.format(outcome)
def why_module_with_no_provider_must_not_be_included(outcome, *causes):
    return 'module {0} has no provider and cannot be included'.format(outcome)
def why_another_module_provides_this(outcome, cause):
    return 'module %s provided by %s' % (cause, outcome)

def why_module_must_be_provided_by_anything(outcome, cause):
    node, value = outcome
    if value and not node._operands:
            return 'Nothing provides {module}'.format(module=cause)
    return 'module {module} must be provided by anything'.format(module=cause)

def why_instance_implies_its_constraints(outcome, cause):
    node, value = outcome
    if value:
        fmt = 'required by {cause.node}'
    else:
        fmt = '{node} disabled as a dependent of {cause.node}'
    return fmt.format(**locals())

def why_inviable_instance_is_disabled(outcome, *_):
    node, value = outcome
    assert not value
    fmt = '{node} is disabled because of an error: {node.error}'
    return fmt.format(**locals())


def resolve(initial_module):
    return Context().resolve(initial_module)


if __name__ == '__main__':
    import util
    util.init_logging('%s.log' % __name__)

    from pprint import pprint

    from mybuild.binding.pydsl import *

    @module
    def conf(self):
        self._constrain(m1(bar=17))
        # self._constrain(m3)
        self.sources = 'test.c'

    @module
    def m1(self, bar=42):
        self._constrain(m2(foo=bar))

    @module
    def m2(self, foo=42):
        if foo == 42:
            raise InstanceError('FUUU')

    @module
    def m3(self):
        pass

    instances = resolve(conf)

    pprint(instances)
