#! /usr/bin/env python3
"""
Synopsis:
Check SUSE supportconfig messages*.txt files for non-monotonic timestamps.

Usage: %(appname)s [-hVvmp][-t secs][-y year][-l log] path...
       -h, --help           this message
       -V, --version        print version and exit
       -v, --verbose        verbose mode (cumulative)
       -l, --logfile=fname  log to this file
       -m, --max            compare against the highest timestamp seen so far
                            instead of the previous line
       -p, --prev           print the reference line before each hit
       -t, --threshold=secs report only backward steps >= secs (default: 0)
       -y, --year=year      year of the first traditional syslog timestamp,
                            required if it cannot be validated (see Notes)

       path may be a file, a glob pattern, or a directory
       (directories are searched for messages*.txt)

Description:
If a file contains the header

    #==[ Log File ]=====================================#
    # /var/log/messages

everything before it is ignored, and the section ends at the next "#==["
header. Files without this header (e.g. messages-yyyymmdd.txt) are
processed completely.

Supported timestamp formats:
    RFC3339/ISO8601 (rsyslog high precision): 2024-01-15T10:23:45.123456+01:00
    traditional syslog:                       Jan 15 10:23:45

For each backward step, one tab separated line is printed to stdout:
    <file>:<lineno>  <negative delta>  <log line>

A per file summary and the total are printed to stderr.
Exit code: 0 = no backward steps, 1 = backward steps found,
2 = a file could not be read or its year could not be validated,
3 = interrupted.

Notes:
Only the Python standard library is used, compatible with Python 3.6+ (SLES 15).
Lines without a timestamp (continuation lines) are skipped.
Traditional syslog timestamps carry no year. The year of the first one is
taken from option -y, or else counted backwards from the first RFC3339
timestamp or the date in the file name (messages-yyyymmdd.txt). From there,
years are counted forwards, a step from December to January is treated as
a year change, not as a backward step. At each RFC3339 timestamp, the year
is resynced. A warning is printed, if a derived year is inconsistent with
a RFC3339 timestamp or the file name date.
Without -y, a file is skipped with an error, if no year source exists, or
the derived year results in Feb 29 of a non-leap year. With -y, that line
is skipped with a warning. The option applies to all given files.

Copyright:
(c)2026 by %(author)s

License:
%(license)s
"""
#
# vim:set et ts=8 sw=4:
#

__version__ = '0.3'
__author__ = 'Hans-Peter Jansen <hpj@urpla.net>'
__license__ = 'GNU GPL v2 - see http://www.gnu.org/licenses/gpl2.txt for details'


import os
import re
import sys
import glob
import getopt
import calendar
import logging

from datetime import datetime, timedelta, timezone


class gpar:
    """Global parameter class"""
    appdir, appname = os.path.split(sys.argv[0])
    if appdir == '.':
        appdir = os.getcwd()
    if appname.endswith('.py'):
        appname = appname[:-3]
    version = __version__
    author = __author__
    license = __license__
    loglevel = logging.ERROR
    logfile = None
    against_max = False
    show_prev = False
    threshold = timedelta(0)
    year = None


log = logging.getLogger(gpar.appname)

stdout = lambda *msg: print(*msg, file = sys.stdout, flush = True)
stderr = lambda *msg: print(*msg, file = sys.stderr, flush = True)


HEADER_RE = re.compile(r'^#==\[\s*Log File\s*\]=+#?\s*$')
LOGNAME_RE = re.compile(r'^#\s*/var/log/messages\b')
SECTION_RE = re.compile(r'^#==\[')

ISO_RE = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})'
    r'(?:[.,](\d+))?'
    r'(Z|[+-]\d{2}:?\d{2})?'
)
BSD_RE = re.compile(r'^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\b')
MONTHS = {m: i for i, m in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}

# date of rotated files, e.g. messages-20240115.txt
FNAME_DATE_RE = re.compile(r'-((?:19|20)\d{6})(?!\d)')


def exit(ret = 0, msg = None, usage = False):
    """Terminate process with optional message and usage"""
    if msg:
        stderr('%s: %s' % (gpar.appname, msg))
    if usage:
        stderr(__doc__ % gpar.__dict__)
    sys.exit(ret)


def setup_logging(logfile, loglevel):
    """Setup various aspects of logging facility"""
    logconfig = dict(
        level = loglevel,
        format = '%(asctime)s %(levelname)5s: [%(name)s] %(message)s',
        datefmt = '%Y-%m-%d %H:%M:%S',
    )
    if logfile not in (None, '-'):
        logconfig['filename'] = logfile
    logging.basicConfig(**logconfig)


