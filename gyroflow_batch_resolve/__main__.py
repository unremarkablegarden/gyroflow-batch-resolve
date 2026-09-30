"""`python -m gyroflow_batch_resolve` runs the command line with arguments, the GUI without."""

import sys

if len(sys.argv) > 1:
    from .cli import main
    raise SystemExit(main())

from .gui import main
main()
