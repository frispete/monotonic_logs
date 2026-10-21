"""Unit tests for monotonic_logs.py (run from the repo root):

    python3 -m unittest discover -s tests -t . -v
"""
#
# vim:set et ts=8 sw=4:
#

import io
import os
import shutil
import tempfile
import unittest
import contextlib

from datetime import datetime, timedelta, timezone
from unittest import mock

import monotonic_logs as ml
from monotonic_logs import gpar


TESTDATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'testdata')


def data(name):
    return os.path.join(TESTDATA, name)


class GparMixin:
    """Save and restore the global parameters around each test"""
    GPAR_ATTRS = ('loglevel', 'logfile', 'against_max', 'show_prev', 'threshold',
                  'year')

    def setUp(self):
        self._gpar = dict((a, getattr(gpar, a)) for a in self.GPAR_ATTRS)

    def tearDown(self):
        for a, v in self._gpar.items():
            setattr(gpar, a, v)


class CheckMixin(GparMixin):
    """Run check_file() with captured output, warnings go to self.warnings"""

    def check(self, name, **opts):
        """Run check_file(), return (count, list of hits as (lineno, delta))"""
        for k, v in opts.items():
            setattr(gpar, k, v)
        path = name if os.path.isabs(name) else data(name)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            count = ml.check_file(path)
        self.warnings = err.getvalue().splitlines()
        hits = []
        for line in out.getvalue().splitlines():
            if not line:
                continue
            loc, delta, _ = line.split('\t', 2)
            self.assertTrue(loc.startswith(path + ':'), loc)
            hits.append((int(loc[len(path) + 1:]), delta.strip()))
        return count, hits


class TestParseIso(unittest.TestCase):

    def parse(self, s):
        m = ml.ISO_RE.match(s)
        self.assertIsNotNone(m, s)
        return ml.parse_iso(m)

    def test_offset(self):
        ts = self.parse('2024-01-15T10:23:45.123456+01:00')
        self.assertEqual(ts, datetime(2024, 1, 15, 10, 23, 45, 123456,
                                      timezone(timedelta(hours = 1))))

    def test_negative_offset(self):
        ts = self.parse('2024-01-15T03:30:00-05:30')
        self.assertEqual(ts.utcoffset(), -timedelta(hours = 5, minutes = 30))

    def test_compact_offset(self):
        ts = self.parse('2024-01-15T10:00:00+0200')
        self.assertEqual(ts.utcoffset(), timedelta(hours = 2))

    def test_zulu(self):
        ts = self.parse('2024-01-15T10:00:00Z')
        self.assertEqual(ts.tzinfo, timezone.utc)

    def test_naive(self):
        ts = self.parse('2024-01-15T10:00:00 host')
        self.assertIsNone(ts.tzinfo)

    def test_space_and_comma(self):
        ts = self.parse('2024-01-15 10:00:00,5+00:00')
        self.assertEqual(ts.microsecond, 500000)

    def test_fraction_lengths(self):
        for frac, us in (('1', 100000), ('123', 123000), ('123456', 123456),
                         ('123456789', 123456)):
            ts = self.parse('2024-01-15T10:00:00.%s+00:00' % frac)
            self.assertEqual(ts.microsecond, us, frac)

    def test_invalid_date(self):
        m = ml.ISO_RE.match('2024-02-30T10:00:00+00:00')
        self.assertRaises(ValueError, ml.parse_iso, m)


class TestFmtDelta(unittest.TestCase):

    def test_negative(self):
        self.assertEqual(ml.fmt_delta(timedelta(seconds = -62)), '-00:01:02.000000')

    def test_fraction(self):
        self.assertEqual(ml.fmt_delta(timedelta(microseconds = -23456)),
                         '-00:00:00.023456')

    def test_days(self):
        self.assertEqual(ml.fmt_delta(-timedelta(days = 1, hours = 2)),
                         '-1d 02:00:00.000000')

    def test_positive_and_zero(self):
        self.assertEqual(ml.fmt_delta(timedelta(hours = 1)), '+01:00:00.000000')
        self.assertEqual(ml.fmt_delta(timedelta(0)), '+00:00:00.000000')


class TestLogLines(unittest.TestCase):

    def test_header_section(self):
        lines = list(ml.log_lines(data('messages.txt')))
        self.assertEqual(lines[0][0], 9)
        self.assertEqual(lines[-1][0], 17)
        text = '\n'.join(l for _, l in lines)
        self.assertNotIn('ignored', text)
        self.assertNotIn('#==[', text)

    def test_no_header(self):
        lines = list(ml.log_lines(data('messages-20240115.txt')))
        self.assertEqual([n for n, _ in lines], list(range(1, 8)))

    def test_empty(self):
        self.assertEqual(list(ml.log_lines(data('messages-empty.txt'))), [])


