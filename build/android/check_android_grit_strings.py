#!/usr/bin/env vpython
# Copyright 2019 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Check .grd and BUILD.gn files for proper Android localized resource outputs.

This script is used to check, and potentially fix, the list of Android
localized string resource files that are provided in GRIT input files (.grd)
and BUILD.gn files. In particular, it checks that:

- Android localized string resource .xml files are generated by GRIT for all
  supported Chrome locales. These corresponds to <output> elements that
  use the type="android" attribute.

- That the output lists in BUILD.gn files also list resources for all
  supported Chrome locales.

The --scan-dir <dir> option can be used to check for all files under a specific
directory, and the --fix-inplace option can be used to try fixing any file
that doesn't pass the check.

This can be very handy to avoid tedious and repetitive work when adding new
translations / locales to the Chrome code base, since this script can update
said input files for you.

Important note: checks and fix may fail on some input files. For example
remoting/resources/remoting_strings.grd contains an in-line comment element
inside its <outputs> section that breaks the script. The check will fail, and
trying to fix it too, but at least the file will not be modified.
"""

import argparse
import os
import re
import sys

_SCRIPT_DIR = os.path.dirname(__file__)

# Assume this script is under build/android/
_TOP_SRC_DIR = os.path.join(_SCRIPT_DIR, '..', '..')

# Need to import gyp/util/resource_utils.py here.
sys.path.insert(0, os.path.join(_SCRIPT_DIR, 'gyp'))

from util import build_utils
from util import resource_utils

# List of all Chromium locale names. These should match the definition of
# the |locales| variable in build/config/locales.gni for Android.
_CHROMIUM_LOCALES = [
    'am',
    'ar',
    'bg',
    'bn',
    'ca',
    'cs',
    'da',
    'de',
    'el',
    'en-GB',
    'en-US',
    'es',
    'es-419',
    'et',
    'fa',
    'fi',
    'fil',
    'fr',
    'gu',
    'he',
    'hi',
    'hr',
    'hu',
    'id',
    'it',
    'ja',
    'kn',
    'ko',
    'lt',
    'lv',
    'ml',
    'mr',
    'ms',
    'nb',
    'nl',
    'pl',
    'pt-BR',
    'pt-PT',
    'ro',
    'ru',
    'sk',
    'sl',
    'sr',
    'sv',
    'sw',
    'ta',
    'te',
    'th',
    'tr',
    'uk',
    'vi',
    'zh-CN',
    'zh-TW',
]

# This locale is the default and doesn't have translations.
_DEFAULT_LOCALE = 'en-US'

# Misc regular expressions used to match elements and their attributes.
_RE_OUTPUT_ELEMENT = re.compile(r'<output (.*)/>')
_RE_FILENAME_ATTRIBUTE = re.compile(r'filename="([^"]*)"')
_RE_LANG_ATTRIBUTE = re.compile(r'lang="([^"]*)"')
_RE_TYPE_ANDROID_ATTRIBUTE = re.compile(r'type="android"')

_RE_GN_VALUES_LIST_LINE = re.compile(
    r'^\s*".*values(\-([A-Za-z0-9-]+))?/.*\.xml",\s*$')

# Misc terminal codes to provide human friendly progress output.
_CONSOLE_CODE_MOVE_CURSOR_TO_COLUMN_0 = '\x1b[0G'
_CONSOLE_CODE_ERASE_LINE = '\x1b[K'
_CONSOLE_START_LINE = (
    _CONSOLE_CODE_MOVE_CURSOR_TO_COLUMN_0 + _CONSOLE_CODE_ERASE_LINE)

# Mapping lang attribute names as they appear in .grd files into the
# corresponding Chromium locale name.
_CHROMIUM_LANG_FIXES = {
    'en': 'en-US',  # For now, Chromium doesn't have an 'en' locale.
    'iw': 'he',  # 'iw' is the obsolete form of ISO 639-1 for Hebrew
    'no': 'nb',  # 'no' is used by the Translation Console for Norwegian (nb).
}


def _FixChromiumLangAttribute(lang):
  """Map XML "lang" attribute values to Chromium locale names."""
  return _CHROMIUM_LANG_FIXES.get(lang, lang)


def _BuildIntervalList(input_list, predicate):
  """Find ranges of contiguous list items that pass a given predicate.

  Args:
    input_list: An input list of items of any type.
    predicate: A function that takes a list item and return True if it
      passes a given test.
  Returns:
    A list of (start_pos, end_pos) tuples, where all items in
    [start_pos, end_pos) pass the predicate.
  """
  result = []
  size = len(input_list)
  start = 0
  while True:
    # Find first item in list that passes the predicate.
    while start < size and not predicate(input_list[start]):
      start += 1

    if start >= size:
      return result

    # Find first item in the rest of the list that does not pass the
    # predicate.
    end = start + 1
    while end < size and predicate(input_list[end]):
      end += 1

    result.append((start, end))
    start = end + 1


def _SortListSubRange(input_list, start, end, key_func):
  """Sort an input list's sub-range according to a specific key function.

  Args:
    input_list: An input list.
    start: Sub-range starting position in list.
    end: Sub-range limit position in list.
    key_func: A function that extracts a sort key from a line.
  Returns:
    A copy of |input_list|, with all items in [|start|, |end|) sorted
    according to |key_func|.
  """
  result = input_list[:start]
  inputs = []
  for pos in xrange(start, end):
    line = input_list[pos]
    key = key_func(line)
    inputs.append((key, line))

  for _, line in sorted(inputs):
    result.append(line)

  result += input_list[end:]
  return result


# Technical note:
#
# Even though .grd files are XML, an xml parser library is not used in order
# to preserve the original file's structure after modification. ElementTree
# tends to re-order attributes in each element when re-writing an XML
# document tree, which is undesirable here.
#
# Thus simple line-based regular expression matching is used instead.
#


def _IsAndroidGrdOutputLine(line):
  """Returns True iff this is an Android-specific <output> line."""
  m = _RE_OUTPUT_ELEMENT.search(line)
  if m:
    return 'type="android"' in m.group(1)
  return False


def _CheckGrdOutputElementRangeForAndroid(grd_lines, start, end,
                                          wanted_locales):
  """Check all <output> elements in specific input .grd lines range.

  This really checks the following:
    - Each item has a correct 'lang' attribute.
    - There are no duplicated lines for the same 'lang' attribute.
    - Filenames are well-formed.
    - That there are no extra locales that Chromium doesn't want.
    - That no wanted locale is missing.

  Args:
    grd_lines: Input .grd lines.
    start: Sub-range start position in input line list.
    end: Sub-range limit position in input line list.
    wanted_locales: Set of wanted Chromium locale names.
  Returns:
    List of error message strings for this input. Empty on success.
  """
  errors = []
  locales = set()
  for pos in xrange(start, end):
    line = grd_lines[pos]
    m = _RE_LANG_ATTRIBUTE.search(line)
    if not m:
      errors.append('%d: Missing "lang" attribute in <output> element' % pos +
                    1)
      continue
    lang = m.group(1)
    cr_locale = _FixChromiumLangAttribute(lang)
    if cr_locale in locales:
      errors.append(
          '%d: Redefinition of <output> for "%s" locale' % (pos + 1, lang))
    locales.add(cr_locale)

    m = _RE_FILENAME_ATTRIBUTE.search(line)
    if not m:
      errors.append('%d: Missing filename attribute in <output> element' % pos +
                    1)
    else:
      filename = m.group(1)
      if not filename.endswith('.xml'):
        errors.append(
            '%d: Filename should end with ".xml": %s' % (pos + 1, filename))

      dirname = os.path.basename(os.path.dirname(filename))
      prefix = ('values-%s' % resource_utils.ToAndroidLocaleName(cr_locale)
                if cr_locale != _DEFAULT_LOCALE else 'values')
      if dirname != prefix:
        errors.append(
            '%s: Directory name should be %s: %s' % (pos + 1, prefix, filename))

  extra_locales = locales.difference(wanted_locales)
  if extra_locales:
    errors.append('%d-%d: Extra locales found: %s' % (start + 1, end + 1,
                                                      sorted(extra_locales)))

  missing_locales = wanted_locales.difference(locales)
  if missing_locales:
    errors.append('%d-%d: Missing locales: %s' % (start + 1, end + 1,
                                                  sorted(missing_locales)))

  return errors


def _CheckGrdOutputElementsForAndroid(grd_lines, wanted_locales):
  """Check all <output> elements related to Android.

  Args:
    grd_lines: List of input .grd lines.
    wanted_locales: set of wanted Chromium locale names.
  Returns:
    List of error message strings. Empty on success.
  """
  intervals = _BuildIntervalList(grd_lines, _IsAndroidGrdOutputLine)
  errors = []
  for start, end in intervals:
    errors += _CheckGrdOutputElementRangeForAndroid(grd_lines, start, end,
                                                    wanted_locales)
  return errors


def _SortGrdOutputElementsRanges(grd_lines):
  """Sort all <output> elements in a list of lines by lang attribute.

  TODO(digit): Handle comment lines in the middle of <output> ranges!

  Args:
    grd_lines: input lines.
  Returns:
    A new list of input lines, with lines [start..end) sorted.
  """
  intervals = _BuildIntervalList(grd_lines,
                                 lambda x: _RE_OUTPUT_ELEMENT.search(x) != None)
  for start, end in intervals:
    grd_lines = _SortListSubRange(
        grd_lines, start, end, lambda x: _RE_LANG_ATTRIBUTE.search(x).group(1))

  return grd_lines


def _GetAndroidGnOutputLocale(line):
  """Check a GN list, and return its Android locale if it is an output .xml"""
  m = _RE_GN_VALUES_LIST_LINE.match(line)
  if not m:
    return None

  if m.group(1):  # First group is optional and contains group 2.
    return m.group(2)

  return resource_utils.ToAndroidLocaleName(_DEFAULT_LOCALE)


def _IsAndroidGnOutputLine(line):
  """Returns True iff this is an Android-specific localized .xml output."""
  return _GetAndroidGnOutputLocale(line) != None


def _CheckGnOutputsRangeForLocalizedStrings(gn_lines, start, end):
  """Check that a range of GN lines corresponds to localized strings.

  Special case: Some BUILD.gn files list several non-localized .xml files
  that should be ignored by this function, e.g. in
  components/cronet/android/BUILD.gn, the following appears:

    inputs = [
      ...
      "sample/res/layout/activity_main.xml",
      "sample/res/layout/dialog_url.xml",
      "sample/res/values/dimens.xml",
      "sample/res/values/strings.xml",
      ...
    ]

  These are non-localized strings, and should be ignored. This function is
  used to detect them quickly.
  """
  for pos in xrange(start, end):
    if not 'values/' in gn_lines[pos]:
      return True
  return False


def _CheckGnOutputsRange(gn_lines, start, end, wanted_locales):
  if not _CheckGnOutputsRangeForLocalizedStrings(gn_lines, start, end):
    return []

  errors = []
  locales = set()
  for pos in xrange(start, end):
    line = gn_lines[pos]
    android_locale = _GetAndroidGnOutputLocale(line)
    assert android_locale != None
    cr_locale = resource_utils.ToChromiumLocaleName(android_locale)
    if cr_locale in locales:
      errors.append('%s: Redefinition of output for "%s" locale' %
                    (pos + 1, android_locale))
    locales.add(cr_locale)

  extra_locales = locales.difference(wanted_locales)
  if extra_locales:
    errors.append('%d-%d: Extra locales: %s' % (start + 1, end + 1,
                                                sorted(extra_locales)))

  missing_locales = wanted_locales.difference(locales)
  if missing_locales:
    errors.append('%d-%d: Missing locales: %s' % (start + 1, end + 1,
                                                  sorted(missing_locales)))

  return errors


def _CheckGnOutputs(gn_lines, wanted_locales):
  intervals = _BuildIntervalList(gn_lines, _IsAndroidGnOutputLine)
  errors = []
  for start, end in intervals:
    errors += _CheckGnOutputsRange(gn_lines, start, end, wanted_locales)
  return errors


def _AddMissingLocalesInGnOutputs(gn_lines, wanted_locales):
  intervals = _BuildIntervalList(gn_lines, _IsAndroidGnOutputLine)
  # NOTE: Since this may insert new lines to each interval, process the
  # list in reverse order to maintain valid (start,end) positions during
  # the iteration.
  for start, end in reversed(intervals):
    if not _CheckGnOutputsRangeForLocalizedStrings(gn_lines, start, end):
      continue

    locales = set()
    for pos in xrange(start, end):
      lang = _GetAndroidGnOutputLocale(gn_lines[pos])
      locale = resource_utils.ToChromiumLocaleName(lang)
      locales.add(locale)

    missing_locales = wanted_locales.difference(locales)
    if not missing_locales:
      continue

    src_locale = 'bg'
    src_values = 'values-%s/' % resource_utils.ToAndroidLocaleName(src_locale)
    src_line = None
    for pos in xrange(start, end):
      if src_values in gn_lines[pos]:
        src_line = gn_lines[pos]
        break

    if not src_line:
      raise Exception(
          'Cannot find output list item with "%s" locale' % src_locale)

    line_count = end - 1
    for locale in missing_locales:
      if locale == _DEFAULT_LOCALE:
        dst_line = src_line.replace('values-%s/' % src_locale, 'values/')
      else:
        dst_line = src_line.replace(
            'values-%s/' % src_locale,
            'values-%s/' % resource_utils.ToAndroidLocaleName(locale))
      gn_lines.insert(line_count, dst_line)
      line_count += 1

    gn_lines = _SortListSubRange(
        gn_lines, start, line_count,
        lambda line: _RE_GN_VALUES_LIST_LINE.match(line).group(1))

  return gn_lines


def _AddMissingLocalesInGrdOutputs(grd_lines, wanted_locales):
  """Fix an input .grd line by adding missing Android outputs.

  Args:
    grd_lines: Input .grd line list.
    wanted_locales: set of Chromium locale names.
  Returns:
    A new list of .grd lines, containing new <output> elements when needed
    for locales from |wanted_locales| that were not part of the input.
  """
  intervals = _BuildIntervalList(grd_lines, _IsAndroidGrdOutputLine)
  for start, end in reversed(intervals):
    locales = set()
    for pos in xrange(start, end):
      lang = _RE_LANG_ATTRIBUTE.search(grd_lines[pos]).group(1)
      locale = _FixChromiumLangAttribute(lang)
      locales.add(locale)

    missing_locales = wanted_locales.difference(locales)
    if not missing_locales:
      continue

    src_locale = 'bg'
    src_lang_attribute = 'lang="%s"' % src_locale
    src_line = None
    for pos in xrange(start, end):
      if src_lang_attribute in grd_lines[pos]:
        src_line = grd_lines[pos]
        break

    if not src_line:
      raise Exception(
          'Cannot find <output> element with "%s" lang attribute' % src_locale)

    line_count = end - 1
    for locale in missing_locales:
      dst_line = src_line.replace(
          'lang="%s"' % src_locale, 'lang="%s"' % locale).replace(
              'values-%s/' % src_locale, 'values-%s/' % locale)
      grd_lines.insert(line_count, dst_line)
      line_count += 1

  return _SortGrdOutputElementsRanges(grd_lines)


def _IsGritInputFile(input_file):
  """Returns True iff this is a GRIT input file."""
  return input_file.endswith('.grd')


def _IsBuildGnInputFile(input_file):
  """Returns True iff this is a BUILD.gn file."""
  return os.path.basename(input_file) == 'BUILD.gn'


def _ProcessFile(input_file, locales, fix_inplace):
  """Process a given input file.

  Args:
    input_file: Input file path.
    locales: Set of wanted Chromium locale names.
    fix_inplace: Flag. If True, try to fix the input file if it does not
      pass the check.
  Returns:
    True iff the file was processed, False if the file type is not supported
    by this script (i.e. not a .grd or BUILD.gn file).
  """
  if _IsGritInputFile(input_file):
    check_func = _CheckGrdOutputElementsForAndroid
    fix_func = _AddMissingLocalesInGrdOutputs
  elif _IsBuildGnInputFile(input_file):
    check_func = _CheckGnOutputs
    fix_func = _AddMissingLocalesInGnOutputs
  else:
    return False

  print '%sProcessing %s...' % (_CONSOLE_START_LINE, input_file),
  sys.stdout.flush()
  with open(input_file) as f:
    input_lines = f.readlines()
  errors = check_func(input_lines, locales)
  if errors:
    print '\n%s%s' % (_CONSOLE_START_LINE, '\n'.join(errors))
    if fix_inplace:
      try:
        input_lines = fix_func(input_lines, locales)
        output = ''.join(input_lines)
        with open(input_file, 'wt') as f:
          f.write(output)
        print 'Fixed %s.' % input_file
      except Exception as e:  # pylint: disable=broad-except
        print 'Skipped %s: %s' % (input_file, e)

  return True


def main(args):
  parser = argparse.ArgumentParser(
      description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

  parser.add_argument(
      '--scan-dir',
      action='append',
      help='Optional directory to scan for input files '
      'recursively.')

  parser.add_argument(
      '--locales', help='Optional GN-list of all Chromium locale names.')

  parser.add_argument(
      '--fix-inplace',
      action='store_true',
      help='If used, try to fix broken files in-place.')

  parser.add_argument(
      'input_file', nargs='?', action='append', help='Input file(s) to check.')

  options = parser.parse_args(args)

  locales = set(_CHROMIUM_LOCALES)
  if options.locales:
    locales = set(build_utils.ParseGnList(options.locales))

  for input_file in options.input_file:
    if input_file and not _ProcessFile(input_file, locales,
                                       options.fix_inplace):
      print 'Unknown file type: %s' % input_file

  if options.scan_dir:
    for src_dir in options.scan_dir:
      for root, _, files in os.walk(src_dir):
        for f in files:
          input_file = os.path.join(root, f)
          _ProcessFile(input_file, locales, options.fix_inplace)

  print '%sDone.' % (_CONSOLE_START_LINE)


if __name__ == "__main__":
  main(sys.argv[1:])
