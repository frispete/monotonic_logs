"""Unit tests for monotonic_logs.py (run from the repo root):

    python3 -m unittest discover -s tests -t . -v
"""
#
# vim:set et ts=8 sw=4:
#

import io
import os
import time
import gzip
import lzma
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


BOOT = os.path.join('scc_host1_240115_1200', 'boot.txt')
LIVE = os.path.join(TESTDATA, 'live')
BOOT_HITS = (3, [(23, '-00:00:01.000000'), (39, '-00:00:01.000000'),
                 (46, '-00:00:02.000000')])


def data(name):
    return os.path.join(TESTDATA, name)


class GparMixin:
    """Save and restore the global parameters around each test"""
    GPAR_ATTRS = ('loglevel', 'logfile', 'against_max', 'show_prev', 'threshold',
                  'summary', 'all_files', 'recursive', 'live', 'year')

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


class TestLogSections(unittest.TestCase):

    def test_header_section(self):
        sections = ml.log_sections(data('messages.txt'))
        self.assertEqual(len(sections), 1)
        lines = sections[0]
        self.assertEqual(lines[0][0], 9)
        self.assertEqual(lines[-1][0], 17)
        text = '\n'.join(l for _, l in lines)
        self.assertNotIn('ignored', text)
        self.assertNotIn('#==[', text)

    def test_no_header(self):
        sections = ml.log_sections(data('messages-20240115.txt'))
        self.assertEqual([[n for n, _ in sec] for sec in sections], [list(range(1, 8))])

    def test_empty(self):
        self.assertEqual(ml.log_sections(data('messages-empty.txt')), [[]])

    def test_boot_sections(self):
        sections = ml.log_sections(data(BOOT))
        # journalctl --boot 0, --boot=-1, dmesg -T and journalctl --output,
        # the last up to EOF
        self.assertEqual([(sec[0][0], sec[-1][0]) for sec in sections],
                         [(21, 28), (31, 34), (37, 41), (44, 46)])

    def test_section_header_variants(self):
        def match(line):
            return any(h.match('#==[ Command ]=======#') and c.match(line)
                       for h, c in ml.LOG_SECTIONS[1:])
        for line in ('# /usr/bin/journalctl --no-pager --boot 0',
                     '# /usr/bin/journalctl --no-pager --boot=-1',
                     '# /bin/journalctl --boot --no-pager',
                     '# /usr/bin/journalctl --no-pager --output=short-iso',
                     '# /usr/bin/journalctl --no-pager --output short-iso | tail -5',
                     '# /usr/bin/journalctl -o short-iso --no-pager',
                     "# /usr/bin/journalctl --no-pager --output=short-iso | "
                     "egrep -e 'gnome|gdm|plasma|kde' | tail -500",
                     '# /bin/dmesg -T',
                     '# /usr/bin/dmesg -T',
                     '# /usr/bin/dmesg --color=never -T'):
            self.assertTrue(match(line), line)
        for line in ('# /usr/bin/journalctl --boot=0 --no-pager --disk-usage',
                     '# /usr/bin/journalctl --no-pager --list-boot',
                     '# /usr/bin/journalctl --boot 0',
                     '# /usr/bin/journalctl --no-pager | grep -o --boot',
                     '# /usr/bin/journalctl --no-pager -u sshd',
                     '# /usr/bin/journalctl --output=short-iso | grep --no-pager',
                     '# /usr/sbin/journalctl --no-pager --boot 0',
                     '# /bin/dmesg',
                     '# /bin/dmesg -t'):
            self.assertFalse(match(line), line)


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

    def output(self, name, **opts):
        for k, v in opts.items():
            setattr(gpar, k, v)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            count = ml.check_file(data(name))
        return count, out.getvalue().split('\n')[:-1]

    def test_summary(self):
        count, lines = self.output('messages.txt', summary = True)
        self.assertEqual(count, 2)
        # largest step, count, sum of all steps (0.5 + 62 s)
        self.assertEqual(lines, [data('messages.txt') + ':15\t-00:01:02.000000\t2\t'
                                 '-00:01:02.500000\t'
                                 '2024-01-15T09:59:00.000000+01:00 host1 chronyd[42]: '
                                 'backward 62s'])

    def test_summary_across_sections(self):
        # equal steps in two sections: the first one wins
        count, lines = self.output(BOOT, summary = True)
        self.assertEqual(count, 3)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(data(BOOT) + ':46\t-00:00:02.000000\t3\t'
                                            '-00:00:04.000000\t'))

    def test_summary_with_prev_and_max(self):
        count, lines = self.output('messages.txt', summary = True, show_prev = True,
                                   against_max = True)
        self.assertEqual(count, 3)
        prev, hit, blank = lines
        # with -m, the reference is the maximum before the step (line 14)
        self.assertEqual(prev, data('messages.txt') + ':14\t' + ' ' * 16 + '\t\t\t'
                         '2024-01-15T10:00:02.000000+01:00 host1 kernel: forward')
        self.assertTrue(hit.startswith(data('messages.txt') + ':15\t-00:01:02.000000\t3\t'
                                       '-00:01:02.700000\t'))
        self.assertEqual(blank, '')

    def test_summary_without_steps(self):
        self.assertEqual(self.output('messages-monotonic.txt', summary = True), (0, []))

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

    def test_boot(self):
        # sections are independent: no step between them, year change in
        # the older boot, journalctl year from the supportconfig directory
        self.assertEqual(self.check(BOOT), BOOT_HITS)
        self.assertEqual(self.warnings, [])

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

    def test_ref_date(self):
        none = (None, None)
        self.assertEqual(ml.ref_date('/x/messages-20240115.txt'),
                         (datetime(2024, 1, 15), 'the file name'))
        self.assertEqual(ml.ref_date('/x/messages.txt'), none)
        self.assertEqual(ml.ref_date('/x/messages-20241301.txt'), none)
        self.assertEqual(ml.ref_date('/x/messages-202401150.txt'), none)
        scc = (datetime(2024, 1, 15), 'the supportconfig directory name')
        self.assertEqual(ml.ref_date('/x/scc_host1_240115_1200/boot.txt'), scc)
        self.assertEqual(ml.ref_date('/x/GCP_Files_scc_host1_240115_1200/boot.txt'), scc)
        self.assertEqual(ml.ref_date('/x/scc_SR0815_host1_240115_1200_'
                                     '44d8fde3-496a-4b6f-bf8b-f8854f84c5f1/x.txt'), scc)
        self.assertEqual(ml.ref_date('/x/scc_host1_240115_1200_garbage/boot.txt'), none)
        self.assertEqual(ml.ref_date('/x/notscc_host1_240115_1200/boot.txt'), none)
        self.assertEqual(ml.ref_date('/x/scc_host1_241315_1200/boot.txt'), none)
        # the file name date has precedence
        self.assertEqual(ml.ref_date('/x/scc_host1_240115_1200/messages-20231231.txt'),
                         (datetime(2023, 12, 31), 'the file name'))

    def test_scc_directory_year(self):
        scc = os.path.join(self.tmpdir, 'scc_host1_230301_1200')
        os.mkdir(scc)
        path = self.write(os.path.join(scc, 'messages.txt'), 'Feb 28 12:00:00 host1 a',
                          'Feb 29 12:00:00 host1 b')
        with self.assertRaises(ml.YearError) as cm:
            self.check(path)
        self.assertIn('Feb 29 in non-leap year 2023 derived from the supportconfig '
                      'directory name', str(cm.exception))

    def test_dmesg_anchor(self):
        path = self.write('messages.txt', 'Dec 31 23:00:00 host1 a',
                          '[Mon Jan  1 00:00:00 2024] b', 'Jan  1 00:00:01 host1 c')
        self.assertEqual(self.check(path), (0, []))
        self.assertEqual(self.warnings, [])

    def test_no_log_section(self):
        path = self.write('boot.txt', '#==[ Command ]=======#', '# /bin/uname -a',
                          'Jan  1 00:00:00 host1 not a log line')
        with self.assertLogs(ml.log, 'WARNING') as cm:
            self.assertEqual(self.check(path), (0, []))
        self.assertEqual(cm.output, ['WARNING:%s:%s: no log section found'
                                     % (ml.log.name, path)])

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


