"""Run a worker with in-memory coverage and return its trace through a pipe."""

from __future__ import annotations

import builtins
import json
import os
import runpy
import sys
from pathlib import Path
from typing import NoReturn

from coverage import Coverage


def main():
    pipe = int(sys.argv[1])
    branch = sys.argv[2] == "branch"
    module = sys.argv[3]
    sys.argv = sys.argv[3:]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    trace = Coverage(
        data_file=None, config_file=False, branch=branch, source=["tgdrive"]
    )
    saved = False

    def flush():
        nonlocal saved
        if saved:
            return
        saved = True
        trace.stop()
        # The worker's file-size limit remains active. A pipe needs no file write.
        data = trace.get_data()
        measure = data.arcs if branch else data.lines
        with os.fdopen(pipe, "w", encoding="utf-8") as output:
            json.dump(
                {filename: measure(filename) for filename in data.measured_files()},
                output,
            )

    if module == "tgdrive.media.compatibility_worker":
        original = os.execvpe

        def trace_before_exec(program, arguments, environment) -> NoReturn:
            flush()
            original(program, arguments, environment)

        os.execvpe = trace_before_exec
    if module == "tgdrive.media.archive_preview_worker":
        print_result = builtins.print

        def trace_before_reply(*values, **options):
            # The parent can kill the worker as soon as a terminal reply arrives.
            if (
                values
                and isinstance(values[0], str)
                and values[0].startswith(('{"result":', '{"error":'))
            ):
                flush()
            print_result(*values, **options)

        builtins.print = trace_before_reply
    trace.start()
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    finally:
        flush()


if __name__ == "__main__":
    main()
