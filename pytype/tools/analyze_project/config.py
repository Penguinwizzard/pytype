"""Config file processing."""

import dataclasses
import logging
import os
import sys
import textwrap
from typing import Any, Callable, Optional

from pytype import config as pytype_config
from pytype import file_utils
from pytype import utils
from pytype.platform_utils import path_utils
from pytype.tools import config


# Args:
#   flag: the name of the command-line flag.
#   to_command_line: a function to transform the value into a command-line arg.
#     If None, the value will be used directly. The result will be interpreted
#     as follows:
#     - If a bool, it will determine whether the flag is included.
#     - Elif it is empty, the flag will be omitted.
#     - Else, it will be considered the flag's command-line value.
@dataclasses.dataclass(eq=True, frozen=True)
class ArgInfo:
  flag: str
  to_command_line: Optional[Callable[[Any], Any]]


# A config item.
# Args:
#   default: the default value.
#   sample: a sample value.
#   arg_info: information about the corresponding command-line argument.
#   comment: help text.
@dataclasses.dataclass(eq=True, frozen=True)
class Item:
  default: Any
  sample: Any
  arg_info: Optional[ArgInfo]
  comment: Optional[str]


# Generates both the default config and the sample config file. These items
# don't have ArgInfo populated, as it is needed only for pytype-single args.
ITEMS = {
    'exclude': Item(
        '', '**/*_test.py **/test_*.py', None,
        'Space-separated list of files or directories to exclude.'),
    'inputs': Item(
        '', '.', None,
        'Space-separated list of files or directories to process.'),
    'keep_going': Item(
        False, 'False', None,
        'Keep going past errors to analyze as many files as possible.'),
    'jobs': Item(
        1, '4', None,
        "Run N jobs in parallel. When 'auto' is used, this will be equivalent "
        'to the number of CPUs on the host system.'),
    'output': Item(
        '.pytype', '.pytype', None, 'All pytype output goes here.'),
    'platform': Item(
        '', sys.platform, None,
        'Platform (e.g., "linux", "win32") that the target code runs on.'),
    'pythonpath': Item(
        '', '.', None,
        f'Paths to source code directories, separated by {os.pathsep!r}.'),
    'python_version': Item(
        '', '{}.{}'.format(*sys.version_info[:2]),
        None, 'Python version (major.minor) of the target code.'),
}


REPORT_ERRORS_ITEMS = {
    'disable': Item(
        None, 'pyi-error', ArgInfo('--disable', ','.join),
        'Comma or space separated list of error names to ignore.'),
    'report_errors': Item(
        None, 'True', ArgInfo('--no-report-errors', lambda v: not v), None),
}


# The missing fields will be filled in by generate_sample_config_or_die.
def _pytype_single_items():
  """Args to pass through to pytype_single."""
  out = {}
  flags = pytype_config.FEATURE_FLAGS + pytype_config.EXPERIMENTAL_FLAGS
  for opt, default, _ in flags:
    dest = opt.lstrip('-').replace('-', '_')
    default = str(default)
    out[dest] = Item(None, default, ArgInfo(opt, None), None)
  out.update(REPORT_ERRORS_ITEMS)
  return out


_PYTYPE_SINGLE_ITEMS = _pytype_single_items()


def get_pytype_single_item(name):
  # We want to avoid exposing this hard-coded list as much as possible so that
  # parser.pytype_single_args, which is guaranteed to match the actual args, is
  # used instead.
  return _PYTYPE_SINGLE_ITEMS[name]


def string_to_bool(s):
  return s == 'True' if s in ('True', 'False') else s


def concat_disabled_rules(s):
  return ','.join(t for t in s.split() if t)


def get_platform(p):
  return p or sys.platform


def get_python_version(v):
  return v or utils.format_version(sys.version_info[:2])


def parse_jobs(s):
  """Parse the --jobs option."""
  if s == 'auto':
    try:
      n = len(os.sched_getaffinity(0))  # pytype: disable=module-attr
    except AttributeError:
      n = os.cpu_count()
    return n or 1
  else:
    return int(s)


def make_converters(cwd=None):
  """For items that need coaxing into their internal representations."""
  return {
      'disable': concat_disabled_rules,
      'exclude': lambda v: file_utils.expand_source_files(v, cwd),
      'inputs': lambda v: file_utils.expand_source_files(v, cwd),
      'jobs': parse_jobs,
      'keep_going': string_to_bool,
      'output': lambda v: file_utils.expand_path(v, cwd),
      'platform': get_platform,
      'python_version': get_python_version,
      'pythonpath': lambda v: file_utils.expand_pythonpath(v, cwd),
  }


