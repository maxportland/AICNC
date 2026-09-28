############################
# Pendant Config panel handler
#
# Standard QTvcp panel handler file for:
#   qtvcp/panels/pendant_config/pendant_config.ui
# Used both when running as a standalone panel (qtvcp pendant_config)
# and when embedded directly by qtdragon_hd_handler.
############################

############################
# **** IMPORT SECTION **** #
############################
from qtvcp.core import Status
from PyQt5.QtCore import QTimer
from PyQt5 import QtWidgets
from datetime import datetime
import hal

# Set up logging
from qtvcp import logger

###########################################
# **** instantiate libraries section **** #
###########################################

STATUS = Status()
LOG = logger.getLogger(__name__)
#LOG.setLevel(logger.INFO) # One of DEBUG, INFO, WARNING, ERROR, CRITICAL

###################################
# **** HANDLER CLASS SECTION **** #
###################################

class HandlerClass:

    ########################
    # **** INITIALIZE **** #
    ########################
    # widgets allows access to widgets from the qtvcp files
    # at this point the widgets and hal pins are not instantiated
    def __init__(self, halcomp, widgets, paths):
        self.hal = halcomp
        self.w = widgets
        self.PATHS = paths

        # Signal monitoring
        self.monitor_timer = QTimer()
        self.previous_values = {}
        self.pendant_pins = []
        self.max_log_lines = 1000

    ##########################################
    # Special Functions called from QTVCP    #
    ##########################################

    # at this point:
    # the widgets are instantiated.
    # the HAL pins are built but HAL is not set ready
    def initialized__(self):
        """
        Called once the pendant config widgets and HAL pins are built.
        When embedded by qtdragon_hd_handler, self.w is the root QWidget
        created from pendant_config.ui.
        """
        try:
            # Make sure the root widget can expand inside whatever layout/tab
            # it is placed into.
            if isinstance(self.w, QtWidgets.QWidget):
                self.w.setMinimumSize(0, 0)
                self.w.setSizePolicy(
                    QtWidgets.QSizePolicy.Expanding,
                    QtWidgets.QSizePolicy.Expanding
                )

            # Let the log view grow with available space
            if hasattr(self.w, "plainTextEdit_signal_log"):
                self.w.plainTextEdit_signal_log.setSizePolicy(
                    QtWidgets.QSizePolicy.Expanding,
                    QtWidgets.QSizePolicy.Expanding
                )

        except Exception as e:
            LOG.warning(f"Pendant config sizing adjustment failed: {e}")

        self.init_pendant_config()
        self.setup_signal_monitor()

    ########################
    # callbacks from STATUS #
    ########################

    #######################
    # callbacks from form #
    #######################

    #####################
    # general functions #
    #####################
    def init_pendant_config(self):
        w = self.w

        # Connect UI buttons
        w.pushButton_clear_log.clicked.connect(self.clear_log)
        w.checkBox_monitor_enabled.toggled.connect(self.toggle_monitoring)

        # The settings groups and Save/Load/Apply have no backing implementation: the
        # pendant is configured in pendant.hal. Hide them so the panel doesn't pretend
        # to change anything; the signal monitor is the working part.
        for name in ("groupBox_jog", "c", "groupBox_overrides", "groupBox_display",
                     "groupBox_connection", "pushButton_save", "pushButton_load",
                     "pushButton_apply"):
            widget = getattr(w, name, None)
            if widget is not None:
                widget.hide()

        # Qt drops the oldest lines itself once the log is full
        w.plainTextEdit_signal_log.setMaximumBlockCount(self.max_log_lines)

    def setup_signal_monitor(self):
        """Initial setup for the HAL signal monitoring (one-time wiring)."""
        # Connect the timer callback once; actual start/stop is controlled
        # by the "Enable Monitoring" checkbox.
        self.monitor_timer.timeout.connect(self.update_signal_monitor)
        self.refresh_pendant_pins(initial=True)

    def refresh_pendant_pins(self, initial=False):
        """
        Refresh the list of pendant-related pins from HAL.
        Called at startup and whenever monitoring is (re)enabled.
        """
        self.pendant_pins = self.get_pendant_pins()

        if not self.pendant_pins:
            msg = "No pendant-related HAL pins found (keywords: whb, pdnt, pendant)."
            LOG.warning(msg)
            self.log_message(msg)
        elif initial:
            self.log_message(
                "Signal monitor initialized. Found {} pendant signals.".format(
                    len(self.pendant_pins)
                )
            )

        # Reset previous values so we log a fresh snapshot on next update.
        self.previous_values.clear()

    def get_pendant_pins(self):
        """Get all HAL pins related to the WHB04B-6 pendant."""
        pins = []
        try:
            # Prefer the detailed info call (list of dicts with 'NAME'),
            # but fall back to hal.pins() if needed for older versions.
            try:
                all_pins = hal.get_info_pins()
            except AttributeError:
                all_pins = hal.pins()

            pendant_keywords = ['whb', 'pdnt', 'pendant']

            for info in all_pins:
                # Coerce various possible structures to a pin name string.
                name = None
                if isinstance(info, dict):
                    name = info.get('NAME') or info.get('name')
                if not name:
                    name = str(info)

                lname = name.lower()
                for keyword in pendant_keywords:
                    if keyword in lname:
                        pins.append(name)
                        break

        except Exception as e:
            LOG.error("Error getting pendant pins: {}".format(e))

        # Filter out noisy or non-user-facing pins we don't care about
        # in the live monitor.
        pins = [p for p in pins if p != 'pendant-reset-toggle.time']

        return pins

    def toggle_monitoring(self, enabled):
        """Start or stop the signal monitoring."""
        if enabled:
            # Re-scan pins each time monitoring is enabled so that any
            # newly-created HAL pins are detected.
            self.refresh_pendant_pins(initial=False)

            if not self.pendant_pins:
                # Nothing to monitor – turn the checkbox back off.
                self.w.checkBox_monitor_enabled.blockSignals(True)
                self.w.checkBox_monitor_enabled.setChecked(False)
                self.w.checkBox_monitor_enabled.blockSignals(False)
                self.log_message("Monitoring not started: no pendant signals found.")
                return

            count = len(self.pendant_pins)
            self.log_message("Monitoring {} pendant pins...".format(count))

            # Start timer (update every 100ms) and immediately log a snapshot
            # so the user sees current values right away.
            self.monitor_timer.start(100)
            self.log_message("=== Monitoring Started ===")
            self.update_signal_monitor()
        else:
            self.monitor_timer.stop()
            self.log_message("=== Monitoring Stopped ===")

    def update_signal_monitor(self):
        """Read HAL pins and update the log."""
        try:
            show_unchanged = self.w.checkBox_show_unchanged.isChecked()

            for pin_name in self.pendant_pins:
                try:
                    # Get pin value directly from HAL.
                    current_value = hal.get_value(pin_name)

                    # Check if value changed
                    previous_value = self.previous_values.get(pin_name)

                    if current_value != previous_value:
                        # Value changed - always log
                        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                        # Determine the type and format the message
                        if isinstance(current_value, bool):
                            msg_val = "TRUE" if current_value else "FALSE"
                            msg = "[{}] {} = {}".format(timestamp, pin_name, msg_val)
                        elif isinstance(current_value, float):
                            msg = "[{}] {} = {:.4f}".format(timestamp, pin_name, current_value)
                        else:
                            msg = "[{}] {} = {}".format(timestamp, pin_name, current_value)

                        self.log_message(msg)
                        self.previous_values[pin_name] = current_value

                    elif show_unchanged and previous_value is None:
                        # First time seeing this pin (unchanged but we want an initial line)
                        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        msg = "[{}] {} = {} (initial)".format(timestamp, pin_name, current_value)
                        self.log_message(msg)
                        self.previous_values[pin_name] = current_value

                except Exception as e:
                    # Pin might not exist or be readable; log at debug level for troubleshooting.
                    LOG.debug("Error reading HAL pin {}: {}".format(pin_name, e))

        except Exception as e:
            LOG.error("Error updating signal monitor: {}".format(e))

    def log_message(self, message):
        """Add a message to the log viewer"""
        log = self.w.plainTextEdit_signal_log
        log.appendPlainText(message)

        # Auto-scroll if enabled
        if self.w.checkBox_auto_scroll.isChecked():
            scrollbar = self.w.plainTextEdit_signal_log.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def clear_log(self):
        """Clear the signal log"""
        self.w.plainTextEdit_signal_log.clear()
        self.previous_values.clear()
        self.log_message("Log cleared.")

    #####################
    # KEY BINDING CALLS #
    #####################

    ###########################
    # **** closing event **** #
    ###########################

    ##############################
    # required class boiler code #
    ##############################

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, item, value):
        return setattr(self, item, value)


################################
# required handler boiler code #
################################

def get_handlers(halcomp, widgets, paths):
    return [HandlerClass(halcomp, widgets, paths)]


