# -*- coding: utf-8 -*-
# Copyright 2017 Camptocamp SA
# Copyright 2017 Odoo
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html)

from collections import defaultdict, OrderedDict

from odoo import models
from odoo.tools import OrderedSet, LastOrderedSet
from .exception import NoComponentError, SeveralComponentError


# this is duplicated from odoo.models.MetaModel._get_addon_name() which we
# unfortunately can't use because it's an instance method and should have been
# a @staticmethod
def _get_addon_name(full_name):
    # The (OpenERP) module name can be in the ``odoo.addons`` namespace
    # or not. For instance, module ``sale`` can be imported as
    # ``odoo.addons.sale`` (the right way) or ``sale`` (for backward
    # compatibility).
    module_parts = full_name.split('.')
    if len(module_parts) > 2 and module_parts[:2] == ['odoo', 'addons']:
        addon_name = full_name.split('.')[2]
    else:
        addon_name = full_name.split('.')[0]
    return addon_name


class ComponentGlobalRegistry(OrderedDict):
    """ Store all the components and allow to find them using criteria

    The key is the ``_name`` of the components.

    This is an OrderedDict, because we want to keep the registration order of
    the components, addons loaded first have their components found first.

    """

    # TODO use a LRU cache (repoze.lru?)
    def lookup(self, collection_name, usage=None, model_name=None):
        """ Find and return a list of components for a usage

        The collection name is required, however, if a component is not
        registered in a particular collection (no ``_collection``), it might
        will be returned (as far as the ``usage`` and ``model_name`` match).
        This is useful to share generic components across different
        collections.

        Then, the components of a collection are filtered by usage and/or
        model. The ``_usage`` is mandatory on the components. When the
        ``_model_name`` is empty, it means it can be used for every models,
        and it will ignore the ``model_name`` argument.

        The abstract components are never returned.

        :param collection_name: the name of the collection the component is
                                registered into.
        :param usage: the usage of component we are looking for
        :param model_name: filter on components that apply on this model

        """

        # keep the order so addons loaded first have components used first
        collection_components = [
            component for component in self.itervalues()
            if (component._collection == collection_name or
                component._collection is None) and
            not component._abstract
        ]
        candidates = []

        if usage is not None:
            components = [component for component in collection_components
                          if component._usage == usage]
            if components:
                candidates = components
        else:
            candidates = collection_components

        # filter out by model name
        candidates = [c for c in candidates
                      if c.apply_on_models is None or
                      model_name in c.apply_on_models]

        return candidates


# This is where we will keep all the generated classes of the Components
# it will be cleared and updated when the odoo's registry is rebuilt
all_components = ComponentGlobalRegistry()


