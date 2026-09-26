#!/bin/sh
set -eu
ruff check lib tests tools setup-desktop.py
ruff format --check lib tests tools setup-desktop.py
mypy --strict lib tools setup-desktop.py
shellcheck bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/check.sh tests/with-xvfb.sh
shfmt -d -i 2 -ci bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/check.sh tests/with-xvfb.sh
bash -n bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/with-xvfb.sh
coverage run -m unittest discover -s tests -v
coverage combine
coverage report
python3 -m compileall -q lib setup-desktop.py
