# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`monotonic_logs.py` checks SUSE supportconfig `messages*.txt` and `boot.txt`
files for non-monotonic (backward stepping) timestamps and reports every line whose
timestamp is earlier than its reference timestamp.

## Requirements

- Python standard library only, no external dependencies.
- Must stay compatible with Python 3.6 (SLES 15). Do not use
  `datetime.fromisoformat()`, f-strings with `=`, walrus operator,
  `dataclasses`, or other features newer than 3.6.
- Log section detection (`LOG_SECTIONS`):
  - Files with `#==[` headers are checked only in these sections, each
    independently:
    - `#==[ Log File ]===...#` followed by `# /var/log/messages`,
      `# /var/log/warn` or `# /var/log/localmessages` (`messages.txt`,
      `messages_localwarn.txt`; suffixes like `- Last 500 Lines` allowed)
    - `#==[ Command ]===...#` followed by `# {/usr,}/bin/journalctl ...` with
      `--no-pager` and any `--boot` (`--boot 0`, `--boot=-1`) or `--output` /
      `-o` form (e.g. `--output=short-iso | egrep ... | tail -500`), but not
      `--disk-usage` / `--list-boot`. Options are only looked up before the
      first `|`. Such sections also appear in other files, e.g. `x.txt`
      (not in `DIR_PATTERNS`, found with `-a` or when given explicitly).
    - `#==[ Command ]===...#` followed by `# {/usr,}/bin/dmesg ... -T`
  - With `-a`, any other `#==[` section with a run of `MIN_RUN` (5)
    consecutive timestamped lines is checked as well.
  - A section ends at the next `#==[` header line or EOF. A file with headers
    but no such section gives a `log.warning()` (visible with `-v`).
  - Files without any `#==[` header (e.g. `messages-yyyymmdd.txt`) are
    processed completely.
- Supported timestamp formats:
  - RFC3339 / ISO8601 (rsyslog high precision):
    `2024-01-15T10:23:45.123456+01:00`
  - `dmesg -T`: `[Mon Jan 15 10:23:45 2024]` (naive, weekday ignored).
  - Time since boot (`boot.msg`, plain `dmesg`, Xorg logs):
    `<6>[   14.028349]` (`MONO_RE`, optional priority). Parsed as `timedelta`,
    never part of the year logic and never compared with wall clock
    timestamps: when the clock type changes, the comparison starts over.
  - Traditional syslog / journalctl: `Jan 15 10:23:45` (no year; a step from
    December to January is a year change, not a backward step). The year of
    the first syslog line of a section comes from `-y`, or else is counted
    backwards from the first full (RFC3339 or dmesg) timestamp, or from the
    date in the file name (`messages-yyyymmdd.txt`) or supportconfig
    directory name (`[prefix_]scc_<host>_yymmdd_hhmm[_<uuid>]`); from there years are
    counted forwards and resynced at each full timestamp. The file mtime is
    not used for copied data (unreliable), only with `-L`.
  - Without `-y`, a file is skipped with an error (exit 2) if no year source
    exists or the derived year gives Feb 29 in a non-leap year. With `-y`,
    such a line is skipped with a warning.
- Lines without a timestamp (continuation lines) are skipped.
- Threshold: default `DEFAULT_THRESHOLD` (0.25 s) to tolerate small
  deviations (locking; real data: 160 of 161 RFC3339 steps < 0.25 s). For
  pairs where either timestamp has whole seconds (traditional syslog,
  journal short, `dmesg -T`, RFC3339 without fraction; 5th entry element
  `coarse`), `RESOLUTION` (1 s) is added, so `-1 s` steps are not reported
  by default. `-t 0` reports all (no resolution added).
- Rotated logs (`is_rotated()`, `ROTATED_RE` on the name without `.gz`/`.xz`:
  `-yyyymmdd[.txt|.log]`, `.N`, `.old`, `boot.omsg`) are only included in
  directory searches (both modes, also `-a` extras and `-r`) with `-R`.
  Files and globs given on the command line are always checked.
- Live system (`-L`): directories are searched for `LIVE_PATTERNS`
  (`messages`, `messages-*`, `warn`, `warn-*`, `localmessages`,
  `localmessages-*`, `boot.msg`, `boot.omsg`) instead of `DIR_PATTERNS`,
  `path` defaults to `LIVE_DIR` (`/var/log`). `-a` / `-r` / `-R` work as usual.
  These files have no `#==[` headers, so they are processed completely.
  Only with `-L` the file mtime is a (last resort) year source. Permission
  errors keep exit code 2, with the hint `(run as root?)`.