class TestCompressed(CheckMixin, unittest.TestCase):
    """Compressed input, created from the plain test data"""
    OPENERS = {'.gz': gzip.open, '.xz': lzma.open}

    def setUp(self):
        CheckMixin.setUp(self)
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        CheckMixin.tearDown(self)

    def compress(self, name, ext, dirname = None):
        dirname = os.path.join(self.tmpdir, dirname or '')
        os.makedirs(dirname, exist_ok = True)
        path = os.path.join(dirname, os.path.basename(name) + ext)
        with open(data(name), 'rb') as src, self.OPENERS[ext](path, 'wb') as dst:
            dst.write(src.read())
        return path

    def test_same_results(self):
        for name in ('messages.txt', 'messages-20240103.txt', 'messages-20240115.txt'):
            expected = self.check(name)
            for ext in self.OPENERS:
                self.assertEqual(self.check(self.compress(name, ext)), expected,
                                 name + ext)

    def test_boot_txt_in_scc_directory(self):
        for ext in self.OPENERS:
            path = self.compress(BOOT, ext, os.path.dirname(BOOT))
            self.assertEqual(self.check(path), BOOT_HITS)

    def test_collect_files(self):
        paths = [self.compress('messages-20240103.txt', '.gz'),
                 self.compress('messages-20240115.txt', '.xz')]
        raw = os.path.join(self.tmpdir, 'messages-20240116.xz')
        shutil.copy(paths[1], raw)
        paths.append(raw)
        for name in ('boot.txt.gz', 'other.gz'):
            open(os.path.join(self.tmpdir, name), 'w').close()
        self.assertEqual(ml.collect_files([self.tmpdir]),
                         sorted(paths + [os.path.join(self.tmpdir, 'boot.txt.gz')]))


