#!/usr/bin/env python3
# Qtvcp touchoff and workpiece height measurement
#
# Copyright (c) 2022  Jim Sloot <persei802@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# This subprogram is used for qtdragon_handler touchoff routines
# and the qtdragon workpiece height measurement utility.
# It enables the probe routines to run without blocking the main GUI.

import sys
import json
import time
import linuxcnc

from PyQt5.QtCore import QObject
from qtvcp.core import Status, Action, Info
from qtvcp import logger

STATUS = Status()
ACTION = Action()
INFO = Info()

LOG = logger.getLogger(__name__)

class SubPrograms(QObject):
    def __init__(self):

        QObject.__init__(self)
        self.timeout = 30

        # input parameters
        self.search_vel = 10.0
        self.probe_vel = 10.0
        self.max_probe = 10.0
        self.retract_distance = 10.0
        self.z_safe_travel = 10.0
        self.start_x = 0.0
        self.start_y = 0.0
        self.start_z = 0.0
        self.tool_number = 1
        self.z_offset = 0.0  # Add this

        # output results
        self.status_z1 = 0.0
        self.status_z2 = 0.0
        self.send_dict = {}
        self.string_to_send = ""

        self.parm_list = ['search_vel',
                          'probe_vel',
                          'max_probe',
                          'retract_distance',
                          'z_safe_travel',
                          'start_x',
                          'start_y',
                          'start_z',
                          'z_offset',
                          'tool_number']

        self.process()
        LOG.info("SubPrograms initialized.")

    # mdi timeout setting
    def set_timeout(self, time):
        self.timeout = time

    def process(self):
        while 1:
            try:
                cmd = sys.stdin.readline()
                if cmd:
                    error = self.process_command(cmd)

                    # block polling here. main program should start polling in their end
                    STATUS.block_error_polling()

                    # error = 1 means success, error = None means ignore, anything else is an error
                    if error is not None:
                        if error != 1:
                            if type(error) == str:
                                sys.stdout.write("Routine error: {}\n".format(error))
                            else:
                                sys.stdout.write("Returned with error from cmd:{}\n".format(cmd))
                        else:
                            self.collect_status()
                            sys.stdout.write(self.string_to_send)
                    else:
                        sys.stdout.write("COMPLETE$ returned FROM COMMAND:{}\n".format(cmd))
                    sys.stdout.flush()
            except KeyboardInterrupt:
                    break
            except Exception as e:
                    sys.stdout.write("Command error: {}\n".format(e))
                    sys.stdout.flush()
            break

    def process_command(self, cmd):
        LOG.debug(f"Processing command: {cmd}")
        cmd = cmd.rstrip().split('$')
        if not STATUS.is_on_and_idle():
            LOG.warning(f"Machine is not ON and IDLE. Ignoring command.")
            return None
        pre = self.prechecks()
        if pre is not None:
            LOG.error(f"Prechecks failed: {pre}")
            return pre

        STATUS.unblock_error_polling()
        ACTION.CALL_MDI("G49")
        LOG.debug(f"COMMAND split: {cmd}")
        if cmd[0] == "toolsetter":
            try:
                self.search_vel = float(cmd[1])
                self.probe_vel = float(cmd[2])
                self.max_probe = float(cmd[3])
                self.retract_distance = float(cmd[4])
                self.z_safe_travel = float(cmd[5])
                self.z_offset = float(cmd[6])
                self.start_x = float(cmd[7])
                self.start_y = float(cmd[8])
                self.start_z = float(cmd[9])
                self.tool_number = int(cmd[10])
                self.status_z1 = 0.0
                self.status_z2 = 0.0
                error = self.toolsetter_routine()
            except Exception as e:
                LOG.error(f"Error parsing toolsetter parameters: {e}")
                return f"Error parsing toolsetter parameters: {e}"
        else:
            LOG.error(f"Unknown command: {cmd[0]}")
            return 'Most provide a command: ex: toolsetter'
        self.postreset()
        return error

    def CALL_MDI_WAIT(self, code, timeout = 5):
        LOG.debug(f"MDI_WAIT_COMMAND= {code}, maxt = {timeout}")
        for l in code:
            try:
                ACTION.CALL_MDI( l )
                result = ACTION.cmd.wait_complete(timeout)
                # give a chance for the error message to get to stdin
                time.sleep(.1)
                error = STATUS.ERROR.poll()
                if not error is None:
                    ACTION.ABORT()
                    LOG.debug(f"MDI error= {error[1]}")
                    return error[1]
            except Exception as e:
                ACTION.ABORT()
                LOG.error(f"Exception during MDI_WAIT_COMMAND: {e}")
                return e

            if result == -1:
                ACTION.ABORT()
                LOG.error(f"Command timed out: ({timeout} second)")
                return 'Command timed out: ({} second)'.format(timeout)
            elif result == linuxcnc.RCS_ERROR:
                ACTION.ABORT()
                LOG.error(f"MDI_COMMAND_WAIT RCS error")
                return 'MDI_COMMAND_WAIT RCS error'

        LOG.debug(f"MDI_WAIT_COMMAND completed successfully.")
        return 1

    # need to be in the right mode - entries are in machine units
    def prechecks(self):
        LOG.debug(f"Performing prechecks.")
        ACTION.CALL_MDI('M70')
        if INFO.MACHINE_IS_METRIC and STATUS.is_metric_mode():
            return None
        if not INFO.MACHINE_IS_METRIC and not STATUS.is_metric_mode():
            return None
        # record motion modes
        if INFO.MACHINE_IS_METRIC:
            ACTION.CALL_MDI('G21')
        else:
            ACTION.CALL_MDI('G20')
        ACTION.CALL_MDI('G90')
        ACTION.CALL_MDI('G40 G49 G80')
        return None

    # return to previous motion modes
    def postreset(self):
        LOG.debug(f"Resetting to previous motion modes.")
        ACTION.CALL_MDI('M72')

    def toolsetter_routine(self):
        LOG.debug(f"Starting toolsetter routine")

        tool_number = int(self.tool_number)
        LOG.info(f"Tool number: {tool_number}")
        if tool_number < 1 or tool_number > 99:
            LOG.error(f"Invalid tool number: {tool_number}")
            return f"Invalid tool number: {tool_number}"
        
        # Call MDI with tool number
        rtn = self.CALL_MDI_WAIT([f"T{tool_number} M6"], self.timeout)
        if rtn != 1:
            LOG.error(f"Tool change failed: {rtn}")
            return f"Tool change failed: {rtn}"
        LOG.debug(f"Tool change completed: T{tool_number} M6")

        name = 'Probe Z 1st positioning'
        LOG.info("Rapid jog to probing start position over toolsetter")
        # Rapid jog to first probe position
        cmdList = [
            "G90",
            f"G0 G53 Z{self.start_z + 20}",
            f"G0 G53 X{self.start_x} Y{self.start_y}",
            f"G0 G53 Z{self.start_z}"
        ]
        LOG.debug(f"Command list for first probe positioning: {cmdList}")
        rtn = self.CALL_MDI_WAIT(cmdList, self.timeout)
        if rtn != 1:
            LOG.error(f"{name} failed: {rtn}")
            return f'{name} failed: {rtn}'

        error = self.probe_down_to_toolsetter()
        ACTION.CALL_MDI("G90")
        if error != 1:
            LOG.error(f"Probing failed: {error}")
            return error

        s = f"G0 G53 Z{self.start_z}"
        LOG.debug(f"Moving to safe Z position with command: {s}")
        rtn = self.CALL_MDI_WAIT([s], self.timeout)
        if rtn != 1:
            LOG.error(f"Probe {name} failed: {rtn}")
            return f'Probe {name} failed: {rtn}'
        
        # Calculate tool length offset
        # self.z_offset should be the known height of the tool setter in machine coordinates
        # OR the probed position of a reference tool (tool 0)
        tool_length_offset = self.status_z2 - self.z_offset
        
        LOG.info(f"Probed position: {self.status_z2}")
        LOG.info(f"Tool setter offset: {self.z_offset}")
        LOG.info(f"Calculated tool length offset: {tool_length_offset}")

        # Set tool length offset - note the NEGATIVE value for typical setups
        ACTION.CALL_MDI(f"G10 L1 P{tool_number} Z{-tool_length_offset}")

        LOG.info(f"Toolsetter routine completed successfully.")
        return 1

    def probe_down_to_toolsetter(self):

        LOG.debug(f"Starting probe_down routine.")

        # Tool setter quick search probe
        name = '1st Probe down'
        ACTION.CALL_MDI("G91")
        cmd = f"G38.2 Z-{self.max_probe} F{self.search_vel}"
        LOG.info(f"Executing first probe down command: {cmd}")
        rtn = self.CALL_MDI_WAIT([cmd], self.timeout)
        if rtn != 1:
            LOG.error(f"{name} failed: {rtn}")
            return f'{name} failed: {rtn}'
        
        # Check if probe made contact by verifying we got a valid position
        self.status_z1 = self.get_z_position()
        if self.status_z1 is None:
            LOG.error("First probe did not make contact with tool setter!")
            return "First probe did not make contact"
        LOG.info(f"Probed Z1 position (status_z1): {self.status_z1}")

        # Retract the probe
        name = 'Probe retract'
        cmd = f"G1 Z{self.retract_distance} F{self.search_vel}"
        LOG.info(f"Executing probe retract command: {cmd}")
        rtn = self.CALL_MDI_WAIT([cmd], self.timeout)
        if rtn != 1:
            LOG.error(f"{name} failed: {rtn}")
            return f'{name} failed: {rtn}'

        # Actual probe for setting tool length offset
        name = '2nd Probe down'
        ACTION.CALL_MDI("G4 P0.5")
        cmd = f"G38.2 Z-{1.1 * self.retract_distance} F{self.probe_vel}"
        LOG.info(f"Executing second probe down command: {cmd}")
        rtn = self.CALL_MDI_WAIT([cmd], self.timeout)
        if rtn != 1:
            LOG.error(f"{name} failed: {rtn}")
            return f'{name} failed: {rtn}'
        
        # Check if probe made contact by verifying we got a valid position
        self.status_z2 = self.get_z_position()
        if self.status_z2 is None:
            LOG.error("Second probe did not make contact with tool setter!")
            return "Second probe did not make contact"
        LOG.info(f"Probed Z2 position (status_z2): {self.status_z2}")
        
        LOG.info(f"Toolsetter probe operation completed successfully.")
        return 1
    
    def get_z_position(self):
        probed_position = STATUS.get_probed_position()
        LOG.debug(f"Raw probed position: {probed_position}")
        
        if probed_position is None or len(probed_position) < 3:
            LOG.error(f"Failed to retrieve probed position.")
            return None
        
        try:
            z_from_probed_position = probed_position[2]
            z_value = round(float(z_from_probed_position), 3)
            # Additional check: if Z position is 0, probe likely didn't trigger
            if z_value == 0.0:
                LOG.warning(f"Z position is 0.0 - probe may not have contacted")
            return z_value
        except Exception as e:
            LOG.error(f"Error retrieving Z position: {e}")
            return None

    def collect_status(self):
        """
        Collects the results of the probing operation and prepares them for output.
        """
        LOG.debug(f"Collecting status: Z1={self.status_z1}, Z2={self.status_z2}")
        if self.status_z1 == 0.0 or self.status_z2 == 0.0:
            if self.status_z1 == 0.0 and self.status_z2 == 0.0:
                LOG.info("Probed positions are zero, likely due to consistent tool length.")
            else:
                LOG.warning("One of the probed positions is zero. Verify probing operation and offsets.")
        data = format(self.status_z1, '.3f')
        self.send_dict.update({'z1': data})
        data = format(self.status_z2, '.3f')
        self.send_dict.update({'z2': data})
        self.string_to_send = json.dumps(self.send_dict)
        LOG.debug(f"Status collected: {self.string_to_send}")

# required code for subscriptable iteration
    def __getitem__(self, item):
        """
        Allows the class to be subscriptable for getting attributes.
        
        Args:
            item (str): Attribute name.
        
        Returns:
            Any: Attribute value.
        """
        return getattr(self, item)
    def __setitem__(self, item, value):
        """
        Allows the class to be subscriptable for setting attributes.
        
        Args:
            item (str): Attribute name.
            value (Any): Value to set.
        """
        return setattr(self, item, value)

####################################
# Testing
####################################
if __name__ == "__main__":
    w = SubPrograms()