- Compressed files are read via `gzip` / `lzma`, detected by magic bytes
  (`MAGICS`, `opener()`, `decompress()`), not by extension (real data has
  gzip files named `.txz`). `COMPRESSED_EXTS` is only used to strip
  extensions for name checks. Corrupt or truncated files count as
  unreadable (exit 2, `READ_ERRORS`).
- Tar archives (`ARCHIVE_EXTS`: `.txz`, `.tar.xz`, `.tgz`, `.tar.gz`,
  `.tar`; `is_archive()` also checks the content) are read in one streaming
  pass (`archive_logs()`, `tarfile` mode `r|*`), never extracted. Members are
  selected like directory files (patterns, `-R`, `-a`; without `-r` only
  depth <= 1, i.e. the `scc_*/` directory), named `<archive>/<member>`, so
  `ref_date()` finds the `scc_*` date; `-L` uses the member mtime
  (`MEMBER_MTIMES`). A truncated archive reports the members read so far,
  then exit 2. Directory searches skip archives whose extracted directory
  exists next to them, and all archives with `-L`.
- Each file is checked independently.

## Output and exit codes

- stdout, one tab separated line per backward step:
  `<file>:<lineno>\t<negative delta>\t<log line>`; with `-s` only the largest
  step per file (first on ties, files without steps print nothing):
  `<file>:<lineno>\t<negative delta>\t<count>\t<cumulated delta>\t<log line>`
  (cumulated delta: sum of all backward steps of the file). With `-p` the
  reference line is printed before each printed step, followed by an empty
  line.
- stderr: per file summary of files with backward steps, the total (always),
  and warnings.
  - `-v` adds `, <checked> lines checked` (lines with a valid timestamp in
    checked sections) to both, and `in <n> files` to the total; `-vv` lists
    every checked file and shows `<checked> of <lines> lines checked`
    (`fmt_checked()`; files that failed are not counted).
  - Warnings `<file>:<lineno>: warning: ...` if a derived syslog year is
    inconsistent with a full timestamp (that year is used) or the
    file/directory name date, or (with `-y`) gives Feb 29 in a non-leap year
    (line skipped). Warnings don't change the exit code.
- Exit codes: 0 = no backward steps, 1 = backward steps found,
  2 = a file could not be read (or decompressed) or its year could not be
  validated,
  3 = interrupted (Ctrl-C).
  Note: 1 is currently also returned for usage errors (bad option, invalid
  `-t`, no path given) and when no files are found.
- All comments and user visible output in English.

## Usage

```
./monotonic_logs.py [-hVvaLmpRrs][-t secs][-y year][-l log] path...
  -h, --help            usage
  -V, --version         print version and exit
  -v, --verbose         verbose (cumulative: -v warning + checked lines,
                        -vv info + all files with lines, -vvv debug)
  -l, --logfile=fname   log to this file (default and `-`: stderr)
  -a, --all             also search directories for all other files with
                        logs, and check all sections with logs
  -L, --live            search a live system (LIVE_PATTERNS), path
                        defaults to /var/log
  -m, --max             compare against highest timestamp seen so far
  -p, --prev            print the reference line before each hit
  -R, --rotated         include rotated logs in directory searches
  -r, --recursive       search directories recursively (os.walk, sorted,
                        symlinked directories not followed)
  -s, --summary         print only the largest backward step per file,
                        with the number and sum of backward steps
  -t, --threshold=secs  report only backward steps >= secs (default 0.25,
                        +1 s for timestamps with whole seconds; 0: all)
  -y, --year=year       year of the first syslog timestamp (all files and
                        sections), required if it cannot be validated
```

`path` may be a file, a tar archive, a glob pattern (which may also match directories), or
a directory (`dir_files()`, with `-r` for each directory of the tree; searched for
`DIR_PATTERNS`: `messages*.txt`, `messages*.gz`, `messages*.xz`, `boot.txt`,
`boot.txt.gz`, `boot.txt.xz`). With `-a`, also all other non-binary regular
files with at least 5 consecutive lines starting with a
supported timestamp (`is_log_file()`, a pre-scan with the combined
`LOG_LINE_RE`, which must stay in sync with `ISO_RE` / `MONO_RE` /
`DMESG_RE` / `BSD_RE`; `test_log_line_re` checks this).

## Architecture (single file)

`main()` (getopt → `gpar`) → `process()` → `collect_files()` → per file
`check_file()` → `log_sections()` (section detection, reads whole file) →
per section `check_section()`. `check_file()` returns
`(backward steps, checked lines, lines)`, `check_section()` `(hits, checked)`.