class TestAllFiles(CheckMixin, unittest.TestCase):
    """Option -a: search all files with logs and all sections with logs"""
    ISO = ['2024-01-15T10:00:%02d+01:00 host1 line %d' % (i, i) for i in range(6)]
    STEP = ISO[:3] + ['2024-01-15T09:00:00+01:00 host1 backward'] + ISO[3:]

    def setUp(self):
        CheckMixin.setUp(self)
        self.tmpdir = tempfile.mkdtemp()
        self.write('app.log', *self.STEP)
        self.write('short.log', *(self.ISO[:4] + ['text'] + self.ISO[:4]))
        self.write('notes.txt', 'just', 'text')
        with open(os.path.join(self.tmpdir, 'data.bin'), 'wb') as fh:
            fh.write(b'\0' + '\n'.join(self.ISO).encode())
        with gzip.open(os.path.join(self.tmpdir, 'app.log.gz'), 'wt') as fh:
            fh.write('\n'.join(self.ISO))
        self.write('zypper.txt', '#==[ Log File ]=====#', '# /var/log/zypp/history',
                   *(self.STEP + ['#==[ Command ]=====#', '# /bin/ls', '2024-01-01T00:00:00Z x']))
        os.mkdir(os.path.join(self.tmpdir, 'subdir'))
        self.write(os.path.join('subdir', 'app.log'), *self.STEP)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        CheckMixin.tearDown(self)

    def write(self, name, *lines):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        return path

    def path(self, name):
        return os.path.join(self.tmpdir, name)

    def test_collect_files(self):
        self.assertEqual(ml.collect_files([self.tmpdir]), [])
        gpar.all_files = True
        self.assertEqual(ml.collect_files([self.tmpdir]),
                         [self.path(n) for n in ('app.log', 'app.log.gz', 'zypper.txt')])

    def test_collect_files_keeps_patterns(self):
        gpar.all_files = True
        self.assertIn(data('messages-20240302.txt'), ml.collect_files([TESTDATA]))

    def test_other_sections(self):
        # without -a, the zypper history section is not a known log section
        with self.assertLogs(ml.log, 'WARNING'):
            self.assertEqual(self.check(self.path('zypper.txt')), (0, []))
        # with -a it is checked, the short Command section is not
        self.assertEqual(self.check(self.path('zypper.txt'), all_files = True),
                         (1, [(6, '-01:00:02.000000')]))

    def test_known_sections_unchanged(self):
        expected = self.check(BOOT)
        self.assertEqual(self.check(BOOT, all_files = True), expected)

    def test_has_log_run(self):
        self.assertTrue(ml.has_log_run(self.ISO[:5]))
        self.assertFalse(ml.has_log_run(self.ISO[:4]))
        self.assertFalse(ml.has_log_run(self.ISO[:4] + ['x'] + self.ISO[:4]))
        self.assertTrue(ml.has_log_run(['Jan  1 00:00:00 a'] * 3 +
                                       ['[Mon Jan  1 00:00:00 2024] b'] * 2))
        self.assertFalse(ml.has_log_run(['Foo  1 00:00:00 a'] * 5))

    def test_log_line_re(self):
        # LOG_LINE_RE must agree with the parsers on all test data lines
        def parsed(line):
            if ml.ISO_RE.match(line) or ml.MONO_RE.match(line):
                return True
            m = ml.DMESG_RE.match(line) or ml.BSD_RE.match(line)
            return bool(m) and m.group(1) in ml.MONTHS
        names = [n for n in os.listdir(TESTDATA) if n.endswith('.txt')] + [BOOT] + \
            [os.path.join('live', n) for n in os.listdir(LIVE) if n != 'sub']
        for name in names:
            with open(data(name)) as fh:
                for line in fh:
                    self.assertEqual(bool(ml.LOG_LINE_RE.match(line)), parsed(line), line)

    def test_main(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ml.main(['-a', self.tmpdir]), 1)
        self.assertEqual(err.getvalue().splitlines()[-1], 'total: 2 backward steps')


