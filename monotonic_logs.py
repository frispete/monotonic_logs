#! /usr/bin/env python3
"""
Synopsis:
Check SUSE supportconfig messages*.txt and boot.txt files, the logs of a
live system and the systemd journal for non-monotonic timestamps.

Usage: %(appname)s [-hVvajLmpRrs][-t secs][-y year][-l log] [path...]
       -h, --help           this message
       -V, --version        print version and exit
       -v, --verbose        verbose mode (cumulative): -v adds the number of
                            checked lines, -vv describes what will be done
                            and lists every checked file with its number
                            of lines
       -l, --logfile=fname  log to this file
       -a, --all            search directories for all files with logs, and
                            check all sections with logs (see Notes)
       -j, --journal        check the systemd journal of this system, each
                            boot separately, as journal/<boot id>
       -L, --live           search directories of a live system: messages,
                            warn, localmessages, boot.msg (with -R also
                            rotated, boot.omsg); path defaults to /var/log
       -m, --max            compare against the highest timestamp seen so far
                            instead of the previous line; consecutive lines
                            below it count as one backward step
       -p, --prev           print the reference line before each hit
       -R, --rotated        include rotated logs in directory searches
                            (e.g. messages-yyyymmdd.txt, messages-*.xz,
                            dnf.log.1, Xorg.0.log.old, boot.omsg)
       -r, --recursive      search directories recursively (symlinked
                            directories are not followed)
       -s, --summary        print only the largest backward step per file,
                            with the number and sum of backward steps
       -t, --threshold=secs report only backward steps >= secs (default: 0.25);
                            for timestamps with whole seconds, their
                            resolution of 1 sec is added; -t 0 reports all
       -y, --year=year      year of the first traditional syslog timestamp,
                            required if it cannot be validated (see Notes)

       path is required, unless -j or -L is given; it may be a file, a
       supportconfig archive (.txz, .tar.xz, .tgz, .tar.gz, .tar), a glob
       pattern, or a directory (directories are searched for messages*.txt
       and boot.txt, and messages*.gz, messages*.xz, boot.txt.gz,
       boot.txt.xz; glob patterns may match directories, too; rotated logs
       only with -R, files given explicitly are always checked)

Description:
Files with supportconfig "#==[" headers are checked in these sections:

    #==[ Log File ]=====================================#
    # /var/log/messages                    (or /var/log/warn, localmessages)

    #==[ Command ]======================================#
    # /usr/bin/journalctl --no-pager --boot 0     (any --boot argument)

    #==[ Command ]======================================#
    # /usr/bin/journalctl --no-pager --output=short-iso | ...
                                                  (any --output or -o)

    #==[ Command ]======================================#
    # /bin/dmesg -T

A section ends at the next "#==[" header, each section is checked
independently. Files without any "#==[" header (e.g. messages-yyyymmdd.txt)
are processed completely.

Supported timestamp formats:
    RFC3339/ISO8601 (rsyslog high precision): 2024-01-15T10:23:45.123456+01:00
    traditional syslog, journalctl:           Jan 15 10:23:45
    dmesg -T:                                 [Mon Jan 15 10:23:45 2024]
    time since boot (boot.msg, dmesg):        <6>[   14.028349]

Times since boot are only compared with each other: if the type of
timestamp changes between wall clock and time since boot, the comparison
starts over.

For each backward step, one tab separated line is printed to stdout:
    <file>:<lineno>  <negative delta>  <log line>
With -s, only the largest step of each file with backward steps:
    <file>:<lineno>  <negative delta>  <number of backward steps>
        <cumulated negative delta>  <log line>

With -p, the reference line is printed before each step, followed by an
empty line.

A per file summary of files with backward steps and the total are printed
to stderr. With -v, the number of checked lines (with timestamp) is added,
with -vv, what will be done is described, and every checked file is listed,
with its number of lines.
Exit code: 0 = no backward steps, 1 = backward steps found or no files
found, 2 = usage error, 3 = a file could not be read (or decompressed, e.g.
a truncated archive) or its year could not be validated, 4 = interrupted.

Notes:
Only the Python standard library is used, compatible with Python 3.6+ (SLES 15).
Lines without a timestamp (continuation lines) are skipped.
Compressed files (gzip, xz) are decompressed on the fly, detected by their
content. Tar archives are read in one pass without extraction: their
members are selected like files in a directory (only the archive root and
its top level directories without -r), and reported as
<archive>/<member>. In directory searches, archives are skipped, if their
extracted directory exists next to them, and with -L.
With -a, directories are also searched for all other non-binary files with
at least 5 consecutive lines starting with a supported timestamp, and in
files with "#==[" headers, all other sections with such lines are checked
as well.
Traditional syslog timestamps carry no year. The year of the first one in
a section is taken from option -y, or else counted backwards from the first
full (RFC3339 or dmesg -T) timestamp, or from the date in the file name
(messages-yyyymmdd.txt) or the nearest supportconfig directory or archive
name above the file (scc_<host>_yymmdd_hhmm[ss][_<suffix>]), with -L also
from the file time stamp. From there, years are counted forwards, a step
from December to January is treated as a year change, not as a backward
step. At each full timestamp, the year is resynced. A warning is printed,
if a derived year is inconsistent with a full timestamp or the date of the
file or directory name.
Without -y, a file is skipped with an error, if no year source exists, or
the derived year results in Feb 29 of a non-leap year. With -y, that line
is skipped with a warning. The option applies to all given files and
sections.

Copyright:
(c)2026 by %(author)s

License:
%(license)s
"""
#
# vim:set et ts=8 sw=4:
#

