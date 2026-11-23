# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.environ.get("FLAKE_HOME", Path(__file__).resolve().parents[2]))
DATA = Path(os.environ.get("FLAKE_DATA", HOME / "data"))
EVALS = HOME / "evals"
