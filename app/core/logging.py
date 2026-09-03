import logging

import structlog


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.ConsoleRenderer()])