class TestRecursive(GparMixin, unittest.TestCase):
    """Option -r: search directories recursively"""
    ISO = ['2024-01-15T10:00:%02d+01:00 host1 line %d' % (i, i) for i in range(6)]

    def setUp(self):
        GparMixin.setUp(self)
        self.tmpdir = tempfile.mkdtemp()
        for name in ('messages.txt', os.path.join('b', 'boot.txt'),
                     os.path.join('b', 'c', 'messages-20240103.txt'),
                     os.path.join('a', 'app.log'),
                     os.path.join('b', 'messages-dir.txt', 'messages.txt')):
            self.write(name, *self.ISO)
        os.symlink(self.tmpdir, self.path('b', 'loop'))

    def tearDown(self):
        shutil.rmtree(self.tmpdir)
        GparMixin.tearDown(self)

    def path(self, *names):
        return os.path.join(self.tmpdir, *names)

    def write(self, name, *lines):
        path = self.path(name)
        os.makedirs(os.path.dirname(path), exist_ok = True)
        with open(path, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')

    def test_not_recursive(self):
        self.assertEqual(ml.collect_files([self.tmpdir]), [self.path('messages.txt')])

    def test_recursive(self):
        gpar.recursive = True
        # sorted per directory, top down; the directory messages-dir.txt is
        # searched, not taken as file; the symlink loop is not followed
        self.assertEqual(ml.collect_files([self.tmpdir]),
                         [self.path('messages.txt'), self.path('b', 'boot.txt'),
                          self.path('b', 'c', 'messages-20240103.txt'),
                          self.path('b', 'messages-dir.txt', 'messages.txt')])

    def test_recursive_all(self):
        gpar.recursive = gpar.all_files = True
        self.assertIn(self.path('a', 'app.log'), ml.collect_files([self.tmpdir]))

    def test_glob_matching_directories(self):
        self.assertEqual(ml.collect_files([self.path('[ab]')]),
                         [self.path('b', 'boot.txt')])
        gpar.recursive = True
        self.assertEqual(ml.collect_files([self.path('[ab]')]),
                         [self.path('b', 'boot.txt'),
                          self.path('b', 'c', 'messages-20240103.txt'),
                          self.path('b', 'messages-dir.txt', 'messages.txt')])

    def test_main_nested_scc_directory(self):
        # the year comes from the nested scc directory
        scc = os.path.join('case', 'scc_host1_240115_1200')
        self.write(os.path.join(scc, 'messages.txt'), 'Jan 15 10:00:01 host1 a',
                   'Jan 15 10:00:00 host1 backward')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ml.main(['-r', '-s', self.path('case')]), 1)
        self.assertTrue(out.getvalue().startswith(self.path(scc, 'messages.txt') + ':2\t'))


