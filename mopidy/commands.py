from __future__ import print_function, unicode_literals

import argparse
import collections
import logging
import os
import sys

import glib

import gobject

from mopidy import config as config_lib, exceptions
from mopidy.audio import Audio
from mopidy.core import Core
from mopidy.utils import deps, process, versioning

logger = logging.getLogger(__name__)

_default_config = []
for base in glib.get_system_config_dirs() + (glib.get_user_config_dir(),):
    _default_config.append(os.path.join(base, b'mopidy', b'mopidy.conf'))
DEFAULT_CONFIG = b':'.join(_default_config)


def config_files_type(value):
    return value.split(b':')


def config_override_type(value):
    try:
        section, remainder = value.split(b'/', 1)
        key, value = remainder.split(b'=', 1)
        return (section.strip(), key.strip(), value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            '%s must have the format section/key=value' % value)


class _ParserError(Exception):
    pass


class _HelpError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise _ParserError(message)


class _HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=None, help=None):
        super(_HelpAction, self).__init__(
            option_strings=option_strings,
            dest=dest or argparse.SUPPRESS,
            default=argparse.SUPPRESS,
            nargs=0,
            help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        raise _HelpError()


class Command(object):
    """Command parser and runner for building trees of commands.

    This class provides a wraper around :class:`argparse.ArgumentParser`
    for handling this type of command line application in a better way than
    argprases own sub-parser handling.
    """

    help = None
    #: Help text to display in help output.

    def __init__(self):
        self._children = collections.OrderedDict()
        self._arguments = []
        self._overrides = {}

    def _build(self):
        actions = []
        parser = _ArgumentParser(add_help=False)
        parser.register('action', 'help', _HelpAction)

        for args, kwargs in self._arguments:
            actions.append(parser.add_argument(*args, **kwargs))

        parser.add_argument('_args', nargs=argparse.REMAINDER,
                            help=argparse.SUPPRESS)
        return parser, actions

    def add_child(self, name, command):
        """Add a child parser to consider using.

        :param name: name to use for the sub-command that is being added.
        :type name: string
        """
        self._children[name] = command

    def add_argument(self, *args, **kwargs):
        """Add an argument to the parser.

        This method takes all the same arguments as the
        :class:`argparse.ArgumentParser` version of this method.
        """
        self._arguments.append((args, kwargs))

    def set(self, **kwargs):
        """Override a value in the finaly result of parsing."""
        self._overrides.update(kwargs)

    def exit(self, status_code=0, message=None, usage=None):
        """Optionally print a message and exit."""
        print('\n\n'.join(m for m in (usage, message) if m))
        sys.exit(status_code)

    def format_usage(self, prog=None):
        """Format usage for current parser."""
        actions = self._build()[1]
        prog = prog or os.path.basename(sys.argv[0])
        return self._usage(actions, prog) + '\n'

    def _usage(self, actions, prog):
        formatter = argparse.HelpFormatter(prog)
        formatter.add_usage(None, actions, [])
        return formatter.format_help().strip()

    def format_help(self, prog=None):
        """Format help for current parser and children."""
        actions = self._build()[1]
        prog = prog or os.path.basename(sys.argv[0])

        formatter = argparse.HelpFormatter(prog)
        formatter.add_usage(None, actions, [])

        if self.help:
            formatter.add_text(self.help)

        if actions:
            formatter.add_text('OPTIONS:')
            formatter.start_section(None)
            formatter.add_arguments(actions)
            formatter.end_section()

        subhelp = []
        for name, child in self._children.items():
            child._subhelp(name, subhelp)

        if subhelp:
            formatter.add_text('COMMANDS:')
            subhelp.insert(0, '')

        return formatter.format_help() + '\n'.join(subhelp)

    def _subhelp(self, name, result):
        actions = self._build()[1]

        if self.help or actions:
            formatter = argparse.HelpFormatter(name)
            formatter.add_usage(None, actions, [], '')
            formatter.start_section(None)
            formatter.add_text(self.help)
            formatter.start_section(None)
            formatter.add_arguments(actions)
            formatter.end_section()
            formatter.end_section()
            result.append(formatter.format_help())

        for childname, child in self._children.items():
            child._subhelp(' '.join((name, childname)), result)

    def parse(self, args, prog=None):
        """Parse command line arguments.

        Will recursively parse commands until a final parser is found or an
        error occurs. In the case of errors we will print a message and exit.
        Otherwise, any overrides are applied and the current parser stored
        in the command attribute of the return value.

        :param args: list of arguments to parse
        :type args: list of strings
        :param prog: name to use for program
        :type prog: string
        :rtype: :class:`argparse.Namespace`
        """
        prog = prog or os.path.basename(sys.argv[0])
        try:
            return self._parse(
                args, argparse.Namespace(), self._overrides.copy(), prog)
        except _HelpError:
            self.exit(0, self.format_help(prog))

    def _parse(self, args, namespace, overrides, prog):
        overrides.update(self._overrides)
        parser, actions = self._build()

        try:
            result = parser.parse_args(args, namespace)
        except _ParserError as e:
            self.exit(1, e.message, self._usage(actions, prog))

        if not result._args:
            for attr, value in overrides.items():
                setattr(result, attr, value)
            delattr(result, '_args')
            result.command = self
            return result

        child = result._args.pop(0)
        if child not in self._children:
            usage = self._usage(actions, prog)
            self.exit(1, 'unrecognized command: %s' % child, usage)

        return self._children[child]._parse(
            result._args, result, overrides, ' '.join([prog, child]))

    def run(self, *args, **kwargs):
        """Run the command.

        Must be implemented by sub-classes that are not simply and intermediate
        in the command namespace.
        """
        raise NotImplementedError


class RootCommand(Command):
    def __init__(self):
        super(RootCommand, self).__init__()
        self.set(base_verbosity_level=0)
        self.add_argument(
            '-h', '--help',
            action='help', help='Show this message and exit')
        self.add_argument(
            '--version', action='version',
            version='Mopidy %s' % versioning.get_version())
        self.add_argument(
            '-q', '--quiet',
            action='store_const', const=-1, dest='verbosity_level',
            help='less output (warning level)')
        self.add_argument(
            '-v', '--verbose',
            action='count', dest='verbosity_level', default=0,
            help='more output (repeat up to 3 times for even more)')
        self.add_argument(
            '--save-debug-log',
            action='store_true', dest='save_debug_log',
            help='save debug log to "./mopidy.log"')
        self.add_argument(
            '--config',
            action='store', dest='config_files', type=config_files_type,
            default=DEFAULT_CONFIG, metavar='FILES',
            help='config files to use, colon seperated, later files override')
        self.add_argument(
            '-o', '--option',
            action='append', dest='config_overrides',
            type=config_override_type, metavar='OPTIONS',
            help='`section/key=value` values to override config options')

    def run(self, args, config):
        loop = gobject.MainLoop()

        backend_classes = args.registry['backend']
        frontend_classes = args.registry['frontend']

        try:
            audio = self.start_audio(config)
            backends = self.start_backends(config, backend_classes, audio)
            core = self.start_core(audio, backends)
            self.start_frontends(config, frontend_classes, core)
            loop.run()
        except (exceptions.BackendError,
                exceptions.FrontendError,
                exceptions.MixerError):
            logger.info('Initialization error. Exiting...')
        except KeyboardInterrupt:
            logger.info('Interrupted. Exiting...')
        finally:
            loop.quit()
            self.stop_frontends(frontend_classes)
            self.stop_core()
            self.stop_backends(backend_classes)
            self.stop_audio()
            process.stop_remaining_actors()

    def start_audio(self, config):
        logger.info('Starting Mopidy audio')
        return Audio.start(config=config).proxy()

    def start_backends(self, config, backend_classes, audio):
        logger.info(
            'Starting Mopidy backends: %s',
            ', '.join(b.__name__ for b in backend_classes) or 'none')

        backends = []
        for backend_class in backend_classes:
            try:
                backend = backend_class.start(
                    config=config, audio=audio).proxy()
                backends.append(backend)
            except exceptions.BackendError as exc:
                logger.error(
                    'Backend (%s) initialization error: %s',
                    backend_class.__name__, exc.message)
                raise

        return backends

    def start_core(self, audio, backends):
        logger.info('Starting Mopidy core')
        return Core.start(audio=audio, backends=backends).proxy()

    def start_frontends(self, config, frontend_classes, core):
        logger.info(
            'Starting Mopidy frontends: %s',
            ', '.join(f.__name__ for f in frontend_classes) or 'none')

        for frontend_class in frontend_classes:
            try:
                frontend_class.start(config=config, core=core)
            except exceptions.FrontendError as exc:
                logger.error(
                    'Frontend (%s) initialization error: %s',
                    frontend_class.__name__, exc.message)
                raise

    def stop_frontends(self, frontend_classes):
        logger.info('Stopping Mopidy frontends')
        for frontend_class in frontend_classes:
            process.stop_actors_by_class(frontend_class)

    def stop_core(self):
        logger.info('Stopping Mopidy core')
        process.stop_actors_by_class(Core)

    def stop_backends(self, backend_classes):
        logger.info('Stopping Mopidy backends')
        for backend_class in backend_classes:
            process.stop_actors_by_class(backend_class)

    def stop_audio(self):
        logger.info('Stopping Mopidy audio')
        process.stop_actors_by_class(Audio)


class ConfigCommand(Command):
    help = 'Show currently active configuration.'

    def __init__(self):
        super(ConfigCommand, self).__init__()
        self.set(base_verbosity_level=-1)

    def run(self, config, errors, extensions):
        print(config_lib.format(config, extensions, errors))
        return 0


class DepsCommand(Command):
    help = 'Show dependencies and debug information.'

    def __init__(self):
        super(DepsCommand, self).__init__()
        self.set(base_verbosity_level=-1)

    def run(self):
        print(deps.format_dependency_list())
        return 0