- `check_section()` works in two passes: first it collects entries
  `[no, line, ts, fields, coarse]` (`ISO_RE`, then `MONO_RE`, then `DMESG_RE`, then `BSD_RE`; syslog
  entries have `ts = None` and the parsed `fields`), then `assign_years()` fills in
  syslog `ts` in one forward pass (start year from `start_year()`), then
  the hits are returned. `check_file()` collects the hits of all sections
  and prints them (or the `-s` summary) via `print_hit()`, so a `YearError`
  in any section prevents all output of that file; `process()` turns it
  into an error and exit code 2.
- Mixed naive (syslog) and aware (ISO) timestamps are compared by stripping
  tzinfo in `comparable()`, so offsets are ignored in mixed comparisons.
- Test files with only syslog timestamps need a dated file name or an
  `scc_*` directory (`testdata/scc_host1_240115_1200/boot.txt`), otherwise
  they raise `YearError`.
- `-m` works by *not* updating `prev_*` after a hit, so the reference stays
  the maximum; with `-p` the "previous" line printed is that maximum line.
- Options are stored as class attributes on `gpar`, so they persist across
  `main()` calls. Tests must save and restore `gpar.against_max`,
  `show_prev`, `threshold`, `year` and `loglevel` (`GparMixin` in the tests,
  which also sets `threshold` to 0 for detection tests; the default is
  tested in `TestThreshold`).
- `check_file()` writes hits via `stdout()`. Capture `sys.stdout`
  in tests; `stdout`/`stderr` look up `sys.stdout`/`sys.stderr` at call
  time, so `contextlib.redirect_stdout` works.

## Code conventions

The script follows the author's standard skeleton. Keep it intact:

- Module docstring with Synopsis / Usage / Description / Notes / Copyright /
  License sections; it is %-formatted with `gpar.__dict__` for `--help`, so
  literal percent signs must be written as `%%`.
- `__version__`, `__author__`, `__license__` at module level; bump
  `__version__` on functional changes.
- Global parameters live in `class gpar`; command line options are parsed
  with `getopt` in `main()` and stored there.
- Use the `stdout()` / `stderr()` lambdas for output, `log.*()` for
  diagnostics, `exit()` for termination with message/usage.
- Keyword arguments with spaces around `=` (`foo(bar = 1)`), as in the
  skeleton.
- 4 space indentation, keep the vim modeline.

## Testing

- Tests use `unittest` only (no pytest dependency). Run from the repo root;
  `-t .` puts the root on `sys.path` so tests can `import monotonic_logs`
  (the script is not a package; `tests/` needs an `__init__.py`):
  - all tests: `python3 -m unittest discover -s tests -t . -v`
  - single test: `python3 -m unittest tests.test_monotonic_logs.TestParseIso.test_offset -v`
- Python 3.6 check (the local interpreter is newer; podman is available,
  the base image has no python3, hence the `zypper` step):
  ```
  podman run --rm -v "$PWD":/src:Z -w /src registry.suse.com/suse/sle15 \
      sh -c 'zypper -q -n in python3 >/dev/null && python3 -m unittest discover -s tests -t . -v'
  ```
- Test data lives in `testdata/` and must be synthetic or anonymized.
  Compressed variants are created at runtime in a temp dir
  (`TestCompressed`), don't commit binary test files.
  Never commit real supportconfig data (hostnames, IPs, user names).
- Run the tests after every change; also check that the script still runs
  under Python 3.6 if an interpreter is available (e.g. a SLES 15 container).

## Workflow

- Work in small, well defined steps; propose a plan before larger changes.
- Do not touch `testdata/` contents that are referenced by tests unless the
  task requires it.
- Commit after each working step with a concise English commit message.
- Update this file as later changes are made.
- `tmp/` is ignored local scratch (older drafts), not part of the project.

## Suggested next steps

Done: synthetic test data, unit tests, Python 3.6 verification (3.6.15),
syslog year derivation (0.2), option `-y` (0.3), boot.txt sections (0.4), compressed input (0.5), summary option `-s` (0.6), option `-a` (0.7), cached tzinfo in `parse_iso()` (0.8, ~30% faster), journalctl `--output`
sections, quiet zero lines, cumulated delta in `-s` (0.9), option `-r`
(0.10), option `-L` for live systems and time since boot timestamps (0.11),
option `-R` for rotated logs (0.12), default threshold 0.25 s plus
resolution of whole seconds (0.13), checked lines with `-v` / `-vv` (0.14), tar archives (0.15), warn and localmessages
sections (0.16).

1. Option `-j` (discuss first): check the systemd journal of a live system
   via `journalctl --no-pager -o short-iso-precise` (subprocess, stdlib),
   e.g. per boot (`--list-boots`), as separate sections.

Further performance work (e.g. comparing raw RFC3339 strings) was considered
and rejected as not worth the complexity (~13 s for a 771 MB supportconfig).

