# Help exits before scheduled maintenance

Date: 2026-10-02. Status: isolated qualification passed; installation is recorded separately.

An actual invocation of `scheduled_nightly.py --help` started the nightly pass:
the script ignored its command line. The agent made this invocation while asking
for usage; that mistake and the resulting maintenance log are retained.
The weekly script has the same entry-point defect.

Three independent primary sources checked today:

- [Python 3.10 argparse](https://docs.python.org/3.10/library/argparse.html): automatic help, usage and invalid-argument errors.
- [Pallets Click callback order](https://click.palletsprojects.com/en/stable/click-concepts/): help exits before action callbacks.
- [Commander automated help](https://github.com/tj/commander.js#automated-help): generated help and strict unknown-option errors before actions.

The GNU and POSIX pages could not be fetched and are not counted as checked.
Choose the already available Python standard library parser. A special manual
check for only `--help` would still ignore other misspelled arguments; adding
Click or a second CLI framework would add an unnecessary dependency.
Both maintenance entry points parse before taking ownership or writing status.
No-argument scheduled execution and internal `main()` callers keep their behavior.
`-h`/`--help` exit zero, unknown arguments exit two. These are parser conventions,
not resource limits. No path, runtime root, environment variable or tool is added.

Regression tests execute the actual terminal script AST with a safe admission
sentinel, so the red run proves that help admitted maintenance without starting
real work. The exact entry point must exit before the sentinel is called.
Real subprocess help qualification additionally checks that no runtime directory
is created. The original accidental run is not relabeled as a deliberate test.

The same qualification fixes a live-update import defect: claims imported a new
helper from `reliable_memory` while an already running process could retain the
previous module without that export. An actual maintenance report records the
ImportError. The old export surface regression reproduced it before repair.
Claims now validates its parent through its existing `_connect` boundary and
closes that validation connection explicitly before taking its rebuild lock;
the duplicate parent validator and new import are removed. This initializes
only the existing derived claims database. The test proves the established
import surface and actual current database opening, not universal hot reload of
all code in an older running host. Existing hosts still retain loaded code until
their normal restart; no agent process is forcibly stopped.

Qualification: 7 red failures before changes; 57 related passes initially;
307 passes / 3 skips after adding real subprocess checks and the quality guards;
37 measured tests. Actual Lizard: changed entry points CCN 5, claims rebuild 4,
new tests at most 2. No complexity threshold, safety refusal or test assertion
was weakened. The accidental nightly completed with two actual failures:
a 5 383 252-byte day exceeded the older 4 MiB compile read bound, and fact-key
corpus collection reached its deadline. Its memory refresh completed in 68 s;
these independent unresolved failures are not relabeled successful maintenance.