__version__ = '0.22'
__author__ = 'Hans-Peter Jansen <hpj@urpla.net>'
__license__ = 'GNU GPL v2 - see http://www.gnu.org/licenses/gpl2.txt for details'


import io
import os
import re
import sys
import glob
import gzip
import lzma
import getopt
import fnmatch
import tarfile
import itertools
import subprocess
import calendar
import logging

from datetime import datetime, timedelta, timezone


# default threshold: tolerate small deviations, e.g. caused by locking
DEFAULT_THRESHOLD = 0.25
# resolution of timestamps with whole seconds
RESOLUTION = timedelta(seconds = 1)


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
    threshold = timedelta(seconds = DEFAULT_THRESHOLD)
    summary = False
    all_files = False
    recursive = False
    live = False
    rotated = False
    journal = False
    year = None


log = logging.getLogger(gpar.appname)

stdout = lambda *msg: print(*msg, file = sys.stdout, flush = True)
stderr = lambda *msg: print(*msg, file = sys.stderr, flush = True)


SECTION_RE = re.compile(r'^#==\[')
LOGFILE_RE = re.compile(r'^#==\[\s*Log File\s*\]=+#?\s*$')
COMMAND_RE = re.compile(r'^#==\[\s*Command\s*\]=+#?\s*$')
# (section header, following line) of log sections
LOG_SECTIONS = (
    # messages.txt, messages_localwarn.txt
    (LOGFILE_RE, re.compile(r'^#\s*/var/log/(?:messages|warn|localmessages)\b')),
    # journalctl options are only looked up before a pipe
    (COMMAND_RE, re.compile(r'^#\s*(?:/usr)?/bin/journalctl'
                            r'(?=[^|]*\s--no-pager\b)'
                            r'(?=[^|]*\s(?:--boot|--output|-o)\b)'
                            r'(?![^|]*\s--(?:disk-usage|list-boots?)\b)')),
    (COMMAND_RE, re.compile(r'^#\s*(?:/usr)?/bin/dmesg\s(?:.*\s)?-T\b')),
)

ISO_RE = re.compile(
    r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})'
    r'(?:[.,](\d+))?'
    r'(Z|[+-]\d{2}:?\d{2})?'
)
# tzinfo cache of parse_tz()
TZINFOS = {'Z': timezone.utc}
BSD_RE = re.compile(r'^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\b')
# dmesg -T: [Thu Feb  5 23:03:22 2026]
DMESG_RE = re.compile(r'^\[[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+'
                      r'(\d{2}):(\d{2}):(\d{2})\s+(\d{4})\]')
# kernel time since boot (boot.msg, dmesg): <6>[   14.028349] or [   14.028349]
MONO_RE = re.compile(r'^(?:<\d+>)?\[\s*(\d+)\.(\d+)\]')
MONTHS = {m: i for i, m in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}
# fast check for any of ISO_RE, DMESG_RE, MONO_RE, BSD_RE (with a valid month)
LOG_LINE_RE = re.compile(
    r'(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
    r'|\[[A-Z][a-z]{2}\s+(?:%(m)s)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}\]'
    r'|(?:<\d+>)?\[\s*\d+\.\d+\]'
    r'|(?:%(m)s)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b)' % dict(m = '|'.join(MONTHS)))

