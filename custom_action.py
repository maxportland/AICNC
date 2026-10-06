# filepath: /home/max/linuxcnc/configs/AICNC/custom_action.py
from qtvcp.qt_action import _Lcnc_Action
from PyQt5.QtCore import QProcess  # Import QProcess
import os
from qtvcp.core import Status  # Import Status
from qtvcp import logger

LOG = logger.getLogger(__name__)

SUBPROGRAMS_PATH = os.path.abspath(os.path.join(
            os.path.dirname(__file__), 'subprograms.py'))

STATUS = Status()

class CustomAction(_Lcnc_Action):
    _instanceNum = 0
    _toolsetter_return = None

    def __init__(self):
        super().__init__()

    def probe_with_toolsetter(self, search_vel, probe_vel, max_probe,
                            z_offset, retract_distance, z_safe_travel, start_x, start_y, start_z, tool_number, rtn_method=None):

        self._toolsetter_return = rtn_method
        # What the subprogram reported, for the caller to check once the process has finished
        # (it exits normally even when the routine failed)
        self.completed = False
        self.error_text = ""

        # Log the parameters for debugging
        LOG.debug(f"Custom parameters: search_vel={search_vel}, probe_vel={probe_vel}, max_probe={max_probe}, "
              f"z_offset={z_offset}, retract_distance={retract_distance}, z_safe_travel={z_safe_travel}, "
              f"start_x={start_x}, start_y={start_y}, start_z={start_z}, tool_number={tool_number}")

        # only treat it as “already running” if it’s actually in the Running state
        if self.proc is not None and self.proc.state() == QProcess.Running:
            LOG.critical("Toolsetter routine already running.")
            return 0

        self.proc = QProcess()
        self.proc.setReadChannel(QProcess.StandardOutput)
        self.proc.started.connect(self.toolsetter_started)
        self.proc.readyReadStandardOutput.connect(self.read_stdout)
        self.proc.readyReadStandardError.connect(self.read_stderror)

        # hook up a cleanup slot that will clear self.proc once it’s done
        self.proc.finished.connect(self.toolsetter_finished)
        # also hook up the user‐supplied return callback if any
        if self._toolsetter_return:
            self.proc.finished.connect(self._toolsetter_return)

        try:
            self.proc.start(f'python3 {SUBPROGRAMS_PATH}')
        except Exception as e:
            LOG.error(f"Failed to start the subprogram: {e}")
            self.proc.close()
            self.proc = None
            return 0
        LOG.debug("Subprogram started.")

        # Prepare the string to send to the subprogram
        string_to_send = "toolsetter${}${}${}${}${}${}${}${}${}${}\n".format(
            str(search_vel),
            str(probe_vel),
            str(max_probe),
            str(retract_distance),
            str(z_safe_travel),
            str(z_offset),
            str(start_x),
            str(start_y),
            str(start_z),
            str(tool_number)
        )
        LOG.debug(f"String to send: {string_to_send}")
        # Block polling and send the data
        STATUS.block_error_polling()
        try:
            self.proc.writeData(bytes(string_to_send, 'utf-8'))
        except Exception as e:
            LOG.error(f"Error writing to process: {e}")
            STATUS.unblock_error_polling()
            return 0
        LOG.debug("Data sent to the subprogram.")
        return 1

    def toolsetter_finished(self, exitCode, exitStatus):
        LOG.debug(
            f"Toolsetter Process finished - exitCode {exitCode} exitStatus {exitStatus}"
        )
        # clear the QProcess so you can re‐start
        self.proc = None
        STATUS.unblock_error_polling()
        # clear your callback reference
        self._toolsetter_return = None

    def toolsetter_started(self):
        LOG.debug("Toolsetter subprogram started with PID {}\n".format(self.proc.processId()))

    def read_stdout(self):
        qba = self.proc.readAllStandardOutput()
        line = qba.data()
        self.parse_line(line)

    def read_stderror(self):
        qba = self.proc.readAllStandardError()
        line = qba.data()
        self.parse_line(line)

    def parse_line(self, line):
        line = line.decode("utf-8")
        if "COMPLETE" in line:
            self.completed = True
            if self._toolsetter_return is None:
                self.SET_DISPLAY_MESSAGE("Toolsetter routine returned successfully")
        elif "Routine error" in line or "Returned with error" in line or "Command error" in line:
            self.error_text = line.strip()
            self.SET_ERROR_MESSAGE(self.error_text)

        # This also gets error text sent from logging of ACTION library in the subprogram
        elif "ERROR" in line:
            # remove preceding text 'ERROR'
            s = line[line.find('ERROR')+6:]
            s = s[s.find(']')+1:]
            self.SET_ERROR_MESSAGE(s)
        elif "DEBUG" in line: # must set DEBUG level on LOG in top of this file
            LOG.debug(line[line.find('DEBUG')+6:])