class WorkContext(object):
    """ Transport the context required to work with components

    It is propagated through all the components, so any
    data or instance (like a random RPC client) that need
    to be propagated transversally to the components
    should be kept here.

    Including:

    .. attribute:: collection

        The collection we are working with. The collection is an Odoo
        Model that inherit from 'collection.base'. The collection attribute
        can be an record or an "empty" model.

    .. attribute:: model_name

        Name of the model we are working with. It means that any lookup for a
        component will be done for this model. It also provides a shortcut
        as a `model` attribute to use directly with the Odoo model from
        the components

    .. attribute:: model

        Odoo Model for ``model_name`` with the same Odoo
        :class:`~odoo.api.Environment` than the ``collection`` attribute.

    This is also the entrypoint to work with the components.

    ::

        collection = self.env['my.collection'].browse(1)
        work = WorkContext(collection, 'res.partner')
        component = work.component(usage='record.importer')

    Usually you will use the shortcut available thanks to the
    `collection.base` Model:

    ::

        collection = self.env['my.collection'].browse(1)
        work = collection.work_on('res.partner')
        component = work.component(usage='record.importer')

    It supports any arbitrary keyword arguments that will become attributes of
    the instance, and be propagated throughout all the components.

    ::

        collection = self.env['my.collection'].browse(1)
        work = collection.work_on('res.partner', hello='world')
        assert work.hello == 'world'

    When you need to work on a different model, a new work instance will be
    created for you when you work with the higher lever API. This is what
    happens under the hood:

    ::

        collection = self.env['my.collection'].browse(1)
        work = collection.work_on('res.partner', hello='world')
        assert work.model_name == 'res.partner'
        assert work.hello == 'world'
        work2 = work.work_on('res.users')
        # => spawn a new WorkContext with a copy of the attributes
        assert work.model_name == 'res.users'
        assert work.hello == 'world'

    """

    def __init__(self, collection, model_name,
                 components_registry=None, **kwargs):
        self.collection = collection
        self.model_name = model_name
        self.model = self.env[model_name]
        # lookup components in an alternative registry, used by the tests
        if components_registry is not None:
            self._components_registry = components_registry
        else:
            self._components_registry = all_components
        self._propagate_kwargs = ['_components_registry']
        for attr_name, value in kwargs.iteritems():
            setattr(self, attr_name, value)
            self._propagate_kwargs.append(attr_name)

    @property
    def env(self):
        """ Return the current Odoo env

        This is the environment of the current collection.
        """
        return self.collection.env

    def work_on(self, model_name):
        """ Create a new work context for another model keeping attributes

        Used when one need to lookup components for another model.
        """
        kwargs = {attr_name: getattr(self, attr_name)
                  for attr_name in self._propagate_kwargs}
        return self.__class__(self.collection, model_name, **kwargs)

    def component_by_name(self, name, model_name=None):
        """ Return a component by its name

        Entrypoint to get a component, then you will probably use
        meth:`~AbstractComponent.component_by_name`
        """
        base = self._components_registry['base'](self)
        return base.component_by_name(name, model_name=model_name)

    def component(self, usage=None, model_name=None):
        """ Return a component

        Entrypoint to get a component, then you will probably use
        meth:`~AbstractComponent.component` or
        meth:`~AbstractComponent.many_components`
        """
        return self._components_registry['base'](self).component(
            usage=usage,
            model_name=model_name,
        )

    def many_components(self, usage=None, model_name=None):
        """ Return several components

        Entrypoint to get a component, then you will probably use
        meth:`~AbstractComponent.component` or
        meth:`~AbstractComponent.many_components`
        """
        return self._components_registry['base'](self).many_components(
            usage=usage,
            model_name=model_name,
        )

    def __str__(self):
        return "WorkContext(%s,%s)" % (repr(self.collection), self.model_name)

    def __unicode__(self):
        return unicode(str(self))

    __repr__ = __str__


class MetaComponent(type):
    """ Metaclass for Components

    Every new :class:`Component` will be added to ``_modules_components``,
    that will be used by the component builder.

    """

    _modules_components = defaultdict(list)

    def __init__(self, name, bases, attrs):
        if not self._register:
            self._register = True
            super(MetaComponent, self).__init__(name, bases, attrs)
            return

        if not hasattr(self, '_module'):
            self._module = _get_addon_name(self.__module__)

        self._modules_components[self._module].append(self)

    @property
    def apply_on_models(self):
        # None means all models
        if self._apply_on is None:
            return None
        # always return a list, used for the lookup
        elif isinstance(self._apply_on, basestring):
            return [self._apply_on]
        return self._apply_on


