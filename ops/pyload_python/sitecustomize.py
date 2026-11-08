"""Apply the operator's WARNING floor before pyLoad initializes its loggers.

The pinned pyLoad logger only selects DEBUG or INFO. Python's process-wide
logging floor survives logger/handler initialization and also covers file logs.
"""

import logging

logging.disable(logging.INFO)
