"""A 组：冻结 V1 信号与退出，使用统一 V1.1 风控。"""

import sys
from pathlib import Path

_USER_DATA = str(Path(__file__).resolve().parents[1])
if _USER_DATA not in sys.path:
    sys.path.insert(0, _USER_DATA)

from hoshi_v11.freqtrade_strategy import HoshiV11Base


class HoshiSpotV11A(HoshiV11Base):
    account_id = "A"