# file name patterns searched in directories
DIR_PATTERNS = ('messages*.txt', 'messages*.gz', 'messages*.xz',
                'boot.txt', 'boot.txt.gz', 'boot.txt.xz')
# file name patterns searched in directories of a live system (option -L)
LIVE_PATTERNS = ('messages', 'messages-*', 'warn', 'warn-*',
                 'localmessages', 'localmessages-*', 'boot.msg', 'boot.omsg')
LIVE_DIR = '/var/log'
# rotated log files (after stripping .gz/.xz): messages-20240115.txt,
# messages-20240115, zypper.log-20240115, dnf.log.1, Xorg.0.log.old, boot.omsg
ROTATED_RE = re.compile(r'(?:-(?:19|20)\d{6}(?:\.txt|\.log)?|\.\d+|\.old|^boot\.omsg)$')

# minimum number of consecutive timestamped lines of a log (option -a)
MIN_RUN = 5

# extensions of compressed files, stripped for name checks
COMPRESSED_EXTS = ('.gz', '.xz')
# openers and decompressors of compressed data by magic bytes
MAGICS = ((b'\x1f\x8b', gzip.open, gzip.decompress),
          (b'\xfd7zXZ\x00', lzma.open, lzma.decompress))
# extensions of (compressed) tar archives, e.g. scc_host1_240115_1200.txz
ARCHIVE_EXTS = ('.txz', '.tar.xz', '.tgz', '.tar.gz', '.tar')
# errors of reading compressed files and archives
READ_ERRORS = (OSError, EOFError, lzma.LZMAError, tarfile.TarError)
# modification times of archive members by path (option -L)
MEMBER_MTIMES = {}

# systemd journal (option -j): output formats, the latter for systemd < 236
JOURNALCTL = 'journalctl'
JOURNAL_FORMATS = ('short-iso-precise', 'short-iso')
# journalctl --list-boots, with or without header: " -1 <boot id> ..."
BOOT_RE = re.compile(r'^\s*(-?\d+)\s+([0-9a-f]{32})\b')
# journalctl hint for users, that see their own messages only
JOURNAL_HINT = 'not seeing messages'
# lines of a stream (the journal) checked at once
CHUNK_LINES = 100000


class JournalSource:
    """Placeholder of the systemd journal in the list of files"""
    def __str__(self):
        return 'journal'


JOURNAL = JournalSource()


class JournalError(OSError):
    """journalctl failed"""

# date of rotated files, e.g. messages-20240115.txt
FNAME_DATE_RE = re.compile(r'-((?:19|20)\d{6})(?!\d)')
# collection date of supportconfig directories or archives, e.g.
# scc_host1_240115_1200, scc_host1_240115_120059, optionally with an UUID or
# a word suffix (scc_host1_240115_120059_appcore)
SCC_DATE_RE = re.compile(r'(?:^|_)scc_.+_(\d{6})_\d{4}(?:\d\d)?'
                         r'(?:_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|_[A-Za-z]+)?'
                         r'(?:%s)?$' % '|'.join(re.escape(ext) for ext in ARCHIVE_EXTS))


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


def parse_tz(tz):
    """Convert an ISO_RE offset (Z, +01:00, -0500) into a tzinfo, cached"""
    tzinfo = TZINFOS.get(tz)
    if tzinfo is None:
        sign = -1 if tz[0] == '-' else 1
        hhmm = tz[1:].replace(':', '')
        tzinfo = TZINFOS[tz] = timezone(sign * timedelta(hours = int(hhmm[:2]),
                                                         minutes = int(hhmm[2:])))
    return tzinfo


def parse_iso(m):
    """Convert an ISO_RE match into a datetime (Python 3.6 compatible)"""
    y, mo, d, h, mi, s, frac, tz = m.groups()
    us = int(frac[:6].ljust(6, '0')) if frac else 0
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), us,
                    parse_tz(tz) if tz else None)


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


def opener(path):
    """Return the opener of a possibly compressed file, by its magic bytes"""
    with open(path, 'rb') as fh:
        head = fh.read(6)
    for magic, func, _ in MAGICS:
        if head.startswith(magic):
            return func
    return open


def decompress(data):
    """Decompress possibly compressed data, by its magic bytes"""
    for magic, _, func in MAGICS:
        if data.startswith(magic):
            return func(data)
    return data


