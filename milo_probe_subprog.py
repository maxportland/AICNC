#!/usr/bin/env python3
"""
qtvcp's probe routines, run the way its BasicProbe widget runs them (a subprocess: one line in,
"<routine>$<json parameters>", and COMPLETE$<json results> / ERROR ... lines out), with one change:
the descents beside the part are probe-protected.

qtvcp goes down with a plain G1, so if the probe was started a little too far over the part (or
there's a clamp), it is driven into it. Here that move is a G38.3: it stops as soon as the probe
touches anything, and the routine ends with a message instead of probing sideways from there.
"""

import sys

from qtvcp.core import Status
from qtvcp.widgets.probe_subprog import ProbeSubprog

STATUS = Status()

TOUCHED_ON_THE_WAY_DOWN = ("the probe touched something on the way down, so it stopped. Start it further "
                           "out from the part (or higher), then try again")


class MiloProbeSubprog(ProbeSubprog):
    def z_clearance_down(self):
        depth = self.data_z_clearance + self.data_extra_depth
        # Search speed: a touch on the way down has to stop within the probe's overtravel
        rtn = self.CALL_MDI_WAIT(f"G91\nG38.3 Z-{depth} F{self.data_search_vel}\nG90", self.timeout)
        if rtn != 1:
            return rtn
        STATUS.stat.poll()
        if STATUS.stat.probe_tripped:
            return TOUCHED_ON_THE_WAY_DOWN
        return 1


if __name__ == "__main__":
    MiloProbeSubprog()
    sys.exit(0)
