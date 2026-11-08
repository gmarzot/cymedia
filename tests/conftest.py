import sys
from pathlib import Path

import cymedia

sys.path.insert(0, str(Path(__file__).parent))
cymedia.set_ffmpeg_log_level(16)