def open_text(path):
    """Open a possibly compressed file for reading text"""
    return opener(path)(path, 'rt', encoding = 'utf-8', errors = 'replace')


def has_log_run(lines):
    """True, if MIN_RUN consecutive lines start with a timestamp"""
    run = 0
    for line in lines:
        if LOG_LINE_RE.match(line):
            run += 1
            if run >= MIN_RUN:
                return True
        else:
            run = 0
    return False


def is_log_file(path):
    """True, if path is a non-binary file with a run of timestamped lines"""
    try:
        with opener(path)(path, 'rb') as fh:
            if b'\0' in fh.read(8192):
                log.info('%s: binary file skipped', path)
                return False
        with open_text(path) as fh:
            return has_log_run(fh)
    except READ_ERRORS as e:
        log.warning('%s: skipped: %s%s', path, e,
                    ' (run as root?)' if isinstance(e, PermissionError) else '')
        return False


def read_lines(path):
    """Return the lines of a possibly compressed file"""
    with open_text(path) as fh:
        return fh.read().splitlines()


def log_sections(path, lines = None):
    """Return the log sections of a file as lists of (lineno, line)

    A file without any "#==[" header is a single section. Otherwise, only
    the sections listed in LOG_SECTIONS are returned, with option -a also
    all other sections with a run of timestamped lines. Each section ends at
    the next "#==[" header or the end of file.
    """
    if lines is None:
        lines = read_lines(path)

    if not any(SECTION_RE.match(line) for line in lines):
        return [list(enumerate(lines, 1))]

    sections = []
    i = 0
    while i < len(lines):
        if not SECTION_RE.match(lines[i]):
            i += 1
            continue
        known = i + 1 < len(lines) and \
            any(h.match(lines[i]) and c.match(lines[i + 1]) for h, c in LOG_SECTIONS)
        start = end = i + (2 if known else 1)
        while end < len(lines) and not SECTION_RE.match(lines[end]):
            end += 1
        if known or (gpar.all_files and has_log_run(lines[start:end])):
            log.debug('%s: section "%s" lines %d-%d', path,
                      lines[i + 1] if known else lines[i], start + 1, end)
            sections.append([(n + 1, lines[n]) for n in range(start, end)])
        i = end
    return sections


class YearError(Exception):
    """Year of traditional syslog timestamps cannot be validated"""


def ref_date(path):
    """Return (date, source) from the name of a rotated file or the nearest
    supportconfig directory above it, or (None, None)"""
    m = FNAME_DATE_RE.search(os.path.basename(path))
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y%m%d'), 'the file name'
        except ValueError:
            pass
    # the nearest supportconfig directory (or archive) above the file
    for name in reversed(os.path.dirname(os.path.abspath(path)).split(os.sep)):
        m = SCC_DATE_RE.search(name)
        if m:
            try:
                return datetime.strptime(m.group(1), '%y%m%d'), 'the supportconfig directory name'
            except ValueError:
                pass
            break
    if gpar.live:
        # reliable on a live system only, not for copied data
        mtime = MEMBER_MTIMES.get(path)
        if mtime is None:
            mtime = os.path.getmtime(path)
        return datetime.fromtimestamp(mtime), 'the file time stamp'
    return None, None


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
            # count backwards from the first full timestamp
            return (year_backwards(entries[:i], e[2].year, e[2].month),
                    'the timestamp at line %d' % e[0])
    ref, source = ref_date(path)
    if ref is None:
        raise YearError('no full timestamp and no date in the file or '
                        'supportconfig directory name')
    return (year_backwards(entries, end_year(ref, entries[-1]), entries[-1][3][0]),
            source)


