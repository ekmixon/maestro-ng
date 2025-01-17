# Copyright (C) 2013-2014 SignalFuse, Inc.
# Copyright (C) 2015-2018 SignalFx, Inc.
#
# Docker container orchestration utility.

from __future__ import print_function

import functools
import fnmatch

from . import audit
from . import entities
from . import exceptions
from . import plays
from . import shipproviders
from . import termoutput

AVAILABLE_MAESTRO_COMMANDS = ['status', 'start', 'stop', 'restart',
                              'pull', 'clean', 'logs', 'deptree']


class Conductor:
    """The Maestro; the Conductor.

    The conductor is in charge of parsing and analyzing the environment
    description and carrying out the orchestration plays to act on the services
    and containers described in the environment.
    """

    def __init__(self, config):
        self._config = config
        if 'name' not in config:
            raise exceptions.EnvironmentConfigurationException(
                'Environment name is missing')

        self.ships = (shipproviders.ShipsProviderFactory
                      .from_config(config).ships())

        # Register defined private Docker registries authentications
        self.registries = self._config.get('registries') or {}

        # Build all the entities.
        self.services = {}
        self.containers = {}

        for kind, service in self._config.get('services', {}).items():
            # Duplicate services can't happen in the YAML structure.
            self.services[kind] = \
                entities.Service(
                    name=kind,
                    image=service['image'],
                    omit=service.get('omit', False),
                    envfile=service.get('envfile', []),
                    env=service.get('env', {}),
                    maestro_schema=self.schema,
                    maestro_env_name=self.env_name,
                    maestro_env_base=self.base_dir,
                    lifecycle=service.get('lifecycle', {}),
                    limits=service.get('limits', {}),
                    ports=service.get('ports', {}))

            for name, instance in service['instances'].items():
                # Duplicate instances can't happen within the same service in
                # the YAML structure, but may happen across services, so we
                # need to check for that.
                existing = self.containers.get(name)
                if existing:
                    raise exceptions.EnvironmentConfigurationException(
                            'Service instance {} is already defined in {}'
                            .format(name, existing.service.name))
                self.containers[name] = \
                    entities.Container(
                        ships=self.ships,
                        name=name,
                        service=self.services[kind],
                        config=instance,
                        maestro_schema=self.schema,
                        maestro_env_base=self.base_dir)

        # Resolve dependencies between services.
        for kind, service in self._config.get('services', {}).items():
            for dependency in service.get('requires', []):
                if dependency not in self.services:
                    raise exceptions.EnvironmentConfigurationException(
                        'Service dependency {} defined on {} does not exist'
                        .format(dependency, kind))
                self.services[kind].add_dependency(self.services[dependency])
                self.services[dependency].add_dependent(self.services[kind])
            for wants_info in service.get('wants_info', []):
                if wants_info not in self.services:
                    raise exceptions.EnvironmentConfigurationException(
                        'Service dependency {} defined on {} does not exist'
                        .format(dependency, kind))
                self.services[kind].add_wants_info(self.services[wants_info])

        # Provide link environment variables to each container of each service
        # that requires it or wants it.
        dep_link_vars_cache = {}
        for service in self.services.values():
            service_link_vars = service.get_link_variables(True)
            for container in service.containers:
                # Containers always know about their peers in the same service.
                container.env.update(service_link_vars)
                # Containers also get links from the service's dependencies.
                for dependency in service.requires.union(service.wants_info):
                    dep_link_vars = dep_link_vars_cache.get(dependency, None)
                    if dep_link_vars is None:
                        dep_link_vars = dependency.get_link_variables()
                        dep_link_vars_cache[dependency] = dep_link_vars
                    container.env.update(dep_link_vars)

        # Check for host locality and volume conflicts on volumes_from, and add
        # service dependencies implicitely required by volumes_from another
        # service's container(s).
        for container in self.containers.values():
            for volumes_from in container.volumes_from:
                if volumes_from not in self.containers:
                    raise exceptions.InvalidVolumeConfigurationException(
                        'Unknown container {} to get volumes from for {}!'
                        .format(volumes_from, container.name))

                other = self.containers[volumes_from]
                if other.ship != container.ship:
                    raise exceptions.InvalidVolumeConfigurationException(
                        '{} and {} must be on the same host for '
                        'volumes_from declaration in {}!'
                        .format(other.name, container.name,
                                container.name))

                conflicts = container.get_volumes().intersection(
                    other.get_volumes())
                if conflicts:
                    raise exceptions.InvalidVolumeConfigurationException(
                        'Volume conflicts between {} and {}: {}!'
                        .format(container.name, other.name,
                                ', '.join(conflicts)))

                # Add the dependency against the volumes_from's service.
                container.service.add_dependency(other.service)

        # Instantiate audit bindings
        self.auditor = audit.AuditorFactory.from_config(
            self._config.get('audit'))

    @property
    def schema(self):
        return self._config['__maestro'].get('schema', 1)

    @property
    def base_dir(self):
        return self._config['__maestro']['base_dir']

    @property
    def env_name(self):
        return self._config['name']

    def _order_dependencies(self, pending=None, ordered=None, forward=True):
        """Order the given set of containers into an order respecting the
        service dependencies in the given direction.

        The list of containers to order should be passed in the pending
        parameter. The ordered list will be returned by the function (the
        ordered parameter is for internal recursion use only).

        The direction of the dependencies controls whether the ordering should
        be constructed for startup (dependencies first) or shutdown (dependents
        first).
        """
        pending = pending or []
        ordered = ordered or []
        wait = []
        for container in pending:
            deps = self._gather_dependencies([container], forward)
            if deps and not deps.issubset(set(ordered + [container])):
                wait.append(container)
            else:
                ordered.append(container)

        # If wait and pending are not empty and have the same length, it means
        # we were not able to order any container from the pending list (they
        # all went to the wait list). This means the dependency tree cannot be
        # resolved and an error should be raised.
        if wait and pending and len(wait) == len(pending):
            raise exceptions.DependencyException(
                'Cannot resolve dependencies for containers {}!'.format(
                    map(lambda x: x.name, wait)))

        # As long as 'wait' has elements, keep recursing to resolve
        # dependencies. Otherwise, returned the ordered list, which should now
        # be final.
        return wait and self._order_dependencies(wait, ordered, forward) \
            or ordered

    def _gather_dependencies(self, containers, forward=True):
        """Transitively gather all containers from the dependencies or
        dependents (depending on the value of the forward parameter) services
        that the services the given containers are members of."""
        result = set(containers or self.containers.values())
        for container in result:
            deps = container.service.requires if forward \
                else container.service.needed_for
            deps = functools.reduce(lambda x, y: x.union(y),
                                    [s.containers for s in deps], set([]))
            result = result.union(deps)
        return result

    def _to_containers(self, things, expand_services, container_filter,
                       ship_filter):
        """Transform a list of "things", container names or service names, to
        an expended list of Container objects.

        Args:
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                 names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        def parse_thing(s):
            if s in self.containers:
                return [self.containers[s]]
            elif s in self.services:
                if expand_services:
                    return self.services[s].containers
                raise exceptions.OrchestrationException(
                    '{} is a service but --expand-services was not specified'
                    .format(s))
            raise exceptions.OrchestrationException(
                '{} is neither a service nor a container!'.format(s))
        result = []
        for thing in things:
            result += parse_thing(thing)
        if container_filter:
            result = [c for c in set(result)
                      if fnmatch.fnmatch(c.name, container_filter)]
        if ship_filter:
            result = [c for c in set(result)
                      if fnmatch.fnmatch(c.ship.name, ship_filter)]
        return sorted(result)

    def _to_services(self, things):
        """Transform a list of "things", container names or service names, to a
        list of Service objects with no duplicates."""
        def parse_thing(s):
            if s in self.containers:
                return self.containers[s].service
            if s in self.services:
                return self.services[s]
            raise exceptions.OrchestrationException(
                '{} is neither a service nor a container!'.format(s))
        return sorted(set(map(parse_thing, things)))

    def _ordered_containers(self, things, expand_services, forward=True):
        """Return the ordered list of containers from the list of names passed
        to it (either container names or service names).

        Args:
            things (list<string>):
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            forward (boolean): controls the direction of the dependency tree.
        """
        return self._order_dependencies(
            sorted(self._gather_dependencies(
                self._to_containers(things, expand_services, None, None),
                forward)),
            forward=forward)

    def complete(self, tokens, **kwargs):
        """Completion handler; designed to work for shell auto-completion.

        Takens in a list of tokens (that may need to be split up into
        individual tokens) and assumes it is the start of a command the user
        tries to type. The method then returns a list of words that would
        complete the last token the user is currently trying to complete.
        """
        args = []
        for token in tokens:
            args += [x for x in token.split(' ') if not x.startswith('-')]

        choices = AVAILABLE_MAESTRO_COMMANDS if len(args) <= 2 \
            else self.services.keys() + self.containers.keys()
        prefix = ''

        if len(args) == 2:
            prefix = args[1]
            if prefix in choices:
                args.append('')

        if len(args) > 2:
            prefix = args.pop()

        print(' '.join(filter(lambda x: x.startswith(prefix), set(choices))))

    def status(self, things, full=False, show_hosts=False,
               with_dependencies=False, concurrency=None, expand_services=True,
               container_filter=None, ship_filter=None, **kwargs):
        """Display the status of the given services and containers, but only
        looking at the container's state, not the application availability.

        Args:
            things (set<string>): The list of things to start.
            full (boolean): Whether to display the full detailed status, with
                port states, for each container.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._ordered_containers(things, expand_services) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)

        if full:
            plays.FullStatus(containers, show_hosts).run()
        else:
            plays.Status(containers, concurrency, show_hosts).run()

    def pull(self, things, with_dependencies=False,
             ignore_dependencies=False, concurrency=None, expand_services=True,
             container_filter=None, ship_filter=None, **kwargs):
        """Force an image pull to refresh images for the given services and
        containers. Dependencies of the requested containers and services are
        pulled first.

        Args:
            things (set<string>): The list of things to pull.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._ordered_containers(things, expand_services) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)

        plays.Pull(containers, self.registries, ignore_dependencies,
                   concurrency, auditor=self.auditor).run()

    def start(self, things, refresh_images=False, with_dependencies=False,
              ignore_dependencies=False, concurrency=None, reuse=False,
              expand_services=True, container_filter=None, ship_filter=None,
              **kwargs):
        """Start the given container(s) and services(s). Dependencies of the
        requested containers and services are started first.

        Args:
            things (set<string>): The list of things to start.
            refresh_images (boolean): Whether to force an image pull for each
                container or not.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            reuse (boolean): Restart the existing container instead of
                destroying/recreating a new one.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._ordered_containers(things, expand_services) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)

        plays.Start(containers, self.registries, refresh_images,
                    ignore_dependencies, concurrency, reuse,
                    auditor=self.auditor).run()

    def restart(self, things, refresh_images=False, with_dependencies=False,
                ignore_dependencies=False, concurrency=None, step_delay=0,
                stop_start_delay=0, reuse=False, only_if_changed=False,
                expand_services=False, container_filter=None, ship_filter=None,
                **kwargs):
        """Restart the given container(s) and services(s). Dependencies of the
        requested containers and services are started first.

        Args:
            things (set<string>): The list of things to start.
            refresh_images (boolean): Whether to force an image pull for each
                container or not before starting it.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            step_delay (int): Time, in seconds, to wait before restarting the
                next container.
            stop_start_delay (int): Time, in seconds, to wait between stopping
                and starting each container.
            reuse (boolean): Restart the existing container instead of
                destroying/recreating a new one.
            only_if_changed (boolean): Only restart the container if its
                underlying image was updated.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._ordered_containers(
                things, expand_services, forward=False) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)
        plays.Restart(containers, self.registries, refresh_images,
                      ignore_dependencies, concurrency, step_delay,
                      stop_start_delay, reuse, only_if_changed,
                      auditor=self.auditor).run()

    def stop(self, things, with_dependencies=False, ignore_dependencies=False,
             concurrency=None, expand_services=False, container_filter=None,
             ship_filter=None, **kwargs):

        """Stop the given container(s) and service(s).

        This one is a bit more tricky because we don't want to look at the
        dependencies of the containers and services we want to stop, but at
        which services depend on the containers and services we want to stop.
        Unless of course the only parameter is set to True.

        Args:
            things (set<string>): The list of things to stop.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._ordered_containers(
                things, expand_services, forward=False) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)
        plays.Stop(containers, ignore_dependencies,
                   concurrency, auditor=self.auditor).run()

    def kill(self, things, with_dependencies=False, ignore_dependencies=False,
             concurrency=None, expand_services=False, container_filter=None,
             ship_filter=None, **kwargs):
        """Kill the given container(s) and service(s).

        This one is a bit more tricky because we don't want to look at the
        dependencies of the containers and services we want to stop, but at
        which services depend on the containers and services we want to stop.
        Unless of course the only parameter is set to True.

        Args:
            things (set<string>): The list of things to stop.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            ignore_dependencies (boolean): Whether dependency order should be
                respected.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
        """
        containers = self._ordered_containers(
                things, expand_services, forward=False) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)
        plays.Kill(containers, ignore_dependencies,
                   concurrency, auditor=self.auditor).run()

    def clean(self, things, with_dependencies=False, concurrency=None,
              expand_services=True, container_filter=None, ship_filter=None,
              **kwargs):
        """Remove the given stopped Docker containers.

        Args:
            things (set<string>): The list of things to clean.
            with_dependencies (boolean): Whether to act on only the specified
                things, or their dependencies as well.
            concurrency (int): The maximum number of instances that can be
                acted on at the same time.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._ordered_containers(things, expand_services) \
            if with_dependencies \
            else self._to_containers(things, expand_services, container_filter,
                                     ship_filter)
        plays.Clean(containers, concurrency, auditor=self.auditor).run()

    def logs(self, things, follow, n, expand_services=True,
             container_filter=None, ship_filter=None, **kwargs):
        """Display the logs of the given container.

        Args:
            things (set<string>): A one-element set containing the name of the
                container to show logs from.
            follow (boolean): Whether to follow (tail) the log.
            n (int): Number of lines to display (when not following), from the
                bottom of the log.
            expand_services (boolean): whether to allow expanding service names
                to their container instance names. If False and a service name
                is encountered, the method will throw an exception.
            container_filter (string): optional pattern to filter container
                names
            ship_filter (string): optional pattern to filter containers on
                 ship names
        """
        containers = self._to_containers(things, expand_services,
                                         container_filter, ship_filter)
        if len(containers) != 1:
            raise exceptions.ParameterException(
                'Logs can only be shown for a single container!')

        container = containers[0]

        o = termoutput.OutputFormatter()
        o.pending('Inspecting container status...')
        status = container.status()
        if not status:
            o.commit(termoutput.red('{} is not running!'.format(container)))
            return

        stream = follow and status['State']['Running']
        if stream:
            o.pending(
                'Now streaming logs for {}. New output will appear below.'
                .format(container.name))

            while True:
                logs = container.ship.backend.attach(container.id, stream=True)
                o.pending('\033[2K')
                for line in logs:
                    print(line.rstrip())

                o.pending('{} has died, waiting for respawn...'
                          .format(container.name))

                events = container.ship.backend.events(decode=True)
                for e in events:
                    if e['status'] == 'start':
                        spec = container.ship.backend.inspect_container(
                            e['id'])
                        if spec['Name'][1:] == container.name:
                            container.id = e['id']
                            break
        else:
            o.pending(
                'Requesting logs for {}. This may take a while...'
                .format(container.name))
            logs = container.ship.backend.logs(
                container.id, tail=n).decode('utf-8').splitlines()

            o.pending('\033[2K')
            for line in logs:
                print(line.rstrip())

    def deptree(self, things, recursive, **kwargs):
        """Display the dependency tree of the given services."""

        def treehelper(service, indent, shown):
            deps = sorted(service.dependencies) if recursive \
                else sorted(service.dependencies.difference(shown))
            shown.update(deps)
            for i, dep in enumerate(deps, 1):
                last = i == len(deps)
                print('{}{} {}'.format(indent,
                                       last and '\\-' or '+-',
                                       dep.name))
                treehelper(dep, indent + (last and '  ' or '|  '), shown)

        services = self._to_services(things or sorted(self.services))
        for i, service in enumerate(services, 1):
            print(service.name)
            treehelper(service, ' ', set([]))
            if i < len(services):
                print()