def _make_spaced_path_formatter(name):
  """Formatter for space-separated paths."""
  def format_spaced_path(p):
    out = []
    out.append(f'{name} =')
    out.extend(f'    {entry}' for entry in p.split())
    return out
  return format_spaced_path


def _make_separated_path_formatter(name, sep):
  """Formatter for paths separated by a non-space token."""
  def format_separated_path(p):
    out = []
    out.append(f'{name} =')
    # Breaks the path after each instance of sep.
    for entry in p.replace(sep, sep + '\n').split('\n'):
      out.append(f'    {entry}')
    return out
  return format_separated_path


def make_formatters():
  return {
      'disable': _make_separated_path_formatter('disable', ','),
      'exclude': _make_spaced_path_formatter('exclude'),
      'inputs': _make_spaced_path_formatter('inputs'),
      'pythonpath': _make_separated_path_formatter('pythonpath', os.pathsep),
  }


def Config(*extra_variables):  # pylint: disable=invalid-name
  """Builds a Config class and returns an instance of it."""

  class Config:  # pylint: disable=redefined-outer-name
    """Configuration variables.

    A lightweight configuration class that reads in attributes from other
    objects and prettyprints itself. The intention is for each source of
    attributes (e.g., FileConfig) to do its own processing, then for Config to
    copy in the final results in the right order.
    """

    __slots__ = tuple(ITEMS) + extra_variables

    def populate_from(self, obj):
      """Populate self from another object's attributes."""
      for k in self.__slots__:
        if hasattr(obj, k):
          setattr(self, k, getattr(obj, k))

    def __str__(self):
      return '\n'.join(
          f'{k} = {getattr(self, k, None)!r}' for k in self.__slots__)

  return Config()


class FileConfig:
  """Configuration variables from a file."""

  def read_from_file(self, filepath):
    """Read config from an INI-style file with a [pytype] section."""

    cfg = config.ConfigSection.create_from_file(filepath, 'pytype')
    if not cfg:
      return None
    converters = make_converters(cwd=path_utils.dirname(filepath))
    for k, v in cfg.items():
      if k in converters:
        v = converters[k](v)
      setattr(self, k, v)
    return filepath


def generate_sample_config_or_die(filename, pytype_single_args):
  """Write out a sample config file."""

  if path_utils.exists(filename):
    logging.critical('Not overwriting existing file: %s', filename)
    sys.exit(1)

  # Combine all arguments into one name -> Item dictionary.
  items = dict(ITEMS)
  assert set(_PYTYPE_SINGLE_ITEMS) == set(pytype_single_args)
  for key, item in _PYTYPE_SINGLE_ITEMS.items():
    val = pytype_single_args[key]
    if item.comment is None:
      items[key] = dataclasses.replace(
          item, default=val.default, comment=val.help)
    else:
      items[key] = dataclasses.replace(item, default=val.default)

  # Not using configparser's write method because it doesn't support comments.

  conf = [
      '# NOTE: All relative paths are relative to the location of this file.',
      '',
      '[pytype]',
      '',
  ]
  formatters = make_formatters()
  for key, item in items.items():
    conf.extend(textwrap.wrap(
        item.comment, 80, initial_indent='# ', subsequent_indent='# '))
    if key in formatters:
      conf.extend(formatters[key](item.sample))
    else:
      conf.append(f'{key} = {item.sample}')
    conf.append('')
  try:
    with open(filename, 'w') as f:
      f.write('\n'.join(conf))
  except OSError as e:
    logging.critical('Cannot write to %s:\n%s', filename, str(e))
    sys.exit(1)


def read_config_file_or_die(filepath):
  """Read config from filepath or from setup.cfg."""

  ret = FileConfig()
  if filepath:
    if not ret.read_from_file(filepath):
      logging.critical('Could not read config file: %s\n'
                       '  Generate a sample configuration via:\n'
                       '  pytype --generate-config sample.cfg', filepath)
      sys.exit(1)
  else:
    # Try reading from setup.cfg.
    filepath = config.find_config_file(path_utils.getcwd())
    if filepath and ret.read_from_file(filepath):
      logging.info('Reading config from: %s', filepath)
    else:
      logging.info('No config file specified, and no [pytype] section in '
                   'setup.cfg. Using default configuration.')
  return ret