class TestCheckFile(CheckMixin, unittest.TestCase):

    def test_header_file(self):
        self.assertEqual(self.check('messages.txt'),
                         (2, [(12, '-00:00:00.500000'), (15, '-00:01:02.000000')]))

    def test_against_max(self):
        self.assertEqual(self.check('messages.txt', against_max = True),
                         (3, [(12, '-00:00:00.500000'), (13, '-00:00:00.200000'),
                              (15, '-00:01:02.000000')]))

    def test_threshold(self):
        self.assertEqual(self.check('messages.txt', threshold = timedelta(seconds = 60)),
                         (1, [(15, '-00:01:02.000000')]))

    def test_show_prev(self):
        gpar.show_prev = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ml.check_file(data('messages.txt'))
        lines = out.getvalue().split('\n')
        prev, hit, blank = lines[:3]
        # continuation line 11 is skipped, so the reference is line 10
        self.assertTrue(prev.endswith('messages.txt:10\t' + ' ' * 16 +
                                      '\t2024-01-15T10:00:01.000000+01:00 host1 kernel: reference'),
                        prev)
        self.assertIn('messages.txt:12\t-00:00:00.500000\t', hit)
        self.assertEqual(blank, '')

    def test_offsets_and_fractions(self):
        self.assertEqual(self.check('messages-20240115.txt'),
                         (2, [(4, '-00:00:00.023456'), (7, '-00:31:00.500000')]))

    def test_syslog_year_change(self):
        self.assertEqual(self.check('messages-20240103.txt'),
                         (1, [(5, '-00:00:01.000000')]))
        self.assertEqual(self.warnings, [])

    def test_syslog_leap_day(self):
        self.assertEqual(self.check('messages-20240302.txt'),
                         (1, [(3, '-1d 00:00:00.000000')]))

    def test_syslog_leap_day_after_year_change(self):
        self.assertEqual(self.check('messages-20240301.txt'),
                         (1, [(4, '-1d 00:00:00.000000')]))
        self.assertEqual(self.warnings, [])

    def test_syslog_invalid_leap_day(self):
        with self.assertRaises(ml.YearError) as cm:
            self.check('messages-20230301.txt')
        self.assertEqual(str(cm.exception), 'line 2: Feb 29 in non-leap year 2023 '
                         'derived from the file name')

    def test_mixed_formats(self):
        # syslog lines take the year from the following RFC3339 timestamp
        self.assertEqual(self.check('messages-mixed.txt'),
                         (1, [(3, '-01:00:05.000000')]))
        self.assertEqual(self.warnings, [])

    def test_mixed_inconsistent_year(self):
        self.assertEqual(self.check('messages-20250115.txt'),
                         (1, [(3, '-01:00:05.000000')]))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('messages-20250115.txt:2: warning: year 2025 derived from '
                      'the file name is inconsistent with the timestamp at line 1, '
                      'using 2024', self.warnings[0])

    def test_monotonic(self):
        self.assertEqual(self.check('messages-monotonic.txt'), (0, []))

    def test_empty(self):
        self.assertEqual(self.check('messages-empty.txt'), (0, []))


