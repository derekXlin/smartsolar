"""Dynamic battery export control for a GloBird ZEROHERO site in an Australian site.

Secures the $1/day ZeroHero credit by holding grid import under 0.03 kWh in every
hour of 18:00-21:00, then sells whatever stored energy tomorrow's free 11:00-14:00
charging window would otherwise strand, at the $0.10/kWh Super Export rate.
"""

from .config import AppConfig
from .decision_engine import DecisionEngine
from .models import Decision, Telemetry

__version__ = "1.7.1"
__all__ = ["AppConfig", "DecisionEngine", "Decision", "Telemetry", "__version__"]
