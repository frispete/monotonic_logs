"""Unit tests for monotonic_logs.py (run from the repo root):

    python3 -m unittest discover -s tests -t . -v
"""
#
# vim:set et ts=8 sw=4:
#

import io
import os
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
    GPAR_ATTRS = ('loglevel', 'logfile', 'against_max', 'show_prev', 'threshold')

    def setUp(self):
        self._gpar = dict((a, getattr(gpar, a)) for a in self.GPAR_ATTRS)

    def tearDown(self):
        for a, v in self._gpar.items():
            setattr(gpar, a, v)


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


class TestCheckFile(GparMixin, unittest.TestCase):

    def check(self, name, **opts):
        """Run check_file(), return (count, list of hits as (lineno, delta))"""
        for k, v in opts.items():
            setattr(gpar, k, v)
        path = data(name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            count = ml.check_file(path)
        hits = []
        for line in out.getvalue().splitlines():
            if not line:
                continue
            loc, delta, _ = line.split('\t', 2)
            self.assertTrue(loc.startswith(path + ':'), loc)
            hits.append((int(loc[len(path) + 1:]), delta.strip()))
        return count, hits

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
        self.assertEqual(self.check('messages-syslog.txt'),
                         (1, [(5, '-00:00:01.000000')]))

    def test_syslog_leap_day(self):
        self.assertEqual(self.check('messages-leap.txt'),
                         (1, [(3, '-1d 00:00:00.000000')]))

    @unittest.expectedFailure
    def test_syslog_leap_day_after_year_change(self):
        # BUG: the year change moves the synthetic year from 2000 to 2001,
        # where "Feb 29" is invalid, so that line is silently skipped.
        self.assertEqual(self.check('messages-leap-yearchange.txt'),
                         (1, [(4, '-1d 00:00:00.000000')]))

    @unittest.expectedFailure
    def test_mixed_formats(self):
        # BUG: syslog timestamps carry the synthetic year 2000 and are
        # compared with real ISO years, giving a bogus ~24 year step.
        self.assertEqual(self.check('messages-mixed.txt'),
                         (1, [(3, '-01:00:05.000000')]))

    def test_monotonic(self):
        self.assertEqual(self.check('messages-monotonic.txt'), (0, []))

    def test_empty(self):
        self.assertEqual(self.check('messages-empty.txt'), (0, []))


class TestCollectFiles(unittest.TestCase):

    def test_directory(self):
        files = ml.collect_files([TESTDATA])
        self.assertIn(data('messages.txt'), files)
        self.assertEqual(files, sorted(files))

    def test_glob(self):
        self.assertEqual(ml.collect_files([data('messages-leap*.txt')]),
                         [data('messages-leap-yearchange.txt'), data('messages-leap.txt')])

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
        self.assertEqual(self.main(TESTDATA), 1)

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

    def test_help_and_version(self):
        self.assertEqual(self.main('-h'), 0)
        self.assertEqual(self.main('-V'), 0)


if __name__ == '__main__':
    unittest.main()