class TestLive(CheckMixin, unittest.TestCase):
    """Option -L: logs of a live system"""
    FILES = ('boot.msg', 'boot.omsg', 'localmessages', 'messages',
             'messages-20240114', 'warn')

    def live(self, *names):
        return os.path.join(LIVE, *names)

    def test_collect_files(self):
        self.assertEqual(ml.collect_files([LIVE]), [])
        gpar.live = True
        self.assertEqual(ml.collect_files([LIVE]), [self.live(n) for n in self.FILES])
        gpar.all_files = True
        self.assertIn(self.live('other.log'), ml.collect_files([LIVE]))
        self.assertNotIn(self.live('sub', 'messages'), ml.collect_files([LIVE]))
        gpar.recursive = True
        self.assertEqual(ml.collect_files([LIVE])[-1], self.live('sub', 'messages'))

    def test_boot_msg(self):
        # continuation line skipped, systemd line with priority <30>
        self.assertEqual(self.check(self.live('boot.msg')), (1, [(5, '-00:00:00.000100')]))

    def test_rotated_year_from_file_name(self):
        self.assertEqual(self.check(self.live('messages-20240114')),
                         (1, [(3, '-00:00:02.000000')]))

    def test_mono_re(self):
        for line, secs in (('<6>[   14.028349] [    T713] x', 14.028349),
                           ('[    0.000000] x', 0.0), ('[123456.5] x', 123456.5)):
            m = ml.MONO_RE.match(line)
            self.assertIsNotNone(m, line)
            self.assertAlmostEqual(float('%s.%s' % m.groups()), secs)
        for line in ('<6> [ 1.0] x', '[ 1] x', '[Mon Jan 15 10:00:00 2024] x'):
            self.assertIsNone(ml.MONO_RE.match(line), line)

    def test_clock_change_starts_over(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        path = os.path.join(tmpdir, 'mixed.log')
        with open(path, 'w') as fh:
            fh.write('[   10.000000] boot\n'
                     '2024-01-15T10:00:00+01:00 host1 wall clock\n'
                     '[    5.000000] not compared with the wall clock\n'
                     '[    4.000000] backward 1s\n'
                     'Jan 15 09:00:00 host1 syslog, year from the timestamp at line 2\n')
        self.assertEqual(self.check(path), (1, [(4, '-00:00:01.000000')]))
        self.assertEqual(self.warnings, [])

    def test_mtime_year(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        path = os.path.join(tmpdir, 'warn')
        with open(path, 'w') as fh:
            fh.write('Feb 29 12:00:00 host1 a\nFeb 28 12:00:00 host1 backward\n')
        t = time.mktime(datetime(2024, 3, 1, 12, 0).timetuple())
        os.utime(path, (t, t))
        with self.assertRaises(ml.YearError):
            self.check(path)
        self.assertEqual(ml.ref_date(path), (None, None))
        gpar.live = True
        self.assertEqual(ml.ref_date(path), (datetime(2024, 3, 1, 12, 0), 'the file time stamp'))
        self.assertEqual(self.check(path), (1, [(2, '-1d 00:00:00.000000')]))

    def test_main_default_path(self):
        out = io.StringIO()
        with mock.patch.object(ml, 'LIVE_DIR', LIVE), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ml.main(['-L', '-s']), 1)
        self.assertEqual([l.split('\t')[0] for l in out.getvalue().splitlines()],
                         [self.live('boot.msg:5'), self.live('localmessages:2'),
                          self.live('messages:3'), self.live('messages-20240114:3')])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'running as root')
    def test_permission_hint(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        path = os.path.join(tmpdir, 'messages')
        open(path, 'w').close()
        os.chmod(path, 0)
        with self.assertLogs(ml.log, 'ERROR') as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ml.main(['-L', tmpdir]), 2)
        self.assertTrue(cm.output[0].endswith('(run as root?)'), cm.output[0])


class TestCollectFiles(unittest.TestCase):

    def test_directory(self):
        files = ml.collect_files([TESTDATA])
        self.assertIn(data('messages.txt'), files)
        self.assertEqual(files, sorted(files))

    def test_glob(self):
        self.assertEqual(ml.collect_files([data('messages-2024030*.txt')]),
                         [data('messages-20240301.txt'), data('messages-20240302.txt')])

    def test_boot_txt(self):
        self.assertEqual(ml.collect_files([os.path.dirname(data(BOOT))]), [data(BOOT)])

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

    def stderr(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            ml.main(list(argv))
        return err.getvalue().splitlines()

    def test_quiet_without_steps(self):
        files = (data('messages-monotonic.txt'), data('messages.txt'))
        self.assertEqual(self.stderr(*files), ['%s: 2 backward steps' % files[1],
                                               'total: 2 backward steps'])
        self.assertEqual(self.stderr(data('messages-monotonic.txt')),
                         ['total: 0 backward steps'])

    def test_verbose_without_steps(self):
        with self.assertLogs(ml.log, 'INFO'):
            lines = self.stderr('-v', data('messages-monotonic.txt'))
        self.assertEqual(lines, ['%s: 0 backward steps' % data('messages-monotonic.txt'),
                                 'total: 0 backward steps'])

    def test_summary_option(self):
        self.assertEqual(self.main('-s', data('messages.txt')), 1)
        self.assertTrue(gpar.summary)
        self.assertEqual(self.main('--summary', '-t', '3600', data('messages.txt')), 0)

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

    def test_corrupt_compressed(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        for name, content in (('messages-20240103.txt.gz', b'not gzip'),
                              ('messages-20240103.txt.xz', b'not xz'),
                              ('messages-20240104.txt.gz', gzip.compress(b'Jan  1 ')[:-8])):
            path = os.path.join(tmpdir, name)
            with open(path, 'wb') as fh:
                fh.write(content)
            with self.assertLogs(ml.log, 'ERROR'):
                self.assertEqual(self.main(path), 2, name)

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