class AbstractComponent(object):
    """ Main Component Model

    All components have a Python inheritance on this class or its companion
    :class:`Component`.

    ``AbstractComponent`` will not appear in the lookups for components,
    however they can be used as a base for other Components through inheritance
    (using ``_inherit``).

    The inheritance mechanism is like the Odoo's one for Models.  Every
    component starts with a ``_name``.

    ::

        class MyComponent(Component):
            _name = 'my.component'

            def speak(self, message):
                print message

    Every component implicitly inherit from the `base` component.

    Then there are two close but distinct inheritance types, which look
    familiar if you already know Odoo.  The first uses ``_inherit`` with an
    existing name, the name of the component we want to extend.  With the
    following example, ``my.component`` is now able to speak and to yell.

    ::

        class MyComponent(Component):  # name of the class does not matter
            _inherit = 'my.component'

            def yell(self, message):
                print message.upper()

    The second has a different ``_name``, it creates a new component, including
    the behavior of the inherited component, but without modifying it. In the
    following example, ``my.component`` is still able to speak and to yell
    (brough by the previous inherit), but not to sing.  ``another.component``
    is able to speak, to yell and to sing.

    ::

        class AnotherComponent(Component):
            _name = 'another.component'
            _inherit = 'my.component'

            def sing(self, message):
                print message.upper()

    It is all for the inheritance.  The next topic is the registration / lookup
    of components.

    It is handled by 3 attributes on the class:

    .. attribute: _collection

        The name of the collection where we want to register the component.
        This is not strictly mandatory as a component can be shared across
        several collections. But usually, you want to set a collection
        to reduce the odds of conflicts for the same usage/model.
        A collection can be for instance ``magento.backend``. It is the name
        of a model that inherits from ``collection.base``.
        See also :class:`WorkContext`.

    .. attribute: _apply_on

        List of names or name of the Odoo model(s) for which the component
        can be used.  When not set, the component can be used on any model.

    .. attribute: _usage

       The collection and the model (``_apply_on``) will help to filter the
       candidate components according to our working context (e.g. I'm working
       on ``magento.backend`` with the model ``magento.res.partner``).  The
       usage will define **what** kind of task the component we are looking for
       serves to. For instance, it might be ``record.importer``,
       ``export.mapper```... but you can be as creative as you want.

    Now, to get a component, you'll likely use :meth:`WorkContext.component`
    when you start to work with components in your flow, but then from within
    your components, you are more likely to use one of:

    * :meth:`component`
    * :meth:`many_components`
    * :meth:`component_by_name` (more rarely though)

    Declaration of some Components can look like::

        class FooBar(models.Model):
            _name = 'foo.bar.collection'
            _inherit = 'collection.base'  # this inherit is required


        class FooBarBase(AbstractComponent):
            _name = 'foo.bar.base'
            _collection = 'foo.bar.collection'  # name of the model above


        class Foo(Component):
            _name = 'foo'
            _inherit = 'foo.bar.base'  # we will inherit the _collection
            _apply_on = 'res.users'
            _usage = 'speak'

            def utter(self, message):
                print message


        class Bar(Component):
            _name = 'bar'
            _inherit = 'foo.bar.base'  # we will inherit the _collection
            _apply_on = 'res.users'
            _usage = 'yell'

            def utter(self, message):
                print message.upper() + '!!!'


        class Vocalizer(Component):
            _name = 'vocalizer'
            _inherit = 'foo.bar.base'
            _usage = 'vocalizer'
            # can be used for any model

            def vocalize(action, message):
                self.component(usage=action).utter(message)


    And their usage::

        >>> coll = self.env['foo.bar.collection'].browse(1)
        >>> work = coll.work_on('res.users')
        >>> vocalizer = work.component(usage='vocalizer')
        >>> vocalizer.vocalize('speak', 'hello world')
        hello world
        >>> vocalizer.vocalize('yell', 'hello world')
        HELLO WORLD!!!

    Hints:

    * If you want to create components without ``_apply_on``, choose a
      ``_usage`` that will not conflict other existing components.
    * Unless this is what you want and in that case you use
      :meth:`many_components` which will return all components for a usage
      with a matching or a not set ``_apply_on``.
    * It is advised to namespace the names of the components (e.g.
    ``magento.xxx``) to prevent conflicts between addons.

    """
    __metaclass__ = MetaComponent

    _register = False
    _abstract = True

    # used for inheritance
    _name = None
    _inherit = None

    # name of the collection to subscribe in
    _collection = None

    # None means any Model, can be a list ['res.users', ...]
    _apply_on = None
    # component purpose ('import.mapper', ...)
    _usage = None

    def __init__(self, work_context):
        super(AbstractComponent, self).__init__()
        self.work = work_context

    @property
    def collection(self):
        """ Collection we are working with """
        return self.work.collection

    @property
    def env(self):
        """ Current Odoo environment, the one of the collection record """
        return self.collection.env

    @property
    def model(self):
        """ The model instance we are working with """
        return self.work.model

    def _component_class_by_name(self, name):
        components_registry = self.work._components_registry
        component_class = components_registry.get(name)
        if not component_class:
            raise NoComponentError("No component with name '%s' found." % name)
        return component_class

    def component_by_name(self, name, model_name=None):
        """ Return a component by its name

        If the component exists, an instance of it will be returned,
        initialized with the current :class:`WorkContext`.

        A ``NoComponentError`` is raised if:

        * no component with this name exists
        * the ``_apply_on`` of the found component does not match
          with the current working model

        In the latter case, it can be an indication that you need to switch to
        a different model, you can do so by providing the ``model_name``
        argument.

        """
        if isinstance(model_name, models.BaseModel):
            model_name = model_name._name
        component_class = self._component_class_by_name(name)
        work_model = model_name or self.work.model_name
        if (component_class.apply_on_models and
                work_model not in component_class.apply_on_models):
            if len(component_class.apply_on_models) == 1:
                hint_models = "'%s'" % (component_class.apply_on_models[0],)
            else:
                hint_models = "<one of %r>" % (
                    component_class.apply_on_models,
                )
            raise NoComponentError(
                "Component with name '%s' can't be used for model '%s'.\n"
                "Hint: you might want to use: "
                "component_by_name('%s', model_name=%s)" %
                (name, work_model, name, hint_models)
            )

        if work_model == self.work.model_name:
            work_context = self.work
        else:
            work_context = self.work.work_on(model_name)
        return component_class(work_context)

    def _lookup_components(self, usage=None, model_name=None):
        component_classes = self.work._components_registry.lookup(
            self.collection._name,
            usage=usage,
            model_name=model_name,
        )

        return component_classes

    def component(self, usage=None, model_name=None):
        """ Find a component by usage and model for the current collection

        It searches a component using the rules of
        :meth:`ComponentGlobalRegistry.lookup`. When a component is found,
        it initialize it with the current :class:`WorkContext` and returned.

        A :class:`component.exception.SeveralComponentError` is raised if
        more than one component match for the provided
        ``usage``/``model_name``.

        A :class:`component.exception.NoComponentError` is raised if
        no component is found for the provided ``usage``/``model_name``.

        """
        if isinstance(model_name, models.BaseModel):
            model_name = model_name._name
        model_name = model_name or self.work.model_name
        component_classes = self._lookup_components(
            usage=usage, model_name=model_name
        )
        if not component_classes:
            raise NoComponentError(
                "No component found for collection '%s', "
                "usage '%s', model_name '%s'." %
                (self.collection._name, usage, model_name)
            )
        elif len(component_classes) > 1:
            raise SeveralComponentError(
                "Several components found for collection '%s', "
                "usage '%s', model_name '%s'. Found: %r" %
                (self.collection._name, usage or '',
                 model_name or '', component_classes)
            )
        if model_name == self.work.model_name:
            work_context = self.work
        else:
            work_context = self.work.work_on(model_name)
        return component_classes[0](work_context)

    def many_components(self, usage=None, model_name=None):
        """ Find many components by usage and model for the current collection

        It searches a component using the rules of
        :meth:`ComponentGlobalRegistry.lookup`. When components are found, they
        initialized with the current :class:`WorkContext` and returned as a
        list.

        If no component is found, an empty list is returned.

        """
        if isinstance(model_name, models.BaseModel):
            model_name = model_name._name
        model_name = model_name or self.work.model_name
        component_classes = self._lookup_components(
            usage=usage, model_name=model_name
        )
        if model_name == self.work.model_name:
            work_context = self.work
        else:
            work_context = self.work.work_on(model_name)
        return [comp(work_context) for comp in component_classes]

    def __str__(self):
        return "Component(%s)" % self._name

    def __unicode__(self):
        return unicode(str(self))

    __repr__ = __str__

    @classmethod
    def _build_component(cls, registry):
        """ Instantiate a given Component in the components registry.

        This method is called at the end of the Odoo's registry build.  The
        caller is :meth:`component.builder.ComponentBuilder.load_components`.

        It generates new classes, which will be the Component classes we will
        be using.  The new classes are generated following the inheritance
        of ``_inherit``. It ensures that the ``__bases__`` of the generated
        Component classes follow the ``_inherit`` chain.

        Once a Component class is created, it adds it in the Component Registry
        (:class:`ComponentGlobalRegistry`), so it will be available for
        lookups.

        At the end of new class creation, a hook method
        :meth:`_complete_component_build` is called, so you can customize
        further the created components. An example can be found in
        :meth:`odoo.addons.connector.components.mapper.Mapper._complete_component_build`

        The following code is roughly the same than the Odoo's one for
        building Models.

        """

        # In the simplest case, the component's registry class inherits from
        # cls and the other classes that define the component in a flat
        # hierarchy.  The registry contains the instance ``component`` (on the
        # left). Its class, ``ComponentClass``, carries inferred metadata that
        # is shared between all the component's instances for this registry
        # only.
        #
        #   class A1(Component):                    Component
        #       _name = 'a'                           / | \
        #                                            A3 A2 A1
        #   class A2(Component):                      \ | /
        #       _inherit = 'a'                    ComponentClass
        #
        #   class A3(Component):
        #       _inherit = 'a'
        #
        # When a component is extended by '_inherit', its base classes are
        # modified to include the current class and the other inherited
        # component classes.
        # Note that we actually inherit from other ``ComponentClass``, so that
        # extensions to an inherited component are immediately visible in the
        # current component class, like in the following example:
        #
        #   class A1(Component):
        #       _name = 'a'                          Component
        #                                            /  / \  \
        #   class B1(Component):                    /  A2 A1  \
        #       _name = 'b'                        /   \  /    \
        #                                         B2 ComponentA B1
        #   class B2(Component):                   \     |     /
        #       _name = 'b'                         \    |    /
        #       _inherit = ['a', 'b']                \   |   /
        #                                            ComponentB
        #   class A2(Component):
        #       _inherit = 'a'

        # determine inherited components
        parents = cls._inherit
        if isinstance(parents, basestring):
            parents = [parents]
        elif parents is None:
            parents = []

        if cls._name in registry:
            raise TypeError('Component %r (in class %r) already exists. '
                            'Consider using _inherit instead of _name '
                            'or using a different _name.' % (cls._name, cls))

        # determine the component's name
        name = cls._name or (len(parents) == 1 and parents[0])

        if not name:
            raise TypeError('Component %r must have a _name' % cls)

        # all components except 'base' implicitly inherit from 'base'
        if name != 'base':
            parents = list(parents) + ['base']

        # create or retrieve the component's class
        if name in parents:
            if name not in registry:
                raise TypeError("Component %r does not exist in registry." %
                                name)
            ComponentClass = registry[name]
        else:
            ComponentClass = type(
                name, (AbstractComponent,),
                {'_name': name,
                 '_register': False,
                 # names of children component
                 '_inherit_children': OrderedSet()},
            )

        # determine all the classes the component should inherit from
        bases = LastOrderedSet([cls])
        for parent in parents:
            if parent not in registry:
                raise TypeError(
                    "Component %r inherits from non-existing component %r." %
                    (name, parent)
                )
            parent_class = registry[parent]
            if parent == name:
                for base in parent_class.__bases__:
                    bases.add(base)
            else:
                bases.add(parent_class)
                parent_class._inherit_children.add(name)
        ComponentClass.__bases__ = tuple(bases)

        ComponentClass._complete_component_build()

        registry[name] = ComponentClass

        return ComponentClass

    @classmethod
    def _complete_component_build(cls):
        """ Complete build of the new component class

        After the component has been built from its bases, this method is
        called, and can be used to customize the class before it can be used.

        Nothing is done in the base Component, but a Component can inherit
        the method to add its own behavior.
        """


class Component(AbstractComponent):
    """ Concrete Component class

    This is the class you inherit from when you want your component to
    be registered in the component collections.

    Look in :class:`AbstractComponent` for more details.

    """
    _register = False
    _abstract = False
