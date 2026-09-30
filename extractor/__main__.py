"""Process entry point: ``python -m extractor``.

Reads configuration from the environment, sends logs to stdout and serves
the app with uvicorn on the configured host and port (12-Factor VII).
"""

import uvicorn

from extractor.config import Settings
from extractor.logging_config import configure_logging
from extractor.main import create_app


def main() -> None:
    settings = Settings()
    configure_logging(settings.LOG_LEVEL)
    uvicorn.run(
        create_app(settings),
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,  # keep uvicorn's logs on our stdout handler
        log_level=settings.LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