def parse_iso(m):
    """Convert an ISO_RE match into a datetime (Python 3.6 compatible)"""
    y, mo, d, h, mi, s, frac, tz = m.groups()
    us = int((frac or '0')[:6].ljust(6, '0'))
    tzinfo = None
    if tz:
        if tz == 'Z':
            tzinfo = timezone.utc
        else:
            sign = -1 if tz[0] == '-' else 1
            tz = tz[1:].replace(':', '')
            tzinfo = timezone(sign * timedelta(hours = int(tz[:2]),
                                               minutes = int(tz[2:])))
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), us, tzinfo)


def comparable(a, b):
    """Make a mix of naive and aware datetimes comparable"""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo = None)
        b = b.replace(tzinfo = None)
    return a, b


def fmt_delta(td):
    """Format a timedelta as [+-][Nd ]HH:MM:SS.ffffff"""
    sign = '-' if td < timedelta(0) else '+'
    td = abs(td)
    hours, rem = divmod(td.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    s = '%02d:%02d:%02d.%06d' % (hours, minutes, seconds, td.microseconds)
    if td.days:
        s = '%dd %s' % (td.days, s)
    return sign + s


def log_lines(path):
    """Yield (lineno, line) of the log part of a file"""
    with open(path, 'r', encoding = 'utf-8', errors = 'replace') as fh:
        lines = fh.read().splitlines()

    start, end = 0, len(lines)
    for i in range(len(lines) - 1):
        if HEADER_RE.match(lines[i]) and LOGNAME_RE.match(lines[i + 1]):
            start = i + 2
            for j in range(start, len(lines)):
                if SECTION_RE.match(lines[j]):
                    end = j
                    break
            break
    log.debug('%s: log section lines %d-%d', path, start + 1, end)

    for i in range(start, end):
        yield i + 1, lines[i]


class YearError(Exception):
    """Year of traditional syslog timestamps cannot be validated"""


def fname_date(path):
    """Return the date of a rotated file from its name, or None"""
    m = FNAME_DATE_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y%m%d')
        except ValueError:
            pass
    return None


def year_backwards(run, year, mon):
    """Count the year backwards over syslog entries, return the first one's"""
    for e in reversed(run):
        if e[3][0] == 12 and mon == 1:
            year -= 1
        mon = e[3][0]
    return year


def end_year(ref, e):
    """Year of the last syslog entry e, rotated at date ref"""
    if e[3][:2] > (ref.month, ref.day):
        return ref.year - 1
    return ref.year


def start_year(path, entries):
    """Return (year, source) of the first entry, which is a syslog one"""
    if gpar.year is not None:
        return gpar.year, 'option -y'
    for i, e in enumerate(entries):
        if e[3] is None:
            # count backwards from the first RFC3339 timestamp
            return (year_backwards(entries[:i], e[2].year, e[2].month),
                    'the timestamp at line %d' % e[0])
    ref = fname_date(path)
    if ref is None:
        raise YearError('no RFC3339 timestamp and no date in the file name')
    return (year_backwards(entries, end_year(ref, entries[-1]), entries[-1][3][0]),
            'the file name')


def assign_years(path, entries):
    """Set the timestamp of traditional syslog entries [no, line, ts, fields]

    The year of the first syslog entry is taken from option -y, or counted
    backwards from the first RFC3339 timestamp, or from the date in the file
    name. From there, years are counted forwards (December -> January is a
    year change) and resynced at each RFC3339 timestamp.
    """
    if all(e[3] is None for e in entries):
        return
    year = mon = source = None
    run_start = None
    if entries[0][3] is not None:
        year, source = start_year(path, entries)
        mon = entries[0][3][0]
        log.debug('%s: start year %d derived from %s', path, year, source)

    for i, e in enumerate(entries):
        if e[3] is None:
            ts = e[2]
            if i > 0 and entries[i - 1][3] is not None:
                expected = year + (1 if mon == 12 and ts.month == 1 else 0)
                if expected != ts.year:
                    stderr('%s:%d: warning: year %d of the preceding syslog '
                           'timestamps derived from %s is inconsistent with '
                           'this timestamp' % (path, e[0], expected, source))
            year, mon = ts.year, ts.month
            source = 'the timestamp at line %d' % e[0]
            continue

        if i > 0 and entries[i - 1][3] is None:
            run_start = i
        m, day, h, mi, s = e[3]
        if mon == 12 and m == 1:
            year += 1
        mon = m
        if (m, day) == (2, 29) and not calendar.isleap(year):
            if gpar.year is None:
                raise YearError('line %d: Feb 29 in non-leap year %d derived from %s'
                                % (e[0], year, source))
            stderr('%s:%d: warning: Feb 29 in non-leap year %d derived from %s, '
                   'line skipped' % (path, e[0], year, source))
            continue
        try:
            e[2] = datetime(year, m, day, h, mi, s)
        except ValueError:
            log.debug('%s:%d: invalid timestamp', path, e[0])

    # last run, resynced at a RFC3339 timestamp: check against the file name
    if gpar.year is None and run_start is not None and entries[-1][3] is not None:
        ref = fname_date(path)
        if ref is not None and end_year(ref, entries[-1]) != year:
            stderr('%s:%d: warning: year %d derived from the file name is '
                   'inconsistent with %s, using %d' % (
                   path, entries[run_start][0], end_year(ref, entries[-1]),
                   source, year))


def check_file(path):
    """Report backward timestamp steps in one file, return their count"""
    entries = []
    for no, line in log_lines(path):
        m = ISO_RE.match(line)
        if m:
            try:
                entries.append([no, line, parse_iso(m), None])
            except ValueError:
                log.debug('%s:%d: invalid timestamp', path, no)
            continue
        m = BSD_RE.match(line)
        if m and m.group(1) in MONTHS:
            mon, day, h, mi, s = m.groups()
            entries.append([no, line, None,
                            (MONTHS[mon], int(day), int(h), int(mi), int(s))])
        # else: continuation line or no timestamp
    assign_years(path, entries)

    prev_ts = prev_line = prev_no = None
    count = 0
    for no, line, ts, _ in entries:
        if ts is None:
            # invalid syslog date
            continue
        if prev_ts is not None:
            a, b = comparable(ts, prev_ts)
            delta = a - b
            if delta < timedelta(0) and -delta >= gpar.threshold:
                count += 1
                fdelta = fmt_delta(delta)
                if gpar.show_prev:
                    stdout('%s:%d\t%s\t%s' % (path, prev_no, ' ' * len(fdelta), prev_line))
                stdout('%s:%d\t%s\t%s' % (path, no, fdelta, line))
                if gpar.show_prev:
                    stdout()
                if gpar.against_max:
                    # keep the maximum as reference
                    continue
        prev_ts, prev_line, prev_no = ts, line, no

    return count


def collect_files(args):
    """Expand directories and glob patterns into a list of files"""
    files = []
    for arg in args:
        if os.path.isdir(arg):
            files.extend(sorted(glob.glob(os.path.join(arg, 'messages*.txt'))))
        else:
            matched = glob.glob(arg)
            files.extend(sorted(matched) if matched else [arg])
    return files


def process(args):
    ret = 0
    log.info('started with pid %s in %s', os.getpid(), gpar.appdir)

    files = collect_files(args)
    if not files:
        exit(1, 'no files found')

    total = 0
    for path in files:
        log.info('processing %s', path)
        try:
            count = check_file(path)
        except OSError as e:
            log.error('%s: %s', path, e)
            ret = 2
            continue
        except YearError as e:
            log.error('%s: year of syslog timestamps cannot be validated: %s, '
                      'use -y to set the starting year', path, e)
            ret = 2
            continue
        stderr('%s: %d backward steps' % (path, count))
        total += count

    stderr('total: %d backward steps' % total)
    if total and not ret:
        ret = 1
    return ret


def main(argv = None):
    """Command line interface and console script entry point."""
    if argv is None:
        argv = sys.argv[1:]

    try:
        optlist, args = getopt.getopt(argv, 'hVvl:mpt:y:',
            ('help', 'version', 'verbose', 'logfile=',
             'max', 'prev', 'threshold=', 'year=')
        )
    except getopt.error as msg:
        exit(1, msg, True)

    for opt, par in optlist:
        if opt in ('-h', '--help'):
            exit(usage = True)
        elif opt in ('-V', '--version'):
            exit(msg = 'version %s' % gpar.version)
        elif opt in ('-v', '--verbose'):
            if gpar.loglevel > logging.DEBUG:
                gpar.loglevel -= 10
        elif opt in ('-l', '--logfile'):
            gpar.logfile = par
        elif opt in ('-m', '--max'):
            gpar.against_max = True
        elif opt in ('-p', '--prev'):
            gpar.show_prev = True
        elif opt in ('-t', '--threshold'):
            try:
                gpar.threshold = timedelta(seconds = float(par))
            except ValueError:
                exit(1, 'invalid threshold: %s' % par, True)
        elif opt in ('-y', '--year'):
            try:
                gpar.year = int(par)
                datetime(gpar.year, 1, 1)
            except ValueError:
                exit(1, 'invalid year: %s' % par, True)

    if not args:
        exit(1, 'no path given', True)

    setup_logging(gpar.logfile, gpar.loglevel)

    try:
        return process(args)
    except KeyboardInterrupt:
        return 3    # interrupted (Ctrl-C)


if __name__ == '__main__':
    sys.exit(main())