class TestYear(CheckMixin, unittest.TestCase):
    """Year sources of traditional syslog timestamps and option -y"""

    def setUp(self):
        CheckMixin.setUp(self)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        CheckMixin.tearDown(self)

    def write(self, name, *lines):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        return path

    def test_fname_date(self):
        self.assertEqual(ml.fname_date('/x/messages-20240115.txt'), datetime(2024, 1, 15))
        self.assertIsNone(ml.fname_date('/x/messages.txt'))
        self.assertIsNone(ml.fname_date('/x/messages-20241301.txt'))
        self.assertIsNone(ml.fname_date('/x/messages-202401150.txt'))

    def test_no_year_source(self):
        path = self.write('messages.txt', 'Jan 15 10:00:00 host1 x')
        with self.assertRaises(ml.YearError):
            self.check(path)

    def test_iso_only_needs_no_year(self):
        path = self.write('messages.txt', '2024-01-15T10:00:00+01:00 host1 x')
        self.assertEqual(self.check(path), (0, []))

    def test_last_entry_after_file_name_date(self):
        # Dec 31 is later than the Jan 1 rotation date, so it is from 2023
        path = self.write('messages-20240101.txt',
                          'Feb 29 12:00:00 host1 leap day in 2023?',
                          'Dec 31 23:00:00 host1 last entry')
        with self.assertRaises(ml.YearError) as cm:
            self.check(path)
        self.assertIn('line 1: Feb 29 in non-leap year 2023', str(cm.exception))

    def test_start_year(self):
        # counted forwards from -y: Feb 29 is in 2024
        lines = ('Dec 31 23:00:00 host1 a', 'Jan  1 01:00:00 host1 b',
                 'Feb 29 12:00:00 host1 c')
        path = self.write('messages.txt', *lines)
        self.assertEqual(self.check(path, year = 2023), (0, []))
        self.assertEqual(self.warnings, [])
        self.assertEqual(self.check(path, year = 2024), (0, []))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('messages.txt:3: warning: Feb 29 in non-leap year 2025 '
                      'derived from option -y, line skipped', self.warnings[0])

    def test_year_overrides_file_name(self):
        self.assertEqual(self.check('messages-20230301.txt', year = 2024), (0, []))
        self.assertEqual(self.warnings, [])

    def test_year_inconsistent_with_iso(self):
        path = self.write('messages.txt', 'Jan 15 10:00:00 host1 a',
                          '2024-01-15T10:00:01+01:00 host1 b')
        self.assertEqual(self.check(path, year = 2023), (0, []))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('messages.txt:2: warning: year 2023 of the preceding syslog '
                      'timestamps derived from option -y is inconsistent',
                      self.warnings[0])

    def test_resync_at_iso(self):
        path = self.write('messages.txt', 'Jan  1 00:00:00 host1 a',
                          '2024-12-31T23:00:00+01:00 host1 b',
                          'Jan  1 00:30:00 host1 c, year change to 2025')
        self.assertEqual(self.check(path, year = 2000), (0, []))
        self.assertEqual(len(self.warnings), 1)

    def test_middle_run_inconsistent(self):
        path = self.write('messages.txt', '2024-01-15T10:00:00+01:00 host1 a',
                          'Jan 15 10:00:05 host1 b',
                          '2025-01-15T10:00:00+01:00 host1 c')
        self.assertEqual(self.check(path), (0, []))
        self.assertEqual(len(self.warnings), 1)
        self.assertIn('messages.txt:3: warning: year 2024 of the preceding syslog '
                      'timestamps derived from the timestamp at line 1', self.warnings[0])


class TestCollectFiles(unittest.TestCase):

    def test_directory(self):
        files = ml.collect_files([TESTDATA])
        self.assertIn(data('messages.txt'), files)
        self.assertEqual(files, sorted(files))

    def test_glob(self):
        self.assertEqual(ml.collect_files([data('messages-2024030*.txt')]),
                         [data('messages-20240301.txt'), data('messages-20240302.txt')])

    def test_unmatched_is_kept(self):
        self.assertEqual(ml.collect_files(['/nonexistent/messages.txt']),
                         ['/nonexistent/messages.txt'])


class TestMain(GparMixin, unittest.TestCase):

    def main(self, *argv):
        """Run main(), return its exit code (from return or SystemExit)"""
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                return ml.main(list(argv))
            except SystemExit as e:
                return e.code

    def test_no_backward_steps(self):
        self.assertEqual(self.main(data('messages-monotonic.txt')), 0)

    def test_backward_steps(self):
        self.assertEqual(self.main(data('messages.txt')), 1)

    def test_threshold_filters_all(self):
        self.assertEqual(self.main('-t', '3600', data('messages.txt')), 0)

    def test_directory(self):
        # messages-20230301.txt cannot be validated without -y
        with self.assertLogs(ml.log, 'ERROR'):
            self.assertEqual(self.main(TESTDATA), 2)
        self.assertEqual(self.main('-y', '2024', TESTDATA), 1)

    def test_unreadable(self):
        with self.assertLogs(ml.log, 'ERROR'):
            self.assertEqual(self.main('/nonexistent/messages.txt'), 2)

    def test_unreadable_wins_over_hits(self):
        with self.assertLogs(ml.log, 'ERROR'):
            self.assertEqual(self.main(data('messages.txt'), '/nonexistent/messages.txt'), 2)

    def test_interrupted(self):
        with mock.patch.object(ml, 'check_file', side_effect = KeyboardInterrupt):
            self.assertEqual(self.main(data('messages.txt')), 3)

    def test_usage_errors(self):
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.main('-x', data('messages.txt')), 1)
        self.assertEqual(self.main('-t', 'abc', data('messages.txt')), 1)
        self.assertEqual(self.main('-y', 'abc', data('messages.txt')), 1)
        self.assertEqual(self.main('-y', '0', data('messages.txt')), 1)

    def test_year_not_validated(self):
        with self.assertLogs(ml.log, 'ERROR') as cm:
            self.assertEqual(self.main(data('messages-20230301.txt')), 2)
        self.assertIn('use -y', cm.output[0])
        self.assertEqual(self.main('--year=2024', data('messages-20230301.txt')), 0)

    def test_help_and_version(self):
        self.assertEqual(self.main('-h'), 0)
        self.assertEqual(self.main('-V'), 0)


if __name__ == '__main__':
    unittest.main()
