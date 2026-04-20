"""Allow running as: python -m halos [tui|msg|task]"""

import sys

if len(sys.argv) > 1 and sys.argv[1] == "tui":
    from .tui import main
    main()
elif len(sys.argv) > 1 and sys.argv[1] == "msg":
    from .msg import run
    # Strip the 'msg' subcommand so msg.py sees only the recipient + message args
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    run()
elif len(sys.argv) > 1 and sys.argv[1] == "task":
    from .task_test import main
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    main()
else:
    from .main import run
    run()