def assign_years(path, entries):
    """Set the timestamp of traditional syslog entries [no, line, ts, fields, coarse]

    The year of the first syslog entry is taken from option -y, or counted
    backwards from the first full (RFC3339 or dmesg -T) timestamp, or from
    the date in the file or supportconfig directory name. From there, years
    are counted forwards (December -> January is a year change) and resynced
    at each full timestamp.
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

    # last run, resynced at a full timestamp: check against the reference date
    if gpar.year is None and run_start is not None and entries[-1][3] is not None:
        ref, ref_source = ref_date(path)
        if ref is not None and end_year(ref, entries[-1]) != year:
            stderr('%s:%d: warning: year %d derived from %s is '
                   'inconsistent with %s, using %d' % (
                   path, entries[run_start][0], end_year(ref, entries[-1]),
                   ref_source, source, year))


def print_hit(path, hit, extra = ()):
    """Print a backward step with extra columns, optionally preceded by its
    reference line"""
    no, delta, line, prev_no, prev_line = hit
    fdelta = fmt_delta(delta)
    if gpar.show_prev:
        stdout('%s:%d\t%s\t%s%s' % (path, prev_no, ' ' * len(fdelta),
                                    '\t' * len(extra), prev_line))
    stdout('%s:%d\t%s\t%s' % (path, no, fdelta,
                              ''.join('%s\t' % col for col in extra) + line))
    if gpar.show_prev:
        stdout()


def check_file(path, lines = None):
    """Report backward timestamp steps in one file, return the number of
    (backward steps, checked lines with timestamp, lines)"""
    if lines is None:
        lines = read_lines(path)
    hits = []
    checked = 0
    if isinstance(lines, list):
        sections = log_sections(path, lines)
        if not sections:
            log.warning('%s: no log section found', path)
        for section in sections:
            section_hits, section_checked, _ = check_section(path, section)
            hits.extend(section_hits)
            checked += section_checked
        nlines = len(lines)
    else:
        # a stream (the journal) is a single section, checked in chunks to
        # limit the memory usage; the comparison state is carried over
        numbered = enumerate(lines, 1)
        state = None
        nlines = 0
        while True:
            chunk = list(itertools.islice(numbered, CHUNK_LINES))
            if not chunk:
                break
            nlines = chunk[-1][0]
            chunk_hits, chunk_checked, state = check_section(path, chunk, state)
            hits.extend(chunk_hits)
            checked += chunk_checked

    if gpar.summary:
        if hits:
            # largest step, the first one on ties
            cumulated = sum((hit[1] for hit in hits), timedelta(0))
            print_hit(path, min(hits, key = lambda hit: hit[1]),
                      (len(hits), fmt_delta(cumulated)))
    else:
        for hit in hits:
            print_hit(path, hit)
    return len(hits), checked, nlines


def check_section(path, lines, state = None):
    """Return the backward timestamp steps in one log section as list of
    (lineno, delta, line, reference lineno, reference line), the number of
    checked lines with timestamp, and the comparison state at its end, to
    continue with the next chunk of a stream"""
    entries = []
    for no, line in lines:
        m = ISO_RE.match(line)
        if m:
            try:
                # whole seconds without fraction
                entries.append([no, line, parse_iso(m), None, m.group(7) is None])
            except ValueError:
                log.debug('%s:%d: invalid timestamp', path, no)
            continue
        m = MONO_RE.match(line)
        if m:
            secs, frac = m.groups()
            # time since boot, a timedelta: never compared with wall clock
            entries.append([no, line, timedelta(seconds = int(secs),
                                                microseconds = int(frac[:6].ljust(6, '0'))),
                            None, False])
            continue
        m = DMESG_RE.match(line)
        if m and m.group(1) in MONTHS:
            mon, day, h, mi, s, y = m.groups()
            try:
                entries.append([no, line, datetime(int(y), MONTHS[mon], int(day),
                                                   int(h), int(mi), int(s)), None, True])
            except ValueError:
                log.debug('%s:%d: invalid timestamp', path, no)
            continue
        m = BSD_RE.match(line)
        if m and m.group(1) in MONTHS:
            mon, day, h, mi, s = m.groups()
            entries.append([no, line, None,
                            (MONTHS[mon], int(day), int(h), int(mi), int(s)), True])
        # else: continuation line or no timestamp
    assign_years(path, [e for e in entries if not isinstance(e[2], timedelta)])

    # with whole seconds, the resolution is added to the threshold
    coarse_threshold = gpar.threshold + RESOLUTION if gpar.threshold else gpar.threshold
    # behind: with -m, the previous line was a backward step already, so
    # lines below the maximum continue that step instead of starting a new one
    prev_ts, prev_line, prev_no, prev_coarse, behind = \
        state or (None, None, None, None, False)
    hits = []
    for no, line, ts, _, coarse in entries:
        if ts is None:
            # invalid syslog date
            continue
        if prev_ts is not None and isinstance(ts, timedelta) != isinstance(prev_ts, timedelta):
            # clock type changed (time since boot <-> wall clock): start over
            prev_ts = None
            behind = False
        if prev_ts is not None:
            if isinstance(ts, timedelta):
                delta = ts - prev_ts
            else:
                a, b = comparable(ts, prev_ts)
                delta = a - b
            threshold = coarse_threshold if coarse or prev_coarse else gpar.threshold
            if delta < timedelta(0) and -delta >= threshold:
                if not behind:
                    hits.append((no, delta, line, prev_no, prev_line))
                if gpar.against_max:
                    # keep the maximum as reference
                    behind = True
                    continue
            behind = False
        prev_ts, prev_line, prev_no, prev_coarse = ts, line, no, coarse

    return (hits, sum(1 for e in entries if e[2] is not None),
            (prev_ts, prev_line, prev_no, prev_coarse, behind))


def is_rotated(path):
    """True, if path is a rotated log file by its name"""
    name, ext = os.path.splitext(os.path.basename(path))
    if ext not in COMPRESSED_EXTS:
        name += ext
    return bool(ROTATED_RE.search(name))


def dir_files(dirname):
    """Return the sorted log files of one directory, rotated ones only
    with option -R, tar archives not with option -L"""
    found = set()
    named = [path for ext in ARCHIVE_EXTS
             for path in glob.glob(os.path.join(dirname, '*' + ext))
             if os.path.isfile(path)]
    archives = set(path for path in named if is_archive(path))
    for pattern in LIVE_PATTERNS if gpar.live else DIR_PATTERNS:
        found.update(path for path in glob.glob(os.path.join(dirname, pattern))
                     if os.path.isfile(path))
    if gpar.all_files:
        for name in os.listdir(dirname):
            path = os.path.join(dirname, name)
            if path not in found and path not in archives and os.path.isfile(path) and \
                    (gpar.rotated or not is_rotated(path)) and is_log_file(path):
                found.add(path)
    if not gpar.rotated:
        found = set(path for path in found if not is_rotated(path))
    for path in named:
        ext = next(ext for ext in ARCHIVE_EXTS if path.endswith(ext))
        if gpar.live:
            # snapshots, not logs of the live system
            if path in archives:
                log.info('%s: archive skipped with -L', path)
        # skip archives, that are extracted next to them
        elif os.path.isdir(path[:-len(ext)]):
            log.info('%s: skipped, extracted in %s', path, path[:-len(ext)])
        else:
            found.add(path)
    return sorted(found)


def is_archive(path):
    """True, if path is a (compressed) tar archive"""
    if not path.endswith(ARCHIVE_EXTS):
        return False
    try:
        return tarfile.is_tarfile(path)
    except READ_ERRORS:
        # reported, when the archive is read
        return True


def archive_logs(path):
    """Yield (path, lines) of the log files in a tar archive, in one pass

    Members are selected like files in a directory: by DIR_PATTERNS (or
    LIVE_PATTERNS), rotated ones with -R, other logs with -a. Without -r,
    only members in the archive root and its top level directories are
    checked. The path of a member is archive path + "/" + member name.
    """
    patterns = LIVE_PATTERNS if gpar.live else DIR_PATTERNS
    with tarfile.open(path, 'r|*') as tar:
        for member in tar:
            name = member.name[2:] if member.name.startswith('./') else member.name
            if not member.isfile() or (name.count('/') > 1 and not gpar.recursive):
                continue
            base = os.path.basename(name)
            if not gpar.rotated and is_rotated(base):
                continue
            known = any(fnmatch.fnmatchcase(base, pattern) for pattern in patterns)
            if not known and not gpar.all_files:
                continue
            data = decompress(tar.extractfile(member).read())
            if not known and b'\0' in data[:8192]:
                continue
            lines = data.decode('utf-8', 'replace').splitlines()
            if not known and not has_log_run(lines):
                continue
            mpath = os.path.join(path, name)
            MEMBER_MTIMES[mpath] = member.mtime
            yield mpath, lines


def collect_files(args):
    """Expand directories and glob patterns into a list of files"""
    files = []
    for arg in args:
        matched = [arg] if os.path.isdir(arg) else sorted(glob.glob(arg)) or [arg]
        for path in matched:
            if not os.path.isdir(path):
                files.append(path)
            elif gpar.recursive:
                # symlinked directories are not followed (os.walk default)
                for root, dirs, _ in os.walk(path):
                    dirs.sort()
                    files.extend(dir_files(root))
            else:
                files.extend(dir_files(path))
    return files


def journalctl(*args):
    """Run journalctl, return (exit code, stdout lines, stderr)"""
    proc = subprocess.run((JOURNALCTL, '--no-pager') + args,
                          stdout = subprocess.PIPE, stderr = subprocess.PIPE)
    return (proc.returncode, proc.stdout.decode('utf-8', 'replace').splitlines(),
            proc.stderr.decode('utf-8', 'replace').strip())


def journal_stream(warned, *args):
    """Yield the stdout lines of journalctl, raise JournalError on failure"""
    proc = subprocess.Popen((JOURNALCTL, '--no-pager') + args,
                            stdout = subprocess.PIPE, stderr = subprocess.PIPE)
    out = io.TextIOWrapper(proc.stdout, encoding = 'utf-8', errors = 'replace')
    try:
        for line in out:
            yield line.rstrip('\n')
    finally:
        # also reached, if the consumer stops early
        out.close()
        err = proc.stderr.read().decode('utf-8', 'replace').strip()
        proc.stderr.close()
        ret = proc.wait()
    if ret:
        raise JournalError('journalctl %s: %s' % (' '.join(args), err or 'exit code %d' % ret))
    if JOURNAL_HINT in err:
        warn_journal_hint(warned)


def warn_journal_hint(warned):
    """Warn once per journal pass, that only the own messages are visible"""
    if not warned:
        warned.append(True)
        stderr('journal: warning: only your own messages are visible (run as root?)')


def journal_logs():
    """Yield (path, line stream) per boot of the systemd journal, oldest first"""
    ret, out, err = journalctl('--list-boots')
    if ret:
        raise JournalError('journalctl --list-boots: %s' % (err or 'exit code %d' % ret))
    warned = []
    if JOURNAL_HINT in err:
        warn_journal_hint(warned)
    # short-iso-precise needs systemd >= 236
    for fmt in JOURNAL_FORMATS:
        if journalctl('-q', '-n', '0', '-o', fmt)[0] == 0:
            break
        log.info('journalctl -o %s not supported', fmt)
    for m in filter(None, map(BOOT_RE.match, out)):
        boot = m.group(2)
        yield 'journal/%s' % boot, journal_stream(warned, '-q', '-o', fmt, '-b', boot)


def plural(n, word):
    """Format a count with a singular or plural noun"""
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def fmt_checked(checked, nlines, suffix = ''):
    """Format the number of checked lines (-v) and lines (-vv)"""
    if gpar.loglevel > logging.WARNING:
        return ''
    if gpar.loglevel > logging.INFO:
        return ', %s checked%s' % (plural(checked, 'line'), suffix)
    return ', %d of %s checked%s' % (checked, plural(nlines, 'line'), suffix)


def describe_plan(args):
    """Log brief, informal messages about what will be done (-vv)"""
    if gpar.loglevel > logging.INFO:
        return
    searched = False
    for arg in args:
        if os.path.isdir(arg):
            log.info('searching folder %s%s', arg, ' and all folders below it'
                     if gpar.recursive else ' (no subfolders, use -r)')
            searched = True
            continue
        matched = sorted(glob.glob(arg))
        if not os.path.exists(arg) and matched:
            dirs = sum(1 for path in matched if os.path.isdir(path))
            what = []
            if len(matched) > dirs:
                what.append('checking %s' % plural(len(matched) - dirs, 'file'))
            if dirs:
                what.append('searching %s%s' % (plural(dirs, 'folder'),
                            ' and all folders below them'
                            if gpar.recursive else ' (no subfolders, use -r)'))
            log.info('pattern %s: %s', arg, ' and '.join(what))
            searched = searched or dirs > 0 or any(map(is_archive, matched))
        elif not os.path.exists(arg):
            log.info('checking file %s (not found)', arg)
        elif is_archive(arg):
            log.info('reading archive %s%s', arg, ', all folders' if gpar.recursive
                     else ', its top level folder only (use -r)')
            searched = True
        else:
            log.info('checking file %s', arg)
    if searched:
        log.info('looking for %s%s', ', '.join(LIVE_PATTERNS if gpar.live else DIR_PATTERNS),
                 ' and any other file with at least %d timestamped lines' % MIN_RUN
                 if gpar.all_files else '')
        log.info('including rotated logs' if gpar.rotated
                 else 'skipping rotated logs (use -R)')
        log.info('skipping tar archives in folders (snapshots, not live logs)' if gpar.live
                 else 'reading tar archives in folders, unless extracted next to them')
    if gpar.all_files:
        log.info('checking all supportconfig sections with at least %d timestamped lines',
                 MIN_RUN)
    if gpar.journal:
        log.info('checking the systemd journal, each boot separately')
    log.info('comparing each line against the highest timestamp so far' if gpar.against_max
             else 'comparing each line against the previous one')
    if gpar.threshold:
        log.info('reporting backward steps >= %g s (+%g s for whole seconds)',
                 gpar.threshold.total_seconds(), RESOLUTION.total_seconds())
    else:
        log.info('reporting all backward steps')
    if gpar.summary:
        log.info('printing only the largest step per file, with count and sum')
    if gpar.show_prev:
        log.info('printing the reference line before each step')
    if gpar.year is not None:
        log.info('syslog year: starting with %d', gpar.year)
    else:
        log.info('syslog year: derived from full timestamps and file or folder names%s',
                 ', file time stamps as last resort' if gpar.live else '')


def process(args):
    ret = 0
    log.info('started with pid %s in %s', os.getpid(), gpar.appdir)
    describe_plan(args)

    files = collect_files(args)
    if gpar.journal:
        files.append(JOURNAL)
    if not files:
        exit(1, 'no files found')

    total = total_checked = total_lines = nfiles = 0
    for path in files:
        log.info('processing %s', path)
        try:
            if path is JOURNAL:
                items = journal_logs()
            elif is_archive(path):
                items = archive_logs(path)
            else:
                items = [(path, None)]
            for fpath, lines in items:
                try:
                    count, checked, nlines = check_file(fpath, lines)
                except YearError as e:
                    log.error('%s: year of syslog timestamps cannot be validated: %s, '
                              'use -y to set the starting year', fpath, e)
                    ret = 3
                    continue
                # -v: checked lines, -vv: all files and their number of lines
                if count or gpar.loglevel <= logging.INFO:
                    stderr('%s: %s%s' % (fpath, plural(count, 'backward step'),
                                         fmt_checked(checked, nlines)))
                total += count
                total_checked += checked
                total_lines += nlines
                nfiles += 1
        except READ_ERRORS as e:
            # EOFError, LZMAError, TarError: truncated or corrupt compressed file
            log.error('%s: %s%s', path, e,
                      ' (run as root?)' if isinstance(e, PermissionError) else '')
            ret = 3

    stderr('total: %s%s' % (plural(total, 'backward step'),
           fmt_checked(total_checked, total_lines, ' in %s' % plural(nfiles, 'file'))))
    if total and not ret:
        ret = 1
    return ret


def main(argv = None):
    """Command line interface and console script entry point."""
    if argv is None:
        argv = sys.argv[1:]

    try:
        optlist, args = getopt.getopt(argv, 'hVvl:ajLmpRrst:y:',
            ('help', 'version', 'verbose', 'logfile=', 'all', 'journal', 'rotated', 'live',
             'max', 'prev', 'recursive', 'summary', 'threshold=', 'year=')
        )
    except getopt.error as msg:
        exit(2, msg, True)

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
        elif opt in ('-a', '--all'):
            gpar.all_files = True
        elif opt in ('-R', '--rotated'):
            gpar.rotated = True
        elif opt in ('-j', '--journal'):
            gpar.journal = True
        elif opt in ('-L', '--live'):
            gpar.live = True
        elif opt in ('-r', '--recursive'):
            gpar.recursive = True
        elif opt in ('-m', '--max'):
            gpar.against_max = True
        elif opt in ('-p', '--prev'):
            gpar.show_prev = True
        elif opt in ('-s', '--summary'):
            gpar.summary = True
        elif opt in ('-t', '--threshold'):
            try:
                gpar.threshold = timedelta(seconds = float(par))
            except ValueError:
                exit(2, 'invalid threshold: %s' % par, True)
        elif opt in ('-y', '--year'):
            try:
                gpar.year = int(par)
                datetime(gpar.year, 1, 1)
            except ValueError:
                exit(2, 'invalid year: %s' % par, True)

    if not args:
        if gpar.live:
            args = [LIVE_DIR]
        elif not gpar.journal:
            exit(2, 'no path given', True)

    setup_logging(gpar.logfile, gpar.loglevel)

    try:
        return process(args)
    except KeyboardInterrupt:
        return 4    # interrupted (Ctrl-C)


if __name__ == '__main__':
    sys.exit(main())
