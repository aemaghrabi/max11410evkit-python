"""One module per tab. Each tab exposes Qt signals for user intents and slots to show results."""

from .adc_config import AdcConfigTab
from .dmm import DmmTab
from .fft import FftTab
from .histogram import HistogramTab
from .other import OtherTab
from .registers import RegistersTab
from .scope import ScopeTab
from .sequence import SequenceTab

__all__ = [
    "AdcConfigTab",
    "OtherTab",
    "SequenceTab",
    "RegistersTab",
    "ScopeTab",
    "DmmTab",
    "HistogramTab",
    "FftTab",
]
