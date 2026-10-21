# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`monotonic_logs.py` checks SUSE supportconfig `messages*.txt` files
for non-monotonic (backward stepping) timestamps and reports every line whose
timestamp is earlier than its reference timestamp.

## Requirements

- Python standard library only, no external dependencies.
- Must stay compatible with Python 3.6 (SLES 15). Do not use
  `datetime.fromisoformat()`, f-strings with `=`, walrus operator,
  `dataclasses`, or other features newer than 3.6.
- Log section detection:
  - If a file contains the header line `#==[ Log File ]===...#` directly
    followed by `# /var/log/messages`, ignore everything before it.
  - The section ends at the next `#==[` header line.
  - Files without this header (e.g. `messages-yyyymmdd.txt`) are processed
    completely.
- Supported timestamp formats:
  - RFC3339 / ISO8601 (rsyslog high precision):
    `2024-01-15T10:23:45.123456+01:00`
  - Traditional syslog: `Jan 15 10:23:45` (no year; a step from December to
    January is a year change, not a backward step). The year is taken from
    the following RFC3339 timestamp, for the last run of syslog lines from
    the file name date (`messages-yyyymmdd.txt`) or else the file mtime,
    counting backwards.
- Lines without a timestamp (continuation lines) are skipped.
- Each file is checked independently.

## Output and exit codes

- stdout, one tab separated line per backward step:
  `<file>:<lineno>\t<negative delta>\t<log line>`
- stderr: per file summary and total, and warnings
  (`<file>:<lineno>: warning: ...`) if a derived syslog year is inconsistent
  with a preceding RFC3339 timestamp (that year is used) or gives an invalid
  date such as Feb 29 (line skipped). Warnings don't change the exit code.
- Exit codes: 0 = no backward steps, 1 = backward steps found,
  2 = a file could not be read, 3 = interrupted (Ctrl-C).
  Note: 1 is currently also returned for usage errors (bad option, invalid
  `-t`, no path given) and when no files are found.
- All comments and user visible output in English.

## Usage

```
./monotonic_logs.py [-hVvmp][-t secs][-l log] path...
  -h, --help            usage
  -V, --version         print version and exit
  -v, --verbose         verbose (cumulative: -v warning, -vv info, -vvv debug)
  -l, --logfile=fname   log to this file (default and `-`: stderr)
  -m, --max             compare against highest timestamp seen so far
  -p, --prev            print the reference line before each hit
  -t, --threshold=secs  report only backward steps >= secs
```

`path` may be a file, a glob pattern, or a directory (searched for
`messages*.txt`).

## Architecture (single file)

`main()` (getopt → `gpar`) → `process()` → `collect_files()` → per file
`check_file()` → `log_lines()` (section detection, reads whole file).

- `check_file()` works in two passes: first it collects entries
  `[no, line, ts, fields]` (`ISO_RE` tried before `BSD_RE`; syslog entries
  have `ts = None` and the parsed `fields`), then `assign_years()` walks the
  entries backwards and fills in syslog `ts` per run (anchor: following
  RFC3339 entry, or `reference_date()` for the last run), then hits are
  reported.
- Mixed naive (syslog) and aware (ISO) timestamps are compared by stripping
  tzinfo in `comparable()`, so offsets are ignored in mixed comparisons.
- Test files with only syslog timestamps need a dated name
  (`messages-yyyymmdd.txt`), otherwise results depend on the checkout mtime.
- `-m` works by *not* updating `prev_*` after a hit, so the reference stays
  the maximum; with `-p` the "previous" line printed is that maximum line.
- Options are stored as class attributes on `gpar`, so they persist across
  `main()` calls. Tests must save and restore `gpar.against_max`,
  `show_prev`, `threshold` and `loglevel` (e.g. in `setUp`/`tearDown`).
- `check_file()` writes hits directly via `stdout()`. Capture `sys.stdout`
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
syslog year derivation (0.2).

1. Possible enhancements (discuss before implementing):
   - Extend logic and parser to parse different sections in boot.txt
     - Starting with `# /usr/bin/journalctl --no-pager --boot` and
       `# {/usr,}/bin/dmesg -T`. Note the command path tolerance and the 
       different timestamp format for `dmesg`. Output ends with file or 
       another comment.
   - Read compressed input (`.gz`, `.xz`) via `gzip` / `lzma`.
   - Option to summarize the largest backward step per file, it's line number
     and the number of backward steps
   - Option to extend the search in all files of a given directory, that match
     all supported timestamp patterns, prefixing at least 5 consecutive 
     lines. 

