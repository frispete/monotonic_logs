# monotonic_logs

Check log files for non-monotonic (backward stepping) timestamps: SUSE
supportconfig data (`messages*.txt`, `boot.txt`, also inside `.txz` and
other tar archives), the logs of a live system (`/var/log`) and the systemd
journal. Every line whose timestamp is earlier than its reference is
reported.

A single Python script, standard library only, Python 3.6+ (SLES 15).

## Usage

```
monotonic_logs.py [-hVvajLmpRrs][-t secs][-y year][-l log] [path...]
```

`path` may be a file, a tar archive, a glob pattern or a directory.

| Option | Meaning |
|---|---|
| `-a` | also check all other files and supportconfig sections with logs |
| `-j` | check the systemd journal, each boot separately |
| `-L` | live system: search for `messages`, `warn`, `boot.msg`, ...; path defaults to `/var/log` |
| `-m` | compare against the highest timestamp so far, not the previous line |
| `-p` | print the reference line before each backward step |
| `-R` | include rotated logs in directory searches |
| `-r` | search directories recursively |
| `-s` | only the largest step per file, with count and sum of all steps |
| `-t secs` | report steps >= secs (default 0.25, +1 s for whole second timestamps; 0: all) |
| `-y year` | year of the first syslog timestamp, if it can't be derived |
| `-v` | verbose: `-v` checked lines, `-vv` plan and every file, `-vvv` debug |

Examples:

```
monotonic_logs.py scc_host_240115_1200.txz          # a supportconfig archive
monotonic_logs.py -rs /work/cases                   # summary of a directory tree
sudo monotonic_logs.py -Ljm                         # live system and journal
```

Supported timestamps: RFC3339 (`2024-01-15T10:23:45.123456+01:00`),
traditional syslog (`Jan 15 10:23:45`, the year is derived from full
timestamps or the file/supportconfig name, or given with `-y`),
`dmesg -T` and time since boot (`[   14.028349]`). Gzip and xz compressed
files are read directly. `monotonic_logs.py -h` describes all details.

## Output

stdout, tab separated, one line per backward step:

```
<file>:<lineno>  <negative delta>  <log line>
```

stderr: a summary per file with backward steps and the total.

Exit codes: 0 = no backward steps, 1 = backward steps found or no files
found, 2 = usage error, 3 = a file could not be read or its year could not
be validated, 4 = interrupted.

## Tests

```
python3 -m unittest discover -s tests -t . -v
```

## License

GNU GPL v2, see `LICENSE`.
